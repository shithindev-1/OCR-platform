"""Several tenants in one request (type-detection endpoint): grouping the read documents into
tenants, and judging, cropping and filling form_data for each tenant on its own."""

import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timezone
from functools import partial
from typing import Any, Hashable

from app.core.leasing.config import settings
from app.schemas.leasing.ocr import (
    INDIVIDUAL_REQUIRED_DOCUMENT_TYPE,
    detect_fields_for_tenant_type,
    is_company,
)
from app.services.leasing.concurrency import run_concurrently
from app.services.leasing.form_normalization import normalize_form_data, parse_date
from app.services.leasing.leasing.cross_messages import message_for, resolve_documents
from app.services.leasing.leasing.outcomes import message_for_log, tag_errors
from app.services.leasing.leasing.outputs import assign_type_file_names
from app.services.leasing.leasing.reports import add_error, fold_merged_pairs, merged_indices
from app.services.leasing.leasing.signatory import apply_signatory_role_from_license
from app.services.leasing.ocr_pipeline import (
    CROSS_VERIFICATION_UNAVAILABLE,
    VerificationUnavailable,
    log_request_usage,
    merge_extracted_data,
    verify_documents_across,
)
from app.services.leasing.post_processing import document_label
from app.services.leasing.type_detection.tenants import (
    LICENCE_TYPES,
    RENTAL_INDEX_TYPE,
    Tenant,
    Unit,
    copy_proof,
    group_companies,
    group_individuals,
    has_link_details,
)

logger = logging.getLogger("uae_ocr.leasing")


# Where form_data takes a value from when documents differ: the Emirates ID and visa are current
# and UAE-issued; a passport can be years older (an old occupation, say).
_VALUE_PRIORITY = {"national_id": 0, "visa": 1, "passport": 2}
_ID_FIELDS = ("National_Id", "Signatory_National_Id")


def is_mandatory_type(document_type: str, company: bool) -> bool:
    """The document a tenant cannot do without: an individual's National ID; a company's trade licence,
    commercial licence or initial approval. Every other document (a company signatory's ID, passport and
    visa included) is a supporting one: when it is not usable it is ignored, never failing the tenant."""
    return document_type in LICENCE_TYPES if company else document_type == INDIVIDUAL_REQUIRED_DOCUMENT_TYPE


def ignore_document(report: dict[str, Any], reason: str, note: str, *, stage: str | None = None) -> None:
    """Reports a document as `ignored` instead of failing: `ignored_reason` the fixed code, `note` the message
    in words, `error` empty. Its error, when the note says something else, is kept for the log only. A
    document ignored again for the same reason gets the new message added to its note."""
    if report.get("ignored"):
        if report.get("ignored_reason") == reason and note and note not in (report.get("note") or ""):
            report["note"] = f"{report['note']} {note}" if report.get("note") else note
        return
    if report["error"] and report["error"] != note:
        report["_detail"] = {**(report.get("_detail") or {}), "replaced_error": message_for_log(report["error"])}
    report.update(error=None, ignored=True, ignored_reason=reason, note=note)
    if stage:
        report["_stage"] = stage


def _reference_labels(named: list[str], types: dict[str, str], company: bool) -> list[str]:
    """The documents a cross-check conflict is judged against: a company's licence or approval, otherwise
    (and for an individual) the National ID. The documents that disagree with them are set aside."""
    if company:
        licences = [label for label in named if types.get(label) in LICENCE_TYPES]
        if licences:
            return licences
    return [label for label in named if types.get(label) == INDIVIDUAL_REQUIRED_DOCUMENT_TYPE]


def _without_home_country_id(document: dict[str, Any]) -> dict[str, Any]:
    """A passport's National_Id is its home-country national number (an Egyptian national number,
    say), never the tenant's Emirates ID: it is dropped, so it can neither fill form_data nor be
    reported as a second, different ID number."""
    if document["document_type"] != "passport" or not any(document["data"].get(field) for field in _ID_FIELDS):
        return document
    return {**document, "data": {**document["data"], **{field: None for field in _ID_FIELDS}}}


def _unmatched_message(unit: Unit, name: str, company: bool, *, several: bool) -> str:
    """The error for a supporting document that belongs to no tenant of the request. `name` (its
    label, e.g. "Ejari certificate 2") says which document; the fix names its plain type."""
    whom = "any tenant in this request" if several else "the tenant"
    kind = document_label(unit.document_type)
    if unit.document_type == RENTAL_INDEX_TYPE:
        if has_link_details(unit, company):
            owner = "any tenant's" if several else ("the company's" if company else "the tenant's")
            return (f"'{name}' doesn't match the property on {owner} Ejari certificate or tenancy contract. "
                    "Please upload the matching Ejari certificate or tenancy contract, or remove it.")
        return f"We couldn't read the property on '{name}'. Please upload a clearer copy."
    if company:
        if has_link_details(unit, company):
            return (f"'{name}' is not for the company on the trade licence, commercial licence or initial "
                    f"approval. Please upload the company's own {kind}, or remove it.")
        return (f"We couldn't confirm that '{name}' is for the company on the trade licence, commercial licence or "
                "initial approval. Please upload a clearer copy.")
    if unit.document_type in LICENCE_TYPES:
        if has_link_details(unit, company):
            return (f"'{name}' doesn't list {whom}. "
                    f"Please upload a {kind} that lists {whom}, or remove it.")
        return f"We couldn't read who is listed on '{name}'. Please upload a clearer copy."
    if has_link_details(unit, company):
        return f"'{name}' doesn't mention {whom}. Please upload the tenant's own {kind}, or remove it."
    return f"We couldn't confirm that '{name}' belongs to {whom}. Please upload a clearer copy."



def _card_aliases(documents: list[dict[str, Any]], own: dict[str, int], types: dict[str, str]) -> dict[str, str]:
    """Makes each front/back pair sent as two files ONE card in `own` and `types` (changed in place),
    and returns the names the model may use for it -> its label.

    The two sides are labelled "National ID 1" and "National ID 2" and the model sees the card as
    "National ID 1 + National ID 2"; it may answer with that, either side, or just "National ID". The
    card is named "National ID" when it is the tenant's only one of its type, otherwise by its front."""
    aliases: dict[str, str] = {}
    for document in documents:
        if not document.get("is_merged_pair"):
            continue
        front, _, back = document["document_name"].partition(" + ")
        if front not in own:
            continue
        index, document_type = own.pop(front), types.pop(front)
        own.pop(back, None)
        types.pop(back, None)
        plain = document_label(document_type)
        plain = plain[:1].upper() + plain[1:]
        label = plain if document_type not in types.values() and plain not in own else front
        own[label], types[label] = index, document_type
        # The plain type name stands for this card only when it is the tenant's one card of that type.
        for name in (document["document_name"], front, back) + ((plain,) if label == plain else ()):
            aliases[name.lower()] = label
    return aliases

# The fields holding a document's own expiry date.
_EXPIRY_FIELDS = {
    "national_id": ("National_Id_Expiry", "Signatory_National_Id_Expiry"),
    "passport": ("Passport_Expiry", "Signatory_Passport_Expiry"),
    "visa": ("Visa_Expiry", "Signatory_Visa_Expiry"),
    "trade_license": ("Trade_License_Expiry",),
    "commercial_license": ("Trade_License_Expiry",),
    "initial_approval": ("Initial_Approval_Expiry",),
}
# Documents a tenant holds one current copy of: an older, replaced or repeated copy is set aside. A trade
# licence and a commercial licence are one kind. Every other type (bank statements, cheques, ...) can
# legitimately come several times: only an exact repeat of the same file is set aside.
_COPY_KINDS = {
    "national_id": "national_id", "passport": "passport", "visa": "visa",
    "trade_license": "licence", "commercial_license": "licence", "initial_approval": "initial_approval",
}


def _expiry(unit: Unit) -> date | None:
    for field in _EXPIRY_FIELDS.get(unit.document_type, ()):
        value = unit.data.get(field)
        if value:
            parsed = parse_date(str(value))
            if parsed:
                return parsed
    return None


def _today() -> date:
    # The same day the self-check judges expiry by (ocr_pipeline.run_self_verification).
    return datetime.now(timezone.utc).date()


def _set_aside_copies(
    tenants: list[Tenant], units: list[Unit], items: list[dict[str, Any]], documents_report: list[dict[str, Any]],
    sha_by_key: dict[Hashable, tuple[str, ...]], company: bool,
) -> dict[Hashable, dict[str, Any]]:
    """Sets aside the copies a tenant does not need, so they never fail the tenant: an expired or replaced
    National ID, passport, visa, licence or approval next to a valid one of the same holder, the same
    document uploaded twice, and - when every copy failed - the older ones (the error stays on the newest).

    A set-aside copy is reported `ignored` with an `ignored_reason` and a `note`, its `error` empty; it is
    removed from its tenants, so it adds nothing to form_data, is not cross-checked and gets no processed
    file. A copy is only ever set aside for another copy PROVEN to be the same holder's (`copy_proof`) - a
    tenant's only copy, or someone else's, keeps its real error. Returns the set-aside keys and why.
    """
    unit_by_key = {unit.key: unit for unit in units}
    order = {unit.key: position for position, unit in enumerate(units)}
    # Judged on the errors as they were before anything is set aside, so a person shared by two
    # company tenants gets the same decision in both, whatever the order of the tenants.
    failed = {unit.key: any(documents_report[index]["error"] for index in unit.key) for unit in units}
    today = _today()
    aside: dict[Hashable, dict[str, Any]] = {}

    def proof(unit: Unit, kept: Unit) -> str | None:
        if sha_by_key.get(unit.key) and sha_by_key.get(unit.key) == sha_by_key.get(kept.key):
            return "same_file"
        return copy_proof(unit, kept, company)

    def put_aside(unit: Unit, kept: Unit, reason: str, how: str) -> None:
        if unit.key in aside:
            return
        expiry, kept_expiry = _expiry(unit), _expiry(kept)
        aside[unit.key] = {
            "reason": reason, "kept": kept.key, "proof": how,
            "expiry": expiry.strftime("%d-%m-%Y") if expiry else None,
            "kept_expiry": kept_expiry.strftime("%d-%m-%Y") if kept_expiry else None,
        }

    for tenant in tenants:
        kinds: dict[Any, list[Unit]] = {}
        for key in tenant.keys:
            unit = unit_by_key[key]
            kinds.setdefault(_COPY_KINDS.get(unit.document_type, ("same_file", unit.document_type)), []).append(unit)
        for kind, copies in kinds.items():
            if len(copies) < 2:
                continue
            if isinstance(kind, tuple):
                # Bank statements, cheques ...: only the same file uploaded again.
                for position, unit in enumerate(copies):
                    first = next((other for other in copies[:position]
                                  if sha_by_key.get(other.key) and sha_by_key.get(other.key) == sha_by_key.get(unit.key)), None)
                    if first is not None:
                        put_aside(unit, first, "duplicate", "same_file")
                continue
            valid = [unit for unit in copies if not failed[unit.key]]
            if valid:
                # The copy used: the valid one that expires last (the first uploaded on a tie).
                kept = max(valid, key=lambda unit: (_expiry(unit) or date.min, -order[unit.key]))
                for unit in copies:
                    how = None if unit is kept else proof(unit, kept)
                    if not how:
                        continue
                    if failed[unit.key]:
                        # Only a provably OLDER failed copy is set aside. One that may be the newer copy (a
                        # later or unreadable expiry - a blurry new passport, say) keeps its own error.
                        expiry, kept_expiry = _expiry(unit), _expiry(kept)
                        if expiry and expiry < today:
                            put_aside(unit, kept, "expired_copy", how)
                        elif expiry and kept_expiry and expiry <= kept_expiry:
                            put_aside(unit, kept, "replaced_copy", how)
                    else:
                        same = how in ("same_file", "same_document_number")
                        put_aside(unit, kept, "duplicate" if same else "replaced_copy", how)
                continue
            # Every copy failed: the newest keeps its error (that is the one to fix); older ones are set aside.
            dated = [unit for unit in copies if _expiry(unit)]
            if len(dated) < 2:
                continue
            newest = max(dated, key=lambda unit: (_expiry(unit), -order[unit.key]))
            for unit in dated:
                if unit is not newest and _expiry(unit) < _expiry(newest):
                    how = proof(unit, newest)
                    if how:
                        put_aside(unit, newest, "older_copy", how)

    if not aside:
        return aside
    for tenant in tenants:
        for keys in (tenant.keys, tenant.signatory_keys, tenant.company_keys):
            keys[:] = [key for key in keys if key not in aside]

    for key, decision in aside.items():
        unit = unit_by_key[key]
        kept_name = items[decision["kept"][0]]["document_name"]
        label = document_label(unit.document_type)
        note = {
            "expired_copy": f"An older {label} that has expired. The valid one ('{kept_name}') is used.",
            "replaced_copy": f"Another copy of this {label} was uploaded. '{kept_name}' is used.",
            "duplicate": f"The same {label} was uploaded more than once. '{kept_name}' is used.",
            "older_copy": f"An older {label}. Please fix the newer one ('{kept_name}').",
        }[decision["reason"]]
        for index in key:
            report = documents_report[index]
            replaced_error = report["error"]
            report["error"] = None
            report["ignored"] = True
            report["ignored_reason"] = decision["reason"]
            # The type-detection service rewrites the note with the kept copy's file names and pages.
            report["note"] = note
            report["_kept_names"] = [items[index]["document_name"] for index in decision["kept"]]
            report["_stage"] = "set_aside"
            report["_detail"] = {
                "kept": kept_name, "kept_document": decision["kept"][0] + 1, "proof": decision["proof"],
                "expiry": decision["expiry"], "kept_expiry": decision["kept_expiry"],
                **({"replaced_error": message_for_log(replaced_error)} if replaced_error else {}),
            }
    logger.info(
        "Copies set aside %s",
        [f"{key[0] + 1}:{unit_by_key[key].document_type}:{decision['reason']}:{decision['proof']}->{decision['kept'][0] + 1}"
         for key, decision in aside.items()],
    )
    return aside


def set_aside_repeated_files(
    candidates: list[dict[str, Any]], index_by_name: dict[str, int], documents_report: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Sets aside every exact repeat of a document already in the request - the same file content found as
    the same type (the same ID front uploaded twice, say) - before front/back pairing, which two identical
    fronts would confuse. The repeat is reported `ignored` as a `duplicate` of the first, with that
    document's tenant(s); it is never paired, checked, cropped or used for form_data. Returns the others."""
    first_by_content: dict[tuple[str, str], dict[str, Any]] = {}
    kept: list[dict[str, Any]] = []
    for candidate in candidates:
        content_key = (candidate.get("sha256") or "", candidate["document_type"])
        first = first_by_content.get(content_key) if content_key[0] else None
        if first is None:
            if content_key[0]:
                first_by_content[content_key] = candidate
            kept.append(candidate)
            continue
        first_index = index_by_name[first["document_name"]]
        report = documents_report[index_by_name[candidate["document_name"]]]
        label = document_label(candidate["document_type"])
        report.update(
            error=None, ignored=True, ignored_reason="duplicate",
            note=f"This {label} was uploaded more than once. '{first['document_name']}' is used.",
            _stage="set_aside", _kept_names=[first["document_name"]], _duplicate_of=first_index,
            _detail={"kept": first["document_name"], "kept_document": first_index + 1, "proof": "same_file"},
        )
    if len(kept) < len(candidates):
        logger.info(
            "Repeated files set aside %s",
            [f"{index_by_name[c['document_name']] + 1}->{index_by_name[first['document_name']] + 1}:{c['document_type']}"
             for c in candidates if c not in kept
             for first in [first_by_content[(c.get('sha256') or '', c['document_type'])]]],
        )
    return kept


async def finish_per_tenant(
    *, tenant_type: str, request_id: str, items: list[dict[str, Any]], candidates: list[dict[str, Any]],
    resolved: list[dict[str, Any]], extracted: list[dict[str, Any]], documents_report: list[dict[str, Any]],
    index_by_name: dict[str, int], crop_and_upload: Callable[[dict[str, Any]], Awaitable[bool]],
    finish: Callable[..., dict[str, Any]], stage_done: Callable[[str, int], None],
) -> dict[str, Any]:
    """The end of `process_batch` for detected types, where one request may hold several tenants.

    Every document that was read is grouped into tenants (app.services.type_detection.tenants). Each tenant is then
    judged on its own. A supporting document that cannot be used - it failed its own checks, belongs to no
    tenant, or disagrees with the tenant's National ID (a company's licence or approval) in the cross-check -
    is IGNORED (ignored_reason + note, no error) and left out; the tenant's valid documents are cropped,
    uploaded and fill its form_data. Only the tenant's mandatory document missing or failing (or a problem on
    our side) fails it: its form_data stays empty, and it never affects another tenant.

    Returns the normal response plus `tenants` - [{tenant_id, status, form_data}] with form_data in
    the pipeline's field names - and `tenant_ids_by_name` (document_name -> tenant ids).
    """
    company = is_company(tenant_type)
    fields, _ = detect_fields_for_tenant_type(tenant_type)
    passed_by_key = {tuple(merged_indices(document, index_by_name)): document for document in extracted}

    # One unit per document that was read: a merged front/back pair is one unit of both uploads.
    units: list[Unit] = []
    covered: set[int] = set()
    for document in resolved:
        key = tuple(merged_indices(document, index_by_name))
        covered.update(key)
        units.append(Unit(key=key, document_type=document["document_type"], data=document["data"]))
    for candidate in candidates:
        index = index_by_name[candidate["document_name"]]
        if index not in covered and not candidate.get("_unmatched_page"):  # read, but failed front/back pairing
            units.append(Unit(key=(index,), document_type=candidate["document_type"], data=candidate["data"]))
    units.sort(key=lambda unit: unit.key[0])

    grouping = group_companies(units) if company else group_individuals(units)
    tenant_ids = [f"tenant_{number}" for number in range(1, len(grouping.tenants) + 1)]
    # Upload positions (1-based) and types only - never names or numbers read off the documents.
    logger.info(
        "Tenant grouping request_id=%s tenant_type=%s tenants=%s unassigned=%s unlisted_people=%s",
        request_id, "company" if company else "individual",
        {
            tenant_id: [f"{index + 1}:{items[index]['document_type']}" for key in tenant.keys for index in key]
            for tenant_id, tenant in zip(tenant_ids, grouping.tenants)
        },
        [index + 1 for key in grouping.unassigned for index in key],
        [[index + 1 for key in keys for index in key] for keys in grouping.unlisted_people],
    )
    tenant_ids_by_index: dict[int, list[str]] = {}
    for tenant_id, tenant in zip(tenant_ids, grouping.tenants):
        for key in tenant.keys:
            for index in key:
                tenant_ids_by_index.setdefault(index, []).append(tenant_id)
    # An exact repeat set aside before pairing (set_aside_repeated_files) belongs to its original's tenant(s).
    for index, report in enumerate(documents_report):
        if report.get("_duplicate_of") is not None and report["_duplicate_of"] in tenant_ids_by_index:
            tenant_ids_by_index[index] = list(tenant_ids_by_index[report["_duplicate_of"]])

    sha_by_index = {index_by_name[candidate["document_name"]]: candidate.get("sha256") for candidate in candidates}
    sha_by_key = {unit.key: tuple(sha_by_index.get(index) or "" for index in unit.key) for unit in units}
    _set_aside_copies(grouping.tenants, units, items, documents_report, sha_by_key, company)

    # Valid documents are always processed; a document that cannot be used is IGNORED (ignored_reason +
    # note) instead of failing its tenant. Only a tenant's mandatory document (an individual's National ID,
    # a company's licence or approval) missing or failing - or a problem on our side - is a real error.
    unit_by_key = {unit.key: unit for unit in units}

    # A document that belongs to no tenant joins none: it gets no processed file and adds nothing to form_data.
    for key in grouping.unassigned:
        for index in key:
            ignore_document(documents_report[index], "not_linked", _unmatched_message(
                unit_by_key[key], items[index]["document_name"], company, several=len(grouping.tenants) > 1,
            ), stage="tenant_match")

    for key in (key for keys in grouping.unlisted_people for key in keys):
        for index in key:
            ignore_document(documents_report[index], "not_linked", (
                f"The person on '{items[index]['document_name']}' is not listed on any uploaded trade licence, "
                "commercial licence or initial approval. Please upload their company's trade licence, commercial "
                "licence or initial approval."
            ), stage="not_listed_on_licence")

    # A supporting document that could not be read or confirmed (it never reached a tenant) is ignored; an
    # unreadable mandatory one keeps its error - it is the document the tenant needs.
    in_units = {index for unit in units for index in unit.key}
    for index, report in enumerate(documents_report):
        if (index not in in_units and report["error"] and not report.get("ignored")
                and not is_mandatory_type(items[index]["document_type"], company)):
            ignore_document(report, "invalid", report["error"])

    def indices(tenant: Tenant) -> list[int]:
        return sorted({index for key in tenant.keys for index in key})

    def has_errors(tenant: Tenant) -> bool:
        return any(documents_report[index]["error"] for index in indices(tenant))

    def drop_ignored(tenant: Tenant) -> None:
        tenant.keys[:] = [key for key in tenant.keys if not all(documents_report[i].get("ignored") for i in key)]

    # An individual without their National ID fails as before, every document showing why.
    missing = [] if company else [tenant for tenant in grouping.tenants if not any(
        items[index]["document_type"] == INDIVIDUAL_REQUIRED_DOCUMENT_TYPE for index in indices(tenant))]
    # A company whose licence lists nobody that was uploaded (owner 2026-11-16): not an error - the licence stays
    # valid and is processed, its `note` says a signatory is still needed, and the company tenant succeeds with
    # the company's fields, the signatory's in `missing`.
    if company:
        for tenant in grouping.tenants:
            if not tenant.signatory_keys:
                for index in indices(tenant):
                    if is_mandatory_type(items[index]["document_type"], company) and not documents_report[index]["error"]:
                        documents_report[index]["note"] = (
                            "None of the people listed on this trade licence, commercial licence or initial approval "
                            "were uploaded. Please upload the ID documents of a person it lists."
                        )
                        documents_report[index]["_detail"] = {**(documents_report[index].get("_detail") or {}),
                                                              "no_signatory_uploaded": True}

    # Every other tenant: a supporting document that failed its own checks (expired, incomplete, covered,
    # front/back not matched ...) is ignored with its message as the note; the valid ones carry on.
    for tenant in grouping.tenants:
        if any(tenant is other for other in missing):
            continue
        for key in tenant.keys:
            if is_mandatory_type(unit_by_key[key].document_type, company):
                continue
            for index in key:
                report = documents_report[index]
                if report["error"] and not report.get("ignored"):
                    ignore_document(report, "invalid", report["error"])
        drop_ignored(tenant)

    for tenant in missing:
        first = items[indices(tenant)[0]]["document_name"]
        message = f"No National ID was uploaded for the person on '{first}'. Please upload their National ID."
        for index in indices(tenant):
            add_error(documents_report[index], message)
        tag_errors(documents_report, "no_national_id")

    # Cross-check each tenant's own documents, all tenants at once.
    def own_documents(tenant: Tenant) -> list[dict[str, Any]]:
        """The tenant's passed documents whose fields describe the tenant - for an individual, not a
        licence that merely lists them - in the order form_data takes values from: the National ID
        first, then the visa, then the passport, then the rest (the first value found wins)."""
        documents = [
            _without_home_country_id(passed_by_key[key]) for key in sorted(tenant.keys)
            if key in passed_by_key and (company or passed_by_key[key]["document_type"] not in LICENCE_TYPES)
        ]
        return sorted(documents, key=lambda document: _VALUE_PRIORITY.get(document["document_type"], len(_VALUE_PRIORITY)))

    async def cross_check(tenant: Tenant) -> None:
        # A rental index describes a property, not a person: there is nothing on it to compare.
        documents = [document for document in own_documents(tenant) if document["document_type"] != RENTAL_INDEX_TYPE]
        if has_errors(tenant) or len(documents) < 2 or not settings.validation_enabled:
            return
        try:
            issues = await verify_documents_across(
                request_id=request_id, documents=documents, tenant_type=tenant_type, short_messages=True,
            )
        except VerificationUnavailable:
            issues = [CROSS_VERIFICATION_UNAVAILABLE]
        own = {items[index]["document_name"]: index for index in indices(tenant)}
        types = {name: items[index]["document_type"] for name, index in own.items()}
        aliases = _card_aliases(documents, own, types)
        for issue in issues:
            if isinstance(issue, dict):
                # A structured conflict: each involved document gets its own message.
                named = resolve_documents(issue.get("documents") or [], types, aliases)
                messages = {label: message_for(label, {**issue, "documents": named or list(own)}, types)
                            for label in (named or own)}
            else:
                # Labels are plain type names ("Passport"), which can also appear as ordinary words in
                # a message, so a quoted label - as the prompt asks for - is matched first.
                lowered = issue.lower()
                named = ([name for name in own if f"'{name.lower()}'" in lowered]
                         or [name for name in own if name.lower() in lowered])
                messages = {label: issue for label in (named or own)}
            if not named or issue == CROSS_VERIFICATION_UNAVAILABLE:
                # The check could not run, or the finding names no document: never dropped - a real
                # error on every document, as nothing tells which one is wrong.
                for label in own:
                    add_error(documents_report[own[label]], messages[label])
                continue
            reference = _reference_labels(named, types, company)
            wrong = [label for label in named if label not in reference]
            if not wrong:
                # Only reference documents disagree (two National IDs, a licence and its approval): the
                # tenant's mandatory document is in doubt - a real error.
                for label in named:
                    add_error(documents_report[own[label]], messages[label])
                continue
            # The documents that disagree with the reference - or, with no reference among them, every
            # document named - are set aside; the reference and the other documents carry on.
            for label in wrong:
                mismatched.setdefault(own[label], []).append(messages[label])

    mismatched: dict[int, list[str]] = {}
    await run_concurrently([partial(cross_check, tenant) for tenant in grouping.tenants], stage="cross_verification")
    key_by_index = {index: unit.key for unit in units for index in unit.key}
    for first, messages in mismatched.items():
        for index in key_by_index.get(first, (first,)):
            for message in messages:
                ignore_document(documents_report[index], "mismatch", message, stage="cross_verification")
    for tenant in grouping.tenants:
        drop_ignored(tenant)
    if mismatched:
        logger.info("Cross-check mismatches set aside request_id=%s documents=%s", request_id,
                    sorted(index + 1 for first in mismatched for index in key_by_index.get(first, (first,))))
    stage_done("cross_verification", len(extracted))

    # Crop and upload every document of every passing tenant - once, even when shared.
    to_crop = {
        key: passed_by_key[key]
        for tenant in grouping.tenants if not has_errors(tenant)
        for key in tenant.keys if key in passed_by_key
    }
    # Every processed file says whose it is - also with a single tenant: Tenant_1_National_ID.pdf.
    tenant_numbers = {
        key: [number for number, tenant in enumerate(grouping.tenants, start=1) if key in tenant.keys]
        for key in to_crop
    }
    assign_type_file_names(to_crop, items, tenant_numbers)
    await run_concurrently([partial(crop_and_upload, document) for document in to_crop.values()], stage="crop_rotate_upload")
    stage_done("crop_rotate_upload", len(to_crop))
    for document in extracted:
        document.pop("content", None)

    tenants = []
    for tenant_id, tenant in zip(tenant_ids, grouping.tenants):
        if has_errors(tenant):
            tenants.append({"tenant_id": tenant_id, "status": "failed", "form_data": {}})
            continue
        documents = own_documents(tenant)
        data = merge_extracted_data(documents, fields)
        if company:
            apply_signatory_role_from_license(data, documents, ignore_spacing=True)
        normalize_form_data(data)
        tenants.append({"tenant_id": tenant_id, "status": "success", "form_data": data})

    # An exact repeat follows its original: when the original was itself set aside (an expired copy sent
    # twice), so is the repeat, for the same reason; when the original failed, the repeat points at it.
    for report in documents_report:
        original_index = report.get("_duplicate_of")
        if original_index is None:
            continue
        original = documents_report[original_index]
        if original.get("ignored"):
            report.update(ignored_reason=original["ignored_reason"], note=original["note"],
                          _kept_names=original.get("_kept_names"))
        elif original["error"]:
            report["_original_failed"] = True

    # Every front/back pair read as one card is one entry - also one that failed, so its message
    # is shown once, and a pair failed by a later check keeps that check's message.
    merged_pairs = [
        (*merged_indices(document, index_by_name), document["document_type"])
        for document in resolved if document.get("is_merged_pair")
    ]
    # success: every tenant succeeded and no document has a real error - ignored documents never fail it.
    status = "success" if tenants and all(tenant["status"] == "success" for tenant in tenants) and not any(
        report["error"] and not report.get("ignored") for report in documents_report
    ) else "failed"
    logger.info(
        "Tenant results request_id=%s status=%s tenants=%s",
        request_id, status, {tenant["tenant_id"]: tenant["status"] for tenant in tenants},
    )
    log_request_usage(request_id, status, settings.detection_enabled)
    response = finish(status, documents=fold_merged_pairs(documents_report, merged_pairs))
    response["tenants"] = tenants
    response["tenant_ids_by_name"] = {
        items[index]["document_name"]: ids for index, ids in tenant_ids_by_index.items()
    }
    return response

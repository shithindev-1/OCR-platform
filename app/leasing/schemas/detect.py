"""Request and response models for the type-detection endpoints, /ocr/leasing/detect."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class DetectSource(BaseModel):
    """One file link. Nothing else is accepted: the document's type is found, never declared."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(..., description="A blob path in the configured container, or an allowed URL.")


class DetectOcrRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_type: str
    document_refnumber: str
    sources: list[DetectSource]


class DetectedSource(BaseModel):
    source: str = Field(..., description="The source URL or path, or the uploaded file's name.")
    file_name: str
    pages: list[int] = Field(..., description="Pages of that file this document was found on.")
    ocr_file: str | None = Field(None, description="Blob URL of the uploaded cropped/rotated result, if any.")


class DetectedDocument(BaseModel):
    """One document found in the uploads - a front/back pair from two files is a single entry."""

    tenant_ids: list[str] = Field(
        ...,
        description="The tenant(s) this document belongs to, matching form_data[].tenant_id. A company's "
                    "trade licence belongs to every tenant (signatory) of that company; [] when it fits none.",
    )
    document_type: str = Field(..., description='The type found, e.g. "national_id"; "other" when unsupported.')
    status: Literal["valid", "invalid", "ignored", "not_checked"] = Field(
        ...,
        description="valid: passed its checks and was fully processed (cropped, rotated, uploaded, used in "
                    "form_data). invalid: see error - only a tenant's mandatory document (an individual's National "
                    "ID, a company's licence or approval) missing or failing, or a problem on our side. ignored: not "
                    "used (see ignored_reason and note) - never an error, and the valid documents are still "
                    "processed. not_checked: another file could not be read, so nothing was checked.",
    )
    merged: bool
    sources: list[DetectedSource]
    error: str | None = Field(
        ..., description="Only ever a problem that stops a tenant; null for valid and ignored documents.",
    )
    ignored_reason: Literal[
        "expired_copy", "replaced_copy", "duplicate", "older_copy", "invalid", "not_supported", "not_linked",
        "mismatch",
    ] | None = Field(
        None,
        description="Why a document was ignored (null otherwise): expired_copy - an expired copy next to a valid "
                    "one of the same holder; replaced_copy - another copy of the holder's document is used; "
                    "duplicate - the same document uploaded again; older_copy - every copy failed, and the error is "
                    "shown on the newest one only; invalid - a supporting document that failed its own checks "
                    "(expired, incomplete, unreadable, covered ...); not_supported - not one of the accepted "
                    "document types; not_linked - it belongs to no tenant of the request; mismatch - it disagrees "
                    "with the tenant's National ID (a company's licence or approval). The note says what to fix.",
    )
    note: str | None = Field(
        None,
        description="Plain-English message, never an error. On an ignored document: what was wrong, or which copy is "
                    "used. On a valid document: information only - e.g. a company licence listing nobody whose ID "
                    "documents were uploaded (the company's fields are filled, the signatory's are in missing).",
    )


class TenantMissing(BaseModel):
    tenant_id: str
    fields: list[str] = Field(..., description="The form_data keys that are null for this tenant.")


class DetectOcrResponse(BaseModel):
    status: Literal["success", "failed"] = Field(
        ..., description="success when every tenant succeeded - ignored documents never make it failed.",
    )
    tenant_type: str
    document_refnumber: str
    form_data: list[dict[str, Any]] = Field(
        ...,
        description="One entry per tenant (a person; for a company, one per company and signatory): "
                    "tenant_id, status (success/failed), then every field - all null when that tenant failed.",
    )
    missing: list[TenantMissing]
    processing_time: str
    documents: list[DetectedDocument]

"""Token accounting for Cheque: one log line per Gemini call, and per request and document.

The same idea as Leasing's hits / token-breakdown / usage / document-usage / timing logs, so what a
cheque costs can be followed call by call, document by document and request by request:

    hits.jsonl             every model call: tokens, time, status, document type
    token-breakdown.jsonl  the same call with its input split (text / image / pdf) and output split
                           (answer / thinking)
    usage.jsonl            one line per request: every kind of call made, and the request total
    document-usage.jsonl   one line per document (an uploaded file): its type and its tokens;
                           the lines of a request add up to the request's line in usage.jsonl
    timing.jsonl           one line per request: its time, each stage's, and each document's steps

A cheque file's document type is always "cheque". Every attempt is counted, including a retry after an
unusable answer: those tokens are billed even though the answer was thrown away. Dollar cost is not
estimated here (`estimated_cost_usd` stays null, as in Leasing's hits.log): multiply the tokens by the
model's current price.
"""

import contextvars
import json
import logging
import time
import uuid
from typing import Any, Optional

from app.core.cheque.logging import (
    DOCUMENT_USAGE_LOGGER_NAME,
    HITS_LOGGER_NAME,
    TIMING_LOGGER_NAME,
    TOKEN_BREAKDOWN_LOGGER_NAME,
    USAGE_LOGGER_NAME,
)
from app.core.cheque.tracing import current_trace
from app.core.logging import log_timestamps
from app.schemas.cheque import TokenUsage

DOCUMENT_TYPE = "cheque"
CALL_EXTRACTION = "extraction"          # the first answer for a file
CALL_RETRY = "model_retry"              # the same file asked again after a failed or unusable answer
CALL_EMPTY_READ = "empty_read_retry"    # a sharp cheque came back with nothing read: asked once more
CALLS = (CALL_EXTRACTION, CALL_RETRY, CALL_EMPTY_READ)
UNASSIGNED = "unassigned"

logger = logging.getLogger("cheque.usage_flow")
hits_logger = logging.getLogger(HITS_LOGGER_NAME)
token_breakdown_logger = logging.getLogger(TOKEN_BREAKDOWN_LOGGER_NAME)
usage_logger = logging.getLogger(USAGE_LOGGER_NAME)
document_usage_logger = logging.getLogger(DOCUMENT_USAGE_LOGGER_NAME)
timing_logger = logging.getLogger(TIMING_LOGGER_NAME)

_calls: contextvars.ContextVar[Optional[list[dict[str, Any]]]] = contextvars.ContextVar(
    "cheque_usage_calls", default=None
)


def start_request() -> contextvars.Token:
    """Begin collecting this request's calls (the middleware does this once per API hit)."""
    return _calls.set([])


def end_request(token: contextvars.Token) -> None:
    _calls.reset(token)


def modality_tokens(metadata: Any) -> dict[str, int]:
    """Prompt tokens by kind (text / image / document) from Gemini's usage metadata, when it gives them."""
    totals: dict[str, int] = {}
    for detail in getattr(metadata, "prompt_tokens_details", None) or []:
        modality = getattr(detail, "modality", None)
        name = str(getattr(modality, "name", modality)).lower()
        totals[name] = totals.get(name, 0) + (getattr(detail, "token_count", 0) or 0)
    return totals


def record_call(
    *,
    file: str,
    content_type: str,
    model: str,
    call: str,
    attempt: int,
    status: str,
    started_at: float,
    usage: Optional[TokenUsage] = None,
    modalities: Optional[dict[str, int]] = None,
    cached_tokens: Optional[int] = None,
    error: Optional[str] = None,
) -> None:
    """One model call (a try that reached Gemini, whatever came back) -> hits and token-breakdown lines."""
    try:
        trace = current_trace()
        request_id = trace.request_id if trace else None
        hit_id = uuid.uuid4().hex[:12]
        modalities = modalities or {}
        input_tokens = usage.prompt_tokens if usage else None
        output_tokens = usage.output_tokens + usage.thinking_tokens if usage else None
        total_tokens = (usage.total_tokens or input_tokens + output_tokens) if usage else None
        duration = round(time.perf_counter() - started_at, 3)
        input_breakdown = {
            "system_instruction_tokens": None,  # Gemini does not report it separately
            "prompt_tokens": modalities.get("text"),
            "image_tokens": modalities.get("image"),
            "pdf_tokens": modalities.get("document"),
        }
        stamps = log_timestamps()
        hits_logger.info(json.dumps({
            "hit_id": hit_id, **stamps, "request_id": request_id,
            "filename": file, "content_type": content_type, "document_type": DOCUMENT_TYPE,
            "call": call, "attempt": attempt, "model": model, "status": status, "error": error,
            "input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": total_tokens,
            "cached_tokens": cached_tokens, "duration_seconds": duration,
            "estimated_cost_usd": None, "input_token_breakdown": input_breakdown,
        }))
        token_breakdown_logger.info(json.dumps({
            "hit_id": hit_id, **stamps, "request_id": request_id,
            "filename": file, "document_type": DOCUMENT_TYPE, "call": call, **input_breakdown,
            "thinking_output_tokens": usage.thinking_tokens if usage else None,
            "text_output_tokens": usage.output_tokens if usage else None,
            "input_tokens_total": input_tokens, "output_tokens_total": output_tokens,
        }))
        logger.info(
            "[%s] Gemini call file=%s type=%s call=%s attempt=%d status=%s tokens in=%s out=%s total=%s %.2fs",
            request_id or "-", file, DOCUMENT_TYPE, call, attempt, status,
            input_tokens, output_tokens, total_tokens, duration,
        )
        calls = _calls.get()
        if calls is not None:
            calls.append({
                "hit_id": hit_id, "file": file or UNASSIGNED, "call": call, "status": status,
                "input_tokens": input_tokens or 0, "output_tokens": output_tokens or 0,
            })
    except Exception:  # accounting must never fail a request
        logger.warning("Could not log a Gemini call", exc_info=True)


def _totals(rows: list[dict[str, Any]]) -> dict[str, int]:
    input_tokens = sum(r["input_tokens"] for r in rows)
    output_tokens = sum(r["output_tokens"] for r in rows)
    return {"calls": len(rows), "input_tokens": input_tokens, "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens}


def _by_call(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    return {call: _totals([r for r in rows if r["call"] == call]) for call in CALLS}


def usage_summary() -> dict[str, Any]:
    """This request's calls so far, by kind, with the request total."""
    rows = _calls.get() or []
    return {**_by_call(rows), "request_total": _totals(rows)}


def document_usage(trace_record: dict[str, Any]) -> list[dict[str, Any]]:
    """This request's tokens per document (an uploaded file), each with its type. A request's documents
    add up to its total."""
    rows = _calls.get() or []
    entries = {f.get("name") or f.get("source"): f for f in trace_record.get("files", [])}
    names = list(dict.fromkeys([r["file"] for r in rows] + [n for n in entries if n]))
    documents = []
    for name in names:
        mine = [r for r in rows if r["file"] == name]
        entry = entries.get(name, {})
        documents.append({
            "document": name,
            "document_type": DOCUMENT_TYPE,
            "file_status": entry.get("status"),
            "cheques_found": len(entry["cheques"]) if isinstance(entry.get("cheques"), list) else None,
            **_by_call(mine),
            "document_total": _totals(mine),
        })
    return documents


def timing_summary(trace_record: dict[str, Any]) -> dict[str, Any]:
    """This request's time: total seconds, seconds per stage, and per document the seconds of each step."""
    steps = (("load", "load_ms"), ("queue_wait", "queue_wait_ms"), ("model", "model_ms"),
             ("quality_check", "quality_check_ms"), ("extraction", "extraction_ms"))
    documents = []
    for f in trace_record.get("files", []):
        done = {step: {"seconds": round(f[key] / 1000, 2)} for step, key in steps if f.get(key) is not None}
        # `model` and `quality_check` happen inside `extraction`: adding them again would count them twice
        own = sum(done[step]["seconds"] for step in ("load", "queue_wait", "extraction") if step in done)
        documents.append({
            "document": f.get("name") or f.get("source"), "document_type": DOCUMENT_TYPE,
            "status": f.get("status"), "steps": done, "document_seconds": round(own, 2),
        })
    total_ms = trace_record.get("total_ms")
    return {
        "total_seconds": round(total_ms / 1000, 2) if total_ms is not None else None,
        "stages": {name: round(ms / 1000, 2) for name, ms in (trace_record.get("stages_ms") or {}).items()},
        "document_count": len(documents),
        "documents": documents,
    }


def log_request_usage(trace_record: dict[str, Any]) -> dict[str, Any]:
    """Write this request's usage, per-document usage and timing lines; returns the usage summary."""
    try:
        stamps = log_timestamps()
        base = {"request_id": trace_record.get("request_id"), "endpoint": trace_record.get("endpoint"),
                "status_code": trace_record.get("status_code"), "outcome": trace_record.get("outcome")}
        summary = usage_summary()
        counts = trace_record.get("counts") or {}
        usage_logger.info(json.dumps({
            **stamps, **base, "document_type": DOCUMENT_TYPE, "files": counts.get("files"),
            "cheques_found": counts.get("cheques_found"), **summary,
        }))
        documents = document_usage(trace_record)
        for document in documents:
            document_usage_logger.info(json.dumps({**stamps, **base, **document}))
        timing = timing_summary(trace_record)
        timing_logger.info(json.dumps({**stamps, **base, **timing}))
        logger.info("[%s] Usage logged: documents=%d calls=%d tokens in=%d out=%d total=%d",
                    base["request_id"], len(documents), summary["request_total"]["calls"],
                    summary["request_total"]["input_tokens"], summary["request_total"]["output_tokens"],
                    summary["request_total"]["total_tokens"])
        return summary
    except Exception:  # accounting must never fail a request
        logger.warning("Could not log a request's usage", exc_info=True)
        return {}

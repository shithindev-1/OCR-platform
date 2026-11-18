"""Background jobs for the leasing endpoints, so a client can show a progress bar.

`POST .../jobs` starts the same processing as the normal endpoints in the background and answers
at once with a `job_id`. The client then polls `GET .../jobs/{job_id}`, which always answers
`{"jobId", "percentage", "status", "result"}`: Processing with `result` null while it runs, then
Completed with `result` holding the very same response the normal endpoint would have given.

`result` is always that normal response shape - status, form_data, missing, processing_time,
documents - even when the job could not finish: then it is a "failed" response with a plain-English
reason on each document, never a bare error message.

Jobs live in process memory: they vanish on restart, and a finished job is forgotten after
`_JOB_TTL_SECONDS`. The service runs a single worker, so every poll reaches the process that holds
the job.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException

from app.leasing.services import progress

logger = logging.getLogger("uae_ocr.jobs")

_JOB_TTL_SECONDS = 3600
# A job still running after this long is reported once as an ERROR (so it is mailed): a normal job ends in
# a few minutes, and every Gemini call has its own time limit, so one this old is very likely stuck.
_STUCK_AFTER_SECONDS = 600
_NOT_FOUND = "This job was not found or has expired. Please upload the documents again."
# Shown on every document when a job could not finish for a reason on our side.
COULD_NOT_FINISH = "We couldn't finish checking your documents. Please try again in a few minutes."

STATUS_PROCESSING = "Processing"
STATUS_COMPLETED = "Completed"

Work = Callable[[], Awaitable[dict[str, Any]]]
# Builds a "failed" response, in the normal shape, with this message on every document.
FailedResponse = Callable[[str], dict[str, Any]]


@dataclass
class Job:
    job_id: str
    failed_response: FailedResponse
    tracker: progress.ProgressTracker = field(default_factory=progress.ProgressTracker)
    result: dict[str, Any] | None = None
    finished_at: float | None = None
    task: asyncio.Task | None = None


_jobs: dict[str, Job] = {}


def _forget_expired() -> None:
    now = time.monotonic()
    for job_id in [
        job_id for job_id, job in _jobs.items()
        if job.finished_at is not None and now - job.finished_at > _JOB_TTL_SECONDS
    ]:
        del _jobs[job_id]


async def _watch(job: Job) -> None:
    """Reports the job once if it is still running after _STUCK_AFTER_SECONDS; never stops it."""
    await asyncio.sleep(_STUCK_AFTER_SECONDS)
    if job.result is None:
        logger.error("Job still running after %d minutes, it may be stuck job_id=%s percentage=%s",
                     _STUCK_AFTER_SECONDS // 60, job.job_id, job.tracker.percent)


async def _run(job: Job, work: Work) -> None:
    progress.install(job.tracker)
    watcher = asyncio.create_task(_watch(job))
    try:
        job.result = await work()
    except HTTPException as exc:
        # A request problem keeps its own (already plain-English) message; anything on our side
        # gets the general one. Either way the result is the normal response shape.
        message = str(exc.detail) if exc.status_code < 500 else COULD_NOT_FINISH
        # The plain-English reason (the same text the caller gets) - so a 400 can be read from the log alone.
        logger.warning(
            "Job ended with a rejection job_id=%s status_code=%d reason=%s",
            job.job_id, exc.status_code, str(exc.detail)[:300],
        )
        job.result = job.failed_response(message)
    except Exception:
        logger.exception("Job failed unexpectedly job_id=%s", job.job_id)
        job.result = job.failed_response(COULD_NOT_FINISH)
    finally:
        watcher.cancel()
        # 100 on every ending - success or failure - so the bar always completes and the client
        # then reads the outcome, including any error, from the result.
        job.tracker.finish()
        job.finished_at = time.monotonic()
        logger.info("Job finished job_id=%s status=%s", job.job_id, (job.result or {}).get("status"))


def start(work: Work, failed_response: FailedResponse) -> str:
    """Starts `work` in the background and returns its job id. `failed_response` builds the result
    used if the work cannot finish."""
    _forget_expired()
    job = Job(job_id=str(uuid.uuid4()), failed_response=failed_response)
    _jobs[job.job_id] = job
    # The task is kept on the job, so it is not garbage-collected while it runs.
    job.task = asyncio.create_task(_run(job, work))
    logger.info("Job started job_id=%s", job.job_id)
    return job.job_id


def get_status(job_id: str) -> dict[str, Any]:
    """`{"jobId", "percentage", "status", "result"}` - Processing with a null result while running,
    Completed with the full response once finished."""
    _forget_expired()
    job = _jobs.get(job_id)
    if job is None:
        # An ERROR, so it is mailed: the user's job is gone - the service restarted, another replica got the
        # poll, or it finished over an hour ago.
        logger.error("Job results not found (lost after a restart, on another replica, or expired) job_id=%s", job_id)
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    finished = job.result is not None
    return {
        "jobId": job.job_id,
        "percentage": job.tracker.percent,
        "status": STATUS_COMPLETED if finished else STATUS_PROCESSING,
        "result": job.result,
    }

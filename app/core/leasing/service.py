"""The Leasing service, packaged for the platform: routes, middleware, error format, startup work."""

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator

from fastapi import FastAPI

from app.api.v1.routers.leasing import service_router
from app.core.config import PlatformSettings
from app.core.leasing.config import settings
from app.core.leasing.exceptions import API_V1_PREFIX, ERROR_HANDLERS
from app.core.leasing.logging import configure_logging
from app.core.leasing.middleware import OcrSecurityMiddleware
from app.core.service import Service
from app.services.leasing.blob_storage import get_blob_storage
from app.services.leasing.gemini import get_gemini_service
from app.services.leasing.retention import run_retention_sweeps

NAME = "leasing"
logger = logging.getLogger("uae_ocr")


async def _warm_up_connections() -> None:
    """Pays the one-off client creation and DNS/TLS setup at startup instead of on the first
    request. Best effort: a failure is logged and the first request simply connects itself."""
    started_at = time.perf_counter()
    targets = [("gemini", get_gemini_service().warm_up())]
    if storage := get_blob_storage():
        targets.append(("blob", storage.warm_up()))
    results = await asyncio.gather(*(job for _, job in targets), return_exceptions=True)
    for (name, _), result in zip(targets, results):
        if isinstance(result, Exception):
            # An ERROR, so it is mailed: an early sign Gemini or blob storage is not reachable from this server.
            logger.error("Warm-up failed target=%s error=%s", name, type(result).__name__)
    logger.info(
        "Warm-up finished targets=%s duration_sec=%.2f",
        [name for name, _ in targets], time.perf_counter() - started_at,
    )


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # In the background, so the service accepts requests immediately rather than after the warm-up.
    warm_up = asyncio.create_task(_warm_up_connections())
    sweeper = asyncio.create_task(run_retention_sweeps(settings)) if settings.output_retention_hours > 0 else None
    try:
        yield
    finally:
        for task in (warm_up, sweeper):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task


def _owns_path(path: str) -> bool:
    # Leasing's routes are /api/v1/ocr and /api/v1/ocr/...; the legacy unprefixed /ocr 404s with a hint.
    return (
        path.startswith(f"{API_V1_PREFIX}/ocr") or path == "/ocr" or path.startswith("/ocr/")
        or path == "/admin/logs" or path.startswith("/admin/logs/")
    )


def build_service(platform: PlatformSettings) -> Service:
    return Service(
        name=NAME,
        router=service_router,
        owns_path=_owns_path,
        error_handlers=ERROR_HANDLERS,
        default_error_service=True,
        configure_logging=lambda shared: configure_logging(settings, platform.log_directory / NAME, shared=shared),
        add_middleware=lambda app: app.add_middleware(OcrSecurityMiddleware),
        lifespan=lifespan,
    )

"""The Cheque service, packaged for the platform: routes, request tracing, error format, logging."""

import logging

from fastapi import FastAPI, Request

from app.api.cheque.errors import ERROR_HANDLERS
from app.api.v1.routers.cheque import API_V1_PREFIX, api_router
from app.core.cheque import usage
from app.core.cheque.config import Settings, get_settings
from app.core.cheque.logging import log_result, setup_logging
from app.core.cheque.tracing import RequestTrace
from app.core.config import PlatformSettings
from app.core.service import Service

NAME = "cheque"
TRACED_PREFIX = f"{API_V1_PREFIX}/cheques"
request_logger = logging.getLogger("cheque.request")


def _trace_requests(app: FastAPI, settings: Settings) -> None:
    """One trace per cheque API hit: written once at the end, id returned as X-Request-ID.

    Besides the full result line, a request writes its usage (tokens per call, per document and in
    total) and its timing lines; see app/core/cheque/usage.py."""
    app_info = {"version": settings.app_version, "env": settings.app_env,
                "model": settings.gemini_model}

    @app.middleware("http")
    async def trace(request: Request, call_next):
        if not request.url.path.startswith(TRACED_PREFIX):
            return await call_next(request)
        trace = RequestTrace(request.method, request.url.path,
                             request.client.host if request.client else None,
                             request.headers.get("user-agent"), app_info)
        token = trace.activate()
        usage_token = usage.start_request()
        request_logger.info("[%s] START %s %s client=%s", trace.request_id, request.method,
                            request.url.path, request.client.host if request.client else "-")
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-ID"] = trace.request_id
            return response
        except Exception as exc:  # unhandled: the 500 handler runs outside this trace
            trace.error(type(exc).__name__, str(exc)[:500])
            raise
        finally:
            record = trace.finish(status)
            log_result(record)
            usage.log_request_usage(record)
            request_logger.info("[%s] SUMMARY %s", trace.request_id, trace.summary_line())
            usage.end_request(usage_token)
            trace.deactivate(token)


def build_service(platform: PlatformSettings) -> Service:
    settings = get_settings()
    return Service(
        name=NAME,
        router=api_router,
        owns_path=lambda path: path.startswith(TRACED_PREFIX),
        error_handlers=ERROR_HANDLERS,
        configure_logging=lambda shared: setup_logging(
            platform.log_level, str(platform.log_directory / NAME), shared=shared,
            console=settings.log_to_console,
        ),
        add_middleware=lambda app: _trace_requests(app, settings),
    )

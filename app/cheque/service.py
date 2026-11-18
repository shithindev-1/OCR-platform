"""The Cheque service, packaged for the platform: routes, request tracing, error format, logging."""

import logging

from fastapi import FastAPI, Request

from app.cheque.api.errors import ERROR_HANDLERS
from app.cheque.api.v1.router import API_V1_PREFIX, api_router
from app.cheque.core.config import Settings, get_settings
from app.cheque.core.logging import log_result, setup_logging
from app.cheque.core.tracing import RequestTrace
from app.common.config import PlatformSettings
from app.common.service import Service

NAME = "cheque"
TRACED_PREFIX = f"{API_V1_PREFIX}/cheques"


def _trace_requests(app: FastAPI, settings: Settings) -> None:
    """One trace per cheque API hit: written once at the end, id returned as X-Request-ID."""
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
            log_result(trace.finish(status))
            logging.getLogger("app.request").info("[%s] SUMMARY %s", trace.request_id,
                                                  trace.summary_line())
            trace.deactivate(token)


def build_service(platform: PlatformSettings) -> Service:
    settings = get_settings()
    return Service(
        name=NAME,
        router=api_router,
        owns_path=lambda path: path.startswith(TRACED_PREFIX),
        error_handlers=ERROR_HANDLERS,
        configure_logging=lambda: setup_logging(platform.log_level, str(platform.log_directory / NAME)),
        add_middleware=lambda app: _trace_requests(app, settings),
    )

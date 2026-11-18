"""One set of app-wide exception handlers that answers each service in its own error format.

Leasing answers `{"detail": ...}` (and its own `errorInfo` shape on /ocr); Cheque answers
`{"error": ...}`. Those are separate API contracts, so each service keeps its own handlers and this
module only decides, by the URL, whose handler runs. Paths nobody owns (an unknown URL, a wrong
method) go to the service marked as the default, else to a plain FastAPI-style answer.
"""

import logging
from collections.abc import Sequence

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.common.service import ErrorHandler, Service

logger = logging.getLogger("app")


async def _generic_unhandled(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error method=%s path=%s error_type=%s", request.method, request.url.path, type(exc).__name__)
    return JSONResponse(status_code=500, content={"detail": "Something went wrong on our side. Please try again in a few minutes."})


_GENERIC: dict[type, ErrorHandler] = {
    StarletteHTTPException: http_exception_handler,
    RequestValidationError: request_validation_exception_handler,
    Exception: _generic_unhandled,
}


def _dispatcher(exc_type: type, services: Sequence[Service]) -> ErrorHandler:
    default = next((s for s in services if s.default_error_service and exc_type in s.error_handlers), None)

    async def handle(request: Request, exc: Exception):
        path = request.url.path
        for service in services:
            if exc_type in service.error_handlers and service.owns_path(path):
                return await service.error_handlers[exc_type](request, exc)
        if default is not None:
            return await default.error_handlers[exc_type](request, exc)
        return await _GENERIC.get(exc_type, _generic_unhandled)(request, exc)

    return handle


def register_exception_handlers(app: FastAPI, services: Sequence[Service]) -> None:
    exc_types = {exc_type for service in services for exc_type in service.error_handlers} | set(_GENERIC)
    for exc_type in exc_types:
        app.add_exception_handler(exc_type, _dispatcher(exc_type, services))

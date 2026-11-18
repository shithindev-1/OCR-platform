"""What a service hands the platform so it can be mounted into the one FastAPI app.

Each service (`app/core/leasing/service.py`, `app/core/cheque/service.py`) builds one of these.
The platform (`app/main.py`) mounts whichever services are switched on, and nothing else about a
service leaks into the app - so a single process can run both, or either alone.
"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, FastAPI, Request

ErrorHandler = Callable[[Request, Any], Any]


@dataclass
class Service:
    name: str
    # Complete router (its own /api/v1 prefix and its own X-API-Key dependency).
    router: APIRouter
    # True for the paths that belong to this service, so errors are answered in its own format.
    owns_path: Callable[[str], bool]
    # Exception class -> handler, answering in this service's error format.
    error_handlers: dict[type, ErrorHandler] = field(default_factory=dict)
    # True for the service that also answers paths nobody owns (unknown URL, wrong method).
    default_error_service: bool = False
    # Called once at startup, before the app is built. The argument is True when another service shares
    # this process: the service then keeps its log records to itself (see app/core/logging.py).
    configure_logging: Callable[[bool], None] = lambda shared: None
    # Adds this service's middleware (each one only acts on its own paths).
    add_middleware: Callable[[FastAPI], None] = lambda app: None
    # Startup / shutdown work (warm-up connections, background sweeps).
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None


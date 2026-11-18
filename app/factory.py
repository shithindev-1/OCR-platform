"""Builds the one FastAPI app that hosts the Leasing and/or Cheque service (entry points: main.py)."""

import contextlib
import importlib
from collections.abc import AsyncIterator, Sequence

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.common.config import PlatformSettings, get_platform_settings
from app.common.errors import register_exception_handlers
from app.common.health import build_health_routers
from app.common.service import Service
from app.common.swagger import enable_swagger_file_picker

API_V1_PREFIX = "/api/v1"

_DESCRIPTIONS = {
    "leasing": "**Leasing OCR** - identity and company documents (`/api/v1/ocr`, `/api/v1/ocr/leasing`).",
    "cheque": "**Cheque OCR** - cheque extraction and matching to rent payments (`/api/v1/cheques`).",
}


def load_service(name: str, platform: PlatformSettings) -> Service:
    # Imported only when switched on, so a service that is off never loads its settings or models.
    return importlib.import_module(f"app.{name}.service").build_service(platform)


def build_app(services: Sequence[str] | None = None) -> FastAPI:
    platform = get_platform_settings()
    names = list(dict.fromkeys(services)) if services else platform.service_list
    loaded = [load_service(name, platform) for name in names]
    for service in loaded:
        service.configure_logging()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with contextlib.AsyncExitStack() as stack:
            for service in loaded:
                if service.lifespan:
                    await stack.enter_async_context(service.lifespan(app))
            yield

    docs = platform.docs_enabled
    application = FastAPI(
        title="OCR Platform API",
        description="\n\n".join(_DESCRIPTIONS[name] for name in names),
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    for service in loaded:
        service.add_middleware(application)
    if platform.cors_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=platform.cors_origins,
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
            expose_headers=["X-Request-ID"],
        )
    register_exception_handlers(application, loaded)
    for router in build_health_routers(__version__, names, API_V1_PREFIX):
        application.include_router(router)
    for service in loaded:
        application.include_router(service.router)
    enable_swagger_file_picker(application)
    return application




"""Liveness checks, one for the whole project.

`GET /health` stays at the root and unauthenticated, exactly `{"status": "ok"}`, so load balancers and
the service monitor can probe it. `GET /api/v1/health` is the same check with the version and the
services this process is running (what the Cheque service has always exposed there).
"""

from fastapi import APIRouter


def build_health_routers(version: str, services: list[str], api_prefix: str) -> list[APIRouter]:
    root = APIRouter(tags=["Health"])
    versioned = APIRouter(prefix=api_prefix, tags=["Health"])

    @root.get("/health", summary="Liveness check")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @versioned.get("/health", summary="Liveness check with version and running services")
    def health_details() -> dict:
        # Deliberately no model or configuration details.
        return {"status": "ok", "version": version, "services": services}

    return [root, versioned]

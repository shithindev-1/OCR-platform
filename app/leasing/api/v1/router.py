"""Every route the service exposes, collected into one router.

Everything lives under `/api/v1` behind the X-API-Key check. The `/health` probe is shared with the
other service (app/common/health.py).
"""

from fastapi import APIRouter, Depends

from app.leasing.api.v1.endpoints import admin_logs, leasing, leasing_detect, ocr
from app.leasing.core.exceptions import API_V1_PREFIX
from app.leasing.core.security import require_api_key

v1_router = APIRouter(prefix=API_V1_PREFIX, dependencies=[Depends(require_api_key)])
v1_router.include_router(ocr.router)
v1_router.include_router(leasing.router)
v1_router.include_router(leasing_detect.router)

# What the platform mounts for Leasing: the /api/v1 routes, plus /admin/logs (its own X-Admin-Key,
# ADMIN_API_KEYS - not the callers' API key; answers 404 while that is unset).
service_router = APIRouter()
service_router.include_router(v1_router)
service_router.include_router(admin_logs.router)

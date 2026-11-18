from fastapi import APIRouter, Depends

from app.cheque.api.security import require_api_key
from app.cheque.api.v1.endpoints import extraction, matching

API_V1_PREFIX = "/api/v1"

api_router = APIRouter(prefix=API_V1_PREFIX)
api_router.include_router(extraction.router, dependencies=[Depends(require_api_key)])
api_router.include_router(matching.router, dependencies=[Depends(require_api_key)])

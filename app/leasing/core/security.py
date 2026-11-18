"""X-API-Key authentication for every /api/v1 Leasing route."""

import logging

from fastapi import HTTPException, Security

from app.common.security import api_key_header, api_key_valid
from app.leasing.core.config import settings

logger = logging.getLogger("uae_ocr")


async def require_api_key(api_key: str | None = Security(api_key_header)) -> None:
    """Reject the request unless X-API-Key matches one of `API_KEYS`.

    With `API_KEYS` empty, authentication is off - for local testing only.
    """
    keys = settings.api_key_values
    if not keys:
        return
    if api_key_valid(api_key, keys):
        return
    # An ERROR, so it is mailed: a wrong or rotated key on the caller's side blocks every user.
    logger.error("Request rejected: API key missing or not valid reason=invalid_api_key")
    raise HTTPException(status_code=401, detail="Your API key is missing or not valid. Please check the X-API-Key header.", headers={"WWW-Authenticate": "APIKey"})

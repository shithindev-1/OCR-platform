"""X-API-Key authentication for client requests."""

import logging
from typing import Optional

from fastapi import Depends, Request, Security

from app.cheque.core.config import Settings, get_settings
from app.cheque.core.exceptions import AuthenticationError
from app.cheque.core.tracing import current_trace, key_fingerprint
from app.common.security import API_KEY_HEADER, api_key_header, api_key_valid  # noqa: F401

logger = logging.getLogger(__name__)


async def require_api_key(
    request: Request,
    api_key: Optional[str] = Security(api_key_header),
    settings: Settings = Depends(get_settings),
) -> None:
    """Reject the request unless X-API-Key matches a configured key (disabled if none set)."""
    if not settings.auth_enabled:
        return
    if api_key_valid(api_key, settings.api_key_values):
        trace = current_trace()
        if trace:  # which client / key was used, never the key itself
            trace.record["client"]["api_key_id"] = key_fingerprint(api_key)
        return
    client = request.client.host if request.client else "unknown"
    logger.warning("Rejected %s %s from %s: %s API key", request.method, request.url.path,
                   client, "invalid" if api_key else "missing")
    raise AuthenticationError()

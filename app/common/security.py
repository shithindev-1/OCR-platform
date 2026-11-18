"""X-API-Key authentication, shared by both services.

One header scheme (so Swagger shows a single "Authorize" box for the whole API) and one
constant-time key check. What a rejection looks like is each service's own contract, so each
service keeps its own tiny dependency that calls `api_key_valid` and raises its own error.
"""

import hmac
from collections.abc import Iterable

from fastapi.security import APIKeyHeader

API_KEY_HEADER = "X-API-Key"

# auto_error=False: a missing key goes through the service's own error handler, so the response
# keeps that service's error shape.
api_key_header = APIKeyHeader(name=API_KEY_HEADER, auto_error=False, description="API key issued for this service")


def api_key_valid(provided: str | None, keys: Iterable[str]) -> bool:
    """True when `provided` equals one of `keys`. Compares against every key, so response timing
    reveals nothing about which one is close."""
    if not provided:
        return False
    matched = False
    for key in keys:
        matched |= hmac.compare_digest(provided.encode(), key.encode())
    return matched

"""Azure Blob Storage access for the /ocr/leasing endpoints: fetch source files, upload results.

Configured by AZURE_STORAGE_CONNECTION_STRING and AZURE_STORAGE_CONTAINER. Connecting, downloading,
uploading and warming up are the shared ones in app/common/blob.py; this module adds what is
Leasing's own - a source is a path inside the container or a full URL into it. Neither the connection
string nor any signed URL is ever logged or returned to a caller.
"""

from functools import lru_cache
from urllib.parse import quote, unquote, urlparse

from app.common.blob import BlobContainer, BlobError
from app.leasing.core.config import settings

# The name Leasing's routes and tests have always used for a blob that could not be read or written.
BlobStorageError = BlobError


class BlobStorage(BlobContainer):
    def __init__(self, connection_string: str, container: str) -> None:
        super().__init__(connection_string, container)
        # e.g. https://<account>.blob.core.windows.net/<container> - no credentials in it.
        self.container_url = self._container.url.rstrip("/")

    def blob_path(self, source: str) -> str | None:
        """The blob path `source` names inside this container, or None if it points elsewhere.

        Accepts either a path relative to the container ("ApplicationFiles/.../Passport.jpg") or a
        full URL into this same container, with or without a query string.
        """
        if source.lower().startswith(("http://", "https://")):
            parsed = urlparse(source)
            base = urlparse(self.container_url)
            prefix = base.path.rstrip("/") + "/"
            if parsed.netloc.lower() != base.netloc.lower() or not parsed.path.startswith(prefix):
                return None
            path = unquote(parsed.path[len(prefix):])
        else:
            path = source.strip().lstrip("/")
        return path or None

    def url(self, path: str) -> str:
        """The blob's full URL, e.g. https://<account>.blob.core.windows.net/<container>/<path>.
        Carries no credentials, so it is safe to return to a caller."""
        return f"{self.container_url}/{quote(path, safe='/')}"


@lru_cache
def get_blob_storage() -> BlobStorage | None:
    """The shared client, or None when blob storage is not configured."""
    if not settings.azure_storage_connection_string or not settings.azure_storage_container:
        return None
    return BlobStorage(settings.azure_storage_connection_string.get_secret_value(), settings.azure_storage_container)

"""Azure Blob Storage core shared by both services: connect, download, upload, warm up.

The SDK client is synchronous, so every call runs in a worker thread rather than blocking the event
loop. Each service keeps only what is specific to it (Leasing: container-relative paths and URLs;
Cheque: a base URL with an optional prefix, SAS links, server-side copy, append blobs) in a small
subclass, and maps `BlobError` onto its own error type.
"""

import asyncio
import logging

from azure.core.exceptions import AzureError, ResourceNotFoundError
from azure.storage.blob import BlobServiceClient, ContentSettings

logger = logging.getLogger("app.blob")


class BlobError(Exception):
    """A blob could not be read or written. The message is safe to show to a caller."""

    def __init__(self, message: str, *, not_found: bool = False, too_large: bool = False) -> None:
        super().__init__(message)
        self.not_found = not_found
        self.too_large = too_large


def _reason(exc: AzureError) -> str:
    # The SDK's message carries RequestId/Time/ErrorCode lines after the human-readable first one.
    return (str(exc).strip().splitlines() or [type(exc).__name__])[0]


class BlobContainer:
    def __init__(self, connection_string: str, container: str) -> None:
        self.service = BlobServiceClient.from_connection_string(connection_string)
        self.container_name = container
        self._container = self.service.get_container_client(container)

    async def download(self, name: str, *, max_bytes: int) -> tuple[bytes, str | None]:
        """Returns (content, content type as stored on the blob)."""

        def _run() -> tuple[bytes, str | None]:
            downloader = self._container.download_blob(name)
            if downloader.size > max_bytes:
                raise BlobError("exceeds the configured size limit", too_large=True)
            return downloader.readall(), downloader.properties.content_settings.content_type

        try:
            return await asyncio.to_thread(_run)
        except AzureError as exc:
            logger.warning("Blob download failed path=%s error=%s", name, type(exc).__name__)
            raise BlobError(_reason(exc), not_found=isinstance(exc, ResourceNotFoundError)) from exc

    async def upload(self, name: str, content: bytes, content_type: str) -> None:
        """Writes `content` to `name`, replacing any blob already there (a re-run of the request)."""

        def _run() -> None:
            self._container.upload_blob(
                name, content, overwrite=True, content_settings=ContentSettings(content_type=content_type),
            )

        try:
            await asyncio.to_thread(_run)
        except AzureError as exc:
            logger.warning("Blob upload failed path=%s error=%s", name, type(exc).__name__)
            raise BlobError(_reason(exc)) from exc
        logger.info("Blob uploaded path=%s bytes=%d content_type=%s", name, len(content), content_type)

    async def warm_up(self) -> None:
        """Opens the pooled connection to the storage account before the first request needs it.
        Reads the container's properties only - no blob is listed or read."""
        await asyncio.to_thread(self._container.get_container_properties)

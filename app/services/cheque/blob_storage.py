"""Azure Blob Storage access for cheque files under AZURE_BLOB_BASE_URL.

Connecting, downloading and uploading are the shared ones in app/services/blob.py; this module
adds what is Cheque's own - a base URL with an optional prefix, SAS links, server-side copy and append
blobs. The connection string comes from the environment only. Only URLs inside the configured container
(the base URL) are read or written with it.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote, unquote, urlparse

from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError
from azure.storage.blob import BlobSasPermissions, ContentSettings, generate_blob_sas

from app.core.cheque.config import Settings
from app.core.cheque.exceptions import FileDownloadError, FileValidationError
from app.services.blob import BlobContainer, BlobError


class BlobStorage(BlobContainer):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        base = urlparse(settings.azure_blob_base_url.rstrip("/") + "/")
        self._host = (base.hostname or "").lower()
        parts = unquote(base.path).strip("/").split("/", 1)
        self.container = parts[0]
        self._prefix = parts[1] + "/" if len(parts) > 1 and parts[1] else ""
        super().__init__(settings.azure_storage_connection_string.get_secret_value(), self.container)
        self._service = self.service

    # ── URL <-> blob name ──

    def blob_name(self, url: str) -> Optional[str]:
        """Blob name if `url` is inside the configured container (and prefix), else None."""
        parsed = urlparse(url)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() != self._host:
            return None
        path = unquote(parsed.path).lstrip("/")
        container, _, name = path.partition("/")
        if container != self.container or not name or not name.startswith(self._prefix):
            return None
        return name

    def url_for(self, blob_name: str) -> str:
        """Public URL of a blob, with a read-only SAS token if AZURE_SAS_EXPIRY_HOURS > 0."""
        hours = self.settings.azure_sas_expiry_hours
        return self._read_url(blob_name, timedelta(hours=hours)) if hours else self._plain_url(blob_name)

    def _plain_url(self, blob_name: str) -> str:
        return f"https://{self._host}/{self.container}/{quote(blob_name)}"

    def _read_url(self, blob_name: str, valid_for: timedelta) -> str:
        token = generate_blob_sas(
            account_name=self._service.account_name,
            container_name=self.container,
            blob_name=blob_name,
            account_key=self._service.credential.account_key,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.now(timezone.utc) + valid_for,
        )
        return f"{self._plain_url(blob_name)}?{token}"

    # ── I/O (sync SDK run in a worker thread) ──

    async def download(self, blob_name: str) -> tuple[bytes, Optional[str]]:
        try:
            return await super().download(blob_name, max_bytes=self.settings.max_file_size_bytes)
        except BlobError as exc:
            if exc.too_large:
                raise FileValidationError(
                    f"The file exceeds the {self.settings.max_file_size_mb} MB limit."
                ) from exc
            raise FileDownloadError() from exc

    async def upload(self, blob_name: str, data: bytes, content_type: str) -> str:
        await super().upload(blob_name, data, content_type)
        return self.url_for(blob_name)

    async def copy(self, source_blob: str, blob_name: str) -> str:
        """Server-side copy inside the container: nothing is downloaded."""
        await asyncio.to_thread(self._copy, source_blob, blob_name)
        return self.url_for(blob_name)

    async def create_if_missing(self, blob_name: str, data: bytes, content_type: str) -> bool:
        """Upload only if the blob does not exist yet; False when it already exists."""
        return await asyncio.to_thread(self._create_if_missing, blob_name, data, content_type)

    async def append(self, blob_name: str, data: bytes, content_type: str) -> None:
        """Add data to the end of an append blob, creating it first if needed."""
        await asyncio.to_thread(self._append, blob_name, data, content_type)

    def _copy(self, source_blob: str, blob_name: str) -> None:
        client = self._service.get_blob_client(self.container, blob_name)
        client.start_copy_from_url(self._read_url(source_blob, timedelta(minutes=15)),
                                   requires_sync=True)

    def _create_if_missing(self, blob_name: str, data: bytes, content_type: str) -> bool:
        client = self._service.get_blob_client(self.container, blob_name)
        try:
            client.upload_blob(data, overwrite=False,
                               content_settings=ContentSettings(content_type=content_type))
            return True
        except ResourceExistsError:
            return False

    def _append(self, blob_name: str, data: bytes, content_type: str) -> None:
        client = self._service.get_blob_client(self.container, blob_name)
        try:
            client.append_block(data)
            return
        except ResourceNotFoundError:
            pass
        try:  # IfMissing: two requests creating the same file must not wipe each other's lines
            client.create_append_blob(content_settings=ContentSettings(content_type=content_type),
                                      match_condition=MatchConditions.IfMissing)
        except (ResourceExistsError, ResourceModifiedError):  # created meanwhile
            pass
        client.append_block(data)


_clients: dict[int, BlobStorage] = {}


def get_blob_storage(settings: Settings) -> Optional[BlobStorage]:
    """Shared client per Settings instance, or None when blob storage is not configured."""
    if not settings.blob_storage_enabled:
        return None
    if id(settings) not in _clients:
        _clients[id(settings)] = BlobStorage(settings)
    return _clients[id(settings)]

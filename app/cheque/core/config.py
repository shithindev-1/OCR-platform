"""Cheque service settings: the shared base (key, model, auth, Blob) plus everything Cheque-specific.

Sources, highest priority first (see app/common/config.py for the shared machinery):
    1. environment variables - CHEQUE_<NAME> for this service only, <NAME> for every service
    2. .env                  secrets only (API keys, connection strings) + APP_ENV
    3. config/settings.<APP_ENV>.json  per-environment values (not committed)
    4. config/settings.json            shared defaults (committed)
    5. defaults in this class

Secrets and the model name have no defaults, so nothing sensitive lives in the code.
"""

from functools import lru_cache
from typing import Literal, Optional

from pydantic import Field, field_validator

from app.common.config import ServiceSettings


class Settings(ServiceSettings):
    service_name = "cheque"

    # ── App ──
    app_name: str = "Paperless Cheque OCR API"
    app_version: str = "1.0.0"

    # ── OCR model: sampling ──
    gemini_temperature: float = Field(0.0, ge=0.0, le=2.0)
    gemini_top_p: Optional[float] = Field(0.95, ge=0.0, le=1.0)
    gemini_top_k: Optional[int] = Field(40, ge=1)
    gemini_max_output_tokens: int = Field(4096, ge=1)
    gemini_seed: Optional[int] = None

    # ── OCR model: thinking ──
    gemini_thinking_budget: Optional[int] = Field(0, ge=-1)
    gemini_thinking_level: Optional[Literal["low", "high"]] = None

    # ── OCR model: reliability ──
    gemini_max_retries: int = Field(2, ge=0)

    # ── Uploads ──
    max_files_per_request: int = Field(20, ge=1)
    max_concurrent_requests: int = Field(5, ge=1)
    allowed_mime_types: frozenset[str] = frozenset({
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/heic",
        "image/heif",
        "application/pdf",
    })

    # ── File URLs (/cheques/match) ──
    # Hosts the API may download from; ".x.com" matches subdomains, [] allows any host
    allowed_file_hosts: list[str] = [".blob.core.windows.net"]
    download_timeout_seconds: int = Field(60, ge=1)
    # Local testing only: plain file names inside this folder. Ignored unless APP_ENV=local
    local_files_dir: Optional[str] = None

    # ── Azure Blob Storage (connection string from .env; base URL from settings.<env>.json) ──
    # URLs under AZURE_BLOB_BASE_URL are read with the connection string, and
    # processed (cropped / rotated) cheques are saved next to the original.
    azure_blob_base_url: Optional[str] = None
    # Hours a read-only SAS token on returned URLs stays valid; 0 = plain URL (public container)
    azure_sas_expiry_hours: int = Field(0, ge=0)

    # ── Cheque image processing (/cheques/match) ──
    image_processing_enabled: bool = True
    processed_file_suffix: str = "_ocr"
    # Folder (inside the container / local_files_dir) for processed files: <folder>/<request id>/<name>.
    # Empty = save next to the original file.
    processed_folder: Optional[str] = None
    # Below this sharpness (Laplacian variance) a page is treated as unreadable; 0 = off
    min_image_sharpness: float = Field(25.0, ge=0.0)
    crop_padding_percent: float = Field(2.0, ge=0.0, le=20.0)
    pdf_render_dpi: int = Field(200, ge=72, le=600)
    processed_jpeg_quality: int = Field(92, ge=50, le=100)

    # ── Case study (/cheques/match, /cheques/match/upload) ──
    # After the response, files behind failed matches are copied to <folder>/<request id>/
    # (inside the container / local_files_dir) with what the OCR read. Empty folder = off.
    case_study_enabled: bool = True
    case_study_folder: Optional[str] = None

    @field_validator(
        "gemini_top_p", "gemini_top_k", "gemini_seed",
        "gemini_thinking_budget", "gemini_thinking_level", "local_files_dir",
        "azure_blob_base_url",
        mode="before",
    )
    @classmethod
    def _blank_to_none(cls, v):  # not _empty_to_none: that name is the base class's validator
        """Treat empty values in .env (e.g. `GEMINI_SEED=`) as unset."""
        return None if v == "" else v

    @property
    def local_files_enabled(self) -> bool:
        return self.app_env == "local" and bool(self.local_files_dir)

    @property
    def blob_storage_enabled(self) -> bool:
        return bool(self.azure_storage_connection_string and self.azure_blob_base_url)


@lru_cache
def get_settings() -> Settings:
    return Settings()

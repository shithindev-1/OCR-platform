"""The pieces both services now share: config sources, API-key check, PDF helpers, Blob core."""

import json
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from azure.core.exceptions import ResourceNotFoundError
from pydantic import ValidationError

from app.cheque.core.config import Settings as ChequeSettings
from app.common.blob import BlobContainer, BlobError
from app.common.config import PlatformSettings
from app.common.pdf import image_to_pdf, render_pdf_page, render_pdf_pages
from app.common.security import api_key_valid
from app.leasing.core.config import Settings as LeasingSettings

CONNECTION = "DefaultEndpointsProtocol=https;AccountName=acct;AccountKey=a2V5;EndpointSuffix=core.windows.net"


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """No developer .env, config file or environment value changes what these tests read."""
    monkeypatch.setenv("APP_CONFIG_DIR", str(tmp_path))
    for name in ("APP_ENV", "API_KEYS", "GEMINI_API_KEY", "GEMINI_MODEL", "LEASING_GEMINI_MODEL",
                 "CHEQUE_GEMINI_MODEL", "LEASING_GEMINI_API_KEY", "CHEQUE_GEMINI_API_KEY",
                 "MAX_FILE_SIZE_MB", "SERVICES", "DOCS_ENABLED", "AZURE_STORAGE_CONNECTION_STRING"):
        monkeypatch.delenv(name, raising=False)


def _leasing(**kwargs) -> LeasingSettings:
    return LeasingSettings(_env_file=None, **kwargs)


def _cheque(**kwargs) -> ChequeSettings:
    return ChequeSettings(_env_file=None, **kwargs)


def _write(directory, name, data) -> None:
    (directory / name).write_text(json.dumps(data), encoding="utf-8")


# --- one config, a prefix to set a value for one service ---------------------------------------------

def test_a_plain_value_configures_both_services(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "shared-key")
    monkeypatch.setenv("GEMINI_MODEL", "shared-model")
    for settings in (_leasing(), _cheque()):
        assert settings.gemini_api_key.get_secret_value() == "shared-key"
        assert settings.gemini_model == "shared-model"


def test_a_prefixed_value_configures_one_service_only(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "shared-key")
    monkeypatch.setenv("GEMINI_MODEL", "shared-model")
    monkeypatch.setenv("CHEQUE_GEMINI_MODEL", "cheque-model")
    monkeypatch.setenv("LEASING_GEMINI_API_KEY", "leasing-key")
    leasing, cheque = _leasing(), _cheque()
    assert (leasing.gemini_model, cheque.gemini_model) == ("shared-model", "cheque-model")
    assert leasing.gemini_api_key.get_secret_value() == "leasing-key"
    assert cheque.gemini_api_key.get_secret_value() == "shared-key"


def test_a_prefixed_value_in_the_env_file_works_too(monkeypatch, tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("GEMINI_API_KEY=k\nGEMINI_MODEL=plain\nLEASING_GEMINI_MODEL=for-leasing\n", encoding="utf-8")
    leasing = LeasingSettings(_env_file=env_file)
    cheque = ChequeSettings(_env_file=env_file)
    assert (leasing.gemini_model, cheque.gemini_model) == ("for-leasing", "plain")


def test_each_service_keeps_its_own_default() -> None:
    leasing, cheque = _leasing(gemini_api_key="k", gemini_model="m"), _cheque(gemini_api_key="k", gemini_model="m")
    assert (leasing.gemini_timeout_seconds, cheque.gemini_timeout_seconds) == (105, 120)
    assert (leasing.gemini_top_k, cheque.gemini_top_k) == (1, 40)


def test_config_file_has_a_section_per_service(tmp_path) -> None:
    _write(tmp_path, "settings.json", {
        "common": {"docs_enabled": False, "log_level": "DEBUG"},
        "leasing": {"gemini_model": "leasing-from-file", "detection_enabled": False},
        "cheque": {"ocr_model": {"gemini_model": "cheque-from-file"}, "uploads": {"max_files_per_request": 3}},
    })
    leasing, cheque = _leasing(gemini_api_key="k"), _cheque(gemini_api_key="k")
    assert leasing.gemini_model == "leasing-from-file" and leasing.detection_enabled is False
    assert cheque.gemini_model == "cheque-from-file" and cheque.max_files_per_request == 3
    platform = PlatformSettings(_env_file=None)
    assert platform.docs_enabled is False and platform.log_level == "DEBUG"


def test_the_environment_beats_the_config_file(monkeypatch, tmp_path) -> None:
    _write(tmp_path, "settings.json", {"leasing": {"gemini_model": "from-file"}})
    monkeypatch.setenv("GEMINI_MODEL", "from-env")
    assert _leasing(gemini_api_key="k").gemini_model == "from-env"


def test_another_services_key_is_an_error_in_this_services_section(tmp_path) -> None:
    _write(tmp_path, "settings.json", {"leasing": {"max_files_per_request": 5}})  # a Cheque setting
    with pytest.raises(ValueError, match="max_files_per_request"):
        _leasing(gemini_api_key="k", gemini_model="m")


def test_secrets_are_refused_in_a_config_file(tmp_path) -> None:
    _write(tmp_path, "settings.json", {"common": {"gemini_api_key": "leak"}})
    with pytest.raises(ValueError, match="not allowed"):
        _cheque(gemini_model="m")


def test_api_keys_are_required_outside_local_for_both_services() -> None:
    for build in (_leasing, _cheque):
        with pytest.raises(ValidationError):
            build(gemini_api_key="k", gemini_model="m", app_env="uat")
        assert build(gemini_api_key="k", gemini_model="m", app_env="uat", api_keys="a").auth_enabled


def test_api_keys_may_be_a_list_or_comma_separated() -> None:
    assert _leasing(gemini_api_key="k", gemini_model="m", api_keys="a, b,").api_key_values == ["a", "b"]
    assert _cheque(gemini_api_key="k", gemini_model="m", api_keys='["x","y"]').api_key_values == ["x", "y"]


def test_keys_never_show_in_a_settings_repr() -> None:
    settings = _leasing(gemini_api_key="super-secret", gemini_model="m", api_keys="also-secret")
    assert "super-secret" not in repr(settings) and "also-secret" not in repr(settings)


def test_platform_settings_pick_the_services(monkeypatch) -> None:
    assert PlatformSettings(_env_file=None).service_list == ["leasing", "cheque"]
    monkeypatch.setenv("SERVICES", "Cheque")
    assert PlatformSettings(_env_file=None).service_list == ["cheque"]
    monkeypatch.setenv("SERVICES", "leasing,payroll")
    with pytest.raises(ValidationError):
        PlatformSettings(_env_file=None)


# --- API key check ---------------------------------------------------------------------------------

@pytest.mark.parametrize("provided, keys, expected", [
    ("a", ["a", "b"], True), ("b", ["a", "b"], True), ("c", ["a", "b"], False),
    (None, ["a"], False), ("", ["a"], False), ("a", [], False), ("ä", ["a"], False),
])
def test_api_key_check(provided, keys, expected) -> None:
    assert api_key_valid(provided, keys) is expected


# --- PDF helpers used by both services -------------------------------------------------------------

def _image(height: int = 120, width: int = 300) -> np.ndarray:
    image = np.full((height, width, 3), 255, np.uint8)
    cv2.rectangle(image, (20, 20), (width - 20, height - 20), (0, 0, 200), 6)
    return image


def test_image_wrapped_in_a_pdf_renders_back_at_the_same_size() -> None:
    pdf = image_to_pdf(_image(), dpi=100, quality=90)
    assert pdf.startswith(b"%PDF")
    page = render_pdf_page(pdf, 1, 100)
    assert abs(page.shape[0] - 120) <= 2 and abs(page.shape[1] - 300) <= 2
    assert page[60, 22].tolist() != [255, 255, 255]  # the border survived (BGR, red-ish)


def test_a_page_number_outside_the_document_gives_its_nearest_page() -> None:
    pdf = image_to_pdf(_image(), dpi=72, quality=90)
    assert render_pdf_page(pdf, 99, 72).shape == render_pdf_page(pdf, 1, 72).shape
    assert render_pdf_page(pdf, 0, 72).shape == render_pdf_page(pdf, 1, 72).shape


def test_every_page_of_a_pdf_can_be_rendered() -> None:
    assert len(render_pdf_pages(image_to_pdf(_image(), dpi=72, quality=90), 72)) == 1


# --- Blob core --------------------------------------------------------------------------------------

class _Downloader:
    def __init__(self, data: bytes, content_type: str | None = "image/png") -> None:
        self.size = len(data)
        self._data = data
        self.properties = SimpleNamespace(content_settings=SimpleNamespace(content_type=content_type))

    def readall(self) -> bytes:
        return self._data


class _FakeClient:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.uploaded: dict[str, tuple[bytes, str]] = {}

    def download_blob(self, name: str) -> _Downloader:
        if name not in self.blobs:
            raise ResourceNotFoundError("The specified blob does not exist.\nRequestId:abc")
        return _Downloader(self.blobs[name])

    def upload_blob(self, name, content, overwrite, content_settings) -> None:
        self.uploaded[name] = (content, content_settings.content_type)

    def get_container_properties(self) -> dict:
        return {}


@pytest.fixture
def container() -> BlobContainer:
    blob = BlobContainer(CONNECTION, "files")
    blob._container = _FakeClient()
    return blob


async def test_blob_download_returns_content_and_type(container) -> None:
    container._container.blobs["a.png"] = b"12345"
    assert await container.download("a.png", max_bytes=10) == (b"12345", "image/png")


async def test_blob_download_over_the_limit_is_flagged_too_large(container) -> None:
    container._container.blobs["a.png"] = b"12345"
    with pytest.raises(BlobError) as caught:
        await container.download("a.png", max_bytes=4)
    assert caught.value.too_large and not caught.value.not_found


async def test_blob_download_of_a_missing_file_is_flagged_not_found_with_a_clean_message(container) -> None:
    with pytest.raises(BlobError) as caught:
        await container.download("missing.png", max_bytes=10)
    assert caught.value.not_found
    assert str(caught.value) == "The specified blob does not exist."  # no RequestId lines


async def test_blob_upload_replaces_and_records_the_content_type(container) -> None:
    await container.upload("out.pdf", b"%PDF", "application/pdf")
    assert container._container.uploaded["out.pdf"] == (b"%PDF", "application/pdf")


async def test_blob_warm_up_reads_only_the_container(container) -> None:
    await container.warm_up()

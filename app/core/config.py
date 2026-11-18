"""Settings for the whole project: one base for every service, one for the process itself.

Sources, highest priority first:
    1. values passed in code
    2. <SERVICE>_<NAME> environment variable / .env entry   e.g. CHEQUE_GEMINI_API_KEY (this service only)
    3. <NAME> environment variable                          e.g. GEMINI_API_KEY (every service)
    4. .env                                                 secrets only + APP_ENV
    5. config/settings.<APP_ENV>.json                       per-environment values
    6. config/settings.json                                 shared defaults
    7. defaults in the class

The prefixed form (2) is what lets Leasing and Cheque use different Gemini keys, models, Blob accounts
or upload limits from the one `.env`, while an unprefixed value still configures both.

A config file has a "common" section (every service), one section per service ("leasing", "cheque"),
and may group keys inside a section ({"uploads": {"max_file_size_mb": 15}}). Keys starting with "_" are
comments. Unknown keys are an error. Secrets (.env only) are refused in a config file.
"""

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, Optional

from dotenv import dotenv_values
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVICE_NAMES = ("leasing", "cheque")
COMMON_SECTION = "common"
# Never accepted in a config file: they belong in .env / the environment only.
_SECRET_ONLY = ("app_env", "api_keys", "gemini_api_key", "azure_storage_connection_string")


def csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _split_list(value: Any) -> list[str]:
    """Accept a JSON list or a comma-separated string; drop blanks."""
    if isinstance(value, str):
        value = json.loads(value) if value.strip().startswith("[") else value.split(",")
    return [str(item).strip() for item in value if str(item).strip()] if value else []


# --- sources -----------------------------------------------------------------------------------


class PrefixedEnvSource(PydanticBaseSettingsSource):
    """`<SERVICE>_<NAME>` from the environment or .env, applied to `<name>` for this service only.

    Simple values only (text, numbers, true/false, comma-separated lists) - the same ones .env holds.
    """

    def __init__(self, settings_cls: type[BaseSettings], prefix: str, env_file: Any) -> None:
        super().__init__(settings_cls)
        file_values = {}
        if env_file and Path(env_file).is_file():
            file_values = {key.upper(): value for key, value in dotenv_values(env_file).items() if value is not None}
        merged = {**file_values, **{key.upper(): value for key, value in os.environ.items()}}
        self._values: dict[str, Any] = {}
        for name in settings_cls.model_fields:
            key = f"{prefix}_{name}".upper()
            if key in merged:
                self._values[name] = merged[key]

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return self._values.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return dict(self._values)


class JsonConfigSettingsSource(PydanticBaseSettingsSource):
    """Reads config/settings.json, then config/settings.<APP_ENV>.json on top."""

    def __init__(self, settings_cls: type[BaseSettings], env_file: Any, service: str) -> None:
        super().__init__(settings_cls)
        config_dir = Path(os.environ.get("APP_CONFIG_DIR") or PROJECT_ROOT / "config")
        app_env = _resolve_app_env(env_file)
        self._values: dict[str, Any] = {}
        for path in (config_dir / "settings.json", config_dir / f"settings.{app_env}.json"):
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                self._values.update(_flatten(data, path, settings_cls.model_fields, service))

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return self._values.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return dict(self._values)


def _resolve_app_env(env_file: Any) -> str:
    """APP_ENV from the environment, else from .env, else 'local'."""
    if os.environ.get("APP_ENV"):
        return os.environ["APP_ENV"]
    if env_file and Path(env_file).is_file():
        return dotenv_values(env_file).get("APP_ENV") or "local"
    return "local"


def _flatten(data: dict, path: Path, fields: dict, service: str) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for key, value in data.items():
        if key.startswith("_"):
            continue
        # Another service's section is none of this service's business.
        if key in SERVICE_NAMES and key != service:
            continue
        items = value.items() if isinstance(value, dict) and key not in fields else [(key, value)]
        for name, item in items:
            if name.startswith("_"):
                continue
            if isinstance(item, dict) and name not in fields and key in (COMMON_SECTION, service):
                # one more level of grouping inside a "common" / service section
                for inner_name, inner in item.items():
                    if not inner_name.startswith("_"):
                        _store(values, inner_name, inner, path, fields, service)
                continue
            _store(values, name, item, path, fields, service)
    return values


def _store(values: dict[str, Any], name: str, item: Any, path: Path, fields: dict, service: str) -> None:
    if name in _SECRET_ONLY:
        raise ValueError(f"{path.name}: '{name}' is not allowed in a config file.")
    if name in fields:
        values[name] = item
    elif service == "platform" or name in PlatformSettings.model_fields:
        # A key for the other settings class: the process-level ones are read by PlatformSettings,
        # a service's by that service. Each class checks its own keys, so nothing is skipped unseen.
        return
    else:
        raise ValueError(f"{path.name}: '{name}' is not a setting of the {service} service.")


# --- settings ----------------------------------------------------------------------------------


class ServiceSettings(BaseSettings):
    """What every service needs. A service's own Settings adds its fields (and may change a default)."""

    service_name: ClassVar[str] = ""

    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["local", "uat", "prod"] = "local"
    # Keys accepted in the X-API-Key header; several allow rotation. Required outside local.
    api_keys: Annotated[list[SecretStr], NoDecode] = []
    gemini_api_key: SecretStr
    gemini_model: str
    # Per Gemini call, not per request.
    gemini_timeout_seconds: int = Field(120, ge=1)
    max_file_size_mb: int = Field(15, ge=1)
    # Empty or unset = blob storage off.
    azure_storage_connection_string: Optional[SecretStr] = None

    @field_validator("azure_storage_connection_string", mode="before")
    @classmethod
    def _empty_to_none(cls, value):
        """Treat an empty value in .env (e.g. `AZURE_STORAGE_CONNECTION_STRING=`) as unset."""
        return None if value == "" else value

    @field_validator("api_keys", mode="before")
    @classmethod
    def _split_api_keys(cls, value):
        return _split_list(value)

    @model_validator(mode="after")
    def _require_api_keys_outside_local(self):
        if self.app_env != "local" and not self.api_keys:
            raise ValueError("API_KEYS must be set when APP_ENV is not 'local'.")
        return self

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings):
        env_file = dotenv_settings.env_file
        return (
            init_settings,
            PrefixedEnvSource(settings_cls, cls.service_name, env_file),
            env_settings,
            dotenv_settings,
            JsonConfigSettingsSource(settings_cls, env_file, cls.service_name),
            file_secret_settings,
        )

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_keys)

    @property
    def api_key_values(self) -> list[str]:
        return [key.get_secret_value() for key in self.api_keys]

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024


class PlatformSettings(BaseSettings):
    """The running process: which services it hosts, where it listens, and what is common to them."""

    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    # Comma-separated: "leasing,cheque" (both, one app), "leasing" or "cheque" (that service alone).
    services: str = "leasing,cheque"
    # Read by `python -m app`, the entry point the Windows service runs.
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    # Reverse proxies whose X-Forwarded-* headers are trusted (comma-separated IPs, or "*").
    forwarded_allow_ips: str = "127.0.0.1"
    # Swagger UI at /docs and the schema at /openapi.json. Turn off on an internet-facing host.
    docs_enabled: bool = True
    # Browser origins allowed to call the API (comma-separated or a JSON list). Empty = no CORS headers.
    cors_origins: Annotated[list[str], NoDecode] = []
    # Where the log files go: <log_dir>/<service>/... Empty = the project's own logs/ folder.
    log_dir: str = ""
    log_level: str = "INFO"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value):
        return _split_list(value)

    @model_validator(mode="after")
    def _known_services(self):
        unknown = [name for name in csv(self.services) if name.lower() not in SERVICE_NAMES]
        if unknown or not csv(self.services):
            raise ValueError(f"SERVICES must list some of {', '.join(SERVICE_NAMES)}; got '{self.services}'.")
        return self

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings):
        platform_json = JsonConfigSettingsSource(settings_cls, dotenv_settings.env_file, "platform")
        return init_settings, env_settings, dotenv_settings, platform_json, file_secret_settings

    @property
    def service_list(self) -> list[str]:
        return list(dict.fromkeys(name.lower() for name in csv(self.services)))

    @property
    def log_directory(self) -> Path:
        return Path(self.log_dir) if self.log_dir else PROJECT_ROOT / "logs"


@lru_cache
def get_platform_settings() -> PlatformSettings:
    return PlatformSettings()

"""Leasing service settings: the shared base (key, model, auth, Blob) plus everything Leasing-specific.

Read from environment variables and `.env`, as before; `LEASING_<NAME>` overrides `<NAME>` for this
service only (see app/common/config.py).
"""

from functools import lru_cache

from pydantic import Field

from app.common.config import ServiceSettings, csv


class Settings(ServiceSettings):
    service_name = "leasing"

    gemini_max_tokens: int = 2048
    # Decoding for every call - each is a precise reading task, not a creative one: temperature 0 and
    # top_k 1 always take the single most likely token, top_p 1 never cuts the candidate list
    # first, and a fixed seed makes repeat runs of the same file answer the same way.
    gemini_temperature: float = 0.0
    gemini_seed: int = 0
    gemini_top_p: float = 1.0
    gemini_top_k: int = 1
    # Model for the two crop/rotate calls (box detection, orientation). Empty = GEMINI_MODEL.
    # A Flash-Lite model is faster and cheaper for these; extraction and verification never use it.
    gemini_vision_model: str = ""
    # Thinking tokens allowed to self- and cross-verification. Most of a check's time is spent
    # writing output, so a smaller budget answers sooner; too small can miss subtler problems.
    gemini_verification_thinking_budget: int = 256
    # Per Gemini call, not per request. A call that runs longer fails like any other provider
    # error, so a hung call can no longer hold a request open indefinitely.
    gemini_timeout_seconds: int = Field(105, ge=1)
    # Gemini scales oversized images down and bills them in 768px tiles anyway, so a larger
    # original adds no detail - only upload bandwidth and inline-payload size.
    gemini_max_image_edge: int = 1568
    require_https: bool = True
    rate_limit_requests: int = 30
    rate_limit_window_seconds: int = 60
    # The whole box-detection/crop/rotate pipeline. Off means one model call per request instead
    # of three, and nothing written to detection_output_dir. A request may override this either
    # way with the X-Detection header.
    detection_enabled: bool = True
    detection_output_dir: str = "detections"
    detection_pdf_dpi: int = 200
    detection_crop_padding_px: int = 10
    # Equal on every side: orientation is only known after cropping, so there is no way to tell
    # in advance which edge will end up as the document's top and deserve the extra allowance.
    # Small, because edge refinement puts the corners on the document's real border — the old
    # generous margin only existed to absorb inaccurate corners, and showed up as background.
    detection_crop_padding_ratio: float = 0.015
    detection_crop_top_padding_ratio: float = 0.015
    detection_rotation_min_confidence: float = 0.7
    detection_crop_target_long_edge: int = 1600
    # Light sharpening only, and no contrast boost by default: on printed cards the boost lifted the
    # background security pattern into noise, and stronger sharpening thickened and haloed letters.
    detection_crop_sharpen_amount: float = 0.4
    detection_crop_contrast_clip: float = 0.0
    # A fourth model call per document (on top of extraction, and crop+rotation when detection is
    # on): Gemini looks at the document again alongside its own extracted fields and judges what a
    # field-by-field rule can't - MRZ-to-printed-data consistency, front/back agreement, and other
    # internal contradictions. Same override pattern as detection_enabled: the X-Validation header
    # overrides this per request either way.
    validation_enabled: bool = True
    # How long a byte-identical file's extraction and passed self-verification are reused instead of
    # calling Gemini again, matched by the file's SHA-256. Held in process memory only - never on
    # disk - and cleared on restart. A finding is never reused, so a rejected file is always
    # re-checked. 0 = off.
    result_cache_seconds: int = 10800
    # Files under detection_output_dir and leasing_output_dir (originals, crops, merged PDFs) older
    # than this are deleted by a background sweep that runs at startup and then hourly. 0 = keep forever.
    output_retention_hours: int = 24
    log_retention_days: int = 30
    # Writes every extraction's parsed fields - National ID, DOB, passport/visa numbers - to
    # logs/results.log. Development only: never enable where real customer documents are processed.
    log_extracted_results: bool = False
    # logs/document-results.log keeps each failed document's message text - the reason it failed. A
    # message can quote a value it compares (a name on the front and back, say); long numbers in it are
    # always masked. false = only the step and reason code are logged, never the text.
    log_error_messages: bool = True
    # The log lines also printed to the console (the terminal, the Windows service's stdout log,
    # `docker logs`), each tagged with its log file, warnings/errors marked to stand out:
    # all = every line of every log file; warnings = only warnings and errors; off = files only.
    log_to_console: str = "all"
    # Keys for GET /admin/logs (read the log files remotely, e.g. with tools/watch_logs.py), sent in the
    # X-Admin-Key header. Separate from API_KEYS: a caller's API key can never read logs. Empty = the
    # log endpoint does not exist (404).
    admin_api_keys: str = ""
    # Email alerts on system errors (Gemini unavailable, a crash, a failed job - never a user's document
    # problem such as an expired ID). Off while ALERT_EMAIL_TO or SMTP_HOST is empty. Sent in the
    # background; the same error at most once per ALERT_EMAIL_COOLDOWN_MINUTES (later ones counted).
    alert_email_to: str = ""  # comma-separated recipients
    alert_email_from: str = ""  # empty = SMTP_USERNAME
    alert_server_name: str = ""  # the environment named in the email, e.g. "PRODUCTION"; empty = the machine name
    alert_email_subject: str = "OCR API Alert"  # start of every alert's subject line
    alert_email_cooldown_minutes: int = 15
    smtp_host: str = "smtp.office365.com"
    smtp_port: int = 587  # 587 = STARTTLS, 465 = SSL
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    # Hosts a /ocr/leasing `source` URL may point at, besides the configured blob container
    # (comma-separated, exact hostnames). Empty = only the blob container. Stops the endpoint being
    # used to make the server fetch arbitrary or internal addresses.
    allowed_source_hosts: str = ""
    # Where the /ocr/leasing endpoints write each request's originals, crops and merged PDFs,
    # one folder per document_refnumber.
    leasing_output_dir: str = "postman_output"
    # A country value in form_data that app/data does not recognise is returned as extracted and
    # saved to a new-names list (new_country_codes.json / new_nationalities.json in this folder);
    # Gemini fills in its exact country name in the background, so the next request with the same
    # value is converted. Kept outside app/data, and out of git, because the server writes to it.
    # A relative path is under the project root. Off = unrecognised values are only returned as is.
    country_learning_enabled: bool = True
    country_learning_dir: str = "learned_countries"
    # Azure Blob Storage for /ocr/leasing: sources are fetched from this container and each
    # document's cropped/rotated result is uploaded back beside its original as <name>_ocr.<ext>.
    # Both unset = blob storage off (sources must then be plain downloadable URLs).
    azure_storage_container: str | None = None

    @property
    def allowed_source_host_list(self) -> list[str]:
        return [host.lower() for host in csv(self.allowed_source_hosts)]

    @property
    def admin_api_key_list(self) -> list[str]:
        return csv(self.admin_api_keys)

    @property
    def alert_email_recipients(self) -> list[str]:
        return csv(self.alert_email_to)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
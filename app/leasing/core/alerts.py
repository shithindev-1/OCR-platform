"""Email alerts for system errors.

Every ERROR (or worse) record of the `uae_ocr` loggers - Gemini unavailable after its retry, an
unhandled crash, a failed or stuck background job, file storage unreachable or refusing our links, a
processed file that could not be saved, requests refused by the rate limit, the API key, the HTTPS
check or a broken request format, a lost job, an uploaded file refused (too large, wrong type,
damaged), a failed warm-up, crop or retention sweep - is mailed to
ALERT_EMAIL_TO as a plain, professional message: environment, severity, the issue in plain words, the
reason (the provider's own error text, secrets masked), the user impact and the recommended action.
No file names, line numbers or tracebacks are ever put in an email. A user's document problem (an expired ID, a name mismatch) is logged as a warning or
not at all, so it never mails. Off while ALERT_EMAIL_TO or SMTP_HOST is empty.

Sending never slows or fails a request: emails go out from a background thread, a send failure is
printed to stderr only (never logged through `uae_ocr`, which would mail again), and the same error
(same logger and message template) is mailed at most once per ALERT_EMAIL_COOLDOWN_MINUTES - the next
email says how many more were held back, so a broken Gemini key gives one email, not one per document.
"""

import logging
import queue
import re
import smtplib
import socket
import ssl
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from html import escape

from app.leasing.core.config import Settings
from app.leasing.core.logging import IST_TIME, UAE_TIME

_MAX_QUEUED = 50


def _root_cause(exc: BaseException | None) -> str | None:
    """The original error under any wrapping (VerificationUnavailable <- GeminiServiceError <- the
    provider's own error), as `Type: message`, cut to 300 characters."""
    if exc is None:
        return None
    seen = set()
    while id(exc) not in seen and (exc.__cause__ or exc.__context__):
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return f"{type(exc).__name__}: {exc}"[:300]


@dataclass(frozen=True)
class _Issue:
    severity: str  # Critical = users are blocked; Warning = no user is blocked
    title: str
    impact: str
    action: str


# Checked in order against the error message, the logger name and the original error (lower case).
# The first rule whose words all match a group decides what the email says.
_RULES: list[tuple[tuple[tuple[str, ...], ...], _Issue]] = [
    ((("test alert",),), _Issue(
        "Test", "Test alert - email settings check",
        "None. This is a test message; the service is working normally.",
        "No action needed.")),
    # Requests refused before any work starts (checked first: their text can mention Gemini or a key).
    ((("api's own rate limit",),), _Issue(
        "Critical", "Requests rejected by the API's rate limit",
        "Users get 'Too many requests' and must wait a minute. Callers that send no X-Tenant-Id / X-User-Id "
        "share one allowance per server.",
        "Make the calling application send X-Tenant-Id and X-User-Id, or raise RATE_LIMIT_REQUESTS for this "
        "environment.")),
    ((("api key missing or not valid",),), _Issue(
        "Critical", "Requests rejected: API key missing or not valid",
        "Requests from the calling application are refused (HTTP 401) until the key is fixed.",
        "Check that the calling application's X-API-Key matches one of API_KEYS in this environment (a key may "
        "have been rotated). Repeated attempts from unknown addresses may be someone trying keys.")),
    ((("not received over https",),), _Issue(
        "Critical", "Requests rejected: not received over HTTPS",
        "Every request reaching the API this way is refused (HTTP 400).",
        "Check that the proxy / ingress in front of the API passes X-Forwarded-Proto: https (and that "
        "FORWARDED_ALLOW_IPS trusts it), or that callers use the https:// address.")),
    ((("job results not found",),), _Issue(
        "Critical", "Job results not found (job lost or expired)",
        "The user's progress check gets 'not found' and has to upload the documents again.",
        "Check whether the service restarted, or runs more than one replica (jobs are kept in one process's "
        "memory: run a single replica). A poll more than an hour after the job finished is also refused.")),
    ((("malformed_request",),), _Issue(
        "Warning", "Requests rejected: request format not valid",
        "Requests from the calling application are refused (HTTP 422) - required fields missing or not valid.",
        "Check the calling application's request against the API documentation (the field names are in the "
        "service logs at the time above).")),
    ((("uploaded file rejected",),), _Issue(
        "Warning", "Uploaded file rejected (too large, wrong type or damaged)",
        "This user's file was refused; they are asked to upload a different file. The service is working.",
        "No action needed unless it repeats often - then check the file size limit (MAX_FILE_SIZE_MB) and the "
        "file types the application allows (JPG, PNG, WEBP, PDF).")),
    ((("warm-up failed",),), _Issue(
        "Warning", "Service warm-up failed at startup",
        "None yet: the first requests will connect on their own, but this is an early sign Gemini or file "
        "storage may not be reachable from this server.",
        "Watch for further alerts; check this server's internet access and the Gemini / storage settings.")),
    ((("api key not valid",), ("api_key_invalid",), ("permission_denied",), ("unauthenticated",)), _Issue(
        "Critical", "AI service (Google Gemini) rejected the API key",
        "Documents cannot be read or checked. Every request fails until this is fixed.",
        "Check GEMINI_API_KEY in this environment's settings (it may be wrong, expired or revoked) "
        "and restart the service.")),
    ((("resource_exhausted",), ("quota",), ("too many requests",)), _Issue(
        "Critical", "AI service (Google Gemini) quota or rate limit reached",
        "Requests fail or are delayed until the quota resets.",
        "Check the Gemini quota and billing for this API key; raise the limit if needed.")),
    ((("storage link refused",),), _Issue(
        "Critical", "Document links refused - file storage settings may be wrong",
        "Users' document links from the company file storage are refused, so their requests fail.",
        "Check that AZURE_STORAGE_CONNECTION_STRING and AZURE_STORAGE_CONTAINER of this environment point to the "
        "storage account and container the application sends links from (or that the link was sent to the right "
        "environment).")),
    ((("blob storage upload",),), _Issue(
        "Critical", "Processed files could not be saved to file storage (Azure Blob)",
        "Users are told their processed copy could not be saved and must try again.",
        "Check the Azure storage connection string, container permissions (write access) and network access for "
        "this environment.")),
    ((("source download failed",),), _Issue(
        "Critical", "Document links could not be downloaded",
        "Documents sent as links cannot be fetched; those requests fail.",
        "Check this server's internet access and that the linked file server is up.")),
    ((("blob",), ("azure",)), _Issue(
        "Critical", "File storage (Azure Blob) is not reachable",
        "Document links cannot be downloaded or processed files cannot be saved; affected requests fail.",
        "Check the Azure storage connection string, container name and network access for this environment.")),
    ((("unavailable",), ("gemini",), ("deadline",)), _Issue(
        "Critical", "AI service (Google Gemini) is not responding",
        "Documents cannot be read or checked; users are told to try again later.",
        "Usually temporary. If it continues for more than 15 minutes, check the Google Gemini status and the "
        "server's internet access.")),
    ((("may be stuck",),), _Issue(
        "Critical", "A background job is stuck",
        "The user's progress bar has stopped moving; their documents are not being processed.",
        "Check the service logs and the Google Gemini status; if several jobs are stuck, restart the service.")),
    ((("job failed",),), _Issue(
        "Critical", "A background job could not finish",
        "The user's request ended with 'could not finish' and has to be submitted again.",
        "Check the service logs at the time above for the cause; report to the development team if it repeats.")),
    ((("unhandled error",),), _Issue(
        "Critical", "Unexpected server error",
        "A request failed with an internal server error (HTTP 500).",
        "Check the service logs at the time above; report to the development team if it repeats.")),
    ((("crop failed",),), _Issue(
        "Warning", "Document cropping failed",
        "No user is blocked: the document was still processed, only its cropped copy was not produced.",
        "No action needed unless it repeats often.")),
    ((("retention",),), _Issue(
        "Warning", "Scheduled clean-up of old files failed",
        "No user is blocked; old processed files may stay on the server longer.",
        "Check the server's free disk space and folder permissions.")),
    ((("could not write",), (".log",)), _Issue(
        "Warning", "A log file could not be written",
        "No user is blocked; some log lines may be missing.",
        "Check the server's free disk space and folder permissions.")),
]
_DEFAULT_ISSUE = _Issue(
    "Critical", "Unexpected system error",
    "A request may have failed.",
    "Check the service logs at the time above; report to the development team if it repeats.")

_SECRET_IN_URL = re.compile(r"(?i)\b(key|sig|token|password|accountkey)=[^&\s'\"]+")
_REFERENCE = re.compile(r"\b(request_id|job_id)=([\w.-]+)")


def _classify(record: logging.LogRecord, message: str, cause: str | None) -> _Issue:
    text = " ".join((message, record.name, cause or "")).lower()
    for groups, issue in _RULES:
        if any(all(word in text for word in group) for group in groups):
            return issue
    return _DEFAULT_ISSUE


def _reason(cause: str | None) -> str:
    """The provider's own error text without the program's internal names, and with any secret masked."""
    if not cause:
        return "No further detail was reported."
    kind, _, detail = cause.partition(": ")
    text = detail.strip() or kind
    return _SECRET_IN_URL.sub(lambda match: f"{match.group(1)}=***", text)[:300]


def _references(message: str) -> str | None:
    found = [f"{'Request' if name == 'request_id' else 'Job'} {value}" for name, value in _REFERENCE.findall(message)]
    return ", ".join(found) or None


def build_alert(record: logging.LogRecord, settings: Settings, *, held_back: int = 0) -> EmailMessage:
    """A plain, professional alert email: what happened, where, the impact and what to do - no code details."""
    environment = settings.alert_server_name or socket.gethostname()
    moment = datetime.fromtimestamp(record.created, tz=timezone.utc)
    message = record.getMessage().splitlines()[0] if record.getMessage() else record.levelname
    cause = _root_cause(record.exc_info[1]) if record.exc_info else None
    issue = _classify(record, message, cause)
    reason = _reason(cause)
    reference = _references(message)
    when = (f"{moment.astimezone(UAE_TIME):%d-%m-%Y %H:%M:%S} UAE  |  {moment:%H:%M:%S} UTC  |  "
            f"{moment.astimezone(IST_TIME):%H:%M:%S} IST")
    cooldown = settings.alert_email_cooldown_minutes
    occurrences = (f"1 + {held_back} more in the last {cooldown} minutes" if held_back
                   else f"First occurrence (repeats are summarised, at most one email per {cooldown} minutes)")

    rows = [
        ("Environment", environment),
        ("Severity", issue.severity),
        ("Issue", issue.title),
        ("Reason", reason),
        ("User impact", issue.impact),
        ("Recommended action", issue.action),
        ("Time", when),
        ("Occurrences", occurrences),
    ]
    if reference:
        rows.append(("Reference", reference))

    footer = ("This is an automated message from the OCR API monitoring. Please do not reply. "
              "Full details are in the service logs (alerts and errors) for the time above.")
    text = "\n".join([f"OCR API Alert - {environment}", "=" * 40, "",
                      *(f"{label + ':':<20}{value}" for label, value in rows), "", footer])

    colour = {"Critical": "#b42318", "Warning": "#b54708"}.get(issue.severity, "#175cd3")
    cells = "".join(
        f'<tr><td style="padding:8px 12px;color:#475467;width:170px;vertical-align:top;border-top:1px solid #eaecf0">'
        f'{escape(label)}</td><td style="padding:8px 12px;color:#101828;border-top:1px solid #eaecf0">'
        f'{escape(value)}</td></tr>'
        for label, value in rows
    )
    html = (
        '<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#101828;max-width:680px">'
        f'<div style="background:{colour};color:#ffffff;padding:14px 18px;border-radius:6px 6px 0 0">'
        f'<div style="font-size:12px;letter-spacing:.5px;text-transform:uppercase">{escape(issue.severity)} '
        f'&middot; {escape(environment)}</div>'
        f'<div style="font-size:18px;font-weight:600;margin-top:4px">{escape(issue.title)}</div></div>'
        '<table style="border-collapse:collapse;width:100%;border:1px solid #eaecf0;border-top:none">'
        f'{cells}</table>'
        f'<p style="font-size:12px;color:#667085;margin-top:14px">{escape(footer)}</p></div>'
    )

    email = EmailMessage()
    prefix = settings.alert_email_subject or "OCR API Alert"
    email["Subject"] = f"{prefix} | {environment} | {issue.severity}: {issue.title}"
    email["From"] = settings.alert_email_from or settings.smtp_username
    email["To"] = ", ".join(settings.alert_email_recipients)
    email.set_content(text)
    email.add_alternative(html, subtype="html")
    return email


def send_email(email: EmailMessage, settings: Settings) -> None:
    context = ssl.create_default_context()
    if settings.smtp_port == 465:
        smtp: smtplib.SMTP = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=20, context=context)
    else:
        smtp = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20)
    with smtp:
        if settings.smtp_port != 465 and settings.smtp_starttls:
            smtp.starttls(context=context)
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(email)


class EmailAlertHandler(logging.Handler):
    """ERROR+ records -> short alert emails, throttled per error, sent in the background."""

    def __init__(self, settings: Settings, *, background: bool = True) -> None:
        super().__init__(level=logging.ERROR)
        self.settings = settings
        self.background = background
        self._last_sent: dict[tuple[str, str], float] = {}
        self._held_back: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()
        self._queue: queue.Queue[EmailMessage] = queue.Queue(maxsize=_MAX_QUEUED)
        if background:
            threading.Thread(target=self._run, name="email-alerts", daemon=True).start()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            key = (record.name, str(record.msg))
            now = time.monotonic()
            with self._lock:
                last = self._last_sent.get(key)
                if last is not None and now - last < self.settings.alert_email_cooldown_minutes * 60:
                    self._held_back[key] = self._held_back.get(key, 0) + 1
                    return
                self._last_sent[key] = now
                held_back = self._held_back.pop(key, 0)
            email = build_alert(record, self.settings, held_back=held_back)
            if self.background:
                self._queue.put_nowait(email)
            else:
                self._deliver(email)
        except queue.Full:
            print("[email-alerts] too many alerts waiting - this one was dropped", file=sys.stderr)
        except Exception:
            self.handleError(record)

    def _deliver(self, email: EmailMessage) -> None:
        try:
            send_email(email, self.settings)
        except Exception as exc:  # never through uae_ocr: that would try to mail again
            print(f"[email-alerts] could not send the alert email: {type(exc).__name__}: {exc}", file=sys.stderr)

    def _run(self) -> None:
        while True:
            self._deliver(self._queue.get())


def configure_email_alerts(settings: Settings, logger_name: str = "uae_ocr") -> None:
    """Adds (or removes, when not configured) the alert handler on `logger_name` - never twice."""
    logger = logging.getLogger(logger_name)
    for handler in [h for h in logger.handlers if isinstance(h, EmailAlertHandler)]:
        logger.removeHandler(handler)
    if settings.alert_email_recipients and settings.smtp_host:
        logger.addHandler(EmailAlertHandler(settings))

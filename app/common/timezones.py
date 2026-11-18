"""UAE and IST time helpers (neither observes DST, so fixed offsets are exact all year, and Windows
has no IANA database for ZoneInfo to read without the extra tzdata package)."""

from datetime import datetime, timedelta, timezone

UAE_TZ = timezone(timedelta(hours=4), "GST")  # Asia/Dubai
IST_TZ = timezone(timedelta(hours=5, minutes=30), "IST")  # Asia/Kolkata


def now_uae() -> datetime:
    return datetime.now(UAE_TZ)


def now_ist() -> datetime:
    return datetime.now(IST_TZ)


def format_timestamp(ts: float, tz: timezone) -> str:
    """Epoch seconds -> 'YYYY-MM-DD HH:MM:SS.mmm' in the given timezone."""
    return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

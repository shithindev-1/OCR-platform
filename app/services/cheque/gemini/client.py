"""Gemini API client (singleton)."""

from functools import lru_cache

from google import genai

from app.core.cheque.config import get_settings
from app.services.gemini_client import build_gemini_client


@lru_cache
def get_gemini_client() -> genai.Client:
    settings = get_settings()
    return build_gemini_client(settings.gemini_api_key.get_secret_value(), settings.gemini_timeout_seconds)

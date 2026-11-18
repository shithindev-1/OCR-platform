"""The one place a Gemini API client is built, for both services."""

from google import genai
from google.genai import types


def build_gemini_client(api_key: str, timeout_seconds: int) -> genai.Client:
    return genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=timeout_seconds * 1000))

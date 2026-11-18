"""Setup for the tests of the merged app: dummy credentials and a throwaway log folder.

Set before anything is imported, so a developer's own .env never changes what is tested and the real
logs/ is never written to. Gemini and Azure are never called.
"""

import os
import tempfile

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="ocr-platform-test-logs-"))
os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("GEMINI_MODEL", "test-model")

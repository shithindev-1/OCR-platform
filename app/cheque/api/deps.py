"""FastAPI dependencies."""

from functools import lru_cache

from app.cheque.core.config import get_settings
from app.cheque.services.cheque_extractor import ChequeExtractor


@lru_cache
def get_cheque_extractor() -> ChequeExtractor:
    return ChequeExtractor(get_settings())

"""PDF helpers for both services: render pages to images, and wrap an image into a PDF.

Leasing and Cheque used two different PDF libraries for the same two jobs; both now go through
PDFium (render) and Pillow (write), so there is one native PDF dependency and one set of behaviour.
"""

import threading
from io import BytesIO

import cv2
import numpy as np
import pypdfium2 as pdfium
from PIL import Image

# Pages of a PDF built from crops are stored as JPEG; Pillow's default (quality 75, halved colour
# resolution) visibly blurs small print, so they are saved near-lossless with full colour detail.
PDF_PAGE_QUALITY = {"quality": 95, "subsampling": 0}

# PDFium is not thread-safe, and PDFs are rendered in worker threads - several files of one request
# at once. Two renders at the same time crashed with "access violation" and every file of the request
# failed as unreadable, so all PDFium work runs one document at a time.
_PDFIUM_LOCK = threading.Lock()


def render_pdf_pages(content: bytes, dpi: int) -> list[np.ndarray]:
    """Every page of a PDF as a BGR array, rendered at `dpi`."""
    with _PDFIUM_LOCK:
        return _render_locked(content, dpi, None)


def render_pdf_page(content: bytes, page_number: int, dpi: int) -> np.ndarray:
    """One page (1-based) as a BGR array; a number outside the document is clamped to its nearest page."""
    with _PDFIUM_LOCK:
        return _render_locked(content, dpi, page_number)[0]


def _render_locked(content: bytes, dpi: int, page_number: int | None) -> list[np.ndarray]:
    pdf = pdfium.PdfDocument(content)
    try:
        scale = dpi / 72
        if page_number is None:
            indexes = range(len(pdf))
        else:
            indexes = [min(max(page_number, 1), len(pdf)) - 1]
        pages = []
        for index in indexes:
            page = pdf[index]
            try:
                bitmap = page.render(scale=scale)
                pil_image = bitmap.to_pil().convert("RGB")
            finally:
                page.close()
            pages.append(cv2.cvtColor(np.array(pil_image), cv2.COLOR_RGB2BGR))
        return pages
    finally:
        pdf.close()


def image_to_pdf(image_bgr: np.ndarray, *, dpi: int, quality: int) -> bytes:
    """A one-page PDF holding the image (JPEG-compressed), sized so it prints at `dpi`."""
    rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
    buffer = BytesIO()
    Image.fromarray(rgb).save(buffer, format="PDF", resolution=float(dpi), quality=quality)
    return buffer.getvalue()

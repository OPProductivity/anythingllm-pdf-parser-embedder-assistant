"""Explicit, configurable limits; exceeding them never means truncation."""
import math
import os
from pathlib import Path


class PdfBudgetExceeded(ValueError):
    pass


DEFAULTS = {"SOURCE_BYTES": 6 * 1024**3, "PAGES": 10_000,
            "RASTER_PIXELS": 50_000_000, "PAGE_TEXT_BYTES": 2 * 1024**2,
            "TEXT_BYTES": 150 * 1024**2, "SEGMENTS": 100_000,
            "PREPARATION_SECONDS": 6 * 60 * 60}


def limit(name):
    variable = "ANYTHINGLLM_PDF_MAX_" + name
    try:
        value = int(os.environ.get(variable, DEFAULTS[name]))
    except ValueError:
        raise PdfBudgetExceeded(f"{variable} must be a positive integer.") from None
    if value <= 0:
        raise PdfBudgetExceeded(f"{variable} must be a positive integer.")
    return value


def check(name, observed):
    maximum = limit(name)
    if observed > maximum:
        raise PdfBudgetExceeded(f"PDF resource budget exceeded: {name}={observed}, limit={maximum}. No truncated output is accepted. Adjust ANYTHINGLLM_PDF_MAX_{name} explicitly if this source is trusted.")


def check_source(path):
    check("SOURCE_BYTES", Path(path).stat().st_size)


def check_document(document):
    check("PAGES", document.page_count)


def check_raster(page, dpi):
    width, height = float(page.rect.width), float(page.rect.height)
    if not (math.isfinite(width) and math.isfinite(height) and width > 0 and height > 0):
        raise PdfBudgetExceeded("PDF page has invalid raster geometry.")
    check("RASTER_PIXELS", math.ceil(width * dpi / 72) * math.ceil(height * dpi / 72))


def check_pages(pages):
    total = 0
    for page in pages:
        size = len(str(page.get("text") or "").encode("utf-8"))
        check("PAGE_TEXT_BYTES", size)
        total += size
        check("TEXT_BYTES", total)
    return pages

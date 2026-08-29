"""PDF extraction helpers and optional-backend runtime discovery.

The Automatic policy calls these helpers to obtain comparable page candidates.
They may locate optional local packages deliberately, because a Desktop user
can have a supported extractor installed outside the project virtual
environment.  Discovery is not selection: the pipeline scores candidates and
records the chosen backend.  Keep path discovery, runtime capability evidence,
and extraction results distinct so a merely importable backend is never
reported as having been used.
"""

import argparse
import asyncio
import csv
import hashlib
import importlib
import importlib.util
import inspect
import json
import logging
import os
import re
import shutil
import site
import subprocess
import sys
import tempfile
import threading
import time
from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from functools import lru_cache
from multiprocessing import get_context
from pathlib import Path
from typing import Any

try:
    import fitz
except ImportError:
    print("PyMuPDF is not installed. Run: python -m pip install --user pymupdf", file=sys.stderr)
    raise

PYMUPDF4LLM_OCR_DPI = 200
PYMUPDF4LLM_OCR_PAGE_WORKERS_DEFAULT = 4
PYMUPDF4LLM_OCR_PAGE_WORKERS_MAX = 4
# This is a worker-liveness lease, not a page-processing deadline. A healthy
# worker refreshes its current page/phase while PyMuPDF4LLM is running and may
# take arbitrarily long. Retirement is allowed only when even that independent
# liveness thread has stopped for a sustained interval.
PYMUPDF4LLM_WORKER_HEARTBEAT_SECONDS = 5.0
PYMUPDF4LLM_WORKER_STALE_HEARTBEAT_SECONDS = 90.0
UNSTRUCTURED_OCR_PAGE_WORKERS_DEFAULT = 2
UNSTRUCTURED_OCR_PAGE_WORKERS_MAX = 4
# A page-level timeout prevents one broken OCR/layout-model invocation from
# holding an entire batch forever.  It is deliberately generous: normal OCR
# may be slow, while a truly stuck native worker needs an explicit recovery
# result rather than an invisible indefinite wait.
UNSTRUCTURED_OCR_PAGE_TIMEOUT_SECONDS_DEFAULT = 240
UNSTRUCTURED_OCR_PAGE_TIMEOUT_SECONDS_MIN = 30
UNSTRUCTURED_OCR_PAGE_TIMEOUT_SECONDS_MAX = 1800
# The cache contains prepared element text, including the conservative
# category cleanup below. Increment this whenever prepared text changes so an
# older noisy OCR cache is never silently reused as current output.
UNSTRUCTURED_OCR_CACHE_SCHEMA_VERSION = 19
# A photographed page benefits from a small, deterministic outer-margin crop
# before OCR.  It removes scanner borders and handwritten marginalia without
# changing the source-page identity or trying to reconstruct a new PDF.
PHOTOGRAPHED_PAGE_OCR_DPI = 144
# Preserve near-edge drop caps on photographed book pages while still trimming
# the scanner/photo border.  The right margin remains wider because it is the
# common location for handwritten annotation in the reviewed sources.
# Keep a small outer-margin guard, but do not trim the final glyphs of warped
# photographed pages whose printed text approaches the right edge of the PDF
# CropBox.  The former .925 boundary visibly changed words such as ``between``
# to ``betweer`` and dropped final letters in an early photographed-PDF pilot.
PHOTOGRAPHED_PAGE_CROP = (0.035, 0.055, 0.965, 0.95)
LOGGER = logging.getLogger(__name__)

LIGATURES = {
    "\ufb00": "ff",
    "\ufb01": "fi",
    "\ufb02": "fl",
    "\ufb03": "ffi",
    "\ufb04": "ffl",
    "\ufb05": "ft",
    "\ufb06": "st",
}


def optional_backend_search_paths():
    candidates = []
    try:
        user_site = site.getusersitepackages()
        if isinstance(user_site, str):
            candidates.append(user_site)
        else:
            candidates.extend(user_site)
    except Exception:
        pass

    version_tag = f"Python{sys.version_info.major}{sys.version_info.minor}"
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(str(Path(appdata) / "Python" / version_tag / "site-packages"))
    local_programs = os.environ.get("LOCALAPPDATA")
    if local_programs:
        candidates.append(
            str(
                Path(local_programs)
                / "Programs"
                / "Python"
                / f"Python{sys.version_info.major}{sys.version_info.minor}"
                / "Lib"
                / "site-packages"
            )
        )

    clean = []
    seen = set()
    for raw_path in candidates:
        path_text = str(raw_path or "").strip()
        if not path_text or path_text in seen:
            continue
        seen.add(path_text)
        if Path(path_text).exists():
            clean.append(path_text)
    return clean


@lru_cache(maxsize=None)
def ensure_optional_backend_path(module_name: str):
    def resolution_details(status, spec=None, added_paths=None):
        origin = str(getattr(spec, "origin", "") or "")
        origin_path = Path(origin) if origin else None
        user_paths = {Path(path).resolve() for path in optional_backend_search_paths()}
        source = "unknown"
        if origin_path:
            try:
                resolved_origin = origin_path.resolve()
            except OSError:
                resolved_origin = origin_path
            if any(parent == resolved_origin or parent in resolved_origin.parents for parent in user_paths):
                source = "optional_user_site_path"
            elif str(resolved_origin).startswith(str(Path(sys.prefix))):
                source = "active_python_environment"
            else:
                source = "external_python_path"
        return {
            "status": status,
            "path": ";".join(added_paths or []),
            "module_origin": origin,
            "resolution_source": source,
            "optional_search_paths_enabled": os.environ.get(
                "RAG_ALLOW_OPTIONAL_BACKEND_PATHS", "1"
            ).strip().casefold() not in {"0", "false", "no", "off"},
        }

    try:
        spec = importlib.util.find_spec(module_name)
        if spec:
            return resolution_details("already_available", spec)
    except Exception:
        pass

    optional_paths_enabled = os.environ.get(
        "RAG_ALLOW_OPTIONAL_BACKEND_PATHS", "1"
    ).strip().casefold() not in {"0", "false", "no", "off"}
    if not optional_paths_enabled:
        return resolution_details("missing")

    added_paths = []
    for path_text in optional_backend_search_paths():
        if path_text not in sys.path:
            sys.path.append(path_text)
            added_paths.append(path_text)
    try:
        spec = importlib.util.find_spec(module_name)
        if spec:
            return resolution_details(
                "resolved_via_optional_search_paths", spec, added_paths
            )
    except Exception:
        pass
    return resolution_details("missing", added_paths=added_paths)


def import_optional_backend(module_name: str):
    if module_name == "unstructured" or module_name.startswith("unstructured."):
        ensure_unstructured_asyncio_compatibility()
    try:
        return importlib.import_module(module_name)
    except ImportError as original_exc:
        resolution = ensure_optional_backend_path(module_name)
        if resolution.get("status") == "missing":
            raise original_exc
        return importlib.import_module(module_name)


def ensure_unstructured_asyncio_compatibility():
    """Bridge Unstructured's removed-API dependency without changing its policy.

    Unstructured 0.18.32 decorates PDF helpers with
    ``asyncio.iscoroutinefunction``. Python 3.14 deprecates that alias and
    Python 3.16 removes it, while the supported replacement has equivalent
    behavior for this project's native ``async def`` call paths. Install the
    replacement only immediately before an Unstructured import. The assignment
    is process-wide because the vendor module itself resolves the attribute
    from the global ``asyncio`` module during import; it is intentionally not a
    general application policy or a patch to site-packages.
    """
    if getattr(asyncio, "iscoroutinefunction", None) is not inspect.iscoroutinefunction:
        asyncio.iscoroutinefunction = inspect.iscoroutinefunction


def default_tesseract_executable_candidates():
    """Return only absolute environment-derived Tesseract install paths."""
    candidates = []
    for root_text, relative_path in (
        (os.environ.get("ProgramFiles"), ("Tesseract-OCR", "tesseract.exe")),
        (os.environ.get("ProgramFiles(x86)"), ("Tesseract-OCR", "tesseract.exe")),
        (os.environ.get("LOCALAPPDATA"), ("Programs", "Tesseract-OCR", "tesseract.exe")),
    ):
        root_text = str(root_text or "").strip()
        root = Path(root_text) if root_text else None
        if root and root.is_absolute():
            candidates.append(root.joinpath(*relative_path))
    return candidates


@lru_cache(maxsize=1)
def detect_tesseract_executable():
    candidates = []

    def add(path_text):
        path = str(path_text or "").strip()
        if path:
            candidates.append(path)

    for env_key in ("TESSERACT_CMD", "TESSERACT_PATH"):
        add(os.environ.get(env_key))
    add(shutil.which("tesseract"))
    for candidate in default_tesseract_executable_candidates():
        add(candidate)

    seen = set()
    for candidate in candidates:
        normalized = candidate.casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        path = Path(candidate)
        if path.exists():
            return path
    return None


@lru_cache(maxsize=1)
def ensure_tesseract_runtime():
    exe = detect_tesseract_executable()
    result = {
        "available": exe is not None,
        "executable": str(exe) if exe else "",
        "tessdata_prefix": "",
    }
    if exe is None:
        return result

    exe_dir = str(exe.parent)
    current_path = os.environ.get("PATH") or ""
    path_entries = [entry for entry in current_path.split(os.pathsep) if entry]
    normalized_entries = {entry.casefold() for entry in path_entries}
    if exe_dir.casefold() not in normalized_entries:
        os.environ["PATH"] = exe_dir + os.pathsep + current_path if current_path else exe_dir

    tessdata_dir = exe.parent / "tessdata"
    if tessdata_dir.exists():
        os.environ.setdefault("TESSDATA_PREFIX", str(tessdata_dir))
        result["tessdata_prefix"] = str(tessdata_dir)
    return result


def unstructured_runtime_status(strategy: str = "fast"):
    backend_resolution = ensure_optional_backend_path("unstructured.partition.pdf")
    backend_available = False
    backend_import_error = ""
    if backend_resolution.get("status") != "missing":
        try:
            import_optional_backend("unstructured.partition.pdf")
            backend_available = True
        except Exception as exc:
            backend_import_error = f"{type(exc).__name__}: {exc}"
    requested = (strategy or "fast").strip().casefold()
    ocr_required = requested in {"hi_res", "ocr_only"}
    tesseract = ensure_tesseract_runtime()
    return {
        "backend_available": backend_available,
        "backend_resolution": backend_resolution.get("status") or "missing",
        "backend_resolution_source": backend_resolution.get("resolution_source") or "unknown",
        "backend_module_origin": backend_resolution.get("module_origin") or "",
        "optional_search_paths_enabled": bool(
            backend_resolution.get("optional_search_paths_enabled", True)
        ),
        "backend_import_error": backend_import_error,
        "ocr_required": ocr_required,
        "tesseract_available": bool(tesseract.get("available")),
        "tesseract_executable": tesseract.get("executable") or "",
        "tessdata_prefix": tesseract.get("tessdata_prefix") or "",
    }


def safe_stem(value: str) -> str:
    value = re.sub(r"[^\w.-]+", "-", value.strip(), flags=re.UNICODE)
    value = value.strip(".-_")
    return value or "rag-source"


def clean_label(value: str) -> str:
    value = re.sub(r"[\r\n\[\]]+", " ", value.strip())
    value = re.sub(r"\s+", " ", value)
    return value or "Source"


def normalize_text(text: str) -> str:
    if not text:
        return ""

    for bad, good in LIGATURES.items():
        text = text.replace(bad, good)

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1-\2", text)
    text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", " ", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r" {2,}", " ", text)
    return text.strip()


def split_into_segments(text: str, max_chars: int, min_boundary: int):
    text = normalize_text(text)
    if not text:
        return []

    segments = []
    remaining = text

    while len(remaining) > max_chars:
        cut = remaining.rfind(". ", 0, max_chars)
        cut_adjust = 1
        if cut < min_boundary:
            cut = remaining.rfind("; ", 0, max_chars)
            cut_adjust = 1
        if cut < min_boundary:
            cut = remaining.rfind(", ", 0, max_chars)
            cut_adjust = 1
        if cut < min_boundary:
            cut = remaining.rfind(" ", 0, max_chars)
            cut_adjust = 0
        if cut < min_boundary:
            cut = max_chars
            cut_adjust = 0
        else:
            cut += cut_adjust

        piece = remaining[:cut].strip()
        if piece:
            segments.append(piece)
        remaining = remaining[cut:].strip()

    if remaining:
        segments.append(remaining)

    return segments


def make_stop_regex(headings):
    headings = [h.strip() for h in headings if h and h.strip()]
    if not headings:
        return None
    pattern = (
        r"^\s*(?:\[[^\]\n]+\]\s*)?(?:#{1,6}\s*)?(?:[*_`~\s])*("
        + "|".join(re.escape(h) for h in headings)
        + r")(?:\b|[*_`~\s]|$)"
    )
    return re.compile(pattern, re.IGNORECASE)


DEFAULT_END_SECTION_HEADINGS = [
    "Notes",
    "Bibliography",
    "Index",
    "References",
    "Works Cited",
    "Endnotes",
]


def looks_like_section_heading(text: str, heading: str) -> bool:
    text = normalize_text(text)
    if not text:
        return False
    pattern = (
        r"^\s*(?:\[[^\]\n]+\]\s*)?(?:#{1,6}\s*)?(?:[*_`~\s])*"
        + re.escape(heading)
        + r"(?:\b|[*_`~\s]|$)"
    )
    return re.search(pattern, text, re.IGNORECASE) is not None


def detect_end_section_start(pages, headings=None, min_fraction=0.55):
    headings = headings or DEFAULT_END_SECTION_HEADINGS
    if not pages:
        return None

    max_page = max((p["page"] for p in pages), default=0)
    min_page = max(1, int(max_page * min_fraction))

    for page_info in pages:
        page_num = page_info["page"]
        if page_num < min_page:
            continue

        clean = normalize_text(page_info.get("text", ""))
        if not clean:
            continue

        for heading in headings:
            if looks_like_section_heading(clean, heading):
                return {"page": page_num, "heading": heading}

    return None


def find_marker(text: str, pos: int) -> str:
    prefix = text[:pos]
    match = None
    for match in re.finditer(r"\[[^\]\n]*PDF_PAGE[^\]\n]*\]", prefix):
        pass
    return match.group(0) if match else ""


def write_validation_report(full_text: str, phrases, report_path: Path):
    rows = []
    lower = full_text.casefold()

    for phrase in phrases:
        phrase_lower = phrase.casefold()
        pos = lower.find(phrase_lower)
        status = "found" if pos >= 0 else "missing"
        preview = ""
        marker = ""
        if pos >= 0:
            start = max(0, pos - 180)
            end = min(len(full_text), pos + 520)
            preview = full_text[start:end].replace("\n", " ")
            marker = find_marker(full_text, pos)
            print("FOUND:", phrase)
            if marker:
                print("Marker:", marker)
            print(preview[:800])
            print()
        else:
            print("MISS:", phrase)

        rows.append(
            {
                "phrase": phrase,
                "status": status,
                "char_position": pos,
                "marker": marker,
                "preview": preview[:1000],
            }
        )

    with report_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["phrase", "status", "char_position", "marker", "preview"],
        )
        writer.writeheader()
        writer.writerows(rows)


def get_pages_with_pymupdf(pdf_path: Path, progress_callback=None):
    pages = []
    # PyMuPDF's ``Document`` supports indexed page access, but its type stub
    # does not promise the iterator protocol. Indexed access is also explicit
    # about the stable page number we put into provenance.
    # Close the native document before returning. On Windows an open PyMuPDF
    # handle can keep the source PDF locked while later preparation stages or
    # the user try to move/delete it; all returned page text is already plain
    # Python data and does not need the document to stay alive.
    with fitz.open(pdf_path) as doc:
        page_count = len(doc)
        for page_index in range(page_count):
            page_num = page_index + 1
            page = doc[page_index]
            pages.append({"page": page_num, "text": page.get_text("text"), "kind": "page"})
            if callable(progress_callback):
                progress_callback(page_num, page_count)
    return pages, page_count


def pymupdf4llm_ocr_page_workers() -> int:
    """Return the bounded, process-safe OCR worker count for every document.

    This is intentionally a global runtime setting, never a per-PDF learned
    choice. Process isolation is required because the underlying Leptonica OCR
    bridge is not thread-safe; the bounded cap protects Desktop responsiveness.
    """
    raw = os.environ.get("RAG_PDF_OCR_PAGE_WORKERS", str(PYMUPDF4LLM_OCR_PAGE_WORKERS_DEFAULT))
    try:
        requested = int(str(raw).strip())
    except (TypeError, ValueError):
        requested = PYMUPDF4LLM_OCR_PAGE_WORKERS_DEFAULT
    return max(1, min(PYMUPDF4LLM_OCR_PAGE_WORKERS_MAX, requested))


def unstructured_ocr_page_workers() -> int:
    """Return a conservative, isolated worker count for Unstructured OCR.

    Tesseract itself can use several threads for one page. Two independent
    workers are therefore the default; four is an opt-in ceiling for machines
    that have been benchmarked with ``RAG_PDF_UNSTRUCTURED_OCR_PAGE_WORKERS``.
    """
    raw = os.environ.get(
        "RAG_PDF_UNSTRUCTURED_OCR_PAGE_WORKERS",
        str(UNSTRUCTURED_OCR_PAGE_WORKERS_DEFAULT),
    )
    try:
        requested = int(str(raw).strip())
    except (TypeError, ValueError):
        requested = UNSTRUCTURED_OCR_PAGE_WORKERS_DEFAULT
    return max(1, min(UNSTRUCTURED_OCR_PAGE_WORKERS_MAX, requested))


def unstructured_ocr_page_timeout_seconds() -> int:
    """Return the bounded per-page timeout for isolated Unstructured OCR."""
    raw = os.environ.get(
        "RAG_PDF_UNSTRUCTURED_OCR_PAGE_TIMEOUT_SECONDS",
        str(UNSTRUCTURED_OCR_PAGE_TIMEOUT_SECONDS_DEFAULT),
    )
    try:
        requested = int(str(raw).strip())
    except (TypeError, ValueError):
        requested = UNSTRUCTURED_OCR_PAGE_TIMEOUT_SECONDS_DEFAULT
    return max(
        UNSTRUCTURED_OCR_PAGE_TIMEOUT_SECONDS_MIN,
        min(UNSTRUCTURED_OCR_PAGE_TIMEOUT_SECONDS_MAX, requested),
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _unstructured_package_version() -> str:
    try:
        from importlib.metadata import version

        return version("unstructured")
    except Exception:
        return "unknown"


def _normalized_ocr_page_numbers(page_numbers, source_page_count=None):
    """Return ordered, unique one-based page numbers, or ``None`` for all pages.

    A targeted OCR cache must never be mistaken for a whole-document OCR cache.
    Keeping this normalization adjacent to cache identity also makes direct and
    worker-process callers agree about the exact requested scope.
    """
    if page_numbers is None:
        return None
    normalized = []
    for raw_page in page_numbers:
        try:
            page_number = int(raw_page)
        except (TypeError, ValueError):
            continue
        if page_number < 1:
            continue
        if source_page_count is not None and page_number > int(source_page_count):
            continue
        if page_number not in normalized:
            normalized.append(page_number)
    return sorted(normalized)


def _unstructured_ocr_cache_identity(pdf_path: Path, strategy: str, runtime: dict, page_numbers=None) -> dict:
    """Return only stable, non-sensitive cache identity fields.

    A cache hit must never cross a source change, strategy change, package
    upgrade, or Tesseract executable change.  The actual extracted text is
    intentionally stored only in the local cache payload, never in logs.
    """
    source = Path(pdf_path)
    tesseract_path = Path(str((runtime or {}).get("tesseract_executable") or ""))
    try:
        tesseract_stat = tesseract_path.stat()
        tesseract_identity = {
            "path": str(tesseract_path),
            "size": int(tesseract_stat.st_size),
            "mtime_ns": int(tesseract_stat.st_mtime_ns),
        }
    except OSError:
        tesseract_identity = {"path": str(tesseract_path), "missing": True}
    return {
        "schema_version": UNSTRUCTURED_OCR_CACHE_SCHEMA_VERSION,
        "source_sha256": _file_sha256(source),
        "strategy": str(strategy or "").casefold(),
        "unstructured_version": _unstructured_package_version(),
        "backend_module_origin": str((runtime or {}).get("backend_module_origin") or ""),
        "tesseract": tesseract_identity,
        # ``all`` is explicit so the identity is self-describing and a future
        # partial result can never satisfy a request for the whole document.
        "page_numbers": _normalized_ocr_page_numbers(page_numbers) or "all",
    }


def _unstructured_ocr_cache_path(cache_dir, identity: dict) -> Path | None:
    if not cache_dir:
        return None
    try:
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        key = hashlib.sha256(encoded).hexdigest()
        root = Path(cache_dir)
        return root / f"unstructured-ocr-{key}.json"
    except (OSError, TypeError, ValueError):
        return None


def load_unstructured_ocr_cache(pdf_path: Path, strategy: str, runtime: dict, cache_dir=None, page_numbers=None):
    """Load a validated local OCR cache entry, or return ``None``.

    Corrupt/incomplete cache files are ignored rather than turning an ordinary
    extraction into a failure.  The caller can simply re-run OCR and replace
    the entry atomically.
    """
    if not cache_dir:
        return None
    try:
        requested_pages = _normalized_ocr_page_numbers(page_numbers)
        identity = _unstructured_ocr_cache_identity(pdf_path, strategy, runtime, requested_pages)
        path = _unstructured_ocr_cache_path(cache_dir, identity)
        if not path or not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("identity") != identity:
            return None
        pages = payload.get("pages")
        element_rows = payload.get("element_rows")
        page_count = int(payload.get("page_count") or 0)
        if not isinstance(pages, list) or not isinstance(element_rows, list) or page_count <= 0:
            return None
        observed_pages = [int(row.get("page") or 0) for row in pages if isinstance(row, dict)]
        expected_pages = requested_pages or list(range(1, page_count + 1))
        if observed_pages != expected_pages:
            return None
        for page in pages:
            page["unstructured_execution"] = {
                "mode": "persistent_ocr_cache_hit",
                "requested_workers": 0,
                "actual_workers": 0,
                "strategy": str(strategy or "").casefold(),
                "cache_path": str(path),
                "page_scope": "targeted_visual_text_pages" if requested_pages else "whole_document",
                "targeted_page_numbers": requested_pages or [],
            }
        return pages, page_count, element_rows
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def save_unstructured_ocr_cache(
    pdf_path: Path,
    strategy: str,
    runtime: dict,
    cache_dir,
    pages,
    page_count,
    element_rows,
    page_numbers=None,
):
    """Persist a complete OCR result atomically and return its local path."""
    if not cache_dir or not pages or int(page_count or 0) <= 0:
        return ""
    try:
        requested_pages = _normalized_ocr_page_numbers(page_numbers, page_count)
        observed_pages = [int(row.get("page") or 0) for row in pages if isinstance(row, dict)]
        expected_pages = requested_pages or list(range(1, int(page_count) + 1))
        if observed_pages != expected_pages:
            return ""
        identity = _unstructured_ocr_cache_identity(pdf_path, strategy, runtime, requested_pages)
        path = _unstructured_ocr_cache_path(cache_dir, identity)
        if not path:
            return ""
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "identity": identity,
            "pages": pages,
            "page_count": int(page_count),
            "element_rows": element_rows,
            "written_at": time.time(),
        }
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(json.dumps(payload, ensure_ascii=False))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return str(path)
    except (OSError, TypeError, ValueError):
        return ""


def _pymupdf4llm_one_page(pdf_path_text: str, page_index: int, ocr_dpi: int | None):
    """Worker-process entry point. It must stay module-level for Windows spawn."""
    pymupdf4llm = import_optional_backend("pymupdf4llm")
    options = {"pages": [page_index], "page_chunks": True}
    if ocr_dpi:
        options["ocr_dpi"] = ocr_dpi
    chunks = pymupdf4llm.to_markdown(pdf_path_text, **options)
    if len(chunks) != 1:
        raise RuntimeError(f"Expected one OCR chunk for page {page_index + 1}, received {len(chunks)}")
    chunk = chunks[0]
    text = chunk.get("text", "") if isinstance(chunk, dict) else str(chunk)
    return {"page": page_index + 1, "text": text, "kind": "markdown_page"}


class Pymupdf4llmWorkerIsolationError(RuntimeError):
    """An isolated native page worker cannot be safely replayed in its parent."""


class Pymupdf4llmWorkerUnresponsiveError(Pymupdf4llmWorkerIsolationError):
    """An isolated page worker stopped reporting liveness, not merely speed."""


def _pymupdf4llm_activity_paths(activity_path: Path) -> tuple[Path, Path]:
    """Return independent heartbeat slots for one isolated page worker.

    Windows can temporarily refuse a replacement while an observer has one
    JSON handle open.  A second slot keeps optional liveness telemetry from
    becoming a single-file failure point; extraction results remain the only
    authority for page content.
    """
    return (
        activity_path,
        activity_path.with_name(f"{activity_path.stem}.heartbeat{activity_path.suffix}"),
    )


def _pymupdf4llm_one_page_observed(
    pdf_path_text: str,
    page_index: int,
    ocr_dpi: int | None,
    activity_path_text: str,
):
    """Run one page while independently reporting its exact active phase."""
    activity_paths = _pymupdf4llm_activity_paths(Path(activity_path_text))
    stop = threading.Event()
    write_lock = threading.Lock()

    def write_activity(phase):
        payload = {
            "pid": os.getpid(),
            "page": page_index + 1,
            "phase": str(phase),
            "updated_at_epoch": time.time(),
        }
        with write_lock:
            failures: list[OSError] = []
            for activity_path in activity_paths:
                temporary = activity_path.with_suffix(f".{os.getpid()}.tmp")
                try:
                    temporary.write_text(json.dumps(payload), encoding="utf-8")
                    os.replace(temporary, activity_path)
                    return
                except OSError as exc:
                    failures.append(exc)
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass
            if failures:
                raise failures[-1]

    write_activity("starting_page_backend")

    def heartbeat():
        while not stop.wait(PYMUPDF4LLM_WORKER_HEARTBEAT_SECONDS):
            try:
                write_activity("extracting_page_with_pymupdf4llm")
            except OSError:
                # The result/exception remains authoritative. A transient
                # activity-file problem must not corrupt extracted text.
                pass

    thread = threading.Thread(
        target=heartbeat,
        name=f"pymupdf4llm-page-{page_index + 1}-activity",
        daemon=True,
    )
    thread.start()
    try:
        result = _pymupdf4llm_one_page(pdf_path_text, page_index, ocr_dpi)
        try:
            write_activity("page_complete")
        except OSError:
            # The extracted page is authoritative. On Windows the parent can
            # briefly hold the heartbeat JSON open while reading it, causing
            # os.replace() to report a sharing violation. Losing this optional
            # final telemetry update must not discard a completed extraction
            # and replay the whole document through the sequential fallback.
            pass
        return result
    finally:
        stop.set()
        thread.join(timeout=1.0)


def _parallel_pymupdf4llm_pages(
    pdf_path: Path,
    page_count: int,
    ocr_dpi: int | None,
    worker_count: int,
    progress_callback=None,
):
    """Extract ordered pages with bounded, activity-observed process workers.

    At most one page is queued per worker. There is deliberately no page or
    document duration limit. A pool is retired only when a submitted worker
    stops refreshing the independent activity record, which distinguishes a
    slow page from an unresponsive native process.
    """
    pages = []
    active_workers = min(worker_count, page_count)
    with tempfile.TemporaryDirectory(prefix="rag-pymupdf4llm-pages-") as activity_dir:
        executor = ProcessPoolExecutor(
            max_workers=active_workers,
            mp_context=get_context("spawn"),
        )
        terminated = False
        pending = {}
        submitted_at = {}
        activity_paths = {}
        next_page_index = 0
        last_activity_report = 0.0

        def submit_next_page():
            nonlocal next_page_index
            page_index = next_page_index
            activity_path = Path(activity_dir) / f"page-{page_index + 1:05d}.json"
            future = executor.submit(
                _pymupdf4llm_one_page_observed,
                str(pdf_path),
                page_index,
                ocr_dpi,
                str(activity_path),
            )
            pending[future] = page_index
            submitted_at[future] = time.monotonic()
            activity_paths[future] = _pymupdf4llm_activity_paths(activity_path)
            next_page_index += 1

        try:
            if callable(progress_callback):
                progress_callback(0, page_count)
            while len(pending) < active_workers and next_page_index < page_count:
                submit_next_page()
            while pending:
                done, _ = wait(set(pending), timeout=0.5, return_when=FIRST_COMPLETED)
                for future in done:
                    page_index = pending.pop(future)
                    submitted_at.pop(future, None)
                    activity_paths.pop(future, None)
                    try:
                        pages.append(future.result())
                    except BrokenProcessPool as exc:
                        _terminate_unstructured_executor(executor)
                        terminated = True
                        raise Pymupdf4llmWorkerIsolationError(
                            f"The isolated PyMuPDF4LLM worker process stopped on PDF page "
                            f"{page_index + 1}. The same native call was not replayed in the parent."
                        ) from exc
                    except Exception as exc:
                        _terminate_unstructured_executor(executor)
                        terminated = True
                        raise RuntimeError(
                            f"PyMuPDF4LLM failed for PDF page {page_index + 1}: "
                            f"{type(exc).__name__}: {exc}"
                        ) from exc
                    if callable(progress_callback):
                        progress_callback(len(pages), page_count)
                    while len(pending) < active_workers and next_page_index < page_count:
                        submit_next_page()

                now_epoch = time.time()
                now_monotonic = time.monotonic()
                unresponsive = []
                active_page_phases = []
                for future, page_index in pending.items():
                    paths = activity_paths[future]
                    last_activity = None
                    newest_activity = None
                    for path in paths:
                        try:
                            activity = json.loads(path.read_text(encoding="utf-8"))
                            observed_at = float(activity.get("updated_at_epoch") or 0.0)
                            if observed_at and (last_activity is None or observed_at > last_activity):
                                newest_activity = activity
                                last_activity = observed_at
                        except (OSError, TypeError, ValueError, json.JSONDecodeError):
                            pass
                    if newest_activity is not None:
                        active_page_phases.append({
                            "page": int(newest_activity.get("page") or page_index + 1),
                            "phase": str(newest_activity.get("phase") or "working"),
                        })
                    stale = (
                        now_epoch - last_activity
                        if last_activity
                        else now_monotonic - submitted_at[future]
                    )
                    if stale > PYMUPDF4LLM_WORKER_STALE_HEARTBEAT_SECONDS:
                        unresponsive.append(page_index + 1)
                if (
                    callable(progress_callback)
                    and active_page_phases
                    and now_monotonic - last_activity_report >= PYMUPDF4LLM_WORKER_HEARTBEAT_SECONDS
                ):
                    activity = {
                        "active_pages": sorted(active_page_phases, key=lambda row: row["page"]),
                        "liveness": "worker_heartbeats_current",
                    }
                    try:
                        parameters = inspect.signature(progress_callback).parameters.values()
                        accepts_activity = any(
                            parameter.name == "activity"
                            or parameter.kind == inspect.Parameter.VAR_KEYWORD
                            for parameter in parameters
                        )
                    except (TypeError, ValueError):
                        accepts_activity = False
                    if accepts_activity:
                        progress_callback(len(pages), page_count, activity=activity)
                    else:
                        progress_callback(len(pages), page_count)
                    last_activity_report = now_monotonic
                if unresponsive:
                    _terminate_unstructured_executor(executor)
                    terminated = True
                    raise Pymupdf4llmWorkerUnresponsiveError(
                        "PyMuPDF4LLM page worker liveness stopped for PDF page(s) "
                        + ", ".join(str(value) for value in sorted(unresponsive))
                        + ". Only the isolated page-worker pool was terminated."
                    )
        finally:
            if not terminated:
                executor.shutdown(wait=True, cancel_futures=True)
    pages.sort(key=lambda row: int(row["page"]))
    expected_pages = list(range(1, page_count + 1))
    observed_pages = [int(row["page"]) for row in pages]
    if observed_pages != expected_pages:
        raise RuntimeError(f"Parallel OCR page coverage mismatch: expected {expected_pages}, observed {observed_pages}")
    return pages


def _annotate_pymupdf4llm_execution(pages, *, requested_workers, actual_workers, mode, fallback_reason=""):
    """Attach one immutable execution record to each extracted page.

    The public extractor return shape remains ``(pages, page_count)`` for all
    existing callers.  Keeping the execution evidence on the page rows lets
    the preparation pipeline persist what actually happened without guessing
    from the current environment after extraction has finished.
    """
    evidence = {
        "requested_workers": int(requested_workers),
        "actual_workers": int(actual_workers),
        "mode": str(mode),
        "fallback_reason": str(fallback_reason or ""),
    }
    for page in pages:
        page["pymupdf4llm_execution"] = dict(evidence)
    return pages


def pymupdf4llm_execution_evidence(pages):
    """Return actual execution evidence preserved by the PyMuPDF4LLM extractor."""
    for page in pages or []:
        evidence = page.get("pymupdf4llm_execution") if isinstance(page, dict) else None
        if isinstance(evidence, dict):
            return dict(evidence)
    return {
        "requested_workers": 0,
        "actual_workers": 0,
        "mode": "not_recorded",
        "fallback_reason": "",
    }


def get_pages_with_pymupdf4llm(pdf_path: Path, progress_callback=None):
    try:
        pymupdf4llm = import_optional_backend("pymupdf4llm")
    except ImportError as exc:
        raise RuntimeError(
            "PyMuPDF4LLM is not installed. Run: python -m pip install --user pymupdf4llm"
        ) from exc

    # PyMuPDF / Tesseract discovers trained data from TESSDATA_PREFIX. Do not
    # rely on an earlier Unstructured capability probe having happened to set
    # it: direct PyMuPDF4LLM calls otherwise degrade scan-only PDFs to image
    # placeholders. A photographed-PDF A/B recovered slightly more text at
    # 200 DPI than 300 DPI while taking about 11% less time, so make that
    # measured setting explicit when OCR is available.
    ocr_runtime = ensure_tesseract_runtime()
    markdown_options: dict[str, Any] = {"page_chunks": True}
    if ocr_runtime.get("available"):
        markdown_options["ocr_dpi"] = PYMUPDF4LLM_OCR_DPI
    worker_count = pymupdf4llm_ocr_page_workers() if ocr_runtime.get("available") else 1
    if worker_count > 1:
        with fitz.open(pdf_path) as source_document:
            source_page_count = source_document.page_count
        if source_page_count > 1:
            try:
                pages = _parallel_pymupdf4llm_pages(
                    pdf_path,
                    source_page_count,
                    markdown_options.get("ocr_dpi"),
                    worker_count,
                    progress_callback=progress_callback,
                )
                LOGGER.info(
                    "Completed PyMuPDF4LLM OCR with %s isolated page workers across %s pages.",
                    min(worker_count, source_page_count),
                    source_page_count,
                )
                return (
                    _annotate_pymupdf4llm_execution(
                        pages,
                        requested_workers=worker_count,
                        actual_workers=min(worker_count, source_page_count),
                        mode="parallel_process_isolated",
                    ),
                    source_page_count,
                )
            except Pymupdf4llmWorkerIsolationError:
                # A liveness failure is not evidence that sequential execution
                # is safe; a crashed or unresponsive native call could hang or
                # terminate the owning worker if it were replayed there.
                # Let Automatic compare another extractor, or surface the
                # explicit-backend failure, without killing the parent tree.
                raise
            except Exception as exc:
                # Complete sequential extraction is safer than retaining a partly
                # observed parallel result. The fallback is visible in background
                # logs and keeps page provenance ordered and whole.
                LOGGER.warning(
                    "Parallel PyMuPDF4LLM OCR failed; falling back to sequential extraction: %s: %s",
                    type(exc).__name__,
                    exc,
                )
                fallback_reason = f"{type(exc).__name__}: {exc}"
        else:
            fallback_reason = ""
    else:
        fallback_reason = ""
    chunks = pymupdf4llm.to_markdown(str(pdf_path), **markdown_options)
    pages = []
    for index, chunk in enumerate(chunks, start=1):
        metadata = chunk.get("metadata", {}) if isinstance(chunk, dict) else {}
        page_num = int(metadata.get("page") or index)
        text = chunk.get("text", "") if isinstance(chunk, dict) else str(chunk)
        pages.append({"page": page_num, "text": text, "kind": "markdown_page"})

    page_count = max((p["page"] for p in pages), default=0)
    if callable(progress_callback) and page_count:
        # The single-call backend exposes no intermediate per-page callbacks;
        # report its measurable completion rather than fabricating a page rate.
        progress_callback(page_count, page_count)
    if not ocr_runtime.get("available"):
        return (
            _annotate_pymupdf4llm_execution(
                pages,
                requested_workers=0,
                actual_workers=0,
                mode="not_applicable_tesseract_unavailable",
            ),
            page_count,
        )
    return (
        _annotate_pymupdf4llm_execution(
            pages,
            requested_workers=worker_count,
            actual_workers=1,
            mode=(
                "sequential_after_parallel_fallback"
                if fallback_reason
                else "sequential_single_worker_or_page"
            ),
            fallback_reason=fallback_reason,
        ),
        page_count,
    )


def _unstructured_partition_elements(pdf_path: Path, resolved_strategy: str, runtime):
    """Run one Unstructured partition call after capability validation."""
    try:
        partition_pdf = import_optional_backend("unstructured.partition.pdf").partition_pdf
    except ImportError as exc:
        raise RuntimeError(
            "Unstructured PDF support is not available. Try: python -m pip install --user unstructured unstructured-inference"
        ) from exc

    if runtime["ocr_required"] and not runtime["tesseract_available"]:
        raise RuntimeError(
            "Tesseract OCR is required for the selected Unstructured strategy but was not found. "
            "Install Tesseract and ensure the runtime can resolve tesseract.exe."
        )
    return partition_pdf(
        filename=str(pdf_path),
        strategy=resolved_strategy,
        include_page_breaks=False,
        infer_table_structure=False,
    )


def _unstructured_is_symbol_noise(text):
    """Identify only OCR fragments that carry virtually no recoverable text.

    ``UncategorizedText`` is a layout bucket, not a quality grade. It can
    contain damaged but useful language, so its category alone must never
    discard it. This deliberately narrow test drops fragments such as
    ``\u201c4 \u2018cu: \u2122`` and ``{7`` while retaining imperfect phrases with several
    word-like pieces.
    """
    raw = str(text or "").strip()
    if not raw:
        return True
    letters = re.findall(r"[A-Za-z\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u00ff]", raw)
    wordlike = re.findall(r"[A-Za-z\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u00ff]{3,}", raw)
    nonspace = re.sub(r"\s+", "", raw)
    non_letter_or_space = re.findall(r"[^A-Za-z\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u00ff\s]", raw)
    # A readable word-like run is enough to preserve the raw OCR. Do not make
    # an English-dictionary judgment on damaged scholarship, names, poetry,
    # or non-English material.
    if wordlike:
        return False
    if not letters:
        return True
    # Two-letter scraps with several digits or symbols have no usable context.
    return len(letters) <= 2 and len(non_letter_or_space) >= 2 and len(nonspace) <= 12


def _unstructured_title_looks_like_heading(text):
    """Return whether a raw Unstructured Title looks like a real heading.

    Layout models commonly misclassify indented verse and prose continuations
    as titles. We retain the structural label only for compact, standalone,
    non-sentence-like text. Rejected titles remain in the transcript as
    NarrativeText; no substantive characters are discarded.
    """
    raw = str(text or "").strip()
    compact = normalize_text(raw)
    if not compact or _unstructured_is_symbol_noise(raw):
        return False
    words = re.findall(
        r"[A-Za-z\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u00ff]+(?:['\u2019-][A-Za-z\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u00ff]+)?",
        compact,
    )
    if not words or len(words) > 14 or len(compact) > 110:
        return False
    # Short genuine headings can be a name, a place, or a poem title, but a
    # run of two-character scraps or internally scrambled casing is not a
    # reliable structural title. Keep the characters as narrative text rather
    # than letting a weak layout prediction create a false heading boundary.
    if not any(len(word) >= 3 for word in words):
        return False
    if any(
        len(word) >= 3
        and not (word.islower() or word.isupper() or (word[:1].isupper() and word[1:].islower()))
        for word in words
    ):
        return False
    word_styles = {
        "upper" if word.isupper() else "lower" if word.islower() else "title"
        for word in words
        if len(word) >= 2
    }
    # OCR debris often has an implausible mixture such as ``av TINNY`` or
    # ``MATT pes``. A real mixed-case title is still retained as content, but
    # it does not receive a structural heading label without stronger evidence.
    if "upper" in word_styles and ("lower" in word_styles or "title" in word_styles):
        return False
    # Sentence punctuation anywhere is stronger evidence of prose or verse
    # than a layout label. A legitimate title with punctuation is retained as
    # text but conservatively loses its structural ``Title`` label; that is a
    # safer error than promoting a sentence into a heading.
    if any(mark in compact for mark in (".", "!", "?", ",", ";")):
        return False
    lowered = f" {compact.casefold()} "
    sentence_markers = (
        " i ", " i've ", " i'm ", " i'd ", " i'll ", " you ", " you're ",
        " my ", " your ", " our ", " their ", " we ", " we're ", " he ",
        " she's ", " they ", " they're ",
    )
    if any(marker in lowered for marker in sentence_markers):
        return False
    # More than one physical line is usually an OCR paragraph/verse fragment,
    # not a title. Keep line-break-free literary headings such as "LULA BELL".
    return len([line for line in raw.splitlines() if line.strip()]) <= 1


def _unstructured_prepared_category(source_category, text):
    if str(source_category or "").casefold() == "title" and not _unstructured_title_looks_like_heading(text):
        return "NarrativeText", "title_reclassified_as_narrative"
    return str(source_category or "UncategorizedText"), "retained"


def _unstructured_element_text(element):
    """Read vendor element text without letting one malformed element abort OCR.

    Some Unstructured element implementations expose a ``text`` value while
    their ``__str__`` implementation returns ``None`` for an empty visual
    region.  ``str(element)`` then raises ``TypeError``.  Empty OCR elements
    carry no retrieval text, so normalize that narrow vendor defect to an
    empty element and retain the rest of the source page/document.
    """
    try:
        raw_text = getattr(element, "text", None)
    except Exception:
        raw_text = None
    if isinstance(raw_text, str):
        return raw_text.strip()
    if raw_text is not None:
        try:
            return str(raw_text).strip()
        except (TypeError, ValueError):
            return ""
    try:
        return str(element).strip()
    except (TypeError, ValueError, AttributeError):
        return ""


def _unstructured_elements_to_pages(elements, *, page_offset=0, expected_page=None):
    by_page = defaultdict(list)
    element_rows = []
    for element_index, element in enumerate(elements, start=1):
        metadata = getattr(element, "metadata", None)
        page_num = getattr(metadata, "page_number", None) or 1
        page_num = int(page_num) + int(page_offset or 0)
        source_category = getattr(element, "category", None) or element.__class__.__name__
        text = _unstructured_element_text(element)
        if _unstructured_is_symbol_noise(text):
            category = "DroppedOCRNoise"
            decision = "dropped_symbol_noise"
        else:
            category, decision = _unstructured_prepared_category(source_category, text)
            by_page[page_num].append(f"[{category}] {text}")
        element_rows.append(
            {
                "element_index": element_index,
                "pdf_page": page_num,
                "category": category,
                "source_category": source_category,
                "content_decision": decision,
                "chars": len(text),
                "preview": normalize_text(text)[:250],
            }
        )
    if expected_page is not None:
        pages = [{
            "page": int(expected_page),
            "text": "\n\n".join(by_page.get(int(expected_page), [])),
            "kind": "unstructured_elements",
        }]
    else:
        pages = [
            {"page": page_num, "text": "\n\n".join(texts), "kind": "unstructured_elements"}
            for page_num, texts in sorted(by_page.items())
        ]
    return pages, element_rows


def photographed_page_ocr_regions(page, runtime):
    """Return OCR-ready reading regions for a visually identified photo page.

    The PDF page number remains the only citation page.  A future true-spread
    detector may yield more than one region, but each region keeps that same
    ``pdf_page`` and is distinguished by region metadata instead of a fake
    logical page. Rotation or uneven photographic border evidence is required,
    so ordinary flatbed scans and born-digital PDFs retain their existing path.
    """
    # A wrapper PDF can contain a short native label such as "Academic year"
    # while the actual letter is one large photographed image. That label is
    # not sufficient semantic content and must not suppress the dedicated
    # embedded-scan path below.
    if len(page.get_text("text").strip()) >= 200:
        return []
    tesseract = str((runtime or {}).get("tesseract_executable") or "").strip()
    if not tesseract or not Path(tesseract).exists():
        return []
    try:
        from PIL import Image, ImageOps, ImageStat
    except ImportError:
        return []

    pix = page.get_pixmap(matrix=fitz.Matrix(PHOTOGRAPHED_PAGE_OCR_DPI / 72, PHOTOGRAPHED_PAGE_OCR_DPI / 72), alpha=False)
    image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    embedded_fraction = embedded_scanned_image_fraction(page)
    if embedded_fraction:
        text = _ocr_photographed_crop(image, embedded_fraction, tesseract, ImageOps)
        if len(text) >= 80:
            return [{
                "text": text,
                "reading_region": "embedded_scanned_document",
                "reading_region_index": 1,
                "reading_region_count": 1,
                "source_column_index": 1,
                "ocr_method": "tesseract_embedded_scanned_document_crop",
                "annotations_excluded": "embedded_image_bounds_and_outer_margin_crop",
                "crop_fraction": list(embedded_fraction),
            }]
    if not photographed_page_visual_signal(page, image, ImageStat):
        return []
    width, height = image.size
    # A wide, image-only rotated page can be an open-book photograph. OCR both
    # halves separately only when each produces substantial prose; this keeps
    # ordinary single-page landscape scans on the existing full-page path.
    # Do not infer a spread from width alone. A neighbour-page sliver can OCR
    # as many tiny word fragments, while still not being a second source page.
    # A continuous dark fold is required before we even consider two regions.
    gutter_fraction = photographed_fold_gutter_fraction(image, ImageStat)
    spread_specs = photographed_spread_crop_specs(width, height, gutter_fraction)
    if spread_specs:
        crop_widths = [fraction[2] - fraction[0] for _name, _index, fraction in spread_specs]
        if min(crop_widths) / max(crop_widths) < .58:
            # Keep the complete page for now.  The narrow strip is only
            # excluded later if its fragments strongly match an adjacent
            # source page; see ``resolve_confirmed_neighbour_runovers``.
            narrow_index = crop_widths.index(min(crop_widths))
            dominant_index = 1 - narrow_index
            full_text = _ocr_photographed_crop(image, PHOTOGRAPHED_PAGE_CROP, tesseract, ImageOps)
            narrow_text = _ocr_photographed_crop(image, spread_specs[narrow_index][2], tesseract, ImageOps)
            dominant_text = _ocr_photographed_crop(image, spread_specs[dominant_index][2], tesseract, ImageOps)
            if len(full_text) >= 80 and len(narrow_text) >= 24 and len(dominant_text) >= 160:
                left, top, right, bottom = PHOTOGRAPHED_PAGE_CROP
                return [{
                    "text": full_text,
                    "reading_region": "full_page_inner_crop",
                    "reading_region_index": 1,
                    "reading_region_count": 1,
                    "source_column_index": 1,
                    "ocr_method": "tesseract_photographed_page_crop",
                    "annotations_excluded": "outer_margin_crop",
                    "crop_fraction": [left, top, right, bottom],
                    "_neighbour_page_runover_candidate": {
                        "side": spread_specs[narrow_index][0],
                        "narrow_text": narrow_text,
                        "dominant_text": dominant_text,
                        "dominant_crop_fraction": list(spread_specs[dominant_index][2]),
                    },
                }]
        region_texts = []
        for name, index, fraction in spread_specs:
            text = _ocr_photographed_crop(image, fraction, tesseract, ImageOps)
            region_texts.append(text)
        if keep_photographed_spread_regions(spread_specs, region_texts):
            return [
                {
                    "text": text,
                    "reading_region": name,
                    "reading_region_index": index,
                    "reading_region_count": 2,
                    "source_column_index": index,
                    "ocr_method": "tesseract_photographed_spread_crop",
                    "annotations_excluded": "outer_margin_crop",
                    "crop_fraction": list(fraction),
                }
                for (name, index, fraction), text in zip(spread_specs, region_texts)
            ]


    left, top, right, bottom = PHOTOGRAPHED_PAGE_CROP
    text = _ocr_photographed_crop(image, PHOTOGRAPHED_PAGE_CROP, tesseract, ImageOps)
    if len(text) < 80:
        return []
    return [
        {
            "text": text,
            "reading_region": "full_page_inner_crop",
            "reading_region_index": 1,
            "reading_region_count": 1,
            "source_column_index": 1,
            "ocr_method": "tesseract_photographed_page_crop",
            "annotations_excluded": "outer_margin_crop",
            "crop_fraction": [left, top, right, bottom],
        }
    ]


def embedded_scanned_image_fraction(page):
    """Return one safely substantial embedded image rectangle, if present.

    This recognizes a scanned letter pasted into a PDF page, rather than a
    normal born-digital report with an incidental illustration. The caller also
    requires an insubstantial native text layer before this can run.
    """
    page_area = max(float(page.rect.width * page.rect.height), 1.0)
    candidates = []
    for image_info in page.get_images(full=True):
        try:
            rects = page.get_image_rects(image_info[0])
        except Exception:
            continue
        for rect in rects:
            coverage = max(0.0, float(rect.width * rect.height)) / page_area
            if coverage >= .28:
                candidates.append((coverage, rect))
    if not candidates:
        return None
    _coverage, rect = max(candidates, key=lambda item: item[0])
    # Keep almost all source text while removing the raster edge, binder holes,
    # and scanner shadow. Do not use a broad PDF-page crop: this rect is the
    # original scanned document's own provenance boundary.
    inset_x = min(float(rect.width) * .012, 8.0)
    inset_y = min(float(rect.height) * .012, 8.0)
    cropped = fitz.Rect(rect.x0 + inset_x, rect.y0 + inset_y, rect.x1 - inset_x, rect.y1 - inset_y)
    return (
        round(max(0.0, cropped.x0 / page.rect.width), 4),
        round(max(0.0, cropped.y0 / page.rect.height), 4),
        round(min(1.0, cropped.x1 / page.rect.width), 4),
        round(min(1.0, cropped.y1 / page.rect.height), 4),
    )


def photographed_page_visual_signal(page, image, image_stat):
    """Identify a photographed page without treating every scan as a photo."""
    if int(getattr(page, "rotation", 0) or 0) % 360:
        return True
    gray = image.convert("L")
    width, height = gray.size
    edge = max(1, int(min(width, height) * .06))
    strips = [
        gray.crop((0, 0, edge, height)),
        gray.crop((width - edge, 0, width, height)),
        gray.crop((0, 0, width, edge)),
        gray.crop((0, height - edge, width, height)),
    ]
    means = [float(image_stat.Stat(strip).mean[0]) for strip in strips]
    # An off-axis photographed page usually has a dark/uneven edge from the
    # book, camera shadow, or background. Uniform light borders are the more
    # common flatbed/clean scan case and retain the standard OCR path.
    return min(means) < 240 and max(means) - min(means) >= 16


def photographed_fold_gutter_fraction(image, image_stat):
    """Locate a strong central fold shadow, returning ``None`` when uncertain."""
    gray = image.convert("L")
    width, height = gray.size
    if width / max(height, 1) < 1.22:
        return None
    top, bottom = int(height * .12), int(height * .90)
    stripe_half_width = max(2, int(width * .012))

    def stripe_mean(fraction):
        center = int(width * fraction)
        left = max(0, center - stripe_half_width)
        right = min(width, center + stripe_half_width)
        return float(image_stat.Stat(gray.crop((left, top, right, bottom))).mean[0])

    candidates = [(fraction, stripe_mean(fraction)) for fraction in [0.34 + step * .01 for step in range(33)]]
    fraction, darkness = min(candidates, key=lambda item: item[1])
    side_baseline = sorted([stripe_mean(.16), stripe_mean(.24), stripe_mean(.76), stripe_mean(.84)])[1:3]
    baseline = sum(side_baseline) / max(len(side_baseline), 1)
    # A true photographed fold is a vertically continuous shadow. Requiring
    # this contrast avoids splitting ordinary landscape pages or columns.
    if darkness > baseline - max(18.0, baseline * .10):
        return None
    return round(fraction, 3)


def photographed_spread_crop_specs(width, height, gutter_fraction=None):
    """Return two equal-sized source regions around a confirmed fold shadow."""
    if float(width) / max(float(height), 1.0) < 1.22 or gutter_fraction is None:
        return []
    gutter = min(.72, max(.28, float(gutter_fraction)))
    return [
        ("spread_left", 1, (0.04, 0.055, max(.05, gutter - .025), 0.95)),
        ("spread_right", 2, (min(.95, gutter + .025), 0.055, 0.96, 0.95)),
    ]


def keep_photographed_spread_regions(specs, region_texts):
    """Keep both regions only when doing so cannot discard plausible content."""
    if len(specs or []) != 2 or len(region_texts or []) != 2:
        return False
    widths = [float(fraction[2]) - float(fraction[0]) for _name, _index, fraction in specs]
    if min(widths) / max(widths) < .58:
        # A narrow side may be facing-page debris, but it can also contain a
        # valid index/letter column. No automatic deletion without cross-page
        # evidence; use full-page OCR instead.
        return False
    return all(len(str(text or "")) >= 240 for text in region_texts)


def neighbour_fragment_match(candidate_text, adjacent_text):
    """Require substantial distinctive overlap before discarding a strip."""
    stopwords = {
        "the", "and", "for", "are", "but", "not", "you", "all", "can", "had",
        "has", "was", "were", "that", "this", "with", "from", "have", "will",
    }
    fragments = sorted(
        {
            fragment
            for fragment in re.findall(r"\b[a-zA-Z][a-zA-Z'-]{2,}\b", str(candidate_text or "").casefold())
            if fragment not in stopwords
        }
    )
    neighbour_words = set(re.findall(r"\b[a-zA-Z][a-zA-Z'-]{4,}\b", str(adjacent_text or "").casefold()))
    if len(fragments) < 8 or not neighbour_words:
        return {"confirmed": False, "matched": 0, "fragments": len(fragments), "ratio": 0.0}

    def fragment_matches_word(fragment, word):
        if len(fragment) < 5:
            return len(word) >= 6 and word.startswith(fragment)
        return word.startswith(fragment) or fragment.startswith(word)

    matched = sum(
        1
        for fragment in fragments
        if any(fragment_matches_word(fragment, word) for word in neighbour_words)
    )
    ratio = matched / len(fragments)
    return {
        "confirmed": matched >= 8 and ratio >= .80,
        "matched": matched,
        "fragments": len(fragments),
        "ratio": round(ratio, 3),
    }


def resolve_confirmed_neighbour_runovers(pages):
    """Apply a nominated strip removal only after adjacent-page confirmation."""
    by_page = {int(row.get("page") or 0): row for row in pages or [] if isinstance(row, dict)}
    for page_number, row in sorted(by_page.items()):
        candidate = row.pop("_neighbour_page_runover_candidate", None)
        if not isinstance(candidate, dict):
            continue
        adjacent_text = "\n".join(
            str(by_page.get(neighbour, {}).get("text") or "")
            for neighbour in (page_number - 1, page_number + 1)
        )
        match = neighbour_fragment_match(candidate.get("narrow_text"), adjacent_text)
        preprocessing = row.setdefault("spread_preprocessing", {})
        preprocessing["neighbour_page_runover"] = {
            "side": candidate.get("side") or "",
            **match,
        }
        if not match["confirmed"]:
            preprocessing["neighbour_page_runover"]["decision"] = "ambiguous_retained"
            continue
        regions = row.get("reading_regions") or []
        if not regions:
            continue
        region = regions[0]
        region["text"] = str(candidate.get("dominant_text") or "")
        region["crop_fraction"] = list(candidate.get("dominant_crop_fraction") or region.get("crop_fraction") or [])
        region["annotations_excluded"] = "outer_margin_crop_and_confirmed_neighbour_page_runover"
        row["text"] = region["text"]
        preprocessing["neighbour_page_runover"]["decision"] = "confirmed_excluded"
    return pages


def _ocr_photographed_crop(image, fraction, tesseract, image_ops):
    """OCR one visual region, retaining only text suitable for preparation."""
    width, height = image.size
    left, top, right, bottom = fraction
    crop_box = (int(width * left), int(height * top), int(width * right), int(height * bottom))
    # Auto-contrast restores black glyphs laid over yellow highlighter while
    # keeping the source crop and page identity unchanged.
    cropped = image_ops.autocontrast(image.crop(crop_box).convert("L"))
    with tempfile.TemporaryDirectory(prefix="rag-photographed-page-") as temp_dir:
        image_path = Path(temp_dir) / "page.png"
        cropped.save(image_path)
        try:
            completed = subprocess.run(
                [tesseract, str(image_path), "stdout", "--psm", "4", "-l", "eng"],
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
    if completed.returncode != 0:
        return ""
    return clean_photographed_ocr_text(completed.stdout)


def clean_photographed_ocr_text(text):
    """Remove isolated OCR traces of handwritten marginal marks.

    This deliberately does not spell-correct or rewrite prose.  It only drops
    standalone pen strokes (for example ``|`` or ``')``) which are not part of
    a word and which the inner-margin crop cannot always exclude on a skewed
    photograph.
    """
    raw_text = str(text or "")
    # The pen stroke can land at the end of an OCR line, with the affected
    # word continuing on the following line. Remove that isolated sequence
    # before line-by-line cleanup so it cannot evade the local rule below.
    raw_text = re.sub(r"(?<!\w)['`]\s*\)\s*(?=[a-z])", "", raw_text)
    raw_text = re.sub(r"(?<=-)\s*[,|]\s*(?=[a-z])", "", raw_text)
    cleaned_lines = []
    for raw_line in raw_text.splitlines():
        if raw_line.strip() in {"|", "'", "`", ")", "(", "_", "-"}:
            continue
        line = re.sub(r"(?<!\w)\|(?!(?:\w|\|))", "", raw_line)
        line = re.sub(r"(?<!\w)['`]\s*\)(?=\s+[a-z])", "", line)
        line = re.sub(r"(?<=-)\s*[,|]\s*(?=[a-z])", "", line)
        cleaned_lines.append(line.rstrip())
    return "\n".join(cleaned_lines).strip()


def _photographed_page_result(source, page_index, runtime):
    """Build a page result when local crop OCR safely handles a photograph."""
    page = source.load_page(page_index)
    regions = photographed_page_ocr_regions(page, runtime)
    if not regions:
        return None
    page_number = page_index + 1
    runover_candidate = regions[0].pop("_neighbour_page_runover_candidate", None)
    page_row = {
        "page": page_number,
        "text": "\n\n".join(region["text"] for region in regions),
        "kind": "photographed_page_crop_ocr",
        "reading_regions": regions,
        "spread_preprocessing": {
            "status": "applied",
            "path": "photographed_page_inner_crop",
            "source_pdf_page": page_number,
            "logical_page_preserved": True,
            "annotations_excluded": "outer_margin_crop",
        },
    }
    if runover_candidate:
        page_row["_neighbour_page_runover_candidate"] = runover_candidate
    return {
        "page": page_number,
        "page_row": page_row,
        "element_rows": [
            {
                "element_index": 1,
                "pdf_page": page_number,
                "category": "PhotographedPageCropOCR",
                "chars": len(regions[0]["text"]),
                "preview": normalize_text(regions[0]["text"])[:250],
            }
        ],
    }


def _unstructured_one_page(pdf_path_text: str, page_index: int, strategy: str, scratch_dir: str, runtime_probe):
    """Windows-spawn-safe Unstructured OCR worker for one source PDF page."""
    # Two-to-four OCR workers must not each let Tesseract allocate its normal
    # multi-thread budget. A caller can override this deliberately, but the
    # conservative default prevents desktop-wide CPU oversubscription.
    os.environ.setdefault("OMP_THREAD_LIMIT", "1")
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    source = fitz.open(pdf_path_text)
    try:
        runtime = dict(runtime_probe or unstructured_runtime_status(strategy))
        photographed_result = _photographed_page_result(source, page_index, runtime)
        if photographed_result is not None:
            return photographed_result
        one_page = fitz.open()
        try:
            one_page.insert_pdf(source, from_page=page_index, to_page=page_index)
            page_path = Path(scratch_dir) / f"page-{page_index + 1:05d}.pdf"
            one_page.save(page_path)
        finally:
            one_page.close()
    finally:
        source.close()
    elements = _unstructured_partition_elements(page_path, strategy, runtime)
    pages, element_rows = _unstructured_elements_to_pages(
        elements,
        page_offset=page_index,
        expected_page=page_index + 1,
    )
    return {"page": page_index + 1, "page_row": pages[0], "element_rows": element_rows}


def _terminate_unstructured_executor(executor):
    """Stop a timed-out native OCR pool without waiting for a stuck worker."""
    terminate = getattr(executor, "terminate_workers", None)
    if callable(terminate):
        terminate()
        return
    # Python <3.14 cannot forcibly end a running ProcessPool future through
    # its public API.  Cancelling pending work still lets the caller return a
    # durable review outcome instead of blocking on unstarted pages.
    executor.shutdown(wait=False, cancel_futures=True)


def _parallel_unstructured_ocr_pages(
    pdf_path: Path,
    page_count: int,
    strategy: str,
    worker_count: int,
    runtime_probe,
    page_numbers=None,
    page_timeout_seconds=None,
    progress_callback=None,
):
    """Extract OCR pages in isolated processes and reassemble source order.

    Each submitted page has a separate deadline.  A deadline breach terminates
    the whole process pool: keeping sibling workers alive after a native OCR
    hang is unsafe, and partial page output must not be silently assembled.
    """
    timeout_seconds = int(page_timeout_seconds or unstructured_ocr_page_timeout_seconds())
    requested_pages = _normalized_ocr_page_numbers(page_numbers, page_count)
    expected_pages = requested_pages or list(range(1, page_count + 1))
    if not expected_pages:
        raise RuntimeError("Unstructured OCR received no valid source pages.")
    with tempfile.TemporaryDirectory(prefix="rag-unstructured-ocr-") as scratch_dir:
        pages = []
        element_rows = []
        # Explicit ``spawn`` avoids inheriting a partially initialized ONNX or
        # Tesseract runtime. It is Windows' default and is safer cross-platform
        # for optional native OCR dependencies.
        active_worker_count = min(worker_count, len(expected_pages))
        executor = ProcessPoolExecutor(
            max_workers=active_worker_count,
            mp_context=get_context("spawn"),
        )
        terminated = False
        try:
            # Do not queue every page at once.  With a 663-page document and
            # two workers, the old code started the 90-second per-page clock
            # for hundreds of pages that had not even been handed to an OCR
            # worker yet.  Healthy queued pages could therefore be falsely
            # reported as timed out.  Keeping at most one active future per
            # worker makes the deadline describe actual OCR work, bounds
            # memory/IPC pressure, and provides a truthful completed-page
            # heartbeat for the parent process.
            pending = {}
            submitted_at = {}
            next_page_position = 0

            def submit_next_page():
                nonlocal next_page_position
                source_page_number = expected_pages[next_page_position]
                source_page_index = source_page_number - 1
                future = executor.submit(
                    _unstructured_one_page,
                    str(pdf_path),
                    source_page_index,
                    strategy,
                    scratch_dir,
                    dict(runtime_probe),
                )
                pending[future] = source_page_index
                submitted_at[future] = time.monotonic()
                next_page_position += 1

            while len(pending) < active_worker_count and next_page_position < len(expected_pages):
                submit_next_page()
            while pending:
                done, _ = wait(set(pending), timeout=0.25, return_when=FIRST_COMPLETED)
                for future in done:
                    page_index = pending.pop(future)
                    submitted_at.pop(future, None)
                    try:
                        result = future.result()
                    except Exception as exc:
                        # A native OCR worker failure should not leave its
                        # siblings running invisibly while the caller waits
                        # for the executor's normal shutdown.  Preserve the
                        # failing page in the error and stop the isolated
                        # worker pool immediately; the higher-level fallback
                        # policy can then make its own bounded decision.
                        _terminate_unstructured_executor(executor)
                        terminated = True
                        raise RuntimeError(
                            f"Unstructured OCR failed for PDF page {page_index + 1}: "
                            f"{type(exc).__name__}: {exc}"
                        ) from exc
                    pages.append(result["page_row"])
                    element_rows.extend(result["element_rows"])
                    if callable(progress_callback):
                        progress_callback(len(pages), len(expected_pages))
                    while len(pending) < active_worker_count and next_page_position < len(expected_pages):
                        submit_next_page()
                timed_out = [
                    pending[future] + 1
                    for future in pending
                    if time.monotonic() - submitted_at[future] > timeout_seconds
                ]
                if timed_out:
                    _terminate_unstructured_executor(executor)
                    terminated = True
                    pages_text = ", ".join(str(page) for page in sorted(timed_out))
                    raise TimeoutError(
                        f"Unstructured OCR timed out after {timeout_seconds}s for PDF page(s) {pages_text}. "
                        "The isolated OCR workers were stopped; rerun after reviewing the OCR runtime or use a smaller PDF."
                    )
        finally:
            if not terminated:
                executor.shutdown(wait=True, cancel_futures=True)
    pages.sort(key=lambda row: int(row["page"]))
    element_rows.sort(key=lambda row: (int(row["pdf_page"]), int(row["element_index"])))
    observed_pages = [int(row["page"]) for row in pages]
    if observed_pages != expected_pages:
        raise RuntimeError(
            f"Parallel Unstructured OCR page coverage mismatch: expected {expected_pages}, observed {observed_pages}"
        )
    resolve_confirmed_neighbour_runovers(pages)
    for page in pages:
        page["unstructured_execution"] = {
            "mode": "isolated_parallel_pages",
            "requested_workers": int(worker_count),
            "actual_workers": min(int(worker_count), len(expected_pages)),
            "strategy": strategy,
            "page_scope": "targeted_visual_text_pages" if requested_pages else "whole_document",
            "targeted_page_numbers": requested_pages or [],
        }
    return pages, page_count, element_rows


def unstructured_execution_evidence(pages):
    """Return persisted execution evidence without inferring worker behavior."""
    for page in pages or []:
        evidence = page.get("unstructured_execution") if isinstance(page, dict) else None
        if isinstance(evidence, dict):
            return dict(evidence)
    return {
        "mode": "not_recorded",
        "requested_workers": 0,
        "actual_workers": 0,
        "strategy": "",
    }


def get_pages_with_unstructured(
    pdf_path: Path,
    strategy: str,
    runtime_probe=None,
    cache_dir=None,
    progress_callback=None,
    page_numbers=None,
):
    requested_strategy = (strategy or "auto").strip().casefold()
    resolved_strategy = "fast" if requested_strategy == "auto" else requested_strategy
    runtime = dict(
        unstructured_runtime_status(resolved_strategy)
        if runtime_probe is None
        else runtime_probe
    )
    # ``fast`` uses text extraction and is already cheap. Only OCR-capable
    # strategies receive the bounded per-page process lane. The parent still
    # owns candidate scoring, artifacts, AnythingLLM mutation, and progress.
    if resolved_strategy in {"hi_res", "ocr_only"} and pdf_path.exists():
        cached = load_unstructured_ocr_cache(
            pdf_path,
            resolved_strategy,
            runtime,
            cache_dir,
            page_numbers=page_numbers,
        )
        if cached is not None:
            if callable(progress_callback):
                completed_pages = len(cached[0]) if page_numbers is not None else int(cached[1])
                progress_callback(completed_pages, completed_pages)
            return cached
        with fitz.open(pdf_path) as document:
            source_page_count = len(document)
            target_page_numbers = _normalized_ocr_page_numbers(page_numbers, source_page_count)
            if page_numbers is not None and not target_page_numbers:
                raise RuntimeError("Unstructured OCR received no valid source pages.")
            photographed_result = (
                _photographed_page_result(document, 0, runtime)
                if source_page_count == 1 and target_page_numbers in (None, [1])
                else None
            )
        if photographed_result is not None:
            pages = [photographed_result["page_row"]]
            page_count = 1
            element_rows = photographed_result["element_rows"]
            resolve_confirmed_neighbour_runovers(pages)
            pages[0]["unstructured_execution"] = {
                "mode": "direct_single_photographed_page",
                "requested_workers": 1,
                "actual_workers": 1,
                "strategy": resolved_strategy,
            }
            cache_path = save_unstructured_ocr_cache(
                pdf_path,
                resolved_strategy,
                runtime,
                cache_dir,
                pages,
                page_count,
                element_rows,
                page_numbers=target_page_numbers,
            )
            if cache_path:
                pages[0]["unstructured_execution"]["cache_path"] = cache_path
            if callable(progress_callback):
                progress_callback(1, 1)
            return pages, page_count, element_rows
        workers = unstructured_ocr_page_workers()
        if source_page_count > 1:
            pages, page_count, element_rows = _parallel_unstructured_ocr_pages(
                pdf_path,
                source_page_count,
                resolved_strategy,
                workers,
                runtime,
                page_numbers=target_page_numbers,
                progress_callback=progress_callback,
            )
            cache_path = save_unstructured_ocr_cache(
                pdf_path,
                resolved_strategy,
                runtime,
                cache_dir,
                pages,
                page_count,
                element_rows,
                page_numbers=target_page_numbers,
            )
            if cache_path:
                for page in pages:
                    page["unstructured_execution"]["cache_path"] = cache_path
            return pages, page_count, element_rows

    elements = _unstructured_partition_elements(pdf_path, resolved_strategy, runtime)
    pages, element_rows = _unstructured_elements_to_pages(elements)
    page_count = max((int(row["page"]) for row in pages), default=0)
    for page in pages:
        page["unstructured_execution"] = {
            "mode": "sequential_document",
            "requested_workers": 1,
            "actual_workers": 1,
            "strategy": resolved_strategy,
        }
    if callable(progress_callback) and page_count:
        # The third-party whole-document call has no per-page callback. This
        # is an honest terminal checkpoint; OCR-capable multi-page work uses
        # the isolated page lane above and reports every finished page.
        progress_callback(page_count, page_count)
    if resolved_strategy in {"hi_res", "ocr_only"}:
        cache_path = save_unstructured_ocr_cache(
            pdf_path, resolved_strategy, runtime, cache_dir, pages, page_count, element_rows
        )
        if cache_path:
            for page in pages:
                page["unstructured_execution"]["cache_path"] = cache_path
    return pages, page_count, element_rows


def get_backend_pages(
    pdf_path: Path,
    backend: str,
    unstructured_strategy: str,
    unstructured_runtime_probe=None,
    unstructured_cache_dir=None,
    progress_callback=None,
    unstructured_page_numbers=None,
):
    backend_key = backend.lower()
    if backend_key == "pymupdf":
        pages, page_count = get_pages_with_pymupdf(pdf_path, progress_callback=progress_callback)
        return pages, page_count, []
    if backend_key == "pymupdf4llm":
        pages, page_count = get_pages_with_pymupdf4llm(pdf_path, progress_callback=progress_callback)
        return pages, page_count, []
    if backend_key == "unstructured":
        result = get_pages_with_unstructured(
            pdf_path,
            unstructured_strategy,
            runtime_probe=unstructured_runtime_probe,
            cache_dir=unstructured_cache_dir,
            progress_callback=progress_callback,
            page_numbers=unstructured_page_numbers,
        )
        return result
    raise ValueError(f"Unsupported backend: {backend}")


def extract_pdf(args):
    pdf_path = Path(args.pdf)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    base = safe_stem(args.output_base_name or pdf_path.stem)
    backend_suffix = safe_stem(args.backend.lower())
    out_text = out_dir / f"{base}-{backend_suffix}-extract.txt"
    page_report = out_dir / f"{base}-{backend_suffix}-page-report.csv"
    validation_report = out_dir / f"{base}-{backend_suffix}-extract-validation.csv"
    element_report = out_dir / f"{base}-{backend_suffix}-elements.csv"

    pages, page_count, element_rows = get_backend_pages(
        pdf_path, args.backend, args.unstructured_strategy
    )
    parts = []
    rows = []

    for page_info in pages:
        page_num = page_info["page"]
        raw = page_info["text"]
        clean = normalize_text(raw)
        parts.append(f"\n\n[PDF_PAGE {page_num}]\n{raw}")
        rows.append(
            {
                "pdf_page": page_num,
                "kind": page_info.get("kind", ""),
                "raw_chars": len(raw),
                "normalized_chars": len(clean),
                "words_approx": len(clean.split()),
                "preview": clean[:300],
            }
        )

    full = "".join(parts)
    out_text.write_text(full, encoding="utf-8")

    with page_report.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "pdf_page",
                "kind",
                "raw_chars",
                "normalized_chars",
                "words_approx",
                "preview",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    if element_rows:
        with element_report.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "element_index", "pdf_page", "category", "source_category",
                    "content_decision", "chars", "preview",
                ],
            )
            writer.writeheader()
            writer.writerows(element_rows)

    print("Input PDF:", pdf_path)
    print("Backend:", args.backend)
    print("Output text:", out_text)
    print("Page report:", page_report)
    if element_rows:
        print("Element report:", element_report)
    print("PDF pages:", page_count)
    print("Extracted characters:", len(full))
    print("Extracted words approx:", len(full.split()))

    if args.validation_phrase:
        print()
        print("Validation:")
        write_validation_report(full, args.validation_phrase, validation_report)
        print("Validation report:", validation_report)


def segment_pdf(args):
    pdf_path = Path(args.pdf)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    base = safe_stem(args.output_base_name or pdf_path.stem)
    backend_suffix = safe_stem(args.backend.lower())
    label = clean_label(args.source_label or pdf_path.stem)
    out_text = out_dir / f"{base}-{backend_suffix}-page-segmented.txt"
    report_csv = out_dir / f"{base}-{backend_suffix}-page-segmented-report.csv"
    validation_report = out_dir / f"{base}-{backend_suffix}-validation.csv"
    element_report = out_dir / f"{base}-{backend_suffix}-elements.csv"
    stop_regex = make_stop_regex(args.stop_heading)

    pages, page_count, element_rows = get_backend_pages(
        pdf_path, args.backend, args.unstructured_strategy
    )
    detected_stop = None
    effective_stop_after_page = args.stop_after_page
    effective_stop_headings = args.stop_heading

    if getattr(args, "auto_detect_end_sections", False):
        candidate_headings = args.stop_heading or DEFAULT_END_SECTION_HEADINGS
        detected_stop = detect_end_section_start(pages, candidate_headings)
        if detected_stop:
            effective_stop_after_page = max(0, detected_stop["page"] - 1)
            effective_stop_headings = [detected_stop["heading"]]
            stop_regex = make_stop_regex(effective_stop_headings)

    all_segments = []
    report_rows = []
    segment_id = 1
    stopped = False

    for page_info in pages:
        page_num = page_info["page"]
        raw = page_info["text"]
        clean = normalize_text(raw)
        row = {
            "pdf_page": page_num,
            "kind": page_info.get("kind", ""),
            "status": "",
            "chars": len(clean),
            "segments": 0,
            "preview": clean[:250],
        }

        if page_num < args.start_page:
            row["status"] = "skipped_before_start_page"
            report_rows.append(row)
            continue

        if (
            effective_stop_after_page > 0
            and page_num > effective_stop_after_page
            and stop_regex is not None
            and stop_regex.match(clean)
        ):
            row["status"] = "stop_heading_detected"
            report_rows.append(row)
            stopped = True
            break

        if len(clean) < args.min_page_chars:
            row["status"] = "skipped_too_short"
            report_rows.append(row)
            continue

        page_segments = split_into_segments(clean, args.segment_chars, args.min_boundary_chars)
        for segment in page_segments:
            header = (
                f"[{label} | BACKEND {args.backend} | PDF_PAGE {page_num} | "
                f"SEGMENT {segment_id:05d}]"
            )
            all_segments.append(header + "\n" + segment)
            segment_id += 1

        row["status"] = "included"
        row["segments"] = len(page_segments)
        report_rows.append(row)

    full = "\n\n".join(all_segments)
    out_text.write_text(full, encoding="utf-8")

    with report_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["pdf_page", "kind", "status", "chars", "segments", "preview"],
        )
        writer.writeheader()
        writer.writerows(report_rows)

    if element_rows:
        with element_report.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["element_index", "pdf_page", "category", "chars", "preview"],
            )
            writer.writeheader()
            writer.writerows(element_rows)

    print("Input PDF:", pdf_path)
    print("Backend:", args.backend)
    print("Output text:", out_text)
    print("Report CSV:", report_csv)
    if element_rows:
        print("Element report:", element_report)
    print("PDF pages:", page_count)
    print("Start page:", args.start_page)
    if detected_stop:
        print(
            "Auto-detected end section:",
            detected_stop["heading"],
            "at PDF page",
            detected_stop["page"],
        )
    print("End-section search starts after page:", effective_stop_after_page)
    print("Segment chars:", args.segment_chars)
    print("Segments written:", len(all_segments))
    print("Characters written:", len(full))
    print("Stopped before notes/bibliography/index:", stopped)

    if args.validation_phrase:
        print()
        print("Validation:")
        write_validation_report(full, args.validation_phrase, validation_report)
        print("Validation report:", validation_report)


def add_common_args(parser):
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--output-base-name", default="")
    parser.add_argument("--validation-phrase", action="append", default=[])
    parser.add_argument(
        "--backend",
        choices=["pymupdf", "pymupdf4llm", "unstructured"],
        default="pymupdf",
    )
    parser.add_argument("--unstructured-strategy", default="auto")


def main():
    parser = argparse.ArgumentParser(description="PDF extraction helpers for RAG ingestion.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract = subparsers.add_parser("extract-pdf", help="Export raw PyMuPDF text plus page markers.")
    add_common_args(extract)
    extract.set_defaults(func=extract_pdf)

    segment = subparsers.add_parser("segment-pdf", help="Export page-segmented RAG text.")
    add_common_args(segment)
    segment.add_argument("--source-label", default="")
    segment.add_argument("--start-page", type=int, default=1)
    segment.add_argument("--stop-after-page", type=int, default=0)
    segment.add_argument("--stop-heading", action="append", default=[])
    segment.add_argument("--auto-detect-end-sections", action="store_true")
    segment.add_argument("--segment-chars", type=int, default=650)
    segment.add_argument("--min-boundary-chars", type=int, default=250)
    segment.add_argument("--min-page-chars", type=int, default=40)
    segment.set_defaults(func=segment_pdf)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

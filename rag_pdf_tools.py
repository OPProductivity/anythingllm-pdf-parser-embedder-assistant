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
import math
import os
import re
import shutil
import site
import statistics
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
UNSTRUCTURED_OCR_PAGE_GROUP_SIZE_DEFAULT = 12
UNSTRUCTURED_OCR_PAGE_GROUP_SIZE_MAX = 32
# OCR page checkpoints are deliberately scoped to one run directory. Their
# schema protects a restarted worker inside that run from incompatible page
# data; unlike the retired shared OCR cache, a new run never consults them.
UNSTRUCTURED_OCR_CHECKPOINT_SCHEMA_VERSION = 1
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


def unstructured_ocr_page_group_size() -> int:
    """Return the bounded checkpoint group size for page-local OCR."""
    raw = os.environ.get(
        "RAG_PDF_UNSTRUCTURED_OCR_PAGE_GROUP_SIZE",
        str(UNSTRUCTURED_OCR_PAGE_GROUP_SIZE_DEFAULT),
    )
    try:
        requested = int(str(raw).strip())
    except (TypeError, ValueError):
        requested = UNSTRUCTURED_OCR_PAGE_GROUP_SIZE_DEFAULT
    return max(1, min(UNSTRUCTURED_OCR_PAGE_GROUP_SIZE_MAX, requested))


def _consecutive_ocr_page_groups(page_numbers, group_size=None):
    """Split ordered pages into bounded consecutive checkpoint groups."""
    cap = max(1, int(group_size or unstructured_ocr_page_group_size()))
    groups = []
    active = []
    for page_number in sorted(set(int(page) for page in page_numbers or [])):
        if active and (page_number != active[-1] + 1 or len(active) >= cap):
            groups.append(active)
            active = []
        active.append(page_number)
    if active:
        groups.append(active)
    return groups


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@lru_cache(maxsize=128)
def _file_sha256_for_version(path_text: str, size: int, mtime_ns: int) -> str:
    """Hash one immutable observed file version once per process."""
    return _file_sha256(Path(path_text))


def _versioned_file_sha256(path: Path) -> str:
    source = Path(path)
    stat = source.stat()
    return _file_sha256_for_version(str(source.resolve()), int(stat.st_size), int(stat.st_mtime_ns))


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


def _unstructured_ocr_checkpoint_identity(pdf_path: Path, strategy: str, runtime: dict, page_numbers=None) -> dict:
    """Return stable, non-sensitive identity fields for a run-local checkpoint.

    A checkpoint hit must never cross a source change, strategy change,
    package upgrade, or Tesseract executable change. The caller supplies a
    directory inside the current run, preventing reuse by independent runs.
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
        "schema_version": UNSTRUCTURED_OCR_CHECKPOINT_SCHEMA_VERSION,
        "source_sha256": _versioned_file_sha256(source),
        "strategy": str(strategy or "").casefold(),
        "unstructured_version": _unstructured_package_version(),
        "backend_module_origin": str((runtime or {}).get("backend_module_origin") or ""),
        "tesseract": tesseract_identity,
        # ``all`` is explicit so the identity is self-describing and a future
        # partial result can never satisfy a request for the whole document.
        "page_numbers": _normalized_ocr_page_numbers(page_numbers) or "all",
    }


def _unstructured_ocr_checkpoint_path(checkpoint_dir, identity: dict) -> Path | None:
    if not checkpoint_dir:
        return None
    try:
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        key = hashlib.sha256(encoded).hexdigest()
        root = Path(checkpoint_dir)
        return root / f"unstructured-page-checkpoint-{key}.json"
    except (OSError, TypeError, ValueError):
        return None


def load_unstructured_ocr_checkpoint(pdf_path: Path, strategy: str, runtime: dict, checkpoint_dir=None, page_numbers=None):
    """Load a validated OCR page checkpoint belonging to the current run.

    Corrupt/incomplete checkpoint files are ignored rather than turning an ordinary
    extraction into a failure.  The caller can simply re-run OCR and replace
    the entry atomically.
    """
    if not checkpoint_dir:
        return None
    try:
        requested_pages = _normalized_ocr_page_numbers(page_numbers)
        identity = _unstructured_ocr_checkpoint_identity(pdf_path, strategy, runtime, requested_pages)
        path = _unstructured_ocr_checkpoint_path(checkpoint_dir, identity)
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
                "mode": "run_local_ocr_checkpoint_hit",
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


def save_unstructured_ocr_checkpoint(
    pdf_path: Path,
    strategy: str,
    runtime: dict,
    checkpoint_dir,
    pages,
    page_count,
    element_rows,
    page_numbers=None,
):
    """Persist a complete run-local OCR page checkpoint atomically."""
    if not checkpoint_dir or not pages or int(page_count or 0) <= 0:
        return ""
    try:
        requested_pages = _normalized_ocr_page_numbers(page_numbers, page_count)
        observed_pages = [int(row.get("page") or 0) for row in pages if isinstance(row, dict)]
        expected_pages = requested_pages or list(range(1, int(page_count) + 1))
        if observed_pages != expected_pages:
            return ""
        identity = _unstructured_ocr_checkpoint_identity(pdf_path, strategy, runtime, requested_pages)
        path = _unstructured_ocr_checkpoint_path(checkpoint_dir, identity)
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
    try:
        chunks = pymupdf4llm.to_markdown(pdf_path_text, **options)
        geometry_recovery = None
    except ValueError as exc:
        if "rect is infinite or empty" not in str(exc).casefold():
            raise
        chunks, geometry_recovery = _pymupdf4llm_retry_without_invalid_page_annotations(
            pymupdf4llm,
            pdf_path_text,
            page_index,
            ocr_dpi,
        )
    if len(chunks) != 1:
        raise RuntimeError(f"Expected one OCR chunk for page {page_index + 1}, received {len(chunks)}")
    chunk = chunks[0]
    text = chunk.get("text", "") if isinstance(chunk, dict) else str(chunk)
    result = {"page": page_index + 1, "text": text, "kind": "markdown_page"}
    if geometry_recovery:
        result["pymupdf4llm_geometry_recovery"] = geometry_recovery
    return result


def _pymupdf4llm_retry_without_invalid_page_annotations(
    pymupdf4llm,
    pdf_path_text: str,
    page_index: int,
    ocr_dpi: int | None,
):
    """Retry one page from a temporary copy after removing impossible annotations.

    Some otherwise readable PDFs contain highlight rectangles at the signed
    32-bit coordinate limits. PyMuPDF4LLM rejects those rectangles before it
    can inspect the page. The source PDF is never changed: this recovery copies
    one physical page, removes only non-finite, empty, or million-point
    annotation rectangles, and makes exactly one retry for the failed page.
    The complete temporary copy is necessary because some annotations retain
    cross-object references when a single page is copied in isolation.
    """
    with tempfile.TemporaryDirectory(prefix="rag-pymupdf4llm-geometry-") as temp_dir:
        page_path = Path(temp_dir) / "sanitized-source.pdf"
        removed = []
        with fitz.open(pdf_path_text) as source:
            for source_page_index in range(source.page_count):
                copied_page = source[source_page_index]
                for annotation in list(copied_page.annots() or []):
                    rect = annotation.rect
                    values = (rect.x0, rect.y0, rect.x1, rect.y1)
                    invalid = (
                        rect.is_infinite
                        or rect.is_empty
                        or not all(math.isfinite(value) for value in values)
                        or max(abs(value) for value in values) > 1_000_000
                    )
                    if not invalid:
                        continue
                    removed.append({
                        "source_page": source_page_index + 1,
                        "type": str(annotation.type[1] or annotation.type[0]),
                        "rect": [float(value) for value in values],
                    })
                    copied_page.delete_annot(annotation)
            if not removed:
                raise ValueError(
                    "PyMuPDF4LLM reported invalid page geometry, but no invalid annotation was found."
                )
            source.save(page_path, garbage=1, deflate=True)
        options = {"pages": [page_index], "page_chunks": True}
        if ocr_dpi:
            options["ocr_dpi"] = ocr_dpi
        chunks = pymupdf4llm.to_markdown(str(page_path), **options)
    return chunks, {
        "status": "temporary_page_copy_succeeded",
        "source_page": page_index + 1,
        "removed_invalid_annotations": removed,
        "source_pdf_modified": False,
    }


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
    # Numbers are content too (table cells, percentages, dates). Keep simple
    # numeric expressions, but not arbitrary OCR debris such as ``{7``.
    if any(char.isdigit() for char in raw) and re.fullmatch(r"[\d\s.,%‰$€£¥+−–—\-/():]+", raw):
        return False
    if any(char.isalpha() and not re.match(r"[A-Za-zÀ-ÖØ-öø-ÿ]", char) for char in raw):
        return False
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


def photographed_page_ocr_regions(page, runtime, *, page_number=None):
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
    width, height = image.size
    # A wide, image-only rotated page can be an open-book photograph. OCR both
    # halves separately only when each produces substantial prose; this keeps
    # ordinary single-page landscape scans on the existing full-page path.
    # Do not infer a spread from width alone. A neighbour-page sliver can OCR
    # as many tiny word fragments, while still not being a second source page.
    # A continuous dark fold is required before we even consider two regions.
    gutter_fraction = photographed_fold_gutter_fraction(image, ImageStat)
    if gutter_fraction is None:
        gutter_fraction = photographed_sparse_spread_gutter(image, ImageStat)
    spread_specs = photographed_spread_crop_specs(width, height, gutter_fraction)
    spread_dpi = photographed_spread_render_dpi(image, spread_specs, ImageOps)
    if spread_dpi > PHOTOGRAPHED_PAGE_OCR_DPI:
        # Two pages share the raster: 144 DPI can silently omit small body
        # lines even with correct crops. Render once at 180 for this route,
        # rather than OCRing several candidates or changing ordinary scans.
        pix = page.get_pixmap(matrix=fitz.Matrix(spread_dpi / 72, spread_dpi / 72), alpha=False)
        image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    route_recovery = None
    portrait_sliver = photographed_portrait_neighbour_sliver(image, ImageOps) if not spread_specs else None
    if portrait_sliver:
        full_text = _ocr_photographed_crop(image, PHOTOGRAPHED_PAGE_CROP, tesseract, ImageOps)
        narrow_text = _ocr_photographed_crop(image, portrait_sliver["narrow_crop_fraction"], tesseract, ImageOps)
        dominant_text = _ocr_photographed_crop(image, portrait_sliver["dominant_crop_fraction"], tesseract, ImageOps)
        narrow_quality = photographed_ocr_text_quality(narrow_text)
        dominant_quality = photographed_ocr_text_quality(dominant_text)
        if len(full_text) >= 80 and narrow_quality["word_count"] >= 8 and dominant_quality["word_count"] >= 80:
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
                "ocr_route_recovery": {
                    "attempted": True,
                    "initial_route": "portrait_page_with_edge_fold",
                    "selected_route": "pending_neighbour_sliver_resolution",
                    "reason": "narrow_edge_text_separated_by_continuous_fold",
                    "region_word_counts": [narrow_quality["word_count"], dominant_quality["word_count"]],
                },
                "_neighbour_page_runover_candidate": {
                    "side": portrait_sliver["side"],
                    "narrow_text": narrow_text,
                    "dominant_text": dominant_text,
                    "dominant_crop_fraction": list(portrait_sliver["dominant_crop_fraction"]),
                    "geometry_confidence": "high",
                    "geometry_evidence": dict(portrait_sliver),
                },
            }]
    if spread_specs:
        precomputed_region_texts = {}
        precomputed_region_decisions = {}
        crop_widths = [fraction[2] - fraction[0] for _name, _index, fraction in spread_specs]
        if min(crop_widths) / max(crop_widths) < .58:
            # Keep the complete page for now.  The narrow strip is only
            # excluded later if its fragments strongly match an adjacent
            # source page; see ``resolve_confirmed_neighbour_runovers``.
            narrow_index = crop_widths.index(min(crop_widths))
            dominant_index = 1 - narrow_index
            full_text = _ocr_photographed_crop(image, PHOTOGRAPHED_PAGE_CROP, tesseract, ImageOps)
            precomputed_region_decisions[narrow_index] = {}
            precomputed_region_decisions[dominant_index] = {}
            narrow_text = _ocr_photographed_crop(image, spread_specs[narrow_index][2], tesseract, ImageOps,
                                               recognition_evidence=precomputed_region_decisions[narrow_index])
            dominant_text = _ocr_photographed_crop(image, spread_specs[dominant_index][2], tesseract, ImageOps,
                                                 recognition_evidence=precomputed_region_decisions[dominant_index])
            precomputed_region_texts[narrow_index] = narrow_text
            precomputed_region_texts[dominant_index] = dominant_text
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
                    "ocr_route_recovery": {
                        "attempted": True,
                        "initial_route": "confirmed_fold_spread_split",
                        "selected_route": "asymmetric_fold_full_page_preservation",
                        "reason": "narrow_fold_side_contains_plausible_text",
                        "region_word_counts": [
                            photographed_ocr_text_quality(narrow_text)["word_count"],
                            photographed_ocr_text_quality(dominant_text)["word_count"],
                        ],
                    },
                    "_neighbour_page_runover_candidate": {
                        "side": spread_specs[narrow_index][0],
                        "narrow_text": narrow_text,
                        "dominant_text": dominant_text,
                        "dominant_crop_fraction": list(spread_specs[dominant_index][2]),
                    },
                }]
        region_texts = []
        region_decisions = []
        resolved_spread_specs = []
        crop_adjustments = []
        for name, index, fraction in spread_specs:
            resolved_fraction, crop_adjustment = adaptive_ocr_crop_fraction(
                image, fraction, ImageOps, max_expand=.018
            )
            resolved_spread_specs.append((name, index, resolved_fraction))
            crop_adjustments.append(crop_adjustment)
        for region_position, (name, index, fraction) in enumerate(resolved_spread_specs):
            text = precomputed_region_texts.get(region_position)
            decision = precomputed_region_decisions.get(region_position, {})
            if text is None:
                text = _ocr_photographed_crop(image, fraction, tesseract, ImageOps,
                                             recognition_evidence=decision,
                                             enhance_annotated_prose=True)
            region_texts.append(text)
            region_decisions.append(decision)
        if keep_photographed_spread_regions(resolved_spread_specs, region_texts):
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
                    "crop_adjustment": crop_adjustment,
                    "render_dpi": spread_dpi,
                    "recognition_layout": dict(decision),
                }
                for (name, index, fraction), text, crop_adjustment, decision in zip(
                    resolved_spread_specs, region_texts, crop_adjustments, region_decisions
                )
            ]
        route_recovery = {
            "attempted": True,
            "initial_route": "confirmed_fold_spread_split",
            "selected_route": "pending_embedded_or_full_page_recovery",
            "reason": "one_or_more_fold_regions_below_prose_threshold",
            "region_word_counts": [
                photographed_ocr_text_quality(text)["word_count"]
                for text in region_texts
            ],
        }

    # A photographed two-page spread is commonly stored as one substantial
    # embedded image.  It must pass through the fold-gated split above before
    # the generic embedded-scan fast path, otherwise both book pages are OCRed
    # as one interleaved reading stream.  Ordinary embedded scans and
    # landscape pages without a strong fold retain the established path.
    embedded_fraction = embedded_scanned_image_fraction(page)
    if embedded_fraction:
        column_evidence = photographed_three_column_signal(image, ImageOps)
        if column_evidence["detected"]:
            text, layout_rows = _ocr_photographed_crop_with_layout(
                image, embedded_fraction, tesseract, ImageOps, psm=3
            )
            if not text:
                text = _ocr_photographed_crop(
                    image, embedded_fraction, tesseract, ImageOps, psm=3
                )
                layout_rows = []
            text, reading_order_evidence = reorder_three_column_ocr_blocks(
                text,
                layout_rows,
                column_evidence,
                embedded_fraction,
            )
            text, drop_cap_evidence = recover_geometry_aligned_drop_caps(
                text,
                image,
                embedded_fraction,
                layout_rows,
                tesseract,
                ImageOps,
            )
            # Some decorative opening Ws are segmented into several strokes
            # rather than one connected glyph. Retain the older, tightly
            # bounded first-page fallback only when the geometry route could
            # not recover an opening initial itself.
            if (
                int(page_number or 0) == 1
                and not drop_cap_evidence.get("opening_recovered")
            ):
                drop_cap_reference = _ocr_photographed_crop(
                    image, embedded_fraction, tesseract, ImageOps, psm=4
                )
                text, opening_evidence = recover_opening_three_column_drop_cap(
                    text,
                    drop_cap_reference,
                    image,
                    tesseract,
                    ImageOps,
                    page_number=page_number,
                )
                drop_cap_evidence["opening_fallback"] = opening_evidence
                if opening_evidence.get("recovered"):
                    drop_cap_evidence["recovered_count"] = int(
                        drop_cap_evidence.get("recovered_count") or 0
                    ) + 1
                    drop_cap_evidence["opening_recovered"] = True
            text, byline_evidence = relocate_unique_opening_ocr_byline(
                text, page_number=page_number
            )
            if len(text) >= 80:
                if route_recovery:
                    route_recovery["selected_route"] = "embedded_three_column_document"
                return [{
                    "text": text,
                    "reading_region": "embedded_scanned_three_column_document",
                    "reading_region_index": 1,
                    "reading_region_count": 1,
                    "source_column_index": 1,
                    "ocr_method": "tesseract_embedded_three_column_document_crop",
                    "annotations_excluded": "embedded_image_bounds_and_outer_margin_crop",
                    "crop_fraction": list(embedded_fraction),
                    "column_preprocessing": {
                        **column_evidence,
                        "psm": 3,
                        "reading_order": reading_order_evidence,
                        "byline": byline_evidence,
                        "drop_cap": drop_cap_evidence,
                    },
                    **({"ocr_route_recovery": route_recovery} if route_recovery else {}),
                }]
        embedded_fraction, crop_adjustment = adaptive_ocr_crop_fraction(image, embedded_fraction, ImageOps)
        recognition_layout = {}
        text, layout_rows = _ocr_photographed_crop_with_layout(
            image, embedded_fraction, tesseract, ImageOps,
            recognition_evidence=recognition_layout,
        )
        if not text:
            text = _ocr_photographed_crop(
                image, embedded_fraction, tesseract, ImageOps,
                recognition_evidence=recognition_layout,
            )
            layout_rows = []
        text, drop_cap_evidence = recover_geometry_aligned_drop_caps(
            text,
            image,
            embedded_fraction,
            layout_rows,
            tesseract,
            ImageOps,
        )
        text, missing_display_evidence = recover_missing_display_regions(
            text, image, embedded_fraction, layout_rows, tesseract, ImageOps,
            page_number=page_number,
        )
        if len(text) < 80 and int(page_number or 0) <= 2:
            display_decision = {}
            display_text = _ocr_photographed_crop(image, embedded_fraction, tesseract, ImageOps, psm=3,
                                                 recognition_evidence=display_decision)
            if credible_short_ocr_display_text(display_text):
                text = display_text
                recognition_layout = display_decision
        embedded_quality = photographed_ocr_text_quality(text)
        crop_boundary = ocr_crop_boundary_evidence(image, embedded_fraction, ImageOps)
        retry_evidence = {
            "attempted": False,
            "reason": ("sparse_opening_text_retained_coverage_unverified"
                       if len(text.split()) <= 18 and int(page_number or 0) <= 2
                       else "embedded_crop_recovery_sufficient"),
            "embedded_crop": embedded_quality,
            "crop_boundary": crop_boundary,
        }
        # PDF image rectangles can be misleading on rotated or transformed
        # wrapper pages. A weak result is therefore checked once against the
        # rendered page's conservative inner crop. Strong ordinary scan pages
        # do not incur this second OCR call.
        weak_embedded_recovery = embedded_scan_crop_needs_full_page_retry(
            embedded_fraction, embedded_quality, crop_boundary
        )
        if weak_embedded_recovery:
            full_page_decision = {}
            full_text = _ocr_photographed_crop(
                image, PHOTOGRAPHED_PAGE_CROP, tesseract, ImageOps,
                recognition_evidence=full_page_decision,
            )
            full_quality = photographed_ocr_text_quality(full_text)
            materially_better_full_page = full_page_ocr_retry_materially_better(
                embedded_quality, full_quality
            )
            retry_evidence = {
                "attempted": True,
                "reason": (
                    "full_page_retry_selected"
                    if materially_better_full_page
                    else "embedded_crop_retained"
                ),
                "embedded_crop": embedded_quality,
                "full_page_inner_crop": full_quality,
                "crop_boundary": crop_boundary,
            }
            if materially_better_full_page:
                text = full_text
                embedded_fraction = PHOTOGRAPHED_PAGE_CROP
                recognition_layout = full_page_decision
        if len(text) >= 80 or credible_short_ocr_display_text(text):
            if route_recovery:
                route_recovery["selected_route"] = "embedded_scan_bounds"
            return [{
                "text": text,
                "reading_region": "embedded_scanned_document",
                "reading_region_index": 1,
                "reading_region_count": 1,
                "source_column_index": 1,
                "ocr_method": "tesseract_embedded_scanned_document_crop",
                "annotations_excluded": "embedded_image_bounds_and_outer_margin_crop",
                "crop_fraction": list(embedded_fraction),
                "crop_adjustment": crop_adjustment,
                "drop_cap_recovery": drop_cap_evidence,
                "recognition_layout": dict(recognition_layout),
                "missing_display_regions": missing_display_evidence,
                "ocr_crop_retry": retry_evidence,
                **({"ocr_route_recovery": route_recovery} if route_recovery else {}),
            }]
    if not photographed_page_visual_signal(page, image, ImageStat):
        return []


    left, top, right, bottom = PHOTOGRAPHED_PAGE_CROP
    text = _ocr_photographed_crop(image, PHOTOGRAPHED_PAGE_CROP, tesseract, ImageOps)
    if len(text) < 80 and int(page_number or 0) <= 2:
        display_text = _ocr_photographed_crop(image, PHOTOGRAPHED_PAGE_CROP, tesseract, ImageOps, psm=3)
        if credible_short_ocr_display_text(display_text):
            text = display_text
    if len(text) < 80:
        return []
    if route_recovery:
        route_recovery["selected_route"] = "photographed_page_inner_crop"
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
            **({"ocr_route_recovery": route_recovery} if route_recovery else {}),
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
            # ``get_image_rects`` reports PDF coordinates before the page's
            # display rotation, while ``page.rect`` and the rendered image used
            # by the caller are rotation-aware.  Comparing those two spaces
            # directly can turn a full-page scan into an apparently clipped
            # 70-80% crop and remove the end of every OCR line.  Transform the
            # provenance rectangle into displayed-page coordinates first.
            try:
                displayed_rect = fitz.Rect(rect)
                if int(getattr(page, "rotation", 0) or 0) % 360:
                    displayed_rect = displayed_rect * page.rotation_matrix
                displayed_rect = displayed_rect & page.rect
            except Exception:
                continue
            if displayed_rect.is_empty or displayed_rect.is_infinite:
                continue
            coverage = max(
                0.0, float(displayed_rect.width * displayed_rect.height)
            ) / page_area
            if coverage >= .28:
                candidates.append((coverage, displayed_rect))
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


def photographed_fold_gutter_fraction(image, image_stat, *, _stripe_fraction=.012):
    """Locate a strong central fold shadow or clean facing-page gutter.

    Some book scans have a dark binding shadow; others have a bright strip of
    paper between two text blocks.  Either signal must be vertically
    continuous and contrast with text-bearing bands on both outer sides.
    Width alone is never sufficient.
    """
    gray = image.convert("L")
    width, height = gray.size
    if width / max(height, 1) < 1.22:
        return None
    top, bottom = int(height * .12), int(height * .90)
    stripe_half_width = max(2, int(width * _stripe_fraction))

    def stripe_mean(fraction):
        center = int(width * fraction)
        left = max(0, center - stripe_half_width)
        right = min(width, center + stripe_half_width)
        return float(image_stat.Stat(gray.crop((left, top, right, bottom))).mean[0])

    candidates = [(fraction, stripe_mean(fraction)) for fraction in [0.34 + step * .01 for step in range(33)]]
    side_values = [stripe_mean(.16), stripe_mean(.24), stripe_mean(.76), stripe_mean(.84)]
    side_baseline = sorted(side_values)[1:3]
    baseline = sum(side_baseline) / max(len(side_baseline), 1)
    # Prefer a clean paper gutter when one exists. On bright book scans, the
    # darkest stripe is commonly a body-text column rather than the binding;
    # choosing it first creates an asymmetric split and can repeat OCR work.
    # The established two-sided contrast requirement prevents a blank title
    # leaf or an empty half-page from being mistaken for a spread.
    light_fraction, lightness = max(candidates, key=lambda item: item[1])
    left_content = min(stripe_mean(.24), stripe_mean(.38))
    right_content = min(stripe_mean(.70), stripe_mean(.82))
    if (
        lightness >= 248.0
        and lightness >= left_content + 18.0
        and lightness >= right_content + 18.0
    ):
        return round(light_fraction, 3)

    fraction, darkness = min(candidates, key=lambda item: item[1])
    # A true photographed fold is a vertically continuous shadow. Requiring
    # this contrast avoids splitting ordinary landscape pages or columns.
    if darkness <= baseline - max(18.0, baseline * .10):
        return round(fraction, 3)
    # A grey binding shadow can be much less dramatic in the whole-height
    # average while remaining visible in nearly every horizontal band. Accept
    # that weaker contrast only with strong vertical continuity. This is much
    # harder for body text, an illustration edge, or a short stain to satisfy.
    band_deltas = []
    for band_index in range(8):
        band_top = int(height * (.12 + band_index * .0975))
        band_bottom = int(height * (.12 + (band_index + 1) * .0975))

        def band_mean(at_fraction):
            center = int(width * at_fraction)
            left = max(0, center - stripe_half_width)
            right = min(width, center + stripe_half_width)
            return float(
                image_stat.Stat(gray.crop((left, band_top, right, band_bottom))).mean[0]
            )

        local_baseline = (band_mean(max(.28, fraction - .055)) + band_mean(min(.72, fraction + .055))) / 2
        band_deltas.append(local_baseline - band_mean(fraction))
    continuous_grey_shadow = sum(delta >= 7.0 for delta in band_deltas) >= 7
    if continuous_grey_shadow and statistics.median(band_deltas) >= 10.0:
        return round(fraction, 3)
    # Preserve every established fold decision. Only an unresolved landscape
    # gets a narrower measurement, with the same contrast/continuity tests.
    # A thin opening-page binding can be washed out by the wider stripe.
    if _stripe_fraction == .012:
        return photographed_fold_gutter_fraction(image, image_stat, _stripe_fraction=.008)
    return None


def photographed_spread_render_dpi(image, specs, image_ops):
    """Raise resolution only for small printed lines in confirmed regions.

    Measure short horizontal ink bands in a narrow body strip to avoid skew
    merging adjacent lines. Ignore specks and illustrations; no OCR trial is
    needed. Larger type keeps the established raster and output unchanged.
    """
    if not specs:
        return PHOTOGRAPHED_PAGE_OCR_DPI
    width, height = image.size
    gray = image_ops.grayscale(image)
    small_type = False
    for _name, _index, fraction in specs:
        left, _top, right, _bottom = fraction
        span = right - left
        strip = gray.crop((int(width*(left+span*.32)), int(height*.20),
                           int(width*(left+span*.70)), int(height*.80)))
        binary = strip.point(lambda value: 255 if value < 140 else 0)
        bands = []
        current = 0
        for y in range(binary.height + 1):
            ink = y < binary.height and binary.crop((0,y,binary.width,y+1)).getbbox() is not None
            if ink:
                current += 1
            else:
                if current > 80:
                    # A large illustration is not small-type evidence.
                    # Keep its established raster: resampling can lose
                    # lettering embedded in the picture.
                    return PHOTOGRAPHED_PAGE_OCR_DPI
                if 5 <= current <= 40:
                    bands.append(current)
                current = 0
        small_type |= len(bands) >= 8 and statistics.median(bands) <= 16
    return 180 if small_type else PHOTOGRAPHED_PAGE_OCR_DPI


def photographed_sparse_spread_gutter(image, image_stat):
    """Allow short facing-page notes, but reject text spanning the gutter.

    The established contrast test must agree in two upper-body windows. A
    near-white gutter must then continue through the lower body. This does
    not relax the ordinary full-height fold detector or infer from width.
    """
    width, height = image.size
    if width / max(1, height) < 1.22:
        return None
    candidates = [photographed_fold_gutter_fraction(
        image.crop((0, 0, width, int(height * end))), image_stat
    ) for end in (.4, .5)]
    first, second = candidates
    if first is None or second is None or abs(first - second) > .015:
        return None
    gutter = (first + second) / 2
    if not .43 <= gutter <= .57:
        return None
    gray = image.convert("L")
    for band in range(8):
        stripe = gray.crop((int(width*(gutter-.008)), int(height*(.15+band*.09)),
                            int(width*(gutter+.008)), int(height*(.24+band*.09))))
        if image_stat.Stat(stripe).mean[0] < 248:
            return None
    return round(gutter, 3)


def photographed_spread_crop_specs(width, height, gutter_fraction=None):
    """Return two equal-sized source regions around a confirmed fold shadow."""
    if float(width) / max(float(height), 1.0) < 1.22 or gutter_fraction is None:
        return []
    gutter = min(.72, max(.28, float(gutter_fraction)))
    return [
        ("spread_left", 1, (0.04, 0.055, max(.05, gutter - .025), 0.95)),
        ("spread_right", 2, (min(.95, gutter + .025), 0.055, 0.96, 0.95)),
    ]


def _grayscale_pixels(image, image_ops, *, width=360, height=480):
    gray = image_ops.grayscale(image).resize((width, height))
    flattened = getattr(gray, "get_flattened_data", None)
    raw_pixels: Any = flattened() if callable(flattened) else gray.getdata()
    return list(raw_pixels), width, height


def photographed_portrait_neighbour_sliver(image, image_ops):
    """Nominate, but do not yet delete, a narrow facing-page strip."""
    width, height = image.size
    if width <= 0 or height <= 0 or width / height >= .90:
        return None
    pixels, sample_width, sample_height = _grayscale_pixels(image, image_ops, width=360, height=500)
    top = int(sample_height * .08)
    bottom = int(sample_height * .94)
    body_height = max(1, bottom - top)
    occupancy = [
        sum(pixels[y * sample_width + x] < 165 for y in range(top, bottom)) / body_height
        for x in range(sample_width)
    ]
    candidates = []
    for side, low, high in (("left", .045, .19), ("right", .81, .955)):
        start = int(sample_width * low)
        end = max(start + 1, int(sample_width * high))
        seam = max(range(start, end), key=occupancy.__getitem__)
        seam_strength = occupancy[seam]
        # Repeated prose lines can share the same left edge and make one
        # ordinary text column look vertically dark. A fold must be a local
        # peak, not merely the darkest x-coordinate in the outer band.
        comparison_radius = max(5, int(sample_width * .019))
        neighbours = (
            occupancy[max(start, seam - comparison_radius):seam]
            + occupancy[seam + 1:min(end, seam + comparison_radius + 1)]
        )
        local_baseline = statistics.median(neighbours) if neighbours else seam_strength
        seam_contrast = seam_strength - local_baseline
        fraction = seam / sample_width
        narrow_width = fraction if side == "left" else 1.0 - fraction
        if seam_strength >= .30 and seam_contrast >= .10 and .045 <= narrow_width <= .19:
            candidates.append((seam_strength, seam_contrast, side, fraction))
    if not candidates:
        return None
    seam_strength, seam_contrast, side, fraction = max(candidates)
    gap = .012
    if side == "left":
        narrow = (.025, .045, max(.035, fraction - gap), .96)
        dominant = (min(.965, fraction + gap), .045, .975, .96)
    else:
        narrow = (min(.965, fraction + gap), .045, .975, .96)
        dominant = (.025, .045, max(.035, fraction - gap), .96)
    return {
        "side": side,
        "seam_fraction": round(fraction, 4),
        "seam_dark_occupancy": round(seam_strength, 4),
        "seam_local_contrast": round(seam_contrast, 4),
        "narrow_crop_fraction": narrow,
        "dominant_crop_fraction": dominant,
        "method": "portrait_edge_continuous_fold_v1",
    }


def ocr_crop_boundary_evidence(image, fraction, image_ops):
    """Measure whether source ink touches a proposed OCR crop boundary."""
    pixels, sample_width, sample_height = _grayscale_pixels(image, image_ops, width=360, height=480)
    left, top, right, bottom = [float(value) for value in fraction]
    x1 = max(0, min(sample_width - 1, int(left * sample_width)))
    x2 = max(x1 + 1, min(sample_width, int(right * sample_width)))
    y1 = max(0, min(sample_height - 1, int(top * sample_height)))
    y2 = max(y1 + 1, min(sample_height, int(bottom * sample_height)))
    band_x = max(2, int(sample_width * .006))
    band_y = max(2, int(sample_height * .006))

    def density(ax, ay, bx, by):
        total = max(1, (bx - ax) * (by - ay))
        return sum(
            pixels[y * sample_width + x] < 190
            for y in range(ay, by) for x in range(ax, bx)
        ) / total

    edges = {
        "left": density(x1, y1, min(x2, x1 + band_x), y2),
        "right": density(max(x1, x2 - band_x), y1, x2, y2),
        "top": density(x1, y1, x2, min(y2, y1 + band_y)),
        "bottom": density(x1, max(y1, y2 - band_y), x2, y2),
    }
    return {
        "edge_ink_density": {key: round(value, 4) for key, value in edges.items()},
        "ink_touches_boundary": any(value >= .035 for value in edges.values()),
    }


def adaptive_ocr_crop_fraction(image, fraction, image_ops, *, max_expand=.014):
    """Expand only crop edges that visibly intersect source ink."""
    original = tuple(float(value) for value in fraction)
    evidence = ocr_crop_boundary_evidence(image, original, image_ops)
    densities = evidence["edge_ink_density"]
    left, top, right, bottom = original
    expanded_edges = []
    if densities["left"] >= .035 and left > .002:
        left = max(.0, left - max_expand)
        expanded_edges.append("left")
    if densities["right"] >= .035 and right < .998:
        right = min(1.0, right + max_expand)
        expanded_edges.append("right")
    if densities["top"] >= .035 and top > .002:
        top = max(.0, top - max_expand)
        expanded_edges.append("top")
    if densities["bottom"] >= .035 and bottom < .998:
        bottom = min(1.0, bottom + max_expand)
        expanded_edges.append("bottom")
    adjusted = (left, top, right, bottom)
    return adjusted, {
        "method": "edge_ink_bounded_expansion_v1",
        "original_crop_fraction": list(original),
        "adjusted_crop_fraction": list(adjusted),
        "expanded_edges": expanded_edges,
        **evidence,
    }


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


def photographed_three_column_signal(image, image_ops):
    """Detect only a strong upper-page three-column gutter pattern.

    The detector is intentionally cheap and conservative. It downsamples the
    upper body of a photographed page and requires two nearly empty vertical
    gutters in broad one-third/two-third search bands. Looking only at the
    upper body lets a lower-page illustration coexist with three text columns,
    while requiring both gutters rejects ordinary one/two-column pages, book
    folds, and isolated internal whitespace.
    """
    gray = image_ops.grayscale(image)
    width, height = gray.size
    if width < 300 or height < 400:
        return {"detected": False, "reason": "image_too_small", "gutters": []}
    upper = gray.crop((
        int(width * .04), int(height * .12),
        int(width * .96), int(height * .42),
    ))
    sample_width = min(600, max(240, upper.width))
    sample_height = min(240, max(120, upper.height))
    sample = upper.resize((sample_width, sample_height))
    flattened = getattr(sample, "get_flattened_data", None)
    raw_pixels: Any = flattened() if callable(flattened) else sample.getdata()
    pixels = list(raw_pixels)
    row_ink = []
    for y in range(sample_height):
        offset = y * sample_width
        ink = sum(pixels[offset + x] < 205 for x in range(sample_width))
        row_ink.append(ink / sample_width)
    text_rows = [index for index, ratio in enumerate(row_ink) if ratio > .008]
    if len(text_rows) < max(18, int(sample_height * .16)):
        return {"detected": False, "reason": "insufficient_text_rows", "gutters": []}
    occupancy = []
    for x in range(sample_width):
        occupancy.append(
            sum(pixels[y * sample_width + x] < 205 for y in text_rows)
            / len(text_rows)
        )
    window = max(3, int(sample_width * .012))
    smoothed = [
        sum(occupancy[start:start + window]) / window
        for start in range(sample_width - window + 1)
    ]
    gutters = []
    for low, high in ((.23, .43), (.55, .77)):
        start = min(len(smoothed) - 1, int(low * sample_width))
        end = max(start + 1, min(len(smoothed), int(high * sample_width)))
        position = min(range(start, end), key=smoothed.__getitem__)
        gutters.append({
            "x_fraction": round(position / sample_width, 3),
            "ink_occupancy": round(float(smoothed[position]), 4),
        })
    column_ink = []
    for low, high in ((.02, .29), (.36, .62), (.70, .98)):
        start = min(len(occupancy) - 1, int(low * sample_width))
        end = max(start + 1, min(len(occupancy), int(high * sample_width)))
        column_ink.append(round(sum(occupancy[start:end]) / (end - start), 4))
    gutter_evidence = all(gutter["ink_occupancy"] <= .01 for gutter in gutters)
    # Blank half-pages and title leaves can contain two large empty bands but
    # are not three-column documents. Require actual ink in all three column
    # bodies as well as the two gutters.
    column_evidence = all(value >= .018 for value in column_ink)
    column_balance = max(column_ink) / max(min(column_ink), .0001) if column_ink else float("inf")
    balanced_columns = column_balance <= 5.0
    detected = gutter_evidence and column_evidence and balanced_columns
    return {
        "detected": detected,
        "reason": (
            "two_upper_page_gutters_and_three_inked_columns"
            if detected
            else "column_ink_imbalanced"
            if gutter_evidence and column_evidence
            else "column_ink_insufficient"
            if gutter_evidence
            else "gutter_evidence_insufficient"
        ),
        "gutters": gutters,
        "column_ink_occupancy": column_ink,
        "column_ink_balance_ratio": round(column_balance, 3),
    }


def relocate_unique_opening_ocr_byline(text, *, page_number=None):
    """Move one exact first-page ``By Person`` line directly below the title.

    PSM 3 correctly follows columns but may place a centered byline after the
    first column. This function changes ordering only: it never edits or
    invents name text, and it abstains unless exactly one constrained byline is
    present on PDF page 1.
    """
    evidence = {"relocated": False, "reason": "not_first_pdf_page"}
    if int(page_number or 0) != 1:
        return str(text or ""), evidence
    content = str(text or "")
    lines = content.splitlines()
    pattern = re.compile(
        r"(?i:by|written by|story by)\s+"
        r"[A-Z][A-Za-z.'’-]+(?:\s+[A-Z][A-Za-z.'’-]+){1,4}"
    )
    candidates = [
        (index, line.strip())
        for index, line in enumerate(lines)
        if pattern.fullmatch(line.strip())
    ]
    if len(candidates) != 1:
        return content, {
            "relocated": False,
            "reason": "exact_unique_byline_not_found",
            "candidate_count": len(candidates),
        }
    index, byline = candidates[0]
    nonempty = [position for position, line in enumerate(lines) if line.strip()]
    if not nonempty:
        return content, {"relocated": False, "reason": "empty_ocr_text"}
    # A periodical masthead can precede the article title. Anchor after the
    # final short heading immediately before the opening prose, rather than
    # blindly treating the first OCR line as the title. If no early prose line
    # makes that boundary clear, preserve the original order.
    early_nonempty = nonempty[:12]
    prose_index = next((
        position
        for position in early_nonempty
        if (
            len(re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]+", lines[position])) >= 7
            or re.search(r"[.!?][\"'’)]?\s*$", lines[position].strip())
        )
    ), None)
    heading_indexes = [
        position
        for position in early_nonempty
        if prose_index is not None and position < prose_index and position != index
    ]
    if not heading_indexes:
        return content, {
            "relocated": False,
            "reason": "opening_title_boundary_unclear",
            "candidate_count": 1,
        }
    title_index = heading_indexes[-1]
    if index <= title_index + 2:
        return content, {
            "relocated": False,
            "reason": "already_adjacent_to_title",
            "candidate_count": 1,
        }
    del lines[index]
    while (
        index < len(lines)
        and index > 0
        and not lines[index].strip()
        and not lines[index - 1].strip()
    ):
        del lines[index]
    lines[title_index + 1:title_index + 1] = ["", byline, ""]
    return "\n".join(lines).strip() + "\n", {
        "relocated": True,
        "reason": "single_exact_opening_byline",
        "candidate_count": 1,
        "byline": byline,
    }


def recover_opening_three_column_drop_cap(
    text, reference_text, image, tesseract, image_ops, *, page_number=None
):
    """Recover one oversized opening glyph from its image component.

    The glyph is read from a tightly bounded connected component.  A second
    layout mode supplies only the following uppercase token.  No dictionary,
    fuzzy word substitution, or invented punctuation is involved.
    """
    evidence = {"recovered": False, "reason": "not_first_pdf_page"}
    if int(page_number or 0) != 1:
        return str(text or ""), evidence
    try:
        import cv2
        import numpy as np
    except ImportError:
        return str(text or ""), {"recovered": False, "reason": "component_runtime_unavailable"}
    gray = np.array(image_ops.grayscale(image))
    mask = (gray < 140).astype("uint8")
    _count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
    height, width = gray.shape[:2]
    candidates = []
    for x, y, component_width, component_height, area in stats[1:]:
        if (
            x < width * .22
            and height * .04 < y < height * .38
            and height * .025 < component_height < height * .12
            and width * .01 < component_width < width * .12
        ):
            candidates.append((int(area), int(x), int(y), int(component_width), int(component_height)))
    if not candidates:
        return str(text or ""), {"recovered": False, "reason": "oversized_opening_component_not_found"}
    _area, x, y, component_width, component_height = max(candidates)
    pad = .01
    fraction = (
        max(0.0, x / width - pad),
        max(0.0, y / height - pad),
        min(1.0, (x + component_width) / width + pad),
        min(1.0, (y + component_height) / height + pad),
    )
    glyph_text = normalize_text(
        _ocr_photographed_crop(image, fraction, tesseract, image_ops, psm=13)
    )
    glyph_match = re.fullmatch(r"([A-Za-z])[.:]?", glyph_text)
    if not glyph_match:
        return str(text or ""), {
            "recovered": False,
            "reason": "component_did_not_resolve_to_one_glyph",
            "component_crop_fraction": list(fraction),
        }
    glyph = glyph_match.group(1).upper()

    def first_prose_token(content):
        for line in str(content or "").splitlines():
            words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]+", line)
            if (
                len(words) >= 5
                and 2 <= len(words[0]) <= 5
                and words[0].isupper()
                and not words[1].isupper()
            ):
                return words[0]
        return ""

    reference_token = first_prose_token(reference_text)
    if not reference_token or reference_token.startswith(glyph):
        return str(text or ""), {
            "recovered": False,
            "reason": "independent_following_token_not_available",
            "glyph": glyph,
            "component_crop_fraction": list(fraction),
        }
    completed_token = glyph + reference_token
    lines = str(text or "").splitlines()
    for index, line in enumerate(lines):
        words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]+", line)
        if len(words) < 5 or not words[0].isupper():
            continue
        observed_token = words[0]
        if observed_token != reference_token and not observed_token.endswith(reference_token):
            continue
        match = re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ]+", line)
        if not match:
            continue
        prefix = line[:match.start()]
        if not re.search(r"[A-Za-z0-9]", prefix):
            prefix = ""
        lines[index] = prefix + completed_token + line[match.end():]
        return "\n".join(lines), {
            "recovered": True,
            "reason": "geometry_glyph_plus_independent_following_token",
            "glyph": glyph,
            "reference_token": reference_token,
            "observed_token": observed_token,
            "completed_token": completed_token,
            "component_crop_fraction": list(fraction),
        }
    return str(text or ""), {
        "recovered": False,
        "reason": "reading_order_token_not_aligned",
        "glyph": glyph,
        "reference_token": reference_token,
        "component_crop_fraction": list(fraction),
    }


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
        geometry_confirmed = str(candidate.get("geometry_confidence") or "") == "high"
        preprocessing["neighbour_page_runover"]["geometry_confidence"] = (
            "high" if geometry_confirmed else "not_established"
        )
        if not match["confirmed"] and not geometry_confirmed:
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
        preprocessing["neighbour_page_runover"]["decision"] = (
            "geometry_confirmed_excluded"
            if geometry_confirmed and not match["confirmed"]
            else "confirmed_excluded"
        )
        if candidate.get("geometry_evidence"):
            preprocessing["neighbour_page_runover"]["geometry_evidence"] = dict(candidate["geometry_evidence"])
    return pages


def _parse_tesseract_tsv(tsv_text):
    """Parse word geometry without treating ordinary quote marks as CSV syntax."""
    rows = []
    for raw_line in str(tsv_text or "").splitlines()[1:]:
        fields = raw_line.split("\t", 11)
        if len(fields) != 12:
            continue
        try:
            level, page, block, paragraph, line, word = (
                int(fields[index]) for index in range(6)
            )
            left, top, width, height = (
                int(fields[index]) for index in range(6, 10)
            )
            confidence = float(fields[10])
        except (TypeError, ValueError):
            continue
        text = fields[11].strip()
        if level != 5 or not text:
            continue
        rows.append({
            "page": page,
            "block": block,
            "paragraph": paragraph,
            "line": line,
            "word": word,
            "left": left,
            "top": top,
            "width": width,
            "height": height,
            "confidence": round(confidence, 3),
            "text": text,
        })
    return rows


@lru_cache(maxsize=4)
def _verified_annotated_model(path, size, mtime):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest() == "8280aed0782fe27257a68ea10fe7ef324ca0f8d85bd2fd145d1c2b560bcb66ba"  # pragma: allowlist secret -- public tessdata model checksum
    except OSError:
        return False


def annotated_model_arguments(requested_psm, resolved_psm):
    if requested_psm != 4 or resolved_psm != 6:
        return []
    model = Path(__file__).resolve().parent / "assets" / "tessdata-annotated" / "eng.traineddata"
    if not model.is_file():
        model = Path(sys.prefix) / "share" / "anythingllm-pdf-assistant" / "assets" / "tessdata-annotated" / "eng.traineddata"
    try:
        stat = model.stat()
        if stat.st_size == 15400601 and _verified_annotated_model(str(model),stat.st_size,stat.st_mtime_ns):
            return ["--tessdata-dir",str(model.parent),"--oem","1"]
    except OSError:
        pass
    return []


def annotated_text_block_psm(cropped, requested=4, *, decision=None):
    """Keep underlined prose together without deleting any source pixels.

    Only an already isolated reading region is eligible. Several long, thin
    horizontal strokes are needed; ruled tables with vertical lines retain
    the established layout mode. Other explicitly requested modes are intact.
    Optional image-analysis failure falls back to the existing OCR route.
    """
    def choose(mode, reason):
        if decision is not None:
            decision.update(candidate_psm=mode, geometry_reason=reason,
                            underlined_prose_detected=mode == 6 and requested == 4)
        return mode

    if requested != 4:
        return choose(requested, "explicit_layout_preserved")
    try:
        import cv2
        import numpy as np
        from PIL import ImageOps
        rgb = np.asarray(cropped.convert("RGB"), dtype=np.int16)
        coloured_marks = (rgb.max(axis=2) - rgb.min(axis=2) > 40) & (rgb.max(axis=2) > 120)
        if np.count_nonzero(coloured_marks) / coloured_marks.size > .005:
            return choose(requested, "coloured_marks_preserve_layout")
        ink = (np.asarray(ImageOps.autocontrast(cropped.convert('L'))) < 170).astype(np.uint8) * 255
        height, width = ink.shape
        if width < 200 or height < 200:
            return choose(requested, "region_too_small")
        # A dirty crop boundary may contain a neighbouring page or scan frame.
        # Do not force its contents into one text block; preserve layout OCR.
        edge_y, edge_x = max(2, height // 100), max(2, width // 100)
        if any(np.count_nonzero(edge) / edge.size > .03 for edge in (
            ink[:edge_y], ink[-edge_y:], ink[:, :edge_x], ink[:, -edge_x:]
        )):
            return choose(requested, "crop_border_ink")
        vertical = cv2.morphologyEx(ink, cv2.MORPH_OPEN,
                                    np.ones((max(40,height//8),1),np.uint8))
        # Even one substantial spine/frame line makes a single-block layout
        # unsafe: it can pull neighbouring-page fragments into the prose.
        if cv2.countNonZero(vertical) > height * .5:
            return choose(requested, "vertical_spine_or_rule")
        horizontal = cv2.morphologyEx(ink, cv2.MORPH_OPEN,
                                      np.ones((1,max(40,width//25)),np.uint8))
        count, _, stats, _ = cv2.connectedComponentsWithStats(horizontal)
        rules = [s for s in stats[1:count] if s[2] >= max(40,width*.04)
                 and s[3] <= max(5,height*.006) and s[2]/max(1,s[3]) >= 30]
        if len(rules) < 3 or sum(s[2] for s in rules) < width*.15:
            return choose(requested, "insufficient_annotation_rules")
        # Five closely spaced parallel rules are more consistent with music
        # staves than underlined prose. Keep the existing layout for these.
        ordered_rules = sorted(rules, key=lambda s: s[1])
        for i in range(len(ordered_rules) - 4):
            group = ordered_rules[i:i + 5]
            gaps = np.diff([s[1] for s in group])
            if (min(gaps) >= 3 and max(gaps) <= height * .012
                    and max(gaps) - min(gaps) <= 2
                    and max(s[0] for s in group) - min(s[0] for s in group) <= width * .02):
                return choose(requested, "staff_like_parallel_rules")
        # Sparse title leaves can contain decorative rules too. Require a
        # substantial text block before changing its segmentation/model.
        _, _, components, _ = cv2.connectedComponentsWithStats(ink)
        text_components = sum(
            2 <= s[2] <= width * .035
            and 3 <= s[3] <= height * .035 and s[4] >= 5
            for s in components[1:]
        )
        return choose(6, "substantial_underlined_prose") if text_components >= 500 else choose(requested, "sparse_text_block")
    except Exception as exc:
        # This optional, non-mutating geometry probe must never block OCR.
        if decision is not None:
            decision["probe_error_type"] = type(exc).__name__
        return choose(requested, "geometry_probe_unavailable")


def _resolve_ocr_recognition(cropped, requested):
    """Resolve the tested model/layout pair once, before invoking Tesseract."""
    decision = {"requested_psm": requested, "annotation_pixels_removed": False}
    candidate = annotated_text_block_psm(cropped, requested, decision=decision)
    model_args = annotated_model_arguments(requested, candidate)
    psm = candidate
    if requested == 4 and candidate == 6 and not model_args:
        # The tested route requires both changes. A missing/invalid model must
        # not leave the changed layout paired with unqualified language data.
        psm = requested
        decision["route_reason"] = "annotated_model_unavailable_original_route"
    else:
        decision["route_reason"] = decision["geometry_reason"]
    decision.update(psm=psm, model="tessdata_best_eng" if model_args else "installed_eng")
    return psm, model_args, decision


def _run_measured_tesseract(command, recognition_evidence):
    """Observe the existing call only; preserve its result and exception policy."""
    started = time.perf_counter()
    outcome = "exception"
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=90, check=False)
        outcome = "exit_ok" if completed.returncode == 0 else "nonzero_exit"
        return completed
    except subprocess.TimeoutExpired:
        outcome = "timeout"
        raise
    except OSError:
        outcome = "launch_error"
        raise
    finally:
        if isinstance(recognition_evidence, dict):
            recognition_evidence["subprocess_seconds"] = round(time.perf_counter() - started, 4)
            recognition_evidence["subprocess_outcome"] = outcome


def _ocr_photographed_crop_with_layout(
    image, fraction, tesseract, image_ops, *, psm=4, recognition_evidence=None
):
    """OCR once and return both readable text and word/block geometry."""
    setup_started = time.perf_counter()
    width, height = image.size
    left, top, right, bottom = fraction
    crop_box = (
        int(width * left), int(height * top),
        int(width * right), int(height * bottom),
    )
    cropped = image_ops.autocontrast(image.crop(crop_box).convert("L"))
    psm, model_args, decision = _resolve_ocr_recognition(image.crop(crop_box), psm)
    if recognition_evidence is not None:
        recognition_evidence.clear()
        recognition_evidence.update(decision, crop_fraction=list(fraction))
        recognition_evidence["setup_seconds"] = round(time.perf_counter() - setup_started, 4)
    with tempfile.TemporaryDirectory(prefix="rag-photographed-layout-") as temp_dir:
        image_path = Path(temp_dir) / "page.png"
        output_base = Path(temp_dir) / "result"
        cropped.save(image_path)
        try:
            completed = _run_measured_tesseract(
                [
                    tesseract, str(image_path), str(output_base),
                    *model_args,
                    "--psm", str(int(psm)), "-l", "eng", "txt", "tsv",
                ],
                recognition_evidence,
            )
        except (OSError, subprocess.TimeoutExpired):
            return "", []
        text_path = output_base.with_suffix(".txt")
        tsv_path = output_base.with_suffix(".tsv")
        if completed.returncode != 0 or not text_path.is_file():
            return "", []
        try:
            text = text_path.read_text(encoding="utf-8", errors="replace")
            tsv_text = (
                tsv_path.read_text(encoding="utf-8", errors="replace")
                if tsv_path.is_file()
                else ""
            )
        except OSError:
            return "", []
    rows = _parse_tesseract_tsv(tsv_text)
    for row in rows:
        row["crop_width"] = cropped.width
        row["crop_height"] = cropped.height
        row["ocr_psm"] = psm
        row["ocr_model"] = "tessdata_best_eng" if model_args else "installed_eng"
    return clean_photographed_ocr_text(text), rows


def _tsv_block_text(rows):
    paragraphs = []
    for paragraph_number in sorted({row["paragraph"] for row in rows}):
        paragraph_rows = [
            row for row in rows if row["paragraph"] == paragraph_number
        ]
        lines = []
        for line_number in sorted({row["line"] for row in paragraph_rows}):
            line_rows = sorted(
                (row for row in paragraph_rows if row["line"] == line_number),
                key=lambda row: row["word"],
            )
            lines.append(" ".join(row["text"] for row in line_rows))
        paragraphs.append("\n".join(lines))
    return "\n\n".join(paragraphs).strip()


def _tsv_column_line_text(rows):
    """Rebuild one column from physical line bands, including split blocks."""
    logical_lines = []
    for block, paragraph, line in sorted({
        (row["block"], row["paragraph"], row["line"]) for row in rows
    }):
        line_rows = [
            row for row in rows
            if (row["block"], row["paragraph"], row["line"])
            == (block, paragraph, line)
        ]
        logical_lines.append({
            "top": min(row["top"] for row in line_rows),
            "bottom": max(row["top"] + row["height"] for row in line_rows),
            "rows": list(line_rows),
        })
    logical_lines.sort(key=lambda item: (item["top"], min(row["left"] for row in item["rows"])))
    bands = []
    for logical_line in logical_lines:
        center = (logical_line["top"] + logical_line["bottom"]) / 2
        matching = next((
            band for band in reversed(bands[-3:])
            if abs(center - band["center"]) <= max(
                4,
                min(
                    logical_line["bottom"] - logical_line["top"],
                    band["bottom"] - band["top"],
                ) * .55,
            )
        ), None)
        if matching is None:
            bands.append({
                "top": logical_line["top"],
                "bottom": logical_line["bottom"],
                "center": center,
                "rows": list(logical_line["rows"]),
            })
        else:
            matching["top"] = min(matching["top"], logical_line["top"])
            matching["bottom"] = max(matching["bottom"], logical_line["bottom"])
            matching["center"] = (matching["top"] + matching["bottom"]) / 2
            matching["rows"].extend(logical_line["rows"])
    rendered = []
    for band in sorted(bands, key=lambda item: (item["top"], min(row["left"] for row in item["rows"]))):
        rendered.append(" ".join(
            row["text"] for row in sorted(band["rows"], key=lambda row: (row["left"], row["word"]))
        ))
    return "\n".join(rendered).strip()


def reorder_three_column_ocr_blocks(text, layout_rows, column_evidence, crop_fraction):
    """Put Tesseract blocks into page-column reading order when geometry is strong.

    Tesseract PSM 3 identifies the right blocks but can emit a tall first
    column, then the middle column, then return to small bottom fragments of
    the first column.  Reordering the *same recognized tokens* fixes that
    structural defect without selecting a second OCR candidate or rewriting
    any word.
    """
    rows = [row for row in (layout_rows or []) if isinstance(row, dict)]
    evidence = {
        "method": "tesseract_block_geometry_column_order_v1",
        "applied": False,
        "reason": "layout_words_unavailable",
        "block_count": 0,
    }
    if not rows or not (column_evidence or {}).get("detected"):
        return str(text or ""), evidence
    gutters = list((column_evidence or {}).get("gutters") or [])
    if len(gutters) != 2:
        evidence["reason"] = "two_gutters_unavailable"
        return str(text or ""), evidence
    crop_left, _crop_top, crop_right, _crop_bottom = crop_fraction
    crop_width_fraction = max(float(crop_right) - float(crop_left), .001)
    # The detector's x values are relative to its 4%-96% page sample.
    gutter_positions = [
        ((.04 + .92 * float(row["x_fraction"])) - float(crop_left))
        / crop_width_fraction
        for row in gutters
    ]
    crop_width = max(row.get("crop_width", 0) for row in rows) or max(
        row["left"] + row["width"] for row in rows
    )
    crop_height = max(row.get("crop_height", 0) for row in rows) or max(
        row["top"] + row["height"] for row in rows
    )
    evidence["coordinate_basis"] = (
        "actual_crop" if all(row.get("crop_width") and row.get("crop_height") for row in rows)
        else "legacy_text_extent"
    )
    blocks = []
    for block_number in sorted({row["block"] for row in rows}):
        block_rows = [row for row in rows if row["block"] == block_number]
        left = min(row["left"] for row in block_rows)
        top = min(row["top"] for row in block_rows)
        right = max(row["left"] + row["width"] for row in block_rows)
        bottom = max(row["top"] + row["height"] for row in block_rows)
        center_fraction = ((left + right) / 2) / max(crop_width, 1)
        if center_fraction < gutter_positions[0]:
            column = 0
        elif center_fraction < gutter_positions[1]:
            column = 1
        else:
            column = 2
        blocks.append({
            "block": block_number,
            "left": left,
            "top": top,
            "right": right,
            "bottom": bottom,
            "column": column,
            "top_fraction": top / max(crop_height, 1),
            "text": _tsv_block_text(block_rows),
        })
    evidence["block_count"] = len(blocks)
    nonempty = [block for block in blocks if block["text"]]
    if len(nonempty) < 3:
        evidence["reason"] = "insufficient_text_blocks"
        return str(text or ""), evidence
    substantial_blocks = [block for block in nonempty if len(block["text"].split()) >= 20]
    body_candidates = substantial_blocks or [
        block for block in nonempty if any(character.islower() for character in block["text"])
    ] or nonempty
    body_top = min(block["top"] for block in body_candidates)
    headers = [
        block for block in nonempty
        if block["top_fraction"] < .22
        and (
            block["bottom"] <= body_top
            or (block["top_fraction"] < .075
                and not any(character.islower() for character in block["text"]))
        )
        and (block["bottom"] - block["top"]) / max(crop_height, 1) < .08
    ]
    # A short lower-page block crossing a gutter is a spanning region, not
    # part of whichever column happens to contain its centre. Keep its
    # contained continuation/byline together after the column prose.
    spanning_tails = [
        block for block in nonempty if block not in headers
        and block["top_fraction"] > .70
        and (block["bottom"] - block["top"]) / max(crop_height, 1) < .20
        and any(
            block["left"] < gutter * crop_width - .035 * crop_width
            and block["right"] > gutter * crop_width + .035 * crop_width
            for gutter in gutter_positions
        )
    ]
    tails = [
        block for block in nonempty
        if block not in headers
        and (
            (block["top_fraction"] > .93 and block["column"] != 0)
            or block in spanning_tails
            or any(
                block["top"] >= caption["bottom"]
                and block["left"] >= caption["left"]
                and block["right"] <= caption["right"]
                for caption in spanning_tails
            )
        )
    ]
    evidence["spanning_tail_blocks"] = [block["block"] for block in spanning_tails]
    body = [block for block in nonempty if block not in headers and block not in tails]
    ordered = (
        sorted(headers, key=lambda block: (block["top"], block["left"]))
        + sorted(body, key=lambda block: (block["column"], block["top"], block["left"]))
        + sorted(tails, key=lambda block: (block["top"], block["left"]))
    )
    header_text = [block["text"] for block in ordered if block in headers]
    body_text = []
    for column in range(3):
        column_blocks = [block for block in body if block["column"] == column]
        column_block_numbers = {block["block"] for block in column_blocks}
        column_rows = [row for row in rows if row["block"] in column_block_numbers]
        rendered_column = _tsv_column_line_text(column_rows)
        if rendered_column:
            body_text.append(rendered_column)
    tail_text = [block["text"] for block in ordered if block in tails]
    rebuilt = clean_photographed_ocr_text("\n\n".join(header_text + body_text + tail_text))
    original_words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]+", str(text or ""))
    rebuilt_words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]+", rebuilt)
    # Geometry is allowed to change order, never lexical content.  Requiring
    # the complete case-insensitive multiset is stronger than a word-count
    # tolerance: a coincidental loss and gain cannot cancel each other out.
    # Include numbers and punctuation too: alphabetic-only comparison could
    # silently discard a year, amount, or punctuation-only line.
    lexical_content_preserved = (
        sorted(re.findall(r"\w+|[^\w\s]", str(text or "").casefold()))
        == sorted(re.findall(r"\w+|[^\w\s]", rebuilt.casefold()))
    )
    if not rebuilt or not lexical_content_preserved:
        evidence["reason"] = "rebuilt_lexical_content_changed"
        evidence["original_word_count"] = len(original_words)
        evidence["rebuilt_word_count"] = len(rebuilt_words)
        return str(text or ""), evidence
    evidence.update({
        "applied": True,
        "reason": "strong_three_column_geometry",
        "original_word_count": len(original_words),
        "rebuilt_word_count": len(rebuilt_words),
        "block_order": [block["block"] for block in ordered],
    })
    return rebuilt, evidence


def _ocr_connected_glyph(image, fraction, tesseract, image_ops):
    raw = normalize_text(
        _ocr_photographed_crop(image, fraction, tesseract, image_ops, psm=10)
    )
    letters = re.findall(r"[A-Za-z]", raw)
    nonletters = re.sub(r"[A-Za-z\s]", "", raw)
    if len(letters) == 1 and len(nonletters) <= 2:
        return letters[0].upper(), raw
    return "", raw


def _geometry_drop_cap_completion(content, glyph, token):
    """Choose a joined or standalone initial using only same-page evidence."""
    joined = glyph + token
    joined_elsewhere = bool(re.search(
        rf"(?i)(?<![A-Za-z]){re.escape(joined)}(?![A-Za-z])",
        content,
    ))
    if len(token) > 4 or joined_elsewhere:
        return joined, "joined_initial", ""
    token_occurrences = len(re.findall(
        rf"(?i)(?<![A-Za-z]){re.escape(token)}(?![A-Za-z])",
        content,
    ))
    if glyph in {"A", "I"} and token_occurrences >= 2:
        return f"{glyph} {token}", "standalone_initial", ""
    return "", "", "short_completion_lacks_same_page_lexical_evidence"


def recover_geometry_aligned_drop_caps(
    text, image, crop_fraction, layout_rows, tesseract, image_ops
):
    """Recover only a physically adjacent oversized initial glyph."""
    evidence = {
        "method": "connected_glyph_plus_tesseract_word_geometry_v1",
        "recovered_count": 0,
        "opening_recovered": False,
        "repairs": [],
        "unresolved": [],
    }
    rows = [row for row in (layout_rows or []) if isinstance(row, dict)]
    if not rows:
        evidence["reason"] = "layout_words_unavailable"
        return str(text or ""), evidence
    word_heights = sorted(row["height"] for row in rows if row["height"] > 0)
    median_height = word_heights[len(word_heights) // 2] if word_heights else 0
    # Only these line starts can reach the existing component matcher. Avoid
    # allocating a page-sized component map when there is nothing to match.
    rows = [row for row in rows if (
        row["word"] == 1
        and 2 <= len(re.sub(r"[^A-Za-z]", "", row["text"])) <= 14
        and (re.sub(r"[^A-Za-z]", "", row["text"]).isupper()
             or len(re.sub(r"[^A-Za-z]", "", row["text"])) <= 3)
        and row["height"] <= max(median_height * 1.55, median_height + 8)
    )]
    if not rows:
        evidence["reason"] = "no_eligible_line_starts"
        evidence["candidate_count"] = 0
        return str(text or ""), evidence
    try:
        import cv2
        import numpy as np
    except ImportError:
        evidence["reason"] = "component_runtime_unavailable"
        return str(text or ""), evidence
    page_width, page_height = image.size
    crop_left, crop_top, crop_right, crop_bottom = crop_fraction
    crop_box = (
        int(page_width * crop_left), int(page_height * crop_top),
        int(page_width * crop_right), int(page_height * crop_bottom),
    )
    cropped = image_ops.autocontrast(image.crop(crop_box).convert("L"))
    gray = np.array(cropped)
    mask = (gray < 140).astype("uint8")
    _count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
    components = stats[1:]
    component_x, component_y, component_w, component_h, component_area = components.T
    content = str(text or "")
    # Associate each oversized connected component with only its best-aligned
    # line start.  Without this ownership step one decorative initial could be
    # compared with several nearby rows, producing noisy unresolved warnings
    # even after the correct row had already been repaired.
    component_matches = {}
    for row in rows:
        token = re.sub(r"[^A-Za-z]", "", row["text"])
        if (
            row["word"] != 1
            or not (2 <= len(token) <= 14)
            or (not token.isupper() and len(token) > 3)
            or row["height"] > max(median_height * 1.55, median_height + 8)
        ):
            continue
        token_top = row["top"]
        token_bottom = token_top + row["height"]
        # Apply the same geometric bounds in NumPy before entering Python.
        # Scans contain thousands of ordinary glyphs; only the few components
        # beside this line start can qualify as a missing oversized initial.
        required_height_ratio = 1.8 if token.isupper() else 3.0
        allowed_gap = (max(45, row["height"] * 3) if token.isupper()
                       else max(12, row["height"] * .75))
        gaps = row["left"] - (component_x + component_w)
        overlaps = np.minimum(token_bottom, component_y + component_h) - np.maximum(token_top, component_y)
        eligible = np.flatnonzero(
            (gaps >= 0) & (gaps <= allowed_gap)
            & (overlaps >= np.minimum(row["height"], component_h) * .35)
            & (component_h >= row["height"] * required_height_ratio)
            & (component_w >= row["height"] * .5)
            & (component_area >= max(80, row["height"] * row["height"] * .7))
        )
        for index in eligible:
            component_index = int(index) + 1
            x, y, component_width, component_height, area = components[index]
            component_bottom = y + component_height
            vertical_overlap = min(token_bottom, component_bottom) - max(token_top, y)
            gap = row["left"] - (x + component_width)
            required_height_ratio = 1.8 if token.isupper() else 3.0
            allowed_gap = (
                max(45, row["height"] * 3)
                if token.isupper()
                else max(12, row["height"] * .75)
            )
            if (
                0 <= gap <= allowed_gap
                and vertical_overlap >= min(row["height"], component_height) * .35
                and component_height >= row["height"] * required_height_ratio
                and component_width >= row["height"] * .5
                and area >= max(80, row["height"] * row["height"] * .7)
            ):
                top_alignment = abs(token_top - y)
                baseline_alignment = abs(token_bottom - component_bottom)
                alignment_score = min(top_alignment, baseline_alignment)
                candidate = (
                    float(alignment_score), float(gap), -int(area), row,
                    int(x), int(y), int(component_width), int(component_height),
                )
                previous = component_matches.get(component_index)
                if previous is None or candidate[:3] < previous[:3]:
                    component_matches[component_index] = candidate
    candidates_checked = len(component_matches)
    for (
        _alignment_score, _gap, _negative_area, row,
        x, y, component_width, component_height,
    ) in component_matches.values():
        token = re.sub(r"[^A-Za-z]", "", row["text"])
        # The conservative policy never rewrites mixed/lowercase suffixes:
        # their spacing is ambiguous. Retain that decision before spending
        # another OCR request on a glyph that cannot change the outcome.
        if not token.isupper():
            evidence["unresolved"].append({
                "token": token,
                "reason": "lowercase_suffix_spacing_ambiguous",
                "glyph_ocr_skipped": True,
            })
            continue
        pad_x = max(3, int(component_height * .16))
        pad_y = max(3, int(component_height * .12))
        glyph_fraction = (
            max(0.0, (crop_box[0] + x - pad_x) / page_width),
            max(0.0, (crop_box[1] + y - pad_y) / page_height),
            min(1.0, (crop_box[0] + x + component_width + pad_x) / page_width),
            min(1.0, (crop_box[1] + y + component_height + pad_y) / page_height),
        )
        glyph, raw_glyph = _ocr_connected_glyph(
            image, glyph_fraction, tesseract, image_ops
        )
        if not glyph:
            evidence["unresolved"].append({
                "token": token,
                "reason": "component_not_one_glyph",
                "raw_glyph": raw_glyph,
            })
            continue
        # Tesseract can preserve the oversized glyph as its own token while
        # also reporting the following word geometry.  In that case there is
        # no missing initial to recover (for example ``I GOT`` must not be
        # treated as a failed attempt to form ``IGOT``).
        if re.search(
            rf"(?i)(?<![A-Za-z]){re.escape(glyph)}\s+{re.escape(token)}(?![A-Za-z])",
            content,
        ):
            continue
        completed_token, repair_mode, unresolved_reason = (
            _geometry_drop_cap_completion(content, glyph, token)
        )
        if unresolved_reason:
            evidence["unresolved"].append({
                "token": token,
                "glyph": glyph,
                "completed_token": glyph + token,
                "reason": unresolved_reason,
            })
            continue
        pattern = re.compile(rf"(?<![A-Za-z]){re.escape(token)}(?![A-Za-z])")
        matches = list(pattern.finditer(content))
        if len(matches) != 1:
            evidence["unresolved"].append({
                "token": token,
                "glyph": glyph,
                "reason": "text_token_not_unique",
                "occurrences": len(matches),
            })
            continue
        content = pattern.sub(completed_token, content, count=1)
        repair = {
            "glyph": glyph,
            "observed_token": token,
            "completed_token": completed_token,
            "repair_mode": repair_mode,
            "block": row["block"],
            "top_fraction": round(row["top"] / max(cropped.height, 1), 4),
        }
        evidence["repairs"].append(repair)
        evidence["recovered_count"] += 1
        if repair["top_fraction"] <= .18:
            evidence["opening_recovered"] = True
    evidence["candidate_count"] = candidates_checked
    evidence["reason"] = (
        "one_or_more_geometry_aligned_initials_recovered"
        if evidence["recovered_count"]
        else "no_unambiguous_geometry_aligned_initial"
    )
    return content, evidence


def recover_missing_display_regions(text, image, fraction, rows, tesseract, image_ops, *, page_number=None):
    """Recover at most three omitted title lines, never re-OCR ordinary prose.

    Only sparse uppercase opening leaves qualify. Pixel bands must contain
    substantial word-shaped ink and have no recognized word overlapping
    them. Existing text is untouched; a unique following TSV line anchors each
    insertion. This is bounded missing-region recovery, not candidate voting.
    """
    evidence = {"method": "missing_display_bands_v1", "recovered_count": 0, "regions": []}
    content = str(text or "")
    letters = [c for c in content if c.isalpha()]
    if (not rows or not page_number or page_number > 2 or not 5 <= len(content.split()) <= 45
            or not letters or sum(c.isupper() for c in letters) / len(letters) < .9):
        evidence["reason"] = "not_sparse_uppercase_opening"
        return content, evidence
    try:
        import cv2
        import numpy as np
    except ImportError:
        evidence["reason"] = "component_runtime_unavailable"
        return content, evidence
    width, height = image.size
    crop = image.crop(tuple(
        int(value * dimension)
        for value, dimension in zip(fraction, (width, height, width, height))
    )).convert("L")
    mask = (np.asarray(crop) < 160).astype("uint8")
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    valid = np.zeros(count, dtype=bool)
    for index, (x, y, component_width, component_height, area) in enumerate(stats):
        if (index and area >= 20 and component_width >= 3 and component_height >= 8
                and x > crop.width * .025 and x + component_width < crop.width * .975):
            valid[index] = True
    ink = valid[np.asarray(labels, dtype=np.int32)]
    active = np.count_nonzero(ink, axis=1) >= 8
    starts = np.flatnonzero(active & ~np.r_[False, active[:-1]])
    ends = np.flatnonzero(active & ~np.r_[active[1:], False]) + 1
    bands = []
    for y1, y2 in zip(starts, ends):
        if y2 - y1 < 15 or y2 > crop.height * .45:
            continue
        if any(min(y2,r["top"]+r["height"])-max(y1,r["top"]) > min(y2-y1,r["height"])*.25 for r in rows):
            continue
        components = [index for index in np.flatnonzero(valid)
                      if stats[index, 1] >= y1 and stats[index, 1] + stats[index, 3] <= y2]
        xs = np.flatnonzero(np.any(ink[y1:y2], axis=0))
        # Touching display letters can be one component (e.g. tightly set AS).
        # Keep only a word-shaped band, not a narrow isolated initial/stroke.
        if len(components) < 2 and (not components or xs[-1] - xs[0] + 1 < (y2 - y1) * 1.15):
            continue
        bands.append((int(y1),int(y2),int(xs[0]),int(xs[-1])+1))
    if not 1 <= len(bands) <= 3:
        evidence["reason"] = "no_bounded_missing_bands"
        return content, evidence
    insertions = {}
    for y1, y2, x1, x2 in bands:
        following = [row for row in rows if row["top"] >= y2]
        if not following:
            continue
        anchor=min(following,key=lambda r:(r["top"],r["left"]))
        line=sorted([r for r in rows if (r["block"],r["paragraph"],r["line"]) == (anchor["block"],anchor["paragraph"],anchor["line"])],key=lambda r:r["word"])
        pattern=r"\s+".join(re.escape(r["text"]) for r in line)
        matches = list(re.finditer(pattern, content))
        if len(matches) != 1:
            continue
        region=(max(0,(x1-8)/crop.width),max(0,(y1-8)/crop.height),min(1,(x2+8)/crop.width),min(1,(y2+8)/crop.height))
        recovered = _ocr_photographed_crop(crop, region, tesseract, image_ops, psm=7).strip()
        # Reject noise and uncertain prose, retaining the old output verbatim.
        if not re.fullmatch(r"[A-Z][A-Z '’\-]{1,79}", recovered):
            continue
        insertions.setdefault(matches[0].start(),[]).append(recovered)
        evidence["regions"].append({"crop_relative_fraction":list(region),"word_count":len(recovered.split()),"psm":7})
    for position, additions in sorted(insertions.items(), reverse=True):
        content = content[:position] + "\n\n".join(additions) + "\n\n" + content[position:]
    evidence["recovered_count"] = len(evidence["regions"])
    evidence["reason"] = "missing_regions_recovered" if insertions else "missing_regions_unresolved"
    return content, evidence


def _ocr_photographed_crop(image, fraction, tesseract, image_ops, *, psm=4, recognition_evidence=None, enhance_annotated_prose=False):
    """OCR one visual region, retaining only text suitable for preparation."""
    setup_started = time.perf_counter()
    width, height = image.size
    left, top, right, bottom = fraction
    crop_box = (int(width * left), int(height * top), int(width * right), int(height * bottom))
    # Auto-contrast restores black glyphs laid over yellow highlighter while
    # keeping the source crop and page identity unchanged.
    cropped = image_ops.autocontrast(image.crop(crop_box).convert("L"))
    psm, model_args, decision = _resolve_ocr_recognition(image.crop(crop_box), psm)
    decision["recognition_raster_scale"] = 1.0
    if enhance_annotated_prose and decision.get("requested_psm") == 4 and psm == 6 and model_args:
        # Only confirmed-spread prose opts in. Auxiliary/reference OCR and
        # TSV geometry keep their established raster. Resolve layout once;
        # preserve the full fractional bounds, without another OCR pass.
        if width * height <= 8_000_000:
            try:
                from PIL import Image
                enlarged = image.resize((round(width * 1.5), round(height * 1.5)), Image.Resampling.LANCZOS)
                enlarged_box = tuple(int(value * size) for value, size in zip(
                    fraction, (enlarged.width, enlarged.height, enlarged.width, enlarged.height)
                ))
                cropped = image_ops.autocontrast(enlarged.crop(enlarged_box).convert("L"))
                decision["recognition_raster_scale"] = 1.5
            except (MemoryError, OSError, ValueError) as exc:
                decision["raster_enhancement_fallback"] = type(exc).__name__
        else:
            decision["raster_enhancement_fallback"] = "raster_budget_preserve_original"
    if recognition_evidence is not None:
        recognition_evidence.clear()
        recognition_evidence.update(decision, crop_fraction=list(fraction))
        recognition_evidence["setup_seconds"] = round(time.perf_counter() - setup_started, 4)
    with tempfile.TemporaryDirectory(prefix="rag-photographed-page-") as temp_dir:
        image_path = Path(temp_dir) / "page.png"
        cropped.save(image_path)
        try:
            completed = _run_measured_tesseract(
                [tesseract, str(image_path), "stdout", *model_args, "--psm", str(int(psm)), "-l", "eng"],
                recognition_evidence,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
    if completed.returncode != 0:
        return ""
    return clean_photographed_ocr_text(completed.stdout)


def photographed_ocr_text_quality(text):
    """Return narrow lexical evidence for choosing between OCR crops.

    This is not a language-quality score and never rewrites words. It exists
    only to notice a crop that returned a small collection of line fragments
    while a bounded full-page retry recovered substantially more coherent
    material from the same source page.
    """
    content = str(text or "")
    words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]+(?:['’-][A-Za-zÀ-ÖØ-öø-ÿ]+)?", content)
    short_words = sum(1 for word in words if len(word) <= 2)
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    fragment_lines = sum(
        1
        for line in lines
        if len(re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]+", line)) <= 3
    )
    return {
        "word_count": len(words),
        "line_count": len(lines),
        "short_word_ratio": round(short_words / max(len(words), 1), 4),
        "fragment_line_ratio": round(fragment_lines / max(len(lines), 1), 4),
    }


def embedded_scan_crop_needs_full_page_retry(crop_fraction, quality, crop_boundary=None):
    """Retry only weak OCR from a suspiciously clipped embedded image box."""
    left, top, right, bottom = crop_fraction
    clipped_wrapper = (right - left) < .82 or (bottom - top) < .82
    weak_recovery = (
        quality["word_count"] < 180
        or quality["fragment_line_ratio"] >= .35
    )
    boundary_clipping = bool((crop_boundary or {}).get("ink_touches_boundary"))
    return bool(clipped_wrapper and (weak_recovery or boundary_clipping))


def credible_short_ocr_display_text(text):
    """Accept a short title/part leaf only when its OCR shape is unambiguous."""
    content = normalize_text(text)
    words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]+", content)
    letters = "".join(words)
    if not (2 <= len(words) <= 12 and 7 <= len(letters) <= 100):
        return False
    if len(content.splitlines()) > 5:
        return False
    uppercase = letters == letters.upper()
    title_case = sum(word[:1].isupper() for word in words) >= max(2, len(words) - 1)
    return bool(uppercase or title_case)


def full_page_ocr_retry_materially_better(embedded_quality, full_quality):
    """Select a retry only when it clearly recovers omitted page content."""
    return bool(
        full_quality["word_count"] >= max(
            embedded_quality["word_count"] + 60,
            math.ceil(embedded_quality["word_count"] * 1.45),
        )
        and full_quality["fragment_line_ratio"]
        <= embedded_quality["fragment_line_ratio"] + .10
    )


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
    regions = photographed_page_ocr_regions(
        page, runtime, page_number=page_index + 1
    )
    if not regions:
        return None
    page_number = page_index + 1
    runover_candidate = regions[0].pop("_neighbour_page_runover_candidate", None)
    region_methods = [str(region.get("ocr_method") or "") for region in regions]
    if region_methods and all(
        method == "tesseract_photographed_spread_crop" for method in region_methods
    ):
        selected_route = "confirmed_fold_spread_split"
    elif "tesseract_embedded_three_column_document_crop" in region_methods:
        selected_route = "embedded_three_column_document"
    elif "tesseract_embedded_scanned_document_crop" in region_methods:
        selected_route = "embedded_scan_bounds"
    else:
        selected_route = "photographed_page_inner_crop"
    combined_text = "\n\n".join(region["text"] for region in regions)
    page_row = {
        "page": page_number,
        "text": combined_text,
        "kind": "photographed_page_crop_ocr",
        "reading_regions": regions,
        "spread_preprocessing": {
            "status": "applied",
            "path": selected_route,
            "source_pdf_page": page_number,
            "logical_page_preserved": True,
            "annotations_excluded": "outer_margin_crop",
            "selection_policy": "deterministic_page_evidence_with_bounded_recovery",
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
                "chars": len(combined_text),
                "preview": normalize_text(combined_text)[:250],
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
    completed_page_callback=None,
    resolve_neighbours=True,
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
            checkpoint_groups = _consecutive_ocr_page_groups(expected_pages)
            group_position = 0
            active_group = checkpoint_groups[0]
            next_page_position = 0

            def submit_next_page():
                nonlocal next_page_position
                source_page_number = active_group[next_page_position]
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

            while len(pending) < active_worker_count and next_page_position < len(active_group):
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
                    if callable(completed_page_callback):
                        completed_page_callback(
                            result["page_row"], result["element_rows"], page_count
                        )
                    if callable(progress_callback):
                        progress_callback(len(pages), len(expected_pages))
                    while len(pending) < active_worker_count and next_page_position < len(active_group):
                        submit_next_page()
                if not pending and group_position + 1 < len(checkpoint_groups):
                    group_position += 1
                    active_group = checkpoint_groups[group_position]
                    next_page_position = 0
                    while len(pending) < active_worker_count and next_page_position < len(active_group):
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
        except BaseException:
            # Callback/checkpoint/submission failures must not enter a waiting
            # shutdown while sibling native OCR workers may be hung. Preserve
            # the original exception and use the existing owned-pool cleanup.
            if not terminated:
                _terminate_unstructured_executor(executor)
                terminated = True
            raise
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
    if resolve_neighbours:
        resolve_confirmed_neighbour_runovers(pages)
    for page in pages:
        page["unstructured_execution"] = {
            "mode": "isolated_parallel_pages",
            "requested_workers": int(worker_count),
            "actual_workers": min(int(worker_count), len(expected_pages)),
            "strategy": strategy,
            "page_scope": "targeted_visual_text_pages" if requested_pages else "whole_document",
            "targeted_page_numbers": requested_pages or [],
            "checkpoint_group_size": unstructured_ocr_page_group_size(),
            "checkpoint_group_count": len(_consecutive_ocr_page_groups(expected_pages)),
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
    checkpoint_dir=None,
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
        cached = load_unstructured_ocr_checkpoint(
            pdf_path,
            resolved_strategy,
            runtime,
            checkpoint_dir,
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
        cached_page_results = {}
        if target_page_numbers and checkpoint_dir:
            for page_number in target_page_numbers:
                page_cached = load_unstructured_ocr_checkpoint(
                    pdf_path, resolved_strategy, runtime, checkpoint_dir,
                    page_numbers=[page_number],
                )
                if page_cached is not None:
                    cached_page_results[page_number] = page_cached
            missing_target_pages = [
                page for page in target_page_numbers if page not in cached_page_results
            ]
            if not missing_target_pages:
                pages = [cached_page_results[page][0][0] for page in target_page_numbers]
                resolve_confirmed_neighbour_runovers(pages)
                element_rows = [
                    row for page in target_page_numbers for row in cached_page_results[page][2]
                ]
                return pages, source_page_count, element_rows
        else:
            missing_target_pages = target_page_numbers
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
            cache_path = save_unstructured_ocr_checkpoint(
                pdf_path,
                resolved_strategy,
                runtime,
                checkpoint_dir,
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
            cached_page_count = len(cached_page_results)

            def checkpoint_completed_page(page_row, page_elements, physical_page_count):
                save_unstructured_ocr_checkpoint(
                    pdf_path, resolved_strategy, runtime, checkpoint_dir,
                    [page_row], physical_page_count, page_elements,
                    page_numbers=[int(page_row.get("page") or 0)],
                )

            def report_fresh_progress(completed, total):
                if callable(progress_callback):
                    progress_callback(cached_page_count + completed, cached_page_count + total)

            fresh_pages, page_count, fresh_element_rows = _parallel_unstructured_ocr_pages(
                pdf_path,
                source_page_count,
                resolved_strategy,
                workers,
                runtime,
                page_numbers=missing_target_pages,
                progress_callback=report_fresh_progress,
                completed_page_callback=checkpoint_completed_page,
                resolve_neighbours=False,
            )
            pages = [
                *(cached_page_results[page][0][0] for page in sorted(cached_page_results)),
                *fresh_pages,
            ]
            pages.sort(key=lambda row: int(row.get("page") or 0))
            resolve_confirmed_neighbour_runovers(pages)
            element_rows = [
                *(row for page in sorted(cached_page_results) for row in cached_page_results[page][2]),
                *fresh_element_rows,
            ]
            element_rows.sort(key=lambda row: (int(row.get("pdf_page") or 0), int(row.get("element_index") or 0)))
            cache_path = save_unstructured_ocr_checkpoint(
                pdf_path,
                resolved_strategy,
                runtime,
                checkpoint_dir,
                pages,
                page_count,
                element_rows,
                page_numbers=target_page_numbers,
            )
            if cache_path:
                for page in pages:
                    page.setdefault("unstructured_execution", {
                        "mode": "page_local_checkpoint_assembly",
                        "requested_workers": int(workers),
                        "actual_workers": min(int(workers), len(target_page_numbers or [])),
                        "strategy": resolved_strategy,
                        "page_scope": "targeted_visual_text_pages" if target_page_numbers else "whole_document",
                        "targeted_page_numbers": target_page_numbers or [],
                    })["cache_path"] = cache_path
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
        cache_path = save_unstructured_ocr_checkpoint(
            pdf_path, resolved_strategy, runtime, checkpoint_dir, pages, page_count, element_rows
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
    unstructured_checkpoint_dir=None,
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
            checkpoint_dir=unstructured_checkpoint_dir,
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

"""Canonical PDF preparation and optional AnythingLLM integration pipeline.

``prepare_pdf`` is the public preparation boundary used by the CLI, the Gradio
application, and the orchestration façade.  The implementation remains large
because it preserves established artifact and integration contracts while the
project is being decomposed.  The private function containing most of the
work still has a ``legacy`` name for compatibility, not because callers should
choose an obsolete alternative.

Keep the following evidence layers separate when editing this module: local
PDF preparation, native upload acceptance, persisted storage inspection,
runtime retrieval, and answer/citation quality.  A successful earlier layer
does not prove a later one.
"""

import argparse
import csv
import concurrent.futures
import hashlib
import html
import io
import json
import math
import mimetypes
import os
import random
import re
import shutil
import sqlite3
import statistics
import subprocess
import tempfile
import time
import threading
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

from portable_paths import application_paths
from typing import Any, cast

import fitz
from embedder_capabilities import (
    UNKNOWN_EMBEDDER_LIMIT,
    openrouter_simulation_option_map,
    resolve_embedder_capability,
)
from anythingllm_state import resolve_state as resolve_authoritative_anythingllm_state
from segmentation_policy import policy_for as segmentation_policy_for
from orchestration import execute_preparation
from semantic_segmentation import detect_page_transition, split_semantic_page
from post_upload_polling import poll_post_upload
from validation_contract import (
    REVIEWABLE_POST_UPLOAD_STATUSES,
    SUCCESSFUL_POST_UPLOAD_STATUSES,
    SUCCESSFUL_UPLOAD_STATUSES,
    evidence_layers_succeeded,
)

from rag_pdf_tools import (
    DEFAULT_END_SECTION_HEADINGS,
    detect_end_section_start,
    get_backend_pages,
    normalize_text,
    pymupdf4llm_execution_evidence,
    pymupdf4llm_ocr_page_workers,
    safe_stem,
    unstructured_execution_evidence,
    unstructured_runtime_status,
)

# Native uploads have a long, externally observable embedding portion.  Keep
# the declared pipeline ranges monotonic so the UI never has to hide a real
# regression (the previous 0.82 -> 0.60 transition made the display stall and
# then jump late in the run).
PIPELINE_PROGRESS_STORAGE_INSPECTION = 0.78
PIPELINE_PROGRESS_EMBEDDING_START = 0.80
PIPELINE_PROGRESS_EMBEDDING_END = 0.94
PIPELINE_PROGRESS_POST_UPLOAD_OBSERVATION = 0.95
PIPELINE_PROGRESS_REPORTING = 0.97
ANYTHINGLLM_HTTP_RESPONSE_TIMEOUT_SECONDS = 180

# Upload-mode progress is a user-facing evidence scale, not the old generic
# pipeline's anonymous 0..1 values.  The eight-PDF medium-file benchmark put
# setup plus local preparation below ten percent of elapsed time; the
# Desktop-owned queue and exact page-parent confirmation dominated it.  Keep
# the detailed units visible so a user can see *what* is progressing rather
# than collapsing that evidence into one "upload" stage. The queue and vector
# observer can overlap in wall-clock time; the allocation is a useful,
# monotonic explanation of owned evidence, not a claim that they are serial.
AUTOMATIC_UPLOAD_PHASE_RANGES = {
    "metadata": (0.0000, 0.0130),
    "extraction": (0.0130, 0.0415),
    "candidate_evaluation": (0.0415, 0.0518),
    "payloads": (0.0518, 0.0674),
    "attachments": (0.0674, 0.0985),
    "queue_receipt": (0.0985, 0.1192),
    "desktop_queue": (0.1192, 0.5855),
    "identity_set": (0.5855, 0.8549),
    "retrieval_sample": (0.8549, 0.9275),
    "validation": (0.9275, 0.9793),
    "reporting": (0.9793, 1.0000),
}


class UploadPhaseReporter:
    """Translate phase evidence into the single Automatic-run progress scale.

    This isolates presentation/evidence mapping from the legacy preparation
    engine. Local preparation, upload, and final reporting can now share the
    same monotonic contract without the engine knowing UI range arithmetic.
    """

    def __init__(self, report_progress, prepare_and_upload):
        self._report_progress = report_progress
        self._prepare_and_upload = bool(prepare_and_upload)

    def emit(
        self,
        phase,
        stage,
        *,
        completed_units=None,
        total_units=None,
        fallback_fraction=0.0,
        desktop_required=False,
        evidence_kind="measured",
    ):
        if not self._prepare_and_upload:
            return self._report_progress(fallback_fraction, stage, desktop_required=desktop_required)
        start, end = AUTOMATIC_UPLOAD_PHASE_RANGES[str(phase)]
        try:
            completed = max(0.0, float(completed_units or 0.0))
            total = max(0.0, float(total_units or 0.0))
            fraction = min(1.0, completed / total) if total else max(0.0, min(1.0, fallback_fraction))
        except (TypeError, ValueError):
            fraction = max(0.0, min(1.0, fallback_fraction))
        return self._report_progress(
            start + (end - start) * fraction,
            stage,
            desktop_required=desktop_required,
            phase=phase,
            completed_units=completed_units,
            total_units=total_units,
            evidence_kind=evidence_kind,
        )


def emit_pipeline_timing_event(args, stage, *, event="phase_completed", elapsed_seconds=0.0, **details):
    """Best-effort timing telemetry that can never fail a preparation run.

    The UI observer is useful, but it is not part of the upload or extraction
    correctness boundary.  In particular, an exception in a progress callback
    must not turn a successfully accepted native batch into a failed PDF.
    """
    callback = getattr(args, "timing_event_callback", None)
    if not callable(callback):
        return False
    payload = {
        "timing_event": str(event or "phase_completed"),
        "phase_elapsed_seconds": round(max(0.0, float(elapsed_seconds or 0.0)), 3),
        "run_id": str(getattr(args, "run_id", "") or ""),
        "correlation_id": str(getattr(args, "correlation_id", "") or ""),
        **details,
    }
    try:
        callback(str(stage or "Working"), payload)
        return True
    except Exception:
        # This intentionally avoids re-raising or performing another callback.
        # The durable pipeline output remains the source of truth.
        return False


@contextmanager
def measured_pipeline_phase(args, stage, **details):
    """Record a monotonic, high-resolution phase span without changing work."""
    started = time.perf_counter()
    emit_pipeline_timing_event(args, stage, event="phase_started", **details)
    try:
        yield
    finally:
        emit_pipeline_timing_event(
            args,
            stage,
            event="phase_completed",
            elapsed_seconds=time.perf_counter() - started,
            **details,
        )


def _inspection_fingerprint(storage_dir: Path, workspace_slug: str, api_url: str):
    """Identify an inspection lane without invalidating on this run's uploads.

    SQLite/Lance timestamps necessarily change as AnythingLLM embeds the prior
    PDF. Including them here would defeat the cache exactly when a batch is
    active. A context never survives a run; its initial snapshot remains an
    intentionally fixed before-state, while mutable evidence is verified per
    upload and audited once at the end.
    """
    return (
        str(Path(storage_dir).resolve()),
        str(workspace_slug or "").strip(),
        str(api_url or "").strip(),
    )


def get_batch_inspection_context(args, storage_dir: Path, workspace_slug: str):
    """Return a caller-shared cache of immutable read-only inspection results.

    The cache is deliberately scoped to the current batch/run.  It never
    carries mutable document/vector evidence between batches or app starts.
    """
    context = getattr(args, "batch_inspection_context", None)
    if not isinstance(context, dict):
        context = {}
        try:
            setattr(args, "batch_inspection_context", context)
        except Exception:
            return context, False
    fingerprint = _inspection_fingerprint(
        storage_dir,
        workspace_slug,
        getattr(args, "anythingllm_api_url", ""),
    )
    reused = context.get("fingerprint") == fingerprint and bool(context.get("global_read_only"))
    if not reused:
        # Configuration/runtime resolution happens earlier in the PDF path
        # than this inspection initializer. Preserve those batch-global
        # preflight observations when establishing the first before-state;
        # otherwise the first document would accidentally discard them and
        # the second would repeat their localhost reads.
        preserved_preflight = {
            key: context[key]
            for key in (
                "anythingllm_preparation_config",
                "resolved_anythingllm_runtime_state",
                "anythingllm_runtime_embedder_probe",
            )
            if key in context
        }
        context.clear()
        context.update(preserved_preflight)
        context["fingerprint"] = fingerprint
        context["global_read_only"] = {}
        context["inspection_dirs"] = []
    return context, reused


OPENING_HEADINGS = [
    "Introduction",
    "Preface",
    "Prologue",
    "Chapter One",
    "Chapter 1",
    "Part One",
    "Part 1",
    "Foreword",
    "Contents",
    "Table of Contents",
]

FRONT_MATTER_TITLES = [
    "cover",
    "title",
    "copyright",
    "dedication",
    "contents",
    "table of contents",
    "acknowledgments",
    "acknowledgements",
]

MAIN_BODY_TITLES = [
    "introduction",
    "chapter",
    "one.",
    "chapter one",
    "chapter 1",
    "part one",
    "part 1",
    "prologue",
]

OUTLINE_IGNORE_VALIDATION_TITLES = [
    "cover",
    "title",
    "copyright",
    "dedication",
]

CHAPTER_RE = re.compile(
    r"^\s*(?:\[[^\]\n]+\]\s*)?(?:#{1,6}\s*)?(?:[*_`~\s])*"
    r"((?:CHAPTER|Chapter)\s+(?:\d+|[A-Z]+|[A-Za-z]+)|"
    r"(?:ONE|TWO|THREE|FOUR|FIVE|SIX|SEVEN|EIGHT|NINE|TEN)\.|"
    r"(?:Introduction|Preface|Foreword|Prologue|Conclusion)\b|"
    r"(?:PART|Part)\s+(?:\d+|[A-Z]+|[A-Za-z]+))"
)

SECTION_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:[*_`~\s])*([A-Z][A-Za-z0-9 ,:;'\u2019\u2013\u2014-]{4,90})(?:[*_`~\s])*$"
)

HEADING_STOPWORDS = {
    "chapter",
    "part",
    "book",
    "section",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "the",
    "and",
    "of",
    "to",
    "in",
    "for",
    "on",
    "with",
    "from",
    "at",
    "against",
    "a",
    "an",
}

PIPELINE_VERSION = "2.0.0"
PROJECT_LOCAL_ENV_PATH = application_paths()["config"] / ".env"
DEFAULT_OPENROUTER_EMBEDDINGS_URL = "https://openrouter.ai/api/v1/embeddings"
DEFAULT_ANYTHINGLLM_API_URL = "http://127.0.0.1:3001"
LOCAL_DESKTOP_SERVICE_API_KEY_NAME = "PDF Assistant localhost service key"  # pragma: allowlist secret -- display label, not a credential
ANYTHINGLLM_API_CANDIDATE_URLS = (
    "http://127.0.0.1:3001",
    "http://127.0.0.1:8888",
    "http://localhost:3001",
    "http://localhost:8888",
)
DEFAULT_ANYTHINGLLM_UPLOAD_FOLDER_PREFIX = "rag-native-uploads"
DEFAULT_ANYTHINGLLM_STARTUP_TIMEOUT_SECONDS = 45.0
DEFAULT_OPENROUTER_TIMEOUT_SECONDS = 45
OPENROUTER_SLOW_REQUEST_THRESHOLD_MS = 8000
OPENROUTER_SIMULATION_OPTIONS = openrouter_simulation_option_map()
ANYTHINGLLM_PROVIDER_MODEL_KEYS = {
    "anythingllm": ["ANYTHINGLLM_MODEL_PREF", "EMBEDDING_MODEL_PREF"],
    "built-in": ["ANYTHINGLLM_MODEL_PREF", "EMBEDDING_MODEL_PREF"],
    "default": ["ANYTHINGLLM_MODEL_PREF", "EMBEDDING_MODEL_PREF"],
    "native": ["ANYTHINGLLM_MODEL_PREF", "EMBEDDING_MODEL_PREF"],
    "ollama": ["OLLAMA_MODEL_PREF"],
    "openrouter": ["OPENROUTER_MODEL_PREF"],
    "openai": ["OPENAI_MODEL_PREF"],
    "generic-openai": ["GENERIC_OPEN_AI_MODEL_PREF"],
    "azure-openai": ["AZURE_OPENAI_MODEL_PREF"],
    "cohere": ["COHERE_MODEL_PREF"],
    "voyageai": ["VOYAGE_MODEL_PREF"],
    "voyage": ["VOYAGE_MODEL_PREF"],
    "mistral": ["MISTRAL_MODEL_PREF"],
    "gemini": ["GEMINI_MODEL_PREF"],
    "litellm": ["LITELLM_MODEL_PREF"],
    "lmstudio": ["LMSTUDIO_MODEL_PREF"],
    "lm-studio": ["LMSTUDIO_MODEL_PREF"],
    "localai": ["LOCALAI_MODEL_PREF"],
    "lemonade": ["LEMONADE_MODEL_PREF"],
    "jinaai": ["JINAAI_MODEL_PREF"],
}
ANYTHINGLLM_PROVIDER_KEY_FIELDS = {
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY",
    "generic-openai": "GENERIC_OPEN_AI_API_KEY",
    "azure-openai": "AZURE_OPENAI_API_KEY",
    "cohere": "COHERE_API_KEY",
    "voyageai": "VOYAGE_API_KEY",
    "voyage": "VOYAGE_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "litellm": "LITELLM_API_KEY",
    "lmstudio": "",
    "lm-studio": "",
    "localai": "",
    "lemonade": "",
    "ollama": "",
}
ANYTHINGLLM_SUPPORTED_EMBEDDER_ENGINES = {
    "anythingllm",
    "built-in",
    "default",
    "native",
    "ollama",
    "openrouter",
    "openai",
    "generic-openai",
    "azure-openai",
    "cohere",
    "voyageai",
    "voyage",
    "mistral",
    "gemini",
    "litellm",
    "lmstudio",
    "lm-studio",
    "localai",
    "lemonade",
    "jinaai",
}
ANYTHINGLLM_LOCALLY_VERIFIED_ENGINES = {
    "openrouter",
    "ollama",
}
ANYTHINGLLM_CLOUD_ONLY_UNSUPPORTED_SIMULATION_ENGINES = {
    "openai",
    "generic-openai",
    "azure-openai",
    "cohere",
    "voyageai",
    "voyage",
    "mistral",
    "gemini",
    "litellm",
}
AUTHOR_INFERENCE_SAMPLE_PAPERS = [
    {
        "file": "bert.pdf",
        "url": "https://arxiv.org/pdf/1810.04805.pdf",
        "title_hint": "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding",
        "expected_contains": ["Jacob Devlin", "Kristina Toutanova"],
    },
    {
        "file": "dpr.pdf",
        "url": "https://aclanthology.org/2020.emnlp-main.550.pdf",
        "title_hint": "Dense Passage Retrieval for Open-Domain Question Answering",
        "expected_contains": ["Vladimir Karpukhin", "Danqi Chen"],
    },
    {
        "file": "transformer.pdf",
        "url": "https://arxiv.org/pdf/1706.03762.pdf",
        "title_hint": "Attention Is All You Need",
        "expected_contains": ["Ashish Vaswani", "Jakob Uszkoreit"],
    },
    {
        "file": "clip.pdf",
        "url": "https://proceedings.mlr.press/v139/radford21a/radford21a.pdf",
        "title_hint": "Learning Transferable Visual Models From Natural Language Supervision",
        "expected_contains": ["Alec Radford", "Ilya Sutskever"],
    },
]
ANYTHINGLLM_RAW_TEXT_METADATA_FIELDS = {
    "url": "Accepted only as an HTTP(S) URL; otherwise AnythingLLM derives a file URL.",
    "title": "Preserved after slugging with a .txt suffix and used as sourceDocument in native chunk headers.",
    "docAuthor": "Preserved in document and vector metadata, but not prepended to chunk text.",
    "description": "Preserved in document and vector metadata, but not prepended to chunk text.",
    "docSource": "Preserved in document and vector metadata, but not prepended to chunk text.",
    "chunkSource": "Preserved; only link:// and youtube:// values become a native source header.",
    "published": "Preserved and prepended to chunk text by the native chunk header.",
}
ANYTHINGLLM_SOURCE_CONTRACT = {
    "raw_text_processor": "https://github.com/Mintplex-Labs/anything-llm/blob/master/collector/processRawText/index.js",
    "text_splitter": "https://github.com/Mintplex-Labs/anything-llm/blob/master/server/utils/TextSplitter/index.js",
    "lancedb_provider": "https://github.com/Mintplex-Labs/anything-llm/blob/master/server/utils/vectorDbProviders/lance/index.js",
}
AUTHOR_ROLE_HINTS = {
    "author",
    "authors",
    "editor",
    "editors",
    "by",
    "written by",
    "edited by",
}
AUTHOR_STOP_TERMS = {
    "chapter",
    "contents",
    "copyright",
    "preface",
    "foreword",
    "introduction",
    "conclusion",
    "notes",
    "bibliography",
    "index",
    "university press",
    "press books",
    "duke university press",
    "yale university press",
    "oxford university press",
    "acknowledgments",
    "acknowledgements",
    "paperback edition",
    "view crossmark data",
    "article views",
    "view related articles",
    "submit your article",
    "to cite this article",
    "to link to this article",
    "full terms conditions of access and use",
    "stable url",
    "published by",
    "journal homepage",
    "proquest ebook central",
    "jstor",
    "terms conditions of use",
    "syllabus",
}
AUTHOR_BLOCK_STOP_HINTS = {
    "abstract",
    "introduction",
    "keywords",
    "contents",
    "table of contents",
    "to cite this article",
    "to link to this article",
    "published online",
    "submit your article",
    "article views",
    "view related articles",
    "view crossmark data",
    "full terms",
    "journal homepage",
    "issn:",
    "doi:",
    "stable url",
    "published by",
    "source:",
    "jstor",
    "proquest ebook central",
    "instructor:",
}

AUTHOR_NAME_PARTICLES = {
    "al",
    "bin",
    "da",
    "de",
    "del",
    "den",
    "der",
    "di",
    "du",
    "el",
    "la",
    "le",
    "ten",
    "van",
    "von",
}
AUTHOR_AFFILIATION_HINTS = {
    "university",
    "institute",
    "school",
    "department",
    "college",
    "laboratory",
    "lab",
    "research",
    "google",
    "facebook",
    "microsoft",
    "openai",
    "anthropic",
    "amazon",
    "meta",
}

AUTHOR_ORGANIZATION_TERMS = {
    "academy",
    "association",
    "center",
    "centre",
    "college",
    "committee",
    "department",
    "foundation",
    "inc",
    "institute",
    "journal",
    "laboratory",
    "llc",
    "ltd",
    "office",
    "press",
    "publisher",
    "school",
    "society",
    "systems",
    "university",
}


@dataclass
class PageStat:
    pdf_page: int
    chars: int
    words: int
    replacement_chars: int
    sentence_marks: int
    line_count: int
    avg_line_len: float
    is_empty: bool
    is_toc_like: bool
    is_index_like: bool
    is_bibliography_like: bool
    estimated_tokens: int
    image_count: int
    rotation: int
    text_fingerprint: str
    repeated_header: str
    repeated_footer: str
    duplicate_of_page: int | None
    preview: str


def sha256_file(path: Path, progress_callback=None) -> str:
    h = hashlib.sha256()
    total_bytes = max(0, int(Path(path).stat().st_size))
    completed_bytes = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
            completed_bytes += len(block)
            if callable(progress_callback):
                progress_callback(completed_bytes, total_bytes)
    return h.hexdigest()


def pdf_date_to_epoch_ms(value):
    if not value:
        return None
    match = re.match(r"^D:(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?", str(value))
    if not match:
        return None
    parts = [int(part) if part else default for part, default in zip(match.groups(), [1970, 1, 1, 0, 0, 0])]
    try:
        parsed = datetime(*parts, tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(parsed.timestamp() * 1000)


def write_json(path: Path, data):
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False))


def atomic_write_text(path: Path, content: str, *, encoding="utf-8", retries=3):
    """Durably replace one local artifact without exposing a partial file.

    Output artefacts are evidence and recovery inputs.  A temporary file in
    the destination directory keeps ``os.replace`` on the same volume; a
    bounded retry handles a short-lived Windows sharing violation without
    turning a permissions or space failure into an unbounded wait.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding=encoding, delete=False, dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp",
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(max(1, int(retries or 1))):
            try:
                os.replace(temporary, path)
                temporary = None
                return
            except PermissionError:
                if attempt + 1 >= max(1, int(retries or 1)):
                    raise
                time.sleep(0.08 * (attempt + 1))
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def append_jsonl(path: Path, rows):
    atomic_write_text(
        path,
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
    )


def append_jsonl_receipt(path: Path, row):
    """Append one small, fsync'd recovery receipt; never retain source text."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def write_csv(path: Path, rows, fieldnames=None):
    rows = list(rows)
    if fieldnames is None:
        keys = []
        for row in rows:
            for key in row.keys():
                if key not in keys:
                    keys.append(key)
        fieldnames = keys
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    atomic_write_text(path, buffer.getvalue())


def pdf_metadata(path: Path, include_page_geometry=False, include_author_samples=False, progress_callback=None):
    """Read PDF metadata and, optionally, author-inference samples in one open.

    Automatic preparation already opens every source to read its metadata and
    page geometry.  Reopening the same PDF immediately afterwards just to
    inspect the six author-inference pages adds avoidable per-document I/O.
    The optional samples are deliberately private transient data: callers
    remove them before writing the public source profile, so source text is
    not copied into ordinary metadata artifacts.
    """
    with fitz.open(path) as doc:
        metadata = dict(doc.metadata or {})
        outline = [
            {"level": level, "title": normalize_text(title), "pdf_page": page}
            for level, title, page in doc.get_toc(simple=True)
        ]
        page_geometry = []
        if include_page_geometry:
            # Indexed access is the explicit PyMuPDF contract. Its stub does
            # not declare Document as Iterable, even though runtime supports
            # it, so avoid weakening type checks for a convenience iterator.
            for page_index in range(len(doc)):
                page_number = page_index + 1
                page = doc[page_index]
                page_geometry.append(
                    {
                        "pdf_page": page_number,
                        "width": round(float(page.rect.width), 2),
                        "height": round(float(page.rect.height), 2),
                        "rotation": int(page.rotation or 0),
                        "image_count": len(page.get_images(full=True)),
                    }
                )
                if callable(progress_callback):
                    progress_callback("page_geometry", page_number, len(doc))
        author_text_samples = []
        author_sample_error = ""
        if include_author_samples:
            page_numbers = []
            for number in [1, 2, 3, 4, max(1, len(doc) - 1), len(doc)]:
                if 1 <= number <= len(doc) and number not in page_numbers:
                    page_numbers.append(number)
            try:
                for page_number in page_numbers:
                    text = doc.load_page(page_number - 1).get_text("text")
                    if text:
                        author_text_samples.append({"page": page_number, "text": text})
                    if callable(progress_callback):
                        progress_callback("author_sample", page_numbers.index(page_number) + 1, len(page_numbers))
            except Exception as exc:
                # Preserve the legacy failure meaning: a broken sampled-page
                # read means author inference abstains instead of using a
                # partial sample that could change metadata provenance.
                author_text_samples = []
                author_sample_error = str(exc)
        result = {
            "pdf_page_count": len(doc),
            "title": normalize_text(metadata.get("title") or ""),
            "author": normalize_text(metadata.get("author") or ""),
            "subject": normalize_text(metadata.get("subject") or ""),
            "keywords": normalize_text(metadata.get("keywords") or ""),
            "creator": normalize_text(metadata.get("creator") or ""),
            "producer": normalize_text(metadata.get("producer") or ""),
            "creationDate": metadata.get("creationDate") or "",
            "modDate": metadata.get("modDate") or "",
            "format": metadata.get("format") or "",
            "is_encrypted": bool(doc.is_encrypted),
            "needs_password": bool(doc.needs_pass),
            "outline": outline,
            "page_geometry": page_geometry,
        }
        if include_author_samples:
            result["_author_text_samples"] = author_text_samples
            result["_author_sample_error"] = author_sample_error
        return result


def normalize_author_candidate(value):
    candidate = normalize_text(value or "")
    candidate = re.sub(r"[*†‡§¶∗]+", " ", candidate)
    candidate = re.sub(r"^(?:by|written by|author(?:\(s\))?|authors?|edited by|instructor|lecturer|professor)\s*[:\-]?\s*", "", candidate, flags=re.I)
    candidate = re.sub(r"\b(?:with a foreword by|foreword by)\b.*$", "", candidate, flags=re.I)
    candidate = re.sub(r"\([^)]*(?:@|www\.|http|doi:)[^)]*\)", "", candidate, flags=re.I)
    candidate = re.sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "", candidate, flags=re.I)
    candidate = candidate.strip(" ,;:-")
    if "," in candidate and not re.search(r"\b(?:Jr|Sr|III|IV|V)\b", candidate):
        parts = [part.strip() for part in candidate.split(",") if part.strip()]
        if len(parts) == 2:
            candidate = f"{parts[1]} {parts[0]}".strip()
    candidate = re.sub(r"\s+", " ", candidate).strip()
    return candidate


def looks_like_person_name(value, title_hint=""):
    candidate = normalize_author_candidate(value)
    if not candidate:
        return False
    if len(candidate) < 5 or len(candidate) > 80:
        return False
    lowered = candidate.casefold()
    if any(term in lowered for term in AUTHOR_STOP_TERMS):
        return False
    normalized_title = normalize_text(title_hint).casefold() if title_hint else ""
    if normalized_title:
        if lowered == normalized_title or lowered in normalized_title:
            return False
    if ":" in candidate:
        return False
    if any(token in lowered for token in {"http", "www.", "@", "doi", "issn", "url"}):
        return False
    if re.search(r"\d", candidate):
        return False
    if candidate.count(" ") > 5:
        return False
    words = re.findall(r"[A-Za-z][A-Za-z'.-]*", candidate)
    if len(words) < 2 or len(words) > 5:
        return False
    if any(word.casefold().rstrip(".") in AUTHOR_ORGANIZATION_TERMS for word in words):
        return False
    if any(word.casefold() in HEADING_STOPWORDS for word in words):
        return False
    capitalized = sum(
        1
        for word in words
        if word[0].isupper() or word.casefold() in AUTHOR_NAME_PARTICLES
    )
    if capitalized < max(2, len(words) - 1):
        return False
    return True


def split_author_line_candidates(line, title_hint=""):
    raw = re.sub(r"[*†‡§¶∗0-9]+", " ", line or "")
    raw = re.sub(r"\s+", " ", raw).strip(" ,;:-")
    if not raw or "@" in raw:
        return []
    if any(term in raw.casefold() for term in AUTHOR_AFFILIATION_HINTS):
        return []
    pieces = [
        piece.strip(" ,;:-")
        for piece in re.split(
            r"\s*(?:,|;|·|•|&|\band\b)\s*(?!\b(?:Jr|Sr|III|IV|V)\b)",
            raw,
            flags=re.I,
        )
        if piece.strip(" ,;:-")
    ]
    if len(pieces) < 2:
        return []
    candidates = []
    for piece in pieces:
        if looks_like_person_name(piece, title_hint=title_hint):
            candidates.append(normalize_author_candidate(piece))
    return candidates if len(candidates) >= 2 else []


def extract_adjacent_person_names(line, title_hint=""):
    digit_markers = re.findall(r"(?:[*†‡§¶∗]?\s*\d+)", line or "")
    if len(digit_markers) >= 2:
        split_parts = [
            normalize_author_candidate(part)
            for part in re.split(r"\s*(?:[*†‡§¶∗]?\s*\d+)\s*", line or "")
            if normalize_author_candidate(part)
        ]
        split_candidates = [
            part for part in split_parts
            if looks_like_person_name(part, title_hint=title_hint)
        ]
        if len(split_candidates) >= 2:
            return split_candidates

    raw = re.sub(r"[*†‡§¶∗0-9]+", " ", line or "")
    raw = re.sub(r"\s+", " ", raw).strip(" ,;:-")
    if not raw or "@" in raw:
        return []
    if any(term in raw.casefold() for term in AUTHOR_AFFILIATION_HINTS):
        return []
    matches = re.findall(r"\b(?:[A-Z][A-Za-z'.-]*\s+){1,3}[A-Z][A-Za-z'.-]*\b", raw)
    candidates = []
    for match in matches:
        candidate = normalize_author_candidate(match)
        if looks_like_person_name(candidate, title_hint=title_hint) and candidate not in candidates:
            candidates.append(candidate)
    return candidates if len(candidates) >= 2 else []


def infer_author_from_text_samples(samples, title_hint=""):
    patterns = [
        (r"(?:^|\n)\s*by\s+([A-Z][A-Za-z.,'\- ]{3,70})", "text_byline"),
        (r"(?:^|\n)\s*written by\s+([A-Z][A-Za-z.,'\- ]{3,70})", "text_written_by"),
        (r"(?:^|\n)\s*edited by\s+([A-Z][A-Za-z.,'\- ]{3,70})", "text_edited_by"),
        (r"(?:^|\n)\s*author(?:\(s\))?\s*[:\-]\s*([A-Z][A-Za-z.,'\- &]{3,90})", "text_author_label"),
        (r"(?:^|\n)\s*authors?(?:\(s\))?\s*[:\-]\s*([A-Z][A-Za-z.,'\- &]{3,90})", "text_author_label"),
        (r"(?:^|\n)\s*instructor\s*[:\-]\s*([^\n]{3,90})", "text_instructor_label"),
    ]
    for sample in samples:
        raw_text = str(sample.get("text") or "").replace("\r\n", "\n").replace("\r", "\n")
        text = normalize_text(raw_text)
        page = int(sample.get("page") or 0)
        if not text:
            continue
        lines = [normalize_text(line) for line in raw_text.splitlines() if normalize_text(line)]
        # Journal PDFs often carry stale workstation-owner metadata. Prefer a
        # visible byline only when its syntax is unusually strong: a personal
        # name followed by an affiliation, or a bare name immediately echoed
        # by a bibliographic ``From <name>, ...`` line. This avoids promoting
        # ordinary title-case headings to authors.
        affiliated_names: list[str] = []
        for line in lines[:24]:
            candidate = ""
            comma_match = re.match(r"^(.{3,80}?),\s*(.+)$", line)
            if comma_match and any(
                hint in comma_match.group(2).casefold() for hint in AUTHOR_AFFILIATION_HINTS
            ):
                candidate = comma_match.group(1)
            parenthetical_match = re.match(r"^(.{3,80}?)\s*\(([^)]{3,100})\)\s*$", line)
            if parenthetical_match and any(
                hint in parenthetical_match.group(2).casefold() for hint in AUTHOR_AFFILIATION_HINTS
            ):
                candidate = parenthetical_match.group(1)
            candidate = normalize_author_candidate(candidate)
            if candidate and looks_like_person_name(candidate, title_hint=title_hint):
                if candidate not in affiliated_names:
                    affiliated_names.append(candidate)
        if affiliated_names:
            return {
                "author": ", ".join(affiliated_names[:12]),
                "source": "text_affiliated_byline",
                "page": page,
                "evidence": " / ".join(affiliated_names[:4]),
            }
        for index, line in enumerate(lines[:12]):
            candidate = normalize_author_candidate(line)
            if not looks_like_person_name(candidate, title_hint=title_hint):
                continue
            following = lines[index + 1] if index + 1 < len(lines) else ""
            if re.match(rf"^From\s+{re.escape(candidate)}(?:\s|,)", following, flags=re.I):
                return {
                    "author": candidate,
                    "source": "text_bibliographic_byline",
                    "page": page,
                    "evidence": f"{line} / {following}",
                }
        for pattern, source in patterns:
            match = re.search(pattern, raw_text, flags=re.I)
            if not match:
                continue
            candidate = normalize_author_candidate(match.group(1))
            if looks_like_person_name(candidate, title_hint=title_hint):
                return {
                    "author": candidate,
                    "source": source,
                    "page": page,
                    "evidence": match.group(0).strip(),
                }
            split_candidates = split_author_line_candidates(candidate, title_hint=title_hint)
            if split_candidates:
                return {
                    "author": ", ".join(split_candidates[:12]),
                    "source": source,
                    "page": page,
                    "evidence": match.group(0).strip(),
                }

        top_lines = lines[:18]
        title_start_index = 0
        title_matched = False
        normalized_title = normalize_text(title_hint).casefold() if title_hint else ""
        if normalized_title:
            for index, line in enumerate(top_lines):
                lowered = line.casefold().strip()
                if lowered and (lowered in normalized_title or normalized_title in lowered):
                    title_start_index = index
                    title_matched = True
                    break
        generic_fallback_last_index = min(len(top_lines) - 1, title_start_index + 8)
        for index, line in enumerate(top_lines):
            lowered = line.casefold().strip()
            if lowered not in AUTHOR_ROLE_HINTS:
                continue
            if index + 1 >= len(top_lines):
                continue
            candidate = normalize_author_candidate(top_lines[index + 1])
            if looks_like_person_name(candidate, title_hint=title_hint):
                return {
                    "author": candidate,
                    "source": "text_role_followup",
                    "page": page,
                    "evidence": f"{line} / {top_lines[index + 1]}",
                }
        detected_names = []
        allow_generic_top_block = page <= 2 or (page <= 4 and title_matched)
        for index, line in enumerate(top_lines):
            if index < title_start_index:
                continue
            lowered = line.casefold().strip()
            if not lowered:
                continue
            if lowered in AUTHOR_BLOCK_STOP_HINTS or re.match(r"^\d+\s+introduction\b", lowered):
                break
            if index > generic_fallback_last_index:
                continue
            if "@" in line or any(term in lowered for term in AUTHOR_AFFILIATION_HINTS):
                continue
            split_candidates = split_author_line_candidates(line, title_hint=title_hint)
            if split_candidates:
                for candidate in split_candidates:
                    if candidate not in detected_names:
                        detected_names.append(candidate)
                continue
            adjacent_candidates = extract_adjacent_person_names(line, title_hint=title_hint)
            if adjacent_candidates:
                for candidate in adjacent_candidates:
                    if candidate not in detected_names:
                        detected_names.append(candidate)
                continue
            if not allow_generic_top_block:
                continue
            candidate = normalize_author_candidate(line)
            if looks_like_person_name(candidate, title_hint=title_hint) and candidate not in detected_names:
                detected_names.append(candidate)
        if detected_names:
            return {
                "author": ", ".join(detected_names[:12]),
                "source": "text_top_block_names",
                "page": page,
                "evidence": " / ".join(detected_names[:4]),
            }
    return {"author": "", "source": "not_found", "page": 0, "evidence": ""}


def infer_author_from_filename(path: Path, title_hint=""):
    stem = normalize_text(path.stem)
    stem = re.sub(r"\[[^\]]+\]$", "", stem).strip()
    parts = [part.strip(" -_,") for part in re.split(r"\s+[-–—]{1,2}\s+|\s{2,}", stem) if part.strip(" -_,")]
    tail_candidates = list(reversed(parts[1:])) if len(parts) >= 2 else []
    for candidate in tail_candidates:
        cleaned = re.sub(r"\b(?:paperback|hardcover|preview|ocr|edition|\d{4})\b", " ", candidate, flags=re.I)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,;:-")
        if looks_like_person_name(cleaned, title_hint=title_hint):
            return {"author": normalize_author_candidate(cleaned), "source": "filename_author_fallback", "page": 0, "evidence": path.name}
    return {"author": "", "source": "not_found", "page": 0, "evidence": ""}


def infer_author_from_samples_or_filename(samples, path: Path, title_hint=""):
    """Apply the established sample rules, then the existing filename fallback."""
    report = infer_author_from_text_samples(samples, title_hint=title_hint)
    return report if report.get("author") else infer_author_from_filename(path, title_hint=title_hint)


def infer_author_from_pdf_text(path: Path, title_hint=""):
    try:
        with fitz.open(path) as doc:
            page_count = len(doc)
            page_numbers = []
            for number in [1, 2, 3, 4, max(1, page_count - 1), page_count]:
                if 1 <= number <= page_count and number not in page_numbers:
                    page_numbers.append(number)
            samples = []
            for page_number in page_numbers:
                text = doc.load_page(page_number - 1).get_text("text")
                if text:
                    samples.append({"page": page_number, "text": text})
            return infer_author_from_samples_or_filename(samples, path, title_hint=title_hint)
    except Exception as exc:
        return {"author": "", "source": "error", "page": 0, "evidence": str(exc)}


def ensure_downloaded_pdf(path: Path, url: str):
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(url, path)
    return path


def evaluate_author_inference_samples(output_dir: Path, sample_pdf_dir: Path | None = None):
    output_dir.mkdir(parents=True, exist_ok=True)
    pdf_root = sample_pdf_dir or output_dir
    pdf_root.mkdir(parents=True, exist_ok=True)
    rows = []
    for sample in AUTHOR_INFERENCE_SAMPLE_PAPERS:
        pdf_path = ensure_downloaded_pdf(pdf_root / sample["file"], sample["url"])
        report = infer_author_from_pdf_text(pdf_path, title_hint=sample["title_hint"])
        inferred_author = report.get("author") or ""
        rows.append(
            {
                "file": sample["file"],
                "url": sample["url"],
                "title_hint": sample["title_hint"],
                "expected_contains": " | ".join(sample["expected_contains"]),
                "inferred_author": inferred_author,
                "source": report.get("source", ""),
                "page": report.get("page", 0),
                "evidence": report.get("evidence", ""),
                "all_expected_found": all(
                    expected in inferred_author for expected in sample["expected_contains"]
                ),
            }
        )
    json_path = output_dir / "author-inference-evaluation.json"
    csv_path = output_dir / "author-inference-evaluation.csv"
    write_json(json_path, rows)
    write_csv(csv_path, rows)
    return {
        "json": str(json_path),
        "csv": str(csv_path),
        "rows": rows,
        "passed": sum(1 for row in rows if row["all_expected_found"]),
        "failed": sum(1 for row in rows if not row["all_expected_found"]),
    }


def page_stats_for(page_info, page_geometry=None):
    raw = page_info.get("text", "")
    clean = normalize_text(raw)
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    words = re.findall(r"\b[\w\u2019'-]+\b", clean, flags=re.UNICODE)
    short_lines = sum(1 for line in lines if len(line) <= 45)
    # A literal ellipsis anywhere in extracted/OCR text is not a contents
    # leader. Require the conventional ``heading .... 12`` shape instead.
    # This prevents OCR noise and ordinary prose punctuation from masquerading
    # as a table of contents.
    dot_leaders = sum(1 for line in lines if re.search(r"\.{3,}\s*\d{1,4}\s*$", line))
    years = len(re.findall(r"\b(?:18|19|20)\d{2}\b", clean))
    sentence_marks = len(re.findall(r"[.!?]", clean))
    alpha_entries = len(re.findall(r"\b[A-Z][a-z]+(?:,\s+[A-Z][a-z]+)?\b", clean))
    avg_line_len = (sum(len(line) for line in lines) / len(lines)) if lines else 0.0
    top_lines = lines[:6]
    contents_heading = any(
        re.match(r"^(?:table of )?contents\b", line, re.I) for line in top_lines
    )
    numbered_entries = sum(
        1
        for line in lines
        if 4 <= len(line) <= 140
        and not re.search(r"[.!?]\s*$", line)
        and re.search(r"\s+\d{1,4}\s*$", line)
    )
    short_line_ratio = short_lines / max(len(lines), 1)
    # A TOC needs independent structural evidence, not just a topical word.
    # The safe bias is to retain a real TOC when uncertain rather than discard
    # a body page that happens to mention "conclusion" or "index".
    toc_signals = bool(
        (contents_heading and (dot_leaders >= 1 or numbered_entries >= 3))
        or (
            dot_leaders >= 3
            and numbered_entries >= 4
            and short_line_ratio >= 0.5
            and re.search(r"\b(chapter|introduction|preface|conclusion|index)\b", clean, re.I)
        )
    )
    # A page can contain list-like short lines and heading words while still
    # being ordinary dense prose.  Treating such a page as a table of contents
    # makes the body-start fallback discard real text (the Weber excerpt did
    # exactly this on page 10). A genuine contents page is normally sparse in
    # sentence punctuation; preserve the signal only when it is not prose.
    dense_prose = len(words) >= 180 and sentence_marks >= 5
    is_toc_like = toc_signals and not dense_prose
    is_index_like = bool(
        re.search(r"^\s*(?:#{1,6}\s*)?(?:[*_`~\s])*(index)\b", clean, re.I)
        or (alpha_entries >= 35 and sentence_marks <= max(3, len(words) // 80))
    )
    is_bibliography_like = bool(
        re.search(r"^\s*(?:#{1,6}\s*)?(?:[*_`~\s])*(bibliography|references|works cited)\b", clean, re.I)
        or (years >= 8 and sentence_marks >= 8 and re.search(r"\b(Press|University|Journal|Review|Cambridge|Oxford|Yale|New York)\b", clean))
    )
    geometry = page_geometry or {}
    fingerprint_source = re.sub(r"\W+", "", clean.casefold())
    fingerprint = hashlib.sha1(fingerprint_source.encode("utf-8")).hexdigest() if fingerprint_source else ""
    return PageStat(
        pdf_page=int(page_info.get("page") or 0),
        chars=len(clean),
        words=len(words),
        replacement_chars=clean.count("\ufffd"),
        sentence_marks=sentence_marks,
        line_count=len(lines),
        avg_line_len=round(avg_line_len, 2),
        is_empty=len(clean) < 40,
        is_toc_like=is_toc_like,
        is_index_like=is_index_like,
        is_bibliography_like=is_bibliography_like,
        estimated_tokens=max(0, math.ceil(len(clean) / 4)),
        image_count=int(geometry.get("image_count") or 0),
        rotation=int(geometry.get("rotation") or 0),
        text_fingerprint=fingerprint,
        repeated_header="",
        repeated_footer="",
        duplicate_of_page=None,
        preview=clean[:260],
    )


def enrich_page_stats(pages, stats):
    """Add cross-page signals that cannot be determined from one page alone."""
    header_counts = Counter()
    footer_counts = Counter()
    page_lines = {}
    for page_info in pages:
        page_num = int(page_info.get("page") or 0)
        lines = [normalize_text(line) for line in page_info.get("text", "").splitlines() if normalize_text(line)]
        page_lines[page_num] = lines
        if lines and 2 <= len(lines[0]) <= 120:
            header_counts[lines[0].casefold()] += 1
        if lines and 2 <= len(lines[-1]) <= 120 and not re.fullmatch(r"\d{1,4}|[ivxlcdm]+", lines[-1], re.I):
            footer_counts[lines[-1].casefold()] += 1

    repeat_threshold = max(3, math.ceil(len(stats) * 0.08))
    seen_fingerprints = {}
    for stat in stats:
        lines = page_lines.get(stat.pdf_page, [])
        if lines and header_counts[lines[0].casefold()] >= repeat_threshold:
            stat.repeated_header = lines[0]
        if lines and footer_counts[lines[-1].casefold()] >= repeat_threshold:
            stat.repeated_footer = lines[-1]
        if stat.text_fingerprint and stat.chars >= 80:
            if stat.text_fingerprint in seen_fingerprints:
                stat.duplicate_of_page = seen_fingerprints[stat.text_fingerprint]
            else:
                seen_fingerprints[stat.text_fingerprint] = stat.pdf_page
    return stats


def remove_verified_photographed_ocr_running_headers(pages):
    """Remove only document-local, repeated running heads from photo OCR.

    Positioned native extraction has geometry for header detection.  A
    photographed Tesseract crop does not, so use a narrower substitute: the
    *first* OCR line must be a short all-caps label and recur verbatim on at
    least two distinct photographed pages.  This leaves a one-off chapter or
    article title untouched while removing book/article running heads such as
    ``CULTURE AS HISTORY``.  The uncropped OCR remains in ``raw_text`` for
    inspection.
    """
    candidates = {}
    for page_info in pages:
        page_number = int(page_info.get("page") or 0)
        for region_index, region in enumerate(page_info.get("reading_regions") or []):
            if not str(region.get("ocr_method") or "").startswith("tesseract_photographed_"):
                continue
            lines = [line.strip() for line in str(region.get("text") or "").splitlines() if normalize_text(line)]
            if not lines:
                continue
            first = normalize_text(lines[0])
            letters = re.sub(r"[^A-Za-z]", "", first)
            if not (5 <= len(first) <= 90 and len(letters) >= 5 and first == first.upper() and not re.search(r"[.!?]$", first)):
                continue
            candidates.setdefault(first.casefold(), {"text": first, "locations": []})["locations"].append(
                (page_number, region_index)
            )

    verified = {
        key: entry for key, entry in candidates.items()
        if len({page_number for page_number, _ in entry["locations"]}) >= 2
    }
    removed = []
    transformed = []
    for page_info in pages:
        copied = dict(page_info)
        regions = []
        for region in page_info.get("reading_regions") or []:
            copied_region = dict(region)
            lines = str(copied_region.get("text") or "").splitlines()
            first_index = next((index for index, line in enumerate(lines) if normalize_text(line)), None)
            first = normalize_text(lines[first_index]) if first_index is not None else ""
            entry = verified.get(first.casefold())
            if entry and str(copied_region.get("ocr_method") or "").startswith("tesseract_photographed_"):
                copied_region["raw_text"] = copied_region.get("text", "")
                copied_region["text"] = "\n".join(line for index, line in enumerate(lines) if index != first_index).strip()
                copied_region["removed_marginalia"] = [{
                    "text": entry["text"], "reason": "verified_repeated_photographed_ocr_running_header"
                }]
                removed.append({"pdf_page": int(page_info.get("page") or 0), "text": entry["text"]})
            regions.append(copied_region)
        if regions:
            copied["raw_text"] = page_info.get("text", "")
            copied["reading_regions"] = regions
            copied["text"] = "\n\n".join(str(region.get("text") or "") for region in regions).strip()
        transformed.append(copied)
    return transformed, {
        "status": "applied" if removed else "no_verified_photographed_ocr_running_headers",
        "method": "repeated_all_caps_first_line_photographed_ocr_v1",
        "verified_headers": [entry["text"] for entry in verified.values()],
        "removed": removed,
    }


def title_key(title):
    return normalize_text(title).casefold()


def is_front_matter_title(title):
    key = title_key(title)
    return any(token in key for token in FRONT_MATTER_TITLES)


def is_main_body_title(title):
    key = title_key(title)
    return any(key.startswith(token) or token in key for token in MAIN_BODY_TITLES)


def outline_token_set(title):
    key = re.sub(r"[_*`~#]+", " ", normalize_text(title).casefold())
    key = re.sub(r"\b(?:chapter|part)\s+(?:\d+|[ivxlcdm]+|one|two|three|four|five|six|seven|eight|nine|ten)\b", " ", key)
    tokens = re.findall(r"[a-z][a-z0-9]{2,}", key)
    return {token for token in tokens if token not in HEADING_STOPWORDS and len(token) >= 4}


def outline_sample_roles(outline):
    if not outline:
        return {}
    roles = {}
    valid = [i for i, row in enumerate(outline) if int(row.get("pdf_page") or 0) > 0]
    meaningful = [
        i
        for i in valid
        if not any(token in title_key(outline[i].get("title", "")) for token in OUTLINE_IGNORE_VALIDATION_TITLES)
    ]
    main = [i for i in meaningful if is_main_body_title(outline[i].get("title", ""))]
    for label, indexes in (("bookmark", valid), ("meaningful", meaningful), ("main_text", main)):
        if not indexes:
            continue
        picks = [
            ("first", indexes[0]),
            ("middle", indexes[len(indexes) // 2]),
            ("last", indexes[-1]),
        ]
        for role, index in picks:
            roles.setdefault(index, []).append(f"{label}_{role}")
    return roles


def validate_outline_against_text(outline, pages, page_count):
    if not outline:
        return {"reliability": "missing", "pass_rate": 0.0, "rows": []}

    page_text = {int(page.get("page") or 0): normalize_text(page.get("text", "")).casefold() for page in pages}
    page_raw = {int(page.get("page") or 0): page.get("text", "") for page in pages}
    sample_roles = outline_sample_roles(outline)
    rows = []

    for index, row in enumerate(outline):
        title = row.get("title", "")
        page = int(row.get("pdf_page") or 0)
        tokens = outline_token_set(title)
        status = "skipped"
        matched_page = ""
        token_hits = 0
        nearby_hits = {}
        ignored_title = any(token in title_key(title) for token in OUTLINE_IGNORE_VALIDATION_TITLES)

        if ignored_title:
            status = "skipped_front_matter_title"
        elif page <= 0 or page > page_count:
            status = "page_out_of_range"
        elif len(tokens) < 1:
            status = "skipped_generic_title"
        else:
            for candidate_page in range(max(1, page - 1), min(page_count, page + 1) + 1):
                raw = page_raw.get(candidate_page, "")
                text = page_text.get(candidate_page, "")
                headings = extract_page_headings(raw, max_lines=24)
                heading_hits = 0
                for heading in headings:
                    heading_text = heading.casefold()
                    heading_hits = max(heading_hits, sum(1 for token in tokens if token in heading_text))
                if heading_hits:
                    hits = heading_hits + len(tokens)
                else:
                    toc_echo = re.search(r"\bcontents\b", text[:400], re.I) and not title_key(title).startswith("contents")
                    hits = 0 if toc_echo else sum(1 for token in tokens if token in text)
                nearby_hits[candidate_page] = hits
            best_page, token_hits = max(nearby_hits.items(), key=lambda item: item[1])
            needed = max(1, math.ceil(len(tokens) * 0.5))
            heading_needed = needed + len(tokens)
            if nearby_hits.get(page, 0) >= needed:
                if nearby_hits.get(page, 0) >= heading_needed or title_key(title).startswith(("contents", "index", "notes", "bibliography")):
                    status = "pass"
                    matched_page = page
                    token_hits = nearby_hits[page]
                else:
                    status = "mismatch"
            elif token_hits >= heading_needed:
                status = "nearby_page_warning"
                matched_page = best_page
            else:
                status = "mismatch"

        rows.append(
            {
                "outline_index": index,
                "sample_roles": ";".join(sample_roles.get(index, [])),
                "level": row.get("level", ""),
                "title": title,
                "pdf_page": page,
                "status": status,
                "matched_page": matched_page,
                "token_hits": token_hits,
                "token_count": len(tokens),
                "tokens_checked": " ".join(sorted(tokens)),
            }
        )

    sampled = [
        r
        for r in rows
        if r["sample_roles"] and r["status"] not in {"skipped", "skipped_generic_title", "skipped_front_matter_title"}
    ]
    usable = [r for r in sampled if r["status"] in {"pass", "nearby_page_warning"}]
    pass_rate = len(usable) / max(len(sampled), 1)
    if not sampled:
        reliability = "untested"
    elif pass_rate >= 0.85:
        reliability = "trusted"
    elif pass_rate >= 0.5:
        reliability = "partially_trusted"
    else:
        reliability = "untrusted"
    return {"reliability": reliability, "pass_rate": round(pass_rate, 3), "rows": rows}


def usable_outline_from_validation(outline, validation):
    reliability = validation.get("reliability")
    if reliability in {"missing", "untrusted"}:
        return []
    if reliability != "partially_trusted":
        return outline
    mismatched_titles = {
        row["title"]
        for row in validation.get("rows", [])
        if row.get("sample_roles") and row.get("status") in {"mismatch", "page_out_of_range"}
    }
    return [row for row in outline if row.get("title") not in mismatched_titles]


def parsed_pdf_text_filename(pdf_path: Path) -> str:
    """Return the portable, user-facing name for a prepared PDF transcript.

    The internal candidate artifact remains deliberately generic because it is
    used by several compatibility checks. The selected/downloadable copy is
    named after its source instead, so it is useful outside AnythingLLM too.
    ``safe_stem`` removes Windows-invalid punctuation and the suffix ensures
    even a reserved bare stem such as ``CON`` is safe as a filename.
    """
    stem = safe_stem(Path(pdf_path).stem)[:140].rstrip("-._ ") or "parsed-document"
    return f"{stem}-pdf-parsed.txt"


LEAN_SUCCESS_ARTIFACT_DIRECTORIES = (
    "candidates",
    "inspection",
    "metadata-api",
    "native-metadata-compatibility-probe",
    "native-metadata-test-kit",
    "retrieval-eval",
)

LEAN_SUCCESS_ARTIFACT_FILES = (
    "diagnostics.csv",
    "diagnostics.html",
    "diagnostics.json",
    "edge-case-report.html",
    "edge-case-results.csv",
    "edge-case-summary.json",
    "output-capacity-preflight.json",
    "pdf-input-preflight.json",
    "source-profile.json",
)

LEAN_SUCCESS_NONRETAINED_SUMMARY_PATH_FIELDS = (
    "inline_metadata_fallback",
    "manifest",
    "page_transition_manifest",
    "page_parent_manifest",
    "child_parent_map",
    "layout_region_review",
    "retrieval_lane_review",
    "supplementary_lane_candidates",
    "provenance_review_manifest",
    "representation_comparison",
    "harmonization_report",
    "representation_recommendation",
    "report",
    "variant_summary",
    "metadata_payloads",
    "page_parent_metadata_payloads",
    "page_parent_upload_plan",
    "metadata_layer_visibility",
    "column_explanations",
    "author_inference_evaluation_csv",
    "author_inference_evaluation_json",
    "api_embedding_batch_ledger",
)


def materialize_retained_segments(prepared_text_path: Path, segments_dir: Path, segments):
    """Write the selected, local chunks as plain text files for a ready run.

    These are the exact chunks selected by this pipeline, named by PDF page and
    their within-page order.  They are intentionally not described as
    AnythingLLM's final chunks: AnythingLLM may normalize or rechunk a stored
    document during its own processing step.
    """
    prepared_text_path = Path(prepared_text_path)
    segments_dir = Path(segments_dir)
    if segments_dir.exists():
        shutil.rmtree(segments_dir)
    segments_dir.mkdir(parents=True, exist_ok=True)
    base_name = safe_stem(prepared_text_path.stem.removesuffix("-pdf-parsed")) or "document"
    page_counts = Counter()
    retained = []
    for segment in segments or ():
        if not isinstance(segment, dict):
            continue
        try:
            pdf_page = max(1, int(segment.get("pdf_page") or 0))
        except (TypeError, ValueError):
            pdf_page = 1
        page_counts[pdf_page] += 1
        within_page = page_counts[pdf_page]
        filename = f"{base_name}-p{pdf_page:03d}-s{within_page:02d}.txt"
        target = segments_dir / filename
        target.write_text(str(segment.get("text") or ""), encoding="utf-8")
        retained.append(target)
    return retained


def retain_successful_run_leanly(out_root: Path, summary, profile, prepared_text_path: Path, *, segments=()):
    """Replace a successful run's forensic tree with usable text + compact facts.

    Full candidates, metadata payload files, storage snapshots, and repeated
    CSV/HTML/JSON reports are valuable only while a run needs review. A ready
    run keeps root-level parsed text, the selected local segment text files,
    and one self-contained summary/recovery record. The caller still receives
    the rich in-memory summary for the live UI.
    """
    if str(summary.get("readiness_status") or "") != "ready":
        return {"applied": False, "reason": "run_needs_review"}
    upload_status = str(summary.get("api_upload_status") or "").casefold()
    post_status = str(summary.get("post_upload_verification_status") or "").casefold()
    runtime_status = str(summary.get("anythingllm_runtime_validation_status") or "").casefold()
    local_only_complete = (
        upload_status == "skipped_prepare_only"
        and post_status == "not_checked_no_upload"
        and runtime_status == "not_checked_no_upload"
    )
    fully_verified_upload = (
        upload_status in {"complete", "complete_with_key_cleanup_warning"}
        and post_status == "pass"
        and runtime_status == "pass"
    )
    if not (local_only_complete or fully_verified_upload):
        # Do not shrink evidence merely because extraction itself was ready.
        # An accepted-but-ambiguous upload, incomplete vector coverage, or
        # retrieval timeout is precisely when the receipts and inspection
        # artifacts are needed for verification-only recovery.
        return {
            "applied": False,
            "reason": "upload_or_verification_needs_review",
            "api_upload_status": upload_status,
            "post_upload_verification_status": post_status,
            "runtime_validation_status": runtime_status,
        }
    out_root = Path(out_root)
    prepared_text_path = Path(prepared_text_path)
    if not prepared_text_path.is_file():
        return {"applied": False, "reason": "prepared_text_missing"}

    deleted = []
    selected_dir = prepared_text_path.parent
    retained_text_path = out_root / prepared_text_path.name
    if prepared_text_path != retained_text_path:
        if retained_text_path.exists():
            raise FileExistsError(
                f"Refusing to overwrite existing retained text: {retained_text_path}"
            )
        shutil.move(str(prepared_text_path), str(retained_text_path))
    retained_segments = materialize_retained_segments(
        retained_text_path,
        out_root / "segments",
        segments,
    )
    # The live Gradio result is built from this same mutable summary.  Rewrite
    # its paths before returning so downloads and output links never point to
    # the selected/ files we are about to remove.
    summary["upload_file"] = str(retained_text_path)
    for key in LEAN_SUCCESS_NONRETAINED_SUMMARY_PATH_FIELDS:
        summary[key] = ""
    summary["variant_outputs"] = {}
    if selected_dir.exists() and selected_dir != out_root:
        for child in selected_dir.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
            deleted.append(str(child.relative_to(out_root)))
        selected_dir.rmdir()
        deleted.append(str(selected_dir.relative_to(out_root)))
    for name in LEAN_SUCCESS_ARTIFACT_DIRECTORIES:
        candidate = out_root / name
        if candidate.exists():
            shutil.rmtree(candidate, ignore_errors=True)
            deleted.append(name)
    for name in LEAN_SUCCESS_ARTIFACT_FILES:
        candidate = out_root / name
        if candidate.exists():
            candidate.unlink(missing_ok=True)
            deleted.append(name)

    compact = {
        "schema_version": 1,
        "retention_policy": "lean_success_v1",
        "output_root": str(out_root),
        "outcome": {
            "readiness_status": summary.get("readiness_status"),
            "selected_backend": summary.get("selected_backend"),
            "total_pipeline_seconds": summary.get("total_pipeline_seconds"),
            "api_upload_status": summary.get("api_upload_status"),
            "post_upload_verification_status": summary.get("post_upload_verification_status"),
            "anythingllm_runtime_validation_status": summary.get("anythingllm_runtime_validation_status"),
        },
        "verification_receipt": {
            # Keep the evidence needed to audit a green run after the verbose
            # inspection tree is pruned. These fields are counts/statuses only;
            # they deliberately contain neither API credentials nor source text.
            "storage": {
                "expected_payloads": summary.get("post_upload_expected_payloads", 0),
                "workspace_documents": summary.get("post_upload_matching_workspace_documents", 0),
                "vectors": summary.get("post_upload_matching_vectors", 0),
                "drawer_layout": summary.get("post_upload_desktop_drawer_layout", "not_checked"),
                "drawer_root_files": summary.get("post_upload_desktop_drawer_root_locations", 0),
                "drawer_nested_files": summary.get("post_upload_desktop_drawer_nested_locations", 0),
                "chunk_survival_ratio": summary.get("post_upload_chunk_survival_ratio", 0.0),
            },
            "runtime": {
                "embedder_status": summary.get("anythingllm_runtime_embedder_probe_status", "not_checked"),
                "embedder_provider": summary.get("anythingllm_runtime_embedder_probe_provider", ""),
                "embedder_model": summary.get("anythingllm_runtime_embedder_probe_model", ""),
                "embedding_dimensions": summary.get("anythingllm_runtime_embedder_probe_dimension", 0),
                "vector_checks_passed": summary.get("anythingllm_runtime_vector_checks_passed", 0),
                "vector_checks_total": summary.get("anythingllm_runtime_vector_checks_total", 0),
                "chat_model": summary.get("anythingllm_runtime_chat_model", ""),
                "chat_status": (
                    "pass" if runtime_status == "pass" else "not_checked"
                ),
            },
            "desktop_queue_observation": {
                # This compact receipt survives lean-success cleanup. It is
                # intentionally counts/statuses only: the queue remains
                # diagnostic evidence, while vector and retrieval checks are
                # the completion criteria.
                "records_requested": summary.get("api_embedding_queue_records", 0),
                "observation": summary.get("api_embedding_progress_observation", {}),
            },
        },
        "source": {
            "file": profile.get("source_file", ""),
            "filename": profile.get("filename", ""),
            "title": profile.get("detected_title", ""),
            "author": profile.get("detected_author", ""),
            "sha256": profile.get("source_sha256", ""),
            "pdf_page_count": profile.get("pdf_page_count", 0),
        },
        "preparation": {
            "start_page": summary.get("start_page"),
            "end_page": summary.get("end_page"),
            "segment_mode": summary.get("segment_mode"),
            "segments": summary.get("segments"),
            "chunk_size": summary.get("chunk_size"),
            "chunk_overlap": summary.get("chunk_overlap"),
        },
        "artifacts": {
            "parsed_text": retained_text_path.relative_to(out_root).as_posix(),
            "parsed_text_bytes": retained_text_path.stat().st_size,
            "segments_directory": "segments",
            "retained_segment_files": len(retained_segments),
            "segment_file_naming": "{document}-p{page:03d}-s{within_page:02d}.txt",
        },
        "recovery": {
            "state": "completed",
            "resume_required": False,
            "detailed_evidence_retained": False,
            "note": "Ready runs retain only parsed text and this compact summary. Review-needed or failed runs keep detailed evidence.",
        },
        "deleted_artifact_groups": sorted(deleted),
    }
    write_json(out_root / "run-summary.json", compact)
    return {
        "applied": True,
        "prepared_text": str(retained_text_path),
        "segments_directory": str(out_root / "segments"),
        "retained_segment_files": len(retained_segments),
        "deleted": sorted(deleted),
    }


def retain_successful_run_without_logs(out_root: Path, summary, profile, prepared_text_path: Path, *, segments=()):
    """Keep a successful local run as a flat, text-only export.

    This is intentionally a distinct retention policy from ``lean_success_v1``:
    it keeps no summary, manifest, report, or subfolder.  The caller uses it
    only for the explicit no-logs output mode.  Review-needed and failed runs
    retain their ordinary evidence rather than being made irrecoverable.
    """
    retained = retain_successful_run_leanly(
        out_root,
        summary,
        profile,
        prepared_text_path,
        segments=segments,
    )
    if not retained.get("applied"):
        return retained

    root = Path(out_root)
    source_name = safe_stem(Path(str(profile.get("filename") or "document")).stem) or "document"
    source_hash = str(profile.get("source_sha256") or "").strip().lower()
    unique_suffix = source_hash[:12] or "local"
    prefix = f"{source_name}-{unique_suffix}"
    current_text = Path(str(retained.get("prepared_text") or ""))
    flat_text = root / f"{prefix}-complete-pdf-parsed.txt"
    if not current_text.is_file():
        return {"applied": False, "reason": "prepared_text_missing_after_lean_cleanup"}
    segment_root = Path(str(retained.get("segments_directory") or root / "segments"))
    planned_segments = []
    for segment_path in sorted(segment_root.glob("*.txt")) if segment_root.is_dir() else []:
        match = re.search(r"-p(\d+)-s(\d+)\.txt$", segment_path.name, re.IGNORECASE)
        if not match:
            continue
        flat_segment = root / f"{prefix}-p{int(match.group(1)):03d}-s{int(match.group(2)):02d}.txt"
        planned_segments.append((segment_path, flat_segment))

    # Validate every destination before moving anything. A late collision used
    # to leave a half-promoted no-logs export (transcript moved, later segment
    # still nested), which is especially confusing after a successful run.
    planned_targets = [flat_text, *(target for _source, target in planned_segments)]
    if len({str(path).casefold() for path in planned_targets}) != len(planned_targets):
        raise FileExistsError("No-logs export would create duplicate flat filenames.")
    existing_target = next((path for path in planned_targets if path.exists()), None)
    if existing_target:
        raise FileExistsError(f"Refusing to overwrite no-logs export: {existing_target}")

    current_text.replace(flat_text)
    flat_segments = []
    for segment_path, flat_segment in planned_segments:
        segment_path.replace(flat_segment)
        flat_segments.append(flat_segment)
    if segment_root.is_dir():
        shutil.rmtree(segment_root, ignore_errors=True)
    no_logs_receipts = (
        "run-checkpoint.json",
        "run-checkpoints.jsonl",
        "run-result.json",
        "run-summary.json",
    )
    for name in no_logs_receipts:
        (root / name).unlink(missing_ok=True)
    summary["upload_file"] = str(flat_text)
    summary["variant_outputs"] = {}
    return {
        "applied": True,
        "policy": "flat_local_no_logs_v1",
        "prepared_text": str(flat_text),
        "retained_segment_files": len(flat_segments),
        "segments_directory": "",
        "deleted": [*(retained.get("deleted") or []), *no_logs_receipts],
    }


def detect_body_start_from_outline(outline, include_front_matter=False):
    if not outline:
        return None

    valid_entries = [row for row in outline if row.get("pdf_page", 0) > 0]
    if include_front_matter:
        for row in valid_entries:
            if not is_front_matter_title(row["title"]):
                return row["pdf_page"], f"pdf_outline:{row['title']}"

    for row in valid_entries:
        if is_main_body_title(row["title"]):
            return row["pdf_page"], f"pdf_outline:{row['title']}"

    for row in valid_entries:
        if not is_front_matter_title(row["title"]):
            return row["pdf_page"], f"pdf_outline_fallback:{row['title']}"

    return None


def detect_end_section_from_outline(outline, page_count, min_fraction=0.55):
    if not outline:
        return None
    min_page = max(1, int(page_count * min_fraction))
    for row in outline:
        page = int(row.get("pdf_page") or 0)
        if page < min_page:
            continue
        key = title_key(row.get("title", ""))
        if any(key.startswith(heading.casefold()) for heading in DEFAULT_END_SECTION_HEADINGS):
            return {"page": page, "heading": row["title"], "source": "pdf_outline"}
    return None


def outline_chapter_map(outline, start_page, end_page):
    entries = []
    for row in outline or []:
        page = int(row.get("pdf_page") or 0)
        title = row.get("title") or ""
        if page < start_page:
            continue
        if end_page and page >= end_page:
            continue
        if is_front_matter_title(title) and not is_main_body_title(title):
            continue
        entries.append({"pdf_page": page, "title": title})
    return sorted(entries, key=lambda x: x["pdf_page"])


def outline_chapter_for_page(chapter_map, page_num, current_chapter=""):
    selected = current_chapter
    for entry in chapter_map:
        if entry["pdf_page"] <= page_num:
            selected = entry["title"]
        else:
            break
    return selected


def outline_context_for_page(outline, page_num, start_page=1, end_page=None):
    context = {"part": "", "chapter": "", "section": "", "subsection": "", "chapter_level": None}
    for row in sorted(outline or [], key=lambda item: (int(item.get("pdf_page") or 0), int(item.get("level") or 1))):
        row_page = int(row.get("pdf_page") or 0)
        if row_page < start_page or row_page > page_num:
            continue
        if end_page and row_page >= end_page:
            continue
        title = normalize_text(row.get("title") or "")
        if not title or is_front_matter_title(title):
            continue
        level = int(row.get("level") or 1)
        key = title.casefold()
        if re.match(r"^part\b", key):
            context.update({"part": title, "chapter": "", "section": "", "subsection": "", "chapter_level": level + 1})
            continue
        explicit_chapter = bool(CHAPTER_RE.match(title))
        if explicit_chapter or (level == 1 and not context["part"]):
            context.update({"chapter": title, "section": "", "subsection": "", "chapter_level": level})
            continue
        chapter_level = context["chapter_level"]
        if not context["chapter"] or chapter_level is None or level <= chapter_level:
            context.update({"chapter": title, "section": "", "subsection": "", "chapter_level": level})
        elif level == chapter_level + 1:
            context["section"] = title
            context["subsection"] = ""
        else:
            context["subsection"] = title
    return context


def detect_body_start(pages, stats, outline=None, include_front_matter=False):
    if include_front_matter:
        # The caller explicitly requested the front matter. Do not let the
        # body-start heuristic silently discard it simply because a later page
        # contains an opening heading such as "Introduction".
        first_nonempty = next((s.pdf_page for s in stats if not s.is_empty), 1)
        return first_nonempty, "include_front_matter_first_nonempty"

    outline_start = detect_body_start_from_outline(outline, include_front_matter=include_front_matter)
    if outline_start:
        return outline_start

    page_by_num = {int(page.get("page") or 0): page for page in pages}
    # Retain the dense-prose guard here as well because callers may provide
    # legacy or externally constructed PageStat objects that predate the
    # stricter page-level classifier.
    toc_pages = [
        s.pdf_page
        for s in stats
        if s.is_toc_like and not (s.words >= 180 and s.sentence_marks >= 5)
    ]
    if toc_pages:
        # A contents page is useful for mapping structure but is usually not a
        # good first page for the final RAG artifact.
        toc_page = min(toc_pages)
        for stat in stats:
            if stat.pdf_page > toc_page and stat.words >= 180 and stat.sentence_marks >= 5:
                raw = page_by_num.get(stat.pdf_page, {}).get("text", "")
                heading = detect_heading_from_page_text(raw).casefold()
                if not include_front_matter and any(
                    heading.startswith(token) for token in ["preface", "foreword", "acknowledgment", "acknowledgement"]
                ) and stat.pdf_page <= max(15, math.ceil(len(stats) * 0.2)):
                    continue
                return stat.pdf_page, "after_table_of_contents_prose_density"
        # A page that only *looks* like contents is not enough evidence to
        # discard everything before it. Retain the document and mark the
        # uncertain structural signal for diagnostics instead.
        first_nonempty = next((s.pdf_page for s in stats if not s.is_empty), 1)
        return first_nonempty, "table_of_contents_unconfirmed_retained"

    fallback_opening_headings = OPENING_HEADINGS if include_front_matter else [
        "Introduction",
        "Prologue",
        "Chapter One",
        "Chapter 1",
        "Part One",
        "Part 1",
    ]
    for page_info, stat in zip(pages, stats):
        if stat.pdf_page > 40:
            break
        heading = detect_heading_from_page_text(page_info.get("text", "")).casefold()
        if any(heading.startswith(h.casefold()) for h in fallback_opening_headings):
            return stat.pdf_page, "opening_heading"

    first_nonempty_stat = next((stat for stat in stats if not stat.is_empty), None)
    if (
        first_nonempty_stat
        and first_nonempty_stat.pdf_page == 1
        and first_nonempty_stat.words < 180
        and first_nonempty_stat.words >= 100
        and first_nonempty_stat.sentence_marks >= 5
        and first_nonempty_stat.line_count >= 8
    ):
        # Poetry, an essay opening, or another substantive first page can be
        # below the ordinary dense-prose threshold.  Retain it rather than
        # silently treating short lines as front matter.
        return 1, "substantive_first_page_retained"

    for stat in stats[:50]:
        if stat.words >= 180 and stat.sentence_marks >= 5:
            return stat.pdf_page, "prose_density"

    first_nonempty = next((s.pdf_page for s in stats if not s.is_empty), 1)
    return first_nonempty, "first_nonempty_page"


def detect_heading_from_page_text(text):
    lines = [normalize_text(line) for line in text.splitlines() if normalize_text(line)]
    for line in lines[:12]:
        match = CHAPTER_RE.match(line)
        if match:
            return line[:120]
    for line in lines[:8]:
        if 5 <= len(line) <= 90 and SECTION_RE.match(line):
            if line.upper() == line or line.istitle():
                return line[:120]
    return ""


def looks_like_heading(line):
    clean = normalize_text(line)
    if not (6 <= len(clean) <= 110):
        return False
    if len(clean.split()) > 14:
        return False
    if re.fullmatch(r"[A-Za-z]\.?", clean):
        return False
    if re.fullmatch(r"\d{1,4}", clean):
        return False
    if clean.endswith((".", ",", ";")):
        return False
    if CHAPTER_RE.match(clean):
        return True
    if SECTION_RE.match(clean) and (clean.istitle() or clean.upper() == clean or ":" in clean):
        return True
    return False


def extract_page_headings(text, max_lines=36):
    headings = []
    lines = [normalize_text(line) for line in text.splitlines() if normalize_text(line)]
    for line in lines[:max_lines]:
        if looks_like_heading(line):
            headings.append(line[:120])
    deduped = []
    seen = set()
    for heading in headings:
        key = heading.casefold()
        if key not in seen:
            deduped.append(heading)
            seen.add(key)
    return deduped


def detect_section_from_page_text(text):
    for line in extract_page_headings(text):
        if not CHAPTER_RE.match(line):
            return line[:120]
    return ""


def detect_logical_page_number(text, pdf_page):
    lines = [normalize_text(line) for line in text.splitlines() if normalize_text(line)]
    candidates = lines[:4] + lines[-4:]
    for line in candidates:
        match = re.fullmatch(r"(?:[-\s]*)(\d{1,4}|[ivxlcdm]{1,12})(?:[-\s]*)", line.casefold())
        if not match:
            continue
        value = match.group(1)
        if value.isdigit():
            number = int(value)
            if 0 < number <= max(pdf_page + 20, 2000):
                return str(number)
        else:
            return value
    return ""


def normalize_page_layout_text(text):
    """Normalize extraction noise while retaining paragraph boundaries."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1-\2", text)
    text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", " ", text)
    paragraphs = []
    for paragraph in re.split(r"\n\s*\n+", text):
        clean = normalize_text(paragraph)
        if clean:
            paragraphs.append(clean)
    return "\n\n".join(paragraphs)


def build_page_line_map(text):
    raw_text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    raw_text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1-\2", raw_text)
    raw_lines = raw_text.split("\n")
    paragraphs = []
    current_lines = []

    def flush_paragraph():
        nonlocal current_lines
        if not current_lines:
            return
        logical_lines = []
        for item in current_lines:
            cleaned = normalize_text(item["text"])
            if not cleaned:
                continue
            if logical_lines and re.search(r"[\w\u2019'-]-$", logical_lines[-1]["text"]) and re.match(
                r"^[A-Za-z0-9\u00C0-\u024F]",
                cleaned,
            ):
                logical_lines[-1]["text"] += cleaned
                logical_lines[-1]["page_line_end"] = item["page_line"]
            else:
                logical_lines.append(
                    {
                        "text": cleaned,
                        "page_line_start": item["page_line"],
                        "page_line_end": item["page_line"],
                    }
                )
        if not logical_lines:
            current_lines = []
            return

        paragraph_text_parts = []
        paragraph_lines = []
        cursor = 0
        for index, line in enumerate(logical_lines):
            if index:
                paragraph_text_parts.append(" ")
                cursor += 1
            start = cursor
            paragraph_text_parts.append(line["text"])
            cursor += len(line["text"])
            paragraph_lines.append(
                {
                    "page_line_start": line["page_line_start"],
                    "page_line_end": line["page_line_end"],
                    "char_start": start,
                    "char_end": cursor,
                    "text": line["text"],
                }
            )
        paragraph_text = "".join(paragraph_text_parts)
        paragraphs.append(
            {
                "text": paragraph_text,
                "page_line_start": paragraph_lines[0]["page_line_start"],
                "page_line_end": paragraph_lines[-1]["page_line_end"],
                "lines": paragraph_lines,
            }
        )
        current_lines = []

    for page_line, raw_line in enumerate(raw_lines, start=1):
        if raw_line.strip():
            current_lines.append({"page_line": page_line, "text": raw_line})
        else:
            flush_paragraph()
    flush_paragraph()

    clean_parts = []
    paragraph_spans = []
    cursor = 0
    for index, paragraph in enumerate(paragraphs):
        if index:
            clean_parts.append("\n\n")
            cursor += 2
        start = cursor
        clean_parts.append(paragraph["text"])
        cursor += len(paragraph["text"])
        lines = []
        for line in paragraph["lines"]:
            lines.append(
                {
                    "page_line_start": line["page_line_start"],
                    "page_line_end": line["page_line_end"],
                    "char_start": start + line["char_start"],
                    "char_end": start + line["char_end"],
                    "text": line["text"],
                }
            )
        paragraph_spans.append(
            {
                "char_start": start,
                "char_end": cursor,
                "page_line_start": paragraph["page_line_start"],
                "page_line_end": paragraph["page_line_end"],
                "text": paragraph["text"],
                "lines": lines,
            }
        )
    return {"clean_text": "".join(clean_parts), "paragraphs": paragraph_spans}


def detect_page_line_range(page_line_map, char_start, char_end):
    paragraphs = (page_line_map or {}).get("paragraphs") or []
    if not paragraphs:
        return None, None
    target_start = max(0, int(char_start or 0))
    target_end = max(target_start, int(char_end or target_start))
    matched_lines = []
    for paragraph in paragraphs:
        if paragraph["char_end"] <= target_start or paragraph["char_start"] >= target_end:
            continue
        local_start = max(target_start, paragraph["char_start"])
        local_end = min(target_end, paragraph["char_end"])
        for line in paragraph["lines"]:
            if line["char_end"] <= local_start or line["char_start"] >= local_end:
                continue
            matched_lines.append((line["page_line_start"], line["page_line_end"]))
    if not matched_lines:
        nearest = None
        nearest_distance = None
        for paragraph in paragraphs:
            distance = min(abs(paragraph["char_start"] - target_start), abs(paragraph["char_end"] - target_end))
            if nearest is None or distance < nearest_distance:
                nearest = paragraph
                nearest_distance = distance
        if nearest:
            return nearest["page_line_start"], nearest["page_line_end"]
        return None, None
    return min(item[0] for item in matched_lines), max(item[1] for item in matched_lines)


def strip_repeated_marginalia(text, repeated_headers=None, repeated_footers=None):
    lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    header_keys = {normalize_text(value).casefold() for value in (repeated_headers or []) if value}
    footer_keys = {normalize_text(value).casefold() for value in (repeated_footers or []) if value}
    while lines and normalize_text(lines[0]).casefold() in header_keys:
        lines.pop(0)
    while lines and normalize_text(lines[-1]).casefold() in footer_keys:
        lines.pop()
    return "\n".join(lines)


def _layout_text_key(text):
    """Compare running text without treating changing page labels as content."""
    value = normalize_text(text).casefold()
    value = re.sub(r"\b\d{1,4}\b", "#", value)
    return re.sub(r"\s+", " ", value).strip(" -–—|#")


def _layout_line_rows(page):
    """Return positioned native text lines; no OCR or content mutation occurs here."""
    rows = []
    for block in page.get_text("dict", sort=False).get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = line.get("spans") or []
            text = "".join(str(span.get("text") or "") for span in spans)
            if not normalize_text(text):
                continue
            bbox = line.get("bbox") or (0, 0, 0, 0)
            rows.append(
                {
                    "text": text,
                    "normalized": normalize_text(text),
                    "x0": float(bbox[0]), "y0": float(bbox[1]),
                    "x1": float(bbox[2]), "y1": float(bbox[3]),
                    "font_sizes": [float(span.get("size") or 0) for span in spans],
                    "fonts": [str(span.get("font") or "") for span in spans],
                    # Preserve the positioned pieces as well as the flattened
                    # line.  On a poor historical scan, a handwritten margin
                    # note may be appended to an otherwise good body line.
                    # Retaining this provenance lets the later, high-threshold
                    # margin rule remove only the confirmed outside piece.
                    "spans": [
                        {
                            "text": str(span.get("text") or ""),
                            "x0": float((span.get("bbox") or bbox)[0]),
                            "y0": float((span.get("bbox") or bbox)[1]),
                            "x1": float((span.get("bbox") or bbox)[2]),
                            "y1": float((span.get("bbox") or bbox)[3]),
                            "font": str(span.get("font") or ""),
                            "size": float(span.get("size") or 0),
                        }
                        for span in spans
                    ],
                }
            )
    return rows


def _layout_is_number(text):
    return bool(re.fullmatch(r"(?:[-–—\s]*)(?:\d{1,4}|[ivxlcdm]{1,12})(?:[-–—\s]*)", normalize_text(text), re.I))


def _layout_is_running_name(row):
    words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ.'’-]*", row.get("normalized") or "")
    italic = bool(row.get("fonts")) and all(
        ("it" in font.casefold() or "oblique" in font.casefold())
        for font in row["fonts"] if font
    )
    return italic and 2 <= len(words) <= 5 and looks_like_person_name(" ".join(words))


def _layout_web_footer_start(rows):
    """Return the visual start of an unmistakable publisher/web footer block."""
    filed_rows = [row for row in rows if re.fullmatch(r"filed under:?", row.get("normalized", ""), re.I)]
    if not filed_rows:
        return None
    start_y = min(row["y0"] for row in filed_rows)
    tail = " ".join(row.get("normalized", "") for row in rows if row["y0"] >= start_y - 1).casefold()
    signals = (
        "filed under",
        "privacy policy",
        "terms of service",
        "sign up",
        "your email",
        "about us",
        "advertising",
        "request a correction",
        "rss",
    )
    # A tag list by itself is not enough. Require a recognisable navigation or
    # signup/legal cluster before suppressing a whole tail block.
    if sum(signal in tail for signal in signals) >= 5 and (
        "privacy policy" in tail or "terms of service" in tail
    ):
        return start_y
    return None


def _layout_alpha_count(text):
    return len(re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]", str(text or "")))


def _layout_marginal_annotation_plan(rows, width, height):
    """Identify a photographed scan's handwritten outer margin with strong evidence.

    This deliberately activates only where long prose establishes a stable body
    rectangle *and* there are many independent outside-body spans.  It is not a
    generic crop: ordinary narrow columns, page labels, and one-off sidebars do
    not meet the evidence threshold and stay untouched.
    """
    prose_rows = [
        row for row in rows
        if _layout_alpha_count(row.get("text")) >= 35
        and row["x0"] >= -1
        and row["x1"] <= width + 1
        and row["y0"] >= height * .06
        and row["y1"] <= height * .93
        and (row["x1"] - row["x0"]) >= width * .35
    ]
    if len(prose_rows) < 8:
        return {"applied": False, "reason": "insufficient_long_prose_rows"}
    left = statistics.median(row["x0"] for row in prose_rows)
    right = statistics.median(row["x1"] for row in prose_rows)
    if right - left < width * .45:
        return {"applied": False, "reason": "body_bounds_too_narrow"}
    # A small tolerance retains natural ragged body lines, including an
    # occasional first-letter indentation, while excluding a separate margin.
    left_bound = max(0.0, left - width * .018)
    right_bound = min(width, right + width * .018)
    outside_candidates = []
    candidates = set()
    bands = set()
    for row_index, row in enumerate(rows):
        for span_index, span in enumerate(row.get("spans") or []):
            outside = span["x1"] < left_bound or span["x0"] > right_bound
            text = normalize_text(span.get("text"))
            if not text:
                continue
            alpha = _layout_alpha_count(text)
            # Digits and a simple hyphen are common in legitimate narrow
            # category columns (for example A-1 or 2024-25), not handwriting.
            symbol_count = len(re.findall(r"[^A-Za-zÀ-ÖØ-öø-ÿ0-9\s-]", text))
            annotation_like = (
                alpha == 0
                or symbol_count >= 2
                or "hiddenhorzocr" in str(span.get("font") or "").casefold()
            )
            if outside and annotation_like:
                outside_candidates.append((row_index, span_index))
                candidates.add((row_index, span_index))
                bands.add(min(7, max(0, int(span["y0"] / max(height, 1) * 8))))
    # Multiple bands prevent a real page number, a single sidebar, or a header
    # from triggering content deletion. The outside pieces are only removed
    # after this document/page-local confirmation succeeds.
    if len(outside_candidates) < 8 or len(bands) < 4:
        return {
            "applied": False,
            "reason": "outside_span_evidence_below_threshold",
            "body_bounds": [round(left_bound, 2), round(right_bound, 2)],
            "outside_span_count": len(outside_candidates),
            "outside_vertical_band_count": len(bands),
        }
    # Once a page has the independent outside-margin evidence above, we can
    # also remove unmistakable pen/OCR debris which overlaps the final quarter
    # of a body line. It must be both edge-adjacent and symbol-heavy: an
    # ordinary word, italic phrase, or narrow categorical column stays intact.
    body_span = max(right_bound - left_bound, 1)
    for row_index, row in enumerate(rows):
        for span_index, span in enumerate(row.get("spans") or []):
            text = normalize_text(span.get("text"))
            if not text:
                continue
            near_outer_edge = (
                span["x0"] > left_bound + body_span * .76
                or span["x1"] < left_bound + body_span * .24
            )
            alpha = _layout_alpha_count(text)
            symbol_count = len(re.findall(r"[^A-Za-zÀ-ÖØ-öø-ÿ0-9\s-]", text))
            noisy = symbol_count >= 2 and alpha / max(len(text), 1) < .78
            if near_outer_edge and noisy:
                candidates.add((row_index, span_index))
    return {
        "applied": True,
        "body_bounds": [round(left_bound, 2), round(right_bound, 2)],
        "outside_span_count": len(outside_candidates),
        "outside_vertical_band_count": len(bands),
        "candidate_span_count": len(candidates),
        "candidate_spans": candidates,
    }


def _layout_text_noise_score(text):
    """Return a deliberately simple OCR-noise signal for same-page comparison."""
    value = str(text or "")
    return (
        len(re.findall(r"[^A-Za-zÀ-ÖØ-öø-ÿ0-9\s.,;:!?()'’\"-]{2,}", value))
        + len(re.findall(r"[A-Za-z][^A-Za-zÀ-ÖØ-öø-ÿ\s]{2,}[A-Za-z]", value))
    )


def _reocr_confirmed_native_body_region(pdf_path, page_number, body_bounds, page_width):
    """OCR only a confirmed printed body rectangle on an annotated scan.

    This path is intentionally unavailable to ordinary native pages. It is a
    recovery for a bad embedded OCR layer where geometry proves that margin
    handwriting is contaminating the body lines. PDF page identity is retained.
    """
    runtime = unstructured_runtime_status("ocr_only")
    tesseract = str(runtime.get("tesseract_executable") or "").strip()
    if not tesseract or not Path(tesseract).exists() or not body_bounds:
        return ""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return ""
    try:
        with fitz.open(pdf_path) as document:
            page = document.load_page(int(page_number) - 1)
            # At this source's print density, 2x retains letterforms while
            # making faint pencil strokes less competitive with the type. The
            # higher 2.5x render made the marginal handwriting more legible to
            # OCR without recovering extra printed prose.
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        left, right = [float(value) for value in body_bounds]
        # Move *inside* the inferred body by a tiny guard. The bounds come
        # from printed prose, while a handwritten mark can overlap their
        # outermost glyph; expanding the crop would reintroduce that mark.
        horizontal_guard = float(page_width) * .010
        crop = (
            max(0, int((left + horizontal_guard) / page_width * image.width)),
            int(image.height * .065),
            min(image.width, int((right - horizontal_guard) / page_width * image.width)),
            int(image.height * .95),
        )
        if crop[2] - crop[0] < image.width * .4:
            return ""
        with tempfile.TemporaryDirectory(prefix="rag-native-body-reocr-") as temp_dir:
            image_path = Path(temp_dir) / "body.png"
            ImageOps.autocontrast(image.crop(crop).convert("L")).save(image_path)
            completed = subprocess.run(
                [tesseract, str(image_path), "stdout", "--psm", "4", "-l", "eng"],
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
        if completed.returncode:
            return ""
        return completed.stdout.strip()
    except (OSError, subprocess.TimeoutExpired, RuntimeError, ValueError):
        return ""


def _layout_photographed_spread_columns(rows, width, height):
    """Return two reading regions only for a visibly overlapping book spread.

    A photographed open book can contain two separately printed pages whose
    inner edges overlap around the camera's fold.  Sorting such rows by y/x
    interleaves the two pages.  This is intentionally stricter than ordinary
    two-column detection: both long regions must cover much of the page and
    their median inner edges must overlap at the physical centre.  A true
    two-facing-page spread is materially landscape; a portrait page can have
    poetry, quotations, or indented prose that creates the same coordinate
    overlap without a camera fold.  A normal journal layout has a real gutter,
    so it cannot take this path either.
    """
    if not rows or width <= 0 or height <= 0:
        return None
    if float(width) / max(float(height), 1.0) < 1.22:
        return None
    prose = [
        row for row in rows
        if _layout_alpha_count(row.get("text")) >= 25
        and (row["x1"] - row["x0"]) >= width * .24
    ]
    left = [row for row in prose if (row["x0"] + row["x1"]) / 2 < width * .49]
    right = [row for row in prose if (row["x0"] + row["x1"]) / 2 > width * .51]

    def coverage(values):
        return max(row["y1"] for row in values) - min(row["y0"] for row in values) if values else 0

    if len(left) < 10 or len(right) < 10:
        return None
    if coverage(left) < height * .30 or coverage(right) < height * .30:
        return None
    left_inner = statistics.median(row["x1"] for row in left)
    right_inner = statistics.median(row["x0"] for row in right)
    fold_overlap = left_inner - right_inner
    fold_centre = (left_inner + right_inner) / 2
    # A real two-column gutter must remain visible.  Permit an off-centre
    # camera fold—cropped phone scans often place it at 55% of the canvas—yet
    # require a material overlap within the central half of the image.  This
    # keeps the rule narrower than generic two-column detection.
    if fold_overlap < width * .02 or not (width * .30 <= fold_centre <= width * .70):
        return None
    return {
        "left": sorted(left, key=lambda row: (row["y0"], row["x0"])),
        "right": sorted(right, key=lambda row: (row["y0"], row["x0"])),
        "fold_overlap_points": round(fold_overlap, 2),
        "left_line_count": len(left),
        "right_line_count": len(right),
    }


def _layout_reading_order(rows, width, height):
    """Use column-first reading order only with strong two-column evidence."""
    if not rows:
        return [], "empty", None
    photographed_spread = _layout_photographed_spread_columns(rows, width, height)
    if photographed_spread:
        ordered = photographed_spread["left"] + photographed_spread["right"]
        regions = [
            {
                "text": "\n".join(row["text"].rstrip() for row in photographed_spread[side]).strip(),
                "reading_region": f"photographed_spread_{side}",
                "reading_region_index": index,
                "reading_region_count": 2,
                "source_column_index": index,
            }
            for index, side in enumerate(("left", "right"), start=1)
        ]
        return ordered, "photographed_spread_column_first", regions
    # A page title, centered byline, or author name can begin above the left
    # column yet cross the gutter. Treating it as left-column material would
    # place a centred "By" before the title when all wide rows are emitted
    # first. A row that spans the real gutter belongs to the visual preamble,
    # not either column.
    wide_or_gutter_crossing = [
        row
        for row in rows
        if (
            row["x1"] - row["x0"] >= width * 0.55
            or (row["x0"] < width * 0.48 and row["x1"] > width * 0.52)
        )
    ]
    left = [row for row in rows if row not in wide_or_gutter_crossing and row["x0"] < width * 0.48]
    right = [row for row in rows if row not in wide_or_gutter_crossing and row["x0"] >= width * 0.50]

    def covered(values):
        return max(row["y1"] for row in values) - min(row["y0"] for row in values) if values else 0
    if len(left) >= 10 and len(right) >= 10 and covered(left) >= height * 0.25 and covered(right) >= height * 0.25:
        # The first true right-column line establishes the start of the
        # two-column body.  Before it, left-aligned labels such as "Abstract"
        # still belong to the visual preamble; treating every narrow line as
        # left-column content would place such a label after its full-width
        # abstract.  This is document-local geometry, not a heading word list.
        column_body_start = min(row["y0"] for row in right)
        preamble = [row for row in rows if row["y0"] < column_body_start]
        body_left = [row for row in left if row["y0"] >= column_body_start]
        body_right = [row for row in right if row["y0"] >= column_body_start]
        # A source line cannot straddle both detected columns in this mode.
        # Keep any post-preamble gutter-crossing material in visual order
        # before the column bodies, as the conservative existing behaviour.
        body_wide = [
            row for row in rows
            if row not in preamble and row not in body_left and row not in body_right
        ]
        ordered = sorted(preamble, key=lambda row: (row["y0"], row["x0"]))
        ordered += sorted(body_wide, key=lambda row: (row["y0"], row["x0"]))
        ordered += sorted(body_left, key=lambda row: (row["y0"], row["x0"]))
        ordered += sorted(body_right, key=lambda row: (row["y0"], row["x0"]))
        return ordered, "two_column_column_first", None
    return sorted(rows, key=lambda row: (row["y0"], row["x0"])), "visual_line_order", None


def apply_region_aware_native_layout(pdf_path, pages):
    """Create conservative semantic text from positioned native PDF lines.

    The original extracted page text is retained in ``raw_text``. Only strong
    top/bottom marginalia signals are removed from semantic text. A lower-page
    note is excluded only when its numbered/symbol marker, small type, position,
    continuation, and meaningful length agree; otherwise it remains embedded
    and is reported as a review candidate.
    """
    page_layouts = {}
    with fitz.open(pdf_path) as document:
        for page_number in range(1, int(document.page_count or 0) + 1):
            page = document.load_page(page_number - 1)
            rows = _layout_line_rows(page)
            page_layouts[page_number] = {
                "width": float(page.rect.width), "height": float(page.rect.height), "rows": rows,
            }
    top_counts = Counter()
    bottom_counts = Counter()
    for layout in page_layouts.values():
        for row in layout["rows"]:
            # PDF media boxes sometimes retain a large blank printer margin.
            # In those files a visually top-of-page running head can sit at
            # thirteen percent of the coordinate height. The broader band is
            # used only with repeated-text/page-number evidence below, never
            # to remove a one-off heading or body paragraph.
            top_text = normalize_text(row["text"])
            if (
                row["y1"] <= layout["height"] * 0.15
                and not _layout_is_number(row["text"])
                # A repeated, complete prose sentence near the top can be a
                # genuine repeated quotation or fixture content. Running
                # heads are labels, not sentence-final prose.
                and not re.search(r"[.!?]$", top_text)
            ):
                key = _layout_text_key(row["text"])
                if key:
                    top_counts[key] += 1
            if row["y0"] >= layout["height"] * 0.90 and not _layout_is_number(row["text"]):
                key = _layout_text_key(row["text"])
                if key:
                    bottom_counts[key] += 1
    # Journals commonly alternate a running author header with a running title
    # header. In a short five-page article, each legitimate repeating header
    # may therefore occur only twice. Two exact top-margin repetitions are
    # already strong document-local evidence and prevent the final odd/even
    # page from leaking a running header into the prepared text.
    repeat_threshold = max(2, math.ceil(len(page_layouts) * 0.08))
    transformed = []
    review_pages = []
    for page_info in pages:
        page_number = int(page_info.get("page") or 0)
        layout = page_layouts.get(page_number)
        if not isinstance(layout, dict):
            transformed.append(page_info)
            continue
        layout_height = float(layout["height"])
        removed = []
        body_rows = []
        retained_note_candidates = []
        excluded_footnotes = []
        annotation_plan = _layout_marginal_annotation_plan(
            layout["rows"], layout["width"], layout_height
        )
        annotation_candidates = annotation_plan.pop("candidate_spans", set())
        body_reocr_text = ""
        if annotation_plan.get("applied"):
            candidate_text = _reocr_confirmed_native_body_region(
                pdf_path,
                page_number,
                annotation_plan.get("body_bounds"),
                layout["width"],
            )
            raw_text = str(page_info.get("text") or "")
            # Select the fresh OCR only when it contains a substantial share
            # of the original prose and demonstrably reduces corruption. This
            # comparison makes a poorer fallback impossible to choose merely
            # because a margin was detected.
            if (
                len(candidate_text) >= 800
                and _layout_alpha_count(candidate_text) >= _layout_alpha_count(raw_text) * .70
                and _layout_text_noise_score(candidate_text) <= _layout_text_noise_score(raw_text) * .65
            ):
                body_reocr_text = candidate_text
        if annotation_plan.get("applied"):
            cleaned_rows = []
            for row_index, row in enumerate(layout["rows"]):
                kept_spans = [
                    span for span_index, span in enumerate(row.get("spans") or [])
                    if (row_index, span_index) not in annotation_candidates
                ]
                if len(kept_spans) == len(row.get("spans") or []):
                    cleaned_rows.append(row)
                    continue
                retained_text = "".join(span["text"] for span in kept_spans)
                removed_text = "".join(
                    span["text"] for span_index, span in enumerate(row.get("spans") or [])
                    if (row_index, span_index) in annotation_candidates
                )
                if normalize_text(retained_text):
                    cleaned_rows.append({
                        **row,
                        "text": retained_text,
                        "normalized": normalize_text(retained_text),
                        "font_sizes": [span["size"] for span in kept_spans],
                        "fonts": [span["font"] for span in kept_spans],
                        "spans": kept_spans,
                    })
                removed.append({
                    "text": normalize_text(removed_text),
                    "reason": "confirmed_outer_margin_annotation",
                    "bbox": [row["x0"], row["y0"], row["x1"], row["y1"]],
                })
            layout_rows = cleaned_rows
        else:
            layout_rows = layout["rows"]
        body_sizes = sorted(size for row in layout["rows"] for size in row["font_sizes"] if size > 0)
        # The upper quartile is a safer body-text baseline than the median on
        # a short page dominated by a multi-line footnote.
        body_text_size = body_sizes[min(len(body_sizes) - 1, math.ceil(len(body_sizes) * 0.75))] if body_sizes else 0
        for row in layout_rows:
            top = row["y1"] <= layout["height"] * 0.15
            bottom = row["y0"] >= layout["height"] * 0.90
            key = _layout_text_key(row["text"])
            reason = ""
            if (top or bottom) and _layout_is_number(row["text"]):
                reason = "positioned_page_number"
            elif top and top_counts.get(key, 0) >= repeat_threshold:
                reason = "repeated_running_header"
            elif bottom and bottom_counts.get(key, 0) >= repeat_threshold:
                reason = "repeated_running_footer"
            elif top and _layout_is_running_name(row):
                reason = "italic_running_author"
            if reason:
                removed.append({"text": row["normalized"], "reason": reason, "bbox": [row["x0"], row["y0"], row["x1"], row["y1"]]})
                continue
            body_rows.append(row)

        web_footer_start = _layout_web_footer_start(body_rows)
        if web_footer_start is not None:
            retained_body_rows = []
            for row in body_rows:
                if row["y0"] >= web_footer_start - 1:
                    removed.append({
                        "text": row["normalized"],
                        "reason": "high_confidence_web_footer_boilerplate",
                        "bbox": [row["x0"], row["y0"], row["x1"], row["y1"]],
                    })
                else:
                    retained_body_rows.append(row)
            body_rows = retained_body_rows

        def small_lower_rows(row):
            return (
                row["y0"] >= layout_height * 0.78
                and body_text_size
                and row["font_sizes"]
                and max(row["font_sizes"]) <= body_text_size - 1.5
            )
        excluded_ids = set()
        for index, row in enumerate(body_rows):
            if not (
                small_lower_rows(row)
                and re.match(r"^\s*(?:\d{1,3}|[*†‡])(?:[.)\]]|\s)", row["normalized"])
            ):
                continue
            group = [row]
            previous_y = row["y1"]
            for following in body_rows[index + 1:]:
                if following["y0"] < row["y0"] or following["y0"] - previous_y > 42:
                    break
                if not small_lower_rows(following):
                    break
                group.append(following)
                previous_y = following["y1"]
            if len(group) >= 2 and sum(len(item["normalized"]) for item in group) >= 45:
                for item in group:
                    excluded_ids.add(id(item))
                excluded_footnotes.append({
                    "text": " ".join(item["normalized"] for item in group),
                    "reason": "high_confidence_lower_page_footnote",
                    "line_count": len(group),
                    "bbox": [group[0]["x0"], group[0]["y0"], group[-1]["x1"], group[-1]["y1"]],
                })
            else:
                retained_note_candidates.append({"text": row["normalized"], "bbox": [row["x0"], row["y0"], row["x1"], row["y1"]]})
        retained = [row for row in body_rows if id(row) not in excluded_ids]
        ordered, reading_order, reading_regions = _layout_reading_order(
            retained, layout["width"], layout["height"]
        )
        semantic_text = "\n".join(row["text"].rstrip() for row in ordered).strip()
        if body_reocr_text:
            semantic_text = body_reocr_text
            reading_order = "reocr_confirmed_annotated_body"
            annotation_plan["body_reocr"] = {
                "selected": True,
                "method": "tesseract_confirmed_native_body_crop",
                "raw_noise_score": _layout_text_noise_score(page_info.get("text", "")),
                "reocr_noise_score": _layout_text_noise_score(body_reocr_text),
            }
        else:
            annotation_plan["body_reocr"] = {"selected": False}
        transformed.append({
            **page_info,
            "raw_text": page_info.get("text", ""),
            "text": semantic_text or page_info.get("text", ""),
            "layout_reading_order": reading_order,
            "layout_removed_marginalia": removed,
            "layout_note_candidates": retained_note_candidates,
            "layout_excluded_footnotes": excluded_footnotes,
            # These regions keep the physical PDF page identity intact while
            # making the two photographed halves independently traceable.
            **({"reading_regions": reading_regions} if reading_regions else {}),
        })
        review_pages.append({
            "pdf_page": page_number,
            "reading_order": reading_order,
            "outer_margin_annotation": annotation_plan,
            "removed_marginalia": removed,
            "note_candidates_retained": retained_note_candidates,
            "excluded_footnotes": excluded_footnotes,
            "photographed_spread": {
                "detected": bool(reading_regions),
                "reading_region_count": len(reading_regions or []),
            },
        })
    return transformed, {
        "status": "applied",
        "method": "native_positioned_lines_conservative_v1",
        "repeat_threshold": repeat_threshold,
        "removed_marginalia_count": sum(len(row["removed_marginalia"]) for row in review_pages),
        "note_candidates_retained_count": sum(len(row["note_candidates_retained"]) for row in review_pages),
        "excluded_footnote_count": sum(len(row["excluded_footnotes"]) for row in review_pages),
        "two_column_page_count": sum(row["reading_order"] == "two_column_column_first" for row in review_pages),
        "photographed_spread_page_count": sum(
            row["reading_order"] == "photographed_spread_column_first" for row in review_pages
        ),
        "pages": review_pages,
    }


def _lane_heading_matches(stat, labels):
    """Return whether a page starts with a structural end-matter heading."""
    preview = normalize_text(getattr(stat, "preview", ""))
    return bool(re.match(rf"^(?:{'|'.join(re.escape(label) for label in labels)})\b", preview, re.I))


def proposed_supplementary_lane_review(segments, stats, pdf_page_count, layout_evidence=None):
    """Identify document-scoped non-prose candidates before payload assembly.

    This is deliberately a small, document-scoped classifier.  It does not use
    global replacement rules and never rewrites ``segments``.  The separate
    promotion step below can exclude only an automatically eligible, sustained references or
    index region; all other medium-confidence candidates remain review-only.
    """
    stats_by_page = {int(stat.pdf_page): stat for stat in stats}
    page_count = max(1, int(pdf_page_count or len(stats_by_page) or 1))

    def sustained_pages(page_numbers, minimum=2):
        """Return pages in a consecutive document-local run, never one noisy page."""
        qualifying = set()
        run = []
        for page_number in sorted(set(page_numbers)):
            if run and page_number != run[-1] + 1:
                if len(run) >= minimum:
                    qualifying.update(run)
                run = []
            run.append(page_number)
        if len(run) >= minimum:
            qualifying.update(run)
        return qualifying

    def region_for(page_number, eligible_pages):
        """Return the one consecutive region containing a classified page."""
        region = {int(page_number)}
        left = int(page_number) - 1
        while left in eligible_pages:
            region.add(left)
            left -= 1
        right = int(page_number) + 1
        while right in eligible_pages:
            region.add(right)
            right += 1
        return sorted(region)

    index_runs = sustained_pages(
        page_number for page_number, stat in stats_by_page.items() if stat.is_index_like
    )
    reference_runs = sustained_pages(
        page_number for page_number, stat in stats_by_page.items() if stat.is_bibliography_like
    )
    segment_text_by_page = {}
    for segment in segments:
        page_number = int(segment.get("pdf_page") or 0)
        if page_number > 0:
            segment_text_by_page.setdefault(page_number, []).append(
                str(segment.get("text") or "")
            )

    def is_short_reference_continuation(page_number):
        """Recognize a final partial page that continues a proven source-notes run.

        It is not a generic citation detector.  The page must immediately
        follow a sustained, late bibliography run, be short, and contain at
        least three numbered source-note starts plus multiple citation signals.
        This covers a final continuation page whose first line began on the
        preceding page, without treating normal academic prose as end matter.
        """
        stat = stats_by_page.get(page_number)
        text = " ".join(segment_text_by_page.get(page_number, [])).strip()
        if not stat or not text or stat.words > 220 or stat.line_count < 8:
            return False
        numbered_note_starts = len(re.findall(r"(?<!\w)\d{1,3}\s+(?=[A-Z])", text))
        citation_signals = len(re.findall(
            r"\b(?:press|review|journal|university|times|speech|vol\.?|https?://|doi(?:\.org)?|pp?\.)\b",
            text,
            re.I,
        ))
        return numbered_note_starts >= 3 and citation_signals >= 3

    # A bibliography often ends with one short page whose first source note
    # started on the prior page. Include only this tightly evidenced adjacent
    # continuation in the same document-local region.
    reference_continuations = set()
    while True:
        continuation = {
            page_number
            for page_number in stats_by_page
            if page_number - 1 in reference_runs
            and page_number not in reference_runs
            and page_number >= math.ceil(page_count * 0.75)
            and is_short_reference_continuation(page_number)
        }
        if not continuation:
            break
        reference_continuations.update(continuation)
        reference_runs.update(continuation)
    candidate_pages = {}
    for page_number, stat in stats_by_page.items():
        late_page = page_number >= max(3, math.ceil(page_count * 0.55))
        explicit_index = _lane_heading_matches(stat, ("index",))
        explicit_references = _lane_heading_matches(
            stat, ("references", "bibliography", "works cited")
        )
        index_structure = bool(
            stat.is_index_like
            and late_page
            and (
                explicit_index
                or (
                    page_number in index_runs
                    and page_number >= math.ceil(page_count * 0.75)
                    and stat.line_count >= 25
                    and stat.sentence_marks <= max(3, stat.words // 100)
                )
            )
        )
        bibliography_structure = bool(
            late_page
            and (
                explicit_references
                or (
                    page_number in reference_runs
                    and page_number >= math.ceil(page_count * 0.75)
                    and (
                        stat.is_bibliography_like
                        and stat.line_count >= 15
                        or page_number in reference_continuations
                    )
                )
            )
        )
        if index_structure:
            candidate_pages[page_number] = {
                "reason": "explicit_index_page" if explicit_index else "sustained_index_region",
                "confidence": "medium",
                "evidence": "explicit_index_heading" if explicit_index else "sustained_index_like_pages",
                "classification_scope": "page_region",
                "scope_pages": [page_number] if explicit_index else region_for(page_number, index_runs),
            }
        elif bibliography_structure:
            candidate_pages[page_number] = {
                "reason": "explicit_references_page" if explicit_references else "sustained_references_region",
                "confidence": "medium",
                "evidence": "explicit_references_heading" if explicit_references else "sustained_bibliography_like_pages",
                "classification_scope": "page_region",
                "scope_pages": [page_number] if explicit_references else region_for(page_number, reference_runs),
            }

    items = []
    for segment in segments:
        page_number = int(segment.get("pdf_page") or 0)
        classification = candidate_pages.get(page_number)
        stat = stats_by_page.get(page_number)
        text = str(segment.get("text") or "").strip()
        if not classification and stat and stat.image_count > 0 and len(text) <= 320:
            if re.match(
                r"^(?:photo(?:graph)?|image|illustration)?\s*(?:credit|courtesy|source)\s*[:—-]|^(?:©|copyright)\b",
                text,
                re.I,
            ):
                classification = {
                    "reason": "possible_image_credit",
                    "confidence": "medium",
                    "evidence": "short_credit_label_on_image_page",
                    "classification_scope": "segment",
                    "scope_pages": [page_number],
                }
        if not classification:
            continue
        items.append(
            {
                "kind": "segment",
                "proposed_lane": "supplementary",
                "confidence": classification["confidence"],
                "reason": classification["reason"],
                "evidence": classification.get("evidence", ""),
                "promotion_eligibility": "not_automatically_eligible",
                "classification_scope": classification.get("classification_scope", "segment"),
                "scope_pages": classification.get("scope_pages", [page_number]),
                "numeric_token_interpretation": "not_classified_as_page_locator",
                "pdf_page": page_number,
                "segment_id": segment.get("segment_id", ""),
                "text": segment.get("text", ""),
            }
        )
    for page in (layout_evidence or {}).get("pages", []):
        for note in page.get("note_candidates_retained", []):
            items.append(
                {
                    "kind": "positioned_line",
                    "proposed_lane": "supplementary",
                    "confidence": "medium",
                    "reason": "possible_lower_page_note",
                    "evidence": "positioned_lower_page_note_candidate",
                    "promotion_eligibility": "not_automatically_eligible",
                    "classification_scope": "positioned_line",
                    "scope_pages": [int(page.get("pdf_page") or 0)],
                    "numeric_token_interpretation": "not_classified_as_page_locator",
                    "pdf_page": int(page.get("pdf_page") or 0),
                    "segment_id": "",
                    "bbox": note.get("bbox", []),
                    "text": note.get("text", ""),
                }
            )
    return {
        "status": "review_only",
        "policy": "review_only_when_no_narrow_automatic_rule_applies",
        "primary_payload_changed": False,
        "proposed_supplementary_count": len(items),
        "proposed_supplementary_segment_count": sum(item["kind"] == "segment" for item in items),
        "proposed_supplementary_positioned_line_count": sum(item["kind"] == "positioned_line" for item in items),
        "items": items,
    }


AUTOMATIC_PRIMARY_SUPPLEMENTARY_REASONS = frozenset({
    "sustained_index_region",
    "sustained_references_region",
})


def apply_automatic_supplementary_lane(segments, lane_review, exclude_from_primary=False):
    """Classify supplementary regions and optionally exclude them from upload.

    The default retains front matter, end matter, and references in the primary
    payload: they remain searchable and every physical PDF page stays
    representable for citation.  An explicit caller opt-in may exclude only a
    document-local sustained index/bibliography run.  A lone date, URL,
    citation, image credit, lower-page note, or isolated explicit heading is
    never removed.  The classification evidence always remains available in
    the lane-review artifacts.
    """
    reviewed_items = [dict(item) for item in lane_review.get("items", [])]
    excluded_ids = {
        str(item.get("segment_id") or "")
        for item in reviewed_items
        if exclude_from_primary
        and item.get("kind") == "segment"
        and item.get("reason") in AUTOMATIC_PRIMARY_SUPPLEMENTARY_REASONS
        and str(item.get("segment_id") or "")
    }
    primary_segments = [
        segment for segment in segments
        if str(segment.get("segment_id") or "") not in excluded_ids
    ]
    for item in reviewed_items:
        if str(item.get("segment_id") or "") in excluded_ids:
            item["applied_to_primary_payload"] = True
            item["promotion_eligibility"] = "automatic_document_scoped_exclusion"
        else:
            item["applied_to_primary_payload"] = False

    applied_count = len(segments) - len(primary_segments)
    review = dict(lane_review)
    review["items"] = reviewed_items
    review["status"] = "applied" if applied_count else "review_only"
    review["policy"] = (
        "automatic_document_scoped_sustained_reference_index_exclusion"
        if applied_count else (
            "opt_in_exclusion_not_requested"
            if any(item.get("reason") in AUTOMATIC_PRIMARY_SUPPLEMENTARY_REASONS for item in reviewed_items)
            else "no_matching_automatic_reference_index_region"
        )
    )
    review["primary_payload_changed"] = bool(applied_count)
    review["primary_excluded_segment_count"] = applied_count
    review["review_only_candidate_count"] = sum(
        not item.get("applied_to_primary_payload") for item in reviewed_items
    )
    return primary_segments, review


def write_supplementary_lane_candidate_text(path, lane_review):
    """Write page-grouped candidate context rather than isolated text fragments."""
    def page_label(pages):
        ordered = sorted({int(page) for page in pages if int(page or 0) > 0})
        if not ordered:
            return "unknown"
        return str(ordered[0]) if len(ordered) == 1 else f"{ordered[0]}–{ordered[-1]}"

    grouped = {}
    individual = []
    for item in lane_review.get("items", []):
        if item.get("kind") == "segment" and item.get("classification_scope") == "page_region":
            key = (
                tuple(item.get("scope_pages") or [item.get("pdf_page")]),
                item.get("reason", "unspecified"),
                item.get("evidence", ""),
                item.get("confidence", "medium"),
            )
            grouped.setdefault(key, []).append(item)
        else:
            individual.append(item)
    blocks = []
    for (scope_pages, reason, evidence, confidence), items in sorted(grouped.items()):
        applied = all(item.get("applied_to_primary_payload") for item in items)
        label = "SUPPLEMENTARY REGION" if applied else "REVIEW-ONLY SUPPLEMENTARY REGION"
        primary_message = (
            "Primary upload: automatically excluded by the sustained reference/index-region rule. "
            "Original text: preserved here for audit."
            if applied else
            "Primary upload: retained because this candidate is not automatically eligible for exclusion."
        )
        blocks.append(
            "\n".join(
                [
                    f"[{label} | pages {page_label(scope_pages)} | {reason} | {confidence} confidence]",
                    f"Classification scope: page region. Evidence: {evidence}.",
                    "Dates, URL date slugs, and page-like numbers inside the text were not individually classified as page locators.",
                    primary_message,
                ]
            )
        )
        by_page = {}
        for item in items:
            by_page.setdefault(int(item.get("pdf_page") or 0), []).append(item)
        for page_number, page_items in sorted(by_page.items()):
            text = "\n".join(str(item.get("text") or "").strip() for item in page_items).strip()
            blocks.append(f"[PDF page {page_number}]\n{text}")
    for item in individual:
        page = item.get("pdf_page") or "unknown"
        header = (
            f"[REVIEW-ONLY SUPPLEMENTARY CANDIDATE | page {page} | "
            f"{item.get('reason', 'unspecified')} | {item.get('confidence', 'medium')} confidence]"
        )
        blocks.append(
            f"{header}\nClassification scope: {item.get('classification_scope', 'segment')}; "
            f"evidence: {item.get('evidence', 'unspecified')}.\n"
            f"{item.get('text', '').strip()}"
        )
    path.write_text("\n\n".join(block for block in blocks if block.strip()) + ("\n" if blocks else ""), encoding="utf-8")


def split_page_with_offsets(clean, target_chars=650, min_boundary=250):
    if not clean:
        return []
    segments = []
    start = 0
    n = len(clean)
    while start < n:
        remaining = n - start
        if remaining <= target_chars:
            end = n
        else:
            window = clean[start : start + target_chars]
            candidates = [
                (window.rfind("\n\n"), 0),
                (window.rfind(". "), 1),
                (window.rfind("? "), 1),
                (window.rfind("! "), 1),
                (window.rfind("; "), 1),
                (window.rfind(", "), 1),
                (window.rfind(" "), 0),
            ]
            cut = -1
            adjust = 0
            for candidate, candidate_adjust in candidates:
                if candidate >= min_boundary:
                    cut = candidate
                    adjust = candidate_adjust
                    break
            if cut < min_boundary:
                cut = target_chars
                adjust = 0
            end = min(n, start + cut + adjust)
        piece = clean[start:end].strip()
        if piece:
            leading_ws = len(clean[start:end]) - len(clean[start:end].lstrip())
            trailing_ws = len(clean[start:end].rstrip())
            segments.append(
                {
                    "text": piece,
                    "char_start_page": start + leading_ws,
                    "char_end_page": start + trailing_ws,
                }
            )
        start = max(end, start + 1)
        while start < n and clean[start].isspace():
            start += 1
    return segments


def merge_short_page_segments(page_segments, min_chars=180, max_chars=950):
    merged = []
    for segment in page_segments:
        if (
            merged
            and len(segment["text"]) < min_chars
            and len(merged[-1]["text"]) + len(segment["text"]) + 1 <= max_chars
        ):
            merged[-1]["text"] = (merged[-1]["text"] + " " + segment["text"]).strip()
            merged[-1]["char_end_page"] = segment["char_end_page"]
        else:
            merged.append(segment)
    if len(merged) >= 2 and len(merged[-1]["text"]) < min_chars:
        previous = merged[-2]
        last = merged[-1]
        if len(previous["text"]) + len(last["text"]) + 1 <= max_chars:
            previous["text"] = (previous["text"] + " " + last["text"]).strip()
            previous["char_end_page"] = last["char_end_page"]
            merged.pop()
    return merged


def split_page_under_limit_with_offsets(clean, max_chars=512):
    if not clean:
        return []
    if max_chars <= 0:
        return split_page_with_offsets(clean, target_chars=650)
    initial = split_page_with_offsets(
        clean,
        target_chars=max_chars,
        min_boundary=max(60, min(250, max_chars // 3)),
    )
    limited = []
    for segment in initial:
        text = segment.get("text") or ""
        start = int(segment.get("char_start_page") or 0)
        if len(text) <= max_chars:
            limited.append(segment)
            continue
        cursor = 0
        min_boundary = max(30, max_chars // 4)
        while cursor < len(text):
            remaining = len(text) - cursor
            if remaining <= max_chars:
                piece_end = len(text)
            else:
                window = text[cursor : cursor + max_chars]
                candidates = [window.rfind("\n\n"), window.rfind(". "), window.rfind("? "), window.rfind("! "), window.rfind("; "), window.rfind(", "), window.rfind(" ")]
                valid = [candidate for candidate in candidates if candidate >= min_boundary]
                cut = max(valid) if valid else max_chars
                adjust = 1 if cut < len(window) and window[cut:cut + 1] in {" ", "\n"} else 0
                piece_end = min(len(text), cursor + cut + adjust)
            raw_piece = text[cursor:piece_end]
            piece_text = raw_piece.strip()
            if piece_text:
                leading_ws = len(raw_piece) - len(raw_piece.lstrip())
                trailing_ws = len(raw_piece.rstrip())
                limited.append(
                    {
                        "text": piece_text,
                        "char_start_page": start + cursor + leading_ws,
                        "char_end_page": start + cursor + trailing_ws,
                    }
                )
            cursor = max(piece_end, cursor + 1)
            while cursor < len(text) and text[cursor].isspace():
                cursor += 1
    return limited


def shorten_heading(text, limit=44):
    clean = re.sub(r"\s+", " ", text or "").strip()
    clean = re.sub(r"^(?:chapter\s+)?(?:one|two|three|four|five|six|seven|eight|nine|ten|\d+)\.?\s+", "", clean, flags=re.I)
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "..."


NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}


ROMAN_VALUES = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}


def roman_to_int(value):
    total = 0
    prev = 0
    for char in reversed((value or "").casefold()):
        current = ROMAN_VALUES.get(char, 0)
        if not current:
            return None
        if current < prev:
            total -= current
        else:
            total += current
            prev = current
    return total or None


def compact_label_token(text, fallback="pdf"):
    token = re.sub(r"[^a-z0-9]+", "-", str(text or "").casefold()).strip("-")
    return token or fallback


def detect_chapter_number(*values):
    for value in values:
        text = normalize_text(value or "")
        if not text:
            continue
        if match := re.search(r"\b(?:chapter|ch|part)\s+(\d{1,3})\b", text, re.I):
            return int(match.group(1))
        if match := re.search(r"\b(?:chapter|ch|part)\s+([ivxlcdm]{1,8})\b", text, re.I):
            roman_value = roman_to_int(match.group(1))
            if roman_value:
                return roman_value
        if match := re.search(r"\b(?:chapter|ch|part)\s+([a-z]+)\b", text, re.I):
            word_value = NUMBER_WORDS.get(match.group(1).casefold())
            if word_value:
                return word_value
        if match := re.search(r"^\s*([ivxlcdm]{1,8}|[a-z]+|\d{1,3})\.\s+", text, re.I):
            raw = match.group(1)
            if raw.isdigit():
                return int(raw)
            roman_value = roman_to_int(raw)
            if roman_value:
                return roman_value
            word_value = NUMBER_WORDS.get(raw.casefold())
            if word_value:
                return word_value
    return None


def native_identity_stem(row, include_segment=True, page_parent=False):
    short_label = compact_label_token(row.get("source_short_label") or row.get("source_title") or "PDF")
    page_start = int(row["pdf_page"])
    page_end = int(row.get("pdf_page_end") or page_start)
    page_part = f"p{page_start}" if page_end == page_start else f"p{page_start}-{page_end}"
    parts = [short_label, page_part]
    logical_page = row.get("logical_page")
    if logical_page not in (None, "", 0, "0"):
        parts.append(f"lp{logical_page}")
    region_index = row.get("reading_region_index")
    region_count = row.get("reading_region_count")
    if region_index not in (None, "", 0, "0") and int(region_count or 1) > 1:
        parts.append(f"r{int(region_index)}")
    # A page-parent represents the complete PDF page. Its page-level line
    # range remains in metadata for provenance, but it is redundant in the
    # visible document name (for example, ``p15-ln1-21-page-parent``).
    # Keep the range in names only when it differentiates a true subchunk.
    if not page_parent:
        line_start = row.get("page_line_start")
        line_end = row.get("page_line_end")
        if line_start not in (None, "", 0, "0"):
            if line_end not in (None, "", 0, "0") and str(line_end) != str(line_start):
                parts.append(f"ln{line_start}-{line_end}")
            else:
                parts.append(f"ln{line_start}")
    if include_segment:
        parts.append(f"s{int(row['segment_index']):05d}")
    elif page_parent:
        parts.append("page-parent")
    chapter_number = detect_chapter_number(
        row.get("chapter"),
        row.get("section"),
        row.get("subsection"),
        row.get("part"),
    )
    if chapter_number:
        parts.append(f"ch{chapter_number:02d}")
    return "-".join(str(part) for part in parts if str(part).strip())


def native_segment_title(row, include_heading=True):
    return native_identity_stem(row, include_segment=True)


def native_page_parent_title(row, include_heading=True):
    return native_identity_stem(row, include_segment=False, page_parent=True)


def compact_marker(row, marker_style="short"):
    chapter = shorten_heading(row.get("chapter") or row.get("section") or "", 44)
    short_label = row.get("source_short_label") or row["source_title"]
    segment_no = f"s{int(row['segment_index']):05d}"
    page_start = int(row["pdf_page"])
    page_end = int(row.get("pdf_page_end") or page_start)
    page_label = f"p{page_start}" if page_end == page_start else f"p{page_start}-{page_end}"
    region_label = reading_region_label(row)
    if marker_style == "full":
        parts = [
            row["source_title"],
            page_label,
        ]
        if chapter:
            parts.append(f"ch: {chapter}")
        if region_label:
            parts.append(region_label)
        parts.append(f"seg: {row['segment_id']}")
        return "[" + " | ".join(parts) + "]"
    if marker_style == "compact":
        parts = [short_label, page_label, segment_no]
        if region_label:
            parts.append(region_label)
        if chapter:
            parts.append(chapter)
        return "[" + " | ".join(parts) + "]"
    parts = [f"{short_label} {page_label} {segment_no}"]
    if region_label:
        parts.append(region_label)
    if chapter:
        parts.append(chapter)
    return "[" + " | ".join(parts) + "]"


def reading_region_label(row):
    """Describe a sub-page reading region without changing citation pages."""
    count = int(row.get("reading_region_count") or 1)
    index = int(row.get("reading_region_index") or 1)
    name = str(row.get("reading_region") or "").replace("_", " ").strip()
    if count <= 1 and not name:
        return ""
    if count <= 1:
        return f"Reading region: {name}."
    return f"Reading region: {name or 'sub-page region'} ({index} of {count})."


def make_segments(
    pdf_path,
    backend,
    pages,
    start_page,
    end_page,
    source_meta,
    target_chars,
    outline=None,
    segment_mode="passages",
    effective_limit=0,
):
    segments = []
    current_part = ""
    current_chapter = ""
    current_section = ""
    current_subsection = ""
    source_id = source_meta["source_id"]
    source_hash_prefix = source_meta["source_sha256"][:12].lower()
    segment_index = 1
    normalized_segment_mode = (segment_mode or "passages").casefold()
    for page_info in pages:
        page_num = int(page_info["page"])
        if page_num < start_page:
            continue
        if end_page and page_num >= end_page:
            break
        duplicate_pages = source_meta.get("duplicate_pages") or {}
        if page_num in duplicate_pages:
            continue

        page_raw = strip_repeated_marginalia(
            page_info.get("text", ""),
            source_meta.get("repeated_headers"),
            source_meta.get("repeated_footers"),
        )
        outline_context = outline_context_for_page(outline, page_num, start_page, end_page)
        outline_chapter = outline_context.get("chapter") or ""
        if outline_context.get("part"):
            current_part = outline_context["part"]
        if outline_chapter:
            current_chapter = outline_chapter
        if outline_context.get("section"):
            current_section = outline_context["section"]
        if outline_context.get("subsection"):
            current_subsection = outline_context["subsection"]
        page_headings = extract_page_headings(page_raw)
        heading = detect_heading_from_page_text(page_raw)
        section = detect_section_from_page_text(page_raw)
        if heading and not outline_chapter:
            current_chapter = heading
        if section:
            current_section = section
        elif page_headings and outline_chapter and page_headings[0] != outline_chapter:
            current_section = page_headings[0]

        body_start = int(source_meta.get("body_start") or start_page)
        end_matter_start = source_meta.get("end_matter_start")
        if page_num < body_start:
            document_region = "front_matter"
        elif end_matter_start and page_num >= int(end_matter_start):
            document_region = "end_matter"
        else:
            document_region = "body"

        reading_regions = page_info.get("reading_regions") or [{
            "text": page_raw,
            "reading_region": "",
            "reading_region_index": 1,
            "reading_region_count": 1,
            "source_column_index": 1,
        }]
        for region in reading_regions:
            raw = strip_repeated_marginalia(
                region.get("text", ""),
                source_meta.get("repeated_headers"),
                source_meta.get("repeated_footers"),
            )
            page_line_map = build_page_line_map(raw)
            clean = page_line_map.get("clean_text") or normalize_page_layout_text(raw)
            if len(clean) < 40:
                continue
            # A photographed spread may have several independently OCRed
            # regions.  They are still the same source PDF page for citations.
            logical_page = page_num if page_info.get("reading_regions") else detect_logical_page_number(raw, page_num)
            hard_limit = int(effective_limit or max(target_chars + 120, 900))
            page_segments = split_semantic_page(
                clean,
                target=int(target_chars or 650),
                hard_limit=hard_limit,
                mode=normalized_segment_mode,
                diagnostic=bool(source_meta.get("boundary_diagnostic_mode")),
            )
            for page_segment in page_segments:
                seg_id = f"pdf_{source_hash_prefix}_p{page_num:04d}_s{segment_index:05d}"
                page_line_start, page_line_end = detect_page_line_range(
                    page_line_map,
                    page_segment.get("char_start_page"),
                    page_segment.get("char_end_page"),
                )
                flags = []
                if len(page_segment["text"]) < 120:
                    flags.append("short_segment")
                if not current_chapter:
                    flags.append("no_heading_context")
                if page_num < start_page + 2:
                    flags.append("near_front_boundary")
                row = {
                    "source_id": source_id,
                    "source_title": source_meta["source_title"],
                    "source_author": source_meta["source_author"],
                    "source_short_label": source_meta.get("source_short_label") or source_meta["source_title"],
                    "source_file": pdf_path.name,
                    "source_sha256": source_meta["source_sha256"],
                    "source_published_epoch_ms": source_meta.get("source_published_epoch_ms"),
                    "metadata_provenance": source_meta.get("metadata_provenance", {}),
                    "backend": backend,
                    "pipeline_version": PIPELINE_VERSION,
                    "pdf_page": page_num,
                    "pdf_page_end": page_num,
                    "logical_page": logical_page,
                    "logical_page_end": logical_page,
                    "reading_region": region.get("reading_region") or "",
                    "reading_region_index": int(region.get("reading_region_index") or 1),
                    "reading_region_count": int(region.get("reading_region_count") or 1),
                    "source_column_index": int(region.get("source_column_index") or 1),
                    "ocr_method": region.get("ocr_method") or "",
                    "annotations_excluded": region.get("annotations_excluded") or "",
                    "document_region": document_region,
                    "part": current_part,
                    "chapter": current_chapter,
                    "section": current_section,
                    "subsection": current_subsection,
                    "headings_on_page": page_headings,
                    "chapter_source": "pdf_outline" if outline_chapter else ("page_text_heading" if heading else ""),
                    "section_source": "page_text_heading" if current_section else "",
                    "boundary_confidence": source_meta.get("boundary_confidence", ""),
                    "segment_id": seg_id,
                    "segment_index": segment_index,
                    "char_start_page": page_segment["char_start_page"],
                    "char_end_page": page_segment["char_end_page"],
                    "page_line_start": page_line_start,
                    "page_line_end": page_line_end,
                    "estimated_tokens": max(1, math.ceil(len(page_segment["text"]) / 4)),
                    "text": page_segment["text"],
                    "quality_flags": flags,
                    "boundary_debug": page_segment.get("boundary_debug", {}),
                }
                segments.append(row)
                segment_index += 1
    if normalized_segment_mode == "none" and segments:
        return collapse_unsegmented_document_segments(segments, source_hash_prefix)
    return segments


def collapse_unsegmented_document_segments(page_segments, source_hash_prefix):
    """Collapse pages into one explicit no-local-segmentation record.

    The page-span map is retained for reviewing the prepared text. It does not
    imply that AnythingLLM can retain those page spans after it re-chunks the
    one uploaded file.
    """
    first = dict(page_segments[0])
    last = page_segments[-1]
    text_parts = []
    page_spans = []
    offset = 0
    for row in page_segments:
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        if text_parts:
            offset += 2
        start = offset
        text_parts.append(text)
        offset += len(text)
        page_spans.append(
            {
                "pdf_page": row.get("pdf_page"),
                "logical_page": row.get("logical_page") or "",
                "text_char_start": start,
                "text_char_end": offset,
            }
        )
    text = "\n\n".join(text_parts)
    first.update(
        {
            "segment_id": f"pdf_{source_hash_prefix}_p{int(first['pdf_page']):04d}_s00001",
            "segment_index": 1,
            "pdf_page_end": last.get("pdf_page_end") or last.get("pdf_page"),
            "logical_page_end": last.get("logical_page_end") or last.get("logical_page") or "",
            "document_region": (
                first.get("document_region")
                if first.get("document_region") == last.get("document_region")
                else "multiple_regions"
            ),
            "char_start_page": None,
            "char_end_page": None,
            "page_line_start": None,
            "page_line_end": None,
            "estimated_tokens": max(1, math.ceil(len(text) / 4)),
            "text": text,
            "quality_flags": list(first.get("quality_flags") or []) + ["no_local_segmentation"],
            "boundary_debug": {
                "reason": "no_local_segmentation",
                "page_range": [first.get("pdf_page"), last.get("pdf_page")],
                "page_span_count": len(page_spans),
            },
            "page_spans": page_spans,
        }
    )
    return [first]


def pdf_page_range_label(row):
    start = int(row.get("pdf_page") or 0)
    end = int(row.get("pdf_page_end") or start)
    return str(start) if end == start else f"{start}-{end}"


def pdf_page_metadata_label(row):
    """Preserve the established single-page metadata wording."""
    start = int(row.get("pdf_page") or 0)
    end = int(row.get("pdf_page_end") or start)
    page_range = pdf_page_range_label(row)
    page_label = f"PDF page: {page_range}." if end == start else f"PDF page range: {page_range}."
    region = reading_region_label(row)
    return f"{page_label} {region}".strip()


def simulated_chunks(text, chunk_size=1000, overlap=140):
    chunks = []
    start = 0
    step = chunk_size - overlap
    while start < len(text):
        chunks.append(text[start : start + chunk_size])
        start += step
    return chunks


def extraction_quality(pages, stats, start_page, end_page):
    included = [s for s in stats if s.pdf_page >= start_page and (not end_page or s.pdf_page < end_page)]
    included_page_numbers = {int(stat.pdf_page) for stat in included}
    included_text = "\n".join(
        str(page.get("text") or "")
        for page in pages
        if int(page.get("page") or 0) in included_page_numbers
    )
    chars = sum(s.chars for s in included)
    words = sum(s.words for s in included)
    replacement = sum(s.replacement_chars for s in included)
    empty = sum(1 for s in included if s.is_empty)
    index_like = sum(1 for s in included if s.is_index_like)
    bibliography_like = sum(1 for s in included if s.is_bibliography_like)
    duplicate_pages = sum(1 for s in included if s.duplicate_of_page is not None)
    image_heavy_low_text_pages = sum(1 for s in included if s.image_count > 0 and s.words < 40)
    rotated_pages = sum(1 for s in included if s.rotation % 360 != 0)
    repeated_header_pages = sum(1 for s in included if s.repeated_header)
    repeated_footer_pages = sum(1 for s in included if s.repeated_footer)
    avg_words_per_page = round(words / max(len(included), 1), 1)
    # OCR can return many nominal "words" while still leaking page-layout
    # syntax (tables/pipes, embedded-image notices, or HTML line breaks). Such
    # output is not equivalent to readable prose. Count only unmistakable
    # extractor artefacts so ordinary punctuation, citations, and source text
    # are never penalised.
    ocr_layout_artifact_count = (
        included_text.count("|")
        + included_text.count("_")
        + included_text.count("<br>") * 2
        + included_text.count("**==> picture") * 8
        + included_text.count("**----- Start of picture text -----**") * 8
        + included_text.count("**----- End of picture text -----**") * 8
    )
    ocr_layout_artifact_ratio = round(
        ocr_layout_artifact_count / max(len(included_text), 1), 4
    )
    scanned_likelihood = (
        "high"
        if included and (empty + image_heavy_low_text_pages) / len(included) >= 0.6
        else "possible"
        if included and (empty + image_heavy_low_text_pages) / len(included) >= 0.2
        else "low"
    )
    return {
        "included_pages": len(included),
        "included_chars": chars,
        "included_words": words,
        "replacement_chars": replacement,
        "empty_pages": empty,
        "index_like_pages": index_like,
        "bibliography_like_pages": bibliography_like,
        "duplicate_pages": duplicate_pages,
        "image_heavy_low_text_pages": image_heavy_low_text_pages,
        "rotated_pages": rotated_pages,
        "repeated_header_pages": repeated_header_pages,
        "repeated_footer_pages": repeated_footer_pages,
        "average_words_per_page": avg_words_per_page,
        "ocr_layout_artifact_count": ocr_layout_artifact_count,
        "ocr_layout_artifact_ratio": ocr_layout_artifact_ratio,
        "scanned_likelihood": scanned_likelihood,
    }


def split_text_for_inline_markers(text, target_chars=320, hard_max_chars=480):
    clean = str(text or "").strip()
    if not clean:
        return []
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n+", clean) if part.strip()]
    blocks = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > hard_max_chars:
            sentences = [part.strip() for part in re.split(r"(?<=[.!?;:])\s+", paragraph) if part.strip()]
            if not sentences:
                sentences = [paragraph]
        else:
            sentences = [paragraph]
        for sentence in sentences:
            candidate = f"{current}\n\n{sentence}".strip() if current else sentence
            if current and len(candidate) > hard_max_chars:
                blocks.append(current.strip())
                current = sentence
                continue
            if current and len(candidate) >= target_chars:
                blocks.append(candidate.strip())
                current = ""
                continue
            current = candidate
    if current.strip():
        blocks.append(current.strip())
    return blocks


def inline_marker_text(row, marker_style="short", target_chars=320, hard_max_chars=480):
    marker = compact_marker(row, marker_style=marker_style)
    blocks = split_text_for_inline_markers(
        row.get("text", ""),
        target_chars=target_chars,
        hard_max_chars=hard_max_chars,
    )
    if not blocks:
        return marker
    return "\n\n".join(f"{marker}\n{block}\n{marker}" for block in blocks)


def has_complete_native_text_candidate(candidates, pdf_page_count, ocr_preflight_hint=None):
    """Return true when a default extractor already recovered trustworthy text.

    Automatic mode may skip a later OCR-capable candidate when an earlier
    native candidate covers the whole file with clean text and valid
    provenance. This deliberately does not relax the gate for an actual
    extractor failure, image-heavy pages, or an untrusted outline.
    """
    page_count = max(1, int(pdf_page_count or 0))
    coverage = dict((ocr_preflight_hint or {}).get("full_native_text_coverage") or {})
    sparse_image_review_only = (
        coverage.get("status") == "verified"
        and len(coverage.get("image_backed_low_text_pages") or []) > 0
        and len(coverage.get("image_backed_low_text_pages") or []) / page_count <= 0.25
    )
    minimum_words = max(500, page_count * 150)
    for candidate in candidates or []:
        quality = candidate.get("quality") or {}
        native_chunk_eval = candidate.get("native_chunk_eval") or {}
        # A title leaf, deliberately blank verso, or separator page is not a
        # missing text page.  Require coverage of every material native page,
        # not the PDF's physical page count.  This retains the strict gate for
        # actual gaps while avoiding a redundant full-document OCR/layout pass
        # for otherwise complete documents.
        empty_pages = max(0, int(quality.get("empty_pages") or 0))
        material_pages = max(1, page_count - empty_pages)
        if (
            not candidate.get("error")
            and len(candidate.get("segments") or []) > 0
            and int(quality.get("included_pages") or 0) >= material_pages
            and int(quality.get("included_words") or 0) >= minimum_words
            and (
                str(quality.get("scanned_likelihood") or "").casefold() == "low"
                # The all-page preflight distinguishes a handful of
                # photographed tables/illustrations from a scan-heavy PDF.
                # Keep the native candidate for the former; PyMuPDF4LLM can
                # otherwise launch OCR internally across the whole document.
                or (
                    sparse_image_review_only
                    and str(quality.get("scanned_likelihood") or "").casefold() == "possible"
                    and float(quality.get("average_words_per_page") or 0) >= 150.0
                )
            )
            and str(native_chunk_eval.get("status") or "").casefold() == "pass"
        ):
            return True
    return False


def is_reliable_structure_reference(quality, pdf_page_count):
    """Whether one candidate can define shared document boundaries.

    Candidate extractors can format the same text very differently.  Once a
    candidate covers every material page with clean native text, body/end
    boundaries must be reused by later candidates so their quality comparison
    measures extraction, not a different slice of the PDF.
    """
    quality = quality or {}
    page_count = max(1, int(pdf_page_count or 0))
    empty_pages = max(0, int(quality.get("empty_pages") or 0))
    material_pages = max(1, page_count - empty_pages)
    return (
        int(quality.get("included_pages") or 0) >= material_pages
        and int(quality.get("included_words") or 0) >= max(500, page_count * 150)
        and str(quality.get("scanned_likelihood") or "").casefold() == "low"
    )


def format_vector_observation(observed_vectors, expected_records, operator_state, *, record_label="vectors"):
    """Describe vector evidence without treating controlled re-chunking as loss.

    Page-parent uploads normally have one planned record per non-empty PDF
    page, while segment uploads can have several records per page.  The UI
    must name that representation instead of presenting vector evidence as a
    completed PDF-page count.
    """
    observed = max(0, int(observed_vectors or 0))
    expected = max(0, int(expected_records or 0))
    state = str(operator_state or "observing")
    label = str(record_label or "vectors").strip() or "vectors"
    if not expected:
        return f"{observed} {label} confirmed (expected count not yet confirmed; {state})"
    if expected and observed > expected:
        return f"{expected} planned records → {observed} {label} confirmed (re-chunked; {state})"
    return f"{observed}/{expected} {label} confirmed ({state})"


def embedding_observation_progress(start_fraction, end_fraction, observed_vectors, expected_records):
    """Map exact vector coverage into the upload portion of workflow progress.

    Request acceptance is not indexing completion.  This prevents a fixed
    post-upload stage value from showing 95% when only a small fraction of the
    planned records is searchable.
    """
    start = min(1.0, max(0.0, float(start_fraction or 0.0)))
    end = min(1.0, max(start, float(end_fraction or start)))
    expected = max(0, int(expected_records or 0))
    observed = max(0, int(observed_vectors or 0))
    coverage = min(1.0, observed / expected) if expected else 0.0
    return start + (end - start) * coverage


def has_document_wide_ocr_evidence(candidates):
    """Whether candidate evidence warrants OCRing an entire PDF.

    ``possible`` deliberately is not enough.  A mostly text-native book can
    contain a title leaf, photographed table, illustration, or blank verso;
    running Unstructured ``hi_res`` against all its pages is both expensive
    and less reliable than retaining the native text and flagging the few
    sparse pages for review.  ``high`` remains the conservative signal for a
    document-wide OCR fallback.
    """
    return any(
        str((candidate.get("quality") or {}).get("scanned_likelihood") or "").casefold()
        == "high"
        for candidate in (candidates or [])
        if not candidate.get("error")
    )


def resolve_unstructured_strategy(requested_strategy, prior_candidates=None, runtime_probe=None):
    requested = (requested_strategy or "auto").strip().casefold()
    runtime_probe = dict(
        unstructured_runtime_status("hi_res") if runtime_probe is None else runtime_probe
    )
    prior_candidates = prior_candidates or []
    scanned_like = has_document_wide_ocr_evidence(prior_candidates)
    backend_failed = any(bool(candidate.get("error")) for candidate in prior_candidates)
    coverage_disagreement = False
    word_counts = [
        int((candidate.get("quality") or {}).get("included_words") or 0)
        for candidate in prior_candidates
        if int((candidate.get("quality") or {}).get("included_words") or 0) > 0
    ]
    if len(word_counts) >= 2:
        coverage_disagreement = (max(word_counts) - min(word_counts)) / max(word_counts) > 0.35

    if requested in {"fast", "hi_res", "ocr_only"}:
        if requested in {"hi_res", "ocr_only"} and not runtime_probe.get("tesseract_available"):
            raise RuntimeError(
                "The selected Unstructured OCR strategy requires Tesseract, but tesseract.exe was not found."
            )
        return {
            "requested": requested,
            "resolved": requested,
            "runtime": {**runtime_probe, "ocr_required": requested in {"hi_res", "ocr_only"}},
            "reason": "explicit_strategy",
        }

    if runtime_probe.get("tesseract_available") and (scanned_like or backend_failed or coverage_disagreement):
        return {
            "requested": requested,
            "resolved": "hi_res",
            "runtime": {**runtime_probe, "ocr_required": True},
            "reason": "ocr_enabled_for_difficult_pdf",
        }
    return {
        "requested": requested,
        "resolved": "fast",
        "runtime": {**runtime_probe, "ocr_required": False},
        "reason": (
            "ocr_unavailable_using_fast"
            if not runtime_probe.get("tesseract_available")
            else "fast_sufficient_for_text_pdf"
        ),
    }


def is_unstructured_runtime_failure(error):
    """Identify failures safe to suppress for the rest of one batch only."""
    text = str(error or "").casefold()
    return any(token in text for token in (
        "tesseract", "unstructured pdf support is not available",
        "unstructured-inference", "onnxruntime", "detectron2", "no module named",
        "unstructured ocr timed out",
    ))


def ocr_assistance_evidence(selected, candidates, profile):
    """Return conservative, observable evidence that extraction needed OCR assistance.

    PyMuPDF4LLM can invoke OCR internally on scan-only PDFs.  Its public result
    does not expose the engine choice, so we only classify that path as
    OCR-assisted when the ordinary PyMuPDF candidate had no text layer and the
    selected candidate recovered material text.  This keeps timing telemetry
    honest without claiming OCR for every PyMuPDF4LLM extraction.
    """
    selected = selected or {}
    backend = str(selected.get("backend") or "").casefold()
    runtime = profile.get("unstructured_runtime") or {}
    # This function is called immediately after candidate selection, before
    # the later reporting step copies ``selected_strategy`` into the profile.
    # ``resolved_strategy`` is the run-scoped value available at that point.
    strategy = str(
        runtime.get("selected_strategy") or runtime.get("resolved_strategy") or ""
    ).casefold()
    if backend == "unstructured" and strategy in {"hi_res", "ocr_only"}:
        return {
            "used": True,
            "evidence": f"unstructured_{strategy}",
        }
    if backend != "pymupdf4llm":
        return {"used": False, "evidence": "not_observed"}

    selected_quality = selected.get("quality") or {}
    recovered_words = int(selected_quality.get("included_words") or 0)
    for candidate in candidates or []:
        if str(candidate.get("backend") or "").casefold() != "pymupdf":
            continue
        native_quality = candidate.get("quality") or {}
        included_pages = int(native_quality.get("included_pages") or 0)
        empty_pages = int(native_quality.get("empty_pages") or 0)
        if (
            included_pages > 0
            and empty_pages >= included_pages
            and recovered_words > 0
            and str(native_quality.get("scanned_likelihood") or "").casefold() == "high"
        ):
            return {
                "used": True,
                "evidence": "pymupdf4llm_recovered_text_from_empty_native_layer",
            }
    # An explicitly selected PyMuPDF4LLM backend need not have produced a
    # separate ordinary-PyMuPDF candidate.  Inspect the selected pages' native
    # text layer directly in that case.  This is a cheap, read-only probe and
    # avoids falsely recording an authentic scan as a text-only run simply
    # because the operator chose the OCR-capable backend up front.
    selected_pages = sorted(
        {
            int(segment.get("pdf_page"))
            for segment in selected.get("segments") or []
            if str(segment.get("pdf_page") or "").isdigit()
            and int(segment.get("pdf_page")) > 0
        }
    )
    source_path = Path(str(profile.get("source_file") or ""))
    if selected_pages and source_path.is_file() and recovered_words > 0:
        try:
            with fitz.open(source_path) as document:
                native_chars = [
                    len(str(document.load_page(page_number - 1).get_text("text") or "").strip())
                    for page_number in selected_pages
                    if page_number <= document.page_count
                ]
            if native_chars and not any(native_chars):
                return {
                    "used": True,
                    "evidence": "pymupdf4llm_recovered_text_from_direct_empty_native_layer_probe",
                }
        except (OSError, RuntimeError, ValueError):
            # Evidence remains inconclusive if the source can no longer be
            # opened.  Do not promote an OCR inference from this fallback.
            pass
    return {"used": False, "evidence": "not_observed"}


def explainable_ocr_coverage_disagreement(selected, candidates, profile, ocr_evidence):
    """Identify a narrow, evidence-backed OCR recovery disagreement.

    A scan-only PDF can legitimately yield no native text, partial/noisy text
    from a general layout extractor, and substantially more text from the
    photographed-page OCR path.  Treating those word counts as equally
    credible makes successful OCR recovery block itself.  This exception is
    deliberately strict: the selected OCR output must cover every physical
    page, be clean enough for retrieval, retain native chunk provenance, and
    every materially shorter peer must carry objective evidence of weakness.
    A disagreement between two clean, substantial extractions still blocks.
    """
    selected = selected or {}
    candidates = candidates or []
    profile = profile or {}
    ocr_evidence = ocr_evidence or {}
    selected_quality = selected.get("quality") or {}
    page_count = max(1, int(profile.get("pdf_page_count") or 0))
    selected_words = int(selected_quality.get("included_words") or 0)
    selected_pages = int(selected_quality.get("included_pages") or 0)
    selected_artifact_ratio = float(selected_quality.get("ocr_layout_artifact_ratio") or 0.0)
    selected_replacements = int(selected_quality.get("replacement_chars") or 0)
    selected_chars = int(selected_quality.get("included_chars") or 0)

    base_checks = {
        "selected_backend_is_unstructured": (
            str(selected.get("backend") or "").casefold() == "unstructured"
        ),
        "ocr_assistance_observed": bool(ocr_evidence.get("used")),
        "all_pdf_pages_covered": selected_pages >= page_count,
        "substantial_selected_text": selected_words >= max(500, page_count * 150),
        "selected_text_density_sufficient": (
            float(selected_quality.get("average_words_per_page") or 0.0) >= 150.0
        ),
        "selected_layout_artifacts_low": selected_artifact_ratio <= 0.005,
        "selected_replacement_characters_low": (
            selected_replacements <= max(20, int(selected_chars * 0.005))
        ),
        "native_chunk_provenance_passes": (
            str((selected.get("native_chunk_eval") or {}).get("status") or "").casefold()
            == "pass"
        ),
    }

    native_layer_absent = any(
        str(candidate.get("backend") or "").casefold() == "pymupdf"
        and not candidate.get("error")
        and not candidate.get("segments")
        and int((candidate.get("quality") or {}).get("included_words") or 0) == 0
        and str((candidate.get("quality") or {}).get("scanned_likelihood") or "").casefold()
        == "high"
        for candidate in candidates
    )
    base_checks["native_text_layer_absent"] = native_layer_absent

    materially_shorter_peers = []
    weak_shorter_peers = []
    for candidate in candidates:
        if candidate is selected or candidate.get("error") or not candidate.get("segments"):
            continue
        quality = candidate.get("quality") or {}
        peer_words = int(quality.get("included_words") or 0)
        if peer_words <= 0 or selected_words <= 0:
            continue
        disagreement = (selected_words - peer_words) / selected_words
        if disagreement <= 0.35:
            continue
        peer = {
            "backend": str(candidate.get("backend") or ""),
            "included_words": peer_words,
            "disagreement": round(disagreement, 4),
            "ocr_layout_artifact_ratio": float(quality.get("ocr_layout_artifact_ratio") or 0.0),
            "average_words_per_page": float(quality.get("average_words_per_page") or 0.0),
            "scanned_likelihood": str(quality.get("scanned_likelihood") or ""),
        }
        materially_shorter_peers.append(peer)
        if (
            peer["ocr_layout_artifact_ratio"] >= 0.01
            or peer["average_words_per_page"] < 100.0
            or peer["scanned_likelihood"].casefold() == "high"
        ):
            weak_shorter_peers.append(peer)

    base_checks["shorter_peer_has_objective_weakness"] = bool(materially_shorter_peers) and (
        len(weak_shorter_peers) == len(materially_shorter_peers)
    )
    accepted = all(base_checks.values())
    return {
        "accepted": accepted,
        "reason": (
            "clean_complete_ocr_recovery_from_absent_native_layer"
            if accepted
            else "coverage_disagreement_requires_review"
        ),
        "checks": base_checks,
        "materially_shorter_peers": materially_shorter_peers,
        "weak_shorter_peers": weak_shorter_peers,
    }


def write_provenance_review_manifest(
    selected_dir: Path,
    source_meta,
    profile,
    selected,
    ocr_evidence,
    page_parent_rows,
    transition_rows,
):
    """Write a compact index for reviewing one prepared run.

    The detailed text, per-segment evidence, and page/child mappings already
    live in their own durable artifacts.  This manifest is deliberately an
    index rather than a duplicate OCR transcript: it lets a reviewer find the
    canonical files and explains whether the selected extraction had observed
    OCR assistance.  All paths remain inside this run's artifact directory.
    """
    quality = selected.get("quality") or {}
    selected_backend = str(selected.get("backend") or "")
    ocr_execution = dict(selected.get("pymupdf4llm_execution") or {})
    legacy_ocr_workers = (
        pymupdf4llm_ocr_page_workers()
        if selected_backend.casefold() == "pymupdf4llm"
        else 0
    )
    manifest = {
        "schema_version": 1,
        "review_scope": "single_fresh_preparation_run",
        "source": {
            "source_id": source_meta.get("source_id", ""),
            "title": source_meta.get("source_title", ""),
            "author": source_meta.get("source_author", ""),
            "sha256": source_meta.get("source_sha256", ""),
            "pdf_page_count": profile.get("pdf_page_count", 0),
        },
        "selected_extraction": {
            "backend": selected_backend,
            "start_page": selected.get("start_page"),
            "end_page": selected.get("end_page"),
            "segments": len(selected.get("segments") or []),
            "page_parents": len(page_parent_rows),
            "included_pages": quality.get("included_pages", 0),
            "included_words": quality.get("included_words", 0),
            "scanned_likelihood": quality.get("scanned_likelihood", ""),
            "ocr_assistance_observed": bool(ocr_evidence.get("used")),
            "ocr_assistance_evidence": ocr_evidence.get("evidence", "not_observed"),
            # Preserve the v1 fields for existing artifact-review tooling.
            # The execution object below is additive evidence of what happened
            # in this specific run; the legacy field remains a configured
            # global worker setting, exactly as it was before this addition.
            "pymupdf4llm_ocr_page_workers": legacy_ocr_workers,
            "ocr_page_workers_scope": (
                "global_process_isolated_setting" if legacy_ocr_workers else "not_applicable"
            ),
            "pymupdf4llm_execution": ocr_execution,
        },
        "review_artifacts": {
            "canonical_extracted_text": "anythingllm-upload.txt",
            "segment_manifest": "segment-manifest.jsonl",
            "extraction_report": "extraction-report.csv",
            "page_parent_manifest": "page-parent-manifest.jsonl",
            "child_parent_map": "child-parent-map.csv",
            "page_transition_manifest": "page-transition-manifest.jsonl",
            "layout_region_review": "layout-region-review.json",
            "retrieval_lane_review": "retrieval-lane-review.json",
            "supplementary_lane_candidates": "supplementary-content-candidates.txt",
            "readiness_report": "readiness-report.html",
        },
        "provenance_checks": {
            "page_transition_boundaries_checked": len(transition_rows),
            "page_transition_companions_created": sum(
                1 for row in transition_rows if row.get("continuation_detected")
            ),
            "readiness_status": selected.get("readiness_status", ""),
            "readiness_reasons": selected.get("readiness_reasons", []),
        },
    }
    path = selected_dir / "provenance-review-manifest.json"
    write_json(path, manifest)
    return path


def generate_upload_text(segments, include_markers=True, marker_style="short"):
    blocks = []
    for row in segments:
        if include_markers:
            blocks.append(inline_marker_text(row, marker_style=marker_style))
        else:
            blocks.append(row["text"])
    return "\n\n".join(blocks)


def marker_ratio_stats(segments, marker_style="short", include_markers=True):
    content_chars = sum(len(row.get("text", "")) for row in segments)
    content_words = sum(len(re.findall(r"\b[\w\u2019'-]+\b", row.get("text", ""), flags=re.UNICODE)) for row in segments)
    if include_markers:
        fallback_blocks = [inline_marker_text(row, marker_style=marker_style) for row in segments]
    else:
        fallback_blocks = []
    marker_chars = sum(
        max(0, len(fallback_block) - len(row.get("text", "")))
        for row, fallback_block in zip(segments, fallback_blocks)
    )
    marker_words = sum(
        max(
            0,
            len(re.findall(r"\b[\w\u2019'-]+\b", fallback_block, flags=re.UNICODE))
            - len(re.findall(r"\b[\w\u2019'-]+\b", row.get("text", ""), flags=re.UNICODE)),
        )
        for row, fallback_block in zip(segments, fallback_blocks)
    )
    content_lengths = [len(row.get("text", "")) for row in segments]
    marker_lengths = [
        max(0, len(fallback_block) - len(row.get("text", "")))
        for row, fallback_block in zip(segments, fallback_blocks)
    ] or [0]
    total = max(content_chars + marker_chars, 1)
    short_segments = sum(1 for length in content_lengths if length < 180)
    return {
        "segment_count": len(segments),
        "marker_style": marker_style if include_markers else "none",
        "marker_chars": marker_chars,
        "content_chars": content_chars,
        "marker_char_ratio": round(marker_chars / total, 4),
        "avg_marker_chars": round(sum(marker_lengths) / max(len(fallback_blocks), 1), 1),
        "avg_content_chars": round(sum(content_lengths) / max(len(content_lengths), 1), 1) if content_lengths else 0,
        "min_content_chars": min(content_lengths) if content_lengths else 0,
        "max_content_chars": max(content_lengths) if content_lengths else 0,
        "short_segments_under_180_chars": short_segments,
        "marker_words": marker_words,
        "content_words": content_words,
    }


def check_status(ok, warn=False):
    if ok:
        return "pass"
    return "warning" if warn else "fail"


def evaluate_edge_cases(profile, selected, selected_dir: Path, native_payloads):
    rows = []

    def add(check, status, details):
        rows.append({"check": check, "status": status, "details": details})

    add("page_count_detected", check_status(int(profile.get("pdf_page_count") or 0) > 0), f"pdf_page_count={profile.get('pdf_page_count')}")
    add("selected_backend_present", check_status(bool(selected.get("backend"))), f"backend={selected.get('backend')}")
    add("body_start_detected", check_status(int(selected.get("start_page") or 0) > 0), f"start_page={selected.get('start_page')} reason={selected.get('start_reason')}")
    add("end_matter_exclusion", check_status(bool(selected.get("end_page")), warn=True), f"end_page={selected.get('end_page') or 'not detected'}")
    outline = selected.get("outline_validation") or {}
    add(
        "outline_validation",
        check_status(
            outline.get("reliability") in {"trusted", "partially_trusted"},
            warn=True,
        ),
        f"reliability={outline.get('reliability')} pass_rate={outline.get('pass_rate')}",
    )
    add("segment_count", check_status(len(selected.get("segments", [])) > 0), f"segments={len(selected.get('segments', []))}")
    marker = selected.get("marker_stats") or {}
    add("metadata_ratio", check_status(float(marker.get("marker_char_ratio") or 1) <= 0.15), f"marker_char_ratio={marker.get('marker_char_ratio')}")
    chunk_eval = selected.get("chunk_eval") or {}
    add(
        "fallback_marker_survival",
        check_status(int(chunk_eval.get("chunks_without_marker") or 0) == 0, warn=True),
        "Fallback-only check. "
        f"chunk_size={chunk_eval.get('chunk_size')} overlap={chunk_eval.get('chunk_overlap')} "
        f"chunks_without_marker={chunk_eval.get('chunks_without_marker')} suspicious_chunks={chunk_eval.get('suspicious_chunks')}",
    )
    native_chunk_eval = selected.get("native_chunk_eval") or {}
    add(
        "native_header_chunk_survival",
        check_status(native_chunk_eval.get("status") == "pass"),
        f"retrieval_chunks={native_chunk_eval.get('retrieval_chunks')} "
        f"without_source_document={native_chunk_eval.get('chunks_without_source_document')} "
        f"without_page_or_segment={native_chunk_eval.get('chunks_without_page_or_segment')}",
    )
    required_fields = {
        "source_id",
        "source_title",
        "source_file",
        "source_sha256",
        "backend",
        "pdf_page",
        "page_line_start",
        "page_line_end",
        "chapter",
        "segment_id",
        "segment_index",
        "text",
        "headings_on_page",
        "chapter_source",
        "section_source",
    }
    first_segment = selected.get("segments", [{}])[0] if selected.get("segments") else {}
    missing_fields = sorted(required_fields - set(first_segment.keys()))
    add("manifest_fields", check_status(not missing_fields), f"missing={', '.join(missing_fields) or 'none'}")
    add(
        "page_text_heading_capture",
        check_status(any(row.get("headings_on_page") for row in selected.get("segments", [])[:50]), warn=True),
        "first 50 segments checked for headings_on_page",
    )
    first_payload = native_payloads[0] if native_payloads else {}
    payload_text = first_payload.get("textContent", "")
    payload_meta = first_payload.get("metadata", {})
    add(
        "native_payload_clean_text",
        check_status(not re.match(r"^\[[^\]\n]*(?:p\d+|seg|PDF_PAGE)[^\]\n]*\]", payload_text)),
        "textContent does not begin with fallback inline marker",
    )
    add(
        "native_payload_metadata_title",
        check_status(bool(re.search(r"\bp\d{1,4}\b", payload_meta.get("title", ""), re.I)) and bool(re.search(r"\bs\d{5}\b", payload_meta.get("title", ""), re.I))),
        f"title={payload_meta.get('title', '')}",
    )
    add(
        "native_payload_metadata_description",
        check_status("PDF page:" in payload_meta.get("description", "") and "Segment:" in payload_meta.get("description", "")),
        payload_meta.get("description", "")[:220],
    )
    add(
        "primary_clean_text_artifact",
        check_status((selected_dir / "anythingllm-upload.txt").exists()),
        str(selected_dir / "anythingllm-upload.txt"),
    )
    add(
        "fallback_inline_artifact",
        check_status(
            (selected_dir / "anythingllm-upload-inline-metadata-fallback.txt").exists(),
            warn=True,
        ),
        str(selected_dir / "anythingllm-upload-inline-metadata-fallback.txt"),
    )
    add("frontmatter_variant", check_status((selected_dir / "anythingllm-upload-frontmatter-and-body.txt").exists(), warn=True), "optional variant")
    add("endmatter_variant", check_status((selected_dir / "anythingllm-upload-body-with-endmatter.txt").exists(), warn=True), "optional variant")
    layout = selected.get("layout_evidence") or {}
    if int(layout.get("removed_marginalia_count") or 0):
        add(
            "PDF_LAYOUT_MARGINALIA_EXCLUDED",
            "info",
            f"excluded={layout['removed_marginalia_count']}; review layout-region-review.json",
        )
    if int(layout.get("note_candidates_retained_count") or 0):
        add(
            "PDF_LAYOUT_NOTE_CANDIDATES_RETAINED",
            "warning",
            f"retained_note_candidates={layout['note_candidates_retained_count']}; review layout-region-review.json",
        )
    if int(layout.get("excluded_footnote_count") or 0):
        add(
            "PDF_LAYOUT_FOOTNOTES_EXCLUDED",
            "info",
            f"excluded_footnote_groups={layout['excluded_footnote_count']}; review layout-region-review.json",
        )
    lane_review = selected.get("lane_review") or {}
    if int(lane_review.get("proposed_supplementary_count") or 0):
        if lane_review.get("primary_payload_changed"):
            add(
                "PDF_SUPPLEMENTARY_REFERENCE_REGIONS_EXCLUDED",
                "info",
                f"excluded_segments={lane_review.get('primary_excluded_segment_count', 0)}; original text retained in retrieval-lane-review.json",
            )
        else:
            add(
                "PDF_SUPPLEMENTARY_LANE_CANDIDATES",
                "warning",
                f"candidates={lane_review['proposed_supplementary_count']}; retained because no narrow automatic exclusion rule matched",
            )
    q = selected.get("quality") or {}
    add("low_text_or_scanned_detection", check_status(int(q.get("included_words") or 0) >= 8000, warn=True), f"included_words={q.get('included_words')}")

    failed = sum(1 for row in rows if row["status"] == "fail")
    warnings = sum(1 for row in rows if row["status"] == "warning")
    overall = "pass" if failed == 0 else "fail"
    return {
        "overall_status": overall,
        "failures": failed,
        "warnings": warnings,
        "rows": rows,
    }


def build_edge_case_html(edge_case_report):
    rows = []
    for row in edge_case_report["rows"]:
        rows.append(
            f"<tr><td>{html.escape(row['check'])}</td><td>{html.escape(row['status'])}</td><td>{html.escape(row['details'])}</td></tr>"
        )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Edge Case Test Report</title>
<style>body{{font-family:Segoe UI,Arial,sans-serif;max-width:1100px;margin:32px auto;line-height:1.45}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ccc;padding:6px;text-align:left}}</style></head>
<body>
<h1>Edge Case Test Report</h1>
<p><b>Overall:</b> {html.escape(edge_case_report['overall_status'])} |
<b>Failures:</b> {edge_case_report['failures']} |
<b>Warnings:</b> {edge_case_report['warnings']}</p>
<table><tr><th>Check</th><th>Status</th><th>Details</th></tr>{''.join(rows)}</table>
</body></html>"""


def build_run_diagnostics(
    profile,
    selected,
    candidates,
    storage_report,
    upload_report,
    workspace_gate,
    post_upload_report,
    metadata_schema_report,
    runtime_validation_report,
    temporary_workspace_validation,
):
    diagnostics = []

    def add(code, severity, stage, message, action=""):
        diagnostics.append(
            {
                "code": code,
                "severity": severity,
                "stage": stage,
                "message": message,
                "recommended_action": action,
            }
        )

    quality = selected.get("quality", {})
    outline_reliability = selected.get("outline_validation", {}).get("reliability")
    ocr_assisted_selection = (
        selected.get("backend") == "unstructured"
        and str(selected.get("unstructured_strategy") or "").casefold() in {"hi_res", "ocr_only"}
        and int(quality.get("included_words") or 0) > 0
    )
    if profile.get("needs_password"):
        add("PDF_ENCRYPTED_PASSWORD_REQUIRED", "error", "inspection", "The PDF requires a password.", "Export an unlocked copy before extraction.")
    if quality.get("scanned_likelihood") == "high":
        if ocr_assisted_selection:
            add("PDF_IMAGE_HEAVY_OCR_USED", "info", "extraction", "Most included pages appear image-heavy, and the selected OCR-assisted extraction path was used.", "Review the OCR output on difficult pages, but no extra OCR pass is required before upload.")
        else:
            add("PDF_OCR_REQUIRED", "error", "extraction", "Most included pages have little text and appear image-heavy.", "Run page-aware OCR, then prepare the OCRed PDF.")
    elif quality.get("scanned_likelihood") == "possible":
        add(
            "PDF_MIXED_TEXT_AND_SCAN",
            "info" if ocr_assisted_selection else "warning",
            "extraction",
            "Some pages may be scanned or have a weak text layer.",
            (
                "Review low-text pages in extraction-report.csv to confirm OCR quality."
                if ocr_assisted_selection
                else "Review low-text pages in extraction-report.csv."
            ),
        )
    if outline_reliability == "missing":
        add("PDF_HAS_NO_BOOKMARKS", "warning", "structure", "The PDF has no usable bookmark outline; text heuristics were used.", "Review detected body and end-matter boundaries.")
    elif outline_reliability == "untrusted":
        add("PDF_OUTLINE_UNTRUSTED", "warning", "structure", "Bookmark destinations do not agree with extracted headings.", "Use text-derived headings or override boundaries in Advanced.")
    if selected.get("start_reason") == "table_of_contents_unconfirmed_retained":
        add(
            "TABLE_OF_CONTENTS_UNCONFIRMED_RETAINED",
            "warning",
            "structure",
            "A page had partial contents-like signals, but no later body boundary could be confirmed, so no pages were excluded.",
            "Use an explicit first-page override only if you want to trim confirmed front matter.",
        )
    if not selected.get("detected_end_page"):
        include_back_matter = bool(selected.get("include_back_matter"))
        add(
            "BOUNDARY_END_MATTER_NOT_DETECTED",
            "info" if include_back_matter else "warning",
            "structure",
            (
                "No confident end-matter boundary was found; the primary output currently includes the document tail."
                if include_back_matter
                else "No confident end-matter boundary was found."
            ),
            (
                "Inspect late-document segments only if you want to trim notes, bibliography, or index pages."
                if include_back_matter
                else "Inspect late-document segments and the full-document variant."
            ),
        )
    if int(quality.get("duplicate_pages") or 0):
        add("PDF_DUPLICATE_TEXT_PAGES", "warning", "extraction", f"{quality['duplicate_pages']} exact duplicate text page(s) were excluded.", "Review page_profile in source-profile.json.")
    layout = selected.get("layout_evidence") or {}
    if int(layout.get("removed_marginalia_count") or 0):
        add(
            "PDF_LAYOUT_MARGINALIA_EXCLUDED",
            "info",
            "extraction",
            f"Excluded {layout['removed_marginalia_count']} high-confidence positioned header/footer item(s).",
            "Review selected/layout-region-review.json before using content-quality retrieval evidence.",
        )
    if int(layout.get("note_candidates_retained_count") or 0):
        add(
            "PDF_LAYOUT_NOTE_CANDIDATES_RETAINED",
            "warning",
            "extraction",
            f"Detected {layout['note_candidates_retained_count']} possible lower-page note line(s), retained in semantic text.",
            "Review selected/layout-region-review.json; possible notes are not silently removed.",
        )
    if int(layout.get("excluded_footnote_count") or 0):
        add(
            "PDF_LAYOUT_FOOTNOTES_EXCLUDED",
            "info",
            "extraction",
            f"Excluded {layout['excluded_footnote_count']} high-confidence lower-page footnote group(s).",
            "Review selected/layout-region-review.json before relying on content-quality retrieval evidence.",
        )
    lane_review = selected.get("lane_review") or {}
    if int(lane_review.get("proposed_supplementary_count") or 0):
        if lane_review.get("primary_payload_changed"):
            add(
                "PDF_SUPPLEMENTARY_REFERENCE_REGIONS_EXCLUDED",
                "info",
                "extraction",
                f"Excluded {lane_review.get('primary_excluded_segment_count', 0)} segment(s) from automatically classified sustained reference/index regions.",
                "Original text and page-level reasons remain in selected/retrieval-lane-review.json and supplementary-content-candidates.txt.",
            )
        else:
            add(
                "PDF_SUPPLEMENTARY_LANE_CANDIDATES",
                "warning",
                "extraction",
                f"Found {lane_review['proposed_supplementary_count']} medium-confidence supplementary candidate(s); retained because no narrow automatic exclusion rule matched.",
                "Inspect selected/retrieval-lane-review.json and supplementary-content-candidates.txt if the document-specific evidence should inform a future narrow rule.",
            )
    if selected.get("backend_word_disagreement", 0) > 0.35:
        disagreement_resolution = selected.get("backend_word_disagreement_resolution") or {}
        if disagreement_resolution.get("accepted"):
            add(
                "BACKEND_TEXT_COVERAGE_DISAGREEMENT_EXPLAINED",
                "info",
                "backend_selection",
                (
                    f"Backend word counts differ by {selected['backend_word_disagreement']:.1%}, "
                    "but the shorter extraction carried objective weakness while the complete "
                    "OCR recovery passed the independent quality and provenance checks."
                ),
                "The retained resolution evidence records every check and peer metric.",
            )
        else:
            add("BACKEND_TEXT_COVERAGE_DISAGREEMENT", "error", "backend_selection", f"Backend word counts differ by {selected['backend_word_disagreement']:.1%}.", "Compare candidate extraction reports before upload.")
    for candidate in candidates:
        if candidate.get("error"):
            code = "UNSTRUCTURED_UNAVAILABLE_OR_FAILED" if candidate.get("backend") == "unstructured" else "EXTRACTION_BACKEND_FAILED"
            add(code, "warning", "backend_selection", f"{candidate.get('backend')} failed: {candidate.get('error')}", "Use a passing candidate or repair the backend dependency.")
    reconciled_boundaries = [candidate for candidate in candidates if candidate.get("boundary_reconciled")]
    if reconciled_boundaries:
        details = ", ".join(
            f"{candidate.get('backend')} proposed page {candidate.get('independent_start_page')}"
            for candidate in reconciled_boundaries
        )
        add(
            "BACKEND_BOUNDARY_DECISION_RECONCILED",
            "warning",
            "structure",
            (
                "Automatic extraction enforced one shared body/end boundary after backend proposals differed "
                f"({details}). The shared boundary prevents an extractor-specific slice from changing selection metrics."
            ),
            "Review the prepared text and override the first page only if the shared boundary is visibly incorrect.",
        )
    if any(candidate.get("boundary_reference_backend") == "conservative_neutral" for candidate in candidates):
        add(
            "BOUNDARY_REFERENCE_UNRELIABLE_CONSERVATIVE_RANGE",
            "warning",
            "structure",
            "No extractor had enough clean text to safely infer a shared body boundary, so all candidates used the same conservative full-document range.",
            "Review the prepared text or set an explicit first/end page before upload if the front or back matter should be excluded.",
        )
    if selected.get("vector_validation_status") == "not_run_extraction_only":
        add("VECTOR_RETRIEVAL_NOT_TESTED", "info", "retrieval", "No local vector retrieval simulation was run.", "Run an embedding simulation or validate after AnythingLLM ingestion.")
    if selected.get("fallback_marker_status") == "warning":
        inline_fallback_required = bool(selected.get("inline_fallback_required"))
        add(
            "INLINE_FALLBACK_MARKER_LOSS",
            "warning" if inline_fallback_required else "info",
            "chunk_simulation",
            (
                "Some simulated AnythingLLM chunks do not contain an inline fallback marker."
                if inline_fallback_required
                else "Some auxiliary inline-fallback chunks do not contain a marker; native metadata remains the primary provenance path."
            ),
            (
                "Use native title metadata or reduce segment/chunk mismatch before relying on inline citations."
                if inline_fallback_required
                else "Review this only if you plan to upload or cite from the inline-fallback artifact."
            ),
        )
    if storage_report.get("status") not in {"complete", "missing"}:
        add("ANYTHINGLLM_STORAGE_INSPECTION_FAILED", "warning", "anythingllm", storage_report.get("error") or str(storage_report.get("status")), "Close database locks or verify the Desktop storage path.")
    runtime_status = metadata_schema_report.get("runtime_api_status")
    if runtime_status == "server_unreachable":
        add(
            "ANYTHINGLLM_SERVER_OFFLINE",
            "info" if upload_report.get("status") == "skipped_prepare_only" else "error",
            "anythingllm",
            "AnythingLLM local storage exists, but its HTTP API is not running at the configured URL.",
            "Start AnythingLLM before native upload or runtime verification.",
        )
    elif runtime_status == "reachable_authentication_failed":
        add(
            "ANYTHINGLLM_API_AUTHENTICATION_REQUIRED",
            "warning" if upload_report.get("status") == "skipped_prepare_only" else "error",
            "anythingllm",
            "The AnythingLLM API is reachable but rejected the current or missing API key.",
            "Create or enter a Developer API key before native upload.",
        )
    upload_status = upload_report.get("status")
    if upload_status not in {
        "skipped_prepare_only",
        "complete",
        "complete_with_key_cleanup_warning",
    }:
        upload_code = {
            "blocked_readiness_gate": "ANYTHINGLLM_UPLOAD_BLOCKED_BY_READINESS",
            "error_missing_api_url": "ANYTHINGLLM_API_URL_MISSING",
            "error_missing_workspace": "ANYTHINGLLM_WORKSPACE_REQUIRED",
            "error": "ANYTHINGLLM_UPLOAD_OR_EMBED_FAILED",
        }.get(upload_status, "ANYTHINGLLM_UPLOAD_NOT_CONFIRMED")
        add(upload_code, "error", "anythingllm", f"Upload status: {upload_status}. {upload_report.get('errors') or ''}", "Correct the reported API/workspace problem and retry; generated files remain usable.")
    if (
        upload_status != "skipped_prepare_only"
        and workspace_gate.get("status")
        in {"workspace_missing", "blocked_model_not_configured", "blocked_claude_or_anthropic_model"}
    ):
        add("ANYTHINGLLM_WORKSPACE_MODEL_GATE_BLOCKED", "warning", "anythingllm", workspace_gate.get("message") or workspace_gate.get("status"), "Select an existing DeepSeek-configured test workspace before accepting an in-app result.")
    if post_upload_report.get("status") not in {
        "not_checked",
        "workspace_missing",
        "not_checked_no_segments",
        "not_checked_no_upload",
        "complete",
        "pass",
        "pass_with_missing_workspace_document_records",
    }:
        add("ANYTHINGLLM_POST_UPLOAD_NOT_CONFIRMED", "warning", "anythingllm", post_upload_report.get("message") or post_upload_report.get("status"), "Verify workspace attachment and LanceDB vector rows after a real upload.")
    runtime_status = runtime_validation_report.get("status")
    if runtime_status == "blocked_provider_authentication":
        add(
            "ANYTHINGLLM_DEEPSEEK_PROVIDER_AUTHENTICATION_FAILED",
            "error",
            "anythingllm_chat",
            "The workspace is configured for DeepSeek, but the provider rejected its API credential.",
            "Update the DeepSeek V4 Pro provider credential in AnythingLLM, then rerun native metadata validation.",
        )
    elif runtime_status == "vector_retrieval_failed":
        add(
            "ANYTHINGLLM_NATIVE_VECTOR_RETRIEVAL_FAILED",
            "error",
            "anythingllm_retrieval",
            "One or more native-metadata compatibility probes did not retrieve the expected source within the requested result set.",
            "Inspect the runtime validation report before using this ingestion mode.",
        )
    elif runtime_status == "chat_citation_failed":
        add(
            "ANYTHINGLLM_NATIVE_METADATA_CHAT_CITATION_FAILED",
            "error",
            "anythingllm_chat",
            "DeepSeek answered, but did not reproduce the expected page and segment from sourceDocument.",
            "Use the filename/title fallback or inline metadata fallback and rerun the test.",
        )
    elif runtime_status in {
        "chat_runtime_timeout",
        "pass_with_chat_timeout",
        "pass_with_vector_timeout",
    }:
        add(
            "ANYTHINGLLM_NATIVE_METADATA_RUNTIME_TIMEOUT",
            "warning",
            "anythingllm_runtime",
            "An optional AnythingLLM runtime probe timed out after exact vector-storage evidence succeeded; this is a runtime event, not a retrieval miss.",
            "Retry the saved runtime validation after the provider settles; keep the vector and provenance evidence separate from this runtime event.",
        )
    temp_status = temporary_workspace_validation.get("status")
    if temp_status not in {None, "", "not_run"}:
        if temp_status == "workspace_create_failed":
            add(
                "ANYTHINGLLM_CHUNK_SURVIVAL_WORKSPACE_CREATE_FAILED",
                "warning",
                "anythingllm_chunk_survival",
                temporary_workspace_validation.get("error") or "Chunk survival test workspace could not be created.",
                "Check the local AnythingLLM API and retry the chunk survival test.",
            )
        elif temp_status != "complete":
            add(
                "ANYTHINGLLM_CHUNK_SURVIVAL_UNCONFIRMED",
                "warning",
                "anythingllm_chunk_survival",
                temporary_workspace_validation.get("error")
                or (temporary_workspace_validation.get("post_upload_report") or {}).get("message")
                or temporary_workspace_validation.get("upload_status")
                or temporary_workspace_validation.get("post_upload_status")
                or "Chunk survival test did not confirm preserved chunk boundaries.",
                "Inspect temporary-workspace-validation.csv and the post-upload verification report before trusting AnythingLLM-parity or page-bounded modes.",
            )
        if temporary_workspace_validation.get("retention_status") not in {"left_visible_for_manual_review", "not_applicable"}:
            add(
                "ANYTHINGLLM_CHUNK_SURVIVAL_WORKSPACE_RETENTION_UNCONFIRMED",
                "warning",
                "anythingllm_chunk_survival",
                temporary_workspace_validation.get("error") or temporary_workspace_validation.get("retention_status"),
                "Check whether the chunk survival workspace remained visible in AnythingLLM for manual review.",
            )
    add(
        "NATIVE_METADATA_ARBITRARY_FIELDS_UNSUPPORTED",
        "info",
        "metadata",
        "AnythingLLM raw-text ingestion does not preserve arbitrary pdf_page, chapter, or segment_id keys.",
        "The native-header payload encodes those values into supported title and description fields.",
    )
    if not diagnostics:
        add("RUN_VALIDATED", "info", "complete", "No diagnostic conditions were detected.")
    return diagnostics


def build_diagnostics_html(diagnostics):
    rows = "".join(
        "<tr>"
        f"<td><code>{html.escape(row['code'])}</code></td>"
        f"<td>{html.escape(row['severity'])}</td>"
        f"<td>{html.escape(row['stage'])}</td>"
        f"<td>{html.escape(row['message'])}</td>"
        f"<td>{html.escape(row['recommended_action'])}</td>"
        "</tr>"
        for row in diagnostics
    )
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Run Diagnostics</title>
<style>body{{font-family:Segoe UI,Arial,sans-serif;max-width:1200px;margin:32px auto;line-height:1.4}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ccd3dc;padding:7px;text-align:left;vertical-align:top}}</style>
</head><body><h1>Run Diagnostics</h1>
<table><tr><th>Code</th><th>Severity</th><th>Stage</th><th>Message</th><th>Recommended action</th></tr>{rows}</table>
</body></html>"""


def write_failure_package(pdf_path: Path, out_root: Path, exc, args=None):
    pdf_path = Path(pdf_path)
    out_root.mkdir(parents=True, exist_ok=True)
    profile = {
        "source_file": str(pdf_path),
        "filename": pdf_path.name,
        "file_size": pdf_path.stat().st_size if pdf_path.exists() else 0,
        "pdf_page_count": "",
        "detected_title": pdf_path.stem,
        "detected_author": "",
    }
    try:
        profile.update(pdf_metadata(pdf_path))
    except Exception:
        pass
    rows = [
        {
            "check": "extraction_produced_segments",
            "status": "fail",
            "details": str(exc),
        },
        {
            "check": "low_text_or_scanned_detection",
            "status": "fail",
            "details": "No extraction backend produced usable segments. Treat as scanned, OCR-needed, corrupt, or very low text until reviewed.",
        },
    ]
    edge_case_report = {"overall_status": "fail", "failures": 2, "warnings": 0, "rows": rows}
    write_csv(out_root / "edge-case-results.csv", rows)
    (out_root / "edge-case-report.html").write_text(build_edge_case_html(edge_case_report), encoding="utf-8")
    write_json(out_root / "edge-case-summary.json", {k: v for k, v in edge_case_report.items() if k != "rows"})
    storage_dir = Path(getattr(args, "anythingllm_storage_dir", "") or default_anythingllm_storage_dir()) if args else default_anythingllm_storage_dir()
    target_workspace_slug = getattr(args, "test_workspace_slug", "test") if args else "test"
    workspace_gate = read_workspace_model_gate(storage_dir, target_workspace_slug)
    inspection_dir = out_root / "inspection"
    write_json(inspection_dir / "workspace-model-gate.json", workspace_gate)
    write_csv(
        inspection_dir / "workspace-model-gate.csv",
        [
            {
                "status": workspace_gate.get("status"),
                "workspace_slug": workspace_gate.get("workspace_slug"),
                "workspace_name": workspace_gate.get("workspace_name"),
                "chat_provider": workspace_gate.get("chat_provider"),
                "chat_model": workspace_gate.get("chat_model"),
                "deepseek_like": workspace_gate.get("deepseek_like"),
                "blocked_terms_present": workspace_gate.get("blocked_terms_present"),
                "message": workspace_gate.get("message"),
            }
        ],
    )
    post_upload = {
        "status": "not_checked_no_segments",
        "classification": "not_checked_no_segments",
        "message": "No segment manifest exists, so post-upload verification is not applicable.",
        "polling_observer_failures": [],
    }
    write_json(inspection_dir / "post-upload-verification.json", post_upload)
    write_csv(inspection_dir / "post-upload-verification.csv", [post_upload])
    error_text = str(exc)
    if "no extraction backend produced usable segments" in error_text.casefold():
        diagnostic_code = "PDF_OCR_REQUIRED_OR_NO_TEXT_LAYER"
        diagnostic_action = "Run OCR or inspect whether the PDF contains extractable body text."
    elif "password" in error_text.casefold() or "encrypted" in error_text.casefold():
        diagnostic_code = "PDF_ENCRYPTED_PASSWORD_REQUIRED"
        diagnostic_action = "Export an unlocked PDF copy."
    else:
        diagnostic_code = "PIPELINE_PREPARATION_FAILED"
        diagnostic_action = "Review the exception and backend logs, then retry with a repaired PDF or alternate backend."
    diagnostics = [
        {
            "code": diagnostic_code,
            "severity": "error",
            "stage": "extraction",
            "message": error_text,
            "recommended_action": diagnostic_action,
        }
    ]
    write_json(out_root / "diagnostics.json", diagnostics)
    write_csv(out_root / "diagnostics.csv", diagnostics)
    (out_root / "diagnostics.html").write_text(build_diagnostics_html(diagnostics), encoding="utf-8")
    summary = {
        "output_root": str(out_root),
        "readiness_status": "failed",
        "selected_backend": "",
        "pdf_page_count": profile.get("pdf_page_count", ""),
        "start_page": "",
        "end_page": "",
        "segments": 0,
        "edge_case_status": "fail",
        "edge_case_failures": 2,
        "edge_case_warnings": 0,
        "edge_case_report": str(out_root / "edge-case-report.html"),
        "edge_case_results": str(out_root / "edge-case-results.csv"),
        "workspace_model_gate_status": workspace_gate.get("status"),
        "workspace_model_gate_message": workspace_gate.get("message"),
        "workspace_model_gate": str(inspection_dir / "workspace-model-gate.csv"),
        "post_upload_verification_status": post_upload["status"],
        "post_upload_classification": post_upload["classification"],
        "post_upload_verification": str(inspection_dir / "post-upload-verification.csv"),
        "diagnostics_report": str(out_root / "diagnostics.html"),
        "diagnostics_csv": str(out_root / "diagnostics.csv"),
        "diagnostic_error_count": 1,
        "diagnostic_warning_count": 0,
        "error": str(exc),
    }
    write_json(out_root / "source-profile.json", profile)
    write_json(out_root / "run-summary.json", summary)
    return summary


def generate_api_payloads(segments, mode):
    rows = []
    for row in segments:
        if mode == "strict":
            segment_title = native_segment_title(row, include_heading=False)
        else:
            segment_title = native_segment_title(row, include_heading=True)
        filename = native_segment_filename(row)
        metadata = {
            "title": segment_title,
            "docAuthor": row.get("source_author") or "",
            "description": (
                f"Source title: {row['source_title']}. "
                f"{pdf_page_metadata_label(row)} "
                f"Logical page: {row.get('logical_page') or 'unknown'}. "
                f"Page lines: {row.get('page_line_start') or 'unknown'}-"
                f"{row.get('page_line_end') or 'unknown'}. "
                + (
                    "This record spans multiple PDF pages; exact page-level citations are unavailable after downstream chunking. "
                    if int(row.get("pdf_page_end") or row["pdf_page"]) != int(row["pdf_page"])
                    else ""
                )
                + f"Segment: {row['segment_id']}. "
                f"Region: {row.get('document_region') or 'unknown'}. "
                f"Part/chapter/section: {row.get('part') or ''} {row.get('chapter') or ''} "
                f"{row.get('section') or ''} {row.get('subsection') or ''}."
            ).strip(),
            "docSource": f"local-pdf://sha256/{row['source_sha256']}",
            "chunkSource": f"segment://{row['segment_id']}",
        }
        if row.get("source_published_epoch_ms") is not None:
            metadata["published"] = row["source_published_epoch_ms"]
        rows.append(
            {
                "filename": filename,
                "textContent": row["text"],
                "metadata": metadata,
            }
        )
    return rows


def native_segment_filename(row):
    return safe_stem(native_segment_title(row, include_heading=True))[:140] + ".txt"


def build_page_parent_rows(segments):
    page_rows = {}
    for row in segments:
        parent_id = f"{row['source_id']}::pdf-p{int(row['pdf_page']):04d}"
        page_row = page_rows.setdefault(
            parent_id,
            {
                "parent_id": parent_id,
                "source_id": row["source_id"],
                "source_title": row["source_title"],
                "source_author": row.get("source_author") or "",
                "source_short_label": row.get("source_short_label") or row["source_title"],
                "source_file": row["source_file"],
                "source_sha256": row["source_sha256"],
                "backend": row["backend"],
                "pdf_page": row["pdf_page"],
                "pdf_page_end": row.get("pdf_page_end") or row["pdf_page"],
                "logical_page": row.get("logical_page") or "",
                "logical_page_end": row.get("logical_page_end") or row.get("logical_page") or "",
                "document_region": row.get("document_region") or "",
                "part": row.get("part") or "",
                "chapter": row.get("chapter") or "",
                "section": row.get("section") or "",
                "subsection": row.get("subsection") or "",
                "segment_count": 0,
                "segment_ids": [],
                "segment_indexes": [],
                "char_start_page": None,
                "char_end_page": None,
                "page_line_start": None,
                "page_line_end": None,
                "text_parts": [],
            },
        )
        page_row["segment_count"] += 1
        page_row["segment_ids"].append(row["segment_id"])
        page_row["segment_indexes"].append(int(row["segment_index"]))
        page_row["text_parts"].append(row.get("text", ""))
        start = row.get("char_start_page")
        end = row.get("char_end_page")
        if start is not None:
            page_row["char_start_page"] = start if page_row["char_start_page"] is None else min(page_row["char_start_page"], start)
        if end is not None:
            page_row["char_end_page"] = end if page_row["char_end_page"] is None else max(page_row["char_end_page"], end)
        line_start = row.get("page_line_start")
        line_end = row.get("page_line_end")
        if line_start is not None:
            page_row["page_line_start"] = (
                line_start if page_row["page_line_start"] is None else min(page_row["page_line_start"], line_start)
            )
        if line_end is not None:
            page_row["page_line_end"] = (
                line_end if page_row["page_line_end"] is None else max(page_row["page_line_end"], line_end)
            )

    parents = []
    for parent in sorted(page_rows.values(), key=lambda value: (int(value["pdf_page"]), value["parent_id"])):
        text = "\n\n".join(part.strip() for part in parent.pop("text_parts") if str(part).strip()).strip()
        parent["text"] = text
        parent["title"] = native_page_parent_title(parent, include_heading=True)
        parent["word_count"] = len(re.findall(r"\b[\w\u2019'-]+\b", text, flags=re.UNICODE))
        parents.append(parent)
    return parents


def build_child_parent_map(segments, parent_rows):
    parent_lookup = {
        (int(parent["pdf_page"]), str(parent.get("logical_page") or "")): parent["parent_id"]
        for parent in parent_rows
    }
    rows = []
    for row in segments:
        key = (int(row["pdf_page"]), str(row.get("logical_page") or ""))
        rows.append(
            {
                "segment_id": row["segment_id"],
                "segment_index": row["segment_index"],
                "parent_id": parent_lookup.get(key) or f"{row['source_id']}::pdf-p{int(row['pdf_page']):04d}",
                "pdf_page": row["pdf_page"],
                "pdf_page_end": row.get("pdf_page_end") or row["pdf_page"],
                "logical_page": row.get("logical_page") or "",
                "logical_page_end": row.get("logical_page_end") or row.get("logical_page") or "",
                "page_line_start": row.get("page_line_start") or "",
                "page_line_end": row.get("page_line_end") or "",
                "chapter": row.get("chapter") or "",
                "section": row.get("section") or "",
            }
        )
    return rows


def generate_page_parent_payloads(parent_rows, mode):
    rows = []
    for row in parent_rows:
        title = row["source_short_label"] if mode == "strict" else row["title"]
        filename = safe_stem(row["title"])[:140] + ".txt"
        metadata = {
            "title": title,
            "docAuthor": row.get("source_author") or "",
            "description": (
                f"Source title: {row['source_title']}. "
                f"{pdf_page_metadata_label(row)} "
                f"Logical page: {row.get('logical_page') or 'unknown'}. "
                f"Page lines: {row.get('page_line_start') or 'unknown'}-"
                f"{row.get('page_line_end') or 'unknown'}. "
                + (
                    "This parent record spans multiple PDF pages; exact page-level citations are unavailable after downstream chunking. "
                    if int(row.get("pdf_page_end") or row["pdf_page"]) != int(row["pdf_page"])
                    else ""
                )
                + f"Parent page id: {row['parent_id']}. "
                f"Contains {row['segment_count']} child segments. "
                f"Part/chapter/section: {row.get('part') or ''} {row.get('chapter') or ''} "
                f"{row.get('section') or ''} {row.get('subsection') or ''}."
            ).strip(),
            "docSource": f"local-pdf://sha256/{row['source_sha256']}",
            "chunkSource": f"page-parent://{row['parent_id']}",
        }
        rows.append({"filename": filename, "textContent": row["text"], "metadata": metadata})
    return rows


def representation_comparison_rows(segments, page_parent_rows, chunk_size, chunk_overlap, embedding_config):
    def summarize_units(name, units, provenance_strength, retrieval_precision, citation_fidelity):
        lengths = [len(str(unit.get("text") or "")) for unit in units]
        if lengths:
            avg_chars = round(sum(lengths) / len(lengths), 1)
            min_chars = min(lengths)
            max_chars = max(lengths)
        else:
            avg_chars = min_chars = max_chars = 0
        return {
            "representation": name,
            "unit_count": len(units),
            "avg_chars": avg_chars,
            "min_chars": min_chars,
            "max_chars": max_chars,
            "embedder_max_chunk_length": embedding_config.get("max_chunk_length") or "",
            "anythingllm_chunk_size": chunk_size,
            "anythingllm_chunk_overlap": chunk_overlap,
            "provenance_strength": provenance_strength,
            "retrieval_precision_expectation": retrieval_precision,
            "citation_fidelity_expectation": citation_fidelity,
        }

    rows = [
        summarize_units(
            "passage_segments",
            segments,
            "high_segment_level",
            "higher",
            "medium_without_parent_reconstruction",
        ),
        summarize_units(
            "page_parents",
            page_parent_rows,
            "high_page_level",
            "lower_without_reranking",
            "higher",
        ),
    ]
    if page_parent_rows and segments:
        rows.append(
            {
                "representation": "relationship",
                "unit_count": round(len(segments) / max(len(page_parent_rows), 1), 3),
                "avg_chars": "",
                "min_chars": "",
                "max_chars": "",
                "embedder_max_chunk_length": embedding_config.get("max_chunk_length") or "",
                "anythingllm_chunk_size": chunk_size,
                "anythingllm_chunk_overlap": chunk_overlap,
                "provenance_strength": "children_per_parent_ratio",
                "retrieval_precision_expectation": f"{len(segments)} child segments across {len(page_parent_rows)} page parents",
                "citation_fidelity_expectation": "parent-child reconstruction available",
            }
        )
    return rows


def metadata_layer_visibility_rows(
    segment_payloads,
    page_parent_payloads,
    metadata_schema_report,
    native_metadata_report,
):
    sample_segment_metadata = (segment_payloads[0] or {}).get("metadata", {}) if segment_payloads else {}
    sample_page_parent_metadata = (page_parent_payloads[0] or {}).get("metadata", {}) if page_parent_payloads else {}
    runtime_schema = metadata_schema_report.get("schema", {}) if isinstance(metadata_schema_report.get("schema"), dict) else {}
    observed_lancedb_fields = set(native_metadata_report.get("metadata_fields_seen") or [])

    rows = []
    direct_fields = [
        "url",
        "title",
        "docAuthor",
        "description",
        "docSource",
        "chunkSource",
        "published",
    ]
    for field in direct_fields:
        chunk_visible = (
            "yes_sourceDocument_header"
            if field == "title"
            else "yes_native_header"
            if field == "published"
            else "conditional_link_only"
            if field == "chunkSource"
            else "no_not_by_default"
        )
        rows.append(
            {
                "field": field,
                "field_type": "direct_supported",
                "segment_payload": "yes" if field in sample_segment_metadata else "no",
                "page_parent_payload": "yes" if field in sample_page_parent_metadata else "no",
                "anythingllm_raw_text_contract": "yes" if field in ANYTHINGLLM_RAW_TEXT_METADATA_FIELDS else "no",
                "runtime_schema_reports": "yes" if field in runtime_schema else "no",
                "lancedb_field_observed": "yes" if field in observed_lancedb_fields else "no",
                "chunk_text_visible_expected": chunk_visible,
                "notes": ANYTHINGLLM_RAW_TEXT_METADATA_FIELDS.get(field, ""),
            }
        )

    derived_rows = [
        ("pdf_page", "title+description", "no_direct_field", "Encode physical page into title and description."),
        ("logical_page", "title+description", "no_direct_field", "Encode logical page into title and description."),
        ("segment_id", "title+description+chunkSource", "no_direct_field", "Keep stable segment identity in title and chunkSource."),
        ("chapter", "title+description", "no_direct_field", "Encode chapter or nearest section into title and description."),
        ("section", "description", "no_direct_field", "Section is usually lower-priority than chapter in the title."),
        ("parent_id", "title+description+chunkSource", "no_direct_field", "Used for page-parent representation and reconstruction."),
    ]
    for field, encoded_in, contract_status, note in derived_rows:
        rows.append(
            {
                "field": field,
                "field_type": "derived_provenance",
                "segment_payload": "encoded" if sample_segment_metadata else "not_generated",
                "page_parent_payload": "encoded" if sample_page_parent_metadata else "not_generated",
                "anythingllm_raw_text_contract": contract_status,
                "runtime_schema_reports": "no_direct_field",
                "lancedb_field_observed": "no_direct_field",
                "chunk_text_visible_expected": "only_if_encoded_into_promoted_text_fields",
                "notes": note,
            }
        )
    return rows


def explain_observed_columns(workspace_report):
    workspace_doc = workspace_report.get("sample_workspace_document") or {}
    workspace_metadata = workspace_doc.get("metadata_parsed") or {}
    custom_doc = workspace_report.get("sample_custom_document_record") or {}
    lancedb_row = workspace_report.get("sample_lancedb_row") or {}

    observed = {
        "workspace_documents_row": sorted(workspace_doc.keys()),
        "workspace_metadata_json": sorted(workspace_metadata.keys()),
        "custom_document_json": sorted(custom_doc.keys()),
        "lancedb_row": sorted(lancedb_row.keys()),
    }

    notes = {
        "id": "Internal identifier. In metadata JSON this is the raw-text document id; in LanceDB this is usually the vector row id.",
        "docId": "Workspace document identifier used to link workspace_documents and document_vectors.",
        "filename": "Workspace-facing document filename or raw upload name.",
        "docpath": "Relative path to the stored custom document JSON file.",
        "metadata": "Serialized metadata JSON stored in workspace_documents.",
        "metadata_parsed": "Parsed view of workspace_documents.metadata for inspection only.",
        "createdAt": "Creation timestamp in the local SQLite layer.",
        "url": "Raw-text source URL or derived file URL.",
        "title": "Best candidate for chunk-visible provenance because AnythingLLM promotes it to sourceDocument in native headers.",
        "docAuthor": "Stored author field. Usually available as metadata, not chunk-visible by default.",
        "description": "Stored descriptive metadata. Good place to encode page/chapter, but not chunk-visible by default.",
        "docSource": "Stable source-document identifier. Good for grouping/filtering.",
        "chunkSource": "Stable retrieval-unit identifier. Good for exact mapping and verification.",
        "published": "Native metadata field that AnythingLLM may prepend into chunk text.",
        "wordCount": "Stored approximate word count.",
        "token_count_estimate": "Stored token estimate, useful for diagnostics rather than retrieval.",
        "pageContent": "Raw cached custom-document body text before embedding.",
        "text": "Chunk text stored in LanceDB; this is the most important LLM-visible field during retrieval.",
        "vector": "Dense embedding values stored in LanceDB.",
    }

    llm_visibility = {
        "title": "likely_via_sourceDocument_header",
        "published": "likely_via_native_header",
        "text": "yes_primary_chunk_text",
        "chunkSource": "conditional_not_default",
        "description": "stored_only_unless_re-encoded",
        "docAuthor": "stored_only_unless_re-encoded",
        "docSource": "stored_only_unless_re-encoded",
        "pageContent": "no_cache_layer_only",
        "vector": "no_embedding_only",
        "metadata": "no_sqlite_wrapper_only",
        "metadata_parsed": "no_inspector_only",
    }

    rows = []
    for layer, keys in observed.items():
        for key in keys:
            rows.append(
                {
                    "layer": layer,
                    "field": key,
                    "likely_llm_visible": llm_visibility.get(key, "no_or_unknown"),
                    "notes": notes.get(key, "Observed field in this local storage layer."),
                }
            )
    return rows


def harmonization_rows(segments, page_parent_rows, chunk_size, chunk_overlap, embedding_config):
    try:
        embedder_limit = int(embedding_config.get("max_chunk_length") or 0)
    except (TypeError, ValueError):
        embedder_limit = 0
    effective_limit = min(chunk_size, embedder_limit) if embedder_limit > 0 else int(chunk_size or 0)
    effective_step = max(1, int(chunk_size or 0) - int(chunk_overlap or 0))

    def summarize(name, units, citation_goal):
        lengths = [len(str(unit.get("text") or "")) for unit in units]
        estimated_tokens = [max(1, math.ceil(length / 4)) for length in lengths]
        exceeding = sum(1 for length in lengths if effective_limit and length > effective_limit)
        split_factor = (
            round(sum(max(1, math.ceil(length / max(effective_step, 1))) for length in lengths) / len(lengths), 3)
            if lengths
            else 0
        )
        if not lengths:
            risk = "no_units"
        elif exceeding == 0:
            risk = "low"
        elif exceeding / len(lengths) <= 0.25:
            risk = "medium"
        else:
            risk = "high"
        recommendation = (
            "Suitable for current settings."
            if risk == "low"
            else "Likely to be re-split by AnythingLLM; preserve provenance in promoted title fields and use parent-child reconstruction."
            if name == "page_parents"
            else "Reduce passage size or rely on local segmentation to keep retrieval units stable."
        )
        return {
            "representation": name,
            "unit_count": len(units),
            "avg_chars": round(sum(lengths) / len(lengths), 1) if lengths else 0,
            "max_chars": max(lengths) if lengths else 0,
            "avg_estimated_tokens": round(sum(estimated_tokens) / len(estimated_tokens), 1) if estimated_tokens else 0,
            "max_estimated_tokens": max(estimated_tokens) if estimated_tokens else 0,
            "anythingllm_chunk_size": chunk_size,
            "anythingllm_chunk_overlap": chunk_overlap,
            "embedder_max_chunk_length": embedder_limit or "",
            "effective_limit": effective_limit or "",
            "units_exceeding_effective_limit": exceeding,
            "percent_exceeding_effective_limit": round((exceeding / len(lengths)) * 100, 1) if lengths else 0,
            "approx_split_factor": split_factor,
            "harmonization_risk": risk,
            "citation_goal": citation_goal,
            "recommendation": recommendation,
        }

    return [
        summarize("passage_segments", segments, "semantic_retrieval_unit"),
        summarize("page_parents", page_parent_rows, "citation_reconstruction_unit"),
    ]


def representation_recommendation_rows(harmonization_report_rows):
    by_name = {row["representation"]: row for row in harmonization_report_rows}
    seg = by_name.get("passage_segments", {})
    parent = by_name.get("page_parents", {})
    seg_risk = str(seg.get("harmonization_risk") or "unknown")
    parent_risk = str(parent.get("harmonization_risk") or "unknown")

    if parent_risk == "low":
        citation_recommendation = "page_parents"
        citation_reason = "Page parents fit within the effective current limit and preserve page-level provenance directly."
    else:
        citation_recommendation = "passage_segments_plus_page_parent_artifacts"
        citation_reason = "Page parents are likely to be re-split; keep passage segments for upload and use page-parent artifacts for reconstruction."

    if seg_risk in {"low", "medium"}:
        default_recommendation = "passage_segments"
        default_reason = "Passage segments are the safer default retrieval unit under current settings."
    else:
        default_recommendation = "one_segment_per_page_or_smaller_passages"
        default_reason = "Current passage units exceed the effective limit too often; reduce local segment size or use one-segment-per-page mode."

    return [
        {
            "decision": "default_native_upload_representation",
            "recommended_value": default_recommendation,
            "reason": default_reason,
        },
        {
            "decision": "citation_heavy_workflow",
            "recommended_value": citation_recommendation,
            "reason": citation_reason,
        },
        {
            "decision": "current_risk_snapshot",
            "recommended_value": f"segments={seg_risk}; page_parents={parent_risk}",
            "reason": "Derived from the current local AnythingLLM chunk settings and embedder max chunk length.",
        },
    ]


def write_native_metadata_test_kit(segments, out_dir: Path, workspace_slug="test", artifact_prefix=""):
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"{safe_stem(artifact_prefix)}-" if artifact_prefix else "manual-"
    files_dir = out_dir / f"{prefix}segment-files"
    files_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for row in segments:
        filename = native_segment_filename(row)
        path = files_dir / filename
        path.write_text(row["text"], encoding="utf-8")
        rows.append(
            {
                "filename": filename,
                "pdf_page": row["pdf_page"],
                "logical_page": row.get("logical_page") or "",
                "segment_id": row["segment_id"],
                "title": native_segment_title(row, include_heading=True),
                "docAuthor": row.get("source_author") or "",
                "description": (
                    f"PDF page {row['pdf_page']}; lines {row.get('page_line_start') or 'unknown'}-"
                    f"{row.get('page_line_end') or 'unknown'}; segment {row['segment_id']}; "
                    f"{row.get('chapter') or ''} {row.get('section') or ''}"
                ).strip(),
                "docSource": f"local-pdf://sha256/{row['source_sha256']}",
                "chunkSource": f"segment://{row['segment_id']}",
                "text_file": str(path),
            }
        )
    write_csv(out_dir / f"{prefix}upload-plan.csv", rows)
    checklist = [
        "# Native Metadata Manual Test Checklist",
        "",
        f"Target workspace: `{workspace_slug}`",
        "",
        "1. In AnythingLLM, configure the target workspace to a DeepSeek-like chat model and not Claude/Sonnet.",
        "2. Upload the generated manual segment files, or use the raw-text payloads if API access becomes available.",
        "3. After upload, run the tool's read-only verification again against the same workspace.",
        "4. Check whether page/segment information survives in workspace document metadata and LanceDB rows.",
        "5. Ask an AnythingLLM query for a known phrase and verify whether the answer can cite page/segment from native metadata.",
        "",
        "Primary native metadata strategy: clean passage text plus page/segment/chapter in title, description, docSource, and chunkSource.",
        "Fallback strategy: use the generated inline-marker upload file only if native metadata is not LLM-visible.",
    ]
    (out_dir / f"{prefix}test-checklist.md").write_text("\n".join(checklist) + "\n", encoding="utf-8")
    return {
        "files_dir": str(files_dir),
        "zip_file": "",
        "upload_plan": str(out_dir / f"{prefix}upload-plan.csv"),
        "checklist": str(out_dir / f"{prefix}test-checklist.md"),
        "file_count": len(rows),
    }


def generate_probes(segments, max_probes=12):
    probes = []
    seen = set()
    candidates = []
    for row in segments:
        text = row["text"]
        sentences = re.split(r"(?<=[.!?])\s+", text)
        for sentence in sentences:
            clean = normalize_text(sentence)
            words = re.findall(r"\b[A-Za-z][A-Za-z\u2019'-]{3,}\b", clean)
            if 8 <= len(words) <= 38 and 80 <= len(clean) <= 260:
                rare_score = sum(1 for w in words if len(w) >= 9)
                candidates.append((rare_score, len(set(w.lower() for w in words)), row, clean))
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    if candidates:
        segment_positions = {row["segment_id"]: index for index, row in enumerate(segments)}
        buckets = [[], [], [], []]
        for candidate in candidates:
            position = segment_positions.get(candidate[2]["segment_id"], 0)
            bucket = min(3, int((position / max(len(segments), 1)) * 4))
            buckets[bucket].append(candidate)
        distributed = []
        while any(buckets):
            for bucket in buckets:
                if bucket:
                    distributed.append(bucket.pop(0))
        candidates = distributed

    for _, _, row, sentence in candidates:
        key = sentence.casefold()
        if key in seen:
            continue
        seen.add(key)
        phrase_words = sentence.split()
        exact_phrase = " ".join(phrase_words[: min(18, len(phrase_words))])
        probes.append(
            {
                "kind": "exact_phrase",
                "query": exact_phrase,
                "expected_segment_id": row["segment_id"],
                "expected_phrase": exact_phrase,
                "expected_pdf_page": row["pdf_page"],
                "chapter": row.get("chapter") or "",
            }
        )
        probes.append(
            {
                "kind": "page_targeted",
                "query": f"On PDF page {row['pdf_page']}, where does the document discuss {exact_phrase}?",
                "expected_segment_id": row["segment_id"],
                "expected_phrase": exact_phrase,
                "expected_pdf_page": row["pdf_page"],
                "chapter": row.get("chapter") or "",
            }
        )
        if row.get("chapter"):
            concept_terms = " ".join(
                w for w in phrase_words if len(re.sub(r"\W", "", w)) >= 7
            )[:180]
            if concept_terms:
                probes.append(
                    {
                        "kind": "concept",
                        "query": f"{row['chapter']} {concept_terms}",
                        "expected_segment_id": row["segment_id"],
                        "expected_phrase": "",
                        "expected_pdf_page": row["pdf_page"],
                        "chapter": row.get("chapter") or "",
                    }
                )
            probes.append(
                {
                    "kind": "chapter_targeted",
                    "query": f"What does the chapter {row['chapter']} say about {' '.join(phrase_words[:10])}?",
                    "expected_segment_id": row["segment_id"],
                    "expected_phrase": "",
                    "expected_pdf_page": row["pdf_page"],
                    "chapter": row.get("chapter") or "",
                }
            )
        if len(probes) >= max_probes:
            break
    return probes[:max_probes]


def add_user_validation_probes(probes, segments, phrases, max_total=24):
    combined = list(probes)
    seen = {(row.get("kind"), (row.get("query") or "").casefold()) for row in combined}
    for phrase in phrases or []:
        clean_phrase = normalize_text(phrase)
        if not clean_phrase:
            continue
        match = next(
            (row for row in segments if clean_phrase.casefold() in row.get("text", "").casefold()),
            None,
        )
        probe = {
            "kind": "user_exact_phrase",
            "query": clean_phrase,
            "expected_segment_id": match.get("segment_id") if match else "",
            "expected_phrase": clean_phrase,
            "expected_pdf_page": match.get("pdf_page") if match else "",
            "chapter": match.get("chapter") if match else "",
        }
        key = (probe["kind"], clean_phrase.casefold())
        if key not in seen:
            combined.append(probe)
            seen.add(key)
        if len(combined) >= max_total:
            break
    return combined


def literal_eval(upload_text, probes):
    lower = upload_text.casefold()
    normalized_lower = re.sub(r"\s+", " ", lower).strip()
    rows = []
    for probe in probes:
        phrase = probe.get("expected_phrase") or ""
        if not phrase:
            rows.append({**probe, "status": "skipped", "char_position": -1, "match_mode": "not_applicable"})
            continue
        pos = lower.find(phrase.casefold())
        if pos >= 0:
            rows.append({**probe, "status": "pass", "char_position": pos, "match_mode": "literal"})
            continue
        # PDF extraction naturally retains some visual line wrapping. An
        # automatic provenance probe must not call a document unsafe merely
        # because identical words are separated by newlines or repeated
        # spaces in the generated upload text. This normalizes whitespace
        # only; punctuation and all meaningful characters remain exact.
        normalized_phrase = re.sub(r"\s+", " ", phrase.casefold()).strip()
        normalized_pos = normalized_lower.find(normalized_phrase) if normalized_phrase else -1
        rows.append(
            {
                **probe,
                "status": "pass" if normalized_pos >= 0 else "fail",
                "char_position": pos,
                "normalized_char_position": normalized_pos,
                "match_mode": "whitespace_normalized" if normalized_pos >= 0 else "missing",
            }
        )
    return rows


def chunk_marker_eval(upload_text, chunk_size=1000, overlap=20):
    chunks = simulated_chunks(upload_text, chunk_size=chunk_size, overlap=overlap)
    without_marker = [
        i for i, chunk in enumerate(chunks) if not re.search(r"\[[^\]\n]*(?:p\d+|PDF_PAGE|seg:)[^\]\n]*\]", chunk)
    ]
    suspicious = []
    for i, chunk in enumerate(chunks):
        letters = len(re.findall(r"\w", chunk))
        nonspace = len(re.findall(r"\S", chunk))
        ratio = letters / nonspace if nonspace else 0
        if len(chunk) < 120 or ratio < 0.45:
            suspicious.append(i)
    return {
        "chunk_size": chunk_size,
        "chunk_overlap": overlap,
        "chunk_count": len(chunks),
        "chunks_without_marker": len(without_marker),
        "suspicious_chunks": len(suspicious),
        "first_chunks_without_marker": without_marker[:20],
        "first_suspicious_chunks": suspicious[:20],
    }


def simulate_native_header_chunks(segments, chunk_size=1000, overlap=20):
    units = []
    for row in segments:
        stored_title = native_segment_filename(row)
        header = f"\nsourceDocument: {stored_title}\n\n"
        for chunk_index, text_chunk in enumerate(
            simulated_chunks(row.get("text", ""), chunk_size=chunk_size, overlap=overlap),
            start=1,
        ):
            units.append(
                {
                    **row,
                    "text": header + text_chunk,
                    "retrieval_unit_id": f"{row['segment_id']}#c{chunk_index:03d}",
                    "retrieval_chunk_index": chunk_index,
                    "native_header": header.strip(),
                }
            )
    return units


def native_header_chunk_eval(units):
    missing_source_document = 0
    missing_page_or_segment = 0
    for row in units:
        text = row.get("text", "")
        if "sourceDocument:" not in text:
            missing_source_document += 1
        if not re.search(r"\bp\d{1,4}\b", text, re.I) or not re.search(r"\bs\d{5}\b", text, re.I):
            missing_page_or_segment += 1
    return {
        "retrieval_chunks": len(units),
        "chunks_without_source_document": missing_source_document,
        "chunks_without_page_or_segment": missing_page_or_segment,
        "status": "pass" if missing_source_document == 0 and missing_page_or_segment == 0 else "fail",
    }


def ollama_available(url):
    try:
        req = urllib.request.Request(url.replace("/api/embed", "/api/tags"))
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status == 200
    except Exception:
        return False


def get_ollama_embeddings(texts, model, url):
    payload = json.dumps({"model": model, "input": texts}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as response:
        data = json.loads(response.read().decode("utf-8"))
    vectors = data.get("embeddings")
    if not vectors:
        raise RuntimeError("Ollama response did not contain embeddings.")
    return vectors


def read_env_file_values(path: Path):
    values = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in raw_line or raw_line.lstrip().startswith("#"):
            continue
        key, value = raw_line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def env_file_value_has_wrapping_quotes(path: Path, key_name: str):
    """Return whether a specific .env value is wrapped in quotes without exposing it."""
    if not path.exists():
        return False
    target = str(key_name or "").strip()
    if not target:
        return False
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in raw_line or raw_line.lstrip().startswith("#"):
            continue
        key, value = raw_line.split("=", 1)
        if key.strip() != target:
            continue
        stripped = value.strip()
        return len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {"'", '"'}
    return False


def project_local_env_path():
    return PROJECT_LOCAL_ENV_PATH


def simulation_app_config(env_path=None):
    path = Path(env_path) if env_path else project_local_env_path()
    values = read_env_file_values(path)
    timeout_raw = values.get("OPENROUTER_SIMULATION_TIMEOUT_SECONDS", "").strip()
    try:
        timeout_seconds = max(5, int(timeout_raw)) if timeout_raw else DEFAULT_OPENROUTER_TIMEOUT_SECONDS
    except ValueError:
        timeout_seconds = DEFAULT_OPENROUTER_TIMEOUT_SECONDS
    api_url = (values.get("OPENROUTER_SIMULATION_API_URL") or DEFAULT_OPENROUTER_EMBEDDINGS_URL).strip()
    app_name = (values.get("OPENROUTER_SIMULATION_APP_NAME") or "AnythingLLM PDF Parser").strip()
    app_referer = (values.get("OPENROUTER_SIMULATION_HTTP_REFERER") or "http://127.0.0.1:7860/").strip()
    return {
        "status": "loaded" if path.exists() else "not_found",
        "path": str(path),
        "openrouter_api_url": api_url or DEFAULT_OPENROUTER_EMBEDDINGS_URL,
        "openrouter_timeout_seconds": timeout_seconds,
        "openrouter_zdr": str(values.get("OPENROUTER_SIMULATION_ZDR", "")).strip().casefold() in {"1", "true", "yes", "on"},
        "openrouter_configured": bool((values.get("OPENROUTER_API_KEY") or "").strip()),
        "openrouter_app_name": app_name,
        "openrouter_http_referer": app_referer,
    }


def simulation_app_secret(path: Path, key_name: str):
    values = read_env_file_values(path)
    return (values.get(key_name) or "").strip()


def anythingllm_storage_secret(storage_dir: Path, key_name: str):
    values = read_env_file_values(Path(storage_dir) / ".env")
    return (values.get(key_name) or "").strip()


def anythingllm_provider_model_preferences(values):
    preferences = {}
    for provider, keys in ANYTHINGLLM_PROVIDER_MODEL_KEYS.items():
        specific_keys = [key for key in keys if key != "EMBEDDING_MODEL_PREF"]
        for key in specific_keys:
            model = (values.get(key) or "").strip()
            if model:
                preferences.setdefault(provider, [])
                preferences[provider].append({"key": key, "value": model})
    return preferences


def anythingllm_llm_config_from_values(values):
    provider = (values.get("LLM_PROVIDER") or "").strip()
    normalized_provider = provider.casefold()
    provider_keys = ANYTHINGLLM_PROVIDER_MODEL_KEYS.get(normalized_provider, [])
    model = ""
    model_key = ""
    for key in provider_keys:
        candidate = (values.get(key) or "").strip()
        if candidate:
            model = candidate
            model_key = key
            break
    if not model:
        fallback_keys = [
            "MODEL_PREF",
            "LLM_MODEL_PREF",
            "OPENROUTER_MODEL_PREF",
            "OLLAMA_MODEL_PREF",
            "GENERIC_OPEN_AI_MODEL_PREF",
            "OPENAI_MODEL_PREF",
            "GEMINI_MODEL_PREF",
            "ANYTHINGLLM_MODEL_PREF",
        ]
        for key in fallback_keys:
            candidate = (values.get(key) or "").strip()
            if candidate:
                model = candidate
                model_key = key
                break
    return {
        "provider": provider,
        "normalized_provider": normalized_provider,
        "model": model,
        "model_key": model_key,
    }


def provider_compatibility_status(engine):
    normalized = (engine or "").strip().casefold()
    if not normalized:
        return "not_configured"
    if normalized in ANYTHINGLLM_LOCALLY_VERIFIED_ENGINES:
        return "locally_verified"
    if normalized in ANYTHINGLLM_SUPPORTED_EMBEDDER_ENGINES:
        return "docs_supported"
    return "unknown_engine"


def looks_like_embedding_model(model_name):
    text = (model_name or "").strip().casefold()
    if not text:
        return False
    embedding_markers = [
        "embed",
        "embedding",
        "bge",
        "e5",
        "nomic",
        "voyage",
        "minilm",
        "mpnet",
        "gte",
        "jina",
        "cohere",
        "multilingual",
        "mxbai",
        "qwen3-embedding",
    ]
    generation_markers = [
        "gpt-5",
        "gpt-4",
        "claude",
        "sonnet",
        "opus",
        "deepseek-chat",
        "deepseek-reasoner",
        "llama-3",
        "gemini-1.5",
        "gemini-2.5-pro",
    ]
    if any(marker in text for marker in embedding_markers):
        return True
    if any(marker in text for marker in generation_markers):
        return False
    return False


def anythingllm_effective_model(values, engine):
    normalized_engine = (engine or "").strip().casefold()
    generic_model = (values.get("EMBEDDING_MODEL_PREF") or "").strip()
    all_preferences = anythingllm_provider_model_preferences(values)
    provider_model_value = ""
    provider_model_key = ""
    for entry in all_preferences.get(normalized_engine, []):
        candidate = (entry.get("value") or "").strip()
        if candidate:
            provider_model_value = candidate
            provider_model_key = entry.get("key") or ""
            break
    adjacent = []
    for provider, entries in all_preferences.items():
        for entry in entries:
            adjacent.append(
                {
                    "provider": provider,
                    "key": entry["key"],
                    "value": entry["value"],
                    "matches_engine": provider == normalized_engine,
                }
            )
    deduped = []
    seen = set()
    for entry in adjacent:
        marker = (entry.get("provider", ""), entry.get("key", ""), entry.get("value", ""))
        if marker in seen:
            continue
        seen.add(marker)
        deduped.append(entry)

    effective_model = ""
    effective_key = ""
    if generic_model:
        effective_model = generic_model
        effective_key = "EMBEDDING_MODEL_PREF"
    elif provider_model_value:
        effective_model = provider_model_value
        effective_key = provider_model_key
    return {
        "engine": normalized_engine,
        "effective_model": effective_model,
        "effective_key": effective_key,
        "generic_model": generic_model,
        "provider_model_key": provider_model_key,
        "provider_model_value": provider_model_value,
        "generic_embedding_like": looks_like_embedding_model(generic_model),
        "provider_embedding_like": looks_like_embedding_model(provider_model_value),
        "adjacent_preferences": deduped,
        "all_preferences": all_preferences,
    }


def classify_anythingllm_embedding_config(values):
    engine = (values.get("EMBEDDING_ENGINE") or "").strip()
    normalized_engine = engine.casefold()
    model_info = anythingllm_effective_model(values, engine)
    llm_info = anythingllm_llm_config_from_values(values)
    anomalies = []
    if not normalized_engine:
        anomalies.append("missing_engine")
    elif normalized_engine not in ANYTHINGLLM_SUPPORTED_EMBEDDER_ENGINES:
        anomalies.append("unknown_engine")
    if normalized_engine and not model_info["effective_model"] and normalized_engine not in {"anythingllm", "built-in", "default", "native"}:
        anomalies.append("embedder_model_missing")
    conflicting_model_preferences = []
    if model_info["generic_model"] and model_info["provider_model_value"] and model_info["generic_model"] != model_info["provider_model_value"]:
        conflicting_model_preferences.append(
            {
                "engine": normalized_engine,
                "generic_model": model_info["generic_model"],
                "provider_model_key": model_info["provider_model_key"],
                "provider_model_value": model_info["provider_model_value"],
            }
        )
    if normalized_engine in ANYTHINGLLM_SUPPORTED_EMBEDDER_ENGINES and model_info["effective_model"] and not looks_like_embedding_model(model_info["effective_model"]):
        anomalies.append("embedder_model_not_embedding_like")
    if conflicting_model_preferences:
        anomalies.append("provider_model_pref_differs_from_embedder")
        if model_info["effective_key"] == model_info["provider_model_key"] and model_info["generic_model"]:
            anomalies.append("stale_embedder_model_pref")
    if (
        llm_info["provider"]
        and llm_info["model"]
        and llm_info["normalized_provider"] == normalized_engine
        and model_info["effective_model"]
        and model_info["effective_model"] != llm_info["model"]
        and not looks_like_embedding_model(llm_info["model"])
    ):
        anomalies.append("stale_chat_model_pref")
        anomalies.append("chat_model_separate_from_embedder")
    key_field = ANYTHINGLLM_PROVIDER_KEY_FIELDS.get(normalized_engine, "")
    key_available = bool((values.get(key_field) or "").strip()) if key_field else False
    if normalized_engine == "openrouter" and not key_available:
        anomalies.append("missing_openrouter_api_key")
    status = "not_configured"
    if normalized_engine or model_info["effective_model"]:
        status = "loaded_with_anomalies" if anomalies else "loaded"
    return {
        "status": status,
        "engine": engine,
        "normalized_engine": normalized_engine,
        "model": model_info["effective_model"],
        "effective_model": model_info["effective_model"],
        "effective_model_source": model_info["effective_key"],
        "generic_model": model_info["generic_model"],
        "provider_model_key": model_info["provider_model_key"],
        "provider_model_value": model_info["provider_model_value"],
        "all_model_preferences": model_info["all_preferences"],
        "adjacent_model_preferences": model_info["adjacent_preferences"],
        "conflicting_model_preferences": conflicting_model_preferences,
        "provider_support": provider_compatibility_status(engine),
        "anomalies": anomalies,
        "openrouter_configured": bool((values.get("OPENROUTER_API_KEY") or "").strip()),
        "openrouter_model_preference": (values.get("OPENROUTER_MODEL_PREF") or "").strip(),
        "llm_provider": llm_info["provider"],
        "llm_model": llm_info["model"],
        "llm_model_source": llm_info["model_key"],
    }


def build_ollama_simulation_adapter(model, url):
    capability = resolve_embedder_capability("ollama", model)
    return {
        "provider": "ollama",
        "model": (model or "").strip(),
        "url": (url or "").strip(),
        "batch_size": 4,
        "display_name": capability.get("display_name") or f"Ollama: {(model or '').strip()}",
        "capability": capability,
        "is_available": True,
        "healthcheck": "ollama_http",
        "usage_snapshot": empty_remote_usage(provider="ollama", model=(model or "").strip()),
    }


def build_openrouter_simulation_adapter(model, env_path=None, storage_dir=None, allow_anythingllm_fallback=True, prefer_anythingllm_fallback=False):
    config = simulation_app_config(env_path)
    if not (model or "").strip():
        raise RuntimeError("AnythingLLM is set to OpenRouter, but no embedding model is configured.")
    if not config["openrouter_api_url"].startswith("https://"):
        raise RuntimeError("OpenRouter simulation endpoint must use HTTPS.")
    storage = Path(storage_dir) if storage_dir else default_anythingllm_storage_dir()
    local_key = simulation_app_secret(Path(config["path"]), "OPENROUTER_API_KEY")
    anythingllm_key = anythingllm_storage_secret(storage, "OPENROUTER_API_KEY") if allow_anythingllm_fallback else ""
    if prefer_anythingllm_fallback and anythingllm_key:
        key_source = "anythingllm_fallback"
        key_path = str(storage / ".env")
    elif local_key:
        key_source = "localhost_env"
        key_path = config["path"]
    elif anythingllm_key:
        key_source = "anythingllm_fallback"
        key_path = str(storage / ".env")
    else:
        raise RuntimeError(
            f"OpenRouter simulation is missing OPENROUTER_API_KEY in both {config['path']} and {storage / '.env'}"
        )
    capability = resolve_embedder_capability("openrouter", model)
    return {
        "provider": "openrouter",
        "model": model.strip(),
        "url": config["openrouter_api_url"],
        "batch_size": 4,
        "timeout_seconds": config["openrouter_timeout_seconds"],
        "env_path": key_path,
        "zdr": bool(config["openrouter_zdr"]),
        "app_name": config["openrouter_app_name"],
        "http_referer": config["openrouter_http_referer"],
        "key_source": key_source,
        "storage_dir": str(storage),
        "display_name": capability.get("display_name") or f"OpenRouter: {model.strip()}",
        "capability": capability,
        "is_available": True,
        "healthcheck": "openrouter_https",
        "usage_snapshot": empty_remote_usage(provider="openrouter", model=model.strip()),
    }


def build_anythingllm_runtime_simulation_adapter(storage_dir=None, api_url=DEFAULT_ANYTHINGLLM_API_URL, api_key=None):
    storage = Path(storage_dir) if storage_dir else default_anythingllm_storage_dir()
    embed = anythingllm_embedding_config(storage)
    provider = (embed.get("normalized_engine") or embed.get("engine") or "").strip().casefold()
    model = (embed.get("effective_model") or embed.get("model") or "").strip()
    capability = resolve_embedder_capability(provider, model)
    normalized_api_url = (api_url or DEFAULT_ANYTHINGLLM_API_URL).strip().rstrip("/")
    runtime_key, key_source = resolve_anythingllm_api_key(normalized_api_url, api_key, storage)
    temporary_key_id = ""
    if not runtime_key and is_local_anythingllm_url(normalized_api_url):
        temporary_key = create_temporary_desktop_api_key(normalized_api_url)
        if temporary_key.get("status") == "created":
            runtime_key = temporary_key.get("secret") or ""
            temporary_key_id = temporary_key.get("id") or ""
            key_source = "temporary_desktop_api_key"
        else:
            raise RuntimeError(
                temporary_key.get("error")
                or "AnythingLLM temporary Desktop API key could not be created for runtime simulation."
            )
    if not runtime_key:
        raise RuntimeError("AnythingLLM runtime simulation requires the managed local service key or an explicit API key.")
    return {
        "provider": "anythingllm-runtime",
        "model": model,
        "url": normalized_api_url + "/api/v1/openai/embeddings",
        "api_url": normalized_api_url,
        "api_key": runtime_key,
        "temporary_key_id": temporary_key_id,
        "batch_size": 4,
        "timeout_seconds": 45,
        "key_source": key_source,
        "storage_dir": str(storage),
        "display_name": f"AnythingLLM runtime / {capability.get('display_name') or model or provider or 'embedder'}",
        "capability": capability,
        "is_available": True,
        "healthcheck": "anythingllm_runtime_local",
        "usage_snapshot": empty_remote_usage(provider="anythingllm-runtime", model=model),
    }


def describe_simulation_adapter(adapter):
    if not adapter:
        return "not configured"
    provider = (adapter.get("provider") or "unknown").strip()
    model = (adapter.get("model") or "unspecified model").strip() or "unspecified model"
    if provider == "ollama":
        return f"Ollama / {model}"
    if provider == "openrouter":
        return f"OpenRouter / {model}"
    if provider == "anythingllm-runtime":
        return f"AnythingLLM runtime / {model}"
    return f"{provider} / {model}"


def normalize_simulation_adapter(adapter_or_model, url=None):
    if isinstance(adapter_or_model, dict):
        provider = (adapter_or_model.get("provider") or "").strip().casefold()
        model = (adapter_or_model.get("model") or "").strip()
        capability = adapter_or_model.get("capability") or resolve_embedder_capability(provider, model)
        return {
            "provider": provider,
            "model": model,
            "url": (adapter_or_model.get("url") or "").strip(),
            "api_url": (adapter_or_model.get("api_url") or "").strip(),
            "api_key": (adapter_or_model.get("api_key") or "").strip(),
            "temporary_key_id": (adapter_or_model.get("temporary_key_id") or "").strip(),
            "batch_size": int(adapter_or_model.get("batch_size") or 4),
            "timeout_seconds": int(adapter_or_model.get("timeout_seconds") or DEFAULT_OPENROUTER_TIMEOUT_SECONDS),
            "env_path": (adapter_or_model.get("env_path") or "").strip(),
            "zdr": bool(adapter_or_model.get("zdr")),
            "app_name": (adapter_or_model.get("app_name") or "").strip(),
            "http_referer": (adapter_or_model.get("http_referer") or "").strip(),
            "key_source": (adapter_or_model.get("key_source") or "").strip(),
            "storage_dir": (adapter_or_model.get("storage_dir") or "").strip(),
            "display_name": (adapter_or_model.get("display_name") or capability.get("display_name") or "").strip(),
            "capability": capability,
            "is_available": bool(adapter_or_model.get("is_available", True)),
            "healthcheck": (adapter_or_model.get("healthcheck") or "").strip(),
            "usage_snapshot": dict(adapter_or_model.get("usage_snapshot") or empty_remote_usage(provider=provider, model=model)),
        }
    return build_ollama_simulation_adapter(adapter_or_model, url)


def resolve_default_simulation_adapter(storage_dir=None, env_path=None, allow_anythingllm_fallback=True, prefer_anythingllm_fallback=False):
    storage = Path(storage_dir) if storage_dir else default_anythingllm_storage_dir()
    config = anythingllm_embedding_config(storage)
    engine = (config.get("normalized_engine") or config.get("engine") or "").strip().casefold()
    model = (config.get("effective_model") or config.get("model") or "").strip()
    capability = resolve_embedder_capability(engine, model)
    details = {
        "status": "not_configured",
        "engine": engine,
        "model": model,
        "adapter": None,
        "message": "AnythingLLM embedder is not configured.",
        "anomalies": list(config.get("anomalies") or []),
        "conflicting_model_preferences": list(config.get("conflicting_model_preferences") or []),
        "effective_embedder": f"{config.get('engine') or 'not detected'} / {model or 'not detected'}",
    }
    if not engine:
        return details
    if engine == "openrouter":
        if "embedder_model_not_embedding_like" in details["anomalies"]:
            details["status"] = "config_error"
            details["message"] = (
                f"AnythingLLM embedder engine is OpenRouter, but the effective model `{model or 'not detected'}` does not look like an embedding model. "
                "Fix the OpenRouter embedder preference in AnythingLLM before using the default simulation option."
            )
            return details
        details["status"] = "ready"
        details["adapter"] = build_openrouter_simulation_adapter(
            model,
            env_path=env_path,
            storage_dir=storage,
            allow_anythingllm_fallback=allow_anythingllm_fallback,
            prefer_anythingllm_fallback=prefer_anythingllm_fallback,
        )
        details["message"] = f"Retrieval simulation will use OpenRouter model: {model}"
        return details
    if engine == "ollama":
        details["status"] = "ready"
        details["adapter"] = build_ollama_simulation_adapter(
            model,
            os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/") + "/api/embed",
        )
        details["message"] = f"Retrieval simulation will use Ollama model: {model}"
        return details
    if engine in {"anythingllm", "native", "built-in", "default"}:
        try:
            details["status"] = "ready"
            details["adapter"] = build_anythingllm_runtime_simulation_adapter(storage_dir=storage)
            details["message"] = (
                f"Retrieval simulation will use the live AnythingLLM runtime embedder: {model or 'native embedder'}."
            )
            return details
        except Exception as exc:
            details["status"] = "manual_local_only"
            details["message"] = (
                f"AnythingLLM default embedder is {engine or 'unspecified'} / {model or 'unspecified model'}. "
                f"Recommended AnythingLLM embedder limit: {capability.get('recommended_anythingllm_limit')}. "
                f"{capability.get('source_note')} "
                f"Runtime simulation setup failed: {exc}"
            )
            return details
    if engine in ANYTHINGLLM_CLOUD_ONLY_UNSUPPORTED_SIMULATION_ENGINES:
        try:
            details["status"] = "ready"
            details["adapter"] = build_anythingllm_runtime_simulation_adapter(storage_dir=storage)
            details["message"] = (
                f"Retrieval simulation will use the live AnythingLLM runtime embedder: {engine or 'unspecified'} / {model or 'unspecified model'}."
            )
            return details
        except Exception as exc:
            details["status"] = "unsupported_cloud"
            details["message"] = (
                f"AnythingLLM default embedder is {engine or 'unspecified'} / {model or 'unspecified model'}. "
                f"Recommended AnythingLLM embedder limit: {capability.get('recommended_anythingllm_limit')}. "
                f"{capability.get('source_note')} "
                f"Runtime simulation setup failed: {exc}"
            )
            return details
    details["status"] = "manual_local_only"
    details["message"] = (
        f"AnythingLLM default embedder is {engine or 'unspecified'} / {model or 'unspecified model'}. "
        "Use an explicit Ollama selection below if you want a local retrieval simulation."
    )
    return details


def openrouter_available(adapter):
    normalized = normalize_simulation_adapter(adapter)
    if normalized.get("provider") != "openrouter":
        return False
    return bool(normalized.get("model") and normalized.get("url", "").startswith("https://") and normalized.get("env_path"))


def anythingllm_runtime_available(adapter):
    normalized = normalize_simulation_adapter(adapter)
    if normalized.get("provider") != "anythingllm-runtime":
        return False
    return bool(normalized.get("model") and normalized.get("url", "").startswith("http") and normalized.get("api_key"))


def empty_remote_usage(provider="", model=""):
    return {
        "provider": provider,
        "model": model,
        "requests": 0,
        "prompt_tokens": 0,
        "total_tokens": 0,
        "cost": 0.0,
        "timeout_seconds": 0,
        "key_source": "",
        "usage_missing_responses": 0,
        "embedding_missing_responses": 0,
        "slow_requests": 0,
        "latency_ms_total": 0,
        "latency_ms_max": 0,
        "anomalies": [],
    }


def merge_remote_usage(total, usage, default_provider="", default_model=""):
    accumulator = dict(total or {})
    incoming = dict(usage or {})
    accumulator["provider"] = incoming.get("provider") or accumulator.get("provider") or default_provider
    accumulator["model"] = incoming.get("model") or accumulator.get("model") or default_model
    accumulator["timeout_seconds"] = int(incoming.get("timeout_seconds") or accumulator.get("timeout_seconds") or 0)
    accumulator["key_source"] = incoming.get("key_source") or accumulator.get("key_source") or ""
    for key in ("requests", "prompt_tokens", "total_tokens"):
        accumulator[key] = int(accumulator.get(key) or 0) + int(incoming.get(key) or 0)
    for key in ("usage_missing_responses", "embedding_missing_responses", "slow_requests", "latency_ms_total"):
        accumulator[key] = int(accumulator.get(key) or 0) + int(incoming.get(key) or 0)
    accumulator["latency_ms_max"] = max(int(accumulator.get("latency_ms_max") or 0), int(incoming.get("latency_ms_max") or 0))
    accumulator["cost"] = round(float(accumulator.get("cost") or 0.0) + float(incoming.get("cost") or 0.0), 10)
    accumulator["anomalies"] = sorted(set((accumulator.get("anomalies") or []) + (incoming.get("anomalies") or [])))
    return accumulator


def get_openrouter_embedding_response(texts, adapter):
    normalized = normalize_simulation_adapter(adapter)
    provider = normalized.get("provider") or "openrouter"
    model = normalized.get("model") or ""
    usage = empty_remote_usage(provider=provider, model=model)
    env_path = Path(normalized.get("env_path") or project_local_env_path())
    api_key = simulation_app_secret(env_path, "OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError(f"Localhost app secret file is missing OPENROUTER_API_KEY: {env_path}")
    payload = {
        "model": model,
        "input": texts,
    }
    if normalized.get("zdr"):
        payload["provider"] = {"zdr": True}
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    app_name = (normalized.get("app_name") or "").strip()
    http_referer = (normalized.get("http_referer") or "").strip()
    if app_name:
        headers["X-Title"] = app_name
    if http_referer:
        headers["HTTP-Referer"] = http_referer
    req = urllib.request.Request(
        normalized["url"],
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
    )
    started = time.time()
    with urllib.request.urlopen(req, timeout=int(normalized.get("timeout_seconds") or DEFAULT_OPENROUTER_TIMEOUT_SECONDS)) as response:
        data = json.loads(response.read().decode("utf-8", errors="replace"))
    elapsed_ms = int((time.time() - started) * 1000)
    vectors = data.get("embeddings")
    if not vectors and isinstance(data.get("data"), list):
        vectors = [row.get("embedding") for row in data["data"] if row.get("embedding")]
    if not vectors:
        usage["embedding_missing_responses"] = 1
        usage["anomalies"] = ["missing_embeddings"]
        raise RuntimeError("OpenRouter response did not contain embeddings.")
    raw_usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    usage["requests"] = 1
    usage["prompt_tokens"] = int(raw_usage.get("prompt_tokens") or 0)
    usage["total_tokens"] = int(raw_usage.get("total_tokens") or 0)
    usage["cost"] = round(float(raw_usage.get("cost") or 0.0), 10)
    usage["timeout_seconds"] = int(normalized.get("timeout_seconds") or DEFAULT_OPENROUTER_TIMEOUT_SECONDS)
    usage["key_source"] = normalized.get("key_source") or ""
    usage["latency_ms_total"] = elapsed_ms
    usage["latency_ms_max"] = elapsed_ms
    usage["slow_requests"] = 1 if elapsed_ms >= OPENROUTER_SLOW_REQUEST_THRESHOLD_MS else 0
    usage["anomalies"] = []
    if not isinstance(data.get("usage"), dict):
        usage["usage_missing_responses"] = 1
        usage["anomalies"].append("missing_usage")
    return vectors, usage


def get_openrouter_embeddings(texts, adapter):
    vectors, _usage = get_openrouter_embedding_response(texts, adapter)
    return vectors


def simulation_adapter_available(adapter):
    normalized = normalize_simulation_adapter(adapter)
    provider = normalized.get("provider")
    if provider == "ollama":
        return ollama_available(normalized.get("url", ""))
    if provider == "openrouter":
        return openrouter_available(normalized)
    if provider == "anythingllm-runtime":
        return anythingllm_runtime_available(normalized)
    return False


def release_simulation_adapter(adapter):
    normalized = normalize_simulation_adapter(adapter)
    if normalized.get("provider") == "anythingllm-runtime" and normalized.get("temporary_key_id"):
        cleanup_temporary_desktop_api_key(
            normalized.get("api_url") or DEFAULT_ANYTHINGLLM_API_URL,
            normalized.get("temporary_key_id"),
        )


def get_anythingllm_runtime_embedding_response(texts, adapter):
    normalized = normalize_simulation_adapter(adapter)
    provider = normalized.get("provider") or "anythingllm-runtime"
    model = normalized.get("model") or ""
    usage = empty_remote_usage(provider=provider, model=model)
    response = post_json_captured(
        normalized.get("url"),
        {"input": list(texts or [])},
        api_key=normalized.get("api_key") or None,
        timeout_label="AnythingLLM runtime simulation",
    )
    usage["requests"] = 1
    usage["key_source"] = normalized.get("key_source") or ""
    usage["timeout_seconds"] = int(normalized.get("timeout_seconds") or 45)
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    embeddings = data.get("data") if isinstance(data, dict) else None
    if response.get("http_status") and 200 <= int(response["http_status"]) < 300 and isinstance(embeddings, list):
        vectors = []
        for row in embeddings:
            embedding = row.get("embedding") if isinstance(row, dict) else None
            if not isinstance(embedding, list):
                usage["embedding_missing_responses"] += 1
                usage["anomalies"].append("missing_embeddings")
                raise RuntimeError("AnythingLLM runtime simulation returned an item without an embedding vector.")
            vectors.append(embedding)
        runtime_usage = data.get("usage") if isinstance(data, dict) else {}
        if isinstance(runtime_usage, dict):
            usage["prompt_tokens"] = int(runtime_usage.get("prompt_tokens") or 0)
            usage["total_tokens"] = int(runtime_usage.get("total_tokens") or 0)
        else:
            usage["usage_missing_responses"] = 1
            usage["anomalies"].append("missing_usage")
        return vectors, usage
    status = response.get("http_status")
    error_text = response.get("error") or json.dumps(data)[:500]
    if status:
        raise RuntimeError(f"AnythingLLM runtime HTTP {status}: {error_text}")
    raise RuntimeError(error_text or "AnythingLLM runtime simulation failed.")


def get_embeddings_with_adapter(texts, adapter):
    normalized = normalize_simulation_adapter(adapter)
    provider = normalized.get("provider")
    if provider == "ollama":
        return get_ollama_embeddings(texts, normalized["model"], normalized["url"])
    if provider == "openrouter":
        return get_openrouter_embeddings(texts, normalized)
    if provider == "anythingllm-runtime":
        vectors, _usage = get_anythingllm_runtime_embedding_response(texts, normalized)
        return vectors
    raise RuntimeError(f"Unsupported simulation embedder provider: {provider or 'unspecified'}")


def get_embeddings_with_adapter_response(texts, adapter):
    normalized = normalize_simulation_adapter(adapter)
    provider = normalized.get("provider")
    if provider == "ollama":
        return get_ollama_embeddings(texts, normalized["model"], normalized["url"]), empty_remote_usage(provider=provider, model=normalized.get("model", ""))
    if provider == "openrouter":
        return get_openrouter_embedding_response(texts, normalized)
    if provider == "anythingllm-runtime":
        return get_anythingllm_runtime_embedding_response(texts, normalized)
    raise RuntimeError(f"Unsupported simulation embedder provider: {provider or 'unspecified'}")


def vector_eval_status_for_exception(exc, adapter):
    provider = normalize_simulation_adapter(adapter).get("provider") or "unknown"
    if isinstance(exc, urllib.error.HTTPError):
        status = int(getattr(exc, "code", 0) or 0)
        labels = {
            400: "request_rejected",
            401: "authentication",
            402: "billing",
            403: "permission",
            429: "rate_limited",
            502: "provider_unavailable",
            503: "provider_overloaded",
        }
        return f"error_{provider}_{labels.get(status, f'http_{status}')}"
    if isinstance(exc, urllib.error.URLError):
        return f"error_{provider}_network"
    text = str(exc).casefold()
    if any(
        token in text
        for token in [
            "maximum context length",
            "input too large",
            "too many tokens",
            "context window",
            "token limit",
            "exceeds",
        ]
    ):
        return f"error_{provider}_embedder_limit"
    if "timed out" in text or "timeout" in text:
        return f"error_{provider}_timeout"
    return f"error_{provider}_runtime"


def vector_eval_error_detail(exc, adapter):
    provider = normalize_simulation_adapter(adapter).get("provider") or "unknown"
    if isinstance(exc, urllib.error.HTTPError):
        return f"{provider} HTTP {int(getattr(exc, 'code', 0) or 0)}: {getattr(exc, 'reason', 'request failed')}"
    if isinstance(exc, urllib.error.URLError):
        return f"{provider} network error: {getattr(exc, 'reason', str(exc))}"
    message = re.sub(r"Bearer\s+[A-Za-z0-9._\-]+", "Bearer [redacted]", str(exc))
    return f"{provider} runtime error: {message[:400]}"


def cosine(a, b):
    dot = sum(float(x) * float(y) for x, y in zip(a, b))
    na = math.sqrt(sum(float(x) * float(x) for x in a))
    nb = math.sqrt(sum(float(y) * float(y) for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def select_vector_eval_rows(segments, probes, max_segments):
    try:
        max_segments = int(max_segments or 0)
    except (TypeError, ValueError):
        max_segments = 0
    if max_segments <= 0:
        return list(segments)
    if len(segments) <= max_segments:
        return list(segments)
    required_ids = {probe.get("expected_segment_id") for probe in probes if probe.get("expected_segment_id")}
    required = [row for row in segments if row.get("segment_id") in required_ids]
    required_keys = {row.get("retrieval_unit_id") or id(row) for row in required}
    remaining = [
        row
        for row in segments
        if (row.get("retrieval_unit_id") or id(row)) not in required_keys
    ]
    slots = max(0, max_segments - len(required))
    if slots and remaining:
        step = len(remaining) / slots
        sampled = [remaining[min(len(remaining) - 1, int(index * step))] for index in range(slots)]
    else:
        sampled = []
    return (required + sampled)[:max_segments]


def vector_eval(segments, probes, adapter_or_model, url=None, max_segments=300, progress_callback=None):
    adapter = normalize_simulation_adapter(adapter_or_model, url)
    try:
        if not simulation_adapter_available(adapter):
            provider = adapter.get("provider") or "unknown"
            return [], f"skipped_{provider}_unavailable", f"{provider} simulation adapter was not available.", empty_remote_usage(provider=provider, model=adapter.get("model", ""))
        rows = select_vector_eval_rows(segments, probes, max_segments)
        texts = [row["text"] for row in rows]
        embeddings = []
        remote_usage = empty_remote_usage(provider=adapter.get("provider", ""), model=adapter.get("model", ""))
        batch_size = max(1, int(adapter.get("batch_size") or 4))
        total_chunks = len(texts)
        total_probes = len(probes)
        total_units = max(1, total_chunks + total_probes)
        completed_chunks = 0
        for i in range(0, len(texts), batch_size):
            try:
                batch_texts = texts[i : i + batch_size]
                batch_vectors, batch_usage = get_embeddings_with_adapter_response(batch_texts, adapter)
                embeddings.extend(batch_vectors)
                remote_usage = merge_remote_usage(remote_usage, batch_usage, default_provider=adapter.get("provider", ""), default_model=adapter.get("model", ""))
                completed_chunks += len(batch_texts)
                if callable(progress_callback):
                    progress_callback(
                        completed_chunks,
                        total_units,
                        f"Embedding retrieval chunks {completed_chunks} of {total_chunks}" if total_chunks > 1 else "Embedding retrieval chunk",
                    )
            except Exception as exc:
                return [], vector_eval_status_for_exception(exc, adapter), vector_eval_error_detail(exc, adapter), remote_usage
        results = []
        try:
            for probe_index, probe in enumerate(probes, start=1):
                if callable(progress_callback):
                    progress_callback(
                        completed_chunks + probe_index - 1,
                        total_units,
                        f"Scoring retrieval probes {probe_index} of {total_probes}" if total_probes > 1 else "Scoring retrieval probe",
                    )
                query_vectors, query_usage = get_embeddings_with_adapter_response([probe["query"]], adapter)
                remote_usage = merge_remote_usage(remote_usage, query_usage, default_provider=adapter.get("provider", ""), default_model=adapter.get("model", ""))
                qvec = query_vectors[0]
                ranked = []
                for row, emb in zip(rows, embeddings):
                    ranked.append((cosine(qvec, emb), row))
                ranked.sort(key=lambda x: x[0], reverse=True)
                top10 = ranked[:10]
                top3 = top10[:3]
                expected_id = probe["expected_segment_id"]
                top_ids = [row["segment_id"] for _, row in top10]
                if expected_id in top_ids[:3]:
                    status = "pass"
                elif expected_id in top_ids:
                    status = "review"
                elif probe["kind"] == "page_targeted":
                    expected_page = int(probe["expected_pdf_page"])
                    status = "pass" if any(int(row["pdf_page"]) == expected_page for _, row in top3) else "fail"
                elif probe["kind"] in {"concept", "chapter_targeted"}:
                    expected_page = int(probe["expected_pdf_page"])
                    expected_chapter = (probe.get("chapter") or "").casefold()
                    if expected_chapter and any((row.get("chapter") or "").casefold() == expected_chapter for _, row in top10):
                        status = "pass"
                    elif any(abs(int(row["pdf_page"]) - expected_page) <= 3 for _, row in top10):
                        status = "review"
                    else:
                        status = "fail"
                else:
                    status = "fail"
                results.append(
                    {
                        **probe,
                        "status": status,
                        "top1_score": round(top10[0][0], 6) if top10 else 0,
                        "top1_segment_id": top10[0][1]["segment_id"] if top10 else "",
                        "top1_pdf_page": top10[0][1]["pdf_page"] if top10 else "",
                        "top10_segment_ids": " | ".join(top_ids),
                    }
                )
                if callable(progress_callback):
                    progress_callback(
                        completed_chunks + probe_index,
                        total_units,
                        f"Scoring retrieval probes {probe_index} of {total_probes}" if total_probes > 1 else "Scoring retrieval probe",
                    )
        except Exception as exc:
            return [], vector_eval_status_for_exception(exc, adapter), vector_eval_error_detail(exc, adapter), remote_usage
        return results, "complete", "", remote_usage
    finally:
        release_simulation_adapter(adapter)


def score_candidate(candidate):
    score = 0
    reasons = []
    q = candidate["quality"]
    chunk_eval = candidate["chunk_eval"]
    literal_rows = candidate["literal_results"]
    vector_rows = candidate.get("vector_results") or []
    native_chunk_eval = candidate.get("native_chunk_eval") or {}
    if q["included_words"] >= 8000:
        score += 25
    else:
        reasons.append("low_word_count")
    if q["replacement_chars"] == 0:
        score += 10
    else:
        reasons.append("replacement_chars")
    if q["index_like_pages"] == 0 and q["bibliography_like_pages"] == 0:
        score += 12
    else:
        reasons.append("possible_end_matter_included")
    if chunk_eval["suspicious_chunks"] == 0:
        score += 5
    else:
        reasons.append("suspicious_chunks")
    marker_stats = candidate.get("marker_stats") or {}
    if marker_stats.get("marker_char_ratio", 0) <= 0.15:
        score += 1
    else:
        reasons.append("high_marker_ratio")
    if q.get("scanned_likelihood") == "low":
        score += 8
    else:
        reasons.append(f"scanned_likelihood_{q.get('scanned_likelihood', 'unknown')}")
    if float(q.get("ocr_layout_artifact_ratio") or 0) >= 0.005:
        # A small count is expected in source material.  At this density the
        # candidate visibly contains extractor noise and must not win merely
        # because that noise inflated its word count.
        score -= 20
        reasons.append("high_ocr_layout_artifact_ratio")
    if int(q.get("duplicate_pages") or 0) == 0:
        score += 5
    else:
        reasons.append("duplicate_pages")
    if native_chunk_eval.get("status") == "pass":
        score += 8
    else:
        reasons.append("native_header_chunk_metadata_missing")
    literal_failures = sum(1 for row in literal_rows if row["status"] == "fail")
    if literal_failures == 0:
        score += 20
    else:
        reasons.append(f"literal_failures_{literal_failures}")
    if vector_rows:
        exact_failures = sum(1 for row in vector_rows if row["kind"] == "exact_phrase" and row["status"] == "fail")
        if exact_failures == 0:
            score += 10
        else:
            reasons.append(f"vector_exact_failures_{exact_failures}")
    if candidate["backend"] == "pymupdf":
        score += 3
    if candidate["backend"] == "unstructured":
        score -= 4
    return score, reasons


def inspect_anythingllm_storage(storage_dir: Path, progress_callback=None):
    result = {
        "storage_dir": str(storage_dir),
        "exists": storage_dir.exists(),
        "lancedb_exists": (storage_dir / "lancedb").exists(),
        "documents_exists": (storage_dir / "documents").exists(),
        "vector_cache_exists": (storage_dir / "vector-cache").exists(),
        "sqlite_exists": (storage_dir / "anythingllm.db").exists(),
        "status": "not_inspected",
        "tables": [],
        "error": "",
    }
    if not result["exists"]:
        result["status"] = "missing_storage_dir"
        return result
    try:
        import lancedb
    except ImportError:
        result["status"] = "missing_lancedb_python_package"
        return result
    try:
        db = lancedb.connect(str(storage_dir / "lancedb"))
        tables = []
        table_names = lancedb_table_names(db)
        total_tables = len(table_names)
        for table_index, table_name in enumerate(table_names, start=1):
            if callable(progress_callback):
                progress_callback(table_index - 1, total_tables, table_name, "started")
            entry = {"name": table_name}
            try:
                table = db.open_table(table_name)
                entry["row_count"] = int(table.count_rows())
                schema = table.schema
                entry["columns"] = [field.name for field in schema]
                sample_cols = [
                    column
                    for column in ["id", "title", "docAuthor", "description", "docSource", "chunkSource", "published", "text"]
                    if column in entry["columns"]
                ]
                if sample_cols:
                    # LanceDB's distributed type stub omits ``head`` even
                    # though it is the supported runtime API. Keep the small
                    # compatibility boundary explicit instead of treating the
                    # entire table object as untyped.
                    table_head = getattr(table, "head")
                    sample_df = table_head(3).to_pandas()
                    entry["sample"] = sample_df[sample_cols].to_dict(orient="records")
                else:
                    entry["sample"] = []
            except Exception as exc:
                entry["error"] = str(exc)
            tables.append(entry)
            if callable(progress_callback):
                progress_callback(table_index, total_tables, table_name, "completed")
        result["tables"] = tables
        result["status"] = "complete"
    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)
    return result


def compare_storage_snapshots(before, after):
    before_tables = {str(row.get("name")): row for row in before.get("tables", [])}
    after_tables = {str(row.get("name")): row for row in after.get("tables", [])}
    rows = []
    for name in sorted(set(before_tables) | set(after_tables)):
        before_row = before_tables.get(name, {})
        after_row = after_tables.get(name, {})
        before_count = int(before_row.get("row_count") or 0)
        after_count = int(after_row.get("row_count") or 0)
        rows.append(
            {
                "table": name,
                "before_rows": before_count,
                "after_rows": after_count,
                "added_rows": after_count - before_count,
                "before_columns": ", ".join(before_row.get("columns") or []),
                "after_columns": ", ".join(after_row.get("columns") or []),
            }
        )
    return {
        "before_status": before.get("status"),
        "after_status": after.get("status"),
        "total_added_rows": sum(max(0, row["added_rows"]) for row in rows),
        "rows": rows,
    }


def finalize_batch_inspection_context(context, storage_dir: Path, output_dir: Path, progress_callback=None):
    """Perform the one mutable storage audit deferred by a shared PDF batch.

    This deliberately runs once after the last document. Per-document upload
    confirmation remains the targeted verifier; this report is a batch-level
    before/after diagnostic, not evidence for any individual submission.
    """
    if not isinstance(context, dict) or not context.get("needs_final_storage_audit"):
        return {"status": "not_required", "output": ""}
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    before = dict((context.get("global_read_only") or {}).get("storage_report") or {})
    after = inspect_anythingllm_storage(Path(storage_dir), progress_callback=progress_callback)
    comparison = compare_storage_snapshots(before, after)
    report = {
        "status": "complete",
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "inspection_scope": "one_batch_final_storage_audit",
        "before": before,
        "after": after,
        "comparison": comparison,
        "per_document_packages": list(context.get("inspection_dirs") or []),
    }
    write_json(output_dir / "batch-anythingllm-storage-audit.json", report)
    write_csv(output_dir / "batch-anythingllm-storage-diff.csv", comparison.get("rows") or [])
    context["needs_final_storage_audit"] = False
    context["batch_final_storage_audit"] = report
    return {"status": "complete", "output": str(output_dir / "batch-anythingllm-storage-audit.json"), **report}


def default_anythingllm_storage_dir():
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "anythingllm-desktop" / "storage"
    return Path.home() / "AppData" / "Roaming" / "anythingllm-desktop" / "storage"


def resolve_anythingllm_api_key(api_url, api_key=None, storage_dir=None):
    """Resolve the one managed localhost credential without exposing its secret.

    AnythingLLM Desktop Developer API keys are instance-wide: their table has
    no workspace or permission-scope columns. A named managed key therefore
    covers this app's workspace, schema, upload, retrieval, and runtime
    embedding operations. Explicit keys still take precedence and non-local
    targets never read Desktop storage.
    """
    explicit = str(api_key or "").strip()
    if explicit:
        return explicit, "provided_api_key"
    if not is_local_anythingllm_url(api_url):
        return "", "none"
    storage = Path(storage_dir) if storage_dir else default_anythingllm_storage_dir()
    db_path = storage / "anythingllm.db"
    if not db_path.exists():
        return "", "none"
    con = None
    try:
        con = sqlite_readonly_connection(db_path)
        row = con.execute(
            "select secret from api_keys where name = ? order by id desc limit 1",
            (LOCAL_DESKTOP_SERVICE_API_KEY_NAME,),
        ).fetchone()
        secret = str(row[0] or "").strip() if row else ""
        return (secret, "managed_local_service_key") if secret else ("", "none")
    except (OSError, sqlite3.Error):
        return "", "none"
    finally:
        if con is not None:
            con.close()


def sqlite_readonly_connection(db_path: Path, timeout=0.25):
    """Open a strictly read-only SQLite connection with a short busy budget."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=max(0.01, float(timeout)))
    con.execute(f"pragma busy_timeout = {max(1, int(float(timeout) * 1000))}")
    return con


def anythingllm_chunk_settings(storage_dir: Path):
    result = {
        "chunk_size": 1000,
        "chunk_overlap": 20,
        "source": "anythingllm_defaults",
        "status": "defaults",
        "error": "",
    }
    db_path = Path(storage_dir) / "anythingllm.db"
    if not db_path.exists():
        result["status"] = "database_missing"
        return result
    con = None
    try:
        con = sqlite_readonly_connection(db_path)
        rows = dict(
            con.execute(
                "select label,value from system_settings where label in (?,?)",
                ("text_splitter_chunk_size", "text_splitter_chunk_overlap"),
            ).fetchall()
        )
        con.close()
        if rows.get("text_splitter_chunk_size"):
            result["chunk_size"] = max(100, int(rows["text_splitter_chunk_size"]))
        if rows.get("text_splitter_chunk_overlap"):
            result["chunk_overlap"] = max(0, int(rows["text_splitter_chunk_overlap"]))
        if result["chunk_overlap"] >= result["chunk_size"]:
            result["chunk_overlap"] = max(0, result["chunk_size"] // 10)
            result["status"] = "invalid_overlap_corrected"
        else:
            result["status"] = "loaded"
        result["source"] = "anythingllm_sqlite_read_only"
    except Exception as exc:
        result["status"] = "read_error"
        result["error"] = str(exc)
    return result


def anythingllm_embedding_config(storage_dir: Path):
    result = {
        "status": "not_found",
        "engine": "",
        "model": "",
        "effective_model": "",
        "effective_model_source": "",
        "generic_model": "",
        "provider_model_key": "",
        "provider_model_value": "",
        "all_model_preferences": {},
        "adjacent_model_preferences": [],
        "conflicting_model_preferences": [],
        "provider_support": "not_configured",
        "anomalies": [],
        "max_chunk_length": "",
        "batch_size": "",
        "source": "anythingllm_env_read_only",
        "error": "",
        "openrouter_configured": False,
        "openrouter_model_preference": "",
        "llm_provider": "",
        "llm_model": "",
        "llm_model_source": "",
    }
    env_path = Path(storage_dir) / ".env"
    if not env_path.exists():
        return result
    try:
        values = read_env_file_values(env_path)
        result.update(classify_anythingllm_embedding_config(values))
        result["max_chunk_length"] = values.get("EMBEDDING_MODEL_MAX_CHUNK_LENGTH", "")
        result["batch_size"] = values.get("OLLAMA_EMBEDDING_BATCH_SIZE", "")
        result["status"] = result.get("status") or ("loaded" if result["engine"] or result["model"] else "not_configured")
    except Exception as exc:
        result["status"] = "read_error"
        result["error"] = str(exc)
    return result


def anythingllm_llm_config(storage_dir: Path):
    result = {
        "status": "not_found",
        "provider": "",
        "normalized_provider": "",
        "model": "",
        "model_source": "",
        "source": "anythingllm_env_read_only",
        "error": "",
    }
    env_path = Path(storage_dir) / ".env"
    if not env_path.exists():
        return result
    try:
        values = read_env_file_values(env_path)
        llm_info = anythingllm_llm_config_from_values(values)
        result.update(
            {
                "status": "loaded" if llm_info["provider"] or llm_info["model"] else "not_configured",
                "provider": llm_info["provider"],
                "normalized_provider": llm_info["normalized_provider"],
                "model": llm_info["model"],
                "model_source": llm_info["model_key"],
            }
        )
    except Exception as exc:
        result["status"] = "read_error"
        result["error"] = str(exc)
    return result


def provider_model_key_for_engine(engine: str):
    normalized_engine = str(engine or "").strip().casefold()
    keys = ANYTHINGLLM_PROVIDER_MODEL_KEYS.get(normalized_engine, [])
    specific = [key for key in keys if key != "EMBEDDING_MODEL_PREF"]
    if specific:
        return specific[0]
    return "EMBEDDING_MODEL_PREF"


def write_anythingllm_env_value(storage_dir: Path, key: str, value):
    env_path = Path(storage_dir) / ".env"
    if not env_path.exists():
        raise FileNotFoundError(f"AnythingLLM .env was not found at {env_path}")
    original = env_path.read_text(encoding="utf-8", errors="replace")
    pattern = re.compile(rf"^(?P<prefix>\s*{re.escape(key)}\s*=\s*)(?P<value>.*)$", re.MULTILINE)
    replacement = f"{key}='{value}'"
    if pattern.search(original):
        updated = pattern.sub(replacement, original, count=1)
    else:
        newline = "" if not original or original.endswith("\n") else "\n"
        updated = original + newline + replacement + "\n"
    env_path.write_text(updated, encoding="utf-8")
    return env_path


def write_anythingllm_sqlite_setting(storage_dir: Path, label: str, value):
    db_path = Path(storage_dir) / "anythingllm.db"
    if not db_path.exists():
        raise FileNotFoundError(f"AnythingLLM SQLite database was not found at {db_path}")
    con = sqlite3.connect(db_path)
    try:
        existing = con.execute("select value from system_settings where label = ?", (label,)).fetchone()
        if existing:
            con.execute("update system_settings set value = ? where label = ?", (str(value), label))
        else:
            con.execute("insert into system_settings(label, value) values (?, ?)", (label, str(value)))
        con.commit()
    finally:
        con.close()
    return db_path


def anythingllm_runtime_verification_status(storage_dir: Path, api_url=DEFAULT_ANYTHINGLLM_API_URL):
    storage = Path(storage_dir)
    env_exists = (storage / ".env").exists()
    db_exists = (storage / "anythingllm.db").exists()
    if env_exists and db_exists:
        probe = verify_anythingllm_runtime_embedder(api_url, storage_dir=storage)
        if probe.get("status") == "pass":
            return {
                "status": "runtime_verified",
                "message": probe.get("message") or "AnythingLLM runtime embedder probe passed.",
                "probe": probe,
            }
        if probe.get("status") in {"skipped_missing_api_url", "authentication_required", "network_error"}:
            return {
                "status": "persisted_but_runtime_unverified",
                "message": probe.get("message") or "Settings were persisted locally, but the live runtime probe did not complete.",
                "probe": probe,
            }
        return {
            "status": "runtime_probe_failed",
            "message": probe.get("message") or "AnythingLLM runtime embedder probe failed.",
            "probe": probe,
        }
    return {
        "status": "runtime_verification_unavailable",
        "message": "AnythingLLM Desktop storage files were not both present for persisted verification.",
    }


def anythingllm_embedder_policy(storage_dir: Path, provider="", model="", authoritative_state=None):
    storage = Path(storage_dir)
    authoritative = (
        dict(authoritative_state)
        if isinstance(authoritative_state, dict)
        else resolve_authoritative_anythingllm_state(storage, runtime_verification={"status": "not_run"})
    )
    embedder_state = authoritative.get("embedder", {})
    chunking_state = authoritative.get("chunking", {})
    engine_field = embedder_state.get("engine") or {}
    model_field = embedder_state.get("model") or {}
    hard_limit_field = embedder_state.get("hard_limit") or {}
    chunk_size_field = chunking_state.get("size") or {}
    chunk_overlap_field = chunking_state.get("overlap") or {}
    resolved_provider = (provider or engine_field.get("effective") or engine_field.get("stored") or "").strip().casefold()
    resolved_model = (model or model_field.get("effective") or "").strip()
    capability = resolve_embedder_capability(resolved_provider, resolved_model)
    try:
        current_limit = int(hard_limit_field.get("stored") or 0)
    except (TypeError, ValueError):
        current_limit = 0
    recommended_limit = int(capability.get("recommended_anythingllm_limit") or UNKNOWN_EMBEDDER_LIMIT)
    if not current_limit:
        status = "unknown"
        action = "raise"
    elif capability.get("status") == "unknown_capability":
        status = "unknown"
        action = "warn_only"
    elif current_limit < recommended_limit:
        status = "too_low"
        action = "raise"
    elif current_limit > recommended_limit:
        status = "too_high"
        action = "lower"
    else:
        status = "ok"
        action = "leave"
    if status in {"too_low", "too_high"} and capability.get("status") != "unknown_capability":
        risk_label = "likely unsafe"
        risk_level = "high"
    elif capability.get("status") == "unknown_capability":
        risk_label = "conservative fallback"
        risk_level = "medium"
    else:
        risk_label = "aligned"
        risk_level = "low"
    return {
        "provider": resolved_provider,
        "model": resolved_model,
        "current_limit": current_limit,
        "recommended_limit": recommended_limit,
        "status": status,
        "action": action,
        "risk_label": risk_label,
        "risk_level": risk_level,
        "capability": capability,
        "chunk_size": int(chunk_size_field.get("effective") or 1000),
        "chunk_overlap": int(chunk_overlap_field.get("effective") or 20),
        "should_auto_correct": capability.get("status") != "unknown_capability" and action in {"raise", "lower"},
        "warning_only": capability.get("status") == "unknown_capability",
        "source": "authoritative_resolver",
    }


def anythingllm_preflight_snapshot(storage_dir: Path, simulation_adapter=None, runtime_verify=True):
    """Read immutable AnythingLLM preflight evidence once for one run.

    Configuration, policy, and runtime presentation used to independently
    reopen the same SQLite/.env state and resolve authoritative settings. A
    run-scoped snapshot keeps those values internally consistent while
    deliberately excluding mutable workspace/vector evidence.
    """
    storage = Path(storage_dir)
    llm = anythingllm_llm_config(storage)
    embed = anythingllm_embedding_config(storage)
    chunk = anythingllm_chunk_settings(storage)
    simulation = normalize_simulation_adapter(simulation_adapter) if simulation_adapter else {}
    runtime = (
        anythingllm_runtime_verification_status(storage)
        if runtime_verify
        else {
            "status": "not_run",
            "message": "Runtime verification was not requested for this read-only rendering path.",
        }
    )
    authoritative = resolve_authoritative_anythingllm_state(
        storage,
        runtime_verification=runtime if runtime.get("status") not in {"", "runtime_verification_unavailable"} else None,
    )
    policy = anythingllm_embedder_policy(storage, authoritative_state=authoritative)
    anomalies = list(embed.get("anomalies") or [])
    if policy.get("status") in {"too_low", "too_high"}:
        anomalies.append("embedder_limit_mismatch")
    if runtime.get("status") == "runtime_verification_unavailable":
        anomalies.append("runtime_verification_unavailable")
    if runtime.get("status") == "runtime_probe_failed":
        anomalies.append("runtime_embedder_probe_failed")
    return {
        "chat_llm": llm,
        "embedder": {
            **embed,
            "capability": policy.get("capability"),
            "policy": policy,
        },
        "chunking": chunk,
        "simulation": simulation,
        "validation": runtime,
        "anomalies": sorted(set(anomalies)),
        "evidence_state": authoritative,
    }


def anythingllm_resolved_state(storage_dir: Path, simulation_adapter=None, runtime_verify=True):
    """Compatibility name for the run-scoped immutable preflight snapshot."""
    return anythingllm_preflight_snapshot(storage_dir, simulation_adapter, runtime_verify)


def should_verify_anythingllm_runtime_during_preparation(args):
    """Whether this preparation still needs a live embedder round trip.

    Local-only preparation does not consume the embedder. The desktop UI
    confirms local availability and upload authentication before constructing a
    run with ``external_preflight_managed``; its continuous runtime guard and
    exact post-upload vector checks remain the live evidence path. Repeating a
    paid network-backed embedding probe inside the first PDF added tens of
    seconds without adding reliable completion evidence. CLI uploads and
    vector evaluations without an external preflight remain verified here.
    """
    if bool(getattr(args, "external_preflight_managed", False)):
        return False
    return bool(
        getattr(args, "prepare_and_upload", False)
        or getattr(args, "run_vector_eval", False)
    )


def persist_anythingllm_chunk_settings(storage_dir: Path, chunk_size, chunk_overlap):
    chunk_db = write_anythingllm_sqlite_setting(storage_dir, "text_splitter_chunk_size", int(chunk_size))
    overlap_db = write_anythingllm_sqlite_setting(storage_dir, "text_splitter_chunk_overlap", int(chunk_overlap))
    persisted = anythingllm_chunk_settings(storage_dir)
    runtime = anythingllm_runtime_verification_status(storage_dir)
    return {
        "status": "persisted",
        "requested": {
            "chunk_size": int(chunk_size),
            "chunk_overlap": int(chunk_overlap),
        },
        "persisted": {
            "chunk_size": int(persisted.get("chunk_size") or 0),
            "chunk_overlap": int(persisted.get("chunk_overlap") or 0),
        },
        "paths": [str(chunk_db), str(overlap_db)],
        "runtime_verification_status": runtime.get("status"),
        "runtime_verification_message": runtime.get("message"),
        "restart_likely_required": False,
        "reembed_required": True,
    }


def persist_anythingllm_embedder_settings(storage_dir: Path, engine, model):
    storage = Path(storage_dir)
    env_path = write_anythingllm_env_value(storage, "EMBEDDING_ENGINE", engine)
    model_key = provider_model_key_for_engine(engine)
    if model:
        write_anythingllm_env_value(storage, model_key, model)
        write_anythingllm_env_value(storage, "EMBEDDING_MODEL_PREF", model)
    persisted = anythingllm_embedding_config(storage)
    runtime = anythingllm_runtime_verification_status(storage)
    return {
        "status": "persisted",
        "requested": {"engine": engine, "model": model},
        "persisted": {
            "engine": persisted.get("engine") or "",
            "model": persisted.get("effective_model") or persisted.get("model") or "",
        },
        "paths": [str(env_path)],
        "runtime_verification_status": runtime.get("status"),
        "runtime_verification_message": runtime.get("message"),
        "restart_likely_required": True,
        "reembed_required": True,
    }


def persist_anythingllm_embedder_limit(storage_dir: Path, limit, trigger="manual", reason="", provider="", model=""):
    storage = Path(storage_dir)
    env_path = write_anythingllm_env_value(storage, "EMBEDDING_MODEL_MAX_CHUNK_LENGTH", int(limit))
    persisted = anythingllm_embedding_config(storage)
    runtime = anythingllm_runtime_verification_status(storage)
    return {
        "status": "persisted",
        "requested": {"limit": int(limit)},
        "persisted": {"limit": int(persisted.get("max_chunk_length") or 0)},
        "paths": [str(env_path)],
        "trigger": trigger,
        "reason": reason,
        "provider": provider,
        "model": model,
        "runtime_verification_status": runtime.get("status"),
        "runtime_verification_message": runtime.get("message"),
        "restart_likely_required": True,
        "reembed_required": True,
    }


def apply_recommended_anythingllm_settings(storage_dir: Path, provider="", model=""):
    storage = Path(storage_dir)
    chunk = anythingllm_chunk_settings(storage)
    embed = anythingllm_embedding_config(storage)
    policy = anythingllm_embedder_policy(storage, provider=provider, model=model)
    runtime_before = anythingllm_runtime_verification_status(storage)
    result = {
        "status": "not_applied",
        "requested": {
            "provider": policy.get("provider") or "",
            "model": policy.get("model") or "",
            "chunk_size": int(chunk.get("chunk_size") or 1000),
            "chunk_overlap": int(chunk.get("chunk_overlap") or 20),
            "embedder_limit": int(policy.get("recommended_limit") or UNKNOWN_EMBEDDER_LIMIT),
        },
        "original": {
            "chunk_size": int(chunk.get("chunk_size") or 1000),
            "chunk_overlap": int(chunk.get("chunk_overlap") or 20),
            "embedder_limit": int(embed.get("max_chunk_length") or 0),
        },
        "policy": policy,
        "write_results": [],
        "runtime_before": runtime_before.get("status"),
        "runtime_after": "",
        "runtime_message": "",
        "message": "",
    }
    write_results = []
    write_results.append(
        persist_anythingllm_chunk_settings(
            storage,
            result["requested"]["chunk_size"],
            result["requested"]["chunk_overlap"],
        )
    )
    write_results.append(
        persist_anythingllm_embedder_limit(
            storage,
            result["requested"]["embedder_limit"],
            trigger="recommended_apply",
            reason=(
                f"Applied recommended settings for {policy.get('provider') or 'unknown'} / "
                f"{policy.get('model') or 'unknown'}"
            ),
            provider=policy.get("provider") or "",
            model=policy.get("model") or "",
        )
    )
    runtime_after = anythingllm_runtime_verification_status(storage)
    result["status"] = "applied"
    result["write_results"] = write_results
    result["runtime_after"] = runtime_after.get("status")
    result["runtime_message"] = runtime_after.get("message") or ""
    result["message"] = (
        f"Applied recommended AnythingLLM settings for {policy.get('provider') or 'unknown'} / "
        f"{policy.get('model') or 'unknown'}: chunk {result['requested']['chunk_size']} / "
        f"{result['requested']['chunk_overlap']}, embedder max chunk {result['requested']['embedder_limit']}."
    )
    return result


def auto_correct_anythingllm_embedder_limit(storage_dir: Path, provider="", model=""):
    policy = anythingllm_embedder_policy(storage_dir, provider=provider, model=model)
    if policy.get("warning_only"):
        return {
            "status": "warning_only",
            "auto_corrected": False,
            "message": "Unknown model capability; used a conservative local fallback and left AnythingLLM limit unchanged.",
            "policy": policy,
            "write_result": None,
        }
    if not policy.get("should_auto_correct"):
        return {
            "status": "no_change",
            "auto_corrected": False,
            "message": "AnythingLLM embedder limit already matches the current model policy.",
            "policy": policy,
            "write_result": None,
        }
    write_result = persist_anythingllm_embedder_limit(
        storage_dir,
        policy["recommended_limit"],
        trigger="auto_policy",
        reason=f"Auto-corrected for {policy['provider']} / {policy['model']}",
        provider=policy["provider"],
        model=policy["model"],
    )
    return {
        "status": "corrected",
        "auto_corrected": True,
        "message": (
            f"Auto-corrected AnythingLLM embedder limit from {policy['current_limit'] or 'unset'} "
            f"to {policy['recommended_limit']} for {policy['provider']} / {policy['model']}."
        ),
        "policy": policy,
        "write_result": write_result,
    }


def simulation_preflight_status_for_exception(exc, adapter):
    provider = normalize_simulation_adapter(adapter).get("provider") or "unknown"
    if isinstance(exc, urllib.error.HTTPError):
        status = int(getattr(exc, "code", 0) or 0)
        labels = {
            400: "SIM-PRE-400",
            401: "SIM-PRE-401",
            402: "SIM-PRE-402",
            403: "SIM-PRE-403",
            408: "SIM-PRE-408",
            413: "SIM-PRE-413",
            422: "SIM-PRE-422",
            429: "SIM-PRE-429",
            500: "SIM-PRE-500",
            502: "SIM-PRE-502",
            503: "SIM-PRE-503",
            504: "SIM-PRE-504",
        }
        return labels.get(status, f"SIM-PRE-HTTP-{status}"), provider
    if isinstance(exc, urllib.error.URLError):
        return "SIM-PRE-NET", provider
    text = str(exc).casefold()
    if any(token in text for token in ["timed out", "timeout"]):
        return "SIM-PRE-TIMEOUT", provider
    if any(
        token in text
        for token in [
            "maximum context length",
            "input too large",
            "too many tokens",
            "context window",
            "too long",
            "token limit",
            "exceeds",
        ]
    ):
        return "SIM-PRE-LIMIT", provider
    if "missing embeddings" in text:
        return "SIM-PRE-EMBED-MISSING", provider
    if "authentication" in text or "api key" in text or "unauthorized" in text:
        return "SIM-PRE-AUTH", provider
    return "SIM-PRE-OTHER", provider


def simulation_preflight(adapter, effective_limit, batch_size=1):
    normalized = normalize_simulation_adapter(adapter)
    provider = normalized.get("provider") or "unknown"
    model = normalized.get("model") or ""
    safe_probe_len = min(512, max(128, int(effective_limit or 4096),))
    boundary_probe_len = min(max(512, int(effective_limit or 4096)), 4096)
    safe_probe = ("safe probe " * ((safe_probe_len // 11) + 2))[:safe_probe_len]
    boundary_probe = ("boundary probe " * ((boundary_probe_len // 15) + 2))[:boundary_probe_len]
    result = {
        "status": "not_run",
        "provider": provider,
        "model": model,
        "effective_limit": int(effective_limit or 0),
        "safe_probe_chars": len(safe_probe),
        "boundary_probe_chars": len(boundary_probe),
        "error_code": "",
        "message": "",
        "usage": empty_remote_usage(provider=provider, model=model),
    }
    try:
        _vectors, usage = get_embeddings_with_adapter_response([safe_probe], normalized)
        result["usage"] = merge_remote_usage(result["usage"], usage, default_provider=provider, default_model=model)
    except Exception as exc:
        code, provider_name = simulation_preflight_status_for_exception(exc, normalized)
        result["status"] = "blocked"
        result["error_code"] = code
        result["message"] = f"{provider_name} simulation preflight failed on the safe probe: {vector_eval_error_detail(exc, normalized)}"
        return result
    if boundary_probe_len <= len(safe_probe):
        result["status"] = "pass"
        result["message"] = f"{provider} simulation preflight passed."
        return result
    try:
        _vectors, usage = get_embeddings_with_adapter_response([boundary_probe] * max(1, int(batch_size or 1)), normalized)
        result["usage"] = merge_remote_usage(result["usage"], usage, default_provider=provider, default_model=model)
        result["status"] = "pass"
        result["message"] = f"{provider} simulation preflight passed."
        return result
    except Exception as exc:
        code, provider_name = simulation_preflight_status_for_exception(exc, normalized)
        result["status"] = "blocked"
        result["error_code"] = code
        if code == "SIM-PRE-LIMIT" or code in {"SIM-PRE-413", "SIM-PRE-422"}:
            result["message"] = (
                f"{provider_name} simulation preflight rejected the planned chunk limit around "
                f"{boundary_probe_len} characters. Use a lower explicit embedder max chunk limit."
            )
        else:
            result["message"] = f"{provider_name} simulation preflight failed near the planned limit: {vector_eval_error_detail(exc, normalized)}"
        return result


def read_workspace_model_gate(storage_dir: Path, workspace_slug="test"):
    result = {
        "status": "not_checked",
        "workspace_slug": workspace_slug,
        "workspace_name": "",
        "chat_provider": "",
        "chat_model": "",
        "top_n": "",
        "similarity_threshold": "",
        "vector_search_mode": "",
        "deepseek_like": False,
        "blocked_terms_present": False,
        "observed_deepseek_workspaces": [],
        "message": "",
        "error": "",
    }
    db_path = storage_dir / "anythingllm.db"
    if not db_path.exists():
        result["status"] = "missing_db"
        result["message"] = "AnythingLLM SQLite database was not found."
        return result
    con = None
    try:
        con = sqlite_readonly_connection(db_path)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        workspaces = [dict(row) for row in cur.execute(
            "select id,name,slug,chatProvider,chatModel,topN,similarityThreshold,vectorSearchMode,chatMode from workspaces order by id"
        )]
        for workspace in workspaces:
            model_text = f"{workspace.get('chatProvider') or ''} {workspace.get('chatModel') or ''}".casefold()
            if "deepseek" in model_text:
                result["observed_deepseek_workspaces"].append(
                    {
                        "name": workspace.get("name"),
                        "slug": workspace.get("slug"),
                        "chatProvider": workspace.get("chatProvider"),
                        "chatModel": workspace.get("chatModel"),
                    }
                )
        target = next((row for row in workspaces if row.get("slug") == workspace_slug), None)
        if not target:
            result["status"] = "workspace_missing"
            result["message"] = f"Workspace `{workspace_slug}` was not found."
            return result
        result.update(
            {
                "workspace_name": target.get("name") or "",
                "chat_provider": target.get("chatProvider") or "",
                "chat_model": target.get("chatModel") or "",
                "top_n": target.get("topN"),
                "similarity_threshold": target.get("similarityThreshold"),
                "vector_search_mode": target.get("vectorSearchMode") or "",
            }
        )
        model_text = f"{result['chat_provider']} {result['chat_model']}".casefold()
        result["deepseek_like"] = "deepseek" in model_text
        result["blocked_terms_present"] = any(term in model_text for term in ["claude", "anthropic", "sonnet"])
        if result["blocked_terms_present"]:
            result["status"] = "blocked_claude_or_anthropic_model"
            result["message"] = f"Workspace `{workspace_slug}` is configured with a blocked model/provider: {model_text.strip() or 'empty'}."
        elif result["deepseek_like"]:
            result["status"] = "pass"
            result["message"] = f"Workspace `{workspace_slug}` is configured with a DeepSeek-like model."
        else:
            suggestion = ""
            if result["observed_deepseek_workspaces"]:
                observed = result["observed_deepseek_workspaces"][0]
                suggestion = f" Observed DeepSeek-like example: workspace `{observed['slug']}` uses `{observed.get('chatModel')}`."
            result["status"] = "blocked_model_not_configured"
            result["message"] = f"Workspace `{workspace_slug}` must be configured in AnythingLLM to a DeepSeek-like model before the inside-AnythingLLM test can count.{suggestion}"
    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)
        result["message"] = "Workspace model gate failed while reading AnythingLLM SQLite data."
    finally:
        try:
            if con:
                con.close()
        except Exception:
            pass
    return result


def read_validation_workspace_template(storage_dir: Path):
    result = {
        "status": "not_checked",
        "source_workspace_slug": "",
        "source_workspace_name": "",
        "chat_provider": "",
        "chat_model": "",
        "top_n": 8,
        "similarity_threshold": 0.25,
        "vector_search_mode": "default",
        "chat_mode": "query",
        "message": "",
        "error": "",
    }
    db_path = storage_dir / "anythingllm.db"
    env_values = read_env_file_values(storage_dir / ".env")
    llm_info = anythingllm_llm_config_from_values(env_values)
    if not db_path.exists():
        if llm_info.get("provider") and llm_info.get("model"):
            result.update(
                {
                    "status": "pass",
                    "source_workspace_slug": "",
                    "source_workspace_name": "AnythingLLM global LLM settings",
                    "chat_provider": llm_info.get("provider") or "",
                    "chat_model": llm_info.get("model") or "",
                    "message": (
                        f"Using AnythingLLM global LLM settings "
                        f"`{llm_info.get('provider')}` / `{llm_info.get('model')}`."
                    ),
                }
            )
            return result
        result["status"] = "missing_db"
        result["message"] = "AnythingLLM SQLite database was not found."
        return result
    try:
        con = sqlite_readonly_connection(db_path)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        workspaces = [dict(row) for row in cur.execute(
            "select id,name,slug,chatProvider,chatModel,topN,similarityThreshold,vectorSearchMode,chatMode from workspaces order by id desc"
        )]
        preferred = None
        fallback = None
        for workspace in workspaces:
            provider = str(workspace.get("chatProvider") or "")
            model = str(workspace.get("chatModel") or "")
            model_text = f"{provider} {model}".casefold()
            if provider or model:
                fallback = fallback or workspace
            if "deepseek" in model_text:
                preferred = workspace
                break
        chosen = preferred or fallback
        if not chosen:
            if llm_info.get("provider") and llm_info.get("model"):
                result.update(
                    {
                        "status": "pass",
                        "source_workspace_slug": "",
                        "source_workspace_name": "AnythingLLM global LLM settings",
                        "chat_provider": llm_info.get("provider") or "",
                        "chat_model": llm_info.get("model") or "",
                        "message": (
                            f"Using AnythingLLM global LLM settings "
                            f"`{llm_info.get('provider')}` / `{llm_info.get('model')}` because "
                            "workspace rows do not carry explicit chat models."
                        ),
                    }
                )
                return result
            result["status"] = "workspace_missing"
            result["message"] = "No AnythingLLM workspace with a configured chat model was found."
            return result
        result.update(
            {
                "status": "pass",
                "source_workspace_slug": chosen.get("slug") or "",
                "source_workspace_name": chosen.get("name") or "",
                "chat_provider": chosen.get("chatProvider") or "",
                "chat_model": chosen.get("chatModel") or "",
                "top_n": int(chosen.get("topN") or 8),
                "similarity_threshold": chosen.get("similarityThreshold") if chosen.get("similarityThreshold") is not None else 0.25,
                "vector_search_mode": chosen.get("vectorSearchMode") or "default",
                "chat_mode": chosen.get("chatMode") or "query",
                "message": (
                    f"Using workspace template `{chosen.get('slug')}` with "
                    f"`{chosen.get('chatProvider')}` / `{chosen.get('chatModel')}`."
                ),
            }
        )
    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)
        result["message"] = "Failed to read a workspace template from AnythingLLM SQLite data."
    finally:
        try:
            con.close()
        except Exception:
            pass
    return result


def update_workspace_runtime_template_sqlite(storage_dir: Path, workspace_slug: str, template: dict):
    result = {
        "status": "not_attempted",
        "write_method": "sqlite",
        "workspace_slug": workspace_slug,
        "verified": False,
        "applied": {},
        "error": "",
        "message": "",
    }
    db_path = storage_dir / "anythingllm.db"
    if not db_path.exists():
        result["status"] = "missing_db"
        result["error"] = f"AnythingLLM SQLite database was not found at {db_path}"
        return result
    con = sqlite3.connect(db_path)
    try:
        cur = con.cursor()
        row = cur.execute(
            "select id from workspaces where slug = ?",
            (workspace_slug,),
        ).fetchone()
        if not row:
            result["status"] = "workspace_missing"
            result["error"] = f"Workspace `{workspace_slug}` was not found in AnythingLLM SQLite data."
            return result
        applied = {
            "chatProvider": str(template.get("chat_provider") or ""),
            "chatModel": str(template.get("chat_model") or ""),
            "topN": int(template.get("top_n") or 8),
            "similarityThreshold": template.get("similarity_threshold") if template.get("similarity_threshold") is not None else 0.25,
            "vectorSearchMode": str(template.get("vector_search_mode") or "default"),
            "chatMode": str(template.get("chat_mode") or "query"),
        }
        cur.execute(
            """
            update workspaces
            set chatProvider = ?, chatModel = ?, topN = ?, similarityThreshold = ?, vectorSearchMode = ?, chatMode = ?
            where slug = ?
            """,
            (
                applied["chatProvider"],
                applied["chatModel"],
                applied["topN"],
                applied["similarityThreshold"],
                applied["vectorSearchMode"],
                applied["chatMode"],
                workspace_slug,
            ),
        )
        con.commit()
        verified = read_workspace_model_gate(storage_dir, workspace_slug)
        result["verified"] = verified.get("chat_provider") == applied["chatProvider"] and verified.get("chat_model") == applied["chatModel"]
        result["status"] = "pass" if result["verified"] else "persisted_but_runtime_unverified"
        result["applied"] = applied
        result["message"] = (
            f"Validation workspace `{workspace_slug}` was seeded from workspace template "
            f"`{template.get('source_workspace_slug') or 'manual-template'}`."
        )
    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)
    finally:
        con.close()
    return result


def default_short_label(title, author):
    author_words = re.findall(r"[A-Za-z][A-Za-z'-]+", author or "")
    if author_words:
        return author_words[-1]
    title_words = [w for w in re.findall(r"[A-Za-z][A-Za-z'-]+", title or "") if w.casefold() not in HEADING_STOPWORDS]
    return title_words[0] if title_words else "PDF"


def post_json(url, body, api_key=None, timeout: float = ANYTHINGLLM_HTTP_RESPONSE_TIMEOUT_SECONDS):
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.status, response.read().decode("utf-8", errors="replace")


def _normalized_anythingllm_document_location(value):
    """Compare Desktop document locations without changing the stored path.

    The Desktop worker emits the relative document path it received.  Windows
    separators and a leading ``./`` are presentation differences only; the
    actual location remains untouched for the update request and ledger.
    """
    return str(value or "").replace("\\", "/").lstrip("./").casefold()


def parse_anythingllm_embed_progress_event(payload):
    """Parse one ``data:`` body from Desktop's ``embed-progress`` SSE feed.

    This is deliberately a small, read-only adapter.  Progress events are not
    acceptance or retrieval proof; the normal exact-vector and retrieval
    checks remain the terminal evidence layers.
    """
    try:
        event = json.loads(str(payload or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(event, dict) or not str(event.get("type") or "").strip():
        return None
    return dict(event)


def anythingllm_embed_progress_message(event):
    """Return a compact user-facing description of a known Desktop event."""
    event_type = str((event or {}).get("type") or "").strip()
    total = int((event or {}).get("totalDocs") or 0)
    index = int((event or {}).get("docIndex") or 0) + 1
    record_position = f"{index}/{total}" if total else f"{index}; total not yet confirmed"
    if event_type == "batch_starting":
        return (
            f"AnythingLLM Desktop queue started ({total} records)"
            if total
            else "AnythingLLM Desktop queue started; record total not yet confirmed"
        )
    if event_type == "doc_starting":
        return f"AnythingLLM Desktop queue: embedding record {record_position}"
    if event_type == "chunk_progress":
        done = int((event or {}).get("chunksProcessed") or 0)
        chunks = int((event or {}).get("totalChunks") or 0)
        chunk_position = f"{done}/{chunks}" if chunks else f"{done}; total not yet confirmed"
        return f"AnythingLLM Desktop queue: record {record_position}, chunks {chunk_position}"
    if event_type == "doc_complete":
        return f"AnythingLLM Desktop queue: completed record {record_position}"
    if event_type == "doc_failed":
        return f"AnythingLLM Desktop queue: record {record_position} needs review"
    if event_type == "file_removed":
        return "AnythingLLM Desktop queue: a queued record was removed"
    if event_type == "all_complete":
        return "AnythingLLM Desktop queue finished; verifying searchable vectors"
    return "AnythingLLM Desktop queue reported an unrecognized progress event"


def _anythingllm_embed_event_matches_locations(event, expected_locations, matched_locations):
    """Return whether an SSE event can be tied to this run's submitted paths."""
    expected = expected_locations or set()
    filenames = []
    filename = (event or {}).get("filename")
    if filename:
        filenames.append(filename)
    filenames.extend((event or {}).get("filenames") or [])
    filenames.extend((event or {}).get("embeddedFiles") or [])
    filenames.extend((event or {}).get("failedFiles") or [])
    normalized = {
        _normalized_anythingllm_document_location(item)
        for item in filenames
        if str(item or "").strip()
    }
    matching = normalized & expected
    if matching:
        matched_locations.update(matching)
        return True
    # ``all_complete`` has no current-file field in older Desktop builds.  It
    # is relevant only after an earlier matching event was seen for this run.
    return bool(matched_locations) and str((event or {}).get("type") or "") == "all_complete"


def listen_for_anythingllm_embed_progress(
    api_url,
    api_key,
    workspace_slug,
    expected_locations,
    stop_event,
    event_callback=None,
    error_callback=None,
    state_callback=None,
    connected_event=None,
    include_unmatched_events=False,
):
    """Read Desktop's SSE queue feed while a single update request is active.

    The feed is observational: failed or unavailable SSE must never fail an
    embedding request.  A short socket timeout lets the daemon listener stop
    promptly after the synchronous update response returns. Once the stream
    has connected, an idle socket timeout is treated as an ordinary polling
    boundary, not as evidence that Desktop's queue is unavailable.
    """
    # Desktop builds have shipped both route mounts.  The ordinary API calls
    # accept ``/api/v1`` on this installation, while the live progress stream
    # is mounted under ``/api``.  Try the documented v1 form first, then make
    # one explicit 404-only fallback instead of treating the stream as absent.
    endpoint_candidates = [
        api_url.rstrip("/") + f"/api/v1/workspace/{workspace_slug}/embed-progress",
        api_url.rstrip("/") + f"/api/workspace/{workspace_slug}/embed-progress",
    ]
    endpoint_index = 0
    expected = {
        _normalized_anythingllm_document_location(location)
        for location in (expected_locations or [])
        if str(location or "").strip()
    }
    matched_locations = set()
    seen_events = set()
    failures = 0
    connected_once = False
    while not stop_event.is_set():
        headers = {"Accept": "text/event-stream", "Cache-Control": "no-cache"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(endpoint_candidates[endpoint_index], headers=headers)
        payload_lines = []
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                failures = 0
                connected_once = True
                if callable(state_callback):
                    state_callback("connected", {"at_monotonic": time.monotonic(), "failures": 0})
                if isinstance(connected_event, threading.Event):
                    connected_event.set()
                for raw_line in response:
                    if stop_event.is_set():
                        return
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                    if line.startswith("data:"):
                        payload_lines.append(line[5:].lstrip())
                        continue
                    if line or not payload_lines:
                        continue
                    event = parse_anythingllm_embed_progress_event("\n".join(payload_lines))
                    payload_lines = []
                    if not event:
                        continue
                    if not include_unmatched_events and not _anythingllm_embed_event_matches_locations(
                        event, expected, matched_locations
                    ):
                        continue
                    event_key = json.dumps(event, sort_keys=True, default=str)
                    if event_key in seen_events:
                        continue
                    seen_events.add(event_key)
                    if callable(event_callback):
                        event_callback(event)
            # A healthy Desktop stream stays open until the client closes it.
            # Treat a clean EOF as a transient disconnect, rather than a
            # zero-delay reconnect loop when Desktop is restarting. Do not
            # retire the observer after two disconnects: a Desktop restart
            # can occur while the app-owned queue remains active, and later
            # SSE events are valuable progress evidence after it recovers.
            if not stop_event.is_set():
                if callable(state_callback):
                    state_callback("reconnecting", {"at_monotonic": time.monotonic(), "reason": "stream_eof", "failures": failures})
                if not connected_once:
                    failures += 1
                stop_event.wait(0.75 if connected_once else min(5.0, 0.25 * (2 ** min(failures, 4))))
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and endpoint_index + 1 < len(endpoint_candidates):
                endpoint_index += 1
                continue
            if stop_event.is_set():
                return
            failures += 1
            if callable(state_callback):
                state_callback("reconnecting", {"at_monotonic": time.monotonic(), "reason": f"HTTP {exc.code}", "failures": failures})
            # Keep the durable report useful during a long restart: record
            # the first error and exponentially spaced repeats, not one row
            # for every reconnect attempt.
            if callable(error_callback) and (failures == 1 or failures & (failures - 1) == 0):
                error_callback(f"HTTP {exc.code}", failures)
            stop_event.wait(min(5.0, 0.25 * (2 ** min(failures, 4))))
        except Exception as exc:
            if stop_event.is_set():
                return
            failures += 1
            # Desktop's SSE endpoint sends no heartbeat. A read timeout after
            # a successful connection therefore means only that no new queue
            # event arrived in this five-second observation window. Recording
            # it as an outage produced a noisy and misleading run receipt.
            if not connected_once:
                failures += 1
            if callable(state_callback):
                state_callback(
                    "reconnecting" if connected_once else "connecting",
                    {"at_monotonic": time.monotonic(), "reason": type(exc).__name__, "failures": failures},
                )
            if (
                not connected_once
                and callable(error_callback)
                and (failures == 1 or failures & (failures - 1) == 0)
            ):
                error_callback(str(exc), failures)
            # After a successful connection, a socket timeout is merely an
            # idle SSE boundary. Reconnect indefinitely (until the owning
            # request ends) so a Desktop restart cannot silently remove the
            # only live queue observer. Before the first connection use a
            # small bounded exponential backoff instead of hammering a down
            # local server.
            delay = 0.75 if connected_once else min(5.0, 0.25 * (2 ** min(failures, 4)))
            stop_event.wait(delay)


def start_anythingllm_embed_progress_listener(
    api_url,
    api_key,
    workspace_slug,
    expected_locations,
    *,
    include_unmatched_events=False,
    observer_callback=None,
    observer_state_callback=None,
):
    """Start a best-effort, path-correlated Desktop progress listener.

    The listener never calls Gradio directly.  It records native events and,
    when supplied, invokes ``observer_callback`` for a caller-owned durable
    relay (the cancellable worker appends those updates to its event file).
    This makes Desktop's own per-record queue activity visible while the
    synchronous update request is still open without letting a background
    listener mutate browser components.
    """
    observed_events = []
    observed_errors = []
    observer_health = {"state": "connecting", "last_state_monotonic": time.monotonic(), "failures": 0, "reason": ""}
    health_lock = threading.Lock()
    stop_event = threading.Event()
    connected_event = threading.Event()

    def receive(event):
        event_copy = dict(event)
        event_copy["observed_at_utc"] = datetime.now(timezone.utc).isoformat()
        observed_events.append(event_copy)
        if callable(observer_callback):
            try:
                observer_callback(dict(event_copy))
            except Exception:
                # Progress observation is informative.  A relay failure must
                # never affect the Desktop submission that it is observing.
                pass

    def receive_error(error, attempt):
        observed_errors.append({"event": "desktop_embed_progress_unavailable", "attempt": attempt, "error": error})

    def receive_state(state, details):
        snapshot = None
        with health_lock:
            observer_health.update(
                {
                    "state": str(state or "unknown"),
                    "last_state_monotonic": float((details or {}).get("at_monotonic") or time.monotonic()),
                    "failures": int((details or {}).get("failures") or 0),
                    "reason": str((details or {}).get("reason") or ""),
                }
            )
            snapshot = dict(observer_health)
        if callable(observer_state_callback):
            try:
                observer_state_callback(snapshot)
            except Exception:
                # The state relay is UI/diagnostic evidence only. Never let a
                # consumer-side failure affect Desktop's submission request.
                pass

    thread = threading.Thread(
        target=listen_for_anythingllm_embed_progress,
        args=(api_url, api_key, workspace_slug, list(expected_locations or []), stop_event),
        kwargs={
            "event_callback": receive,
            "error_callback": receive_error,
            "state_callback": receive_state,
            "connected_event": connected_event,
            "include_unmatched_events": include_unmatched_events,
        },
        name="anythingllm-embed-progress",
        daemon=True,
    )
    thread.start()
    return {
        "stop_event": stop_event,
        "thread": thread,
        "connected_event": connected_event,
        "events": observed_events,
        "errors": observed_errors,
        "health": observer_health,
    }


def observe_workspace_embedding_queue_activity(
    api_url,
    api_key,
    workspace_slug,
    owned_locations,
    *,
    observation_seconds=3.0,
):
    """Take one bounded, read-only queue observation for recovery safety.

    Desktop's event feed is not a queue snapshot.  Silence therefore remains
    uncertainty, while an event whose filename is outside this run's durable
    submission ledger is positive evidence that another workflow is active.
    """
    listener = start_anythingllm_embed_progress_listener(
        api_url,
        api_key,
        workspace_slug,
        owned_locations,
        include_unmatched_events=True,
    )
    budget = max(0.0, min(10.0, float(observation_seconds or 0.0)))
    listener["connected_event"].wait(timeout=min(1.0, budget))
    if budget:
        listener["stop_event"].wait(timeout=budget)
    listener["stop_event"].set()
    listener["thread"].join(timeout=1.0)
    owned = {
        _normalized_anythingllm_document_location(location)
        for location in (owned_locations or [])
        if str(location or "").strip()
    }
    owned_events, non_owned_events = [], []
    for event in listener["events"]:
        filenames = [event.get("filename")]
        filenames.extend(event.get("filenames") or [])
        filenames.extend(event.get("embeddedFiles") or [])
        filenames.extend(event.get("failedFiles") or [])
        names = {
            _normalized_anythingllm_document_location(name)
            for name in filenames
            if str(name or "").strip()
        }
        if names & owned:
            owned_events.append(dict(event))
        if names - owned:
            non_owned_events.append(dict(event))
    if non_owned_events:
        status = "non_owned_activity_observed"
    elif owned_events:
        status = "owned_activity_observed"
    elif listener["connected_event"].is_set():
        status = "quiet_stream_uncertain"
    else:
        status = "stream_unavailable_uncertain"
    return {
        "status": status,
        "stream_connected": listener["connected_event"].is_set(),
        "owned_event_count": len(owned_events),
        "non_owned_event_count": len(non_owned_events),
        "events": list(listener["events"]),
        "stream_errors": list(listener["errors"]),
        "automatic_mutation_allowed": status == "owned_activity_observed",
        "automatic_restart_allowed": status == "owned_activity_observed",
    }


def confirmed_submission_locations_from_ledger(ledger):
    """Return only locations whose submission crossed the app's request boundary.

    Planned locations include records never sent because a cancellation can
    stop before the next batch. They are not authority to mutate Desktop.
    """
    confirmed_states = {
        "submitted", "accepted", "unresolved", "reconciliation_pending",
        "verification_failed",
    }
    locations = []
    seen = set()
    batches = list((ledger or {}).get("batches") or [])
    inflight = (ledger or {}).get("inflight_batch")
    if isinstance(inflight, dict):
        batches.append(inflight)
    for batch in batches:
        if str((batch or {}).get("submission_state") or "").strip() not in confirmed_states:
            continue
        for location in (batch or {}).get("locations") or []:
            normalized = _normalized_anythingllm_document_location(location)
            if normalized and normalized not in seen:
                seen.add(normalized)
                locations.append(normalized)
    return locations


def post_multipart_form(url, fields, file_field_name, file_path, api_key=None, timeout=120):
    boundary = f"----CodexBoundary{int(time.time() * 1000)}"
    body = bytearray()

    def add_text_part(name, value):
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8")
        )
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")

    for key, value in (fields or {}).items():
        if value is None:
            continue
        add_text_part(key, value)

    mime_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    filename = Path(file_path).name
    file_bytes = Path(file_path).read_bytes()
    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(
        (
            f'Content-Disposition: form-data; name="{file_field_name}"; '
            f'filename="{filename}"\r\n'
        ).encode("utf-8")
    )
    body.extend(f"Content-Type: {mime_type}\r\n\r\n".encode("utf-8"))
    body.extend(file_bytes)
    body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))

    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=bytes(body), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.status, response.read().decode("utf-8", errors="replace")


def get_json(url, api_key=None, timeout: float = 30.0):
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.status, response.read().decode("utf-8", errors="replace")


def get_json_with_retry(url, api_key=None, timeout: float = 5.0, max_attempts=3, sleeper=time.sleep, jitter=random.uniform):
    """Retry safe read requests only, retaining bounded attempt evidence.

    This helper is deliberately not used for upload POSTs: an ambiguous write
    is reconciled through the durable submission receipt instead of replayed.
    """
    attempts = []
    limit = max(1, int(max_attempts or 1))
    for attempt in range(1, limit + 1):
        retry_after = None
        try:
            status, text = get_json(url, api_key=api_key, timeout=timeout)
            attempts.append({"attempt": attempt, "http_status": status, "error": ""})
            if status not in {429, 500, 502, 503, 504} or attempt >= limit:
                return {"http_status": status, "text": text, "attempts": attempts}
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            try:
                body = exc.read().decode("utf-8", errors="replace")
            finally:
                exc.close()
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            attempts.append({"attempt": attempt, "http_status": status, "error": body[:300]})
            if status not in {429, 500, 502, 503, 504} or attempt >= limit:
                return {"http_status": status, "text": body, "attempts": attempts}
        except Exception as exc:
            attempts.append({"attempt": attempt, "http_status": None, "error": str(exc)[:300]})
            if attempt >= limit:
                return {"http_status": None, "text": "", "attempts": attempts}
        try:
            delay = float(retry_after) if retry_after else min(2.0, .20 * (2 ** (attempt - 1)))
        except (TypeError, ValueError):
            delay = min(2.0, .20 * (2 ** (attempt - 1)))
        sleeper(max(0.0, delay + float(jitter(0.0, .10))))
    return {"http_status": None, "text": "", "attempts": attempts}


def delete_json(url, api_key=None, timeout=30, body=None):
    headers = {}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=data, headers=headers, method="DELETE")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.status, response.read().decode("utf-8", errors="replace")


def is_local_anythingllm_url(api_url):
    try:
        hostname = (urllib.parse.urlparse(api_url).hostname or "").casefold()
    except Exception:
        return False
    return hostname in {"127.0.0.1", "localhost", "::1"}


def _safe_app_owned_queue_location(location):
    normalized = str(location or "").replace("\\", "/").strip().lstrip("/")
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if len(parts) < 2 or parts[0].casefold() != "custom-documents" or any(part == ".." for part in parts):
        return ""
    return "/".join(parts)


def remove_confirmed_workspace_queue_entries(
    api_url,
    api_key,
    workspace_slug,
    locations,
    activity_observation,
    *,
    total_timeout=20.0,
    request_timeout=4.0,
    initial_workers=2,
    max_workers=4,
):
    """Bounded cleanup for confirmed app submissions after positive ownership evidence.

    This helper will not run from a quiet or unavailable stream: those states
    cannot establish that a manual queue is absent.  It permits at most one
    retry per record and returns at its total deadline even if Desktop has
    stopped responding.
    """
    result = {
        "status": "not_attempted", "attempted": 0, "removed": 0, "absent": 0,
        "timed_out": 0, "retry_count": 0, "deadline_seconds": max(1.0, float(total_timeout)),
        "errors": [], "unresolved_locations": [], "removed_locations": [],
    }
    if not is_local_anythingllm_url(api_url) or not is_lancedb_safe_namespace(workspace_slug):
        result["status"] = "rejected_target"
        return result
    if str((activity_observation or {}).get("status") or "") != "owned_activity_observed":
        result["status"] = "blocked_by_manual_activity_or_uncertainty"
        return result
    trusted, seen = [], set()
    for location in locations or []:
        safe_location = _safe_app_owned_queue_location(location)
        if safe_location and safe_location.casefold() not in seen:
            seen.add(safe_location.casefold())
            trusted.append(safe_location)
    if not trusted:
        result["status"] = "no_confirmed_managed_locations"
        return result
    endpoint = api_url.rstrip("/") + f"/api/v1/workspace/{workspace_slug}/embed-queue"
    deadline = time.monotonic() + result["deadline_seconds"]
    attempts = {location: 0 for location in trusted}
    pending = list(trusted)
    workers = max(1, min(2, int(initial_workers or 1), len(pending)))
    ceiling = max(workers, min(4, int(max_workers or 1)))

    def remove_one(location, timeout):
        started = time.monotonic()
        try:
            status, text = delete_json(endpoint, api_key=api_key, timeout=timeout, body={"filename": location})
            try:
                payload = json.loads(text) if str(text or "").strip() else {}
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            if int(status) in {200, 201, 202} and isinstance(payload, dict) and payload.get("success") is False:
                return location, "absent", time.monotonic() - started, ""
            if int(status) in {200, 201, 202, 204}:
                return location, "removed", time.monotonic() - started, ""
            if int(status) == 404:
                return location, "absent", time.monotonic() - started, ""
            return location, "retryable", time.monotonic() - started, f"HTTP {int(status)}"
        except urllib.error.HTTPError as exc:
            return location, ("absent" if int(exc.code) == 404 else "retryable"), time.monotonic() - started, f"HTTP {int(exc.code)}"
        except (TimeoutError, urllib.error.URLError) as exc:
            return location, "retryable", time.monotonic() - started, type(exc).__name__
        except Exception as exc:
            return location, "retryable", time.monotonic() - started, type(exc).__name__

    while pending and time.monotonic() < deadline:
        wave = pending[:workers]
        pending = pending[workers:]
        timeout = max(0.25, min(float(request_timeout), deadline - time.monotonic()))
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=len(wave))
        futures = {executor.submit(remove_one, location, timeout): location for location in wave}
        done, unfinished = concurrent.futures.wait(futures, timeout=max(0.0, deadline - time.monotonic()))
        executor.shutdown(wait=False, cancel_futures=True)
        prompt_wave = bool(done) and not unfinished
        for future in done:
            location, state, elapsed, error = future.result()
            attempts[location] += 1
            result["attempted"] += 1
            if state == "removed":
                result["removed"] += 1
                result["removed_locations"].append(location)
            elif state == "absent":
                result["absent"] += 1
            elif attempts[location] <= 1 and time.monotonic() < deadline:
                result["retry_count"] += 1
                pending.append(location)
            else:
                result["timed_out"] += 1
                result["errors"].append({"location": location, "error": error or "queue removal unresolved"})
            prompt_wave = prompt_wave and elapsed <= 1.0
        for future in unfinished:
            location = futures[future]
            future.cancel()
            attempts[location] += 1
            result["attempted"] += 1
            if attempts[location] <= 1 and time.monotonic() < deadline:
                result["retry_count"] += 1
                pending.append(location)
            else:
                result["timed_out"] += 1
                result["errors"].append({"location": location, "error": "total cleanup deadline reached"})
        if prompt_wave and workers < ceiling:
            workers = min(ceiling, workers + 1)
    result["unresolved_locations"] = list(dict.fromkeys(pending))
    if pending:
        result["timed_out"] += len(pending)
    result["status"] = "complete" if not result["errors"] and not pending else "partial"
    return result


def default_anythingllm_desktop_executable_candidates():
    candidates = []
    local_app_data = Path(os.environ.get("LOCALAPPDATA") or "")
    program_files = Path(os.environ.get("ProgramFiles") or "")
    program_files_x86 = Path(os.environ.get("ProgramFiles(x86)") or "")
    for candidate in (
        local_app_data / "Programs" / "AnythingLLM" / "AnythingLLM.exe",
        program_files / "AnythingLLM" / "AnythingLLM.exe",
        program_files_x86 / "AnythingLLM" / "AnythingLLM.exe",
    ):
        if candidate and str(candidate) not in {"", "."}:
            candidates.append(candidate)
    seen = set()
    deduped = []
    for candidate in candidates:
        normalized = str(candidate).casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(candidate)
    return deduped


def find_anythingllm_desktop_executable():
    for candidate in default_anythingllm_desktop_executable_candidates():
        if candidate.exists():
            return candidate
    return None


def anythingllm_desktop_process_running():
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq AnythingLLM.exe", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                check=False,
            )
            output = (result.stdout or "").strip()
            return bool(output and "No tasks are running" not in output)
        result = subprocess.run(
            ["pgrep", "-f", "AnythingLLM"],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0 and bool((result.stdout or "").strip())
    except Exception:
        return False


def anythingllm_runtime_ports(storage_dir=None):
    storage = Path(storage_dir) if storage_dir else default_anythingllm_storage_dir()
    values = read_env_file_values(storage / ".env")
    ports = []
    for key in ("SERVER_PORT", "COLLECTOR_PORT"):
        raw = str(values.get(key) or "").strip()
        if not raw:
            continue
        try:
            port = int(raw)
        except ValueError:
            continue
        if 1 <= port <= 65535 and port not in ports:
            ports.append(port)
    for default_port in (3001, 8888):
        if default_port not in ports:
            ports.append(default_port)
    return ports


def preferred_anythingllm_api_urls(preferred_url=""):
    candidates = []

    def add(url):
        normalized = str(url or "").strip().rstrip("/")
        if normalized and normalized not in candidates:
            candidates.append(normalized)

    add(preferred_url)
    storage = default_anythingllm_storage_dir()
    for port in anythingllm_runtime_ports(storage):
        add(f"http://127.0.0.1:{port}")
        add(f"http://localhost:{port}")
    for candidate in ANYTHINGLLM_API_CANDIDATE_URLS:
        add(candidate)
    return candidates


def ping_anythingllm_api(api_url, api_key=None, timeout=2.0):
    normalized = str(api_url or "").strip().rstrip("/")
    result = {
        "api_url": normalized,
        "status": "missing_url",
        "http_status": None,
        "message": "",
        "error": "",
    }
    if not normalized:
        result["message"] = "No AnythingLLM API URL was provided."
        return result
    try:
        read = get_json_with_retry(
            normalized + "/api/ping", api_key=api_key, timeout=timeout, max_attempts=2,
        )
        status, response_text = read.get("http_status"), read.get("text", "")
        result["read_attempts"] = read.get("attempts", [])
        result["http_status"] = status
        if status is None:
            result["status"] = "unreachable"
            result["error"] = str((read.get("attempts") or [{}])[-1].get("error") or "No response")
            result["message"] = result["error"]
        elif 200 <= status < 300:
            result["status"] = "reachable"
            try:
                data = json.loads(response_text)
                if isinstance(data, dict) and (
                    "online" in data
                    or "message" in data
                    or "authenticated" in data
                ):
                    result["message"] = str(data.get("message") or data)
                else:
                    result["status"] = "unexpected_payload"
                    result["message"] = "AnythingLLM /api/ping did not return the expected JSON contract."
            except Exception:
                body = str(response_text or "").strip()
                if body.upper() == "OK":
                    result["status"] = "collector_stub"
                    result["message"] = (
                        "AnythingLLM responded with plain-text OK on /api/ping. "
                        "This is a health stub, not the JSON Desktop API endpoint used for uploads."
                    )
                else:
                    result["status"] = "unexpected_payload"
                    result["message"] = "AnythingLLM /api/ping returned non-JSON content."
        elif status in {401, 403}:
            result["status"] = "reachable_auth_required"
            result["message"] = f"AnythingLLM responded on {normalized}, but rejected authentication."
        else:
            result["status"] = "unexpected_status"
            result["message"] = f"AnythingLLM returned HTTP {status} on /api/ping."
    except urllib.error.HTTPError as exc:
        result["http_status"] = exc.code
        if exc.code in {401, 403}:
            result["status"] = "reachable_auth_required"
            result["message"] = f"AnythingLLM responded on {normalized}, but rejected authentication."
        else:
            result["status"] = "http_error"
            result["error"] = str(exc)
            result["message"] = f"AnythingLLM returned HTTP {exc.code} on /api/ping."
    except Exception as exc:
        result["status"] = "unreachable"
        result["error"] = str(exc)
        result["message"] = str(exc)
    return result


def detect_anythingllm_api_url(preferred_url="", api_key=None, timeout=2.0):
    attempts = []
    for candidate in preferred_anythingllm_api_urls(preferred_url):
        probe = ping_anythingllm_api(candidate, api_key=api_key, timeout=timeout)
        attempts.append(probe)
        if probe["status"] in {"reachable", "reachable_auth_required"}:
            return {
                "status": probe["status"],
                "api_url": probe["api_url"],
                "attempts": attempts,
                "message": probe["message"] or f"AnythingLLM is reachable at {probe['api_url']}.",
            }
    fallback_url = str(preferred_url or DEFAULT_ANYTHINGLLM_API_URL).strip().rstrip("/") or DEFAULT_ANYTHINGLLM_API_URL
    return {
        "status": "unreachable",
        "api_url": fallback_url,
        "attempts": attempts,
        "message": "AnythingLLM did not respond on the preferred or fallback local URLs.",
    }


def start_anythingllm_desktop(executable_path=None):
    exe = Path(executable_path) if executable_path else find_anythingllm_desktop_executable()
    result = {
        "status": "not_attempted",
        "started": False,
        "already_running": False,
        "executable": str(exe) if exe else "",
        "error": "",
    }
    if exe is None:
        result["status"] = "missing_executable"
        result["error"] = "AnythingLLM Desktop executable was not found on this machine."
        return result
    if anythingllm_desktop_process_running():
        result["status"] = "already_running"
        result["already_running"] = True
        return result
    try:
        creationflags = 0
        if os.name == "nt":
            creationflags = 0x00000008 | 0x00000200
        subprocess.Popen(
            [str(exe)],
            cwd=str(exe.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
            close_fds=False if os.name == "nt" else True,
        )
        result["status"] = "started"
        result["started"] = True
        return result
    except Exception as exc:
        result["status"] = "start_failed"
        result["error"] = str(exc)
        return result


def restart_anythingllm_desktop(
    preferred_url="",
    api_key=None,
    *,
    startup_timeout=45.0,
):
    """Restart local Desktop only after the caller has established safety.

    This deliberately performs no queue inspection or policy decision itself.
    Callers must record why a restart was allowed before invoking it.
    """
    result = {"status": "not_attempted", "stopped": False, "start": {}, "error": ""}
    if preferred_url and not is_local_anythingllm_url(preferred_url):
        result["status"] = "rejected_nonlocal_runtime"
        return result
    if anythingllm_desktop_process_running():
        try:
            completed = subprocess.run(
                ["taskkill", "/IM", "AnythingLLM.exe", "/T", "/F"] if os.name == "nt" else ["pkill", "-f", "AnythingLLM"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            result["stopped"] = completed.returncode == 0
        except (OSError, subprocess.SubprocessError) as exc:
            result["status"] = "stop_failed"
            result["error"] = str(exc)
            return result
        if not result["stopped"]:
            result["status"] = "stop_failed"
            return result
    started = start_anythingllm_desktop()
    result["start"] = started
    if started.get("status") in {"missing_executable", "start_failed"}:
        result["status"] = started.get("status")
        result["error"] = str(started.get("error") or "")
        return result
    runtime = ensure_anythingllm_runtime(
        preferred_url,
        api_key=api_key,
        timeout=1.25,
        startup_timeout=max(5.0, float(startup_timeout)),
        autostart_local=False,
    )
    result["runtime"] = runtime
    result["status"] = "ready" if runtime.get("status") in {"reachable", "reachable_auth_required"} else "startup_timeout"
    return result


def ensure_anythingllm_runtime(
    preferred_url="",
    api_key=None,
    timeout=2.0,
    startup_timeout=DEFAULT_ANYTHINGLLM_STARTUP_TIMEOUT_SECONDS,
    autostart_local=False,
    status_callback=None,
    startup_poll_interval=1.5,
    startup_fast_poll_interval=None,
    startup_fast_poll_window=0.0,
):
    """Ensure the local Desktop API can be reached, reporting bounded lifecycle updates.

    ``status_callback`` is deliberately observational: callers such as the
    Gradio run status can show that Desktop is launching or has become ready,
    but a callback failure can never change the recovery outcome.
    """
    def report_status(phase, snapshot):
        if not callable(status_callback):
            return
        try:
            status_callback(str(phase), dict(snapshot or {}))
        except Exception:
            # A presentation observer must not break runtime recovery.
            pass

    detection = detect_anythingllm_api_url(preferred_url, api_key=api_key, timeout=timeout)
    result = {
        **detection,
        "start": {
            "status": "not_attempted",
            "started": False,
            "already_running": False,
            "executable": "",
            "error": "",
        },
        "waited_for_runtime": False,
        "lifecycle": [{"phase": "initial_detection", "status": detection.get("status", "not_checked")}],
    }
    report_status("initial_detection", result)
    if detection.get("status") in {"reachable", "reachable_auth_required"}:
        result["lifecycle"].append({"phase": "ready", "status": detection.get("status")})
        report_status("ready", result)
        return result
    if not autostart_local:
        result["lifecycle"].append({"phase": "autostart_skipped", "status": "not_requested"})
        report_status("autostart_skipped", result)
        return result
    if preferred_url and not is_local_anythingllm_url(preferred_url):
        result["lifecycle"].append({"phase": "autostart_skipped", "status": "non_local_target"})
        report_status("autostart_skipped", result)
        return result
    result["lifecycle"].append({"phase": "starting_desktop", "status": "pending"})
    report_status("starting_desktop", result)
    start_result = start_anythingllm_desktop()
    result["start"] = start_result
    if start_result.get("status") in {"missing_executable", "start_failed"}:
        result["lifecycle"].append({"phase": "start_failed", "status": start_result.get("status")})
        report_status("start_failed", result)
        return result
    deadline = time.time() + max(5.0, float(startup_timeout or DEFAULT_ANYTHINGLLM_STARTUP_TIMEOUT_SECONDS))
    poll_interval = max(0.1, float(startup_poll_interval or 1.5))
    try:
        fast_poll_interval = max(0.1, float(startup_fast_poll_interval or poll_interval))
    except (TypeError, ValueError):
        fast_poll_interval = poll_interval
    try:
        fast_poll_window = max(0.0, float(startup_fast_poll_window or 0.0))
    except (TypeError, ValueError):
        fast_poll_window = 0.0
    started_waiting_at = time.time()
    result["waited_for_runtime"] = True
    result["startup_timeout_seconds"] = max(5.0, float(startup_timeout or DEFAULT_ANYTHINGLLM_STARTUP_TIMEOUT_SECONDS))
    result["startup_poll_interval_seconds"] = poll_interval
    result["startup_fast_poll_interval_seconds"] = fast_poll_interval
    result["startup_fast_poll_window_seconds"] = fast_poll_window
    result["startup_probe_count"] = 0
    result["lifecycle"].append({"phase": "waiting_for_runtime", "status": start_result.get("status", "started")})
    report_status("waiting_for_runtime", result)
    latest = detection
    while time.time() < deadline:
        latest = detect_anythingllm_api_url(preferred_url, api_key=api_key, timeout=min(float(timeout or 2.0), 1.25))
        result.update(latest)
        result["startup_probe_count"] += 1
        result["startup_wait_elapsed_seconds"] = round(
            max(0.0, result["startup_timeout_seconds"] - max(0.0, deadline - time.time())), 3
        )
        result["startup_wait_remaining_seconds"] = round(max(0.0, deadline - time.time()), 3)
        if latest.get("status") in {"reachable", "reachable_auth_required"}:
            result["lifecycle"].append({"phase": "ready_after_start", "status": latest.get("status")})
            report_status("ready_after_start", result)
            return result
        report_status("waiting_for_runtime", result)
        # A newly launched Electron window is commonly visible before its
        # local API listener has finished binding.  Probe quickly during that
        # short startup window, then revert to the caller's quieter interval.
        # This is a responsiveness rule, not a second timeout.
        elapsed_waiting = max(0.0, time.time() - started_waiting_at)
        next_interval = (
            fast_poll_interval
            if fast_poll_window and elapsed_waiting < fast_poll_window
            else poll_interval
        )
        time.sleep(min(next_interval, max(0.0, deadline - time.time())))
    result.update(latest)
    result["lifecycle"].append({"phase": "startup_timeout", "status": latest.get("status", "unreachable")})
    report_status("startup_timeout", result)
    return result


def describe_api_exception(exc, service_name):
    """Return a concise, secret-safe description for an API request failure."""
    service = str(service_name or "service")
    if isinstance(exc, urllib.error.HTTPError):
        status = int(getattr(exc, "code", 0) or 0)
        reason = str(getattr(exc, "reason", "request failed") or "request failed")
        return f"{service} returned HTTP {status}: {reason}."
    if isinstance(exc, urllib.error.URLError):
        reason = str(getattr(exc, "reason", "network request failed") or "network request failed")
        return f"{service} network error: {reason}."
    message = re.sub(r"Bearer\s+[A-Za-z0-9._\-]+", "Bearer [redacted]", str(exc))
    return f"{service} request failed: {message[:400]}"


def verify_anythingllm_upload_auth(api_url, api_key=None):
    result = {
        "status": "not_checked",
        "authenticated": False,
        "authentication_mode": "provided_api_key" if api_key else "none",
        "temporary_key_cleanup": {"status": "not_applicable", "error": ""},
        "message": "",
        "error": "",
    }
    normalized = str(api_url or "").strip().rstrip("/")
    if not normalized:
        result["status"] = "missing_api_url"
        result["message"] = "AnythingLLM API URL is missing."
        return result
    runtime_key, authentication_mode = resolve_anythingllm_api_key(normalized, api_key)
    result["authentication_mode"] = authentication_mode
    if runtime_key:
        try:
            status, _ = get_json(normalized + "/api/v1/workspaces", api_key=runtime_key, timeout=10)
            result["authenticated"] = 200 <= status < 300
            result["status"] = "authenticated" if result["authenticated"] else "authentication_failed"
            result["message"] = (
                "AnythingLLM service API key was accepted."
                if result["authenticated"]
                else f"AnythingLLM returned HTTP {status} while verifying the provided API key."
            )
            return result
        except Exception as exc:
            result["status"] = "authentication_failed"
            result["error"] = str(exc)
            result["message"] = describe_api_exception(exc, "AnythingLLM")
            return result
    if not is_local_anythingllm_url(normalized):
        result["status"] = "authentication_required"
        result["message"] = "A Developer API key is required for non-local AnythingLLM upload targets."
        return result
    temporary_key = create_temporary_desktop_api_key(normalized)
    if temporary_key.get("status") != "created":
        result["status"] = "authentication_required"
        result["authentication_mode"] = "unavailable"
        result["error"] = temporary_key.get("error", "")
        result["message"] = "AnythingLLM Desktop temporary API key creation is unavailable."
        return result
    result["authenticated"] = True
    result["authentication_mode"] = "temporary_desktop_api_key"
    result["status"] = "authenticated"
    result["message"] = "AnythingLLM Desktop temporary API key route is available."
    result["temporary_key_cleanup"] = cleanup_temporary_desktop_api_key(normalized, temporary_key.get("id"))
    return result


def create_temporary_desktop_api_key(api_url):
    if not is_local_anythingllm_url(api_url):
        return {
            "status": "not_local_desktop",
            "id": None,
            "secret": "",
            "error": "Temporary API keys are only created for loopback AnythingLLM Desktop URLs.",
        }
    endpoint = api_url.rstrip("/") + "/api/system/generate-api-key"
    try:
        status, response_text = post_json(
            endpoint,
            {"name": "PDF Assistant temporary native metadata test"},
        )
        data = json.loads(response_text)
        api_key = data.get("apiKey") if isinstance(data, dict) else None
        if not (200 <= status < 300) or not isinstance(api_key, dict):
            raise RuntimeError(data.get("error") if isinstance(data, dict) else "No API key was returned.")
        key_id = api_key.get("id")
        secret = api_key.get("secret")
        if not key_id or not secret:
            raise RuntimeError("AnythingLLM returned an incomplete temporary API key.")
        return {
            "status": "created",
            "id": key_id,
            "secret": secret,
            "error": "",
        }
    except Exception as exc:
        return {
            "status": "error",
            "id": None,
            "secret": "",
            "error": str(exc),
        }


def delete_temporary_desktop_api_key(api_url, key_id, api_key=None):
    if not key_id:
        return {"status": "not_applicable", "error": ""}
    endpoint = api_url.rstrip("/") + f"/api/system/api-key/{key_id}"
    try:
        management_key, _ = resolve_anythingllm_api_key(api_url, api_key)
        status, _ = delete_json(endpoint, api_key=management_key or None)
        return {
            "status": "deleted" if 200 <= status < 300 else "delete_failed",
            "http_status": status,
            "error": "",
        }
    except Exception as exc:
        return {"status": "delete_failed", "error": str(exc)}


def cleanup_temporary_desktop_api_key(api_url, key_id, api_key=None):
    """Delete one managed temporary key with one bounded retry.

    The caller owns the final outcome policy. This helper only normalizes the
    security-relevant cleanup evidence so upload, runtime, and validation paths
    do not silently diverge or retry an unknown credential indefinitely.
    """
    cleanup_kwargs = {"api_key": api_key} if api_key else {}
    first = delete_temporary_desktop_api_key(api_url, key_id, **cleanup_kwargs)
    result: dict[str, Any] = dict(first or {})
    result["attempt_count"] = 1
    result["retry_attempted"] = False
    if result.get("status") != "delete_failed":
        return result
    second = delete_temporary_desktop_api_key(api_url, key_id, **cleanup_kwargs)
    result = dict(second or {})
    result["attempt_count"] = 2
    result["retry_attempted"] = True
    result["first_attempt_status"] = str((first or {}).get("status") or "delete_failed")
    return result


def anythingllm_runtime_embedder_failure_hint(result, storage_dir=None):
    """Produce one short provider-specific recovery instruction without secrets."""
    storage = Path(storage_dir) if storage_dir else None
    provider = str((result or {}).get("provider") or "").casefold()
    if not provider and storage:
        provider = str(anythingllm_embedding_config(storage).get("normalized_engine") or "").casefold()
    if provider != "openrouter":
        return ""
    return "Restart AnythingLLM if it persists."


def anythingllm_openrouter_auth_failure_observed(storage_dir=None):
    """Return whether recent local backend logs prove an OpenRouter auth rejection.

    AnythingLLM Desktop can collapse an upstream OpenRouter 401 into an empty
    HTTP 500 response.  Do not infer an expired credential from every 500: the
    warning is emitted only when the local backend log records the provider's
    authentication rejection.  The helper returns a boolean and never exposes
    log content or credentials.
    """
    if not storage_dir:
        return False
    logs_dir = Path(storage_dir) / "logs"
    try:
        candidates = sorted(
            logs_dir.glob("backend-*.log"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[:3]
    except OSError:
        return False
    for path in candidates:
        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 512_000))
                text = handle.read().decode("utf-8", errors="replace").casefold()
        except OSError:
            continue
        if "openrouter failed to embed" not in text:
            continue
        if any(marker in text for marker in (
            "401 user not found",
            "401 unauthorized",
            "401 invalid api key",
            "401 invalid api-key",
        )):
            return True
    return False


def verify_anythingllm_runtime_embedder(api_url, api_key=None, storage_dir=None, sample_text="Runtime embedder verification probe."):
    result = {
        "status": "not_checked",
        "http_status": None,
        "provider": "",
        "model": "",
        "dimension": 0,
        "usage_prompt_tokens": 0,
        "usage_total_tokens": 0,
        "authentication_mode": "provided_api_key" if api_key else "none",
        "temporary_key_cleanup": {"status": "not_applicable", "error": ""},
        "message": "",
        "error": "",
    }
    if storage_dir:
        embed = anythingllm_embedding_config(storage_dir)
        result["provider"] = embed.get("normalized_engine") or embed.get("engine") or ""
        result["model"] = embed.get("effective_model") or embed.get("model") or ""
    api_url = (api_url or "").strip()
    if not api_url:
        result["status"] = "skipped_missing_api_url"
        result["message"] = "AnythingLLM API URL was not provided."
        return result
    runtime_key, authentication_mode = resolve_anythingllm_api_key(api_url, api_key, storage_dir)
    result["authentication_mode"] = authentication_mode
    temporary_key_id = None
    try:
        if not runtime_key and is_local_anythingllm_url(api_url):
            temporary_key = create_temporary_desktop_api_key(api_url)
            if temporary_key.get("status") == "created":
                runtime_key = temporary_key["secret"]
                temporary_key_id = temporary_key["id"]
                result["authentication_mode"] = "temporary_desktop_api_key"
            else:
                result["status"] = "authentication_required"
                result["message"] = "AnythingLLM temporary Desktop API key could not be created."
                result["error"] = temporary_key.get("error", "")
                return result
        endpoint = api_url.rstrip("/") + "/api/v1/openai/embeddings"
        response = post_json_captured(
            endpoint,
            {"input": [str(sample_text or "Runtime embedder verification probe.")]},
            api_key=runtime_key,
            timeout_label="AnythingLLM embedder runtime probe",
        )
        result["http_status"] = response.get("http_status")
        data = response.get("data") if isinstance(response.get("data"), dict) else {}
        embeddings = data.get("data") if isinstance(data, dict) else None
        usage = data.get("usage") if isinstance(data, dict) else {}
        if response.get("http_status") and 200 <= response["http_status"] < 300 and isinstance(embeddings, list) and embeddings:
            first_embedding = embeddings[0].get("embedding") if isinstance(embeddings[0], dict) else None
            if isinstance(first_embedding, list) and first_embedding:
                result["dimension"] = len(first_embedding)
                result["usage_prompt_tokens"] = int((usage or {}).get("prompt_tokens") or 0)
                result["usage_total_tokens"] = int((usage or {}).get("total_tokens") or 0)
                result["status"] = "pass"
                result["message"] = (
                    f"AnythingLLM returned embeddings from /api/v1/openai/embeddings"
                    f" ({result['dimension']} dimensions)."
                )
                return result
            result["status"] = "missing_embeddings"
            result["message"] = "AnythingLLM responded successfully but did not return embedding vectors."
            result["error"] = response.get("error") or json.dumps(data)[:500]
            return result
        http_status = response.get("http_status")
        raw_body = ""
        if isinstance(data, dict):
            raw_body = str(data.get("raw") or data.get("error") or "")
        result["error"] = response.get("error") or raw_body
        if http_status == 401:
            result["status"] = "authentication_failed"
            result["message"] = "AnythingLLM rejected the runtime embedder probe with 401."
        elif http_status and http_status >= 500 and not raw_body:
            result["status"] = "server_error_empty_body"
            result["message"] = "AnythingLLM returned a server error with an empty body for the runtime embedder probe."
        elif http_status and http_status >= 500:
            result["status"] = "server_error"
            result["message"] = f"AnythingLLM returned HTTP {http_status} for the runtime embedder probe."
        elif http_status:
            result["status"] = f"http_{http_status}"
            result["message"] = f"AnythingLLM returned HTTP {http_status} for the runtime embedder probe."
        else:
            result["status"] = "network_error"
            result["message"] = "AnythingLLM runtime embedder probe did not complete."
        if (
            str(result.get("provider") or "").casefold() == "openrouter"
            and anythingllm_openrouter_auth_failure_observed(storage_dir)
        ):
            result["status"] = "openrouter_credential_reverification_required"
            result["warning_code"] = "AUTO-OPENROUTER-KEY-REVERIFY-001"
            result["message"] = (
                "OpenRouter rejected the embedding key (401); this PDF was not uploaded."
            )
        hint = anythingllm_runtime_embedder_failure_hint(result, storage_dir)
        if hint:
            result["message"] = f"{result['message']} {hint}"
        return result
    finally:
        if temporary_key_id:
            result["temporary_key_cleanup"] = cleanup_temporary_desktop_api_key(
                api_url,
                temporary_key_id,
            )


def refresh_local_anythingllm_openrouter_runtime(api_url, api_key=None, storage_dir=None):
    """Refresh a stale local OpenRouter secret without exposing or changing it.

    AnythingLLM's supported update-env route writes the existing value into
    the running process. Updating only OpenRouterApiKey cannot reset vector
    namespaces; embedding engine/model updates are deliberately excluded.
    """
    result = {
        "status": "not_attempted",
        "http_status": None,
        "authentication_mode": "none",
        "updated_keys": [],
        "message": "",
        "error": "",
    }
    storage = Path(storage_dir) if storage_dir else default_anythingllm_storage_dir()
    if not is_local_anythingllm_url(api_url):
        result["status"] = "skipped_non_local_runtime"
        result["message"] = "Automatic runtime refresh is limited to local AnythingLLM Desktop."
        return result
    provider = str(anythingllm_embedding_config(storage).get("normalized_engine") or "").casefold()
    if provider != "openrouter":
        result["status"] = "skipped_unsupported_provider"
        result["message"] = "Automatic runtime refresh currently supports only the configured OpenRouter embedder."
        return result
    provider_key = anythingllm_storage_secret(storage, "OPENROUTER_API_KEY")
    if not provider_key:
        result["status"] = "skipped_missing_provider_key"
        result["message"] = "The persisted OpenRouter key is unavailable."
        return result
    runtime_key, authentication_mode = resolve_anythingllm_api_key(api_url, api_key, storage)
    result["authentication_mode"] = authentication_mode
    if not runtime_key:
        result["status"] = "skipped_missing_api_authentication"
        result["message"] = "AnythingLLM Developer API authentication is unavailable."
        return result
    response = post_json_captured(
        api_url.rstrip("/") + "/api/v1/system/update-env",
        {"OpenRouterApiKey": provider_key},
        api_key=runtime_key,
        timeout_label="AnythingLLM OpenRouter runtime refresh",
        timeout_seconds=30,
    )
    result["http_status"] = response.get("http_status")
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    new_values = data.get("newValues") if isinstance(data, dict) else {}
    result["updated_keys"] = sorted(new_values.keys()) if isinstance(new_values, dict) else []
    result["error"] = str((data or {}).get("error") or response.get("error") or "")
    if response.get("http_status") == 200 and not result["error"] and "OpenRouterApiKey" in result["updated_keys"]:
        result["status"] = "refreshed"
        result["message"] = "AnythingLLM refreshed its existing OpenRouter key in the running process."
    else:
        result["status"] = "refresh_failed"
        result["message"] = "AnythingLLM did not confirm the runtime environment refresh."
    return result


def _alphabetic_series_label(index):
    value = int(index)
    if value < 0:
        value = 0
    label = ""
    while True:
        value, remainder = divmod(value, 26)
        label = chr(ord("A") + remainder) + label
        if value == 0:
            break
        value -= 1
    return label


def next_validation_workspace_prefix(storage_dir=None, base_prefix="Chunk Survival Validation"):
    prefix_root = "Chunk Survival Validation"
    storage_path = Path(storage_dir) if storage_dir else None
    if not storage_path:
        return f"A {base_prefix}"
    db_path = storage_path / "anythingllm.db"
    if not db_path.exists():
        return f"A {base_prefix}"
    try:
        con = sqlite3.connect(db_path)
        try:
            names = [
                str(row[0] or "")
                for row in con.execute(
                    "select name from workspaces where lower(name) like ?",
                    ("%chunk survival validation%",),
                ).fetchall()
            ]
        finally:
            con.close()
    except Exception:
        return f"A {base_prefix}"
    prefixed_names = [
        name for name in names
        if re.match(rf"^[A-Z]+ {re.escape(base_prefix)}\b", name)
        or re.match(rf"^[A-Z]+ {re.escape(prefix_root)}\b", name)
    ]
    next_label = _alphabetic_series_label(len(prefixed_names))
    return f"{next_label} {base_prefix}"


def create_validation_workspace(
    api_url,
    api_key=None,
    name_prefix="Chunk Survival Validation",
    top_n=8,
    storage_dir=None,
    workspace_name="",
):
    runtime_key, authentication_mode = resolve_anythingllm_api_key(api_url, api_key, storage_dir)
    temporary_key_id = None
    if not runtime_key:
        temporary_key = create_temporary_desktop_api_key(api_url)
        if temporary_key.get("status") != "created":
            return {
                "status": "authentication_required",
                "workspace_slug": "",
                "workspace_name": "",
                "authentication_mode": authentication_mode,
                "temporary_key_cleanup": {"status": "not_applicable", "error": ""},
                "error": temporary_key.get("error", ""),
            }
        runtime_key = temporary_key["secret"]
        temporary_key_id = temporary_key["id"]
        authentication_mode = "temporary_desktop_api_key"
    requested_workspace_name = str(workspace_name or "").strip()
    if not requested_workspace_name:
        visible_prefix = next_validation_workspace_prefix(storage_dir, name_prefix)
        requested_workspace_name = f"{visible_prefix} {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    workspace_name, collision_suffix = unique_lancedb_workspace_name(
        requested_workspace_name,
        storage_dir=storage_dir,
    )
    workspace_template = (
        read_validation_workspace_template(Path(storage_dir))
        if storage_dir
        else {"status": "not_checked"}
    )
    # Validation may intentionally use a smaller retrieval context for large
    # page-parent records. Preserve provider/model/template settings, but make
    # the requested bounded top-N authoritative for this disposable workspace.
    if workspace_template.get("status") == "pass":
        workspace_template = {**workspace_template, "top_n": int(top_n)}
    cleanup = {"status": "not_applicable", "error": ""}
    try:
        status, response_text = post_json(
            api_url.rstrip("/") + "/api/v1/workspace/new",
            {
                "name": workspace_name,
                "chatMode": "query",
                "topN": int(top_n),
            },
            api_key=runtime_key,
        )
        data = json.loads(response_text) if response_text else {}
        workspace = data.get("workspace", {}) if isinstance(data, dict) else {}
        workspace_slug = workspace.get("slug") or ""
        # This is a hard boundary check. If a future AnythingLLM release changes
        # its slugifier, a workspace with a non-LanceDB-compatible namespace is
        # deleted immediately instead of becoming a document-ingestion trap.
        if 200 <= status < 300 and workspace_slug and not is_lancedb_safe_namespace(workspace_slug):
            cleanup_error = ""
            try:
                delete_status, delete_response = delete_json(
                    api_url.rstrip("/") + f"/api/v1/workspace/{workspace_slug}",
                    api_key=runtime_key,
                    timeout=60,
                )
                if not 200 <= delete_status < 300:
                    cleanup_error = delete_response[:500]
            except Exception as cleanup_exc:
                cleanup_error = str(cleanup_exc)
            return {
                "status": "unsafe_workspace_slug",
                "workspace_slug": "",
                "workspace_name": workspace.get("name") or workspace_name,
                "requested_workspace_name": requested_workspace_name,
                "workspace_name_sanitized": workspace_name,
                "workspace_name_collision_suffix": collision_suffix,
                "authentication_mode": authentication_mode,
                "workspace_template": workspace_template,
                "workspace_template_apply": {"status": "not_attempted", "message": "", "error": ""},
                "temporary_key_cleanup": cleanup,
                "error": (
                    f"AnythingLLM returned unsafe workspace slug `{workspace_slug}`; the app removed that workspace. "
                    f"LanceDB accepts only letters, numbers, underscores, hyphens, and periods."
                    + (f" Cleanup error: {cleanup_error}" if cleanup_error else "")
                ),
            }
        template_apply = {"status": "not_attempted", "message": "", "error": ""}
        if 200 <= status < 300 and workspace_slug and storage_dir and workspace_template.get("status") == "pass":
            template_apply = update_workspace_runtime_template_sqlite(
                Path(storage_dir),
                workspace_slug,
                workspace_template,
            )
        return {
            "status": "created" if 200 <= status < 300 and workspace_slug else "error",
            "workspace_slug": workspace_slug,
            "workspace_name": workspace.get("name") or workspace_name,
            "requested_workspace_name": requested_workspace_name,
            "workspace_name_sanitized": workspace_name,
            "workspace_name_collision_suffix": collision_suffix,
            "authentication_mode": authentication_mode,
            "workspace_template": workspace_template,
            "workspace_template_apply": template_apply,
            "temporary_key_cleanup": cleanup,
            "error": "" if 200 <= status < 300 else (data.get("error") if isinstance(data, dict) else response_text),
        }
    except Exception as exc:
        return {
            "status": "error",
            "workspace_slug": "",
            "workspace_name": workspace_name,
            "requested_workspace_name": requested_workspace_name,
            "workspace_name_sanitized": workspace_name,
            "workspace_name_collision_suffix": collision_suffix,
            "authentication_mode": authentication_mode,
            "workspace_template": workspace_template,
            "workspace_template_apply": {"status": "not_attempted", "message": "", "error": ""},
            "temporary_key_cleanup": cleanup,
            "error": str(exc),
        }
    finally:
        if temporary_key_id:
            cleanup.update(cleanup_temporary_desktop_api_key(api_url, temporary_key_id))


def cleanup_validation_workspace_documents(workspace_slug, storage_dir=None, document_folder_path=""):
    """Delete only the managed document folder belonging to a deleted validation workspace."""
    result = {"status": "not_applicable", "path": "", "error": ""}
    slug = str(workspace_slug or "").strip()
    if not slug:
        return result
    storage = Path(storage_dir) if storage_dir else default_anythingllm_storage_dir()
    documents_root = (storage / "documents").resolve()
    custom_documents_root = (documents_root / "custom-documents").resolve()
    expected_folder = (
        custom_documents_root
        / sanitize_anythingllm_folder_name(f"{slug}-docs")
    ).resolve()
    requested_folder = Path(document_folder_path).resolve() if document_folder_path else expected_folder
    result["path"] = str(requested_folder)
    try:
        requested_folder.relative_to(documents_root)
    except ValueError:
        result["status"] = "rejected_outside_documents_root"
        result["error"] = "Validation cleanup folder is outside AnythingLLM documents storage."
        return result
    if requested_folder in {documents_root, custom_documents_root}:
        result["status"] = "rejected_unmanaged_path"
        result["error"] = "Validation cleanup refuses to remove a broad AnythingLLM documents root."
        return result
    try:
        requested_folder.relative_to(custom_documents_root)
    except ValueError:
        result["status"] = "rejected_unmanaged_path"
        result["error"] = "Validation cleanup only removes managed folders under custom-documents."
        return result
    if not document_folder_path and requested_folder != expected_folder:
        result["status"] = "rejected_unmanaged_path"
        result["error"] = "Validation cleanup default folder did not match the managed workspace document folder."
        return result
    if not requested_folder.exists():
        result["status"] = "already_absent"
        return result
    try:
        shutil.rmtree(requested_folder)
        result["status"] = "deleted"
    except Exception as exc:
        result["status"] = "delete_failed"
        result["error"] = str(exc)
    return result


def delete_validation_workspace(api_url, workspace_slug, api_key=None, storage_dir=None, document_folder_path=""):
    if not workspace_slug:
        return {"status": "not_applicable", "error": ""}
    runtime_key, _ = resolve_anythingllm_api_key(api_url, api_key, storage_dir)
    temporary_key_id = None
    if not runtime_key:
        temporary_key = create_temporary_desktop_api_key(api_url)
        if temporary_key.get("status") != "created":
            return {
                "status": "authentication_required",
                "error": temporary_key.get("error", ""),
            }
        runtime_key = temporary_key["secret"]
        temporary_key_id = temporary_key["id"]
    result = {
        "status": "error",
        "error": "",
        "document_folder_cleanup": {"status": "not_attempted", "path": "", "error": ""},
    }
    try:
        status, response_text = delete_json(
            api_url.rstrip("/") + f"/api/v1/workspace/{workspace_slug}",
            api_key=runtime_key,
            timeout=60,
        )
        result["status"] = "deleted" if 200 <= status < 300 else "delete_failed"
        result["error"] = "" if 200 <= status < 300 else response_text
        if result["status"] == "deleted":
            result["document_folder_cleanup"] = cleanup_validation_workspace_documents(
                workspace_slug,
                storage_dir=storage_dir,
                document_folder_path=document_folder_path,
            )
            if result["document_folder_cleanup"].get("status") == "delete_failed":
                result["status"] = "deleted_with_document_cleanup_warning"
                result["error"] = result["document_folder_cleanup"].get("error") or "Managed document folder cleanup failed."
        return result
    except Exception as exc:
        result["error"] = str(exc)
        return result
    finally:
        if temporary_key_id:
            cleanup = cleanup_temporary_desktop_api_key(api_url, temporary_key_id)
            if cleanup.get("status") == "delete_failed" and not result.get("error"):
                result["error"] = cleanup.get("error", "")


def run_temporary_workspace_validation(
    api_url,
    api_key,
    storage_dir: Path,
    source_sha: str,
    payloads,
    upload_limit=0,
    top_n=8,
    upload_transport="raw_text",
    upload_plan_rows=None,
    cleanup_policy="cleanup_always",
    status_callback=None,
    embedder_probe_override=None,
):
    result = {
        "status": "not_run",
        "workspace_slug": "",
        "workspace_name": "",
        "workspace_create_status": "not_run",
        "upload_status": "not_run",
        "post_upload_status": "not_run",
        "runtime_validation_status": "not_run",
        "retention_status": "not_run",
        "post_upload_report": {},
        "runtime_validation_report": {},
        "upload_report": {},
        "workspace_template": {},
        "workspace_template_apply": {},
        "cleanup_policy": cleanup_policy,
        "cleanup_result": {"status": "not_run", "error": ""},
        "error": "",
    }
    if cleanup_policy not in {
        "retain_for_review",
        "cleanup_on_success",
        "cleanup_always",
    }:
        result["status"] = "invalid_cleanup_policy"
        result["error"] = f"Unsupported validation cleanup policy: {cleanup_policy}"
        return result
    if not api_url:
        result["status"] = "api_url_missing"
        result["error"] = "AnythingLLM API URL was not provided."
        return result
    workspace = create_validation_workspace(
        api_url,
        api_key=api_key,
        name_prefix=f"Chunk Survival Validation {source_sha[:8]}",
        top_n=top_n,
        storage_dir=storage_dir,
    )
    result["workspace_create_status"] = workspace.get("status", "error")
    result["workspace_slug"] = workspace.get("workspace_slug", "")
    result["workspace_name"] = workspace.get("workspace_name", "")
    result["workspace_template"] = workspace.get("workspace_template") or {}
    result["workspace_template_apply"] = workspace.get("workspace_template_apply") or {}
    if callable(status_callback):
        status_callback(
            "Creating temporary AnythingLLM validation workspace",
            {"stage": "workspace_creation", "workspace_slug": result["workspace_slug"]},
        )
    if workspace.get("status") != "created":
        result["status"] = "workspace_create_failed"
        result["error"] = workspace.get("error", "")
        return result
    expected_payloads = (
        upload_plan_rows_to_expected_payloads(upload_plan_rows or [])
        if str(upload_transport or "").strip().casefold() == "file_upload"
        else payloads
    )
    selected_expected_payloads = select_upload_payloads(expected_payloads, upload_limit)
    validation_reconciliation = {
        "started_at": None,
        "deadline_seconds": ANYTHINGLLM_VALIDATION_RECONCILIATION_TIMEOUT_SECONDS,
    }

    def validation_remaining_seconds():
        started_at = validation_reconciliation.get("started_at")
        if not started_at:
            return float(validation_reconciliation["deadline_seconds"])
        return max(0.0, float(validation_reconciliation["deadline_seconds"]) - (time.time() - float(started_at)))

    def begin_validation_reconciliation():
        if not validation_reconciliation.get("started_at"):
            validation_reconciliation["started_at"] = time.time()
        return validation_remaining_seconds()

    def verify_validation_embedding_batch(batch_report):
        """Resolve an accepted or timed-out batch from exact persisted vectors."""
        start_index = int(batch_report.get("start_index") or 0)
        end_index = int(batch_report.get("end_index") or start_index)
        expected_batch = selected_expected_payloads[start_index:end_index]
        if not expected_batch:
            return {"status": "error", "message": "No expected batch identities were available."}
        remaining = begin_validation_reconciliation()
        def report_validation_batch_observation(evidence, operator_state):
            if callable(status_callback):
                status_callback(
                    "Temporary validation: checking exact vectors",
                    {
                        "stage": "validation_batch_reconciliation",
                        "attempt": evidence.get("attempt"),
                        "observed": evidence.get("matching_vector_rows") or evidence.get("lancedb_matching_rows") or 0,
                        "expected": len(expected_batch),
                        "operator_status": operator_state,
                        "shared_reconciliation_remaining_seconds": round(validation_remaining_seconds(), 3),
                    },
                )

        polling = poll_post_upload(
            lambda: verify_anythingllm_post_upload(
                storage_dir,
                result["workspace_slug"],
                source_sha,
                expected_batch,
                upload_locations=(batch_report.get("locations") or []),
                observation_mode="fast",
            ),
            interval_seconds=2.0,
            timeout_seconds=min(45.0, remaining),
            hard_cap_seconds=min(45.0, remaining),
            observation_callback=report_validation_batch_observation,
            retryable_evidence_codes={"partial_vector_coverage"},
        )
        evidence = dict(polling.final_evidence)
        evidence.update(
            {
                "status": polling.status,
                "polling_attempts": polling.attempts,
                "polling_elapsed_seconds": polling.elapsed_seconds,
                "polling_observer_failures": polling.observer_failures,
                "shared_reconciliation_started_at_epoch": validation_reconciliation["started_at"],
                "shared_reconciliation_deadline_seconds": validation_reconciliation["deadline_seconds"],
            }
        )
        return evidence

    upload_report = maybe_upload_to_anythingllm(
        api_url,
        api_key,
        payloads,
        upload_limit=upload_limit,
        workspace_slug=result["workspace_slug"],
        upload_transport=upload_transport,
        upload_plan_rows=upload_plan_rows,
        storage_dir=storage_dir,
        folder_name=f"custom-documents/{result['workspace_slug']}-docs",
        status_callback=status_callback,
        batch_verifier=verify_validation_embedding_batch,
        embedding_batch_size=ANYTHINGLLM_VALIDATION_EMBEDDING_UPDATE_BATCH_SIZE,
        embedding_warmup_batch_size=ANYTHINGLLM_VALIDATION_WARMUP_BATCH_SIZE,
        embedding_warmup_batch_count=ANYTHINGLLM_VALIDATION_WARMUP_BATCH_COUNT,
    )
    result["upload_report"] = upload_report
    result["upload_status"] = upload_report.get("status", "not_run")
    if callable(status_callback):
        status_callback(
            "AnythingLLM upload submission finished; inspecting native indexing state",
            {
                "stage": "post_upload_observation",
                "workspace_slug": result["workspace_slug"],
                "upload_status": result["upload_status"],
                "uploaded": upload_report.get("uploaded", 0),
            },
        )
    if upload_report.get("uploaded", 0) > 0:
        # A successful update-embeddings response confirms submission, not that
        # the Desktop worker has finished materialising every vector.  Do one
        # bounded, auditable poll before querying retrieval.  Without it, a
        # large but healthy queue can be misclassified as a retrieval defect
        # while it is still actively indexing.
        def report_validation_document_observation(evidence, operator_state):
            if callable(status_callback):
                status_callback(
                    "Temporary validation: checking document-wide exact vectors",
                    {
                        "stage": "validation_document_reconciliation",
                        "attempt": evidence.get("attempt"),
                        "observed": evidence.get("matching_vector_rows") or evidence.get("lancedb_matching_rows") or 0,
                        "expected": len(selected_expected_payloads),
                        "operator_status": operator_state,
                    },
                )

        remaining = begin_validation_reconciliation()
        post_upload_poll = poll_post_upload(
            lambda: verify_anythingllm_post_upload(
                storage_dir,
                result["workspace_slug"],
                source_sha,
                selected_expected_payloads,
                upload_locations=(upload_report.get("locations") or []),
            ),
            interval_seconds=3.0,
            timeout_seconds=remaining,
            hard_cap_seconds=remaining,
            observation_callback=report_validation_document_observation,
            retryable_evidence_codes={"partial_vector_coverage"},
        )
        post_upload_report = dict(post_upload_poll.final_evidence)
        post_upload_report.update(
            {
                "polling_attempts": post_upload_poll.attempts,
                "polling_elapsed_seconds": post_upload_poll.elapsed_seconds,
                "polling_observer_failures": post_upload_poll.observer_failures,
                "shared_reconciliation_started_at_epoch": validation_reconciliation["started_at"],
                "shared_reconciliation_deadline_seconds": validation_reconciliation["deadline_seconds"],
                "shared_reconciliation_remaining_seconds": round(validation_remaining_seconds(), 3),
            }
        )
        if post_upload_report.get("status") in SUCCESSFUL_POST_UPLOAD_STATUSES:
            runtime_validation_report = validate_anythingllm_native_runtime(
                api_url,
                api_key,
                result["workspace_slug"],
                selected_expected_payloads,
                0,
                storage_dir,
                embedder_probe_override=embedder_probe_override,
            )
        else:
            runtime_validation_report = {
                "status": "not_run_post_upload_incomplete",
                "workspace_slug": result["workspace_slug"],
                "message": "Runtime retrieval was not queried because native vector coverage was incomplete.",
            }
    else:
        post_upload_report = {
            "status": "upload_failed",
            "classification": "not_checked",
            "message": "Chunk survival test upload did not complete.",
        }
        runtime_validation_report = {
            "status": "not_checked",
            "workspace_slug": result["workspace_slug"],
        }
    result["post_upload_report"] = post_upload_report
    result["runtime_validation_report"] = runtime_validation_report
    result["post_upload_status"] = post_upload_report.get("status", "not_run")
    result["runtime_validation_status"] = runtime_validation_report.get("status", "not_run")
    validation_succeeded = evidence_layers_succeeded(
        result["upload_status"],
        result["post_upload_status"],
        result["runtime_validation_status"],
    )
    cleanup_required = cleanup_policy == "cleanup_always" or (
        cleanup_policy == "cleanup_on_success" and validation_succeeded
    )
    if cleanup_required:
        result["cleanup_result"] = delete_validation_workspace(
            api_url,
            result["workspace_slug"],
            api_key=api_key,
            storage_dir=storage_dir,
            document_folder_path=(upload_report.get("document_folder_path") or ""),
        )
        cleanup_status = result["cleanup_result"].get("status")
        result["retention_status"] = (
            "cleaned_up"
            if cleanup_status == "deleted"
            else "cleanup_warning"
        )
    else:
        result["retention_status"] = (
            "left_visible_after_unsuccessful_validation"
            if cleanup_policy == "cleanup_on_success"
            else "left_visible_for_manual_review"
        )
    if validation_succeeded:
        result["status"] = "complete"
    elif result["upload_status"] not in SUCCESSFUL_UPLOAD_STATUSES:
        result["status"] = "upload_failed"
        result["error"] = str(
            upload_report.get("error")
            or next(
                (
                    row.get("error")
                    for row in (upload_report.get("errors") or [])
                    if isinstance(row, dict) and row.get("error")
                ),
                "AnythingLLM did not embed every submitted document.",
            )
        )
    elif result["post_upload_status"] not in SUCCESSFUL_POST_UPLOAD_STATUSES:
        result["status"] = "post_upload_failed"
        result["error"] = str(
            post_upload_report.get("message")
            or post_upload_report.get("error")
            or "Post-upload vector coverage did not pass."
        )
    else:
        result["status"] = "runtime_validation_failed"
        result["error"] = str(
            runtime_validation_report.get("error")
            or runtime_validation_report.get("message")
            or "AnythingLLM runtime validation did not pass."
        )
    return result


def should_run_chunk_survival_validation(selected: dict) -> bool:
    status = str((selected or {}).get("readiness_status") or "").strip().casefold()
    reasons = {
        str(reason).strip()
        for reason in ((selected or {}).get("readiness_reasons") or [])
        if str(reason).strip()
    }
    if status == "ready":
        return True
    if status != "needs_review":
        return False
    blocking_reasons = {
        "ocr_or_text_layer_failure_likely",
        "implausibly_low_text_coverage",
        "backend_text_coverage_disagreement",
        "excessive_replacement_characters",
        "native_header_metadata_does_not_survive_chunk_simulation",
    }
    return not reasons.intersection(blocking_reasons)


def upload_block_reason_for_readiness(selected: dict) -> str:
    """Return the narrow OCR/readability conditions that must withhold upload.

    ``needs_review`` also covers harmless structural uncertainty, so it is not
    itself an upload block. A material disagreement between OCR extractors is
    different: for a scanned source it is direct evidence that the prepared
    text may omit or corrupt content, and must be reviewed before ingestion.
    """
    reasons = {
        str(reason).strip()
        for reason in ((selected or {}).get("readiness_reasons") or [])
        if str(reason).strip()
    }
    if "photographed_spread_requires_manual_review" in reasons:
        return "photographed_spread_requires_manual_review"
    if "needs_unstructured_or_ocr" in reasons:
        return "needs_unstructured_or_ocr"
    ocr_assisted = (
        str((selected or {}).get("backend") or "").casefold() == "unstructured"
        and str((selected or {}).get("unstructured_strategy") or "").casefold()
        in {"hi_res", "ocr_only"}
    )
    if "backend_text_coverage_disagreement" in reasons and ocr_assisted:
        return "ocr_backend_text_coverage_disagreement"
    return ""


def get_anythingllm_metadata_schema(api_url, api_key=None):
    result = {
        "status": "source_contract",
        "schema": ANYTHINGLLM_RAW_TEXT_METADATA_FIELDS,
        "sources": ANYTHINGLLM_SOURCE_CONTRACT,
        "runtime_api_status": "not_checked",
        "error": "",
    }
    if not api_url:
        result["runtime_api_status"] = "skipped_missing_api_url"
        return result
    endpoint = api_url.rstrip("/") + "/api/v1/document/metadata-schema"
    runtime_key, authentication_mode = resolve_anythingllm_api_key(api_url, api_key)
    result["authentication_mode"] = authentication_mode
    temporary_key_id = None
    try:
        if not runtime_key and is_local_anythingllm_url(api_url):
            temporary_key = create_temporary_desktop_api_key(api_url)
            if temporary_key["status"] == "created":
                runtime_key = temporary_key["secret"]
                temporary_key_id = temporary_key["id"]
                result["authentication_mode"] = "temporary_desktop_api_key"
            else:
                result["authentication_mode"] = "unavailable"
                result["temporary_key_error"] = temporary_key.get("error", "")
        status, response_text = get_json(endpoint, api_key=runtime_key)
        runtime_data = json.loads(response_text)
        result["runtime_api_status"] = "reachable_authorized"
        result["http_status"] = status
        if isinstance(runtime_data, dict) and isinstance(runtime_data.get("schema"), dict):
            result["runtime_schema"] = runtime_data["schema"]
            result["schema_matches_source_contract"] = (
                set(runtime_data["schema"]) == set(ANYTHINGLLM_RAW_TEXT_METADATA_FIELDS)
            )
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        finally:
            exc.close()
        result["runtime_api_status"] = "reachable_authentication_failed"
        result["http_status"] = exc.code
        result["error"] = body
    except urllib.error.URLError as exc:
        result["runtime_api_status"] = "server_unreachable"
        result["error"] = str(exc.reason)
    except Exception as exc:
        result["runtime_api_status"] = "runtime_check_error"
        result["error"] = str(exc)
    finally:
        if temporary_key_id:
            result["temporary_key_cleanup"] = cleanup_temporary_desktop_api_key(
                api_url,
                temporary_key_id,
            )
    return result


def extract_document_location(response_text):
    try:
        data = json.loads(response_text)
    except Exception:
        return ""

    candidates = []
    if isinstance(data, dict):
        candidates.extend(
            [
                data.get("location"),
                data.get("url"),
                data.get("path"),
                data.get("document", {}).get("location") if isinstance(data.get("document"), dict) else None,
                data.get("document", {}).get("url") if isinstance(data.get("document"), dict) else None,
                data.get("document", {}).get("path") if isinstance(data.get("document"), dict) else None,
            ]
        )
        documents = data.get("documents")
        if isinstance(documents, list):
            for document in documents:
                if isinstance(document, dict):
                    candidates.extend([document.get("location"), document.get("url"), document.get("path")])
    for candidate in candidates:
        if candidate and isinstance(candidate, str):
            return candidate
    return ""


def select_upload_payloads(payloads, upload_limit=0, upload_indices=None):
    """Select native upload records in stable prepared-record order.

    ``upload_indices`` is an explicit 1-based list supplied by the UI.  It is
    intentionally distinct from the retired two-record diagnostic probe.
    """
    payloads = list(payloads or [])
    if upload_indices:
        selected = []
        for index in sorted(set(int(value) for value in upload_indices)):
            if index < 1 or index > len(payloads):
                raise ValueError(
                    f"Custom upload record {index} is outside the available 1-{len(payloads)} range."
                )
            selected.append(payloads[index - 1])
        return selected
    if upload_limit == 2 and len(payloads) > 2:
        return [payloads[0], payloads[len(payloads) // 2]]
    return payloads if not upload_limit or upload_limit <= 0 else payloads[:upload_limit]


def load_upload_plan_rows(upload_plan_path):
    rows = []
    path = Path(upload_plan_path or "")
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            rows.append(dict(row))
    return rows


def upload_plan_rows_to_expected_payloads(upload_rows):
    payloads = []
    for row in upload_rows:
        filename = row.get("filename") or ""
        text_content = ""
        text_file = Path(row.get("text_file") or "")
        if text_file.exists():
            try:
                text_content = text_file.read_text(encoding="utf-8")
            except Exception:
                text_content = ""
        payloads.append(
            {
                "filename": filename,
                "textContent": text_content,
                "metadata": {
                    "title": filename or row.get("title") or "",
                    "docAuthor": row.get("docAuthor") or "",
                    "description": " | ".join(
                        value
                        for value in [row.get("title") or "", row.get("description") or ""]
                        if value
                    ),
                    "docSource": row.get("docSource") or "",
                    "chunkSource": row.get("chunkSource") or "",
                }
            }
        )
    return payloads


def sanitize_anythingllm_folder_name(folder_name: str) -> str:
    value = re.sub(r"[<>:\"/\\\\|?*\x00-\x1F]+", "-", str(folder_name or "").strip())
    value = value.strip(" .")
    return value or "custom-documents"


def sanitize_anythingllm_relative_folder_path(folder_name: str) -> str:
    parts = []
    for raw_part in str(folder_name or "").replace("\\", "/").split("/"):
        raw_part = raw_part.strip()
        if not raw_part or raw_part in {".", ".."}:
            continue
        parts.append(sanitize_anythingllm_folder_name(raw_part))
    return "/".join(parts) or "custom-documents"


def document_title_folder_name(source_title: str, source_sha: str = "") -> str:
    title = safe_stem(normalize_text(source_title or "")) or "document"
    title = re.sub(r"-{2,}", "-", title).strip("-._ ")
    title = title[:72].rstrip("-._ ")
    if source_sha:
        return sanitize_anythingllm_folder_name(f"{title}-{source_sha[:8].lower()}")
    return sanitize_anythingllm_folder_name(title)


def managed_anythingllm_upload_folder_path(
    workspace_slug="",
    source_title="",
    source_sha="",
    create_document_folders=False,
    explicit_folder_name="",
):
    """Return a safe relative Documents path for one prepared PDF.

    The first component stays under ``custom-documents``. When document
    folders are enabled, each PDF gets a hash-qualified child folder. This is
    an explicit advanced layout: AnythingLLM Desktop 1.15's Documents drawer
    currently lists immediate ``custom-documents`` files but does not recurse
    into those managed child folders. The normal production path therefore
    leaves the option off so a completed upload has visible drawer evidence.
    """
    requested_folder_name = str(explicit_folder_name or "").strip()
    if not create_document_folders:
        return sanitize_anythingllm_relative_folder_path(requested_folder_name or "custom-documents")
    document_folder = document_title_folder_name(source_title, source_sha)
    if requested_folder_name:
        return sanitize_anythingllm_relative_folder_path(f"custom-documents/{requested_folder_name}/{document_folder}")
    return sanitize_anythingllm_relative_folder_path(f"custom-documents/{document_folder}")


def managed_anythingllm_upload_folder_name(
    workspace_slug="",
    source_title="",
    source_sha="",
    create_document_folders=False,
    explicit_folder_name="",
):
    # ``sanitize_anythingllm_folder_name("")`` intentionally returns the
    # legacy ``custom-documents`` fallback.  Do not call it until we know the
    # user actually supplied a folder: otherwise an omitted optional setting
    # silently defeats the managed per-workspace folder below. In Desktop
    # 1.15, foldered records are valid storage but are omitted by the visible
    # Documents drawer's non-recursive listing; callers should use that layout
    # only when they consciously trade drawer evidence for folder isolation.
    return managed_anythingllm_upload_folder_path(
        workspace_slug=workspace_slug,
        source_title=source_title,
        source_sha=source_sha,
        create_document_folders=create_document_folders,
        explicit_folder_name=explicit_folder_name,
    )


def choose_native_upload_transport(api_url, requested_transport="raw_text", upload_plan_rows=None, storage_dir=None):
    transport = str(requested_transport or "raw_text").strip().casefold() or "raw_text"
    if transport == "file_upload":
        return "file_upload"
    # Respect the selected raw-text transport. Promoting it implicitly because
    # local storage is visible forced every normal run to materialize thousands
    # of metadata-api files despite AnythingLLM raw-text upload being proven.
    return transport


def build_file_upload_rows_from_payloads(payloads, files_dir: Path):
    files_dir = Path(files_dir)
    files_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for index, payload in enumerate(payloads or [], start=1):
        metadata = payload.get("metadata", {}) if isinstance(payload, dict) else {}
        filename = str(payload.get("filename") or "").strip()
        if not filename:
            title = str(metadata.get("title") or f"upload-{index:05d}").strip()
            filename = safe_stem(title)[:140].rstrip("-._ ") + ".txt"
        text_path = files_dir / filename
        atomic_write_text(text_path, str(payload.get("textContent") or ""))
        rows.append(
            {
                "filename": filename,
                "title": str(metadata.get("title") or filename),
                "docAuthor": str(metadata.get("docAuthor") or ""),
                "description": str(metadata.get("description") or ""),
                "docSource": str(metadata.get("docSource") or ""),
                "chunkSource": str(metadata.get("chunkSource") or ""),
                "text_file": str(text_path),
            }
        )
    return rows


def submission_receipt_for_payload(
    payload,
    *,
    run_id="",
    workspace_slug="",
    transport="raw_text",
    state="submitted",
    correlation_id="",
    http_status=None,
    location="",
    error="",
    next_check="",
    prepared_payload_hash="",
):
    """Return a redacted, append-only submission/recovery receipt.

    The receipt intentionally contains identifiers and hashes only.  It never
    duplicates the API key or the prepared source text, while still providing
    enough evidence to reconcile an ambiguous POST without replaying it.
    """
    metadata = payload.get("metadata", {}) if isinstance(payload, dict) else {}
    text = str(payload.get("textContent") or "") if isinstance(payload, dict) else ""
    source = str(metadata.get("docSource") or "")
    pdf_hash = source.rsplit("/", 1)[-1] if "sha256/" in source else ""
    return {
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run_id": str(run_id or ""),
        "correlation_id": str(correlation_id or ""),
        "workspace_slug": str(workspace_slug or ""),
        "transport": str(transport or ""),
        "state": str(state or "submitted"),
        "pdf_sha256": pdf_hash,
        "prepared_payload_sha256": str(prepared_payload_hash or hashlib.sha256(text.encode("utf-8")).hexdigest()),
        "chunk_source": str(metadata.get("chunkSource") or ""),
        "request_time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "http_status": http_status,
        "document_location": str(location or ""),
        "error": str(error or "")[:500],
        "next_check": str(next_check or ""),
    }


def record_submission_receipt(receipt_path, payload, **kwargs):
    if not receipt_path:
        return {}
    receipt = submission_receipt_for_payload(payload, **kwargs)
    append_jsonl_receipt(Path(receipt_path), receipt)
    return receipt


def set_embedding_batch_lifecycle(batch_report, state, detail=""):
    """Record a durable, externally-observed embedding operation state.

    ``update-embeddings`` has no job identifier.  The ledger is therefore our
    operation journal: it records what the client submitted and what local
    AnythingLLM observations later established.  A client deadline is never
    represented as an embedding rejection.
    """
    lifecycle_state = str(state or "unknown")
    history = list(batch_report.get("state_history") or [])
    history.append(
        {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "state": lifecycle_state,
            "detail": str(detail or ""),
        }
    )
    batch_report["lifecycle_state"] = lifecycle_state
    batch_report["state_history"] = history
    return batch_report


def relocate_uploaded_document(storage_dir: Path, location: str, folder_name: str) -> tuple[str, str]:
    storage = Path(storage_dir or "")
    if not storage:
        return location, ""
    raw_location = str(location or "").strip()
    if not raw_location:
        return location, ""
    normalized_location = raw_location.replace("\\", "/")
    documents_root = (storage / "documents").resolve()
    absolute_candidate = Path(raw_location)
    if absolute_candidate.is_absolute():
        try:
            relative_path = absolute_candidate.resolve().relative_to(documents_root)
            normalized_location = str(relative_path).replace("\\", "/")
        except Exception:
            return raw_location, ""
    else:
        normalized_location = normalized_location.lstrip("/")
    if normalized_location.startswith("custom-documents/") is False:
        return raw_location, ""
    # Validate each path piece again at this filesystem boundary: an explicit
    # folder name must never turn into a traversal path.
    target_folder = sanitize_anythingllm_relative_folder_path(folder_name)
    if target_folder == "custom-documents":
        # AnythingLLM Desktop 1.15's Documents drawer compares the workspace
        # row's docpath to its own relative ``folder/file`` inventory. The
        # upload endpoint may return an absolute Windows path, but submitting
        # that path to update-embeddings stores it verbatim and leaves the
        # document invisible in the workspace panel. No move is needed at the
        # drawer root; normalize the endpoint response to its canonical
        # relative document location before embedding.
        return normalized_location.replace("\\", "/"), ""
    source_path = storage / "documents" / normalized_location
    if not source_path.exists():
        return raw_location, f"Uploaded document path was not found on disk: {source_path}"
    target_relative = f"{target_folder}/{Path(normalized_location).name}"
    target_path = (storage / "documents" / target_relative).resolve()
    try:
        target_path.relative_to(documents_root)
    except ValueError:
        return raw_location, "Managed upload target is outside AnythingLLM documents storage."
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source_path), str(target_path))
    return target_relative.replace("\\", "/"), ""


# AnythingLLM Desktop's Documents dialog submits all selected paths in one
# ``update-embeddings`` request, then processes those paths sequentially while
# emitting SSE progress events.  A concurrent request wave overlaps recursive
# splitting, OpenRouter embedding, and LanceDB writes; on this installation a
# four-request wave intermittently dropped documents even when every HTTP
# request returned 200.  Normal PDF ingestion therefore submits one complete
# Desktop-style queue per workspace.  The bounded batch scheduler remains for
# explicit recovery/diagnostic work, not the normal transport.
ANYTHINGLLM_EMBEDDING_SUBMISSION_STRATEGY = "desktop_queue"
ANYTHINGLLM_EMBEDDING_UPDATE_BATCH_SIZE = 2
ANYTHINGLLM_EMBEDDING_WARMUP_BATCH_SIZE = 1
ANYTHINGLLM_EMBEDDING_WARMUP_BATCH_COUNT = 1
# This is deliberately a hard cap, rather than a default.  Callers must not
# accidentally restore the unsafe four-to-six ramp by passing a larger limit.
ANYTHINGLLM_EMBEDDING_MAX_CONCURRENT_BATCHES = 1
ANYTHINGLLM_EMBEDDING_INITIAL_CONCURRENT_BATCHES = 1
ANYTHINGLLM_EMBEDDING_FAILURE_FALLBACK_CONCURRENT_BATCHES = 1
# This deadline applies to one ``update-embeddings`` HTTP request, not to a
# whole PDF. The first request gets a modest cold-start allowance; later
# requests learn from accepted warm-up throughput and their actual record
# count. Raw PDF pages belong in the whole-run ETA, not this request boundary.
ANYTHINGLLM_EMBEDDING_SUBMISSION_BOOTSTRAP_TIMEOUT_SECONDS = 240.0
ANYTHINGLLM_EMBEDDING_SUBMISSION_TIMEOUT_FLOOR_SECONDS = 180.0
ANYTHINGLLM_EMBEDDING_SUBMISSION_TIMEOUT_CAP_SECONDS = 480.0
ANYTHINGLLM_EMBEDDING_SUBMISSION_TIMEOUT_SAFETY_FACTOR = 1.75
ANYTHINGLLM_EMBEDDING_SUBMISSION_TIMEOUT_SETUP_SECONDS = 30.0
# ``update-embeddings`` is synchronous in Desktop even though its work is an
# asynchronous queue.  Waiting for the HTTP response for a whole document
# therefore made the normal path blind for the first four minutes of a long
# queue.  This is only a receipt deadline: after it expires we reconcile the
# already-submitted queue through SSE and exact local vector evidence, without
# issuing a duplicate request.
ANYTHINGLLM_DESKTOP_QUEUE_RECEIPT_TIMEOUT_SECONDS = 20.0
# A timed-out HTTP response is not an embedding verdict. Give the exact-vector
# reconciler enough time to observe slow sequential provider work, while still
# retaining a finite circuit-breaker boundary.
ANYTHINGLLM_EMBEDDING_RECONCILIATION_TIMEOUT_SECONDS = 480.0
# The first reconciliation window is finite, but a locally owned Desktop queue
# that is still proving forward progress must not be declared failed merely
# because its provider is slower than the original observation estimate. Every
# extension is evidence-backed and the whole run stays bounded by this cap.
ANYTHINGLLM_EMBEDDING_RECONCILIATION_ACTIVE_CAP_SECONDS = 3600.0
ANYTHINGLLM_EMBEDDING_RECONCILIATION_PROGRESS_GRACE_SECONDS = 90.0
ANYTHINGLLM_EMBEDDING_RECONCILIATION_STALL_SECONDS = 90.0
# Temporary validation uses one shorter, shared observation budget. Its
# per-batch checkpoint and final document check observe the same Desktop work;
# they must not add independent 45- and 180-second waits.
ANYTHINGLLM_VALIDATION_RECONCILIATION_TIMEOUT_SECONDS = 180.0
# HTTP 429 is an explicit refusal to start a request, unlike an interrupted
# response where Desktop may already have accepted the write. Only this narrow
# case may retry in the same run at reduced concurrency.
ANYTHINGLLM_EMBEDDING_SAFE_PARALLEL_FALLBACK_HTTP_STATUSES = frozenset({429})
# The serialized ingestion path may retry one *explicitly refused* request.
# This is deliberately narrower than generic retry logic: a timeout, transport
# failure, 5xx response, or failed vector check can still conceal accepted work.
ANYTHINGLLM_EMBEDDING_RATE_LIMIT_MAX_RETRIES = 1
ANYTHINGLLM_EMBEDDING_RATE_LIMIT_RETRY_SECONDS = 15.0


def embedding_submission_timeout_seconds(record_count: Any, observed_seconds_per_record: Any = None) -> float:
    """Choose a finite write deadline from accepted batch throughput.

    Prepared upload records are bounded text units, so PDF page count is not a
    useful predictor for an individual request. It remains available to the
    document-level ETA model.
    """
    records = max(1, int(record_count or 1))
    try:
        observed = float(observed_seconds_per_record)
    except (TypeError, ValueError):
        observed = 0.0
    if observed <= 0:
        return float(ANYTHINGLLM_EMBEDDING_SUBMISSION_BOOTSTRAP_TIMEOUT_SECONDS)
    predicted = observed * records
    budget = max(
        float(ANYTHINGLLM_EMBEDDING_SUBMISSION_TIMEOUT_FLOOR_SECONDS),
        float(ANYTHINGLLM_EMBEDDING_SUBMISSION_TIMEOUT_SETUP_SECONDS)
        + predicted * float(ANYTHINGLLM_EMBEDDING_SUBMISSION_TIMEOUT_SAFETY_FACTOR),
    )
    return round(
        min(float(ANYTHINGLLM_EMBEDDING_SUBMISSION_TIMEOUT_CAP_SECONDS), budget),
        1,
    )


def planned_embedding_batch_count(
    record_count,
    batch_size=ANYTHINGLLM_EMBEDDING_UPDATE_BATCH_SIZE,
    warmup_batch_size=ANYTHINGLLM_EMBEDDING_WARMUP_BATCH_SIZE,
    warmup_batch_count=ANYTHINGLLM_EMBEDDING_WARMUP_BATCH_COUNT,
):
    """Return the exact request count for the bounded warm-up batch policy."""
    remaining = max(0, int(record_count or 0))
    if remaining <= 0:
        return 0
    steady = max(1, int(batch_size or 1))
    warmup = max(0, int(warmup_batch_size or 0))
    warmups = max(0, int(warmup_batch_count or 0))
    if warmup <= 0 or warmup >= steady or warmups <= 0:
        return math.ceil(remaining / steady)
    used_warmups = min(warmups, math.ceil(remaining / warmup))
    remaining = max(0, remaining - used_warmups * warmup)
    return used_warmups + (math.ceil(remaining / steady) if remaining else 0)
# Temporary-workspace validation is deliberately more conservative than a
# normal upload. A 2-record warm-up followed by a 4-record request left the
# Desktop update endpoint indefinitely pending during the source-folder gate.
# A three-record steady-state request remains below the observed stalled
# four-record boundary while avoiding an unnecessary extra request for common
# five-to-eight-page validation PDFs. The first request remains a two-record
# warm-up, preserving a cheap, observable failure boundary. This affects
# disposable acceptance work only; normal app uploads use two-record requests
# with one active request and exact vector verification between requests.
ANYTHINGLLM_VALIDATION_EMBEDDING_UPDATE_BATCH_SIZE = 3
ANYTHINGLLM_VALIDATION_WARMUP_BATCH_SIZE = 2
ANYTHINGLLM_VALIDATION_WARMUP_BATCH_COUNT = 1
# A full storage/LanceDB poll after every five-record update was safe but made
# large PDFs impractical: the prior batch observed 10–40 seconds of read-only
# polling per update.  Checkpoints retain an early failure boundary and a
# durable audit trail while the final document-wide verifier proves coverage.
ANYTHINGLLM_EMBEDDING_VERIFICATION_CHECKPOINT_INTERVAL = 10

# LanceDB namespaces are derived from AnythingLLM workspace slugs. Do not rely
# on AnythingLLM's display-name slugifier: it has allowed apostrophes through,
# while LanceDB rejects them after documents have already been queued.
LANCEDB_NAMESPACE_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


def lancedb_safe_workspace_name(value, fallback="PDF workspace"):
    """Return a human-readable name whose server-derived slug is LanceDB-safe."""
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    # Remove possessive punctuation rather than joining two words with a
    # separator ("Vance's" -> "Vances"). All other unsafe punctuation becomes
    # whitespace before collapsing so generated slugs remain readable.
    ascii_value = ascii_value.replace("'", "")
    safe = re.sub(r"[^A-Za-z0-9._ -]+", " ", ascii_value)
    safe = re.sub(r"\s+", " ", safe).strip(" .-")
    return safe[:120] or fallback


def is_lancedb_safe_namespace(value):
    return bool(LANCEDB_NAMESPACE_PATTERN.fullmatch(str(value or "")))


def unique_lancedb_workspace_name(value, storage_dir=None):
    """Return a safe visible name that will not reuse a local workspace.

    AnythingLLM does not expose a collision behaviour that is safe to assume.
    Looking up both stored names and their slugs lets the app create ``Name 2``
    (then ``Name 3``) deterministically, rather than overwriting or silently
    selecting an existing workspace.
    """
    base = lancedb_safe_workspace_name(value)
    storage = Path(storage_dir) if storage_dir else None
    db_path = storage / "anythingllm.db" if storage else None
    existing_names = set()
    existing_slugs = set()
    if db_path and db_path.exists():
        try:
            con = sqlite_readonly_connection(db_path)
            try:
                for name, slug in con.execute("select name, slug from workspaces"):
                    existing_names.add(str(name or "").casefold())
                    existing_slugs.add(str(slug or "").casefold())
            finally:
                con.close()
        except Exception:
            # The returned slug is still validated after creation. A transient
            # read-only inspection issue must not invent a false collision.
            pass

    def available(candidate):
        return (
            candidate.casefold() not in existing_names
            and safe_stem(candidate).casefold() not in existing_slugs
        )

    if available(base):
        return base, 0
    for suffix in range(2, 10_000):
        candidate = f"{base[: max(1, 116 - len(str(suffix)))]} {suffix}"
        if available(candidate):
            return candidate, suffix
    raise RuntimeError("Could not choose a unique safe workspace name after 9,999 attempts.")


def _write_embedding_batch_ledger(ledger_path, workspace_slug, result):
    if not ledger_path:
        return
    ledger_path = Path(ledger_path)
    payload = {
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "workspace_slug": workspace_slug,
        "requested": result.get("requested", 0),
        "accepted": result.get("accepted", 0),
        "batch_size": result.get("batch_size", 0),
        "concurrent_batch_limit": result.get("concurrent_batch_limit", 1),
        "initial_concurrent_batches": result.get("initial_concurrent_batches", 1),
        "failure_fallback_concurrent_batches": result.get("failure_fallback_concurrent_batches", 1),
        "recommended_resume_parallelism": result.get("recommended_resume_parallelism"),
        "parallelism_schedule": result.get("parallelism_schedule", []),
        "submission_timeout_policy": result.get("submission_timeout_policy", {}),
        "verification_mode": result.get("verification_mode", "every_batch"),
        "verification_interval": result.get("verification_interval", 1),
        "deferred_verification_batches": result.get("deferred_verification_batches", []),
        "final_verification_required": bool(result.get("final_verification_required")),
        "batches": result.get("batches", []),
        "inflight_batch": result.get("inflight_batch"),
        "runtime_events": result.get("runtime_events", []),
        "errors": result.get("errors", []),
    }
    # Keep a recovery artifact beside the ordinary ledger.  It is deliberately
    # only a plan: no automatic retry is ever attempted after an ambiguous
    # Desktop timeout or failed searchability check.
    failed_batch = next(
        (batch for batch in payload["batches"] if batch.get("submission_state") not in {"accepted"}),
        None,
    )
    inflight_batch = payload.get("inflight_batch") or {}
    if not failed_batch and str(inflight_batch.get("submission_state") or "") in {
        "unresolved", "rejected", "verification_failed", "cancelled_before_submission"
    }:
        failed_batch = inflight_batch
    if failed_batch:
        failed_at = int(failed_batch.get("start_index") or 0)
        # A cooperative cancellation deliberately does not materialize batches
        # after the first unsubmitted one. Keep the complete original plan in
        # memory long enough to make recovery exact; deriving pending work from
        # the recorded batches alone would lose every never-created later batch.
        planned_locations = list(result.get("planned_locations") or [])
        if planned_locations:
            pending = planned_locations[failed_at:]
        else:
            pending = []
            for batch in payload["batches"]:
                if int(batch.get("start_index") or 0) >= failed_at:
                    pending.extend(batch.get("locations") or [])
        # A parallel wave can return an accepted sibling after another batch
        # in that same wave fails. Those locations were already handed to
        # AnythingLLM and must be reconciled, never blindly submitted again.
        accepted_after_failure = {
            str(location)
            for batch in payload["batches"]
            if int(batch.get("start_index") or 0) >= failed_at
            and str(batch.get("submission_state") or "") == "accepted"
            for location in (batch.get("locations") or [])
        }
        if accepted_after_failure:
            pending = [location for location in pending if str(location) not in accepted_after_failure]
        confirmed_locations = {
            str(location)
            for location in ((failed_batch.get("verification") or {}).get("confirmed_locations") or [])
            if str(location)
        }
        if confirmed_locations:
            pending = [location for location in pending if str(location) not in confirmed_locations]
        payload["recovery"] = {
            "state": "resume_available",
            "from_batch": failed_batch.get("batch"),
            "remaining_locations": pending,
            "accepted_concurrent_locations": sorted(accepted_after_failure),
            "operator_note": "Review the failed batch before explicitly resuming. The original run was not retried automatically.",
        }
    else:
        payload["recovery"] = {"state": "not_needed", "remaining_locations": []}
    write_json(ledger_path, payload)
    resume_path = ledger_path.with_name("resume-embedding-manifest.json")
    if failed_batch or resume_path.exists():
        # Overwrite a stale interim recovery file with ``not_needed`` once a
        # formerly ambiguous batch has been reconciled successfully.
        write_json(resume_path, payload)


def _update_workspace_embeddings_batched_serial(
    api_url,
    api_key,
    workspace_slug,
    locations,
    batch_size=ANYTHINGLLM_EMBEDDING_UPDATE_BATCH_SIZE,
    warmup_batch_size=ANYTHINGLLM_EMBEDDING_WARMUP_BATCH_SIZE,
    warmup_batch_count=ANYTHINGLLM_EMBEDDING_WARMUP_BATCH_COUNT,
    ledger_path=None,
    status_callback=None,
    batch_verifier=None,
    batch_inspector=None,
    cancel_callback=None,
    verification_mode="checkpoint",
    verification_interval=ANYTHINGLLM_EMBEDDING_VERIFICATION_CHECKPOINT_INTERVAL,
    adaptive_single_record_threshold_seconds=60.0,
    submission_timeout_override=None,
):
    """Submit a bounded sequence of embedding updates and retain partial progress.

    AnythingLLM accepts a list of document locations for each update.  Sending
    hundreds of one-record files in one request can leave the client waiting
    for a very long indexing operation (or time out while Desktop continues in
    the background).  The API response still represents *acceptance* of a
    batch, not proof that every vector is searchable; callers retain their
    post-upload/observer checks for that separate fact.
    """
    unique_locations = list(dict.fromkeys(str(location) for location in locations if location))
    try:
        normalized_batch_size = max(1, int(batch_size or ANYTHINGLLM_EMBEDDING_UPDATE_BATCH_SIZE))
    except (TypeError, ValueError):
        normalized_batch_size = ANYTHINGLLM_EMBEDDING_UPDATE_BATCH_SIZE

    normalized_verification_mode = str(verification_mode or "checkpoint").strip().casefold()
    if normalized_verification_mode not in {"checkpoint", "every_batch", "none"}:
        normalized_verification_mode = "checkpoint"
    try:
        normalized_verification_interval = max(1, int(verification_interval or 1))
    except (TypeError, ValueError):
        normalized_verification_interval = ANYTHINGLLM_EMBEDDING_VERIFICATION_CHECKPOINT_INTERVAL
    try:
        normalized_warmup_size = max(0, int(warmup_batch_size or 0))
        normalized_warmup_count = max(0, int(warmup_batch_count or 0))
    except (TypeError, ValueError):
        normalized_warmup_size = 0
        normalized_warmup_count = 0
    if normalized_warmup_size >= normalized_batch_size:
        normalized_warmup_size = 0
        normalized_warmup_count = 0
    result = {
        "accepted": 0,
        "requested": len(unique_locations),
        "planned_locations": unique_locations,
        "batch_size": normalized_batch_size,
        "warmup_batch_size": normalized_warmup_size,
        "warmup_batch_count": normalized_warmup_count,
        "verification_mode": normalized_verification_mode,
        "verification_interval": normalized_verification_interval,
        "submission_timeout_policy": {
            "bootstrap_seconds": ANYTHINGLLM_EMBEDDING_SUBMISSION_BOOTSTRAP_TIMEOUT_SECONDS,
            "floor_seconds": ANYTHINGLLM_EMBEDDING_SUBMISSION_TIMEOUT_FLOOR_SECONDS,
            "cap_seconds": ANYTHINGLLM_EMBEDDING_SUBMISSION_TIMEOUT_CAP_SECONDS,
            "safety_factor": ANYTHINGLLM_EMBEDDING_SUBMISSION_TIMEOUT_SAFETY_FACTOR,
            "observed_seconds_per_record": None,
        },
        "deferred_verification_batches": [],
        "final_verification_required": bool(batch_verifier),
        "batches": [],
        "runtime_events": [],
        "errors": [],
    }
    _write_embedding_batch_ledger(ledger_path, workspace_slug, result)
    endpoint = api_url.rstrip("/") + f"/api/v1/workspace/{workspace_slug}/update-embeddings"
    batch_plan = []
    start = 0
    while start < len(unique_locations):
        use_warmup = len(batch_plan) < normalized_warmup_count
        size = normalized_warmup_size if use_warmup else normalized_batch_size
        end = min(len(unique_locations), start + size)
        batch_plan.append((start, end))
        start = end
    total_batches = len(batch_plan)
    batch_index = 0
    observed_seconds_per_record = None
    while batch_index < len(batch_plan):
        start, end = batch_plan[batch_index]
        batch_number = batch_index + 1
        batch = unique_locations[start:end]
        if submission_timeout_override is None:
            submission_timeout_seconds = embedding_submission_timeout_seconds(
                len(batch), observed_seconds_per_record
            )
            submission_timeout_basis = (
                "bootstrap" if observed_seconds_per_record is None else "accepted_warmup_throughput"
            )
        else:
            submission_timeout_seconds = max(1.0, float(submission_timeout_override))
            submission_timeout_basis = "receipt_deadline_override"
        batch_report = {
            "operation_id": f"embedding-{uuid.uuid4().hex}",
            "batch": batch_number,
            "total_batches": total_batches,
            "start_index": start,
            "end_index": start + len(batch),
            "requested": len(batch),
            "accepted": 0,
            "locations": batch,
            "submission_state": "submitting",
            "timing_event": "submission_started",
            "submission_timeout_seconds": submission_timeout_seconds,
            "submission_timeout_basis": submission_timeout_basis,
            "observed_seconds_per_record": observed_seconds_per_record,
        }
        set_embedding_batch_lifecycle(batch_report, "pending_submission")
        unresolved_error = None
        batch_started = time.perf_counter()
        if callable(cancel_callback) and cancel_callback():
            batch_report["submission_state"] = "cancelled_before_submission"
            batch_report["error"] = "The operator requested a stop before this batch was submitted."
            set_embedding_batch_lifecycle(batch_report, "cancelled_before_submission", batch_report["error"])
            result["errors"].append(
                {"endpoint": "operator-cancellation", "batch": batch_number, "error": batch_report["error"]}
            )
            result["batches"].append(batch_report)
            _write_embedding_batch_ledger(ledger_path, workspace_slug, result)
            if callable(status_callback):
                status_callback("Stop requested; no later AnythingLLM batches were submitted", dict(batch_report))
            break
        if callable(batch_inspector):
            preflight_evidence: dict[str, Any]
            try:
                observed_evidence = batch_inspector(dict(batch_report))
                preflight_evidence = (
                    cast(dict[str, Any], observed_evidence)
                    if isinstance(observed_evidence, dict)
                    else {}
                )
            except Exception as exc:
                preflight_evidence = {"status": "observer_error", "error": str(exc)}
            preflight_observed = int(
                preflight_evidence.get("matching_vector_rows")
                or preflight_evidence.get("lancedb_matching_rows")
                or 0
            )
            batch_report["pre_submission_evidence"] = preflight_evidence
            if (
                str(preflight_evidence.get("status") or "") in REVIEWABLE_POST_UPLOAD_STATUSES
                and preflight_observed >= len(batch)
            ):
                batch_report.update({
                    "accepted": len(batch),
                    "submission_state": "accepted",
                    "acceptance_basis": "exact_vectors_preexisted_before_submission",
                    "searchability_proven": True,
                    "submission_seconds": 0.0,
                    "verification_seconds": 0.0,
                    "verification": preflight_evidence,
                    "batch_elapsed_seconds": round(time.perf_counter() - batch_started, 4),
                    "timing_event": "batch_completed",
                })
                set_embedding_batch_lifecycle(
                    batch_report,
                    "vector_observed",
                    "Exact vector evidence existed before this submission; duplicate request skipped.",
                )
                result["accepted"] += len(batch)
                result["batches"].append(batch_report)
                _write_embedding_batch_ledger(ledger_path, workspace_slug, result)
                if callable(status_callback):
                    status_callback(
                        f"AnythingLLM batch {batch_number} was already searchable; duplicate submission skipped",
                        dict(batch_report),
                    )
                batch_index += 1
                continue
        result["inflight_batch"] = dict(batch_report)
        set_embedding_batch_lifecycle(batch_report, "submitted", "POST /update-embeddings started.")
        _write_embedding_batch_ledger(ledger_path, workspace_slug, result)
        if callable(status_callback):
            status_callback(
                f"Submitting AnythingLLM batch {batch_number} of {total_batches} "
                f"({len(batch)} records)",
                dict(batch_report),
            )
        try:
            submission_started = time.perf_counter()
            rate_limit_retries = 0
            while True:
                status, response_text = post_json(
                    endpoint,
                    {"adds": batch, "deletes": []},
                    api_key=api_key,
                    timeout=max(1, int(math.ceil(submission_timeout_seconds))),
                )
                if (
                    status != 429
                    or rate_limit_retries >= ANYTHINGLLM_EMBEDDING_RATE_LIMIT_MAX_RETRIES
                ):
                    break
                rate_limit_retries += 1
                batch_report["rate_limit_retry_count"] = rate_limit_retries
                batch_report["rate_limit_retry_seconds"] = ANYTHINGLLM_EMBEDDING_RATE_LIMIT_RETRY_SECONDS
                retry_message = (
                    "AnythingLLM explicitly rate-limited this serial request; "
                    f"waiting {ANYTHINGLLM_EMBEDDING_RATE_LIMIT_RETRY_SECONDS:.0f} seconds before its one safe retry."
                )
                set_embedding_batch_lifecycle(batch_report, "rate_limited_waiting_retry", retry_message)
                result["runtime_events"].append({
                    "event": "rate_limit_retry",
                    "batch": batch_number,
                    "retry_count": rate_limit_retries,
                    "retry_seconds": ANYTHINGLLM_EMBEDDING_RATE_LIMIT_RETRY_SECONDS,
                })
                result["inflight_batch"] = dict(batch_report)
                _write_embedding_batch_ledger(ledger_path, workspace_slug, result)
                if callable(status_callback):
                    status_callback(
                        (
                            f"AnythingLLM rate-limited batch {batch_number}; retrying once in "
                            f"{ANYTHINGLLM_EMBEDDING_RATE_LIMIT_RETRY_SECONDS:.0f} seconds at one active request"
                        ),
                        dict(batch_report),
                    )
                time.sleep(ANYTHINGLLM_EMBEDDING_RATE_LIMIT_RETRY_SECONDS)
                set_embedding_batch_lifecycle(
                    batch_report,
                    "submitted",
                    "Retrying the explicitly refused request at one active request.",
                )
                result["inflight_batch"] = dict(batch_report)
                _write_embedding_batch_ledger(ledger_path, workspace_slug, result)
            batch_report["http_status"] = status
            if 200 <= status < 300:
                batch_report["accepted"] = len(batch)
                batch_report["submission_state"] = "accepted"
                batch_report["acceptance_basis"] = "http_2xx_submission_only"
                batch_report["searchability_proven"] = False
                result["accepted"] += len(batch)
                set_embedding_batch_lifecycle(
                    batch_report,
                    "awaiting_observation",
                    "AnythingLLM accepted the request; attachment and retrieval are still being observed.",
                )
            else:
                error = {
                    "status": status,
                    "endpoint": "update-embeddings",
                    "batch": batch_number,
                    "response": response_text[:500],
                }
                batch_report["error"] = error["response"] or f"HTTP {status}"
                batch_report["submission_state"] = "rejected"
                result["errors"].append(error)
                set_embedding_batch_lifecycle(batch_report, "rejected", batch_report["error"])
            batch_report["submission_seconds"] = round(time.perf_counter() - submission_started, 4)
        except Exception as exc:
            exception_text = str(exc)
            timeout_like = "timed out" in exception_text.casefold() or "timeout" in exception_text.casefold()
            error = {
                "error": exception_text,
                "endpoint": "update-embeddings",
                "batch": batch_number,
                "classification": "client_timeout_submission_unknown" if timeout_like else "client_transport_submission_unknown",
            }
            batch_report["error"] = error["error"]
            # A client timeout does not prove that Desktop rejected the work.
            # Preserve that uncertainty and do not automatically retry it.
            batch_report["submission_state"] = "unresolved"
            # The short receipt deadline is deliberately not a second
            # embedding deadline.  It starts one shared, durable observation
            # budget which later document-wide checks must honour rather than
            # beginning another full 480-second wait.
            batch_report["receipt_deadline_elapsed"] = bool(timeout_like)
            batch_report["reconciliation_started_at_epoch"] = time.time()
            batch_report["reconciliation_deadline_seconds"] = (
                ANYTHINGLLM_EMBEDDING_RECONCILIATION_TIMEOUT_SECONDS
            )
            result["errors"].append(error)
            set_embedding_batch_lifecycle(
                batch_report,
                "reconciliation_pending",
                "The client did not receive a final response; observing AnythingLLM before any retry.",
            )
            unresolved_error = error
            batch_report["submission_seconds"] = round(time.perf_counter() - submission_started, 4)
            result["inflight_batch"] = dict(batch_report)
            _write_embedding_batch_ledger(ledger_path, workspace_slug, result)
            if callable(status_callback):
                status_callback(
                    (
                        f"AnythingLLM batch {batch_number} response timed out; reconciling exact vectors before deciding whether anything failed"
                        if timeout_like else
                        f"AnythingLLM batch {batch_number} response was interrupted; reconciling exact vectors before deciding whether anything failed"
                    ),
                    dict(batch_report),
                )
        if callable(status_callback) and batch_report["submission_state"] == "accepted":
            batch_report["timing_event"] = "submission_completed"
            status_callback(
                f"AnythingLLM batch {batch_number} submission accepted; awaiting vector evidence",
                dict(batch_report),
            )
        if batch_report["submission_state"] == "accepted":
            accepted_count = max(1, int(batch_report.get("accepted") or len(batch)))
            observed_rate = float(batch_report.get("submission_seconds") or 0.0) / accepted_count
            if observed_rate > 0:
                observed_seconds_per_record = max(
                    float(observed_seconds_per_record or 0.0), observed_rate
                )
                result["submission_timeout_policy"]["observed_seconds_per_record"] = round(
                    observed_seconds_per_record, 4
                )
        is_first_batch = batch_number == 1
        is_last_batch = batch_number == total_batches
        is_checkpoint = batch_number % normalized_verification_interval == 0
        should_verify = (
            normalized_verification_mode == "every_batch"
            or (normalized_verification_mode == "checkpoint" and (is_first_batch or is_last_batch or is_checkpoint))
            or batch_report["submission_state"] == "unresolved"
        )
        batch_report["verification_required"] = bool(should_verify and callable(batch_verifier))
        if batch_report["submission_state"] in {"accepted", "unresolved"} and callable(batch_verifier) and should_verify:
            was_unresolved = batch_report["submission_state"] == "unresolved"
            batch_report["timing_event"] = "verification_started"
            if callable(status_callback):
                status_callback(
                    f"Verifying AnythingLLM batch {batch_number} of {total_batches}",
                    dict(batch_report),
                )
            verification_started = time.perf_counter()
            try:
                verification_payload = batch_verifier(dict(batch_report))
                verification: dict[str, Any] = (
                    dict(verification_payload)
                    if isinstance(verification_payload, dict)
                    else {}
                )
            except Exception as exc:
                verification = {"status": "error", "error": str(exc)}
            batch_report["verification_seconds"] = round(time.perf_counter() - verification_started, 4)
            batch_report["verification"] = verification
            if verification.get("reconciliation_effective_deadline_seconds") is not None:
                # Preserve the active, evidence-backed deadline in the
                # durable ledger so the document-level final read does not
                # mistakenly believe that the original 480-second window has
                # already exhausted a still-moving Desktop queue.
                batch_report["reconciliation_effective_deadline_seconds"] = float(
                    verification["reconciliation_effective_deadline_seconds"]
                )
                batch_report["reconciliation_deadline_extensions"] = int(
                    verification.get("reconciliation_deadline_extensions") or 0
                )
            verification_status = str(verification.get("status") or "incomplete")
            verification_observed = int(
                verification.get("matching_vector_rows")
                or verification.get("lancedb_matching_rows")
                or 0
            )
            exact_vector_coverage = verification_observed >= len(batch)
            # ``pass_with_review`` means an inspection layer could not make a
            # clean storage assertion (for example, SQLite was locked while
            # Desktop was writing). It is never proof that this batch's exact
            # vectors exist. A timeout must be recovered only by the full
            # batch's provenance-matched vector count, not a reviewable
            # observer outcome with zero or partial rows.
            batch_report["searchability_proven"] = bool(
                verification_status in {"pass", "pass_with_review"}
                and exact_vector_coverage
            )
            if not was_unresolved and batch_report["searchability_proven"]:
                set_embedding_batch_lifecycle(
                    batch_report,
                    "vector_observed",
                    "Exact vector evidence was observed after the accepted submission.",
                )
            if was_unresolved and (
                batch_report["searchability_proven"]
                or verification_status == "workspace_attached_pending_vectors"
            ):
                batch_report["submission_state"] = "accepted"
                batch_report["accepted"] = len(batch)
                batch_report["acceptance_basis"] = (
                    "vector_observed_after_client_timeout"
                    if batch_report["searchability_proven"]
                    else "workspace_attached_after_client_timeout"
                )
                result["accepted"] += len(batch)
                if unresolved_error in result["errors"]:
                    result["errors"].remove(unresolved_error)
                set_embedding_batch_lifecycle(
                    batch_report,
                    "vector_observed" if batch_report["searchability_proven"] else "workspace_attached",
                    "Recovered after a client timeout using local AnythingLLM observation.",
                )
                result["runtime_events"].append(
                    {
                        **dict(unresolved_error or {}),
                        "classification": (
                            "client_timeout_recovered_by_vector_observation"
                            if batch_report["searchability_proven"]
                            else "client_timeout_recovered_by_workspace_attachment"
                        ),
                        "verification_status": verification_status,
                    }
                )
            elif (
                not was_unresolved
                and verification_status == "timeout"
                and normalized_verification_mode == "every_batch"
            ):
                # Ingestion uses this strict mode because a second request
                # while the first still has no exact vector evidence recreates
                # the fan-out that drops documents in AnythingLLM Desktop.
                # Keep the accepted write recoverable, but do not submit a
                # later batch until reconciliation has proved it searchable.
                batch_report["submission_state"] = "reconciliation_pending"
                batch_report["error"] = (
                    "AnythingLLM accepted this batch but did not expose exact vectors "
                    "within the verification window; later batches were withheld."
                )
                result["errors"].append(
                    {
                        "endpoint": "batch-searchability-check",
                        "batch": batch_number,
                        "status": "pending_delayed_indexing",
                        "error": batch_report["error"],
                    }
                )
                batch_report["verification"] = {
                    **verification,
                    "status": "pending_delayed_indexing",
                    "message": batch_report["error"],
                }
                result["deferred_verification_batches"].append(batch_number)
                set_embedding_batch_lifecycle(
                    batch_report,
                    "reconciliation_pending",
                    "Exact vector evidence is pending; later submissions are withheld.",
                )
            elif not was_unresolved and verification_status == "timeout":
                # A 2xx embedding submission has been accepted, but Desktop
                # may not expose its LanceDB rows within this short checkpoint.
                # Do not turn that delayed materialization into a false
                # permanent failure or abandon later accepted batches.  The
                # mandatory document-wide observation decides searchability.
                batch_report["verification"] = {
                    **verification,
                    "status": "pending_delayed_indexing",
                    "message": (
                        "AnythingLLM accepted this batch, but its vectors were not visible "
                        "during the checkpoint. Searchability will be decided by the final "
                        "document-wide observation."
                    ),
                }
                result["deferred_verification_batches"].append(batch_number)
                result["runtime_events"].append(
                    {
                        "event": "batch_vector_visibility_delayed",
                        "batch": batch_number,
                        "checkpoint_status": verification_status,
                        "reason": "Accepted upload retained for final document-wide vector observation.",
                    }
                )
                set_embedding_batch_lifecycle(
                    batch_report,
                    "awaiting_observation",
                    "Accepted request is still indexing; final document observation remains required.",
                )
            elif not was_unresolved and verification_status not in {"pass", "pass_with_review"}:
                batch_report["submission_state"] = "verification_failed"
                batch_report["error"] = verification.get("message") or verification.get("error") or (
                    f"AnythingLLM did not make batch {batch_number} searchable."
                )
                result["errors"].append(
                    {
                        "endpoint": "batch-searchability-check",
                        "batch": batch_number,
                        "status": verification_status,
                        "error": batch_report["error"],
                    }
                )
                set_embedding_batch_lifecycle(batch_report, "verification_failed", batch_report["error"])
            elif was_unresolved:
                batch_report["timeout_recovery_verification"] = verification
        elif batch_report["submission_state"] == "accepted" and callable(batch_verifier):
            batch_report["verification"] = {
                "status": "deferred_to_checkpoint",
                "reason": (
                    f"Batch {batch_number} is recorded as accepted; targeted verification is scheduled at "
                    f"batch {min(total_batches, ((batch_number // normalized_verification_interval) + 1) * normalized_verification_interval)} "
                    "and the mandatory document-wide final verification."
                ),
            }
            result["deferred_verification_batches"].append(batch_number)
        batch_report["batch_elapsed_seconds"] = round(time.perf_counter() - batch_started, 4)
        batch_report["timing_event"] = "batch_completed"
        result["batches"].append(batch_report)
        result.pop("inflight_batch", None)
        _write_embedding_batch_ledger(ledger_path, workspace_slug, result)
        if callable(status_callback):
            if batch_report["submission_state"] == "accepted":
                verification_status = str((batch_report.get("verification") or {}).get("status") or "")
                if verification_status == "deferred_to_checkpoint":
                    message = (
                        f"AnythingLLM batch {batch_number} accepted; per-batch storage verification deferred "
                        "to the next checkpoint and final document verification"
                    )
                elif batch_report.get("searchability_proven"):
                    message = f"AnythingLLM batch {batch_number} is searchable; document-list evidence will be finalized after upload"
                elif verification_status == "pass_with_review":
                    message = (
                        f"AnythingLLM batch {batch_number} could not be fully observed while Desktop was writing; "
                        "exact vector reconciliation remains required"
                    )
                else:
                    message = f"AnythingLLM batch {batch_number} is searchable; proceeding to the next batch"
                status_callback(message, dict(batch_report))
            else:
                status_callback(
                    f"AnythingLLM batch {batch_number} is {batch_report['submission_state']}; later batches were not submitted",
                    dict(batch_report),
                )
        # If the one-record warm-up itself is slow, keep all later requests at
        # one record. This adds request boundaries but does not add embedding
        # work because AnythingLLM processes the documents sequentially.
        if (
            batch_number == 1
            and not result["errors"]
            and normalized_batch_size > 1
            and end < len(unique_locations)
            and float(batch_report.get("batch_elapsed_seconds") or 0)
            >= max(0.0, float(adaptive_single_record_threshold_seconds or 0))
        ):
            batch_plan = batch_plan[: batch_index + 1] + [
                (index, index + 1) for index in range(end, len(unique_locations))
            ]
            total_batches = len(batch_plan)
            result["adaptive_single_record_mode"] = True
            result["adaptive_reason"] = "one_record_warmup_exceeded_60_seconds"
            if callable(status_callback):
                status_callback(
                    "AnythingLLM warm-up was slow; later requests are limited to one record for reliability",
                    dict(batch_report),
                )
        batch_index += 1
        # Later batches are not attempted after a rejected/failed update. This
        # makes a partial result deterministic and safe to retry with only the
        # remaining prepared records.
        if result["errors"]:
            break
    return result


def update_workspace_embeddings_batched(
    api_url,
    api_key,
    workspace_slug,
    locations,
    batch_size=ANYTHINGLLM_EMBEDDING_UPDATE_BATCH_SIZE,
    warmup_batch_size=ANYTHINGLLM_EMBEDDING_WARMUP_BATCH_SIZE,
    warmup_batch_count=ANYTHINGLLM_EMBEDDING_WARMUP_BATCH_COUNT,
    ledger_path=None,
    status_callback=None,
    batch_verifier=None,
    batch_inspector=None,
    cancel_callback=None,
    verification_mode="checkpoint",
    verification_interval=ANYTHINGLLM_EMBEDDING_VERIFICATION_CHECKPOINT_INTERVAL,
    adaptive_single_record_threshold_seconds=60.0,
    concurrent_batch_limit=1,
    initial_concurrent_batches=ANYTHINGLLM_EMBEDDING_INITIAL_CONCURRENT_BATCHES,
    submission_timeout_override=None,
):
    """Submit bounded embedding requests.

    The normal PDF flow admits one active request at a time. AnythingLLM
    exposes no asynchronous job receipt, so uncertain submissions are kept for
    reconciliation rather than retried speculatively.
    """
    try:
        limit = max(1, min(int(concurrent_batch_limit or 1), ANYTHINGLLM_EMBEDDING_MAX_CONCURRENT_BATCHES))
    except (TypeError, ValueError):
        limit = 1
    if limit <= 1:
        return _update_workspace_embeddings_batched_serial(
            api_url, api_key, workspace_slug, locations,
            batch_size=batch_size,
            warmup_batch_size=warmup_batch_size,
            warmup_batch_count=warmup_batch_count,
            ledger_path=ledger_path,
            status_callback=status_callback,
            batch_verifier=batch_verifier,
            batch_inspector=batch_inspector,
            cancel_callback=cancel_callback,
            verification_mode=verification_mode,
            verification_interval=verification_interval,
            adaptive_single_record_threshold_seconds=adaptive_single_record_threshold_seconds,
            submission_timeout_override=submission_timeout_override,
        )

    unique_locations = list(dict.fromkeys(str(location) for location in locations if location))
    steady_size = max(1, int(batch_size or ANYTHINGLLM_EMBEDDING_UPDATE_BATCH_SIZE))
    # An embedding-update response is synchronous in AnythingLLM Desktop. A
    # one-record warm-up therefore costs a full provider/LanceDB round trip
    # without making the following four-request wave safer. Keep the request
    # body at two records but begin the bounded concurrency test at four.
    warmup_size = 0
    warmups = 0
    if warmup_size >= steady_size:
        warmup_size, warmups = 0, 0
    plan, start = [], 0
    while start < len(unique_locations):
        size = warmup_size if len(plan) < warmups else steady_size
        end = min(len(unique_locations), start + max(1, size))
        plan.append((start, end))
        start = end
    total = len(plan)
    result = {
        "accepted": 0,
        "requested": len(unique_locations),
        "planned_locations": unique_locations,
        "batch_size": steady_size,
        "warmup_batch_size": warmup_size,
        "warmup_batch_count": warmups,
        "verification_mode": str(verification_mode or "checkpoint"),
        "verification_interval": max(1, int(verification_interval or 1)),
        "concurrent_batch_limit": limit,
        "initial_concurrent_batches": min(limit, max(1, int(initial_concurrent_batches or 1))),
        "failure_fallback_concurrent_batches": min(
            limit, ANYTHINGLLM_EMBEDDING_FAILURE_FALLBACK_CONCURRENT_BATCHES
        ),
        "parallelism_schedule": [],
        "submission_timeout_policy": {
            "bootstrap_seconds": ANYTHINGLLM_EMBEDDING_SUBMISSION_BOOTSTRAP_TIMEOUT_SECONDS,
            "floor_seconds": ANYTHINGLLM_EMBEDDING_SUBMISSION_TIMEOUT_FLOOR_SECONDS,
            "cap_seconds": ANYTHINGLLM_EMBEDDING_SUBMISSION_TIMEOUT_CAP_SECONDS,
            "safety_factor": ANYTHINGLLM_EMBEDDING_SUBMISSION_TIMEOUT_SAFETY_FACTOR,
            "observed_seconds_per_record": None,
        },
        "deferred_verification_batches": [],
        "final_verification_required": bool(batch_verifier),
        "batches": [], "runtime_events": [], "errors": [],
    }
    _write_embedding_batch_ledger(ledger_path, workspace_slug, result)
    if not plan:
        return result

    try:
        parallelism = min(limit, max(1, int(initial_concurrent_batches or 1)))
    except (TypeError, ValueError):
        parallelism = min(limit, ANYTHINGLLM_EMBEDDING_INITIAL_CONCURRENT_BATCHES)
    # Keep a queue of logical batch indexes instead of a single contiguous
    # cursor. A safe retry after a 429 must revisit only the explicitly
    # rejected request; siblings already acknowledged in the same wave must
    # never be submitted again.
    pending_plan_indexes = list(range(total))
    while pending_plan_indexes:
        if callable(cancel_callback) and cancel_callback():
            plan_index = pending_plan_indexes[0]
            start_index, end_index = plan[plan_index]
            cancelled = {
                "batch": plan_index + 1, "total_batches": total,
                "start_index": start_index, "end_index": end_index,
                "requested": end_index - start_index, "accepted": 0,
                "locations": unique_locations[start_index:end_index],
                "submission_state": "cancelled_before_submission",
                "error": "The operator requested a stop before this batch was submitted.",
            }
            result["batches"].append(cancelled)
            result["errors"].append({"endpoint": "operator-cancellation", "batch": plan_index + 1, "error": cancelled["error"]})
            _write_embedding_batch_ledger(ledger_path, workspace_slug, result)
            break
        wave_count = min(parallelism, len(pending_plan_indexes))
        wave_indexes = pending_plan_indexes[:wave_count]
        batch_numbers = [item + 1 for item in wave_indexes]
        result["parallelism_schedule"].append({
            "start_batch": min(batch_numbers),
            "end_batch": max(batch_numbers),
            "batch_numbers": batch_numbers,
            "parallelism": wave_count,
        })
        if callable(status_callback):
            if wave_count == 1:
                plan_index = wave_indexes[0]
                status_callback(f"Submitting AnythingLLM batch {plan_index + 1} of {total} ({plan[plan_index][1] - plan[plan_index][0]} records)", {"batch": plan_index + 1, "total_batches": total, "requested": plan[plan_index][1] - plan[plan_index][0], "parallelism": 1})
            else:
                labels = ", ".join(str(item) for item in batch_numbers)
                status_callback(f"Submitting AnythingLLM batches {labels} of {total} concurrently ({steady_size} records each)", {"batch": batch_numbers[0], "total_batches": total, "requested": sum(plan[item][1] - plan[item][0] for item in wave_indexes), "parallelism": wave_count})

        require_wave_exact_vector_evidence = (
            callable(batch_verifier)
            and str(verification_mode or "checkpoint").casefold() != "none"
        )

        def submit_one(plan_index):
            start_index, end_index = plan[plan_index]
            actual_batch = plan_index + 1
            # The serial helper journals state transitions eagerly.  Giving
            # several concurrent helpers the aggregate ledger path let their
            # last write temporarily replace the scheduler's own wave state.
            # Keep those operation journals separate; the outer scheduler
            # remains the sole writer of the aggregate ledger.
            operation_ledger_path = None
            if ledger_path:
                aggregate_ledger = Path(ledger_path)
                operation_ledger_path = aggregate_ledger.with_name(
                    f"{aggregate_ledger.stem}.operation-{actual_batch}{aggregate_ledger.suffix}"
                )
            # ``update-embeddings`` may acknowledge file attachment while
            # AnythingLLM continues indexing in the background.  A second
            # concurrent wave launched solely on HTTP 2xx receipts can stack
            # another four or six jobs behind that hidden work.  When the
            # caller supplied an exact-vector verifier, use it as the wave
            # gate for *every* concurrent member.  This keeps the scheduler
            # bounded by observed AnythingLLM work, not just client replies.
            verify_here = (
                require_wave_exact_vector_evidence
                or str(verification_mode or "checkpoint").casefold() == "every_batch"
                or actual_batch == 1 or actual_batch == total
                or actual_batch % max(1, int(verification_interval or 1)) == 0
            )
            def remap(callback, local_report):
                report = dict(local_report)
                report.update({"batch": actual_batch, "total_batches": total, "start_index": start_index, "end_index": end_index, "locations": unique_locations[start_index:end_index]})
                return callback(report)
            inner = _update_workspace_embeddings_batched_serial(
                api_url, api_key, workspace_slug, unique_locations[start_index:end_index],
                batch_size=end_index - start_index, warmup_batch_size=0, warmup_batch_count=0,
                ledger_path=operation_ledger_path,
                batch_verifier=(lambda report: remap(batch_verifier, report)) if callable(batch_verifier) and verify_here else None,
                batch_inspector=(lambda report: remap(batch_inspector, report)) if callable(batch_inspector) else None,
                cancel_callback=cancel_callback, verification_mode="every_batch" if verify_here else "none",
                verification_interval=1, adaptive_single_record_threshold_seconds=adaptive_single_record_threshold_seconds,
            )
            if inner.get("runtime_events"):
                inner["runtime_events"] = [
                    {
                        **dict(event),
                        "batch": actual_batch
                        if int((event or {}).get("batch") or 0) == 1
                        else (event or {}).get("batch"),
                    }
                    for event in (inner.get("runtime_events") or [])
                    if isinstance(event, dict)
                ]
            report = dict((inner.get("batches") or [{}])[0])
            report.update({"batch": actual_batch, "total_batches": total, "start_index": start_index, "end_index": end_index, "locations": unique_locations[start_index:end_index], "parallelism": wave_count})
            return report, inner

        completed = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=wave_count, thread_name_prefix="anythingllm-embed") as executor:
            futures = {executor.submit(submit_one, item): item for item in wave_indexes}
            for future in concurrent.futures.as_completed(futures):
                plan_index = futures[future]
                try:
                    completed[plan_index] = future.result()
                except Exception as exc:
                    start_index, end_index = plan[plan_index]
                    completed[plan_index] = ({"batch": plan_index + 1, "total_batches": total, "start_index": start_index, "end_index": end_index, "requested": end_index - start_index, "accepted": 0, "locations": unique_locations[start_index:end_index], "submission_state": "unresolved", "error": str(exc), "parallelism": wave_count}, {"errors": [{"error": str(exc)}]})
        wave_reports = [(plan_index, *completed[plan_index]) for plan_index in wave_indexes]
        failed_wave_reports = [
            (plan_index, report, inner)
            for plan_index, report, inner in wave_reports
            if str(report.get("submission_state") or "") != "accepted"
        ]
        # A 429 is the one explicitly documented retryable outcome. It means
        # Desktop refused this request before it entered its embedding queue.
        # Do not extend this exception to timeouts, transport errors, failed
        # verification, or generic 5xx responses: those can still conceal an
        # accepted write and must retain the normal reconciliation workflow.
        safe_parallel_fallback = (
            len(wave_indexes) >= ANYTHINGLLM_EMBEDDING_INITIAL_CONCURRENT_BATCHES
            and bool(failed_wave_reports)
            and all(
                str(report.get("submission_state") or "") == "rejected"
                and int(report.get("http_status") or 0)
                in ANYTHINGLLM_EMBEDDING_SAFE_PARALLEL_FALLBACK_HTTP_STATUSES
                for _plan_index, report, _inner in failed_wave_reports
            )
        )

        for plan_index, report, inner in wave_reports:
            result["runtime_events"].extend(inner.get("runtime_events") or [])
            rate = float(report.get("submission_seconds") or 0.0) / max(
                1, int(report.get("accepted") or report.get("requested") or 1)
            )
            if rate > 0:
                result["submission_timeout_policy"]["observed_seconds_per_record"] = round(
                    max(
                        float(result["submission_timeout_policy"].get("observed_seconds_per_record") or 0.0),
                        rate,
                    ),
                    4,
                )
            if safe_parallel_fallback and any(
                failed_plan_index == plan_index
                for failed_plan_index, _failed_report, _failed_inner in failed_wave_reports
            ):
                result["runtime_events"].append(
                    {
                        "event": "parallelism_fallback_retry_scheduled",
                        "batch": report.get("batch"),
                        "http_status": report.get("http_status"),
                        "failed_parallelism": wave_count,
                        "retry_parallelism": ANYTHINGLLM_EMBEDDING_FAILURE_FALLBACK_CONCURRENT_BATCHES,
                        "reason": "HTTP 429 explicitly rejected this request before submission; it will retry at reduced concurrency.",
                    }
                )
                continue
            result["batches"].append(report)
            result["accepted"] += int(report.get("accepted") or 0)
            if str(report.get("submission_state") or "") != "accepted":
                result["errors"].extend(inner.get("errors") or [{"endpoint": "update-embeddings", "batch": report.get("batch"), "error": report.get("error") or "submission did not complete"}])
            elif str((report.get("verification") or {}).get("status") or "") in {
                "deferred_to_checkpoint",
                "pending_delayed_indexing",
            }:
                result["deferred_verification_batches"].append(int(report["batch"]))
        if safe_parallel_fallback:
            # Persist the explicit rejection before retrying it. If this
            # process dies between waves, the recovery manifest must still
            # describe the rejected batch instead of losing it merely because
            # its retry had already been scheduled in memory.
            result["batches"].extend(
                report
                for _plan_index, report, _inner in failed_wave_reports
            )
        result["batches"].sort(key=lambda row: int(row.get("batch") or 0))
        _write_embedding_batch_ledger(ledger_path, workspace_slug, result)
        wave_failed = bool(failed_wave_reports)
        if callable(status_callback):
            status_callback(
                f"AnythingLLM concurrent wave {', '.join(str(item) for item in batch_numbers)} {'accepted' if not wave_failed else 'needs reconciliation'}",
                {"batch": batch_numbers[-1], "total_batches": total, "parallelism": wave_count, "wave_failed": wave_failed},
            )
        if safe_parallel_fallback:
            fallback = min(limit, ANYTHINGLLM_EMBEDDING_FAILURE_FALLBACK_CONCURRENT_BATCHES)
            retry_indexes = [plan_index for plan_index, _report, _inner in failed_wave_reports]
            retry_batch_numbers = {plan_index + 1 for plan_index in retry_indexes}
            accepted_indexes = {
                plan_index
                for plan_index, report, _inner in wave_reports
                if str(report.get("submission_state") or "") == "accepted"
            }
            pending_plan_indexes = retry_indexes + [
                plan_index
                for plan_index in pending_plan_indexes[wave_count:]
                if plan_index not in accepted_indexes
            ]
            # Keep the rejected attempt durable until the retry begins, then
            # replace it in memory with the final logical batch result. The
            # retry evidence remains in ``runtime_events``; a completed
            # ledger therefore has one final record per logical batch.
            result["batches"] = [
                report
                for report in result["batches"]
                if int(report.get("batch") or 0) not in retry_batch_numbers
            ]
            parallelism = fallback
            result["parallelism_fallback_applied"] = True
            result["runtime_events"].append(
                {
                    "event": "parallelism_fallback_applied",
                    "failed_parallelism": wave_count,
                    "fallback_parallelism": fallback,
                    "retry_batches": [plan_index + 1 for plan_index in retry_indexes],
                    "reason": "HTTP 429 rejections are safe to retry; accepted sibling batches were retained and not replayed.",
                }
            )
            if callable(status_callback):
                status_callback(
                    f"AnythingLLM rate-limited the concurrent wave; retrying only rejected batches at {fallback} concurrent requests",
                    {"batch": batch_numbers[-1], "total_batches": total, "parallelism": fallback, "wave_failed": True, "fallback_applied": True},
                )
            continue
        wave_observation_pending = [
            report
            for _plan_index, report, _inner in wave_reports
            if str(report.get("submission_state") or "") == "accepted"
            and str(report.get("lifecycle_state") or "") != "vector_observed"
        ]
        if require_wave_exact_vector_evidence and wave_observation_pending:
            pending_batches = [int(report.get("batch") or 0) for report in wave_observation_pending]
            result["runtime_events"].append(
                {
                    "event": "concurrent_wave_exact_vector_gate_pending",
                    "parallelism": wave_count,
                    "accepted_batches": pending_batches,
                    "reason": (
                        "AnythingLLM accepted the concurrent wave but had not exposed exact vectors for every "
                        "submitted batch. No later requests were submitted, preventing hidden indexing work from "
                        "being overfilled."
                    ),
                }
            )
            _write_embedding_batch_ledger(ledger_path, workspace_slug, result)
            if callable(status_callback):
                status_callback(
                    (
                        "AnythingLLM accepted the concurrent wave but is still indexing it; "
                        "no later requests were submitted pending exact vector evidence"
                    ),
                    {
                        "batch": batch_numbers[-1],
                        "total_batches": total,
                        "parallelism": wave_count,
                        "wave_observation_pending": True,
                        "pending_batches": pending_batches,
                    },
                )
            break
        if wave_failed:
            if parallelism >= ANYTHINGLLM_EMBEDDING_INITIAL_CONCURRENT_BATCHES:
                fallback = min(
                    limit,
                    ANYTHINGLLM_EMBEDDING_FAILURE_FALLBACK_CONCURRENT_BATCHES,
                )
                result["recommended_resume_parallelism"] = fallback
                result["runtime_events"].append(
                    {
                        "event": "parallelism_fallback_recommended",
                        "failed_parallelism": parallelism,
                        "recommended_resume_parallelism": fallback,
                        "reason": "A four-or-more concurrent AnythingLLM wave did not complete cleanly; unresolved writes are not replayed automatically.",
                    }
                )
                _write_embedding_batch_ledger(ledger_path, workspace_slug, result)
                if callable(status_callback):
                    status_callback(
                        f"AnythingLLM four-request wave did not complete cleanly; recovery is limited to {fallback} concurrent requests after exact-vector reconciliation",
                        {"batch": batch_numbers[-1], "total_batches": total, "parallelism": fallback, "wave_failed": True},
                    )
            break
        # A clean four-request wave (HTTP acknowledgement plus exact vector
        # evidence when a verifier is available) earns the six-request cap.
        # An uncertain response deliberately does not get replayed here:
        # AnythingLLM may have completed it after the client lost the
        # response, so recovery must reconcile exact vectors first.
        pending_plan_indexes = pending_plan_indexes[wave_count:]
        # A clean initial wave earns six. Once a rate-limit fallback has been
        # applied, keep the remainder of this run at two: probing six again
        # would turn one known overload signal into repeated avoidable load.
        if not result.get("parallelism_fallback_applied"):
            parallelism = min(limit, ANYTHINGLLM_EMBEDDING_MAX_CONCURRENT_BATCHES)
    return result


def update_workspace_embeddings_desktop_queue(
    api_url,
    api_key,
    workspace_slug,
    locations,
    ledger_path=None,
    status_callback=None,
    batch_verifier=None,
    batch_inspector=None,
    cancel_callback=None,
    record_label="uploaded files",
):
    """Mirror AnythingLLM Desktop's one-submit, sequential queue contract.

    Desktop's Documents dialog sends every selected document path in one
    ``POST /workspace/:slug/update-embeddings`` call and separately listens to
    ``/embed-progress``.  With non-native embedders (including OpenRouter),
    Desktop processes the submitted list one file at a time inside that one
    request.  Submitting one or two paths per request from this app adds costly
    request boundaries without adding safety.

    This wrapper deliberately retains the existing durable ledger, timeout
    reconciliation, exact-vector verification, and no-speculative-retry
    policy.  It only changes the submission boundary to the full workspace
    queue.  A cancellation request can prevent submission, but cannot safely
    abort a queue that Desktop has already accepted.
    """
    unique_locations = list(dict.fromkeys(str(location) for location in locations if location))
    requested = len(unique_locations)
    if requested == 0:
        return {
            "accepted": 0,
            "requested": 0,
            "planned_locations": [],
            "submission_strategy": ANYTHINGLLM_EMBEDDING_SUBMISSION_STRATEGY,
            "queue_records": 0,
            "batches": [],
            "runtime_events": [],
            "errors": [],
        }

    normalized_record_label = str(record_label or "uploaded files").strip() or "uploaded files"
    queue_state_lock = threading.Lock()
    queue_state = {
        "completed": 0,
        "current": 0,
        "last_event_type": "",
        "events_observed": 0,
        "last_event_monotonic": 0.0,
        "queue_records": requested,
        "observer_state": "connecting",
        "observer_last_state_monotonic": time.monotonic(),
        "observer_failures": 0,
        "observer_reason": "",
    }

    def queue_snapshot():
        with queue_state_lock:
            completed = min(requested, max(0, int(queue_state["completed"])))
            current = min(requested, max(0, int(queue_state["current"])))
            last_event = float(queue_state["last_event_monotonic"] or 0.0)
            return {
                "desktop_queue_completed": completed,
                "desktop_queue_current": current,
                "desktop_queue_events_observed": max(0, int(queue_state["events_observed"])),
                "desktop_queue_last_event_type": str(queue_state["last_event_type"] or ""),
                "desktop_queue_last_event_age_seconds": (
                    round(max(0.0, time.monotonic() - last_event), 3)
                    if last_event else None
                ),
                "queue_records": requested,
                "desktop_queue_observer_state": str(queue_state["observer_state"] or "unknown"),
                "desktop_queue_observer_last_state_age_seconds": (
                    round(max(0.0, time.monotonic() - float(queue_state["observer_last_state_monotonic"] or 0.0)), 3)
                    if queue_state["observer_last_state_monotonic"] else None
                ),
                "desktop_queue_observer_failures": max(0, int(queue_state["observer_failures"] or 0)),
                "desktop_queue_observer_reason": str(queue_state["observer_reason"] or ""),
            }

    def queue_activity_summary():
        snapshot = queue_snapshot()
        completed = int(snapshot["desktop_queue_completed"])
        current = int(snapshot["desktop_queue_current"])
        if completed:
            return f"Desktop completed {completed}/{requested} {normalized_record_label}"
        if current:
            return f"Desktop is embedding {current}/{requested} {normalized_record_label}"
        observer_state = str(snapshot.get("desktop_queue_observer_state") or "unknown")
        if observer_state == "reconnecting":
            return "Desktop queue observer is reconnecting; queue completion is not yet observable"
        if observer_state == "connected":
            return "Desktop queue observer is connected; waiting for the next queue event"
        return f"0/{requested} {normalized_record_label} completed"

    def publish_desktop_queue_event(event):
        """Relay a matching Desktop queue update through the worker event file.

        ``doc_complete`` means Desktop completed a queued file, not that its
        vector is already searchable.  That distinction stays explicit until
        the targeted vector observer has confirmed the page-parent record.
        """
        event_type = str((event or {}).get("type") or "").strip()
        try:
            position = int((event or {}).get("docIndex") or 0) + 1
        except (TypeError, ValueError):
            position = 0
        try:
            event_total = int((event or {}).get("totalDocs") or 0)
        except (TypeError, ValueError):
            event_total = 0
        total = requested or event_total
        if total <= 0:
            return
        with queue_state_lock:
            if event_type == "doc_complete":
                queue_state["completed"] = max(queue_state["completed"], position)
            elif event_type in {"doc_starting", "chunk_progress"}:
                queue_state["current"] = max(queue_state["current"], position)
            queue_state["last_event_type"] = event_type
            queue_state["events_observed"] += 1
            queue_state["last_event_monotonic"] = time.monotonic()
            completed = min(total, max(0, int(queue_state["completed"])))
            current = min(total, max(0, int(queue_state["current"])))
        snapshot = queue_snapshot()
        if not callable(status_callback):
            return
        if event_type == "doc_complete":
            message = (
                f"AnythingLLM Desktop queue: {completed}/{total} {normalized_record_label} completed; "
                "searchable-vector confirmation is still in progress"
            )
        elif event_type in {"doc_starting", "chunk_progress"}:
            message = (
                f"AnythingLLM Desktop queue: embedding {current}/{total} {normalized_record_label}; "
                f"{completed}/{total} completed; searchable-vector confirmation follows"
            )
        elif event_type == "all_complete":
            message = (
                f"AnythingLLM Desktop queue finished {completed}/{total} {normalized_record_label}; "
                "verifying searchable vectors"
            )
        else:
            return
        status_callback(
            message,
            {
                "timing_event": (
                    "desktop_queue_completed"
                    if event_type == "all_complete"
                    else "queue_progress"
                ),
                "desktop_queue_event_type": event_type,
                "desktop_events_observed": completed,
                "desktop_current_record": current,
                **snapshot,
                "queue_completion_fraction": completed / total,
            },
        )

    def publish_observer_state(health):
        """Make the observer's transport state available to queue evidence.

        A quiet Desktop queue and a reconnecting SSE observer are different
        facts.  They share the same worker-side state object so a later batch
        verifier can report the distinction without treating either one as a
        vector-completion signal.
        """
        health = health if isinstance(health, dict) else {}
        with queue_state_lock:
            queue_state["observer_state"] = str(health.get("state") or "unknown")
            queue_state["observer_last_state_monotonic"] = float(
                health.get("last_state_monotonic") or time.monotonic()
            )
            queue_state["observer_failures"] = int(health.get("failures") or 0)
            queue_state["observer_reason"] = str(health.get("reason") or "")

    def desktop_queue_status(message, report):
        if not callable(status_callback):
            return
        normalized = str(message or "")
        report = {**dict(report or {}), **queue_snapshot()}
        queue_summary = queue_activity_summary()
        if normalized.startswith("Submitting AnythingLLM batch"):
            normalized = (
                f"Submitting AnythingLLM sequential queue ({requested} {normalized_record_label}); "
                f"{queue_summary}; searchable-vector confirmation follows"
            )
        elif normalized.startswith("AnythingLLM batch") and "submission accepted" in normalized:
            normalized = (
                f"AnythingLLM accepted the sequential queue; {queue_summary}; "
                "verifying searchable vectors next"
            )
        elif normalized.startswith("Verifying AnythingLLM batch"):
            normalized = (
                f"Verifying AnythingLLM indexing; {queue_summary}; "
                "checking exact searchable vectors"
            )
        elif normalized.startswith("AnythingLLM batch") and "is searchable" in normalized:
            if bool(report.get("searchability_proven")):
                normalized = (
                    f"AnythingLLM queue is searchable; {queue_summary}; "
                    "continuing final workspace checks"
                )
            else:
                normalized = (
                    f"AnythingLLM queue request finished; {queue_summary}; "
                    "exact searchable-vector confirmation is still pending"
                )
        status_callback(normalized, report)

    # The Desktop UI opens this exact SSE endpoint alongside its one update
    # request.  We mirror that observation channel, but only render events
    # whose emitted file path matches this run's submitted locations.
    # Consequently an unrelated manually started queue cannot make this run
    # look complete.  The listener is never an acceptance/retrieval signal.
    progress_listener = start_anythingllm_embed_progress_listener(
        api_url,
        api_key,
        workspace_slug,
        unique_locations,
        observer_callback=publish_desktop_queue_event,
        observer_state_callback=publish_observer_state,
    )
    # Do not make the write depend on the optional observer, but give the SSE
    # request a small head start. This lets the receipt capture Desktop's
    # initial ``batch_starting`` event when the local server is responsive,
    # without changing the one-request FIFO submission boundary.
    progress_listener["connected_event"].wait(timeout=0.75)
    def queue_aware_batch_verifier(batch_report):
        if not callable(batch_verifier):
            return {}
        # A mutable, JSON-safe snapshot lets the vector observer report live
        # SSE queue evidence in its own status line.  It does not make SSE a
        # success signal and does not alter queue ownership.
        contextual_report = dict(batch_report)
        contextual_report["desktop_queue_observer"] = queue_state
        verification = batch_verifier(contextual_report)
        if isinstance(verification, dict):
            return {**verification, "desktop_queue_observer": queue_snapshot()}
        return verification

    try:
        result = update_workspace_embeddings_batched(
            api_url,
            api_key,
            workspace_slug,
            unique_locations,
            # One request containing all managed locations: Desktop serializes the
            # individual documents inside its own queue.
            batch_size=requested,
            warmup_batch_size=0,
            warmup_batch_count=0,
            ledger_path=ledger_path,
            status_callback=desktop_queue_status,
            batch_verifier=queue_aware_batch_verifier if callable(batch_verifier) else None,
            batch_inspector=batch_inspector,
            cancel_callback=cancel_callback,
            verification_mode="checkpoint",
            concurrent_batch_limit=1,
            submission_timeout_override=ANYTHINGLLM_DESKTOP_QUEUE_RECEIPT_TIMEOUT_SECONDS,
        )
    finally:
        progress_listener["stop_event"].set()
        progress_listener["thread"].join(timeout=1.0)
    result["runtime_events"].extend(progress_listener["events"])
    if progress_listener["errors"]:
        # Keep this diagnostic in the ledger rather than making a valid queue
        # submission fail merely because the optional stream was unavailable.
        result["runtime_events"].extend(progress_listener["errors"])
    result["submission_strategy"] = ANYTHINGLLM_EMBEDDING_SUBMISSION_STRATEGY
    result["queue_records"] = requested
    result["queue_cancellation_boundary"] = "before_submission_only"
    event_counts = {}
    observed_locations = set()
    for event in progress_listener["events"]:
        event_type = str(event.get("type") or "unknown")
        event_counts[event_type] = event_counts.get(event_type, 0) + 1
        location = _normalized_anythingllm_document_location(event.get("filename"))
        if location:
            observed_locations.add(location)
    result["progress_observation"] = {
        "stream_connected": progress_listener["connected_event"].is_set(),
        "expected_records": requested,
        "event_counts": dict(sorted(event_counts.items())),
        "matching_records_observed": len(observed_locations),
        "stream_start_errors": len(progress_listener["errors"]),
        "meaning": (
            "Desktop queue events are observational only; exact vector and retrieval "
            "verification determine run success."
        ),
        "final_queue_snapshot": queue_snapshot(),
    }
    return result


def maybe_upload_payloads(
    api_url,
    api_key,
    payloads,
    upload_limit=0,
    upload_indices=None,
    workspace_slug="",
    embedding_ledger_path=None,
    status_callback=None,
    batch_verifier=None,
    batch_inspector=None,
    embedding_batch_size=None,
    embedding_warmup_batch_size=ANYTHINGLLM_EMBEDDING_WARMUP_BATCH_SIZE,
    embedding_warmup_batch_count=ANYTHINGLLM_EMBEDDING_WARMUP_BATCH_COUNT,
    cancel_callback=None,
    submission_receipt_path=None,
    run_id="",
    record_label="uploaded files",
):
    if not api_url:
        return {"status": "error_missing_api_url", "uploaded": 0, "embedded": 0, "errors": [{"error": "Missing AnythingLLM API URL."}]}
    if not workspace_slug:
        return {"status": "error_missing_workspace", "uploaded": 0, "embedded": 0, "errors": [{"error": "Native metadata upload requires a selected workspace slug."}]}
    api_key, authentication_mode = resolve_anythingllm_api_key(api_url, api_key)
    temporary_key_id = None
    temporary_key_cleanup = {"status": "not_applicable", "error": ""}
    if not api_key:
        temporary_key = create_temporary_desktop_api_key(api_url)
        if temporary_key["status"] != "created":
            return {
                "status": "error_authentication_required",
                "uploaded": 0,
                "embedded": 0,
                "authentication_mode": "unavailable",
                "temporary_key_cleanup": temporary_key_cleanup,
                "errors": [
                    {
                        "error": (
                            "AnythingLLM Developer API authentication is required. "
                            "The local Desktop temporary-key route was unavailable."
                        ),
                        "details": temporary_key.get("error", ""),
                    }
                ],
            }
        api_key = temporary_key["secret"]
        temporary_key_id = temporary_key["id"]
        authentication_mode = "temporary_desktop_api_key"

    endpoint = api_url.rstrip("/") + "/api/v1/document/raw-text"
    uploaded = 0
    embedded = 0
    errors = []
    locations = []
    embedding_update = {"accepted": 0, "requested": 0, "batch_size": ANYTHINGLLM_EMBEDDING_UPDATE_BATCH_SIZE, "batches": [], "errors": []}
    upload_rows = select_upload_payloads(payloads, upload_limit, upload_indices)
    correlation_id = f"upload-{uuid.uuid4().hex}"
    try:
        for payload in upload_rows:
            record_submission_receipt(
                submission_receipt_path, payload,
                run_id=run_id, workspace_slug=workspace_slug, transport="raw_text",
                state="submitted", correlation_id=correlation_id,
                next_check="Reconcile document location, workspace attachment, and targeted vector evidence before any resubmission.",
            )
            try:
                status, response_text = post_json(endpoint, payload, api_key=api_key)
                if 200 <= status < 300:
                    uploaded += 1
                    location = extract_document_location(response_text)
                    record_submission_receipt(
                        submission_receipt_path, payload,
                        run_id=run_id, workspace_slug=workspace_slug, transport="raw_text",
                        state="attached" if location else "submitted",
                        correlation_id=correlation_id, http_status=status, location=location,
                        next_check=(
                            "Submit the recorded location to the workspace embedding update."
                            if location else "Read the raw-text response and reconcile the native document before retrying."
                        ),
                    )
                    if location:
                        locations.append(location)
                    if callable(status_callback):
                        status_callback(
                            f"Attaching prepared records to AnythingLLM: {uploaded}/{len(upload_rows)} accepted",
                            {
                                "timing_event": "attachment_progress",
                                "attachments_completed": uploaded,
                                "attachments_total": len(upload_rows),
                            },
                        )
                else:
                    errors.append({"status": status, "segment": payload.get("metadata", {}).get("chunkSource", "")})
                    record_submission_receipt(
                        submission_receipt_path, payload,
                        run_id=run_id, workspace_slug=workspace_slug, transport="raw_text",
                        state="rejected", correlation_id=correlation_id, http_status=status,
                        error=f"HTTP {status}",
                        next_check="Inspect the server response and correct the request before an explicit resubmission.",
                    )
            except Exception as exc:
                errors.append({"error": str(exc), "segment": payload.get("metadata", {}).get("chunkSource", "")})
                record_submission_receipt(
                    submission_receipt_path, payload,
                    run_id=run_id, workspace_slug=workspace_slug, transport="raw_text",
                    state="submission_unknown", correlation_id=correlation_id, error=str(exc),
                    next_check="Reconcile document location, workspace attachment, and targeted vector evidence; do not replay this POST automatically.",
                )
                break

        if uploaded > 0 and locations:
            embedding_update = update_workspace_embeddings_desktop_queue(
                api_url,
                api_key,
                workspace_slug,
                locations,
                ledger_path=embedding_ledger_path,
                status_callback=status_callback,
                batch_verifier=batch_verifier,
                batch_inspector=batch_inspector,
                cancel_callback=cancel_callback,
                record_label=record_label,
            )
            embedded = embedding_update["accepted"]
            errors.extend(embedding_update["errors"])
        elif uploaded > 0:
            errors.append(
                {
                    "endpoint": "update-embeddings",
                    "error": "Raw-text upload responses did not include document locations to verify embedding.",
                }
            )
    finally:
        if temporary_key_id:
            temporary_key_cleanup = cleanup_temporary_desktop_api_key(api_url, temporary_key_id)

    reconciliation_pending = any(
        str(batch.get("lifecycle_state") or "") in {
            "reconciliation_pending", "workspace_attached"
        }
        for batch in (embedding_update.get("batches") or [])
    )
    if uploaded > 0 and locations and embedded != len(locations) and not errors:
        errors.append(
            {
                "endpoint": "update-embeddings",
                "classification": "embedding_update_incomplete",
                "error": (
                    f"AnythingLLM accepted {embedded} of {len(locations)} embedding location(s); "
                    "read-only reconciliation is required before any retry."
                ),
            }
        )
    if uploaded == 0:
        status = "error"
    elif reconciliation_pending:
        # Workspace attachment prevents a duplicate, but it is still weaker
        # than vector observation. The surrounding document-level poll will
        # either promote this original run to complete or retain an explicit
        # unknown/reconciliation-required terminal state.
        status = "reconciliation_pending"
    elif embedded != len(locations) or errors:
        status = "error"
    elif temporary_key_cleanup.get("status") == "delete_failed":
        status = "complete_with_key_cleanup_warning"
    else:
        status = "complete"
    return {
        "status": status,
        "uploaded": uploaded,
        "embedded": embedded,
        "locations": locations,
        "embedding_update": embedding_update,
        "transport": "raw_text",
        "authentication_mode": authentication_mode,
        "submission_receipt_path": str(submission_receipt_path or ""),
        "correlation_id": correlation_id,
        "temporary_key_cleanup": temporary_key_cleanup,
        "errors": errors or ([] if uploaded else [{"error": "No payloads were uploaded."}]),
    }


def maybe_upload_segment_files(
    api_url,
    api_key,
    upload_rows,
    upload_limit=0,
    upload_indices=None,
    workspace_slug="",
    folder_name="custom-documents",
    storage_dir=None,
    embedding_ledger_path=None,
    status_callback=None,
    batch_verifier=None,
    batch_inspector=None,
    embedding_batch_size=None,
    embedding_warmup_batch_size=ANYTHINGLLM_EMBEDDING_WARMUP_BATCH_SIZE,
    embedding_warmup_batch_count=ANYTHINGLLM_EMBEDDING_WARMUP_BATCH_COUNT,
    cancel_callback=None,
    submission_receipt_path=None,
    run_id="",
    record_label="uploaded files",
):
    if not api_url:
        return {"status": "error_missing_api_url", "uploaded": 0, "embedded": 0, "errors": [{"error": "Missing AnythingLLM API URL."}]}
    if not workspace_slug:
        return {"status": "error_missing_workspace", "uploaded": 0, "embedded": 0, "errors": [{"error": "Native metadata upload requires a selected workspace slug."}]}
    api_key, authentication_mode = resolve_anythingllm_api_key(api_url, api_key, storage_dir)
    temporary_key_id = None
    temporary_key_cleanup = {"status": "not_applicable", "error": ""}
    if not api_key:
        temporary_key = create_temporary_desktop_api_key(api_url)
        if temporary_key["status"] != "created":
            return {
                "status": "error_authentication_required",
                "uploaded": 0,
                "embedded": 0,
                "authentication_mode": "unavailable",
                "temporary_key_cleanup": temporary_key_cleanup,
                "errors": [
                    {
                        "error": (
                            "AnythingLLM Developer API authentication is required. "
                            "The local Desktop temporary-key route was unavailable."
                        ),
                        "details": temporary_key.get("error", ""),
                    }
                ],
            }
        api_key = temporary_key["secret"]
        temporary_key_id = temporary_key["id"]
        authentication_mode = "temporary_desktop_api_key"

    endpoint = api_url.rstrip("/") + "/api/v1/document/upload"
    uploaded = 0
    embedded = 0
    errors = []
    locations = []
    embedding_update = {"accepted": 0, "requested": 0, "batch_size": ANYTHINGLLM_EMBEDDING_UPDATE_BATCH_SIZE, "batches": [], "errors": []}
    selected_rows = select_upload_payloads(upload_rows, upload_limit, upload_indices)
    correlation_id = f"upload-{uuid.uuid4().hex}"
    try:
        for row in selected_rows:
            text_file = Path(row.get("text_file") or "")
            if not text_file.exists():
                errors.append({"error": f"Missing upload file: {text_file}", "segment": row.get("chunkSource", "")})
                break
            metadata = {
                "title": row.get("title") or row.get("filename") or "",
                "docAuthor": row.get("docAuthor") or "",
                "description": row.get("description") or "",
                "docSource": row.get("docSource") or "",
                "chunkSource": row.get("chunkSource") or "",
            }
            receipt_payload = {"metadata": metadata, "textContent": ""}
            # The file-upload receipt hashes only the prepared file bytes;
            # unlike raw-text transport it never loads the full text into the
            # receipt or JSONL evidence stream.
            try:
                prepared_file_hash = hashlib.sha256(text_file.read_bytes()).hexdigest()
            except OSError:
                prepared_file_hash = hashlib.sha256(str(text_file).encode("utf-8")).hexdigest()
            record_submission_receipt(
                submission_receipt_path, receipt_payload,
                run_id=run_id, workspace_slug=workspace_slug, transport="file_upload",
                state="submitted", correlation_id=correlation_id,
                prepared_payload_hash=prepared_file_hash,
                next_check="Reconcile document location, workspace attachment, and targeted vector evidence before any resubmission.",
            )
            try:
                status, response_text = post_multipart_form(
                    endpoint,
                    fields={"metadata": json.dumps(metadata, ensure_ascii=False)},
                    file_field_name="file",
                    file_path=text_file,
                    api_key=api_key,
                )
                if 200 <= status < 300:
                    uploaded += 1
                    location = extract_document_location(response_text)
                    record_submission_receipt(
                        submission_receipt_path, receipt_payload,
                        run_id=run_id, workspace_slug=workspace_slug, transport="file_upload",
                        state="attached" if location else "submitted",
                        correlation_id=correlation_id, http_status=status, location=location,
                        prepared_payload_hash=prepared_file_hash,
                        next_check=(
                            "Submit the recorded location to the workspace embedding update."
                            if location else "Read the upload response and reconcile the native document before retrying."
                        ),
                    )
                    if location:
                        if storage_dir:
                            location, relocation_error = relocate_uploaded_document(
                                Path(storage_dir),
                                location,
                                folder_name,
                            )
                            if relocation_error:
                                errors.append(
                                    {
                                        "error": relocation_error,
                                        "segment": row.get("chunkSource", ""),
                                        "filename": row.get("filename", ""),
                                    }
                                )
                                break
                        locations.append(location)
                    if callable(status_callback):
                        status_callback(
                            f"Attaching page-parent files to AnythingLLM: {uploaded}/{len(selected_rows)} accepted",
                            {
                                "timing_event": "attachment_progress",
                                "attachments_completed": uploaded,
                                "attachments_total": len(selected_rows),
                            },
                        )
                else:
                    errors.append({"status": status, "segment": row.get("chunkSource", ""), "filename": row.get("filename", "")})
                    record_submission_receipt(
                        submission_receipt_path, receipt_payload,
                        run_id=run_id, workspace_slug=workspace_slug, transport="file_upload",
                        state="rejected", correlation_id=correlation_id, http_status=status,
                        prepared_payload_hash=prepared_file_hash,
                        error=f"HTTP {status}",
                        next_check="Inspect the server response and correct the request before an explicit resubmission.",
                    )
            except Exception as exc:
                errors.append({"error": str(exc), "segment": row.get("chunkSource", ""), "filename": row.get("filename", "")})
                record_submission_receipt(
                    submission_receipt_path, receipt_payload,
                    run_id=run_id, workspace_slug=workspace_slug, transport="file_upload",
                    state="submission_unknown", correlation_id=correlation_id, error=str(exc),
                    prepared_payload_hash=prepared_file_hash,
                    next_check="Reconcile document location, workspace attachment, and targeted vector evidence; do not replay this POST automatically.",
                )
                break

        if uploaded > 0 and locations:
            embedding_update = update_workspace_embeddings_desktop_queue(
                api_url,
                api_key,
                workspace_slug,
                locations,
                ledger_path=embedding_ledger_path,
                status_callback=status_callback,
                batch_verifier=batch_verifier,
                batch_inspector=batch_inspector,
                cancel_callback=cancel_callback,
                record_label=record_label,
            )
            embedded = embedding_update["accepted"]
            errors.extend(embedding_update["errors"])
        elif uploaded > 0:
            errors.append(
                {
                    "endpoint": "update-embeddings",
                    "error": "File upload responses did not include document locations to verify embedding.",
                }
            )
    finally:
        if temporary_key_id:
            temporary_key_cleanup = cleanup_temporary_desktop_api_key(api_url, temporary_key_id)

    reconciliation_pending = any(
        str(batch.get("lifecycle_state") or "") in {
            "reconciliation_pending", "workspace_attached"
        }
        for batch in (embedding_update.get("batches") or [])
    )
    if uploaded > 0 and locations and embedded != len(locations) and not errors:
        errors.append(
            {
                "endpoint": "update-embeddings",
                "classification": "embedding_update_incomplete",
                "error": (
                    f"AnythingLLM accepted {embedded} of {len(locations)} embedding location(s); "
                    "read-only reconciliation is required before any retry."
                ),
            }
        )
    if uploaded == 0:
        status = "error"
    elif reconciliation_pending:
        status = "reconciliation_pending"
    elif embedded != len(locations) or errors:
        status = "error"
    elif temporary_key_cleanup.get("status") == "delete_failed":
        status = "complete_with_key_cleanup_warning"
    else:
        status = "complete"
    return {
        "status": status,
        "uploaded": uploaded,
        "embedded": embedded,
        "locations": locations,
        "embedding_update": embedding_update,
        "transport": "file_upload",
        "document_folder_name": sanitize_anythingllm_relative_folder_path(folder_name),
        "document_folder_path": (
            str(Path(storage_dir) / "documents" / sanitize_anythingllm_relative_folder_path(folder_name))
            if storage_dir and folder_name
            else ""
        ),
        "authentication_mode": authentication_mode,
        "submission_receipt_path": str(submission_receipt_path or ""),
        "correlation_id": correlation_id,
        "temporary_key_cleanup": temporary_key_cleanup,
        "errors": errors or ([] if uploaded else [{"error": "No files were uploaded."}]),
    }


def maybe_upload_to_anythingllm(
    api_url,
    api_key,
    payloads,
    upload_limit=0,
    upload_indices=None,
    workspace_slug="",
    upload_transport="raw_text",
    upload_plan_rows=None,
    storage_dir=None,
    folder_name="custom-documents",
    embedding_ledger_path=None,
    status_callback=None,
    batch_verifier=None,
    batch_inspector=None,
    embedding_batch_size=None,
    embedding_warmup_batch_size=ANYTHINGLLM_EMBEDDING_WARMUP_BATCH_SIZE,
    embedding_warmup_batch_count=ANYTHINGLLM_EMBEDDING_WARMUP_BATCH_COUNT,
    cancel_callback=None,
    submission_receipt_path=None,
    run_id="",
    record_label="uploaded files",
):
    transport = str(upload_transport or "raw_text").strip().casefold()
    if transport == "file_upload":
        if not upload_plan_rows:
            return {
                "status": "error_missing_upload_files",
                "uploaded": 0,
                "embedded": 0,
                "transport": "file_upload",
                "errors": [
                    {
                        "error": (
                            "File-upload transport requires generated segment files. "
                            "This run did not provide a usable upload plan."
                        )
                    }
                ],
            }
        return maybe_upload_segment_files(
            api_url,
            api_key,
            upload_plan_rows or [],
            upload_limit=upload_limit,
            upload_indices=upload_indices,
            workspace_slug=workspace_slug,
            folder_name=folder_name,
            storage_dir=storage_dir,
            embedding_ledger_path=embedding_ledger_path,
            status_callback=status_callback,
            batch_verifier=batch_verifier,
            batch_inspector=batch_inspector,
            embedding_batch_size=embedding_batch_size,
            embedding_warmup_batch_size=embedding_warmup_batch_size,
            embedding_warmup_batch_count=embedding_warmup_batch_count,
            cancel_callback=cancel_callback,
            submission_receipt_path=submission_receipt_path,
            run_id=run_id,
            record_label=record_label,
        )
    return maybe_upload_payloads(
        api_url,
        api_key,
        payloads,
        upload_limit=upload_limit,
        upload_indices=upload_indices,
        workspace_slug=workspace_slug,
        embedding_ledger_path=embedding_ledger_path,
        status_callback=status_callback,
        batch_verifier=batch_verifier,
        batch_inspector=batch_inspector,
        embedding_batch_size=embedding_batch_size,
        embedding_warmup_batch_size=embedding_warmup_batch_size,
        embedding_warmup_batch_count=embedding_warmup_batch_count,
        cancel_callback=cancel_callback,
        submission_receipt_path=submission_receipt_path,
        run_id=run_id,
        record_label=record_label,
    )


def apply_temporary_key_cleanup_review(selected, upload_report, prepare_and_upload):
    """Promote an unresolved managed-key cleanup to a final review outcome.

    The native transport keeps its established
    ``complete_with_key_cleanup_warning`` status. This helper owns the
    separate run-level truth: upload may be complete while the overall run is
    not green because a temporary credential still requires operator review.
    """
    cleanup = dict((upload_report or {}).get("temporary_key_cleanup") or {})
    if not prepare_and_upload or cleanup.get("status") != "delete_failed":
        return False
    cleanup_reason = "cleanup_warning"
    selected["readiness_status"] = "needs_review"
    selected["readiness_reasons"] = list(dict.fromkeys([
        *(selected.get("readiness_reasons") or []), cleanup_reason,
    ]))
    upload_report["cleanup_obligations"] = [
        {
            "kind": "temporary_desktop_api_key",
            "status": "pending_review",
            "reason": cleanup_reason,
            "retry_attempted": bool(cleanup.get("retry_attempted")),
            "attempt_count": int(cleanup.get("attempt_count") or 1),
        }
    ]
    warnings = list(upload_report.get("warnings") or [])
    warnings.append(
        {
            "warning": "Temporary Desktop API key cleanup needs review; upload evidence is retained.",
            "reason": cleanup_reason,
        }
    )
    upload_report["warnings"] = warnings
    return True


def post_json_captured(url, body, api_key=None, timeout_label="request", timeout_seconds: float = 120):
    started = time.perf_counter()
    try:
        status, response_text = post_json(url, body, api_key=api_key, timeout=timeout_seconds)
        try:
            data = json.loads(response_text)
        except Exception:
            data = {"raw": response_text}
        return {
            "http_status": status,
            "data": data,
            "error": "",
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }
    except urllib.error.HTTPError as exc:
        try:
            response_text = exc.read().decode("utf-8", errors="replace")
        finally:
            exc.close()
        try:
            data = json.loads(response_text)
        except Exception:
            data = {"raw": response_text}
        return {
            "http_status": exc.code,
            "data": data,
            "error": data.get("error", response_text) if isinstance(data, dict) else response_text,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }
    except Exception as exc:
        return {
            "http_status": None,
            "data": {},
            "error": f"{timeout_label} failed: {exc}",
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }


def runtime_request_error_class(response):
    """Classify captured runtime requests without flattening a timeout into a miss."""
    status = int((response or {}).get("http_status") or 0)
    error = str((response or {}).get("error") or "").casefold()
    if not error and 200 <= status < 300:
        return "none"
    if "timed out" in error or "timeout" in error:
        return "timeout"
    if any(token in error for token in ("connection refused", "connection reset", "network", "unreachable")):
        return "connection"
    if status in {502, 503, 504}:
        return "transient_http"
    if status:
        return f"http_{status}"
    return "runtime_error"


def post_json_captured_with_retry(
    url,
    body,
    api_key=None,
    timeout_label="request",
    timeout_seconds: float = 120,
    max_attempts=2,
):
    """Run a bounded runtime probe and retain every attempt for later review."""
    attempts = []
    total_started = time.perf_counter()
    limit = max(1, int(max_attempts or 1))
    response = {}
    for attempt in range(1, limit + 1):
        response = post_json_captured(
            url,
            body,
            api_key=api_key,
            timeout_label=timeout_label,
            timeout_seconds=timeout_seconds,
        )
        error_class = runtime_request_error_class(response)
        attempts.append(
            {
                "attempt": attempt,
                "endpoint": url,
                "http_status": response.get("http_status"),
                "elapsed_seconds": float(response.get("elapsed_seconds") or 0),
                "error_class": error_class,
                "error": str(response.get("error") or ""),
            }
        )
        if error_class not in {"timeout", "connection", "transient_http"} or attempt >= limit:
            break
        # The retry budget is intentionally tiny: this is evidence recovery
        # for a local transient event, never a hidden long-running retry loop.
        time.sleep(0.25)
    result = dict(response or {})
    result["endpoint"] = url
    result["attempts"] = attempts
    result["retry_count"] = max(0, len(attempts) - 1)
    result["error_class"] = runtime_request_error_class(result)
    result["total_elapsed_seconds"] = round(time.perf_counter() - total_started, 3)
    return result


def expected_page_segment_tokens(payload):
    metadata = payload.get("metadata", {})
    haystack = " ".join(
        str(metadata.get(key) or "")
        for key in ["title", "description", "chunkSource"]
    )
    page_match = re.search(r"(?<![A-Za-z0-9])p0*(\d{1,4})(?!\d)", haystack, re.I)
    segment_match = re.search(r"(?<![A-Za-z0-9])s0*(\d{1,6})(?!\d)", haystack, re.I)
    return {
        "page_number": int(page_match.group(1)) if page_match else None,
        "segment_number": int(segment_match.group(1)) if segment_match else None,
        "logical_page": (
            int(logical_page_match.group(1))
            if (logical_page_match := re.search(r"(?:logical[-\s]?p|lp)0*(\d{1,4})", haystack, re.I))
            else None
        ),
        "chunk_source": metadata.get("chunkSource") or "",
        "title": metadata.get("title") or "",
        "representation": "page_parent"
        if str(metadata.get("chunkSource") or "").startswith("page-parent://")
        else "segment",
    }


def runtime_validation_query_text(payload, limit=240):
    """Choose a dense prose window for an exact runtime retrieval probe.

    A first segment can legitimately start with a letterhead, page number, or
    imperfect OCR.  Replaying that prefix is a weak semantic-search query and
    makes the runtime gate measure OCR noise rather than whether the indexed
    body is retrievable.  This does not alter uploaded text: it selects an
    existing, verbatim window from it for the validation query.

    A final body paragraph can share a page-parent payload with a references
    section.  Citation entries often look artificially "dense" to the simple
    lexical scorer below, yet they are a poor query for testing retrieval of
    the page's substantive content.  When an explicit references heading
    leaves a meaningful body prefix, score that prefix instead.
    """
    raw_text = str(payload.get("textContent") or "")
    body_prefix = re.split(
        r"(?im)^\s*(?:references|bibliography|works\s+cited)\b",
        raw_text,
        maxsplit=1,
    )[0]
    text = re.sub(r"\s+", " ", raw_text).strip()
    normalized_body_prefix = re.sub(r"\s+", " ", body_prefix).strip()
    if len(normalized_body_prefix) >= max(120, limit // 2):
        text = normalized_body_prefix
    if len(text) <= limit:
        return text
    starts = set(range(0, max(1, len(text) - limit + 1), max(40, limit // 3)))
    starts.add(max(0, len(text) - limit))
    best_text = text[:limit]
    best_score = float("-inf")
    for start in sorted(starts):
        candidate = text[start:start + limit].strip()
        letters = sum(character.isalpha() for character in candidate)
        digits = sum(character.isdigit() for character in candidate)
        words = re.findall(r"[A-Za-z]{3,}", candidate)
        unique_words = len({word.casefold() for word in words})
        sentence_marks = candidate.count(".") + candidate.count(";") + candidate.count(":")
        score = letters + (unique_words * 5) + (sentence_marks * 8) - (digits * 3)
        if score > best_score:
            best_score = score
            best_text = candidate
    return best_text


def select_runtime_validation_payloads(payloads, upload_limit=0, limit=2, upload_indices=None):
    """Pick deterministic, stratified, body-rich fragments for live probes.

    The selected records remain exact upload payloads and are spread across
    the document rather than repeatedly favouring the strongest early pages.
    Within each stratum, a stable content-quality score chooses the probe. The
    determinism makes a failed sample reproducible for later diagnosis.
    """
    candidates = list(select_upload_payloads(payloads, upload_limit, upload_indices))
    if len(candidates) <= 1:
        return candidates

    requested = max(1, min(int(limit or 1), len(candidates)))
    scored = []
    for index, payload in enumerate(candidates):
        query = runtime_validation_query_text(payload)
        words = re.findall(r"[A-Za-z]{3,}", query)
        alpha = sum(character.isalpha() for character in query)
        digits = sum(character.isdigit() for character in query)
        punctuation = sum(character in ".;:" for character in query)
        expected = expected_page_segment_tokens(payload)
        score = alpha + (len({word.casefold() for word in words}) * 5) + (punctuation * 8) - (digits * 3)
        scored.append((score, index, expected.get("page_number"), payload))
    if requested == 1:
        return [max(scored, key=lambda row: (row[0], -row[1]))[3]]

    chosen = []
    pages = set()
    # Do not let a page made entirely of a letterhead, a page number, or OCR
    # noise consume one of the few live probes merely because it happens to
    # be in the first document stratum.
    strongest_score = max(row[0] for row in scored)
    substantive = [row for row in scored if row[0] >= max(120, strongest_score * 0.45)]
    if not substantive:
        substantive = list(scored)
    for stratum in range(requested):
        start = (stratum * len(scored)) // requested
        end = max(start + 1, ((stratum + 1) * len(scored)) // requested)
        # Prefer a substantive probe in each contiguous document region, but
        # do not collapse a multi-page sample onto the same page.
        region = sorted(
            [row for row in scored[start:end] if row in substantive],
            key=lambda row: (-row[0], row[1]),
        )
        selected = next(
            (row for row in region if row[2] is None or row[2] not in pages),
            None,
        )
        if selected is None:
            midpoint = (start + end - 1) / 2.0
            selected = next(
                (
                    row
                    for row in sorted(
                        substantive,
                        key=lambda row: (abs(row[1] - midpoint), -row[0], row[1]),
                    )
                    if row[3] not in chosen and (row[2] is None or row[2] not in pages)
                ),
                None,
            )
        if selected is None:
            continue
        _score, _index, page_number, payload = selected
        if payload in chosen:
            continue
        chosen.append(payload)
        if page_number is not None:
            pages.add(page_number)
    # Multiple fragments from the same PDF page exercise the same provenance
    # and workspace lane. One strong probe for that page is sufficient; do
    # not refill the quota with a duplicate-page request merely because the
    # document has only one page (or the upload scope was a two-fragment pilot).
    if pages:
        return chosen
    for _score, _index, _page_number, payload in sorted(scored, key=lambda row: (-row[0], row[1])):
        if payload not in chosen:
            chosen.append(payload)
        if len(chosen) >= requested:
            break
    return chosen


def runtime_validation_sample_size(payloads, upload_limit=0, upload_indices=None):
    """Return a small, bounded retrieval sample for one confirmed upload."""
    count = len(select_upload_payloads(payloads, upload_limit, upload_indices))
    if count <= 1:
        return count
    # Four samples cover the usual medium PDF in early/middle/late regions;
    # five is the cap for large files so retrieval diagnostics never become a
    # second sequential queue.
    return min(5, max(2, int(math.ceil(math.sqrt(count)))))


def response_contains_page_segment(response_text, expected):
    page_number = expected.get("page_number")
    segment_number = expected.get("segment_number")
    if page_number is None:
        return False
    page_found = bool(
        re.search(
            rf"(?:\bp|pdf\s+page|page)\s*0*{page_number}\b",
            response_text or "",
            re.I,
        )
    )
    if expected.get("representation") == "page_parent":
        return page_found
    if segment_number is None:
        return False
    segment_found = bool(
        re.search(
            rf"(?:\bs|segment)\s*0*{segment_number}\b",
            response_text or "",
            re.I,
        )
    )
    return page_found and segment_found


def validate_anythingllm_native_runtime(
    api_url,
    api_key,
    workspace_slug,
    payloads,
    upload_limit,
    storage_dir,
    upload_indices=None,
    embedder_probe_override=None,
    include_chat_probe=False,
    runtime_probe_limit=2,
    vector_timeout_seconds=45,
    vector_max_attempts=2,
    retry_timed_out_siblings=True,
    status_callback=None,
):
    validation_started = time.perf_counter()
    embedder_probe = (
        dict(embedder_probe_override)
        if isinstance(embedder_probe_override, dict) and embedder_probe_override
        else verify_anythingllm_runtime_embedder(api_url, api_key=api_key, storage_dir=storage_dir)
    )
    if isinstance(embedder_probe_override, dict) and embedder_probe_override:
        embedder_probe["cache_reused"] = True
    result = {
        "status": "not_checked",
        "workspace_slug": workspace_slug,
        "model_gate": read_workspace_model_gate(storage_dir, workspace_slug),
        "embedder_probe": embedder_probe,
        "vector_checks": [],
        "vector_recheck_status": "not_needed",
        "chat_check": {"status": "skipped_not_required"},
        "chat_probe_requested": bool(include_chat_probe),
        "validation_seconds": 0.0,
        "vector_search_seconds": 0.0,
        "chat_seconds": 0.0,
        "authentication_mode": "provided_api_key" if api_key else "none",
        "temporary_key_cleanup": {"status": "not_applicable", "error": ""},
    }
    if result["model_gate"].get("status") != "pass":
        result["status"] = "blocked_model_gate"
        return result

    runtime_key, authentication_mode = resolve_anythingllm_api_key(api_url, api_key, storage_dir)
    result["authentication_mode"] = authentication_mode
    temporary_key_id = None
    if not runtime_key:
        temporary_key = create_temporary_desktop_api_key(api_url)
        if temporary_key.get("status") != "created":
            result["status"] = "authentication_required"
            result["error"] = temporary_key.get("error", "")
            return result
        runtime_key = temporary_key["secret"]
        temporary_key_id = temporary_key["id"]
        result["authentication_mode"] = "temporary_desktop_api_key"

    runtime_probe_limit = max(1, int(runtime_probe_limit or 1))
    vector_timeout_seconds = max(1.0, float(vector_timeout_seconds or 1.0))
    vector_max_attempts = max(1, int(vector_max_attempts or 1))
    selected_payloads = select_runtime_validation_payloads(
        payloads, upload_limit, limit=runtime_probe_limit, upload_indices=upload_indices
    )
    result["probe_selection"] = [
        {
            "title": expected_page_segment_tokens(payload).get("title"),
            "chunk_source": expected_page_segment_tokens(payload).get("chunk_source"),
            "query_characters": len(runtime_validation_query_text(payload)),
        }
        for payload in selected_payloads
    ]

    def report_validation_progress(stage, **details):
        if not callable(status_callback):
            return
        try:
            status_callback(str(stage), dict(details))
        except Exception:
            # Runtime validation evidence must not depend on a UI observer.
            pass

    def execute_vector_check(payload, probe_index, probe_kind, max_attempts=None, recheck_of=""):
        """Run one provenance-gated vector query and retain its exact evidence."""
        expected = expected_page_segment_tokens(payload)
        normalized_text = runtime_validation_query_text(payload)
        if probe_index == 0:
            query = normalized_text[:240]
        else:
            query = "Find the indexed source passage discussing: " + normalized_text[:180]
        response = post_json_captured_with_retry(
            api_url.rstrip("/")
            + f"/api/v1/workspace/{workspace_slug}/vector-search",
            {"query": query, "topN": 10, "scoreThreshold": 0.0},
            api_key=runtime_key,
            timeout_label="vector search",
            # The first live query after an upload can be cold while LanceDB
            # and the configured embedder settle. The later, targeted
            # reconciliation below handles a remaining partial timeout rather
            # than silently treating storage proof as retrieval proof.
            timeout_seconds=vector_timeout_seconds,
            max_attempts=vector_max_attempts if max_attempts is None else max(1, int(max_attempts)),
        )
        rows = response["data"].get("results", []) if isinstance(response["data"], dict) else []
        top_metadata = rows[0].get("metadata", {}) if rows else {}
        top_chunk_source = top_metadata.get("chunkSource") or ""
        matching_ranks = [
            index + 1
            for index, row in enumerate(rows)
            if expected["chunk_source"]
            and str((row.get("metadata") or {}).get("chunkSource") or "")
            == expected["chunk_source"]
        ]
        return {
            "probe_kind": probe_kind,
            "recheck_of": recheck_of,
            "http_status": response["http_status"],
            "expected_title": expected["title"],
            "expected_chunk_source": expected["chunk_source"],
            "top_title": top_metadata.get("title") or "",
            "top_chunk_source": top_chunk_source,
            "top_1_expected": bool(
                rows
                and expected["chunk_source"]
                and top_chunk_source == expected["chunk_source"]
            ),
            "expected_in_top_n": bool(matching_ranks),
            "expected_result_rank": matching_ranks[0] if matching_ranks else None,
            "matching_result_count": len(matching_ranks),
            "result_count": len(rows),
            "error": response["error"],
            "endpoint": response.get("endpoint", ""),
            "error_class": response.get("error_class", "none"),
            "elapsed_seconds": response.get("total_elapsed_seconds", response.get("elapsed_seconds", 0)),
            "retry_count": response.get("retry_count", 0),
            "attempts": response.get("attempts", []),
        }

    try:
        for probe_index, payload in enumerate(selected_payloads):
            if probe_index == 0:
                probe_kind = "distinctive_exact_anchor"
            else:
                probe_kind = "natural_language_anchor"
            report_validation_progress(
                "vector_probe_started",
                completed=probe_index,
                total=len(selected_payloads),
                probe_kind=probe_kind,
            )
            result["vector_checks"].append(execute_vector_check(payload, probe_index, probe_kind))
            report_validation_progress(
                "vector_probe_completed",
                completed=probe_index + 1,
                total=len(selected_payloads),
                probe_kind=probe_kind,
                result_status=("pass" if result["vector_checks"][-1].get("expected_in_top_n") else "review"),
            )

        if include_chat_probe and selected_payloads:
            report_validation_progress("chat_probe_started", completed=0, total=1)
            expected = expected_page_segment_tokens(selected_payloads[0])
            prompt = (
                "Using only the indexed sources, identify the PDF page "
                + (
                    "and segment number "
                    if expected.get("representation") != "page_parent"
                    else ""
                )
                + "for the passage represented by the most relevant source. State the page "
                + (
                    "and segment exactly as shown in sourceDocument, "
                    if expected.get("representation") != "page_parent"
                    else "exactly as shown in sourceDocument, "
                )
                + "then briefly identify the passage. "
                + f"The passage begins: {selected_payloads[0].get('textContent', '')[:180]}"
            )
            chat_response = post_json_captured_with_retry(
                api_url.rstrip("/") + f"/api/v1/workspace/{workspace_slug}/chat",
                {
                    "message": prompt,
                    "mode": "query",
                    "sessionId": f"rag-native-validation-{int(time.time())}",
                    "reset": False,
                },
                api_key=runtime_key,
                timeout_label="DeepSeek chat",
                timeout_seconds=45,
                # Vector retrieval above is the indexing gate. A repeated
                # timed-out generation call adds up to another 45 seconds and
                # can duplicate provider work without strengthening the
                # citation verdict; preserve the first timeout as reviewable
                # runtime evidence instead.
                max_attempts=1,
            )
            chat_data = chat_response["data"] if isinstance(chat_response["data"], dict) else {}
            text_response = chat_data.get("textResponse") or ""
            result["chat_check"] = {
                "http_status": chat_response["http_status"],
                "configured_provider": result["model_gate"].get("chat_provider"),
                "configured_model": result["model_gate"].get("chat_model"),
                "expected_page": expected["page_number"],
                "expected_segment": expected["segment_number"],
                "response_contains_expected_page_segment": response_contains_page_segment(
                    text_response,
                    expected,
                ),
                "text_response": text_response,
                "source_count": len(chat_data.get("sources") or []),
                "error": chat_data.get("error") or chat_response["error"],
                "endpoint": chat_response.get("endpoint", ""),
                "error_class": chat_response.get("error_class", "none"),
                "elapsed_seconds": chat_response.get("total_elapsed_seconds", chat_response.get("elapsed_seconds", 0)),
                "retry_count": chat_response.get("retry_count", 0),
                "attempts": chat_response.get("attempts", []),
            }
            report_validation_progress(
                "chat_probe_completed",
                completed=1,
                total=1,
                result_status=("pass" if result["chat_check"].get("response_contains_expected_page_segment") else "review"),
            )

        # A successful sibling vector query establishes that the workspace is
        # searchable, while a timed-out sibling can simply have hit a cold
        # provider/cache window. Recheck only those timed-out records once.
        # This is evidence recovery, not an unbounded retry loop and never
        # re-submits any document for embedding.
        initial_vector_checks = list(result["vector_checks"])
        transient_classes = {"timeout", "connection", "transient_http"}
        if retry_timed_out_siblings and any(check.get("expected_in_top_n") for check in initial_vector_checks):
            timed_out_checks = [
                (index, payload, check)
                for index, (payload, check) in enumerate(zip(selected_payloads, initial_vector_checks))
                if str(check.get("error_class") or "") in transient_classes
            ]
            if timed_out_checks:
                result["vector_recheck_status"] = "attempted"
                for recheck_index, (probe_index, payload, original) in enumerate(timed_out_checks, start=1):
                    report_validation_progress(
                        "vector_recheck_started",
                        completed=recheck_index - 1,
                        total=len(timed_out_checks),
                    )
                    recheck = execute_vector_check(
                        payload,
                        probe_index,
                        f"{original.get('probe_kind') or 'vector'}_recheck",
                        max_attempts=1,
                        recheck_of=original.get("expected_chunk_source") or "",
                    )
                    result["vector_checks"].append(recheck)
                    report_validation_progress(
                        "vector_recheck_completed",
                        completed=recheck_index,
                        total=len(timed_out_checks),
                        result_status=("pass" if recheck.get("expected_in_top_n") else "review"),
                    )
                rechecks = result["vector_checks"][len(initial_vector_checks):]
                result["vector_recheck_status"] = (
                    "recovered" if all(check.get("expected_in_top_n") for check in rechecks) else "still_unresolved"
                )
    finally:
        if temporary_key_id:
            result["temporary_key_cleanup"] = cleanup_temporary_desktop_api_key(
                api_url,
                temporary_key_id,
            )

    expected_vector_sources = {
        str(check.get("expected_chunk_source") or "")
        for check in result["vector_checks"]
        if check.get("expected_chunk_source")
    }
    matched_vector_sources = {
        str(check.get("expected_chunk_source") or "")
        for check in result["vector_checks"]
        if check.get("expected_in_top_n") and check.get("expected_chunk_source")
    }
    vector_pass = bool(expected_vector_sources) and expected_vector_sources.issubset(matched_vector_sources)
    chat_pass = bool(
        result["chat_check"].get("response_contains_expected_page_segment")
    )
    chat_error = str(result["chat_check"].get("error") or "").casefold()
    unresolved_timed_vector_sources = {
        str(check.get("expected_chunk_source") or "")
        for check in result["vector_checks"]
        if str(check.get("error_class") or "") in {"timeout", "connection", "transient_http"}
        and check.get("expected_chunk_source")
        and check.get("expected_chunk_source") not in matched_vector_sources
    }
    vector_timeout = bool(unresolved_timed_vector_sources)
    vector_authentication_error = any(
        (
            "401" in str(check.get("error") or "")
            or "invalid api key" in str(check.get("error") or "").casefold()
            or "user not found" in str(check.get("error") or "").casefold()
        )
        for check in result["vector_checks"]
    )
    vector_any_pass = any(check.get("expected_in_top_n") for check in result["vector_checks"])
    if not vector_pass and vector_authentication_error:
        # A workspace can retain vectors created earlier while a later query
        # cannot be embedded because its active provider credential changed or
        # expired. This is a retrieval failure, not storage evidence.
        result["status"] = "blocked_provider_authentication"
    elif not vector_pass and vector_timeout and vector_any_pass:
        # Exact vector-storage checks have already covered every uploaded
        # location. A cold or queued live-search timeout therefore remains a
        # diagnostic when another exact live-search probe succeeds.
        result["status"] = "pass_with_vector_timeout"
    elif not vector_pass and vector_timeout:
        result["status"] = "vector_runtime_timeout"
    elif not vector_pass:
        result["status"] = "vector_retrieval_failed"
    elif not include_chat_probe:
        result["status"] = "pass"
    elif chat_pass:
        result["status"] = "pass"
    elif "401" in chat_error or "invalid api key" in chat_error:
        result["status"] = "blocked_provider_authentication"
    elif "timed out" in chat_error or "timeout" in chat_error:
        # A client-side chat timeout is operational evidence, not evidence that
        # indexed vectors or page metadata were wrong. Keep it distinct from a
        # completed answer that failed the citation contract.
        result["status"] = "pass_with_chat_timeout"
    else:
        result["status"] = "chat_citation_failed"
    result["vector_search_seconds"] = round(
        sum(float(check.get("elapsed_seconds") or 0.0) for check in result["vector_checks"]),
        3,
    )
    result["chat_seconds"] = round(
        float((result.get("chat_check") or {}).get("elapsed_seconds") or 0.0),
        3,
    )
    result["validation_seconds"] = round(time.perf_counter() - validation_started, 3)
    return result


def lancedb_table_names(db):
    if hasattr(db, "list_tables"):
        listed = db.list_tables()
        return list(getattr(listed, "tables", listed))
    return list(db.table_names())


def text_contains_page_or_segment_metadata(value):
    return bool(
        re.search(
            r"(?<![A-Za-z0-9])p\d{1,4}(?!\d)|segment|(?<![A-Za-z0-9])s\d{5}(?!\d)",
            str(value),
            re.I,
        )
    )


def expected_upload_needles(payloads, source_sha=""):
    needles = set()
    if source_sha:
        needles.add(str(source_sha)[:16])
    for payload in payloads[:100]:
        metadata = payload.get("metadata", {}) if isinstance(payload, dict) else {}
        title = str(metadata.get("title") or "")
        description = str(metadata.get("description") or "")
        chunk_source = str(metadata.get("chunkSource") or "")
        for value in [title, safe_stem(title), description, chunk_source]:
            value = str(value or "").strip()
            if value:
                needles.add(value)
        for match in re.findall(r"(?:pdf-p|logical-p|lp|p|s|ch)(\d{1,6})", f"{title} {description} {chunk_source}", flags=re.I):
            needles.add(match)
    return {needle for needle in needles if needle}


def inspect_native_metadata_rows(
    storage_dir: Path,
    source_sha: str,
    expected_chunk_sources=None,
    workspace_namespace=None,
):
    result = {
        "status": "not_inspected",
        "observation_mode": "full_diagnostic_materialization",
        "matching_rows": 0,
        "tables": [],
        "matching_table_names": [],
        "vector_ids": [],
        "metadata_fields_seen": [],
        "text_contains_source_document": False,
        "text_contains_segment_or_page": False,
        "error": "",
    }
    if not storage_dir.exists():
        result["status"] = "missing_storage_dir"
        return result
    try:
        import lancedb
    except ImportError:
        result["status"] = "missing_lancedb_python_package"
        return result
    try:
        db = lancedb.connect(str(storage_dir / "lancedb"))
        table_names = lancedb_table_names(db)
        fields_seen = set()
        vector_ids = set()
        matching_table_names = set()
        expected_chunk_sources = [str(value) for value in (expected_chunk_sources or []) if value]
        expected_chunk_source_set = set(expected_chunk_sources)
        for table_name in table_names:
            if workspace_namespace and table_name != workspace_namespace:
                continue
            table = db.open_table(table_name)
            columns = [field.name for field in table.schema]
            if "docSource" not in columns:
                result["tables"].append(
                    {
                        "table": table_name,
                        "matching_rows": 0,
                        "columns": columns,
                        "status": "docSource_column_missing",
                    }
                )
                continue
            expected_doc_source = f"local-pdf://sha256/{source_sha}"
            try:
                df = (
                    table.search()
                    .where(f"docSource = '{expected_doc_source}'", prefilter=True)
                    .limit(max(100, len(expected_chunk_sources) + 10))
                    .to_pandas()
                )
            except Exception as exc:
                result["tables"].append(
                    {
                        "table": table_name,
                        "matching_rows": 0,
                        "columns": columns,
                        "status": "filtered_query_failed",
                        "error": str(exc),
                    }
                )
                continue
            if df.empty:
                continue
            matches = df
            if expected_chunk_source_set and "chunkSource" in matches.columns:
                # Pandas accepts a set at runtime, but its typed API requires
                # a Sequence or Mapping. The values were already de-duplicated
                # above, so a list retains the same retrieval semantics.
                matches = matches[
                    matches["chunkSource"].astype(str).isin(list(expected_chunk_source_set))
                ]
            if matches.empty:
                continue
            for col in ["title", "docAuthor", "description", "docSource", "chunkSource", "published", "text"]:
                if col in matches.columns:
                    fields_seen.add(col)
            text_values = matches["text"].astype(str) if "text" in matches.columns else []
            table_entry = {
                "table": table_name,
                "matching_rows": len(matches),
                "columns": list(matches.columns),
            }
            result["tables"].append(table_entry)
            result["matching_rows"] += len(matches)
            matching_table_names.add(table_name)
            if "id" in matches.columns:
                vector_ids.update(str(value) for value in matches["id"].tolist() if value)
            if "text" in matches.columns:
                result["text_contains_source_document"] = result["text_contains_source_document"] or any(
                    "sourceDocument" in value for value in text_values
                )
                result["text_contains_segment_or_page"] = result["text_contains_segment_or_page"] or any(
                    text_contains_page_or_segment_metadata(value) for value in text_values
                )
        result["metadata_fields_seen"] = sorted(fields_seen)
        result["matching_table_names"] = sorted(matching_table_names)
        result["vector_ids"] = sorted(vector_ids)
        result["status"] = "complete"
    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)
    return result


def inspect_native_metadata_count(
    storage_dir: Path,
    source_sha: str,
    workspace_namespace="",
    expected_chunk_sources=None,
    identity_set_mode="on_completion",
):
    """Read a bounded vector-count observation for active polling.

    Polling should answer only whether indexing is advancing.  It must not
    repeatedly materialize every matching vector row while AnythingLLM is
    writing.  The caller retains the full metadata/vector audit for the final
    verification boundary.
    """
    result = {
        "status": "not_inspected",
        "observation_mode": "bounded_count_and_identity_set_on_completion",
        "sample_limit": 1,
        "matching_rows": 0,
        "chunk_source_filter_count": 0,
        "identity_set_checked": False,
        "identity_set_complete": None,
        "expected_chunk_source_count": 0,
        "observed_chunk_source_count": 0,
        "duplicate_chunk_source_count": 0,
        "missing_chunk_sources": [],
        "observed_chunk_sources": [],
        "matching_table_names": [],
        "metadata_fields_seen": [],
        "text_contains_segment_or_page": False,
        "text_contains_source_document": False,
        "error": "",
    }
    if not storage_dir.exists() or not workspace_namespace:
        result["status"] = "missing_storage_or_workspace"
        return result
    try:
        import lancedb
        db = lancedb.connect(str(storage_dir / "lancedb"))
        if workspace_namespace not in lancedb_table_names(db):
            result["status"] = "workspace_table_missing"
            return result
        table = db.open_table(workspace_namespace)
        columns = [field.name for field in table.schema]
        if "docSource" not in columns:
            result["status"] = "docSource_column_missing"
            return result
        expected_doc_source = f"local-pdf://sha256/{source_sha}".replace("'", "''")
        filters = [f"docSource = '{expected_doc_source}'"]
        normalized_chunk_sources = sorted(
            {
                str(value).strip()
                for value in (expected_chunk_sources or [])
                if str(value).strip()
            }
        )
        result["chunk_source_filter_count"] = len(normalized_chunk_sources)
        result["expected_chunk_source_count"] = len(normalized_chunk_sources)
        # Per-batch polling must observe the precise submitted identities, not
        # every segment previously indexed for the same PDF. A final full
        # diagnostic observation still checks all metadata and text fields.
        if normalized_chunk_sources and "chunkSource" in columns:
            quoted_sources = ",".join(
                "'" + value.replace("'", "''") + "'"
                for value in normalized_chunk_sources
            )
            filters.append(f"chunkSource IN ({quoted_sources})")
        row_filter = " AND ".join(filters)
        result["matching_rows"] = int(table.count_rows(filter=row_filter))
        result["matching_table_names"] = [workspace_namespace] if result["matching_rows"] else []
        result["metadata_fields_seen"] = [
            name for name in ("title", "docAuthor", "description", "docSource", "chunkSource", "published", "text")
            if name in columns
        ]
        # A one-row head is deliberately only a diagnostic sample. It gives a
        # useful page-marker hint without turning the five-second observer into
        # a document-wide materialization.
        if result["matching_rows"] and "text" in columns:
            sample = table.search().where(row_filter, prefilter=True).limit(1).to_pandas()
            if not sample.empty:
                text = str(sample.iloc[0].get("text") or "")
                result["text_contains_source_document"] = "sourceDocument" in text
                result["text_contains_segment_or_page"] = text_contains_page_or_segment_metadata(text)
        # A count proves that *some* matching rows exist, but equal counts can
        # still conceal a duplicated page-parent plus a missing sibling. Once
        # the bounded count reaches the planned total, fetch only the scalar
        # identity column and compare its complete set. This avoids reading
        # dense vectors and full text in the ordinary healthy path while
        # retaining complete coverage proof for every planned page-parent.
        identity_mode = str(identity_set_mode or "on_completion").casefold()
        result["identity_set_mode"] = identity_mode
        if (
            normalized_chunk_sources
            and "chunkSource" in columns
            and (
                identity_mode == "always"
                or result["matching_rows"] >= len(normalized_chunk_sources)
            )
        ):
            identities = (
                table.search()
                .where(row_filter, prefilter=True)
                .select(["chunkSource"])
                .limit(result["matching_rows"] + 1)
                .to_pandas()
            )
            identity_column = identities.get("chunkSource")
            identity_values = identity_column.tolist() if identity_column is not None else []
            observed_sources = [
                str(value).strip()
                for value in identity_values
                if str(value).strip()
            ]
            observed_set = set(observed_sources)
            expected_set = set(normalized_chunk_sources)
            missing_sources = sorted(expected_set - observed_set)
            result["identity_set_checked"] = True
            result["observed_chunk_source_count"] = len(observed_set)
            # Keep the exact scalar identities available to a bounded recovery
            # manifest. This avoids reopening LanceDB once per planned page at
            # a deadline while never reading text or dense vectors.
            result["observed_chunk_sources"] = sorted(observed_set)
            result["duplicate_chunk_source_count"] = max(0, len(observed_sources) - len(observed_set))
            result["missing_chunk_sources"] = missing_sources[:25]
            result["identity_set_complete"] = bool(
                not missing_sources
                and not result["duplicate_chunk_source_count"]
                and len(observed_sources) == len(expected_set)
            )
        result["status"] = "complete"
    except Exception as exc:
        result["status"] = "database_busy" if "lock" in str(exc).casefold() or "busy" in str(exc).casefold() else "error"
        result["error"] = str(exc)
    return result


def inspect_lancedb_vector_ids(storage_dir: Path, vector_ids):
    result = {
        "status": "not_checked",
        "matching_rows": 0,
        "metadata_fields_seen": [],
        "text_contains_page_or_segment": False,
        "text_contains_source_document": False,
        "tables": [],
        "error": "",
    }
    ids = [str(value) for value in vector_ids if value]
    if not ids:
        result["status"] = "no_vector_ids"
        return result
    try:
        import lancedb
        db = lancedb.connect(str(Path(storage_dir) / "lancedb"))
        fields_seen = set()
        for table_name in lancedb_table_names(db):
            table = db.open_table(table_name)
            columns = [field.name for field in table.schema]
            if "id" not in columns:
                continue
            table_rows = 0
            for start in range(0, len(ids), 100):
                batch = ids[start : start + 100]
                quoted = ",".join("'" + value.replace("'", "''") + "'" for value in batch)
                try:
                    frame = (
                        table.search()
                        .where(f"id IN ({quoted})", prefilter=True)
                        .limit(len(batch))
                        .to_pandas()
                    )
                except Exception as exc:
                    result["tables"].append(
                        {
                            "table": table_name,
                            "status": "filtered_query_failed",
                            "error": str(exc),
                        }
                    )
                    break
                if frame.empty:
                    continue
                table_rows += len(frame)
                for column in ["title", "docAuthor", "description", "docSource", "chunkSource", "published", "text"]:
                    if column in frame.columns:
                        fields_seen.add(column)
                if "text" in frame.columns:
                    text_values = frame["text"].astype(str)
                    result["text_contains_source_document"] = result["text_contains_source_document"] or any(
                        "sourceDocument" in value for value in text_values
                    )
                    result["text_contains_page_or_segment"] = result["text_contains_page_or_segment"] or any(
                        text_contains_page_or_segment_metadata(value)
                        for value in text_values
                    )
            if table_rows:
                result["tables"].append(
                    {
                        "table": table_name,
                        "status": "matched",
                        "matching_rows": table_rows,
                        "columns": columns,
                    }
                )
                result["matching_rows"] += table_rows
        result["metadata_fields_seen"] = sorted(fields_seen)
        result["status"] = "complete"
    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)
    return result


def inspect_lancedb_workspace_table(storage_dir: Path, workspace_slug: str, sample_limit=5):
    result = {
        "status": "not_checked",
        "workspace_slug": (workspace_slug or "").strip(),
        "matching_rows": 0,
        "columns": [],
        "metadata_fields_seen": [],
        "text_contains_page_or_segment": False,
        "text_contains_source_document": False,
        "sample_rows": [],
        "error": "",
    }
    slug = (workspace_slug or "").strip()
    if not slug:
        result["status"] = "missing_workspace"
        return result
    try:
        import lancedb

        db = lancedb.connect(str(Path(storage_dir) / "lancedb"))
        table_names = lancedb_table_names(db)
        if slug not in table_names:
            result["status"] = "workspace_table_missing"
            return result
        table = db.open_table(slug)
        result["matching_rows"] = int(table.count_rows())
        # ``to_arrow().slice`` materializes the whole workspace table before
        # trimming it. Ask LanceDB for the bounded sample instead.
        table_head = getattr(table, "head")
        head_result = table_head(max(1, int(sample_limit or 1)))
        sample = head_result.to_arrow() if hasattr(head_result, "to_arrow") else head_result
        result["columns"] = list(sample.schema.names)
        rows = sample.to_pylist()
        result["sample_rows"] = rows
        metadata_fields = [
            field
            for field in ["title", "docAuthor", "description", "docSource", "chunkSource", "published", "text"]
            if field in result["columns"]
        ]
        result["metadata_fields_seen"] = metadata_fields
        text_values = [str(row.get("text") or "") for row in rows]
        result["text_contains_source_document"] = any("sourceDocument" in value for value in text_values)
        result["text_contains_page_or_segment"] = any(
            text_contains_page_or_segment_metadata(value) for value in text_values
        )
        result["status"] = "complete"
    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)
    return result


def inspect_uploaded_location_files(storage_dir, upload_locations, expected_needles=None):
    result = {
        "status": "not_checked",
        "existing_files": 0,
        "matching_files": 0,
        "metadata_visible": False,
        "sample_path": "",
        "sample_record": {},
        "error": "",
        "reported_locations": 0,
        "resolved_locations": 0,
        "missing_locations": 0,
        "rejected_locations": 0,
        "desktop_drawer_root_locations": 0,
        "desktop_drawer_nested_locations": 0,
    }
    documents_root = (Path(storage_dir) / "documents").resolve()
    reported_locations = [str(location) for location in (upload_locations or []) if location]
    result["reported_locations"] = len(reported_locations)
    locations = []
    for location in reported_locations:
        path = Path(location)
        path = path.resolve() if path.is_absolute() else (documents_root / path).resolve()
        try:
            relative_path = path.relative_to(documents_root)
        except ValueError:
            # Upload locations are untrusted report data. Never read an
            # arbitrary absolute path merely because it appeared in a report.
            result["rejected_locations"] += 1
            continue
        if relative_path.parts and relative_path.parts[0] == "custom-documents":
            # AnythingLLM Desktop 1.15's drawer currently enumerates only the
            # immediate custom-documents entries. This is compatibility
            # evidence, not a claim that the authenticated renderer has shown
            # the record.
            if len(relative_path.parts) == 2:
                result["desktop_drawer_root_locations"] += 1
            elif len(relative_path.parts) > 2:
                result["desktop_drawer_nested_locations"] += 1
        locations.append(path)
    result["resolved_locations"] = len(locations)
    needles = [str(needle).strip() for needle in (expected_needles or []) if str(needle).strip()]
    if not locations:
        result["status"] = "no_locations"
        return result
    try:
        for path in locations:
            if not path.exists():
                result["missing_locations"] += 1
                continue
            result["existing_files"] += 1
            text = path.read_text(encoding="utf-8", errors="replace")
            try:
                record = json.loads(text)
            except Exception:
                record = {"raw": text[:1200]}
            haystack = " ".join([str(path), json.dumps(record, ensure_ascii=False)[:4000]])
            if not result["sample_path"]:
                result["sample_path"] = str(path)
                result["sample_record"] = record
            if text_contains_page_or_segment_metadata(haystack):
                result["metadata_visible"] = True
            if not needles or any(needle in haystack for needle in needles):
                result["matching_files"] += 1
        result["status"] = "complete"
    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)
    return result


def storage_observation_fingerprint(storage_dir: Path):
    """Return a small read-only fingerprint for SQLite/Lance write races."""
    rows = []
    for relative in ("anythingllm.db", "anythingllm.db-wal", "anythingllm.db-shm"):
        path = Path(storage_dir) / relative
        try:
            stat = path.stat()
            rows.append({"file": relative, "size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)})
        except OSError:
            rows.append({"file": relative, "missing": True})
    return rows


def observe_anythingllm_desktop_frontend_workspace(api_url, workspace_slug, timeout=3.0):
    """Report that the authenticated Desktop drawer cannot be probed remotely.

    AnythingLLM 1.15's renderer uses its authenticated in-app session for the
    Documents drawer. A localhost process without that session cannot honestly
    treat an unauthenticated HTTP response as drawer evidence. Keep this
    structured result for reports, but leave visible-drawer confirmation to a
    person or a deliberate in-app test.
    """
    result = {
        "status": "requires_authenticated_desktop_session",
        "workspace_slug": str(workspace_slug or ""),
        "http_status": None,
        "workspace_found": False,
        "document_count": None,
        "message": "The localhost app cannot verify AnythingLLM's authenticated Documents drawer.",
        "error": "",
    }
    slug = str(workspace_slug or "").strip()
    if not slug:
        result["status"] = "missing_workspace_slug"
        result["message"] = "Drawer observation was skipped because no workspace slug was supplied."
        return result
    if not str(api_url or "").strip() or not is_local_anythingllm_url(api_url):
        result["status"] = "not_applicable_nonlocal_runtime"
        result["message"] = "Drawer observation is available only for a local AnythingLLM Desktop runtime."
    return result


def full_post_upload_observation_is_required(
    fast_report,
    expected_records,
    *,
    failed_checkpoint=False,
    ambiguous_submission=False,
):
    """Keep broad storage/frontend inspection off a healthy success path.

    Exact, provenance-matched vector coverage is the ordinary completion
    evidence. A broad scan remains mandatory when there is an incomplete,
    contradicted, or failed-checkpoint result. An ambiguous HTTP receipt alone
    is no longer enough once its complete exact identity set is observed.
    """
    expected = max(0, int(expected_records or 0))
    observed = max(
        int((fast_report or {}).get("matching_vector_rows") or 0),
        int((fast_report or {}).get("lancedb_matching_rows") or 0),
    )
    status = str((fast_report or {}).get("status") or "")
    diagnostic_error = "busy" in status.casefold() or "error" in status.casefold()
    return not (
        expected > 0
        and observed >= expected
        and status in REVIEWABLE_POST_UPLOAD_STATUSES
        and not failed_checkpoint
        and not diagnostic_error
    )


def verify_anythingllm_post_upload(storage_dir: Path, workspace_slug, source_sha, payloads, upload_locations=None, observation_mode="full", frontend_api_url=""):
    result = {
        "status": "not_checked",
        "workspace_slug": workspace_slug,
        "workspace_found": False,
        "workspace_document_count": 0,
        "workspace_document_observation": "not_checked",
        "workspace_document_global_count": 0,
        "workspace_documents_globally_unused": False,
        "desktop_frontend_observation": "not_checked",
        "desktop_frontend_document_count": None,
        "desktop_frontend_document_count_matches_storage": None,
        "desktop_frontend_http_status": None,
        "desktop_frontend_message": "",
        "matching_workspace_documents": 0,
        "matching_vector_rows": 0,
        "metadata_survived_in_workspace_documents": False,
        "lancedb_matching_rows": 0,
        "lancedb_matching_tables": [],
        "lancedb_text_contains_page_or_segment": False,
        "upload_location_existing_files": 0,
        "upload_location_matching_files": 0,
        "upload_location_metadata_visible": False,
        "upload_location_sample_path": "",
        "desktop_drawer_root_locations": 0,
        "desktop_drawer_nested_locations": 0,
        "desktop_drawer_layout": "not_checked",
        "desktop_drawer_workspace_relative_paths": 0,
        "desktop_drawer_workspace_absolute_paths": 0,
        "desktop_drawer_workspace_path_status": "not_checked",
        "expected_payload_count": 0,
        "uploaded_payload_count": 0,
        "upload_chain_local_expected_count": 0,
        "upload_chain_custom_documents_matching_count": 0,
        "upload_chain_lancedb_matching_count": 0,
        "chunk_survival_ratio": 0.0,
        "chunk_survival_flag": "unknown",
        "identity_set_checked": False,
        "identity_set_complete": None,
        "expected_chunk_source_count": 0,
        "observed_chunk_source_count": 0,
        "duplicate_chunk_source_count": 0,
        "missing_chunk_sources": [],
        "page_provenance_risk": "unknown",
        "classification": "not_checked",
        "message": "",
        "error": "",
    }
    result["storage_fingerprint_before"] = storage_observation_fingerprint(storage_dir)
    db_path = storage_dir / "anythingllm.db"
    if not db_path.exists():
        result["status"] = "missing_db"
        result["message"] = "AnythingLLM SQLite database was not found."
        return result
    con = None
    try:
        con = sqlite_readonly_connection(db_path)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        workspace = cur.execute("select id,name,slug from workspaces where slug = ?", (workspace_slug,)).fetchone()
        if not workspace:
            result["status"] = "workspace_missing"
            result["message"] = f"Workspace `{workspace_slug}` was not found."
            return result
        result["workspace_found"] = True
        workspace_id = workspace["id"]
        normalized_observation_mode = str(observation_mode or "full").casefold()
        if normalized_observation_mode in {"fast", "identity"}:
            # The five-second observer needs only a bounded health signal.
            # Pulling every workspace-document JSON blob on every poll caused
            # a batch-global cost once per PDF, and it could race Desktop
            # writes. The complete document-row evidence is collected once at
            # the final verification boundary or in diagnostic mode.
            docs = []
            result["workspace_document_count"] = int(
                cur.execute(
                    "select count(*) from workspace_documents where workspaceId = ?",
                    (workspace_id,),
                ).fetchone()[0]
                or 0
            )
            result["workspace_document_observation"] = "counts_only_deferred_to_full_observation"
        else:
            docs = [dict(row) for row in cur.execute(
                "select docId,filename,docpath,metadata from workspace_documents where workspaceId = ?",
                (workspace_id,),
            )]
            result["workspace_document_count"] = len(docs)
            result["workspace_document_observation"] = "full_targeted_materialization"
        global_workspace_document_count = cur.execute(
            "select count(*) from workspace_documents"
        ).fetchone()[0]
        result["workspace_document_global_count"] = int(global_workspace_document_count or 0)
        result["workspace_documents_globally_unused"] = result["workspace_document_global_count"] == 0
        expected_payloads = payloads
        result["expected_payload_count"] = len(expected_payloads)
        expected_needles = expected_upload_needles(expected_payloads, source_sha=source_sha)
        expected_chunk_sources = [
            str((payload.get("metadata", {}) or {}).get("chunkSource") or "")
            for payload in expected_payloads
            if isinstance(payload, dict)
        ]
        location_report = inspect_uploaded_location_files(
            storage_dir,
            upload_locations,
            expected_needles=expected_needles,
        )
        result["upload_location_existing_files"] = location_report.get("existing_files", 0)
        result["upload_location_matching_files"] = location_report.get("matching_files", 0)
        result["upload_location_metadata_visible"] = bool(location_report.get("metadata_visible"))
        result["upload_location_sample_path"] = location_report.get("sample_path", "")
        result["upload_location_reported_count"] = location_report.get("reported_locations", 0)
        result["upload_location_missing_count"] = location_report.get("missing_locations", 0)
        result["upload_location_rejected_count"] = location_report.get("rejected_locations", 0)
        result["desktop_drawer_root_locations"] = location_report.get("desktop_drawer_root_locations", 0)
        result["desktop_drawer_nested_locations"] = location_report.get("desktop_drawer_nested_locations", 0)
        if result["desktop_drawer_nested_locations"]:
            result["desktop_drawer_layout"] = "nested_may_be_hidden"
        elif result["desktop_drawer_root_locations"]:
            result["desktop_drawer_layout"] = "root_layout_compatible"
        elif upload_locations:
            result["desktop_drawer_layout"] = "unknown"
        result["uploaded_payload_count"] = (
            result["upload_location_matching_files"]
            or result["upload_location_existing_files"]
            or result["expected_payload_count"]
        )
        result["upload_chain_local_expected_count"] = result["expected_payload_count"]
        result["upload_chain_custom_documents_matching_count"] = result["upload_location_matching_files"]
        matching_docs = []
        for doc in docs:
            haystack = " ".join(str(doc.get(key) or "") for key in ["filename", "docpath", "metadata"])
            # Recovery/checkpoint verification is a subset operation. A
            # source hash, document title, or page number is shared by many
            # sibling segments and therefore cannot identify this batch.
            # Prefer exact chunkSource identities whenever the payloads carry
            # them; use the broader legacy needles only for old payloads that
            # lack that field.
            if (
                any(chunk_source in haystack for chunk_source in expected_chunk_sources)
                if expected_chunk_sources
                else any(needle and needle in haystack for needle in expected_needles)
            ):
                matching_docs.append(doc)
        result["matching_workspace_documents"] = len(matching_docs)
        for doc in matching_docs:
            raw_docpath = str(doc.get("docpath") or "").strip()
            if not raw_docpath:
                continue
            if Path(raw_docpath).is_absolute():
                result["desktop_drawer_workspace_absolute_paths"] += 1
            elif raw_docpath.replace("\\", "/").lstrip("/").startswith(
                "custom-documents/"
            ):
                result["desktop_drawer_workspace_relative_paths"] += 1
        if result["matching_workspace_documents"]:
            if result["desktop_drawer_workspace_absolute_paths"]:
                result["desktop_drawer_workspace_path_status"] = "absolute_paths_incompatible"
            elif (
                result["desktop_drawer_workspace_relative_paths"]
                == result["matching_workspace_documents"]
            ):
                result["desktop_drawer_workspace_path_status"] = "relative_paths_compatible"
            else:
                result["desktop_drawer_workspace_path_status"] = "mixed_or_unknown"
        result["metadata_survived_in_workspace_documents"] = any(
            any(term in str(doc.get("metadata") or "") for term in ["docSource", "chunkSource", "PDF page", "Segment:", "source_sha256"])
            or bool(re.search(r"\bp\d{3,4}\b.*\bs\d{5}\b|\bs\d{5}\b.*\bp\d{3,4}\b", str(doc.get("filename") or ""), re.I))
            for doc in matching_docs
        )
        vector_ids = []
        if matching_docs:
            doc_ids = [doc["docId"] for doc in matching_docs if doc.get("docId")]
            if doc_ids:
                placeholders = ",".join("?" for _ in doc_ids)
                vector_ids = [
                    row[0]
                    for row in cur.execute(
                    f"select vectorId from document_vectors where docId in ({placeholders})",
                    doc_ids,
                    ).fetchall()
                ]
                result["matching_vector_rows"] = len(vector_ids)
        native_rows = (
            inspect_native_metadata_count(
                storage_dir,
                source_sha,
                workspace_namespace=workspace_slug,
                expected_chunk_sources=expected_chunk_sources,
                identity_set_mode=(
                    "always" if normalized_observation_mode == "identity" else "on_completion"
                ),
            )
            if normalized_observation_mode in {"fast", "identity"}
            else inspect_native_metadata_rows(
                storage_dir,
                source_sha,
                expected_chunk_sources,
                workspace_namespace=workspace_slug,
            )
        )
        native_vector_ids = native_rows.get("vector_ids", [])
        if native_vector_ids:
            placeholders = ",".join("?" for _ in native_vector_ids)
            matching_native_vector_count = cur.execute(
                f"select count(*) from document_vectors where vectorId in ({placeholders})",
                native_vector_ids,
            ).fetchone()[0]
            result["matching_vector_rows"] = max(
                result["matching_vector_rows"],
                matching_native_vector_count,
            )
        vector_rows = (
            {"matching_rows": 0, "text_contains_page_or_segment": False}
            if normalized_observation_mode in {"fast", "identity"}
            else inspect_lancedb_vector_ids(storage_dir, vector_ids)
        )
        if normalized_observation_mode in {"fast", "identity"}:
            # A Lance table row is itself vector evidence.  The count is a
            # bounded readiness signal, not a replacement for the later full
            # metadata audit.
            result["matching_vector_rows"] = max(
                result["matching_vector_rows"], int(native_rows.get("matching_rows") or 0)
            )
        result["lancedb_matching_rows"] = max(
            native_rows.get("matching_rows", 0),
            vector_rows.get("matching_rows", 0),
        )
        result["identity_set_checked"] = bool(native_rows.get("identity_set_checked", False))
        result["identity_set_complete"] = native_rows.get("identity_set_complete")
        result["expected_chunk_source_count"] = int(native_rows.get("expected_chunk_source_count") or 0)
        result["observed_chunk_source_count"] = int(native_rows.get("observed_chunk_source_count") or 0)
        result["duplicate_chunk_source_count"] = int(native_rows.get("duplicate_chunk_source_count") or 0)
        result["missing_chunk_sources"] = list(native_rows.get("missing_chunk_sources") or [])
        result["observed_chunk_sources"] = list(native_rows.get("observed_chunk_sources") or [])
        result["upload_chain_lancedb_matching_count"] = result["lancedb_matching_rows"]
        result["lancedb_matching_tables"] = native_rows.get("matching_table_names", [])
        result["lancedb_text_contains_page_or_segment"] = bool(
            native_rows.get("text_contains_segment_or_page", False)
            or vector_rows.get("text_contains_page_or_segment", False)
        )
        result["observation_mode"] = normalized_observation_mode
        if result["uploaded_payload_count"] > 0 and result["lancedb_matching_rows"] > 0:
            result["chunk_survival_ratio"] = round(
                float(result["lancedb_matching_rows"]) / float(result["uploaded_payload_count"]),
                4,
            )
            if result["lancedb_matching_rows"] == result["uploaded_payload_count"]:
                result["chunk_survival_flag"] = "preserved"
            elif result["lancedb_matching_rows"] > result["uploaded_payload_count"]:
                result["chunk_survival_flag"] = "likely_rechunked"
            else:
                result["chunk_survival_flag"] = "partial_or_missing"
        elif result["uploaded_payload_count"] > 0:
            result["chunk_survival_flag"] = "missing_after_upload"
        identity_provenance_evidence = bool(
            result["identity_set_checked"] and result["identity_set_complete"]
        )
        if result["lancedb_text_contains_page_or_segment"] or identity_provenance_evidence:
            result["page_provenance_risk"] = "low"
        elif result["lancedb_matching_rows"] > 0:
            result["page_provenance_risk"] = "medium"
        else:
            result["page_provenance_risk"] = "unknown"
        vectors_are_in_target_namespace = workspace_slug in result["lancedb_matching_tables"]
        vector_namespace_evidence_exists = (
            result["matching_vector_rows"] > 0 and vectors_are_in_target_namespace
        )
        # Searchability of one or two probes is useful evidence, but it never
        # proves that the complete planned payload arrived.  In particular a
        # cancelled/failed embedding batch can leave genuinely retrievable
        # vectors behind.  Report that honestly instead of letting the later
        # document/vector branches call the upload a pass.
        if result["identity_set_checked"] and not result["identity_set_complete"]:
            result["status"] = "partial_vector_coverage"
            result["classification"] = "page_parent_identity_set_mismatch"
            result["message"] = (
                "The target workspace did not expose one exact identity for every planned page-parent "
                f"({result['observed_chunk_source_count']}/{result['expected_chunk_source_count']} unique; "
                f"{result['duplicate_chunk_source_count']} duplicate row(s)). Reconciliation or deep verification is required."
            )
        elif result["chunk_survival_flag"] == "partial_or_missing":
            result["status"] = "partial_vector_coverage"
            result["classification"] = "planned_payloads_not_fully_observed"
            result["message"] = (
                f"Only {result['lancedb_matching_rows']} matching LanceDB row(s) were observed for "
                f"{result['uploaded_payload_count']} planned/uploaded payload(s). Retrieval may work for a subset, "
                "but this document is incomplete and needs review or explicit recovery."
            )
        elif not matching_docs and vector_namespace_evidence_exists:
            if normalized_observation_mode in {"fast", "identity"}:
                # Fast polling deliberately counts workspace rows without
                # materializing their metadata, so ``matching_docs`` is empty
                # by design. Do not turn that deferred observation into a
                # false document-list warning when exact expected vectors and
                # page/segment provenance are already present.
                if result["lancedb_text_contains_page_or_segment"] or identity_provenance_evidence:
                    result["status"] = "pass"
                    result["classification"] = "native_metadata_llm_visible_fast_document_observation_deferred"
                    result["message"] = (
                        "Exact expected vectors are embedded in the target workspace namespace and retain "
                        "page/segment evidence. Workspace document metadata observation was deferred to a "
                        "separate deep verification and does not block retrieval."
                    )
                else:
                    result["status"] = "review"
                    result["classification"] = "native_metadata_source_panel_only_fast_document_observation_deferred"
                    result["message"] = (
                        "Exact expected vectors are embedded, but page/segment evidence was not visible in the "
                        "fast observation. Use Verify deeply before relying on page-cited retrieval."
                    )
            elif result["workspace_documents_globally_unused"]:
                if result["lancedb_text_contains_page_or_segment"] or identity_provenance_evidence:
                    result["status"] = "pass"
                    result["classification"] = "native_metadata_llm_visible_legacy_workspace_table_unused"
                    result["message"] = (
                        "Matching vectors are embedded in the target workspace namespace and LanceDB text retains "
                        "page/segment evidence. This AnythingLLM install appears not to use workspace_documents rows "
                        "at all, so vector/Lance evidence is treated as authoritative."
                    )
                else:
                    result["status"] = "review"
                    result["classification"] = "native_metadata_source_panel_only_legacy_workspace_table_unused"
                    result["message"] = (
                        "Matching vectors are embedded in the target workspace namespace, but this AnythingLLM install "
                        "appears not to use workspace_documents rows. Vector/Lance evidence suggests the upload worked, "
                        "yet page/segment text markers were not found."
                    )
            else:
                result["status"] = "pass_with_missing_workspace_document_records"
                result["classification"] = (
                    "native_metadata_llm_visible_vector_only"
                    if result["lancedb_text_contains_page_or_segment"]
                    else "native_metadata_source_panel_only_vector_only"
                )
                result["message"] = (
                    "Matching vectors are embedded in the target workspace namespace, but the final storage "
                    "observation could not confirm workspace document-list rows. Retrieval is independently "
                    "verified; inspect the workspace document list before relying on document-management operations."
                )
        elif not matching_docs and result["upload_location_matching_files"] > 0:
            result["status"] = "docs_without_vectors"
            result["classification"] = "raw_upload_present_not_embedded"
            result["message"] = (
                "AnythingLLM wrote matching raw files to custom-documents, but no searchable vectors were observed. "
                "File handoff is not proof of indexing; the document must be recovered or re-embedded."
            )
        elif not matching_docs:
            result["status"] = "no_matching_native_docs"
            result["classification"] = "not_uploaded_or_not_identifiable"
            result["message"] = "No matching native-metadata documents were found in the target workspace."
        elif result["matching_vector_rows"] <= 0:
            result["status"] = "docs_without_vectors"
            result["classification"] = "uploaded_not_embedded"
            result["message"] = "Matching workspace documents exist, but no vector rows were found."
        elif result["lancedb_text_contains_page_or_segment"] or identity_provenance_evidence:
            result["status"] = "pass"
            result["classification"] = "native_metadata_llm_visible"
            result["message"] = "Matching documents and vectors exist, and LanceDB text appears to include page/segment metadata."
        else:
            result["status"] = "review"
            result["classification"] = "native_metadata_source_panel_only"
            result["message"] = "Matching documents and vectors exist, but page/segment metadata was not found in LanceDB text. Use filename/title-header fallback if answers cannot cite pages."
        if result["desktop_drawer_nested_locations"]:
            result["status"] = "review"
            result["classification"] = "desktop_drawer_layout_nested"
            result["message"] = (
                "Stored vectors were observed, but this upload uses a nested custom-documents layout that "
                "AnythingLLM Desktop 1.15 may hide in its Documents drawer. Re-upload with document folders off "
                "for visible drawer evidence."
            )
        if result["desktop_drawer_workspace_absolute_paths"]:
            result["status"] = "review"
            result["classification"] = "desktop_drawer_workspace_path_incompatible"
            result["message"] = (
                "Documents and vectors were stored, but the workspace rows use absolute document paths. "
                "AnythingLLM Desktop 1.15 matches its Drawer against relative custom-documents paths, "
                "so this workspace will not show the attachments. Re-upload with normalized relative paths."
            )
        if normalized_observation_mode != "fast":
            frontend = observe_anythingllm_desktop_frontend_workspace(
                frontend_api_url,
                workspace_slug,
            )
            result["desktop_frontend_observation"] = frontend.get("status") or "not_checked"
            result["desktop_frontend_document_count"] = frontend.get("document_count")
            result["desktop_frontend_http_status"] = frontend.get("http_status")
            result["desktop_frontend_message"] = frontend.get("message") or ""
            if frontend.get("status") == "observed":
                matches_storage = (
                    int(frontend.get("document_count") or 0)
                    == int(result["workspace_document_count"] or 0)
                )
                result["desktop_frontend_document_count_matches_storage"] = matches_storage
                if not matches_storage:
                    result["status"] = "review"
                    result["classification"] = "desktop_frontend_document_count_mismatch"
                    result["message"] = (
                        "AnythingLLM storage and vectors were observed, but the Desktop frontend returned "
                        f"{int(frontend.get('document_count') or 0)} workspace document(s) while SQLite returned "
                        f"{int(result['workspace_document_count'] or 0)}. Treat document-manager visibility as "
                        "unresolved and review it before relying on workspace document operations."
                    )
        if result["chunk_survival_flag"] == "likely_rechunked":
            result["message"] += (
                f" Uploaded payloads expanded from {result['uploaded_payload_count']} to "
                f"{result['lancedb_matching_rows']} LanceDB row(s), so chunk boundaries likely did not survive intact."
            )
        elif result["chunk_survival_flag"] == "preserved":
            result["message"] += (
                f" Uploaded payload count ({result['uploaded_payload_count']}) matched the observed LanceDB row count."
            )
        result["storage_fingerprint_after"] = storage_observation_fingerprint(storage_dir)
        result["storage_changed_during_observation"] = (
            result["storage_fingerprint_before"] != result["storage_fingerprint_after"]
        )
        if (
            result["storage_changed_during_observation"]
            and result["status"] in {"no_matching_native_docs", "docs_without_vectors"}
        ):
            result["status"] = "concurrent_write_ambiguous"
            result["classification"] = "concurrent_write_ambiguous"
            result["message"] = (
                "AnythingLLM storage changed while it was observed; missing vectors/documents are ambiguous. "
                "Retry verification only after the writer settles."
            )
            result["recovery_next_check"] = "Repeat the read-only workspace/vector observation; do not re-upload or re-embed."
    except (sqlite3.OperationalError, OSError) as exc:
        # A live Desktop write can briefly hold SQLite/Lance. Inspection is
        # evidence, never a reason to reinterpret an accepted upload as a
        # failed submission. Preserve a precise recovery obligation instead.
        result["status"] = "verified_unavailable"
        result["classification"] = "storage_inspection_unavailable"
        result["error"] = str(exc)
        result["message"] = "Upload evidence could not be observed because AnythingLLM storage was busy or unavailable; rerun verification only."
        result["recovery_next_check"] = "Retry read-only workspace/vector observation after Desktop write activity settles; do not re-upload or re-embed."
    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)
        result["message"] = "Post-upload verification failed while reading AnythingLLM storage."
    finally:
        if con is not None:
            con.close()
    return result


def workspace_storage_inspector(storage_dir: Path, workspace_slug):
    result = {
        "status": "not_checked",
        "workspace_slug": workspace_slug,
        "workspace_name": "",
        "workspace_found": False,
        "workspace_document_count": 0,
        "raw_native_doc_count": 0,
        "embedded_chunk_count": 0,
        "sample_workspace_document": {},
        "sample_custom_document_path": "",
        "sample_custom_document_record": {},
        "sample_lancedb_row": {},
        "lancedb_workspace_row_count": 0,
        "page_segment_visibility": "not_checked",
        "metadata_fields_seen": [],
        "sqlite_workspace_metadata_fields": [],
        "custom_document_json_fields": [],
        "lancedb_row_fields": [],
        "storage_dir": str(storage_dir),
        "error": "",
    }
    slug = (workspace_slug or "").strip()
    if not slug:
        result["status"] = "missing_workspace"
        return result
    db_path = storage_dir / "anythingllm.db"
    if not db_path.exists():
        result["status"] = "missing_db"
        result["error"] = f"AnythingLLM database was not found at {db_path}"
        return result
    try:
        con = sqlite_readonly_connection(db_path)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        workspace = cur.execute(
            "select id,name,slug from workspaces where slug = ? limit 1",
            (slug,),
        ).fetchone()
        if not workspace:
            result["status"] = "workspace_missing"
            result["error"] = f"Workspace `{slug}` was not found."
            return result
        result["workspace_found"] = True
        result["workspace_name"] = workspace["name"] or workspace["slug"] or slug
        # This used to fetch every workspace document into Python merely to
        # compute two counts and select one sample. Large workspaces made the
        # supposedly read-only inspection dominate batch time. Keep the
        # diagnostic semantics but ask SQLite for aggregates and one sample.
        workspace_id = workspace["id"]
        result["workspace_document_count"] = int(cur.execute(
            "select count(*) from workspace_documents where workspaceId = ?", (workspace_id,)
        ).fetchone()[0] or 0)
        result["raw_native_doc_count"] = int(cur.execute(
            "select count(*) from workspace_documents where workspaceId = ? and filename like 'raw-%'",
            (workspace_id,),
        ).fetchone()[0] or 0)
        result["embedded_chunk_count"] = int(cur.execute(
            "select count(*) from document_vectors dv "
            "join workspace_documents wd on wd.docId = dv.docId "
            "where wd.workspaceId = ?",
            (workspace_id,),
        ).fetchone()[0] or 0)
        sample_row = cur.execute(
            "select id,docId,filename,docpath,metadata,createdAt from workspace_documents "
            "where workspaceId = ? order by case when filename like 'raw-%' then 0 else 1 end, id desc limit 1",
            (workspace_id,),
        ).fetchone()
        sample_doc = dict(sample_row) if sample_row else None
        vector_ids = []
        if sample_doc:
            result["sample_workspace_document"] = sample_doc
            metadata_text = sample_doc.get("metadata") or ""
            try:
                parsed_metadata = json.loads(metadata_text) if metadata_text else {}
            except Exception:
                parsed_metadata = {"raw": metadata_text}
            result["sample_workspace_document"]["metadata_parsed"] = parsed_metadata
            if isinstance(parsed_metadata, dict):
                result["sqlite_workspace_metadata_fields"] = sorted(parsed_metadata.keys())
            docpath = sample_doc.get("docpath") or ""
            custom_path = storage_dir / "documents" / docpath if docpath else None
            if custom_path and custom_path.exists():
                result["sample_custom_document_path"] = str(custom_path)
                try:
                    result["sample_custom_document_record"] = json.loads(
                        custom_path.read_text(encoding="utf-8", errors="replace")
                    )
                    if isinstance(result["sample_custom_document_record"], dict):
                        result["custom_document_json_fields"] = sorted(result["sample_custom_document_record"].keys())
                except Exception as exc:
                    result["sample_custom_document_record"] = {"read_error": str(exc)}
            if sample_doc.get("docId"):
                vector_ids = [
                    row[0]
                    for row in cur.execute(
                        "select vectorId from document_vectors where docId = ? order by id limit 5",
                        (sample_doc["docId"],),
                    ).fetchall()
                ]
        con.close()
        workspace_lance = inspect_lancedb_workspace_table(storage_dir, slug)
        if workspace_lance.get("status") == "complete":
            result["lancedb_workspace_row_count"] = int(workspace_lance.get("matching_rows") or 0)
            result["embedded_chunk_count"] = max(
                int(result.get("embedded_chunk_count") or 0),
                int(workspace_lance.get("matching_rows") or 0),
            )
            if workspace_lance.get("metadata_fields_seen"):
                result["metadata_fields_seen"] = workspace_lance.get("metadata_fields_seen", [])
            if workspace_lance.get("sample_rows"):
                result["sample_lancedb_row"] = workspace_lance.get("sample_rows", [{}])[0]
                if isinstance(result["sample_lancedb_row"], dict):
                    result["lancedb_row_fields"] = sorted(result["sample_lancedb_row"].keys())
        elif workspace_lance.get("status") == "error" and workspace_lance.get("error"):
            result["sample_lancedb_row"] = {"read_error": workspace_lance.get("error")}

        if vector_ids:
            vector_report = inspect_lancedb_vector_ids(storage_dir, vector_ids)
            if vector_report.get("tables") and not result.get("metadata_fields_seen"):
                result["metadata_fields_seen"] = vector_report.get("metadata_fields_seen", [])
            if vector_report.get("status") == "complete" and not result.get("sample_lancedb_row"):
                try:
                    import lancedb

                    db = lancedb.connect(str(storage_dir / "lancedb"))
                    for table_name in lancedb_table_names(db):
                        if table_name != slug:
                            continue
                        table = db.open_table(table_name)
                        for vector_id in vector_ids:
                            vector_id_sql = str(vector_id).replace("'", "''")
                            rows = (
                                table.search()
                                .where(f"id = '{vector_id_sql}'", prefilter=True)
                                .limit(1)
                                .to_list()
                            )
                            if rows:
                                result["sample_lancedb_row"] = rows[0]
                                if isinstance(result["sample_lancedb_row"], dict):
                                    result["lancedb_row_fields"] = sorted(result["sample_lancedb_row"].keys())
                                break
                        if result["sample_lancedb_row"]:
                            break
                except Exception as exc:
                    result["sample_lancedb_row"] = {"read_error": str(exc)}

        if not result.get("metadata_fields_seen"):
            merged_fields = set(result.get("sqlite_workspace_metadata_fields") or [])
            merged_fields.update(result.get("custom_document_json_fields") or [])
            merged_fields.update(result.get("lancedb_row_fields") or [])
            result["metadata_fields_seen"] = sorted(merged_fields)

        page_segment_in_metadata = False
        sample_doc_record = result.get("sample_custom_document_record") or {}
        sample_lance_row = result.get("sample_lancedb_row") or {}
        metadata_probe = " ".join(
            str(value or "")
            for value in [
                result.get("sample_workspace_document", {}).get("filename"),
                json.dumps(result.get("sample_workspace_document", {}).get("metadata_parsed", {}), ensure_ascii=False),
                sample_doc_record.get("title"),
                sample_doc_record.get("description"),
                sample_doc_record.get("chunkSource"),
                sample_lance_row.get("title"),
                sample_lance_row.get("description"),
                sample_lance_row.get("chunkSource"),
            ]
        )
        page_segment_in_metadata = text_contains_page_or_segment_metadata(metadata_probe)
        text_probe = str(sample_lance_row.get("text") or "")
        if text_contains_page_or_segment_metadata(text_probe):
            result["page_segment_visibility"] = "visible_in_chunk_text"
        elif workspace_lance.get("text_contains_page_or_segment"):
            result["page_segment_visibility"] = "visible_in_chunk_text"
        elif page_segment_in_metadata:
            result["page_segment_visibility"] = "visible_in_metadata_only"
        else:
            result["page_segment_visibility"] = "not_detected"
        result["status"] = "complete"
    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)
    return result


def anythingllm_storage_audit(storage_dir: Path, workspace_slug=""):
    result = {
        "status": "not_checked",
        "storage_dir": str(storage_dir),
        "workspace_slug": (workspace_slug or "").strip(),
        "workspace_found": False,
        "workspace_document_global_count": 0,
        "workspace_document_selected_count": 0,
        "document_vector_global_count": 0,
        "custom_document_json_global_count": 0,
        "missing_docpath_file_count": 0,
        "unreferenced_custom_document_count": 0,
        "orphan_vector_docid_count": 0,
        "sample_missing_docpaths": [],
        "sample_unreferenced_custom_documents": [],
        "sample_orphan_vector_docids": [],
        "error": "",
    }
    db_path = storage_dir / "anythingllm.db"
    if not db_path.exists():
        result["status"] = "missing_db"
        result["error"] = f"AnythingLLM database was not found at {db_path}"
        return result
    documents_root = storage_dir / "documents"
    con = None
    try:
        con = sqlite_readonly_connection(db_path)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        all_docs = [
            dict(row)
            for row in cur.execute(
                "select workspaceId,docId,filename,docpath,metadata from workspace_documents order by id desc"
            )
        ]
        result["workspace_document_global_count"] = len(all_docs)
        selected_docs = all_docs
        slug = (workspace_slug or "").strip()
        if slug:
            workspace = cur.execute(
                "select id,slug from workspaces where slug = ? limit 1",
                (slug,),
            ).fetchone()
            if not workspace:
                result["status"] = "workspace_missing"
                result["error"] = f"Workspace `{slug}` was not found."
                return result
            result["workspace_found"] = True
            selected_docs = [doc for doc in all_docs if doc.get("workspaceId") == workspace["id"]]
        result["workspace_document_selected_count"] = len(selected_docs)
        result["document_vector_global_count"] = int(
            cur.execute("select count(*) from document_vectors").fetchone()[0] or 0
        )

        global_doc_ids = {
            str(doc.get("docId") or "").strip()
            for doc in all_docs
            if str(doc.get("docId") or "").strip()
        }
        vector_doc_ids = {
            str(row[0] or "").strip()
            for row in cur.execute("select distinct docId from document_vectors where docId is not null")
            if str(row[0] or "").strip()
        }
        orphan_vector_doc_ids = sorted(vector_doc_ids - global_doc_ids)
        result["orphan_vector_docid_count"] = len(orphan_vector_doc_ids)
        result["sample_orphan_vector_docids"] = orphan_vector_doc_ids[:10]

        referenced_docpaths = {
            str(doc.get("docpath") or "").replace("\\", "/").strip("/")
            for doc in all_docs
            if str(doc.get("docpath") or "").strip()
        }
        missing_docpaths = []
        for doc in selected_docs:
            docpath = str(doc.get("docpath") or "").replace("\\", "/").strip("/")
            if not docpath:
                continue
            custom_path = documents_root / docpath
            if not custom_path.exists():
                missing_docpaths.append(docpath)
        result["missing_docpath_file_count"] = len(missing_docpaths)
        result["sample_missing_docpaths"] = missing_docpaths[:10]

        custom_files = []
        if documents_root.exists():
            custom_files = [
                path.relative_to(documents_root).as_posix()
                for path in documents_root.rglob("*.json")
            ]
        result["custom_document_json_global_count"] = len(custom_files)
        unreferenced_custom_documents = sorted(
            docpath for docpath in custom_files if docpath not in referenced_docpaths
        )
        result["unreferenced_custom_document_count"] = len(unreferenced_custom_documents)
        result["sample_unreferenced_custom_documents"] = unreferenced_custom_documents[:10]
        result["status"] = "complete"
    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)
    finally:
        if con is not None:
            con.close()
    return result


def anythingllm_stale_artifact_report(storage_dir: Path, workspace_slug=""):
    audit = anythingllm_storage_audit(storage_dir, workspace_slug)
    result = {
        "status": audit.get("status", "error"),
        "storage_dir": audit.get("storage_dir", str(storage_dir)),
        "workspace_slug": audit.get("workspace_slug", (workspace_slug or "").strip()),
        "workspace_found": audit.get("workspace_found", False),
        "audit": audit,
        "candidate_buckets": [],
        "recommended_sequence": [],
        "operator_summary": "",
        "error": audit.get("error", ""),
    }
    if audit.get("status") != "complete":
        result["operator_summary"] = audit.get("error") or "Dry-run stale-artifact report could not run."
        return result

    candidate_buckets = []
    if int(audit.get("missing_docpath_file_count") or 0) > 0:
        candidate_buckets.append(
            {
                "bucket": "workspace_rows_missing_custom_document_files",
                "count": int(audit.get("missing_docpath_file_count") or 0),
                "scope": "selected_workspace" if result["workspace_slug"] else "global",
                "risk": "high",
                "reason": (
                    "workspace_documents rows reference custom-document JSON paths that are no longer present on disk."
                ),
                "recommended_first_step": (
                    "Export and inspect the affected workspace_documents rows before any deletion or rewrite."
                ),
            }
        )
    if int(audit.get("unreferenced_custom_document_count") or 0) > 0:
        candidate_buckets.append(
            {
                "bucket": "unreferenced_custom_document_json_files",
                "count": int(audit.get("unreferenced_custom_document_count") or 0),
                "scope": "global",
                "risk": "medium",
                "reason": (
                    "Custom-document JSON files exist under AnythingLLM storage but are not referenced by workspace_documents."
                ),
                "recommended_first_step": (
                    "Correlate the files with recent failed uploads and verify that no active workflow still depends on them."
                ),
            }
        )
    if int(audit.get("orphan_vector_docid_count") or 0) > 0:
        candidate_buckets.append(
            {
                "bucket": "orphan_document_vectors",
                "count": int(audit.get("orphan_vector_docid_count") or 0),
                "scope": "global",
                "risk": "high",
                "reason": (
                    "document_vectors rows exist whose docId does not map back to any workspace_documents row."
                ),
                "recommended_first_step": (
                    "Verify whether the orphan docIds still have matching LanceDB rows and custom-document files before cleanup."
                ),
            }
        )

    result["candidate_buckets"] = candidate_buckets
    result["recommended_sequence"] = [
        {
            "step": 1,
            "title": "Create a fresh AnythingLLM backup",
            "details": "Snapshot the SQLite database and storage/documents tree before any repair attempt.",
        },
        {
            "step": 2,
            "title": "Export candidate artifacts without mutating storage",
            "details": (
                "Write a reviewable CSV/JSON report for missing workspace rows, unreferenced custom-document files, "
                "and orphan vector docIds."
            ),
        },
        {
            "step": 3,
            "title": "Review missing workspace-document file references first",
            "details": (
                "These are the highest-risk inconsistencies because the relational layer points at paths that no longer exist."
            ),
        },
        {
            "step": 4,
            "title": "Review unreferenced custom-document files second",
            "details": (
                "These often come from failed or partial uploads and are safer to classify before touching vectors."
            ),
        },
        {
            "step": 5,
            "title": "Review orphan vector docIds last",
            "details": (
                "Vector cleanup should happen only after the file/document layer is understood, because it is the hardest layer to reconstruct."
            ),
        },
    ]
    if candidate_buckets:
        result["operator_summary"] = (
            f"Dry-run report found {len(candidate_buckets)} stale-artifact bucket(s). "
            "No deletion was performed."
        )
    else:
        result["operator_summary"] = "Dry-run report found no stale-artifact buckets."
    return result


def workspace_segment_preview(storage_dir: Path, workspace_slug, chunk_source="", title="", segment_id=""):
    result = {
        "status": "not_checked",
        "workspace_slug": (workspace_slug or "").strip(),
        "segment_id": segment_id or "",
        "chunk_source": chunk_source or "",
        "title": title or "",
        "matching_workspace_documents": 0,
        "matching_vector_rows": 0,
        "workspace_document": {},
        "custom_document_record": {},
        "lancedb_rows": [],
        "error": "",
    }
    slug = (workspace_slug or "").strip()
    if not slug:
        result["status"] = "missing_workspace"
        return result
    db_path = storage_dir / "anythingllm.db"
    if not db_path.exists():
        result["status"] = "missing_db"
        result["error"] = f"AnythingLLM database was not found at {db_path}"
        return result
    try:
        con = sqlite_readonly_connection(db_path)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        workspace = cur.execute(
            "select id,name,slug from workspaces where slug = ? limit 1",
            (slug,),
        ).fetchone()
        if not workspace:
            result["status"] = "workspace_missing"
            result["error"] = f"Workspace `{slug}` was not found."
            return result
        docs = [
            dict(row)
            for row in cur.execute(
                "select id,docId,filename,docpath,metadata,createdAt from workspace_documents where workspaceId = ? order by id desc",
                (workspace["id"],),
            )
        ]
        matches = []
        needles = [str(chunk_source or "").strip(), str(title or "").strip(), str(segment_id or "").strip()]
        needles = [needle for needle in needles if needle]
        for doc in docs:
            doc_haystack = " ".join(
                [
                    str(doc.get("filename") or ""),
                    str(doc.get("docpath") or ""),
                    str(doc.get("metadata") or ""),
                ]
            )
            if needles and any(needle in doc_haystack for needle in needles):
                matches.append(doc)
        result["matching_workspace_documents"] = len(matches)
        if matches:
            selected_doc = matches[0]
            result["workspace_document"] = selected_doc
            docpath = selected_doc.get("docpath") or ""
            custom_path = storage_dir / "documents" / docpath if docpath else None
            if custom_path and custom_path.exists():
                try:
                    result["custom_document_record"] = json.loads(
                        custom_path.read_text(encoding="utf-8", errors="replace")
                    )
                except Exception as exc:
                    result["custom_document_record"] = {"read_error": str(exc)}
            vector_ids = [
                row[0]
                for row in cur.execute(
                    "select vectorId from document_vectors where docId = ? order by id",
                    (selected_doc.get("docId"),),
                ).fetchall()
            ]
            result["matching_vector_rows"] = len(vector_ids)
            con.close()
            if vector_ids:
                try:
                    import lancedb

                    db = lancedb.connect(str(storage_dir / "lancedb"))
                    table = db.open_table(slug)
                    for vector_id in vector_ids[:10]:
                        vector_id_sql = str(vector_id).replace("'", "''")
                        rows = (
                            table.search()
                            .where(f"id = '{vector_id_sql}'", prefilter=True)
                            .limit(1)
                            .to_list()
                        )
                        if rows:
                            result["lancedb_rows"].append(rows[0])
                except Exception as exc:
                    result["lancedb_rows"] = [{"read_error": str(exc)}]
        else:
            con.close()
        result["status"] = "complete"
    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)
    return result


def build_html_report(profile, candidates, selected, output_paths, storage_report, upload_report):
    candidate_rows = []
    for candidate in candidates:
        quality = candidate.get("quality") or {}
        candidate_rows.append(
            f"<tr><td>{html.escape(candidate['backend'])}</td><td>{candidate.get('score', '')}</td>"
            f"<td>{quality.get('included_words', '')}</td><td>{len(candidate.get('segments', []))}</td>"
            f"<td>{candidate.get('marker_stats', {}).get('marker_char_ratio', '')}</td>"
            f"<td>{html.escape(', '.join(candidate.get('score_reasons', [])) or 'none')}</td></tr>"
        )
    if selected["readiness_status"] != "ready":
        selected_status = "Needs review"
    elif upload_report.get("status") == "complete":
        selected_status = "Extraction ready; native upload completed pending storage verification"
    else:
        selected_status = "Extraction ready; native metadata behavior not yet proven in AnythingLLM"
    outline_validation = selected.get("outline_validation") or {}
    marker_stats = selected.get("marker_stats") or {}
    fallback_note = (
        "Repeated inline metadata is available only in the explicitly named fallback artifact."
        if selected.get("fallback_marker_status") != "disabled"
        else "Inline metadata fallback generation was disabled for this run."
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>AnythingLLM Readiness Report</title>
<style>body{{font-family:Segoe UI,Arial,sans-serif;max-width:1100px;margin:32px auto;line-height:1.45}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ccc;padding:6px;text-align:left}}
code{{background:#eee;padding:1px 4px}}</style></head>
<body>
<h1>AnythingLLM Readiness Report</h1>
<h2>{html.escape(selected_status)}</h2>
<p><b>Selected backend:</b> {html.escape(selected['backend'])}</p>
<p><b>Source:</b> {html.escape(profile['source_file'])}</p>
<p><b>Detected pages:</b> {profile['pdf_page_count']} |
<b>Body start:</b> {selected['start_page']} ({html.escape(selected['start_reason'])}) |
<b>End matter:</b> {selected.get('end_page') or 'not detected'}</p>
<p><b>Segments:</b> {len(selected['segments'])} |
<b>Inline fallback marker check:</b> {selected['chunk_eval']['chunks_without_marker']} chunks without marker</p>
<p><b>Inline fallback marker overhead:</b> {marker_stats.get('marker_char_ratio', '')} of marker+content characters |
<b>Average content chars:</b> {marker_stats.get('avg_content_chars', '')} |
<b>Short segments under 180 chars:</b> {marker_stats.get('short_segments_under_180_chars', '')}</p>
<p>The primary <code>anythingllm-upload.txt</code> contains clean passage text. {html.escape(fallback_note)}</p>
<p><b>PDF outline validation:</b> {html.escape(outline_validation.get('reliability', 'unknown'))}
({outline_validation.get('pass_rate', '')} sampled pass rate). Bookmark mismatches are treated as warnings/fallback signals.</p>
<h2>Backend Scores</h2>
<table><tr><th>Backend</th><th>Score</th><th>Words</th><th>Segments</th><th>Marker Ratio</th><th>Warnings</th></tr>
{''.join(candidate_rows)}
</table>
<h2>Generated Files</h2>
<ul>
{''.join(f'<li><code>{html.escape(str(path))}</code></li>' for path in output_paths)}
</ul>
<h2>AnythingLLM Storage Inspection</h2>
<p>Status: <code>{html.escape(storage_report.get('status', 'unknown'))}</code></p>
<h2>API Upload</h2>
<p>Status: <code>{html.escape(upload_report.get('status', 'unknown'))}</code>; uploaded: {upload_report.get('uploaded', 0)}</p>
</body></html>"""


def _prepare_pdf_legacy_engine(pdf_path: Path, out_root: Path, args):
    """Characterized legacy engine.

    New callers must use ``prepare_pdf`` or the common orchestration façade.
    This private function remains intact while its tightly coupled report and
    external-integration data flow is migrated into typed phase results.
    """
    total_started = time.perf_counter()
    progress_callback = getattr(args, "progress_callback", None)

    def report_progress(
        value,
        stage,
        *,
        desktop_required=False,
        phase="",
        completed_units=None,
        total_units=None,
        evidence_kind="",
    ):
        if callable(progress_callback):
            try:
                progress_callback(
                    float(value),
                    str(stage),
                    desktop_required=bool(desktop_required),
                    phase=str(phase or ""),
                    completed_units=completed_units,
                    total_units=total_units,
                    evidence_kind=str(evidence_kind or ""),
                )
            except TypeError:
                # The Advanced diagnostics callback predates the structured
                # Automatic-worker protocol. It remains a two-argument local
                # callback and has no Desktop-dependent phases.
                progress_callback(float(value), str(stage))
            except Exception:
                # UI/status observers are informative only. A browser callback
                # failure must never cancel extraction, local artifacts, or an
                # already accepted AnythingLLM upload.
                return False
        return True

    progress_prepare_and_upload = bool(getattr(args, "prepare_and_upload", False))
    report_upload_phase = UploadPhaseReporter(report_progress, progress_prepare_and_upload).emit

    pdf_path = Path(pdf_path)
    # Native ingestion is normally the slowest user-visible phase.  Give it a
    # proportionate evidence range rather than making a three-of-twenty batch
    # upload appear ~82% complete.  Local-only preparation retains the older
    # packaging-oriented range because it has no embedding phase.
    selection_progress = 0.15 if progress_prepare_and_upload else 0.60
    metadata_progress = 0.20 if progress_prepare_and_upload else 0.72
    storage_progress = 0.23 if progress_prepare_and_upload else PIPELINE_PROGRESS_STORAGE_INSPECTION
    report_upload_phase(
        "metadata",
        "Reading PDF metadata",
        completed_units=0,
        total_units=1,
        fallback_fraction=0.01,
        evidence_kind="phase_started",
    )
    metadata_started = time.perf_counter()
    def report_hash_progress(completed, total):
        mib = 1024 * 1024
        report_upload_phase(
            "metadata",
            f"Hashing source PDF: {float(completed) / mib:.1f}/{max(1.0, float(total) / mib):.1f} MiB",
            completed_units=completed,
            total_units=total,
            fallback_fraction=0.0,
            evidence_kind="source_hash_progress",
        )

    source_sha = sha256_file(pdf_path, progress_callback=report_hash_progress)
    def report_metadata_progress(step, completed, total):
        detail = (
            "Profiling PDF page geometry"
            if str(step) == "page_geometry"
            else "Sampling PDF metadata for author detection"
        )
        report_upload_phase(
            "metadata",
            f"{detail}: {int(completed)}/{max(1, int(total))}",
            completed_units=completed,
            total_units=total,
            fallback_fraction=0.0,
            evidence_kind="metadata_progress",
        )

    pdf_meta = pdf_metadata(
        pdf_path,
        include_page_geometry=True,
        include_author_samples=True,
        progress_callback=report_metadata_progress,
    )
    report_upload_phase(
        "metadata",
        "PDF metadata and page profile ready",
        completed_units=1,
        total_units=1,
        fallback_fraction=0.05,
        evidence_kind="phase_completed",
    )
    author_text_samples = pdf_meta.pop("_author_text_samples", [])
    author_sample_error = str(pdf_meta.pop("_author_sample_error", "") or "")
    use_file_title_fallback = getattr(args, "use_file_title_fallback", True)
    if args.document_label:
        title = args.document_label
        title_source = "user_override"
    elif pdf_meta.get("title"):
        title = pdf_meta["title"]
        title_source = "pdf_metadata"
    elif use_file_title_fallback:
        title = pdf_path.stem
        title_source = "filename_fallback"
    else:
        title = "Untitled PDF"
        title_source = "generated_placeholder"
    inferred_author = (
        {"author": "", "source": "error", "page": 0, "evidence": author_sample_error}
        if author_sample_error
        else infer_author_from_samples_or_filename(author_text_samples, pdf_path, title_hint=title)
    )
    if args.document_author:
        author = args.document_author
        author_source = "user_override"
    elif pdf_meta.get("author"):
        metadata_author = normalize_text(pdf_meta.get("author") or "")
        inferred_source = str(inferred_author.get("source") or "")
        inferred_value = normalize_text(inferred_author.get("author") or "")
        if (
            inferred_value
            and inferred_value.casefold() != metadata_author.casefold()
            and inferred_source in {"text_affiliated_byline", "text_bibliographic_byline"}
        ):
            author = inferred_value
            author_source = f"{inferred_source}_overrode_pdf_metadata"
        else:
            author = metadata_author
            author_source = "pdf_metadata"
    elif inferred_author.get("author"):
        author = inferred_author.get("author") or ""
        author_source = inferred_author.get("source") or "text_inference"
    else:
        author = ""
        author_source = "not_available"
    source_meta = {
        "source_id": f"pdf_{source_sha[:16]}",
        "source_title": normalize_text(title),
        "source_author": normalize_text(author),
        "source_sha256": source_sha,
        "source_published_epoch_ms": pdf_date_to_epoch_ms(pdf_meta.get("creationDate")),
        "metadata_provenance": {
            "source_title": title_source,
            "source_author": author_source,
            "subject": "pdf_metadata" if pdf_meta.get("subject") else "not_available",
        },
        "author_inference": inferred_author,
    }
    source_meta["source_short_label"] = normalize_text(
        args.document_short_label or default_short_label(source_meta["source_title"], source_meta["source_author"])
    )
    profile = {
        "source_file": str(pdf_path),
        "filename": pdf_path.name,
        "file_size": pdf_path.stat().st_size,
        "source_sha256": source_sha,
        **pdf_meta,
        "detected_title": source_meta["source_title"],
        "detected_author": source_meta["source_author"],
        "source_short_label": source_meta["source_short_label"],
        "metadata_provenance": source_meta["metadata_provenance"],
        "author_inference": inferred_author,
        "effective_subject": pdf_meta.get("subject") or (source_meta["source_title"] if use_file_title_fallback else ""),
        "effective_subject_provenance": (
            "pdf_metadata"
            if pdf_meta.get("subject")
            else ("filename_or_title_fallback" if use_file_title_fallback else "not_available")
        ),
        "metadata_read_before_text_parse": True,
        "pipeline_version": PIPELINE_VERSION,
    }
    emit_pipeline_timing_event(
        args,
        "source_metadata_and_author_inference",
        elapsed_seconds=time.perf_counter() - metadata_started,
        source_bytes=profile["file_size"],
        pdf_pages=profile["pdf_page_count"],
        author_source=author_source,
        author_samples_reused=True,
    )
    outline = pdf_meta.get("outline") or []
    geometry_by_page = {
        int(row.get("pdf_page") or 0): row
        for row in pdf_meta.get("page_geometry", [])
    }
    storage_dir = (
        Path(args.anythingllm_storage_dir)
        if getattr(args, "anythingllm_storage_dir", "")
        else default_anythingllm_storage_dir()
    )
    shared_runtime_context = getattr(args, "batch_inspection_context", None)
    cached_config = (
        shared_runtime_context.get("anythingllm_preparation_config")
        if isinstance(shared_runtime_context, dict) else None
    )
    if isinstance(cached_config, dict):
        detected_chunk_settings = dict(cached_config["chunk_settings"])
        embedding_config = dict(cached_config["embedding_config"])
        embedder_policy = dict(cached_config["embedder_policy"])
    else:
        with measured_pipeline_phase(args, "anythingllm_configuration_resolution"):
            preflight_snapshot = anythingllm_preflight_snapshot(
                storage_dir,
                getattr(args, "simulation_adapter", None),
                runtime_verify=should_verify_anythingllm_runtime_during_preparation(args),
            )
            detected_chunk_settings = dict(preflight_snapshot["chunking"])
            embedding_config = dict(preflight_snapshot["embedder"])
            embedder_policy = dict(embedding_config.get("policy") or {})
        if isinstance(shared_runtime_context, dict):
            shared_runtime_context["anythingllm_preparation_config"] = {
                "chunk_settings": dict(detected_chunk_settings),
                "embedding_config": dict(embedding_config),
                "embedder_policy": dict(embedder_policy),
            }
            shared_runtime_context["resolved_anythingllm_runtime_state"] = dict(preflight_snapshot)
    auto_correction_report = {
        "status": "not_applied",
        "auto_corrected": False,
        "message": "Recommended AnythingLLM settings were not auto-applied during PDF preparation.",
        "policy": embedder_policy,
        "write_result": None,
    }
    chunk_size = int(getattr(args, "anythingllm_chunk_size", 0) or detected_chunk_settings["chunk_size"])
    try:
        embedder_chunk_limit = int(embedding_config.get("max_chunk_length") or 0)
    except (TypeError, ValueError):
        embedder_chunk_limit = 0
    if not embedder_chunk_limit and embedder_policy.get("recommended_limit"):
        embedder_chunk_limit = int(embedder_policy["recommended_limit"])
    if embedder_chunk_limit > 0:
        chunk_size = min(chunk_size, embedder_chunk_limit)
    chunk_overlap = int(
        getattr(args, "anythingllm_chunk_overlap", -1)
        if getattr(args, "anythingllm_chunk_overlap", -1) >= 0
        else detected_chunk_settings["chunk_overlap"]
    )
    if chunk_overlap >= chunk_size:
        chunk_overlap = max(0, chunk_size // 10)
    profile["anythingllm_chunk_simulation"] = {
        **detected_chunk_settings,
        "chunk_size": chunk_size,
        "chunk_overlap": chunk_overlap,
        "recommended_embedder_limit": embedder_policy.get("recommended_limit"),
        "embedder_limit_status": embedder_policy.get("status"),
        "overridden": bool(
            getattr(args, "anythingllm_chunk_size", 0)
            or getattr(args, "anythingllm_chunk_overlap", -1) >= 0
        ),
    }
    profile["anythingllm_embedding_config"] = embedding_config
    profile["anythingllm_embedder_policy"] = embedder_policy
    profile["anythingllm_auto_correction"] = auto_correction_report
    if isinstance(shared_runtime_context, dict) and "resolved_anythingllm_runtime_state" in shared_runtime_context:
        # Runtime/config resolution is a batch-global preflight observation;
        # it is not post-upload vector evidence. Reusing it avoids repeated
        # localhost/API probes while keeping each PDF's profile auditable.
        profile["anythingllm_resolved_state"] = dict(
            shared_runtime_context["resolved_anythingllm_runtime_state"]
        )
        profile["anythingllm_resolved_state_reused"] = True
    else:
        with measured_pipeline_phase(args, "anythingllm_runtime_state_resolution"):
            resolved_runtime_state = anythingllm_resolved_state(
                storage_dir,
                getattr(args, "simulation_adapter", None),
                runtime_verify=should_verify_anythingllm_runtime_during_preparation(args),
            )
        profile["anythingllm_resolved_state"] = resolved_runtime_state
        profile["anythingllm_resolved_state_reused"] = False
        if isinstance(shared_runtime_context, dict):
            shared_runtime_context["resolved_anythingllm_runtime_state"] = dict(resolved_runtime_state)
    requested_unstructured_strategy = getattr(args, "unstructured_strategy", "auto") or "auto"
    # The Automatic UI performs one batch-scoped capability check before the
    # user confirms. Reuse that immutable result for every PDF; a missing
    # preflight remains fully supported for CLI/direct callers.
    supplied_runtime_probe = getattr(args, "unstructured_runtime_probe", None)
    shared_unstructured_probe = (
        shared_runtime_context.get("unstructured_runtime_probe")
        if isinstance(shared_runtime_context, dict)
        else None
    )
    unstructured_runtime_probe = dict(
        supplied_runtime_probe
        if isinstance(supplied_runtime_probe, dict) and supplied_runtime_probe
        else shared_unstructured_probe
        if isinstance(shared_unstructured_probe, dict) and shared_unstructured_probe
        else {}
    )
    resolved_unstructured = resolve_unstructured_strategy(
        requested_unstructured_strategy,
        runtime_probe=unstructured_runtime_probe,
    )
    profile["unstructured_runtime"] = {
        **resolved_unstructured["runtime"],
        "requested_strategy": resolved_unstructured["requested"],
        "resolved_strategy": resolved_unstructured["resolved"],
        "selection_reason": resolved_unstructured["reason"],
    }

    backend_mode = (getattr(args, "backend_mode", "automatic") or "automatic").casefold()
    backend_aliases = {
        "pymupdf": "pymupdf",
        "pymupdf4llm": "pymupdf4llm",
        "unstructured": "unstructured",
    }
    if backend_mode in backend_aliases:
        backend_names = [backend_aliases[backend_mode]]
    else:
        backend_names = ["pymupdf", "pymupdf4llm"]
    if args.deep_extraction and "unstructured" not in backend_names:
        backend_names.append("unstructured")
    auto_unstructured_reasons = []
    auto_unstructured_suppressed_reasons = []
    automatic_candidate_shortcuts = []
    shared_boundary_reference = None

    candidates = []
    for backend_index, backend in enumerate(backend_names):
        if (
            backend_mode == "automatic"
            and backend == "pymupdf4llm"
            and not bool(args.deep_extraction)
            and has_complete_native_text_candidate(
                candidates,
                profile.get("pdf_page_count"),
                getattr(args, "ocr_preflight_hint", None),
            )
        ):
            # PyMuPDF4LLM's layout path can re-run Tesseract on every image
            # page even when the preceding native candidate already has
            # complete text and page-level provenance. Keep the explicit and
            # deep-extraction modes available; this is only an Automatic-mode
            # shortcut with a recorded, auditable basis.
            automatic_candidate_shortcuts.append(
                "pymupdf4llm_ocr_candidate_skipped_due_to_complete_native_text"
            )
            continue
        if backend == "unstructured" and not unstructured_runtime_probe:
            with measured_pipeline_phase(args, "unstructured_runtime_capability_probe"):
                unstructured_runtime_probe = dict(unstructured_runtime_status("hi_res"))
            if isinstance(shared_runtime_context, dict):
                shared_runtime_context["unstructured_runtime_probe"] = dict(
                    unstructured_runtime_probe
                )
        active_unstructured = resolve_unstructured_strategy(
            requested_unstructured_strategy,
            prior_candidates=candidates,
            runtime_probe=unstructured_runtime_probe,
        )
        profile["unstructured_runtime"] = {
            **active_unstructured["runtime"],
            "requested_strategy": active_unstructured["requested"],
            "resolved_strategy": active_unstructured["resolved"],
            "selection_reason": active_unstructured["reason"],
        }
        extraction_label = (
            f"Extracting and evaluating with {backend}"
            + (
                f" ({active_unstructured['resolved']})"
                if backend == "unstructured"
                else ""
            )
        )
        known_pages = max(1, int(pdf_meta.get("pdf_page_count") or 0))
        extraction_total = max(1, known_pages * max(1, len(backend_names)))
        report_upload_phase(
            "extraction",
            extraction_label,
            completed_units=backend_index * known_pages,
            total_units=extraction_total,
            fallback_fraction=backend_index / max(1, len(backend_names)),
            evidence_kind="phase_started",
        )
        candidate_dir = out_root / "candidates" / backend
        candidate_dir.mkdir(parents=True, exist_ok=True)
        unstructured_circuit = getattr(args, "unstructured_circuit_breaker", None)
        if backend == "unstructured" and isinstance(unstructured_circuit, dict) and unstructured_circuit.get("blocked"):
            candidates.append(
                {
                    "backend": backend,
                    "score": -999,
                    "score_reasons": ["unstructured_runtime_circuit_open"],
                    "error": str(unstructured_circuit.get("reason") or "Unstructured OCR runtime was unavailable earlier in this batch."),
                    "segments": [],
                    "quality": {},
                    "chunk_eval": {},
                    "native_chunk_eval": {},
                    "literal_results": [],
                    "vector_results": [],
                    "vector_error_detail": "",
                    "candidate_dir": str(candidate_dir),
                }
            )
            continue
        backend_started = time.perf_counter()
        try:
            def report_backend_page_progress(completed, total):
                page_total = max(1, int(total or known_pages))
                aggregate_total = max(1, page_total * max(1, len(backend_names)))
                aggregate_completed = backend_index * page_total + min(page_total, max(0, int(completed or 0)))
                report_upload_phase(
                    "extraction",
                    f"{extraction_label}: {min(page_total, max(0, int(completed or 0)))}/{page_total} pages processed",
                    completed_units=aggregate_completed,
                    total_units=aggregate_total,
                    fallback_fraction=aggregate_completed / aggregate_total,
                    evidence_kind="page_completed",
                )

            pages, page_count, element_rows = get_backend_pages(
                pdf_path,
                backend,
                active_unstructured["resolved"] if backend == "unstructured" else requested_unstructured_strategy,
                unstructured_runtime_probe=active_unstructured["runtime"] if backend == "unstructured" else None,
                unstructured_cache_dir=(
                    getattr(args, "unstructured_ocr_cache_dir", "")
                    if backend == "unstructured"
                    else None
                ),
                progress_callback=report_backend_page_progress,
            )
            layout_evidence = {
                "status": "not_applied",
                "reason": "Positioned native-line cleanup is currently limited to the native PyMuPDF backend.",
            }
            if backend == "pymupdf":
                pages, layout_evidence = apply_region_aware_native_layout(pdf_path, pages)
            elif backend == "unstructured":
                pages, layout_evidence = remove_verified_photographed_ocr_running_headers(pages)
            write_json(candidate_dir / "layout-region-review.json", layout_evidence)
            pymupdf4llm_execution = (
                pymupdf4llm_execution_evidence(pages)
                if backend == "pymupdf4llm"
                else {}
            )
            unstructured_execution = (
                unstructured_execution_evidence(pages)
                if backend == "unstructured"
                else {}
            )
            stats = enrich_page_stats(
                pages,
                [
                    page_stats_for(page, geometry_by_page.get(int(page.get("page") or 0)))
                    for page in pages
                ],
            )
            outline_validation = validate_outline_against_text(outline, pages, page_count)
            usable_outline = usable_outline_from_validation(outline, outline_validation)
            candidate_start_page, candidate_start_reason = detect_body_start(
                pages,
                stats,
                outline=usable_outline,
                include_front_matter=getattr(args, "include_front_matter", False),
            )
            first_page_override = int(getattr(args, "first_page_override", 0) or 0)
            if first_page_override > 0:
                candidate_start_page = min(max(1, first_page_override), page_count)
                candidate_start_reason = "user_override"
            end_headings = getattr(args, "end_section_names", None) or DEFAULT_END_SECTION_HEADINGS
            candidate_end_detected = detect_end_section_from_outline(usable_outline, page_count) or detect_end_section_start(
                pages, end_headings
            )
            end_page_override = int(getattr(args, "end_page_override", 0) or 0)
            if end_page_override > 0:
                candidate_detected_end_page = min(max(candidate_start_page + 1, end_page_override), page_count + 1)
                candidate_end_detected = {"page": candidate_detected_end_page, "heading": "User override", "source": "user_override"}
            else:
                candidate_detected_end_page = candidate_end_detected["page"] if candidate_end_detected else None
            include_back_matter = bool(getattr(args, "include_back_matter", False))
            candidate_end_page = None if include_back_matter else candidate_detected_end_page
            raw_quality = extraction_quality(pages, stats, 1, None)
            if shared_boundary_reference:
                start_page = int(shared_boundary_reference["start_page"])
                start_reason = f"shared_{shared_boundary_reference['start_reason']}"
                detected_end_page = shared_boundary_reference["detected_end_page"]
                end_detected = shared_boundary_reference["end_detected"]
                end_page = shared_boundary_reference["end_page"]
                boundary_reference_backend = shared_boundary_reference["backend"]
            else:
                reference_is_reliable = is_reliable_structure_reference(raw_quality, page_count)
                if reference_is_reliable:
                    start_page = candidate_start_page
                    start_reason = candidate_start_reason
                    detected_end_page = candidate_detected_end_page
                    end_detected = candidate_end_detected
                    end_page = candidate_end_page
                    boundary_reference_backend = backend
                else:
                    # Never let a weak extractor decide a different body slice
                    # from its peers. Preserve only an explicit user override;
                    # otherwise use the conservative whole-document range and
                    # surface the condition as a structural warning later.
                    start_page = candidate_start_page if first_page_override > 0 else 1
                    start_reason = (
                        "user_override"
                        if first_page_override > 0
                        else "conservative_unreliable_structure_fallback"
                    )
                    detected_end_page = candidate_detected_end_page if end_page_override > 0 else None
                    end_detected = candidate_end_detected if end_page_override > 0 else None
                    end_page = detected_end_page if end_page_override > 0 else None
                    boundary_reference_backend = "conservative_neutral"
                shared_boundary_reference = {
                    "backend": boundary_reference_backend,
                    "start_page": start_page,
                    "start_reason": start_reason,
                    "detected_end_page": detected_end_page,
                    "end_detected": end_detected,
                    "end_page": end_page,
                    "reliable": reference_is_reliable,
                }
            boundary_reconciled = bool(
                boundary_reference_backend != backend
                and (
                    candidate_start_page != start_page
                    or candidate_detected_end_page != detected_end_page
                    or candidate_end_page != end_page
                )
            )
            candidate_source_meta = {
                **source_meta,
                "body_start": start_page,
                "end_matter_start": detected_end_page,
                "selected_end_page": end_page,
                "boundary_reference_backend": boundary_reference_backend,
                "boundary_reference_reliable": bool(shared_boundary_reference.get("reliable")),
                "independent_start_page": candidate_start_page,
                "independent_detected_end_page": candidate_detected_end_page,
                "independent_end_page": candidate_end_page,
                "boundary_reconciled": boundary_reconciled,
                "include_back_matter": include_back_matter,
                "boundary_confidence": (
                    "high"
                    if start_reason.startswith("pdf_outline") and end_detected and end_detected.get("source") == "pdf_outline"
                    else "medium"
                    if start_reason != "first_nonempty_page"
                    else "low"
                ),
                "repeated_headers": sorted({s.repeated_header for s in stats if s.repeated_header}),
                "repeated_footers": sorted({s.repeated_footer for s in stats if s.repeated_footer}),
                "layout_evidence": layout_evidence,
                "duplicate_pages": {
                    s.pdf_page: s.duplicate_of_page
                    for s in stats
                    if s.duplicate_of_page is not None
                },
            }
            segments = make_segments(
                pdf_path,
                backend,
                pages,
                start_page,
                end_page,
                candidate_source_meta,
                args.target_passage_length,
                outline=usable_outline,
                segment_mode=getattr(args, "segment_mode", "passages"),
                effective_limit=chunk_size,
            )
            lane_review = proposed_supplementary_lane_review(
                segments,
                stats,
                page_count,
                layout_evidence=layout_evidence,
            )
            segments, lane_review = apply_automatic_supplementary_lane(
                segments,
                lane_review,
                exclude_from_primary=bool(getattr(args, "exclude_supplementary_end_matter", False)),
            )
            upload_text = generate_upload_text(segments, include_markers=False)
            generate_inline_fallback = not bool(getattr(args, "disable_inline_markers", False))
            fallback_upload_text = (
                generate_upload_text(
                    segments,
                    include_markers=True,
                    marker_style=args.marker_style,
                )
                if generate_inline_fallback
                else ""
            )
            marker_stats = marker_ratio_stats(
                segments,
                marker_style=args.marker_style,
                include_markers=generate_inline_fallback,
            )
            probes = add_user_validation_probes(
                generate_probes(segments),
                segments,
                getattr(args, "validation_phrases", []),
            )
            literal_results = literal_eval(upload_text, probes)
            chunk_eval = (
                chunk_marker_eval(
                    fallback_upload_text,
                    chunk_size=chunk_size,
                    overlap=chunk_overlap,
                )
                if generate_inline_fallback
                else {
                    "status": "disabled",
                    "chunk_size": chunk_size,
                    "chunk_overlap": chunk_overlap,
                    "chunk_count": 0,
                    "chunks_without_marker": 0,
                    "suspicious_chunks": 0,
                    "first_chunks_without_marker": [],
                    "first_suspicious_chunks": [],
                }
            )
            native_retrieval_units = simulate_native_header_chunks(
                segments,
                chunk_size=chunk_size,
                overlap=chunk_overlap,
            )
            native_chunk_eval = native_header_chunk_eval(native_retrieval_units)
            quality = extraction_quality(pages, stats, start_page, end_page)

            (candidate_dir / "anythingllm-upload.txt").write_text(upload_text, encoding="utf-8")
            write_json(candidate_dir / "retrieval-lane-review.json", lane_review)
            write_supplementary_lane_candidate_text(
                candidate_dir / "supplementary-content-candidates.txt", lane_review
            )
            if generate_inline_fallback:
                (candidate_dir / "anythingllm-upload-inline-metadata-fallback.txt").write_text(
                    fallback_upload_text,
                    encoding="utf-8",
                )
            append_jsonl(candidate_dir / "segment-manifest.jsonl", segments)
            write_csv(candidate_dir / "extraction-report.csv", [asdict(s) for s in stats])
            write_csv(candidate_dir / "outline-validation.csv", outline_validation["rows"])
            write_csv(candidate_dir / "metadata-ratio.csv", [marker_stats])
            append_jsonl(candidate_dir / "probes.jsonl", probes)
            write_csv(candidate_dir / "literal-results.csv", literal_results)
            write_csv(
                candidate_dir / "native-header-chunk-audit.csv",
                [
                    {
                        "retrieval_unit_id": row["retrieval_unit_id"],
                        "segment_id": row["segment_id"],
                        "pdf_page": row["pdf_page"],
                        "chapter": row.get("chapter") or "",
                        "native_header": row.get("native_header") or "",
                        "chunk_chars": len(row.get("text", "")),
                    }
                    for row in native_retrieval_units
                ],
            )
            if element_rows:
                write_csv(candidate_dir / "elements.csv", element_rows)

            variant_outputs = {}

            def write_upload_variant(name, variant_start, variant_end):
                if variant_start == start_page and variant_end == end_page:
                    return
                variant_segments = make_segments(
                    pdf_path,
                    backend,
                    pages,
                    variant_start,
                    variant_end,
                    candidate_source_meta,
                    args.target_passage_length,
                    outline=usable_outline,
                    segment_mode=getattr(args, "segment_mode", "passages"),
                    effective_limit=chunk_size,
                )
                if not variant_segments:
                    return
                variant_lane_review = proposed_supplementary_lane_review(
                    variant_segments,
                    stats,
                    page_count,
                    layout_evidence=layout_evidence,
                )
                variant_segments, _ = apply_automatic_supplementary_lane(
                    variant_segments,
                    variant_lane_review,
                    exclude_from_primary=bool(getattr(args, "exclude_supplementary_end_matter", False)),
                )
                if not variant_segments:
                    return
                variant_text = generate_upload_text(variant_segments, include_markers=False)
                variant_fallback_text = (
                    generate_upload_text(
                        variant_segments,
                        include_markers=True,
                        marker_style=args.marker_style,
                    )
                    if generate_inline_fallback
                    else ""
                )
                variant_path = candidate_dir / f"anythingllm-upload-{name}.txt"
                variant_fallback_path = candidate_dir / f"anythingllm-upload-{name}-inline-metadata-fallback.txt"
                variant_manifest = candidate_dir / f"segment-manifest-{name}.jsonl"
                variant_ratio = candidate_dir / f"metadata-ratio-{name}.csv"
                variant_path.write_text(variant_text, encoding="utf-8")
                if generate_inline_fallback:
                    variant_fallback_path.write_text(variant_fallback_text, encoding="utf-8")
                append_jsonl(variant_manifest, variant_segments)
                write_csv(
                    variant_ratio,
                    [
                        marker_ratio_stats(
                            variant_segments,
                            marker_style=args.marker_style,
                            include_markers=generate_inline_fallback,
                        )
                    ],
                )
                variant_outputs[name] = {
                    "upload_file": str(variant_path),
                    "fallback_upload_file": str(variant_fallback_path) if generate_inline_fallback else "",
                    "manifest": str(variant_manifest),
                    "metadata_ratio": str(variant_ratio),
                    "start_page": variant_start,
                    "end_page": variant_end,
                    "segments": len(variant_segments),
                    "content_chars": sum(len(row.get("text", "")) for row in variant_segments),
                    "content_words": sum(
                        len(re.findall(r"\b[\w\u2019'-]+\b", row.get("text", ""), flags=re.UNICODE))
                        for row in variant_segments
                    ),
                }

            if not getattr(args, "include_front_matter", False):
                front_start, _ = detect_body_start(
                    pages,
                    stats,
                    outline=usable_outline,
                    include_front_matter=True,
                )
                if front_start < start_page:
                    write_upload_variant("frontmatter-and-body", front_start, end_page)
                    write_upload_variant("full-document", front_start, None)
            if end_page:
                write_upload_variant("body-with-endmatter", start_page, None)

            candidate = {
                "backend": backend,
                "pymupdf4llm_execution": pymupdf4llm_execution,
                "unstructured_execution": unstructured_execution,
                "unstructured_strategy": active_unstructured["resolved"] if backend == "unstructured" else "",
                "unstructured_strategy_reason": active_unstructured["reason"] if backend == "unstructured" else "",
                "page_count": page_count,
                "page_stats": [asdict(s) for s in stats],
                "layout_evidence": layout_evidence,
                "lane_review": lane_review,
                "start_page": start_page,
                "start_reason": start_reason,
                "end_page": end_page,
                "detected_end_page": detected_end_page,
                "boundary_reference_backend": boundary_reference_backend,
                "boundary_reference_reliable": bool(shared_boundary_reference.get("reliable")),
                "independent_start_page": candidate_start_page,
                "independent_detected_end_page": candidate_detected_end_page,
                "independent_end_page": candidate_end_page,
                "boundary_reconciled": boundary_reconciled,
                "include_back_matter": include_back_matter,
                "end_heading": end_detected["heading"] if end_detected else "",
                "end_source": end_detected.get("source", "text_heuristic") if end_detected else "",
                "segments": segments,
                "quality": quality,
                "chunk_eval": chunk_eval,
                "native_chunk_eval": native_chunk_eval,
                "marker_stats": marker_stats,
                "outline_validation": {
                    "reliability": outline_validation["reliability"],
                    "pass_rate": outline_validation["pass_rate"],
                },
                "variant_outputs": variant_outputs,
                "literal_results": literal_results,
                "vector_results": [],
                "vector_status": "not_run",
                "vector_error_detail": "",
                "candidate_dir": str(candidate_dir),
                "error": "",
            }

            if args.run_vector_eval:
                vector_started = time.perf_counter()
                raw_max_vector_chunks = getattr(args, "max_vector_chunks", 300)
                max_vector_chunks = int(raw_max_vector_chunks if raw_max_vector_chunks not in (None, "") else 300)
                vector_eval_rows = select_vector_eval_rows(
                    native_retrieval_units,
                    probes[: args.max_vector_probes],
                    max_vector_chunks,
                )
                simulation_adapter = getattr(args, "simulation_adapter", None)
                if simulation_adapter is None:
                    simulation_adapter = build_ollama_simulation_adapter(args.ollama_model, args.ollama_url)
                vector_results, vector_status, vector_error_detail, vector_remote_usage = vector_eval(
                    vector_eval_rows,
                    probes[: args.max_vector_probes],
                    simulation_adapter,
                    max_segments=len(vector_eval_rows),
                    progress_callback=lambda done, total, stage: report_upload_phase(
                        "candidate_evaluation",
                        stage,
                        completed_units=done,
                        total_units=total,
                        fallback_fraction=done / max(total, 1),
                        evidence_kind="evaluation_unit_completed",
                    ),
                )
                candidate["vector_results"] = vector_results
                candidate["vector_status"] = vector_status
                candidate["vector_error_detail"] = vector_error_detail
                candidate["vector_provider"] = simulation_adapter.get("provider", "")
                candidate["vector_model"] = simulation_adapter.get("model", "")
                candidate["vector_eval_seconds"] = round(time.perf_counter() - vector_started, 2)
                candidate["vector_remote_usage"] = vector_remote_usage
                candidate["vector_embedded_segments"] = len(
                    {row.get("segment_id") for row in vector_eval_rows}
                )
                candidate["vector_embedded_chunks"] = len(vector_eval_rows)
                candidate["vector_probe_count"] = len(probes[: args.max_vector_probes])
                candidate["vector_request_batches"] = math.ceil(
                    len(vector_eval_rows) / max(1, int(simulation_adapter.get("batch_size") or 4))
                ) if vector_eval_rows else 0
                candidate["vector_remote_requests"] = int(vector_remote_usage.get("requests") or 0)
                write_csv(candidate_dir / "vector-results.csv", vector_results)

            score, reasons = score_candidate(candidate)
            candidate["score"] = score
            candidate["score_reasons"] = reasons
            candidates.append(candidate)
        except Exception as exc:
            if (
                backend == "unstructured"
                and isinstance(unstructured_circuit, dict)
                and is_unstructured_runtime_failure(exc)
            ):
                unstructured_circuit.update({
                    "blocked": True,
                    "reason": f"{type(exc).__name__}: {exc}",
                    "blocked_at": datetime.now().isoformat(timespec="seconds"),
                })
            candidates.append(
                {
                    "backend": backend,
                    "score": -999,
                    "score_reasons": ["backend_failed"],
                    "error": str(exc),
                    "segments": [],
                    "quality": {},
                    "chunk_eval": {},
                    "native_chunk_eval": {},
                    "literal_results": [],
                    "vector_results": [],
                    "vector_error_detail": "",
                    "candidate_dir": str(candidate_dir),
                }
            )
        emit_pipeline_timing_event(
            args,
            f"extraction_backend:{backend}",
            elapsed_seconds=time.perf_counter() - backend_started,
            event="phase_completed",
            backend=backend,
            candidate_success=not bool(candidates[-1].get("error")),
        )
        if backend_mode == "automatic" and backend == "pymupdf4llm" and "unstructured" not in backend_names:
            evaluated = [candidate for candidate in candidates if not candidate.get("error")]
            completed = [candidate for candidate in evaluated if candidate.get("segments")]
            failed = [candidate for candidate in candidates if candidate.get("error")]
            if failed:
                auto_unstructured_reasons.append("one_default_backend_failed")
            if evaluated and not completed:
                auto_unstructured_reasons.append("default_backends_produced_no_usable_segments")
            word_counts = [
                int(candidate.get("quality", {}).get("included_words") or 0)
                for candidate in evaluated
                if int(candidate.get("quality", {}).get("included_words") or 0) > 0
            ]
            if len(word_counts) >= 2 and (max(word_counts) - min(word_counts)) / max(word_counts) > 0.35:
                if has_complete_native_text_candidate(
                    candidates,
                    profile.get("pdf_page_count"),
                    getattr(args, "ocr_preflight_hint", None),
                ):
                    auto_unstructured_suppressed_reasons.append(
                        "default_backends_disagree_but_complete_native_text_exists"
                    )
                else:
                    auto_unstructured_reasons.append("default_backends_disagree_on_text_coverage")
            if has_document_wide_ocr_evidence(evaluated):
                auto_unstructured_reasons.append("default_backends_show_low_text_or_image_heavy_pages")
            elif any(
                str((candidate.get("quality") or {}).get("scanned_likelihood") or "").casefold()
                == "possible"
                for candidate in evaluated
            ):
                # Keep sparse image/table pages in the diagnostics lane. They
                # are not evidence that a whole mostly-text PDF needs the
                # costly hi_res OCR backend.
                auto_unstructured_suppressed_reasons.append(
                    "sparse_or_mixed_pages_do_not_require_document_wide_ocr"
                )
            if any(
                candidate.get("outline_validation", {}).get("reliability") == "untrusted"
                for candidate in evaluated
            ):
                auto_unstructured_reasons.append("outline_and_extracted_headings_disagree")
            if auto_unstructured_reasons:
                backend_names.append("unstructured")

    viable = [c for c in candidates if c.get("segments")]
    if not viable:
        raise RuntimeError("No extraction backend produced usable segments.")
    selected = sorted(viable, key=lambda c: c["score"], reverse=True)[0]
    # This evidence is only available after the candidate has actually run.
    # Emit it before the upload phase so an outer progress UI can adjust the
    # remaining estimate without charging OCR to text-only PDFs in advance.
    ocr_evidence = ocr_assistance_evidence(selected, candidates, profile)
    selection_stage = f"Selected {selected['backend']} and writing output variants"
    if ocr_evidence["used"]:
        selection_stage += " — OCR-assisted extraction observed"
    report_upload_phase(
        "candidate_evaluation",
        selection_stage,
        completed_units=1,
        total_units=1,
        fallback_fraction=selection_progress,
        evidence_kind="selection_completed",
    )
    readiness_reasons = []
    exact_vector_fail = any(
        row.get("kind") in {"exact_phrase", "user_exact_phrase"} and row.get("status") == "fail"
        for row in selected.get("vector_results", [])
    )
    vector_status = str(selected.get("vector_status") or "not_run")
    literal_fail = any(
        row.get("status") == "fail"
        for row in selected.get("literal_results", [])
        if row.get("kind") in {"exact_phrase", "user_exact_phrase"}
    )
    quality = selected.get("quality", {})
    if exact_vector_fail:
        readiness_reasons.append("exact_vector_retrieval_failed")
    if vector_status.startswith("error_"):
        readiness_reasons.append(vector_status)
    elif vector_status.startswith("skipped_") and vector_status != "not_run":
        readiness_reasons.append(vector_status)
    if literal_fail:
        readiness_reasons.append("exact_literal_probe_failed")
    if quality.get("scanned_likelihood") == "high":
        readiness_reasons.append("ocr_or_text_layer_failure_likely")
    if quality.get("included_pages", 0) >= 10 and quality.get("average_words_per_page", 0) < 30:
        readiness_reasons.append("implausibly_low_text_coverage")
    if quality.get("replacement_chars", 0) > max(20, int(quality.get("included_chars", 0) * 0.005)):
        readiness_reasons.append("excessive_replacement_characters")
    if selected.get("native_chunk_eval", {}).get("status") != "pass":
        readiness_reasons.append("native_header_metadata_does_not_survive_chunk_simulation")
    # The column-first recovery keeps citations tied to the original physical
    # PDF page, but it cannot prove that a photographed book spread has no
    # cropped or obscured words at its fold.  Require a human visual check
    # before upload when the condition is sustained across a document.
    if int(layout_evidence.get("photographed_spread_page_count") or 0) >= 2:
        readiness_reasons.append("photographed_spread_requires_manual_review")

    # A UI preflight is deliberately only a three-page native sample. It must
    # not block an otherwise usable text PDF. Once the real extraction agrees
    # that coverage is inadequate, however, spell out the missing capability
    # and prevent an unreliable payload from entering AnythingLLM.
    ocr_preflight_hint = getattr(args, "ocr_preflight_hint", {}) or {}
    insufficient_native_text = (
        quality.get("scanned_likelihood") == "high"
        or (
            quality.get("included_pages", 0) >= 10
            and quality.get("average_words_per_page", 0) < 30
        )
    )
    if insufficient_native_text and not unstructured_runtime_probe:
        with measured_pipeline_phase(args, "unstructured_runtime_capability_probe"):
            unstructured_runtime_probe = dict(unstructured_runtime_status("hi_res"))
        if isinstance(shared_runtime_context, dict):
            shared_runtime_context["unstructured_runtime_probe"] = dict(
                unstructured_runtime_probe
            )
    ocr_runtime_available = bool(
        unstructured_runtime_probe.get("backend_available")
        and unstructured_runtime_probe.get("tesseract_available")
    )
    unstructured_candidate_errors = [
        str(candidate.get("error") or "")
        for candidate in candidates
        if str(candidate.get("backend") or "").casefold() == "unstructured"
        and candidate.get("error")
    ]
    # The cheap preflight is deliberately conservative and can miss a scan
    # whose three sampled pages happen to contain a cover or partial OCR text.
    # The full extraction quality result is decisive: do not upload a document
    # that demonstrably needs OCR merely because its early sample was unclear.
    if insufficient_native_text and not ocr_runtime_available:
        readiness_reasons.extend(["needs_unstructured_or_ocr", "ocr_runtime_unavailable"])
    elif insufficient_native_text and unstructured_candidate_errors:
        readiness_reasons.extend(["needs_unstructured_or_ocr", "ocr_attempt_failed"])

    viable_word_counts = [
        int(candidate.get("quality", {}).get("included_words") or 0)
        for candidate in viable
        if int(candidate.get("quality", {}).get("included_words") or 0) > 0
    ]
    backend_word_disagreement = 0.0
    backend_word_disagreement_resolution = {
        "accepted": False,
        "reason": "not_applicable",
        "checks": {},
        "materially_shorter_peers": [],
        "weak_shorter_peers": [],
    }
    if len(viable_word_counts) >= 2:
        backend_word_disagreement = (max(viable_word_counts) - min(viable_word_counts)) / max(viable_word_counts)
        if backend_word_disagreement > 0.35:
            backend_word_disagreement_resolution = explainable_ocr_coverage_disagreement(
                selected,
                candidates,
                profile,
                ocr_evidence,
            )
            if not backend_word_disagreement_resolution["accepted"]:
                readiness_reasons.append("backend_text_coverage_disagreement")

    selected["readiness_status"] = "needs_review" if readiness_reasons else "ready"
    selected["readiness_reasons"] = readiness_reasons
    selected["ocr_preflight_hint"] = ocr_preflight_hint
    selected["upload_blocked_reason"] = upload_block_reason_for_readiness(selected)
    selected["backend_word_disagreement"] = round(backend_word_disagreement, 4)
    selected["backend_word_disagreement_resolution"] = backend_word_disagreement_resolution
    selected["vector_validation_status"] = (
        selected.get("vector_status", "not_run")
        if selected.get("vector_status") != "not_run"
        else "not_run_extraction_only"
    )
    selected["fallback_marker_status"] = (
        "disabled"
        if selected["chunk_eval"].get("status") == "disabled"
        else ("pass" if selected["chunk_eval"].get("chunks_without_marker", 0) == 0 else "warning")
    )

    selected_dir = out_root / "selected"
    selected_dir.mkdir(parents=True, exist_ok=True)
    src_candidate_dir = Path(selected["candidate_dir"])
    shutil.copy2(src_candidate_dir / "anythingllm-upload.txt", selected_dir / "anythingllm-upload.txt")
    prepared_text_path = selected_dir / parsed_pdf_text_filename(pdf_path)
    shutil.copy2(selected_dir / "anythingllm-upload.txt", prepared_text_path)
    candidate_fallback = src_candidate_dir / "anythingllm-upload-inline-metadata-fallback.txt"
    if candidate_fallback.exists():
        shutil.copy2(
            candidate_fallback,
            selected_dir / "anythingllm-upload-inline-metadata-fallback.txt",
        )
    shutil.copy2(src_candidate_dir / "segment-manifest.jsonl", selected_dir / "segment-manifest.jsonl")
    transition_rows = []
    page_text_by_number = {}
    for segment in selected["segments"]:
        page_text_by_number.setdefault(int(segment["pdf_page"]), []).append(segment)
    sorted_pages = sorted(page_text_by_number)
    companion_dir = selected_dir / "page-transition-companions"
    for left_page, right_page in zip(sorted_pages, sorted_pages[1:]):
        if right_page != left_page + 1:
            continue
        left_segments = sorted(page_text_by_number[left_page], key=lambda row: row["char_start_page"])
        right_segments = sorted(page_text_by_number[right_page], key=lambda row: row["char_start_page"])
        transition = detect_page_transition(
            " ".join(row["text"] for row in left_segments),
            " ".join(row["text"] for row in right_segments),
            left_page,
            right_page,
            source_meta.get("source_short_label") or "document",
        )
        transition["left_source_segment_ids"] = [row["segment_id"] for row in left_segments[-2:]]
        transition["right_source_segment_ids"] = [row["segment_id"] for row in right_segments[:2]]
        if transition["continuation_detected"]:
            companion_dir.mkdir(parents=True, exist_ok=True)
            companion_path = companion_dir / f"{transition['boundary_id']}.txt"
            companion_path.write_text(transition["reconstructed_text"], encoding="utf-8")
            transition["artifact_path"] = str(companion_path)
            transition["materialization_status"] = "local_artifact"
        else:
            transition["artifact_path"] = ""
            transition["materialization_status"] = "manifest_only"
        transition_rows.append(transition)
    append_jsonl(selected_dir / "page-transition-manifest.jsonl", transition_rows)
    layout_review = src_candidate_dir / "layout-region-review.json"
    if layout_review.exists():
        shutil.copy2(layout_review, selected_dir / "layout-region-review.json")
    for filename in ("retrieval-lane-review.json", "supplementary-content-candidates.txt"):
        source_path = src_candidate_dir / filename
        if source_path.exists():
            shutil.copy2(source_path, selected_dir / filename)
    shutil.copy2(src_candidate_dir / "extraction-report.csv", selected_dir / "extraction-report.csv")
    shutil.copy2(src_candidate_dir / "outline-validation.csv", selected_dir / "outline-validation.csv")
    shutil.copy2(src_candidate_dir / "metadata-ratio.csv", selected_dir / "metadata-ratio.csv")
    shutil.copy2(
        src_candidate_dir / "native-header-chunk-audit.csv",
        selected_dir / "native-header-chunk-audit.csv",
    )
    page_parent_rows = build_page_parent_rows(selected["segments"])
    child_parent_rows = build_child_parent_map(selected["segments"], page_parent_rows)
    append_jsonl(selected_dir / "page-parent-manifest.jsonl", page_parent_rows)
    write_csv(selected_dir / "child-parent-map.csv", child_parent_rows)
    provenance_review_manifest = write_provenance_review_manifest(
        selected_dir,
        source_meta,
        profile,
        selected,
        ocr_evidence,
        page_parent_rows,
        transition_rows,
    )
    comparison_rows = representation_comparison_rows(
        selected["segments"],
        page_parent_rows,
        chunk_size,
        chunk_overlap,
        profile["anythingllm_embedding_config"],
    )
    write_csv(selected_dir / "representation-comparison.csv", comparison_rows)
    write_json(selected_dir / "representation-comparison.json", comparison_rows)
    harmonization_report_rows = harmonization_rows(
        selected["segments"],
        page_parent_rows,
        chunk_size,
        chunk_overlap,
        profile["anythingllm_embedding_config"],
    )
    write_csv(selected_dir / "harmonization-report.csv", harmonization_report_rows)
    write_json(selected_dir / "harmonization-report.json", harmonization_report_rows)
    recommendation_rows = representation_recommendation_rows(harmonization_report_rows)
    write_csv(selected_dir / "representation-recommendation.csv", recommendation_rows)
    write_json(selected_dir / "representation-recommendation.json", recommendation_rows)
    selected_variants = {}
    for variant_name, variant in (selected.get("variant_outputs") or {}).items():
        copied = {}
        for key, filename in (
            ("upload_file", f"anythingllm-upload-{variant_name}.txt"),
            ("manifest", f"segment-manifest-{variant_name}.jsonl"),
            ("metadata_ratio", f"metadata-ratio-{variant_name}.csv"),
        ):
            source_path = Path(variant[key])
            target_path = selected_dir / filename
            if source_path.exists():
                shutil.copy2(source_path, target_path)
                copied[key] = str(target_path)
        if variant.get("fallback_upload_file"):
            source_path = Path(variant["fallback_upload_file"])
            if source_path.exists():
                target_path = selected_dir / f"anythingllm-upload-{variant_name}-inline-metadata-fallback.txt"
                shutil.copy2(source_path, target_path)
                copied["fallback_upload_file"] = str(target_path)
        selected_variants[variant_name] = {**variant, **copied}

    variant_labels = {
        "recommended-body": "Recommended body only",
        "frontmatter-and-body": "Front matter and body",
        "body-with-endmatter": "Body with end matter",
        "full-document": "Full document",
    }
    variant_rows = [
        {
            "variant": "recommended-body",
            "display_name": variant_labels["recommended-body"],
            "start_pdf_page": selected.get("start_page"),
            "end_before_pdf_page": selected.get("end_page") or "",
            "segments": len(selected.get("segments", [])),
            "content_chars": sum(len(row.get("text", "")) for row in selected.get("segments", [])),
            "content_words": selected.get("quality", {}).get("included_words", 0),
            "clean_file": str(prepared_text_path),
            "clean_file_bytes": prepared_text_path.stat().st_size,
            "inline_fallback_file": (
                str(selected_dir / "anythingllm-upload-inline-metadata-fallback.txt")
                if (selected_dir / "anythingllm-upload-inline-metadata-fallback.txt").exists()
                else ""
            ),
            "inline_fallback_bytes": (
                (selected_dir / "anythingllm-upload-inline-metadata-fallback.txt").stat().st_size
                if (selected_dir / "anythingllm-upload-inline-metadata-fallback.txt").exists()
                else 0
            ),
        }
    ]
    for variant_name, variant in selected_variants.items():
        clean_path = Path(variant["upload_file"])
        fallback_path = Path(variant["fallback_upload_file"]) if variant.get("fallback_upload_file") else None
        variant_rows.append(
            {
                "variant": variant_name,
                "display_name": variant_labels.get(variant_name, variant_name),
                "start_pdf_page": variant.get("start_page"),
                "end_before_pdf_page": variant.get("end_page") or "",
                "segments": variant.get("segments"),
                "content_chars": variant.get("content_chars"),
                "content_words": variant.get("content_words"),
                "clean_file": str(clean_path),
                "clean_file_bytes": clean_path.stat().st_size,
                "inline_fallback_file": str(fallback_path) if fallback_path else "",
                "inline_fallback_bytes": fallback_path.stat().st_size if fallback_path and fallback_path.exists() else 0,
            }
        )
    write_csv(selected_dir / "output-variant-summary.csv", variant_rows)

    metadata_artifact_started = time.perf_counter()
    metadata_dir = out_root / "metadata-api"
    lean_retention = bool(getattr(args, "lean_retention", False))
    requested_upload_transport = getattr(args, "native_upload_transport", "raw_text")
    materialize_metadata_artifacts = not lean_retention or requested_upload_transport == "file_upload"
    report_upload_phase(
        "payloads",
        "Preparing native upload payloads" if lean_retention else "Generating native metadata payloads and test kit",
        completed_units=0,
        total_units=1,
        fallback_fraction=metadata_progress,
        evidence_kind="phase_started",
    )
    strict_payloads = generate_api_payloads(selected["segments"], "strict")
    native_payloads = generate_api_payloads(selected["segments"], "native_header")
    page_parent_strict_payloads = generate_page_parent_payloads(page_parent_rows, "strict")
    page_parent_native_payloads = generate_page_parent_payloads(page_parent_rows, "native_header")
    if materialize_metadata_artifacts:
        segment_strict_upload_rows = build_file_upload_rows_from_payloads(
            strict_payloads, metadata_dir / "file-upload-segments-strict"
        )
        segment_native_upload_rows = build_file_upload_rows_from_payloads(
            native_payloads, metadata_dir / "file-upload-segments-native-header"
        )
        page_parent_strict_upload_rows = build_file_upload_rows_from_payloads(
            page_parent_strict_payloads, metadata_dir / "file-upload-page-parents-strict"
        )
        page_parent_native_upload_rows = build_file_upload_rows_from_payloads(
            page_parent_native_payloads, metadata_dir / "file-upload-page-parents-native-header"
        )
        append_jsonl(metadata_dir / "raw-text-payloads-strict.jsonl", strict_payloads)
        append_jsonl(metadata_dir / "raw-text-payloads-native-header.jsonl", native_payloads)
        append_jsonl(metadata_dir / "raw-text-payloads-page-parents-strict.jsonl", page_parent_strict_payloads)
        append_jsonl(metadata_dir / "raw-text-payloads-page-parents-native-header.jsonl", page_parent_native_payloads)
        write_csv(metadata_dir / "file-upload-plan-segments-strict.csv", segment_strict_upload_rows)
        write_csv(metadata_dir / "file-upload-plan-segments-native-header.csv", segment_native_upload_rows)
        write_csv(metadata_dir / "file-upload-plan-page-parents-strict.csv", page_parent_strict_upload_rows)
        write_csv(metadata_dir / "file-upload-plan-page-parents-native-header.csv", page_parent_native_upload_rows)
        write_csv(metadata_dir / "upload-plan.csv", segment_native_upload_rows)
        write_csv(metadata_dir / "page-parent-upload-plan.csv", page_parent_native_upload_rows)
    else:
        segment_strict_upload_rows = []
        segment_native_upload_rows = []
        page_parent_strict_upload_rows = []
        page_parent_native_upload_rows = []
    selected_workspace_slug = getattr(args, "workspace_slug", "")
    target_workspace_slug = (
        selected_workspace_slug
        if getattr(args, "prepare_and_upload", False) and selected_workspace_slug
        else getattr(args, "test_workspace_slug", "") or selected_workspace_slug or "test"
    )
    if materialize_metadata_artifacts:
        native_test_kit = write_native_metadata_test_kit(
            selected["segments"], out_root / "native-metadata-test-kit", workspace_slug=target_workspace_slug
        )
        compatibility_probe_segments = (
            [selected["segments"][0], selected["segments"][len(selected["segments"]) // 2]]
            if len(selected["segments"]) > 2 else selected["segments"]
        )
        native_probe_kit = write_native_metadata_test_kit(
            compatibility_probe_segments, out_root / "native-metadata-compatibility-probe",
            workspace_slug=target_workspace_slug, artifact_prefix="compatibility",
        )
    else:
        native_test_kit = {"files_dir": "", "upload_plan": "", "checklist": "", "file_count": 0}
        native_probe_kit = {"files_dir": "", "upload_plan": "", "checklist": "", "file_count": 0}
    edge_case_report = evaluate_edge_cases(profile, selected, selected_dir, native_payloads)
    write_csv(out_root / "edge-case-results.csv", edge_case_report["rows"])
    (out_root / "edge-case-report.html").write_text(build_edge_case_html(edge_case_report), encoding="utf-8")
    write_json(out_root / "edge-case-summary.json", {k: v for k, v in edge_case_report.items() if k != "rows"})
    emit_pipeline_timing_event(
        args,
        "payload_packaging_and_diagnostics",
        elapsed_seconds=time.perf_counter() - metadata_artifact_started,
        event="phase_completed",
        lean_retention=lean_retention,
        metadata_artifacts_materialized=materialize_metadata_artifacts,
    )

    inspection_dir = out_root / "inspection"
    has_shared_batch_inspection = isinstance(getattr(args, "batch_inspection_context", None), dict)
    inspection_context, inspection_reused = get_batch_inspection_context(
        args, storage_dir, target_workspace_slug
    )
    inspection_context.setdefault("inspection_dirs", []).append(str(inspection_dir))
    global_reads = inspection_context.setdefault("global_read_only", {})
    report_upload_phase(
        "payloads",
        (
            "Reusing batch-global AnythingLLM configuration inspection"
            if inspection_reused else
            "Inspecting AnythingLLM configuration and storage read-only"
        ),
        completed_units=1,
        total_units=1,
        fallback_fraction=storage_progress,
        evidence_kind="payloads_ready",
    )
    if not inspection_reused:
        # These reads are batch-global: author regression samples, Desktop
        # configuration/schema, and the pre-upload workspace baseline do not
        # change merely because the next PDF is prepared.  Caching them here
        # removes the former N-times full storage scan without caching mutable
        # post-upload document/vector evidence.
        run_author_sample_evaluation = bool(
            getattr(args, "run_author_inference_sample_evaluation", False)
        )
        if run_author_sample_evaluation:
            with measured_pipeline_phase(args, "author_inference_sample_evaluation"):
                global_reads["author_eval"] = evaluate_author_inference_samples(
                    inspection_dir,
                    sample_pdf_dir=application_paths()["root"] / "diagnostic-samples" / "author-inference-samples",
                )
        else:
            # This suite evaluates unrelated regression PDFs. It never affects
            # the selected document's author inference or any native upload
            # decision, so running it during ordinary preparation adds avoidable
            # I/O and sometimes network work. Advanced/explicit callers can
            # still request it and receive the original durable artifacts.
            global_reads["author_eval"] = {
                "status": "skipped_not_requested",
                "csv": "",
                "json": "",
                "rows": [],
                "passed": 0,
                "failed": 0,
            }
        inspection_steps = [
            ("Reading local AnythingLLM storage baseline", "storage_report", lambda: inspect_anythingllm_storage(storage_dir)),
            ("Reading workspace model configuration", "workspace_gate", lambda: read_workspace_model_gate(storage_dir, target_workspace_slug)),
            ("Reading AnythingLLM metadata schema", "metadata_schema_report", lambda: get_anythingllm_metadata_schema(
                (args.anythingllm_api_url or "").strip(), args.anythingllm_api_key,
            )),
            ("Reading workspace storage layout", "workspace_layer_report", lambda: workspace_storage_inspector(
                storage_dir, target_workspace_slug
            )),
        ]
        with measured_pipeline_phase(args, "anythingllm_batch_read_only_inspection"):
            for step_index, (label, key, inspector) in enumerate(inspection_steps, start=1):
                report_upload_phase(
                    "payloads",
                    f"{label} ({step_index}/{len(inspection_steps)})",
                    completed_units=step_index - 1,
                    total_units=len(inspection_steps),
                    fallback_fraction=0.0,
                    evidence_kind="read_only_inspection_started",
                )
                global_reads[key] = inspector()
                report_upload_phase(
                    "payloads",
                    f"{label} complete ({step_index}/{len(inspection_steps)})",
                    completed_units=step_index,
                    total_units=len(inspection_steps),
                    fallback_fraction=0.0,
                    evidence_kind="read_only_inspection_completed",
                )
    author_eval = dict(global_reads.get("author_eval") or {})
    storage_report = dict(global_reads.get("storage_report") or {"status": "not_available"})
    workspace_gate = dict(global_reads.get("workspace_gate") or {"status": "not_available"})
    metadata_schema_report = dict(global_reads.get("metadata_schema_report") or {"status": "not_available"})
    workspace_layer_report = dict(global_reads.get("workspace_layer_report") or {"status": "not_available"})
    metadata_schema = metadata_schema_report.get("schema")
    metadata_schema_keys = metadata_schema.keys() if isinstance(metadata_schema, dict) else ()
    if inspection_reused and author_eval.get("status") != "skipped_not_requested":
        # Each document retains a readable audit package while avoiding a
        # second sample-suite download/extraction. Paths in this copy point to
        # the first package, which is recorded explicitly below.
        write_json(inspection_dir / "author-inference-evaluation.json", author_eval.get("rows") or [])
        write_csv(inspection_dir / "author-inference-evaluation.csv", author_eval.get("rows") or [])
        author_eval["reused_from_batch"] = True
        author_eval["source_package"] = str((inspection_context.get("inspection_dirs") or [""])[0])
    write_json(inspection_dir / "lancedb-before.json", storage_report)
    write_json(inspection_dir / "workspace-model-gate.json", workspace_gate)
    write_csv(
        inspection_dir / "workspace-model-gate.csv",
        [
            {
                "status": workspace_gate.get("status"),
                "workspace_slug": workspace_gate.get("workspace_slug"),
                "workspace_name": workspace_gate.get("workspace_name"),
                "chat_provider": workspace_gate.get("chat_provider"),
                "chat_model": workspace_gate.get("chat_model"),
                "deepseek_like": workspace_gate.get("deepseek_like"),
                "blocked_terms_present": workspace_gate.get("blocked_terms_present"),
                "message": workspace_gate.get("message"),
            }
        ],
    )
    write_json(inspection_dir / "anythingllm-metadata-schema.json", metadata_schema_report)
    write_csv(
        inspection_dir / "metadata-compatibility-report.csv",
        [
            {
                "check": "storage_inspection",
                "status": storage_report.get("status"),
                "details": storage_report.get("error") or "Read-only inspection only; no direct DB writes attempted.",
            },
            {
                "check": "metadata_schema",
                "status": metadata_schema_report.get("status"),
                "details": metadata_schema_report.get("error") or ", ".join(metadata_schema_keys),
            },
            {
                "check": "metadata_payloads_generated",
                "status": "pass",
                "details": f"{len(native_payloads)} payloads generated for API experiment.",
            },
            {
                "check": "published_metadata_semantics",
                "status": "pass" if source_meta.get("source_published_epoch_ms") is not None else "warning",
                "details": (
                    "PDF creation date was converted to an epoch timestamp for AnythingLLM."
                    if source_meta.get("source_published_epoch_ms") is not None
                    else "No parseable PDF creation date exists. The payload omits published, but AnythingLLM's raw-text processor may substitute the ingestion date and prepend it to native chunk headers."
                ),
            },
            {
                "check": "arbitrary_metadata_fields",
                "status": "unsupported_by_source_contract",
                "details": "AnythingLLM raw-text processing only preserves url, title, docAuthor, description, docSource, chunkSource, and published. Page/chapter/segment data is therefore encoded into supported title and description fields.",
            },
        ],
    )
    metadata_layer_rows = metadata_layer_visibility_rows(
        native_payloads,
        page_parent_native_payloads,
        metadata_schema_report,
        native_metadata_report={"metadata_fields_seen": []},
    )
    write_csv(inspection_dir / "metadata-layer-visibility.csv", metadata_layer_rows)
    write_json(inspection_dir / "metadata-layer-visibility.json", metadata_layer_rows)
    column_explanation_rows = explain_observed_columns(workspace_layer_report)
    write_csv(inspection_dir / "column-explanations.csv", column_explanation_rows)
    write_json(inspection_dir / "column-explanations.json", column_explanation_rows)

    upload_report = {"status": "skipped_prepare_only", "uploaded": 0, "errors": []}
    embedding_batch_ledger_path = inspection_dir / "embedding-batch-ledger.json"
    submission_receipt_path = inspection_dir / "submission-receipts.jsonl"

    def report_upload_status(stage, batch_report=None):
        # This reaches the Gradio progress channel through the existing typed
        # preparation callback. It is stage evidence, never a completion claim.
        batch_report = batch_report or {}
        timing_event = str(batch_report.get("timing_event") or "status")
        if timing_event == "attachment_progress":
            phase = "attachments"
            completed = int(batch_report.get("attachments_completed") or 0)
            total = int(batch_report.get("attachments_total") or 0)
        elif timing_event in {"queue_progress", "desktop_queue_completed"}:
            phase = "desktop_queue"
            completed = int(batch_report.get("desktop_events_observed") or 0)
            total = int(batch_report.get("queue_records") or 0)
        elif timing_event in {"submission_started", "submission_completed", "verification_started"}:
            # Receipt/acceptance is deliberately distinct from the long
            # Desktop-owned queue. It reaches its end only when the HTTP
            # receipt is known; an unresolved receipt remains visible without
            # falsely implying that the queued page parents are complete.
            phase = "queue_receipt"
            completed = 1 if timing_event == "submission_completed" else 0
            total = 1
        else:
            # Request acceptance is useful state, but it is not vector
            # evidence. Keep it at the current queue checkpoint until either
            # Desktop emits per-file events or the exact observer sees rows.
            phase = "queue_receipt"
            completed = int(batch_report.get("desktop_events_observed") or 0)
            total = max(1, int(batch_report.get("queue_records") or batch_report.get("requested") or 0))
        report_upload_phase(
            phase,
            stage,
            completed_units=completed,
            total_units=total,
            fallback_fraction=0.0,
            desktop_required=True,
            evidence_kind=str(batch_report.get("desktop_queue_event_type") or timing_event),
        )
        timing_callback = getattr(args, "timing_event_callback", None)
        if callable(timing_callback):
            try:
                timing_callback(stage, dict(batch_report))
            except Exception:
                # Native upload acceptance must not depend on an optional UI
                # observer or timing ledger remaining healthy.
                pass

    payloads_to_upload = []
    upload_representation = getattr(args, "native_upload_representation", "segments")
    requested_upload_transport = getattr(args, "native_upload_transport", "raw_text")
    # Keep legacy callers that do not yet provide this option compatible with
    # the Desktop 1.15 Documents drawer: it only enumerates direct children of
    # custom-documents, not title/hash subfolders.
    create_document_folders = bool(
        getattr(args, "anythingllm_create_document_folders", False)
    )
    selected["primary_provenance_strategy"] = getattr(
        args,
        "primary_provenance_strategy",
        "native_metadata",
    )
    selected["inline_fallback_required"] = bool(
        getattr(args, "inline_fallback_required", False)
    )
    validation_payloads = (
        page_parent_strict_payloads
        if upload_representation == "page_parents"
        and getattr(args, "native_metadata_upload_mode", "native_header") == "strict"
        else page_parent_native_payloads
        if upload_representation == "page_parents"
        else strict_payloads
        if getattr(args, "native_metadata_upload_mode", "native_header") == "strict"
        else native_payloads
    )
    upload_plan_rows = (
        page_parent_strict_upload_rows
        if upload_representation == "page_parents"
        and getattr(args, "native_metadata_upload_mode", "native_header") == "strict"
        else page_parent_native_upload_rows
        if upload_representation == "page_parents"
        else segment_strict_upload_rows
        if getattr(args, "native_metadata_upload_mode", "native_header") == "strict"
        else segment_native_upload_rows
    )
    upload_transport = choose_native_upload_transport(
        args.anythingllm_api_url,
        requested_upload_transport,
        upload_plan_rows=upload_plan_rows,
        storage_dir=storage_dir,
    )
    upload_folder_name = managed_anythingllm_upload_folder_name(
        workspace_slug=target_workspace_slug,
        source_title=pdf_path.stem,
        source_sha=source_meta.get("source_sha256", ""),
        create_document_folders=create_document_folders,
        explicit_folder_name=getattr(args, "anythingllm_document_folder_name", ""),
    )
    upload_indices = tuple(getattr(args, "upload_indices", ()) or ())
    expected_upload_payloads = (
        upload_plan_rows_to_expected_payloads(select_upload_payloads(upload_plan_rows, args.upload_limit, upload_indices))
        if upload_transport == "file_upload" and upload_plan_rows
        else select_upload_payloads(payloads_to_upload, args.upload_limit, upload_indices)
    )
    vector_record_label = (
        "page-parent vectors"
        if upload_representation == "page_parents"
        else "segment vectors"
    )

    def verify_embedding_batch(batch_report):
        """Reconcile a batch through attachment first, then vector evidence.

        A late HTTP response is an unknown client outcome.  Full workspace-row
        inspection is deliberately used only on that exceptional path so an
        observed attachment prevents a duplicate submission while the later
        document-wide poll continues waiting for vectors.
        """
        start_index = int(batch_report.get("start_index") or 0)
        end_index = int(batch_report.get("end_index") or start_index)
        expected_batch = expected_upload_payloads[start_index:end_index]
        batch_number = int(batch_report.get("batch") or 0)
        if not expected_batch:
            return {
                "status": "error",
                "message": f"No expected payload identity was available for batch {batch_number}.",
            }

        unresolved_submission = str(batch_report.get("submission_state") or "") == "unresolved"
        reconciliation_started_at = float(
            batch_report.get("reconciliation_started_at_epoch") or time.time()
        )
        reconciliation_deadline = max(
            0.0,
            float(
                batch_report.get("reconciliation_deadline_seconds")
                or ANYTHINGLLM_EMBEDDING_RECONCILIATION_TIMEOUT_SECONDS
            ),
        )
        reconciliation_tracker = {
            "highest_observed": 0,
            "last_vector_progress_elapsed_seconds": None,
            "highest_queue_position": 0,
            "last_queue_progress_elapsed_seconds": None,
            "deadline_extensions": 0,
            "effective_deadline_seconds": reconciliation_deadline,
            "storage_busy_observed": False,
            # Fast observations are deliberately cheap enough to run on the
            # normal two-second cadence.  The two scheduled diagnostics below
            # are a *read-only* deeper look at the workspace/document state;
            # they never enqueue, restart Desktop, retry an upload, or alter
            # the recovery decision by themselves.
            "completed_read_only_checkpoints": set(),
            "read_only_checkpoints": [],
        }

        def live_desktop_queue_snapshot():
            # ``update_workspace_embeddings_desktop_queue`` supplies a
            # JSON-safe mutable SSE snapshot.  Non-Desktop callers simply
            # receive an empty context.
            raw = batch_report.get("desktop_queue_observer")
            if not isinstance(raw, dict):
                return {}
            last_event = float(raw.get("last_event_monotonic") or 0.0)
            return {
                "desktop_queue_completed": max(0, int(raw.get("completed") or 0)),
                "desktop_queue_current": max(0, int(raw.get("current") or 0)),
                "desktop_queue_events_observed": max(0, int(raw.get("events_observed") or 0)),
                "desktop_queue_last_event_type": str(raw.get("last_event_type") or ""),
                "desktop_queue_last_event_age_seconds": (
                    round(max(0.0, time.monotonic() - last_event), 3)
                    if last_event else None
                ),
                "queue_records": max(0, int(raw.get("queue_records") or len(expected_batch))),
                "desktop_queue_observer_state": str(raw.get("observer_state") or "unknown"),
                "desktop_queue_observer_failures": max(0, int(raw.get("observer_failures") or 0)),
                "desktop_queue_observer_reason": str(raw.get("observer_reason") or ""),
            }

        def reconcile_evidence(evidence):
            evidence = dict(evidence or {})
            observed = int(
                evidence.get("matching_vector_rows")
                or evidence.get("lancedb_matching_rows")
                or 0
            )
            elapsed = max(0.0, time.time() - reconciliation_started_at)
            if observed > int(reconciliation_tracker["highest_observed"]):
                reconciliation_tracker["highest_observed"] = observed
                reconciliation_tracker["last_vector_progress_elapsed_seconds"] = round(elapsed, 3)
            error_text = " ".join(
                str(evidence.get(key) or "") for key in ("error", "message", "classification", "status")
            ).casefold()
            if any(token in error_text for token in ("database is locked", "sqlite", "lock", "busy")):
                reconciliation_tracker["storage_busy_observed"] = True
            queue = live_desktop_queue_snapshot()
            queue_position = max(
                int(queue.get("desktop_queue_completed") or 0),
                int(queue.get("desktop_queue_current") or 0),
            )
            if queue_position > int(reconciliation_tracker["highest_queue_position"]):
                reconciliation_tracker["highest_queue_position"] = queue_position
                reconciliation_tracker["last_queue_progress_elapsed_seconds"] = round(elapsed, 3)
            heartbeat_age = queue.get("desktop_queue_last_event_age_seconds")
            heartbeat_live = (
                str(queue.get("desktop_queue_observer_state") or "") == "connected"
                and
                heartbeat_age is not None
                and float(heartbeat_age) <= max(15.0, float(getattr(args, "post_upload_poll_interval", 2.0)) * 3.0)
            )
            if observed >= len(expected_batch):
                classification = "reconciliation_exact_vectors_confirmed"
            elif observed > 0 and reconciliation_tracker["last_vector_progress_elapsed_seconds"] == round(elapsed, 3):
                classification = "reconciliation_vector_progressing"
            elif heartbeat_live:
                classification = "reconciliation_desktop_queue_heartbeating"
            elif reconciliation_tracker["storage_busy_observed"]:
                classification = "reconciliation_storage_busy"
            elif elapsed >= 60.0:
                classification = "reconciliation_no_new_evidence"
            else:
                classification = "reconciliation_waiting_for_evidence"
            evidence.update(queue)
            evidence.update({
                "reconciliation_classification": classification,
                "reconciliation_elapsed_seconds": round(elapsed, 3),
                "reconciliation_deadline_seconds": reconciliation_deadline,
                "reconciliation_effective_deadline_seconds": round(
                    float(reconciliation_tracker["effective_deadline_seconds"]), 3
                ),
                "reconciliation_remaining_seconds": round(
                    max(0.0, float(reconciliation_tracker["effective_deadline_seconds"]) - elapsed), 3
                ),
                "reconciliation_vector_progress_count": int(reconciliation_tracker["highest_observed"]),
                "reconciliation_queue_progress_count": int(reconciliation_tracker["highest_queue_position"]),
                "reconciliation_last_queue_progress_elapsed_seconds": reconciliation_tracker[
                    "last_queue_progress_elapsed_seconds"
                ],
                "reconciliation_deadline_extensions": int(reconciliation_tracker["deadline_extensions"]),
                "reconciliation_storage_busy_observed": bool(reconciliation_tracker["storage_busy_observed"]),
                "reconciliation_read_only_checkpoints": list(
                    reconciliation_tracker["read_only_checkpoints"]
                ),
            })
            return evidence

        def extend_reconciliation_deadline(evidence, poll_elapsed, current_deadline):
            """Extend only while this run's owned Desktop queue proves movement.

            The receipt POST is intentionally never replayed.  Once it has
            timed out, fresh SSE queue progress or exact-vector progress is
            the only basis for continued observation.  A quiet/reconnecting
            stream cannot prolong the run, and a hard cap prevents an endless
            wait if Desktop stops reporting entirely.
            """
            evidence = dict(evidence or {})
            elapsed = float(evidence.get("reconciliation_elapsed_seconds") or poll_elapsed or 0.0)
            if elapsed < reconciliation_deadline:
                return None
            queue_total = int(evidence.get("queue_records") or len(expected_batch))
            queue_position = max(
                int(evidence.get("desktop_queue_completed") or 0),
                int(evidence.get("desktop_queue_current") or 0),
            )
            last_queue_progress = reconciliation_tracker["last_queue_progress_elapsed_seconds"]
            last_vector_progress = reconciliation_tracker["last_vector_progress_elapsed_seconds"]
            recent_queue_progress = (
                last_queue_progress is not None
                and elapsed - float(last_queue_progress) <= ANYTHINGLLM_EMBEDDING_RECONCILIATION_STALL_SECONDS
            )
            recent_vector_progress = (
                last_vector_progress is not None
                and elapsed - float(last_vector_progress) <= ANYTHINGLLM_EMBEDDING_RECONCILIATION_STALL_SECONDS
            )
            owned_queue_active = (
                queue_total > 0
                and queue_position < queue_total
                and str(evidence.get("desktop_queue_observer_state") or "") == "connected"
                and recent_queue_progress
            )
            if not (owned_queue_active or recent_vector_progress):
                return None
            extended = min(
                ANYTHINGLLM_EMBEDDING_RECONCILIATION_ACTIVE_CAP_SECONDS,
                max(
                    float(current_deadline),
                    elapsed + ANYTHINGLLM_EMBEDDING_RECONCILIATION_PROGRESS_GRACE_SECONDS,
                ),
            )
            if extended > float(current_deadline):
                reconciliation_tracker["deadline_extensions"] += 1
                reconciliation_tracker["effective_deadline_seconds"] = extended
                evidence["reconciliation_deadline_extensions"] = int(reconciliation_tracker["deadline_extensions"])
                evidence["reconciliation_effective_deadline_seconds"] = round(extended, 3)
                return extended
            return None

        def report_batch_observation(evidence, operator_state):
            observed = int(evidence.get("matching_vector_rows") or evidence.get("lancedb_matching_rows") or 0)
            batch_coverage = min(1.0, observed / len(expected_batch)) if expected_batch else 0.0
            queue_current = int(evidence.get("desktop_queue_current") or 0)
            queue_completed = int(evidence.get("desktop_queue_completed") or 0)
            queue_total = int(evidence.get("queue_records") or len(expected_batch))
            reconciliation_elapsed = float(evidence.get("reconciliation_elapsed_seconds") or 0.0)
            reconciliation_deadline_seconds = float(
                evidence.get("reconciliation_effective_deadline_seconds")
                or evidence.get("reconciliation_deadline_seconds")
                or 0.0
            )
            queue_detail = (
                f"Desktop queue: embedding {queue_current}/{queue_total} page-parent files; "
                f"{queue_completed}/{queue_total} completed. "
                if queue_total and (queue_current or queue_completed) else ""
            )
            checkpoint_detail = ""
            checkpoints = evidence.get("reconciliation_read_only_checkpoints") or []
            if checkpoints:
                latest_checkpoint = checkpoints[-1]
                checkpoint_detail = (
                    f" Read-only inspection at {int(latest_checkpoint.get('scheduled_seconds') or 0)}s: "
                    f"{int(latest_checkpoint.get('observed_vectors') or 0)}/{len(expected_batch)} observed."
                )
            report_upload_phase(
                "identity_set",
                (
                    f"AnythingLLM reconciliation {reconciliation_elapsed:.0f}/{reconciliation_deadline_seconds:.0f}s: "
                    f"{queue_detail}"
                    f"{format_vector_observation(observed, len(expected_batch), operator_state, record_label=vector_record_label)}"
                    f"{checkpoint_detail}"
                ),
                completed_units=observed,
                total_units=len(expected_batch),
                fallback_fraction=batch_coverage,
                desktop_required=True,
                evidence_kind=(
                    "exact_vector_observation_completed"
                    if observed >= len(expected_batch)
                    else "exact_vector_observation"
                ),
            )

        # A 2xx response only proves that Desktop accepted the request.  The
        # live serialized probe showed valid vectors materializing after the
        # former 180-second window, so both accepted and interrupted requests
        # use the same bounded observation budget.  This preserves one active
        # request while slow provider work completes instead of converting it
        # into a needless resume requirement.
        observation_timeout = (
            max(0.0, reconciliation_deadline - max(0.0, time.time() - reconciliation_started_at))
            if unresolved_submission else ANYTHINGLLM_EMBEDDING_RECONCILIATION_TIMEOUT_SECONDS
        )

        def inspect_batch_vectors():
            # The frequent observer is intentionally fast even when the
            # request receipt was ambiguous.  Performing a full workspace and
            # frontend materialization every two seconds both slows Desktop
            # while it is writing and makes the poller itself a source of
            # contention.  The 60/120-second checkpoints below provide the
            # deeper, still read-only inspection requested for an unresolved
            # receipt.
            evidence = verify_anythingllm_post_upload(
                storage_dir,
                target_workspace_slug,
                source_sha,
                expected_batch,
                upload_locations=(batch_report.get("locations") or []),
                observation_mode="fast",
            )
            if unresolved_submission:
                elapsed = max(0.0, time.time() - reconciliation_started_at)
                for checkpoint_seconds in (60.0, 120.0):
                    if (
                        elapsed < checkpoint_seconds
                        or checkpoint_seconds in reconciliation_tracker["completed_read_only_checkpoints"]
                    ):
                        continue
                    # ``verify_anythingllm_post_upload`` only opens local
                    # SQLite/LanceDB/filesystem views in this path.  Do not
                    # pass a frontend endpoint here: inspecting at a recovery
                    # checkpoint must not trigger any application-side work.
                    checkpoint_evidence = verify_anythingllm_post_upload(
                        storage_dir,
                        target_workspace_slug,
                        source_sha,
                        expected_batch,
                        upload_locations=(batch_report.get("locations") or []),
                        observation_mode="identity",
                    )
                    checkpoint_observed = int(
                        checkpoint_evidence.get("matching_vector_rows")
                        or checkpoint_evidence.get("lancedb_matching_rows")
                        or 0
                    )
                    checkpoint = {
                        "scheduled_seconds": int(checkpoint_seconds),
                        "observed_at_elapsed_seconds": round(elapsed, 3),
                        "observed_vectors": checkpoint_observed,
                        "status": str(checkpoint_evidence.get("status") or "not_checked"),
                        "classification": str(checkpoint_evidence.get("classification") or ""),
                        "workspace_documents": int(checkpoint_evidence.get("matching_workspace_documents") or 0),
                        "identity_set_complete": checkpoint_evidence.get("identity_set_complete"),
                        "storage_changed_during_observation": bool(
                            checkpoint_evidence.get("storage_changed_during_observation")
                        ),
                    }
                    reconciliation_tracker["completed_read_only_checkpoints"].add(checkpoint_seconds)
                    reconciliation_tracker["read_only_checkpoints"].append(checkpoint)
                    # A deep inspection can see identities which became
                    # visible between the fast poll and this checkpoint.  It
                    # may improve positive evidence only; a review-only
                    # document-list result must never turn a fast exact-vector
                    # result into a failure.
                    fast_observed = int(
                        evidence.get("matching_vector_rows")
                        or evidence.get("lancedb_matching_rows")
                        or 0
                    )
                    if checkpoint_observed > fast_observed:
                        for key in (
                            "matching_vector_rows",
                            "lancedb_matching_rows",
                            "identity_set_checked",
                            "identity_set_complete",
                            "expected_chunk_source_count",
                            "observed_chunk_source_count",
                            "duplicate_chunk_source_count",
                            "missing_chunk_sources",
                            "observed_chunk_sources",
                            "lancedb_matching_tables",
                            "lancedb_text_contains_page_or_segment",
                        ):
                            evidence[key] = checkpoint_evidence.get(key)
            observed = int(
                evidence.get("matching_vector_rows")
                or evidence.get("lancedb_matching_rows")
                or 0
            )
            # A reviewable storage read (for example a transient SQLite lock)
            # never proves this batch searchable. Keep polling the explicit
            # retryable partial state until every exact vector is observable
            # or the bounded reconciliation deadline is reached.
            if (
                observed < len(expected_batch)
                and str(evidence.get("status") or "") in REVIEWABLE_POST_UPLOAD_STATUSES
            ):
                evidence = dict(evidence)
                evidence["status"] = "partial_vector_coverage"
                evidence["classification"] = "exact_batch_vectors_not_fully_observed"
                evidence["message"] = (
                    f"Only {observed}/{len(expected_batch)} exact vectors are observable; "
                    "AnythingLLM may still be indexing, so reconciliation continues."
                )
            return reconcile_evidence(evidence)

        polling = poll_post_upload(
            inspect_batch_vectors,
            interval_seconds=float(getattr(args, "post_upload_poll_interval", 2.0)),
            # A 120-second client timeout can leave Desktop still embedding a
            # four-record batch. The old 45-second observer window then
            # misclassified recoverable partial coverage and stopped later
            # batches. Successful observations still return immediately; the
            # longer cap is paid only by the exceptional timeout path.
            timeout_seconds=observation_timeout,
            hard_cap_seconds=ANYTHINGLLM_EMBEDDING_RECONCILIATION_ACTIVE_CAP_SECONDS,
            observation_callback=report_batch_observation,
            retryable_evidence_codes={"partial_vector_coverage"},
            deadline_extension=extend_reconciliation_deadline,
        )
        evidence = dict(polling.final_evidence)
        evidence.update(
            {
                "status": polling.status,
                "polling_attempts": polling.attempts,
                "polling_elapsed_seconds": polling.elapsed_seconds,
                "polling_observer_failures": polling.observer_failures,
            }
        )
        if polling.status == "timeout":
            cap_classification = (
                "reconciliation_cap_partial_vector_progress"
                if int(reconciliation_tracker["highest_observed"]) > 0
                else "reconciliation_cap_queue_heartbeat"
                if int(evidence.get("desktop_queue_events_observed") or 0) > 0
                and float(evidence.get("desktop_queue_last_event_age_seconds") or 10**9) <= 30.0
                else "reconciliation_cap_storage_busy"
                if reconciliation_tracker["storage_busy_observed"]
                else "reconciliation_cap_no_new_evidence"
            )
            evidence["reconciliation_cap_classification"] = cap_classification
            evidence["reconciliation_outcome"] = "bounded_window_exhausted"
            evidence["message"] = (
                f"AnythingLLM reconciliation reached its {float(evidence.get('reconciliation_effective_deadline_seconds') or reconciliation_deadline):.0f}-second "
                "evidence-backed observation cap: "
                f"{int(evidence.get('matching_vector_rows') or evidence.get('lancedb_matching_rows') or 0)}/"
                f"{len(expected_batch)} exact page-parent vectors were observed ({cap_classification})."
            )
            # The final cap classification is itself meaningful UI evidence;
            # do not make the user infer a 480-second timeout from a stale
            # earlier count.
            report_batch_observation(evidence, "incomplete")
            # One exact scalar identity-set observation maps every confirmed
            # page-parent at once.  The former loop opened LanceDB once per
            # record at the worst possible moment: while Desktop was already
            # late and potentially still writing.  This preserves complete
            # provenance evidence without N repeated storage reads.
            identity_evidence = verify_anythingllm_post_upload(
                storage_dir,
                target_workspace_slug,
                source_sha,
                expected_batch,
                upload_locations=(batch_report.get("locations") or []),
                observation_mode="identity",
            )
            identity_observed = {
                str(value).strip()
                for value in (identity_evidence.get("observed_chunk_sources") or [])
                if str(value).strip()
            }
            expected_sources = [
                str((payload.get("metadata", {}) or {}).get("chunkSource") or f"batch-{batch_number}-record-{offset + 1}")
                for offset, payload in enumerate(expected_batch)
            ]
            confirmed_chunk_sources = [source for source in expected_sources if source in identity_observed]
            unresolved_chunk_sources = [source for source in expected_sources if source not in identity_observed]
            evidence["final_identity_set_observation"] = {
                key: identity_evidence.get(key)
                for key in (
                    "status",
                    "classification",
                    "identity_set_checked",
                    "identity_set_complete",
                    "expected_chunk_source_count",
                    "observed_chunk_source_count",
                    "duplicate_chunk_source_count",
                    "missing_chunk_sources",
                )
            }
            batch_locations = list(batch_report.get("locations") or [])
            evidence["confirmed_chunk_sources"] = confirmed_chunk_sources
            evidence["unresolved_chunk_sources"] = unresolved_chunk_sources
            evidence["confirmed_locations"] = [
                batch_locations[index]
                for index, payload in enumerate(expected_batch)
                if str((payload.get("metadata", {}) or {}).get("chunkSource") or f"batch-{batch_number}-record-{index + 1}")
                in set(confirmed_chunk_sources)
                and index < len(batch_locations)
            ]
            evidence["unresolved_locations"] = [
                batch_locations[index]
                for index, payload in enumerate(expected_batch)
                if str((payload.get("metadata", {}) or {}).get("chunkSource") or f"batch-{batch_number}-record-{index + 1}")
                in set(unresolved_chunk_sources)
                and index < len(batch_locations)
            ]
            evidence["confirmed_record_count"] = len(confirmed_chunk_sources)
            evidence["unresolved_record_count"] = len(unresolved_chunk_sources)
            if (
                unresolved_submission
                and int(evidence.get("matching_workspace_documents") or 0) >= len(expected_batch)
            ):
                evidence["status"] = "workspace_attached_pending_vectors"
                evidence["classification"] = "workspace_attachment_observed_before_vector_materialization"
                evidence["message"] = (
                    "AnythingLLM attached the submitted document(s) to the workspace, but their exact vectors "
                    "were not yet observable. Continuing with the document-wide reconciliation window."
                )
        return evidence

    def inspect_embedding_batch_once(batch_report):
        """Non-blocking idempotency check immediately before submission."""
        start_index = int(batch_report.get("start_index") or 0)
        end_index = int(batch_report.get("end_index") or start_index)
        expected_batch = expected_upload_payloads[start_index:end_index]
        if not expected_batch:
            return {"status": "identity_unavailable", "matching_vector_rows": 0}
        return verify_anythingllm_post_upload(
            storage_dir,
            target_workspace_slug,
            source_sha,
            expected_batch,
            upload_locations=(batch_report.get("locations") or []),
            observation_mode="fast",
        )

    if args.prepare_and_upload:
        payloads_to_upload = (
            page_parent_strict_payloads
            if upload_representation == "page_parents"
            and getattr(args, "native_metadata_upload_mode", "native_header") == "strict"
            else page_parent_native_payloads
            if upload_representation == "page_parents"
            else strict_payloads
            if getattr(args, "native_metadata_upload_mode", "native_header") == "strict"
            else native_payloads
        )
        expected_upload_payloads = (
            upload_plan_rows_to_expected_payloads(select_upload_payloads(upload_plan_rows, args.upload_limit, upload_indices))
            if upload_transport == "file_upload" and upload_plan_rows
            else select_upload_payloads(payloads_to_upload, args.upload_limit, upload_indices)
        )
        emit_pipeline_timing_event(
            args,
            "exact_segment_plan_ready",
            event="exact_segment_plan_ready",
            exact_records=len(expected_upload_payloads),
            exact_batches=(
                1
                if int(args.upload_limit or 0) > 0 and len(expected_upload_payloads)
                else planned_embedding_batch_count(len(expected_upload_payloads))
            ),
            upload_scope="probe" if int(args.upload_limit or 0) > 0 else "full",
            upload_withheld=bool(selected.get("upload_blocked_reason")),
        )
        if selected.get("upload_blocked_reason"):
            block_reason = str(selected["upload_blocked_reason"])
            if block_reason == "ocr_backend_text_coverage_disagreement":
                withheld_message = (
                    "AnythingLLM upload was withheld because OCR extractors materially disagree about text coverage."
                )
            elif block_reason == "photographed_spread_requires_manual_review":
                withheld_message = (
                    "AnythingLLM upload was withheld because photographed spreads require visual review."
                )
            else:
                withheld_message = "AnythingLLM upload was withheld because reliable OCR is required but unavailable."
            upload_report = {
                "status": "skipped_needs_ocr_review",
                "uploaded": 0,
                "embedded": 0,
                "errors": [],
                "warnings": [{
                    "warning": withheld_message,
                    "reasons": selected["readiness_reasons"],
                }],
            }
            expected_upload_payloads = []
        else:
            upload_report = maybe_upload_to_anythingllm(
                args.anythingllm_api_url,
                args.anythingllm_api_key,
                payloads_to_upload,
                upload_limit=args.upload_limit,
                upload_indices=upload_indices,
                workspace_slug=args.workspace_slug,
                upload_transport=upload_transport,
                upload_plan_rows=upload_plan_rows,
                storage_dir=storage_dir,
                folder_name=upload_folder_name,
                embedding_ledger_path=embedding_batch_ledger_path,
                status_callback=report_upload_status,
                batch_verifier=verify_embedding_batch,
                batch_inspector=inspect_embedding_batch_once,
                cancel_callback=getattr(args, "cancel_callback", None),
                submission_receipt_path=submission_receipt_path,
                run_id=str(getattr(args, "run_id", "") or source_sha or out_root.name),
                record_label=(
                    "page-parent files"
                    if upload_representation == "page_parents"
                    else "segment files"
                ),
            )
        upload_report["representation"] = upload_representation
        upload_report["transport"] = upload_transport
        upload_report["document_foldering_enabled"] = create_document_folders
        if selected["readiness_status"] != "ready":
            warnings = list(upload_report.get("warnings") or [])
            warnings.append(
                {
                    "warning": "Upload continued even though preparation needs review.",
                    "reasons": selected["readiness_reasons"],
                }
            )
            upload_report["warnings"] = warnings
    apply_temporary_key_cleanup_review(
        selected,
        upload_report,
        bool(args.prepare_and_upload),
    )
    selected_expected_upload_payloads = expected_upload_payloads
    write_json(inspection_dir / "api-upload-report.json", upload_report)
    if has_shared_batch_inspection:
        # The per-batch verifier above already observes this document's exact
        # upload path and vector evidence.  A full LanceDB/storage traversal is
        # both global and mutable, so defer it to one batch-final audit.
        storage_after_report = {
            "status": "deferred_to_batch_finalization",
            "message": "Per-document mutable storage sweep skipped; targeted post-upload verification remains recorded.",
        }
        inspection_context["needs_final_storage_audit"] = True
    else:
        with measured_pipeline_phase(args, "anythingllm_post_upload_storage_audit"):
            storage_after_report = inspect_anythingllm_storage(storage_dir)
    write_json(inspection_dir / "lancedb-after.json", storage_after_report)
    storage_diff = (
        {
            "before_status": storage_report.get("status"),
            "after_status": storage_after_report.get("status"),
            "total_added_rows": None,
            "rows": [],
            "status": "deferred_to_batch_finalization",
        }
        if has_shared_batch_inspection
        else compare_storage_snapshots(storage_report, storage_after_report)
    )
    write_json(inspection_dir / "lancedb-diff.json", storage_diff)
    write_csv(inspection_dir / "lancedb-diff.csv", storage_diff["rows"])
    if args.prepare_and_upload and has_shared_batch_inspection:
        # The pre-upload count is necessarily stale as soon as embedding
        # begins.  Deferring it avoids repeatedly opening the same LanceDB
        # namespaces for every PDF in a batch; the targeted post-upload poll
        # below supplies bounded live evidence instead.
        native_metadata_report = {
            "status": "deferred_to_post_upload_observation",
            "matching_rows": 0,
            "metadata_fields_seen": [],
            "text_contains_source_document": False,
            "text_contains_segment_or_page": False,
            "error": "",
        }
    else:
        with measured_pipeline_phase(args, "targeted_native_metadata_observation"):
            native_metadata_report = inspect_native_metadata_count(
                storage_dir,
                source_sha,
                workspace_namespace=str(target_workspace_slug or "") if args.prepare_and_upload else "",
            )
    write_json(inspection_dir / "native-metadata-storage-report.json", native_metadata_report)
    write_csv(
        inspection_dir / "native-metadata-storage-report.csv",
        [
            {
                "status": native_metadata_report.get("status"),
                "matching_rows": native_metadata_report.get("matching_rows"),
                "metadata_fields_seen": ", ".join(native_metadata_report.get("metadata_fields_seen", [])),
                "text_contains_source_document": native_metadata_report.get("text_contains_source_document"),
                "text_contains_segment_or_page": native_metadata_report.get("text_contains_segment_or_page"),
                "error": native_metadata_report.get("error", ""),
            }
        ],
    )
    metadata_layer_rows = metadata_layer_visibility_rows(
        native_payloads,
        page_parent_native_payloads,
        metadata_schema_report,
        native_metadata_report,
    )
    write_csv(inspection_dir / "metadata-layer-visibility.csv", metadata_layer_rows)
    write_json(inspection_dir / "metadata-layer-visibility.json", metadata_layer_rows)
    column_explanation_rows = explain_observed_columns(workspace_layer_report)
    write_csv(inspection_dir / "column-explanations.csv", column_explanation_rows)
    write_json(inspection_dir / "column-explanations.json", column_explanation_rows)
    failed_embedding_checkpoint = next(
        (
            batch
            for batch in ((upload_report.get("embedding_update") or {}).get("batches") or [])
            if str(batch.get("submission_state") or "") in {
                "verification_failed", "rejected", "cancelled_before_submission"
            }
        ),
        None,
    )
    ambiguous_embedding_submission = any(
        str(batch.get("submission_state") or "") == "unresolved"
        or str(batch.get("lifecycle_state") or "") in {
            "reconciliation_pending", "workspace_attached"
        }
        for batch in ((upload_report.get("embedding_update") or {}).get("batches") or [])
    )
    if args.prepare_and_upload and upload_report.get("uploaded", 0) > 0:
        def report_post_upload_observation(evidence, operator_state):
            observed_vectors = int(evidence.get("matching_vector_rows") or evidence.get("lancedb_matching_rows") or 0)
            expected_records = len(selected_expected_upload_payloads or payloads_to_upload)
            report_upload_phase(
                "identity_set",
                (
                    f"AnythingLLM indexing observation {evidence.get('attempt', 0)}: "
                    f"{format_vector_observation(observed_vectors, expected_records, operator_state, record_label=vector_record_label)}"
                ),
                completed_units=observed_vectors,
                total_units=expected_records,
                fallback_fraction=(observed_vectors / expected_records if expected_records else 0.0),
                desktop_required=True,
                evidence_kind="exact_vector_observation",
            )

        shared_reconciliation_elapsed = 0.0
        shared_reconciliation_remaining = None
        if ambiguous_embedding_submission:
            reconciliation_starts = [
                float(batch.get("reconciliation_started_at_epoch") or 0.0)
                for batch in ((upload_report.get("embedding_update") or {}).get("batches") or [])
                if float(batch.get("reconciliation_started_at_epoch") or 0.0) > 0.0
            ]
            if reconciliation_starts:
                shared_reconciliation_elapsed = max(0.0, time.time() - min(reconciliation_starts))
                effective_deadlines = [
                    float(
                        batch.get("reconciliation_effective_deadline_seconds")
                        or batch.get("reconciliation_deadline_seconds")
                        or ANYTHINGLLM_EMBEDDING_RECONCILIATION_TIMEOUT_SECONDS
                    )
                    for batch in ((upload_report.get("embedding_update") or {}).get("batches") or [])
                    if float(batch.get("reconciliation_started_at_epoch") or 0.0) > 0.0
                ]
                shared_reconciliation_remaining = max(
                    0.0,
                    max(effective_deadlines or [ANYTHINGLLM_EMBEDDING_RECONCILIATION_TIMEOUT_SECONDS])
                    - shared_reconciliation_elapsed,
                )
        if failed_embedding_checkpoint:
            fast_post_upload_report = verify_anythingllm_post_upload(
                storage_dir,
                target_workspace_slug,
                source_sha,
                selected_expected_upload_payloads or payloads_to_upload,
                upload_locations=(upload_report.get("locations") or []),
                observation_mode="fast",
            )
            polling_status = "skipped_redundant_after_failed_checkpoint"
            polling_attempts = 1
            polling_elapsed_seconds = 0.0
            polling_observations = [dict(fast_post_upload_report)]
            polling_observer_failures = []
        else:
            # An unresolved receipt already spent this run's shared
            # reconciliation budget in the batch observer.  Do one final
            # immediate document-wide read when it is exhausted; never begin
            # a second hidden 480-second poll after the first one.
            final_observation_timeout = (
                float(shared_reconciliation_remaining)
                if shared_reconciliation_remaining is not None
                else float(getattr(args, "post_upload_poll_timeout", 60.0))
            )
            polling_result = poll_post_upload(
                lambda: verify_anythingllm_post_upload(
                    storage_dir,
                    target_workspace_slug,
                    source_sha,
                    selected_expected_upload_payloads or payloads_to_upload,
                    upload_locations=(upload_report.get("locations") or []),
                    observation_mode="fast",
                ),
                interval_seconds=float(getattr(args, "post_upload_poll_interval", 2.0)),
                timeout_seconds=final_observation_timeout,
                hard_cap_seconds=final_observation_timeout,
                observation_callback=report_post_upload_observation,
                retryable_evidence_codes={"partial_vector_coverage"},
            )
            fast_post_upload_report = dict(polling_result.final_evidence)
            polling_status = polling_result.status
            polling_attempts = polling_result.attempts
            polling_elapsed_seconds = polling_result.elapsed_seconds
            polling_observations = polling_result.observations
            polling_observer_failures = polling_result.observer_failures
        expected_post_upload_records = len(selected_expected_upload_payloads or payloads_to_upload)
        # Polling must be cheap and non-materializing. The broad observation
        # is valuable for a mismatch or recovery, but exact expected-vector
        # evidence already proves the normal success case. Do not turn that
        # healthy state into a delayed or weaker result solely to re-scan
        # storage and the Desktop document list.
        if full_post_upload_observation_is_required(
            fast_post_upload_report,
            expected_post_upload_records,
            failed_checkpoint=bool(failed_embedding_checkpoint),
            ambiguous_submission=ambiguous_embedding_submission,
        ):
            try:
                post_upload_report = verify_anythingllm_post_upload(
                    storage_dir,
                    target_workspace_slug,
                    source_sha,
                    selected_expected_upload_payloads or payloads_to_upload,
                    upload_locations=(upload_report.get("locations") or []),
                    observation_mode="full",
                    frontend_api_url=args.anythingllm_api_url,
                )
            except Exception as exc:
                post_upload_report = dict(fast_post_upload_report)
                post_upload_report["full_observation_error"] = f"{type(exc).__name__}: {exc}"
        else:
            post_upload_report = dict(fast_post_upload_report)
            post_upload_report["full_observation"] = "deferred_exact_vectors_healthy"
            post_upload_report["full_observation_reason"] = (
                "Exact expected vectors were observed without a failed checkpoint or ambiguous submission. "
                "Use Verify deeply or the workspace audit for a later broad inspection."
            )
        post_upload_report["fast_polling_status"] = fast_post_upload_report.get("status")
        post_upload_report["operator_status"] = polling_status
        post_upload_report["polling_attempts"] = polling_attempts
        post_upload_report["polling_elapsed_seconds"] = polling_elapsed_seconds
        post_upload_report["polling_observations"] = polling_observations
        post_upload_report["polling_observer_failures"] = polling_observer_failures
        if failed_embedding_checkpoint:
            post_upload_report["polling_skipped_reason"] = (
                "A completed batch checkpoint had already failed searchability verification; "
                "the pipeline took one final full observation instead of repeating the wait."
            )
        elif ambiguous_embedding_submission:
            post_upload_report["reconciliation_window_seconds"] = (
                ANYTHINGLLM_EMBEDDING_RECONCILIATION_TIMEOUT_SECONDS
            )
            post_upload_report["reconciliation_elapsed_before_document_check_seconds"] = round(
                shared_reconciliation_elapsed, 3
            )
            post_upload_report["reconciliation_remaining_for_document_check_seconds"] = round(
                float(shared_reconciliation_remaining or 0.0), 3
            )
            post_upload_report["reconciliation_reason"] = (
                "At least one embedding submission had an ambiguous client outcome. "
                "The original run remained active under one shared 480-second local workspace/vector observation budget."
            )
    else:
        post_upload_report = {
            "status": "not_checked_no_upload",
            "workspace_slug": target_workspace_slug,
            "workspace_found": False,
            "workspace_document_count": 0,
            "matching_workspace_documents": 0,
            "matching_vector_rows": 0,
            "metadata_survived_in_workspace_documents": False,
            "lancedb_matching_rows": 0,
            "lancedb_matching_tables": [],
            "lancedb_text_contains_page_or_segment": False,
            "classification": "not_checked",
            "message": "Post-upload verification was skipped because no upload was attempted.",
            "error": "",
            "polling_observer_failures": [],
        }
    embedding_checkpoint_evidence = dict(upload_report.get("embedding_update") or {})
    reconciliation_cap_evidence = [
        dict((batch.get("verification") or {}))
        for batch in (embedding_checkpoint_evidence.get("batches") or [])
        if isinstance(batch, dict)
        and str((batch.get("verification") or {}).get("reconciliation_cap_classification") or "")
    ]
    if reconciliation_cap_evidence:
        latest_cap = reconciliation_cap_evidence[-1]
        post_upload_report["reconciliation_cap_classification"] = latest_cap.get(
            "reconciliation_cap_classification"
        )
        post_upload_report["reconciliation_cap_message"] = latest_cap.get("message")
        post_upload_report["reconciliation_cap_vector_progress_count"] = latest_cap.get(
            "reconciliation_vector_progress_count"
        )
    post_upload_report["embedding_verification_policy"] = {
        "mode": embedding_checkpoint_evidence.get("verification_mode", "not_applicable"),
        "interval": embedding_checkpoint_evidence.get("verification_interval", 0),
        "deferred_batches": embedding_checkpoint_evidence.get("deferred_verification_batches", []),
        "final_document_verification": "observed" if args.prepare_and_upload else "not_applicable",
    }
    # An ambiguous HTTP outcome can be resolved by the *same run* once all
    # expected vectors appear.  Replace the interim timeout state rather than
    # leaving the UI and recovery manifest stuck on a historic failure.
    observed_vectors = max(
        int(post_upload_report.get("matching_vector_rows") or 0),
        int(post_upload_report.get("lancedb_matching_rows") or 0),
    )
    expected_vectors = len(selected_expected_upload_payloads or payloads_to_upload)
    if (
        ambiguous_embedding_submission
        and post_upload_report.get("status") in REVIEWABLE_POST_UPLOAD_STATUSES
        and expected_vectors > 0
        and observed_vectors >= expected_vectors
    ):
        embedding_update = upload_report.get("embedding_update") or {}
        recovered_batches = []
        for batch in embedding_update.get("batches") or []:
            if (
                str(batch.get("submission_state") or "") == "unresolved"
                or str(batch.get("lifecycle_state") or "") in {
                    "reconciliation_pending", "workspace_attached"
                }
            ):
                batch["submission_state"] = "accepted"
                batch["accepted"] = int(batch.get("requested") or 0)
                batch["acceptance_basis"] = "document_wide_vector_observation_after_client_timeout"
                set_embedding_batch_lifecycle(
                    batch,
                    "vector_observed",
                    "The original run's document-wide reconciliation observed complete vectors.",
                )
                recovered_batches.append(int(batch.get("batch") or 0))
        def is_ambiguous_submission_error(error):
            return str((error or {}).get("classification") or "") in {
                "client_timeout_submission_unknown", "client_transport_submission_unknown"
            }
        embedding_update["errors"] = [
            error for error in (embedding_update.get("errors") or [])
            if not is_ambiguous_submission_error(error)
        ]
        embedding_update["accepted"] = max(
            int(embedding_update.get("accepted") or 0),
            len(upload_report.get("locations") or []),
        )
        embedding_update.setdefault("runtime_events", []).append(
            {
                "event": "client_timeout_reconciled_by_document_wide_vector_observation",
                "recovered_batches": recovered_batches,
                "observed_vectors": observed_vectors,
                "expected_vectors": expected_vectors,
            }
        )
        upload_report["errors"] = [
            error for error in (upload_report.get("errors") or [])
            if not is_ambiguous_submission_error(error)
        ]
        upload_report["embedded"] = len(upload_report.get("locations") or [])
        if not upload_report["errors"]:
            upload_report["status"] = (
                "complete_with_key_cleanup_warning"
                if (upload_report.get("temporary_key_cleanup") or {}).get("status") == "delete_failed"
                else "complete"
            )
        post_upload_report["reconciled_original_run"] = True
        post_upload_report["reconciled_batches"] = recovered_batches
        write_json(inspection_dir / "api-upload-report.json", upload_report)
    if post_upload_report.get("status") == "partial_vector_coverage":
        # Preserve an explicit upload-level incomplete state even when all
        # HTTP submissions were accepted. A successful probe must not hide
        # missing planned coverage.
        if upload_report.get("status") in {"complete", "complete_with_key_cleanup_warning"}:
            upload_report["status"] = "partial_searchable"
        upload_report.setdefault("warnings", []).append(
            {
                "warning": "Some searchable vectors exist, but final document-wide verification found incomplete planned coverage.",
                "post_upload_status": "partial_vector_coverage",
            }
        )
        # A timed-out Desktop request remains unresolved, but the final
        # document-wide observation may already show a subset of its exact
        # vectors. Record that subset per planned location before writing the
        # durable resume manifest. The recovery action can then resubmit only
        # the genuinely missing records, never the eight (or any other) late
        # vectors that did complete after the client lost its response.
        embedding_update = upload_report.get("embedding_update") or {}
        unresolved_batches = [
            batch for batch in (embedding_update.get("batches") or [])
            if str(batch.get("submission_state") or "") == "unresolved"
            or str(batch.get("lifecycle_state") or "") == "reconciliation_pending"
        ]
        if unresolved_batches and embedding_batch_ledger_path:
            locations = list(upload_report.get("locations") or [])
            # Recover the exact missing locations from one scalar
            # identity-set observation.  Do not open the workspace table once
            # per page-parent while Desktop may still be writing it.
            identity_evidence = verify_anythingllm_post_upload(
                storage_dir,
                target_workspace_slug,
                source_sha,
                selected_expected_upload_payloads or payloads_to_upload,
                upload_locations=locations,
                observation_mode="identity",
            )
            observed_sources = {
                str(value).strip()
                for value in (identity_evidence.get("observed_chunk_sources") or [])
                if str(value).strip()
            }
            confirmed_locations = {
                str(locations[index])
                for index, payload in enumerate(selected_expected_upload_payloads or payloads_to_upload)
                if index < len(locations)
                and str((payload.get("metadata", {}) or {}).get("chunkSource") or "").strip()
                in observed_sources
            }
            for batch in unresolved_batches:
                batch_locations = [str(item) for item in (batch.get("locations") or [])]
                verification = dict(batch.get("verification") or {})
                verification["confirmed_locations"] = [
                    location for location in batch_locations if location in confirmed_locations
                ]
                verification["unresolved_locations"] = [
                    location for location in batch_locations if location not in confirmed_locations
                ]
                verification["confirmed_record_count"] = len(verification["confirmed_locations"])
                verification["unresolved_record_count"] = len(verification["unresolved_locations"])
                verification["status"] = "partial_vector_coverage"
                batch["verification"] = verification
            _write_embedding_batch_ledger(
                embedding_batch_ledger_path,
                target_workspace_slug,
                embedding_update,
            )
    write_json(inspection_dir / "post-upload-verification.json", post_upload_report)
    write_csv(
        inspection_dir / "post-upload-verification.csv",
        [
            {
                "status": post_upload_report.get("status"),
                "workspace_slug": post_upload_report.get("workspace_slug"),
                "workspace_document_count": post_upload_report.get("workspace_document_count"),
                "matching_workspace_documents": post_upload_report.get("matching_workspace_documents"),
                "desktop_frontend_observation": post_upload_report.get("desktop_frontend_observation"),
                "desktop_frontend_document_count": post_upload_report.get("desktop_frontend_document_count"),
                "desktop_frontend_document_count_matches_storage": post_upload_report.get("desktop_frontend_document_count_matches_storage"),
                "matching_vector_rows": post_upload_report.get("matching_vector_rows"),
                "metadata_survived_in_workspace_documents": post_upload_report.get("metadata_survived_in_workspace_documents"),
                "lancedb_matching_rows": post_upload_report.get("lancedb_matching_rows"),
                "lancedb_text_contains_page_or_segment": post_upload_report.get("lancedb_text_contains_page_or_segment"),
                "classification": post_upload_report.get("classification"),
                "message": post_upload_report.get("message"),
                # The full structured failures are retained in the JSON report
                # and batch ledger. Keep the spreadsheet-facing report compact
                # while making an observer problem impossible to miss.
                "polling_observer_failure_count": len(
                    post_upload_report.get("polling_observer_failures") or []
                ),
            }
        ],
    )
    if (
        args.prepare_and_upload
        and upload_report.get("uploaded", 0) > 0
        and post_upload_report.get("status") in REVIEWABLE_POST_UPLOAD_STATUSES
    ):
        report_upload_phase(
            "retrieval_sample",
            "Checking runtime retrieval after exact vector confirmation",
            completed_units=0,
            total_units=1,
            fallback_fraction=0.0,
            evidence_kind="validation_started",
        )
        cached_embedder_probe = None
        if isinstance(inspection_context, dict):
            candidate_probe = inspection_context.get("anythingllm_runtime_embedder_probe")
            if isinstance(candidate_probe, dict) and candidate_probe.get("status") == "pass":
                cached_embedder_probe = candidate_probe
        def report_runtime_validation_progress(stage, details):
            completed = int((details or {}).get("completed") or 0)
            total = max(1, int((details or {}).get("total") or 1))
            action = {
                "vector_probe_started": "Running retrieval sample",
                "vector_probe_completed": "Retrieval sample completed",
                "vector_recheck_started": "Rechecking a timed-out retrieval sample",
                "vector_recheck_completed": "Timed-out retrieval sample rechecked",
                "chat_probe_started": "Running chat retrieval diagnostic",
                "chat_probe_completed": "Chat retrieval diagnostic completed",
            }.get(str(stage), "Running runtime retrieval validation")
            report_upload_phase(
                "retrieval_sample",
                f"{action}: {completed}/{total}",
                completed_units=completed,
                total_units=total,
                fallback_fraction=0.0,
                desktop_required=True,
                evidence_kind=str(stage),
            )

        runtime_validation_report = validate_anythingllm_native_runtime(
            args.anythingllm_api_url,
            args.anythingllm_api_key,
            target_workspace_slug,
            selected_expected_upload_payloads or payloads_to_upload,
            0,
            storage_dir,
            embedder_probe_override=cached_embedder_probe,
            runtime_probe_limit=runtime_validation_sample_size(
                selected_expected_upload_payloads or payloads_to_upload,
            ),
            vector_timeout_seconds=8,
            # A freshly completed Desktop queue can leave a live search cold
            # even though local exact-vector evidence is already complete.
            # Keep each sampled probe short and single-shot: a timeout stays
            # diagnostic rather than converting validation into a long hidden
            # retry loop.
            vector_max_attempts=1,
            retry_timed_out_siblings=False,
            status_callback=report_runtime_validation_progress,
        )
        # Normal completion uses the exact identity set plus a small
        # stratified retrieval sample. If that sample exposes a miss or a
        # runtime timeout, preserve the fast result but immediately perform
        # the existing all-page-parent diagnostic audit. This is the explicit
        # escalation path, not a cost paid by every healthy large PDF.
        if runtime_validation_report.get("status") in {
            "vector_retrieval_failed",
            "vector_runtime_timeout",
            "blocked_provider_authentication",
        }:
            try:
                post_upload_report["sample_failure_full_identity_audit"] = verify_anythingllm_post_upload(
                    storage_dir,
                    target_workspace_slug,
                    source_sha,
                    selected_expected_upload_payloads or payloads_to_upload,
                    upload_locations=(upload_report.get("locations") or []),
                    observation_mode="full",
                )
            except Exception as exc:
                post_upload_report["sample_failure_full_identity_audit"] = {
                    "status": "audit_error",
                    "error": f"{type(exc).__name__}: {exc}",
                }
        embedder_probe = runtime_validation_report.get("embedder_probe")
        if (
            isinstance(inspection_context, dict)
            and isinstance(embedder_probe, dict)
            and embedder_probe.get("status") == "pass"
        ):
            inspection_context["anythingllm_runtime_embedder_probe"] = {
                key: value
                for key, value in embedder_probe.items()
                if key != "cache_reused"
            }
    else:
        runtime_validation_report = {
            "status": (
                "not_run_post_upload_incomplete"
                if args.prepare_and_upload and upload_report.get("uploaded", 0) > 0
                else "not_checked_no_upload"
            ),
            "workspace_slug": target_workspace_slug,
            "model_gate": workspace_gate,
            "vector_checks": [],
            "chat_check": {},
            "authentication_mode": "not_applicable",
            "temporary_key_cleanup": {
                "status": "not_applicable",
                "error": "",
            },
            "message": (
                "Runtime retrieval was not queried because searchable-vector verification was incomplete."
                if args.prepare_and_upload and upload_report.get("uploaded", 0) > 0
                else "Runtime retrieval was not queried because no upload completed."
            ),
        }
    if (
        (not args.prepare_and_upload)
        and bool(getattr(args, "run_chunk_survival_validation", False))
        and (args.anythingllm_api_url or "").strip()
        and should_run_chunk_survival_validation(selected)
    ):
        cached_embedder_probe = None
        if isinstance(inspection_context, dict):
            candidate_probe = inspection_context.get("anythingllm_runtime_embedder_probe")
            if isinstance(candidate_probe, dict) and candidate_probe.get("status") == "pass":
                cached_embedder_probe = candidate_probe
        temporary_workspace_validation = run_temporary_workspace_validation(
            args.anythingllm_api_url,
            args.anythingllm_api_key,
            storage_dir,
            source_sha,
            validation_payloads,
            upload_limit=max(0, int(getattr(args, "upload_limit", 0) or 0)),
            top_n=8,
            upload_transport=upload_transport,
            upload_plan_rows=upload_plan_rows,
            cleanup_policy=getattr(
                args, "temporary_validation_cleanup_policy", "cleanup_always"
            ),
            embedder_probe_override=cached_embedder_probe,
        )
        embedder_probe = (
            temporary_workspace_validation.get("runtime_validation_report") or {}
        ).get("embedder_probe")
        if (
            isinstance(inspection_context, dict)
            and isinstance(embedder_probe, dict)
            and embedder_probe.get("status") == "pass"
        ):
            inspection_context["anythingllm_runtime_embedder_probe"] = {
                key: value
                for key, value in embedder_probe.items()
                if key != "cache_reused"
            }
    else:
        temporary_workspace_validation = {
            "status": "not_run",
            "workspace_slug": "",
            "workspace_name": "",
            "workspace_create_status": "not_run",
            "upload_status": "not_run",
            "post_upload_status": "not_run",
            "runtime_validation_status": "not_run",
            "retention_status": "not_run",
            "post_upload_report": {},
            "runtime_validation_report": {},
            "upload_report": {},
            "error": "",
        }
    write_json(
        inspection_dir / "anythingllm-runtime-validation.json",
        runtime_validation_report,
    )
    write_json(
        inspection_dir / "temporary-workspace-validation.json",
        temporary_workspace_validation,
    )
    write_csv(
        inspection_dir / "temporary-workspace-validation.csv",
        [
            {
                "status": temporary_workspace_validation.get("status"),
                "workspace_slug": temporary_workspace_validation.get("workspace_slug"),
                "workspace_create_status": temporary_workspace_validation.get("workspace_create_status"),
                "upload_status": temporary_workspace_validation.get("upload_status"),
                "post_upload_status": temporary_workspace_validation.get("post_upload_status"),
                "runtime_validation_status": temporary_workspace_validation.get("runtime_validation_status"),
                "cleanup_policy": temporary_workspace_validation.get("cleanup_policy"),
                "retention_status": temporary_workspace_validation.get("retention_status"),
                "cleanup_status": (temporary_workspace_validation.get("cleanup_result") or {}).get("status"),
                "chunk_survival_flag": (temporary_workspace_validation.get("post_upload_report") or {}).get("chunk_survival_flag"),
                "chunk_survival_ratio": (temporary_workspace_validation.get("post_upload_report") or {}).get("chunk_survival_ratio"),
                "page_provenance_risk": (temporary_workspace_validation.get("post_upload_report") or {}).get("page_provenance_risk"),
                "error": temporary_workspace_validation.get("error"),
            }
        ],
    )
    runtime_rows = []
    embedder_probe = runtime_validation_report.get("embedder_probe") or {}
    if embedder_probe:
        runtime_rows.append(
            {
                "check": "embedder_runtime_probe",
                "status": "pass" if embedder_probe.get("status") == "pass" else "fail",
                "http_status": embedder_probe.get("http_status"),
                "expected": (
                    f"{embedder_probe.get('provider') or 'unknown'} / "
                    f"{embedder_probe.get('model') or 'unknown'}"
                ),
                "observed": (
                    f"status={embedder_probe.get('status')} "
                    f"dim={embedder_probe.get('dimension', 0)} "
                    f"tokens={embedder_probe.get('usage_total_tokens', 0)}"
                ),
                "elapsed_seconds": embedder_probe.get("elapsed_seconds", 0),
                "retry_count": embedder_probe.get("retry_count", 0),
                "error_class": embedder_probe.get("error_class", "none"),
                "error": embedder_probe.get("error", "") or embedder_probe.get("message", ""),
            }
        )
    runtime_rows.extend(
        {
            "check": f"vector_{index + 1}",
            "status": "pass" if row.get("expected_in_top_n") else "fail",
            "http_status": row.get("http_status"),
            "expected": row.get("expected_chunk_source"),
            "observed": row.get("top_chunk_source"),
            "endpoint": row.get("endpoint", ""),
            "elapsed_seconds": row.get("elapsed_seconds", 0),
            "retry_count": row.get("retry_count", 0),
            "error_class": row.get("error_class", "none"),
            "error": row.get("error", ""),
        }
        for index, row in enumerate(runtime_validation_report.get("vector_checks", []))
    )
    if runtime_validation_report.get("chat_check"):
        chat_check = runtime_validation_report["chat_check"]
        runtime_rows.append(
            {
                "check": "deepseek_chat_page_segment",
                "status": (
                    "pass"
                    if chat_check.get("response_contains_expected_page_segment")
                    else "fail"
                ),
                "http_status": chat_check.get("http_status"),
                "expected": (
                    f"page {chat_check.get('expected_page')}, "
                    f"segment {chat_check.get('expected_segment')}"
                ),
                "observed": chat_check.get("text_response", ""),
                "endpoint": chat_check.get("endpoint", ""),
                "elapsed_seconds": chat_check.get("elapsed_seconds", 0),
                "retry_count": chat_check.get("retry_count", 0),
                "error_class": chat_check.get("error_class", "none"),
                "error": chat_check.get("error", ""),
            }
        )
    write_csv(
        inspection_dir / "anythingllm-runtime-validation.csv",
        runtime_rows,
    )
    diagnostics = build_run_diagnostics(
        profile,
        selected,
        candidates,
        storage_report,
        upload_report,
        workspace_gate,
        post_upload_report,
        metadata_schema_report,
        runtime_validation_report,
        temporary_workspace_validation,
    )
    write_json(out_root / "diagnostics.json", diagnostics)
    write_csv(out_root / "diagnostics.csv", diagnostics)
    (out_root / "diagnostics.html").write_text(build_diagnostics_html(diagnostics), encoding="utf-8")

    retrieval_dir = out_root / "retrieval-eval"
    partial_vector_coverage = (
        str(post_upload_report.get("status") or "") == "partial_vector_coverage"
    )
    if not partial_vector_coverage:
        report_upload_phase(
            "validation",
            "Exact storage and retrieval validation complete",
            completed_units=1,
            total_units=1,
            fallback_fraction=PIPELINE_PROGRESS_REPORTING,
            evidence_kind="validation_completed",
        )
        report_upload_phase(
            "reporting",
            "Writing retrieval and readiness reports",
            completed_units=0,
            total_units=1,
            fallback_fraction=PIPELINE_PROGRESS_REPORTING,
            evidence_kind="phase_started",
        )
    else:
        # Reports are still written for investigation, but they are not
        # workflow completion evidence. Keep the single progress bar at the
        # last exact vector checkpoint rather than advancing into validation
        # or final reporting after a partial Desktop queue.
        report_upload_phase(
            "identity_set",
            "AnythingLLM indexing remains incomplete; preserving the exact vector checkpoint",
            completed_units=int(post_upload_report.get("matching_vector_rows") or 0),
            total_units=len(selected_expected_upload_payloads or payloads_to_upload),
            fallback_fraction=0.0,
            desktop_required=True,
            evidence_kind="partial_vector_coverage",
        )
    retrieval_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_candidate_dir / "probes.jsonl", retrieval_dir / "probes.jsonl")
    shutil.copy2(src_candidate_dir / "literal-results.csv", retrieval_dir / "literal-results.csv")
    if (src_candidate_dir / "vector-results.csv").exists():
        shutil.copy2(src_candidate_dir / "vector-results.csv", retrieval_dir / "vector-results.csv")
    else:
        write_csv(retrieval_dir / "vector-results.csv", [])

    profile["backends"] = [
        {
            "backend": c["backend"],
            "score": c.get("score"),
            "error": c.get("error", ""),
            "start_page": c.get("start_page", ""),
            "end_page": c.get("end_page", ""),
            "detected_end_page": c.get("detected_end_page", ""),
            "boundary_reference_backend": c.get("boundary_reference_backend", ""),
            "boundary_reference_reliable": bool(c.get("boundary_reference_reliable")),
            "independent_start_page": c.get("independent_start_page", ""),
            "independent_detected_end_page": c.get("independent_detected_end_page", ""),
            "boundary_reconciled": bool(c.get("boundary_reconciled")),
            "include_back_matter": c.get("include_back_matter", False),
            "segments": len(c.get("segments", [])),
            "quality": c.get("quality", {}),
            "marker_stats": c.get("marker_stats", {}),
            "native_chunk_eval": c.get("native_chunk_eval", {}),
            "outline_validation": c.get("outline_validation", {}),
            "variant_outputs": c.get("variant_outputs", {}),
            "unstructured_execution": c.get("unstructured_execution", {}),
            "score_reasons": c.get("score_reasons", []),
        }
        for c in candidates
    ]
    profile["selected_backend"] = selected["backend"]
    profile["unstructured_runtime"]["used_for_selected_output"] = selected["backend"] == "unstructured"
    profile["unstructured_runtime"]["selected_strategy"] = selected.get("unstructured_strategy") or None
    profile["unstructured_runtime"]["selected_strategy_reason"] = (
        selected.get("unstructured_strategy_reason") or ""
    )
    profile["unstructured_runtime"]["execution"] = dict(
        selected.get("unstructured_execution") or {}
    )
    profile["unstructured_runtime"]["batch_circuit_breaker"] = dict(
        getattr(args, "unstructured_circuit_breaker", {}) or {}
    )
    profile["unstructured_auto_trigger"] = {
        "triggered": "unstructured" in backend_names and not bool(args.deep_extraction),
        "reasons": sorted(set(auto_unstructured_reasons)),
        "suppressed_reasons": sorted(set(auto_unstructured_suppressed_reasons)),
        "user_requested": bool(args.deep_extraction),
    }
    profile["automatic_candidate_shortcuts"] = sorted(set(automatic_candidate_shortcuts))
    profile["readiness_status"] = selected["readiness_status"]
    profile["readiness_reasons"] = selected["readiness_reasons"]
    profile["backend_word_disagreement"] = selected["backend_word_disagreement"]
    profile["backend_word_disagreement_resolution"] = selected[
        "backend_word_disagreement_resolution"
    ]
    profile["boundary_decisions"] = {
        "body_start": {
            "pdf_page": selected.get("start_page"),
            "source": selected.get("start_reason"),
            "confidence": (
                "high"
                if str(selected.get("start_reason", "")).startswith("pdf_outline")
                else "medium"
                if selected.get("start_reason") not in {"first_nonempty_page", "table_of_contents_fallback"}
                else "low"
            ),
            "excluded_front_range": (
                f"1-{int(selected.get('start_page')) - 1}"
                if int(selected.get("start_page") or 1) > 1
                else "none"
            ),
        },
        "end_matter": {
            "starts_at_pdf_page": selected.get("detected_end_page"),
            "included_in_primary_output": bool(selected.get("include_back_matter")),
            "effective_selected_end_page": selected.get("end_page"),
            "heading": selected.get("end_heading") or "",
            "source": selected.get("end_source") or "",
            "confidence": (
                "high"
                if selected.get("end_source") in {"pdf_outline", "user_override"}
                else "medium"
                if selected.get("detected_end_page")
                else "not_detected"
            ),
            "excluded_end_range": (
                f"{selected.get('detected_end_page')}-{profile.get('pdf_page_count')}"
                if selected.get("detected_end_page") and not selected.get("include_back_matter")
                else "none"
            ),
        },
    }
    profile["page_profile"] = selected.get("page_stats", [])
    profile["layout_evidence"] = selected.get("layout_evidence") or {}
    profile["retrieval_lane_review"] = selected.get("lane_review") or {}
    selected_quality = selected.get("quality", {})
    profile["document_classification"] = {
        "scanned_likelihood": selected_quality.get("scanned_likelihood", "unknown"),
        "text_layered": selected_quality.get("scanned_likelihood") == "low",
        "front_matter_heavy": bool(selected.get("start_page", 1) > max(8, int(profile["pdf_page_count"] * 0.12))),
        "index_or_bibliography_detected": bool(
            selected_quality.get("index_like_pages") or selected_quality.get("bibliography_like_pages")
        ),
        "outline_reliability": selected.get("outline_validation", {}).get("reliability", "unknown"),
    }
    write_json(out_root / "source-profile.json", profile)

    output_paths = [
        prepared_text_path,
        selected_dir / "anythingllm-upload-inline-metadata-fallback.txt",
        selected_dir / "segment-manifest.jsonl",
        selected_dir / "page-transition-manifest.jsonl",
        selected_dir / "page-parent-manifest.jsonl",
        selected_dir / "child-parent-map.csv",
        selected_dir / "layout-region-review.json",
        selected_dir / "retrieval-lane-review.json",
        selected_dir / "supplementary-content-candidates.txt",
        provenance_review_manifest,
        selected_dir / "representation-comparison.csv",
        selected_dir / "representation-comparison.json",
        selected_dir / "harmonization-report.csv",
        selected_dir / "harmonization-report.json",
        selected_dir / "representation-recommendation.csv",
        selected_dir / "representation-recommendation.json",
        selected_dir / "extraction-report.csv",
        selected_dir / "outline-validation.csv",
        selected_dir / "metadata-ratio.csv",
        selected_dir / "native-header-chunk-audit.csv",
        selected_dir / "output-variant-summary.csv",
        selected_dir / "readiness-report.html",
        metadata_dir / "raw-text-payloads-native-header.jsonl",
        metadata_dir / "raw-text-payloads-page-parents-native-header.jsonl",
        metadata_dir / "upload-plan.csv",
        metadata_dir / "page-parent-upload-plan.csv",
        inspection_dir / "metadata-compatibility-report.csv",
        inspection_dir / "metadata-layer-visibility.csv",
        inspection_dir / "metadata-layer-visibility.json",
        inspection_dir / "column-explanations.csv",
        inspection_dir / "column-explanations.json",
        inspection_dir / "author-inference-evaluation.csv",
        inspection_dir / "author-inference-evaluation.json",
        inspection_dir / "anythingllm-metadata-schema.json",
        inspection_dir / "native-metadata-storage-report.csv",
        inspection_dir / "workspace-model-gate.csv",
        inspection_dir / "post-upload-verification.csv",
        inspection_dir / "anythingllm-runtime-validation.csv",
        inspection_dir / "anythingllm-runtime-validation.json",
        out_root / "edge-case-results.csv",
        out_root / "edge-case-report.html",
        out_root / "diagnostics.html",
        out_root / "diagnostics.csv",
    ]
    for kit in (native_test_kit, native_probe_kit):
        for key in ("files_dir", "upload_plan", "checklist"):
            if kit.get(key):
                output_paths.append(Path(kit[key]))
    for variant in selected_variants.values():
        if variant.get("upload_file"):
            output_paths.append(Path(variant["upload_file"]))
        if variant.get("fallback_upload_file"):
            output_paths.append(Path(variant["fallback_upload_file"]))
    output_paths = [path for path in output_paths if path.exists()]
    report_html = build_html_report(profile, candidates, selected, output_paths, storage_report, upload_report)
    (selected_dir / "readiness-report.html").write_text(report_html, encoding="utf-8")
    harmonization_by_name = {row["representation"]: row for row in harmonization_report_rows}
    segment_harmonization = harmonization_by_name.get("passage_segments", {})
    parent_harmonization = harmonization_by_name.get("page_parents", {})
    active_segmentation_policy = segmentation_policy_for(getattr(args, "segment_mode", "passages"))
    sample_custom_document = workspace_layer_report.get("sample_custom_document_record")
    sample_lancedb_row = workspace_layer_report.get("sample_lancedb_row")
    sample_custom_document_title = (
        str(sample_custom_document.get("title") or "")
        if isinstance(sample_custom_document, dict)
        else ""
    )
    sample_lancedb_title = (
        str(sample_lancedb_row.get("title") or "")
        if isinstance(sample_lancedb_row, dict)
        else ""
    )
    summary = {
        "output_root": str(out_root),
        "source_sha256": profile.get("source_sha256", ""),
        "total_pipeline_seconds": round(time.perf_counter() - total_started, 2),
        "readiness_status": selected["readiness_status"],
        "readiness_reasons": selected["readiness_reasons"],
        "vector_validation_status": selected["vector_validation_status"],
        "vector_error_detail": selected.get("vector_error_detail", ""),
        "vector_eval_seconds": selected.get("vector_eval_seconds", 0),
        "vector_embedded_segments": selected.get("vector_embedded_segments", 0),
        "vector_embedded_chunks": selected.get("vector_embedded_chunks", 0),
        "vector_probe_count": selected.get("vector_probe_count", 0),
        "vector_request_batches": selected.get("vector_request_batches", 0),
        "vector_remote_requests": selected.get("vector_remote_requests", 0),
        "vector_remote_prompt_tokens": (selected.get("vector_remote_usage") or {}).get("prompt_tokens", 0),
        "vector_remote_total_tokens": (selected.get("vector_remote_usage") or {}).get("total_tokens", 0),
        "vector_remote_cost": (selected.get("vector_remote_usage") or {}).get("cost", 0.0),
        "vector_remote_timeout_seconds": (selected.get("vector_remote_usage") or {}).get("timeout_seconds", 0),
        "vector_remote_key_source": (selected.get("vector_remote_usage") or {}).get("key_source", ""),
        "vector_remote_usage_missing_responses": (selected.get("vector_remote_usage") or {}).get("usage_missing_responses", 0),
        "vector_remote_embedding_missing_responses": (selected.get("vector_remote_usage") or {}).get("embedding_missing_responses", 0),
        "vector_remote_slow_requests": (selected.get("vector_remote_usage") or {}).get("slow_requests", 0),
        "vector_remote_latency_ms_total": (selected.get("vector_remote_usage") or {}).get("latency_ms_total", 0),
        "vector_remote_latency_ms_max": (selected.get("vector_remote_usage") or {}).get("latency_ms_max", 0),
        "vector_remote_anomalies": (selected.get("vector_remote_usage") or {}).get("anomalies", []),
        "backend_word_disagreement": selected["backend_word_disagreement"],
        "backend_word_disagreement_resolution": selected[
            "backend_word_disagreement_resolution"
        ],
        "selected_backend": selected["backend"],
        "unstructured_selected_strategy": profile["unstructured_runtime"].get("selected_strategy"),
        "ocr_assisted_extraction_used": bool(ocr_evidence["used"]),
        "ocr_assisted_extraction_evidence": ocr_evidence["evidence"],
        "pdf_page_count": profile["pdf_page_count"],
        "start_page": selected["start_page"],
        "end_page": selected["end_page"],
        "detected_end_page": selected.get("detected_end_page"),
        "include_back_matter": bool(selected.get("include_back_matter")),
        "segment_mode": getattr(args, "segment_mode", "passages"),
        "segmentation_algorithm_version": active_segmentation_policy.algorithm_version,
        "segmentation_policy": active_segmentation_policy.to_dict(),
        "segments": len(selected["segments"]),
        "page_parents": len(page_parent_rows),
        "segment_units_exceeding_effective_limit": segment_harmonization.get("units_exceeding_effective_limit", 0),
        "page_parent_units_exceeding_effective_limit": parent_harmonization.get("units_exceeding_effective_limit", 0),
        "segment_harmonization_risk": segment_harmonization.get("harmonization_risk", ""),
        "page_parent_harmonization_risk": parent_harmonization.get("harmonization_risk", ""),
        "marker_char_ratio": selected.get("marker_stats", {}).get("marker_char_ratio"),
        "avg_content_chars": selected.get("marker_stats", {}).get("avg_content_chars"),
        "outline_reliability": selected.get("outline_validation", {}).get("reliability"),
        "layout_region_status": (selected.get("layout_evidence") or {}).get("status", "not_applied"),
        "layout_removed_marginalia_count": (selected.get("layout_evidence") or {}).get("removed_marginalia_count", 0),
        "layout_note_candidates_retained_count": (selected.get("layout_evidence") or {}).get("note_candidates_retained_count", 0),
        "layout_excluded_footnote_count": (selected.get("layout_evidence") or {}).get("excluded_footnote_count", 0),
        "layout_two_column_page_count": (selected.get("layout_evidence") or {}).get("two_column_page_count", 0),
        "retrieval_lane_status": (selected.get("lane_review") or {}).get("status", "not_available"),
        "retrieval_lane_primary_payload_changed": bool((selected.get("lane_review") or {}).get("primary_payload_changed")),
        "retrieval_lane_proposed_supplementary_count": (selected.get("lane_review") or {}).get("proposed_supplementary_count", 0),
        "retrieval_lane_proposed_supplementary_segments": (selected.get("lane_review") or {}).get("proposed_supplementary_segment_count", 0),
        "retrieval_lane_primary_excluded_segments": (selected.get("lane_review") or {}).get("primary_excluded_segment_count", 0),
        "chunk_size": chunk_size,
        "chunk_overlap": chunk_overlap,
        "chunk_settings_source": profile["anythingllm_chunk_simulation"].get("source"),
        "anythingllm_embedding_engine": profile["anythingllm_embedding_config"].get("engine"),
        "anythingllm_embedding_model": profile["anythingllm_embedding_config"].get("model"),
        "anythingllm_embedding_effective_model_source": profile["anythingllm_embedding_config"].get("effective_model_source"),
        "anythingllm_embedding_generic_model": profile["anythingllm_embedding_config"].get("generic_model"),
        "anythingllm_embedding_provider_support": profile["anythingllm_embedding_config"].get("provider_support"),
        "anythingllm_embedding_anomalies": profile["anythingllm_embedding_config"].get("anomalies", []),
        "anythingllm_embedding_conflicts": profile["anythingllm_embedding_config"].get("conflicting_model_preferences", []),
        "anythingllm_embedding_batch_size": profile["anythingllm_embedding_config"].get("batch_size"),
        "anythingllm_embedding_max_chunk_length": profile["anythingllm_embedding_config"].get("max_chunk_length"),
        "anythingllm_embedding_capability_status": profile["anythingllm_embedder_policy"].get("capability", {}).get("status"),
        "anythingllm_embedding_capability_limit_kind": profile["anythingllm_embedder_policy"].get("capability", {}).get("limit_kind"),
        "anythingllm_embedding_capability_source_note": profile["anythingllm_embedder_policy"].get("capability", {}).get("source_note"),
        "anythingllm_embedding_safe_max_chunk_length": profile["anythingllm_embedder_policy"].get("capability", {}).get("safe_max_chunk_length"),
        "anythingllm_embedding_recommended_limit": profile["anythingllm_embedder_policy"].get("recommended_limit"),
        "anythingllm_embedding_policy_status": profile["anythingllm_embedder_policy"].get("status"),
        "anythingllm_embedding_policy_action": profile["anythingllm_embedder_policy"].get("action"),
        "anythingllm_embedding_runtime_verification_status": profile["anythingllm_resolved_state"].get("validation", {}).get("status"),
        "anythingllm_embedding_runtime_verification_message": profile["anythingllm_resolved_state"].get("validation", {}).get("message"),
        "anythingllm_auto_correction_status": profile["anythingllm_auto_correction"].get("status"),
        "anythingllm_auto_correction_message": profile["anythingllm_auto_correction"].get("message"),
        "anythingllm_auto_correction_applied": profile["anythingllm_auto_correction"].get("auto_corrected"),
        "simulation_provider": selected.get("vector_provider", ""),
        "simulation_model": selected.get("vector_model", ""),
        "selected_region_embedding_coverage": "100%",
        "variant_outputs": selected_variants,
        "upload_file": str(prepared_text_path),
        "inline_metadata_fallback": (
            str(selected_dir / "anythingllm-upload-inline-metadata-fallback.txt")
            if (selected_dir / "anythingllm-upload-inline-metadata-fallback.txt").exists()
            else ""
        ),
        "manifest": str(selected_dir / "segment-manifest.jsonl"),
        "page_transition_manifest": str(selected_dir / "page-transition-manifest.jsonl"),
        "page_transition_boundaries_checked": len(transition_rows),
        "page_transition_companions_created": sum(
            1 for row in transition_rows if row.get("continuation_detected")
        ),
        "page_parent_manifest": str(selected_dir / "page-parent-manifest.jsonl"),
        "child_parent_map": str(selected_dir / "child-parent-map.csv"),
        "layout_region_review": str(selected_dir / "layout-region-review.json"),
        "retrieval_lane_review": str(selected_dir / "retrieval-lane-review.json"),
        "supplementary_lane_candidates": str(selected_dir / "supplementary-content-candidates.txt"),
        "provenance_review_manifest": str(provenance_review_manifest),
        "representation_comparison": str(selected_dir / "representation-comparison.csv"),
        "harmonization_report": str(selected_dir / "harmonization-report.csv"),
        "representation_recommendation": str(selected_dir / "representation-recommendation.csv"),
        "report": str(selected_dir / "readiness-report.html"),
        "variant_summary": str(selected_dir / "output-variant-summary.csv"),
        "metadata_payloads": str(metadata_dir / "raw-text-payloads-native-header.jsonl"),
        "page_parent_metadata_payloads": str(metadata_dir / "raw-text-payloads-page-parents-native-header.jsonl"),
        "page_parent_upload_plan": str(metadata_dir / "page-parent-upload-plan.csv"),
        "native_upload_representation": upload_representation,
        "native_upload_transport": upload_transport,
        "storage_inspection_status": storage_report.get("status"),
        "storage_workspace_document_count": workspace_layer_report.get("workspace_document_count", 0),
        "storage_raw_native_doc_count": workspace_layer_report.get("raw_native_doc_count", 0),
        "storage_embedded_chunk_count": workspace_layer_report.get("embedded_chunk_count", 0),
        "storage_page_segment_visibility": workspace_layer_report.get("page_segment_visibility", "not_checked"),
        "storage_sample_custom_document_title": sample_custom_document_title,
        "storage_sample_lancedb_title": sample_lancedb_title,
        "metadata_layer_visibility": str(inspection_dir / "metadata-layer-visibility.csv"),
        "column_explanations": str(inspection_dir / "column-explanations.csv"),
        "author_inference_evaluation_status": author_eval.get("status", "complete"),
        "author_inference_evaluation_csv": author_eval.get("csv", ""),
        "author_inference_evaluation_json": author_eval.get("json", ""),
        "author_inference_passed": author_eval.get("passed", 0),
        "author_inference_failed": author_eval.get("failed", 0),
        "api_upload_status": upload_report.get("status"),
        "api_uploaded": upload_report.get("uploaded", 0),
        "api_embedded": upload_report.get("embedded", 0),
        "api_embedding_update_requested": (upload_report.get("embedding_update") or {}).get("requested", 0),
        "api_embedding_update_accepted": (upload_report.get("embedding_update") or {}).get("accepted", 0),
        "api_embedding_queue_records": (upload_report.get("embedding_update") or {}).get("queue_records", 0),
        "api_embedding_progress_observation": (upload_report.get("embedding_update") or {}).get("progress_observation", {}),
        "api_embedding_update_batch_size": (upload_report.get("embedding_update") or {}).get("batch_size", 0),
        "api_embedding_verification_mode": (upload_report.get("embedding_update") or {}).get("verification_mode", ""),
        "api_embedding_verification_interval": (upload_report.get("embedding_update") or {}).get("verification_interval", 0),
        "api_embedding_deferred_verification_batches": (upload_report.get("embedding_update") or {}).get("deferred_verification_batches", []),
        "api_embedding_update_batches": (upload_report.get("embedding_update") or {}).get("batches", []),
        "api_embedding_batch_ledger": str(embedding_batch_ledger_path) if embedding_batch_ledger_path.exists() else "",
        "api_authentication_mode": upload_report.get("authentication_mode", "not_applicable"),
        "api_transport": upload_report.get("transport", upload_transport),
        "api_document_foldering_enabled": upload_report.get("document_foldering_enabled", False),
        "api_document_folder_name": upload_report.get("document_folder_name", ""),
        "api_document_folder_path": upload_report.get("document_folder_path", ""),
        "api_temporary_key_cleanup": upload_report.get("temporary_key_cleanup", {}).get(
            "status",
            "not_applicable",
        ),
        "api_temporary_key_cleanup_attempt_count": upload_report.get(
            "temporary_key_cleanup", {}
        ).get("attempt_count", 0),
        "api_temporary_key_cleanup_retry_attempted": bool(
            upload_report.get("temporary_key_cleanup", {}).get("retry_attempted")
        ),
        "cleanup_obligations": upload_report.get("cleanup_obligations", []),
        "api_upload_error": (
            str(
                ((upload_report.get("errors") or [{}])[0] or {}).get("error")
                or ((upload_report.get("errors") or [{}])[0] or {}).get("details")
                or ""
            ).strip()
        ),
        "api_upload_error_classification": str(
            ((upload_report.get("errors") or [{}])[0] or {}).get("classification") or ""
        ),
        "api_upload_warning": (
            str(
                ((upload_report.get("warnings") or [{}])[0] or {}).get("warning")
                or ""
            ).strip()
        ),
        "metadata_schema_status": metadata_schema_report.get("status"),
        "anythingllm_runtime_status": metadata_schema_report.get("runtime_api_status"),
        "native_metadata_rows": native_metadata_report.get("matching_rows", 0),
        "edge_case_status": edge_case_report.get("overall_status"),
        "edge_case_failures": edge_case_report.get("failures"),
        "edge_case_warnings": edge_case_report.get("warnings"),
        "native_test_kit": native_test_kit,
        "native_probe_kit": native_probe_kit,
        "workspace_model_gate_status": workspace_gate.get("status"),
        "workspace_model_gate_message": workspace_gate.get("message"),
        "post_upload_verification_status": post_upload_report.get("status"),
        "post_upload_classification": post_upload_report.get("classification"),
        "post_upload_matching_workspace_documents": post_upload_report.get(
            "matching_workspace_documents", 0
        ),
        "post_upload_desktop_drawer_layout": post_upload_report.get(
            "desktop_drawer_layout", "not_checked"
        ),
        "post_upload_desktop_drawer_root_locations": post_upload_report.get(
            "desktop_drawer_root_locations", 0
        ),
        "post_upload_desktop_drawer_nested_locations": post_upload_report.get(
            "desktop_drawer_nested_locations", 0
        ),
        "post_upload_chunk_survival_ratio": post_upload_report.get(
            "chunk_survival_ratio", 0.0
        ),
        "post_upload_location_files": post_upload_report.get("upload_location_existing_files", 0),
        "post_upload_location_matches": post_upload_report.get("upload_location_matching_files", 0),
        "post_upload_chain_local_expected_count": post_upload_report.get("upload_chain_local_expected_count", 0),
        "post_upload_chain_custom_documents_matching_count": post_upload_report.get("upload_chain_custom_documents_matching_count", 0),
        "post_upload_chain_lancedb_matching_count": post_upload_report.get("upload_chain_lancedb_matching_count", 0),
        "post_upload_matching_vectors": max(
            int(post_upload_report.get("matching_vector_rows") or 0),
            int(post_upload_report.get("lancedb_matching_rows") or 0),
        ),
        "post_upload_expected_payloads": int(post_upload_report.get("expected_payload_count") or 0),
        "post_upload_reconciliation_cap_classification": post_upload_report.get(
            "reconciliation_cap_classification", ""
        ),
        "post_upload_reconciliation_cap_message": post_upload_report.get(
            "reconciliation_cap_message", ""
        ),
        "anythingllm_runtime_validation_status": runtime_validation_report.get("status"),
        "anythingllm_runtime_embedder_probe_status": runtime_validation_report.get(
            "embedder_probe",
            {},
        ).get("status", ""),
        "anythingllm_runtime_embedder_probe_provider": runtime_validation_report.get(
            "embedder_probe",
            {},
        ).get("provider", ""),
        "anythingllm_runtime_embedder_probe_model": runtime_validation_report.get(
            "embedder_probe",
            {},
        ).get("model", ""),
        "anythingllm_runtime_embedder_probe_dimension": runtime_validation_report.get(
            "embedder_probe",
            {},
        ).get("dimension", 0),
        "anythingllm_runtime_vector_checks_passed": sum(
            1
            for row in runtime_validation_report.get("vector_checks", [])
            if row.get("top_1_expected")
        ),
        "anythingllm_runtime_vector_checks_total": len(
            runtime_validation_report.get("vector_checks", [])
        ),
        "anythingllm_runtime_validation_seconds": runtime_validation_report.get(
            "validation_seconds", 0.0
        ),
        "anythingllm_runtime_vector_search_seconds": runtime_validation_report.get(
            "vector_search_seconds", 0.0
        ),
        "anythingllm_runtime_chat_seconds": runtime_validation_report.get(
            "chat_seconds", 0.0
        ),
        "anythingllm_runtime_chat_probe_requested": bool(
            runtime_validation_report.get("chat_probe_requested")
        ),
        "anythingllm_runtime_chat_model": runtime_validation_report.get(
            "chat_check",
            {},
        ).get("configured_model", ""),
        "anythingllm_runtime_chat_error": runtime_validation_report.get(
            "chat_check",
            {},
        ).get("error", ""),
        "anythingllm_runtime_validation": str(
            inspection_dir / "anythingllm-runtime-validation.csv"
        ),
        "temporary_workspace_validation_status": temporary_workspace_validation.get("status"),
        "temporary_workspace_validation_workspace_slug": temporary_workspace_validation.get("workspace_slug"),
        "temporary_workspace_validation_post_upload_status": temporary_workspace_validation.get("post_upload_status"),
        "temporary_workspace_validation_runtime_status": temporary_workspace_validation.get("runtime_validation_status"),
        "temporary_workspace_validation_cleanup_policy": temporary_workspace_validation.get("cleanup_policy"),
        "temporary_workspace_validation_retention_status": temporary_workspace_validation.get("retention_status"),
        "temporary_workspace_validation_cleanup_status": (
            temporary_workspace_validation.get("cleanup_result", {}) or {}
        ).get("status", "not_run"),
        "temporary_workspace_validation_chunk_survival_flag": (
            temporary_workspace_validation.get("post_upload_report", {}) or {}
        ).get("chunk_survival_flag", ""),
        "temporary_workspace_validation_chunk_survival_ratio": (
            temporary_workspace_validation.get("post_upload_report", {}) or {}
        ).get("chunk_survival_ratio", 0.0),
        "temporary_workspace_validation_page_provenance_risk": (
            temporary_workspace_validation.get("post_upload_report", {}) or {}
        ).get("page_provenance_risk", ""),
        "temporary_workspace_validation_report": str(
            inspection_dir / "temporary-workspace-validation.csv"
        ),
        "unstructured_backend_resolution": profile["unstructured_runtime"].get(
            "backend_resolution", "not_checked"
        ),
        "unstructured_backend_resolution_source": profile["unstructured_runtime"].get(
            "backend_resolution_source", "unknown"
        ),
        "unstructured_backend_module_origin": profile["unstructured_runtime"].get(
            "backend_module_origin", ""
        ),
        "unstructured_optional_search_paths_enabled": profile["unstructured_runtime"].get(
            "optional_search_paths_enabled", True
        ),
        "edge_case_report": str(out_root / "edge-case-report.html"),
        "edge_case_results": str(out_root / "edge-case-results.csv"),
        "diagnostics_report": str(out_root / "diagnostics.html"),
        "diagnostics_csv": str(out_root / "diagnostics.csv"),
        "diagnostic_error_count": sum(1 for row in diagnostics if row["severity"] == "error"),
        "diagnostic_warning_count": sum(1 for row in diagnostics if row["severity"] == "warning"),
        "workspace_model_gate": str(inspection_dir / "workspace-model-gate.csv"),
        "post_upload_verification": str(inspection_dir / "post-upload-verification.csv"),
    }
    write_json(out_root / "run-summary.json", summary)
    if bool(getattr(args, "flat_output_without_logs", False)):
        summary["lean_retention"] = retain_successful_run_without_logs(
            out_root,
            summary,
            profile,
            prepared_text_path,
            segments=selected.get("segments") or (),
        )
    elif bool(getattr(args, "lean_retention", False)):
        summary["lean_retention"] = retain_successful_run_leanly(
            out_root,
            summary,
            profile,
            prepared_text_path,
            segments=selected.get("segments") or (),
        )
    if not partial_vector_coverage:
        report_upload_phase(
            "reporting",
            "Preparation complete",
            completed_units=1,
            total_units=1,
            fallback_fraction=1.0,
            evidence_kind="phase_completed",
        )
    return summary


def prepare_pdf(pdf_path: Path, out_root: Path, args):
    """Prepare one PDF through the stable compatibility boundary.

    The public API is intentionally thin. Runtime control, preflight, persisted
    checkpoints, and caller-specific presentation belong to ``orchestration``.
    ``_prepare_pdf_legacy_engine`` is the current implementation behind this
    boundary; its name preserves artifact/test compatibility during extraction
    of smaller modules and does not identify a deprecated execution path.
    """
    pdf_path = Path(pdf_path)
    out_root = Path(out_root)
    if pdf_path.suffix.casefold() != ".pdf":
        raise ValueError(f"Input is not a PDF: {pdf_path}")
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF input was not found: {pdf_path}")
    out_root.mkdir(parents=True, exist_ok=True)
    input_preflight = pdf_input_preflight(pdf_path)
    write_json(out_root / "pdf-input-preflight.json", input_preflight)
    if input_preflight["status"] != "pass":
        raise ValueError(input_preflight["message"])
    output_capacity = output_capacity_preflight(pdf_path, out_root)
    write_json(out_root / "output-capacity-preflight.json", output_capacity)
    if output_capacity["status"] != "pass":
        raise OSError(
            output_capacity["message"]
            + " Free space or choose a shorter output root, then rerun this PDF; existing payloads are retained."
        )
    return _prepare_pdf_legacy_engine(pdf_path, out_root, args)


def pdf_input_preflight(pdf_path: Path, sample_pages=8):
    """Validate one source cheaply before the expensive extraction cascade."""
    path = Path(pdf_path)
    result = {
        "status": "pass",
        "path": str(path),
        "readable": False,
        "encrypted": False,
        "page_count": 0,
        "sampled_pages": 0,
        "sampled_text_chars": 0,
        "mean_sampled_text_chars": 0.0,
        "likely_scan_or_image_pdf": False,
        "message": "PDF is readable and ready for staged extraction.",
    }
    try:
        if not path.is_file() or not os.access(path, os.R_OK):
            raise OSError("The PDF path is not readable.")
        with fitz.open(path) as document:
            result["readable"] = True
            result["encrypted"] = bool(document.needs_pass)
            if document.needs_pass:
                result["status"] = "password_required"
                result["message"] = "This PDF is encrypted or password-protected; provide an unlocked copy before processing."
                return result
            result["page_count"] = int(document.page_count or 0)
            if result["page_count"] <= 0:
                result["status"] = "invalid_pdf"
                result["message"] = "The PDF has no readable pages."
                return result
            indexes = sorted({round(index * (result["page_count"] - 1) / max(1, min(sample_pages, result["page_count"]) - 1)) for index in range(min(sample_pages, result["page_count"]))})
            chars = []
            for index in indexes:
                page_text = document.load_page(index).get_text("text")
                chars.append(len(str(page_text or "").strip()))
            result["sampled_pages"] = len(chars)
            result["sampled_text_chars"] = sum(chars)
            result["mean_sampled_text_chars"] = round(sum(chars) / max(1, len(chars)), 1)
            result["likely_scan_or_image_pdf"] = bool(chars and result["mean_sampled_text_chars"] < 80)
    except Exception as exc:
        result["status"] = "unreadable_pdf"
        result["message"] = f"PDF preflight could not open this file: {exc}"
    return result


def output_capacity_preflight(pdf_path: Path, out_root: Path, safety_reserve_bytes=256 * 1024 * 1024):
    """Check destination-volume capacity before creating a document package.

    The estimate deliberately reserves the original source size, a generous
    prepared/audit package allowance, a temporary-write copy, and a fixed
    local safety reserve.  It is a preflight guard, not a quota reservation;
    large artifact writes still recheck naturally through atomic finalization.
    """
    source_bytes = max(0, int(Path(pdf_path).stat().st_size))
    # Text can expand considerably for OCR, element JSON, and retained review
    # packs. Four source copies is conservative for ordinary PDFs while the
    # fixed reserve protects small source files on a nearly full disk.
    projected_artifacts = max(16 * 1024 * 1024, source_bytes * 2)
    temporary_write = max(8 * 1024 * 1024, projected_artifacts // 2)
    required = source_bytes + projected_artifacts + temporary_write + max(0, int(safety_reserve_bytes))
    usage = shutil.disk_usage(Path(out_root))
    status = "pass" if usage.free >= required else "insufficient_space"
    return {
        "status": status,
        "source_bytes": source_bytes,
        "projected_artifact_bytes": projected_artifacts,
        "temporary_write_bytes": temporary_write,
        "safety_reserve_bytes": int(safety_reserve_bytes),
        "required_free_bytes": required,
        "available_free_bytes": int(usage.free),
        "output_root": str(Path(out_root)),
        "message": (
            "Destination volume has sufficient free space for this PDF package."
            if status == "pass"
            else f"Insufficient free space at {out_root}: need {required:,} bytes, found {usage.free:,} bytes."
        ),
    }


def discover_pdfs(input_path: Path):
    if input_path.is_file() and input_path.suffix.lower() == ".pdf":
        return [input_path]
    if input_path.is_dir():
        return sorted(input_path.rglob("*.pdf"))
    raise FileNotFoundError(f"Input is not a PDF file or folder: {input_path}")


def main():
    parser = argparse.ArgumentParser(description="Automatically prepare PDFs for AnythingLLM.")
    parser.add_argument("--input", required=True, help="PDF file or folder containing PDFs.")
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--document-label", default="")
    parser.add_argument("--document-author", default="")
    parser.add_argument("--document-short-label", default="")
    parser.add_argument("--deep-extraction", action="store_true")
    parser.add_argument("--include-front-matter", action="store_true")
    parser.add_argument(
        "--backend-mode",
        choices=["automatic", "pymupdf", "pymupdf4llm", "unstructured"],
        default="automatic",
    )
    parser.add_argument("--first-page-override", type=int, default=0)
    parser.add_argument("--end-page-override", type=int, default=0)
    parser.add_argument(
        "--end-section-name",
        dest="end_section_names",
        action="append",
        default=None,
        help="End-matter heading to detect; repeat for multiple headings.",
    )
    parser.add_argument(
        "--validation-phrase",
        dest="validation_phrases",
        action="append",
        default=[],
        help="Additional exact phrase probe; repeat for multiple phrases.",
    )
    parser.add_argument(
        "--unstructured-strategy",
        choices=["auto", "fast", "hi_res", "ocr_only"],
        default="auto",
    )
    parser.add_argument("--target-passage-length", type=int, default=750)
    parser.add_argument(
        "--segment-mode",
        choices=["none", "passages", "page", "page_limit", "page_passages"],
        default="passages",
        help="`none` creates one prepared content record per PDF; AnythingLLM can still re-chunk it. `passages` pre-chunks near AnythingLLM ingestion. `page_limit` preserves each page until the active safety ceiling requires a split. `page_passages` creates shorter semantic passages without crossing a page boundary. `page` keeps one retrieval unit per included PDF page unless safety limits force subdivision.",
    )
    parser.add_argument(
        "--anythingllm-chunk-size",
        type=int,
        default=0,
        help="Override simulated AnythingLLM chunk size. 0 reads the local AnythingLLM setting.",
    )
    parser.add_argument(
        "--anythingllm-chunk-overlap",
        type=int,
        default=-1,
        help="Override simulated overlap. -1 reads the local AnythingLLM setting.",
    )
    parser.add_argument("--marker-style", choices=["short", "compact", "full"], default="short")
    parser.add_argument("--disable-inline-markers", action="store_true")
    parser.add_argument(
        "--exclude-supplementary-end-matter",
        action="store_true",
        help="Exclude only high-confidence sustained reference/index regions from the primary upload. By default all front/end matter remains searchable.",
    )
    parser.add_argument(
        "--retain-diagnostic-evidence",
        dest="lean_retention",
        action="store_false",
        default=True,
        help="Keep full candidates, metadata payloads, inspections, and reports even after a ready run.",
    )
    parser.add_argument("--run-vector-eval", action="store_true")
    parser.add_argument(
        "--run-author-inference-sample-evaluation",
        action="store_true",
        help="Run the separate author-inference regression sample suite and retain its diagnostic artifacts.",
    )
    parser.add_argument("--ollama-model", default="bge-m3:latest")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434/api/embed")
    parser.add_argument("--max-vector-probes", type=int, default=8)
    parser.add_argument("--max-vector-chunks", type=int, default=300)
    parser.add_argument("--prepare-and-upload", action="store_true")
    parser.add_argument("--anythingllm-api-url", default="")
    parser.add_argument("--anythingllm-api-key", default="")
    parser.add_argument("--workspace-slug", default="")
    parser.add_argument("--test-workspace-slug", default="test")
    parser.add_argument("--upload-limit", type=int, default=0, help="Native metadata upload limit. 0 means all segments.")
    parser.add_argument(
        "--upload-indices",
        nargs="*",
        type=int,
        default=[],
        help="Optional explicit 1-based prepared-record indices for native upload.",
    )
    parser.add_argument(
        "--native-metadata-upload-mode",
        choices=["native_header", "strict"],
        default="native_header",
    )
    parser.add_argument(
        "--native-upload-representation",
        choices=["segments", "page_parents"],
        default="segments",
    )
    parser.add_argument(
        "--native-upload-transport",
        choices=["raw_text", "file_upload"],
        default="raw_text",
    )
    parser.add_argument(
        "--anythingllm-create-document-folders",
        dest="anythingllm_create_document_folders",
        action="store_true",
        default=False,
        help="Advanced layout: upload under custom-documents/<PDF title>-<hash> subfolders. Desktop 1.15 may hide these from its Documents drawer.",
    )
    parser.add_argument(
        "--no-anythingllm-create-document-folders",
        dest="anythingllm_create_document_folders",
        action="store_false",
        help="Use the drawer-visible flat custom-documents upload folder (the default).",
    )
    parser.add_argument("--anythingllm-storage-dir", default="")
    parser.add_argument(
        "--run-chunk-survival-validation",
        action="store_true",
        help="For a controlled local-only acceptance run, create a temporary validation workspace and record native upload/retrieval evidence.",
    )
    parser.add_argument(
        "--temporary-validation-cleanup-policy",
        choices=["cleanup_always", "cleanup_on_success", "retain_for_review"],
        default="cleanup_always",
        help="Cleanup policy for the temporary validation workspace. The default removes it after evidence is captured.",
    )
    args = parser.parse_args()
    if getattr(args, "prepare_and_upload", False) or (args.anythingllm_api_url or "").strip():
        resolution = detect_anythingllm_api_url(
            args.anythingllm_api_url,
            api_key=args.anythingllm_api_key,
            timeout=1.25,
        )
        args.anythingllm_api_url = (
            resolution.get("api_url")
            or args.anythingllm_api_url
            or DEFAULT_ANYTHINGLLM_API_URL
        )
    if getattr(args, "prepare_and_upload", False):
        # The Gradio path performs a managed live preflight before entering the
        # common orchestration façade. Give the standalone CLI the same safety
        # contract instead of blocking every authenticated upload merely
        # because static compatibility evidence labels API mutations unknown.
        cli_auth = verify_anythingllm_upload_auth(
            args.anythingllm_api_url,
            api_key=args.anythingllm_api_key or None,
        )
        cli_embedder_probe = verify_anythingllm_runtime_embedder(
            args.anythingllm_api_url,
            api_key=args.anythingllm_api_key or None,
            storage_dir=Path(args.anythingllm_storage_dir) if args.anythingllm_storage_dir else None,
        )
        args.runtime_probe = cli_embedder_probe
        args.external_preflight_managed = bool(
            cli_auth.get("authenticated") and cli_embedder_probe.get("status") == "pass"
        )

    input_path = Path(args.input)
    default_out = application_paths()["automatic_outputs"]
    base_out = Path(args.out_dir) if args.out_dir else default_out
    run_stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_root = base_out / f"run-{run_stamp}"
    run_root.mkdir(parents=True, exist_ok=True)

    summaries = []
    for pdf in discover_pdfs(input_path):
        pdf_out = run_root / safe_stem(pdf.stem)
        run_result = execute_preparation(pdf, pdf_out, args, prepare_pdf)
        reporting_stage = run_result.stages.get("reporting")
        legacy_summary = (
            reporting_stage.evidence.get("legacy_summary", {})
            if run_result.status == "pass" and reporting_stage
            else {}
        )
        summaries.append(
            {
                **legacy_summary,
                "run_control": run_result.to_dict(),
            }
        )

    write_json(run_root / "batch-summary.json", summaries)
    print(json.dumps({"run_root": str(run_root), "documents": summaries}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

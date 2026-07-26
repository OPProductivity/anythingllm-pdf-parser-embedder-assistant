"""Local Gradio application for the AnythingLLM PDF preparation workflow.

This module is intentionally the user-interface composition root.  It owns the
Automatic and Advanced component trees, browser-only Gradio adaptations, UI
state transitions, locally persisted run history, and presentation helpers. It
does not own the canonical PDF preparation algorithm or the low-level
AnythingLLM transport contract; those live in ``auto_anythingllm_pipeline``
and the narrower support modules it imports.

The separation is not yet complete.  Some callbacks still translate UI values
into pipeline arguments here, so a developer changing a visible field must
trace its event chain and the corresponding argument construction before
assuming a control is display-only.  In particular, local preparation,
AnythingLLM mutation, passive observation, and guarded Desktop sidebar refresh
are deliberately different actions with different safety rules.
"""

import json
import html
import hashlib
import math
import os
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import fitz
import gradio as gr
from gradio.routes import App as GradioFastAPIApp
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from structured_logging import configure_structured_logger
from portable_paths import ensure_application_directories, package_resource_path
from embedder_capabilities import (
    PROVIDER_LABELS,
    openrouter_simulation_option_map,
    portable_catalog_entries,
    provider_catalog_counts,
    provider_catalog_entries,
)

from auto_anythingllm_pipeline import (
    ANYTHINGLLM_EMBEDDING_FAILURE_FALLBACK_CONCURRENT_BATCHES,
    ANYTHINGLLM_EMBEDDING_MAX_CONCURRENT_BATCHES,
    ANYTHINGLLM_EMBEDDING_SUBMISSION_STRATEGY,
    ANYTHINGLLM_EMBEDDING_UPDATE_BATCH_SIZE,
    ANYTHINGLLM_EMBEDDING_VERIFICATION_CHECKPOINT_INTERVAL,
    ANYTHINGLLM_RAW_TEXT_METADATA_FIELDS,
    ANYTHINGLLM_SOURCE_CONTRACT,
    apply_recommended_anythingllm_settings,
    anythingllm_desktop_process_running,
    anythingllm_storage_audit,
    anythingllm_stale_artifact_report,
    ensure_anythingllm_runtime,
    anythingllm_embedding_config,
    anythingllm_embedder_policy,
    anythingllm_resolved_state,
    build_ollama_simulation_adapter,
    build_openrouter_simulation_adapter,
    compatible_output_document_directory,
    create_validation_workspace,
    confirmed_submission_locations_from_ledger,
    create_temporary_desktop_api_key,
    delete_temporary_desktop_api_key,
    detect_anythingllm_api_url,
    default_short_label,
    describe_simulation_adapter,
    enrich_page_stats,
    extraction_quality,
    finalize_batch_inspection_context,
    get_anythingllm_metadata_schema,
    infer_author_from_pdf_text,
    is_local_anythingllm_url,
    is_lancedb_safe_namespace,
    lancedb_safe_workspace_name,
    native_identity_stem,
    observe_workspace_embedding_queue_activity,
    page_stats_for,
    pdf_metadata,
    planned_embedding_batch_count,
    prepare_pdf,
    persist_anythingllm_chunk_settings,
    persist_anythingllm_embedder_limit,
    persist_anythingllm_embedder_settings,
    provider_model_key_for_engine,
    project_local_env_path,
    remove_confirmed_workspace_queue_entries,
    restart_anythingllm_desktop,
    resolve_anythingllm_api_key,
    resolve_embedder_capability,
    resolve_default_simulation_adapter,
    safe_stem,
    sha256_file,
    simulation_app_config,
    simulation_preflight,
    verify_anythingllm_upload_auth,
    update_workspace_embeddings_batched,
    verify_anythingllm_post_upload,
    workspace_segment_preview,
    workspace_storage_inspector,
    write_failure_package,
)
from validation_contract import REVIEWABLE_POST_UPLOAD_STATUSES
from rag_pdf_tools import (
    DEFAULT_END_SECTION_HEADINGS,
    get_backend_pages,
    unstructured_runtime_status,
)
from orchestration import execute_preparation, legacy_summary_from_run

# Retain direct execution for test doubles and narrow developer probes.  A
# monkeypatched callable cannot cross a spawned Windows process, while normal
# user runs always retain these canonical imports and therefore use isolation.
CANONICAL_EXECUTE_PREPARATION = execute_preparation
CANONICAL_PREPARE_PDF = prepare_pdf


PORTABLE_APPLICATION_PATHS = ensure_application_directories()


APP_LOGGER = configure_structured_logger(
    "rag_pdf_gradio_app",
    PORTABLE_APPLICATION_PATHS["logs"] / "rag-pdf-app.jsonl",
)

# ``BASE_OUTPUT_DIR`` contains ordinary per-run output. ``AUTO_OUTPUT_DIR`` is
# an older but still active stable location for aggregate timing/history data.
# Do not merge the two casually: the former must be unique for every run while
# the latter deliberately survives across runs for generic model learning.
BASE_OUTPUT_DIR = PORTABLE_APPLICATION_PATHS["interactive_outputs"]
AUTO_OUTPUT_DIR = PORTABLE_APPLICATION_PATHS["automatic_outputs"]
ADVANCED_DIAGNOSTICS_OUTPUT_DIR = BASE_OUTPUT_DIR / "advanced-diagnostics"
INGESTION_HISTORY_PATH = AUTO_OUTPUT_DIR / "ingestion-history.jsonl"
AUTOMATIC_RECOVERY_HISTORY_PATH = AUTO_OUTPUT_DIR / "automatic-recovery-history.jsonl"
TIMING_MODEL_DIR = AUTO_OUTPUT_DIR / "timing-model"
TIMING_MODEL_RUNS_PATH = TIMING_MODEL_DIR / "timing-runs.jsonl"
TIMING_MODEL_EVENTS_PATH = TIMING_MODEL_DIR / "timing-events.jsonl"
DESKTOP_REFRESH_EVENTS_PATH = TIMING_MODEL_DIR / "desktop-refresh-events.jsonl"
TIMING_MODEL_SUMMARY_PATH = TIMING_MODEL_DIR / "timing-model-summary.json"
# Version 2 distinguishes a single Desktop-style workspace queue from the
# retired client-side two-record batch scheduler.  Version-1 rows remain in
# the append-only log for audit, but must not teach a queue-shaped ETA that a
# 19-record upload means ten independent HTTP requests.
TIMING_MODEL_VERSION = 2
BACKGROUND_LOG_RETENTION_DAYS = 365
REPEAT_RUN_SETTINGS_SCHEMA_VERSION = 1
USER_DOWNLOADS_DIR = PORTABLE_APPLICATION_PATHS["downloads"]
GRADIO_DOWNLOAD_CACHE_DIR = Path(tempfile.gettempdir()) / "anythingllm-pdf-prep-downloads"
# Folder selection is an interactive UI operation, not a bulk-ingestion API.
# Keep its browser payload and preflight work bounded; a user can select a
# narrower subfolder when a library exceeds this deliberately generous limit.
BATCH_FOLDER_MAX_DOCUMENTS = 750
BATCH_FOLDER_VISIBLE_FILE_LIMIT = 60
BATCH_FOLDER_INITIAL_PROFILE_DOCUMENT_LIMIT = 12
BATCH_FOLDER_FULL_PROFILE_DOCUMENT_LIMIT = 24
BATCH_FOLDER_SCAN_PROGRESS_INTERVAL = 128
APP_VERSION = package_resource_path("VERSION").read_text(encoding="utf-8").strip()
# Last committed base checkpoint for the 0.5.0 release line.
APP_BASE_COMMIT = "portable-package"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
SIMULATION_ANYTHINGLLM_DEFAULT_PREFIX = "Default AnythingLLM embedder"
SIMULATION_ANYTHINGLLM_DEFAULT_LABEL = SIMULATION_ANYTHINGLLM_DEFAULT_PREFIX
SIMULATION_SKIP_LABEL = "None"
MODE_LOCAL_ONLY_LABEL = "Create local files only"
MODE_LOCAL_NO_LOGS_LABEL = "Create local files without logs"
MODE_NATIVE_UPLOAD_LABEL = "Create local files and upload to AnythingLLM"
NEW_DOCUMENT_WORKSPACE_VALUE = "__new_workspace_for_document__"
NEW_DOCUMENT_WORKSPACE_LABEL = "New workspace for this document"
NATIVE_UPLOAD_SCOPE_ALL_LABEL = "All segments"
NATIVE_UPLOAD_SCOPE_CUSTOM_LABEL = "Custom range"
# Kept only to read older saved settings and timing rows. It is no longer a
# selectable production scope: partial uploads must be explicit and legible.
NATIVE_UPLOAD_SCOPE_PROBE_LABEL = "Two test segments"
NATIVE_BOUNDARY_CURRENT_LABEL = "Use current AnythingLLM re-chunking"
NATIVE_BOUNDARY_PASSAGES_LABEL = "Passage records with zero additional overlap"
NATIVE_BOUNDARY_PAGE_LIMIT_LABEL = "Page-bounded records with zero overlap"
NATIVE_BOUNDARY_WHOLE_PAGE_LABEL = "Whole-page records with zero overlap"
DEFAULT_ANYTHINGLLM_API_URL = "http://127.0.0.1:3001"
SEGMENT_PASSAGES_LABEL = "AnythingLLM-parity subchunking"
SEGMENT_PAGE_ONLY_LABEL = "Whole-page chunks"
SEGMENT_PAGE_LIMIT_LABEL = "Page - preserve automatically"
SEGMENT_PAGE_PASSAGES_LABEL = "Shorter page-local passages"
LEGACY_SEGMENT_PAGE_LIMIT_LABEL = "Page-bounded subchunking"
SEGMENT_NONE_LABEL = "All in one file"
ADVANCED_BACKEND_AUTOMATIC_LABEL = "Automatic"


def is_page_preserving_segment_mode(value):
    """Return whether a UI label selects whole-page-first preservation.

    Accept the earlier saved labels as well as the current wording so an old
    browser session cannot silently change a run's segmentation policy.
    """
    normalized = str(value or "").casefold()
    return (
        normalized == SEGMENT_PAGE_LIMIT_LABEL.casefold()
        or "page-preserving" in normalized
        or "page-bounded" in normalized
    )
ADVANCED_BACKEND_TESSERACT_LABEL = "Unstructured OCR (Tesseract)"
ADVANCED_BACKEND_CHOICES = [
    ADVANCED_BACKEND_AUTOMATIC_LABEL,
    "PyMuPDF",
    "PyMuPDF4LLM",
    "Unstructured",
    ADVANCED_BACKEND_TESSERACT_LABEL,
]
# This is a named historical preset retained for comparison and deliberate
# operator experiments. It is not a claim that 768/128 is the current
# universal optimum; the staged benchmark owns promotion of a new default.
TESTED_RETRIEVAL_CHUNK_SIZE = 768
TESTED_RETRIEVAL_CHUNK_OVERLAP = 128
CHUNK_SIZE_PRESET_CHOICES = ["512", "640", "768", "832", "1024"]
CHUNK_OVERLAP_PRESET_CHOICES = ["0", "32", "64", "75", "96", "128"]
TARGET_PASSAGE_LENGTH_PRESET_CHOICES = ["300", "450", "600", "750", "768", "832", "900", "1024", "1200"]
TARGET_PASSAGE_INHERIT_LABEL = "Inherit recommended target for segmentation mode"
TARGET_PASSAGE_CUSTOM_LABEL = "Use a custom target passage length"
DEFAULT_TARGET_PASSAGE_LENGTH = 750
EMBEDDING_OBSERVER_QUIET_SECONDS = 60
# This timer only reads local AnythingLLM state. It never starts the desktop app,
# creates an API key, mutates a workspace, or retries an embedding job.
BACKGROUND_RECONCILIATION_INTERVAL_SECONDS = 5
# This is a passive page-level health signal, intentionally slower than the
# run-status clock. It makes a stopped Desktop visible in an already-open
# browser tab but never starts Desktop, creates a key, or changes a workspace.
ANYTHINGLLM_STARTUP_STATUS_INTERVAL_SECONDS = 10
# This descriptor exists only while the separately-installed guarded Desktop
# bridge is running. The timer reads it for status but never calls the bridge.
DESKTOP_REFRESH_BRIDGE_FILENAME = "anythingllm-pdf-prep-refresh-bridge.json"
DESKTOP_REFRESH_BRIDGE_MARKER = "anythingllm-pdf-prep-refresh-bridge-v1"
DESKTOP_REFRESH_BRIDGE_CURRENT_REVISION = "drawer-audit-v2"
# A 32-byte Node ``base64url`` token is exactly 43 URL-safe characters.  Do
# not treat arbitrary descriptor text as an HTTP capability credential.
DESKTOP_REFRESH_BRIDGE_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
# The bridge must expose this fail-closed selector version before the localhost
# app is allowed to ask it to reload Desktop. Older descriptors are visible for
# diagnosis but cannot discard an unsent chat draft.
DESKTOP_REFRESH_BRIDGE_REQUIRED_DRAFT_GUARD_VERSION = 2
# Passage lengths and AnythingLLM's text splitter are measured in characters.
# Embedder capability limits are tokens, so this deliberately conservative
# estimate is only used to surface an early warning, never as a hard conversion.
CONSERVATIVE_CHARS_PER_EMBEDDING_TOKEN = 3
AUTOMATIC_RUN_FIELDS = (
    "pdf_files", "folder_pdf_files", "document_label", "document_author", "document_short_label",
    "use_file_title_fallback", "mode", "output_root_override", "api_url", "api_key", "workspace_slug",
    "native_upload_scope", "native_upload_custom_range", "native_metadata_mode", "anythingllm_create_document_folders",
    "anythingllm_document_folder_name", "local_check_mode", "custom_ollama_model", "ollama_url",
    "vector_audit_scope", "deep_extraction", "include_front_matter", "include_back_matter", "backend_mode",
    "first_page_override", "end_page_override", "target_passage_length", "page_preserve_ceiling", "segment_mode",
    "advanced_end_section_names", "automatic_validation_phrases", "unstructured_strategy",
    "generate_inline_fallback", "inherit_anythingllm_settings", "anythingllm_chunk_size",
    "anythingllm_chunk_overlap", "auto_apply_recommended_settings", "download_full_folder",
    "download_segments_folder",
)
ANYTHINGLLM_EMBEDDER_ENGINE_CHOICES = [
    "anythingllm",
    "openrouter",
    "openai",
    "ollama",
    "generic-openai",
    "gemini",
    "mistral",
    "cohere",
    "voyage",
    "jinaai",
    "azure-openai",
    "litellm",
    "lmstudio",
    "localai",
    "lemonade",
]
APP_ICON = package_resource_path("assets/anythingllm-pdf-assistant-start.ico")
APP_THEME = gr.themes.Soft(primary_hue="blue", neutral_hue="slate")
LAST_SIMULATION_DIAGNOSTICS = {}
LAST_TIMING_ESTIMATE = {}
# The run worker and the compact UI status poller share only this small
# read-mostly record. The durable copy lives beside the generated artifacts so
# an error can always be inspected after the browser event has ended.
LIVE_AUTOMATIC_RUN_STATUS = {}
# This is deliberately a short-lived UI cache, not a timing-model input.  It
# avoids re-reading a selected PDF for every settings change solely to render
# the repeat-run notice.  The cache is invalidated when the path's size or
# modification time changes and is never persisted.
# Each expensive one-document pipeline runs in an owned child process.  Cancel
# first records durable intent, then terminates that child and its descendants.
# An AnythingLLM request already accepted by Desktop can still have an unknown
# remote outcome, so cancellation records recovery evidence rather than making
# a false rollback claim.
CANCELLED_AUTOMATIC_RUN_ROOTS = set()
AUTOMATIC_RUN_CANCELLATION_MARKER = ".cancel-requested.json"
AUTOMATIC_RUN_WORKER_MARKER = ".active-preparation-worker.json"
AUTOMATIC_RUN_CANCELLATION_RECOVERY = "cancellation-recovery.json"
AUTOMATIC_RUN_RECOVERY_STATE = "automatic-recovery-state.json"
AUTOMATIC_RUN_RECOVERY_ATTEMPT = "automatic-recovery-attempt.json"
AUTOMATIC_RUN_RUNTIME_GUARD = "runtime-guard.json"
AUTOMATIC_RUN_RUNTIME_RECOVERY = "runtime-recovery.json"
AUTOMATIC_RUN_RUNTIME_PREFLIGHT = "anythingllm-runtime-preflight.json"
AUTOMATIC_RUN_RUNTIME_EVENTS = "runtime-events.jsonl"
# Recovery observes before doing anything remotely. These are intentionally
# bounded so a Cancel action cannot become a long-running queue client.
AUTOMATIC_RECOVERY_OBSERVATION_SECONDS = 20.0
AUTOMATIC_RECOVERY_GRACE_SECONDS = 15.0
AUTOMATIC_RECOVERY_CLEANUP_TIMEOUT_SECONDS = 20.0
AUTOMATIC_RUNTIME_GUARD_INTERVAL_SECONDS = 5.0
# A failed health probe is deliberately confirmed before recovery, but the
# confirmation must not make a stopped Desktop invisible for another full
# monitoring interval.
AUTOMATIC_RUNTIME_GUARD_RECHECK_SECONDS = 2.0
AUTOMATIC_RUNTIME_GUARD_FAILURE_THRESHOLD = 2
# The benchmark measured run-wide readiness at roughly 0.2--0.4% for the
# medium-PDF cohort. Reserve only 0.5% for it. Pipeline evidence then owns
# 0.5--97%; the final 3% is durable report/download handoff.
AUTOMATIC_RUN_PREFLIGHT_DISPLAY_END = 0.005
AUTOMATIC_RUN_TERMINAL_DISPLAY_START = 0.97
AUTOMATIC_RUN_DOCUMENT_DISPLAY_SPAN = (
    AUTOMATIC_RUN_TERMINAL_DISPLAY_START - AUTOMATIC_RUN_PREFLIGHT_DISPLAY_END
)
# Legacy, unstructured callbacks still use the former generic pipeline
# fractions. Structured callbacks use ``AUTOMATIC_UPLOAD_PHASE_RANGES``
# directly, so this compatibility mapper cannot alter normal upload progress.
# Values returned here are source values inside the app's 0.5--97% document
# allocation. Structured events carry the authoritative phase mapping; this
# legacy route exists only so old callbacks cannot jump ahead of it.
AUTOMATIC_UPLOAD_PREPARATION_SOURCE_END = 0.80
AUTOMATIC_UPLOAD_PREPARATION_DISPLAY_END = 0.1600
AUTOMATIC_UPLOAD_VECTOR_SOURCE_END = 0.94
AUTOMATIC_UPLOAD_VECTOR_DISPLAY_END = 0.7800
AUTOMATIC_UPLOAD_VALIDATION_SOURCE_END = 0.97
AUTOMATIC_UPLOAD_VALIDATION_DISPLAY_END = 0.9800


def reweight_automatic_upload_progress(value):
    """Map only a legacy unstructured progress value to the current slots."""
    try:
        source = float(value)
    except (TypeError, ValueError):
        source = 0.0
    source = max(0.0, min(1.0, source))
    if source <= AUTOMATIC_UPLOAD_PREPARATION_SOURCE_END:
        return (
            source
            / AUTOMATIC_UPLOAD_PREPARATION_SOURCE_END
            * AUTOMATIC_UPLOAD_PREPARATION_DISPLAY_END
        )
    if source <= AUTOMATIC_UPLOAD_VECTOR_SOURCE_END:
        vector_source_span = (
            AUTOMATIC_UPLOAD_VECTOR_SOURCE_END
            - AUTOMATIC_UPLOAD_PREPARATION_SOURCE_END
        )
        vector_display_span = (
            AUTOMATIC_UPLOAD_VECTOR_DISPLAY_END
            - AUTOMATIC_UPLOAD_PREPARATION_DISPLAY_END
        )
        return AUTOMATIC_UPLOAD_PREPARATION_DISPLAY_END + (
            (source - AUTOMATIC_UPLOAD_PREPARATION_SOURCE_END)
            / vector_source_span
            * vector_display_span
        )
    if source <= AUTOMATIC_UPLOAD_VALIDATION_SOURCE_END:
        return AUTOMATIC_UPLOAD_VECTOR_DISPLAY_END + (
            (source - AUTOMATIC_UPLOAD_VECTOR_SOURCE_END)
            / (AUTOMATIC_UPLOAD_VALIDATION_SOURCE_END - AUTOMATIC_UPLOAD_VECTOR_SOURCE_END)
            * (AUTOMATIC_UPLOAD_VALIDATION_DISPLAY_END - AUTOMATIC_UPLOAD_VECTOR_DISPLAY_END)
        )
    return AUTOMATIC_UPLOAD_VALIDATION_DISPLAY_END
# Desktop's splash can reach 100% before the local API and worker processes are
# usable. Recovery waits for the API itself rather than treating the window as
# ready; cold starts observed on this machine can exceed the general 45-second
# startup default, so retain one bounded three-minute app-owned attempt.
AUTOMATIC_RUNTIME_RECOVERY_STARTUP_TIMEOUT_SECONDS = 180.0
AUTOMATIC_RUNTIME_RECOVERY_STARTUP_POLL_INTERVAL_SECONDS = 10.0
# The long interval is for a genuinely slow startup. Immediately after a
# launch, probe rapidly so a ready Desktop is not left idle until the next
# ten-second tick.
AUTOMATIC_RUNTIME_RECOVERY_STARTUP_FAST_POLL_INTERVAL_SECONDS = 1.0
AUTOMATIC_RUNTIME_RECOVERY_STARTUP_FAST_POLL_WINDOW_SECONDS = 45.0
# A marker survives a server crash for recovery, but a Windows PID can later be
# reused by an unrelated process. Keep the live Popen handle in this process as
# the authority for a forced taskkill; the durable marker remains useful for
# cooperative cancellation and recovery, never as a licence to kill a reused
# PID after a restart.
ACTIVE_AUTOMATIC_RUN_WORKER_PROCESSES = {}
ACTIVE_AUTOMATIC_RECOVERY_THREADS = {}
# A cross-process worker proves liveness by refreshing run-progress.json.
# Older records remain recovery evidence, but must never turn a pre-start
# Cancel click into a stop request against an unrelated historical run.
AUTOMATIC_RUN_PROGRESS_STALE_SECONDS = 180
# Gradio 6 passes ``head`` content through its frontend configuration.  Script
# tags in that configuration are rendered as DOM content, not parser-executed
# scripts, so a browser-only watchdog placed there never runs in Chrome.  The
# middleware below injects this small, self-contained script into the initial
# HTML document while it is still being parsed by the browser.
APP_CONNECTION_WATCHDOG_HEAD = """
<style id="rag-local-server-connection-watchdog-style">
  #rag-server-connection-banner {
    position: fixed !important;
    top: 16px !important;
    left: 50% !important;
    transform: translateX(-50%) !important;
    z-index: 10000 !important;
    box-sizing: border-box !important;
    display: flex !important;
    align-items: flex-start !important;
    gap: 10px !important;
    width: min(1000px, calc(100vw - 32px)) !important;
    margin: 0 !important;
    padding: 10px 14px !important;
    border: 1px solid #d97706 !important;
    border-radius: 8px !important;
    background: #fffbeb !important;
    color: #78350f !important;
    font: 600 14px/1.35 "Aptos", "Segoe UI", system-ui, sans-serif !important;
    box-shadow: 0 3px 12px rgba(120, 53, 15, 0.14) !important;
  }
  #rag-server-connection-banner[hidden] { display: none !important; }
  #rag-server-connection-banner.offline { border-color: #dc2626 !important; background: #fef2f2 !important; color: #991b1b !important; }
  #rag-server-connection-banner.reconnected { border-color: #047857 !important; background: #ecfdf5 !important; color: #065f46 !important; }
  #rag-server-connection-banner .rag-server-connection-message { min-width: 0 !important; flex: 1 1 auto !important; }
  #rag-server-connection-banner .rag-server-connection-dismiss {
    display: inline-grid !important;
    place-items: center !important;
    flex: 0 0 auto !important;
    width: 28px !important;
    min-width: 28px !important;
    height: 28px !important;
    min-height: 28px !important;
    margin: -4px -6px -4px 0 !important;
    padding: 0 !important;
    border: 0 !important;
    border-radius: 6px !important;
    background: transparent !important;
    color: currentColor !important;
    font: 700 22px/1 "Segoe UI", system-ui, sans-serif !important;
    line-height: 1 !important;
    cursor: pointer !important;
  }
  #rag-server-connection-banner .rag-server-connection-dismiss:hover,
  #rag-server-connection-banner .rag-server-connection-dismiss:focus-visible { background: color-mix(in srgb, currentColor 12%, transparent) !important; }
</style>
<script id="rag-local-server-connection-watchdog">
(() => {
  const install = () => {
    if (window.ragLocalServerConnectionWatchdogInstalled) return;
    window.ragLocalServerConnectionWatchdogInstalled = true;
    document.documentElement.dataset.ragLocalServerConnectionWatchdog = "installed";
    const ensureBanner = () => {
      let banner = document.getElementById("rag-server-connection-banner");
      if (banner) return banner;
      banner = document.createElement("section");
      banner.id = "rag-server-connection-banner";
      banner.className = "rag-server-connection-banner";
      banner.setAttribute("role", "alert");
      banner.setAttribute("aria-live", "assertive");
      const message = document.createElement("span");
      message.className = "rag-server-connection-message";
      banner.appendChild(message);
      const dismiss = document.createElement("button");
      dismiss.type = "button";
      dismiss.className = "rag-server-connection-dismiss";
      dismiss.setAttribute("aria-label", "Dismiss connection notice");
      dismiss.textContent = "×";
      dismiss.addEventListener("click", () => {
        window.clearTimeout(window.ragLocalServerConnectionDismissTimer);
        banner.hidden = true;
      });
      banner.appendChild(dismiss);
      banner.hidden = true;
      document.body.prepend(banner);
      return banner;
    };
    const render = (state) => {
      const banner = ensureBanner();
      const message = banner.querySelector(".rag-server-connection-message");
      window.clearTimeout(window.ragLocalServerConnectionDismissTimer);
      if (state === "offline") {
        banner.hidden = false;
        banner.className = "rag-server-connection-banner offline";
        message.innerHTML = "<strong>Connection to the PDF app was lost.</strong> The localhost server is not responding. Start or restart the PDF app server. Your existing AnythingLLM work is not changed by this warning.";
      } else if (state === "reconnected") {
        banner.hidden = false;
        banner.className = "rag-server-connection-banner reconnected";
        message.innerHTML = "<strong>Connection restored.</strong> The PDF app server is responding again.";
        window.ragLocalServerConnectionDismissTimer = window.setTimeout(() => {
          banner.hidden = true;
        }, 4000);
      } else {
        banner.hidden = true;
        message.textContent = "";
      }
    };
    // A native file chooser can still open after the localhost server has
    // stopped.  Do not let that browser-only selection look accepted: clear it
    // in the capture phase before Gradio's upload handler can render a row.
    // This covers both the ordinary PDF picker and the directory picker while
    // leaving already selected files alone.
    document.addEventListener("change", (event) => {
      const input = event.target;
      if (window.ragLocalServerConnectionState !== "offline"
          || !(input instanceof HTMLInputElement)
          || input.type !== "file") return;
      try { input.value = ""; } catch (_) {}
      event.preventDefault();
      event.stopImmediatePropagation();
      render("offline");
    }, true);
    let consecutiveConnectionFailures = 0;
    const poll = async () => {
      const prior = window.ragLocalServerConnectionState || "unknown";
      const controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), 2500);
      let reachable = false;
      try {
        const response = await fetch("/healthz", {
          method: "GET", cache: "no-store", signal: controller.signal,
          headers: { "Cache-Control": "no-cache" },
        });
        reachable = response.ok;
      } catch (_) {
        reachable = false;
      } finally {
        window.clearTimeout(timeout);
      }
      if (!reachable) {
        consecutiveConnectionFailures += 1;
        // A local restart can miss the first three five-second probes while
        // the server is coming back. Keep that brief recovery silent; only a
        // longer, sustained loss should show an offline/reconnected pair.
        if (consecutiveConnectionFailures >= 4) {
          if (prior !== "offline") render("offline");
          window.ragLocalServerConnectionState = "offline";
        }
      } else if (prior === "offline") {
        consecutiveConnectionFailures = 0;
        render("reconnected");
        window.ragLocalServerConnectionState = "reconnected";
      } else {
        consecutiveConnectionFailures = 0;
        window.ragLocalServerConnectionState = "online";
      }
    };
    poll();
    window.ragLocalServerConnectionTimer = window.setInterval(poll, 5000);
    window.addEventListener("online", poll);
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", install, { once: true });
  } else {
    install();
  }
})();
</script>
"""


class LocalServerConnectionWatchdogMiddleware(BaseHTTPMiddleware):
    """Place the localhost-loss watchdog in the parser-executed initial HTML.

    This middleware must be registered on the app *before* Gradio adds its
    Brotli middleware.  It then receives the uncompressed HTML response,
    injects the watchdog exactly once, and lets Gradio apply normal response
    compression afterwards.  API and asset responses are deliberately left
    untouched.
    """

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        content_type = response.headers.get("content-type", "")
        is_root_document = request.method == "GET" and request.url.path in {"", "/"}
        if (
            not is_root_document
            or response.status_code != 200
            or "text/html" not in content_type.lower()
        ):
            return response

        body = b"".join([chunk async for chunk in response.body_iterator])
        document = body.decode("utf-8", errors="replace")
        marker = 'id="rag-local-server-connection-watchdog"'
        if marker in document or "</head>" not in document.lower():
            return Response(
                content=body,
                status_code=response.status_code,
                headers={
                    key: value
                    for key, value in response.headers.items()
                    if key.lower() != "content-length"
                },
                media_type=response.media_type,
                background=response.background,
            )

        document = re.sub(
            r"</head>",
            f"{APP_CONNECTION_WATCHDOG_HEAD}</head>",
            document,
            count=1,
            flags=re.IGNORECASE,
        )
        return Response(
            content=document.encode("utf-8"),
            status_code=response.status_code,
            headers={
                key: value
                for key, value in response.headers.items()
                if key.lower() != "content-length"
            },
            media_type=response.media_type,
            background=response.background,
        )


async def local_pdf_app_healthz():
    """Minimal watchdog probe, independent of Gradio's heavier API schema."""
    return Response(status_code=204)


def gradio_server_app_with_connection_watchdog():
    """Create the app shell early enough for the HTML watchdog middleware."""

    app = GradioFastAPIApp()
    app.add_api_route("/healthz", local_pdf_app_healthz, methods=["GET"], include_in_schema=False)
    app.add_middleware(LocalServerConnectionWatchdogMiddleware)
    return app


APP_JS = """
() => {
  // This JavaScript is a narrow rendering adapter for unstable Gradio DOM
  // details. It must not become a second business-logic layer: durable run
  // state, validation, upload, and cancellation decisions stay in Python.
  //
  // The local-server connection watchdog is injected into the parser-executed
  // initial HTML by LocalServerConnectionWatchdogMiddleware. Gradio renders
  // this configuration JavaScript as DOM content, so keeping a duplicate copy
  // here would make the two paths drift and would not fix a stopped server.
  const isDarkTheme = () => {
    return document.body.classList.contains("dark")
      || document.documentElement.classList.contains("dark")
      || document.querySelector("gradio-app")?.classList.contains("dark");
  };

  const applyThemeAwareControlStyles = () => {
    const dark = isDarkTheme();
    const setButtonVisual = (button, palette) => {
      if (!button) return;
      button.style.borderColor = palette.border;
      button.style.background = palette.background;
      button.style.color = palette.color;
      button.style.boxShadow = "none";
    };

    const subtleButton = dark
      ? { border: "#64748b", background: "#111827", color: "#e5eefc" }
      : { border: "#94a3b8", background: "transparent", color: "#334155" };
    const downloadToggle = dark
      ? { border: "#60a5fa", background: "#172033", color: "#f8fafc" }
      : { border: "#93c5fd", background: "#dbeafe", color: "#0f172a" };

    setButtonVisual(document.getElementById("expand-all-accordions-button"), subtleButton);

    const choosePdfFolderRoot = document.getElementById("choose-pdf-folder-button");
    if (choosePdfFolderRoot) {
      choosePdfFolderRoot.style.position = "absolute";
      choosePdfFolderRoot.style.inset = "0";
      choosePdfFolderRoot.style.minHeight = "100%";
      choosePdfFolderRoot.style.height = "auto";
      choosePdfFolderRoot.style.display = "flex";
      choosePdfFolderRoot.style.alignItems = "center";
      choosePdfFolderRoot.style.justifyContent = "center";
      choosePdfFolderRoot.style.padding = "0 16px";
      choosePdfFolderRoot.style.zIndex = "1";
      for (const node of choosePdfFolderRoot.querySelectorAll("button, .wrap, .form")) {
        node.style.minHeight = "100%";
        node.style.height = "100%";
        node.style.display = "flex";
        node.style.alignItems = "center";
        node.style.justifyContent = "center";
      }
    }

    for (const title of document.querySelectorAll(".batch-folder-title")) {
      const referenceTag = document.querySelector(".pdf-upload-input label[data-testid='block-label']");
      const referenceStyle = referenceTag ? getComputedStyle(referenceTag) : null;
      title.style.background = referenceStyle?.backgroundColor || "#dbeafe";
      title.style.color = referenceStyle?.color || "#3b82f6";
      title.style.border = "0";
      title.style.boxShadow = "none";
      title.style.marginLeft = "0";
      title.style.cursor = "pointer";
    }

    for (const icon of document.querySelectorAll(".batch-folder-title-icon")) {
      const referenceTag = document.querySelector(".pdf-upload-input label[data-testid='block-label']");
      const referenceStyle = referenceTag ? getComputedStyle(referenceTag) : null;
      icon.style.color = referenceStyle?.color || "#3b82f6";
    }

    for (const label of document.querySelectorAll(".download-folder-control label")) {
      label.style.borderColor = downloadToggle.border;
      label.style.background = downloadToggle.background;
      label.style.color = downloadToggle.color;
      label.style.boxShadow = "none";
    }

    for (const span of document.querySelectorAll(".download-folder-control span")) {
      span.style.color = downloadToggle.color;
    }

    for (const title of document.querySelectorAll(".downloads-header-title")) {
      title.style.background = "transparent";
      title.style.color = dark ? "#f8fafc" : "var(--body-text-color)";
      title.style.padding = "0";
      title.style.borderRadius = "0";
      title.style.display = "inline-flex";
      title.style.alignItems = "center";
      title.style.justifyContent = "flex-start";
      title.style.fontWeight = "700";
    }
  };

  const systemThemeQuery = window.matchMedia("(prefers-color-scheme: dark)");
  const themeFollowSystemKey = "rag-pdf-follow-system-theme";
  const themeOverrideKey = "rag-pdf-theme";
  const followsSystemTheme = () => {
    try { return localStorage.getItem(themeFollowSystemKey) !== "false"; } catch (_) { return true; }
  };
  const applyTheme = (dark) => {
    document.documentElement.classList.toggle("dark", dark);
    document.body.classList.toggle("dark", dark);
    document.querySelector("gradio-app")?.classList.toggle("dark", dark);
    requestAnimationFrame(applyThemeAwareControlStyles);
  };
  const applySystemTheme = () => {
    if (followsSystemTheme()) applyTheme(systemThemeQuery.matches);
  };
  const syncFollowSystemControl = () => {
    const checkbox = document.querySelector("#follow-windows-theme input[type='checkbox']");
    if (!checkbox) return;
    checkbox.checked = followsSystemTheme();
    if (checkbox.dataset.ragThemeWired) return;
    checkbox.dataset.ragThemeWired = "true";
    checkbox.addEventListener("change", () => {
      setFollowSystem(checkbox.checked);
    });
  };
  const setFollowSystem = (followSystem) => {
    try { localStorage.setItem(themeFollowSystemKey, followSystem ? "true" : "false"); } catch (_) {}
    if (followSystem) applyTheme(systemThemeQuery.matches);
    syncFollowSystemControl();
  };
  // Old query parameters are one-time legacy overrides. Remove them so a
  // current Windows preference, or the explicit override controls, wins.
  const initialUrl = new URL(window.location.href);
  if (initialUrl.searchParams.has("__theme")) {
    initialUrl.searchParams.delete("__theme");
    window.history.replaceState({}, "", initialUrl.toString());
  }
  systemThemeQuery.addEventListener("change", applySystemTheme);
  if (followsSystemTheme()) {
    applySystemTheme();
  } else {
    let storedOverride = null;
    try { storedOverride = localStorage.getItem(themeOverrideKey); } catch (_) {}
    applyTheme(storedOverride === "dark");
  }
  window.setTimeout(() => {
    applySystemTheme();
    syncFollowSystemControl();
  }, 0);
  const simulationRefreshCooldownMs = 1500;

  // Gradio 6 separates a selected file's filename/size table from its
  // upper-right action container. Add an explicit replacement button to that
  // real top-right container instead of guessing that the file row contains
  // two actions. The native clear/X control remains beside it.
  const selectedPdfUploadGlyph = `
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M7.5 18.5h10a4 4 0 0 0 .55-7.96A6.25 6.25 0 0 0 6.1 9.3 4.6 4.6 0 0 0 7.5 18.5Z"></path>
      <path d="M12 16V8"></path>
      <path d="m8.8 11.2 3.2-3.2 3.2 3.2"></path>
    </svg>`;
  const decorateSelectedPdfActions = () => {
    for (const uploadRoot of document.querySelectorAll(".pdf-upload-input")) {
      const filePreview = uploadRoot.querySelector(".file-preview");
      const actionHost = uploadRoot.querySelector(".icon-button-wrapper.top-panel");
      if (!filePreview || !actionHost || actionHost.querySelector(".rag-selected-file-replace")) continue;
      const clearButton = [...actionHost.querySelectorAll("button")].find((button) =>
        !button.classList.contains("rag-selected-file-replace")
      );
      if (!clearButton) continue;
      const replaceButton = document.createElement("button");
      replaceButton.type = "button";
      replaceButton.className = "rag-selected-file-replace";
      replaceButton.setAttribute("aria-label", "Replace selected PDF");
      replaceButton.setAttribute("title", "Replace selected PDF");
      replaceButton.innerHTML = selectedPdfUploadGlyph;
      replaceButton.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        clearButton.click();
        // Clearing re-renders the normal uploader synchronously in Gradio.
        // Try the next frames as well so slower page updates still open its
        // native file picker without leaving the app in an ambiguous state.
        let attempts = 0;
        const openNativePicker = () => {
          const uploadButton = uploadRoot.querySelector('button[aria-label="Click to upload or drop files"]');
          if (uploadButton) {
            uploadButton.click();
            return;
          }
          attempts += 1;
          if (attempts < 4) requestAnimationFrame(openNativePicker);
        };
        requestAnimationFrame(openNativePicker);
      });
      actionHost.insertBefore(replaceButton, clearButton);
    }
  };
  decorateSelectedPdfActions();
  const selectedPdfActionObserver = new MutationObserver(decorateSelectedPdfActions);
  selectedPdfActionObserver.observe(document.body, { childList: true, subtree: true });

  // Keep the ETA moving without using Gradio's event `js=` preprocessor.
  // That preprocessor can replace the confirmation State input when it does
  // not return a value, which must never be allowed to affect a real run.
  const startAutomaticRunTimer = (resetStart = false) => {
    const timer = document.getElementById("automatic-run-timing");
    if (!timer) return;
    // Active runs are server-ticked. This browser fallback is retained only
    // for the brief handoff after Confirm is clicked.
    if (timer.dataset.serverTimer === "true") return;
    if (window.ragAutomaticRunTimerInterval) {
      window.clearTimeout(window.ragAutomaticRunTimerInterval);
    }
    const serverStartedAt = Number(timer.dataset.startedEpoch || 0) * 1000;
    const expected = Number(timer.dataset.expectedSeconds || 0);
    const runKey = `${serverStartedAt}:${expected}`;
    if (resetStart || window.ragAutomaticRunTimerRunKey !== runKey || !window.ragAutomaticRunStartedAt) {
      // Prefer the durable server timestamp. It survives a Gradio HTML-node
      // replacement, whereas a click-local timestamp does not.
      window.ragAutomaticRunStartedAt = serverStartedAt > 0 ? serverStartedAt : Date.now();
      window.ragAutomaticRunTimerRunKey = runKey;
      window.ragAutomaticRunDisplayedRemaining = null;
    }
    const formatEstimate = (seconds) => {
      const value = Math.ceil(Number(seconds || 0));
      const sign = value < 0 ? "-" : "";
      const total = Math.abs(value);
      const hours = Math.floor(total / 3600);
      const minutes = Math.floor((total % 3600) / 60);
      const clock = `${String(minutes).padStart(2, "0")}m${String(total % 60).padStart(2, "0")}s`;
      return `${sign}${hours > 0 ? `${String(hours).padStart(2, "0")}h` : ""}${clock}`;
    };
    const render = () => {
      if (!timer.isConnected || (timer.dataset.runState && timer.dataset.runState !== "ready" && timer.dataset.runState !== "running")) {
        window.clearTimeout(window.ragAutomaticRunTimerInterval);
        window.ragAutomaticRunTimerInterval = 0;
        return;
      }
      const elapsedMs = Math.max(0, Date.now() - window.ragAutomaticRunStartedAt);
      const targetRemaining = expected - Math.floor(elapsedMs / 1000);
      if (!Number.isFinite(window.ragAutomaticRunDisplayedRemaining)) {
        window.ragAutomaticRunDisplayedRemaining = targetRemaining;
      }
      // Never skip a displayed second. If rendering was delayed, show each
      // missed second in quick succession until the clock catches up. A normal
      // second is still displayed for its full real-world second.
      if (window.ragAutomaticRunDisplayedRemaining > targetRemaining) {
        window.ragAutomaticRunDisplayedRemaining -= 1;
      } else if (window.ragAutomaticRunDisplayedRemaining < targetRemaining) {
        // A replacement run can have a later authoritative start timestamp.
        // Moving forward is the only safe correction in that exceptional case.
        window.ragAutomaticRunDisplayedRemaining = targetRemaining;
      }
      timer.dataset.runState = "running";
      timer.className = "automatic-run-timing running";
      timer.innerHTML = `<strong>Est: ${expected > 0 ? formatEstimate(window.ragAutomaticRunDisplayedRemaining) : "00m00s"}</strong>`;
      const catchUp = window.ragAutomaticRunDisplayedRemaining > targetRemaining;
      const untilNextSecond = Math.max(20, 1000 - (elapsedMs % 1000));
      window.ragAutomaticRunTimerInterval = window.setTimeout(render, catchUp ? 110 : untilNextSecond);
    };
    render();
  };
  const wireAutomaticRunTimer = () => {
    const host = document.getElementById("confirm-automatic-run-button");
    const button = host?.querySelector("button") || host;
    if (!button || button.dataset.ragRunTimerBound === "true") return;
    button.dataset.ragRunTimerBound = "true";
    button.addEventListener("click", () => startAutomaticRunTimer(true));
  };
  const syncAutomaticRunTimer = () => {
    const timer = document.getElementById("automatic-run-timing");
    if (!timer || timer.dataset.runState !== "running" || timer.dataset.serverTimer === "true") return;
    // Gradio can replace this node while a run streams. Rebind to the current
    // node without resetting the displayed-second queue.
    startAutomaticRunTimer(false);
  };
  const wireAutomaticRunCancellation = () => {
    const host = document.getElementById("cancel-automatic-run-button");
    const button = host?.querySelector("button") || host;
    if (!button || button.dataset.ragCancelTimerBound === "true") return;
    button.dataset.ragCancelTimerBound = "true";
    button.addEventListener("click", () => {
      // Only an active worker can be cancelled. Before Confirm,
      // Cancel is a harmless reset handled by the server; do not falsely show
      // an in-flight stop state in the browser before one exists.
      const activity = document.querySelector(
        ".automatic-run-activity[data-run-state='running'], .automatic-run-activity[data-run-state='preparing']"
      );
      if (!activity) return;
      // Give immediate feedback while the server records the stop and kills
      // the owned document-worker process tree.
      if (window.ragAutomaticRunTimerInterval) {
        window.clearTimeout(window.ragAutomaticRunTimerInterval);
        window.ragAutomaticRunTimerInterval = 0;
      }
      const timer = document.getElementById("automatic-run-timing");
      if (timer) {
        timer.dataset.runState = "cancelled";
        timer.className = "automatic-run-timing cancelled";
        timer.innerHTML = "<strong>Est: 00m00s</strong>";
      }
      button.textContent = "Stopping processing…";
      button.classList.add("rag-cancel-deferred");
      button.disabled = true;
    });
  };
  const syncAutomaticRunCancellation = () => {
    const activity = document.querySelector(
      ".automatic-run-activity[data-run-state='running'], .automatic-run-activity[data-run-state='preparing']"
    );
    const host = document.getElementById("cancel-automatic-run-button");
    const button = host?.querySelector("button") || host;
    if (!activity || !button) return;
    const requested = activity.dataset.cancelRequested === "true";
    const available = activity.dataset.cancelAvailable !== "false";
    button.classList.toggle("rag-cancel-deferred", requested);
    if (requested) {
      button.textContent = "Stopping processing…";
      button.disabled = true;
      button.title = "The stop request is saved; the active document worker is being terminated.";
      return;
    }
    // The active document pipeline is isolated in an owned child process, so
    // this remains available during OCR, extraction, upload, and verification.
    button.disabled = false;
    button.title = available
      ? "Stop this document run now"
      : "Stop request is available; the run is still entering its active worker.";
  };

  applyThemeAwareControlStyles();

  const wireDropdownAutoRefresh = (dropdownId, refreshHostId, stampKey) => {
    const dropdown = document.getElementById(dropdownId);
    const refreshHost = document.getElementById(refreshHostId);
    const refreshButton = refreshHost?.querySelector("button");
    if (!dropdown || !refreshButton || dropdown.dataset.ragRefreshBound === "true") return;
    dropdown.dataset.ragRefreshBound = "true";
    const triggerRefresh = () => {
      const now = Date.now();
      const previous = Number(dropdown.dataset[stampKey] || "0");
      if (now - previous < simulationRefreshCooldownMs) return;
      dropdown.dataset[stampKey] = String(now);
      refreshButton.click();
    };
    dropdown.addEventListener("pointerdown", triggerRefresh, true);
    dropdown.addEventListener("focusin", triggerRefresh, true);
  };

  const wireRefreshControls = () => {
    wireDropdownAutoRefresh(
      "simulation-model-dropdown",
      "simulation-model-auto-refresh",
      "ragSimulationLastRefreshTs"
    );
    wireDropdownAutoRefresh(
      "anythingllm-embedder-model-dropdown",
      "anythingllm-embedder-model-auto-refresh",
      "ragAnythingllmEmbedderLastRefreshTs"
    );
  };

  const wireExpandAllButton = () => {
    const expandControl = document.getElementById("expand-all-accordions-button");
    const button = expandControl?.matches("button")
      ? expandControl
      : expandControl?.querySelector("button");
    if (!button || button.dataset.ragExpandAllBound === "true") return;
    button.dataset.ragExpandAllBound = "true";
    button.addEventListener("click", () => {
      const expandPass = () => {
        const accordions = [
          ...document.querySelectorAll(".top-level-accordion"),
          ...document.querySelectorAll(".native-upload-subaccordion"),
        ];
        for (const accordion of accordions) {
          const toggle = accordion.querySelector(":scope > button.label-wrap");
          const content = accordion.querySelector(":scope > [data-testid='accordion-content']");
          const ariaExpanded = toggle?.getAttribute("aria-expanded");
          const contentHidden = content && getComputedStyle(content).display === "none";
          // Gradio 6 does not consistently emit aria-expanded. Its accordion
          // content visibility is the reliable state signal in that version.
          const isClosed = ariaExpanded === "false" || (ariaExpanded === null && contentHidden);
          if (toggle && isClosed) {
            toggle.click();
          }
        }
      };
      // Gradio can replace accordion nodes while earlier accordions expand.
      // Repeat after layout updates so every top-level toggle is reached.
      expandPass();
      requestAnimationFrame(expandPass);
      setTimeout(expandPass, 80);
      setTimeout(expandPass, 220);
    });
  };

  const wireBatchFolderPanel = () => {
    const panel = document.querySelector(".batch-folder-panel");
    if (!panel || panel.dataset.ragBatchPanelBound === "true") return;
    panel.dataset.ragBatchPanelBound = "true";
    panel.style.cursor = "pointer";
    panel.addEventListener("click", (event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      if (
        target.closest(".batch-folder-file-list") ||
        target.closest(".batch-folder-selection") ||
        target.closest(".batch-folder-status") ||
        target.closest(".batch-folder-inline-notice")
      ) {
        return;
      }
      const chooseHost = document.getElementById("choose-pdf-folder-button");
      const chooseButton = chooseHost?.querySelector("button") || chooseHost;
      if (!chooseButton || target.closest("#choose-pdf-folder-button")) return;
      chooseButton.click();
    });
  };

  // The compact completed-run field is intentionally a real native folder
  // picker, not a path the user has to discover and type. Keep the underlying
  // textbox so the latest-run buttons can still populate it and so a path can
  // be pasted with the keyboard when needed.
  const wireDiagnosticsFolderPicker = () => {
    if (!document.documentElement.dataset.ragDiagnosticsFolderPickerBound) {
      document.documentElement.dataset.ragDiagnosticsFolderPickerBound = "true";
      // Delegate from the document so this continues to work when Gradio
      // mounts the Advanced tab lazily or replaces its textbox after an event.
      document.addEventListener("pointerdown", (event) => {
        const target = event.target instanceof Element
          ? event.target.closest("#diagnostics-run-directory-input input, #diagnostics-run-directory-input textarea")
          : null;
        if (!target) return;
        const triggerHost = document.getElementById("choose-diagnostics-run-directory-button");
        const trigger = triggerHost?.querySelector("button") || triggerHost;
        if (!trigger) return;
        // Do not show an inert text caret for a click that opens Windows'
        // directory chooser. Keyboard focus still permits an explicit paste.
        event.preventDefault();
        trigger.click();
      }, true);
    }
    const field = document.getElementById("diagnostics-run-directory-input");
    const input = field?.querySelector("input, textarea");
    if (input) input.title = "Choose a completed run folder";
  };

  const updateAutomaticButtonState = () => {
    for (const hostId of ["automatic-process-button", "confirm-automatic-run-button"]) {
      const host = document.getElementById(hostId);
      const button = host?.querySelector("button") || host;
      if (!button) continue;
      const text = (button.textContent || "").toLowerCase();
      button.classList.toggle("rag-run-success", text.includes("successful"));
      button.classList.toggle("rag-run-warning", text.includes("review upload"));
      button.classList.toggle("rag-run-failed", text.includes("failed"));
      // Confirm is mounted but intentionally disabled before Review. Only the
      // accepted in-flight state should retain a bright primary appearance.
      button.classList.toggle("rag-run-processing", text.includes("processing"));
    }
  };

  wireRefreshControls();
  wireExpandAllButton();
  wireBatchFolderPanel();
  wireDiagnosticsFolderPicker();
  wireAutomaticRunTimer();
  syncAutomaticRunTimer();
  wireAutomaticRunCancellation();
  syncAutomaticRunCancellation();
  updateAutomaticButtonState();
  applyThemeAwareControlStyles();
  const refreshObserver = new MutationObserver(wireRefreshControls);
  refreshObserver.observe(document.body, { childList: true, subtree: true });
  const expandObserver = new MutationObserver(wireExpandAllButton);
  expandObserver.observe(document.body, { childList: true, subtree: true });
  const batchPanelObserver = new MutationObserver(wireBatchFolderPanel);
  batchPanelObserver.observe(document.body, { childList: true, subtree: true });
  const diagnosticsFolderPickerObserver = new MutationObserver(wireDiagnosticsFolderPicker);
  diagnosticsFolderPickerObserver.observe(document.body, { childList: true, subtree: true });
  const automaticRunTimerObserver = new MutationObserver(() => {
    wireAutomaticRunTimer();
    syncAutomaticRunTimer();
    wireAutomaticRunCancellation();
    syncAutomaticRunCancellation();
  });
  automaticRunTimerObserver.observe(document.body, { childList: true, subtree: true });
  const automaticButtonObserver = new MutationObserver(updateAutomaticButtonState);
  automaticButtonObserver.observe(document.body, { childList: true, subtree: true });
  const themeAwareObserver = new MutationObserver(() => {
    applyThemeAwareControlStyles();
    syncFollowSystemControl();
  });
  themeAwareObserver.observe(document.body, {
    attributes: true,
    childList: true,
    subtree: true,
    attributeFilter: ["class"],
  });
}
"""
THEME_TOGGLE_JS = """
() => {
  const nextDark = !(document.body.classList.contains("dark")
    || document.documentElement.classList.contains("dark")
    || document.querySelector("gradio-app")?.classList.contains("dark"));
  const followSystem = nextDark === window.matchMedia("(prefers-color-scheme: dark)").matches;
  try {
    localStorage.setItem("rag-pdf-follow-system-theme", followSystem ? "true" : "false");
    localStorage.setItem("rag-pdf-theme", nextDark ? "dark" : "light");
  } catch (_) {}
  document.documentElement.classList.toggle("dark", nextDark);
  document.body.classList.toggle("dark", nextDark);
  document.querySelector("gradio-app")?.classList.toggle("dark", nextDark);
  const checkbox = document.querySelector("#follow-windows-theme input[type='checkbox']");
  if (checkbox && checkbox.checked !== followSystem) checkbox.click();
}
"""
THEME_FOLLOW_SYSTEM_JS = """
(followSystem) => {
  const follow = Boolean(followSystem);
  try { localStorage.setItem("rag-pdf-follow-system-theme", follow ? "true" : "false"); } catch (_) {}
  if (!follow) return;
  const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.classList.toggle("dark", dark);
  document.body.classList.toggle("dark", dark);
  document.querySelector("gradio-app")?.classList.toggle("dark", dark);
}
"""
EXPAND_ALL_CLICK_JS = """
() => {
  const expandPass = () => {
    for (const accordion of document.querySelectorAll(
      ".top-level-accordion, .native-upload-subaccordion"
    )) {
      const toggle = accordion.querySelector(":scope > button.label-wrap");
      const content = accordion.querySelector(":scope > [data-testid='accordion-content']");
      if (toggle && content && getComputedStyle(content).display === "none") {
        toggle.click();
      }
    }
  };
  expandPass();
  requestAnimationFrame(expandPass);
  setTimeout(expandPass, 80);
  setTimeout(expandPass, 220);
}
"""
APP_CSS = """
html,
body,
gradio-app,
.gradio-container {
    overflow-x: hidden !important;
}
html,
body,
gradio-app {
    width: 100% !important;
    max-width: 100vw !important;
}
gradio-app,
.gradio-container,
.gradio-container .prose,
.gradio-container .prose h1 {
    overflow-y: visible !important;
}
.gradio-container .prose {
    scrollbar-width: none !important;
}
.gradio-container .prose::-webkit-scrollbar {
    display: none !important;
}
.gradio-container {
    width: min(1000px, calc(100vw - 48px)) !important;
    max-width: min(1000px, calc(100vw - 48px)) !important;
    margin: 0 auto !important;
    padding: 22px 24px 40px !important;
    font-family: "Aptos", "Segoe UI Variable Text", "Segoe UI", system-ui, -apple-system, BlinkMacSystemFont, sans-serif !important;
    font-size: 15px !important;
    letter-spacing: 0 !important;
}
.gradio-container,
.gradio-container > div,
.gradio-container .app,
.gradio-container .wrap,
.gradio-container main.container {
    max-width: min(1000px, calc(100vw - 48px)) !important;
    margin-left: auto !important;
    margin-right: auto !important;
}
.gradio-container *,
.gradio-container *::before,
.gradio-container *::after {
    box-sizing: border-box !important;
}
.gradio-container .prose h1 {
    font-family: "Aptos Display", "Aptos", "Segoe UI Variable Display", "Segoe UI", system-ui, sans-serif !important;
    font-size: 1.45rem !important;
    line-height: 1.2 !important;
    margin-bottom: 0.85rem !important;
    letter-spacing: 0 !important;
}
.gradio-container button,
.gradio-container input,
.gradio-container textarea,
.gradio-container select,
.gradio-container [role="combobox"] {
    font-family: inherit !important;
    letter-spacing: 0 !important;
}
/*
 * Gradio's generic status line is only a queue stopwatch ("processing |
 * 10.2s") for this app.  It does not describe pipeline progress and can stay
 * visible indefinitely after a small field callback.  Real run progress,
 * stage text, estimates, and the native loading indicator use separate DOM
 * elements, so suppress this text globally without hiding the loader itself.
 */
.gradio-container .progress-text {
    display: none !important;
}
.gradio-container .tabs,
.gradio-container .tabitem,
.gradio-container .block,
.gradio-container .form,
.gradio-container .panel,
.gradio-container .column,
.gradio-container .row,
.gradio-container .accordion,
.gradio-container .metadata-summary,
.gradio-container .document-metadata-preview {
    box-shadow: none !important;
    width: 100% !important;
    max-width: 100% !important;
    min-width: 0 !important;
}
.gradio-container .block,
.gradio-container .form {
    border-radius: 8px !important;
    gap: 4px !important;
}
.gradio-container .block.hide-container {
    overflow: visible !important;
    scrollbar-width: none !important;
}
.gradio-container .block.hide-container::-webkit-scrollbar {
    display: none !important;
}
.gradio-container .accordion {
    border-radius: 10px !important;
    overflow: hidden !important;
    padding: 0 !important;
}
.gradio-container .accordion > button,
.gradio-container .accordion button.label-wrap,
.gradio-container button.label-wrap {
    width: 100% !important;
    min-height: 48px !important;
    padding: 0 13px !important;
    margin: 0 !important;
    border: 0 !important;
    display: flex !important;
    align-items: center !important;
    justify-content: space-between !important;
    cursor: pointer !important;
    background: transparent !important;
    box-shadow: none !important;
    border-radius: inherit !important;
}
.gradio-container .accordion {
    cursor: pointer !important;
}
.gradio-container .label-wrap {
    margin: 0 !important;
    padding: 0 !important;
    background: transparent !important;
    border: 0 !important;
    box-shadow: none !important;
    border-radius: inherit !important;
}
.gradio-container .label-wrap span,
.gradio-container label span {
    background: transparent !important;
    padding: 0 !important;
    color: var(--body-text-color) !important;
    font-weight: 600 !important;
}
/* The child accordion is folded inside its already-expanded parent. Let its
   header meet the parent outline directly; an inset creates a distracting
   grey gutter around the label. */
.document-metadata-details:not(:has(> button.label-wrap.open)) {
    padding: 0 !important;
    background: transparent !important;
    overflow: hidden !important;
}
.document-metadata-details:not(:has(> button.label-wrap.open)) > button,
.document-metadata-details:not(:has(> button.label-wrap.open)) > button.label-wrap,
.document-metadata-details:not(:has(> button.label-wrap.open)) button.label-wrap {
    min-height: 48px !important;
    height: 48px !important;
    padding: 0 10px !important;
}
.document-metadata-details .label-wrap span {
    font-size: 0.82rem !important;
}
.native-upload-stack {
    gap: 6px !important;
}
/* Keep top-level sections visually continuous in both states.  Making an
   expanded section borderless caused its heading and content to blend into
   the page while adjacent collapsed sections remained outlined. */
.top-level-accordion:has(> button.label-wrap.open) {
    border-color: var(--border-color-primary) !important;
    background: var(--background-fill-secondary) !important;
    box-shadow: none !important;
}
.top-level-accordion:has(> button.label-wrap.open) > button.label-wrap {
    background: var(--background-fill-secondary) !important;
    border-radius: 10px 10px 0 0 !important;
}
.top-level-accordion:has(> button.label-wrap.open) > [data-testid="accordion-content"] {
    margin: 0 !important;
    padding: 5px 6px 7px !important;
    background: var(--background-fill-secondary) !important;
    border: 0 !important;
    border-radius: 0 0 10px 10px !important;
}
.native-upload-subsection-title {
    margin: 6px 0 0 0 !important;
    font-size: 0.84rem;
    font-weight: 700;
    color: var(--body-text-color-subdued);
    letter-spacing: 0.02em;
    text-transform: uppercase;
}
.native-upload-subsection-separator {
    margin: 2px 0 4px 0 !important;
    border: 0;
    border-top: 1px solid var(--border-color-primary);
    opacity: 0.9;
}
.native-upload-subaccordion {
    margin: 2px 0 0 0 !important;
}
.native-upload-readiness-panel {
    border: 1px solid var(--border-color-primary);
    border-radius: 10px;
    padding: 10px 12px;
    background: color-mix(in srgb, var(--background-fill-secondary) 88%, transparent);
    margin: 2px 0 4px 0;
}
.native-upload-readiness-title {
    font-size: 0.84rem;
    font-weight: 700;
    margin-bottom: 6px;
    color: var(--body-text-color);
}
.native-upload-readiness-row {
    display: grid;
    grid-template-columns: 150px 68px minmax(0, 1fr);
    gap: 10px;
    align-items: start;
    padding: 4px 0;
    border-top: 1px solid color-mix(in srgb, var(--border-color-primary) 55%, transparent);
}
.native-upload-readiness-row:first-of-type {
    border-top: 0;
}
.native-upload-readiness-key {
    font-weight: 600;
    color: var(--body-text-color);
}
.native-upload-readiness-state {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 52px;
    padding: 2px 8px;
    border-radius: 999px;
    font-size: 0.76rem;
    font-weight: 700;
    text-transform: lowercase;
}
.native-upload-readiness-state.pass {
    background: rgba(34, 197, 94, 0.14);
    color: #15803d;
}
.native-upload-readiness-state.fail {
    background: rgba(239, 68, 68, 0.14);
    color: #b91c1c;
}
.native-upload-readiness-state.pending {
    background: rgba(148, 163, 184, 0.16);
    color: var(--body-text-color-subdued);
}
.native-upload-readiness-detail {
    min-width: 0;
    color: var(--body-text-color-subdued);
    overflow-wrap: anywhere;
}
@media (max-width: 780px) {
    .native-upload-readiness-row {
        grid-template-columns: 1fr;
        gap: 4px;
    }
}
.gradio-container input,
.gradio-container textarea,
.gradio-container select,
.gradio-container [role="combobox"] {
    border-radius: 7px !important;
    min-height: 42px !important;
}
.gradio-container textarea {
    line-height: 1.45 !important;
}
/* Accordion controls otherwise accumulate Gradio's 12px component shell,
   another 12px dropdown wrapper, and a zero-inset combobox input.  Keep a
   modest, even inset so expanded sections stay compact while their values do
   not visually touch the dark control surface. */
.gradio-container .top-level-accordion .block.padded {
    padding-left: 6px !important;
    padding-right: 6px !important;
}
.gradio-container .top-level-accordion .wrap-inner {
    padding-left: 6px !important;
    padding-right: 6px !important;
}
.gradio-container .top-level-accordion input[role="combobox"] {
    padding-left: 8px !important;
    padding-right: 8px !important;
}
.gradio-container input[type="checkbox"],
.gradio-container input[type="radio"] {
    accent-color: #2563eb !important;
    width: 17px !important;
    height: 17px !important;
}
.gradio-container input[type="radio"],
.gradio-container input[type="checkbox"] {
    appearance: none !important;
    -webkit-appearance: none !important;
    box-shadow: none !important;
    border: 1.5px solid #7aa7ee !important;
    background: transparent !important;
    display: inline-grid !important;
    place-content: center !important;
    flex: 0 0 auto !important;
}
.gradio-container input[type="radio"] {
    border-radius: 999px !important;
}
.gradio-container input[type="checkbox"] {
    border-radius: 5px !important;
}
.gradio-container input[type="checkbox"] {
    width: 0 !important;
    height: 0 !important;
    min-height: 0 !important;
    border: 0 !important;
    padding: 0 !important;
    margin: 0 !important;
    opacity: 0 !important;
    position: absolute !important;
    pointer-events: none !important;
}
.gradio-container input[type="radio"]::before,
.gradio-container input[type="checkbox"]::before {
    content: "" !important;
    width: 7px !important;
    height: 7px !important;
    transform: scale(0) !important;
    transition: transform 100ms ease-in-out !important;
    background: #2563eb !important;
}
.gradio-container input[type="radio"]::before {
    border-radius: 999px !important;
}
.gradio-container input[type="checkbox"]::before {
    border-radius: 2px !important;
}
.gradio-container input[type="radio"]:checked::before,
.gradio-container input[type="checkbox"]:checked::before {
    transform: scale(1) !important;
}
.gradio-container input[type="radio"]:focus-visible,
.gradio-container input[type="checkbox"]:focus-visible {
    outline: 2px solid #93c5fd !important;
    outline-offset: 2px !important;
}
.gradio-container label:has(input[type="radio"]),
.gradio-container label:has(input[type="checkbox"]) {
    border-radius: 7px !important;
    border: 1px solid transparent !important;
    padding: 9px 11px !important;
    gap: 8px !important;
    cursor: pointer !important;
    transition: background-color 120ms ease, border-color 120ms ease, color 120ms ease !important;
}
.gradio-container label[data-testid$="-radio-label"],
.gradio-container label[data-testid$="-checkbox-label"] {
    box-shadow: none !important;
    background: transparent !important;
    color: var(--body-text-color) !important;
}
.gradio-container label:has(input[type="radio"]:checked),
.gradio-container label:has(input[type="checkbox"]:checked) {
    background: transparent !important;
    color: var(--body-text-color) !important;
    border-color: #3b82f6 !important;
}
.gradio-container label:has(input[type="radio"]:checked) span,
.gradio-container label:has(input[type="checkbox"]:checked) span {
    color: var(--body-text-color) !important;
}
.gradio-container input[type="radio"]:checked,
.gradio-container input[type="checkbox"]:checked {
    background: #eff6ff !important;
    border-color: #2563eb !important;
}
/* Gradio 6 provides its own checkbox indicator.  The older hidden-input plus
   pseudo-element rules above now collide with it, producing a pale rotated
   glyph instead of a reliable checked/unchecked control.  Retain the label
   cards, but restore a native, accessible checkbox indicator. */
.gradio-container label.checkbox-container input[type="checkbox"] {
    appearance: auto !important;
    -webkit-appearance: checkbox !important;
    width: 17px !important;
    height: 17px !important;
    min-width: 17px !important;
    min-height: 17px !important;
    margin: 0 !important;
    padding: 0 !important;
    border: 0 !important;
    border-radius: 0 !important;
    background: initial !important;
    opacity: 1 !important;
    position: static !important;
    pointer-events: auto !important;
    accent-color: #16a34a !important;
}
.gradio-container label.checkbox-container input[type="checkbox"]::before {
    content: none !important;
}
/* Radio groups use Gradio's data-testid label contract rather than the
   checkbox-container class.  Their old custom pseudo-element was square and
   therefore made mutually exclusive choices look like checkboxes. */
.gradio-container label[data-testid$="-radio-label"] input[type="radio"] {
    appearance: auto !important;
    -webkit-appearance: radio !important;
    width: 17px !important;
    height: 17px !important;
    min-width: 17px !important;
    min-height: 17px !important;
    margin: 0 !important;
    padding: 0 !important;
    border: 0 !important;
    border-radius: 50% !important;
    background: initial !important;
    opacity: 1 !important;
    position: static !important;
    pointer-events: auto !important;
    accent-color: #2563eb !important;
}
.gradio-container label[data-testid$="-radio-label"] input[type="radio"]::before {
    content: none !important;
}
.gradio-container button.primary {
    background: #2563eb !important;
    border-color: #2563eb !important;
    color: #ffffff !important;
}
.gradio-container button.primary:hover {
    background: #1d4ed8 !important;
    border-color: #1d4ed8 !important;
}
.output-folder-actions .form {
    min-width: 0 !important;
}
.top-toolbar {
    align-items: center !important;
    justify-content: space-between !important;
    gap: 12px !important;
    margin-bottom: 8px !important;
}
.top-toolbar > .block,
.top-toolbar > .form,
.top-toolbar > div {
    min-width: 0 !important;
    background: transparent !important;
    border: 0 !important;
    box-shadow: none !important;
    padding: 0 !important;
}
.anythingllm-startup-status-module {
    width: 100%;
    margin: 0 0 14px;
    padding: 12px;
    border: 1px solid #bfdbfe;
    border-radius: 10px;
    background: #eff6ff;
    color: #1e3a5f;
}
.anythingllm-startup-status-module:has(.anythingllm-startup-status--offline) {
    border-color: #fca5a5;
    background: #fef2f2;
    color: #991b1b;
}
.anythingllm-startup-status {
    margin: 0 0 10px;
    color: inherit;
}
.anythingllm-startup-status strong { color: inherit; }
#anythingllm-startup-status,
#anythingllm-startup-status .html-container {
    padding: 0 !important;
    border: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
}
#anythingllm-startup-status-module > .form,
#anythingllm-startup-status-module > .block,
#anythingllm-startup-status-module > div {
    min-width: 0 !important;
}
#refresh-anythingllm-startup-status,
#refresh-anythingllm-startup-status > button {
    width: 100% !important;
}
#refresh-anythingllm-startup-status.anythingllm-refresh-status-flash,
#refresh-anythingllm-startup-status .anythingllm-refresh-status-flash {
    background: #dc2626 !important;
    border-color: #dc2626 !important;
    color: #ffffff !important;
}
.dark .anythingllm-startup-status-module,
body.dark .anythingllm-startup-status-module,
gradio-app.dark .anythingllm-startup-status-module {
    border-color: #1d4ed8;
    background: #172554;
    color: #dbeafe;
}
.dark .anythingllm-startup-status-module:has(.anythingllm-startup-status--offline),
body.dark .anythingllm-startup-status-module:has(.anythingllm-startup-status--offline),
gradio-app.dark .anythingllm-startup-status-module:has(.anythingllm-startup-status--offline) {
    border-color: #b91c1c;
    background: #450a0a;
    color: #fee2e2;
}
.top-level-accordion,
.native-upload-subaccordion,
.output-downloads-accordion {
    padding: 0 !important;
    border-radius: 10px !important;
    overflow: hidden !important;
}
.top-level-accordion > button,
.top-level-accordion > button.label-wrap,
.top-level-accordion button.label-wrap,
.native-upload-subaccordion > button,
.native-upload-subaccordion > button.label-wrap,
.native-upload-subaccordion button.label-wrap,
.output-downloads-accordion > button,
.output-downloads-accordion > button.label-wrap,
.output-downloads-accordion button.label-wrap {
    width: 100% !important;
    min-height: 48px !important;
    padding: 0 13px !important;
    margin: 0 !important;
    background: transparent !important;
    border: 0 !important;
    box-shadow: none !important;
    border-radius: inherit !important;
}
.top-level-accordion .label-wrap,
.native-upload-subaccordion .label-wrap,
.output-downloads-accordion .label-wrap {
    margin: 0 !important;
    box-shadow: none !important;
}
#expand-all-accordions-button {
    flex: 0 0 auto !important;
    width: auto !important;
    min-width: 0 !important;
    margin-left: auto !important;
    min-height: 32px !important;
    height: 32px !important;
    padding: 0 12px !important;
    border: 1px solid var(--border-color-primary) !important;
    background: var(--background-fill-primary) !important;
    color: var(--body-text-color) !important;
    box-shadow: none !important;
    font-size: 0.88rem !important;
}
#expand-all-accordions-button:hover {
    border-color: #60a5fa !important;
    background: var(--background-fill-secondary) !important;
    color: var(--body-text-color) !important;
}
.output-folder-actions button {
    width: 100% !important;
}
.run-actions {
    align-items: stretch !important;
    gap: 10px !important;
    margin-bottom: 0 !important;
    padding-bottom: 0 !important;
}
#automatic-actions {
    width: 100% !important;
    min-height: 0 !important;
    display: flex !important;
    flex-wrap: nowrap !important;
    padding: 3px !important;
    border: 1px solid var(--border-color-primary) !important;
    border-radius: 8px !important;
    background: var(--background-fill-secondary) !important;
}
/* Desktop keeps the three run actions on one baseline.  Below this breakpoint
   their long labels have a larger intrinsic width than the form, so forcing
   nowrap made the entire control row wider than the viewport and then hid it
   behind the app's no-horizontal-scroll guard. */
@media (max-width: 700px) {
    #automatic-actions {
        flex-wrap: wrap !important;
        gap: 6px !important;
    }
    #automatic-actions > .block,
    #automatic-actions > .form,
    #automatic-actions > div,
    #automatic-actions > button {
        flex: 1 1 100% !important;
        min-width: 0 !important;
    }
}
#automatic-run-flow,
#automatic-run-flow > div,
#automatic-run-flow > .wrap {
    gap: 0 !important;
    margin: 0 !important;
    padding-top: 0 !important;
    padding-bottom: 0 !important;
}
/* The confirmation host is always mounted so Gradio never has to reveal a
   container asynchronously. Hide only its empty HTML shell with CSS; modern
   Desktop Chromium supports :has(), and populated review content restores it
   without changing the Gradio layout tree. */
#automatic-run-confirmation:has(.prose:empty) {
    display: none !important;
}
.run-actions > .block,
.run-actions > div {
    min-width: 0 !important;
}
.run-actions button {
    width: 100% !important;
    min-width: 0 !important;
    white-space: normal !important;
}
#automatic-process-button button[disabled="disabled"],
#automatic-process-button button[disabled],
#open-generated-output-button button[disabled="disabled"],
#open-generated-output-button button[disabled] {
    background: #d8dee8 !important;
    border: 1px solid #94a3b8 !important;
    color: #475569 !important;
    opacity: 1 !important;
    box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.25) !important;
}
/* The disabled review action is intentionally quieter than the actionable
   primary buttons in dark mode, while remaining easy to read. */
body.dark #automatic-process-button[disabled="disabled"],
body.dark #automatic-process-button[disabled] {
    background: #1e3a5f !important;
    border: 1px solid #3b82f6 !important;
    color: #dbeafe !important;
    box-shadow: none !important;
}
.download-list-placeholder {
    border: 1px solid var(--border-color-primary);
    border-radius: 8px;
    padding: 0;
    margin: 4px 0 6px;
    background: var(--background-fill-secondary);
    overflow: hidden;
}
.download-list-placeholder .download-title {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    margin: 8px 0 6px 10px;
    padding: 6px 10px;
    border-radius: 6px;
    background: #2563eb;
    color: #ffffff;
    font-size: 0.9rem;
    font-weight: 700;
}
.download-list-placeholder .download-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    min-height: 31px;
    padding: 5px 12px;
    gap: 12px;
    border-top: 1px solid color-mix(in srgb, var(--border-color-primary) 70%, transparent);
}
.download-list-placeholder .download-row:nth-child(odd) {
    background: color-mix(in srgb, var(--background-fill-primary) 50%, transparent);
}
.download-list-placeholder .download-name {
    flex: 1 1 auto;
    min-width: 0;
    overflow: hidden;
    white-space: nowrap;
    text-overflow: ellipsis;
    color: var(--body-text-color);
}
.download-list-placeholder .download-status {
    flex: 0 0 auto;
    white-space: nowrap;
    text-align: right;
    color: var(--body-text-color-subdued);
    font-variant-numeric: tabular-nums;
}
.metadata-summary {
    border: 1px solid var(--border-color-primary);
    border-radius: 8px;
    overflow: hidden;
    background: var(--background-fill-secondary);
}
.metadata-summary .metadata-file {
    padding: 12px 14px;
    border-top: 1px solid var(--border-color-primary);
}
.metadata-summary .metadata-file:first-child {
    border-top: 0;
}
.metadata-summary .metadata-file-name {
    font-weight: 700;
    margin-bottom: 8px;
    overflow-wrap: anywhere;
}
.metadata-summary .metadata-grid {
    display: grid;
    grid-template-columns: minmax(130px, 0.7fr) minmax(0, 1.9fr);
    gap: 5px 12px;
    align-items: baseline;
}
.metadata-summary .metadata-key {
    color: var(--body-text-color-subdued);
}
.metadata-summary .metadata-value {
    overflow-wrap: anywhere;
}
.metadata-summary .metadata-status {
    padding: 18px 14px;
    color: var(--body-text-color-subdued);
}
.metadata-summary .inspector-pre {
    margin: 0;
    padding: 10px 12px;
    border-radius: 6px;
    border: 1px solid var(--border-color-primary);
    background: var(--background-fill-primary);
    font-size: 0.82rem;
    line-height: 1.45;
    white-space: pre-wrap;
    word-break: break-word;
    overflow-x: hidden;
}
.workspace-verification-card {
    padding: 12px 14px;
    border-left: 4px solid var(--border-color-primary);
    border-radius: 7px;
    background: var(--background-fill-secondary);
    display: grid;
    gap: 8px;
}
.workspace-verification-card.green { border-left-color: #16a34a; }
.workspace-verification-card.yellow { border-left-color: #d97706; }
.workspace-verification-card.red { border-left-color: #dc2626; }
.workspace-verification-card .metadata-grid { margin-top: 2px; }
.ingestion-history.successful { border-left: 4px solid #16a34a; }
.ingestion-history.warning { border-left: 4px solid #d97706; }
.ingestion-history.failed { border-left: 4px solid #dc2626; }
.document-metadata-preview {
    max-height: min(52vh, 520px);
    overflow-y: auto !important;
    overflow-x: hidden !important;
}
.document-metadata-preview > div,
.document-metadata-preview .metadata-summary {
    width: 100% !important;
    max-width: 100% !important;
    min-width: 0 !important;
}
.document-metadata-preview::-webkit-scrollbar {
    width: 10px;
}
.run-summary-panel {
    border: 1px solid var(--border-color-primary);
    border-radius: 8px;
    overflow: hidden;
    background: var(--background-fill-secondary);
}
.run-summary-panel .summary-placeholder {
    padding: 18px 14px;
    color: var(--body-text-color-subdued);
}
.run-summary-panel .summary-row {
    display: grid;
    grid-template-columns: minmax(165px, 0.8fr) minmax(0, 2fr);
    gap: 12px;
    padding: 6px 12px;
    border-top: 1px solid color-mix(in srgb, var(--border-color-primary) 65%, transparent);
}
.run-summary-panel .summary-key {
    color: var(--body-text-color-subdued);
    font-weight: 600;
}
.run-summary-panel .summary-value {
    overflow-wrap: anywhere;
}
.run-summary-panel .summary-heading {
    padding: 10px 12px 6px;
    font-weight: 750;
    color: var(--body-text-color);
}
.run-summary-panel .summary-bullet {
    padding: 3px 12px 3px 28px;
    color: var(--body-text-color);
}
.run-summary-panel .summary-status {
    display: inline-flex;
    margin: 10px 12px;
    padding: 4px 9px;
    border-radius: 999px;
    border: 1px solid #22c55e;
    color: #15803d;
    font-weight: 700;
}
.run-summary-panel .summary-status.error {
    border-color: #ef4444;
    color: #dc2626;
}
@media (max-width: 720px) {
    .metadata-summary .metadata-grid {
        grid-template-columns: 1fr;
        gap: 2px;
    }
    .document-metadata-preview {
        max-height: min(46vh, 420px);
    }
}
@media (max-width: 760px) {
    .download-list-placeholder .download-row {
        display: flex !important;
        grid-template-columns: minmax(0, 1fr) auto !important;
        justify-content: space-between !important;
        align-items: center !important;
        gap: 12px !important;
    }
    .download-list-placeholder .download-status {
        flex: 0 0 auto !important;
        margin-left: 12px !important;
        white-space: nowrap !important;
    }
}
.control-row {
    align-items: end !important;
}
.control-row > .block,
.control-row > div {
    min-width: 0 !important;
}
.control-row button {
    min-height: 42px !important;
    height: 42px !important;
    padding: 0 16px !important;
    display: inline-flex !important;
    align-items: center !important;
    justify-content: center !important;
    white-space: normal !important;
    line-height: 1.15 !important;
}
.control-row .form {
    gap: 0 !important;
}
.aligned-settings-row {
    align-items: stretch !important;
}
.aligned-settings-row > .block,
.aligned-settings-row > div {
    min-width: 0 !important;
    align-self: stretch !important;
}
.aligned-settings-row .form {
    height: 100% !important;
    display: flex !important;
    flex-direction: column !important;
    justify-content: flex-start !important;
}
.aligned-settings-row .form > label,
.aligned-settings-row .form label {
    min-height: 54px !important;
    align-items: flex-start !important;
}
.aligned-settings-row button {
    align-self: stretch !important;
    min-height: 42px !important;
    height: 42px !important;
}
.aligned-action-button,
.aligned-action-button > div,
.aligned-action-button .form {
    height: 100% !important;
}
.aligned-action-button button {
    min-height: 42px !important;
    height: 42px !important;
    width: 100% !important;
}
.automatic-run-summary {
    margin-top: 6px !important;
    margin-bottom: 4px !important;
}
.output-downloads-accordion [data-testid='accordion-content'],
.output-downloads-accordion .accordion-content,
.output-downloads-accordion > div:nth-child(2) {
    padding-top: 0 !important;
    margin-top: 0 !important;
    border-top: 0 !important;
}
.output-downloads-accordion .automatic-run-summary:empty {
    display: none !important;
    margin: 0 !important;
    padding: 0 !important;
}
#automatic-download-section,
.automatic-download-section {
    border: 0 !important;
    border-radius: 0 !important;
    overflow: hidden !important;
    background: transparent !important;
    margin-top: 0 !important;
    margin-bottom: 4px !important;
    padding: 0 !important;
}
#automatic-download-section,
#automatic-download-section > .form,
#automatic-download-section > .block,
#automatic-download-section > div,
#automatic-download-section .form,
#automatic-download-section .block,
.automatic-download-section > .form,
.automatic-download-section > .block,
.automatic-download-section > div {
    margin-top: 0 !important;
    padding-top: 0 !important;
    border-top: 0 !important;
    background: transparent !important;
}
#automatic-download-section.gr-group,
#automatic-download-section.gradio-container,
#automatic-download-section.block,
#automatic-download-section.wrap,
#automatic-download-section .gr-group,
#automatic-download-section .block {
    border-top: 0 !important;
    margin-top: 0 !important;
    padding-top: 0 !important;
}
.downloads-header-row {
    align-items: center !important;
    justify-content: flex-start !important;
    flex-wrap: nowrap !important;
    min-height: 32px !important;
    padding: 0 0 2px 0 !important;
    gap: 8px !important;
    border-bottom: 0 !important;
    background: transparent !important;
    margin: 0 !important;
    overflow: hidden !important;
}
.downloads-header-row .form.svelte-d5xbca,
.downloads-header-row .block.download-folder-control {
    overflow-x: hidden !important;
}
.downloads-header-row > .form,
.downloads-header-row > .block,
.downloads-header-row .form.svelte-d5xbca {
    flex: 0 0 auto !important;
    flex-grow: 0 !important;
    min-width: 0 !important;
    width: auto !important;
    overflow: hidden !important;
}
.downloads-header-row .downloads-header-title,
.downloads-header-row > .block:first-child {
    min-width: 0 !important;
    margin: 0 !important;
    padding: 0 !important;
    flex: 1 1 auto !important;
}
.downloads-header-title {
    display: inline-flex;
    align-items: center;
    justify-content: flex-start;
    width: auto;
    min-width: 0;
    padding: 0 !important;
    border-radius: 0;
    background: transparent !important;
    color: var(--body-text-color) !important;
    font-size: 0.98rem;
    font-weight: 700;
}
.download-folder-control {
    position: static !important;
    flex: 0 0 auto !important;
    margin-left: 0 !important;
    width: auto !important;
    max-width: 190px !important;
    min-width: 0 !important;
    height: 28px !important;
    max-height: 28px !important;
    padding: 0 !important;
    border: 0 !important;
    outline: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
    overflow: hidden !important;
}
.download-folder-control .form,
.download-folder-control .wrap {
    width: auto !important;
    max-width: 190px !important;
    min-width: 0 !important;
    height: 28px !important;
    min-height: 28px !important;
    max-height: 28px !important;
    margin: 0 !important;
    padding: 0 !important;
    background: transparent !important;
    border: 0 !important;
    box-shadow: none !important;
    outline: 0 !important;
    overflow: hidden !important;
}
.download-folder-control label {
    width: auto !important;
    max-width: 190px !important;
    min-width: 0 !important;
    height: 28px !important;
    min-height: 28px !important;
    max-height: 28px !important;
    margin: 0 !important;
    padding: 4px 10px !important;
    gap: 5px !important;
    border: 1px solid #60a5fa !important;
    border-radius: 6px !important;
    background: rgba(59, 130, 246, 0.12) !important;
    color: var(--body-text-color) !important;
    box-shadow: none !important;
    overflow: hidden !important;
}
.download-folder-control input {
    width: 14px !important;
    height: 14px !important;
    min-width: 14px !important;
}
.download-folder-control span {
    font-size: 0.76rem !important;
    line-height: 1 !important;
    white-space: nowrap !important;
}
/* The downloads header contains two independent switches.  Its former
   nowrap/overflow-hidden layout silently clipped a switch on narrow screens.
   Let only this header reflow; wide Desktop layouts remain a compact row. */
@media (max-width: 700px) {
    .downloads-header-row {
        flex-wrap: wrap !important;
        min-height: 0 !important;
        overflow: visible !important;
    }
    .downloads-header-row .downloads-header-title,
    .downloads-header-row > .block:first-child {
        flex: 1 0 100% !important;
    }
    .downloads-header-row > .block.download-folder-control,
    .downloads-header-row > .form.download-folder-control,
    .downloads-header-row .block.download-folder-control {
        flex: 1 1 100% !important;
        width: auto !important;
        max-width: none !important;
        overflow: visible !important;
    }
    .downloads-header-row .download-folder-control .form,
    .downloads-header-row .download-folder-control .wrap,
    .downloads-header-row .download-folder-control label {
        width: 100% !important;
        max-width: none !important;
        overflow: visible !important;
    }
}
.automatic-download-section .download-list-placeholder {
    margin: 0 !important;
    border: 0 !important;
    border-radius: 0 !important;
}
.automatic-download-section .styler > .block.hide-container.auto-margin:last-child > .html-container,
.automatic-download-section .styler > .block.hide-container.auto-margin:last-child > .html-container > .prose {
    margin: 0 !important;
    padding: 0 !important;
    border: 0 !important;
    background: transparent !important;
}
.automatic-download-section .download-list-placeholder .download-row:first-child {
    border-top: 0 !important;
}
.automatic-download-section .file-preview-holder,
.automatic-download-section .file-preview {
    margin: 0 !important;
    border: 0 !important;
    border-top: 0 !important;
    border-radius: 6px !important;
    box-shadow: none !important;
}
.automatic-download-section .file-preview-holder {
    padding-top: 0 !important;
}
.automatic-download-section .file-preview button,
.automatic-download-section .file-preview a,
.automatic-download-section .file-preview li,
.automatic-download-section .file-preview .file-preview-item,
.automatic-download-section .file-preview .file-preview-item > * {
    white-space: nowrap !important;
}
.automatic-download-section .file-preview svg,
.automatic-download-section .file-preview .delete,
.automatic-download-section .file-preview .download-link {
    flex-shrink: 0 !important;
}
.batch-folder-panel {
    margin: 6px 0 0 0 !important;
    padding: 0 !important;
    background: #f4f7fb !important;
    border: 1px solid #bcc9d8 !important;
    border-top: 0 !important;
    border-radius: 0 0 10px 10px !important;
    box-shadow: none !important;
    position: relative !important;
    min-height: 96px !important;
    overflow: hidden !important;
}
.batch-folder-panel > div,
.batch-folder-panel > .block,
.batch-folder-panel > .form,
.batch-folder-panel > .column {
    margin: 0 !important;
    padding: 0 !important;
    background: transparent !important;
    border: 0 !important;
    box-shadow: none !important;
}
.batch-folder-panel .html-container,
.batch-folder-panel .html-container > .prose,
.batch-folder-panel .prose {
    margin: 0 !important;
    padding: 0 !important;
    background: transparent !important;
    border: 0 !important;
}
.batch-folder-panel .batch-folder-title {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    margin: 6px 0 0 6px !important;
    padding: 4px 6px !important;
    border-radius: 6px;
    background: #dbeafe !important;
    color: #3b82f6 !important;
    border: 0 !important;
    box-shadow: none !important;
    font-size: 14px !important;
    font-weight: 700;
    align-self: flex-start !important;
    cursor: pointer !important;
    position: relative !important;
    z-index: 4 !important;
}
.batch-folder-panel .batch-folder-title-icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 16px;
    height: 16px;
    color: #3b82f6 !important;
}
.batch-folder-panel .batch-folder-title-icon svg {
    width: 16px;
    height: 16px;
    stroke: currentColor;
    fill: none;
    stroke-width: 2;
    stroke-linecap: round;
    stroke-linejoin: round;
}
.batch-folder-inner {
    margin: 0 !important;
    padding: 0 !important;
    gap: 0 !important;
    display: flex !important;
    flex-direction: column !important;
    align-items: center !important;
    position: static !important;
    min-height: 110px !important;
}
.batch-folder-inner > div,
.batch-folder-inner > .block,
.batch-folder-inner > .column,
.batch-folder-inner > .form {
    margin: 0 !important;
    padding: 0 !important;
    width: 100% !important;
}
#choose-pdf-folder-button,
.batch-folder-chooser {
    margin: 0 !important;
    padding: 0 !important;
    min-height: 100% !important;
    height: 100% !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    position: absolute !important;
    inset: 0 !important;
    z-index: 1 !important;
    padding: 0 16px !important;
    line-height: 1 !important;
}
#choose-pdf-folder-button > div,
#choose-pdf-folder-button > .block,
#choose-pdf-folder-button .wrap,
#choose-pdf-folder-button .form,
.batch-folder-chooser > div,
.batch-folder-chooser > .block,
.batch-folder-chooser .wrap,
.batch-folder-chooser .form {
    width: 100% !important;
    min-height: 100% !important;
    height: 100% !important;
    margin: 0 !important;
    padding: 0 !important;
}
#choose-pdf-folder-button button,
.batch-folder-chooser button {
    margin: 0 !important;
    width: 100% !important;
    min-height: 100% !important;
    height: 100% !important;
    padding: 0 16px !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    line-height: 1 !important;
    border-radius: 10px !important;
    font-size: 16px !important;
}
#choose-pdf-folder-button button::before,
.batch-folder-chooser button::before {
    content: "";
    display: inline-block;
    margin-right: 8px;
    width: 16px;
    height: 16px;
    background-color: currentColor;
    -webkit-mask: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='M12 16V4'/><path d='M7 9l5-5 5 5'/><path d='M20 20H4'/></svg>") center / contain no-repeat;
    mask: url(\"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='M12 16V4'/><path d='M7 9l5-5 5 5'/><path d='M20 20H4'/></svg>\") center / contain no-repeat;
}
.batch-folder-file-list {
    width: 100% !important;
    margin: 10px 0 0 0 !important;
    padding: 0 14px 14px 14px !important;
    display: flex !important;
    justify-content: center !important;
    position: relative !important;
    z-index: 5 !important;
}
.dark .batch-folder-panel,
body.dark .batch-folder-panel,
gradio-app.dark .batch-folder-panel {
    background: #1f2937 !important;
    border-color: #475569 !important;
}
.dark .batch-folder-panel .batch-folder-title,
body.dark .batch-folder-panel .batch-folder-title,
gradio-app.dark .batch-folder-panel .batch-folder-title {
    background: #172554 !important;
    color: #bfdbfe !important;
    box-shadow: none !important;
}
.dark .batch-folder-panel .batch-folder-title-icon,
body.dark .batch-folder-panel .batch-folder-title-icon,
gradio-app.dark .batch-folder-panel .batch-folder-title-icon {
    color: #bfdbfe !important;
}
.extraction-options-row {
    flex-wrap: wrap !important;
    gap: 8px !important;
    overflow-x: hidden !important;
}
.extraction-options-row > .block,
.extraction-options-row > div {
    min-width: 0 !important;
    flex: 1 1 200px !important;
}
.extraction-options-row label:has(input[type="checkbox"]),
.extraction-options-row label[data-testid$="-checkbox-label"] {
    width: 100% !important;
    min-width: 0 !important;
    white-space: normal !important;
    overflow-wrap: anywhere !important;
}
.batch-folder-file-list > div,
.batch-folder-file-list > .block,
.batch-folder-file-list .html-container,
.batch-folder-file-list .download-list-placeholder {
    margin: 0 !important;
    width: 100% !important;
    min-width: 0 !important;
    max-width: none !important;
}
.batch-folder-file-list {
    justify-content: stretch !important;
    align-items: stretch !important;
}
.batch-folder-file-list .download-list-placeholder {
    box-sizing: border-box !important;
}
.batch-folder-file-list .download-row {
    display: grid !important;
    grid-template-columns: minmax(0, 1fr) max-content !important;
    justify-content: normal !important;
    align-items: center !important;
    width: 100% !important;
    min-width: 0 !important;
    gap: 0 !important;
}
.batch-folder-file-list .download-name {
    min-width: 0 !important;
    overflow: hidden !important;
    white-space: nowrap !important;
    text-overflow: ellipsis !important;
    direction: ltr !important;
    text-align: left !important;
}
.batch-folder-file-list .download-status {
    min-width: 5.5ch !important;
    margin-left: 10px !important;
    padding-left: 10px !important;
    border-left: 1px solid color-mix(in srgb, var(--border-color-primary) 70%, transparent) !important;
    white-space: nowrap !important;
    text-align: right !important;
    font-variant-numeric: tabular-nums !important;
}
.batch-folder-selection {
    margin-top: 4px !important;
}
.batch-folder-selection .wrap,
.batch-folder-selection .checkbox-group {
    gap: 3px !important;
}
.batch-folder-selection label {
    min-height: 0 !important;
    margin: 0 !important;
    padding: 4px 7px !important;
    font-size: 0.78rem !important;
    line-height: 1.2 !important;
}
.batch-folder-selection label span {
    font-size: 0.78rem !important;
    line-height: 1.2 !important;
}
.batch-folder-selection input[type="checkbox"] {
    width: 15px !important;
    height: 15px !important;
}
.batch-folder-selection {
    margin: 4px 0 0 !important;
    position: relative !important;
    z-index: 5 !important;
}
.batch-folder-inner #choose-pdf-folder-button,
.batch-folder-inner .batch-folder-chooser {
    position: relative !important;
    inset: auto !important;
    flex: 0 0 110px !important;
    min-height: 110px !important;
    height: 110px !important;
}
.batch-folder-inner #choose-pdf-folder-button.hide,
.batch-folder-inner #choose-pdf-folder-button[style*="display: none"],
.batch-folder-inner .batch-folder-chooser.hide,
.batch-folder-inner .batch-folder-chooser[style*="display: none"] {
    display: none !important;
}
.batch-folder-status {
    width: 100% !important;
    margin: 8px 0 0 0 !important;
    padding: 0 14px 12px 14px !important;
    position: relative !important;
    z-index: 5 !important;
}
.batch-folder-status .artifact-placeholder {
    margin: 0 !important;
    text-align: center;
}
@media (max-width: 600px) {
    .batch-folder-file-list .download-row {
        padding: 5px 8px !important;
    }
    .batch-folder-file-list .download-status {
        min-width: 4.8ch !important;
        margin-left: 7px !important;
        padding-left: 7px !important;
        font-size: 0.78rem !important;
    }
}
.batch-folder-inline-notice {
    width: min(520px, 100%);
    margin: 0 auto 8px !important;
    padding: 8px 10px;
    border: 1px solid #f59e0b;
    border-radius: 7px;
    background: #fffbeb;
    color: #78350f;
    font-size: 0.9rem;
    line-height: 1.35;
    text-align: center;
}
body.dark .batch-folder-inline-notice {
    border-color: #a16207;
    background: #422006;
    color: #fef3c7;
}
.pdf-upload-input,
.pdf-upload-input > div,
.pdf-upload-input > .block {
    min-height: 0 !important;
}
/* Gradio's file preview may otherwise inherit the browser's fallback or its
   monospace metadata token. Keep the selected-file row on the same interface
   font as the rest of this app, including an upload selected during recovery. */
.pdf-upload-input,
.pdf-upload-input *,
.pdf-upload-input .file-preview,
.pdf-upload-input .file-preview * {
    font-family: "Aptos", "Segoe UI Variable Text", "Segoe UI", system-ui, -apple-system, BlinkMacSystemFont, sans-serif !important;
    font-style: normal !important;
    letter-spacing: 0 !important;
}
/* Gradio's connection toast is fixed at 12px by default, which overlaps the
   independent server-watchdog banner. Move its actual fixed host below the
   banner with CSS so this remains true across Svelte rerenders. */
.toast-wrap {
    top: 96px !important;
    right: 16px !important;
}
.pdf-upload-input [data-testid="file-upload"],
.pdf-upload-input .file-drop-area,
.pdf-upload-input .file-drop,
.pdf-upload-input section {
    min-height: 133px !important;
    height: 133px !important;
}
/* Advanced accepts one file, so replace Gradio's generic multi-file copy
   without changing the standard Automatic uploader. */
#advanced-pdf-upload button[aria-label="Click to upload or drop files"] .wrap {
    font-size: 0 !important;
}
#advanced-pdf-upload button[aria-label="Click to upload or drop files"] .wrap::after {
    content: "Drop single PDF File Here";
    display: block;
    font-size: 1rem;
}
#advanced-pdf-upload button[aria-label="Click to upload or drop files"] .wrap .or {
    display: none !important;
}
/* APP_JS inserts a real replacement action into Gradio's selected-file
   top-right action container. This avoids relying on a missing stock glyph or
   an inferred sibling relationship in the filename table. */
.pdf-upload-input .icon-button-wrapper.top-panel {
    align-items: center !important;
    gap: 3px !important;
}
.pdf-upload-input button.rag-selected-file-replace {
    display: inline-flex !important;
    align-items: center !important;
    justify-content: center !important;
    width: 30px !important;
    height: 30px !important;
    min-width: 30px !important;
    min-height: 30px !important;
    padding: 0 !important;
    border: 1px solid #93c5fd !important;
    border-radius: 6px !important;
    background: #eff6ff !important;
    color: #2563eb !important;
    opacity: 1 !important;
}
.pdf-upload-input button.rag-selected-file-replace svg {
    display: block !important;
    width: 17px !important;
    height: 17px !important;
    margin: 0 !important;
    overflow: visible !important;
    fill: none !important;
    stroke: currentColor !important;
    stroke-width: 2 !important;
    stroke-linecap: round !important;
    stroke-linejoin: round !important;
    opacity: 1 !important;
}
gradio-app.dark .pdf-upload-input button.rag-selected-file-replace,
.dark .pdf-upload-input button.rag-selected-file-replace {
    border-color: #60a5fa !important;
    background: #172554 !important;
    color: #bfdbfe !important;
}
.copy-storage-path-button {
    margin-left: 8px;
    padding: 2px 8px;
    border: 1px solid #93c5fd;
    border-radius: 6px;
    background: #dbeafe;
    color: #0f172a;
    cursor: pointer;
    font-size: 0.78rem;
}
.advanced-app-meta {
    align-items: center !important;
    justify-content: space-between !important;
    flex-wrap: wrap !important;
    gap: 8px !important;
    margin-bottom: 8px !important;
}
.advanced-app-meta > .block,
.advanced-app-meta > div {
    min-width: 0 !important;
}
.advanced-version {
    color: var(--body-text-color-subdued);
    font-family: var(--font-mono);
    font-size: 0.8rem;
    line-height: 1.3;
    overflow-wrap: anywhere;
}
.theme-controls {
    align-items: center !important;
    justify-content: space-between !important;
    width: 100% !important;
    gap: 6px !important;
    flex: 0 1 auto !important;
}
#follow-windows-theme {
    flex: 0 1 260px !important;
    width: 260px !important;
    min-width: 0 !important;
    height: 41px !important;
    min-height: 41px !important;
    padding: 10px 12px !important;
    border: 1px solid #bcc9d8 !important;
    border-radius: 8px !important;
    background: #f4f7fb !important;
    box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.45) !important;
}
#follow-windows-theme.auto-margin {
    margin-left: 0 !important;
    margin-right: 0 !important;
}
#follow-windows-theme label {
    display: flex !important;
    align-items: center !important;
    justify-content: flex-start !important;
    width: 100% !important;
    height: 100% !important;
    padding: 0 !important;
    border: 0 !important;
    border-radius: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
    white-space: nowrap !important;
    text-align: left !important;
    font-family: "Aptos", "Segoe UI Variable Text", "Segoe UI", system-ui, sans-serif !important;
    font-size: 0.82rem !important;
    font-weight: 700 !important;
}
#follow-windows-theme .wrap,
#follow-windows-theme .form {
    justify-content: flex-start !important;
    text-align: left !important;
}
#theme-toggle-button {
    flex: 0 0 auto !important;
    margin-left: auto !important;
    width: 260px !important;
    min-width: 260px !important;
    height: 41px !important;
    min-height: 41px !important;
    padding: 10px 12px !important;
    border: 1px solid #bcc9d8 !important;
    border-radius: 8px !important;
    background: #f4f7fb !important;
    box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.45) !important;
}
#theme-toggle-button,
#theme-toggle-button button {
    font-family: "Aptos", "Segoe UI Variable Text", "Segoe UI", system-ui, sans-serif !important;
    font-size: 0.82rem !important;
    font-weight: 700 !important;
}
body.dark #theme-toggle-button {
    border-color: #475569 !important;
    background: #111c2e !important;
    color: #e5eefc !important;
    box-shadow: inset 0 0 0 1px rgba(148, 163, 184, 0.14) !important;
}
.advanced-source-row,
.advanced-numeric-row {
    flex-wrap: wrap !important;
    gap: 10px !important;
}
.advanced-source-row > .block,
.advanced-source-row > div {
    flex: 1 1 280px !important;
    min-width: 0 !important;
}
.advanced-source-row {
    align-items: stretch !important;
}
.advanced-source-row > .advanced-source-card,
.advanced-source-row > .block:has(.advanced-source-card) {
    height: 320px !important;
    min-height: 320px !important;
    max-height: 320px !important;
    overflow: hidden !important;
}
.advanced-source-row .advanced-source-card {
    height: 320px !important;
    min-height: 320px !important;
    max-height: 320px !important;
    overflow: hidden !important;
}
.advanced-pdf-card {
    position: relative !important;
}
.advanced-pdf-card #advanced-pdf-upload {
    position: relative !important;
    height: 320px !important;
    min-height: 320px !important;
    max-height: 320px !important;
    overflow: hidden !important;
}
.advanced-pdf-card #advanced-pdf-upload .icon-button-wrapper.top-panel > button:not(.rag-selected-file-replace) {
    display: grid !important;
    place-items: center !important;
    width: 34px !important;
    min-width: 34px !important;
    height: 34px !important;
    min-height: 34px !important;
    margin: 0 !important;
    padding: 0 !important;
    line-height: 1 !important;
    text-align: center !important;
    text-indent: 0 !important;
}
.advanced-pdf-card #advanced-pdf-upload .icon-button-wrapper.top-panel > button:not(.rag-selected-file-replace) > * {
    display: block !important;
    width: 17px !important;
    height: 17px !important;
    margin: 0 !important;
}
.advanced-new-run-divider {
    margin: 18px 0 8px !important;
    padding: 10px 0 0 !important;
    border-top: 1px solid var(--border-color-primary) !important;
    color: var(--body-text-color) !important;
    font-size: 0.95rem;
    font-weight: 700;
}
.advanced-pdf-warning-host {
    position: absolute !important;
    inset: 48px 12px 12px !important;
    z-index: 6 !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    pointer-events: none !important;
}
.advanced-pdf-warning-host.hide-container {
    display: none !important;
}
.advanced-pdf-warning {
    max-width: min(92%, 420px);
    padding: 10px 14px;
    border: 1px solid #ef4444;
    border-radius: 8px;
    background: rgba(254, 242, 242, 0.96);
    color: #991b1b;
    text-align: center;
    font-weight: 600;
    box-shadow: 0 3px 12px rgba(127, 29, 29, 0.14);
}
.advanced-run-status {
    margin: 10px 0 0 !important;
    padding: 9px 12px !important;
    border: 1px solid var(--border-color-primary) !important;
    border-radius: 8px !important;
    background: var(--block-background-fill) !important;
    color: var(--body-text-color) !important;
    font-size: 0.92rem;
    line-height: 1.35;
}
.advanced-run-status strong {
    display: block;
    margin-bottom: 2px;
}
#advanced-prepared-text-file {
    margin-top: 12px !important;
    padding: 8px !important;
    border: 3px solid #2563eb !important;
    border-radius: 10px !important;
    background: #eff6ff !important;
}
body.dark #advanced-prepared-text-file,
gradio-app.dark #advanced-prepared-text-file {
    border-color: #60a5fa !important;
    background: #172554 !important;
}
body.dark .advanced-pdf-warning {
    border-color: #f87171;
    background: rgba(69, 10, 10, 0.94);
    color: #fecaca;
}
.completed-diagnostics-actions {
    align-items: stretch !important;
}
.completed-diagnostics-actions > .block,
.completed-diagnostics-actions > div {
    display: flex !important;
    align-items: stretch !important;
}
.completed-diagnostics-actions .completed-diagnostics-action,
.completed-diagnostics-actions .completed-diagnostics-action > button {
    height: 100% !important;
    min-height: 76px !important;
    white-space: normal !important;
}
#choose-diagnostics-run-directory-button {
    display: none !important;
}
.advanced-numeric-row > .block,
.advanced-numeric-row > div {
    flex: 1 1 150px !important;
    min-width: 0 !important;
}
.gradio-container button.secondary,
.gradio-container button:not(.primary) {
    border-radius: 7px !important;
}
@media (prefers-color-scheme: light) {
    body, .gradio-container {
        background: #eef2f7 !important;
    }
    .gradio-container .block,
    .gradio-container .form,
    .gradio-container .accordion,
    .gradio-container .tabitem {
        background: #f4f7fb !important;
        border: 1px solid #bcc9d8 !important;
        box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.45) !important;
    }
    .gradio-container button.secondary,
    .gradio-container button:not(.primary) {
        background: #f2f5f9 !important;
        border-color: #d2dbe6 !important;
        color: #273449 !important;
    }
    .gradio-container button.secondary:hover,
    .gradio-container button:not(.primary):hover {
        background: #e9eef5 !important;
        border-color: #cdd7e4 !important;
    }
    .gradio-container .tabitem {
        background: transparent !important;
        border: 0 !important;
        box-shadow: none !important;
    }
    .gradio-container input,
    .gradio-container textarea,
    .gradio-container select,
    .gradio-container [role="combobox"] {
        background: #fbfcfe !important;
        border-color: #d7dee8 !important;
    }
    .gradio-container [role="listbox"],
    .gradio-container [role="option"] {
        background: #eff6ff !important;
        border-color: #bfdbfe !important;
        color: #172033 !important;
    }
    .gradio-container [role="option"]:hover,
    .gradio-container [role="option"][aria-selected="true"] {
        background: #dbeafe !important;
    }
    .gradio-container input[type="checkbox"],
    .gradio-container input[type="radio"] {
        border-color: #93c5fd !important;
    }
    .gradio-container label:has(input[type="radio"]),
    .gradio-container label:has(input[type="checkbox"]),
    .gradio-container label[data-testid$="-radio-label"],
    .gradio-container label[data-testid$="-checkbox-label"] {
        background: #f3f6fa !important;
        border-color: #d6e0eb !important;
    }
    .gradio-container label:has(input[type="radio"]:hover),
    .gradio-container label:has(input[type="checkbox"]:hover) {
        background: #edf2f7 !important;
        border-color: #cad7e6 !important;
    }
    .gradio-container label:has(input[type="radio"]:checked),
    .gradio-container label:has(input[type="checkbox"]:checked),
    .gradio-container label[data-testid$="-radio-label"].selected {
        background: #f4f8ff !important;
        border-color: #60a5fa !important;
    }
}
body:not(.dark),
body:not(.dark) .gradio-container {
    background: #eef2f7 !important;
}
/* The browser's Windows-backed colour-scheme preference controls the dark
   class. Gradio's bundled Soft theme supplies light variables, so explicit
   dark surfaces are required when that preference changes without a reload. */
body.dark,
body.dark .gradio-container {
    background: #0f172a !important;
    color: #e5eefc !important;
}
body.dark .gradio-container .block,
body.dark .gradio-container .form,
body.dark .gradio-container .accordion,
body.dark .gradio-container .tabitem {
    background: #111c2e !important;
    border-color: #475569 !important;
    color: #e5eefc !important;
    box-shadow: inset 0 0 0 1px rgba(148, 163, 184, 0.14) !important;
}
body.dark .gradio-container button.secondary,
body.dark .gradio-container button:not(.primary) {
    background: #172033 !important;
    border-color: #64748b !important;
    color: #e5eefc !important;
}
body.dark .gradio-container input,
body.dark .gradio-container textarea,
body.dark .gradio-container select,
body.dark .gradio-container [role="combobox"] {
    background: #0b1220 !important;
    border-color: #475569 !important;
    color: #e5eefc !important;
}
/* Keep normal accordion content panels blue-toned.  Only dropdown components
   get a continuous dark control surface with no intermediate shell. */
body.dark .gradio-container .top-level-accordion .block.padded:has(input[role="combobox"]) {
    background: #0d1625 !important;
    border-color: #1f2d40 !important;
    box-shadow: none !important;
}
body.dark .gradio-container .top-level-accordion .wrap-inner:has(input[role="combobox"]) {
    background: #0d1625 !important;
    border-color: #1f2d40 !important;
}
body.dark .gradio-container .top-level-accordion input[role="combobox"] {
    background: #0d1625 !important;
}
/* Gradio's dark theme resolves the generic secondary-fill variable to the
   page background when an accordion opens. Keep the opened panel on the same
   bounded blue-navy surface as its collapsed state instead. */
body.dark .gradio-container .contain .top-level-accordion:has(> button.label-wrap.open),
body.dark .gradio-container .contain .native-upload-subaccordion:has(> button.label-wrap.open),
body.dark .gradio-container .contain .output-downloads-accordion:has(> button.label-wrap.open) {
    background: #111c2e !important;
    border-color: #475569 !important;
    box-shadow: inset 0 0 0 1px rgba(148, 163, 184, 0.14) !important;
}
body.dark .gradio-container .contain .top-level-accordion:has(> button.label-wrap.open) > button.label-wrap,
body.dark .gradio-container .contain .native-upload-subaccordion:has(> button.label-wrap.open) > button.label-wrap,
body.dark .gradio-container .contain .output-downloads-accordion:has(> button.label-wrap.open) > button.label-wrap,
body.dark .gradio-container .contain .top-level-accordion:has(> button.label-wrap.open) > [data-testid="accordion-content"],
body.dark .gradio-container .contain .native-upload-subaccordion:has(> button.label-wrap.open) > [data-testid="accordion-content"],
body.dark .gradio-container .contain .output-downloads-accordion:has(> button.label-wrap.open) > [data-testid="accordion-content"] {
    background: #111c2e !important;
}
body.dark .gradio-container label:has(input[type="radio"]),
body.dark .gradio-container label:has(input[type="checkbox"]),
body.dark .gradio-container label[data-testid$="-radio-label"],
body.dark .gradio-container label[data-testid$="-checkbox-label"] {
    background: #172033 !important;
    border-color: #475569 !important;
    color: #e5eefc !important;
}
body.dark .gradio-container label:has(input[type="radio"]:checked),
body.dark .gradio-container label:has(input[type="checkbox"]:checked),
body.dark .gradio-container label[data-testid$="-radio-label"].selected {
    background: #172554 !important;
    border-color: #60a5fa !important;
}
body:not(.dark) .gradio-container .block,
body:not(.dark) .gradio-container .form,
body:not(.dark) .gradio-container .accordion,
body:not(.dark) .gradio-container .tabitem {
    background: #f4f7fb !important;
    border: 1px solid #bcc9d8 !important;
    box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.45) !important;
}
body:not(.dark) .gradio-container .accordion > button,
body:not(.dark) .gradio-container .accordion button.label-wrap,
body:not(.dark) .gradio-container button.label-wrap {
    background: transparent !important;
    border: 0 !important;
    box-shadow: none !important;
    border-radius: inherit !important;
}
body:not(.dark) .gradio-container button.secondary,
body:not(.dark) .gradio-container button:not(.primary) {
    background: #f2f5f9 !important;
    border-color: #d2dbe6 !important;
    color: #273449 !important;
}
body:not(.dark) .gradio-container button.secondary:hover,
body:not(.dark) .gradio-container button:not(.primary):hover {
    background: #e9eef5 !important;
    border-color: #cdd7e4 !important;
}
body:not(.dark) .gradio-container .tabitem {
    background: transparent !important;
    border: 0 !important;
    box-shadow: none !important;
}
body:not(.dark) .gradio-container input,
body:not(.dark) .gradio-container textarea,
body:not(.dark) .gradio-container select,
body:not(.dark) .gradio-container [role="combobox"] {
    background: #fbfcfe !important;
    border-color: #d7dee8 !important;
}
body:not(.dark) .gradio-container label:has(input[type="radio"]),
body:not(.dark) .gradio-container label:has(input[type="checkbox"]),
body:not(.dark) .gradio-container label[data-testid$="-radio-label"],
body:not(.dark) .gradio-container label[data-testid$="-checkbox-label"] {
    background: #f3f6fa !important;
    border-color: #d6e0eb !important;
}
body:not(.dark) .gradio-container label:has(input[type="radio"]:hover),
body:not(.dark) .gradio-container label:has(input[type="checkbox"]:hover) {
    background: #edf2f7 !important;
    border-color: #cad7e6 !important;
}
body:not(.dark) .gradio-container label:has(input[type="radio"]:checked),
body:not(.dark) .gradio-container label:has(input[type="checkbox"]:checked),
body:not(.dark) .gradio-container label[data-testid$="-radio-label"].selected {
    background: #f4f8ff !important;
    border-color: #60a5fa !important;
}
@media (prefers-color-scheme: dark) {
    body, .gradio-container {
        background: #0f172a !important;
    }
    .gradio-container .block,
    .gradio-container .form,
    .gradio-container .accordion,
    .gradio-container .tabitem {
        background: #111827 !important;
        border-color: #273449 !important;
    }
    .gradio-container input,
    .gradio-container textarea,
    .gradio-container select,
    .gradio-container [role="combobox"] {
        background: #0b1220 !important;
        border-color: #334155 !important;
    }
    .gradio-container [role="listbox"],
    .gradio-container [role="option"] {
        background: #132037 !important;
        border-color: #334155 !important;
    }
    .gradio-container label:has(input[type="radio"]),
    .gradio-container label:has(input[type="checkbox"]),
    .gradio-container label[data-testid$="-radio-label"],
    .gradio-container label[data-testid$="-checkbox-label"] {
        background: #121c2f !important;
        border-color: #26344b !important;
    }
    .gradio-container label:has(input[type="radio"]:hover),
    .gradio-container label:has(input[type="checkbox"]:hover) {
        background: #17243b !important;
        border-color: #3b4b65 !important;
    }
    .gradio-container label:has(input[type="radio"]:checked),
    .gradio-container label:has(input[type="checkbox"]:checked),
    .gradio-container label[data-testid$="-radio-label"].selected {
        background: #1a2a44 !important;
        border-color: #60a5fa !important;
    }
    .gradio-container input[type="radio"]:checked,
    .gradio-container input[type="checkbox"]:checked {
        background: #0f172a !important;
        border-color: #60a5fa !important;
    }
    .gradio-container input[type="radio"]::before,
    .gradio-container input[type="checkbox"]::before {
        background: #93c5fd !important;
    }
}

/* Final radio treatment: subdued tiles with checkmark boxes instead of blue dot controls. */
.gradio-container label:has(input[type="radio"]),
.gradio-container label[data-testid$="-radio-label"] {
    border-radius: 7px !important;
    border: 1px solid transparent !important;
    padding: 10px 12px !important;
    gap: 9px !important;
    transition: background-color 130ms ease, border-color 130ms ease, color 130ms ease !important;
}
.gradio-container input[type="radio"] {
    width: 24px !important;
    height: 16px !important;
    min-width: 24px !important;
    min-height: 16px !important;
    max-width: 24px !important;
    max-height: 16px !important;
    border-radius: 5px !important;
    border: 1.5px solid #64748b !important;
    background: transparent !important;
    appearance: none !important;
    -webkit-appearance: none !important;
    display: inline-grid !important;
    place-content: center !important;
    flex: 0 0 24px !important;
    align-self: center !important;
    box-sizing: border-box !important;
}
.gradio-container input[type="radio"]::before {
    content: "" !important;
    width: 13px !important;
    height: 8px !important;
    display: block !important;
    transform: rotate(-45deg) translateY(-1px) !important;
    transition: border-color 130ms ease, opacity 130ms ease !important;
    background: transparent !important;
    border: solid #94a3b8 !important;
    border-width: 0 0 2px 2px !important;
    border-radius: 0 !important;
    color: transparent !important;
    font-size: 0 !important;
    line-height: 0 !important;
    opacity: 0.55 !important;
}
.gradio-container label:has(input[type="radio"]):hover,
.gradio-container label[data-testid$="-radio-label"]:hover {
    border-color: rgba(34, 197, 94, 0.72) !important;
}
.gradio-container label:has(input[type="radio"]):hover input[type="radio"],
.gradio-container label[data-testid$="-radio-label"]:hover input[type="radio"] {
    border-color: #4ade80 !important;
}
.gradio-container label:has(input[type="radio"]:checked),
.gradio-container label[data-testid$="-radio-label"].selected {
    border-color: rgba(34, 197, 94, 0.9) !important;
}
.gradio-container label:has(input[type="radio"]:checked) input[type="radio"],
.gradio-container label[data-testid$="-radio-label"].selected input[type="radio"] {
    background: #22c55e !important;
    border-color: #22c55e !important;
}
.gradio-container label:has(input[type="radio"]:checked) input[type="radio"]::before,
.gradio-container label[data-testid$="-radio-label"].selected input[type="radio"]::before {
    border-color: #ffffff !important;
    color: transparent !important;
    opacity: 1 !important;
}
@media (prefers-color-scheme: light) {
    .gradio-container label:has(input[type="radio"]),
    .gradio-container label[data-testid$="-radio-label"] {
        background: #f8fbff !important;
        border-color: #e3ebf4 !important;
    }
    .gradio-container label:has(input[type="radio"]):hover,
    .gradio-container label[data-testid$="-radio-label"]:hover {
        background: #f0fdf4 !important;
        border-color: #86efac !important;
    }
    .gradio-container label:has(input[type="radio"]:checked),
    .gradio-container label[data-testid$="-radio-label"].selected {
        background: #ecfdf5 !important;
        border-color: #22c55e !important;
        color: #14532d !important;
    }
}
@media (prefers-color-scheme: dark) {
    .gradio-container label:has(input[type="radio"]),
    .gradio-container label[data-testid$="-radio-label"] {
        background: #223044 !important;
        border-color: #334155 !important;
    }
    .gradio-container label:has(input[type="radio"]):hover,
    .gradio-container label[data-testid$="-radio-label"]:hover {
        background: #183627 !important;
        border-color: #4ade80 !important;
    }
    .gradio-container label:has(input[type="radio"]:checked),
    .gradio-container label[data-testid$="-radio-label"].selected {
        background: #163321 !important;
        border-color: #22c55e !important;
        color: #f8fafc !important;
    }
}

/* Match checkbox controls to the compact green check treatment used by the mode selector. */
.gradio-container label:has(input[type="checkbox"]),
.gradio-container label[data-testid$="-checkbox-label"] {
    border-radius: 7px !important;
    border: 1px solid transparent !important;
    padding: 10px 12px !important;
    gap: 9px !important;
    transition: background-color 130ms ease, border-color 130ms ease, color 130ms ease !important;
}
.gradio-container input[type="checkbox"] {
    width: 24px !important;
    height: 16px !important;
    min-width: 24px !important;
    min-height: 16px !important;
    max-width: 24px !important;
    max-height: 16px !important;
    border-radius: 5px !important;
    border: 1.5px solid #64748b !important;
    background: transparent !important;
    appearance: none !important;
    -webkit-appearance: none !important;
    display: inline-grid !important;
    place-content: center !important;
    flex: 0 0 24px !important;
    align-self: center !important;
    box-sizing: border-box !important;
    margin: 0 !important;
    opacity: 1 !important;
    position: static !important;
    pointer-events: auto !important;
}
.gradio-container input[type="checkbox"]::before {
    content: "" !important;
    width: 13px !important;
    height: 8px !important;
    display: block !important;
    transform: rotate(-45deg) translateY(-1px) !important;
    transition: border-color 130ms ease, opacity 130ms ease !important;
    background: transparent !important;
    border: solid #94a3b8 !important;
    border-width: 0 0 2px 2px !important;
    border-radius: 0 !important;
    color: transparent !important;
    font-size: 0 !important;
    line-height: 0 !important;
    opacity: 0.55 !important;
}
.gradio-container label:has(input[type="checkbox"]):hover,
.gradio-container label[data-testid$="-checkbox-label"]:hover {
    border-color: rgba(34, 197, 94, 0.72) !important;
}
.gradio-container label:has(input[type="checkbox"]):hover input[type="checkbox"],
.gradio-container label[data-testid$="-checkbox-label"]:hover input[type="checkbox"] {
    border-color: #4ade80 !important;
}
.gradio-container label:has(input[type="checkbox"]:checked),
.gradio-container label[data-testid$="-checkbox-label"].selected {
    border-color: rgba(34, 197, 94, 0.9) !important;
}
.gradio-container label:has(input[type="checkbox"]:checked) input[type="checkbox"],
.gradio-container label[data-testid$="-checkbox-label"].selected input[type="checkbox"] {
    background: #22c55e !important;
    border-color: #22c55e !important;
}
.gradio-container label:has(input[type="checkbox"]:checked) input[type="checkbox"]::before,
.gradio-container label[data-testid$="-checkbox-label"].selected input[type="checkbox"]::before {
    border-color: #ffffff !important;
    color: transparent !important;
    opacity: 1 !important;
}
@media (prefers-color-scheme: light) {
    .gradio-container label:has(input[type="checkbox"]),
    .gradio-container label[data-testid$="-checkbox-label"] {
        background: #f8fbff !important;
        border-color: #e3ebf4 !important;
    }
    .gradio-container label:has(input[type="checkbox"]):hover,
    .gradio-container label[data-testid$="-checkbox-label"]:hover {
        background: #f0fdf4 !important;
        border-color: #86efac !important;
    }
    .gradio-container label:has(input[type="checkbox"]:checked),
    .gradio-container label[data-testid$="-checkbox-label"].selected {
        background: #ecfdf5 !important;
        border-color: #22c55e !important;
        color: #14532d !important;
    }
}
@media (prefers-color-scheme: dark) {
    .gradio-container label:has(input[type="checkbox"]),
    .gradio-container label[data-testid$="-checkbox-label"] {
        background: #223044 !important;
        border-color: #334155 !important;
    }
    .gradio-container label:has(input[type="checkbox"]):hover,
    .gradio-container label[data-testid$="-checkbox-label"]:hover {
        background: #183627 !important;
        border-color: #4ade80 !important;
    }
    .gradio-container label:has(input[type="checkbox"]:checked),
    .gradio-container label[data-testid$="-checkbox-label"].selected {
        background: #163321 !important;
        border-color: #22c55e !important;
        color: #f8fafc !important;
    }
}
/* Gradio 6 renders the control inputs themselves reliably.  Older card-style
   rules above predate that markup and still set a 24px custom indicator late
   in this stylesheet.  End the cascade with one native, accessible geometry
   for every ordinary radio and checkbox; the output-mode exception below
   deliberately keeps its smaller inline radio. */
.gradio-container label:has(input[type="radio"]) input[type="radio"],
.gradio-container label[data-testid$="-radio-label"] input[type="radio"] {
    appearance: auto !important;
    -webkit-appearance: radio !important;
    width: 17px !important;
    height: 17px !important;
    min-width: 17px !important;
    min-height: 17px !important;
    max-width: 17px !important;
    max-height: 17px !important;
    margin: 0 !important;
    padding: 0 !important;
    border: 0 !important;
    border-radius: 50% !important;
    background: initial !important;
    opacity: 1 !important;
    position: static !important;
    pointer-events: auto !important;
    accent-color: #2563eb !important;
}
.gradio-container label:has(input[type="checkbox"]) input[type="checkbox"],
.gradio-container label[data-testid$="-checkbox-label"] input[type="checkbox"] {
    appearance: auto !important;
    -webkit-appearance: checkbox !important;
    width: 17px !important;
    height: 17px !important;
    min-width: 17px !important;
    min-height: 17px !important;
    max-width: 17px !important;
    max-height: 17px !important;
    margin: 0 !important;
    padding: 0 !important;
    border: 0 !important;
    border-radius: 0 !important;
    background: initial !important;
    opacity: 1 !important;
    position: static !important;
    pointer-events: auto !important;
    accent-color: #16a34a !important;
}
.gradio-container label:has(input[type="radio"]) input[type="radio"]::before,
.gradio-container label:has(input[type="checkbox"]) input[type="checkbox"]::before,
.gradio-container label[data-testid$="-radio-label"] input[type="radio"]::before,
.gradio-container label[data-testid$="-checkbox-label"] input[type="checkbox"]::before {
    content: none !important;
}
/* Output mode should use normal radios instead of the app-wide tile treatment. */
#output-mode-radio label:has(input[type="radio"]),
#output-mode-radio label[data-testid$="-radio-label"] {
    display: inline-flex !important;
    align-items: center !important;
    gap: 8px !important;
    min-height: 0 !important;
    padding: 2px 14px 2px 0 !important;
    border: 0 !important;
    border-radius: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
}
#output-mode-radio label:has(input[type="radio"]):hover,
#output-mode-radio label[data-testid$="-radio-label"]:hover,
#output-mode-radio label:has(input[type="radio"]:checked),
#output-mode-radio label[data-testid$="-radio-label"].selected {
    border: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
    color: var(--body-text-color) !important;
}
#output-mode-radio input[type="radio"] {
    appearance: auto !important;
    -webkit-appearance: radio !important;
    width: 15px !important;
    height: 15px !important;
    min-width: 15px !important;
    border: 0 !important;
    border-radius: 50% !important;
    background: transparent !important;
    box-shadow: none !important;
    accent-color: var(--color-accent) !important;
}
#output-mode-radio input[type="radio"]::before {
    content: none !important;
}
body:not(.dark) .gradio-container button.secondary,
body:not(.dark) .gradio-container button:not(.primary) {
    border-color: #e0e7ef !important;
}
body:not(.dark) .gradio-container input,
body:not(.dark) .gradio-container textarea,
body:not(.dark) .gradio-container select,
body:not(.dark) .gradio-container [role="combobox"] {
    border-color: #e2e8f0 !important;
}
body:not(.dark) .gradio-container label:has(input[type="radio"]),
body:not(.dark) .gradio-container label:has(input[type="checkbox"]),
body:not(.dark) .gradio-container label[data-testid$="-radio-label"],
body:not(.dark) .gradio-container label[data-testid$="-checkbox-label"] {
    background: #f7faff !important;
    border-color: #e3ebf4 !important;
}
#automatic-run-confirmation {
    margin: 0 !important;
    padding: 0 !important;
    border: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
}
#automatic-run-confirmation > div,
#automatic-run-confirmation .wrap {
    margin: 0 !important;
    padding: 0 !important;
    border: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
}
.automatic-confirmation-summary {
    color: var(--body-text-color);
    margin: 0 !important;
    padding: 0 !important;
    font-size: 0.88em;
    line-height: 1.25;
}
.automatic-run-timing {
    display: block !important;
    width: 100% !important;
    min-height: 1.25em;
    margin: 0 !important;
    padding: 3px 0 0 !important;
    border: 0 !important;
    border-radius: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
    font-size: 0.92em;
    font-variant-numeric: tabular-nums;
}
.automatic-run-timing-host,
.automatic-run-timing-host > div,
.automatic-run-timing-host > .wrap,
.automatic-run-timing-host .wrap,
.automatic-run-timing-host .html,
.automatic-run-timing-host .block {
    display: block !important;
    width: 100% !important;
    margin: 0 !important;
    padding: 0 !important;
    border: 0 !important;
    border-color: transparent !important;
    border-radius: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
    outline: 0 !important;
}
.automatic-run-activity-host,
.automatic-run-activity-host > div,
.automatic-run-activity-host > .wrap,
.automatic-run-activity-host .wrap,
.automatic-run-activity-host .html,
.automatic-run-activity-host .block,
.automatic-run-activity {
    display: block !important;
    width: 100% !important;
    box-sizing: border-box;
    margin: 0 !important;
    padding: 2px 0 0 !important;
    border: 0 !important;
    border-radius: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
    font-size: 0.96em;
    line-height: 1.25;
}
.automatic-run-activity.warning { color: #a16207; }
.automatic-run-activity.failed { color: #b91c1c; }
.automatic-run-activity.ready { color: var(--body-text-color-subdued, #64748b); }
.automatic-run-progress {
    height: 8px;
    width: 100%;
    overflow: hidden;
    border-radius: 999px;
    background: color-mix(in srgb, var(--border-color-primary) 36%, transparent);
}
.automatic-run-progress-fill {
    height: 100%;
    min-width: 0;
    border-radius: inherit;
    background: #3b82f6;
    transition: width 0.45s ease-out;
}
.automatic-run-activity.warning .automatic-run-progress-fill { background: #d97706; }
.automatic-run-activity.failed .automatic-run-progress-fill { background: #dc2626; }
.automatic-run-activity.ready .automatic-run-progress-fill { background: #64748b; }
.automatic-run-progress-label {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    column-gap: 5px;
    row-gap: 0;
    min-height: 1.25em;
    padding-top: 3px;
}
.automatic-run-progress-label span { font-variant-numeric: tabular-nums; }
.automatic-run-activity.preparing .automatic-run-progress-label {
    display: block;
    min-height: 2.45em;
    padding-top: 3px;
    line-height: 1.2;
}
.automatic-run-activity.preparing .automatic-run-progress-label strong,
.automatic-run-activity.preparing .automatic-run-progress-label span {
    display: block;
}
.automatic-run-activity.preparing .automatic-run-progress-label span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.automatic-run-progress-timing {
    /* The duration is deliberately a compact second line. It must never
       move horizontally when a variable-length activity description wraps. */
    display: block;
    flex-basis: 100%;
    margin-left: 0;
    margin-top: -1px;
    line-height: 1.15;
    color: var(--body-text-color-subdued, #64748b);
    font-size: .92em;
}
.dark .automatic-run-activity.warning,
body.dark .automatic-run-activity.warning { color: #fcd34d; }
.dark .automatic-run-activity.failed,
body.dark .automatic-run-activity.failed { color: #fca5a5; }
#automatic-run-failure,
#automatic-run-failure > div,
#automatic-run-failure .wrap {
    margin: 0 !important;
    padding: 3px 0 0 !important;
    border: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
}
.automatic-run-failure {
    color: #b91c1c;
    font-size: 0.88em;
    line-height: 1.3;
}
.dark .automatic-run-failure,
body.dark .automatic-run-failure,
gradio-app.dark .automatic-run-failure {
    color: #fca5a5;
}
.automatic-confirmation-title {
    margin-bottom: 8px;
    font-weight: 700;
    color: var(--body-text-color);
}
.automatic-confirmation-warnings {
    margin: 10px 0 0 18px;
    color: #92400e;
}
.automatic-confirmation-note {
    display: block;
    margin-top: 6px;
    color: var(--body-text-color-subdued);
}
.automatic-run-timing-detail {
    display: inline;
    margin-left: 6px;
    color: var(--body-text-color-subdued);
}
.automatic-run-timing.successful {
    border: 0 !important;
    background: transparent !important;
}
.automatic-run-timing.warning {
    border: 0 !important;
    background: transparent !important;
}
.automatic-run-timing.failed {
    border: 0 !important;
    background: transparent !important;
}
#automatic-process-button button.rag-run-success,
#confirm-automatic-run-button button.rag-run-success {
    background: #16a34a !important;
    border-color: #16a34a !important;
    color: #ffffff !important;
}
#automatic-process-button button.rag-run-warning,
#confirm-automatic-run-button button.rag-run-warning {
    background: #f59e0b !important;
    border-color: #f59e0b !important;
    color: #1f2937 !important;
}
#automatic-process-button button.rag-run-failed,
#confirm-automatic-run-button button.rag-run-failed {
    background: #dc2626 !important;
    border-color: #dc2626 !important;
    color: #ffffff !important;
}
/* Processing is a state label, not a disabled control. The mounted Confirm
   button is otherwise visibly inactive before Review or after a reset. */
#confirm-automatic-run-button button.rag-run-processing,
#confirm-automatic-run-button button.rag-run-processing:disabled {
    background: #2563eb !important;
    border-color: #2563eb !important;
    color: #ffffff !important;
    opacity: 1 !important;
}
#cancel-automatic-run-button button {
    border: 1px solid #94a3b8 !important;
    background: #f1f5f9 !important;
}
body.dark #automatic-actions {
    background: #1e293b !important;
    border-color: #475569 !important;
}
body.dark #cancel-automatic-run-button button {
    background: #334155 !important;
    border-color: #64748b !important;
}
#cancel-automatic-run-button button.rag-cancel-deferred,
#cancel-automatic-run-button button.rag-cancel-deferred:disabled {
    background: #e5e7eb !important;
    border-color: #d1d5db !important;
    color: #4b5563 !important;
    opacity: 1 !important;
}
body.dark #cancel-automatic-run-button button.rag-cancel-deferred,
body.dark #cancel-automatic-run-button button.rag-cancel-deferred:disabled {
    background: #475569 !important;
    border-color: #64748b !important;
    color: #e2e8f0 !important;
}
.dark .automatic-run-timing,
body.dark .automatic-run-timing,
gradio-app.dark .automatic-run-timing {
    background: transparent !important;
    border: 0 !important;
}
/* Gradio scopes the generic timer rule more strongly than a class-only dark
   selector. Keep this as a text status, not a button-like surface. */
body.dark #automatic-run-timing.automatic-run-timing {
    background: transparent !important;
    border: 0 !important;
    color: inherit !important;
    box-shadow: none !important;
}
body.dark #automatic-run-timing.automatic-run-timing strong,
body.dark #automatic-run-timing.automatic-run-timing .automatic-run-timing-detail {
    color: inherit !important;
}
"""


def default_anythingllm_storage_dir():
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "anythingllm-desktop" / "storage"
    return Path.home() / "AppData" / "Roaming" / "anythingllm-desktop" / "storage"


def default_anythingllm_documents_dir():
    return default_anythingllm_storage_dir() / "documents"


def local_workspace_choices():
    db_path = default_anythingllm_storage_dir() / "anythingllm.db"
    if not db_path.exists():
        return [], f"Local AnythingLLM database not found at {db_path}."

    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "select id,name,slug,chatProvider,chatModel from workspaces order by id"
        ).fetchall()
        con.close()
    except Exception as exc:
        return [], f"Could not read local AnythingLLM workspaces from {db_path}: {exc}"

    choices = []
    for row in rows:
        slug = row["slug"]
        if not slug:
            continue
        name = row["name"] or slug
        model = row["chatModel"]
        provider = row["chatProvider"]
        model_suffix = f" - {provider} / {model}" if (provider or model) else ""
        choices.append((f"{name} ({slug}){model_suffix}", slug))

    if not choices:
        return [], f"Local AnythingLLM database was found at {db_path}, but it has no workspaces."
    return choices, f"Found {len(choices)} workspace(s) from local AnythingLLM Desktop database: {db_path}"


def preferred_workspace_slug(choices):
    for _, slug in choices:
        if slug and slug not in {"assistant-chats", NEW_DOCUMENT_WORKSPACE_VALUE}:
            return slug
    return next(
        (slug for _, slug in choices if slug and slug != NEW_DOCUMENT_WORKSPACE_VALUE),
        None,
    )


def workspace_choices_with_new_document(choices, new_first=True):
    existing = [
        choice
        for choice in (choices or [])
        if len(choice) >= 2 and choice[1] != NEW_DOCUMENT_WORKSPACE_VALUE
    ]
    action = (NEW_DOCUMENT_WORKSPACE_LABEL, NEW_DOCUMENT_WORKSPACE_VALUE)
    return [action, *existing] if new_first else [*existing, action]


def is_new_document_workspace_choice(workspace_slug):
    return (workspace_slug or "").strip() == NEW_DOCUMENT_WORKSPACE_VALUE


def workspace_update_from_local(prefix="", auto_select=True, include_new_document_choice=False):
    choices, status = local_workspace_choices()
    if include_new_document_choice:
        choices = workspace_choices_with_new_document(choices)
    value = (
        preferred_workspace_slug(choices)
        if auto_select
        else NEW_DOCUMENT_WORKSPACE_VALUE
        if include_new_document_choice
        else None
    )
    if prefix:
        status = f"{prefix}\n{status}"
    if choices and not auto_select:
        status += (
            "\nA new document workspace will be created after confirmation."
            if include_new_document_choice
            else "\nSelect a target workspace explicitly before native upload."
        )
    return gr.update(choices=choices, value=value), status


def native_upload_readiness_report(
    api_url="",
    api_key="",
    workspace_slug="",
    upload_result=None,
    autostart_runtime=False,
    verify_authentication=False,
    status_callback=None,
):
    storage_dir = default_anythingllm_storage_dir()
    db_path = storage_dir / "anythingllm.db"
    report = {
        "local_db_found": db_path.exists(),
        "local_db_message": (
            f"Found local AnythingLLM database at {db_path}."
            if db_path.exists()
            else f"Local AnythingLLM database not found at {db_path}."
        ),
        "workspace_slug": (workspace_slug or "").strip(),
        "workspace_slug_found": None,
        "workspace_slug_message": "No workspace selected.",
        "workspace_api_found": None,
        "workspace_api_message": "Live API workspace check not run.",
        "runtime_api_url": (api_url or "").strip() or DEFAULT_ANYTHINGLLM_API_URL,
        "runtime_api_reachable": False,
        "runtime_api_status": "not_checked",
        "runtime_api_message": "Runtime API not checked yet.",
        "runtime_start_status": "not_attempted",
        "runtime_start_message": "Desktop runtime start was not attempted.",
        "authenticated": None,
        "authentication_status": "not_checked",
        "authentication_message": "Authentication not checked yet.",
        "upload_succeeded": None,
        "upload_status": "not_run",
        "upload_message": "No upload has run in this session.",
    }
    if is_new_document_workspace_choice(report["workspace_slug"]):
        report["workspace_slug_found"] = None
        report["workspace_slug_message"] = (
            "A new workspace for this document will be created only after you confirm the run."
        )
    elif report["workspace_slug"]:
        workspace_found, workspace_message = local_workspace_slug_exists(report["workspace_slug"])
        report["workspace_slug_found"] = workspace_found
        report["workspace_slug_message"] = workspace_message
    runtime = ensure_anythingllm_runtime(
        report["runtime_api_url"],
        api_key=(api_key or "").strip(),
        timeout=1.25 if autostart_runtime else 0.5,
        autostart_local=bool(autostart_runtime),
        status_callback=status_callback,
    )
    report["runtime_api_url"] = runtime.get("api_url") or report["runtime_api_url"]
    report["runtime_api_status"] = runtime.get("status") or "not_checked"
    report["runtime_api_reachable"] = report["runtime_api_status"] in {"reachable", "reachable_auth_required"}
    report["runtime_api_message"] = runtime.get("message") or "Runtime API not checked yet."
    start = runtime.get("start") or {}
    report["runtime_start_status"] = start.get("status") or "not_attempted"
    if report["runtime_start_status"] == "started":
        report["runtime_start_message"] = f"Started AnythingLLM Desktop from {start.get('executable') or 'the installed executable'}."
    elif report["runtime_start_status"] == "already_running":
        report["runtime_start_message"] = "AnythingLLM Desktop process was already running."
    elif report["runtime_start_status"] == "missing_executable":
        report["runtime_start_message"] = start.get("error") or "AnythingLLM Desktop executable was not found."
    elif report["runtime_start_status"] == "start_failed":
        report["runtime_start_message"] = start.get("error") or "AnythingLLM Desktop could not be started."
    if report["runtime_api_reachable"] and verify_authentication:
        auth = verify_anythingllm_upload_auth(report["runtime_api_url"], api_key=(api_key or "").strip())
        report["authenticated"] = bool(auth.get("authenticated"))
        report["authentication_status"] = auth.get("status") or "not_checked"
        report["authentication_message"] = auth.get("message") or "Authentication not checked yet."
        if report["authenticated"] and report["workspace_slug"] and not is_new_document_workspace_choice(report["workspace_slug"]):
            api_found, api_message = api_workspace_slug_exists(
                report["runtime_api_url"],
                (api_key or "").strip(),
                report["workspace_slug"],
            )
            report["workspace_api_found"] = api_found
            report["workspace_api_message"] = api_message
            if api_found is True:
                # A successful live query is decisive when Desktop's SQLite
                # writer has not yet exposed the just-created row to a reader.
                report["workspace_slug_found"] = True
                report["workspace_slug_message"] = (
                    f"{api_message} Workspace exists in the live API"
                    + ("; local storage is still catching up." if workspace_found is not True else ".")
                )
            elif api_found is False:
                report["workspace_slug_found"] = False
                report["workspace_slug_message"] = f"{api_message} The selected workspace is not present."
    elif report["runtime_api_status"] == "collector_stub":
        report["authentication_status"] = "not_applicable"
        report["authentication_message"] = "The detected endpoint is only the plain-text health stub; upload auth cannot be checked there."
    if isinstance(upload_result, dict):
        upload_status = upload_result.get("status") or "not_run"
        report["upload_status"] = upload_status
        report["upload_succeeded"] = upload_status in {"complete", "complete_with_key_cleanup_warning"}
        if report["upload_succeeded"]:
            report["upload_message"] = (
                f"Upload succeeded: {upload_result.get('uploaded', 0)} uploaded, "
                f"{upload_result.get('embedded', 0)} embedded."
            )
        else:
            errors = upload_result.get("errors") or []
            first_error = ""
            if errors and isinstance(errors[0], dict):
                first_error = str(errors[0].get("error") or errors[0].get("details") or "").strip()
            report["upload_message"] = first_error or f"Upload did not succeed ({upload_status})."
    return report


def native_upload_readiness_html(report=None):
    report = report or native_upload_readiness_report()

    def state_label(value):
        if value is True:
            return "yes", "pass"
        if value is False:
            return "no", "fail"
        return "not run", "pending"

    rows = []
    runtime_detail = f"{report.get('runtime_api_url')}: {report.get('runtime_api_message')}"
    if report.get("runtime_start_status") not in {"", "not_attempted"}:
        runtime_detail += f" {report.get('runtime_start_message') or ''}".rstrip()
    for title, value, detail in [
        ("Local DB found", report.get("local_db_found"), report.get("local_db_message")),
        ("Workspace slug found", report.get("workspace_slug_found"), report.get("workspace_slug_message")),
        ("Runtime API reachable", report.get("runtime_api_reachable"), runtime_detail),
        ("Authenticated", report.get("authenticated"), report.get("authentication_message")),
        ("Upload succeeded", report.get("upload_succeeded"), report.get("upload_message")),
    ]:
        label, css = state_label(value)
        rows.append(
            '<div class="native-upload-readiness-row">'
            f'<div class="native-upload-readiness-key">{html.escape(str(title))}</div>'
            f'<div class="native-upload-readiness-state {css}">{html.escape(label)}</div>'
            f'<div class="native-upload-readiness-detail">{html.escape(str(detail or ""))}</div>'
            '</div>'
        )
    return (
        '<div class="native-upload-readiness-panel">'
        '<div class="native-upload-readiness-title">Native upload readiness</div>'
        + "".join(rows)
        + "</div>"
    )


def api_headers(api_key):
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def api_get_json(api_url, path, api_key, timeout=20):
    req = urllib.request.Request(api_url.rstrip("/") + path, headers=api_headers(api_key))
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.status, json.loads(response.read().decode("utf-8", errors="replace"))


def api_workspace_slug_exists(api_url, api_key, slug, timeout=5):
    """Confirm a workspace through the live API without exposing a key.

    The Desktop SQLite database is useful retained evidence, but the runtime
    API is authoritative after a workspace has just been created or Desktop is
    still flushing its local write.  This helper never creates credentials or
    mutates AnythingLLM; it uses the already-managed local service key when
    one is available.
    """
    normalized_slug = str(slug or "").strip()
    if not normalized_slug:
        return False, "No workspace slug was provided."
    normalized_api_url = str(api_url or "").strip().rstrip("/")
    if not normalized_api_url:
        return None, "AnythingLLM API URL is missing."
    runtime_key, _mode = resolve_anythingllm_api_key(normalized_api_url, (api_key or "").strip() or None)
    if not runtime_key:
        return None, "No managed AnythingLLM API credential was available for workspace verification."
    try:
        status, data = api_get_json(
            normalized_api_url,
            "/api/v1/workspaces",
            runtime_key,
            timeout=timeout,
        )
        if not 200 <= int(status) < 300:
            return None, f"AnythingLLM returned HTTP {status} while listing workspaces."
        workspaces = data.get("workspaces") if isinstance(data, dict) else None
        if not isinstance(workspaces, list):
            return None, "AnythingLLM returned an invalid workspace list."
        found = any(str(item.get("slug") or "").strip() == normalized_slug for item in workspaces if isinstance(item, dict))
        return found, f"Checked live AnythingLLM API at {normalized_api_url}."
    except Exception as exc:
        return None, describe_api_exception(exc, "AnythingLLM")


def describe_api_exception(exc, service_name):
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = exc.read().decode("utf-8", errors="replace").strip()
        except Exception:
            body = ""
        finally:
            exc.close()
        status_labels = {
            400: "bad request or malformed API payload",
            401: "authentication required or API key missing",
            403: "API key rejected or access denied",
            404: "wrong API path, wrong base URL, or resource not found",
            409: "conflict with current server state",
            413: "payload too large for this endpoint",
            415: "unsupported media type",
            422: "request understood but rejected by validation",
            429: "rate limit or too many requests",
            500: "server-side error",
            502: "bad gateway or upstream provider error",
            503: "service unavailable or still starting",
            504: "gateway timeout or upstream provider timed out",
        }
        label = status_labels.get(exc.code, "unexpected HTTP response")
        detail = f"{service_name} API returned HTTP {exc.code}: {label}."
        if body:
            detail += f" Response body: {body[:700]}"
        return detail
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", exc)
        reason_text = str(reason)
        lower = reason_text.casefold()
        if "10061" in reason_text or "connection refused" in lower or "actively refused" in lower:
            return (
                f"{service_name} API is not reachable at the configured URL. "
                "This usually means the desktop app/API server is not running, is still starting, or is using another port."
            )
        if "timed out" in lower or "timeout" in lower:
            return f"{service_name} API did not respond before the timeout. The app may be starting slowly or blocked."
        if "name or service not known" in lower or "getaddrinfo failed" in lower:
            return f"{service_name} API host could not be resolved. Check the URL."
        if "certificate" in lower or "ssl" in lower:
            return f"{service_name} API connection failed during TLS/SSL validation. Check whether the URL should be http:// rather than https://, or whether a local certificate is trusted."
        if "connection reset" in lower or "forcibly closed" in lower or "10054" in reason_text:
            return f"{service_name} API connection was reset while reading the response. The server may have crashed, restarted, or closed the request."
        if "network is unreachable" in lower or "no route to host" in lower or "10051" in reason_text:
            return f"{service_name} API network route is unavailable. Check network/VPN/firewall state and the configured host."
    if isinstance(exc, TimeoutError):
        return f"{service_name} API timed out before returning a response."
    if isinstance(exc, ValueError):
        return f"{service_name} API URL or response was invalid: {exc}"
    return f"{service_name} API query failed: {exc}"


def refresh_workspaces(
    api_url,
    api_key,
    autostart_runtime=False,
    auto_select=True,
    include_new_document_choice=False,
):
    resolution = ensure_anythingllm_runtime(
        api_url,
        api_key=(api_key or "").strip(),
        timeout=1.25,
        autostart_local=bool(autostart_runtime),
    )
    api_url = resolution.get("api_url") or (api_url or "").strip() or DEFAULT_ANYTHINGLLM_API_URL
    start = resolution.get("start") or {}
    start_note = (
        "Started AnythingLLM Desktop and waited for the local API.\n"
        if start.get("status") == "started"
        else "AnythingLLM Desktop was already running; waited for the local API.\n"
        if start.get("status") == "already_running" and resolution.get("waited_for_runtime")
        else ""
    )
    resolution_note = (
        start_note + f"Using detected AnythingLLM API URL `{api_url}`.\n"
        if resolution.get("status") in {"reachable", "reachable_auth_required"}
        else start_note
    )
    try:
        _, data = api_get_json(api_url, "/api/v1/workspaces", (api_key or "").strip())
        workspaces = data.get("workspaces") or []
        choices = [
            (f"{workspace.get('name') or workspace.get('slug')} ({workspace.get('slug')})", workspace.get("slug"))
            for workspace in workspaces
            if workspace.get("slug")
        ]
        if not choices:
            return workspace_update_from_local(
                resolution_note + "API connected, but returned no workspaces. Falling back to local desktop database.",
                auto_select=auto_select,
                include_new_document_choice=include_new_document_choice,
            )
        if include_new_document_choice:
            choices = workspace_choices_with_new_document(choices)
        selected = (
            preferred_workspace_slug(choices)
            if auto_select
            else NEW_DOCUMENT_WORKSPACE_VALUE
            if include_new_document_choice
            else None
        )
        selection_message = (
            f"Auto-selected `{selected}`."
            if selected
            else "Select a target workspace explicitly before native upload."
        )
        if include_new_document_choice and not auto_select:
            selection_message = "A new document workspace will be created after confirmation."
        return gr.update(choices=choices, value=selected), (
            resolution_note + f"Found {len(choices)} workspace(s). {selection_message}"
        )
    except urllib.error.HTTPError as exc:
        return workspace_update_from_local(
            resolution_note + f"{describe_api_exception(exc, 'AnythingLLM')}\nUsing read-only local database fallback.",
            auto_select=auto_select,
            include_new_document_choice=include_new_document_choice,
        )
    except Exception as exc:
        return workspace_update_from_local(
            resolution_note + f"{describe_api_exception(exc, 'AnythingLLM')}\nUsing read-only local database fallback.",
            auto_select=auto_select,
            include_new_document_choice=include_new_document_choice,
        )


def load_workspaces_on_open():
    return workspace_update_from_local(
        "Loaded workspaces automatically from local AnythingLLM Desktop storage.",
        auto_select=False,
        include_new_document_choice=True,
    )


def initialize_anythingllm_on_app_open(api_url="", api_key="", workspace_slug=""):
    """Perform the one allowed Desktop start attempt while the app is opening.

    The top-of-page status previously observed Desktop but did not invoke the
    already-existing local-start helper.  That left an initially red warning in
    place until a later workspace refresh or upload preflight happened to start
    Desktop.  App open is a useful, explicit lifecycle boundary: make one
    bounded attempt there for a local endpoint, then leave the recurring status
    timer strictly observational.  This neither authenticates nor creates an
    API key, and remote URLs remain excluded by ``ensure_anythingllm_runtime``.
    """
    workspace_update, workspace_status = load_workspaces_on_open()
    selected_workspace = (
        (workspace_slug or "").strip()
        or str(workspace_update.get("value") or "").strip()
    )
    report = native_upload_readiness_report(
        api_url,
        api_key,
        selected_workspace,
        autostart_runtime=True,
        verify_authentication=False,
    )
    reachable = bool(report.get("runtime_api_reachable"))
    status_html = anythingllm_startup_status_html(
        report.get("runtime_api_url") or api_url,
        health={"reachable": reachable},
    )
    return (
        workspace_update,
        workspace_status,
        native_upload_readiness_html(report),
        status_html,
        gr.update(visible=not reachable),
    )


def initial_native_upload_readiness_report():
    storage_dir = default_anythingllm_storage_dir()
    db_path = storage_dir / "anythingllm.db"
    workspace_slug = INITIAL_WORKSPACE_VALUE or ""
    workspace_found = None
    workspace_message = "No workspace selected."
    if is_new_document_workspace_choice(workspace_slug):
        workspace_message = "A new workspace for this document will be created after confirmation."
    elif workspace_slug:
        workspace_found, workspace_message = local_workspace_slug_exists(workspace_slug)
    return {
        "local_db_found": db_path.exists(),
        "local_db_message": (
            f"Found local AnythingLLM database at {db_path}."
            if db_path.exists()
            else f"Local AnythingLLM database not found at {db_path}."
        ),
        "workspace_slug": workspace_slug,
        "workspace_slug_found": workspace_found,
        "workspace_slug_message": workspace_message,
        "runtime_api_url": DEFAULT_ANYTHINGLLM_API_URL,
        "runtime_api_reachable": None,
        "runtime_api_status": "not_checked",
        "runtime_api_message": "Runtime API not checked yet.",
        "runtime_start_status": "not_attempted",
        "runtime_start_message": "Desktop runtime start was not attempted.",
        "authenticated": None,
        "authentication_status": "not_checked",
        "authentication_message": "Authentication not checked yet.",
        "upload_succeeded": None,
        "upload_status": "not_run",
        "upload_message": "No upload has run in this session.",
    }


def refresh_workspaces_with_readiness(api_url, api_key, workspace_slug):
    workspace_update, workspace_status = refresh_workspaces(
        api_url,
        api_key,
        autostart_runtime=True,
        auto_select=False,
        include_new_document_choice=True,
    )
    readiness = native_upload_readiness_html(
        native_upload_readiness_report(
            api_url,
            api_key,
            workspace_slug or workspace_update.get("value"),
            autostart_runtime=True,
            verify_authentication=True,
        )
    )
    return workspace_update, workspace_status, readiness


def document_workspace_name(document_label, pdf_files):
    files = normalize_file_list(pdf_files)
    source_name = (document_label or "").strip()
    if not source_name and files:
        source_name = Path(files[0]).stem
    if not source_name:
        return ""
    clean_name = re.sub(r"\s+", " ", source_name).strip()[:72]
    return f"PDF — {clean_name} — {datetime.now().strftime('%Y-%m-%d')}"


def suggested_document_workspace_name(document_label, pdf_files):
    return lancedb_safe_workspace_name(document_workspace_name(document_label, pdf_files))


def update_new_workspace_name_control(workspace_slug, document_label, pdf_files, current_name, last_suggestion):
    """Show an editable proposed name only when the new-workspace choice is active."""
    if str((LIVE_AUTOMATIC_RUN_STATUS or {}).get("state") or "") == "running":
        # A selection can arrive while another run is still executing.  The
        # next-run name must not replace the name being reported for that live
        # run; the next explicit selection will refresh it after completion.
        return gr.update(), gr.update()
    if not is_new_document_workspace_choice(workspace_slug):
        return gr.update(visible=False), last_suggestion or ""
    suggestion = suggested_document_workspace_name(document_label, pdf_files)
    current = str(current_name or "").strip()
    # Preserve a deliberate edit. Metadata refreshes may happen after upload,
    # so only replace an empty field or the previous automatic suggestion.
    value = suggestion if not current or current == str(last_suggestion or "") else current
    return gr.update(value=value, visible=True, interactive=True), suggestion


def create_new_document_workspace(api_url, api_key, document_label, pdf_files, workspace_name_override=""):
    workspace_name = str(workspace_name_override or "").strip() or document_workspace_name(document_label, pdf_files)
    if not workspace_name:
        return {
            "status": "missing_document_name",
            "error": "Choose a PDF or enter a document title before creating a workspace.",
        }
    # Workspace creation used to run before ``run_automatic`` reached its
    # normal Desktop-runtime gate. A stopped AnythingLLM Desktop instance then
    # surfaced as the misleading generic AUTO-WORKSPACE-004 failure. Resolve
    # and, when appropriate, start the local runtime before this irreversible
    # workspace mutation so the returned endpoint/key are known-good.
    runtime = ensure_anythingllm_runtime(
        (api_url or "").strip() or DEFAULT_ANYTHINGLLM_API_URL,
        api_key=(api_key or "").strip(),
        timeout=1.25,
        autostart_local=True,
    )
    resolved_api_url = runtime.get("api_url") or (api_url or "").strip() or DEFAULT_ANYTHINGLLM_API_URL
    if runtime.get("status") not in {"reachable", "reachable_auth_required"}:
        start = runtime.get("start") or {}
        error_parts = [runtime.get("message") or "AnythingLLM local runtime did not become reachable."]
        if start.get("error"):
            error_parts.append(str(start["error"]))
        return {
            "status": "runtime_unavailable",
            "workspace_slug": "",
            "workspace_name": workspace_name,
            "runtime": runtime,
            "error": " ".join(error_parts),
        }
    authentication = verify_anythingllm_upload_auth(
        resolved_api_url,
        api_key=(api_key or "").strip() or None,
    )
    if not authentication.get("authenticated"):
        return {
            "status": "authentication_required",
            "workspace_slug": "",
            "workspace_name": workspace_name,
            "runtime": runtime,
            "authentication": authentication,
            "error": authentication.get("message") or "AnythingLLM authentication could not be verified.",
        }
    result = create_validation_workspace(
        resolved_api_url,
        api_key=(api_key or "").strip() or None,
        top_n=8,
        storage_dir=default_anythingllm_storage_dir(),
        workspace_name=workspace_name,
    )
    result["runtime"] = runtime
    result["authentication"] = authentication
    if result.get("status") == "created":
        result["desktop_refresh"] = request_desktop_workspace_refresh()
    return result


def create_document_workspace_for_upload(api_url, api_key, document_label, pdf_files, workspace_name_override=""):
    workspace_name = str(workspace_name_override or "").strip() or document_workspace_name(document_label, pdf_files)
    result = create_new_document_workspace(api_url, api_key, document_label, pdf_files, workspace_name_override)
    if result.get("status") != "created" or not result.get("workspace_slug"):
        return (
            gr.update(),
            f"Could not create workspace `{workspace_name}`: {result.get('error') or result.get('status')}",
            native_upload_readiness_html(
                native_upload_readiness_report(api_url, api_key, "", autostart_runtime=False)
            ),
        )
    choices, local_status = local_workspace_choices()
    slug = result["workspace_slug"]
    if slug not in [choice_slug for _, choice_slug in choices]:
        choices.append((f"{result.get('workspace_name') or workspace_name} ({slug})", slug))
    choices = workspace_choices_with_new_document(choices, new_first=False)
    readiness = native_upload_readiness_html(
        native_upload_readiness_report(
            api_url,
            api_key,
            slug,
            autostart_runtime=False,
            verify_authentication=True,
        )
    )
    desktop_refresh = result.get("desktop_refresh") or {}
    desktop_note = desktop_workspace_refresh_note(desktop_refresh)
    return (
        gr.update(choices=choices, value=slug),
        f"Created and selected workspace `{result.get('workspace_name') or workspace_name}` ({slug}).\n{local_status}\n{desktop_note}",
        readiness,
    )


def initial_workspace_controls():
    choices, status = local_workspace_choices()
    choices = workspace_choices_with_new_document(choices)
    selected = NEW_DOCUMENT_WORKSPACE_VALUE
    if choices:
        status = (
            "Loaded initial workspace choices from local AnythingLLM Desktop storage.\n"
            + status
            + "\nNew workspace for this document is selected. It will be created only after confirmation."
        )
    return choices, selected, status


def refresh_native_upload_readiness(api_url, api_key, workspace_slug, autostart_runtime=False):
    return native_upload_readiness_html(
        native_upload_readiness_report(
            api_url,
            api_key,
            workspace_slug,
            autostart_runtime=autostart_runtime,
        )
    )


def ollama_base_url(value):
    base = (value or "").strip() or DEFAULT_OLLAMA_URL
    for suffix in ("/api/embed", "/api/embeddings", "/api/tags"):
        if base.rstrip("/").endswith(suffix):
            base = base.rstrip("/")[: -len(suffix)]
            break
    return base.rstrip("/")


def is_embedding_like_model(name):
    lowered = name.casefold()
    hints = ["embed", "embedding", "bge", "nomic", "mxbai", "e5", "gte", "jina", "snowflake"]
    return any(hint in lowered for hint in hints)


def normalize_simulation_choice(value):
    choice = (value or SIMULATION_SKIP_LABEL).strip()
    lowered = choice.casefold()
    if lowered in {
        "anythingllm",
        "anythingllm default",
        "default anythingllm model",
        "default anythingllm embedder",
        SIMULATION_ANYTHINGLLM_DEFAULT_LABEL.casefold(),
    }:
        return SIMULATION_ANYTHINGLLM_DEFAULT_LABEL
    if lowered.startswith(SIMULATION_ANYTHINGLLM_DEFAULT_PREFIX.casefold()):
        return SIMULATION_ANYTHINGLLM_DEFAULT_LABEL
    if lowered in {
        "none",
        "off",
        "skip",
        "skip simulation",
        "skip vector simulation",
        "skip retrieval",
        SIMULATION_SKIP_LABEL.casefold(),
    }:
        return SIMULATION_SKIP_LABEL
    if choice.startswith("Ollama: "):
        return choice[len("Ollama: ") :].strip()
    if choice.startswith("ollama::"):
        return choice.split("::", 1)[1].strip()
    if choice.startswith("ollama:"):
        return choice[len("ollama:") :].strip()
    return choice or SIMULATION_SKIP_LABEL


def simulation_default_choice_label():
    config = anythingllm_embedding_config(default_anythingllm_storage_dir())
    engine = (config.get("engine") or "").strip()
    normalized_engine = engine.casefold()
    model = (config.get("effective_model") or config.get("model") or "").strip()
    if normalized_engine:
        capability = resolve_embedder_capability(normalized_engine, model)
        suffix = capability.get("display_name") or f"{engine}: {model or 'not configured'}"
    else:
        suffix = "not configured"
    return f"{SIMULATION_ANYTHINGLLM_DEFAULT_PREFIX} ({suffix})"


def current_openrouter_simulation_options(force_refresh=False):
    # Initial UI rendering must remain offline and deterministic. Live catalog
    # discovery occurs only through an explicit refresh action.
    return openrouter_simulation_option_map(force_refresh=force_refresh, include_live=bool(force_refresh))


def provider_catalog_hint(provider, limit=10):
    rows = provider_catalog_entries(provider, force_refresh=False)
    if not rows:
        return ""
    preview = ", ".join(
        f"{row.get('model')}→{row.get('recommended_anythingllm_limit')}"
        for row in rows[:limit]
    )
    if len(rows) > limit:
        preview += f", … (+{len(rows) - limit} more)"
    provider_label = (provider or "").strip() or "provider"
    return f" Known {provider_label} model cards: {preview}."


def provider_catalog_preview(provider, limit=8, force_refresh=False):
    rows = provider_catalog_entries(provider, force_refresh=force_refresh)
    if not rows:
        return ""
    preview = ", ".join(
        f"{row.get('model')}→{row.get('recommended_anythingllm_limit')}"
        for row in rows[:limit]
        if row.get("model")
    )
    if len(rows) > limit:
        preview += f", … (+{len(rows) - limit} more)"
    provider_label = PROVIDER_LABELS.get((provider or "").strip().casefold(), (provider or "").strip() or "provider")
    return f"{provider_label}: {preview}"


def provider_limit_preview(provider, limit=8, force_refresh=False):
    rows = provider_catalog_entries(provider, force_refresh=force_refresh)
    if not rows:
        return ""
    provider_label = PROVIDER_LABELS.get((provider or "").strip().casefold(), (provider or "").strip() or "provider")
    preview = ", ".join(
        f"{row.get('model')}→{row.get('recommended_anythingllm_limit')}"
        for row in rows[:limit]
        if row.get("model")
    )
    if len(rows) > limit:
        preview += f", … (+{len(rows) - limit} more)"
    return f"{provider_label} limit cards: {preview}"


def provider_catalog_status(provider, limit=10, force_refresh=False):
    normalized = (provider or "").strip().casefold()
    rows = provider_catalog_entries(provider, force_refresh=force_refresh)
    provider_label = PROVIDER_LABELS.get(normalized, (provider or "").strip() or "provider")
    if not rows:
        return f"{provider_label}: no curated capability cards loaded."
    preview = ", ".join(
        f"{row.get('model')}→{row.get('recommended_anythingllm_limit')}"
        for row in rows[:limit]
        if row.get("model")
    )
    if len(rows) > limit:
        preview += f", … (+{len(rows) - limit} more)"
    return f"{provider_label}: {preview}"


def portable_catalog_hint(limit=12):
    rows = portable_catalog_entries(force_refresh=False)
    if not rows:
        return ""
    preview = ", ".join(
        f"{row.get('model')}→{row.get('recommended_anythingllm_limit')}"
        for row in rows[:limit]
    )
    if len(rows) > limit:
        preview += f", … (+{len(rows) - limit} more)"
    return f" Portable embedder registry: {preview}."


def known_embedder_catalog_summary(force_refresh=False):
    counts = provider_catalog_counts(force_refresh=force_refresh)
    ordered = [
        ("portable", "Portable registry"),
        ("anythingllm", "AnythingLLM built-in"),
        ("ollama", "Ollama live"),
        ("openai", "OpenAI-compatible"),
        ("gemini", "Gemini"),
        ("mistral", "Mistral"),
        ("cohere", "Cohere"),
        ("voyage", "Voyage"),
        ("jinaai", "Jina"),
        ("openrouter", "OpenRouter live"),
    ]
    return "; ".join(f"{label} {counts.get(key, 0)}" for key, label in ordered)


def anythingllm_embedder_model_choices(engine_value, current_value=""):
    engine = (engine_value or "").strip()
    rows = provider_catalog_entries(engine, force_refresh=True)
    choices = [row.get("model") for row in rows if row.get("model")]
    current = (current_value or "").strip()
    if current and current not in choices:
        choices = [current, *choices]
    deduped = []
    seen = set()
    for item in choices:
        value = (item or "").strip()
        if not value or value in seen:
            continue
        deduped.append(value)
        seen.add(value)
    return deduped


def refresh_anythingllm_embedder_model_dropdown(engine_value, current_value=""):
    engine = (engine_value or "").strip()
    current = (current_value or "").strip()
    choices = anythingllm_embedder_model_choices(engine, current)
    if current and current in choices:
        value = current
    elif choices:
        value = choices[0]
    else:
        value = current
    model_key = provider_model_key_for_engine(engine)
    preview = provider_catalog_preview(engine, limit=8, force_refresh=True)
    provider_label = PROVIDER_LABELS.get((engine or "").strip().casefold(), engine or "provider")
    info = f"Writes {model_key} for the active AnythingLLM embedder. "
    info += f"Known {provider_label} model cards: {len(choices)}."
    if preview:
        info += f" {preview}"
    limits_preview = provider_limit_preview(engine, limit=10, force_refresh=True)
    if limits_preview:
        info += f" {limits_preview}."
    else:
        info += f" {provider_label} limit cards: none loaded."
    if engine.strip().casefold() == "ollama":
        info += " Live Ollama embedding models are re-queried whenever you open this dropdown."
    elif engine.strip().casefold() in {"generic-openai", "litellm", "lmstudio", "lm-studio", "localai", "lemonade"}:
        info += " This provider uses the app's portable OpenAI-compatible registry so you can save common embedder model ids even when the upstream service does not expose a public model list."
    return gr.update(choices=choices, value=value, info=info)


def refresh_anythingllm_embedder_model_controls(engine_value, current_model="", current_limit_value=0):
    model_update = refresh_anythingllm_embedder_model_dropdown(engine_value, current_model)
    resolved_model = ""
    if isinstance(model_update, dict):
        resolved_model = (model_update.get("value") or current_model or "").strip()
    max_update, recommended_update, status_text = preview_anythingllm_embedder_policy(
        engine_value,
        resolved_model,
        current_limit_value,
    )
    return model_update, max_update, recommended_update, status_text


def preview_anythingllm_embedder_policy(engine_value, model_value, current_limit_value=0):
    engine = (engine_value or "").strip()
    model = (model_value or "").strip()
    policy = anythingllm_embedder_policy(default_anythingllm_storage_dir(), provider=engine, model=model)
    capability = policy.get("capability") or {}
    recommended_limit = int(policy.get("recommended_limit") or capability.get("recommended_anythingllm_limit") or 4096)
    try:
        current_limit = int(current_limit_value or 0)
    except (TypeError, ValueError):
        current_limit = 0
    next_limit = recommended_limit
    source_note = (capability.get("source_note") or "").strip()
    provider_label = PROVIDER_LABELS.get((engine or "").strip().casefold(), engine or "provider")
    status_text = (
        f"Selected {provider_label} embedder policy: {model or 'not configured'}. "
        f"Recommended AnythingLLM embedder max chunk limit: {recommended_limit}. "
        f"Status: {policy.get('status') or 'unknown'}; action: {policy.get('action') or 'warn_only'}; "
        f"risk: {policy.get('risk_label') or 'unknown'}."
    )
    if source_note:
        status_text += f" {source_note}"
    if current_limit and current_limit != recommended_limit:
        status_text += f" Preview updated the local field from {current_limit} to {recommended_limit}; save to persist it."
    else:
        status_text += " Save embedder engine/model or save max chunk limit to persist it into AnythingLLM."
    return (
        gr.update(value=next_limit),
        gr.update(value=recommended_limit),
        status_text,
    )


def default_simulation_resolution():
    try:
        return resolve_default_simulation_adapter()
    except Exception as exc:
        config = anythingllm_embedding_config(default_anythingllm_storage_dir())
        return {
            "status": "config_error",
            "engine": (config.get("engine") or "").strip().casefold(),
            "model": (config.get("model") or "").strip(),
            "adapter": None,
            "message": str(exc),
        }


def simulation_scope_suffix():
    return " Simulation embeds the prepared AnythingLLM-shaped chunks plus the probe queries, then ranks matches locally. It does not upload anything to AnythingLLM."


def format_progress_desc(stage, current=None, total=None):
    label = (stage or "").strip() or "Working"
    if current is not None and total and int(total) > 1:
        return f"{label} ({current}/{total})"
    return label


def describe_simulation_choice(local_check_mode, custom_ollama_model):
    local_choice = normalize_simulation_choice(local_check_mode)
    openrouter_options = current_openrouter_simulation_options(force_refresh=False)
    if local_choice == SIMULATION_ANYTHINGLLM_DEFAULT_LABEL:
        resolved = default_simulation_resolution()
        return f"{resolved['message']}{simulation_scope_suffix()}"
    if local_choice == SIMULATION_SKIP_LABEL:
        return "Retrieval simulation is off. No local embedding test will run before output generation."
    if local_choice in openrouter_options:
        model = openrouter_options[local_choice]
        capability = resolve_embedder_capability("openrouter", model)
        return (
            f"Retrieval simulation will use {local_choice}. "
            f"Recommended AnythingLLM embedder limit: {capability.get('recommended_anythingllm_limit')}. "
            f"{capability.get('source_note')}{simulation_scope_suffix()}"
        )
    capability = resolve_embedder_capability("ollama", local_choice)
    return (
        f"Retrieval simulation will use Ollama model: {local_choice}. "
        f"Recommended AnythingLLM embedder limit: {capability.get('recommended_anythingllm_limit')}. "
        f"{capability.get('source_note')}{simulation_scope_suffix()}"
    )


def ollama_model_choices(ollama_url, current_value=None):
    base = ollama_base_url(ollama_url)
    default_choice = simulation_default_choice_label()
    openrouter_options = current_openrouter_simulation_options(force_refresh=True)
    choices = [SIMULATION_SKIP_LABEL, default_choice, *openrouter_options.keys()]
    try:
        req = urllib.request.Request(f"{base}/api/tags")
        with urllib.request.urlopen(req, timeout=8) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
        all_models = [model.get("name") for model in data.get("models", []) if model.get("name")]
        models = sorted(
            [model for model in all_models if is_embedding_like_model(model)],
            key=str.casefold,
        )
        choices.extend([f"Ollama: {model}" for model in models])
        selected_value = current_value if current_value in choices else SIMULATION_SKIP_LABEL
        if not models:
            return (
                choices,
                selected_value,
                f"Ollama responded at {base}, but returned no embedding-oriented local models.",
            )
        return (
            choices,
            selected_value,
            f"Detected {len(models)} embedding-oriented Ollama model(s) at {base}. {len(all_models) - len(models)} non-embedding Ollama model(s) were omitted.",
        )
    except Exception as exc:
        selected_value = current_value if current_value in choices else SIMULATION_SKIP_LABEL
        return (
            choices,
            selected_value,
            f"Could not query Ollama at {base}: {exc}",
        )


def refresh_simulation_embedders(ollama_url, current_value=None):
    choices, value, discovery_status = ollama_model_choices(ollama_url, current_value=current_value)
    selected_status = describe_simulation_choice(value, "")
    if discovery_status:
        selected_status = f"{selected_status}\n\n{discovery_status}"
    return gr.update(choices=choices, value=value), selected_status


def load_simulation_embedders_on_open():
    return refresh_simulation_embedders(DEFAULT_OLLAMA_URL, SIMULATION_SKIP_LABEL)


INITIAL_SIMULATION_VALUE = SIMULATION_SKIP_LABEL
INITIAL_SIMULATION_CHOICES = [
    SIMULATION_SKIP_LABEL,
    SIMULATION_ANYTHINGLLM_DEFAULT_LABEL,
    *current_openrouter_simulation_options(force_refresh=False).keys(),
]
INITIAL_SIMULATION_DISCOVERY_STATUS = (
    "Live Ollama and OpenRouter discovery has not run yet; refresh the embedder controls to query them."
)
INITIAL_SIMULATION_STATUS = describe_simulation_choice(INITIAL_SIMULATION_VALUE, "")
if INITIAL_SIMULATION_DISCOVERY_STATUS:
    INITIAL_SIMULATION_STATUS = (
        f"{INITIAL_SIMULATION_STATUS}\n\n{INITIAL_SIMULATION_DISCOVERY_STATUS}"
    )


def initial_automatic_section_state():
    """Open core preparation/upload choices while keeping secondary tools collapsed."""
    return [
        gr.update(open=False),
        gr.update(open=True),
        gr.update(open=True),
        gr.update(open=False),
        gr.update(open=False),
    ]


def automatic_section_open_state():
    """Keep the primary preparation and workspace choices visible on a fresh app load."""
    return gr.update(open=True), gr.update(open=True), gr.update(open=True)


def metadata_contract_text(runtime_status="Runtime API: not checked."):
    contract_lines = [
        "AnythingLLM raw-text metadata contract:",
        *[f"{key}: {value}" for key, value in ANYTHINGLLM_RAW_TEXT_METADATA_FIELDS.items()],
        "",
        "Native chunk-text behavior: title becomes sourceDocument; published is included; "
        "chunkSource becomes source only for link:// or youtube:// values.",
        "Arbitrary fields such as pdf_page, chapter, and segment_id are discarded by the raw-text processor.",
        "",
        *[f"Source ({name}): {url}" for name, url in ANYTHINGLLM_SOURCE_CONTRACT.items()],
    ]
    return "\n".join([runtime_status, "", *contract_lines])


def pretty_json_preview(value, limit=1800):
    if value in (None, "", [], {}):
        return "Not available."
    try:
        text = json.dumps(value, indent=2, ensure_ascii=False)
    except TypeError:
        text = str(value)
    if len(text) > limit:
        text = text[: limit - 3].rstrip() + "..."
    return text


def humanize_vector_status(value):
    status = (value or "not_run").strip()
    labels = {
        "complete": "complete",
        "not_run": "not run",
        "not_run_extraction_only": "not run (extraction only)",
        "error_openrouter_authentication": "OpenRouter authentication failed",
        "error_openrouter_billing": "OpenRouter billing or credits issue",
        "error_openrouter_permission": "OpenRouter permission denied",
        "error_openrouter_rate_limited": "OpenRouter rate limited the request",
        "error_openrouter_provider_overloaded": "OpenRouter provider was overloaded",
        "error_openrouter_provider_unavailable": "OpenRouter provider was unavailable",
        "error_openrouter_network": "OpenRouter network request failed",
        "error_openrouter_timeout": "OpenRouter request timed out",
        "error_openrouter_request_rejected": "OpenRouter rejected the embedding request",
        "error_openrouter_embedder_limit": "OpenRouter rejected the planned embedding chunk limit",
        "error_ollama_embedder_limit": "Ollama rejected the planned embedding chunk limit",
        "error_anythingllm-runtime_embedder_limit": "AnythingLLM runtime rejected the planned embedding chunk limit",
        "skipped_openrouter_unavailable": "OpenRouter simulation adapter was not available",
        "skipped_ollama_unavailable": "Ollama was not available",
    }
    if status in labels:
        return labels[status]
    if status.startswith("error_"):
        return status.replace("error_", "").replace("_", " ")
    if status.startswith("skipped_"):
        return status.replace("skipped_", "skipped ").replace("_", " ")
    return status.replace("_", " ")


def anythingllm_settings_snapshot_html():
    storage_dir = default_anythingllm_storage_dir()
    state = anythingllm_resolved_state(storage_dir)
    embed = state["embedder"]
    llm = state["chat_llm"]
    chunk = state["chunking"]
    policy = embed.get("policy") or {}
    capability = embed.get("capability") or {}
    runtime = state.get("validation") or {}
    simulation_config = simulation_app_config()
    simulation_secret_status = "configured" if simulation_config.get("openrouter_configured") else "missing OPENROUTER_API_KEY"  # pragma: allowlist secret -- status label only
    adjacent_lines = []
    for item in embed.get("adjacent_model_preferences") or []:
        provider = item.get("provider") or "unknown"
        marker = "embedder engine" if item.get("matches_engine") else "other provider"
        adjacent_lines.append(f"{provider}: {item.get('key')}={item.get('value')} [{marker}]")
    effective_rows = [
        ("Chat provider / model", f"{llm.get('provider') or 'not detected'} / {llm.get('model') or 'not detected'}"),
        ("Embedder provider / model", f"{embed.get('engine') or 'not detected'} / {embed.get('effective_model') or embed.get('model') or 'not detected'}"),
        ("Chunk size / overlap", f"{chunk.get('chunk_size')} / {chunk.get('chunk_overlap')}"),
        ("Embedder max chunk", f"{embed.get('max_chunk_length') or 'not detected'}"),
        ("Recommended embedder limit", f"{policy.get('recommended_limit') or 'not detected'}"),
        ("Simulation default", simulation_default_choice_label()),
    ]
    risk_rows = [
        ("Policy status", policy.get("status") or "not detected"),
        ("Policy action", policy.get("action") or "not detected"),
        ("Capability status", capability.get("status") or "unknown_capability"),
        ("Capability note", capability.get("source_note") or "Not detected"),
        ("Anomalies", ", ".join(state.get("anomalies") or []) or "none"),
        ("Adjacent provider model settings", " | ".join(adjacent_lines) or "none"),
        ("Runtime verification", runtime.get("status") or "not detected"),
        ("Verification note", runtime.get("message") or "Not detected"),
    ]
    last_rows = [
        ("Provider / model", f"{(LAST_SIMULATION_DIAGNOSTICS.get('provider') or 'not run')} / {(LAST_SIMULATION_DIAGNOSTICS.get('model') or 'not run')}"),
        ("Requests", LAST_SIMULATION_DIAGNOSTICS.get("requests", 0)),
        ("Tokens", LAST_SIMULATION_DIAGNOSTICS.get("total_tokens", 0)),
        ("Cost", LAST_SIMULATION_DIAGNOSTICS.get("cost", 0.0)),
        ("Latency max ms", LAST_SIMULATION_DIAGNOSTICS.get("latency_ms_max", 0)),
        ("Key source", LAST_SIMULATION_DIAGNOSTICS.get("key_source") or "not run"),
        ("Last failure", LAST_SIMULATION_DIAGNOSTICS.get("last_failure") or "none"),
    ]
    meta_rows = [
        ("Storage", f"{storage_dir}"),
        ("Localhost app .env", f"{simulation_config.get('path') or project_local_env_path()}"),
        ("OpenRouter simulation key", simulation_secret_status),
        ("AnythingLLM OpenRouter fallback", "available" if embed.get("openrouter_configured") else "not available"),
        ("OpenRouter simulation timeout", f"{simulation_config.get('openrouter_timeout_seconds') or 'not detected'}"),
        ("OpenRouter simulation ZDR", "on" if simulation_config.get("openrouter_zdr") else "off"),
        ("Known embedder catalogs", known_embedder_catalog_summary(force_refresh=False)),
        ("Portable registry preview", portable_catalog_hint(limit=8).replace(" Portable embedder registry: ", "").strip() or "not detected"),
    ]
    def rows_html(rows):
        return "".join(
            '<div class="metadata-key">{}</div><div class="metadata-value">{}</div>'.format(
                html.escape(str(key)),
                html.escape(str(value)),
            )
            for key, value in rows
        )
    return (
        '<div class="metadata-summary">'
        '<section class="metadata-file"><div class="metadata-file-name">Effective State</div>'
        f'<div class="metadata-grid">{rows_html(effective_rows)}</div></section>'
        '<section class="metadata-file"><div class="metadata-file-name">Corrections and Risks</div>'
        f'<div class="metadata-grid">{rows_html(risk_rows)}</div></section>'
        '<section class="metadata-file"><div class="metadata-file-name">Last Simulation</div>'
        f'<div class="metadata-grid">{rows_html(last_rows)}</div></section>'
        '<section class="metadata-file"><div class="metadata-file-name">Local Paths and Secrets</div>'
        f'<div class="metadata-grid">{rows_html(meta_rows)}</div></section>'
        '</div>'
    )


def current_anythingllm_embedder_max_chunk_value():
    state = anythingllm_resolved_state(default_anythingllm_storage_dir(), runtime_verify=False)
    embed = state["embedder"]
    try:
        return int(embed.get("max_chunk_length") or 0) or int((embed.get("policy") or {}).get("recommended_limit") or 0) or 4096
    except (TypeError, ValueError):
        return 4096


def current_anythingllm_recommended_embedder_limit_value():
    state = anythingllm_resolved_state(default_anythingllm_storage_dir(), runtime_verify=False)
    try:
        return int((state["embedder"].get("policy") or {}).get("recommended_limit") or 0) or 4096
    except (TypeError, ValueError):
        return 4096


def current_anythingllm_effective_model_value():
    embed = anythingllm_embedding_config(default_anythingllm_storage_dir())
    return (embed.get("effective_model") or embed.get("model") or "").strip()


def current_anythingllm_engine_value():
    embed = anythingllm_embedding_config(default_anythingllm_storage_dir())
    return (embed.get("engine") or "").strip()


def current_anythingllm_chunk_size_value():
    state = anythingllm_resolved_state(default_anythingllm_storage_dir(), runtime_verify=False)
    chunk = state["chunking"]
    try:
        return str(int(chunk.get("chunk_size") or 512))
    except (TypeError, ValueError):
        return "512"


def current_anythingllm_chunk_overlap_value():
    state = anythingllm_resolved_state(default_anythingllm_storage_dir(), runtime_verify=False)
    chunk = state["chunking"]
    try:
        return str(int(chunk.get("chunk_overlap") or 75))
    except (TypeError, ValueError):
        return "75"


def anythingllm_settings_reference_html():
    state = anythingllm_resolved_state(default_anythingllm_storage_dir(), runtime_verify=False)
    chunk = state["chunking"]
    embed = state["embedder"]
    policy = embed.get("policy") or {}
    current_limit = embed.get("max_chunk_length") or "unset"
    recommendation_rows = [
        (
            "Historical retrieval comparison",
            f"{SEGMENT_PASSAGES_LABEL} with chunk {TESTED_RETRIEVAL_CHUNK_SIZE} / {TESTED_RETRIEVAL_CHUNK_OVERLAP}. "
            "Use as a controlled comparison preset; it is not a universal retrieval recommendation.",
        ),
        (
            "Best page provenance",
            f"{SEGMENT_PAGE_LIMIT_LABEL}. Keep each source page intact unless the active safety ceiling requires a split.",
        ),
        (
            "Chunk-survival comparison context",
            f"{SEGMENT_PASSAGES_LABEL} plus the historical retrieval comparison preset. "
            "Current evidence is not sufficient to promote it over the page-preserving default, and AnythingLLM may still rechunk uploaded units during embedding.",
        ),
    ]
    recommendation_html = "".join(
        '<div class="metadata-key">{}</div><div class="metadata-value">{}</div>'.format(
            html.escape(str(key)),
            html.escape(str(value)),
        )
        for key, value in recommendation_rows
    )
    return (
        '<div class="setting-reference-note"><em>'
        f"Current AnythingLLM values: chunk {html.escape(str(chunk.get('chunk_size') or ''))} / "
        f"{html.escape(str(chunk.get('chunk_overlap') or ''))}; "
        f"embedder max chunk {html.escape(str(current_limit))}. "
        f"Recommended embedder max chunk: {html.escape(str(policy.get('recommended_limit') or 'unknown'))}. "
        f"Risk: {html.escape(str(policy.get('risk_label') or 'unknown'))}. "
        f"Historical retrieval comparison preset: chunk {TESTED_RETRIEVAL_CHUNK_SIZE} / {TESTED_RETRIEVAL_CHUNK_OVERLAP}. "
        "The current local operating default is page-preserving automatic; AnythingLLM native chunk size and overlap remain independent benchmark variables."
        "</em></div>"
        '<div class="metadata-summary"><section class="metadata-file"><div class="metadata-file-name">Workflow Recommendations</div>'
        f'<div class="metadata-grid">{recommendation_html}</div></section></div>'
    )


def numeric_dropdown_update(value, base_choices, interactive=True):
    normalized_choices = []
    seen = set()
    for item in base_choices:
        text = str(item).strip()
        if not text or text in seen:
            continue
        normalized_choices.append(text)
        seen.add(text)
    normalized_value = ""
    try:
        normalized_value = str(int(value))
    except (TypeError, ValueError):
        normalized_value = str(value or "").strip()
    if normalized_value and normalized_value not in seen:
        normalized_choices = [normalized_value, *normalized_choices]
    return gr.update(
        choices=normalized_choices,
        value=normalized_value,
        interactive=interactive,
    )


def apply_tested_retrieval_preset_ui(inherit_enabled, current_embedder_max_chunk=0):
    persisted = persist_anythingllm_chunk_settings(
        default_anythingllm_storage_dir(),
        TESTED_RETRIEVAL_CHUNK_SIZE,
        TESTED_RETRIEVAL_CHUNK_OVERLAP,
    )
    html_value, chunk_update, overlap_update, embedder_update, recommended_update, engine_update, model_update = refresh_anythingllm_settings(
        inherit_enabled,
        TESTED_RETRIEVAL_CHUNK_SIZE,
        TESTED_RETRIEVAL_CHUNK_OVERLAP,
        current_embedder_max_chunk,
    )
    return (
        html_value,
        chunk_update,
        overlap_update,
        embedder_update,
        recommended_update,
        engine_update,
        model_update,
        "Applied the historical retrieval comparison preset: "
        f"chunk {TESTED_RETRIEVAL_CHUNK_SIZE} / {TESTED_RETRIEVAL_CHUNK_OVERLAP}. "
        + persisted.get("runtime_verification_message", "")
        + " This does not promote a universal default: the current local operating default remains page-bounded subchunking with a 750-character target, and AnythingLLM may still rechunk uploaded units during embedding. "
        + refresh_desktop_after_anythingllm_mutation(),
    )


def save_anythingllm_chunk_settings(chunk_size_value, chunk_overlap_value, inherit_enabled, current_embedder_max_chunk=0):
    try:
        chunk_size = int(chunk_size_value or 0)
        chunk_overlap = int(chunk_overlap_value or 0)
    except (TypeError, ValueError):
        html_value, chunk_update, overlap_update, embedder_update, recommended_update, engine_update, model_update = refresh_anythingllm_settings(
            inherit_enabled,
            chunk_size_value,
            chunk_overlap_value,
            current_embedder_max_chunk,
        )
        return html_value, chunk_update, overlap_update, embedder_update, recommended_update, engine_update, model_update, "Chunk size and overlap must be whole numbers."
    if chunk_size < 100:
        html_value, chunk_update, overlap_update, embedder_update, recommended_update, engine_update, model_update = refresh_anythingllm_settings(
            inherit_enabled,
            chunk_size_value,
            chunk_overlap_value,
            current_embedder_max_chunk,
        )
        return html_value, chunk_update, overlap_update, embedder_update, recommended_update, engine_update, model_update, "Chunk size must be at least 100."
    if chunk_overlap < 0 or chunk_overlap >= chunk_size:
        html_value, chunk_update, overlap_update, embedder_update, recommended_update, engine_update, model_update = refresh_anythingllm_settings(
            inherit_enabled,
            chunk_size_value,
            chunk_overlap_value,
            current_embedder_max_chunk,
        )
        return html_value, chunk_update, overlap_update, embedder_update, recommended_update, engine_update, model_update, "Chunk overlap must be 0 or more and smaller than chunk size."
    persisted = persist_anythingllm_chunk_settings(default_anythingllm_storage_dir(), chunk_size, chunk_overlap)
    html_value, chunk_update, overlap_update, embedder_update, recommended_update, engine_update, model_update = refresh_anythingllm_settings(
        inherit_enabled,
        chunk_size,
        chunk_overlap,
        current_embedder_max_chunk,
    )
    return (
        html_value,
        chunk_update,
        overlap_update,
        embedder_update,
        recommended_update,
        engine_update,
        model_update,
        "Saved AnythingLLM chunk settings. "
        + persisted.get("runtime_verification_message", "")
        + " Re-embed existing documents if you want current workspaces to reflect the new chunk boundaries. "
        + refresh_desktop_after_anythingllm_mutation(),
    )


def apply_recommended_anythingllm_settings_ui(inherit_enabled):
    result = apply_recommended_anythingllm_settings(default_anythingllm_storage_dir())
    html_value, chunk_update, overlap_update, embedder_update, recommended_update, engine_update, model_update = refresh_anythingllm_settings(
        inherit_enabled,
        result.get("requested", {}).get("chunk_size", 0),
        result.get("requested", {}).get("chunk_overlap", -1),
        result.get("requested", {}).get("embedder_limit", 0),
    )
    return (
        html_value,
        chunk_update,
        overlap_update,
        embedder_update,
        recommended_update,
        engine_update,
        model_update,
        result.get("message", "") + " " + result.get("runtime_message", "") + " " + refresh_desktop_after_anythingllm_mutation(),
    )


def save_anythingllm_embedder_engine_model(engine_value, model_value, inherit_enabled, current_chunk_size=0, current_chunk_overlap=-1, current_embedder_max_chunk=0):
    engine = (engine_value or "").strip()
    model = (model_value or "").strip()
    if not engine:
        html_value, chunk_update, overlap_update, embedder_update, recommended_update, engine_update, model_update = refresh_anythingllm_settings(
            inherit_enabled,
            current_chunk_size,
            current_chunk_overlap,
            current_embedder_max_chunk,
        )
        return html_value, chunk_update, overlap_update, embedder_update, recommended_update, engine_update, model_update, "Select an embedder engine first."
    storage_dir = default_anythingllm_storage_dir()
    persisted = persist_anythingllm_embedder_settings(storage_dir, engine, model)
    html_value, chunk_update, overlap_update, embedder_update, recommended_update, engine_update, model_update = refresh_anythingllm_settings(
        inherit_enabled,
        current_chunk_size,
        current_chunk_overlap,
        current_embedder_max_chunk,
    )
    return (
        html_value,
        chunk_update,
        overlap_update,
        embedder_update,
        recommended_update,
        engine_update,
        model_update,
        "Saved AnythingLLM embedder engine/model. "
        + persisted.get("runtime_verification_message", "")
        + " Review the recommended chunk settings before running a simulation or re-embedding."
        + " "
        + refresh_desktop_after_anythingllm_mutation(),
    )


def save_anythingllm_embedder_max_chunk_limit(limit_value, inherit_enabled, current_chunk_size=0, current_chunk_overlap=-1):
    try:
        limit = int(limit_value or 0)
    except (TypeError, ValueError):
        html_value, chunk_update, overlap_update, embedder_update, recommended_update, engine_update, model_update = refresh_anythingllm_settings(
            inherit_enabled,
            current_chunk_size,
            current_chunk_overlap,
        )
        return html_value, chunk_update, overlap_update, embedder_update, recommended_update, engine_update, model_update, "Enter a whole-number embedder max chunk limit."
    if limit < 1:
        html_value, chunk_update, overlap_update, embedder_update, recommended_update, engine_update, model_update = refresh_anythingllm_settings(
            inherit_enabled,
            current_chunk_size,
            current_chunk_overlap,
        )
        return html_value, chunk_update, overlap_update, embedder_update, recommended_update, engine_update, model_update, "Embedder max chunk limit must be at least 1."
    persisted = persist_anythingllm_embedder_limit(default_anythingllm_storage_dir(), limit, trigger="manual")
    html_value, chunk_update, overlap_update, embedder_update, recommended_update, engine_update, model_update = refresh_anythingllm_settings(
        inherit_enabled,
        current_chunk_size,
        current_chunk_overlap,
    )
    return (
        html_value,
        chunk_update,
        overlap_update,
        embedder_update,
        recommended_update,
        engine_update,
        model_update,
        f"Saved EMBEDDING_MODEL_MAX_CHUNK_LENGTH={limit}. "
        + persisted.get("runtime_verification_message", "")
        + " "
        + refresh_desktop_after_anythingllm_mutation(),
    )


def refresh_anythingllm_settings(inherit_enabled, current_chunk_size=0, current_chunk_overlap=-1, current_embedder_max_chunk=0):
    storage_dir = default_anythingllm_storage_dir()
    state = anythingllm_resolved_state(storage_dir)
    chunk = state["chunking"]
    embed = state["embedder"]
    try:
        embedder_max_chunk = int(embed.get("max_chunk_length") or 0) or int((embed.get("policy") or {}).get("recommended_limit") or 0) or 2048
    except (TypeError, ValueError):
        embedder_max_chunk = 2048
    try:
        recommended_limit = int((embed.get("policy") or {}).get("recommended_limit") or 0) or 2048
    except (TypeError, ValueError):
        recommended_limit = 2048
    html_value = anythingllm_settings_snapshot_html()
    current_engine = (embed.get("engine") or "").strip()
    current_model = (embed.get("effective_model") or embed.get("model") or "").strip()
    model_update = refresh_anythingllm_embedder_model_dropdown(current_engine, current_model)
    engine_update = gr.update(choices=ANYTHINGLLM_EMBEDDER_ENGINE_CHOICES, value=current_engine)
    if inherit_enabled:
        return (
            html_value,
            numeric_dropdown_update(int(chunk.get("chunk_size") or 1000), CHUNK_SIZE_PRESET_CHOICES, interactive=True),
            numeric_dropdown_update(int(chunk.get("chunk_overlap") or 20), CHUNK_OVERLAP_PRESET_CHOICES, interactive=True),
            gr.update(value=embedder_max_chunk),
            gr.update(value=recommended_limit),
            engine_update,
            model_update,
        )
    return (
        html_value,
        numeric_dropdown_update(
            current_chunk_size if current_chunk_size not in (None, "") else int(chunk.get("chunk_size") or 1000),
            CHUNK_SIZE_PRESET_CHOICES,
            interactive=True,
        ),
        numeric_dropdown_update(
            current_chunk_overlap if current_chunk_overlap not in (None, "") else int(chunk.get("chunk_overlap") or 20),
            CHUNK_OVERLAP_PRESET_CHOICES,
            interactive=True,
        ),
        gr.update(value=current_embedder_max_chunk if current_embedder_max_chunk not in (None, "", 0) else embedder_max_chunk),
        gr.update(value=recommended_limit),
        engine_update,
        model_update,
    )


def workspace_inspector_html(workspace_slug):
    storage_dir = default_anythingllm_storage_dir()
    report = workspace_storage_inspector(storage_dir, workspace_slug)
    storage_path_literal = json.dumps(str(storage_dir))
    storage_path_html = (
        '<div class="metadata-key">AnythingLLM storage path</div>'
        '<div class="metadata-value">'
        f'<code>{html.escape(str(storage_dir))}</code>'
        f'<button type="button" class="copy-storage-path-button" onclick="navigator.clipboard.writeText({html.escape(storage_path_literal, quote=True)})">Copy path</button>'
        '</div>'
    )
    if report.get("status") != "complete":
        message = report.get("error") or "Select a workspace to inspect AnythingLLM storage."
        return (
            '<div class="metadata-summary"><section class="metadata-file"><div class="metadata-file-name">Workspace storage check</div>'
            f'<div class="metadata-grid">{storage_path_html}</div>'
            f'<div class="metadata-status">{html.escape(message)}</div></section></div>'
        )

    rows = [
        ("Selected workspace", report.get("workspace_name") or report.get("workspace_slug")),
        ("Workspace document count", report.get("workspace_document_count")),
        ("Raw native docs", report.get("raw_native_doc_count")),
        ("Embedded chunk count", report.get("embedded_chunk_count")),
        ("SQLite workspace_documents.metadata fields", ", ".join(report.get("sqlite_workspace_metadata_fields") or []) or "Not detected"),
        ("Custom document JSON fields", ", ".join(report.get("custom_document_json_fields") or []) or "Not detected"),
        ("LanceDB row fields", ", ".join(report.get("lancedb_row_fields") or []) or "Not detected"),
        ("Text-visible page/segment evidence", report.get("page_segment_visibility")),
    ]
    row_html = "".join(
        '<div class="metadata-key">{}</div><div class="metadata-value">{}</div>'.format(
            html.escape(str(key)),
            html.escape(str(value)),
        )
        for key, value in rows
    )
    sample_html = "".join(
        '<section class="metadata-file"><div class="metadata-file-name">{}</div><pre class="inspector-pre">{}</pre></section>'.format(
            html.escape(title),
            html.escape(body),
        )
        for title, body in [
            ("Sample workspace_documents row", pretty_json_preview(report.get("sample_workspace_document"))),
            ("Sample custom-documents record", pretty_json_preview(report.get("sample_custom_document_record"))),
            ("Sample LanceDB text row", pretty_json_preview(report.get("sample_lancedb_row"))),
        ]
    )
    return (
        '<div class="metadata-summary">'
        '<section class="metadata-file"><div class="metadata-file-name">Workspace storage check</div>'
        '<div class="metadata-status">Observed metadata fields are layer-specific. They do not prove that every native metadata field survives into retrieval-visible context.</div>'
        f'<div class="metadata-grid">{storage_path_html}</div>'
        f'<div class="metadata-grid">{row_html}</div></section>'
        f"{sample_html}"
        "</div>"
    )


def workspace_verification_card_html(api_url, workspace_slug):
    """Render one concise, read-only verification result for the selected workspace."""
    slug = (workspace_slug or "").strip()
    if not slug:
        return '<div class="artifact-placeholder"><strong>Verification needs a selected workspace.</strong></div>'
    observer = workspace_ingestion_observer_snapshot(slug, api_url)
    storage = workspace_storage_inspector(default_anythingllm_storage_dir(), slug)
    docs = observer.get("workspace_documents")
    vectors = observer.get("embedded_vectors")
    embedded_chunks = storage.get("embedded_chunk_count") if storage.get("status") == "complete" else None
    if observer.get("database_status") == "database_busy":
        category, title, detail = "yellow", "Verification limited", "AnythingLLM is writing its database; retry this read-only check in a moment."
    elif not observer.get("api", {}).get("reachable"):
        category, title, detail = "yellow", "Runtime not reachable", "The local API could not be reached, so this check cannot claim retrieval is ready."
    elif storage.get("status") != "complete":
        category, title, detail = "red", "Verification failed", storage.get("error") or "The selected workspace could not be inspected."
    elif (vectors or 0) > 0 or (embedded_chunks or 0) > 0:
        if (docs or 0) == 0 and (embedded_chunks or 0) > 0:
            category, title, detail = "yellow", "Vectors found; document list caveat", "Embedding evidence exists, but the workspace document list has no matching rows. Use a retrieval check before relying on document management."
        else:
            category, title, detail = "green", "Embedding evidence found", "Storage contains embedded records for the selected workspace. This is structural evidence, not a claim about answer quality."
    else:
        category, title, detail = "red", "No embedding evidence found", "No workspace vectors or embedded chunks were observed for the selected workspace."
    rows = [
        ("Workspace", slug),
        ("Runtime API", "reachable" if observer.get("api", {}).get("reachable") else "not reachable"),
        ("Workspace document rows", docs if docs is not None else "not observed"),
        ("Vectors via document rows", vectors if vectors is not None else "not observed"),
        ("Embedded chunks in storage", embedded_chunks if embedded_chunks is not None else "not observed"),
        ("Page/segment evidence", storage.get("page_segment_visibility") or "not observed"),
    ]
    grid = "".join(
        f'<div class="metadata-key">{html.escape(str(key))}</div><div class="metadata-value">{html.escape(str(value))}</div>'
        for key, value in rows
    )
    return (
        f'<div class="workspace-verification-card {category}"><strong>{html.escape(title)}</strong>'
        f'<div>{html.escape(detail)}</div><div class="metadata-grid">{grid}</div>'
        '<div class="metadata-status">Read-only check: this does not start, retry, or change AnythingLLM work.</div></div>'
    )


def _safe_history_value(value, limit=500):
    return str(value or "").replace("\n", " ")[:limit]


def prune_background_jsonl(path, retention_days=BACKGROUND_LOG_RETENTION_DAYS):
    """Keep compact app histories for one year without touching run artifacts.

    Only the app-owned JSONL histories are compacted.  Malformed or undated
    lines are retained for inspection instead of being silently discarded.
    """
    history_path = Path(path)
    result = {"status": "not_needed", "kept": 0, "removed": 0}
    if not history_path.exists():
        return result
    try:
        cutoff = datetime.now() - timedelta(days=max(1, int(retention_days or BACKGROUND_LOG_RETENTION_DAYS)))
        kept_lines = []
        removed = 0
        for line in history_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                recorded_at = datetime.fromisoformat(str(record.get("recorded_at") or ""))
            except (TypeError, ValueError, json.JSONDecodeError):
                kept_lines.append(line)
                continue
            if recorded_at < cutoff:
                removed += 1
            else:
                kept_lines.append(line)
        result = {"status": "unchanged" if not removed else "pruned", "kept": len(kept_lines), "removed": removed}
        if removed:
            temporary_path = history_path.with_name(history_path.name + ".retention.tmp")
            temporary_path.write_text(
                "".join(f"{line}\n" for line in kept_lines),
                encoding="utf-8",
            )
            temporary_path.replace(history_path)
            APP_LOGGER.info("pruned %s expired background log records from %s", removed, history_path)
        return result
    except OSError as exc:
        APP_LOGGER.warning("could not apply background-log retention to %s: %s", history_path, exc)
        return {"status": "error", "kept": 0, "removed": 0, "error": str(exc)}


def _history_value_fingerprint(value):
    """Hash a potentially long setting without retaining its contents in history."""
    normalized = str(value or "").replace("\r\n", "\n").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def automatic_run_processing_settings(values):
    """Return a stable, non-secret description of run-defining settings.

    The description exists only for the optional repeat-run notice.  It is
    deliberately separate from ETA features: it never contains a source hash,
    filename, output path, workspace, API URL/key, or a previous duration.
    """
    settings = values if isinstance(values, dict) else {}
    local_only = settings.get("mode") != MODE_NATIVE_UPLOAD_LABEL
    raw = {
        "document_label": settings.get("document_label"),
        "document_author": settings.get("document_author"),
        "document_short_label": settings.get("document_short_label"),
        "use_file_title_fallback": settings.get("use_file_title_fallback"),
        "mode": settings.get("mode"),
        # Local-only runs must not be marked as different merely because an
        # invisible upload-only control retained its previous value.
        "native_upload_scope": "local_only" if local_only else settings.get("native_upload_scope"),
        "native_metadata_mode": "not_applicable" if local_only else settings.get("native_metadata_mode"),
        "anythingllm_create_document_folders": False if local_only else settings.get("anythingllm_create_document_folders"),
        "anythingllm_document_folder_name": "" if local_only else settings.get("anythingllm_document_folder_name"),
        "local_check_mode": settings.get("local_check_mode"),
        "custom_ollama_model": settings.get("custom_ollama_model"),
        "vector_audit_scope": settings.get("vector_audit_scope"),
        "deep_extraction": settings.get("deep_extraction"),
        "include_front_matter": settings.get("include_front_matter"),
        "include_back_matter": settings.get("include_back_matter"),
        "backend_mode": settings.get("backend_mode"),
        "first_page_override": settings.get("first_page_override"),
        "end_page_override": settings.get("end_page_override"),
        "target_passage_length": settings.get("target_passage_length"),
        "segment_mode": settings.get("segment_mode"),
        "unstructured_strategy": settings.get("unstructured_strategy"),
        "generate_inline_fallback": settings.get("generate_inline_fallback"),
        "inherit_anythingllm_settings": settings.get("inherit_anythingllm_settings"),
        "anythingllm_chunk_size": settings.get("anythingllm_chunk_size"),
        "anythingllm_chunk_overlap": settings.get("anythingllm_chunk_overlap"),
        "auto_apply_recommended_settings": False if local_only else settings.get("auto_apply_recommended_settings"),
        # Store only the digest of free-text controls. They affect processing,
        # but should not be copied into a one-year app history merely to decide
        # whether a warning can say "same settings".
        "advanced_end_section_names_sha256": _history_value_fingerprint(settings.get("advanced_end_section_names")),
        "automatic_validation_phrases_sha256": _history_value_fingerprint(settings.get("automatic_validation_phrases")),
    }
    canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return {
        "schema_version": REPEAT_RUN_SETTINGS_SCHEMA_VERSION,
        "fingerprint": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def fresh_automatic_run_setting_values(pdf_files=None, folder_pdf_files=None):
    """Return the persisted-setting shape applied to every fresh selection."""
    return {
        "pdf_files": pdf_files,
        "folder_pdf_files": folder_pdf_files,
        "document_label": "",
        "document_author": "",
        "document_short_label": "",
        "use_file_title_fallback": True,
        "mode": MODE_NATIVE_UPLOAD_LABEL,
        "output_root_override": str(AUTO_OUTPUT_DIR),
        "api_url": DEFAULT_ANYTHINGLLM_API_URL,
        "api_key": "",
        "workspace_slug": INITIAL_WORKSPACE_VALUE,
        "native_upload_scope": NATIVE_UPLOAD_SCOPE_ALL_LABEL,
        "native_upload_custom_range": "",
        "native_metadata_mode": "Native title header (priority)",
        # AnythingLLM Desktop 1.15's Documents drawer enumerates the immediate
        # ``custom-documents`` entries only.  Its nested-folder representation
        # is not a reliable visible attachment check, so production uploads are
        # drawer-visible by default. Foldered storage remains an explicit
        # advanced opt-in for people who accept that limitation.
        "anythingllm_create_document_folders": False,
        "anythingllm_document_folder_name": "",
        "local_check_mode": INITIAL_SIMULATION_VALUE,
        "custom_ollama_model": "",
        "ollama_url": DEFAULT_OLLAMA_URL,
        "vector_audit_scope": "Full corpus",
        # Automatic mode already invokes Unstructured only after auditable
        # native-extraction warning signals.  The opt-in below means "also
        # evaluate every PDF with Unstructured", which is intentionally off.
        "deep_extraction": False,
        "include_front_matter": True,
        "include_back_matter": True,
        "backend_mode": "Automatic",
        "first_page_override": 0,
        "end_page_override": 0,
        "target_passage_length": str(DEFAULT_TARGET_PASSAGE_LENGTH),
        # 0 means follow the active AnythingLLM splitter/embedder ceiling.
        "page_preserve_ceiling": 0,
        "segment_mode": SEGMENT_PAGE_LIMIT_LABEL,
        "advanced_end_section_names": "\n".join(DEFAULT_END_SECTION_HEADINGS),
        "automatic_validation_phrases": "",
        "unstructured_strategy": "auto",
        "generate_inline_fallback": True,
        "inherit_anythingllm_settings": True,
        "anythingllm_chunk_size": current_anythingllm_chunk_size_value(),
        "anythingllm_chunk_overlap": current_anythingllm_chunk_overlap_value(),
        "auto_apply_recommended_settings": False,
        "download_full_folder": False,
        "download_segments_folder": False,
    }


def refresh_automatic_run_estimate_for_fresh_selection(pdf_files=None, folder_pdf_files=None, folder_manifest=None):
    """Refresh ETA after the selection reset with only the fresh defaults."""
    defaults = fresh_automatic_run_setting_values(pdf_files, folder_pdf_files)
    return refresh_automatic_run_estimate(
        defaults["pdf_files"],
        defaults["folder_pdf_files"],
        defaults["mode"],
        defaults["native_upload_scope"],
        defaults["workspace_slug"],
        defaults["segment_mode"],
        defaults["target_passage_length"],
        defaults["anythingllm_chunk_size"],
        defaults["anythingllm_chunk_overlap"],
        defaults["backend_mode"],
        defaults["unstructured_strategy"],
        defaults["local_check_mode"],
        folder_manifest=folder_manifest,
        api_url=defaults.get("api_url", ""),
        inherit_anythingllm_settings=defaults.get("inherit_anythingllm_settings"),
    )


AUTOMATIC_RUN_FOLDER_PATTERNS = ("r-*", "app-run-*")


def automatic_run_artifact_paths(root, relative_pattern):
    """Find current short run folders and pre-limit historical run folders."""
    base = Path(root)
    return [
        path
        for folder_pattern in AUTOMATIC_RUN_FOLDER_PATTERNS
        for path in base.glob(f"{folder_pattern}/{relative_pattern}")
    ]


def create_fresh_automatic_run_root(output_root_base):
    """Atomically reserve a new output folder, even for same-second retries."""
    base = Path(output_root_base)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for suffix in range(1, 1000):
        label = f"r-{stamp}" if suffix == 1 else f"r-{stamp}-{suffix}"
        candidate = base / label
        if len(str(candidate)) > 250:
            raise OSError(
                "Output root is too long for Windows-compatible app-run artifacts; choose a shorter folder."
            )
        try:
            candidate.mkdir(parents=True, exist_ok=False)
            return candidate
        except FileExistsError:
            continue
    raise OSError("Could not reserve a fresh automatic run folder after 999 attempts.")


def flat_no_logs_output_folder_name(pdf_path, source_sha="", parent=None):
    """Name one user-visible flat export without a date/time run container."""
    pdf = Path(pdf_path)
    stem = safe_stem(pdf.stem) or "document"
    digest = str(source_sha or "").strip().lower()
    if not digest:
        digest = sha256_file(pdf)
    preferred = f"parsed-pdf-{stem}-{digest[:12]}"
    if parent is None:
        return preferred[:72].rstrip("-._ ")
    available = 250 - len(str(Path(parent))) - 1
    if available < 16:
        raise OSError("Output root is too long for a Windows-compatible flat export folder.")
    if len(preferred) <= available:
        return preferred
    # Keep a visible source prefix and the source-content suffix so reruns
    # remain distinguishable without relying on long Windows paths.
    suffix = f"-{digest[:12]}"
    prefix_length = max(1, min(48, available - len("parsed-pdf-") - len(suffix)))
    return f"parsed-pdf-{stem[:prefix_length].rstrip('-._ ')}{suffix}"


def promote_flat_no_logs_output(output_root, temporary_output_dir, pdf_path, summary):
    """Move one ready flat export beside the chosen root and update its UI path.

    Same-content reruns never overwrite an earlier export.  They receive a
    small numeric suffix, not a date/time marker, so the stable source hash
    remains visible in the folder name.
    """
    base = Path(output_root)
    source = Path(temporary_output_dir)
    if not source.is_dir():
        raise FileNotFoundError(f"No-logs export directory is missing: {source}")
    name = flat_no_logs_output_folder_name(
        pdf_path,
        (summary or {}).get("source_sha256"),
        parent=base,
    )
    target = base / name
    for suffix in range(2, 1000):
        if not target.exists():
            break
        target = base / f"{name}-{suffix}"
    else:
        raise OSError("Could not reserve a unique no-logs output folder after 998 retries.")
    moved = Path(shutil.move(str(source), str(target)))
    prepared = Path(str((summary or {}).get("upload_file") or ""))
    if prepared.name:
        summary["upload_file"] = str(moved / prepared.name)
    summary["flat_no_logs_output_directory"] = str(moved)
    return moved


def append_ingestion_history(
    run_root,
    summaries,
    completion,
    prepare_and_upload,
    workspace_slug,
    processing_settings=None,
    mode=None,
):
    """Persist a compact audit trail after every terminal app run."""
    docs = []
    for summary in summaries:
        pdf = Path(summary.get("pdf") or summary.get("upload_file") or "document")
        docs.append({
            "name": pdf.name,
            "source_sha256": summary.get("source_sha256") or summary.get("source_sha") or "",
            "segments": summary.get("segments", 0),
            "upload_status": summary.get("api_upload_status", "not_requested"),
            "post_upload": summary.get("post_upload_verification_status", "not_checked"),
            "runtime_validation": summary.get("anythingllm_runtime_validation_status", "not_checked"),
            "retrieval_smoke": {
                "vector_checks_passed": summary.get("anythingllm_runtime_vector_checks_passed", 0),
                "vector_checks_total": summary.get("anythingllm_runtime_vector_checks_total", 0),
                "chat_error": summary.get("anythingllm_runtime_chat_error", ""),
            },
            "chunk_size": summary.get("chunk_size", ""),
            "chunk_overlap": summary.get("chunk_overlap", ""),
            "segment_mode": summary.get("segment_mode", ""),
        })
    record = {
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "run_root": str(run_root),
        "state": completion.get("state"),
        "message": completion.get("message"),
        "mode": mode or (MODE_NATIVE_UPLOAD_LABEL if prepare_and_upload else MODE_LOCAL_ONLY_LABEL),
        "workspace_slug": workspace_slug or "",
        "documents": docs,
        "processing_settings": processing_settings or {},
    }
    try:
        Path(run_root).mkdir(parents=True, exist_ok=True)
        # A terminal audit record must not turn a completed cancellation into a
        # UI failure if a future setting gains a Path-like value. Preserve it
        # as its literal local path instead.
        (Path(run_root) / "ingestion-terminal-record.json").write_text(
            json.dumps(record, indent=2, default=str), encoding="utf-8"
        )
        AUTO_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        prune_background_jsonl(INGESTION_HISTORY_PATH)
        with INGESTION_HISTORY_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except (OSError, TypeError, ValueError) as exc:
        APP_LOGGER.warning("could not append ingestion history: %s", exc)
    return record


def ingestion_history_html(workspace_slug="", limit=12):
    if not INGESTION_HISTORY_PATH.exists():
        return '<div class="artifact-placeholder"><strong>No completed-run history yet.</strong></div>'
    try:
        records = [json.loads(line) for line in INGESTION_HISTORY_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, json.JSONDecodeError) as exc:
        return f'<div class="artifact-placeholder"><strong>Could not read run history:</strong> {html.escape(str(exc))}</div>'
    slug = str(workspace_slug or "").strip()
    if slug:
        records = [record for record in records if str(record.get("workspace_slug") or "") == slug]
    records = records[-limit:]
    if not records:
        return '<div class="artifact-placeholder"><strong>No completed-run history yet.</strong></div>'
    cards = []
    for record in reversed(records):
        state = str(record.get("state") or "warning")
        documents = ", ".join(doc.get("name") or "document" for doc in record.get("documents") or []) or "no document recorded"
        smoke = ", ".join(
            f"{doc.get('runtime_validation', 'not checked')} ({(doc.get('retrieval_smoke') or {}).get('vector_checks_passed', 0)}/{(doc.get('retrieval_smoke') or {}).get('vector_checks_total', 0)} vector checks)"
            for doc in record.get("documents") or []
        ) or "not checked"
        cards.append(
            f'<section class="metadata-file ingestion-history {html.escape(state)}"><div class="metadata-file-name">'
            f'{html.escape(record.get("recorded_at") or "unknown time")} — {html.escape(state)}</div>'
            f'<div class="metadata-status">{html.escape(_safe_history_value(record.get("message")))}</div>'
            f'<div class="metadata-status">Workspace: {html.escape(record.get("workspace_slug") or "local only")}<br>Documents: {html.escape(documents)}<br>Retrieval smoke: {html.escape(smoke)}</div></section>'
        )
    return '<div class="metadata-summary">' + "".join(cards) + "</div>"


def latest_resume_manifest(workspace_slug):
    slug = (workspace_slug or "").strip()
    candidates = sorted(automatic_run_artifact_paths(AUTO_OUTPUT_DIR, "**/resume-embedding-manifest.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in candidates:
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if slug and manifest.get("workspace_slug") != slug:
            continue
        recovery = manifest.get("recovery") or {}
        if recovery.get("state") == "resume_available":
            return path, manifest
    return None, None


def latest_resume_manifest_html(workspace_slug):
    path, manifest = latest_resume_manifest(workspace_slug)
    if path and manifest:
        recovery = manifest.get("recovery") or {}
        return (
                '<div class="metadata-summary"><section class="metadata-file"><div class="metadata-file-name">Interrupted embedding recovery available</div>'
                f'<div class="metadata-status">Resume from batch {html.escape(str(recovery.get("from_batch")))} with '
                f'{html.escape(str(len(recovery.get("remaining_locations") or [])))} prepared locations. '
                'The manifest is an audit artifact only; resuming remains an explicit operator action.</div>'
                f'<div class="metadata-status"><code>{html.escape(str(path))}</code></div></section></div>'
            )
    return '<div class="artifact-placeholder">No interrupted embedding recovery manifest was found for this workspace.</div>'


def reconcile_resume_manifest_late_vectors(manifest):
    """Remove a formerly ambiguous batch only after exact vector evidence exists.

    The recovery manifest intentionally includes the timed-out batch because a
    client timeout cannot prove acceptance. Before an explicit resume, inspect
    that one batch by its exact ``chunkSource`` identities. This prevents a
    late-completing AnythingLLM request from being submitted a second time,
    while never removing later batches that were not attempted.
    """
    recovery = manifest.get("recovery") or {}
    remaining = [str(item) for item in recovery.get("remaining_locations") or [] if str(item).strip()]
    failed_batch = next(
        (
            batch for batch in (manifest.get("batches") or [])
            if str(batch.get("submission_state") or "") != "accepted"
        ),
        None,
    )
    candidates = [
        str(item) for item in ((failed_batch or {}).get("locations") or [])
        if str(item) in remaining
    ]
    if not candidates:
        return remaining, {"status": "not_applicable", "reconciled_locations": 0}

    documents_root = default_anythingllm_documents_dir().resolve()
    payloads = []
    source_sha = ""
    for location in candidates:
        candidate_path = (documents_root / Path(location.replace("/", os.sep))).resolve()
        try:
            candidate_path.relative_to(documents_root)
        except ValueError:
            return remaining, {"status": "unsafe_location", "reconciled_locations": 0}
        try:
            native_document = json.loads(candidate_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return remaining, {"status": "document_unavailable", "reconciled_locations": 0}
        metadata = {
            key: native_document.get(key)
            for key in ANYTHINGLLM_RAW_TEXT_METADATA_FIELDS
            if native_document.get(key) not in {None, ""}
        }
        payloads.append({"metadata": metadata})
        match = re.search(r"local-pdf://sha256/([0-9a-f]{64})", str(metadata.get("docSource") or ""), re.I)
        if match:
            source_sha = match.group(1).lower()
    if len(payloads) != len(candidates) or not source_sha:
        return remaining, {"status": "identity_unavailable", "reconciled_locations": 0}

    report = verify_anythingllm_post_upload(
        default_anythingllm_storage_dir(),
        str(manifest.get("workspace_slug") or ""),
        source_sha,
        payloads,
        upload_locations=candidates,
        observation_mode="full",
    )
    observed = int(report.get("lancedb_matching_rows") or 0)
    if str(report.get("status") or "") in REVIEWABLE_POST_UPLOAD_STATUSES and observed >= len(candidates):
        candidate_set = set(candidates)
        remaining = [location for location in remaining if location not in candidate_set]
        report["reconciled_locations"] = len(candidates)
    else:
        report["reconciled_locations"] = 0
    return remaining, report


def submit_embedding_resume_manifest(
    path,
    manifest,
    api_url,
    api_key,
    workspace_slug,
    *,
    automatic=False,
    expected_run_root=None,
    status_callback=None,
):
    """Reconcile and submit only missing records from one durable manifest."""
    result = {"status": "not_started", "accepted": 0, "reconciled_locations": 0, "message": ""}
    path = Path(path)
    if expected_run_root:
        try:
            path.resolve().relative_to(Path(expected_run_root).resolve())
        except ValueError:
            result.update(status="rejected_outside_run", message="Recovery manifest is outside the selected run.")
            return result
    recovery = manifest.get("recovery") or {}
    locations, reconciliation = reconcile_resume_manifest_late_vectors(manifest)
    reconciled_count = int(reconciliation.get("reconciled_locations") or 0)
    if reconciled_count:
        recovery["remaining_locations"] = locations
        recovery["late_vector_reconciliation"] = {
            "status": reconciliation.get("status"),
            "reconciled_locations": reconciled_count,
            "observed_vectors": reconciliation.get("lancedb_matching_rows"),
            "recorded_at": datetime.now().isoformat(timespec="seconds"),
        }
        _write_automatic_run_json(path, manifest)
    slug = str(manifest.get("workspace_slug") or "").strip()
    if not slug or slug != str(workspace_slug or "").strip() or not is_lancedb_safe_namespace(slug):
        result.update(status="rejected_workspace", message="Recovery manifest does not match the selected safe workspace.")
        return result
    if not locations:
        result.update(status="nothing_to_resume", reconciled_locations=reconciled_count, message="No prepared locations remain after reconciliation.")
        return result
    resolved_api = (api_url or DEFAULT_ANYTHINGLLM_API_URL).strip()
    secret = (api_key or "").strip()
    authentication_mode = "provided_api_key" if secret else "none"
    if not secret:
        # Normal localhost operation uses the one named Desktop service key.
        # A temporary key remains only as a backwards-compatible fallback for
        # an older local install that lacks that managed key.
        secret, authentication_mode = resolve_anythingllm_api_key(resolved_api)
    temporary_key_id = None
    if not secret:
        if automatic:
            result.update(status="no_existing_local_api_key", message="Automatic recovery never creates a new Developer API key.")
            return result
        temporary = create_temporary_desktop_api_key(resolved_api)
        if temporary.get("status") != "created":
            result.update(status="authorization_failed", message=str(temporary.get("error") or "AnythingLLM did not issue a local API key."))
            return result
        secret, temporary_key_id = temporary["secret"], temporary["id"]
        authentication_mode = "temporary_desktop_api_key"
    try:
        resume_parallelism = max(
            1,
            min(
                int(manifest.get("recommended_resume_parallelism") or ANYTHINGLLM_EMBEDDING_FAILURE_FALLBACK_CONCURRENT_BATCHES),
                ANYTHINGLLM_EMBEDDING_MAX_CONCURRENT_BATCHES,
            ),
        )
        report = update_workspace_embeddings_batched(
            resolved_api,
            secret,
            slug,
            locations,
            batch_size=manifest.get("batch_size") or ANYTHINGLLM_EMBEDDING_UPDATE_BATCH_SIZE,
            # The recovery manifest is the durable proof of what may be
            # resumed.  Do not let a new transport attempt overwrite it.
            ledger_path=path.with_name("resume-embedding-attempt-ledger.json"),
            concurrent_batch_limit=resume_parallelism,
            initial_concurrent_batches=resume_parallelism,
            status_callback=status_callback,
        )
    finally:
        if temporary_key_id:
            delete_temporary_desktop_api_key(resolved_api, temporary_key_id)
    if report.get("errors"):
        result.update(status="resume_failed", reconciled_locations=reconciled_count, message="Resume stopped; review the recovery manifest.")
        return result
    result.update(
        status="submitted",
        accepted=int(report.get("accepted") or 0),
        reconciled_locations=reconciled_count,
        authentication=authentication_mode,
        message="Missing prepared locations were submitted; retrieval verification remains required.",
    )
    return result


def resume_latest_embedding_manifest(api_url, api_key, workspace_slug):
    """Explicitly submit only the prepared batches left in the latest recovery manifest."""
    path, manifest = latest_resume_manifest(workspace_slug)
    if not path or not manifest:
        return latest_resume_manifest_html(workspace_slug), gr.update(value="No recovery manifest", variant="secondary")
    result = submit_embedding_resume_manifest(path, manifest, api_url, api_key, workspace_slug)
    if result.get("status") == "rejected_workspace":
        return '<div class="artifact-placeholder"><strong>Recovery manifest rejected.</strong> It does not match the selected safe workspace.</div>', gr.update(value="Recovery manifest rejected", variant="stop")
    if result.get("status") == "nothing_to_resume":
        return '<div class="artifact-placeholder"><strong>Recovery manifest has no pending locations.</strong></div>', gr.update(value="Nothing to resume", variant="secondary")
    if result.get("status") in {"authorization_failed", "no_existing_local_api_key"}:
        return latest_resume_manifest_html(workspace_slug), gr.update(value="Resume needs API authorization", variant="stop")
    if result.get("status") != "submitted":
        return latest_resume_manifest_html(workspace_slug), gr.update(value="Resume stopped — review recovery manifest", variant="stop")
    return (
        '<div class="metadata-summary"><section class="metadata-file"><div class="metadata-file-name">Resume submitted</div>'
        f'<div class="metadata-status">AnythingLLM accepted {html.escape(str(result.get("accepted", 0)))} prepared locations. '
        f'{html.escape(str(result.get("reconciled_locations", 0)))} late-completing location(s) were verified and omitted before resubmission. '
        f'Authorization: {html.escape(str(result.get("authentication") or ""))}. Use Verify current workspace after indexing settles; '
        'acceptance alone is not a retrieval-success claim.</div></section></div>',
        gr.update(value="Resume submitted — verify workspace", variant="secondary"),
    )


RECOVERY_POLICY_LABELS = {
    "Leave everything running": "leave_everything_running",
    "Cancel only my confirmed queued records": "cancel_confirmed_queues",
    "Restart AnythingLLM anyway": "restart_anythingllm_anyway",
}


def apply_latest_recovery_policy(api_url, api_key, workspace_slug, policy_label, restart_confirmed=False):
    """Apply an explicitly chosen recovery action to the latest matching run."""
    path, manifest = latest_resume_manifest(workspace_slug)
    if not path or not manifest:
        return latest_resume_manifest_html(workspace_slug), gr.update(value="No recovery manifest", variant="secondary")
    policy = RECOVERY_POLICY_LABELS.get(str(policy_label or ""), "leave_everything_running")
    run_root = path.parents[2]
    result = recover_automatic_run(
        run_root,
        policy=policy,
        explicit_restart_confirmation=bool(restart_confirmed),
        observation_seconds=AUTOMATIC_RECOVERY_OBSERVATION_SECONDS,
    )
    message = str(result.get("message") or result.get("status") or "Recovery recorded.")
    if result.get("status") in {"restart_confirmation_required", "review_required"}:
        variant = "stop"
    else:
        variant = "secondary"
    return latest_resume_manifest_html(workspace_slug), gr.update(value=message, variant=variant)


def storage_audit_html(workspace_slug):
    storage_dir = default_anythingllm_storage_dir()
    report = anythingllm_storage_audit(storage_dir, workspace_slug)
    if report.get("status") != "complete":
        message = report.get("error") or "Storage audit could not run."
        return (
            '<div class="metadata-summary"><section class="metadata-file"><div class="metadata-file-name">Storage audit</div>'
            f'<div class="metadata-status">{html.escape(message)}</div></section></div>'
        )
    rows = [
        ("AnythingLLM storage path", report.get("storage_dir")),
        ("Workspace document rows (global)", report.get("workspace_document_global_count")),
        ("Workspace document rows (selected scope)", report.get("workspace_document_selected_count")),
        ("document_vectors rows", report.get("document_vector_global_count")),
        ("Custom document JSON files", report.get("custom_document_json_global_count")),
        ("Selected rows missing custom-document file", report.get("missing_docpath_file_count")),
        ("Unreferenced custom-document JSON files", report.get("unreferenced_custom_document_count")),
        ("Orphan vector docIds", report.get("orphan_vector_docid_count")),
    ]
    row_html = "".join(
        '<div class="metadata-key">{}</div><div class="metadata-value">{}</div>'.format(
            html.escape(str(key)),
            html.escape(str(value)),
        )
        for key, value in rows
    )
    sample_html = "".join(
        '<section class="metadata-file"><div class="metadata-file-name">{}</div><pre class="inspector-pre">{}</pre></section>'.format(
            html.escape(title),
            html.escape(json.dumps(value, indent=2, ensure_ascii=False)),
        )
        for title, value in [
            ("Sample missing docpaths", report.get("sample_missing_docpaths") or []),
            ("Sample unreferenced custom-document files", report.get("sample_unreferenced_custom_documents") or []),
            ("Sample orphan vector docIds", report.get("sample_orphan_vector_docids") or []),
        ]
    )
    return (
        '<div class="metadata-summary">'
        '<section class="metadata-file"><div class="metadata-file-name">Storage audit</div>'
        '<div class="metadata-status">Read-only audit. This report does not delete or repair AnythingLLM storage.</div>'
        f'<div class="metadata-grid">{row_html}</div></section>'
        f"{sample_html}"
        "</div>"
    )


def stale_artifact_report_html(workspace_slug):
    storage_dir = default_anythingllm_storage_dir()
    report = anythingllm_stale_artifact_report(storage_dir, workspace_slug)
    if report.get("status") != "complete":
        message = report.get("error") or report.get("operator_summary") or "Dry-run stale-artifact report could not run."
        return (
            '<div class="metadata-summary"><section class="metadata-file"><div class="metadata-file-name">Dry-run stale-artifact repair plan</div>'
            f'<div class="metadata-status">{html.escape(message)}</div></section></div>'
        )
    overview_rows = [
        ("AnythingLLM storage path", report.get("storage_dir")),
        ("Selected workspace scope", report.get("workspace_slug") or "global"),
        ("Candidate buckets", len(report.get("candidate_buckets") or [])),
        ("Operator summary", report.get("operator_summary") or "none"),
    ]
    overview_html = "".join(
        '<div class="metadata-key">{}</div><div class="metadata-value">{}</div>'.format(
            html.escape(str(key)),
            html.escape(str(value)),
        )
        for key, value in overview_rows
    )
    bucket_rows = []
    for bucket in report.get("candidate_buckets") or []:
        bucket_rows.extend(
            [
                ("Bucket", bucket.get("bucket")),
                ("Count", bucket.get("count")),
                ("Scope", bucket.get("scope")),
                ("Risk", bucket.get("risk")),
                ("Why it matters", bucket.get("reason")),
                ("Recommended first step", bucket.get("recommended_first_step")),
            ]
        )
    bucket_html = (
        "".join(
            '<div class="metadata-key">{}</div><div class="metadata-value">{}</div>'.format(
                html.escape(str(key)),
                html.escape(str(value)),
            )
            for key, value in bucket_rows
        )
        if bucket_rows
        else '<div class="metadata-status">No stale-artifact buckets were detected in this read-only pass.</div>'
    )
    sequence_html = "".join(
        '<section class="metadata-file"><div class="metadata-file-name">Step {}</div><div class="metadata-status"><strong>{}</strong><br>{}</div></section>'.format(
            html.escape(str(step.get("step"))),
            html.escape(str(step.get("title") or "")),
            html.escape(str(step.get("details") or "")),
        )
        for step in (report.get("recommended_sequence") or [])
    )
    return (
        '<div class="metadata-summary">'
        '<section class="metadata-file"><div class="metadata-file-name">Dry-run stale-artifact repair plan</div>'
        '<div class="metadata-status">Read-only planning report. No deletion, rewrite, or DB repair has been performed.</div>'
        f'<div class="metadata-grid">{overview_html}</div></section>'
        '<section class="metadata-file"><div class="metadata-file-name">Candidate buckets</div>'
        f'<div class="metadata-grid">{bucket_html}</div></section>'
        f"{sequence_html}"
        "</div>"
    )


def anythingllm_observer_api_health(api_url):
    """Check the existing local runtime without starting it or creating an API key."""
    target = ((api_url or DEFAULT_ANYTHINGLLM_API_URL).strip().rstrip("/") + "/api/ping")
    try:
        request = urllib.request.Request(target, method="GET")
        with urllib.request.urlopen(request, timeout=1.5) as response:
            return {"reachable": True, "http_status": response.status, "error": ""}
    except urllib.error.HTTPError as exc:
        return {"reachable": False, "http_status": exc.code, "error": f"HTTP {exc.code}"}
    except Exception as exc:
        return {"reachable": False, "http_status": None, "error": str(exc)}


def anythingllm_startup_status_html(api_url="", health=None):
    """Render the inexpensive pre-selection Desktop availability signal.

    This deliberately checks only the local API ping. The provider-backed
    embedding probe still runs immediately after Confirm, because a key can
    expire after this page-level status was last refreshed.
    """
    health = health if health is not None else anythingllm_observer_api_health(api_url)
    if health.get("reachable"):
        return ""
    return (
        '<section class="anythingllm-startup-status anythingllm-startup-status--offline" role="alert">'
        '<div><strong>AnythingLLM is not available.</strong> Please start AnythingLLM Desktop, then click Refresh Status. '
        'PDF preparation can continue in local-only mode, but AnythingLLM upload cannot start until Desktop is running.</div>'
        '</section>'
    )


def anythingllm_startup_status_view(api_url=""):
    """Return the quiet pre-selection signal and whether its warning is needed."""
    health = anythingllm_observer_api_health(api_url)
    unavailable = not health.get("reachable")
    return (
        anythingllm_startup_status_html(api_url, health=health),
        gr.update(visible=unavailable),
    )


def refresh_anythingllm_startup_status(api_url=""):
    """Refresh the passive Desktop signal, showing a cue only for an unchanged outage."""
    health = anythingllm_observer_api_health(api_url)
    unavailable = not health.get("reachable")
    return (
        anythingllm_startup_status_html(api_url, health=health),
        gr.update(visible=unavailable),
        gr.update(value="Refresh Status", variant="secondary"),
    )


def automatic_runtime_start_notice(readiness_report):
    """Describe a start performed by this run without treating it as a restart.

    A normal upload preflight may start a closed local Desktop instance. The
    operation is deliberately visible to the person running the job, while a
    process that was already running remains quiet.
    """
    report = readiness_report if isinstance(readiness_report, dict) else {}
    if str(report.get("runtime_start_status") or "") != "started":
        return "", ""
    message = str(report.get("runtime_start_message") or "").strip()
    detail = "AnythingLLM Desktop was unavailable and has been started before PDF preparation."
    if message:
        detail += f" {message}"
    return "AnythingLLM Desktop started automatically", detail


def record_automatic_runtime_preflight(run_root, readiness_report):
    """Persist non-secret runtime-start evidence beside its automatic run."""
    report = readiness_report if isinstance(readiness_report, dict) else {}
    fields = (
        "runtime_api_url",
        "runtime_api_status",
        "runtime_api_reachable",
        "runtime_api_message",
        "runtime_start_status",
        "runtime_start_message",
        "authenticated",
        "authentication_status",
        "authentication_message",
        "workspace_slug",
        "workspace_slug_found",
        "workspace_slug_message",
        "workspace_api_found",
        "workspace_api_message",
    )
    record = {
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "readiness": {field: report.get(field) for field in fields if field in report},
    }
    path = Path(run_root) / AUTOMATIC_RUN_RUNTIME_PREFLIGHT
    _write_automatic_run_json(path, record)
    return path


ANYTHINGLLM_STARTUP_STATUS_REFRESH_JS = r"""
() => {
  const status = document.getElementById("anythingllm-startup-status");
  const button = document.getElementById("refresh-anythingllm-startup-status");
  if (!status?.querySelector(".anythingllm-startup-status--offline") || !button) return;
  window.clearTimeout(window.anythingllmRefreshStatusFlashTimer);
  button.classList.remove("anythingllm-refresh-status-flash");
  void button.offsetWidth;
  button.classList.add("anythingllm-refresh-status-flash");
  window.anythingllmRefreshStatusFlashTimer = window.setTimeout(() => {
    button.classList.remove("anythingllm-refresh-status-flash");
  }, 1300);
}
"""
def anythingllm_desktop_process_seen():
    """Return a best-effort local process signal; API health remains authoritative."""
    if os.name != "nt":
        return None
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq AnythingLLM.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
        return "AnythingLLM.exe" in (result.stdout or "")
    except Exception:
        return None


def _observer_tail_log_lines(storage_dir, workspace_slug, limit_bytes=131072):
    logs_dir = Path(storage_dir) / "logs"
    log_files = sorted(logs_dir.glob("backend-*.log"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not log_files:
        return {"file": "", "modified_epoch": 0.0, "matches": []}
    log_path = log_files[0]
    try:
        with log_path.open("rb") as handle:
            handle.seek(max(0, log_path.stat().st_size - limit_bytes))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError as exc:
        return {"file": str(log_path), "modified_epoch": 0.0, "matches": [], "error": str(exc)}
    ansi = re.compile(r"\x1b\[[0-9;]*m")
    needle = (workspace_slug or "").casefold()
    matches = [
        ansi.sub("", line).strip()
        for line in text.splitlines()
        if needle and needle in ansi.sub("", line).casefold()
    ]
    return {
        "file": str(log_path),
        "modified_epoch": log_path.stat().st_mtime,
        "matches": matches[-4:],
    }


def workspace_ingestion_observer_snapshot(workspace_slug, api_url="", storage_dir=None):
    """Collect short, read-only evidence for a manually initiated embedding run."""
    storage = Path(storage_dir) if storage_dir else default_anythingllm_storage_dir()
    snapshot = {
        "observed_at": datetime.now().isoformat(timespec="seconds"),
        "observed_epoch": time.time(),
        "workspace_slug": (workspace_slug or "").strip(),
        "desktop_process_seen": anythingllm_desktop_process_seen(),
        "api": anythingllm_observer_api_health(api_url),
        "database_status": "not_checked",
        "workspace_found": False,
        "workspace_documents": None,
        "embedded_vectors": None,
        "observed_records": None,
        "storage_evidence_status": "not_checked",
        "storage_embedded_chunks": None,
        "storage_lancedb_rows": None,
        "latest_document_epoch_ms": None,
        "database_error": "",
        "log": {},
    }
    snapshot["log"] = _observer_tail_log_lines(storage, snapshot["workspace_slug"])
    db_path = storage / "anythingllm.db"
    if not snapshot["workspace_slug"]:
        snapshot["database_status"] = "workspace_not_selected"
        return snapshot
    if not db_path.exists():
        snapshot["database_status"] = "database_missing"
        snapshot["database_error"] = f"AnythingLLM database was not found at {db_path}."
        return snapshot
    con = None
    try:
        # Avoid competing with AnythingLLM's writer: an observer never retries or writes.
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.25)
        con.execute("pragma busy_timeout = 250")
        row = con.execute("select id from workspaces where slug = ?", (snapshot["workspace_slug"],)).fetchone()
        if not row:
            snapshot["database_status"] = "workspace_missing"
            return snapshot
        workspace_id = row[0]
        snapshot["workspace_found"] = True
        snapshot["workspace_documents"] = con.execute(
            "select count(*) from workspace_documents where workspaceId = ?", (workspace_id,)
        ).fetchone()[0]
        snapshot["embedded_vectors"] = con.execute(
            "select count(*) from document_vectors where docId in "
            "(select docId from workspace_documents where workspaceId = ?)",
            (workspace_id,),
        ).fetchone()[0]
        snapshot["latest_document_epoch_ms"] = con.execute(
            "select max(lastUpdatedAt) from workspace_documents where workspaceId = ?", (workspace_id,)
        ).fetchone()[0]
        snapshot["database_status"] = "observed"
    except sqlite3.OperationalError as exc:
        snapshot["database_status"] = "database_busy"
        snapshot["database_error"] = str(exc)
    except Exception as exc:
        snapshot["database_status"] = "database_error"
        snapshot["database_error"] = str(exc)
    finally:
        if con is not None:
            con.close()
    # Native raw-text ingestion can write directly to the workspace LanceDB
    # table without creating legacy workspace_documents rows.  Reconcile that
    # first-class vector evidence after the SQLite read has finished; this
    # observer remains strictly read-only.
    if snapshot["database_status"] == "observed":
        storage_report = workspace_storage_inspector(storage, snapshot["workspace_slug"])
        snapshot["storage_evidence_status"] = storage_report.get("status") or "not_checked"
        if storage_report.get("status") == "complete":
            storage_chunks = int(storage_report.get("embedded_chunk_count") or 0)
            storage_rows = int(storage_report.get("lancedb_workspace_row_count") or 0)
            snapshot["storage_embedded_chunks"] = storage_chunks
            snapshot["storage_lancedb_rows"] = storage_rows
            snapshot["embedded_vectors"] = max(int(snapshot.get("embedded_vectors") or 0), storage_chunks)
            snapshot["observed_records"] = max(
                int(snapshot.get("workspace_documents") or 0),
                storage_rows,
            )
        else:
            snapshot["observed_records"] = int(snapshot.get("workspace_documents") or 0)
    return snapshot


def _observer_progressed(previous, current):
    if not previous:
        return False
    fields = ("workspace_documents", "embedded_vectors", "observed_records", "latest_document_epoch_ms")
    if any(current.get(field) != previous.get(field) for field in fields):
        return True
    previous_log = (previous.get("log") or {}).get("matches") or []
    current_log = (current.get("log") or {}).get("matches") or []
    return current_log != previous_log


def _observer_state_status(snapshot, previous_snapshot, expected_records, quiet_since):
    """Classify an observation without inferring completion from a client timeout."""
    if not snapshot.get("api", {}).get("reachable"):
        return "runtime_unreachable", None, 0
    if snapshot.get("database_status") == "database_busy":
        return "database_busy", None, 0
    if snapshot.get("database_status") != "observed":
        return snapshot.get("database_status") or "observation_limited", None, 0
    if _observer_progressed(previous_snapshot, snapshot):
        return "progress_observed", None, 0
    docs = snapshot.get("observed_records") or snapshot.get("workspace_documents") or 0
    vectors = snapshot.get("embedded_vectors") or 0
    expected = max(0, int(expected_records or 0))
    if not expected or docs < expected or vectors < expected:
        return "quiet_incomplete", None, 0
    observed_epoch = float(snapshot.get("observed_epoch") or time.time())
    quiet_started = float(quiet_since or observed_epoch)
    quiet_seconds = max(0, int(observed_epoch - quiet_started))
    if quiet_seconds >= EMBEDDING_OBSERVER_QUIET_SECONDS:
        return "complete_observed", quiet_started, quiet_seconds
    return "completion_candidate", quiet_started, quiet_seconds


def ingestion_observer_html(state):
    snapshot = (state or {}).get("last_snapshot") or {}
    status = (state or {}).get("status") or "not_started"
    api = snapshot.get("api") or {}
    log = snapshot.get("log") or {}
    expected = (state or {}).get("expected_records")
    quiet_seconds = (state or {}).get("quiet_seconds") or 0
    rows = [
        ("Observer state", status.replace("_", " ")),
        ("Observed at", snapshot.get("observed_at") or "not yet observed"),
        ("Desktop process seen", str(snapshot.get("desktop_process_seen"))),
        ("Runtime API", "reachable" if api.get("reachable") else (api.get("error") or "not reachable")),
        ("Database", snapshot.get("database_status") or "not checked"),
        ("Observed records", snapshot.get("observed_records") if snapshot.get("observed_records") is not None else "not observed"),
        ("Legacy workspace documents", snapshot.get("workspace_documents") if snapshot.get("workspace_documents") is not None else "not observed"),
        ("Embedded vectors", snapshot.get("embedded_vectors") if snapshot.get("embedded_vectors") is not None else "not observed"),
        ("Native storage evidence", snapshot.get("storage_evidence_status") or "not checked"),
        ("Expected prepared records", expected if expected else "not supplied"),
        ("Stable time after expected count", f"{quiet_seconds}s / {EMBEDDING_OBSERVER_QUIET_SECONDS}s"),
    ]
    table = "".join(
        f'<div class="metadata-key">{html.escape(str(key))}</div><div class="metadata-value">{html.escape(str(value))}</div>'
        for key, value in rows
    )
    log_preview = "<br>".join(html.escape(line) for line in (log.get("matches") or [])[-2:]) or "No matching recent backend-log line."
    return (
        '<div class="metadata-summary"><section class="metadata-file">'
        '<div class="metadata-file-name">Read-only embedding observation</div>'
        '<div class="metadata-status">Record a baseline before you manually start an embedding update. '
        'The observer neither starts, stops, retries, nor modifies AnythingLLM.</div>'
        f'<div class="metadata-grid">{table}</div>'
        f'<div class="metadata-status"><strong>Recent workspace log evidence:</strong><br>{log_preview}</div>'
        '</section></div>'
    )


def embedding_observer_log_text(state):
    history = (state or {}).get("history") or []
    if not history:
        return "No observation baseline has been recorded."
    return "\n".join(
        " | ".join(
            [
                sample.get("observed_at") or "",
                sample.get("status") or "",
                f"records={sample.get('observed_records')}",
                f"legacy_docs={sample.get('workspace_documents')}",
                f"vectors={sample.get('embedded_vectors')}",
                f"database={sample.get('database_status')}",
            ]
        )
        for sample in history[-12:]
    )


def start_embedding_observer(api_url, workspace_slug, expected_records):
    snapshot = workspace_ingestion_observer_snapshot(workspace_slug, api_url)
    state = {
        "workspace_slug": (workspace_slug or "").strip(),
        "expected_records": max(0, int(expected_records or 0)),
        "status": "baseline_recorded",
        "quiet_since": None,
        "quiet_seconds": 0,
        "last_snapshot": snapshot,
        "history": [dict(snapshot, status="baseline_recorded")],
    }
    return state, ingestion_observer_html(state), embedding_observer_log_text(state)


def sample_embedding_observer(api_url, workspace_slug, expected_records, state):
    previous = (state or {}).get("last_snapshot") or {}
    snapshot = workspace_ingestion_observer_snapshot(workspace_slug, api_url)
    status, quiet_since, quiet_seconds = _observer_state_status(
        snapshot,
        previous,
        expected_records,
        (state or {}).get("quiet_since"),
    )
    updated = {
        "workspace_slug": (workspace_slug or "").strip(),
        "expected_records": max(0, int(expected_records or 0)),
        "status": status,
        "quiet_since": quiet_since,
        "quiet_seconds": quiet_seconds,
        "last_snapshot": snapshot,
        "history": [*((state or {}).get("history") or []), dict(snapshot, status=status)][-24:],
    }
    return updated, ingestion_observer_html(updated), embedding_observer_log_text(updated)


def desktop_refresh_bridge_descriptor_path():
    """Return the guarded Desktop bridge descriptor in durable Desktop storage."""
    app_data = (os.environ.get("APPDATA") or "").strip()
    if not app_data:
        return None
    return Path(app_data) / "anythingllm-desktop" / "storage" / DESKTOP_REFRESH_BRIDGE_FILENAME


def desktop_bridge_process_is_live(pid):
    """Check a bridge's main-process PID without trusting a stale descriptor."""
    if not pid:
        return True
    if os.name == "nt":
        try:
            probe = subprocess.run(
                ["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            return str(int(pid)) in (probe.stdout or "")
        except (OSError, ValueError, subprocess.SubprocessError):
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def read_desktop_refresh_bridge_descriptor(include_capability_token=False):
    """Read and validate the narrow Desktop-refresh capability descriptor.

    The descriptor is created by the guarded Desktop patch, contains a
    per-process random token, and is deleted when AnythingLLM exits. Reading it
    does not contact the Desktop app or alter its UI.
    """
    path = desktop_refresh_bridge_descriptor_path()
    base_report = {"available": False, "descriptor_path": str(path) if path else ""}
    if not path or not path.exists():
        return dict(base_report, status="not_installed_or_not_running")
    try:
        descriptor = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return dict(base_report, status="invalid_descriptor", error=str(exc))
    try:
        port = int(descriptor.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    token = descriptor.get("token")
    try:
        bridge_pid = int(descriptor.get("pid") or 0)
    except (TypeError, ValueError):
        bridge_pid = 0
    try:
        draft_guard_version = int(descriptor.get("draftGuardVersion") or 0)
    except (TypeError, ValueError):
        draft_guard_version = 0
    if (
        descriptor.get("marker") != DESKTOP_REFRESH_BRIDGE_MARKER
        or descriptor.get("schemaVersion") != 1
        or not isinstance(token, str)
        or not DESKTOP_REFRESH_BRIDGE_TOKEN_PATTERN.fullmatch(token)
        or not 1 <= port <= 65535
    ):
        return dict(base_report, status="invalid_descriptor")
    # A forced process stop cannot run Electron's graceful quit cleanup. Do not
    # treat its leftover capability descriptor as a live bridge.
    if bridge_pid and not desktop_bridge_process_is_live(bridge_pid):
        return dict(base_report, status="not_installed_or_not_running", stale_descriptor=True)
    report = dict(
        base_report,
        available=True,
        status="ready",
        port=port,
        pid=bridge_pid or descriptor.get("pid"),
        app_version=descriptor.get("appVersion"),
        bridge_revision=descriptor.get("bridgeRevision") or "unknown",
        bridge_revision_current=(
            descriptor.get("bridgeRevision") == DESKTOP_REFRESH_BRIDGE_CURRENT_REVISION
        ),
        started_at=descriptor.get("startedAt"),
        draft_guard_version=draft_guard_version,
        draft_guard_current=(
            draft_guard_version >= DESKTOP_REFRESH_BRIDGE_REQUIRED_DRAFT_GUARD_VERSION
        ),
    )
    # The token is a loopback capability, not status information.  Keep it out
    # of UI state, diagnostics, logs, and return values unless the one caller
    # that actually sends the authenticated refresh request asks for it.
    if include_capability_token:
        report["token"] = token
    return report


def request_desktop_workspace_refresh(timeout_seconds=6.0):
    """Request the one action the guarded Desktop bridge permits.

    This is intentionally not used by the five-second background timer. Call
    it only after a workspace mutation that has already succeeded locally.
    """
    def finish(report):
        safe = {
            "recorded_at": datetime.now().isoformat(timespec="seconds"),
            "status": report.get("status") or "unknown",
            "app_version": report.get("app_version") or "",
            "bridge_available": bool(report.get("available")),
        }
        response = report.get("refresh_response") or {}
        if isinstance(response, dict):
            safe["renderer_ready"] = response.get("rendererReady")
            safe["action"] = response.get("action") or response.get("error") or ""
        _append_timing_jsonl(DESKTOP_REFRESH_EVENTS_PATH, safe)
        return {key: value for key, value in report.items() if key != "token"}

    descriptor = read_desktop_refresh_bridge_descriptor(include_capability_token=True)
    if not descriptor.get("available"):
        return finish(descriptor)
    if not descriptor.get("draft_guard_current"):
        return finish(dict(
            descriptor,
            status="draft_guard_outdated",
            error=(
                "The installed Desktop refresh bridge predates the required "
                "fail-closed draft guard and was not invoked."
            ),
        ))
    request = urllib.request.Request(
        f"http://127.0.0.1:{descriptor['port']}/v1/refresh-workspaces",
        method="POST",
        headers={"X-AnythingLLM-Pdf-Prep-Bridge": descriptor["token"]},
        data=b"",
    )
    try:
        with urllib.request.urlopen(request, timeout=max(0.1, float(timeout_seconds))) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
        if int(getattr(response, "status", 0) or 0) == 202 and payload.get("ok"):
            report = dict(descriptor, status="refreshed", refresh_response=payload)
        else:
            report = dict(descriptor, status="rejected", refresh_response=payload)
        APP_LOGGER.info("guarded Desktop sidebar refresh result=%s", report["status"])
        return finish(report)
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8") or "{}")
        except (OSError, ValueError, json.JSONDecodeError):
            payload = {}
        if exc.code == 409 and payload.get("error") == "unsent_draft_detected":
            report = dict(descriptor, status="draft_protected", refresh_response=payload)
        else:
            report = dict(descriptor, status="rejected", refresh_response=payload, error=str(exc))
        APP_LOGGER.info("guarded Desktop sidebar refresh result=%s", report["status"])
        return finish(report)
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        report = dict(descriptor, status="unreachable", error=str(exc))
        APP_LOGGER.info("guarded Desktop sidebar refresh result=%s", report["status"])
        return finish(report)


def desktop_workspace_refresh_note(report):
    status = (report or {}).get("status") or "not_checked"
    if status == "refreshed":
        return "Asked the active AnythingLLM Desktop workspace sidebar to refresh."
    if status == "not_installed_or_not_running":
        return "Desktop sidebar refresh bridge is not active; the localhost app refreshed its own workspace state only."
    if status == "unreachable":
        return "Desktop sidebar refresh bridge was detected but could not be reached; the localhost app refreshed its own workspace state only."
    if status == "draft_protected":
        return "Desktop sidebar refresh was deferred because AnythingLLM has unsent draft text; no Desktop UI state was discarded."
    if status == "draft_guard_outdated":
        return "Desktop sidebar refresh was not invoked because its installed draft guard is outdated; update the bridge before any Desktop reload."
    if status == "invalid_descriptor":
        return "Desktop sidebar refresh bridge descriptor is invalid; no Desktop action was attempted."
    if status == "rejected":
        return "Desktop sidebar refresh bridge rejected the request; the localhost app refreshed its own workspace state only."
    return "Desktop sidebar refresh was not requested."


def desktop_refresh_result_html(report):
    """Render the actual guarded-refresh outcome without treating it as drawer proof."""
    status = str((report or {}).get("status") or "not_requested")
    messages = {
        "refreshed": "Desktop refresh: renderer reloaded; verify the Documents drawer separately.",
        "draft_protected": "Desktop refresh: deferred to protect an unsent draft.",
        "not_installed_or_not_running": "Desktop refresh: bridge unavailable.",
        "unreachable": "Desktop refresh: bridge unreachable.",
        "draft_guard_outdated": "Desktop refresh: bridge guard is outdated.",
        "invalid_descriptor": "Desktop refresh: bridge descriptor is invalid.",
        "rejected": "Desktop refresh: bridge rejected the request.",
    }
    message = messages.get(status, "Desktop refresh: not requested.")
    return (
        '<div class="metadata-status desktop-refresh-result">'
        f'{html.escape(message)}'
        '</div>'
    )


def add_desktop_refresh_result_to_run_outputs(run_outputs, report):
    """Append a compact, factual bridge outcome to the terminal timing panel."""
    if not isinstance(run_outputs, (tuple, list)) or len(run_outputs) < 7:
        return run_outputs
    updated = list(run_outputs)
    timing_update = updated[6]
    if not isinstance(timing_update, dict):
        return tuple(updated)
    rendered = str(timing_update.get("value") or "")
    updated[6] = dict(timing_update, value=rendered + desktop_refresh_result_html(report))
    return tuple(updated)


def refresh_desktop_after_anythingllm_mutation():
    """Invoke the guarded Desktop refresh only after a completed mutation."""
    return desktop_workspace_refresh_note(request_desktop_workspace_refresh())


def background_reconciliation_html(workspace_slug, snapshot):
    """Render a deliberately non-authoritative background state snapshot.

    AnythingLLM Desktop can retain a stale document-picker view. This panel is
    intentionally scoped to data the localhost app can observe, rather than
    treating a desktop repaint as an embedding signal.
    """
    slug = (workspace_slug or "").strip()
    api = (snapshot or {}).get("api") or {}
    if not slug or is_new_document_workspace_choice(slug):
        return (
            '<div class="setting-reference-note"><strong>Background sync:</strong> '
            'waiting for a concrete workspace selection. It refreshes local workspace and setting information every '
            f'{BACKGROUND_RECONCILIATION_INTERVAL_SECONDS} seconds without changing AnythingLLM.</div>'
        )
    desktop_bridge = read_desktop_refresh_bridge_descriptor()
    rows = [
        ("Last observed", (snapshot or {}).get("observed_at") or "not yet observed"),
        ("Runtime API", "reachable" if api.get("reachable") else (api.get("error") or "not reachable")),
        ("Workspace documents", (snapshot or {}).get("workspace_documents") if (snapshot or {}).get("workspace_documents") is not None else "not observed"),
        ("Embedded vectors", (snapshot or {}).get("embedded_vectors") if (snapshot or {}).get("embedded_vectors") is not None else "not observed"),
        ("Database", (snapshot or {}).get("database_status") or "not checked"),
        ("Desktop sidebar bridge", "ready" if desktop_bridge.get("available") else "not active"),
    ]
    table = "".join(
        f'<div class="metadata-key">{html.escape(str(key))}</div><div class="metadata-value">{html.escape(str(value))}</div>'
        for key, value in rows
    )
    return (
        '<div class="metadata-summary"><section class="metadata-file">'
        '<div class="metadata-file-name">Background workspace reconciliation</div>'
        '<div class="metadata-status">Read-only local observation. A visible Desktop document list can lag this state; '
        'this panel does not declare an embedding complete or control the Desktop UI. The guarded Desktop bridge is only '
        'called after an already-successful workspace mutation, never by this timer.</div>'
        f'<div class="metadata-grid">{table}</div></section></div>'
    )


def _background_workspace_update(selected_workspace):
    """Refresh local workspace choices without replacing a valid user selection."""
    choices, local_status = local_workspace_choices()
    choices = workspace_choices_with_new_document(choices)
    valid_values = {value for _label, value in choices}
    selected = (selected_workspace or "").strip()
    if selected not in valid_values:
        selected = NEW_DOCUMENT_WORKSPACE_VALUE
    return gr.update(choices=choices, value=selected), local_status


def refresh_background_reconciliation(
    api_url,
    api_key,
    workspace_slug,
    inherit_enabled,
    current_chunk_size,
    current_chunk_overlap,
    current_embedder_max_chunk,
):
    """Refresh app-visible state after local or external AnythingLLM changes.

    This is suitable for a Gradio timer: all inspection is read-only and the
    runtime check has autostart disabled. In particular, it does not create a
    temporary Desktop key, submit another embedding request, or touch the
    AnythingLLM Electron window.
    """
    selected = (workspace_slug or "").strip()
    snapshot = workspace_ingestion_observer_snapshot(selected, api_url)
    workspace_update, local_status = _background_workspace_update(selected)
    resolved_workspace = workspace_update.get("value") or ""
    readiness = native_upload_readiness_html(
        native_upload_readiness_report(
            api_url,
            api_key,
            resolved_workspace,
            autostart_runtime=False,
            verify_authentication=False,
        )
    )
    settings = refresh_anythingllm_settings(
        bool(inherit_enabled),
        current_chunk_size,
        current_chunk_overlap,
        current_embedder_max_chunk,
    )
    status = (
        "Background sync refreshed local workspace and AnythingLLM setting state "
        f"at {snapshot.get('observed_at') or 'an unknown time'}. {local_status}"
    )
    return (
        workspace_update,
        status,
        readiness,
        background_reconciliation_html(resolved_workspace, snapshot),
        *settings,
        anythingllm_settings_reference_html(),
    )


def refresh_metadata_schema(api_url, api_key, autostart_runtime=False):
    resolution = ensure_anythingllm_runtime(
        api_url,
        api_key=(api_key or "").strip(),
        timeout=1.25,
        autostart_local=bool(autostart_runtime),
    )
    api_url = resolution.get("api_url") or (api_url or "").strip() or DEFAULT_ANYTHINGLLM_API_URL
    report = get_anythingllm_metadata_schema(api_url, (api_key or "").strip())
    runtime = f"Runtime API: {report.get('runtime_api_status', 'not checked')}."
    if report.get("authentication_mode") == "temporary_desktop_api_key":
        runtime += " Used a temporary local Desktop API key in memory."
        runtime += (
            " Cleanup: "
            + str(report.get("temporary_key_cleanup", {}).get("status", "not reported"))
            + "."
        )
    if report.get("error"):
        runtime += f" {report['error']}"
    if report.get("runtime_schema"):
        runtime += " Runtime fields: " + ", ".join(report["runtime_schema"]) + "."
    return metadata_contract_text(runtime)


def normalize_lines(value: str):
    return [line.strip() for line in (value or "").splitlines() if line.strip()]


def merged_end_section_headings(value: str):
    """Keep the proven default end-matter signals while accepting additions.

    Advanced users often need a local heading such as ``Appendix`` or
    ``Filmography``.  Treating their text box as a replacement used to make
    accidentally deleting ``References`` silently weaken the baseline.  The
    UI therefore presents this as an additive list, and this helper enforces
    that contract for both Automatic and Advanced diagnostics.
    """
    merged = []
    seen = set()
    for heading in [*DEFAULT_END_SECTION_HEADINGS, *normalize_lines(value)]:
        key = heading.casefold()
        if key and key not in seen:
            seen.add(key)
            merged.append(heading)
    return merged


def file_size_label(path):
    try:
        size = Path(path).stat().st_size
    except OSError:
        return "size unknown"
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"


def artifact_type(path):
    name = Path(path).name.casefold()
    suffix = Path(path).suffix.casefold()
    if "child-parent-map" in name:
        return "Parent-child map"
    if "representation-comparison" in name:
        return "Representation comparison"
    if "metadata-layer-visibility" in name:
        return "Metadata layer report"
    if "column-explanations" in name:
        return "Column explanation report"
    if "harmonization-report" in name:
        return "Harmonization report"
    if "representation-recommendation" in name:
        return "Representation recommendation"
    if "page-parent" in name:
        return "Page-parent artifact"
    if "anythingllm-upload" in name:
        return "AnythingLLM upload file"
    if "manifest" in name:
        return "Segment manifest"
    if "readiness-report" in name or suffix == ".html":
        return "Readiness report"
    if "payload" in name or "metadata-api" in str(path).casefold():
        return "Native metadata payload"
    if "edge-case" in name:
        return "Edge-case report"
    if "workspace-model" in name:
        return "Workspace model gate"
    if "post-upload" in name:
        return "Post-upload verification"
    if "upload-plan" in name or "checklist" in name or suffix == ".zip":
        return "Manual test kit"
    if suffix == ".csv":
        return "CSV report"
    if suffix == ".json" or suffix == ".jsonl":
        return "Structured data"
    return "Generated file"


def artifact_placeholder_html(title="Generated files"):
    if "edge" in title.casefold() or "test" in title.casefold():
        expected = [
            "edge-case-report.html",
            "edge-case-results.csv",
            "workspace-model-gate.csv",
            "post-upload-verification.csv",
            "raw-text-payloads-native-header.jsonl",
            "segment-manifest.jsonl",
            "manual-segment-files.zip",
            "manual-upload-plan.csv",
            "manual-test-checklist.md",
        ]
    else:
        expected = [
            "anythingllm-upload.txt",
        ]
    rows = "\n".join(
        f'<div class="download-row"><span class="download-name">{html.escape(name)}</span><span class="download-status">...</span></div>'
        for name in expected
    )
    title_html = (
        ""
        if title == "Prepared output package"
        else f'<div class="download-title">{html.escape(title)}</div>'
    )
    return f'<div class="download-list-placeholder">{title_html}{rows}</div>'


def artifact_display_html(paths, title="Generated files"):
    clean_paths = []
    seen = set()
    for item in paths or []:
        if not item:
            continue
        path = str(item)
        if path in seen:
            continue
        seen.add(path)
        clean_paths.append(path)
    if not clean_paths:
        return artifact_placeholder_html(title)
    return ""


def selected_pdf_list_html(paths, title="", max_items=None):
    clean_paths = clean_downloadable_paths(paths)
    if not clean_paths:
        return ""
    displayed_paths = clean_paths if max_items is None else clean_paths[:max(1, int(max_items))]
    rows = "\n".join(
        '<div class="download-row"><span class="download-name">{}</span><span class="download-status">{}</span></div>'.format(
            html.escape(path.name),
            html.escape(file_size_label(path)),
        )
        for path in displayed_paths
    )
    if len(displayed_paths) < len(clean_paths):
        rows += (
            '<div class="download-row"><span class="download-name">'
            f"{len(clean_paths) - len(displayed_paths)} more PDF file(s) selected"
            "</span></div>"
        )
    title_html = f'<div class="download-title">{html.escape(title)}</div>' if title else ""
    return f'<div class="download-list-placeholder">{title_html}{rows}</div>'


def batch_folder_notice_html(paths=None):
    """Explain mixed-folder filtering without an acknowledgement overlay."""
    selected = selected_pdf_list_html(paths or [], title="PDFs selected")
    return (
        '<div class="batch-folder-inline-notice" data-notice="Only PDF files were uploaded from this mixed folder">'
        "Only PDF files were uploaded from this mixed folder."
        "</div>"
        + selected
    )


def clean_downloadable_paths(paths):
    clean_paths = []
    seen = set()
    for item in paths or []:
        if not item:
            continue
        path = Path(str(item))
        if not path.exists() or not path.is_file():
            continue
        path_text = str(path)
        if path_text in seen:
            continue
        seen.add(path_text)
        clean_paths.append(path)
    return clean_paths


def clean_existing_paths(paths):
    clean_paths = []
    seen = set()
    for item in paths or []:
        if not item:
            continue
        path = Path(str(item))
        if not path.exists():
            continue
        path_text = str(path)
        if path_text in seen:
            continue
        seen.add(path_text)
        clean_paths.append(path)
    return clean_paths


def is_gradio_safe_download_path(path: Path):
    """Whether Gradio may serve ``path`` without first copying it to cache."""
    try:
        resolved = Path(path).resolve()
        safe_roots = (Path.cwd().resolve(), Path(tempfile.gettempdir()).resolve())
        return any(resolved.is_relative_to(root) for root in safe_roots)
    except OSError:
        return False


def gradio_download_cache_path(filename: str):
    """Allocate a temporary, Gradio-safe path without overwriting another run."""
    GRADIO_DOWNLOAD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    requested = Path(filename)
    stem = safe_stem(requested.stem) or "download"
    suffix = requested.suffix or ".zip"
    candidate = GRADIO_DOWNLOAD_CACHE_DIR / f"{stem}{suffix}"
    if not candidate.exists():
        return candidate
    return GRADIO_DOWNLOAD_CACHE_DIR / (
        f"{stem}-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}{suffix}"
    )


def gradio_safe_download_file(path: Path):
    """Return a path that Gradio's file output can safely move into its cache.

    User-chosen output folders often live under Downloads or another drive.
    Gradio rejects those paths by design. Copy only on download, leaving the
    durable run artifact in place and avoiding an overly broad ``allowed_paths``
    launch setting.
    """
    source = Path(path)
    if is_gradio_safe_download_path(source):
        return source
    target = gradio_download_cache_path(source.name)
    shutil.copy2(source, target)
    return target


def primary_prepared_download_paths(summaries):
    """Return only the prepared text that is usable outside the app.

    Manifests, variants, payloads, and validation reports remain durable run
    evidence, but they are not ordinary user downloads.  The upload payload is
    also the useful OCR-review text when Automatic selected an OCR-capable
    extractor, so it remains the single normal output in either case.
    """
    paths = []
    for summary in summaries or []:
        summary = summary or {}
        candidate = Path(str(summary.get("upload_file") or ""))
        if candidate.is_file():
            paths.append(str(candidate))
            continue
        # A lean retained run may have replaced the rich in-memory summary
        # with a compact run-summary.  Recover the durable prepared-text path
        # directly instead of making a warning/review outcome lose its most
        # useful local result.
        output_root = Path(str(summary.get("output_root") or ""))
        compact_summary = output_root / "run-summary.json"
        if compact_summary.is_file():
            try:
                compact = json.loads(compact_summary.read_text(encoding="utf-8"))
                relative = str((compact.get("artifacts") or {}).get("parsed_text") or "")
                restored = output_root / relative if relative else None
                if restored and restored.is_file():
                    paths.append(str(restored))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
    return paths


def package_downloadable_paths(paths, bundle_name="_download-package.zip"):
    existing_paths = clean_existing_paths(paths)
    if not existing_paths:
        return None
    common_parent = Path(
        os.path.commonpath(
            [str(path if path.is_dir() else path.parent) for path in existing_paths]
        )
    )
    bundle_path = gradio_download_cache_path(bundle_name)
    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in existing_paths:
            if path.is_dir():
                for child in path.rglob("*"):
                    if not child.is_file():
                        continue
                    try:
                        arcname = child.relative_to(common_parent)
                    except ValueError:
                        arcname = Path(path.name) / child.relative_to(path)
                    bundle.write(child, arcname=str(arcname))
            else:
                try:
                    arcname = path.relative_to(common_parent)
                except ValueError:
                    arcname = path.name
                bundle.write(path, arcname=str(arcname))
    return bundle_path


DIAGNOSTIC_EVIDENCE_DIRECTORIES = (
    "candidates",
    "inspection",
    "metadata-api",
    "native-metadata-compatibility-probe",
    "native-metadata-test-kit",
    "retrieval-eval",
)
DIAGNOSTIC_EVIDENCE_ROOT_FILES = (
    "diagnostics.csv",
    "diagnostics.html",
    "diagnostics.json",
    "edge-case-report.html",
    "edge-case-results.csv",
    "edge-case-summary.json",
    "run-checkpoint.json",
    "run-checkpoints.jsonl",
    "run-result.json",
    "run-summary.json",
    "source-profile.json",
)


def diagnostic_evidence_paths(run_directory):
    """Return the forensic-only artifacts for an already completed PDF run."""
    root = Path(str(run_directory or "")).expanduser()
    if not root.is_dir() or not (root / "run-summary.json").is_file():
        return [], "Choose one completed PDF output folder containing run-summary.json."
    paths = []
    for name in DIAGNOSTIC_EVIDENCE_DIRECTORIES:
        candidate = root / name
        if candidate.is_dir():
            paths.append(candidate)
    for name in DIAGNOSTIC_EVIDENCE_ROOT_FILES:
        candidate = root / name
        if candidate.is_file():
            paths.append(candidate)
    return paths, ""


def export_run_diagnostics_evidence(run_directory):
    """Create an opt-in diagnostics bundle outside the normal Automatic flow."""
    paths, error = diagnostic_evidence_paths(run_directory)
    if error:
        return error, gr.update(value=[], visible=False)
    bundle = package_downloadable_paths(paths, bundle_name="_diagnostics-evidence.zip")
    if bundle is None:
        return "No diagnostics evidence files were found for that completed run.", gr.update(value=[], visible=False)
    return f"Diagnostics evidence package created: {bundle.name}", gr.update(value=[str(bundle)], visible=True)


def retained_run_diagnostics_html(run_directory):
    """Render the compact facts retained for a completed Automatic run."""
    root = Path(str(run_directory or "")).expanduser()
    summary_path = root / "run-summary.json"
    if not root.is_dir() or not summary_path.is_file():
        return '<div class="run-diagnostics-summary warning">Choose a completed PDF output folder containing <code>run-summary.json</code>.</div>'
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f'<div class="run-diagnostics-summary warning">Could not read the compact summary: {html.escape(str(exc))}</div>'
    outcome = dict(summary.get("outcome") or {})
    source = dict(summary.get("source") or {})
    preparation = dict(summary.get("preparation") or {})
    artifact = dict(summary.get("artifacts") or {})
    prepared = root / str(artifact.get("parsed_text") or "")
    prepared_status = "available" if prepared.is_file() else "missing"
    rows = [
        ("Status", outcome.get("readiness_status") or summary.get("readiness_status") or "unknown"),
        ("Source", source.get("filename") or source.get("file") or "unknown"),
        ("Backend", outcome.get("selected_backend") or summary.get("selected_backend") or "unknown"),
        ("Prepared text", f"{prepared.name or 'not recorded'} ({prepared_status})"),
        ("Pages / segments", f"{source.get('pdf_page_count', '—')} / {preparation.get('segments', '—')}"),
        ("Chunk settings", f"{preparation.get('chunk_size', '—')} / {preparation.get('chunk_overlap', '—')}"),
        ("Upload verification", outcome.get("post_upload_verification_status") or "not applicable"),
    ]
    cells = "".join(
        f"<tr><th>{html.escape(str(label))}</th><td>{html.escape(str(value))}</td></tr>"
        for label, value in rows
    )
    return (
        '<div class="run-diagnostics-summary"><strong>Completed-run diagnostics</strong>'
        '<p>This is the compact record retained for a ready run. Detailed extraction and metadata artifacts are retained automatically only when a run needs review or fails.</p>'
        f"<table>{cells}</table></div>"
    )


def retained_run_diagnostics_update(run_directory):
    """Show the diagnostics panel only after a concrete completed run is selected."""
    if not str(run_directory or "").strip():
        return gr.update(value="", visible=False)
    return gr.update(
        value=retained_run_diagnostics_html(run_directory),
        visible=True,
    )


def latest_automatic_pdf_output_directory():
    """Find the newest completed per-PDF output below the default Automatic root."""
    candidates = sorted(
        automatic_run_artifact_paths(AUTO_OUTPUT_DIR, "*/run-summary.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return str(candidates[0].parent) if candidates else ""


def latest_advanced_diagnostics_output_directory(output_root_override=""):
    """Find the latest completed local-only Advanced diagnostic run."""
    root = Path(
        str(output_root_override or "").strip() or str(ADVANCED_DIAGNOSTICS_OUTPUT_DIR)
    )
    candidates = sorted(
        automatic_run_artifact_paths(root, "*/run-summary.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return str(candidates[0].parent) if candidates else ""


def segment_package_paths(paths):
    existing_paths = clean_existing_paths(paths)
    packaged = []
    for path in existing_paths:
        lowered = path.name.casefold()
        if path.is_file() and (
            lowered == "manual-segment-files.zip" or lowered.endswith("-segment-files.zip")
        ):
            packaged.append(path)
            continue
        if path.is_dir() and (
            lowered == "manual-segment-files" or lowered.endswith("-segment-files")
        ):
            bundle = package_downloadable_paths([path], bundle_name=f"_{path.name}-download.zip")
            if bundle and bundle.exists():
                packaged.append(bundle)
    return packaged


def download_files_update(paths, download_full_folder=True, download_segments_folder=False):
    existing_paths = clean_existing_paths(paths)
    clean_paths = clean_downloadable_paths(paths)
    if not existing_paths:
        return gr.update(value=[], visible=False)
    packaged = []
    if download_full_folder:
        bundle = package_downloadable_paths(existing_paths)
        if bundle and bundle.exists():
            packaged.append(str(bundle))
    if download_segments_folder:
        packaged.extend(
            str(gradio_safe_download_file(path))
            for path in segment_package_paths(existing_paths)
        )
    if packaged:
        seen = set()
        deduped = [path for path in packaged if not (path in seen or seen.add(path))]
        return gr.update(value=deduped, visible=True)
    return gr.update(
        value=[str(gradio_safe_download_file(path)) for path in clean_paths],
        visible=True,
    )


def unique_copy_path(target_dir, filename):
    candidate = target_dir / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    for index in range(2, 1000):
        alternative = target_dir / f"{stem}-{index}{suffix}"
        if not alternative.exists():
            return alternative
    return target_dir / f"{stem}-{datetime.now().strftime('%H%M%S%f')}{suffix}"


def copy_downloads_to_user_downloads(paths):
    clean_paths = clean_downloadable_paths(paths)

    if not clean_paths:
        return "No generated files are available yet. Run the PDF pipeline first."

    target_dir = USER_DOWNLOADS_DIR / f"RAG Prepared Files {datetime.now().strftime('%Y-%m-%d %H-%M-%S')}"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        for path in clean_paths:
            shutil.copy2(path, unique_copy_path(target_dir, path.name))
    except Exception as exc:
        return f"Could not copy all files to Downloads: {exc}"

    return f"Copied {len(clean_paths)} file(s) to {target_dir}"


def choose_output_directory(current_value=""):
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(
            initialdir=(current_value or str(AUTO_OUTPUT_DIR)),
            title="Choose output folder for prepared PDF runs",
            mustexist=True,
        )
        root.destroy()
    except Exception:
        return current_value or str(AUTO_OUTPUT_DIR)
    if not selected:
        return current_value or str(AUTO_OUTPUT_DIR)
    return selected


def choose_pdf_input_directory(current_value="", *, preserve_on_cancel=True):
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(
            initialdir=(current_value or str(USER_DOWNLOADS_DIR)),
            title="Choose a folder to scan for PDF files",
            mustexist=True,
        )
        root.destroy()
    except Exception as exc:
        APP_LOGGER.warning("native PDF-folder picker failed: %s", exc)
        return (current_value or "") if preserve_on_cancel else None
    if not selected:
        return (current_value or "") if preserve_on_cancel else None
    return selected


def reset_output_directory():
    return str(AUTO_OUTPUT_DIR)


def directory_scan_entries(path_text=""):
    target = Path((path_text or "").strip())
    if not path_text:
        return []
    if not target.exists() or not target.is_dir():
        return []
    try:
        return [str(child) for child in target.iterdir()]
    except OSError:
        return []


def iter_recursive_pdf_folder_scan(path_text="", *, max_documents=BATCH_FOLDER_MAX_DOCUMENTS):
    """Yield bounded folder-discovery progress and return a scan manifest.

    The previous picker inspected only direct children, although its label
    promised folder scanning.  This iterator deliberately skips symlinked
    directories, reports progress without reading PDF contents, and stops at
    a safe interactive limit before an accidental library-wide selection can
    freeze the browser session.
    """
    root = Path((path_text or "").strip())
    stats = {
        "root": str(root),
        "directories_scanned": 0,
        "files_seen": 0,
        "pdf_paths": [],
        "non_pdf_files": 0,
        "scan_errors": [],
        "truncated": False,
        "max_documents": max(1, int(max_documents)),
    }
    if not path_text or not root.exists() or not root.is_dir():
        stats["invalid_root"] = True
        yield "complete", stats
        return

    pending = [root]
    seen_directories = set()
    last_reported_entries = -1
    while pending and not stats["truncated"]:
        current = pending.pop()
        try:
            directory_key = str(current.resolve()).casefold()
        except OSError:
            directory_key = str(current.absolute()).casefold()
        if directory_key in seen_directories:
            continue
        seen_directories.add(directory_key)
        stats["directories_scanned"] += 1
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    stats["files_seen"] += 1
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            if Path(entry.name).suffix.casefold() == ".pdf":
                                stats["pdf_paths"].append(entry.path)
                                if len(stats["pdf_paths"]) >= stats["max_documents"]:
                                    stats["truncated"] = True
                                    break
                            else:
                                stats["non_pdf_files"] += 1
                    except OSError as exc:
                        if len(stats["scan_errors"]) < 5:
                            stats["scan_errors"].append(f"{entry.path}: {exc}")
                    if (
                        stats["files_seen"] - last_reported_entries
                        >= BATCH_FOLDER_SCAN_PROGRESS_INTERVAL
                    ):
                        last_reported_entries = stats["files_seen"]
                        yield "progress", dict(stats, pdf_paths=list(stats["pdf_paths"]))
        except OSError as exc:
            if len(stats["scan_errors"]) < 5:
                stats["scan_errors"].append(f"{current}: {exc}")
        if stats["files_seen"] != last_reported_entries:
            last_reported_entries = stats["files_seen"]
            yield "progress", dict(stats, pdf_paths=list(stats["pdf_paths"]))
    yield "complete", stats


def recursive_pdf_folder_scan(path_text="", *, max_documents=BATCH_FOLDER_MAX_DOCUMENTS):
    """Return the final bounded recursive-discovery manifest."""
    result = None
    for _stage, payload in iter_recursive_pdf_folder_scan(path_text, max_documents=max_documents):
        result = payload
    return result or {
        "root": str(path_text or ""),
        "directories_scanned": 0,
        "files_seen": 0,
        "pdf_paths": [],
        "non_pdf_files": 0,
        "scan_errors": [],
        "truncated": False,
        "max_documents": max(1, int(max_documents)),
        "invalid_root": True,
    }


def folder_scan_progress_html(scan):
    """Render a compact, stable progress line while recursive discovery runs."""
    directories = int((scan or {}).get("directories_scanned") or 0)
    entries = int((scan or {}).get("files_seen") or 0)
    pdfs = len((scan or {}).get("pdf_paths") or [])
    return (
        '<div class="artifact-placeholder batch-folder-scanning"><strong>Scanning PDF folder…</strong>'
        f"<br>Checked {directories} folder{'s' if directories != 1 else ''}, {entries} entr{'y' if entries == 1 else 'ies'}; "
        f"found {pdfs} PDF file{'s' if pdfs != 1 else ''} so far.</div>"
    )


def folder_validation_progress_html(progress):
    checked = int((progress or {}).get("checked") or 0)
    total = int((progress or {}).get("total") or 0)
    valid = int((progress or {}).get("valid_pdfs") or 0)
    return (
        '<div class="artifact-placeholder batch-folder-scanning"><strong>Validating PDF files…</strong>'
        f"<br>Checked {checked} of {total} discovered PDF file{'s' if total != 1 else ''}; "
        f"{valid} readable PDF file{'s' if valid != 1 else ''} confirmed so far.</div>"
    )


def folder_scan_status_html(manifest):
    """Summarize a completed discovery without rendering every selected file."""
    manifest = dict(manifest or {})
    if manifest.get("invalid_root"):
        return '<div class="artifact-placeholder"><strong>Selected folder could not be scanned.</strong></div>'
    pdf_count = len(manifest.get("pdf_candidates") or [])
    directories = int(manifest.get("directories_scanned") or 0)
    entries = int(manifest.get("files_seen") or 0)
    if not pdf_count:
        detail = f"Checked {directories} folder{'s' if directories != 1 else ''} and {entries} entries."
        errors = manifest.get("scan_errors") or []
        if errors:
            detail += " Some paths could not be read; see the application log for details."
        return f'<div class="artifact-placeholder"><strong>No readable PDFs found.</strong><br>{html.escape(detail)}</div>'
    detail = (
        f"Found {pdf_count} readable PDF file{'s' if pdf_count != 1 else ''} "
        f"across {directories} folder{'s' if directories != 1 else ''} ({entries} entries checked)."
    )
    ignored_non_pdf = int(manifest.get("non_pdf_files") or 0)
    if ignored_non_pdf:
        detail += f" Ignoring {ignored_non_pdf} non-PDF file{'s' if ignored_non_pdf != 1 else ''}."
    if manifest.get("truncated"):
        detail += (
            f" Selection stopped at the {int(manifest.get('max_documents') or BATCH_FOLDER_MAX_DOCUMENTS)}-PDF "
            "interactive safety limit; choose a narrower subfolder to process the remainder."
        )
    invalid_count = len(manifest.get("invalid_pdf_headers") or [])
    if invalid_count:
        detail += f" Rejected {invalid_count} file{'s' if invalid_count != 1 else ''} without a valid PDF header."
    if manifest.get("scan_errors"):
        detail += " Some unreadable paths were skipped."
    return f'<div class="artifact-placeholder"><strong>Batch folder ready.</strong><br>{html.escape(detail)}</div>'


def scan_pdf_folder_manifest(path_text=""):
    """Recursively discover and validate PDFs once for the interactive picker."""
    manifest = recursive_pdf_folder_scan(path_text)
    inspection = inspect_uploaded_pdf_candidates(manifest.get("pdf_paths") or [])
    manifest.update(inspection)
    manifest["pdf_candidates"] = inspection["pdf_candidates"]
    manifest["picker_page_details"] = pdf_picker_page_details(manifest["pdf_candidates"])
    manifest["schema_version"] = 1
    APP_LOGGER.info(
        "PDF folder scan completed",
        extra={
            "event": "pdf_folder_scan_completed",
            "folder": manifest.get("root"),
            "pdf_count": len(manifest["pdf_candidates"]),
            "directories_scanned": manifest.get("directories_scanned"),
            "entries_seen": manifest.get("files_seen"),
            "truncated": manifest.get("truncated"),
        },
    )
    return manifest


def folder_manifest_candidates(folder_pdf_files=None, folder_manifest=None):
    """Reuse a just-validated picker manifest for UI-only follow-up callbacks."""
    expected = list(dict.fromkeys(normalize_file_list(folder_pdf_files)))
    manifest = dict(folder_manifest or {}) if isinstance(folder_manifest, dict) else {}
    # ``pdf_candidates`` remains the complete, validated discovery set. A
    # batch can subsequently be narrowed through the individual-file picker,
    # so prefer its explicit selection when present.
    candidates = list(dict.fromkeys(normalize_file_list(
        manifest.get("selected_pdf_candidates")
        if "selected_pdf_candidates" in manifest
        else manifest.get("pdf_candidates")
    )))
    if expected and candidates == expected and not manifest.get("invalid_root"):
        return candidates, True
    return inspect_uploaded_pdf_candidates(folder_pdf_files).get("pdf_candidates", []), False


def batch_folder_relative_label(path_value, root=""):
    path = Path(str(path_value))
    try:
        root_path = Path(str(root)).resolve()
        return str(path.resolve().relative_to(root_path)).replace("\\", "/")
    except (OSError, RuntimeError, ValueError):
        return path.name


PDF_PICKER_NATIVE_INSPECTION_CACHE = {}


def pdf_picker_native_inspection_key(path):
    """Identify an exact local source version without retaining its text."""
    source = Path(path)
    stat = source.stat()
    try:
        resolved = str(source.resolve())
    except OSError:
        resolved = str(source.absolute())
    return resolved, int(stat.st_size), int(stat.st_mtime_ns)


def pdf_picker_native_inspection(path):
    """Reuse an exact page scan until the selected source file changes."""
    source = Path(path)
    key = pdf_picker_native_inspection_key(source)
    cached = PDF_PICKER_NATIVE_INSPECTION_CACHE.get(key)
    if cached:
        return cached
    coverage = automatic_full_native_text_coverage(source)
    if coverage.get("status") != "verified":
        raise RuntimeError(coverage.get("error") or "native page inspection failed")
    detail = {
        "pages": max(0, int(coverage.get("page_count") or 0)),
        "ocr_pages": len(coverage.get("image_backed_low_text_pages") or []),
        "ocr_label": str(len(coverage.get("image_backed_low_text_pages") or [])),
        "page_scan_complete": True,
    }
    result = {"coverage": coverage, "detail": detail}
    PDF_PICKER_NATIVE_INSPECTION_CACHE[key] = result
    return result


def pdf_picker_page_details(paths=None):
    """Return page totals and likely OCR/Unstructured candidate counts per PDF.

    This checks every physical page's native text and embedded-image count,
    then caches the result for the same source bytes/version through Confirm.
    It does not invoke OCR or Unstructured; ``ocr_pages`` means pages that
    look image-backed and lack sufficient native text, not pages already run
    through an OCR backend.
    """
    details = {}
    for raw_path in clean_downloadable_paths(paths or []):
        path = Path(raw_path)
        try:
            details[str(path)] = dict(pdf_picker_native_inspection(path)["detail"])
        except Exception as exc:
            APP_LOGGER.info("PDF picker page inspection skipped for %s: %s", path, exc)
            details[str(path)] = {"pages": "?", "ocr_pages": "?"}
    return details


def merge_uploaded_pdfs_into_folder_batch(pdf_files=None, folder_manifest=None):
    """Fold a new ordinary-picker selection into an existing folder batch.

    The ordinary picker remains independent when no folder batch exists. Once
    a batch exists, uploaded PDFs become selected batch entries and the
    ordinary picker is cleared so the same sources are not represented twice.
    """
    direct_paths = [str(path) for path in inspect_uploaded_pdf_candidates(pdf_files).get("pdf_candidates", [])]
    manifest = dict(folder_manifest or {}) if isinstance(folder_manifest, dict) else {}
    folder_paths = [str(path) for path in clean_downloadable_paths(manifest.get("pdf_candidates") or [])]
    if not direct_paths or not folder_paths:
        return (
            gr.update(), [], manifest, gr.update(), gr.update(), gr.update(), gr.update(),
        )

    candidates = list(dict.fromkeys([*folder_paths, *direct_paths]))
    selected_before = [str(path) for path in clean_downloadable_paths(
        manifest.get("selected_pdf_candidates")
        if "selected_pdf_candidates" in manifest
        else folder_paths
    )]
    selected = list(dict.fromkeys([*selected_before, *direct_paths]))
    page_details = dict(manifest.get("picker_page_details") or {})
    page_details.update(pdf_picker_page_details(direct_paths))
    manifest["pdf_candidates"] = candidates
    manifest["selected_pdf_candidates"] = selected
    manifest["picker_page_details"] = page_details
    return (
        gr.update(value=[]),
        selected,
        manifest,
        gr.update(
            choices=batch_folder_selection_choices(manifest),
            value=selected,
            visible=True,
        ),
        gr.update(visible=True),
        gr.update(value=batch_folder_selected_status_html(manifest, selected), visible=True),
    )


def batch_folder_selection_choices(manifest=None):
    manifest = dict(manifest or {}) if isinstance(manifest, dict) else {}
    root = manifest.get("root") or ""
    candidates = clean_downloadable_paths(manifest.get("pdf_candidates") or [])
    page_details = manifest.get("picker_page_details") or {}
    return [
        (
            f"{batch_folder_relative_label(path, root)} ({file_size_label(path)}) "
            f"({page_details.get(str(path), {}).get('pages', '?')} pages, "
            f"{page_details.get(str(path), {}).get('ocr_label', '?')} OCR)",
            str(path),
        )
        for path in candidates
    ]


def update_batch_folder_selection(manifest=None, selected_paths=None):
    """Keep a reversible subset selection without rescanning the directory."""
    updated = dict(manifest or {}) if isinstance(manifest, dict) else {}
    all_candidates = list(dict.fromkeys(normalize_file_list(updated.get("pdf_candidates"))))
    requested = set(normalize_file_list(selected_paths))
    selected = [path for path in all_candidates if path in requested]
    updated["selected_pdf_candidates"] = selected
    return updated, selected


def batch_folder_selected_status_html(manifest=None, selected_paths=None):
    updated, selected = update_batch_folder_selection(manifest, selected_paths)
    detail = folder_scan_status_html(updated)
    total = len(normalize_file_list(updated.get("pdf_candidates")))
    if total:
        detail = detail.replace(
            "</div>",
            f"<br><strong>{len(selected)} of {total} PDFs selected for this run.</strong></div>",
            1,
        )
    return detail


def apply_batch_folder_file_selection(manifest=None, selected_paths=None):
    updated, selected = update_batch_folder_selection(manifest, selected_paths)
    return (
        selected,
        updated,
        gr.update(
            choices=batch_folder_selection_choices(updated),
            value=selected,
            visible=bool(updated.get("pdf_candidates")),
        ),
        gr.update(value=batch_folder_selected_status_html(updated, selected), visible=True),
    )


def choose_pdf_input_directory_for_scan(current_value=""):
    """Select a root, then let the next event stream the actual scan visibly."""
    selected = choose_pdf_input_directory(current_value, preserve_on_cancel=False)
    if selected is None:
        return (
            gr.update(),
            False,
            gr.update(value="", visible=False),
            gr.update(visible=True),
            gr.update(value="Select PDF Folder Here", interactive=True),
        )
    return (
        selected,
        True,
        gr.update(
            value='<div class="artifact-placeholder batch-folder-scanning"><strong>Preparing folder scan…</strong><br>The selected folder will be searched recursively for PDFs.</div>',
            visible=True,
        ),
        gr.update(visible=True),
        gr.update(value="Scanning PDF folder…", interactive=False),
    )


def stream_selected_pdf_directory(path_text="", scan_requested=False):
    """Stream recursive discovery UI updates, then return one validated manifest."""
    if str((LIVE_AUTOMATIC_RUN_STATUS or {}).get("state") or "") == "running":
        yield tuple(gr.update() for _ in range(7))
        return
    if not scan_requested:
        yield (
            gr.update(), gr.update(), gr.update(), gr.update(), gr.update(),
            gr.update(visible=True),
            gr.update(value="Select PDF Folder Here", interactive=True),
        )
        return

    final_scan = None
    for stage, scan in iter_recursive_pdf_folder_scan(path_text):
        final_scan = scan
        if stage == "progress":
            yield (
                gr.update(),
                gr.update(),
                gr.update(),
                gr.update(),
                gr.update(value=folder_scan_progress_html(scan), visible=True),
                gr.update(visible=True),
                gr.update(value="Scanning PDF folder…", interactive=False),
            )
    manifest = dict(final_scan or {})
    inspection = None
    for stage, payload in iter_uploaded_pdf_candidate_inspection(manifest.get("pdf_paths") or []):
        if stage == "progress":
            yield (
                gr.update(),
                gr.update(),
                gr.update(),
                gr.update(),
                gr.update(value=folder_validation_progress_html(payload), visible=True),
                gr.update(visible=True),
                gr.update(value="Validating PDF files…", interactive=False),
            )
        else:
            inspection = payload
    inspection = inspection or inspect_uploaded_pdf_candidates([])
    manifest.update(inspection)
    manifest["pdf_candidates"] = inspection["pdf_candidates"]
    manifest["picker_page_details"] = pdf_picker_page_details(manifest["pdf_candidates"])
    manifest["schema_version"] = 1
    APP_LOGGER.info(
        "PDF folder scan completed",
        extra={
            "event": "pdf_folder_scan_completed",
            "folder": manifest.get("root"),
            "pdf_count": len(manifest["pdf_candidates"]),
            "directories_scanned": manifest.get("directories_scanned"),
            "entries_seen": manifest.get("files_seen"),
            "truncated": manifest.get("truncated"),
        },
    )
    candidates = manifest["pdf_candidates"]
    manifest["selected_pdf_candidates"] = list(candidates)
    yield (
        candidates,
        manifest,
        gr.update(
            choices=batch_folder_selection_choices(manifest),
            value=candidates,
            visible=bool(candidates),
        ),
        gr.update(visible=bool(candidates)),
        gr.update(value=batch_folder_selected_status_html(manifest, candidates), visible=True),
        gr.update(visible=False),
        gr.update(value="Select PDF Folder Here", interactive=True),
    )


def scan_selected_pdf_directory(
    path_text="",
    current_title="",
    current_author="",
    current_short_label="",
    use_file_title_fallback=True,
):
    if str((LIVE_AUTOMATIC_RUN_STATUS or {}).get("state") or "") == "running":
        # Folder scanning is next-run preparation.  Preserve the active run's
        # visible source/metadata rather than partially replacing it midway.
        return tuple(gr.update() for _ in range(7))
    manifest = scan_pdf_folder_manifest(path_text)
    if not path_text:
        return (
            [],
            "",
            current_title,
            current_author,
            current_short_label,
            '<div class="metadata-summary"><div class="metadata-status">Select a PDF to inspect embedded metadata, technical properties, page count, and bookmarks.</div></div>',
            gr.update(open=False),
        )

    if manifest.get("invalid_root"):
        status_html = (
            '<div class="artifact-placeholder"><strong>Selected folder could not be scanned.</strong>'
            f"<br>{html.escape(path_text)}</div>"
        )
        return (
            [],
            status_html,
            current_title,
            current_author,
            current_short_label,
            '<div class="metadata-summary"><div class="metadata-status">The selected folder did not produce readable files for metadata inspection.</div></div>',
            gr.update(open=False),
        )

    status_html = folder_scan_status_html(manifest)
    title, author, short_label, metadata_preview, section_update = folder_detected_metadata_preview(
        manifest.get("pdf_candidates") or [],
        current_title=current_title,
        current_author=current_author,
        current_short_label=current_short_label,
        use_file_title_fallback=use_file_title_fallback,
    )
    scanned_files = manifest.get("pdf_candidates") or []
    return (
        scanned_files,
        status_html,
        title,
        author,
        short_label,
        metadata_preview,
        section_update,
    )


def choose_and_scan_pdf_directory(
    current_path="",
    current_title="",
    current_author="",
    current_short_label="",
    use_file_title_fallback=True,
):
    if str((LIVE_AUTOMATIC_RUN_STATUS or {}).get("state") or "") == "running":
        # Folder scanning is next-run preparation. Do not even open the native
        # chooser while the active run owns the visible inputs.
        return tuple(gr.update() for _ in range(9))
    selected = choose_pdf_input_directory(current_path)
    manifest = scan_pdf_folder_manifest(selected)
    scanned_files, status_html, title, author, short_label, metadata_preview, section_update = scan_selected_pdf_directory(
        selected,
        current_title=current_title,
        current_author=current_author,
        current_short_label=current_short_label,
        use_file_title_fallback=use_file_title_fallback,
    )
    needs_notice = bool(scanned_files) and bool(manifest.get("non_pdf_files"))
    file_list_update = gr.update(
        value=batch_folder_notice_html(scanned_files) if needs_notice else selected_pdf_list_html(scanned_files),
        visible=bool(scanned_files),
    )
    status_update = gr.update(value="" if scanned_files else status_html, visible=bool(selected and not scanned_files))
    return (
        selected,
        scanned_files,
        file_list_update,
        status_update,
        title,
        author,
        short_label,
        metadata_preview,
        section_update,
    )


def resolve_input_directory(pdf_paths=None, folder_pdf_paths=None):
    candidates = []
    for group in [pdf_paths or [], folder_pdf_paths or []]:
        for item in group:
            if not item:
                continue
            path = Path(str(item))
            if path.exists():
                candidates.append(path.resolve())
    if not candidates:
        return None, "No uploaded PDF source is available yet."
    parents = [path.parent for path in candidates]
    first_parent = parents[0]
    try:
        common_parent = Path(os.path.commonpath([str(parent) for parent in parents]))
    except Exception:
        common_parent = first_parent
    if all(parent == common_parent for parent in parents):
        return common_parent, ""
    return first_parent, "Uploaded PDFs came from multiple folders, so the first source folder was used."


def launch_windows_explorer(path_text=""):
    target = Path((path_text or "").strip() or str(AUTO_OUTPUT_DIR))
    target.mkdir(parents=True, exist_ok=True)
    target_text = str(target.resolve())
    try:
        subprocess.Popen(["explorer.exe", target_text])
        return f"Requested Windows File Explorer for {target_text} via explorer.exe."
    except Exception:
        pass
    try:
        os.startfile(target_text)
        return f"Requested Windows File Explorer for {target_text} via Windows shell."
    except Exception:
        pass
    try:
        subprocess.Popen(["explorer.exe", "/select,", target_text])
        return f"Requested the parent location for {target_text} via explorer.exe /select."
    except Exception as exc:
        return (
            f"Could not launch Windows File Explorer for {target_text}: {exc}. "
            "The folder still exists; open it manually from the shown path."
        )


def open_input_directory(pdf_paths=None, folder_pdf_paths=None):
    target, note = resolve_input_directory(pdf_paths, folder_pdf_paths)
    if not target:
        return note
    message = launch_windows_explorer(str(target))
    if note:
        return f"{message} {note}"
    return message


def open_output_directory(path_text=""):
    return launch_windows_explorer(path_text)


def generated_output_directory(paths, output_root=""):
    """Resolve the most useful concrete folder from this terminal run.

    The prepared transcript is appended first to the terminal run state.  Its
    parent is therefore the exact flat no-logs export folder (or the exact
    document folder in the standard modes), not merely the configured root.
    A batch intentionally opens the first prepared document rather than
    misleadingly selecting an arbitrary timestamped container.
    """
    existing = clean_existing_paths(paths)
    prepared_parents = sorted(
        {
            path.parent
            for path in existing
            if path.is_file() and path.name.endswith("-complete-pdf-parsed.txt")
        },
        key=lambda path: str(path).casefold(),
    )
    if len(prepared_parents) == 1:
        return prepared_parents[0]
    if len(prepared_parents) > 1:
        # A batch has more than one concrete processed-file directory. Open
        # their shared run/export root instead of arbitrarily showing only the
        # first PDF's output.
        try:
            return Path(os.path.commonpath([str(path) for path in prepared_parents]))
        except ValueError:
            # Different drives cannot have one common path. Fall back to the
            # configured root, which is the only honest batch-level target.
            pass
    first_file = next((path for path in existing if path.is_file()), None)
    if first_file:
        return first_file.parent
    configured_text = str(output_root or "").strip()
    return Path(configured_text).expanduser() if configured_text else AUTO_OUTPUT_DIR


def open_generated_output_directory(paths, output_root=""):
    """Open the completed run's real artifact directory, not stale browser state.

    A normal completion event updates ``auto_download_state`` in the browser.
    If that WebSocket event is lost, the one-second recovery poll can still
    restore the button from the durable terminal record.  In that recovery
    path the browser State is deliberately not rewritten, so it can refer to
    an older run.  Prefer the server's current terminal artifacts whenever
    they still exist; only then fall back to the client-provided paths.
    """
    record = LIVE_AUTOMATIC_RUN_STATUS or {}
    terminal_state = str(record.get("state") or "").casefold()
    terminal_paths = clean_existing_paths(record.get("output_paths") or [])
    selected_paths = (
        terminal_paths
        if terminal_state in {"successful", "warning", "failed", "cancelled"} and terminal_paths
        else paths
    )
    return launch_windows_explorer(str(generated_output_directory(selected_paths, output_root)))


def output_folder_button_state(paths, output_root=""):
    """Expose the actual generated folder whenever a terminal artifact exists."""
    target = generated_output_directory(paths, output_root)
    enabled = target.is_dir()
    return gr.update(
        value="Open Generated Output Folder",
        interactive=enabled,
        visible=enabled,
    )


def metadata_selection_layout_state(pdf_files=None, folder_pdf_files=None):
    """Reserve the editable metadata area before slow inspection finishes."""
    has_input = bool(normalize_file_list(pdf_files) or normalize_file_list(folder_pdf_files))
    return gr.update(open=has_input)


def selected_manifest_path(paths):
    candidates = []
    for item in paths or []:
        if not item:
            continue
        path = Path(str(item))
        if path.name == "segment-manifest.jsonl" and path.exists():
            return path
        if path.name.startswith("segment-manifest") and path.suffix == ".jsonl" and path.exists():
            candidates.append(path)
    return candidates[0] if candidates else None


def preview_manifest_segment(paths, segment_number):
    manifest = selected_manifest_path(paths)
    if not manifest:
        return "No segment manifest is available yet. Run the PDF pipeline first."

    try:
        index = int(float(segment_number or 1))
    except (TypeError, ValueError):
        return "Enter a whole segment number, for example 1."
    if index < 1:
        return "Segment numbers start at 1."

    try:
        rows = [
            json.loads(line)
            for line in manifest.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        for position, row in enumerate(rows):
            if int(row.get("segment_index") or position + 1) != index:
                continue
            headings = row.get("headings_on_page") or []
            provenance = row.get("metadata_provenance") or {}
            previous_row = rows[position - 1] if position > 0 else None
            next_row = rows[position + 1] if position + 1 < len(rows) else None
            header = [
                f"Segment {row.get('segment_index')} of {len(rows)} | {row.get('segment_id')}",
                f"Region: {row.get('document_region') or 'unknown'} | backend: {row.get('backend') or 'unknown'}",
                f"PDF page: {row.get('pdf_page')} | logical page: {row.get('logical_page') or 'not detected'}",
                f"Page lines: {row.get('page_line_start') or 'not detected'} - {row.get('page_line_end') or 'not detected'}",
                f"Chapter: {row.get('chapter') or 'not detected'}",
                f"Section: {row.get('section') or 'not detected'}",
                f"Headings on page: {', '.join(headings) if headings else 'none'}",
                f"Character offsets on page: {row.get('char_start_page')} - {row.get('char_end_page')}",
                f"Estimated tokens: {row.get('estimated_tokens') or 'unknown'}",
                f"Boundary confidence: {row.get('boundary_confidence') or 'unknown'}",
                "Metadata provenance: "
                + (", ".join(f"{key}={value}" for key, value in provenance.items()) or "not recorded"),
                f"Quality flags: {', '.join(row.get('quality_flags') or []) or 'none'}",
                f"Previous: {previous_row.get('segment_id') if previous_row else 'none'}",
                f"Next: {next_row.get('segment_id') if next_row else 'none'}",
                "",
            ]
            return "\n".join(header) + (row.get("text") or "")
    except Exception as exc:
        return f"Could not read segment manifest: {exc}"

    return f"Segment {index} was not found in {manifest.name}."


def preview_workspace_segment(paths, workspace_slug, segment_number):
    manifest = selected_manifest_path(paths)
    if not manifest:
        return "No segment manifest is available yet. Run the PDF pipeline first."
    if not (workspace_slug or "").strip():
        return "Select a workspace first to compare the prepared segment against AnythingLLM storage."

    try:
        index = int(float(segment_number or 1))
    except (TypeError, ValueError):
        return "Enter a whole segment number, for example 1."
    if index < 1:
        return "Segment numbers start at 1."

    try:
        rows = [
            json.loads(line)
            for line in manifest.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except Exception as exc:
        return f"Could not read segment manifest: {exc}"

    target = None
    for position, row in enumerate(rows):
        if int(row.get("segment_index") or position + 1) == index:
            target = row
            break
    if not target:
        return f"Segment {index} was not found in {manifest.name}."

    report = workspace_segment_preview(
        default_anythingllm_storage_dir(),
        workspace_slug,
        chunk_source=f"segment://{target.get('segment_id')}",
        title="",
        segment_id=str(target.get("segment_id") or ""),
    )
    lines = [
        f"Prepared segment: {target.get('segment_id')}",
        f"Workspace: {workspace_slug}",
        f"Prepared page lines: {target.get('page_line_start') or 'not detected'} - {target.get('page_line_end') or 'not detected'}",
        f"Title stem: {native_identity_stem(target, include_segment=True)}",
        f"Chunk source: segment://{target.get('segment_id')}",
        f"Preview status: {report.get('status')}",
        f"Matching workspace documents: {report.get('matching_workspace_documents', 0)}",
        f"Matching vector rows: {report.get('matching_vector_rows', 0)}",
    ]
    if report.get("error"):
        lines.extend(["", f"Error: {report['error']}"])
    if report.get("workspace_document"):
        lines.extend(["", "workspace_documents row", pretty_json_preview(report.get("workspace_document"))])
    if report.get("custom_document_record"):
        lines.extend(["", "custom-documents record", pretty_json_preview(report.get("custom_document_record"))])
    lancedb_rows = report.get("lancedb_rows") or []
    if lancedb_rows:
        lines.extend(["", "LanceDB row 1", pretty_json_preview(lancedb_rows[0])])
    return "\n".join(lines)


def navigate_manifest_segment(paths, segment_number, delta):
    try:
        current = int(float(segment_number or 1))
    except (TypeError, ValueError):
        current = 1
    target = max(1, current + int(delta))
    return target, preview_manifest_segment(paths, target)


def navigate_manifest_segment_with_storage(paths, workspace_slug, segment_number, delta):
    try:
        current = int(float(segment_number or 1))
    except (TypeError, ValueError):
        current = 1
    target = max(1, current + int(delta))
    return (
        target,
        preview_manifest_segment(paths, target),
        preview_workspace_segment(paths, workspace_slug, target),
    )


def advanced_diagnostic_backend_settings(choice, unstructured_strategy="auto"):
    """Translate the Advanced diagnostic selector into the normal pipeline contract.

    ``Automatic`` deliberately passes through as ``automatic`` instead of using
    the old lightweight Advanced resolver.  The preparation engine then uses
    the same candidate evaluation, shared-boundary reconciliation, and OCR
    escalation as the ordinary Automatic flow.
    """
    requested = str(choice or ADVANCED_BACKEND_AUTOMATIC_LABEL).strip().casefold()
    strategy = str(unstructured_strategy or "auto").strip().casefold() or "auto"
    if requested == ADVANCED_BACKEND_TESSERACT_LABEL.casefold():
        return "unstructured", "hi_res", "explicit Unstructured OCR (Tesseract)"
    if requested in {"pymupdf", "pymupdf4llm", "unstructured"}:
        return requested, strategy, f"explicit {choice}"
    return "automatic", strategy, "Automatic policy shared with the Automatic tab"


def advanced_diagnostic_progress_stage(stage):
    """Make Automatic candidate evaluation distinct from the selected backend."""
    raw = str(stage or "Preparing diagnostic output")
    normalized = raw.casefold().strip()
    if normalized.startswith("extracting and evaluating with unstructured"):
        return "Testing fallback candidate: Unstructured (not selected yet)"
    if normalized.startswith("extracting and evaluating with "):
        backend = raw[len("Extracting and evaluating with "):].split(" (")[0].strip()
        return f"Evaluating extraction candidate: {backend}"
    return raw


def advanced_diagnostic_pdf_selection_update(
    pdf_file,
    current_title="",
    current_author="",
    current_short_label="",
):
    """Validate the Advanced picker with the same readable-PDF contract.

    File chooser filters are only a convenience: drag/drop and some Windows
    shell picker paths can still supply a non-PDF or a broken PDF.  This keeps
    the Advanced tab from accepting inputs that Automatic would reject.
    """
    raw_paths = normalize_file_list(pdf_file)
    if not raw_paths:
        return (
            gr.update(value=current_title or ""),
            gr.update(value=current_author or ""),
            gr.update(value=current_short_label or ""),
            "",
            gr.update(value="", visible=False),
        )
    files, report = validate_pdf_inputs(raw_paths)
    if report or len(files) != 1:
        detail = report or "Choose exactly one readable PDF for an Advanced diagnostic run."
        return (
            gr.update(value=current_title or ""),
            gr.update(value=current_author or ""),
            gr.update(value=current_short_label or ""),
            '<div class="metadata-summary"><div class="metadata-status">'
            + html.escape(detail)
            + "</div></div>",
            gr.update(
                value=(
                    '<div class="advanced-pdf-warning"><strong>PDF needs attention.</strong>'
                    f"<br>{html.escape(detail)}</div>"
                ),
                visible=True,
            ),
        )

    title_update, author_update, short_update, metadata_html, _accordion = detected_metadata_preview(
        files,
        current_title,
        current_author,
        current_short_label,
        True,
    )
    return (
        title_update,
        author_update,
        short_update,
        metadata_html,
        gr.update(value="", visible=True, interactive=True),
    )


def advanced_diagnostic_action_state(pdf_file):
    """Only enable Advanced generation after its source passes readability checks."""
    files, report = validate_pdf_inputs(normalize_file_list(pdf_file))
    return gr.update(
        value="Generate diagnostic extraction",
        interactive=not report and len(files) == 1,
        visible=True,
    )


def advanced_diagnostic_running_status():
    """Acknowledge the queued Advanced action at the point the user clicks it."""
    return (
        gr.update(
            value=(
                '<div class="advanced-run-status"><strong>Diagnostic extraction started.</strong>'
                "Automatic is evaluating the available extraction candidates. "
                "The result will appear here when that comparison is complete.</div>"
            ),
            visible=True,
        ),
        gr.update(value="Diagnostic extraction is running…", interactive=False),
    )


def advanced_diagnostic_idle_status():
    """Clear the previous run acknowledgement when the source changes."""
    return gr.update(value="", visible=False)


def advanced_diagnostic_completion_status(summary):
    """Describe the actual result without confusing a tested candidate with the winner."""
    result = summary or {}
    outcome = result.get("outcome") or {}
    readiness = str(outcome.get("readiness_status") or result.get("readiness_status") or "unknown")
    backend = result.get("selected_backend") or outcome.get("selected_backend") or "unknown"
    if readiness == "ready":
        heading = "Diagnostic extraction complete."
        detail = f"Selected backend: {backend}."
    else:
        heading = "Diagnostic extraction finished and needs review."
        detail = f"Selected backend: {backend}; review the result below."
    return gr.update(
        value=(
            '<div class="advanced-run-status"><strong>'
            + html.escape(heading)
            + "</strong>"
            + html.escape(detail)
            + "</div>"
        ),
        visible=True,
    )


def advanced_diagnostics_result_html(output_dir, summary, selection_reason=""):
    """Render the compact retained result plus the Automatic decisions worth reviewing."""
    root = Path(str(output_dir or ""))
    compact = retained_run_diagnostics_html(root)
    result = summary or {}
    rows = [
        ("Extraction policy", selection_reason or "Automatic"),
        ("Selected backend", result.get("selected_backend") or "unknown"),
        ("OCR-assisted extraction", "used" if result.get("ocr_assisted_extraction_used") else "not used"),
        ("Shared body boundary", f"page {result.get('start_page', '—')}"),
        (
            "Detected end-matter boundary",
            (
                f"page {result.get('detected_end_page')}"
                if result.get("detected_end_page")
                else "none detected"
            ),
        ),
        ("Boundary evidence", result.get("outline_reliability") or "not reported"),
        ("Backend word disagreement", result.get("backend_word_disagreement", "not reported")),
        ("Diagnostics", f"{result.get('diagnostic_error_count', 0)} errors / {result.get('diagnostic_warning_count', 0)} warnings"),
    ]
    table = "".join(
        f"<tr><th>{html.escape(str(label))}</th><td>{html.escape(str(value))}</td></tr>"
        for label, value in rows
    )
    return (
        '<div class="run-diagnostics-summary"><strong>Advanced diagnostic result</strong>'
        f"<table>{table}</table></div>{compact}"
    )


def run_advanced_diagnostics(
    pdf_file,
    output_root_override,
    document_title,
    document_author,
    document_short_label,
    use_file_title_fallback,
    backend_choice,
    deep_extraction,
    include_front_matter,
    include_back_matter,
    first_page_override,
    end_page_override,
    segment_mode,
    target_passage_policy,
    target_passage_length,
    end_section_names,
    validation_phrases,
    unstructured_strategy,
    retain_detailed_evidence,
    progress=gr.Progress(track_tqdm=False),
):
    """Create one timestamped, local-only diagnostic run through ``prepare_pdf``.

    This intentionally does not call the legacy ``segment_pdf`` converter.
    That converter has a separate structural heuristic and would make a
    diagnostic result disagree with the normal Automatic workflow.
    """
    files, report = validate_pdf_inputs(normalize_file_list(pdf_file))
    if report or len(files) != 1:
        detail = report or "Choose exactly one readable PDF for an Advanced diagnostic run."
        raise gr.Error(detail)
    pdf_path = Path(files[0])
    backend_mode, resolved_strategy, selection_reason = advanced_diagnostic_backend_settings(
        backend_choice,
        unstructured_strategy,
    )
    sizing = target_passage_sizing_plan(
        segment_mode,
        target_passage_policy,
        target_passage_length,
        True,
        0,
        0,
    )
    resolved_target = int(sizing["resolved_target"])
    progress(0.03, desc="Validating Advanced diagnostic input")
    try:
        output_root = Path(
            str(output_root_override or "").strip() or str(ADVANCED_DIAGNOSTICS_OUTPUT_DIR)
        )
        run_root = create_fresh_automatic_run_root(output_root)
        output_dir = compatible_output_document_directory(run_root, pdf_path)
    except OSError as exc:
        raise gr.Error(f"Could not create the Advanced diagnostic output folder: {exc}") from exc

    def report_progress(value, stage):
        progress(
            min(0.98, max(0.04, float(value or 0.0))),
            desc=advanced_diagnostic_progress_stage(stage),
        )

    args = SimpleNamespace(
        document_label=(document_title or "").strip(),
        document_author=(document_author or "").strip(),
        document_short_label=(document_short_label or "").strip(),
        use_file_title_fallback=bool(use_file_title_fallback),
        deep_extraction=bool(deep_extraction),
        include_front_matter=bool(include_front_matter),
        include_back_matter=bool(include_back_matter),
        backend_mode=backend_mode,
        first_page_override=int(first_page_override or 0),
        end_page_override=int(end_page_override or 0),
        target_passage_length=resolved_target,
        segment_mode=pipeline_segment_mode(segment_mode),
        end_section_names=merged_end_section_headings(end_section_names),
        validation_phrases=normalize_lines(validation_phrases),
        unstructured_strategy=resolved_strategy,
        anythingllm_chunk_size=0,
        anythingllm_chunk_overlap=-1,
        marker_style="short",
        disable_inline_markers=False,
        lean_retention=not bool(retain_detailed_evidence),
        run_vector_eval=False,
        simulation_adapter=None,
        simulation_embedder_choice=SIMULATION_SKIP_LABEL,
        ollama_model="",
        ollama_url="",
        max_vector_probes=0,
        max_vector_chunks=0,
        prepare_and_upload=False,
        anythingllm_api_url="",
        anythingllm_api_key="",
        workspace_slug="",
        test_workspace_slug="advanced-diagnostics",
        upload_limit=0,
        native_upload_transport="raw_text",
        native_metadata_upload_mode="native_header",
        native_upload_representation="segments",
        anythingllm_create_document_folders=False,
        anythingllm_document_folder_name="",
        anythingllm_storage_dir="",
        progress_callback=report_progress,
        timing_event_callback=None,
        external_preflight_managed=False,
        temporary_validation_cleanup_policy="cleanup_always",
        cancel_callback=lambda: False,
    )
    try:
        controlled_run = execute_preparation(pdf_path, output_dir, args, prepare_pdf)
        if controlled_run.status != "pass":
            raise RuntimeError(controlled_run.operator_summary)
        summary = legacy_summary_from_run(controlled_run)
        summary["run_control"] = controlled_run.to_dict()
    except Exception as exc:
        classified = classify_pipeline_exception(exc)
        try:
            summary = write_failure_package(pdf_path, output_dir, exc, args)
            summary["app_error_code"] = classified["code"]
            summary["app_error_title"] = classified["title"]
        except Exception as failure_exc:
            raise gr.Error(
                f"Advanced diagnostic preparation failed: {exc}. Failure evidence could not be written: {failure_exc}"
            ) from failure_exc

    progress(1.0, desc="Advanced diagnostic run complete")
    primary = primary_prepared_download_paths([summary])
    if not primary:
        primary = [str(path) for path in (output_dir / "prepared").glob("*.txt") if path.is_file()]
    return (
        gr.update(
            value=advanced_diagnostics_result_html(output_dir, summary, selection_reason),
            visible=True,
        ),
        str(output_dir),
        download_files_update(primary, False, False),
        advanced_diagnostic_completion_status(summary),
        gr.update(visible=False),
    )


def normalize_file_list(pdf_files):
    if not pdf_files:
        return []
    if isinstance(pdf_files, (str, Path)):
        return [str(pdf_files)]
    return [str(item) for item in pdf_files if item]


def iter_uploaded_pdf_candidate_inspection(files, *, progress_interval=32):
    """Yield bounded header-validation progress before the final inspection."""
    pdf_candidates = []
    ignored_non_pdf = []
    missing_paths = []
    directory_entries = []
    empty_pdf_files = []
    invalid_pdf_headers = []
    unreadable_pdf_files = []
    raw_entries = normalize_file_list(files)
    total = len(raw_entries)
    for index, raw_path in enumerate(raw_entries, start=1):
        path = Path(raw_path)
        if not path.exists():
            missing_paths.append(str(path))
            continue
        if path.is_dir():
            directory_entries.append(str(path))
            continue
        if path.suffix.casefold() != ".pdf":
            ignored_non_pdf.append(path.name)
            continue
        try:
            size = path.stat().st_size
        except OSError as exc:
            unreadable_pdf_files.append(f"{path.name}: {exc}")
            continue
        if size == 0:
            empty_pdf_files.append(path.name)
            continue
        header = read_pdf_header(path)
        if isinstance(header, OSError):
            unreadable_pdf_files.append(f"{path.name}: {header}")
            continue
        if not header.startswith(b"%PDF-"):
            invalid_pdf_headers.append(path.name)
        else:
            pdf_candidates.append(str(path))
        if index % max(1, int(progress_interval)) == 0 or index == total:
            yield "progress", {
                "checked": index,
                "total": total,
                "valid_pdfs": len(pdf_candidates),
            }
    yield "complete", {
        "raw_entries": raw_entries,
        "pdf_candidates": list(dict.fromkeys(pdf_candidates)),
        "ignored_non_pdf": ignored_non_pdf,
        "missing_paths": missing_paths,
        "directory_entries": directory_entries,
        "empty_pdf_files": empty_pdf_files,
        "invalid_pdf_headers": invalid_pdf_headers,
        "unreadable_pdf_files": unreadable_pdf_files,
    }


def inspect_uploaded_pdf_candidates(files):
    """Validate a path list synchronously for non-streaming callers."""
    inspection = None
    for _stage, payload in iter_uploaded_pdf_candidate_inspection(files):
        inspection = payload
    return inspection or {
        "raw_entries": [],
        "pdf_candidates": [],
        "ignored_non_pdf": [],
        "missing_paths": [],
        "directory_entries": [],
        "empty_pdf_files": [],
        "invalid_pdf_headers": [],
        "unreadable_pdf_files": [],
    }


def no_pdf_in_folder_report(folder_files):
    inspection = inspect_uploaded_pdf_candidates(folder_files)
    details = ["The uploaded batch folder did not contain any readable PDF files."]
    if inspection["ignored_non_pdf"]:
        sample = ", ".join(inspection["ignored_non_pdf"][:5])
        details.append(
            f"Ignored {len(inspection['ignored_non_pdf'])} non-PDF file(s)"
            + (f", for example: {sample}" if sample else ".")
        )
    if inspection["missing_paths"]:
        sample = ", ".join(Path(path).name for path in inspection["missing_paths"][:5])
        details.append(
            f"{len(inspection['missing_paths'])} uploaded path(s) were missing by the time the app inspected them)"
            + (f", for example: {sample}" if sample else ".")
        )
    if inspection["directory_entries"]:
        sample = ", ".join(Path(path).name or path for path in inspection["directory_entries"][:5])
        details.append(
            "The upload also contained directory entries instead of files"
            + (f", for example: {sample}" if sample else ".")
        )
    if inspection["empty_pdf_files"]:
        sample = ", ".join(inspection["empty_pdf_files"][:5])
        details.append(
            f"{len(inspection['empty_pdf_files'])} .pdf file(s) were empty"
            + (f", for example: {sample}" if sample else ".")
        )
    if inspection["invalid_pdf_headers"]:
        sample = ", ".join(inspection["invalid_pdf_headers"][:5])
        details.append(
            f"{len(inspection['invalid_pdf_headers'])} .pdf file(s) did not contain a valid PDF header"
            + (f", for example: {sample}" if sample else ".")
        )
    if inspection["unreadable_pdf_files"]:
        sample = ", ".join(inspection["unreadable_pdf_files"][:3])
        details.append(
            f"{len(inspection['unreadable_pdf_files'])} .pdf file(s) could not be opened for validation"
            + (f", for example: {sample}" if sample else ".")
        )
    return app_error_report(
        "AUTO-INPUT-003",
        "The uploaded batch folder does not contain PDFs",
        details,
        [
            "Choose a folder that contains one or more real .pdf files.",
            "If the folder is mixed, the app will only use the PDF files after upload.",
            "If you only want to test the UI, upload a small folder that includes at least one valid PDF.",
        ],
    )


def batch_folder_status_html(folder_files):
    inspection = inspect_uploaded_pdf_candidates(folder_files)
    if not inspection["raw_entries"]:
        return ""
    pdf_count = len(inspection["pdf_candidates"])
    ignored_count = len(inspection["ignored_non_pdf"])
    missing_count = len(inspection["missing_paths"])
    dir_count = len(inspection["directory_entries"])
    empty_count = len(inspection["empty_pdf_files"])
    invalid_header_count = len(inspection["invalid_pdf_headers"])
    unreadable_count = len(inspection["unreadable_pdf_files"])
    if pdf_count == 0:
        details = ["No readable PDF files were found in the uploaded folder payload."]
        if ignored_count:
            sample = ", ".join(inspection["ignored_non_pdf"][:5])
            details.append(
                f"Ignored {ignored_count} non-PDF file(s)"
                + (f", for example: {sample}" if sample else "")
                + "."
            )
        if empty_count:
            sample = ", ".join(inspection["empty_pdf_files"][:5])
            details.append(
                f"Rejected {empty_count} empty .pdf file(s)"
                + (f", for example: {sample}" if sample else "")
                + "."
            )
        if invalid_header_count:
            sample = ", ".join(inspection["invalid_pdf_headers"][:5])
            details.append(
                f"Rejected {invalid_header_count} .pdf file(s) without a valid PDF header"
                + (f", for example: {sample}" if sample else "")
                + "."
            )
        if unreadable_count:
            details.append(f"{unreadable_count} .pdf file(s) could not be opened for validation.")
        if missing_count:
            details.append(f"{missing_count} uploaded path(s) were missing at inspection time.")
        if dir_count:
            details.append(f"{dir_count} directory entr{'' if dir_count == 1 else 'ies'} were included in the payload.")
        body = "<br>".join(html.escape(line) for line in details)
        return (
            '<div class="artifact-placeholder"><strong>No PDFs found in selected folder.</strong>'
            f"<br>{body}</div>"
        )

    summary = [f"Found {pdf_count} readable PDF file{'s' if pdf_count != 1 else ''} in the selected folder."]
    if ignored_count:
        summary.append(f"Ignoring {ignored_count} non-PDF file{'s' if ignored_count != 1 else ''}.")
    if empty_count:
        summary.append(f"Rejected {empty_count} empty .pdf file{'s' if empty_count != 1 else ''}.")
    if invalid_header_count:
        summary.append(
            f"Rejected {invalid_header_count} .pdf file{'s' if invalid_header_count != 1 else ''} without a valid PDF header."
        )
    if unreadable_count:
        summary.append(
            f"{unreadable_count} .pdf file{'s' if unreadable_count != 1 else ''} could not be opened for validation."
        )
    if missing_count:
        summary.append(f"{missing_count} uploaded path(s) were missing by inspection time.")
    if dir_count:
        summary.append(f"{dir_count} directory entr{'' if dir_count == 1 else 'ies'} were ignored.")
    return (
        '<div class="artifact-placeholder"><strong>Batch folder ready.</strong>'
        f"<br>{'<br>'.join(html.escape(line) for line in summary)}</div>"
    )


def folder_detected_metadata_preview(
    folder_files,
    current_title="",
    current_author="",
    current_short_label="",
    use_file_title_fallback=True,
    folder_manifest=None,
):
    candidates, _manifest_reused = folder_manifest_candidates(folder_files, folder_manifest)
    return detected_metadata_preview(
        candidates,
        current_title=current_title,
        current_author=current_author,
        current_short_label=current_short_label,
        use_file_title_fallback=use_file_title_fallback,
    )


def automatic_detected_metadata_preview(
    pdf_files,
    folder_files,
    current_title="",
    current_author="",
    current_short_label="",
    use_file_title_fallback=True,
    folder_manifest=None,
):
    """Inspect the batch when present, otherwise the ordinary picker files."""
    folder_candidates, _manifest_reused = folder_manifest_candidates(folder_files, folder_manifest)
    return detected_metadata_preview(
        folder_candidates or normalize_file_list(pdf_files),
        current_title=current_title,
        current_author=current_author,
        current_short_label=current_short_label,
        use_file_title_fallback=use_file_title_fallback,
    )


def automatic_process_button_state(pdf_files=None, folder_pdf_files=None, folder_manifest=None, processed=False):
    if str((LIVE_AUTOMATIC_RUN_STATUS or {}).get("state") or "") == "running":
        # The observer owns the action row while work is active.  A late file
        # selection callback must not re-enable Review on top of Cancel.
        return gr.update()
    folder_candidates, _manifest_reused = folder_manifest_candidates(folder_pdf_files, folder_manifest)
    has_input = bool(normalize_file_list(pdf_files) or folder_candidates)
    if not has_input:
        return gr.update(
            value="Confirm and start processing",
            interactive=False,
            variant="secondary",
        )
    if processed:
        return gr.update(
            value="Processing successful ✓",
            # This component remains bound to the irreversible Confirm
            # handler. A completed state is evidence, not an invitation to
            # replay preparation/upload with the same PDF selection.
            interactive=False,
            variant="huggingface",
        )
    return gr.update(
        value="Confirm and start processing",
        interactive=True,
        variant="primary",
    )


def automatic_selection_action_states(pdf_files=None, folder_pdf_files=None, folder_manifest=None):
    """Reveal the inert Cancel control only after a real selection settles.

    Gradio's File.change callback can run before its temporary path has fully
    propagated to an earlier reset handler. Pairing this with the final
    readiness callback keeps Confirm and Cancel visually consistent.
    """
    folder_candidates, _manifest_reused = folder_manifest_candidates(folder_pdf_files, folder_manifest)
    has_input = bool(normalize_file_list(pdf_files) or folder_candidates)
    if str((LIVE_AUTOMATIC_RUN_STATUS or {}).get("state") or "") == "preparing":
        clear_live_automatic_run_status()
    return (
        automatic_process_button_state(pdf_files, folder_pdf_files, folder_manifest),
        gr.update(value="Cancel", interactive=False, visible=has_input),
        gr.update(value=automatic_live_status_html({"state": "ready"}), visible=True),
    )


def automatic_selection_pending_action_states(pdf_files=None, folder_pdf_files=None):
    """Show the action row as soon as the selector has a usable value.

    Metadata extraction, history lookup, and workspace-name derivation may be
    expensive for a large batch. They must not make the page look inert. The
    final readiness callback still enables Confirm only after those values are
    settled, so this is presentation feedback rather than early authorization.
    """
    has_input = bool(normalize_file_list(pdf_files) or normalize_file_list(folder_pdf_files))
    return (
        gr.update(
            value="Confirm and start processing",
            interactive=False,
            visible=True,
            variant="primary",
        ),
        gr.update(value="Cancel", interactive=False, visible=has_input),
    )


def format_run_duration(seconds):
    total = max(0, int(round(float(seconds or 0))))
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def observed_phase_timing_lines(summary):
    """Render non-additive pipeline timings for the detailed run report."""
    timing = dict((summary or {}).get("phase_timing") or {})
    if not timing:
        return []
    lines = []
    extraction = float(timing.get("extraction_seconds") or 0.0)
    if extraction:
        lines.append(f"Observed native extraction: {format_run_duration(extraction)}")
    queue = dict(timing.get("desktop_queue") or {})
    batches = int(queue.get("batches_completed") or 0)
    if batches:
        records = int(queue.get("records_submitted") or 0)
        lines.append(
            "Observed AnythingLLM Desktop queue: "
            f"{format_run_duration(queue.get('batch_elapsed_seconds', 0))} across "
            f"{batches} completed batch(es), {records} record(s); "
            f"submission {format_run_duration(queue.get('submission_seconds', 0))}, "
            f"verification {format_run_duration(queue.get('verification_seconds', 0))}"
        )
    if lines:
        lines.append("Timing interpretation: phase timers can overlap; Completed is the overall wall time.")
    return lines


def format_estimate_clock(seconds, *, signed=False):
    """Format a run estimate as the compact clock requested by the UI."""
    value = int(round(float(seconds or 0)))
    prefix = "-" if signed and value < 0 else ""
    total = abs(value) if signed else max(0, value)
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    hour_text = f"{hours:02d}h" if hours else ""
    return f"{prefix}{hour_text}{minutes:02d}m{seconds:02d}s"


# The status worker can receive several real stage callbacks in rapid
# succession. Presenting each raw jump makes the bar look like a stopwatch
# rather than an estimate. Limit displayed catch-up to four whole percentage
# points per second.  A later observation may make an ETA less certain, but it
# can never revoke work the interface has already shown as complete.
VISIBLE_PROGRESS_SPEED_PER_SECOND = 0.04
# A queue-rate reprice is based on the observed queue interval and does not
# yet include the confirmation and handoff tail.  Keep this measured safety
# margin before using that optimistic forecast to pull a real x/y checkpoint
# forward.  It changes only bar presentation; the visible ETA is unchanged.
PRESENTATION_ETA_CONSERVATISM = 1.20
# A few early Desktop queue events are dominated by worker start-up and SSE
# delivery timing.  They are evidence of activity, not yet a dependable rate.
# Do not turn them into a multi-minute visible ETA revision.
QUEUE_ETA_MIN_COMPLETED_RECORDS = 12
QUEUE_ETA_MIN_EVENT_SAMPLES = 12
QUEUE_ETA_REPRICE_INTERVAL_SECONDS = 30.0
QUEUE_ETA_MAX_CHANGE_RATIO = 0.25


def concurrent_ingestion_progress_fraction(evidence_fraction, elapsed_seconds, expected_seconds):
    """Combine owned queue/vector evidence with the current elapsed/ETA pair.

    This is used only when a real ingestion callback arrives.  The ETA caps
    evidence that would otherwise outrun its forecast, but never supplies
    progress by itself: a stalled queue must hold at its last owned x/y
    checkpoint. Queue and exact-vector observations therefore remain one
    concurrent interval rather than two additive phases.
    """
    evidence = min(.95, max(0.0, float(evidence_fraction or 0.0)))
    expected = max(0.0, float(expected_seconds or 0.0))
    if expected <= 0.0:
        return evidence
    presentation_expected = expected * PRESENTATION_ETA_CONSERVATISM
    elapsed_share = min(
        .95,
        max(0.0, float(elapsed_seconds or 0.0) / presentation_expected),
    )
    return min(.95, min(evidence, elapsed_share + .05))
def raw_paced_progress_fraction(record, now=None):
    """Return the evidence-plus-time target before display smoothing."""
    now = time.time() if now is None else float(now)
    confirmed = min(1.0, max(0.0, float(record.get("confirmed_fraction") or 0.0)))
    if record.get("cancel_requested"):
        # Once the operator has requested a stop, neither elapsed time nor
        # late worker events are progress evidence. Keep the bar exactly at
        # the last confirmed checkpoint while the worker is terminated.
        return confirmed
    phase_started = float(record.get("phase_started_epoch") or now)
    budget_value = record.get("phase_budget_seconds")
    allowance_value = record.get("phase_allowance")
    budget = max(1.0, float(15.0 if budget_value is None else budget_value))
    # Zero is a valid explicit allowance: it means the bar should wait for
    # evidence rather than fill a phase from elapsed time.
    allowance = max(0.0, float(0.025 if allowance_value is None else allowance_value))
    phase_start = min(confirmed, max(0.0, float(record.get("phase_start_fraction") or confirmed)))
    paced = phase_start + allowance * min(1.0, max(0.0, now - phase_started) / budget)
    return min(0.995, max(confirmed, paced))


def paced_progress_fraction(record, now=None):
    """Return the stable visible fraction, rather than every raw estimate."""
    now = time.time() if now is None else float(now)
    state = str(record.get("state") or "")
    if state in {"successful", "warning"}:
        return 1.0
    if record.get("cancel_requested"):
        # The cancel request is durable before the owned worker necessarily
        # observes it. A final progress event can therefore arrive while the
        # status is still technically ``running``. It is not new completed
        # work and must not make the visible checkpoint creep forward.
        return min(
            0.995,
            max(0.0, float(record.get("display_anchor_fraction") or record.get("confirmed_fraction") or 0.0)),
        )
    raw = raw_paced_progress_fraction(record, now)
    anchor = min(0.995, max(0.0, float(record.get("display_anchor_fraction") or raw)))
    anchor_epoch = float(record.get("display_anchor_epoch") or now)
    target = min(0.995, max(anchor, raw, float(record.get("display_target_fraction") or raw)))
    # A learned ETA is not evidence. Do not make a completed vector batch look
    # only 70% complete merely because a pessimistic opening estimate says 70%.
    # Real stage evidence stays below terminal completion by construction; the
    # independent clock communicates whether that evidence arrived early/late.
    elapsed = max(0.0, now - anchor_epoch)
    return min(target, anchor + elapsed * VISIBLE_PROGRESS_SPEED_PER_SECOND)


def update_live_automatic_run_status(
    run_root=None,
    *,
    state,
    phase,
    expected_seconds=0,
    details="",
    confirmed_fraction=None,
    cancel_available=None,
    cancel_requested=None,
    activity_observed=True,
    estimate_range=None,
    confidence_label=None,
    comparable_runs=None,
    output_paths=None,
    reset_progress=False,
    progress_phase=None,
    completed_units=None,
    total_units=None,
    evidence_kind=None,
    eta_reprice_reason=None,
):
    """Persist progress evidence plus a deliberately capped time-based estimate.

    ``confirmed_fraction`` only advances from actual stage/batch evidence.  The
    browser may fill a small, paced part of the current stage from elapsed time,
    but it cannot run past that stage's cap while the worker is slow or stuck.
    """
    global LIVE_AUTOMATIC_RUN_STATUS
    previous = dict(LIVE_AUTOMATIC_RUN_STATUS or {})
    requested_run_root = str(run_root or "")
    # A new run must not inherit either the previous run's completion evidence
    # or its visible-progress floor.
    if requested_run_root and previous.get("run_root") and requested_run_root != previous.get("run_root"):
        previous = {}
    # A bounded Desktop recovery currently replays local preparation before it
    # can submit the document again.  That is a new attempt, not 99% completed
    # work, so explicitly discard only its display high-water state.  Preserve
    # the original run root and start time for durable recovery evidence.
    if reset_progress and previous:
        previous = {
            **previous,
            "confirmed_fraction": 0.0,
            "display_anchor_fraction": 0.0,
            "display_target_fraction": 0.0,
            "elapsed_percent_floor": 0,
            "phase_started_epoch": 0.0,
            "phase_start_fraction": 0.0,
            "phase_allowance": 0.0,
            "phase_budget_seconds": 0.0,
        }
    now = time.time()
    previous_confirmed = float(previous.get("confirmed_fraction") or 0.0)
    try:
        incoming_confirmed = float(confirmed_fraction)
    except (TypeError, ValueError):
        incoming_confirmed = previous_confirmed
    cancellation_freezes_progress = bool(
        previous.get("cancel_requested") or cancel_requested
    )
    confirmed = (
        previous_confirmed
        if cancellation_freezes_progress and previous
        else min(1.0, max(previous_confirmed, incoming_confirmed, 0.0))
    )
    # A phase name is the durable protocol boundary. Its human-readable text
    # may change every second (for example 93/663 then 94/663 vectors), which
    # must not reset the small, bounded estimate allowance each time.
    incoming_progress_phase = str(progress_phase or "").strip()
    previous_progress_phase = str(previous.get("progress_phase") or "").strip()
    phase_changed = (
        incoming_progress_phase != previous_progress_phase
        if incoming_progress_phase else str(phase or "Working") != str(previous.get("phase") or "")
    )
    expected = max(0, int(expected_seconds or previous.get("expected_seconds") or 0))
    if cancel_available is None:
        cancel_available = previous.get("cancel_available", str(state or "") == "running")
    if cancel_requested is None:
        cancel_requested = previous.get("cancel_requested", False)
    comparable_value = previous.get("comparable_runs") if comparable_runs is None else comparable_runs
    try:
        comparable_value = max(0, int(comparable_value or 0))
    except (TypeError, ValueError):
        comparable_value = 0
    remaining_fraction = max(0.0, 1.0 - confirmed)
    # A phase earns at most a modest forecast allowance. If the phase exceeds
    # its budget, the bar waits at the cap for real evidence instead of racing
    # falsely towards completion.
    has_count_evidence = completed_units is not None and total_units is not None
    phase_allowance = (
        0.0
        if cancellation_freezes_progress
        # Measured x/y phases already supply real movement. Retain only a
        # very small, explicitly bounded interpolation during a quiet poll
        # interval, instead of applying the old generic 2.5--8% timer jump.
        else min(0.04, max(0.01, remaining_fraction * 0.06))
        if has_count_evidence
        else min(0.08, max(0.025, remaining_fraction * 0.18))
    )
    phase_budget = max(8.0, min(45.0, (expected * max(phase_allowance, 0.04)) if expected else 15.0))
    # Retain the visible clock's high-water mark when a batch-derived ETA
    # revision changes its denominator. This is display state only; the raw
    # elapsed/estimate ratio remains available in progress-trace.jsonl.
    previous_elapsed_floor = displayed_elapsed_time_percent(previous, now) if previous else 0
    current_elapsed_raw = (
        (max(0.0, now - float(previous.get("started_epoch") or now)) / expected * 100.0)
        if expected and previous else 0.0
    )
    terminal_state = str(state or "").casefold() in {"successful", "warning", "failed", "cancelled"}
    # A terminal presentation can be emitted after the worker has already
    # stopped producing pipeline evidence (for example after a browser/app
    # resume).  Preserve the last *actual* pipeline activity separately from
    # the outer request lifetime.  This makes a completed duration auditable
    # instead of silently charging inactive time to PDF processing.
    prior_activity = float(previous.get("last_activity_epoch") or previous.get("updated_epoch") or now)
    last_activity = now if activity_observed else prior_activity
    record = {
        "state": str(state or "running"),
        "phase": str(phase or "Working"),
        "progress_phase": incoming_progress_phase or previous_progress_phase,
        "completed_units": completed_units if completed_units is not None else previous.get("completed_units"),
        "total_units": total_units if total_units is not None else previous.get("total_units"),
        "evidence_kind": str(evidence_kind or previous.get("evidence_kind") or ""),
        # A calibration exception is allowed only immediately after this
        # explicit, evidence-backed ETA reprice.  Persist the reason so a
        # later benchmark can distinguish it from an ordinary status repaint.
        "eta_reprice_reason": str(eta_reprice_reason or ""),
        "details": str(details or ""),
        "cancel_available": bool(cancel_available),
        "cancel_requested": bool(cancel_requested),
        "expected_seconds": expected,
        "estimate_range": str(
            previous.get("estimate_range") if estimate_range is None else estimate_range or ""
        ),
        "confidence_label": str(
            previous.get("confidence_label") if confidence_label is None else confidence_label or ""
        ),
        "comparable_runs": comparable_value,
        "confirmed_fraction": confirmed,
        "phase_started_epoch": now if phase_changed else float(previous.get("phase_started_epoch") or now),
        "phase_start_fraction": confirmed if phase_changed else float(previous.get("phase_start_fraction") or confirmed),
        "phase_allowance": phase_allowance if phase_changed else float(previous.get("phase_allowance") or phase_allowance),
        "phase_budget_seconds": phase_budget if phase_changed else float(previous.get("phase_budget_seconds") or phase_budget),
        "started_epoch": float(previous.get("started_epoch") or now),
        "finished_epoch": (
            now
            if terminal_state
            else float(previous.get("finished_epoch") or 0.0)
        ),
        "last_activity_epoch": last_activity,
        "activity_observed": bool(activity_observed),
        # Elapsed-estimate share is diagnostic only.  It must never be allowed
        # to masquerade as completed work while a long OCR/upload phase is
        # still active (the old high-water mark repeatedly became 100%).
        "elapsed_percent_floor": min(99, max(previous_elapsed_floor, int(math.floor(current_elapsed_raw)))),
        "updated_epoch": now,
        "run_root": str(run_root or previous.get("run_root") or ""),
        # Ordinary local artifact paths allow the read-only status poller to
        # restore the completed-run folder button after a lost stream.
        "output_paths": (
            [str(path) for path in (output_paths or []) if str(path or "").strip()]
            if output_paths is not None
            else list(previous.get("output_paths") or [])
        ),
    }
    # Do not derive ETA acceleration from the evidence bar. A fast sequence
    # of cheap checkpoints is not evidence that AnythingLLM's next embedding
    # request will be fast. The clock remains a literal countdown; the timing
    # model is adjusted only from completed, measured runs and batch timings.
    record["eta_acceleration_seconds"] = 0.0
    raw_target = raw_paced_progress_fraction(record, now)
    evidence_checkpoint = (
        incoming_progress_phase in {
            "desktop_queue",
            "identity_set",
            # Retrieval/validation are discrete, confirmed stage boundaries.
            # Holding them behind interpolation can make a real 78--94%
            # transition invisible until terminal completion, which both
            # understates the run and leaves no observable 80% checkpoint.
            "retrieval_sample",
            "validation",
        }
        and completed_units is not None
        and total_units is not None
        and not cancellation_freezes_progress
    )
    previous_visible = paced_progress_fraction(previous, now) if previous else 0.0
    visible_floor = max(
        0.0,
        float(previous.get("display_anchor_fraction") or 0.0) if previous else 0.0,
        previous_visible,
    )
    if evidence_checkpoint:
        # A queue/vector x/y callback or a confirmed retrieval/validation
        # boundary has already been reconciled by ``report_automatic_progress``.
        # Do not make that proven checkpoint wait behind visual smoothing, but
        # never move the visible bar backwards when a prior paced checkpoint
        # was ahead of the next x/y observation.
        display_anchor = max(visible_floor, raw_target)
        display_target = display_anchor
    elif previous:
        # A new ETA can slow or speed the future clock, never turn a completed
        # UI checkpoint into negative progress.  The underlying trace retains
        # the raw ETA/evidence relationship for calibration analysis.
        display_anchor = visible_floor
        display_target = max(visible_floor, raw_target)
    else:
        display_anchor = raw_target
        display_target = raw_target
    record["display_anchor_fraction"] = display_anchor
    record["display_anchor_epoch"] = now
    record["display_target_fraction"] = display_target
    LIVE_AUTOMATIC_RUN_STATUS = record
    status_path = Path(record["run_root"]) / "run-progress.json" if record["run_root"] else None
    if status_path:
        try:
            temporary_path = status_path.with_suffix(".json.tmp")
            temporary_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
            temporary_path.replace(status_path)
        except OSError as exc:
            APP_LOGGER.warning("could not persist automatic run progress: %s", exc)
    # The JSON snapshot above powers recovery. This compact append-only trace
    # preserves the evidence bar and clock relationship that a screenshot
    # shows, so ETA calibration can be audited stage by stage after a run.
    if record["run_root"]:
        try:
            trace_path = Path(record["run_root"]) / "progress-trace.jsonl"
            elapsed = max(0.0, now - float(record.get("started_epoch") or now))
            expected = max(0, int(record.get("expected_seconds") or 0))
            elapsed_percent = raw_elapsed_time_percent(record, now)
            trace_entry = {
                "recorded_at": datetime.now().isoformat(timespec="milliseconds"),
                "state": record["state"],
                "phase": record["phase"],
                "progress_phase": record.get("progress_phase", ""),
                "completed_units": record.get("completed_units"),
                "total_units": record.get("total_units"),
                "evidence_kind": record.get("evidence_kind", ""),
                "eta_reprice_reason": record.get("eta_reprice_reason", ""),
                "details": record["details"],
                "confirmed_percent": round(float(record["confirmed_fraction"]) * 100.0, 3),
                "visible_progress_percent": paced_progress_percent(record, now),
                "elapsed_seconds": round(elapsed, 3),
                "active_window_seconds": round(
                    max(0.0, float(record.get("last_activity_epoch") or now) - float(record.get("started_epoch") or now)),
                    3,
                ),
                "activity_observed": bool(record.get("activity_observed", True)),
                "elapsed_percent_uncapped": round(elapsed_percent, 3),
                "elapsed_percent_display": displayed_elapsed_time_percent(record, now),
                "expected_seconds": expected,
            }
            with trace_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(trace_entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            APP_LOGGER.warning("could not append automatic run progress trace: %s", exc)
    return record


def paced_progress_percent(record, now=None):
    """Return a whole-percent, slow-safe estimate for the visible progress bar."""
    now = time.time() if now is None else float(now)
    state = str(record.get("state") or "")
    if state in {"successful", "warning"}:
        return 100
    if state in {"cancelled", "failed"}:
        # Terminal status must never continue to look alive because the page
        # timer refreshed. Freeze it at the last displayed evidence checkpoint.
        frozen = float(
            record.get("display_anchor_fraction")
            or record.get("confirmed_fraction")
            or 0.0
        )
        return min(99, math.ceil(max(0.0, min(0.99, frozen)) * 100.0 - 1e-9))
    # Round up to whole percentages, but never show 100% before terminal
    # evidence has been received.
    return min(99, math.ceil(paced_progress_fraction(record, now) * 100.0 - 1e-9))


def raw_elapsed_time_percent(record, now=None):
    """Return the uncapped elapsed share for audit purposes only."""
    now = time.time() if now is None else float(now)
    expected = max(0, int(record.get("expected_seconds") or 0))
    started = float(record.get("started_epoch") or now)
    elapsed = max(0.0, now - started)
    return (elapsed / expected * 100.0) if expected else 0.0


def displayed_elapsed_time_percent(record, now=None):
    """Return a capped, non-decreasing elapsed-time indicator.

    Batch observations can legitimately revise the remaining-time forecast.
    They must not make the visible ``Time used`` bar jump backwards merely
    because its denominator grew.  Keep its previous high-water mark while
    preserving the uncapped raw value in the trace for ETA analysis.
    """
    raw = raw_elapsed_time_percent(record, now)
    floor = max(0, int(record.get("elapsed_percent_floor") or 0))
    # While the run is active, elapsed share is only a diagnostic and must not
    # announce completion before terminal pipeline evidence exists.
    terminal = str(record.get("state") or "").casefold() in {"successful", "warning", "failed", "cancelled"}
    return min(100 if terminal else 99, max(floor, int(math.floor(raw))))


def automatic_live_status_html(status=None):
    """Render the one visible, evidence-led automatic-run progress bar.

    The bar itself only follows confirmed work plus the bounded in-phase
    pacing allowance.  Elapsed/remaining time remains useful diagnostic
    context, but it is text inside the bar's label rather than a second bar:
    elapsed estimate share is not process completion evidence.
    """
    record = status or LIVE_AUTOMATIC_RUN_STATUS or {}
    state = str(record.get("state") or "")
    if not state or state == "ready":
        return (
            '<div class="automatic-run-activity ready" data-run-state="ready" '
            'data-cancel-available="false" data-cancel-requested="false">'
            '<div class="automatic-run-progress" role="progressbar" aria-label="Overall run progress" '
            'aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">'
            '<div class="automatic-run-progress-fill" style="width: 0%"></div></div>'
            '<div class="automatic-run-progress-label"><strong>Overall progress: 0%</strong> '
            '<span>Ready — Confirm to begin processing.</span></div></div>'
        )
    if state == "preparing":
        phase = html.escape(str(record.get("phase") or "Pre-processing"))
        details = html.escape(str(record.get("details") or "Preparing the confirmed document run."))
        cancel_available = "true" if record.get("cancel_available", False) else "false"
        cancel_requested = "true" if record.get("cancel_requested", False) else "false"
        return (
            '<div class="automatic-run-activity preparing" data-run-state="preparing" '
            f'data-cancel-available="{cancel_available}" data-cancel-requested="{cancel_requested}">'
            '<div class="automatic-run-progress" role="progressbar" aria-label="Overall run progress" '
            'aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">'
            '<div class="automatic-run-progress-fill" style="width: 0%"></div></div>'
            '<div class="automatic-run-progress-label"><strong>Overall progress: 0%</strong> '
            f'<span>{phase} — {details}</span></div></div>'
        )
    phase = html.escape(str(record.get("phase") or "Working"))
    details = html.escape(str(record.get("details") or ""))
    suffix = f" — {details}" if details else ""
    percent = paced_progress_percent(record)
    percent_text = f"{percent:d}%"
    now = time.time()
    cancel_available = "true" if record.get("cancel_available", True) else "false"
    cancel_requested = "true" if record.get("cancel_requested", False) else "false"
    timing_text = ""
    if state == "running":
        started = float(record.get("started_epoch") or now)
        elapsed = max(0, int(now - started))
        timing_text = (
            '<span class="automatic-run-progress-timing">'
            f"Elapsed {format_estimate_clock(elapsed)}"
            "</span>"
        )
    elif state in {"successful", "warning", "failed", "cancelled"}:
        started = float(record.get("started_epoch") or now)
        # The terminal UI deliberately reports the observed work window, not
        # a possibly later request/browser finalization time.
        finished = float(
            record.get("last_activity_epoch")
            or record.get("finished_epoch")
            or record.get("updated_epoch")
            or now
        )
        actual = max(0, int(finished - started))
        terminal_label = "Stopped" if state == "cancelled" else "Completed"
        timing_text = (
            '<span class="automatic-run-progress-timing">'
            f"{terminal_label} {format_estimate_clock(actual)}"
            "</span>"
        )
    return (
        f'<div class="automatic-run-activity {html.escape(state)}" '
        f'data-run-state="{html.escape(state)}" '
        f'data-cancel-available="{cancel_available}" '
        f'data-cancel-requested="{cancel_requested}">'
        f'<div class="automatic-run-progress" role="progressbar" aria-label="Overall run progress" '
        f'aria-valuemin="0" aria-valuemax="100" aria-valuenow="{percent:d}">'
        f'<div class="automatic-run-progress-fill" style="width: {percent:d}%"></div></div>'
        f'<div class="automatic-run-progress-label"><strong>Overall progress: {percent_text}</strong> '
        f'<span>{phase}</span>{suffix}{timing_text}</div></div>'
    )


def refresh_live_automatic_run_status():
    """Read-only UI poller; never changes AnythingLLM or retries work."""
    rendered = automatic_live_status_html()
    return gr.update(value=rendered, visible=True)


def refresh_live_automatic_run_ui():
    """Reconcile an active run while retaining its stable terminal evidence.

    The worker writes ``run-progress.json`` before returning its final streamed
    response.  An active record stays authoritative while a run is in flight;
    a terminal record remains authoritative until the user starts or resets a
    run. This prevents the final timer from flickering back to its original
    estimate between one-second observer ticks.
    """
    global LIVE_AUTOMATIC_RUN_STATUS
    record = dict(LIVE_AUTOMATIC_RUN_STATUS or {})
    rendered = automatic_live_status_html(record)
    state = str(record.get("state") or "")
    activity = gr.update(value=rendered, visible=bool(rendered))
    if state not in {"successful", "warning", "failed", "cancelled"}:
        # The durable server record owns active ETA rendering. It subtracts
        # elapsed seconds every tick even if the browser's own timer stalls.
        timing = automatic_run_timing_html(
            record.get("expected_seconds", 0),
            state="running",
            started_epoch=record.get("started_epoch"),
            eta_acceleration_seconds=record.get("eta_acceleration_seconds", 0),
            server_driven=True,
            estimate_range=record.get("estimate_range", ""),
            confidence_label=record.get("confidence_label", ""),
            comparable_runs=record.get("comparable_runs"),
        ) if state == "running" else gr.update()
        return (
            activity,
            timing,
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(),
            gr.update(
                value="Stopping processing…" if record.get("cancel_requested") else "Cancel",
                interactive=not bool(record.get("cancel_requested")),
                visible=True,
            ),
            gr.update(),
            gr.update(),
            gr.update(),
        )

    elapsed = max(
        0.0,
        float(record.get("last_activity_epoch") or record.get("updated_epoch") or time.time())
        - float(record.get("started_epoch") or time.time()),
    )
    message = str(record.get("details") or "Run completed.")
    completion = {"state": state, "message": message}
    timing = automatic_run_timing_html(
        record.get("expected_seconds", 0),
        "durable run status",
        state=state,
        actual_seconds=elapsed,
        message=message,
    )
    summary = gr.update(
        value=run_summary_html(f"Status: {state}\nCompletion assessment: {message}"),
        visible=True,
    )
    failure_code_match = re.search(r"\b([A-Z][A-Z0-9-]*-\d{3})\b", message)
    failure_code = failure_code_match.group(1) if failure_code_match else "AUTO-RUN-RECONCILED-001"
    reconciliation_attention = (
        state == "warning"
        and "AUTO-EMBEDDING-RECONCILE-001" in message
    )
    failure = (
        automatic_run_failure_banner_update(failure_code, message)
        if state == "failed" or reconciliation_attention
        else gr.update(value="", visible=False)
    )
    primary_button = automatic_completion_button_state(completion)
    return (
        activity,
        timing,
        gr.update(visible=False, interactive=False),
        primary_button,
        gr.update(),
        gr.update(),
        gr.update(value="Cancel", interactive=False),
        failure,
        summary,
        output_folder_button_state(record.get("output_paths") or [], record.get("run_root") or ""),
    )


def clear_live_automatic_run_status():
    global LIVE_AUTOMATIC_RUN_STATUS
    LIVE_AUTOMATIC_RUN_STATUS = {}
    return gr.update(value=automatic_live_status_html({"state": "ready"}), visible=True)


def reset_automatic_run_presentation(pdf_files=None, folder_pdf_files=None):
    """Return the complete fresh-run presentation after a selection change.

    A new selection is never a resume request.  Clear every transient
    completion/confirmation/download view.  The companion settings callback
    restores the documented defaults; the repeat-run notice is a separate
    history lookup that cannot resume or otherwise alter the new run.
    """
    global LIVE_AUTOMATIC_RUN_STATUS
    if str((LIVE_AUTOMATIC_RUN_STATUS or {}).get("state") or "") == "running":
        # A selection event must not erase an in-flight run's durable evidence
        # or make a completed batch look like a fresh idle state. The selected
        # files are still preserved by Gradio for the next explicit run.
        return tuple(gr.update() for _ in range(19))
    has_input = bool(normalize_file_list(pdf_files) or normalize_file_list(folder_pdf_files))
    LIVE_AUTOMATIC_RUN_STATUS = (
        {"state": "preparing", "phase": "Finishing preparation"} if has_input else {}
    )
    return (
        gr.update(
            value=automatic_live_status_html(LIVE_AUTOMATIC_RUN_STATUS or {"state": "ready"}),
            visible=True,
        ),
        gr.update(value=automatic_run_timing_html(state="ready")),
        # The retired review control stays mounted but invisible to preserve
        # the long-lived Gradio output contract without a second action.
        gr.update(visible=False, interactive=False),
        gr.update(value="Confirm and start processing", variant="primary", interactive=False),
        gr.update(),
        gr.update(value=""),
        {},
        gr.update(),
        gr.update(value="Cancel", interactive=False, visible=has_input),
        gr.update(value="", visible=False),
        gr.update(value="", visible=False),
        gr.update(value=[], visible=False),
        gr.update(value=artifact_placeholder_html("Prepared output package")),
        [],
        gr.update(visible=False, interactive=False),
        gr.update(open=False),
        gr.update(value=1),
        gr.update(value="Run the PDF pipeline first, then type a segment number."),
        gr.update(value="After native upload, this shows the matching workspace_documents record, custom-documents JSON, and a sample LanceDB row for the selected segment."),
    )


def reset_automatic_run_settings_to_defaults():
    """Restore each per-run control to the same defaults as a fresh page load.

    Persisted AnythingLLM engine/model settings are intentionally not changed:
    they are application configuration, not state inherited from a previous
    PDF run.  The API key input returns to blank; the managed local key remains
    intact and is still resolved at confirmation time.
    """
    if str((LIVE_AUTOMATIC_RUN_STATUS or {}).get("state") or "") == "running":
        return tuple(gr.update() for _ in range(41))
    return (
        gr.update(value=""),
        gr.update(value=""),
        gr.update(value=""),
        gr.update(value=True),
        gr.update(value=MODE_NATIVE_UPLOAD_LABEL),
        gr.update(value=str(AUTO_OUTPUT_DIR)),
        gr.update(value=DEFAULT_ANYTHINGLLM_API_URL),
        gr.update(value=""),
        gr.update(value=INITIAL_WORKSPACE_VALUE),
        gr.update(value="", visible=True),
        "",
        gr.update(value=NATIVE_UPLOAD_SCOPE_ALL_LABEL),
        gr.update(value="", visible=False),
        gr.update(value=NATIVE_BOUNDARY_CURRENT_LABEL),
        gr.update(value="Native title header (priority)"),
        # Keep reset aligned with the visible-by-default initial control.
        gr.update(value=False),
        gr.update(value=""),
        gr.update(value=INITIAL_SIMULATION_VALUE),
        gr.update(value=""),
        gr.update(value=DEFAULT_OLLAMA_URL),
        gr.update(value="Full corpus"),
        gr.update(value=False),
        gr.update(value=True),
        gr.update(value=True),
        gr.update(value=SEGMENT_PAGE_LIMIT_LABEL),
        gr.update(value="Automatic"),
        gr.update(value=0),
        gr.update(value=0),
        gr.update(value=TARGET_PASSAGE_INHERIT_LABEL),
        gr.update(value=str(DEFAULT_TARGET_PASSAGE_LENGTH)),
        gr.update(value=0, visible=True),
        gr.update(value=True),
        gr.update(value=current_anythingllm_chunk_size_value()),
        gr.update(value=current_anythingllm_chunk_overlap_value()),
        gr.update(value=False),
        gr.update(value=False),
        gr.update(value=False),
        gr.update(value="\n".join(DEFAULT_END_SECTION_HEADINGS)),
        gr.update(value=""),
        gr.update(value="auto"),
        gr.update(value=True),
    )


def active_automatic_run_root():
    """Find the current run even when Gradio serves controls in another process."""
    live_root = str((LIVE_AUTOMATIC_RUN_STATUS or {}).get("run_root") or "")
    if live_root:
        return Path(live_root)
    candidates = sorted(
        automatic_run_artifact_paths(AUTO_OUTPUT_DIR, "run-progress.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for progress_path in candidates:
        try:
            if time.time() - progress_path.stat().st_mtime > AUTOMATIC_RUN_PROGRESS_STALE_SECONDS:
                continue
            record = json.loads(progress_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(record.get("state") or "") in {"running", "preparing"}:
            return Path(str(record.get("run_root") or progress_path.parent))
    return None


def _read_automatic_run_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _append_automatic_recovery_history(record):
    try:
        AUTOMATIC_RECOVERY_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with AUTOMATIC_RECOVERY_HISTORY_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        APP_LOGGER.warning("could not record automatic recovery history: %s", exc)


def _recovery_ledger_groups(run_root):
    """Yield each ledger with its nearest worker configuration, never a sibling's."""
    root = Path(run_root)
    for ledger_path in sorted(root.rglob("embedding-batch-ledger.json")):
        ledger = _read_automatic_run_json(ledger_path)
        workspace_slug = str(ledger.get("workspace_slug") or "").strip()
        locations = confirmed_submission_locations_from_ledger(ledger)
        if not workspace_slug or not locations:
            continue
        config = {}
        parent = ledger_path.parent
        while True:
            candidate = parent / ".automatic-worker-config.json"
            if candidate.is_file():
                config = _read_automatic_run_json(candidate)
                break
            if parent == root or root not in parent.parents:
                break
            parent = parent.parent
        args = config.get("args") if isinstance(config.get("args"), dict) else {}
        yield {
            "ledger_path": str(ledger_path),
            "workspace_slug": workspace_slug,
            "locations": locations,
            "api_url": str(args.get("anythingllm_api_url") or DEFAULT_ANYTHINGLLM_API_URL).strip(),
            "provided_api_key": str(args.get("anythingllm_api_key") or "").strip(),
        }


def _is_most_recent_recovery_run(run_root):
    root = Path(run_root)
    candidates = sorted(
        automatic_run_artifact_paths(AUTO_OUTPUT_DIR, "**/resume-embedding-manifest.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return True
    return root == candidates[0].parents[2]


def recover_automatic_run(
    run_root,
    *,
    policy="leave_everything_running",
    automatic=False,
    explicit_restart_confirmation=False,
    observation_seconds=AUTOMATIC_RECOVERY_OBSERVATION_SECONDS,
    grace_seconds=AUTOMATIC_RECOVERY_GRACE_SECONDS,
    sleeper=time.sleep,
):
    """Apply one bounded, durable recovery policy to one interrupted run.

    ``leave_everything_running`` is the default. Automatic recovery is limited
    to the latest interrupted run and only when live SSE evidence identifies
    owned activity and no non-owned activity. Quiet or unavailable streams are
    treated as uncertainty and never trigger queue mutation or a restart.
    """
    root = Path(run_root)
    result = {
        "schema_version": 1,
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "run_root": str(root),
        "policy": str(policy or "leave_everything_running"),
        "automatic": bool(automatic),
        "status": "not_started",
        "groups": [],
        "message": "",
    }
    if not root.is_dir():
        result.update(status="run_root_unavailable", message="Recovery run folder is unavailable.")
    elif automatic and not _is_most_recent_recovery_run(root):
        result.update(status="blocked_not_most_recent", message="Only the most recent interrupted run may resume automatically.")
    elif policy == "restart_anythingllm_anyway" and not explicit_restart_confirmation:
        result.update(status="restart_confirmation_required", message="Restart AnythingLLM anyway requires explicit destructive confirmation.")
    else:
        groups = list(_recovery_ledger_groups(root))
        if not groups:
            result.update(status="no_confirmed_submissions", message="No confirmed submitted or pending records were found in this run.")
        for group in groups:
            api_url = group["api_url"]
            secret, auth_mode = resolve_anythingllm_api_key(api_url, group["provided_api_key"] or None)
            row = {**group, "authentication": auth_mode, "action": "none"}
            if not secret:
                row.update(status="no_existing_local_api_key", message="No existing local API key was available; no key was created.")
                result["groups"].append(row)
                continue
            activity = observe_workspace_embedding_queue_activity(
                api_url, secret, group["workspace_slug"], group["locations"],
                observation_seconds=observation_seconds,
            )
            row["activity"] = activity
            if policy == "leave_everything_running":
                row.update(status="left_running", message="Default policy left AnythingLLM unchanged.")
            elif policy == "restart_anythingllm_anyway":
                row["restart"] = restart_anythingllm_desktop(api_url, secret)
                row.update(action="restart", status=str(row["restart"].get("status") or "unknown"))
            elif activity.get("status") != "owned_activity_observed":
                row.update(status="blocked_by_manual_activity_or_uncertainty", message="AnythingLLM was not changed because manual activity or uncertainty was observed.")
            else:
                cleanup = remove_confirmed_workspace_queue_entries(
                    api_url, secret, group["workspace_slug"], group["locations"], activity,
                    total_timeout=AUTOMATIC_RECOVERY_CLEANUP_TIMEOUT_SECONDS,
                )
                row["cleanup"] = cleanup
                row["action"] = "cancel_confirmed_queues"
                if policy == "cancel_confirmed_queues":
                    row["status"] = cleanup.get("status")
                elif automatic:
                    # A healthy slow queue is never restarted. Only a completed
                    # removal proves a queued record was still present; a 404 or
                    # quiet stream can also describe an active record, so neither
                    # permits a duplicate automatic submission.
                    if cleanup.get("status") == "complete" and int(cleanup.get("removed") or 0) > 0:
                        sleeper(max(0.0, min(30.0, float(grace_seconds))))
                        runtime = detect_anythingllm_api_url(api_url, api_key=secret, timeout=1.25)
                        row["runtime_after_grace"] = runtime
                        if runtime.get("status") not in {"reachable", "reachable_auth_required"}:
                            row["restart"] = restart_anythingllm_desktop(api_url, secret)
                            row["action"] = "restart_confirmed_stalled_queue"
                            if row["restart"].get("status") != "ready":
                                row["status"] = str(row["restart"].get("status") or "unknown")
                                result["groups"].append(row)
                                continue
                        else:
                            row["restart"] = {"status": "not_needed_runtime_healthy"}
                        manifest_path = Path(group["ledger_path"]).with_name("resume-embedding-manifest.json")
                        manifest = _read_automatic_run_json(manifest_path)
                        if manifest:
                            row["resume"] = submit_embedding_resume_manifest(
                                manifest_path, manifest, api_url, secret, group["workspace_slug"],
                                automatic=True, expected_run_root=root,
                            )
                            row["action"] = "reconcile_missing_and_resume"
                            row["status"] = str(row["resume"].get("status") or "review_required")
                        else:
                            row["status"] = "resume_manifest_missing"
                    else:
                        row["status"] = "active_or_absent_records_not_resubmitted"
                else:
                    row["status"] = cleanup.get("status")
            result["groups"].append(row)
        if groups:
            result["status"] = "complete" if all(str(row.get("status")) in {"complete", "ready", "left_running"} for row in result["groups"]) else "review_required"
            if any(str((row.get("restart") or {}).get("status") or "") == "ready" for row in result["groups"]):
                result["message"] = "AnythingLLM restarted to clear this run's confirmed stalled queue. Other workspace content was not modified."
            else:
                result["message"] = "AnythingLLM recovery recorded. Check run history before relying on an interrupted upload."
    try:
        _write_automatic_run_json(root / AUTOMATIC_RUN_RECOVERY_STATE, result)
    except OSError:
        pass
    _append_automatic_recovery_history(result)
    return result


def schedule_automatic_recovery(run_root, *, reason="runtime_interrupted"):
    """Schedule one guarded recovery attempt for an interrupted run.

    The recovery policy still leaves Desktop untouched unless its bounded
    observer proves that the most recent interrupted run owns active queue
    work and no other activity is present. When that proof exists, it can
    reconcile missing records after one bounded Desktop recovery attempt.
    """
    root = Path(run_root)
    key = str(root)
    attempt_path = root / AUTOMATIC_RUN_RECOVERY_ATTEMPT
    if attempt_path.is_file():
        return False
    existing = ACTIVE_AUTOMATIC_RECOVERY_THREADS.get(key)
    if existing and existing.is_alive():
        return False
    try:
        _write_automatic_run_json(
            attempt_path,
            {"scheduled_at": datetime.now().isoformat(timespec="seconds"), "reason": str(reason or "runtime_interrupted")},
        )
    except OSError:
        return False

    def recover_in_background():
        try:
            recover_automatic_run(root, policy="automatic_recover", automatic=True)
        except Exception as exc:
            APP_LOGGER.warning("automatic recovery attempt failed: %s", exc)
        finally:
            ACTIVE_AUTOMATIC_RECOVERY_THREADS.pop(key, None)

    thread = threading.Thread(
        target=recover_in_background,
        name=f"anythingllm-recovery-{root.name}",
        daemon=True,
    )
    ACTIVE_AUTOMATIC_RECOVERY_THREADS[key] = thread
    thread.start()
    return True


def request_automatic_run_cancellation(run_root):
    """Persist a stop request and terminate the run's owned worker if present."""
    raw_root = str(run_root or "").strip()
    if not raw_root:
        return False
    root = Path(raw_root)
    try:
        root.mkdir(parents=True, exist_ok=True)
        _write_automatic_run_json(
            root / AUTOMATIC_RUN_CANCELLATION_MARKER,
            {"requested_at": datetime.now().isoformat()},
        )
    except OSError as exc:
        APP_LOGGER.warning("could not persist automatic run cancellation request: %s", exc)
        return False
    CANCELLED_AUTOMATIC_RUN_ROOTS.add(str(root))
    terminate_automatic_run_worker(root)
    APP_LOGGER.info("automatic run cancellation requested", extra={"run_root": str(root)})
    return True


def automatic_run_cancellation_requested(run_root):
    root = Path(str(run_root or ""))
    return (
        str(root) in CANCELLED_AUTOMATIC_RUN_ROOTS
        or (root / AUTOMATIC_RUN_CANCELLATION_MARKER).is_file()
    )


def _write_automatic_run_json(path, payload):
    """Atomically persist small control records used across Gradio workers."""
    target = Path(path)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)


def active_automatic_run_worker(run_root):
    """Return a validated owned child-worker record, never an arbitrary PID."""
    root = Path(str(run_root or ""))
    marker = root / AUTOMATIC_RUN_WORKER_MARKER
    try:
        record = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        pid = int(record.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    if (
        pid <= 0
        or record.get("kind") != "automatic-preparation-worker"
        or str(record.get("run_root") or "") != str(root)
    ):
        return None
    # This record is copied into durable cancellation evidence. Keep the
    # marker's filesystem value serializable at that boundary; callers only
    # need it for audit display, not Path methods.
    return {"pid": pid, "marker": str(marker), **record}


def terminate_automatic_run_worker(run_root):
    """Hard-stop the exact process tree created for this automatic PDF run.

    The PID is accepted only from a marker written in the current run folder
    and bound to that same folder.  Failure is harmless: the durable marker is
    still observed by checkpoint-aware stages when they next return.
    """
    worker = active_automatic_run_worker(run_root)
    if not worker:
        return False
    worker_key = str(Path(run_root))
    process = ACTIVE_AUTOMATIC_RUN_WORKER_PROCESSES.get(worker_key)
    if process is None or process.pid != worker["pid"] or process.poll() is not None:
        APP_LOGGER.warning(
            "refusing taskkill for an unowned automatic worker marker",
            extra={"run_root": worker_key, "pid": worker["pid"]},
        )
        return False
    try:
        completed = subprocess.run(
            ["taskkill", "/PID", str(worker["pid"]), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        stopped = completed.returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        APP_LOGGER.warning("could not terminate automatic preparation worker: %s", exc)
        stopped = False
    if stopped:
        APP_LOGGER.info(
            "automatic preparation worker terminated",
            extra={"run_root": str(run_root), "pid": worker["pid"]},
        )
    return stopped


def write_automatic_cancellation_recovery(run_root, pdf_path=None, worker_record=None):
    """Record the only safe postcondition after a forced child termination."""
    root = Path(run_root)
    live = dict(LIVE_AUTOMATIC_RUN_STATUS or {})
    try:
        confirmed_percent = round(
            min(1.0, max(0.0, float(live.get("confirmed_fraction") or 0.0))) * 100.0,
            1,
        )
    except (TypeError, ValueError):
        confirmed_percent = 0.0
    payload = {
        "schema_version": 1,
        "status": "cancelled",
        "cancelled_at": datetime.now().isoformat(),
        "source_pdf": str(pdf_path or ""),
        "worker": dict(worker_record or {}),
        "checkpoint": {
            "phase": str(live.get("phase") or ""),
            "confirmed_percent": confirmed_percent,
            "meaning": "Progress was frozen at this evidence-backed checkpoint when cancellation was requested.",
        },
        "local_result": "partial artifacts may remain and are intentionally not deleted automatically",
        "anythingllm_result": (
            "No further request will be submitted by this app run. An embedding request already accepted by "
            "AnythingLLM may still finish; its outcome is intentionally recorded as unknown rather than claimed stopped."
        ),
        "resume_guidance": "Inspect the workspace and this run folder before rerunning; a rerun may safely recreate prepared local artifacts.",
    }
    try:
        _write_automatic_run_json(root / AUTOMATIC_RUN_CANCELLATION_RECOVERY, payload)
        return str(root / AUTOMATIC_RUN_CANCELLATION_RECOVERY)
    except (OSError, TypeError, ValueError) as exc:
        APP_LOGGER.warning("could not write automatic cancellation recovery record: %s", exc)
        return ""


def automatic_run_cancelled_outputs(
    run_root,
    expected_seconds,
    started_at,
    readiness_html,
    download_full_folder=False,
    download_segments_folder=False,
    actual_seconds=None,
):
    """Return the stable terminal UI contract for a stop before any PDF ends."""
    recovery = write_automatic_cancellation_recovery(run_root)
    files = [recovery] if recovery and Path(recovery).is_file() else []
    live = dict(LIVE_AUTOMATIC_RUN_STATUS or {})
    try:
        checkpoint_percent = round(
            min(1.0, max(0.0, float(live.get("confirmed_fraction") or 0.0))) * 100.0
        )
    except (TypeError, ValueError):
        checkpoint_percent = 0
    message = (
        f"Cancelled at the {checkpoint_percent}% evidence checkpoint. Progress was frozen there; "
        "no later PDF or new AnythingLLM request was submitted. An already accepted Desktop request may still finish."
    )
    update_live_automatic_run_status(
        run_root,
        state="cancelled",
        phase="Processing stopped by operator",
        expected_seconds=expected_seconds,
        details=message,
        confirmed_fraction=None,
        cancel_available=False,
        cancel_requested=True,
        activity_observed=False,
    )
    elapsed_seconds = (
        max(0.0, float(actual_seconds))
        if actual_seconds is not None
        else max(0.0, time.perf_counter() - float(started_at or time.perf_counter()))
    )
    return (
        gr.update(value=run_summary_html("Status: cancelled\n" + message), visible=True),
        download_files_update(files, download_full_folder, download_segments_folder),
        artifact_display_html(files, "Cancellation recovery record"),
        files,
        automatic_completion_button_state({"state": "cancelled", "message": message}),
        readiness_html,
        automatic_run_timing_html(
            expected_seconds,
            "confirmation estimate",
            state="cancelled",
            actual_seconds=elapsed_seconds,
            message=message,
        ),
    )


AUTOMATIC_SUCCESS_WORKER_ARTIFACTS = (
    ".automatic-worker-config.json",
    ".automatic-worker-events.jsonl",
    ".automatic-worker-result.json",
)


def cleanup_automatic_success_worker_artifacts(output_dir, summary):
    """Remove Automatic-worker transport receipts after lean cleanup succeeds.

    These files are useful while a run is active, cancelled, or needs review.
    A lean-ready run already has its compact ``run-summary.json`` and no longer
    needs the process hand-off payload, progress stream, or raw worker result.
    """
    if not bool((dict(summary or {}).get("lean_retention") or {}).get("applied")):
        return []
    output = Path(output_dir)
    removed = []
    for name in AUTOMATIC_SUCCESS_WORKER_ARTIFACTS:
        candidate = output / name
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            continue
        removed.append(name)
    return removed


def new_automatic_runtime_guard():
    return {
        "desktop_required": False,
        "consecutive_failures": 0,
        "next_check_epoch": 0.0,
        "last_healthy_at": "",
        "checks": [],
    }


def append_automatic_runtime_event(run_root, event):
    """Append concise non-secret Desktop liveness evidence for one run."""
    record = dict(event or {})
    record.setdefault("recorded_at", datetime.now().isoformat(timespec="seconds"))
    path = Path(run_root) / AUTOMATIC_RUN_RUNTIME_EVENTS
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def attempt_automatic_runtime_start(run_root, api_url, api_key, *, stage="", status_callback=None):
    """Make one bounded *start-only* Desktop recovery attempt and save evidence.

    This does not restart a reachable Desktop process, remove queue entries, or
    resubmit records.  It is safe to call after the health guard has established
    that the configured local service is unavailable: if Desktop was closed,
    its normal launcher is invoked once; if it returns, the interrupted run is
    still left for reconciliation because the submission boundary is unknown.
    """
    root = Path(run_root)
    started = time.perf_counter()
    record = {
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "stage": str(stage or ""),
        "requested_url": str(api_url or ""),
        "action": "start_only",
        "status": "not_attempted",
        "error": "",
    }
    process_running = anythingllm_desktop_process_running()
    record["desktop_process_running"] = process_running
    # A live process with a dead API can be a hung Desktop instance, but it can
    # also be in its ordinary startup window or own manual queue work. Never
    # force-kill it here. ``ensure_anythingllm_runtime(..., autostart_local=True)``
    # is still safe: its launcher merely returns ``already_running`` and then
    # polls the local API. Previously this branch failed immediately, so a
    # Desktop instance that became ready seconds later was reported as a failed
    # automatic startup.
    record["action"] = "wait_for_running_process_api" if process_running else "start_only"
    try:
        runtime = ensure_anythingllm_runtime(
            preferred_url=str(api_url or ""),
            api_key=api_key or None,
            # The guard has already confirmed two failed local probes. Keep the
            # initial sweep short; the bounded startup loop below performs the
            # patient, observable readiness wait.
            timeout=0.4,
            startup_timeout=AUTOMATIC_RUNTIME_RECOVERY_STARTUP_TIMEOUT_SECONDS,
            autostart_local=True,
            status_callback=status_callback,
            startup_poll_interval=AUTOMATIC_RUNTIME_RECOVERY_STARTUP_POLL_INTERVAL_SECONDS,
            startup_fast_poll_interval=AUTOMATIC_RUNTIME_RECOVERY_STARTUP_FAST_POLL_INTERVAL_SECONDS,
            startup_fast_poll_window=AUTOMATIC_RUNTIME_RECOVERY_STARTUP_FAST_POLL_WINDOW_SECONDS,
        )
        record["runtime"] = runtime
        status = str(runtime.get("status") or "unknown")
        if status in {"reachable", "reachable_auth_required"}:
            record["status"] = "ready"
        elif process_running:
            record["status"] = "running_process_api_timeout"
            record["error"] = (
                "AnythingLLM Desktop remained running, but its local API did not respond "
                "before the recovery deadline. Automatic force-restart was withheld."
            )
        else:
            record["status"] = status
    except Exception as exc:
        record["status"] = "failed"
        record["error"] = str(exc)
    record["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    try:
        _write_automatic_run_json(root / AUTOMATIC_RUN_RUNTIME_RECOVERY, record)
    except OSError:
        pass
    return record


def poll_automatic_runtime_guard(guard, desktop_required, stage, api_url, api_key, *, now=None, probe=detect_anythingllm_api_url):
    """Perform bounded passive health probes for an upload-mode run.

    A broken SSE connection is diagnostic only. Recovery is authorized only
    after two real API-health failures, and callers own the one-per-run
    recovery attempt plus durable reconciliation.
    """
    state = dict(guard or new_automatic_runtime_guard())
    state["desktop_required"] = bool(state.get("desktop_required")) or bool(desktop_required)
    if not state["desktop_required"] or not str(api_url or "").strip():
        return state, {"status": "not_required"}
    moment = time.time() if now is None else float(now)
    if moment < float(state.get("next_check_epoch") or 0.0):
        return state, {"status": "not_due"}
    try:
        # This is a frequent liveness signal, not an embedding request. A
        # short bounded probe preserves fast recovery without delaying parsing.
        health = probe(str(api_url), api_key=api_key or None, timeout=0.4)
    except Exception as exc:  # A monitor must never bring down a live run.
        health = {"status": "probe_error", "error": str(exc)}
    healthy = str(health.get("status") or "") in {"reachable", "reachable_auth_required"}
    state["consecutive_failures"] = 0 if healthy else int(state.get("consecutive_failures") or 0) + 1
    state["next_check_epoch"] = moment + (
        AUTOMATIC_RUNTIME_GUARD_INTERVAL_SECONDS
        if healthy
        else AUTOMATIC_RUNTIME_GUARD_RECHECK_SECONDS
    )
    if healthy:
        state["last_healthy_at"] = datetime.now().isoformat(timespec="seconds")
    observation = {
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "stage": str(stage or ""),
        "desktop_required": True,
        "health_status": str(health.get("status") or "unknown"),
        "consecutive_failures": state["consecutive_failures"],
    }
    state["checks"] = (list(state.get("checks") or []) + [observation])[-12:]
    return state, {
        "status": "unavailable" if state["consecutive_failures"] >= AUTOMATIC_RUNTIME_GUARD_FAILURE_THRESHOLD else "healthy" if healthy else "transient_failure",
        "health": health,
    }


def submission_runtime_recovery_needed(summary, api_url, api_key, *, probe=detect_anythingllm_api_url):
    """Recognize a Desktop loss at a narrow embedding boundary.

    The regular worker guard intentionally needs two low-frequency failed
    probes.  That protects a long upload from one transient request failure,
    but a PDF can finish local preparation between those probes.  In that
    short final window, a failed temporary-key request is ambiguous until a
    fresh health probe says whether Desktop is actually gone.  Only then may
    the caller make its one bounded *start-only* recovery attempt.
    """
    result = {
        "needed": False,
        "reason": "not_submission_runtime_loss",
        "health": {},
    }
    source = dict(summary or {})
    status = str(source.get("api_upload_status") or source.get("status") or "").casefold()
    runtime_loss_statuses = {
        "error_authentication_required",
        "authentication_required",
        "network_error",
    }
    if status not in runtime_loss_statuses or not is_local_anythingllm_url(api_url):
        return result
    try:
        health = probe(str(api_url), api_key=api_key or None, timeout=1.25)
    except Exception as exc:
        health = {"status": "probe_error", "error": str(exc)}
    result["health"] = dict(health or {})
    if str(result["health"].get("status") or "") in {"reachable", "reachable_auth_required"}:
        result["reason"] = "runtime_still_reachable"
        return result
    result.update(needed=True, reason="local_runtime_unavailable_after_submission_auth_failure")
    return result


def can_resume_local_preparation_after_runtime_start(output_dir, runtime_recovery):
    """Allow one automatic resume only before an AnythingLLM submission exists."""
    ledger_path = Path(output_dir) / "inspection" / "embedding-batch-ledger.json"
    return bool(
        isinstance(runtime_recovery, dict)
        and runtime_recovery.get("status") == "ready"
        and not ledger_path.is_file()
    )


def resume_owned_embedding_manifest_after_runtime_start(
    run_root,
    output_dir,
    api_url,
    api_key,
    *,
    status_callback=None,
):
    """Resume one current run's ledger-proven missing locations after Desktop returns.

    This deliberately does not rerun PDF preparation and never uses a broad
    workspace scan.  The manifest first removes locations with exact late
    vector evidence, then resubmits only the remainder using an existing
    Desktop key.  The most-recent-run restriction prevents an old manifest
    from reviving beside a newer operator run.
    """
    root = Path(run_root)
    manifest_path = Path(output_dir) / "inspection" / "resume-embedding-manifest.json"
    manifest = _read_automatic_run_json(manifest_path)
    recovery = manifest.get("recovery") if isinstance(manifest, dict) else {}
    if not manifest_path.is_file() or not isinstance(recovery, dict):
        return {"status": "manifest_unavailable", "message": "No durable embedding recovery manifest was found."}
    if str(recovery.get("state") or "") != "resume_available":
        return {"status": "manifest_not_resumable", "message": "The embedding recovery manifest is not resumable."}
    if not _is_most_recent_recovery_run(root):
        return {"status": "blocked_not_most_recent", "message": "Only the most recent interrupted run may resume automatically."}
    if callable(status_callback):
        try:
            status_callback("Reconciling exact vectors before resuming this run's missing embedding records")
        except Exception:
            pass
    result = submit_embedding_resume_manifest(
        manifest_path,
        manifest,
        api_url,
        api_key,
        str(manifest.get("workspace_slug") or ""),
        automatic=True,
        expected_run_root=root,
        status_callback=(
            lambda stage, _report: status_callback(
                f"AnythingLLM recovered; resuming only this run's missing records — {stage}"
            )
            if callable(status_callback)
            else None
        ),
    )
    result["manifest_path"] = str(manifest_path)
    return result


def execute_automatic_preparation_in_worker(
    pdf_path,
    out_dir,
    args,
    run_root,
    progress_callback,
    timing_event_callback=None,
    *,
    allow_runtime_restart_resume=True,
):
    """Run one document outside Gradio so the Cancel button can stop it now.

    Progress is relayed through an append-only file rather than a pipe.  That
    avoids a full stdout pipe freezing an OCR worker and leaves useful evidence
    if it is forcibly stopped.  The worker result is authoritative only when
    it has atomically written its terminal JSON record.
    """
    if execute_preparation is not CANONICAL_EXECUTE_PREPARATION or prepare_pdf is not CANONICAL_PREPARE_PDF:
        controlled = execute_preparation(pdf_path, out_dir, args, prepare_pdf)
        return {
            "status": "completed" if controlled.status == "pass" else "failed",
            "error": "" if controlled.status == "pass" else controlled.operator_summary,
            "summary": legacy_summary_from_run(controlled),
            "run_control": controlled.to_dict(),
        }
    root = Path(run_root)
    output = Path(out_dir)
    output.mkdir(parents=True, exist_ok=True)
    config_path = output / ".automatic-worker-config.json"
    result_path = output / ".automatic-worker-result.json"
    events_path = output / ".automatic-worker-events.jsonl"
    worker_script = Path(__file__).with_name("cancellable_preparation_worker.py")
    argument_values = {
        key: value
        for key, value in vars(args).items()
        if key not in {"progress_callback", "cancel_callback", "timing_event_callback"}
    }
    # Keep the worker contract JSON-only.  All Automatic-run argument values
    # are scalar/list/dict configuration; stringifying an unexpected observer
    # object is safer than making cancellation unavailable for the whole run.
    serializable_arguments = json.loads(json.dumps(argument_values, default=str))
    _write_automatic_run_json(
        config_path,
        {
            "pdf_path": str(pdf_path),
            "output_dir": str(output),
            "run_root": str(root),
            "result_path": str(result_path),
            "events_path": str(events_path),
            "args": serializable_arguments,
        },
    )
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process = subprocess.Popen(
        [sys.executable, str(worker_script), str(config_path)],
        cwd=str(Path(__file__).resolve().parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    worker_marker = root / AUTOMATIC_RUN_WORKER_MARKER
    _write_automatic_run_json(
        worker_marker,
        {
            "kind": "automatic-preparation-worker",
            "pid": process.pid,
            "run_root": str(root),
            "pdf_path": str(pdf_path),
            "started_at": datetime.now().isoformat(),
        },
    )
    worker_key = str(root)
    ACTIVE_AUTOMATIC_RUN_WORKER_PROCESSES[worker_key] = process
    event_offset = 0
    latest_progress_value = 0.0
    latest_stage = ""
    # Observe upload-mode runs continuously, but keep the event-specific flag
    # separate. A Desktop loss during local parsing can be repaired in the
    # background while extraction continues; only an actual Desktop operation
    # is allowed to stop the worker.
    desktop_required = bool(getattr(args, "prepare_and_upload", False))
    stage_requires_desktop = False
    background_runtime_recovery_attempted = False
    runtime_guard = new_automatic_runtime_guard()
    worker_record = active_automatic_run_worker(root)
    try:
        while process.poll() is None:
            if automatic_run_cancellation_requested(root):
                terminate_automatic_run_worker(root)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
                recovery = write_automatic_cancellation_recovery(root, pdf_path, worker_record)
                return {"status": "cancelled", "recovery": recovery}
            try:
                with events_path.open("r", encoding="utf-8") as handle:
                    handle.seek(event_offset)
                    for line in handle:
                        try:
                            event = json.loads(line)
                            if event.get("type") == "timing":
                                latest_stage = event.get("stage", latest_stage)
                                if callable(timing_event_callback):
                                    timing_event_callback(event.get("stage", ""), event.get("batch_report") or {})
                            else:
                                latest_progress_value = event.get("value", 0.0)
                                latest_stage = event.get("stage", "Working")
                                stage_requires_desktop = bool(event.get("desktop_required"))
                                progress_callback(latest_progress_value, latest_stage, progress_event=event)
                        except (TypeError, ValueError, json.JSONDecodeError):
                            continue
                    event_offset = handle.tell()
            except OSError:
                pass
            runtime_guard, runtime_probe = poll_automatic_runtime_guard(
                runtime_guard,
                desktop_required,
                latest_stage,
                getattr(args, "anythingllm_api_url", ""),
                getattr(args, "anythingllm_api_key", ""),
            )
            if runtime_probe.get("status") not in {"not_required", "not_due"}:
                try:
                    append_automatic_runtime_event(
                        root,
                        {
                            "phase": latest_stage,
                            "desktop_required": bool(runtime_guard.get("desktop_required")),
                            "status": runtime_probe.get("status"),
                            "health_status": (runtime_probe.get("health") or {}).get("status", ""),
                            "consecutive_failures": runtime_guard.get("consecutive_failures", 0),
                        },
                    )
                except OSError:
                    pass
            if runtime_probe.get("status") == "unavailable":
                _write_automatic_run_json(root / AUTOMATIC_RUN_RUNTIME_GUARD, runtime_guard)
                if not stage_requires_desktop:
                    # PDF extraction is local work. Restart Desktop in the
                    # parent while the child keeps parsing, rather than
                    # killing the worker and making the operator wait for the
                    # same PDF to be extracted a second time.
                    if not background_runtime_recovery_attempted:
                        background_runtime_recovery_attempted = True
                        progress_callback(
                            latest_progress_value,
                            "AnythingLLM is unavailable; restarting Desktop while local PDF preparation continues",
                        )

                        def report_background_runtime_recovery(lifecycle_phase, _runtime):
                            phase = str(lifecycle_phase or "")
                            if phase == "starting_desktop":
                                message = "Starting AnythingLLM Desktop while local PDF preparation continues"
                            elif phase == "waiting_for_runtime":
                                probe_count = int((_runtime or {}).get("startup_probe_count") or 0)
                                message = (
                                    "Waiting for AnythingLLM Desktop API while local PDF preparation continues "
                                    f"(check {probe_count})"
                                )
                            elif phase in {"ready_after_start", "ready"}:
                                message = "AnythingLLM is ready; local PDF preparation continued without restarting"
                            elif phase == "start_failed":
                                message = "AnythingLLM could not be started, but local PDF preparation is continuing"
                            else:
                                return
                            progress_callback(latest_progress_value, message)

                        runtime_recovery = attempt_automatic_runtime_start(
                            root,
                            getattr(args, "anythingllm_api_url", ""),
                            getattr(args, "anythingllm_api_key", ""),
                            stage=latest_stage,
                            status_callback=report_background_runtime_recovery,
                        )
                        try:
                            append_automatic_runtime_event(
                                root,
                                {
                                    "phase": latest_stage,
                                    "background_local_preparation": True,
                                    **runtime_recovery,
                                },
                            )
                        except OSError:
                            pass
                        # The recovery's final API result supersedes the two
                        # stale failures that triggered it. The next passive
                        # check starts from a clean evidence window.
                        runtime_guard = new_automatic_runtime_guard()
                    time.sleep(0.12)
                    continue
                progress_callback(
                    latest_progress_value,
                    "AnythingLLM runtime unavailable; stopping this worker and starting Desktop once",
                )
                terminate_automatic_run_worker(root)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
                if not allow_runtime_restart_resume:
                    return {
                        "status": "runtime_unavailable",
                        "error": (
                            "AnythingLLM became unavailable again after this run's one automatic "
                            "Desktop recovery attempt. The interrupted work was left for safe reconciliation."
                        ),
                        "runtime_guard": runtime_guard,
                    }

                def report_runtime_recovery(lifecycle_phase, _runtime):
                    phase = str(lifecycle_phase or "")
                    if phase == "starting_desktop":
                        message = "Checking AnythingLLM Desktop runtime"
                    elif phase == "waiting_for_runtime":
                        probe_count = int((_runtime or {}).get("startup_probe_count") or 0)
                        already_running = str(((_runtime or {}).get("start") or {}).get("status") or "") == "already_running"
                        message = (
                            "AnythingLLM is already open; waiting for its local API "
                            if already_running
                            else "Waiting for AnythingLLM Desktop API "
                        ) + (
                            f"(check {probe_count}; checking about once a second for the first 45 seconds, "
                            "then every 10 seconds for up to 3 minutes)"
                        )
                    elif phase in {"ready_after_start", "ready"}:
                        message = "AnythingLLM is ready; resuming local preparation"
                    elif phase == "start_failed":
                        message = "AnythingLLM Desktop could not be started automatically"
                    else:
                        return
                    progress_callback(latest_progress_value, message)

                runtime_recovery = attempt_automatic_runtime_start(
                    root,
                    getattr(args, "anythingllm_api_url", ""),
                    getattr(args, "anythingllm_api_key", ""),
                    stage=latest_stage,
                    status_callback=report_runtime_recovery,
                )
                try:
                    append_automatic_runtime_event(root, {"phase": latest_stage, **runtime_recovery})
                except OSError:
                    pass
                if can_resume_local_preparation_after_runtime_start(output, runtime_recovery):
                    # No owned submission ledger exists, so Desktop stopped
                    # during local-only preparation. Starting the worker once
                    # more is safe: no AnythingLLM queue record could have
                    # been created, and the resumed worker keeps the same run
                    # folder and cancellation authority.
                    progress_callback(
                        latest_progress_value,
                        "AnythingLLM restarted; resuming local preparation",
                    )
                    return execute_automatic_preparation_in_worker(
                        pdf_path,
                        out_dir,
                        args,
                        root,
                        progress_callback,
                        timing_event_callback,
                        allow_runtime_restart_resume=False,
                    )
                return {
                    "status": "runtime_unavailable",
                    "error": (
                        "AnythingLLM stopped during a Desktop-dependent stage. "
                        + (
                            "Desktop was started again; this interrupted run was left for safe reconciliation."
                            if runtime_recovery.get("status") == "ready"
                            else (
                                "Desktop process is still present but its API is unavailable; automatic force-restart was withheld because manual activity cannot be ruled out."
                                if runtime_recovery.get("status") == "restart_withheld_manual_activity_uncertain"
                                else "Desktop could not be started automatically; inspect the saved recovery evidence."
                            )
                        )
                    ),
                    "runtime_guard": runtime_guard,
                    "runtime_recovery": runtime_recovery,
                }
            time.sleep(0.12)
        # Drain the final progress callback before resolving the result.
        try:
            with events_path.open("r", encoding="utf-8") as handle:
                handle.seek(event_offset)
                for line in handle:
                    event = json.loads(line)
                    if event.get("type") == "timing":
                        if callable(timing_event_callback):
                            timing_event_callback(event.get("stage", ""), event.get("batch_report") or {})
                    else:
                        progress_callback(
                            event.get("value", 0.0),
                            event.get("stage", "Working"),
                            progress_event=event,
                        )
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        if automatic_run_cancellation_requested(root):
            recovery = write_automatic_cancellation_recovery(root, pdf_path, worker_record)
            return {"status": "cancelled", "recovery": recovery}
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {
                "status": "failed",
                "error": f"The preparation worker ended without a readable result: {exc}",
            }
        if result.get("status") != "completed":
            return {"status": result.get("status") or "failed", **result}
        if result.get("run_status") != "pass":
            return {
                "status": "failed",
                "error": result.get("operator_summary") or "The preparation worker reported a non-passing run.",
            }
        result["lean_worker_artifacts_removed"] = cleanup_automatic_success_worker_artifacts(
            output,
            result.get("summary") or {},
        )
        return {"status": "completed", **result}
    finally:
        # A stale marker must never authorize a later Cancel click against a
        # reused Windows PID.  The cancellation recovery record remains.
        try:
            worker_marker.unlink(missing_ok=True)
        except OSError:
            pass
        if ACTIVE_AUTOMATIC_RUN_WORKER_PROCESSES.get(worker_key) is process:
            ACTIVE_AUTOMATIC_RUN_WORKER_PROCESSES.pop(worker_key, None)


def cancellation_safe_display_progress(run_root, proposed_fraction):
    """Freeze progress while an owned worker is being force-stopped."""
    try:
        proposed = min(1.0, max(0.0, float(proposed_fraction)))
    except (TypeError, ValueError):
        proposed = 0.0
    if not automatic_run_cancellation_requested(run_root):
        return proposed, False
    live = dict(LIVE_AUTOMATIC_RUN_STATUS or {})
    if str(live.get("run_root") or "") != str(run_root or ""):
        return proposed, True
    try:
        confirmed = min(1.0, max(0.0, float(live.get("confirmed_fraction") or 0.0)))
    except (TypeError, ValueError):
        confirmed = 0.0
    return min(proposed, confirmed), True


def automatic_run_cancel_is_safe(stage):
    """The worker isolation contract makes every active stage cancellable."""
    return True


def cancel_or_reset_automatic_run(
    pdf_files=None,
    folder_pdf_files=None,
    folder_manifest=None,
    run_activity_html="",
):
    """Reset safely before Confirm, or request a cooperative in-flight stop."""
    record = dict(LIVE_AUTOMATIC_RUN_STATUS or {})
    # A historical ``run-progress.json`` can legitimately retain ``running``
    # after an interrupted localhost process. It is recovery evidence, not an
    # active request owned by this screen. The current screen must show a
    # running activity record (or this process must own one) before the
    # destructive cooperative-stop path is authorized.
    visible_running = 'data-run-state="running"' in str(run_activity_html or "")
    run_root_path = (
        active_automatic_run_root()
        if str(record.get("state") or "") in {"running", "preparing"} or visible_running
        else None
    )
    if run_root_path:
        run_root = str(run_root_path)
        if not request_automatic_run_cancellation(run_root):
            return (
                gr.update(value=""),
                gr.update(),
                automatic_run_timing_html(state="failed", message="Could not persist the stop request; close the localhost app to prevent later uploads."),
                gr.update(value="Stop request failed", interactive=False, variant="stop"),
                gr.update(),
                gr.update(interactive=False),
                gr.update(),
                gr.update(value="", visible=False),
                gr.update(visible=False, interactive=False),
            )
        progress_record = record
        if str(progress_record.get("run_root") or "") != run_root:
            try:
                progress_record = json.loads((run_root_path / "run-progress.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                progress_record = record
        update_live_automatic_run_status(
            run_root,
            state="running",
            phase="Cancellation requested — stopping at the current safe checkpoint",
            expected_seconds=progress_record.get("expected_seconds", 0),
            details=(
                "Progress is frozen at its last confirmed checkpoint. The active PDF worker is stopping; "
                "no later PDF or new AnythingLLM request will be submitted. An already accepted request may still finish."
            ),
            confirmed_fraction=progress_record.get("confirmed_fraction"),
            cancel_available=False,
            cancel_requested=True,
        )
        return (
            gr.update(value=""),
            gr.update(),
            gr.update(value=automatic_run_timing_html(state="cancelled")),
            gr.update(value="Stopping processing…", interactive=False, variant="secondary"),
            gr.update(),
            gr.update(interactive=False),
            gr.update(),
            gr.update(value="", visible=False),
            gr.update(visible=False, interactive=False),
        )
    return (
        gr.update(value=""),
        {},
        automatic_run_timing_html(state="ready"),
        automatic_process_button_state(pdf_files, folder_pdf_files, folder_manifest),
        gr.update(),
        gr.update(visible=False, interactive=False),
        gr.update(value="Cancel", interactive=False),
        gr.update(value="", visible=False),
        gr.update(visible=False, interactive=False),
    )


def automatic_run_timing_html(
    expected_seconds=0,
    source="",
    state="ready",
    actual_seconds=None,
    message="",
    started_epoch=None,
    now=None,
    eta_acceleration_seconds=0,
    server_driven=False,
    estimate_range="",
    confidence_label="",
    comparable_runs=None,
):
    expected = max(0, int(round(float(expected_seconds or 0))))
    actual = max(0, int(round(float(actual_seconds or 0)))) if actual_seconds is not None else None
    current_time = time.time() if now is None else float(now)
    started = float(started_epoch or 0)
    server_timer_value = "true" if server_driven else "false"
    if state == "running":
        elapsed = max(0, int(current_time - started)) if started else 0
        acceleration = max(0, int(round(float(eta_acceleration_seconds or 0))))
        # A remaining-time estimate may reach zero, but it is never a signed
        # "negative ETA". Completion duration is shown separately at the
        # terminal state, where it is factual rather than a forecast.
        remaining = max(0, expected - elapsed - acceleration)
        label = f"Est: {format_estimate_clock(remaining)}"
    elif state == "cancelled" and actual is not None:
        label = f"Stopped: {format_estimate_clock(actual)}"
    elif actual is not None and state in {"successful", "warning", "failed"}:
        label = f"Compl: {format_estimate_clock(actual)}"
    else:
        label = f"Est: {format_estimate_clock(expected)}"
    evidence_parts = []
    # Low-confidence intervals add visual precision without dependable
    # information, so omit the entire evidence line. Medium/high confidence
    # shows only the range; confidence labels and opaque comparable-run counts
    # belong in the diagnostics panel rather than the primary timer.
    low_confidence = str(confidence_label or "").strip().casefold() == "low confidence"
    if estimate_range and state in {"ready", "running"} and not low_confidence:
        evidence_parts.append(f"Range {estimate_range}")
    evidence_html = (
        f'<div class="automatic-run-timing-evidence">{html.escape(" · ".join(evidence_parts))}</div>'
        if evidence_parts else ""
    )
    return (
        f'<div id="automatic-run-timing" class="automatic-run-timing {html.escape(state)}" '
        f'data-expected-seconds="{expected}" data-actual-seconds="{actual if actual is not None else ""}" '
        f'data-started-epoch="{started:.3f}" '
        f'data-server-timer="{server_timer_value}" '
        f'data-run-state="{html.escape(state)}">'
        f'<strong>{html.escape(label)}</strong>'
        f'{evidence_html}'
        '</div>'
    )


def _append_timing_jsonl(path, record):
    """Append a small, inspectable timing record without PDF text or API keys."""
    try:
        TIMING_MODEL_DIR.mkdir(parents=True, exist_ok=True)
        prune_background_jsonl(path)
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError as exc:
        APP_LOGGER.warning("could not persist timing-model record: %s", exc)


def _read_timing_jsonl(path, limit=240):
    if not Path(path).exists():
        return []
    try:
        rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
        return rows[-max(1, int(limit)):]
    except (OSError, json.JSONDecodeError) as exc:
        APP_LOGGER.warning("could not read timing-model history: %s", exc)
        return []


def _bucket(value, low, high, *, low_label="low", high_label="high", middle_label="medium"):
    if value <= low:
        return low_label
    if value >= high:
        return high_label
    return middle_label


def automatic_timing_document_profile(files, *, document_limit=None, page_sample_limit=24):
    """Build a bounded local-only difficulty profile for ETA calibration.

    At most ``page_sample_limit`` evenly-spaced pages per input are sampled.
    The normal ETA path uses 24; the confirmation OCR preflight deliberately
    uses a much smaller sample and never runs OCR. The profile contains
    counts and density ratios only; no extracted PDF text is retained in the
    central timing history.
    """
    raw_paths = [Path(raw_path) for raw_path in files or [] if raw_path]
    if document_limit is not None and len(raw_paths) > max(1, int(document_limit)):
        limit = max(1, int(document_limit))
        # Evenly sample the ordered batch rather than overfitting the ETA to
        # whichever filenames happen to sort first.
        sample_indexes = sorted({round(index * (len(raw_paths) - 1) / max(1, limit - 1)) for index in range(limit)})
        profiled_paths = [raw_paths[index] for index in sample_indexes]
    else:
        profiled_paths = raw_paths
    profile = {
        "documents": len(raw_paths),
        "profiled_documents": len(profiled_paths),
        "profile_document_limit": document_limit,
        "page_count": 0,
        "file_bytes": 0,
        "sampled_pages": 0,
        "sampled_text_chars": 0,
        "sampled_images": 0,
        "sampled_drawings": 0,
        "tableish_lines": 0,
        "sampled_lines": 0,
        "sampled_words": 0,
        "sparse_pages": 0,
        "short_pages": 0,
        "long_pages": 0,
    }
    sampled_char_counts = []
    sampled_line_counts = []
    for path in raw_paths:
        try:
            profile["file_bytes"] += path.stat().st_size
        except OSError:
            pass
    for path in profiled_paths:
        try:
            document = fitz.open(path)
            pages = int(document.page_count or 0)
            profile["page_count"] += pages
            sample_count = min(max(1, int(page_sample_limit or 1)), pages)
            indexes = sorted({round(index * (pages - 1) / max(1, sample_count - 1)) for index in range(sample_count)})
            for index in indexes:
                page = document.load_page(index)
                text = page.get_text("text") or ""
                chars = len(text.strip())
                lines = [line for line in text.splitlines() if line.strip()]
                profile["sampled_pages"] += 1
                profile["sampled_text_chars"] += chars
                profile["sampled_lines"] += len(lines)
                profile["sampled_words"] += len(re.findall(r"\S+", text))
                sampled_char_counts.append(chars)
                sampled_line_counts.append(len(lines))
                profile["sampled_images"] += len(page.get_images(full=True))
                profile["sampled_drawings"] += len(page.get_drawings())
                profile["tableish_lines"] += sum(
                    1 for line in text.splitlines()
                    if "|" in line or len(re.findall(r"\S\s{3,}\S", line)) >= 1
                )
                profile["sparse_pages"] += int(chars < 180)
                profile["short_pages"] += int(chars < 600)
                profile["long_pages"] += int(chars > 3000)
            document.close()
        except Exception as exc:
            APP_LOGGER.info("timing profile skipped unreadable input %s: %s", path, exc)
    sampling_ratio = len(raw_paths) / max(1, len(profiled_paths))
    if sampling_ratio > 1:
        # Preserve density ratios while scaling page- and batch-driven timing
        # features to the full selected batch. This is only used for the
        # immediate pre-run estimate; actual processing still validates every
        # input before it begins.
        for key in (
            "page_count", "sampled_pages", "sampled_text_chars", "sampled_images",
            "sampled_drawings", "tableish_lines", "sampled_lines", "sampled_words",
            "sparse_pages", "short_pages", "long_pages",
        ):
            profile[key] = int(round(profile[key] * sampling_ratio))
    profile["profile_sampling_ratio"] = round(sampling_ratio, 3)
    sampled = max(1, profile["sampled_pages"])
    profile["mean_chars_per_page"] = round(profile["sampled_text_chars"] / sampled, 1)
    profile["mean_lines_per_page"] = round(profile["sampled_lines"] / sampled, 1)
    profile["mean_words_per_page"] = round(profile["sampled_words"] / sampled, 1)
    profile["median_chars_per_page"] = round(statistics.median(sampled_char_counts), 1) if sampled_char_counts else 0.0
    profile["p90_chars_per_page"] = round(_timing_percentile(sampled_char_counts, .90) or 0.0, 1)
    profile["page_text_variability"] = round(
        statistics.pstdev(sampled_char_counts) / max(1.0, profile["mean_chars_per_page"]), 3
    ) if len(sampled_char_counts) > 1 else 0.0
    profile["bytes_per_page"] = round(profile["file_bytes"] / max(1, profile["page_count"]), 1)
    profile["image_density"] = round(profile["sampled_images"] / sampled, 3)
    profile["drawing_density"] = round(profile["sampled_drawings"] / sampled, 3)
    profile["tableish_density"] = round(profile["tableish_lines"] / sampled, 3)
    profile["sparse_fraction"] = round(profile["sparse_pages"] / sampled, 3)
    profile["short_fraction"] = round(profile["short_pages"] / sampled, 3)
    profile["long_fraction"] = round(profile["long_pages"] / sampled, 3)
    profile["text_density_bucket"] = _bucket(profile["mean_chars_per_page"], 650, 2400)
    profile["layout_bucket"] = (
        "image_or_table_heavy"
        if profile["image_density"] >= .5 or profile["tableish_density"] >= 3.0 or profile["drawing_density"] >= 15
        else "text_first"
    )
    profile["ocr_risk_bucket"] = (
        "high" if profile["sparse_fraction"] >= .45 and profile["image_density"] >= .25
        else "possible" if profile["sparse_fraction"] >= .25
        else "low"
    )
    profile["line_density_bucket"] = _bucket(profile["mean_lines_per_page"], 18, 52)
    profile["page_variability_bucket"] = _bucket(
        profile["page_text_variability"], .35, .85,
        low_label="consistent", middle_label="mixed", high_label="variable",
    )
    profile["file_size_bucket"] = _bucket(
        profile["bytes_per_page"], 80_000, 800_000,
        low_label="light", middle_label="medium", high_label="heavy",
    )
    return profile


def automatic_ocr_preflight_risk(profile):
    """Classify a cheap native-text sample without claiming OCR has run."""
    sparse_fraction = float(profile.get("sparse_fraction") or 0.0)
    chars_per_page = float(profile.get("mean_chars_per_page") or 0.0)
    image_density = float(profile.get("image_density") or 0.0)
    risk_bucket = str(profile.get("ocr_risk_bucket") or "low")
    # A likely scan must be almost text-empty *and* visibly image-backed.
    # This keeps ordinary sparse pages, title pages, and academic references
    # out of the strong warning lane.
    if sparse_fraction >= .90 and chars_per_page <= 20.0 and image_density >= .50:
        return "likely"
    if risk_bucket in {"possible", "high"}:
        return "possible"
    return "native_text_likely"


def automatic_full_native_text_coverage(path):
    """Verify native text coverage on every physical page before OCR is deferred.

    The normal ETA sampler intentionally opens only representative pages.  It
    is not sufficient evidence to skip OCR-risk work: one scanned appendix or
    photographed insert can occur outside that sample.  This bounded pass
    reads only native text and image counts; it does not OCR or retain page
    content.  Any low-text page forces the subsequent OCR capability check.
    """
    pdf_path = Path(path)
    result = {
        "status": "verified",
        "page_count": 0,
        "low_text_pages": [],
        "image_backed_low_text_pages": [],
    }
    try:
        with fitz.open(pdf_path) as document:
            result["page_count"] = int(document.page_count or 0)
            for index in range(result["page_count"]):
                page = document.load_page(index)
                text_characters = len((page.get_text("text") or "").strip())
                image_count = len(page.get_images(full=True))
                if text_characters < 160:
                    row = {
                        "page": index + 1,
                        "native_text_characters": text_characters,
                        "image_count": image_count,
                    }
                    result["low_text_pages"].append(row)
                    if image_count:
                        result["image_backed_low_text_pages"].append(row)
    except Exception as exc:
        result.update({
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
        })
    return result


def automatic_ocr_preflight_manifest(files, *, backend_mode="Automatic", unstructured_strategy="auto"):
    """Build one run-scoped OCR-risk manifest before confirmation.

    It samples three representative pages for timing, then checks native text
    and image counts on every physical page before calling a file text-first.
    When the picker already performed that exact native scan, it reuses the
    cached result rather than reading those pages a second time.
    It does not invoke Tesseract, Unstructured extraction, or retain document
    text during coverage inspection. A single shared Unstructured/Tesseract
    capability probe is performed for document-wide scan evidence or an
    explicit OCR selection. Sparse image/table pages remain review candidates
    but do not trigger a whole-document OCR import.
    """
    rows = []
    for raw_path in files or []:
        path = Path(raw_path)
        profile = automatic_timing_document_profile([path], page_sample_limit=3)
        risk = automatic_ocr_preflight_risk(profile)
        try:
            coverage = pdf_picker_native_inspection(path)["coverage"]
        except Exception:
            coverage = automatic_full_native_text_coverage(path)
        low_text_pages = list(coverage.get("low_text_pages") or [])
        image_backed_low_text_pages = list(coverage.get("image_backed_low_text_pages") or [])
        # A representative sample can look healthy while an un-sampled page
        # is scanned. Do not call that file native-text clear until every page
        # has passed this lightweight coverage gate.
        if coverage.get("status") != "verified":
            # A failed coverage check cannot prove a document-wide scan. Keep
            # the original sample's strong signal, otherwise require review.
            risk = "likely" if risk == "likely" else "possible"
        elif low_text_pages:
            page_count = max(1, int(coverage.get("page_count") or profile.get("page_count") or 0))
            image_backed_fraction = len(image_backed_low_text_pages) / page_count
            low_text_fraction = len(low_text_pages) / page_count
            # Whole-document OCR is justified only when the native layer is
            # weak across most of the PDF. A few photographs, tables, title
            # leaves, or blank versos must not turn a text-native book into a
            # hi_res OCR job.
            document_wide_scan = (
                image_backed_fraction >= 0.50
                or (low_text_fraction >= 0.90 and image_backed_fraction >= 0.50)
            )
            risk = "likely" if document_wide_scan else "possible"
        rows.append({
            "file": str(path),
            "name": path.name,
            "pages": max(0, int(profile.get("page_count") or 0)),
            "sampled_pages": max(0, int(profile.get("sampled_pages") or 0)),
            "risk": risk,
            "mean_chars_per_page": float(profile.get("mean_chars_per_page") or 0.0),
            "image_density": float(profile.get("image_density") or 0.0),
            "sparse_fraction": float(profile.get("sparse_fraction") or 0.0),
            "full_native_text_coverage": coverage,
            "low_text_page_count": len(low_text_pages),
            "image_backed_low_text_page_count": len(image_backed_low_text_pages),
        })
    normalized_backend = str(backend_mode or "Automatic").casefold()
    normalized_strategy = str(unstructured_strategy or "auto").casefold()
    likely = [row for row in rows if row["risk"] == "likely"]
    possible = [row for row in rows if row["risk"] == "possible"]
    ocr_explicit = normalized_backend == "unstructured" and normalized_strategy in {"hi_res", "ocr_only"}
    # Importing Unstructured's OCR stack costs several seconds on this machine.
    # It is deferred for text-native and mixed PDFs. A handful of sparse image
    # pages is reported for review, not treated as a prerequisite for an OCR
    # engine before the user can continue.
    runtime = {
        "status": "deferred_native_text_clear",
        "backend_available": None,
        "tesseract_available": None,
    }
    if likely or ocr_explicit:
        try:
            runtime = dict(unstructured_runtime_status("hi_res"))
            runtime["status"] = (
                "ready"
                if runtime.get("backend_available") and runtime.get("tesseract_available")
                else "unavailable"
            )
        except Exception as exc:
            runtime = {
                "status": "probe_failed",
                "backend_available": False,
                "tesseract_available": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
    warnings = []
    if likely:
        names = ", ".join(row["name"] for row in likely[:4])
        suffix = "" if len(likely) <= 4 else f" (+{len(likely) - 4} more)"
        warnings.append(f"{len(likely)} PDF(s) look scan-only from a three-page native sample: {names}{suffix}.")
        if runtime.get("status") != "ready":
            warnings.append(
                "OCR capability is unavailable. If native extraction remains inadequate, affected PDFs will be marked needs_review and withheld from AnythingLLM upload."
            )
        else:
            warnings.append("Automatic may use Unstructured OCR only for the affected PDFs; this is included as an uncertainty range, not a full-batch OCR charge.")
    elif possible:
        warnings.append(f"{len(possible)} PDF(s) have mixed native-text/OCR signals; Automatic will use native extraction first.")
    if ocr_explicit and runtime.get("status") != "ready":
        warnings.append("The selected Unstructured OCR strategy cannot run until both Unstructured and Tesseract are available.")
    return {
        "schema_version": 1,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "sample_policy": "three_evenly_spaced_native_pages_per_pdf",
        "coverage_policy": "all_pages_native_text_and_image_count_before_ocr_deferral",
        "backend_mode": str(backend_mode or "Automatic"),
        "unstructured_strategy": normalized_strategy,
        "files": rows,
        "likely_files": likely,
        "possible_files": possible,
        "likely_pages": sum(row["pages"] for row in likely),
        "runtime": runtime,
        "warnings": warnings,
        "status": "blocked" if ocr_explicit and runtime.get("status") != "ready" else ("warning" if warnings else "clear"),
    }


def automatic_timing_profile_document_limit(files):
    """Use one deterministic profiling policy for every pre-run ETA surface.

    Small folders are cheap enough to profile exactly. Larger folders retain
    the bounded evenly-spaced sample, but selection, setting changes, Review,
    and Confirm all use that same policy so a late callback cannot replace an
    estimate with a materially different profiling scope.
    """
    count = len([path for path in files or [] if path])
    return None if count <= BATCH_FOLDER_FULL_PROFILE_DOCUMENT_LIMIT else BATCH_FOLDER_INITIAL_PROFILE_DOCUMENT_LIMIT


def automatic_progress_file_allocations(
    files,
    *,
    segment_mode="",
    chunk_size=0,
    chunk_overlap=0,
    backend_mode="Automatic",
    unstructured_strategy="auto",
):
    """Allocate evidence progress by general local processing difficulty.

    A fixed equal share per PDF made an eighteen-page image-only scan appear
    to consume exactly as much work as a five-page native-text PDF.  The
    result was a visibly premature total-process bar even though terminal
    completion itself remained correctly guarded.  This deliberately uses
    only bounded, preflight file characteristics; it neither retains text nor
    learns a per-document identity or outcome.
    """
    rows = []
    for raw_path in files or []:
        path = Path(raw_path)
        profile = automatic_timing_document_profile([path])
        features = timing_model_features(
            profile,
            MODE_LOCAL_ONLY_LABEL,
            "local only",
            segment_mode=segment_mode,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            backend_mode=backend_mode,
            unstructured_strategy=unstructured_strategy,
        )
        pages = max(1, int(profile.get("page_count") or 0))
        records = max(pages, int(features.get("estimated_records") or 0))
        # Native extraction scales sublinearly for a long text PDF because it
        # is local and page-bounded. Estimated records still matter, but not
        # as a one-record/one-remote-request proxy.
        weight = 1.0 + .45 * math.sqrt(pages) + .25 * math.sqrt(records)
        # This is deliberately narrower than the broad "possible" bucket:
        # every sampled page must be effectively text-empty and image-backed.
        # It therefore predicts a generic scan-class cost without mispricing
        # a normal OCR-origin PDF such as Weber, which has a usable text layer.
        ocr_likely = bool(
            features.get("ocr_escalation_possible")
            and float(profile.get("sparse_fraction") or 0.0) >= .90
            and float(profile.get("mean_chars_per_page") or 0.0) <= 20.0
            and float(profile.get("image_density") or 0.0) >= .50
        )
        if ocr_likely:
            weight += 15.0
        rows.append({
            "file": path.name,
            "pages": pages,
            "estimated_records": records,
            "ocr_likely_from_preflight": ocr_likely,
            "weight": round(weight, 6),
        })
    total = sum(float(row["weight"]) for row in rows)
    if total <= 0:
        return []
    accumulated = 0.0
    for row in rows:
        share = float(row["weight"]) / total
        row["share"] = share
        row["start_share"] = accumulated
        accumulated += share
        row["end_share"] = accumulated
    # Avoid a harmless floating-point gap at the terminal boundary.
    if rows:
        rows[-1]["end_share"] = 1.0
    return rows


def embedding_timing_lane(engine, model):
    """Classify runtime capacity without pretending all embedders are alike.

    Small local models must retain their own measured cadence because their
    speed depends on the machine and model size.  Hosted OpenRouter models
    share remote infrastructure characteristics closely enough to use a
    cloud-provider prior until a model-specific sample is available.
    """
    normalized_engine = str(engine or "unknown").casefold()
    normalized_model = str(model or "unknown").casefold()
    if normalized_engine in {"ollama", "lmstudio", "localai", "lemonade"}:
        return f"local:{normalized_engine}:{normalized_model}"
    if normalized_engine in {"openrouter", "openai", "gemini", "mistral", "cohere", "voyage", "jinaai", "azure-openai"}:
        return f"cloud:{normalized_engine}"
    # Do not guess whether a generic OpenAI-compatible endpoint is local or
    # hosted. Keep an unknown endpoint model-specific until runtime evidence
    # identifies it.
    return f"provider:{normalized_engine}:{normalized_model}"


def timing_formula_lane(features):
    """Return the non-interchangeable execution lane for ETA evidence."""
    return "|".join([
        str(features.get("mode") or "unknown"),
        str(features.get("native_upload_scope") or "local only"),
        str(features.get("native_upload_transport") or "not_applicable"),
        str(features.get("segment_mode") or "unknown"),
        str(features.get("page_preserve_text_lane") or "not_applicable"),
        str(features.get("effective_segment_target") or features.get("target_passage_length") or 0),
        str(features.get("chunk_size") or 0),
        str(features.get("chunk_overlap") or 0),
        str(features.get("native_upload_representation") or "not_applicable"),
        str(features.get("embedding_timing_lane") or "unknown"),
        str(features.get("embedding_submission_strategy") or "not_applicable"),
        str(features.get("embedding_submission_parallelism") or 0),
        str(features.get("embedding_verification_mode") or "not_applicable"),
        str(features.get("embedding_verification_interval") or 0),
    ])


def timing_native_upload_transport(mode, api_url=""):
    """Classify the upload protocol before a run without probing or mutating it."""
    if mode != MODE_NATIVE_UPLOAD_LABEL:
        return "not_applicable"
    target_url = str(api_url or DEFAULT_ANYTHINGLLM_API_URL).strip()
    return "file_upload" if is_local_anythingllm_url(target_url) else "raw_text"


def timing_local_simulation_identity(local_check_mode):
    """Resolve the ETA-relevant local simulation lane without starting a model."""
    choice = normalize_simulation_choice(local_check_mode)
    if choice == SIMULATION_SKIP_LABEL:
        return "disabled", "none"
    if choice == SIMULATION_ANYTHINGLLM_DEFAULT_LABEL:
        embedding = anythingllm_embedding_config(default_anythingllm_storage_dir())
        return (
            str(embedding.get("engine") or "unknown"),
            str(embedding.get("model") or embedding.get("effective_model") or "unknown"),
        )
    openrouter_models = current_openrouter_simulation_options(force_refresh=False)
    if choice in openrouter_models:
        return "openrouter", str(openrouter_models[choice] or "unknown")
    return "ollama", str(choice or "unknown")


def timing_model_features(profile, mode, native_upload_scope, *, segment_mode="", chunk_size=0, chunk_overlap=0, target_passage_length=0, backend_mode="", unstructured_strategy="auto", native_upload_transport="", native_upload_representation="", simulation_engine="", simulation_model=""):
    page_count = max(0, int(profile.get("page_count") or 0))
    document_count = max(1, int(profile.get("documents") or 1))
    density = max(1.0, float(profile.get("mean_chars_per_page") or 1.0))
    # A deliberately conservative record count approximation for a pre-run
    # estimate. The pipeline replaces it with the exact segment count later.
    normalized_mode = str(segment_mode or "").casefold()
    normalized_backend = str(backend_mode or "Automatic").casefold()
    normalized_strategy = str(unstructured_strategy or "auto").casefold()
    # A scan-like appearance is diagnostic information, not proof that OCR
    # will run. Only an explicitly OCR-capable Unstructured plan earns an OCR
    # cost before the pipeline starts; Automatic can escalate later on evidence.
    ocr_planned = normalized_backend == "unstructured" and normalized_strategy in {"hi_res", "ocr_only"}
    try:
        local_target = max(1, int(target_passage_length or chunk_size or 512))
    except (TypeError, ValueError):
        # The editable dropdown can briefly emit an incomplete custom value.
        # Keep the ETA available while the normal validation path reports a
        # real invalid setting at Review time.
        local_target = max(1, int(chunk_size or 512))
    if normalized_mode == "none" or "all in one file" in normalized_mode or "prepare all content" in normalized_mode:
        # None produces one prepared content file per PDF. AnythingLLM can
        # still split it later, but the app submits one local record.
        effective_segment_target = 0
        records = 1
    elif "whole page" in normalized_mode:
        effective_segment_target = 0
        records = page_count
    else:
        # The configured AnythingLLM-compatible splitter caps the prepared
        # page-bounded payload in both local and native runs.  The local run
        # may not upload, but it still embeds those same prepared records for
        # its retrieval check.  Model the active cap, not a stale 750-character
        # requested target (for example a live 350-character splitter).
        try:
            native_splitter_limit = max(0, int(chunk_size or 0))
        except (TypeError, ValueError):
            native_splitter_limit = 0
        effective_segment_target = local_target
        if native_splitter_limit > 0:
            effective_segment_target = min(effective_segment_target, native_splitter_limit)
        records = max(
            page_count,
            math.ceil((page_count * density) / max(220.0, effective_segment_target * .78)),
        )
    # A scanned PDF can have an empty native text layer while OCR later yields
    # several normal-size page-bounded records.  Treat that as a conservative
    # uncertainty, not as evidence that there will be only one record/page.
    if ocr_planned and profile.get("ocr_risk_bucket") == "high":
        records = max(records, page_count * 10)
    elif ocr_planned and profile.get("ocr_risk_bucket") == "possible":
        records = max(records, page_count * 4)
    # Native page-limit uploads deliberately send one page parent per physical
    # page, while retaining child passages only in the local provenance map.
    # Estimating child passages as independent Desktop requests turned a
    # 13-page upload into an imaginary 33-batch ETA before the exact plan was
    # ready. This does not change the payload; it models the production
    # transport that ``run_automatic_batch`` actually selects.
    if (
        mode == MODE_NATIVE_UPLOAD_LABEL
        and is_page_preserving_segment_mode(normalized_mode)
        and str(native_upload_representation or "").casefold() == "page_parents"
    ):
        records = page_count
    if native_upload_scope == NATIVE_UPLOAD_SCOPE_PROBE_LABEL:
        records = min(2, records)
    embedding = anythingllm_embedding_config(default_anythingllm_storage_dir()) if mode == MODE_NATIVE_UPLOAD_LABEL else {}
    selected_embedding_engine = (
        str(embedding.get("engine") or "unknown")
        if mode == MODE_NATIVE_UPLOAD_LABEL
        else str(simulation_engine or "unknown")
    )
    selected_embedding_model = (
        str(embedding.get("model") or embedding.get("effective_model") or "unknown")
        if mode == MODE_NATIVE_UPLOAD_LABEL
        else str(simulation_model or "unknown")
    )
    try:
        anythingllm_config_batch_size = int(embedding.get("batch_size") or 0)
    except (TypeError, ValueError):
        anythingllm_config_batch_size = 0
    try:
        configured_chunk_overlap = max(0, int(chunk_overlap or 0))
    except (TypeError, ValueError):
        configured_chunk_overlap = 0
    # Local page-bounded payloads use the capped passage size but intentionally
    # do not add splitter overlap; the local prepared-text audit records that
    # exact zero-overlap payload.  Native uploads retain AnythingLLM's active
    # overlap because the Desktop splitter owns that later stage.
    payload_chunk_overlap = (
        configured_chunk_overlap if mode == MODE_NATIVE_UPLOAD_LABEL else 0
    )
    # The app owns its outer submission cadence even when AnythingLLM advertises
    # a different internal batch size. Normal ingestion submits *one* Desktop-
    # style queue per workspace/PDF; AnythingLLM serializes its individual
    # documents inside that accepted queue. The legacy two-record constant is
    # still used by explicit recovery tools, never to price the normal queue.
    batch_size = int(ANYTHINGLLM_EMBEDDING_UPDATE_BATCH_SIZE)
    desktop_queue = (
        mode == MODE_NATIVE_UPLOAD_LABEL
        and str(ANYTHINGLLM_EMBEDDING_SUBMISSION_STRATEGY or "").casefold() == "desktop_queue"
    )
    result = {
        "mode": mode,
        "native_upload_scope": native_upload_scope,
        "native_upload_transport": (
            str(native_upload_transport or "").strip()
            if mode == MODE_NATIVE_UPLOAD_LABEL
            else "not_applicable"
        ) or timing_native_upload_transport(mode),
        "native_upload_representation": (
            str(native_upload_representation or "").casefold()
            if mode == MODE_NATIVE_UPLOAD_LABEL else "not_applicable"
        ) or "segments",
        "segment_mode": str(segment_mode or "unknown"),
        "page_preserve_text_lane": (
            "native_text_only"
            if is_page_preserving_segment_mode(normalized_mode)
            and profile.get("layout_bucket") == "text_first"
            and profile.get("ocr_risk_bucket") == "low"
            else "mixed_or_not_page_preserve"
        ),
        "chunk_size": int(chunk_size or 0),
        "chunk_overlap": payload_chunk_overlap,
        "target_passage_length": local_target,
        "effective_segment_target": effective_segment_target,
        "backend_mode": str(backend_mode or "Automatic"),
        "unstructured_strategy": normalized_strategy,
        "ocr_planned": ocr_planned,
        "ocr_escalation_possible": normalized_backend == "automatic" and profile.get("ocr_risk_bucket") in {"possible", "high"},
        "page_count": page_count,
        "document_count": document_count,
        "estimated_records": int(records),
        # Each PDF receives one workspace queue. This is true both for probe
        # scope and full uploads under the Desktop queue contract; it is not
        # ``ceil(records / 2)`` client-side requests. A multi-PDF selection
        # therefore has one queue per document workspace.
        "estimated_batches": (
            document_count
            if desktop_queue
            else document_count
            if mode == MODE_NATIVE_UPLOAD_LABEL and native_upload_scope == NATIVE_UPLOAD_SCOPE_PROBE_LABEL
            else planned_embedding_batch_count(records, batch_size=batch_size) if mode == MODE_NATIVE_UPLOAD_LABEL else 0
        ),
        "text_density_bucket": profile.get("text_density_bucket", "unknown"),
        "layout_bucket": profile.get("layout_bucket", "unknown"),
        "ocr_risk_bucket": profile.get("ocr_risk_bucket", "unknown"),
        "line_density_bucket": profile.get("line_density_bucket", "unknown"),
        "page_variability_bucket": profile.get("page_variability_bucket", "unknown"),
        "file_size_bucket": profile.get("file_size_bucket", "unknown"),
        "embedding_engine": selected_embedding_engine,
        "embedding_model": selected_embedding_model,
        "embedding_timing_lane": embedding_timing_lane(
            selected_embedding_engine,
            selected_embedding_model,
        ),
        "embedding_submission_strategy": (
            str(ANYTHINGLLM_EMBEDDING_SUBMISSION_STRATEGY or "desktop_queue")
            if mode == MODE_NATIVE_UPLOAD_LABEL else "not_applicable"
        ),
        "embedding_batch_size": batch_size,
        "embedding_submission_parallelism": (
            1
            if mode == MODE_NATIVE_UPLOAD_LABEL else 0
        ),
        "embedding_verification_mode": "checkpoint" if mode == MODE_NATIVE_UPLOAD_LABEL else "not_applicable",
        "embedding_verification_interval": (
            int(ANYTHINGLLM_EMBEDDING_VERIFICATION_CHECKPOINT_INTERVAL)
            if mode == MODE_NATIVE_UPLOAD_LABEL else 0
        ),
        "anythingllm_config_batch_size": anythingllm_config_batch_size,
    }
    result["timing_formula_lane"] = timing_formula_lane(result)
    return result


def timing_model_observation_usable(row):
    """Reject test fixtures and incomplete runs from ETA calibration.

    The run log is intentionally append-only, so bad historical rows stay
    inspectable but never influence a real user's next estimate.
    """
    run_key_parts = {part.casefold() for part in Path(str(row.get("run_key") or "")).parts}
    # The test suite intentionally invokes the real orchestration function
    # under this exact disposable directory. Its append-only artifacts remain
    # useful for debugging, but synthetic latency must never train the app.
    is_fixture_path = "tmp-output" in run_key_parts
    # A one-off Windows OCR harness was accidentally re-entered by spawned
    # workers under this directory.  Keep those preserved records auditable,
    # but do not let concurrent duplicate runs distort real local ETA lanes.
    is_reentered_harness_path = "timer local calibration" in run_key_parts
    terminal_message = str(row.get("timing_terminal_message") or "")
    completed_warning_with_searchability = (
        str(row.get("state") or "") == "warning"
        and terminal_message.startswith(
            "Searchable vectors and runtime retrieval succeeded, but the final storage observation could not confirm workspace document-list rows"
        )
    )
    return (
        str(row.get("source") or "") in {"automatic-run", "backfilled-run-summary"}
        and not is_fixture_path
        and not is_reentered_harness_path
        and (str(row.get("state") or "") == "successful" or completed_warning_with_searchability)
        and int(row.get("page_count") or 0) > 0
        and float(row.get("actual_seconds") or 0) >= 5.0
        and str(row.get("duration_provenance") or "") == "active_observation_window"
    )


def timing_model_legacy_observation_usable(row):
    """Return low-confidence eligibility for pre-provenance completed runs.

    Older automatic runs did not record an activity-window boundary, so they
    cannot be treated as exact duration truth.  Excluding every successful
    one, however, left the estimator blind to the project’s best historical
    evidence.  This lane is deliberately lower weight and never admits test,
    cancelled, or backfilled-only records.
    """
    run_key_parts = {part.casefold() for part in Path(str(row.get("run_key") or "")).parts}
    provenance = str(row.get("duration_provenance") or "")
    return (
        str(row.get("source") or "") == "automatic-run"
        and str(row.get("state") or "") == "successful"
        and "tmp-output" not in run_key_parts
        and "timer local calibration" not in run_key_parts
        and int(row.get("page_count") or 0) > 0
        and float(row.get("actual_seconds") or 0) >= 5.0
        and provenance in {"", "legacy_wall_clock", "wall_clock"}
    )


def timing_model_learning_observation_usable(row):
    """Accept active observations first, with guarded legacy fallback."""
    return timing_model_observation_usable(row) or timing_model_legacy_observation_usable(row)


def timing_model_batch_observation_usable(row):
    """Accept measured batch cadence when vector evidence is usable for timing.

    A final ``warning`` can mean the workspace document list was ambiguous
    even though every submitted batch reached searchable vectors.  That is not
    sufficient to learn whole-run completion duration, but its measured HTTP +
    searchability latency is still valid provider-capacity evidence.
    """
    run_key_parts = {part.casefold() for part in Path(str(row.get("run_key") or "")).parts}
    accepted_measurements = [
        measurement for measurement in (row.get("batch_measurements") or [])
        if str(measurement.get("state") or "").casefold() == "accepted"
        and float(measurement.get("elapsed_seconds") or 0) > 0
    ]
    terminal_legacy_run = (
        str(row.get("state") or "") in {"successful", "warning"}
        and any(float(value) > 0 for value in (row.get("batch_seconds") or []))
    )
    return (
        str(row.get("source") or "") == "automatic-run"
        and "tmp-output" not in run_key_parts
        # Per-batch measurements are captured at request completion, rather
        # than inferred from outer wall clock. An accepted batch remains valid
        # capacity evidence even when a later batch makes the overall run fail.
        and str(row.get("duration_provenance") or "") in {"", "active_observation_window", "legacy_wall_clock", "wall_clock"}
        and int(row.get("actual_batches") or 0) > 0
        and bool(accepted_measurements or terminal_legacy_run)
    )


def timing_model_batch_prior_seconds(features, history):
    """Use robust measured request time from the matching extraction regime."""
    engine = str(features.get("embedding_engine") or "").casefold()
    model = str(features.get("embedding_model") or "").casefold()
    lane = str(features.get("embedding_timing_lane") or embedding_timing_lane(engine, model))
    ocr_regime = bool(features.get("ocr_planned") or features.get("ocr_observed"))
    requested_verification_mode = str(
        features.get("embedding_verification_mode")
        or ("every_batch" if str(features.get("mode") or "") == MODE_NATIVE_UPLOAD_LABEL else "not_applicable")
    ).casefold()
    requested_verification_interval = int(
        features.get("embedding_verification_interval")
        or (ANYTHINGLLM_EMBEDDING_VERIFICATION_CHECKPOINT_INTERVAL if requested_verification_mode == "checkpoint" else 1 if requested_verification_mode == "every_batch" else 0)
    )
    requested_strategy = str(features.get("embedding_submission_strategy") or "").casefold()
    exact_samples = []
    family_samples = []
    for row in history:
        if not timing_model_batch_observation_usable(row):
            continue
        historical_engine = str(row.get("embedding_engine") or "").casefold()
        historical_model = str(row.get("embedding_model") or "").casefold()
        historical_lane = str(row.get("embedding_timing_lane") or embedding_timing_lane(historical_engine, historical_model))
        # Native scope and local segmentation alter both the number of native
        # locations and how much document-level preparation precedes them.
        # A two-record probe is not a valid cadence proxy for an all-record
        # upload, and passage units are not interchangeable with page units.
        if str(row.get("mode") or "") != str(features.get("mode") or ""):
            continue
        if str(row.get("native_upload_scope") or "") != str(features.get("native_upload_scope") or ""):
            continue
        if str(row.get("native_upload_transport") or "") != str(features.get("native_upload_transport") or ""):
            continue
        if str(row.get("native_upload_representation") or "") != str(features.get("native_upload_representation") or ""):
            continue
        if str(row.get("segment_mode") or "") != str(features.get("segment_mode") or ""):
            continue
        if str(row.get("embedding_submission_strategy") or "").casefold() != requested_strategy:
            continue
        if requested_strategy == "desktop_queue" and str(row.get("state") or "") != "successful":
            # A timed-out/warning queue may have eventually indexed, but its
            # elapsed request includes recovery ambiguity. Keep it auditable,
            # never let it establish the normal Desktop-queue ETA.
            continue
        # A serialized exact-vector gate is a different capacity regime from
        # the retired fan-out scheduler. Historical rows without this explicit
        # field are retained for audit but cannot make a new serial ETA look
        # like a concurrent run.
        if int(row.get("embedding_submission_parallelism") or 0) != int(
            features.get("embedding_submission_parallelism") or 0
        ):
            continue
        # A local payload target and AnythingLLM's native splitter both alter
        # record sizes and queue behavior.  Keep benchmark conditions clean:
        # a measured 750-character/1024-token run is not evidence for a
        # different segmentation or native chunking condition.
        exact_settings = True
        for key in ("effective_segment_target", "chunk_size", "chunk_overlap"):
            if int(row.get(key) or 0) != int(features.get(key) or 0):
                exact_settings = False
                break
        # Local cadence is machine/model-specific. Hosted OpenRouter capacity
        # is intentionally pooled by provider, not by model label, because
        # provider infrastructure dominates the observed request timing.
        if lane.startswith("local:") and historical_lane != lane:
            continue
        if lane.startswith("cloud:") and historical_lane != lane:
            continue
        if not lane.startswith(("local:", "cloud:")) and historical_lane != lane:
            continue
        # OCR-generated records have radically different batch counts and
        # request cadence. They must not make ordinary text PDFs pessimistic,
        # nor make a confirmed OCR run inherit text-only timings.
        historical_ocr = bool(row.get("ocr_used") or row.get("ocr_planned"))
        if historical_ocr != ocr_regime:
            continue
        historical_mode = str(row.get("embedding_verification_mode") or "every_batch").casefold()
        historical_interval = int(row.get("embedding_verification_interval") or 1)
        destination = exact_samples if exact_settings else family_samples
        measurements = list(row.get("batch_measurements") or [])
        accepted_measurements = [
            measurement for measurement in measurements
            if str(measurement.get("state") or "").casefold() == "accepted"
        ]
        if historical_mode == requested_verification_mode and historical_interval == requested_verification_interval:
            if measurements:
                destination.extend(
                    float(measurement.get("elapsed_seconds") or 0)
                    for measurement in accepted_measurements
                    if float(measurement.get("elapsed_seconds") or 0) > 0
                )
            else:
                # Legacy terminal runs predate per-batch state recording.
                destination.extend(float(value) for value in row.get("batch_seconds") or [] if float(value) > 0)
        elif requested_verification_mode == "checkpoint":
            # Older measurements included a full searchability poll after
            # every submission.  Reusing their elapsed batch time would make
            # the new checkpoint policy look as slow as the retired policy.
            # Submission-only measurements are compatible capacity evidence.
            submission_samples = (
                [measurement.get("submission_seconds") for measurement in accepted_measurements]
                if measurements else row.get("batch_submission_seconds") or []
            )
            destination.extend(float(value) for value in submission_samples if float(value or 0) > 0)
    samples = exact_samples or family_samples
    if samples:
        # A robust upper-middle observation improves the general prediction
        # without storing a document identity or treating a one-off timeout as
        # normal. The cap prevents a stalled backend from becoming a permanent
        # estimate for every subsequent run.
        minimum = 1.0 if lane.startswith("cloud:") else 1.5
        # A two-record probe repeats many tiny requests. Its occasional
        # provider queue spike should not be multiplied by every remaining
        # document, so use an upper-middle (not upper-quartile) sample there.
        percentile = .60 if str(features.get("native_upload_scope") or "") == NATIVE_UPLOAD_SCOPE_PROBE_LABEL else .75
        source = (
            f"measured exact {lane} batches"
            if exact_samples else
            f"measured {lane} batch family (same mode/scope/transport/segmentation/OCR; chunk settings differ)"
        )
        # A valid serialized Desktop request can take several minutes while
        # the provider recursively splits, embeds, and writes LanceDB rows.
        # The former 90-second ceiling hid observed 248–309 second batches and
        # made later ETAs materially optimistic. Keep a finite ceiling below
        # the reconciliation circuit breaker, rather than pretending that
        # accepted slow work is an anomaly.
        return min(360.0, max(minimum, _timing_percentile(samples, percentile) * 1.08)), len(samples), source
    if lane.startswith("cloud:"):
        # AnythingLLM adds native documents sequentially inside one API call.
        # A tiny generic network prior severely understates a first observed
        # cloud run, especially for four-document batches.
        # Production serializes this path and waits for exact-vector evidence
        # after every request. Start conservatively until this exact lane has
        # its own measured batches; retired concurrent rows are intentionally
        # not borrowed here.
        return 180.0, 0, f"conservative unmeasured serialized {lane} batch prior"
    if lane.startswith("local:"):
        return 10.0, 0, f"conservative unmeasured {lane} prior"
    return 6.0, 0, f"conservative unmeasured {lane} prior"


def timing_model_base_seconds(features, *, batch_seconds_prior=6.0):
    """Transparent phase-based ETA formula, using a measured batch-rate when known."""
    pages = max(0, int(features.get("page_count") or 0))
    records = max(0, int(features.get("estimated_records") or 0))
    batches = max(0, int(features.get("estimated_batches") or 0))
    documents = max(1, int(features.get("document_count") or 1))
    preflight_likely_pages = max(0, int(features.get("ocr_preflight_likely_pages") or 0))
    # A multi-PDF run is neither one giant document nor N independent full
    # runs.  It has one shared run setup/finish plus a small boundary cost for
    # each additional PDF (metadata, output package, and state transition).
    document_boundaries = max(0, documents - 1)
    segment_mode = str(features.get("segment_mode") or "").casefold()
    if features.get("mode") != MODE_NATIVE_UPLOAD_LABEL:
        # Local preparation is normally dominated by inexpensive extraction,
        # segmentation, and writing the review package.  The old generic
        # formula charged every estimated record like a remote embedding
        # request, which made native-text documents (especially long books)
        # several times too pessimistic.  This intentionally models classes,
        # not filenames or a per-PDF history.
        if features.get("ocr_planned") or (
            features.get("ocr_escalation_possible")
            and features.get("ocr_risk_bucket") == "high"
        ):
            # Automatic has not yet proved that OCR will be used, but a PDF
            # with no usable sampled text is enough evidence to reserve a
            # bounded scan-class budget.  This avoids the previous optimistic
            # text-only ETA without pretending OCR has already started.
            return max(30.0, 12.0 + document_boundaries * .75 + pages * 1.5 + records * .02)
        # Local segment serialization is measurable but remains a small
        # fraction of native-text preparation; keep it bounded so long text
        # PDFs do not inherit a remote-embedding-style per-record cost.
        segment_packaging = .0 if "whole page" in segment_mode else records * .0025
        local_seconds = 6.0 + document_boundaries * .75 + pages * .035 + records * .006 + segment_packaging
        simulation_engine = str(features.get("embedding_engine") or "").casefold()
        # A local retrieval check truly embeds every prepared record.  Ollama
        # runs on the user's hardware, so its record time is a separate lane
        # from cloud checks; cloud remains materially faster, and Skip does no
        # embedding at all.  Per-model historical calibration then refines the
        # conservative Ollama baseline without pooling Qwen and Gemma.
        if simulation_engine == "ollama":
            local_seconds += records * max(.10, float(features.get("local_embedding_seconds_per_record") or 1.10))
        elif simulation_engine == "openrouter":
            local_seconds += 4.0 + records * max(.02, float(features.get("local_embedding_seconds_per_record") or .12))
        if features.get("layout_bucket") == "image_or_table_heavy":
            local_seconds *= 1.10
        if features.get("line_density_bucket") == "high":
            local_seconds += .25
        if features.get("page_variability_bucket") == "variable":
            local_seconds += .25
        # A scan-only preflight is evidence, not proof that OCR will be
        # selected. Reserve a modest preparation allowance here so the ETA
        # does not jump only after this PDF reaches the extractor.
        local_seconds += preflight_likely_pages * .8
        return max(8.0, local_seconds)
    # Local preparation is mostly linear file I/O and report generation.  The
    # upload path includes file preparation, the remote embedding queue, and
    # final vector observation.  OCR and upload costs are accounted for at
    # their distinct phases rather than borrowing local-only timings.
    extraction = 8.0 + document_boundaries * 1.5 + pages * .18
    if features.get("ocr_planned") and features.get("ocr_risk_bucket") == "high":
        extraction += pages * .6
    elif features.get("ocr_planned") and features.get("ocr_risk_bucket") == "possible":
        extraction += pages * .25
    if features.get("layout_bucket") == "image_or_table_heavy":
        extraction *= 1.10
    if features.get("line_density_bucket") == "high":
        extraction += pages * .15
    if features.get("page_variability_bucket") == "variable":
        extraction += pages * .12
    extraction += preflight_likely_pages * 1.5
    # A native probe has deliberately bounded locations and a narrower final
    # observation. Keep it in a different formula lane from full corpus
    # upload even when the embedding provider is identical.
    probe_scope = str(features.get("native_upload_scope") or "") == NATIVE_UPLOAD_SCOPE_PROBE_LABEL
    file_upload = str(features.get("native_upload_transport") or "") == "file_upload"
    # The Desktop file hand-off has one small staging action; a remote raw-text
    # request instead serializes the payload directly.  This is intentionally
    # a bounded setup difference, not an invented per-document network rate.
    transport_setup = 1.5 if file_upload else .75
    raw_upload = (3.0 if probe_scope else 5.0) + transport_setup + document_boundaries * .5 + records * .05
    embedding_batches = batches * max(1.5, float(batch_seconds_prior or 6.0))
    if probe_scope:
        # Unlike a full payload, every probe document waits for its own vector
        # observation and readiness report before the next PDF can begin.  A
        # former single five-second final-observation term omitted this
        # repeated barrier and produced extreme underestimates for 10-PDF
        # validation runs.  This is deliberately bounded; measured runs then
        # refine the lane without treating one slow observer as universal.
        configured_probe_observation = features.get("probe_observation_seconds")
        probe_observation_seconds = (
            35.0
            if configured_probe_observation is None
            else max(0.0, float(configured_probe_observation))
        )
        final_observation = 5.0 + document_boundaries * probe_observation_seconds
    else:
        final_observation = 8.0
    # Global Desktop/schema inspection is now once per batch rather than once
    # per PDF. A measured matching-stage prior replaces the generic allowance
    # after the first compatible run; it is never multiplied by documents.
    inspection = max(2.0, float(features.get("batch_inspection_seconds_prior") or 12.0))
    return max(35.0 if probe_scope else 60.0, extraction + inspection + raw_upload + embedding_batches + final_observation)


def ocr_runtime_surcharge_seconds(estimate, history=None, observed_pages=0):
    """Calculate a bounded, matching-regime OCR adjustment for one PDF.

    OCR is discovered after a file's extraction pass, so a batch estimate must
    never reprice every file using full-run historical upload duration.  The
    returned amount is the extra budget for the observed PDF after subtracting
    that file's proportional share of the existing total estimate.
    """
    features = dict((estimate or {}).get("features") or {})
    if not features or features.get("ocr_planned"):
        return 0
    total_pages = max(1, int(features.get("page_count") or 0))
    pages = max(1, min(total_pages, int(observed_pages or total_pages)))
    mode = str(features.get("mode") or "")
    scope = str(features.get("native_upload_scope") or "")
    segment_mode = str(features.get("segment_mode") or "")
    # A local-only OCR record measures extraction/packaging.  An upload record
    # also includes AnythingLLM's embedding queue.  Mixing them was the source
    # of a large, misleading jump in a historical local timing sample.
    matching_per_page = [
        float(row.get("actual_seconds") or 0) / max(1, int(row.get("page_count") or 0))
        for row in (history or [])
        if (
            timing_model_observation_usable(row)
            and bool(row.get("ocr_used"))
            and str(row.get("mode") or "") == mode
            and str(row.get("native_upload_scope") or "") == scope
            and str(row.get("segment_mode") or "") == segment_mode
        )
    ]
    # An automatic high-resolution candidate is only an in-progress fallback,
    # not proof that every remaining page will require OCR.  Start with a
    # deliberately modest allowance and let observed phase/batch timings
    # revise it; the prior 12s/page upload assumption produced multi-hour
    # jumps for a single unproven 791-page candidate.
    default_per_page = 1.5 if mode != MODE_NATIVE_UPLOAD_LABEL else 3.0
    if matching_per_page:
        ceiling = 4.0 if mode != MODE_NATIVE_UPLOAD_LABEL else 6.0
        per_page = min(ceiling, max(1.0, _timing_percentile(matching_per_page, .75)))
    else:
        per_page = default_per_page
    target_for_observed_file = per_page * pages
    current = float((estimate or {}).get("expected_seconds") or 0)
    proportional_budget = current * pages / total_pages
    cap = max(20.0, pages * (4.0 if mode != MODE_NATIVE_UPLOAD_LABEL else 6.0))
    return int(math.ceil(min(cap, max(0.0, target_for_observed_file - proportional_budget))))


def recalibrated_run_eta_seconds(
    current_expected,
    elapsed_seconds,
    total_batches,
    completed_batches,
    batch_seconds,
    *,
    remaining_batch_count=None,
    remaining_non_batch_seconds=15.0,
):
    """Adapt an in-flight ETA from its own completed batch observations.

    This is not a per-PDF learned prediction: it uses only the bounded current
    run's accepted batches, after enough observations exist to reject a single
    transient request. It corrects both a fast ordinary run and a slow backend
    period while preserving the evidence-driven completion bar.
    """
    current = max(0, int(round(float(current_expected or 0))))
    elapsed = max(0.0, float(elapsed_seconds or 0.0))
    total = max(0, int(total_batches or 0))
    completed = max(0, min(total, int(completed_batches or 0)))
    samples = [float(value) for value in (batch_seconds or []) if float(value) > 0]
    if total <= 0 or len(samples) < 3 or completed <= 0:
        return current
    # The first accepted native request commonly includes connection/setup
    # work that is not representative of the remaining hundreds of batches.
    # A three-sample 75th percentile made that cold request dominate the ETA
    # and produced a large early jump on a healthy local run.
    # Use the robust middle cadence; after four observations, drop the cold
    # request entirely. Slow sustained service is still reflected because the
    # median rises only when multiple completed batches are actually slow.
    cadence_samples = samples[1:] if len(samples) >= 4 else samples
    per_batch = statistics.median(cadence_samples)
    remaining = max(0, total - completed) if remaining_batch_count is None else max(0, int(remaining_batch_count))
    # A current document's batch count is not the whole batch run.  The caller
    # supplies the remaining count across the current and all later PDFs;
    # separately preserve remaining extraction/report work so a fast first
    # document cannot collapse a ten-document ETA to a few minutes.
    candidate = elapsed + per_batch * remaining + max(10.0, float(remaining_non_batch_seconds or 0.0))
    candidate = max(elapsed + 10.0, candidate)
    if abs(candidate - current) < 15.0:
        return current
    return int(math.ceil(candidate))


def evidence_paced_eta_seconds(current_expected, elapsed_seconds, confirmed_fraction, *, minimum_remaining=15.0):
    """Bounded ETA correction from completed pipeline-stage evidence.

    This is deliberately separate from the visual progress bar: only a real
    completed stage may influence it, and a small early stage cannot collapse
    a long upload estimate. It gives a run a way to learn before three native
    batches exist (for example when global inspection is unexpectedly slow).
    """
    current = max(0.0, float(current_expected or 0.0))
    elapsed = max(0.0, float(elapsed_seconds or 0.0))
    confirmed = min(.92, max(0.0, float(confirmed_fraction or 0.0)))
    if current <= 0 or elapsed < 10.0 or confirmed < .18:
        return int(round(current))
    paced = elapsed / confirmed
    # Keep phase evidence conservative: do not allow one early phase to cut
    # more than 65%, or inflate more than 2.5x, before batch cadence arrives.
    candidate = min(current * 2.5, max(current * .35, paced, elapsed + minimum_remaining))
    return int(math.ceil(candidate))


def queue_evidence_eta_seconds(elapsed_seconds, queue_remaining_seconds):
    """Forecast completion from the owned Desktop queue without double-counting vectors.

    The queue observer and exact-vector observer describe one concurrent
    ingestion interval.  A live owned queue rate is therefore stronger than a
    preflight prior once it has an estimated remaining duration.  Reserve only
    a bounded tail for exact-vector/retrieval handoff; it is not added as a
    second queue phase.
    """
    elapsed = max(0.0, float(elapsed_seconds or 0.0))
    remaining = max(0.0, float(queue_remaining_seconds or 0.0))
    handoff_tail = min(20.0, max(5.0, remaining * 0.5))
    return int(math.ceil(max(elapsed + 8.0, elapsed + remaining + handoff_tail)))


def bounded_queue_eta_reprice(current_expected, queue_forecast):
    """Take one readable ETA step toward mature owned-queue evidence.

    The forecast can be much more accurate than the opening prior, but a
    single update must not replace (say) two minutes with forty-four.  The
    next mature observation may take another bounded step.  This makes the
    countdown stable while preserving the full raw rate in timing evidence.
    """
    current = max(0.0, float(current_expected or 0.0))
    forecast = max(0.0, float(queue_forecast or 0.0))
    if current <= 0.0:
        return int(math.ceil(forecast))
    lower = current * (1.0 - QUEUE_ETA_MAX_CHANGE_RATIO)
    upper = current * (1.0 + QUEUE_ETA_MAX_CHANGE_RATIO)
    return int(math.ceil(min(upper, max(lower, forecast))))


def timing_model_similarity(features, historical):
    score = 0.0
    for key, weight in (("timing_formula_lane", 7), ("mode", 5), ("segment_mode", 4), ("page_preserve_text_lane", 3), ("native_upload_scope", 3), ("native_upload_transport", 2), ("native_upload_representation", 3), ("chunk_size", 3), ("chunk_overlap", 2), ("effective_segment_target", 3), ("target_passage_length", 1), ("backend_mode", 1), ("unstructured_strategy", 2), ("embedding_engine", 3), ("embedding_model", 4), ("text_density_bucket", 2), ("layout_bucket", 2), ("ocr_risk_bucket", 2), ("line_density_bucket", 1), ("page_variability_bucket", 1), ("file_size_bucket", 1)):
        if str(features.get(key) or "") == str(historical.get(key) or ""):
            score += weight
    expected_ocr = bool(features.get("ocr_planned") or features.get("ocr_observed"))
    historical_ocr = bool(historical.get("ocr_used") or historical.get("ocr_planned"))
    if expected_ocr == historical_ocr:
        score += 3
    pages = max(1, int(features.get("page_count") or 0), int(historical.get("page_count") or 0))
    score += max(0.0, 3.0 - 3.0 * abs(int(features.get("page_count") or 0) - int(historical.get("page_count") or 0)) / pages)
    return score


def timing_local_embedding_prior(features, history):
    """Return a small, lane-specific local embed cost from completed runs.

    This is deliberately one number per model lane, rather than a second ETA
    system.  It keeps local Ollama models distinct while allowing cloud local
    checks to pool by provider through ``embedding_timing_lane``.
    """
    if str(features.get("mode") or "") == MODE_NATIVE_UPLOAD_LABEL:
        return 0.0, 0, "not applicable to native uploads"
    engine = str(features.get("embedding_engine") or "").casefold()
    if engine not in {"ollama", "openrouter"}:
        return 0.0, 0, "no embedding check selected"
    lane = str(features.get("embedding_timing_lane") or "")
    samples = []
    for row in history or []:
        if (
            not timing_model_observation_usable(row)
            or str(row.get("mode") or "") != str(features.get("mode") or "")
            or str(row.get("embedding_timing_lane") or "") != lane
            or str(row.get("segment_mode") or "") != str(features.get("segment_mode") or "")
        ):
            continue
        records = int(row.get("actual_records") or row.get("estimated_records") or 0)
        actual_seconds = float(row.get("actual_seconds") or 0)
        if records <= 0 or actual_seconds <= 0:
            continue
        without_embedding = dict(row, embedding_engine="disabled", embedding_model="none")
        baseline = timing_model_base_seconds(without_embedding)
        samples.append(max(.02, min(5.0, (actual_seconds - baseline) / records)))
    if not samples:
        return 0.0, 0, f"conservative unmeasured {lane} prior"
    return _timing_percentile(samples, .6) or 0.0, len(samples), f"measured {lane} local embedding cost"


def timing_native_probe_observation_prior(features, history):
    """Learn the repeated per-PDF post-indexing wait for native probe runs."""
    if (
        str(features.get("mode") or "") != MODE_NATIVE_UPLOAD_LABEL
        or str(features.get("native_upload_scope") or "") != NATIVE_UPLOAD_SCOPE_PROBE_LABEL
    ):
        return 0.0, 0, "not applicable outside native probe scope"
    lane = str(features.get("embedding_timing_lane") or "")
    samples = []
    for row in history or []:
        if (
            not timing_model_observation_usable(row)
            or str(row.get("mode") or "") != MODE_NATIVE_UPLOAD_LABEL
            or str(row.get("native_upload_scope") or "") != NATIVE_UPLOAD_SCOPE_PROBE_LABEL
            or str(row.get("native_upload_transport") or "") != str(features.get("native_upload_transport") or "")
            or str(row.get("embedding_timing_lane") or "") != lane
        ):
            continue
        documents = max(1, int(row.get("document_count") or 1))
        if documents < 2:
            continue
        actual_seconds = float(row.get("actual_seconds") or 0)
        if actual_seconds <= 0:
            continue
        without_repeated_observation = dict(row, probe_observation_seconds=0)
        baseline = timing_model_base_seconds(without_repeated_observation)
        samples.append(max(5.0, min(120.0, (actual_seconds - baseline) / (documents - 1))))
    if not samples:
        return 0.0, 0, f"conservative unmeasured {lane} probe observation prior"
    return _timing_percentile(samples, .75) or 0.0, len(samples), f"measured {lane} probe observation cost"


def _timing_percentile(values, percentile=.75):
    ordered = sorted(float(value) for value in values if float(value) > 0)
    if not ordered:
        return None
    index = min(len(ordered) - 1, max(0, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


def ensure_timing_model_backfill():
    """Seed the central model from existing app run summaries exactly once."""
    existing = {str(row.get("run_key") or "") for row in _read_timing_jsonl(TIMING_MODEL_RUNS_PATH, limit=1000)}
    seeded = 0
    for summary_path in sorted(automatic_run_artifact_paths(AUTO_OUTPUT_DIR, "*/run-summary.json"), key=lambda path: path.stat().st_mtime)[-100:]:
        run_key = str(summary_path.parent.parent)
        if run_key in existing:
            continue
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            elapsed = float(summary.get("total_pipeline_seconds") or 0)
            pages = int(summary.get("pdf_page_count") or 0)
            if elapsed <= 0 or pages <= 0:
                continue
            uploaded = str(summary.get("api_upload_status") or "") in {"complete", "complete_with_key_cleanup_warning", "error"}
            records = int(summary.get("api_embedding_update_requested") or summary.get("segments") or 0)
            batch_size = max(1, int(
                summary.get("api_embedding_update_batch_size") or ANYTHINGLLM_EMBEDDING_UPDATE_BATCH_SIZE
            ))
            row = {
                "schema_version": TIMING_MODEL_VERSION,
                "recorded_at": datetime.now().isoformat(timespec="seconds"),
                "run_key": run_key,
                "source": "backfilled-run-summary",
                "state": "historical",
                "actual_seconds": round(elapsed, 3),
                "page_count": pages,
                "estimated_records": records,
                "actual_records": records,
                "estimated_batches": math.ceil(records / batch_size) if uploaded and records else 0,
                "actual_batches": len(summary.get("api_embedding_update_batches") or []),
                "mode": MODE_NATIVE_UPLOAD_LABEL if uploaded else MODE_LOCAL_ONLY_LABEL,
                "native_upload_scope": NATIVE_UPLOAD_SCOPE_ALL_LABEL if uploaded else "local only",
                "segment_mode": summary.get("segment_mode") or "unknown",
                "chunk_size": int(summary.get("chunk_size") or 0),
                "chunk_overlap": int(summary.get("chunk_overlap") or 0),
                "backend_mode": summary.get("selected_backend") or "unknown",
                "embedding_engine": summary.get("anythingllm_embedding_engine") or "unknown",
                "embedding_model": summary.get("anythingllm_embedding_model") or "unknown",
                "embedding_batch_size": int(summary.get("anythingllm_embedding_batch_size") or batch_size),
                "text_density_bucket": "unknown",
                "layout_bucket": "unknown",
                "ocr_risk_bucket": "unknown",
                "line_density_bucket": "unknown",
                "page_variability_bucket": "unknown",
                "file_size_bucket": "unknown",
                "batch_seconds": [
                    round(float(batch.get("batch_elapsed_seconds") or 0), 3)
                    for batch in summary.get("api_embedding_update_batches") or []
                    if float(batch.get("batch_elapsed_seconds") or 0) > 0
                ],
            }
            _append_timing_jsonl(TIMING_MODEL_RUNS_PATH, row)
            existing.add(run_key)
            seeded += 1
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return seeded


def hydrated_timing_model_history():
    """Read the append-only log and enrich old rows from their local summary.

    Early timing rows predate provider/model and per-batch measurements.  The
    original JSONL is intentionally preserved; this read-time hydration lets
    the model immediately benefit from the corresponding run summary.
    """
    hydrated = []
    for original in _read_timing_jsonl(TIMING_MODEL_RUNS_PATH):
        row = dict(original)
        run_root = Path(str(row.get("run_key") or ""))
        if run_root.exists():
            try:
                terminal_path = run_root / "ingestion-terminal-record.json"
                if terminal_path.exists():
                    terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
                    row["timing_terminal_message"] = str(terminal.get("message") or "")
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                pass
        if (timing_model_observation_usable(row) or timing_model_batch_observation_usable(row)) and run_root.exists():
            try:
                summary_path = next(run_root.glob("*/run-summary.json"), None)
                if summary_path:
                    summary = json.loads(summary_path.read_text(encoding="utf-8"))
                    row["embedding_engine"] = row.get("embedding_engine") or summary.get("anythingllm_embedding_engine") or "unknown"
                    row["embedding_model"] = row.get("embedding_model") or summary.get("anythingllm_embedding_model") or "unknown"
                    row["embedding_batch_size"] = (
                        row.get("embedding_batch_size")
                        or summary.get("anythingllm_embedding_batch_size")
                        or ANYTHINGLLM_EMBEDDING_UPDATE_BATCH_SIZE
                    )
                    row["backend_mode"] = row.get("backend_mode") or summary.get("selected_backend") or "unknown"
                    # Terminal rows created by older app processes can carry
                    # the visible dropdown value (for example 1024) even
                    # though inherited AnythingLLM settings actually split
                    # at 350. The per-PDF summary is the authoritative
                    # observed setting, so use it to safely normalize legacy
                    # timing evidence at read time without rewriting history.
                    observed_chunk_size = int(summary.get("chunk_size") or 0)
                    observed_chunk_overlap = int(summary.get("chunk_overlap") or 0)
                    if observed_chunk_size > 0:
                        row["chunk_size"] = observed_chunk_size
                    # Prepared page-bounded payloads have zero local overlap.
                    # For a native upload, however, the ETA condition must
                    # retain AnythingLLM's configured downstream splitter
                    # overlap from the original timing row.
                    if observed_chunk_overlap >= 0 and str(row.get("mode") or "") != MODE_NATIVE_UPLOAD_LABEL:
                        row["chunk_overlap"] = observed_chunk_overlap
                    segment_mode = str(row.get("segment_mode") or "").casefold()
                    is_page_bounded = not (
                        segment_mode == "none"
                        or "all in one file" in segment_mode
                        or "prepare all content" in segment_mode
                        or "whole page" in segment_mode
                    )
                    requested_target = int(row.get("target_passage_length") or 0)
                    effective_chunk_size = observed_chunk_size or int(row.get("chunk_size") or 0)
                    if is_page_bounded and requested_target > 0 and effective_chunk_size > 0:
                        row["effective_segment_target"] = min(requested_target, effective_chunk_size)
                    actual_records = int(row.get("actual_records") or 0)
                    if actual_records > 0:
                        # A completed run knows the exact prepared record
                        # count.  Preserve the raw log's pre-run estimate, but
                        # calibrate read-time history against that observed
                        # workload so an underestimated inherited splitter
                        # cannot inflate its learned duration multiplier.
                        row["estimated_records"] = actual_records
                        if str(row.get("mode") or "") == MODE_NATIVE_UPLOAD_LABEL:
                            # Recent version-1 rows can be identified from
                            # their one accepted batch containing more than
                            # the retired two-record client batch. They were
                            # already sent as the Desktop-style queue; only
                            # their ETA metadata still described the old
                            # scheduler. Reclassify in memory for learning,
                            # never rewrite the append-only source line.
                            if (
                                not row.get("embedding_submission_strategy")
                                and int(row.get("actual_batches") or 0) == 1
                                and actual_records > int(ANYTHINGLLM_EMBEDDING_UPDATE_BATCH_SIZE)
                            ):
                                row["embedding_submission_strategy"] = "desktop_queue"
                                row["embedding_submission_parallelism"] = 1
                                row["embedding_verification_mode"] = "checkpoint"
                                row["embedding_verification_interval"] = int(
                                    ANYTHINGLLM_EMBEDDING_VERIFICATION_CHECKPOINT_INTERVAL
                                )
                            row["estimated_batches"] = (
                                max(1, int(row.get("actual_batches") or row.get("document_count") or 1))
                                if (
                                    str(row.get("embedding_submission_strategy") or "").casefold() == "desktop_queue"
                                    or str(row.get("native_upload_scope") or "") == NATIVE_UPLOAD_SCOPE_PROBE_LABEL
                                )
                                else math.ceil(
                                    actual_records / max(1, int(row.get("embedding_batch_size") or ANYTHINGLLM_EMBEDDING_UPDATE_BATCH_SIZE))
                                )
                            )
                    if str(row.get("mode") or "") == MODE_NATIVE_UPLOAD_LABEL:
                        row["native_upload_transport"] = (
                            row.get("native_upload_transport")
                            or timing_native_upload_transport(MODE_NATIVE_UPLOAD_LABEL)
                        )
                    if not row.get("embedding_timing_lane"):
                        row["embedding_timing_lane"] = embedding_timing_lane(
                            row.get("embedding_engine"),
                            row.get("embedding_model"),
                        )
                    row["timing_formula_lane"] = timing_formula_lane(row)
                    if not row.get("batch_seconds"):
                        row["batch_seconds"] = [
                            round(float(batch.get("batch_elapsed_seconds") or 0), 3)
                            for batch in summary.get("api_embedding_update_batches") or []
                            if float(batch.get("batch_elapsed_seconds") or 0) > 0
                        ]
            except (OSError, StopIteration, TypeError, ValueError, json.JSONDecodeError):
                pass
        hydrated.append(row)
    return hydrated


def timing_stage_prior_seconds(features, stage, history=None):
    """Return a guarded measured prior for a named completed pipeline phase."""
    rows_by_key = {
        str(row.get("run_key") or ""): row
        for row in (history or hydrated_timing_model_history())
        if timing_model_learning_observation_usable(row)
    }
    samples = []
    for event in _read_timing_jsonl(TIMING_MODEL_EVENTS_PATH, limit=5000):
        if str(event.get("event") or "") != "phase_completed":
            continue
        if str(event.get("stage") or "") != str(stage):
            continue
        row = rows_by_key.get(str(event.get("run_key") or ""))
        if not row:
            continue
        if (
            str(row.get("mode") or "") != str(features.get("mode") or "")
            or str(row.get("native_upload_scope") or "") != str(features.get("native_upload_scope") or "")
            or str(row.get("embedding_timing_lane") or "") != str(features.get("embedding_timing_lane") or "")
        ):
            continue
        elapsed = float(event.get("phase_elapsed_seconds") or 0)
        if elapsed > 0:
            samples.append(elapsed)
    if not samples:
        return 0.0, 0, "conservative unmeasured phase prior"
    return min(900.0, max(2.0, _timing_percentile(samples, .75) * 1.08)), len(samples), "measured matching stage prior"


def estimate_automatic_run(
    files,
    mode,
    native_upload_scope,
    *,
    segment_mode="",
    chunk_size=0,
    chunk_overlap=0,
    target_passage_length=0,
    backend_mode="Automatic",
    unstructured_strategy="auto",
    api_url="",
    inherit_anythingllm_settings=None,
    local_check_mode="",
    profile_document_limit=None,
    ocr_preflight_manifest=None,
):
    global LAST_TIMING_ESTIMATE
    ensure_timing_model_backfill()
    profile = (
        automatic_timing_document_profile(files)
        if profile_document_limit is None
        else automatic_timing_document_profile(files, document_limit=profile_document_limit)
    )
    effective_chunk_size = chunk_size
    effective_chunk_overlap = chunk_overlap
    if mode == MODE_NATIVE_UPLOAD_LABEL and inherit_anythingllm_settings is True:
        # The launcher passes 0/-1 to the pipeline in inherited mode; resolve
        # the active Desktop splitter now so the ETA sees the same ceiling the
        # segment harmonizer will actually enforce. Leave ``None`` untouched
        # for lightweight UI refreshes that only know their visible controls.
        effective_chunk_size = current_anythingllm_chunk_size_value()
        effective_chunk_overlap = current_anythingllm_chunk_overlap_value()
    simulation_engine, simulation_model = timing_local_simulation_identity(local_check_mode)
    features = timing_model_features(
        profile, mode, native_upload_scope,
        segment_mode=segment_mode, chunk_size=effective_chunk_size, chunk_overlap=effective_chunk_overlap,
        target_passage_length=target_passage_length,
        backend_mode=backend_mode, unstructured_strategy=unstructured_strategy,
        native_upload_transport=timing_native_upload_transport(mode, api_url),
        native_upload_representation=(
            "page_parents"
            if mode == MODE_NATIVE_UPLOAD_LABEL
            and is_page_preserving_segment_mode(segment_mode)
            else "segments"
        ),
        simulation_engine=simulation_engine,
        simulation_model=simulation_model,
    )
    manifest = ocr_preflight_manifest or {}
    likely_pages = max(0, int(manifest.get("likely_pages") or 0))
    features["ocr_preflight_likely_files"] = len(manifest.get("likely_files") or [])
    features["ocr_preflight_possible_files"] = len(manifest.get("possible_files") or [])
    features["ocr_preflight_likely_pages"] = likely_pages
    features["ocr_preflight_runtime_status"] = str(
        (manifest.get("runtime") or {}).get("status") or "not_checked"
    )
    history = hydrated_timing_model_history()
    inspection_prior, inspection_samples, inspection_source = timing_stage_prior_seconds(
        features,
        "anythingllm_batch_read_only_inspection",
        history,
    )
    if inspection_prior > 0:
        features["batch_inspection_seconds_prior"] = round(inspection_prior, 3)
    features["batch_inspection_prior_samples"] = inspection_samples
    features["batch_inspection_prior_source"] = inspection_source
    batch_prior, batch_samples, batch_source = timing_model_batch_prior_seconds(features, history)
    features["batch_seconds_prior"] = round(batch_prior, 3)
    features["batch_prior_source"] = batch_source
    features["batch_prior_samples"] = batch_samples
    local_embedding_prior, local_embedding_samples, local_embedding_source = timing_local_embedding_prior(features, history)
    if local_embedding_prior > 0:
        features["local_embedding_seconds_per_record"] = round(local_embedding_prior, 3)
    features["local_embedding_prior_samples"] = local_embedding_samples
    features["local_embedding_prior_source"] = local_embedding_source
    probe_observation_prior, probe_observation_samples, probe_observation_source = timing_native_probe_observation_prior(features, history)
    if probe_observation_prior > 0:
        features["probe_observation_seconds"] = round(probe_observation_prior, 3)
    features["probe_observation_prior_samples"] = probe_observation_samples
    features["probe_observation_prior_source"] = probe_observation_source
    base = timing_model_base_seconds(features, batch_seconds_prior=batch_prior)
    comparable = sorted(
        ((timing_model_similarity(features, row), row) for row in history if timing_model_learning_observation_usable(row)),
        key=lambda item: item[0], reverse=True,
    )[:8]
    ratios = []
    active_ratio_count = 0
    legacy_ratio_count = 0
    for score, row in comparable:
        # A timing correction must agree on the upload plan and segmentation
        # family and come from a run with a real sampled document profile.
        # Backfilled summaries are useful archival evidence, but do not carry
        # the original pre-run difficulty profile and therefore cannot safely
        # calibrate a new ETA.
        if (
            score < 17
            or str(row.get("source") or "") != "automatic-run"
            # The two modes share extraction code but not their dominant cost:
            # local preparation is file/report work, while native upload is an
            # external embedding queue.  A local run must never lower an
            # upload ETA, even when page and segmentation features match.
            or str(row.get("mode") or "") != str(features.get("mode") or "")
            or str(row.get("native_upload_scope") or "") != str(features.get("native_upload_scope") or "")
            or str(row.get("native_upload_transport") or "") != str(features.get("native_upload_transport") or "")
            or str(row.get("segment_mode") or "") != str(features.get("segment_mode") or "")
            # Similar-looking local files can be much slower under a different
            # Ollama model.  Exact local lanes stay separate; cloud lanes are
            # already intentionally pooled at the provider level.
            or str(row.get("embedding_timing_lane") or "") != str(features.get("embedding_timing_lane") or "")
            or int(row.get("effective_segment_target") or row.get("target_passage_length") or 0) != int(features.get("effective_segment_target") or features.get("target_passage_length") or 0)
            or int(row.get("chunk_size") or 0) != int(features.get("chunk_size") or 0)
            or int(row.get("chunk_overlap") or 0) != int(features.get("chunk_overlap") or 0)
        ):
            continue
        historical_batch_prior, _, _ = timing_model_batch_prior_seconds(row, history)
        historical_base = timing_model_base_seconds(row, batch_seconds_prior=historical_batch_prior)
        if historical_base > 0:
            ratio = float(row["actual_seconds"]) / historical_base
            # Active-window timing is stronger evidence. A guarded legacy
            # record still teaches broad scale, but receives half influence
            # and cannot swing a prediction by itself.
            if timing_model_observation_usable(row):
                ratios.extend([ratio, ratio])
                active_ratio_count += 1
            else:
                ratios.append(ratio)
                legacy_ratio_count += 1
    learned_multiplier = _timing_percentile(ratios, .75)
    # Do not let one optimistic or anomalous run dominate an ETA. Learning is
    # intentionally conservative and becomes more influential with evidence.
    if learned_multiplier is not None:
        confidence = min(.65, len(ratios) / 8.0)
        learned_floor = 1.0 if mode == MODE_NATIVE_UPLOAD_LABEL else .70
        multiplier = (1.0 - confidence) + confidence * min(1.75, max(learned_floor, learned_multiplier))
        source = (
            f"phase-calibrated from {active_ratio_count} active-window and "
            f"{legacy_ratio_count} guarded legacy comparable run(s); {batch_source}"
        )
    else:
        multiplier = 1.0
        source = f"conservative first-run formula; {batch_source}"
    if profile.get("profile_sampling_ratio", 1) > 1:
        source += f"; initial profile sampled {profile.get('profiled_documents')} of {profile.get('documents')} PDFs"
    minimum_expected = 60 if mode == MODE_NATIVE_UPLOAD_LABEL else 8
    expected = max(minimum_expected, int(math.ceil(base * multiplier)))
    lower = max(5, int(expected * .78))
    ocr_uncertainty = likely_pages * (4 if mode == MODE_NATIVE_UPLOAD_LABEL else 2)
    upper = max(
        lower + (20 if mode == MODE_NATIVE_UPLOAD_LABEL else 8),
        int(expected * 1.35) + ocr_uncertainty,
    )
    document_term = (
        f"shared setup/finish + {features['document_count']} PDF preparation paths"
        if int(features.get("document_count") or 1) > 1
        else "one PDF preparation path"
    )
    formula = (
        f"{base:.0f}s base = {document_term} + extraction/profile + "
          + (f"batch inspection ({inspection_source}) + raw upload + {features['estimated_batches']} batches × {batch_prior:.1f}s ({batch_source}) + final verification ({probe_observation_source})" if mode == MODE_NATIVE_UPLOAD_LABEL else f"local packaging + selected embedder ({local_embedding_source})")
        + f"; × {multiplier:.2f} learned conservative multiplier = {expected}s"
    )
    comparable_count = active_ratio_count + legacy_ratio_count
    confidence_label = (
        "high confidence" if comparable_count >= 6
        else "medium confidence" if comparable_count >= 3
        else "low confidence"
    )
    result = {
        "page_count": features["page_count"],
        "expected_seconds": expected,
        "range": f"{format_estimate_clock(lower)} - {format_estimate_clock(upper)}",
        "source": source,
        "formula": formula,
        "features": features,
        "profile": profile,
        "comparable_runs": comparable_count,
        "confidence_label": confidence_label,
    }
    LAST_TIMING_ESTIMATE = dict(result)
    return result


def timing_model_html(estimate=None):
    """Show the ETA method and its durable audit locations without cluttering the run confirmation."""
    ensure_timing_model_backfill()
    rows = _read_timing_jsonl(TIMING_MODEL_RUNS_PATH)
    last = rows[-1] if rows else {}
    estimate = estimate or LAST_TIMING_ESTIMATE or {}
    formula = estimate.get("formula") or (
        "Before a PDF is selected: base phase durations are combined with a conservative multiplier from comparable completed local runs."
    )
    profile = estimate.get("profile") or {}
    features = estimate.get("features") or {}
    detail = []
    if profile:
        detail.append(
            f"Profile: {features.get('page_count', 0)} pages; {profile.get('text_density_bucket', 'unknown')} text density; "
            f"{profile.get('layout_bucket', 'unknown')} layout; OCR risk {profile.get('ocr_risk_bucket', 'unknown')}."
        )
    detail.append(f"Completed timing records: {len(rows)}. Latest recorded actual duration: {format_run_duration(last.get('actual_seconds', 0)) if last else 'none'}.")
    return (
        '<div class="metadata-summary"><section class="metadata-file">'
        '<div class="metadata-file-name">ETA formula and learning data</div>'
        f'<div class="metadata-status">{html.escape(formula)}</div>'
        f'<div class="metadata-status">{html.escape(" ".join(detail))}</div>'
        f'<div class="metadata-status">Inspectable append-only logs: <code>{html.escape(str(TIMING_MODEL_RUNS_PATH))}</code> '
        f'and <code>{html.escape(str(TIMING_MODEL_EVENTS_PATH))}</code>.</div>'
        '</section></div>'
    )


def record_timing_model_run(
    run_root,
    summaries,
    completion,
    settings,
    actual_seconds,
    *,
    wall_clock_seconds=None,
):
    """Store one terminal timing observation and a compact batch-duration audit."""
    try:
        estimate = dict((settings or {}).get("timing_estimate") or {})
        features = dict(estimate.get("features") or {})
        profile = dict(estimate.get("profile") or {})
        batches = []
        actual_records = 0
        for summary in summaries or []:
            actual_records += int(summary.get("api_embedding_update_requested") or summary.get("segments") or 0)
            for batch in summary.get("api_embedding_update_batches") or []:
                batches.append({
                    "batch": int(batch.get("batch") or 0),
                    "records": int(batch.get("requested") or 0),
                    "submission_seconds": round(float(batch.get("submission_seconds") or 0), 3),
                    "verification_seconds": round(float(batch.get("verification_seconds") or 0), 3),
                    "elapsed_seconds": round(float(batch.get("batch_elapsed_seconds") or 0), 3),
                    "state": str(batch.get("submission_state") or "unknown"),
                    "searchability_proven": bool(batch.get("searchability_proven")),
                })
        if not profile.get("page_count") or float(actual_seconds or 0) < 5.0:
            # Test fixtures and rejected preflight attempts are useful in their
            # own run summaries, but must never distort a user's future ETA.
            return {}
        latest_summary = (summaries or [{}])[-1]
        configured_documents = list((settings or {}).get("source_documents") or [])
        document_timing = []
        for index, document_summary in enumerate(summaries or []):
            configured = configured_documents[index] if index < len(configured_documents) else {}
            document_timing.append({
                # Keep only the filename in the central local timing model;
                # absolute source paths are not useful for calibration and can
                # expose a user's folder structure. The run artifact retains
                # the full provenance separately when requested.
                "filename": Path(str(configured.get("path") or "")).name,
                "pages": int(document_summary.get("pdf_page_count") or configured.get("pages") or 0),
                "records": int(document_summary.get("api_embedding_update_requested") or document_summary.get("segments") or 0),
                "phase_timing": dict(document_summary.get("phase_timing") or {}),
                "total_pipeline_seconds": round(float(document_summary.get("total_pipeline_seconds") or 0.0), 3),
            })
        if str(features.get("mode") or "") == MODE_NATIVE_UPLOAD_LABEL:
            features["embedding_engine"] = str(latest_summary.get("anythingllm_embedding_engine") or features.get("embedding_engine") or "unknown")
            features["embedding_model"] = str(latest_summary.get("anythingllm_embedding_model") or features.get("embedding_model") or "unknown")
        else:
            # Local-output timing belongs to the selected retrieval simulation
            # model, not to whatever AnythingLLM happens to be configured to
            # use in the background.
            features["embedding_engine"] = str(features.get("embedding_engine") or "unknown")
            features["embedding_model"] = str(features.get("embedding_model") or "unknown")
        features["embedding_timing_lane"] = embedding_timing_lane(
            features["embedding_engine"], features["embedding_model"]
        )
        features["timing_formula_lane"] = timing_formula_lane(features)
        features["embedding_batch_size"] = int(
            latest_summary.get("api_embedding_update_batch_size")
            or features.get("embedding_batch_size")
            or ANYTHINGLLM_EMBEDDING_UPDATE_BATCH_SIZE
        )
        features["embedding_verification_mode"] = str(
            latest_summary.get("api_embedding_verification_mode") or "not_applicable"
        )
        features["embedding_verification_interval"] = int(
            latest_summary.get("api_embedding_verification_interval") or 0
        )
        features["anythingllm_config_batch_size"] = int(
            latest_summary.get("anythingllm_embedding_batch_size")
            or features.get("anythingllm_config_batch_size")
            or 0
        )
        # Preserve the requested backend as a pre-run comparison feature and
        # store the selected backend separately as an observed outcome.
        features["selected_backend"] = str(latest_summary.get("selected_backend") or "unknown")
        features["ocr_used"] = bool(latest_summary.get("ocr_assisted_extraction_used"))
        row = {
            "schema_version": TIMING_MODEL_VERSION,
            "recorded_at": datetime.now().isoformat(timespec="seconds"),
            "run_key": str(run_root),
            "source": "automatic-run",
            "state": str((completion or {}).get("state") or "unknown"),
            "actual_seconds": round(float(actual_seconds or 0), 3),
            "wall_clock_seconds": round(float(wall_clock_seconds or actual_seconds or 0), 3),
            "duration_provenance": "active_observation_window",
            "expected_seconds": int(estimate.get("expected_seconds") or 0),
            "estimate_source": estimate.get("source", ""),
            "estimate_formula": estimate.get("formula", ""),
            "actual_records": actual_records,
            "actual_batches": len(batches),
            "batch_seconds": [row["elapsed_seconds"] for row in batches if row["elapsed_seconds"] > 0],
            "batch_submission_seconds": [row["submission_seconds"] for row in batches if row["submission_seconds"] > 0],
            "batch_verification_seconds": [row["verification_seconds"] for row in batches if row["verification_seconds"] > 0],
            "batch_measurements": batches,
            "document_timing": document_timing,
            "profile": profile,
            **features,
        }
        _append_timing_jsonl(TIMING_MODEL_RUNS_PATH, row)
        summary = {
            "schema_version": TIMING_MODEL_VERSION,
            "updated_at": row["recorded_at"],
            "run_count": len(_read_timing_jsonl(TIMING_MODEL_RUNS_PATH, limit=1000)),
            "latest_run_key": row["run_key"],
            "latest_actual_seconds": row["actual_seconds"],
            "formula": row["estimate_formula"],
        }
        TIMING_MODEL_DIR.mkdir(parents=True, exist_ok=True)
        TIMING_MODEL_SUMMARY_PATH.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return row
    except (OSError, TypeError, ValueError) as exc:
        APP_LOGGER.warning("could not record terminal timing model row: %s", exc)
        return {}


def record_timing_model_event(run_root, stage, batch_report=None):
    """Persist batch evidence for ETA learning and this run's own timeline."""
    batch = batch_report or {}
    event = {
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "run_key": str(run_root),
        "run_id": str(batch.get("run_id") or ""),
        "correlation_id": str(batch.get("correlation_id") or ""),
        "stage": str(stage or "Working"),
        "event": str(batch.get("timing_event") or "status"),
        "batch": int(batch.get("batch") or 0),
        "total_batches": int(batch.get("total_batches") or 0),
        "records": int(batch.get("requested") or 0),
        "submission_seconds": round(float(batch.get("submission_seconds") or 0), 3),
        "verification_seconds": round(float(batch.get("verification_seconds") or 0), 3),
        "batch_elapsed_seconds": round(float(batch.get("batch_elapsed_seconds") or 0), 3),
        "phase_elapsed_seconds": round(float(batch.get("phase_elapsed_seconds") or 0), 3),
        "desktop_queue_current": int(batch.get("desktop_queue_current") or batch.get("desktop_current_record") or 0),
        "desktop_queue_completed": int(batch.get("desktop_queue_completed") or 0),
        "desktop_queue_total": int(batch.get("queue_records") or 0),
        "desktop_queue_records_per_minute": batch.get("desktop_queue_records_per_minute"),
        "desktop_queue_estimated_remaining_seconds": batch.get("desktop_queue_estimated_remaining_seconds"),
        "desktop_queue_observer_state": str(batch.get("desktop_queue_observer_state") or ""),
        "submission_state": str(batch.get("submission_state") or ""),
        "backend": str(batch.get("backend") or ""),
        "candidate_success": bool(batch.get("candidate_success")),
    }
    _append_timing_jsonl(TIMING_MODEL_EVENTS_PATH, event)
    # The aggregate model is useful for future estimates, but it forces an
    # operator investigating one slow PDF to sift through unrelated runs.
    # Keep a parallel, privacy-minimal timeline beside that run's artifacts.
    # It contains counters and timings only—never PDF text, API keys, or SSE
    # payload content—and a write failure must never affect the run itself.
    try:
        timeline_path = Path(run_root) / "timing-evidence-timeline.jsonl"
        timeline_path.parent.mkdir(parents=True, exist_ok=True)
        with timeline_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    except (OSError, TypeError, ValueError) as exc:
        APP_LOGGER.warning("could not persist run timing timeline: %s", exc)


def refresh_automatic_run_estimate(
    pdf_files,
    folder_pdf_files,
    mode,
    native_upload_scope,
    _workspace_slug="",
    segment_mode="",
    target_passage_length=0,
    anythingllm_chunk_size=0,
    anythingllm_chunk_overlap=0,
    backend_mode="Automatic",
    unstructured_strategy="auto",
    local_check_mode="",
    folder_manifest=None,
    api_url="",
    inherit_anythingllm_settings=None,
    *,
    profile_document_limit=None,
):
    """Refresh the visible estimate without starting, uploading, or extracting.

    The estimate depends directly on the selected readable PDFs, output mode,
    native-upload scope, local segmentation target, and native splitter
    settings, so the visible value cannot silently lag a setting change.
    """
    if str((LIVE_AUTOMATIC_RUN_STATUS or {}).get("state") or "") in {
        "preparing", "running", "successful", "warning", "failed", "cancelled",
    }:
        # The active or terminal run owns the visible timer. A late selection
        # or settings callback can be delivered after completion and used to
        # alternate the factual ``Compl`` duration with the old ready-state
        # ETA. A genuine new selection first calls
        # ``reset_automatic_run_presentation``, which clears this record and
        # then permits a fresh estimate.
        return gr.update()
    folder_candidates, manifest_reused = folder_manifest_candidates(folder_pdf_files, folder_manifest)
    direct_inputs = normalize_file_list(pdf_files)
    if manifest_reused:
        if direct_inputs:
            validated_direct, validation_report = validate_pdf_inputs(direct_inputs)
            if validation_report:
                return automatic_run_timing_html(state="ready")
            files = list(dict.fromkeys((validated_direct or []) + folder_candidates))
        else:
            files = folder_candidates
            validation_report = None
    else:
        files, validation_report = validate_pdf_inputs(
            list(dict.fromkeys(direct_inputs + folder_candidates))
        )
    if validation_report:
        return automatic_run_timing_html(state="ready")
    effective_profile_document_limit = (
        automatic_timing_profile_document_limit(files)
        if profile_document_limit is None
        else profile_document_limit
    )
    estimate = estimate_automatic_run(
        files,
        mode,
        native_upload_scope,
        segment_mode=segment_mode,
        chunk_size=anythingllm_chunk_size,
        chunk_overlap=anythingllm_chunk_overlap,
        target_passage_length=target_passage_length,
        backend_mode=backend_mode,
        unstructured_strategy=unstructured_strategy,
        api_url=api_url,
        inherit_anythingllm_settings=inherit_anythingllm_settings,
        local_check_mode=local_check_mode,
        profile_document_limit=effective_profile_document_limit,
    )
    return automatic_run_timing_html(
        estimate["expected_seconds"],
        estimate["source"],
        state="ready",
    )


def automatic_completion(summaries, prepare_and_upload):
    if any(summary.get("app_error_code") for summary in summaries):
        return {"state": "failed", "message": "Preparation failed; inspect the generated failure report."}
    if not prepare_and_upload:
        return {"state": "successful", "message": "Local preparation and checks completed successfully."}
    ocr_withheld = [
        summary for summary in summaries
        if summary.get("api_upload_status") == "skipped_needs_ocr_review"
    ]
    if ocr_withheld:
        names = ", ".join(Path(str(summary.get("pdf") or "document")).name for summary in ocr_withheld[:3])
        suffix = "" if len(ocr_withheld) <= 3 else f" (+{len(ocr_withheld) - 3} more)"
        first_warning = str(ocr_withheld[0].get("api_upload_warning") or "").strip()
        photographed_spread_hold = "photographed spread" in first_warning.casefold()
        warning_detail = first_warning.removeprefix(
            "AnythingLLM upload was withheld because "
        ).removeprefix("AnythingLLM upload was withheld ").strip()
        return {
            "state": "warning",
            "code": (
                "AUTO-LAYOUT-REVIEW-001"
                if photographed_spread_hold
                else "AUTO-OCR-REVIEW-001"
            ),
            "message": (
                f"Local preparation completed, but AnythingLLM upload was withheld for {names}{suffix} "
                + (
                    f"because {warning_detail[0].lower() + warning_detail[1:]} "
                    "Review the saved readiness report."
                    if warning_detail
                    else "because reliable OCR is required and unavailable. "
                    "Review the saved OCR/readiness report."
                )
            ),
        }
    credential_reverification = [
        summary for summary in summaries
        if summary.get("anythingllm_embedder_warning_code") == "AUTO-OPENROUTER-KEY-REVERIFY-001"
    ]
    if credential_reverification:
        return {
            "state": "failed",
            "code": "AUTO-OPENROUTER-KEY-REVERIFY-001",
            "message": "OpenRouter rejected the embedding key. Update it in AnythingLLM Settings, then retry.",
        }
    post_statuses = [str(summary.get("post_upload_verification_status") or "") for summary in summaries]
    post_ok = all(status == "pass" for status in post_statuses)
    post_searchable_with_caveat = all(status in REVIEWABLE_POST_UPLOAD_STATUSES for status in post_statuses)
    runtime_statuses = [str(summary.get("anythingllm_runtime_validation_status") or "") for summary in summaries]
    # Stored vectors and a live runtime probe are separate claims. Exact
    # page-parent vector evidence is sufficient to complete an upload. A
    # timed-out optional probe is retained for diagnostics, but must never
    # make already-searchable documents unusable or block the run.
    runtime_ok = all(status == "pass" for status in runtime_statuses)
    runtime_transient_timeout = all(
        status in {
            "pass",
            "chat_runtime_timeout",
            "vector_runtime_timeout",
            "pass_with_chat_timeout",
            "pass_with_vector_timeout",
        }
        for status in runtime_statuses
    )
    upload_ok = all(summary.get("api_upload_status") in {"complete", "complete_with_key_cleanup_warning"} for summary in summaries)
    if upload_ok and post_ok and runtime_ok:
        # Storage, vector search, and chat are proven independently below.
        # Desktop's visible workspace list is a fourth concern: it needs the
        # guarded local bridge after a mutation.  Never quietly imply that a
        # green retrieval result refreshed the Desktop window when that bridge
        # is absent (for example, after an AnythingLLM restart or update).
        return {
            "state": "successful",
            "message": (
                "Ready for retrieval: exact vector storage and runtime retrieval checks succeeded. "
                "Documents drawer visibility is reported separately."
            ),
        }
    if upload_ok and post_searchable_with_caveat and runtime_ok:
        if any(status == "verified_unavailable" for status in post_statuses):
            return {
                "state": "warning",
                "message": "Upload and runtime retrieval succeeded, but live storage evidence was unavailable. A verification-only recovery task was recorded; no document was re-uploaded.",
            }
        if any(status == "concurrent_write_ambiguous" for status in post_statuses):
            return {
                "state": "warning",
                "message": "Upload and runtime retrieval succeeded, but AnythingLLM was still writing during final storage verification. Prepared files remain available; retry the saved verification only after Desktop becomes idle.",
            }
        if any(status == "review" for status in post_statuses):
            return {
                "state": "warning",
                "message": "Documents, vectors, and runtime retrieval succeeded, but page/segment metadata visibility needs review. Prepared files remain available; retrieval is not withheld.",
            }
        return {
            "state": "warning",
            "message": "Searchable vectors and runtime retrieval succeeded, but the final storage observation could not confirm workspace document-list rows for one or more uploads. Retrieval evidence remains valid; inspect document management separately.",
        }
    if upload_ok and post_searchable_with_caveat and runtime_transient_timeout:
        return {
            "state": "successful",
            "code": "AUTO-RETRIEVAL-RUNTIME-001",
            "message": "Searchable page-parent vectors verified. An optional live retrieval probe timed out and was saved for diagnostics; it does not block use of this workspace.",
        }
    if upload_ok and post_searchable_with_caveat and any(
        status == "blocked_provider_authentication" for status in runtime_statuses
    ):
        return {
            "state": "warning",
            "code": "AUTO-RETRIEVAL-AUTH-001",
            "message": "Stored, but retrieval is unavailable: the embedding credential was rejected. Fix the key, then retry validation.",
        }
    if upload_ok and post_searchable_with_caveat and any(
        status == "vector_retrieval_failed" for status in runtime_statuses
    ):
        return {
            "state": "warning",
            "code": "AUTO-RETRIEVAL-VERIFY-001",
            "message": "Stored, but retrieval failed its live vector check. This workspace is not ready for retrieval.",
        }
    if upload_ok and post_searchable_with_caveat:
        if any(status == "chat_citation_failed" for status in runtime_statuses):
            return {
                "state": "warning",
                "code": "AUTO-RETRIEVAL-CHAT-001",
                "message": "Stored, but chat retrieval did not confirm the expected source. Review the runtime report before using this workspace.",
            }
        return {
            "state": "warning",
            "code": "AUTO-RETRIEVAL-UNVERIFIED-001",
            "message": "Stored, but retrieval was not verified. Open the runtime report before using this workspace.",
        }
    partial = [summary for summary in summaries if str(summary.get("post_upload_verification_status") or "") == "partial_vector_coverage"]
    if partial:
        first = partial[0]
        observed = int(first.get("post_upload_matching_vectors") or 0)
        expected = int(first.get("post_upload_expected_payloads") or 0)
        remaining = max(0, expected - observed)
        representation = str(first.get("native_upload_representation") or "").casefold()
        record_label = "page-parent vectors" if representation == "page_parents" else "segment vectors"
        coverage = f" ({observed / expected:.0%} confirmed)" if expected else ""
        counts = (
            f"{observed} of {expected} planned {record_label} were confirmed searchable{coverage}"
            if expected else "Only part of the prepared document was indexed"
        )
        recovery = f"; {remaining} remain unconfirmed and require reconciliation before any resubmission" if expected else ""
        cap_classification = str(first.get("post_upload_reconciliation_cap_classification") or "")
        cap_detail = {
            "reconciliation_cap_partial_vector_progress": " Exact-vector progress was still arriving during the shared 480-second observation window.",
            "reconciliation_cap_queue_heartbeat": " Desktop was still emitting queue activity near the shared 480-second observation cap.",
            "reconciliation_cap_storage_busy": " Local storage was busy during the shared 480-second observation window.",
            "reconciliation_cap_no_new_evidence": " No new Desktop or exact-vector evidence arrived before the shared 480-second observation cap.",
        }.get(cap_classification, "")
        return {
            "state": "failed",
            "code": "AUTO-EMBEDDING-PARTIAL-001",
            "message": f"AnythingLLM indexing stalled: {counts}{recovery}.{cap_detail} The original Desktop queue was not replayed automatically because its remaining outcome is ambiguous; the saved recovery manifest limits any later resume to this run's exact missing records.",
        }
    reconciliation_pending = [
        summary for summary in summaries
        if summary.get("api_upload_status") == "reconciliation_pending"
    ]
    if reconciliation_pending:
        return {
            "state": "warning",
            "code": "AUTO-EMBEDDING-RECONCILE-001",
            "message": (
                "Local preparation is complete: prepared text and segments complete, but please check AnythingLLM. "
                "AnythingLLM attached the submitted document, but complete vector evidence was not observed "
                "within the bounded reconciliation window. The outcome remains unknown, not rejected; "
                "use the saved recovery manifest to reconcile late vectors before submitting only confirmed-missing records."
            ),
        }
    failed_uploads = [summary for summary in summaries if summary.get("api_upload_status") not in {"complete", "complete_with_key_cleanup_warning"}]
    if failed_uploads:
        first = failed_uploads[0]
        upload_error = str(
            first.get("api_upload_error")
            or ((first.get("api_upload_report") or {}).get("error") if isinstance(first.get("api_upload_report"), dict) else "")
            or "AnythingLLM did not confirm the embedding submission."
        )
        upload_error_classification = str(first.get("api_upload_error_classification") or "")
        if (
            "timed out" in upload_error.casefold()
            or "timeout" in upload_error.casefold()
            or upload_error_classification in {
                "client_timeout_submission_unknown",
                "client_transport_submission_unknown",
            }
        ):
            return {
                # A client deadline followed by a bounded, inconclusive
                # observation window is not an embedding rejection. Keep the
                # run visibly actionable without pinning the UI to a false
                # failure state; the durable ledger records the exact next
                # reconciliation action.
                "state": "warning",
                "code": "AUTO-EMBEDDING-RECONCILE-001",
                "message": (
                    "Local preparation is complete: prepared text and segments complete, but please check AnythingLLM. "
                    "AnythingLLM did not return before the client deadline and no complete exact-vector proof was observed "
                    "within the bounded reconciliation window. The outcome remains unknown, not rejected; use the saved "
                    "recovery manifest to reconcile late vectors before submitting only confirmed-missing records."
                ),
            }
        return {
            "state": "failed",
            "code": "AUTO-EMBEDDING-SUBMIT-001",
            "message": f"AnythingLLM embedding submission failed before any partial searchable coverage could be confirmed: {upload_error}",
        }
    return {
        "state": "failed",
        "code": "AUTO-EMBEDDING-VERIFY-001",
        "message": "AnythingLLM accepted the submission, but no complete searchable-vector evidence was observed in the target workspace.",
    }


def automatic_completion_phase(completion, prepare_and_upload):
    """Choose a terminal phase that makes only evidence-backed claims.

    A warning can mean successful searchable vectors with a document-list
    caveat, but it can also mean that OCR withheld submission entirely. Those
    outcomes must never share a "vectors verified" label.
    """
    completion = completion if isinstance(completion, dict) else {}
    state = str(completion.get("state") or "")
    code = str(completion.get("code") or "")
    if state == "cancelled":
        return "Processing stopped by operator"
    if state == "successful" and prepare_and_upload:
        return "Ready for retrieval"
    if code == "AUTO-EMBEDDING-RECONCILE-001":
        return "Preparation complete — AnythingLLM verification pending"
    if code == "AUTO-OCR-REVIEW-001":
        return "Local preparation complete — upload withheld for OCR review"
    if code == "AUTO-LAYOUT-REVIEW-001":
        return "Local preparation complete — upload withheld for layout review"
    if state == "warning" and prepare_and_upload:
        return "Searchable vectors verified; document-list observation needs review"
    if state == "successful":
        return "Local preparation complete"
    return "Run needs attention"


def automatic_batch_diagnostics_required(
    summaries,
    prepare_and_upload,
    *,
    retain_detailed_evidence=False,
    cancellation_requested=False,
):
    """Keep broad storage inspection out of the ordinary successful path.

    Exact submitted-record, vector, and provenance-matched runtime evidence is
    enough for normal completion. A broad storage inspection is valuable for a
    mismatch or an explicit diagnostic request, but it must not extend every
    successful upload with another potentially slow workspace scan.
    """
    if cancellation_requested or not summaries:
        return False
    # Detailed per-run artifacts are retained independently.  A global scan of
    # every LanceDB table is a diagnostic for an evidence mismatch, not another
    # completion requirement for a document whose exact page-parent and
    # retrieval checks already succeeded.  Explicit deep audit remains a
    # separate Workspace-maintenance action.
    return automatic_completion(summaries, prepare_and_upload).get("state") != "successful"


def automatic_completion_button_state(completion):
    """Render terminal state as evidence, never as an implicit retry action.

    The visible Confirm button owns the only full-pipeline click binding. It
    used to stay interactive after warning/success completion, so a click on
    the label ``Completed — review upload checks`` silently re-entered PDF
    preparation and could repeat an already-completed upload. Verification
    and narrow recovery remain explicit Workspace-maintenance actions; this
    terminal control deliberately cannot mutate AnythingLLM.
    """
    state = (completion or {}).get("state")
    if state == "successful":
        return gr.update(value="Processing successful ✓", interactive=False, variant="huggingface")
    if state == "warning":
        if (completion or {}).get("code") == "AUTO-EMBEDDING-RECONCILE-001":
            return gr.update(value="Preparation complete — check AnythingLLM", interactive=False, variant="secondary")
        return gr.update(value="Completed — upload checks need review", interactive=False, variant="secondary")
    if state == "cancelled":
        return gr.update(value="Processing stopped", interactive=False, variant="secondary")
    return gr.update(value="Processing failed — review report", interactive=False, variant="stop")


def automatic_confirmation_html(settings):
    mode = settings["mode"]
    if mode != MODE_NATIVE_UPLOAD_LABEL:
        values = [
            mode,
            (
                "Flat text-only export (no logs)"
                if mode == MODE_LOCAL_NO_LOGS_LABEL
                else "Local output only"
            ),
            settings["segment_mode"],
            f"{settings['target_passage_length']} character target",
        ]
        summary = '<div class="automatic-confirmation-summary">' + html.escape(" - ".join(str(value) for value in values)) + "</div>"
        return summary + automatic_ocr_preflight_html(settings.get("ocr_preflight_manifest"))
    workspace_label = (
        f"New workspace for this document: {lancedb_safe_workspace_name(settings.get('new_workspace_name') or document_workspace_name(settings.get('document_label'), settings.get('files') or settings.get('pdf_files')))}"
        if is_new_document_workspace_choice(settings["workspace_slug"])
        else settings["workspace_slug"] or "Not selected"
    )
    values = [
        mode,
        workspace_label,
        settings["native_upload_scope"] if mode == MODE_NATIVE_UPLOAD_LABEL else "Local files only",
        settings["segment_mode"],
        f"{settings['anythingllm_chunk_size']} chunk / {settings['anythingllm_chunk_overlap']} overlap",
    ]
    summary = '<div class="automatic-confirmation-summary">' + html.escape(" - ".join(str(value) for value in values)) + "</div>"
    prediction = native_boundary_prediction_html(settings)
    return summary + prediction + automatic_ocr_preflight_html(settings.get("ocr_preflight_manifest"))


def native_boundary_prediction_html(settings):
    """State the expected splitter boundary before any upload is started."""
    if settings.get("mode") != MODE_NATIVE_UPLOAD_LABEL:
        return ""
    plan = target_passage_sizing_plan(
        settings.get("segment_mode"),
        TARGET_PASSAGE_CUSTOM_LABEL,
        settings.get("target_passage_length"),
        settings.get("inherit_anythingllm_settings", True),
        settings.get("anythingllm_chunk_size", 0),
        settings.get("anythingllm_chunk_overlap", 0),
        page_preserve_ceiling=settings.get("page_preserve_ceiling", 0),
    )
    if not plan.get("page_preserving"):
        return ""
    splitter = int(plan.get("splitter_char_limit") or 0)
    local_ceiling = int(plan.get("page_preserve_effective_ceiling") or 0)
    overlap = int(plan.get("overlap_characters") or 0)
    statement = (
        f"<strong>Boundary prediction:</strong> prepared page-preserving records will not exceed {local_ceiling} characters; "
        f"AnythingLLM Text Chunk Size is {splitter} characters. No size-driven re-chunking is expected."
        if local_ceiling <= splitter
        else f"<strong>Boundary prediction:</strong> prepared records can exceed AnythingLLM's {splitter}-character Text Chunk Size, so re-chunking is expected."
    )
    if overlap:
        statement += f" AnythingLLM's {overlap}-character overlap remains a global provider setting."
    return f'<div class="setting-reference-note">{statement}</div>'


def automatic_ocr_preflight_html(manifest):
    """Render an actionable confirmation warning, never a hidden OCR run."""
    manifest = manifest or {}
    warnings = [str(row) for row in manifest.get("warnings") or [] if str(row).strip()]
    if not warnings:
        return ""
    state = str(manifest.get("status") or "warning")
    title = "OCR preflight needs attention" if state == "blocked" else "OCR preflight"
    rows = "".join(f"<li>{html.escape(row)}</li>" for row in warnings)
    return (
        f'<div class="automatic-ocr-preflight {html.escape(state)}" role="status">'
        f"<strong>{html.escape(title)}.</strong><ul>{rows}</ul>"
        "</div>"
    )


def automatic_action_row_updates():
    """Reset the static action bar after a cancel or completed run.

    This must never show or hide a Gradio Row. Row visibility updates are
    asynchronous layout transitions and previously produced stacked action
    bars. The retired review control stays hidden; Confirm owns the visible
    terminal state.
    """
    terminal_state = str((LIVE_AUTOMATIC_RUN_STATUS or {}).get("state") or "")
    if terminal_state in {"successful", "warning", "failed", "cancelled"}:
        return (
            gr.update(),
            automatic_completion_button_state({"state": terminal_state}),
            gr.update(value="Cancel", interactive=False),
        )
    return (
        gr.update(),
        gr.update(value="Confirm and start processing", interactive=False),
        gr.update(value="Cancel", interactive=False),
    )


def automatic_run_failure_banner_html(code, message):
    return (
        '<div class="automatic-run-failure" role="alert">'
        f'<strong>Run needs attention ({html.escape(str(code))}).</strong> '
        f'{html.escape(str(message))} Open <em>Run output and downloads</em> for the full report.'
        '</div>'
    )


def automatic_run_failure_banner_update(code, message):
    return gr.update(
        value=automatic_run_failure_banner_html(code, message),
        visible=True,
    )


def automatic_run_result_failure_banner(run_outputs):
    """Expose handled pipeline failures outside the collapsed output accordion."""
    summary_update = run_outputs[0] if isinstance(run_outputs, (tuple, list)) and run_outputs else {}
    rendered = str((summary_update or {}).get("value") or "") if isinstance(summary_update, dict) else ""
    if 'summary-status error' in rendered:
        return automatic_run_failure_banner_update(
            "AUTO-RUN-RESULT-001",
            "The run returned a failure report instead of a successful result.",
        )
    return gr.update(value="", visible=False)


def completed_native_upload_requires_desktop_refresh(run_outputs):
    """Return true only after a terminal successful/warning upload result.

    The guarded Desktop bridge is a necessary notification after a real
    AnythingLLM mutation.  It must not be called merely because the UI handler
    returned: a failed preparation/upload result has no completed state to
    surface in Desktop.  ``run_automatic`` already exposes the canonical
    terminal state in its timing update, so use that durable UI contract
    rather than inferring success from an absence of exceptions.
    """
    if not isinstance(run_outputs, (tuple, list)) or len(run_outputs) < 7:
        return False
    timing_update = run_outputs[6]
    rendered = (
        str((timing_update or {}).get("value") or "")
        if isinstance(timing_update, dict)
        else str(timing_update or "")
    )
    # The timing panel is the canonical terminal state, but older Gradio
    # updates have produced either a data attribute or only the summary-status
    # class. Accept both renderings so a completed warning run (which may have
    # attached documents before a later retrieval check failed) is still able
    # to notify the guarded Desktop bridge. Never refresh a failed run.
    return bool(re.search(
        r'(?:data-run-state|summary-status)\s*=\s*["\']?(?:successful|warning|completed)["\']?'
        r'|summary-status\s+(?:successful|warning|completed)',
        rendered,
        re.I,
    ))


def automatic_confirmation_failure_response(code, title, details, next_steps=None, timing_html=None):
    """Return the full confirm-click output contract for a known failed start."""
    APP_LOGGER.error("automatic run did not start: %s (%s)", code, title)
    record = dict(LIVE_AUTOMATIC_RUN_STATUS or {})
    if str(record.get("state") or "") == "preparing":
        root_cause = str((details or [""])[0] or "").strip()
        update_live_automatic_run_status(
            record.get("run_root"),
            state="failed",
            phase="Pre-processing needs attention",
            expected_seconds=record.get("expected_seconds", 0),
            details=(f"{code}: {title}" + (f" — {root_cause}" if root_cause else "")),
            confirmed_fraction=record.get("confirmed_fraction"),
            cancel_available=False,
        )
    return (
        *automatic_error_outputs(
            code,
            title,
            details,
            next_steps,
            timing_html=timing_html,
        ),
        gr.update(),
        automatic_run_failure_banner_update(code, title),
    )


def validated_automatic_run_settings(values):
    """Build one canonical automatic-run settings dictionary from UI values.

    Both Review and Confirm use this function.  Confirm deliberately receives
    the actual UI controls rather than an opaque ``gr.State`` value: that makes
    its request self-contained and prevents a lost client-side State update
    from turning a visible Confirm click into a no-op.
    """
    core_values = tuple(values[: len(AUTOMATIC_RUN_FIELDS)])
    settings = dict(zip(AUTOMATIC_RUN_FIELDS, core_values, strict=True))
    settings["new_workspace_name"] = (
        str(values[len(AUTOMATIC_RUN_FIELDS)] or "").strip()
        if len(values) > len(AUTOMATIC_RUN_FIELDS)
        else ""
    )
    folder_inspection = inspect_uploaded_pdf_candidates(settings["folder_pdf_files"])
    files, validation_report = validate_pdf_inputs(
        list(dict.fromkeys(normalize_file_list(settings["pdf_files"]) + folder_inspection["pdf_candidates"]))
    )
    if validation_report:
        return settings, validation_report, [], False
    settings["files"] = files
    if (
        settings["mode"] == MODE_NATIVE_UPLOAD_LABEL
        and settings["native_upload_scope"] == NATIVE_UPLOAD_SCOPE_CUSTOM_LABEL
        and len(files) != 1
    ):
        return (
            settings,
            app_error_report(
                "AUTO-NATIVE-RANGE-001",
                "Custom range is available for one PDF only",
                [
                    "Custom range applies to prepared records from one selected PDF.",
                    "For a batch, choose All segments instead.",
                ],
            ),
            [],
            False,
        )
    if (
        settings["mode"] == MODE_NATIVE_UPLOAD_LABEL
        and settings["native_upload_scope"] == NATIVE_UPLOAD_SCOPE_CUSTOM_LABEL
        and not native_upload_custom_range_supported(settings.get("segment_mode"))
    ):
        return (
            settings,
            app_error_report(
                "AUTO-NATIVE-RANGE-002",
                "Custom range requires a page-based segmentation mode",
                [
                    "Use Page - preserve automatically or Whole-page chunks.",
                    "Other segmentation modes create passage records, so 1-3 would not reliably mean PDF pages 1-3.",
                ],
            ),
            [],
            False,
        )
    ocr_preflight_manifest = automatic_ocr_preflight_manifest(
        files,
        backend_mode=settings.get("backend_mode", "Automatic"),
        unstructured_strategy=settings.get("unstructured_strategy", "auto"),
    )
    settings["ocr_preflight_manifest"] = ocr_preflight_manifest
    if settings["mode"] != MODE_NATIVE_UPLOAD_LABEL:
        # Retain the browser controls so a later switch back to upload mode
        # restores them, but remove every upload-only value from this canonical
        # run request. Direct callers receive the same safety boundary.
        settings.update({
            "api_url": "",
            "api_key": "",
            "workspace_slug": "",
            "native_upload_scope": "local_only",
            "native_metadata_mode": "not_applicable",
            "anythingllm_create_document_folders": False,
            "anythingllm_document_folder_name": "",
            "auto_apply_recommended_settings": False,
        })
    estimate = estimate_automatic_run(
        files,
        settings["mode"],
        settings["native_upload_scope"],
        segment_mode=settings.get("segment_mode", ""),
        chunk_size=settings.get("anythingllm_chunk_size", 0),
        chunk_overlap=settings.get("anythingllm_chunk_overlap", 0),
        target_passage_length=settings.get("target_passage_length", 0),
        backend_mode=settings.get("backend_mode", "Automatic"),
        unstructured_strategy=settings.get("unstructured_strategy", "auto"),
        api_url=settings.get("api_url", ""),
        inherit_anythingllm_settings=settings.get("inherit_anythingllm_settings"),
        local_check_mode=settings.get("local_check_mode", ""),
        profile_document_limit=automatic_timing_profile_document_limit(files),
        ocr_preflight_manifest=ocr_preflight_manifest,
    )
    settings["expected_seconds"] = estimate["expected_seconds"]
    settings["estimate_source"] = estimate["source"]
    # Direct callers and legacy tests may supply a deliberately minimal
    # estimate fixture. The visible evidence fields enrich real estimates but
    # must never make that compatibility path fail.
    settings["estimate_range"] = estimate.get("range", "")
    settings["estimate_confidence"] = estimate.get("confidence_label", "")
    settings["estimate_comparable_runs"] = estimate.get("comparable_runs", 0)
    settings["timing_estimate"] = estimate
    warnings = []
    if settings["mode"] == MODE_NATIVE_UPLOAD_LABEL and not (settings["workspace_slug"] or "").strip():
        warnings.append("Native upload is blocked until you select or create a workspace.")
    elif settings["mode"] == MODE_NATIVE_UPLOAD_LABEL and is_new_document_workspace_choice(settings["workspace_slug"]):
        warnings.append("A new workspace for this document will be created only after you confirm this run.")
    if settings["mode"] == MODE_NATIVE_UPLOAD_LABEL and settings["native_upload_scope"] == NATIVE_UPLOAD_SCOPE_CUSTOM_LABEL:
        try:
            settings["native_upload_indices"] = parse_native_upload_custom_range(
                settings.get("native_upload_custom_range")
            )
        except ValueError as exc:
            warnings.append(f"Custom upload range is invalid: {exc}")
    warnings.extend(ocr_preflight_manifest.get("warnings") or [])
    if (
        settings["mode"] == MODE_NATIVE_UPLOAD_LABEL
        and settings["anythingllm_chunk_overlap"] not in {0, "0", -1, "-1", None}
    ):
        warnings.append("AnythingLLM overlap can cross prepared boundaries after upload; review the native boundary policy before using page-bounded retrieval.")
    allowed = not any(
        "blocked" in warning.casefold() or "custom upload range is invalid" in warning.casefold()
        for warning in warnings
    ) and ocr_preflight_manifest.get("status") != "blocked"
    return settings, None, warnings, allowed


def automatic_mode_ui_updates(mode):
    """Keep local-only runs visually and operationally separate from upload.

    The hidden controls stay mounted and retain their values. This lets an
    operator switch back to upload mode without losing a selected workspace,
    while the canonical run request independently removes those values in
    local-only mode.
    """
    if str((LIVE_AUTOMATIC_RUN_STATUS or {}).get("state") or "") == "running":
        # Do not reconfigure mounted control rows mid-run.  The execution
        # already owns an immutable settings snapshot, and changing only the
        # visible mode would make the current progress report misleading.
        return tuple(gr.update() for _ in range(15))
    upload_enabled = mode == MODE_NATIVE_UPLOAD_LABEL
    # Keep this order paired with automatic_mode_ui_outputs in build_interface.
    # Whole Rows are updated where possible so local-only mode cannot leave an
    # empty native-settings grid behind.
    upload_updates = (
        gr.update(visible=upload_enabled),  # AnythingLLM documents root
        gr.update(visible=upload_enabled),  # native metadata accordion
        gr.update(visible=upload_enabled),  # API URL
        gr.update(visible=upload_enabled),  # API key
        gr.update(visible=upload_enabled),  # inherit setting
        gr.update(visible=upload_enabled),  # refresh button
        gr.update(visible=upload_enabled),  # live settings report
        gr.update(visible=upload_enabled),  # recommendation report
        gr.update(visible=upload_enabled),  # chunk size/overlap row
        gr.update(visible=upload_enabled),  # embedder-limit row
        gr.update(visible=upload_enabled),  # configuration actions row
        gr.update(visible=upload_enabled),  # embedder engine/model row
        gr.update(visible=upload_enabled),  # embedder save row
        gr.update(visible=upload_enabled),  # settings status
    )
    # The explanatory local-only notice was useful during development, but it
    # occupies scarce vertical space in normal use. Keep the output slot only
    # for callback compatibility; it is always visually absent.
    return (*upload_updates, gr.update(value="", visible=False))


def prepare_automatic_confirmation(*values):
    """Legacy non-mutating validator retained for direct callers/tests.

    The live UI now uses one Confirm action rather than a separate review
    screen. This helper retains the former output shape for compatibility.
    """
    if str((LIVE_AUTOMATIC_RUN_STATUS or {}).get("state") or "") == "running":
        # The browser disables Review while a run is active, but callbacks are
        # still an API boundary. A replayed request must not clear the durable
        # progress record or replace the active confirmation/action row.
        return tuple(gr.update() for _ in range(8))
    try:
        clear_live_automatic_run_status()
        settings, validation_report, _warnings, allowed = validated_automatic_run_settings(values)
        if validation_report:
            return (
                gr.update(),
                run_summary_html(validation_report),
                automatic_run_timing_html(state="failed", message="Choose a readable PDF before confirming."),
                {},
                gr.update(value="Confirm and start processing", variant="primary", interactive=False),
                gr.update(visible=False, interactive=False),
                gr.update(value="Cancel", interactive=False),
                gr.update(value="", visible=False),
            )
        return (
            gr.update(),
            automatic_confirmation_html(settings),
            automatic_run_timing_html(
                settings["expected_seconds"],
                settings["estimate_source"],
                state="ready",
                estimate_range=settings.get("estimate_range", ""),
                confidence_label=settings.get("estimate_confidence", ""),
                comparable_runs=settings.get("estimate_comparable_runs"),
            ),
            settings,
            # Confirm and Cancel are deliberately always mounted. Their state
            # is enabled only after this non-mutating review succeeds.
            gr.update(
                value="Confirm and start processing",
                variant="primary",
                interactive=allowed,
            ),
            gr.update(visible=False, interactive=False),
            gr.update(value="Cancel", interactive=True),
            gr.update(value="", visible=False),
        )
    except Exception as exc:
        report = app_error_report(
            "AUTO-CONFIRM-002",
            "Could not prepare the confirmation screen",
            [f"The app could not validate the current run settings: {exc}"],
            ["Refresh the page and review the selected PDF and settings.", "If the problem persists, use the failure code when reporting it."],
        )
        return (
            gr.update(),
            run_summary_html(report),
            automatic_run_timing_html(state="failed", message="Confirmation could not be prepared; review the failure report."),
            {},
            gr.update(value="Confirm and start processing", variant="primary", interactive=False),
            gr.update(visible=False, interactive=False),
            gr.update(value="Cancel", interactive=False),
            gr.update(value="", visible=False),
        )


def dispatch_confirmed_automatic_run(settings, *, progress):
    """Build one keyword-only launch request from the confirmed settings.

    Keep estimate/recovery metadata outside ``AUTOMATIC_RUN_FIELDS`` and pass
    every field by name. This prevents a UI ordering change from silently
    binding an estimate as a positional argument and then passing the same
    value again by keyword.
    """
    run_kwargs = {field: settings.get(field) for field in AUTOMATIC_RUN_FIELDS}
    run_kwargs.update(
        expected_seconds=settings.get("expected_seconds", 0),
        ocr_preflight_manifest=settings.get("ocr_preflight_manifest"),
        estimate_range=settings.get("estimate_range", ""),
        estimate_confidence=settings.get("estimate_confidence", ""),
        estimate_comparable_runs=settings.get("estimate_comparable_runs"),
        run_root_override=str(settings.get("_reserved_run_root") or "") or None,
        retain_detailed_evidence=bool(settings.get("retain_detailed_evidence")),
        progress=progress,
    )
    return run_automatic(**run_kwargs)


def run_automatic_from_confirmation(*values, progress=gr.Progress(track_tqdm=False)):
    """Run from explicit current UI values (or a dict in direct/test calls).

    The one-dict form remains supported for direct callers.  The Gradio event
    uses all run controls so the browser sends a normal, inspectable payload on
    the actual Confirm click instead of relying on ``gr.State`` persistence.
    """
    workspace_update = gr.update()
    # The outer exception handler is deliberately broad because this is the
    # final UI boundary. Initialise it before validation so a malformed UI
    # payload still becomes the established visible failure report rather than
    # a secondary UnboundLocalError in the diagnostic log.
    confirmed_settings = {}
    try:
        if len(values) == 1 and isinstance(values[0], dict):
            confirmed_settings = dict(values[0])
        else:
            confirmed_settings, validation_report, _warnings, allowed = validated_automatic_run_settings(values)
            # This optional post-confirmation control deliberately stays out
            # of the run identity/settings fingerprint: it only determines
            # whether a ready run keeps its evidence files for inspection.
            if len(values) > len(AUTOMATIC_RUN_FIELDS) + 1:
                confirmed_settings["retain_detailed_evidence"] = bool(
                    values[len(AUTOMATIC_RUN_FIELDS) + 1]
                )
            if validation_report:
                code, title, details = automatic_validation_report_summary(validation_report)
                return automatic_confirmation_failure_response(
                    code,
                    title,
                    details,
                    timing_html=automatic_run_timing_html(state="failed", message="The current settings could not be confirmed."),
                )
            if not allowed:
                return automatic_confirmation_failure_response(
                    "AUTO-CONFIRM-001",
                    "Current settings cannot start a run",
                    _warnings or ["Review the selected PDF, output mode, and workspace, then confirm again."],
                    timing_html=automatic_run_timing_html(state="failed", message="The current settings could not be confirmed."),
                )
        if not confirmed_settings:
            return automatic_confirmation_failure_response(
                "AUTO-CONFIRM-001",
                "Run settings were not confirmed",
                ["Review the current settings and confirm the run again."],
                timing_html=automatic_run_timing_html(state="failed", message="No confirmed settings were available."),
            )
        reserved_run_root = str(confirmed_settings.get("_reserved_run_root") or "")
        if reserved_run_root and automatic_run_cancellation_requested(reserved_run_root):
            return dispatch_confirmed_automatic_run(confirmed_settings, progress=progress)
        if (
            confirmed_settings.get("mode") == MODE_NATIVE_UPLOAD_LABEL
            and is_new_document_workspace_choice(confirmed_settings.get("workspace_slug"))
        ):
            update_live_automatic_run_status(
                state="preparing",
                phase="Pre-processing: creating document workspace",
                expected_seconds=confirmed_settings.get("expected_seconds", 0),
                details="Creating the isolated AnythingLLM workspace before document processing.",
                confirmed_fraction=0.0,
                cancel_available=False,
            )
            result = create_new_document_workspace(
                confirmed_settings.get("api_url"),
                confirmed_settings.get("api_key"),
                confirmed_settings.get("document_label"),
                confirmed_settings.get("files"),
                confirmed_settings.get("new_workspace_name"),
            )
            if result.get("status") != "created" or not result.get("workspace_slug"):
                return automatic_confirmation_failure_response(
                    "AUTO-WORKSPACE-004",
                    "Could not create the new document workspace",
                    [result.get("error") or result.get("status") or "Workspace creation did not return a slug."],
                    ["Review the document title and AnythingLLM runtime, then confirm the run again."],
                    timing_html=automatic_run_timing_html(state="failed", message="The new workspace was not created."),
                )
            confirmed_settings["workspace_slug"] = result["workspace_slug"]
            choices, _status = local_workspace_choices()
            choices = workspace_choices_with_new_document(choices)
            workspace_update = gr.update(choices=choices, value=result["workspace_slug"])
        APP_LOGGER.info(
            "automatic run dispatch: mode=%s scope=%s workspace=%s files=%s",
            confirmed_settings.get("mode") or "unknown",
            confirmed_settings.get("native_upload_scope") or "not-applicable",
            confirmed_settings.get("workspace_slug") or "not-selected",
            len(confirmed_settings.get("files") or []),
        )
        run_outputs = dispatch_confirmed_automatic_run(confirmed_settings, progress=progress)
        if not isinstance(run_outputs, (tuple, list)) or len(run_outputs) != 7:
            raise RuntimeError("Automatic run returned an invalid UI result contract.")
        failure_banner = automatic_run_result_failure_banner(run_outputs)
        # The prep run has finished its own upload/verification work. Refreshing the
        # guarded Desktop sidebar here is a notification only; it never alters the
        # pipeline result or turns incomplete embedding work into a success.
        if (
            confirmed_settings.get("mode") == MODE_NATIVE_UPLOAD_LABEL
            and completed_native_upload_requires_desktop_refresh(run_outputs)
        ):
            refresh_report = request_desktop_workspace_refresh()
            run_outputs = add_desktop_refresh_result_to_run_outputs(run_outputs, refresh_report)
        APP_LOGGER.info("automatic run completed dispatch with %s outputs", len(run_outputs))
        return (*run_outputs, workspace_update, failure_banner)
    except Exception as exc:
        try:
            progress(None)
        except Exception:
            pass
        APP_LOGGER.exception(
            "automatic run dispatch failed: mode=%s scope=%s workspace=%s files=%s",
            confirmed_settings.get("mode") or "unknown",
            confirmed_settings.get("native_upload_scope") or "not-applicable",
            confirmed_settings.get("workspace_slug") or "not-selected",
            len(confirmed_settings.get("files") or []),
        )
        return automatic_confirmation_failure_response(
            "AUTO-RUN-UNEXPECTED-001",
            "The confirmed run could not start",
            [str(exc) or type(exc).__name__],
            ["Open Run output and downloads for the detailed failure report.", "Retry after correcting the reported setting or runtime issue."],
            timing_html=automatic_run_timing_html(state="failed", message="The confirmed run did not start."),
        )


def automatic_run_started_response(settings):
    """Return an immediate, honest acknowledgement before lengthy work starts.

    Preparation and AnythingLLM ingestion can take minutes.  This response is
    intentionally *not* a completion claim: it confirms that the localhost
    app accepted the confirmed settings and has entered the worker.
    """
    expected_seconds = int((settings or {}).get("expected_seconds") or 0)
    return (
        gr.update(
            value=run_summary_html(
                "Status: running\n"
                "The parser/chunker has started. AnythingLLM upload and embedding completion are verified before success is shown."
            ),
            visible=True,
        ),
        gr.update(),
        gr.update(),
        gr.update(),
        gr.update(value="Processing…", interactive=False, variant="primary"),
        gr.update(),
        automatic_run_timing_html(
            expected_seconds,
            "confirmation estimate",
            state="running",
            server_driven=True,
            estimate_range=(settings or {}).get("estimate_range", ""),
            confidence_label=(settings or {}).get("estimate_confidence", ""),
            comparable_runs=(settings or {}).get("estimate_comparable_runs"),
        ),
        gr.update(),
        gr.update(value="", visible=False),
        gr.update(value="Processing started", interactive=False, variant="primary"),
        gr.update(value="Cancel", interactive=True, visible=True),
        gr.update(visible=False, interactive=False),
    )


def automatic_preprocessing_started_response():
    """Immediately acknowledge the post-click pre-processing stage.

    Validation, full-page native-text coverage, and conditional OCR checks are
    real work.  This response is deliberately yielded before those checks so
    a disabled Confirm button never leaves the page visually at "Ready".
    """
    return (
        gr.update(
            value=run_summary_html(
                "Status: preparing\n"
                "Pre-processing has started: validating selected files, verifying native text coverage, and checking OCR risk where needed."
            ),
            visible=True,
        ),
        gr.update(),
        gr.update(),
        gr.update(),
        gr.update(value="Preparing…", interactive=False, variant="primary"),
        gr.update(),
        automatic_run_timing_html(0, "pre-processing", state="running", server_driven=True),
        gr.update(),
        gr.update(value="", visible=False),
        gr.update(value="Preparing…", interactive=False, variant="primary"),
        gr.update(value="Cancel", interactive=False, visible=True),
        gr.update(visible=False, interactive=False),
    )


def confirm_button_completion_update(run_outputs):
    """Mirror the terminal result on the visible confirmation button."""
    primary_update = run_outputs[4] if isinstance(run_outputs, (tuple, list)) and len(run_outputs) > 4 else {}
    value = str((primary_update or {}).get("value") or "Processing complete")
    variant = str((primary_update or {}).get("variant") or "secondary")
    return gr.update(value=value, interactive=False, variant=variant)


def run_automatic_from_confirmation_stream(*values, progress=gr.Progress(track_tqdm=False)):
    """Stream a start acknowledgement, then the verified terminal run result.

    The prior single-return handler left the browser unchanged for the entire
    extraction/upload request, even though the backend was already working.
    Keeping the worker synchronous after the first yield preserves the current
    compatibility and post-upload verification contract while making progress
    visible immediately.
    """
    # Yield before any expensive confirmation work. The browser's one-second
    # observer reads the same durable ``preparing`` state, so this covers all
    # post-click work rather than making the action button look stuck.
    update_live_automatic_run_status(
        state="preparing",
        phase="Pre-processing: validating selected files",
        expected_seconds=0,
        details="Verifying native text coverage and OCR risk where needed.",
        confirmed_fraction=0.0,
        cancel_available=False,
    )
    yield automatic_preprocessing_started_response()
    try:
        settings, validation_report, _warnings, allowed = validated_automatic_run_settings(values)
        if len(values) > len(AUTOMATIC_RUN_FIELDS) + 1:
            settings["retain_detailed_evidence"] = bool(
                values[len(AUTOMATIC_RUN_FIELDS) + 1]
            )
    except Exception:
        # The canonical handler turns this into the established visible error.
        final = run_automatic_from_confirmation(*values, progress=progress)
        yield (
            *final,
            confirm_button_completion_update(final),
            gr.update(value="Cancel", interactive=False),
            gr.update(visible=False, interactive=False),
        )
        return
    if validation_report or not allowed:
        final = run_automatic_from_confirmation(*values, progress=progress)
        yield (
            *final,
            confirm_button_completion_update(final),
            gr.update(value="Cancel", interactive=False),
            gr.update(visible=False, interactive=False),
        )
        return

    APP_LOGGER.info(
        "automatic run acknowledged: mode=%s scope=%s workspace=%s files=%s",
        settings.get("mode") or "unknown",
        settings.get("native_upload_scope") or "not-applicable",
        settings.get("workspace_slug") or "not-selected",
        len(settings.get("files") or []),
    )
    # Reserve the durable run folder before any post-confirmation workspace
    # action.  Cancel can now reach the same marker even while a new AnythingLLM
    # workspace is being created, rather than being blind during "preparing".
    try:
        reserved_base = Path((settings.get("output_root_override") or "").strip() or str(AUTO_OUTPUT_DIR))
        reserved_run_root = create_fresh_automatic_run_root(reserved_base)
        settings["_reserved_run_root"] = str(reserved_run_root)
        update_live_automatic_run_status(
            reserved_run_root,
            state="preparing",
            phase="Pre-processing complete — starting pipeline",
            expected_seconds=settings.get("expected_seconds", 0),
            details="Run folder reserved; cancellation is available while the pipeline starts.",
            confirmed_fraction=0.0,
            cancel_available=True,
        )
    except OSError as exc:
        APP_LOGGER.warning("could not reserve early automatic run folder: %s", exc)
    # The first streamed response is emitted before the worker reserves its
    # output directory. Publish that real intermediate state so the one-second
    # observer cannot leave the page showing "Ready" while the Confirm button
    # is disabled and the confirmed request is being validated.
    if not settings.get("_reserved_run_root"):
        update_live_automatic_run_status(
            state="preparing",
            phase="Pre-processing complete — starting pipeline",
            expected_seconds=settings.get("expected_seconds", 0),
            details="Native text coverage and OCR-risk checks completed.",
            confirmed_fraction=0.0,
            cancel_available=False,
        )
    yield automatic_run_started_response(settings)
    # Reuse the exact validated snapshot which produced the visible start
    # acknowledgement. Revalidating the same browser payload here could race
    # a settings refresh and start with a different ETA than was displayed.
    final = run_automatic_from_confirmation(settings, progress=progress)
    yield (
        *final,
        confirm_button_completion_update(final),
        gr.update(value="Cancel", interactive=False),
        # Do not rely on a chained Gradio event after a streaming callback.
        # Older desktop/browser clients can leave that child update queued,
        # which keeps the already-mounted button hidden after success.
        output_folder_button_state(final[3], settings.get("output_root_override")),
    )


def metadata_text_layer_preview(pdf_path: Path):
    try:
        pages, _page_count, _element_rows = get_backend_pages(pdf_path, "pymupdf", "fast")
        stats = enrich_page_stats(
            pages,
            [page_stats_for(page) for page in pages],
        )
        quality = extraction_quality(pages, stats, 1, 0)
        sample = ""
        sample_page = 0
        for page in pages:
            text = re.sub(r"\s+", " ", (page.get("text") or "")).strip()
            if text:
                sample = text[:220]
                sample_page = int(page.get("page") or 0)
                break
        return {
            "status": "ok",
            "quality": quality,
            "sample": sample,
            "sample_page": sample_page,
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": str(exc),
        }


def target_passage_length_control_update(segment_mode_value, current_value=750, allow_custom=True):
    mode = (segment_mode_value or "").casefold()
    try:
        value = int(current_value or 750)
    except (TypeError, ValueError):
        value = 750
    value_text = str(value)
    if "all in one file" in mode or "prepare all content" in mode or mode == "none":
        return gr.update(
            label="Target passage length",
            info="No local segmentation is selected, so this setting is ignored. AnythingLLM can still re-chunk the one prepared file.",
            interactive=False,
            value=value_text,
        )
    if "whole-page" in mode:
        return gr.update(
            label="Target passage length",
            info="Whole-page chunks ignore this setting because each page stays intact unless another limit forces a split later.",
            interactive=False,
            value=value_text,
        )
    if is_page_preserving_segment_mode(mode):
        return gr.update(
            label="Target passage length",
            info="Page - preserve automatically keeps each page intact and only splits it when the active safety ceiling requires it.",
            interactive=False,
            value=value_text,
        )
    if "shorter page-local" in mode:
        return gr.update(
            label="Target subchunk length within each page",
            info="Creates shorter semantic passages without crossing a source-page boundary.",
            interactive=allow_custom,
            value=value_text,
        )
    return gr.update(
        label="Target passage length",
        info="Primary target for passage-style segmentation. Segmentation mode establishes boundaries first; this setting only sizes the passages inside that boundary contract.",
        interactive=allow_custom,
        value=value_text,
    )


def _positive_int(value, fallback=0):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return fallback


def parse_native_upload_custom_range(value, maximum=None):
    """Parse an explicit 1-based PDF-page range without guessing.

    Custom Range is available only for page-addressable modes, so a range such
    as ``3-5, 8-9`` selects those source PDF pages in document order. The
    returned positions are still consumed by the prepared upload plan, keeping
    the Desktop FIFO queue stable without staging unselected pages.
    """
    text = str(value or "").strip()
    if not text:
        raise ValueError("Enter at least one PDF page number or range.")
    selected = set()
    for part in text.split(","):
        token = part.strip()
        match = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", token)
        if not match:
            raise ValueError(f"Invalid range '{token}'. Use forms such as 1-3, 4, 9.")
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start < 1 or end < start:
            raise ValueError(f"Invalid range '{token}'. PDF page numbers start at 1.")
        if maximum is not None and end > int(maximum):
            raise ValueError(
                f"Range '{token}' exceeds the {int(maximum)} PDF pages available for this run."
            )
        selected.update(range(start, end + 1))
    return tuple(sorted(selected))


def automatic_validation_report_summary(report):
    """Extract a concise, user-facing failure from an app error report."""
    code = "AUTO-CONFIRM-001"
    title = "Current settings cannot start a run"
    details = []
    in_details = False
    for raw_line in str(report or "").splitlines():
        line = raw_line.strip()
        if line.startswith("Error code:"):
            code = line.partition(":")[2].strip() or code
        elif line.startswith("Problem:"):
            title = line.partition(":")[2].strip() or title
        elif line == "Details:":
            in_details = True
        elif in_details and line.startswith("- "):
            details.append(line[2:].strip())
        elif in_details and line:
            break
    return code, title, details or ["Review the selected PDF and settings, then confirm again."]


def native_upload_custom_range_supported(segment_mode_value):
    """Whether a Custom Range has an exact, source-page meaning.

    Page-preserve uploads one parent record per PDF page even when a very long
    page needs local child subdivision. Whole-page chunks also produce one
    uploadable record per PDF page. Other modes address passage positions, not
    pages, so accepting a value such as ``3-5`` there would be misleading.
    """
    return pipeline_segment_mode(segment_mode_value) in {"page", "page_limit"}


def native_upload_scope_batch_guard(
    scope,
    pdf_files=None,
    folder_pdf_files=None,
    segment_mode_value=SEGMENT_PAGE_LIMIT_LABEL,
):
    """Offer Custom Range only for one PDF with page-addressable output."""
    selected_files = list(dict.fromkeys(
        normalize_file_list(pdf_files) + normalize_file_list(folder_pdf_files)
    ))
    is_batch = len(selected_files) > 1
    supports_custom_range = native_upload_custom_range_supported(segment_mode_value)
    custom = (
        not is_batch
        and supports_custom_range
        and str(scope or "") == NATIVE_UPLOAD_SCOPE_CUSTOM_LABEL
    )
    effective_scope = (
        NATIVE_UPLOAD_SCOPE_CUSTOM_LABEL if custom else NATIVE_UPLOAD_SCOPE_ALL_LABEL
    )
    choices = [NATIVE_UPLOAD_SCOPE_ALL_LABEL]
    if not is_batch and supports_custom_range:
        choices.append(NATIVE_UPLOAD_SCOPE_CUSTOM_LABEL)
    return (
        gr.update(choices=choices, value=effective_scope),
        gr.update(
            value="",
            visible=not is_batch and supports_custom_range,
            interactive=not is_batch and supports_custom_range,
        ),
    )


def page_preserve_ceiling_control_update(segment_mode_value):
    enabled = is_page_preserving_segment_mode(segment_mode_value)
    return gr.update(visible=enabled, interactive=enabled)


def _recommended_target_passage_length(maximum_characters):
    ceiling = max(1, _positive_int(maximum_characters, DEFAULT_TARGET_PASSAGE_LENGTH))
    preferred = min(DEFAULT_TARGET_PASSAGE_LENGTH, ceiling)
    preset_values = [int(value) for value in TARGET_PASSAGE_LENGTH_PRESET_CHOICES]
    compatible_presets = [value for value in preset_values if value <= preferred]
    return compatible_presets[-1] if compatible_presets else preferred


def target_passage_sizing_plan(
    segment_mode_value,
    target_policy,
    requested_target=DEFAULT_TARGET_PASSAGE_LENGTH,
    inherit_anythingllm_settings=True,
    configured_chunk_size=0,
    configured_chunk_overlap=0,
    resolved_state=None,
    page_preserve_ceiling=0,
):
    """Resolve character-based passage sizing without comparing it directly to token limits."""
    state = resolved_state or anythingllm_resolved_state(
        default_anythingllm_storage_dir(), runtime_verify=False
    )
    chunk = state.get("chunking") or {}
    embedder = state.get("embedder") or {}
    embedder_policy = embedder.get("policy") or {}
    inherited = target_policy != TARGET_PASSAGE_CUSTOM_LABEL
    stored_chunk_size = _positive_int(chunk.get("chunk_size"), 1000) or 1000
    stored_overlap = _positive_int(chunk.get("chunk_overlap"), 20)
    splitter_char_limit = (
        stored_chunk_size
        if inherit_anythingllm_settings
        else (_positive_int(configured_chunk_size, stored_chunk_size) or stored_chunk_size)
    )
    overlap_characters = (
        stored_overlap
        if inherit_anythingllm_settings
        else _positive_int(configured_chunk_overlap, stored_overlap)
    )
    token_limit = _positive_int(
        embedder_policy.get("recommended_limit") or embedder.get("max_chunk_length"),
        0,
    )
    estimated_embedder_character_budget = (
        token_limit * CONSERVATIVE_CHARS_PER_EMBEDDING_TOKEN if token_limit else 0
    )
    effective_character_ceiling = min(
        value for value in (splitter_char_limit, estimated_embedder_character_budget) if value > 0
    ) if estimated_embedder_character_budget else splitter_char_limit
    requested = _positive_int(requested_target, DEFAULT_TARGET_PASSAGE_LENGTH) or DEFAULT_TARGET_PASSAGE_LENGTH
    resolved_target = (
        _recommended_target_passage_length(effective_character_ceiling)
        if inherited
        else requested
    )
    mode = (segment_mode_value or "").casefold()
    unsegmented = "all in one file" in mode or "prepare all content" in mode or mode == "none"
    whole_page = "whole-page" in mode
    page_preserving = is_page_preserving_segment_mode(mode)
    page_local_passages = "shorter page-local" in mode
    requested_page_ceiling = _positive_int(page_preserve_ceiling, 0)
    page_preserve_effective_ceiling = (
        min(requested_page_ceiling, effective_character_ceiling)
        if requested_page_ceiling > 0
        else effective_character_ceiling
    )
    exceeds_splitter = resolved_target > splitter_char_limit
    exceeds_embedder_estimate = bool(
        estimated_embedder_character_budget
        and resolved_target > estimated_embedder_character_budget
    )
    return {
        "inherited": inherited,
        "requested_target": requested,
        "resolved_target": resolved_target,
        "splitter_char_limit": splitter_char_limit,
        "overlap_characters": overlap_characters,
        "embedder_token_limit": token_limit,
        "estimated_embedder_character_budget": estimated_embedder_character_budget,
        "effective_character_ceiling": effective_character_ceiling,
        "whole_page": whole_page,
        "unsegmented": unsegmented,
        "target_ignored": whole_page or unsegmented or page_preserving,
        "page_bounded": page_local_passages,
        "page_preserving": page_preserving,
        "page_preserve_requested_ceiling": requested_page_ceiling,
        "page_preserve_effective_ceiling": page_preserve_effective_ceiling,
        "exceeds_splitter": exceeds_splitter,
        "exceeds_embedder_estimate": exceeds_embedder_estimate,
        "embedder_name": (
            embedder.get("effective_model")
            or embedder.get("model")
            or embedder_policy.get("model")
            or "active AnythingLLM embedder"
        ),
    }


def target_passage_length_warning_html(plan):
    if plan.get("unsegmented"):
        return (
            '<div class="setting-reference-note"><strong>No local segmentation:</strong> the app prepares one content file per PDF and keeps a page-span review map. '
            'AnythingLLM may still split that file according to its own settings, so uploaded chunks cannot promise exact page citations.</div>'
        )
    if plan["whole_page"]:
        return (
            '<div class="setting-reference-note"><strong>Whole-page mode:</strong> target passage length is intentionally ignored. '
            'Each page remains one prepared record; use page-bounded subchunking for the page-local hybrid.</div>'
        )
    if plan.get("page_preserving"):
        origin = (
            f"your {plan['page_preserve_requested_ceiling']}-character override"
            if plan.get("page_preserve_requested_ceiling")
            else "the active AnythingLLM splitter and embedder limits"
        )
        return (
            '<div class="setting-reference-note"><strong>Page - preserve automatically:</strong> each source page is one prepared record unless '
            f"{origin} requires page-local subdivision. Effective local ceiling: {plan['page_preserve_effective_ceiling']} characters. "
            "This follows current AnythingLLM settings by default and does not write to them.</div>"
        )
    mode_text = "page-local hybrid" if plan["page_bounded"] else "passage segmentation"
    target = plan["resolved_target"]
    if plan["exceeds_splitter"] or plan["exceeds_embedder_estimate"]:
        concerns = []
        if plan["exceeds_splitter"]:
            concerns.append(
                f"it exceeds the active AnythingLLM text-splitter size of {plan['splitter_char_limit']} characters"
            )
        if plan["exceeds_embedder_estimate"]:
            concerns.append(
                f"it is above the conservative {plan['estimated_embedder_character_budget']}-character estimate for "
                f"{html.escape(str(plan['embedder_name']))} ({plan['embedder_token_limit']} tokens × "
                f"{CONSERVATIVE_CHARS_PER_EMBEDDING_TOKEN} characters/token)"
            )
        return (
            '<div class="setting-reference-note"><strong>Target-length warning:</strong> '
            f"{target} characters may be re-chunked or truncated because {' and '.join(concerns)}. "
            "Choose the inherited target or a lower custom target. The model check is an intentionally conservative "
            "prose estimate, not an exact tokenizer measurement.</div>"
        )
    policy_text = "Inherited recommendation" if plan["inherited"] else "Custom target"
    return (
        '<div class="setting-reference-note"><strong>'
        f"{policy_text}:</strong> {target} characters for {mode_text}. "
        f"Active splitter: {plan['splitter_char_limit']} characters; overlap: {plan['overlap_characters']} characters. "
        "Embedder safety is checked with a conservative character estimate; overlap affects re-chunking continuity, not the primary target length.</div>"
    )


def target_passage_length_policy_update(
    target_policy,
    segment_mode_value,
    current_value=DEFAULT_TARGET_PASSAGE_LENGTH,
    inherit_anythingllm_settings=True,
    configured_chunk_size=0,
    configured_chunk_overlap=0,
):
    plan = target_passage_sizing_plan(
        segment_mode_value,
        target_policy,
        current_value,
        inherit_anythingllm_settings,
        configured_chunk_size,
        configured_chunk_overlap,
    )
    control = target_passage_length_control_update(
        segment_mode_value,
        plan["resolved_target"],
        allow_custom=not plan["inherited"],
    )
    if plan["inherited"] and not plan["target_ignored"]:
        control["info"] += " This is inherited and will follow the active segmentation and AnythingLLM safety limits."
    return control, target_passage_length_warning_html(plan)


def native_upload_boundary_policy_update(policy, current_target=750):
    policy = (policy or NATIVE_BOUNDARY_CURRENT_LABEL).strip()
    mapping = {
        NATIVE_BOUNDARY_PASSAGES_LABEL: SEGMENT_PASSAGES_LABEL,
        NATIVE_BOUNDARY_PAGE_LIMIT_LABEL: SEGMENT_PAGE_LIMIT_LABEL,
        NATIVE_BOUNDARY_WHOLE_PAGE_LABEL: SEGMENT_PAGE_ONLY_LABEL,
    }
    segment_value = mapping.get(policy)
    if not segment_value:
        return (
            gr.update(),
            target_passage_length_control_update(SEGMENT_PASSAGES_LABEL, current_target),
            gr.update(),
            gr.update(),
            (
                '<div class="setting-reference-note"><em>Current policy leaves segmentation and AnythingLLM settings unchanged. '
                'AnythingLLM may re-chunk uploaded records according to its own global chunk size and overlap.</em></div>'
            ),
        )
    return (
        gr.update(value=segment_value),
        target_passage_length_control_update(segment_value, current_target),
        gr.update(value=False),
        gr.update(value="0"),
        (
            '<div class="setting-reference-note"><strong>Prepared upload policy selected.</strong> '
            'The app will prepare boundary-aware records with zero additional overlap. This does not write the global '
            'AnythingLLM setting: use “Save chunk size and overlap to AnythingLLM” if you also want the server to stop '
            're-chunking records after upload.</div>'
        ),
    )


def native_upload_boundary_policy_and_timer_update(
    policy,
    current_target,
    pdf_files,
    folder_pdf_files,
    mode,
    native_upload_scope,
    workspace_slug,
    current_segment_mode,
    anythingllm_chunk_size,
    current_overlap,
    backend_mode,
    unstructured_strategy,
):
    """Apply a boundary preset and atomically refresh its derived ETA.

    Gradio does not guarantee that a server-side update of ``segment_mode`` or
    overlap will emit that component's separate ``change`` listener.  Compute
    the derived estimate in this same callback instead of leaving a stale ETA
    until an unrelated control is touched.
    """
    updates = native_upload_boundary_policy_update(policy, current_target)
    segment_update, target_update, _inherit_update, overlap_update, _note = updates
    segment_mode = segment_update.get("value", current_segment_mode) if isinstance(segment_update, dict) else current_segment_mode
    target = target_update.get("value", current_target) if isinstance(target_update, dict) else current_target
    overlap = overlap_update.get("value", current_overlap) if isinstance(overlap_update, dict) else current_overlap
    timing = refresh_automatic_run_estimate(
        pdf_files,
        folder_pdf_files,
        mode,
        native_upload_scope,
        workspace_slug,
        segment_mode,
        target,
        anythingllm_chunk_size,
        overlap,
        backend_mode,
        unstructured_strategy,
    )
    return (*updates, timing)


def pipeline_segment_mode(segment_mode_value):
    """Translate the user-facing mode label into the stable pipeline contract."""
    value = str(segment_mode_value or "").casefold()
    if value == "none" or "all in one file" in value or "prepare all content" in value:
        return "none"
    if "shorter page-local" in value:
        return "page_passages"
    if "4-page" in value or "four-page" in value:
        # A stale browser session can still submit the retired label. Preserve
        # exact citations rather than silently changing it to global passages.
        return "page_limit"
    # Keep old saved UI state page-preserving. The old label promised a target
    # it did not actually apply below the safety ceiling, so silently mapping
    # it to the new shorter-passages mode would change existing runs.
    if "page-bounded" in value:
        return "page_limit"
    if is_page_preserving_segment_mode(value):
        return "page_limit"
    if "whole-page" in value:
        return "page"
    return "passages"




def extraction_backend_help(choice):
    value = (choice or "").strip().casefold()
    if value == "pymupdf4llm":
        body = (
            "<strong>PyMuPDF4LLM</strong> converts each page into Markdown-like text. "
            "It is usually better at preserving headings, lists, and lightweight layout cues, so it can help when structural boundaries matter more than literal plain-text continuity."
        )
    elif value == "unstructured":
        body = (
            "<strong>Unstructured</strong> partitions the PDF into layout-aware elements. "
            "Use it for difficult or layout-heavy files when the lighter PyMuPDF paths are not good enough. "
            "The app can now escalate to OCR-heavy Unstructured extraction when the PDF looks image-based and local OCR is available, but expect more dependencies and slower processing."
        )
    elif value == "automatic":
        body = (
            "<strong>Automatic</strong> tries the lighter local extraction paths first, compares candidate outputs, and only escalates into Unstructured when the file looks difficult enough to justify it. "
            "This is the normal default when you want the app to choose between plain-text, Markdown-oriented, and OCR-assisted extraction without forcing one backend upfront."
        )
    else:
        body = (
            "<strong>PyMuPDF</strong> reads the PDF text layer directly as plain text. "
            "It is the simplest and usually fastest path, and it is often the best baseline when the PDF already has a clean text layer and you do not need Markdown-style structure recovery."
        )
    return f'<div class="metadata-summary"><div class="metadata-file"><div class="metadata-status">{body}</div></div></div>'


def detected_metadata_preview(pdf_files, current_title="", current_author="", current_short_label="", use_file_title_fallback=True):
    if str((LIVE_AUTOMATIC_RUN_STATUS or {}).get("state") or "") == "running":
        # Metadata inspection can be slow on large PDFs and is strictly
        # next-run preparation.  Never let a late selection callback replace
        # visible metadata while a confirmed run is in progress.
        return tuple(gr.update() for _ in range(5))
    files = normalize_file_list(pdf_files)
    if not files:
        return (
            gr.update(value=current_title or ""),
            gr.update(value=current_author or ""),
            gr.update(value=current_short_label or ""),
            '<div class="metadata-summary"><div class="metadata-status">Select a PDF to inspect embedded metadata, technical properties, page count, and bookmarks.</div></div>',
            gr.update(open=False),
        )

    previews = []
    detected_title = ""
    detected_author = ""
    detected_short = ""
    for index, raw_path in enumerate(files[:5], start=1):
        pdf_path = Path(raw_path)
        if not pdf_path.exists():
            previews.append(
                f'<section class="metadata-file"><div class="metadata-file-name">{html.escape(pdf_path.name)}</div>'
                '<div class="metadata-status">File is no longer available.</div></section>'
            )
            continue
        if pdf_path.suffix.casefold() != ".pdf":
            previews.append(
                f'<section class="metadata-file"><div class="metadata-file-name">{html.escape(pdf_path.name)}</div>'
                '<div class="metadata-status">This file is not a PDF.</div></section>'
            )
            continue
        try:
            meta = pdf_metadata(pdf_path)
        except Exception as exc:
            previews.append(
                f'<section class="metadata-file"><div class="metadata-file-name">{html.escape(pdf_path.name)}</div>'
                f'<div class="metadata-status">Metadata inspection failed: {html.escape(str(exc))}</div></section>'
            )
            continue

        embedded_title = meta.get("title") or ""
        title = embedded_title or (pdf_path.stem if use_file_title_fallback else "")
        title_origin = "PDF metadata" if embedded_title else ("filename fallback" if title else "not available")
        author = meta.get("author") or ""
        author_origin = "PDF metadata" if author else "not available"
        author_inference = {"author": "", "source": "not_found", "page": 0, "evidence": ""}
        if not author:
            author_inference = infer_author_from_pdf_text(pdf_path, title_hint=title)
            if author_inference.get("author"):
                author = author_inference["author"]
                author_origin = f"PDF text inference (page {author_inference.get('page')})"
        short_label = default_short_label(title or pdf_path.stem, author)
        if index == 1:
            detected_title = title
            detected_author = author
            detected_short = short_label

        outline = meta.get("outline") or []
        first_outline = ", ".join(item.get("title") or "" for item in outline[:5] if item.get("title"))
        text_layer = metadata_text_layer_preview(pdf_path)
        if text_layer.get("status") == "ok":
            quality = text_layer.get("quality") or {}
            scanned_likelihood = quality.get("scanned_likelihood") or "unknown"
            if scanned_likelihood == "low":
                text_layer_status = "Looks like a normal text PDF"
            elif scanned_likelihood == "possible":
                text_layer_status = "Possible scanned or low-text PDF"
            else:
                text_layer_status = "Likely scanned or OCR-needed PDF"
            text_layer_detail = (
                f"{quality.get('included_words', 0)} words across {quality.get('included_pages', 0)} profiled page(s); "
                f"empty pages {quality.get('empty_pages', 0)}, image-heavy low-text pages {quality.get('image_heavy_low_text_pages', 0)}"
            )
            sample_text = text_layer.get("sample") or "No text layer sample was extracted from the profiled pages."
            sample_origin = (
                f"PyMuPDF sample (page {text_layer.get('sample_page') or 'unknown'})"
                if text_layer.get("sample_page")
                else "PyMuPDF sample"
            )
        else:
            text_layer_status = "Could not profile the text layer"
            text_layer_detail = text_layer.get("error") or "Unknown profiling error"
            sample_text = "No sample available"
            sample_origin = "PyMuPDF sample"
        metadata_rows = [
            ("Title used", title or "Not available", title_origin),
            ("Embedded title", embedded_title or "Not set", "PDF metadata"),
            ("Author", author or "Not set", author_origin),
            ("Author evidence", author_inference.get("evidence") or "None", "PDF text scan"),
            ("Text-layer check", text_layer_status, "PyMuPDF sample"),
            ("OCR / scanned risk", text_layer_detail, "PyMuPDF page profile"),
            ("Sample extracted text", sample_text, sample_origin),
            ("Subject", meta.get("subject") or "Not set", "PDF metadata"),
            ("Keywords", meta.get("keywords") or "Not set", "PDF metadata"),
            ("Pages", meta.get("pdf_page_count", "Unknown"), "PDF structure"),
            ("Outline entries", len(outline), "PDF bookmarks"),
            ("First outline entries", first_outline or "None", "PDF bookmarks"),
            ("Creator", meta.get("creator") or "Not set", "PDF metadata"),
            ("Producer", meta.get("producer") or "Not set", "PDF metadata"),
            (
                "Created / modified",
                f"{meta.get('creationDate') or 'Not set'} / {meta.get('modDate') or 'Not set'}",
                "PDF metadata",
            ),
            (
                "Encryption",
                "Password required" if meta.get("needs_password") else ("Encrypted" if meta.get("is_encrypted") else "None detected"),
                "PDF structure",
            ),
        ]
        row_html = "".join(
            '<div class="metadata-key">{}</div><div class="metadata-value">{}</div>'.format(
                html.escape(str(key)),
                html.escape(str(value)),
            )
            for key, value, _origin in metadata_rows
        )
        previews.append(
            f'<section class="metadata-file"><div class="metadata-file-name">{html.escape(pdf_path.name)}</div>'
            f'<div class="metadata-grid">{row_html}</div></section>'
        )

    if len(files) > 5:
        previews.append(
            f'<section class="metadata-file"><div class="metadata-status">{len(files) - 5} more file(s) are selected but not shown.</div></section>'
        )

    should_fill_fields = len(files) == 1
    title_value = current_title or (detected_title if should_fill_fields else "")
    author_value = current_author or (detected_author if should_fill_fields else "")
    short_value = current_short_label or (detected_short if should_fill_fields else "")
    if len(files) > 1:
        previews.insert(
            0,
            '<section class="metadata-file"><div class="metadata-status">Multiple PDFs selected. Shared override fields remain empty because each document may have different metadata.</div></section>',
        )

    return (
        gr.update(value=title_value),
        gr.update(value=author_value),
        gr.update(value=short_value),
        '<div class="metadata-summary">' + "".join(previews) + "</div>",
        gr.update(open=True),
    )


def app_error_report(code, title, details, next_steps=None, context=None):
    lines = [
        "Status: blocked",
        f"Error code: {code}",
        f"Problem: {title}",
        "",
        "Details:",
    ]
    for detail in details:
        lines.append(f"- {detail}")
    if next_steps:
        lines.extend(["", "Next steps:"])
        for step in next_steps:
            lines.append(f"- {step}")
    if context:
        lines.extend(["", "Context:"])
        for key, value in context.items():
            lines.append(f"- {key}: {value}")
    return "\n".join(lines)


def run_summary_html(value=""):
    text = str(value or "").strip()
    if not text:
        return ""
    rows = []
    status_completed = "Status: completed" in text
    status_running = "Status: running" in text
    status_preparing = "Status: preparing" in text
    local_only_mode = any(
        f"Mode: {label}" in text
        for label in (MODE_LOCAL_ONLY_LABEL, MODE_LOCAL_NO_LOGS_LABEL)
    )
    readiness_needs_review = "Readiness: needs_review" in text
    upload_completed = "Native metadata upload: complete" in text or "Native metadata upload: complete_with_key_cleanup_warning" in text
    readiness_review_only = (
        readiness_needs_review
        and "Status: failed" not in text
        and "Status: blocked" not in text
        and (
            upload_completed
            or (status_completed and local_only_mode)
        )
    )
    is_error = (
        "Status: blocked" in text
        or "Status: failed" in text
        or (readiness_needs_review and not readiness_review_only)
        or "Readiness: failed" in text
    )
    status_label = (
        "Completed with review flags"
        if readiness_review_only
        else "Needs attention"
        if is_error
        else "Preparing"
        if status_preparing
        else "Running"
        if status_running
        else "Completed"
    )
    rows.append(
        f'<div class="summary-status{" error" if is_error else ""}">{status_label}</div>'
    )
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("- "):
            rows.append(f'<div class="summary-bullet">{html.escape(line[2:])}</div>')
        elif ":" in line:
            key, value = line.split(":", 1)
            rows.append(
                '<div class="summary-row">'
                f'<div class="summary-key">{html.escape(key.strip())}</div>'
                f'<div class="summary-value">{html.escape(value.strip())}</div>'
                "</div>"
            )
        else:
            rows.append(f'<div class="summary-heading">{html.escape(line)}</div>')
    return '<div class="run-summary-panel">' + "".join(rows) + "</div>"


def aggregate_upload_result(summaries):
    relevant = [summary for summary in (summaries or []) if summary.get("api_upload_status")]
    if not relevant:
        return None
    statuses = [summary.get("api_upload_status") for summary in relevant]
    success = all(status in {"complete", "complete_with_key_cleanup_warning"} for status in statuses)
    needs_ocr_review = any(status == "skipped_needs_ocr_review" for status in statuses)
    first_error = next(
        (
            summary.get("api_upload_error")
            for summary in relevant
            if summary.get("api_upload_error")
        ),
        "",
    )
    return {
        "status": "complete" if success else ("needs_ocr_review" if needs_ocr_review else "error"),
        "uploaded": sum(int(summary.get("api_uploaded", 0) or 0) for summary in relevant),
        "embedded": sum(int(summary.get("api_embedded", 0) or 0) for summary in relevant),
        "errors": ([{"error": first_error}] if first_error else []),
    }


def automatic_error_outputs(
    code,
    title,
    details,
    next_steps=None,
    context=None,
    readiness_html=None,
    timing_html=None,
    *,
    terminal_state="failed",
):
    record = dict(LIVE_AUTOMATIC_RUN_STATUS or {})
    run_root = str(record.get("run_root") or "")
    if run_root and automatic_run_cancellation_requested(run_root):
        # A stop request is authoritative even if a concurrently finishing
        # preflight or worker reports an ordinary exception.  Presenting that
        # controlled terminal action as a red failure both misleads the user
        # and obscures the recovery record needed to inspect partial work.
        started_epoch = float(record.get("started_epoch") or time.time())
        return automatic_run_cancelled_outputs(
            run_root,
            record.get("expected_seconds", 0),
            0,
            readiness_html or native_upload_readiness_html(initial_native_upload_readiness_report()),
            actual_seconds=max(0.0, time.time() - started_epoch),
        )
    if str(record.get("state") or "") == "running" and record.get("run_root"):
        # Once the worker has reserved an output folder, the background
        # observer treats its durable status as authoritative.  A normal early
        # return must therefore close that status before rendering the failure
        # panel, otherwise the next poll can repaint the UI as still running.
        update_live_automatic_run_status(
            record["run_root"],
            state=terminal_state,
            phase="Run needs attention",
            expected_seconds=record.get("expected_seconds", 0),
            details=f"{code}: {title}",
            confirmed_fraction=record.get("confirmed_fraction"),
            cancel_available=False,
            cancel_requested=bool(record.get("cancel_requested")),
        )
    return (
        gr.update(value=run_summary_html(app_error_report(code, title, details, next_steps, context)), visible=True),
        gr.update(value=[], visible=False),
        artifact_placeholder_html("Prepared output package"),
        [],
        gr.update(visible=False, interactive=False),
        readiness_html or native_upload_readiness_html(initial_native_upload_readiness_report()),
        timing_html or automatic_run_timing_html(
            state=terminal_state,
            message=(
                "Automatic recovery completed its bounded resume step; final indexing verification remains pending."
                if terminal_state == "warning"
                else "Run did not start or did not complete."
            ),
        ),
    )


def read_pdf_header(path):
    try:
        with open(path, "rb") as handle:
            return handle.read(8)
    except OSError as exc:
        return exc


def validate_pdf_inputs(files):
    if not files:
        return (
            None,
            app_error_report(
                "AUTO-INPUT-001",
                "No PDF selected",
                ["The Automatic workflow received no files from the upload control."],
                ["Choose at least one PDF before running automatic preparation."],
            ),
        )

    valid_files = []
    problems = []
    for raw_path in files:
        pdf_path = Path(raw_path)
        if not raw_path:
            problems.append("Empty file entry received from the upload control.")
            continue
        if not pdf_path.exists():
            problems.append(f"File does not exist or the temporary upload was removed: {pdf_path}")
            continue
        if pdf_path.is_dir():
            problems.append(f"Expected a PDF file but received a folder: {pdf_path}")
            continue
        if pdf_path.suffix.casefold() != ".pdf":
            problems.append(f"Unsupported file extension for Automatic mode: {pdf_path.name}")
            continue
        try:
            size = pdf_path.stat().st_size
        except OSError as exc:
            problems.append(f"Cannot read file metadata for {pdf_path.name}: {exc}")
            continue
        if size == 0:
            problems.append(f"PDF is empty: {pdf_path.name}")
            continue
        header = read_pdf_header(pdf_path)
        if isinstance(header, OSError):
            problems.append(f"Cannot open PDF for reading: {pdf_path.name}: {header}")
            continue
        if not header.startswith(b"%PDF-"):
            problems.append(f"File does not start with a PDF header: {pdf_path.name}")
            continue
        valid_files.append(str(pdf_path))

    if valid_files:
        return list(dict.fromkeys(valid_files)), None

    if problems:
        return (
            None,
            app_error_report(
                "AUTO-INPUT-002",
                "No readable PDF files were found in the selected upload",
                problems,
                [
                    "Keep mixed folders if you want; the app will only use readable PDF files.",
                    "Remove invalid entries or add at least one readable PDF file.",
                    "If the file is open in another program, close it and upload again.",
                    "If this is a scanned image or renamed file, convert it to a valid PDF first.",
                ],
            ),
        )
    return valid_files, None


def local_workspace_slug_exists(slug):
    if not slug:
        return False, "No workspace slug was provided."
    db_path = default_anythingllm_storage_dir() / "anythingllm.db"
    if not db_path.exists():
        return None, f"Local AnythingLLM database was not found at {db_path}."
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = con.execute("select slug from workspaces where slug = ? limit 1", (slug,)).fetchone()
        con.close()
    except Exception as exc:
        return None, f"Could not read local AnythingLLM workspace database: {exc}"
    return bool(row), f"Checked local AnythingLLM database: {db_path}"


def resolve_simulation_run(local_choice, custom_ollama_model, ollama_url):
    if local_choice == SIMULATION_SKIP_LABEL:
        return {
            "enabled": False,
            "adapter": None,
            "note": "Retrieval simulation is off. No local embedding test will run before output generation.",
            "error_report": None,
        }
    if local_choice == SIMULATION_ANYTHINGLLM_DEFAULT_LABEL:
        try:
            resolved = resolve_default_simulation_adapter()
        except Exception as exc:
            local_env = simulation_app_config()
            return {
                "enabled": False,
                "adapter": None,
                "note": "",
                "error_report": app_error_report(
                    "AUTO-SIM-005",
                    "Default OpenRouter simulation is not configured for the localhost app",
                    [str(exc)],
                    [
                        "Add the OpenRouter API key to the localhost app secret file.",
                        "Use None if you only want the prepared output files.",
                    ],
                    {"Localhost app .env": local_env.get("path") or str(project_local_env_path())},
                ),
            }
        if not resolved.get("adapter"):
            return {
                "enabled": False,
                "adapter": None,
                "note": resolved.get("message") or "",
                "error_report": app_error_report(
                    "AUTO-SIM-007",
                    "Default AnythingLLM embedder cannot run true retrieval simulation from the localhost app",
                    [resolved.get("message") or "The active AnythingLLM embedder does not have a supported true-simulation adapter."],
                    [
                        "Switch the AnythingLLM embedder to a supported simulation route such as OpenRouter or Ollama.",
                        "Or choose None if you only want preparation output files.",
                    ],
                ),
            }
        return {
            "enabled": bool(resolved.get("adapter")),
            "adapter": resolved.get("adapter"),
            "note": resolved.get("message") or "",
            "error_report": None,
        }
    openrouter_options = current_openrouter_simulation_options(force_refresh=False)
    if local_choice in openrouter_options:
        try:
            adapter = build_openrouter_simulation_adapter(openrouter_options[local_choice])
        except Exception as exc:
            local_env = simulation_app_config()
            return {
                "enabled": False,
                "adapter": None,
                "note": "",
                "error_report": app_error_report(
                    "AUTO-SIM-006",
                    "Selected OpenRouter simulation embedder is not configured for the localhost app",
                    [str(exc)],
                    [
                        "Check the localhost app .env file and verify OPENROUTER_API_KEY is set.",
                        "Use None if you only want the prepared output files.",
                    ],
                    {"Localhost app .env": local_env.get("path") or str(project_local_env_path())},
                ),
            }
        return {
            "enabled": True,
            "adapter": adapter,
            "note": f"Retrieval simulation will use {local_choice}.",
            "error_report": None,
        }
    model = (local_choice or "").strip()
    if not model:
        return {
            "enabled": False,
            "adapter": None,
            "note": "",
            "error_report": app_error_report(
                "AUTO-SIM-001",
                "Ollama model name is empty",
                ["No Ollama model was selected for retrieval simulation."],
                ["Choose one of the discovered Ollama embedding models from the dropdown."],
            ),
        }
    base = ollama_base_url(ollama_url)
    try:
        req = urllib.request.Request(f"{base}/api/tags")
        with urllib.request.urlopen(req, timeout=8) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        return {
            "enabled": False,
            "adapter": None,
            "note": "",
            "error_report": app_error_report(
                "AUTO-SIM-002",
                "Ollama is not reachable for local retrieval simulation",
                [describe_api_exception(exc, "Ollama")],
                [
                    "Start Ollama, or choose the default AnythingLLM embedder if you do not want a local simulation override.",
                    "Check that the Ollama URL points to the base server, usually http://127.0.0.1:11434.",
                ],
                {"Ollama URL": base},
            ),
        }
    models = [item.get("name") for item in data.get("models", []) if item.get("name")]
    if model not in models:
        return {
            "enabled": False,
            "adapter": None,
            "note": "",
            "error_report": app_error_report(
                "AUTO-SIM-003",
                "Selected Ollama model is not installed",
                [f"Selected model: {model}", f"Installed models: {', '.join(models) if models else 'none'}"],
                [
                    "Refresh the installed-model list and select an installed model.",
                    "Install the model with ollama pull <model>, or choose the default AnythingLLM embedder.",
                ],
                {"Ollama URL": base},
            ),
        }
    adapter = build_ollama_simulation_adapter(model, f"{base}/api/embed")
    return {
        "enabled": True,
        "adapter": adapter,
        "note": f"Retrieval simulation will use {describe_simulation_adapter(adapter)}",
        "error_report": None,
    }


def classify_pipeline_exception(exc):
    message = str(exc)
    lower = message.casefold()
    if isinstance(exc, FileNotFoundError) or "no such file" in lower or "cannot find the file" in lower:
        return {
            "code": "AUTO-PDF-001",
            "title": "PDF file was not available when preparation started",
            "details": [message],
            "next_steps": [
                "Upload the PDF again.",
                "Avoid moving or deleting the temporary upload while preparation is running.",
            ],
        }
    if "password" in lower or "encrypted" in lower or "decrypt" in lower:
        return {
            "code": "AUTO-PDF-002",
            "title": "PDF appears encrypted or password-protected",
            "details": [message],
            "next_steps": [
                "Open the PDF in a viewer and export or save an unlocked copy.",
                "Retry with the unlocked copy so the extraction backend can read the text layer.",
            ],
        }
    if "cannot open" in lower or "xref" in lower or "trailer" in lower or "damaged" in lower or "corrupt" in lower:
        return {
            "code": "AUTO-PDF-003",
            "title": "PDF could not be parsed reliably",
            "details": [message],
            "next_steps": [
                "Open the PDF in a viewer and re-save/export a fresh copy.",
                "If it was downloaded from the web, download it again and rerun preparation.",
            ],
        }
    if "no extraction backend produced usable segments" in lower or "low_text" in lower or "scanned" in lower or "ocr" in lower:
        return {
            "code": "AUTO-EXTRACT-001",
            "title": "No usable text segments were extracted",
            "details": [
                message,
                "This usually means the PDF is scanned/image-only, very low-text, corrupt, or dominated by non-prose pages.",
            ],
            "next_steps": [
                "Run OCR first, then upload the OCRed PDF.",
                "Try Unstructured/deep extraction if the PDF is layout-heavy rather than scanned.",
                "Check the failure report for per-backend word counts and empty-page counts.",
            ],
        }
    if "pymupdf" in lower or "fitz" in lower:
        return {
            "code": "AUTO-EXTRACT-002",
            "title": "PyMuPDF extraction failed",
            "details": [message],
            "next_steps": [
                "Try deep extraction so Unstructured can be attempted if available.",
                "Re-save the PDF if it may contain malformed objects.",
            ],
        }
    if "unstructured" in lower:
        return {
            "code": "AUTO-EXTRACT-003",
            "title": "Unstructured extraction failed or is unavailable",
            "details": [message],
            "next_steps": [
                "Install or repair Unstructured only if deep extraction is needed.",
                "Use the PyMuPDF/PyMuPDF4LLM candidate output if it already passes readiness checks.",
            ],
        }
    if "ollama" in lower or "openrouter" in lower or "embedding" in lower or "embeddings" in lower or "/api/embed" in lower:
        return {
            "code": "AUTO-SIM-004",
            "title": "Retrieval simulation failed during embedding",
            "details": [message],
            "next_steps": [
                "Choose None if you only want the prepared output files.",
                "Choose an installed embedding model from the dropdown for local tests.",
                "If OpenRouter is selected through AnythingLLM, verify the localhost app .env file and network access.",
            ],
        }
    if isinstance(exc, PermissionError) or "permission" in lower or "access is denied" in lower:
        return {
            "code": "AUTO-IO-001",
            "title": "The output folder or source file was blocked by permissions",
            "details": [message],
            "next_steps": [
                "Close programs that may have locked the file.",
                "Check write access to the configured application output folder.",
            ],
        }
    if isinstance(exc, OSError) or "path too long" in lower or "disk" in lower or "space" in lower:
        return {
            "code": "AUTO-IO-002",
            "title": "File-system error while preparing output",
            "details": [message],
            "next_steps": [
                "Check free disk space.",
                "Try a shorter output path if Windows path length may be involved.",
                "Close files from the output folder before rerunning.",
            ],
        }
    return {
        "code": "AUTO-PIPELINE-001",
        "title": "Automatic preparation failed",
        "details": [message],
        "next_steps": [
            "Open the generated failure report if one was written.",
            "Use the Automatic and Advanced tabs to isolate whether this is extraction, segmentation, metadata, or retrieval simulation.",
        ],
    }


def run_automatic(
    pdf_files,
    folder_pdf_files,
    document_label,
    document_author,
    document_short_label,
    use_file_title_fallback,
    mode,
    output_root_override,
    api_url,
    api_key,
    workspace_slug,
    native_upload_scope,
    native_upload_custom_range,
    native_metadata_mode,
    anythingllm_create_document_folders,
    anythingllm_document_folder_name,
    local_check_mode,
    custom_ollama_model,
    ollama_url,
    vector_audit_scope,
    deep_extraction,
    include_front_matter,
    include_back_matter,
    backend_mode,
    first_page_override,
    end_page_override,
    target_passage_length,
    page_preserve_ceiling,
    segment_mode,
    advanced_end_section_names,
    automatic_validation_phrases,
    unstructured_strategy,
    generate_inline_fallback,
    inherit_anythingllm_settings,
    anythingllm_chunk_size,
    anythingllm_chunk_overlap,
    auto_apply_recommended_settings,
    download_full_folder,
    download_segments_folder,
    expected_seconds=0,
    ocr_preflight_manifest=None,
    estimate_range="",
    estimate_confidence="",
    estimate_comparable_runs=None,
    run_root_override=None,
    retain_detailed_evidence=False,
    progress=gr.Progress(track_tqdm=False),
):
    global LAST_SIMULATION_DIAGNOSTICS
    LAST_SIMULATION_DIAGNOSTICS = {}
    started_at = time.perf_counter()
    run_local_values = locals().copy()
    processing_settings = automatic_run_processing_settings(
        {field: run_local_values.get(field) for field in AUTOMATIC_RUN_FIELDS}
    )
    page_preserve_plan = target_passage_sizing_plan(
        segment_mode,
        TARGET_PASSAGE_CUSTOM_LABEL,
        target_passage_length,
        inherit_anythingllm_settings,
        anythingllm_chunk_size,
        anythingllm_chunk_overlap,
        page_preserve_ceiling=page_preserve_ceiling,
    )
    if page_preserve_plan.get("page_preserving"):
        target_passage_length = int(page_preserve_plan["page_preserve_effective_ceiling"])
    latest_readiness_html = native_upload_readiness_html(initial_native_upload_readiness_report())
    progress(0.01, desc="Validating PDF input")
    direct_inputs = normalize_file_list(pdf_files)
    folder_inspection = inspect_uploaded_pdf_candidates(folder_pdf_files)
    combined_inputs = direct_inputs + folder_inspection["pdf_candidates"]
    if folder_inspection["raw_entries"] and not folder_inspection["pdf_candidates"] and not direct_inputs:
        progress(None)
        validation_report = no_pdf_in_folder_report(folder_pdf_files)
        return (
            gr.update(value=run_summary_html(validation_report), visible=True),
            download_files_update([], download_full_folder, download_segments_folder),
            artifact_placeholder_html("Prepared output package"),
            [],
            automatic_process_button_state(pdf_files, folder_pdf_files, processed=False),
            latest_readiness_html,
            automatic_run_timing_html(state="failed", message="No PDF files were found in the selected folder."),
        )
    files, validation_report = validate_pdf_inputs(list(dict.fromkeys(combined_inputs)))
    if validation_report:
        progress(None)
        return (
            gr.update(value=run_summary_html(validation_report), visible=True),
            download_files_update([], download_full_folder, download_segments_folder),
            artifact_placeholder_html("Prepared output package"),
            [],
            automatic_process_button_state(pdf_files, folder_pdf_files, processed=False),
            latest_readiness_html,
            automatic_run_timing_html(state="failed", message="Choose a readable PDF before confirming."),
        )

    run_timing_estimate = estimate_automatic_run(
        files,
        mode,
        native_upload_scope,
        segment_mode=segment_mode,
        chunk_size=anythingllm_chunk_size,
        chunk_overlap=anythingllm_chunk_overlap,
        target_passage_length=target_passage_length,
        backend_mode=backend_mode,
        unstructured_strategy=unstructured_strategy,
        api_url=api_url,
        inherit_anythingllm_settings=inherit_anythingllm_settings,
        local_check_mode=local_check_mode,
        profile_document_limit=automatic_timing_profile_document_limit(files),
        ocr_preflight_manifest=ocr_preflight_manifest,
    )
    # The reviewed estimate is authoritative for this visible run. Preserve its
    # formula/profile for terminal learning even if a concurrent prior run adds
    # history while this one is working.
    run_timing_estimate["expected_seconds"] = int(expected_seconds or run_timing_estimate["expected_seconds"])
    # The browser normally provides the confirmation estimate.  Direct/recovery
    # callers may omit it, in which case the freshly calculated estimate must
    # drive the same status timer rather than rendering an empty 00m00s clock.
    expected_seconds = int(run_timing_estimate["expected_seconds"])

    try:
        progress(0.025, desc="Creating output folder")
        output_root_base = Path((output_root_override or "").strip() or str(AUTO_OUTPUT_DIR))
        run_root = Path(run_root_override) if run_root_override else create_fresh_automatic_run_root(output_root_base)
        run_root.mkdir(parents=True, exist_ok=True)
        APP_LOGGER.info(
            "automatic run output folder created",
            extra={"event": "automatic_run_started", "run_id": run_root.name},
        )
        update_live_automatic_run_status(
            run_root,
            state="running",
            phase="Preparing PDF and checking AnythingLLM",
            expected_seconds=expected_seconds,
            estimate_range=estimate_range or run_timing_estimate.get("range", ""),
            confidence_label=estimate_confidence or run_timing_estimate.get("confidence_label", ""),
            comparable_runs=(
                run_timing_estimate.get("comparable_runs")
                if estimate_comparable_runs is None else estimate_comparable_runs
            ),
            # Creating the local run folder is only readiness work.  Reserve
            # the first 0.5% for it; actual PDF evidence owns the rest of the
            # early preparation range.
            confirmed_fraction=AUTOMATIC_RUN_PREFLIGHT_DISPLAY_END,
        )
        if ocr_preflight_manifest:
            (run_root / "ocr-preflight-manifest.json").write_text(
                json.dumps(ocr_preflight_manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    except OSError as exc:
        progress(None)
        return automatic_error_outputs(
            "AUTO-OUTPUT-001",
            "Could not create the output folder",
            [str(exc)],
            [
                "Check whether the configured output folder is writable.",
                "Close programs that may be locking the folder and try again.",
                "If Windows path length is involved, move the project to a shorter path.",
            ],
            {"Output root": (output_root_override or str(AUTO_OUTPUT_DIR))},
            readiness_html=latest_readiness_html,
        )

    if automatic_run_cancellation_requested(run_root):
        # Cancellation is terminal, but it is not a successful 100% run.
        # Preserve the last evidence-backed checkpoint in the durable status.
        progress(None)
        return automatic_run_cancelled_outputs(
            run_root,
            expected_seconds,
            started_at,
            latest_readiness_html,
            download_full_folder,
            download_segments_folder,
        )

    prepare_and_upload = mode == MODE_NATIVE_UPLOAD_LABEL
    flat_no_logs_output = mode == MODE_LOCAL_NO_LOGS_LABEL
    if not prepare_and_upload:
        native_upload_scope = "local_only"
        native_metadata_mode = "not_applicable"
        anythingllm_create_document_folders = False
        anythingllm_document_folder_name = ""
        auto_apply_recommended_settings = False
    workspace_slug = (workspace_slug or "").strip() if prepare_and_upload else ""
    resolved_api_url = (api_url or "").strip() if prepare_and_upload else ""
    if prepare_and_upload:
        preferred_api_url = resolved_api_url or DEFAULT_ANYTHINGLLM_API_URL
        api_resolution = detect_anythingllm_api_url(
            preferred_api_url,
            api_key=(api_key or "").strip(),
            timeout=1.25,
        )
        resolved_api_url = api_resolution.get("api_url") or preferred_api_url
    native_upload_transport = (
        "file_upload"
        if resolved_api_url and is_local_anythingllm_url(resolved_api_url)
        else "raw_text"
    )
    # Resolution can select a different compatible local endpoint than the
    # visible URL. Record the protocol actually used, so future ETAs do not
    # mix a Desktop file hand-off with a remote raw-text upload.
    run_timing_estimate.setdefault("features", {})["native_upload_transport"] = (
        native_upload_transport if prepare_and_upload else "not_applicable"
    )
    run_timing_estimate["features"]["timing_formula_lane"] = timing_formula_lane(
        run_timing_estimate["features"]
    )
    progress(0.035, desc="Checking mode and workspace settings")
    if prepare_and_upload and not workspace_slug:
        progress(None)
        return automatic_error_outputs(
            "AUTO-WORKSPACE-001",
            "Native metadata upload requires a workspace",
            ["No workspace slug was selected."],
            [
                "Click Refresh workspace info and select an existing AnythingLLM workspace.",
                f"Use {MODE_LOCAL_ONLY_LABEL} if you only want local output files.",
            ],
            readiness_html=latest_readiness_html,
        )
    if prepare_and_upload:
        if not is_lancedb_safe_namespace(workspace_slug):
            progress(None)
            return automatic_error_outputs(
                "AUTO-WORKSPACE-INVALID-SLUG-001",
                "Selected workspace cannot be used for AnythingLLM embedding",
                [
                    f"Workspace slug: {workspace_slug}",
                    "AnythingLLM Desktop uses the slug as a LanceDB namespace, which accepts only letters, numbers, underscores, hyphens, and periods.",
                ],
                [
                    "Create a new document workspace from this app; new names are now sanitized before creation.",
                    "Do not retry an existing unsafe workspace because it cannot create searchable vectors.",
                ],
                readiness_html=latest_readiness_html,
            )
    should_manage_local_runtime = prepare_and_upload
    anythingllm_embedder_preflight = {}
    if should_manage_local_runtime:
        # The confirmation view already owns the normal availability check.
        # Re-check it here only as a fast, non-progress-bearing race guard: a
        # user can close Desktop in the interval between confirmation and the
        # first worker.  The worker's continuous health guard handles later
        # transitions, so a healthy run does not advertise routine polling as
        # a distinct parsing stage.
        def report_launch_runtime_status(lifecycle_phase, _runtime):
            phase = str(lifecycle_phase or "")
            if phase == "starting_desktop":
                live_phase = "Starting AnythingLLM Desktop"
                details = "AnythingLLM was unavailable immediately after confirmation; launching Desktop once before local preparation."
            elif phase == "waiting_for_runtime":
                live_phase = "Waiting for AnythingLLM Desktop to become ready"
                details = "Desktop was launched; local preparation will begin as soon as its API responds."
            elif phase in {"ready_after_start", "ready"}:
                live_phase = "AnythingLLM Desktop is ready; starting local preparation"
                details = "Desktop is responding again. Continuing this confirmed run."
            elif phase == "start_failed":
                live_phase = "AnythingLLM Desktop could not be started"
                details = "The automatic Desktop launch did not succeed; review the runtime status before retrying."
            else:
                return
            update_live_automatic_run_status(
                run_root,
                state="running",
                phase=live_phase,
                expected_seconds=expected_seconds,
                details=details,
                confirmed_fraction=AUTOMATIC_RUN_PREFLIGHT_DISPLAY_END,
                cancel_available=False,
            )

        readiness_report = native_upload_readiness_report(
            resolved_api_url,
            api_key,
            workspace_slug,
            autostart_runtime=True,
            verify_authentication=prepare_and_upload,
            status_callback=report_launch_runtime_status,
        )
        latest_readiness_html = native_upload_readiness_html(readiness_report)
        resolved_api_url = readiness_report.get("runtime_api_url") or resolved_api_url
        try:
            runtime_preflight_path = record_automatic_runtime_preflight(run_root, readiness_report)
        except OSError as exc:
            runtime_preflight_path = None
            APP_LOGGER.warning("could not persist AnythingLLM runtime preflight: %s", exc)
        startup_phase, startup_detail = automatic_runtime_start_notice(readiness_report)
        if startup_phase:
            update_live_automatic_run_status(
                run_root,
                state="running",
                phase=startup_phase,
                expected_seconds=expected_seconds,
                details=startup_detail,
                confirmed_fraction=AUTOMATIC_RUN_PREFLIGHT_DISPLAY_END,
            )
    if prepare_and_upload:
        if not readiness_report.get("runtime_api_reachable"):
            progress(None)
            return automatic_error_outputs(
                "AUTO-UPLOAD-001",
                "AnythingLLM runtime API is not reachable for upload mode",
                [
                    readiness_report.get("runtime_api_message") or "AnythingLLM runtime did not expose a usable API endpoint.",
                    readiness_report.get("runtime_start_message") or "Desktop runtime start was not attempted.",
                ],
                [
                    "Wait for AnythingLLM Desktop to finish starting and try again.",
                    "If the Desktop window is open but the API is still down, restart AnythingLLM Desktop.",
                    f"Use {MODE_LOCAL_ONLY_LABEL} if you only want local preparation output.",
                ],
                {
                    "Detected API URL": readiness_report.get("runtime_api_url") or resolved_api_url,
                    "Runtime status": readiness_report.get("runtime_api_status"),
                    "Runtime preflight report": str(runtime_preflight_path) if runtime_preflight_path else "Could not save report",
                },
                readiness_html=latest_readiness_html,
            )
        if readiness_report.get("authenticated") is False:
            progress(None)
            return automatic_error_outputs(
                "AUTO-UPLOAD-002",
                "AnythingLLM upload authentication could not be verified",
                [
                    readiness_report.get("authentication_message") or "Authentication could not be verified.",
                ],
                [
                    "Enter a valid Developer API key, or keep using the local Desktop temporary-key route if it is available.",
                    "Confirm the AnythingLLM Desktop API is healthy before retrying.",
                    f"Use {MODE_LOCAL_ONLY_LABEL} if you only want local preparation output.",
                ],
                {
                    "Detected API URL": readiness_report.get("runtime_api_url") or resolved_api_url,
                    "Authentication status": readiness_report.get("authentication_status"),
                },
                readiness_html=latest_readiness_html,
            )
        workspace_api_found = readiness_report.get("workspace_api_found")
        workspace_found = readiness_report.get("workspace_slug_found")
        workspace_message = readiness_report.get("workspace_slug_message") or "Workspace verification did not return a result."
        api_message = readiness_report.get("workspace_api_message") or "Live API workspace check was not available."
        if workspace_api_found is False:
            progress(None)
            return automatic_error_outputs(
                "AUTO-WORKSPACE-002",
                "Selected AnythingLLM workspace was not found",
                [f"Workspace slug: {workspace_slug}", workspace_message],
                [
                    "Click Refresh workspace info and select a workspace that exists now.",
                    "If the workspace was deleted in AnythingLLM, create or select another workspace.",
                ],
                readiness_html=latest_readiness_html,
            )
        if workspace_api_found is None and workspace_found is not True:
            progress(None)
            return automatic_error_outputs(
                "AUTO-WORKSPACE-003",
                "Could not verify the selected AnythingLLM workspace",
                [workspace_message, api_message],
                [
                    "Make sure AnythingLLM Desktop is fully started, then confirm again.",
                    "Refresh workspace info to select a currently visible workspace.",
                    f"Use {MODE_LOCAL_ONLY_LABEL} if you only want the generated files and reports.",
                ],
                {"Workspace slug": workspace_slug},
                readiness_html=latest_readiness_html,
            )

    local_choice = normalize_simulation_choice(local_check_mode)
    progress(0.004, desc="Checking retrieval simulation settings")
    simulation_plan = resolve_simulation_run(local_choice, custom_ollama_model, ollama_url)
    simulation_warning = ""
    if simulation_plan.get("error_report"):
        error_report = simulation_plan["error_report"]
        if isinstance(error_report, dict):
            details = error_report.get("details") or []
            simulation_warning = (
                f"{error_report.get('code') or 'SIMULATION-SETUP'}: "
                f"{error_report.get('title') or 'Retrieval simulation unavailable'}"
            )
            if details:
                simulation_warning += f" ({details[0]})"
        else:
            warning_lines = [
                line.strip()
                for line in str(error_report).splitlines()
                if line.strip() and not line.strip().casefold().startswith(("status:", "details:", "next steps:", "context:"))
            ]
            simulation_warning = "; ".join(warning_lines[:3]) or "Retrieval simulation unavailable"
        simulation_plan = {
            "enabled": False,
            "adapter": None,
            "note": (
                "Retrieval simulation was skipped because the selected simulation embedder "
                "could not be prepared. PDF preparation continued."
            ),
            "degraded_from_error": error_report,
        }
    simulation_adapter = simulation_plan.get("adapter")
    run_vector_eval = bool(simulation_plan.get("enabled"))
    if simulation_warning:
        LAST_SIMULATION_DIAGNOSTICS = {
            "provider": (simulation_adapter or {}).get("provider") or "",
            "model": (simulation_adapter or {}).get("model") or "",
            "last_failure": simulation_warning,
        }
    auto_correction = {
        "status": "not_applied",
        "auto_corrected": False,
        "message": "Recommended AnythingLLM settings were not auto-applied for this run.",
        "policy": anythingllm_embedder_policy(default_anythingllm_storage_dir()),
        "write_result": None,
    }
    if prepare_and_upload and bool(auto_apply_recommended_settings):
        progress(0.006, desc="Applying recommended AnythingLLM settings")
        auto_correction = apply_recommended_anythingllm_settings(default_anythingllm_storage_dir())
    if run_vector_eval and simulation_adapter:
        current_state = anythingllm_resolved_state(default_anythingllm_storage_dir(), simulation_adapter)
        embed_policy = (current_state.get("embedder") or {}).get("policy") or {}
        effective_limit = int(
            (0 if inherit_anythingllm_settings else int(anythingllm_chunk_size or 0))
            or embed_policy.get("recommended_limit")
            or 4096
        )
        progress(0.008, desc="Running embedder preflight")
        preflight = simulation_preflight(
            simulation_adapter,
            effective_limit=effective_limit,
            batch_size=4,
        )
        if preflight.get("status") != "pass":
            progress(None)
            return automatic_error_outputs(
                preflight.get("error_code") or "SIM-PRE-OTHER",
                "Embedder preflight blocked the run",
                [
                    preflight.get("message") or "The selected simulation embedder did not accept the planned settings.",
                    f"Provider / model: {preflight.get('provider') or 'unknown'} / {preflight.get('model') or 'unknown'}",
                    f"Planned effective chunk limit: {effective_limit}",
                ],
                [
                    "Apply the recommended AnythingLLM settings, or enter a lower explicit embedder max chunk limit.",
                    "If the provider is overloaded or unavailable, retry later.",
                    "Use None only if you intentionally want preparation without simulation.",
                ],
                readiness_html=latest_readiness_html,
            )
    ollama_model = ""
    ollama_embed_url = f"{ollama_base_url(ollama_url)}/api/embed"
    if simulation_adapter and simulation_adapter.get("provider") == "ollama":
        ollama_model = simulation_adapter.get("model", "").strip() or "bge-m3:latest"
        ollama_embed_url = simulation_adapter.get("url", "").strip() or ollama_embed_url

    summaries = []
    # Immutable AnythingLLM configuration/schema reads are shared across this
    # run only. The pipeline invalidates this context if its storage/config
    # fingerprint changes, and performs one final mutable storage audit below.
    batch_inspection_context = {}
    if anythingllm_embedder_preflight:
        batch_inspection_context["anythingllm_runtime_embedder_probe"] = dict(
            anythingllm_embedder_preflight
        )
    downloadable = []
    flat_no_logs_exports = []
    total_files = max(len(files), 1)
    cancellation_requested = False
    ocr_eta_applied_files = set()
    # The worker supplies this same ordering on every structured event.  Keep
    # it run-local so stale callbacks from an earlier phase cannot repaint the
    # single visible progress bar after the workflow has advanced.
    automatic_phase_rank = 0
    progress_allocations = automatic_progress_file_allocations(
        files,
        segment_mode=segment_mode,
        chunk_size=int(target_passage_length or DEFAULT_TARGET_PASSAGE_LENGTH),
        chunk_overlap=int(anythingllm_chunk_overlap or 0),
        backend_mode=backend_mode or "Automatic",
        unstructured_strategy=unstructured_strategy or "auto",
    )
    if len(progress_allocations) != len(files):
        # A profile failure must never block the run. Retain the historical
        # equal split only as a safe fallback and make it visible in the log.
        progress_allocations = [
            {
                "file": Path(path).name,
                "pages": 0,
                "estimated_records": 0,
                "ocr_likely_from_preflight": False,
                "weight": 1.0,
                "share": 1.0 / total_files,
                "start_share": index / total_files,
                "end_share": (index + 1) / total_files,
            }
            for index, path in enumerate(files)
        ]
    try:
        (run_root / "progress-allocation.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "purpose": "general preflight difficulty allocation for evidence progress; not an ETA or per-document learned prediction",
                    "allocations": progress_allocations,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        APP_LOGGER.warning("could not write automatic progress allocation: %s", exc)

    def report_automatic_progress(
        value,
        stage,
        file_index=0,
        total=0,
        start=0.0,
        end=1.0,
        reset_progress=False,
        progress_event=None,
    ):
        nonlocal expected_seconds, ocr_eta_applied_files, automatic_phase_rank
        stage_text = str(stage or "Working")
        stage_key = stage_text.casefold()
        if (
            str(pdf_path) not in ocr_eta_applied_files
            and (
                ("unstructured" in stage_key and ("hi_res" in stage_key or "ocr_only" in stage_key))
                or "ocr-assisted extraction observed" in stage_key
            )
        ):
            try:
                observed_pages = int(pdf_metadata(pdf_path).get("pdf_page_count") or 0)
            except Exception:
                observed_pages = 0
            surcharge_estimate = dict(run_timing_estimate)
            surcharge_estimate["features"] = dict(run_timing_estimate.get("features") or {})
            surcharge = ocr_runtime_surcharge_seconds(
                surcharge_estimate,
                hydrated_timing_model_history(),
                observed_pages=observed_pages,
            )
            if surcharge:
                expected_seconds = max(0, int(expected_seconds or run_timing_estimate.get("expected_seconds") or 0)) + surcharge
                run_timing_estimate["expected_seconds"] = expected_seconds
                run_timing_estimate.setdefault("ocr_runtime_surcharges", []).append(
                    {"file": pdf_path.name, "pages": observed_pages, "seconds": surcharge}
                )
            ocr_eta_applied_files.add(str(pdf_path))
        source_fraction = max(0.0, min(1.0, float(value)))
        # Structured worker events already carry the canonical automatic
        # phase allocation. Do not run them through the old source-scale
        # mapper: that would reclassify local extraction as Desktop indexing.
        # Untagged callbacks remain diagnostic-only legacy fallbacks while the
        # remaining non-automatic callers keep their established values.
        phase_name = str((progress_event or {}).get("phase") or "").strip()
        # A worker can flush a buffered extraction or queue callback after a
        # later observer has already supplied exact vector evidence.  Numeric
        # high-water protection alone is insufficient: without this guard the
        # bar can keep its correct percentage while its text regresses to an
        # earlier, less informative phase.  Structured phases are ordered by
        # actual ownership and completed work, not by the text of a message.
        phase_rank = {
            "metadata": 1,
            "extraction": 2,
            "candidate_evaluation": 3,
            "payloads": 4,
            "attachments": 5,
            "queue_receipt": 6,
            "desktop_queue": 7,
            "identity_set": 8,
            "retrieval_sample": 9,
            "validation": 10,
            "reporting": 11,
        }.get(phase_name, 0)
        if phase_rank and phase_rank < automatic_phase_rank:
            return
        if phase_rank:
            automatic_phase_rank = phase_rank
        display_fraction = source_fraction if phase_name else (
            reweight_automatic_upload_progress(source_fraction)
            if prepare_and_upload
            else source_fraction
        )
        confirmed_fraction = start + (end - start) * display_fraction
        # Desktop queue and exact page-parent observations are concurrent
        # evidence for the same ingestion interval. A callback may therefore
        # bring a lagging bar up to the current elapsed/remaining share, but
        # it cannot take x/y evidence more than five points above that share.
        # This happens only on a real owned callback, never as an unattended
        # timer fill during a stalled queue.
        if phase_name in {"desktop_queue", "identity_set"} and expected_seconds:
            live = dict(LIVE_AUTOMATIC_RUN_STATUS or {})
            started_epoch = float(live.get("started_epoch") or 0.0)
            if started_epoch > 0.0:
                elapsed_seconds = max(0.0, time.time() - started_epoch)
                confirmed_fraction = concurrent_ingestion_progress_fraction(
                    confirmed_fraction, elapsed_seconds, expected_seconds
                )
        confirmed_fraction, cancellation_active = cancellation_safe_display_progress(
            run_root, confirmed_fraction
        )
        if cancellation_active:
            stage_text = "Cancellation requested — stopping at the current safe checkpoint"
        update_live_automatic_run_status(
            run_root,
            state="running",
            phase=stage_text,
            expected_seconds=expected_seconds,
            details=(f"PDF {file_index}/{total}" if total else ""),
            confirmed_fraction=confirmed_fraction,
            cancel_available=(
                automatic_run_cancel_is_safe(stage_text) and not cancellation_active
            ),
            cancel_requested=cancellation_active,
            reset_progress=reset_progress,
            progress_phase=phase_name or None,
            completed_units=(progress_event or {}).get("completed_units") if phase_name else None,
            total_units=(progress_event or {}).get("total_units") if phase_name else None,
            evidence_kind=(progress_event or {}).get("evidence_kind") if phase_name else None,
        )
        progress(
            confirmed_fraction,
            desc=format_progress_desc(stage_text, file_index, total) if total else stage_text,
        )

    completed_batch_seconds = []
    last_eta_recalibration_batch = 0
    completed_batches_across_run = 0
    last_queue_eta_recalibration_elapsed = -float("inf")
    active_file_index = 0
    planned_batch_adjustments = {}
    initial_expected_seconds = expected_seconds
    initial_estimated_batches = max(0, int((run_timing_estimate.get("features") or {}).get("estimated_batches") or 0))
    initial_non_batch_seconds = max(
        20.0,
        float(initial_expected_seconds) - initial_estimated_batches * float((run_timing_estimate.get("features") or {}).get("batch_seconds_prior") or 0.0),
    )

    def record_pipeline_timing(stage, batch_report=None):
        """Record actual batch request/verification durations for later ETAs.

        The callback is observational: it neither retries AnythingLLM nor
        changes the pipeline's completion decision.
        """
        nonlocal expected_seconds, last_eta_recalibration_batch, completed_batches_across_run, last_queue_eta_recalibration_elapsed
        # The worker can flush an event after the browser has already received
        # a durable cancel acknowledgement. Do not let that late observation
        # revise the ETA, status, or progress checkpoint.
        if automatic_run_cancellation_requested(run_root):
            return
        record_timing_model_event(run_root, stage, batch_report)
        report = batch_report or {}
        live = dict(LIVE_AUTOMATIC_RUN_STATUS or {})
        try:
            queue_rate = float(report.get("desktop_queue_records_per_minute") or 0.0)
        except (TypeError, ValueError):
            queue_rate = 0.0
        queue_remaining = report.get("desktop_queue_estimated_remaining_seconds")
        try:
            queue_completed = max(
                0,
                int(report.get("desktop_queue_completed") or 0),
                int(report.get("desktop_queue_current") or report.get("desktop_current_record") or 0) - 1,
            )
        except (TypeError, ValueError):
            queue_completed = 0
        try:
            queue_total = max(0, int(report.get("queue_records") or 0))
        except (TypeError, ValueError):
            queue_total = 0
        try:
            queue_event_samples = max(0, int(report.get("desktop_queue_events_observed") or 0))
        except (TypeError, ValueError):
            queue_event_samples = 0
        # Require a material part of a long queue, while still allowing a
        # small document to learn after at least three completed records.
        required_queue_samples = min(
            QUEUE_ETA_MIN_COMPLETED_RECORDS,
            max(3, int(math.ceil(queue_total * .02))) if queue_total else QUEUE_ETA_MIN_COMPLETED_RECORDS,
        )
        queue_rate_is_mature = (
            queue_completed >= required_queue_samples
            and queue_event_samples >= min(QUEUE_ETA_MIN_EVENT_SAMPLES, required_queue_samples)
        )
        if (
            live.get("run_root") == str(run_root)
            and queue_rate > 0.0
            and queue_remaining is not None
            and queue_rate_is_mature
        ):
            try:
                remaining_seconds = max(0.0, float(queue_remaining))
            except (TypeError, ValueError):
                remaining_seconds = None
            elapsed = time.time() - float(live.get("started_epoch") or time.time())
            # Reprice from owned queue evidence at a bounded cadence.  The
            # raw observer may emit several records for one queue position;
            # a mature sample is still repriced only every 30 seconds so the
            # visible countdown cannot bounce between noisy rate estimates.
            if (
                remaining_seconds is not None
                and elapsed - last_queue_eta_recalibration_elapsed >= QUEUE_ETA_REPRICE_INTERVAL_SECONDS
            ):
                raw_queue_forecast = queue_evidence_eta_seconds(elapsed, remaining_seconds)
                queue_forecast = bounded_queue_eta_reprice(
                    expected_seconds, raw_queue_forecast,
                )
                if abs(queue_forecast - expected_seconds) >= 5:
                    expected_seconds = queue_forecast
                    run_timing_estimate["expected_seconds"] = expected_seconds
                    run_timing_estimate.setdefault("queue_rate_recalibrations", []).append({
                        "elapsed_seconds": round(elapsed, 3),
                        "queue_completed": queue_completed,
                        "queue_total": queue_total,
                        "queue_event_samples": queue_event_samples,
                        "queue_records_per_minute": round(queue_rate, 3),
                        "queue_remaining_seconds": round(remaining_seconds, 3),
                        "raw_forecast_seconds": raw_queue_forecast,
                        "expected_seconds": expected_seconds,
                    })
                    update_live_automatic_run_status(
                        run_root,
                        state="running",
                        phase=live.get("phase") or str(stage or "Working"),
                        expected_seconds=expected_seconds,
                        details=live.get("details") or "",
                        confirmed_fraction=live.get("confirmed_fraction"),
                        cancel_available=live.get("cancel_available", True),
                        cancel_requested=live.get("cancel_requested", False),
                        eta_reprice_reason="owned_queue_rate",
                    )
                last_queue_eta_recalibration_elapsed = elapsed
        if str(report.get("timing_event") or "") == "exact_segment_plan_ready":
            exact_records = max(0, int(report.get("exact_records") or 0))
            exact_batches = max(0, int(report.get("exact_batches") or 0))
            if (
                live.get("run_root") == str(run_root)
                and active_file_index
                and not bool(report.get("upload_withheld"))
            ):
                allocation = progress_allocations[active_file_index - 1]
                planned_for_file = (
                    1
                    if native_upload_scope == NATIVE_UPLOAD_SCOPE_PROBE_LABEL and exact_records
                    else math.ceil(
                        max(0, int(allocation.get("estimated_records") or 0))
                        / max(1, int(ANYTHINGLLM_EMBEDDING_UPDATE_BATCH_SIZE))
                    )
                )
                planned_batch_adjustments[active_file_index] = exact_batches - planned_for_file
                batch_prior = max(
                    1.0,
                    float((run_timing_estimate.get("features") or {}).get("batch_seconds_prior") or 0.0),
                )
                # Exact segment count is stronger evidence than the initial
                # character-density approximation. Apply it before the first
                # native request, but use a modest cap to keep one malformed
                # PDF from rewriting an entire batch forecast.
                raw_delta = (exact_batches - planned_for_file) * batch_prior
                bounded_delta = max(-expected_seconds * .20, min(expected_seconds * .35, raw_delta))
                if abs(bounded_delta) >= 3:
                    expected_seconds = max(
                        int(time.time() - float(live.get("started_epoch") or time.time())) + 10,
                        int(round(expected_seconds + bounded_delta)),
                    )
                    run_timing_estimate["expected_seconds"] = expected_seconds
                run_timing_estimate.setdefault("exact_segment_repricing", []).append({
                    "file_index": active_file_index,
                    "exact_records": exact_records,
                    "exact_batches": exact_batches,
                    "estimated_batches_before": planned_for_file,
                    "applied_seconds": round(bounded_delta, 3),
                    "expected_seconds": expected_seconds,
                })
                update_live_automatic_run_status(
                    run_root,
                    state="running",
                    phase="Exact segment count ready — updating remaining upload estimate",
                    expected_seconds=expected_seconds,
                    details=f"PDF {active_file_index}/{total_files}: {exact_records} prepared records",
                    confirmed_fraction=live.get("confirmed_fraction"),
                    cancel_available=live.get("cancel_available", True),
                    cancel_requested=live.get("cancel_requested", False),
                    eta_reprice_reason="exact_segment_count",
                )
        if str(report.get("timing_event") or "") == "phase_completed":
            phase_elapsed = float(report.get("phase_elapsed_seconds") or 0.0)
            if live.get("run_root") == str(run_root) and phase_elapsed >= 10.0:
                elapsed = time.time() - float(live.get("started_epoch") or time.time())
                phase_corrected = evidence_paced_eta_seconds(
                    expected_seconds,
                    elapsed,
                    float(live.get("confirmed_fraction") or 0.0),
                )
                # Stage timing is useful early evidence, but batch cadence is
                # stronger. Apply a small damping factor to keep the visible
                # countdown readable and prevent a cheap stage from whiplash.
                damped_phase = int(round(expected_seconds + (phase_corrected - expected_seconds) * .30))
                if abs(damped_phase - expected_seconds) >= 15:
                    expected_seconds = max(0, damped_phase)
                    run_timing_estimate["expected_seconds"] = expected_seconds
                    run_timing_estimate.setdefault("phase_recalibrations", []).append({
                        "stage": str(stage), "phase_elapsed_seconds": round(phase_elapsed, 3),
                        "expected_seconds": expected_seconds,
                    })
                    update_live_automatic_run_status(
                        run_root,
                        state="running",
                        phase=live.get("phase") or str(stage or "Working"),
                        expected_seconds=expected_seconds,
                        details=live.get("details") or "",
                        confirmed_fraction=live.get("confirmed_fraction"),
                        cancel_available=live.get("cancel_available", True),
                        cancel_requested=live.get("cancel_requested", False),
                        eta_reprice_reason="completed_phase_timing",
                    )
        if str(report.get("timing_event") or "") != "batch_completed":
            return
        try:
            completed_batch_seconds.append(float(report.get("batch_elapsed_seconds") or 0.0))
        except (TypeError, ValueError):
            return
        live = dict(LIVE_AUTOMATIC_RUN_STATUS or {})
        completed_batches = max(0, int(report.get("batch") or 0))
        total_batches = max(0, int(report.get("total_batches") or 0))
        completed_batches_across_run += 1
        if active_file_index and active_file_index not in planned_batch_adjustments:
            allocation = progress_allocations[active_file_index - 1]
            # Probe scope intentionally submits one capped two-record request
            # for each PDF.  Its local allocation may contain the full
            # prepared-record count, which must not rewrite the run plan into
            # imaginary five-record batches during in-flight ETA correction.
            planned_for_file = (
                1
                if (
                    str((run_timing_estimate.get("features") or {}).get("mode") or "")
                    == MODE_NATIVE_UPLOAD_LABEL
                    and native_upload_scope == NATIVE_UPLOAD_SCOPE_PROBE_LABEL
                )
                else math.ceil(
                    max(0, int(allocation.get("estimated_records") or 0))
                    / max(1, int(report.get("requested") or ANYTHINGLLM_EMBEDDING_UPDATE_BATCH_SIZE))
                )
            )
            planned_batch_adjustments[active_file_index] = total_batches - planned_for_file
        if (
            live.get("run_root") != str(run_root)
            or len(completed_batch_seconds) < 3
            or (completed_batches_across_run - last_eta_recalibration_batch < 3 and completed_batches != total_batches)
        ):
            return
        planned_total_batches = max(0, initial_estimated_batches + sum(planned_batch_adjustments.values()))
        remaining_batch_count = max(0, planned_total_batches - completed_batches_across_run)
        remaining_non_batch_seconds = initial_non_batch_seconds * max(0.12, 1.0 - float(live.get("confirmed_fraction") or 0.0))
        recalibrated = recalibrated_run_eta_seconds(
            expected_seconds,
            time.time() - float(live.get("started_epoch") or time.time()),
            planned_total_batches,
            completed_batches_across_run,
            completed_batch_seconds,
            remaining_batch_count=remaining_batch_count,
            remaining_non_batch_seconds=remaining_non_batch_seconds,
        )
        # Damped three-batch checkpoints keep the countdown readable. A single
        # transient slow request can influence the next ETA, but cannot make it
        # swing wildly every five-record submission.
        damped = int(round(expected_seconds + (recalibrated - expected_seconds) * .45))
        if abs(damped - expected_seconds) < 15:
            last_eta_recalibration_batch = completed_batches_across_run
            return
        expected_seconds = max(0, damped)
        last_eta_recalibration_batch = completed_batches_across_run
        run_timing_estimate["expected_seconds"] = expected_seconds
        update_live_automatic_run_status(
            run_root,
            state="running",
            phase=live.get("phase") or str(stage or "Working"),
            expected_seconds=expected_seconds,
            details=live.get("details") or "",
            confirmed_fraction=live.get("confirmed_fraction"),
            cancel_available=live.get("cancel_available", True),
            cancel_requested=live.get("cancel_requested", False),
            eta_reprice_reason="batch_cadence",
        )

    preflight_by_path = {
        str(row.get("file") or ""): dict(row)
        for row in (ocr_preflight_manifest or {}).get("files") or []
    }
    batch_ocr_runtime_probe = dict((ocr_preflight_manifest or {}).get("runtime") or {})
    # This mutable object is intentionally run-scoped. It contains only a
    # runtime-failure reason, never extracted text, and prevents a known-missing
    # OCR dependency from being retried for every remaining scan PDF.
    unstructured_circuit_breaker = {}
    for file_index, file_path in enumerate(files, start=1):
        if automatic_run_cancellation_requested(run_root):
            cancellation_requested = True
            break
        pdf_path = Path(file_path)
        active_file_index = file_index
        automatic_phase_rank = 0
        progress_allocation = progress_allocations[file_index - 1]
        # The first 1% covers run setup and the final 5% covers durable
        # reports/downloads. The document protocol therefore receives the
        # evidence-bearing 1--95% range, weighted across PDFs by the bounded
        # preflight difficulty profile rather than an equal-file split.
        start_fraction = AUTOMATIC_RUN_PREFLIGHT_DISPLAY_END + float(progress_allocation["start_share"]) * AUTOMATIC_RUN_DOCUMENT_DISPLAY_SPAN
        end_fraction = AUTOMATIC_RUN_PREFLIGHT_DISPLAY_END + float(progress_allocation["end_share"]) * AUTOMATIC_RUN_DOCUMENT_DISPLAY_SPAN
        progress(start_fraction, desc=format_progress_desc(f"Preparing {pdf_path.name}", file_index, total_files))
        resolved_segment_mode = pipeline_segment_mode(segment_mode)
        args = SimpleNamespace(
            document_label=(document_label or "").strip(),
            document_author=(document_author or "").strip(),
            document_short_label=(document_short_label or "").strip(),
            use_file_title_fallback=bool(use_file_title_fallback),
            deep_extraction=bool(deep_extraction),
            include_front_matter=bool(include_front_matter),
            include_back_matter=bool(include_back_matter),
            backend_mode=(backend_mode or "Automatic").casefold(),
            first_page_override=int(first_page_override or 0),
            end_page_override=int(end_page_override or 0),
            target_passage_length=int(target_passage_length or 750),
            segment_mode=resolved_segment_mode,
            end_section_names=merged_end_section_headings(advanced_end_section_names),
            validation_phrases=normalize_lines(automatic_validation_phrases),
            unstructured_strategy=(unstructured_strategy or "fast").casefold(),
            anythingllm_chunk_size=0 if inherit_anythingllm_settings else int(anythingllm_chunk_size or 0),
            anythingllm_chunk_overlap=int(
                -1
                if inherit_anythingllm_settings
                else (anythingllm_chunk_overlap if anythingllm_chunk_overlap is not None else -1)
            ),
            marker_style="short",
            disable_inline_markers=not bool(generate_inline_fallback),
            # Successful customer runs remain compact by default.  The
            # explicit evidence option is useful for acceptance tests and
            # forensic review; it does not alter the prepared payload,
            # AnythingLLM calls, or verification decision.
            lean_retention=not bool(retain_detailed_evidence),
            flat_output_without_logs=flat_no_logs_output,
            run_vector_eval=bool(run_vector_eval),
            simulation_adapter=simulation_adapter,
            simulation_embedder_choice=local_choice,
            ollama_model=ollama_model.strip() or "bge-m3:latest",
            ollama_url=ollama_embed_url,
            max_vector_probes=8,
            max_vector_chunks=0 if vector_audit_scope == "Full corpus" else 300,
            prepare_and_upload=prepare_and_upload,
            anythingllm_api_url=resolved_api_url,
            anythingllm_api_key=(api_key or "").strip(),
            workspace_slug=workspace_slug,
            test_workspace_slug=workspace_slug or "test",
            upload_limit=0,
            upload_indices=(
                parse_native_upload_custom_range(native_upload_custom_range)
                if native_upload_scope == NATIVE_UPLOAD_SCOPE_CUSTOM_LABEL
                else ()
            ),
            native_upload_transport=native_upload_transport,
            native_metadata_upload_mode=(
                "strict" if native_metadata_mode == "Strict metadata only" else "native_header"
            ),
            # Page-bounded subchunks are retained in the local manifest, but
            # sending each ~300-character child as an independent AnythingLLM
            # document creates hundreds of one-chunk provider requests. Upload
            # one page parent instead: its native metadata keeps the exact PDF
            # page and its child map retains the original subchunk ranges for
            # audit/recovery, while AnythingLLM can batch its own internal
            # chunks efficiently.
            native_upload_representation=(
                "page_parents" if resolved_segment_mode == "page_limit" else "segments"
            ),
            anythingllm_create_document_folders=bool(anythingllm_create_document_folders),
            anythingllm_document_folder_name=(anythingllm_document_folder_name or "").strip(),
            anythingllm_storage_dir="",
            progress_callback=lambda value, stage, start=start_fraction, end=end_fraction, progress_event=None: report_automatic_progress(
                value, stage, file_index=file_index, total=total_files, start=start, end=end, progress_event=progress_event
            ),
            timing_event_callback=record_pipeline_timing,
            batch_inspection_context=batch_inspection_context,
            ocr_preflight_hint=preflight_by_path.get(str(pdf_path), {}),
            unstructured_runtime_probe=(
                batch_ocr_runtime_probe
                if batch_ocr_runtime_probe.get("status") in {"ready", "unavailable", "probe_failed"}
                else None
            ),
            unstructured_circuit_breaker=unstructured_circuit_breaker,
            # OCR output is expensive but deterministic only within its exact
            # source/runtime identity.  The extractor validates that identity
            # before reuse; this durable cache never changes an existing
            # AnythingLLM payload or a completed run's artifacts.
            unstructured_ocr_cache_dir=str(AUTO_OUTPUT_DIR / "_unstructured-ocr-cache"),
            external_preflight_managed=True,
            temporary_validation_cleanup_policy="cleanup_always",
            cancel_callback=lambda root=run_root: automatic_run_cancellation_requested(root),
        )
        out_dir = compatible_output_document_directory(run_root, pdf_path)
        try:
            worker_result = execute_automatic_preparation_in_worker(
                pdf_path,
                out_dir,
                args,
                run_root,
                lambda value, stage, progress_event=None: report_automatic_progress(
                    value,
                    stage,
                    file_index=file_index,
                    total=total_files,
                    start=start_fraction,
                    end=end_fraction,
                    progress_event=progress_event,
                ),
                record_pipeline_timing,
            )
            if worker_result.get("status") == "cancelled":
                cancellation_requested = True
                recovery = worker_result.get("recovery")
                if recovery:
                    downloadable.append(recovery)
                # The Cancel handler has already returned after recording the
                # stop request.  Any Desktop reconciliation must remain a
                # separate bounded background action, and only starts when a
                # ledger proves that this document crossed the submission
                # boundary.  Its observer still blocks all mutation whenever
                # manual activity or queue state is uncertain.
                if (out_dir / "inspection" / "embedding-batch-ledger.json").is_file():
                    schedule_automatic_recovery(run_root, reason="operator_cancellation")
                break
            if worker_result.get("status") == "runtime_unavailable":
                runtime_recovery = dict(worker_result.get("runtime_recovery") or {})
                runtime_ready = runtime_recovery.get("status") == "ready"
                running_process_timeout = runtime_recovery.get("status") == "running_process_api_timeout"
                if runtime_ready:
                    # The worker stopped at a submission boundary because the
                    # client could not know whether Desktop accepted that one
                    # request.  Desktop is now back. Reconcile the exact
                    # app-owned identities and submit only those still missing;
                    # do not rerun PDF parsing or replay the original batch.
                    def report_manifest_resume(stage):
                        report_automatic_progress(
                            0.0,
                            stage,
                            file_index=file_index,
                            total=total_files,
                            start=start_fraction,
                            end=end_fraction,
                        )

                    resume_result = resume_owned_embedding_manifest_after_runtime_start(
                        run_root,
                        out_dir,
                        getattr(args, "anythingllm_api_url", ""),
                        getattr(args, "anythingllm_api_key", ""),
                        status_callback=report_manifest_resume,
                    )
                    resume_status = str(resume_result.get("status") or "unknown")
                    if resume_status in {"submitted", "nothing_to_resume"}:
                        # The bridge is guarded against unsent drafts. Ask it
                        # once only after the recovered Desktop has accepted
                        # this run's resume step, so the visible workspace can
                        # catch up without making UI reload a readiness test.
                        desktop_refresh_note = refresh_desktop_after_anythingllm_mutation()
                        resumed = int(resume_result.get("accepted") or 0)
                        reconciled = int(resume_result.get("reconciled_locations") or 0)
                        phase = (
                            "AnythingLLM recovered — all interrupted records already became searchable"
                            if resume_status == "nothing_to_resume"
                            else "AnythingLLM recovered — missing embedding records were resubmitted"
                        )
                        detail = (
                            "Exact vector reconciliation found no missing records after Desktop restarted. "
                            "Final retrieval verification remains pending."
                            if resume_status == "nothing_to_resume"
                            else (
                                f"Desktop restarted and the app resubmitted {resumed} ledger-proven missing record(s). "
                                f"{reconciled} late-completing record(s) were excluded; final retrieval verification remains pending."
                            )
                        ) + f" {desktop_refresh_note}"
                        update_live_automatic_run_status(
                            run_root,
                            state="warning",
                            phase=phase,
                            expected_seconds=expected_seconds,
                            details=detail,
                            confirmed_fraction=start_fraction,
                            cancel_available=False,
                            activity_observed=False,
                        )
                        progress(None)
                        return automatic_error_outputs(
                            "AUTO-EMBEDDING-RECOVERY-001",
                            phase,
                            [detail],
                            [
                                "AnythingLLM was restarted and this run resumed only its durable, app-owned missing records.",
                                "Use the saved recovery manifest to inspect the exact reconciliation evidence while AnythingLLM finishes indexing.",
                            ],
                            {
                                "Runtime recovery report": str(run_root / AUTOMATIC_RUN_RUNTIME_RECOVERY),
                                "Embedding recovery manifest": resume_result.get("manifest_path") or "",
                            },
                            readiness_html=latest_readiness_html,
                            terminal_state="warning",
                        )
                failure_code = "AUTO-EMBEDDING-RECONCILE-001" if runtime_ready else (
                    "AUTO-ANYTHINGLLM-RUNTIME-001" if running_process_timeout else "AUTO-ANYTHINGLLM-STARTUP-001"
                )
                failure_title = (
                    "AnythingLLM restarted after an embedding submission boundary"
                    if runtime_ready
                    else (
                        "AnythingLLM is running but its local API did not become ready"
                        if running_process_timeout
                        else "AnythingLLM did not become ready after automatic startup"
                    )
                )
                failure_detail = (
                    "AnythingLLM became ready, but this run had already crossed a submission boundary. "
                    "The app stopped rather than risk a duplicate embedding submission."
                    if runtime_ready
                    else (
                        runtime_recovery.get("error")
                        if running_process_timeout
                        else worker_result.get("error") or "Desktop's local API did not respond before the recovery deadline."
                    )
                )
                update_live_automatic_run_status(
                    run_root,
                    state="failed",
                    phase=failure_title,
                    expected_seconds=expected_seconds,
                    details=f"{failure_code}: {failure_title}.",
                    confirmed_fraction=start_fraction,
                    cancel_available=False,
                    activity_observed=False,
                )
                progress(None)
                return automatic_error_outputs(
                    failure_code,
                    failure_title,
                    [failure_detail],
                    [
                        (
                            "Open the saved recovery report and resume only the ledger-proven missing records."
                            if runtime_ready
                            else "The app stopped this run before any unproven retry could submit duplicate embeddings."
                        ),
                        "Wait for AnythingLLM Desktop's local API to respond, then confirm a new run.",
                    ],
                    {
                        "Runtime recovery report": str(run_root / AUTOMATIC_RUN_RUNTIME_RECOVERY),
                        "Recovery status": runtime_recovery.get("status") or "unknown",
                    },
                    readiness_html=latest_readiness_html,
                )
            if worker_result.get("status") != "completed":
                raise RuntimeError(worker_result.get("error") or "The preparation worker did not complete.")
            # Cancellation can race a normally exiting child process. Once the
            # marker exists, its result is intentionally not incorporated into
            # this run: no follow-on callback, retry decision, or later PDF
            # may be allowed to move the checkpoint or touch AnythingLLM.
            if automatic_run_cancellation_requested(run_root):
                cancellation_requested = True
                recovery = write_automatic_cancellation_recovery(
                    run_root,
                    pdf_path,
                    active_automatic_run_worker(run_root),
                )
                if recovery:
                    downloadable.append(recovery)
                break
            summary = dict(worker_result.get("summary") or {})
            if not summary:
                raise RuntimeError("The preparation worker completed without a pipeline summary.")
            summary["run_control"] = dict(worker_result.get("run_control") or {})
            submission_recovery = submission_runtime_recovery_needed(
                summary,
                getattr(args, "anythingllm_api_url", ""),
                getattr(args, "anythingllm_api_key", ""),
            )
            if submission_recovery.get("needed"):
                report_automatic_progress(
                    0.0,
                    "AnythingLLM stopped before submission; restarting Desktop before retrying this document",
                    file_index=file_index,
                    total=total_files,
                    start=start_fraction,
                    end=end_fraction,
                    reset_progress=True,
                )
                runtime_recovery = attempt_automatic_runtime_start(
                    run_root,
                    getattr(args, "anythingllm_api_url", ""),
                    getattr(args, "anythingllm_api_key", ""),
                    stage="AnythingLLM submission authentication",
                )
                runtime_recovery["trigger"] = submission_recovery
                try:
                    append_automatic_runtime_event(
                        run_root,
                        {"phase": "AnythingLLM submission authentication", **runtime_recovery},
                    )
                except OSError:
                    pass
                summary["runtime_recovery"] = runtime_recovery
                if runtime_recovery.get("status") == "ready":
                    report_automatic_progress(
                        0.0,
                        "AnythingLLM restarted; retrying this document from local preparation",
                        file_index=file_index,
                        total=total_files,
                        start=start_fraction,
                        end=end_fraction,
                    )
                    retry_result = execute_automatic_preparation_in_worker(
                        pdf_path,
                        out_dir,
                        args,
                        run_root,
                        lambda value, stage, progress_event=None: report_automatic_progress(
                            value,
                            stage,
                            file_index=file_index,
                            total=total_files,
                            start=start_fraction,
                            end=end_fraction,
                            progress_event=progress_event,
                        ),
                        record_pipeline_timing,
                    )
                    if retry_result.get("status") == "cancelled":
                        cancellation_requested = True
                        recovery = retry_result.get("recovery")
                        if recovery:
                            downloadable.append(recovery)
                        break
                    if retry_result.get("status") != "completed":
                        raise RuntimeError(
                            retry_result.get("error")
                            or "The automatic retry after Desktop startup did not complete."
                        )
                    if automatic_run_cancellation_requested(run_root):
                        cancellation_requested = True
                        recovery = write_automatic_cancellation_recovery(
                            run_root,
                            pdf_path,
                            active_automatic_run_worker(run_root),
                        )
                        if recovery:
                            downloadable.append(recovery)
                        break
                    summary = dict(retry_result.get("summary") or {})
                    if not summary:
                        raise RuntimeError(
                            "The automatic retry after Desktop startup completed without a pipeline summary."
                        )
                    summary["run_control"] = dict(retry_result.get("run_control") or {})
                    summary["runtime_recovery"] = runtime_recovery
                    summary["runtime_retry_after_desktop_start"] = True
                    worker_result = retry_result
            worker_context = worker_result.get("batch_inspection_context")
            if isinstance(worker_context, dict):
                batch_inspection_context.update(worker_context)
            flat_retention = dict(summary.get("lean_retention") or {})
            if (
                flat_no_logs_output
                and flat_retention.get("applied")
                and flat_retention.get("policy") == "flat_local_no_logs_v1"
            ):
                flat_no_logs_exports.append(
                    promote_flat_no_logs_output(output_root_base, out_dir, pdf_path, summary)
                )
        except Exception as exc:
            LAST_SIMULATION_DIAGNOSTICS = {
                "provider": (simulation_adapter or {}).get("provider") or "",
                "model": (simulation_adapter or {}).get("model") or "",
                "last_failure": str(exc),
            }
            classified_error = classify_pipeline_exception(exc)
            # A local worker can fail after Desktop accepted an embedding
            # request. Schedule only that run's bounded recovery path, and
            # only for transport/runtime-shaped failures. The recovery worker
            # itself still requires positive owned-queue evidence before it
            # can mutate Desktop or resume anything.
            recovery_scheduled = False
            recovery_text = str(exc).casefold()
            if (
                (out_dir / "inspection" / "embedding-batch-ledger.json").is_file()
                and any(token in recovery_text for token in ("anythingllm", "update-embeddings", "timeout", "connection", "urlopen"))
            ):
                recovery_scheduled = schedule_automatic_recovery(run_root, reason="runtime_or_transport_failure")
            try:
                summary = write_failure_package(pdf_path, out_dir, exc, args)
                summary["app_error_code"] = classified_error["code"]
                summary["app_error_title"] = classified_error["title"]
                summary["app_error_message"] = str(exc)
                summary["app_error_next_steps"] = classified_error["next_steps"]
                summary["automatic_recovery_scheduled"] = recovery_scheduled
            except Exception as failure_exc:
                return automatic_error_outputs(
                    "AUTO-PIPELINE-002",
                    "Preparation failed and the failure report could not be written",
                    [
                        f"Original preparation error: {exc}",
                        f"Failure-report error: {failure_exc}",
                    ],
                    [
                        "Check write permissions and available disk space.",
                        "Try a shorter output folder path if Windows path length may be involved.",
                        "If the PDF is encrypted, scanned, or corrupt, test it in Edge/Okular and try another copy.",
                    ],
                    {"PDF": pdf_path, "Output folder": out_dir},
                    readiness_html=latest_readiness_html,
                )
        indexing_incomplete = str(summary.get("post_upload_verification_status") or "") == "partial_vector_coverage"
        if indexing_incomplete:
            # Local text is prepared, but it is false to credit the workflow
            # with this document's full allocation while exact searchable
            # page/segment-vector coverage remains incomplete.
            live = dict(LIVE_AUTOMATIC_RUN_STATUS or {})
            progress(
                float(live.get("confirmed_fraction") or start_fraction),
                desc=format_progress_desc("AnythingLLM indexing incomplete; preserving the exact recovery checkpoint", file_index, total_files),
            )
        else:
            progress(end_fraction, desc=format_progress_desc(f"Collected output files for {pdf_path.name}", file_index, total_files))
        summaries.append(summary)
        # Prepared text is the operator's usable result even if upload,
        # indexing, or a later verification layer needs review.  Keep it in
        # the general Run output state as well as the dedicated prepared-file
        # control, so neither a warning nor lean retention can strand it.
        downloadable.extend(primary_prepared_download_paths([summary]))
        LAST_SIMULATION_DIAGNOSTICS = {
            "provider": summary.get("simulation_provider") or "",
            "model": summary.get("simulation_model") or "",
            "requests": summary.get("vector_remote_requests", 0),
            "total_tokens": summary.get("vector_remote_total_tokens", 0),
            "cost": summary.get("vector_remote_cost", 0.0),
            "latency_ms_max": summary.get("vector_remote_latency_ms_max", 0),
            "key_source": summary.get("vector_remote_key_source", ""),
            "last_failure": summary.get("vector_error_detail", "") or "",
        }
        for key in [
            "upload_file",
            "inline_metadata_fallback",
            "manifest",
            "page_parent_manifest",
            "child_parent_map",
            "layout_region_review",
            "retrieval_lane_review",
            "supplementary_lane_candidates",
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
            "edge_case_report",
            "edge_case_results",
            "diagnostics_report",
            "diagnostics_csv",
            "workspace_model_gate",
            "post_upload_verification",
            "anythingllm_runtime_validation",
        ]:
            if summary.get(key):
                downloadable.append(summary[key])
        if summary.get("upload_file"):
            base_dir = Path(summary["upload_file"]).parent
            for derived in ["metadata-ratio.csv", "outline-validation.csv", "native-header-chunk-audit.csv"]:
                candidate = base_dir / derived
                if candidate.exists():
                    downloadable.append(str(candidate))
        native_kit = summary.get("native_test_kit") or {}
        for key in ["files_dir", "upload_plan", "checklist"]:
            if native_kit.get(key):
                downloadable.append(native_kit[key])
        probe_kit = summary.get("native_probe_kit") or {}
        for key in ["files_dir", "upload_plan", "checklist"]:
            if probe_kit.get(key):
                downloadable.append(probe_kit[key])
        for variant in (summary.get("variant_outputs") or {}).values():
            if variant.get("upload_file"):
                downloadable.append(variant["upload_file"])
        if automatic_run_cancellation_requested(run_root):
            cancellation_requested = True
            break

    if automatic_batch_diagnostics_required(
        summaries,
        prepare_and_upload,
        retain_detailed_evidence=retain_detailed_evidence,
        cancellation_requested=cancellation_requested,
    ):
        try:
            def report_batch_audit_progress(completed_tables, total_tables, table_name, state):
                total = max(0, int(total_tables or 0))
                completed = min(total, max(0, int(completed_tables or 0))) if total else 0
                table_label = Path(str(table_name or "")).name or "storage table"
                update_live_automatic_run_status(
                    run_root,
                    state="running",
                    phase=(
                        f"Post-completion diagnostic: inspecting AnythingLLM storage table "
                        f"{completed}/{total} ({table_label})"
                    ),
                    expected_seconds=expected_seconds,
                    details="A broad storage audit is collecting diagnostic evidence after an upload warning; it does not change verified document evidence.",
                    confirmed_fraction=None,
                    cancel_available=False,
                    cancel_requested=False,
                    activity_observed=True,
                    progress_phase="post_completion_storage_diagnostic",
                    completed_units=completed,
                    total_units=total,
                    evidence_kind="diagnostic",
                )

            batch_audit = finalize_batch_inspection_context(
                batch_inspection_context,
                default_anythingllm_storage_dir(),
                run_root / "batch-inspection",
                progress_callback=report_batch_audit_progress,
            )
            if batch_audit.get("output"):
                downloadable.append(batch_audit["output"])
                lines_for_batch_audit = (  # retained in the terminal summary below
                    f"Batch-global AnythingLLM storage audit: {batch_audit['output']}"
                )
            else:
                lines_for_batch_audit = ""
        except Exception as exc:
            APP_LOGGER.warning("could not finalize batch AnythingLLM storage audit: %s", exc)
            lines_for_batch_audit = "Batch-global AnythingLLM storage audit: unavailable"
    else:
        lines_for_batch_audit = ""

    if not summaries:
        if cancellation_requested:
            completion = {
                "state": "cancelled",
                "message": "Stopped by operator. The active document worker was terminated; no further documents were started.",
            }
            recovery = run_root / AUTOMATIC_RUN_CANCELLATION_RECOVERY
            recovered_files = [str(recovery)] if recovery.is_file() else []
            progress(None)
            update_live_automatic_run_status(
                run_root,
                state="cancelled",
                phase="Processing stopped by operator",
                expected_seconds=expected_seconds,
                details=completion["message"],
                confirmed_fraction=None,
                cancel_available=False,
                cancel_requested=True,
                activity_observed=False,
            )
            return (
                gr.update(value=run_summary_html("Status: cancelled\n" + completion["message"]), visible=True),
                download_files_update(recovered_files, download_full_folder, download_segments_folder),
                artifact_display_html(recovered_files, "Cancellation recovery record"),
                recovered_files,
                automatic_completion_button_state(completion),
                latest_readiness_html,
                automatic_run_timing_html(
                    expected_seconds,
                    "confirmation estimate",
                    state="cancelled",
                    actual_seconds=time.perf_counter() - started_at,
                    message=completion["message"],
                ),
            )
        progress(None)
        return automatic_error_outputs(
            "AUTO-INPUT-003",
            "No PDF files were prepared",
            ["The upload control did not provide any valid PDF after validation."],
            ["Upload one or more readable PDF files and run automatic preparation again."],
            readiness_html=latest_readiness_html,
        )

    downloadable = [
        str(path)
        for path in downloadable
        if path and Path(path).exists()
    ]
    incomplete_indexing = any(
        str(summary.get("post_upload_verification_status") or "") == "partial_vector_coverage"
        for summary in summaries
    )
    if not incomplete_indexing:
        progress(AUTOMATIC_RUN_TERMINAL_DISPLAY_START, desc="Writing the run report and preparing downloads")
    seen_downloads = set()
    downloadable = [
        path
        for path in downloadable
        if not (path in seen_downloads or seen_downloads.add(path))
    ]

    lines = [
        f"Output folder: {run_root}",
        f"Documents prepared: {len(summaries)}",
        f"Mode: {mode}",
        f"Native upload scope: {native_upload_scope if prepare_and_upload else 'not applicable'}",
        f"Native metadata strategy: {native_metadata_mode if prepare_and_upload else 'payload files generated only'}",
        f"Simulation embedder: {describe_simulation_choice(local_choice, custom_ollama_model)}",
        f"Embedder auto-correction: {auto_correction.get('message') or 'not needed'}",
        f"AnythingLLM settings source: {'inherit current local settings' if inherit_anythingllm_settings else 'manual chunking values'}",
    ]
    if lines_for_batch_audit:
        lines.append(lines_for_batch_audit)
    if folder_inspection["ignored_non_pdf"]:
        lines.append(
            f"Batch-folder non-PDF files ignored: {len(folder_inspection['ignored_non_pdf'])}"
        )
    if folder_inspection["missing_paths"]:
        lines.append(
            f"Batch-folder uploaded paths missing at inspection time: {len(folder_inspection['missing_paths'])}"
        )
    if simulation_warning:
        lines.append(f"Retrieval simulation warning: {simulation_warning}")
    for summary in summaries:
        document_name = Path(summary.get("upload_file") or summary.get("pdf") or "document").stem
        if summary.get("upload_file"):
            document_name = Path(summary["upload_file"]).parent.parent.name
        lines.extend(["", document_name])
        if "author_inference_passed" in summary:
            lines.append(
                f"Author inference sample benchmark: {summary.get('author_inference_passed', 0)} passed, {summary.get('author_inference_failed', 0)} failed"
            )
        if summary.get("app_error_code"):
            lines.extend(
                [
                    "Status: failed",
                    f"Error code: {summary.get('app_error_code')}",
                    f"Problem: {summary.get('app_error_title')}",
                    f"Error: {summary.get('app_error_message')}",
                    f"Failure report: {summary.get('report') or 'not written'}",
                ]
            )
            next_steps = summary.get("app_error_next_steps") or []
            if next_steps:
                lines.append("Next steps:")
                for step in next_steps:
                    lines.append(f"- {step}")
            continue
        runtime_api_status = summary.get("anythingllm_runtime_status", "not checked")
        if runtime_api_status in {"", "skipped_missing_api_url", "not_checked", "not_checked_local_only"}:
            runtime_api_status = "not checked"

        lines.extend(
            [
                f"Readiness: {summary.get('readiness_status', 'unknown')}",
                f"Readiness reasons: {', '.join(summary.get('readiness_reasons') or []) or 'none'}",
                f"Total localhost pipeline time: {summary.get('total_pipeline_seconds', 0)} seconds",
                *observed_phase_timing_lines(summary),
                f"Vector validation: {humanize_vector_status(summary.get('vector_validation_status', 'not run'))}",
                f"Vector validation detail: {summary.get('vector_error_detail') or 'none'}",
                f"Vector simulation provider/model: {summary.get('simulation_provider') or 'not run'} / {summary.get('simulation_model') or 'not run'}",
                f"Vector workload: {summary.get('vector_embedded_chunks', 0)} AnythingLLM-shaped chunk embeddings from {summary.get('vector_embedded_segments', 0)} parent segments in {summary.get('vector_eval_seconds', 0)} seconds",
                f"Vector remote requests: {summary.get('vector_remote_requests', 0)} total ({summary.get('vector_request_batches', 0)} chunk-batch requests + {summary.get('vector_probe_count', 0)} probe requests)",
                f"Vector remote token usage: prompt {summary.get('vector_remote_prompt_tokens', 0)}, total {summary.get('vector_remote_total_tokens', 0)}",
                f"Vector remote cost: {summary.get('vector_remote_cost', 0)}",
                f"Vector remote diagnostics: key source {summary.get('vector_remote_key_source') or 'not reported'}, timeout {summary.get('vector_remote_timeout_seconds', 0)}s, slow requests {summary.get('vector_remote_slow_requests', 0)}, missing usage payloads {summary.get('vector_remote_usage_missing_responses', 0)}, missing embedding payloads {summary.get('vector_remote_embedding_missing_responses', 0)}",
                f"Vector remote latency: total {summary.get('vector_remote_latency_ms_total', 0)} ms, max single request {summary.get('vector_remote_latency_ms_max', 0)} ms",
                f"Vector remote anomalies: {', '.join(summary.get('vector_remote_anomalies') or []) or 'none'}",
                f"Selected backend: {summary.get('selected_backend', 'unknown')}",
                (
                    "Unstructured runtime provenance: "
                    f"{summary.get('unstructured_backend_resolution', 'not checked')} / "
                    f"{summary.get('unstructured_backend_resolution_source', 'unknown')}"
                ),
                (
                    "Unstructured module origin: "
                    f"{summary.get('unstructured_backend_module_origin') or 'not installed'} "
                    f"(optional path search {'enabled' if summary.get('unstructured_optional_search_paths_enabled', True) else 'disabled'})"
                ),
                f"Detected pages: {summary.get('pdf_page_count', 'unknown')}",
                f"Body start: {summary.get('start_page', 'unknown')}",
                f"End matter starts: {summary.get('end_page') or 'not detected'}",
                f"Segment mode: {summary.get('segment_mode', 'passages')}",
                f"Segments: {summary.get('segments', 'unknown')}",
                f"Page-parent records: {summary.get('page_parents', 'unknown')}",
                f"Harmonization risk: segments {summary.get('segment_harmonization_risk', 'unknown')} ({summary.get('segment_units_exceeding_effective_limit', '0')} exceed effective limit), page parents {summary.get('page_parent_harmonization_risk', 'unknown')} ({summary.get('page_parent_units_exceeding_effective_limit', '0')} exceed effective limit)",
                f"Marker overhead: {summary.get('marker_char_ratio', '')}",
                f"Average segment content: {summary.get('avg_content_chars', '')} characters",
                f"AnythingLLM chunk simulation: {summary.get('chunk_size', '')} characters with {summary.get('chunk_overlap', '')} overlap ({summary.get('chunk_settings_source', '')})",
                f"Current AnythingLLM embedder configuration: {summary.get('anythingllm_embedding_engine') or 'not detected'} / {summary.get('anythingllm_embedding_model') or 'not detected'}",
                f"AnythingLLM embedder resolution: source {summary.get('anythingllm_embedding_effective_model_source') or 'not detected'}, generic fallback {summary.get('anythingllm_embedding_generic_model') or 'not detected'}, provider support {summary.get('anythingllm_embedding_provider_support') or 'not detected'}",
                f"AnythingLLM embedder anomalies: {', '.join(summary.get('anythingllm_embedding_anomalies') or []) or 'none'}",
                f"AnythingLLM embedding limits: max chunk {summary.get('anythingllm_embedding_max_chunk_length') or 'not detected'}, batch size {summary.get('anythingllm_embedding_batch_size') or 'not detected'}",
                f"Prepared-region coverage: {summary.get('selected_region_embedding_coverage', '100%')} of selected text is included; focused vector simulation may sample the generated chunks",
                (
                    "Layout-aware extraction: "
                    f"{summary.get('layout_region_status', 'not applied')}; "
                    f"excluded marginalia {summary.get('layout_removed_marginalia_count', 0)}, "
                    f"excluded footnote groups {summary.get('layout_excluded_footnote_count', 0)}, "
                    f"retained note candidates {summary.get('layout_note_candidates_retained_count', 0)}, "
                    f"column-first pages {summary.get('layout_two_column_page_count', 0)}"
                ),
                (
                    "Supplementary-lane routing: "
                    f"{summary.get('retrieval_lane_status', 'not available')}; "
                    f"{summary.get('retrieval_lane_proposed_supplementary_count', 0)} candidate(s) "
                    f"({summary.get('retrieval_lane_proposed_supplementary_segments', 0)} segment(s)); "
                    + (
                        f"{summary.get('retrieval_lane_primary_excluded_segments', 0)} automatically classified reference/index segment(s) excluded from the primary upload; original text retained for audit"
                        if summary.get('retrieval_lane_primary_payload_changed')
                        else "no narrow automatic exclusion matched; candidate material remains in the primary upload"
                    )
                ),
                f"Backend word-count disagreement: {summary.get('backend_word_disagreement', '')}",
                f"PDF outline reliability: {summary.get('outline_reliability', '')}",
                f"Read-only local AnythingLLM storage inspection: {summary.get('storage_inspection_status', '')} (does not mean AnythingLLM is running)",
                f"Storage workspace document count: {summary.get('storage_workspace_document_count', 0)}",
                f"Storage raw native docs: {summary.get('storage_raw_native_doc_count', 0)}",
                f"Storage embedded chunk count: {summary.get('storage_embedded_chunk_count', 0)}",
                f"Storage page/segment visibility: {summary.get('storage_page_segment_visibility', 'not_checked')}",
                f"Storage sample custom-document title: {summary.get('storage_sample_custom_document_title') or 'not found'}",
                f"Storage sample LanceDB title: {summary.get('storage_sample_lancedb_title') or 'not found'}",
                f"AnythingLLM runtime API: {runtime_api_status}",
                "Native metadata payload files: generated for manual/API testing",
                f"Edge-case tests: {summary.get('edge_case_status')} ({summary.get('edge_case_failures')} failures, {summary.get('edge_case_warnings')} warnings)",
                f"Run diagnostics: {summary.get('diagnostic_error_count', 0)} errors, {summary.get('diagnostic_warning_count', 0)} warnings",
            ]
        )
        if prepare_and_upload:
            lines.extend(
                [
                    f"Metadata schema API check: {summary.get('metadata_schema_status', '')}",
                    f"Native metadata upload: {summary.get('api_upload_status', 'not requested')}",
                    f"Native metadata upload detail: {summary.get('api_upload_error') or 'none'}",
                    f"Native metadata upload warning: {summary.get('api_upload_warning') or 'none'}",
                    f"AnythingLLM API authentication: {summary.get('api_authentication_mode', 'not reported')}",
                    f"AnythingLLM document foldering: {'enabled' if summary.get('api_document_foldering_enabled') else 'disabled'}",
                    f"AnythingLLM document folder name: {summary.get('api_document_folder_name') or 'shared custom-documents'}",
                    f"AnythingLLM document folder path: {summary.get('api_document_folder_path') or 'not applicable'}",
                    f"Temporary Desktop API key cleanup: {summary.get('api_temporary_key_cleanup', 'not applicable')}",
                    (
                        "Cleanup obligations: "
                        + (
                            ", ".join(
                                str(item.get("reason") or item.get("status") or "needs review")
                                for item in (summary.get("cleanup_obligations") or [])
                                if isinstance(item, dict)
                            )
                            or "none"
                        )
                    ),
                    f"Uploaded documents: {summary.get('api_uploaded', 0)}",
                    (
                        "Embedding updates accepted: "
                        f"{summary.get('api_embedding_update_accepted', summary.get('api_embedded', 0))} / "
                        f"{summary.get('api_embedding_update_requested', summary.get('api_embedded', 0))} "
                        f"document(s), in batches of "
                        f"{summary.get('api_embedding_update_batch_size') or 'not reported'}. "
                        "Acceptance is verified separately from searchable-vector completion."
                    ),
                    f"Native metadata rows found: {summary.get('native_metadata_rows', 0)}",
                    f"Workspace model gate: {summary.get('workspace_model_gate_status')}",
                    f"Post-upload verification: {summary.get('post_upload_verification_status')} ({summary.get('post_upload_classification')})",
                    f"AnythingLLM runtime validation: {summary.get('anythingllm_runtime_validation_status')} ({summary.get('anythingllm_runtime_vector_checks_passed', 0)}/{summary.get('anythingllm_runtime_vector_checks_total', 0)} vector probes at rank 1)",
                    f"AnythingLLM chat model: {summary.get('anythingllm_runtime_chat_model') or 'not run'}",
                    f"AnythingLLM chat error: {summary.get('anythingllm_runtime_chat_error') or 'none'}",
                ]
            )
        else:
            lines.extend(
                [
                    f"AnythingLLM API/upload: skipped because mode is {mode}",
                    (
                        "Workspace model gate: skipped in summary until Native metadata upload is used"
                        if summary.get("temporary_workspace_validation_status") in {"", "not_run"}
                        else "Workspace model gate: checked inside the chunk survival test workspace"
                    ),
                    (
                        f"Chunk survival test: {summary.get('temporary_workspace_validation_status')}"
                        if summary.get("temporary_workspace_validation_status") not in {"", "not_run"}
                        else "Chunk survival test: not run"
                    ),
                    (
                        f"Chunk survival workspace: {summary.get('temporary_workspace_validation_workspace_slug') or 'not created'}"
                        if summary.get("temporary_workspace_validation_status") not in {"", "not_run"}
                        else "Chunk survival workspace: not created"
                    ),
                    (
                        f"Chunk survival post-upload verification: {summary.get('temporary_workspace_validation_post_upload_status') or 'not run'}"
                        if summary.get("temporary_workspace_validation_status") not in {"", "not_run"}
                        else "Chunk survival post-upload verification: not run"
                    ),
                    (
                        f"Chunk survival runtime validation: {summary.get('temporary_workspace_validation_runtime_status') or 'not run'}"
                        if summary.get("temporary_workspace_validation_status") not in {"", "not_run"}
                        else "Chunk survival runtime validation: not run"
                    ),
                    (
                        f"Chunk survival flag: {summary.get('temporary_workspace_validation_chunk_survival_flag') or 'not reported'}"
                        if summary.get("temporary_workspace_validation_status") not in {"", "not_run"}
                        else "Chunk survival flag: not run"
                    ),
                    (
                        f"Chunk survival ratio: {summary.get('temporary_workspace_validation_chunk_survival_ratio', 0.0)}"
                        if summary.get("temporary_workspace_validation_status") not in {"", "not_run"}
                        else "Chunk survival ratio: not run"
                    ),
                    (
                        f"Chunk survival provenance risk: {summary.get('temporary_workspace_validation_page_provenance_risk') or 'not reported'}"
                        if summary.get("temporary_workspace_validation_status") not in {"", "not_run"}
                        else "Chunk survival provenance risk: not run"
                    ),
                    (
                        f"Chunk survival cleanup policy: {summary.get('temporary_workspace_validation_cleanup_policy') or 'not reported'}"
                        if summary.get("temporary_workspace_validation_status") not in {"", "not_run"}
                        else "Chunk survival cleanup policy: not applicable"
                    ),
                    (
                        f"Chunk survival retention: {summary.get('temporary_workspace_validation_retention_status') or 'not reported'}"
                        if summary.get("temporary_workspace_validation_status") not in {"", "not_run"}
                        else "Chunk survival retention: not applicable"
                    ),
                    (
                        f"Chunk survival cleanup result: {summary.get('temporary_workspace_validation_cleanup_status') or 'not reported'}"
                        if summary.get("temporary_workspace_validation_status") not in {"", "not_run"}
                        else "Chunk survival cleanup result: not applicable"
                    ),
                ]
            )
    aggregate_upload = aggregate_upload_result(summaries)
    if prepare_and_upload and aggregate_upload:
        latest_readiness_html = native_upload_readiness_html(
            native_upload_readiness_report(
                resolved_api_url,
                api_key,
                workspace_slug,
                upload_result=aggregate_upload,
                autostart_runtime=False,
                verify_authentication=True,
            )
        )
    if not incomplete_indexing:
        progress(0.98, desc="Finalizing run output and completion checks")

    completion = (
        {
            "state": "cancelled",
            "message": (
                "Cancellation completed at the current safe checkpoint. Progress remained frozen; no later PDF or "
                "new AnythingLLM request was submitted. An already accepted Desktop request may still finish; review "
                "the recovery manifest before resuming."
            ),
        }
        if cancellation_requested
        else automatic_completion(summaries, prepare_and_upload)
    )
    flat_no_logs_complete = (
        flat_no_logs_output
        and completion["state"] == "successful"
        and len(flat_no_logs_exports) == len(summaries)
    )
    wall_clock_seconds = time.perf_counter() - started_at
    live_timing_status = dict(LIVE_AUTOMATIC_RUN_STATUS or {})
    if live_timing_status.get("run_root") == str(run_root):
        active_finished_epoch = float(
            live_timing_status.get("last_activity_epoch")
            or live_timing_status.get("updated_epoch")
            or time.time()
        )
        active_started_epoch = float(live_timing_status.get("started_epoch") or active_finished_epoch)
        actual_seconds = max(0.0, active_finished_epoch - active_started_epoch)
    else:
        # This is a defensive fallback for an early failure before progress
        # could be persisted. It is intentionally marked separately below.
        actual_seconds = wall_clock_seconds
    if not flat_no_logs_complete:
        record_timing_model_run(
            run_root,
            summaries,
            completion,
            {
                "timing_estimate": run_timing_estimate,
                "source_documents": [
                    {"path": str(path), "pages": int((progress_allocations[index] or {}).get("pages") or 0)}
                    for index, path in enumerate(files)
                ],
            },
            actual_seconds,
            wall_clock_seconds=wall_clock_seconds,
        )
        append_ingestion_history(
            run_root,
            summaries,
            completion,
            prepare_and_upload,
            workspace_slug,
            processing_settings=processing_settings,
            mode=mode,
        )
    display_status = "completed" if completion["state"] == "successful" else completion["state"]
    lines.insert(0, f"Status: {display_status}")
    lines.insert(1, f"Completion assessment: {completion['message']}")

    if completion["state"] in {"cancelled", "failed"}:
        progress(None)
    else:
        progress(1.0, desc="Preparation complete")
    prepared_paths = primary_prepared_download_paths(summaries)
    update_live_automatic_run_status(
        run_root,
        state=completion["state"],
        phase=automatic_completion_phase(completion, prepare_and_upload),
        expected_seconds=expected_seconds,
        details=(
            f"{completion.get('code')}: {completion['message']}"
            if completion.get("code")
            else completion["message"]
        ),
        confirmed_fraction=1.0 if completion["state"] in {"successful", "warning"} else None,
        cancel_available=False if completion["state"] == "cancelled" else None,
        cancel_requested=True if completion["state"] == "cancelled" else None,
        output_paths=prepared_paths,
        activity_observed=False,
    )
    if flat_no_logs_complete:
        # The temporary app-run directory contains worker/progress receipts.
        # Only the promoted flat text folder is intentionally user-visible.
        shutil.rmtree(run_root, ignore_errors=True)
    return (
        gr.update(value=run_summary_html("\n".join(lines)), visible=True),
        download_files_update(prepared_paths, False, False),
        artifact_display_html(prepared_paths, "Prepared text ready to download"),
        downloadable,
        automatic_completion_button_state(completion),
        latest_readiness_html,
        automatic_run_timing_html(
            expected_seconds,
            "confirmation estimate",
            state=completion["state"],
            actual_seconds=actual_seconds,
            message=completion["message"],
        ),
    )


def run_edge_case_tests(
    pdf_files,
    document_label,
    document_short_label,
    target_workspace,
    deep_extraction,
    include_front_matter,
):
    files = normalize_file_list(pdf_files)
    if not files:
        raise gr.Error("Choose at least one PDF.")

    run_root = AUTO_OUTPUT_DIR / f"edge-case-run-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    run_root.mkdir(parents=True, exist_ok=True)
    summaries = []
    downloadable = []
    for file_path in files:
        pdf_path = Path(file_path)
        if pdf_path.suffix.lower() != ".pdf":
            continue
        args = SimpleNamespace(
            document_label=document_label.strip(),
            document_author="",
            document_short_label=document_short_label.strip(),
            deep_extraction=bool(deep_extraction),
            include_front_matter=bool(include_front_matter),
            include_back_matter=False,
            target_passage_length=750,
            marker_style="short",
            disable_inline_markers=False,
            run_vector_eval=False,
            ollama_model="bge-m3:latest",
            ollama_url="http://127.0.0.1:11434/api/embed",
            max_vector_probes=8,
            prepare_and_upload=False,
            anythingllm_api_url="",
            anythingllm_api_key="",
            workspace_slug="",
            test_workspace_slug=(target_workspace or "test").strip() or "test",
            upload_limit=0,
            anythingllm_storage_dir="",
            external_preflight_managed=True,
        )
        out_dir = compatible_output_document_directory(run_root, pdf_path)
        try:
            controlled_run = execute_preparation(pdf_path, out_dir, args, prepare_pdf)
            if controlled_run.status != "pass":
                raise RuntimeError(controlled_run.operator_summary)
            summary = legacy_summary_from_run(controlled_run)
            summary["run_control"] = controlled_run.to_dict()
        except Exception as exc:
            summary = write_failure_package(pdf_path, out_dir, exc, args)
        summaries.append(summary)
        for key in ["edge_case_report", "edge_case_results", "workspace_model_gate", "post_upload_verification", "metadata_payloads", "manifest"]:
            if summary.get(key):
                downloadable.append(summary[key])
        if summary.get("native_test_kit"):
            downloadable.extend(
                [
                    summary["native_test_kit"]["files_dir"],
                    summary["native_test_kit"]["upload_plan"],
                    summary["native_test_kit"]["checklist"],
                ]
            )

    if not summaries:
        raise gr.Error("No PDF files were tested.")

    lines = [f"Output folder: {run_root}", f"Documents tested: {len(summaries)}"]
    for summary in summaries:
        lines.extend(
            [
                "",
                f"{Path(summary['upload_file']).parent.parent.name}",
                f"Readiness: {summary['readiness_status']}",
                f"Edge-case tests: {summary.get('edge_case_status')} ({summary.get('edge_case_failures')} failures, {summary.get('edge_case_warnings')} warnings)",
                f"Workspace model gate: {summary.get('workspace_model_gate_status')}",
                f"Gate message: {summary.get('workspace_model_gate_message')}",
                f"Post-upload verification: {summary.get('post_upload_verification_status')} ({summary.get('post_upload_classification')})",
                f"Native manual files: {summary.get('native_test_kit', {}).get('file_count', 0)}",
            ]
        )
    return "\n".join(lines), gr.update(value=downloadable, visible=True), artifact_display_html(downloadable, "Test reports and native metadata kit")


INITIAL_WORKSPACE_CHOICES, INITIAL_WORKSPACE_VALUE, INITIAL_WORKSPACE_STATUS = initial_workspace_controls()
INITIAL_ANYTHINGLLM_STARTUP_STATUS = anythingllm_startup_status_html()


with gr.Blocks(title="PDF to AnythingLLM Text") as demo:
    with gr.Row(elem_classes=["top-toolbar"]):
        gr.Markdown("# PDF to AnythingLLM Text")
        expand_all_button = gr.Button(
            "Expand All",
            variant="secondary",
            size="sm",
            min_width=104,
            elem_id="expand-all-accordions-button",
        )
    with gr.Column(
        visible=bool(INITIAL_ANYTHINGLLM_STARTUP_STATUS),
        elem_id="anythingllm-startup-status-module",
        elem_classes=["anythingllm-startup-status-module"],
    ) as anythingllm_startup_status_module:
        anythingllm_startup_status = gr.HTML(
            value=INITIAL_ANYTHINGLLM_STARTUP_STATUS,
            elem_id="anythingllm-startup-status",
        )
        refresh_anythingllm_startup_status_button = gr.Button(
            "Refresh Status",
            variant="secondary",
            size="sm",
            elem_id="refresh-anythingllm-startup-status",
        )

    with gr.Tabs():
        with gr.Tab("Automatic"):
            auto_pdfs = gr.File(
                label="PDF files",
                file_types=[".pdf"],
                type="filepath",
                file_count="multiple",
                height=156,
                elem_classes=["pdf-upload-input"],
            )
            with gr.Group(elem_classes=["batch-folder-panel"]):
                gr.HTML(
                    '<div class="batch-folder-title">'
                    '<span class="batch-folder-title-icon" aria-hidden="true">'
                    "<svg viewBox='0 0 24 24' xmlns='http://www.w3.org/2000/svg'>"
                    "<path d='M12 16V4'></path>"
                    "<path d='M7 9l5-5 5 5'></path>"
                    "<path d='M20 20H4'></path>"
                    "</svg>"
                    "</span>"
                    '<span>PDF batch</span>'
                    '</div>'
                )
                auto_folder_path = gr.State("")
                auto_folder_pdfs = gr.State([])
                auto_folder_manifest = gr.State({})
                auto_folder_scan_requested = gr.State(False)
                with gr.Column(elem_classes=["batch-folder-inner"]):
                    # Hide the wrapper rather than only the Button.  Gradio can
                    # render component updates on an outer block; this makes the
                    # completed batch state unambiguously remove the picker and
                    # its reserved vertical space.
                    with gr.Column(elem_classes=["batch-folder-picker-host"]) as pdf_folder_picker_area:
                        choose_pdf_folder_button = gr.Button(
                            "Select PDF Folder Here",
                            elem_id="choose-pdf-folder-button",
                            elem_classes=["batch-folder-chooser"],
                        )
                    with gr.Column(visible=False, elem_classes=["batch-folder-selection"]) as batch_folder_selection_panel:
                        auto_folder_file_selector = gr.CheckboxGroup(
                            choices=[],
                            value=[],
                            label="PDFs to process",
                            info="All discovered PDFs start selected. Uncheck files to exclude them. This never deletes source files.",
                        )
                    auto_folder_status = gr.HTML(value="", visible=False, elem_classes=["batch-folder-status"])
            with gr.Accordion("Document metadata", open=False, elem_classes=["top-level-accordion"]) as document_metadata_section:
                auto_label = gr.Textbox(
                    label="Document title",
                    placeholder="Filled from PDF metadata when available; edit if needed",
                    lines=1,
                    max_lines=1,
                )
                auto_author = gr.Textbox(
                    label="Author",
                    placeholder="Filled from PDF metadata or title-page text inference when available; edit if needed",
                    lines=1,
                    max_lines=1,
                )
                auto_use_file_title_fallback = gr.Checkbox(
                    value=True,
                    label="Use the file title as a fallback",
                )
                # Opening this parent after a new file is detected exposes
                # only the three editable identity controls. The potentially
                # long technical report stays opt-in below.
                with gr.Accordion(
                    "Citation label and detected PDF metadata",
                    open=False,
                    elem_classes=["document-metadata-details"],
                ):
                    auto_short_label = gr.Textbox(
                        label="Short citation label",
                        placeholder="Filled from author/title when available; used in filenames and segment metadata",
                        lines=1,
                        max_lines=1,
                    )
                    refresh_metadata_button = gr.Button("Refresh detected PDF metadata")
                    gr.Markdown("**Detected PDF metadata**")
                    auto_metadata_preview = gr.HTML(
                        value='<div class="metadata-summary"><div class="metadata-status">Select a PDF to inspect embedded metadata, technical properties, page count, and bookmarks.</div></div>',
                        elem_classes=["document-metadata-preview"],
                    )
            with gr.Row():
                auto_mode = gr.Radio(
                    choices=[MODE_LOCAL_ONLY_LABEL, MODE_LOCAL_NO_LOGS_LABEL, MODE_NATIVE_UPLOAD_LABEL],
                    value=MODE_NATIVE_UPLOAD_LABEL,
                    label="Output mode",
                    info="Every run pauses at the settings confirmation screen before local files are created or AnythingLLM is changed.",
                    elem_id="output-mode-radio",
                )
            local_only_mode_notice = gr.HTML(
                value="",
                visible=False,
                elem_id="local-only-mode-notice",
            )
            with gr.Row(elem_classes=["output-folder-actions"]):
                choose_output_root_button = gr.Button("Choose Output Folder")
                open_output_root_button = gr.Button("Open Output Root")
                reset_output_root_button = gr.Button("Choose Default")
            output_root_override = gr.Textbox(
                label="(Change) Output Folder Path",
                value=str(AUTO_OUTPUT_DIR),
                placeholder=str(AUTO_OUTPUT_DIR),
                lines=1,
                max_lines=1,
                info="Default local run root. Each run still gets its own timestamped subfolder.",
            )
            output_root_status = gr.Textbox(
                label="Folder action",
                value="",
                lines=1,
                max_lines=2,
                interactive=False,
                visible=False,
            )
            anythingllm_output_root = gr.Textbox(
                label="AnythingLLM output folder",
                value=str(default_anythingllm_documents_dir()),
                placeholder=str(default_anythingllm_documents_dir()),
                lines=1,
                max_lines=1,
                interactive=False,
                info="Read-only local AnythingLLM documents storage root used for uploaded document copies.",
                elem_id="anythingllm-output-folder",
            )

            with gr.Accordion("Extraction options", open=True, elem_classes=["top-level-accordion"]) as extraction_options_section:
                with gr.Row(elem_classes=["extraction-options-row"]):
                    deep_extraction = gr.Checkbox(
                        value=False,
                        label="Force Unstructured (instead of automatic)",
                        min_width=0,
                        scale=1,
                    )
                    include_front_matter = gr.Checkbox(value=True, label="Include foreword/preface", min_width=0, scale=1)
                    include_back_matter = gr.Checkbox(value=True, label="Include notes/bibliography/index", min_width=0, scale=1)
                segment_mode = gr.Dropdown(
                    choices=[
                        SEGMENT_NONE_LABEL,
                        SEGMENT_PASSAGES_LABEL,
                        SEGMENT_PAGE_ONLY_LABEL,
                        SEGMENT_PAGE_LIMIT_LABEL,
                        SEGMENT_PAGE_PASSAGES_LABEL,
                    ],
                    value=SEGMENT_PAGE_LIMIT_LABEL,
                    label="Segmentation mode",
                    info="All in one file prepares one content file per PDF without local segmentation; AnythingLLM may still re-chunk it.",
                    interactive=True,
                )

            with gr.Accordion(
                "Native metadata upload",
                open=True,
                elem_id="native-metadata-upload-section",
                elem_classes=["top-level-accordion"],
                visible=True,
            ) as native_metadata_section:
                with gr.Column(elem_classes=["native-upload-stack"]):
                    with gr.Accordion("Select a workspace", open=True, elem_classes=["native-upload-subaccordion"]) as workspace_selection_section:
                        with gr.Row():
                            refresh_workspace_button = gr.Button("Refresh workspace info")
                            refresh_schema_button = gr.Button("Refresh metadata schema")
                        workspace_slug = gr.Dropdown(
                            choices=INITIAL_WORKSPACE_CHOICES,
                            value=INITIAL_WORKSPACE_VALUE,
                            label="Workspace",
                            interactive=True,
                            allow_custom_value=False,
                        )
                        new_workspace_name = gr.Textbox(
                            label="New workspace name",
                            value="",
                            placeholder="Choose a PDF to derive a safe name, or type your own",
                            lines=1,
                            max_lines=1,
                            visible=True,
                            info="Used only for New workspace for this document. Unsafe characters are normalized; an existing name becomes Name 2, Name 3, and so on.",
                        )
                        new_workspace_name_auto_state = gr.State("")
                        with gr.Accordion("Workspace query status", open=False, elem_classes=["native-upload-subaccordion"]):
                            workspace_status = gr.Textbox(
                                label="Workspace query status",
                                value=INITIAL_WORKSPACE_STATUS,
                                lines=3,
                                interactive=False,
                                show_label=False,
                            )
                    with gr.Accordion("Native upload settings", open=True, elem_classes=["native-upload-subaccordion"]):
                        native_upload_scope = gr.Dropdown(
                            choices=[NATIVE_UPLOAD_SCOPE_ALL_LABEL, NATIVE_UPLOAD_SCOPE_CUSTOM_LABEL],
                            value=NATIVE_UPLOAD_SCOPE_ALL_LABEL,
                            label="Native upload scope",
                            info="Custom range is available only for one PDF in a page-based segmentation mode; it then selects PDF page numbers.",
                            interactive=True,
                        )
                        native_upload_custom_range = gr.Textbox(
                            value="",
                            label="PDF page range (Custom range only)",
                            placeholder="1-3, 4, 9, 12-30",
                            info="Enter comma-separated PDF page numbers or inclusive ranges. Page - preserve automatically and Whole-page chunks only.",
                            visible=True,
                            lines=1,
                            max_lines=1,
                        )
                        native_boundary_policy = gr.Dropdown(
                            choices=[
                                NATIVE_BOUNDARY_CURRENT_LABEL,
                                NATIVE_BOUNDARY_PASSAGES_LABEL,
                                NATIVE_BOUNDARY_PAGE_LIMIT_LABEL,
                                NATIVE_BOUNDARY_WHOLE_PAGE_LABEL,
                            ],
                            value=NATIVE_BOUNDARY_CURRENT_LABEL,
                            label="Native upload boundary policy",
                            info="Controls prepared record boundaries and the planned overlap. It does not silently rewrite AnythingLLM's global settings.",
                            interactive=True,
                        )
                        native_boundary_policy_note = gr.HTML(
                            value=(
                                '<div class="setting-reference-note"><em>Current policy leaves segmentation and AnythingLLM settings unchanged. '
                                'Use a boundary policy when page-local retrieval matters.</em></div>'
                            )
                        )
                        native_metadata_mode = gr.Dropdown(
                            choices=["Native title header (priority)", "Strict metadata only"],
                            value="Native title header (priority)",
                            label="Native metadata strategy",
                            interactive=True,
                        )
                    with gr.Accordion("AnythingLLM document folders", open=False, elem_classes=["native-upload-subaccordion"]):
                        anythingllm_create_document_folders = gr.Checkbox(
                            value=False,
                            label="Create dedicated AnythingLLM document folder per title",
                            info=(
                                "Default off for AnythingLLM Desktop 1.15: its Documents drawer only lists "
                                "top-level custom-documents records. Turn this on only if tidy on-disk folders "
                                "matter more than visible drawer evidence."
                            ),
                        )
                        anythingllm_document_folder_name = gr.Textbox(
                            value="",
                            label="AnythingLLM document folder name (optional)",
                            placeholder="Leave blank to derive the folder name automatically",
                            lines=1,
                            max_lines=1,
                            info="Optional grouping folder under custom-documents; per-PDF child folders remain enabled above.",
                        )
                    with gr.Accordion("Native upload readiness", open=False, elem_classes=["native-upload-subaccordion"]):
                        native_upload_readiness = gr.HTML(
                            value=native_upload_readiness_html(initial_native_upload_readiness_report()),
                            elem_classes=["native-upload-readiness-html"],
                        )
                        background_reconciliation_status = gr.HTML(
                            value=background_reconciliation_html("", {}),
                            elem_classes=["native-upload-readiness-html"],
                        )
                    with gr.Accordion("Embedding observer (read-only)", open=False, elem_classes=["native-upload-subaccordion"]):
                        gr.Markdown(
                            "Record a baseline, manually start an AnythingLLM embedding update, then observe it here. "
                            "This control never starts, stops, retries, or changes the upload."
                        )
                        embedding_expected_records = gr.Number(
                            value=0,
                            precision=0,
                            minimum=0,
                            label="Expected prepared records (optional)",
                            info="Enter the number of records you deliberately submitted. Completion requires that count and 60 seconds without observed change.",
                        )
                        with gr.Row():
                            start_embedding_observer_button = gr.Button("Record embedding baseline")
                            sample_embedding_observer_button = gr.Button("Observe embedding now")
                        embedding_observer_state = gr.State({})
                        embedding_observer_status = gr.HTML(
                            value=ingestion_observer_html({}),
                            elem_classes=["native-upload-readiness-html"],
                        )
                        embedding_observer_log = gr.Textbox(
                            label="Observation history",
                            value="No observation baseline has been recorded.",
                            lines=5,
                            interactive=False,
                        )
                    with gr.Accordion("Workspace verification", open=False, elem_classes=["native-upload-subaccordion"]):
                        gr.Markdown(
                            "A concise, read-only terminal check. It separates embedded-vector evidence from the "
                            "AnythingLLM document-list state so an empty Documents pane is never silently treated as a failed embedding."
                        )
                        verify_current_workspace_button = gr.Button("Verify current workspace")
                        workspace_verification = gr.HTML(
                            value='<div class="artifact-placeholder">Select a workspace, then verify its current storage and runtime evidence.</div>'
                        )
                    with gr.Accordion("AnythingLLM native metadata contract", open=False, elem_classes=["native-upload-subaccordion"]):
                        metadata_schema_status = gr.Textbox(
                            label="AnythingLLM native metadata contract",
                            value=metadata_contract_text(),
                            lines=12,
                            interactive=False,
                            show_label=False,
                        )
                    with gr.Accordion("Workspace storage inspector", open=False, elem_classes=["native-upload-subaccordion"]):
                        inspect_workspace_button = gr.Button("Inspect selected workspace")
                        workspace_inspector = gr.HTML(value="")
                    with gr.Accordion("Workspace maintenance and storage audit", open=False, elem_classes=["native-upload-subaccordion"]):
                        run_storage_audit_button = gr.Button("Run read-only storage audit")
                        storage_audit = gr.HTML(
                            value=(
                                '<div class="artifact-placeholder"><strong>Storage audit has not run.</strong>'
                                "<br>Select a workspace if you want a scoped report, then run the audit.</div>"
                            ),
                        )
                        run_stale_artifact_report_button = gr.Button("Generate dry-run stale-artifact repair plan")
                        stale_artifact_report = gr.HTML(
                            value=(
                                '<div class="artifact-placeholder"><strong>Dry-run stale-artifact repair plan has not run.</strong>'
                                "<br>This report is read-only and only proposes candidate cleanup buckets and review order.</div>"
                            ),
                        )
                    with gr.Accordion("Run history and recovery", open=False, elem_classes=["native-upload-subaccordion"]):
                        refresh_ingestion_history_button = gr.Button("Refresh ingestion history")
                        ingestion_history = gr.HTML(value=ingestion_history_html())
                        refresh_resume_manifest_button = gr.Button("Check interrupted embedding recovery")
                        resume_manifest_status = gr.HTML(value=latest_resume_manifest_html(INITIAL_WORKSPACE_VALUE))
                        resume_embedding_button = gr.Button("Resume pending embedding batches", variant="secondary")
                        recovery_policy = gr.Radio(
                            choices=list(RECOVERY_POLICY_LABELS),
                            value="Leave everything running",
                            label="Interrupted-run recovery action",
                            info="Default is non-destructive. Queue cleanup runs only with positive evidence that the active queue belongs to this app run.",
                        )
                        recovery_restart_confirmation = gr.Checkbox(
                            value=False,
                            label="I understand that restarting AnythingLLM can interrupt other Desktop work",
                        )
                        apply_recovery_policy_button = gr.Button("Apply recovery action", variant="secondary")
                    with gr.Accordion("ETA model and timing diagnostics", open=False, elem_classes=["native-upload-subaccordion"]):
                        refresh_timing_model_button = gr.Button("Refresh ETA model")
                        timing_model_status = gr.HTML(value=timing_model_html())

            with gr.Accordion("Retrieval simulation", open=False, elem_classes=["top-level-accordion"]) as retrieval_simulation_section:
                with gr.Row(elem_classes=["control-row"]):
                    ollama_url = gr.Textbox(
                        label="Ollama URL",
                        value=DEFAULT_OLLAMA_URL,
                        placeholder=DEFAULT_OLLAMA_URL,
                        lines=1,
                        max_lines=1,
                    )
                local_check_mode = gr.Dropdown(
                    choices=INITIAL_SIMULATION_CHOICES,
                    value=INITIAL_SIMULATION_VALUE,
                    label="Simulation embedder",
                    interactive=True,
                    allow_custom_value=False,
                    elem_id="simulation-model-dropdown",
                )
                simulation_auto_refresh_button = gr.Button("Auto refresh simulation models", visible=False, elem_id="simulation-model-auto-refresh")
                custom_ollama_model = gr.Textbox(value="", visible=False)
                simulation_status = gr.Textbox(
                    label="Simulation embedder status",
                    value=INITIAL_SIMULATION_STATUS,
                    lines=5,
                    interactive=False,
                )
                vector_audit_scope = gr.Dropdown(
                    choices=["Full corpus", "Focused (up to 300 chunks)"],
                    value="Full corpus",
                    label="Simulation workload",
                    interactive=True,
                )
            with gr.Accordion("Advanced preparation overrides", open=False, elem_classes=["top-level-accordion"]) as automatic_advanced_section:
                with gr.Group(elem_id="anythingllm-run-api-controls") as anythingllm_run_api_controls:
                    with gr.Row():
                        api_url = gr.Textbox(
                            label="AnythingLLM API URL",
                            value=DEFAULT_ANYTHINGLLM_API_URL,
                            placeholder=DEFAULT_ANYTHINGLLM_API_URL,
                            lines=1,
                            max_lines=1,
                        )
                        api_key = gr.Textbox(
                            label="AnythingLLM API key (optional for local Desktop)",
                            type="password",
                            placeholder="Leave blank to use a temporary local Desktop key",
                            lines=1,
                            max_lines=1,
                        )
                backend_mode = gr.Dropdown(
                    choices=["Automatic", "PyMuPDF", "PyMuPDF4LLM", "Unstructured"],
                    value="Automatic",
                    label="Extraction backend",
                    info="Controls whether the app prefers plain-text extraction, Markdown-like extraction, or a heavier layout-aware fallback.",
                    interactive=True,
                    # Automatic is deliberately fixed for normal runs: it
                    # begins with PyMuPDF and escalates only when extraction
                    # evidence warrants a heavier backend.  Retain the
                    # hidden component for stable callback contracts and old
                    # browser sessions, but do not expose a manual override.
                    visible=False,
                )
                extraction_backend_help_box = gr.HTML(
                    value=extraction_backend_help("Automatic"),
                    visible=False,
                )
                with gr.Row():
                    first_page_override = gr.Number(
                        value=0,
                        precision=0,
                        minimum=0,
                        label="First PDF page override",
                        info="0 uses automatic detection.",
                    )
                    end_page_override = gr.Number(
                        value=0,
                        precision=0,
                        minimum=0,
                        label="End-matter start override",
                        info="0 uses automatic detection; this page is excluded.",
                    )
                target_passage_length_policy = gr.Radio(
                    choices=[TARGET_PASSAGE_INHERIT_LABEL, TARGET_PASSAGE_CUSTOM_LABEL],
                    value=TARGET_PASSAGE_INHERIT_LABEL,
                    label="Target passage length policy",
                    info="Inherited is the safe default. Choose a custom target only when you deliberately want to override the mode-aware recommendation.",
                )
                target_passage_length = gr.Dropdown(
                    choices=TARGET_PASSAGE_LENGTH_PRESET_CHOICES,
                    value=str(DEFAULT_TARGET_PASSAGE_LENGTH),
                    allow_custom_value=True,
                    label="Target passage length",
                    info="Primary target for passage-style segmentation. Segmentation mode establishes boundaries first; this setting only sizes the passages inside that boundary contract.",
                    interactive=False,
                )
                page_preserve_ceiling = gr.Number(
                    value=0,
                    precision=0,
                    minimum=0,
                    label="Page-preserve safety ceiling (characters)",
                    info="0 follows the active AnythingLLM Text Chunk Size and embedder safety limit. A lower value splits only within each source page; it never changes AnythingLLM settings.",
                )
                target_passage_length_warning = gr.HTML(
                    value=(
                        '<div class="setting-reference-note"><em>Target sizing will be checked against the active '
                        "AnythingLLM splitter and embedder when this page loads.</em></div>"
                    ),
                )
                inherit_anythingllm_settings = gr.Checkbox(
                    value=True,
                    label="Inherit current AnythingLLM chunk and embedder limits",
                )
                refresh_anythingllm_settings_button = gr.Button("Refresh current AnythingLLM settings")
                anythingllm_settings_snapshot = gr.HTML(
                    value=(
                        '<div class="setting-reference-note"><em>Live AnythingLLM settings have not been '
                        "queried. Use “Refresh current AnythingLLM settings” to inspect them.</em></div>"
                    ),
                )
                anythingllm_reference_values = gr.HTML(
                    value=(
                        '<div class="setting-reference-note"><em>Recommendations will be calculated after '
                        "the current AnythingLLM state is refreshed.</em></div>"
                    ),
                )
                with gr.Row(elem_classes=["aligned-settings-row"]) as anythingllm_chunk_controls:
                    anythingllm_chunk_size = gr.Dropdown(
                        choices=CHUNK_SIZE_PRESET_CHOICES,
                        value=current_anythingllm_chunk_size_value(),
                        label="AnythingLLM chunk size",
                        info="Used when inheritance is off. Click for tested presets or type a whole number.",
                        allow_custom_value=True,
                        interactive=True,
                    )
                    anythingllm_chunk_overlap = gr.Dropdown(
                        choices=CHUNK_OVERLAP_PRESET_CHOICES,
                        value=current_anythingllm_chunk_overlap_value(),
                        label="AnythingLLM chunk overlap",
                        info="Used when inheritance is off. Click for tested presets or type a whole number.",
                        allow_custom_value=True,
                        interactive=True,
                    )
                with gr.Row(elem_classes=["aligned-settings-row"]) as anythingllm_embedder_limit_controls:
                    anythingllm_embedder_max_chunk = gr.Number(
                        value=current_anythingllm_embedder_max_chunk_value(),
                        precision=0,
                        minimum=1,
                        label="AnythingLLM embedder max chunk limit",
                        info="Writes EMBEDDING_MODEL_MAX_CHUNK_LENGTH in AnythingLLM storage .env.",
                    )
                    anythingllm_embedder_recommended_limit = gr.Number(
                        value=current_anythingllm_recommended_embedder_limit_value(),
                        precision=0,
                        minimum=1,
                        label="Recommended embedder max chunk limit",
                        info="Model-aware recommendation from the localhost capability registry.",
                        interactive=False,
                    )
                    save_anythingllm_embedder_max_chunk_button = gr.Button(
                        "Save embedder max chunk limit to AnythingLLM",
                        elem_classes=["aligned-action-button"],
                    )
                with gr.Row() as anythingllm_settings_actions:
                    save_anythingllm_chunk_settings_button = gr.Button(
                        "Save chunk size and overlap to AnythingLLM",
                        elem_classes=["aligned-action-button"],
                    )
                    apply_recommended_settings_button = gr.Button(
                        "Apply recommended AnythingLLM settings",
                        elem_classes=["aligned-action-button"],
                    )
                    apply_tested_retrieval_preset_button = gr.Button(
                        "Apply tested retrieval preset (768 / 128)",
                        elem_classes=["aligned-action-button"],
                    )
                    auto_apply_before_run = gr.Checkbox(
                        value=False,
                        label="Auto-apply before upload run",
                        info="Known models only. Changes shared AnythingLLM settings before an upload run.",
                        elem_id="auto-apply-before-run",
                    )
                with gr.Row(elem_classes=["aligned-settings-row"]) as anythingllm_embedder_model_controls:
                    anythingllm_embedder_engine = gr.Dropdown(
                        choices=ANYTHINGLLM_EMBEDDER_ENGINE_CHOICES,
                        value=current_anythingllm_engine_value(),
                        label="AnythingLLM embedder engine",
                        allow_custom_value=True,
                        info="Examples: openrouter, ollama, generic-openai, gemini.",
                    )
                    anythingllm_embedder_model = gr.Dropdown(
                        choices=anythingllm_embedder_model_choices(
                            current_anythingllm_engine_value(),
                            current_anythingllm_effective_model_value(),
                        ),
                        value=current_anythingllm_effective_model_value(),
                        label="AnythingLLM embedder model",
                        allow_custom_value=True,
                        info="Writes EMBEDDING_MODEL_PREF for the selected AnythingLLM embedder engine.",
                        elem_id="anythingllm-embedder-model-dropdown",
                    )
                    anythingllm_embedder_model_auto_refresh_button = gr.Button(
                        "Auto refresh AnythingLLM embedder models",
                        visible=False,
                        elem_id="anythingllm-embedder-model-auto-refresh",
                    )
                with gr.Row() as anythingllm_embedder_save_controls:
                    save_anythingllm_embedder_engine_button = gr.Button("Save embedder engine and model to AnythingLLM")
                anythingllm_embedder_limit_status = gr.Textbox(
                    label="AnythingLLM settings update status",
                    value="",
                    lines=3,
                    interactive=False,
                )
                advanced_end_section_names = gr.Textbox(
                    value="\n".join(DEFAULT_END_SECTION_HEADINGS),
                    label="End-matter headings",
                    lines=6,
                )
                automatic_validation_phrases = gr.Textbox(
                    value="",
                    label="Additional exact validation phrases",
                    placeholder="Optional, one phrase per line",
                    lines=5,
                )
                unstructured_strategy = gr.Dropdown(
                    choices=["auto", "fast", "hi_res", "ocr_only"],
                    value="auto",
                    label="Unstructured strategy",
                    info="Auto prefers fast layout partitioning for normal text PDFs and escalates to OCR-heavy mode only when the file looks difficult and OCR is available.",
                    interactive=True,
                )
                generate_inline_fallback = gr.Checkbox(
                    value=True,
                    label="Generate inline metadata fallback files",
                )
                retain_detailed_evidence = gr.Checkbox(
                    value=False,
                    label="Retain detailed evidence even if this run is ready",
                    info="Keeps the embedding ledger, receipts, and verification evidence for audit. It does not change processing.",
                )
            with gr.Group(elem_id="automatic-run-flow"):
                # Keep one permanent action row. Changing a Row's visibility
                # during an async callback previously left stacked actions in
                # the UI, so only the buttons' state changes.
                with gr.Group(elem_id="automatic-run-confirmation") as automatic_confirmation:
                    auto_confirmation_html = gr.HTML(value="")
                with gr.Row(elem_id="automatic-actions", elem_classes=["run-actions"]) as automatic_normal_actions:
                    auto_button = gr.Button(
                        "Retired review action",
                        variant="secondary",
                        scale=5,
                        min_width=0,
                        interactive=False,
                        elem_id="automatic-process-button",
                        visible=False,
                    )
                    confirm_automatic_run_button = gr.Button(
                        "Confirm and start processing",
                        variant="primary",
                        scale=5,
                        min_width=0,
                        interactive=False,
                        elem_id="confirm-automatic-run-button",
                    )
                    cancel_automatic_run_button = gr.Button(
                        "Cancel",
                        variant="secondary",
                        scale=1,
                        min_width=0,
                        interactive=False,
                        elem_id="cancel-automatic-run-button",
                        visible=False,
                    )
                auto_confirmation_state = gr.State({})
                background_reconciliation_timer = gr.Timer(
                    BACKGROUND_RECONCILIATION_INTERVAL_SECONDS,
                    active=True,
                )
                anythingllm_startup_status_timer = gr.Timer(
                    ANYTHINGLLM_STARTUP_STATUS_INTERVAL_SECONDS,
                    active=True,
                )
                # This ticker drives the visible ETA from durable server state.
                # It is a clock, not a request-duration display, so one second
                # is the smallest useful refresh interval.
                automatic_run_status_timer = gr.Timer(1, active=True)
                automatic_run_activity = gr.HTML(
                    value=automatic_live_status_html({"state": "ready"}),
                    visible=True,
                    elem_classes=["automatic-run-activity-host"],
                )
                auto_run_timing = gr.HTML(
                    value=automatic_run_timing_html(state="ready"),
                    elem_classes=["automatic-run-timing-host"],
                )
                automatic_run_failure = gr.HTML(
                    value="",
                    visible=False,
                    elem_id="automatic-run-failure",
                )
            with gr.Row():
                open_generated_output_button = gr.Button(
                    "Open Generated Output Folder",
                    interactive=False,
                    visible=False,
                    elem_id="open-generated-output-button",
                )
            auto_download_state = gr.State([])
            with gr.Accordion(
                "Run output and downloads",
                open=False,
                elem_id="run-output-downloads",
                elem_classes=["top-level-accordion", "output-downloads-accordion"],
            ) as run_output_downloads_section:
                auto_summary = gr.HTML(value="", visible=False, elem_classes=["automatic-run-summary"])
                with gr.Group(elem_id="automatic-download-section", elem_classes=["automatic-download-section"]):
                    with gr.Row(elem_classes=["downloads-header-row"]):
                        gr.HTML('<div class="downloads-header-title">Prepared text files</div>')
                        auto_download_full_folder = gr.Checkbox(
                            value=False,
                            label="Download Full Folder",
                            visible=False,
                            elem_classes=["download-folder-control"],
                            scale=1,
                            min_width=150,
                        )
                        auto_download_segments_folder = gr.Checkbox(
                            value=False,
                            label="Download Segments Folder",
                            visible=False,
                            elem_classes=["download-folder-control"],
                            scale=1,
                            min_width=170,
                        )
                    auto_artifacts = gr.HTML(
                        value=artifact_placeholder_html("Prepared output package"),
                        elem_classes=["downloads-artifacts-html"],
                    )
                    auto_files = gr.File(label="Downloads", file_count="multiple", visible=False, show_label=False)
            with gr.Accordion("Segment preview", open=False, elem_classes=["top-level-accordion"]):
                with gr.Row(elem_classes=["control-row"]):
                    previous_segment = gr.Button("←", size="sm", min_width=48)
                    segment_number = gr.Number(
                        label="Segment number",
                        value=1,
                        precision=0,
                        minimum=1,
                        step=1,
                    )
                    next_segment = gr.Button("→", size="sm", min_width=48)
                segment_preview = gr.Textbox(
                    label="Segment text",
                    value="Run the PDF pipeline first, then type a segment number.",
                    lines=14,
                    interactive=False,
                )
                segment_storage_preview = gr.Textbox(
                    label="AnythingLLM storage match",
                    value="After native upload, this shows the matching workspace_documents record, custom-documents JSON, and a sample LanceDB row for the selected segment.",
                    lines=16,
                    interactive=False,
                )

            refresh_workspace_button.click(
                fn=refresh_workspaces_with_readiness,
                inputs=[api_url, api_key, workspace_slug],
                outputs=[workspace_slug, workspace_status, native_upload_readiness],
                show_progress="hidden",
                queue=False,
            )
            refresh_anythingllm_startup_status_refresh = refresh_anythingllm_startup_status_button.click(
                fn=refresh_anythingllm_startup_status,
                inputs=[api_url],
                outputs=[
                    anythingllm_startup_status,
                    anythingllm_startup_status_module,
                    refresh_anythingllm_startup_status_button,
                ],
                show_progress="hidden",
                queue=False,
                trigger_mode="always_last",
                js=ANYTHINGLLM_STARTUP_STATUS_REFRESH_JS,
            )
            inspect_workspace_button.click(
                fn=workspace_inspector_html,
                inputs=workspace_slug,
                outputs=workspace_inspector,
                show_progress="hidden",
            )
            verify_current_workspace_button.click(
                fn=workspace_verification_card_html,
                inputs=[api_url, workspace_slug],
                outputs=workspace_verification,
                show_progress="hidden",
                queue=False,
            )
            refresh_ingestion_history_button.click(
                fn=ingestion_history_html,
                inputs=workspace_slug,
                outputs=ingestion_history,
                show_progress="hidden",
                queue=False,
            )
            refresh_timing_model_button.click(
                fn=timing_model_html,
                outputs=timing_model_status,
                show_progress="hidden",
                queue=False,
            )
            refresh_resume_manifest_button.click(
                fn=latest_resume_manifest_html,
                inputs=workspace_slug,
                outputs=resume_manifest_status,
                show_progress="hidden",
                queue=False,
            )
            resume_embedding_button.click(
                fn=resume_latest_embedding_manifest,
                inputs=[api_url, api_key, workspace_slug],
                outputs=[resume_manifest_status, resume_embedding_button],
                show_progress="full",
            )
            apply_recovery_policy_button.click(
                fn=apply_latest_recovery_policy,
                inputs=[api_url, api_key, workspace_slug, recovery_policy, recovery_restart_confirmation],
                outputs=[resume_manifest_status, apply_recovery_policy_button],
                show_progress="full",
            )
            run_storage_audit_button.click(
                fn=storage_audit_html,
                inputs=workspace_slug,
                outputs=storage_audit,
                show_progress="hidden",
            )
            run_stale_artifact_report_button.click(
                fn=stale_artifact_report_html,
                inputs=workspace_slug,
                outputs=stale_artifact_report,
                show_progress="hidden",
            )
            start_embedding_observer_button.click(
                fn=start_embedding_observer,
                inputs=[api_url, workspace_slug, embedding_expected_records],
                outputs=[embedding_observer_state, embedding_observer_status, embedding_observer_log],
                show_progress="hidden",
                queue=False,
            )
            sample_embedding_observer_button.click(
                fn=sample_embedding_observer,
                inputs=[api_url, workspace_slug, embedding_expected_records, embedding_observer_state],
                outputs=[embedding_observer_state, embedding_observer_status, embedding_observer_log],
                show_progress="hidden",
                queue=False,
            )
            background_reconciliation_timer.tick(
                fn=refresh_background_reconciliation,
                inputs=[
                    api_url,
                    api_key,
                    workspace_slug,
                    inherit_anythingllm_settings,
                    anythingllm_chunk_size,
                    anythingllm_chunk_overlap,
                    anythingllm_embedder_max_chunk,
                ],
                outputs=[
                    workspace_slug,
                    workspace_status,
                    native_upload_readiness,
                    background_reconciliation_status,
                    anythingllm_settings_snapshot,
                    anythingllm_chunk_size,
                    anythingllm_chunk_overlap,
                    anythingllm_embedder_max_chunk,
                    anythingllm_embedder_recommended_limit,
                    anythingllm_embedder_engine,
                    anythingllm_embedder_model,
                    anythingllm_reference_values,
                ],
                show_progress="hidden",
                queue=False,
                trigger_mode="always_last",
            )
            anythingllm_startup_status_timer.tick(
                fn=anythingllm_startup_status_view,
                inputs=[api_url],
                outputs=[anythingllm_startup_status, anythingllm_startup_status_module],
                show_progress="hidden",
                queue=False,
                trigger_mode="always_last",
            )
            automatic_run_status_timer.tick(
                fn=refresh_live_automatic_run_ui,
                inputs=None,
                outputs=[
                    automatic_run_activity,
                    auto_run_timing,
                    auto_button,
                    confirm_automatic_run_button,
                    automatic_confirmation,
                    automatic_normal_actions,
                    cancel_automatic_run_button,
                    automatic_run_failure,
                    auto_summary,
                    open_generated_output_button,
                ],
                show_progress="hidden",
                queue=False,
                trigger_mode="always_last",
            )
            refresh_schema_button.click(
                fn=lambda api_url, api_key: refresh_metadata_schema(api_url, api_key, autostart_runtime=True),
                inputs=[api_url, api_key],
                outputs=metadata_schema_status,
                show_progress="hidden",
                queue=False,
            )
            expand_all_button.click(
                fn=None,
                inputs=None,
                outputs=None,
                js=EXPAND_ALL_CLICK_JS,
                queue=False,
                show_progress="hidden",
            )
            demo.load(
                fn=initialize_anythingllm_on_app_open,
                inputs=[api_url, api_key, workspace_slug],
                outputs=[
                    workspace_slug,
                    workspace_status,
                    native_upload_readiness,
                    anythingllm_startup_status,
                    anythingllm_startup_status_module,
                ],
                show_progress="minimal",
            )
            fresh_run_presentation_outputs = [
                automatic_run_activity,
                auto_run_timing,
                auto_button,
                confirm_automatic_run_button,
                automatic_confirmation,
                auto_confirmation_html,
                auto_confirmation_state,
                automatic_normal_actions,
                cancel_automatic_run_button,
                automatic_run_failure,
                auto_summary,
                auto_files,
                auto_artifacts,
                auto_download_state,
                open_generated_output_button,
                run_output_downloads_section,
                segment_number,
                segment_preview,
                segment_storage_preview,
            ]
            fresh_run_settings_outputs = [
                auto_label,
                auto_author,
                auto_short_label,
                auto_use_file_title_fallback,
                auto_mode,
                output_root_override,
                api_url,
                api_key,
                workspace_slug,
                new_workspace_name,
                new_workspace_name_auto_state,
                native_upload_scope,
                native_upload_custom_range,
                native_boundary_policy,
                native_metadata_mode,
                anythingllm_create_document_folders,
                anythingllm_document_folder_name,
                local_check_mode,
                custom_ollama_model,
                ollama_url,
                vector_audit_scope,
                deep_extraction,
                include_front_matter,
                include_back_matter,
                segment_mode,
                backend_mode,
                first_page_override,
                end_page_override,
                target_passage_length_policy,
                target_passage_length,
                page_preserve_ceiling,
                inherit_anythingllm_settings,
                anythingllm_chunk_size,
                anythingllm_chunk_overlap,
                auto_apply_before_run,
                auto_download_full_folder,
                auto_download_segments_folder,
                advanced_end_section_names,
                automatic_validation_phrases,
                unstructured_strategy,
                generate_inline_fallback,
            ]
            automatic_mode_ui_outputs = [
                anythingllm_output_root,
                native_metadata_section,
                api_url,
                api_key,
                inherit_anythingllm_settings,
                refresh_anythingllm_settings_button,
                anythingllm_settings_snapshot,
                anythingllm_reference_values,
                anythingllm_chunk_controls,
                anythingllm_embedder_limit_controls,
                anythingllm_settings_actions,
                anythingllm_embedder_model_controls,
                anythingllm_embedder_save_controls,
                anythingllm_embedder_limit_status,
                local_only_mode_notice,
            ]
            # A terminal result belongs to the browser session that ran it.
            # Without this load-time reset, a subsequent fresh page could be
            # reconciled into a prior terminal failure even though it has no
            # selected PDFs (the exact stale "AUTO-RUN-RECONCILED-001" state
            # shown in the UI regression).  Running work is deliberately
            # preserved by reset_automatic_run_presentation.
            demo.load(
                fn=reset_automatic_run_presentation,
                inputs=None,
                outputs=fresh_run_presentation_outputs,
                show_progress="hidden",
                queue=False,
            )
            automatic_run_inputs = [
                auto_pdfs,
                auto_folder_pdfs,
                auto_label,
                auto_author,
                auto_short_label,
                auto_use_file_title_fallback,
                auto_mode,
                output_root_override,
                api_url,
                api_key,
                workspace_slug,
                native_upload_scope,
                native_upload_custom_range,
                native_metadata_mode,
                anythingllm_create_document_folders,
                anythingllm_document_folder_name,
                local_check_mode,
                custom_ollama_model,
                ollama_url,
                vector_audit_scope,
                deep_extraction,
                include_front_matter,
                include_back_matter,
                backend_mode,
                first_page_override,
                end_page_override,
                target_passage_length,
                page_preserve_ceiling,
                segment_mode,
                advanced_end_section_names,
                automatic_validation_phrases,
                unstructured_strategy,
                generate_inline_fallback,
                inherit_anythingllm_settings,
                anythingllm_chunk_size,
                anythingllm_chunk_overlap,
                auto_apply_before_run,
                auto_download_full_folder,
                auto_download_segments_folder,
                new_workspace_name,
                retain_detailed_evidence,
            ]
            automatic_timer_inputs = [
                auto_pdfs,
                auto_folder_pdfs,
                auto_mode,
                native_upload_scope,
                workspace_slug,
                segment_mode,
                target_passage_length,
                anythingllm_chunk_size,
                anythingllm_chunk_overlap,
                backend_mode,
                unstructured_strategy,
                local_check_mode,
                auto_folder_manifest,
                api_url,
                inherit_anythingllm_settings,
            ]
            workspace_slug.change(
                fn=workspace_inspector_html,
                inputs=workspace_slug,
                outputs=workspace_inspector,
                show_progress="hidden",
                queue=False,
            )
            native_upload_scope.change(
                fn=native_upload_scope_batch_guard,
                inputs=[native_upload_scope, auto_pdfs, auto_folder_pdfs, segment_mode],
                outputs=[native_upload_scope, native_upload_custom_range],
                show_progress="hidden",
                queue=False,
            )
            workspace_slug.change(
                fn=lambda api_url, api_key, workspace_slug: refresh_native_upload_readiness(
                    api_url,
                    api_key,
                    workspace_slug,
                    autostart_runtime=False,
                ),
                inputs=[api_url, api_key, workspace_slug],
                outputs=[native_upload_readiness],
                show_progress="hidden",
                queue=False,
            )
            workspace_slug.change(
                fn=update_new_workspace_name_control,
                inputs=[workspace_slug, auto_label, auto_pdfs, new_workspace_name, new_workspace_name_auto_state],
                outputs=[new_workspace_name, new_workspace_name_auto_state],
                show_progress="hidden",
                queue=False,
            )
            auto_label.change(
                fn=update_new_workspace_name_control,
                inputs=[workspace_slug, auto_label, auto_pdfs, new_workspace_name, new_workspace_name_auto_state],
                outputs=[new_workspace_name, new_workspace_name_auto_state],
                show_progress="hidden",
                queue=False,
            )
            api_url.change(
                fn=lambda api_url, api_key, workspace_slug: refresh_native_upload_readiness(
                    api_url,
                    api_key,
                    workspace_slug,
                    autostart_runtime=False,
                ),
                inputs=[api_url, api_key, workspace_slug],
                outputs=[native_upload_readiness],
                show_progress="hidden",
                queue=False,
            )
            api_url.change(
                fn=anythingllm_startup_status_view,
                inputs=[api_url],
                outputs=[anythingllm_startup_status, anythingllm_startup_status_module],
                show_progress="hidden",
                queue=False,
            )
            api_key.change(
                fn=lambda api_url, api_key, workspace_slug: refresh_native_upload_readiness(
                    api_url,
                    api_key,
                    workspace_slug,
                    autostart_runtime=False,
                ),
                inputs=[api_url, api_key, workspace_slug],
                outputs=[native_upload_readiness],
                show_progress="hidden",
                queue=False,
            )
            # A file selection begins a new run, never a visual continuation
            # of the last one.  The chained metadata refresh is intentionally
            # last so cleared manual metadata cannot leak onto the new PDF.
            auto_pdfs.change(
                fn=merge_uploaded_pdfs_into_folder_batch,
                inputs=[auto_pdfs, auto_folder_manifest],
                outputs=[
                    auto_pdfs,
                    auto_folder_pdfs,
                    auto_folder_manifest,
                    auto_folder_file_selector,
                    batch_folder_selection_panel,
                    auto_folder_status,
                ],
                show_progress="minimal",
                queue=True,
                concurrency_limit=1,
                concurrency_id="automatic-native-page-inspection",
            ).then(
                fn=reset_automatic_run_presentation,
                inputs=[auto_pdfs, auto_folder_pdfs],
                outputs=fresh_run_presentation_outputs,
                show_progress="hidden",
                queue=False,
            ).then(
                fn=metadata_selection_layout_state,
                inputs=[auto_pdfs, auto_folder_pdfs],
                outputs=[document_metadata_section],
                show_progress="hidden",
                queue=False,
            ).then(
                fn=reset_automatic_run_settings_to_defaults,
                inputs=None,
                outputs=fresh_run_settings_outputs,
                show_progress="hidden",
                queue=False,
            ).then(
                fn=native_upload_scope_batch_guard,
                inputs=[native_upload_scope, auto_pdfs, auto_folder_pdfs, segment_mode],
                outputs=[native_upload_scope, native_upload_custom_range],
                show_progress="hidden",
                queue=False,
            ).then(
                fn=automatic_selection_pending_action_states,
                inputs=[auto_pdfs, auto_folder_pdfs],
                outputs=[confirm_automatic_run_button, cancel_automatic_run_button],
                show_progress="hidden",
                queue=False,
            ).then(
                fn=refresh_automatic_run_estimate_for_fresh_selection,
                inputs=[auto_pdfs, auto_folder_pdfs, auto_folder_manifest],
                outputs=[auto_run_timing],
                show_progress="hidden",
                queue=False,
            ).then(
                fn=automatic_mode_ui_updates,
                inputs=[auto_mode],
                outputs=automatic_mode_ui_outputs,
                show_progress="hidden",
                queue=False,
            ).then(
                # Metadata can take a moment on a large PDF. Set the safe
                # ready action state first so a late metadata callback cannot
                # leave Cancel interactive while Confirm is already enabled.
                fn=automatic_selection_action_states,
                inputs=[auto_pdfs, auto_folder_pdfs, auto_folder_manifest],
                outputs=[
                    confirm_automatic_run_button,
                    cancel_automatic_run_button,
                    automatic_run_activity,
                ],
                show_progress="hidden",
                queue=False,
            ).then(
                fn=automatic_detected_metadata_preview,
                inputs=[auto_pdfs, auto_folder_pdfs, auto_label, auto_author, auto_short_label, auto_use_file_title_fallback, auto_folder_manifest],
                outputs=[
                    auto_label,
                    auto_author,
                    auto_short_label,
                    auto_metadata_preview,
                    document_metadata_section,
                ],
                show_progress="hidden",
            ).then(
                fn=update_new_workspace_name_control,
                inputs=[workspace_slug, auto_label, auto_pdfs, new_workspace_name, new_workspace_name_auto_state],
                outputs=[new_workspace_name, new_workspace_name_auto_state],
                show_progress="hidden",
                queue=False,
            ).then(
                # Confirm is enabled only after automatic metadata and the
                # derived workspace name have settled. Otherwise an operator
                # can submit a transient blank/default snapshot.
                fn=automatic_selection_action_states,
                inputs=[auto_pdfs, auto_folder_pdfs, auto_folder_manifest],
                outputs=[
                    confirm_automatic_run_button,
                    cancel_automatic_run_button,
                    automatic_run_activity,
                ],
                show_progress="hidden",
                queue=False,
            )
            choose_pdf_folder_button.click(
                fn=choose_pdf_input_directory_for_scan,
                inputs=[auto_folder_path],
                outputs=[
                    auto_folder_path,
                    auto_folder_scan_requested,
                    auto_folder_status,
                    pdf_folder_picker_area,
                    choose_pdf_folder_button,
                ],
                show_progress="minimal",
                queue=False,
            ).then(
                fn=stream_selected_pdf_directory,
                inputs=[auto_folder_path, auto_folder_scan_requested],
                outputs=[
                    auto_folder_pdfs,
                    auto_folder_manifest,
                    auto_folder_file_selector,
                    batch_folder_selection_panel,
                    auto_folder_status,
                    pdf_folder_picker_area,
                    choose_pdf_folder_button,
                ],
                show_progress="minimal",
                queue=True,
                concurrency_limit=1,
                concurrency_id="automatic-folder-scan",
            ).then(
                fn=reset_automatic_run_presentation,
                inputs=[auto_pdfs, auto_folder_pdfs],
                outputs=fresh_run_presentation_outputs,
                show_progress="hidden",
                queue=False,
            ).then(
                fn=metadata_selection_layout_state,
                inputs=[auto_pdfs, auto_folder_pdfs],
                outputs=[document_metadata_section],
                show_progress="hidden",
                queue=False,
            ).then(
                fn=reset_automatic_run_settings_to_defaults,
                inputs=None,
                outputs=fresh_run_settings_outputs,
                show_progress="hidden",
                queue=False,
            ).then(
                fn=native_upload_scope_batch_guard,
                inputs=[native_upload_scope, auto_pdfs, auto_folder_pdfs, segment_mode],
                outputs=[native_upload_scope, native_upload_custom_range],
                show_progress="hidden",
                queue=False,
            ).then(
                fn=automatic_selection_pending_action_states,
                inputs=[auto_pdfs, auto_folder_pdfs],
                outputs=[confirm_automatic_run_button, cancel_automatic_run_button],
                show_progress="hidden",
                queue=False,
            ).then(
                fn=refresh_automatic_run_estimate_for_fresh_selection,
                inputs=[auto_pdfs, auto_folder_pdfs, auto_folder_manifest],
                outputs=[auto_run_timing],
                show_progress="hidden",
                queue=False,
            ).then(
                fn=automatic_mode_ui_updates,
                inputs=[auto_mode],
                outputs=automatic_mode_ui_outputs,
                show_progress="hidden",
                queue=False,
            ).then(
                fn=folder_detected_metadata_preview,
                inputs=[auto_folder_pdfs, auto_label, auto_author, auto_short_label, auto_use_file_title_fallback, auto_folder_manifest],
                outputs=[
                    auto_label,
                    auto_author,
                    auto_short_label,
                    auto_metadata_preview,
                    document_metadata_section,
                ],
                show_progress="hidden",
            ).then(
                fn=update_new_workspace_name_control,
                inputs=[workspace_slug, auto_label, auto_pdfs, new_workspace_name, new_workspace_name_auto_state],
                outputs=[new_workspace_name, new_workspace_name_auto_state],
                show_progress="hidden",
                queue=False,
            ).then(
                fn=automatic_selection_action_states,
                inputs=[auto_pdfs, auto_folder_pdfs, auto_folder_manifest],
                outputs=[
                    confirm_automatic_run_button,
                    cancel_automatic_run_button,
                    automatic_run_activity,
                ],
                show_progress="hidden",
                queue=False,
            )
            auto_folder_file_selector.input(
                fn=apply_batch_folder_file_selection,
                inputs=[auto_folder_manifest, auto_folder_file_selector],
                outputs=[
                    auto_folder_pdfs,
                    auto_folder_manifest,
                    auto_folder_file_selector,
                    auto_folder_status,
                ],
                show_progress="hidden",
                queue=False,
            ).then(
                fn=native_upload_scope_batch_guard,
                inputs=[native_upload_scope, auto_pdfs, auto_folder_pdfs, segment_mode],
                outputs=[native_upload_scope, native_upload_custom_range],
                show_progress="hidden",
                queue=False,
            ).then(
                fn=folder_detected_metadata_preview,
                inputs=[auto_folder_pdfs, auto_label, auto_author, auto_short_label, auto_use_file_title_fallback, auto_folder_manifest],
                outputs=[
                    auto_label,
                    auto_author,
                    auto_short_label,
                    auto_metadata_preview,
                    document_metadata_section,
                ],
                show_progress="hidden",
                queue=False,
            ).then(
                fn=update_new_workspace_name_control,
                inputs=[workspace_slug, auto_label, auto_pdfs, new_workspace_name, new_workspace_name_auto_state],
                outputs=[new_workspace_name, new_workspace_name_auto_state],
                show_progress="hidden",
                queue=False,
            ).then(
                fn=refresh_automatic_run_estimate_for_fresh_selection,
                inputs=[auto_pdfs, auto_folder_pdfs, auto_folder_manifest],
                outputs=[auto_run_timing],
                show_progress="hidden",
                queue=False,
            ).then(
                fn=automatic_selection_action_states,
                inputs=[auto_pdfs, auto_folder_pdfs, auto_folder_manifest],
                outputs=[
                    confirm_automatic_run_button,
                    cancel_automatic_run_button,
                    automatic_run_activity,
                ],
                show_progress="hidden",
                queue=False,
            )
            auto_use_file_title_fallback.change(
                fn=detected_metadata_preview,
                inputs=[auto_pdfs, auto_label, auto_author, auto_short_label, auto_use_file_title_fallback],
                outputs=[
                    auto_label,
                    auto_author,
                    auto_short_label,
                    auto_metadata_preview,
                    document_metadata_section,
                ],
                show_progress="hidden",
            )
            backend_mode.change(
                fn=extraction_backend_help,
                inputs=[backend_mode],
                outputs=[extraction_backend_help_box],
                show_progress="hidden",
                queue=False,
            )
            refresh_metadata_button.click(
                fn=detected_metadata_preview,
                inputs=[auto_pdfs, auto_label, auto_author, auto_short_label, auto_use_file_title_fallback],
                outputs=[
                    auto_label,
                    auto_author,
                    auto_short_label,
                    auto_metadata_preview,
                    document_metadata_section,
                ],
                show_progress="hidden",
            )
            local_check_mode.change(
                fn=describe_simulation_choice,
                inputs=[local_check_mode, custom_ollama_model],
                outputs=simulation_status,
                show_progress="hidden",
                queue=False,
            )
            simulation_auto_refresh_button.click(
                fn=refresh_simulation_embedders,
                inputs=[ollama_url, local_check_mode],
                outputs=[local_check_mode, simulation_status],
                show_progress="hidden",
                queue=False,
            )
            demo.load(
                fn=load_simulation_embedders_on_open,
                inputs=None,
                outputs=[local_check_mode, simulation_status],
            )
            refresh_anythingllm_settings_button.click(
                fn=refresh_anythingllm_settings,
                inputs=[inherit_anythingllm_settings, anythingllm_chunk_size, anythingllm_chunk_overlap],
                outputs=[
                    anythingllm_settings_snapshot,
                    anythingllm_chunk_size,
                    anythingllm_chunk_overlap,
                    anythingllm_embedder_max_chunk,
                    anythingllm_embedder_recommended_limit,
                    anythingllm_embedder_engine,
                    anythingllm_embedder_model,
                ],
                show_progress="hidden",
                queue=False,
            ).then(
                fn=anythingllm_settings_reference_html,
                inputs=None,
                outputs=[anythingllm_reference_values],
                show_progress="hidden",
                queue=False,
            )
            inherit_anythingllm_settings.change(
                fn=refresh_anythingllm_settings,
                inputs=[inherit_anythingllm_settings, anythingllm_chunk_size, anythingllm_chunk_overlap],
                outputs=[
                    anythingllm_settings_snapshot,
                    anythingllm_chunk_size,
                    anythingllm_chunk_overlap,
                    anythingllm_embedder_max_chunk,
                    anythingllm_embedder_recommended_limit,
                    anythingllm_embedder_engine,
                    anythingllm_embedder_model,
                ],
                show_progress="hidden",
                queue=False,
            ).then(
                fn=anythingllm_settings_reference_html,
                inputs=None,
                outputs=[anythingllm_reference_values],
                show_progress="hidden",
                queue=False,
            )
            demo.load(
                fn=lambda: refresh_anythingllm_settings(True, 0, -1),
                inputs=None,
                outputs=[
                    anythingllm_settings_snapshot,
                    anythingllm_chunk_size,
                    anythingllm_chunk_overlap,
                    anythingllm_embedder_max_chunk,
                    anythingllm_embedder_recommended_limit,
                    anythingllm_embedder_engine,
                    anythingllm_embedder_model,
                ],
            ).then(
                fn=anythingllm_settings_reference_html,
                inputs=None,
                outputs=[anythingllm_reference_values],
            )
            save_anythingllm_embedder_max_chunk_button.click(
                fn=save_anythingllm_embedder_max_chunk_limit,
                inputs=[
                    anythingllm_embedder_max_chunk,
                    inherit_anythingllm_settings,
                    anythingllm_chunk_size,
                    anythingllm_chunk_overlap,
                ],
                outputs=[
                    anythingllm_settings_snapshot,
                    anythingllm_chunk_size,
                    anythingllm_chunk_overlap,
                    anythingllm_embedder_max_chunk,
                    anythingllm_embedder_recommended_limit,
                    anythingllm_embedder_engine,
                    anythingllm_embedder_model,
                    anythingllm_embedder_limit_status,
                ],
                show_progress="hidden",
                queue=False,
            ).then(
                fn=anythingllm_settings_reference_html,
                inputs=None,
                outputs=[anythingllm_reference_values],
                show_progress="hidden",
                queue=False,
            )
            save_anythingllm_chunk_settings_button.click(
                fn=save_anythingllm_chunk_settings,
                inputs=[
                    anythingllm_chunk_size,
                    anythingllm_chunk_overlap,
                    inherit_anythingllm_settings,
                    anythingllm_embedder_max_chunk,
                ],
                outputs=[
                    anythingllm_settings_snapshot,
                    anythingllm_chunk_size,
                    anythingllm_chunk_overlap,
                    anythingllm_embedder_max_chunk,
                    anythingllm_embedder_recommended_limit,
                    anythingllm_embedder_engine,
                    anythingllm_embedder_model,
                    anythingllm_embedder_limit_status,
                ],
                show_progress="hidden",
                queue=False,
            ).then(
                fn=anythingllm_settings_reference_html,
                inputs=None,
                outputs=[anythingllm_reference_values],
                show_progress="hidden",
                queue=False,
            )
            apply_recommended_settings_button.click(
                fn=apply_recommended_anythingllm_settings_ui,
                inputs=[inherit_anythingllm_settings],
                outputs=[
                    anythingllm_settings_snapshot,
                    anythingllm_chunk_size,
                    anythingllm_chunk_overlap,
                    anythingllm_embedder_max_chunk,
                    anythingllm_embedder_recommended_limit,
                    anythingllm_embedder_engine,
                    anythingllm_embedder_model,
                    anythingllm_embedder_limit_status,
                ],
                show_progress="hidden",
                queue=False,
            ).then(
                fn=anythingllm_settings_reference_html,
                inputs=None,
                outputs=[anythingllm_reference_values],
                show_progress="hidden",
                queue=False,
            )
            apply_tested_retrieval_preset_button.click(
                fn=apply_tested_retrieval_preset_ui,
                inputs=[inherit_anythingllm_settings, anythingllm_embedder_max_chunk],
                outputs=[
                    anythingllm_settings_snapshot,
                    anythingllm_chunk_size,
                    anythingllm_chunk_overlap,
                    anythingllm_embedder_max_chunk,
                    anythingllm_embedder_recommended_limit,
                    anythingllm_embedder_engine,
                    anythingllm_embedder_model,
                    anythingllm_embedder_limit_status,
                ],
                show_progress="hidden",
                queue=False,
            ).then(
                fn=anythingllm_settings_reference_html,
                inputs=None,
                outputs=[anythingllm_reference_values],
                show_progress="hidden",
                queue=False,
            )
            anythingllm_embedder_engine.change(
                fn=refresh_anythingllm_embedder_model_controls,
                inputs=[
                    anythingllm_embedder_engine,
                    anythingllm_embedder_model,
                    anythingllm_embedder_max_chunk,
                ],
                outputs=[
                    anythingllm_embedder_model,
                    anythingllm_embedder_max_chunk,
                    anythingllm_embedder_recommended_limit,
                    anythingllm_embedder_limit_status,
                ],
                show_progress="hidden",
                queue=False,
            )
            anythingllm_embedder_model_auto_refresh_button.click(
                fn=refresh_anythingllm_embedder_model_controls,
                inputs=[
                    anythingllm_embedder_engine,
                    anythingllm_embedder_model,
                    anythingllm_embedder_max_chunk,
                ],
                outputs=[
                    anythingllm_embedder_model,
                    anythingllm_embedder_max_chunk,
                    anythingllm_embedder_recommended_limit,
                    anythingllm_embedder_limit_status,
                ],
                show_progress="hidden",
                queue=False,
            )
            anythingllm_embedder_model.change(
                fn=preview_anythingllm_embedder_policy,
                inputs=[
                    anythingllm_embedder_engine,
                    anythingllm_embedder_model,
                    anythingllm_embedder_max_chunk,
                ],
                outputs=[
                    anythingllm_embedder_max_chunk,
                    anythingllm_embedder_recommended_limit,
                    anythingllm_embedder_limit_status,
                ],
                show_progress="hidden",
                queue=False,
            )
            save_anythingllm_embedder_engine_button.click(
                fn=save_anythingllm_embedder_engine_model,
                inputs=[
                    anythingllm_embedder_engine,
                    anythingllm_embedder_model,
                    inherit_anythingllm_settings,
                    anythingllm_chunk_size,
                    anythingllm_chunk_overlap,
                    anythingllm_embedder_max_chunk,
                ],
                outputs=[
                    anythingllm_settings_snapshot,
                    anythingllm_chunk_size,
                    anythingllm_chunk_overlap,
                    anythingllm_embedder_max_chunk,
                    anythingllm_embedder_recommended_limit,
                    anythingllm_embedder_engine,
                    anythingllm_embedder_model,
                    anythingllm_embedder_limit_status,
                ],
                show_progress="hidden",
                queue=False,
            ).then(
                fn=anythingllm_settings_reference_html,
                inputs=None,
                outputs=[anythingllm_reference_values],
                show_progress="hidden",
                queue=False,
            )
            demo.load(
                fn=initial_automatic_section_state,
                inputs=None,
                outputs=[
                    document_metadata_section,
                    extraction_options_section,
                    native_metadata_section,
                    retrieval_simulation_section,
                    automatic_advanced_section,
                ],
            )
            auto_mode.change(
                fn=automatic_mode_ui_updates,
                inputs=[auto_mode],
                outputs=automatic_mode_ui_outputs,
                show_progress="hidden",
                queue=False,
            )
            # Advanced children are lazy-mounted when this accordion opens.
            # Reapply the active mode at that boundary so local-only cannot
            # reveal an upload-only control with stale visibility.
            automatic_advanced_section.expand(
                fn=automatic_mode_ui_updates,
                inputs=[auto_mode],
                outputs=automatic_mode_ui_outputs,
                show_progress="hidden",
                queue=False,
            )

            # Keep the timer estimate in sync with the input set and every
            # run-defining visible choice. The callback does no extraction,
            # workspace mutation, or upload work.
            for timer_setting in [
                auto_mode,
                native_upload_scope,
                workspace_slug,
                segment_mode,
                target_passage_length,
                anythingllm_chunk_size,
                anythingllm_chunk_overlap,
                backend_mode,
                unstructured_strategy,
                local_check_mode,
                api_url,
                inherit_anythingllm_settings,
            ]:
                timer_setting.change(
                    fn=refresh_automatic_run_estimate,
                    inputs=automatic_timer_inputs,
                    outputs=[auto_run_timing],
                    show_progress="hidden",
                    queue=False,
                )
            confirm_automatic_run_button.click(
                fn=run_automatic_from_confirmation_stream,
                # Do not rely on gr.State for this irreversible action.  A
                # normal control payload makes the Confirm click observable by
                # the server and resilient to a stale client-side State value.
                inputs=automatic_run_inputs,
                outputs=[
                    auto_summary,
                    auto_files,
                    auto_artifacts,
                    auto_download_state,
                    auto_button,
                    native_upload_readiness,
                    auto_run_timing,
                    workspace_slug,
                    automatic_run_failure,
                    confirm_automatic_run_button,
                    cancel_automatic_run_button,
                    open_generated_output_button,
                ],
                show_progress="full",
                show_progress_on=auto_summary,
            ).then(
                fn=lambda: gr.update(value=""),
                inputs=None,
                outputs=[auto_confirmation_html],
                show_progress="hidden",
                queue=False,
            ).then(
                fn=automatic_action_row_updates,
                inputs=None,
                outputs=[
                    automatic_normal_actions,
                    confirm_automatic_run_button,
                    cancel_automatic_run_button,
                ],
                show_progress="hidden",
                queue=False,
            ).then(
                fn=refresh_background_reconciliation,
                inputs=[
                    api_url,
                    api_key,
                    workspace_slug,
                    inherit_anythingllm_settings,
                    anythingllm_chunk_size,
                    anythingllm_chunk_overlap,
                    anythingllm_embedder_max_chunk,
                ],
                outputs=[
                    workspace_slug,
                    workspace_status,
                    native_upload_readiness,
                    background_reconciliation_status,
                    anythingllm_settings_snapshot,
                    anythingllm_chunk_size,
                    anythingllm_chunk_overlap,
                    anythingllm_embedder_max_chunk,
                    anythingllm_embedder_recommended_limit,
                    anythingllm_embedder_engine,
                    anythingllm_embedder_model,
                    anythingllm_reference_values,
                ],
                show_progress="hidden",
                queue=False,
            )
            cancel_automatic_run_button.click(
                fn=cancel_or_reset_automatic_run,
                inputs=[auto_pdfs, auto_folder_pdfs, auto_folder_manifest, automatic_run_activity],
                outputs=[
                    auto_confirmation_html,
                    auto_confirmation_state,
                    auto_run_timing,
                    confirm_automatic_run_button,
                    automatic_normal_actions,
                    auto_button,
                    cancel_automatic_run_button,
                    automatic_run_failure,
                    open_generated_output_button,
                ],
                show_progress="hidden",
                queue=False,
                concurrency_limit=None,
                concurrency_id="automatic-cancel",
            )
            choose_output_root_button.click(
                fn=choose_output_directory,
                inputs=[output_root_override],
                outputs=[output_root_override],
                show_progress="hidden",
                queue=False,
            )
            open_output_root_button.click(
                fn=open_output_directory,
                inputs=[output_root_override],
                outputs=[output_root_status],
                show_progress="hidden",
                queue=False,
            )
            open_generated_output_button.click(
                fn=open_generated_output_directory,
                inputs=[auto_download_state, output_root_override],
                outputs=[output_root_status],
                show_progress="hidden",
                queue=False,
            )
            reset_output_root_button.click(
                fn=reset_output_directory,
                inputs=None,
                outputs=[output_root_override],
                show_progress="hidden",
                queue=False,
            )
            auto_download_full_folder.change(
                fn=download_files_update,
                inputs=[auto_download_state, auto_download_full_folder, auto_download_segments_folder],
                outputs=[auto_files],
                show_progress="hidden",
                queue=False,
            )
            auto_download_segments_folder.change(
                fn=download_files_update,
                inputs=[auto_download_state, auto_download_full_folder, auto_download_segments_folder],
                outputs=[auto_files],
                show_progress="hidden",
                queue=False,
            )
            segment_mode.change(
                fn=target_passage_length_policy_update,
                inputs=[
                    target_passage_length_policy,
                    segment_mode,
                    target_passage_length,
                    inherit_anythingllm_settings,
                    anythingllm_chunk_size,
                    anythingllm_chunk_overlap,
                ],
                outputs=[target_passage_length, target_passage_length_warning],
                show_progress="hidden",
                queue=False,
            )
            segment_mode.change(
                fn=page_preserve_ceiling_control_update,
                inputs=[segment_mode],
                outputs=[page_preserve_ceiling],
                show_progress="hidden",
                queue=False,
            )
            segment_mode.change(
                fn=native_upload_scope_batch_guard,
                inputs=[native_upload_scope, auto_pdfs, auto_folder_pdfs, segment_mode],
                outputs=[native_upload_scope, native_upload_custom_range],
                show_progress="hidden",
                queue=False,
            )
            target_passage_length_policy.change(
                fn=target_passage_length_policy_update,
                inputs=[
                    target_passage_length_policy,
                    segment_mode,
                    target_passage_length,
                    inherit_anythingllm_settings,
                    anythingllm_chunk_size,
                    anythingllm_chunk_overlap,
                ],
                outputs=[target_passage_length, target_passage_length_warning],
                show_progress="hidden",
                queue=False,
            )
            target_passage_length.change(
                fn=target_passage_length_policy_update,
                inputs=[
                    target_passage_length_policy,
                    segment_mode,
                    target_passage_length,
                    inherit_anythingllm_settings,
                    anythingllm_chunk_size,
                    anythingllm_chunk_overlap,
                ],
                outputs=[target_passage_length, target_passage_length_warning],
                show_progress="hidden",
                queue=False,
            )
            page_preserve_ceiling.change(
                fn=lambda segment_mode_value, target_value, ceiling, inherit, chunk_size, chunk_overlap: target_passage_length_warning_html(
                    target_passage_sizing_plan(
                        segment_mode_value,
                        TARGET_PASSAGE_CUSTOM_LABEL,
                        target_value,
                        inherit,
                        chunk_size,
                        chunk_overlap,
                        page_preserve_ceiling=ceiling,
                    )
                ),
                inputs=[
                    segment_mode,
                    target_passage_length,
                    page_preserve_ceiling,
                    inherit_anythingllm_settings,
                    anythingllm_chunk_size,
                    anythingllm_chunk_overlap,
                ],
                outputs=[target_passage_length_warning],
                show_progress="hidden",
                queue=False,
            )
            native_boundary_policy.change(
                fn=native_upload_boundary_policy_and_timer_update,
                inputs=[
                    native_boundary_policy,
                    target_passage_length,
                    auto_pdfs,
                    auto_folder_pdfs,
                    auto_mode,
                    native_upload_scope,
                    workspace_slug,
                    segment_mode,
                    anythingllm_chunk_size,
                    anythingllm_chunk_overlap,
                    backend_mode,
                    unstructured_strategy,
                ],
                outputs=[
                    segment_mode,
                    target_passage_length,
                    inherit_anythingllm_settings,
                    anythingllm_chunk_overlap,
                    native_boundary_policy_note,
                    auto_run_timing,
                ],
                show_progress="hidden",
                queue=False,
            ).then(
                fn=target_passage_length_policy_update,
                inputs=[
                    target_passage_length_policy,
                    segment_mode,
                    target_passage_length,
                    inherit_anythingllm_settings,
                    anythingllm_chunk_size,
                    anythingllm_chunk_overlap,
                ],
                outputs=[target_passage_length, target_passage_length_warning],
                show_progress="hidden",
                queue=False,
            )
            inherit_anythingllm_settings.change(
                fn=target_passage_length_policy_update,
                inputs=[
                    target_passage_length_policy,
                    segment_mode,
                    target_passage_length,
                    inherit_anythingllm_settings,
                    anythingllm_chunk_size,
                    anythingllm_chunk_overlap,
                ],
                outputs=[target_passage_length, target_passage_length_warning],
                show_progress="hidden",
                queue=False,
            )
            anythingllm_chunk_size.change(
                fn=target_passage_length_policy_update,
                inputs=[
                    target_passage_length_policy,
                    segment_mode,
                    target_passage_length,
                    inherit_anythingllm_settings,
                    anythingllm_chunk_size,
                    anythingllm_chunk_overlap,
                ],
                outputs=[target_passage_length, target_passage_length_warning],
                show_progress="hidden",
                queue=False,
            )
            anythingllm_chunk_overlap.change(
                fn=target_passage_length_policy_update,
                inputs=[
                    target_passage_length_policy,
                    segment_mode,
                    target_passage_length,
                    inherit_anythingllm_settings,
                    anythingllm_chunk_size,
                    anythingllm_chunk_overlap,
                ],
                outputs=[target_passage_length, target_passage_length_warning],
                show_progress="hidden",
                queue=False,
            )
            demo.load(
                fn=target_passage_length_policy_update,
                inputs=[
                    target_passage_length_policy,
                    segment_mode,
                    target_passage_length,
                    inherit_anythingllm_settings,
                    anythingllm_chunk_size,
                    anythingllm_chunk_overlap,
                ],
                outputs=[target_passage_length, target_passage_length_warning],
            )
            demo.load(
                fn=automatic_section_open_state,
                inputs=None,
                outputs=[
                    extraction_options_section,
                    native_metadata_section,
                    workspace_selection_section,
                ],
            )
            segment_number.change(
                fn=preview_manifest_segment,
                inputs=[auto_download_state, segment_number],
                outputs=segment_preview,
                show_progress="hidden",
                queue=False,
            )
            segment_number.change(
                fn=preview_workspace_segment,
                inputs=[auto_download_state, workspace_slug, segment_number],
                outputs=segment_storage_preview,
                show_progress="hidden",
                queue=False,
            )
            segment_number.submit(
                fn=preview_manifest_segment,
                inputs=[auto_download_state, segment_number],
                outputs=segment_preview,
                show_progress="hidden",
                queue=False,
            )
            segment_number.submit(
                fn=preview_workspace_segment,
                inputs=[auto_download_state, workspace_slug, segment_number],
                outputs=segment_storage_preview,
                show_progress="hidden",
                queue=False,
            )
            previous_segment.click(
                fn=lambda paths, workspace, number: navigate_manifest_segment_with_storage(paths, workspace, number, -1),
                inputs=[auto_download_state, workspace_slug, segment_number],
                outputs=[segment_number, segment_preview, segment_storage_preview],
                show_progress="hidden",
                queue=False,
            )
            next_segment.click(
                fn=lambda paths, workspace, number: navigate_manifest_segment_with_storage(paths, workspace, number, 1),
                inputs=[auto_download_state, workspace_slug, segment_number],
                outputs=[segment_number, segment_preview, segment_storage_preview],
                show_progress="hidden",
                queue=False,
            )
            workspace_slug.change(
                fn=preview_workspace_segment,
                inputs=[auto_download_state, workspace_slug, segment_number],
                outputs=segment_storage_preview,
                show_progress="hidden",
                queue=False,
            )

        with gr.Tab("Advanced"):
            with gr.Row(elem_classes=["advanced-app-meta"]):
                gr.HTML(
                    f'<div class="advanced-version">Version {APP_VERSION} · base version {APP_BASE_COMMIT}</div>'
                )
                with gr.Row(elem_classes=["theme-controls"]):
                    theme_follow_system_toggle = gr.Checkbox(
                        label="Follow Windows system theme",
                        value=True,
                        interactive=True,
                        container=False,
                        elem_id="follow-windows-theme",
                    )
                    theme_toggle_button = gr.Button(
                        "Light / Dark",
                        variant="secondary",
                        size="sm",
                        min_width=96,
                        elem_id="theme-toggle-button",
                    )
            theme_follow_system_toggle.change(
                fn=None,
                inputs=[theme_follow_system_toggle],
                outputs=None,
                js=THEME_FOLLOW_SYSTEM_JS,
                queue=False,
                show_progress="hidden",
            )
            theme_toggle_button.click(
                fn=None,
                inputs=None,
                outputs=None,
                js=THEME_TOGGLE_JS,
                queue=False,
                show_progress="hidden",
            )
            with gr.Row(elem_classes=["advanced-output-root-controls"]):
                advanced_output_root_override = gr.Textbox(
                    label="Advanced diagnostic output folder",
                    value=str(ADVANCED_DIAGNOSTICS_OUTPUT_DIR),
                    lines=1,
                    max_lines=1,
                    scale=4,
                )
                choose_advanced_output_root_button = gr.Button(
                    "Choose Advanced output folder",
                    scale=1,
                )
            with gr.Accordion("Completed-run diagnostics", open=True, elem_classes=["top-level-accordion"]):
                diagnostics_run_directory = gr.Textbox(
                    label="Completed run folder",
                    value="",
                    placeholder="Click to choose a folder containing run-summary.json",
                    lines=1,
                    elem_id="diagnostics-run-directory-input",
                )
                choose_diagnostics_run_directory_button = gr.Button(
                    "Choose completed run folder",
                    elem_id="choose-diagnostics-run-directory-button",
                )
                with gr.Row(elem_classes=["completed-diagnostics-actions"]):
                    use_latest_diagnostics_run = gr.Button(
                        "Use latest Automatic run",
                        elem_classes=["completed-diagnostics-action"],
                    )
                    use_latest_advanced_diagnostics_run = gr.Button(
                        "Use latest Advanced diagnostic run",
                        elem_classes=["completed-diagnostics-action"],
                    )
                    show_retained_diagnostics = gr.Button(
                        "Show diagnostics",
                        elem_classes=["completed-diagnostics-action"],
                    )
                diagnostics_summary = gr.HTML(
                    value="",
                    visible=False,
                    elem_classes=["run-diagnostics-summary-host"],
                )

            gr.HTML('<div class="advanced-new-run-divider">New Diagnostics Run</div>')
            with gr.Row(elem_classes=["advanced-source-row"]):
                with gr.Column(elem_classes=["advanced-source-card", "advanced-pdf-card"]):
                    advanced_pdf = gr.File(
                        label="PDF for diagnostic run",
                        file_types=[".pdf"],
                        type="filepath",
                        height=320,
                        elem_id="advanced-pdf-upload",
                    )
                    advanced_input_status = gr.HTML(
                        value="",
                        visible=False,
                        elem_classes=["advanced-pdf-warning-host"],
                    )
                advanced_backend = gr.Radio(
                    choices=ADVANCED_BACKEND_CHOICES,
                    value=ADVANCED_BACKEND_AUTOMATIC_LABEL,
                    label="Extraction policy",
                    elem_classes=["advanced-source-card"],
                )
            advanced_run_result = gr.HTML(
                value="",
                visible=False,
                elem_classes=["run-diagnostics-summary-host"],
            )
            advanced_run_status = gr.HTML(
                value="",
                visible=False,
                elem_classes=["advanced-run-status-host"],
            )

            with gr.Accordion("Document metadata", open=True, elem_classes=["top-level-accordion"]):
                with gr.Row():
                    advanced_document_title = gr.Textbox(
                        label="Document title",
                        placeholder="Derived from PDF metadata or filename; edit if needed",
                        lines=1,
                        max_lines=1,
                    )
                    advanced_document_author = gr.Textbox(
                        label="Author",
                        placeholder="Derived from PDF metadata or title-page text; edit if needed",
                        lines=1,
                        max_lines=1,
                    )
                advanced_document_short_label = gr.Textbox(
                    label="Short citation label",
                    placeholder="Derived from author/title; used in provenance metadata",
                    lines=1,
                    max_lines=1,
                )
                advanced_use_file_title_fallback = gr.Checkbox(
                    value=True,
                    label="Use the file title as a fallback",
                )
                advanced_metadata_preview = gr.HTML(
                    value="",
                    elem_classes=["document-metadata-preview"],
                )

            with gr.Accordion("Extraction, boundaries, and segmentation", open=True, elem_classes=["top-level-accordion"]):
                with gr.Row(elem_classes=["extraction-options-row"]):
                    advanced_deep_extraction = gr.Checkbox(
                        value=False,
                        label="Force Unstructured (instead of automatic)",
                        min_width=0,
                        scale=1,
                    )
                    advanced_include_front_matter = gr.Checkbox(value=True, label="Include foreword/preface", min_width=0, scale=1)
                    advanced_include_back_matter = gr.Checkbox(value=True, label="Include notes/bibliography/index", min_width=0, scale=1)
                advanced_unstructured_strategy = gr.Dropdown(
                    choices=["auto", "fast", "hi_res", "ocr_only"],
                    value="auto",
                    label="Unstructured strategy",
                )
                with gr.Row(elem_classes=["advanced-numeric-row"]):
                    advanced_first_page_override = gr.Number(
                        value=0,
                        precision=0,
                    minimum=0,
                    label="First PDF page override",
                    )
                    advanced_end_page_override = gr.Number(
                        value=0,
                        precision=0,
                    minimum=0,
                    label="End-matter start override",
                    )
                advanced_end_section_names = gr.Textbox(
                    value="\n".join(DEFAULT_END_SECTION_HEADINGS),
                    lines=6,
                    label="End-matter headings",
                )
                advanced_segment_mode = gr.Dropdown(
                    choices=[
                        SEGMENT_NONE_LABEL,
                        SEGMENT_PASSAGES_LABEL,
                        SEGMENT_PAGE_ONLY_LABEL,
                        SEGMENT_PAGE_LIMIT_LABEL,
                        SEGMENT_PAGE_PASSAGES_LABEL,
                    ],
                    value=SEGMENT_PAGE_LIMIT_LABEL,
                    label="Segmentation mode",
                )
                advanced_target_passage_policy = gr.Radio(
                    choices=[TARGET_PASSAGE_INHERIT_LABEL, TARGET_PASSAGE_CUSTOM_LABEL],
                    value=TARGET_PASSAGE_INHERIT_LABEL,
                    label="Target passage length policy",
                )
                advanced_target_passage_length = gr.Dropdown(
                    choices=TARGET_PASSAGE_LENGTH_PRESET_CHOICES,
                    value=str(DEFAULT_TARGET_PASSAGE_LENGTH),
                    allow_custom_value=True,
                    label="Target passage length",
                    interactive=False,
                )
                advanced_target_passage_warning = gr.HTML(
                    value=target_passage_length_warning_html(
                        target_passage_sizing_plan(
                            SEGMENT_PAGE_LIMIT_LABEL,
                            TARGET_PASSAGE_INHERIT_LABEL,
                        )
                    ),
                )
                advanced_inherit_anythingllm_settings = gr.State(True)
                advanced_anythingllm_chunk_size = gr.State(0)
                advanced_anythingllm_chunk_overlap = gr.State(0)

            with gr.Accordion("Validation and evidence", open=False, elem_classes=["top-level-accordion"]):
                advanced_validation_phrases = gr.Textbox(
                    lines=5,
                    label="Additional exact validation phrases",
                )
                advanced_retain_detailed_evidence = gr.Checkbox(
                    value=False,
                    label="Retain detailed evidence even if this run is ready",
                )

            advanced_run_button = gr.Button(
                "Generate diagnostic extraction",
                variant="primary",
                interactive=False,
            )
            advanced_output_directory = gr.Textbox(
                label="Advanced diagnostic output folder",
                value="",
                interactive=False,
                visible=False,
            )
            advanced_files = gr.File(
                label="Prepared text file",
                file_count="multiple",
                visible=False,
                elem_id="advanced-prepared-text-file",
            )

            advanced_pdf.change(
                fn=advanced_diagnostic_pdf_selection_update,
                inputs=[
                    advanced_pdf,
                    advanced_document_title,
                    advanced_document_author,
                    advanced_document_short_label,
                ],
                outputs=[
                    advanced_document_title,
                    advanced_document_author,
                    advanced_document_short_label,
                    advanced_metadata_preview,
                    advanced_input_status,
                ],
                show_progress="minimal",
            ).then(
                fn=advanced_diagnostic_action_state,
                inputs=[advanced_pdf],
                outputs=[advanced_run_button],
                show_progress="hidden",
                queue=False,
            ).then(
                fn=advanced_diagnostic_idle_status,
                inputs=None,
                outputs=[advanced_run_status],
                show_progress="hidden",
                queue=False,
            )
            for advanced_target_source in (
                advanced_segment_mode,
                advanced_target_passage_policy,
                advanced_target_passage_length,
            ):
                advanced_target_source.change(
                    fn=target_passage_length_policy_update,
                    inputs=[
                        advanced_target_passage_policy,
                        advanced_segment_mode,
                        advanced_target_passage_length,
                        advanced_inherit_anythingllm_settings,
                        advanced_anythingllm_chunk_size,
                        advanced_anythingllm_chunk_overlap,
                    ],
                    outputs=[advanced_target_passage_length, advanced_target_passage_warning],
                    show_progress="hidden",
                    queue=False,
                )

            advanced_run_button.click(
                fn=advanced_diagnostic_running_status,
                inputs=None,
                outputs=[advanced_run_status, advanced_run_button],
                show_progress="hidden",
                queue=False,
            ).then(
                fn=run_advanced_diagnostics,
                inputs=[
                    advanced_pdf,
                    advanced_output_root_override,
                    advanced_document_title,
                    advanced_document_author,
                    advanced_document_short_label,
                    advanced_use_file_title_fallback,
                    advanced_backend,
                    advanced_deep_extraction,
                    advanced_include_front_matter,
                    advanced_include_back_matter,
                    advanced_first_page_override,
                    advanced_end_page_override,
                    advanced_segment_mode,
                    advanced_target_passage_policy,
                    advanced_target_passage_length,
                    advanced_end_section_names,
                    advanced_validation_phrases,
                    advanced_unstructured_strategy,
                    advanced_retain_detailed_evidence,
                ],
                outputs=[
                    advanced_run_result,
                    advanced_output_directory,
                    advanced_files,
                    advanced_run_status,
                    advanced_run_button,
                ],
            )
            choose_diagnostics_run_directory_button.click(
                fn=choose_output_directory,
                inputs=[diagnostics_run_directory],
                outputs=[diagnostics_run_directory],
                show_progress="hidden",
                queue=False,
            )
            choose_advanced_output_root_button.click(
                fn=choose_output_directory,
                inputs=[advanced_output_root_override],
                outputs=[advanced_output_root_override],
                show_progress="hidden",
                queue=False,
            )
            use_latest_diagnostics_run.click(
                fn=latest_automatic_pdf_output_directory,
                inputs=None,
                outputs=[diagnostics_run_directory],
                show_progress="hidden",
                queue=False,
            )
            use_latest_advanced_diagnostics_run.click(
                fn=latest_advanced_diagnostics_output_directory,
                inputs=[advanced_output_root_override],
                outputs=[diagnostics_run_directory],
                show_progress="hidden",
                queue=False,
            )
            show_retained_diagnostics.click(
                fn=retained_run_diagnostics_update,
                inputs=[diagnostics_run_directory],
                outputs=[diagnostics_summary],
                show_progress="hidden",
            )


def launch_application(*, port: int | None = None, inbrowser: bool = False):
    """Launch the UI through either direct Python or the pipx console app."""

    selected_port = port if port is not None else int(os.environ.get("GRADIO_SERVER_PORT", "7860"))
    demo.launch(
        _app=gradio_server_app_with_connection_watchdog(),
        server_name="127.0.0.1",
        server_port=selected_port,
        inbrowser=inbrowser,
        theme=APP_THEME,
        css=APP_CSS,
        js=APP_JS,
        favicon_path=APP_ICON,
        footer_links=[],
    )


if __name__ == "__main__":
    launch_application()

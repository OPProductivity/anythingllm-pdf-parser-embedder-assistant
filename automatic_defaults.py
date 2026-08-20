"""Private, versioned saved defaults for future Automatic runs.

This module deliberately knows nothing about Gradio or a currently selected
PDF.  A saved profile is a small per-user preference document, not a recovery
snapshot and not an AnythingLLM connection profile.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from portable_paths import application_paths


AUTOMATIC_DEFAULTS_SCHEMA_VERSION = 1
AUTOMATIC_DEFAULTS_FILENAME = "automatic-defaults.json"

# Browser callbacks share one application process.  The revision check and
# replacement must be one critical section, otherwise two sessions can both
# accept the same revision and the later write silently loses the first.
_AUTOMATIC_DEFAULTS_WRITE_LOCK = threading.RLock()

# Keep this list deliberately narrower than the Automatic form. It keeps
# reusable preparation, connection, workspace-target, simulation, and output
# preferences, but excludes source selection/metadata, secrets, per-document
# names and upload ranges, shared-provider mutation, and all current-run or
# recovery state.
PERSISTABLE_AUTOMATIC_DEFAULT_FIELDS = frozenset(
    {
        "use_file_title_fallback",
        "mode",
        "output_root_override",
        "api_url",
        "workspace_slug",
        "native_upload_scope",
        "native_metadata_mode",
        "anythingllm_create_document_folders",
        "existing_workspace_duplicate_policy",
        "local_check_mode",
        "ollama_url",
        "vector_audit_scope",
        "deep_extraction",
        "include_front_matter",
        "include_back_matter",
        "backend_mode",
        "first_page_override",
        "end_page_override",
        "target_passage_length",
        "page_preserve_ceiling",
        "segment_mode",
        "custom_page_group_sizes",
        "advanced_end_section_names",
        "automatic_validation_phrases",
        "unstructured_strategy",
        "generate_inline_fallback",
        "inherit_anythingllm_settings",
        "anythingllm_chunk_size",
        "anythingllm_chunk_overlap",
        "download_full_folder",
        "download_segments_folder",
    }
)


def automatic_defaults_path(home_directory: str | Path | None = None) -> Path:
    """Return the private profile path without creating it."""

    root = Path(home_directory) if home_directory is not None else application_paths()["root"]
    return root / "settings" / AUTOMATIC_DEFAULTS_FILENAME


def persistable_automatic_defaults(values: dict[str, Any], builtin_defaults: dict[str, Any]) -> dict[str, Any]:
    """Return only safe, type-compatible future-run preferences."""

    result: dict[str, Any] = {}
    for field in PERSISTABLE_AUTOMATIC_DEFAULT_FIELDS:
        if field not in values or field not in builtin_defaults:
            continue
        value = values[field]
        default = builtin_defaults[field]
        # ``bool`` is an ``int`` subclass, so require its exact type before
        # accepting numeric controls.
        if type(value) is type(default):
            result[field] = value
    return result


def _backup_profile(path: Path, reason: str) -> Path | None:
    if not path.is_file():
        return None
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.stem}.{reason}-{timestamp}{path.suffix}")
    suffix = 1
    while backup.exists():
        backup = path.with_name(f"{path.stem}.{reason}-{timestamp}-{suffix}{path.suffix}")
        suffix += 1
    try:
        shutil.copy2(path, backup)
    except OSError:
        return None
    return backup


def _read_profile(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, "corrupt"
    if not isinstance(raw, dict):
        return None, "corrupt"
    if raw.get("schema_version") != AUTOMATIC_DEFAULTS_SCHEMA_VERSION:
        return None, "unsupported"
    if not isinstance(raw.get("revision"), int) or raw["revision"] < 0:
        return None, "corrupt"
    if not isinstance(raw.get("defaults"), dict):
        return None, "corrupt"
    return raw, None


def load_automatic_defaults(
    builtin_defaults: dict[str, Any], *, home_directory: str | Path | None = None
) -> dict[str, Any]:
    """Load a profile, safely falling back to built-ins with an operator notice."""

    path = automatic_defaults_path(home_directory)
    if not path.exists():
        return {"defaults": {}, "revision": 0, "notice": "", "path": path}
    profile, issue = _read_profile(path)
    if issue:
        backup = _backup_profile(path, issue)
        detail = f" A backup was saved to {backup.name}." if backup else ""
        return {
            "defaults": {},
            "revision": 0,
            "notice": f"Saved Automatic defaults were {issue}; built-in defaults are in use.{detail}",
            "path": path,
        }
    assert profile is not None
    return {
        "defaults": persistable_automatic_defaults(profile["defaults"], builtin_defaults),
        "revision": profile["revision"],
        "notice": "",
        "path": path,
    }


def save_automatic_defaults(
    values: dict[str, Any],
    builtin_defaults: dict[str, Any],
    *,
    expected_revision: int,
    home_directory: str | Path | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Atomically save defaults, rejecting a concurrently changed profile."""

    path = automatic_defaults_path(home_directory)
    with _AUTOMATIC_DEFAULTS_WRITE_LOCK:
        current = load_automatic_defaults(builtin_defaults, home_directory=home_directory)
        current_revision = int(current["revision"])
        if current_revision != expected_revision and not overwrite:
            return {
                "status": "conflict",
                "revision": current_revision,
                "message": "Saved Automatic defaults changed in another browser session. Reload them or explicitly overwrite them.",
            }
        profile = {
            "schema_version": AUTOMATIC_DEFAULTS_SCHEMA_VERSION,
            "revision": current_revision + 1,
            "defaults": persistable_automatic_defaults(values, builtin_defaults),
        }
        temporary_path: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
            ) as handle:
                json.dump(profile, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
                temporary_path = Path(handle.name)
            os.replace(temporary_path, path)
        except OSError as exc:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            return {
                "status": "error",
                "revision": current_revision,
                "message": f"Could not save Automatic defaults: {exc}",
            }
        return {
            "status": "saved",
            "revision": profile["revision"],
            "message": "Saved Automatic defaults for future selections.",
        }

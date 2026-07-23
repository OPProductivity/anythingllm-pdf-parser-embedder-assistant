"""User-neutral filesystem locations for the distributable application.

The installed package itself is read-only from the application's point of
view.  Generated PDFs, logs, timing observations, and any optional local
configuration therefore belong in the current user's data directory instead
of beside the Python modules.
"""

from __future__ import annotations

import os
import sys
import sysconfig
from pathlib import Path


APPLICATION_DIRECTORY_NAME = "AnythingLLM PDF Parser Embedder Assistant"
DATA_DIRECTORY_ENVIRONMENT_VARIABLE = "ANYTHINGLLM_PDF_ASSISTANT_HOME"


def application_data_dir() -> Path:
    """Return the writable, user-specific root for this installation.

    ``ANYTHINGLLM_PDF_ASSISTANT_HOME`` is intentionally the one supported
    override.  It is useful for portable drives and automated tests without
    leaking an author's home directory into package defaults.
    """

    override = os.environ.get(DATA_DIRECTORY_ENVIRONMENT_VARIABLE, "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            return Path(local_app_data) / APPLICATION_DIRECTORY_NAME
        return Path.home() / "AppData" / "Local" / APPLICATION_DIRECTORY_NAME
    xdg_data_home = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg_data_home:
        return Path(xdg_data_home) / "anythingllm-pdf-assistant"
    return Path.home() / ".local" / "share" / "anythingllm-pdf-assistant"


def application_paths() -> dict[str, Path]:
    """Return the canonical writable locations without creating them."""

    root = application_data_dir()
    outputs = root / "outputs"
    return {
        "root": root,
        "outputs": outputs,
        "automatic_outputs": outputs / "automatic-runs",
        "interactive_outputs": outputs / "interactive-runs",
        "logs": root / "logs",
        "config": root / "config",
        "downloads": Path.home() / "Downloads",
    }


def ensure_application_directories() -> dict[str, Path]:
    """Create the writable application locations and return them."""

    paths = application_paths()
    for key in ("root", "outputs", "automatic_outputs", "interactive_outputs", "logs", "config"):
        paths[key].mkdir(parents=True, exist_ok=True)
    return paths


def package_resource_path(relative_path: str) -> Path:
    """Find a non-writable package resource in source or installed form."""

    relative = Path(relative_path)
    source_candidate = Path(__file__).resolve().parent / relative
    if source_candidate.is_file():
        return source_candidate
    installed_candidate = (
        Path(sysconfig.get_path("data"))
        / "share"
        / "anythingllm-pdf-assistant"
        / relative
    )
    if installed_candidate.is_file():
        return installed_candidate
    raise FileNotFoundError(f"Required package resource is missing: {relative_path}")

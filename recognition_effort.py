"""Persist the last author-recognition effort choice, separately from run data.

The UI will use Thorough on first use. Legacy engine calls keep their full
acquisition behavior unless they explicitly receive another effort value.
This module stores a preference only; it cannot authorize an unqualified
early-stop policy or turn a workload heuristic into a failure probability.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import os
from pathlib import Path
import tempfile
import threading

from portable_paths import application_paths
from process_lock import named_process_lock


class RecognitionEffort(str, Enum):
    THOROUGH = "thorough"
    EXTREMELY_THOROUGH = "extremely_thorough"

    @property
    def label(self) -> str:
        return "Thorough" if self is self.THOROUGH else "Extremely Thorough"

    @property
    def slider_value(self) -> int:
        return 0 if self is self.THOROUGH else 1


def normalize_effort(value: object) -> RecognitionEffort:
    if isinstance(value, RecognitionEffort):
        return value
    # Gradio can return a float for an integer-step slider. Never silently
    # truncate an invalid value or accept bool as the number 0/1.
    if type(value) in (int, float) and value in (0, 1):
        return RecognitionEffort.THOROUGH if value == 0 else RecognitionEffort.EXTREMELY_THOROUGH
    if isinstance(value, str):
        key = value.strip().casefold().replace(" ", "_")
        try:
            return RecognitionEffort(key)
        except ValueError:
            pass
    raise ValueError("Choose Thorough or Extremely Thorough.")


@dataclass(frozen=True)
class EffortPreference:
    effort: RecognitionEffort
    notice: str = ""


_WRITE_LOCK = threading.RLock()
_SCHEMA_VERSION = 1


def recognition_effort_path(home_directory: str | Path | None = None) -> Path:
    root = Path(home_directory) if home_directory is not None else application_paths()["root"]
    return root / "settings" / "author-recognition-effort.json"


def load_recognition_effort(*, home_directory: str | Path | None = None) -> EffortPreference:
    """Read without creating files or changing the saved Automatic profile."""
    path = recognition_effort_path(home_directory)
    try:
        raw = json.loads(path.read_text(encoding="utf8"))
    except FileNotFoundError:
        return EffortPreference(RecognitionEffort.THOROUGH)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return EffortPreference(
            RecognitionEffort.THOROUGH,
            "The saved recognition effort could not be read. Thorough is selected; choose a mode to save it again.",
        )
    if (
        not isinstance(raw, dict)
        or type(raw.get("schema_version")) is not int
        or raw["schema_version"] != _SCHEMA_VERSION
        or not isinstance(raw.get("effort"), str)
        or raw.get("effort") not in {mode.value for mode in RecognitionEffort}
    ):
        return EffortPreference(
            RecognitionEffort.THOROUGH,
            "The saved recognition effort is unsupported or invalid. Thorough is selected; choose a mode to save it again.",
        )
    return EffortPreference(RecognitionEffort(raw["effort"]))


def save_recognition_effort(
    value: object, *, home_directory: str | Path | None = None
) -> RecognitionEffort:
    """Save immediately and atomically. A failed write remains visible to UI."""
    effort = normalize_effort(value)
    path = recognition_effort_path(home_directory)
    identity = os.path.normcase(str(path.resolve()))
    with _WRITE_LOCK, named_process_lock("recognition-effort", identity, timeout_seconds=5.0):
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, scratch = tempfile.mkstemp(prefix=".recognition-effort-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf8", newline="\n") as output:
                json.dump({"schema_version": _SCHEMA_VERSION, "effort": effort.value}, output, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(scratch, path)
        finally:
            # Only the exact owned temporary file is eligible for cleanup.
            Path(scratch).unlink(missing_ok=True)
    return effort

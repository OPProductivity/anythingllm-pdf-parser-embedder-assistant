"""Guarded AnythingLLM settings mutation with redacted snapshots."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from anythingllm_compatibility import characterize


ENV_SECRET_MARKERS = ("KEY", "SECRET", "TOKEN", "PASSWORD")
SQLITE_SETTINGS = {"text_splitter_chunk_size", "text_splitter_chunk_overlap"}
ENVIRONMENT_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _read_env_lines(path):
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def _env_value(text, key):
    match = re.search(rf"^\s*{re.escape(key)}\s*=\s*(.*)$", text, re.MULTILINE)
    if not match:
        return None
    value = match.group(1).strip()
    if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        return value[1:-1]
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return value[1:-1]
        return decoded if isinstance(decoded, str) else value[1:-1]
    return value


def _redacted_value(key, value):
    if any(marker in key.upper() for marker in ENV_SECRET_MARKERS):
        return {"present": bool(value), "value": None}
    return {"present": value is not None, "value": value}


def _validated_environment_assignment(key, value):
    normalized_key = str(key or "").strip()
    normalized_value = str(value)
    if not ENVIRONMENT_KEY_PATTERN.fullmatch(normalized_key):
        raise ValueError(f"Unsupported environment setting name: {key!r}")
    if "\r" in normalized_value or "\n" in normalized_value:
        raise ValueError("Environment setting values must be single-line.")
    return normalized_key, normalized_value


def _environment_assignment_line(key, value):
    """Encode one .env scalar without allowing quotes to corrupt its line."""
    return f"{key}={json.dumps(value, ensure_ascii=False)}"


def _atomic_write_text(path: Path, content: str) -> None:
    """Replace a settings artifact only after its full UTF-8 content is durable."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            delete=False,
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


class AnythingLLMPersistenceAdapter:
    """Perform narrowly allowed Desktop-setting writes with a redacted snapshot.

    This adapter is intentionally separate from read-only state resolution.
    Every write must be capability-gated, snapshot the exact setting it will
    change, verify the stored result, and report whether Desktop restart is
    likely required. It is not a general-purpose database writer.
    """
    def __init__(self, storage_dir, run_id, snapshot_dir):
        self.storage = Path(storage_dir)
        self.run_id = str(run_id)
        self.snapshot_dir = Path(snapshot_dir)
        self.compatibility = characterize(self.storage)

    def _require(self, capability):
        state = self.compatibility["capabilities"][capability]
        if state["status"] != "supported":
            raise RuntimeError(f"{capability} is {state['status']}: {state['message']}")

    def snapshot(self, env_keys=(), sqlite_labels=(), reason="operator_requested_change"):
        env_text = _read_env_lines(self.storage / ".env")
        env_values = {key: _redacted_value(key, _env_value(env_text, key)) for key in env_keys}
        sqlite_values = {}
        if sqlite_labels:
            con = sqlite3.connect(
                f"file:{self.storage / 'anythingllm.db'}?mode=ro",
                uri=True,
                timeout=1.0,
            )
            try:
                for label in sqlite_labels:
                    row = con.execute("select value from system_settings where label=?", (label,)).fetchone()
                    sqlite_values[label] = row[0] if row else None
            finally:
                con.close()
        payload = {
            "schema_version": 1,
            "run_id": self.run_id,
            "created_at": _now(),
            "reason": reason,
            "matched_profile": self.compatibility["matched_profile"],
            "env": env_values,
            "sqlite": sqlite_values,
        }
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        # Each mutation needs an immutable restoration point. A fixed filename
        # used to overwrite the snapshot from an earlier setting change in the
        # same run, making a full rollback impossible.
        path = self.snapshot_dir / f"anythingllm-settings-snapshot-{uuid.uuid4().hex}.json"
        _atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False))
        return path

    def write_env_setting(self, key, value, reason="operator_requested_change"):
        self._require("can_write_env_settings")
        key, value = _validated_environment_assignment(key, value)
        snapshot = self.snapshot(env_keys=[key], reason=reason)
        path = self.storage / ".env"
        original = _read_env_lines(path)
        replacement = _environment_assignment_line(key, value)
        pattern = re.compile(rf"^\s*{re.escape(key)}\s*=.*$", re.MULTILINE)
        updated = pattern.sub(lambda _match: replacement, original, count=1) if pattern.search(original) else original.rstrip() + "\n" + replacement + "\n"
        _atomic_write_text(path, updated)
        if _env_value(_read_env_lines(path), key) != value:
            raise RuntimeError(f"Write verification failed for {key}")
        return {"status": "verified", "snapshot": str(snapshot), "key": key, "restart_likely_required": True}

    def write_sqlite_setting(self, label, value, reason="operator_requested_change"):
        if label not in SQLITE_SETTINGS:
            raise ValueError(f"Unsupported guarded SQLite setting: {label}")
        self._require("can_write_sqlite_settings")
        snapshot = self.snapshot(sqlite_labels=[label], reason=reason)
        con = sqlite3.connect(self.storage / "anythingllm.db", timeout=1.0)
        try:
            row = con.execute("select value from system_settings where label=?", (label,)).fetchone()
            if row:
                con.execute("update system_settings set value=? where label=?", (str(value), label))
            else:
                con.execute("insert into system_settings(label,value) values(?,?)", (label, str(value)))
            con.commit()
            verified = con.execute("select value from system_settings where label=?", (label,)).fetchone()
        finally:
            con.close()
        if not verified or verified[0] != str(value):
            raise RuntimeError(f"Write verification failed for {label}")
        return {"status": "verified", "snapshot": str(snapshot), "label": label, "value": str(value)}

    def restore(self, snapshot_path):
        self._require("can_restore_snapshotted_settings")
        payload = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
        restored = {"env": [], "sqlite": [], "skipped_env": []}
        env_updates = []
        env_removals = []
        for key, record in payload.get("env", {}).items():
            if not isinstance(record, dict):
                restored["skipped_env"].append(str(key))
                continue
            if record.get("value") is None:
                # A redacted secret that existed cannot be restored because
                # its value intentionally never enters the snapshot. An
                # originally absent key, however, has an exact safe restore:
                # remove the assignment written after the snapshot.
                if record.get("present") is False:
                    try:
                        key, _ = _validated_environment_assignment(key, "")
                    except (KeyError, ValueError, TypeError):
                        restored["skipped_env"].append(str(key))
                        continue
                    env_removals.append(key)
                else:
                    restored["skipped_env"].append(str(key))
                continue
            try:
                key, value = _validated_environment_assignment(key, record["value"])
            except (KeyError, ValueError, TypeError):
                restored["skipped_env"].append(str(key))
                continue
            env_updates.append((key, value))
        if env_updates or env_removals:
            path = self.storage / ".env"
            updated = _read_env_lines(path)
            for key, value in env_updates:
                replacement = _environment_assignment_line(key, value)
                pattern = re.compile(rf"^\s*{re.escape(key)}\s*=.*$", re.MULTILINE)
                updated = pattern.sub(lambda _match: replacement, updated, count=1) if pattern.search(updated) else updated.rstrip() + "\n" + replacement + "\n"
            for key in env_removals:
                pattern = re.compile(rf"^\s*{re.escape(key)}\s*=.*(?:\r?\n|$)", re.MULTILINE)
                updated = pattern.sub("", updated, count=1)
            _atomic_write_text(path, updated)
            persisted = _read_env_lines(path)
            for key, value in env_updates:
                if _env_value(persisted, key) != value:
                    raise RuntimeError(f"Restore verification failed for {key}")
                restored["env"].append(key)
            for key in env_removals:
                if _env_value(persisted, key) is not None:
                    raise RuntimeError(f"Restore verification failed for absent {key}")
                restored["env"].append(key)
        if payload.get("sqlite"):
            con = sqlite3.connect(self.storage / "anythingllm.db", timeout=1.0)
            try:
                for label, value in payload["sqlite"].items():
                    if label not in SQLITE_SETTINGS:
                        continue
                    if value is None:
                        con.execute("delete from system_settings where label=?", (label,))
                    elif con.execute("select 1 from system_settings where label=?", (label,)).fetchone():
                        con.execute("update system_settings set value=? where label=?", (value, label))
                    else:
                        con.execute("insert into system_settings(label,value) values(?,?)", (label, value))
                    restored["sqlite"].append(label)
                con.commit()
                for label, value in payload["sqlite"].items():
                    if label not in SQLITE_SETTINGS:
                        continue
                    row = con.execute("select value from system_settings where label=?", (label,)).fetchone()
                    if (row[0] if row else None) != value:
                        raise RuntimeError(f"Restore verification failed for {label}")
            finally:
                con.close()
        return {"status": "restored", "restored": restored, "restart_likely_required": bool(restored["env"])}

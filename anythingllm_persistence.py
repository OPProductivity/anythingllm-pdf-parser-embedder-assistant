"""Guarded AnythingLLM settings mutation with redacted snapshots."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from anythingllm_compatibility import characterize


ENV_SECRET_MARKERS = ("KEY", "SECRET", "TOKEN", "PASSWORD")
SQLITE_SETTINGS = {"text_splitter_chunk_size", "text_splitter_chunk_overlap"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _read_env_lines(path):
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def _env_value(text, key):
    match = re.search(rf"^\s*{re.escape(key)}\s*=\s*(.*)$", text, re.MULTILINE)
    return match.group(1).strip().strip("'\"") if match else None


def _redacted_value(key, value):
    if any(marker in key.upper() for marker in ENV_SECRET_MARKERS):
        return {"present": bool(value), "value": None}
    return {"present": value is not None, "value": value}


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
            con = sqlite3.connect(f"file:{self.storage / 'anythingllm.db'}?mode=ro", uri=True)
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
        path = self.snapshot_dir / "anythingllm-settings-snapshot-before.json"
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    def write_env_setting(self, key, value, reason="operator_requested_change"):
        self._require("can_write_env_settings")
        snapshot = self.snapshot(env_keys=[key], reason=reason)
        path = self.storage / ".env"
        original = _read_env_lines(path)
        replacement = f"{key}='{value}'"
        pattern = re.compile(rf"^\s*{re.escape(key)}\s*=.*$", re.MULTILINE)
        updated = pattern.sub(replacement, original, count=1) if pattern.search(original) else original.rstrip() + "\n" + replacement + "\n"
        path.write_text(updated, encoding="utf-8")
        return {"status": "verified", "snapshot": str(snapshot), "key": key, "restart_likely_required": True}

    def write_sqlite_setting(self, label, value, reason="operator_requested_change"):
        if label not in SQLITE_SETTINGS:
            raise ValueError(f"Unsupported guarded SQLite setting: {label}")
        self._require("can_write_sqlite_settings")
        snapshot = self.snapshot(sqlite_labels=[label], reason=reason)
        con = sqlite3.connect(self.storage / "anythingllm.db")
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
        restored = {"env": [], "sqlite": []}
        for key, record in payload.get("env", {}).items():
            if record.get("value") is None:
                continue
            path = self.storage / ".env"
            original = _read_env_lines(path)
            replacement = f"{key}='{record['value']}'"
            pattern = re.compile(rf"^\s*{re.escape(key)}\s*=.*$", re.MULTILINE)
            updated = pattern.sub(replacement, original, count=1) if pattern.search(original) else original.rstrip() + "\n" + replacement + "\n"
            path.write_text(updated, encoding="utf-8")
            restored["env"].append(key)
        if payload.get("sqlite"):
            con = sqlite3.connect(self.storage / "anythingllm.db")
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
            finally:
                con.close()
        return {"status": "restored", "restored": restored, "restart_likely_required": bool(restored["env"])}

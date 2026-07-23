"""Read-only AnythingLLM Desktop compatibility characterization.

This module deliberately does not mutate AnythingLLM. A recognized profile is
not a blanket authorization: every operation family receives its own capability
decision and evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


PROFILE_ID = "anythingllm-desktop-1.14.2-through-1.15.0-r2-observed-profile-2"
OBSERVED_COMPATIBLE_DESKTOP_VERSIONS = ("1.14.2", "1.15.0-r2")
CAPABILITIES = (
    "can_read_env_state",
    "can_read_sqlite_state",
    "can_read_workspace_storage",
    "can_inspect_lance",
    "can_write_env_settings",
    "can_write_sqlite_settings",
    "can_create_temp_api_key",
    "can_delete_temp_api_key",
    "can_create_workspace",
    "can_delete_workspace",
    "can_upload_native_metadata",
    "can_poll_post_upload_state",
    "can_runtime_verify_embedder",
    "can_restore_snapshotted_settings",
)
REQUIRED_COLUMNS = {
    "system_settings": {"label", "value"},
    "workspaces": {
        "id", "name", "slug", "chatProvider", "chatModel", "topN",
        "similarityThreshold", "vectorSearchMode", "chatMode",
    },
    "workspace_documents": {"id", "docId", "filename", "docpath", "metadata", "createdAt"},
    "document_vectors": {"docId", "vectorId"},
}


@dataclass(frozen=True)
class Evidence:
    source_type: str
    location: str
    observed: str
    timestamp: str
    content_hash: str = ""


@dataclass
class Capability:
    status: str = "unknown"
    evidence: list[Evidence] = field(default_factory=list)
    blocking_anomalies: list[str] = field(default_factory=list)
    message: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _evidence(source_type: str, location: str, observed: object) -> Evidence:
    text = str(observed)
    return Evidence(
        source_type=source_type,
        location=location,
        observed=text[:240],
        timestamp=_now(),
        content_hash=hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest(),
    )


def default_storage_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "anythingllm-desktop" / "storage"
    return Path.home() / "AppData" / "Roaming" / "anythingllm-desktop" / "storage"


def _sqlite_schema(db_path: Path) -> tuple[dict[str, set[str]], list[Evidence], list[str]]:
    schema: dict[str, set[str]] = {}
    evidence: list[Evidence] = []
    errors: list[str] = []
    if not db_path.exists():
        return schema, evidence, ["sqlite_database_missing"]
    con = None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        table_rows = con.execute("select name from sqlite_master where type='table'").fetchall()
        for (table,) in table_rows:
            safe_table = str(table).replace('"', '""')
            columns = {row[1] for row in con.execute(f'pragma table_info("{safe_table}")').fetchall()}
            schema[str(table)] = columns
            evidence.append(_evidence("sqlite", f"{table} columns", ",".join(sorted(columns))))
    except Exception as exc:
        errors.append(f"sqlite_schema_error:{type(exc).__name__}")
    finally:
        if con is not None:
            con.close()
    return schema, evidence, errors


def _supported(evidence: Iterable[Evidence], message: str) -> Capability:
    return Capability(status="supported", evidence=list(evidence), message=message)


def _blocked(anomalies: Iterable[str], message: str, evidence: Iterable[Evidence] = ()) -> Capability:
    return Capability(
        status="blocked",
        evidence=list(evidence),
        blocking_anomalies=list(anomalies),
        message=message,
    )


def characterize(storage_dir: Path | str | None = None) -> dict:
    storage = Path(storage_dir) if storage_dir else default_storage_dir()
    env_path = storage / ".env"
    db_path = storage / "anythingllm.db"
    documents = storage / "documents"
    lance = storage / "lancedb"
    schema, schema_evidence, errors = _sqlite_schema(db_path)

    missing = {
        table: sorted(columns - schema.get(table, set()))
        for table, columns in REQUIRED_COLUMNS.items()
        if columns - schema.get(table, set())
    }
    profile_match = storage.exists() and env_path.exists() and db_path.exists() and not missing and not errors
    profile = PROFILE_ID if profile_match else ""

    env_evidence = [_evidence("filesystem", ".env", "present")] if env_path.exists() else []
    db_evidence = [_evidence("filesystem", "anythingllm.db", "present"), *schema_evidence] if db_path.exists() else []
    document_evidence = [_evidence("filesystem", "documents", "present")] if documents.exists() else []
    lance_evidence = [_evidence("filesystem", "lancedb", "present")] if lance.exists() else []

    capabilities = {name: Capability(message="No supporting profile evidence.") for name in CAPABILITIES}
    capabilities["can_read_env_state"] = (
        _supported(env_evidence, "AnythingLLM .env is readable.")
        if env_path.exists() else _blocked(["env_missing"], "AnythingLLM .env was not found.")
    )
    capabilities["can_read_sqlite_state"] = (
        _supported(db_evidence, "Required SQLite structures match the observed profile.")
        if profile_match else _blocked([*errors, *(f"missing_columns:{k}" for k in missing)], "SQLite profile did not match.", db_evidence)
    )
    capabilities["can_read_workspace_storage"] = (
        _supported(document_evidence, "Document storage directory is present.")
        if documents.exists() else _blocked(["documents_directory_missing"], "Document storage is unavailable.")
    )
    capabilities["can_inspect_lance"] = (
        _supported(lance_evidence, "LanceDB directory is present; package/runtime verification is still required.")
        if lance.exists() else _blocked(["lancedb_directory_missing"], "LanceDB storage is unavailable.")
    )

    if profile_match:
        capabilities["can_write_env_settings"] = _supported(env_evidence, "Observed profile permits guarded .env writes.")
        capabilities["can_write_sqlite_settings"] = _supported(db_evidence, "Observed profile permits guarded SQLite writes.")
        capabilities["can_restore_snapshotted_settings"] = _supported(
            [*env_evidence, *db_evidence], "Observed profile supports guarded restoration of known settings."
        )
    else:
        for name in ("can_write_env_settings", "can_write_sqlite_settings", "can_restore_snapshotted_settings"):
            capabilities[name] = _blocked(["profile_not_matched"], "Mutation is blocked without a recognized profile.")

    # API capabilities require endpoint probes and remain unknown in filesystem-only characterization.
    result = {
        "schema_version": 1,
        "observed_at": _now(),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "supported_contract": platform.system() == "Windows" and platform.machine().casefold() in {"amd64", "x86_64"},
        },
        "storage_dir": str(storage),
        "matched_profile": profile,
        "profile_status": "matched" if profile_match else "unknown",
        "observed_compatible_desktop_versions": list(OBSERVED_COMPATIBLE_DESKTOP_VERSIONS),
        "schema": {table: sorted(columns) for table, columns in schema.items()},
        "missing_required_columns": missing,
        "errors": errors,
        "capabilities": {name: asdict(value) for name, value in capabilities.items()},
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Characterize a local AnythingLLM Desktop installation without mutation.")
    parser.add_argument("--storage-dir", default="")
    parser.add_argument("--json", action="store_true", help="Emit the complete machine-readable result.")
    args = parser.parse_args()
    result = characterize(args.storage_dir or None)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"Profile: {result['matched_profile'] or 'unrecognized'}")
        for name, capability in result["capabilities"].items():
            print(f"{name}: {capability['status']} - {capability['message']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

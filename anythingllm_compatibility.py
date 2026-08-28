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
import subprocess
import threading
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


_PACKAGE_FINGERPRINT_CACHE: dict[tuple[str, int, int], str] = {}
_PACKAGE_FINGERPRINT_CACHE_LOCK = threading.Lock()


# A *mutation* profile authorizes the guarded persistence adapter only.  Do
# not conflate it with the much smaller read-only storage contract.  In
# particular, a new Desktop version may retain all of the tables required for
# inspection while its settings-write behavior remains unqualified.
# Legacy releases have the earlier version-only observed mutation profile.
# Desktop 1.16 is deliberately separate: it is qualified only when the exact
# official app.asar fingerprint and the current settings schema both match.
PROFILE_ID = "anythingllm-desktop-1.14.2-through-1.15.0-r2-observed-profile-2"
V116_PROFILE_ID = "anythingllm-desktop-1.16.0-fingerprinted-settings-profile-1"
V116_NATIVE_CONTRACT_ID = "anythingllm-desktop-1.16.0-native-pdf-contract-1"
V1161_PROFILE_ID = "anythingllm-desktop-1.16.1-fingerprinted-settings-profile-1"
V1161_NATIVE_CONTRACT_ID = "anythingllm-desktop-1.16.1-native-pdf-contract-1"
OBSERVED_COMPATIBLE_DESKTOP_VERSIONS = ("1.14.2", "1.15.0-r2")
OBSERVED_CANDIDATE_DESKTOP_VERSIONS = ("1.16.0", "1.16.1")
OBSERVED_CANDIDATE_PACKAGE_FINGERPRINTS = {
    # Official Desktop v1.16.0 package observed during the isolated native
    # upload/confirmation run.  A release label alone does not identify a
    # locally modified or repackaged Electron bundle.
    "1.16.0": "a40a9bb5915e6383f51fd2a02e76052724a6c9d2576d852025eefcbb23d4282b",  # pragma: allowlist secret -- public package SHA-256 fingerprint, not a credential
    # Official Windows x64 Desktop v1.16.1 package observed and qualified by
    # an isolated create/upload/link/vector/retrieval/cleanup contract run on
    # 2026-08-28.  The exact package remains part of the authority boundary.
    "1.16.1": "4f00651eb1a421a3a37fb60dc9486e0dc5577d21efac96dcf4b05ad2887ea910",  # pragma: allowlist secret -- public package SHA-256 fingerprint, not a credential
}
OBSERVED_CANDIDATE_SETTINGS_PROFILES = {
    "1.16.0": V116_PROFILE_ID,
    "1.16.1": V1161_PROFILE_ID,
}
# Mutation authority is capability-specific. This immutable record comes from
# the isolated 2026-08-22 real-PDF run: workspace creation, native metadata
# attachment, workspace linking, and exact vector observation all completed.
# It intentionally does not claim temporary-key deletion or workspace cleanup,
# which require their own opt-in contract probe evidence.
OBSERVED_NATIVE_MUTATION_CONTRACTS = {
    "a40a9bb5915e6383f51fd2a02e76052724a6c9d2576d852025eefcbb23d4282b": {  # pragma: allowlist secret -- package SHA-256
        "contract_id": V116_NATIVE_CONTRACT_ID,
        "desktop_version": "1.16.0",
        "observed_at": "2026-08-22T00:00:00+00:00",
        "capabilities": (
            "can_create_workspace",
            "can_upload_native_metadata",
            "can_poll_post_upload_state",
        ),
    },
    "4f00651eb1a421a3a37fb60dc9486e0dc5577d21efac96dcf4b05ad2887ea910": {  # pragma: allowlist secret -- package SHA-256
        "contract_id": V1161_NATIVE_CONTRACT_ID,
        "desktop_version": "1.16.1",
        "observed_at": "2026-08-28T06:47:45+00:00",
        "capabilities": (
            "can_create_temp_api_key",
            "can_delete_temp_api_key",
            "can_create_workspace",
            "can_delete_workspace",
            "can_upload_native_metadata",
            "can_poll_post_upload_state",
            "can_runtime_verify_embedder",
        ),
    },
}
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

# These are the only API routes needed to establish that the documented
# read-only/upload contract is present.  A match is useful evidence during a
# Desktop upgrade, but deliberately does *not* authorize any mutation by
# itself: authentication, response semantics, and the guarded version/profile
# rules remain separate controls.
REQUIRED_API_CONTRACT_ROUTES = (
    "/v1/document/raw-text",
    "/v1/document/upload",
    "/v1/workspaces",
    "/v1/workspace/{slug}/update-embeddings",
)

# Desktop has exposed these paths in some builds, but the current public
# OpenAPI document does not promise either one.  They are reported so an
# operator can see the distinction; progress is UI evidence only, while queue
# cleanup remains a narrow recovery-only action with its own ownership guard.
ADVISORY_API_CONTRACT_ROUTES = (
    "/v1/workspace/{slug}/embed-progress",
    "/v1/workspace/{slug}/embed-queue",
)


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


def _desktop_executable() -> Path | None:
    """Return the standard per-user Desktop executable without probing arbitrary paths."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return None
    candidate = Path(local_app_data) / "Programs" / "AnythingLLM" / "AnythingLLM.exe"
    return candidate if candidate.is_file() else None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _desktop_package_identity(
    executable: Path | None,
    *,
    include_fingerprint: bool,
) -> tuple[dict, list[Evidence], list[str]]:
    """Describe the bundled app without making regular UI discovery expensive.

    Hashing ``app.asar`` is intentionally an explicit diagnostic request.  The
    normal UI path receives stable size/mtime identity data; a compatibility
    audit can ask for the stronger fingerprint before a new profile or bridge
    anchor is accepted.
    """
    result = {
        "resources_dir": "",
        "app_asar": "",
        "app_asar_size_bytes": 0,
        "app_asar_mtime_ns": 0,
        "app_asar_sha256": "",
        "fingerprint_status": "not_requested",
    }
    if executable is None:
        result["fingerprint_status"] = "desktop_executable_missing"
        return result, [], ["desktop_executable_missing"]
    asar = executable.parent / "resources" / "app.asar"
    result["resources_dir"] = str(asar.parent)
    result["app_asar"] = str(asar)
    if not asar.is_file():
        result["fingerprint_status"] = "app_asar_missing"
        return result, [], ["app_asar_missing"]
    try:
        stat = asar.stat()
    except OSError as exc:
        result["fingerprint_status"] = "app_asar_stat_error"
        return result, [], [f"app_asar_stat_error:{type(exc).__name__}"]
    result["app_asar_size_bytes"] = int(stat.st_size)
    result["app_asar_mtime_ns"] = int(stat.st_mtime_ns)
    observed = f"size={stat.st_size};mtime_ns={stat.st_mtime_ns}"
    evidence = [_evidence("filesystem", str(asar), observed)]
    if not include_fingerprint:
        return result, evidence, []
    cache_key = (str(asar.resolve()), int(stat.st_size), int(stat.st_mtime_ns))
    try:
        with _PACKAGE_FINGERPRINT_CACHE_LOCK:
            cached_fingerprint = _PACKAGE_FINGERPRINT_CACHE.get(cache_key, "")
        if cached_fingerprint:
            result["app_asar_sha256"] = cached_fingerprint
        else:
            computed_fingerprint = _sha256_file(asar)
            with _PACKAGE_FINGERPRINT_CACHE_LOCK:
                # Retain only the current identity for this path. A replaced
                # package has a different size/mtime key and is re-hashed.
                for key in [key for key in _PACKAGE_FINGERPRINT_CACHE if key[0] == cache_key[0]]:
                    _PACKAGE_FINGERPRINT_CACHE.pop(key, None)
                _PACKAGE_FINGERPRINT_CACHE[cache_key] = computed_fingerprint
            result["app_asar_sha256"] = computed_fingerprint
    except OSError as exc:
        result["fingerprint_status"] = "fingerprint_error"
        return result, evidence, [f"app_asar_fingerprint_error:{type(exc).__name__}"]
    result["fingerprint_status"] = "computed"
    evidence.append(Evidence(
        source_type="filesystem",
        location=str(asar),
        observed="sha256 computed",
        timestamp=_now(),
        content_hash=result["app_asar_sha256"],
    ))
    return result, evidence, []


def _desktop_version(executable: Path | None) -> tuple[str, list[Evidence], list[str]]:
    """Read the installed Desktop version on Windows without starting Desktop."""
    if executable is None:
        return "", [], ["desktop_executable_missing"]
    if platform.system() != "Windows":
        return "", [], ["desktop_version_probe_unsupported_platform"]
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"(Get-Item -LiteralPath '{str(executable).replace("'", "''")}').VersionInfo.ProductVersion",
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return "", [], [f"desktop_version_probe_error:{type(exc).__name__}"]
    version = completed.stdout.strip()
    if completed.returncode != 0 or not version:
        return "", [], ["desktop_version_unavailable"]
    return version, [_evidence("filesystem", str(executable), version)], []


def _normalized_desktop_version(version: str) -> str:
    """Normalize only the Windows product-version zero-padding convention.

    Electron/Windows commonly reports a three-part package version such as
    ``1.16.0`` as ``1.16.0.0``.  Removing terminal zero-only components makes
    that representation comparable without accepting a later patch or a
    prerelease label.  The raw version remains in the evidence record.
    """
    value = str(version or "").strip()
    if not value or "-" in value or "+" in value:
        return value
    parts = value.split(".")
    if not all(part.isdigit() for part in parts):
        return value
    while len(parts) > 3 and parts[-1] == "0":
        parts.pop()
    return ".".join(parts)


def _desktop_release_status(version: str, package_sha256: str = "") -> tuple[str, str]:
    """Classify the release without upgrading a capability by implication.

    A 1.16 release is eligible for guarded-settings writes only when its exact
    package fingerprint matches the observed official build.  A version label
    alone remains a read-only candidate because Electron applications can be
    locally repackaged without changing that label.
    """
    if version in OBSERVED_COMPATIBLE_DESKTOP_VERSIONS:
        return "recognized_mutation_profile", PROFILE_ID
    if version in OBSERVED_CANDIDATE_DESKTOP_VERSIONS:
        expected_fingerprint = OBSERVED_CANDIDATE_PACKAGE_FINGERPRINTS.get(version, "")
        if not package_sha256:
            return "candidate_version_requires_package_fingerprint", ""
        if package_sha256 != expected_fingerprint:
            return "unprofiled_package_fingerprint", ""
        return "recognized_mutation_profile", OBSERVED_CANDIDATE_SETTINGS_PROFILES[version]
    if version:
        return "unprofiled", ""
    return "unidentified", ""


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


def _loopback_api_docs_url(api_url: str) -> tuple[str, str]:
    """Return a safe Swagger-init URL for a local Desktop API root.

    This discovery probe sends no credential and makes no API mutation.  It is
    intentionally loopback-only: the Desktop compatibility command is about
    the local application, and accepting arbitrary network URLs would make a
    harmless-looking diagnostic unexpectedly contact a remote service.
    """
    candidate = str(api_url or "").strip()
    parsed = urllib.parse.urlparse(candidate)
    hostname = (parsed.hostname or "").casefold()
    if parsed.scheme not in {"http", "https"} or hostname not in {"127.0.0.1", "localhost", "::1"}:
        return "", "api_url_must_be_loopback_http"
    if parsed.query or parsed.fragment or parsed.params:
        return "", "api_url_must_not_include_query_or_fragment"
    path = (parsed.path or "").rstrip("/")
    if path not in {"", "/api"}:
        return "", "api_url_must_be_api_root"
    root = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))
    return root.rstrip("/") + "/api/docs/swagger-ui-init.js", ""


def _documented_openapi_paths(payload: str) -> set[str]:
    """Extract path keys from Swagger UI's generated initialization script.

    AnythingLLM serves a JavaScript initializer rather than a stable standalone
    JSON file.  Looking only for JSON-shaped OpenAPI path keys avoids executing
    the script and remains read-only when Swagger UI changes its surrounding
    bootstrap code.
    """
    import re

    return {
        match.group("path")
        for match in re.finditer(r'"(?P<path>/v1/[^"\\]+)"\s*:\s*\{', str(payload or ""))
    }


def _openapi_paths_from_file(openapi_path: Path | None) -> tuple[set[str], str]:
    """Read only the bounded installed OpenAPI file tied to this package."""
    if openapi_path is None or not openapi_path.is_file():
        return set(), "installed_openapi_missing"
    try:
        if openapi_path.stat().st_size > 4 * 1024 * 1024:
            return set(), "installed_openapi_too_large"
        payload = json.loads(openapi_path.read_text(encoding="utf-8"))
        paths = payload.get("paths", {}) if isinstance(payload, dict) else {}
        if not isinstance(paths, dict):
            return set(), "installed_openapi_paths_invalid"
        return {str(path) for path in paths}, ""
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        return set(), f"installed_openapi_error:{type(exc).__name__}"


def probe_api_contract(
    api_url: str,
    *,
    timeout_seconds: float = 5.0,
    installed_openapi_path: Path | None = None,
) -> dict:
    """Read the local Swagger contract without granting write authority.

    The probe never creates a key, workspace, document, or queue entry.  It
    only reports whether the documented core routes are visible and separately
    marks progress/recovery routes as undocumented when absent.  Callers must
    not convert ``qualified_read_only_contract`` into permission to mutate a
    new Desktop version.
    """
    docs_url, validation_error = _loopback_api_docs_url(api_url)
    result = {
        "schema_version": 1,
        "observed_at": _now(),
        "api_url": str(api_url or "").strip(),
        "docs_url": docs_url,
        "status": "unavailable",
        "contract_scope": "read_only_documentation_discovery",
        "write_authority": "not_granted_by_api_contract",
        "required_core_routes": list(REQUIRED_API_CONTRACT_ROUTES),
        "documented_core_routes": [],
        "missing_core_routes": list(REQUIRED_API_CONTRACT_ROUTES),
        "advisory_routes": {
            route: "not_checked" for route in ADVISORY_API_CONTRACT_ROUTES
        },
        "contract_evidence_source": "",
        "error": validation_error,
    }
    if validation_error:
        return result
    try:
        request = urllib.request.Request(
            docs_url,
            headers={"Accept": "application/javascript, application/json"},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=max(0.25, min(15.0, float(timeout_seconds)))) as response:
            body = response.read(2 * 1024 * 1024 + 1)
        if len(body) > 2 * 1024 * 1024:
            result["error"] = "api_docs_response_too_large"
            return result
        paths = _documented_openapi_paths(body.decode("utf-8", errors="replace"))
        if paths:
            result["contract_evidence_source"] = "loopback_swagger_initializer"
    except Exception as exc:
        result["error"] = f"api_docs_probe_error:{type(exc).__name__}"
        paths = set()
    if not paths and installed_openapi_path is not None:
        paths, fallback_error = _openapi_paths_from_file(installed_openapi_path)
        if paths:
            result["contract_evidence_source"] = "installed_package_openapi"
            result["error"] = ""
        elif not result["error"]:
            result["error"] = fallback_error
    documented = [route for route in REQUIRED_API_CONTRACT_ROUTES if route in paths]
    missing = [route for route in REQUIRED_API_CONTRACT_ROUTES if route not in paths]
    result["documented_core_routes"] = documented
    result["missing_core_routes"] = missing
    result["advisory_routes"] = {
        route: ("documented_but_advisory" if route in paths else "undocumented_advisory")
        for route in ADVISORY_API_CONTRACT_ROUTES
    }
    result["status"] = "qualified_read_only_contract" if not missing else "incomplete_documented_contract"
    result["error"] = ""
    return result


def characterize(
    storage_dir: Path | str | None = None,
    *,
    include_package_fingerprint: bool = False,
    api_url: str = "",
) -> dict:
    storage = Path(storage_dir) if storage_dir else default_storage_dir()
    env_path = storage / ".env"
    db_path = storage / "anythingllm.db"
    documents = storage / "documents"
    lance = storage / "lancedb"
    schema, schema_evidence, schema_errors = _sqlite_schema(db_path)
    desktop_executable = _desktop_executable()
    desktop_version, desktop_evidence, desktop_errors = _desktop_version(desktop_executable)
    desktop_package, package_evidence, package_errors = _desktop_package_identity(
        desktop_executable,
        include_fingerprint=include_package_fingerprint,
    )
    desktop_version_normalized = _normalized_desktop_version(desktop_version)
    errors = [*schema_errors, *desktop_errors, *package_errors]

    missing = {
        table: sorted(columns - schema.get(table, set()))
        for table, columns in REQUIRED_COLUMNS.items()
        if columns - schema.get(table, set())
    }
    desktop_release_status, desktop_candidate_profile = _desktop_release_status(
        desktop_version_normalized,
        desktop_package.get("app_asar_sha256", ""),
    )
    # Desktop executable/package discovery is useful identity evidence, but a
    # missing or non-standard bundle must not make an otherwise readable local
    # SQLite store invisible.  Storage and Desktop observations remain
    # independently reportable and independently gated.
    storage_schema_match = storage.exists() and db_path.exists() and not missing and not schema_errors
    mutation_profile_match = (
        storage_schema_match
        and env_path.exists()
        and desktop_release_status == "recognized_mutation_profile"
    )
    profile = desktop_candidate_profile if mutation_profile_match else ""
    native_contract = OBSERVED_NATIVE_MUTATION_CONTRACTS.get(
        str(desktop_package.get("app_asar_sha256") or ""),
        {},
    )
    native_contract_match = bool(
        native_contract
        and storage_schema_match
        and desktop_version_normalized == native_contract.get("desktop_version")
    )

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
        _supported(
            [*desktop_evidence, *db_evidence],
            "Required SQLite structures are readable; Desktop release qualification is reported separately.",
        )
        if storage_schema_match else _blocked(
            [*schema_errors, *(f"missing_columns:{k}" for k in missing)],
            "Required SQLite structures were not available for read-only inspection.",
            [*desktop_evidence, *db_evidence],
        )
    )
    capabilities["can_read_workspace_storage"] = (
        _supported(document_evidence, "Document storage directory is present.")
        if documents.exists() else _blocked(["documents_directory_missing"], "Document storage is unavailable.")
    )
    capabilities["can_inspect_lance"] = (
        _supported(lance_evidence, "LanceDB directory is present; package/runtime verification is still required.")
        if lance.exists() else _blocked(["lancedb_directory_missing"], "LanceDB storage is unavailable.")
    )

    if mutation_profile_match:
        capabilities["can_write_env_settings"] = _supported(env_evidence, "Observed profile permits guarded .env writes.")
        capabilities["can_write_sqlite_settings"] = _supported(db_evidence, "Observed profile permits guarded SQLite writes.")
        capabilities["can_restore_snapshotted_settings"] = _supported(
            [*env_evidence, *db_evidence], "Observed profile supports guarded restoration of known settings."
        )
    else:
        for name in ("can_write_env_settings", "can_write_sqlite_settings", "can_restore_snapshotted_settings"):
            capabilities[name] = _blocked(
                ["mutation_profile_not_matched", f"desktop_release_status:{desktop_release_status}"],
                "Mutation is blocked without a recognized guarded-settings profile.",
                desktop_evidence,
            )

    if native_contract_match:
        contract_evidence = [
            _evidence(
                "immutable_native_contract",
                str(native_contract["contract_id"]),
                f"observed_at={native_contract['observed_at']};package_sha256_matched",
            ),
            *package_evidence,
            *db_evidence,
        ]
        for name in native_contract["capabilities"]:
            capabilities[name] = _supported(
                contract_evidence,
                f"Exact package and storage contract match {native_contract['contract_id']}.",
            )
    else:
        for name in ("can_create_workspace", "can_upload_native_metadata", "can_poll_post_upload_state"):
            capabilities[name] = _blocked(
                ["native_mutation_contract_not_matched"],
                "Native mutation is blocked until an exact package contract or isolated probe qualifies it.",
                [*package_evidence, *db_evidence],
            )

    # API capabilities require endpoint probes and remain unknown in filesystem-only characterization.
    result = {
        "schema_version": 3,
        "observed_at": _now(),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "supported_contract": platform.system() == "Windows" and platform.machine().casefold() in {"amd64", "x86_64"},
        },
        "storage_dir": str(storage),
        "desktop_executable": str(desktop_executable) if desktop_executable else "",
        "desktop_version": desktop_version,
        "desktop_version_normalized": desktop_version_normalized,
        "desktop_package": desktop_package,
        "desktop_release_status": desktop_release_status,
        "desktop_candidate_profile": desktop_candidate_profile,
        "storage_schema_status": "matched" if storage_schema_match else "unmatched",
        "matched_profile": profile,
        "native_mutation_contract": (
            str(native_contract.get("contract_id") or "") if native_contract_match else ""
        ),
        "native_mutation_contract_status": "matched" if native_contract_match else "unmatched",
        "profile_status": "matched" if mutation_profile_match else desktop_release_status,
        "observed_compatible_desktop_versions": list(OBSERVED_COMPATIBLE_DESKTOP_VERSIONS),
        "observed_candidate_desktop_versions": list(OBSERVED_CANDIDATE_DESKTOP_VERSIONS),
        "schema": {table: sorted(columns) for table, columns in schema.items()},
        "missing_required_columns": missing,
        "errors": errors,
        "capabilities": {name: asdict(value) for name, value in capabilities.items()},
    }
    if str(api_url or "").strip():
        resources_dir_text = str(desktop_package.get("resources_dir") or "").strip()
        installed_openapi = (
            Path(resources_dir_text) / "backend" / "swagger" / "openapi.json"
            if resources_dir_text
            else None
        )
        result["api_contract"] = probe_api_contract(
            api_url,
            installed_openapi_path=installed_openapi,
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Characterize a local AnythingLLM Desktop installation without mutation.")
    parser.add_argument("--storage-dir", default="")
    parser.add_argument(
        "--api-url",
        default="",
        help="optional loopback AnythingLLM API root for read-only Swagger contract discovery",
    )
    parser.add_argument("--json", action="store_true", help="Emit the complete machine-readable result.")
    args = parser.parse_args()
    result = characterize(args.storage_dir or None, api_url=args.api_url)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"Profile: {result['matched_profile'] or 'unrecognized'}")
        for name, capability in result["capabilities"].items():
            print(f"{name}: {capability['status']} - {capability['message']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

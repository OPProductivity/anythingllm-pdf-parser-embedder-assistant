"""Authoritative, read-only AnythingLLM state resolution.

Stored values, assumed effective values, and runtime verification are kept
separate. Raw evidence is deliberately small and secret-safe.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from anythingllm_compatibility import characterize, default_storage_dir
from embedder_capabilities import resolve_embedder_capability
from segmentation_policy import UNKNOWN_MODEL_HARD_LIMIT


PROVIDER_MODEL_KEYS = {
    "openrouter": ("OPENROUTER_MODEL_PREF",),
    "ollama": ("OLLAMA_MODEL_PREF",),
    "openai": ("OPENAI_MODEL_PREF",),
    "generic-openai": ("GENERIC_OPEN_AI_MODEL_PREF",),
    "gemini": ("GEMINI_MODEL_PREF",),
    "mistral": ("MISTRAL_MODEL_PREF",),
    "cohere": ("COHERE_MODEL_PREF",),
    "voyage": ("VOYAGE_MODEL_PREF",),
    "jinaai": ("JINA_MODEL_PREF",),
}
SECRET_MARKERS = ("KEY", "SECRET", "TOKEN", "PASSWORD")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _hash(value):
    return hashlib.sha256(str(value).encode("utf-8", errors="replace")).hexdigest()


def read_env_values(path: Path):
    values = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in raw or raw.lstrip().startswith("#"):
            continue
        key, value = raw.split("=", 1)
        raw_value = value.strip()
        if len(raw_value) >= 2 and raw_value[0] == "'" and raw_value[-1] == "'":
            values[key.strip()] = raw_value[1:-1]
        elif len(raw_value) >= 2 and raw_value[0] == '"' and raw_value[-1] == '"':
            try:
                decoded = json.loads(raw_value)
            except (TypeError, ValueError, json.JSONDecodeError):
                decoded = raw_value[1:-1]
            values[key.strip()] = decoded if isinstance(decoded, str) else raw_value[1:-1]
        else:
            values[key.strip()] = raw_value
    return values


def evidence(source_type, location, value, secret=False):
    text = str(value or "")
    return {
        "source_type": source_type,
        "location": location,
        "observed_at": _now(),
        "value": "" if secret else text,
        "present": bool(text),
        "value_hash": _hash(text) if text else "",
    }


def resolved_field(stored=None, source=None, effective=None, basis="unknown", confidence="low",
                   raw_sources=None, conflicting_sources=None, notes=None):
    return {
        "stored": stored,
        "source": source or "",
        "effective": effective,
        "effective_basis": basis,
        "confidence": confidence,
        "raw_sources": raw_sources or [],
        "conflicting_sources": conflicting_sources or [],
        "resolution_notes": notes or [],
    }


def _chunk_values(storage):
    result = {"size": None, "overlap": None, "sources": [], "errors": []}
    db_path = storage / "anythingllm.db"
    if not db_path.exists():
        result["errors"].append("sqlite_database_missing")
        return result
    con = None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rows = dict(con.execute(
            "select label,value from system_settings where label in (?,?)",
            ("text_splitter_chunk_size", "text_splitter_chunk_overlap"),
        ).fetchall())
        if "text_splitter_chunk_size" in rows:
            result["size"] = int(rows["text_splitter_chunk_size"])
            result["sources"].append(evidence("sqlite", "system_settings.text_splitter_chunk_size", rows["text_splitter_chunk_size"]))
        if "text_splitter_chunk_overlap" in rows:
            result["overlap"] = int(rows["text_splitter_chunk_overlap"])
            result["sources"].append(evidence("sqlite", "system_settings.text_splitter_chunk_overlap", rows["text_splitter_chunk_overlap"]))
    except Exception as exc:
        result["errors"].append(f"chunk_settings_read_error:{type(exc).__name__}")
    finally:
        if con is not None:
            con.close()
    return result


def _positive_int(value):
    try:
        number = int(str(value or "").strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def resolve_state(storage_dir=None, runtime_verification=None, workspace_state=None):
    storage = Path(storage_dir) if storage_dir else default_storage_dir()
    compatibility = characterize(storage)
    env_path = storage / ".env"
    values = read_env_values(env_path)
    anomalies = []

    engine = (values.get("EMBEDDING_ENGINE") or "").strip().casefold()
    generic_model = (values.get("EMBEDDING_MODEL_PREF") or "").strip()
    provider_entries = []
    for provider, keys in PROVIDER_MODEL_KEYS.items():
        for key in keys:
            value = (values.get(key) or "").strip()
            if value:
                provider_entries.append({"provider": provider, "key": key, "value": value})
    active = next((row for row in provider_entries if row["provider"] == engine), None)
    active_model = active["value"] if active else ""
    active_key = active["key"] if active else ""
    conflicts = []
    if generic_model and active_model and generic_model != active_model:
        conflicts = [
            evidence("env", "EMBEDDING_MODEL_PREF", generic_model),
            evidence("env", active_key, active_model),
        ]
        anomalies.append("generic_provider_precedence_unverified")
        anomalies.append("provider_model_mismatch")

    if not engine:
        anomalies.append("embedder_engine_missing")
    if engine not in {"anythingllm", "built-in", "default", "native"} and not (generic_model or active_model):
        anomalies.append("embedder_model_missing")

    runtime_model = (runtime_verification or {}).get("model")
    configured_hard_limit = _positive_int(values.get("EMBEDDING_MODEL_MAX_CHUNK_LENGTH"))
    if runtime_model:
        effective_model = runtime_model
        basis = "runtime_verified"
        confidence = "high"
    elif active_model and not generic_model:
        effective_model = active_model
        basis = "profile_defined"
        confidence = "medium"
    elif generic_model and not active_model:
        effective_model = generic_model
        basis = "profile_defined"
        confidence = "medium"
    else:
        effective_model = generic_model or active_model
        basis = "heuristic_assumption" if effective_model else "unknown"
        confidence = "low"
    if not runtime_model:
        anomalies.append("runtime_verification_unavailable")

    # A missing AnythingLLM environment override is not evidence that a known
    # embedder only supports the generic 4096-token fallback.  Prefer the
    # curated capability contract for the resolved engine/model; retain the
    # conservative fallback only for genuinely unknown models.  This state is
    # planning evidence, not a mutation of AnythingLLM's own setting.
    capability = resolve_embedder_capability(engine, effective_model)
    catalog_hard_limit = _positive_int(capability.get("safe_max_chunk_length"))
    if configured_hard_limit:
        effective_hard_limit = configured_hard_limit
        hard_limit_basis = "profile_defined"
        hard_limit_confidence = "medium"
        hard_limit_notes = ["Uses the positive limit configured in AnythingLLM."]
    elif capability.get("status") != "unknown_capability" and catalog_hard_limit:
        effective_hard_limit = catalog_hard_limit
        hard_limit_basis = "catalog_capability"
        hard_limit_confidence = "high" if capability.get("verified") else "medium"
        hard_limit_notes = [
            "AnythingLLM has no positive max-chunk override; uses the resolved embedder capability contract.",
            str(capability.get("source_note") or "").strip(),
        ]
    else:
        effective_hard_limit = UNKNOWN_MODEL_HARD_LIMIT
        hard_limit_basis = "conservative_fallback"
        hard_limit_confidence = "low"
        hard_limit_notes = ["Falls back to the conservative unknown-model limit when AnythingLLM does not expose a positive max chunk length."]

    chunk = _chunk_values(storage)
    anomalies.extend(chunk["errors"])
    if chunk["size"] is not None and chunk["overlap"] is not None and chunk["overlap"] >= chunk["size"]:
        anomalies.append("chunk_overlap_invalid")

    llm_provider = (values.get("LLM_PROVIDER") or "").strip()
    llm_model = (values.get("MODEL_PREF") or values.get("LLM_MODEL_PREF") or "").strip()
    workspace_state = workspace_state or {}
    if workspace_state.get("model") and llm_model and workspace_state["model"] != llm_model:
        anomalies.append("chat_workspace_model_mismatch")

    secret_sources = [
        evidence("env", key, value, secret=True)
        for key, value in values.items()
        if any(marker in key.upper() for marker in SECRET_MARKERS)
    ]
    return {
        "schema_version": 1,
        "observed_at": _now(),
        "compatibility": compatibility,
        "chat_llm": {
            "global_provider": resolved_field(
                llm_provider, "LLM_PROVIDER", llm_provider, "profile_defined", "medium",
                [evidence("env", "LLM_PROVIDER", llm_provider)],
            ),
            "global_model": resolved_field(
                llm_model, "MODEL_PREF/LLM_MODEL_PREF", llm_model, "heuristic_assumption", "low",
                [evidence("env", "MODEL_PREF/LLM_MODEL_PREF", llm_model)],
            ),
            "workspace": workspace_state,
        },
        "embedder": {
            "engine": resolved_field(
                engine, "EMBEDDING_ENGINE", engine, "profile_defined", "medium",
                [evidence("env", "EMBEDDING_ENGINE", engine)],
            ),
            "model": resolved_field(
                {"generic": generic_model, "provider_specific": active_model},
                "AnythingLLM .env",
                effective_model,
                basis,
                confidence,
                [
                    evidence("env", "EMBEDDING_MODEL_PREF", generic_model),
                    evidence("env", active["key"], active_model) if active else evidence("env", "provider-specific model key", ""),
                ],
                conflicts,
                ["Generic/provider precedence remains unverified when both values are populated."],
            ),
            "hard_limit": resolved_field(
                configured_hard_limit,
                "EMBEDDING_MODEL_MAX_CHUNK_LENGTH",
                effective_hard_limit,
                hard_limit_basis,
                hard_limit_confidence,
                [evidence("env", "EMBEDDING_MODEL_MAX_CHUNK_LENGTH", values.get("EMBEDDING_MODEL_MAX_CHUNK_LENGTH", ""))],
                [],
                [note for note in hard_limit_notes if note],
            ),
            "runtime_verification": runtime_verification or {"status": "not_run"},
            "secret_sources": secret_sources,
        },
        "chunking": {
            "size": resolved_field(chunk["size"], "system_settings", chunk["size"], "profile_defined", "medium", chunk["sources"]),
            "overlap": resolved_field(chunk["overlap"], "system_settings", chunk["overlap"], "profile_defined", "medium", chunk["sources"]),
        },
        "validation": runtime_verification or {"status": "not_run"},
        "anomalies": sorted(set(anomalies)),
    }


def sanitized_json(state):
    return json.dumps(state, ensure_ascii=False, sort_keys=True)

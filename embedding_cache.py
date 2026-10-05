"""Bounded integrity and active-contract checks for Desktop's vector cache."""

from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import struct
import uuid

CACHE_CONTRACT = "openrouter-v117-cache-identity-1"
CACHE_MARKER = "pdf-assistant-embedding-cache-contract.json"
MAX_CACHE_BYTES = 64 * 1024 * 1024


def cache_configuration(storage_dir):
    """Read non-secret persisted controls; uncertainty supplies no reuse authority."""
    if not storage_dir:
        return {"unavailable": True}
    storage = Path(storage_dir)
    marker_path = storage / CACHE_MARKER
    if not marker_path.is_file():
        return {"unavailable": True}
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if marker.get("contract") != CACHE_CONTRACT:
            return {"unavailable": True}
        from anythingllm_state import read_env_values

        values = read_env_values(storage / ".env")
        engine = str(values.get("EMBEDDING_ENGINE") or "").strip().lower()
        if engine != "openrouter":
            return {"unavailable": True}
        database = (storage / "anythingllm.db").resolve()
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0.1)) as connection:
            rows = dict(connection.execute(
                "select label,value from system_settings "
                "where label in ('text_splitter_chunk_size','text_splitter_chunk_overlap')"
            ))
        return {
            "engine": engine,
            "model": str(values.get("EMBEDDING_MODEL_PREF") or "baai/bge-m3"),
            "chunk_size": str(rows.get("text_splitter_chunk_size") or "1000"),
            "chunk_overlap": str(
                rows.get("text_splitter_chunk_overlap")
                if rows.get("text_splitter_chunk_overlap") is not None else "20"
            ),
            "chunk_limit": str(values.get("EMBEDDING_MODEL_MAX_CHUNK_LENGTH") or ""),
        }
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return {"unavailable": True}


def cache_entry_usable(storage_dir, location, *, configuration=None, expected_metadata=None):
    """Validate vectors, provenance, hashes, and the active embedding controls."""
    if not storage_dir or not str(location or "").strip():
        return False
    root = Path(storage_dir) / "vector-cache"
    normalized = str(location).replace("\\", "/")
    active_configuration = (
        cache_configuration(storage_dir) if configuration is None else configuration
    )
    if not isinstance(active_configuration, dict) or active_configuration.get("unavailable"):
        return False
    for candidate in dict.fromkeys((str(location), normalized)):
        path = root / (str(uuid.uuid5(uuid.NAMESPACE_URL, candidate)) + ".json")
        try:
            if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_CACHE_BYTES:
                continue
            raw = path.read_bytes()
            if len(raw) > MAX_CACHE_BYTES:
                continue
            groups = json.loads(raw)
            if not isinstance(groups, list) or not groups or any(
                not isinstance(group, list) or not group for group in groups
            ):
                continue
            dimension = None
            valid = True
            for chunk in (item for group in groups for item in group):
                if not isinstance(chunk, dict) or not isinstance(chunk.get("metadata"), dict):
                    valid = False
                    break
                vector = chunk.get("values")
                if (
                    not isinstance(vector, list)
                    or not vector
                    or any(
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(value)
                        for value in vector
                    )
                ):
                    valid = False
                    break
                dimension = len(vector) if dimension is None else dimension
                if len(vector) != dimension:
                    valid = False
                    break
                metadata = chunk["metadata"]
                if any(
                    str(metadata.get(name) or "") != str(value)
                    for name, value in (expected_metadata or {}).items()
                    if name in ("docSource", "chunkSource") and value
                ):
                    valid = False
                    break
                proof = chunk.get("pdfAssistantCacheIdentity")
                if (
                    not isinstance(proof, dict)
                    or proof.get("contract") != CACHE_CONTRACT
                    or proof.get("configuration") != active_configuration
                ):
                    valid = False
                    break
                text = "\0".join(
                    str(metadata.get(name) or "")
                    for name in ("text", "docSource", "chunkSource")
                )
                text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
                vector_hash = hashlib.sha256(
                    struct.pack("<" + "d" * len(vector), *vector)
                ).hexdigest()
                if proof.get("text_sha256") != text_hash or proof.get("vector_sha256") != vector_hash:
                    valid = False
                    break
            if valid:
                return True
        except (OSError, ValueError, TypeError, OverflowError, struct.error):
            continue
    return False

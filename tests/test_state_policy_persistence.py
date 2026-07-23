import json
import sqlite3

import pytest

from anythingllm_persistence import AnythingLLMPersistenceAdapter
from anythingllm_state import resolve_state, sanitized_json
from segmentation_policy import (
    ALGORITHM_VERSION,
    UNKNOWN_MODEL_HARD_LIMIT,
    policy_for,
    source_span_identity,
)


pytestmark = pytest.mark.offline_deterministic


def create_storage(path):
    (path / "documents").mkdir()
    (path / "lancedb").mkdir()
    con = sqlite3.connect(path / "anythingllm.db")
    con.execute("create table system_settings (label text, value text)")
    con.execute(
        "create table workspaces (id integer, name text, slug text, chatProvider text, chatModel text, "
        "topN integer, similarityThreshold real, vectorSearchMode text, chatMode text)"
    )
    con.execute(
        "create table workspace_documents (id integer, docId text, filename text, docpath text, "
        "metadata text, createdAt text)"
    )
    con.execute("create table document_vectors (docId text, vectorId text)")
    con.execute("insert into system_settings values ('text_splitter_chunk_size','768')")
    con.execute("insert into system_settings values ('text_splitter_chunk_overlap','128')")
    con.commit()
    con.close()


def test_resolver_preserves_conflict_and_does_not_claim_precedence(tmp_path):
    create_storage(tmp_path)
    secret = "must-not-leak"  # pragma: allowlist secret -- redaction fixture
    (tmp_path / ".env").write_text(
        "EMBEDDING_ENGINE='openrouter'\n"
        "EMBEDDING_MODEL_PREF='generic-model'\n"
        "OPENROUTER_MODEL_PREF='provider-model'\n"
        f"OPENROUTER_API_KEY='{secret}'\n"
        "LLM_PROVIDER='openrouter'\n"
        "MODEL_PREF='chat-model'\n",
        encoding="utf-8",
    )

    state = resolve_state(tmp_path)

    assert state["embedder"]["model"]["effective_basis"] == "heuristic_assumption"
    assert "generic_provider_precedence_unverified" in state["anomalies"]
    assert state["chunking"]["size"]["effective"] == 768
    assert secret not in sanitized_json(state)


def test_runtime_verified_model_wins_without_rewriting_stored_evidence(tmp_path):
    create_storage(tmp_path)
    (tmp_path / ".env").write_text(
        "EMBEDDING_ENGINE='openrouter'\nEMBEDDING_MODEL_PREF='stored-model'\n",
        encoding="utf-8",
    )

    state = resolve_state(tmp_path, runtime_verification={"status": "pass", "model": "runtime-model"})

    assert state["embedder"]["model"]["effective"] == "runtime-model"
    assert state["embedder"]["model"]["effective_basis"] == "runtime_verified"
    assert state["embedder"]["model"]["stored"]["generic"] == "stored-model"


def test_resolver_exposes_embedder_hard_limit_contract(tmp_path):
    (tmp_path / ".env").write_text(
        "EMBEDDING_ENGINE='ollama'\n"
        "OLLAMA_MODEL_PREF='nomic-embed-text'\n"
        "EMBEDDING_MODEL_MAX_CHUNK_LENGTH='2048'\n",
        encoding="utf-8",
    )

    state = resolve_state(tmp_path)

    assert state["embedder"]["hard_limit"]["stored"] == 2048
    assert state["embedder"]["hard_limit"]["effective"] == 2048
    assert state["embedder"]["hard_limit"]["source"] == "EMBEDDING_MODEL_MAX_CHUNK_LENGTH"


def test_mode_contracts_are_quantitatively_distinct():
    unsegmented = policy_for("none")
    page_limit = policy_for("page_limit")
    passages = policy_for("passages")

    assert not unsegmented.page_local
    assert unsegmented.target_drift_fraction == pytest.approx(0.0)
    assert page_limit.page_local and passages.page_local
    assert page_limit.target_drift_fraction == pytest.approx(0.30)
    assert passages.target_drift_fraction == pytest.approx(0.20)
    assert page_limit.small_tail_fraction == pytest.approx(0.35)
    assert passages.small_tail_fraction == pytest.approx(0.45)
    assert UNKNOWN_MODEL_HARD_LIMIT == 4096


def test_source_span_identity_is_stable_and_version_sensitive():
    first = source_span_identity("doc", 64, "page", 10, 20, 1)
    second = source_span_identity("doc", 64, "page", 10, 20, 1)
    changed = source_span_identity("doc", 64, "page", 10, 20, 1, ALGORITHM_VERSION + "-changed")

    assert first == second
    assert first != changed


def test_guarded_sqlite_write_snapshots_and_restores(tmp_path):
    storage = tmp_path / "storage"
    storage.mkdir()
    create_storage(storage)
    (storage / ".env").write_text("EMBEDDING_ENGINE='openrouter'\n", encoding="utf-8")
    adapter = AnythingLLMPersistenceAdapter(storage, "run-1", tmp_path / "snapshots")

    result = adapter.write_sqlite_setting("text_splitter_chunk_size", 900)
    snapshot = json.loads((tmp_path / "snapshots" / "anythingllm-settings-snapshot-before.json").read_text())

    assert result["status"] == "verified"
    assert snapshot["sqlite"]["text_splitter_chunk_size"] == "768"
    adapter.restore(result["snapshot"])
    con = sqlite3.connect(storage / "anythingllm.db")
    try:
        assert con.execute(
            "select value from system_settings where label='text_splitter_chunk_size'"
        ).fetchone()[0] == "768"
    finally:
        con.close()


def test_env_snapshot_redacts_secret_and_restore_skips_unrecoverable_secret(tmp_path):
    storage = tmp_path / "storage"
    storage.mkdir()
    create_storage(storage)
    secret = "do-not-copy"  # pragma: allowlist secret -- redaction fixture
    (storage / ".env").write_text(
        f"OPENROUTER_API_KEY='{secret}'\nEMBEDDING_ENGINE='openrouter'\n",
        encoding="utf-8",
    )
    adapter = AnythingLLMPersistenceAdapter(storage, "run-2", tmp_path / "snapshots")

    path = adapter.snapshot(env_keys=["OPENROUTER_API_KEY", "EMBEDDING_ENGINE"])
    payload_text = path.read_text(encoding="utf-8")
    payload = json.loads(payload_text)

    assert secret not in payload_text
    assert payload["env"]["OPENROUTER_API_KEY"]["present"] is True
    assert payload["env"]["OPENROUTER_API_KEY"]["value"] is None
    assert payload["env"]["EMBEDDING_ENGINE"]["value"] == "openrouter"

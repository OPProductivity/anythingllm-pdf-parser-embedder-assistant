import json
import sqlite3
from pathlib import Path

import pytest

import anythingllm_persistence
from anythingllm_persistence import AnythingLLMPersistenceAdapter, _env_value
from anythingllm_state import read_env_values, resolve_state, sanitized_json
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


def guarded_mutation_adapter(storage, run_id, snapshot_dir, monkeypatch):
    """Build a fixture with explicit mutation authority for write-mechanism tests.

    These tests exercise snapshot/write/restore mechanics, not Desktop version
    discovery. Injecting the capability prevents a temporary SQLite fixture
    without a Desktop executable from weakening the production profile gate.
    """
    supported = {"status": "supported", "message": "test capability"}
    monkeypatch.setattr(
        anythingllm_persistence,
        "characterize",
        lambda _storage, **_kwargs: {
            "matched_profile": "test-recognized-mutation-profile",
            "capabilities": {
                "can_write_env_settings": dict(supported),
                "can_write_sqlite_settings": dict(supported),
                "can_restore_snapshotted_settings": dict(supported),
            },
        },
    )
    return AnythingLLMPersistenceAdapter(storage, run_id, snapshot_dir)


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


def test_guarded_sqlite_write_snapshots_and_restores(tmp_path, monkeypatch):
    storage = tmp_path / "storage"
    storage.mkdir()
    create_storage(storage)
    (storage / ".env").write_text("EMBEDDING_ENGINE='openrouter'\n", encoding="utf-8")
    adapter = guarded_mutation_adapter(storage, "run-1", tmp_path / "snapshots", monkeypatch)

    result = adapter.write_sqlite_setting("text_splitter_chunk_size", 900)
    snapshot = json.loads(Path(result["snapshot"]).read_text())

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


def test_guarded_group_writes_share_one_snapshot_and_remain_atomic(tmp_path, monkeypatch):
    storage = tmp_path / "storage"
    storage.mkdir()
    create_storage(storage)
    env_path = storage / ".env"
    env_path.write_text("EMBEDDING_ENGINE='anythingllm'\nEMBEDDING_MODEL_PREF='old'\n", encoding="utf-8")
    adapter = guarded_mutation_adapter(storage, "group-write", tmp_path / "snapshots", monkeypatch)

    env_result = adapter.write_env_settings({
        "EMBEDDING_ENGINE": "openai",
        "OPENAI_MODEL_PREF": "text-embedding-3-small",
        "EMBEDDING_MODEL_PREF": "text-embedding-3-small",
    })
    sqlite_result = adapter.write_sqlite_settings({
        "text_splitter_chunk_size": 900,
        "text_splitter_chunk_overlap": 80,
    })

    env_snapshot = json.loads(Path(env_result["snapshot"]).read_text(encoding="utf-8"))
    sqlite_snapshot = json.loads(Path(sqlite_result["snapshot"]).read_text(encoding="utf-8"))
    assert set(env_snapshot["env"]) == {"EMBEDDING_ENGINE", "OPENAI_MODEL_PREF", "EMBEDDING_MODEL_PREF"}
    assert set(sqlite_snapshot["sqlite"]) == {"text_splitter_chunk_size", "text_splitter_chunk_overlap"}
    assert _env_value(env_path.read_text(encoding="utf-8"), "EMBEDDING_ENGINE") == "openai"
    assert _env_value(env_path.read_text(encoding="utf-8"), "EMBEDDING_MODEL_PREF") == "text-embedding-3-small"

    adapter.restore(env_result["snapshot"])
    adapter.restore(sqlite_result["snapshot"])
    con = sqlite3.connect(storage / "anythingllm.db")
    try:
        values = dict(con.execute("select label, value from system_settings"))
    finally:
        con.close()
    assert _env_value(env_path.read_text(encoding="utf-8"), "EMBEDDING_ENGINE") == "anythingllm"
    assert _env_value(env_path.read_text(encoding="utf-8"), "EMBEDDING_MODEL_PREF") == "old"
    assert values["text_splitter_chunk_size"] == "768"
    assert values["text_splitter_chunk_overlap"] == "128"


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


def test_identical_settings_state_reuses_one_snapshot(tmp_path, monkeypatch):
    storage = tmp_path / "storage"
    storage.mkdir()
    create_storage(storage)
    (storage / ".env").write_text("EMBEDDING_ENGINE='openrouter'\n", encoding="utf-8")
    adapter = guarded_mutation_adapter(storage, "deduplicated", tmp_path / "snapshots", monkeypatch)

    first = adapter.snapshot(env_keys=["EMBEDDING_ENGINE"])
    second = adapter.snapshot(env_keys=["EMBEDDING_ENGINE"])

    assert second == first
    assert len(list((tmp_path / "snapshots").glob("anythingllm-settings-snapshot-*.json"))) == 1


def test_guarded_env_write_rejects_multiline_or_malformed_assignments(tmp_path, monkeypatch):
    storage = tmp_path / "storage"
    storage.mkdir()
    create_storage(storage)
    env_path = storage / ".env"
    env_path.write_text("EMBEDDING_ENGINE='openrouter'\n", encoding="utf-8")
    adapter = guarded_mutation_adapter(storage, "run-3", tmp_path / "snapshots", monkeypatch)

    with pytest.raises(ValueError, match="Unsupported environment setting name"):
        adapter.write_env_setting("EMBEDDING_ENGINE\nOTHER_SETTING", "ollama")
    with pytest.raises(ValueError, match="single-line"):
        adapter.write_env_setting("EMBEDDING_ENGINE", "ollama\nOTHER_SETTING='changed'")

    assert env_path.read_text(encoding="utf-8") == "EMBEDDING_ENGINE='openrouter'\n"


def test_guarded_env_write_round_trips_quotes_and_keeps_each_snapshot(tmp_path, monkeypatch):
    storage = tmp_path / "storage"
    storage.mkdir()
    create_storage(storage)
    env_path = storage / ".env"
    env_path.write_text("EMBEDDING_ENGINE='openrouter'\n", encoding="utf-8")
    adapter = guarded_mutation_adapter(storage, "run-4", tmp_path / "snapshots", monkeypatch)
    expected = 'provider with "quotes", apostrophe\'s, and \\slashes'

    first = adapter.write_env_setting("EMBEDDING_ENGINE", expected)
    second = adapter.write_env_setting("LLM_PROVIDER", "openrouter")

    written = env_path.read_text(encoding="utf-8")
    assert _env_value(written, "EMBEDDING_ENGINE") == expected
    assert first["snapshot"] != second["snapshot"]
    assert Path(first["snapshot"]).is_file()
    assert Path(second["snapshot"]).is_file()

    restored = adapter.restore(first["snapshot"])

    assert restored["restored"]["env"] == ["EMBEDDING_ENGINE"]
    assert _env_value(env_path.read_text(encoding="utf-8"), "EMBEDDING_ENGINE") == "openrouter"


def test_env_restore_removes_a_setting_that_was_absent_in_the_snapshot(tmp_path, monkeypatch):
    storage = tmp_path / "storage"
    storage.mkdir()
    create_storage(storage)
    env_path = storage / ".env"
    env_path.write_text("EMBEDDING_ENGINE='openrouter'\n", encoding="utf-8")
    adapter = guarded_mutation_adapter(storage, "run-absent-env", tmp_path / "snapshots", monkeypatch)

    write = adapter.write_env_setting("LLM_PROVIDER", "openrouter")
    assert _env_value(env_path.read_text(encoding="utf-8"), "LLM_PROVIDER") == "openrouter"

    restored = adapter.restore(write["snapshot"])

    assert "LLM_PROVIDER" in restored["restored"]["env"]
    assert _env_value(env_path.read_text(encoding="utf-8"), "LLM_PROVIDER") is None


def test_state_reader_decodes_json_quoted_environment_values(tmp_path):
    expected = 'model with "quotes", apostrophe\'s, and \\slashes'
    env_path = tmp_path / ".env"
    env_path.write_text(f"EMBEDDING_MODEL_PREF={json.dumps(expected)}\n", encoding="utf-8")

    assert read_env_values(env_path)["EMBEDDING_MODEL_PREF"] == expected

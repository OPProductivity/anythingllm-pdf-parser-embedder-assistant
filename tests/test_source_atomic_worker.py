import hashlib
import json
import sys
import threading
from pathlib import Path
from unittest import mock

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import anythingllm_source_atomic_worker as source_atomic  # noqa: E402
import auto_anythingllm_pipeline as pipeline  # noqa: E402


pytestmark = pytest.mark.offline_deterministic


def _qualified_report(executable):
    return {
        "status": "pass",
        "characterization": {
            "desktop_version_normalized": "1.16.1",
            "native_mutation_contract": source_atomic.V1161_NATIVE_CONTRACT_ID,
            "desktop_package": {
                "app_asar_sha256": source_atomic.OBSERVED_CANDIDATE_PACKAGE_FINGERPRINTS["1.16.1"],
            },
            "desktop_executable": str(executable),
        },
    }


def _fixture_worker():
    return (
        '"use strict";\n'
        'async function ah(){let legacyWorkerBehavior=true;return legacyWorkerBehavior}\n'
        'process.on("message",async s=>{});\n'
    )


def test_hybrid_patch_keeps_legacy_branch_and_is_idempotent():
    patched = source_atomic.patch_v1161_embedding_worker_source(_fixture_worker())

    assert source_atomic.SOURCE_ATOMIC_PATCH_ID in patched
    assert source_atomic.SOURCE_ATOMIC_OPENROUTER_GATE in patched
    configured_cap = source_atomic.SOURCE_ATOMIC_DEFAULT_PROVIDER_BATCH_SIZE
    assert (
        f'SOURCE_ATOMIC_EMBED_BATCH_SIZE||"{configured_cap}",10)||{configured_cap}'
        in patched
    )
    assert "legacyWorkerBehavior=true" in patched
    assert source_atomic.patch_v1161_embedding_worker_source(patched) == patched


def test_current_patch_migrates_only_the_exact_known_v1_worker(tmp_path, monkeypatch):
    worker = tmp_path / "embedding-worker.js"
    original = _fixture_worker()
    worker.write_text(original, encoding="utf-8")
    executable = tmp_path / "AnythingLLM.exe"
    executable.write_bytes(b"desktop")
    report = _qualified_report(executable)

    backup = worker.with_name(f"{worker.name}.pdf-assistant-v1161.backup")
    backup.write_text(original, encoding="utf-8")
    monkeypatch.setattr(
        source_atomic,
        "V1161_EMBEDDING_WORKER_SHA256",
        hashlib.sha256(backup.read_bytes()).hexdigest(),
    )
    legacy = source_atomic._legacy_v1_patched_worker_source(
        backup.read_bytes().decode("utf-8")
    )
    worker.write_bytes(legacy.encode("utf-8"))

    migrated = source_atomic.ensure_source_atomic_embedding_worker(report, worker_path=worker)

    assert migrated["status"] == "restart_required", migrated
    assert migrated["upgraded_from_patch_id"] == source_atomic.SOURCE_ATOMIC_LEGACY_PATCH_ID
    patched = worker.read_text(encoding="utf-8")
    assert source_atomic.SOURCE_ATOMIC_PATCH_ID in patched
    assert source_atomic.SOURCE_ATOMIC_OPENROUTER_GATE in patched


def test_current_patch_migrates_only_the_exact_known_v2_worker(tmp_path, monkeypatch):
    worker = tmp_path / "embedding-worker.js"
    original = _fixture_worker()
    worker.write_text(original, encoding="utf-8")
    executable = tmp_path / "AnythingLLM.exe"
    executable.write_bytes(b"desktop")
    report = _qualified_report(executable)
    backup = worker.with_name(f"{worker.name}.pdf-assistant-v1161.backup")
    backup.write_text(original, encoding="utf-8")
    monkeypatch.setattr(
        source_atomic,
        "V1161_EMBEDDING_WORKER_SHA256",
        hashlib.sha256(backup.read_bytes()).hexdigest(),
    )
    previous = source_atomic._previous_v2_patched_worker_source(
        backup.read_bytes().decode("utf-8")
    )
    worker.write_bytes(previous.encode("utf-8"))

    migrated = source_atomic.ensure_source_atomic_embedding_worker(report, worker_path=worker)

    assert migrated["status"] == "restart_required", migrated
    assert migrated["upgraded_from_patch_id"] == source_atomic.SOURCE_ATOMIC_PREVIOUS_PATCH_ID
    patched = worker.read_text(encoding="utf-8")
    assert source_atomic.SOURCE_ATOMIC_PATCH_ID in patched
    assert "source_atomic_gate_observed" in patched


def test_installer_is_a_noop_without_exact_v1161_authority(tmp_path):
    worker = tmp_path / "embedding-worker.js"
    original = _fixture_worker()
    worker.write_text(original, encoding="utf-8")
    executable = tmp_path / "AnythingLLM.exe"
    executable.write_bytes(b"desktop")
    report = _qualified_report(executable)
    report["characterization"]["desktop_version_normalized"] = "1.16.0"

    result = source_atomic.ensure_source_atomic_embedding_worker(report, worker_path=worker)

    assert result["enabled"] is False
    assert result["status"] == "disabled"
    assert worker.read_text(encoding="utf-8") == original
    assert not worker.with_name(f"{worker.name}.pdf-assistant-v1161.backup").exists()


def test_installer_is_exact_backuped_and_idempotent(tmp_path, monkeypatch):
    worker = tmp_path / "embedding-worker.js"
    original = _fixture_worker()
    worker.write_text(original, encoding="utf-8")
    # Text-mode fixtures can acquire CRLF line endings on Windows; the
    # installer fingerprints the actual worker bytes, as production does.
    original_sha = hashlib.sha256(worker.read_bytes()).hexdigest()
    monkeypatch.setattr(source_atomic, "V1161_EMBEDDING_WORKER_SHA256", original_sha)

    executable = tmp_path / "AnythingLLM.exe"
    executable.write_bytes(b"desktop")
    report = _qualified_report(executable)
    installed = source_atomic.ensure_source_atomic_embedding_worker(report, worker_path=worker)

    assert installed["status"] == "restart_required"
    assert installed["installed"] is True
    assert installed["enabled"] is False
    assert installed["restart_required"] is True
    assert source_atomic.SOURCE_ATOMIC_PATCH_ID in worker.read_text(encoding="utf-8")
    backup = worker.with_name(f"{worker.name}.pdf-assistant-v1161.backup")
    assert backup.read_text(encoding="utf-8") == original
    manifest = json.loads(
        worker.with_name(f"{worker.name}.pdf-assistant-source-atomic.json").read_text(encoding="utf-8")
    )
    assert manifest["original_worker_sha256"] == original_sha
    assert manifest["provider_batch_size"] == 36

    monkeypatch.setattr(source_atomic, "_desktop_root_started_after", lambda *_args: (True, ""))
    repeated = source_atomic.ensure_source_atomic_embedding_worker(report, worker_path=worker)
    assert repeated["status"] == "already_enabled"
    assert repeated["enabled"] is True
    assert repeated["restart_required"] is False


def test_existing_patch_requires_desktop_restart_until_a_new_root_is_observed(tmp_path, monkeypatch):
    worker = tmp_path / "embedding-worker.js"
    original = _fixture_worker()
    worker.write_text(original, encoding="utf-8")
    original_sha = hashlib.sha256(worker.read_bytes()).hexdigest()
    monkeypatch.setattr(source_atomic, "V1161_EMBEDDING_WORKER_SHA256", original_sha)
    executable = tmp_path / "AnythingLLM.exe"
    executable.write_bytes(b"desktop")
    report = _qualified_report(executable)

    source_atomic.ensure_source_atomic_embedding_worker(report, worker_path=worker)
    monkeypatch.setattr(
        source_atomic,
        "_desktop_root_started_after",
        lambda *_args: (False, "anythingllm_desktop_not_running"),
    )
    waiting = source_atomic.ensure_source_atomic_embedding_worker(report, worker_path=worker)

    assert waiting["status"] == "restart_required"
    assert waiting["enabled"] is False
    assert waiting["restart_required"] is True
    assert waiting["reason"] == "anythingllm_desktop_not_running"


def test_desktop_restart_observer_accepts_live_cim_datetime_output(tmp_path, monkeypatch):
    executable = tmp_path / "AnythingLLM.exe"
    executable.write_bytes(b"desktop")
    monkeypatch.setattr(source_atomic.os, "name", "nt")
    completed = mock.Mock(
        returncode=0,
        stdout='"2026-08-31T18:30:00.0000000Z"',
    )
    monkeypatch.setattr(source_atomic.subprocess, "run", lambda *_args, **_kwargs: completed)

    active, reason = source_atomic._desktop_root_started_after(
        executable,
        1788190000.0,
    )

    assert active is True
    assert reason == ""


def test_installer_refuses_unexpected_worker_hash(tmp_path):
    worker = tmp_path / "embedding-worker.js"
    worker.write_text(_fixture_worker(), encoding="utf-8")

    result = source_atomic.ensure_source_atomic_embedding_worker(
        _qualified_report(tmp_path / "AnythingLLM.exe"), worker_path=worker
    )

    assert result["enabled"] is False
    assert result["reason"] == "v1_16_1_embedding_worker_hash_not_matched"


def test_installer_preserves_an_unknown_existing_backup(tmp_path, monkeypatch):
    worker = tmp_path / "embedding-worker.js"
    worker.write_text(_fixture_worker(), encoding="utf-8")
    monkeypatch.setattr(
        source_atomic,
        "V1161_EMBEDDING_WORKER_SHA256",
        hashlib.sha256(worker.read_bytes()).hexdigest(),
    )
    backup = worker.with_name(f"{worker.name}.pdf-assistant-v1161.backup")
    backup.write_text("unrelated backup", encoding="utf-8")

    result = source_atomic.ensure_source_atomic_embedding_worker(
        _qualified_report(tmp_path / "AnythingLLM.exe"), worker_path=worker
    )

    assert result["enabled"] is False
    assert result["reason"] == "source_atomic_worker_existing_backup_hash_mismatch"
    assert backup.read_text(encoding="utf-8") == "unrelated backup"


def test_explicit_precommit_rejection_allows_the_next_source_window():
    class FakeThread:
        def join(self, timeout=None):
            return None

    def fake_listener(_api_url, _api_key, _workspace, locations, **kwargs):
        observer = kwargs["observer_callback"]
        first = str(locations[0])
        if first.endswith("a.json"):
            events = [
                {
                    "type": "source_rejected_before_commit",
                    "filename": first,
                    "sourceKey": "file:a.pdf",
                    "error": "provider rejected test source",
                }
            ]
        else:
            events = [
                {"type": "doc_starting", "filename": first, "docIndex": 0, "totalDocs": 1},
                {"type": "doc_complete", "filename": first, "docIndex": 0, "totalDocs": 1},
            ]
        for event in events:
            observer(dict(event))
        connected = threading.Event()
        connected.set()
        return {
            "stop_event": threading.Event(),
            "thread": FakeThread(),
            "connected_event": connected,
            "events": events,
            "errors": [],
        }

    class ImmediateTracker:
        def wait(self, timeout=None):
            return True

        def outcome(self):
            return {"kind": "http_response", "status": 200, "response_text": "{}"}

        def close_response_read(self):
            return None

        def join(self, timeout=None):
            return None

        def is_alive(self):
            return False

    with (
        mock.patch.object(pipeline, "start_anythingllm_embed_progress_listener", side_effect=fake_listener),
        mock.patch.object(pipeline, "start_json_post_response_tracker", return_value=ImmediateTracker()),
    ):
        result = pipeline.update_workspace_embeddings_desktop_queue(
            "http://anythingllm",
            "key",
            "workspace",
            ["custom/a.json", "custom/b.json"],
            location_sources=[
                {"location": "custom/a.json", "source_path": "C:/sources/a.pdf"},
                {"location": "custom/b.json", "source_path": "C:/sources/b.pdf"},
            ],
            batch_verifier=lambda report: {
                "status": "pass",
                "matching_vector_rows": len(report["locations"]),
            },
        )

    assert [batch["submission_state"] for batch in result["batches"]] == ["rejected", "accepted"]
    assert result["accepted"] == 1
    assert "stopped_after_source_window" not in result
    assert any(error.get("may_continue_later_sources") for error in result["errors"])


@pytest.mark.parametrize(
    "queue",
    [
        {"source_atomic_precommit_rejection": {"error": "pre-write"}, "desktop_queue_current": 1},
        {"source_atomic_precommit_rejection": {"error": "pre-write"}, "desktop_queue_completed": 1},
        {
            "source_atomic_precommit_rejection": {"error": "pre-write"},
            "source_atomic_commit_ambiguity": {"error": "post-write"},
        },
    ],
)
def test_precommit_rejection_is_not_safe_after_any_namespace_write_evidence(queue):
    assert pipeline.source_atomic_precommit_rejection(queue) is None

import threading
from unittest import mock
import pytest
import anythingllm_source_atomic_common as source_atomic
import anythingllm_source_atomic_worker as legacy_helpers
import auto_anythingllm_pipeline as pipeline
pytestmark = pytest.mark.offline_deterministic


def test_neutral_atomic_writer_replaces_complete_bytes_and_cleans_staging(tmp_path):
    target = tmp_path / "nested" / "artifact.json"
    source_atomic._atomic_write(target, b"old")
    source_atomic._atomic_write(target, b"new complete bytes")
    assert target.read_bytes() == b"new complete bytes"
    assert list(target.parent.iterdir()) == [target]


def test_neutral_atomic_writer_failure_preserves_previous_artifact(tmp_path, monkeypatch):
    target = tmp_path / "artifact.json"
    target.write_bytes(b"previous")

    def blocked(*_):
        raise PermissionError("fixture")

    monkeypatch.setattr(source_atomic.os, "replace", blocked)
    with pytest.raises(PermissionError):
        source_atomic._atomic_write(target, b"new")
    assert target.read_bytes() == b"previous"
    assert list(tmp_path.iterdir()) == [target]


def test_neutral_activation_unknown_is_not_a_restart_or_active_claim(tmp_path, monkeypatch):
    executable = tmp_path / "AnythingLLM.exe"
    executable.touch()
    worker = tmp_path / "server.js"
    worker.touch()
    monkeypatch.setattr(source_atomic, "_desktop_root_started_after", lambda *_: (None, "unknown_fixture"))
    assert source_atomic._activation_state_for_installed_worker(
        executable, worker, tmp_path / "missing-manifest.json",
    ) == (False, "unknown_fixture", False)


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


def test_explicit_precommit_rejection_allows_the_next_source_window():
    class FakeThread:
        def is_alive(self):
            return False

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


def test_legacy_module_keeps_helpers_but_has_no_worker_installer():
    assert legacy_helpers._atomic_write is source_atomic._atomic_write
    assert legacy_helpers._sha256_bytes is source_atomic._sha256_bytes
    assert not hasattr(legacy_helpers, "ensure_source_atomic_embedding_worker")
    assert not hasattr(legacy_helpers, "patch_v1161_embedding_worker_source")

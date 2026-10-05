"""Containment checks for superseded paths and their still-active replacements."""

import json
import os
import re
import csv
import threading
import random
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import anythingllm_persistence as persistence
import auto_anythingllm_pipeline as pipeline
import rag_pdf_gradio_app as app
import semantic_segmentation as segmentation
from prepared_recovery import build_prepared_recovery_plan


pytestmark = pytest.mark.offline_deterministic


@pytest.mark.parametrize("separator", ["\n\n", "\r\n\r\n", "\n \t\n ", "\n\n\n"])
def test_paragraph_boundary_retains_its_semantic_strength(separator):
    text = "First paragraph without terminal punctuation" + separator + "Next paragraph."
    position = text.index(separator) + len(separator)
    assert segmentation._candidate_kind(text, position) == ("paragraph", 100)
    assert any(row["kind"] == "paragraph" for row in
               segmentation.boundary_candidates(text, 0, position, len(text), 0.3))


@pytest.mark.parametrize("separator", [" ", "\n", "\r\n"])
def test_soft_wrap_is_not_a_paragraph(separator):
    assert segmentation._candidate_kind("First line" + separator + "next line", 10 + len(separator)) == (
        "whitespace", 5)


@pytest.mark.parametrize("mode", ["page_limit", "page", "page_passages", "passages", "none"])
@pytest.mark.parametrize("ending", ["Complete final sentence.", "unfinished final fragment", ""])
def test_paragraph_scoring_changes_boundaries_without_losing_text_or_offsets(mode, ending):
    text = "\n\n".join(["Scholarly prose with evidence and a reference (Author, 2026). " * 4,
                          "A heading without terminal punctuation", "A second paragraph. " * 5, ending]).strip()
    rows = segmentation.split_semantic_page(text, target=80, hard_limit=120, mode=mode)
    assert re.sub(r"\s+", "", "".join(row["text"] for row in rows)) == re.sub(r"\s+", "", text)
    previous = 0
    for row in rows:
        start, end = row["char_start_page"], row["char_end_page"]
        assert start >= previous and not text[previous:start].strip()
        assert text[start:end].strip() == row["text"]
        assert mode == "none" or len(row["text"]) <= 120
        previous = end
    assert not text[previous:].strip()
    if mode in {"page_limit", "page"}:
        assert segmentation.split_semantic_page(text, 80, len(text) + 1, mode)[0]["text"] == text


@pytest.mark.parametrize("relative", ["resume-embedding-manifest.json",
                                       "source/inspection/resume-embedding-manifest.json",
                                       "queue-groups/resume-embedding-manifest.json"])
@pytest.mark.parametrize("prefix", ["r-", "app-run-"])
def test_nested_manifest_belongs_to_its_current_run(tmp_path, monkeypatch, relative, prefix):
    older, latest = tmp_path / "r-older", tmp_path / (prefix + "latest")
    older.mkdir()
    (older / "resume-embedding-manifest.json").write_text("{}")
    manifest = latest / relative
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("{}")
    os.utime(older / "resume-embedding-manifest.json", (1, 1))
    os.utime(manifest, (2, 2))
    monkeypatch.setattr(app, "AUTO_RUN_STATE_DIR", tmp_path)
    assert app._is_most_recent_recovery_run(latest)
    assert not app._is_most_recent_recovery_run(older)


@pytest.mark.parametrize("absent", [False, True])
def test_restore_updates_or_removes_every_duplicate_assignment(tmp_path, monkeypatch, absent):
    supported = {"status": "supported"}
    monkeypatch.setattr(persistence, "characterize", lambda *_args, **_kwargs: {
        "matched_profile": "fixture", "capabilities": {
            "can_write_env_settings": supported, "can_restore_snapshotted_settings": supported}})
    env = tmp_path / ".env"
    original = "OTHER='unchanged'\n" + ("" if absent else "MODEL='earlier'\nMODEL='original'\n")
    env.write_text(original)
    adapter = persistence.AnythingLLMPersistenceAdapter(tmp_path, "fixture", tmp_path / "snapshots")
    update = adapter.write_env_setting("MODEL", "changed")
    # Simulate an old Desktop/editor adding an additional assignment afterwards.
    with env.open("a") as handle:
        handle.write("MODEL='changed'\n")
    assert adapter.restore(update["snapshot"])["status"] == "restored"
    assert persistence._env_value(env.read_text(), "MODEL") == (None if absent else "original")
    assert "OTHER='unchanged'" in env.read_text()


def test_staged_queue_rejection_is_preserved_not_held_or_replayed(tmp_path):
    (tmp_path / "source-transaction-ledger.json").write_text(json.dumps({
        "transaction_count": 1, "transactions": [{"source_index": 1, "source_sha256": "fixture",
            "planned_records": 2, "state": "source_queue_rejected_without_remote_mutation"}]}))
    plan = build_prepared_recovery_plan(tmp_path)
    assert plan["sources"][0]["action"] == "preserve_rejection_and_continue"
    assert plan["automatic_submission_allowed"] is False


def test_superseded_helpers_are_gone_but_active_replacements_remain():
    for name in ("outline_chapter_map", "outline_chapter_for_page",
                 "merge_short_page_segments", "split_page_under_limit_with_offsets"):
        assert not hasattr(pipeline, name)
    assert callable(pipeline.outline_context_for_page)
    assert callable(pipeline.split_semantic_page)


@pytest.mark.parametrize("event,current,completed,age,state,expected", [
    ("source_staging_provider_batch_waiting", 0, 0, 0, "connected", True),
    ("source_staging_provider_batch_completed", 0, 0, 1, "connected", False),
    ("source_staging_provider_batch_attempt_completed", 0, 0, 1, "reconnecting", True),
    ("chunk_progress", 2, 1, 0, "connected", True),
    ("chunk_progress", 2, 1, 89, "connected", True),
    ("chunk_progress", 2, 1, 90, "connected", False),
    ("chunk_progress", 2, 1, 1, "disconnected", False),
    ("all_complete", 2, 1, 0, "connected", False),
    ("source_commit_ambiguous", 2, 1, 0, "connected", False),
    ("source_rejected_before_commit", 0, 0, 0, "connected", False),
    ("source_staging_provider_batch_attempt_failed", 0, 0, 0, "connected", False),
    ("chunk_progress", 2, 2, 0, "connected", False),
    ("chunk_progress", 2, 1, None, "connected", False),
])
def test_owned_activity_is_not_completion_or_retry_authority(event, current, completed, age, state, expected):
    assert pipeline.owned_reconciliation_activity({
        "queue_records": 2, "desktop_queue_current": current, "desktop_queue_completed": completed,
        "desktop_queue_last_event_type": event, "desktop_queue_last_event_age_seconds": age,
        "desktop_queue_observer_state": state,
    }) is expected


@pytest.mark.parametrize("staging", [False, True])
@pytest.mark.parametrize("fresh", [False, True])
def test_production_group_verifier_observes_active_staging_and_last_record(tmp_path, staging, fresh, monkeypatch):
    text = tmp_path / "prepared.txt"
    text.write_text("Prepared academic text")
    plan = tmp_path / "plan.csv"
    with plan.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["filename", "docSource", "chunkSource", "text_file"])
        writer.writeheader()
        for i in range(2):
            writer.writerow({"filename": f"page-{i}.txt", "docSource": "local-pdf://sha256/fixture",
                             "chunkSource": f"fixture-p{i}", "text_file": str(text)})
    summary = {"pdf": str(tmp_path / "fixture.pdf"), "native_upload_plan": str(plan),
               "source_sha256": "fixture", "native_upload_transport": "file_upload"}
    clock = [1.0]
    queue = {"queue_records": 2, "current": 0 if staging else 2, "completed": 0 if staging else 1,
             "observer_state": "connected", "last_event_monotonic": 1.0,
             "last_event_type": "source_staging_provider_batch_waiting" if staging else "chunk_progress"}
    captured = {}

    def sleep(_):
        if clock[0] < 481:
            clock[0] = 481.0
            queue["last_event_monotonic"] = 481.0 if fresh else 391.0
        else:
            clock[0] = 482.0
            queue.update(completed=2, current=2, last_event_monotonic=482.0, last_event_type="all_complete")

    def verify(*_args, **_kwargs):
        complete = queue["completed"] == 2
        return {"status": "pass" if complete else "incomplete",
                "current_upload_vector_evidence_complete": complete,
                "current_upload_document_vector_count": 2 if complete else 0}

    def upload(*_args, **kwargs):
        captured["result"] = kwargs["batch_verifier"]({
            "start_index": 0, "end_index": 2, "locations": ["custom-documents/a.json", "custom-documents/b.json"],
            "desktop_queue_observer": queue})
        return {"status": "complete" if fresh else "reconciliation_pending", "uploaded": 0, "embedded": 0}

    monkeypatch.setattr(app, "maybe_upload_to_anythingllm", upload)
    monkeypatch.setattr(app, "verify_anythingllm_post_upload", verify)
    monkeypatch.setattr(app, "ensure_source_atomic_embedding_server", lambda *_: {"enabled": False})
    monkeypatch.setattr(app, "default_anythingllm_storage_dir", lambda: tmp_path / "empty-storage")
    monkeypatch.setattr(app.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(app.time, "sleep", sleep)
    app.upload_prepared_automatic_batch([summary], api_url="http://fixture", api_key="",
                                       workspace_slug="fixture", run_root=tmp_path / "run")
    assert captured["result"]["status"] == ("pass" if fresh else "timeout")
    assert clock[0] == (482.0 if fresh else 481.0)


def test_provider_detail_tail_does_not_truncate_totals_or_invent_recovered_events():
    connected = threading.Event()
    connected.set()
    events, timing = [], []
    for source, count in [("a", 260), ("b", 1)]:
        location = f"custom-documents/{source}.json"
        events.append({"type": "source_staging_source_plan", "sourceKey": source,
                       "filename": location, "providerChunkCount": count})
        batches = [{"batchIndex": i, "chunkCount": 1, "elapsed_ms": 1} for i in range(count)]
        events.extend({"type": "source_staging_provider_batch", "sourceKey": source,
                       "filename": location, **batch} for batch in batches)
        # A duplicate terminal event must not recount batches lost from the tail.
        terminal = {"type": "source_staging_finished", "sourceKey": source,
                    "filename": location, "providerBatches": batches}
        events.extend([terminal, terminal])

    def listener(*_args, **kwargs):
        for event in events:
            kwargs["observer_callback"](event)
        return {"connected_event": connected, "stop_event": threading.Event(), "events": events,
                "errors": [], "thread": SimpleNamespace(join=lambda **_: None, is_alive=lambda: False)}

    with (patch.object(pipeline, "start_anythingllm_embed_progress_listener", listener),
          patch.object(pipeline, "_anythingllm_vector_cache_hit", return_value=False),
          patch.object(pipeline, "update_workspace_embeddings_batched", return_value={"runtime_events": [], "batches": []})):
        report = pipeline.update_workspace_embeddings_desktop_queue(
            "http://fixture", "", "fixture", ["custom-documents/a.json", "custom-documents/b.json"],
            _source_window_execution=True, status_callback=lambda _message, detail: timing.append(detail))
    snapshot = report["progress_observation"]["final_queue_snapshot"]
    assert snapshot["source_atomic_provider_batch_count"] == 261
    assert snapshot["source_atomic_provider_chunk_count"] == 261
    assert snapshot["source_atomic_provider_elapsed_ms"] == 261
    assert snapshot["source_atomic_provider_telemetry_complete"] is True
    assert len(snapshot["source_atomic_provider_batches"]) == 256
    assert not any(row.get("source_atomic_timing_recovered_from_terminal") for row in timing)


def test_bounded_sentence_matcher_preserves_the_original_span_contract():
    pattern = r"(?s)(\S.*?[.!?][\"'\u201d\u2019)]?)(?=\s+|$)"
    randomizer = random.Random(20261005)
    for _ in range(2000):
        text = "".join(randomizer.choice(["word ", "\n", "\n\n", ". ", ".x", "!", "? ", ")", '"', "\u201d"])
                       for _ in range(randomizer.randint(0, 35)))
        old = list(re.finditer(pattern, text))
        expected = (old[-1].start(1), old[-1].end(1)) if old else None
        assert segmentation._last_complete_sentence_span(text) == expected


def test_sentence_matcher_never_backtracks_through_a_long_incomplete_tail():
    text = "A complete sentence. " + "unfinished prose " * 10_000
    finditer = re.finditer
    with patch.object(segmentation.re, "finditer", wraps=finditer) as calls:
        assert segmentation._last_complete_sentence_span(text) == (0, 20)
    assert len(calls.call_args_list[-1].args[1]) == 20
    with patch.object(segmentation.re, "finditer", wraps=finditer) as calls:
        assert segmentation._last_complete_sentence_span("unfinished prose " * 10_000) is None
    assert calls.call_count == 1


@pytest.mark.parametrize("automatic", [False, True])
def test_automatic_recovery_policy_cannot_cancel_or_resume_a_queue(tmp_path, monkeypatch, automatic):
    group = {"ledger_path": str(tmp_path / "batch-embedding-ledger.json"), "workspace_slug": "fixture",
             "locations": ["custom-documents/a.json"], "api_url": "http://fixture"}
    monkeypatch.setattr(app, "_is_most_recent_recovery_run", lambda *_: True)
    monkeypatch.setattr(app, "_recovery_ledger_groups", lambda *_: [group])
    monkeypatch.setattr(app, "resolve_anythingllm_api_key", lambda *_: ("fixture", "provided"))
    monkeypatch.setattr(app, "observe_workspace_embedding_queue_activity", lambda *_, **__: {
        "status": "owned_activity_observed"})

    def forbidden(*_, **__):
        pytest.fail("Automatic observation must not mutate the queue or runtime")

    monkeypatch.setattr(app, "remove_confirmed_workspace_queue_entries", forbidden)
    monkeypatch.setattr(app, "restart_anythingllm_desktop", forbidden)
    monkeypatch.setattr(app, "submit_embedding_resume_manifest", forbidden)
    result = app.recover_automatic_run(tmp_path, policy="automatic_recover", automatic=automatic)
    assert result["status"] == "complete"
    assert result["groups"][0]["action"] == "none"


def test_unknown_recovery_policy_is_rejected_before_contacting_anythingllm(tmp_path, monkeypatch):
    def forbidden(*_, **__):
        pytest.fail("Unknown policy must be rejected before any recovery work")

    monkeypatch.setattr(app, "_recovery_ledger_groups", forbidden)
    assert app.recover_automatic_run(tmp_path, policy="unknown-policy")["status"] == "unsupported_policy"

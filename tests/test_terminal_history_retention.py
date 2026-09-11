import json

import pytest
import rag_pdf_gradio_app as app

pytestmark = pytest.mark.offline_deterministic


@pytest.fixture
def history_home(tmp_path, monkeypatch):
    for name, filename in {
        "INGESTION_HISTORY_PATH": "ingestion.jsonl",
        "TIMING_MODEL_RUNS_PATH": "runs.jsonl",
        "TIMING_MODEL_EVENTS_PATH": "events.jsonl",
        "TIMING_MODEL_SUMMARY_PATH": "summary.json",
    }.items():
        monkeypatch.setattr(app, name, tmp_path / filename)
    monkeypatch.setattr(app, "TIMING_MODEL_DIR", tmp_path)
    monkeypatch.setattr(app, "hydrated_timing_model_history", lambda: [])
    return tmp_path


def read(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_append_is_durable_and_idempotent_with_legacy_keys(tmp_path, monkeypatch):
    history = tmp_path / "history.jsonl"
    history.write_text(json.dumps({"run_root": "legacy"}) + "\n")
    syncs = []
    monkeypatch.setattr(app.os, "fsync", lambda fd: syncs.append(fd))
    for _ in range(2):
        app.append_private_history_records(history, [{"run_key": "legacy"}, {"run_key": "new"}], ("run_key",))
    assert len(read(history)) == 2
    assert len(syncs) == 2


def test_phase_repair_links_to_the_terminal_run_without_duplicate_weight(history_home):
    run = history_home / "run"
    run.mkdir()
    event = {"run_key": str(run), "recorded_at": "2026-09-11T01:00:00", "event": "phase_completed",
             "stage": "native_extract", "phase_elapsed_seconds": 3.0}
    (run / "timing-evidence-timeline.jsonl").write_text(json.dumps(event) + "\n")
    assert app.persist_terminal_phase_history(run)
    assert app.persist_terminal_phase_history(run)
    assert len(read(app.TIMING_MODEL_EVENTS_PATH)) == 1
    assert read(app.TIMING_MODEL_EVENTS_PATH)[0]["run_key"] == str(run)


@pytest.mark.parametrize("failure", ["write", "corrupt_timeline"])
def test_phase_retention_reports_failure(history_home, monkeypatch, failure):
    run = history_home / "run"
    run.mkdir()
    if failure == "corrupt_timeline":
        (run / "timing-evidence-timeline.jsonl").write_text('{"broken')
    else:
        monkeypatch.setattr(app, "append_private_history_records", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    assert not app.persist_terminal_phase_history(run)
    assert run.exists()


def test_flat_history_points_to_exports_but_joins_timing_by_original_key(history_home):
    run = history_home / "staging"
    target = history_home / "exports"
    row = app.append_ingestion_history(run, [], {"state": "successful"}, False, "",
        export_root=target, timing_row={"actual_seconds": 12})
    assert row["run_root"] == str(target)
    assert row["run_key"] == str(run)
    assert row["timing"]["actual_seconds"] == 12
    assert read(app.INGESTION_HISTORY_PATH) == [row]


@pytest.mark.parametrize("state", ["cancelled", "failed"])
def test_early_terminal_is_recorded_once_but_never_calibrates(history_home, state):
    run = history_home / "run"
    run.mkdir()
    for _ in range(2):
        app.retain_early_terminal_history(run, state, "Stopped", 2, 10)
    timing = app._read_timing_jsonl(app.TIMING_MODEL_RUNS_PATH)
    assert len(timing) == len(read(app.INGESTION_HISTORY_PATH)) == 1
    assert timing[0]["state"] == state
    assert timing[0]["actual_seconds"] == 2
    assert not app.timing_model_learning_observation_usable(timing[0])


def test_unowned_preview_does_not_create_a_history_run(history_home):
    app.retain_early_terminal_history(None, "failed", "No run started", 2, 10)
    assert not app.INGESTION_HISTORY_PATH.exists()


def test_terminal_write_failure_is_not_reported_as_persisted(history_home, monkeypatch):
    monkeypatch.setattr(app, "append_private_history_records", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    assert not app.append_ingestion_history(history_home / "run", [], {"state": "successful"}, False, "")
    assert not app.record_timing_model_run(history_home / "run", [], {"state": "successful"}, {}, 1)
    assert (history_home / "run" / "ingestion-terminal-record.json").exists()


def test_invalid_existing_history_preserves_terminal_evidence(history_home):
    app.INGESTION_HISTORY_PATH.write_bytes(b"[]\n")
    assert app.append_ingestion_history(history_home / "run", [], {"state": "successful"}, False, "")
    assert next(history_home.glob("ingestion.jsonl.damaged-*")).read_bytes() == b"[]\n"
    assert len(read(app.INGESTION_HISTORY_PATH)) == 1
    assert (history_home / "run" / "ingestion-terminal-record.json").is_file()


def test_duplicate_at_end_keeps_canonical_observed_backend(history_home):
    row = app.record_timing_model_run(history_home / "run", [
        {"selected_backend": "pymupdf", "api_upload_status": "skipped_prepare_only"},
        {"api_upload_status": "skipped_exact_duplicate"},
    ], {"state": "successful"}, {"timing_estimate": {"features": {"mode": app.MODE_LOCAL_ONLY_LABEL}}}, 10)
    assert row["selected_backend"] == "pymupdf"
    saved = app._read_timing_jsonl(app.TIMING_MODEL_RUNS_PATH)[0]
    assert saved["selected_input_duplicate_documents"] == 1
    assert saved["document_timing"][1]["selected_input_duplicate"]


@pytest.mark.parametrize("reverse", [False, True])
def test_batch_ocr_and_backend_are_order_independent(history_home, reverse):
    docs = [{"selected_backend":"unstructured", "ocr_assisted_extraction_used":True, "pdf_page_count":10},
            {"selected_backend":"pymupdf", "ocr_assisted_extraction_used":False, "pdf_page_count":20}]
    row = app.record_timing_model_run(history_home / "run", docs[::-1] if reverse else docs,
        {"state":"successful"}, {}, 30)
    assert row["ocr_used"] is True
    assert row["selected_backend"] == "mixed"
    assert row["observed_backends"] == ["pymupdf", "unstructured"]
    assert row["unique_processed_pages"] == 30


@pytest.mark.parametrize("legacy", [False, True])
def test_selected_duplicates_cannot_teach_whole_run_but_batches_remain_usable(legacy, monkeypatch):
    monkeypatch.setattr(app, "timing_model_formula_compatible", lambda r: True)
    row = {"source":"automatic-run", "state":"successful", "run_key":"run", "page_count":40,
           "actual_seconds":20, "duration_provenance":"wall_clock" if legacy else "active_observation_window",
           "actual_batches":1, "batch_seconds":[10]}
    assert app.timing_model_learning_observation_usable(row)
    row["selected_input_duplicate_documents"] = 1
    assert not app.timing_model_learning_observation_usable(row)
    assert app.timing_model_batch_observation_usable(row)
    row.pop("selected_input_duplicate_documents")
    row["document_timing"] = [{"selected_input_duplicate":True}]
    assert not app.timing_model_learning_observation_usable(row)


def test_wall_clock_fallback_is_stored_as_lower_confidence(history_home):
    row = app.record_timing_model_run(history_home / "run", [], {"state":"successful"},
        {"duration_provenance":"wall_clock"}, 17)
    assert row["duration_provenance"] == "wall_clock"
    assert not app.timing_model_observation_usable(row)
    import inspect
    body = inspect.getsource(app.run_automatic)
    assert 'duration_provenance = "wall_clock"' in body
    assert '"duration_provenance": duration_provenance' in body


@pytest.mark.parametrize("damage", [b'{"run_key":', b'[]\n', b'\xff\xfe\n'])
def test_repair_keeps_good_rows_and_exact_damaged_bytes(history_home, damage):
    path = app.INGESTION_HISTORY_PATH
    raw = b'{"run_key":"before"}\n' + damage + b'\n{"run_key":"after"}\n'
    path.write_bytes(raw)
    for _ in range(2):
        app.append_private_history_records(path, [{"run_key":"new"}], ("run_key",))
    assert [r["run_key"] for r in read(path)] == ["before", "after", "new"]
    backups = list(history_home.glob("ingestion.jsonl.damaged-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == raw


def test_missing_final_newline_does_not_fuse_records(history_home):
    path = app.INGESTION_HISTORY_PATH
    path.write_bytes(b'{"run_key":"before"}')
    app.append_private_history_records(path, [{"run_key":"after"}], ("run_key",))
    assert len(read(path)) == 2
    assert not list(history_home.glob("*.damaged-*"))


def test_failed_backup_never_replaces_damaged_history(history_home, monkeypatch):
    path = app.INGESTION_HISTORY_PATH
    raw = b'{"torn'
    path.write_bytes(raw)
    monkeypatch.setattr(app.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        app.append_private_history_records(path, [{"run_key":"new"}], ("run_key",))
    assert path.read_bytes() == raw


def test_truncated_old_backup_does_not_prevent_recovery(history_home):
    path = app.INGESTION_HISTORY_PATH
    raw = b'{"torn'
    path.write_bytes(raw)
    backup = path.with_name(path.name + ".damaged-" + app.hashlib.sha256(raw).hexdigest()[:16])
    backup.write_bytes(b"partial")
    app.append_private_history_records(path, [{"run_key":"new"}], ("run_key",))
    assert read(path) == [{"run_key":"new"}]
    assert backup.read_bytes() == b"partial"
    assert any(p.read_bytes() == raw for p in history_home.glob("*.damaged-*"))


def test_locked_cleanup_is_reported_and_exports_are_untouched(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    staging.mkdir()
    export = tmp_path / "final.txt"
    export.write_text("good output")
    monkeypatch.setattr(app.shutil, "rmtree", lambda p: (_ for _ in ()).throw(PermissionError("locked")))
    message = app.cleanup_flat_local_staging(staging)
    assert "PermissionError" in message and "TXT files are ready" in message
    assert staging.exists() and export.read_text() == "good output"


def test_successful_cleanup_and_already_removed_folder(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    assert app.cleanup_flat_local_staging(staging) == ""
    assert app.cleanup_flat_local_staging(staging) == ""

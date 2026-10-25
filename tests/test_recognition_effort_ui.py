"""Persistent effort, selection ownership and actual worker/UI boundaries."""
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import Mock

import fitz
import pytest

import rag_pdf_gradio_app as app
from automatic_worker_protocol import serializable_automatic_worker_arguments
from recognition_effort import load_recognition_effort

pytestmark = pytest.mark.offline_deterministic


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("ANYTHINGLLM_PDF_ASSISTANT_HOME", str(tmp_path / "profile"))
    monkeypatch.setattr(app, "LIVE_AUTOMATIC_RUN_STATUS", {})
    monkeypatch.setattr(app, "SELECTION_PREVIEW_OWNERS", OrderedDict())


def test_mode_input_persists_immediately_and_a_new_browser_load_reads_latest_choice():
    assert app.load_effort_control()[0]["value"] == 0
    app.persist_and_begin_effort_preview(1)
    assert app.load_effort_control()[0]["value"] == 1
    assert load_recognition_effort().effort.value == "extremely_thorough"


def test_mode_change_invalidates_old_preview_and_preserves_explicit_metadata_edit_flags(tmp_path):
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfixture")
    request = SimpleNamespace(session_hash="effort-browser")
    old = app.automatic_selection_begin_state({}, "", [str(pdf)], [], {}, request=request)[0]
    old = app.mark_metadata_field_edited("document_author", "Chosen name", old)
    changed = app.persist_and_begin_effort_preview(0, old, "", [str(pdf)], [], {}, request=request)
    state = changed[2]
    assert state["metadata_user_edits"]["document_author"]
    assert state["recognition_effort"] == "thorough"
    assert not app._selection_preview_is_current(old, request)
    assert app._selection_preview_is_current(state, request)
    assert 'data-state="scanning"' in changed[-1]
    second = app.persist_and_begin_effort_preview(1, state, "", [str(pdf)], [], {}, request=request)
    assert second[2]["preview_request_identity"] != state["preview_request_identity"]


def test_a_running_job_keeps_its_mode_while_new_choice_is_saved_for_next_run(monkeypatch):
    monkeypatch.setattr(app, "LIVE_AUTOMATIC_RUN_STATUS", {"state": "running", "run_root": "owned-run"})
    state = {"state": "running", "recognition_effort": "thorough"}
    output = app.persist_and_begin_effort_preview(1, state)
    assert output[2] == state
    assert "next run" in output[1]["value"]
    assert app.load_effort_control()[0]["value"] == 1


def test_preference_write_failure_is_visible_and_does_not_clear_the_current_choice(monkeypatch):
    monkeypatch.setattr(app, "save_recognition_effort", Mock(side_effect=PermissionError("Preference file busy")))
    output = app.persist_and_begin_effort_preview(1)
    assert output[0]["value"] == 1
    assert output[1]["visible"] and "could not be saved" in output[1]["value"]


def test_effort_is_part_of_run_identity_and_the_durable_worker_allowlist():
    assert app.automatic_run_processing_settings({"recognition_effort": 0}) != app.automatic_run_processing_settings({"recognition_effort": 1})
    encoded, _fingerprint = serializable_automatic_worker_arguments(SimpleNamespace(recognition_effort="thorough"))
    assert encoded["recognition_effort"] == "thorough"


def test_workspace_identity_is_fresh_across_requests_for_the_same_file(tmp_path, monkeypatch):
    path = tmp_path / "source.pdf"
    path.write_bytes(b"%PDF-1.4\nfixture")
    reader = Mock(return_value={})
    person = Mock(side_effect=[{"label": "First", "kind": "person"}, {"label": "Second", "kind": "person"}])
    monkeypatch.setattr(app, "pdf_metadata", reader)
    monkeypatch.setattr(app, "workspace_person_identity_from_pdf", person)
    assert app.workspace_source_identity_from_pdf(path)["label"] == "First"
    assert app.workspace_source_identity_from_pdf(path)["label"] == "Second"
    assert reader.call_count == person.call_count == 2


def test_preview_uses_its_own_just_acquired_author_report_without_a_second_author_scan(tmp_path, monkeypatch):
    path = tmp_path / "source.pdf"
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_text((72, 100), "An independent source\nAlice Harrington\nDocument body text.")
        pdf.save(path)
    inference = Mock(return_value={"author": "Alice Harrington", "source": "text_strict_credit_block",
        "evidence": "Visible local byline", "author_confidence": {"level": "high", "score": 92}})
    monkeypatch.setattr(app, "infer_author_from_initial_pdf_pages", inference)
    output = app.detected_metadata_preview([str(path)], recognition_effort="thorough")
    assert "Alice Harrington" in output[3] and "Harrington" in output[3]
    inference.assert_called_once_with(path, page_limit=4, use_file_title_fallback=True, recognition_effort="thorough")


def test_every_preview_stream_receives_the_effort_control_and_mode_input_preserves_override_transport():
    config = app.demo.config
    components = {c.get("props", {}).get("elem_id"): c["id"] for c in config["components"]}
    slider = components["author-recognition-effort"]
    streams = [d for d in config["dependencies"] if "stream_automatic_detected_metadata_preview" in str(d.get("api_name", ""))]
    assert len(streams) >= 6 and all(slider in d["inputs"] for d in streams)
    change = next(d for d in config["dependencies"] if (slider, "input") in d.get("targets", []))
    assert components["automatic-document-overrides-transport"] not in change["outputs"]
    assert change["queue"] is False

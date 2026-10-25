"""Persistent effort, selection ownership and actual worker/UI boundaries."""
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import Mock

import fitz
import pytest

import rag_pdf_gradio_app as app
from automatic_worker_protocol import serializable_automatic_worker_arguments
from recognition_effort import save_recognition_effort
from run_request import RunRequest

pytestmark = pytest.mark.offline_deterministic


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("ANYTHINGLLM_PDF_ASSISTANT_HOME", str(tmp_path / "profile"))
    monkeypatch.setattr(app, "LIVE_AUTOMATIC_RUN_STATUS", {})
    monkeypatch.setattr(app, "SELECTION_PREVIEW_OWNERS", OrderedDict())


def test_retired_saved_preference_does_not_change_production_defaults():
    save_recognition_effort("extremely_thorough")
    effective = app.fresh_automatic_run_setting_values()
    assert effective["recognition_effort"] == "thorough"


@pytest.mark.parametrize("value", [None, "", 0, 1, "thorough", "extremely_thorough"])
def test_historical_requests_and_worker_intent_use_the_single_production_policy(value):
    assert app.automatic_run_processing_settings({"recognition_effort": value}) == app.automatic_run_processing_settings({"recognition_effort": "thorough"})
    request = RunRequest(input_paths=("sample.pdf",), recognition_effort=value)
    encoded, _fingerprint = serializable_automatic_worker_arguments(SimpleNamespace(recognition_effort=request.to_legacy_namespace().recognition_effort))
    assert encoded["recognition_effort"] == "thorough"


def test_retired_effort_does_not_create_a_second_run_identity():
    assert app.automatic_run_processing_settings({"recognition_effort": 0}) == app.automatic_run_processing_settings({"recognition_effort": 1})


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


def test_visible_effort_controls_are_absent_and_preview_callbacks_use_hidden_thorough_state():
    config = app.demo.config
    assert not any(c.get("props", {}).get("elem_id") == "author-recognition-effort" for c in config["components"])
    assert "recognition-effort-endpoints" not in str(config)
    states = [c["id"] for c in config["components"] if c["type"] == "state" and app.demo.blocks[c["id"]].value == "thorough"]
    assert len(states) == 1
    streams = [d for d in config["dependencies"] if "stream_automatic_detected_metadata_preview" in str(d.get("api_name", ""))]
    assert len(streams) >= 5 and all(states[0] in d["inputs"] for d in streams)
    assert not any(d.get("api_name") in {"load_effort_control", "persist_and_begin_effort_preview"} for d in config["dependencies"])

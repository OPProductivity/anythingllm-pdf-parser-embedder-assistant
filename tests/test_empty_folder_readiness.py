"""An unselected discovery set must not masquerade as a ready selection."""
from types import SimpleNamespace

import pytest

import rag_pdf_gradio_app as app

pytestmark = pytest.mark.offline_deterministic


@pytest.fixture(autouse=True)
def idle_app(monkeypatch):
    monkeypatch.setattr(app, "LIVE_AUTOMATIC_RUN_STATUS", {})
    monkeypatch.setattr(app, "SELECTION_PREVIEW_OWNERS", app.OrderedDict())


def test_explicit_empty_subset_is_not_the_full_folder_signature():
    manifest = {"pdf_candidates": ["C:/discovered.pdf"], "selected_pdf_candidates": []}
    assert app.automatic_selection_signature([], [], manifest) == ""
    assert app.automatic_selection_signature([], [], {"pdf_candidates": ["C:/discovered.pdf"]})


@pytest.mark.parametrize("has_discovery", [False, True])
def test_removing_every_selected_file_finishes_idle(has_discovery):
    manifest = {"pdf_candidates": ["C:/discovered.pdf"] if has_discovery else [],
                "selected_pdf_candidates": []}
    request = SimpleNamespace(session_hash="empty-subset")
    state = app.automatic_selection_begin_state({}, "", [], [], manifest, request=request)[0]
    finished = app.automatic_selection_finish_state(state, [], [], manifest, request=request)
    assert finished[0]["state"] == "idle"
    assert finished[2]["interactive"] is False
    assert 'data-run-state="idle"' in finished[4]["value"]
    assert "No PDFs selected" in finished[4]["value"]
    assert "Confirm to begin" not in finished[4]["value"]


def test_empty_action_row_explains_why_confirm_is_disabled():
    result = app.automatic_selection_action_states([], [], {}, {"state": "idle"})
    assert result[0]["interactive"] is False
    assert "No PDFs selected" in result[2]["value"]


def test_empty_reset_does_not_flash_a_ready_instruction():
    result = app.reset_automatic_run_presentation([], [], {"state": "pending"})
    assert "No PDFs selected" in result[0]["value"]


def test_initial_browser_view_explains_the_empty_selection():
    activity = [c["props"].get("value", "") for c in app.demo.config["components"]
                if c["type"] == "html" and 'data-run-state="idle"' in c["props"].get("value", "")]
    assert any("No PDFs selected" in value for value in activity)


def test_ordinary_file_still_finishes_ready_with_an_empty_folder_subset():
    manifest = {"pdf_candidates": ["C:/discovered.pdf"], "selected_pdf_candidates": []}
    request = SimpleNamespace(session_hash="ordinary-file")
    state = app.automatic_selection_begin_state({}, "", ["C:/selected.pdf"], [], manifest, request=request)[0]
    finished = app.automatic_selection_finish_state(state, ["C:/selected.pdf"], [], manifest, request=request)
    assert finished[0]["state"] == "ready"
    assert finished[2]["interactive"] is True
    assert 'data-run-state="ready"' in finished[4]["value"]

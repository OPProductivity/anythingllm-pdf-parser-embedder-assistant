"""Selection feedback/ownership controls; author inference is not mocked accuracy proof."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import rag_pdf_gradio_app as app

pytestmark = pytest.mark.offline_deterministic


@pytest.fixture(autouse=True)
def idle_selection(monkeypatch):
    monkeypatch.setattr(app, "LIVE_AUTOMATIC_RUN_STATUS", {})
    monkeypatch.setattr(app, "SELECTION_PREVIEW_OWNERS", app.OrderedDict())
    monkeypatch.setattr(app, "folder_manifest_candidates", lambda files, manifest: (files or [], True))


def result(path, *_args):
    return (
        app.gr.update(value="detected title"),
        app.gr.update(value="detected author"),
        app.gr.update(value="detected label"),
        f"<section>Complete: {path}</section>",
        app.gr.update(open=True),
    )


def test_scanning_is_yielded_before_inference_and_each_result_before_next_file(monkeypatch):
    inspect = Mock(side_effect=result)
    monkeypatch.setattr(app, "detected_metadata_preview", inspect)
    stream = app.stream_automatic_detected_metadata_preview(["first.pdf", "second.pdf"], [])
    first = next(stream)
    assert "recognition scanning" in first[3]
    assert "first.pdf" in first[5]["value"]
    inspect.assert_not_called()
    completed_first = next(stream)
    assert "Complete: ['first.pdf']" in completed_first[3]
    assert inspect.call_count == 1
    scanning_second = next(stream)
    assert "second.pdf" in scanning_second[5]["value"]
    assert "Complete: ['first.pdf']" in scanning_second[3]
    assert inspect.call_count == 1
    completed_second = next(stream)
    assert "Complete: ['second.pdf']" in completed_second[3]
    assert inspect.call_count == 2
    assert all(update["value"] == "" for update in completed_second[:3])
    assert list(stream) == []


def test_preview_limit_is_explicit_and_no_document_is_inspected_twice(monkeypatch):
    inspect = Mock(side_effect=result)
    monkeypatch.setattr(app, "detected_metadata_preview", inspect)
    files = [f"source-{index}.pdf" for index in range(11)]
    events = list(app.stream_automatic_detected_metadata_preview(files, []))
    assert "11 PDF(s) selected. Previewing 5." in events[-1][3]
    assert "Every selected PDF will be inspected freshly" in events[-1][3]
    assert [call.args[0][0] for call in inspect.call_args_list] == files[:5]


def test_folder_names_are_visible_before_the_all_page_inspection_starts(monkeypatch, tmp_path):
    pdf = tmp_path / "selected.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    path = str(pdf)
    monkeypatch.setattr(app, "iter_recursive_pdf_folder_scan", lambda _root: iter([
        ("complete", {"root": str(tmp_path), "pdf_paths": [path]}),
    ]))
    monkeypatch.setattr(app, "iter_uploaded_pdf_candidate_inspection", lambda _paths: iter([
        ("complete", {"pdf_candidates": [path]}),
    ]))
    inspect = Mock(return_value=iter([{
        "completed": 1, "total": 1, "pages": 12, "likely_ocr_pages": 0,
        "details": {path: {"pages": 12, "ocr_label": "0"}},
        "coverage_by_key": {},
    }]))
    monkeypatch.setattr(app, "iter_pdf_picker_page_details", inspect)
    monkeypatch.setattr(app, "batch_folder_selected_status_html", lambda *_args: "Selected")
    monkeypatch.setattr(app, "retain_batch_native_inspection", lambda _coverage: None)
    stream = app.stream_selected_pdf_directory(str(tmp_path), True)
    initial = next(stream)
    inspect.assert_not_called()
    assert initial[0] == [path]
    assert initial[2]["visible"] and not initial[2]["interactive"]
    assert "selected.pdf" in initial[2]["choices"][0][0]
    assert "pages checking" in initial[2]["choices"][0][0]
    final = list(stream)[-1]
    assert final[2]["interactive"]
    assert "12 pages" in final[2]["choices"][0][0]


def test_one_failed_preview_does_not_strand_selection_or_hide_other_results(monkeypatch):
    inspect = Mock(side_effect=[RuntimeError("bad PDF view"), result(["second.pdf"])])
    monkeypatch.setattr(app, "detected_metadata_preview", inspect)
    events = list(app.stream_automatic_detected_metadata_preview(["first.pdf", "second.pdf"], []))
    assert "Metadata preview failed" in events[-1][3]
    assert "Complete: ['second.pdf']" in events[-1][3]
    assert len(events[-1]) == 6


def test_later_selection_prevents_old_result_and_readiness_publication(monkeypatch):
    request = SimpleNamespace(session_hash="same-browser")
    state = app.automatic_selection_begin_state({}, "", ["first.pdf"], [], {}, request=request)[0]

    def supersede(*args):
        app.automatic_selection_begin_state(state, "", ["new.pdf"], [], {}, request=request)
        return result(*args)

    monkeypatch.setattr(app, "detected_metadata_preview", supersede)
    stream = app.stream_automatic_detected_metadata_preview(
        ["first.pdf"], [], selection_state=state, request=request,
    )
    assert "recognition scanning" in next(stream)[3]
    assert all(update == app.gr.update() for update in next(stream))
    assert list(stream) == []
    assert all(update == app.gr.update() for update in app.automatic_selection_finish_state(
        state, ["first.pdf"], [], {}, request=request,
    ))


def test_browser_sessions_do_not_cancel_each_others_previews(monkeypatch):
    first = SimpleNamespace(session_hash="first-browser")
    second = SimpleNamespace(session_hash="second-browser")
    state = app.automatic_selection_begin_state({}, "", ["first.pdf"], [], {}, request=first)[0]
    app.automatic_selection_begin_state({}, "", ["second.pdf"], [], {}, request=second)
    monkeypatch.setattr(app, "detected_metadata_preview", result)
    events = list(app.stream_automatic_detected_metadata_preview(
        ["first.pdf"], [], selection_state=state, request=first,
    ))
    assert events[-1][1]["value"] == "detected author"


def test_workspace_identity_work_is_announced_before_it_begins(monkeypatch):
    monkeypatch.setattr(app, "is_new_document_workspace_choice", lambda _value: True)
    inspect = Mock(return_value=(app.gr.update(placeholder="New name"), {"suggestion": "New name"}))
    monkeypatch.setattr(app, "update_new_workspace_name_control", inspect)
    stream = app.stream_automatic_workspace_name_preview("new", "", ["first.pdf"])
    initial = next(stream)
    inspect.assert_not_called()
    assert "additional author and source-identity checks" in initial[2]["value"]
    assert "confirmed processing has not started" in initial[2]["value"]
    assert next(stream)[0]["placeholder"] == "New name"
    assert list(stream) == []


def test_estimate_work_is_announced_before_it_begins(monkeypatch):
    calculate = Mock(return_value='An estimate')
    monkeypatch.setattr(app, 'refresh_automatic_run_estimate_for_fresh_selection', calculate)
    stream = app.stream_refresh_automatic_run_estimate_for_fresh_selection(['first.pdf'])
    first = next(stream)
    calculate.assert_not_called()
    assert 'Calculating the processing estimate' in first[1]['value']
    assert 'Confirmed processing has not started' in first[1]['value']
    assert next(stream)[0] == 'An estimate'
    assert list(stream) == []


def test_obsolete_estimate_cannot_replace_a_newer_selection(monkeypatch):
    request = SimpleNamespace(session_hash='same-browser')
    state = app.automatic_selection_begin_state({}, '', ['first.pdf'], [], {}, request=request)[0]

    def supersede(*_args):
        app.automatic_selection_begin_state(state, '', ['new.pdf'], [], {}, request=request)
        return 'Obsolete estimate'

    monkeypatch.setattr(app, 'refresh_automatic_run_estimate_for_fresh_selection', supersede)
    stream = app.stream_refresh_automatic_run_estimate_for_fresh_selection(
        ['first.pdf'], selection_state=state, request=request,
    )
    next(stream)
    assert all(update == app.gr.update() for update in next(stream))
    assert list(stream) == []


def test_obsolete_workspace_suggestion_cannot_replace_a_newer_selection(monkeypatch):
    request = SimpleNamespace(session_hash="same-browser")
    state = app.automatic_selection_begin_state({}, "", ["first.pdf"], [], {}, request=request)[0]

    def supersede(*_args):
        app.automatic_selection_begin_state(state, "", ["new.pdf"], [], {}, request=request)
        return app.gr.update(placeholder="Obsolete name"), {"suggestion": "Obsolete name"}

    monkeypatch.setattr(app, "update_new_workspace_name_control", supersede)
    stream = app.stream_automatic_workspace_name_preview(
        "new", "", ["first.pdf"], selection_state=state, request=request,
    )
    next(stream)
    assert all(update == app.gr.update() for update in next(stream))
    assert list(stream) == []


def test_direct_and_folder_selection_chains_use_the_same_stream():
    config = app.demo.config
    pdf_component = next(c for c in config["components"]
                         if c.get("props", {}).get("elem_id") == "automatic-pdf-upload")
    direct = next(d for d in config["dependencies"]
                  if (pdf_component["id"], "change") in d.get("targets", []))
    names = []
    while direct:
        names.append(str(direct.get("api_name") or ""))
        children = [d for d in config["dependencies"] if d.get("trigger_after") == direct["id"]]
        assert len(children) <= 1
        direct = children[0] if children else None
    assert names.index(next(n for n in names if "reset_automatic_run_selection_settings" in n)) < names.index(
        next(n for n in names if "stream_automatic_detected_metadata_preview" in n)
    )
    assert names.index(next(n for n in names if 'stream_automatic_detected_metadata_preview' in n)) < names.index(
        next(n for n in names if 'stream_refresh_automatic_run_estimate_for_fresh_selection' in n)
    )
    streams = [d for d in config["dependencies"]
               if "stream_automatic_detected_metadata_preview" in str(d.get("api_name") or "")]
    assert len(streams) >= 5
    assert all(d["queue"] and len(d["outputs"]) == 6 for d in streams)


def test_preview_authors_cannot_bypass_fresh_role_reconciliation():
    values = [None] * len(app.AUTOMATIC_RUN_FIELDS)
    title = app.AUTOMATIC_RUN_FIELDS.index("document_label")
    author = app.AUTOMATIC_RUN_FIELDS.index("document_author")
    short = app.AUTOMATIC_RUN_FIELDS.index("document_short_label")
    shared = app.AUTOMATIC_RUN_FIELDS.index("apply_batch_metadata_overrides")
    values[title], values[author], values[short] = "Visible work", "Preview Person", "Person"
    state = {"metadata_user_edits": {}}
    fresh = app.automatic_explicit_metadata_values(state, values)
    assert fresh[title] == "Visible work"
    assert fresh[author] == fresh[short] == ""
    edited = app.mark_metadata_field_edited("document_author", "Chosen Person", state)
    values[author] = "Chosen Person"
    assert app.automatic_explicit_metadata_values(edited, values)[author] == "Chosen Person"
    assert app.automatic_explicit_metadata_values(edited, values)[short] == ""
    values[shared] = True
    assert app.automatic_explicit_metadata_values(state, values) == tuple(values)
    assert app.automatic_explicit_metadata_values({}, values) == values


def test_preview_option_change_preserves_intent_but_new_source_resets_it():
    state = app.automatic_selection_begin_state({}, "", ["first.pdf"], [], {})[0]
    state = app.mark_metadata_field_edited("document_author", "Chosen Person", state)
    changed_option = app.automatic_metadata_option_begin_state(state, "", ["first.pdf"], [], {})[0]
    assert changed_option["metadata_user_edits"]["document_author"]
    new_source = app.automatic_selection_begin_state(state, "", ["next.pdf"], [], {})[0]
    assert new_source["metadata_user_edits"] == {}


def test_empty_selection_clears_and_unlocks_inferred_fields(monkeypatch):
    monkeypatch.setattr(app, "detected_metadata_preview", lambda *_a: (*result([])[:3], "Choose PDF", app.gr.update(open=False)))
    final = list(app.stream_automatic_detected_metadata_preview([], [], "old title", "old author", "old label"))[-1]
    assert all(update["value"] == "" and update["interactive"] for update in final[:3])


def test_old_inferred_fields_do_not_mask_a_new_preview_but_manual_edits_survive(monkeypatch):
    inspect = Mock(side_effect=result)
    monkeypatch.setattr(app, "detected_metadata_preview", inspect)
    list(app.stream_automatic_detected_metadata_preview(
        ["first.pdf"], [], "old title", "old author", "old label",
        selection_state={"metadata_user_edits": {}},
    ))
    inspect.assert_called_once_with(["first.pdf"], "", "", "", True, False)
    inspect.reset_mock()
    state = {"metadata_user_edits": {"document_author": True}}
    list(app.stream_automatic_detected_metadata_preview(
        ["first.pdf"], [], "old title", "manual author", "old label", selection_state=state,
    ))
    inspect.assert_called_once_with(["first.pdf"], "", "manual author", "", True, False)

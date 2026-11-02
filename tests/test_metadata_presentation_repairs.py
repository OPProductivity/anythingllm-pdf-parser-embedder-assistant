"""Focused metadata presentation and corroboration regressions."""

import pytest

import auto_anythingllm_pipeline as pipeline
import rag_pdf_gradio_app as app
from author_metadata.identity import (
    normalize_metadata_title,
    readable_person_author_case,
    repair_metadata_title_encoding,
    resolve_author_from_metadata_and_inference,
)
from author_metadata.pdf import corroborate_terminal_filename_credit


pytestmark = pytest.mark.offline_deterministic


def test_embedded_title_repairs_two_misdecodes_and_keeps_ordinary_titles():
    damaged = (
        "Conservatism, neoliberalism and resentment in Trumpland: "
        "The Ã¢Â&#x80;Â&#x98;betrayalÃ¢Â&#x80;Â&#x99; of the United States"
    )
    expected = (
        "Conservatism, neoliberalism and resentment in Trumpland: "
        "The ‘betrayal’ of the United States"
    )
    assert repair_metadata_title_encoding(damaged) == expected
    assert normalize_metadata_title(damaged) == expected
    for ordinary in ("Ângela's Study", "The ‘betrayal’ of the United States", "Études culturelles"):
        assert repair_metadata_title_encoding(ordinary) == ordinary


def test_accepted_person_case_and_short_citation_roster():
    report = {"author": "ANTHONY HARKINS, MEREDITH McCARROLL",
              "source": "text_titlepage_publisher_byline"}
    resolved = resolve_author_from_metadata_and_inference("", report)
    assert resolved["author"] == "Anthony Harkins, Meredith McCarroll"
    assert pipeline.default_short_label("Appalachian Reckoning", resolved["author"]) == "Harkins and McCarroll"
    stem = pipeline.native_identity_stem({
        "source_short_label": "Harkins and McCarroll",
        "pdf_page": 1, "pdf_page_end": 1, "segment_index": 1,
    }, page_parent=True)
    assert stem.startswith("harkins-and-mccarroll-p1-")
    assert pipeline.default_short_label("Example", "Jane Doe, John Roe, Ari Smith") == "Doe et al."
    assert pipeline.default_short_label("Example", "J. D. VANCE") == "Vance"
    assert readable_person_author_case("Brian Fraga") == "Brian Fraga"
    assert readable_person_author_case("MEREDITH MCCARROLL") == "Meredith McCarroll"
    assert resolve_author_from_metadata_and_inference(
        "", report, author_override="ANTHONY HARKINS"
    )["author"] == "ANTHONY HARKINS"


def test_terminal_filename_credit_needs_matching_title_and_full_name():
    report = {"author": "Stefan Borg", "source": "text_top_block_names",
              "page": 5, "evidence": "Stefan Borg"}
    path = "The Return of the Common Good - Stefan Borg.pdf"
    promoted = corroborate_terminal_filename_credit(path, "The Return of the Common Good", report)
    assert promoted["source"] == "filename_corroborated_text_name"
    assert corroborate_terminal_filename_credit(path, "A Different Work", report) == report
    assert corroborate_terminal_filename_credit(
        "The Return of the Common Good - Another Name.pdf", "The Return of the Common Good", report
    ) == report
    assert corroborate_terminal_filename_credit(
        path, "The Return of the Common Good", {**report, "evidence": "Stefan Borg / unrelated"}
    ) == {**report, "evidence": "Stefan Borg / unrelated"}


def test_missing_author_is_visibly_flagged_and_full_evidence_retained():
    rows = [("Author", "Not set", "not available"),
            ("Short citation label", "PDF", "derived"),
            ("Title used", "The Return of the Common Good", "metadata")]
    table = app.metadata_table_html(rows, missing_author=True)
    assert 'data-author-missing="true"' in table
    assert table.count('class="metadata-missing-author-row"') == 2
    assert app.concise_author_evidence(
        "W. Carson Byrd", "Hillbillies, Genetic Pathology / W. Carson Byrd"
    ) == "W. Carson Byrd"
    assert app.concise_author_evidence("", "Stefan Borg") == "Stefan Borg"


def test_finished_preview_reports_missing_author_count_without_selected_word(monkeypatch):
    def preview(paths, *_args, **_kwargs):
        missing = paths[0] == "first.pdf"
        return (
            app.gr.update(value=""), app.gr.update(value=""), app.gr.update(value=""),
            app.metadata_table_html([
                ("Author", "Not set" if missing else "Jane Doe", "inference"),
                ("Short citation label", "PDF" if missing else "Doe", "derived"),
            ], missing_author=missing),
            app.gr.update(),
        )
    monkeypatch.setattr(app, "detected_metadata_preview", preview)
    monkeypatch.setattr(app, "folder_manifest_candidates", lambda files, _manifest: (files or [], True))
    events = list(app.stream_automatic_detected_metadata_preview(
        ["first.pdf", "second.pdf"], [],
    ))
    status = events[-1][4]
    assert "Preview ready for 2 of 2 PDFs." in status
    assert "(1 file lacks recognized author metadata!)" in status
    assert "Expand here" in status
    assert "selected PDFs" not in status


def test_auto_start_holds_when_picker_value_changes_before_selection_state(monkeypatch):
    def should_not_run(*_args, **_kwargs):
        raise AssertionError("Stale metadata must not start a run")
    monkeypatch.setattr(app, "run_automatic_for_browser_stream", should_not_run)
    selection = {"state": "ready", "revision": 2,
                 "selection_signature": app.automatic_selection_signature(["old.pdf"])}
    status = app.metadata_scan_status_html(
        "ready", "Preview ready", scope="all", complete=True, revision=2,
    )
    outputs = list(app.run_server_owned_auto_confirmation(
        True, True, status, selection, "", {}, ["new.pdf"], [],
    ))
    assert len(outputs) == 1
    assert len(outputs[0]) == 17

from unittest import mock

import pytest

import rag_pdf_gradio_app as app


pytestmark = pytest.mark.offline_deterministic


@pytest.fixture(autouse=True)
def idle_form():
    with mock.patch.object(app, "LIVE_AUTOMATIC_RUN_STATUS", {}):
        yield


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("selected", ["target", app.NEW_DOCUMENT_WORKSPACE_VALUE, "", "removed"])
def test_refresh_preserves_slug_and_readiness_target(fallback, selected):
    choices = [("Renamed target (target)", "target")]
    response = (200, {"workspaces": [{"name": "Renamed target", "slug": "target"}]})
    with (
        mock.patch.object(app, "ensure_anythingllm_runtime", return_value={}),
        mock.patch.object(app, "api_get_json", side_effect=OSError("offline") if fallback else None, return_value=response),
        mock.patch.object(app, "local_workspace_choices", return_value=(choices, "Local workspaces")),
        mock.patch.object(app, "native_upload_readiness_report", return_value={}) as readiness,
        mock.patch.object(app, "native_upload_readiness_html", return_value="readiness"),
    ):
        update, status, _ = app.refresh_workspaces_with_readiness("http://localhost:3001", "", selected)
    expected = selected if selected in {"target", app.NEW_DOCUMENT_WORKSPACE_VALUE} else None
    assert update["value"] == expected
    assert readiness.call_args.args[2] == expected
    assert ("Renamed target (target)", "target") in update["choices"]
    if selected == "removed":
        assert "unavailable" in status


def test_initial_workspace_default_is_unchanged():
    assert app.refreshed_workspace_value([], False, True) == app.NEW_DOCUMENT_WORKSPACE_VALUE


def test_retry_resets_every_control_and_omitted_evidence_setting():
    expected = app.reset_automatic_run_settings_to_defaults()
    actual = app.reset_automatic_run_retry_settings()
    assert actual[:-1] == expected
    assert len(actual) == 46
    assert actual[9]["value"] == app.fresh_automatic_run_setting_values()["workspace_slug"]
    assert actual[8]["value"] == ""
    assert actual[10]["value"] == ""
    assert actual[11] == ""
    assert actual[-1]["value"] is False


def test_clear_resets_workspace_but_new_file_preserves_operator_target():
    with mock.patch.object(app, "local_workspace_choices", return_value=([("Target", "target")], "")):
        added = app.reset_automatic_run_selection_settings("target", ["new.pdf"], [], {"state": "pending"})
        cleared = app.reset_automatic_run_selection_settings("target", [], [], {"state": "pending"})
        mixed_clear = app.reset_automatic_run_selection_settings("target", [], ["batch.pdf"], {"state": "pending"})
    assert added[9]["value"] == "target"
    assert cleared[9]["value"] == app.fresh_automatic_run_setting_values()["workspace_slug"]
    assert mixed_clear[9]["value"] == cleared[9]["value"]


def test_retry_same_paths_is_not_an_acknowledgement_replay():
    ready = {"state": "ready", "revision": 7, "selection_signature": "same"}
    with mock.patch.object(app, "automatic_selection_signature", return_value="same"):
        replay = app.automatic_selection_begin_state(ready, "old-run", ["same.pdf"])
        retry = app.automatic_retry_selection_begin_state(ready, "old-run", ["same.pdf"])
    assert replay[0] == ready
    assert replay[1] == "old-run"
    assert retry[0]["state"] == "pending"
    assert retry[0]["revision"] == 8
    assert retry[1] == ""
    assert retry[3]["interactive"] is False


def test_completed_selection_replay_cannot_overwrite_new_settings_or_completion():
    app.LIVE_AUTOMATIC_RUN_STATUS = {"state": "successful", "run_root": "previous"}
    ready = {"state": "ready"}
    settings = app.reset_automatic_run_selection_settings("target", ["same.pdf"], [], ready)
    presentation = app.reset_automatic_run_presentation(["same.pdf"], [], ready)
    assert all("value" not in update for update in settings + presentation)
    assert app.LIVE_AUTOMATIC_RUN_STATUS["run_root"] == "previous"


def test_retry_file_change_replay_cannot_restore_previous_workspace():
    with (
        mock.patch.object(app, "automatic_selection_signature", return_value="same"),
        mock.patch.object(app, "local_workspace_choices", return_value=([("Target", "target")], "")),
    ):
        retry = app.automatic_retry_selection_begin_state({"state": "ready"}, "previous", ["same.pdf"])
        replay = app.automatic_selection_begin_state(retry[0], "", ["same.pdf"])
        settings = app.reset_automatic_run_selection_settings("target", ["same.pdf"], [], replay[0])
    assert replay[0] == retry[0]
    assert settings[9]["value"] == app.fresh_automatic_run_setting_values()["workspace_slug"]


def test_explicit_reset_acknowledgement_does_not_absorb_new_selection():
    with mock.patch.object(app, "automatic_selection_signature", return_value="same"):
        retry = app.automatic_retry_selection_begin_state({"state": "ready", "revision": 1}, "previous", ["same.pdf"])
        folder = app.automatic_folder_selection_begin_state(retry[0], "", ["same.pdf"])
    with mock.patch.object(app, "automatic_selection_signature", return_value="different"):
        direct = app.automatic_selection_begin_state(retry[0], "", ["different.pdf"])
    assert folder[0]["revision"] == direct[0]["revision"] == 3
    assert folder[0]["accept_next_signature"] is True
    assert "reset_per_run_defaults" not in folder[0]
    assert "reset_per_run_defaults" not in direct[0]


def test_batch_clear_releases_old_signature_and_reenables_controls():
    with mock.patch.object(app, "automatic_selection_signature", side_effect=["old-files", ""]):
        begun = app.automatic_clear_selection_begin_state({"state": "ready"}, "previous", [], ["batch.pdf"])
        app.reset_automatic_run_presentation([], [])
        finished = app.automatic_selection_finish_state(begun[0], [], [], {})
    assert begun[1] == ""
    assert finished[0]["state"] == "idle"
    assert finished[1]["interactive"] is True
    assert finished[5]["interactive"] is True


@pytest.mark.parametrize("state", ["running", "preparing"])
def test_explicit_resets_cannot_change_active_run(state):
    app.LIVE_AUTOMATIC_RUN_STATUS = {"state": state, "run_root": "owned", "confirmation_in_flight": state == "preparing"}
    assert all("value" not in update for update in app.reset_automatic_run_retry_settings())
    assert all("value" not in update for update in app.clear_selected_pdf_batch())
    assert "value" not in app.reuse_selected_pdf_files(["next.pdf"])


def test_reserved_preparation_does_not_reset_the_evidence_toggle():
    app.LIVE_AUTOMATIC_RUN_STATUS = {"state": "preparing", "run_root": "reserved"}
    assert all("value" not in update for update in app.reset_automatic_run_retry_settings())


def test_fast_duplicate_receipt_counts_existing_vectors_not_new_queue_work(tmp_path):
    summary = app.page_parent_duplicate_shortcut_summary(
        tmp_path / "source.pdf", tmp_path / "receipt",
        {"physical_page_count": 10, "source_sha256": "a" * 64},
        {"status": "complete"},
    )
    with mock.patch.object(app, "api_get_json", side_effect=AssertionError("skip must not submit")):
        report = app.upload_prepared_automatic_batch(
            [summary], api_url="http://localhost:3001", api_key="",
            workspace_slug="target", run_root=tmp_path,
        )
    assert report["selected_records"] == 10
    assert report["existing_workspace_records"] == 10
    assert report["vector_confirmed_records"] == 10
    assert report["newly_attached_records"] == 0
    assert report["queue_requested_records"] == 0
    assert report["queue_accepted_records"] == 0
    assert report["queue_completed_records"] == 0
    assert app.automatic_terminal_document_counts(report) == (0, 1)


def test_terminal_document_counts_require_full_source_proof():
    proven = {"records": 10, "embedded": 10, "uploaded": 0, "post_status": "pass", "searchability_proven": True}
    report = {"document_results": {
        "existing": proven,
        "new": proven | {"uploaded": 10},
        "partial": proven | {"embedded": 9, "uploaded": 9},
        "unproven": proven | {"searchability_proven": False},
        "duplicate": proven | {"records": 0, "embedded": 0},
    }}
    assert app.automatic_terminal_document_counts(report) == (2, 2)


def test_terminal_legacy_transaction_fallback_remains_supported():
    assert app.automatic_terminal_document_counts({"source_transactions": [
        {"state": "exact_vectors_proven", "newly_attached_records": 1},
        {"state": "ambiguous_external_mutation_held", "uploaded": 0},
    ]}) == (1, 1)


@pytest.mark.parametrize("defect", [None, "global_cache_only", "partial_vectors", "missing_target_links", "unproven", "local_mode", "prepared_transcript", "ordinary_prepared_skip"])
def test_only_proven_target_workspace_skip_can_omit_new_txt(defect):
    summary = {"pdf": "same.pdf", "post_upload_classification": "workspace_existing_content_skipped", "post_upload_verification_status": "pass",
               "workspace_duplicate_preflight": {"status": "fast_physical_page_identity_skip"}}
    proof = {"records": 7, "existing_workspace_records": 7, "embedded": 7, "uploaded": 0,
             "post_classification": "workspace_existing_content_skipped", "post_status": "pass", "searchability_proven": True}
    if defect == "global_cache_only":
        proof["post_classification"] = "prepared_record_cache_reused"
    elif defect == "partial_vectors":
        proof["embedded"] = 6
    elif defect == "missing_target_links":
        proof["existing_workspace_records"] = 0
    elif defect == "unproven":
        proof["searchability_proven"] = False
    elif defect == "prepared_transcript":
        summary["upload_file"] = "prepared.txt"
    elif defect == "ordinary_prepared_skip":
        summary["workspace_duplicate_preflight"] = {"status": "complete_selected_content_skipped"}
    required, skipped = app.automatic_local_text_export_selection([summary], defect != "local_mode", {"document_results": {"same.pdf": proof}})
    assert skipped == (1 if defect is None else 0)
    assert required == ([] if defect is None else [summary])


def test_mixed_batch_still_requires_new_sources_local_exports():
    skipped = {"pdf": "old.pdf", "post_upload_classification": "workspace_existing_content_skipped", "post_upload_verification_status": "pass",
               "workspace_duplicate_preflight": {"status": "fast_physical_page_identity_skip"}}
    fresh = {"pdf": "new.pdf", "post_upload_classification": "uploaded"}
    proof = {"records": 7, "existing_workspace_records": 7, "embedded": 7, "uploaded": 0,
             "post_classification": "workspace_existing_content_skipped", "post_status": "pass", "searchability_proven": True}
    assert app.automatic_local_text_export_selection([skipped, fresh], True, {"document_results": {"old.pdf": proof}}) == ([fresh], 1)


def test_export_exemption_preserves_selected_duplicate_filename_semantics():
    fresh = {"pdf": "new.pdf", "source_sha256": "hash"}
    alias = {"pdf": "alias.pdf", "source_sha256": "hash", "api_upload_status": "skipped_exact_duplicate", "selected_input_duplicate_of": "new.pdf"}
    required, skipped = app.automatic_local_text_export_selection([fresh, alias], True, {})
    assert required == [fresh, alias]
    assert required[1] is alias
    assert skipped == 0


def test_reset_event_outputs_include_all_mounted_controls():
    config = app.demo.get_config_file()
    components = {c["id"]: c for c in config["components"]}
    evidence = next(c["id"] for c in components.values() if c.get("props", {}).get("label") == "Retain detailed evidence even if this run is ready")
    resets = [d for d in config["dependencies"] if "reset_automatic_run_retry_settings" in str(d.get("api_name")) or "reset_automatic_run_selection_settings" in str(d.get("api_name"))]
    assert len(resets) == 5
    for dependency in resets:
        assert len(dependency["outputs"]) == 46
        assert evidence in dependency["outputs"]
        assert all(output in components for output in dependency["outputs"])

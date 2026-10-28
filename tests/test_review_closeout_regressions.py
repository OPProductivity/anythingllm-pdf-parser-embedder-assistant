"""Offline regressions for the bounded October review findings.

Temporary local storage and synthetic text only: no installed Desktop or API.
"""
import csv
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

import auto_anythingllm_pipeline as pipeline
import rag_pdf_gradio_app as app
import rag_pdf_tools
from automatic_worker_protocol import serializable_automatic_worker_arguments

pytestmark = pytest.mark.offline_deterministic


def confirm_settings(tmp_path):
    source = tmp_path / "fixture.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture for mocked dispatch")
    settings = {field: "" for field in app.AUTOMATIC_RUN_FIELDS}
    settings.update(files=[str(source)], document_label="Fixture", mode=app.MODE_NATIVE_UPLOAD_LABEL,
                    workspace_slug=app.NEW_DOCUMENT_WORKSPACE_VALUE, output_root_override=str(tmp_path))
    return settings


def test_output_failure_stops_before_workspace_creation(tmp_path):
    with (
        mock.patch.object(app, "automatic_native_mutation_compatibility_report", return_value={"status": "pass"}),
        mock.patch.object(app, "automatic_batch_output_capacity_preflight",
                          return_value={"status": "insufficient_space", "message": "Not enough room"}),
        mock.patch.object(app, "create_new_document_workspace") as create,
        mock.patch.object(app, "dispatch_confirmed_automatic_run") as dispatch,
    ):
        result = app.run_automatic_from_confirmation(confirm_settings(tmp_path), progress=lambda *a, **k: None)
    create.assert_not_called()
    dispatch.assert_not_called()
    assert "Not enough room" in str(result)
    assert "No workspace was created" in str(result)


def test_post_creation_failure_identifies_retained_workspace(tmp_path):
    with (
        mock.patch.object(app, "automatic_native_mutation_compatibility_report", return_value={"status": "pass"}),
        mock.patch.object(app, "create_new_document_workspace",
                          return_value={"status": "created", "workspace_slug": "created-fixture"}),
        mock.patch.object(app, "local_workspace_choices", return_value=([("Fixture", "created-fixture")], "ok")),
        mock.patch.object(app, "dispatch_confirmed_automatic_run", side_effect=RuntimeError("Later failure")) as dispatch,
        mock.patch.object(app, "update_live_automatic_run_status"),
    ):
        result = app.run_automatic_from_confirmation(confirm_settings(tmp_path), progress=lambda *a, **k: None)
    assert "created-fixture" in str(result)
    assert "not been automatically removed" in str(result)
    assert result[7]["value"] == "created-fixture"
    assert dispatch.call_args.args[0]["_created_workspace_slug"] == "created-fixture"


@pytest.mark.parametrize("result,expected", [
    ({"status": "runtime_unavailable", "error": "offline"}, "No workspace was created"),
    ({"status": "error", "error": "response lost"}, "could not be confirmed"),
    ({"status": "workspace_display_name_update_failed", "workspace_slug": "possible-fixture",
      "error": "rename failed"}, "possible-fixture"),
])
def test_workspace_creation_failure_does_not_guess_external_state(tmp_path, result, expected):
    with (
        mock.patch.object(app, "automatic_native_mutation_compatibility_report", return_value={"status": "pass"}),
        mock.patch.object(app, "automatic_batch_output_capacity_preflight", return_value={"status": "pass"}),
        mock.patch.object(app, "create_new_document_workspace", return_value=result),
    ):
        response = app.run_automatic_from_confirmation(confirm_settings(tmp_path), progress=lambda *a, **k: None)
    assert expected in str(response)


def test_preupload_hold_note_distinguishes_creation_from_upload():
    assert "created-fixture" in app.automatic_preupload_mutation_note("created-fixture")
    assert "No document upload has started" in app.automatic_preupload_mutation_note()
    assert "was created" not in app.automatic_preupload_mutation_note()


@pytest.mark.parametrize("existing,new,confirmed", [(8, 2, 8), (0, 2, 1), (0, 0, 0)])
def test_cancellation_counts_keep_proof_and_attachment_populations_separate(existing, new, confirmed):
    report = app.explicit_upload_count_schema({"existing_workspace_records": existing,
        "newly_attached_records": new, "vector_confirmed_records": confirmed})
    message = app.cancellation_reconciliation_message(report, 2)
    assert f"proof covers {confirmed} selected record(s)" in message
    assert f"{new} record(s) were newly attached" in message
    assert " of " not in message


@pytest.mark.parametrize("mode,inherited,override,expected", [
    ("page_limit", 1000, 200, 200), ("page_limit", 1000, 0, 1000),
    ("page_limit", 150, 200, 150), ("passages", 1000, 200, 1000), ("none", 1000, 200, 1000),
])
def test_page_ceiling_is_a_mode_specific_upper_bound(mode, inherited, override, expected):
    assert pipeline.page_preserve_segment_limit(mode, inherited, override) == expected


def test_page_ceiling_survives_the_actual_worker_json_boundary():
    synthetic_key = "synthetic-value"
    payload, key = serializable_automatic_worker_arguments(SimpleNamespace(
        segment_mode="page_limit", target_passage_length=200, page_preserve_ceiling=200,
        anythingllm_chunk_size=1000, anythingllm_api_key=synthetic_key,
    ))
    assert payload["page_preserve_ceiling"] == 200
    assert "anythingllm_api_key" not in payload and key == synthetic_key
    assert pipeline.page_preserve_segment_limit(payload["segment_mode"], payload["anythingllm_chunk_size"],
                                                payload["page_preserve_ceiling"]) == 200


def test_page_ceiling_splits_locally_and_parent_fallback_preserves_provenance():
    text = ("This source page contains one sentence for a bounded split check. " * 10).strip()
    hard_limit = pipeline.page_preserve_segment_limit("page_limit", 1000, 200)
    segments = pipeline.make_segments(Path("fixture.pdf"), "pymupdf", [{"page": 1, "text": text}], 1, 2,
        {"source_id": "fixture", "source_title": "Fixture", "source_author": "Author",
         "source_sha256": "a" * 64}, hard_limit, segment_mode="page_limit", effective_limit=hard_limit)
    assert len(segments) > 1
    assert all(len(row["text"]) <= 200 and row["pdf_page"] == 1 for row in segments)
    assert " ".join(row["text"] for row in segments).split() == text.split()
    parents = pipeline.build_page_parent_rows(segments)
    rows = pipeline.harmonization_rows(segments, parents, hard_limit, 0, {"max_chunk_length": 1000})
    representation, report = pipeline.resolve_native_upload_representation("page_parents", rows)
    assert representation == "segments" and report["applied"]
    assert len(parents) == 1 and parents[0]["pdf_page"] == 1


def test_two_page_pdf_preparation_applies_ceiling_and_parent_fallback(tmp_path):
    import fitz
    from manifest_text import read_manifest_rows
    source = tmp_path / "fixture.pdf"
    with fitz.open() as document:
        for page_number in (1, 2):
            page = document.new_page()
            sentence = ("Alpha theory explains the first subject with clear local boundaries. "
                        if page_number == 1 else
                        "Beta evidence develops another interpretation with independent supporting detail. ")
            page.insert_textbox(fitz.Rect(60, 60, 540, 760),
                f"Page {page_number} sentinel. " + sentence * 18,
                fontsize=10)
        document.save(source)
    args = SimpleNamespace(document_label="Fixture", document_author="Ada Writer", document_short_label="Fixture",
        use_file_title_fallback=False, deep_extraction=False, include_front_matter=True, include_back_matter=True,
        backend_mode="pymupdf", first_page_override=1, end_page_override=0, target_passage_length=200,
        page_preserve_ceiling=200, segment_mode="page_limit", end_section_names=[], validation_phrases=[],
        unstructured_strategy="fast", marker_style="short", disable_inline_markers=True,
        run_vector_eval=False, ollama_model="", ollama_url="", max_vector_probes=0,
        prepare_and_upload=False, anythingllm_api_url="", anythingllm_api_key="", workspace_slug="",
        test_workspace_slug="", upload_limit=0, anythingllm_storage_dir=str(tmp_path / "missing-storage"),
        anythingllm_chunk_size=1000, anythingllm_chunk_overlap=0, native_upload_representation="page_parents",
        run_author_inference_sample_evaluation=False)
    summary = pipeline.prepare_pdf(source, tmp_path / "prepared", args)
    rows = read_manifest_rows(Path(summary["manifest"]))
    assert {row["pdf_page"] for row in rows} == {1, 2}
    assert all(len(row["text"]) <= 200 for row in rows)
    assert summary["native_upload_representation"] == "segments"
    harmonization = json.loads(Path(summary["harmonization_report"]).read_text(encoding="utf-8"))
    assert all(row["anythingllm_chunk_size"] == 1000 for row in harmonization)
    assert all(row["local_segment_limit"] == 200 for row in harmonization)


def test_missing_lancedb_is_not_initialized_by_inspection(tmp_path):
    before = list(tmp_path.iterdir())
    report = pipeline.inspect_anythingllm_storage(tmp_path)
    assert report["status"] == "missing_lancedb_dir"
    assert list(tmp_path.iterdir()) == before
    assert pipeline.inspect_anythingllm_storage(tmp_path / "absent")["status"] == "missing_storage_dir"


@pytest.mark.parametrize("child", [False, True])
def test_uninitialized_lancedb_child_is_not_created_or_modified(tmp_path, child):
    database = tmp_path / "lancedb"
    database.mkdir()
    if child:
        (database / "unrelated.txt").write_text("preserve", encoding="utf-8")
    before = sorted(p.name for p in database.iterdir())
    report = pipeline.inspect_anythingllm_storage(tmp_path)
    assert report["status"] == "missing_lancedb_manifest"
    assert sorted(p.name for p in database.iterdir()) == before
    proof = pipeline.inspect_workspace_vector_ids(tmp_path, "fixture", ["vector-1"])
    assert proof["status"] == "workspace_table_missing"
    assert sorted(p.name for p in database.iterdir()) == before


def test_existing_lancedb_inspection_preserves_fixture_files(tmp_path):
    import lancedb
    database = tmp_path / "lancedb"
    db = lancedb.connect(str(database))
    db.create_table("fixture", data=[{"id": "vector-1", "vector": [0.1, 0.2]}])
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    report = pipeline.inspect_anythingllm_storage(tmp_path)
    assert report["status"] == "complete"
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")) == before
    proof = pipeline.inspect_workspace_vector_ids(tmp_path, "fixture", ["vector-1"])
    assert proof["status"] == "complete" and proof["present_ids"] == {"vector-1"}
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")) == before


def upload_fixture(tmp_path):
    storage = tmp_path / "storage"
    staged = storage / "documents" / "custom-documents"
    staged.mkdir(parents=True)
    text_file = tmp_path / "page-0001.txt"
    text_file.write_text("Prepared fixture text.", encoding="utf-8")
    row = {"filename": text_file.name, "title": "Fixture", "docAuthor": "Author", "description": "Page 1",
           "docSource": "local-pdf://sha256/" + "a" * 64, "chunkSource": "page-parent://fixture-p0001",
           "text_file": str(text_file)}
    return storage, staged, row


@pytest.mark.parametrize("response", ["success", "non_2xx", "lost_response"])
@pytest.mark.parametrize("folder", ["custom-documents", "custom-documents/managed-pages"])
def test_normal_and_reconciled_uploads_honor_requested_folder(tmp_path, response, folder):
    storage, staged, row = upload_fixture(tmp_path)
    metadata = pipeline.upload_plan_rows_to_expected_payloads([row])[0]["metadata"]
    def post(*_args, **_kwargs):
        (staged / "page-0001.json").write_text(json.dumps({"pageContent": "Prepared fixture text.", **metadata}), encoding="utf-8")
        if response == "lost_response":
            raise ConnectionError("response lost after staging")
        if response == "non_2xx":
            return 502, "gateway lost the response"
        return 200, json.dumps({"documents": [{"location": "custom-documents/page-0001.json"}]})
    with mock.patch.object(pipeline, "post_multipart_form", side_effect=post) as posted:
        report = pipeline.maybe_upload_segment_files("http://127.0.0.1:3001", "fixture-key", [row],
            workspace_slug="fixture", folder_name=folder, storage_dir=storage, defer_embedding_update=True)
    assert report["status"] == "attached_pending_queue" and not report["errors"]
    assert posted.call_count == 1
    assert report["locations"] == [folder + "/page-0001.json"]
    assert (storage / "documents" / report["locations"][0]).is_file()


def test_reconciled_upload_uses_per_source_folder_and_holds_on_move_failure(tmp_path):
    storage, staged, row = upload_fixture(tmp_path)
    row["_anythingllm_folder_name"] = "custom-documents/source-specific"
    metadata = pipeline.upload_plan_rows_to_expected_payloads([row])[0]["metadata"]
    def post(*_args, **_kwargs):
        (staged / "page-0001.json").write_text(json.dumps({"pageContent": "Prepared fixture text.", **metadata}), encoding="utf-8")
        raise ConnectionError("response lost")
    with (
        mock.patch.object(pipeline, "post_multipart_form", side_effect=post) as posted,
        mock.patch.object(pipeline.shutil, "move", side_effect=PermissionError("locked")) as move,
    ):
        report = pipeline.maybe_upload_segment_files("http://127.0.0.1:3001", "fixture-key", [row],
            workspace_slug="fixture", folder_name="custom-documents", storage_dir=storage, defer_embedding_update=True)
    assert posted.call_count == 1 and move.call_count == 1
    assert "source-specific" in str(move.call_args)
    assert report["status"] == "error" and report["errors"]
    assert not report["locations"]
    assert report["attachment_results"][0]["status"] == "relocation_failed"


def test_relocation_rejects_external_path_and_existing_target(tmp_path):
    storage, staged, _row = upload_fixture(tmp_path)
    source = staged / "page-0001.json"
    source.write_text("new", encoding="utf-8")
    target = staged / "managed-pages" / source.name
    target.parent.mkdir()
    target.write_text("existing", encoding="utf-8")
    _, outside_error = pipeline.relocate_uploaded_document(storage, str(tmp_path / "outside.json"),
                                                            "custom-documents/managed-pages")
    _, collision_error = pipeline.relocate_uploaded_document(storage, "custom-documents/page-0001.json",
                                                              "custom-documents/managed-pages")
    assert "outside" in outside_error
    assert "already exists" in collision_error
    assert source.read_text(encoding="utf-8") == "new"
    assert target.read_text(encoding="utf-8") == "existing"
    same_location, same_error = pipeline.relocate_uploaded_document(
        storage, "custom-documents/managed-pages/page-0001.json", "custom-documents/managed-pages",
    )
    assert same_location == "custom-documents/managed-pages/page-0001.json" and not same_error


def test_normal_upload_receipt_records_final_moved_location(tmp_path):
    storage, staged, row = upload_fixture(tmp_path)
    receipt_path = tmp_path / "receipts.jsonl"
    metadata = pipeline.upload_plan_rows_to_expected_payloads([row])[0]["metadata"]
    def post(*_args, **_kwargs):
        (staged / "page-0001.json").write_text(json.dumps({"pageContent": "Prepared fixture text.", **metadata}), encoding="utf-8")
        return 200, json.dumps({"documents": [{"location": "custom-documents/page-0001.json"}]})
    with mock.patch.object(pipeline, "post_multipart_form", side_effect=post):
        report = pipeline.maybe_upload_segment_files("http://127.0.0.1:3001", "fixture-key", [row],
            workspace_slug="fixture", folder_name="custom-documents/managed-pages", storage_dir=storage,
            submission_receipt_path=receipt_path, defer_embedding_update=True)
    receipts = [json.loads(line) for line in receipt_path.read_text(encoding="utf-8").splitlines()]
    assert report["status"] == "attached_pending_queue"
    assert receipts[-1]["document_location"] == "custom-documents/managed-pages/page-0001.json"


def relational_fixture(tmp_path, pages=1):
    with sqlite3.connect(tmp_path / "anythingllm.db") as con:
        con.executescript("create table workspaces (id integer,slug text);"
            "create table workspace_documents (workspaceId integer,docId text,filename text,docpath text,metadata text);"
            "create table document_vectors (docId text,vectorId text);")
        con.execute("insert into workspaces values(1,'fixture')")
        for page in range(1, pages + 1):
            con.execute("insert into workspace_documents values(1,?,?,?,?)", (f"doc-{page}", f"p{page}.json",
                f"custom-documents/p{page}.json", json.dumps({"chunkSource": f"page-parent://fixture-p{page}"})))
            con.execute("insert into document_vectors values(?,?)", (f"doc-{page}", f"vector-{page}"))
    con.close()
    return [{"metadata": {"chunkSource": f"page-parent://fixture-p{page}"}, "_automatic_source_path": "fixture.pdf"}
            for page in range(1, pages + 1)]


def test_stale_sqlite_mapping_cannot_be_physical_skip_proof(tmp_path):
    payloads = relational_fixture(tmp_path)
    report = pipeline.workspace_duplicate_identity_audit(tmp_path, "fixture", payloads)
    assert report["skip_proof_blocked"]
    assert report["status"] == "physical_vector_proof_unavailable"
    assert not report["fully_indexed_source_paths"]
    assert not (tmp_path / "lancedb").exists()
    # Inventory without a skip request remains relational and read-only.
    assert pipeline.workspace_duplicate_identity_audit(tmp_path, "fixture")["status"] == "complete"


def test_existing_document_without_linked_vector_holds_skip_proof(tmp_path):
    payloads = relational_fixture(tmp_path)
    with sqlite3.connect(tmp_path / "anythingllm.db") as con:
        con.execute("delete from document_vectors")
    report = pipeline.workspace_duplicate_identity_audit(tmp_path, "fixture", payloads)
    assert report["skip_proof_blocked"]
    assert not report["fully_indexed_source_paths"]


def test_one_of_multiple_linked_vectors_missing_blocks_skip(tmp_path):
    import lancedb
    payloads = relational_fixture(tmp_path)
    with sqlite3.connect(tmp_path / "anythingllm.db") as con:
        con.execute("insert into document_vectors values(?,?)", ("doc-1", "vector-1b"))
    db = lancedb.connect(str(tmp_path / "lancedb"))
    db.create_table("fixture", data=[{"id": "vector-1", "vector": [0.1, 0.2]}])
    report = pipeline.workspace_duplicate_identity_audit(tmp_path, "fixture", payloads)
    assert report["skip_proof_blocked"]
    assert report["expected_ready_chunk_source_count"] == 0


def test_physical_lookup_failure_holds_skip(tmp_path):
    payloads = relational_fixture(tmp_path)
    with mock.patch.object(pipeline, "inspect_workspace_vector_ids", side_effect=RuntimeError("read failed")):
        report = pipeline.workspace_duplicate_identity_audit(tmp_path, "fixture", payloads)
    assert report["status"] == "error"
    assert not report["fully_indexed_source_paths"]


def test_unavailable_duplicate_inventory_holds_before_attachment(tmp_path):
    storage, _staged, row = upload_fixture(tmp_path)
    with sqlite3.connect(storage / "anythingllm.db") as con:
        con.execute("create table unrelated (id integer)")
    plan = tmp_path / "upload-plan.csv"
    with plan.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
    summary = {"pdf": "fixture.pdf", "source_sha256": "a" * 64,
               "native_upload_plan": str(plan), "native_upload_transport": "file_upload",
               "output_root": str(tmp_path / "prepared")}
    with (
        mock.patch.object(app, "default_anythingllm_storage_dir", return_value=storage),
        mock.patch.object(app, "maybe_upload_to_anythingllm") as upload,
    ):
        report = app.upload_prepared_automatic_batch([summary], api_url="http://127.0.0.1:3001",
            api_key="", workspace_slug="fixture", run_root=tmp_path / "run")
    upload.assert_not_called()
    assert report["status"] == "error_workspace_vector_proof"


def test_missing_local_database_holds_skip_mode_before_attachment(tmp_path):
    storage, _staged, row = upload_fixture(tmp_path)
    assert not (storage / "anythingllm.db").exists()
    plan = tmp_path / "upload-plan.csv"
    with plan.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
    summary = {"pdf": "fixture.pdf", "source_sha256": "a" * 64,
               "native_upload_plan": str(plan), "native_upload_transport": "file_upload",
               "output_root": str(tmp_path / "prepared")}
    with (
        mock.patch.object(app, "default_anythingllm_storage_dir", return_value=storage),
        mock.patch.object(app, "maybe_upload_to_anythingllm") as upload,
    ):
        report = app.upload_prepared_automatic_batch([summary], api_url="http://127.0.0.1:3001",
            api_key="", workspace_slug="fixture", run_root=tmp_path / "run")
    upload.assert_not_called()
    assert report["status"] == "error_workspace_vector_proof"
    assert report["uploaded"] == 0 and report["embedded"] == 0
    assert report["duplicate_preflight"]["status"] == "missing_db"


def test_live_api_workspace_can_pass_readiness_without_local_database(tmp_path):
    with (
        mock.patch.object(app, "default_anythingllm_storage_dir", return_value=tmp_path),
        mock.patch.object(app, "local_workspace_slug_exists", return_value=(False, "Local database missing")),
        mock.patch.object(app, "ensure_anythingllm_runtime",
                          return_value={"status": "reachable", "message": "API ready"}),
        mock.patch.object(app, "verify_anythingllm_upload_auth",
                          return_value={"authenticated": True, "status": "pass"}),
        mock.patch.object(app, "api_workspace_slug_exists", return_value=(True, "API workspace found")),
    ):
        report = app.native_upload_readiness_report(
            "http://127.0.0.1:3001", "", "fixture", verify_authentication=True)
    assert report["local_db_found"] is False
    assert report["workspace_api_found"] is True
    assert report["workspace_slug_found"] is True


def test_batch_holds_stale_mapping_without_any_attachment_or_reupload(tmp_path):
    storage, _staged, row = upload_fixture(tmp_path)
    relational_fixture(storage)
    row["chunkSource"] = "page-parent://fixture-p1"
    plan = tmp_path / "upload-plan.csv"
    with plan.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
    summary = {"pdf": "fixture.pdf", "source_sha256": "a" * 64,
               "native_upload_plan": str(plan), "native_upload_transport": "file_upload",
               "output_root": str(tmp_path / "prepared")}
    with (
        mock.patch.object(app, "default_anythingllm_storage_dir", return_value=storage),
        mock.patch.object(app, "maybe_upload_to_anythingllm") as upload,
    ):
        report = app.upload_prepared_automatic_batch([summary], api_url="http://127.0.0.1:3001",
            api_key="", workspace_slug="fixture", run_root=tmp_path / "run")
    upload.assert_not_called()
    assert report["status"] == "error_workspace_vector_proof"
    assert report["uploaded"] == 0 and report["embedded"] == 0
    assert report["errors"]
    assert summary["api_upload_status"] == "error_workspace_vector_proof"
    assert summary["post_upload_verification_status"] != "pass"


@pytest.mark.parametrize("namespace,physical_ids,ready", [
    ("fixture", ["vector-1", "vector-2"], 2),
    ("fixture", ["vector-1"], 1), ("other-workspace", ["vector-1", "vector-2"], 0),
])
def test_duplicate_proof_checks_all_ids_in_the_selected_physical_namespace(tmp_path, namespace, physical_ids, ready):
    import lancedb
    payloads = relational_fixture(tmp_path, pages=2)
    db = lancedb.connect(str(tmp_path / "lancedb"))
    db.create_table(namespace, data=[{"id": value, "vector": [0.1, 0.2]} for value in physical_ids])
    report = pipeline.workspace_duplicate_identity_audit(tmp_path, "fixture", payloads)
    assert report["expected_ready_chunk_source_count"] == ready
    assert bool(report["fully_indexed_source_paths"]) == (ready == 2)
    assert report["skip_proof_blocked"] == (ready != 2)


def test_targeted_vector_lookup_is_complete_across_batches_and_duplicate_rows(tmp_path):
    import lancedb
    ids = [f"vector-{index}" for index in range(121)]
    db = lancedb.connect(str(tmp_path / "lancedb"))
    db.create_table("fixture", data=[{"id": value, "vector": [0.1, 0.2]} for value in ids + ids[:3]])
    result = pipeline.inspect_workspace_vector_ids(tmp_path, "fixture", ids)
    assert result["status"] == "complete"
    assert result["present_ids"] == set(ids)


def test_short_ocr_retains_stacked_titles_without_claiming_a_line_cutoff():
    assert rag_pdf_tools.credible_short_ocr_display_text("The\nRoad\nTo\nThe\nOld\nStone\nHouse")
    assert rag_pdf_tools.credible_short_ocr_display_text("PART TWO")
    assert not rag_pdf_tools.credible_short_ocr_display_text("i rn oe qe fi")

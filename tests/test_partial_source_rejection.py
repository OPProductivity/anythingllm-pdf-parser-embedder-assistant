"""Exercise source rejection through production payloads and coordinator callbacks."""

import csv
import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import auto_anythingllm_pipeline as pipeline
import rag_pdf_gradio_app as app
from reliability_audit import _audit_source_transactions, audit_run_directory
from run_evidence import read_run_json


pytestmark = pytest.mark.offline_deterministic


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("live", [False, True])
def test_partition_preserves_ambiguous_and_unrelated_sources(nested, live):
    keys = ["a", "b", "b", "c"]
    payloads = [{"docSource": key} for key in keys]
    if nested:
        payloads = [{"metadata": payload} for payload in payloads]
    rejections = {key: {"source_key": key} for key in ["a", "b", "unselected"]}
    observer = {
        "source_atomic_precommit_rejections": rejections if live else list(rejections.values()),
        "source_atomic_commit_ambiguity": {"source_key": "a"},
    }
    kept, locations, evidence = app.partition_precommit_rejected_vector_expectation(
        payloads, ["a.json", "b1.json", "b2.json", "c.json"], observer
    )
    assert kept == [payloads[0], payloads[3]]
    assert locations == ["a.json", "c.json"]
    assert evidence["source_keys"] == ["b"]
    assert evidence["rejected_records"] == 2


@pytest.mark.parametrize("locations", [[], [""], ["a.json", "extra.json"]])
def test_partition_without_aligned_locations_does_not_remove_expectations(locations):
    payloads = [{"metadata": {"docSource": "a"}}]
    assert app.partition_precommit_rejected_vector_expectation(
        payloads, locations, {"source_atomic_precommit_rejections": {"a": {"source_key": "a"}}}
    ) == (payloads, locations, {})


def _prepared_sources(root, counts):
    summaries = []
    for source, count in enumerate(counts):
        name = f"source-{source}"
        text = root / f"{name}.txt"
        text.write_text("Prepared academic text", encoding="utf-8")
        plan = root / f"{name}.csv"
        with plan.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["filename", "docSource", "chunkSource", "text_file"])
            writer.writeheader()
            for page in range(count):
                writer.writerow({"filename": f"{name}-p{page}.txt", "text_file": str(text),
                                 "docSource": f"local-pdf://sha256/{name}", "chunkSource": f"{name}-p{page}"})
        summaries.append({"pdf": str(root / f"{name}.pdf"), "source_sha256": name,
                          "native_upload_plan": str(plan), "native_upload_transport": "file_upload"})
    return summaries


@pytest.mark.parametrize("counts,rejected,late", [
    ((15, 13, 22, 17), (0, 1), False),
    ((15, 13, 22, 17), (0, 1), True),
    ((1, 1, 1, 1), (0, 1), False),
    ((3, 2, 4, 2), (1,), False),
    ((1, 1), (0, 1), False),
    ((3,), (0,), False),
    ((1, 1), (), False),
])
def test_real_grouped_coordinator_credits_only_physically_proven_siblings(tmp_path, counts, rejected, late):
    summaries = _prepared_sources(tmp_path, counts)
    run = tmp_path / "run"
    keys = [f"local-pdf://sha256/source-{i}" for i in rejected]
    rejections = {key: {"source_key": key, "error": "Connection error."} for key in keys}
    queue_calls, verifier_calls = [], []
    queue = {"queue_records": sum(counts), "completed": sum(counts), "current": sum(counts),
             "observer_state": "connected", "events_observed": 1,
             "source_atomic_precommit_rejections": {} if late else rejections}

    def attach(_api, _key, rows, **_kwargs):
        locations = [f"custom-documents/{row['filename']}.json" for row in rows]
        return {"status": "attached_pending_queue", "uploaded": len(rows), "locations": locations,
                "errors": [], "attachment_results": [
                    {"status": "attached", "location": loc, "source_path": row["_automatic_source_path"],
                     "chunk_source": row["chunkSource"]} for row, loc in zip(rows, locations)]}

    def verify(_storage, _workspace, _sha, payloads, **kwargs):
        verifier_calls.append(len(payloads))
        # Production converter, not the simplified top-level docSource fixture.
        assert all("docSource" not in row and "docSource" in row["metadata"] for row in payloads)
        if late and len(verifier_calls) == 1:
            queue["source_atomic_precommit_rejections"] = rejections
            return {"status": "partial_vector_coverage", "current_upload_vector_evidence_complete": False,
                    "current_upload_document_vector_count": sum(counts[i] for i in range(len(counts)) if i not in rejected)}
        assert all(row["metadata"]["docSource"] not in keys for row in payloads)
        return {"status": "pass", "matching_vector_rows": len(payloads),
                "current_upload_vector_evidence_complete": True,
                "current_upload_document_vector_count": len(payloads),
                "current_upload_locations_with_vectors": kwargs["upload_locations"]}

    def queue_update(_api, _key, _workspace, locations, **kwargs):
        queue_calls.append(locations)
        observation = kwargs["batch_verifier"]({"start_index": 0, "end_index": len(locations),
                                               "locations": locations, "desktop_queue_observer": queue})
        # Keep whole-group searchability false when only siblings were proven.
        safe_rejection = observation.get("safe_source_rejection", False)
        return {"requested": len(locations), "accepted": 0 if safe_rejection else len(locations),
                "queue_records": len(locations), "errors": [], "runtime_events": [],
                "batches": [{"submission_state": "rejected" if safe_rejection else "accepted",
                             "locations": locations, "searchability_proven": not rejected,
                             "verification": observation}],
                "progress_observation": {"final_queue_snapshot": {
                    "source_atomic_precommit_rejections": list(rejections.values())}}}

    with (
        patch.object(app, "default_anythingllm_storage_dir", return_value=tmp_path / "storage"),
        patch.object(app, "ensure_source_atomic_embedding_server", return_value={"enabled": False}),
        patch.object(app, "verify_anythingllm_post_upload", side_effect=verify),
        patch.object(app.time, "sleep", side_effect=lambda _: None),
        patch.object(pipeline, "resolve_anythingllm_api_key", return_value=("fixture", "provided")),
        patch.object(pipeline, "find_reusable_cached_document_locations", return_value=[""] * sum(counts)),
        patch.object(pipeline, "snapshot_staged_document_locations", return_value=set()),
        patch.object(pipeline, "maybe_upload_segment_files", side_effect=attach),
        patch.object(pipeline, "update_workspace_embeddings_desktop_queue", side_effect=queue_update),
    ):
        report = app.upload_prepared_automatic_batch(summaries, api_url="http://fixture", api_key="",
                                                   workspace_slug="fixture", run_root=run)
    expected = sum(counts[i] for i in range(len(counts)) if i not in rejected)
    assert len(queue_calls) == 1
    assert verifier_calls == ([sum(counts), expected] if late else [expected] if expected else [])
    assert report["vector_confirmed_records"] == expected
    assert report["status"] == ("error" if rejected else "complete")
    for i, summary in enumerate(summaries):
        result = report["document_results"][summary["pdf"]]
        assert result["embedded"] == (0 if i in rejected else counts[i])
        assert result["searchability_proven"] == (i not in rejected)
    ledger = read_run_json(run / "source-transaction-ledger.json")
    for i, transaction in enumerate(ledger["transactions"]):
        assert transaction["state"] == ("source_queue_rejected_without_remote_mutation"
                                        if i in rejected else "exact_vectors_proven")
    # The independent terminal audit sees the actual coordinator output.
    (run / "batch-native-upload-report.json").write_text(json.dumps(report), encoding="utf-8")
    (run / "run-progress.json").write_text(json.dumps({"state": "warning" if rejected else "successful"}), encoding="utf-8")
    audit = audit_run_directory(run)
    assert audit["audit_status"] == "pass", audit["findings"]


@pytest.mark.parametrize("mutation,code", [
    ({"uploaded": 1}, "AUDIT-QUEUE-REJECTED-SOURCE-001"),
    ({"embedded": 1}, "AUDIT-QUEUE-REJECTED-SOURCE-002"),
    ({"locations": ["a.json", "a.json"]}, "AUDIT-QUEUE-REJECTED-SOURCE-003"),
    ({"later_sources_released": False}, "AUDIT-QUEUE-REJECTED-SOURCE-004"),
])
def test_queue_rejection_audit_still_detects_invalid_claims(mutation, code):
    row = {"source_index": 1, "state": "source_queue_rejected_without_remote_mutation",
           "planned_records": 2, "uploaded": 2, "embedded": 0,
           "locations": ["a.json", "b.json"], "later_sources_released": True, **mutation}
    findings = []
    _audit_source_transactions({"transaction_count": 1, "transactions": [row]}, findings)
    assert code in {finding.code for finding in findings}


@pytest.mark.parametrize("sources", [1, 2])
def test_queue_wrapper_cannot_reject_siblings_before_their_first_document(sources):
    connected = threading.Event()
    connected.set()
    locations = [f"custom-documents/source-{i}.json" for i in range(sources)]
    verifications = []

    def listener(*_args, **kwargs):
        kwargs["observer_callback"]({"type": "source_rejected_before_commit",
                                     "sourceKey": "local-pdf://sha256/source-0",
                                     "filename": locations[0], "error": "Connection error."})
        return {"connected_event": connected, "stop_event": threading.Event(),
                "thread": SimpleNamespace(join=lambda **_: None, is_alive=lambda: False),
                "events": [], "errors": []}

    def verifier(report):
        verifications.append(report)
        return {"status": "timeout", "classification": "pending_sibling_proof"}

    def batched(*_args, **kwargs):
        observed = kwargs["batch_verifier"]({"locations": locations})
        assert bool(observed.get("safe_source_rejection")) == (sources == 1)
        return {"runtime_events": [], "batches": [{"verification": observed}]}

    with (
        patch.object(pipeline, "start_anythingllm_embed_progress_listener", side_effect=listener),
        patch.object(pipeline, "update_workspace_embeddings_batched", side_effect=batched),
        patch.object(pipeline, "_anythingllm_vector_cache_hit", return_value=False),
    ):
        pipeline.update_workspace_embeddings_desktop_queue(
            "http://fixture", "", "fixture", locations, batch_verifier=verifier,
            location_sources=[{"location": location, "source_path": f"source-{i}.pdf"}
                              for i, location in enumerate(locations)],
            _source_window_execution=True,
        )
    assert len(verifications) == (1 if sources == 2 else 0)

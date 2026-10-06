import hashlib
import inspect
import json

import pytest

import anythingllm_source_atomic_server as server
import anythingllm_source_atomic_v117 as v117
import auto_anythingllm_pipeline as pipeline
import rag_pdf_gradio_app as app

pytestmark = pytest.mark.offline_deterministic


def payload(source, page=1, segment=None):
    identity = f"{source}_p{page:04d}"
    if segment is not None:
        identity += f"_s{segment:05d}"
    return {
        "textContent": "A distinctive academic discussion of institutions, cultural identity and collective political action.",
        "metadata": {
            "title": f"{source} p{page}",
            "docSource": f"local-pdf://{source}",
            "chunkSource": f"segment://{identity}",
        },
    }


def test_qualified_v1161_generated_bytes_are_unchanged():
    fixture = ('"use strict";var UM={addDocuments:async function(s,e=[],t=null){'
               'let legacy=true;return {legacy:legacy}},removeDocuments:async function(){}};')
    assert hashlib.sha256(server.patch_v1161_server_source(fixture).encode()).hexdigest() == (
        "fa4624f70286f0463d440d49e392d7552d83604335b125ad42f1deba21b7fad7"  # pragma: allowlist secret - fixture digest
    )
    assert hashlib.sha256(server.SOURCE_ATOMIC_SERVER_BODY.encode()).hexdigest() == (
        "322daed1472858df809b59528129d0b8c340753d79e2bdc69097aac55efa14b7"  # pragma: allowlist secret - generated body digest
    )


def test_v117_chooses_native_policy_without_using_legacy_timeout_helper():
    body = v117.provider_staging_body()
    assert "retry_owner:\"desktop_sdk\"" in body
    assert "__sourceAtomicRecoveryAttemptTimeoutMs" not in body
    assert "__SOURCE_ATOMIC_" not in body


@pytest.mark.parametrize("limit", [1, 6, None, "invalid", -2])
def test_legacy_concurrency_arguments_cannot_enable_parallel_mutations(monkeypatch, limit):
    captured = {}

    def serial(*args, **kwargs):
        captured.update(kwargs)
        return {"serial": True}

    monkeypatch.setattr(pipeline, "_update_workspace_embeddings_batched_serial", serial)
    result = pipeline.update_workspace_embeddings_batched(
        "http://127.0.0.1:3001", "", "test", ["one", "two"],
        concurrent_batch_limit=limit, initial_concurrent_batches=6,
        submission_timeout_override=71, receipt_observer="observer",
    )
    assert result == {"serial": True}
    assert captured["submission_timeout_override"] == 71
    assert captured["receipt_observer"] == "observer"
    assert "ThreadPoolExecutor" not in inspect.getsource(pipeline.update_workspace_embeddings_batched)


def test_validation_workspace_keeps_explicit_api_settings_without_template_reads(monkeypatch):
    captured = {}
    monkeypatch.setattr(pipeline, "resolve_anythingllm_api_key", lambda *_: ("fixture", "provided"))
    monkeypatch.setattr(pipeline, "unique_lancedb_workspace_name", lambda *_, **__: ("Test", ""))

    def post(url, body, **kwargs):
        captured.update(url=url, body=body)
        return 200, json.dumps({"workspace": {"slug": "test", "name": "Test"}})

    monkeypatch.setattr(pipeline, "post_json", post)
    result = pipeline.create_validation_workspace(
        "http://127.0.0.1:3001", top_n=3, workspace_name="Test",
    )
    assert result["status"] == "created"
    assert captured["body"] == {"name": "Test", "chatMode": "query", "topN": 3}
    assert "workspace_template" not in result and "workspace_template_apply" not in result
    assert not hasattr(pipeline, "read_validation_workspace_template")


def test_unicode_workspace_name_keeps_visible_accents_with_safe_creation_slug(monkeypatch):
    calls = []
    monkeypatch.setattr(pipeline, "resolve_anythingllm_api_key", lambda *_: ("fixture", "provided"))

    def post(url, body, **_kwargs):
        calls.append((url, body))
        if url.endswith("/workspace/new"):
            return 200, json.dumps({"workspace": {"slug": "hernandezezaaco", "name": body["name"]}})
        return 200, json.dumps({"workspace": {"slug": "hernandezezaaco", "name": body["name"]}})

    monkeypatch.setattr(pipeline, "post_json", post)
    result = pipeline.create_validation_workspace(
        "http://127.0.0.1:3001", workspace_name="Herñañdézëzáàço",
    )
    assert result["status"] == "created"
    assert result["workspace_name"] == "Herñañdézëzáàço"
    assert result["workspace_slug"] == "hernandezezaaco"
    assert calls[0][1]["name"] == "Hernandezezaaco"
    assert calls[1][0].endswith("/workspace/hernandezezaaco/update")
    assert calls[1][1] == {"name": "Herñañdézëzáàço"}


def test_unicode_workspace_name_failed_update_removes_empty_workspace(monkeypatch):
    calls = []
    monkeypatch.setattr(pipeline, "resolve_anythingllm_api_key", lambda *_: ("fixture", "provided"))

    def post(url, body, **_kwargs):
        if url.endswith("/workspace/new"):
            return 200, json.dumps({"workspace": {"slug": "garcia", "name": body["name"]}})
        return 200, json.dumps({"workspace": {"slug": "garcia", "name": "Garcia"}})

    monkeypatch.setattr(pipeline, "post_json", post)
    monkeypatch.setattr(pipeline, "delete_json", lambda url, **_kwargs: calls.append(url) or (200, ""))
    result = pipeline.create_validation_workspace(
        "http://127.0.0.1:3001", workspace_name="García",
    )
    assert result["status"] == "workspace_display_name_update_failed"
    assert result["workspace_slug"] == ""
    assert calls == ["http://127.0.0.1:3001/api/v1/workspace/garcia"]


def test_unicode_workspace_collision_suffix_is_visible(monkeypatch):
    calls = []
    monkeypatch.setattr(pipeline, "resolve_anythingllm_api_key", lambda *_: ("fixture", "provided"))
    monkeypatch.setattr(pipeline, "unique_lancedb_workspace_name", lambda *_args, **_kwargs: ("Garcia 2", 2))

    def post(url, body, **_kwargs):
        calls.append(body["name"])
        return 200, json.dumps({"workspace": {"slug": "garcia-2", "name": body["name"]}})

    monkeypatch.setattr(pipeline, "post_json", post)
    result = pipeline.create_validation_workspace(
        "http://127.0.0.1:3001", workspace_name="García",
    )
    assert result["workspace_name"] == "García 2"
    assert calls == ["Garcia 2", "García 2"]


@pytest.mark.parametrize("with_source", [True, False])
def test_retrieval_sampling_keeps_equal_page_numbers_from_different_pdfs(with_source):
    candidates = [payload("first"), payload("second")]
    if not with_source:
        for item in candidates:
            item["metadata"].pop("docSource")
    assert len(pipeline.select_runtime_validation_payloads(candidates, limit=2)) == 2
    assert len(pipeline.select_runtime_validation_payloads(
        [payload("same", segment=1), payload("same", segment=2)], limit=2,
    )) == 1


def test_optional_payload_loader_uses_receipt_target_and_deduplicates_safe_paths(tmp_path, monkeypatch):
    documents = tmp_path / "documents"
    documents.mkdir()
    record = payload("sample")
    document = documents / "one.json"
    document.write_text(json.dumps({"pageContent": record["textContent"], **record["metadata"]}))
    report_path = tmp_path / "batch-native-upload-report.json"
    report_path.write_text(json.dumps({
        "status": "complete", "workspace_slug": "actual",
        "locations": ["one.json", "one.json", "../outside.json", None],
    }))
    (tmp_path / ".automatic-batch-upload-config.json").write_text(json.dumps({"workspace_slug": "old"}))
    (tmp_path / "outside.json").write_text(document.read_text())
    monkeypatch.setattr(app, "automatic_run_artifact_paths", lambda *_: [report_path])
    monkeypatch.setattr(app, "default_anythingllm_documents_dir", lambda: documents)
    root, selected = app._latest_workspace_runtime_payloads("actual")
    assert root == tmp_path and len(selected) == 1
    assert app._latest_workspace_runtime_payloads("unrelated") == (None, [])


def test_dotted_contents_cannot_dominate_body_window_or_stratified_sample():
    body = "The substantive academic argument concerns institutions, cultural identity and collective political action. " * 4
    noise = "." * 220 + " 7 9 IN T R O D"
    assert pipeline._runtime_validation_prose_score(noise) < pipeline._runtime_validation_prose_score(body[:240])
    candidate = payload("body")
    candidate["textContent"] = noise + " " + body
    before = candidate["textContent"]
    query = pipeline.runtime_validation_query_text(candidate)
    assert "substantive academic" in query
    assert candidate["textContent"] == before
    contents = payload("contents")
    contents["textContent"] = noise
    selected = pipeline.select_runtime_validation_payloads(
        [payload("first"), contents, payload("last")], limit=2,
    )
    assert len(selected) == 2 and contents not in selected


def test_optional_payload_loader_bounds_reads_across_entire_receipt(tmp_path, monkeypatch):
    documents = tmp_path / "documents"
    documents.mkdir()
    locations = []
    for index in range(7):
        record = payload(str(index))
        name = f"{index}.json"
        (documents / name).write_text(json.dumps({"pageContent": record["textContent"], **record["metadata"]}))
        locations.append(name)
    report = tmp_path / "batch-native-upload-report.json"
    report.write_text(json.dumps({"status": "complete", "workspace_slug": "test", "locations": locations}))
    monkeypatch.setattr(app, "automatic_run_artifact_paths", lambda *_: [report])
    monkeypatch.setattr(app, "default_anythingllm_documents_dir", lambda: documents)
    _, selected = app._latest_workspace_runtime_payloads("test", limit=3)
    assert [item["metadata"]["docSource"] for item in selected] == [
        "local-pdf://0", "local-pdf://3", "local-pdf://6",
    ]


@pytest.mark.parametrize("failure", [False, True])
def test_optional_button_remains_manual_bounded_and_retains_diagnostic_errors(tmp_path, monkeypatch, failure):
    captured = {}
    monkeypatch.setattr(app, "_latest_workspace_runtime_payloads", lambda *_: (tmp_path, [payload("one")]))

    def validate(*args, **kwargs):
        captured.update(kwargs)
        if failure:
            raise RuntimeError("private provider detail must not escape")
        return {"status": "pass", "vector_checks": [{"expected_in_top_n": True}], "chat_check": {}}

    monkeypatch.setattr(app, "validate_anythingllm_native_runtime", validate)
    html = app.optional_workspace_live_retrieval_check("http://127.0.0.1:3001", "", "test")
    assert captured["include_chat_probe"] is False
    assert captured["vector_max_attempts"] == 1
    assert captured["retry_timed_out_siblings"] is False
    report = app._read_automatic_run_json(tmp_path / "optional-live-retrieval-check.json")
    assert report["status"] == ("diagnostic_error" if failure else "pass")
    assert "private provider detail" not in html
    assert not (tmp_path / "run-summary.json").exists()


def test_optional_retrieval_miss_is_not_presented_as_missing_embeddings(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "_latest_workspace_runtime_payloads", lambda *_: (tmp_path, [payload("one")]))
    monkeypatch.setattr(app, "validate_anythingllm_native_runtime", lambda *_, **__: {
        "status": "vector_retrieval_failed", "vector_checks": [{"expected_in_top_n": False}],
    })
    result = app.optional_workspace_live_retrieval_check("http://127.0.0.1:3001", "", "test")
    assert "does not prove that its embeddings are missing" in result
    assert app._read_automatic_run_json(tmp_path / "optional-live-retrieval-check.json")["status"] == "vector_retrieval_failed"

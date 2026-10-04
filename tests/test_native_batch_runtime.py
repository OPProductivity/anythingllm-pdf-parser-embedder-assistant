import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import automatic_worker_protocol as protocol
import auto_anythingllm_pipeline as pipeline


pytestmark = pytest.mark.offline_deterministic


def manifest(count=2):
    return {
        "backend_mode": "Automatic", "unstructured_strategy": "auto",
        "runtime": {"status": "deferred_native_text_clear"},
        "files": [{"file": f"source-{i}.pdf", "risk": "native_text_likely",
                   "full_native_text_coverage": {"status": "verified"},
                   "targeted_visual_text_pages": []} for i in range(count)],
    }


@pytest.mark.parametrize("count", [2, 3, 8])
def test_two_is_minimum_and_input_unchanged(count):
    preflight = manifest(count)
    original = copy.deepcopy(preflight)
    assert protocol.can_share_native_batch_runtime(
        [Path(row["file"]) for row in preflight["files"]], preflight, "Automatic", "auto")
    assert preflight == original


@pytest.mark.parametrize("case", ["single", "missing", "stale_files", "coverage", "scan", "targeted",
                                  "ready", "unavailable", "probe_failed", "explicit_backend", "explicit_strategy", "deep"])
def test_noneligible_cases_stay_on_existing_path(case):
    preflight = manifest()
    files, backend, strategy = ["source-0.pdf", "source-1.pdf"], "Automatic", "auto"
    if case == "single":
        files = files[:1]
    elif case == "missing":
        preflight = None
    elif case == "stale_files":
        files = ["other.pdf", "source-1.pdf"]
    elif case == "coverage":
        preflight["files"][0]["full_native_text_coverage"]["status"] = "failed"
    elif case == "scan":
        preflight["files"][0]["risk"] = "likely"
    elif case == "targeted":
        preflight["files"][0]["targeted_visual_text_pages"] = [2]
    elif case in {"ready", "unavailable", "probe_failed"}:
        preflight["runtime"]["status"] = case
    elif case == "explicit_backend":
        backend = "Unstructured"
    elif case == "explicit_strategy":
        strategy = "ocr_only"
    assert not protocol.can_share_native_batch_runtime(
        files, preflight, backend, strategy, deep_extraction=case == "deep")


@pytest.mark.parametrize("backend,tesseract", [(True, True), (False, True), (True, False), (False, False)])
def test_real_observation_is_shared_without_ocr_selection(backend, tesseract):
    runtime = {"backend_available": backend, "tesseract_available": tesseract, "ocr_required": False,
               "backend_import_error": "missing" if not backend else "", "tesseract_executable": "fixture"}
    original = copy.deepcopy(runtime)
    context = {"share_native_runtime_probe": True}
    protocol.remember_native_batch_runtime(context, runtime)
    assert context["unstructured_runtime_probe"] == {k: v for k, v in original.items() if k != "ocr_required"}
    assert runtime == original
    context = json.loads(json.dumps(context))
    protocol.remember_native_batch_runtime(context, {**runtime, "backend_available": not backend})
    assert context["unstructured_runtime_probe"]["backend_available"] is backend


@pytest.mark.parametrize("runtime", [{}, None, {"backend_available": True},
                                      {"backend_available": "ready", "tesseract_available": True}])
def test_incomplete_observation_is_not_cached(runtime):
    context = {"share_native_runtime_probe": True}
    protocol.remember_native_batch_runtime(context, runtime)
    assert "unstructured_runtime_probe" not in context


def test_without_eligibility_marker_no_new_cache():
    context = {}
    protocol.remember_native_batch_runtime(context, {"backend_available": True, "tesseract_available": True})
    assert context == {}


def test_capabilities_do_not_freeze_ocr_strategy():
    context = {"share_native_runtime_probe": True}
    native = pipeline.resolve_unstructured_strategy("auto", runtime_probe={"backend_available": True,
                                                                         "tesseract_available": True})
    protocol.remember_native_batch_runtime(context, native["runtime"])
    hard = pipeline.resolve_unstructured_strategy(
        "auto", runtime_probe=context["unstructured_runtime_probe"],
        prior_candidates=[{"quality": {"scanned_likelihood": "high"}}])
    assert native["resolved"] == "fast" and native["runtime"]["ocr_required"] is False
    assert hard["resolved"] == "hi_res" and hard["runtime"]["ocr_required"] is True
    assert "ocr_required" not in context["unstructured_runtime_probe"]


def test_worker_json_handoff_keeps_snapshot_and_no_credentials():
    context = {"share_native_runtime_probe": True,
               "unstructured_runtime_probe": {"backend_available": True, "tesseract_available": True}}
    args = SimpleNamespace(batch_inspection_context=context, anythingllm_api_key="test-key")  # pragma: allowlist secret
    values, key = protocol.serializable_automatic_worker_arguments(args)
    assert values == {"batch_inspection_context": context}
    assert key == "test-key" and "test-key" not in json.dumps(values)  # pragma: allowlist secret


def test_first_inspection_initialization_preserves_snapshot():
    context = {"share_native_runtime_probe": True,
               "unstructured_runtime_probe": {"backend_available": True, "tesseract_available": True}}
    result, reused = pipeline.get_batch_inspection_context(
        SimpleNamespace(batch_inspection_context=context, anythingllm_api_url=""), Path("test-storage"), "")
    assert reused is False and result["share_native_runtime_probe"] is True
    assert result["unstructured_runtime_probe"]["backend_available"] is True


def test_each_shared_worker_restores_ocr_environment_without_mutating_snapshot():
    context = {"share_native_runtime_probe": True, "unstructured_runtime_probe": {
        "backend_available": True, "tesseract_available": True, "tesseract_executable": "old-path"}}
    original = copy.deepcopy(context)
    calls = []

    def setup():
        calls.append("local environment setup")
        return {"available": True, "executable": "current-path", "tessdata_prefix": "language-data"}

    first = protocol.native_batch_runtime_probe(context, setup)
    second = protocol.native_batch_runtime_probe(context, setup)
    assert len(calls) == 2 and first == second
    assert first["tesseract_executable"] == "current-path" and first["tessdata_prefix"] == "language-data"
    assert context == original


def test_disappearing_tesseract_is_not_reported_as_available():
    context = {"share_native_runtime_probe": True, "unstructured_runtime_probe": {
        "backend_available": True, "tesseract_available": True}}
    runtime = protocol.native_batch_runtime_probe(context, lambda: {"available": False})
    assert runtime["tesseract_available"] is False and runtime["tesseract_executable"] == ""


def test_legacy_or_absent_context_does_not_add_environment_checks():
    def forbidden():
        pytest.fail("Unchanged routes must not acquire another check")

    runtime = {"backend_available": True, "tesseract_available": True}
    assert protocol.native_batch_runtime_probe({"unstructured_runtime_probe": runtime}, forbidden) == runtime
    assert protocol.native_batch_runtime_probe({}, forbidden) is None


def test_unrelated_inspection_invalidation_keeps_previous_behavior():
    context = {"unstructured_runtime_probe": {"backend_available": True}}
    result, _ = pipeline.get_batch_inspection_context(
        SimpleNamespace(batch_inspection_context=context, anythingllm_api_url=""), Path("test-storage"), "")
    assert "unstructured_runtime_probe" not in result


def test_new_run_does_not_inherit_previous_capabilities():
    completed = {"share_native_runtime_probe": True}
    protocol.remember_native_batch_runtime(completed, {"backend_available": True, "tesseract_available": True})
    next_run = {"share_native_runtime_probe": True}
    assert protocol.native_batch_runtime_probe(next_run, lambda: pytest.fail("No snapshot yet")) is None
    assert "unstructured_runtime_probe" not in next_run

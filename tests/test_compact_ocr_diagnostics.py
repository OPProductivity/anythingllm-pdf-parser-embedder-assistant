"""Logging only: preserve calls/output, all-region accounting, bounded rollup."""

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import auto_anythingllm_pipeline as a
import rag_pdf_tools as t
import rag_pdf_gradio_app as g

pytestmark = pytest.mark.offline_deterministic


def ledger(regions):
    return a.ocr_page_evidence_ledger(
        [{"page": 1, "text": "Actual source words", "reading_regions": regions}],
        {"status": "not_available"},
        [1],
    )


def test_later_region_retry_is_not_hidden_by_first_region():
    rows = [
        {
            "ocr_crop_retry": {
                "attempted": False,
                "reason": "embedded_crop_recovery_sufficient",
            }
        },
        {"ocr_crop_retry": {"attempted": True, "reason": "full_page_retry_selected"}},
        {"ocr_crop_retry": {"attempted": True, "reason": "embedded_crop_retained"}},
    ]
    result = ledger(rows)
    page = result["pages"][0]
    assert page["crop_retry_attempted"]
    assert page["crop_retry_result"] == "multiple_region_results"
    assert page["crop_retry_region_counts"]["attempted"] == 2
    assert page["crop_retry_region_counts"]["selected"] == 1
    report = a.summarize_ocr_run_evidence([{"ocr_page_evidence": result}])
    assert report["recovery"]["crop_retry_regions_attempted"] == 2
    assert report["coverage"]["selected_ocr_output"] == 1  # not three PDFs/pages


def test_missing_retry_evidence_is_not_a_successful_nonretry():
    page = ledger([{}])["pages"][0]
    assert page["crop_retry_result"] == "not_recorded"
    assert page["crop_retry_region_counts"]["assessed"] == 0
    assert page["crop_retry_region_counts"]["not_recorded"] == 1


def test_coverage_states_distinguish_absent_partial_and_invalid_measurements():
    rows = [ledger(regions)["pages"][0] for regions in [
        [], [{}], [{"recognition_layout": {"subprocess_seconds": -1}}],
        [{"recognition_layout": {"subprocess_seconds": 0}}],
        [{"recognition_layout": {"subprocess_seconds": 1}},
         {"recognition_layout": {"model": "installed_eng"}}],
    ]]
    report = a.summarize_ocr_run_evidence([{"ocr_page_evidence": {
        "schema_version": 2, "pages": rows,
    }}])
    assert report["measurement_page_states"] == {
        "no_retained_region_evidence": 1,
        "regions_without_recognition_profiles": 1,
        "profiles_without_valid_timing": 1,
        "all_retained_profiles_timed": 1,
        "some_retained_profiles_timed": 1,
    }
    assert report["coverage"]["pages_without_retained_call_measurements"] == 3
    assert report["assessment_page_states"]["reading_order"] == {"not_recorded": 5}
    assert "unknown_not_ineligible" in report["coverage_interpretation"]


def test_unavailable_drop_cap_layout_is_reported_not_silently_successful():
    result = ledger([{"drop_cap_recovery": {"reason": "layout_words_unavailable"}}])
    report = a.summarize_ocr_run_evidence([{"ocr_page_evidence": result}])
    assert report["drop_cap_assessment_reasons"] == {"layout_words_unavailable": 1}
    assert report["recovery"]["drop_caps_recovered"] == 0


@pytest.mark.parametrize(
    "failure,outcome",
    [
        (subprocess.TimeoutExpired("tesseract", 90), "timeout"),
        (OSError("unavailable"), "launch_error"),
    ],
)
def test_measurement_does_not_swallow_or_retry_errors(failure, outcome):
    evidence = {}
    with patch.object(t.subprocess, "run", side_effect=failure) as call:
        with pytest.raises(type(failure)):
            t._run_measured_tesseract(["tesseract"], evidence)
    assert call.call_count == 1
    assert evidence["subprocess_outcome"] == outcome
    assert evidence["subprocess_seconds"] >= 0


@pytest.mark.parametrize("code,outcome", [(0, "exit_ok"), (1, "nonzero_exit")])
def test_measured_call_preserves_command_arguments_and_result(code, outcome):
    expected = SimpleNamespace(returncode=code, stdout="unchanged")
    evidence = {}
    with patch.object(t.subprocess, "run", return_value=expected) as call:
        assert (
            t._run_measured_tesseract(["tesseract", "page.png"], evidence) is expected
        )
    call.assert_called_once_with(
        ["tesseract", "page.png"],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert evidence["subprocess_outcome"] == outcome


def test_aggregation_does_not_mutate_evidence_or_grow_with_page_count():
    page = ledger(
        [
            {
                "recognition_layout": {
                    "model": "installed_eng",
                    "psm": 4,
                    "route_reason": "ordinary",
                    "setup_seconds": 0.1,
                    "subprocess_seconds": 0.8,
                    "subprocess_outcome": "exit_ok",
                }
            }
        ]
    )["pages"][0]
    summaries = [{"ocr_page_evidence": {"schema_version": 2, "pages": [page] * 1000}}]
    before = json.dumps(summaries)
    result = a.summarize_ocr_run_evidence(summaries)
    assert json.dumps(summaries) == before
    assert result["measured_seconds"]["tesseract_subprocess"] == 800
    assert len(json.dumps(result)) < 2500
    assert "Actual source words" not in json.dumps(result)
    assert "not_accuracy" in result["scope"]
    assert "not_run_wall_time" in result["measured_seconds"]["scope"]


def test_parallel_measurements_have_no_shared_collector():
    def run(index):
        e = {}
        t._run_measured_tesseract([str(index)], e)
        return e

    with patch.object(
        t.subprocess,
        "run",
        side_effect=lambda command, **kw: SimpleNamespace(
            returncode=int(command[0]) % 2
        ),
    ):
        with ThreadPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(run, range(12)))
    assert [r["subprocess_outcome"] for r in rows] == ["exit_ok", "nonzero_exit"] * 6


@pytest.mark.parametrize("broken", [False, True])
def test_terminal_only_rollup_cannot_change_cancellation_or_history(tmp_path, broken):
    run = tmp_path / "run"
    history = tmp_path / "history.jsonl"
    with (
        patch.object(g, "AUTO_OUTPUT_DIR", tmp_path),
        patch.object(g, "INGESTION_HISTORY_PATH", history),
        patch.object(g, "prune_background_jsonl"),
        patch.object(
            g,
            "summarize_ocr_run_evidence",
            side_effect=RuntimeError("diagnostic only") if broken else None,
            return_value={"schema_version": 1},
        ),
    ):
        g.append_ingestion_history(
            run, [], {"state": "cancelled", "message": "Cancelled"}, False, ""
        )
    terminal = json.loads((run / "ingestion-terminal-record.json").read_text())
    historical = json.loads(history.read_text())
    assert terminal["state"] == historical["state"] == "cancelled"
    assert "ocr_diagnostics" in terminal
    assert "ocr_diagnostics" not in historical
    assert sorted(p.name for p in run.iterdir()) == ["ingestion-terminal-record.json"]
    if broken:
        assert terminal["ocr_diagnostics"]["status"] == "unavailable"

import copy

import pytest
import rag_pdf_gradio_app as app

pytestmark = pytest.mark.offline_deterministic


def example(unique=2, copies=1):
    return {
        "mode": app.MODE_LOCAL_ONLY_LABEL, "embedding_engine": "disabled",
        "state": "successful", "duration_provenance": "active_observation_window",
        "document_count": unique + copies, "page_count": (unique + copies) * 10,
        "actual_seconds": 90, "actual_records": unique,
        "selected_input_duplicate_documents": copies,
        "unique_processed_documents": unique, "unique_processed_pages": unique * 10,
        "document_timing": [dict(pages=10, records=1, total_pipeline_seconds=3,
                                 selected_input_duplicate=False) for _ in range(unique)]
        + [dict(pages=10, records=0, total_pipeline_seconds=0,
                selected_input_duplicate=True) for _ in range(copies)],
    }


@pytest.mark.parametrize("unique,copies", [(1, 1), (42, 1), (10, 10), (300, 20)])
def test_exact_unique_workload_changes_only_learning_view(unique, copies):
    row = example(unique, copies)
    before = copy.deepcopy(row)
    view = app.timing_model_unique_local_workload(row)
    assert view["document_count"] == unique
    assert view["page_count"] == unique * 10
    assert view["estimated_records"] == unique
    assert view["actual_seconds"] == row["actual_seconds"]
    assert not app.timing_model_has_selected_duplicates(view)
    assert app.timing_model_has_selected_duplicates(row)
    assert row == before
    assert app.timing_model_unique_local_workload(view) is view


@pytest.mark.parametrize("field,value", [
    ("unique_processed_pages", 19), ("unique_processed_documents", 1),
    ("document_count", 4), ("actual_records", 3),
    ("selected_input_duplicate_documents", 2), ("state", "cancelled"),
    ("duration_provenance", "wall_clock"), ("embedding_engine", "ollama"),
    ("mode", app.MODE_NATIVE_UPLOAD_LABEL), ("unique_processed_pages", "bad"),
])
def test_ambiguous_or_other_lanes_stay_excluded(field, value):
    row = example()
    row[field] = value
    assert app.timing_model_unique_local_workload(row) is row


@pytest.mark.parametrize("value", [0, float("nan"), float("inf"), "bad"])
def test_missing_or_invalid_measured_processing_stays_excluded(value):
    row = example()
    row["document_timing"][0]["total_pipeline_seconds"] = value
    assert app.timing_model_unique_local_workload(row) is row


def test_false_copy_with_records_stays_excluded():
    row = example()
    row["document_timing"][-1]["records"] = 1
    assert app.timing_model_unique_local_workload(row) is row


def test_local_diagnostics_supported_and_normal_runs_unchanged():
    row = example()
    row["mode"] = app.MODE_LOCAL_WITH_LOGS_LABEL
    assert app.timing_model_unique_local_workload(row) is not row
    normal = example(copies=0)
    assert app.timing_model_unique_local_workload(normal) is normal

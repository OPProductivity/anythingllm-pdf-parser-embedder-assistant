"""Deterministic tests for the shared native validation contract."""

import pytest

from validation_contract import (
    condition_satisfies_live_contract,
    evidence_layers_succeeded,
    post_upload_status_class,
    validation_report_succeeded,
)


pytestmark = pytest.mark.offline_deterministic


def test_evidence_layers_require_upload_post_upload_and_runtime_success():
    assert evidence_layers_succeeded("complete", "pass", "pass") is True
    assert evidence_layers_succeeded("complete", "pass", "pass_with_chat_timeout") is False
    assert evidence_layers_succeeded("complete", "pass", "pass_with_vector_timeout") is False
    assert evidence_layers_succeeded("error", "pass", "pass") is False
    assert evidence_layers_succeeded("complete", "partial_vector_coverage", "pass") is False
    assert evidence_layers_succeeded("complete", "pass", "timeout") is False


def test_validation_report_does_not_trust_complete_flag_over_failed_layer():
    validation = {
        "status": "complete",
        "upload_status": "error",
        "post_upload_status": "pass",
        "runtime_validation_status": "pass",
    }
    assert validation_report_succeeded(validation) is False


def test_post_upload_status_class_is_shared_and_keeps_concurrent_writes_retryable():
    assert post_upload_status_class("pass") == "pass"
    assert post_upload_status_class("review") == "review"
    assert post_upload_status_class("partial_vector_coverage") == "error"
    assert post_upload_status_class(
        "concurrent_write_ambiguous", concurrent_writes_are_transient=True
    ) == "incomplete"


def test_condition_contract_reconciles_all_counts():
    condition = {
        "status": "complete",
        "preparation": {"payload_count": 8},
        "validation": {
            "status": "complete",
            "upload_status": "complete",
            "post_upload_status": "pass",
            "runtime_validation_status": "pass",
            "upload_report": {"embedded": 8},
        },
        "native_observation": {"after_runtime_validation": {"lancedb_vector_count": 8}},
    }
    assert condition_satisfies_live_contract(condition) is True
    condition["validation"]["upload_report"]["embedded"] = 7
    assert condition_satisfies_live_contract(condition) is False

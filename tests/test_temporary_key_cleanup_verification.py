import sqlite3
from unittest import mock

import pytest

import auto_anythingllm_pipeline as pipeline


pytestmark = pytest.mark.offline_deterministic


def test_key_absence_observation_reads_only_the_bound_desktop_store(tmp_path):
    with sqlite3.connect(tmp_path / "anythingllm.db") as connection:
        connection.execute("create table api_keys(id integer primary key)")
        connection.execute("insert into api_keys(id) values (7)")
    with (
        mock.patch.object(pipeline, "default_anythingllm_storage_dir", return_value=tmp_path),
        mock.patch.object(pipeline, "desktop_listener", return_value=True),
    ):
        assert pipeline.observe_temporary_desktop_key_absence(
            "http://127.0.0.1:3001", 7
        )["status"] == "present"
        assert pipeline.observe_temporary_desktop_key_absence(
            "http://127.0.0.1:3001", 8
        )["status"] == "absent"


def test_http_success_without_physical_absence_is_cleanup_failure():
    with (
        mock.patch.object(
            pipeline, "resolve_anythingllm_api_key", return_value=("management", "provided")
        ),
        mock.patch.object(pipeline, "delete_json", return_value=(200, "{}")),
        mock.patch.object(
            pipeline,
            "observe_temporary_desktop_key_absence",
            return_value={"status": "present"},
        ),
    ):
        result = pipeline.delete_temporary_desktop_api_key(
            "http://127.0.0.1:3001", 7, "temporary"
        )
    assert result["status"] == "delete_failed"
    assert result["http_status"] == 200
    assert result["absence_verification"] == {"status": "present"}


def test_http_success_and_verified_absence_is_cleanup_success():
    with (
        mock.patch.object(
            pipeline, "resolve_anythingllm_api_key", return_value=("management", "provided")
        ),
        mock.patch.object(pipeline, "delete_json", return_value=(200, "{}")),
        mock.patch.object(
            pipeline,
            "observe_temporary_desktop_key_absence",
            return_value={"status": "absent"},
        ),
    ):
        result = pipeline.delete_temporary_desktop_api_key(
            "http://127.0.0.1:3001", 7, "temporary"
        )
    assert result["status"] == "deleted"
    assert result["error"] == ""

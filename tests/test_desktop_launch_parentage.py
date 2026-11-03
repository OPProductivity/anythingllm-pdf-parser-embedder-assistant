"""Desktop startup must outlive a routine assistant server tree stop."""

import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import auto_anythingllm_pipeline as pipeline


pytestmark = pytest.mark.offline_deterministic


@pytest.mark.skipif(os.name != "nt", reason="Windows Desktop launch contract")
def test_desktop_launch_uses_short_lived_broker_not_server_child(monkeypatch, tmp_path):
    executable = tmp_path / "AnythingLLM.exe"
    monkeypatch.setattr(pipeline, "anythingllm_desktop_process_running", lambda: False)
    broker = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(pipeline.subprocess, "run", broker)
    direct_launch = Mock()
    monkeypatch.setattr(pipeline.subprocess, "Popen", direct_launch)

    result = pipeline.start_anythingllm_desktop(executable)

    assert result["status"] == "started"
    assert broker.call_args.args[0] == ["cmd.exe", "/c", "start", "", "/b", str(executable)]
    assert broker.call_args.kwargs["timeout"] <= 3.0
    direct_launch.assert_not_called()


@pytest.mark.skipif(os.name != "nt", reason="Windows Desktop launch contract")
def test_desktop_launch_broker_failure_is_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "anythingllm_desktop_process_running", lambda: False)
    monkeypatch.setattr(
        pipeline.subprocess, "run",
        Mock(return_value=SimpleNamespace(returncode=1)),
    )

    result = pipeline.start_anythingllm_desktop(tmp_path / "AnythingLLM.exe")

    assert result["status"] == "start_failed"
    assert result["started"] is False
    assert "broker exited with code 1" in result["error"]

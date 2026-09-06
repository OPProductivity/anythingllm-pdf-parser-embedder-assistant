from types import SimpleNamespace
from unittest.mock import patch

import psutil
import pytest
import anythingllm_pdf_assistant_cli as cli
import rag_pdf_gradio_app as app

pytestmark = [pytest.mark.offline_deterministic,
              pytest.mark.skipif(app.os.name != 'nt', reason='Windows bridge probe')]


@pytest.mark.parametrize('output,code,expected', [
    ('"AnythingLLM.exe","1234","Console","1","500 K"', 0, False),
    ('"AnythingLLM.exe","123","Console","1","500 K"', 0, True),
    ('"AnythingLLM.exe","123","Console","1","500 K"', 1, None),
    ('INFO: No tasks are running which match the specified criteria.', 0, False),
])
def test_fallback_requires_exact_pid_and_success(output, code, expected):
    with (patch.object(cli, '_native_process_api', return_value=None),
          patch.object(app.subprocess, 'run', return_value=SimpleNamespace(stdout=output, returncode=code))):
        assert app.desktop_bridge_process_is_live(123) is expected


def test_native_bridge_probe_does_not_launch_tasklist():
    with (patch.object(cli, '_native_process_api', return_value=psutil),
          patch.object(app.subprocess, 'run', side_effect=AssertionError('no tasklist'))):
        assert app.desktop_bridge_process_is_live(app.os.getpid()) is True
        assert app.desktop_bridge_process_is_live(-1) is False
        assert app.desktop_bridge_process_is_live('invalid') is False


def test_denied_native_bridge_read_does_not_fall_back():
    with (patch.object(cli, '_native_process_api', return_value=psutil),
          patch.object(psutil, 'Process', side_effect=psutil.AccessDenied(123)),
          patch.object(app.subprocess, 'run', side_effect=AssertionError('no fallback'))):
        assert app.desktop_bridge_process_is_live(123) is None


def test_unverified_bridge_is_not_marked_stale(tmp_path):
    import json
    path = tmp_path / 'bridge.json'
    path.write_text(json.dumps({'marker':app.DESKTOP_REFRESH_BRIDGE_MARKER,
        'schemaVersion':1,'token':'a'*43,'port':54321,'pid':123}))
    with (patch.object(app, 'desktop_refresh_bridge_descriptor_path', return_value=path),
          patch.object(app, 'desktop_bridge_process_is_live', return_value=None),
          patch.object(app.urllib.request, 'urlopen', side_effect=AssertionError('no refresh'))):
        report = app.read_desktop_refresh_bridge_descriptor()
        assert report['status'] == 'process_verification_unavailable'
        assert not report['available'] and 'stale_descriptor' not in report
        assert 'does not mean Desktop has stopped' in app.desktop_workspace_refresh_note(report)


def test_missing_bridge_creation_evidence_is_unknown():
    with patch.object(psutil.Process, 'create_time', return_value=None):
        assert app.desktop_bridge_process_is_live(app.os.getpid()) is None

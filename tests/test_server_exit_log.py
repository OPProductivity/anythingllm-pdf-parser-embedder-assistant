import json
import os
from pathlib import Path
import subprocess
import sys
from contextlib import nullcontext
from types import SimpleNamespace
from unittest import mock

import pytest

import anythingllm_pdf_assistant_cli as cli
import server_exit_log as evidence

pytestmark = pytest.mark.offline_deterministic


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv('ANYTHINGLLM_PDF_ASSISTANT_HOME', str(tmp_path))


def events(tmp_path):
    path = tmp_path / 'logs' / 'server-lifecycle.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_only_safe_identity_and_exception_location_are_retained(tmp_path):
    try:
        raise ValueError('api_key=private-secret plus document content')
    except ValueError as exc:
        evidence.record_server_event('server_failed', error=exc, record={
            'pid': 12, 'port': 7860, 'started_at': 123,
            'stop_notification_token': 'do-not-log', 'command': 'private command',
        }, exit_code=1)
    row = events(tmp_path)[0]
    assert row['exception_type'] == 'ValueError'
    assert row['frames'][-1]['function'] == 'test_only_safe_identity_and_exception_location_are_retained'
    assert row['server'] == {'pid': 12, 'port': 7860, 'started_at': 123}
    assert 'private' not in json.dumps(row) and 'do-not-log' not in json.dumps(row)


def test_rotation_is_small_and_has_one_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(evidence, 'MAX_BYTES', 450)
    for i in range(20):
        evidence.record_server_event('test_event', sequence=i)
    paths = list((tmp_path / 'logs').iterdir())
    assert len(paths) == 2 and all(p.stat().st_size <= 450 for p in paths)
    assert events(tmp_path)[-1]['sequence'] == 19


@pytest.mark.parametrize('error', [PermissionError('locked'), TimeoutError('lock busy')])
def test_logging_failure_is_nonfatal(error):
    with mock.patch.object(evidence, 'named_process_lock', side_effect=error):
        evidence.record_server_event('server_starting')


def test_unavailable_log_directory_is_nonfatal(tmp_path):
    (tmp_path / 'logs').write_text('not a directory')
    evidence.record_server_event('server_starting')


@pytest.mark.parametrize('failure,expected,code', [
    (None, 'server_returned', 0),
    (RuntimeError('test failure'), 'server_failed', 1),
    (KeyboardInterrupt(), 'server_interrupted', None),
    (SystemExit(7), 'server_system_exit', 7),
    (SystemExit(None), 'server_system_exit', 0),
])
def test_cli_exit_classification_preserves_behavior(tmp_path, failure, expected, code):
    app = SimpleNamespace(launch_application=mock.Mock(side_effect=failure))
    marker = tmp_path / 'config' / 'localhost-server.json'
    with (mock.patch.object(cli, '_server_start_ownership_lock', return_value=nullcontext()),
          mock.patch.object(cli, '_recorded_server_is_alive_on_port', return_value=False),
          mock.patch.object(cli, '_doctor', return_value=0),
          mock.patch.object(cli, '_write_server_marker', return_value=marker),
          mock.patch.object(cli, '_server_marker_for_diagnostics', return_value={}),
          mock.patch.object(cli, '_remove_own_server_marker') as remove,
          mock.patch.dict(sys.modules, {'rag_pdf_gradio_app': app})):
        if isinstance(failure, (KeyboardInterrupt, SystemExit)):
            with pytest.raises(type(failure)):
                cli._start(7860, False)
        else:
            assert cli._start(7860, False) == code
    assert events(tmp_path)[-1]['event'] == expected
    assert events(tmp_path)[-1].get('exit_code') == code
    remove.assert_called_once_with(marker)


def test_old_marker_is_discovery_not_invented_exit_reason(tmp_path):
    old = {'pid': 99, 'port': 7860, 'started_at': 100}
    with (mock.patch.object(cli, '_server_start_ownership_lock', return_value=nullcontext()),
          mock.patch.object(cli, '_recorded_server_is_alive_on_port', return_value=False),
          mock.patch.object(cli, '_doctor', return_value=0),
          mock.patch.object(cli, '_write_server_marker', return_value=tmp_path / 'marker'),
          mock.patch.object(cli, '_server_marker_for_diagnostics', side_effect=[old, {}]),
          mock.patch.object(cli, '_remove_own_server_marker'),
          mock.patch.dict(sys.modules, {'rag_pdf_gradio_app': SimpleNamespace(launch_application=lambda **kw: None)})):
        assert cli._start(7860, False) == 0
    row = events(tmp_path)[0]
    assert row['event'] == 'previous_server_marker_found'
    assert row['previous_exit_code'] is None
    assert row['server']['started_at'] == 100
    assert 'exit_time' not in row


def test_start_log_sits_after_owned_claim_and_stop_log_before_kill(tmp_path):
    record = {'pid': 123, 'port': 7860}
    marker = tmp_path / 'marker'
    marker.write_text(json.dumps(record))
    def terminate(*args, **kwargs):
        assert events(tmp_path)[-1]['event'] == 'intentional_stop_requested'
        return SimpleNamespace(returncode=0)
    with (mock.patch.object(cli, '_listener_belongs_to_server_root', return_value=True),
          mock.patch.object(cli, '_prepare_owned_active_runs_for_server_stop', return_value=[]),
          mock.patch.object(cli, '_notify_browser_stop'),
          mock.patch.object(cli.subprocess, 'run', side_effect=terminate),
          mock.patch.object(cli, '_finalize_owned_runs_after_server_stop'),
          mock.patch.object(cli, '_port_is_available', return_value=True)):
        assert cli._stop_pinned_server(marker, record, 123, 7860) == 0
    row = events(tmp_path)[-1]
    assert row['event'] == 'intentional_stop_completed'
    assert row['termination_command_exit_code'] == 0 and 'exit_code' not in row


def test_independent_process_writes_remain_valid_json(tmp_path):
    env = dict(os.environ, ANYTHINGLLM_PDF_ASSISTANT_HOME=str(tmp_path))
    source = 'from server_exit_log import record_server_event; [record_server_event("probe", n=i) for i in range(12)]'
    children = [subprocess.Popen([sys.executable, '-c', source], cwd=Path(cli.__file__).parent, env=env,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                for _ in range(4)]
    assert all(child.wait(timeout=20) == 0 for child in children)
    assert len(events(tmp_path)) == 48


def test_abrupt_exit_does_not_fabricate_terminal_event(tmp_path):
    source = 'import os; from server_exit_log import record_server_event; record_server_event("server_starting"); os._exit(23)'
    child = subprocess.run([sys.executable, '-c', source], cwd=Path(cli.__file__).parent,
                           env=dict(os.environ, ANYTHINGLLM_PDF_ASSISTANT_HOME=str(tmp_path)),
                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0, timeout=20)
    assert child.returncode == 23
    assert [row['event'] for row in events(tmp_path)] == ['server_starting']

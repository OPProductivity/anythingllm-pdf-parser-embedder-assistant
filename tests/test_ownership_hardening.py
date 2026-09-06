import json
import os
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import psutil
import pytest
import anythingllm_pdf_assistant_cli as cli

pytestmark = [pytest.mark.offline_deterministic,
              pytest.mark.skipif(sys.platform != 'win32', reason='Windows ownership')]


def test_missing_creation_evidence_never_authorizes():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        sock.listen()
        with patch.object(psutil.Process, 'create_time', side_effect=psutil.AccessDenied(os.getpid())):
            with pytest.raises(cli.ServerOwnershipProbeError):
                cli._native_listener_belongs_to_root(psutil, sock.getsockname()[1], os.getpid())
            assert cli._owned_server_process_command(os.getpid()) is None


def test_suppressed_constructor_error_requires_independent_creation_evidence():
    # psutil constructor catches this error itself. A public creation-time
    # lookup must still succeed; comparing two (PID,None) tuples is forbidden.
    with patch.object(psutil.Process, '_get_ident', side_effect=psutil.AccessDenied(os.getpid())):
        p = psutil.Process(os.getpid())
        assert p._ident[1] is None
        assert cli._required_process_identity(p)[1] > 0
        with patch.object(psutil.Process, 'create_time', return_value=None):
            with pytest.raises(cli.ServerOwnershipProbeError):
                cli._required_process_identity(p)


@pytest.mark.parametrize('native', [True, False])
def test_dualstack_listener_is_not_treated_as_absent(native):
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        sock.bind(('::', 0))
        sock.listen()
        port = sock.getsockname()[1]
        with socket.create_connection(('127.0.0.1', port), timeout=2):
            pass
        with patch.object(cli, '_native_process_api', return_value=psutil if native else None):
            assert cli._listener_belongs_to_server_root(port, os.getpid()) is True


def test_timeout_does_not_accumulate_threads_or_reuse_late_result():
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    def stalled():
        entered.set()
        release.wait(5)
        returned.set()
        return True
    try:
        with pytest.raises(cli.ServerOwnershipProbeError, match='timed out'):
            cli._bounded_native_read(stalled, .03)
        assert entered.is_set()
        for _ in range(10):
            with pytest.raises(cli.ServerOwnershipProbeError, match='pending'):
                cli._bounded_native_read(lambda: pytest.fail('must not launch'), .03)
    finally:
        release.set()
        assert returned.wait(2)
    deadline = time.monotonic() + 2
    while cli._NATIVE_READ_SLOT.locked() and time.monotonic() < deadline:
        time.sleep(.005)
    assert cli._bounded_native_read(lambda: False) is False


def test_stop_rejects_wrong_incarnation_before_recovery_or_kill(tmp_path):
    with cli._pinned_server_process(os.getpid()) as ticks:
        pass
    marker = tmp_path / 'server.json'
    marker.write_text(json.dumps({'pid':os.getpid(),'port':54321,'process_creation_ticks':str(ticks+1)}))
    with (patch.object(cli, '_server_marker_path', return_value=marker),
          patch.object(cli, '_owned_server_process_command', return_value='python -m anythingllm_pdf_assistant_cli start'),
          patch.object(cli, '_prepare_owned_active_runs_for_server_stop') as prepare,
          patch.object(cli.subprocess, 'run') as kill):
        assert cli._stop() == 1
        prepare.assert_not_called()
        kill.assert_not_called()
    assert marker.exists()


def test_stop_waits_for_start_ownership_mutex_before_entering(tmp_path):
    marker = tmp_path / 'server.json'
    marker.write_text(json.dumps({'pid':123,'port':54323}))
    started, entered = threading.Event(), threading.Event()
    result = []
    def invoke():
        started.set()
        result.append(cli._stop())
    with (patch.object(cli, '_server_marker_path', return_value=marker),
          patch.object(cli, '_stop_under_start_lock', side_effect=lambda: entered.set() or 0)):
        with cli._server_start_ownership_lock(54323):
            t = threading.Thread(target=invoke, daemon=True)
            t.start()
            assert started.wait(2)
            assert not entered.wait(.1)
        t.join(timeout=3)
    assert entered.is_set() and result == [0]


def test_retained_handle_survives_exit_and_rejects_wrong_creation():
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    try:
        with cli._pinned_server_process(child.pid) as ticks:
            assert ticks > 0
            with pytest.raises(cli.ServerOwnershipProbeError, match='incarnation'):
                with cli._pinned_server_process(child.pid, str(ticks + 1)):
                    pytest.fail('wrong creation accepted')
            child.terminate()
            child.wait(timeout=5)
            # The retained kernel object can still be opened with exactly
            # the same identity after exit; its PID has not been recycled.
            with cli._pinned_server_process(child.pid, str(ticks)) as after:
                assert after == ticks
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)


def test_stop_holds_real_handle_through_recovery_and_tree_stop(tmp_path):
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    try:
        with cli._pinned_server_process(child.pid) as ticks:
            pass
        marker = tmp_path / 'server.json'
        marker.write_text(json.dumps({'pid':child.pid,'port':54321,'process_creation_ticks':str(ticks)}))
        events = []
        def stop(*args, **kwargs):
            events.append('kill')
            # Exit precisely after validation but before the PID-based action.
            child.terminate()
            child.wait(timeout=5)
            with cli._pinned_server_process(child.pid, ticks):
                pass
            return SimpleNamespace(returncode=0)
        with (patch.object(cli, '_server_marker_path', return_value=marker),
              patch.object(cli, '_owned_server_process_command', return_value='python -m anythingllm_pdf_assistant_cli start'),
              patch.object(cli, '_listener_belongs_to_server_root', return_value=True),
              patch.object(cli, '_prepare_owned_active_runs_for_server_stop', side_effect=lambda _: events.append('recovery') or []),
              patch.object(cli, '_finalize_owned_runs_after_server_stop', side_effect=lambda *_: events.append('finalize')),
              patch.object(cli, '_port_is_available', return_value=True),
              patch.object(cli.subprocess, 'run', side_effect=stop)):
            assert cli._stop() == 0
        assert events == ['recovery', 'kill', 'finalize']
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)


def test_real_tree_stop_leaves_unrelated_sibling_alive(tmp_path):
    child_pid_file = tmp_path / 'child.pid'
    code = ("import subprocess,sys,time,pathlib; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            f"pathlib.Path({str(child_pid_file)!r}).write_text(str(p.pid)); time.sleep(60)")
    root = subprocess.Popen([sys.executable, '-c', code])
    sibling = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    descendant = None
    try:
        deadline = time.monotonic() + 5
        while not child_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        descendant = psutil.Process(int(child_pid_file.read_text()))
        with cli._pinned_server_process(root.pid) as ticks:
            pass
        marker = tmp_path / 'server.json'
        marker.write_text(json.dumps({'pid':root.pid,'port':54322,'process_creation_ticks':str(ticks)}))
        with (patch.object(cli, '_server_marker_path', return_value=marker),
              patch.object(cli, '_owned_server_process_command', return_value='python -m anythingllm_pdf_assistant_cli start'),
              patch.object(cli, '_listener_belongs_to_server_root', return_value=None),
              patch.object(cli, '_prepare_owned_active_runs_for_server_stop', return_value=[]),
              patch.object(cli, '_finalize_owned_runs_after_server_stop'),
              patch.object(cli, '_port_is_available', return_value=True)):
            assert cli._stop() == 0
        root.wait(timeout=5)
        descendant.wait(timeout=5)
        assert sibling.poll() is None
        assert not marker.exists()
    finally:
        if descendant is not None and descendant.is_running():
            descendant.terminate()
            descendant.wait(timeout=5)
        for p in (root, sibling):
            if p.poll() is None:
                p.terminate()
            p.wait(timeout=5)

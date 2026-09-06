import os
import socket
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

import psutil
import pytest
import anythingllm_pdf_assistant_cli as cli

pytestmark = pytest.mark.offline_deterministic


def setup_api(pids=(10,)):
    root = Mock(pid=10)
    root.is_running.return_value = True
    child = Mock(pid=11)
    child.parent.return_value = root
    foreign = Mock(pid=20)
    foreign.parent.return_value = None
    processes = {10: root, 11: child, 20: foreign}
    for pid, process in processes.items():
        process.create_time.return_value = 100.0 + pid
    api = SimpleNamespace(Error=psutil.Error, NoSuchProcess=psutil.NoSuchProcess, CONN_LISTEN='LISTEN',
                          Process=Mock(side_effect=lambda pid: processes[pid]),
                          net_connections=Mock(return_value=[SimpleNamespace(pid=pid, status='LISTEN',
                              laddr=SimpleNamespace(ip='127.0.0.1', port=7860)) for pid in pids]))
    return api, root, child, foreign


@pytest.mark.parametrize('pids,expected', [([], None), ([10], True), ([11], True), ([20], False), ([10,20], False)])
def test_listener_identity_and_all_listeners(pids, expected):
    api, *_ = setup_api(pids)
    assert cli._native_listener_belongs_to_root(api, 7860, 10) is expected


def test_missing_pid_is_unknown():
    api, *_ = setup_api([None])
    with pytest.raises(cli.ServerOwnershipProbeError):
        cli._native_listener_belongs_to_root(api, 7860, 10)


def test_access_denied_does_not_become_absent():
    api, *_ = setup_api()
    api.net_connections.side_effect = psutil.AccessDenied()
    with pytest.raises(cli.ServerOwnershipProbeError):
        cli._native_listener_belongs_to_root(api, 7860, 10)


def test_disappearing_root_is_unknown():
    api, root, *_ = setup_api()
    root.is_running.return_value = False
    with pytest.raises(cli.ServerOwnershipProbeError):
        cli._native_listener_belongs_to_root(api, 7860, 10)


def test_parent_cycle_cannot_authorize_stop():
    api, root, child, foreign = setup_api([20])
    foreign.parent.return_value = foreign
    assert cli._native_listener_belongs_to_root(api, 7860, 10) is False


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows native command path')
def test_native_command_does_not_spawn_powershell():
    with (patch.object(cli, '_native_process_api', return_value=psutil),
          patch.object(cli.subprocess, 'run', side_effect=AssertionError('no subprocess'))):
        assert 'python' in cli._owned_server_process_command(os.getpid()).lower()


def test_real_disposable_listener_owned():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        sock.listen()
        port = sock.getsockname()[1]
        assert cli._native_listener_belongs_to_root(psutil, port, os.getpid()) is True


@pytest.mark.skipif(sys.platform != 'win32', reason='Compare Windows ownership backends')
@pytest.mark.parametrize('backend', [psutil, None], ids=['native', 'powershell'])
def test_real_child_listener_and_foreign_sibling(backend):
    # Reading readiness has a bounded deadline; cleanup uses the Popen handle, never
    # a PID discovered from the machine's live application processes.
    listener = subprocess.Popen([sys.executable, '-c',
        "import socket,time; s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen(); "
        "print(s.getsockname()[1],flush=True); time.sleep(30)"], stdout=subprocess.PIPE, text=True)
    sibling = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    try:
        import threading
        line = []
        reader = threading.Thread(target=lambda: line.append(listener.stdout.readline()), daemon=True)
        reader.start()
        reader.join(timeout=3)
        assert line and line[0].strip(), 'child listener did not become ready'
        port = int(line[0])
        with patch.object(cli, '_native_process_api', return_value=backend):
            assert cli._listener_belongs_to_server_root(port, os.getpid()) is True
            try:
                foreign_result = cli._listener_belongs_to_server_root(port, sibling.pid)
            except cli.ServerOwnershipProbeError:
                # CIM can reach an already-exited ancestor when proving that
                # this sibling is unrelated. Unknown must also refuse ownership.
                assert backend is None
            else:
                assert foreign_result is False
    finally:
        for process in (listener, sibling):
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
        listener.stdout.close()


@pytest.mark.parametrize('exception,expected', [(psutil.NoSuchProcess(10), ''), (psutil.AccessDenied(10), None)])
@pytest.mark.skipif(sys.platform != 'win32', reason='Windows native command path')
def test_native_command_missing_and_denied_are_distinct(exception, expected):
    api, *_ = setup_api()
    api.Process.side_effect = exception
    with patch.object(cli, '_native_process_api', return_value=api):
        assert cli._owned_server_process_command(10) is expected


def test_unrelated_address_does_not_authorize_localhost():
    api, *_ = setup_api()
    api.net_connections.return_value[0].laddr.ip = '192.0.2.1'
    assert cli._native_listener_belongs_to_root(api, 7860, 10) is None

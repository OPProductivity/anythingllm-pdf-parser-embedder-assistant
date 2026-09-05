import json
import os
import subprocess
import sys
import time

import pytest

from diagnostic_runner import run_diagnostic

pytestmark = [pytest.mark.offline_deterministic, pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects")]


def test_success_large_stderr_and_failure_are_retained(tmp_path):
    result = run_diagnostic([sys.executable, "-c", "import sys; sys.stderr.write('x'*200000); print('done')"], tmp_path/'ok', timeout_seconds=10)
    assert result['status'] == 'completed'
    assert (tmp_path/'ok'/'stderr.log').stat().st_size == 200000
    result = run_diagnostic([sys.executable, "-c", "raise SystemExit(7)"], tmp_path/'fail', timeout_seconds=10)
    assert result['status'] == 'failed' and result['exit_code'] == 7


def _child_command(marker):
    return [sys.executable, '-c', f"import time,pathlib; time.sleep(3); pathlib.Path({str(marker)!r}).write_text('escaped')"]


@pytest.mark.parametrize('wait', [True, False])
def test_timeout_and_success_clean_up_descendants(tmp_path, wait):
    marker = tmp_path/'escaped'
    code = f"import subprocess,time; subprocess.Popen({_child_command(marker)!r}); print('child started',flush=True); " + ('time.sleep(30)' if wait else '')
    result = run_diagnostic([sys.executable,'-c',code], tmp_path/'run', timeout_seconds=1)
    assert result['status'] == ('timed_out' if wait else 'completed')
    assert result['cleanup'] == 'job_closed'
    time.sleep(3.2)
    assert not marker.exists()


def test_wrapper_death_kills_owned_tree(tmp_path):
    marker = tmp_path/'escaped'
    ready = tmp_path/'ready'
    code = f"import subprocess,time,pathlib; subprocess.Popen({_child_command(marker)!r}); pathlib.Path({str(ready)!r}).touch(); time.sleep(30)"
    root = tmp_path/'run'
    wrapper = subprocess.Popen([sys.executable, '-m', 'diagnostic_runner', '--timeout', '20', '--output', str(root), '--', sys.executable, '-c', code], stdout=subprocess.DEVNULL)
    try:
        deadline = time.monotonic()+10
        while not ready.exists() and time.monotonic()<deadline:
            time.sleep(.05)
        assert ready.exists()
    finally:
        wrapper.kill()
        wrapper.wait(timeout=5)
    time.sleep(3.2)
    assert not marker.exists()
    # Abrupt death cannot claim that a terminal receipt was written.
    assert json.loads((root/'result.json').read_text())['status'] == 'running'


def test_rejects_unbounded_timeout_and_existing_output(tmp_path):
    for timeout in [0, -1, float('inf'), float('nan')]:
        with pytest.raises(ValueError):
            run_diagnostic(['unused'], tmp_path/'new', timeout_seconds=timeout)
    with pytest.raises(FileExistsError):
        run_diagnostic(['unused'], tmp_path, timeout_seconds=1)


def test_windows_mutex_contention_across_processes(tmp_path):
    from process_lock import named_process_lock
    code = "from process_lock import named_process_lock; " + f"\nwith named_process_lock('test', {str(tmp_path)!r}, timeout_seconds=.1): pass"
    with named_process_lock('test', str(tmp_path)):
        result = subprocess.run([sys.executable,'-c',code], capture_output=True, timeout=10)
    assert result.returncode != 0 and b'TimeoutError' in result.stderr
    with named_process_lock('test', str(tmp_path), timeout_seconds=.1):
        pass


def test_transport_startup_exception_still_stops_test_server(tmp_path, monkeypatch):
    import reliability_fault_injection as faults
    from unittest.mock import Mock
    server = Mock()
    monkeypatch.setattr(faults, 'SCENARIOS', {'test': {}})
    monkeypatch.setattr(faults.subprocess, 'Popen', Mock(return_value=server))
    def failed_start(*args):
        raise RuntimeError('startup failed')
    monkeypatch.setattr(faults, '_wait_for_server', failed_start)
    with pytest.raises(RuntimeError, match='startup failed'):
        faults.run_transport_fault_acceptance(tmp_path)
    server.terminate.assert_called_once()
    server.wait.assert_called_once()


def test_containment_failure_never_releases_payload(tmp_path, monkeypatch):
    import diagnostic_runner as runner
    marker = tmp_path/'not-started'
    def fail_assignment(*args):
        raise OSError('cannot contain')
    monkeypatch.setattr(runner._Job, 'assign', fail_assignment)
    with pytest.raises(OSError, match='cannot contain'):
        run_diagnostic([sys.executable, '-c', f"from pathlib import Path; Path({str(marker)!r}).touch()"], tmp_path/'run', timeout_seconds=5)
    assert not marker.exists()
    assert json.loads((tmp_path/'run'/'result.json').read_text())['status'] == 'runner_error'


def test_parent_loss_probe_uses_file_not_pipe_for_stderr(tmp_path, monkeypatch):
    import reliability_parent_loss_acceptance as probe
    from unittest.mock import Mock
    pdf = tmp_path/'source.pdf'
    pdf.touch()
    parent = Mock(returncode=1)
    def launch(*args, **kwargs):
        assert kwargs['stderr'] != subprocess.PIPE
        kwargs['stderr'].write(b'x'*20000)
        kwargs['stderr'].flush()
        return parent
    monkeypatch.setattr(probe.subprocess, 'Popen', launch)
    monkeypatch.setattr(probe, '_wait_for_first', lambda *args: None)
    result = probe.run_parent_loss_acceptance(pdf, tmp_path/'output')
    assert len(result['stderr_tail']) == 12000


def test_mutation_lease_blocks_other_process_then_releases(monkeypatch):
    import uuid
    import rag_pdf_gradio_app as app
    name = 'Local\\diagnostic-lease-test-' + uuid.uuid4().hex
    monkeypatch.setattr(app, 'AUTOMATIC_ANYTHINGLLM_MUTATION_MUTEX_NAME', name)
    code = ("import json,rag_pdf_gradio_app as a; "
            f"a.AUTOMATIC_ANYTHINGLLM_MUTATION_MUTEX_NAME={name!r}; "
            "print(json.dumps(a.acquire_automatic_anythingllm_mutation_lease('child'))); "
            "a.release_automatic_anythingllm_mutation_lease('child')")
    assert app.acquire_automatic_anythingllm_mutation_lease('test')['acquired']
    try:
        child = subprocess.run([sys.executable,'-c',code], capture_output=True, text=True, timeout=30)
        assert child.returncode == 0, child.stderr
        assert not json.loads(child.stdout.strip().splitlines()[-1])['acquired']
    finally:
        app.release_automatic_anythingllm_mutation_lease('test')
    child = subprocess.run([sys.executable,'-c',code], capture_output=True, text=True, timeout=30)
    assert child.returncode == 0, child.stderr
    assert json.loads(child.stdout.strip().splitlines()[-1])['acquired']

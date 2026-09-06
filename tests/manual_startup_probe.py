"""Explicit idle-server startup measurement, including the Desktop launch path."""
import json
import os
import subprocess
import sys
import time
import urllib.request

import anythingllm_pdf_assistant_cli as cli


def main():
    begin = time.perf_counter()
    if cli._server_marker_path().exists():
        record = json.loads(cli._server_marker_path().read_text())
        assert not cli._owned_active_run_roots(cli._marker_root_pid(record))
        assert cli._stop() == 0
    stopped = time.perf_counter() - begin
    command = f'"{cli._powershell()}" {cli._shortcut_arguments("start")}'
    begin = time.perf_counter()
    if '--uri' in sys.argv:
        os.startfile('anythingllm-pdf-assistant://start')
    else:
        subprocess.Popen(command, creationflags=subprocess.CREATE_NO_WINDOW)
    deadline = begin + 45
    while time.perf_counter() < deadline:
        try:
            with urllib.request.urlopen('http://127.0.0.1:7860/healthz', timeout=.5) as response:
                assert response.status == 200
            break
        except OSError:
            time.sleep(.05)
    else:
        raise RuntimeError('Server did not become ready')
    ready = time.perf_counter() - begin
    with urllib.request.urlopen('http://127.0.0.1:7860/', timeout=5) as response:
        assert b'gradio' in response.read()
    print(json.dumps({'stop_seconds': round(stopped, 3), 'start_to_health_seconds': round(ready, 3), 'start_to_html_seconds': round(time.perf_counter()-begin, 3)}))


if __name__ == '__main__':
    main()

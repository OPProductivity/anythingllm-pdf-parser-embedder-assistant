"""Read-only comparison of reverted security transports; no embedding calls."""
import json
import os
from pathlib import Path
import socket
import statistics
import subprocess
import sys
import threading
import time
import types
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def historical_module(name):
    source = subprocess.check_output(
        ['git', 'show', f'581fb0b:{name}.py'], cwd=ROOT).decode('utf-8')
    module = types.ModuleType(name)
    exec(compile(source, f'581fb0b:{name}.py', 'exec'), module.__dict__)
    return module


def measure(function, samples=8):
    results = []
    for _ in range(samples):
        start = time.perf_counter()
        function()
        results.append(time.perf_counter() - start)
    return {'samples': samples, 'median_seconds': round(statistics.median(results), 6),
            'max_seconds': round(max(results), 6), 'total_seconds': round(sum(results), 6)}


def main():
    import auto_anythingllm_pipeline as pipeline
    import ingestion_observation as baseline_observer

    trust = historical_module('desktop_service_trust')
    sys.modules['desktop_service_trust'] = trust
    http = historical_module('authenticated_http')
    sys.modules['authenticated_http'] = http
    security_observer = historical_module('ingestion_observation')
    storage = pipeline.default_anythingllm_storage_dir()
    url = 'http://127.0.0.1:3001'
    result = {'schema': 'security_transport_ab_v1', 'mutations': 'temporary API key only; removed finally'}
    result['listener_probe'] = measure(lambda: trust.desktop_listener(url, storage))
    def connected_probe():
        with socket.create_connection(('127.0.0.1', 3001), timeout=5) as connection:
            trust.verify_connected_desktop_socket(connection, url, storage)
    result['connected_socket_probe'] = measure(connected_probe)
    temporary = pipeline.create_temporary_desktop_api_key(url)
    assert temporary.get('status') == 'created', temporary.get('status')
    key = temporary['secret']
    trust.register_managed_key(key, storage)
    try:
        def old_request():
            status, body = pipeline.get_json(url + '/api/ping', api_key=key)
            assert status == 200 and json.loads(body)
        def new_request():
            request = urllib.request.Request(url + '/api/ping', headers={'Authorization': 'Bearer ' + key})
            trust.require_managed_key_origin(request.full_url, key)
            with http.authenticated_opener(request).open(request, timeout=5) as response:
                assert response.status == 200 and json.loads(http.read_bounded_response(response))
        result['baseline_authenticated_ping'] = measure(old_request)
        result['security_authenticated_ping'] = measure(new_request)
        for name, observer in [('baseline', baseline_observer), ('security', security_observer)]:
            stopped, connected = observer.StreamStopEvent(), threading.Event()
            states = []
            endpoints = [url + '/api/v1/workspace/pdf-workspace/embed-progress',
                         url + '/api/workspace/pdf-workspace/embed-progress']
            thread = threading.Thread(target=observer.listen_to_progress_stream,
                args=(endpoints, key, stopped, lambda payload: None),
                kwargs={'connected_event': connected, 'state_callback': lambda state, detail: states.append(state)},
                daemon=True)
            start = time.perf_counter()
            thread.start()
            try:
                assert connected.wait(8), states
                connect_seconds = time.perf_counter() - start
            finally:
                start = time.perf_counter()
                stopped.set()
                thread.join(2)
                assert not thread.is_alive(), 'Observer failed to cancel'
            result[name + '_live_sse'] = {'connect_seconds': round(connect_seconds, 6),
                'cancel_seconds': round(time.perf_counter() - start, 6), 'states': states}
    finally:
        cleanup = pipeline.cleanup_temporary_desktop_api_key(url, temporary['id'])
        result['temporary_key_cleanup'] = cleanup.get('status')
        assert cleanup.get('status') != 'delete_failed'
    runs = Path(os.environ['LOCALAPPDATA']) / 'AnythingLLM PDF Parser Embedder Assistant/run-state/automatic-runs'
    result['queue_observations'] = []
    for name in ['r-20261003-230550-4d139627a8', 'r-20261003-234619-557b87b46f']:
        for path in sorted((runs / name / 'queue-groups').glob('*ledger.events.jsonl')):
            events = [json.loads(line)['event'] for line in path.read_text(encoding='utf-8').splitlines()]
            complete = [event for event in events if event.get('type') == 'doc_complete']
            result['queue_observations'].append({'run': name, 'group': path.name,
                'complete_count': len(complete), 'first_completion': complete[:1],
                'event_types': sorted(set(event.get('type', event.get('event', '')) for event in events))})
    (ROOT / 'tmp-output/security-transport-ab-20261004.json').write_text(
        json.dumps(result, ensure_ascii=True, separators=(',', ':')), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()

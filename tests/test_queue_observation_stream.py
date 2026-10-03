import http.server
import json
import threading
import time
from unittest.mock import patch

import pytest
import httpx

import auto_anythingllm_pipeline as pipeline
from ingestion_observation import StreamStopEvent

pytestmark = pytest.mark.offline_deterministic


class BroadcastServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), BroadcastHandler)
        self.clients = []
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.connection_count = 0

    def emit(self, kind, filename=None):
        event = {"type": kind, "workspaceSlug": "workspace"}
        if filename:
            event["filename"] = filename
        payload = ("data: " + json.dumps(event) + "\n\n").encode()
        with self.lock:
            for stream in list(self.clients):
                try:
                    stream.write(payload)
                    stream.flush()
                except OSError:
                    self.clients.remove(stream)


class BroadcastHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/api/v1/'):
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        with self.server.lock:
            self.server.connection_count += 1
            self.server.clients.append(self.wfile)
        self.end_headers()
        self.wfile.flush()
        self.server.closed.wait(15)

    def log_message(self, *_args):
        pass


@pytest.mark.parametrize('quiet_seconds', [0.15, 5.15])
def test_quiet_stream_keeps_events_and_stops_without_socket_timeout(quiet_seconds):
    server = BroadcastServer()
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    observed = []
    complete = threading.Event()

    def receive(event):
        observed.append(event)
        if event["type"] == "all_complete":
            complete.set()

    listener = None
    try:
        # The accelerated connection/read timeout reproduces the production
        # five-second quiet-boundary gap without slowing the test suite.
        with patch('ingestion_observation.STREAM_CONNECT_TIMEOUT_SECONDS', 0.08):
            listener = pipeline.start_anythingllm_embed_progress_listener(
                f"http://127.0.0.1:{server.server_port}", "", "workspace",
                ["custom/a.json", "custom/b.json"], observer_callback=receive,
            )
            assert listener["connected_event"].wait(1)
            server.emit("doc_starting", "custom/a.json")
            time.sleep(quiet_seconds)
            server.emit("doc_complete", "custom/a.json")
            server.emit("doc_starting", "custom/b.json")
            time.sleep(0.85)
            server.emit("doc_complete", "custom/b.json")
            server.emit("all_complete")
            assert complete.wait(1)
            assert [row["type"] for row in observed] == [
                "doc_starting", "doc_complete", "doc_starting", "doc_complete", "all_complete",
            ]
            assert server.connection_count == 1
    finally:
        if listener:
            listener["stop_event"].set()
            listener["thread"].join(1)
        server.closed.set()
        server.shutdown()
        server.server_close()


def test_quiet_stream_stop_interrupts_blocked_read():
    server = BroadcastServer()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    listener = pipeline.start_anythingllm_embed_progress_listener(
        f"http://127.0.0.1:{server.server_port}", "", "workspace", [],
    )
    try:
        assert listener["connected_event"].wait(1)
        listener["stop_event"].set()
        listener["thread"].join(0.3)
        assert not listener["thread"].is_alive()
    finally:
        listener["stop_event"].set()
        server.closed.set()
        server.shutdown()
        server.server_close()
        listener["thread"].join(1)


def test_actual_disconnects_reconnect_and_remain_diagnostic():
    stop = StreamStopEvent()
    calls = []
    errors = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            if len(calls) < 3:
                return
            yield b'data: {"type":"doc_starting","filename":"custom/a.json"}\n\n'

    def handle(request):
        calls.append(request)
        return httpx.Response(200, stream=Stream())

    original_client = httpx.AsyncClient
    with patch('httpx.AsyncClient', side_effect=lambda **kwargs:
               original_client(transport=httpx.MockTransport(handle), **kwargs)):
        pipeline.listen_for_anythingllm_embed_progress(
            'http://localhost', '', 'workspace', ['custom/a.json'], stop,
            event_callback=lambda event: stop.set(),
            error_callback=lambda reason, attempt: errors.append(reason),
        )
    assert len(calls) == 3
    assert errors == ['stream_eof', 'stream_eof']


def test_authenticated_stream_redirect_is_not_followed():
    stop = StreamStopEvent()
    calls = []
    errors = []

    def handle(request):
        calls.append(request)
        return httpx.Response(302, headers={'Location': 'http://different-host/stream'})

    original_client = httpx.AsyncClient
    with patch('httpx.AsyncClient', side_effect=lambda **kwargs:
               original_client(transport=httpx.MockTransport(handle), **kwargs)):
        pipeline.listen_for_anythingllm_embed_progress(
            'http://localhost', 'disposable-test-key', 'workspace', [], stop,
            error_callback=lambda reason, attempt: errors.append(reason),
        )
    assert len(calls) == 1
    assert errors and 'redirect rejected' in errors[0]


def test_stop_before_connection_does_not_open_stream():
    stop = StreamStopEvent()
    stop.set()
    with patch('httpx.AsyncClient') as client:
        pipeline.listen_for_anythingllm_embed_progress('http://localhost', '', 'workspace', [], stop)
    client.assert_not_called()

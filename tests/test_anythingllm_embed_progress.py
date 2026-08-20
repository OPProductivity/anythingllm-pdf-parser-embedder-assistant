import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from auto_anythingllm_pipeline import (
    _anythingllm_embed_event_matches_locations,
    _anythingllm_vector_cache_hit,
    anythingllm_embed_progress_message,
    find_reusable_cached_document_locations,
    listen_for_anythingllm_embed_progress,
    parse_anythingllm_embed_progress_event,
)


pytestmark = pytest.mark.offline_deterministic


def test_parses_desktop_embed_progress_event_and_renders_compact_status():
    event = parse_anythingllm_embed_progress_event(
        json.dumps(
            {
                "type": "chunk_progress",
                "filename": "custom-documents/example-p001-s01.txt",
                "docIndex": 2,
                "totalDocs": 6,
                "chunksProcessed": 3,
                "totalChunks": 4,
            }
        )
    )

    assert event["type"] == "chunk_progress"
    assert anythingllm_embed_progress_message(event) == (
        "AnythingLLM Desktop queue: record 3/6, chunks 3/4"
    )


def test_renders_a_cache_reuse_status_only_for_explicit_cache_evidence(tmp_path):
    location = "custom-documents/page-parent-p001.txt"
    cache_dir = tmp_path / "vector-cache"
    cache_dir.mkdir()
    (cache_dir / f"{uuid.uuid5(uuid.NAMESPACE_URL, location)}.json").write_text(
        "[]", encoding="utf-8"
    )

    assert _anythingllm_vector_cache_hit(tmp_path, location)
    assert not _anythingllm_vector_cache_hit(tmp_path, "custom-documents/other.txt")
    assert anythingllm_embed_progress_message(
        {
            "type": "doc_starting",
            "docIndex": 0,
            "totalDocs": 1,
            "vector_cache_hit": True,
        }
    ) == (
        "AnythingLLM Desktop queue: reusing cached embeddings for record 1/1; "
        "writing its page-parent record to this workspace"
    )


def test_reuses_only_an_exact_cached_page_parent_document(tmp_path):
    location = "custom-documents/existing-page-parent.json"
    document = {
        "pageContent": "Exact page-parent text.",
        "title": "Example title",
        "docAuthor": "Example author",
        "description": "PDF page: 1.",
        "docSource": "local-pdf://sha256/example",
        "chunkSource": "page-parent://example-p0001",
    }
    document_path = tmp_path / "documents" / location
    document_path.parent.mkdir(parents=True)
    document_path.write_text(json.dumps(document), encoding="utf-8")
    cache_dir = tmp_path / "vector-cache"
    cache_dir.mkdir()
    (cache_dir / f"{uuid.uuid5(uuid.NAMESPACE_URL, location)}.json").write_text(
        "[]", encoding="utf-8"
    )
    payload = {
        "textContent": document["pageContent"],
        "metadata": {
            key: document[key]
            for key in ("title", "docAuthor", "description", "docSource", "chunkSource")
        },
    }
    changed_payload = {
        **payload,
        "textContent": "A changed page must not reuse the old vector.",
    }

    assert find_reusable_cached_document_locations(tmp_path, [payload, changed_payload]) == [
        location,
        "",
    ]


def test_cached_reuse_respects_requested_document_folder_layout(tmp_path):
    location = "custom-documents/nested/example-page-parent.json"
    document = {
        "pageContent": "Exact page-parent text.",
        "title": "Example title",
        "docAuthor": "Example author",
        "description": "PDF page: 1.",
        "docSource": "local-pdf://sha256/example",
        "chunkSource": "page-parent://example-p0001",
    }
    document_path = tmp_path / "documents" / location
    document_path.parent.mkdir(parents=True)
    document_path.write_text(json.dumps(document), encoding="utf-8")
    cache_dir = tmp_path / "vector-cache"
    cache_dir.mkdir()
    (cache_dir / f"{uuid.uuid5(uuid.NAMESPACE_URL, location)}.json").write_text(
        "[]", encoding="utf-8"
    )
    payload = {
        "textContent": document["pageContent"],
        "metadata": {
            key: document[key]
            for key in ("title", "docAuthor", "description", "docSource", "chunkSource")
        },
    }

    assert find_reusable_cached_document_locations(
        tmp_path, [payload], folder_names=["custom-documents"]
    ) == [""]
    assert find_reusable_cached_document_locations(
        tmp_path, [payload], folder_names=["custom-documents/nested"]
    ) == [location]


def test_progress_listener_filters_unrelated_workspace_queue_events():
    expected = {"custom-documents/expected-p001-s01.txt"}
    matched = set()
    unrelated = {
        "type": "doc_starting",
        "filename": "custom-documents/manual-upload.txt",
        "docIndex": 0,
        "totalDocs": 1,
    }
    ours = {
        "type": "doc_starting",
        "filename": "custom-documents\\expected-p001-s01.txt",
        "docIndex": 0,
        "totalDocs": 1,
    }

    assert not _anythingllm_embed_event_matches_locations(unrelated, expected, matched)
    assert _anythingllm_embed_event_matches_locations(ours, expected, matched)
    assert matched == {"custom-documents/expected-p001-s01.txt"}


def test_all_complete_is_only_relevant_after_this_run_has_matched_a_file():
    expected = {"custom-documents/expected-p001-s01.txt"}
    completion = {"type": "all_complete", "embedded": 1, "failed": 0}

    assert not _anythingllm_embed_event_matches_locations(completion, expected, set())
    assert _anythingllm_embed_event_matches_locations(
        completion, expected, {"custom-documents/expected-p001-s01.txt"}
    )


def test_sse_listener_observes_only_the_submitted_document_paths():
    class ProgressHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.server.paths.append(self.path)
            if self.path.startswith("/api/v1/"):
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for event in (
                {"type": "doc_starting", "filename": "custom-documents/unrelated.txt"},
                {"type": "doc_complete", "filename": "custom-documents/expected.txt"},
            ):
                self.wfile.write(f"data: {json.dumps(event)}\n\n".encode("utf-8"))
                self.wfile.flush()
            self.server.sent.set()
            self.server.release.wait(2)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), ProgressHandler)
    server.paths = []
    server.sent = threading.Event()
    server.release = threading.Event()
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    stop_event = threading.Event()
    observed = []
    listener = threading.Thread(
        target=listen_for_anythingllm_embed_progress,
        args=(
            f"http://127.0.0.1:{server.server_port}",
            "test-key",
            "workspace",
            ["custom-documents/expected.txt"],
            stop_event,
        ),
        kwargs={"event_callback": observed.append},
        daemon=True,
    )
    try:
        listener.start()
        assert server.sent.wait(1)
        stop_event.set()
        server.release.set()
        listener.join(1)
        assert [event["type"] for event in observed] == ["doc_complete"]
        assert server.paths[0].startswith("/api/v1/")
        assert server.paths[1].startswith("/api/workspace/")
    finally:
        stop_event.set()
        server.release.set()
        server.shutdown()
        server.server_close()


def test_sse_listener_signals_connection_without_turning_idle_timeouts_into_errors():
    class ProgressHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.server.connected.set()
            self.server.release.wait(1)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), ProgressHandler)
    server.connected = threading.Event()
    server.release = threading.Event()
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    stop_event = threading.Event()
    connection_ready = threading.Event()
    errors = []
    listener = threading.Thread(
        target=listen_for_anythingllm_embed_progress,
        args=(
            f"http://127.0.0.1:{server.server_port}",
            "test-key",
            "workspace",
            ["custom-documents/expected.txt"],
            stop_event,
        ),
        kwargs={
            "connected_event": connection_ready,
            "error_callback": lambda error, attempt: errors.append((error, attempt)),
        },
        daemon=True,
    )
    try:
        listener.start()
        assert server.connected.wait(1)
        assert connection_ready.wait(1)
        stop_event.set()
        server.release.set()
        listener.join(1)
        assert errors == []
    finally:
        stop_event.set()
        server.release.set()
        server.shutdown()
        server.server_close()

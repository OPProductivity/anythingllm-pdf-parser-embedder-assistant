import json
import sqlite3
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from auto_anythingllm_pipeline import (
    _anythingllm_embed_event_matches_locations,
    _anythingllm_vector_cache_hit,
    anythingllm_embed_progress_message,
    build_reusable_cached_document_snapshot,
    find_reusable_cached_document_locations,
    find_reusable_cached_document_locations_from_snapshot,
    listen_for_anythingllm_embed_progress,
    observe_indexed_source_identity_hint,
    parse_anythingllm_embed_progress_event,
)


pytestmark = pytest.mark.offline_deterministic


def test_shadow_identity_hint_counts_distinct_sources_without_claiming_cache(tmp_path):
    con = sqlite3.connect(tmp_path / "anythingllm.db")
    try:
        con.execute("create table workspace_documents (metadata text)")
        con.executemany(
            "insert into workspace_documents (metadata) values (?)",
            [
                (json.dumps({"chunkSource": "page-parent://one"}),),
                (json.dumps({"chunkSource": "page-parent://one"}),),
                (json.dumps({"chunkSource": "page-parent://other"}),),
                ("not-json",),
            ],
        )
        con.commit()
    finally:
        con.close()

    observation = observe_indexed_source_identity_hint(
        tmp_path,
        ["page-parent://one", "page-parent://two"],
    )

    assert observation == {
        "status": "complete",
        "expected_source_identities": 2,
        "matched_source_identities": 1,
    }


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
    message = anythingllm_embed_progress_message(
        {
            "type": "doc_starting",
            "docIndex": 0,
            "totalDocs": 1,
            "vector_cache_hit": True,
        }
    )
    assert "record 1/1" in message
    assert "reusing its cached embeddings" in message
    assert "page-parent record" in message


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


def test_cached_reuse_uses_desktop_index_and_ignores_unattached_orphan(tmp_path):
    location = "custom-documents/indexed-page-parent.json"
    orphan_location = "custom-documents/orphan-page-parent.json"
    document = {
        "pageContent": "Exact indexed text.",
        "title": "Example title",
        "docAuthor": "Example author",
        "description": "PDF page: 1.",
        "docSource": "local-pdf://sha256/indexed",
        "chunkSource": "page-parent://indexed-p0001",
    }
    documents_root = tmp_path / "documents"
    for candidate_location in (location, orphan_location):
        path = documents_root / candidate_location
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document), encoding="utf-8")
    cache_dir = tmp_path / "vector-cache"
    cache_dir.mkdir()
    for candidate_location in (location, orphan_location):
        (cache_dir / f"{uuid.uuid5(uuid.NAMESPACE_URL, candidate_location)}.json").write_text(
            "[]", encoding="utf-8"
        )
    con = sqlite3.connect(tmp_path / "anythingllm.db")
    try:
        con.execute("create table workspace_documents (docpath text, metadata text)")
        con.execute(
            "insert into workspace_documents (docpath, metadata) values (?, ?)",
            (location, json.dumps({"chunkSource": document["chunkSource"]})),
        )
        con.commit()
    finally:
        con.close()
    payload = {
        "textContent": document["pageContent"],
        "metadata": {
            key: document[key]
            for key in ("title", "docAuthor", "description", "docSource", "chunkSource")
        },
    }

    # The attached Desktop index gives one bounded candidate. The identical
    # orphan does not trigger a recursive full-store recovery scan.
    assert find_reusable_cached_document_locations(tmp_path, [payload]) == [location]


def test_readonly_cache_snapshot_reuses_exact_indexed_payload_without_rescanning_store(tmp_path):
    location = "custom-documents/indexed-page-parent.json"
    document = {
        "pageContent": "Exact indexed text.",
        "title": "Example title",
        "docAuthor": "Example author",
        "description": "PDF page: 1.",
        "docSource": "local-pdf://sha256/indexed",
        "chunkSource": "page-parent://indexed-p0001",
    }
    document_path = tmp_path / "documents" / location
    document_path.parent.mkdir(parents=True)
    document_path.write_text(json.dumps(document), encoding="utf-8")
    cache_dir = tmp_path / "vector-cache"
    cache_dir.mkdir()
    (cache_dir / f"{uuid.uuid5(uuid.NAMESPACE_URL, location)}.json").write_text(
        "[]", encoding="utf-8"
    )
    con = sqlite3.connect(tmp_path / "anythingllm.db")
    try:
        con.execute("create table workspace_documents (docpath text, metadata text)")
        con.execute(
            "insert into workspace_documents (docpath, metadata) values (?, ?)",
            (location, json.dumps({"chunkSource": document["chunkSource"]})),
        )
        con.commit()
    finally:
        con.close()
    payload = {
        "textContent": document["pageContent"],
        "metadata": {
            key: document[key]
            for key in ("title", "docAuthor", "description", "docSource", "chunkSource")
        },
    }

    snapshot = build_reusable_cached_document_snapshot(tmp_path)

    assert snapshot["status"] == "ready"
    assert find_reusable_cached_document_locations_from_snapshot(
        snapshot, [payload], folder_names=["custom-documents"]
    ) == [location]
    # One raw location may only support one selected page-parent plan.
    assert find_reusable_cached_document_locations_from_snapshot(
        snapshot, [payload], folder_names=["custom-documents"]
    ) == [""]


def test_readonly_cache_snapshot_with_no_desktop_index_withholds_negative_eta_evidence(tmp_path):
    (tmp_path / "documents" / "custom-documents").mkdir(parents=True)
    (tmp_path / "vector-cache").mkdir()

    snapshot = build_reusable_cached_document_snapshot(tmp_path)

    assert snapshot["status"] == "unavailable"
    assert "index" in snapshot["error"]


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
    observed_expected = threading.Event()

    def capture_event(event):
        observed.append(event)
        if event.get("type") == "doc_complete":
            observed_expected.set()

    listener = threading.Thread(
        target=listen_for_anythingllm_embed_progress,
        args=(
            f"http://127.0.0.1:{server.server_port}",
            "test-key",
            "workspace",
            ["custom-documents/expected.txt"],
            stop_event,
        ),
        kwargs={"event_callback": capture_event},
        daemon=True,
    )
    try:
        listener.start()
        assert server.sent.wait(1)
        # The listener is deliberately cancellable. Stopping it immediately
        # after the server flushes bytes races the client parser and tests no
        # transport invariant. Wait for the matching event we are asserting.
        assert observed_expected.wait(1)
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

"""Cheap commit hints; never substitutes for exact physical vector proof."""

from pathlib import Path
import asyncio
import sqlite3
import threading
import time
from contextlib import closing
from authenticated_http import validate_authenticated_url, ResponseBudgetExceeded
from desktop_service_trust import require_managed_key_origin, managed_async_transport

STREAM_CONNECT_TIMEOUT_SECONDS = 5
MAX_STREAM_LINE_BYTES = 2 * 1024 * 1024
MAX_STREAM_EVENT_BYTES = 4 * 1024 * 1024


async def bounded_stream_lines(response):
    pending = bytearray()
    async for chunk in response.aiter_raw():
        pending.extend(chunk)
        while (newline := pending.find(b"\n")) >= 0:
            if newline > MAX_STREAM_LINE_BYTES:
                raise ResponseBudgetExceeded("Progress stream line exceeds its byte budget.")
            yield bytes(pending[:newline]).rstrip(b"\r").decode("utf-8", errors="replace")
            del pending[:newline + 1]
        if len(pending) > MAX_STREAM_LINE_BYTES:
            raise ResponseBudgetExceeded("Progress stream line exceeds its byte budget.")
    if pending:
        yield bytes(pending).rstrip(b"\r").decode("utf-8", errors="replace")


def observe_submission_vector_ids(
    storage_dir,
    workspace_slug,
    location_vector_ids,
    *,
    expected_provenance=None,
):
    """Prove mapped IDs and supplied source identities in the selected namespace."""
    expected = {str(value) for values in location_vector_ids.values() for value in values}
    result = {
        'status': 'pending', 'expected_vector_count': len(expected),
        'matched_vector_count': 0, 'locations_with_vectors': [], 'complete': False,
        'provenance_checked': False, 'provenance_mismatched_vector_count': 0,
        'provenance_mismatches': [],
    }
    vector_store = Path(storage_dir) / 'lancedb'
    if not expected or not workspace_slug or not vector_store.is_dir():
        return result
    try:
        import lancedb

        table = lancedb.connect(str(vector_store)).open_table(workspace_slug)
        present = set()
        accepted = set()
        provenance_by_id = {}
        for location, values in location_vector_ids.items():
            identity = (expected_provenance or {}).get(location) or {}
            wanted = {
                name: str(identity.get(name) or '').strip()
                for name in ('docSource', 'chunkSource')
                if str(identity.get(name) or '').strip()
            }
            for value in values:
                provenance_by_id.setdefault(str(value), []).append(wanted)
        fields = sorted({
            name
            for identities in provenance_by_id.values()
            for identity in identities
            for name in identity
        })
        columns = set(table.schema.names)
        selected = ['id'] + [name for name in fields if name in columns]
        result['provenance_checked'] = bool(fields)
        mismatched = set()
        ids = sorted(expected)
        for start in range(0, len(ids), 256):
            batch = ids[start:start + 256]
            quoted = ','.join("'" + value.replace("'", "''") + "'" for value in batch)
            rows = (table.search().where(f'id IN ({quoted})', prefilter=True)
                    .select(selected).limit(len(batch)).to_arrow().to_pylist())
            for row in rows:
                identity = str(row['id'])
                present.add(identity)
                wrong = sorted({
                    name
                    for wanted in provenance_by_id.get(identity, [])
                    for name, value in wanted.items()
                    if str(row.get(name) or '').strip() != value
                })
                if wrong:
                    mismatched.add(identity)
                    if len(result['provenance_mismatches']) < 25:
                        result['provenance_mismatches'].append(
                            {'id': identity, 'fields': wrong}
                        )
                else:
                    accepted.add(identity)
        result.update({
            'status': 'provenance_mismatch' if mismatched else 'complete',
            'matched_vector_count': len(expected & present),
            'provenance_mismatched_vector_count': len(mismatched),
            'locations_with_vectors': sorted(
                location for location, values in location_vector_ids.items()
                if values and set(values).issubset(accepted)
            ),
            'complete': expected.issubset(accepted),
        })
    except Exception as exc:
        # Missing/busy physical storage is uncertainty, never retry authority.
        result.update({'status': 'unavailable', 'error_type': type(exc).__name__, 'error': str(exc)})
    return result


class StreamStopEvent(threading.Event):
    """Cancel an async observer without waiting for a quiet socket timeout."""

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._loop = None
        self._task = None

    def bind_task(self, loop, task):
        with self._lock:
            self._loop, self._task = loop, task
            if self.is_set():
                task.cancel()

    def unbind_task(self):
        with self._lock:
            self._loop = self._task = None

    def set(self):
        super().set()
        with self._lock:
            if self._loop is not None and self._task is not None:
                try:
                    self._loop.call_soon_threadsafe(self._task.cancel)
                except RuntimeError:
                    pass


def listen_to_progress_stream(endpoints, api_key, stop_event, payload_callback,
                              error_callback=None, state_callback=None, connected_event=None):
    """Advisory SSE transport with bounded connect and cancellable idle reads."""
    import httpx

    if stop_event.is_set():
        return

    def state(name, reason='', failures=0):
        if callable(state_callback):
            state_callback(name, {'at_monotonic': time.monotonic(), 'reason': reason, 'failures': failures})

    async def consume():
        task = asyncio.current_task()
        assert task is not None
        watcher = None
        if callable(getattr(stop_event, 'bind_task', None)):
            stop_event.bind_task(asyncio.get_running_loop(), task)
        else:
            # Compatibility for callers supplying an ordinary threading.Event.
            # Production observers use immediate thread-safe task cancellation.
            async def watch_stop():
                while not stop_event.is_set():
                    await asyncio.sleep(0.05)
                task.cancel()
            watcher = asyncio.create_task(watch_stop())
        headers = {'Accept': 'text/event-stream', 'Cache-Control': 'no-cache', 'Accept-Encoding': 'identity'}
        if api_key:
            headers['Authorization'] = f'Bearer {api_key}'
        endpoint = 0
        failures = 0
        connected_once = False
        try:
            owned_transport = managed_async_transport(endpoints[0], api_key) if api_key else None
            async with httpx.AsyncClient(timeout=httpx.Timeout(STREAM_CONNECT_TIMEOUT_SECONDS, read=None),
                                         follow_redirects=False, trust_env=False,
                                         **({"transport": owned_transport} if owned_transport else {})) as client:
                while not stop_event.is_set():
                    try:
                        if api_key:
                            validate_authenticated_url(endpoints[endpoint])
                            require_managed_key_origin(endpoints[endpoint], api_key)
                        async with client.stream('GET', endpoints[endpoint], headers=headers) as response:
                            if response.status_code == 404 and endpoint + 1 < len(endpoints):
                                endpoint += 1
                                continue
                            if response.is_redirect:
                                reason = 'API redirect rejected; use the final API endpoint. No redirect was followed.'
                                state('unavailable', reason, 1)
                                if callable(error_callback):
                                    error_callback(reason, 1)
                                return
                            response.raise_for_status()
                            if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                                raise ResponseBudgetExceeded('Compressed progress streams are not accepted.')
                            failures = 0
                            connected_once = True
                            state('connected')
                            if connected_event is not None:
                                connected_event.set()
                            response.encoding = 'utf-8'
                            payload_lines = []
                            payload_bytes = 0
                            async for line in bounded_stream_lines(response):
                                if stop_event.is_set():
                                    return
                                if line.startswith('data:'):
                                    payload_bytes += len(line.encode('utf-8'))
                                    if payload_bytes > MAX_STREAM_EVENT_BYTES:
                                        raise ResponseBudgetExceeded('Progress stream event exceeds its byte budget.')
                                    payload_lines.append(line[5:].lstrip())
                                elif not line and payload_lines:
                                    payload_callback('\n'.join(payload_lines))
                                    payload_lines = []
                                    payload_bytes = 0
                        reason = 'stream_eof'
                    except (ResponseBudgetExceeded, ValueError) as exc:
                        reason = str(exc)
                        state('unavailable', reason, 1)
                        if callable(error_callback):
                            error_callback(reason, 1)
                        return
                    except httpx.HTTPError as exc:
                        reason = (f'HTTP {exc.response.status_code}' if isinstance(exc, httpx.HTTPStatusError)
                                  else type(exc).__name__)
                    except Exception as exc:
                        reason = type(exc).__name__
                    if stop_event.is_set():
                        return
                    failures += 1
                    state('reconnecting' if connected_once else 'connecting', reason, failures)
                    if callable(error_callback) and (failures == 1 or failures & (failures - 1) == 0):
                        error_callback(reason, failures)
                    await asyncio.sleep(0.75 if connected_once else min(5, 0.25 * (2 ** min(failures, 4))))
        finally:
            if watcher is not None:
                watcher.cancel()
            if callable(getattr(stop_event, 'unbind_task', None)):
                stop_event.unbind_task()

    try:
        asyncio.run(consume())
    except asyncio.CancelledError:
        pass


class SubmissionCommitSignal:
    """Wake exact verification when mappings commit despite missing SSE events.

    The fingerprint only avoids repeating the same SQLite query; it is not a
    completion verdict or liveness clock. No LanceDB writer is opened here.
    """

    def __init__(self, storage_dir, workspace_slug):
        self.database = Path(storage_dir) / 'anythingllm.db'
        self.workspace_slug = workspace_slug
        self.last_key = None
        self.complete = False

    def observe(self, locations):
        expected = tuple(sorted(set(str(path) for path in locations if str(path))))
        if not expected:
            return False
        try:
            fingerprint = []
            for path in (self.database, Path(str(self.database) + '-wal')):
                try:
                    info = path.stat()
                    fingerprint.append((info.st_size, info.st_mtime_ns))
                except FileNotFoundError:
                    fingerprint.append(None)
            key = (expected, tuple(fingerprint))
            if key == self.last_key:
                return self.complete
            present = set()
            # A tiny, strictly read-only, bounded query. Busy/unavailable
            # storage is uncertainty, not an upload failure or retry license.
            with closing(sqlite3.connect(self.database.resolve().as_uri() + '?mode=ro',
                                         uri=True, timeout=0.025)) as connection:
                for start in range(0, len(expected), 256):
                    portion = expected[start:start + 256]
                    placeholders = ','.join('?' for _ in portion)
                    rows = connection.execute(
                        'SELECT wd.docpath FROM workspace_documents wd '
                        'JOIN workspaces w ON w.id=wd.workspaceId '
                        f'WHERE w.slug=? AND wd.docpath IN ({placeholders}) '
                        'AND EXISTS (SELECT 1 FROM document_vectors dv WHERE dv.docId=wd.docId)',
                        (self.workspace_slug, *portion),
                    )
                    present.update(row[0] for row in rows)
            self.last_key = key
            self.complete = present == set(expected)
            return self.complete
        except (OSError, sqlite3.Error):
            # Do not memoize a failed observation: it can resolve without
            # changing the database fingerprint (e.g. a short sharing lock).
            return False

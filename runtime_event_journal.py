"""Append-only queue evidence beside bounded recovery summaries."""

import json
import os
from pathlib import Path
import threading
from contextlib import contextmanager
from contextvars import ContextVar

_LOCK = threading.Lock()
_PENDING = ContextVar('pending_runtime_event_journals', default=frozenset())


@contextmanager
def observation_scope(path):
    """Intermediate ledgers cannot claim that an active SSE stream is complete."""
    if path is None:
        yield
        return
    token = _PENDING.set(_PENDING.get() | {Path(path).resolve()})
    try:
        yield
    finally:
        _PENDING.reset(token)


def retain_runtime_events(path, events):
    path = Path(path).resolve()
    with _LOCK:
        sequence, previous, total = 1, [], 0
        if path.exists():
            with path.open(encoding='utf8') as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    if row['sequence'] != sequence:
                        if row['sequence'] != sequence + 1:
                            raise ValueError('Invalid runtime event journal sequence')
                        sequence, previous = row['sequence'], []
                    if row['position'] != len(previous):
                        raise ValueError('Invalid runtime event journal position')
                    previous.append(row['event'])
                    total += 1
        events = list(events)
        if previous == events[:len(previous)]:
            additions = events[len(previous):]
            first = len(previous)
        elif events == previous[:len(events)]:
            return {'path': str(path), 'sequence': sequence, 'retained_events': total,
                    'sequence_events': len(previous), 'observation_pending': path in _PENDING.get()}
        else:
            # A resumed caller can supply a new observation sequence instead
            # of the old prefix. Preserve both; never overwrite older events.
            sequence += 1
            additions = events
            first = 0
        if additions:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('a', encoding='utf8', newline='\n') as handle:
                for position, event in enumerate(additions, first):
                    handle.write(json.dumps({'sequence': sequence, 'position': position,
                                             'event': event}, ensure_ascii=False,
                                            separators=(',', ':')) + '\n')
                handle.flush()
                os.fsync(handle.fileno())
        total += len(additions)
        return {'path': str(path), 'sequence': sequence, 'retained_events': total,
                'sequence_events': len(events), 'observation_pending': path in _PENDING.get()}

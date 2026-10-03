"""Append-only queue evidence beside bounded recovery summaries."""

import json
import os
from pathlib import Path
import threading

_LOCK = threading.Lock()
_STATE = {}


def retain_runtime_events(path, events):
    path = Path(path).resolve()
    with _LOCK:
        if path not in _STATE:
            rows = [json.loads(line) for line in path.read_text(encoding='utf8').splitlines()
                    if line.strip()] if path.exists() else []
            sequence = max((row['sequence'] for row in rows), default=1)
            previous = [row['event'] for row in rows if row['sequence'] == sequence]
            _STATE[path] = (sequence, previous, len(rows))
        sequence, previous, total = _STATE[path]
        events = list(events)
        if previous == events[:len(previous)]:
            additions = events[len(previous):]
            first = len(previous)
        elif events == previous[:len(events)]:
            return {'path': str(path), 'sequence': sequence, 'retained_events': total}
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
        _STATE[path] = (sequence, json.loads(json.dumps(events)), total)
        return {'path': str(path), 'sequence': sequence, 'retained_events': total}

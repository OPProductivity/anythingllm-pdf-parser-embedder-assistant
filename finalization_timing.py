"""Small local finalization timings, independent of ETA and queue state."""
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import time


def finalization_checkpoint(root, step, started):
    finished = time.perf_counter()
    record = {
        'recorded_at_utc': datetime.now(timezone.utc).isoformat(),
        'step': str(step),
        'elapsed_seconds': round(max(0.0, finished - started), 6),
    }
    try:
        with (Path(root) / 'finalization-timing.jsonl').open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(record, separators=(',', ':')) + '\n')
    except OSError:
        logging.getLogger(__name__).warning('Could not retain finalization timing', exc_info=True)
    return time.perf_counter()

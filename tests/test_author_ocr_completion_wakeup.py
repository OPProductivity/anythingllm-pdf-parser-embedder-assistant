"""Region completion wakes the owner without changing result/failure order."""

from queue import Queue
from threading import Event
from unittest.mock import patch

import pytest

from author_metadata import evidence_collection as acquisition

pytestmark = pytest.mark.offline_deterministic


def test_completion_notifies_waiting_owner_including_failed_regions():
    waiting = Event()
    notices = []

    class ObservedQueue(Queue):
        def put(self, item, *args, **kwargs):
            notices.append(item)
            return super().put(item, *args, **kwargs)

        def get(self, block=True, timeout=None):
            if timeout is not None:
                waiting.set()
                timeout = 2
            return super().get(block=block, timeout=timeout)

    tasks = [{"page": page, "clip": [0, 0, 10, 10]} for page in (1, 2)]

    def recognize(_prepared, task, _executable):
        assert waiting.wait(2), "Owner must be able to wait while recognition runs"
        if task["page"] == 2:
            raise RuntimeError("controlled OCR failure")
        return [{"text": "Retained author evidence"}], [{"status": "read"}]

    with patch("queue.Queue", ObservedQueue), patch.object(
        acquisition, "_prepare_region", return_value=object()
    ), patch.object(acquisition, "replace", side_effect=lambda value, **_: value), patch.object(
        acquisition, "_read_region", recognize
    ):
        rows = list(acquisition._read_regions(None, tasks, "unused", workers=2))

    assert [task["page"] for task, _, _ in rows] == [1, 2]
    assert rows[0][1] == [{"text": "Retained author evidence"}]
    assert rows[1][1] == []
    assert rows[1][2][0]["status"] == "failed"
    assert rows[1][2][0]["error"] == "controlled OCR failure"
    assert len(notices) == 2
    assert all(item is notices[0] for item in notices)

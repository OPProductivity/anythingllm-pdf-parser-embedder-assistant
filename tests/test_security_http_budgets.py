import io
import asyncio
from unittest.mock import patch

import pytest

from authenticated_http import (read_bounded_response, ResponseBudgetExceeded,
                                validate_authenticated_url, authenticated_opener)

pytestmark = pytest.mark.offline_deterministic


@pytest.mark.parametrize("url", ["http://remote.invalid/api", "http://127.0.0.1.evil.invalid",
                                "https://user:pass@remote.invalid", "file:///private/key"])  # pragma: allowlist secret - dummy URL rejection fixture
def test_unsafe_authenticated_urls_are_rejected_before_transport(url):
    import urllib.request
    with patch.object(urllib.request, "build_opener") as transport:
        with pytest.raises(ValueError):
            authenticated_opener(urllib.request.Request(url))
    transport.assert_not_called()


@pytest.mark.parametrize("url", ["http://127.0.0.1:3001/api", "http://localhost:3001",
                                "http://[::1]:3001/api", "https://remote.invalid/api"])
def test_supported_authenticated_urls(url):
    assert validate_authenticated_url(url).hostname


@pytest.mark.parametrize("declared", [None, "20", "-1", "invalid"])
def test_response_budget_rejects_oversized_or_invalid_bodies(declared):
    response = io.BytesIO(b"x" * 20)
    response.headers = {"Content-Length": declared} if declared is not None else {}
    with pytest.raises(ResponseBudgetExceeded):
        read_bounded_response(response, 10)
    assert response.tell() <= 11


def test_exact_budget_preserves_all_bytes():
    response = io.BytesIO(b"x" * 10)
    response.headers = {"Content-Length": "10"}
    assert read_bounded_response(response, 10) == b"x" * 10


def test_small_stream_events_are_not_delayed_and_split_utf8_is_preserved():
    from ingestion_observation import bounded_stream_lines
    class Stream:
        async def aiter_raw(self):
            yield b"data: caf\xc3"
            yield b"\xa9\r\n\r\n"
            raise RuntimeError("Later read should not precede delivery")
    async def check():
        lines = bounded_stream_lines(Stream())
        assert await anext(lines) == "data: caf\u00e9"
        assert await anext(lines) == ""
        await lines.aclose()
    asyncio.run(check())


def test_unterminated_stream_line_is_bounded(monkeypatch):
    import ingestion_observation as observer
    monkeypatch.setattr(observer, "MAX_STREAM_LINE_BYTES", 10)
    class Stream:
        async def aiter_raw(self):
            yield b"x" * 11
    async def check():
        with pytest.raises(ResponseBudgetExceeded):
            async for _ in observer.bounded_stream_lines(Stream()):
                pass
    asyncio.run(check())

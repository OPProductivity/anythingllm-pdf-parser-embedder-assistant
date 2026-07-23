"""Focused regression tests for the isolated runtime logging utility."""

from __future__ import annotations

import json

import pytest

from structured_logging import configure_structured_logger


pytestmark = pytest.mark.offline_deterministic


def test_structured_logger_writes_timestamped_redacted_jsonl(tmp_path):
    log_path = tmp_path / "Logs" / "app.jsonl"
    logger = configure_structured_logger("tests.structured_logging.redaction", log_path)
    try:
        logger.info(
            "upload failed for api_key=sk-test-not-a-real-key at C:\\Users\\Example\\secret.pdf",
            extra={"event": "upload_failed", "run_id": "run-20260715"},
        )
        for handler in logger.handlers:
            handler.flush()

        payload = json.loads(log_path.read_text(encoding="utf-8").strip())
        assert payload["timestamp"].endswith("Z")
        assert payload["event"] == "upload_failed"
        assert payload["run_id"] == "run-20260715"
        assert "sk-test-not-a-real-key" not in payload["message"]
        assert "C:\\Users\\Example" not in payload["message"]
        assert "[REDACTED]" in payload["message"]
        assert "[LOCAL_PATH]" in payload["message"]
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()

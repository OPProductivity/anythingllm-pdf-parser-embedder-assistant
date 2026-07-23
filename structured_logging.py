"""Small, dependency-free structured logging helpers for the local app.

The UI process keeps its normal human-readable console output.  A rotating
JSONL file is added for incident reconstruction without routing Gradio's root
logger or changing any event/callback behaviour.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path


_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:api[_-]?key|token|secret|password)\b\s*(?:=|:)\s*)([^\s,;]+)"
)
_BEARER_TOKEN = re.compile(r"(?i)(\bBearer\s+)([^\s,;]+)")
_SENSITIVE_QUERY_VALUE = re.compile(
    r"(?i)([?&](?:api[_-]?key|token|secret|password)=)([^&#\s]+)"
)
_WINDOWS_PATH = re.compile(r"(?i)\b[A-Z]:\\(?:[^\s\"']+\\)*[^\s\"']*")
_UNIX_PATH = re.compile(r"(?<![\w:])/(?:[^\s\"']+/)*[^\s\"']+")


def redact_log_value(value: object) -> str:
    """Return a log-safe representation without credentials or local paths."""
    text = str(value)
    text = _SENSITIVE_ASSIGNMENT.sub(r"\1[REDACTED]", text)
    text = _BEARER_TOKEN.sub(r"\1[REDACTED]", text)
    text = _SENSITIVE_QUERY_VALUE.sub(r"\1[REDACTED]", text)
    text = _WINDOWS_PATH.sub("[LOCAL_PATH]", text)
    return _UNIX_PATH.sub("[LOCAL_PATH]", text)


class _LogSafetyFilter(logging.Filter):
    """Normalize log records before either installed handler formats them."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = redact_log_value(getattr(record, "run_id", ""))
        record.event = redact_log_value(getattr(record, "event", ""))
        record.msg = redact_log_value(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = redact_log_value(logging.Formatter().formatException(record.exc_info))
        return True


class JsonLinesFormatter(logging.Formatter):
    """One compact, timestamped, redacted JSON object per log record."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", "") or "log",
            "message": record.getMessage(),
        }
        if getattr(record, "run_id", ""):
            payload["run_id"] = record.run_id
        if record.exc_info:
            payload["exception"] = redact_log_value(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def configure_structured_logger(
    name: str,
    log_path: Path,
    *,
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 6,
) -> logging.Logger:
    """Configure an app-specific console + rotating JSONL logger once.

    Failure to open the file is intentionally non-fatal: normal console logging
    remains available and the local UI can still start.
    """
    logger = logging.getLogger(name)
    if any(getattr(handler, "_rag_structured_handler", False) for handler in logger.handlers):
        return logger

    logger.setLevel(logging.INFO)
    logger.propagate = False
    safety_filter = _LogSafetyFilter()

    console = logging.StreamHandler()
    console._rag_structured_handler = True  # type: ignore[attr-defined]
    console.addFilter(safety_filter)
    console.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s [run=%(run_id)s] %(message)s")
    )
    logger.addHandler(console)

    try:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler._rag_structured_handler = True  # type: ignore[attr-defined]
        file_handler.addFilter(safety_filter)
        file_handler.setFormatter(JsonLinesFormatter())
        logger.addHandler(file_handler)
    except OSError:
        # Do not make a read-only or temporarily locked Logs directory prevent
        # a local document run. The console handler above still records events.
        pass
    return logger

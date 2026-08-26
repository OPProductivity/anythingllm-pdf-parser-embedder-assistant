"""Durable append-only source-transaction state shared by upload and recovery.

The small ledger is the published contract.  While a run is active, the
append-only journal is authoritative for each source's latest state.  A
terminal run replaces the small ledger with a complete snapshot, but retains
the journal as crash evidence.

Initialization is deliberately non-destructive: an existing ledger or event
journal is evidence from an earlier invocation and must never be truncated by
an accidental re-entry into the same run directory.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from run_control import atomic_write_json


JOURNAL_SCHEMA_VERSION = 1
LEDGER_NAME = "source-transaction-ledger.json"
EVENT_JOURNAL_NAME = "source-transaction-events.jsonl"


class ExistingSourceTransactionEvidence(RuntimeError):
    """Raised before mutation when a run root already contains evidence."""


def transaction_paths(run_root: str | Path) -> tuple[Path, Path]:
    root = Path(run_root)
    return root / LEDGER_NAME, root / EVENT_JOURNAL_NAME


def initialize_source_transaction_journal(
    ledger_path: str | Path,
    *,
    workspace_slug: str,
    run_id: str,
    transaction_count: int,
) -> Path:
    """Publish a new journal contract without overwriting prior evidence."""
    ledger = Path(ledger_path)
    event_path = ledger.with_name(EVENT_JOURNAL_NAME)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    existing = [path.name for path in (ledger, event_path) if path.exists()]
    if existing:
        raise ExistingSourceTransactionEvidence(
            "source transaction evidence already exists: " + ", ".join(existing)
        )

    # Exclusive creation prevents two workers from claiming the same run root.
    descriptor = os.open(event_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        atomic_write_json(ledger, {
            "workspace_slug": str(workspace_slug or ""),
            "run_id": str(run_id or ""),
            "transaction_count": max(0, int(transaction_count or 0)),
            "transactions": [],
            "transaction_event_journal": event_path.name,
            "journal_finalized": False,
            "stopped_after_source_transaction": None,
            "stop_reason": "",
        })
    except BaseException:
        # A normal write failure occurred before this function returned and
        # therefore before the caller can mutate AnythingLLM. Remove only the
        # empty file created by this invocation. A process death bypasses this
        # branch and intentionally leaves the reservation as crash evidence.
        try:
            if event_path.stat().st_size == 0:
                event_path.unlink()
        except OSError:
            pass
        raise
    return event_path


def append_source_transaction_event(
    event_path: str | Path,
    transaction: dict[str, Any],
    *,
    stopped_after_source_transaction: int | None = None,
    stop_reason: str = "",
) -> None:
    """Durably append one complete source state transition."""
    if not isinstance(transaction, dict):
        raise TypeError("transaction must be a dictionary")
    source_index = int(transaction.get("source_index") or 0)
    if source_index < 1:
        raise ValueError("transaction requires a positive source_index")
    path = Path(event_path)
    row = {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "recorded_at_epoch": time.time(),
        "transaction": dict(transaction),
        "stopped_after_source_transaction": stopped_after_source_transaction,
        "stop_reason": str(stop_reason or ""),
    }
    encoded = json.dumps(row, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def finalize_source_transaction_journal(
    ledger_path: str | Path,
    *,
    workspace_slug: str,
    run_id: str,
    transaction_count: int,
    transactions: list[dict[str, Any]],
    stopped_after_source_transaction: int | None = None,
    stop_reason: str = "",
) -> None:
    """Atomically publish the complete terminal compatibility snapshot."""
    ledger = Path(ledger_path)
    event_path = ledger.with_name(EVENT_JOURNAL_NAME)
    if not ledger.is_file() or not event_path.is_file():
        raise FileNotFoundError("source transaction journal was not initialized")
    atomic_write_json(ledger, {
        "workspace_slug": str(workspace_slug or ""),
        "run_id": str(run_id or ""),
        "transaction_count": max(0, int(transaction_count or 0)),
        "transactions": [dict(row) for row in transactions],
        "transaction_event_journal": event_path.name,
        "journal_finalized": True,
        "stopped_after_source_transaction": stopped_after_source_transaction,
        "stop_reason": str(stop_reason or ""),
    })


def materialize_source_transaction_journal(
    root: str | Path,
    ledger: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Overlay each source's latest complete event onto an active ledger."""
    journal_name = str(ledger.get("transaction_event_journal") or "").strip()
    if not journal_name or bool(ledger.get("journal_finalized")):
        return ledger, []
    journal_path = Path(root) / Path(journal_name).name
    if not journal_path.is_file():
        return ledger, ["source_transaction_event_journal_missing"]
    latest: dict[int, dict[str, Any]] = {}
    stopped_after = ledger.get("stopped_after_source_transaction")
    stop_reason = str(ledger.get("stop_reason") or "")
    errors: list[str] = []
    try:
        with journal_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                    transaction = event.get("transaction") if isinstance(event, dict) else None
                    if not isinstance(transaction, dict):
                        raise ValueError("event has no transaction object")
                    source_index = int(transaction.get("source_index") or 0)
                    if source_index < 1:
                        raise ValueError("event has no valid source index")
                    latest[source_index] = transaction
                    if event.get("stopped_after_source_transaction") is not None:
                        stopped_after = event.get("stopped_after_source_transaction")
                    if str(event.get("stop_reason") or ""):
                        stop_reason = str(event.get("stop_reason") or "")
                except (json.JSONDecodeError, TypeError, ValueError):
                    errors.append(f"malformed_source_transaction_event_line:{line_number}")
    except (OSError, UnicodeError) as exc:
        errors.append(f"source_transaction_event_read_error:{type(exc).__name__}")
    if not latest and int(ledger.get("transaction_count") or 0) > 0 and not errors:
        errors.append("source_transaction_journal_has_no_source_event")
    materialized = dict(ledger)
    materialized["transactions"] = [latest[index] for index in sorted(latest)]
    materialized["stopped_after_source_transaction"] = stopped_after
    materialized["stop_reason"] = stop_reason
    return materialized, errors

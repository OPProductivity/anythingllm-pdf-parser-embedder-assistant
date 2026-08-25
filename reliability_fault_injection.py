"""Process-boundary transport fault acceptance for the PDF assistant.

The harness uses a real loopback HTTP server in a separate process and the
production durable ledger/receipt writers.  It never contacts AnythingLLM.
Its purpose is to prove request ordering and replay decisions under faults
that in-process mocks cannot represent faithfully.
"""

from __future__ import annotations

import argparse
import http.client
import json
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from auto_anythingllm_pipeline import (
    append_jsonl_receipt,
    get_json,
    post_json,
    record_submission_receipt,
)
from prepared_recovery import build_prepared_recovery_plan
from reliability_audit import audit_run_directory
from run_control import atomic_write_json


SCHEMA = "anythingllm_pdf_assistant_transport_fault_acceptance_v1"
SOURCE_HASHES = ("a" * 64, "b" * 64)
CRASH_SAFE_TIMEOUT_SECONDS = 20


def _append(path: Path, row: dict[str, Any]) -> None:
    append_jsonl_receipt(path, {
        "recorded_at": datetime.now(UTC).isoformat(),
        **row,
    })


class _FaultHandler(BaseHTTPRequestHandler):
    server_version = "PDFReliabilityFaultServer/1"

    def log_message(self, *_args):
        return

    @property
    def scenario(self) -> str:
        return str(getattr(self.server, "scenario", ""))

    @property
    def journal(self) -> Path:
        return Path(getattr(self.server, "journal"))

    def _reply(self, status: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        if self.path == "/health":
            self._reply(200, {"ok": True})
            return
        if self.path.startswith("/vectors/"):
            count = int(getattr(self.server, "vector_observations", 0)) + 1
            setattr(self.server, "vector_observations", count)
            _append(self.journal, {
                "event": "vector_observation",
                "attempt": count,
                "path": self.path,
            })
            if self.scenario == "sqlite_busy_then_vectors" and count <= 2:
                self._reply(503, {"status": "busy"})
            elif self.scenario in {"delayed_vectors", "sqlite_busy_then_vectors"} and count <= 2:
                self._reply(200, {"confirmed": False})
            else:
                self._reply(200, {"confirmed": True})
            return
        self._reply(404, {"error": "not_found"})

    def do_POST(self):
        length = max(0, int(self.headers.get("Content-Length") or 0))
        body = self.rfile.read(length)
        source_index = int(self.path.rsplit("/", 1)[-1])
        _append(self.journal, {
            "event": "request_received",
            "source_index": source_index,
            "body_sha256_present": bool(body),
        })
        if self.scenario == "definite_rejection_then_success" and source_index == 1:
            self._reply(422, {"error": "fixture rejection"})
            return
        location = f"custom-documents/source-{source_index}.json"
        _append(self.journal, {
            "event": "mutation_accepted",
            "source_index": source_index,
            "location": location,
        })
        if self.scenario == "lost_response_after_acceptance":
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        self._reply(200, {"location": location})


def serve_faults(port: int, scenario: str, journal: Path) -> int:
    server = ThreadingHTTPServer(("127.0.0.1", port), _FaultHandler)
    server.scenario = scenario
    server.journal = str(journal)
    server.vector_observations = 0
    server.serve_forever(poll_interval=0.05)
    return 0


def _payload(source_index: int) -> dict[str, Any]:
    return {
        "filename": f"source-{source_index}.txt",
        "textContent": f"prepared source {source_index}",
        "metadata": {
            "docSource": f"local-pdf://sha256/{SOURCE_HASHES[source_index - 1]}",
            "chunkSource": f"page-parent://source-{source_index}::p1",
        },
    }


def _persist_source_ledger(root: Path, transactions: list[dict[str, Any]], stop_reason: str = "") -> None:
    held = bool(stop_reason)
    atomic_write_json(root / "source-transaction-ledger.json", {
        "workspace_slug": "fault-workspace",
        "run_id": root.name,
        "transaction_count": len(transactions),
        "transactions": transactions,
        "stopped_after_source_transaction": len(transactions) if held else None,
        "stop_reason": stop_reason,
    })


def _terminal_artifacts(root: Path, transactions: list[dict[str, Any]], *, held: bool) -> None:
    uploaded = sum(int(row.get("uploaded") or 0) for row in transactions)
    embedded = sum(int(row.get("embedded") or 0) for row in transactions)
    locations = [
        location for row in transactions for location in (row.get("locations") or [])
    ]
    atomic_write_json(root / "batch-native-upload-report.json", {
        "status": "reconciliation_pending" if held else (
            "error" if any(row.get("state") == "source_rejected_without_remote_mutation" for row in transactions)
            else "complete"
        ),
        "uploaded": uploaded,
        "embedded": embedded,
        "locations": locations,
        "source_transactions": transactions,
    })
    remaining = ["custom-documents/ambiguous.json"] if held else []
    atomic_write_json(root / "batch-embedding-ledger.json", {
        "workspace_slug": "fault-workspace",
        "requested": uploaded,
        "accepted": embedded,
        "recovery": {
            "state": "resume_available" if held else "not_needed",
            "remaining_locations": remaining,
        },
    })
    has_rejection = any(
        row.get("state") == "source_rejected_without_remote_mutation"
        for row in transactions
    )
    atomic_write_json(root / "run-progress.json", {
        # An explicit source-local rejection is safely non-blocking for later
        # sources, but the batch as a whole is not an unqualified success.
        "state": "warning" if held or has_rejection else "successful",
        "completed_units": len(transactions),
        "total_units": len(transactions),
    })


def run_client(root: Path, scenario: str, endpoint: str) -> int:
    root.mkdir(parents=True, exist_ok=True)
    receipts = root / "batch-submission-receipts.jsonl"
    source_count = 2 if scenario == "definite_rejection_then_success" else 1
    transactions: list[dict[str, Any]] = []
    stop_reason = ""
    for source_index in range(1, source_count + 1):
        payload = _payload(source_index)
        transaction = {
            "source_index": source_index,
            "source_count": source_count,
            "source_sha256": SOURCE_HASHES[source_index - 1],
            "planned_records": 1,
            "state": "attachment_intent_durable",
            "mutation_scope": "current_source",
        }
        transactions.append(transaction)
        _persist_source_ledger(root, transactions)
        record_submission_receipt(
            receipts,
            payload,
            run_id=root.name,
            workspace_slug="fault-workspace",
            transport="raw_text",
            state="submitted",
            correlation_id=f"source-{source_index}",
            next_check="reconcile before replay",
        )
        try:
            status, response = post_json(
                f"{endpoint}/source/{source_index}",
                {key: payload[key] for key in ("filename", "textContent", "metadata")},
                timeout=1.0,
            )
            if status < 200 or status >= 300:
                raise RuntimeError(f"unexpected status {status}")
            location = str((json.loads(response) or {}).get("location") or "")
            record_submission_receipt(
                receipts, payload, run_id=root.name, workspace_slug="fault-workspace",
                transport="raw_text", state="attached", correlation_id=f"source-{source_index}",
                http_status=status, location=location, next_check="verify exact vectors",
            )
            confirmed = scenario not in {"delayed_vectors", "sqlite_busy_then_vectors"}
            for _attempt in range(4):
                if confirmed:
                    break
                try:
                    _status, text = get_json(
                        f"{endpoint}/vectors/{source_index}", timeout=0.5,
                    )
                    confirmed = bool((json.loads(text) or {}).get("confirmed"))
                except urllib.error.HTTPError as exc:
                    if exc.code != 503:
                        raise
                if not confirmed:
                    time.sleep(0.05)
            if not confirmed:
                raise TimeoutError("exact vectors remained unavailable")
            transaction.update(
                state="exact_vectors_proven", uploaded=1, embedded=1,
                locations=[location], errors=[],
            )
            _persist_source_ledger(root, transactions)
        except urllib.error.HTTPError as exc:
            if exc.code != 422:
                raise
            record_submission_receipt(
                receipts, payload, run_id=root.name, workspace_slug="fault-workspace",
                transport="raw_text", state="rejected", correlation_id=f"source-{source_index}",
                http_status=exc.code, error="explicit rejection", next_check="later source may continue",
            )
            transaction.update(
                state="source_rejected_without_remote_mutation", uploaded=0,
                embedded=0, locations=[], errors=[{"classification": "explicit_rejection"}],
                later_sources_released=True,
            )
            _persist_source_ledger(root, transactions)
        except (
            TimeoutError,
            urllib.error.URLError,
            http.client.RemoteDisconnected,
            ConnectionError,
            OSError,
        ):
            transaction.update(
                state="ambiguous_external_mutation_held", uploaded=0,
                embedded=0, locations=[], errors=[{"classification": "ambiguous_transport"}],
                later_sources_released=False,
            )
            stop_reason = "ambiguous_external_mutation_held"
            _persist_source_ledger(root, transactions, stop_reason)
            break
    _terminal_artifacts(root, transactions, held=bool(stop_reason))
    return 0


SCENARIOS = {
    "definite_rejection_then_success": {
        "expected_requests": [1, 2],
        "expected_actions": ["preserve_rejection_and_continue", "preserve_completed"],
    },
    "lost_response_after_acceptance": {
        "expected_requests": [1],
        "expected_actions": ["hold_for_reconciliation"],
    },
    "connection_refused_before_request": {
        "expected_requests": [],
        "expected_actions": ["hold_for_reconciliation"],
        "no_server": True,
    },
    "delayed_vectors": {
        "expected_requests": [1],
        "expected_actions": ["preserve_completed"],
    },
    "sqlite_busy_then_vectors": {
        "expected_requests": [1],
        "expected_actions": ["preserve_completed"],
    },
}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_server(endpoint: str) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{endpoint}/health", timeout=0.2) as response:
                if response.status == 200:
                    return
        except (OSError, TimeoutError, http.client.HTTPException, urllib.error.URLError):
            time.sleep(0.03)
    raise RuntimeError("fault server did not become ready")


def _read_journal(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def run_transport_fault_acceptance(output_root: str | Path) -> dict[str, Any]:
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    results = []
    for scenario, expected in SCENARIOS.items():
        scenario_root = root / scenario
        journal = scenario_root / "fault-server-journal.jsonl"
        scenario_root.mkdir(parents=True, exist_ok=True)
        port = _free_port()
        endpoint = f"http://127.0.0.1:{port}"
        server = None
        if not expected.get("no_server"):
            server = subprocess.Popen(
                [
                    sys.executable, "-m", "reliability_fault_injection",
                    "--server-worker", "--port", str(port),
                    "--scenario", scenario, "--journal", str(journal),
                ],
                cwd=Path(__file__).resolve().parent,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            _wait_for_server(endpoint)
        try:
            client = subprocess.run(
                [
                    sys.executable, "-m", "reliability_fault_injection",
                    "--client-worker", "--run-root", str(scenario_root),
                    "--scenario", scenario, "--endpoint", endpoint,
                ],
                cwd=Path(__file__).resolve().parent,
                capture_output=True,
                text=True,
                timeout=CRASH_SAFE_TIMEOUT_SECONDS,
                check=False,
            )
        finally:
            if server is not None:
                server.terminate()
                try:
                    server.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=3)
        journal_rows = _read_journal(journal)
        requests = [
            int(row.get("source_index") or 0)
            for row in journal_rows if row.get("event") == "request_received"
        ]
        recovery = build_prepared_recovery_plan(scenario_root)
        actions = [row.get("action") for row in recovery.get("sources") or []]
        audit = audit_run_directory(scenario_root)
        passed = (
            client.returncode == 0
            and requests == expected["expected_requests"]
            and actions == expected["expected_actions"]
            and audit.get("audit_status") == "pass"
        )
        results.append({
            "scenario": scenario,
            "status": "pass" if passed else "fail",
            "client_exit": client.returncode,
            "request_sources": requests,
            "expected_request_sources": expected["expected_requests"],
            "restart_actions": actions,
            "expected_restart_actions": expected["expected_actions"],
            "integrity_audit": audit.get("audit_status"),
            "mutation_acceptances": sum(
                1 for row in journal_rows if row.get("event") == "mutation_accepted"
            ),
            "vector_observations": sum(
                1 for row in journal_rows if row.get("event") == "vector_observation"
            ),
        })
    report = {
        "schema": SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "pass" if all(row["status"] == "pass" for row in results) else "fail",
        "scenario_count": len(results),
        "results": results,
    }
    atomic_write_json(root / "transport-fault-acceptance-report.json", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run transport fault acceptance.")
    parser.add_argument("--output-root", default="")
    parser.add_argument("--server-worker", action="store_true")
    parser.add_argument("--client-worker", action="store_true")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--scenario", choices=tuple(SCENARIOS), default="")
    parser.add_argument("--journal", default="")
    parser.add_argument("--run-root", default="")
    parser.add_argument("--endpoint", default="")
    args = parser.parse_args(argv)
    if args.server_worker:
        return serve_faults(args.port, args.scenario, Path(args.journal))
    if args.client_worker:
        return run_client(Path(args.run_root), args.scenario, args.endpoint)
    if args.output_root:
        report = run_transport_fault_acceptance(args.output_root)
    else:
        with tempfile.TemporaryDirectory(prefix="anythingllm-pdf-transport-fault-") as temp_dir:
            report = run_transport_fault_acceptance(temp_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

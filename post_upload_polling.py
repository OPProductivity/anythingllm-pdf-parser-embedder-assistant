"""Evidence-accumulating polling for asynchronous AnythingLLM ingestion."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Collection, Literal, Mapping, Protocol, TypedDict, cast

from validation_contract import post_upload_status_class


LOGGER = logging.getLogger(__name__)

OperatorStatus = Literal["pass", "pass_with_review", "error", "incomplete"]
PollingStatus = Literal["pass", "pass_with_review", "error", "timeout"]
Evidence = dict[str, Any]
EvidenceInput = Mapping[str, Any]


class PollingInspector(Protocol):
    def __call__(self) -> EvidenceInput | None:
        """Return the latest AnythingLLM ingestion evidence."""


class ObservationCallback(Protocol):
    def __call__(self, evidence: Evidence, operator_status: OperatorStatus, /) -> None:
        """Observe progress without changing the polling verdict."""


class ObserverFailure(TypedDict):
    attempt: int
    operator_status: OperatorStatus
    exception_type: str
    message: str


@dataclass
class PollingResult:
    status: PollingStatus
    evidence_code: str
    attempts: int
    elapsed_seconds: float
    observations: list[Evidence] = field(default_factory=list)
    final_evidence: Evidence = field(default_factory=dict)
    observer_failures: list[ObserverFailure] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return cast(dict[str, Any], asdict(self))


@dataclass(frozen=True)
class PollingPolicy:
    interval_seconds: float = 2.0
    timeout_seconds: float = 60.0
    hard_cap_seconds: float = 90.0

    @classmethod
    def from_values(
        cls,
        *,
        interval_seconds: float = 2.0,
        timeout_seconds: float = 60.0,
        hard_cap_seconds: float = 90.0,
    ) -> "PollingPolicy":
        hard_cap = max(0.0, float(hard_cap_seconds))
        return cls(
            interval_seconds=max(0.0, float(interval_seconds)),
            timeout_seconds=min(max(0.0, float(timeout_seconds)), hard_cap),
            hard_cap_seconds=hard_cap,
        )


def operator_status(evidence: EvidenceInput) -> OperatorStatus:
    internal = str(evidence.get("status") or evidence.get("classification") or "")
    classification = post_upload_status_class(internal, concurrent_writes_are_transient=True)
    return cast(OperatorStatus, "pass_with_review" if classification == "review" else classification)


def poll_post_upload(
    inspector: PollingInspector,
    interval_seconds: float = 2.0,
    timeout_seconds: float = 60.0,
    hard_cap_seconds: float = 90.0,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    observation_callback: ObservationCallback | None = None,
    retryable_evidence_codes: Collection[str] = (),
) -> PollingResult:
    policy = PollingPolicy.from_values(
        interval_seconds=interval_seconds,
        timeout_seconds=timeout_seconds,
        hard_cap_seconds=hard_cap_seconds,
    )
    timeout = policy.timeout_seconds
    started = monotonic()
    observations: list[Evidence] = []
    observer_failures: list[ObserverFailure] = []
    retryable_codes = {str(code) for code in retryable_evidence_codes}
    attempts = 0
    while True:
        attempts += 1
        evidence = dict(inspector() or {})
        evidence["attempt"] = attempts
        evidence["observed_elapsed_seconds"] = round(monotonic() - started, 4)
        observations.append(evidence)
        status = operator_status(evidence)
        evidence_code = str(evidence.get("status") or evidence.get("classification") or "")
        # Some evidence codes are terminal in an ordinary inspection but are
        # expected intermediate states in a narrowly scoped asynchronous
        # recovery. For example, four of five vectors after an HTTP timeout is
        # progress, not proof that the fifth vector will never materialize.
        if status == "error" and evidence_code in retryable_codes:
            status = "incomplete"
        if callable(observation_callback):
            # Progress and diagnostic observers must never alter the durable
            # ingestion verdict. An upload can be fully verified even when a
            # UI/reporting callback has failed, so retain the polling result
            # and make the observer failure visible only in background logs.
            try:
                observation_callback(dict(evidence), status)
            except Exception as exc:  # observers are explicitly best-effort
                failure: ObserverFailure = {
                    "attempt": attempts,
                    "operator_status": status,
                    "exception_type": type(exc).__name__,
                    "message": str(exc) or "observer callback failed",
                }
                observer_failures.append(failure)
                LOGGER.exception(
                    "Post-upload observation callback failed; retaining polling result and recording observer evidence: %s",
                    failure,
                )
        if status in {"pass", "pass_with_review", "error"}:
            terminal_status = cast(PollingStatus, status)
            return PollingResult(
                status=terminal_status,
                evidence_code=evidence_code,
                attempts=attempts,
                elapsed_seconds=round(monotonic() - started, 4),
                observations=observations,
                final_evidence=evidence,
                observer_failures=observer_failures,
            )
        elapsed = monotonic() - started
        if elapsed >= timeout:
            return PollingResult(
                status="timeout",
                evidence_code=evidence_code or "insufficient_evidence",
                attempts=attempts,
                elapsed_seconds=round(elapsed, 4),
                observations=observations,
                final_evidence=evidence,
                observer_failures=observer_failures,
            )
        sleeper(min(policy.interval_seconds, max(0.0, timeout - elapsed)))

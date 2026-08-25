"""Redacted release certification for the PDF assistant.

This module is read-only.  It does not install packages, mutate AnythingLLM,
clean the worktree, or create a Git tag.  Its job is to keep a release from
being described as reproducible without a known source revision, rollback
revision, supported Python runtime, passing offline fault matrices, and a
recognized local Desktop mutation contract.
"""

from __future__ import annotations

import importlib.metadata
import json
import platform
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from anythingllm_compatibility import characterize
from reliability_acceptance import run_offline_crash_acceptance
from reliability_fault_injection import run_transport_fault_acceptance
from run_control import atomic_write_json


SCHEMA = "anythingllm_pdf_assistant_release_certificate_v1"
DEPENDENCIES = (
    "gradio", "PyMuPDF", "pymupdf4llm", "unstructured",
    "unstructured-inference", "lancedb", "pyarrow", "numpy", "pandas",
    "pillow", "pi-heif", "pydantic", "requests",
)
REQUIRED_MUTATION_CAPABILITIES = (
    "can_upload_native_metadata",
    "can_poll_post_upload_state",
)


def _git(repo: Path, *args: str) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True,
            check=False, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return completed.returncode, completed.stdout.strip()


def _git_state(repo: Path, rollback_ref: str) -> dict[str, Any]:
    head_code, head = _git(repo, "rev-parse", "HEAD")
    status_code, status = _git(repo, "status", "--porcelain", "--untracked-files=all")
    rollback_code, rollback = _git(repo, "rev-parse", "--verify", f"{rollback_ref}^{{commit}}") if rollback_ref else (1, "")
    return {
        "head": head if head_code == 0 else "unavailable",
        "worktree_clean": status_code == 0 and not status,
        "changed_entry_count": len(status.splitlines()) if status_code == 0 else None,
        "rollback_ref_supplied": bool(rollback_ref),
        "rollback_commit": rollback if rollback_code == 0 else "unavailable",
        "rollback_valid": rollback_code == 0 and bool(rollback),
    }


def _live_canary_passed(path: str | Path | None) -> bool:
    if not path:
        return False
    try:
        report = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(report, dict)
        and report.get("schema") == "anythingllm_pdf_assistant_grouped_live_canary_v1"
        and report.get("status") == "pass"
        and report.get("integrity_audit") == "pass"
        and report.get("ambiguous_mutation") is False
        and int(report.get("selected_pdf_count") or 0) >= 9
        and report.get("batch_scale") in {"medium", "large"}
        and report.get("workspace_retained") is False
        and report.get("document_folder_cleanup_status")
        in {"deleted", "already_absent"}
    )


def _scale_acceptance_passed(path: str | Path | None) -> bool:
    if not path:
        return False
    try:
        report = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    checks = report.get("checks") or {} if isinstance(report, dict) else {}
    required = {
        "all_sources_checkpointed",
        "all_sources_reloadable",
        "single_changed_artifact_blocks_reuse",
        "restored_artifact_revalidates",
        "submission_started_never_replays",
    }
    return bool(
        isinstance(report, dict)
        and report.get("schema") == "anythingllm_pdf_assistant_scale_acceptance_v1"
        and report.get("status") == "pass"
        and int(report.get("source_count") or 0) >= 1000
        and int(report.get("artifact_count") or 0) >= 3000
        and report.get("external_mutation_attempted") is False
        and required.issubset(checks)
        and all(checks[name] is True for name in required)
    )


def _eta_regression_passed(path: str | Path | None) -> bool:
    if not path:
        return False
    try:
        report = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    checks = report.get("checks") or {} if isinstance(report, dict) else {}
    required = {
        "workload_scale_is_monotonic",
        "ocr_reserve_does_not_reduce_estimate",
        "cache_realization_never_increases_current_eta",
        "queue_repricing_is_bounded_per_observation",
        "recalibration_waits_for_three_samples",
    }
    return bool(
        isinstance(report, dict)
        and report.get("schema") == "anythingllm_pdf_assistant_eta_regression_evidence_v1"
        and report.get("status") == "pass"
        and report.get("private_history_used") is False
        and required.issubset(checks)
        and all(checks[name] is True for name in required)
    )


def _junit_passed(
    path: str | Path | None,
    *,
    minimum_tests: int,
    maximum_skipped: int | None = None,
) -> bool:
    if not path:
        return False
    try:
        root = ET.parse(Path(path)).getroot()
    except (OSError, ET.ParseError):
        return False
    suites = [root] if root.tag == "testsuite" else list(root.findall(".//testsuite"))
    if not suites:
        return False
    tests = sum(int(suite.attrib.get("tests", 0)) for suite in suites)
    failures = sum(int(suite.attrib.get("failures", 0)) for suite in suites)
    errors = sum(int(suite.attrib.get("errors", 0)) for suite in suites)
    skipped = sum(int(suite.attrib.get("skipped", 0)) for suite in suites)
    return bool(
        tests >= minimum_tests
        and failures == 0
        and errors == 0
        and (maximum_skipped is None or skipped <= maximum_skipped)
    )


def _default_suite_passed(path: str | Path | None) -> bool:
    # The default suite currently has more than 840 cases and intentionally
    # excludes the separately executed 17-test browser layer.  The lower bound
    # rejects a focused or accidentally deselected JUnit file without coupling
    # certification to an exact test count that naturally grows over time.
    return _junit_passed(path, minimum_tests=800, maximum_skipped=20)


def _ui_acceptance_passed(path: str | Path | None) -> bool:
    return _junit_passed(path, minimum_tests=17, maximum_skipped=0)


def environment_fingerprint(repo_root: str | Path, compatibility: dict[str, Any]) -> dict[str, Any]:
    """Return operational versions without usernames, paths, or source data."""
    versions = {}
    for distribution in DEPENDENCIES:
        try:
            versions[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            versions[distribution] = "missing"
    capabilities = compatibility.get("capabilities") or {}
    desktop_package = compatibility.get("desktop_package") or {}
    return {
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
            "supported_range": ">=3.11,<3.15",
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "assistant_version": (
            (Path(repo_root) / "VERSION").read_text(encoding="utf-8").strip()
            if (Path(repo_root) / "VERSION").is_file() else "unavailable"
        ),
        "dependencies": versions,
        "anythingllm": {
            "desktop_version_normalized": compatibility.get("desktop_version_normalized") or "unavailable",
            "desktop_release_status": compatibility.get("desktop_release_status") or "unavailable",
            "matched_profile": compatibility.get("matched_profile") or "",
            "native_mutation_contract": compatibility.get("native_mutation_contract") or "",
            "app_asar_sha256": desktop_package.get("app_asar_sha256") or "",
            "storage_schema_status": compatibility.get("storage_schema_status") or "unavailable",
            "capability_statuses": {
                name: str((capabilities.get(name) or {}).get("status") or "unknown")
                for name in REQUIRED_MUTATION_CAPABILITIES
            },
        },
    }


def certify_release(
    repo_root: str | Path,
    *,
    rollback_ref: str,
    output_path: str | Path | None = None,
    storage_dir: str | Path | None = None,
    live_canary_path: str | Path | None = None,
    scale_report_path: str | Path | None = None,
    eta_report_path: str | Path | None = None,
    default_junit_path: str | Path | None = None,
    ui_junit_path: str | Path | None = None,
) -> dict[str, Any]:
    repo = Path(repo_root).resolve()
    compatibility = characterize(
        storage_dir,
        include_package_fingerprint=True,
        api_url="http://127.0.0.1:3001/api",
    )
    with tempfile.TemporaryDirectory(prefix="anythingllm-pdf-release-check-") as temp_dir:
        root = Path(temp_dir)
        crash = run_offline_crash_acceptance(root / "crash")
        transport = run_transport_fault_acceptance(root / "transport")
    git_state = _git_state(repo, rollback_ref)
    fingerprint = environment_fingerprint(repo, compatibility)
    python_supported = (3, 11) <= sys.version_info[:2] < (3, 15)
    mutation_capabilities = fingerprint["anythingllm"]["capability_statuses"]
    compatibility_ready = (
        compatibility.get("desktop_release_status") == "recognized_mutation_profile"
        and bool(compatibility.get("native_mutation_contract"))
        and all(status == "supported" for status in mutation_capabilities.values())
    )
    checks = {
        "python_supported": python_supported,
        "offline_crash_matrix": crash.get("status") == "pass",
        "transport_fault_matrix": transport.get("status") == "pass",
        "anythingllm_mutation_contract": compatibility_ready,
        "disposable_live_canary": _live_canary_passed(live_canary_path),
        "large_scale_acceptance": _scale_acceptance_passed(scale_report_path),
        "eta_regression_evidence": _eta_regression_passed(eta_report_path),
        "default_python_suite": _default_suite_passed(default_junit_path),
        "browser_ui_acceptance": _ui_acceptance_passed(ui_junit_path),
        "source_revision_available": git_state["head"] != "unavailable",
        "worktree_clean": bool(git_state["worktree_clean"]),
        "rollback_commit_valid": bool(git_state["rollback_valid"]),
    }
    report = {
        "schema": SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "pass" if all(checks.values()) else "blocked",
        "checks": checks,
        "git": git_state,
        "environment": fingerprint,
        "acceptance": {
            "crash_scenarios": crash.get("scenario_count"),
            "transport_scenarios": transport.get("scenario_count"),
        },
    }
    if output_path:
        atomic_write_json(Path(output_path), report)
    return report


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Certify a PDF assistant release candidate.")
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--rollback-ref", required=True)
    parser.add_argument("--output", default="")
    parser.add_argument("--storage-dir", default="")
    parser.add_argument("--live-canary-report", default="")
    parser.add_argument("--scale-report", default="")
    parser.add_argument("--eta-report", default="")
    parser.add_argument("--default-junit", default="")
    parser.add_argument("--ui-junit", default="")
    args = parser.parse_args(argv)
    report = certify_release(
        args.repo_root,
        rollback_ref=args.rollback_ref,
        output_path=args.output or None,
        storage_dir=args.storage_dir or None,
        live_canary_path=args.live_canary_report or None,
        scale_report_path=args.scale_report or None,
        eta_report_path=args.eta_report or None,
        default_junit_path=args.default_junit or None,
        ui_junit_path=args.ui_junit or None,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

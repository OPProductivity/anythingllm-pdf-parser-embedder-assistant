"""Planned-path validation before expensive PDF preparation or mutation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from segmentation_policy import UNKNOWN_MODEL_HARD_LIMIT, policy_for


@dataclass
class PreflightFinding:
    code: str
    severity: str
    message: str


@dataclass
class PreflightResult:
    status: str
    planned_hard_limit: int
    selected_mode: str
    required_capabilities: list[str]
    findings: list[PreflightFinding] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


def validate_planned_path(
    resolved_state,
    mode,
    target_length,
    requested_chunk_size,
    prepare_upload=False,
    run_simulation=False,
    runtime_probe=None,
):
    policy = policy_for(mode)
    compatibility = resolved_state.get("compatibility", {})
    capabilities = compatibility.get("capabilities", {})
    findings = []
    required = []

    embedder_state = resolved_state.get("embedder", {}) if isinstance(resolved_state, dict) else {}
    hard_limit_state = embedder_state.get("hard_limit")
    if isinstance(hard_limit_state, dict):
        configured_limit = hard_limit_state.get("effective")
    else:
        configured_limit = (
            embedder_state.get("max_chunk_length")
            or embedder_state.get("recommended_limit")
            or embedder_state.get("current_limit")
        )
    planned_limit = int(configured_limit or UNKNOWN_MODEL_HARD_LIMIT)

    if target_length <= 0:
        findings.append(PreflightFinding("invalid_target_length", "blocking", "Target passage length must be positive."))
    if requested_chunk_size and requested_chunk_size > planned_limit:
        findings.append(PreflightFinding(
            "chunk_size_exceeds_operational_limit",
            "blocking",
            f"Requested chunk size {requested_chunk_size} exceeds planned hard limit {planned_limit}.",
        ))
    if not policy.page_local:
        # ``none`` is an explicit operator-selected control mode: it prepares
        # the complete document as one file so AnythingLLM's own splitter can
        # be observed.  It cannot promise exact final page provenance after
        # upload, but that is a capability limitation to disclose, not a
        # reason to present an executable UI option that the runner blocks.
        if policy.mode == "none":
            findings.append(PreflightFinding(
                "mode_not_page_local",
                "warning",
                "No-local-segmentation mode delegates final boundaries to AnythingLLM; exact page provenance after upload is not guaranteed.",
            ))
        else:
            findings.append(PreflightFinding("mode_not_page_local", "blocking", "Current research modes must remain page-local."))

    if prepare_upload:
        required.extend(["can_upload_native_metadata", "can_create_temp_api_key"])
    if run_simulation:
        required.append("can_runtime_verify_embedder")
    for capability in required:
        state = capabilities.get(capability, {})
        if state.get("status") != "supported":
            findings.append(PreflightFinding(
                f"capability_{capability}_{state.get('status', 'unknown')}",
                "blocking",
                f"Planned execution requires {capability}, which is {state.get('status', 'unknown')}.",
            ))

    depends_on_runtime = prepare_upload or run_simulation
    if depends_on_runtime:
        probe_status = (runtime_probe or {}).get("status", "not_run")
        if probe_status != "pass":
            findings.append(PreflightFinding(
                "runtime_embedder_probe_required",
                "blocking",
                "The planned path depends on live embedder behavior and requires a passing runtime probe.",
            ))

    anomalies = set(resolved_state.get("anomalies") or [])
    if "generic_provider_precedence_unverified" in anomalies:
        findings.append(PreflightFinding(
            "embedder_precedence_unverified",
            "warning",
            "Generic and provider-specific embedder preferences conflict; runtime verification is recommended.",
        ))

    status = "pass"
    if any(row.severity == "blocking" for row in findings):
        status = "error"
    elif findings:
        status = "pass_with_review"
    return PreflightResult(status, planned_limit, mode, required, findings)

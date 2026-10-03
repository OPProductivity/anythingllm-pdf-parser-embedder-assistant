# Latest Codex Security Cloud scan

Repository: OPProductivity/anythingllm-pdf-parser-embedder-assistant.
Run: wfr_b780c5f057b29f0912f9837cb2f57fe5e02f1e4c98a339cd6824b5f097c01503.
Scanned commit: b83957b5b57240beec1c9eb09d9e86e99353fd39.
Completed: 2026-10-03 17:56:46 UTC (19:56:46 Europe/Amsterdam).
Status: completed; no output validation warning.

The scan targets an earlier revision, not the current local HEAD. Its output
includes a report, findings, coverage, manifest and bundled artifacts. This
review read its workflow status, output metadata and all ten finding summaries.
It did not reproduce all attacks, download/read the complete report, launch a
new scan, modify findings, or implement security changes.

## Reported findings

| Severity | Finding | Primary area |
| --- | --- | --- |
| High | Unauthenticated loopback callbacks can mutate AnythingLLM/operator state | Gradio callbacks and launch |
| High | Mutable unpinned remote installer packages | Main and Desktop bridge installers |
| High | Hostname-only service trust may disclose managed API keys | Endpoint discovery and authentication |
| Medium | Mutable source substitution under trusted PDF identity | Source hashing and worker preparation |
| Medium | Plaintext remote HTTP and implicit proxy routing | Authenticated network helpers |
| Medium | Missing aggregate PDF/extraction/OCR resource budgets | Preflight and extraction |
| Medium | Relative upload paths can traverse outside storage during relocation | Native upload relocation |
| Medium | Unbounded peer response/SSE bodies | HTTP and SSE transports |
| Low | Predictable CLI exports overwrite existing destinations | extract-pdf and segment-pdf CLI |
| Low | Some UI/recovery paths can orphan temporary API keys | Simulation, recovery, auth readiness |

These are scanner severity assessments, not evidence of compromise. Local
loopback access, a hostile endpoint, a replaced source, or an untrusted remote
package are different threat scenarios and need separate current-code review.
The latest production run's verified API key cleanup does not establish that
every other UI/recovery credential lifecycle is correct.

Narrow current checks confirmed that the UI launch still has no auth/auth
dependency, the main installer still defaults to a main-branch archive, and the
optional bridge installer still invokes unversioned npx @electron/asar.
The other findings require current-code validation before accepting, dismissing
or repairing them. No proposed security repair should silently remove existing
remote-server, installer, export, or large-PDF functionality.

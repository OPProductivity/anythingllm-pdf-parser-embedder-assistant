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

## Local remediation, 2026-10-03

The paragraphs above describe the original scan review, not the modified source.
The following changes preserve the visible interface and production OCR logic:

- Launcher-issued single-use tickets establish per-launch browser cookies;
  middleware guards mounted HTTP and WebSocket routes and rejects foreign
  hosts/origins. Health readiness remains public; existing authenticated Stop
  notifications remain available.
- Installers accept externally hash-verified offline bundles, not mutable main
  archives, online pip bootstrap, or unversioned npx. The release builder and
  procedure are documented in VERIFIED-RELEASES.md. An existing runtime is not
  modified by installation.
- Automatic Desktop credential discovery requires matching OS process, listener
  and configured storage evidence. Managed transports verify the established
  socket before sending credentials. Explicit custom-server keys remain supported.
- Windows preparation holds a read-only sharing lock and rehashes the source
  before accepting its parent identity. POSIX advisory locking is not equivalent
  to this Windows mandatory-sharing protection.
- Authenticated remote endpoints require HTTPS; implicit proxies and redirects
  are rejected. Main JSON/error and SSE readers have explicit byte budgets.
- Relocation rejects relative traversal and verifies resolved source containment.
- CLI exports are staged and refuse overwrite unless explicitly requested.
- Read-only adapter descriptions no longer create temporary keys; failed key
  cleanup creates a secret-free durable obligation record.

Approved configurable per-PDF defaults: 6 GiB source, 10,000 pages, 50 million
OCR pixels per rendered page, 2 MiB text per page, 150 MiB aggregate UTF-8 text,
100,000 segments and six hours automatic local preparation excluding embedding
waits. Exceeding a limit is an explicit failure, never silent truncation.

This is not a claim that every finding is closed. Resource runtime enforcement
is not yet uniform across CLI/advanced/preflight paths, and post-extraction text
checks do not constitute an OS memory sandbox. Remaining provider/error readers
need a complete bounds audit. A real verified release installation and complete
credential-lifecycle/transport fault matrix remain to be qualified. No follow-up
cloud scan or release publication has been performed.

# Security deployment verification

The security implementation was committed as 581fb0b. A live restart exposed
Gradio's internal unauthenticated startup-events request being rejected. That
was corrected without exposing the route: only the same server process's
established outbound socket can authorize GET startup-events or HEAD root.
Other callbacks still require the browser session.

Verification on Windows, 2026-10-03, Europe/Amsterdam:

- Baseline plus security regression suite: 2,265 passed, one skipped, 34
  deselected, 15 subtests passed; three dependency deprecation warnings.
- Additional verified release tests: two passed. Verify-only executed no
  release payload and rejected an incorrect outer hash.
- Following the startup correction: 67 targeted browser, lifecycle, shortcut,
  transport-budget and release tests passed.
- Ruff, Pyright and tracked/untracked source secret scan passed. One dummy
  basic-auth URL rejection fixture was explicitly allowlisted, not a real key.
- Both managed urllib and HTTPX connected-socket verification passed against
  the live Desktop public ping using a dummy bearer registered only in test
  process memory. No Desktop API key or database mutation was needed.
- Idle PDF assistant restarted; final root PID 5472, port 7860. Health returned
  200; unauthenticated root/config/startup-events returned 403. Launcher ticket
  session accessed root/config successfully, including all 372 components.
  Ticket replay and foreign-origin rejection were checked on the first restart;
  startup route rejection was rechecked after its correction.
- All eight pre-existing AnythingLLM processes retained unchanged creation
  times. No workspace, document, embedding, source PDF or Desktop bridge was
  modified during this deployment check.

Runtime PID is a historical deployment observation, not an ownership credential.
No new security cloud scan or verified release publication was performed.
Outstanding qualification and coverage gaps remain listed in
security-scan-review-20261003.md; these checks do not close every finding.

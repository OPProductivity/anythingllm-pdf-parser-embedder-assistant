# Running local diagnostics safely

Use the bounded runner for disposable local analysis, comparisons and offline test suites:

```powershell
.venv\Scripts\python.exe -m diagnostic_runner --timeout 600 --output tmp/unique-diagnostic-name -- .venv\Scripts\python.exe -m pytest -q
```

The output directory must be new. It retains stdout, stderr and a result receipt.
The command runs without a shell. Supply arguments individually; do not put secrets
in command arguments or diagnostic output. This is containment, not a security sandbox.

Windows Job Objects contain the bootstrap and descendants before the command
starts. Closing the job cleans up descendants on timeout, exception, normal exit,
or abrupt death of the runner. No application-wide PID/name matching is used.
A receipt still saying `running` after runner death is incomplete, not success.
The wrapper cannot retroactively contain an already-running unwrapped command.

Do not wrap production servers, live AnythingLLM mutations, or production workers
intended to survive their parent. Killing a local client cannot undo a remote
request. Use existing live-probe recovery and cleanup contracts for those.

Keep comparisons bounded independently: equality and token counters first, then
per-page differences. Do not align entire repetitive books character-by-character
with SequenceMatcher autojunk disabled. A yielded tool session is still running:
poll its receipt through completion and check owned processes before handoff.

Logs are streamed to files rather than held in memory. Choose a sensible timeout
and avoid intentionally unbounded/noisy commands. No production OCR deadlines or
server-death survival policies are changed by this helper.

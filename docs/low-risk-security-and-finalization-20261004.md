# Low-runtime-risk security and finalization qualification

The retained functional source before these changes matched pre-security
`ad798af` exactly. The only committed difference was the historical benchmark
report. No non-security fix was omitted by the restored `31c8ad5` checkpoint:
editor metadata, cover boundaries, canonical evidence, event retention, vector
confirmation, accented labels and shared-upload count messages remain present.

## Selected security changes

Performance-risk scores describe automatic-run exposure, not vulnerability
severity or a measured guarantee:

| Change | Speed-risk score | Automatic-run exposure |
| --- | ---: | --- |
| Immutable hash-verified offline installer bundles, including bridge tooling | 1/10 | No installer execution during ordinary runs |
| Explicit standalone CLI overwrite consent and staged export publication | 1/10 | Only extract/segment CLI wrappers; GUI uses lower-level functions |

Launcher authentication, HTTP/SSE transport changes, source locking, resource
budgets and other security changes are not reinstated here. The optional bridge
is not reinstalled or changed in the running AnythingLLM application. These two
changes cannot establish that all previous security findings are resolved.

Release tests exercise bundle generation, member hashes, exclusive publication
and PowerShell verification-only success/failure without executing package code.
A complete production wheel bundle and disposable full installation have not
been qualified by those tests; distribution requires that separate release work.
Standalone CLI tests cover rejection before extraction, explicit overwrite,
extraction failure and restoration after partial publication failure.

## Finalization

The earlier fresh run `r-20261004-120456-25686cf89e` took 972.257 seconds, with
approximately 37.29 seconds between final batch confirmation and completion.
This tail is not embedding-provider time. Its full breakdown was unavailable.

Inspection found selected segments being written to temporary files, read back,
then discarded when identical canonical TXT bytes were already retained. The
new retention planner reuses those canonical bytes before staging. Filename,
collision, export mapping and public output cleanup behavior are unchanged.
Newly staged duplicates still receive a second canonical lookup on publication.

An isolated replay on full copies of that run measured retention at 8.397 seconds
before and 7.206 seconds after. All 575 page/body hash pairs matched. The profile
removed 1,150 file opens (575 temporary writes and 575 read-backs). This is a
local diagnostic measurement, not proof that the full 37-second tail is fixed.

Seven private finalization timing checkpoints now separate retention, optional
storage diagnostics, completion reporting, readiness, public publication,
integrity audit and publication-receipt refresh. Timing-write failure is
non-fatal; no ETA, OCR, queue or upload algorithm is changed.

The pre-benchmark focused suites passed 42 and 34 tests respectively. The full
offline-deterministic suite passed 2,229 tests and 15 subtests (34 deselected;
three dependency deprecation warnings) in 523.34 seconds. Deployment, fresh
embedding and cleanup evidence follows after those operations complete.
Historical artifacts and source PDFs are preserved.

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
One qualification to the functional-history statement: the reverted security
commits also contained credential lifecycle bug prevention (avoiding credential
creation during settings preview, cleanup retries/reporting in recovery paths,
and failure-only cleanup obligations). Those remain deliberately deferred, not
silently restored as part of this benchmark.

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

## Live benchmark

Commit `6487547` was pushed before the test. Two fresh assistant starts produced
healthy servers with distinct PIDs 21036 and 23460 and matching source hashes.
The currently running AnythingLLM root/API processes (13868/9328) were preserved;
older remembered process identities were no longer current at verification.

The first submission failed Gradio validation before processing because the
previous `pdf-workspace` container had been removed outside this benchmark.
The current database contained only Thesis1 and Thesis 2 page-parents. A fresh
test-only `PDF workspace` was created, workspace choices refreshed, and the
same eight source PDFs/settings were submitted again. Qwen OpenRouter embedding
configuration matched; no research workspace was selected or modified.

Run `r-20261004-125909-6dec0b1bdf` completed successfully in **1001.604 seconds
(16 minutes 41.604 seconds)**. The assistant's own run clock was 997.693 seconds;
the harness includes browser/client dispatch and completion polling. It proved:

- Eight new worker configurations and 575 newly generated TXT payloads.
- All 575 payload-byte hash multiset entries matched the reference.
- Zero assistant cache reuse and exactly 575 observed fresh provider calls.
- All 575 selected records linked and confirmed; integrity audit: no findings.
- Complete event journal: 1,731 retained events, observation no longer pending.

| Measurement | Prior valid run | This run |
| --- | ---: | ---: |
| Harness seconds | 972.257 | 1001.604 |
| Group 1 seconds | 384.6921 | 612.6730 |
| Group 2 seconds | 315.2816 | 181.3079 |
| Observed embedding/provider seconds | 653.103 | 768.822 |
| Final Desktop all-complete event to assistant finish | 40.469970 | 9.201804 |

The full run is 29.347 seconds (3.02 percent) longer than the prior valid run;
observed provider intervals are 115.719 seconds longer. The remaining run time
is 86.372 seconds shorter. These log-bracket measurements do not identify
upstream HTTP/provider causes or establish an edit-caused speed difference.
This single measurement is in the requested 16-17 minute range, but does not
guarantee that range under future provider variability.

The older roughly 37-second figure started after final vector confirmation;
the comparable SSE-to-finish interval is 40.47 seconds. The new local timings
are: retention 3.376033, optional diagnostics 0.000205, reporting 0.025494,
readiness 0.075184, public TXT publication 2.118670, integrity 0.074313 and
publication-receipt refresh 0.586921 seconds, totaling **6.256820 seconds**.
The remaining 2.944984 seconds include the final exact-vector check and
uninstrumented receipt/checkpoint/completion work. No claim is made that the
earlier 37-second variation was entirely caused by temporary TXT staging;
only the avoidable staging cost was isolated and removed.

## Cleanup and retained output

Native API cleanup removed all 575 owned documents/vector caches. It left one
owned orphan SQLite vector mapping; the pre-deletion ownership snapshot proved
the exact row belonged to this run, and a physical-vector absence check plus
an exact-row transaction removed it. All 6,796 protected document records and
unrelated mappings remained unchanged. No global orphan cleanup was performed.

The recreated test workspace had no remaining documents or chats. Its empty
physical vector table was verified, then the native API removed that workspace.
The remaining workspace containers are Thesis1 and Thesis 2 page-parents, with
their records unchanged. Temporary test API keys were deleted. AnythingLLM's
root/API process identities remained unchanged throughout the replay/cleanup.
The PDF assistant remains healthy on the committed code after the second start.

Source PDFs, historical runs and the new run evidence remain intact. The public
output is still flat TXT-only: 575 segment exports plus eight complete parsed
transcripts, with no diagnostic JSON/JSONL introduced into that directory.
Exact cleanup receipts and the ownership plan are retained in this test run's
private run-state folder.

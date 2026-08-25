# Broad stability refactor

This document is the executable reliability contract for the PDF Parser
Embedder Assistant. Passing unit tests alone does not complete this work.

## Product invariants

| ID | Invariant | Authoritative evidence |
| --- | --- | --- |
| INV-01 | A source with exact vector proof is never submitted again. | Source transaction ledger plus fault-run request log. |
| INV-02 | An ambiguous external mutation is never automatically replayed. | Durable stop boundary, recovery manifest, and request log. |
| INV-03 | A definite pre-mutation rejection releases safe later sources. | Per-source terminal states and later request log. |
| INV-04 | Completed work survives worker, server, and Desktop interruption. | Crash matrix restart result and exact vector identities. |
| INV-05 | Every selected PDF has an explicit outcome. | Selection receipt reconciled with proven, cached, duplicate, rejected, skipped, held, or failed sources. |
| INV-06 | Terminal counts reconcile across independent artifacts. | `integrity-audit.json`; no error findings. |
| INV-07 | Optional retrieval observation never invalidates exact vector proof. | Terminal classifier and integrity audit. |
| INV-08 | Unknown mutation contracts stop before a write. | Compatibility sentinel evidence plus zero mutating requests. |
| INV-09 | Diagnostics contain no credentials, document text, private paths, workspace names, or raw source identities. | Secret/path scan of compact bundles. |
| INV-10 | Prepared-but-unsubmitted work can continue without reparsing proven inputs. | Prepared artifact identity and restart certification. |

## Fault matrix

Every scenario has four required assertions: request sequence, durable state,
recovery plan, and terminal audit.

| Scenario | Injection boundary | Required outcome |
| --- | --- | --- |
| F-01 | Definite HTTP rejection before mutation | Current source rejected; later source proceeds. |
| F-02 | Connection loss before request bytes | No mutation credited; safe retry requires positive pre-write evidence. |
| F-03 | Response lost after acceptance | Source held; no automatic replay; later mutation lane stops. |
| F-04 | Delayed workspace link/vector visibility | Bounded observation continues from owned progress; no duplicate request. |
| F-05 | SQLite busy during read-only observation | Observation retries remain bounded; accepted mutation is not classified as rejected. |
| F-06 | AnythingLLM restart before mutation | Runtime may recover before first write; otherwise explicit global hold. |
| F-07 | AnythingLLM restart after mutation | Reconcile exact identities; never blind-retry. |
| F-08 | Worker kill at prepared state | Prepared artifacts remain reusable; no remote state claimed. |
| F-09 | Worker kill after durable intent | Restart distinguishes unsent from ambiguous intent. |
| F-10 | Worker kill after receipt | Restart reconciles before any submission. |
| F-11 | Worker kill after exact vector proof | Proven source is never resubmitted. |
| F-12 | Server loss while worker continues | Worker evidence remains authoritative; browser ownership can reconnect. |
| F-13 | Source disappears or changes after preview | Changed source is rejected locally; unaffected sources continue. |
| F-14 | Disk capacity falls before next source | Next source is held before preparation; earlier work retained. |
| F-15 | Exact selected-input duplicate | One canonical preparation; duplicate receives explicit skipped outcome. |
| F-16 | Fully cached source | Link/confirmation is distinct from fresh embedding. |
| F-17 | Partially cached source | Only missing exact records enter the mutation plan. |
| F-18 | Failure-bundle write unavailable | Pipeline outcome remains; certification becomes a non-retry warning. |

## Crash checkpoints

The subprocess crash runner must terminate at each checkpoint and restart from
the same retained run root:

1. preparation complete;
2. source transaction created;
3. attachment intent durable;
4. request started;
5. response accepted;
6. first workspace link observed;
7. first exact vector observed;
8. all exact vectors proven;
9. terminal audit written;
10. terminal progress record written.

## Validation layers

1. **Pure model checks:** enumerate legal and illegal source-state sequences.
2. **Process-boundary checks:** real subprocesses and local fault server; no
   mocks for durability, request ordering, or restart behavior.
3. **Read-only live check:** characterize the installed AnythingLLM runtime.
4. **Disposable live canary:** isolated workspace, representative PDF copies,
   exact record reconciliation, and confirmed cleanup or named leftovers.
5. **Soak:** medium batch on release candidates and occasional large batch.

`reliability_eta_evidence.py` separately emits anonymous, machine-readable
regression evidence for the existing classic ETA formula. It asserts workload
monotonicity, OCR reserve direction, cache-realization bounds, bounded queue
repricing, and the three-sample recalibration threshold. It never reads private
timing history and is evidence only; it does not replace or tune the formula.

`reliability_scale_acceptance.py` supplies the non-mutating large-batch layer:
1,000 independent source checkpoints (3,000 retained artifacts) are written and
verified with production durability code. It proves exact reload, changed-file
rejection, restoration, and the no-replay rule after submission may have
started. This complements, but does not substitute for, disposable live canaries.

The live canary is never run against an existing user workspace. Paid-provider
use remains opt-in. A canary failure cannot trigger an automatic retry after an
ambiguous mutation.

## Release gate

A stability release requires:

- deterministic checks with a finite per-test deadline;
- a machine-readable passing result from the full default Python suite (a
  focused JUnit file cannot satisfy this gate);
- every fault and crash scenario passing;
- a passing terminal audit for representative single, mixed, duplicate,
  cached, partial, and medium-batch runs;
- a current read-only compatibility report;
- a successful disposable live canary;
- the release-certifying canary must contain at least nine PDFs and therefore
  exercise the medium-batch path; a small passing probe is useful evidence but
  cannot certify a release by itself;
- passing anonymous 1,000-source durability and classic-ETA regression reports;
- a redacted environment fingerprint and clean release artifact;
- a rollback reference to the previous known-good commit;
- no unresolved high-impact audit finding.

## Deliberate non-goals

This refactor does not redesign the classic ETA, add permanent catalogs,
support non-PDF formats, add model/provider allowlists, make retrieval probes
mandatory, auto-retry ambiguous mutations, increase mutation concurrency,
restore diagnostic UI tabs, or perform unrelated visual refinement.

## Current validation evidence (2026-08-25)

The release candidate has been exercised on CPython 3.14.6 and AnythingLLM
Desktop 1.16.0. This section records evidence, not a permanent claim about a
future dependency or Desktop build.

- Default deterministic suite: 851 passed, 17 browser UI tests intentionally
  deselected, one third-party Starlette/httpx deprecation warning. Statement
  and branch coverage was 66.75%, above the enforced 63% project floor.
- Separate isolated Chromium UI suite: all 17 tests passed, including
  selection readiness, six-PDF local preparation, dark mode, and narrow layout.
- Crash acceptance: 13 scenarios passed, covering every numbered checkpoint
  and the rejection/success boundary cases.
- Process-boundary transport acceptance: five scenarios passed (definite
  rejection, lost response, connection refusal, delayed vectors, SQLite busy).
- Disposable live grouped canary: two real PDFs, two exact source windows,
  nine records uploaded and confirmed, terminal integrity audit passed.
- Exact selected-input duplicate canary: one canonical PDF embedded, its
  byte-identical selected copy explicitly skipped, all selected inputs
  accounted, exact cleanup passed.
- Cache sequence: fresh two-PDF import, mixed two-cached-plus-one-fresh import,
  and an all-cached no-submission run all passed in one disposable workspace.
- Partial sequence: page one of two PDFs was first imported; the subsequent
  full run submitted only seven missing page records and confirmed all nine.
- Medium live canary: ten distinct real-PDF copies, one page each, produced ten
  sequential exact-vector source transactions; all ten were independently
  accounted, the integrity audit passed, and exact workspace/folder cleanup
  passed. A preceding cohort deliberately surfaced one source-local empty-page
  preparation review while all nine unaffected later sources still completed,
  also proving continuation and cleanup on the review path.
- Compatibility-authority handoff: all ten medium-canary workers retained their
  fast local snapshot as explicitly read-only/non-authoritative while carrying
  the same qualified v1.16 mutation contract and package fingerprint proven by
  the single batch gate. The 60 MB Desktop package was not rehashed per PDF.
- Anonymous large-scale acceptance: 1,000 independent source checkpoints and
  3,000 retained artifacts passed exact reload, tamper rejection, restoration,
  and no-replay-after-submission-start checks.
- Classic ETA regression evidence passed for anonymous single, medium, large,
  OCR, cache-realization, queue-repricing, and recalibration-threshold cases;
  private timing history was not read and the estimator was not changed.
- Wheel build: standard isolated setuptools build completed; the wheel
  contained the new recovery and reliability modules, installed into a clean
  target directory, imported successfully, and resolved its packaged 0.5.1
  version resource outside the source tree.

Three defects were found by these live canaries and added to the contract:

1. an all-cached batch is a valid verified preparation state even though it
   has no uploadable sources;
2. disposable workspace cleanup must target and verify the exact managed
   document folder, not merely report that the workspace row was deleted.
3. the direct PDF picker and retry picker require explicit six-output and
   seven-output adapters; relying on Gradio to ignore an extra update can leave
   the selection transaction unfinished and Confirm disabled.

The 17 browser tests remain a separate explicit layer because they launch a
real browser and localhost UI. They are not silently counted as default-suite
passes. The deprecation warning is currently inside the third-party
FastAPI/Starlette test-client import path; it is not a production runtime
failure, but dependency upgrades should recheck it.

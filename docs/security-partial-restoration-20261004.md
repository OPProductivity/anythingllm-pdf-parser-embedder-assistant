# Functionality-first security restoration

The two requested revert commits, f09658f and 84b37d3, were pushed to main.
The restored baseline exactly matched ad798af. Its fresh browser-path replay
of eight PDFs completed in approximately 15 minutes, with 575 records, zero
cache reuse, and complete exact vector coverage. The first queue group took
425.852 seconds instead of 1285.446 seconds in the cancelled security run.
The uploaded TXT payloads were byte-identical between those runs.

## Attribution limits

The original browser-access middleware in 581fb0b caused a confirmed HTTP 403
startup incompatibility with Gradio's internal startup request. The subsequent
5dd19a4 introduced a process-owned self-probe exception. Browser authentication
remains deferred rather than being silently restored.

The queue slowdown has NOT been attributed to a particular security edit.
Temporal improvement after rollback is evidence for investigation, not causal
proof. Read-only live A/B probes of the historical transport found additional
authenticated-request cost in the tens of milliseconds, not tens of seconds.
Both SSE versions connected and cancelled promptly. These small tests do not
qualify transport behavior under a full active embedding workload.

The Desktop executes queued per-document provider calls independently of the
assistant's HTTP upload and SSE observer. No provider latency/timeout receipt
was available to distinguish provider delays from Desktop internal indexing.
Do not label either cause proven, or label the process identity checks proven
responsible, from these observations.

## Reintroduced independently

- Upload location containment before filesystem relocation.
- Windows read-only source sharing and authoritative source SHA verification.
- Configurable source, extraction, raster and local preparation budgets.
- Verified immutable offline release bundles, including optional ASAR tooling.
- Staged CLI exports with explicit overwrite consent.
- Failure-only temporary-key cleanup obligations and credential-free UI previews.

Agreed defaults remain 6 GiB per source PDF, 150 MiB extracted text per PDF,
10,000 pages, 50 million raster pixels, 2 MiB text per page, 100,000 segments,
and six hours of local preparation. Exceeding a limit fails explicitly instead
of truncating. Desktop embedding time is not charged to the preparation budget.

The normal interface, OCR selection, ETA, duplicate protection, output retention,
Desktop queue transport, SSE transport and vector-confirmation algorithms are
unchanged from the faster rollback baseline. Installer policy and CLI overwrite
policy intentionally retain the previously authorized security restrictions.

## Deferred

- Browser authentication and its launcher/middleware changes.
- Managed Desktop listener/socket identity enforcement.
- Changed authenticated HTTP routing and response readers.
- Changed SSE framing, response bounds and event memory limits.

These deferrals leave relevant high/medium findings open. They do not claim
that all deferred edits caused the slowdown, or that the system is fully hardened.

## Verification

Focused boundary/release tests: 25 passed, one privilege-dependent symlink skip.
Fresh preparation of all eight workload PDFs: eight completed, zero failures;
all body text identical, all manifest row counts identical, all references decoded.
Historical preparation artifacts are retained under tmp-output and isolated
test homes; original PDFs and historical run evidence are not changed.

The initial full suite loaded six old geometry-less OCR mocks before the fixture
updates, producing six fixture failures and 2239 passes. Fresh focused checks
pass; a clean complete suite and restarted live test are required before final
qualification. No production OCR algorithm was changed to accommodate a mock.

Final clean suite: 2248 passed, one privilege-dependent skip, 34 deselected,
three existing dependency deprecation warnings, 15 subtests passed (295.59 s).
The source protection and resource checks retained byte-identical body text
and identical record counts for all eight historical PDFs.

The assistant was restarted with the partial restoration. Fresh normal-browser
tests used three unique PDFs and a dedicated document folder per batch:
54.092 seconds and 61.089 seconds respectively; each confirmed nine of nine
fresh records, zero cache reuse, and passed the terminal integrity audit.
All pre-existing workspace-document rows and vector-mapping rows were unchanged;
all original AnythingLLM Desktop process identities were unchanged.

The test harness initially submitted a newly created workspace before refreshing
the browser's choices. Gradio rejected it before any run or upload. Its empty
workspace was deleted. The harness now performs the normal refresh callback.
Its first completed run supplied a relative cleanup directory where the existing
cleanup API requires an absolute path; containment correctly rejected that
directory. Documents and workspace were deleted; the owned empty folder was
then separately removed using the API's managed default path. The corrected
harness uses that default, and the repeated batch completed cleanup automatically.
Neither issue required a production change or weakening path containment.

Verified all three disposable workspace namespaces absent, all 18 uploaded
test document files absent, and their location-derived vector cache files absent.
Generated test PDFs and diagnostic receipts are retained locally. Research
workspaces, research documents and the eight-PDF rollback replay remain intact.

Reproducible probes: experiments/security_transport_ab_20261004.py and
experiments/security_partial_live_20261004.py. The first creates and removes a
temporary API key only. The second owns a uniquely named disposable workspace
and newly generated PDFs, removes its documents/workspace in finally, verifies
pre-existing document/vector mappings, and verifies Desktop process identity.

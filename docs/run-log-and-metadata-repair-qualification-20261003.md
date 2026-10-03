# Run-log and metadata repair qualification

This follow-up implements the remaining findings from run
`r-20261003-162544-e28b3f2caa`. The opening-cover scope correction and canonical
diagnostic directions were already implemented in `ff20b41`; see
`opening-ocr-boundary-qualification-20261003.md` for that investigation.

## Changes

- Known PDF producer placeholders (`No Job Name`, the observed OP production
  filename shape) no longer override real filename titles. Structured catalog
  filenames separate the title from the author and publisher. Visible author
  corroboration remains required; explicit user overrides are unchanged.
- Private structural manifests retain independent metadata records but reference
  long text bodies in one readable, document-local `manifest-text.jsonl`.
  References use local ordinal IDs and integrity hashes, not opaque filenames.
  Legacy inline manifests still work. Missing or changed evidence fails visibly.
  Short strings remain inline to avoid creating unnecessary extra artifacts.
- Identical global storage observations and column descriptors share existing
  immutable run evidence. Audited checklist/column CSV files also share one
  physical file in the run pool. Artifact roles point to verified relative paths.
  Normal upload TXT and standalone public exports remain self-contained.
- Preflight saves every available timestamped callback once in
  `confirmation-preflight.json`, with source names/page counts stored separately.
  The timing timeline retains event/source counts. This adds observability, not
  new instrumentation for every internal operation, and does not change ETA.
- Queue aggregates retain all observed events. The bounded recovery ledger keeps
  its 96-event tail and points to an append-only `.events.jsonl` journal. An honest
  completeness flag distinguishes a short inline tail from missing history.
  Previously discarded historical events cannot be recreated.
- The redirect test now mocks HTTPX, matching the approved SSE transport. Its
  old urllib mock allowed an unintended network attempt and indefinite advisory
  reconnection. The transport itself did not need modification.

## Production-path evidence

Five real PDFs (Hansen, Bordwell, Casetti, Schatz, Aparicio), each with a distinct
settings profile, passed legacy/private preparation comparison: decoded manifests,
upload text and metadata, public exports, canonical references, prepared recovery,
manual-kit materialization, and diagnostic-bundle relocation. All four upload
contracts were checked. Inputs remained byte-identical and no filesystem links
were retained. Receipts: `tmp-output/run-log-repair-matrix-20261003/results.json`.

A preparation-only production-worker replay of The Racial Middle retained page 1
and the unchanged native body after its new cover prefix, and recognized Eileen
O'Brien from visible credit. Fresh native samples also recovered Herbert J. Gans
and Travis Franks after rejecting their production-title placeholders.

The old six manifest files totaled 3,760,348 bytes. The replay's six manifest
records plus shared text store total 1,278,227 bytes (approximately 66% less).
The store contains two genuinely different bodies, native and cover-recovered
OCR, rather than collapsing them. There is still necessary overlap with the
self-contained upload TXT. This is not a claim of zero duplication everywhere.

The tests created no AnythingLLM workspaces or vectors. Original PDFs, historical
runs, existing embeddings, OCR recognition/crops/reading order, interface, duplicate
protection and public output cleanup were not changed. Historical misleading
metadata is not silently rewritten or re-embedded.

## Automated checks

- The broad suite completed: 2,147 passed, four failed, 34 deselected and 15
  subtests passed. The four failures were one diagnostic error-wording regression,
  the missing already-approved HTTPX runtime-lock entry, and two stale export
  fixtures assuming private staging inside the public output root.
- After correcting those issues, all 28 tests in the affected diagnostic,
  packaging, export, manifest, preflight/metadata and runtime-history groups passed.
  The broad suite was not repeated after these final corrections.
- Separately, 86 focused tests passed, including authenticated redirects and
  journal restart/resume. The initial broad attempt's stale urllib SSE mock was
  replaced by HTTPX MockTransport; production SSE behavior was not changed.
- Ruff, core Pyright and all staged pre-commit checks passed before the final
  corrections; these checks are repeated by the commit hook.

Export fixtures now exercise collision handling when allocating the private run
directory, then verify the public directory uses the same name and remains flat.
No production output naming, copying or cleanup code was changed for these tests.

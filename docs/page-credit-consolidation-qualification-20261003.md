# Page-sized evidence and wrapped editor credits

## Scope

This follow-up addresses findings 1 and 4 from production run
`r-20261003-200541-5faf1b82fd`. Title detection and accented-surname label
algorithms remain unchanged. No OCR recognition, reading-order, extraction
selection, vector confirmation, duplicate protection, ETA or interface design
was changed. Historical runs and existing embeddings are not rewritten.

## Implementation

- Inline `By`, `Written by` and `Edited by` credits now use the existing
  complete-block detector. Continuation requires comma/conjunction evidence,
  is bounded to six lines, and retains existing person-name, affiliation,
  publisher and title-fragment guards. The standalone-role branch is unchanged.
  Evidence records include the accepted continuation lines. User overrides win.
- Private manifest bodies at least 512 UTF-8 bytes share the existing local
  ordinal/hash text store; tiny labels remain inline. This conservative threshold
  pays for store/reference metadata when even two substantial copies exist.
  Text and metadata remain independent and decoded readers remain compatible
  with legacy inline manifests and the previous store format.
- Each manifest commits its new evidence in one durable batch before publishing
  references. Failed appends roll back to the previous byte boundary. Tests cover
  failed writes, retries, modified/missing evidence, Unicode and escaped text.
- Successful private retention reuses byte-identical indexed TXT files instead
  of writing another permanent segment copy. The retention receipt separates
  logical export filenames from relative canonical paths. CLI and GUI publication
  use that mapping, with the original root-file scan for older receipts.
  Unsafe/missing mappings fail visibly. Public names, bytes, flat layout,
  collision handling and cleanup behavior are unchanged. No links/compression.

## Qualification

- Broad offline suite: 2,189 passed initially; 2,190 passed on the repeat,
  34 deselected and 15 subtests passed. Three existing dependency deprecations.
  After the byte-exact reuse refinement and page/body pairing guard, all 49
  focused manifest, canonical-artifact and metadata tests passed.
- Ruff and core Pyright passed. The commit hook repeats production checks.
- Five real academic sources across six settings profiles: all 30 cases passed
  legacy/private equality for decoded manifests, prepared text, upload metadata
  and payload bytes, effective settings, canonical references, recovery,
  relocated diagnostic bundles, optional manual checks and public export names
  and bytes. All four upload contracts were checked without API writes.
  Receipt: `tmp-output/page-credit-consolidation-matrix-20261003/results.json`.
- Fresh preparation of 43 inventoried PDFs (1,451 source pages total) used
  recorded settings and new OCR checkpoints. Every source hash stayed unchanged.
  The final comparison checks exact decoded bodies, effective boundaries and
  settings, metadata changes, every JSONL, canonical role resolution, and each
  segment export's page/order-to-body pairing.
- Precarity's five editors are retained in the source summary and all 291
  selected upload-plan rows. It is the only new metadata change versus the prior
  qualification/baseline: its derived label changes from FALC to SCHAEFFER as a
  consequence of the complete credit list, not a label-algorithm change.
- Paired structural files including text stores total 18,984,211 bytes before
  and 9,827,389 bytes after (48.2% less). This is not a total run-folder reduction.
  The latest-nine paired subset totals 14,482,173 versus 6,119,406 bytes; all 779
  local page exports in that subset reuse existing canonical TXT bytes.

Audit receipt:
`tmp-output/historical-replay-20261003/preparation-0ebfbe79c5bd4cc68c099afc55bc1325/page-credit-audit.json`.
The repeatable audit records explicit replacement and Git-baseline receipts.

## Exceptions Kept Visible

The first retention audit misclassified the verbose replay home as standalone,
compacted its first generated test case, then hit the Windows filename budget in
the second. No production/historical run was modified. The first case was freshly
requalified; subsequent retention checks use copied runs in short, explicitly
isolated private homes. Replay and audit tools retain the earlier evidence and
record replacement receipts rather than silently reusing an earlier success.

Salsa's fresh Unstructured OCR worker repeatedly failed with BrokenProcessPool,
including an isolated short-path run. The preserved pre-change a2c6bbd code
reproduced the exact same failure/native fallback with the same isolated settings.
Thus its current selected body matches the fresh Git baseline, but lacks the
cover recovered in the earlier successful production run. The other 42 PDFs
match previously qualified/successful bodies. No claim is made that all 43 match
the earlier successful OCR output. The OCR-worker crash remains unresolved and
outside these two fixes; its underlying cause is not established by this test.

Baseline code was extracted from Git into an owned temporary directory. The
baseline replay records that directory and its pipeline file hash. No embedding
provider was called, and no AnythingLLM workspaces/vectors were created or removed.
The main AnythingLLM application was left running throughout qualification.

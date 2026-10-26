# Setup, preparation measurements and exact vector observations

Progress allocation was reopening each confirmed PDF for a 24-page difficulty
sample. It now consumes the count/density facts already present in the current
confirmation manifest, only when the resolved path and source version match.
Malformed, missing or stale facts fall back to a three-page progress estimate.
This affects advisory progress weights, not required all-page native coverage,
OCR acquisition, expert interpretation or author priorities. No new persistent
document cache is introduced.

The existing PDF summary now includes bounded aggregate measurements for native
decoding/layout, author inference/review, cleanup, segmentation, payload building,
diagnostics, serialization and existing configuration/inspection phases. Each
object is capped at 3,840 bytes when serialized independently with indent 2;
excess stages merge into a counter. No per-page or per-operation log stream is
added. Worker results separately report module-import and control/preparation
milliseconds. PDF measurement scope ends before final summary writing/retention;
worker duration covers a broader boundary. Stage inclusive times overlap; use
self wall times for a non-overlapping partition. Python process CPU includes
native threads but excludes child OCR CPU. Unattributed time remains explicit.

Exact vector observation previously scanned the same filter for a count and
again for chunkSource identities. One scalar-column scan now supplies both.
The diagnostic sample projects only its text column. Physical vector IDs,
duplicate/missing identity checks, provenance requirements, completion policy
and storage-race checks remain in place; no vectors are sampled away.

## Qualification

- Ruff and core Pyright passed. Complete offline suite: 3,517 passed, one skipped,
  43 deselected, 28 subtests passed; three dependency deprecation warnings.
- Two actual local full-PDF preparations (6 and 33 pages) matched the previous
  release on every TXT content hash, selected manifests, complete author
  inference except collection duration, and selected metadata fields. Generated
  path-dependent basenames were normalized by content, not claimed identical.
- Final candidate also passed an actual cancellable worker subprocess on the
  six-page PDF. Source hash stayed unchanged, author remained Josephine A.
  Ruggiero, bounded measurements and worker timings reached the result.
- A freshly generated confirmation manifest supplied actual six-page facts
  while the progress sampler was forbidden: no redundant PDF sampling occurred.
- Fully imported reference and candidate verifiers inspected the same existing
  768-record group read-only: identical functional-result hashes and complete
  exact coverage. Elapsed time was 23.389 versus 19.178 seconds (18.0% lower);
  Python process CPU was 74.078 versus 63.672 seconds. These are sequential local
  observations, not an end-to-end ingestion speed qualification.

No fresh embeddings or full ingestion were performed for this change. The
1,150-presentation recognition suite was not rerun: this change does not edit
author experts, OCR, filename rules, acquisition policy, cleanup or native layout.
The previously qualified scores remain the reference: original 356/386, neutral
322/382, pure neutral 297/382. Output agreement is not new manual validation of
every author or proof that every artifact is identical.

The larger native extraction/layout cost and historical large-document
packaging cost still require investigation using these narrower measurements.
No whole-run speed improvement or new recognition gain is claimed here.

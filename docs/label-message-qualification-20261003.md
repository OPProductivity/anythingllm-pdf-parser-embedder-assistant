# Segmentation messages and accented labels

## Scope

Shared preparation, queue, cache-progress, reconciliation and count descriptions
now refer to upload records. This is valid for whole-file, page, page-limit,
passage, page-passage and custom-page-range segmentation, with either upload
representation. Counters, liveness, source identity, confirmation, payload text,
segmentation and duplicate protection are unchanged. Actual page-parent-only
fast-skip and provider-staging descriptions retain their specific terminology.

Default short-label inference folds Unicode diacritics before applying its
established surname/title-word policy. It supports precomposed and decomposed
accents and preserves casing, apostrophes and hyphens. Full author metadata,
source text and explicit label overrides are not rewritten. Future derived
filenames may change; historical files, cached storage and workspace identities
are not migrated or deleted.

## Source recheck

Reviewed the first three source pages of all eight PDFs in production run
r-20261003-212315-5d5b5faf3c, plus Salsa page 4 and the review signature on page 3.
Rendered and visually checked Precarity's title page and Salsa's author page.

| Source | Result |
| --- | --- |
| Precarity and Belonging | All five volume editors correct; Matt Garcia's series-editor credit correctly excluded |
| Salsa Crossings | Series editors correctly excluded; actual author Cindy Garcia is on page 4, outside the existing three-page inference bound |
| Feeling Brown | Jose Esteban Munoz is the article author; editorial acknowledgments are not authors |
| Blood Oranges review | Sonia Hernandez is the reviewer, not the reviewed book's Timothy Paul Bowman; page-1 embedded text has a separate encoding error |
| Who Identifies as Latinx? | Mora, Perez and Vargas bylines correct |
| New White Ethnics | Rebecca A. Schut byline correct |
| Settler Violence | Christopher A. Loperena byline correct |
| Politics of Aztlan | No incorrect editor assigned; author inference abstained on fragmented title-page text (Ignacio Molina Garcia), an existing limitation |

The table transliterates accents for ASCII documentation only. Source names and
API author metadata retain their original spelling/encoding. No editor-inference
or OCR change was needed or made. The malformed full reviewer metadata remains
a separate matter from correctly encoded surname label extraction.

## Security review

See security-scan-review-20261003.md for the latest completed cloud scan, its
scanned revision and all ten reported findings. No security remediation or new
scan was performed as part of these narrowly scoped changes.

## Verification

- Full offline suite: 2,218 passed, two fixture failures, 34 deselected and
  15 subtests passed. One assertion expected the removed page-parent wording;
  the other replay fixture had not supplied the newly required code root.
- Corrected those test fixtures without weakening production validation:
  56 focused tests passed, followed by all 13 replay qualification tests.
  The replay fixture now also checks the actual code root and pipeline digest.
- 28 new label/message tests cover NFC/NFD names, accented Latin surnames,
  apostrophes/hyphens, title fallback, native filenames and generic record
  descriptions. Existing wrapped-credit and publisher/series guards passed.
- Real-source matrix: 30/30 cases passed across five PDFs and six segmentation
  profiles, transports, representations, metadata modes, overrides and OCR/
  native backends. Private/standalone manifests, payloads and export bytes
  matched; source hashes stayed unchanged. Receipt:
  tmp-output/label-message-settings-matrix-20261003/results.json.
- Ruff passed; Pyright reported zero errors and warnings. Existing dependency
  deprecation notices remain. No API writes, provider calls or new workspaces.
- A redundant --lf invocation after the failure cache had cleared would have
  repeated the whole suite; its verified owned processes were stopped. That
  partial invocation is not included in the qualification results above.

The separate short-label repair for the malformed HernÃ¡ndez byline was proposed
to the user for approval and is not included in this change. No broad encoding
repair was applied to metadata, OCR or extracted content.

## Deployment

Source changes committed locally as 7e7858b; no push performed. Checked the
owned assistant server (PID 9948) had zero active runs, then stopped it through
the ownership-checked CLI and started the normal launcher hidden from this
repository. New assistant root PID 22840, started 2026-10-03 22:08:53 local time;
repository cwd verified and http://127.0.0.1:7860 returned HTTP 200.
AnythingLLM PID 9676 and its creation timestamp 1791034250.083542 were unchanged.
The subsequent documentation commit does not require another server restart.

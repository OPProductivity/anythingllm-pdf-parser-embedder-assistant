# On-demand private TXT artifacts

## Changes

Normal private preparation no longer writes page-transition companion TXT files
or supplementary-content candidate TXT reports. Transition detection and exact
reconstructed text remain in `page-transition-manifest.jsonl`; supplementary
text, classification and exclusion evidence remain in `retrieval-lane-review.json`.
Created-file counts now distinguish materialized companions from detected continuations.

Upload representation is resolved before file materialization. Only the effective
representation and metadata mode receive TXT files and a file-backed plan. The
existing oversized-parent guard still switches unsafe parents to page-local
segments. Required selected-route files remain available to grouped submission
and prepared-batch recovery, including raw-text transport: those consumers need
a file-backed plan. Four retained payload JSONL roles are unchanged; identical
bodies still share canonical files.

Strict page-parent mode preserves an existing direct/grouped distinction: direct
uploads use strict metadata, whereas the outer grouped handoff uses native-header
metadata. Two small CSV plans reference the same canonical TXT bodies in that
case; upload identity is not silently changed by the storage refactor.

Manual plans/checklists stay available. They reuse existing canonical segment
text where possible. Otherwise a blank `text_file` and `text_manifest` identify
the segment's retained source text without writing an unused TXT. These manual
plans are not the selected submission plan, whose file paths remain complete.

## Explicit Rendering

From the repository, use its installed Python environment. Supply the per-document
private directory containing `run-summary.json`, not the whole batch directory:

```powershell
.\.venv\Scripts\python.exe -m run_artifact_tools "DOCUMENT_RUN_DIRECTORY" --kind diagnostic-text
.\.venv\Scripts\python.exe -m run_artifact_tools "DOCUMENT_RUN_DIRECTORY" --kind manual-kits
.\.venv\Scripts\python.exe -m run_artifact_tools "DOCUMENT_RUN_DIRECTORY" --kind upload-alternatives
```

The command writes under `on-demand`, reuses available TXT bodies for kits/plans,
and does not upload, embed, prune or alter original summaries/submission plans.
It resolves canonical roles inside the supplied directory, including relocated
diagnostic bundles, without relying on stale absolute paths. Windows index
separators are normalized using path APIs. Missing/unsafe evidence fails visibly.
Only indexed canonical TXT files and explicitly rendered files are reused;
unindexed flat exports do not become dependencies of a generated manual plan.
Diagnostic exports include the transition manifest and explicitly rendered files.
Original manual CSV paths alone are not a relocation-aware uploader.

## Boundaries

Interface layout, OCR, ETA, duplicate protection, selected upload identity and
public output-folder publication/cleanup are unchanged. Alternative front/back
matter exports remain available. Legacy standalone preparation retains its
existing portable files. Historical runs and original source PDFs are untouched.
No live AnythingLLM documents, vectors or workspaces were created in qualification.

## Qualification

Five real PDFs used five distinct settings profiles in paired legacy/private
preparations. Text, manifest records, selected upload metadata/body, effective
settings and public filenames/bytes matched. Every private plan passed durable
prepared-batch checkpoint verification. All three optional artifact kinds were
rendered from extracted diagnostic ZIPs; original summary bytes remained unchanged.
Every one of 438 regenerated alternative payload rows matched retained filename,
text, title, author, description, docSource and chunkSource evidence.

Two additional paired preparations retained actual page parents (one file-upload
native-header case and one raw-text strict case), rather than taking the oversized
parent fallback. Both passed the same export/recovery/relocation checks. Another
32 regenerated alternative rows matched, for 470 checked rows in total. The strict
case verifies the preserved direct/grouped metadata distinction described above.
Its initial longer-page fixture correctly fell back at the existing 4,096-character
effective ceiling; a shorter page was then used to exercise ordinary parents.
The first strict parity attempt identified the existing native-header handoff;
the two-plan/same-TXT preservation was qualified on retry. Earlier attempts remain.

Additional receipts: `tmp-output/optional-parent-followup-20261003/results.json`;
the initial oversized fixture is `oversized-fixture-attempts.json` beside it.
Use `--parent-followup` with both qualification scripts for this small follow-up.

Metadata TXT storage across these five cases fell from 214 files / 270,363 bytes
to 179 files / 136,638 bytes. This is metadata TXT storage only, not whole-run
storage or a corpus-wide estimate; public exports and JSONL evidence remain.

The first relocation attempt exposed the new renderer's incorrect assumption
that selected files lived in a `selected` subfolder. Current production flattens
these files into the document directory. The renderer and synthetic fixtures
were corrected to that layout. An overly broad diagnostic directory addition
was also removed after the existing forensic-export regression test rejected it.
Earlier experimental attempts are preserved in receipts, not treated as successes.

Focused tests cover deferred/manual rendering, exact metadata/body generation,
role relocation, Windows separators, unsafe references and filenames, and the
unchanged oversized-parent resolution guard. Ruff and explicit core Pyright pass.
The broader regression run passed 958 tests and 15 subtests; 31 focused tests
passed after the final optional-renderer dependency restriction. One existing
Starlette TestClient/httpx deprecation warning was not an application failure.

Evidence: `tmp-output/optional-artifact-followup-20261003/results.json`.
Scripts: `experiments/canonical_settings_matrix_20261003.py --optional-followup`
and `experiments/validate_optional_artifact_receipts_20261003.py`.

Changes are not loaded into an already-running server until it restarts.

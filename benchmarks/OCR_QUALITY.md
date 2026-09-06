# Local OCR output regression pack

Run explicitly from the repository with its production Python environment:

```powershell
.venv\Scripts\python.exe -m benchmarks.ocr_output_quality --manifest benchmarks/private/ocr-quality/manifest.json --output benchmarks/private/ocr-quality/latest.json --tesseract 'C:\Program Files\Tesseract-OCR\tesseract.exe'
```

The private pack contains seven visually reviewed pages from five source PDFs:
underlined Gorbman prose, a musical-staves spread, Adorno's title page, both
Marx scan variants, and two Cohen folds. Private PDFs, reference passages and
results are intentionally not published in Git. Preserve the private directory
when cleaning old production runs; it contains independent source copies.

This invokes the actual production page-region OCR function, not a second lab
extractor. It does not exercise full-document candidate selection, upload,
embedding, or the UI. It does not write timing history or reuse OCR results.
Tests under `tests/test_ocr_quality_pack.py` exercise the checker with deliberate
mutations; they do not replace this real-PDF qualification command.

## Ruled-text hybrid qualification (September 6)

Horizontal rules alone no longer imply handwritten underlining. The production
recognition selector distinguishes aligned short printed rules (block layout,
installed model), paired printed heading rules (qualified model, original
raster), and supported prose underlines (existing qualified model/raster path).
Staff detection also checks long overlapping lines when notes fragment a line;
these newly detected cases keep the qualified model with layout mode 4, avoiding
the unnecessary loss of its prose improvements alongside the score.
No source-specific names, post-OCR word substitutions, extra competing OCR
passes, crop-bound changes or changes to ingestion/ETA are involved.

Private matched replays cover every physical page of Gorbman (11), Alba/Nee
(50) and Rossellini (11), using the same runtime and source bytes. Inspect both
changed and unchanged pages; a globally better recognizer is not established.
Alba retains improvements to citation completeness while recovering several
spellings, but not every spelling. Rossellini retains block-recognition gains
without the enlargement artifact. Gorbman's opening-page treatment remains
unchanged: tested alternatives lost a known-correct passage. Its remaining
margin noise is not claimed solved. The staff-page fallback removes musical
symbol debris but retains older lexical limitations. Existing seven-page
reference assertions are not weakened to hide those tradeoffs.

Run-local OCR checkpoint schema 2 prevents a resumed run from using page data
prepared under the prior recognition policy. This is not cross-run caching.
The extra geometry counts and route reasons stay in existing page evidence.
Private experiments/results are under the September 6 audit's `hybrid-*`
directories, not in the production OCR execution path.

Required passages and ordered anchors were checked against rendered pages.
Hashes detect any other normalized-text change, including unexpected bleed;
they do NOT certify every word as correct. Known existing mistakes are stated
in each private manifest's review note. Missing sources, changed source bytes,
changed output, missing verified text and changed region counts fail explicitly.
An output change requires visual review even when it is an improvement. There
is deliberately no automatic baseline-update option. These checks never run
inside an ordinary user processing job.

## Compact production diagnostics

`ingestion-terminal-record.json` now holds one `ocr_diagnostics` rollup from
available source summaries. It is not copied into cumulative Run History.
No extra production files, OCR passes, rendering, or periodic writes are added.
Existing page evidence includes all regions' crop-retry results, not just the
first. Retry counts are region counts; coverage counts are physical pages.

Retained recognition decisions include setup time (crop/contrast/recognition
configuration) and Tesseract subprocess time (including process/model startup),
plus process outcome. `exit_ok` does not mean correct or nonempty text. Times
are cumulative measured calls, NOT parallel run elapsed time and NOT a full
OCR-time decomposition: discarded calls and uninstrumented backend paths are
explicitly excluded. Rendering, full geometry analysis and reconciliation time
are not measured separately here. Do not subtract these partial totals from a
run timer and call the remainder wasted time.

The rollup reports observed/retained coverage, unresolved indicators, recovery
counts and actual recognition profiles. Missing assessments remain unknown,
not successful. Boundary ink is a risk signal, not proof of lost text. Partial
or cancelled runs summarize available source evidence, not work whose worker
never returned. This block is diagnostic only; routing, OCR acceptance, ETA,
reconciliation and completion classification must never consume it.

Measurement page states distinguish timed profiles, partially timed profiles,
profiles lacking valid timing, regions lacking profiles, and absent retained
region evidence. Assessment page states separately count recorded, partially
recorded, and unrecorded evidence. Unrecorded does not establish that an
assessment was ineligible or that another backend performed it. These are
bounded counters in the same terminal record, not new per-file logs.

## September 6 targeted recognition-raster qualification

Confirmed spread regions with the existing annotated-prose recognition profile
can enlarge the recognition raster 1.5x without changing crop fractions, model,
PSM or the single-call policy. Auxiliary and TSV-coordinate routes are excluded.
An input-pixel budget and allocation fallback retain the original route.
Existing page evidence records recognition_raster_scale and any fallback.

A matched 37-source ab7cb74/current local replay completed with equal 1,741-page
coverage and 1,810 segments. Only two pages changed; metadata and recorded
routing/reconciliation decisions did not. This is a modest, mixed lexical gain,
not universally improved OCR: Cohen gains several words but loses two others.
Dense-endnote experiments were rejected rather than introducing citation errors.
The default suite, targeted raster checks, UI suite, seven-page quality pack and
offline recovery scenarios passed. No fresh live provider comparison was made.
Detailed private evidence: version-balance-20260905/HYBRID-RESULTS.md.

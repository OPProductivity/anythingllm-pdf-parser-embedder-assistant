# PDF selection preview: implementation and qualification scope

The first ordinary multi-file selection previously deferred metadata while the
folder route inspected PDFs and ran a metadata preview. Retry followed yet
another chain. The resulting UI could be ready without metadata, or unavailable
without an explanation of the operation doing the work.

Direct, folder, retry and subset selection now share a streaming metadata
preview. It announces recognition before inference and publishes individual
results without a manual refresh. The supporting preview retains its five-PDF
limit, explicitly distinguishes that from the selected total, and never replaces
fresh authoritative processing of every selected PDF after Confirm.

Metadata appears before the ETA calculation. The estimate and optional workspace
identity work have separate announced stages. Actual live observation found an
estimate callback lasting 108.68 seconds while settings reset took 0.04 seconds;
those are observed UI callback times under the test workload, not recognition
benchmark results or universal performance claims. No OCR acquisition is removed.

Per-browser revision tokens suppress obsolete metadata, workspace and estimate
results after a new selection supersedes the old one. Tokens contain no source
or inference evidence. Inferred fields reset when sources change; explicit user
edits survive option changes. Untouched preview author/short-label values are
not promoted to authoritative override arguments at Confirm, so later role
evidence remains eligible during fresh preparation.

The folder list exposes validated source names before all-page native inspection
finishes. Programmatic component updates do not trigger duplicate option chains.
Confirm remains mounted while unavailable, and its immediate feedback supports
both native-button and wrapped-button DOM. Exact active labels avoid treating
the idle text 'Confirm and start processing' as an active run.

## Validation at this commit

- Full offline suite on the prior UI revision: 3,312 passed, one skipped,
  35 deselected, 28 subtests passed.
- Latest estimate-stage/order changes: all 15 metadata stream tests and four
  adjacent picker/selection controls passed. Ruff and whitespace checks passed.
- Opt-in actual Edge client regression: native and wrapped button controls
  pass; the previous client fails the idle/active state control.
- Actual Gradio 6.19 direct and folder previews with the same eleven real PDFs
  reach ready and publish metadata without Refresh. The native OS folder-dialog
  return alone was supplied by a private harness. These checks do not establish
  default full processing or embedding success.
- A real eleven-PDF direct run has started on the separate frozen installation,
  including native layout and targeted OCR. It is still in progress at this
  commit. The latest estimate-stage change still needs a live check.

These are local development results. Both ports, both routes, equal-source output
comparison, actual embedding receipts, source-specific author timings and normal
server restart remain required before live qualification is complete. The frozen
installation retains its original recognition modules; a UI-only overlay is not
an upgrade of that source snapshot.

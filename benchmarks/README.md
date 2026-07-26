# Reproducible medium-PDF benchmark

`manifest.json` is deliberately public and contains only random document IDs
and safe metadata. The ignored private source map supplies the real path and
local fingerprint for each ID. Never add a filename, source path, workspace
name, PDF text, raw timing timeline, API key, or hash to this directory.

The runner invokes the actual `run_automatic` Gradio handler with the approved
ordinary page-preserving defaults. It retains the production progress trace and
ETA reprices, and privately samples the same one-second paced-progress helper
used by the localhost status panel. Calibration therefore uses the visible UI
timeline, not merely the worker callback cadence. It also records two
complementary timing views:

- **Disjoint wall-clock attribution**, which covers every second of a completed
  run exactly once and therefore sums to 100%.
- **Overlapping evidence spans**, which retain independent queue, vector,
  polling, and validation observations without misusing them as additive time.

Run one excluded session warm-up, then two serial trials for each manifest ID.
The runner rejects an unavailable, active, or uncertain queue guard result and
marks recovery, cancellation, manual activity, provider authentication failure,
or missing attribution as `invalid_for_calibration`.

Private map shape (keep outside Git):

```json
{"documents": [{"document_id": "B01", "path": "C:\\private\\source.pdf", "fingerprint": "private"}]}
```

The runner writes private raw evidence below `benchmarks/private/` and an
operator-visible, safe `benchmark-status.json` beside the selected public
result location. A change to the presentation-controller version makes earlier
trials stale for calibration; the status must remain `awaiting-rerun` until all
16 current-version trials exist. Review generated results before adding any of
them to Git.

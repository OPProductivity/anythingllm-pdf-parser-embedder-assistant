# Private Evidence Retention

Private run-state evidence is now retained after success. The configured
application run-state root is resolved structurally, including the supported
application-home override; similarly named public output folders do not match.

The private branch still materializes exactly the same selected export records,
but copies the transcript instead of moving it out of its diagnostic tree,
preserves the full summary and skips forensic-tree pruning. Worker receipts,
batch receipts and private TXT diagnostics also survive post-publication
cleanup. Metadata evidence is materialized even for lean private preparations.
The normal standalone cleanup outside private run-state remains unchanged.

Neither public publisher was broadened: the GUI and CLI still select only the
complete transcript and selected flat page/segment TXT records. Whole-file
mode still exports one file per PDF. No diagnostics, logs or subdirectories are
published. Existing user output directories were not modified.

## Qualification

- Eight paired cases compare old versus private-preserving behavior across
  local/shared-upload, whole-file/page-passage and GUI/CLI publication. Exact
  output filenames and bytes match, while all seeded private evidence survives.
- Boundary test rejects similarly named public output directories.
- 76 focused and adjacent tests passed, plus six legacy cleanup tests.
- Real local-only CLI run `run-20261002-181744` retained 106 private files,
  including diagnostics and manifests, and published seven flat TXT exports
  with no non-TXT items or directories.

## Warning Investigation

Run `r-20261002-174505-94101bd49f` submitted 214 records to
`second-time-but-different-workspace`. The owned Desktop queue receipt arrived
in about 0.05 seconds. Exact reconciliation stopped at its 480-second limit
(480.4285 seconds observed), with no deadline extensions. The final queue
snapshot recorded 129 completed, 130 current, and about 281 seconds estimated
remaining. The observer was connected with zero recorded failures. Thus the
warning did not establish upload rejection or wrong settings: processing had
not finished within the assistant's observation window.

A subsequent read-only check matched all 214 selected document paths to their
workspace links, all 214 SQLite vector mappings, and all 214 actual LanceDB
vector IDs. The records are now physically indexed. This is not a fresh chat
retrieval test. No resubmission, queue mutation or reconciliation write was
performed, and the original warning evidence remains unchanged.

The earlier successful run's deleted diagnostics cannot be reconstructed
faithfully from its compact receipts. This change prevents future pruning;
it does not claim to restore already deleted files.

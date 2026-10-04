# Partial source rejection qualification

## Failure reconstructed

Run `r-20261004-214235-d414f821b8` staged 67 page-parent documents from
four PDFs (15, 13, 22 and 17 records). The first two sources returned
`Connection error.` before namespace commit. The other sources committed
39 documents. The assistant nevertheless waited for 67 vectors for roughly
eight minutes, reported zero confirmed records, and failed its terminal audit.

The filter expected top-level `docSource`, whereas the production converter
places it inside `metadata`. It also iterated a list-shaped snapshot but
received the live observer's source-keyed dictionary. Existing tests supplied
simplified top-level payloads. Separately, the auditor did not recognize the
existing `source_queue_rejected_without_remote_mutation` state. Despite that
historical name, this state permits global file staging, not vector commitment.
The primary archived assistant history also describes this audit-state problem
in its September 4 investigation; it was not introduced by today's cleanup.

## Contained correction

- Read canonical nested source identities, retaining legacy top-level support.
- Accept live rejection dictionaries and durable rejection lists. Exclude
  ambiguous and unrelated sources; require aligned, nonempty attachment paths.
- Invalidate cached observations when the exact verification target changes.
- Stop an entirely rejected group without waiting for impossible vectors, but
  never classify that group as searchable.
- For mixed requests, let the source-aware verifier partition rejections rather
  than rejecting the whole request before any sibling document has started.
  Preserve the existing single-source shortcut.
- Audit staged queue-rejected sources independently from pre-upload rejections:
  planned/staged/location counts agree, locations are unique, confirmed vectors
  remain zero, and later sources are released.

Physical vector verification is unchanged. OCR, parser selection, provider
requests/retry policy, ETA, UI design and output cleanup are unchanged. No
historical artifact, document, workspace or embedding was modified.

## Verification

- `tests/test_partial_source_rejection.py`, `tests/test_reliability_audit.py`
  and `tests/test_final_audit_repairs.py`: 76 passed.
- Focused pipeline queue, source-atomic, scheduling, grouped-upload and
  confirmation regressions: 31 passed.
- Submission-vector confirmation, ingestion observation, prepared-batch
  recovery and shared run evidence suites: 41 passed.
- Production callback/coordinator tests use real upload-plan conversion and
  ledger/audit writers. Only remote attachment/queue and storage boundaries
  are synthetic. They cover the exact 67/28/39 split, whole-file and segmented
  records, late rejection events, all-rejected groups and complete success.
- A read-only replay of the actual run's four canonical plans and exact
  attachment locations excludes 28 rejected records. The unchanged production
  fast verifier physically matched all 39 remaining vector IDs, returned
  `current_submission_exact_vector_coverage`, and took 3.464 seconds. This is
  an evidence replay, not a new parsing/embedding benchmark.

The two original connection errors' underlying socket/SDK causes remain unknown
because the recorded adapter messages do not retain them. This correction
handles those failures truthfully; it does not claim to prevent network errors
or introduce automatic resubmission.

Activation requires restarting the idle PDF assistant only. AnythingLLM Desktop
does not need a restart because its embedding adapter was not changed.

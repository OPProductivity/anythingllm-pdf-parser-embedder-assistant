# Segmentation matrix qualification

Shared-pool P02; committed source snapshot `76f85e9`. Retained experiments:
`C:/Users/Ninkear/.codex/tmp/shared-p02-segmentation-20261005`.
Original Ruggiero, Mullins and Harkins PDFs were only read; all source hashes
were checked. Each case used a fresh profile and production cancellable workers.

## Local preparation

The same 21 physical pages conserved normalized per-page text across four modes:
whole-file (3 records), page-preserving (21), shorter page-local passages (155,
target 450), and custom groups (9, pattern 2/3). Page-preserving ceiling was 2400.
Every segment retained unique identity, valid physical-page bounds, exact text
span lengths and page-line bounds. Whole/group page-span slices were checked
against emitted text. Titles/authors were invariant across modes. Every mode
exported exactly one output TXT per source.

Separate whole-file/custom-group cases agreed when front/back matter were
excluded (14 retained pages), and with first page 2/end-before page 6 overrides
(12 pages). All eight cases completed successfully, with original PDFs and
production research workspace/document/vector counts/config hash unchanged.
Evidence: `qualification.json`, `qualification-body.json`,
`qualification-range.json` and per-case dereferenced summaries/profiles.
Thirty-three focused existing segmentation/provenance regressions passed.

## Fresh live continuation

Each live case started an owned native AnythingLLM backend with schema-only
empty storage, fresh PDF parsing and no previous payload/vector reuse. The
backend was rendered in the lab from the preserved native backup; the installed
Desktop process and research storage were not altered by these trials.

| Mode | Selected / Confirmed Records | Seconds |
| --- | ---: | ---: |
| Whole-file | 3 / 3 | 56.644 |
| Custom groups | 9 / 9 | 48.944 |
| Page-preserving | 21 / 21 | 36.305 |
| Shorter page-local passages | 155 / 155 | 56.030 |

All four ran through native upload, physical vector confirmation, clean
integrity audit and manual retrieval (HTTP 200, eight returned results).
Whole-file/group records may legitimately span multiple downstream vectors;
these counts are selected-record confirmations, not claimed page-perfect
downstream chunk counts. Timings are functional controls, not comparative
benchmark claims.

After owned child shutdown, test workspace/document/vector/API-key rows were
zero; test namespaces, vector-cache entries and uploaded documents were removed.
Original source hashes and production research state were preserved.
`live-qualification.json` indexes the four qualified results.

Two additional successful ingestion trials were excluded from preservation
qualification because the parallel bridge review deployed new native backend
bytes to disk at 12:08:25 without restarting Desktop. Their snapshot comparison
correctly detected this change. The peer reported old server SHA-256
`51dfb3a0f967f801788fac96f1e06e90d5b40a77da192a50d8f4ec3050395c63`
and new SHA-256
`2121070357a4f9b86c36f486199e4fae9610d87db3d7706c8804ab88d9d99cf3`.
The harness now stores both snapshots and changed-field names. Fresh page and
passage reruns then passed unchanged-state checks; original failed-check reports
remain intact. No assertion was weakened or retrospective success fabricated.

## Conclusion

No segmentation defect was demonstrated. No segmentation, extraction, OCR,
ETA, output cleanup or interface code was changed for P02. This qualification
does not promise correctness for every possible PDF or replace P04's actual
same-browser clear/retry setting tests.

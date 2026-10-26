# Upload-only crossover test and requested rollback

The user requested a small real upload-only comparison against b8d27d3 and restoration of that production baseline. The test used exact Git snapshots of b8d27d3 (A) and b8e930a (B), in A1, B1, B2, A2 order. AnythingLLM Desktop was not restarted between trials.

Two existing prepared sources were selected: De Grazia, Irresistible Empire introduction (17 records), and Nancy Fraser, Rethinking Recognition (14 records). They include requests that were slow in the earlier full ingestion. All 31 prepared texts, titles/authors and source identities were frozen once and reused byte-for-byte. No PDF parsing or author inference was performed.

## Measured results

| Trial | Snapshot | Upload/embedding/confirmation seconds | Embedding SDK seconds |
|---|---|---:|---:|
| A1 | b8d27d3 | 18.597 | 12.587 |
| B1 | b8e930a | 7.728 | 2.296 |
| B2 | b8e930a | 7.675 | 3.086 |
| A2 | b8d27d3 | 16.679 | 6.977 |

Mean upload window: b8d27d3 17.638 seconds; b8e930a 7.701 seconds. These four observations did not reproduce the earlier slowdown. They do not establish a general speed advantage for either version.

## Freshness and preservation

Each trial created a new isolated workspace and uploaded new document locations. The harness forced the prepared-record cache lookup to miss identically in both snapshots, without changing production code. Backend source plans independently confirm zero old-cache resolutions; each trial records two successful embedding SDK calls covering all 31 chunks. All 124 records were genuinely requested for embedding, accepted and exactly vector-confirmed. Source-processing and fitz.open entry points were guarded to fail on PDF parsing; there were zero attempts.

Uploaded content and citation fingerprints match for all 31 records across all four trials. Fresh IDs, ingestion timestamps and temporary URLs are excluded. The frozen fixture remains unchanged. The four temporary workspaces, their namespaces, 124 unshared stored records, SQL links and cache entries were removed after validation. Other workspaces remain unchanged, including the retained 31-PDF comparison workspace.

## Requested rollback

Production was restored to the b8d27d3 implementation. All 114 production Python modules match that Git baseline after newline normalization. The two b8e930a optimizations and their completion-notification-specific test are reverted. Historical release documentation and experiment evidence are preserved. Twenty-seven focused regression tests pass. The rollback uses a new commit so shared main history is retained.

This rollback follows the user's requested baseline choice. It is not described as a confirmed fix for the earlier 14-minute upload window: this limited crossover test did not demonstrate that regression.

## Limits

The test isolates upload of two sources/31 records, not the original 31 sources/1,659 records. It does not recreate thermal, memory, or backend state after a full parsing/OCR run, and does not determine the upstream cause of occasional slow SDK calls. There is no new author-recognition score, full presentation benchmark or manual author validation. Workspace creation, imports and cleanup are outside the upload timer. SDK time is nested inside that timer.

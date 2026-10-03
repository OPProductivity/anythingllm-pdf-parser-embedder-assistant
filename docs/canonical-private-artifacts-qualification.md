# Canonical private artifacts

Future private document runs store audited identical static artifacts once, at
ordinary descriptive filenames. `artifact-locations.json` records relative role
paths and their actual files. No hard links, symlinks, compression, or historical
run migrations are introduced.

## Scope

- Selected extracted text moves from its candidate location to the established
  descriptive parsed-TXT filename instead of being copied twice.
- Identical candidate/variant bodies and JSONL manifests reuse a physical file.
  Different post-selection metadata still produces a separate manifest.
- Selected extraction/layout/retrieval reports reference candidate evidence
  rather than making another selected copy.
- Strict/native segment and page-parent file uploads share identical TXT bodies,
  but their plan rows retain separate metadata and multipart upload filenames.
- Redundant default upload-plan copies are replaced by existing native plan paths.
- Summaries, provenance, checks and downloads use the updated actual paths.
- Diagnostics bundles include the canonical path index and its verified files,
  plus reachable shared JSON evidence, so they remain inspectable after moving.

The in-memory content lookup uses length/digest and verifies exact bytes before
reuse. It does not create hashed filenames or introduce persistent blob references.
Static canonical files cannot be overwritten with different content.

## Preserved Boundaries

The output-folder publication, flat TXT filenames, retention and cleanup remain
unchanged. Existing public exports can therefore still require their own copies.
Mutable transaction/checkpoint records remain independent. Private automatic-run
manual test kits and compatibility probes retain their CSV plans and checklists,
but reference canonical TXT files rather than writing duplicate payloads. Their
empty payload-directory download entry is omitted. A kit directory alone is no
longer portable; complete diagnostic bundles include the referenced files. CSV
metadata is an expectation record, not a Desktop UI metadata-import mechanism.
Explicit legacy standalone preparation without a private catalog still produces
its portable kit. Historical runs and existing `.run-evidence` storage are unchanged.
This is deliberately not a claim of zero duplication across every artifact.

## Production-Path Qualification

Prepare-only legacy and private runs used the actual Gunning nine-page article
and Garncarz seven-page article. No AnythingLLM writes were performed. Compared
primary TXT bytes, manifest records, upload metadata/body rows and published flat
output filenames/bytes were identical. Provenance paths resolved.

| Document | Before | Canonical private | Reduction |
| --- | ---: | ---: | ---: |
| Gunning | 646,278 bytes | 386,039 bytes | 40.3% |
| Garncarz | 1,065,561 bytes | 648,818 bytes | 39.1% |

These are whole document-directory measurements before retention, not estimates
for every corpus file. Receipts: `tmp-output/canonical-check2/results.json`.
Focused tests cover identity/metadata preservation, missing/changed files,
out-of-root paths, promotion, overwrite prevention, and relocated diagnostics ZIPs.
The two real-document diagnostic bundles were also extracted to unrelated temporary
folders: all 51 and 60 indexed roles respectively resolved, ZIP members were unique,
and the resolved shared-evidence summaries matched the original runs.

Implementation is not a running-server deployment until the server is restarted.

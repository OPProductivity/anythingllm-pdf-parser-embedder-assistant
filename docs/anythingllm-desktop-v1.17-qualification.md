# AnythingLLM Desktop v1.17.0 qualification

Qualification date: 2026-10-02. This record covers the installed Windows x64
Desktop package and the assistant's existing native ingestion contract.
It does not grant authority to other releases or modified packages.

## Diagnosis

The retained 12:26 local-time attempt stopped before workspace creation with
`AUTO-COMPATIBILITY-001`. Its own compatibility report identified Desktop
**1.16.2**, not 1.17.0. Both releases were outside the existing qualification
registry, which ended at 1.16.1. At investigation time, the installed executable
and package identified **1.17.0**. The failed attempt prepared and uploaded no
PDF records. There is no evidence that PDF extraction caused this warning.

The vendor published v1.17.0 on 2026-10-01:
https://github.com/Mintplex-Labs/anything-llm/releases/tag/v1.17.0
Release notes are context only; the installed package and live tests establish
the assistant's compatibility.

## Scoped changes

- Add a separate settings profile and native contract for exactly 1.17.0 and
  `app.asar` SHA-256
  `32aaa9e127ff27b0d909ce90146eab74ff0b694adbaba8e9a8a00a364ac4786a`.
- Extend the optional bridge installer's existing exact-anchor profiles to
  1.17.0: preload constant `KX`, startup function `ZX`, main window `jX`.
- Cover the new profile and reject changed fingerprints or a different release.

No parsing, OCR, segmentation, metadata inference, queue, recovery, timing,
output retention, or UI implementation was changed. Earlier profiles remain
intact. The installed Desktop archive was not patched, and the optional bridge
remains uninstalled. Installing it would change the package fingerprint and
require independent qualification of that modified package.

## Qualification Evidence

Required SQLite structures match. Installed OpenAPI documentation contains all
four required core routes: raw-text, file upload, workspace list, and embedding
update. Documentation discovery does not itself grant write authority.

An isolated native contract run through existing assistant functions passed:

1. Temporary API key creation.
2. Disposable workspace creation.
3. Native raw-text metadata upload and workspace linking.
4. Exact vector and source-identity confirmation in SQLite and LanceDB.
5. Runtime embedding verification and source retrieval.
6. Workspace, exact raw-document, and temporary-key cleanup.

The before/after baseline matched for existing workspace IDs, API-key IDs,
workspace-document/vector counts, settings values, and the environment file.

The installed backend still reads the two text-splitter labels from
`system_settings` and loads the storage `.env` through dotenv. Guarded settings
write and restoration also passed against a local schema clone, with no real
Desktop configuration changes.

The normal Automatic production path was exercised with generated two-page
PDFs, using a separate assistant data directory and disposable workspaces:

| Case | Outcome |
| --- | --- |
| One PDF, All in one file, new workspace | One record uploaded and exact vectors confirmed |
| Repeat the same PDF in that workspace | Already-indexed record re-verified and skipped; no upload |
| Two PDFs, page-preserving mode | Four records uploaded and exact vectors confirmed |

All production-run integrity audits passed. Production runs retain their normal
deferred optional retrieval behavior; retrieval was separately proven by the
isolated contract run. The initial whole-file run was also successful, although
the local test harness initially looked for its summary in the wrong directory.
The harness's cleanup predicate was subsequently corrected using exact fixture
source hashes and recorded locations. All test workspaces, document files,
vector tables, and test-owned credentials were confirmed absent afterward.

The bridge validator recognizes `anythingllm-1.17-main-window-j` and reports
`CanInstallOrUpgrade: True`. An archive-copy audit confirmed one occurrence of
each anchor, the main-window identity, patched JavaScript syntax, and ordering
of window creation before bridge startup. This is installer qualification,
not evidence of a running v17 bridge.

## Regression Checks And Limits

- Compatibility/authority tests: 15 passed.
- Final compatibility-module recheck after bridge cleanup: 14 passed.
- Focused compatibility, persistence, temporary-key, whole-file and bridge
  checks after the installer update: 34 passed, 908 deselected.
- Full offline suite: 1,998 passed, 2 failed, 34 deselected, 15 subtests passed.
- Ruff and diff whitespace checks passed.

Both full-suite failures are in
`test_compact_run_names.py::test_timestamp_promotion_preserves_staging_and_existing_exports`.
They also reproduce with the committed, pre-change compatibility module. Their
fixture puts public output and private staging under the same directory, while
the existing promotion implementation expects separate roots with matching run
names. Neither that implementation nor those tests were changed for this task.
This record therefore does not claim an entirely green project suite.

The installed 1.17.0 package and tested operation families are qualified;
1.16.2, future versions, modified archives, and every possible PDF/provider
combination are not implicitly qualified. No downgrade or reinstall was
required. The scoped changes were subsequently prepared for publication at
the user's request.

## Subsequent Production Run Review

The two subsequent user runs were reviewed without changing their artifacts
or workspace contents:

| Case | Elapsed | Live storage confirmation | Delivered output |
| --- | --- | --- | --- |
| Ten-page PDF, whole-file mode, new workspace | 19.33 seconds | One document link; four internal vectors, with exact SQLite/LanceDB IDs matching | One TXT |
| Nine-page PDF, page-preserving mode, same workspace | 98.22 seconds | Nine document links; nine vectors, with exact SQLite/LanceDB IDs matching | Complete TXT plus nine page TXTs |

Both retained integrity audits passed with no findings, and a fresh read-only
audit also passed. All expected source identities and document files were
present. The first source remained intact after the second upload. Both queue
groups completed, temporary API keys were deleted successfully, recovery was
not needed, and the delivered folders contained no logs or subdirectories.

The first run reused a cached document and cached embeddings; its four vectors
reflect normal provider splitting of one whole-file record. The second run
freshly embedded all nine records and spent approximately 82 seconds waiting
for embedding completion. Its initial 60-second estimate was optimistic.
Owned queue receipt followed by exact-vector reconciliation was the established
asynchronous path, not a failed HTTP submission or retry.

Optional per-document retrieval/chat validation remained deferred after exact
vector proof, as designed. The older version-specific source-atomic provider
patch was not enabled on 1.17.0; these runs used the established unmodified
Desktop queue path. This review does not claim those optional paths were run.

The installed 1.17.0 archive already contained no refresh bridge. Its stale,
inactive bridge connection descriptor was removed, and the old diagnostic log
was moved into the ignored local qualification archive to preserve history.
No old application archive was restored, no new bridge was installed, and
the installed package fingerprint remained unchanged. The optional installer
and existing assistant behavior were preserved.

Machine-local receipts and qualification scripts are preserved under the
ignored `tmp-output/v117-qualification` directory. They are not public profile
data and must not be committed as a group.

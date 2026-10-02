# Workspace selection, reset, and cached-document qualification

Qualification date: 2026-10-02. Installed AnythingLLM Desktop: 1.17.0.
This follow-up is separate from release compatibility qualification.

## Verified Causes

- Explicit workspace refresh replaced the selected slug with the New workspace
  sentinel. A rename should update the label, not change the destination.
- Reset preserved a valid existing workspace even during explicit clear/retry.
  A concurrent change from re-emitted retained files could restore that target.
- The detailed-evidence checkbox was absent from per-run reset outputs.
- Folder-batch clear did not clear the viewed run root or finish the selection
  transaction, allowing old presentation state to survive.
- Fully indexed fast skips supplied document results but no source mutation
  transactions. Terminal PDF counts incorrectly depended on those transactions.
  Their record counters also described existing vectors as new queue work.
- Fast skips correctly produced no TXT files, but finalization required a new
  TXT export and issued `AUTO-TEXT-OUTPUT-PUBLISH-001` after exact vector proof.

## Bounded Changes

Refresh preserves explicit intent by stable slug, including local-database
fallback. A missing/cleared selection does not silently request creation.
Initial-load defaults are unchanged.

Clear/retry restores all per-run defaults. Retry retains the selected PDFs;
clear removes the relevant picker selection. Saved default profiles and shared
AnythingLLM engine/model configuration are not rewritten. Active-run guards
remain intact. An explicit reset owns its concurrent file-change callbacks;
completed acknowledgement replays cannot overwrite later settings.

Terminal source counts use proven document receipts, including zero-mutation
workspace skips. Existing records and queue completions remain distinct.

With explicit user approval, a source already proven fully indexed in the target
workspace need not reproduce its local TXT export. Mixed batches still require
exports for other sources. A global cache hit, missing target link, incomplete
vector proof, or local-only run does not qualify for this exception. Existing
selected-input duplicate filename conventions remain intact.

No components, layout, segmentation, metadata inference, cache identity, or
duplicate-protection rules were redesigned. No Desktop package patch was made.

## Drawer Evidence

The selected existing workspace contains the ten reported page-parent records, with
their matching SQLite and LanceDB vectors. They were linked on September 21.
The current native drawer was opened and source pages 7-10 were visually seen.
All ten locations were returned by the drawer's own workspace and document-path
metadata endpoints, with unique document IDs.

The right-hand linked-document list is not alphabetical or a list of only newly
added records. This source block occurs at rows 1065-1074 of 3398, between older
document blocks. The left-hand search concerns unlinked documents. These facts
locate the entries in the current drawer; they do not establish whether an
earlier view was stale or the block was missed. No speculative drawer change
was made, and the unintended non-test workspace was not deleted.

## Production Browser Verification

Three original PDFs from the user's All sources folder were used without
modifying or deleting them:

1. Changed metadata, mode, target workspace and detailed-evidence setting.
2. Retried the same files: all three retained, defaults restored, controls
   re-enabled. Later settings changes remained effective.
3. Refreshed an existing workspace: its selected slug remained unchanged.
4. Cleared with X: files and per-run settings reset; fresh selection worked.
5. Created one explicitly named disposable workspace and ran native
   page-preserving upload: all three PDFs were entirely cache-backed; twenty
   cached records were linked to that workspace and exact vectors confirmed.
6. Repeated in that existing workspace: twenty existing records confirmed,
   zero attachments, zero queue requests/completions, three of three PDFs
   confirmed. After the approved export adjustment, completion was successful
   with no new TXT files or output folder for the skipped run.

Production runs retained the established deferred optional retrieval policy;
this qualification proves exact indexing, not a new retrieval/chat test.

## Cleanup And Preservation

All twenty reused locations were already referenced by non-test workspaces.
Their cached document files were preserved. Native API removal deleted only
the twenty test-workspace links, vectors, and test-owned vector mappings;
the disposable workspace and its LanceDB namespace were then removed. The
temporary cleanup credential was also removed. No global remove-documents
operation was used.

Before/after verification matched all three preexisting workspace identities,
6805 links, 6808 protected vector mappings, cached document file hashes, and
the contents of all three original LanceDB tables. The three source PDF
SHA-256 hashes were unchanged. Local test evidence and screenshots remain in
ignored `tmp-output/workspace-reset-qualification`; do not commit that folder.

Focused reset, reporting, export and worker-contract tests passed after the
final state-contract adjustment. Core configured type checking, Ruff, secret
scanning and whitespace checks passed. The earlier full-suite baseline has two
existing compact-run-name fixture failures, described in the v1.17 qualification
record; neither that implementation nor those fixtures was changed here.

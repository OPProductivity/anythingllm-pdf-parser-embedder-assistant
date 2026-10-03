# Evidence review follow-ups

## Findings and provenance

The 34-PDF preparation comparison used retained historical manifests, not fresh
executions of every old Git revision. Thirty-three text bodies were identical.
The Racial Middle retained its previously excluded page-1 cover; Gans, Franks and
O'Brien had the expected metadata repairs. Those extraction differences are
separate from the five evidence-review findings below.

1. The Desktop queue wrapper appended SSE events after the batch function's final
   ledger write. This predated the latest cover/manifest changes. Aggregate output
   could preserve the observations while individual queue-group journals did not.
   The wrapper now saves the final result after observer shutdown. Intermediate
   saves explicitly mark observation pending; an observer still alive at the join
   boundary cannot claim complete evidence. SSE remains advisory, not vector proof.
2. The journal introduced in e601db4 kept every complete event list in a permanent
   process-global cache. It now reloads append-only evidence when writing and keeps
   no persistent event cache. Only the current sequence is held transiently. This
   trades bounded disk reads for eliminating lifetime memory accumulation. Prior
   sequences remain intact, and a shorter supplied prefix cannot claim an exact
   complete history.
3. The new historical replay harness reused existing result files without checking
   code/settings/source freshness. Every invocation now has a distinct result and
   worker directory, always launches workers, and preserves earlier receipts.
4. The new isolated audit accepted a vacuously empty or wrong returned source set.
   It now requires the exact nonempty unique expected set and separately checks
   complete group/aggregate journal agreement. Qualification is an explicit gate,
   not a completion label. Audit database access is read-only; disposable API-key
   cleanup belongs to the owned isolated runner after process shutdown.
5. Older mixed-scan and visual-text diagnostic directions named bare files that
   canonical consolidation can relocate. They now use the selected canonical
   paths, or name the exact role in artifact-locations.json when unavailable.

No OCR mechanism, ETA, interface, duplicate protection, submission ownership,
vector acceptance rule or public-output cleanup was changed.

## Verification receipts

- Final complete offline suite: 2,169 passed, 34 deselected, 15 subtests passed in
  277.95 seconds. Three dependency deprecation warnings remained; no failures.
- Final focused queue/event/diagnostic regression selection: 33 passed.
- Final journal/source-identity/diagnostic regression modules: 20 passed.
- Evidence/fresh-worker/source-atomic modules: 40 passed, including the fresh
  invocation regression test added after the initial focused selection.
- Full-suite reruns exposed an observer double missing is_alive() and an older
  receipt fixture that emitted its event after a 200 ms timeout even without POST.
  The doubles now match the real thread contract; the receipt fixture waits for
  actual POST and read-abandonment signals and joins its real emitter thread.
  The corrected receipt test passed ten consecutive runs. Production receipt
  ownership and its deadlines were not changed.
- Ruff checks passed for the changed production, experiment and new test modules.
- Two fresh Harkins preparation invocations completed in 14.209 and 14.370 seconds,
  with different worker/result roots; no historical result was reused or replaced.
- Fresh isolated shared-workspace replay: isolated-mixed-6a90ec. Nine PDFs, nine
  selected and exactly confirmed records, 134 physical vectors, zero cache reuse,
  no errors. All 36 control events match the top ledger and three group journals.
- Strict audit exit status was zero; source identity and queue history both passed.
  Receipt: live-audit-0280fb4d62ef4eb8bbc53abd35fe327b.json under the ignored
  tmp-output/historical-replay-20261003 directory.
- Cleanup left zero test workspaces, document links, vector mappings, API keys,
  vector-cache files and LanceDB namespaces; owned test processes stopped.

The revised audit also correctly rejects older incomplete group histories and
missing source-local reports. It still records the 1,032 retained aggregate events
from isolated-full-878099; rejecting group completeness does not erase valid
record-level confirmation evidence. Original PDFs and research storage are intact.

## Deployment

Code commit 93891b6 passed all commit hooks. The owned assistant was verified idle,
stopped through its ownership-checked CLI, and started detached with a hidden
window. At 19:49 local time, new root PID 20368 owned port 7860 and the Gradio
configuration endpoint returned HTTP 200. The original AnythingLLM process stayed
PID 9676 with its 15:30:50 start time; its port-3001 ping also returned HTTP 200.
No push was performed.

## Larger-case timing observation

In the additional full-corpus embedding replay, the 103-record source completed
in 464.9164 seconds, compared with 134.5462 seconds in the earlier qualification.
The owned queue receipt took 0.0529 seconds and exact-vector observation took
0.292 seconds. Cache files continued appearing during ingestion; recent progress
kept the source live. Its final history retains all 312 events despite the bounded
96-event ledger tail. The journal was not repeatedly rewritten during this stretch.
This distinguishes active ingestion from an idle final-confirmation delay, but
does not establish which native/provider factor caused the slower throughput.
No ETA or liveness deadline was changed to accommodate the replay.

## Full-corpus live qualification

The additional replay isolated-full-d1d0b6 passed all 34 source identities and all
310 selected records, confirming 483 physical vectors with zero cache reuse and
no errors. All 1,032 observed control events match the individual queue-group and
aggregate histories. The strict audit returned zero and qualification_passed=true.
Receipt: live-audit-acf44d221e5f4e20981effc0ce1b8444.json in the ignored receipt root.

This phase reused retained prepared canonical TXT inputs deliberately, but used
fresh empty AnythingLLM vector storage; it was not another complete OCR replay.
The two independent Harkins preparation invocations and automated fresh-worker
test separately verified that the preparation harness no longer skips workers.

Cleanup verified zero workspaces, document links, vector mappings, API keys,
vector-cache files and LanceDB namespaces, with no cleanup errors. All owned test
processes stopped. Research workspaces, vectors, PDFs and historical evidence were
not modified. The strict audit still distinguishes older retained aggregate
evidence from incomplete historical group journals instead of relabeling them.

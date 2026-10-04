# Earlier production checkpoint

## Superseded diagnostic rollback

After reviewing the scope, the user requested retention of author/editor,
accented-label, cover-boundary, evidence and vector-confirmation fixes. Production
source is therefore restored to the pre-security `ad798af` tree, not the older
checkpoint below. Security edits remain excluded. The temporary rollback is
preserved in Git history as `1d5c4b0`.

The older-checkpoint trial `r-20261004-114606-7f7f13d9db` finished in 521.210
seconds but reused 291 records and freshly embedded only 284. This timing is
explicitly invalid as an uncached speed comparison. Its 575 newly generated TXT
payloads were byte-identical to the reference, but that did not guarantee fresh
backend embedding calls.

The user authorized removal of existing benchmark embeddings, including the 291
locations also linked to the historical automatically named workspace. Cleanup
is scoped to the exact 575 submitted locations; workspace containers, unrelated
records, historical runs and source PDFs are retained. The next benchmark must
produce 575 new provider calls in addition to zero assistant cache reuse and
575 fresh TXT exports.

## Original rollback record

At the user's request, restore production source and its matching tests to
`e8258e6e346173a240a75ec2045857d581663dcb` in a history-preserving commit.
Historical investigation documents, experiments, source PDFs and run artifacts
are retained. Security work remains deferred.

This checkpoint precedes the submission-scoped physical-vector confirmation
overhaul and subsequent metadata, label and evidence refinements. It retains
canonical artifact storage, on-demand diagnostic exports and cancellable SSE.
This is a deliberate diagnostic rollback, not a claim that these later changes
caused the observed slowdown.

Before rollback, the fresh eight-PDF benchmark produced 575 new records in
1462.605 seconds, compared with 900.911 seconds for the earlier reference.
Both used identical production source. The additional time was concentrated
inside Desktop embedding groups; final vector checks totaled 3.702 seconds.
The exact provider-side reason remains unproven.

All 575 benchmark-owned records and vector caches were removed and cleanup
verified before restart. The 8054 protected unrelated records were preserved.
AnythingLLM itself must not be stopped or restarted for this deployment.

## Valid uncached comparison

Production was committed as `31c8ad5` and the assistant restarted with PID
18656. Its runtime files are byte-identical to pre-security `ad798af`.
The original AnythingLLM GUI/API process creation times were verified unchanged.
The restored confirmation, recognition, metadata, cover-boundary and evidence
paths passed 101 targeted tests. The intermediate narrowed rollback had passed
1018 tests and 15 subtests before the confirmation path was restored.

Run `r-20261004-120456-25686cf89e` completed successfully:

| Measurement | Earlier reference | Slow comparison | Valid new run |
| --- | ---: | ---: | ---: |
| Benchmark seconds | 900.911 | 1462.605 | 972.257 |
| Group 1 seconds | 425.8523 | 580.8305 | 384.6921 |
| Group 2 seconds | 245.0462 | 680.5080 | 315.2816 |

The new run used the same eight PDFs/settings, generated 575 new TXT exports
under its own run directory, and made exactly 575 fresh Desktop provider calls.
Assistant cache reuse was zero; TXT SHA256 multisets matched the earlier
reference. It took 33.53 percent less time than the slow comparison, but 7.92
percent longer than the earlier reference. No production diff existed during
the run. These results do not establish a code-caused speedup.

Observed Desktop embedding/provider intervals totaled 653.103 seconds, including
13 intervals at least 10 seconds long (208.964 seconds combined; maximum
31.075 seconds). These are log-bracket timings, not upstream HTTP status or
packet evidence; the exact provider/network reason remains unproven.

Exact vector observations totaled 5.399 seconds. Final batch confirmation was
logged at 12:20:19; the assistant finished at approximately 12:20:56. This roughly
37-second finalization tail is separate from embedding and is not yet fully
attributed. The comparable tails were approximately 9.72 seconds in the earlier
reference and 11.49 seconds in the slow comparison. This is an unexpected
difference, not evidence that the unchanged source is its cause.
Cleanup evidence is stored in this run's `operator-requested-cleanup.json`.
Native API cleanup removed all 575 documents/caches and physical vectors, but
left three owned orphan SQLite mapping rows. Read-only LanceDB version 6585
proved those vector IDs belonged to benchmark Schaeffer pages 136/150 and Oxford
page 11. After confirming physical vectors and workspace links were absent, an
exact three-row SQLite transaction removed only those remnants. It verified all
7763 unrelated document records, unrelated vector mappings and workspace
containers unchanged. Older unrelated orphan mappings were not removed.

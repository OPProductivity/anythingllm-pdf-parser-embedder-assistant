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

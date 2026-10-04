# Earlier production checkpoint

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

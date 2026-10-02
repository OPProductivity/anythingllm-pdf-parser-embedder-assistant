# Grouped Queue Deadline Liveness

The 214-record run `r-20261002-174505-94101bd49f` reached its initial
480-second observation boundary with its owned observer connected, 129 records
complete, record 130 current, and its last event about 49 seconds old.

The batch verifier had two separate recency conditions: queue-position
advancement within the existing 90-second stall interval, and an event age
of at most 15 seconds. Both were required for a queue-backed extension.
Advancement resets the last-progress timestamp, not the initial wall-clock
boundary. Thus recent advancement could satisfy the stall check while the
additional 15-second event-age gate still refused an extension. Deferred
storage reads while the owned writer is active meant there was also no fresh
vector-count advancement to independently justify extending the deadline.

## Narrow Correction

The batch boundary event-age gate now uses the existing 90-second stall
constant, consistently with its active-queue storage deferral. The ownership,
connected-observer, unfinished-queue, recent-position-advance, cancellation,
exact-vector and no-resubmission checks remain unchanged. No global timeout
was increased and no UI or export behavior changed.

## Verification

Five clock-controlled production batch-verifier cases cover connected owned
queues with 49-, 89-, 90- and 91-second gaps, plus a disconnected observer.
The first two now extend and complete after the initial 480-second boundary.
Stale or disconnected cases remain bounded. All cases submit exactly once.
Restoring the old condition in an isolated test process produces the expected
two failures for the 49- and 89-second cases; all five pass with the correction.

These tests do not stand in for cached and uncached live reproductions.
Same-record chunk-progress and reconnection policy are separate mechanisms,
not changed by this correction. Original workspace data and run evidence
remain untouched. Live cache-deletion testing awaits the user's choice of
test-workspace scope so shared production caches are not removed.

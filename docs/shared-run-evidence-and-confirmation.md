# Shared private evidence and condition-based confirmation

## Scope

This change does not alter UI design, ETA calculations, segmentation, duplicate
protection, native upload payloads, upload retry authority, or output retention.
Existing private runs are neither rewritten nor pruned. Compression is deferred.

## Confirmation audit

The 2026-10-02 20:23 run's final record had only observed start events. Its
SQLite vector mappings were timestamped about six seconds after submission;
exact physical verification occurred about 90 seconds after submission.
The absence of completion events is established, but their absence's cause is
not. The existing read gate treated fresh start telemetry as an active writer
and withheld further vector reads until the quiet boundary. The same gate was
used by both per-document and shared-batch confirmation.

Both paths now consult a read-only `SubmissionCommitSignal`. It queries exact
selected document paths in the target workspace, requiring a vector mapping
for every selected document. Unchanged database/WAL fingerprints avoid repeated
queries. Locked or unavailable SQLite is uncertainty and is retried, never
submission authority. A positive mapping signal schedules the existing exact
physical vector check; it cannot itself pass a run or reset liveness clocks.
The hint never opens LanceDB. Existing finite stall/deadline rules remain.

The recovery activity probe formerly added stream-establishment time to the
observation budget and continued waiting after positive foreign activity.
It now shares one monotonic observation budget and wakes on a foreign event.
That event only establishes a conservative hold; silence remains uncertain.
Stopping/joining the listener remains separately bounded.

Other inspected waits serve distinct purposes and are retained: transient
Windows sharing-violation backoff, explicit HTTP 429 refusal recovery, SSE
reconnection without a server heartbeat, runtime-readiness probing, and bounded
ambiguous attachment recovery. Resolved observer errors and deliberately
abandoned late HTTP responses remain diagnostic evidence, not automatic reasons
to resubmit. This is not a claim that every timer has been audited or removed.

## Private JSON contract

Private run JSON writers use compact serialization. Outside configured
`run-state`, existing formatting remains unchanged. Export publishers and TXT
contents are untouched.

Allowlisted summaries, source profiles, checkpoints, final results and worker
config/results share large immutable control snapshots via `.run-evidence`
inside their run. The on-disk reference is:

```json
{"$run_evidence":1,"sha256":"<64 lowercase hexadecimal characters>"}
```

Snapshot JSON uses canonical keys and compact encoding. Identical bytes share
one file; changed observations have different identities. Snapshot publication
precedes the referencing control record. Concurrent workers cannot expose
partially written snapshots. Readers verify hashes, reference versions and
containment, reject missing/corrupt references, and accept legacy plain JSON.
Prepared-batch verification checks referenced evidence before granting reuse.

Keep the complete run folder when copying it. To inspect a fully expanded
record, use `python -m run_evidence <path-to-record.json>`. Internal worker,
recovery and diagnostic readers resolve references automatically. Third-party
raw JSON consumers must use `read_run_json` when reading referenced fields.

Small fields, mutable status envelopes, JSONL journals, source text, alternate
extraction artifacts and native payloads remain self-contained. Therefore this
is deliberately not zero duplication across every artifact. Further text/file
consolidation requires proving its extraction, provenance and replay contracts.

## Qualification

Temporary re-serialization of the two retained runs checked all 376 JSON files
for value-identical round trips. Including unchanged non-JSON bytes, terminal
snapshots projected 19,139,893 -> 7,453,827 bytes and 16,360,810 -> 5,996,618 bytes
(61.06% and 63.35% reductions). Existing runs were untouched. These are terminal
re-serialization measurements, not promises about future lifecycle sizes;
changed intermediate snapshots are intentionally retained.

Three real Film II PDFs passed the actual isolated preparation child worker,
using pages 1-4 with 2,2 page groups. The publisher produced nine flat TXT files.
Original PDFs were preserved; no AnythingLLM documents or workspaces were
mutated during this qualification. This does not qualify live in-flight
confirmation latency. The commit hint was additionally exercised read-only
against both original runs' current workspace mappings.

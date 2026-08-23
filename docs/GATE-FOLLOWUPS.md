# Deferred Gate Follow-ups

These items are deliberately non-blocking placeholders. They record controls
that protect data integrity today but depend on AnythingLLM versions, provider
behaviour, or OCR heuristics. Do not change a run's outcome solely because an
item is listed here.

| ID | Current control | Deferred improvement | Current recommendation |
| --- | --- | --- | --- |
| GATE-FOLLOWUP-001 | SQLite compatibility profiles fail closed for native mutation capability checks. | The read-only Swagger capability report now captures documented core-route evidence for a loopback Desktop API. Add the separately opt-in create/upload/delete contract probe before qualifying any new mutation profile. | Keep fail-closed for SQLite writes; a documented route never grants write authority by itself. |
| GATE-FOLLOWUP-002 | Provider/model name catalogues classify embedding settings. | Replace static name heuristics with a cached live embedder capability probe where available. | Keep catalogues advisory; do not make a provider name alone block a run. |
| GATE-FOLLOWUP-003 | A live embedder probe is required before native raw-text upload. | Distinguish a transient probe failure from a confirmed incompatible embedder and provide bounded retry evidence. | Keep the probe requirement for raw-text upload. |
| GATE-FOLLOWUP-004 | OCR readiness withholds unreliable scanned/spread documents. | Add an explicit, per-document operator override that preserves the review evidence and never applies silently. | Keep automatic withholding. |
| GATE-FOLLOWUP-005 | Exact-vector reconciliation withholds later submissions after an unconfirmed batch. | Add a durable pending/recovery state that can continue only after provenance-matched confirmation, not elapsed time alone. | Keep no-blind-retry and visible failure behavior. |
| GATE-FOLLOWUP-006 | Temporary-workspace names must be LanceDB-safe. | Revalidate the namespace rule against a new AnythingLLM release before widening it. | Keep the current protective check. |
| GATE-FOLLOWUP-007 | Desktop refresh bridge requires a matching draft-safety protocol version. | Add an upgrade compatibility test when the bridge protocol changes. | Keep fail-closed. |
| GATE-FOLLOWUP-008 | Automatic native batches defer lean/flat retention until shared submission has consumed every PDF plan. | Add a batch-final compaction pass that preserves the complete recovery ledger and every required upload artifact. | Retain full evidence for now; never compact before shared submission. |
| GATE-FOLLOWUP-009 | Desktop progress SSE and queue-cleanup paths are not part of the current public OpenAPI contract. | Reconfirm their behavior against each new Desktop release, or replace recovery cleanup with a documented API when upstream provides one. | Treat SSE strictly as advisory UI evidence; permit queue cleanup only as recovery-only, after positive app-owned provenance and activity evidence. |

# Integration cleanup and evidence scope

## Review boundaries

This cleanup separates inactive integration machinery from supported ingestion.
It does not change PDF extraction, OCR, metadata recognition, provider batching,
vector commit semantics, ETA, output retention, model selection or UI layout.
Document foldering is explicitly out of scope.

Historical investigation preceded the changes. Earlier direct SQLite copying
of validation-workspace settings had already been removed. Concurrent API
mutation waves had lost records despite successful HTTP acknowledgements and
were subsequently hard-capped at one. These histories explain why superficially
similar code must not be indiscriminately revived or removed.

## Retained and removed paths

- Validation workspaces still use the API with explicit `chatMode` and `topN`.
  Model inspection and validation remain. The unused template reader and its
  misleading report fields are removed, not replaced with settings copying.
- Hashing, atomic writes, batch caps and Desktop activation checks now live in
  `anythingllm_source_atomic_common`. The worker module name remains as a
  helper-only import facade; obsolete worker rendering/installation is removed.
- The qualified v1.16.1 policy is isolated in
  `anythingllm_source_atomic_v1161_policy`. Its 35s/45s deadlines remain because
  they are active for that supported version. Deleting them would be a behavior
  change, not dead-code removal. v1.17 selects its native SDK policy directly
  from an unrendered staging template rather than replacing a rendered legacy
  helper. The generated v1.16.1 and v1.17 backend bytes are unchanged.
- Concurrent mutation-wave scheduling is removed. Compatibility argument slots
  still accept old callers but always dispatch to the existing serial scheduler.
  Serial recovery, cancellation, acknowledgement and exact-vector observation
  remain. Historical ledger fields are still readable; old accepted siblings
  must not become eligible for blind replay merely because waves are retired.

## Optional live retrieval

The existing button remains manual. Chat generation remains off by default.
The diagnostic reads a successful assistant-owned receipt for the selected
workspace, preferring the completed receipt's actual target over old intent.
It bounds file reads while distributing them across the full receipt and
rejects paths outside the documents root.

Sampling distinguishes `(source identity, physical page)`, not just a page
number shared by unrelated PDFs. Multiple fragments of one page still share
one probe. Dotted table-of-contents leaders cannot inflate punctuation rewards
and dominate the entire sample. Query windows are still verbatim normalized
windows from existing payloads; source text is never rewritten.

There are at most three vector queries, one client attempt each, with a 20s
per-query timeout and no automatic sibling retry. Existing query-vector
generation and API authentication are retained; this is not a new document
ingestion or embedding run. Temporary keys are cleaned up by the existing
validator. Query evidence is persisted separately from the upload summary.
Unexpected diagnostic failures do not change the upload verdict. A sampled
source absent from top results is not proof of a missing stored embedding.

The live production callback was exercised against the installed v1.17 runtime.
The original scoring selected one contents-leader passage and missed it. With
bounded punctuation scoring, three prose probes were issued: all returned
HTTP 200, one returned its exact source at rank one, two did not return their
expected identity in the top ten. That unresolved retrieval result is retained,
not made green by expanding results or tuning queries to known answers.
Temporary diagnostic keys were deleted successfully. No documents, workspaces,
stored vectors or chat messages were created or deleted by these checks.

## Characterization

Characterization now reports independent facts: installed identity, readable
storage schema, API-documentation provenance, current documentation-endpoint
reachability, and prior exact-package qualification. Packaged OpenAPI fallback
cannot erase the fact that the live documentation request failed. Neither
an HTTP rejection nor a body-read failure is confused with failure to contact
the runtime. The HTTP status remains available for diagnosis. Neither
matching documentation nor historical qualification is described as a fresh
embedding or retrieval test. Assessment is a pure summary of existing evidence,
with a next diagnostic action; it adds no calls, retries or mutation authority.

## Desktop refresh bridge assessment

These scores are engineering judgments from code inspection, not performance
measurements or a grade for AnythingLLM as a whole. The bridge remains optional,
uninstalled and unchanged by this cleanup.

| Area | Score / 10 | Reason |
| --- | --- | --- |
| Narrow usefulness | 6 | Refreshes stale workspace views, but reloads the renderer. |
| Access boundary | 8 | Loopback-only, random token, constant-time check, one fixed operation. |
| Draft safety | 5 | Fails closed on inspection failure, but checking and reload are separate awaited operations. Typing can begin between them. |
| Upgrade and rollback | 3 | Exact minified anchors are fragile; uninstall restores the newest backup without matching it to the current version or checking that it is pristine. |
| Efficiency | 6 | Bounded reload wait and clean listeners; synchronous descriptor writes every second and unbounded diagnostic append remain. No benchmark slowdown is attributed to these without measurement. |
| Maintainability | 4 | Vendor archive rewriting couples a small feature to Desktop implementation details. |

Overall: approximately 5/10 as a refresh workaround. Its strongest qualities
are limited authority and fail-closed checks; its weak point is reliable lifecycle
and renderer integration across upgrades. The practical low-effort improvement
is to keep it optional and clearly separate from ingestion, rather than making
embedding depend on it or claiming safe cross-version automatic upgrades.
Any future bridge redesign should first address backup provenance and the
check/reload race, not expand its command surface.

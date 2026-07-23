# Findings from `dastra/anythingllm-document-sync`

## Scope and conclusion

This note examines [`dastra/anythingllm-document-sync`](https://github.com/dastra/anythingllm-document-sync), a small Python utility that copies local files into one AnythingLLM workspace. It is valuable as a concrete third-party integration, but it is not a general-purpose ingestion engine and it should not be treated as proof that AnythingLLM exposes a durable background-job API. Its most useful lesson is narrower: submit staged documents one at a time, retain an external record of document identities, and determine the next action from observable workspace state rather than from a guessed internal lifecycle.

That lesson directly applies to this project. Our last test sent four simultaneous `update-embeddings` calls, each carrying two page-parent records. AnythingLLM expanded them into many chunks and continued embedding after our client-side deadline. The sync project avoids that pressure by serialising calls. It does **not** poll a job queue, inspect LanceDB, or recover a timeout; we must add those capabilities ourselves.

## What the project does

The project is a local-folder synchroniser. Its README describes a six-stage run: scan configured folders, upload new or changed files, embed staged-but-unembedded documents into one workspace, unembed removed files, remove their staged copies, and retain a local SQLite mapping. It targets AnythingLLM Desktop or server at `http://localhost:3001` and uses a normal Developer API key. See the [README](https://github.com/dastra/anythingllm-document-sync/blob/main/README.md).

Its important distinction is between three states:

1. A file exists on the user’s disk.
2. AnythingLLM has accepted it into the document processor and returned a staged `location`, such as `custom-documents/name-uuid.json`.
3. That staged location appears in the workspace’s document list after embedding.

The local SQLite row stores the absolute path, source modification time, AnythingLLM staged location, and a JSON copy of the upload response. It stores neither the embedding result nor a state such as `embedding_pending`, so it is a durable *identity map*, not an ingestion ledger. See [`database.py`](https://github.com/dastra/anythingllm-document-sync/blob/main/anythingllm_loader/database.py). On each run it compares source modification time with the saved mapping: changed files are staged again and unchanged files are skipped.

The upload call is conventional: `POST /api/v1/document/upload` with multipart file data. The tool only persists the mapping after a 200 response whose JSON has `success: true`; it takes the staging path from `documents[0].location`. The project comments show the expected payload fields, including `location`, `pageContent`, metadata, and the `cached` flag exposed by AnythingLLM’s document listing. See [`anythingllm_api.py`](https://github.com/dastra/anythingllm-document-sync/blob/main/anythingllm_loader/anythingllm_api.py#L56-L141).

## How it embeds

For embedding, it first requests `GET /api/v1/workspace/{slug}` and extracts the existing workspace document paths. It compares those paths with the local SQLite mapping. Every staged location absent from the workspace list is passed to `POST /api/v1/workspace/{slug}/update-embeddings` in an `adds` array containing **one** location. The author’s comment is unusually direct: “embedding one at a time as larger batches seem to max out CPU.” See [`ingest_anythingllm_docs.py`](https://github.com/dastra/anythingllm-document-sync/blob/main/ingest_anythingllm_docs.py#L97-L107).

The API wrapper itself uses a 60-second HTTP timeout and interprets HTTP 200 as success. After a successful call it sleeps for half a second “so [as] not to overload anythingllm.” It does not parse the returned workspace object for the exact submitted location, wait for a vector count, perform a search, retry a timeout, or poll until a document enters the workspace list. See [`embed_new_document`](https://github.com/dastra/anythingllm-document-sync/blob/main/anythingllm_loader/anythingllm_api.py#L184-L236).

That means its effective synchronization loop is coarse but sensible:

```text
local file → staged location in local SQLite
          → one update-embeddings POST
          → later GET workspace documents
          → embed only locations not yet listed
```

The most important part is that the *next run* re-derives the unembedded set from AnythingLLM’s workspace document list. It does not need an internal queue endpoint to do that. If AnythingLLM completed work after the client disconnected, a later workspace listing may show the document and prevent a duplicate POST. If the document is absent, the next run tries it again. That is a form of eventual reconciliation, although it is not immediate and does not distinguish a queued document from a genuinely failed one.

## What it teaches us

### 1. Submission, staging, workspace membership, and retrieval are different facts

The project is built around staged `location` values, not filenames. That is correct. A source filename is neither globally unique nor the value the `update-embeddings` endpoint expects. The endpoint is given the processor-generated JSON location. The [AnythingLLM API collection](https://www.postman.com/tcarambat/mintplex-labs/request/7gb661e/embed-document-into-workspace) likewise shows `adds` containing staged `custom-documents/...json` paths.

For our app, this validates keeping an explicit per-page-parent identity and a durable ledger. We should retain that part of the existing design. But we must make the states clearer:

* **staged**: the document processor wrote the expected `custom-documents` record;
* **submitted**: the POST was sent, but its outcome can be unknown;
* **attached**: the workspace API shows the exact staged location;
* **vector-observed**: exact vector/LanceDB evidence exists;
* **retrieval-observed**: a live search can return a matching stored source.

No one of those facts substitutes for another. In particular, a 200 response is not a retrieval-quality result, but a timeout is not evidence of non-embedding.

### 2. One-at-a-time is an evidence-based baseline, not a philosophical ban on concurrency

The sync project uses serial submission because its author observed larger batches consuming too much CPU. Our local AnythingLLM log corroborates the underlying mechanism for this installation: each page parent is recursively split into many chunks, then those chunks are embedded through OpenRouter and inserted into LanceDB. Four concurrent requests multiplied that work before any one response could complete.

This does **not** establish that AnythingLLM cannot queue or process asynchronous work. It establishes that our old client had no evidence about what Desktop was doing after the HTTP deadline and created too much in-flight work to reason about it. The correct default is a single active embedding request during verification. Later, after measured success, we can test controlled concurrency of two. We should not begin at four merely because it is faster in theory.

### 3. Reconciliation must happen during the same run

The sync project only gains eventual reconciliation because it is meant to be rerun. That is acceptable for a folder-sync utility but inadequate for an interactive PDF-processing application. A user who clicks “start” needs to see that AnythingLLM is still embedding, that the client response is late, and that the app is waiting and checking rather than declaring failure.

Our app should use the same observable workspace-document comparison immediately after a late response, then enrich it with the exact vector evidence we already implemented. A timeout should enter a visible **Waiting for AnythingLLM** state. For a bounded period it should poll the workspace membership, matching vector rows, and Desktop liveness. It should only become “needs attention” when those observations remain absent after the wait budget. If evidence arrives, the original run must complete normally; recovery must not be a hidden side route that leaves `run-progress.json` failed.

### 4. The sync project is deliberately narrow and has real blind spots

It has no retries, backoff, idempotency key, cancellation protocol, queue inspection, vector audit, or retrieval validation. Its 60-second deadline could produce the same unknown-outcome problem for a sufficiently large document. It also records upload state before proving embedding state and relies on a future workspace listing rather than a dedicated `pending` record. These are reasonable omissions for a compact personal synchroniser, but make it unsuitable for direct adoption as our ingestion service.

## Implications for this application

The appropriate revised architecture is an external, observable state machine, not a guess at AnythingLLM’s undocumented internal queue:

```text
prepared → staged → submitted → waiting-for-observation
         → attached → vector-observed → retrieval-validated → complete
                         └→ unresolved → recoverable / needs attention
```

Each transition should carry the exact staged location, source hash, workspace slug, timestamps, response classification, and observed evidence. A timeout moves a record to `waiting-for-observation`; it does not make the whole run failed. The UI should display the active waiting timer, the last observation, and the number of page parents still unresolved. It should not promise that an internal queue exists, but it can truthfully say that Desktop has continued processing after the client response deadline when local evidence shows it.

For **verification runs only**, each PDF should use a separate workspace so retrieval checks have an unambiguous corpus. For the normal product flow, retain the current shared-workspace behavior by default and add an explicit “create a workspace per PDF” option. Shared workspaces are a valid product choice; they just cannot support a strict per-document top-1 retrieval assertion without a document-aware test query or filtering strategy.

Finally, the retrieval gate must be revised. AnythingLLM chunks and embeds its generated document text, which can include metadata headers; querying only a raw source excerpt and demanding it rank first is not a reliable completion condition. An AnythingLLM API discussion documents this metadata-sensitive behavior. See [issue #2937](https://github.com/Mintplex-Labs/anything-llm/issues/2937). The stronger test is: confirm every expected page-parent identity is attached and vectorized, then use live search to confirm a matching source occurs in the requested results for a query derived from the actual stored representation. Ranking quality can be recorded separately from ingestion completeness.

## Bottom line

Automatic AnythingLLM embedding is feasible. The sync project shows a stable, repeatable staged-document workflow and provides a useful serial baseline. It does not prove that AnythingLLM provides a reliable background job queue, and it does not solve late HTTP responses. Our next implementation should preserve our superior evidence and recovery artifacts, replace speculative fan-out with measured serial submission, poll observable Desktop state after late responses, and make recovery update the original run and UI all the way to a verified terminal result.

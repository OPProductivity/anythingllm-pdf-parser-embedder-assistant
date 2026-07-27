# AnythingLLM integration notes

## API boundary

The assistant communicates with a running local AnythingLLM Desktop backend
through its local HTTP workspace and document endpoints. It prepares text
records before submission so that filename and metadata conventions can retain
page provenance.

AnythingLLM ultimately controls its own embedding queue, vector storage, and
global text-splitting preferences. An uploaded record can therefore be split by
AnythingLLM according to the active settings. The assistant reports expected
boundaries before processing and validates separately what storage, vector, and
retrieval evidence it can observe afterwards.

## Verification terminology

- **Prepared locally:** text and segments were written successfully.
- **Stored:** the workspace received the expected records.
- **Vector observation:** corresponding vector evidence was observed.
- **Retrieval verified:** a runtime search returned matching content.

Do not treat a single status, including the Documents drawer, as proof of all
four stages.

## Current queue and recovery behavior

The upload path submits one app-owned Desktop FIFO queue request for planned
managed relative locations. The assistant observes only queue events that
match those locations. It records the SSE observer health and uses exact
expected page-parent identities to confirm storage; Desktop queue and vector
observer percentages are not added because those activities overlap.

If a request outcome is ambiguous, reconciliation uses one bounded shared
observation window with read-only checks. A later document-level observer uses
the remaining portion of that window rather than starting a second full
timeout. Queue cleanup or restart is allowed only for ledger-proven app-owned
work; ambiguous ownership or manual activity is preserved rather than changed
automatically.

## Page-parent queue timing

The current Desktop route processes page-parent records serially inside one
accepted workspace queue. The assistant therefore prices the initial native
upload ETA by estimated provider requests per page parent, not by the single
outer HTTP receipt. It still uses owned queue evidence to reprice a live run.
Client-side queue concurrency and automatic retries remain disabled because
they can collide with Desktop's global progress state and create ambiguous
ownership.

The safe performance target is an upstream Desktop batching change: retain one
page-parent document identity and progress record for every vector, batch only
the provider embedding inputs to that provider's supported limit, then map the
returned vectors back to those same identities. It must be delivered and tested
against Desktop source, not injected into a packaged runtime; it must also
preserve ordered per-parent queue evidence. Client concurrency and retry
changes are not substitutes for that design.

## Desktop refresh bridge

The bridge is optional. It patches an installed AnythingLLM Desktop archive only
when required file anchors are present, creates a backup, and offers validation
and uninstall commands. Because it touches an installed application, close
AnythingLLM Desktop before changing it and retain the backup until you have
confirmed normal behavior.

```powershell
anythingllm-pdf-assistant bridge validate
anythingllm-pdf-assistant bridge install
anythingllm-pdf-assistant bridge upgrade
anythingllm-pdf-assistant bridge uninstall
```

## Troubleshooting

- Run `anythingllm-pdf-assistant doctor` before reporting a startup problem.
- Confirm that AnythingLLM's embedding provider works independently before
  starting an upload run.
- Read the generated run report for the stage that actually failed.
- Never post provider keys, desktop storage folders, or private document text
  in an issue.

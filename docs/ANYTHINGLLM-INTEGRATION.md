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

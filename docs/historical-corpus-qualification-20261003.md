# Historical-corpus qualification

## Scope and isolation

The retained private run-state inventory contains 35 runs, 35 PDF paths and 34
distinct PDF contents, totaling 646 pages. All input paths remain available and
all distinct PDFs have retained worker configurations. The corpus includes real
articles, a 255-page book, excerpt PDFs and earlier synthetic clear-button fixtures;
34 is not a count of 34 independent scholarly articles.

No research workspace, embedding, cache or document was deleted. The user's live
AnythingLLM Desktop backend stayed under PID 9676 on port 3001. The assistant was
not restarted. Test preparation workers were lowered to BelowNormal priority.

The live qualification uses the installed native AnythingLLM backend and collector
with independent ports 43131/43132, separate APPDATA, STORAGE_DIR and DATABASE_URL,
and initially empty document, vector and cache storage. Only database schema and
the two chunk-splitter settings are copied from the research database, through a
read-only connection. OpenRouter embedding configuration is supplied to the owned
child processes without printing or storing the provider credential. No chat is
run and no Alibaba token-plan API is used.

Each test process is explicitly owned and cleanup targets only the isolated API.
The initial empty-storage collector pilot exposed its requirement that `hotdir`
exist; the test harness now creates the normal empty storage directory structure.
No installed application code was changed to accommodate testing.

## Fresh preparation

All 34 PDFs completed through the actual cancellable production preparation
worker. Historical document settings and preflight evidence were reused, but OCR
checkpoint directories were fresh, and upload/vector evaluation was disabled for
this phase. Source hashes remained unchanged. All private JSON evidence decoded,
all canonical roles resolved and OCR checkpoint reuse was zero.

Thirty-three extracted text bodies match their retained historical manifests
exactly. The remaining book keeps its formerly excluded page-1 OCR cover and the
old body after it. Its scope changes from page 2 to page 1. The expected metadata
repairs affect Gans, Franks and O'Brien; no other metadata/scope differences were
found by the comparison script.

There are 310 selected manifest records before and after replay. This is not
necessarily the number of uploaded records: page-parent representation can change
that number. The settings span whole-file, page-passage, page-limit and custom-range
processing, segment/page-parent upload representations and both back-matter choices.
The prior five-document matrix separately covers transport/metadata alternatives.

This comparison uses original retained outputs as the historical baseline. It is
not a fresh execution of every historical Git revision under the new backend and
does not establish universal compatibility with PDFs absent from this corpus.

Receipts are in the ignored `tmp-output/historical-replay-20261003` directory:
`inventory.json`, `results.json`, `comparison.json`, fresh worker logs and manifests.

## Live confirmation

The one-document pilot completed with zero cache reuse. API cleanup removed its
workspace, document links, document-vector mappings and vector-cache files; no
LanceDB workspace namespace remains. Pilot processes were stopped. The full
34-document and mixed-nine-document receipts are audited separately by
`experiments/audit_isolated_live_replay_20261003.py`.

The full replay's 103-record source confirmed all 103 selected records after
134.5462 seconds. Recent document progress kept the run live; elapsed time exceeding
90 seconds did not itself produce a premature failure. This elapsed time includes
active native embedding, not 134 seconds of idle post-completion waiting.

The first full live pass proved 310 record-level vector confirmations but omitted
the source identity normally added by the GUI coordinator after worker return.
The first mixed pass consequently collapsed source-local result keys. These are
test-harness limitations, not evidence of a production regression. Their receipts
are preserved and marked `source_identity_complete=false`; they are not used as
source-attribution qualification. The corrected harness supplies the coordinator's
`pdf` field and asserts every source-local result and exact confirmation count.

The corrected full replay (`isolated-full-878099`) passed all 34 sources: 310
selected records, 310 exact vector-confirmed records, 483 physical vectors, zero
cache reuse and no reported errors. Its complete journals retain 1,032 queue-control
events. The evidence audit checks hydrated reports, per-source confirmation,
canonical references and journal lengths rather than relying on completion labels.

The corrected shared-workspace replay of the latest nine corpus PDFs passed all
nine source-local results, with nine selected records, 134 physical vectors, zero
cache reuse and 36 queue-control events retained in the complete event journal.

A read-only comparison with the original nine-source research workspace found
identical physical vector counts for all eight articles. The corrected book has
76 instead of 75 vectors, consistent with retaining its formerly excluded cover;
the combined count changes from 133 to 134. Existing research data was not edited.

Every isolated attempt, including rejected harness attempts and pilots, finished
cleanup with zero workspace rows, document links, document-vector mappings,
vector-cache files and LanceDB namespaces. Disposable API capabilities were also
removed after their owned processes stopped. Original PDFs and historical run
artifacts remain intact. Test receipts are retained locally, not provider secrets.

This qualification adds only experiment scripts and this record. It does not
change production OCR, the UI, ETA or public-output cleanup. No production service
restart is necessary for these test-only additions.

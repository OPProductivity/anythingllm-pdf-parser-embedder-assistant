# AnythingLLM Desktop v1.16 compatibility plan

## Purpose and current evidence

This plan makes the PDF Parser Embedder Assistant explicitly compatible with
AnythingLLM Desktop v1.16 without turning a single observed release into a
promise for every later Desktop build.

On 2026-08-22, the assistant completed a live, isolated v1.16 run using a
three-page real PDF with its normal page-preserving settings.  The run created
an isolated workspace, prepared three page records, linked all three records,
and confirmed three linked vectors.  This proves one end-to-end contract, not
all options or future releases.

The refresh bridge is qualified separately with exact package anchors. It was
not installed into the running v1.16.1 package during the contract
qualification, so upload authority remains independent from renderer-refresh
customization. The Desktop in-app updater remains outside this plan: its
packaged updater configuration must not be treated as a supported update route.

### v1.16.1 qualification (2026-08-28)

The official Windows Desktop v1.16.1 package is now a separate immutable
profile, not an alias for v1.16.0. Qualification recorded:

- exact `app.asar` SHA-256
  `4f00651eb1a421a3a37fb60dc9486e0dc5577d21efac96dcf4b05ad2887ea910`;
- the required SQLite columns and all four documented core API routes;
- temporary API-key creation and revocation;
- isolated workspace creation, raw-text metadata upload, document linking,
  exact vector confirmation, runtime retrieval, workspace deletion, and exact
  probe-document deletion;
- no surviving validation workspace or raw probe document after cleanup; and
- exact one-occurrence refresh-bridge anchors for v1.16.1, validated without
  modifying the installed package.

The v1.16.1 Swagger initializer can briefly be empty while Desktop starts.
Read-only route discovery may therefore fall back to the bounded OpenAPI file
inside the same fingerprinted package. That documentation fallback never
grants mutation authority by itself.

## Principles

1. **Capabilities, not a version string, authorize a write.**  A version is an
   input to a profile; every mutating operation needs independently recorded
   evidence.
2. **Keep the established run architecture.**  Preparation, duplicate
   inspection, native submission, observer confirmation, and recovery evidence
   remain distinct phases.  Do not replace them with a new polling loop.
3. **Fail closed before a write and fail informative after one.**  Before a
   native submission, block if its contract is unproven.  Once submission
   begins, preserve the recovery plan and report the precise unknown state.
4. **Protect user data.**  Compatibility work must use an explicitly named
   isolated workspace, never a user workspace such as W3C.  It must not reset
   the Desktop data directory.
5. **Profiles are immutable evidence records.**  A later build must create a
   new profile or a reviewed extension of a profile; it must never inherit
   v1.16 merely because a SQLite schema looks similar.

## Implementation phases

### 1. Split discovery from qualification

Replace the current all-or-nothing `profile_match` result with four separately
reported facts:

| Fact | Evidence | Purpose |
| --- | --- | --- |
| Installed Desktop identity | executable path, product version, app package version and package fingerprint | Selects a candidate profile |
| Storage identity | database schema, documents root and Lance layout | Enables read-only storage inspection |
| Runtime identity | loopback API reachability, authenticated temporary key lifecycle, backend build metadata when available | Enables live submission probes |
| Bridge identity | bridge revision, descriptor format, exact source-anchor profile and health endpoint | Enables renderer refresh only |

The assistant should expose these facts in the compatibility panel and in the
per-run preflight file.  An unqualified Desktop can still be inspected
read-only; only native write capabilities remain blocked.

**Implemented first increment:** the read-only characterization now separates
storage-schema status from guarded-settings qualification.  The exact observed
v1.16 package is an `observed_candidate` only when the optional `app.asar`
fingerprint matches; ordinary fast inspection says that v1.16 requires that
fingerprint.  Direct SQLite copying of a newly-created workspace's runtime
settings has also been retired rather than carried into v1.16.

**Implemented second increment:** `anythingllm_pdf_assistant_cli compatibility
inspect --api-url http://127.0.0.1:3001` reads only the local Swagger
initializer and reports the documented raw-text, upload, workspace-list, and
update-embeddings routes. It is loopback-only, sends no credential, creates no
Desktop state, and explicitly records that API documentation evidence does not
grant write authority. Progress SSE and queue-cleanup paths are reported as
advisory/undocumented rather than silently being treated as part of the core
contract.

### 2. Define a v1.16 capability contract

Create a separate explicit profile record for every qualified patch release
(currently v1.16.0 and v1.16.1).
It should store only non-secret contract facts:

- exact Desktop version and a hash/fingerprint of the relevant package entry;
- existing storage tables and the columns used by the assistant;
- the temporary API-key creation and revocation route shape;
- workspace create/read selection behavior;
- native document submission response shape;
- document-link and vector-confirmation queries;
- duplicate-identity lookups;
- expected background-queue observation semantics and timeout classes.

Do not put API credentials, user paths, workspace names, document content, or
full database rows in the profile.

### 3. Add an isolated, opt-in contract probe command

Add a CLI command such as `anythingllm_pdf_assistant_cli compatibility-probe`.
It must be opt-in because it creates and removes only a dedicated probe
workspace.  Its steps are:

1. record a read-only baseline and verify the Desktop runtime is reachable;
2. create a short-lived temporary API key and verify revocation in a `finally`
   path;
3. create one uniquely named probe workspace;
4. submit a tiny generated *text payload* through the same native metadata
   route used by real runs (not a synthetic PDF);
5. wait for both a workspace-document link and its vector confirmation;
6. query the record through the read-only observer; and
7. remove only the probe workspace and its probe record, then write a redacted
   evidence report.

This probes the AnythingLLM native contract without consuming or touching the
user's documents.  If cleanup cannot be confirmed, it must stop and show the
precise leftover workspace identifier rather than guessing.

### 4. Qualify the normal run variants

After the small contract probe, run a controlled matrix against a disposable
workspace using real PDFs already authorized for local testing.  Record each
variant separately:

- one PDF, default page-preserving mode;
- one PDF, all-in-one mode;
- multiple PDFs, default per-file metadata;
- repeated same-PDF submission (fully indexed skip);
- partial prior import (only missing records submit);
- an existing workspace containing unrelated documents;
- safe custom-range/front-back exclusions where those controls are enabled;
- cancellation during preparation and cancellation after submission begins.

For each, require the same invariant: planned records, linked records, and
confirmed vectors must be reconciled at completion.  A timeout is a needs-
attention result, not a success and not an automatic retry.

### 5. Integrate v1.16 only after evidence passes

Once a v1.16 profile has the required evidence:

1. add it to the supported Desktop profile registry;
2. map its read-only and write capabilities independently;
3. retain a compatibility panel showing profile id, evidence timestamp,
   package fingerprint, and the last probe result;
4. gate native upload, temporary API-key creation, and destructive maintenance
   independently, rather than making every capability depend on SQLite schema;
5. keep the current conservative behavior for unknown versions.

### 6. Strengthen submission and confirmation observability

The completed live run had a long 99% interval while Desktop completed the
background queue.  Preserve the current phase-based ETA model, but add
version-neutral evidence to the existing timing timeline:

- submission accepted timestamp and response identifier;
- first linked workspace-document timestamp;
- first vector-confirmation timestamp;
- final planned/linked/confirmed reconciliation;
- queue-observer state (`no signal`, `pending`, `active`, `complete`,
  `stale`) and its source.

The UI should say `Waiting for AnythingLLM to confirm X of Y records` while
there is a live signal; when there is no signal, it should explain the bounded
verification wait rather than displaying a fabricated queue ETA.  This is a
presentation/observability addition to the existing phases, not a replacement
for them.

### 7. Make the refresh bridge separately versioned

Keep bridge qualification separate from upload qualification.

- Each Desktop build requires exact one-occurrence anchor checks and a package
  fingerprint before patching.
- Back up `app.asar` before an installation or upgrade and validate the packed
  result afterward.
- Maintain draft protection and use a local authenticated descriptor.
- Do not infer that a bridge-compatible build is upload-compatible, or vice
  versa.
- Reject unknown builds without changing their package.

### 8. Define the supported Desktop upgrade path

The current v1.16 test showed normal startup/restart and the PDF contract can
work.  The Desktop in-app update download flow remains unsupported because its
packaged updater feed is not a reliable vendor route.  The supported procedure
is therefore:

1. archive (do not delete) the program bundle and updater cache;
2. retain the complete Desktop data directory in place;
3. install the official signed Desktop installer manually;
4. validate launch, local runtime ports, workspace count, and a named
   important workspace before bridge installation;
5. run `Install-AnythingLLMDesktopRefreshBridge.ps1 -Validate`;
6. run the non-mutating compatibility discovery; and
7. run the opt-in contract probe before enabling a new profile.

## Acceptance criteria

v1.16 becomes fully supported only when all of the following are true:

- profile discovery identifies v1.16 without treating any other version as
  equivalent;
- the isolated contract probe succeeds and leaves no unaccounted probe data;
- the normal-run matrix above passes, including duplicate and partial-import
  reconciliation;
- run output always reports planned, linked, confirmed, skipped, and failed
  counts without conflating them;
- unknown versions remain read-only/blocked for writes;
- bridge install/validation either passes exact checks or makes no package
  change;
- no credentials, private document content, workspace names, or personal paths
  are written into public source or compatibility profiles.

## Rollout order

1. Implement discovery/qualification separation and the profile data model.
2. Implement the opt-in contract probe plus redacted evidence writer.
3. Run the probe and codify the v1.16 profile.
4. Add the queue-confirmation observability fields and wording.
5. Execute the qualification matrix and correct any discovered contract gaps.
6. Promote v1.16 to fully supported only after the acceptance criteria pass.

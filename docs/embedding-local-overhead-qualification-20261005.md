# Local embedding overhead qualification

Three measured costs are removed without changing PDF text, segmentation,
embedding inputs, vector verification, or the provider batching policy.

## Changes

- `authenticated_http.py` defers urllib HTTPS handler initialization until an
  HTTPS request. Plain loopback HTTP no longer loads the Windows trust store.
  An opener initializes TLS once, retains standard certificate and hostname
  validation, and can retry after an initialization failure. Proxy exclusion,
  redirect rejection, response budgets and Desktop socket ownership remain.
- `anythingllm_compatibility.py` reads ProductVersion directly through the
  Windows version-resource API. It reads fresh data on every call, validates
  translation table boundaries, tries available translations, and retains the
  previous bounded PowerShell fallback for unavailable or unusual resources.
- `anythingllm_source_atomic_v117.py` derives the native collector payload
  signer once per current key/salt pair across CollectorApi instances. Changing
  either value invalidates it; native initialization still supplies missing
  credentials. The cache is populated after that initialization. Signing,
  encryption, request bodies and collector verification are unchanged.

The backend patch revision is `server_v117_2`. It still requires the qualified
pristine v1.17.0 backend and package. Installation also accepts the exact prior
generated patch hash, with a verified pristine backup, so existing installations
can upgrade. Unknown edits remain refused. The pristine backup is preserved and
a Desktop restart is still required before activation can be reported. v1.16
generation is unchanged.

## Experiments and final review

All live experiments used copies of the code and isolated Desktop backend,
collector, SQLite, vector cache and LanceDB state. Original PDFs were read only.
Production worker preparation and the production upload routine were exercised;
embedding calls were real. No research Desktop process was restarted or patched.

Initial experiments included repeated runs of a two-PDF/38-record pair and a
different two-PDF/31-record pair. Component measurements showed:

| Measured component | Original | Experimental candidate |
| --- | ---: | ---: |
| 39 authenticated opener constructions | 4.468 s | 0.029 s |
| Seven version probes across fresh workers and app import | 6.596 s | 0.358 s |
| Signer derivation during 38 page attachments | 38 calls / 6.122 s | 1 call / 0.184 s |
| Multipart upload of the same 38 records | 17.574 s | 2.315 s |

Final review made additional changes before integration: preserve single TLS
initialization for a reused HTTPS opener, test failed initialization retry,
validate native version translation boundaries, and support exact prior-patch
upgrades. Signer qualification now exercises the generated candidate backend,
rather than a separately handwritten replacement. The actual previous generated
backend upgraded byte-for-byte to the reviewed candidate in isolated storage.

Fresh review runs confirmed 38/38 and 31/31 records, with multipart times of
1.585 s and 1.191 s respectively. Every stored vector matched the measured input
and response (4096 dimensions, minimum cosine 1.0), and vector IDs matched SQLite
document mappings. Cached follow-ups confirmed all records with zero embedding
calls and unchanged stored text bodies.

After the final TLS refinement, a reversed-order repeat of the 38-record pair
also passed: 1.352 s multipart upload, one signer derivation, all vectors
confirmed, and zero embedding calls on cached reuse. Stored text bodies matched
the original baseline. No production code changed after this candidate run.

Boundary checks include 57 real loopback HTTP/TLS cases (including untrusted
certificates and hostname mismatch), 56 requests through the actual native
signer class, and deterministic generated-code tests for overlapping requests,
key rotation, salt rotation, empty/missing credentials and reuse after native
initialization. Version probes matched the installed ProductVersion; simulated
resource tests cover malformed translations and language fallback.

Two initial harness failures are retained in the local experiment evidence:
the first isolated backend exceeded its 40-second startup allowance, and a
fault-injection test server exceeded its five-second readiness allowance during
a concurrent test/live run. Neither reached the operation being qualified.
The live harness now records startup separately with a 120-second bound; the
fault test passed unchanged on its standalone rerun. A fixture writer also
needed binary output to avoid Windows newline translation in byte comparisons.

These are component measurements, not a claim that the historical eight-PDF
16-to-19-minute regression is solved. That run used the MMT Keywords Resit corpus,
not the thesis PDFs used here; its exact eight-file set and regression commit
remain unverified. Raw logs and private corpus data are retained locally rather
than committed.

## Regression checks

Run the offline suite, Ruff, Pyright and repository pre-commit checks. The new
`tests/test_embedding_speed_boundaries.py` covers TLS initialization, fresh
version reads/fallback, generated signer invalidation and safe patch upgrades.
The existing v1.17 adapter suite continues to check batching, cache reuse,
unmarked/native routes, malformed vectors, rejection, commit ambiguity and SDK
timeout/retry ownership.

Final integrated offline gate: 2,437 passed, one skipped, 34 deselected, and
25 subtests passed. The three warnings are existing dependency deprecations.
Ruff and Pyright passed. The secret scanner's match on the public predecessor
SHA-256 is explicitly marked as a false positive, like the pristine backend
fingerprint; this is a hash of vendor code, not a credential.

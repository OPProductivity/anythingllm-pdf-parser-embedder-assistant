# Recognition effort and chapter geometry

Status: source 03 passed the completed corpus, evidence and bounded live-path
checks. See recognition-effort-qualification-20261025.md for scores, runtime
review, coverage and limits. Git/runtime release receipts distinguish publication
from the running server.

## Policy and persistence

The Automatic form offers Thorough and Extremely Thorough. Thorough is the
first-use preference. The choice is saved immediately in the assistant's private
home and is restored by a new browser session and a restarted server. Stable and
development homes have separate preference files. No PDF evidence is saved in
this preference. A confirmed job owns an immutable copy of its selected policy;
moving the slider during that job changes the next preference, not that job.

Legacy engine/CLI calls default to Extremely Thorough. The CLI exposes
`--recognition-effort thorough|extremely_thorough`. Preview, workspace identity,
confirmation, the durable worker protocol and local preparation carry the
selected value. Source profiles and run summaries record the actual policy and
the number of deliberately deferred optional retries.

Both modes inspect every primary region in their acquired plan and run all
scheduled experts. Extremely Thorough retains the full retry schedule. Thorough
can defer an optional orientation/segmentation recheck of an interior ordinary
image only after its primary reading has at most two alphabetic characters and
no useful confidence (below 10, or unavailable). A continuous-tone, low-light
profile provides the initial conservative trigger. A large image plan may use
three actually empty primary/recheck pairs in its first eight completed tasks
to enable a broader continuous-tone trigger on later images. Decisions use plan
order, not whichever parallel worker finishes first.

Covers, explicit bylines, native-text overlays, narrow text crops, sparse native
pages, initials repairs and technical retries retain the full schedule. A
three-letter or longer possible credit protects the optional recheck as well.
All primary readings and their coordinates remain available to every expert.
The audit distinguishes selected-policy completion from completion of the full
Extremely Thorough schedule and records every intentional deferral.

This is a bounded regional retry policy, not a calibrated claim that an entire
PDF has a greater-than-90-percent probability of failure. A reliable whole-PDF
failure predictor and a held-out calibration experiment remain separate work.

## Work and role geometry

The chapter specialist relates an explicit local chapter marker to a personal
name above its following title. It requires the full name on at least two
distinct later running-head pages and an independent running head matching the
following title. It follows the marker's local column so a facing previous leaf
does not supply the title or author. Whole issues are excluded. A repeated
lowercase topic use can make this specialist abstain; properly cased
self-citations do not by themselves contradict authorship.

Coordinates are normalized from the recorded OCR scale into points, then
anchored by the actual crop origin to native page geometry. A crop's local top
is not treated as a page header. Missing or invalid page/crop anchors cannot
justify this specialist's new claim. Raw OCR/native records are preserved.
Other experts retain their evidence and
may disagree. The new claim joins the existing adjudication rather than
returning the first plausible name. Priority 116 uses the existing strong
chapter-credit level; the other priority values are unchanged.

The native field mapper also uses the current line's actual word indexes when
recognizing a mixed title/By-line. Earlier code accidentally indexed the page's
first words when the line's membership began later in the word array. Separate
regressions protect a real later byline and prevent an unrelated earlier By
from splitting another title.

## Evidence acquisition and testing

The optional laboratory checkpoint callback observes deep-copied evidence
prefixes after native acquisition and completed OCR tasks. It cannot observe
future OCR readings. Production has no callback/deep-copy overhead unless one
is explicitly supplied. Laboratory acquisition always keeps Extremely Thorough
regardless of the production preference. Saved research bundles are never used
to classify a new production PDF.

The old global workspace-author cache/in-flight sharing was removed. Each new
request obtains fresh evidence. Within one preview, its just-acquired author
report can support the workspace label without a second author scan; it is not
shared with another request or the confirmed run.

Qualification compares 386 original-name presentations, 382 neutral filenames
with internal metadata retained, and 382 pure-neutral presentations: 1,150
presentations of 382 distinct PDFs. Labels stay in the outer scorer, never in
recognition workers. Extremely Thorough permits no gross loss against current
main. Thorough permits at most five gross previously-correct losses in each
filename condition, counted as both presentations and distinct PDFs. Gains do
not cancel losses. Every changed author decision needs source-level review.

The full benchmark measures metadata inference, not complete PDF extraction,
upload or embedding. Separate bounded tests cover the real UI, folder preview,
independent overrides, preference restoration and full local-only preparation.
Exact OCR input/output hashes and source/input integrity accompany the score
comparison. Wall latency is assessed at the completed full suite; CPU work,
power, system load and memory remain diagnostics, not an invented correction
formula. See `metadata-measurement-review-20261025.md` for the counter definitions.

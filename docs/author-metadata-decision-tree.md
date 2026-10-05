# Author metadata decision tree

The author field describes the selected work, not every named person in a PDF.
Keep the visible interface and OCR selection independent of this policy.

1. A user-entered author wins. The embedded PDF `Author` property is diagnostic
   only; exporters often fill it with a workstation account or publisher.
2. Inspect native text from the opening pages and the bounded closing-page
   samples. Prefer a complete, role-specific work credit (`By`, `Author`,
   `Reviewed by`, or `Edited by`) or a byline independently supported by title
   position and document structure. Preserve each accepted name in full;
   never publish a surname prefix cut by a capture limit.
3. Reject document furniture, organizations, explicit translator credits, and
   names belonging to a different work. A generic issue masthead `Editor(s)`
   credit does not establish authorship of an article inside that issue.
4. Use a filename convention only when its structure or agreement with visible
   evidence meets the existing trusted-source rules. A filename-derived title
   may itself contain author names; that fact alone cannot prove either that a
   visible byline is false or that an affiliated name is the work's author.
5. After OCR, accept author mutation only from the narrower post-extraction
   source allowlist. If the surviving evidence is weak or conflicting, leave
   the durable author empty and retain the inference source and evidence for
   inspection.

The implemented path is `infer_author_from_samples_or_filename`, followed by
`resolve_author_from_metadata_and_inference`. `TRUSTED_AUTHOR_INFERENCE_SOURCES`
and `POST_EXTRACTION_AUTHOR_TRUSTED_SOURCES` determine which inferences may
become durable metadata. Workspace-name suggestions sample only the first
three native pages and may differ from full preparation.

## Known unresolved layouts

- The Mullins review has both reviewers on page one, but its filename-derived
  title also contains their names. A general title-guard relaxation promoted
  interviewees, reviewed-book subjects, and contributors in negative controls.
- The Anzel article still mistakes a city/country dateline for an author. A
  repository cover in the Pruitt PDF now abstains rather than publishing its
  `Publication Date` field as a person. These need independent work-level role
  evidence before adding another recovery path.
- Several older inference paths cap a multi-author result at 12 people. This
  limit is separate from surname capture and remains unchanged pending a
  dedicated large-collaboration policy.

Any new role rule should be checked against its target PDF, misleading
neighboring roles, the first-three-page native corpus comparison, and the
production metadata preparation path. Matching selected fields does not prove
complete PDF output or semantic correctness for every document.

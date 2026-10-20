# Embedded creator proposals and visible responsibility — 19–20 October 2026

This revision supplies Info Author, XMP pdf:Author/dc:creator and structured bibliographic Keywords to the existing responsibility adjudicator. Raw credits are never eligible author verdicts. Visible selected-work evidence must support the proposed person and role; ordinary native/image acquisition and all scheduled experts still complete before selection. GROBID remains disabled and no reusable PDF/OCR evidence cache is introduced.

## Field-specific audit

The independent read-only document-level Info/XMP audit covered all 382 active PDFs. All 162 records with readable Author/creator values were manually compared with the existing source-native/visual work and role adjudications. Twenty-two complete first-five-page overviews were rereviewed during this revision; this is not a renewed visual review of all 382 PDFs.

| Field | Nonblank readable fields | Complete canonical selected-creator roster content | Recognizable verified identity, including partial/variant |
|---|---:|---:|---:|
| Info Author | 138 | 54 | 84 |
| XMP pdf:Author | 5 | 1 | 3 |
| XMP dc:creator | 103 | 64 | 76 |

The recognizable Info identities comprise 77 personal writers, two organizational authors and five selected collection-editor rosters. For dc:creator the corresponding counts are 69, two and five. The five pdf:Author values comprise one complete correct author, two incomplete contributor credits, and two unrelated account names. A partial identity is not a complete author roster. A collection editor remains distinguishable from an authored-book writer even when the selected file is an edited collection.

Fields overlap: 59 Info-only, 24 XMP-only and 79 both give 162 distinct author-field-bearing records; 220 have neither readable field. All five pdf:Author records are also in Info Author and dc:creator. Among the whole population, 162 have readable Author/creator values, 83 have other bibliographic fields without Author/creator, 128 contain only technical metadata, eight have no Info/XMP values, and one has unreadable XMP with no other bibliographic data. These five disjoint groups sum to 382. The actual technical-only Creator/Producer values and XMP field inventories were read: they identify tools/devices, timestamps and identifiers. Info Creator is commonly the generating application and is not equivalent to XMP dc:creator. None of these categories describes author visibility on the pages.

Seven XMP objects/packets could not be fully decoded in the independent audit. A separate eighth packet needed recovery there but is rejected by the production strict parser. Diagnostics preserve unknown values instead of treating malformed metadata as empty. Optional XMP read errors no longer abort page acquisition; Info survives. All affected sources were already correctly recognized. Three empty canonical creator labels refer to whole multi-author issues, not three files without any printed author names.

Additional document-level XMP leaf-name inspection located five Calibre author-sort fields with correct canonical identities, two Springer structured author-name fields with correct authors, and three legacy xmp:Author fields containing one correct author and two unrelated accounts. These all overlap the principal author-field groups. Four pdf:Creator properties identify software, including one unresolved scanned source. None adds an additional failing-source author-credit recovery. These additional namespaces were audited, not integrated as new runtime adapters.

## Responsibility checks

Direct single-person proposals require an actual opening standalone name, a visible matching work title and named copyright, with secondary-role blocks separated. Complete local contribution claims retain higher contextual priority. A complete three-person local roster, for example, must outrank an embedded first-author-only proposal.

The additional bibliography expert recognizes a structured ebook export, splits publisher/year from the right to preserve internal title punctuation, and requires the actual ordered creator-plus-parent-title credit on multiple opening pages. A single-page part divider has a deliberately narrow exception. Edited anthologies, et-al lists, whole issues and separately scoped forewords/afterwords/translator prefaces are not automatic parent-author inheritance. This is a scoped export grammar with explicit limitations, not a general publishing-language solution.

The visible supported families receive contextual heuristic priorities 101/102. Those are ordering values, not calibrated probabilities. Raw embedded proposals remain ineligible and untrusted. The old resolver shortcut still ignores raw Author; support arises through the expert path. Correct embedded names with insufficient work/role corroboration remain unresolved rather than being accepted to meet an opportunity count.

Eleven remaining author-field-bearing misses have local private source/code comparisons recording observed pages, evidence strength, role interpretation, the literal code limitation and a proposed manual trial. Some unsigned excerpts require primary parent-work corroboration. Rejecting a wrong embedded name does not automatically repair an older unrelated false text guess. Existing ground truth was not replaced by recognizer outputs. These are ordinary unresolved cases, not automatically deferred as exceptional.

## Qualification

All 1150 active filename-condition cases were freshly inferred on 19 October, with source hashes unchanged and AC1/saver0 recorded before and after every call. No processing exceptions, failed native/OCR attempts, incomplete acquisition or expert assessments, or corpus regressions were recorded.

| Condition | Current main | Experimental candidate | Gain |
|---|---:|---:|---:|
| Original filename | 348/386 | 351/386 (90.9%) | 3 |
| Neutral filename, metadata retained | 310/382 | 317/382 (83.0%) | 7 |
| Pure neutral, active Info/XMP removed | 292/382 | 292/382 (76.4%) | 0 |

The seven ordinary-neutral recoveries comprise one direct creator proposal and six structured Keywords parent-work credits. The 72 current-main failures become 65 in this experiment, including 11 with author fields. The two remaining correct direct-field opportunities lack sufficient responsibility corroboration under the current expert and are not promoted merely to reach the earlier conditional nine-case estimate. All 80 previously reviewed external development works were also replayed in three conditions without changed verdicts: original 65/80, ordinary neutral 66/80, pure neutral 67/80. This was development regression coverage, not a new unseen test.

Runtime **failed qualification**: 3722.4586 seconds versus 2122.1814 seconds for the retained original mains baseline, ratio **1.7541×**, above the 1.7× aggregate cap. The measured candidate would need approximately 114.75 seconds (3.1%) less total inference time to meet that retained comparison. This does not isolate the cause of the gap. A separate alternating-order 40-record current sample measured new-versus-qualified15 overhead of 4.5%, but both versions exceeded 1.7× on that sample; it is not substituted for full-population qualification. The baseline was not freshly interleaved across the full new run. OS disk caches were not flushed. Full ingestion, upload and embedding were not timed.

A subsequent controlled expert-only diagnostic also **failed**. A page-one name/title/named-copyright combination created a supported author claim even when page three explicitly labelled the person as translator. The existing role recognizer recorded the translation credit, but the new expert did not reconcile that later role before adding its author claim. This is a synthetic counterexample, not an observed corpus regression or proof that a translator can never also write a work. The missing functionality is selected-work role reconciliation for the new hypothesis, allowing independently supported dual roles rather than a universal name blacklist.

Recognition changes are therefore **not merged or deployed**. They are preserved in the isolated experiment. At the user's request, this audit/qualification part is closed and further optimization or role-rule repair is paused.

Validation before this final diagnostic: 3266 deterministic tests passed, one skipped, 34 deselected, 28 subtests; after the final metadata diagnostic-ledger addition, 785 author-selected adjacent tests passed. Sixteen focused metadata controls cover untrusted fields, selected contribution priority, catalogue formatting, commas in titles, scoped dividers, malformed XML and provider failures. Passing these controls did not erase the later failing role diagnostic. One real neutral-named three-page review completed the actual standalone preparation CLI with the correct creator in its profile and all five segment records and unchanged input bytes. An initial harness attempt hit the existing Windows path-length guard; the successful check used a shorter isolated temporary output path. No upload, embedding, browser interaction or whole-text fidelity claim is made.

The test history preserves an intermittent submission-observation failure and its deterministic fixed-fingerprint reproduction. Pending SQLite observations now recheck the bounded read-only query when the filesystem fingerprint is unchanged; complete observations retain the existing cache fast path. See the separate submission-pending-fingerprint qualification note. No live database was modified by these fixture tests.

Private PDFs, source filenames, raw field payloads, detailed traces and the expanded workbook remain local. Public counts and synthetic tests do not establish generalization to independent publisher families. The ordinary neutral and pure-neutral presentations are paired copies of the same works, not independent PDFs. Original filename recognition and broad content-only recognition remain well below the long-term goal.

Field semantics: [Adobe document Info dictionary](https://opensource.adobe.com/dc-acrobat-sdk-docs/library/pdfmark/pdfmark_Basic.html#document-info-dictionary-docinfo), [XMP Dublin Core creator](https://developer.adobe.com/xmp/docs/xmp-namespaces/dc/), and [Adobe PDF namespace](https://developer.adobe.com/xmp/docs/xmp-namespaces/pdf/). These definitions guide interpretation; the actual field values and selected-work roles determine whether a credit is useful.

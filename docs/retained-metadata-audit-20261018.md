# Retained-metadata audit and PDF-only scope — 18 October 2026

The ordinary neutral-filename condition preserves the PDF's internal metadata; the current recognition architecture does not fully exploit that metadata as authorship evidence. The production pipeline passes Info Title into work identity, constructs author evidence from page samples, and sends Info Author to a resolver that explicitly treats it as diagnostic-only. XMP creator and structured bibliographic Keywords are not supplied to these authorship experts. This audit changes no runtime recognition rule.

## PDF-only results

One historical input was HTML mislabeled as PDF. Excluding it removes one incorrect original and one incorrect neutral presentation. Frozen raw runs and the historical workbook are preserved. Active scope becomes 382 PDF records, with four additional original-name aliases; these are not 770 independent works.

| Condition | Previous main baseline, PDF only | Earlier accuracy candidate, PDF only | Qualified candidate, PDF only |
|---|---:|---:|---:|
| Original filename | 326/386 | 339/386 | 348/386 (90.2%) |
| Neutral filename, metadata retained | 289/382 | 304/382 | 310/382 (81.2%) |
| Pure neutral, active Info/XMP removed | 264/382 | 279/382 | 292/382 (76.4%) |

The ordinary neutral result improved by six cases and 1.57 percentage points relative to the earlier accuracy candidate, with no paired regressions, or by 21 cases and 5.50 points relative to the previous main baseline. The last performance qualification preserved the accuracy candidate's decisions; it was not a dedicated metadata-credit improvement. The corrected combined original/ordinary-neutral total is 658/768 (85.7%). Removing an incorrect non-PDF changes denominators and is not a recognition improvement. These are retrospective filters of completed runs, not a new inference or timing run. The prior 1152-input timing qualification remains historical; 1150 active inference records remain after filtering.

## Metadata coverage and remaining opportunity

Read-only field inspection covered every one of the 382 ordinary-neutral PDFs, using their existing manually adjudicated complete author rosters. No OCR, author inference, external lookup or document mutation was performed for this audit.

| Definition | PDFs | Currently correct in ordinary-neutral mode | Remaining misses |
|---|---:|---:|---:|
| Any Info/XMP metadata, including technical fields | 374 | 304 | 70 |
| Nonblank Info Author or XMP creator | 162 | 144 | 18 |
| Full matching author identity content, allowing catalogue inversion/date formatting | 91 | 88 | 3 |

Metadata presence is not evidence of a correct author credit: software names, account names, generic placeholders, editors and translators occur. Identity-content matching is ground-truth-assisted diagnostic comparison, not a demonstrated safe roster parser or evidence of calibrated confidence. Two XMP objects were unreadable as streams and six packets required non-network recovery parsing; those eight records were already correct in ordinary-neutral mode. Their extraction limits remain recorded rather than being interpreted as empty metadata.

Three currently failing records contain the full correct author identity in Author/creator: an XMP-only credit, a normal Info Author, and an inverted catalogue name with a birth year. Recovering all three without regressions would give 313/382 (81.9%). This is not a ceiling for all metadata.

Of 39 nonblank Keywords fields, 16 contain structured ProQuest bibliographic records. Seven of these currently fail: six contain a full parent-monograph author credit, while the seventh supplies only a first author followed by et al. The six include two related excerpts of one work; one is a one-page part divider. Existing manual publishing-role adjudications distinguish parent monograph, selected excerpt, subjects, epigraph sources, translators and foreword writers. Combined with the three Author/creator opportunities, nine diagnostic recoveries would yield 319/382 (83.5%), a gain of 2.36 percentage points, if all were achieved without any regression. This is a conditional opportunity count, not an implemented result, independent-work count, or exhaustive theoretical upper bound.

Seven currently correct structured-Keywords counterexamples identify parent editors/anthologies rather than the selected chapter creator. Automatic promotion would put these decisions at risk. One additional failing Author field combines the correct author with the translator; role evidence is necessary before accepting a roster. An et-al credit cannot supply omitted coauthors by itself.

If all 162 author-field-bearing PDFs were somehow solved, holding every other output fixed, the arithmetic would be 328/382 (85.9%). This deliberately unrealistic subgroup oracle includes wrong and incomplete credits; it is not a metadata-only forecast. Fifty-four of the 72 current misses have no nonblank Author/creator field. Other metadata, work identity and visible/OCR evidence can still help those records. The 81.2% overall score is not an accuracy ceiling.

The next mechanism to investigate is a metadata responsibility expert: acquire Info/XMP credits and bibliographic exports, normalize catalogue forms, identify the selected and parent work, and reconcile contributor roles against visible publishing evidence. It should share evidence with existing experts and receive context-dependent priority, rather than blanket acceptance or blanket rejection. This audit implements none of those proposed runtime changes.

Validation is field inspection plus paired-output statistics and static report checks, not full PDF preparation, upload, embedding, installed application behavior or a new performance qualification. Private filenames, PDFs, metadata payloads, source identities and detailed adjudications remain local. The misnamed HTML is excluded from active scope; deletion was rejected by automatic approval review with only 'blocked by policy', so the payloads remain on disk. No alternative deletion route was attempted.

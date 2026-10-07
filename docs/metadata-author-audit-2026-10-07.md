# Author metadata recognition audit — 2026-10-07

## Method and results

The deterministic local corpus contains the prior 64-PDF sample, 24 additional MMT entries, 20 capitalism, 15 cultural, 15 film, 15 theory, 15 thesis, and 5 external dissertation entries. One MMT path named `.pdf` is a directory, leaving 172 readable PDFs. The before/after comparison used native text from pages 1–4 and the last two pages; books also used pages 5–12. This is sampled metadata inference, not full PDF processing or a manual correctness check of every author.

| Group | Readable | Unknown before | Unknown after |
| --- | ---: | ---: | ---: |
| Prior 64 | 64 | 18 | 13 |
| Other MMT | 23 | 15 | 14 |
| External dissertations | 5 | 3 | 1 |
| Capitalism | 20 | 7 | 7 |
| Cultural | 15 | 6 | 6 |
| Film | 15 | 1 | 1 |
| Theory | 15 | 7 | 7 |
| Thesis | 15 | 1 | 1 |
| **Total** | **172** | **58** | **50** |

Eight previously empty authors were populated; no previously populated author changed in this sample. The eight new assignments were checked against visible source credits:

| PDF | Newly recognized author(s) | Evidence used |
| --- | --- | --- |
| `0220254.pdf` | Diana N. Carvajal, Yohualli B. Anaya, Ivonne McLean, Miranda Aragón, Edgar Figueroa, Gabriela Plasencia, Viviana Martinez-Bianchi, José E. Rodríguez | Eight credentialed names, numbered affiliations, corresponding author |
| `Critical_Latinx…pdf` | Luis Urrieta, Jr.; Dolores Calderón | Byline after a title clipped by the filename |
| `Cultural Homogenization…pdf` | Daniele Conversi | Repository cover and matching document byline |
| `donald-trumps-contribution…pdf` | John R. Hibbing | First-page byline with machine-generated filename title |
| `lst.2012.29.pdf` | Suzanne Oboler | Signed editorial closing block |
| `lisa-5279.pdf` | Ramón A. Gutiérrez | OpenEdition credit repeated in electronic citation |
| OhioLINK Begley dissertation | Mary Ann Begley | Name before dissertation submission statement |
| OhioLINK Toledo dissertation | Rosalinda C. Dunlap | Title-page byline, excluding committee/dean credits |

## Path checks and limitations

Five changed PDFs from the prior 64-PDF sample and both OhioLINK dissertations completed the offline automatic preparation CLI with `ready`, zero diagnostic errors, the expected final `detected_author`, and `skipped_prepare_only` upload status. The five original-sample runs generated 87, 226, 292, 140, and 87 output payloads respectively. Both dissertations used native text and zero OCR seconds. The UI preview and workspace label path were checked on the signed editorial: it found Suzanne Oboler on page 9 and produced `Oboler`. A nine-page synthetic editorial PDF exercises that same tail-page preview branch.

The remaining 50 Unknown cases have not been resolved or manually classified. Many are books or excerpts. Unchanged nonempty assignments have not all been manually validated; a stable author field alone does not prove that the full PDF output is unchanged. The automatic CLI runs did not upload or embed into AnythingLLM, and the running Desktop app was not restarted.

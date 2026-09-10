from copy import deepcopy
from types import SimpleNamespace

import pytest

import auto_anythingllm_pipeline as pipeline

pytestmark = pytest.mark.offline_deterministic


def row(text, y, x=40, width=500, size=7):
    return {"text": text, "normalized": text, "x0": x, "x1": x+width,
            "y0": y, "y1": y+8, "font_sizes": [size]}


def run(rows, rules=(), **kwargs):
    original = deepcopy(rows)
    result = pipeline._layout_note_groups(rows, 600, 800, 10, rules, **kwargs)
    assert rows == original
    assert sum(g["line_count"] for g in result[1]) == len(result[0])
    return result


def test_separator_extends_above_cutoff_without_losing_body():
    rows = [row("Body text stays above the separator.", 570, size=10),
            row("36 A detailed source note that begins above the old cutoff.", 612),
            row("37 The next citation is below the old cutoff.", 628),
            row("Continuation of that same source note.", 638)]
    excluded, groups, _ = run(rows, [(40, 600, 120)])
    assert excluded == {id(r) for r in rows[1:]}
    assert groups[0]["reason"] == "separator_confirmed_footnote_block"
    assert len(groups) == 1


@pytest.mark.parametrize("rules", [[], [(40, 490, 120)], [(40, 600, 590)], [(300, 600, 400)]])
def test_position_alone_does_not_authorize_upward_expansion(rules):
    rows = [row("36 A detailed note above the old cutoff is ambiguous.", 612),
            row("37 A source note below the old cutoff.", 628), row("Its continuation stays here.", 638)]
    excluded, _, _ = run(rows, rules)
    assert id(rows[0]) not in excluded


def test_nonsequential_numbers_and_wrong_font_block_expansion():
    rows = [row("36 A numbered item above the cutoff.", 612),
            row("99 An unrelated numbered item below the cutoff.", 628), row("Continued item.", 638)]
    assert id(rows[0]) not in run(rows, [(40, 600, 120)])[0]
    rows[0]["font_sizes"] = [10]
    assert id(rows[0]) not in run(rows, [(40, 600, 120)])[0]


@pytest.mark.parametrize("title", ["References", "Bibliography", "Notes", "End notes", "ENDNOTES"])
def test_explicit_end_matter_belongs_to_later_inclusion_policy(title):
    rows = [row(title, 590, size=10), row("1. A bibliography or end note entry.", 640),
            row("Continuation that would previously have been excluded.", 652)]
    assert run(rows, [(40, 610, 120)])[0] == set()


def test_reference_heading_does_not_protect_other_column_footnote():
    rows = [row("References", 590, x=340, width=180, size=10),
            row("1 A legitimate left-column source footnote.", 640, width=230),
            row("Continuation of the left-column footnote.", 652, width=230)]
    assert run(rows)[0] == {id(r) for r in rows[1:]}


@pytest.mark.parametrize("seed", ["12 (1), 33–36.", "88.1", "9.1", "91.3"])
def test_volume_continuation_and_decimal_table_values_cannot_start_notes(seed):
    rows = [row(seed, 640), row("Graeber, D., 2015. The Utopia of Rules.", 652),
            row("Harvey, D., 2005. A Brief History of Neoliberalism.", 664)]
    assert run(rows)[0] == set()


def test_single_line_ambiguous_note_preserved_and_margin_guard_honored():
    rows = [row("1 A detailed but single-line source note.", 650)]
    assert not run(rows)[0]
    rows.append(row("Continuation of the same source note.", 660))
    assert run(rows)[0]
    assert not run(rows, [(40, 640, 120)], disabled=True)[0]


def test_separate_marker_fragments_do_not_break_existing_note():
    rows = [row("170.", 650, width=15), row("114", 660, width=15),
            row("The legal enforcement of the old rules remains a citation.", 660, x=70, width=460),
            row("This note continues across another line.", 674)]
    assert run(rows)[0] == {id(r) for r in rows}


def test_graphics_failure_is_optional_and_no_candidate_means_no_graphics_read():
    class Page:
        rect = SimpleNamespace(height=800)
        def get_drawings(self):
            raise RuntimeError("malformed graphics")
    assert pipeline._layout_note_separator_rules(Page(), []) == ([], "not_required")
    assert pipeline._layout_note_separator_rules(Page(), [row("1 Source note", 650)]) == ([], "unavailable")


def test_reconciled_notes_follow_actual_selected_pages_not_all_native_candidates():
    note = {"text": "An excluded note", "reason": "separator_confirmed_footnote_block", "line_count": 2}
    evidence = {"status": "applied", "pages": [{"pdf_page": 2, "removed_marginalia": [{"text":"OCR footer"}]}],
                "excluded_footnote_count": 0, "other_evidence": "unchanged"}
    pages = [{"page": 1, "layout_excluded_footnotes": [note], "layout_note_candidates": []},
             {"page": 2, "text": "OCR chosen on this page"}]
    original = deepcopy(evidence)
    result = pipeline._reconcile_layout_note_evidence(evidence, pages)
    assert evidence == original
    assert result["excluded_footnote_count"] == 1
    assert result["other_evidence"] == "unchanged"
    assert result["pages"][1]["removed_marginalia"] == [{"text":"OCR footer"}]
    assert "note_evidence_origin" not in result["pages"][1]
    assert result["pages"][0]["note_evidence_origin"] == "selected_native_page"


@pytest.mark.parametrize("letter,next_letter", [("y", "a"), ("e", "i"), ("t", "t"), ("e", "t")])
def test_actual_broad_corpus_false_positive_geometry_remains_untouched(letter,next_letter):
    chars = [{"c":letter,"bbox":(0,0,6,10),"origin":(0,8)},
             {"c":" ","bbox":(3,0,5.5,10),"origin":(3,8)},
             {"c":next_letter,"bbox":(6,0,11,10),"origin":(6,8)}]
    assert pipeline._layout_ligature_space_repair(chars,10) == (letter+" "+next_letter,0)

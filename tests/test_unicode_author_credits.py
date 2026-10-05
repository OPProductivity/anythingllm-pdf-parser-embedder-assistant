from author_metadata.dispatcher import infer_author_from_samples
from pathlib import Path
import unicodedata

import pytest

import auto_anythingllm_pipeline as pipeline

pytestmark = pytest.mark.offline_deterministic


def infer(credit, *, title="Research on Social Inequality", tail=(), page=1, filename="article.pdf"):
    samples = [{"page": page, "text": "An academic discussion follows.\n" + credit + "\nFurther discussion continues."}, *tail]
    report = infer_author_from_samples(samples, Path(filename), title_hint=title)
    return pipeline.resolve_author_from_metadata_and_inference("Unrelated Computer User", report), report


@pytest.mark.parametrize("name", [
    "Jos\u00e9 Hern\u00e1ndez", "Jose Hern\u00e1ndez", "Jose Ram\u00edrez", "Jose Mu\u00f1oz",
    "J. M. Hern\u00e1ndez", "Jean-Luc L\u00e9vesque", "Fran\u00e7ois L'\u00c9cuyer",
    "Fran\u00e7ois L\u2019\u00c9cuyer", "\u00c9lodie Martin", "\u0141ukasz \u017bulczyk",
])
@pytest.mark.parametrize("role", ["By", "Reviewed by", "Author:", "Writer:"])
def test_explicit_unicode_credit_preserves_complete_person_name(name, role):
    resolved, _ = infer(f"{role} {name}")
    assert resolved["author"] == name


@pytest.mark.parametrize("role", ["By", "Written by", "Edited by", "Reviewed by", "Authors:"])
def test_decomposed_explicit_credit_has_canonical_equivalent_name(role):
    name = "Jos\u00e9 Hern\u00e1ndez"
    resolved, _ = infer(f"{role} {unicodedata.normalize('NFD', name)}")
    assert resolved["author"] == name


@pytest.mark.parametrize("conjunction", ["and", "&"])
@pytest.mark.parametrize("role", ["By", "Authors:"])
def test_comma_conjunction_credit_preserves_three_complete_names_and_order(role, conjunction):
    resolved, _ = infer(f"{role} Jos\u00e9 Hern\u00e1ndez, Ana Ram\u00edrez {conjunction} Clara Mu\u00f1oz")
    assert resolved["author"] == "Jos\u00e9 Hern\u00e1ndez, Ana Ram\u00edrez, Clara Mu\u00f1oz"


@pytest.mark.parametrize("role", ["By", "Authors:"])
def test_new_conjunction_path_never_silently_drops_invalid_member(role):
    report = pipeline.infer_author_from_text_samples([
        {"page": 1, "text": "An academic discussion follows.\n"
         f"{role} Alice Martin, Bruno Santos and metadata pending\nFurther discussion continues."},
    ], title_hint="Research on Social Inequality")
    resolved = pipeline.resolve_author_from_metadata_and_inference("", report)
    assert resolved["author"] == ""


@pytest.mark.parametrize("role", ["By", "Reviewed by", "Author:"])
def test_existing_title_fragment_guard_remains_active(role):
    name = "Jos\u00e9 Hern\u00e1ndez"
    resolved, _ = infer(f"{role} {name}", title="The Political Thought of " + name)
    assert resolved["author"] == ""


@pytest.mark.parametrize("page", [2, 3, 4])
@pytest.mark.parametrize("decomposed", [False, True])
def test_single_translator_exclusion_survives_unicode_and_fallback_paths(page, decomposed):
    name = "Jos\u00e9 Hern\u00e1ndez"
    raw = unicodedata.normalize("NFD", name) if decomposed else name
    resolved, _ = infer(f"By {raw}", tail=[{"page": page, "text": "Translated by " + raw}])
    assert resolved["author"] == ""


def test_translator_exclusion_removes_only_matching_person_from_list():
    name = "Jos\u00e9 Hern\u00e1ndez"
    raw = unicodedata.normalize("NFD", name)
    resolved, _ = infer(f"By Alice Martin and {raw}", tail=[{"page": 3, "text": "Translated by " + raw}])
    assert resolved["author"] == "Alice Martin"


def test_final_translator_exclusion_also_covers_explicit_filename_fallback():
    resolved, _ = infer("No visible byline.", filename="article-by-Alice-Martin.pdf",
                        tail=[{"page": 3, "text": "Translated by Alice Martin"}])
    assert resolved["author"] == ""


def test_user_author_override_still_wins_after_inference_exclusion():
    _, report = infer("By Alice Martin", tail=[{"page": 3, "text": "Translated by Alice Martin"}])
    resolved = pipeline.resolve_author_from_metadata_and_inference("", report, author_override="Chosen Author")
    assert resolved == {"author": "Chosen Author", "source": "user_override"}


def test_single_inverted_catalog_credit_is_not_treated_as_a_multiple_author_list():
    resolved, _ = infer("Author: Hern\u00e1ndez, Jos\u00e9")
    assert resolved["author"] == "Jos\u00e9 Hern\u00e1ndez"
    assert pipeline.normalize_author_candidate("Garc\u00eda M\u00e1rquez, Gabriel Jos\u00e9") == "Gabriel Jos\u00e9 Garc\u00eda M\u00e1rquez"


@pytest.mark.parametrize("credit", ["By Universit\u00e9 de Montr\u00e9al", "By jos\u00e9 hern\u00e1ndez", "By Jos\u00e9"])
def test_existing_nonperson_capitalization_and_single_name_guards_remain_active(credit):
    resolved, _ = infer(credit)
    assert resolved["author"] == ""


def test_existing_late_plain_byline_guard_is_unchanged():
    resolved, _ = infer("By Jos\u00e9 Hern\u00e1ndez", page=5)
    assert resolved["author"] == ""


@pytest.mark.parametrize("role", ["By", "Authors:"])
@pytest.mark.parametrize("length", [71, 80, 85, 100])
def test_long_credit_never_publishes_a_surname_prefix(role, length):
    name = "Ada " + "H" + "a" * (length - 5)
    resolved, _ = infer(f"{role} {name}")
    assert resolved["author"] == (name if length <= 80 else "")


def test_explicit_long_author_list_is_not_reduced_to_its_last_three_names():
    names = (
        "Alice Martin", "Bruno Santos", "Catherine Worthington",
        "Elizabeth Richardson", "Frederick Cunningham",
    )
    resolved, _ = infer("By " + ", ".join(names[:-1]) + " and " + names[-1])
    assert resolved["author"] == ", ".join(names)


def test_adjacent_affiliated_names_preserve_accented_surname():
    assert pipeline.extract_adjacent_affiliated_name_pairs(
        "Alice Martin Clara Hern\u00e1ndez University of Amsterdam"
    ) == ["Alice Martin", "Clara Hern\u00e1ndez"]


def test_inline_triple_name_match_requires_complete_list():
    assert pipeline.extract_explicit_multi_author_byline(
        "Alice Martin, Bruno Santos and Clara Hern\u00e1ndez"
    ) == ["Alice Martin", "Bruno Santos", "Clara Hern\u00e1ndez"]
    assert pipeline.extract_explicit_multi_author_byline(
        "Adam Lewis, Alice Martin, Bruno Santos and Clara Hern\u00e1ndez"
    ) == []


@pytest.mark.parametrize("marker", ["*", "\u2020", "\u00b2", "1", "12"])
def test_end_of_line_footnote_marker_does_not_hide_a_complete_byline(marker):
    resolved, _ = infer("By Jos\u00e9 Hern\u00e1ndez" + marker)
    assert resolved["author"] == "Jos\u00e9 Hern\u00e1ndez"


def test_cited_year_is_not_treated_as_a_short_footnote():
    resolved, _ = infer("By Jane Doe, 2002")
    assert resolved["author"] == ""

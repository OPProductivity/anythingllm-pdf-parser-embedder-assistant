import pytest
from author_metadata.physical_work import opening_title_hypotheses

pytestmark = pytest.mark.offline_deterministic


def line(text, size, y):
    return {
        "text": text,
        "size": size,
        "box": [50, y, 450, y + size],
        "baseline": y + size,
    }


def bundle(lines):
    return {
        "native": [
            {
                "page": 4,
                "text": "\n".join(r["text"] for r in lines),
                "horizontal_lines": lines,
            }
        ]
    }


def test_explicit_editor_boundary_allows_equal_sized_title_and_people():
    b = bundle(
        [
            line("Collective Memory", 24, 70),
            line("Edited by", 12, 130),
            line("Amelia Hart", 24, 170),
            line("and", 12, 210),
            line("Benedict Vale", 24, 250),
            line("Example University Press", 10, 340),
        ]
    )
    result = opening_title_hypotheses(b, "document-100")
    assert [r["title"] for r in result] == ["Collective Memory"]


def test_imprint_can_support_a_two_word_title_without_by():
    b = bundle(
        [
            line("Collective Memory", 24, 70),
            line("Amelia Hart", 14, 170),
            line("Fourth edition", 10, 230),
            line("Example University Press", 10, 280),
        ]
    )
    assert (
        opening_title_hypotheses(b, "document-100")[0]["title"] == "Collective Memory"
    )


def test_known_visible_title_does_not_get_replaced_by_another_hypothesis():
    b = bundle(
        [
            line("Collective Memory", 24, 70),
            line("Amelia Hart", 14, 170),
            line("Example University Press", 10, 280),
        ]
    )
    assert not opening_title_hypotheses(b, "Collective Memory")


def test_parent_editor_titlepage_is_not_a_local_chapter_title():
    b = bundle(
        [
            line("Collective Memory", 24, 70),
            line("Edited by", 12, 130),
            line("Amelia Hart", 18, 170),
            line("Example University Press", 10, 280),
        ]
    )
    assert not opening_title_hypotheses(b, "document-100", work_scope="chapter")


def test_series_advertisement_does_not_supply_primary_title():
    b = bundle(
        [
            line("A series edited by Amelia Hart", 12, 50),
            line("Other Coastal Stories", 24, 100),
            line("By Benedict Vale", 12, 170),
            line("Another Memory Book", 24, 230),
            line("By Cecilia Brooks", 12, 290),
        ]
    )
    assert not opening_title_hypotheses(b, "document-100")


def test_neutral_title_alternative_reaches_the_final_editor_roster(tmp_path):
    import fitz
    from author_metadata.context import AuthorEvidenceContext
    from author_metadata.dispatcher import infer_author
    from author_metadata.profile import DocumentProfile

    doc = fitz.open()
    for n in range(5):
        page = doc.new_page()
        if n == 3:
            for text, size, y in [
                ("Collective Memory", 24, 70),
                ("Edited by", 12, 130),
                ("Amelia Hart", 24, 170),
                ("and", 12, 210),
                ("Benedict Vale", 24, 250),
                ("Example University Press", 10, 340),
            ]:
                page.insert_text((50, y), text, fontsize=size)
        elif n == 4:
            page.insert_text(
                (50, 70), "Copyright 2025. All rights reserved. ISBN 1234", fontsize=10
            )
    path = tmp_path / "document-100.pdf"
    doc.save(path)
    doc.close()
    result = infer_author(
        AuthorEvidenceContext.from_samples(
            [],
            path=path,
            title_hint="document-100",
            profile=DocumentProfile("book", (), (), work_scope="single_work"),
        )
    )
    assert result["author"] == "Amelia Hart, Benedict Vale"
    assert any(
        h["title"] == "Collective Memory"
        for h in result["evidence_review"]["work_title_hypotheses"]
    )


def test_unlabelled_body_keeps_all_author_affiliation_pairs():
    from author_metadata.native_article import credit, alternating_credit

    lines = [
        line("Transforming power through local institutions", 20, 25),
        line("Amelia Hart", 12, 70),
        line("Example University", 10, 88),
        line("Benedict Vale", 12, 108),
        line("Another University", 10, 126),
        line(
            "The research examines how these institutions change their social relations over time.",
            12,
            160,
        ),
    ]
    assert not credit(lines, 600, 800)["author"]
    assert alternating_credit(lines, 600, 800)["author"] == "Amelia Hart, Benedict Vale"


@pytest.mark.parametrize(
    "date",
    [
        "January 17, 2025",
        "17 Jan. 2025",
        "2025-01-17",
        "17/01/2025",
        "Revised: Sept. 2025",
        "Spring 2025",
    ],
)
def test_publication_date_is_a_field_after_the_person_roster(date):
    from author_metadata.native_article import credit

    lines = [
        line("Collective Memory and Public Life", 24, 30),
        line("Amelia Hart", 12, 85),
        line(date, 12, 108),
        line("University of Westport", 10, 140),
        line("Abstract", 12, 180),
        line(
            "This study examines the records and shared public life of historical communities.",
            10,
            210,
        ),
    ]
    assert credit(lines, 600, 800)["author"] == "Amelia Hart"


def test_three_separate_name_columns_preserve_all_people():
    from author_metadata.native_article import _column_lines

    lines = [
        {"text": name, "size": 12, "baseline": 100, "box": [x, 88, x + 75, 100]}
        for name, x in [
            ("Amelia Hart", 50),
            ("Benedict Vale", 240),
            ("Cecilia Brooks", 430),
        ]
    ]
    assert (
        _column_lines(lines, 0, 600)[0]["text"]
        == "Amelia Hart, Benedict Vale, Cecilia Brooks"
    )


def test_centered_title_does_not_crop_wider_wrapped_author_roster():
    from author_metadata.native_article import credit

    title = {
        "text": "Collective Memory and Public Life",
        "size": 24,
        "baseline": 55,
        "box": [170, 30, 440, 55],
    }
    rows = [
        {
            "text": "Amelia Hart, Benedict Vale, Cecilia Brooks,",
            "size": 12,
            "baseline": 100,
            "box": [50, 88, 550, 100],
        },
        {
            "text": "Dorian Reed, Elena Moss and Farid Noor",
            "size": 12,
            "baseline": 114,
            "box": [60, 102, 540, 114],
        },
    ]
    following = [
        line("University of Westport", 10, 135),
        line("Abstract", 12, 180),
        line(
            "This study examines the public records and collective life of historical communities.",
            10,
            210,
        ),
    ]
    assert (
        credit([title, *rows, *following], 600, 800)["author"]
        == "Amelia Hart, Benedict Vale, Cecilia Brooks, Dorian Reed, Elena Moss, Farid Noor"
    )


def test_numbered_affiliation_can_use_acronyms_and_same_font_without_a_space():
    from author_metadata.native_article import credit

    rows = [
        line("Collective Memory and Public Life", 24, 30),
        line("Amelia Hart, Benedict Vale and Cecilia Brooks", 12, 85),
        line("1WRI, Westport, USA", 12, 115),
        line("2MRC, Northport, Canada", 12, 132),
        line("Abstract", 12, 180),
        line(
            "This study examines historical records and the shared public life of communities.",
            10,
            210,
        ),
    ]
    assert (
        credit(rows, 600, 800)["author"] == "Amelia Hart, Benedict Vale, Cecilia Brooks"
    )


def test_explicit_full_name_filename_can_identify_a_visible_article_title(tmp_path):
    import fitz
    from author_metadata.context import AuthorEvidenceContext
    from author_metadata.dispatcher import infer_author
    from author_metadata.profile import DocumentProfile

    doc = fitz.open()
    p = doc.new_page()
    p.insert_text((50, 70), "Collective Memory and Public Life", fontsize=22)
    for i in range(8):
        p.insert_text(
            (50, 130 + i * 20),
            "This discussion examines historical records and the changing institutions of public life.",
            fontsize=10,
        )
    path = tmp_path / "Amelia Hart - Collective Memory and Public Life.pdf"
    doc.save(path)
    doc.close()
    profile = DocumentProfile("scholarly_article", (), (), work_scope="single_work")
    result = infer_author(
        AuthorEvidenceContext.from_samples(
            [],
            path=path,
            title_hint="Collective Memory and Public Life",
            profile=profile,
        )
    )
    assert (
        result["author"] == "Amelia Hart"
        and result["source"] == "filename_leading_name"
    )
    different = tmp_path / "Amelia Hart - An Entirely Different Work.pdf"
    different.write_bytes(path.read_bytes())
    other = infer_author(
        AuthorEvidenceContext.from_samples(
            [],
            path=different,
            title_hint="Collective Memory and Public Life",
            profile=profile,
        )
    )
    assert not other["author"]


def test_removed_reference_marker_preserves_space_before_a_conjunction():
    from author_metadata.native_article import positioned_lines

    class Page:
        def get_text(self, *args, **kwargs):
            return {
                "blocks": [
                    {
                        "lines": [
                            {
                                "dir": (1, 0),
                                "bbox": [0, 0, 250, 20],
                                "spans": [
                                    {
                                        "text": "Amelia Hart",
                                        "size": 12,
                                        "flags": 0,
                                        "origin": (0, 12),
                                        "bbox": [0, 0, 60, 15],
                                    },
                                    {
                                        "text": "1,*",
                                        "size": 7,
                                        "flags": 1,
                                        "origin": (60, 7),
                                        "bbox": [60, 0, 68, 8],
                                    },
                                    {
                                        "text": "and Benedict Vale",
                                        "size": 12,
                                        "flags": 0,
                                        "origin": (68, 12),
                                        "bbox": [68, 0, 200, 15],
                                    },
                                ],
                            }
                        ]
                    }
                ]
            }

    assert positioned_lines(Page())[0]["text"] == "Amelia Hart and Benedict Vale"

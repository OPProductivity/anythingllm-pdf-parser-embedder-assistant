"""Transfer cases for publishing relations, including competing roles."""

import pytest

from author_metadata.publishing_relations import claims, select
from author_metadata.native_article import positioned_lines
from author_metadata.structured_credit import _names


pytestmark = pytest.mark.offline_deterministic


def test_owner_needs_separate_complete_article_credit_and_scope():
    samples = [
        {"page": 1, "text": "© 2024 by Amina Rao, Leon Wu, and Marta Silva."},
        {"page": 2, "text": "Markets across several countries\nAmina Rao, Leon Wu, and Marta Silva\nWorking Paper 123\nAbstract\nA summary of this research."},
    ]
    found, _ = claims(samples, kind="scholarly_article", work_scope="article")
    chosen, _ = select({"author": "", "source": "not_found"}, found, kind="scholarly_article", work_scope="article")
    assert chosen["author"] == "Amina Rao, Leon Wu, Marta Silva"
    for replacement in ["Amina Rao, Leon Wu", "Amina Rao, Leon Wu, and Dario Costa"]:
        modified = [samples[0], samples[1] | {"text": samples[1]["text"].replace("Amina Rao, Leon Wu, and Marta Silva", replacement)}]
        found, _ = claims(modified, kind="scholarly_article", work_scope="article")
        assert not found
    found, _ = claims(samples, kind="scholarly_article", work_scope="whole_issue")
    assert not found


def test_component_and_publisher_ownership_do_not_promote_people():
    for ownership in ["Cover photograph © 2024 Amina Rao", "Translation © 2024 Amina Rao", "© 2024 Example University Press"]:
        samples = [{"page": 1, "text": ownership}, {"page": 2, "text": "Markets across several countries\nAmina Rao\nAbstract\nSummary"}]
        found, _ = claims(samples, kind="scholarly_article", work_scope="article")
        assert not found


def test_professional_role_delimits_person_but_dash_alone_does_not():
    for line, expected in [("Amina Rao—Community History Forum Research Associate", "Amina Rao"), ("Amina Rao—Leon Wu", "")]:
        found, _ = claims([{"page": 1, "text": "Changing accounts of local history\n" + line}], kind="generic", work_scope="article")
        assert [c["author"] for c in found] == ([expected] if expected else [])


def test_local_chapter_contributors_remain_complete():
    found, _ = claims([{"page": 1, "text": "4. A bounded chapter\nContributors: Amina Rao; Leon Wu\n\nOpening prose."}], kind="book_chapter", work_scope="chapter")
    assert any(c["author"] == "Amina Rao, Leon Wu" and c["family"] == "local_chapter_contributors" for c in found)


def test_superscript_whitespace_does_not_merge_two_coauthors():
    class Page:
        def get_text(self, *_args, **_kwargs):
            return {"blocks": [{"lines": [{"dir": (1, 0), "bbox": (0, 0, 250, 15), "spans": [
                {"text": "Amina Rao", "size": 12, "flags": 0, "origin": (0, 12)},
                {"text": "4", "size": 7, "flags": 1, "origin": (60, 7)},
                {"text": " ", "size": 12, "flags": 1, "origin": (65, 7)},
                {"text": "and Leon Wu", "size": 12, "flags": 0, "origin": (69, 12)},
            ]}]}]}
    lines = positioned_lines(Page())
    assert _names(lines[0]["text"]) == ["Amina Rao", "Leon Wu"]


def test_unlabelled_summary_cannot_truncate_alternating_author_affiliation_pairs():
    from author_metadata.native_article import credit
    def line(text, y, size=12):
        return {"text": text, "size": size, "box": [70, y, 480, y + size], "baseline": y + size}
    title = [line("Transforming power through local institutions", 25, 20)]
    single = title + [line("Amina Rao", 70), line("Example University", 88, 10), line("The research examines how these institutions change their social relations over time.", 130)]
    assert credit(single, 600, 800)["author"] == "Amina Rao"
    alternating = title + [line("Amina Rao", 70), line("Example University", 88, 10), line("Leon Wu", 108), line("Another University", 126, 10), line("The research examines how these institutions change their social relations over time.", 160)]
    assert not credit(alternating, 600, 800)["author"]

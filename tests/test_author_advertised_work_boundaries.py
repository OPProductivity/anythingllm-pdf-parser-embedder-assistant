"""Publishing-unit and wrapped roster regressions with invented names."""

import pytest
from author_metadata.text_responsibility import _credit, claims

pytestmark = pytest.mark.offline_deterministic


def test_comma_wrapped_roster_is_not_a_one_person_credit():
    assert _credit(
        "Amelia Hart,\nBenedict Vale, and Cecilia Brooks.\nDescription: Harbor: Example Press, 2025."
    ) == ["Amelia Hart", "Benedict Vale", "Cecilia Brooks."]


def test_ampersand_wrapped_roster_reaches_the_final_person():
    assert _credit(
        "AMELIA HART,\nBENEDICT VALE &\nCECILIA BROOKS\n\nEXAMPLE PRESS"
    ) == ["AMELIA HART", "BENEDICT VALE", "CECILIA BROOKS"]


def test_unfinished_multiname_line_does_not_return_an_earlier_prefix():
    assert _credit("Amelia Hart,\nBenedict Vale,") == []


def test_series_advertisement_does_not_become_the_selected_work_byline():
    text = "New Directions in Coastal Studies\n\nA series edited by Otto Finch and Petra Moss\n\nTidal Lives in the North\n  by Amelia Hart\n\nSelected Coastline Studies\n  edited by Benedict Vale and Cecilia Brooks\n\nA second different work with many words about this field and the history of northern seas, landscapes, communities and their public representations."
    result = claims(
        [{"page": 2, "text": text}],
        kind="book",
        work_scope="book",
        title_hint="Selected Coastline Studies",
    )
    assert not any(
        c["author"] == "Amelia Hart" and c["work"] == "selected work" for c in result
    )
    assert any(
        c["author"] == "Benedict Vale, Cecilia Brooks" and c["work"] == "selected work"
        for c in result
    )


def test_a_matching_local_title_can_bind_a_listed_byline():
    text = "A series edited by Otto Finch and Petra Moss\n\nSelected Coastline Studies\nby Amelia Hart\n\nAnother book\nby Benedict Vale\n\nExample University Press ISBN 1234"
    result = claims(
        [{"page": 2, "text": text}],
        kind="book",
        work_scope="book",
        title_hint="Selected Coastline Studies",
    )
    assert any(
        c["author"] == "Amelia Hart" and c["work"] == "selected work" for c in result
    )
    assert not any(
        c["author"] == "Benedict Vale" and c["work"] == "selected work" for c in result
    )


def test_late_catalogue_credit_is_not_cut_mid_name_by_a_character_window():
    text = (
        "Collective Memory and Public Life\n"
        + ("Legal production and distribution information. " * 44)
        + "\nCollective Memory and Public Life\nedited by Amelia Hart, Benedict Vale and Cecilia Brooks\n\nExample Press"
    )
    result = claims(
        [{"page": 5, "text": text}],
        kind="book",
        work_scope="single_work",
        title_hint="Collective Memory and Public Life",
    )
    assert any(
        c["author"] == "Amelia Hart, Benedict Vale, Cecilia Brooks" for c in result
    )
    assert not any(
        c["author"] in ["Amelia", "Amelia Ha", "Amelia Hart"] for c in result
    )

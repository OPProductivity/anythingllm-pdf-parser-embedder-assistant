"""Temporary lexical calculations must never become cross-input evidence."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from author_metadata.context import AuthorEvidenceContext
from author_metadata.dispatcher import infer_author
from author_metadata.lexical_scope import _SCOPE, lexical_scope
from author_metadata.names import looks_like_person_name, normalize_author_candidate
from author_metadata.structured_credit import _names, _words

pytestmark = pytest.mark.offline_deterministic


def test_each_inference_gets_a_new_table_and_discards_it(monkeypatch):
    tables = []

    def infer(_context):
        tables.append(_SCOPE.get())
        assert _names("Amelia Hart") == ["Amelia Hart"]
        assert tables[-1]
        return {"author": "Amelia Hart", "source": "text_byline"}

    monkeypatch.setattr("author_metadata.adjudication.infer", infer)
    for _ in range(2):
        assert (
            infer_author(AuthorEvidenceContext.from_samples([]))["author"]
            == "Amelia Hart"
        )
        assert _SCOPE.get() is None
    assert tables[0] is not tables[1]
    assert tables == [{}, {}]


def test_inference_exception_also_discards_all_text(monkeypatch):
    tables = []

    def fail(_context):
        tables.append(_SCOPE.get())
        _names("Amelia Hart")
        raise RuntimeError("recognizer failed")

    monkeypatch.setattr("author_metadata.adjudication.infer", fail)
    with pytest.raises(RuntimeError, match="recognizer failed"):
        infer_author(AuthorEvidenceContext.from_samples([]))
    assert _SCOPE.get() is None
    assert tables == [{}]


def test_nested_inference_has_independent_state_and_restores_outer():
    with lexical_scope():
        outer = _SCOPE.get()
        _names("Amelia Hart")
        with lexical_scope():
            inner = _SCOPE.get()
            assert inner is not outer
            assert not inner
            _names("Bernard Stone")
        assert not inner
        assert _SCOPE.get() is outer
        assert outer
    assert not outer
    assert _SCOPE.get() is None


def test_parallel_inferences_do_not_share_tables(monkeypatch):
    barrier = Barrier(3)
    tables = []

    def infer(_context):
        table = _SCOPE.get()
        tables.append(table)
        _names("Amelia Hart")
        barrier.wait(timeout=5)
        assert table is _SCOPE.get()
        return {"author": "Amelia Hart"}

    monkeypatch.setattr("author_metadata.adjudication.infer", infer)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(
            pool.map(
                lambda _: infer_author(AuthorEvidenceContext.from_samples([])), range(3)
            )
        )
    assert all(r["author"] == "Amelia Hart" for r in results)
    assert len({id(table) for table in tables}) == 3
    assert tables == [{}, {}, {}]
    assert _SCOPE.get() is None


def test_mutating_one_returned_roster_cannot_rewrite_another_expert():
    with lexical_scope():
        first = _names("Amelia Hart and Bernard Stone")
        first.append("Cecilia Brooks")
        assert _names("Amelia Hart and Bernard Stone") == [
            "Amelia Hart",
            "Bernard Stone",
        ]


def test_name_grammar_keeps_title_and_capitalization_arguments_distinct():
    with lexical_scope():
        assert looks_like_person_name("AMELIA HART", allow_all_caps=True)
        assert not looks_like_person_name("AMELIA HART", allow_all_caps=False)
        for title in ("", "Amelia Hart", "The Archive of Modern Letters"):
            for caps in (True, False):
                expected = looks_like_person_name.__wrapped__(
                    "Amelia Hart", title, allow_all_caps=caps
                )
                assert (
                    looks_like_person_name("Amelia Hart", title, allow_all_caps=caps)
                    == expected
                )


@pytest.mark.parametrize(
    "value",
    [
        "Amelia Hart",
        "Bernard Francis-Bacon",
        "Cecilia Brooks, Jr.",
        "D. Stone",
        "Reader-Response and Reception Theory",
        "2. A Section Title",
        "Department of Letters",
        "By Amelia Hart",
        "Amelia Hart, Bernard Stone,",
        "Amelia Hart · Bernard Stone",
        "",
        "OpenAI",
    ],
)
def test_temporary_parsing_preserves_existing_grammar(value):
    expected_words = _words(value)
    expected_names = _names(value)
    expected_normalized = normalize_author_candidate(value)
    with lexical_scope():
        for _ in range(3):
            assert _words(value) == expected_words
            assert _names(value) == expected_names
            assert normalize_author_candidate(value) == expected_normalized


def test_memory_bound_does_not_truncate_lexical_analysis():
    with lexical_scope():
        for index in range(8200):
            value = f"Reference heading {index}"
            assert _words(value) == _words.__wrapped__(value)
        assert len(_SCOPE.get()) <= 8192
        assert _names("Amelia Hart and Bernard Stone") == [
            "Amelia Hart",
            "Bernard Stone",
        ]


def test_long_input_is_processed_without_retaining_it():
    value = "ordinary text " * 700
    with lexical_scope():
        assert _words(value) == _words.__wrapped__(value)
        assert not _SCOPE.get()

import fitz
import pytest
from author_metadata.native_reads import (
    native_review_scope,
    native_measurement,
    native_read,
)

pytestmark = pytest.mark.offline_deterministic


def pdf(path, text="By Laura Field"):
    with fitz.open() as doc:
        for _ in range(2):
            page = doc.new_page()
            page.insert_text((50, 50), text)
        doc.save(path)


def test_same_bytes_reopened_keep_independent_mutable_values_and_scope(tmp_path):
    path = tmp_path / "work.pdf"
    pdf(path)
    calls = []

    def acquire():
        calls.append(1)
        return {"words": ["Laura", "Field"]}

    with native_review_scope(path) as scope:
        with fitz.open(path) as doc:
            first = native_measurement(doc[0], "words", ("exact",), acquire)
            first["words"].append("mutated")
        with fitz.open(path) as doc:
            second = native_measurement(doc[0], "words", ("exact",), acquire)
            assert second == {"words": ["Laura", "Field"]}
        assert len(calls) == 1
        assert scope.operations["words"] == {"requests": 2, "reads": 1, "reused": 1}
    with native_review_scope(path):
        with fitz.open(path) as doc:
            native_measurement(doc[0], "words", ("exact",), acquire)
    assert len(calls) == 2


def test_other_page_options_document_and_mutation_cannot_share(tmp_path):
    path = tmp_path / "work.pdf"
    other = tmp_path / "other.pdf"
    pdf(path)
    pdf(other, "By Another Person")
    calls = []

    def read():
        calls.append(1)
        return len(calls)

    with native_review_scope(path):
        with fitz.open(path) as doc, fitz.open(other) as foreign:
            assert native_measurement(doc[0], "read", ("one",), read) == 1
            assert native_measurement(doc[1], "read", ("one",), read) == 2
            assert native_measurement(doc[0], "read", ("two",), read) == 3
            assert native_measurement(foreign[0], "read", ("one",), read) == 4
            doc[0].insert_text((50, 80), "changed")
            assert native_measurement(doc[0], "read", ("one",), read) == 5


def test_native_options_stay_distinct_and_results_match_direct_reads(tmp_path):
    path = tmp_path / "work.pdf"
    pdf(path)
    with native_review_scope(path) as scope, fitz.open(path) as doc:
        page = doc[0]
        for sort in [False, True, False, True]:
            assert native_read(page, "get_text", "words", sort=sort) == page.get_text(
                "words", sort=sort
            )
        assert scope.operations["get_text"]["reads"] == 2
        assert scope.operations["get_text"]["reused"] == 2


def test_source_change_invalidates_and_exception_is_not_reused(tmp_path):
    path = tmp_path / "work.pdf"
    pdf(path)
    with native_review_scope(path) as scope, fitz.open(path) as doc:

        def fail():
            raise RuntimeError("read failed")

        with pytest.raises(RuntimeError):
            native_measurement(doc[0], "read", (), fail)
        assert native_measurement(doc[0], "read", (), lambda: "ok") == "ok"
        # The scope must stop sharing if the source changes during a review.
        with path.open("ab") as stream:
            stream.write(b"\n")
        assert native_measurement(doc[0], "read", (), lambda: "fresh") == "fresh"
        assert scope.invalidated is True

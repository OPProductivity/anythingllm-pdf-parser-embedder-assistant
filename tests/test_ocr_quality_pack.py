import json
import pytest
from benchmarks.ocr_output_quality import assess, run_pack, text_hash

pytestmark = pytest.mark.offline_deterministic


def reference(text="First sentence. Last sentence."):
    return {
        "required_passages": ["First sentence."],
        "forbidden_fragments": ["Neighbour page text"],
        "ordered_anchors": ["First", "Last"],
        "region_count": 1,
        "baseline_text_sha256": text_hash(text),
        "review_note": "Verified specimen",
    }


def kinds(case, text, regions=None):
    return {f["kind"] for f in assess(case, text, [{}] if regions is None else regions)}


def test_whitespace_is_not_a_text_change():
    assert not kinds(reference(), "First\n sentence.  Last sentence.")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("First sentance. Last sentence.", "missing_verified_passage"),
        ("Last sentence.", "missing_verified_passage"),
        ("Last sentence. First sentence.", "missing_or_reordered_anchor"),
        ("First sentence. Last sentence. Neighbour page text", "forbidden_fragment"),
        ("First sentence. Last sentence. unexpected", "output_changed_requires_review"),
    ],
)
def test_controlled_output_mutations_are_detected(text, expected):
    c = reference()
    before = json.dumps(c)
    assert expected in kinds(c, text)
    assert json.dumps(c) == before  # no self-blessing


def test_baseline_hash_alone_is_not_a_quality_reference():
    c = reference()
    c["required_passages"] = []
    assert "human_reference_not_established" in kinds(
        c, "First sentence. Last sentence."
    )
    assert "region_count_changed" in kinds(
        reference(), "First sentence. Last sentence.", []
    )


def test_missing_pdf_fails_instead_of_skipping(tmp_path):
    manifest = tmp_path / "pack.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "cases": [{"id": "missing", "source": "missing.pdf", **reference()}],
            }
        )
    )
    before = manifest.read_bytes()
    result = run_pack(manifest, "unused")
    assert not result["passed"]
    assert result["cases"][0]["failures"][0]["kind"] == "fixture_execution_failed"
    assert before == manifest.read_bytes()


@pytest.mark.parametrize("cases", [[], [{"id": "same"}, {"id": "same"}]])
def test_empty_or_duplicate_pack_is_not_a_pass(tmp_path, cases):
    path = tmp_path / "pack.json"
    path.write_text(json.dumps({"schema_version": 1, "cases": cases}))
    with pytest.raises(ValueError):
        run_pack(path, "unused")

from copy import deepcopy
import pytest
import rag_pdf_gradio_app as app

pytestmark = pytest.mark.offline_deterministic


def manifest(path):
    return {
        "files": [
            {
                "file": str(path),
                "source_version": app.local_source_version(path),
                "pages": 12,
                "sampled_pages": 3,
                "mean_chars_per_page": 2000,
                "sparse_fraction": 0,
                "image_density": 0,
                "risk": "clear",
                "full_native_text_coverage": {},
            }
        ]
    }


def test_confirmation_facts_avoid_reopening_source_for_progress(tmp_path, monkeypatch):
    path = tmp_path / "source.pdf"
    path.write_bytes(b"only stat identity is needed")
    confirmed = manifest(path)
    before = deepcopy(confirmed)

    def no_sample(*args, **kwargs):
        raise AssertionError("redundant progress sample")

    monkeypatch.setattr(app, "automatic_timing_document_profile", no_sample)
    result = app.automatic_progress_file_allocations(
        [path], confirmation_manifest=confirmed
    )
    assert result[0]["profile_origin"] == "confirmation"
    assert result[0]["pages"] == 12
    assert result[-1]["end_share"] == 1
    assert confirmed == before


def test_replaced_source_uses_bounded_fresh_estimate(tmp_path, monkeypatch):
    path = tmp_path / "source.pdf"
    path.write_bytes(b"old")
    confirmed = manifest(path)
    path.write_bytes(b"changed source")
    calls = []

    def sample(paths, **kwargs):
        calls.append((paths, kwargs))
        return {"page_count": 8, "mean_chars_per_page": 2000}

    monkeypatch.setattr(app, "automatic_timing_document_profile", sample)
    result = app.automatic_progress_file_allocations(
        [path], confirmation_manifest=confirmed
    )
    assert calls[0][1] == {"page_sample_limit": 3}
    assert result[0]["pages"] == 8
    assert result[0]["profile_origin"] == "three_page_estimate"


@pytest.mark.parametrize(
    "bad", [None, [], "bad", {"files": 5}, {"files": [None, {}, {"file": 5}]}]
)
def test_malformed_manifest_cannot_claim_confirmed_facts(tmp_path, bad):
    path = tmp_path / "source.pdf"
    path.write_bytes(b"source")
    assert app.confirmed_progress_profile(path, bad) is None


def test_same_basename_in_another_directory_cannot_share_facts(tmp_path):
    one = tmp_path / "one" / "source.pdf"
    two = tmp_path / "two" / "source.pdf"
    one.parent.mkdir()
    two.parent.mkdir()
    one.write_bytes(b"same")
    two.write_bytes(b"same")
    assert app.confirmed_progress_profile(two, manifest(one)) is None

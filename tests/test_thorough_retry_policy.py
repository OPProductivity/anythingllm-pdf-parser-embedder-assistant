"""Effort differences target a low-information retry, never another PDF region."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from author_metadata import evidence_collection as acquisition
from author_metadata.ocr_effort_policy import defer_photographic_orientation_retry
from run_request import RunRequest

pytestmark = pytest.mark.offline_deterministic


def task(**overrides):
    return {"page": 3, "reason": "image_without_native_overlay", "clip": [0, 0, 400, 600],
            "recognition_effort": "thorough", **overrides}


FRAME = {"payload": b"rendered pixels", "border": 0, "preprocessing": [],
         "visual_profile": {"mid_tone_fraction": .7, "light_fraction": .1}}


@pytest.mark.parametrize("override", [
    {"page": 1}, {"recognition_effort": "extremely_thorough"},
    {"reason": "invisible_native_text_overlay"}, {"reason": "invisible_explicit_byline"},
    {"reason": "narrow_text_image"}, {"reason": "sparse_native"},
])
def test_covers_scans_and_explicit_text_tasks_keep_the_full_retry_schedule(override):
    assert not defer_photographic_orientation_retry(task(**override), FRAME, "", None)


@pytest.mark.parametrize("text,confidence", [("IBM", 0), ("A. Li", 0), ("By Jo", 0), ("", 20)])
def test_even_short_alphabetic_credits_or_useful_confidence_are_not_called_lost(text, confidence):
    assert not defer_photographic_orientation_retry(task(), FRAME, text, confidence)


def test_white_text_page_or_unknown_image_profile_does_not_qualify_as_a_photograph():
    assert not defer_photographic_orientation_retry(task(), {}, "", None)
    frame = deepcopy(FRAME)
    frame["visual_profile"] = {"mid_tone_fraction": .05, "light_fraction": .9}
    assert not defer_photographic_orientation_retry(task(), frame, "", None)


@pytest.mark.parametrize("effort,executions", [("thorough", 1), ("extremely_thorough", 2)])
def test_read_region_preserves_primary_and_records_an_intentional_deferral(tmp_path, monkeypatch, effort, executions):
    executable = tmp_path / "tesseract.exe"
    executable.write_bytes(b"fixture")
    calls = []
    monkeypatch.setattr(acquisition.subprocess, "run", lambda *a, **kw: calls.append(kw["input"]) or SimpleNamespace(stdout=b"tsv"))
    from author_metadata import portable_layout
    monkeypatch.setattr(portable_layout, "read_tsv", lambda _data: ("", []))
    raster = acquisition._RasterRegion(400, 600, {2.5: deepcopy(FRAME)})
    readings, attempts = acquisition._read_region(raster, task(recognition_effort=effort), str(executable))
    assert len(calls) == executions
    assert len(readings) == 1 and readings[0]["text"] == ""
    assert attempts[0]["status"] == "read"
    deferred = [a for a in attempts if a["status"] == "deferred_by_effort"]
    assert len(deferred) == (effort == "thorough")
    if deferred:
        assert deferred[0]["primary_reading_preserved"]


def test_confirmed_request_freezes_the_selected_effort_and_passes_it_to_worker_namespace():
    settings = {"recognition_effort": "thorough", "files": ["sample.pdf"]}
    request = RunRequest.from_automatic_settings(settings, mode="local_only", segment_mode="none")
    settings["recognition_effort"] = "extremely_thorough"
    assert request.recognition_effort == "thorough"
    assert request.to_legacy_namespace().recognition_effort == "thorough"


def test_legacy_engine_request_retains_extremely_thorough_acquisition():
    assert RunRequest(input_paths=("sample.pdf",)).recognition_effort == "extremely_thorough"


@pytest.mark.parametrize("empty_pairs,expected", [(2, False), (3, True), (8, True)])
def test_feedback_uses_completed_prefix_and_keeps_every_primary_region(monkeypatch, empty_pairs, expected):
    groups = []
    tasks = [task(page=index + 2) for index in range(12)]

    def read(_document, selected, _executable, *, workers):
        groups.append(deepcopy(selected))
        for item in selected:
            empty = item["page"] - 2 < empty_pairs
            attempts = [{"status": "read", "reason": reason,
                         "alphabetic_characters": 0 if empty else 20}
                        for reason in [item["reason"], "empty_or_low_quality_orientation_recheck"]]
            yield item, [{"page": item["page"], "text": "" if empty else "A useful credit"}], attempts

    monkeypatch.setattr(acquisition, "_read_regions", read)
    results = list(acquisition._read_regions_with_effort(None, tasks, "ocr", workers=8))
    assert [len(group) for group in groups] == [8, 4]
    assert [result[0]["page"] for result in results] == list(range(2, 14))
    assert all(item.get("empty_orientation_prefix_exhausted", False) == expected for item in groups[1])
    assert all("empty_orientation_prefix_exhausted" not in item for item in tasks)


def test_small_plan_does_not_wait_for_a_feedback_phase(monkeypatch):
    tasks = [task(page=index + 2) for index in range(7)]
    groups = []
    monkeypatch.setattr(acquisition, "_read_regions", lambda _doc, selected, _exe, **kw: groups.append(selected) or iter(()))
    assert list(acquisition._read_regions_with_effort(None, tasks, "ocr", workers=8)) == []
    assert groups == [tasks]

"""Preference persistence is independent of detected metadata and run defaults."""

import json
import pytest

from recognition_effort import (
    RecognitionEffort,
    load_recognition_effort,
    normalize_effort,
    recognition_effort_path,
    save_recognition_effort,
)

pytestmark = pytest.mark.offline_deterministic


def test_first_use_is_thorough_without_creating_state(tmp_path):
    state = load_recognition_effort(home_directory=tmp_path)
    assert state.effort is RecognitionEffort.THOROUGH
    assert not state.notice
    assert not recognition_effort_path(tmp_path).exists()


def test_saved_last_choice_survives_a_fresh_read_and_can_change_back(tmp_path):
    automatic = tmp_path / "settings" / "automatic-defaults.json"
    automatic.parent.mkdir()
    automatic.write_text('{"retained":true}', encoding="utf8")
    saved = automatic.read_bytes()
    assert save_recognition_effort(1, home_directory=tmp_path) is RecognitionEffort.EXTREMELY_THOROUGH
    assert load_recognition_effort(home_directory=tmp_path).effort is RecognitionEffort.EXTREMELY_THOROUGH
    save_recognition_effort(0.0, home_directory=tmp_path)
    assert load_recognition_effort(home_directory=tmp_path).effort is RecognitionEffort.THOROUGH
    assert automatic.read_bytes() == saved
    assert not list(automatic.parent.glob("*.tmp"))


@pytest.mark.parametrize("bad", [True, False, None, -1, 2, 0.5, "fast", [], {}])
def test_invalid_input_does_not_reset_an_existing_preference(tmp_path, bad):
    save_recognition_effort(1, home_directory=tmp_path)
    before = recognition_effort_path(tmp_path).read_bytes()
    with pytest.raises(ValueError):
        save_recognition_effort(bad, home_directory=tmp_path)
    assert recognition_effort_path(tmp_path).read_bytes() == before


@pytest.mark.parametrize("raw", ["{", "null", "[]", '{"schema_version":true,"effort":"thorough"}', '{"schema_version":2,"effort":"thorough"}', '{"schema_version":1,"effort":"fast"}', '{"schema_version":1,"effort":[]}'])
def test_bad_state_is_reported_and_not_silently_rewritten(tmp_path, raw):
    path = recognition_effort_path(tmp_path)
    path.parent.mkdir()
    path.write_text(raw, encoding="utf8")
    state = load_recognition_effort(home_directory=tmp_path)
    assert state.effort is RecognitionEffort.THOROUGH
    assert state.notice
    assert path.read_text(encoding="utf8") == raw


def test_write_failure_preserves_last_saved_value(tmp_path, monkeypatch):
    save_recognition_effort(1, home_directory=tmp_path)
    path = recognition_effort_path(tmp_path)
    before = path.read_bytes()

    def unavailable(*args):
        raise PermissionError("File is busy")

    monkeypatch.setattr("recognition_effort.os.replace", unavailable)
    with pytest.raises(PermissionError, match="File is busy"):
        save_recognition_effort(0, home_directory=tmp_path)
    assert path.read_bytes() == before
    assert not list(path.parent.glob("*.tmp"))


def test_snapshot_profiles_remain_separate(tmp_path):
    development, stable = tmp_path / "development", tmp_path / "stable"
    save_recognition_effort(1, home_directory=development)
    assert load_recognition_effort(home_directory=stable).effort is RecognitionEffort.THOROUGH
    save_recognition_effort(0, home_directory=stable)
    assert load_recognition_effort(home_directory=development).effort is RecognitionEffort.EXTREMELY_THOROUGH


def test_round_trip_serializes_only_preference(tmp_path):
    save_recognition_effort("Extremely Thorough", home_directory=tmp_path)
    raw = json.loads(recognition_effort_path(tmp_path).read_text(encoding="utf8"))
    assert raw == {"schema_version": 1, "effort": "extremely_thorough"}
    assert normalize_effort(RecognitionEffort.THOROUGH).slider_value == 0
    assert normalize_effort("Extremely Thorough").label == "Extremely Thorough"


@pytest.mark.parametrize("value", [None, "", 0, 1, 0.0, 1.0, "thorough", "Extremely Thorough"])
def test_production_policy_accepts_known_historical_values_without_restoring_retired_mode(value):
    from recognition_effort import production_effort
    assert production_effort(value) is RecognitionEffort.THOROUGH


@pytest.mark.parametrize("value", [False, True, 2, .5, "fast", {}, []])
def test_fixed_production_policy_does_not_silently_accept_invalid_intent(value):
    from recognition_effort import production_effort
    with pytest.raises(ValueError):
        production_effort(value)

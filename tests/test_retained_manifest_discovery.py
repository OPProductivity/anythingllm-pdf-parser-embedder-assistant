"""Retained malformed evidence must not hide readable recovery manifests."""
import json
import os

import pytest

import rag_pdf_gradio_app as app

pytestmark = pytest.mark.offline_deterministic


@pytest.mark.parametrize("content", [b"\xff\xfeinvalid", b"{partial", b"[]"])
@pytest.mark.parametrize("prefix", ["r-", "app-run-"])
def test_newer_unreadable_manifest_does_not_hide_older_readable_run(tmp_path, monkeypatch, content, prefix):
    good = tmp_path / "r-older" / "queue-groups" / "g1" / "resume-embedding-manifest.json"
    bad = tmp_path / (prefix + "newer") / "resume-embedding-manifest.json"
    good.parent.mkdir(parents=True)
    bad.parent.mkdir(parents=True)
    payload = {"workspace_slug": "academic", "recovery": {"state": "observation_required"}}
    good.write_text(json.dumps(payload), encoding="utf8")
    bad.write_bytes(content)
    os.utime(good, (1, 1))
    os.utime(bad, (2, 2))
    monkeypatch.setattr(app, "AUTO_RUN_STATE_DIR", tmp_path)
    assert app.latest_resume_manifest("academic") == (good, payload)
    assert bad.read_bytes() == content
    assert json.loads(good.read_text(encoding="utf8")) == payload


def test_nested_manifest_filters_workspace_and_retains_observation_only(tmp_path, monkeypatch):
    expected = None
    for number, (workspace, state) in enumerate([
        ("academic", "observation_required"), ("other", "resume_available"),
        ("academic", "complete"),
    ]):
        path = tmp_path / f"r-{number}" / "queue-groups" / "g1" / "resume-embedding-manifest.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"workspace_slug": workspace, "recovery": {"state": state}}))
        os.utime(path, (number + 1, number + 1))
        if number == 0:
            expected = path
    monkeypatch.setattr(app, "AUTO_RUN_STATE_DIR", tmp_path)
    found, payload = app.latest_resume_manifest("academic")
    assert found == expected
    assert payload["recovery"]["state"] == "observation_required"

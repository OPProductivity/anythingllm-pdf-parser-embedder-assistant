"""The production upload must use exact socket ownership before credentials leave."""

import json
from unittest import mock

import pytest

import auto_anythingllm_pipeline as pipeline


pytestmark = pytest.mark.offline_deterministic


def test_failed_desktop_socket_proof_is_a_known_pre_send_hold():
    outcome = pipeline.classify_anythingllm_mutation_outcome(
        stage="document_attachment",
        exception=pipeline.DesktopSocketIdentityError("no credential was sent"),
    )
    assert outcome["classification"] == "desktop_socket_identity_unavailable_before_send"
    assert outcome["may_continue_later_sources"] is False
    assert pipeline.mutation_outcome_receipt_state(outcome) == "global_hold"


def test_authenticated_api_uses_connected_socket_guard_without_listener_precheck(monkeypatch):
    request = pipeline.urllib.request.Request(
        "http://127.0.0.1:3001/api/v1/document/upload",
        data=b"prepared-record",
        headers={"Authorization": "Bearer test-key"},
    )
    opener = mock.Mock()
    opener.open.return_value = object()
    monkeypatch.setattr(pipeline, "authenticated_opener", lambda _request: opener)
    monkeypatch.setattr(pipeline, "desktop_listener", mock.Mock(side_effect=AssertionError("precheck")))

    assert pipeline._api_urlopen(request, 3) is opener.open.return_value
    opener.open.assert_called_once_with(request, timeout=3)


def test_file_upload_persists_pre_send_hold_instead_of_unknown(monkeypatch, tmp_path):
    prepared = tmp_path / "page-parent.txt"
    prepared.write_text("Prepared page text", encoding="utf-8")
    receipts = tmp_path / "receipts.jsonl"
    monkeypatch.setattr(
        pipeline, "post_multipart_form",
        mock.Mock(side_effect=pipeline.DesktopSocketIdentityError("no credential was sent")),
    )

    report = pipeline.maybe_upload_segment_files(
        "http://127.0.0.1:3001", "fixture-key",
        [{
            "filename": prepared.name,
            "title": "Prepared page",
            "docSource": "local-pdf://sha256/" + "a" * 64,
            "chunkSource": "page-parent://fixture",
            "text_file": str(prepared),
            "_automatic_source_path": "fixture.pdf",
        }],
        workspace_slug="fixture-workspace",
        storage_dir=tmp_path,
        submission_receipt_path=receipts,
    )

    assert report["attachment_results"][0]["status"] == "global_hold"
    assert report["errors"][0]["classification"] == "desktop_socket_identity_unavailable_before_send"
    assert json.loads(receipts.read_text(encoding="utf-8").splitlines()[-1])["state"] == "global_hold"

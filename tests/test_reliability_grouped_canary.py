import json

import pytest

import reliability_grouped_canary as canary


pytestmark = pytest.mark.offline_deterministic


def test_grouped_canary_requires_multiple_copied_pdfs(tmp_path):
    source = tmp_path / "one.pdf"
    source.write_bytes(b"pdf")
    with pytest.raises(ValueError, match="2-1000"):
        canary.run_grouped_live_canary([source], tmp_path / "output")


def test_grouped_canary_rejects_more_than_the_product_picker_limit(tmp_path):
    source = tmp_path / "one.pdf"
    source.write_bytes(b"pdf")
    with pytest.raises(ValueError, match="2-1000"):
        canary.run_grouped_live_canary(
            [source] * (canary.MAX_CANARY_PDFS + 1),
            tmp_path / "output",
        )


def test_grouped_canary_requires_exact_source_transactions_before_cleanup(tmp_path, monkeypatch):
    first = tmp_path / "one.pdf"
    second = tmp_path / "two.pdf"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    monkeypatch.setattr(
        canary.pipeline,
        "create_validation_workspace",
        lambda *_args, **_kwargs: {
            "status": "created", "workspace_slug": "canary-workspace", "workspace_name": "Canary",
        },
    )
    monkeypatch.setattr(canary.pipeline, "default_anythingllm_storage_dir", lambda: tmp_path)
    monkeypatch.setattr(
        canary.pipeline,
        "delete_validation_workspace",
        lambda *_args, **_kwargs: {
            "status": "deleted", "error": "",
            "document_folder_cleanup": {"status": "deleted"},
        },
    )
    monkeypatch.setattr(
        canary.app,
        "fresh_automatic_run_setting_values",
        lambda: {},
    )
    monkeypatch.setattr(
        canary.app,
        "automatic_ocr_preflight_manifest",
        lambda *_args, **_kwargs: {"files": []},
    )

    def fake_run_automatic(**settings):
        root = canary.Path(settings["run_root_override"])
        root.mkdir(parents=True)
        (root / "run-progress.json").write_text(
            json.dumps({"state": "successful"}), encoding="utf-8",
        )
        (root / "batch-native-upload-report.json").write_text(
            json.dumps({
                "status": "complete", "uploaded": 2, "embedded": 2,
                "document_folder_path": str(tmp_path / "managed"),
                "document_results": {
                    str(first.resolve()): {"status": "complete", "searchability_proven": True},
                    str(second.resolve()): {"status": "complete", "searchability_proven": True},
                },
            }),
            encoding="utf-8",
        )
        (root / "source-transaction-ledger.json").write_text(
            json.dumps({
                "transactions": [
                    {"state": "exact_vectors_proven"},
                    {"state": "exact_vectors_proven"},
                ]
            }),
            encoding="utf-8",
        )

    monkeypatch.setattr(canary.app, "run_automatic", fake_run_automatic)
    monkeypatch.setattr(canary, "audit_run_directory", lambda _root: {"audit_status": "pass"})

    result = canary.run_grouped_live_canary(
        [first, second], tmp_path / "result",
    )

    assert result["status"] == "pass"
    assert result["source_states"] == ["exact_vectors_proven", "exact_vectors_proven"]
    assert result["cleanup_status"] == "deleted"
    assert result["batch_scale"] == "small"


def test_grouped_canary_counts_explicit_exact_selection_duplicates(tmp_path, monkeypatch):
    first = tmp_path / "one.pdf"
    duplicate = tmp_path / "copy.pdf"
    first.write_bytes(b"same")
    duplicate.write_bytes(b"same")
    monkeypatch.setattr(
        canary.pipeline,
        "create_validation_workspace",
        lambda *_args, **_kwargs: {
            "status": "created", "workspace_slug": "canary-workspace", "workspace_name": "Canary",
        },
    )
    monkeypatch.setattr(canary.pipeline, "default_anythingllm_storage_dir", lambda: tmp_path)
    monkeypatch.setattr(
        canary.pipeline,
        "delete_validation_workspace",
        lambda *_args, **_kwargs: {
            "status": "deleted", "error": "",
            "document_folder_cleanup": {"status": "deleted"},
        },
    )
    monkeypatch.setattr(canary.app, "fresh_automatic_run_setting_values", lambda: {})
    monkeypatch.setattr(
        canary.app,
        "automatic_ocr_preflight_manifest",
        lambda *_args, **_kwargs: {"files": []},
    )

    def fake_run_automatic(**settings):
        root = canary.Path(settings["run_root_override"])
        root.mkdir(parents=True)
        (root / "run-progress.json").write_text(
            json.dumps({"state": "successful"}), encoding="utf-8",
        )
        (root / "batch-native-upload-report.json").write_text(
            json.dumps({
                "status": "complete", "uploaded": 1, "embedded": 1,
                "document_folder_path": str(tmp_path / "managed"),
                "selected_input_exact_duplicates": [
                    {"source_path": str(duplicate), "duplicate_of": str(first)},
                ],
                "document_results": {
                    str(first.resolve()): {"status": "complete", "searchability_proven": True},
                    str(duplicate.resolve()): {"status": "skipped_exact_duplicate"},
                },
            }),
            encoding="utf-8",
        )
        (root / "source-transaction-ledger.json").write_text(
            json.dumps({"transactions": [{"state": "exact_vectors_proven"}]}),
            encoding="utf-8",
        )

    monkeypatch.setattr(canary.app, "run_automatic", fake_run_automatic)
    monkeypatch.setattr(canary, "audit_run_directory", lambda _root: {"audit_status": "pass"})

    result = canary.run_grouped_live_canary([first, duplicate], tmp_path / "result")

    assert result["status"] == "pass"
    assert result["selected_exact_duplicate_count"] == 1
    assert result["accounted_source_count"] == 2

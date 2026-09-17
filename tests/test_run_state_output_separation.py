import json

import pytest

import auto_anythingllm_pipeline as pipeline
import portable_paths
import rag_pdf_gradio_app as app


pytestmark = pytest.mark.offline_deterministic


def test_application_paths_separate_outputs_from_run_state(tmp_path, monkeypatch):
    monkeypatch.setenv(
        portable_paths.DATA_DIRECTORY_ENVIRONMENT_VARIABLE, str(tmp_path)
    )
    paths = portable_paths.ensure_application_directories()

    assert paths["automatic_outputs"] == tmp_path / "outputs" / "automatic-runs"
    assert paths["interactive_outputs"] == tmp_path / "outputs" / "interactive-runs"
    assert paths["automatic_run_state"] == tmp_path / "run-state" / "automatic-runs"
    assert paths["interactive_run_state"] == tmp_path / "run-state" / "interactive-runs"
    assert all(
        paths[key].is_dir()
        for key in (
            "automatic_outputs",
            "interactive_outputs",
            "automatic_run_state",
            "interactive_run_state",
        )
    )


def test_publication_keeps_user_output_flat_and_text_only(tmp_path):
    output_root = tmp_path / "outputs" / "automatic-runs"
    run_root = tmp_path / "run-state" / "automatic-runs" / "r-staging"
    document_root = run_root / "document-one"
    document_root.mkdir(parents=True)
    transcript = document_root / "internal-complete-pdf-parsed.txt"
    segment = document_root / "internal-p001-s01.txt"
    transcript.write_text("complete text", encoding="utf-8")
    segment.write_text("page text", encoding="utf-8")
    (run_root / "run-progress.json").write_text("{}", encoding="utf-8")
    summary_receipt = document_root / "run-summary.json"
    summary_receipt.write_text(
        json.dumps(
            {
                "output_root": str(document_root),
                "upload_file": str(transcript),
                "lean_retention": {"prepared_text": str(transcript)},
            }
        ),
        encoding="utf-8",
    )
    (document_root / "private-recovery-payload.txt").write_text(
        "private copy", encoding="utf-8"
    )
    summaries = [
        {
            "pdf": str(tmp_path / "Source One.pdf"),
            "output_root": str(document_root),
            "upload_file": str(transcript),
            "source_sha256": "a" * 64,
        }
    ]

    published = app.promote_flat_no_logs_batch_output(
        output_root, run_root, [], summaries
    )
    app.finalize_published_text_separation(run_root, summaries)
    stored = json.loads(summary_receipt.read_text(encoding="utf-8"))
    app.finalize_published_text_separation(run_root, [stored])

    assert published.parent == output_root
    assert published.name == run_root.name
    assert sorted(path.suffix for path in published.iterdir()) == [".txt", ".txt"]
    assert all(path.is_file() for path in published.iterdir())
    assert not list(run_root.rglob("*.txt"))
    stored = json.loads(summary_receipt.read_text(encoding="utf-8"))
    assert stored["upload_file"] == summaries[0]["upload_file"]
    assert stored["published_output_directory"] == str(published)
    assert (run_root / "run-progress.json").is_file()
    assert (document_root / "run-summary.json").is_file()


def test_regular_lean_run_is_publishable_from_private_state(tmp_path):
    source = tmp_path / "run-state" / "r-one" / "paper" / "paper.txt"
    source.parent.mkdir(parents=True)
    source.write_text("text", encoding="utf-8")
    assert app.automatic_text_outputs_ready(
        [
            {
                "pdf": str(tmp_path / "paper.pdf"),
                "upload_file": str(source),
                "source_sha256": "b" * 64,
                "lean_retention": {"applied": True, "policy": "lean_success_v1"},
            }
        ]
    )


def test_cli_publication_keeps_state_receipts_out_of_outputs(tmp_path):
    state_root = tmp_path / "run-state" / "automatic-runs" / "run-state-one"
    document_root = state_root / "paper"
    document_root.mkdir(parents=True)
    transcript = document_root / "paper.txt"
    page = document_root / "paper-p001-s01.txt"
    transcript.write_text("complete", encoding="utf-8")
    page.write_text("page one", encoding="utf-8")
    (document_root / "private-recovery-payload.txt").write_text(
        "private copy", encoding="utf-8"
    )
    (document_root / "run-summary.json").write_text("{}", encoding="utf-8")

    published_root, published = pipeline.publish_cli_text_outputs(
        tmp_path / "outputs" / "automatic-runs",
        state_root,
        [{"pdf": str(tmp_path / "Paper.pdf"), "upload_file": str(transcript)}],
    )

    assert published_root is not None
    assert published_root.name == state_root.name
    assert len(published) == 2
    assert all(
        path.is_file() and path.suffix == ".txt" for path in published_root.iterdir()
    )
    assert not list(state_root.rglob("*.txt"))
    assert (document_root / "run-summary.json").is_file()


def test_warning_runs_are_not_excluded_from_text_publication_gate():
    import inspect

    code = inspect.getsource(app.run_automatic)
    assert "if text_outputs_ready:" in code
    assert 'completion["state"] == "successful" and automatic_text_outputs_ready' not in code

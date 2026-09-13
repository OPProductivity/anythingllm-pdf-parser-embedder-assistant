from pathlib import Path

import pytest

import rag_pdf_gradio_app as app

pytestmark = pytest.mark.offline_deterministic


def prepared(root, index, name="Paper.pdf", snippets=False):
    folder = root / str(index)
    folder.mkdir(parents=True)
    text = folder / "internal-0123456789-complete-pdf-parsed.txt"
    text.write_text(f"Café Müller: prepared source {index}\n", encoding="utf-8")
    (folder / "pdf-input-preflight.json").write_text("{}")
    if snippets:
        (folder / "internal-0123456789-p001-s01.txt").write_text("first passage")
    return {
        "pdf": str(root / "inputs" / str(index) / name),
        "source_sha256": f"{index:064x}",
        "upload_file": str(text),
        "api_upload_status": "skipped_prepare_only",
        "lean_retention": {"applied": True, "policy": "flat_local_no_logs_v1"},
    }


def duplicate(row, name="Alias.pdf"):
    return {
        "pdf": str(Path(row["pdf"]).with_name(name)),
        "source_sha256": row["source_sha256"],
        "api_upload_status": "skipped_exact_duplicate",
        "selected_input_duplicate_of": row["pdf"],
    }


@pytest.mark.parametrize("snippets", [False, True])
def test_duplicate_exports_flat_without_reparsing_or_receipts(tmp_path, snippets):
    staging = tmp_path / "staging"
    first = prepared(staging, 1, snippets=snippets)
    alias = duplicate(first)
    rows = [first, alias]
    source = Path(first["upload_file"])
    assert app.local_export_retention_complete(rows)
    result = app.promote_flat_no_logs_batch_output(tmp_path, staging, [], rows)
    assert len(list(result.iterdir())) == (4 if snippets else 2)
    assert all(p.is_file() and p.suffix == ".txt" for p in result.iterdir())
    assert Path(first["upload_file"]).name == "Paper-complete-pdf-parsed.txt"
    assert Path(alias["upload_file"]).name == "Alias-complete-pdf-parsed-(duplicate).txt"
    if snippets:
        assert (result / "Alias-p001-s01-(duplicate).txt").is_file()
    assert Path(alias["upload_file"]).read_bytes() == source.read_bytes()
    assert len(app.primary_prepared_download_paths(rows)) == 2
    assert source.exists()  # caller removes staging only after success
    assert (source.parent / "pdf-input-preflight.json").exists()


@pytest.mark.parametrize("defect", ["hash", "missing", "unready", "chain"])
def test_duplicate_cannot_hide_missing_or_unready_canonical(tmp_path, defect):
    first = prepared(tmp_path / "staging", 1)
    alias = duplicate(first)
    if defect == "hash":
        alias["source_sha256"] = "different"
    elif defect == "missing":
        alias["selected_input_duplicate_of"] = "missing.pdf"
    elif defect == "unready":
        first["lean_retention"] = {"applied": False}
    else:
        first["api_upload_status"] = "skipped_exact_duplicate"
    assert not app.local_export_retention_complete([first, alias])


@pytest.mark.parametrize("names", [
    ["Paper.pdf", "paper.pdf", "Paper-2.pdf"],
    ["Title & subtitle.pdf", "Title + subtitle.pdf"],
    ["VeryLongTitle" * 17 + "A.pdf", "VeryLongTitle" * 17 + "B.pdf"],
])
def test_friendly_names_handle_collisions_and_windows_paths(tmp_path, names):
    staging = tmp_path / "staging"
    rows = [prepared(staging, i, name) for i, name in enumerate(names)]
    original_bytes = [Path(r["upload_file"]).read_bytes() for r in rows]
    result = app.promote_flat_no_logs_batch_output(tmp_path, staging, [], rows)
    assert len(list(result.iterdir())) == len(names)
    assert len({p.name.casefold() for p in result.iterdir()}) == len(names)
    assert all(len(str(p)) <= 250 for p in result.iterdir())
    assert [Path(r["upload_file"]).read_bytes() for r in rows] == original_bytes


def test_failed_copy_preserves_all_staging_and_summary_paths(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    rows = [prepared(staging, i) for i in (1, 2)]
    originals = [r["upload_file"] for r in rows]
    actual_copy = app.shutil.copyfileobj
    calls = 0

    def fail_second(reader, writer):
        nonlocal calls
        calls += 1
        if calls == 2:
            writer.write(b"partial")
            raise OSError("simulated disk write failure")
        return actual_copy(reader, writer)

    monkeypatch.setattr(app.shutil, "copyfileobj", fail_second)
    with pytest.raises(OSError, match="simulated"):
        app.promote_flat_no_logs_batch_output(tmp_path, staging, [], rows)
    assert [r["upload_file"] for r in rows] == originals
    assert all(Path(p).is_file() for p in originals)
    assert list(tmp_path.iterdir()) == [staging]


def test_missing_text_never_promotes(tmp_path):
    staging = tmp_path / "staging"
    row = prepared(staging, 1)
    Path(row["upload_file"]).unlink()
    with pytest.raises(FileNotFoundError):
        app.promote_flat_no_logs_batch_output(tmp_path, staging, [], [row])
    assert list(tmp_path.iterdir()) == [staging]


def test_long_duplicate_collision_keeps_marker_at_end(tmp_path):
    staging = tmp_path / "staging"
    first = prepared(staging, 1)
    alias = duplicate(first, "LongTitle" * 30 + ".pdf")
    another = dict(alias, pdf=str(staging / "other" / Path(alias["pdf"]).name))
    rows = [first, alias, another]
    result = app.promote_flat_no_logs_batch_output(tmp_path, staging, [], rows)
    assert len(list(result.iterdir())) == 3
    assert alias["upload_file"] != another["upload_file"]
    for row in (alias, another):
        file = Path(row["upload_file"])
        assert file.name.endswith("-(duplicate).txt")
        assert len(str(file)) <= 250
        assert file.read_bytes() == Path(first["upload_file"]).read_bytes()


def test_parent_gate_is_retention_based_not_selected_count():
    import inspect
    code = inspect.getsource(app.run_automatic)
    assert "automatic_text_outputs_ready(summaries)" in code
    assert "len(flat_no_logs_exports) == len(summaries)" not in code
    assert "text-only output publication is incomplete" in code

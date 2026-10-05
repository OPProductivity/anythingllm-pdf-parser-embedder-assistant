import pytest
import shutil

import portable_paths


pytestmark = pytest.mark.offline_deterministic


def test_package_resource_path_supports_pip_target_layout(tmp_path, monkeypatch):
    module_root = tmp_path / "target"
    module_root.mkdir()
    fake_module = module_root / "portable_paths.py"
    fake_module.write_text("# installed module", encoding="utf-8")
    resource = (
        module_root
        / "share"
        / "anythingllm-pdf-assistant"
        / "VERSION"
    )
    resource.parent.mkdir(parents=True)
    resource.write_text("0.5.1", encoding="utf-8")
    monkeypatch.setattr(portable_paths, "__file__", str(fake_module))
    monkeypatch.setattr(
        portable_paths.sysconfig,
        "get_path",
        lambda _name: str(tmp_path / "unrelated-interpreter-data"),
    )

    assert portable_paths.package_resource_path("VERSION") == resource


def test_package_resource_path_rejects_missing_resource(tmp_path, monkeypatch):
    fake_module = tmp_path / "target" / "portable_paths.py"
    fake_module.parent.mkdir()
    fake_module.write_text("# installed module", encoding="utf-8")
    monkeypatch.setattr(portable_paths, "__file__", str(fake_module))
    monkeypatch.setattr(
        portable_paths.sysconfig,
        "get_path",
        lambda _name: str(tmp_path / "unrelated-interpreter-data"),
    )

    with pytest.raises(FileNotFoundError, match="Required package resource"):
        portable_paths.package_resource_path("missing.txt")


def test_target_installed_annotated_model_uses_the_verified_packaged_asset(tmp_path, monkeypatch):
    import rag_pdf_tools as tools

    relative = "assets/tessdata-annotated/eng.traineddata"
    original = portable_paths.package_resource_path(relative)
    module = tmp_path / "target"
    resource = module / "share/anythingllm-pdf-assistant" / relative
    resource.parent.mkdir(parents=True)
    shutil.copyfile(original, resource)
    monkeypatch.setattr(portable_paths, "__file__", str(module / "portable_paths.py"))
    monkeypatch.setattr(tools, "__file__", str(module / "rag_pdf_tools.py"))
    monkeypatch.setattr(tools.sys, "prefix", str(tmp_path / "unrelated-interpreter"))
    monkeypatch.setattr(portable_paths.sysconfig, "get_path", lambda _: str(tmp_path / "unrelated-data"))

    assert tools.annotated_model_arguments(4, 6) == ["--tessdata-dir", str(resource.parent), "--oem", "1"]
    assert tools.annotated_model_arguments(4, 4) == []

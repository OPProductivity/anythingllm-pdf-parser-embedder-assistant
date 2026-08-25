import pytest

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

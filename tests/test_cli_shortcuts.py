from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

import anythingllm_pdf_assistant_cli as cli


def test_shortcut_arguments_start_the_packaged_module():
    assert "anythingllm_pdf_assistant_cli start --browser" in cli._shortcut_arguments("start")
    assert "anythingllm_pdf_assistant_cli stop" in cli._shortcut_arguments("stop")
    assert "-WindowStyle Minimized" in cli._shortcut_arguments("start")


def test_shortcut_install_uses_start_and_stop_icons_for_current_desktop():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        desktop = root / "Desktop"
        start_icon = root / "start.ico"
        stop_icon = root / "stop.ico"
        start_icon.touch()
        stop_icon.touch()
        writes = []

        def resource_path(relative_path: str) -> Path:
            return start_icon if relative_path.endswith("start.ico") else stop_icon

        with (
            mock.patch.object(cli.sys, "platform", "win32"),
            mock.patch.object(cli, "_desktop_directory", return_value=desktop),
            mock.patch.object(cli, "package_resource_path", side_effect=resource_path),
            mock.patch.object(cli, "_write_windows_shortcut", side_effect=lambda *args: writes.append(args)),
        ):
            paths = cli.install_desktop_shortcuts(overwrite=True)

    assert paths == (desktop / cli.START_SHORTCUT_NAME, desktop / cli.STOP_SHORTCUT_NAME)
    assert [entry[0].name for entry in writes] == [cli.START_SHORTCUT_NAME, cli.STOP_SHORTCUT_NAME]
    assert writes[0][2] == start_icon
    assert writes[1][2] == stop_icon


def test_stop_refuses_a_recycled_or_unowned_process():
    with tempfile.TemporaryDirectory() as tmpdir:
        marker = Path(tmpdir) / "localhost-server.json"
        marker.write_text('{"pid": 42}', encoding="utf-8")
        process_result = mock.Mock(returncode=0, stdout="python unrelated_job.py", stderr="")
        with (
            mock.patch.object(cli.sys, "platform", "win32"),
            mock.patch.object(cli, "_server_marker_path", return_value=marker),
            mock.patch.object(cli, "_powershell", return_value="powershell.exe"),
            mock.patch.object(cli.subprocess, "run", return_value=process_result) as run,
        ):
            assert cli._stop() == 1

    assert run.call_count == 1

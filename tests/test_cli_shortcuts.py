from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

import anythingllm_pdf_assistant_cli as cli


def test_shortcut_arguments_start_the_packaged_module():
    start_arguments = cli._shortcut_arguments("start")
    assert "anythingllm_pdf_assistant_cli', 'start', '--browser" in start_arguments
    assert "Start-Process" in start_arguments
    assert "-WindowStyle Hidden" in start_arguments
    assert "-ArgumentList @('-m', 'anythingllm_pdf_assistant_cli', 'start', '--browser')" in start_arguments
    assert "-WorkingDirectory" in start_arguments
    stop_arguments = cli._shortcut_arguments("stop")
    assert "anythingllm_pdf_assistant_cli stop" in stop_arguments
    assert "Start-Sleep -Seconds 4" in stop_arguments
    assert "-WindowStyle Hidden" in start_arguments


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


def test_shortcut_writer_uses_module_directory_not_application_data_as_working_directory(tmp_path: Path):
    shortcut = tmp_path / "Start AnythingLLM PDF Assistant.lnk"
    icon = tmp_path / "start.ico"
    icon.touch()
    captured_environment = {}

    def fake_run(_command, **kwargs):
        captured_environment.update(kwargs["env"])
        shortcut.touch()
        return mock.Mock(returncode=0, stdout="", stderr="")

    with (
        mock.patch.object(cli, "_powershell", return_value="powershell.exe"),
        mock.patch.object(cli.subprocess, "run", side_effect=fake_run),
    ):
        cli._write_windows_shortcut(shortcut, "-Command test", icon, "test")

    assert captured_environment["ANYTHINGLLM_SHORTCUT_WORKING_DIRECTORY"] == str(
        Path(cli.__file__).resolve().parent
    )


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


def test_stop_queries_the_exact_owned_pid_with_one_wmi_filter_expression():
    with tempfile.TemporaryDirectory() as tmpdir:
        marker = Path(tmpdir) / "localhost-server.json"
        marker.write_text('{"pid": 42}', encoding="utf-8")
        process_result = mock.Mock(
            returncode=0,
            stdout="python.exe -m anythingllm_pdf_assistant_cli start --browser",
            stderr="",
        )
        stop_result = mock.Mock(returncode=0, stdout="SUCCESS", stderr="")
        with (
            mock.patch.object(cli.sys, "platform", "win32"),
            mock.patch.object(cli, "_server_marker_path", return_value=marker),
            mock.patch.object(cli, "_powershell", return_value="powershell.exe"),
            mock.patch.object(cli.subprocess, "run", side_effect=[process_result, stop_result]) as run,
        ):
            assert cli._stop() == 0

    probe_command = run.call_args_list[0].args[0][-1]
    assert "-Filter ('ProcessId = ' + $env:ANYTHINGLLM_SERVER_PID)" in probe_command

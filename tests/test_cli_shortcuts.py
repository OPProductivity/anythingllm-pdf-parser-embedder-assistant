from __future__ import annotations

import json
import subprocess
import tempfile
import threading
from pathlib import Path
from unittest import mock

import pytest

import anythingllm_pdf_assistant_cli as cli


pytestmark = pytest.mark.offline_deterministic


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


def test_shortcut_writer_reports_a_bounded_powershell_timeout(tmp_path: Path):
    shortcut = tmp_path / "Start AnythingLLM PDF Assistant.lnk"
    icon = tmp_path / "start.ico"
    icon.touch()

    with (
        mock.patch.object(cli, "_powershell", return_value="powershell.exe"),
        mock.patch.object(
            cli.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["powershell.exe"], cli.POWERSHELL_COMMAND_TIMEOUT_SECONDS),
        ),
    ):
        try:
            cli._write_windows_shortcut(shortcut, "-Command test", icon, "test")
        except RuntimeError as exc:
            assert "timeout" in str(exc).casefold()
        else:
            raise AssertionError("A timed-out PowerShell shortcut write must be reported.")


def test_stop_refuses_a_recycled_or_unowned_process():
    with tempfile.TemporaryDirectory() as tmpdir:
        marker = Path(tmpdir) / "localhost-server.json"
        marker.write_text('{"pid": 42, "port": 7860}', encoding="utf-8")
        process_result = mock.Mock(returncode=0, stdout="python unrelated_job.py", stderr="")
        with (
            mock.patch.object(cli.sys, "platform", "win32"),
            mock.patch.object(cli, "_server_marker_path", return_value=marker),
            mock.patch.object(cli, "_powershell", return_value="powershell.exe"),
            mock.patch.object(cli, "_port_is_available", return_value=False),
            mock.patch.object(cli.subprocess, "run", return_value=process_result) as run,
        ):
            assert cli._stop() == 1

    assert run.call_count == 1


def test_stop_removes_a_stale_marker_when_its_port_is_free(capsys):
    with tempfile.TemporaryDirectory() as tmpdir:
        marker = Path(tmpdir) / "localhost-server.json"
        marker.write_text('{"pid": 42, "port": 7860}', encoding="utf-8")
        with (
            mock.patch.object(cli.sys, "platform", "win32"),
            mock.patch.object(cli, "_server_marker_path", return_value=marker),
            mock.patch.object(cli, "_owned_server_process_command", return_value=""),
            mock.patch.object(cli, "_port_is_available", return_value=True),
        ):
            assert cli._stop() == 0
        assert not marker.exists()
    assert "stale" in capsys.readouterr().out.casefold()


def test_stop_terminates_only_a_verified_owned_server_tree():
    with tempfile.TemporaryDirectory() as tmpdir:
        marker = Path(tmpdir) / "localhost-server.json"
        marker.write_text('{"pid": 42, "port": 7860}', encoding="utf-8")
        process_result = mock.Mock(
            returncode=0,
            stdout="python.exe -m anythingllm_pdf_assistant_cli start --browser",
            stderr="",
        )
        stop_result = mock.Mock(returncode=0, stdout="SUCCESS", stderr="")
        with (
            mock.patch.object(cli.sys, "platform", "win32"),
            mock.patch.object(cli, "_server_marker_path", return_value=marker),
            mock.patch.object(cli, "_owned_server_process_command", return_value=process_result.stdout),
            mock.patch.object(cli, "_listener_belongs_to_server_root", return_value=True),
            mock.patch.object(cli, "_port_is_available", return_value=True),
            mock.patch.object(cli.subprocess, "run", return_value=stop_result) as run,
        ):
            assert cli._stop() == 0

    assert run.call_args.args[0] == ["taskkill", "/PID", "42", "/T", "/F"]


def test_stop_retains_owned_run_cancellation_before_terminating_server_tree(tmp_path: Path):
    marker = tmp_path / "localhost-server.json"
    marker.write_text('{"root_pid": 42, "port": 7860}', encoding="utf-8")
    outputs = tmp_path / "automatic-runs"
    run_root = outputs / "r-owned"
    run_root.mkdir(parents=True)
    progress_path = run_root / cli.AUTOMATIC_RUN_PROGRESS_NAME
    progress_path.write_text(
        json.dumps(
            {
                "state": "running",
                "server_root_pid": 42,
                "phase": "AnythingLLM queue",
                "confirmed_fraction": 0.42,
            }
        ),
        encoding="utf-8",
    )
    foreign_root = outputs / "r-foreign"
    foreign_root.mkdir()
    (foreign_root / cli.AUTOMATIC_RUN_PROGRESS_NAME).write_text(
        json.dumps({"state": "running", "server_root_pid": 99}), encoding="utf-8"
    )
    stop_result = mock.Mock(returncode=0, stdout="SUCCESS", stderr="")
    with (
        mock.patch.object(cli.sys, "platform", "win32"),
        mock.patch.object(cli, "_server_marker_path", return_value=marker),
        mock.patch.object(cli, "_owned_server_process_command", return_value="python -m anythingllm_pdf_assistant_cli start"),
        mock.patch.object(cli, "_listener_belongs_to_server_root", return_value=True),
        mock.patch.object(cli, "_port_is_available", return_value=True),
        mock.patch.object(cli, "application_paths", return_value={"automatic_outputs": outputs}),
        mock.patch.object(cli.subprocess, "run", return_value=stop_result),
    ):
        assert cli._stop() == 0

    terminal = json.loads(progress_path.read_text(encoding="utf-8"))
    recovery = json.loads((run_root / cli.AUTOMATIC_RUN_CANCELLATION_RECOVERY).read_text(encoding="utf-8"))
    assert terminal["state"] == "cancelled"
    assert terminal["cancel_requested"] is True
    assert recovery["status"] == "cancelled"
    assert recovery["reason"] == "owned_local_server_stop"
    assert (run_root / cli.AUTOMATIC_RUN_CANCELLATION_MARKER).is_file()
    assert json.loads((foreign_root / cli.AUTOMATIC_RUN_PROGRESS_NAME).read_text(encoding="utf-8"))["state"] == "running"


def test_server_stop_refuses_to_kill_when_owned_recovery_cannot_be_written(tmp_path: Path, capsys):
    marker = tmp_path / "localhost-server.json"
    marker.write_text('{"root_pid": 42, "port": 7860}', encoding="utf-8")
    with (
        mock.patch.object(cli.sys, "platform", "win32"),
        mock.patch.object(cli, "_server_marker_path", return_value=marker),
        mock.patch.object(cli, "_owned_server_process_command", return_value="python -m anythingllm_pdf_assistant_cli start"),
        mock.patch.object(cli, "_listener_belongs_to_server_root", return_value=True),
        mock.patch.object(cli, "_prepare_owned_active_runs_for_server_stop", side_effect=OSError("disk unavailable")),
        mock.patch.object(cli.subprocess, "run") as run,
    ):
        assert cli._stop() == 1
    assert run.call_count == 0
    assert "cancellation recovery" in capsys.readouterr().err


def test_stop_keeps_the_marker_when_ownership_probe_times_out(capsys):
    with tempfile.TemporaryDirectory() as tmpdir:
        marker = Path(tmpdir) / "localhost-server.json"
        marker.write_text('{"pid": 42, "port": 7860}', encoding="utf-8")
        with (
            mock.patch.object(cli.sys, "platform", "win32"),
            mock.patch.object(cli, "_server_marker_path", return_value=marker),
            mock.patch.object(cli, "_powershell", return_value="powershell.exe"),
            mock.patch.object(
                cli.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(["powershell.exe"], cli.POWERSHELL_COMMAND_TIMEOUT_SECONDS),
            ),
        ):
            assert cli._stop() == 1

        assert marker.exists()
    assert "refusing to stop" in capsys.readouterr().err


def test_server_marker_preserves_the_launch_root_for_a_gradio_listener_child(tmp_path: Path, monkeypatch):
    marker = tmp_path / "localhost-server.json"
    monkeypatch.setenv(cli.SERVER_ROOT_PID_ENV, "5678")
    with mock.patch.object(cli, "_server_marker_path", return_value=marker):
        cli._write_server_marker(7860)
    record = json.loads(marker.read_text(encoding="utf-8"))
    assert record["pid"] == 5678
    assert record["root_pid"] == 5678


def test_recorded_server_requires_a_listener_from_its_owned_process_tree(tmp_path: Path):
    marker = tmp_path / "localhost-server.json"
    marker.write_text(json.dumps({"root_pid": 42, "port": 7860}), encoding="utf-8")
    with (
        mock.patch.object(cli, "_server_marker_path", return_value=marker),
        mock.patch.object(cli, "_owned_server_process_command", return_value="python -m anythingllm_pdf_assistant_cli start"),
        mock.patch.object(cli, "_listener_belongs_to_server_root", return_value=False),
    ):
        assert not cli._recorded_server_is_alive_on_port(7860)


def test_server_marker_stays_valid_under_overlapping_start_writes(tmp_path: Path):
    marker = tmp_path / "localhost-server.json"
    start = threading.Event()
    writers = [
        threading.Thread(target=lambda port=port: (start.wait(), cli._write_server_marker(port)))
        for port in range(7900, 7912)
    ]
    with mock.patch.object(cli, "_server_marker_path", return_value=marker):
        for writer in writers:
            writer.start()
        start.set()
        for writer in writers:
            writer.join(timeout=3)

    assert all(not writer.is_alive() for writer in writers)
    record = json.loads(marker.read_text(encoding="utf-8"))
    assert record["port"] in range(7900, 7912)
    assert record["command"] == "anythingllm-pdf-assistant start"
    assert not list(tmp_path.glob(".localhost-server.json.*.tmp"))


def test_reliability_audit_command_is_explicitly_read_only_by_default(tmp_path: Path):
    with mock.patch.object(cli, "_reliability_audit_run", return_value=0) as audit:
        result = cli.main([
            "reliability",
            "audit-run",
            "--run-root",
            str(tmp_path),
            "--json",
        ])

    assert result == 0
    audit.assert_called_once_with(str(tmp_path), False, True)

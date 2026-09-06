from pathlib import Path
from unittest import mock

import pytest

import assistant_browser_launcher as launcher
import anythingllm_pdf_assistant_cli as cli

pytestmark = pytest.mark.offline_deterministic


def test_browser_launcher_ignores_uri_and_uses_fixed_owned_start():
    with (
        mock.patch.object(launcher.sys, 'argv', ['launcher.py', 'anythingllm-pdf-assistant://start?cmd=stop', '--port', '1']),
        mock.patch.object(launcher.subprocess, 'Popen') as start,
    ):
        launcher.launch()
    args, kwargs = start.call_args
    assert args[0][1:] == ['-m', 'anythingllm_pdf_assistant_cli', 'start', '--browser']
    assert Path(args[0][0]).name == 'python.exe'
    assert kwargs['cwd'] == str(Path(launcher.__file__).resolve().parent)
    assert kwargs['creationflags'] == launcher.subprocess.CREATE_NO_WINDOW
    assert not kwargs.get('shell')


def test_browser_launcher_command_dispatch():
    with mock.patch.object(cli, 'install_browser_launcher', return_value='anythingllm-pdf-assistant://start') as install:
        assert cli.main(['browser-launcher']) == 0
        install.assert_called_once()


def test_launcher_registration_is_not_done_during_start():
    with (
        mock.patch.object(cli, 'install_browser_launcher') as install,
        mock.patch.object(cli, '_start', return_value=0),
    ):
        assert cli.main(['start', '--browser']) == 0
        install.assert_not_called()


def test_registry_command_has_no_uri_substitution():
    import sys

    registry = mock.MagicMock()
    registry.OpenKey.side_effect = FileNotFoundError
    with mock.patch.dict(sys.modules, {'winreg': registry}):
        assert cli.install_browser_launcher() == 'anythingllm-pdf-assistant://start'
    values = [call.args[-1] for call in registry.SetValueEx.call_args_list]
    command = values[-1]
    assert '%1' not in command
    assert 'pythonw.exe" "' in command
    assert command.endswith('assistant_browser_launcher.py"')


def test_foreign_protocol_registration_is_not_replaced():
    import sys

    registry = mock.MagicMock()
    registry.QueryValueEx.return_value = ('another owner', 1)
    with mock.patch.dict(sys.modules, {'winreg': registry}):
        with pytest.raises(RuntimeError, match='not replaced'):
            cli.install_browser_launcher()
    registry.CreateKey.assert_not_called()

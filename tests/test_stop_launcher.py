from pathlib import Path
from unittest import mock

import pytest

import assistant_stop_launcher as launcher

pytestmark = pytest.mark.offline_deterministic


@pytest.mark.parametrize('code', [0, 1])
def test_stop_is_windowless_and_only_failure_notifies(code):
    with (
        mock.patch.object(launcher.subprocess, 'run', return_value=mock.Mock(returncode=code, stderr='ownership refused', stdout='')) as run,
        mock.patch.object(launcher, 'show_failure') as notify,
        mock.patch.object(launcher, 'show_info') as info,
    ):
        assert launcher.stop() == code
    args, kwargs = run.call_args
    assert args[0][1:] == ['-m', 'anythingllm_pdf_assistant_cli', 'stop']
    assert Path(args[0][0]).name == 'python.exe'
    assert kwargs['creationflags'] == launcher.subprocess.CREATE_NO_WINDOW
    assert kwargs['capture_output']
    assert notify.call_count == code
    info.assert_not_called()


def test_already_stopped_shows_an_information_message():
    result = mock.Mock(returncode=0, stderr='', stdout=launcher.NO_SERVER_TO_STOP_MESSAGE + '\n')
    with (
        mock.patch.object(launcher.subprocess, 'run', return_value=result),
        mock.patch.object(launcher, 'show_failure') as failure,
        mock.patch.object(launcher, 'show_info') as info,
    ):
        assert launcher.stop() == 0
    failure.assert_not_called()
    info.assert_called_once_with(launcher.NO_SERVER_TO_STOP_MESSAGE)


def test_launch_failure_is_visible():
    with (
        mock.patch.object(launcher.subprocess, 'run', side_effect=OSError('unavailable')),
        mock.patch.object(launcher, 'show_failure') as notify,
    ):
        assert launcher.stop() == 1
    assert 'unavailable' in notify.call_args.args[0]

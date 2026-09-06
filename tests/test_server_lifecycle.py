from unittest import mock
import asyncio
import json
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import anythingllm_pdf_assistant_cli as cli
from server_lifecycle import TOKEN_ENV, install_lifecycle_routes

pytestmark = pytest.mark.offline_deterministic


def test_authenticated_notification_and_cancel(monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, "test-capability")
    app = FastAPI()
    install_lifecycle_routes(app)
    headers = {"X-Assistant-Stop-Token": "test-capability"}
    with TestClient(app) as client:
        assert client.post('/assistant-lifecycle/stop_requested').status_code == 403
        assert client.post('/assistant-lifecycle/wrong', headers=headers).status_code == 400
        assert client.post('/assistant-lifecycle/stop_requested', headers=headers).status_code == 204
        assert client.post('/assistant-lifecycle/stop_cancelled', headers=headers).status_code == 204


def test_event_stream_delivers_request_and_cancel(monkeypatch):
    from starlette.requests import Request

    monkeypatch.setenv(TOKEN_ENV, 'test-capability')
    app = FastAPI()
    install_lifecycle_routes(app)
    watch = next(r.endpoint for r in app.routes if r.path == '/assistant-lifecycle')
    notify = next(r.endpoint for r in app.routes if r.path == '/assistant-lifecycle/{event}')

    async def exercise():
        response = await watch()
        stream = response.body_iterator
        assert await anext(stream) == 'data: ready\n\n'
        for event in ('stop_requested', 'stop_cancelled'):
            request = Request({'type': 'http', 'headers': [(b'x-assistant-stop-token', b'test-capability')], 'path_params': {'event': event}})
            pending = asyncio.create_task(notify(request))
            assert await anext(stream) == f'data: {event}\n\n'
            assert (await pending).status_code == 204
        await stream.aclose()
        response = await watch()
        assert await anext(response.body_iterator) == 'data: ready\n\n'
        await response.body_iterator.aclose()

    asyncio.run(exercise())


def test_missing_capability_cannot_notify(monkeypatch):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    app = FastAPI()
    install_lifecycle_routes(app)
    with TestClient(app) as client:
        assert client.post('/assistant-lifecycle/stop_requested').status_code == 403


def test_notification_failure_does_not_raise_or_use_proxy():
    with mock.patch.object(cli.urllib.request, 'build_opener') as build:
        build.return_value.open.side_effect = TimeoutError('unresponsive')
        cli._notify_browser_stop({'port': 7860, 'stop_notification_token': 'test'}, 'stop_requested')
        assert build.return_value.open.call_args.kwargs['timeout'] == 0.8


def test_legacy_marker_does_not_notify():
    with mock.patch.object(cli.urllib.request, 'build_opener') as build:
        cli._notify_browser_stop({'port': 7860}, 'stop_requested')
        build.assert_not_called()


@pytest.mark.parametrize('failure', [OSError('failed'), subprocess.TimeoutExpired('taskkill', 15), None])
def test_failed_stop_withdraws_notification(tmp_path, failure):
    marker = tmp_path / 'marker.json'
    record = {'pid': 123, 'port': 7860}
    marker.write_text(json.dumps(record))
    with (
        mock.patch.object(cli, '_listener_belongs_to_server_root', return_value=True),
        mock.patch.object(cli, '_prepare_owned_active_runs_for_server_stop', return_value=[]),
        mock.patch.object(cli, '_notify_browser_stop') as notify,
        mock.patch.object(cli.subprocess, 'run', side_effect=failure, return_value=mock.Mock(returncode=1, stderr='failed')),
    ):
        assert cli._stop_pinned_server(marker, record, 123, 7860) == 1
        assert notify.call_args_list == [mock.call(record, 'stop_requested'), mock.call(record, 'stop_cancelled')]

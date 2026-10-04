"""Exercise urllib's real redirect handling without external network traffic."""
import io
import threading
import urllib.request
from email.message import Message
from urllib.response import addinfourl
from unittest.mock import patch

import pytest
import auto_anythingllm_pipeline as pipeline

pytestmark = pytest.mark.offline_deterministic


@pytest.fixture
def web_app():
    import gradio.analytics
    # Import-time health checks are unrelated to the transport under test.
    with patch.object(urllib.request, 'urlopen', side_effect=urllib.error.URLError('offline test')), \
         patch.object(gradio.analytics, 'version_check'), \
         patch.object(gradio.analytics, 'initiated_analytics'):
        import rag_pdf_gradio_app as app
    return app


class Wire(urllib.request.BaseHandler):
    handler_order = 100

    def __init__(self, status, location):
        self.status, self.location, self.requests = status, location, []

    def http_open(self, request):
        self.requests.append(request)
        headers = Message()
        headers['Location'] = self.location
        response = addinfourl(io.BytesIO(b'{}'), headers, request.full_url, self.status)
        response.msg = 'test response'
        return response

    https_open = http_open


@pytest.fixture
def wire(monkeypatch):
    factory = urllib.request.build_opener

    def install(status, location='https://other.invalid/target'):
        transport = Wire(status, location)
        monkeypatch.setattr(urllib.request, 'build_opener', lambda *handlers: factory(transport, *handlers))
        return transport

    monkeypatch.setattr('socket.create_connection', lambda *a, **kw: pytest.fail('unexpected network request'))
    return install


@pytest.mark.parametrize('method', ['GET', 'POST', 'DELETE'])
@pytest.mark.parametrize('status', [301, 302, 303, 307, 308])
@pytest.mark.parametrize('location', ['https://other.invalid/target', '/same-origin', 'http://other.invalid/target'])
def test_no_authenticated_redirect_is_followed(wire, method, status, location):
    transport = wire(status, location)
    request = urllib.request.Request('https://original.invalid/api', method=method,
                                     headers={'Authorization': 'Bearer synthetic'})
    with pytest.raises(pipeline.AuthenticatedRedirectError) as caught:
        pipeline._api_urlopen(request, timeout=1)
    assert caught.value.code == status
    assert len(transport.requests) == 1
    assert 'synthetic' not in str(caught.value)  # pragma: allowlist secret -- dummy offline fixture


@pytest.mark.parametrize('method', ['get', 'post', 'delete'])
def test_normal_authenticated_response(wire, method):
    transport = wire(200)
    helper = getattr(pipeline, method + '_json')
    args = ({},) if method == 'post' else ()
    assert helper('https://original.invalid/api', *args, api_key='synthetic') == (200, '{}')  # pragma: allowlist secret -- dummy offline fixture
    assert len(transport.requests) == 1


@pytest.mark.parametrize('status', [301, 302, 303, 307, 308])
def test_captured_redirects_are_clear_and_not_retried(wire, status):
    transport = wire(status)
    with patch.object(pipeline.time, 'sleep', side_effect=AssertionError('must not retry')):
        response = pipeline.post_json_captured_with_retry('https://original.invalid/api', {}, api_key='synthetic')  # pragma: allowlist secret -- dummy offline fixture
    assert response['http_status'] == status
    assert 'redirect rejected' in response['error']
    assert len(transport.requests) == 1
    transport.requests.clear()
    response = pipeline.get_json_with_retry('https://original.invalid/api', api_key='synthetic',  # pragma: allowlist secret -- dummy offline fixture
                                           sleeper=lambda _: pytest.fail('must not retry'))
    assert response['http_status'] == status
    assert 'redirect rejected' in response['attempts'][0]['error']
    assert len(transport.requests) == 1


def test_observer_redirect_is_unavailable_without_reconnect(wire):
    transport = wire(302)
    states, errors = [], []
    pipeline.listen_for_anythingllm_embed_progress(
        'https://original.invalid', 'synthetic', 'workspace', [], threading.Event(),  # pragma: allowlist secret -- dummy offline fixture
        state_callback=lambda *args: states.append(args), error_callback=lambda *args: errors.append(args))
    assert len(transport.requests) == 1
    assert states[-1][0] == 'unavailable'
    assert len(errors) == 1


def test_unauthenticated_transport_is_unchanged():
    request = urllib.request.Request('https://original.invalid')
    with patch.object(urllib.request, 'urlopen', return_value='response') as original:
        assert pipeline._api_urlopen(request, timeout=3) == 'response'
        original.assert_called_once_with(request, timeout=3)


@pytest.mark.parametrize('status', [200, 302, 307, 308])
def test_multipart_uses_the_same_redirect_policy(wire, tmp_path, status):
    transport = wire(status)
    source = tmp_path / 'source.txt'
    source.write_text('Synthetic document.')
    if status == 200:
        assert pipeline.post_multipart_form('https://original.invalid/api', {}, 'file', source,
                                            api_key='synthetic') == (200, '{}')  # pragma: allowlist secret -- dummy offline fixture
    else:
        with pytest.raises(pipeline.AuthenticatedRedirectError):
            pipeline.post_multipart_form('https://original.invalid/api', {}, 'file', source, api_key='synthetic')  # pragma: allowlist secret -- dummy offline fixture
    assert len(transport.requests) == 1


@pytest.mark.parametrize('status', [200, 301, 302, 303, 307, 308])
def test_web_app_workspace_api_uses_redirect_policy(wire, status, web_app):
    app = web_app
    transport = wire(status)
    if status == 200:
        assert app.api_get_json('https://original.invalid', '/api/v1/workspaces', 'synthetic') == (200, {})  # pragma: allowlist secret -- dummy offline fixture
    else:
        with pytest.raises(pipeline.AuthenticatedRedirectError) as caught:
            app.api_get_json('https://original.invalid', '/api/v1/workspaces', 'synthetic')  # pragma: allowlist secret -- dummy offline fixture
        assert caught.value.code == status
    assert len(transport.requests) == 1


@pytest.mark.parametrize('status', [301, 302, 303, 307, 308])
def test_capability_token_requests_do_not_redirect(wire, status, web_app):
    import anythingllm_pdf_assistant_cli as cli
    app = web_app
    transport = wire(status)
    cli._notify_browser_stop({'port': 7860, 'stop_notification_token': 'synthetic'}, 'stop_requested')  # pragma: allowlist secret -- dummy offline fixture
    assert len(transport.requests) == 1
    transport.requests.clear()
    with patch.object(app, 'read_desktop_refresh_bridge_descriptor', return_value={
        'available': True, 'draft_guard_current': True, 'port': 43123, 'token': 'synthetic'  # pragma: allowlist secret -- dummy offline fixture
    }):
        report = app.request_desktop_workspace_refresh()
    assert report['status'] == 'rejected'
    assert 'redirect rejected' in report['error']
    assert len(transport.requests) == 1

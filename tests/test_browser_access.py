import pytest
from unittest.mock import patch
from types import SimpleNamespace
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient

from browser_access import BOOTSTRAP_PATH, BrowserAccessMiddleware, browser_ticket, same_process_request

pytestmark = pytest.mark.offline_deterministic
KEY = "offline-launcher-key"


def client():
    async def ok(request):
        return PlainTextResponse("ok")
    async def socket(ws):
        await ws.accept()
        await ws.send_text("ok")
    app = Starlette(routes=[Route("/", ok), Route("/healthz", ok),
                            Route("/gradio_api/startup-events", ok),
                            Route("/gradio_api/queue/join", ok, methods=["POST"]),
                            WebSocketRoute("/socket", socket)])
    app.add_middleware(BrowserAccessMiddleware, key=KEY)
    return TestClient(app, base_url="http://127.0.0.1:7860")


def authenticate(c):
    return c.post(BOOTSTRAP_PATH, headers={"X-Assistant-Browser-Ticket": browser_ticket(KEY)})


def test_routes_reject_unauthorized_clients_and_accept_launcher_session():
    with client() as c:
        assert c.get("/healthz").status_code == 200
        assert c.get("/").status_code == 403
        assert c.post("/gradio_api/queue/join").status_code == 403
        assert c.get("/gradio_api/startup-events").status_code == 403
        assert authenticate(c).status_code == 204
        cookie = c.cookies.get("pdf_assistant_session_7860")
        assert cookie
        assert c.get("/").status_code == 200
        assert c.post("/gradio_api/queue/join").status_code == 200
        with c.websocket_connect("ws://127.0.0.1:7860/socket") as ws:
            assert ws.receive_text() == "ok"


def test_internal_probe_exemption_is_exact_and_process_bound():
    with client() as c, patch("browser_access.same_process_request", return_value=True):
        assert c.get("/gradio_api/startup-events").status_code == 200
        assert c.head("/").status_code == 200
        assert c.get("/").status_code == 403
        assert c.post("/gradio_api/queue/join").status_code == 403
        assert c.get("/gradio_api/startup-events", headers={"Origin":"https://evil.invalid"}).status_code == 403


def test_signed_internal_probe_is_limited_to_gradio_startup_routes():
    async def ok(request):
        return PlainTextResponse("ok")
    app = Starlette(routes=[Route("/", ok), Route("/healthz", ok),
                            Route("/gradio_api/startup-events", ok),
                            Route("/gradio_api/queue/join", ok, methods=["POST"])])
    app.add_middleware(BrowserAccessMiddleware, key=KEY, internal_probe_token="private-startup-token")
    header = {"X-PDF-Assistant-Internal-Probe": "private-startup-token"}
    with TestClient(app, base_url="http://127.0.0.1:7860") as c:
        assert c.get("/gradio_api/startup-events", headers=header).status_code == 200
        assert c.head("/", headers=header).status_code == 200
        assert c.get("/", headers=header).status_code == 403
        assert c.post("/gradio_api/queue/join", headers=header).status_code == 403
        assert c.get("/gradio_api/startup-events").status_code == 403


def test_closed_self_probe_socket_still_requires_own_exact_endpoints():
    connection = SimpleNamespace(
        status="CLOSE", laddr=("127.0.0.1", 60663),
        raddr=("127.0.0.1", 60183),
    )
    owned = {"client": ("127.0.0.1", 60663), "server": ("127.0.0.1", 60183)}
    unrelated = {"client": ("127.0.0.1", 60664), "server": ("127.0.0.1", 60183)}
    with patch("browser_access.psutil.Process") as process, patch("browser_access.time.sleep"):
        process.return_value.net_connections.return_value = [connection]
        assert same_process_request(owned)
        assert not same_process_request(unrelated)


def test_ticket_is_one_use_and_session_does_not_cross_server_instances():
    ticket = browser_ticket(KEY)
    with client() as first, client() as second:
        headers = {"X-Assistant-Browser-Ticket": ticket}
        response = first.post(BOOTSTRAP_PATH, headers=headers)
        assert response.status_code == 204
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "SameSite=strict" in response.headers["set-cookie"]
        assert first.post(BOOTSTRAP_PATH, headers=headers).status_code == 403
        second.cookies.update(first.cookies)
        assert second.get("/").status_code == 403


@pytest.mark.parametrize("ticket", ["", "invalid", browser_ticket(KEY, now=0), "non-ascii-\u00f1"])
def test_invalid_expired_tickets_fail_closed(ticket):
    with client() as c:
        # ASGI permits hostile Latin-1 headers even though httpx disallows them.
        if not ticket.isascii():
            guard = BrowserAccessMiddleware(None, key=KEY)
            assert not guard.consume_ticket(ticket)
        else:
            assert c.post(BOOTSTRAP_PATH, headers={"X-Assistant-Browser-Ticket": ticket}).status_code == 403


def test_cross_origin_requests_and_dns_rebinding_are_rejected():
    with client() as c:
        assert authenticate(c).status_code == 204
        assert c.post("/gradio_api/queue/join", headers={"Origin": "https://evil.invalid"}).status_code == 403
        assert c.get("/healthz", headers={"Host": "evil.invalid"}).status_code == 403
        assert c.post(BOOTSTRAP_PATH, headers={"Origin": "https://evil.invalid",
                      "X-Assistant-Browser-Ticket": browser_ticket(KEY)}).status_code == 403

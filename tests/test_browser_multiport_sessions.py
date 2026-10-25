"""A shared browser cookie jar must keep both localhost applications usable."""
import asyncio

import httpx
import pytest
from starlette.responses import PlainTextResponse

from browser_access import BOOTSTRAP_PATH, BrowserAccessMiddleware, browser_ticket

pytestmark = pytest.mark.offline_deterministic


async def ok(scope, receive, send):
    await PlainTextResponse("ok")(scope, receive, send)


class TwoPorts(httpx.AsyncBaseTransport):
    def __init__(self):
        self.transports = {
            port: httpx.ASGITransport(app=BrowserAccessMiddleware(ok, key=f"offline-port-{port}"))
            for port in (7860, 7861)
        }

    async def handle_async_request(self, request):
        return await self.transports[request.url.port].handle_async_request(request)


def test_one_browser_keeps_both_ports_authenticated_after_each_bootstrap():
    async def scenario():
        async with httpx.AsyncClient(transport=TwoPorts()) as browser:
            for port in (7860, 7861):
                origin = f"http://127.0.0.1:{port}"
                response = await browser.post(origin + BOOTSTRAP_PATH,
                    headers={"Origin": origin, "X-Assistant-Browser-Ticket": browser_ticket(f"offline-port-{port}")})
                assert response.status_code == 204
                assert "HttpOnly" in response.headers["set-cookie"]
                assert "SameSite=strict" in response.headers["set-cookie"]
            for port in (7860, 7861):
                origin = f"http://127.0.0.1:{port}"
                assert (await browser.get(origin + "/", headers={"Origin": origin})).status_code == 200
            assert len(browser.cookies) == 2
    asyncio.run(scenario())


def test_other_port_token_never_authorizes_a_different_server():
    async def scenario():
        async with httpx.AsyncClient(transport=TwoPorts()) as browser:
            assert (await browser.post("http://127.0.0.1:7860" + BOOTSTRAP_PATH,
                headers={"X-Assistant-Browser-Ticket": browser_ticket("offline-port-7860")})).status_code == 204
            assert (await browser.get("http://127.0.0.1:7861/")).status_code == 403
            token = next(iter(browser.cookies.jar)).value
            browser.cookies.set("pdf_assistant_session_7861", token, domain="127.0.0.1", path="/")
            assert (await browser.get("http://127.0.0.1:7861/")).status_code == 403
    asyncio.run(scenario())


def test_cookie_namespace_uses_the_listener_not_a_spoofed_host_port():
    async def scenario():
        async with httpx.AsyncClient(transport=TwoPorts()) as browser:
            response = await browser.post("http://127.0.0.1:7861" + BOOTSTRAP_PATH,
                headers={"Host": "127.0.0.1:7860", "X-Assistant-Browser-Ticket": browser_ticket("offline-port-7861")})
            assert response.status_code == 204
            assert response.headers["set-cookie"].startswith("pdf_assistant_session_7861=")
    asyncio.run(scenario())


def test_missing_listener_address_has_a_separate_instance_fallback():
    first = BrowserAccessMiddleware(ok, key="offline-first-server")
    second = BrowserAccessMiddleware(ok, key="offline-second-server")
    assert first.cookie_name({"server": None}) != second.cookie_name({"server": None})

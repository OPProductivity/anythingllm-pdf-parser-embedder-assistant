"""Launcher-owned, per-server browser sessions; no visible login workflow."""
import hashlib
import hmac
import os
import secrets
import re
import threading
import time
from http.cookies import SimpleCookie

from starlette.responses import HTMLResponse, PlainTextResponse, Response

KEY_ENV = "ANYTHINGLLM_PDF_ASSISTANT_BROWSER_KEY"
COOKIE = "pdf_assistant_session"
BOOTSTRAP_PATH = "/assistant-browser-session"
TICKET_SECONDS = 90


def browser_ticket(key, *, now=None):
    issued = int(time.time() if now is None else now)
    body = f"{issued}.{secrets.token_hex(16)}"
    signature = hmac.new(key.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


class BrowserAccessMiddleware:
    """Protect every mounted Gradio HTTP/websocket route before dispatch."""

    def __init__(self, app, *, key, stop_token=""):
        self.app, self.key, self.stop_token = app, key, stop_token
        self.sessions = set()
        self.used_tickets = {}
        self.lock = threading.Lock()

    def consume_ticket(self, ticket):
        if not re.fullmatch(r"[0-9]{1,12}\.[0-9a-f]{32}\.[0-9a-f]{64}", ticket):
            return False
        try:
            issued, nonce, signature = ticket.split(".")
            timestamp = int(issued)
        except (ValueError, TypeError):
            return False
        now = time.time()
        expected = hmac.new(self.key.encode(), f"{issued}.{nonce}".encode(), hashlib.sha256).hexdigest()
        if not (0 <= now - timestamp <= TICKET_SECONDS) or not hmac.compare_digest(expected, signature):
            return False
        with self.lock:
            self.used_tickets = {t: expiry for t, expiry in self.used_tickets.items() if expiry >= now}
            if ticket in self.used_tickets or len(self.used_tickets) >= 512:
                return False
            self.used_tickets[ticket] = timestamp + TICKET_SECONDS
        return True

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        host = headers.get("host", "")
        # Reject DNS-rebinding hostnames, even on the public readiness route.
        allowed_host = host.split(":", 1)[0] in {"127.0.0.1", "localhost"}
        origin = headers.get("origin")
        same_origin = origin is None or origin == f"http://{host}"
        path = scope.get("path", "")
        if allowed_host and same_origin and scope["type"] == "http":
            if path == "/healthz" and scope["method"] == "GET":
                return await self.app(scope, receive, send)
            if path == BOOTSTRAP_PATH and scope["method"] == "GET":
                # The secret lives in a fragment, never an HTTP URL or log.
                page = """<!doctype html><meta charset="utf-8"><script>
                (async () => {
                  const ticket = location.hash.slice(1);
                  history.replaceState(null, '', location.pathname);
                  const response = await fetch(location.pathname, {method:'POST',
                    headers:{'X-Assistant-Browser-Ticket':ticket}, credentials:'same-origin'});
                  if (response.ok) location.replace('/');
                  else document.body.textContent = 'Open the assistant using its Start shortcut.';
                })();</script><body></body>"""
                return await HTMLResponse(page, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})(scope, receive, send)
            if path == BOOTSTRAP_PATH and scope["method"] == "POST":
                if self.consume_ticket(headers.get("x-assistant-browser-ticket", "")):
                    session = secrets.token_urlsafe(32)
                    with self.lock:
                        if len(self.sessions) >= 512:
                            response = PlainTextResponse("Too many browser sessions; restart the idle assistant.", status_code=429)
                        else:
                            self.sessions.add(session)
                            response = Response(status_code=204, headers={"Cache-Control": "no-store"})
                            response.set_cookie(COOKIE, session, httponly=True, samesite="strict", path="/")
                    return await response(scope, receive, send)
                return await PlainTextResponse("Invalid or already used browser ticket.", status_code=403)(scope, receive, send)
            if path in {"/assistant-lifecycle/stop_requested", "/assistant-lifecycle/stop_cancelled"}:
                token = headers.get("x-assistant-stop-token", "")
                if self.stop_token and hmac.compare_digest(token, self.stop_token):
                    return await self.app(scope, receive, send)
        cookie = SimpleCookie()
        try:
            cookie.load(headers.get("cookie", ""))
            token = cookie[COOKIE].value if COOKIE in cookie else ""
        except Exception:
            token = ""
        with self.lock:
            authorized = token in self.sessions
        if allowed_host and same_origin and authorized:
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 1008})
        return await PlainTextResponse("Open the assistant using its Start shortcut.", status_code=403,
                                       headers={"Cache-Control": "no-store"})(scope, receive, send)


def server_browser_key():
    key = os.environ.get(KEY_ENV)
    if not key:
        key = secrets.token_urlsafe(32)
        os.environ[KEY_ENV] = key
    return key

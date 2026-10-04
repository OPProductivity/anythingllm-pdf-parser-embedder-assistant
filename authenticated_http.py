"""Redirect policy shared by API and local capability-token requests."""
import urllib.error
import urllib.request
import urllib.parse
import time
import http.client
from desktop_service_trust import managed_key_storage, verify_connected_desktop_socket

MAX_JSON_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_ERROR_RESPONSE_BYTES = 64 * 1024
MAX_HTTP_HEADER_BYTES = 64 * 1024


class ResponseBudgetExceeded(ValueError):
    """The peer exceeded a response budget; no truncated JSON is accepted."""


def validate_authenticated_url(url):
    parsed = urllib.parse.urlsplit(str(url))
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or any(ord(c) < 32 for c in str(url))):
        raise ValueError("Authenticated API endpoint must be a plain HTTP(S) origin without URL credentials.")
    if parsed.scheme == "http" and parsed.hostname.lower() not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Remote authenticated API endpoints require HTTPS; no credential was sent.")
    return parsed


def read_bounded_response(response, limit=MAX_JSON_RESPONSE_BYTES, *, deadline_seconds=30):
    length = response.headers.get("Content-Length") if getattr(response, "headers", None) else None
    if length is not None:
        try:
            declared = int(length)
        except ValueError:
            raise ResponseBudgetExceeded("Invalid HTTP response Content-Length.") from None
        if declared < 0 or declared > limit:
            raise ResponseBudgetExceeded("HTTP response exceeds the configured byte budget.")
    deadline = time.monotonic() + max(0.01, float(deadline_seconds))
    read = getattr(response, "read1", None) or response.read
    data = bytearray()
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError("HTTP response body exceeded its absolute read deadline.")
        chunk = read(min(65536, limit + 1 - len(data)))
        if time.monotonic() >= deadline:
            raise TimeoutError("HTTP response body exceeded its absolute read deadline.")
        if not chunk:
            return bytes(data)
        data.extend(chunk)
        if len(data) > limit:
            raise ResponseBudgetExceeded("HTTP response exceeds the configured byte budget.")


def authenticated_opener(request):
    validate_authenticated_url(request.full_url)
    # Never entrust credentials to implicit environment-controlled proxies.
    authorization = request.get_header("Authorization", "")
    storage = managed_key_storage(authorization[7:]) if authorization.startswith("Bearer ") else None
    handlers = [urllib.request.ProxyHandler({}), RejectAuthenticatedRedirects()]
    if storage:
        class DesktopConnection(http.client.HTTPConnection):
            def connect(self):
                super().connect()
                try:
                    verify_connected_desktop_socket(self.sock, request.full_url, storage)
                except BaseException:
                    self.close()
                    raise
        class DesktopHandler(urllib.request.HTTPHandler):
            def http_open(self, req):
                return self.do_open(DesktopConnection, req)
        handlers.append(DesktopHandler())
    return urllib.request.build_opener(*handlers)


class AuthenticatedRedirectError(urllib.error.HTTPError):
    """An authenticated request redirected; nothing was replayed."""


class RejectAuthenticatedRedirects(urllib.request.HTTPRedirectHandler):
    def http_error_302(self, req, fp, code, msg, headers):
        fp.close()
        raise AuthenticatedRedirectError(
            req.full_url, code,
            "Authenticated API redirect rejected; use the final API endpoint. No redirect was followed.",
            headers, None,
        )

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302

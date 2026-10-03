"""Redirect policy shared by API and local capability-token requests."""
import urllib.error
import urllib.request


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

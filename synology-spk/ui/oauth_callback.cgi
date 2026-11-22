#!/usr/bin/python3
"""Clean query-free OAuth2 redirect endpoint for Google Drive inside Synology DSM 7.3+.

Google OAuth2 does not permit query parameters in Registered Redirect URIs, and
Synology DSM 7 nginx requires URLs to end in `.cgi`. This script exposes:
    https://<nas>:5001/webman/3rdparty/gphotos-migrator/oauth_callback.cgi
and delegates allowlisted OAuth callback params straight to:
    http://127.0.0.1:8765/api/oauth/google/callback
using the shared `dsm_auth.py` session validator (bridge cookie or fresh webapi validation).
"""

import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dsm_auth  # noqa: E402

ALLOWED_OAUTH_PARAMS = frozenset({"code", "state", "error", "error_description"})


def main():
    authenticated, dsm_user, dsm_is_admin = dsm_auth.authenticate_oauth_callback()
    if not authenticated:
        dsm_auth.write_response(
            403,
            [("Content-Type", "text/plain; charset=utf-8")],
            b"DSM authentication required. Please sign in to Synology DSM and retry.",
        )
        return

    raw_qs = os.environ.get("QUERY_STRING", "")
    filtered_pairs = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(raw_qs, keep_blank_values=True)
        if k in ALLOWED_OAUTH_PARAMS
    ]
    safe_qs = urllib.parse.urlencode(filtered_pairs, doseq=True)
    upstream_url = dsm_auth.BACKEND_BASE + "/api/oauth/google/callback"
    if safe_qs:
        upstream_url = upstream_url + "?" + safe_qs

    proto = "https" if os.environ.get("HTTPS", "").lower() in ("on", "1", "true") else "http"
    if os.environ.get("SERVER_PORT") == "5001":
        proto = "https"
    host = os.environ.get("HTTP_HOST", "")

    headers = {
        "X-Forwarded-Proto": proto,
        "X-Forwarded-Prefix": dsm_auth.UI_PREFIX,
        "X-Proxy-Mode": "cgi",
        "X-DSM-Session-Verified": "1",
    }
    if dsm_user:
        headers["X-DSM-User"] = dsm_user
        headers["X-DSM-Admin"] = "1" if dsm_is_admin else "0"
    proxy_secret = dsm_auth.get_proxy_secret()
    if proxy_secret:
        headers["X-Proxy-Secret"] = proxy_secret
    if host:
        safe_host = dsm_auth.sanitize_header_part(host)
        if safe_host is not None:
            headers["X-Forwarded-Host"] = safe_host

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    req = urllib.request.Request(upstream_url, headers=headers, method="GET")
    try:
        with opener.open(req, timeout=30) as resp:
            dsm_auth.write_response(resp.status, list(resp.headers.items()), resp.read())
    except urllib.error.HTTPError as err:
        resp_headers = list(err.headers.items()) if err.headers else [("Content-Type", "text/plain")]
        body = err.read() if err.fp else b""
        dsm_auth.write_response(err.code, resp_headers, body)
    except (urllib.error.URLError, OSError):
        dsm_auth.write_response(
            503,
            [("Content-Type", "text/plain; charset=utf-8")],
            b"Google Photos Migrator service is starting. Please refresh in a few seconds.",
        )


if __name__ == "__main__":
    main()

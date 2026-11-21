#!/usr/bin/python3
"""Clean query-free OAuth2 redirect endpoint for Google Drive inside Synology DSM 7.

Google OAuth2 does not permit query parameters in Registered Redirect URIs, and
Synology DSM 7 nginx requires URLs to end in `.cgi`. This script exposes:
    https://<nas>:5001/webman/3rdparty/gphotos-migrator/oauth_callback.cgi
and delegates the callback straight to `http://127.0.0.1:8765/api/oauth/google/callback`.
"""

import os
import subprocess
import sys
import urllib.error
import urllib.request

BACKEND_BASE = os.environ.get("GPHOTOS_BACKEND_URL", "http://127.0.0.1:8765").rstrip("/")
UI_PREFIX = "/webman/3rdparty/gphotos-migrator"
AUTH_CGI = "/usr/syno/synoman/webman/authenticate.cgi"

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
}


def get_dsm_user():
    if not os.path.isfile(AUTH_CGI):
        if os.environ.get("GPHOTOS_SKIP_DSM_AUTH") == "1":
            return os.environ.get("GPHOTOS_TEST_DSM_USER", "admin")
        return None
    try:
        proc = subprocess.run([AUTH_CGI], capture_output=True, text=True, timeout=5, check=False)
        user = (proc.stdout or "").strip()
        return user if user else None
    except Exception:
        return None


def write_response(status, headers, body):
    out = sys.stdout.buffer
    out.write(("Status: %d\r\n" % status).encode("ascii"))
    for k, v in headers:
        if k.lower() in HOP_BY_HOP_HEADERS:
            continue
        out.write(("%s: %s\r\n" % (k, v)).encode("latin-1", errors="ignore"))
    out.write(("Content-Length: %d\r\n" % len(body)).encode("ascii"))
    out.write(b"\r\n")
    out.write(body)
    out.flush()


def main():
    dsm_user = get_dsm_user()
    if not dsm_user:
        write_response(
            403,
            [("Content-Type", "text/plain; charset=utf-8")],
            b"DSM authentication required. Please sign in to Synology DSM and retry.",
        )
        return

    qs = os.environ.get("QUERY_STRING", "")
    upstream_url = BACKEND_BASE + "/api/oauth/google/callback"
    if qs:
        upstream_url = upstream_url + "?" + qs

    proto = "https" if os.environ.get("HTTPS", "").lower() in ("on", "1", "true") else "http"
    if os.environ.get("SERVER_PORT") == "5001":
        proto = "https"
    host = os.environ.get("HTTP_HOST", "")

    headers = {
        "X-Forwarded-Proto": proto,
        "X-Forwarded-Prefix": UI_PREFIX,
        "X-Proxy-Mode": "cgi",
        "X-DSM-User": dsm_user,
    }
    if host:
        headers["X-Forwarded-Host"] = host

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            return None

    opener = urllib.request.build_opener(_NoRedirect())
    req = urllib.request.Request(upstream_url, headers=headers, method="GET")
    try:
        with opener.open(req, timeout=30) as resp:
            write_response(resp.status, list(resp.headers.items()), resp.read())
    except urllib.error.HTTPError as err:
        resp_headers = list(err.headers.items()) if err.headers else [("Content-Type", "text/plain")]
        body = err.read() if err.fp else b""
        write_response(err.code, resp_headers, body)
    except (urllib.error.URLError, OSError):
        write_response(
            200,
            [("Content-Type", "text/plain; charset=utf-8")],
            b"Google Photos Migrator service is starting. Please refresh in a few seconds.",
        )


if __name__ == "__main__":
    main()

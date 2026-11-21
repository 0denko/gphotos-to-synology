#!/usr/bin/env python3
"""Same-origin DSM 7 CGI reverse proxy for Google Photos Migrator.

Forwards authenticated Synology DSM requests from:
    https://<nas>:5001/webman/3rdparty/gphotos-migrator/proxy.cgi/<path>
to the local background worker container at:
    http://127.0.0.1:8765/<path>
"""

from __future__ import annotations

import os
import subprocess
import sys
import urllib.error
import urllib.request

BACKEND_BASE = os.environ.get("GPHOTOS_BACKEND_URL", "http://127.0.0.1:8765").rstrip("/")
PROXY_PREFIX = "/webman/3rdparty/gphotos-migrator/proxy.cgi"
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


def get_dsm_user() -> str | None:
    """Verify an active Synology DSM web session and return the authenticated DSM username."""
    if not os.path.isfile(AUTH_CGI):
        if os.environ.get("GPHOTOS_SKIP_DSM_AUTH") == "1":
            return os.environ.get("GPHOTOS_TEST_DSM_USER", "admin")
        return None
    try:
        proc = subprocess.run(
            [AUTH_CGI],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        user = proc.stdout.strip()
        return user if user else None
    except Exception:
        return None


def write_response(status: int, headers: list[tuple[str, str]], body: bytes) -> None:
    out = sys.stdout.buffer
    out.write(f"Status: {status}\r\n".encode("ascii"))
    for k, v in headers:
        if k.lower() in HOP_BY_HOP_HEADERS:
            continue
        out.write(f"{k}: {v}\r\n".encode("latin-1", errors="ignore"))
    out.write(f"Content-Length: {len(body)}\r\n".encode("ascii"))
    out.write(b"\r\n")
    out.write(body)
    out.flush()


def render_backend_offline_html() -> bytes:
    html = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta http-equiv="refresh" content="3" />
<title>Google Photos Migrator — Starting Service</title>
<style>
  body{margin:0;background:#0b1120;color:#e5ecf6;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;padding:40px 24px;line-height:1.55}
  .box{max-width:560px;margin:40px auto;background:#111827;border:1px solid #243047;border-radius:12px;padding:28px;text-align:center}
  h1{margin:0 0 10px;font-size:1.3rem}
  p{color:#94a3b8;margin:0 0 18px}
  .btn{display:inline-block;background:#3b82f6;color:#fff;border:none;border-radius:8px;padding:9px 18px;font-weight:600;cursor:pointer}
  .btn:hover{background:#2563eb}
</style>
</head>
<body>
<div class="box">
  <h1>Starting Google Photos Migrator…</h1>
  <p>The background service is starting up. This window will connect automatically in a few seconds.</p>
  <p style="font-size:0.85rem">If this message stays for more than 15 seconds, open <strong>Package Center → Installed → Google Photos Migrator</strong> and make sure its status is <strong>Running</strong>.</p>
  <button class="btn" onclick="window.location.reload()">Retry Now</button>
</div>
</body>
</html>"""
    return html.encode("utf-8")


def main() -> None:
    dsm_user = get_dsm_user()
    if not dsm_user:
        write_response(
            403,
            [("Content-Type", "text/plain; charset=utf-8")],
            b"DSM authentication required. Please sign in to Synology DSM.",
        )
        return

    path_info = os.environ.get("PATH_INFO", "")
    query_string = os.environ.get("QUERY_STRING", "")

    # Ensure trailing slash on root so relative static/ and api/ paths resolve under proxy.cgi/
    if path_info == "":
        target = f"{PROXY_PREFIX}/"
        if query_string:
            target = f"{target}?{query_string}"
        write_response(302, [("Location", target)], b"")
        return

    method = os.environ.get("REQUEST_METHOD", "GET").upper()
    upstream_url = f"{BACKEND_BASE}{path_info}"
    if query_string:
        upstream_url = f"{upstream_url}?{query_string}"

    req_body: bytes | None = None
    content_length_raw = os.environ.get("CONTENT_LENGTH", "").strip()
    if content_length_raw.isdigit() and int(content_length_raw) > 0:
        req_body = sys.stdin.buffer.read(int(content_length_raw))

    proto = "https" if os.environ.get("HTTPS", "").lower() in ("on", "1", "true") else "http"
    if os.environ.get("SERVER_PORT") == "5001":
        proto = "https"
    host = os.environ.get("HTTP_HOST", "")

    headers: dict[str, str] = {
        "X-Forwarded-Proto": proto,
        "X-Forwarded-Prefix": PROXY_PREFIX,
        "X-DSM-User": dsm_user,
    }
    if host:
        headers["X-Forwarded-Host"] = host
    if os.environ.get("CONTENT_TYPE"):
        headers["Content-Type"] = os.environ["CONTENT_TYPE"]
    if os.environ.get("HTTP_ACCEPT"):
        headers["Accept"] = os.environ["HTTP_ACCEPT"]

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            return None

    opener = urllib.request.build_opener(_NoRedirect())
    req = urllib.request.Request(upstream_url, data=req_body, headers=headers, method=method)

    try:
        with opener.open(req, timeout=60) as resp:
            resp_headers = list(resp.headers.items())
            body = resp.read()
            write_response(resp.status, resp_headers, body)
    except urllib.error.HTTPError as err:
        resp_headers = list(err.headers.items()) if err.headers else [("Content-Type", "text/plain")]
        body = err.read() if err.fp else b""
        write_response(err.code, resp_headers, body)
    except (urllib.error.URLError, OSError):
        write_response(
            503,
            [("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store")],
            render_backend_offline_html(),
        )


if __name__ == "__main__":
    main()

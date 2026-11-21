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


def is_dsm_authenticated() -> bool:
    """Verify an active Synology DSM web session using DSM's built-in authenticate.cgi."""
    if not os.path.isfile(AUTH_CGI):
        # Allow local non-DSM unit tests to run cleanly
        return os.environ.get("GPHOTOS_SKIP_DSM_AUTH") == "1"
    try:
        proc = subprocess.run(
            [AUTH_CGI],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return bool(proc.stdout.strip())
    except Exception:
        return False


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
<title>Google Photos Migrator — Start Background Service</title>
<style>
  body{margin:0;background:#0b1120;color:#e5ecf6;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;padding:32px;line-height:1.55}
  .box{max-width:740px;margin:0 auto;background:#111827;border:1px solid #243047;border-radius:12px;padding:24px 28px}
  h1{margin:0 0 8px;font-size:1.3rem}
  p{color:#94a3b8;margin:0 0 16px}
  pre{background:#060a14;border:1px solid #243047;border-radius:8px;padding:14px;overflow-x:auto;font-size:0.84rem;color:#93c5fd}
  .btn{display:inline-block;background:#3b82f6;color:#fff;border:none;border-radius:8px;padding:9px 16px;font-weight:600;cursor:pointer;text-decoration:none}
  .btn:hover{background:#2563eb}
</style>
</head>
<body>
<div class="box">
  <h1>Background Migrator Engine Is Not Running Yet</h1>
  <p>This native DSM window is installed and ready! To start the background engine, open <strong>Container Manager → Project → Create</strong>, choose <strong>Create docker-compose.yml</strong>, paste the config below, and click <strong>Start</strong>:</p>
<pre>services:
  gphotos-migrator:
    image: ghcr.io/0denko/gphotos-to-synology:latest
    container_name: gphotos-to-synology
    restart: unless-stopped
    ports:
      - "8765:8765"
    environment:
      - PUID=1026
      - PGID=100
      - TZ=America/Los_Angeles
    volumes:
      - /volume1/docker/gphotos-migrator:/data
      - /volume1/photo:/data/photos
    extra_hosts:
      - "host.docker.internal:host-gateway"</pre>
  <p style="font-size:0.85rem">For Synology Photos <strong>Personal Space</strong>, replace <code>/volume1/photo:/data/photos</code> with <code>/volume1/homes/&lt;YOUR_DSM_USER&gt;/Photos:/data/photos</code>.</p>
  <button class="btn" onclick="window.location.reload()">Retry Connection</button>
</div>
</body>
</html>"""
    return html.encode("utf-8")


def main() -> None:
    if not is_dsm_authenticated():
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

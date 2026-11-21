#!/usr/bin/python3
"""Same-origin DSM 7 CGI reverse proxy for Google Photos Migrator.

Synology DSM 7 nginx routes `/webman/3rdparty/.../*.cgi` (matching `\\.cgi$`
without PATH_INFO). Therefore subpaths are passed via the `p` query parameter:
    https://<nas>:5001/webman/3rdparty/gphotos-migrator/proxy.cgi?p=/api/status
and forwarded to the local daemon at:
    http://127.0.0.1:8765/api/status
"""

import os
import subprocess
import sys
import urllib.error
import urllib.parse
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


def render_backend_offline_html():
    html = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta http-equiv="refresh" content="3" />
<title>Google Photos Migrator - Starting Service</title>
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
  <h1>Starting Google Photos Migrator...</h1>
  <p>The background service is starting up. This window will connect automatically in a few seconds.</p>
  <p style="font-size:0.85rem">If this message stays for more than 15 seconds, open <strong>Package Center &rarr; Installed &rarr; Google Photos Migrator</strong> and make sure its status is <strong>Running</strong>.</p>
  <button class="btn" onclick="window.location.reload()">Retry Now</button>
</div>
</body>
</html>"""
    return html.encode("utf-8")


def _extract_target_path_and_query():
    raw_qs = os.environ.get("QUERY_STRING", "")
    path_info = os.environ.get("PATH_INFO", "")

    pairs = urllib.parse.parse_qsl(raw_qs, keep_blank_values=True)
    target_path = None
    forwarded_pairs = []
    for k, v in pairs:
        if k == "p" and target_path is None:
            target_path = v
        else:
            forwarded_pairs.append((k, v))

    if not target_path:
        target_path = path_info if path_info else "/"

    # Normalize & block any scheme or traversal attempts
    if "://" in target_path or ".." in target_path.split("/"):
        target_path = "/"
    if not target_path.startswith("/"):
        target_path = "/" + target_path

    forwarded_qs = urllib.parse.urlencode(forwarded_pairs, doseq=True)
    return target_path, forwarded_qs


def main():
    dsm_user = get_dsm_user()
    if not dsm_user:
        write_response(
            403,
            [("Content-Type", "text/plain; charset=utf-8")],
            b"DSM authentication required. Please sign in to Synology DSM.",
        )
        return

    target_path, forwarded_qs = _extract_target_path_and_query()
    method = os.environ.get("REQUEST_METHOD", "GET").upper()
    upstream_url = BACKEND_BASE + target_path
    if forwarded_qs:
        upstream_url = upstream_url + "?" + forwarded_qs

    req_body = None
    content_length_raw = os.environ.get("CONTENT_LENGTH", "").strip()
    if content_length_raw.isdigit() and int(content_length_raw) > 0:
        req_body = sys.stdin.buffer.read(int(content_length_raw))

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
    if os.environ.get("CONTENT_TYPE"):
        headers["Content-Type"] = os.environ["CONTENT_TYPE"]
    if os.environ.get("HTTP_ACCEPT"):
        headers["Accept"] = os.environ["HTTP_ACCEPT"]
    if os.environ.get("HTTP_X_PROFILE_ID"):
        headers["X-Profile-Id"] = os.environ["HTTP_X_PROFILE_ID"]

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            return None

    opener = urllib.request.build_opener(_NoRedirect())
    req = urllib.request.Request(upstream_url, data=req_body, headers=headers, method=method)

    try:
        with opener.open(req, timeout=60) as resp:
            resp_headers = list(resp.headers.items())
            body = resp.read()
            if target_path == "/" and b"static/styles.css" in body:
                body = body.replace(b'href="static/styles.css"', b'href="proxy.cgi?p=/static/styles.css"')
                body = body.replace(b'src="static/app.js"', b'src="proxy.cgi?p=/static/app.js"')
            write_response(resp.status, resp_headers, body)
    except urllib.error.HTTPError as err:
        resp_headers = list(err.headers.items()) if err.headers else [("Content-Type", "text/plain")]
        body = err.read() if err.fp else b""
        write_response(err.code, resp_headers, body)
    except (urllib.error.URLError, OSError):
        # Return HTTP 200 so Synology DSM nginx does not replace our starting page with a generic 503 error page
        write_response(
            200,
            [("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store")],
            render_backend_offline_html(),
        )


if __name__ == "__main__":
    main()

#!/usr/bin/python3
"""Same-origin DSM 7.3+ CGI reverse proxy for Google Photos Migrator.

Synology DSM 7 nginx routes `/webman/3rdparty/.../*.cgi` (matching `\\.cgi$`
without PATH_INFO). Subpaths are passed via the `p` query parameter:
    https://<nas>:5001/webman/3rdparty/gphotos-migrator/proxy.cgi?p=/api/status
and forwarded to the local daemon at:
    http://127.0.0.1:8765/api/status
using shared webapi session validation + session bridge (`dsm_auth.py`),
`X-Proxy-Secret`, `X-DSM-User`, `X-DSM-Admin`, and `X-Requested-With`.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dsm_auth  # noqa: E402

MAX_REQUEST_BODY_BYTES = 1024 * 1024  # 1 MiB cap for JSON API payloads


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


def render_auth_error_html(title, detail):
    html = (
        """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>%s</title>
<style>
  body{margin:0;background:#0b1120;color:#e5ecf6;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;padding:40px 24px;line-height:1.55}
  .box{max-width:560px;margin:40px auto;background:#111827;border:1px solid #243047;border-radius:12px;padding:28px;text-align:center}
  h1{margin:0 0 10px;font-size:1.25rem;color:#f87171}
  p{color:#94a3b8;margin:0 0 16px}
</style>
</head>
<body>
<div class="box">
  <h1>%s</h1>
  <p>%s</p>
</div>
</body>
</html>"""
        % (title, title, detail)
    )
    return html.encode("utf-8")


def _extract_target_path_and_query():
    raw_qs = os.environ.get("QUERY_STRING", "")
    path_info = os.environ.get("PATH_INFO", "")

    pairs = urllib.parse.parse_qsl(raw_qs, keep_blank_values=True)
    target_path = None
    desktop_dsm_user = ""
    forwarded_pairs = []
    for k, v in pairs:
        if k == "p" and target_path is None:
            target_path = v
        elif k == "dsm_user" and not desktop_dsm_user:
            desktop_dsm_user = v
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
    return target_path, forwarded_qs, desktop_dsm_user


def main():
    target_path, forwarded_qs, desktop_dsm_user = _extract_target_path_and_query()
    method = os.environ.get("REQUEST_METHOD", "GET").upper()

    proto = "https" if os.environ.get("HTTPS", "").lower() in ("on", "1", "true") else "http"
    if os.environ.get("SERVER_PORT") == "5001":
        proto = "https"
    is_https = proto == "https"

    dsm_user, dsm_is_admin, set_cookie, auth_err = dsm_auth.authenticate_proxy_request(
        desktop_dsm_user, is_https
    )
    if not dsm_user:
        if auth_err == "missing_user":
            msg = (
                "Could not determine your Synology DSM username from the desktop session. "
                "Please close this window, refresh DSM, and open Google Photos Migrator from the DSM Main Menu."
            )
            if method == "GET" and target_path == "/":
                dsm_auth.write_response(
                    403,
                    [("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store")],
                    render_auth_error_html("DSM User Identity Missing", msg),
                )
            else:
                dsm_auth.write_response(
                    403,
                    [("Content-Type", "application/json; charset=utf-8")],
                    json.dumps({"detail": msg}).encode("utf-8"),
                )
            return
        if auth_err == "not_admin":
            msg = "Administrator privileges are required to open Google Photos Migrator."
            if method == "GET" and target_path == "/":
                dsm_auth.write_response(
                    403,
                    [("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store")],
                    render_auth_error_html("Administrator Access Required", msg),
                )
            else:
                dsm_auth.write_response(
                    403,
                    [("Content-Type", "application/json; charset=utf-8")],
                    json.dumps({"detail": msg}).encode("utf-8"),
                )
            return
        dsm_auth.write_response(
            403,
            [("Content-Type", "text/plain; charset=utf-8")],
            b"DSM authentication required. Please sign in to Synology DSM.",
        )
        return

    upstream_url = dsm_auth.BACKEND_BASE + target_path
    if forwarded_qs:
        upstream_url = upstream_url + "?" + forwarded_qs

    req_body = None
    content_length_raw = os.environ.get("CONTENT_LENGTH", "").strip()
    if content_length_raw.isdigit():
        content_len = int(content_length_raw)
        if content_len > MAX_REQUEST_BODY_BYTES:
            dsm_auth.write_response(
                413,
                [("Content-Type", "application/json; charset=utf-8")],
                json.dumps({"detail": "Request payload too large (max 1 MiB)"}).encode("utf-8"),
            )
            return
        if content_len > 0:
            req_body = sys.stdin.buffer.read(content_len)

    host = os.environ.get("HTTP_HOST", "")

    headers = {
        "X-Forwarded-Proto": proto,
        "X-Forwarded-Prefix": dsm_auth.UI_PREFIX,
        "X-Proxy-Mode": "cgi",
        "X-DSM-User": dsm_user,
        "X-DSM-Admin": "1" if dsm_is_admin else "0",
    }
    proxy_secret = dsm_auth.get_proxy_secret()
    if proxy_secret:
        headers["X-Proxy-Secret"] = proxy_secret
    raw_forward = [
        ("X-Forwarded-Host", host),
        ("Content-Type", os.environ.get("CONTENT_TYPE", "")),
        ("Accept", os.environ.get("HTTP_ACCEPT", "")),
        ("X-Profile-Id", os.environ.get("HTTP_X_PROFILE_ID", "")),
        ("X-Requested-With", os.environ.get("HTTP_X_REQUESTED_WITH", "")),
    ]
    for hdr_name, hdr_val in raw_forward:
        if not hdr_val:
            continue
        safe_val = dsm_auth.sanitize_header_part(hdr_val)
        if safe_val is None:
            dsm_auth.write_response(
                400,
                [("Content-Type", "application/json; charset=utf-8")],
                json.dumps({"detail": "Invalid control characters in request header"}).encode("utf-8"),
            )
            return
        headers[hdr_name] = safe_val

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            return None

    # Bypass ambient HTTP_PROXY for loopback backend requests
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    req = urllib.request.Request(upstream_url, data=req_body, headers=headers, method=method)

    try:
        with opener.open(req, timeout=60) as resp:
            resp_headers = list(resp.headers.items())
            if set_cookie:
                resp_headers.append(("Set-Cookie", set_cookie))
            body = resp.read()
            if target_path == "/" and b"static/styles.css" in body:
                body = body.replace(b'href="static/styles.css"', b'href="proxy.cgi?p=/static/styles.css"')
                body = body.replace(b'src="static/app.js"', b'src="proxy.cgi?p=/static/app.js"')
            dsm_auth.write_response(resp.status, resp_headers, body)
    except urllib.error.HTTPError as err:
        resp_headers = list(err.headers.items()) if err.headers else [("Content-Type", "text/plain")]
        if set_cookie:
            resp_headers.append(("Set-Cookie", set_cookie))
        body = err.read() if err.fp else b""
        dsm_auth.write_response(err.code, resp_headers, body)
    except (urllib.error.URLError, OSError):
        if method == "GET" and target_path == "/":
            resp_headers = [("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store")]
            if set_cookie:
                resp_headers.append(("Set-Cookie", set_cookie))
            dsm_auth.write_response(200, resp_headers, render_backend_offline_html())
        else:
            dsm_auth.write_response(
                503,
                [("Content-Type", "application/json; charset=utf-8"), ("Cache-Control", "no-store")],
                json.dumps({"detail": "Google Photos Migrator service is starting or unavailable"}).encode("utf-8"),
            )


if __name__ == "__main__":
    main()

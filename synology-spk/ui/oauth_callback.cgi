#!/usr/bin/python3
"""Clean query-free OAuth2 redirect endpoint for Google Drive inside Synology DSM 7.

Google OAuth2 does not permit query parameters in Registered Redirect URIs, and
Synology DSM 7 nginx requires URLs to end in `.cgi`. This script exposes:
    https://<nas>:5001/webman/3rdparty/gphotos-migrator/oauth_callback.cgi
and delegates allowlisted OAuth callback params straight to:
    http://127.0.0.1:8765/api/oauth/google/callback
"""

import grp
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

BACKEND_BASE = os.environ.get("GPHOTOS_BACKEND_URL", "http://127.0.0.1:8765").rstrip("/")
UI_PREFIX = "/webman/3rdparty/gphotos-migrator"
AUTH_CGI = "/usr/syno/synoman/webman/authenticate.cgi"
DEFAULT_SECRET_FILE = "/var/packages/gphotos-migrator/var/proxy.secret"
DSM_USER_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")
ALLOWED_OAUTH_PARAMS = frozenset({"code", "state", "error", "error_description"})

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


def get_proxy_secret():
    env_secret = os.environ.get("GPHOTOS_PROXY_SECRET", "").strip()
    if env_secret:
        return env_secret
    secret_file = os.environ.get("GPHOTOS_PROXY_SECRET_FILE", DEFAULT_SECRET_FILE)
    try:
        if os.path.isfile(secret_file):
            with open(secret_file, "r", encoding="utf-8") as fh:
                return fh.read().strip()
    except OSError:
        pass
    return ""


def get_dsm_user():
    if not os.path.isfile(AUTH_CGI):
        if os.environ.get("GPHOTOS_SKIP_DSM_AUTH") == "1":
            user = os.environ.get("GPHOTOS_TEST_DSM_USER", "admin").strip()
            return user if DSM_USER_RE.match(user) else None
        return None
    try:
        proc = subprocess.run([AUTH_CGI], capture_output=True, text=True, timeout=5, check=False)
        lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
        if not lines:
            return None
        user = lines[-1]
        return user if DSM_USER_RE.match(user) else None
    except Exception:
        return None


def is_dsm_admin(username):
    if not os.path.isfile(AUTH_CGI) and os.environ.get("GPHOTOS_SKIP_DSM_AUTH") == "1":
        return os.environ.get("GPHOTOS_TEST_DSM_ADMIN", "1").strip() == "1"
    if not username:
        return False
    try:
        admin_grp = grp.getgrnam("administrators")
        if username in admin_grp.gr_mem:
            return True
    except KeyError:
        pass
    return username == "admin"


def _sanitize_header_part(value):
    s = str(value)
    if "\r" in s or "\n" in s or "\x00" in s:
        return None
    return s


def write_response(status, headers, body):
    out = sys.stdout.buffer
    out.write(("Status: %d\r\n" % int(status)).encode("ascii"))
    for k, v in headers:
        if k.lower() in HOP_BY_HOP_HEADERS:
            continue
        sk = _sanitize_header_part(k)
        sv = _sanitize_header_part(v)
        if sk is None or sv is None:
            continue
        out.write(("%s: %s\r\n" % (sk, sv)).encode("latin-1", errors="ignore"))
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

    raw_qs = os.environ.get("QUERY_STRING", "")
    filtered_pairs = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(raw_qs, keep_blank_values=True)
        if k in ALLOWED_OAUTH_PARAMS
    ]
    safe_qs = urllib.parse.urlencode(filtered_pairs, doseq=True)
    upstream_url = BACKEND_BASE + "/api/oauth/google/callback"
    if safe_qs:
        upstream_url = upstream_url + "?" + safe_qs

    proto = "https" if os.environ.get("HTTPS", "").lower() in ("on", "1", "true") else "http"
    if os.environ.get("SERVER_PORT") == "5001":
        proto = "https"
    host = os.environ.get("HTTP_HOST", "")

    headers = {
        "X-Forwarded-Proto": proto,
        "X-Forwarded-Prefix": UI_PREFIX,
        "X-Proxy-Mode": "cgi",
        "X-DSM-User": dsm_user,
        "X-DSM-Admin": "1" if is_dsm_admin(dsm_user) else "0",
    }
    proxy_secret = get_proxy_secret()
    if proxy_secret:
        headers["X-Proxy-Secret"] = proxy_secret
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
            503,
            [("Content-Type", "text/plain; charset=utf-8")],
            b"Google Photos Migrator service is starting. Please refresh in a few seconds.",
        )


if __name__ == "__main__":
    main()

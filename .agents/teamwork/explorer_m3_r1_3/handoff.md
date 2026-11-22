# Explorer 3 (`explorer_m3_r1_3`) Handoff Report — DSM 7 CGIs, Frontend Web UI & CGI/JS Tests

## 1. Executive Summary & Critical E2E Compatibility Findings

We audited:
- `synology-spk/ui/proxy.cgi` (lines 1–193)
- `synology-spk/ui/oauth_callback.cgi` (lines 1–111)
- `synology-spk/ui/GPhotosMigrator.js` (lines 1–69)
- `src/gphotos_synology/web/static/app.js` (lines 1–773)
- `src/gphotos_synology/web/static/index.html` (lines 1–523)
- `src/gphotos_synology/web/static/styles.css` (lines 1–625)
- `tests/e2e/conftest.py` (`run_cgi` lines 972–1072, `run_app_js_scenario` lines 1218–1545)
- `tests/e2e/test_tier1_features.py` (`test_t1_e4_proxy_and_oauth_cgi_forward_authenticated_requests` lines 1053–1097, `test_t1_e5_frontend_js_syntax_and_app_js_happy_path_in_node` lines 1099–1120)
- `tests/e2e/test_tier2_boundaries.py` (`test_t2_minor_proxy_cgi_limits_offline_503_crlf_and_fail_closed_auth` lines 1155–1233, `test_t2_minor_oauth_callback_cgi_allowlists_query_params_and_direct_ignores_forwarded_host` lines 1235–1274, `test_t2_minor_app_js_escape_html_prevents_attribute_breakout_xss` lines 1276–1305)
- `tests/e2e/test_tier3_interactions.py` (`test_t3_p1_proxy_cgi_with_real_fastapi_c1_c2_g6` lines 55–147, `test_t3_p2_oauth_callback_cgi_with_real_fastapi_g3_c2` lines 149–241, `test_t3_p9_frontend_app_js_xss_defense_header_merge_and_cgi_routing` lines 644–692)
- `tests/e2e/test_tier4_workloads.py` (`test_t4_w1_multi_user_dsm_household_migration` lines 53–198, `test_t4_w4_adversarial_security_penetration_chain` lines 403–572)

### Critical E2E Contract Notes (Must Follow!)
1. **Unauthenticated / Invalid DSM Username Status Code in CGIs MUST remain HTTP `403` (NOT `401`)**:
   - In `tests/e2e/test_tier2_boundaries.py` lines 1168 and 1177:
     ```python
     assert r_no_auth.status == 403
     ...
     assert r_bad_user.status == 403
     ```
   - Both `proxy.cgi` and `oauth_callback.cgi` MUST continue returning `write_response(403, [("Content-Type", "text/plain; charset=utf-8")], b"DSM authentication required...")` when `get_dsm_user()` returns `None` or `""`. (The FastAPI backend returns `401` on missing/invalid `X-Proxy-Secret`, whereas the CGI layer itself returns `403` when DSM session authentication fails or the username fails regex validation.)
2. **Client Request Header CRLF Sanitization in `proxy.cgi` & `oauth_callback.cgi`**:
   - In `tests/e2e/test_tier2_boundaries.py` line 1230:
     ```python
     r_crlf = run_cgi(
         PROXY_CGI_PATH,
         backend_url=mock_backend_server.url,
         query_string="p=/api/status",
         proxy_secret=secret,
         http_headers={"X-Profile-Id": "default\r\nX-Injected: yes"},
     )
     assert b"X-Injected" not in r_crlf.raw_header_block
     ```
   - Python 3.11+ `http.client.putheader` raises `ValueError: Invalid header value` if a request header value contains `\r` or `\n`. Therefore, `proxy.cgi` and `oauth_callback.cgi` MUST sanitize/reject any incoming environment header containing `\r`, `\n`, or `\x00` ( via `_clean_header_value()`) **before** passing `headers` to `urllib.request.Request(...)`, AND also strip any response header containing `\r`, `\n`, or `\x00` inside `write_response(...)`.
3. **1 MiB Body Cap Without Reading Stdin When `CONTENT_LENGTH > 1048576`**:
   - In `tests/e2e/test_tier2_boundaries.py` line 1185:
     `run_cgi(..., content_length_override=str(1024 * 1024 + 1), body=b"")` sends `CONTENT_LENGTH=1048577` with an empty stdin.
   - `proxy.cgi` must check `content_length > MAX_REQUEST_BODY_BYTES` and return HTTP `413` **before** attempting to read from `sys.stdin.buffer`.
4. **Node `app.js` Harness Compatibility (`tests/e2e/conftest.py` lines 1218–1545)**:
   - `conftest.py` runs `app.js` inside a custom Node `vm` sandbox where `document.getElementById(id)` always returns a `MockElement` (never `null`), and `div.textContent = s` only escapes `&`, `<`, `>` (mirroring real browser `div.innerHTML`).
   - Therefore, `escapeHtml(text)` in `app.js` MUST use string `.replace(...)` for `&`, `<`, `>`, `"`, and `'` rather than `document.createElement("div")`.
   - Furthermore, at startup `app.js` must still execute `loadProfiles(true)`, `loadSettings()`, `refreshDashboard()`, and `checkUpdates(false)` without blocking on extra DOM queries so that `test_t1_e5`, `test_t2_minor_app_js_escape_html`, `test_t3_p9`, and `test_t4_w4` see all expected `/api/*` fetch calls immediately.

---

## 2. Exact Implementation Specification for `synology-spk/ui/proxy.cgi`

Replace `/home/antigravity/gphotos-to-synology/synology-spk/ui/proxy.cgi` (lines 1–193) with the following hardened implementation:

```python
#!/usr/bin/python3
"""Same-origin DSM 7 CGI reverse proxy for Google Photos Migrator.

Synology DSM 7 nginx routes `/webman/3rdparty/.../*.cgi` (matching `\\.cgi$`
without PATH_INFO). Therefore subpaths are passed via the `p` query parameter:
    https://<nas>:5001/webman/3rdparty/gphotos-migrator/proxy.cgi?p=/api/status
and forwarded to the local daemon at:
    http://127.0.0.1:8765/api/status
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
DEFAULT_AUTH_CGI = "/usr/syno/synoman/webman/authenticate.cgi"
FALLBACK_AUTH_CGI = "/usr/syno/synoman/webman/modules/authenticate.cgi"
DEFAULT_PROXY_SECRET_FILE = "/var/packages/gphotos-migrator/var/proxy.secret"
MAX_REQUEST_BODY_BYTES = 1024 * 1024  # 1 MiB (1,048,576 bytes)

_VALID_USER_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")

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


def _has_control_chars(value):
    return any(c in value for c in ("\r", "\n", "\x00"))


def _clean_header_value(value):
    if not value or _has_control_chars(value):
        return None
    return value


def _resolve_auth_cgi():
    override = os.environ.get("GPHOTOS_AUTH_CGI", "").strip()
    if override:
        return override
    if os.path.isfile(DEFAULT_AUTH_CGI):
        return DEFAULT_AUTH_CGI
    if os.path.isfile(FALLBACK_AUTH_CGI):
        return FALLBACK_AUTH_CGI
    return DEFAULT_AUTH_CGI


def get_dsm_user():
    """Verify an active Synology DSM web session and return the validated DSM username."""
    auth_cgi = _resolve_auth_cgi()
    if not os.path.isfile(auth_cgi):
        if os.environ.get("GPHOTOS_SKIP_DSM_AUTH") == "1":
            candidate = os.environ.get("GPHOTOS_TEST_DSM_USER", "admin").strip()
            if candidate and not _has_control_chars(candidate) and _VALID_USER_RE.match(candidate):
                return candidate
        return None
    try:
        proc = subprocess.run(
            [auth_cgi],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode != 0:
            return None
        lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
        if not lines:
            return None
        user = lines[-1]
        if not _VALID_USER_RE.match(user):
            return None
        return user
    except Exception:
        return None


def is_dsm_admin(user):
    """Return True if the authenticated DSM user belongs to the Synology administrators group."""
    if not user:
        return False
    test_override = os.environ.get("GPHOTOS_TEST_DSM_ADMIN", "").strip().lower()
    if test_override in ("1", "true", "yes"):
        return True
    if test_override in ("0", "false", "no"):
        return False
    try:
        admin_group = grp.getgrnam("administrators")
        members_lower = {m.lower() for m in admin_group.gr_mem}
        if user in admin_group.gr_mem or user.lower() in members_lower:
            return True
    except (KeyError, OSError):
        pass
    return user.lower() == "admin"


def get_proxy_secret():
    """Load the shared proxy secret from env (for tests) or the SPK secret file."""
    env_secret = os.environ.get("GPHOTOS_PROXY_SECRET", "").strip()
    if env_secret:
        return env_secret
    secret_file = (
        os.environ.get("GPHOTOS_PROXY_SECRET_FILE", "").strip()
        or os.environ.get("PROXY_SECRET_FILE", "").strip()
        or DEFAULT_PROXY_SECRET_FILE
    )
    try:
        if os.path.isfile(secret_file):
            with open(secret_file, "r", encoding="utf-8") as fh:
                return fh.read().strip()
    except OSError:
        pass
    return ""


def write_response(status, headers, body):
    out = sys.stdout.buffer
    out.write(("Status: %d\r\n" % int(status)).encode("ascii"))
    for k, v in headers:
        k_str = str(k)
        v_str = str(v)
        if k_str.lower() in HOP_BY_HOP_HEADERS:
            continue
        if _has_control_chars(k_str) or _has_control_chars(v_str):
            continue
        out.write(("%s: %s\r\n" % (k_str, v_str)).encode("latin-1", errors="ignore"))
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

    # Normalize & block any scheme, control chars, or traversal attempts
    if "://" in target_path or ".." in target_path.split("/") or _has_control_chars(target_path):
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
    if content_length_raw:
        try:
            content_length = int(content_length_raw)
        except ValueError:
            write_response(
                400,
                [("Content-Type", "application/json; charset=utf-8")],
                b'{"detail": "Invalid Content-Length header"}',
            )
            return
        if content_length < 0:
            write_response(
                400,
                [("Content-Type", "application/json; charset=utf-8")],
                b'{"detail": "Invalid Content-Length header"}',
            )
            return
        if content_length > MAX_REQUEST_BODY_BYTES:
            write_response(
                413,
                [("Content-Type", "application/json; charset=utf-8")],
                b'{"detail": "Request payload exceeds 1 MiB limit"}',
            )
            return
        if content_length > 0:
            req_body = sys.stdin.buffer.read(content_length)
            if len(req_body) > MAX_REQUEST_BODY_BYTES:
                write_response(
                    413,
                    [("Content-Type", "application/json; charset=utf-8")],
                    b'{"detail": "Request payload exceeds 1 MiB limit"}',
                )
                return

    proto = "https" if os.environ.get("HTTPS", "").lower() in ("on", "1", "true") else "http"
    if os.environ.get("SERVER_PORT") == "5001":
        proto = "https"

    headers = {
        "X-Forwarded-Proto": proto,
        "X-Forwarded-Prefix": UI_PREFIX,
        "X-Proxy-Mode": "cgi",
        "X-DSM-User": dsm_user,
        "X-DSM-Admin": "1" if is_dsm_admin(dsm_user) else "0",
    }

    proxy_secret = _clean_header_value(get_proxy_secret())
    if proxy_secret:
        headers["X-Proxy-Secret"] = proxy_secret

    host = _clean_header_value(os.environ.get("HTTP_HOST", ""))
    if host:
        headers["X-Forwarded-Host"] = host

    content_type = _clean_header_value(os.environ.get("CONTENT_TYPE", ""))
    if content_type:
        headers["Content-Type"] = content_type

    accept = _clean_header_value(os.environ.get("HTTP_ACCEPT", ""))
    if accept:
        headers["Accept"] = accept

    profile_id = _clean_header_value(os.environ.get("HTTP_X_PROFILE_ID", ""))
    if profile_id:
        headers["X-Profile-Id"] = profile_id

    requested_with = _clean_header_value(os.environ.get("HTTP_X_REQUESTED_WITH", ""))
    if requested_with:
        headers["X-Requested-With"] = requested_with

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
        if method == "GET" and target_path in ("/", ""):
            # Return HTTP 200 only for initial GET / so DSM nginx shows our starting page
            write_response(
                200,
                [("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store")],
                render_backend_offline_html(),
            )
        else:
            write_response(
                503,
                [("Content-Type", "application/json; charset=utf-8"), ("Cache-Control", "no-store")],
                b'{"detail": "Google Photos Migrator service is starting or unavailable."}',
            )


if __name__ == "__main__":
    main()
```

---

## 3. Exact Implementation Specification for `synology-spk/ui/oauth_callback.cgi`

Replace `/home/antigravity/gphotos-to-synology/synology-spk/ui/oauth_callback.cgi` (lines 1–111) with the following hardened implementation:

```python
#!/usr/bin/python3
"""Clean query-free OAuth2 redirect endpoint for Google Drive inside Synology DSM 7.

Google OAuth2 does not permit query parameters in Registered Redirect URIs, and
Synology DSM 7 nginx requires URLs to end in `.cgi`. This script exposes:
    https://<nas>:5001/webman/3rdparty/gphotos-migrator/oauth_callback.cgi
and delegates the callback straight to `http://127.0.0.1:8765/api/oauth/google/callback`.
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
DEFAULT_AUTH_CGI = "/usr/syno/synoman/webman/authenticate.cgi"
FALLBACK_AUTH_CGI = "/usr/syno/synoman/webman/modules/authenticate.cgi"
DEFAULT_PROXY_SECRET_FILE = "/var/packages/gphotos-migrator/var/proxy.secret"

_VALID_USER_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")
ALLOWED_OAUTH_QUERY_PARAMS = {"code", "state", "error", "error_description"}

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


def _has_control_chars(value):
    return any(c in value for c in ("\r", "\n", "\x00"))


def _clean_header_value(value):
    if not value or _has_control_chars(value):
        return None
    return value


def _resolve_auth_cgi():
    override = os.environ.get("GPHOTOS_AUTH_CGI", "").strip()
    if override:
        return override
    if os.path.isfile(DEFAULT_AUTH_CGI):
        return DEFAULT_AUTH_CGI
    if os.path.isfile(FALLBACK_AUTH_CGI):
        return FALLBACK_AUTH_CGI
    return DEFAULT_AUTH_CGI


def get_dsm_user():
    """Verify an active Synology DSM web session and return the validated DSM username."""
    auth_cgi = _resolve_auth_cgi()
    if not os.path.isfile(auth_cgi):
        if os.environ.get("GPHOTOS_SKIP_DSM_AUTH") == "1":
            candidate = os.environ.get("GPHOTOS_TEST_DSM_USER", "admin").strip()
            if candidate and not _has_control_chars(candidate) and _VALID_USER_RE.match(candidate):
                return candidate
        return None
    try:
        proc = subprocess.run([auth_cgi], capture_output=True, text=True, timeout=5, check=False)
        if proc.returncode != 0:
            return None
        lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
        if not lines:
            return None
        user = lines[-1]
        if not _VALID_USER_RE.match(user):
            return None
        return user
    except Exception:
        return None


def is_dsm_admin(user):
    """Return True if the authenticated DSM user belongs to the Synology administrators group."""
    if not user:
        return False
    test_override = os.environ.get("GPHOTOS_TEST_DSM_ADMIN", "").strip().lower()
    if test_override in ("1", "true", "yes"):
        return True
    if test_override in ("0", "false", "no"):
        return False
    try:
        admin_group = grp.getgrnam("administrators")
        members_lower = {m.lower() for m in admin_group.gr_mem}
        if user in admin_group.gr_mem or user.lower() in members_lower:
            return True
    except (KeyError, OSError):
        pass
    return user.lower() == "admin"


def get_proxy_secret():
    """Load the shared proxy secret from env (for tests) or the SPK secret file."""
    env_secret = os.environ.get("GPHOTOS_PROXY_SECRET", "").strip()
    if env_secret:
        return env_secret
    secret_file = (
        os.environ.get("GPHOTOS_PROXY_SECRET_FILE", "").strip()
        or os.environ.get("PROXY_SECRET_FILE", "").strip()
        or DEFAULT_PROXY_SECRET_FILE
    )
    try:
        if os.path.isfile(secret_file):
            with open(secret_file, "r", encoding="utf-8") as fh:
                return fh.read().strip()
    except OSError:
        pass
    return ""


def write_response(status, headers, body):
    out = sys.stdout.buffer
    out.write(("Status: %d\r\n" % int(status)).encode("ascii"))
    for k, v in headers:
        k_str = str(k)
        v_str = str(v)
        if k_str.lower() in HOP_BY_HOP_HEADERS:
            continue
        if _has_control_chars(k_str) or _has_control_chars(v_str):
            continue
        out.write(("%s: %s\r\n" % (k_str, v_str)).encode("latin-1", errors="ignore"))
    out.write(("Content-Length: %d\r\n" % len(body)).encode("ascii"))
    out.write(b"\r\n")
    out.write(body)
    out.flush()


def _filter_oauth_query_string(raw_qs):
    pairs = urllib.parse.parse_qsl(raw_qs, keep_blank_values=True)
    allowed_pairs = [(k, v) for k, v in pairs if k in ALLOWED_OAUTH_QUERY_PARAMS]
    return urllib.parse.urlencode(allowed_pairs, doseq=True)


def main():
    dsm_user = get_dsm_user()
    if not dsm_user:
        write_response(
            403,
            [("Content-Type", "text/plain; charset=utf-8")],
            b"DSM authentication required. Please sign in to Synology DSM and retry.",
        )
        return

    qs = _filter_oauth_query_string(os.environ.get("QUERY_STRING", ""))
    upstream_url = BACKEND_BASE + "/api/oauth/google/callback"
    if qs:
        upstream_url = upstream_url + "?" + qs

    proto = "https" if os.environ.get("HTTPS", "").lower() in ("on", "1", "true") else "http"
    if os.environ.get("SERVER_PORT") == "5001":
        proto = "https"

    headers = {
        "X-Forwarded-Proto": proto,
        "X-Forwarded-Prefix": UI_PREFIX,
        "X-Proxy-Mode": "cgi",
        "X-DSM-User": dsm_user,
        "X-DSM-Admin": "1" if is_dsm_admin(dsm_user) else "0",
    }

    proxy_secret = _clean_header_value(get_proxy_secret())
    if proxy_secret:
        headers["X-Proxy-Secret"] = proxy_secret

    host = _clean_header_value(os.environ.get("HTTP_HOST", ""))
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
```

---

## 4. Exact Implementation Specification for Frontend (`app.js`, `index.html`, `styles.css`)

### 4.1 `src/gphotos_synology/web/static/app.js`

Make the following specific edits in `/home/antigravity/gphotos-to-synology/src/gphotos_synology/web/static/app.js`:

1. **Lines 23–26 (State variables)**:
   Add `let isAdmin = true;` alongside `activeProfileId`:
   ```javascript
   let activeProfileId = "default";
   let profilesCache = [];
   let isAdmin = true;
   let isPaused = false;
   let toastTimer = null;
   ```

2. **Lines 58–78 (`escapeHtml` and `apiFetch`)**:
   - Replace `escapeHtml(text)` with explicit character escaping for `&`, `<`, `>`, `"`, and `'` so it prevents attribute-context XSS in both browsers and the Node E2E harness:
     ```javascript
     function escapeHtml(text) {
       return String(text ?? "")
         .replace(/&/g, "&amp;")
         .replace(/</g, "&lt;")
         .replace(/>/g, "&gt;")
         .replace(/"/g, "&quot;")
         .replace(/'/g, "&#39;");
     }
     ```
   - Replace `apiFetch(url, options = {})` so `...options` is spread **before** `headers`, `"X-Requested-With": "XMLHttpRequest"` is always sent, and `401` responses reveal the API token login modal when in direct/Docker mode:
     ```javascript
     async function apiFetch(url, options = {}) {
       const resp = await fetch(urlFor(url), {
         ...options,
         headers: {
           "Content-Type": "application/json",
           "X-Profile-Id": activeProfileId,
           "X-Requested-With": "XMLHttpRequest",
           ...(options.headers || {}),
         },
       });
       const data = await resp.json().catch(() => ({}));
       if (!resp.ok) {
         if (resp.status === 401 && !IS_CGI_PROXY) {
           $("auth-modal").classList.remove("hidden");
         }
         throw new Error(data.detail || `Request failed (${resp.status})`);
       }
       return data;
     }
     ```

3. **Lines 108–118 (`renderStatus` Drive badge & refresh error surfacing)**:
   Surface `data.gdrive_error` (from `GoogleDriveClient.connection_status()`) if present:
   ```javascript
   const driveBadge = $("gdrive-badge");
   if (data.gdrive_connected) {
     driveBadge.textContent = "Connected";
     driveBadge.className = "pill pill-ok";
     driveBadge.title = "";
   } else if (data.gdrive_error) {
     driveBadge.textContent = "Token Error";
     driveBadge.className = "pill pill-err";
     driveBadge.title = data.gdrive_error;
   } else if (data.gdrive_oauth_configured) {
     driveBadge.textContent = "Ready to Authorize";
     driveBadge.className = "pill pill-warn";
     driveBadge.title = "";
   } else {
     driveBadge.textContent = "Not Configured";
     driveBadge.className = "pill pill-muted";
     driveBadge.title = "";
   }
   ```

4. **Lines 329–355 (`saveSettings` G4 password-clearing notice)**:
   When `PUT /api/settings` returns updated `settings` (or `warnings`), sync `$("synology_password").value` so a cleared password reflects immediately in the UI and show any warning toast:
   ```javascript
   try {
     const res = await apiFetch("/api/settings", {
       method: "PUT",
       body: JSON.stringify({ updates }),
     });
     if (res && res.settings && $("synology_password")) {
       $("synology_password").value = res.settings.synology_password ?? "";
     }
     if (res && Array.isArray(res.warnings) && res.warnings.length > 0) {
       showToast(res.warnings.join(" "));
     } else {
       showToast("Settings saved and applied.");
     }
     await refreshDashboard();
     return true;
   } catch (err) {
     showToast(err.message, true);
     return false;
   }
   ```

5. **Lines 499–524 (`renderUpdateStatus` semver & `.sha256` checksum display)**:
   Update `renderUpdateStatus(u, notifyIfCurrent = false)` to display semver version info, `.sha256` checksum badge/link, and hide `#btn-apply-update` for non-admin users:
   ```javascript
   function renderUpdateStatus(u, notifyIfCurrent = false) {
     currentBuildSha = u.current_sha || "";
     const verLabel = `v${u.current_version}${u.current_sha ? ` · ${u.current_sha}` : ""}`;
     $("version-badge").textContent = verLabel;

     const banner = $("update-banner");
     if (u.update_available) {
       const fromLabel = u.current_version ? `v${u.current_version}` : (u.current_sha || "");
       const toLabel = u.latest_version ? `v${u.latest_version}` : (u.latest_sha || "");
       const msg = u.latest_commit_message ? ` — ${u.latest_commit_message}` : "";
       const shaSuffix = u.spk_sha256 ? ` (SHA-256: ${String(u.spk_sha256).slice(0, 12)}…)` : "";
       $("update-banner-text").textContent = `${fromLabel} → ${toLabel}${msg}${shaSuffix}`;
       $("link-download-spk").href = u.spk_download_url || u.release_url || "#";
       const shaLink = $("link-download-sha256");
       const shaUrl = u.spk_sha256_url || u.sha256_sums_url || "";
       if (shaUrl) {
         shaLink.href = shaUrl;
         shaLink.classList.remove("hidden");
       } else {
         shaLink.classList.add("hidden");
       }
       const applyBtn = $("btn-apply-update");
       applyBtn.textContent = u.auto_updater_configured ? "Update Now" : "Pull Latest in Container Manager";
       applyBtn.disabled = !u.auto_updater_configured || !isAdmin;
       applyBtn.classList.toggle("hidden", !isAdmin);
       banner.classList.remove("hidden");
     } else {
       banner.classList.add("hidden");
       if (notifyIfCurrent) {
         if (u.error) {
           showToast(u.error, true);
         } else {
           showToast(`Up to date (${verLabel}).`);
         }
       }
     }
   }
   ```

6. **Lines 622–655 (`loadProfiles` non-admin UX enforcement)**:
   Track `isAdmin = data.is_admin !== false;` and toggle admin-only controls (`#btn-manage-users`, `#btn-toggle-pause`, `#btn-apply-update`):
   ```javascript
   async function loadProfiles(selectOnInit = false) {
     try {
       const data = await apiFetch("/api/profiles");
       profilesCache = data.profiles || [];
       isAdmin = data.is_admin !== false;

       const dsmUser = data.dsm_user || data.current_dsm_user;
       if (dsmUser) {
         const pill = $("dsm-user-pill");
         pill.textContent = `DSM: ${dsmUser}`;
         pill.classList.remove("hidden");
       }

       $("btn-manage-users").classList.toggle("hidden", !isAdmin);
       $("btn-toggle-pause").classList.toggle("hidden", !isAdmin);
       $("btn-apply-update").classList.toggle("hidden", !isAdmin);

       const fallbackProfileId =
         data.recommended_profile_id || (profilesCache[0] && profilesCache[0].id) || "default";
       if (selectOnInit) {
         const urlProfile = new URLSearchParams(window.location.search).get("profile");
         const savedProfile = window.localStorage.getItem("gphotos_active_profile");
         const candidate = urlProfile || savedProfile || fallbackProfileId;
         activeProfileId = profilesCache.some((p) => p.id === candidate) ? candidate : fallbackProfileId;
       } else if (!profilesCache.some((p) => p.id === activeProfileId)) {
         activeProfileId = fallbackProfileId;
       }
   ```
   *(Notice `fallbackProfileId`! For a non-admin DSM user like `alice`, `profilesCache` only contains `[alice]`, NOT `"default"`! Defaulting `activeProfileId` to `fallbackProfileId` instead of hardcoded `"default"` ensures non-admin users automatically select their own profile instead of getting 403 on `"default"`!)*

7. **Direct / Docker Session Login Form Handler (`#auth-token-form`)**:
   Bind `#auth-token-form` submit and `#btn-close-auth-modal` click before the startup IIFE:
   ```javascript
   $("auth-token-form").addEventListener("submit", async (e) => {
     e.preventDefault();
     const token = ($("auth-token-input").value || "").trim();
     if (!token) return;
     try {
       await apiFetch("/api/auth/session", {
         method: "POST",
         body: JSON.stringify({ token }),
       });
       $("auth-token-input").value = "";
       $("auth-modal").classList.add("hidden");
       showToast("Signed in successfully.");
       await loadProfiles(true);
       await Promise.all([loadSettings(), refreshDashboard(), checkUpdates(false)]);
     } catch (err) {
       showToast(err.message, true);
     }
   });

   $("btn-close-auth-modal").addEventListener("click", () => {
     $("auth-modal").classList.add("hidden");
   });
   ```

---

### 4.2 `src/gphotos_synology/web/static/index.html`

Make the following updates in `/home/antigravity/gphotos-to-synology/src/gphotos_synology/web/static/index.html`:

1. **Line 20**: Update default version pill from `v1.0.0` to `v1.2.0`:
   ```html
   <span id="version-badge" class="pill pill-muted">v1.2.0</span>
   ```
2. **Lines 59–66 (Update Banner)**: Add `#link-download-sha256` next to `#link-download-spk`:
   ```html
   <div class="inline-actions" style="margin-top:0">
     <button id="btn-apply-update" class="btn btn-sm btn-primary" type="button">
       Update Now
     </button>
     <a id="link-download-spk" class="btn btn-sm btn-secondary" href="#" target="_blank" rel="noreferrer">
       DSM App (.spk)
     </a>
     <a id="link-download-sha256" class="btn btn-sm btn-secondary hidden" href="#" target="_blank" rel="noreferrer">
       .sha256 Checksum
     </a>
   </div>
   ```
3. **Lines 274–290 (Google Drive `trash` scope escalation & disconnect revocation notices)**:
   ```html
   <div class="field">
     <label for="gdrive_post_action">After Verified Import</label>
     <select id="gdrive_post_action" name="gdrive_post_action">
       <option value="keep">Keep Archive in Drive (Read-Only Scope)</option>
       <option value="trash">Move Archive to Drive Trash (Reclaim Quota)</option>
     </select>
     <small id="gdrive-scope-notice" class="hint">
       Selecting <strong>Move Archive to Drive Trash</strong> escalates Google OAuth from read-only
       (<code>drive.readonly</code>) to full Drive access (<code>drive</code>) and requires re-connecting Google Drive.
     </small>
   </div>
   ```
   And below the `Connect Google Drive` / `Disconnect` buttons (line 289):
   ```html
   <small id="gdrive-disconnect-notice" class="hint">
     Disconnecting deletes the local OAuth token on this NAS. To revoke the refresh token on Google's servers,
     also remove access at <a href="https://myaccount.google.com/permissions" target="_blank" rel="noreferrer">myaccount.google.com/permissions</a>.
   </small>
   ```
4. **Lines 310–338 (Synology DSM URL HTTPS placeholder, G4 password-clearing notice, and `synology_verify_ssl` notice)**:
   ```html
   <div class="row-2">
     <div class="field">
       <label for="synology_url">DSM URL</label>
       <input id="synology_url" name="synology_url" type="text" placeholder="https://127.0.0.1:5001" />
       <small id="synology-url-notice" class="hint">
         Changing the DSM URL clears any stored DSM password unless you enter a new password at the same time.
       </small>
     </div>
     <div class="field">
       <label for="synology_space">Synology Photos Space</label>
       <select id="synology_space" name="synology_space">
         <option value="personal">Personal Space (/homes/&lt;user&gt;/Photos)</option>
         <option value="shared">Shared Space (/photo)</option>
       </select>
     </div>
   </div>
   ...
   <label class="checkbox-row">
     <input id="synology_verify_ssl" name="synology_verify_ssl" type="checkbox" checked />
     <span>Verify HTTPS SSL certificate (recommended; uncheck only for self-signed LAN certificates)</span>
   </label>
   ```
5. **Lines 518–520 (Before `<script src="static/app.js"></script>`)**: Add the Direct/Docker API Token Sign-In Modal:
   ```html
   <!-- DIRECT / DOCKER API TOKEN AUTH MODAL -->
   <div id="auth-modal" class="modal-backdrop hidden" role="dialog" aria-modal="true" aria-labelledby="auth-modal-title">
     <div class="modal-card" style="max-width:460px">
       <div class="card-header">
         <div>
           <h2 id="auth-modal-title">API Token Required</h2>
           <p class="muted" style="margin:3px 0 0;font-size:0.85rem">
             Enter your <code>API_TOKEN</code> (or the auto-generated token from <code>/data/config/api.token</code>) to establish an authenticated session.
           </p>
         </div>
         <button id="btn-close-auth-modal" class="btn btn-sm btn-secondary" type="button">Close</button>
       </div>
       <form id="auth-token-form">
         <div class="field">
           <label for="auth-token-input">API Token</label>
           <input id="auth-token-input" name="token" type="password" placeholder="Paste API token..." autocomplete="current-password" />
         </div>
         <button type="submit" class="btn btn-primary">Sign In</button>
       </form>
     </div>
   </div>
   ```

---

## 5. Comprehensive Test Specification for `tests/test_cgi_and_security.py`

Create `/home/antigravity/gphotos-to-synology/tests/test_cgi_and_security.py` covering all CGI subprocess behaviors, security middleware (C1, C2, G3, G4, G6, G14), and frontend JS execution:

1. **DSM 7 `proxy.cgi` Subprocess Tests**:
   - `test_proxy_cgi_fails_closed_when_unauthenticated`:
     - Invokes `proxy.cgi` without `GPHOTOS_SKIP_DSM_AUTH` and with nonexistent `GPHOTOS_AUTH_CGI` -> asserts `status == 403`.
     - Invokes `proxy.cgi` with a temporary executable `GPHOTOS_AUTH_CGI` script exiting `1` (even while printing `"admin"`) -> asserts `status == 403`.
     - Invokes `proxy.cgi` with a temporary executable `GPHOTOS_AUTH_CGI` script printing multi-line output (`"Content-Type: text/plain\n\nalice\n"`) and exiting `0` -> asserts `status == 200` and upstream receives `X-DSM-User: alice`.
     - Invokes `proxy.cgi` with invalid username charsets (`"bad user"`, `"admin\r\nX-Evil: 1"`, `"../root"`, `"a" * 65`) -> asserts `status == 403`.
   - `test_proxy_cgi_forwards_secret_from_file_admin_flag_and_csrf_header`:
     - Writes a 64-hex secret to `tmp_path / "proxy.secret"`, sets `GPHOTOS_PROXY_SECRET_FILE`, passes `HTTP_X_REQUESTED_WITH="XMLHttpRequest"`, `HTTP_X_PROFILE_ID="alice"`, `GPHOTOS_TEST_DSM_ADMIN="0"` -> asserts upstream request receives `X-Proxy-Secret`, `X-Proxy-Mode: cgi`, `X-DSM-User: alice`, `X-DSM-Admin: 0`, `X-Requested-With: XMLHttpRequest`, `X-Profile-Id: alice`.
   - `test_proxy_cgi_enforces_1mib_body_cap_and_validates_content_length`:
     - `CONTENT_LENGTH=1048577` (`1024 * 1024 + 1`) -> returns `status == 413` JSON error without calling upstream.
     - `CONTENT_LENGTH="-5"` and `CONTENT_LENGTH="notanumber"` -> returns `status == 400` JSON error.
     - `CONTENT_LENGTH` with valid small JSON body -> returns `status == 200` and forwards body intact.
   - `test_proxy_cgi_offline_backend_returns_200_html_only_for_get_root_and_503_for_api`:
     - Points `GPHOTOS_BACKEND_URL` at an unused loopback port (`http://127.0.0.1:1`).
     - `GET` with `p=/` -> returns `status == 200` with `text/html` starter page.
     - `GET` with `p=/api/status` -> returns `status == 503` with `application/json`.
     - `POST` with `p=/` or `p=/api/actions/scan` -> returns `status == 503` with `application/json`.
   - `test_proxy_cgi_strips_crlf_headers_and_rewrites_root_static_urls`:
     - Sends `HTTP_X_PROFILE_ID="default\r\nX-Injected: 1"` -> returns `200` and `X-Injected` is not in CGI response headers or upstream request.
     - Upstream returns `GET /` HTML containing `href="static/styles.css"` and `src="static/app.js"` -> `proxy.cgi` rewrites them to `href="proxy.cgi?p=/static/styles.css"` and `src="proxy.cgi?p=/static/app.js"`.

2. **DSM 7 `oauth_callback.cgi` Subprocess Tests**:
   - `test_oauth_callback_cgi_auth_and_query_param_allowlist`:
     - Fails closed (`403`) when unauthenticated or invalid DSM username.
     - Filters `QUERY_STRING="code=c1&state=s1&error=e1&error_description=desc&profile=victim&evil=1"` to forward only `code`, `state`, `error`, `error_description` to `/api/oauth/google/callback` along with `X-Proxy-Secret`, `X-DSM-User`, `X-DSM-Admin`, and `X-Proxy-Mode: cgi`.

3. **Frontend `app.js`, `GPhotosMigrator.js` & `index.html` Tests**:
   - `test_frontend_js_syntax_and_escape_html_xss_protection`:
     - Runs `node --check` on `app.js` and `GPhotosMigrator.js`.
     - Executes `app.js` in Node VM with malicious archive filenames containing `"`, `'`, `<`, `>` and verifies `&quot;` / `&#39;` escaping in `#archives-tbody` `title="..."`.
   - `test_frontend_app_js_apifetch_headers_and_non_admin_ux`:
     - Verifies `apiFetch` sends `X-Requested-With: XMLHttpRequest`, `X-Profile-Id`, and `Content-Type: application/json` on mutating requests (`POST /api/actions/scan`, `PUT /api/settings`).
     - Verifies non-admin `/api/profiles` response (`is_admin: false`, `profiles: [{id: "alice", ...}]`) hides `#btn-manage-users`, `#btn-toggle-pause`, `#btn-apply-update` and selects `"alice"` as `activeProfileId`.
   - `test_index_html_security_notices_and_https_placeholder`:
     - Asserts `index.html` contains `placeholder="https://127.0.0.1:5001"`, `drive.readonly` / `drive` trash scope escalation notice, `myaccount.google.com/permissions` disconnect revocation notice, and G4 DSM URL password-clearing notice.

"""Shared Synology DSM 7.3+ session validation, bridge cookie, and CGI helpers.

Python 3.8+ compatible (Synology DSM 7.3 ships /usr/bin/python3 = 3.8; uses stdlib only).

Why not `/usr/syno/synoman/webman/authenticate.cgi`?
On DSM 7.3.2+, `authenticate.cgi` exits 7 (no output) for real browser desktop sessions
(`_SSID` / `id` / `did` / `stay_login` cookies) and exits 5 for `SYNO.API.Auth` sessions
when source IP differs from login IP. Instead, this module validates the browser's
forwarded `Cookie` header against DSM's own loopback webapi (`https://localhost:5001/webapi/entry.cgi`),
combines it with the DSM desktop username (`SYNO.SDS.Session`) and server-side `administrators`
group check, and mints a short-lived HttpOnly bridge cookie bound to the DSM session hash.
"""

import grp
import hashlib
import hmac
import json
import os
import re
import secrets
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BACKEND_BASE = os.environ.get("GPHOTOS_BACKEND_URL", "http://127.0.0.1:8765").rstrip("/")
UI_PREFIX = "/webman/3rdparty/gphotos-migrator"
DEFAULT_SECRET_FILE = "/var/packages/gphotos-migrator/var/proxy.secret"
DEFAULT_BRIDGE_DIR = "/var/packages/gphotos-migrator/var/sessions"
DEFAULT_DSM_WEBAPI_URL = "https://localhost:5001/webapi/entry.cgi"
BRIDGE_COOKIE_NAME = "gphotos_dsm_bridge"
DEFAULT_BRIDGE_TTL_SECONDS = 1800
DSM_USER_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")

# Lightweight auth-required DSM APIs tried in order before falling back to Initdata:
# 1. SYNO.Core.NormalUser (v1, get): ~200 B, returns authenticated user's own username when available
# 2. SYNO.Core.Desktop.Timeout (v1, get): ~100 B, proves valid DSM session without SynoToken
# 3. SYNO.Core.Desktop.Initdata (v1, get): verified live on DSM 7.3.2-86009 with real browser cookies
_WEBAPI_PROBES = (
    ("SYNO.Core.NormalUser", "1", "get"),
    ("SYNO.Core.Desktop.Timeout", "1", "get"),
    ("SYNO.Core.Desktop.Initdata", "1", "get"),
)

HOP_BY_HOP_HEADERS = frozenset(
    {
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
)


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


def get_dsm_webapi_url():
    return os.environ.get("GPHOTOS_DSM_WEBAPI_URL", DEFAULT_DSM_WEBAPI_URL).strip()


def get_bridge_dir():
    return os.environ.get("GPHOTOS_BRIDGE_DIR", DEFAULT_BRIDGE_DIR).strip()


def get_bridge_ttl_seconds():
    raw = os.environ.get("GPHOTOS_BRIDGE_TTL_SECONDS", "").strip()
    if raw:
        try:
            val = int(raw)
            if val > 0:
                return val
        except ValueError:
            pass
    return DEFAULT_BRIDGE_TTL_SECONDS


def is_skip_auth_test_mode():
    return (
        os.environ.get("GPHOTOS_SKIP_DSM_AUTH") == "1"
        and os.environ.get("GPHOTOS_FORCE_WEBAPI_AUTH") != "1"
    )


def is_valid_dsm_username(username):
    return bool(username and DSM_USER_RE.match(username))


def is_dsm_admin(username):
    """Determine if the authenticated DSM user belongs to the Synology administrators group."""
    if is_skip_auth_test_mode():
        return os.environ.get("GPHOTOS_TEST_DSM_ADMIN", "1").strip() == "1"
    if not username or not DSM_USER_RE.match(username):
        return False
    # Optional test override for admin membership when exercising webapi auth outside a Synology NAS
    test_admins = os.environ.get("GPHOTOS_TEST_ADMIN_USERS", "").strip()
    if test_admins:
        allowed = {u.strip().lower() for u in test_admins.split(",") if u.strip()}
        return username.lower() in allowed
    try:
        admin_grp = grp.getgrnam("administrators")
        if username in admin_grp.gr_mem:
            return True
    except KeyError:
        pass
    return username == "admin"


def sanitize_header_part(value):
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
        sk = sanitize_header_part(k)
        sv = sanitize_header_part(v)
        if sk is None or sv is None:
            continue
        out.write(("%s: %s\r\n" % (sk, sv)).encode("latin-1", errors="ignore"))
    out.write(("Content-Length: %d\r\n" % len(body)).encode("ascii"))
    out.write(b"\r\n")
    out.write(body)
    out.flush()


def parse_cookies(cookie_header):
    """Parse HTTP Cookie header into a {name: value} dict safely without rejecting DSM cookie names."""
    cookies = {}
    if not cookie_header:
        return cookies
    for part in cookie_header.split(";"):
        item = part.strip()
        if not item or "=" not in item:
            continue
        name, val = item.split("=", 1)
        name = name.strip()
        val = val.strip()
        if name:
            cookies[name] = val
    return cookies


def compute_dsm_session_fingerprint(cookie_header):
    """Compute a stable SHA-256 fingerprint of the active DSM session cookies.

    Binds the bridge cookie to the DSM `id` / `_SSID` session tokens so that logging out
    of DSM or switching DSM users invalidates any previously issued bridge cookie.
    """
    cookies = parse_cookies(cookie_header)
    id_val = cookies.get("id", "")
    ssid_val = cookies.get("_SSID", "")
    if id_val or ssid_val:
        material = "id=%s&_SSID=%s" % (id_val, ssid_val)
    else:
        filtered = sorted(
            (k, v) for k, v in cookies.items() if k != BRIDGE_COOKIE_NAME
        )
        if not filtered:
            return ""
        material = "&".join("%s=%s" % (k, v) for k, v in filtered)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _loopback_ssl_context(url):
    """Build SSL context for DSM webapi probe.

    DSM's local loopback HTTPS listener (`https://localhost:5001` / `https://127.0.0.1:5001`)
    presents DSM's self-signed certificate by default, which is not signed by a public CA.
    Skip certificate verification strictly for loopback HTTPS URLs.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() != "https":
        return None
    host = (parsed.hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        return ssl._create_unverified_context()
    return ssl.create_default_context()


def validate_dsm_session_via_webapi(cookie_header, syno_token=None):
    """Validate browser DSM session cookies against DSM's local webapi.

    Returns `(is_valid: bool, server_username: str | None)`.
    When `syno_token` is supplied (from `SYNO.SDS.Session.SynoToken`), it is sent as
    both `SynoToken` query param and `X-SYNO-TOKEN` header so DSM's `Initdata` returns
    the full `Session.user` identity even for foreign-IP sessions.
    Fails closed on missing cookies, HTTP/network errors, or DSM auth error codes.
    """
    if not cookie_header or not cookie_header.strip():
        return False, None

    # Strip our own bridge cookie before forwarding to DSM webapi
    cookies = parse_cookies(cookie_header)
    dsm_pairs = ["%s=%s" % (k, v) for k, v in cookies.items() if k != BRIDGE_COOKIE_NAME]
    if not dsm_pairs:
        return False, None
    forward_cookie = "; ".join(dsm_pairs)

    clean_token = None
    if syno_token:
        tok = sanitize_header_part(str(syno_token).strip())
        if tok:
            clean_token = tok

    base_url = get_dsm_webapi_url()
    ssl_ctx = _loopback_ssl_context(base_url)
    # Bypass any ambient HTTP_PROXY so loopback DSM webapi calls stay strictly local
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl_ctx) if ssl_ctx is not None else urllib.request.HTTPSHandler(),
    )

    for api_name, version, method in _WEBAPI_PROBES:
        params = {"api": api_name, "version": version, "method": method}
        if clean_token:
            params["SynoToken"] = clean_token
        qs = urllib.parse.urlencode(params)
        sep = "&" if "?" in base_url else "?"
        url = base_url + sep + qs
        req_headers = {
            "Cookie": forward_cookie,
            "Accept": "application/json",
        }
        if clean_token:
            req_headers["X-SYNO-TOKEN"] = clean_token
        req = urllib.request.Request(
            url,
            headers=req_headers,
            method="GET",
        )
        try:
            with opener.open(req, timeout=5) as resp:
                if resp.status != 200:
                    return False, None
                raw = resp.read(2 * 1024 * 1024)
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            return False, None

        if isinstance(payload, dict) and payload.get("success") is True:
            data = payload.get("data")
            server_user = None
            if isinstance(data, dict):
                for key in ("username", "user", "account"):
                    cand = data.get(key)
                    if isinstance(cand, str) and is_valid_dsm_username(cand.strip()):
                        server_user = cand.strip()
                        break
                if server_user is None:
                    session_obj = data.get("Session")
                    if isinstance(session_obj, dict):
                        for key in ("user", "username", "account"):
                            cand = session_obj.get(key)
                            if isinstance(cand, str) and is_valid_dsm_username(cand.strip()):
                                server_user = cand.strip()
                                break
            return True, server_user

        # Any DSM error from a non-final probe (including 119 on NormalUser/Timeout due to
        # loopback source-IP binding) falls through to the next probe; only failing ALL probes
        # (including Initdata) fails closed.
        continue

    return False, None


def _bridge_file_path(token):
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return os.path.join(get_bridge_dir(), token_hash + ".json")


def _prune_expired_bridges(bridge_dir, now):
    try:
        entries = os.listdir(bridge_dir)
    except OSError:
        return
    # Opportunistic bounded cleanup so expired bridge files never accumulate
    for name in entries[:64]:
        if not name.endswith(".json"):
            continue
        path = os.path.join(bridge_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                rec = json.load(fh)
            if float(rec.get("expires_at", 0)) <= now:
                os.unlink(path)
        except Exception:
            try:
                os.unlink(path)
            except OSError:
                pass


def load_valid_bridge(cookie_header):
    """Return `(username, is_admin)` if a valid, unexpired, DSM-session-bound bridge cookie exists."""
    cookies = parse_cookies(cookie_header)
    token = cookies.get(BRIDGE_COOKIE_NAME, "").strip()
    if not token or len(token) < 32:
        return None

    current_fp = compute_dsm_session_fingerprint(cookie_header)
    if not current_fp:
        return None

    path = _bridge_file_path(token)
    if not os.path.isfile(path):
        return None

    try:
        with open(path, "r", encoding="utf-8") as fh:
            rec = json.load(fh)
    except Exception:
        return None

    now = time.time()
    expires_at = float(rec.get("expires_at", 0))
    stored_fp = str(rec.get("dsm_fp", ""))
    username = str(rec.get("username", "")).strip()

    if expires_at <= now or not stored_fp or not hmac.compare_digest(stored_fp, current_fp):
        try:
            os.unlink(path)
        except OSError:
            pass
        return None

    if not is_valid_dsm_username(username) or not is_dsm_admin(username):
        try:
            os.unlink(path)
        except OSError:
            pass
        return None

    return username, True


def mint_bridge_cookie(username, dsm_fp, is_https):
    """Persist a server-side bridge session bound to `dsm_fp` and return a Set-Cookie header value."""
    bridge_dir = get_bridge_dir()
    os.makedirs(bridge_dir, mode=0o700, exist_ok=True)
    try:
        os.chmod(bridge_dir, 0o700)
    except OSError:
        pass

    now = time.time()
    _prune_expired_bridges(bridge_dir, now)

    token = secrets.token_hex(32)
    path = _bridge_file_path(token)
    tmp_path = path + ".tmp.%d" % os.getpid()
    record = {
        "username": username,
        "is_admin": True,
        "dsm_fp": dsm_fp,
        "created_at": now,
        "expires_at": now + get_bridge_ttl_seconds(),
    }
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    os.replace(tmp_path, path)

    attrs = [
        "%s=%s" % (BRIDGE_COOKIE_NAME, token),
        "Path=%s/" % UI_PREFIX,
        "HttpOnly",
        "SameSite=Lax",
        "Max-Age=%d" % get_bridge_ttl_seconds(),
    ]
    if is_https:
        attrs.append("Secure")
    return "; ".join(attrs)


def authenticate_proxy_request(desktop_dsm_user, is_https, syno_token=None):
    """Authenticate a `proxy.cgi` request.

    Returns `(username, is_admin, set_cookie_header_or_None, error_code_or_None)`.
    `error_code` is one of: `None`, `"invalid_session"`, `"missing_user"`, `"user_mismatch"`, `"not_admin"`.
    """
    if is_skip_auth_test_mode():
        user = os.environ.get("GPHOTOS_TEST_DSM_USER", "admin").strip()
        if not is_valid_dsm_username(user):
            return None, False, None, "invalid_session"
        return user, is_dsm_admin(user), None, None

    hint_user = (desktop_dsm_user or "").strip()
    cookie_header = os.environ.get("HTTP_COOKIE", "")
    bridge = load_valid_bridge(cookie_header)
    if bridge is not None:
        username, admin_flag = bridge
        if hint_user and username.lower() != hint_user.lower():
            return None, False, None, "user_mismatch"
        return username, admin_flag, None, None

    valid_session, server_user = validate_dsm_session_via_webapi(
        cookie_header, syno_token=syno_token
    )
    if not valid_session:
        return None, False, None, "invalid_session"

    if server_user and hint_user and server_user.lower() != hint_user.lower():
        return None, False, None, "user_mismatch"

    candidate_user = server_user or hint_user
    if not is_valid_dsm_username(candidate_user):
        return None, False, None, "missing_user"

    if not is_dsm_admin(candidate_user):
        return None, False, None, "not_admin"

    dsm_fp = compute_dsm_session_fingerprint(cookie_header)
    set_cookie = mint_bridge_cookie(candidate_user, dsm_fp, is_https) if dsm_fp else None
    return candidate_user, True, set_cookie, None


def authenticate_oauth_callback():
    """Authenticate an `oauth_callback.cgi` top-level navigation.

    Returns `(authenticated: bool, username: str | None, is_admin: bool)`.
    - If a valid bridge cookie is present -> `(True, username, is_admin)`.
    - Otherwise validates the DSM session via webapi -> `(True, server_user_or_None, is_admin_if_user)`.
      When `username` is None, the backend resolves the initiating user from server-side OAuth state.
    - Invalid/expired DSM session -> `(False, None, False)`.
    """
    if is_skip_auth_test_mode():
        user = os.environ.get("GPHOTOS_TEST_DSM_USER", "admin").strip()
        if not is_valid_dsm_username(user):
            return False, None, False
        return True, user, is_dsm_admin(user)

    cookie_header = os.environ.get("HTTP_COOKIE", "")
    bridge = load_valid_bridge(cookie_header)
    if bridge is not None:
        username, admin_flag = bridge
        return True, username, admin_flag

    valid_session, server_user = validate_dsm_session_via_webapi(cookie_header)
    if not valid_session:
        return False, None, False

    if server_user and is_valid_dsm_username(server_user):
        return True, server_user, is_dsm_admin(server_user)
    return True, None, False

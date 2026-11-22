"""Tests for DSM 7.3+ webapi session validation, desktop identity, bridge cookie, and OAuth callback."""

from __future__ import annotations

import ast
import http.server
import json
import os
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

from gphotos_synology.config import Settings
from gphotos_synology.main import create_app
from gphotos_synology.sources.gdrive import GoogleDriveClient

REPO_ROOT = Path(__file__).resolve().parent.parent
UI_DIR = REPO_ROOT / "synology-spk" / "ui"
PROXY_CGI = UI_DIR / "proxy.cgi"
OAUTH_CGI = UI_DIR / "oauth_callback.cgi"
DSM_AUTH_PY = UI_DIR / "dsm_auth.py"


class _MockDSMWebAPI:
    """In-process mock of DSM 7.3's /webapi/entry.cgi for testing cookie validation."""

    def __init__(
        self,
        *,
        mode: str = "initdata_only",
        valid_cookies: set[str] | None = None,
        session_user: str = "odanylch",
    ) -> None:
        self.mode = mode  # "normal_user" | "timeout_only" | "initdata_only" | "dsm73_loopback_119" | "dsm73_initdata_session_user"
        self.valid_cookies: set[str] = valid_cookies or {"_SSID=valid-ssid; id=valid-id-1"}
        self.session_user = session_user
        self.calls: list[dict[str, Any]] = []
        self._server: socketserver.TCPServer | None = None
        self._thread: threading.Thread | None = None
        self.url: str = ""

    def __enter__(self) -> _MockDSMWebAPI:
        parent = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_GET(self) -> None:
                parsed = urllib.parse.urlsplit(self.path)
                qs = urllib.parse.parse_qs(parsed.query)
                api = (qs.get("api") or [""])[0]
                syno_token_qs = (qs.get("SynoToken") or [""])[0]
                syno_token_hdr = self.headers.get("X-SYNO-TOKEN", "")
                cookie = self.headers.get("Cookie", "")
                parent.calls.append(
                    {
                        "api": api,
                        "cookie": cookie,
                        "syno_token_qs": syno_token_qs,
                        "syno_token_hdr": syno_token_hdr,
                    }
                )

                # Check if any known valid cookie token is present
                is_valid = any(tok in cookie for tok in parent.valid_cookies)
                if not is_valid:
                    body = json.dumps({"success": False, "error": {"code": 119}}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if parent.mode == "normal_user" and api == "SYNO.Core.NormalUser":
                    payload = {"success": True, "data": {"username": "oleksandr"}}
                elif parent.mode == "timeout_only" and api == "SYNO.Core.Desktop.Timeout":
                    payload = {"success": True, "data": {"timeout": 15}}
                elif parent.mode == "initdata_only" and api == "SYNO.Core.Desktop.Initdata":
                    payload = {"success": True, "data": {"Session": {"SynoToken": "tok"}}}
                elif parent.mode == "dsm73_loopback_119":
                    if api == "SYNO.Core.Desktop.Initdata":
                        payload = {"success": True, "data": {"Session": {"SynoToken": "tok"}}}
                    else:
                        # Real DSM 7.3.2 behavior: NormalUser & Timeout return 119 from loopback
                        payload = {"success": False, "error": {"code": 119}}
                elif parent.mode == "dsm73_initdata_session_user":
                    if api == "SYNO.Core.Desktop.Initdata":
                        payload = {
                            "success": True,
                            "data": {
                                "Session": {
                                    "SynoToken": "tok",
                                    "user": parent.session_user,
                                    "is_admin": True,
                                }
                            },
                        }
                    else:
                        payload = {"success": False, "error": {"code": 119}}
                elif parent.mode == "dsm73_foreign_synotoken_gated":
                    if api == "SYNO.Core.Desktop.Initdata":
                        if syno_token_qs == "live-syno-token" and syno_token_hdr == "live-syno-token":
                            payload = {
                                "success": True,
                                "data": {
                                    "Session": {
                                        "SynoToken": "live-syno-token",
                                        "user": parent.session_user,
                                        "is_admin": True,
                                    }
                                },
                            }
                        else:
                            # Real DSM 7.3 foreign session without SynoToken: degraded payload
                            payload = {
                                "success": True,
                                "data": {"Session": {"user": None, "is_admin": False}},
                            }
                    else:
                        payload = {"success": False, "error": {"code": 119}}
                else:
                    # Simulate API not present -> triggers fallback probe
                    payload = {"success": False, "error": {"code": 102}}

                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        socketserver.TCPServer.allow_reuse_address = True
        self._server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{port}/webapi/entry.cgi"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()


class _LiveBackend:
    """Minimal live uvicorn server for real CGI-to-FastAPI end-to-end tests."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.url = ""
        self._server: Any = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> _LiveBackend:
        import socket

        import uvicorn

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        self.url = f"http://127.0.0.1:{port}"
        config = uvicorn.Config(self.app, host="127.0.0.1", port=port, log_level="error")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if self._server.started:
                break
            time.sleep(0.05)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._server:
            self._server.should_exit = True
        if self._thread:
            self._thread.join(timeout=5.0)


def _exec_cgi(
    script: Path,
    *,
    backend_url: str,
    dsm_webapi_url: str,
    bridge_dir: Path,
    proxy_secret: str,
    query_string: str = "",
    cookie: str = "",
    method: str = "GET",
    admin_users: str = "oleksandr,admin",
    bridge_ttl: int = 1800,
    extra_env: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    env = dict(os.environ)
    env.pop("GPHOTOS_SKIP_DSM_AUTH", None)
    env.update(
        {
            "GPHOTOS_FORCE_WEBAPI_AUTH": "1",
            "GPHOTOS_BACKEND_URL": backend_url,
            "GPHOTOS_DSM_WEBAPI_URL": dsm_webapi_url,
            "GPHOTOS_BRIDGE_DIR": str(bridge_dir),
            "GPHOTOS_BRIDGE_TTL_SECONDS": str(bridge_ttl),
            "GPHOTOS_PROXY_SECRET": proxy_secret,
            "GPHOTOS_TEST_ADMIN_USERS": admin_users,
            "REQUEST_METHOD": method,
            "QUERY_STRING": query_string,
            "HTTP_COOKIE": cookie,
            "HTTP_HOST": "nas.local:5001",
            "SERVER_PORT": "5001",
            "HTTPS": "on",
        }
    )
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, str(script)],
        env=env,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert proc.returncode == 0, f"CGI crashed: {proc.stderr.decode('utf-8', errors='replace')}"
    raw = proc.stdout
    sep = b"\r\n\r\n"
    assert sep in raw, f"Missing CGI header/body separator in {raw!r}"
    head, body = raw.split(sep, 1)
    headers: dict[str, str] = {}
    status = 200
    for line in head.decode("latin-1").split("\r\n"):
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        kl = k.strip().lower()
        if kl == "status":
            status = int(v.strip().split()[0])
        else:
            headers[kl] = v.strip()
    return status, headers, body


def _extract_bridge_cookie(set_cookie: str) -> str:
    assert "gphotos_dsm_bridge=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=Lax" in set_cookie
    assert "Path=/webman/3rdparty/gphotos-migrator/" in set_cookie
    assert "Secure" in set_cookie
    return set_cookie.split(";", 1)[0].strip()


def test_dsm73_cgi_scripts_are_python38_compatible() -> None:
    """Synology DSM 7.3 ships Python 3.8; ensure no 3.10+ syntax sneaks into UI scripts."""
    for path in (DSM_AUTH_PY, PROXY_CGI, OAUTH_CGI):
        source = path.read_text(encoding="utf-8")
        ast.parse(source, filename=str(path), feature_version=(3, 8))


def test_ui_config_registers_single_desktop_app_and_notify_strings() -> None:
    """Ensure ui/config registers a single clean DSM desktop app (no duplicate .url) and notification strings."""
    cfg = json.loads((UI_DIR / "config").read_text(encoding="utf-8"))
    desktop = cfg["GPhotosMigrator.js"]["SYNO.SDS.GPhotosMigrator.Application"]
    assert desktop["type"] == "app"
    assert desktop["allUsers"] is True
    assert ".url" not in cfg

    strings_txt = (UI_DIR / "texts" / "enu" / "strings").read_text(encoding="utf-8")
    assert "[notification]" in strings_txt
    assert "update_ready_title" in strings_txt
    assert "update_check_failed_title" in strings_txt


def test_dsm73_webapi_validation_bridge_expiry_and_session_change(tmp_path: Path) -> None:
    """Test valid _SSID session via Initdata fallback, invalid/expired 403, missing user,
    non-admin rejection, bridge cookie reuse, expiry, and invalidation on DSM session switch."""
    secret = "dsm73-test-secret"
    bridge_dir = tmp_path / "sessions"
    settings = Settings(
        data_dir=tmp_path / "config",
        work_dir=tmp_path / "work",
        inbox_dir=tmp_path / "inbox",
        photos_dir=tmp_path / "photos",
        proxy_secret=secret,
        min_free_disk_gb=0.1,
    )
    app = create_app(base_settings=settings, start_worker=False)

    dsm_cookie_a = "_SSID=ssid-browser-1; id=dsm-id-alpha; did=device-1; stay_login=1"
    dsm_cookie_b = "_SSID=ssid-browser-2; id=dsm-id-beta; did=device-1; stay_login=1"

    with (
        _LiveBackend(app) as backend,
        _MockDSMWebAPI(mode="initdata_only", valid_cookies={"id=dsm-id-alpha", "id=dsm-id-beta"}) as webapi,
    ):
        # 1. Invalid / missing DSM cookie -> 403
        st, _hdrs, _body = _exec_cgi(
            PROXY_CGI,
            backend_url=backend.url,
            dsm_webapi_url=webapi.url,
            bridge_dir=bridge_dir,
            proxy_secret=secret,
            query_string="dsm_user=oleksandr",
            cookie="id=expired-session",
        )
        assert st == 403

        # 2. Valid DSM session but missing desktop username (Initdata has no user) -> 403 explanatory HTML
        st, hdrs, body = _exec_cgi(
            PROXY_CGI,
            backend_url=backend.url,
            dsm_webapi_url=webapi.url,
            bridge_dir=bridge_dir,
            proxy_secret=secret,
            query_string="",
            cookie=dsm_cookie_a,
        )
        assert st == 403
        assert "DSM User Identity Missing" in body.decode("utf-8")
        assert "set-cookie" not in hdrs

        # 3. Valid DSM session with asserted non-admin user -> 403 rejected
        st, hdrs, body = _exec_cgi(
            PROXY_CGI,
            backend_url=backend.url,
            dsm_webapi_url=webapi.url,
            bridge_dir=bridge_dir,
            proxy_secret=secret,
            query_string="dsm_user=guestuser",
            cookie=dsm_cookie_a,
        )
        assert st == 403
        assert "Administrator Access Required" in body.decode("utf-8")
        assert "set-cookie" not in hdrs

        # 4. Valid _SSID browser session + desktop admin user -> 200 dashboard + bridge cookie minted
        webapi.calls.clear()
        st, hdrs, body = _exec_cgi(
            PROXY_CGI,
            backend_url=backend.url,
            dsm_webapi_url=webapi.url,
            bridge_dir=bridge_dir,
            proxy_secret=secret,
            query_string="dsm_user=oleksandr",
            cookie=dsm_cookie_a,
        )
        assert st == 200
        assert b"Google Photos" in body
        assert len(webapi.calls) == 3  # NormalUser(102) -> Timeout(102) -> Initdata(ok)
        bridge_pair = _extract_bridge_cookie(hdrs["set-cookie"])

        # 5. Subsequent API call carries bridge cookie (NO dsm_user query param) -> skips webapi!
        webapi.calls.clear()
        st, hdrs, body = _exec_cgi(
            PROXY_CGI,
            backend_url=backend.url,
            dsm_webapi_url=webapi.url,
            bridge_dir=bridge_dir,
            proxy_secret=secret,
            query_string="p=/api/status",
            cookie=f"{dsm_cookie_a}; {bridge_pair}",
        )
        assert st == 200
        assert json.loads(body)["photos_dir_writable"] is True
        assert len(webapi.calls) == 0  # Bridge cookie avoided hitting DSM webapi

        # 6. DSM session changes (logout / user switch: id changes to beta) -> bridge cookie INVALIDATED!
        webapi.calls.clear()
        st, _hdrs, _body = _exec_cgi(
            PROXY_CGI,
            backend_url=backend.url,
            dsm_webapi_url=webapi.url,
            bridge_dir=bridge_dir,
            proxy_secret=secret,
            query_string="p=/api/status",
            cookie=f"{dsm_cookie_b}; {bridge_pair}",
        )
        # Re-validation ran (webapi called), and because no desktop dsm_user was passed on API call -> 403
        assert st == 403
        assert len(webapi.calls) > 0

        # 7. Expired bridge cookie -> re-validates via webapi and mints fresh bridge cookie
        st, hdrs, _body = _exec_cgi(
            PROXY_CGI,
            backend_url=backend.url,
            dsm_webapi_url=webapi.url,
            bridge_dir=bridge_dir,
            proxy_secret=secret,
            query_string="dsm_user=oleksandr",
            cookie=dsm_cookie_a,
            bridge_ttl=1,
        )
        assert st == 200
        short_bridge = _extract_bridge_cookie(hdrs["set-cookie"])
        time.sleep(1.2)
        webapi.calls.clear()
        st, _hdrs, _body = _exec_cgi(
            PROXY_CGI,
            backend_url=backend.url,
            dsm_webapi_url=webapi.url,
            bridge_dir=bridge_dir,
            proxy_secret=secret,
            query_string="p=/api/status",
            cookie=f"{dsm_cookie_a}; {short_bridge}",
        )
        assert st == 403
        assert len(webapi.calls) > 0


def test_dsm73_oauth_callback_via_bridge_and_fresh_webapi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """oauth_callback.cgi succeeds both with a bridge cookie AND via fresh webapi validation
    (resolving the initiating user from server-side OAuth state), and still blocks replay."""
    secret = "dsm73-oauth-secret"
    bridge_dir = tmp_path / "sessions"
    settings = Settings(
        data_dir=tmp_path / "config",
        work_dir=tmp_path / "work",
        inbox_dir=tmp_path / "inbox",
        photos_dir=tmp_path / "photos",
        proxy_secret=secret,
        google_client_id="cid.apps.googleusercontent.com",
        google_client_secret="csec",
        min_free_disk_gb=0.1,
    )
    app = create_app(base_settings=settings, start_worker=False)

    exchanged: list[tuple[str, str]] = []

    def _fake_exchange(
        self: GoogleDriveClient, *, code: str, redirect_uri: str, code_verifier: str | None = None
    ) -> None:
        exchanged.append((code, redirect_uri))

    monkeypatch.setattr(GoogleDriveClient, "exchange_code", _fake_exchange)

    dsm_cookie = "_SSID=ssid-oauth; id=dsm-id-oauth"

    with (
        _LiveBackend(app) as backend,
        _MockDSMWebAPI(mode="timeout_only", valid_cookies={"id=dsm-id-oauth"}) as webapi,
    ):
        # Mint bridge cookie via proxy.cgi
        st, hdrs, _body = _exec_cgi(
            PROXY_CGI,
            backend_url=backend.url,
            dsm_webapi_url=webapi.url,
            bridge_dir=bridge_dir,
            proxy_secret=secret,
            query_string="dsm_user=oleksandr",
            cookie=dsm_cookie,
        )
        assert st == 200
        bridge_pair = _extract_bridge_cookie(hdrs["set-cookie"])

        # Flow 1: Start OAuth -> complete callback WITH bridge cookie
        st, _hdrs, body = _exec_cgi(
            PROXY_CGI,
            backend_url=backend.url,
            dsm_webapi_url=webapi.url,
            bridge_dir=bridge_dir,
            proxy_secret=secret,
            query_string="p=/api/oauth/google/start",
            cookie=f"{dsm_cookie}; {bridge_pair}",
        )
        assert st == 200
        start1 = json.loads(body)
        state1 = urllib.parse.parse_qs(urllib.parse.urlsplit(start1["authorization_url"]).query)["state"][0]

        webapi.calls.clear()
        st, hdrs, _body = _exec_cgi(
            OAUTH_CGI,
            backend_url=backend.url,
            dsm_webapi_url=webapi.url,
            bridge_dir=bridge_dir,
            proxy_secret=secret,
            query_string=f"code=code-bridge&state={urllib.parse.quote(state1)}",
            cookie=f"{dsm_cookie}; {bridge_pair}",
        )
        assert st == 302
        assert "oauth=success" in hdrs.get("location", "")
        assert len(webapi.calls) == 0  # Used bridge cookie
        assert exchanged[-1][0] == "code-bridge"

        # Flow 2: Start OAuth -> complete callback WITHOUT bridge cookie (fresh webapi validation)
        st, _hdrs, body = _exec_cgi(
            PROXY_CGI,
            backend_url=backend.url,
            dsm_webapi_url=webapi.url,
            bridge_dir=bridge_dir,
            proxy_secret=secret,
            query_string="p=/api/oauth/google/start",
            cookie=f"{dsm_cookie}; {bridge_pair}",
        )
        assert st == 200
        start2 = json.loads(body)
        state2 = urllib.parse.parse_qs(urllib.parse.urlsplit(start2["authorization_url"]).query)["state"][0]

        webapi.calls.clear()
        st, hdrs, _body = _exec_cgi(
            OAUTH_CGI,
            backend_url=backend.url,
            dsm_webapi_url=webapi.url,
            bridge_dir=bridge_dir,
            proxy_secret=secret,
            query_string=f"code=code-fresh&state={urllib.parse.quote(state2)}",
            cookie=dsm_cookie,  # No bridge cookie!
        )
        assert st == 302
        assert "oauth=success" in hdrs.get("location", "")
        assert len(webapi.calls) > 0  # Validated via DSM webapi
        assert exchanged[-1][0] == "code-fresh"

        # Replay of state2 must still fail closed!
        st, hdrs, _body = _exec_cgi(
            OAUTH_CGI,
            backend_url=backend.url,
            dsm_webapi_url=webapi.url,
            bridge_dir=bridge_dir,
            proxy_secret=secret,
            query_string=f"code=code-replay&state={urllib.parse.quote(state2)}",
            cookie=dsm_cookie,
        )
        assert st == 302
        assert "oauth=error" in hdrs.get("location", "")

        # Invalid DSM session on oauth_callback.cgi -> 403
        st, _hdrs, _body = _exec_cgi(
            OAUTH_CGI,
            backend_url=backend.url,
            dsm_webapi_url=webapi.url,
            bridge_dir=bridge_dir,
            proxy_secret=secret,
            query_string=f"code=code-bad&state={urllib.parse.quote(state2)}",
            cookie="id=invalid-session",
        )
        assert st == 403


def test_dsm73_probe_chain_falls_through_on_119_and_short_circuits_on_normal_user(
    tmp_path: Path,
) -> None:
    """Regression for 1.2.0-0002 live bug: NormalUser/Timeout returning 119 from loopback
    must fall through to Initdata; all-probes-119 still returns 403; NormalUser returning
    username short-circuits on probe 1 without needing desktop dsm_user."""
    secret = "dsm73-119-secret"
    bridge_dir = tmp_path / "sessions"
    settings = Settings(
        data_dir=tmp_path / "config",
        work_dir=tmp_path / "work",
        inbox_dir=tmp_path / "inbox",
        photos_dir=tmp_path / "photos",
        proxy_secret=secret,
        min_free_disk_gb=0.1,
    )
    app = create_app(base_settings=settings, start_worker=False)
    valid_cookie = "_SSID=real-browser-ssid; id=real-browser-id; did=dev1; stay_login=1"

    with _LiveBackend(app) as backend:
        # 1. DSM 7.3 loopback behavior: NormalUser -> 119, Timeout -> 119, Initdata -> success
        with _MockDSMWebAPI(mode="dsm73_loopback_119", valid_cookies={"id=real-browser-id"}) as webapi:
            st, hdrs, body = _exec_cgi(
                PROXY_CGI,
                backend_url=backend.url,
                dsm_webapi_url=webapi.url,
                bridge_dir=bridge_dir,
                proxy_secret=secret,
                query_string="dsm_user=oleksandr",
                cookie=valid_cookie,
            )
            assert st == 200
            assert b"Google Photos" in body
            assert [c["api"] for c in webapi.calls] == [
                "SYNO.Core.NormalUser",
                "SYNO.Core.Desktop.Timeout",
                "SYNO.Core.Desktop.Initdata",
            ]
            _extract_bridge_cookie(hdrs["set-cookie"])

            # Invalid session fails all three probes (including Initdata -> 119) -> 403
            webapi.calls.clear()
            st_bad, _hdrs_bad, _body_bad = _exec_cgi(
                PROXY_CGI,
                backend_url=backend.url,
                dsm_webapi_url=webapi.url,
                bridge_dir=bridge_dir,
                proxy_secret=secret,
                query_string="dsm_user=oleksandr",
                cookie="id=expired-or-forged",
            )
            assert st_bad == 403
            assert [c["api"] for c in webapi.calls] == [
                "SYNO.Core.NormalUser",
                "SYNO.Core.Desktop.Timeout",
                "SYNO.Core.Desktop.Initdata",
            ]

        # 2. When NormalUser succeeds and returns data.username, probe 1 short-circuits
        with _MockDSMWebAPI(mode="normal_user", valid_cookies={"id=real-browser-id"}) as webapi:
            st_nu, hdrs_nu, body_nu = _exec_cgi(
                PROXY_CGI,
                backend_url=backend.url,
                dsm_webapi_url=webapi.url,
                bridge_dir=bridge_dir,
                proxy_secret=secret,
                query_string="",  # No desktop dsm_user needed when NormalUser supplies username
                cookie=valid_cookie,
            )
            assert st_nu == 200
            assert b"Google Photos" in body_nu
            assert [c["api"] for c in webapi.calls] == ["SYNO.Core.NormalUser"]
            _extract_bridge_cookie(hdrs_nu["set-cookie"])


def test_dsm73_standalone_initdata_session_user_and_hint_mismatch(tmp_path: Path) -> None:
    """Regression for v1.2.0-0004 standalone missing_user bug + hint override security fix:
    1. 119 / 119 / Initdata(Session.user='odanylch') authenticates standalone with NO dsm_user hint
       and mints a bridge cookie.
    2. A mismatched ?dsm_user=<other> hint is rejected (fail closed) and mints NO bridge cookie,
       preventing privilege escalation via ?dsm_user=<admin>.
    3. A mismatched hint is also rejected when a valid bridge cookie is present."""
    secret = "dsm73-standalone-secret"
    bridge_dir = tmp_path / "sessions"
    settings = Settings(
        data_dir=tmp_path / "config",
        work_dir=tmp_path / "work",
        inbox_dir=tmp_path / "inbox",
        photos_dir=tmp_path / "photos",
        proxy_secret=secret,
        min_free_disk_gb=0.1,
    )
    app = create_app(base_settings=settings, start_worker=False)
    valid_cookie = "_SSID=phone-ssid; id=phone-session-id; did=phone-dev; stay_login=1"

    with _LiveBackend(app) as backend:
        # 1. Standalone admin session: Initdata returns data.Session.user = "odanylch", NO hint
        with _MockDSMWebAPI(
            mode="dsm73_initdata_session_user",
            valid_cookies={"id=phone-session-id"},
            session_user="odanylch",
        ) as webapi:
            st, hdrs, body = _exec_cgi(
                PROXY_CGI,
                backend_url=backend.url,
                dsm_webapi_url=webapi.url,
                bridge_dir=bridge_dir,
                proxy_secret=secret,
                query_string="",  # Standalone phone/bookmark access: NO dsm_user hint!
                cookie=valid_cookie,
                admin_users="odanylch,admin",
            )
            assert st == 200
            assert b"Google Photos" in body
            assert [c["api"] for c in webapi.calls] == [
                "SYNO.Core.NormalUser",
                "SYNO.Core.Desktop.Timeout",
                "SYNO.Core.Desktop.Initdata",
            ]
            bridge_pair = _extract_bridge_cookie(hdrs["set-cookie"])

            # Subsequent API call using the newly minted bridge cookie skips webapi
            webapi.calls.clear()
            st_api, _hdrs_api, body_api = _exec_cgi(
                PROXY_CGI,
                backend_url=backend.url,
                dsm_webapi_url=webapi.url,
                bridge_dir=bridge_dir,
                proxy_secret=secret,
                query_string="p=/api/status",
                cookie=f"{valid_cookie}; {bridge_pair}",
                admin_users="odanylch,admin",
            )
            assert st_api == 200
            assert json.loads(body_api)["photos_dir_writable"] is True
            assert len(webapi.calls) == 0

            # Mismatched hint WITH valid bridge cookie -> 403 fail closed
            st_bm, hdrs_bm, body_bm = _exec_cgi(
                PROXY_CGI,
                backend_url=backend.url,
                dsm_webapi_url=webapi.url,
                bridge_dir=bridge_dir,
                proxy_secret=secret,
                query_string="dsm_user=admin",
                cookie=f"{valid_cookie}; {bridge_pair}",
                admin_users="odanylch,admin",
            )
            assert st_bm == 403
            assert "DSM User Identity Mismatch" in body_bm.decode("utf-8")
            assert "set-cookie" not in hdrs_bm

            # Mismatched hint WITHOUT bridge cookie -> 403 fail closed, no bridge minted
            st_mm, hdrs_mm, body_mm = _exec_cgi(
                PROXY_CGI,
                backend_url=backend.url,
                dsm_webapi_url=webapi.url,
                bridge_dir=bridge_dir,
                proxy_secret=secret,
                query_string="dsm_user=admin",
                cookie=valid_cookie,
                admin_users="odanylch,admin",
            )
            assert st_mm == 403
            assert "DSM User Identity Mismatch" in body_mm.decode("utf-8")
            assert "set-cookie" not in hdrs_mm

        # 2. Non-admin DSM session trying to escalate via ?dsm_user=admin -> 403 fail closed!
        with _MockDSMWebAPI(
            mode="dsm73_initdata_session_user",
            valid_cookies={"id=guest-session-id"},
            session_user="guestuser",
        ) as webapi:
            st_esc, hdrs_esc, body_esc = _exec_cgi(
                PROXY_CGI,
                backend_url=backend.url,
                dsm_webapi_url=webapi.url,
                bridge_dir=bridge_dir,
                proxy_secret=secret,
                query_string="dsm_user=admin",
                cookie="_SSID=guest-ssid; id=guest-session-id",
                admin_users="odanylch,admin",
            )
            assert st_esc == 403
            assert "DSM User Identity Mismatch" in body_esc.decode("utf-8")
            assert "set-cookie" not in hdrs_esc


def test_dsm73_synotoken_upgrades_degraded_initdata_and_enforces_crosscheck(tmp_path: Path) -> None:
    """Regression for foreign-session Initdata degradation:
    - Without SynoToken, Initdata returns Session.user=None -> standalone fails closed (403 missing_user).
    - With syno_token (forwarded by GPhotosMigrator.js), dsm_auth sends SynoToken + X-SYNO-TOKEN,
      receives full Session.user, authenticates cleanly, mints bridge cookie, and rejects mismatched dsm_user."""
    secret = "dsm73-synotoken-secret"
    bridge_dir = tmp_path / "sessions"
    settings = Settings(
        data_dir=tmp_path / "config",
        work_dir=tmp_path / "work",
        inbox_dir=tmp_path / "inbox",
        photos_dir=tmp_path / "photos",
        proxy_secret=secret,
        min_free_disk_gb=0.1,
    )
    app = create_app(base_settings=settings, start_worker=False)
    valid_cookie = "_SSID=foreign-ssid; id=foreign-id; did=foreign-dev"

    with (
        _LiveBackend(app) as backend,
        _MockDSMWebAPI(
            mode="dsm73_foreign_synotoken_gated",
            valid_cookies={"id=foreign-id"},
            session_user="odanylch",
        ) as webapi,
    ):
        # 1. No SynoToken & no hint -> degraded Initdata -> 403 missing_user (fail closed)
        st_no_tok, hdrs_no_tok, body_no_tok = _exec_cgi(
            PROXY_CGI,
            backend_url=backend.url,
            dsm_webapi_url=webapi.url,
            bridge_dir=bridge_dir,
            proxy_secret=secret,
            query_string="",
            cookie=valid_cookie,
            admin_users="odanylch,admin",
        )
        assert st_no_tok == 403
        assert "DSM User Identity Missing" in body_no_tok.decode("utf-8")
        assert "set-cookie" not in hdrs_no_tok

        # 2. With syno_token + mismatched dsm_user=admin -> full Session.user='odanylch' -> 403 mismatch!
        webapi.calls.clear()
        st_mm, hdrs_mm, body_mm = _exec_cgi(
            PROXY_CGI,
            backend_url=backend.url,
            dsm_webapi_url=webapi.url,
            bridge_dir=bridge_dir,
            proxy_secret=secret,
            query_string="dsm_user=admin&syno_token=live-syno-token",
            cookie=valid_cookie,
            admin_users="odanylch,admin",
        )
        assert st_mm == 403
        assert "DSM User Identity Mismatch" in body_mm.decode("utf-8")
        assert "set-cookie" not in hdrs_mm
        assert all(c["syno_token_qs"] == "live-syno-token" for c in webapi.calls)
        assert all(c["syno_token_hdr"] == "live-syno-token" for c in webapi.calls)

        # 3. With syno_token + matching dsm_user=odanylch -> 200 + bridge cookie minted
        webapi.calls.clear()
        st_ok, hdrs_ok, body_ok = _exec_cgi(
            PROXY_CGI,
            backend_url=backend.url,
            dsm_webapi_url=webapi.url,
            bridge_dir=bridge_dir,
            proxy_secret=secret,
            query_string="dsm_user=odanylch&syno_token=live-syno-token",
            cookie=valid_cookie,
            admin_users="odanylch,admin",
        )
        assert st_ok == 200
        assert b"Google Photos" in body_ok
        _extract_bridge_cookie(hdrs_ok["set-cookie"])


def test_personal_mobile_access_link_create_use_cookie_and_revoke(tmp_path: Path) -> None:
    """End-to-end test for opt-in revocable personal mobile access links:
    1. Admin creates a token via POST /api/mobile-tokens.
    2. Phone opens proxy.cgi?token=<raw> with NO DSM cookies -> 200 + HttpOnly gphotos_mobile_token cookie.
    3. Follow-up API call using only gphotos_mobile_token cookie -> 200.
    4. Mismatched ?dsm_user=admin -> 403 fail closed; invalid token -> 403 fail closed.
    5. Revoking via DELETE /api/mobile-tokens/{id} immediately invalidates both URL and cookie."""
    secret = "dsm73-mobile-token-secret"
    bridge_dir = tmp_path / "sessions"
    settings = Settings(
        data_dir=tmp_path / "config",
        work_dir=tmp_path / "work",
        inbox_dir=tmp_path / "inbox",
        photos_dir=tmp_path / "photos",
        proxy_secret=secret,
        min_free_disk_gb=0.1,
    )
    app = create_app(base_settings=settings, start_worker=False)
    mobile_tokens_dir = str(settings.mobile_tokens_dir)
    extra_env = {"GPHOTOS_MOBILE_TOKENS_DIR": mobile_tokens_dir}

    with (
        _LiveBackend(app) as backend,
        _MockDSMWebAPI(
            mode="dsm73_foreign_synotoken_gated",
            valid_cookies={"id=admin-sess"},
            session_user="odanylch",
        ) as webapi,
    ):
        # Mint bridge via normal DSM session with SynoToken
        st, hdrs, _ = _exec_cgi(
            PROXY_CGI,
            backend_url=backend.url,
            dsm_webapi_url=webapi.url,
            bridge_dir=bridge_dir,
            proxy_secret=secret,
            query_string="dsm_user=odanylch&syno_token=live-syno-token",
            cookie="_SSID=s1; id=admin-sess",
            admin_users="odanylch,admin",
            extra_env=extra_env,
        )
        assert st == 200
        bridge_pair = _extract_bridge_cookie(hdrs["set-cookie"])

        # 1. Create personal mobile token via backend API
        import urllib.request

        req = urllib.request.Request(
            f"{backend.url}/api/mobile-tokens",
            data=json.dumps({"label": "iPhone"}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Proxy-Secret": secret,
                "X-DSM-User": "odanylch",
                "X-DSM-Admin": "1",
                "X-Requested-With": "XMLHttpRequest",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            created = json.loads(resp.read().decode("utf-8"))
        raw_token = created["token"]
        token_id = created["id"]
        assert len(raw_token) >= 32
        assert created["username"] == "odanylch"

        # 2. Phone opens proxy.cgi?token=<raw_token> with ZERO DSM cookies!
        st_mob, hdrs_mob, body_mob = _exec_cgi(
            PROXY_CGI,
            backend_url=backend.url,
            dsm_webapi_url=webapi.url,
            bridge_dir=bridge_dir,
            proxy_secret=secret,
            query_string=f"token={urllib.parse.quote(raw_token)}",
            cookie="",
            admin_users="odanylch,admin",
            extra_env=extra_env,
        )
        assert st_mob == 200
        assert b"Google Photos" in body_mob
        set_cookie = hdrs_mob["set-cookie"]
        assert "gphotos_mobile_token=" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "Secure" in set_cookie
        mobile_cookie_pair = set_cookie.split(";", 1)[0].strip()

        # 3. Follow-up API call using ONLY gphotos_mobile_token cookie (no DSM cookies)
        st_api, _hdrs_api, body_api = _exec_cgi(
            PROXY_CGI,
            backend_url=backend.url,
            dsm_webapi_url=webapi.url,
            bridge_dir=bridge_dir,
            proxy_secret=secret,
            query_string="p=/api/status",
            cookie=mobile_cookie_pair,
            admin_users="odanylch,admin",
            extra_env=extra_env,
        )
        assert st_api == 200
        assert json.loads(body_api)["photos_dir_writable"] is True

        # 4. Mismatched dsm_user hint with mobile token -> 403 fail closed
        st_mm, _hdrs_mm, _body_mm = _exec_cgi(
            PROXY_CGI,
            backend_url=backend.url,
            dsm_webapi_url=webapi.url,
            bridge_dir=bridge_dir,
            proxy_secret=secret,
            query_string=f"token={urllib.parse.quote(raw_token)}&dsm_user=admin",
            cookie="",
            admin_users="odanylch,admin",
            extra_env=extra_env,
        )
        assert st_mm == 403

        # Invalid/forged token -> 403 fail closed
        st_bad, _hdrs_bad, body_bad = _exec_cgi(
            PROXY_CGI,
            backend_url=backend.url,
            dsm_webapi_url=webapi.url,
            bridge_dir=bridge_dir,
            proxy_secret=secret,
            query_string="token=" + "a" * 43,
            cookie="",
            admin_users="odanylch,admin",
            extra_env=extra_env,
        )
        assert st_bad == 403
        assert "Mobile Access Link Revoked" in body_bad.decode("utf-8")

        # 5. Revoke token via DELETE /api/mobile-tokens/{id}
        del_req = urllib.request.Request(
            f"{backend.url}/api/mobile-tokens/{token_id}",
            headers={
                "X-Proxy-Secret": secret,
                "X-DSM-User": "odanylch",
                "X-DSM-Admin": "1",
                "X-Requested-With": "XMLHttpRequest",
            },
            method="DELETE",
        )
        with urllib.request.urlopen(del_req, timeout=5) as resp:
            assert json.loads(resp.read().decode("utf-8"))["ok"] is True

        # Both ?token= URL and existing cookie must immediately fail closed (403)!
        st_rev_url, _, _ = _exec_cgi(
            PROXY_CGI,
            backend_url=backend.url,
            dsm_webapi_url=webapi.url,
            bridge_dir=bridge_dir,
            proxy_secret=secret,
            query_string=f"token={urllib.parse.quote(raw_token)}",
            cookie="",
            admin_users="odanylch,admin",
            extra_env=extra_env,
        )
        assert st_rev_url == 403

        st_rev_cookie, _, _ = _exec_cgi(
            PROXY_CGI,
            backend_url=backend.url,
            dsm_webapi_url=webapi.url,
            bridge_dir=bridge_dir,
            proxy_secret=secret,
            query_string="p=/api/status",
            cookie=mobile_cookie_pair,
            admin_users="odanylch,admin",
            extra_env=extra_env,
        )
        assert st_rev_cookie == 403
        assert bridge_pair  # keep flake/unused quiet





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

    def __init__(self, *, mode: str = "initdata_only", valid_cookies: set[str] | None = None) -> None:
        self.mode = mode  # "normal_user" | "timeout_only" | "initdata_only"
        self.valid_cookies: set[str] = valid_cookies or {"_SSID=valid-ssid; id=valid-id-1"}
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
                cookie = self.headers.get("Cookie", "")
                parent.calls.append({"api": api, "cookie": cookie})

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

    def _fake_exchange(self: GoogleDriveClient, *, code: str, redirect_uri: str) -> None:
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

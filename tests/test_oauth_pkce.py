"""End-to-end regression test for OAuth2 PKCE code_verifier persistence through real Flow.fetch_token()."""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import socketserver
import threading
import urllib.parse
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from gphotos_synology.config import Settings
from gphotos_synology.main import create_app
from gphotos_synology.sources.gdrive import GoogleDriveClient


class _MockGoogleTokenEndpoint:
    """Local HTTP mock of https://oauth2.googleapis.com/token that enforces PKCE S256 verification."""

    def __init__(self) -> None:
        self.expected_challenge: str = ""
        self.requests: list[dict[str, list[str]]] = []
        self._server: socketserver.TCPServer | None = None
        self._thread: threading.Thread | None = None
        self.url: str = ""

    def __enter__(self) -> _MockGoogleTokenEndpoint:
        parent = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw_body = self.rfile.read(length).decode("utf-8")
                form = urllib.parse.parse_qs(raw_body)
                form["_auth_header"] = [self.headers.get("Authorization", "")]
                parent.requests.append(form)

                verifier = (form.get("code_verifier") or [""])[0]
                if not verifier:
                    err = json.dumps(
                        {"error": "invalid_grant", "error_description": "Missing code verifier."}
                    ).encode("utf-8")
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(err)))
                    self.end_headers()
                    self.wfile.write(err)
                    return

                computed = (
                    base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
                    .rstrip(b"=")
                    .decode("ascii")
                )
                if parent.expected_challenge and computed != parent.expected_challenge:
                    err = json.dumps(
                        {"error": "invalid_grant", "error_description": "Invalid code verifier."}
                    ).encode("utf-8")
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(err)))
                    self.end_headers()
                    self.wfile.write(err)
                    return

                token_resp = json.dumps(
                    {
                        "access_token": "ya29.mock-access-token",
                        "refresh_token": "1//mock-refresh-token",
                        "expires_in": 3600,
                        "token_type": "Bearer",
                        "scope": "https://www.googleapis.com/auth/drive.readonly",
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(token_resp)))
                self.end_headers()
                self.wfile.write(token_resp)

        socketserver.TCPServer.allow_reuse_address = True
        self._server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{port}/token"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()


def test_oauth_start_to_callback_persists_pkce_code_verifier_against_real_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full /api/oauth/google/start -> /api/oauth/google/callback regression test using real
    google-auth-oauthlib Flow.fetch_token() against a local PKCE-enforcing token endpoint."""
    # Allow http://127.0.0.1 mock token_uri in oauthlib without weakening production HTTPS
    monkeypatch.setenv("OAUTHLIB_INSECURE_TRANSPORT", "1")
    # Ensure requests bypasses any ambient HTTP_PROXY when calling 127.0.0.1
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")

    with _MockGoogleTokenEndpoint() as token_srv:
        orig_client_config = GoogleDriveClient._client_config

        def _patched_client_config(self: GoogleDriveClient, redirect_uri: str) -> dict:
            cfg = orig_client_config(self, redirect_uri)
            cfg["web"]["token_uri"] = token_srv.url
            return cfg

        monkeypatch.setattr(GoogleDriveClient, "_client_config", _patched_client_config)

        secret = "pkce-test-proxy-secret"
        settings = Settings(
            data_dir=tmp_path / "config",
            work_dir=tmp_path / "work",
            inbox_dir=tmp_path / "inbox",
            photos_dir=tmp_path / "photos",
            proxy_secret=secret,
            google_client_id="pkce-client.apps.googleusercontent.com",
            google_client_secret="pkce-client-secret",
            min_free_disk_gb=0.1,
        )
        app = create_app(base_settings=settings, start_worker=False)
        headers = {
            "X-Proxy-Secret": secret,
            "X-DSM-User": "oleksandr",
            "X-DSM-Admin": "1",
            "X-Proxy-Mode": "cgi",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "192-168-100-97.example.direct.quickconnect.to:5001",
            "X-Forwarded-Prefix": "/webman/3rdparty/gphotos-migrator",
        }

        with TestClient(app, headers=headers) as client:
            # 1. Start OAuth flow
            r_start = client.get("/api/oauth/google/start")
            assert r_start.status_code == 200
            start_data = r_start.json()
            auth_qs = urllib.parse.parse_qs(urllib.parse.urlsplit(start_data["authorization_url"]).query)

            assert "code_challenge" in auth_qs
            assert auth_qs.get("code_challenge_method") == ["S256"]
            challenge = auth_qs["code_challenge"][0]
            state = auth_qs["state"][0]
            token_srv.expected_challenge = challenge

            # Server-side state record exists and holds the code_verifier
            raw_state = app.state.ctx.db.get_state(f"oauth_state:{state}")
            assert raw_state is not None
            stored_rec = json.loads(raw_state)
            assert stored_rec["code_verifier"], "code_verifier must be persisted in oauth_state record"

            # 2. Complete OAuth callback -> real Flow.fetch_token() posts matching code_verifier
            r_cb = client.get(
                f"/api/oauth/google/callback?code=real-auth-code&state={urllib.parse.quote(state)}",
                follow_redirects=False,
            )
            assert r_cb.status_code == 302
            assert "oauth=success" in r_cb.headers["location"]

            # Verify token endpoint received exact matching PKCE verifier + client authentication
            assert len(token_srv.requests) == 1
            posted = token_srv.requests[0]
            assert posted["code"] == ["real-auth-code"]
            assert posted["code_verifier"] == [stored_rec["code_verifier"]]
            auth_hdr = (posted.get("_auth_header") or [""])[0]
            assert posted.get("client_secret") == ["pkce-client-secret"] or auth_hdr.startswith("Basic ")

            # Token file persisted on disk & state record consumed (one-time use)
            assert settings.token_path.is_file()
            saved_token = json.loads(settings.token_path.read_text(encoding="utf-8"))
            assert saved_token["refresh_token"] == "1//mock-refresh-token"
            assert app.state.ctx.db.get_state(f"oauth_state:{state}") is None

            # 3. Replay fails closed and never hits token endpoint again
            r_replay = client.get(
                f"/api/oauth/google/callback?code=real-auth-code&state={urllib.parse.quote(state)}",
                follow_redirects=False,
            )
            assert r_replay.status_code == 302
            assert "oauth=error" in r_replay.headers["location"]
            assert len(token_srv.requests) == 1

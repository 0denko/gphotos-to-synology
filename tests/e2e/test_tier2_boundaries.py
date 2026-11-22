"""Tier 2 — Boundary Value Analysis & Corner/Negative E2E Tests (Target v1.2.0 Specification).

Verifies every fail-closed boundary, security rejection, limit cap, edge case, and
negative contract across C1, C2, G3-G14, and all high-value Minors/Nits.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import stat
import struct
import time
import urllib.parse
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import piexif
import pytest
from conftest import (
    OAUTH_CALLBACK_CGI_PATH,
    PROXY_CGI_PATH,
    REPO_ROOT,
    MockBackendServer,
    MockDSMServer,
    MockWebhookServer,
    build_tar_gz_archive,
    build_test_spk,
    build_zip_archive,
    extract_spk,
    image_to_jpeg_bytes,
    make_github_mock_client,
    make_isolated_settings,
    make_proxy_headers,
    make_sidecar_payload,
    make_token_headers,
    run_app_js_scenario,
    run_cgi,
    run_posix_script,
    save_corrupt_exif_jpeg,
    save_jpeg,
    save_png,
)
from fastapi.testclient import TestClient

from gphotos_synology.config import ConfigManager
from gphotos_synology.database import StateDB
from gphotos_synology.main import create_app
from gphotos_synology.profiles import CreateProfileRequest, ProfileRegistry
from gphotos_synology.sources.gdrive import DriveArchiveItem, GoogleDriveClient
from gphotos_synology.sources.inbox import LocalInboxScanner
from gphotos_synology.synology.photos_api import SynologyPhotosClient
from gphotos_synology.synology.scanner import ExistingLibraryScanner
from gphotos_synology.takeout.dedup import (
    ImageQualityMetrics,
    compare_image_quality,
    compute_media_fingerprint,
    is_perceptual_match,
)
from gphotos_synology.takeout.extractor import extract_archive
from gphotos_synology.takeout.matcher import DirectoryJsonIndex, find_sidecar_json
from gphotos_synology.takeout.metadata import PhotoMetadata, apply_metadata
from gphotos_synology.updater import UpdateService
from gphotos_synology.worker.pipeline import MigrationPipeline


def _dhash_with_bits_flipped(num_bits: int, *, length_hex: int = 32, spread_blocks: bool = False) -> str:
    """Build a hex dHash differing from `'0' * length_hex` by exact `num_bits` Hamming distance."""
    val = 0
    if spread_blocks and length_hex == 32:
        # Flip 1 bit in each 16-bit (4-hex-char) block round-robin
        for idx in range(num_bits):
            block = idx % 8
            bit_in_block = idx // 8
            val |= 1 << (block * 16 + bit_in_block)
    else:
        for idx in range(num_bits):
            val |= 1 << idx
    return f"{val:0{length_hex}x}"


# ===========================================================================
# C1 — API Auth Middleware, Shared Secret, Secret/Mutable Fields & Docker Loopback
# ===========================================================================


def test_t2_c1_unauthenticated_and_spoofed_proxy_headers_rejected(tmp_path: Path) -> None:
    """Unauthenticated /api/*, wrong X-Proxy-Secret, and forged X-DSM-* without secret return 401."""
    secret = "f" * 64
    settings = make_isolated_settings(tmp_path, proxy_secret=secret)
    app = create_app(base_settings=settings, start_worker=False)

    with TestClient(app) as client:
        for path in ("/api/status", "/api/profiles", "/api/settings", "/api/archives", "/api/upgrades"):
            r = client.get(path)
            assert r.status_code == 401, f"Expected 401 on unauthenticated GET {path}, got {r.status_code}"

        # Wrong X-Proxy-Secret
        r_wrong = client.get(
            "/api/status",
            headers=make_proxy_headers(secret="0" * 64, dsm_user="admin", is_admin=True),
        )
        assert r_wrong.status_code == 401

        # Forged X-DSM-User / X-DSM-Admin / X-Proxy-Mode without X-Proxy-Secret
        r_spoof = client.get(
            "/api/profiles",
            headers={
                "X-Proxy-Mode": "cgi",
                "X-DSM-User": "admin",
                "X-DSM-Admin": "1",
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        assert r_spoof.status_code == 401


def test_t2_c1_auto_generated_api_token_is_0600_and_non_mutable_fields_blocked(tmp_path: Path) -> None:
    """When neither proxy_secret nor api_token is set, api.token is generated 0600; UI cannot mutate secrets/system fields."""
    settings = make_isolated_settings(tmp_path)
    app = create_app(base_settings=settings, start_worker=False)

    token_file = settings.data_dir / "api.token"
    assert token_file.is_file(), "Auto-generated api.token must exist when no auth secret is configured"
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    token = token_file.read_text(encoding="utf-8").strip()
    assert len(token) >= 32

    with TestClient(app) as client:
        assert client.get("/api/status").status_code == 401

        r = client.put(
            "/api/settings",
            json={
                "updates": {
                    "proxy_secret": "hijacked",
                    "api_token": "hijacked",
                    "dsm_admin_users": ["attacker"],
                    "trust_proxy_headers": True,
                    "allow_insecure_no_auth": True,
                    "photos_dir": "/etc",
                    "watchtower_url": "http://evil.invalid",
                    "watchtower_token": "evil-token",
                    "app_host": "0.0.0.0",
                }
            },
            headers=make_token_headers(token),
        )
        assert r.status_code == 200
        live_settings = app.state.ctx.config_manager.settings
        assert live_settings.photos_dir == settings.photos_dir
        assert live_settings.watchtower_url == ""
        assert live_settings.watchtower_token == ""
        assert getattr(live_settings, "allow_insecure_no_auth", False) is False
        assert getattr(live_settings, "trust_proxy_headers", False) is False
        assert getattr(live_settings, "proxy_secret", "") != "hijacked"
        assert getattr(live_settings, "api_token", "") != "hijacked"


def test_t2_c1_docker_compose_binds_loopback_by_default() -> None:
    """docker-compose.yml binds loopback 127.0.0.1:8765:8765 rather than all interfaces."""
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "127.0.0.1:8765:8765" in compose
    assert '"8765:8765"' not in compose


# ===========================================================================
# C2 — Server-Side DSM User Profile Isolation & Admin Gate
# ===========================================================================


def test_t2_c2_non_admin_cannot_access_other_profile_or_admin_actions(tmp_path: Path) -> None:
    """Non-admin DSM user gets 403 on another profile (header/query) and on all admin endpoints."""
    secret = "e" * 64
    settings = make_isolated_settings(tmp_path, proxy_secret=secret)
    (settings.homes_dir / "alice" / "Photos").mkdir(parents=True, exist_ok=True)
    (settings.homes_dir / "bob" / "Photos").mkdir(parents=True, exist_ok=True)
    app = create_app(base_settings=settings, start_worker=False)

    admin_hdrs = make_proxy_headers(secret=secret, dsm_user="admin", is_admin=True)
    with TestClient(app) as client:
        r_a = client.post(
            "/api/profiles",
            json={"dsm_username": "alice", "display_name": "Alice", "target_space": "personal"},
            headers=admin_hdrs,
        )
        r_b = client.post(
            "/api/profiles",
            json={"dsm_username": "bob", "display_name": "Bob", "target_space": "personal"},
            headers=admin_hdrs,
        )
        assert r_a.status_code == 200 and r_b.status_code == 200
        alice_id = r_a.json()["profile"]["id"]
        bob_id = r_b.json()["profile"]["id"]

        alice_hdrs = make_proxy_headers(secret=secret, dsm_user="alice", is_admin=False)

        # Alice's GET /api/profiles only exposes alice, is_admin=False
        prof_resp = client.get("/api/profiles", headers=alice_hdrs)
        assert prof_resp.status_code == 200
        pdata = prof_resp.json()
        assert pdata["is_admin"] is False
        assert [p["id"] for p in pdata["profiles"]] == [alice_id]
        discovered_names = [u["username"] for u in pdata.get("discovered_synology_users", [])]
        assert "bob" not in discovered_names

        # Alice CAN access own profile
        assert client.get("/api/status", headers={**alice_hdrs, "X-Profile-Id": alice_id}).status_code == 200

        # Alice CANNOT read or mutate bob or default (header OR query param)
        assert client.get("/api/status", headers={**alice_hdrs, "X-Profile-Id": bob_id}).status_code == 403
        assert client.get(f"/api/status?profile={bob_id}", headers=alice_hdrs).status_code == 403
        assert client.get("/api/settings", headers={**alice_hdrs, "X-Profile-Id": "default"}).status_code == 403
        assert (
            client.post("/api/upgrades/purge-all", headers={**alice_hdrs, "X-Profile-Id": bob_id}).status_code
            == 403
        )

        # Alice CANNOT call admin-only endpoints
        assert client.post("/api/actions/pause", headers=alice_hdrs).status_code == 403
        assert client.post("/api/actions/resume", headers=alice_hdrs).status_code == 403
        assert client.post("/api/updates/apply", headers=alice_hdrs).status_code == 403
        assert (
            client.post(
                "/api/profiles",
                json={"dsm_username": "charlie", "target_space": "personal"},
                headers=alice_hdrs,
            ).status_code
            == 403
        )
        assert client.patch(f"/api/profiles/{bob_id}", json={"enabled": False}, headers=alice_hdrs).status_code == 403
        assert client.delete(f"/api/profiles/{bob_id}", headers=alice_hdrs).status_code == 403

        # User with NO profile cannot access default or alice
        stranger_hdrs = make_proxy_headers(secret=secret, dsm_user="stranger", is_admin=False)
        s_profs = client.get("/api/profiles", headers=stranger_hdrs)
        assert s_profs.status_code == 200
        assert s_profs.json()["profiles"] == []
        assert client.get("/api/status", headers={**stranger_hdrs, "X-Profile-Id": "default"}).status_code == 403


# ===========================================================================
# G3 — OAuth Callback Fail-Closed State Store, Replay, Expiry & Error Encoding
# ===========================================================================


def test_t2_g3_oauth_callback_rejects_missing_unknown_spoofed_expired_replayed_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OAuth callback fails closed on missing/unknown/spoofed/expired/replayed state and URL-encodes error."""
    secret = "d" * 64
    settings = make_isolated_settings(
        tmp_path,
        proxy_secret=secret,
        google_client_id="cid-g3.apps.googleusercontent.com",
        google_client_secret="csec-g3",
    )
    (settings.homes_dir / "alice" / "Photos").mkdir(parents=True, exist_ok=True)
    app = create_app(base_settings=settings, start_worker=False)
    admin_hdrs = make_proxy_headers(secret=secret, dsm_user="admin", is_admin=True)

    exchange_calls: list[tuple[str, str]] = []

    def _fake_exchange(
        self: GoogleDriveClient, code: str, redirect_uri: str, code_verifier: str | None = None
    ) -> None:
        exchange_calls.append((code, redirect_uri))

    monkeypatch.setattr(GoogleDriveClient, "exchange_code", _fake_exchange)

    with TestClient(app) as client:
        # 1. Fail-closed on fresh profile with NO stored state (baseline v1.1.0 bug)
        r_fresh = client.get(
            "/api/oauth/google/callback?code=evil_code&state=default:forged",
            headers=admin_hdrs,
            follow_redirects=False,
        )
        assert r_fresh.status_code in (302, 307, 400, 403)
        assert len(exchange_calls) == 0
        if r_fresh.status_code in (302, 307):
            assert "oauth=error" in r_fresh.headers.get("location", "")

        # 2. Missing state param
        r_missing = client.get(
            "/api/oauth/google/callback?code=abc",
            headers=admin_hdrs,
            follow_redirects=False,
        )
        assert len(exchange_calls) == 0
        assert "oauth=error" in r_missing.headers.get("location", "") or r_missing.status_code >= 400

        # 3. Start real OAuth flow for default profile
        r_start = client.get("/api/oauth/google/start", headers=admin_hdrs)
        assert r_start.status_code == 200
        auth_url = r_start.json()["authorization_url"]
        expected_redirect = r_start.json()["redirect_uri"]
        parsed_qs = urllib.parse.parse_qs(urllib.parse.urlsplit(auth_url).query)
        valid_state = parsed_qs["state"][0]

        # 4. Prefix-spoofed state (attempting to retarget alice) must fail closed
        client.post(
            "/api/profiles",
            json={"dsm_username": "alice", "display_name": "Alice", "target_space": "personal"},
            headers=admin_hdrs,
        )
        token_part = valid_state.split(":", 1)[-1]
        r_spoof = client.get(
            f"/api/oauth/google/callback?code=abc&state=alice:{token_part}",
            headers=admin_hdrs,
            follow_redirects=False,
        )
        assert len(exchange_calls) == 0
        assert "oauth=error" in r_spoof.headers.get("location", "") or r_spoof.status_code >= 400

        # 5. Valid state succeeds ONCE and binds the original redirect_uri
        r_ok = client.get(
            f"/api/oauth/google/callback?code=good_code&state={urllib.parse.quote(valid_state)}",
            headers=admin_hdrs,
            follow_redirects=False,
        )
        assert r_ok.status_code in (302, 307)
        assert "oauth=success" in r_ok.headers.get("location", "")
        assert exchange_calls == [("good_code", expected_redirect)]

        # 6. Replay of the SAME state MUST fail closed (one-time delete_state)
        r_replay = client.get(
            f"/api/oauth/google/callback?code=replay_code&state={urllib.parse.quote(valid_state)}",
            headers=admin_hdrs,
            follow_redirects=False,
        )
        assert len(exchange_calls) == 1, "Replayed OAuth state must NOT trigger exchange_code"
        assert "oauth=error" in r_replay.headers.get("location", "") or r_replay.status_code >= 400

        # 7. TTL > 600s expiry check
        r_start2 = client.get("/api/oauth/google/start", headers=admin_hdrs)
        state2 = urllib.parse.parse_qs(urllib.parse.urlsplit(r_start2.json()["authorization_url"]).query)[
            "state"
        ][0]
        # Backdate any persisted OAuth state timestamp in SQLite app_state by 700s
        with sqlite3.connect(settings.db_path) as conn:
            rows = conn.execute("SELECT key, value FROM app_state").fetchall()
            updated_any = False
            for k, v in rows:
                if state2 in str(k) or state2 in str(v) or "oauth" in str(k).lower():
                    try:
                        obj = json.loads(v)
                        if isinstance(obj, dict):
                            for fld in ("created_at", "issued_at", "ts", "timestamp", "expires_at"):
                                if fld in obj:
                                    if isinstance(obj[fld], (int, float)):
                                        obj[fld] = obj[fld] - 700
                                        updated_any = True
                                    elif isinstance(obj[fld], str):
                                        dt = datetime.fromisoformat(obj[fld]) - timedelta(seconds=700)
                                        obj[fld] = dt.isoformat()
                                        updated_any = True
                            if updated_any:
                                conn.execute("UPDATE app_state SET value = ? WHERE key = ?", (json.dumps(obj), k))
                    except Exception:
                        pass
            conn.commit()
        assert updated_any, "G3 requires OAuth state record with TTL timestamp stored in StateDB"

        r_expired = client.get(
            f"/api/oauth/google/callback?code=late_code&state={urllib.parse.quote(state2)}",
            headers=admin_hdrs,
            follow_redirects=False,
        )
        assert len(exchange_calls) == 1, "Expired (>600s) OAuth state must fail closed"
        assert "oauth=error" in r_expired.headers.get("location", "") or r_expired.status_code >= 400

        # 8. Error parameter URL-encoding prevents extra query injection
        r_err = client.get(
            "/api/oauth/google/callback?error=" + urllib.parse.quote("access_denied&injected=1"),
            headers=admin_hdrs,
            follow_redirects=False,
        )
        assert r_err.status_code in (302, 307)
        loc = r_err.headers.get("location", "")
        assert "oauth=error" in loc
        loc_qs = urllib.parse.parse_qs(urllib.parse.urlsplit(loc).query)
        assert "injected" not in loc_qs


# ===========================================================================
# G4 & G5 — Synology URL Credential-Exfiltration Guard, URL Validation & TLS Defaults
# ===========================================================================


def test_t2_g4_url_change_clears_stored_password_and_rejects_bad_schemes(tmp_path: Path) -> None:
    """Changing synology_url without a new plaintext password clears stored synology_password; invalid schemes fail."""
    settings = make_isolated_settings(
        tmp_path,
        synology_url="https://127.0.0.1:5001",
        synology_username="nas_admin",
        synology_password="super-secret-dsm-password",
    )
    cfg = ConfigManager(settings)

    # 1. URL change with omitted password -> clears password
    updated1 = cfg.update({"synology_url": "https://192.168.1.50:5001"})
    assert updated1.synology_password == ""

    # Re-set password, then change URL while sending masked sentinel "********" -> STILL clears password!
    cfg.update({"synology_password": "second-secret-password"})
    assert cfg.settings.synology_password == "second-secret-password"
    updated2 = cfg.update(
        {"synology_url": "https://192.168.1.51:5001", "synology_password": "********"}
    )
    assert updated2.synology_password == ""

    # 2. Changing URL WITH a brand-new plaintext password retains the new password
    updated3 = cfg.update(
        {"synology_url": "https://192.168.1.52:5001", "synology_password": "brand-new-password"}
    )
    assert updated3.synology_password == "brand-new-password"

    # 3. Invalid schemes / whitespace / CRLF rejected with ValueError
    for bad_url in (
        "javascript:alert(1)",
        "ftp://192.168.1.50:21",
        "https://192.168.1.50:5001/path with spaces",
        "https://192.168.1.50:5001\r\nX-Injected: 1",
    ):
        with pytest.raises(ValueError):
            cfg.update({"synology_url": bad_url})


def test_t2_g5_env_example_uses_https_and_verify_ssl_true() -> None:
    """.env.example enforces SYNOLOGY_VERIFY_SSL=true and HTTPS SYNOLOGY_URL."""
    env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "SYNOLOGY_VERIFY_SSL=true" in env_example
    assert "SYNOLOGY_URL=https://" in env_example


# ===========================================================================
# G6 — CSRF Defense (`X-Requested-With` Required on Mutating `/api/*`)
# ===========================================================================


def test_t2_g6_csrf_header_required_on_all_mutating_api_methods(tmp_path: Path) -> None:
    """POST/PUT/PATCH/DELETE to /api/* (including /api/auth/session) without X-Requested-With -> 403; GET allowed."""
    token = "csrf-test-token-t2g6"
    settings = make_isolated_settings(tmp_path, api_token=token)
    app = create_app(base_settings=settings, start_worker=False)

    auth_no_csrf = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        # GET works without X-Requested-With
        assert client.get("/api/status", headers=auth_no_csrf).status_code == 200

        # Mutating endpoints without X-Requested-With MUST return 403
        assert client.post("/api/auth/session", json={"token": token}).status_code == 403
        assert client.post("/api/actions/pause", headers=auth_no_csrf).status_code == 403
        assert client.put("/api/settings", json={"updates": {"dedup_mode": "strict_hash"}}, headers=auth_no_csrf).status_code == 403
        assert client.patch("/api/profiles/default", json={"display_name": "X"}, headers=auth_no_csrf).status_code == 403
        assert client.delete("/api/profiles/nonexistent", headers=auth_no_csrf).status_code == 403


# ===========================================================================
# G7 — Updater Non-Downgrade, Error Surfacing, POST-only Watchtower & CI/Docker Hardening
# ===========================================================================


def test_t2_g7_updater_rejects_downgrade_same_version_surfaces_error_and_post_only_watchtower(
    tmp_path: Path, mock_webhook_server: MockWebhookServer
) -> None:
    """UpdateService rejects v1.1.0 downgrade & v1.2.0 SHA churn, surfaces 500 error, and never GETs Watchtower on 405."""
    settings = make_isolated_settings(
        tmp_path,
        app_build_sha="aaaa111",
        watchtower_url=f"http://127.0.0.1:{mock_webhook_server.port}",
        watchtower_token="wt-secret",
    )

    # 1. Downgrade (v1.1.0 < 1.2.0) -> update_available MUST be False
    def _downgrade_handler(req: httpx.Request) -> httpx.Response:
        if str(req.url).endswith("/releases/latest"):
            return httpx.Response(
                200,
                json={"tag_name": "v1.1.0", "html_url": "https://example.invalid/v1.1.0", "assets": []},
            )
        return httpx.Response(
            200,
            json={"sha": "bbbb222222222222222222222222222222222222", "commit": {"message": "old", "committer": {"date": "2026-01-01T00:00:00Z"}}},
        )

    with make_github_mock_client(_downgrade_handler) as c1:
        st_down = UpdateService().get_status(settings, force=True, http_client=c1)
    assert st_down["update_available"] is False

    # 2. Same version (v1.2.0 == 1.2.0) with DIFFERENT commit SHA -> update_available MUST be False
    def _same_ver_handler(req: httpx.Request) -> httpx.Response:
        if str(req.url).endswith("/releases/latest"):
            return httpx.Response(
                200,
                json={"tag_name": "v1.2.0", "html_url": "https://example.invalid/v1.2.0", "assets": []},
            )
        return httpx.Response(
            200,
            json={"sha": "cccc333333333333333333333333333333333333", "commit": {"message": "same", "committer": {"date": "2026-01-02T00:00:00Z"}}},
        )

    with make_github_mock_client(_same_ver_handler) as c2:
        st_same = UpdateService().get_status(settings, force=True, http_client=c2)
    assert st_same["update_available"] is False

    # 3. GitHub release endpoint 500 error MUST surface in status["error"] (not silently swallowed)
    def _err_handler(req: httpx.Request) -> httpx.Response:
        if str(req.url).endswith("/releases/latest"):
            return httpx.Response(500, json={"message": "Internal Server Error"})
        return httpx.Response(
            200,
            json={"sha": "aaaa111111111111111111111111111111111111", "commit": {"message": "ok", "committer": {"date": "2026-01-02T00:00:00Z"}}},
        )

    with make_github_mock_client(_err_handler) as c3:
        st_err = UpdateService().get_status(settings, force=True, http_client=c3)
    assert st_err["error"], "Release check HTTP 500 must surface a non-empty error string"

    # 4. Watchtower HTTP 405 MUST raise/fail without falling back to a state-changing GET
    mock_webhook_server.status_code = 405
    with pytest.raises((httpx.HTTPError, RuntimeError, ValueError)):
        UpdateService().trigger_self_update(settings)
    assert all(r.method == "POST" for r in mock_webhook_server.requests), (
        f"Watchtower trigger issued non-POST request: {[r.method for r in mock_webhook_server.requests]}"
    )


def test_t2_g7_ci_workflow_docker_and_dockerignore_hardening_contracts() -> None:
    """Verify CI workflow, Dockerfile, docker-compose.yml, and .dockerignore v1.2.0 hardening."""
    workflow = (REPO_ROOT / ".github" / "workflows" / "docker-publish.yml").read_text(encoding="utf-8")
    assert "--locked" in workflow
    assert "--collect-all pillow_heif" in workflow or "--collect-all=pillow_heif" in workflow
    assert "gh release upload v1.0.0" not in workflow
    assert ".sha256" in workflow or "SHA256SUMS" in workflow
    # Top-level permissions must NOT grant broad write to read-only test job
    top_before_jobs = workflow.split("\njobs:", 1)[0]
    assert "contents: write" not in top_before_jobs
    assert "packages: write" not in top_before_jobs

    dockerignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert ".agents" in dockerignore

    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "gphotos-internal-update-token" not in compose
    assert "TZ=UTC" in compose or "TZ: UTC" in compose
    assert "cap_drop" in compose
    assert "no-new-privileges" in compose


# ===========================================================================
# G8 — SPK Lifecycle `preupgrade` Rotation & `preuninst` Backup Guard
# ===========================================================================


def test_t2_g8_spk_preuninst_upgrade_guard_excludes_secret_and_preupgrade_keeps_3(
    tmp_path: Path,
) -> None:
    """preuninst skips backup on UPGRADE, backs up data excluding proxy.secret on UNINSTALL; preupgrade keeps <=3."""
    spk = build_test_spk(tmp_path, arch="x86_64", standalone=True, output_name="g8.spk")
    extracted = extract_spk(spk, tmp_path / "unpacked")

    pkg_var = tmp_path / "pkg_var"
    photos_dir = tmp_path / "photos"
    (pkg_var / "config").mkdir(parents=True)
    (pkg_var / "work" / "replaced_duplicates").mkdir(parents=True)
    (pkg_var / "inbox").mkdir(parents=True)
    photos_dir.mkdir(parents=True)

    (pkg_var / "proxy.secret").write_text("0" * 64 + "\n", encoding="utf-8")
    (pkg_var / "config" / "state.db").write_bytes(b"SQLITE_DATA")
    (pkg_var / "work" / "replaced_duplicates" / "orig.jpg").write_bytes(b"SOLE_COPY_JPG")
    (pkg_var / "inbox" / "pending.zip").write_bytes(b"PENDING_ZIP")

    base_env = {
        "SYNOPKG_PKGDEST": str(extracted["target"]),
        "SYNOPKG_PKGVAR": str(pkg_var),
        "PHOTOS_DIR": str(photos_dir),
    }

    # 1. preuninst with SYNOPKG_PKG_STATUS=UPGRADE must NOT create uninstall backup
    r_upg = run_posix_script(
        extracted["scripts"] / "preuninst",
        env={**base_env, "SYNOPKG_PKG_STATUS": "UPGRADE"},
    )
    assert r_upg.returncode == 0
    backup_root = photos_dir / ".gphotos-migrator-backup"
    assert not backup_root.exists() or not list(backup_root.iterdir())

    # 2. preuninst with SYNOPKG_PKG_STATUS=UNINSTALL backs up user data & EXCLUDES proxy.secret
    r_uninst = run_posix_script(
        extracted["scripts"] / "preuninst",
        env={**base_env, "SYNOPKG_PKG_STATUS": "UNINSTALL"},
    )
    assert r_uninst.returncode == 0
    assert backup_root.is_dir(), "preuninst UNINSTALL must create ${PHOTOS_DIR}/.gphotos-migrator-backup"
    backups = [p for p in backup_root.iterdir() if p.is_dir()]
    assert len(backups) == 1
    bdir = backups[0]
    assert list(bdir.rglob("orig.jpg")), "Quarantined photo must be preserved in uninstall backup"
    assert list(bdir.rglob("state.db")), "State DB must be preserved in uninstall backup"
    assert list(bdir.rglob("pending.zip")), "Pending inbox archive must be preserved in uninstall backup"
    assert not list(bdir.rglob("proxy.secret")), "proxy.secret MUST NEVER be copied to shared photos backup"

    # 3. preupgrade run 5 times retains at most 3 snapshots
    preupgrade = extracted["scripts"] / "preupgrade"
    for i in range(5):
        r_pre = run_posix_script(preupgrade, env=base_env)
        assert r_pre.returncode == 0
        if i < 4:
            time.sleep(1.05)  # Ensure distinct second-resolution snapshot timestamps
    # Locate upgrade snapshot directories under pkg_var
    snap_candidates = [
        p for p in pkg_var.rglob("*") if p.is_dir() and list(p.glob("state.db")) and p != pkg_var / "config"
    ]
    assert 1 <= len(snap_candidates) <= 3, f"Expected 1..3 preupgrade snapshots, found {len(snap_candidates)}"


# ===========================================================================
# G9 — Strict Sidecar Matcher Negative & Ambiguity Boundaries
# ===========================================================================


def test_t2_g9_strict_sidecar_matcher_rejects_unnumbered_fallback_and_ambiguity(tmp_path: Path) -> None:
    """(1) does not inherit unnumbered JSON; ambiguous title/46-char prefix return None; stem-only after title."""
    # Case 1: Numbered duplicate IMG_1234(1).jpg MUST NOT match unnumbered IMG_1234.jpg.json
    d1 = tmp_path / "case1"
    d1.mkdir()
    dup_photo = save_jpeg(d1 / "IMG_1234(1).jpg", seed=1)
    (d1 / "IMG_1234.jpg.json").write_text(
        json.dumps(make_sidecar_payload("IMG_1234.jpg", timestamp=1700000000)),
        encoding="utf-8",
    )
    assert find_sidecar_json(dup_photo, DirectoryJsonIndex.build(d1)) is None

    # Case 2: Ambiguous internal title (2 JSONs share identical title) -> returns None
    d2 = tmp_path / "case2"
    d2.mkdir()
    ambig_photo = save_jpeg(d2 / "VACATION.jpg", seed=2)
    (d2 / "meta_a.json").write_text(json.dumps(make_sidecar_payload("VACATION.jpg")), encoding="utf-8")
    (d2 / "meta_b.json").write_text(json.dumps(make_sidecar_payload("VACATION.jpg")), encoding="utf-8")
    assert find_sidecar_json(ambig_photo, DirectoryJsonIndex.build(d2)) is None

    # Case 3: Ambiguous 46-char prefix (2 JSONs share same 46-char prefix) -> returns None
    d3 = tmp_path / "case3"
    d3.mkdir()
    prefix46 = "A" * 46
    long_photo = save_jpeg(d3 / f"{prefix46}_extra_suffix_name.jpg", seed=3)
    (d3 / f"{prefix46}_one.json").write_text(json.dumps(make_sidecar_payload("other1.jpg")), encoding="utf-8")
    (d3 / f"{prefix46}_two.json").write_text(json.dumps(make_sidecar_payload("other2.jpg")), encoding="utf-8")
    assert find_sidecar_json(long_photo, DirectoryJsonIndex.build(d3)) is None

    # Case 4: Stem-only candidate is tried AFTER title disambiguation
    d4 = tmp_path / "case4"
    d4.mkdir()
    jpg_photo = save_jpeg(d4 / "CLIP_01.jpg", seed=4)
    # Stem-only JSON belongs to CLIP_01.png, while true_meta.json has exact title CLIP_01.jpg
    (d4 / "CLIP_01.json").write_text(json.dumps(make_sidecar_payload("CLIP_01.png")), encoding="utf-8")
    true_meta = d4 / "true_meta.json"
    true_meta.write_text(json.dumps(make_sidecar_payload("CLIP_01.jpg")), encoding="utf-8")
    assert find_sidecar_json(jpg_photo, DirectoryJsonIndex.build(d4)) == true_meta

    # Case 5: Case-differing sidecars on case-sensitive FS do not clobber exact-case match
    d5 = tmp_path / "case5"
    d5.mkdir()
    upper_jpg = save_jpeg(d5 / "Photo.JPG", seed=5)
    lower_json = d5 / "photo.jpg.json"
    exact_json = d5 / "Photo.JPG.json"
    lower_json.write_text(json.dumps(make_sidecar_payload("photo.jpg")), encoding="utf-8")
    exact_json.write_text(json.dumps(make_sidecar_payload("Photo.JPG")), encoding="utf-8")
    assert find_sidecar_json(upper_jpg, DirectoryJsonIndex.build(d5)) == exact_json


# ===========================================================================
# G10 — Corrupt/Undecodable Image Warning & Persistent Stable Skip
# ===========================================================================


def test_t2_g10_undecodable_heic_logs_warning_and_persists_stable_skip(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Corrupt .heic logs WARNING, sets visual_status='undecodable', and scanner skips on cycle 2."""
    settings = make_isolated_settings(tmp_path)
    db = StateDB(settings.db_path)

    corrupt_heic = settings.photos_dir / "2024/07/broken.heic"
    corrupt_heic.parent.mkdir(parents=True, exist_ok=True)
    corrupt_heic.write_bytes(b"NOT_A_VALID_HEIF_CONTAINER_BYTES_123456")

    with caplog.at_level(logging.WARNING):
        fp = compute_media_fingerprint(corrupt_heic, compute_visual=True)
    assert getattr(fp, "visual_status", "") == "undecodable"
    assert any(rec.levelno >= logging.WARNING for rec in caplog.records)

    scanner = ExistingLibraryScanner(settings, db)
    r1 = scanner.scan()
    assert r1.newly_indexed == 1
    assert r1.unchanged_skipped == 0

    with sqlite3.connect(settings.db_path) as conn:
        row = conn.execute(
            "SELECT visual_status FROM media_items WHERE dest_rel_path = ?",
            ("2024/07/broken.heic",),
        ).fetchone()
    assert row is not None and row[0] == "undecodable"

    # Second scan must skip stably without re-indexing
    r2 = scanner.scan()
    assert r2.newly_indexed == 0
    assert r2.unchanged_skipped == 1


# ===========================================================================
# G11 — Perceptual Dedup Boundaries, Timezone Normalization, Buckets & Sharpness Guard
# ===========================================================================


def test_t2_g11_perceptual_dedup_corroboration_unknown_dims_tz_buckets_and_sharpness(
    tmp_path: Path,
) -> None:
    """Verify unknown-dim rejection, dist>4 corroboration, .astimezone(UTC), bucket recall, and sharpness guard."""
    base_h = "0" * 32
    dist5_h = _dhash_with_bits_flipped(5)
    dist4_h = _dhash_with_bits_flipped(4)
    dist6_spread = _dhash_with_bits_flipped(6, spread_blocks=True)
    dist8_spread = _dhash_with_bits_flipped(8, spread_blocks=True)

    # 1. dist == 5 without timestamp and with different stem MUST return False (even at max_distance=10)
    assert (
        is_perceptual_match(
            dist5_h,
            base_h,
            incoming_taken_at=None,
            existing_taken_at=None,
            incoming_filename="IMG_AAA.jpg",
            existing_filename="IMG_BBB.jpg",
            incoming_width=1000,
            incoming_height=800,
            existing_width=1000,
            existing_height=800,
            max_distance=10,
        )
        is False
    )

    # 2. dist == 5 WITH matching stem and known compatible dims -> True
    assert (
        is_perceptual_match(
            dist5_h,
            base_h,
            incoming_taken_at=None,
            existing_taken_at=None,
            incoming_filename="IMG_SAME.jpg",
            existing_filename="IMG_SAME.jpg",
            incoming_width=1000,
            incoming_height=800,
            existing_width=1000,
            existing_height=800,
            max_distance=10,
        )
        is True
    )

    # 3. Unknown dimensions fail looser branches (dist=5 with same stem or dist=4 without strong match)
    assert (
        is_perceptual_match(
            dist5_h,
            base_h,
            incoming_taken_at="2024-07-03T10:00:00+00:00",
            existing_taken_at="2024-07-03T10:00:00+00:00",
            incoming_filename="IMG_A.jpg",
            existing_filename="IMG_B.jpg",
            incoming_width=0,
            incoming_height=0,
            existing_width=1000,
            existing_height=800,
            max_distance=10,
        )
        is False
    )

    # 4. Mixed timezone offsets: +02:00 vs +00:00 at same instant (0s delta) MUST match at dist=5
    assert (
        is_perceptual_match(
            dist5_h,
            base_h,
            incoming_taken_at="2024-07-03T12:00:00+02:00",
            existing_taken_at="2024-07-03T10:00:00+00:00",
            incoming_filename="IMG_A.jpg",
            existing_filename="IMG_B.jpg",
            incoming_width=1000,
            incoming_height=800,
            existing_width=1000,
            existing_height=800,
            max_distance=10,
        )
        is True
    )
    # Ensure dist <= 4 with matching timestamp still matches
    assert (
        is_perceptual_match(
            dist4_h,
            base_h,
            incoming_taken_at="2024-07-03T10:00:00+00:00",
            existing_taken_at="2024-07-03T10:00:00+00:00",
            incoming_filename="IMG_A.jpg",
            existing_filename="IMG_B.jpg",
            incoming_width=1000,
            incoming_height=800,
            existing_width=1000,
            existing_height=800,
            max_distance=10,
        )
        is True
    )

    # 5. StateDB bucket recall: effective_max < 8 (dist=6), effective_max >= 8 (dist=8 across all 8 blocks), legacy 16-char
    db = StateDB(tmp_path / "buckets.db")
    db.record_media_item(
        original_sha256="sha_base",
        final_sha256="sha_base",
        original_filename="BASE.jpg",
        dest_rel_path="2024/07/BASE.jpg",
        file_size=5000,
        taken_at="2024-07-03T10:00:00+00:00",
        latitude=None,
        longitude=None,
        exif_status="repaired",
        source_archive_id=None,
        pixel_sha256="px_base",
        dhash=base_h,
        width=1000,
        height=800,
    )
    db.record_media_item(
        original_sha256="sha_legacy",
        final_sha256="sha_legacy",
        original_filename="LEGACY.jpg",
        dest_rel_path="2024/07/LEGACY.jpg",
        file_size=5000,
        taken_at="2024-07-04T10:00:00+00:00",
        latitude=None,
        longitude=None,
        exif_status="repaired",
        source_archive_id=None,
        pixel_sha256="px_legacy",
        dhash="0" * 16,
        width=1000,
        height=800,
    )

    m_bucket, r_bucket = db.find_duplicate_media(
        sha256="x1",
        dhash=dist6_spread,
        width=1000,
        height=800,
        taken_at="2024-07-03T10:00:00+00:00",
        filename="BASE_copy.jpg",
        perceptual_max_distance=7,
    )
    assert r_bucket == "perceptual_dhash" and m_bucket is not None

    m_fallback, r_fallback = db.find_duplicate_media(
        sha256="x2",
        dhash=dist8_spread,
        width=1000,
        height=800,
        taken_at="2024-07-03T10:00:00+00:00",
        filename="BASE_copy2.jpg",
        perceptual_max_distance=10,
    )
    assert r_fallback == "perceptual_dhash" and m_fallback is not None

    m_leg, r_leg = db.find_duplicate_media(
        sha256="x3",
        dhash=_dhash_with_bits_flipped(1, length_hex=16),
        width=1000,
        height=800,
        taken_at="2024-07-04T10:00:00+00:00",
        filename="LEGACY.jpg",
        perceptual_max_distance=4,
    )
    assert r_leg == "perceptual_dhash" and m_leg is not None

    # 6. Sharpness-guarded >=15% size upgrade rule rejects blurrier larger photo
    existing_sharp = ImageQualityMetrics(width=2000, height=1500, file_size=500_000, sharpness=80.0, filename="a.jpg")
    incoming_blurrier_larger = ImageQualityMetrics(
        width=2000, height=1500, file_size=700_000, sharpness=68.0, filename="b.jpg"
    )
    decision = compare_image_quality(incoming_blurrier_larger, existing_sharp)
    assert decision.incoming_wins is False


# ===========================================================================
# G12 — Extraction Decompression Limits & ZipSlip/TarSlip Controls
# ===========================================================================


def test_t2_g12_extraction_caps_and_zipslip_tarslip_skip_vs_fail_closed(tmp_path: Path) -> None:
    """extract_archive enforces entry count, uncompressed bytes, streaming bytes, ratio, and unsafe-entry policy."""
    # 1. Entry count cap
    many_zip = build_zip_archive(
        tmp_path / "many.zip",
        {f"f_{i}.txt": b"x" for i in range(6)},
    )
    with pytest.raises(ValueError):
        extract_archive(many_zip, tmp_path / "out_many", max_entries=3)

    # 2. Pre-flight uncompressed byte cap
    big_zip = build_zip_archive(
        tmp_path / "big.zip",
        {"payload.bin": b"A" * 50_000},
    )
    with pytest.raises(ValueError):
        extract_archive(big_zip, tmp_path / "out_big", max_uncompressed_bytes=10_000)

    # 3. Compression ratio cap (2 MB of zeros compresses to ~2 KB -> ratio ~1000x)
    bomb_zip = build_zip_archive(
        tmp_path / "bomb.zip",
        {"zeros.bin": b"\x00" * (2 * 1024 * 1024)},
        compress_type=zipfile.ZIP_DEFLATED,
    )
    with pytest.raises(ValueError):
        extract_archive(
            bomb_zip,
            tmp_path / "out_bomb",
            max_uncompressed_bytes=10 * 1024 * 1024,
            max_compression_ratio=20.0,
        )

    # 4. ZipSlip fail-closed (default skip_unsafe_entries=False) vs skip-and-log (skip_unsafe_entries=True)
    slip_zip = build_zip_archive(
        tmp_path / "slip.zip",
        {
            "../escaped.txt": b"evil",
            "Takeout/safe.txt": b"good",
        },
    )
    with pytest.raises(ValueError):
        extract_archive(slip_zip, tmp_path / "out_slip_fail", skip_unsafe_entries=False)
    assert not (tmp_path / "escaped.txt").exists()

    out_skip = extract_archive(slip_zip, tmp_path / "out_slip_skip", skip_unsafe_entries=True)
    assert (out_skip / "Takeout" / "safe.txt").read_bytes() == b"good"
    assert not (tmp_path / "escaped.txt").exists()

    # 5. TarSlip + symlink never materialized
    tar_slip = build_tar_gz_archive(
        tmp_path / "slip.tar.gz",
        {"../tar_escape.txt": b"evil", "Takeout/tar_safe.txt": b"ok"},
        symlinks={"Takeout/bad_link": "/etc/passwd"},
    )
    with pytest.raises(ValueError):
        extract_archive(tar_slip, tmp_path / "out_tar_fail", skip_unsafe_entries=False)

    out_tar_skip = extract_archive(tar_slip, tmp_path / "out_tar_skip", skip_unsafe_entries=True)
    assert (out_tar_skip / "Takeout" / "tar_safe.txt").read_bytes() == b"ok"
    assert not (out_tar_skip / "Takeout" / "bad_link").exists()
    assert not (tmp_path / "tar_escape.txt").exists()


# ===========================================================================
# G13 & Metadata Boundaries — Corrupt EXIF Preservation, DMS 60s Clamp, Non-JPEG Warning
# ===========================================================================


def test_t2_g13_corrupt_exif_preserved_dms_60s_clamped_and_non_jpeg_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Corrupt EXIF skips write (exact bytes preserved); GPS 37.99999999 carries cleanly; PNG warns."""
    # 1. G13: Corrupt EXIF APP1 causes piexif.load to fail -> bytes MUST remain untouched
    corrupt_jpg = save_corrupt_exif_jpeg(tmp_path / "corrupt_exif.jpg", seed=13)
    original_bytes = corrupt_jpg.read_bytes()
    with pytest.raises((piexif.InvalidImageDataError, ValueError, struct.error, KeyError, OSError)):
        piexif.load(str(corrupt_jpg))

    meta = PhotoMetadata(
        title="corrupt_exif.jpg",
        taken_at=datetime(2024, 7, 3, 10, 0, 0, tzinfo=UTC),
        latitude=37.5,
        longitude=-122.5,
    )
    status = apply_metadata(corrupt_jpg, meta, write_exif=True, exiftool_path="nonexistent")
    assert status == "mtime_only"
    assert corrupt_jpg.read_bytes() == original_bytes
    assert abs(corrupt_jpg.stat().st_mtime - meta.taken_at.timestamp()) < 2.0

    # 2. GPS DMS seconds == 60 carry clamp (37.99999999 -> 38 deg 0 min 0.0 sec)
    dms_jpg = save_jpeg(tmp_path / "dms_edge.jpg", seed=14)
    dms_meta = PhotoMetadata(
        title="dms_edge.jpg",
        taken_at=datetime(2024, 7, 3, 10, 0, 0, tzinfo=UTC),
        latitude=37.99999999,
        longitude=-122.99999999,
    )
    assert apply_metadata(dms_jpg, dms_meta, write_exif=True, exiftool_path="nonexistent") == "repaired"
    gps = piexif.load(str(dms_jpg))["GPS"]
    lat_dms = gps[piexif.GPSIFD.GPSLatitude]
    lon_dms = gps[piexif.GPSIFD.GPSLongitude]
    for dms in (lat_dms, lon_dms):
        deg_num, deg_den = dms[0]
        min_num, min_den = dms[1]
        sec_num, sec_den = dms[2]
        assert 0 <= (min_num / min_den) < 60
        assert 0 <= (sec_num / sec_den) < 60
        assert deg_num / deg_den in (38.0, 123.0)

    # 3. Non-JPEG without ExifTool emits a clear WARNING log and returns mtime_only
    png_path = save_png(tmp_path / "sample.png", seed=15)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        png_status = apply_metadata(png_path, meta, write_exif=True, exiftool_path="nonexistent")
    assert png_status == "mtime_only"
    assert any(rec.levelno >= logging.WARNING for rec in caplog.records)


# ===========================================================================
# G14 — Personal & Shared `custom_subfolder` Path Containment
# ===========================================================================


def test_t2_g14_personal_and_shared_subfolder_rejects_traversal_and_symlink_escape(
    tmp_path: Path,
) -> None:
    """Personal & shared custom_subfolder reject .., absolute paths, backslashes, and symlink escapes."""
    settings = make_isolated_settings(tmp_path)
    alice_photos = settings.homes_dir / "alice" / "Photos"
    maria_photos = settings.homes_dir / "maria" / "Photos"
    alice_photos.mkdir(parents=True)
    maria_photos.mkdir(parents=True)

    # Create symlink inside alice's Photos pointing to maria's Photos
    (alice_photos / "escape_link").symlink_to(maria_photos)
    # Create symlink inside shared photos_dir pointing outside
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    (settings.photos_dir / "shared_escape").symlink_to(outside)

    registry = ProfileRegistry(settings)

    for bad_sub in ("../../maria/Photos", "../escape", "/etc/passwd", r"..\..\maria", "escape_link"):
        with pytest.raises(ValueError):
            registry.create_profile(
                CreateProfileRequest(
                    dsm_username="alice",
                    display_name="Alice Bad",
                    target_space="personal",
                    custom_subfolder=bad_sub,
                )
            )

    for bad_shared in ("../outside_dir", "/etc/passwd", "shared_escape"):
        with pytest.raises(ValueError):
            registry.create_profile(
                CreateProfileRequest(
                    dsm_username="sharedbad",
                    display_name="Shared Bad",
                    target_space="shared",
                    custom_subfolder=bad_shared,
                )
            )


# ===========================================================================
# Minors — Profile Collisions, Corrupt-File Recovery, CGIs, XSS, Inbox, Drive, DSM, Purge Guard
# ===========================================================================


def test_t2_minor_reserved_profile_ids_duplicate_photos_dir_and_corrupt_files_resilience(
    tmp_path: Path,
) -> None:
    """Reserved slugs & duplicate photos_dir rejected; corrupt JSON/SQLite recover without startup crash."""
    settings = make_isolated_settings(tmp_path)
    (settings.homes_dir / "processed" / "Photos").mkdir(parents=True)
    (settings.homes_dir / "profiles" / "Photos").mkdir(parents=True)
    (settings.homes_dir / "alice" / "Photos").mkdir(parents=True)

    registry = ProfileRegistry(settings)

    # Reserved IDs must never collide with `processed` or `profiles`
    for reserved in ("processed", "profiles"):
        try:
            ctx = registry.create_profile(
                CreateProfileRequest(
                    dsm_username=reserved,
                    display_name=reserved,
                    target_space="personal",
                )
            )
            assert ctx.profile.id not in {"processed", "profiles", "default"}
        except ValueError:
            pass  # Explicit rejection is also compliant

    # Duplicate photos_dir rejected (second shared profile with empty custom_subfolder collides with default)
    with pytest.raises(ValueError):
        registry.create_profile(
            CreateProfileRequest(
                dsm_username="dup_shared",
                display_name="Duplicate Shared",
                target_space="shared",
                custom_subfolder="",
            )
        )

    # Corrupt runtime_settings.json -> backs up corrupt file & recovers defaults
    cfg_dir = tmp_path / "corrupt_cfg"
    s_cfg = make_isolated_settings(cfg_dir)
    corrupt_text = "{corrupt json!!!"
    s_cfg.runtime_config_path.write_text(corrupt_text, encoding="utf-8")
    cm = ConfigManager(s_cfg)
    assert cm.settings.app_host == "127.0.0.1"
    backups = [
        p
        for p in s_cfg.data_dir.iterdir()
        if p != s_cfg.runtime_config_path and p.is_file() and corrupt_text in p.read_text(errors="ignore")
    ]
    assert backups, "Corrupt runtime_settings.json must be backed up before recovery"

    # Corrupt profiles.json -> backs up corrupt file & recovers default profile
    prof_dir = tmp_path / "corrupt_prof"
    s_prof = make_isolated_settings(prof_dir)
    pjson = s_prof.data_dir / "profiles.json"
    pjson.write_text("{not valid json", encoding="utf-8")
    reg2 = ProfileRegistry(s_prof)
    assert reg2.get_context("default") is not None
    p_backups = [
        p
        for p in s_prof.data_dir.iterdir()
        if p != pjson and p.is_file() and "{not valid json" in p.read_text(errors="ignore")
    ]
    assert p_backups, "Corrupt profiles.json must be backed up before recovery"

    # Corrupt non-default profile SQLite DB must not crash startup
    db_dir = tmp_path / "corrupt_db"
    s_db = make_isolated_settings(db_dir, api_token="tok-resilience")
    (s_db.homes_dir / "baduser" / "Photos").mkdir(parents=True)
    reg3 = ProfileRegistry(s_db)
    bad_ctx = reg3.create_profile(
        CreateProfileRequest(dsm_username="baduser", display_name="BadDB", target_space="personal")
    )
    bad_db_path = bad_ctx.config_manager.settings.db_path
    bad_db_path.write_bytes(b"NOT A SQLITE DATABASE AT ALL - CORRUPTED BYTES")

    app = create_app(base_settings=s_db, start_worker=False)
    with TestClient(app) as client:
        r = client.get("/api/status", headers=make_token_headers("tok-resilience"))
        assert r.status_code == 200


def test_t2_minor_proxy_cgi_limits_offline_503_crlf_and_fail_closed_auth(
    mock_backend_server: MockBackendServer,
) -> None:
    """proxy.cgi enforces 1MiB cap (413), offline 200 HTML vs 503 JSON, CRLF strip, and fail-closed 403."""
    secret = "7" * 64

    # 1. Fail-closed 403 when AUTH_CGI absent and GPHOTOS_SKIP_DSM_AUTH unset
    r_no_auth = run_cgi(
        PROXY_CGI_PATH,
        backend_url=mock_backend_server.url,
        skip_dsm_auth=False,
        proxy_secret=secret,
    )
    assert r_no_auth.status == 403

    # 2. Invalid DSM username charset rejected with 403
    r_bad_user = run_cgi(
        PROXY_CGI_PATH,
        backend_url=mock_backend_server.url,
        proxy_secret=secret,
        dsm_user="bad user\r\nX-Inject: 1",
    )
    assert r_bad_user.status == 403

    # 3. 1 MiB body cap -> HTTP 413
    r_413 = run_cgi(
        PROXY_CGI_PATH,
        backend_url=mock_backend_server.url,
        method="POST",
        query_string="p=/api/settings",
        content_length_override=str(1024 * 1024 + 1),
        proxy_secret=secret,
    )
    assert r_413.status == 413

    # 4. Offline backend: GET / -> 200 HTML; GET /api/status -> 503 JSON; POST / -> 503 JSON
    dead_url = "http://127.0.0.1:1"
    r_off_root = run_cgi(PROXY_CGI_PATH, backend_url=dead_url, method="GET", query_string="p=/", proxy_secret=secret)
    assert r_off_root.status == 200
    assert "text/html" in r_off_root.headers.get("content-type", "")

    r_off_api = run_cgi(
        PROXY_CGI_PATH,
        backend_url=dead_url,
        method="GET",
        query_string="p=/api/status",
        proxy_secret=secret,
    )
    assert r_off_api.status == 503
    assert "application/json" in r_off_api.headers.get("content-type", "")

    r_off_post = run_cgi(
        PROXY_CGI_PATH,
        backend_url=dead_url,
        method="POST",
        query_string="p=/",
        proxy_secret=secret,
    )
    assert r_off_post.status == 503

    # 5. CRLF header injection in upstream response stripped/blocked
    mock_backend_server.raw_response_bytes = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: application/json\r\n"
        b"X-Good: ok\r\n"
        b"X-Evil: safe\r\nSet-Cookie: pwned=1\r\n"
        b"Content-Length: 11\r\n\r\n"
        b'{"ok":true}'
    )
    # Also test client request header with embedded CR/LF never splits upstream headers
    r_crlf = run_cgi(
        PROXY_CGI_PATH,
        backend_url=mock_backend_server.url,
        query_string="p=/api/status",
        proxy_secret=secret,
        http_headers={"X-Profile-Id": "default\r\nX-Injected: yes"},
    )
    assert b"X-Injected" not in r_crlf.raw_header_block


def test_t2_minor_oauth_callback_cgi_allowlists_query_params_and_direct_ignores_forwarded_host(
    tmp_path: Path, mock_backend_server: MockBackendServer
) -> None:
    """oauth_callback.cgi forwards only code/state/error; direct mode ignores spoofed X-Forwarded-Host."""
    secret = "8" * 64
    res = run_cgi(
        OAUTH_CALLBACK_CGI_PATH,
        backend_url=mock_backend_server.url,
        query_string="code=abc&state=xyz&error=none&profile=victim&evil=1",
        proxy_secret=secret,
        dsm_user="alice",
        dsm_admin="0",
    )
    assert res.status == 200
    req = mock_backend_server.requests[-1]
    assert set(req.query.keys()) <= {"code", "state", "error"}
    assert "profile" not in req.query
    assert "evil" not in req.query

    # Direct mode (_public_base_url) MUST ignore spoofed X-Forwarded-Host when trust_proxy_headers=False
    token = "direct-host-test-tok"
    settings = make_isolated_settings(
        tmp_path,
        api_token=token,
        google_client_id="cid-direct",
        google_client_secret="csec-direct",
    )
    app = create_app(base_settings=settings, start_worker=False)
    with TestClient(app) as client:
        r = client.get(
            "/api/oauth/google/start",
            headers={
                **make_token_headers(token),
                "X-Forwarded-Host": "attacker.evil.example",
                "X-Forwarded-Proto": "https",
            },
        )
        assert r.status_code == 200
        assert "attacker.evil.example" not in r.json()["redirect_uri"]


def test_t2_minor_app_js_escape_html_prevents_attribute_breakout_xss() -> None:
    """app.js escapeHtml escapes double and single quotes in renderArchives title attribute."""
    malicious_name = 'takeout" onmouseover="alert(1)\' data-x="'
    result = run_app_js_scenario(
        pathname="/",
        routes={
            "/api/archives": {
                "status": 200,
                "json": {
                    "archives": [
                        {
                            "id": 1,
                            "filename": malicious_name,
                            "source": "inbox",
                            "status": "completed",
                            "imported_files": 1,
                            "duplicate_files": 0,
                            "failed_files": 0,
                            "error_message": "",
                        }
                    ]
                },
            }
        },
        inspect_ids=["archives-tbody"],
    )
    rendered = result["elements"]["archives-tbody"]["innerHTML"]
    assert 'onmouseover="alert(1)"' not in rendered, f"XSS attribute breakout detected in: {rendered}"
    assert "&quot;" in rendered


def test_t2_minor_inbox_skips_symlinks_and_tracks_mtime_stability(tmp_path: Path) -> None:
    """LocalInboxScanner skips symlinks and resets stability timer when mtime_ns changes at same size."""
    settings = make_isolated_settings(tmp_path, inbox_stability_seconds=1)
    scanner = LocalInboxScanner(settings)

    # 1. Symlink to valid archive outside inbox must be skipped
    outside_zip = build_zip_archive(tmp_path / "outside.zip", {"a.txt": b"1234"})
    (settings.inbox_dir / "takeout-link.zip").symlink_to(outside_zip)

    # 2. Regular archive modified in-place with identical byte length (mtime_ns changes)
    real_zip = build_zip_archive(settings.inbox_dir / "takeout-real.zip", {"a.txt": b"1111"})
    size_before = real_zip.stat().st_size

    assert scanner.scan_ready_archives() == []  # First observation starts stability timer
    time.sleep(0.6)
    # Touch mtime_ns forward while preserving exact byte size
    new_ns = real_zip.stat().st_mtime_ns + 5_000_000_000
    os.utime(real_zip, ns=(new_ns, new_ns))
    assert real_zip.stat().st_size == size_before

    # Because mtime_ns changed, 0.5s later (< 1s since change) it must STILL NOT be ready!
    time.sleep(0.5)
    assert scanner.scan_ready_archives() == []

    # Wait full stability window (>= 1s) after mtime change was observed -> now ready
    time.sleep(1.1)
    ready = scanner.scan_ready_archives()
    assert [i.name for i in ready] == ["takeout-real.zip"]


def test_t2_minor_gdrive_md5_mismatch_rejected_and_refresh_error_surfaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GoogleDriveClient rejects MD5 mismatch without promoting dest and surfaces refresh errors."""
    settings = make_isolated_settings(tmp_path, google_client_id="cid", google_client_secret="csec")
    # Expired token to force refresh failure
    settings.token_path.write_text(
        json.dumps(
            {
                "token": "expired",
                "refresh_token": "revoked_refresh",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": "cid",
                "client_secret": "csec",
                "scopes": ["https://www.googleapis.com/auth/drive.readonly"],
                "expiry": "2020-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    from google.oauth2.credentials import Credentials

    def _boom_refresh(self: Credentials, request: Any) -> None:
        raise RuntimeError("invalid_grant: Token has been revoked")

    monkeypatch.setattr(Credentials, "refresh", _boom_refresh)

    client = GoogleDriveClient(settings)
    st = client.connection_status()
    assert st["connected"] is False
    assert "revoked" in st["error"].lower() or len(st["error"]) > 0

    # Now provide valid token and simulate corrupt download with bad MD5
    settings.token_path.write_text(
        json.dumps(
            {
                "token": "valid",
                "refresh_token": "r",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": "cid",
                "client_secret": "csec",
                "scopes": ["https://www.googleapis.com/auth/drive.readonly"],
                "expiry": "2099-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    class _CorruptDownloader:
        def __init__(self, fh: Any, request: Any, chunksize: int = 0) -> None:
            self._fh = fh

        def next_chunk(self, num_retries: int = 3) -> tuple[Any, bool]:
            self._fh.write(b"CORRUPTED_BYTES")
            return None, True

    class _FakeService:
        def files(self) -> Any:
            class _F:
                def get_media(self, fileId: str) -> object:
                    return object()

            return _F()

    import gphotos_synology.sources.gdrive as gdrive_mod

    monkeypatch.setattr(gdrive_mod, "build", lambda *a, **kw: _FakeService())
    monkeypatch.setattr(gdrive_mod, "MediaIoBaseDownload", _CorruptDownloader)

    dest = tmp_path / "dl" / "takeout-corrupt.zip"
    item = DriveArchiveItem(
        file_id="f-bad",
        name="takeout-corrupt.zip",
        size_bytes=15,
        md5_checksum=hashlib.md5(b"EXPECTED_DIFFERENT_BYTES").hexdigest(),
        created_time="2024-07-03T10:00:00Z",
        batch_key="20240703",
    )
    with pytest.raises((ValueError, RuntimeError, OSError)):
        client.download_archive(item, dest, max_retries=1)
    assert not dest.exists(), "Corrupt download failing MD5 check must never be promoted to dest"


def test_t2_minor_synology_client_code_119_relogin_and_root_resolve_scoping(
    tmp_path: Path, mock_dsm_server: MockDSMServer
) -> None:
    """SynologyPhotosClient auto-relogins on code 119 and does not match subfolder files for root path."""
    # Register IMG_ROOT.jpg ONLY inside subfolder 2024/07 (NOT at root!)
    sub_id = mock_dsm_server.state.add_indexed_item("2024/07/IMG_ROOT.jpg")
    settings = make_isolated_settings(
        tmp_path,
        synology_url=mock_dsm_server.url,
        synology_username=mock_dsm_server.state.valid_username,
        synology_password=mock_dsm_server.state.valid_password,
        synology_verify_ssl=False,
    )

    with SynologyPhotosClient(settings) as client:
        assert mock_dsm_server.state.sid_counter == 1
        # Root-level resolve must NOT return the subfolder item
        assert client.resolve_item_id("IMG_ROOT.jpg") is None
        # Exact subfolder resolve DOES return sub_id
        assert client.resolve_item_id("2024/07/IMG_ROOT.jpg") == sub_id

        # Simulate expired session (code 119) on next call -> client auto-relogins transparently
        mock_dsm_server.state.expire_next_api_calls = 1
        aid = client.get_or_create_album("After Relogin Album")
        assert aid > 0
        assert mock_dsm_server.state.sid_counter == 2


def test_t2_minor_purge_rejects_restored_status_and_path_traversal(tmp_path: Path) -> None:
    """purge_quality_upgrade rejects non-'quarantined' status and path traversal with ValueError & HTTP 400."""
    token = "purge-guard-tok"
    settings = make_isolated_settings(
        tmp_path,
        api_token=token,
        dedup_mode="perceptual",
        duplicate_quality_policy="auto_upgrade_quarantine",
        write_exif_metadata=False,
    )
    app = create_app(base_settings=settings, start_worker=False)
    pipeline: MigrationPipeline = app.state.ctx.pipeline
    db: StateDB = app.state.ctx.db

    save_jpeg(settings.photos_dir / "2024/07/p1.jpg", width=200, height=150, seed=81, quality=50)
    pipeline.index_existing_library()
    build_zip_archive(
        settings.inbox_dir / "takeout-p1.zip",
        {
            "Takeout/Google Photos/Photos from 2024/p1.jpg": image_to_jpeg_bytes(
                width=800, height=600, seed=81, quality=95
            )
        },
    )
    pipeline.run_cycle()
    upg = db.list_quality_upgrades()[0]

    # Restore upgrade first -> status becomes 'restored'
    assert pipeline.restore_quality_upgrade(upg["id"])["ok"] is True

    # Purging an already-restored upgrade MUST raise ValueError and return HTTP 400
    with pytest.raises(ValueError):
        pipeline.purge_quality_upgrade(upg["id"])

    with TestClient(app) as client:
        r_400 = client.post(
            f"/api/upgrades/{upg['id']}/purge",
            headers=make_token_headers(token),
        )
        assert r_400.status_code == 400

    # Path traversal in quarantine_filename rejected with ValueError
    evil_id = db.record_quality_upgrade(
        media_id=upg["media_id"],
        kept_rel_path="2024/07/p1.jpg",
        quarantine_filename="../../outside.jpg",
        old_filename="p1.jpg",
        new_filename="p1.jpg",
        old_width=200,
        old_height=150,
        old_file_size=100,
        old_sharpness=1.0,
        new_width=800,
        new_height=600,
        new_file_size=500,
        new_sharpness=2.0,
        match_reason="perceptual_dhash",
        upgrade_reason="resolution",
    )
    with pytest.raises(ValueError):
        pipeline.purge_quality_upgrade(evil_id)


def test_t2_minor_ui_only_spk_status_probes_health_instead_of_blind_zero(tmp_path: Path) -> None:
    """UI-only (noarch) SPK start-stop-status `status` probes backend health and exits non-zero when offline."""
    spk = build_test_spk(tmp_path, arch="noarch", standalone=False, output_name="ui_only.spk")
    extracted = extract_spk(spk, tmp_path / "unpacked_ui")

    env = {
        "SYNOPKG_PKGDEST": str(extracted["target"]),
        "SYNOPKG_PKGVAR": str(tmp_path / "pkg_var_ui"),
        "APP_PORT": "1",  # Guaranteed offline port
    }
    res = run_posix_script(extracted["scripts"] / "start-stop-status", ["status"], env=env)
    assert res.returncode != 0, "UI-only SPK status must not blindly exit 0 when backend is unreachable"

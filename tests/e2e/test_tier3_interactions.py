"""Tier 3 — Pairwise Combinatorial Cross-Feature Interaction E2E Tests (Target v1.2.0 Specification).

Combines multiple subsystems through real public interfaces (live loopback HTTP servers,
real CGI subprocesses, SQLite WAL DB, real pipeline cycles, POSIX `/bin/sh` SPK scripts,
and Node `app.js` execution) across 10 high-value feature pairs.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import time
import urllib.parse
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import (
    OAUTH_CALLBACK_CGI_PATH,
    PROXY_CGI_PATH,
    MockDSMServer,
    MockWebhookServer,
    build_test_spk,
    build_zip_archive,
    extract_spk,
    image_to_jpeg_bytes,
    make_github_mock_client,
    make_isolated_settings,
    make_sidecar_payload,
    make_token_headers,
    run_app_js_scenario,
    run_cgi,
    run_posix_script,
    save_heic,
    save_jpeg,
    sha256_file,
    start_live_fastapi_server,
)
from fastapi.testclient import TestClient

from gphotos_synology.config import ConfigManager
from gphotos_synology.database import StateDB
from gphotos_synology.main import create_app
from gphotos_synology.profiles import CreateProfileRequest, ProfileRegistry
from gphotos_synology.sources.gdrive import GoogleDriveClient
from gphotos_synology.synology.photos_api import SynologyPhotosClient
from gphotos_synology.synology.scanner import ExistingLibraryScanner
from gphotos_synology.updater import UpdateService
from gphotos_synology.worker.pipeline import MigrationPipeline


def test_t3_p1_proxy_cgi_with_real_fastapi_c1_c2_g6(tmp_path: Path) -> None:
    """Pair 1: Real proxy.cgi subprocess -> live FastAPI server verifying C1 secret, C2 isolation, and G6 CSRF."""
    secret = "1a" * 32
    settings = make_isolated_settings(tmp_path, proxy_secret=secret)
    (settings.homes_dir / "alice" / "Photos").mkdir(parents=True)
    (settings.homes_dir / "bob" / "Photos").mkdir(parents=True)

    with start_live_fastapi_server(settings) as live:
        # Admin creates alice and bob profiles through real proxy.cgi
        r_a = run_cgi(
            PROXY_CGI_PATH,
            backend_url=live.url,
            method="POST",
            query_string="p=/api/profiles",
            body=json.dumps({"dsm_username": "alice", "display_name": "Alice", "target_space": "personal"}).encode(),
            content_type="application/json",
            proxy_secret=secret,
            dsm_user="admin",
            dsm_admin="1",
            http_headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert r_a.status == 200
        alice_id = r_a.json()["profile"]["id"]

        r_b = run_cgi(
            PROXY_CGI_PATH,
            backend_url=live.url,
            method="POST",
            query_string="p=/api/profiles",
            body=json.dumps({"dsm_username": "bob", "display_name": "Bob", "target_space": "personal"}).encode(),
            content_type="application/json",
            proxy_secret=secret,
            dsm_user="admin",
            dsm_admin="1",
            http_headers={"X-Requested-With": "XMLHttpRequest"},
        )
        assert r_b.status == 200
        bob_id = r_b.json()["profile"]["id"]

        # Non-admin alice can read her own profile via proxy.cgi
        r_self = run_cgi(
            PROXY_CGI_PATH,
            backend_url=live.url,
            method="GET",
            query_string="p=/api/status",
            proxy_secret=secret,
            dsm_user="alice",
            dsm_admin="0",
            http_headers={"X-Profile-Id": alice_id},
        )
        assert r_self.status == 200
        assert r_self.json()["profile"]["id"] == alice_id

        # Non-admin alice CANNOT read bob's profile via proxy.cgi (C2)
        r_cross = run_cgi(
            PROXY_CGI_PATH,
            backend_url=live.url,
            method="GET",
            query_string="p=/api/status",
            proxy_secret=secret,
            dsm_user="alice",
            dsm_admin="0",
            http_headers={"X-Profile-Id": bob_id},
        )
        assert r_cross.status == 403

        # Mutating call via proxy.cgi WITHOUT X-Requested-With fails with 403 (G6)
        r_no_csrf = run_cgi(
            PROXY_CGI_PATH,
            backend_url=live.url,
            method="PUT",
            query_string="p=/api/settings",
            body=json.dumps({"updates": {"dedup_mode": "strict_hash"}}).encode(),
            content_type="application/json",
            proxy_secret=secret,
            dsm_user="alice",
            dsm_admin="0",
            http_headers={"X-Profile-Id": alice_id},
        )
        assert r_no_csrf.status == 403

        # Wrong proxy secret from rogue CGI -> 401 (C1)
        r_bad_sec = run_cgi(
            PROXY_CGI_PATH,
            backend_url=live.url,
            method="GET",
            query_string="p=/api/status",
            proxy_secret="00" * 32,
            dsm_user="admin",
            dsm_admin="1",
        )
        assert r_bad_sec.status == 401


def test_t3_p2_oauth_callback_cgi_with_real_fastapi_g3_c2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pair 2: Real oauth_callback.cgi -> live FastAPI server enforcing G3 state/replay & C2 ownership."""
    secret = "2b" * 32
    settings = make_isolated_settings(
        tmp_path,
        proxy_secret=secret,
        google_client_id="cid-p2.apps.googleusercontent.com",
        google_client_secret="csec-p2",
    )
    (settings.homes_dir / "alice" / "Photos").mkdir(parents=True)
    (settings.homes_dir / "bob" / "Photos").mkdir(parents=True)

    exchanged: list[tuple[str, str]] = []

    def _fake_exchange(
        self: GoogleDriveClient, code: str, redirect_uri: str, code_verifier: str | None = None
    ) -> None:
        exchanged.append((code, redirect_uri))

    monkeypatch.setattr(GoogleDriveClient, "exchange_code", _fake_exchange)

    with start_live_fastapi_server(settings) as live:
        # Admin creates alice & bob
        for uname in ("alice", "bob"):
            r = run_cgi(
                PROXY_CGI_PATH,
                backend_url=live.url,
                method="POST",
                query_string="p=/api/profiles",
                body=json.dumps({"dsm_username": uname, "target_space": "personal"}).encode(),
                content_type="application/json",
                proxy_secret=secret,
                dsm_user="admin",
                dsm_admin="1",
                http_headers={"X-Requested-With": "XMLHttpRequest"},
            )
            assert r.status == 200

        # Alice initiates OAuth start via proxy.cgi
        r_start = run_cgi(
            PROXY_CGI_PATH,
            backend_url=live.url,
            method="GET",
            query_string="p=/api/oauth/google/start",
            proxy_secret=secret,
            dsm_user="alice",
            dsm_admin="0",
            http_headers={"X-Profile-Id": "alice"},
        )
        assert r_start.status == 200
        start_data = r_start.json()
        assert start_data["redirect_uri"].endswith("/webman/3rdparty/gphotos-migrator/oauth_callback.cgi")
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(start_data["authorization_url"]).query)["state"][0]

        # Bob tries to hijack Alice's callback via oauth_callback.cgi -> rejected by C2 ownership!
        r_bob = run_cgi(
            OAUTH_CALLBACK_CGI_PATH,
            backend_url=live.url,
            query_string=f"code=hijack&state={urllib.parse.quote(state)}&profile=alice&extra=dropme",
            proxy_secret=secret,
            dsm_user="bob",
            dsm_admin="0",
        )
        assert len(exchanged) == 0
        assert r_bob.status in (302, 307, 403)
        if r_bob.status in (302, 307):
            assert "oauth=error" in r_bob.headers.get("location", "")

        # Alice completes her own callback via oauth_callback.cgi -> succeeds ONCE
        r_alice = run_cgi(
            OAUTH_CALLBACK_CGI_PATH,
            backend_url=live.url,
            query_string=f"code=alice_ok&state={urllib.parse.quote(state)}&ignored=1",
            proxy_secret=secret,
            dsm_user="alice",
            dsm_admin="0",
        )
        assert r_alice.status in (302, 307)
        assert "oauth=success" in r_alice.headers.get("location", "")
        assert exchanged == [("alice_ok", start_data["redirect_uri"])]

        # Replay of Alice's same state fails closed (one-time use)
        r_replay = run_cgi(
            OAUTH_CALLBACK_CGI_PATH,
            backend_url=live.url,
            query_string=f"code=alice_replay&state={urllib.parse.quote(state)}",
            proxy_secret=secret,
            dsm_user="alice",
            dsm_admin="0",
        )
        assert len(exchanged) == 1
        assert "oauth=error" in r_replay.headers.get("location", "") or r_replay.status >= 400


def test_t3_p3_g1_failed_file_delete_guard_and_retry_counter_reset(tmp_path: Path) -> None:
    """Pair 3: G1 failed file prevents delete-on-finalize, sets completed_with_errors, then retries & resets counters."""
    settings = make_isolated_settings(
        tmp_path,
        inbox_post_action="delete",
        write_exif_metadata=True,
    )
    cfg = ConfigManager(settings)
    db = StateDB(settings.db_path)
    pipeline = MigrationPipeline(cfg, db)

    archive_path = build_zip_archive(
        settings.inbox_dir / "takeout-g1-retry.zip",
        {
            # Photo 1 goes to 2024/08 (writable -> succeeds)
            "Takeout/Google Photos/Photos from 2024/GOOD.jpg": image_to_jpeg_bytes(seed=101),
            "Takeout/Google Photos/Photos from 2024/GOOD.jpg.json": json.dumps(
                make_sidecar_payload("GOOD.jpg", timestamp=1723000000)  # Aug 2024
            ),
            # Photo 2 goes to 2024/07 (temporarily read-only -> fails transiently!)
            "Takeout/Google Photos/Photos from 2024/RETRY_ME.jpg": image_to_jpeg_bytes(seed=102),
            "Takeout/Google Photos/Photos from 2024/RETRY_ME.jpg.json": json.dumps(
                make_sidecar_payload("RETRY_ME.jpg", timestamp=1720000000)  # Jul 2024
            ),
        },
    )

    blocked_dir = settings.photos_dir / "2024" / "07"
    blocked_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(blocked_dir, 0o555)
    try:
        # Cycle 1: 1 succeeds, 1 fails -> archive MUST stay in inbox (NOT deleted!) & status=completed_with_errors
        pipeline.run_cycle()
        assert archive_path.is_file(), "G1: source archive with failed files MUST NOT be deleted!"
        archives_after_1 = db.list_archives()
        assert len(archives_after_1) == 1
        a1 = archives_after_1[0]
        assert a1["status"] == "completed_with_errors"
        assert a1["completed_at"] is None
        assert a1["failed_files"] == 1
        assert a1["imported_files"] == 1
        assert db.get_stats()["completed_archives"] == 0
    finally:
        os.chmod(blocked_dir, 0o755)

    # Cycle 2: transient issue resolved -> archive retried, counters reset, GOOD skipped as dup, RETRY_ME imported, now deleted
    pipeline.run_cycle()
    assert not archive_path.exists(), "After clean retry (failed==0), inbox_post_action='delete' removes archive"
    archives_after_2 = db.list_archives()
    a2 = archives_after_2[0]
    assert a2["status"] == "completed"
    assert a2["completed_at"] is not None
    assert a2["failed_files"] == 0
    assert a2["imported_files"] == 1
    assert a2["duplicate_files"] == 1
    assert (settings.photos_dir / "2024" / "07" / "RETRY_ME.jpg").is_file()
    assert (settings.photos_dir / "2024" / "08" / "GOOD.jpg").is_file()


def test_t3_p4_exif_repair_post_repair_dedup_upgrade_restore_and_purge_reject(tmp_path: Path) -> None:
    """Pair 4: EXIF repair + quality upgrade records sidecar metadata -> restore preserves old_rel_path/GPS -> purge rejected."""
    token = "p4-token"
    settings = make_isolated_settings(
        tmp_path,
        api_token=token,
        dedup_mode="perceptual",
        duplicate_quality_policy="auto_upgrade_quarantine",
        write_exif_metadata=True,
    )
    app = create_app(base_settings=settings, start_worker=False)
    pipeline: MigrationPipeline = app.state.ctx.pipeline
    db: StateDB = app.state.ctx.db

    # Archive 1 imports low-res photo with rich sidecar time & GPS
    build_zip_archive(
        settings.inbox_dir / "takeout-batch1.zip",
        {
            "Takeout/Google Photos/Photos from 2024/mountain.jpg": image_to_jpeg_bytes(
                width=320, height=240, seed=111, quality=65
            ),
            "Takeout/Google Photos/Photos from 2024/mountain.jpg.json": json.dumps(
                make_sidecar_payload(
                    "mountain.jpg",
                    timestamp=1720000000,
                    description="Alps",
                    lat=46.5197,
                    lon=7.9631,
                )
            ),
        },
    )
    pipeline.run_cycle()
    orig_items = db.get_indexed_files_by_rel_path()
    assert len(orig_items) == 1
    old_rel_path = next(iter(orig_items.keys()))

    # Archive 2 imports high-res version of same scene -> triggers quality upgrade
    build_zip_archive(
        settings.inbox_dir / "takeout-batch2.zip",
        {
            "Takeout/Google Photos/Photos from 2024/mountain_4k.jpg": image_to_jpeg_bytes(
                width=1280, height=960, seed=111, quality=95
            ),
            "Takeout/Google Photos/Photos from 2024/mountain_4k.jpg.json": json.dumps(
                make_sidecar_payload("mountain_4k.jpg", timestamp=1720000000)
            ),
        },
    )
    pipeline.run_cycle()
    upgrades = db.list_quality_upgrades()
    assert len(upgrades) == 1
    upg = upgrades[0]
    assert upg["old_rel_path"] == old_rel_path
    assert upg["old_latitude"] == pytest.approx(46.5197, abs=1e-3)
    assert upg["old_longitude"] == pytest.approx(7.9631, abs=1e-3)

    # Restore via HTTP API -> back at old_rel_path with sidecar GPS restored in media_items
    with TestClient(app) as client:
        r_restore = client.post(
            f"/api/upgrades/{upg['id']}/restore",
            headers=make_token_headers(token),
        )
        assert r_restore.status_code == 200
        assert r_restore.json()["restored_rel_path"] == old_rel_path
        assert (settings.photos_dir / old_rel_path).is_file()

        with sqlite3.connect(settings.db_path) as conn:
            row = conn.execute(
                "SELECT taken_at, latitude, longitude FROM media_items WHERE id = ?",
                (upg["media_id"],),
            ).fetchone()
        assert row is not None
        assert row[0] and "2024-07-03" in str(row[0])
        assert row[1] == pytest.approx(46.5197, abs=1e-3)
        assert row[2] == pytest.approx(7.9631, abs=1e-3)

        # Subsequent purge of already-restored upgrade MUST fail with HTTP 400
        r_purge = client.post(
            f"/api/upgrades/{upg['id']}/purge",
            headers=make_token_headers(token),
        )
        assert r_purge.status_code == 400


def test_t3_p5_g10_heic_undecodable_scanner_incremental_skip_and_g11_perceptual_dedup(
    tmp_path: Path,
) -> None:
    """Pair 5: Valid HEIC + corrupt HEIC indexed once, skipped on cycle 2; recompressed HEIC triggers perceptual-dedup log."""
    settings = make_isolated_settings(
        tmp_path,
        dedup_mode="perceptual",
        duplicate_quality_policy="keep_existing",
        write_exif_metadata=False,
    )
    cfg = ConfigManager(settings)
    db = StateDB(settings.db_path)
    pipeline = MigrationPipeline(cfg, db)

    valid_heic = save_heic(settings.photos_dir / "2024/07/iphone_orig.heic", width=640, height=480, seed=120, quality=92)
    corrupt_heic = settings.photos_dir / "2024/07/corrupt_raw.heic"
    corrupt_heic.write_bytes(b"CORRUPT_HEIC_DATA_BYTES_XYZ")

    scanner = ExistingLibraryScanner(settings, db)
    s1 = scanner.scan()
    assert s1.newly_indexed == 2
    assert s1.unchanged_skipped == 0

    # Second scan skips BOTH valid HEIC and undecodable HEIC cleanly
    s2 = scanner.scan()
    assert s2.newly_indexed == 0
    assert s2.unchanged_skipped == 2

    # Takeout contains a recompressed lower-quality HEIC of the exact same photo -> perceptual dedup skip
    recomp_heic = save_heic(tmp_path / "staging" / "iphone_orig.heic", width=480, height=360, seed=120, quality=60)
    build_zip_archive(
        settings.inbox_dir / "takeout-heic-dup.zip",
        {
            "Takeout/Google Photos/Photos from 2024/iphone_orig.heic": recomp_heic.read_bytes(),
        },
    )
    pipeline.run_cycle()

    # Original stays sole copy in library; distinct `perceptual-dedup` event logged
    assert list(settings.photos_dir.rglob("*.heic")) and valid_heic.is_file()
    archives = db.list_archives()
    assert archives[0]["duplicate_files"] == 1
    assert archives[0]["imported_files"] == 0
    logs = db.get_recent_logs(limit=100)
    assert any(entry["component"] == "perceptual-dedup" for entry in logs), (
        f"G11 requires distinct 'perceptual-dedup' log event; got components={set(e['component'] for e in logs)}"
    )


def test_t3_p6_multi_profile_lazy_oauth_delete_cleanup_and_recreate(tmp_path: Path) -> None:
    """Pair 6: Lazy OAuth inheritance + rotation + delete_profile disk cleanup + clean recreate."""
    settings = make_isolated_settings(tmp_path)
    (settings.homes_dir / "alice" / "Photos").mkdir(parents=True)
    registry = ProfileRegistry(settings)

    alice_ctx = registry.create_profile(
        CreateProfileRequest(dsm_username="alice", display_name="Alice", target_space="personal")
    )
    alice_id = alice_ctx.profile.id
    alice_settings = alice_ctx.config_manager.settings

    # Update & rotate default profile OAuth creds -> propagates immediately to alice
    default_ctx = registry.get_context("default")
    default_ctx.config_manager.update(
        {"google_client_id": "cid-v1", "google_client_secret": "sec-v1"}
    )
    assert alice_ctx.config_manager.settings.google_client_id == "cid-v1"

    default_ctx.config_manager.update(
        {"google_client_id": "cid-v2", "google_client_secret": "sec-v2"}
    )
    assert alice_ctx.config_manager.settings.google_client_id == "cid-v2"
    assert alice_ctx.config_manager.settings.google_client_secret == "sec-v2"

    # Populate alice's token, DB, inbox, quarantine, AND a photo in her photos_dir
    alice_settings.token_path.write_text('{"token":"live-refresh"}', encoding="utf-8")
    alice_ctx.db.set_state("sentinel", "old-alice-state")
    (alice_settings.inbox_dir / "leftover.zip").write_bytes(b"zip")
    (alice_settings.quarantine_dir / "q.jpg").write_bytes(b"q")
    alice_photo = save_jpeg(alice_settings.photos_dir / "keep_me.jpg", seed=130)

    # Delete alice -> disk artifacts removed, photos_dir untouched
    registry.delete_profile(alice_id)
    assert not alice_settings.data_dir.exists()
    assert not alice_settings.work_dir.exists()
    assert not alice_settings.inbox_dir.exists()
    assert alice_photo.is_file(), "delete_profile MUST NOT delete user's photos_dir"

    # Recreating alice starts completely clean (no resurrected token or DB state)
    recreated = registry.create_profile(
        CreateProfileRequest(dsm_username="alice", display_name="Alice New", target_space="personal")
    )
    assert not recreated.config_manager.settings.token_path.exists()
    assert recreated.db.get_state("sentinel") is None


def test_t3_p7_g4_url_change_password_clear_and_synology_post_sid_relogin(
    tmp_path: Path, mock_dsm_server: MockDSMServer
) -> None:
    """Pair 7: G4 URL change prevents exfiltrating stored password to attacker server; fresh creds use POST _sid + code 119 relogin."""
    token = "p7-token"
    settings = make_isolated_settings(
        tmp_path,
        api_token=token,
        synology_url="https://legitimate-nas.local:5001",
        synology_username=mock_dsm_server.state.valid_username,
        synology_password=mock_dsm_server.state.valid_password,
        synology_verify_ssl=False,
    )
    app = create_app(base_settings=settings, start_worker=False)

    with TestClient(app) as client:
        # Attacker changes synology_url to attacker-controlled mock_dsm_server sending masked password
        r_upd = client.put(
            "/api/settings",
            json={
                "updates": {
                    "synology_url": mock_dsm_server.url,
                    "synology_password": "********",
                }
            },
            headers=make_token_headers(token),
        )
        assert r_upd.status_code == 200
        # Stored password MUST be cleared
        assert r_upd.json()["settings"]["synology_password"] == ""

        # Calling test-synology MUST NOT leak the old password to mock_dsm_server!
        r_test = client.post("/api/actions/test-synology", headers=make_token_headers(token))
        assert r_test.status_code == 400
        for req in mock_dsm_server.state.requests:
            assert mock_dsm_server.state.valid_password.encode() not in req.body
            assert mock_dsm_server.state.valid_password not in req.raw_url

        # Now user legitimately supplies fresh plaintext password -> test-synology succeeds
        r_upd2 = client.put(
            "/api/settings",
            json={
                "updates": {
                    "synology_url": mock_dsm_server.url,
                    "synology_password": mock_dsm_server.state.valid_password,
                }
            },
            headers=make_token_headers(token),
        )
        assert r_upd2.status_code == 200
        r_test2 = client.post("/api/actions/test-synology", headers=make_token_headers(token))
        assert r_test2.status_code == 200

    # Verify POST _sid + code 119 relogin on live client
    mock_dsm_server.state.expire_next_api_calls = 1
    with SynologyPhotosClient(app.state.ctx.config_manager.settings) as dsm_client:
        dsm_client.list_albums()
    for req in mock_dsm_server.state.requests:
        assert "_sid" not in req.query


def test_t3_p8_spk_deterministic_double_build_lifecycle_and_updater_checksum(tmp_path: Path) -> None:
    """Pair 8: Two SPK builds match SHA-256 byte-for-byte (G7), lifecycle scripts work (G8), updater surfaces .sha256."""
    bdir = tmp_path / "shared_bundle"
    bdir.mkdir()
    exe = bdir / "gphotos-migrator"
    exe.write_text("#!/bin/sh\ntrap 'exit 0' TERM INT\nwhile :; do sleep 1; done\n", encoding="utf-8")
    exe.chmod(0o755)

    spk1 = build_test_spk(
        tmp_path,
        arch="x86_64",
        standalone=True,
        build_sha="deadbeef1234",
        build_date="2026-06-01T00:00:00Z",
        output_name="build1.spk",
        bundle_dir=bdir,
    )
    time.sleep(1.1)  # Ensure wall clock ticks past 1s to catch non-deterministic mtimes/gzip headers
    spk2 = build_test_spk(
        tmp_path,
        arch="x86_64",
        standalone=True,
        build_sha="deadbeef1234",
        build_date="2026-06-01T00:00:00Z",
        output_name="build2.spk",
        bundle_dir=bdir,
    )

    digest1 = sha256_file(spk1)
    digest2 = sha256_file(spk2)
    assert digest1 == digest2, f"G7 deterministic SPK build failed: {digest1} != {digest2}"

    # Unpack & run start-stop-status + preuninst verifying 0600 proxy.secret excluded from backup
    extracted = extract_spk(spk1, tmp_path / "unpacked")
    pkg_var = tmp_path / "pkg_var"
    photos_dir = tmp_path / "photos"
    pkg_var.mkdir()
    photos_dir.mkdir()
    env = {
        "SYNOPKG_PKGDEST": str(extracted["target"]),
        "SYNOPKG_PKGVAR": str(pkg_var),
        "PHOTOS_DIR": str(photos_dir),
    }
    sss = extracted["scripts"] / "start-stop-status"
    try:
        assert run_posix_script(sss, ["start"], env=env).returncode == 0
        assert stat.S_IMODE((pkg_var / "proxy.secret").stat().st_mode) == 0o600
    finally:
        run_posix_script(sss, ["stop"], env=env)

    (pkg_var / "config").mkdir(parents=True, exist_ok=True)
    (pkg_var / "config" / "state.db").write_bytes(b"db")
    assert (
        run_posix_script(
            extracted["scripts"] / "preuninst",
            env={**env, "SYNOPKG_PKG_STATUS": "UNINSTALL"},
        ).returncode
        == 0
    )
    backup_root = photos_dir / ".gphotos-migrator-backup"
    assert list(backup_root.rglob("state.db"))
    assert not list(backup_root.rglob("proxy.secret"))

    # Updater surfaces .sha256 for the built artifact
    settings = make_isolated_settings(tmp_path / "upd", app_build_sha="deadbeef")

    def _gh_handler(req: httpx.Request) -> httpx.Response:
        if str(req.url).endswith("/releases/latest"):
            return httpx.Response(
                200,
                json={
                    "tag_name": "v1.3.0",
                    "html_url": "https://example.invalid/v1.3.0",
                    "assets": [
                        {
                            "name": "gphotos-migrator-dsm7-x86_64.spk",
                            "browser_download_url": "https://example.invalid/pkg-x86_64.spk",
                        },
                        {
                            "name": "gphotos-migrator-dsm7-x86_64.spk.sha256",
                            "browser_download_url": "https://example.invalid/pkg-x86_64.spk.sha256",
                        },
                    ],
                },
            )
        if str(req.url).endswith(".sha256"):
            return httpx.Response(200, text=f"{digest1}  gphotos-migrator-dsm7-x86_64.spk\n")
        return httpx.Response(
            200,
            json={"sha": "9999999999999999999999999999999999999999", "commit": {"message": "v1.3.0", "committer": {"date": "2026-07-01T00:00:00Z"}}},
        )

    with make_github_mock_client(_gh_handler) as client:
        st = UpdateService().get_status(settings, force=True, http_client=client)
    assert st["update_available"] is True
    assert any(
        ("sha256" in k.lower() or "checksum" in k.lower()) and v for k, v in st.items()
    )


def test_t3_p9_frontend_app_js_xss_defense_header_merge_and_cgi_routing() -> None:
    """Pair 9: app.js in CGI proxy mode escapes XSS quotes, routes via proxy.cgi?p=..., and sends CSRF headers."""
    xss_archive = 'bad" onclick="alert(1)\' ><img src=x>'
    result = run_app_js_scenario(
        pathname="/webman/3rdparty/gphotos-migrator/proxy.cgi",
        routes={
            "/api/archives": {
                "status": 200,
                "json": {
                    "archives": [
                        {
                            "id": 1,
                            "filename": xss_archive,
                            "source": "inbox",
                            "status": "completed_with_errors",
                            "imported_files": 1,
                            "duplicate_files": 0,
                            "failed_files": 1,
                            "error_message": xss_archive,
                        }
                    ]
                },
            }
        },
        actions=[
            {"type": "click", "id": "btn-scan"},
            {"type": "click", "id": "btn-purge-all"},
        ],
        inspect_ids=["archives-tbody"],
    )

    html = result["elements"]["archives-tbody"]["innerHTML"]
    assert 'onclick="alert(1)"' not in html
    assert "<img src=x>" not in html
    assert "&quot;" in html

    # All API calls routed via proxy.cgi?p=...
    api_calls = [c for c in result["fetchCalls"] if "api" in c["url"]]
    assert len(api_calls) >= 4
    assert all("proxy.cgi?p=" in c["url"] for c in api_calls)

    # Every mutating call includes X-Requested-With, Content-Type, and X-Profile-Id
    mutating = [c for c in api_calls if c["method"] in ("POST", "PUT", "PATCH", "DELETE")]
    assert len(mutating) >= 2
    for call in mutating:
        assert call["headers"].get("x-requested-with"), f"Missing X-Requested-With on {call}"
        assert call["headers"].get("x-profile-id") == "default"
        assert "application/json" in call["headers"].get("content-type", "")


def test_t3_p10_album_pending_index_root_scope_scanner_isolation_and_throttled_async_notifier(
    tmp_path: Path,
    mock_dsm_server: MockDSMServer,
    mock_webhook_server: MockWebhookServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pair 10: Scanner error isolation + root scoping + pending album retries + 12h throttled async disk alert."""
    settings = make_isolated_settings(
        tmp_path,
        synology_api_enabled=True,
        synology_url=mock_dsm_server.url,
        synology_username=mock_dsm_server.state.valid_username,
        synology_password=mock_dsm_server.state.valid_password,
        synology_verify_ssl=False,
        webhook_url=mock_webhook_server.url,
        webhook_type="generic",
    )
    cfg = ConfigManager(settings)
    db = StateDB(settings.db_path)
    pipeline = MigrationPipeline(cfg, db)

    # 1. Scanner isolation: force index_existing_library() to raise, verify inbox STILL imports!
    build_zip_archive(
        settings.inbox_dir / "takeout-iso.zip",
        {
            "Takeout/Google Photos/Tokyo Trip/IMG_TKY.jpg": image_to_jpeg_bytes(seed=140),
            "Takeout/Google Photos/Tokyo Trip/IMG_TKY.jpg.json": json.dumps(
                make_sidecar_payload("IMG_TKY.jpg", timestamp=1720000000)
            ),
        },
    )

    def _exploding_scan(self: ExistingLibraryScanner, *a: Any, **kw: Any) -> Any:
        raise PermissionError("Simulated unreadable NAS folder")

    monkeypatch.setattr(ExistingLibraryScanner, "scan", _exploding_scan)
    summary = pipeline.run_cycle()
    assert summary["errors"] >= 1
    assert summary["inbox_processed"] == 1
    assert list(settings.photos_dir.rglob("IMG_TKY.jpg"))

    # Restore scanner
    monkeypatch.undo()

    # 2. Album membership stays pending with retry_count == 0 across 22 cycles before indexing
    for _ in range(22):
        assert pipeline.sync_pending_albums() == 0
    pending = db.get_pending_album_memberships()
    assert len(pending) == 1
    assert pending[0]["retry_count"] == 0

    # Index photo in DSM -> syncs on cycle 23!
    mock_dsm_server.state.add_indexed_item(pending[0]["dest_rel_path"])
    assert pipeline.sync_pending_albums() == 1
    assert db.get_pending_album_memberships() == []

    # 3. 12h disk-guard throttle + async dispatch: 3 low-disk cycles trigger ONLY 1 webhook
    mock_webhook_server.requests.clear()
    cfg.update({"min_free_disk_gb": 1_000_000.0})
    build_zip_archive(
        settings.inbox_dir / "takeout-lowdisk.zip",
        {"Takeout/Google Photos/2024/L.jpg": image_to_jpeg_bytes(seed=141)},
    )
    for _ in range(3):
        pipeline.run_cycle()

    # Wait briefly for async daemon thread delivery
    time.sleep(0.4)
    assert len(mock_webhook_server.requests) == 1, (
        f"Expected 1 throttled disk-guard alert across 3 cycles, got {len(mock_webhook_server.requests)}"
    )

"""Tier 4 — Real-World End-to-End Application Workloads (Target v1.2.0 Specification).

Exercises 6 complete multi-step NAS user journeys end-to-end across HTTP APIs, real CGI
subprocesses, live loopback servers, Takeout ingestion, crash recovery, SPK lifecycle
scripts, update checks, and frontend JS rendering.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import time
from pathlib import Path

import httpx
import piexif
import pytest
from conftest import (
    OAUTH_CALLBACK_CGI_PATH,
    PROXY_CGI_PATH,
    REPO_ROOT,
    MockDSMServer,
    MockWebhookServer,
    build_tar_gz_archive,
    build_test_spk,
    build_zip_archive,
    extract_spk,
    image_to_jpeg_bytes,
    make_corrupt_exif_jpeg_bytes,
    make_github_mock_client,
    make_isolated_settings,
    make_sidecar_payload,
    make_token_headers,
    run_app_js_scenario,
    run_cgi,
    run_posix_script,
    save_jpeg,
    sha256_file,
    start_live_fastapi_server,
)
from fastapi.testclient import TestClient

import gphotos_synology
from gphotos_synology.config import ConfigManager
from gphotos_synology.database import StateDB
from gphotos_synology.main import create_app
from gphotos_synology.sources.gdrive import GoogleDriveClient
from gphotos_synology.worker.pipeline import MigrationPipeline


def test_t4_w1_multi_user_dsm_household_migration(tmp_path: Path) -> None:
    """Workload 1: Multi-user DSM household setup, lazy OAuth inheritance, isolated migrations via proxy.cgi."""
    secret = "w1" * 32
    settings = make_isolated_settings(tmp_path, proxy_secret=secret)
    (settings.homes_dir / "alice" / "Photos").mkdir(parents=True)
    (settings.homes_dir / "bob" / "Photos").mkdir(parents=True)

    with start_live_fastapi_server(settings) as live:
        # 1. Admin configures shared Google OAuth client on default profile
        r_oauth_cfg = run_cgi(
            PROXY_CGI_PATH,
            backend_url=live.url,
            method="PUT",
            query_string="p=/api/settings",
            body=json.dumps(
                {
                    "updates": {
                        "google_client_id": "household-client.apps.googleusercontent.com",
                        "google_client_secret": "household-client-secret",
                    }
                }
            ).encode(),
            content_type="application/json",
            proxy_secret=secret,
            dsm_user="admin",
            dsm_admin="1",
            http_headers={"X-Requested-With": "XMLHttpRequest", "X-Profile-Id": "default"},
        )
        assert r_oauth_cfg.status == 200

        # 2. Admin creates personal profiles for alice & bob with legitimate nested subfolders
        created_ids: dict[str, str] = {}
        for user, sub in (("alice", "GooglePhotos/Alice"), ("bob", "GooglePhotos/Bob")):
            r_create = run_cgi(
                PROXY_CGI_PATH,
                backend_url=live.url,
                method="POST",
                query_string="p=/api/profiles",
                body=json.dumps(
                    {
                        "dsm_username": user,
                        "display_name": user.title(),
                        "target_space": "personal",
                        "custom_subfolder": sub,
                    }
                ).encode(),
                content_type="application/json",
                proxy_secret=secret,
                dsm_user="admin",
                dsm_admin="1",
                http_headers={"X-Requested-With": "XMLHttpRequest"},
            )
            assert r_create.status == 200
            created_ids[user] = r_create.json()["profile"]["id"]

        # 3. Alice & Bob verify lazy OAuth inheritance & strict profile visibility via proxy.cgi
        for user, other in (("alice", "bob"), ("bob", "alice")):
            pid = created_ids[user]
            other_pid = created_ids[other]

            r_profs = run_cgi(
                PROXY_CGI_PATH,
                backend_url=live.url,
                method="GET",
                query_string="p=/api/profiles",
                proxy_secret=secret,
                dsm_user=user,
                dsm_admin="0",
            )
            assert r_profs.status == 200
            pdata = r_profs.json()
            assert pdata["is_admin"] is False
            assert [p["id"] for p in pdata["profiles"]] == [pid]

            r_set = run_cgi(
                PROXY_CGI_PATH,
                backend_url=live.url,
                method="GET",
                query_string="p=/api/settings",
                proxy_secret=secret,
                dsm_user=user,
                dsm_admin="0",
                http_headers={"X-Profile-Id": pid},
            )
            assert r_set.status == 200
            assert r_set.json()["settings"]["google_client_id"] == "household-client.apps.googleusercontent.com"

            # Cross-user access blocked with 403
            r_forbidden = run_cgi(
                PROXY_CGI_PATH,
                backend_url=live.url,
                method="GET",
                query_string="p=/api/status",
                proxy_secret=secret,
                dsm_user=user,
                dsm_admin="0",
                http_headers={"X-Profile-Id": other_pid},
            )
            assert r_forbidden.status == 403

        # 4. Drop distinct Takeout archives into Alice's and Bob's isolated inboxes & run their pipelines
        registry = live.app.state.ctx.registry
        alice_ctx = registry.get_context(created_ids["alice"])
        bob_ctx = registry.get_context(created_ids["bob"])

        build_zip_archive(
            alice_ctx.config_manager.settings.inbox_dir / "takeout-alice.zip",
            {
                "Takeout/Google Photos/2024/ALICE_ONLY.jpg": image_to_jpeg_bytes(seed=201),
                "Takeout/Google Photos/2024/ALICE_ONLY.jpg.json": json.dumps(
                    make_sidecar_payload("ALICE_ONLY.jpg", timestamp=1720000000)
                ),
            },
        )
        build_zip_archive(
            bob_ctx.config_manager.settings.inbox_dir / "takeout-bob.zip",
            {
                "Takeout/Google Photos/2024/BOB_ONLY.jpg": image_to_jpeg_bytes(seed=202),
                "Takeout/Google Photos/2024/BOB_ONLY.jpg.json": json.dumps(
                    make_sidecar_payload("BOB_ONLY.jpg", timestamp=1720100000)
                ),
            },
        )

        alice_ctx.pipeline.run_cycle()
        bob_ctx.pipeline.run_cycle()

        # Verify complete filesystem and API isolation
        assert list((settings.homes_dir / "alice" / "Photos" / "GooglePhotos" / "Alice").rglob("ALICE_ONLY.jpg"))
        assert not list((settings.homes_dir / "alice").rglob("BOB_ONLY.jpg"))
        assert list((settings.homes_dir / "bob" / "Photos" / "GooglePhotos" / "Bob").rglob("BOB_ONLY.jpg"))
        assert not list((settings.homes_dir / "bob").rglob("ALICE_ONLY.jpg"))

        r_alice_st = run_cgi(
            PROXY_CGI_PATH,
            backend_url=live.url,
            method="GET",
            query_string="p=/api/status",
            proxy_secret=secret,
            dsm_user="alice",
            dsm_admin="0",
            http_headers={"X-Profile-Id": created_ids["alice"]},
        )
        assert r_alice_st.status == 200
        assert r_alice_st.json()["stats"]["takeout_media"] == 1


def test_t4_w2_incremental_multi_archive_batch_with_failures_safety_caps_and_retry(
    tmp_path: Path,
) -> None:
    """Workload 2: Multi-archive ZIP+TAR batch with G9 strict (1), G13 corrupt EXIF, G12 ZipSlip, and G1 failure+retry."""
    settings = make_isolated_settings(
        tmp_path,
        inbox_post_action="delete",
        write_exif_metadata=True,
    )
    cfg = ConfigManager(settings)
    db = StateDB(settings.db_path)
    pipeline = MigrationPipeline(cfg, db)

    corrupt_bytes = make_corrupt_exif_jpeg_bytes(seed=210)
    # Archive 1 (ZIP): valid photo + distinct (1) duplicate without numbered JSON (G9) + corrupt EXIF (G13)
    a1 = build_zip_archive(
        settings.inbox_dir / "takeout-20240703T100000Z-001.zip",
        {
            "Takeout/Google Photos/Photos from 2024/IMG_500.jpg": image_to_jpeg_bytes(seed=211),
            "Takeout/Google Photos/Photos from 2024/IMG_500.jpg.json": json.dumps(
                make_sidecar_payload("IMG_500.jpg", timestamp=1720000000, description="Base Photo")
            ),
            # Distinct photo colliding on (1) name with NO numbered sidecar -> MUST NOT inherit IMG_500.jpg.json!
            "Takeout/Google Photos/Photos from 2024/IMG_500(1).jpg": image_to_jpeg_bytes(seed=212),
            # Corrupt EXIF JPEG -> MUST be imported with exact bytes preserved (G13)
            "Takeout/Google Photos/Photos from 2024/CORRUPT_EXIF.jpg": corrupt_bytes,
            "Takeout/Google Photos/Photos from 2024/CORRUPT_EXIF.jpg.json": json.dumps(
                make_sidecar_payload("CORRUPT_EXIF.jpg", timestamp=1720000000)
            ),
        },
    )

    # Archive 2 (TAR.GZ): contains a photo going to 2024/06 (temporarily blocked -> G1 failure) + a good photo in 2024/08
    a2 = build_tar_gz_archive(
        settings.inbox_dir / "takeout-20240703T100000Z-002.tgz",
        {
            "Takeout/Google Photos/Photos from 2024/JUNE_FAIL.jpg": image_to_jpeg_bytes(seed=213),
            "Takeout/Google Photos/Photos from 2024/JUNE_FAIL.jpg.json": json.dumps(
                make_sidecar_payload("JUNE_FAIL.jpg", timestamp=1718000000)  # June 2024
            ),
            "Takeout/Google Photos/Photos from 2024/AUG_OK.jpg": image_to_jpeg_bytes(seed=214),
            "Takeout/Google Photos/Photos from 2024/AUG_OK.jpg.json": json.dumps(
                make_sidecar_payload("AUG_OK.jpg", timestamp=1723000000)  # Aug 2024
            ),
        },
        symlinks={"Takeout/Google Photos/evil_symlink": "/etc/passwd"},
    )

    june_dir = settings.photos_dir / "2024" / "06"
    june_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(june_dir, 0o555)
    try:
        pipeline.run_cycle()

        # Archive 1 succeeded cleanly -> deleted by inbox_post_action='delete'
        assert not a1.exists()
        # Archive 2 had a failed file -> MUST survive in inbox despite inbox_post_action='delete' (G1)
        assert a2.is_file()

        # Verify G13: CORRUPT_EXIF.jpg was NOT wiped (bytes bit-for-bit identical)
        corrupt_imported = list(settings.photos_dir.rglob("CORRUPT_EXIF.jpg"))
        assert len(corrupt_imported) == 1
        assert corrupt_imported[0].read_bytes() == corrupt_bytes

        # Verify G9: IMG_500(1).jpg did NOT inherit IMG_500.jpg's 2024-07-03 description/EXIF
        dup_imported = list(settings.photos_dir.rglob("IMG_500(1).jpg"))
        assert len(dup_imported) == 1
        dup_exif = piexif.load(str(dup_imported[0]))
        assert dup_exif["0th"].get(piexif.ImageIFD.ImageDescription) != b"Base Photo"

        # Verify Archive 2 recorded completed_with_errors
        archives_map = {a["filename"]: a for a in db.list_archives()}
        assert archives_map[a1.name]["status"] == "completed"
        assert archives_map[a2.name]["status"] == "completed_with_errors"
        assert archives_map[a2.name]["failed_files"] == 1
    finally:
        os.chmod(june_dir, 0o755)

    # Cycle 2: June directory writable again -> Archive 2 retried, succeeds, and is now deleted
    pipeline.run_cycle()
    assert not a2.exists()
    assert (settings.photos_dir / "2024" / "06" / "JUNE_FAIL.jpg").is_file()
    archives_map2 = {a["filename"]: a for a in db.list_archives()}
    assert archives_map2[a2.name]["status"] == "completed"
    assert archives_map2[a2.name]["failed_files"] == 0


def test_t4_w3_quality_upgrade_crash_recovery_and_api_restore_purge_workflow(tmp_path: Path) -> None:
    """Workload 3: Upgrade 2 photos + crash-recovery on in_progress swap + fresh stat guard + API preview/restore/purge."""
    token = "w3-token"
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

    # 1. Pre-populate NAS library with 4 photos
    save_jpeg(settings.photos_dir / "Legacy/old_a.jpg", width=320, height=240, seed=221, quality=55)
    save_jpeg(settings.photos_dir / "Legacy/old_b.jpg", width=320, height=240, seed=222, quality=55)
    old_c = save_jpeg(settings.photos_dir / "Legacy/old_c.jpg", width=320, height=240, seed=223, quality=55)
    old_d = save_jpeg(settings.photos_dir / "Legacy/old_d.jpg", width=320, height=240, seed=224, quality=55)
    pipeline.index_existing_library()

    # Externally upgrade old_d on disk WITHOUT telling the DB -> tests G2 fresh stat/re-fingerprint!
    save_jpeg(old_d, width=1600, height=1200, seed=224, quality=96)
    old_d_bytes = old_d.read_bytes()

    # 2. Takeout upgrades old_a & old_b, and submits a medium-res copy of old_d (which must LOSE to fresh 1600x1200 on disk!)
    build_zip_archive(
        settings.inbox_dir / "takeout-w3.zip",
        {
            "Takeout/Google Photos/Photos from 2024/old_a.jpg": image_to_jpeg_bytes(
                width=960, height=720, seed=221, quality=95
            ),
            "Takeout/Google Photos/Photos from 2024/old_b.jpg": image_to_jpeg_bytes(
                width=960, height=720, seed=222, quality=95
            ),
            "Takeout/Google Photos/Photos from 2024/old_d.jpg": image_to_jpeg_bytes(
                width=800, height=600, seed=224, quality=90
            ),
        },
    )
    pipeline.run_cycle()

    # old_d MUST NOT have been downgraded by the 800x600 Takeout copy!
    assert old_d.is_file() and old_d.read_bytes() == old_d_bytes
    upgrades = db.list_quality_upgrades()
    assert len(upgrades) == 2

    # 3. Simulate mid-swap power loss on old_c: write-ahead 'in_progress' row + moved to quarantine + missing from photos_dir
    assert hasattr(db, "create_pending_quality_upgrade"), "G2 requires StateDB.create_pending_quality_upgrade"
    assert hasattr(db, "list_in_progress_quality_upgrades"), "G2 requires StateDB.list_in_progress_quality_upgrades"
    indexed = db.get_indexed_files_by_rel_path()
    c_media_id = indexed["Legacy/old_c.jpg"]["id"]
    c_bytes = old_c.read_bytes()
    q_crash_name = "crash_recovery_old_c.jpg"
    settings.quarantine_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(old_c), str(settings.quarantine_dir / q_crash_name))
    assert not old_c.exists()

    db.create_pending_quality_upgrade(
        media_id=c_media_id,
        kept_rel_path="2024/07/old_c_new.jpg",
        quarantine_filename=q_crash_name,
        old_filename="old_c.jpg",
        new_filename="old_c_new.jpg",
        old_width=320,
        old_height=240,
        old_file_size=len(c_bytes),
        old_sharpness=10.0,
        new_width=960,
        new_height=720,
        new_file_size=len(c_bytes) * 3,
        new_sharpness=30.0,
        match_reason="perceptual_dhash",
        upgrade_reason="resolution",
        old_rel_path="Legacy/old_c.jpg",
        old_taken_at="",
        old_latitude=None,
        old_longitude=None,
    )
    assert len(db.list_in_progress_quality_upgrades()) == 1

    # Fresh pipeline startup / cycle recovers the crashed swap automatically!
    recovered_pipeline = MigrationPipeline(app.state.ctx.config_manager, db)
    recovered_pipeline.run_cycle()
    assert db.list_in_progress_quality_upgrades() == []
    assert old_c.is_file(), "Crash recovery must restore un-swapped original back to old_rel_path"
    assert old_c.read_bytes() == c_bytes

    # 4. User inspects preview, restores upgrade #1 via API, fails purge-after-restore, and purges remaining via purge-all
    hdrs = make_token_headers(token)
    with TestClient(app) as client:
        r_list = client.get("/api/upgrades", headers=hdrs)
        assert r_list.status_code == 200
        q_items = [u for u in r_list.json()["upgrades"] if u["status"] == "quarantined"]
        assert len(q_items) == 2

        u1, u2 = q_items[0], q_items[1]
        assert client.get(f"/api/upgrades/{u1['id']}/preview/kept", headers=hdrs).status_code == 200
        assert client.get(f"/api/upgrades/{u1['id']}/preview/quarantined", headers=hdrs).status_code == 200

        r_rest = client.post(f"/api/upgrades/{u1['id']}/restore", headers=hdrs)
        assert r_rest.status_code == 200
        assert r_rest.json()["restored_rel_path"] in ("Legacy/old_a.jpg", "Legacy/old_b.jpg")
        assert (settings.photos_dir / r_rest.json()["restored_rel_path"]).is_file()

        # Purge after restore MUST fail with HTTP 400
        assert client.post(f"/api/upgrades/{u1['id']}/purge", headers=hdrs).status_code == 400

        # Batched purge-all cleans remaining quarantined upgrade (u2) without touching u1
        r_pall = client.post("/api/upgrades/purge-all", headers=hdrs)
        assert r_pall.status_code == 200
        assert r_pall.json()["purged"] == 1
        assert db.get_quality_upgrade(u2["id"])["status"] == "purged"
        assert db.get_quality_upgrade(u1["id"])["status"] == "restored"


def test_t4_w4_adversarial_security_penetration_chain(
    tmp_path: Path, mock_dsm_server: MockDSMServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Workload 4: End-to-end adversarial attack chain testing C1, C2, G3, G4, G6, G14, and XSS defenses."""
    secret = "w4" * 32
    settings = make_isolated_settings(
        tmp_path,
        proxy_secret=secret,
        google_client_id="cid-w4",
        google_client_secret="csec-w4",
        synology_url="https://legit-nas.internal:5001",
        synology_username=mock_dsm_server.state.valid_username,
        synology_password=mock_dsm_server.state.valid_password,
    )
    (settings.homes_dir / "victim" / "Photos").mkdir(parents=True)
    (settings.homes_dir / "attacker" / "Photos").mkdir(parents=True)
    (settings.homes_dir / "attacker" / "Photos" / "link_to_victim").symlink_to(
        settings.homes_dir / "victim" / "Photos"
    )

    exchanged: list[str] = []
    monkeypatch.setattr(GoogleDriveClient, "exchange_code", lambda self, c, r: exchanged.append(c))

    with start_live_fastapi_server(settings) as live:
        # Setup victim & attacker profiles as admin
        for user in ("victim", "attacker"):
            r = run_cgi(
                PROXY_CGI_PATH,
                backend_url=live.url,
                method="POST",
                query_string="p=/api/profiles",
                body=json.dumps({"dsm_username": user, "target_space": "personal"}).encode(),
                content_type="application/json",
                proxy_secret=secret,
                dsm_user="admin",
                dsm_admin="1",
                http_headers={"X-Requested-With": "XMLHttpRequest"},
            )
            assert r.status == 200

        # Attack 1 (C1): Direct unauthenticated HTTP & forged X-DSM-User without secret -> 401
        assert httpx.get(f"{live.url}/api/settings").status_code == 401
        assert (
            httpx.get(
                f"{live.url}/api/settings",
                headers={"X-Proxy-Mode": "cgi", "X-DSM-User": "admin", "X-DSM-Admin": "1"},
            ).status_code
            == 401
        )

        # Attack 2 (G6): CSRF-headerless mutating POST through proxy.cgi -> 403
        r_csrf = run_cgi(
            PROXY_CGI_PATH,
            backend_url=live.url,
            method="POST",
            query_string="p=/api/upgrades/purge-all",
            proxy_secret=secret,
            dsm_user="admin",
            dsm_admin="1",
        )
        assert r_csrf.status == 403

        # Attack 3 (C2): Non-admin attacker tries to purge victim's quarantine & pause daemon via proxy.cgi -> 403
        r_cross = run_cgi(
            PROXY_CGI_PATH,
            backend_url=live.url,
            method="POST",
            query_string="p=/api/upgrades/purge-all",
            proxy_secret=secret,
            dsm_user="attacker",
            dsm_admin="0",
            http_headers={"X-Profile-Id": "victim", "X-Requested-With": "XMLHttpRequest"},
        )
        assert r_cross.status == 403

        # Attack 4 (G3): OAuth callback state forgery against victim via oauth_callback.cgi -> fails closed
        r_oauth = run_cgi(
            OAUTH_CALLBACK_CGI_PATH,
            backend_url=live.url,
            query_string="code=attacker_drive_code&state=victim:forged_state",
            proxy_secret=secret,
            dsm_user="victim",
            dsm_admin="0",
        )
        assert len(exchanged) == 0
        assert r_oauth.status in (302, 307, 400, 403)

        # Attack 5 (G4): Redirect synology_url to attacker HTTP listener -> stored password cleared, NEVER exfiltrated
        r_url_swap = run_cgi(
            PROXY_CGI_PATH,
            backend_url=live.url,
            method="PUT",
            query_string="p=/api/settings",
            body=json.dumps({"updates": {"synology_url": mock_dsm_server.url}}).encode(),
            content_type="application/json",
            proxy_secret=secret,
            dsm_user="admin",
            dsm_admin="1",
            http_headers={"X-Profile-Id": "default", "X-Requested-With": "XMLHttpRequest"},
        )
        assert r_url_swap.status == 200
        assert r_url_swap.json()["settings"]["synology_password"] == ""

        r_exfil = run_cgi(
            PROXY_CGI_PATH,
            backend_url=live.url,
            method="POST",
            query_string="p=/api/actions/test-synology",
            proxy_secret=secret,
            dsm_user="admin",
            dsm_admin="1",
            http_headers={"X-Profile-Id": "default", "X-Requested-With": "XMLHttpRequest"},
        )
        assert r_exfil.status == 400
        assert all(
            mock_dsm_server.state.valid_password.encode() not in req.body
            for req in mock_dsm_server.state.requests
        )

        # Attack 6 (G14): Symlink & ../ subfolder escape into victim's Photos -> 400
        for bad_sub in ("../../victim/Photos", "link_to_victim"):
            r_sub = run_cgi(
                PROXY_CGI_PATH,
                backend_url=live.url,
                method="POST",
                query_string="p=/api/profiles",
                body=json.dumps(
                    {
                        "dsm_username": "attacker",
                        "display_name": "Escape",
                        "target_space": "personal",
                        "custom_subfolder": bad_sub,
                    }
                ).encode(),
                content_type="application/json",
                proxy_secret=secret,
                dsm_user="admin",
                dsm_admin="1",
                http_headers={"X-Requested-With": "XMLHttpRequest"},
            )
            assert r_sub.status == 400

    # Attack 7 (Stored XSS): Malicious archive filename rendered through app.js cannot break out of title=""
    js_res = run_app_js_scenario(
        pathname="/webman/3rdparty/gphotos-migrator/proxy.cgi",
        routes={
            "/api/archives": {
                "status": 200,
                "json": {
                    "archives": [
                        {
                            "id": 99,
                            "filename": 'x" onfocus="fetch(\'//evil\')" autofocus="',
                            "source": "inbox",
                            "status": "failed",
                            "imported_files": 0,
                            "duplicate_files": 0,
                            "failed_files": 1,
                            "error_message": 'err" onmouseover="alert(document.cookie)',
                        }
                    ]
                },
            }
        },
        inspect_ids=["archives-tbody"],
    )
    rendered = js_res["elements"]["archives-tbody"]["innerHTML"]
    assert 'onfocus="' not in rendered
    assert 'onmouseover="' not in rendered


def test_t4_w5_spk_build_upgrade_uninstall_and_reinstall_data_survival(tmp_path: Path) -> None:
    """Workload 5: Deterministic SPK double-build, daemon lifecycle, stale PID safety, 4 preupgrades, uninstall survival."""
    bdir = tmp_path / "bundle"
    bdir.mkdir()
    exe = bdir / "gphotos-migrator"
    exe.write_text("#!/bin/sh\ntrap 'exit 0' TERM INT\nwhile :; do sleep 1; done\n", encoding="utf-8")
    exe.chmod(0o755)

    spk_a = build_test_spk(
        tmp_path,
        arch="x86_64",
        standalone=True,
        build_sha="f00dbabe1234",
        build_date="2026-08-01T00:00:00Z",
        output_name="rel_a.spk",
        bundle_dir=bdir,
    )
    time.sleep(1.1)
    spk_b = build_test_spk(
        tmp_path,
        arch="x86_64",
        standalone=True,
        build_sha="f00dbabe1234",
        build_date="2026-08-01T00:00:00Z",
        output_name="rel_b.spk",
        bundle_dir=bdir,
    )
    assert sha256_file(spk_a) == sha256_file(spk_b)

    extracted = extract_spk(spk_a, tmp_path / "installed")
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

    # Stale/reused PID (current pytest process PID is NOT gphotos-migrator!) -> status must exit non-zero!
    pid_file = pkg_var / "gphotos-migrator.pid"
    pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
    assert run_posix_script(sss, ["status"], env=env).returncode != 0, (
        "start-stop-status must verify PID cmdline rather than blindly trusting PID file"
    )

    try:
        assert run_posix_script(sss, ["start"], env=env).returncode == 0
        assert stat.S_IMODE((pkg_var / "proxy.secret").stat().st_mode) == 0o600
        assert run_posix_script(sss, ["status"], env=env).returncode == 0
    finally:
        assert run_posix_script(sss, ["stop"], env=env).returncode == 0
    assert run_posix_script(sss, ["status"], env=env).returncode != 0

    # Populate real user state inside PKG_VAR
    (pkg_var / "config").mkdir(parents=True, exist_ok=True)
    (pkg_var / "work" / "replaced_duplicates").mkdir(parents=True, exist_ok=True)
    (pkg_var / "inbox").mkdir(parents=True, exist_ok=True)
    (pkg_var / "config" / "state.db").write_bytes(b"USER_MIGRATION_DB_V1")
    (pkg_var / "config" / "runtime_settings.json").write_text('{"dedup_mode":"perceptual"}', encoding="utf-8")
    (pkg_var / "work" / "replaced_duplicates" / "precious_original.jpg").write_bytes(b"SOLE_COPY_IMAGE")
    (pkg_var / "inbox" / "unprocessed.zip").write_bytes(b"PENDING_ARCHIVE")

    # Run 4 preupgrade hooks -> at most 3 snapshots kept
    for i in range(4):
        assert run_posix_script(extracted["scripts"] / "preupgrade", env=env).returncode == 0
        if i < 3:
            time.sleep(1.05)
    snaps = [
        p for p in pkg_var.rglob("*") if p.is_dir() and (p / "state.db").is_file() and p != pkg_var / "config"
    ]
    assert 1 <= len(snaps) <= 3

    # Run preuninst with UNINSTALL -> backs up outside PKG_VAR, excludes proxy.secret
    assert (
        run_posix_script(
            extracted["scripts"] / "preuninst",
            env={**env, "SYNOPKG_PKG_STATUS": "UNINSTALL"},
        ).returncode
        == 0
    )

    # Simulate DSM completely deleting PKG_VAR on uninstall
    shutil.rmtree(pkg_var)
    assert not pkg_var.exists()

    # Verify full user data survival outside PKG_VAR and ZERO proxy.secret leakage
    backup_root = photos_dir / ".gphotos-migrator-backup"
    assert backup_root.is_dir()
    preserved_db = list(backup_root.rglob("state.db"))
    preserved_q = list(backup_root.rglob("precious_original.jpg"))
    preserved_inbox = list(backup_root.rglob("unprocessed.zip"))
    assert preserved_db and preserved_db[0].read_bytes() == b"USER_MIGRATION_DB_V1"
    assert preserved_q and preserved_q[0].read_bytes() == b"SOLE_COPY_IMAGE"
    assert preserved_inbox and preserved_inbox[0].read_bytes() == b"PENDING_ARCHIVE"
    assert not list(backup_root.rglob("proxy.secret"))


def test_t4_w6_release_update_check_non_downgrade_checksum_and_watchtower_workflow(
    tmp_path: Path, mock_webhook_server: MockWebhookServer
) -> None:
    """Workload 6: Full release update workflow (non-downgrade, outage error, v1.3.0 checksums, POST-only Watchtower, repo contracts)."""
    token = "w6-token"
    settings = make_isolated_settings(
        tmp_path,
        api_token=token,
        app_build_sha="1234567",
        watchtower_url=f"http://127.0.0.1:{mock_webhook_server.port}",
        watchtower_token="per-install-wt-token",
    )
    app = create_app(base_settings=settings, start_worker=False)
    updater = app.state.ctx.updater

    # 1. Downgrade v1.1.0 -> ignored
    def _v110(req: httpx.Request) -> httpx.Response:
        if str(req.url).endswith("/releases/latest"):
            return httpx.Response(200, json={"tag_name": "v1.1.0", "html_url": "https://gh/v1.1.0", "assets": []})
        return httpx.Response(200, json={"sha": "9999999999999999999999999999999999999999", "commit": {"message": "old", "committer": {"date": "2026-01-01T00:00:00Z"}}})

    with make_github_mock_client(_v110) as c:
        st1 = updater.get_status(settings, force=True, http_client=c)
    assert st1["update_available"] is False

    # 2. GitHub 503 outage -> error surfaced cleanly
    def _outage(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"message": "Service Unavailable"})

    with make_github_mock_client(_outage) as c:
        st2 = updater.get_status(settings, force=True, http_client=c)
    assert st2["error"]

    # 3. Newer v1.3.0 with .sha256 + SHA256SUMS.txt -> update_available=True + checksum surfaced via HTTP /api/updates
    def _v130(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("/releases/latest"):
            return httpx.Response(
                200,
                json={
                    "tag_name": "v1.3.0",
                    "html_url": "https://github.com/0denko/gphotos-to-synology/releases/tag/v1.3.0",
                    "assets": [
                        {
                            "name": "gphotos-migrator-dsm7-x86_64.spk",
                            "browser_download_url": "https://example.invalid/x86_64.spk",
                        },
                        {
                            "name": "gphotos-migrator-dsm7-x86_64.spk.sha256",
                            "browser_download_url": "https://example.invalid/x86_64.spk.sha256",
                        },
                        {
                            "name": "SHA256SUMS.txt",
                            "browser_download_url": "https://example.invalid/SHA256SUMS.txt",
                        },
                    ],
                },
            )
        if url.endswith((".sha256", "SHA256SUMS.txt")):
            return httpx.Response(200, text="a" * 64 + "  gphotos-migrator-dsm7-x86_64.spk\n")
        return httpx.Response(200, json={"sha": "abcdef1234567890abcdef1234567890abcdef12", "commit": {"message": "v1.3.0", "committer": {"date": "2026-12-01T00:00:00Z"}}})

    with make_github_mock_client(_v130) as c:
        st3 = updater.get_status(settings, force=True, http_client=c)
    assert st3["update_available"] is True
    assert any(("sha256" in k.lower() or "checksum" in k.lower()) and v for k, v in st3.items())

    # Serve cached status via HTTP GET /api/updates & trigger Watchtower via POST /api/updates/apply
    hdrs = make_token_headers(token)
    with TestClient(app) as client:
        r_upd = client.get("/api/updates?force=false", headers=hdrs)
        assert r_upd.status_code == 200
        assert r_upd.json()["update_available"] is True

        # Watchtower returns 405 -> POST /api/updates/apply returns 400 and NEVER sends GET
        mock_webhook_server.status_code = 405
        r_405 = client.post("/api/updates/apply", headers=hdrs)
        assert r_405.status_code == 400
        assert all(r.method == "POST" for r in mock_webhook_server.requests)

        # Watchtower returns 200 -> POST /api/updates/apply succeeds
        mock_webhook_server.status_code = 200
        r_ok = client.post("/api/updates/apply", headers=hdrs)
        assert r_ok.status_code == 200

    # 4. Repo-level release readiness checks
    assert gphotos_synology.__version__ == "1.2.0"
    assert (REPO_ROOT / "uv.lock").is_file()
    workflow = (REPO_ROOT / ".github" / "workflows" / "docker-publish.yml").read_text(encoding="utf-8")
    assert "--locked" in workflow
    assert "gh release upload v1.0.0" not in workflow
    assert ".agents" in (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")

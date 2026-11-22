"""Tier 1 — Category-Partition / Feature Coverage E2E Tests (Target v1.2.0 Specification).

Covers isolated happy-path behavior across all 5 major subsystem areas (6 tests per area = 30 tests):
  - Area A: Security, Auth, Profiles & Config
  - Area B: Takeout Extraction, Sidecar Matching, Metadata & Dedup
  - Area C: Pipeline, Quality Upgrades, Albums, Scheduler & Notifier
  - Area D: Sources & Synology Client/Scanner
  - Area E: Packaging, SPK Lifecycle, Updater & Frontend
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import piexif
import pytest
from conftest import (
    APP_JS_PATH,
    GPHOTOS_MIGRATOR_JS_PATH,
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
    make_csrf_headers,
    make_github_mock_client,
    make_isolated_settings,
    make_proxy_headers,
    make_sidecar_payload,
    make_stub_exiftool,
    make_token_headers,
    parse_spk_info,
    run_app_js_scenario,
    run_cgi,
    run_posix_script,
    save_heic,
    save_jpeg,
)
from fastapi.testclient import TestClient
from PIL import Image

import gphotos_synology
from gphotos_synology.config import SECRET_FIELDS, ConfigManager, Settings
from gphotos_synology.database import StateDB
from gphotos_synology.main import create_app
from gphotos_synology.notifications import Notifier
from gphotos_synology.sources.gdrive import DriveArchiveItem, GoogleDriveClient
from gphotos_synology.sources.inbox import LocalInboxScanner
from gphotos_synology.synology.importer import SynologyFolderImporter
from gphotos_synology.synology.photos_api import SynologyPhotosClient
from gphotos_synology.synology.scanner import ExistingLibraryScanner
from gphotos_synology.takeout.dedup import compute_media_fingerprint
from gphotos_synology.takeout.extractor import extract_archive
from gphotos_synology.takeout.matcher import DirectoryJsonIndex, find_sidecar_json
from gphotos_synology.takeout.metadata import PhotoMetadata, apply_metadata, parse_sidecar_json
from gphotos_synology.updater import UpdateService
from gphotos_synology.worker.pipeline import MigrationPipeline
from gphotos_synology.worker.scheduler import BackgroundScheduler

# ===========================================================================
# Area A — Security, Auth, Profiles & Config (6 tests)
# ===========================================================================


def test_t1_a1_public_endpoints_exempt_from_auth(tmp_path: Path) -> None:
    """GET /api/health, /, /static/app.js, and /api/auth/status succeed without credentials."""
    settings = make_isolated_settings(tmp_path, api_token="secret-token-t1a1")
    app = create_app(base_settings=settings, start_worker=False)

    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        root = client.get("/")
        assert root.status_code == 200
        assert "text/html" in root.headers.get("content-type", "")

        js = client.get("/static/app.js")
        assert js.status_code == 200

        auth_status = client.get("/api/auth/status")
        assert auth_status.status_code == 200
        assert isinstance(auth_status.json(), dict)


def test_t1_a2_direct_api_token_and_session_cookie_auth(tmp_path: Path) -> None:
    """Bearer token, X-API-Token, and POST /api/auth/session cookie authenticate API calls."""
    token = "direct-token-t1a2-xyz"
    settings = make_isolated_settings(tmp_path, api_token=token)
    app = create_app(base_settings=settings, start_worker=False)

    with TestClient(app) as client:
        r_bearer = client.get("/api/status", headers=make_token_headers(token, header_style="bearer"))
        assert r_bearer.status_code == 200
        assert "stats" in r_bearer.json()

        r_header = client.get("/api/profiles", headers=make_token_headers(token, header_style="x-api-token"))
        assert r_header.status_code == 200
        assert "profiles" in r_header.json()

        # Login via POST /api/auth/session with CSRF header -> sets HttpOnly session cookie
        r_login = client.post(
            "/api/auth/session",
            json={"token": token},
            headers=make_csrf_headers(),
        )
        assert r_login.status_code == 200
        set_cookie = r_login.headers.get("set-cookie", "")
        assert "gphotos_session=" in set_cookie
        assert "httponly" in set_cookie.lower()

        # Subsequent request authenticated solely via session cookie succeeds
        r_cookie = client.get("/api/status")
        assert r_cookie.status_code == 200


def test_t1_a3_proxy_secret_auth_with_dsm_admin_and_user(tmp_path: Path) -> None:
    """Valid X-Proxy-Secret + X-DSM-User + X-DSM-Admin authenticates and exposes identity."""
    secret = "a" * 64
    settings = make_isolated_settings(tmp_path, proxy_secret=secret)
    app = create_app(base_settings=settings, start_worker=False)

    with TestClient(app) as client:
        resp = client.get(
            "/api/profiles",
            headers=make_proxy_headers(secret=secret, dsm_user="admin", is_admin=True),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_admin"] is True
        assert data["dsm_user"] == "admin"
        assert len(data["profiles"]) >= 1


def test_t1_a4_admin_profile_crud_and_lazy_oauth_inheritance(tmp_path: Path) -> None:
    """Admin creates/updates/deletes profile; default OAuth creds propagate lazily without restart."""
    secret = "b" * 64
    settings = make_isolated_settings(tmp_path, proxy_secret=secret)
    (settings.homes_dir / "alice" / "Photos").mkdir(parents=True, exist_ok=True)
    app = create_app(base_settings=settings, start_worker=False)
    admin_hdrs = make_proxy_headers(secret=secret, dsm_user="admin", is_admin=True)

    with TestClient(app) as client:
        created = client.post(
            "/api/profiles",
            json={"dsm_username": "alice", "display_name": "Alice", "target_space": "personal"},
            headers=admin_hdrs,
        )
        assert created.status_code == 200
        profile_id = created.json()["profile"]["id"]

        # Update default profile's Google OAuth creds AFTER alice was created
        upd = client.put(
            "/api/settings",
            json={
                "updates": {
                    "google_client_id": "lazy-shared-client-id.apps.googleusercontent.com",
                    "google_client_secret": "lazy-shared-secret",
                }
            },
            headers={**admin_hdrs, "X-Profile-Id": "default"},
        )
        assert upd.status_code == 200

        # Alice immediately inherits the default profile's OAuth credentials without restart
        alice_settings = client.get(
            "/api/settings",
            headers={**admin_hdrs, "X-Profile-Id": profile_id},
        )
        assert alice_settings.status_code == 200
        assert (
            alice_settings.json()["settings"]["google_client_id"]
            == "lazy-shared-client-id.apps.googleusercontent.com"
        )
        assert alice_settings.json()["settings"]["google_client_secret"] == "********"

        patched = client.patch(
            f"/api/profiles/{profile_id}",
            json={"display_name": "Alice Renamed", "enabled": True},
            headers=admin_hdrs,
        )
        assert patched.status_code == 200
        assert patched.json()["profile"]["display_name"] == "Alice Renamed"

        deleted = client.delete(f"/api/profiles/{profile_id}", headers=admin_hdrs)
        assert deleted.status_code == 200
        assert deleted.json()["ok"] is True


def test_t1_a5_v120_secure_settings_defaults_and_config_update(tmp_path: Path) -> None:
    """Clean Settings defaults enforce loopback host, HTTPS Synology URL, and TLS verification."""
    clean = Settings(_env_file=None)  # type: ignore[call-arg]
    assert clean.app_host == "127.0.0.1"
    assert clean.synology_url == "https://127.0.0.1:5001"
    assert clean.synology_verify_ssl is True
    assert hasattr(clean, "synology_ca_cert")

    for expected_secret in (
        "google_client_secret",
        "synology_password",
        "smtp_password",
        "watchtower_token",
        "proxy_secret",
        "api_token",
    ):
        assert expected_secret in SECRET_FIELDS

    settings = make_isolated_settings(tmp_path)
    cfg = ConfigManager(settings)
    updated = cfg.update({"dedup_mode": "exact_and_pixels", "synology_password": "top-secret"})
    assert updated.dedup_mode == "exact_and_pixels"
    pub = cfg.to_public_dict()
    assert pub["synology_password"] == "********"


def test_t1_a6_quarantine_dir_override_property(tmp_path: Path) -> None:
    """Settings.quarantine_dir uses quarantine_dir_override when set else work_dir/replaced_duplicates."""
    default_settings = make_isolated_settings(tmp_path / "default_q")
    assert default_settings.quarantine_dir == default_settings.work_dir / "replaced_duplicates"

    custom_q = tmp_path / "external_quarantine"
    override_settings = make_isolated_settings(
        tmp_path / "override_q",
        quarantine_dir_override=custom_q,
    )
    assert override_settings.quarantine_dir == custom_q


# ===========================================================================
# Area B — Takeout Extraction, Sidecar Matching, Metadata & Dedup (6 tests)
# ===========================================================================


def test_t1_b1_extract_zip_and_targz_and_cleanup_stale_tmp(tmp_path: Path) -> None:
    """extract_archive unpacks ZIP & TAR.GZ archives; cleanup_stale_temp_files removes stale temps."""
    from gphotos_synology.takeout import extractor as extractor_mod

    zpath = build_zip_archive(
        tmp_path / "takeout.zip",
        {"Takeout/Google Photos/2024/IMG_0001.jpg": image_to_jpeg_bytes(seed=1)},
    )
    z_out = extract_archive(zpath, tmp_path / "out_zip")
    assert (z_out / "Takeout/Google Photos/2024/IMG_0001.jpg").is_file()

    tpath = build_tar_gz_archive(
        tmp_path / "takeout.tar.gz",
        {"Takeout/Google Photos/2024/IMG_0002.jpg": image_to_jpeg_bytes(seed=2)},
    )
    t_out = extract_archive(tpath, tmp_path / "out_tar")
    assert (t_out / "Takeout/Google Photos/2024/IMG_0002.jpg").is_file()

    cleanup_fn = getattr(extractor_mod, "cleanup_stale_temp_files", None)
    assert callable(cleanup_fn), "v1.2.0 requires cleanup_stale_temp_files in takeout.extractor"

    photo_dir = tmp_path / "photos_clean"
    photo_dir.mkdir()
    keep_file = photo_dir / "IMG_REAL.jpg"
    keep_file.write_bytes(b"real-photo")
    stale_tmp = photo_dir / ".IMG_PARTIAL.jpg.tmp.99999"
    stale_tmp.write_bytes(b"partial")

    removed = cleanup_fn(photo_dir, max_age_seconds=0.0)
    assert removed >= 1
    assert not stale_tmp.exists()
    assert keep_file.is_file()


def test_t1_b2_strict_sidecar_matching_happy_paths(tmp_path: Path) -> None:
    """find_sidecar_json matches standard, supplemental, numbered (1), unique title, and -edited."""
    album_dir = tmp_path / "Photos from 2024"
    album_dir.mkdir()

    # 1. Exact .json
    p1 = save_jpeg(album_dir / "IMG_001.jpg", seed=1)
    j1 = album_dir / "IMG_001.jpg.json"
    j1.write_text(json.dumps(make_sidecar_payload("IMG_001.jpg")), encoding="utf-8")

    # 2. Supplemental metadata
    p2 = save_jpeg(album_dir / "IMG_002.jpg", seed=2)
    j2 = album_dir / "IMG_002.jpg.supplemental-metadata.json"
    j2.write_text(json.dumps(make_sidecar_payload("IMG_002.jpg")), encoding="utf-8")

    # 3. Numbered duplicate with numbered sidecar
    p3 = save_jpeg(album_dir / "IMG_003(1).jpg", seed=3)
    j3 = album_dir / "IMG_003.jpg(1).json"
    j3.write_text(json.dumps(make_sidecar_payload("IMG_003(1).jpg")), encoding="utf-8")

    # 4. Unique title match
    p4 = save_jpeg(album_dir / "UNIQUE_TITLE_PHOTO.jpg", seed=4)
    j4 = album_dir / "random_sidecar_name.json"
    j4.write_text(json.dumps(make_sidecar_payload("UNIQUE_TITLE_PHOTO.jpg")), encoding="utf-8")

    # 5. -edited variant inheriting base sidecar
    p5 = save_jpeg(album_dir / "IMG_005-edited.jpg", seed=5)
    j5 = album_dir / "IMG_005.jpg.json"
    j5.write_text(json.dumps(make_sidecar_payload("IMG_005.jpg")), encoding="utf-8")

    index = DirectoryJsonIndex.build(album_dir)
    assert find_sidecar_json(p1, index) == j1
    assert find_sidecar_json(p2, index) == j2
    assert find_sidecar_json(p3, index) == j3
    assert find_sidecar_json(p4, index) == j4
    assert find_sidecar_json(p5, index) == j5


def test_t1_b3_apply_metadata_piexif_offset_ascii_unicode_and_float_ts(tmp_path: Path) -> None:
    """parse_sidecar_json parses float timestamps; piexif writes OffsetTimeOriginal, ASCII desc, UTF-8 UserComment."""
    json_path = tmp_path / "photo.jpg.json"
    json_path.write_text(
        json.dumps(
            make_sidecar_payload(
                "photo.jpg",
                timestamp="1720000000.5",
                description="Café 東京 sunset",
                lat=37.7749,
                lon=-122.4194,
                alt=15.0,
            )
        ),
        encoding="utf-8",
    )
    meta = parse_sidecar_json(json_path)
    assert meta.taken_at is not None
    assert int(meta.taken_at.timestamp()) == 1720000000
    assert meta.has_gps is True

    jpg = save_jpeg(tmp_path / "photo.jpg", seed=10)
    status = apply_metadata(jpg, meta, write_exif=True, exiftool_path="nonexistent-exiftool")
    assert status == "repaired"

    loaded = piexif.load(str(jpg))
    assert loaded["Exif"].get(piexif.ExifIFD.DateTimeOriginal) == b"2024:07:03 09:46:40"
    assert loaded["Exif"].get(piexif.ExifIFD.OffsetTimeOriginal) == b"+00:00"

    # ImageDescription must be strictly ASCII if written
    img_desc = loaded["0th"].get(piexif.ImageIFD.ImageDescription)
    if img_desc is not None:
        img_desc.decode("ascii")

    # Full Unicode description preserved in ExifIFD.UserComment
    raw_uc = loaded["Exif"].get(piexif.ExifIFD.UserComment)
    assert raw_uc is not None
    decoded_uc = piexif.helper.UserComment.load(raw_uc)
    assert "Café 東京 sunset" in decoded_uc

    # mtime synchronized
    assert abs(jpg.stat().st_mtime - 1720000000) < 2.0


def test_t1_b4_apply_metadata_with_stub_exiftool_happy_path(tmp_path: Path) -> None:
    """When exiftool is available via exiftool_path, apply_metadata invokes it with OffsetTimeOriginal."""
    stub_bin, log_file = make_stub_exiftool(tmp_path)
    jpg = save_jpeg(tmp_path / "stub_test.jpg", seed=11)
    meta = PhotoMetadata(
        title="stub_test.jpg",
        description="Stub ExifTool test",
        taken_at=datetime(2024, 7, 3, 10, 0, 0, tzinfo=UTC),
        latitude=48.8566,
        longitude=2.3522,
    )
    status = apply_metadata(jpg, meta, write_exif=True, exiftool_path=str(stub_bin))
    assert status == "repaired"
    assert log_file.is_file()
    invoked_args = log_file.read_text(encoding="utf-8")
    assert "DateTimeOriginal=2024:07:03 10:00:00" in invoked_args
    assert "OffsetTimeOriginal=+00:00" in invoked_args


def test_t1_b5_heic_visual_fingerprinting_via_pillow_heif(tmp_path: Path) -> None:
    """Real HEIC file produces visual_status='ok' with non-empty pixel_sha256, 32-char dhash, and dims."""
    heic_path = save_heic(tmp_path / "iphone_photo.heic", width=320, height=240, seed=12)
    fp = compute_media_fingerprint(heic_path, compute_visual=True)
    assert fp.sha256
    assert getattr(fp, "visual_status", "") == "ok"
    assert len(fp.pixel_sha256) == 64
    assert len(fp.dhash) == 32
    assert fp.width == 320
    assert fp.height == 240
    assert fp.quality_score > 0


def test_t1_b6_statedb_dhash_buckets_and_hash_lookup_ordering(tmp_path: Path) -> None:
    """StateDB populates media_dhash_buckets and prioritizes exact original_sha256 over final_sha256."""
    db_path = tmp_path / "state.db"
    db = StateDB(db_path)

    # Row A has final_sha256 == "hash_target"; Row B has original_sha256 == "hash_target"
    dhash_32 = "0123456789abcdef0123456789abcdef"
    row_a_id = db.record_media_item(
        original_sha256="hash_a_orig",
        final_sha256="hash_target",
        original_filename="A.jpg",
        dest_rel_path="2024/07/A.jpg",
        file_size=1000,
        taken_at="2024-07-03T10:00:00+00:00",
        latitude=None,
        longitude=None,
        exif_status="repaired",
        source_archive_id=None,
        pixel_sha256="px_a",
        dhash=dhash_32,
        width=320,
        height=240,
    )
    row_b_id = db.record_media_item(
        original_sha256="hash_target",
        final_sha256="hash_b_final",
        original_filename="B.jpg",
        dest_rel_path="2024/07/B.jpg",
        file_size=1000,
        taken_at="2024-07-03T10:05:00+00:00",
        latitude=None,
        longitude=None,
        exif_status="repaired",
        source_archive_id=None,
        pixel_sha256="px_b",
        dhash="fedcba9876543210fedcba9876543210",
        width=320,
        height=240,
    )

    # Exact original_sha256 must win over final_sha256 match
    found_orig = db.get_media_by_original_hash("hash_target")
    assert found_orig is not None
    assert found_orig["id"] == row_b_id

    dup_row, reason = db.find_duplicate_media(sha256="hash_target")
    assert reason == "sha256"
    assert dup_row is not None
    assert dup_row["id"] == row_b_id

    # Verify media_dhash_buckets table exists and has 8 buckets for row A
    with sqlite3.connect(db_path) as conn:
        buckets = conn.execute(
            "SELECT bucket_idx, bucket_val FROM media_dhash_buckets WHERE media_id = ? ORDER BY bucket_idx",
            (row_a_id,),
        ).fetchall()
    assert len(buckets) == 8
    assert buckets[0] == (0, "0123")
    assert buckets[7] == (7, "cdef")

    # Bucketed perceptual lookup finds row A with 1-bit flip (effective_max=4 < 8)
    flipped_dhash = "0123456789abcdef0123456789abcdee"
    matched, match_reason = db.find_duplicate_media(
        sha256="different_sha",
        pixel_sha256="different_px",
        dhash=flipped_dhash,
        width=320,
        height=240,
        taken_at="2024-07-03T10:00:00+00:00",
        filename="A_copy.jpg",
        dedup_mode="perceptual",
        perceptual_max_distance=4,
    )
    assert match_reason == "perceptual_dhash"
    assert matched is not None
    assert matched["id"] == row_a_id


# ===========================================================================
# Area C — Pipeline, Quality Upgrades, Albums, Scheduler & Notifier (6 tests)
# ===========================================================================


def test_t1_c1_pipeline_run_cycle_full_import_and_finalize(tmp_path: Path) -> None:
    """MigrationPipeline.run_cycle imports a Takeout ZIP with EXIF repair and finalizes inbox."""
    settings = make_isolated_settings(
        tmp_path,
        inbox_post_action="move_to_processed",
        write_exif_metadata=True,
    )
    cfg = ConfigManager(settings)
    db = StateDB(settings.db_path)
    pipeline = MigrationPipeline(cfg, db)

    build_zip_archive(
        settings.inbox_dir / "takeout-20240703T100000Z-001.zip",
        {
            "Takeout/Google Photos/Summer Trip/IMG_1001.jpg": image_to_jpeg_bytes(seed=21),
            "Takeout/Google Photos/Summer Trip/IMG_1001.jpg.json": json.dumps(
                make_sidecar_payload("IMG_1001.jpg", timestamp=1720000000, description="Beach")
            ),
        },
    )

    summary = pipeline.run_cycle()
    assert summary["inbox_processed"] == 1
    assert summary["errors"] == 0

    imported_files = list(settings.photos_dir.rglob("IMG_1001.jpg"))
    assert len(imported_files) == 1
    # Archive moved to processed/
    assert not (settings.inbox_dir / "takeout-20240703T100000Z-001.zip").exists()
    assert (settings.inbox_dir / "processed" / "takeout-20240703T100000Z-001.zip").is_file()

    archives = db.list_archives()
    assert len(archives) == 1
    assert archives[0]["status"] == "completed"
    assert archives[0]["imported_files"] == 1
    assert archives[0]["failed_files"] == 0


def test_t1_c2_quality_upgrade_swap_quarantine_and_restore_to_old_rel_path(tmp_path: Path) -> None:
    """Higher-res Takeout upgrades NAS photo, records old_rel_path, and restore puts it back."""
    settings = make_isolated_settings(
        tmp_path,
        dedup_mode="perceptual",
        duplicate_quality_policy="auto_upgrade_quarantine",
        write_exif_metadata=True,
    )
    cfg = ConfigManager(settings)
    db = StateDB(settings.db_path)
    pipeline = MigrationPipeline(cfg, db)

    # Pre-existing low-res photo in custom NAS subfolder
    old_rel = "LegacyAlbum/2023/vacation_old.jpg"
    save_jpeg(settings.photos_dir / old_rel, width=320, height=240, seed=30, quality=60)
    pipeline.index_existing_library()

    # Incoming high-res Takeout copy of same scene
    build_zip_archive(
        settings.inbox_dir / "takeout-upgrade-001.zip",
        {
            "Takeout/Google Photos/Photos from 2024/vacation_hires.jpg": image_to_jpeg_bytes(
                width=960, height=720, seed=30, quality=95
            ),
            "Takeout/Google Photos/Photos from 2024/vacation_hires.jpg.json": json.dumps(
                make_sidecar_payload("vacation_hires.jpg", timestamp=1720000000)
            ),
        },
    )

    pipeline.run_cycle()
    upgrades = db.list_quality_upgrades()
    assert len(upgrades) == 1
    upg = upgrades[0]
    assert upg["status"] == "quarantined"
    assert upg.get("old_rel_path") == old_rel
    assert (settings.quarantine_dir / upg["quarantine_filename"]).is_file()
    # Same-format upgrade happens in place: the library path is preserved and
    # now holds the higher-resolution content; the original sits in quarantine.
    upgraded_path = settings.photos_dir / old_rel
    assert upgraded_path.is_file()
    with Image.open(upgraded_path) as upgraded_img:
        assert upgraded_img.size == (960, 720)

    # Restore puts original back at old_rel_path
    res = pipeline.restore_quality_upgrade(upg["id"])
    assert res["ok"] is True
    assert res["restored_rel_path"] == old_rel
    assert (settings.photos_dir / old_rel).is_file()
    assert db.get_quality_upgrade(upg["id"])["status"] == "restored"


def test_t1_c3_album_sync_and_pending_index_without_burning_retries(
    tmp_path: Path, mock_dsm_server: MockDSMServer
) -> None:
    """Pending Synology indexing keeps retry_count == 0, then syncs cleanly once indexed."""
    settings = make_isolated_settings(
        tmp_path,
        synology_api_enabled=True,
        synology_url=mock_dsm_server.url,
        synology_username=mock_dsm_server.state.valid_username,
        synology_password=mock_dsm_server.state.valid_password,
        synology_verify_ssl=False,
    )
    cfg = ConfigManager(settings)
    db = StateDB(settings.db_path)
    pipeline = MigrationPipeline(cfg, db)

    build_zip_archive(
        settings.inbox_dir / "takeout-album-001.zip",
        {
            "Takeout/Google Photos/Paris 2024/IMG_PARIS.jpg": image_to_jpeg_bytes(seed=41),
            "Takeout/Google Photos/Paris 2024/IMG_PARIS.jpg.json": json.dumps(
                make_sidecar_payload("IMG_PARIS.jpg", timestamp=1720000000)
            ),
        },
    )
    pipeline.run_cycle()

    # Before DSM indexes the photo, run sync_pending_albums 3 times -> retry_count stays 0!
    for _ in range(3):
        synced = pipeline.sync_pending_albums()
        assert synced == 0

    pending = db.get_pending_album_memberships()
    assert len(pending) == 1
    assert pending[0]["retry_count"] == 0

    # Now mock DSM finishes indexing the imported photo
    rel_path = pending[0]["dest_rel_path"]
    mock_dsm_server.state.add_indexed_item(rel_path)

    synced_now = pipeline.sync_pending_albums()
    assert synced_now == 1
    assert db.get_pending_album_memberships() == []
    assert "Paris 2024" in mock_dsm_server.state.albums


def test_t1_c4_scheduler_pause_clears_next_run_and_manual_run_sets_last_run_at(
    tmp_path: Path,
) -> None:
    """BackgroundScheduler pause sets next_run_at=None; manual trigger_now sets last_run_at."""
    settings = make_isolated_settings(tmp_path)
    cfg = ConfigManager(settings)
    db = StateDB(settings.db_path)
    pipeline = MigrationPipeline(cfg, db)
    scheduler = BackgroundScheduler(cfg, db, pipeline)

    scheduler.start()
    try:
        scheduler.trigger_now(index_existing_only=True)
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            prog = pipeline.get_progress()
            if prog.get("last_run_at"):
                break
            time.sleep(0.05)
        assert pipeline.get_progress().get("last_run_at"), "manual index run must set last_run_at"

        scheduler.pause()
        assert scheduler.is_paused is True
        prog_paused = pipeline.get_progress()
        assert prog_paused["state"] == "paused"
        assert prog_paused["next_run_at"] is None
    finally:
        scheduler.stop()


def test_t1_c5_notifier_sync_send_and_nonblocking_send_async(
    tmp_path: Path, mock_webhook_server: MockWebhookServer
) -> None:
    """Notifier.send delivers synchronously; Notifier.send_async returns immediately and delivers."""
    settings = make_isolated_settings(
        tmp_path,
        webhook_url=mock_webhook_server.url,
        webhook_type="generic",
    )
    notifier = Notifier(settings)

    ok = notifier.send("Sync Title", "Sync Message", level="info")
    assert ok is True
    assert len(mock_webhook_server.requests) == 1
    assert json.loads(mock_webhook_server.requests[0].body)["title"] == "Sync Title"

    assert hasattr(notifier, "send_async"), "v1.2.0 requires Notifier.send_async()"
    mock_webhook_server.delay_seconds = 0.8
    t0 = time.monotonic()
    notifier.send_async("Async Title", "Async Message", level="warning")
    elapsed = time.monotonic() - t0
    assert elapsed < 0.25, f"send_async blocked caller for {elapsed:.2f}s"

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and len(mock_webhook_server.requests) < 2:
        time.sleep(0.05)
    assert len(mock_webhook_server.requests) == 2
    assert json.loads(mock_webhook_server.requests[1].body)["title"] == "Async Title"


def test_t1_c6_purge_all_quarantined_and_op_lock_presence(tmp_path: Path) -> None:
    """MigrationPipeline exposes _op_lock and purge_all_quarantined removes quarantined copies."""
    settings = make_isolated_settings(
        tmp_path,
        dedup_mode="perceptual",
        duplicate_quality_policy="auto_upgrade_quarantine",
        write_exif_metadata=False,
    )
    cfg = ConfigManager(settings)
    db = StateDB(settings.db_path)
    pipeline = MigrationPipeline(cfg, db)
    assert hasattr(pipeline, "_op_lock"), "PROJECT.md requires MigrationPipeline._op_lock"

    save_jpeg(settings.photos_dir / "2024/07/sample.jpg", width=240, height=180, seed=55, quality=55)
    pipeline.index_existing_library()
    build_zip_archive(
        settings.inbox_dir / "takeout-purge-001.zip",
        {
            "Takeout/Google Photos/Photos from 2024/sample.jpg": image_to_jpeg_bytes(
                width=800, height=600, seed=55, quality=95
            )
        },
    )
    pipeline.run_cycle()
    upgrades = db.list_quality_upgrades()
    assert len(upgrades) == 1
    q_file = settings.quarantine_dir / upgrades[0]["quarantine_filename"]
    assert q_file.is_file()

    purged = pipeline.purge_all_quarantined()
    assert purged == 1
    assert not q_file.exists()
    assert db.get_quality_upgrade(upgrades[0]["id"])["status"] == "purged"


# ===========================================================================
# Area D — Sources & Synology Client/Scanner (6 tests)
# ===========================================================================


def test_t1_d1_inbox_scanner_discovery_and_finalize_actions(tmp_path: Path) -> None:
    """LocalInboxScanner discovers archives and executes keep, move_to_processed, and delete."""
    settings = make_isolated_settings(tmp_path, inbox_post_action="move_to_processed")
    scanner = LocalInboxScanner(settings)

    a1 = build_zip_archive(settings.inbox_dir / "takeout-001.zip", {"a.txt": b"1"})
    items = scanner.scan_ready_archives()
    assert [i.name for i in items] == ["takeout-001.zip"]

    scanner.finalize_archive(a1)
    assert not a1.exists()
    assert (settings.inbox_dir / "processed" / "takeout-001.zip").is_file()

    settings_del = make_isolated_settings(tmp_path / "del_case", inbox_post_action="delete")
    a2 = build_zip_archive(settings_del.inbox_dir / "takeout-002.zip", {"b.txt": b"2"})
    LocalInboxScanner(settings_del).finalize_archive(a2)
    assert not a2.exists()

    settings_keep = make_isolated_settings(tmp_path / "keep_case", inbox_post_action="keep")
    a3 = build_zip_archive(settings_keep.inbox_dir / "takeout-003.zip", {"c.txt": b"3"})
    LocalInboxScanner(settings_keep).finalize_archive(a3)
    assert a3.is_file()


def test_t1_d2_existing_library_scanner_read_only_and_ignored_dirs(tmp_path: Path) -> None:
    """ExistingLibraryScanner indexes valid files read-only and skips @eaDir/#recycle/dot-dirs."""
    settings = make_isolated_settings(tmp_path)
    db = StateDB(settings.db_path)

    valid_img = save_jpeg(settings.photos_dir / "2024/07/real.jpg", seed=61)
    before_bytes = valid_img.read_bytes()
    before_mtime_ns = valid_img.stat().st_mtime_ns

    # Ignored directories & symlinked directory
    save_jpeg(settings.photos_dir / "2024/07/@eaDir/thumb.jpg", seed=62)
    save_jpeg(settings.photos_dir / "#recycle/deleted.jpg", seed=63)
    save_jpeg(settings.photos_dir / "#snapshot/snap.jpg", seed=64)
    save_jpeg(settings.photos_dir / ".hidden/secret.jpg", seed=65)
    ext_dir = tmp_path / "outside_photos"
    save_jpeg(ext_dir / "external.jpg", seed=66)
    (settings.photos_dir / "linked_dir").symlink_to(ext_dir)

    res = ExistingLibraryScanner(settings, db).scan()
    assert res.newly_indexed == 1
    assert res.errors == 0
    assert valid_img.read_bytes() == before_bytes
    assert valid_img.stat().st_mtime_ns == before_mtime_ns
    assert set(db.get_indexed_files_by_rel_path().keys()) == {"2024/07/real.jpg"}


def test_t1_d3_synology_photos_client_login_post_sid_album_and_resolve(
    tmp_path: Path, mock_dsm_server: MockDSMServer
) -> None:
    """SynologyPhotosClient authenticates, sends _sid only in POST body, manages albums & resolves items."""
    item_id = mock_dsm_server.state.add_indexed_item("2024/07/IMG_7001.jpg")
    settings = make_isolated_settings(
        tmp_path,
        synology_url=mock_dsm_server.url,
        synology_username=mock_dsm_server.state.valid_username,
        synology_password=mock_dsm_server.state.valid_password,
        synology_verify_ssl=False,
    )

    with SynologyPhotosClient(settings) as client:
        aid = client.get_or_create_album("Road Trip")
        resolved = client.resolve_item_id("2024/07/IMG_7001.jpg")
        assert resolved == item_id
        client.add_items_to_album(aid, [item_id])

    assert item_id in mock_dsm_server.state.album_items[aid]
    # Security contract: _sid MUST NEVER travel in URL query string on any request
    for req in mock_dsm_server.state.requests:
        assert "_sid" not in req.query, f"_sid leaked in URL query: {req.method} {req.raw_url}"
        if "_sid" in req.form:
            assert req.method == "POST"


def test_t1_d4_gdrive_connection_status_pagination_and_verified_md5_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GoogleDriveClient supports connection_status(), paginated folder search, and MD5-verified download."""
    settings = make_isolated_settings(
        tmp_path,
        google_client_id="cid",
        google_client_secret="csec",
    )
    settings.token_path.write_text(
        json.dumps(
            {
                "token": "ya29.valid-access-token",
                "refresh_token": "1//refresh",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": "cid",
                "client_secret": "csec",
                "scopes": ["https://www.googleapis.com/auth/drive.readonly"],
                "expiry": "2099-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    os.chmod(settings.token_path, 0o600)

    client = GoogleDriveClient(settings)
    assert hasattr(client, "connection_status"), "v1.2.0 requires GoogleDriveClient.connection_status()"
    status = client.connection_status()
    assert status["connected"] is True
    assert status["error"] == ""

    # Multi-page find_takeout_folder_ids pagination via public service parameter
    class _FakeExec:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload

        def execute(self) -> dict[str, Any]:
            return self._payload

    class _FakeFiles:
        def list(self, **kwargs: Any) -> _FakeExec:
            pt = kwargs.get("pageToken")
            if not pt:
                return _FakeExec({"files": [{"id": "folder_1"}], "nextPageToken": "page2"})
            return _FakeExec({"files": [{"id": "folder_2"}]})

    class _FakeService:
        def files(self) -> _FakeFiles:
            return _FakeFiles()

    folder_ids = client.find_takeout_folder_ids(_FakeService())
    assert folder_ids == ["folder_1", "folder_2"]

    # Verified MD5 download via library boundary mock
    archive_bytes = b"PK\x03\x04valid-zip-content-for-md5-check"
    expected_md5 = hashlib.md5(archive_bytes).hexdigest()

    class _FakeDownloader:
        def __init__(self, fh: Any, request: Any, chunksize: int = 0) -> None:
            self._fh = fh
            self._done = False

        def next_chunk(self, num_retries: int = 3) -> tuple[Any, bool]:
            self._fh.write(archive_bytes)
            self._done = True
            return None, True

    class _DownloadFiles:
        def get_media(self, fileId: str) -> object:
            return object()

    class _DownloadService:
        def files(self) -> _DownloadFiles:
            return _DownloadFiles()

    import gphotos_synology.sources.gdrive as gdrive_mod

    monkeypatch.setattr(gdrive_mod, "build", lambda *a, **kw: _DownloadService())
    monkeypatch.setattr(gdrive_mod, "MediaIoBaseDownload", _FakeDownloader)

    dest = tmp_path / "downloads" / "takeout-001.zip"
    item = DriveArchiveItem(
        file_id="f1",
        name="takeout-001.zip",
        size_bytes=len(archive_bytes),
        md5_checksum=expected_md5,
        created_time="2024-07-03T10:00:00Z",
        batch_key="20240703",
    )
    out = client.download_archive(item, dest, max_retries=1)
    assert out == dest
    assert dest.read_bytes() == archive_bytes


def test_t1_d5_synology_folder_importer_sanitization_and_collision_safety(tmp_path: Path) -> None:
    """SynologyFolderImporter sanitizes unsafe characters and never overwrites colliding filenames."""
    settings = make_isolated_settings(tmp_path, folder_template="album")
    importer = SynologyFolderImporter(settings)
    meta = PhotoMetadata(
        title="photo.jpg",
        taken_at=datetime(2024, 7, 3, 10, 0, 0, tzinfo=UTC),
        album_name='Summer: "2024" <Trips>?',
    )

    src1 = save_jpeg(tmp_path / "stage1" / "IMG_1.jpg", seed=71)
    dst1, rel1 = importer.import_media(src1, meta)
    assert dst1.is_file()
    assert not any(c in rel1 for c in ':<>?"*')

    src2 = save_jpeg(tmp_path / "stage2" / "IMG_1.jpg", seed=72)
    dst2, rel2 = importer.import_media(src2, meta)
    assert dst2.is_file()
    assert dst1.is_file()
    assert dst2 != dst1
    assert dst2.name == "IMG_1_2.jpg"
    assert rel1 != rel2


def test_t1_d6_statedb_delete_state_and_periodic_log_prune(tmp_path: Path) -> None:
    """StateDB.delete_state deletes stored keys; log_event persists and serves recent logs."""
    db = StateDB(tmp_path / "state.db")
    db.set_state("oauth_state:tok123", '{"profile_id":"default"}')
    assert db.get_state("oauth_state:tok123") is not None

    assert hasattr(db, "delete_state"), "PROJECT.md requires StateDB.delete_state(key)"
    db.delete_state("oauth_state:tok123")
    assert db.get_state("oauth_state:tok123") is None

    db.log_event("INFO", "e2e", "Test log line 1")
    logs = db.get_recent_logs(limit=10)
    assert any(entry["message"] == "Test log line 1" for entry in logs)


# ===========================================================================
# Area E — Packaging, SPK Lifecycle, Updater & Frontend (6 tests)
# ===========================================================================


def test_t1_e1_build_spk_metadata_and_archive_structure(tmp_path: Path) -> None:
    """build_spk.py produces a valid DSM 7 .spk with unified v1.2.0-0001 metadata and privilege."""
    spk = build_test_spk(tmp_path, arch="x86_64", standalone=True, output_name="tier1.spk")
    extracted = extract_spk(spk, tmp_path / "unpacked")

    info = parse_spk_info(extracted["info"])
    assert info["package"] == "gphotos-migrator"
    assert info["version"] == "1.2.0-0005"
    assert info["maintainer"] == "Oleksandr Danylchenko"
    assert info["distributor"] == "Oleksandr Danylchenko"
    assert info["os_min_ver"] == "7.0-40000"

    priv = json.loads((extracted["conf"] / "privilege").read_text(encoding="utf-8"))
    assert priv["defaults"]["run-as"] == "package"
    assert priv["username"] == "gphotos-migrator"

    assert (extracted["target"] / "ui" / "proxy.cgi").is_file()
    assert (extracted["target"] / "ui" / "oauth_callback.cgi").is_file()
    assert (extracted["target"] / "ui" / "texts" / "enu" / "strings").is_file()
    assert (extracted["scripts"] / "start-stop-status").is_file()
    assert (extracted["scripts"] / "preupgrade").is_file()
    assert (extracted["scripts"] / "preuninst").is_file()


def test_t1_e2_spk_start_stop_status_creates_0600_proxy_secret(tmp_path: Path) -> None:
    """start-stop-status generates 0600 64-hex proxy.secret, starts daemon, reports status, stops."""
    spk = build_test_spk(tmp_path, arch="x86_64", standalone=True, output_name="lifecycle.spk")
    extracted = extract_spk(spk, tmp_path / "unpacked")

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
        r_start = run_posix_script(sss, ["start"], env=env)
        assert r_start.returncode == 0, f"start failed: {r_start.stdout} {r_start.stderr}"

        secret_file = pkg_var / "proxy.secret"
        assert secret_file.is_file(), "start-stop-status must create ${PKG_VAR}/proxy.secret"
        secret_val = secret_file.read_text(encoding="utf-8").strip()
        assert len(secret_val) == 64
        int(secret_val, 16)
        assert stat.S_IMODE(secret_file.stat().st_mode) == 0o600

        r_status = run_posix_script(sss, ["status"], env=env)
        assert r_status.returncode == 0

        # Wait briefly for stub daemon to dump its env
        env_dump = pkg_var / "config" / "env_dump.txt"
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not env_dump.is_file():
            time.sleep(0.05)
        assert env_dump.is_file()
        assert f"PROXY_SECRET_FILE={secret_file}" in env_dump.read_text(encoding="utf-8")
    finally:
        run_posix_script(sss, ["stop"], env=env)

    r_stopped = run_posix_script(sss, ["status"], env=env)
    assert r_stopped.returncode != 0


def test_t1_e3_updater_detects_newer_semver_with_sha256_checksum(tmp_path: Path) -> None:
    """UpdateService.get_status flags newer semver v1.3.0 and surfaces .sha256 checksum info."""
    settings = make_isolated_settings(tmp_path, app_build_sha="1111111")

    def _handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/commits/main"):
            return httpx.Response(
                200,
                json={
                    "sha": "2222222222222222222222222222222222222222",
                    "commit": {"message": "Release v1.3.0", "committer": {"date": "2026-12-01T00:00:00Z"}},
                },
            )
        if url.endswith("/releases/latest"):
            return httpx.Response(
                200,
                json={
                    "tag_name": "v1.3.0",
                    "name": "v1.3.0",
                    "html_url": "https://github.com/0denko/gphotos-to-synology/releases/tag/v1.3.0",
                    "assets": [
                        {
                            "name": "gphotos-migrator-dsm7-x86_64.spk",
                            "browser_download_url": "https://example.invalid/gphotos-migrator-dsm7-x86_64.spk",
                        },
                        {
                            "name": "gphotos-migrator-dsm7-x86_64.spk.sha256",
                            "browser_download_url": "https://example.invalid/gphotos-migrator-dsm7-x86_64.spk.sha256",
                        },
                        {
                            "name": "gphotos-migrator-dsm7-arm64.spk",
                            "browser_download_url": "https://example.invalid/gphotos-migrator-dsm7-arm64.spk",
                        },
                        {
                            "name": "gphotos-migrator-dsm7-arm64.spk.sha256",
                            "browser_download_url": "https://example.invalid/gphotos-migrator-dsm7-arm64.spk.sha256",
                        },
                    ],
                },
            )
        if url.endswith(".sha256"):
            return httpx.Response(200, text="deadbeef" * 8 + "  gphotos-migrator-dsm7-x86_64.spk\n")
        return httpx.Response(404)

    with make_github_mock_client(_handler) as client:
        status = UpdateService().get_status(settings, force=True, http_client=client)

    assert status["update_available"] is True
    assert "1.3.0" in str(status["latest_version"])
    assert status["error"] == ""
    # Surfaces checksum URL or digest field for G7
    checksum_fields = [
        v for k, v in status.items() if ("sha256" in k.lower() or "checksum" in k.lower()) and v
    ]
    assert checksum_fields, f"Expected .sha256 checksum metadata in UpdateStatus, got keys={list(status)}"


def test_t1_e4_proxy_and_oauth_cgi_forward_authenticated_requests(
    mock_backend_server: MockBackendServer,
) -> None:
    """proxy.cgi and oauth_callback.cgi inject X-Proxy-Secret, X-DSM-User, X-DSM-Admin, X-Requested-With."""
    secret = "c" * 64
    res_proxy = run_cgi(
        PROXY_CGI_PATH,
        backend_url=mock_backend_server.url,
        method="POST",
        query_string="p=/api/actions/scan",
        body=b"{}",
        content_type="application/json",
        proxy_secret=secret,
        dsm_user="alice",
        dsm_admin="0",
        http_headers={"X-Profile-Id": "alice", "X-Requested-With": "XMLHttpRequest"},
    )
    assert res_proxy.status == 200
    assert len(mock_backend_server.requests) == 1
    req = mock_backend_server.requests[0]
    assert req.path == "/api/actions/scan"
    assert req.headers.get("x-proxy-secret") == secret
    assert req.headers.get("x-proxy-mode") == "cgi"
    assert req.headers.get("x-dsm-user") == "alice"
    assert req.headers.get("x-dsm-admin") == "0"
    assert req.headers.get("x-requested-with") == "XMLHttpRequest"

    res_oauth = run_cgi(
        OAUTH_CALLBACK_CGI_PATH,
        backend_url=mock_backend_server.url,
        method="GET",
        query_string="code=authcode123&state=statexyz",
        proxy_secret=secret,
        dsm_user="alice",
        dsm_admin="0",
    )
    assert res_oauth.status == 200
    assert len(mock_backend_server.requests) == 2
    req2 = mock_backend_server.requests[1]
    assert req2.path == "/api/oauth/google/callback"
    assert req2.headers.get("x-proxy-secret") == secret
    assert req2.headers.get("x-dsm-user") == "alice"
    assert req2.headers.get("x-dsm-admin") == "0"
    assert req2.query == {"code": ["authcode123"], "state": ["statexyz"]}


def test_t1_e5_frontend_js_syntax_and_app_js_happy_path_in_node() -> None:
    """node --check passes on app.js & GPhotosMigrator.js, and app.js sends X-Requested-With."""
    for js_file in (APP_JS_PATH, GPHOTOS_MIGRATOR_JS_PATH):
        proc = subprocess.run(
            ["node", "--check", str(js_file)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert proc.returncode == 0, f"node --check failed on {js_file}: {proc.stderr}"

    result = run_app_js_scenario(
        pathname="/",
        actions=[{"type": "click", "id": "btn-scan"}],
        inspect_ids=["kpi-media"],
    )
    scan_calls = [c for c in result["fetchCalls"] if c["method"] == "POST" and "/api/actions/scan" in c["url"]]
    assert len(scan_calls) >= 1
    assert scan_calls[0]["headers"].get("x-requested-with"), "app.js must send X-Requested-With"
    assert scan_calls[0]["headers"].get("x-profile-id") == "default"


def test_t1_e6_unified_version_and_packaging_metadata() -> None:
    """Unified v1.2.0 version across __init__.py & pyproject.toml, manylinux2014 pins, and uv.lock."""
    assert gphotos_synology.__version__ == "1.2.0-0005"

    pyproject_text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "1.2.0.5"' in pyproject_text
    assert "pillow-heif" in pyproject_text
    assert "<12.3.0" in pyproject_text
    assert (REPO_ROOT / "uv.lock").is_file(), "G7 requires committed uv.lock"

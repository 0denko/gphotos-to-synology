"""Tests for visual similarity matching, resolution/clarity auto-upgrade, quarantine, restore & purge."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFilter

from gphotos_synology.config import ConfigManager, Settings
from gphotos_synology.database import StateDB
from gphotos_synology.main import create_app
from gphotos_synology.worker.pipeline import MigrationPipeline


def _detailed_scene(size: tuple[int, int] = (800, 600)) -> Image.Image:
    """Render a crisp, detailed scene with high-frequency edges for realistic sharpness tests."""
    w, h = size
    img = Image.new("RGB", (w, h), color=(28, 48, 86))
    draw = ImageDraw.Draw(img)
    # Sky gradient bands & sharp architectural/geometric details
    for i in range(0, w, max(8, w // 25)):
        draw.line([(i, 0), (i, h // 2)], fill=(40 + (i % 120), 90, 150), width=2)
    draw.rectangle([w // 8, h // 4, w // 2, (h * 3) // 4], fill=(235, 175, 45), outline=(255, 255, 255), width=4)
    draw.ellipse([w // 2, h // 5, (w * 7) // 8, (h * 4) // 5], fill=(35, 195, 130), outline=(15, 25, 40), width=4)
    # Crisp grid lines for strong Laplacian edge response
    for y in range(h // 3, (h * 2) // 3, max(6, h // 30)):
        draw.line([(w // 6, y), ((w * 5) // 6, y)], fill=(250, 250, 250), width=2)
    return img


def _jpeg_bytes(img: Image.Image, *, quality: int = 92, exif_dt: str | None = None) -> bytes:
    buf = io.BytesIO()
    kwargs: dict = {"format": "JPEG", "quality": quality}
    if exif_dt:
        exif = Image.Exif()
        exif[36867] = exif_dt
        kwargs["exif"] = exif.tobytes()
    img.save(buf, **kwargs)
    return buf.getvalue()


def test_higher_resolution_takeout_upgrades_lower_res_nas_and_quarantines_old(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "config",
        work_dir=tmp_path / "work",
        inbox_dir=tmp_path / "inbox",
        photos_dir=tmp_path / "photos",
        inbox_stability_seconds=0,
        min_free_disk_gb=0.1,
        write_exif_metadata=False,
        auto_index_existing_photos=True,
        dedup_mode="perceptual",
        duplicate_quality_policy="auto_upgrade_quarantine",
    )
    cfg = ConfigManager(settings)
    db = StateDB(cfg.settings.db_path)

    high_res = _detailed_scene((800, 600))
    low_res = high_res.resize((320, 240), Image.Resampling.LANCZOS)

    # Low-res photo already sits on NAS
    nas_file = cfg.settings.photos_dir / "2024" / "07" / "ROADTRIP.jpg"
    nas_file.parent.mkdir(parents=True)
    nas_file.write_bytes(_jpeg_bytes(low_res, quality=80, exif_dt="2024:07:03 10:13:20"))

    # Takeout brings the crisp 800x600 original inside an album
    zip_path = cfg.settings.inbox_dir / "takeout-20261121T030000Z-001.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("Takeout/Google Photos/Roadtrip/metadata.json", json.dumps({"title": "Roadtrip"}))
        zf.writestr("Takeout/Google Photos/Roadtrip/ROADTRIP_ORIG.jpg", _jpeg_bytes(high_res, quality=95))
        zf.writestr(
            "Takeout/Google Photos/Roadtrip/ROADTRIP_ORIG.jpg.json",
            json.dumps({"title": "ROADTRIP_ORIG.jpg", "photoTakenTime": {"timestamp": "1720001600"}}),
        )

    pipeline = MigrationPipeline(config_manager=cfg, db=db)
    pipeline.run_cycle()

    # The NAS path was upgraded in place to 800x600
    with Image.open(nas_file) as upgraded_img:
        assert upgraded_img.size == (800, 600)

    # Replaced 320x240 copy is safely quarantined outside PHOTOS_DIR
    upgrades = db.list_quality_upgrades()
    assert len(upgrades) == 1
    up = upgrades[0]
    assert up["status"] == "quarantined"
    assert up["old_width"] == 320 and up["new_width"] == 800
    assert "Higher resolution" in up["upgrade_reason"]
    q_file = cfg.settings.quarantine_dir / up["quarantine_filename"]
    assert q_file.is_file()
    with Image.open(q_file) as old_img:
        assert old_img.size == (320, 240)

    # Album membership was preserved on the upgraded photo
    albums = db.list_albums_summary()
    assert len(albums) == 1 and albums[0]["name"] == "Roadtrip" and albums[0]["total_items"] == 1


def test_sharper_image_upgrades_blurry_copy_at_same_resolution_and_never_downgrades(
    tmp_path: Path,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "config",
        work_dir=tmp_path / "work",
        inbox_dir=tmp_path / "inbox",
        photos_dir=tmp_path / "photos",
        inbox_stability_seconds=0,
        min_free_disk_gb=0.1,
        write_exif_metadata=False,
        auto_index_existing_photos=True,
        dedup_mode="perceptual",
        duplicate_quality_policy="auto_upgrade_quarantine",
    )
    cfg = ConfigManager(settings)
    db = StateDB(cfg.settings.db_path)

    sharp = _detailed_scene((640, 480))
    blurry = sharp.filter(ImageFilter.GaussianBlur(radius=3.0))

    # Blurry version currently on NAS
    nas_file = cfg.settings.photos_dir / "2024" / "08" / "CITY.jpg"
    nas_file.parent.mkdir(parents=True)
    nas_file.write_bytes(_jpeg_bytes(blurry, quality=85, exif_dt="2024:08:01 09:00:00"))

    # Archive 1: sharp version -> should UPGRADE
    zip1 = cfg.settings.inbox_dir / "takeout-20261121T040000Z-001.zip"
    with zipfile.ZipFile(zip1, "w") as zf:
        zf.writestr("Takeout/Google Photos/Photos from 2024/CITY.jpg", _jpeg_bytes(sharp, quality=85))
        zf.writestr(
            "Takeout/Google Photos/Photos from 2024/CITY.jpg.json",
            json.dumps({"title": "CITY.jpg", "photoTakenTime": {"timestamp": "1722502800"}}),
        )

    pipeline = MigrationPipeline(config_manager=cfg, db=db)
    pipeline.run_cycle()

    upgrades = db.list_quality_upgrades()
    assert len(upgrades) == 1
    assert "Better clarity" in upgrades[0]["upgrade_reason"]
    assert upgrades[0]["new_sharpness"] > upgrades[0]["old_sharpness"]

    # Archive 2: blurry copy arrives later -> must NOT downgrade the crisp NAS photo!
    zip2 = cfg.settings.inbox_dir / "takeout-20261121T050000Z-001.zip"
    with zipfile.ZipFile(zip2, "w") as zf:
        zf.writestr("Takeout/Google Photos/Photos from 2024/CITY_BLUR.jpg", _jpeg_bytes(blurry, quality=80))
        zf.writestr(
            "Takeout/Google Photos/Photos from 2024/CITY_BLUR.jpg.json",
            json.dumps({"title": "CITY_BLUR.jpg", "photoTakenTime": {"timestamp": "1722502800"}}),
        )

    pipeline.run_cycle()
    assert len(db.list_quality_upgrades()) == 1  # Still just the 1 upgrade!
    assert db.get_stats()["total_duplicates_skipped"] == 1


def test_upgrades_api_preview_restore_and_purge(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "config",
        work_dir=tmp_path / "work",
        inbox_dir=tmp_path / "inbox",
        photos_dir=tmp_path / "photos",
        inbox_stability_seconds=0,
        min_free_disk_gb=0.1,
        write_exif_metadata=False,
        auto_index_existing_photos=True,
        dedup_mode="perceptual",
        duplicate_quality_policy="auto_upgrade_quarantine",
    )
    app = create_app(base_settings=settings, start_worker=False)

    high_res = _detailed_scene((640, 480))
    low_res = high_res.resize((240, 180), Image.Resampling.LANCZOS)

    nas_file = settings.photos_dir / "2024" / "09" / "SUNSET.jpg"
    nas_file.parent.mkdir(parents=True)
    nas_file.write_bytes(_jpeg_bytes(low_res, quality=80, exif_dt="2024:09:10 12:00:00"))

    zip_path = settings.inbox_dir / "takeout-20261121T060000Z-001.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("Takeout/Google Photos/Photos from 2024/SUNSET.jpg", _jpeg_bytes(high_res, quality=92))
        zf.writestr(
            "Takeout/Google Photos/Photos from 2024/SUNSET.jpg.json",
            json.dumps({"title": "SUNSET.jpg", "photoTakenTime": {"timestamp": "1725969600"}}),
        )

    with TestClient(app) as client:
        app.state.ctx.pipeline.run_cycle()

        upgrades = client.get("/api/upgrades").json()["upgrades"]
        assert len(upgrades) == 1
        uid = upgrades[0]["id"]

        # Side-by-side preview endpoints return valid JPEG thumbnails
        kept_prev = client.get(f"/api/upgrades/{uid}/preview/kept")
        quar_prev = client.get(f"/api/upgrades/{uid}/preview/quarantined")
        assert kept_prev.status_code == 200 and kept_prev.headers["content-type"] == "image/jpeg"
        assert quar_prev.status_code == 200 and quar_prev.headers["content-type"] == "image/jpeg"

        # Restore swaps the quarantined 240x180 copy back into PHOTOS_DIR
        restore_resp = client.post(f"/api/upgrades/{uid}/restore")
        assert restore_resp.status_code == 200
        with Image.open(nas_file) as restored_img:
            assert restored_img.size == (240, 180)
        assert client.get("/api/upgrades").json()["upgrades"][0]["status"] == "restored"

        # Purge endpoint cleans up cleanly
        purge_resp = client.post(f"/api/upgrades/{uid}/purge")
        assert purge_resp.status_code == 200
        assert client.get("/api/upgrades").json()["upgrades"][0]["status"] == "purged"

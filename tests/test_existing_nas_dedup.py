"""Tests for existing Synology NAS library indexing and 3-tier deduplication."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw

from gphotos_synology.config import ConfigManager, Settings
from gphotos_synology.database import StateDB
from gphotos_synology.synology.scanner import ExistingLibraryScanner
from gphotos_synology.worker.pipeline import MigrationPipeline


def _make_sample_image(seed: int = 1, size: tuple[int, int] = (160, 120)) -> Image.Image:
    """Create a rich, non-uniform RGB test image so dHash has distinctive gradients."""
    img = Image.new("RGB", size, color=(20 + seed * 30, 40, 80))
    draw = ImageDraw.Draw(img)
    if seed == 1:
        draw.rectangle([10, 10, 80, 100], fill=(240, 180, 50))
        draw.ellipse([70, 30, 150, 110], fill=(30, 200, 140))
        draw.line([0, 0, 160, 120], fill=(255, 255, 255), width=6)
    else:
        draw.polygon([(80, 5), (155, 115), (5, 115)], fill=(230, 50, 90))
        draw.rectangle([90, 10, 150, 60], fill=(40, 120, 255))
    return img


def _image_bytes(img: Image.Image, fmt: str = "PNG", **save_kwargs) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt, **save_kwargs)
    return buf.getvalue()


def test_existing_nas_scanner_skips_synology_system_dirs_and_is_incremental(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "config",
        work_dir=tmp_path / "work",
        inbox_dir=tmp_path / "inbox",
        photos_dir=tmp_path / "photos",
        dedup_mode="exact_and_pixels",
    )
    settings.ensure_directories()
    db = StateDB(settings.db_path)

    # Real existing NAS photo
    real_photo = settings.photos_dir / "2023" / "05" / "existing.png"
    real_photo.parent.mkdir(parents=True)
    real_photo.write_bytes(_image_bytes(_make_sample_image(1), "PNG"))

    # Synology DSM thumbnail & recycle bin files that MUST be ignored
    thumb = settings.photos_dir / "2023" / "05" / "@eaDir" / "existing.png" / "SYNOPHOTO_THUMB_M.jpg"
    thumb.parent.mkdir(parents=True)
    thumb.write_bytes(_image_bytes(_make_sample_image(2), "JPEG"))

    recycled = settings.photos_dir / "#recycle" / "deleted_photo.jpg"
    recycled.parent.mkdir(parents=True)
    recycled.write_bytes(_image_bytes(_make_sample_image(2), "JPEG"))

    scanner = ExistingLibraryScanner(settings, db)
    first = scanner.scan()
    assert first.scanned == 1
    assert first.newly_indexed == 1
    assert first.unchanged_skipped == 0
    assert db.get_stats()["existing_nas_indexed"] == 1

    # Second scan skips unchanged file immediately
    second = scanner.scan()
    assert second.scanned == 1
    assert second.newly_indexed == 0
    assert second.unchanged_skipped == 1

    # Deleting the file from NAS prunes its stale index row
    real_photo.unlink()
    third = scanner.scan()
    assert third.pruned_missing == 1
    assert db.get_stats()["existing_nas_indexed"] == 0


def test_dedup_against_existing_nas_exact_and_exif_header_changed(tmp_path: Path) -> None:
    """Verify exact SHA-256 AND pixel-content SHA-256 skip duplicates already on the NAS."""
    settings = Settings(
        data_dir=tmp_path / "config",
        work_dir=tmp_path / "work",
        inbox_dir=tmp_path / "inbox",
        photos_dir=tmp_path / "photos",
        inbox_stability_seconds=0,
        min_free_disk_gb=0.1,
        write_exif_metadata=False,
        auto_index_existing_photos=True,
        dedup_mode="exact_and_pixels",
    )
    cfg = ConfigManager(settings)
    db = StateDB(cfg.settings.db_path)

    img1 = _make_sample_image(1)
    img2 = _make_sample_image(2)

    # Place an existing PNG on NAS with EXIF/comment metadata (different file bytes from bare PNG!)
    nas_photo = cfg.settings.photos_dir / "2024" / "01" / "NAS_ORIGINAL.png"
    nas_photo.parent.mkdir(parents=True)
    from PIL.PngImagePlugin import PngInfo

    pnginfo = PngInfo()
    pnginfo.add_text("Comment", "Already backed up to Synology NAS with extra header")
    nas_bytes = _image_bytes(img1, "PNG", pnginfo=pnginfo)
    nas_photo.write_bytes(nas_bytes)

    # Takeout archive contains:
    #  1) Same image WITHOUT the PNG text chunk (different SHA-256, identical RGB pixels!) inside an album
    #  2) A genuinely new second photo
    takeout_bare_bytes = _image_bytes(img1, "PNG")
    assert takeout_bare_bytes != nas_bytes  # Confirm raw SHA-256 differs!

    zip_path = cfg.settings.inbox_dir / "takeout-20261121T010000Z-001.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("Takeout/Google Photos/Favorites/metadata.json", json.dumps({"title": "Favorites"}))
        zf.writestr("Takeout/Google Photos/Favorites/IMG_DUP.png", takeout_bare_bytes)
        zf.writestr(
            "Takeout/Google Photos/Favorites/IMG_DUP.png.json",
            json.dumps({"title": "IMG_DUP.png", "photoTakenTime": {"timestamp": "1705000000"}}),
        )
        zf.writestr("Takeout/Google Photos/Photos from 2024/IMG_NEW.png", _image_bytes(img2, "PNG"))
        zf.writestr(
            "Takeout/Google Photos/Photos from 2024/IMG_NEW.png.json",
            json.dumps({"title": "IMG_NEW.png", "photoTakenTime": {"timestamp": "1706000000"}}),
        )

    pipeline = MigrationPipeline(config_manager=cfg, db=db)
    summary = pipeline.run_cycle()

    assert summary["existing_newly_indexed"] == 1
    assert summary["inbox_processed"] == 1
    assert summary["errors"] == 0

    stats = db.get_stats()
    assert stats["existing_nas_indexed"] == 1
    assert stats["total_media"] == 1  # Only IMG_NEW.png was imported!
    assert stats["total_duplicates_skipped"] == 1  # IMG_DUP.png was caught via pixel_sha256!

    # Existing NAS photo still received the 'Favorites' album membership!
    albums = db.list_albums_summary()
    assert len(albums) == 1
    assert albums[0]["name"] == "Favorites"
    assert albums[0]["total_items"] == 1


def test_perceptual_dedup_catches_storage_saver_recompression_without_false_positives(
    tmp_path: Path,
) -> None:
    """In perceptual mode, JPEG re-compression of the same photo is caught; distinct photos are kept."""
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
        perceptual_max_distance=4,
    )
    cfg = ConfigManager(settings)
    db = StateDB(cfg.settings.db_path)

    img1 = _make_sample_image(1)
    img2 = _make_sample_image(2)

    # High-quality original already on NAS with EXIF DateTimeOriginal
    exif = Image.Exif()
    exif[36867] = "2024:07:03 10:13:20"  # Matches unix timestamp 1720001600 UTC
    nas_photo = cfg.settings.photos_dir / "2024" / "07" / "VACATION_01.jpg"
    nas_photo.parent.mkdir(parents=True)
    nas_photo.write_bytes(_image_bytes(img1, "JPEG", quality=95, exif=exif.tobytes()))

    # Takeout contains a Storage-Saver re-compressed JPEG (quality=55, different SHA-256 & pixels!)
    # plus a completely distinct second JPEG
    recompressed = _image_bytes(img1, "JPEG", quality=55)
    distinct = _image_bytes(img2, "JPEG", quality=90)

    zip_path = cfg.settings.inbox_dir / "takeout-20261121T020000Z-001.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("Takeout/Google Photos/Photos from 2024/VACATION_01.jpg", recompressed)
        zf.writestr(
            "Takeout/Google Photos/Photos from 2024/VACATION_01.jpg.json",
            json.dumps({"title": "VACATION_01.jpg", "photoTakenTime": {"timestamp": "1720001600"}}),
        )
        zf.writestr("Takeout/Google Photos/Photos from 2024/VACATION_02.jpg", distinct)
        zf.writestr(
            "Takeout/Google Photos/Photos from 2024/VACATION_02.jpg.json",
            json.dumps({"title": "VACATION_02.jpg", "photoTakenTime": {"timestamp": "1720001605"}}),
        )

    pipeline = MigrationPipeline(config_manager=cfg, db=db)
    pipeline.run_cycle()

    stats = db.get_stats()
    assert stats["existing_nas_indexed"] == 1
    assert stats["total_duplicates_skipped"] == 1  # Re-compressed VACATION_01 skipped!
    assert stats["total_media"] == 1  # Distinct VACATION_02 imported!
    assert (cfg.settings.photos_dir / "2024/07/VACATION_02.jpg").exists()

"""Unit tests for metadata parsing, mtime syncing, and atomic Synology folder importing."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from gphotos_synology.config import Settings
from gphotos_synology.synology.importer import SynologyFolderImporter
from gphotos_synology.takeout.metadata import (
    UNKNOWN_DATE,
    PhotoMetadata,
    apply_metadata,
    parse_date_from_filename,
    parse_sidecar_json,
)


def test_parse_sidecar_json_and_zero_gps_handling(tmp_path: Path) -> None:
    sidecar = tmp_path / "IMG_1.JPG.json"
    sidecar.write_text(
        json.dumps(
            {
                "title": "IMG_1.JPG",
                "description": "Golden gate sunset",
                "photoTakenTime": {"timestamp": "1720000000"},
                "geoData": {"latitude": 37.8199, "longitude": -122.4783, "altitude": 67.5},
                "geoDataExif": {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0},
                "people": [{"name": "Alice"}, {"name": "Bob"}],
            }
        ),
        encoding="utf-8",
    )

    meta = parse_sidecar_json(sidecar, album_name="SF Trip")
    assert meta.title == "IMG_1.JPG"
    assert meta.description == "Golden gate sunset"
    assert meta.taken_at == datetime.fromtimestamp(1720000000, tz=UTC)
    # Should fall back to geoData when geoDataExif is 0.0, 0.0
    assert meta.has_gps is True
    assert meta.latitude == 37.8199
    assert meta.longitude == -122.4783
    assert meta.people == ["Alice", "Bob"]
    assert meta.album_name == "SF Trip"


def test_apply_metadata_mtime_fallback_and_folder_importer(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "config",
        work_dir=tmp_path / "work",
        inbox_dir=tmp_path / "inbox",
        photos_dir=tmp_path / "photos",
        folder_template="year_month",
    )
    settings.ensure_directories()

    photo = tmp_path / "work" / "IMG_5555.JPG"
    photo.write_bytes(b"fake-jpeg-bytes-1")

    taken = datetime(2024, 7, 15, 14, 30, 0, tzinfo=UTC)
    meta = PhotoMetadata(taken_at=taken, album_name="Beach")

    # Force mtime_only path (write_exif=False) so test works regardless of exiftool presence
    status = apply_metadata(photo, meta, write_exif=False)
    assert status == "mtime_only"
    assert int(photo.stat().st_mtime) == int(taken.timestamp())

    importer = SynologyFolderImporter(settings)
    dest_abs, dest_rel = importer.import_media(photo, meta)
    assert dest_rel == "2024/07/IMG_5555.JPG"
    assert dest_abs.exists()
    assert not photo.exists()

    # Collision safety: second distinct file with same name gets _2 suffix
    photo2 = tmp_path / "work" / "IMG_5555.JPG"
    photo2.write_bytes(b"fake-jpeg-bytes-2")
    dest_abs2, dest_rel2 = importer.import_media(photo2, meta)
    assert dest_rel2 == "2024/07/IMG_5555_2.JPG"
    assert dest_abs2.exists()


def test_apply_metadata_piexif_jpeg_fallback_without_exiftool(tmp_path: Path) -> None:
    import piexif
    from PIL import Image

    jpeg_path = tmp_path / "sunset.jpg"
    Image.new("RGB", (64, 48), (120, 80, 200)).save(jpeg_path, format="JPEG")

    taken = datetime(2023, 8, 20, 18, 45, 12, tzinfo=UTC)
    meta = PhotoMetadata(
        taken_at=taken,
        description="Pier sunset",
        latitude=37.8199,
        longitude=-122.4783,
        altitude=12.5,
    )

    status = apply_metadata(
        jpeg_path,
        meta,
        write_exif=True,
        exiftool_path="nonexistent_exiftool_binary_for_test",
    )
    assert status == "repaired"
    assert int(jpeg_path.stat().st_mtime) == int(taken.timestamp())

    exif = piexif.load(str(jpeg_path))
    assert exif["Exif"][piexif.ExifIFD.DateTimeOriginal] == b"2023:08:20 18:45:12"
    assert exif["0th"][piexif.ImageIFD.ImageDescription] == b"Pier sunset"
    assert exif["GPS"][piexif.GPSIFD.GPSLatitudeRef] == b"N"
    assert exif["GPS"][piexif.GPSIFD.GPSLongitudeRef] == b"W"


def test_parse_date_from_filename() -> None:
    # Compact YYYYMMDD with a trailing _HHMMSS time group.
    assert parse_date_from_filename("VID_20161021_122127.mp4") == datetime(
        2016, 10, 21, 12, 21, 27, tzinfo=UTC
    )
    assert parse_date_from_filename("VID_20221229_183719.mp4") == datetime(
        2022, 12, 29, 18, 37, 19, tzinfo=UTC
    )
    assert parse_date_from_filename("20141031_114457.jpg") == datetime(
        2014, 10, 31, 11, 44, 57, tzinfo=UTC
    )
    # Date only, no time group: midnight UTC.
    assert parse_date_from_filename("IMG_20161021.jpg") == datetime(2016, 10, 21, tzinfo=UTC)
    # Separator forms.
    assert parse_date_from_filename("IMG_2024-07-15.jpg") == datetime(2024, 7, 15, tzinfo=UTC)
    assert parse_date_from_filename("IMG_2024_07_15.jpg") == datetime(2024, 7, 15, tzinfo=UTC)
    # Invalid date-like prefix does not shadow a valid date later in the filename.
    assert parse_date_from_filename("export_20260230_20240715.jpg") == datetime(
        2024, 7, 15, tzinfo=UTC
    )
    # No date clue in the name.
    assert parse_date_from_filename("- 11445.JPG") is None
    assert parse_date_from_filename("IMG_0559.MP4") is None
    assert parse_date_from_filename("DSC_0001.JPG") is None
    # Digit runs that are not valid dates or are in the future are rejected.
    assert parse_date_from_filename("IMG_20231301.jpg") is None  # month 13
    assert parse_date_from_filename("IMG_20260230.jpg") is None  # Feb 30
    assert parse_date_from_filename("IMG_12345678.jpg") is None  # month 34
    assert parse_date_from_filename("IMG_20990101.jpg") is None  # far future


def test_unknown_date_sentinel_and_importer_folder(tmp_path: Path) -> None:
    from gphotos_synology.takeout.metadata import is_unknown_date

    assert datetime(1970, 1, 2, tzinfo=UTC) == UNKNOWN_DATE
    assert UNKNOWN_DATE.timestamp() > 0
    assert is_unknown_date(UNKNOWN_DATE) is True
    assert is_unknown_date(UNKNOWN_DATE.isoformat()) is True
    assert is_unknown_date("1900-01-01T00:00:00+00:00") is True
    assert is_unknown_date("2024-07-15T00:00:00+00:00") is False
    assert is_unknown_date(None) is False

    settings = Settings(
        data_dir=tmp_path / "config",
        work_dir=tmp_path / "work",
        inbox_dir=tmp_path / "inbox",
        photos_dir=tmp_path / "photos",
        folder_template="year_month",
    )
    settings.ensure_directories()

    # A dateless item stamped with the sentinel files under 1970/01,
    # never under the import month, so it cannot pop up in "Recent".
    photo = tmp_path / "work" / "- 11445.JPG"
    photo.write_bytes(b"fake-jpeg-bytes")
    meta = PhotoMetadata(taken_at=UNKNOWN_DATE)
    apply_metadata(photo, meta, write_exif=False)
    assert int(photo.stat().st_mtime) == int(UNKNOWN_DATE.timestamp())
    importer = SynologyFolderImporter(settings)
    _, dest_rel = importer.import_media(photo, meta)
    assert dest_rel == "1970/01/- 11445.JPG"


def test_perceptual_dedup_treats_unknown_date_sentinel_as_missing() -> None:
    from gphotos_synology.takeout.dedup import is_perceptual_match

    # Two 128-bit hashes at Hamming distance 6 (within max_distance=10, > strict 4)
    base_hash = "00000000000000000000000000000000"
    dist6_hash = "0000000000000000000000000000003f"  # 6 bits set

    # Two distinct dateless photos with different filenames must NOT falsely
    # corroborate each other just because both carry UNKNOWN_DATE.
    assert (
        is_perceptual_match(
            base_hash,
            dist6_hash,
            incoming_taken_at=UNKNOWN_DATE.isoformat(),
            existing_taken_at=UNKNOWN_DATE.isoformat(),
            incoming_filename="- 11445.JPG",
            existing_filename="- 11446.JPG",
            incoming_width=1600,
            incoming_height=1200,
            existing_width=1600,
            existing_height=1200,
            max_distance=10,
        )
        is False
    )

    # A dateless incoming copy compared against a dated existing photo with the
    # same stem must NOT be rejected as "conflicting timestamps".
    assert (
        is_perceptual_match(
            base_hash,
            dist6_hash,
            incoming_taken_at=None,
            existing_taken_at="2015-06-10T12:00:00+00:00",
            incoming_filename="IMG_1234.JPG",
            existing_filename="IMG_1234.JPG",
            incoming_width=1600,
            incoming_height=1200,
            existing_width=1600,
            existing_height=1200,
            max_distance=10,
        )
        is True
    )
    assert (
        is_perceptual_match(
            base_hash,
            dist6_hash,
            incoming_taken_at="2015-06-10T12:00:00+00:00",
            existing_taken_at=UNKNOWN_DATE.isoformat(),
            incoming_filename="IMG_1234.JPG",
            existing_filename="IMG_1234.JPG",
            incoming_width=1600,
            incoming_height=1200,
            existing_width=1600,
            existing_height=1200,
            max_distance=10,
        )
        is True
    )


def test_pipeline_dateless_import_and_quality_upgrade_preserves_real_date(tmp_path: Path) -> None:
    import zipfile

    import piexif
    from PIL import Image, ImageDraw

    from gphotos_synology.config import ConfigManager
    from gphotos_synology.database import StateDB
    from gphotos_synology.worker.pipeline import MigrationPipeline

    settings = Settings(
        data_dir=tmp_path / "config",
        work_dir=tmp_path / "work",
        inbox_dir=tmp_path / "inbox",
        photos_dir=tmp_path / "photos",
        inbox_stability_seconds=0,
        min_free_disk_gb=0.1,
        write_exif_metadata=True,
        exiftool_path="nonexistent_exiftool_binary_for_test",
        synology_sync_albums=False,
        dedup_mode="perceptual",
        duplicate_quality_policy="auto_upgrade_quarantine",
    )
    settings.ensure_directories()

    # 1. Seed an existing dated lower-res photo in the NAS library so we can
    #    verify a dateless higher-res Takeout copy upgrades it WITHOUT
    #    clobbering its real taken_at with UNKNOWN_DATE.
    def _draw_scene(size: tuple[int, int]) -> Image.Image:
        img = Image.new("RGB", size, (30, 60, 90))
        draw = ImageDraw.Draw(img)
        w, h = size
        draw.rectangle([w // 6, h // 6, w * 5 // 6, h * 5 // 6], fill=(200, 140, 40))
        draw.ellipse([w // 3, h // 3, w * 2 // 3, h * 2 // 3], fill=(40, 190, 120))
        return img

    existing_dir = settings.photos_dir / "2018" / "05"
    existing_dir.mkdir(parents=True, exist_ok=True)
    existing_photo = existing_dir / "SCENE_01.JPG"
    _draw_scene((160, 120)).save(existing_photo, format="JPEG", quality=75)
    real_taken = datetime(2018, 5, 20, 10, 0, 0, tzinfo=UTC)
    apply_metadata(
        existing_photo,
        PhotoMetadata(taken_at=real_taken),
        write_exif=True,
        exiftool_path="nonexistent_exiftool_binary_for_test",
    )

    # 2. Build a Takeout ZIP with:
    #    - a dateless higher-res copy of SCENE_01.JPG (should upgrade & keep 2018-05-20)
    #    - a dateless filename-dated video VID_20161021_122127.mp4 (should file under 2016/10)
    #    - a truly dateless photo - 11445.JPG (should file under 1970/01)
    hires_tmp = tmp_path / "hires.jpg"
    _draw_scene((640, 480)).save(hires_tmp, format="JPEG", quality=95)
    dateless_tmp = tmp_path / "dateless.jpg"
    Image.new("RGB", (120, 90), (180, 30, 50)).save(dateless_tmp, format="JPEG", quality=90)

    zip_path = settings.inbox_dir / "takeout-dateless-test.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(hires_tmp, "Takeout/Google Photos/Photos from 2018/SCENE_01.JPG")
        zf.writestr(
            "Takeout/Google Photos/Photos from 2016/VID_20161021_122127.mp4",
            b"\x00\x00\x00\x18ftypmp42MOCK_VIDEO_BYTES",
        )
        zf.write(dateless_tmp, "Takeout/Google Photos/Photos from 2026/- 11445.JPG")

    cfg = ConfigManager(settings)
    db = StateDB(cfg.settings.db_path)
    pipeline = MigrationPipeline(cfg, db)

    # Index existing library first, then run the Takeout archive
    pipeline.index_existing_library()
    summary = pipeline.run_cycle()
    assert summary["inbox_processed"] == 1
    assert summary["errors"] == 0

    # Verify filename-dated video landed under 2016/10
    vid_dest = settings.photos_dir / "2016" / "10" / "VID_20161021_122127.mp4"
    assert vid_dest.is_file()

    # Verify truly dateless photo landed under 1970/01 with positive mtime & EXIF
    dateless_dest = settings.photos_dir / "1970" / "01" / "- 11445.JPG"
    assert dateless_dest.is_file()
    assert int(dateless_dest.stat().st_mtime) == int(UNKNOWN_DATE.timestamp())
    dateless_exif = piexif.load(str(dateless_dest))
    assert dateless_exif["Exif"][piexif.ExifIFD.DateTimeOriginal] == b"1970:01:02 00:00:00"

    # Verify upgraded SCENE_01.JPG kept its real 2018-05-20 date in DB and EXIF
    assert existing_photo.is_file()
    upgraded_exif = piexif.load(str(existing_photo))
    assert upgraded_exif["Exif"][piexif.ExifIFD.DateTimeOriginal] == b"2018:05:20 10:00:00"
    with db.transaction() as conn:
        rows = {
            r["dest_rel_path"]: r["taken_at"]
            for r in conn.execute("SELECT dest_rel_path, taken_at FROM media_items").fetchall()
        }
    assert rows["2018/05/SCENE_01.JPG"] == real_taken.isoformat()
    assert rows["2016/10/VID_20161021_122127.mp4"] == datetime(2016, 10, 21, 12, 21, 27, tzinfo=UTC).isoformat()
    assert rows["1970/01/- 11445.JPG"] == UNKNOWN_DATE.isoformat()



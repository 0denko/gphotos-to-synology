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
    # No date clue in the name.
    assert parse_date_from_filename("- 11445.JPG") is None
    assert parse_date_from_filename("IMG_0559.MP4") is None
    assert parse_date_from_filename("DSC_0001.JPG") is None
    # Digit runs that are not valid dates are rejected.
    assert parse_date_from_filename("IMG_20231301.jpg") is None  # month 13
    assert parse_date_from_filename("IMG_20260230.jpg") is None  # Feb 30
    assert parse_date_from_filename("IMG_12345678.jpg") is None  # month 34


def test_unknown_date_sentinel_and_importer_folder(tmp_path: Path) -> None:
    assert UNKNOWN_DATE == datetime(1900, 1, 1, tzinfo=UTC)

    settings = Settings(
        data_dir=tmp_path / "config",
        work_dir=tmp_path / "work",
        inbox_dir=tmp_path / "inbox",
        photos_dir=tmp_path / "photos",
        folder_template="year_month",
    )
    settings.ensure_directories()

    # A dateless item stamped with the sentinel files under 1900/01,
    # never under the import month, so it cannot pop up in "Recent".
    photo = tmp_path / "work" / "- 11445.JPG"
    photo.write_bytes(b"fake-jpeg-bytes")
    meta = PhotoMetadata(taken_at=UNKNOWN_DATE)
    importer = SynologyFolderImporter(settings)
    _, dest_rel = importer.import_media(photo, meta)
    assert dest_rel == "1900/01/- 11445.JPG"


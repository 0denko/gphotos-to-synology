"""Unit tests for metadata parsing, mtime syncing, and atomic Synology folder importing."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from gphotos_synology.config import Settings
from gphotos_synology.synology.importer import SynologyFolderImporter
from gphotos_synology.takeout.metadata import PhotoMetadata, apply_metadata, parse_sidecar_json


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

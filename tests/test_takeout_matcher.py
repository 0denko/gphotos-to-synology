"""Unit tests for Google Takeout JSON sidecar matching and album detection."""

from __future__ import annotations

import json
from pathlib import Path

from gphotos_synology.takeout.matcher import (
    DirectoryJsonIndex,
    detect_album_name,
    find_sidecar_json,
)


def _touch(path: Path, content: str = "data") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_standard_and_supplemental_metadata_matches(tmp_path: Path) -> None:
    photo1 = _touch(tmp_path / "IMG_1001.JPG")
    json1 = _touch(tmp_path / "IMG_1001.JPG.json", "{}")

    photo2 = _touch(tmp_path / "IMG_1002.HEIC")
    json2 = _touch(tmp_path / "IMG_1002.HEIC.supplemental-metadata.json", "{}")

    photo3 = _touch(tmp_path / "PXL_20250601_123456789.PORTRAIT.jpg")
    json3 = _touch(tmp_path / "PXL_20250601_123456789.PORTRAIT.jpg.suppl.json", "{}")

    idx = DirectoryJsonIndex.build(tmp_path)
    assert find_sidecar_json(photo1, idx) == json1
    assert find_sidecar_json(photo2, idx) == json2
    assert find_sidecar_json(photo3, idx) == json3


def test_numbered_duplicate_and_edited_matches(tmp_path: Path) -> None:
    # Takeout puts (1) after extension in JSON name
    dup_photo = _touch(tmp_path / "IMG_2000(1).JPG")
    dup_json = _touch(tmp_path / "IMG_2000.JPG(1).json", "{}")

    dup_photo2 = _touch(tmp_path / "IMG_2001(2).JPG")
    dup_json2 = _touch(tmp_path / "IMG_2001.JPG.supplemental-metadata(2).json", "{}")

    # Edited copy inherits unedited JSON
    edited_photo = _touch(tmp_path / "IMG_3000-edited.JPG")
    orig_json = _touch(tmp_path / "IMG_3000.JPG.supplemental-metadata.json", "{}")

    # Localized edited suffix
    german_edited = _touch(tmp_path / "IMG_3001-bearbeitet.JPG")
    german_json = _touch(tmp_path / "IMG_3001.JPG.json", "{}")

    idx = DirectoryJsonIndex.build(tmp_path)
    assert find_sidecar_json(dup_photo, idx) == dup_json
    assert find_sidecar_json(dup_photo2, idx) == dup_json2
    assert find_sidecar_json(edited_photo, idx) == orig_json
    assert find_sidecar_json(german_edited, idx) == german_json


def test_live_photo_and_46_char_truncation_and_title_fallback(tmp_path: Path) -> None:
    # Live Photo companion video matches HEIC sidecar
    live_vid = _touch(tmp_path / "IMG_4000.MP4")
    heic_json = _touch(tmp_path / "IMG_4000.HEIC.supplemental-metadata.json", "{}")

    # 46-char truncated sidecar filename
    long_name = "a_very_long_google_photos_exported_filename_20250701_99999.jpg"
    long_photo = _touch(tmp_path / long_name)
    trunc_json = _touch(tmp_path / f"{long_name[:46]}.json", "{}")

    # Fallback via internal "title" JSON field
    weird_photo = _touch(tmp_path / "Sunset Over Tokyo.jpg")
    title_json = _touch(
        tmp_path / "unpredictable_sidecar_name.json",
        json.dumps({"title": "Sunset Over Tokyo.jpg"}),
    )

    idx = DirectoryJsonIndex.build(tmp_path)
    assert find_sidecar_json(live_vid, idx) == heic_json
    assert find_sidecar_json(long_photo, idx) == trunc_json
    assert find_sidecar_json(weird_photo, idx) == title_json


def test_detect_album_name(tmp_path: Path) -> None:
    yearly = tmp_path / "Photos from 2024"
    yearly.mkdir()
    assert detect_album_name(yearly) is None

    archive = tmp_path / "Archive"
    archive.mkdir()
    assert detect_album_name(archive) is None

    album_dir = tmp_path / "Summer_Trip_2024"
    album_dir.mkdir()
    assert detect_album_name(album_dir) == "Summer_Trip_2024"

    # Prefer canonical album title from metadata.json when present
    (album_dir / "metadata.json").write_text(
        json.dumps({"title": "Summer Trip: Italy 2024"}),
        encoding="utf-8",
    )
    assert detect_album_name(album_dir) == "Summer Trip: Italy 2024"

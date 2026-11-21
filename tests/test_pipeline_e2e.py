"""End-to-end test: synthetic Google Takeout ZIP -> inbox -> dedup -> import -> mock DSM 7 API."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import ClassVar

from gphotos_synology.config import ConfigManager, Settings
from gphotos_synology.database import StateDB
from gphotos_synology.worker.pipeline import MigrationPipeline


class FakeSynologyPhotosClient:
    """In-memory mock of Synology DSM 7 Photos API for end-to-end verification."""

    created_albums: ClassVar[dict[str, int]] = {}
    album_items: ClassVar[dict[int, list[int]]] = {}
    indexed_paths: ClassVar[dict[str, int]] = {}

    def __init__(self, settings) -> None:
        self.settings = settings

    def __enter__(self) -> FakeSynologyPhotosClient:
        return self

    def __exit__(self, *args) -> None:
        return None

    def get_or_create_album(self, album_name: str) -> int:
        if album_name not in self.created_albums:
            self.created_albums[album_name] = 100 + len(self.created_albums)
        return self.created_albums[album_name]

    def resolve_item_id(self, dest_rel_path: str) -> int | None:
        return self.indexed_paths.get(dest_rel_path)

    def add_items_to_album(self, album_id: int, item_ids: list[int]) -> None:
        self.album_items.setdefault(album_id, []).extend(item_ids)


def _create_takeout_zip(zip_path: Path) -> None:
    photo_bytes = b"\xff\xd8\xff\xe0MOCK_JPEG_SHARED_PHOTO_DATA"
    unique_bytes = b"\xff\xd8\xff\xe0MOCK_JPEG_UNIQUE_ALBUM_PHOTO"

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # 1. Photo in default yearly folder
        zf.writestr("Takeout/Google Photos/Photos from 2024/IMG_0001.JPG", photo_bytes)
        zf.writestr(
            "Takeout/Google Photos/Photos from 2024/IMG_0001.JPG.supplemental-metadata.json",
            json.dumps(
                {
                    "title": "IMG_0001.JPG",
                    "photoTakenTime": {"timestamp": "1720000000"},  # 2024-07-03 UTC
                }
            ),
        )

        # 2. Same exact photo duplicated in a user Album (Takeout always duplicates album photos!)
        zf.writestr("Takeout/Google Photos/Summer 2024/metadata.json", json.dumps({"title": "Summer 2024"}))
        zf.writestr("Takeout/Google Photos/Summer 2024/IMG_0001.JPG", photo_bytes)
        zf.writestr(
            "Takeout/Google Photos/Summer 2024/IMG_0001.JPG.json",
            json.dumps(
                {
                    "title": "IMG_0001.JPG",
                    "photoTakenTime": {"timestamp": "1720000000"},
                }
            ),
        )

        # 3. Second unique photo only in the album
        zf.writestr("Takeout/Google Photos/Summer 2024/IMG_0002.JPG", unique_bytes)
        zf.writestr(
            "Takeout/Google Photos/Summer 2024/IMG_0002.JPG.json",
            json.dumps(
                {
                    "title": "IMG_0002.JPG",
                    "photoTakenTime": {"timestamp": "1722500000"},  # 2024-08-01 UTC
                }
            ),
        )


def test_full_pipeline_dedup_and_synology_album_sync(tmp_path: Path) -> None:
    FakeSynologyPhotosClient.created_albums = {}
    FakeSynologyPhotosClient.album_items = {}
    FakeSynologyPhotosClient.indexed_paths = {
        "2024/07/IMG_0001.JPG": 5001,
        "2024/08/IMG_0002.JPG": 5002,
    }

    settings = Settings(
        data_dir=tmp_path / "config",
        work_dir=tmp_path / "work",
        inbox_dir=tmp_path / "inbox",
        photos_dir=tmp_path / "photos",
        inbox_stability_seconds=0,
        min_free_disk_gb=0.1,
        write_exif_metadata=False,
        synology_api_enabled=True,
        synology_username="tester",
        synology_password="secret",
    )
    cfg = ConfigManager(settings)
    db = StateDB(cfg.settings.db_path)

    zip_file = cfg.settings.inbox_dir / "takeout-20261121T000000Z-001.zip"
    _create_takeout_zip(zip_file)

    pipeline = MigrationPipeline(
        config_manager=cfg,
        db=db,
        synology_client_factory=FakeSynologyPhotosClient,
    )
    summary = pipeline.run_cycle()

    assert summary["inbox_processed"] == 1
    assert summary["errors"] == 0
    assert summary["albums_synced"] == 2

    # Archive moved to inbox/processed
    assert not zip_file.exists()
    assert (cfg.settings.inbox_dir / "processed" / zip_file.name).exists()

    # Exactly 2 deduplicated files imported into YYYY/MM structure
    assert (cfg.settings.photos_dir / "2024/07/IMG_0001.JPG").exists()
    assert (cfg.settings.photos_dir / "2024/08/IMG_0002.JPG").exists()

    stats = db.get_stats()
    assert stats["total_media"] == 2
    assert stats["total_duplicates_skipped"] == 1
    assert stats["total_albums"] == 1
    assert stats["synced_album_items"] == 2
    assert stats["pending_album_items"] == 0

    # Both photos (including the deduplicated IMG_0001.JPG) attached to 'Summer 2024' album
    album_id = FakeSynologyPhotosClient.created_albums["Summer 2024"]
    assert sorted(FakeSynologyPhotosClient.album_items[album_id]) == [5001, 5002]

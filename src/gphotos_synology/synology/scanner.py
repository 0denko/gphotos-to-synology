"""Incremental scanner for indexing existing photos already stored on Synology DSM."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from gphotos_synology.config import Settings
from gphotos_synology.database import StateDB
from gphotos_synology.takeout.dedup import compute_media_fingerprint
from gphotos_synology.takeout.matcher import is_media_file

logger = logging.getLogger(__name__)

# Synology DSM and OS metadata directories that MUST NEVER be indexed as real photos
IGNORED_DIR_NAMES: set[str] = {
    "@eaDir",
    "@tmp",
    "@SynoResource",
    "#recycle",
    "#snapshot",
    ".Thumbnails",
    ".Trashes",
    ".Spotlight-V100",
    ".fseventsd",
    "__MACOSX",
    "_replaced_duplicates",
}


@dataclass
class LibraryScanResult:
    scanned: int = 0
    newly_indexed: int = 0
    unchanged_skipped: int = 0
    pruned_missing: int = 0
    errors: int = 0


class ExistingLibraryScanner:
    """Indexes existing Synology Photos media so migrations never create duplicates."""

    def __init__(self, settings: Settings, db: StateDB) -> None:
        self.settings = settings
        self.db = db

    def scan(
        self,
        *,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> LibraryScanResult:
        """Incrementally index all existing media files under PHOTOS_DIR."""
        photos_dir = self.settings.photos_dir
        photos_dir.mkdir(parents=True, exist_ok=True)

        result = LibraryScanResult()
        indexed_by_path = self.db.get_indexed_files_by_rel_path()
        compute_visual = self.settings.dedup_mode != "strict_hash"

        # 1. Discover media files while pruning Synology system directories in-place
        discovered: list[tuple[Path, str]] = []
        for root, dirs, files in os.walk(photos_dir):
            dirs[:] = [
                d for d in dirs if d not in IGNORED_DIR_NAMES and not d.startswith(".")
            ]
            root_path = Path(root)
            for filename in sorted(files):
                full_path = root_path / filename
                if not is_media_file(full_path):
                    continue
                rel_posix = full_path.relative_to(photos_dir).as_posix()
                discovered.append((full_path, rel_posix))

        total = len(discovered)
        present_paths: set[str] = set()

        for idx, (full_path, rel_posix) in enumerate(discovered, start=1):
            present_paths.add(rel_posix)
            result.scanned += 1
            try:
                stat = full_path.stat()
                existing_meta = indexed_by_path.get(rel_posix)
                if (
                    existing_meta is not None
                    and existing_meta["file_size"] == stat.st_size
                    and existing_meta["file_mtime_ns"] == stat.st_mtime_ns
                    and (not compute_visual or existing_meta["has_visual"] or not self._is_image(full_path))
                ):
                    result.unchanged_skipped += 1
                    continue

                fp = compute_media_fingerprint(full_path, compute_visual=compute_visual)
                self.db.upsert_existing_nas_media(
                    sha256=fp.sha256,
                    pixel_sha256=fp.pixel_sha256,
                    dhash=fp.dhash,
                    width=fp.width,
                    height=fp.height,
                    sharpness=fp.sharpness,
                    quality_score=fp.quality_score,
                    filename=full_path.name,
                    dest_rel_path=rel_posix,
                    file_size=stat.st_size,
                    file_mtime_ns=stat.st_mtime_ns,
                    taken_at=fp.exif_taken_at,
                )
                result.newly_indexed += 1
            except Exception as exc:
                result.errors += 1
                logger.warning("Failed indexing existing NAS file %s: %s", rel_posix, exc)
            finally:
                if on_progress and (idx % 25 == 0 or idx == total):
                    on_progress(idx, total)

        # Remove stale records for existing-NAS files that were deleted from disk
        result.pruned_missing = self.db.prune_missing_existing_nas_media(present_paths)
        return result

    @staticmethod
    def _is_image(path: Path) -> bool:
        from gphotos_synology.takeout.matcher import is_video_file

        return not is_video_file(path)

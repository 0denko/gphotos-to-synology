"""Direct folder importer into Synology Photos with atomic moves and collision safety."""

from __future__ import annotations

import contextlib
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from gphotos_synology.config import Settings
from gphotos_synology.takeout.extractor import safe_move_or_copy
from gphotos_synology.takeout.metadata import PhotoMetadata

UNSAFE_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_component(name: str, fallback: str = "Untitled") -> str:
    cleaned = UNSAFE_CHARS_RE.sub("_", name).strip(" .")
    return cleaned[:180] if cleaned else fallback


class SynologyFolderImporter:
    """Places EXIF-repaired media into the mounted Synology Photos directory."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _resolve_date(self, media_path: Path, metadata: PhotoMetadata) -> datetime | None:
        if metadata.taken_at is not None:
            return metadata.taken_at
        try:
            mtime = media_path.stat().st_mtime
            if mtime > 0:
                return datetime.fromtimestamp(mtime, tz=UTC)
        except OSError:
            pass
        return None

    def compute_target_directory(self, media_path: Path, metadata: PhotoMetadata) -> Path:
        """Compute target subfolder relative to PHOTOS_DIR based on folder_template."""
        template = self.settings.folder_template
        dt = self._resolve_date(media_path, metadata)

        if template == "album" and metadata.album_name:
            return Path("Albums") / sanitize_component(metadata.album_name, fallback="Album")

        if dt is None:
            return Path("Unknown")

        year = f"{dt.year:04d}"
        month = f"{dt.month:02d}"
        if template == "year_ym":
            return Path(year) / f"{year}-{month}"
        return Path(year) / month

    def _unique_destination(self, target_dir: Path, filename: str) -> Path:
        safe_name = sanitize_component(filename, fallback="media")
        candidate = target_dir / safe_name
        if not candidate.exists():
            return candidate

        stem = Path(safe_name).stem
        suffix = Path(safe_name).suffix
        counter = 2
        while True:
            candidate = target_dir / f"{stem}_{counter}{suffix}"
            if not candidate.exists():
                return candidate
            counter += 1

    def import_media(self, staged_path: Path, metadata: PhotoMetadata) -> tuple[Path, str]:
        """Move `staged_path` into PHOTOS_DIR and return (absolute_dest, relative_posix_path)."""
        rel_dir = self.compute_target_directory(staged_path, metadata)
        abs_dir = self.settings.photos_dir / rel_dir
        abs_dir.mkdir(parents=True, exist_ok=True)

        dest_path = self._unique_destination(abs_dir, staged_path.name)
        safe_move_or_copy(staged_path, dest_path)

        with contextlib.suppress(OSError):
            os.chmod(dest_path, 0o644)

        rel_posix = dest_path.relative_to(self.settings.photos_dir).as_posix()
        return dest_path, rel_posix

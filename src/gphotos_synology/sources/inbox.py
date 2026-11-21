"""Watched local/NAS inbox scanner with file-size stability checks."""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from gphotos_synology.config import Settings
from gphotos_synology.takeout.extractor import extract_batch_key, is_supported_archive


@dataclass
class InboxArchiveItem:
    """Represents a ready-to-process Takeout archive discovered in INBOX_DIR."""

    path: Path
    source_id: str
    name: str
    size_bytes: int
    batch_key: str


class LocalInboxScanner:
    """Scans a mounted NAS directory for Takeout archives (manual or Synology Cloud Sync)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._seen_sizes: dict[str, tuple[int, float]] = {}

    def _fingerprint(self, path: Path, size: int, mtime_ns: int) -> str:
        raw = f"{path.name}:{size}:{mtime_ns}".encode()
        return hashlib.sha256(raw).hexdigest()[:24]

    def scan_ready_archives(self) -> list[InboxArchiveItem]:
        """Return archives in INBOX_DIR whose size has been stable for `inbox_stability_seconds`."""
        inbox = self.settings.inbox_dir
        inbox.mkdir(parents=True, exist_ok=True)

        now = time.monotonic()
        ready: list[InboxArchiveItem] = []
        active_keys: set[str] = set()

        for entry in sorted(inbox.iterdir(), key=lambda p: p.name.lower()):
            if not entry.is_file():
                continue
            if entry.name.startswith(".") or entry.name.endswith(".part"):
                continue
            if not is_supported_archive(entry.name):
                continue

            try:
                stat = entry.stat()
            except OSError:
                continue

            key = str(entry.resolve())
            active_keys.add(key)

            if self.settings.inbox_stability_seconds > 0:
                prev = self._seen_sizes.get(key)
                if prev is None or prev[0] != stat.st_size:
                    self._seen_sizes[key] = (stat.st_size, now)
                    continue
                if (now - prev[1]) < self.settings.inbox_stability_seconds:
                    continue

            ready.append(
                InboxArchiveItem(
                    path=entry,
                    source_id=self._fingerprint(entry, stat.st_size, stat.st_mtime_ns),
                    name=entry.name,
                    size_bytes=stat.st_size,
                    batch_key=extract_batch_key(entry.name),
                )
            )

        # Clean up tracking entries for files no longer in inbox
        for stale in list(self._seen_sizes.keys()):
            if stale not in active_keys:
                self._seen_sizes.pop(stale, None)

        return ready

    def finalize_archive(self, archive_path: Path) -> None:
        """Apply configured post-processing action after successful archive ingestion."""
        action = self.settings.inbox_post_action
        if action == "keep":
            return
        if action == "delete":
            archive_path.unlink(missing_ok=True)
            return
        if action == "move_to_processed":
            processed_dir = self.settings.inbox_dir / "processed"
            processed_dir.mkdir(parents=True, exist_ok=True)
            dest = processed_dir / archive_path.name
            if dest.exists():
                stem = archive_path.stem
                suffix = archive_path.suffix
                counter = 1
                while dest.exists():
                    dest = processed_dir / f"{stem}_{counter}{suffix}"
                    counter += 1
            try:
                os.replace(archive_path, dest)
            except OSError:
                shutil.move(str(archive_path), str(dest))

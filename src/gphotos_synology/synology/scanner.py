"""Incremental scanner for indexing existing photos already stored on Synology DSM."""

from __future__ import annotations

import concurrent.futures
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from gphotos_synology.config import Settings
from gphotos_synology.database import StateDB
from gphotos_synology.takeout.dedup import MediaFingerprint, compute_media_fingerprint
from gphotos_synology.takeout.matcher import is_media_file

logger = logging.getLogger(__name__)

# Hard politeness ceiling for background NAS indexing workers
MAX_INDEX_WORKERS = 4
_WORKER_NICE_INCREMENT = 10
_LOAD_CHECK_INTERVAL_SECONDS = 2.0

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


def _lower_worker_priority() -> None:
    """Run indexer worker threads at reduced scheduling priority on Linux/POSIX."""
    if hasattr(os, "nice"):
        try:
            current = os.nice(0)
            delta = _WORKER_NICE_INCREMENT - current
            if delta > 0:
                os.nice(delta)
        except OSError:
            pass


def _compute_fingerprint_worker(path: Path, compute_visual: bool) -> MediaFingerprint:
    return compute_media_fingerprint(path, compute_visual=compute_visual)


def _get_system_load_1m() -> float | None:
    """Return 1-minute system load average if available on this platform."""
    if hasattr(os, "getloadavg"):
        try:
            return float(os.getloadavg()[0])
        except OSError:
            return None
    return None


@dataclass
class _PendingWorkItem:
    full_path: Path
    rel_posix: str
    file_size: int
    file_mtime_ns: int


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

    def _effective_max_workers(self) -> int:
        configured = getattr(self.settings, "index_workers", 2)
        return max(1, min(int(configured), MAX_INDEX_WORKERS))

    def _compute_active_limit(
        self,
        max_workers: int,
        current_limit: int,
        cpu_count: int,
    ) -> tuple[int, float]:
        """Load-aware politeness throttle: shed concurrency to 1 worker when NAS load is high.

        Returns `(active_worker_limit, optional_yield_sleep_seconds)`.
        """
        if max_workers <= 1:
            return 1, 0.0
        load1 = _get_system_load_1m()
        if load1 is None:
            return max_workers, 0.0
        high_threshold = max(1.0, cpu_count * 0.70)
        critical_threshold = max(1.5, cpu_count * 0.90)
        recover_threshold = high_threshold * 0.85

        if load1 >= critical_threshold:
            return 1, 0.05
        if load1 >= high_threshold:
            return 1, 0.0
        if current_limit < max_workers and load1 >= recover_threshold:
            return current_limit, 0.0
        return max_workers, 0.0

    def scan(
        self,
        *,
        on_progress: Callable[[int, int], None] | None = None,
        on_telemetry: Callable[[int, int, bool], None] | None = None,
    ) -> LibraryScanResult:
        """Incrementally index all existing media files under PHOTOS_DIR.

        Uses a bounded, low-priority (`nice +10`) ThreadPoolExecutor for CPU-bound
        hashing/decoding (Pillow C codecs & hashlib release the GIL) while keeping
        pre-dispatch skip checks, single-writer SQLite upserts, and monotonic progress
        strictly on the main thread.
        """
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
        if total == 0:
            result.pruned_missing = self.db.prune_missing_existing_nas_media(present_paths)
            return result

        max_workers = self._effective_max_workers()
        cpu_count = os.cpu_count() or 1
        active_limit = max_workers
        yield_sleep = 0.0
        last_load_check = 0.0
        completed = 0

        def _emit_telemetry(active: int, throttled: bool) -> None:
            if on_telemetry is not None:
                on_telemetry(active, max_workers, throttled)

        def _emit_progress() -> None:
            if on_progress and (completed % 25 == 0 or completed == total):
                on_progress(completed, total)

        def _commit_future(
            fut: concurrent.futures.Future[MediaFingerprint],
            item: _PendingWorkItem,
        ) -> None:
            nonlocal completed
            try:
                fp = fut.result()
                self.db.upsert_existing_nas_media(
                    sha256=fp.sha256,
                    pixel_sha256=fp.pixel_sha256,
                    dhash=fp.dhash,
                    width=fp.width,
                    height=fp.height,
                    sharpness=fp.sharpness,
                    quality_score=fp.quality_score,
                    filename=item.full_path.name,
                    dest_rel_path=item.rel_posix,
                    file_size=item.file_size,
                    file_mtime_ns=item.file_mtime_ns,
                    taken_at=fp.exif_taken_at,
                    visual_status=fp.visual_status,
                )
                result.newly_indexed += 1
            except Exception as exc:
                result.errors += 1
                logger.warning("Failed indexing existing NAS file %s: %s", item.rel_posix, exc)
            finally:
                completed += 1
                _emit_progress()

        pending: dict[concurrent.futures.Future[MediaFingerprint], _PendingWorkItem] = {}

        def _drain_some(target_max_pending: int) -> None:
            while len(pending) > target_max_pending:
                done, _ = concurrent.futures.wait(
                    pending.keys(), return_when=concurrent.futures.FIRST_COMPLETED
                )
                for fut in done:
                    item = pending.pop(fut)
                    _commit_future(fut, item)

        _emit_telemetry(active_limit, False)
        try:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix="nas-indexer",
                initializer=_lower_worker_priority,
            ) as pool:
                for full_path, rel_posix in discovered:
                    present_paths.add(rel_posix)
                    result.scanned += 1
                    try:
                        stat = full_path.stat()
                    except Exception as exc:
                        result.errors += 1
                        logger.warning("Failed indexing existing NAS file %s: %s", rel_posix, exc)
                        completed += 1
                        _emit_progress()
                        continue

                    # Pre-dispatch skip check on the main thread (never submits unchanged files)
                    existing_meta = indexed_by_path.get(rel_posix)
                    visual_stable = (
                        existing_meta is not None
                        and (
                            existing_meta["has_visual"]
                            or existing_meta.get("visual_status") == "undecodable"
                        )
                    )
                    if (
                        existing_meta is not None
                        and existing_meta["file_size"] == stat.st_size
                        and existing_meta["file_mtime_ns"] == stat.st_mtime_ns
                        and (not compute_visual or visual_stable or not self._is_image(full_path))
                    ):
                        result.unchanged_skipped += 1
                        completed += 1
                        _emit_progress()
                        continue

                    # Periodically refresh load-aware politeness throttle
                    now = time.monotonic()
                    if max_workers > 1 and (now - last_load_check) >= _LOAD_CHECK_INTERVAL_SECONDS:
                        new_limit, new_sleep = self._compute_active_limit(
                            max_workers, active_limit, cpu_count
                        )
                        if new_limit != active_limit or (new_sleep > 0) != (yield_sleep > 0):
                            _emit_telemetry(
                                new_limit,
                                new_limit < max_workers or new_sleep > 0,
                            )
                        active_limit, yield_sleep = new_limit, new_sleep
                        last_load_check = now

                    # Bound in-flight futures to active_limit so decoded images never pile up in RAM
                    _drain_some(max(0, active_limit - 1))
                    if yield_sleep > 0:
                        time.sleep(yield_sleep)

                    fut = pool.submit(_compute_fingerprint_worker, full_path, compute_visual)
                    pending[fut] = _PendingWorkItem(
                        full_path=full_path,
                        rel_posix=rel_posix,
                        file_size=stat.st_size,
                        file_mtime_ns=stat.st_mtime_ns,
                    )

                # Drain all remaining in-flight work before pruning
                _drain_some(0)
        finally:
            _emit_telemetry(0, False)

        # Remove stale records for existing-NAS files that were deleted from disk
        result.pruned_missing = self.db.prune_missing_existing_nas_media(present_paths)
        return result

    @staticmethod
    def _is_image(path: Path) -> bool:
        from gphotos_synology.takeout.matcher import is_video_file

        return not is_video_file(path)

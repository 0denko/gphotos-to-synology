"""End-to-end Takeout migration pipeline: fetch -> extract -> repair -> dedup -> import -> album sync."""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from gphotos_synology.config import ConfigManager
from gphotos_synology.database import StateDB, utc_now_iso
from gphotos_synology.notifications import Notifier
from gphotos_synology.sources.gdrive import DriveArchiveItem, GoogleDriveClient
from gphotos_synology.sources.inbox import InboxArchiveItem, LocalInboxScanner
from gphotos_synology.synology.importer import SynologyFolderImporter
from gphotos_synology.synology.photos_api import SynologyPhotosClient
from gphotos_synology.synology.scanner import ExistingLibraryScanner
from gphotos_synology.takeout.dedup import (
    ImageQualityMetrics,
    compare_image_quality,
    compute_media_fingerprint,
)
from gphotos_synology.takeout.extractor import (
    cleanup_directory,
    compute_file_sha256,
    extract_archive,
    get_free_disk_gb,
    safe_move_or_copy,
)
from gphotos_synology.takeout.matcher import (
    DirectoryJsonIndex,
    detect_album_name,
    find_sidecar_json,
    is_media_file,
)
from gphotos_synology.takeout.metadata import PhotoMetadata, apply_metadata, parse_sidecar_json

logger = logging.getLogger(__name__)

_DISK_ALERT_THROTTLE_HOURS = 12


def _resolve_contained(base_dir: Path, rel_or_name: str) -> Path:
    """Resolve `base_dir / rel_or_name` and verify strict containment within `base_dir`."""
    if not rel_or_name or rel_or_name.startswith(("/", "\\")):
        raise ValueError(f"Invalid relative path: {rel_or_name!r}")
    base = base_dir.resolve()
    target = (base / rel_or_name).resolve()
    if target != base and base not in target.parents:
        raise ValueError(f"Path escapes base directory: {rel_or_name!r}")
    return target


_RATE_WINDOW_SECONDS = 120.0
_CPU_SAMPLE_MIN_INTERVAL_SECONDS = 1.0


def _process_cpu_seconds() -> float:
    t = os.times()
    return float(t.user + t.system)


@dataclass
class PipelineProgress:
    """Thread-safe snapshot of live background migration state."""

    state: str = "idle"
    current_archive: str = ""
    current_step: str = "Waiting for next poll"
    download_pct: float = 0.0
    processed_files: int = 0
    total_files: int = 0
    last_run_at: str | None = None
    next_run_at: str | None = None
    last_error: str = ""
    renewal_reminder_due: bool = False
    # Live worker telemetry (rolling rate, ETA, CPU usage, indexer politeness)
    items_per_min: float = 0.0
    eta_seconds: int | None = None
    worker_cpu_pct: float = 0.0
    worker_cpu_cores: float = 0.0
    cpu_count: int = 1
    active_index_workers: int = 0
    configured_index_workers: int = 0
    index_throttled: bool = False
    index_throttle_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MigrationPipeline:
    """Orchestrates unattended Google Takeout ingestion into Synology DSM / Synology Photos."""

    def __init__(
        self,
        config_manager: ConfigManager,
        db: StateDB,
        *,
        synology_client_factory: Callable[..., SynologyPhotosClient] | None = None,
    ) -> None:
        self.config_manager = config_manager
        self.db = db
        self._synology_client_factory = synology_client_factory or SynologyPhotosClient
        self._progress_lock = threading.Lock()
        self._op_lock = threading.RLock()
        self._cpu_count = max(1, os.cpu_count() or 1)
        self._progress = PipelineProgress(cpu_count=self._cpu_count)
        self._rate_samples: deque[tuple[float, int]] = deque()
        self._rate_phase_key: tuple[str, str] = ("idle", "")
        self._last_cpu_wall = time.monotonic()
        self._last_cpu_proc = _process_cpu_seconds()
        self._inbox_scanner = LocalInboxScanner(self.config_manager.settings)

    def _sample_cpu_locked(self, now: float) -> None:
        dt = now - self._last_cpu_wall
        if dt < _CPU_SAMPLE_MIN_INTERVAL_SECONDS:
            return
        cur_proc = _process_cpu_seconds()
        dcpu = max(0.0, cur_proc - self._last_cpu_proc)
        cores = dcpu / dt
        pct = min(100.0, max(0.0, (cores / self._cpu_count) * 100.0))
        self._progress.worker_cpu_cores = round(cores, 2)
        self._progress.worker_cpu_pct = round(pct, 1)
        self._progress.cpu_count = self._cpu_count
        self._last_cpu_wall = now
        self._last_cpu_proc = cur_proc

    def _update_rate_and_eta_locked(self, now: float) -> None:
        phase_key = (self._progress.state, self._progress.current_archive)
        processed = self._progress.processed_files
        total = self._progress.total_files

        if phase_key != self._rate_phase_key or (
            self._rate_samples and processed < self._rate_samples[-1][1]
        ):
            self._rate_samples.clear()
            self._rate_phase_key = phase_key
            self._progress.items_per_min = 0.0
            self._progress.eta_seconds = None

        if self._progress.state not in {"indexing_nas", "importing"} or total <= 0:
            self._progress.items_per_min = 0.0
            self._progress.eta_seconds = None
            return

        if not self._rate_samples or self._rate_samples[-1][1] != processed:
            self._rate_samples.append((now, processed))

        cutoff = now - _RATE_WINDOW_SECONDS
        while len(self._rate_samples) > 2 and self._rate_samples[1][0] <= cutoff:
            self._rate_samples.popleft()

        if len(self._rate_samples) >= 2:
            t0, c0 = self._rate_samples[0]
            t1, c1 = self._rate_samples[-1]
            dt = t1 - t0
            dc = c1 - c0
            if dt >= 1.0 and dc > 0:
                rate_per_sec = dc / dt
                self._progress.items_per_min = round(rate_per_sec * 60.0, 1)
                remaining = max(0, total - processed)
                self._progress.eta_seconds = round(remaining / rate_per_sec) if remaining > 0 else 0
                return

        self._progress.items_per_min = 0.0
        self._progress.eta_seconds = None

    def get_progress(self) -> dict[str, Any]:
        with self._progress_lock:
            self._sample_cpu_locked(time.monotonic())
            return self._progress.to_dict()

    def _set_progress(self, **kwargs: Any) -> None:
        with self._progress_lock:
            for key, value in kwargs.items():
                if hasattr(self._progress, key):
                    setattr(self._progress, key, value)
            now = time.monotonic()
            if (
                "state" in kwargs
                or "current_archive" in kwargs
                or "processed_files" in kwargs
                or "total_files" in kwargs
            ):
                self._update_rate_and_eta_locked(now)
            self._sample_cpu_locked(now)

    def _log(self, level: str, component: str, message: str) -> None:
        log_fn = getattr(logger, level.lower(), logger.info)
        log_fn("[%s] %s", component, message)
        self.db.log_event(level, component, message)

    def _check_disk_space(self, required_gb: float, label: str) -> bool:
        settings = self.config_manager.settings
        work_free = get_free_disk_gb(settings.work_dir)
        photos_free = get_free_disk_gb(settings.photos_dir)
        if work_free < required_gb or photos_free < settings.min_free_disk_gb:
            msg = (
                f"Low disk space before {label}: work={work_free:.2f} GB "
                f"(need {required_gb:.2f} GB), photos={photos_free:.2f} GB "
                f"(min {settings.min_free_disk_gb:.2f} GB)"
            )
            self._log("WARNING", "disk-guard", msg)
            self._send_throttled_disk_alert(msg)
            return False
        return True

    def _send_throttled_disk_alert(self, msg: str) -> None:
        now = datetime.now(UTC)
        last_raw = self.db.get_state("last_disk_alert_at")
        if last_raw:
            try:
                last_dt = datetime.fromisoformat(last_raw)
                if (now - last_dt) < timedelta(hours=_DISK_ALERT_THROTTLE_HOURS):
                    return
            except ValueError:
                pass
        Notifier(self.config_manager.settings).send_async(
            "Low Disk Space Guard Triggered", msg, level="error"
        )
        self.db.set_state("last_disk_alert_at", utc_now_iso())

    def recover_in_progress_upgrades(self) -> int:
        """Recover any quality-upgrade swaps left `in_progress` by a crash or power loss (G2)."""
        with self._op_lock:
            settings = self.config_manager.settings
            pending = self.db.list_in_progress_quality_upgrades()
            recovered = 0
            for row in pending:
                upgrade_id = int(row["id"])
                q_name = str(row.get("quarantine_filename") or "")
                old_rel = str(row.get("old_rel_path") or "")
                kept_rel = str(row.get("kept_rel_path") or "")
                try:
                    q_path = _resolve_contained(settings.quarantine_dir, q_name) if q_name else None
                    old_abs = _resolve_contained(settings.photos_dir, old_rel) if old_rel else None
                    kept_abs = _resolve_contained(settings.photos_dir, kept_rel) if kept_rel else None
                except ValueError:
                    self.db.delete_quality_upgrade(upgrade_id)
                    recovered += 1
                    continue

                if q_path and q_path.is_file() and old_abs and not old_abs.is_file() and not (
                    kept_abs and kept_abs.is_file()
                ):
                    # Original was moved to quarantine but incoming never landed — roll back safely
                    safe_move_or_copy(q_path, old_abs)
                    self.db.delete_quality_upgrade(upgrade_id)
                    self._log(
                        "WARNING",
                        "quality-upgrade",
                        f"Recovered interrupted upgrade #{upgrade_id}: restored {old_rel} from quarantine",
                    )
                    recovered += 1
                elif kept_abs and kept_abs.is_file() and q_path and q_path.is_file():
                    self.db.finalize_quality_upgrade(upgrade_id, kept_rel)
                    recovered += 1
                else:
                    self.db.delete_quality_upgrade(upgrade_id)
                    recovered += 1
            return recovered

    def index_existing_library(self) -> dict[str, int]:
        """Incrementally index existing photos on Synology DSM so migrations never create duplicates."""
        settings = self.config_manager.settings
        self._set_progress(
            state="indexing_nas",
            current_archive="",
            current_step="Indexing existing Synology Photos library for deduplication",
            processed_files=0,
            total_files=0,
        )
        scanner = ExistingLibraryScanner(settings, self.db)
        scan_res = scanner.scan(
            on_progress=lambda done, total: self._set_progress(
                processed_files=done,
                total_files=total,
                current_step=f"Indexing existing NAS photos ({done}/{total})",
            ),
            on_telemetry=lambda active, configured, throttled, reason: self._set_progress(
                active_index_workers=active,
                configured_index_workers=configured,
                index_throttled=throttled,
                index_throttle_reason=reason,
            ),
        )
        if scan_res.newly_indexed > 0 or scan_res.pruned_missing > 0:
            self._log(
                "INFO",
                "nas-index",
                (
                    f"Indexed existing NAS library: {scan_res.newly_indexed} newly indexed, "
                    f"{scan_res.unchanged_skipped} unchanged, {scan_res.pruned_missing} pruned"
                ),
            )
        return {
            "scanned": scan_res.scanned,
            "newly_indexed": scan_res.newly_indexed,
            "unchanged_skipped": scan_res.unchanged_skipped,
            "pruned_missing": scan_res.pruned_missing,
            "errors": scan_res.errors,
        }

    def run_cycle(self) -> dict[str, Any]:
        """Execute one complete indexing, polling, ingestion, and album-sync cycle."""
        with self._op_lock:
            settings = self.config_manager.settings
            self._inbox_scanner.settings = settings
            summary = {
                "existing_newly_indexed": 0,
                "inbox_processed": 0,
                "gdrive_processed": 0,
                "albums_synced": 0,
                "errors": 0,
            }

            self._set_progress(
                state="scanning",
                current_archive="",
                current_step="Scanning existing NAS library, inbox, and Google Drive",
                download_pct=0.0,
                processed_files=0,
                total_files=0,
                last_error="",
            )

            try:
                # Recover any crash-interrupted quality upgrade swaps before ingesting
                self.recover_in_progress_upgrades()

                # 0. Incrementally index existing NAS photos (isolated so a bad dir doesn't abort cycle)
                if settings.auto_index_existing_photos:
                    try:
                        index_stats = self.index_existing_library()
                        summary["existing_newly_indexed"] = index_stats["newly_indexed"]
                        summary["errors"] += index_stats["errors"]
                    except Exception as exc:
                        err_msg = f"Existing NAS library indexing failed: {exc}"
                        self._log("ERROR", "nas-index", err_msg)
                        self._set_progress(last_error=err_msg)
                        summary["errors"] += 1

                # 1. Process local watched inbox archives
                ready_inbox = self._inbox_scanner.scan_ready_archives()
                for item in ready_inbox:
                    existing = self.db.get_archive_by_source("inbox", item.source_id)
                    if existing and existing["status"] == "completed":
                        continue
                    ok = self._process_inbox_archive(item)
                    if ok:
                        summary["inbox_processed"] += 1
                    else:
                        summary["errors"] += 1

                # 2. Poll Google Drive if connected
                gdrive = GoogleDriveClient(settings)
                if gdrive.is_connected():
                    self._set_progress(state="scanning", current_step="Checking Google Drive Takeout folder")
                    try:
                        drive_items = gdrive.list_takeout_archives()
                    except Exception as exc:
                        err_msg = f"Google Drive poll failed: {exc}"
                        self._log("ERROR", "gdrive", err_msg)
                        self._set_progress(last_error=err_msg)
                        Notifier(settings).send_async("Google Drive Poll Error", err_msg, level="error")
                        summary["errors"] += 1
                        drive_items = []

                    for item in drive_items:
                        existing = self.db.get_archive_by_source("gdrive", item.file_id)
                        if existing and existing["status"] == "completed":
                            continue
                        ok = self._process_gdrive_archive(gdrive, item)
                        if ok:
                            summary["gdrive_processed"] += 1
                        else:
                            summary["errors"] += 1

                # 3. Optional Synology Photos DSM 7 API album sync
                if settings.synology_api_enabled:
                    synced_count = self.sync_pending_albums()
                    summary["albums_synced"] = synced_count

                # 4. Annual Google Takeout renewal check
                self._check_renewal_reminder()

                self._set_progress(
                    state="idle",
                    current_archive="",
                    current_step="Idle — waiting for next scheduled poll",
                    last_run_at=utc_now_iso(),
                )
            except Exception as exc:
                err_msg = f"Unexpected pipeline failure: {exc}"
                self._log("ERROR", "pipeline", err_msg)
                self._set_progress(state="error", current_step=err_msg, last_error=err_msg)
                summary["errors"] += 1

            return summary

    def _process_inbox_archive(self, item: InboxArchiveItem) -> bool:
        settings = self.config_manager.settings
        needed_gb = max(settings.min_free_disk_gb, (item.size_bytes / (1024**3)) * 1.5)
        if not self._check_disk_space(needed_gb, item.name):
            return False

        archive_id = self.db.upsert_archive(
            source="inbox",
            source_id=item.source_id,
            filename=item.name,
            batch_key=item.batch_key,
            size_bytes=item.size_bytes,
            status="processing",
        )
        self._log("INFO", "inbox", f"Processing local archive: {item.name}")

        work_subdir = settings.work_dir / f"archive_{archive_id}"
        try:
            stats = self._ingest_archive_file(archive_id, item.path, work_subdir)
            # G1: Never finalize/delete source archive when any file failed
            if stats["failed"] == 0:
                self._inbox_scanner.finalize_archive(item.path)
            else:
                self._log(
                    "WARNING",
                    "inbox",
                    (
                        f"Keeping source archive {item.name} in inbox for retry because "
                        f"{stats['failed']} file(s) failed to import"
                    ),
                )
            self._record_first_export_timestamp()
            self._notify_archive_done(item.name, stats)
            return stats["failed"] == 0
        except Exception as exc:
            err = str(exc)
            self.db.update_archive_progress(archive_id, status="failed", error_message=err)
            self._log("ERROR", "inbox", f"Failed processing {item.name}: {err}")
            self._set_progress(last_error=err)
            Notifier(settings).send_async(f"Migration Failed: {item.name}", err, level="error")
            return False
        finally:
            cleanup_directory(work_subdir)

    def _process_gdrive_archive(self, gdrive: GoogleDriveClient, item: DriveArchiveItem) -> bool:
        settings = self.config_manager.settings
        archive_gb = item.size_bytes / (1024**3)
        needed_gb = max(settings.min_free_disk_gb, archive_gb * 2.2)
        if not self._check_disk_space(needed_gb, item.name):
            return False

        archive_id = self.db.upsert_archive(
            source="gdrive",
            source_id=item.file_id,
            filename=item.name,
            batch_key=item.batch_key,
            size_bytes=item.size_bytes,
            checksum=item.md5_checksum,
            status="downloading",
        )
        self._log("INFO", "gdrive", f"Downloading {item.name} ({archive_gb:.2f} GB) from Google Drive")

        work_subdir = settings.work_dir / f"archive_{archive_id}"
        downloaded_path = work_subdir / item.name
        try:
            self._set_progress(
                state="downloading",
                current_archive=item.name,
                current_step=f"Downloading {item.name} from Google Drive",
                download_pct=0.0,
            )
            gdrive.download_archive(
                item,
                downloaded_path,
                on_progress=lambda pct: self._set_progress(download_pct=round(pct, 1)),
            )

            self.db.update_archive_progress(archive_id, status="processing")
            stats = self._ingest_archive_file(archive_id, downloaded_path, work_subdir / "extracted")
            self._record_first_export_timestamp()

            # Optional reversible Drive cleanup only when zero files failed
            if settings.gdrive_post_action == "trash" and stats["failed"] == 0:
                try:
                    gdrive.trash_archive(item.file_id)
                    self._log("INFO", "gdrive", f"Moved verified archive {item.name} to Google Drive Trash")
                except Exception as exc:
                    self._log("WARNING", "gdrive", f"Could not trash {item.name} in Drive: {exc}")

            self._notify_archive_done(item.name, stats)
            return stats["failed"] == 0
        except Exception as exc:
            err = str(exc)
            self.db.update_archive_progress(archive_id, status="failed", error_message=err)
            self._log("ERROR", "gdrive", f"Failed processing Drive archive {item.name}: {err}")
            self._set_progress(last_error=err)
            Notifier(settings).send_async(f"Drive Migration Failed: {item.name}", err, level="error")
            return False
        finally:
            cleanup_directory(work_subdir)

    def _compute_extraction_byte_cap(self) -> int | None:
        settings = self.config_manager.settings
        free_gb = get_free_disk_gb(settings.work_dir)
        usable_gb = free_gb - settings.min_free_disk_gb
        if usable_gb <= 0:
            # Already passed disk guard (or tiny test disk); allow a small safe cap
            return max(256 * 1024 * 1024, int(free_gb * 0.5 * (1024**3)))
        return int(usable_gb * (1024**3))

    def _ingest_archive_file(
        self,
        archive_id: int,
        archive_path: Path,
        extract_dir: Path,
    ) -> dict[str, int]:
        """Extract an archive, repair EXIF metadata, deduplicate, and import to Synology Photos."""
        settings = self.config_manager.settings
        importer = SynologyFolderImporter(settings)

        self._set_progress(
            state="extracting",
            current_archive=archive_path.name,
            current_step=f"Extracting {archive_path.name}",
            download_pct=100.0,
            processed_files=0,
            total_files=0,
        )
        extract_archive(
            archive_path,
            extract_dir,
            max_uncompressed_bytes=self._compute_extraction_byte_cap(),
            skip_unsafe_entries=True,
        )

        # Discover all media files grouped by directory so we build each DirectoryJsonIndex once
        media_by_dir: dict[Path, list[Path]] = {}
        total_files = 0
        for root, _dirs, files in os.walk(extract_dir):
            root_path = Path(root)
            dir_media = [
                root_path / name
                for name in sorted(files)
                if is_media_file(root_path / name)
            ]
            if dir_media:
                media_by_dir[root_path] = dir_media
                total_files += len(dir_media)

        self.db.update_archive_progress(archive_id, total_files=total_files)
        self._set_progress(
            state="importing",
            current_archive=archive_path.name,
            current_step=f"Importing {total_files} media items from {archive_path.name}",
            total_files=total_files,
            processed_files=0,
        )

        imported = 0
        duplicates = 0
        failed = 0
        processed = 0

        compute_visual = settings.dedup_mode != "strict_hash"

        for directory, media_files in media_by_dir.items():
            album_name = detect_album_name(directory)
            json_index = DirectoryJsonIndex.build(directory)

            for media_path in media_files:
                processed += 1
                try:
                    # Parse sidecar first so timestamp is available for perceptual dedup guardrails
                    sidecar = find_sidecar_json(media_path, index=json_index)
                    if sidecar is not None:
                        try:
                            metadata = parse_sidecar_json(sidecar, album_name=album_name)
                        except Exception as exc:
                            self._log(
                                "WARNING",
                                "metadata",
                                f"Invalid sidecar JSON {sidecar.name} for {media_path.name}: {exc}",
                            )
                            metadata = PhotoMetadata(album_name=album_name)
                    else:
                        metadata = PhotoMetadata(album_name=album_name)

                    fp = compute_media_fingerprint(media_path, compute_visual=compute_visual)
                    taken_at_iso = (
                        metadata.taken_at.isoformat()
                        if metadata.taken_at is not None
                        else fp.exif_taken_at
                    )

                    existing, match_reason = self.db.find_duplicate_media(
                        sha256=fp.sha256,
                        pixel_sha256=fp.pixel_sha256,
                        dhash=fp.dhash,
                        width=fp.width,
                        height=fp.height,
                        taken_at=taken_at_iso,
                        filename=media_path.name,
                        dedup_mode=settings.dedup_mode,
                        perceptual_max_distance=settings.perceptual_max_distance,
                    )
                    if existing is not None:
                        # G2: If the DB points to a missing file on disk, repair it instead of skipping forever
                        existing_rel = str(existing["dest_rel_path"])
                        existing_abs = settings.photos_dir / existing_rel
                        if not existing_abs.is_file():
                            self._repair_missing_existing_media(
                                existing=existing,
                                incoming_path=media_path,
                                incoming_fp=fp,
                                metadata=metadata,
                                taken_at_iso=taken_at_iso,
                                importer=importer,
                            )
                            if album_name:
                                self.db.add_media_to_album(album_name, int(existing["id"]))
                            imported += 1
                            continue

                        upgraded = False
                        if (
                            match_reason in ("pixel_sha256", "perceptual_dhash")
                            and settings.duplicate_quality_policy == "auto_upgrade_quarantine"
                        ):
                            upgraded = self._maybe_upgrade_duplicate(
                                existing=existing,
                                incoming_path=media_path,
                                incoming_fp=fp,
                                metadata=metadata,
                                taken_at_iso=taken_at_iso,
                                match_reason=match_reason,
                                importer=importer,
                            )

                        if album_name:
                            self.db.add_media_to_album(album_name, int(existing["id"]))

                        if upgraded:
                            imported += 1
                        else:
                            duplicates += 1
                            if match_reason == "perceptual_dhash":
                                self._log(
                                    "INFO",
                                    "perceptual-dedup",
                                    f"Perceptual duplicate skipped: {media_path.name} matched {existing_rel}",
                                )
                        continue

                    exif_status = apply_metadata(
                        media_path,
                        metadata,
                        write_exif=settings.write_exif_metadata,
                        write_album_tags=settings.write_album_tags,
                        exiftool_path=settings.exiftool_path,
                    )

                    final_hash = compute_file_sha256(media_path)
                    # Secondary check in case an existing NAS copy was already EXIF-repaired
                    if final_hash != fp.sha256:
                        post_existing = self.db.get_media_by_original_hash(final_hash)
                        if post_existing is not None:
                            duplicates += 1
                            if album_name:
                                self.db.add_media_to_album(album_name, int(post_existing["id"]))
                            continue

                    dest_abs, dest_rel = importer.import_media(media_path, metadata)
                    dest_stat = dest_abs.stat()

                    media_id = self.db.record_media_item(
                        original_sha256=fp.sha256,
                        final_sha256=final_hash,
                        pixel_sha256=fp.pixel_sha256,
                        dhash=fp.dhash,
                        width=fp.width,
                        height=fp.height,
                        sharpness=fp.sharpness,
                        quality_score=fp.quality_score,
                        original_filename=media_path.name,
                        dest_rel_path=dest_rel,
                        file_size=dest_stat.st_size,
                        file_mtime_ns=dest_stat.st_mtime_ns,
                        taken_at=taken_at_iso,
                        latitude=metadata.latitude,
                        longitude=metadata.longitude,
                        exif_status=exif_status,
                        source_archive_id=archive_id,
                        visual_status=fp.visual_status,
                    )
                    if album_name:
                        self.db.add_media_to_album(album_name, media_id)
                    imported += 1
                except Exception as exc:
                    failed += 1
                    self._log("ERROR", "importer", f"Failed importing {media_path.name}: {exc}")
                finally:
                    if processed % 10 == 0 or processed == total_files:
                        self._set_progress(processed_files=processed)
                        self.db.update_archive_progress(
                            archive_id,
                            imported_files=imported,
                            duplicate_files=duplicates,
                            failed_files=failed,
                        )

        # G1: Only mark 'completed' when zero files failed; otherwise retryable 'completed_with_errors'
        final_status = "completed" if failed == 0 else "completed_with_errors"
        self.db.update_archive_progress(
            archive_id,
            status=final_status,
            total_files=total_files,
            imported_files=imported,
            duplicate_files=duplicates,
            failed_files=failed,
            completed=(failed == 0),
        )
        self._log(
            "WARNING" if failed > 0 else "INFO",
            "pipeline",
            f"Finished {archive_path.name} ({final_status}): {imported} imported, "
            f"{duplicates} duplicates skipped, {failed} failed",
        )
        return {
            "total": total_files,
            "imported": imported,
            "duplicates": duplicates,
            "failed": failed,
        }

    def _repair_missing_existing_media(
        self,
        *,
        existing: dict[str, Any],
        incoming_path: Path,
        incoming_fp: Any,
        metadata: PhotoMetadata,
        taken_at_iso: str | None,
        importer: SynologyFolderImporter,
    ) -> None:
        """Repair a phantom DB row whose on-disk photo disappeared by importing the incoming copy."""
        settings = self.config_manager.settings
        exif_status = apply_metadata(
            incoming_path,
            metadata,
            write_exif=settings.write_exif_metadata,
            write_album_tags=settings.write_album_tags,
            exiftool_path=settings.exiftool_path,
        )
        final_hash = compute_file_sha256(incoming_path)
        dest_abs, dest_rel = importer.import_media(incoming_path, metadata)
        dest_stat = dest_abs.stat()
        self.db.upgrade_media_item(
            int(existing["id"]),
            original_sha256=incoming_fp.sha256,
            final_sha256=final_hash,
            pixel_sha256=incoming_fp.pixel_sha256,
            dhash=incoming_fp.dhash,
            width=incoming_fp.width,
            height=incoming_fp.height,
            sharpness=incoming_fp.sharpness,
            quality_score=incoming_fp.quality_score,
            original_filename=incoming_path.name,
            dest_rel_path=dest_rel,
            file_size=dest_stat.st_size,
            file_mtime_ns=dest_stat.st_mtime_ns,
            taken_at=taken_at_iso,
            exif_status=exif_status,
            visual_status=incoming_fp.visual_status,
            latitude=metadata.latitude,
            longitude=metadata.longitude,
        )
        self._log(
            "WARNING",
            "pipeline",
            f"Repaired missing library file {existing['dest_rel_path']} with {dest_rel}",
        )

    def _maybe_upgrade_duplicate(
        self,
        *,
        existing: dict[str, Any],
        incoming_path: Path,
        incoming_fp: Any,
        metadata: PhotoMetadata,
        taken_at_iso: str | None,
        match_reason: str,
        importer: SynologyFolderImporter,
    ) -> bool:
        """Compare visual duplicate quality and crash-atomically auto-upgrade if incoming is better (G2)."""
        settings = self.config_manager.settings
        existing_rel = str(existing["dest_rel_path"])
        existing_abs = settings.photos_dir / existing_rel
        if not existing_abs.is_file():
            return False

        incoming_size = incoming_path.stat().st_size
        existing_stat = existing_abs.stat()
        existing_width = int(existing.get("width") or 0)
        existing_height = int(existing.get("height") or 0)
        existing_sharpness = float(existing.get("sharpness") or 0.0)
        existing_size = existing_stat.st_size

        # G2: Re-fingerprint existing file if missing dimensions OR on-disk file diverged from DB
        db_size = int(existing.get("file_size") or 0)
        db_mtime = int(existing.get("file_mtime_ns") or 0)
        if (
            existing_width <= 0
            or existing_height <= 0
            or (db_size > 0 and db_size != existing_stat.st_size)
            or (db_mtime > 0 and db_mtime != existing_stat.st_mtime_ns)
        ):
            ext_fp = compute_media_fingerprint(existing_abs, compute_visual=True)
            existing_width = ext_fp.width
            existing_height = ext_fp.height
            existing_sharpness = ext_fp.sharpness

        decision = compare_image_quality(
            ImageQualityMetrics(
                width=incoming_fp.width,
                height=incoming_fp.height,
                file_size=incoming_size,
                sharpness=incoming_fp.sharpness,
                filename=incoming_path.name,
            ),
            ImageQualityMetrics(
                width=existing_width,
                height=existing_height,
                file_size=existing_size,
                sharpness=existing_sharpness,
                filename=existing_abs.name,
            ),
        )
        if not decision.incoming_wins:
            return False

        # Repair EXIF on the incoming higher-quality copy before swapping
        exif_status = apply_metadata(
            incoming_path,
            metadata,
            write_exif=settings.write_exif_metadata,
            write_album_tags=settings.write_album_tags,
            exiftool_path=settings.exiftool_path,
        )
        final_hash = compute_file_sha256(incoming_path)
        repaired_incoming_size = incoming_path.stat().st_size

        # Prepare collision-safe quarantine destination outside PHOTOS_DIR
        quarantine_dir = settings.quarantine_dir
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        media_id = int(existing["id"])
        ts_now = int(datetime.now(UTC).timestamp())
        q_name = f"media{media_id}_{ts_now}_{existing_abs.name}"
        q_path = quarantine_dir / q_name
        counter = 2
        while q_path.exists():
            q_name = f"media{media_id}_{ts_now}_{counter}_{existing_abs.name}"
            q_path = quarantine_dir / q_name
            counter += 1

        # G2: Write-ahead 'in_progress' journal record BEFORE touching the filesystem
        upgrade_id = self.db.create_pending_quality_upgrade(
            media_id=media_id,
            quarantine_filename=q_name,
            old_rel_path=existing_rel,
            old_filename=existing_abs.name,
            new_filename=incoming_path.name,
            old_width=existing_width,
            old_height=existing_height,
            old_file_size=existing_size,
            old_sharpness=existing_sharpness,
            new_width=incoming_fp.width,
            new_height=incoming_fp.height,
            new_file_size=repaired_incoming_size,
            new_sharpness=incoming_fp.sharpness,
            match_reason=match_reason,
            upgrade_reason=decision.reason,
            old_taken_at=str(existing.get("taken_at") or ""),
            old_latitude=existing.get("latitude"),
            old_longitude=existing.get("longitude"),
        )

        moved_existing = False
        placed_dest: Path | None = None
        try:
            # 1. Move replaced lower-quality file into safe quarantine
            safe_move_or_copy(existing_abs, q_path)
            moved_existing = True

            # 2. Place higher-quality image into PHOTOS_DIR. When the format
            #    (extension) matches, upgrade in place at the existing library
            #    path so the media record's path/filename is preserved; only a
            #    genuine format change imports under the incoming name.
            if existing_abs.suffix.lower() == incoming_path.suffix.lower():
                dest_abs = existing_abs
                safe_move_or_copy(incoming_path, dest_abs)
                dest_rel = existing_rel
            else:
                dest_abs, dest_rel = importer.import_media(incoming_path, metadata)
            placed_dest = dest_abs

            dest_stat = dest_abs.stat()
            self.db.upgrade_media_item(
                media_id,
                original_sha256=incoming_fp.sha256,
                final_sha256=final_hash,
                pixel_sha256=incoming_fp.pixel_sha256,
                dhash=incoming_fp.dhash,
                width=incoming_fp.width,
                height=incoming_fp.height,
                sharpness=incoming_fp.sharpness,
                quality_score=incoming_fp.quality_score,
                original_filename=incoming_path.name,
                dest_rel_path=dest_rel,
                file_size=dest_stat.st_size,
                file_mtime_ns=dest_stat.st_mtime_ns,
                taken_at=taken_at_iso,
                exif_status=exif_status,
                visual_status=incoming_fp.visual_status,
                latitude=metadata.latitude,
                longitude=metadata.longitude,
            )
            self.db.finalize_quality_upgrade(upgrade_id, dest_rel, new_file_size=dest_stat.st_size)
        except Exception:
            # Roll back filesystem & journal cleanly so no photo is ever lost
            if placed_dest is not None and placed_dest != existing_abs and placed_dest.is_file():
                with contextlib.suppress(OSError):
                    placed_dest.unlink()
            if moved_existing and q_path.is_file():
                with contextlib.suppress(Exception):
                    safe_move_or_copy(q_path, existing_abs)
            with contextlib.suppress(Exception):
                self.db.delete_quality_upgrade(upgrade_id)
            raise

        self._log(
            "INFO",
            "quality-upgrade",
            f"Upgraded {existing_rel} -> {dest_rel}: {decision.reason} (replaced copy quarantined as {q_name})",
        )
        return True

    def restore_quality_upgrade(self, upgrade_id: int) -> dict[str, Any]:
        """Safely restore a quarantined original image back into PHOTOS_DIR and undo the upgrade."""
        with self._op_lock:
            settings = self.config_manager.settings
            row = self.db.get_quality_upgrade(upgrade_id)
            if row is None:
                raise ValueError(f"Quality upgrade #{upgrade_id} not found")
            if row["status"] != "quarantined":
                raise ValueError(f"Upgrade #{upgrade_id} is already '{row['status']}'")

            q_path = _resolve_contained(settings.quarantine_dir, str(row["quarantine_filename"]))
            if not q_path.is_file():
                raise FileNotFoundError(f"Quarantined file missing: {q_path.name}")

            kept_abs = _resolve_contained(settings.photos_dir, str(row["kept_rel_path"]))
            old_rel = str(row.get("old_rel_path") or "").strip()
            if old_rel:
                restore_abs = _resolve_contained(settings.photos_dir, old_rel)
            else:
                restore_abs = _resolve_contained(
                    kept_abs.parent, str(row["old_filename"])
                )
                # Ensure restore_abs stays inside photos_dir
                _resolve_contained(
                    settings.photos_dir,
                    restore_abs.relative_to(settings.photos_dir.resolve()).as_posix(),
                )

            # Avoid clobbering an unrelated file if another photo now sits at restore_abs
            if restore_abs != kept_abs and restore_abs.exists():
                stem = restore_abs.stem
                suffix = restore_abs.suffix
                counter = 2
                candidate = restore_abs.with_name(f"{stem}_restored_{counter}{suffix}")
                while candidate.exists():
                    counter += 1
                    candidate = restore_abs.with_name(f"{stem}_restored_{counter}{suffix}")
                restore_abs = candidate

            # Stage kept_abs aside before moving q_path in, so failure never loses both copies
            staged_kept: Path | None = None
            if kept_abs.is_file():
                staged_kept = kept_abs.with_name(f".{kept_abs.name}.restore_tmp.{os.getpid()}")
                safe_move_or_copy(kept_abs, staged_kept)

            try:
                safe_move_or_copy(q_path, restore_abs)
            except Exception:
                if staged_kept is not None and staged_kept.is_file() and not kept_abs.exists():
                    with contextlib.suppress(Exception):
                        safe_move_or_copy(staged_kept, kept_abs)
                raise
            else:
                if staged_kept is not None and staged_kept.is_file():
                    with contextlib.suppress(OSError):
                        staged_kept.unlink()

            restored_rel = restore_abs.resolve().relative_to(settings.photos_dir.resolve()).as_posix()
            fp = compute_media_fingerprint(restore_abs, compute_visual=True)
            st = restore_abs.stat()

            preserved_taken_at = str(row.get("old_taken_at") or "").strip() or fp.exif_taken_at
            preserved_lat = row.get("old_latitude")
            preserved_lon = row.get("old_longitude")

            if row["media_id"] is not None:
                self.db.upgrade_media_item(
                    int(row["media_id"]),
                    original_sha256=fp.sha256,
                    final_sha256=fp.sha256,
                    pixel_sha256=fp.pixel_sha256,
                    dhash=fp.dhash,
                    width=fp.width,
                    height=fp.height,
                    sharpness=fp.sharpness,
                    quality_score=fp.quality_score,
                    original_filename=restore_abs.name,
                    dest_rel_path=restored_rel,
                    file_size=st.st_size,
                    file_mtime_ns=st.st_mtime_ns,
                    taken_at=preserved_taken_at,
                    exif_status="existing",
                    visual_status=fp.visual_status,
                    latitude=preserved_lat,
                    longitude=preserved_lon,
                )

            self.db.mark_quality_upgrade_status(upgrade_id, "restored")
            self._log(
                "INFO",
                "quality-upgrade",
                f"Restored quarantined copy {restore_abs.name} -> {restored_rel}",
            )
            return {"ok": True, "restored_rel_path": restored_rel}

    def purge_quality_upgrade(self, upgrade_id: int) -> dict[str, Any]:
        """Permanently delete a single quarantined lower-quality copy after review."""
        with self._op_lock:
            settings = self.config_manager.settings
            row = self.db.get_quality_upgrade(upgrade_id)
            if row is None:
                raise ValueError(f"Quality upgrade #{upgrade_id} not found")
            if row["status"] != "quarantined":
                raise ValueError(
                    f"Upgrade #{upgrade_id} cannot be purged from status '{row['status']}'"
                )
            q_path = _resolve_contained(settings.quarantine_dir, str(row["quarantine_filename"]))
            q_path.unlink(missing_ok=True)
            self.db.mark_quality_upgrade_status(upgrade_id, "purged")
            self._log(
                "INFO",
                "quality-upgrade",
                f"Purged quarantined duplicate {row['quarantine_filename']}",
            )
            return {"ok": True}

    def purge_all_quarantined(self) -> int:
        """Permanently delete all quarantined lower-quality copies in batches."""
        with self._op_lock:
            purged = 0
            while True:
                batch = self.db.list_quarantined_upgrades_batch(limit=500)
                if not batch:
                    break
                for row in batch:
                    self.purge_quality_upgrade(int(row["id"]))
                    purged += 1
            return purged

    def sync_pending_albums(self) -> int:
        """Sync pending Takeout album memberships into Synology Photos via DSM 7 API."""
        settings = self.config_manager.settings
        if not settings.synology_api_enabled:
            return 0

        pending = self.db.get_pending_album_memberships(limit=500)
        if not pending:
            return 0

        self._set_progress(
            state="syncing_albums",
            current_step=f"Syncing {len(pending)} pending album item(s) to Synology Photos",
        )

        # Group by album_id
        grouped: dict[int, list[dict[str, Any]]] = {}
        for row in pending:
            grouped.setdefault(int(row["album_id"]), []).append(row)

        synced_total = 0
        try:
            with self._synology_client_factory(settings) as syno:
                for album_id, items in grouped.items():
                    album_name = str(items[0]["album_name"])
                    syno_album_id = items[0]["synology_album_id"]
                    if syno_album_id is None:
                        syno_album_id = syno.get_or_create_album(album_name)
                        self.db.set_album_synology_id(album_id, syno_album_id)

                    resolved_pairs: list[tuple[int, int]] = []
                    for row in items:
                        media_id = int(row["media_id"])
                        syno_item_id = row["synology_item_id"]
                        if syno_item_id is None:
                            syno_item_id = syno.resolve_item_id(str(row["dest_rel_path"]))
                            if syno_item_id is not None:
                                self.db.set_media_synology_id(media_id, syno_item_id)

                        if syno_item_id is None:
                            # Synology DSM may still be indexing newly written files; do not burn retries
                            self.db.mark_album_membership(
                                album_id,
                                media_id,
                                status="pending",
                                error="Waiting for Synology Photos to index file",
                                increment_retry=False,
                            )
                        else:
                            resolved_pairs.append((media_id, int(syno_item_id)))

                    if resolved_pairs:
                        syno.add_items_to_album(
                            int(syno_album_id),
                            [syno_id for _media_id, syno_id in resolved_pairs],
                        )
                        for media_id, _syno_id in resolved_pairs:
                            self.db.mark_album_membership(album_id, media_id, status="synced")
                            synced_total += 1

            if synced_total > 0:
                self._log("INFO", "synology-api", f"Synced {synced_total} photo(s) into Synology Photos albums")
        except Exception as exc:
            err_msg = f"Synology Photos album sync failed: {exc}"
            self._log("ERROR", "synology-api", err_msg)
            self._set_progress(last_error=err_msg)

        return synced_total

    def _record_first_export_timestamp(self) -> None:
        if self.db.get_state("takeout_schedule_started_at") is None:
            self.db.set_state("takeout_schedule_started_at", utc_now_iso())

    def _check_renewal_reminder(self) -> None:
        settings = self.config_manager.settings
        started_raw = self.db.get_state("takeout_schedule_started_at")
        if not started_raw:
            self._set_progress(renewal_reminder_due=False)
            return
        try:
            started_dt = datetime.fromisoformat(started_raw)
        except ValueError:
            return

        now = datetime.now(UTC)
        due = (now - started_dt) >= timedelta(days=settings.takeout_renewal_reminder_days)
        self._set_progress(renewal_reminder_due=due)
        if not due:
            return

        last_reminded_raw = self.db.get_state("last_renewal_reminder_at")
        if last_reminded_raw:
            try:
                last_reminded = datetime.fromisoformat(last_reminded_raw)
                if (now - last_reminded) < timedelta(days=30):
                    return
            except ValueError:
                pass

        msg = (
            "Google Takeout scheduled incremental exports expire after 1 year. "
            "Visit takeout.google.com to start a new annual Google Photos export schedule, "
            "then click 'Reset Schedule Timer' in the dashboard."
        )
        self._log("WARNING", "schedule", msg)
        Notifier(settings).send_async("Renew Google Takeout Annual Schedule", msg, level="info")
        self.db.set_state("last_renewal_reminder_at", utc_now_iso())

    def _notify_archive_done(self, filename: str, stats: dict[str, int]) -> None:
        settings = self.config_manager.settings
        msg = (
            f"Archive: {filename}\n"
            f"Imported: {stats['imported']}\n"
            f"Duplicates skipped: {stats['duplicates']}\n"
            f"Failed: {stats['failed']}"
        )
        level = "error" if stats["failed"] > 0 else "info"
        Notifier(settings).send_async(f"Migration Complete: {filename}", msg, level=level)

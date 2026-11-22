"""Background daemon scheduler with multi-user profile cycling, pause/resume, and manual triggers."""

from __future__ import annotations

import contextlib
import logging
import threading
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from gphotos_synology.config import ConfigManager
from gphotos_synology.database import StateDB, utc_now_iso
from gphotos_synology.takeout.extractor import cleanup_stale_temp_files
from gphotos_synology.worker.pipeline import MigrationPipeline

if TYPE_CHECKING:
    from gphotos_synology.profiles import ProfileRegistry

logger = logging.getLogger(__name__)


class BackgroundScheduler:
    """Runs MigrationPipeline continuously across all enabled Synology user profiles."""

    def __init__(
        self,
        config_manager: ConfigManager,
        db: StateDB,
        pipeline: MigrationPipeline,
        *,
        registry: ProfileRegistry | None = None,
    ) -> None:
        self.config_manager = config_manager
        self.db = db
        self.pipeline = pipeline
        self.registry = registry

        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._run_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._requested_mode: str = "full"
        self._requested_profile_id: str | None = None

        paused_raw = self.db.get_state("worker_paused", "false")
        self._paused = paused_raw == "true"

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            return
        self._stop_event.clear()
        # Recover any interrupted upgrade swaps and clean stale .tmp.<pid> files on startup
        for pipe in self._all_pipelines():
            try:
                pipe.recover_in_progress_upgrades()
                cleanup_stale_temp_files(pipe.config_manager.settings.photos_dir, max_age_seconds=300.0)
            except Exception as exc:
                logger.warning("Startup recovery check failed: %s", exc)
        self._thread = threading.Thread(
            target=self._loop,
            name="gphotos-migrator-worker",
            daemon=True,
        )
        self._thread.start()
        self.db.log_event("INFO", "scheduler", "Background migration worker started")

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10.0)
        for pipe in self._all_pipelines():
            with contextlib.suppress(Exception):
                cleanup_stale_temp_files(pipe.config_manager.settings.photos_dir, max_age_seconds=0.0)
        self.db.log_event("INFO", "scheduler", "Background migration worker stopped")

    def _all_pipelines(self) -> list[MigrationPipeline]:
        if self.registry is not None:
            return [ctx.pipeline for ctx in self.registry.enabled_contexts()]
        return [self.pipeline]

    def pause(self) -> None:
        self._paused = True
        self.db.set_state("worker_paused", "true")
        for pipe in self._all_pipelines():
            pipe._set_progress(
                state="paused",
                current_step="Worker paused by user",
                next_run_at=None,
            )
        self.db.log_event("INFO", "scheduler", "Background worker paused")

    def resume(self) -> None:
        self._paused = False
        self.db.set_state("worker_paused", "false")
        for pipe in self._all_pipelines():
            pipe._set_progress(state="idle", current_step="Worker resumed")
        self.db.log_event("INFO", "scheduler", "Background worker resumed")
        self._wake_event.set()

    def trigger_now(
        self,
        *,
        profile_id: str | None = None,
        albums_only: bool = False,
        index_existing_only: bool = False,
    ) -> None:
        self._requested_profile_id = profile_id
        if index_existing_only:
            self._requested_mode = "index_existing"
        elif albums_only:
            self._requested_mode = "albums_only"
        else:
            self._requested_mode = "full"
        self._wake_event.set()

    def _resolve_targets(self, profile_id: str | None) -> list[MigrationPipeline]:
        if self.registry is None:
            return [self.pipeline]
        if profile_id:
            try:
                return [self.registry.get_context(profile_id).pipeline]
            except KeyError:
                return []
        return [ctx.pipeline for ctx in self.registry.enabled_contexts()]

    def _loop(self) -> None:
        # Run a quick initial check 3 seconds after container boot unless paused
        self._wake_event.wait(timeout=3.0)

        while not self._stop_event.is_set():
            self._wake_event.clear()
            mode = self._requested_mode
            target_profile = self._requested_profile_id
            self._requested_mode = "full"
            self._requested_profile_id = None

            if not self._paused:
                with self._run_lock:
                    targets = self._resolve_targets(target_profile)
                    for pipe in targets:
                        if self._stop_event.is_set() or self._paused:
                            break
                        if mode == "albums_only":
                            pipe.sync_pending_albums()
                            pipe._set_progress(
                                state="idle",
                                current_step="Idle — waiting for next scheduled poll",
                                last_run_at=utc_now_iso(),
                            )
                        elif mode == "index_existing":
                            pipe.index_existing_library()
                            pipe._set_progress(
                                state="idle",
                                current_step="Idle — existing NAS library indexed",
                                last_run_at=utc_now_iso(),
                            )
                        else:
                            pipe.run_cycle()
            else:
                for pipe in self._all_pipelines():
                    pipe._set_progress(
                        state="paused",
                        current_step="Worker paused by user",
                        next_run_at=None,
                    )

            interval_s = max(60, self.config_manager.settings.poll_interval_minutes * 60)
            if self._paused:
                for pipe in self._all_pipelines():
                    pipe._set_progress(next_run_at=None)
            else:
                next_dt = datetime.now(UTC) + timedelta(seconds=interval_s)
                for pipe in self._all_pipelines():
                    pipe._set_progress(next_run_at=next_dt.isoformat())

            self._wake_event.wait(timeout=interval_s)

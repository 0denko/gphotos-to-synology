"""Background daemon scheduler with pause/resume and immediate manual triggers."""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta

from gphotos_synology.config import ConfigManager
from gphotos_synology.database import StateDB
from gphotos_synology.worker.pipeline import MigrationPipeline

logger = logging.getLogger(__name__)


class BackgroundScheduler:
    """Runs MigrationPipeline continuously in a dedicated daemon thread."""

    def __init__(
        self,
        config_manager: ConfigManager,
        db: StateDB,
        pipeline: MigrationPipeline,
    ) -> None:
        self.config_manager = config_manager
        self.db = db
        self.pipeline = pipeline

        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._run_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._requested_mode: str = "full"

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
        self.db.log_event("INFO", "scheduler", "Background migration worker stopped")

    def pause(self) -> None:
        self._paused = True
        self.db.set_state("worker_paused", "true")
        self.pipeline._set_progress(state="paused", current_step="Worker paused by user")
        self.db.log_event("INFO", "scheduler", "Background worker paused")

    def resume(self) -> None:
        self._paused = False
        self.db.set_state("worker_paused", "false")
        self.pipeline._set_progress(state="idle", current_step="Worker resumed")
        self.db.log_event("INFO", "scheduler", "Background worker resumed")
        self._wake_event.set()

    def trigger_now(self, *, albums_only: bool = False, index_existing_only: bool = False) -> None:
        if index_existing_only:
            self._requested_mode = "index_existing"
        elif albums_only:
            self._requested_mode = "albums_only"
        else:
            self._requested_mode = "full"
        self._wake_event.set()

    def _loop(self) -> None:
        # Run a quick initial check 3 seconds after container boot unless paused
        self._wake_event.wait(timeout=3.0)

        while not self._stop_event.is_set():
            self._wake_event.clear()
            mode = self._requested_mode
            self._requested_mode = "full"

            if not self._paused:
                with self._run_lock:
                    if mode == "albums_only":
                        self.pipeline.sync_pending_albums()
                        self.pipeline._set_progress(
                            state="idle",
                            current_step="Idle — waiting for next scheduled poll",
                        )
                    elif mode == "index_existing":
                        self.pipeline.index_existing_library()
                        self.pipeline._set_progress(
                            state="idle",
                            current_step="Idle — existing NAS library indexed",
                        )
                    else:
                        self.pipeline.run_cycle()
            else:
                self.pipeline._set_progress(state="paused", current_step="Worker paused by user")

            interval_s = max(60, self.config_manager.settings.poll_interval_minutes * 60)
            next_dt = datetime.now(UTC) + timedelta(seconds=interval_s)
            self.pipeline._set_progress(next_run_at=next_dt.isoformat())

            self._wake_event.wait(timeout=interval_s)

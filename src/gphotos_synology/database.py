"""Thread-safe SQLite (WAL) state store for deduplication, archive tracking, and album sync."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 30000;

CREATE TABLE IF NOT EXISTS archives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,                      -- 'gdrive' | 'inbox'
    source_id TEXT NOT NULL,                   -- Drive file_id or canonical inbox fingerprint
    filename TEXT NOT NULL,
    batch_key TEXT NOT NULL DEFAULT '',        -- e.g. takeout-20261121T000000Z
    size_bytes INTEGER NOT NULL DEFAULT 0,
    checksum TEXT NOT NULL DEFAULT '',         -- Drive md5Checksum or local sha256
    status TEXT NOT NULL DEFAULT 'pending',    -- pending | downloading | processing | completed | failed
    total_files INTEGER NOT NULL DEFAULT 0,
    imported_files INTEGER NOT NULL DEFAULT 0,
    duplicate_files INTEGER NOT NULL DEFAULT 0,
    failed_files INTEGER NOT NULL DEFAULT 0,
    error_message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(source, source_id)
);

CREATE TABLE IF NOT EXISTS media_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    original_sha256 TEXT NOT NULL UNIQUE,      -- SHA-256 before EXIF modification (dedup key)
    final_sha256 TEXT NOT NULL DEFAULT '',     -- SHA-256 after EXIF modification
    pixel_sha256 TEXT NOT NULL DEFAULT '',     -- SHA-256 of decoded RGB pixels (EXIF-resilient dedup)
    dhash TEXT NOT NULL DEFAULT '',            -- 128-bit composite perceptual hash (32 hex chars)
    width INTEGER NOT NULL DEFAULT 0,
    height INTEGER NOT NULL DEFAULT 0,
    sharpness REAL NOT NULL DEFAULT 0.0,       -- Laplacian edge variance (clarity metric)
    quality_score REAL NOT NULL DEFAULT 0.0,
    origin TEXT NOT NULL DEFAULT 'takeout',    -- 'takeout' | 'existing_nas'
    original_filename TEXT NOT NULL,
    dest_rel_path TEXT NOT NULL,               -- Path relative to PHOTOS_DIR
    file_size INTEGER NOT NULL DEFAULT 0,
    file_mtime_ns INTEGER NOT NULL DEFAULT 0,
    taken_at TEXT,                             -- ISO-8601 UTC timestamp
    latitude REAL,
    longitude REAL,
    exif_status TEXT NOT NULL DEFAULT 'none',  -- repaired | mtime_only | existing | skipped | error | none
    source_archive_id INTEGER REFERENCES archives(id) ON DELETE SET NULL,
    synology_item_id INTEGER,                  -- Resolved Synology Photos item ID (optional API mode)
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_media_dest_path ON media_items(dest_rel_path);
CREATE INDEX IF NOT EXISTS idx_media_final_hash ON media_items(final_sha256);
CREATE INDEX IF NOT EXISTS idx_media_pixel_hash ON media_items(pixel_sha256);
CREATE INDEX IF NOT EXISTS idx_media_dhash ON media_items(dhash);

CREATE TABLE IF NOT EXISTS quality_upgrades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_id INTEGER REFERENCES media_items(id) ON DELETE SET NULL,
    kept_rel_path TEXT NOT NULL,
    quarantine_filename TEXT NOT NULL,
    old_filename TEXT NOT NULL,
    new_filename TEXT NOT NULL,
    old_width INTEGER NOT NULL DEFAULT 0,
    old_height INTEGER NOT NULL DEFAULT 0,
    old_file_size INTEGER NOT NULL DEFAULT 0,
    old_sharpness REAL NOT NULL DEFAULT 0.0,
    new_width INTEGER NOT NULL DEFAULT 0,
    new_height INTEGER NOT NULL DEFAULT 0,
    new_file_size INTEGER NOT NULL DEFAULT 0,
    new_sharpness REAL NOT NULL DEFAULT 0.0,
    match_reason TEXT NOT NULL DEFAULT '',
    upgrade_reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'quarantined', -- quarantined | restored | purged
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_quality_upgrades_status ON quality_upgrades(status);

CREATE TABLE IF NOT EXISTS albums (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    synology_album_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS album_memberships (
    album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    media_id INTEGER NOT NULL REFERENCES media_items(id) ON DELETE CASCADE,
    sync_status TEXT NOT NULL DEFAULT 'pending', -- pending | synced | error
    retry_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (album_id, media_id)
);

CREATE INDEX IF NOT EXISTS idx_album_memberships_status ON album_memberships(sync_status);

CREATE TABLE IF NOT EXISTS activity_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT NOT NULL,                       -- INFO | WARNING | ERROR
    component TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class StateDB:
    """Thread-safe SQLite repository for migration state."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._connect()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _init_schema(self) -> None:
        with self.transaction() as conn:
            conn.executescript(SCHEMA_SQL)
            # Migrate existing databases smoothly if new columns were added
            existing_cols = {
                row["name"] for row in conn.execute("PRAGMA table_info(media_items)").fetchall()
            }
            migrations = [
                ("pixel_sha256", "TEXT NOT NULL DEFAULT ''"),
                ("dhash", "TEXT NOT NULL DEFAULT ''"),
                ("width", "INTEGER NOT NULL DEFAULT 0"),
                ("height", "INTEGER NOT NULL DEFAULT 0"),
                ("sharpness", "REAL NOT NULL DEFAULT 0.0"),
                ("quality_score", "REAL NOT NULL DEFAULT 0.0"),
                ("origin", "TEXT NOT NULL DEFAULT 'takeout'"),
                ("file_mtime_ns", "INTEGER NOT NULL DEFAULT 0"),
            ]
            for col_name, col_def in migrations:
                if col_name not in existing_cols:
                    conn.execute(f"ALTER TABLE media_items ADD COLUMN {col_name} {col_def}")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_media_final_hash ON media_items(final_sha256)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_media_pixel_hash ON media_items(pixel_sha256)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_media_dhash ON media_items(dhash)")

    # ------------------------------------------------------------------
    # App state key-value helpers
    # ------------------------------------------------------------------
    def get_state(self, key: str, default: str | None = None) -> str | None:
        with self.transaction() as conn:
            row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        now = utc_now_iso()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO app_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, value, now),
            )

    # ------------------------------------------------------------------
    # Activity logs
    # ------------------------------------------------------------------
    def log_event(self, level: str, component: str, message: str, max_rows: int = 2000) -> None:
        now = utc_now_iso()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO activity_logs (level, component, message, created_at) VALUES (?, ?, ?, ?)",
                (level.upper(), component, message, now),
            )
            conn.execute(
                """
                DELETE FROM activity_logs
                WHERE id NOT IN (
                    SELECT id FROM activity_logs ORDER BY id DESC LIMIT ?
                )
                """,
                (max_rows,),
            )

    def get_recent_logs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT id, level, component, message, created_at FROM activity_logs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Archives
    # ------------------------------------------------------------------
    def get_archive_by_source(self, source: str, source_id: str) -> dict[str, Any] | None:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM archives WHERE source = ? AND source_id = ?",
                (source, source_id),
            ).fetchone()
            return dict(row) if row else None

    def upsert_archive(
        self,
        *,
        source: str,
        source_id: str,
        filename: str,
        batch_key: str = "",
        size_bytes: int = 0,
        checksum: str = "",
        status: str = "pending",
    ) -> int:
        now = utc_now_iso()
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT id FROM archives WHERE source = ? AND source_id = ?",
                (source, source_id),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE archives
                    SET filename = ?, batch_key = ?, size_bytes = ?, checksum = ?,
                        status = ?, error_message = '', updated_at = ?
                    WHERE id = ?
                    """,
                    (filename, batch_key, size_bytes, checksum, status, now, existing["id"]),
                )
                return int(existing["id"])
            cursor = conn.execute(
                """
                INSERT INTO archives (
                    source, source_id, filename, batch_key, size_bytes, checksum,
                    status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (source, source_id, filename, batch_key, size_bytes, checksum, status, now, now),
            )
            return int(cursor.lastrowid)

    def update_archive_progress(
        self,
        archive_id: int,
        *,
        status: str | None = None,
        total_files: int | None = None,
        imported_files: int | None = None,
        duplicate_files: int | None = None,
        failed_files: int | None = None,
        error_message: str | None = None,
        completed: bool = False,
    ) -> None:
        fields: list[str] = ["updated_at = ?"]
        params: list[Any] = [utc_now_iso()]

        if status is not None:
            fields.append("status = ?")
            params.append(status)
        if total_files is not None:
            fields.append("total_files = ?")
            params.append(total_files)
        if imported_files is not None:
            fields.append("imported_files = ?")
            params.append(imported_files)
        if duplicate_files is not None:
            fields.append("duplicate_files = ?")
            params.append(duplicate_files)
        if failed_files is not None:
            fields.append("failed_files = ?")
            params.append(failed_files)
        if error_message is not None:
            fields.append("error_message = ?")
            params.append(error_message)
        if completed:
            fields.append("completed_at = ?")
            params.append(utc_now_iso())

        params.append(archive_id)
        with self.transaction() as conn:
            conn.execute(f"UPDATE archives SET {', '.join(fields)} WHERE id = ?", params)

    def list_archives(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM archives ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Media items, existing NAS library index & multi-tier deduplication
    # ------------------------------------------------------------------
    def get_media_by_original_hash(self, original_sha256: str) -> dict[str, Any] | None:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM media_items WHERE original_sha256 = ? OR final_sha256 = ?",
                (original_sha256, original_sha256),
            ).fetchone()
            return dict(row) if row else None

    def find_duplicate_media(
        self,
        *,
        sha256: str,
        pixel_sha256: str = "",
        dhash: str = "",
        width: int = 0,
        height: int = 0,
        taken_at: str | None = None,
        filename: str = "",
        dedup_mode: str = "perceptual",
        perceptual_max_distance: int = 10,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Look up an existing media record using the configured deduplication tier.

        Returns (matched_row, reason) where reason is 'sha256', 'pixel_sha256',
        'perceptual_dhash', or None.
        """
        from gphotos_synology.takeout.dedup import is_perceptual_match

        with self.transaction() as conn:
            # Tier 1: Exact file SHA-256 (checks both raw original and EXIF-repaired final hashes)
            row = conn.execute(
                "SELECT * FROM media_items WHERE original_sha256 = ? OR final_sha256 = ? LIMIT 1",
                (sha256, sha256),
            ).fetchone()
            if row is not None:
                return dict(row), "sha256"

            if dedup_mode == "strict_hash":
                return None, None

            # Tier 2: Exact decoded RGB pixel SHA-256 (catches EXIF/header differences, zero false positives)
            if pixel_sha256:
                px_row = conn.execute(
                    "SELECT * FROM media_items WHERE pixel_sha256 = ? LIMIT 1",
                    (pixel_sha256,),
                ).fetchone()
                if px_row is not None:
                    return dict(px_row), "pixel_sha256"

            # Tier 3: Guarded 128-bit perceptual dHash+vHash match across resolutions & compression levels
            if dedup_mode == "perceptual" and dhash:
                exact_dhash_rows = conn.execute(
                    "SELECT * FROM media_items WHERE dhash = ?",
                    (dhash,),
                ).fetchall()
                for cand in exact_dhash_rows:
                    if is_perceptual_match(
                        dhash,
                        str(cand["dhash"]),
                        incoming_taken_at=taken_at,
                        existing_taken_at=cand["taken_at"],
                        incoming_filename=filename,
                        existing_filename=str(cand["original_filename"]),
                        incoming_width=width,
                        incoming_height=height,
                        existing_width=int(cand["width"] or 0),
                        existing_height=int(cand["height"] or 0),
                        max_distance=perceptual_max_distance,
                    ):
                        return dict(cand), "perceptual_dhash"

                all_dhash_rows = conn.execute(
                    "SELECT * FROM media_items WHERE dhash != '' AND dhash != ?",
                    (dhash,),
                ).fetchall()
                for cand in all_dhash_rows:
                    if is_perceptual_match(
                        dhash,
                        str(cand["dhash"]),
                        incoming_taken_at=taken_at,
                        existing_taken_at=cand["taken_at"],
                        incoming_filename=filename,
                        existing_filename=str(cand["original_filename"]),
                        incoming_width=width,
                        incoming_height=height,
                        existing_width=int(cand["width"] or 0),
                        existing_height=int(cand["height"] or 0),
                        max_distance=perceptual_max_distance,
                    ):
                        return dict(cand), "perceptual_dhash"

        return None, None

    def get_indexed_files_by_rel_path(self) -> dict[str, dict[str, Any]]:
        """Return lightweight metadata of all indexed files keyed by dest_rel_path."""
        with self.transaction() as conn:
            rows = conn.execute(
                """
                SELECT id, dest_rel_path, file_size, file_mtime_ns, pixel_sha256, dhash, width, origin
                FROM media_items
                """
            ).fetchall()
            return {
                str(r["dest_rel_path"]): {
                    "id": int(r["id"]),
                    "file_size": int(r["file_size"]),
                    "file_mtime_ns": int(r["file_mtime_ns"]),
                    "has_visual": bool(r["pixel_sha256"]) and len(str(r["dhash"] or "")) == 32 and int(r["width"] or 0) > 0,
                    "origin": str(r["origin"]),
                }
                for r in rows
            }

    def upsert_existing_nas_media(
        self,
        *,
        sha256: str,
        pixel_sha256: str,
        dhash: str,
        width: int = 0,
        height: int = 0,
        sharpness: float = 0.0,
        quality_score: float = 0.0,
        filename: str,
        dest_rel_path: str,
        file_size: int,
        file_mtime_ns: int,
        taken_at: str | None,
    ) -> int:
        """Index an existing photo/video already present on the Synology NAS."""
        now = utc_now_iso()
        with self.transaction() as conn:
            by_path = conn.execute(
                "SELECT id FROM media_items WHERE dest_rel_path = ?",
                (dest_rel_path,),
            ).fetchone()
            if by_path:
                conn.execute(
                    """
                    UPDATE media_items
                    SET final_sha256 = ?,
                        pixel_sha256 = CASE WHEN ? != '' THEN ? ELSE pixel_sha256 END,
                        dhash = CASE WHEN ? != '' THEN ? ELSE dhash END,
                        width = CASE WHEN ? > 0 THEN ? ELSE width END,
                        height = CASE WHEN ? > 0 THEN ? ELSE height END,
                        sharpness = CASE WHEN ? > 0 THEN ? ELSE sharpness END,
                        quality_score = CASE WHEN ? > 0 THEN ? ELSE quality_score END,
                        file_size = ?,
                        file_mtime_ns = ?,
                        taken_at = COALESCE(taken_at, ?)
                    WHERE id = ?
                    """,
                    (
                        sha256,
                        pixel_sha256,
                        pixel_sha256,
                        dhash,
                        dhash,
                        width,
                        width,
                        height,
                        height,
                        sharpness,
                        sharpness,
                        quality_score,
                        quality_score,
                        file_size,
                        file_mtime_ns,
                        taken_at,
                        by_path["id"],
                    ),
                )
                return int(by_path["id"])

            by_hash = conn.execute(
                "SELECT id FROM media_items WHERE original_sha256 = ? OR final_sha256 = ?",
                (sha256, sha256),
            ).fetchone()
            if by_hash:
                return int(by_hash["id"])

            cursor = conn.execute(
                """
                INSERT INTO media_items (
                    original_sha256, final_sha256, pixel_sha256, dhash,
                    width, height, sharpness, quality_score, origin,
                    original_filename, dest_rel_path, file_size, file_mtime_ns,
                    taken_at, exif_status, source_archive_id, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'existing_nas', ?, ?, ?, ?, ?, 'existing', NULL, ?)
                """,
                (
                    sha256,
                    sha256,
                    pixel_sha256,
                    dhash,
                    width,
                    height,
                    sharpness,
                    quality_score,
                    filename,
                    dest_rel_path,
                    file_size,
                    file_mtime_ns,
                    taken_at,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def prune_missing_existing_nas_media(self, present_rel_paths: set[str]) -> int:
        """Delete stale existing_nas index rows for files that were removed from the NAS."""
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT id, dest_rel_path FROM media_items WHERE origin = 'existing_nas'"
            ).fetchall()
            missing_ids = [
                int(r["id"]) for r in rows if str(r["dest_rel_path"]) not in present_rel_paths
            ]
            if not missing_ids:
                return 0
            for start in range(0, len(missing_ids), 500):
                chunk = missing_ids[start : start + 500]
                placeholders = ",".join("?" for _ in chunk)
                conn.execute(f"DELETE FROM media_items WHERE id IN ({placeholders})", chunk)
            return len(missing_ids)

    def record_media_item(
        self,
        *,
        original_sha256: str,
        final_sha256: str,
        original_filename: str,
        dest_rel_path: str,
        file_size: int,
        taken_at: str | None,
        latitude: float | None,
        longitude: float | None,
        exif_status: str,
        source_archive_id: int | None,
        pixel_sha256: str = "",
        dhash: str = "",
        width: int = 0,
        height: int = 0,
        sharpness: float = 0.0,
        quality_score: float = 0.0,
        file_mtime_ns: int = 0,
    ) -> int:
        now = utc_now_iso()
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO media_items (
                    original_sha256, final_sha256, pixel_sha256, dhash,
                    width, height, sharpness, quality_score, origin,
                    original_filename, dest_rel_path, file_size, file_mtime_ns,
                    taken_at, latitude, longitude, exif_status,
                    source_archive_id, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'takeout', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    original_sha256,
                    final_sha256,
                    pixel_sha256,
                    dhash,
                    width,
                    height,
                    sharpness,
                    quality_score,
                    original_filename,
                    dest_rel_path,
                    file_size,
                    file_mtime_ns,
                    taken_at,
                    latitude,
                    longitude,
                    exif_status,
                    source_archive_id,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def upgrade_media_item(
        self,
        media_id: int,
        *,
        original_sha256: str,
        final_sha256: str,
        pixel_sha256: str,
        dhash: str,
        width: int,
        height: int,
        sharpness: float,
        quality_score: float,
        original_filename: str,
        dest_rel_path: str,
        file_size: int,
        file_mtime_ns: int,
        taken_at: str | None,
        exif_status: str,
    ) -> None:
        """Replace an existing media_items record in place with a higher-quality image.

        Preserves `media_id` so all Google Photos album memberships stay linked, and resets
        Synology item sync status so DSM 7 API re-binds the upgraded photo.
        """
        now = utc_now_iso()
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE media_items
                SET original_sha256 = ?,
                    final_sha256 = ?,
                    pixel_sha256 = ?,
                    dhash = ?,
                    width = ?,
                    height = ?,
                    sharpness = ?,
                    quality_score = ?,
                    original_filename = ?,
                    dest_rel_path = ?,
                    file_size = ?,
                    file_mtime_ns = ?,
                    taken_at = COALESCE(?, taken_at),
                    exif_status = ?,
                    synology_item_id = NULL
                WHERE id = ?
                """,
                (
                    original_sha256,
                    final_sha256,
                    pixel_sha256,
                    dhash,
                    width,
                    height,
                    sharpness,
                    quality_score,
                    original_filename,
                    dest_rel_path,
                    file_size,
                    file_mtime_ns,
                    taken_at,
                    exif_status,
                    media_id,
                ),
            )
            conn.execute(
                """
                UPDATE album_memberships
                SET sync_status = 'pending', retry_count = 0, updated_at = ?
                WHERE media_id = ?
                """,
                (now, media_id),
            )

    def set_media_synology_id(self, media_id: int, synology_item_id: int) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE media_items SET synology_item_id = ? WHERE id = ?",
                (synology_item_id, media_id),
            )

    # ------------------------------------------------------------------
    # Quality upgrades & quarantine review queue
    # ------------------------------------------------------------------
    def record_quality_upgrade(
        self,
        *,
        media_id: int,
        kept_rel_path: str,
        quarantine_filename: str,
        old_filename: str,
        new_filename: str,
        old_width: int,
        old_height: int,
        old_file_size: int,
        old_sharpness: float,
        new_width: int,
        new_height: int,
        new_file_size: int,
        new_sharpness: float,
        match_reason: str,
        upgrade_reason: str,
    ) -> int:
        now = utc_now_iso()
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO quality_upgrades (
                    media_id, kept_rel_path, quarantine_filename,
                    old_filename, new_filename,
                    old_width, old_height, old_file_size, old_sharpness,
                    new_width, new_height, new_file_size, new_sharpness,
                    match_reason, upgrade_reason, status, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'quarantined', ?)
                """,
                (
                    media_id,
                    kept_rel_path,
                    quarantine_filename,
                    old_filename,
                    new_filename,
                    old_width,
                    old_height,
                    old_file_size,
                    old_sharpness,
                    new_width,
                    new_height,
                    new_file_size,
                    new_sharpness,
                    match_reason,
                    upgrade_reason,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def list_quality_upgrades(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM quality_upgrades ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_quality_upgrade(self, upgrade_id: int) -> dict[str, Any] | None:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM quality_upgrades WHERE id = ?",
                (upgrade_id,),
            ).fetchone()
            return dict(row) if row else None

    def mark_quality_upgrade_status(self, upgrade_id: int, status: str) -> None:
        now = utc_now_iso()
        with self.transaction() as conn:
            conn.execute(
                "UPDATE quality_upgrades SET status = ?, resolved_at = ? WHERE id = ?",
                (status, now, upgrade_id),
            )

    # ------------------------------------------------------------------
    # Albums & Synology virtual album sync queue
    # ------------------------------------------------------------------
    def add_media_to_album(self, album_name: str, media_id: int) -> int:
        now = utc_now_iso()
        with self.transaction() as conn:
            row = conn.execute("SELECT id FROM albums WHERE name = ?", (album_name,)).fetchone()
            if row:
                album_id = int(row["id"])
            else:
                cursor = conn.execute(
                    "INSERT INTO albums (name, created_at, updated_at) VALUES (?, ?, ?)",
                    (album_name, now, now),
                )
                album_id = int(cursor.lastrowid)

            conn.execute(
                """
                INSERT OR IGNORE INTO album_memberships (album_id, media_id, sync_status, updated_at)
                VALUES (?, ?, 'pending', ?)
                """,
                (album_id, media_id, now),
            )
            return album_id

    def set_album_synology_id(self, album_id: int, synology_album_id: int) -> None:
        now = utc_now_iso()
        with self.transaction() as conn:
            conn.execute(
                "UPDATE albums SET synology_album_id = ?, updated_at = ? WHERE id = ?",
                (synology_album_id, now, album_id),
            )

    def get_pending_album_memberships(self, limit: int = 500) -> list[dict[str, Any]]:
        with self.transaction() as conn:
            rows = conn.execute(
                """
                SELECT
                    am.album_id,
                    am.media_id,
                    am.retry_count,
                    a.name AS album_name,
                    a.synology_album_id,
                    m.dest_rel_path,
                    m.original_filename,
                    m.synology_item_id
                FROM album_memberships am
                JOIN albums a ON a.id = am.album_id
                JOIN media_items m ON m.id = am.media_id
                WHERE am.sync_status IN ('pending', 'error') AND am.retry_count < 20
                ORDER BY am.album_id, am.media_id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_album_membership(
        self, album_id: int, media_id: int, *, status: str, error: str = ""
    ) -> None:
        now = utc_now_iso()
        with self.transaction() as conn:
            if status == "synced":
                conn.execute(
                    """
                    UPDATE album_memberships
                    SET sync_status = 'synced', last_error = '', updated_at = ?
                    WHERE album_id = ? AND media_id = ?
                    """,
                    (now, album_id, media_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE album_memberships
                    SET sync_status = ?, retry_count = retry_count + 1,
                        last_error = ?, updated_at = ?
                    WHERE album_id = ? AND media_id = ?
                    """,
                    (status, error, now, album_id, media_id),
                )

    def list_albums_summary(self) -> list[dict[str, Any]]:
        with self.transaction() as conn:
            rows = conn.execute(
                """
                SELECT
                    a.id,
                    a.name,
                    a.synology_album_id,
                    COUNT(am.media_id) AS total_items,
                    SUM(CASE WHEN am.sync_status = 'synced' THEN 1 ELSE 0 END) AS synced_items,
                    SUM(CASE WHEN am.sync_status != 'synced' THEN 1 ELSE 0 END) AS pending_items
                FROM albums a
                LEFT JOIN album_memberships am ON am.album_id = a.id
                GROUP BY a.id
                ORDER BY a.name COLLATE NOCASE
                """
            ).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Dashboard metrics
    # ------------------------------------------------------------------
    def get_stats(self) -> dict[str, Any]:
        with self.transaction() as conn:
            media_row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN origin = 'takeout' THEN 1 ELSE 0 END) AS migrated_media,
                    SUM(CASE WHEN origin = 'existing_nas' THEN 1 ELSE 0 END) AS existing_nas_indexed,
                    COUNT(*) AS total_library_items,
                    COALESCE(SUM(CASE WHEN origin = 'takeout' THEN file_size ELSE 0 END), 0) AS migrated_bytes,
                    SUM(CASE WHEN exif_status = 'repaired' THEN 1 ELSE 0 END) AS exif_repaired
                FROM media_items
                """
            ).fetchone()
            archive_row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_archives,
                    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed_archives,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_archives,
                    COALESCE(SUM(duplicate_files), 0) AS total_duplicates_skipped
                FROM archives
                """
            ).fetchone()
            album_row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_albums,
                    (SELECT COUNT(*) FROM album_memberships WHERE sync_status = 'synced') AS synced_memberships,
                    (SELECT COUNT(*) FROM album_memberships WHERE sync_status != 'synced') AS pending_memberships
                FROM albums
                """
            ).fetchone()
            upgrade_row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_upgrades,
                    SUM(CASE WHEN status = 'quarantined' THEN 1 ELSE 0 END) AS quarantined_count
                FROM quality_upgrades
                """
            ).fetchone()

            return {
                "total_media": int(media_row["migrated_media"] or 0),
                "existing_nas_indexed": int(media_row["existing_nas_indexed"] or 0),
                "total_library_items": int(media_row["total_library_items"] or 0),
                "total_bytes": int(media_row["migrated_bytes"] or 0),
                "exif_repaired": int(media_row["exif_repaired"] or 0),
                "total_archives": int(archive_row["total_archives"] or 0),
                "completed_archives": int(archive_row["completed_archives"] or 0),
                "failed_archives": int(archive_row["failed_archives"] or 0),
                "total_duplicates_skipped": int(archive_row["total_duplicates_skipped"] or 0),
                "total_albums": int(album_row["total_albums"] or 0),
                "synced_album_items": int(album_row["synced_memberships"] or 0),
                "pending_album_items": int(album_row["pending_memberships"] or 0),
                "quality_upgrades_count": int(upgrade_row["total_upgrades"] or 0),
                "quarantined_count": int(upgrade_row["quarantined_count"] or 0),
            }

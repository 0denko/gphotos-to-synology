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


_UNSET: Any = object()
_HEX_CHARS = frozenset("0123456789abcdef")

_DHASH_BUCKET_CANDIDATES_SQL = """
SELECT m.*
FROM media_items m
WHERE m.id IN (
    SELECT media_id FROM media_dhash_buckets WHERE bucket_idx = 0 AND bucket_val = ?
    UNION
    SELECT media_id FROM media_dhash_buckets WHERE bucket_idx = 1 AND bucket_val = ?
    UNION
    SELECT media_id FROM media_dhash_buckets WHERE bucket_idx = 2 AND bucket_val = ?
    UNION
    SELECT media_id FROM media_dhash_buckets WHERE bucket_idx = 3 AND bucket_val = ?
    UNION
    SELECT media_id FROM media_dhash_buckets WHERE bucket_idx = 4 AND bucket_val = ?
    UNION
    SELECT media_id FROM media_dhash_buckets WHERE bucket_idx = 5 AND bucket_val = ?
    UNION
    SELECT media_id FROM media_dhash_buckets WHERE bucket_idx = 6 AND bucket_val = ?
    UNION
    SELECT media_id FROM media_dhash_buckets WHERE bucket_idx = 7 AND bucket_val = ?
)
AND m.dhash != ?
ORDER BY m.id ASC
"""


def _normalize_dhash(dhash: str) -> str:
    return (dhash or "").strip().lower()


def _is_valid_128bit_dhash(dhash: str) -> bool:
    return len(dhash) == 32 and all(ch in _HEX_CHARS for ch in dhash)


def _dhash_buckets(dhash: str) -> list[tuple[int, str]]:
    return [(i, dhash[i * 4 : (i + 1) * 4]) for i in range(8)]


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
    status TEXT NOT NULL DEFAULT 'pending',    -- pending | downloading | processing | completed | completed_with_errors | failed
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
    visual_status TEXT NOT NULL DEFAULT '',    -- ok | undecodable | skipped | ''
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

CREATE TABLE IF NOT EXISTS media_dhash_buckets (
    bucket_idx INTEGER NOT NULL,
    bucket_val TEXT NOT NULL,
    media_id INTEGER NOT NULL REFERENCES media_items(id) ON DELETE CASCADE,
    PRIMARY KEY (bucket_idx, bucket_val, media_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_dhash_buckets_media_id ON media_dhash_buckets(media_id);

CREATE TABLE IF NOT EXISTS quality_upgrades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_id INTEGER REFERENCES media_items(id) ON DELETE SET NULL,
    kept_rel_path TEXT NOT NULL,
    old_rel_path TEXT NOT NULL DEFAULT '',
    old_taken_at TEXT NOT NULL DEFAULT '',
    old_latitude REAL,
    old_longitude REAL,
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
    status TEXT NOT NULL DEFAULT 'quarantined', -- in_progress | quarantined | restored | purged
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

    _LOG_PRUNE_INTERVAL = 50

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._log_insert_count = 0
        self._has_legacy_dhash = False
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

    def _sync_dhash_buckets(
        self, conn: sqlite3.Connection, media_id: int, dhash_norm: str
    ) -> None:
        conn.execute("DELETE FROM media_dhash_buckets WHERE media_id = ?", (media_id,))
        if _is_valid_128bit_dhash(dhash_norm):
            conn.executemany(
                "INSERT INTO media_dhash_buckets (bucket_idx, bucket_val, media_id) VALUES (?, ?, ?)",
                [(idx, val, media_id) for idx, val in _dhash_buckets(dhash_norm)],
            )
        elif dhash_norm:
            self._has_legacy_dhash = True

    def _init_schema(self) -> None:
        with self.transaction() as conn:
            conn.executescript(SCHEMA_SQL)
            # Migrate existing databases smoothly if new columns were added
            existing_media_cols = {
                row["name"] for row in conn.execute("PRAGMA table_info(media_items)").fetchall()
            }
            media_migrations = [
                ("pixel_sha256", "TEXT NOT NULL DEFAULT ''"),
                ("dhash", "TEXT NOT NULL DEFAULT ''"),
                ("width", "INTEGER NOT NULL DEFAULT 0"),
                ("height", "INTEGER NOT NULL DEFAULT 0"),
                ("sharpness", "REAL NOT NULL DEFAULT 0.0"),
                ("quality_score", "REAL NOT NULL DEFAULT 0.0"),
                ("visual_status", "TEXT NOT NULL DEFAULT ''"),
                ("origin", "TEXT NOT NULL DEFAULT 'takeout'"),
                ("file_mtime_ns", "INTEGER NOT NULL DEFAULT 0"),
            ]
            for col_name, col_def in media_migrations:
                if col_name not in existing_media_cols:
                    conn.execute(f"ALTER TABLE media_items ADD COLUMN {col_name} {col_def}")

            existing_upgrade_cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(quality_upgrades)").fetchall()
            }
            upgrade_migrations = [
                ("old_rel_path", "TEXT NOT NULL DEFAULT ''"),
                ("old_taken_at", "TEXT NOT NULL DEFAULT ''"),
                ("old_latitude", "REAL"),
                ("old_longitude", "REAL"),
            ]
            for col_name, col_def in upgrade_migrations:
                if col_name not in existing_upgrade_cols:
                    conn.execute(f"ALTER TABLE quality_upgrades ADD COLUMN {col_name} {col_def}")

            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_media_final_hash ON media_items(final_sha256)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_media_pixel_hash ON media_items(pixel_sha256)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_media_dhash ON media_items(dhash)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_dhash_buckets_media_id ON media_dhash_buckets(media_id)"
            )

            # Normalize any uppercase dhash values before backfilling buckets
            conn.execute("UPDATE media_items SET dhash = lower(dhash) WHERE dhash != lower(dhash)")

            # Mark already-fingerprinted legacy rows as visual_status='ok' while leaving
            # empty-visual rows (e.g. pre-v1.2.0 HEIC files) as '' so they get re-indexed once.
            conn.execute(
                """
                UPDATE media_items
                SET visual_status = 'ok'
                WHERE visual_status = ''
                  AND pixel_sha256 != ''
                  AND length(dhash) = 32
                  AND width > 0
                """
            )

            # Idempotently backfill media_dhash_buckets for any 32-hex rows lacking buckets
            missing_bucket_rows = conn.execute(
                """
                SELECT m.id, m.dhash
                FROM media_items m
                WHERE length(m.dhash) = 32
                  AND NOT EXISTS (
                      SELECT 1 FROM media_dhash_buckets b WHERE b.media_id = m.id
                  )
                """
            ).fetchall()
            bucket_inserts: list[tuple[int, str, int]] = []
            for row in missing_bucket_rows:
                dhash_norm = _normalize_dhash(str(row["dhash"]))
                if _is_valid_128bit_dhash(dhash_norm):
                    mid = int(row["id"])
                    for idx, val in _dhash_buckets(dhash_norm):
                        bucket_inserts.append((idx, val, mid))
            if bucket_inserts:
                conn.executemany(
                    "INSERT OR IGNORE INTO media_dhash_buckets (bucket_idx, bucket_val, media_id) VALUES (?, ?, ?)",
                    bucket_inserts,
                )

            legacy_row = conn.execute(
                "SELECT 1 FROM media_items WHERE dhash != '' AND length(dhash) != 32 LIMIT 1"
            ).fetchone()
            self._has_legacy_dhash = legacy_row is not None

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

    def delete_state(self, key: str) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM app_state WHERE key = ?", (key,))

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
            self._log_insert_count += 1
            if self._log_insert_count % self._LOG_PRUNE_INTERVAL == 0:
                if max_rows <= 0:
                    conn.execute("DELETE FROM activity_logs")
                else:
                    conn.execute(
                        """
                        DELETE FROM activity_logs
                        WHERE id < COALESCE(
                            (SELECT id FROM activity_logs ORDER BY id DESC LIMIT 1 OFFSET ?),
                            0
                        )
                        """,
                        (max_rows - 1,),
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
        error_message: str = "",
    ) -> int:
        now = utc_now_iso()
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT id, status FROM archives WHERE source = ? AND source_id = ?",
                (source, source_id),
            ).fetchone()
            if existing:
                if str(existing["status"]) != "completed":
                    conn.execute(
                        """
                        UPDATE archives
                        SET filename = ?, batch_key = ?, size_bytes = ?, checksum = ?,
                            status = ?, total_files = 0, imported_files = 0,
                            duplicate_files = 0, failed_files = 0,
                            error_message = ?, completed_at = NULL, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            filename,
                            batch_key,
                            size_bytes,
                            checksum,
                            status,
                            error_message,
                            now,
                            existing["id"],
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE archives
                        SET filename = ?, batch_key = ?, size_bytes = ?, checksum = ?,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (filename, batch_key, size_bytes, checksum, now, existing["id"]),
                    )
                return int(existing["id"])
            cursor = conn.execute(
                """
                INSERT INTO archives (
                    source, source_id, filename, batch_key, size_bytes, checksum,
                    status, error_message, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source,
                    source_id,
                    filename,
                    batch_key,
                    size_bytes,
                    checksum,
                    status,
                    error_message,
                    now,
                    now,
                ),
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
                """
                SELECT * FROM media_items
                WHERE original_sha256 = ? OR final_sha256 = ?
                ORDER BY CASE WHEN original_sha256 = ? THEN 0 ELSE 1 END, id ASC
                LIMIT 1
                """,
                (original_sha256, original_sha256, original_sha256),
            ).fetchone()
            return dict(row) if row else None

    def get_media_by_id(self, media_id: int) -> dict[str, Any] | None:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM media_items WHERE id = ?",
                (media_id,),
            ).fetchone()
            return dict(row) if row else None

    def delete_media_item(self, media_id: int) -> bool:
        with self.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM media_items WHERE id = ?",
                (media_id,),
            )
            return cursor.rowcount > 0

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

        dhash_norm = _normalize_dhash(dhash)

        with self.transaction() as conn:
            # Tier 1: Exact file SHA-256 (checks both raw original and EXIF-repaired final hashes)
            row = conn.execute(
                """
                SELECT * FROM media_items
                WHERE original_sha256 = ? OR final_sha256 = ?
                ORDER BY CASE WHEN original_sha256 = ? THEN 0 ELSE 1 END, id ASC
                LIMIT 1
                """,
                (sha256, sha256, sha256),
            ).fetchone()
            if row is not None:
                return dict(row), "sha256"

            if dedup_mode == "strict_hash":
                return None, None

            # Tier 2: Exact decoded RGB pixel SHA-256 (catches EXIF/header differences, zero false positives)
            if pixel_sha256:
                px_row = conn.execute(
                    "SELECT * FROM media_items WHERE pixel_sha256 = ? ORDER BY id ASC LIMIT 1",
                    (pixel_sha256,),
                ).fetchone()
                if px_row is not None:
                    return dict(px_row), "pixel_sha256"

            # Tier 3: Guarded 128-bit perceptual dHash+vHash match across resolutions & compression levels
            if dedup_mode == "perceptual" and dhash_norm:
                exact_dhash_rows = conn.execute(
                    "SELECT * FROM media_items WHERE dhash = ? ORDER BY id ASC",
                    (dhash_norm,),
                ).fetchall()
                for cand in exact_dhash_rows:
                    if is_perceptual_match(
                        dhash_norm,
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

                if _is_valid_128bit_dhash(dhash_norm) and perceptual_max_distance < 8:
                    # Pigeonhole principle: with 8 disjoint 16-bit blocks, any 128-bit hash
                    # within Hamming distance <= 7 must share at least one exact 4-hex block.
                    bucket_vals = [val for _, val in _dhash_buckets(dhash_norm)]
                    candidate_rows = conn.execute(
                        _DHASH_BUCKET_CANDIDATES_SQL,
                        (*bucket_vals, dhash_norm),
                    ).fetchall()
                    for cand in candidate_rows:
                        if is_perceptual_match(
                            dhash_norm,
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

                    if self._has_legacy_dhash:
                        legacy_rows = conn.execute(
                            """
                            SELECT * FROM media_items
                            WHERE dhash != '' AND length(dhash) != 32 AND dhash != ?
                            ORDER BY id ASC
                            """,
                            (dhash_norm,),
                        ).fetchall()
                        for cand in legacy_rows:
                            if is_perceptual_match(
                                dhash_norm,
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
                else:
                    all_dhash_rows = conn.execute(
                        "SELECT * FROM media_items WHERE dhash != '' AND dhash != ? ORDER BY id ASC",
                        (dhash_norm,),
                    ).fetchall()
                    for cand in all_dhash_rows:
                        if is_perceptual_match(
                            dhash_norm,
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
                SELECT id, dest_rel_path, file_size, file_mtime_ns,
                       pixel_sha256, dhash, width, visual_status, origin
                FROM media_items
                """
            ).fetchall()
            result: dict[str, dict[str, Any]] = {}
            for r in rows:
                has_visual = (
                    bool(r["pixel_sha256"])
                    and len(str(r["dhash"] or "")) == 32
                    and int(r["width"] or 0) > 0
                )
                visual_status = str(r["visual_status"] or "")
                result[str(r["dest_rel_path"])] = {
                    "id": int(r["id"]),
                    "file_size": int(r["file_size"]),
                    "file_mtime_ns": int(r["file_mtime_ns"]),
                    "has_visual": has_visual,
                    "visual_status": visual_status,
                    "visual_done": has_visual or visual_status in ("ok", "undecodable"),
                    "origin": str(r["origin"]),
                }
            return result

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
        visual_status: str = "",
        filename: str,
        dest_rel_path: str,
        file_size: int,
        file_mtime_ns: int,
        taken_at: str | None,
    ) -> int:
        """Index an existing photo/video already present on the Synology NAS."""
        now = utc_now_iso()
        dhash_norm = _normalize_dhash(dhash)
        with self.transaction() as conn:
            by_path = conn.execute(
                "SELECT id FROM media_items WHERE dest_rel_path = ?",
                (dest_rel_path,),
            ).fetchone()
            if by_path:
                media_id = int(by_path["id"])
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
                        visual_status = CASE
                            WHEN ? != '' AND (? != 'skipped' OR visual_status = '') THEN ?
                            ELSE visual_status
                        END,
                        file_size = ?,
                        file_mtime_ns = ?,
                        taken_at = COALESCE(taken_at, ?)
                    WHERE id = ?
                    """,
                    (
                        sha256,
                        pixel_sha256,
                        pixel_sha256,
                        dhash_norm,
                        dhash_norm,
                        width,
                        width,
                        height,
                        height,
                        sharpness,
                        sharpness,
                        quality_score,
                        quality_score,
                        visual_status,
                        visual_status,
                        visual_status,
                        file_size,
                        file_mtime_ns,
                        taken_at,
                        media_id,
                    ),
                )
                if dhash_norm != "":
                    self._sync_dhash_buckets(conn, media_id, dhash_norm)
                return media_id

            by_hash = conn.execute(
                """
                SELECT id FROM media_items
                WHERE original_sha256 = ? OR final_sha256 = ?
                ORDER BY CASE WHEN original_sha256 = ? THEN 0 ELSE 1 END, id ASC
                LIMIT 1
                """,
                (sha256, sha256, sha256),
            ).fetchone()
            if by_hash:
                return int(by_hash["id"])

            cursor = conn.execute(
                """
                INSERT INTO media_items (
                    original_sha256, final_sha256, pixel_sha256, dhash,
                    width, height, sharpness, quality_score, visual_status, origin,
                    original_filename, dest_rel_path, file_size, file_mtime_ns,
                    taken_at, exif_status, source_archive_id, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'existing_nas', ?, ?, ?, ?, ?, 'existing', NULL, ?)
                """,
                (
                    sha256,
                    sha256,
                    pixel_sha256,
                    dhash_norm,
                    width,
                    height,
                    sharpness,
                    quality_score,
                    visual_status,
                    filename,
                    dest_rel_path,
                    file_size,
                    file_mtime_ns,
                    taken_at,
                    now,
                ),
            )
            media_id = int(cursor.lastrowid)
            self._sync_dhash_buckets(conn, media_id, dhash_norm)
            return media_id

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
        visual_status: str = "",
    ) -> int:
        now = utc_now_iso()
        dhash_norm = _normalize_dhash(dhash)
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO media_items (
                    original_sha256, final_sha256, pixel_sha256, dhash,
                    width, height, sharpness, quality_score, visual_status, origin,
                    original_filename, dest_rel_path, file_size, file_mtime_ns,
                    taken_at, latitude, longitude, exif_status,
                    source_archive_id, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'takeout', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    original_sha256,
                    final_sha256,
                    pixel_sha256,
                    dhash_norm,
                    width,
                    height,
                    sharpness,
                    quality_score,
                    visual_status,
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
            media_id = int(cursor.lastrowid)
            self._sync_dhash_buckets(conn, media_id, dhash_norm)
            return media_id

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
        visual_status: str | None = None,
        latitude: float | None = _UNSET,
        longitude: float | None = _UNSET,
    ) -> None:
        """Replace an existing media_items record in place with a higher-quality image.

        Preserves `media_id` so all Google Photos album memberships stay linked, and resets
        Synology item sync status so DSM 7 API re-binds the upgraded photo.
        """
        now = utc_now_iso()
        dhash_norm = _normalize_dhash(dhash)
        set_clauses = [
            "original_sha256 = ?",
            "final_sha256 = ?",
            "pixel_sha256 = ?",
            "dhash = ?",
            "width = ?",
            "height = ?",
            "sharpness = ?",
            "quality_score = ?",
            "original_filename = ?",
            "dest_rel_path = ?",
            "file_size = ?",
            "file_mtime_ns = ?",
            "taken_at = COALESCE(?, taken_at)",
            "exif_status = ?",
            "synology_item_id = NULL",
        ]
        params: list[Any] = [
            original_sha256,
            final_sha256,
            pixel_sha256,
            dhash_norm,
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
        ]
        if visual_status is not None:
            set_clauses.append("visual_status = ?")
            params.append(visual_status)
        if latitude is not _UNSET:
            set_clauses.append("latitude = ?")
            params.append(latitude)
        if longitude is not _UNSET:
            set_clauses.append("longitude = ?")
            params.append(longitude)
        params.append(media_id)

        with self.transaction() as conn:
            conn.execute(
                f"UPDATE media_items SET {', '.join(set_clauses)} WHERE id = ?",
                params,
            )
            self._sync_dhash_buckets(conn, media_id, dhash_norm)
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
        old_rel_path: str = "",
        old_taken_at: str = "",
        old_latitude: float | None = None,
        old_longitude: float | None = None,
    ) -> int:
        now = utc_now_iso()
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO quality_upgrades (
                    media_id, kept_rel_path, old_rel_path, old_taken_at,
                    old_latitude, old_longitude, quarantine_filename,
                    old_filename, new_filename,
                    old_width, old_height, old_file_size, old_sharpness,
                    new_width, new_height, new_file_size, new_sharpness,
                    match_reason, upgrade_reason, status, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'quarantined', ?)
                """,
                (
                    media_id,
                    kept_rel_path,
                    old_rel_path,
                    old_taken_at,
                    old_latitude,
                    old_longitude,
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

    def create_pending_quality_upgrade(
        self,
        *,
        media_id: int,
        quarantine_filename: str,
        old_rel_path: str,
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
        kept_rel_path: str = "",
        old_taken_at: str = "",
        old_latitude: float | None = None,
        old_longitude: float | None = None,
    ) -> int:
        """Create a write-ahead 'in_progress' quality upgrade journal row prior to disk swap."""
        now = utc_now_iso()
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO quality_upgrades (
                    media_id, kept_rel_path, old_rel_path, old_taken_at,
                    old_latitude, old_longitude, quarantine_filename,
                    old_filename, new_filename,
                    old_width, old_height, old_file_size, old_sharpness,
                    new_width, new_height, new_file_size, new_sharpness,
                    match_reason, upgrade_reason, status, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'in_progress', ?)
                """,
                (
                    media_id,
                    kept_rel_path,
                    old_rel_path,
                    old_taken_at,
                    old_latitude,
                    old_longitude,
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

    def finalize_quality_upgrade(
        self,
        upgrade_id: int,
        kept_rel_path: str,
        *,
        new_file_size: int | None = None,
    ) -> None:
        """Transition an 'in_progress' quality upgrade journal entry to 'quarantined'."""
        with self.transaction() as conn:
            if new_file_size is not None:
                conn.execute(
                    """
                    UPDATE quality_upgrades
                    SET status = 'quarantined', kept_rel_path = ?, new_file_size = ?
                    WHERE id = ? AND status = 'in_progress'
                    """,
                    (kept_rel_path, new_file_size, upgrade_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE quality_upgrades
                    SET status = 'quarantined', kept_rel_path = ?
                    WHERE id = ? AND status = 'in_progress'
                    """,
                    (kept_rel_path, upgrade_id),
                )

    def delete_quality_upgrade(self, upgrade_id: int) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM quality_upgrades WHERE id = ?", (upgrade_id,))

    def list_in_progress_quality_upgrades(self) -> list[dict[str, Any]]:
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM quality_upgrades WHERE status = 'in_progress' ORDER BY id ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    def list_quarantined_upgrades_batch(self, limit: int = 500) -> list[dict[str, Any]]:
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM quality_upgrades WHERE status = 'quarantined' ORDER BY id ASC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_quality_upgrades(
        self, limit: int = 100, *, include_in_progress: bool = False
    ) -> list[dict[str, Any]]:
        with self.transaction() as conn:
            if include_in_progress:
                rows = conn.execute(
                    "SELECT * FROM quality_upgrades ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM quality_upgrades WHERE status != 'in_progress' ORDER BY id DESC LIMIT ?",
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
                INSERT INTO album_memberships (album_id, media_id, sync_status, updated_at)
                VALUES (?, ?, 'pending', ?)
                ON CONFLICT(album_id, media_id) DO UPDATE SET
                    sync_status = 'pending',
                    retry_count = 0,
                    last_error = '',
                    updated_at = excluded.updated_at
                WHERE album_memberships.sync_status = 'error'
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
        self,
        album_id: int,
        media_id: int,
        *,
        status: str,
        error: str = "",
        increment_retry: bool | None = None,
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
                should_increment = (
                    (status == "error") if increment_retry is None else increment_retry
                )
                if should_increment:
                    conn.execute(
                        """
                        UPDATE album_memberships
                        SET sync_status = ?, retry_count = retry_count + 1,
                            last_error = ?, updated_at = ?
                        WHERE album_id = ? AND media_id = ?
                        """,
                        (status, error, now, album_id, media_id),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE album_memberships
                        SET sync_status = ?, last_error = ?, updated_at = ?
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
                    SUM(CASE WHEN status = 'completed_with_errors' THEN 1 ELSE 0 END) AS completed_with_errors_archives,
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
                    SUM(CASE WHEN status != 'in_progress' THEN 1 ELSE 0 END) AS total_upgrades,
                    SUM(CASE WHEN status = 'quarantined' THEN 1 ELSE 0 END) AS quarantined_count
                FROM quality_upgrades
                """
            ).fetchone()

            return {
                "total_media": int(media_row["migrated_media"] or 0),
                "takeout_media": int(media_row["migrated_media"] or 0),
                "existing_nas_indexed": int(media_row["existing_nas_indexed"] or 0),
                "total_library_items": int(media_row["total_library_items"] or 0),
                "total_bytes": int(media_row["migrated_bytes"] or 0),
                "exif_repaired": int(media_row["exif_repaired"] or 0),
                "total_archives": int(archive_row["total_archives"] or 0),
                "completed_archives": int(archive_row["completed_archives"] or 0),
                "completed_with_errors_archives": int(
                    archive_row["completed_with_errors_archives"] or 0
                ),
                "failed_archives": int(archive_row["failed_archives"] or 0),
                "total_duplicates_skipped": int(archive_row["total_duplicates_skipped"] or 0),
                "total_albums": int(album_row["total_albums"] or 0),
                "synced_album_items": int(album_row["synced_memberships"] or 0),
                "pending_album_items": int(album_row["pending_memberships"] or 0),
                "quality_upgrades_count": int(upgrade_row["total_upgrades"] or 0),
                "quarantined_count": int(upgrade_row["quarantined_count"] or 0),
            }

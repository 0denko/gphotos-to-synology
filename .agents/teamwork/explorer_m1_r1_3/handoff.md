# Explorer 3 (M1 R1) Handoff — `src/gphotos_synology/database.py` & All Callers

**Core Summary:** Completed a line-by-line analysis of `src/gphotos_synology/database.py` (940 lines), audited every `StateDB` caller across `src/` and `tests/`, and verified SQLite 3.40.1 runtime behavior (`WITHOUT ROWID` + `ON DELETE CASCADE` + query plans). Identified **two critical SQLite / threshold insights** before implementation:
1. **SQLite query plan trap:** `(b.bucket_idx, b.bucket_val) IN ((0, ?), ...)` causes a full table `SCAN b` in SQLite 3.40.1, whereas an 8-way `UNION` subquery on `media_dhash_buckets` uses 8 exact primary-key index lookups (`SEARCH media_dhash_buckets USING PRIMARY KEY (bucket_idx=? AND bucket_val=?)`) with zero `TEMP B-TREE` sort/distinct overhead on `media_items`.
2. **Threshold interaction with `config.py:62`:** With 8 blocks of 4 hex chars (`16 bits` each), the pigeonhole principle guarantees 100% exact recall for `effective_max <= 7` (`< 8`). Today `config.py:62` defaults `perceptual_max_distance = 10` (`>= 8`), which hits the full-scan fallback at default settings unless `config.py` lowers the default to `<= 7` (empirical measurements on all existing upgrade/dedup tests show real matches are at Hamming distance `1–3`).

---

## 1. Environment & SQLite Verification

Verified directly in `/home/antigravity/gphotos-to-synology` via `.venv/bin/python`:
- **SQLite version:** `3.40.1`
- **`WITHOUT ROWID` + Foreign Key `ON DELETE CASCADE` + secondary index (`media_id`):** Empirically tested and passed (`DELETE FROM media_items WHERE id = ?` cascaded and deleted all 8 bucket rows cleanly with `PRAGMA foreign_keys = ON`).
- **`OFFSET`-based `activity_logs` pruning:** Empirically tested and passed (`DELETE FROM activity_logs WHERE id < COALESCE((SELECT id FROM activity_logs ORDER BY id DESC LIMIT 1 OFFSET ?), 0)` keeps exact `max_rows` rows and deletes 0 rows when count `< max_rows`).

---

## 2. Line-by-Line Audit of `src/gphotos_synology/database.py`

### 2.1 Connection, Locking, WAL & Migrations (`database.py:18-183`)
- **`SCHEMA_SQL` (`lines 18-129`)**:
  - Sets `PRAGMA journal_mode = WAL;`, `PRAGMA foreign_keys = ON;`, `PRAGMA busy_timeout = 30000;`.
  - 7 tables: `archives`, `media_items`, `quality_upgrades`, `albums`, `album_memberships`, `activity_logs`, `app_state`.
- **Connection lifecycle (`lines 135-159`)**:
  - `self._lock = threading.RLock()` serializes all transactions in-process.
  - `_connect()` opens a fresh connection per `transaction()` with `timeout=30.0`, `check_same_thread=False`, `row_factory=sqlite3.Row`, and executes `PRAGMA foreign_keys = ON;` on every connection (WAL mode persists in DB header).
  - `transaction()` commits on clean exit, rolls back on exception, and always closes `conn`.
- **`_init_schema()` (`lines 160-183`)**:
  - Runs `conn.executescript(SCHEMA_SQL)` (note: `executescript` implicitly commits before running; subsequent `ALTER TABLE` / backfill statements run inside the `transaction()` block and commit cleanly on exit).
  - Inspects `PRAGMA table_info(media_items)` and runs idempotent `ALTER TABLE media_items ADD COLUMN ...` for 8 historical columns (`pixel_sha256`, `dhash`, `width`, `height`, `sharpness`, `quality_score`, `origin`, `file_mtime_ns`), plus `CREATE INDEX IF NOT EXISTS` for `final_sha256`, `pixel_sha256`, `dhash`.
  - **Missing for v1.2.0:**
    - `media_items.visual_status TEXT NOT NULL DEFAULT ''`
    - `media_dhash_buckets` table (`WITHOUT ROWID` + `idx_dhash_buckets_media_id`) and idempotent backfill from existing 32-hex `media_items` rows
    - `quality_upgrades` additive columns: `old_rel_path TEXT NOT NULL DEFAULT ''`, `old_taken_at TEXT NOT NULL DEFAULT ''`, `old_latitude REAL`, `old_longitude REAL`

### 2.2 Complete Method & Caller Inventory

| `StateDB` Method (lines) | Current Behavior / Defects | Every Caller (`src/` & `tests/`) | Required M1 Changes |
|---|---|---|---|
| `get_state(key, default=None)` (`187-190`) | Reads `app_state` | `routes.py:408`, `scheduler.py:43`, `pipeline.py:774,779,794` | Keep unchanged |
| `set_state(key, value)` (`192-202`) | Upserts `app_state` | `routes.py:308,372`, `scheduler.py:80,87`, `pipeline.py:775,810` | Keep unchanged |
| *(NEW)* `delete_state(key)` | Missing (blocks G3 one-time OAuth state invalidation) | Will be called by `routes.py` (M3) | Add `DELETE FROM app_state WHERE key = ?` |
| `log_event(level, component, message, max_rows=2000)` (`207-222`) | **Defect:** Runs `DELETE ... WHERE id NOT IN (SELECT id ... LIMIT ?)` on *every* insert | `routes.py` (13 calls), `scheduler.py:64,71,83,90`, `pipeline.py:94` | Prune every `_LOG_PRUNE_INTERVAL = 50` inserts (track `self._log_insert_count` under `self._lock`); use $O(\log N)$ primary-key `OFFSET` cutoff |
| `get_recent_logs(limit=100)` (`224-230`) | Returns newest `limit` rows | `routes.py:147` | Keep unchanged |
| `get_archive_by_source(source, source_id)` (`235-241`) | Lookup by `(source, source_id)` | `pipeline.py:178,202` | Keep unchanged |
| `upsert_archive(...)` (`243-281`) | **Defect:** On existing row (`lines 261-269`), updates metadata/status/error_message but does **not** reset `total_files`, `imported_files`, `duplicate_files`, `failed_files`, or `completed_at` on retry | `pipeline.py:239,273` | Reset `total_files = 0, imported_files = 0, duplicate_files = 0, failed_files = 0, completed_at = NULL` on update |
| `update_archive_progress(...)` (`283-322`) | Dynamic field update; sets `completed_at` iff `completed=True` | `pipeline.py:258,299,315,357,487,494` | Keep signature; update schema comment for `completed_with_errors` |
| `list_archives(limit=50)` (`324-330`) | Newest `limit` archives | `routes.py:137` | Keep unchanged |
| `get_media_by_original_hash(original_sha256)` (`335-341`) | **Defect:** `WHERE original_sha256 = ? OR final_sha256 = ?` with **no `ORDER BY`** | `pipeline.py:449` | Add `ORDER BY CASE WHEN original_sha256 = ? THEN 0 ELSE 1 END, id ASC LIMIT 1` |
| `find_duplicate_media(...)` (`343-426`) | **Defects:** (1) Tier 1 (`366`) has no `ORDER BY` preferring `original_sha256`; (2) Tier 3 (`406-409`) full-scans all `dhash != ''` rows into Python per lookup ($O(N \times M)$ inside `self._lock`) | `pipeline.py:402` | (1) Deterministic Tier 1 `ORDER BY CASE WHEN original_sha256 = ? THEN 0 ELSE 1 END, id ASC LIMIT 1`; (2) Deterministic `ORDER BY id ASC` in Tier 2 & Tier 3 exact match; (3) Normalize `dhash` lowercase; (4) Use `media_dhash_buckets` 8-way `UNION` when `len(dhash) == 32 and perceptual_max_distance < 8` (+ legacy 16-char supplement if `_has_legacy_dhash`), fallback full scan otherwise |
| `get_indexed_files_by_rel_path()` (`428-446`) | Returns `{rel_path: {id, file_size, file_mtime_ns, has_visual, origin}}`; no `visual_status` / `visual_done` (causes G10 scanner re-hash loop on undecodable files) | `scanner.py:60` (checks `file_size`, `file_mtime_ns`, `has_visual` at `scanner.py:86-90`) | Preserve all 5 existing keys verbatim; add `"visual_status": str` and `"visual_done": has_visual or visual_status in ("ok", "undecodable")` |
| `upsert_existing_nas_media(...)` (`448-543`) | 3 branches: (A) update by `dest_rel_path`, (B) match by hash (needs `ORDER BY`), (C) insert new. No `visual_status`, no bucket sync | `scanner.py:96` | Add `visual_status: str = ""`; normalize `dhash`; deterministic `ORDER BY` in Branch B; smart `visual_status` update in Branch A (don't clobber `'ok'` with `'skipped'`); call `_sync_dhash_buckets` in Branch A (when `dhash != ""`) and Branch C |
| `prune_missing_existing_nas_media(present_rel_paths)` (`545-560`) | Deletes `origin = 'existing_nas'` rows not in `present_rel_paths` in chunks of 500 | `scanner.py:119` | `ON DELETE CASCADE` on `media_dhash_buckets(media_id)` automatically prunes buckets cleanly (verify in tests) |
| `record_media_item(...)` (`562-617`) | Inserts `origin='takeout'` row; no `visual_status`, no bucket sync | `pipeline.py:459` | Add `visual_status: str = ""`; normalize `dhash`; call `_sync_dhash_buckets(conn, media_id, dhash)` in same transaction |
| `upgrade_media_item(...)` (`619-690`) | Replaces `media_items` row in place, resets `synology_item_id = NULL` and `album_memberships` to `pending` / `retry_count = 0`; no bucket sync, no optional `latitude`/`longitude`/`visual_status` | `pipeline.py:597,662` | Normalize `dhash`; add optional `visual_status: str | None = None` and `_UNSET`-defaulted `latitude` / `longitude` (so M4 restore can restore exact GPS including `None`, while existing callers leave them untouched); call `_sync_dhash_buckets(conn, media_id, dhash)` in same transaction |
| *(NEW)* `get_media_by_id(media_id)` & `delete_media_item(media_id)` | Helpful primitives for G2 missing-file repair & crash recovery without raw SQL in `pipeline.py` | Will be used by `pipeline.py` (M4) | `get_media_by_id(int) -> dict \| None`; `delete_media_item(int) -> bool` (cascades to buckets & memberships) |
| `set_media_synology_id(media_id, synology_item_id)` (`692-697`) | Updates `synology_item_id` only | `pipeline.py:742` | Keep unchanged |
| `record_quality_upgrade(...)` (`702-753`) | Inserts row with `status='quarantined'`; missing `old_rel_path`, `old_taken_at`, `old_latitude`, `old_longitude` | `pipeline.py:614` | Add optional `old_rel_path=""`, `old_taken_at=""`, `old_latitude=None`, `old_longitude=None` (100% backwards-compatible) |
| *(NEW)* `create_pending_quality_upgrade(...)` | Missing (G2 write-ahead journal) | Will be called by `pipeline.py` (M4) | Inserts `quality_upgrades` row with `status='in_progress'`, storing `old_rel_path`, `old_taken_at`, `old_latitude`, `old_longitude`, `kept_rel_path=""` default |
| *(NEW)* `finalize_quality_upgrade(upgrade_id, kept_rel_path, *, new_file_size=None)` | Missing (G2 journal commit) | Will be called by `pipeline.py` (M4) | Updates `status = 'quarantined'`, `kept_rel_path = ?` (and optional `new_file_size`) `WHERE id = ? AND status = 'in_progress'` |
| *(NEW)* `delete_quality_upgrade(upgrade_id)` | Missing (G2 rollback cleanup) | Will be called by `pipeline.py` (M4) | `DELETE FROM quality_upgrades WHERE id = ?` |
| *(NEW)* `list_in_progress_quality_upgrades()` | Missing (G2 startup crash recovery) | Will be called by `pipeline.py` (M4) | `SELECT * FROM quality_upgrades WHERE status = 'in_progress' ORDER BY id ASC` |
| *(NEW)* `list_quarantined_upgrades_batch(limit=500)` | Missing (fixes 1000-row silent cap in `purge_all_quarantined`) | Will be called by `pipeline.py` (M4) | `SELECT * FROM quality_upgrades WHERE status = 'quarantined' ORDER BY id ASC LIMIT ?` |
| `list_quality_upgrades(limit=100)` (`755-761`) | Returns all rows (`ORDER BY id DESC LIMIT ?`) | `routes.py:155`, `pipeline.py:699`, `test_visual_quality_upgrade.py:88,142,157` | Add `include_in_progress: bool = False` (default excludes transient `'in_progress'` journal rows from UI while remaining 100% compatible with existing callers/tests) |
| `get_quality_upgrade(upgrade_id)` (`763-769`) | Lookup by ID | `routes.py:166`, `pipeline.py:641,687` | Keep unchanged |
| `mark_quality_upgrade_status(upgrade_id, status)` (`771-777`) | Sets `status` & `resolved_at` | `pipeline.py:680,692` | Keep unchanged |
| `add_media_to_album(album_name, media_id)` (`782-802`) | Creates album if needed + `INSERT OR IGNORE` membership | `pipeline.py:430,453,479` | Keep `'synced'`/`'pending'` intact; optionally revive `'error'` rows (`ON CONFLICT(album_id, media_id) DO UPDATE SET sync_status='pending', retry_count=0, last_error='', updated_at=excluded.updated_at WHERE album_memberships.sync_status = 'error'`) |
| `set_album_synology_id(album_id, synology_album_id)` (`804-810`) | Sets `synology_album_id` | `pipeline.py:733` | Keep unchanged |
| `get_pending_album_memberships(limit=500)` (`812-834`) | Selects `sync_status IN ('pending', 'error') AND retry_count < 20` | `pipeline.py:711` | Keep unchanged |
| `mark_album_membership(album_id, media_id, *, status, error="")` (`836-859`) | **Defect:** Increments `retry_count` whenever `status != 'synced'`, burning 20 retries while waiting for Synology Photos indexing (`status='pending'`) | `pipeline.py:746` (`status="pending"`), `pipeline.py:761` (`status="synced"`) | Add `increment_retry: bool \| None = None`, defaulting to `(status == "error")`. Only increment `retry_count` when `should_increment` is True! |
| `list_albums_summary()` (`861-878`) | Aggregates album counts | `routes.py:142`, `test_existing_nas_dedup.py:145`, `test_visual_quality_upgrade.py:100` | Keep unchanged |
| `get_stats()` (`883-939`) | Returns 14 dashboard metric keys | `routes.py:119`, `profiles.py:173`, `app.js:120-126,161`, `test_api.py:37`, `test_profiles.py:87-88`, `test_pipeline_e2e.py:131`, `test_existing_nas_dedup.py:68,80,139,201`, `test_visual_quality_upgrade.py:158` | Keep all 14 existing keys with exact semantics; add `"completed_with_errors_archives"`; exclude `'in_progress'` from `quality_upgrades_count` |

---

## 3. Deep Verification of Key SQL, Math & Edge Cases

### 3.1 `media_dhash_buckets` Schema, Synchronization & Pigeonhole Math (G11)

**Table & Index DDL (added to `SCHEMA_SQL`):**
```sql
CREATE TABLE IF NOT EXISTS media_dhash_buckets (
    bucket_idx INTEGER NOT NULL,
    bucket_val TEXT NOT NULL,
    media_id INTEGER NOT NULL REFERENCES media_items(id) ON DELETE CASCADE,
    PRIMARY KEY (bucket_idx, bucket_val, media_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_dhash_buckets_media_id ON media_dhash_buckets(media_id);
```
- **Why `idx_dhash_buckets_media_id` is mandatory:** Without it, `DELETE FROM media_dhash_buckets WHERE media_id = ?` (both explicit bucket updates and `ON DELETE CASCADE` during `prune_missing_existing_nas_media`) would full-scan the `WITHOUT ROWID` B-tree.

**Normalization & Validation Helpers:**
```python
_HEX_CHARS = frozenset("0123456789abcdef")

def _normalize_dhash(dhash: str) -> str:
    return (dhash or "").strip().lower()

def _is_valid_128bit_dhash(dhash: str) -> bool:
    return len(dhash) == 32 and all(ch in _HEX_CHARS for ch in dhash)

def _dhash_buckets(dhash: str) -> list[tuple[int, str]]:
    return [(i, dhash[i * 4 : (i + 1) * 4]) for i in range(8)]
```
- `_compute_composite_dhash_hex()` (`src/gphotos_synology/takeout/dedup.py:119`) returns `f"{h_bits:016x}{v_bits:016x}"` (always 32 lowercase hex chars). Normalizing on both write and read guarantees uppercase inputs in tests or legacy DBs never miss a bucket or exact match.

**Single Sync Primitive (`_sync_dhash_buckets(conn, media_id, dhash_norm)`):**
```python
def _sync_dhash_buckets(self, conn: sqlite3.Connection, media_id: int, dhash_norm: str) -> None:
    conn.execute("DELETE FROM media_dhash_buckets WHERE media_id = ?", (media_id,))
    if _is_valid_128bit_dhash(dhash_norm):
        conn.executemany(
            "INSERT INTO media_dhash_buckets (bucket_idx, bucket_val, media_id) VALUES (?, ?, ?)",
            [(idx, val, media_id) for idx, val in _dhash_buckets(dhash_norm)],
        )
    elif dhash_norm:
        self._has_legacy_dhash = True
```
**Exhaustive Verification of EVERY `media_items` Mutation Site:**
1. `record_media_item` → INSERT → calls `_sync_dhash_buckets(conn, media_id, dhash_norm)` in same txn.
2. `upsert_existing_nas_media` Branch A (`by_path` UPDATE) → updates `dhash` only when `dhash_norm != ""` (`CASE WHEN ? != '' THEN ? ELSE dhash END`), so call `_sync_dhash_buckets(conn, media_id, dhash_norm)` whenever `dhash_norm != ""`. (When `dhash_norm == ""`, existing `dhash` and its buckets are preserved.)
3. `upsert_existing_nas_media` Branch B (`by_hash` hit) → returns existing `id` without modifying `dhash`.
4. `upsert_existing_nas_media` Branch C (INSERT) → calls `_sync_dhash_buckets(conn, media_id, dhash_norm)` in same txn.
5. `upgrade_media_item` (UPDATE in place) → always overwrites `dhash`, so always calls `_sync_dhash_buckets(conn, media_id, dhash_norm)` in same txn.
6. `prune_missing_existing_nas_media` (DELETE) → `ON DELETE CASCADE` removes all bucket rows automatically.
7. `delete_media_item` (DELETE) → `ON DELETE CASCADE` removes all bucket rows automatically.
8. `_init_schema()` migration backfill → normalizes any uppercase `dhash` in `media_items` (`UPDATE media_items SET dhash = lower(dhash) WHERE dhash != lower(dhash)`), then populates missing buckets for all `length(dhash) = 32` rows via `NOT EXISTS (SELECT 1 FROM media_dhash_buckets b WHERE b.media_id = m.id)`.

**Pigeonhole Proof & SQLite `EXPLAIN QUERY PLAN` Verification:**
- Partition 128 bits into 8 disjoint 16-bit blocks (`4 hex chars` each). If two 128-bit hashes have Hamming distance $d \le 7$ (`effective_max < 8`), then $\sum_{i=0}^{7} d_i \le 7 \implies \exists\, i \in \{0..7\}$ with $d_i = 0$ (exact 16-bit block match).
- In `takeout/dedup.py:372-373`:
  ```python
  effective_len = min(len(incoming_dhash), len(existing_dhash))
  effective_max = max_distance if effective_len == 32 else max(2, max_distance // 2)
  ```
  For 32-hex comparisons, `effective_max == perceptual_max_distance`.
- **Verified SQLite 3.40.1 Query Plans:**
  - **DO NOT USE** `WHERE (b.bucket_idx, b.bucket_val) IN ((0, ?), ...)` → SQLite produces `SCAN b` (full table scan!).
  - **USE 8-way `UNION` subquery:**
    ```sql
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
    ```
    Verified `EXPLAIN QUERY PLAN`: 8× `SEARCH media_dhash_buckets USING PRIMARY KEY (bucket_idx=? AND bucket_val=?)` → `UNION` deduplicates integer `media_id`s → `SEARCH m USING INTEGER PRIMARY KEY (rowid=?)` (zero temp B-tree sort/distinct on `media_items`!).
- **Mixed Legacy 16-char Hash Recall:**
  - Initialize `self._has_legacy_dhash` in `_init_schema()` via `SELECT 1 FROM media_items WHERE dhash != '' AND length(dhash) != 32 LIMIT 1`.
  - When `find_duplicate_media` takes the bucketed fast path (`_is_valid_128bit_dhash(dhash_norm) and perceptual_max_distance < 8`), if `self._has_legacy_dhash` is `True`, also check `SELECT * FROM media_items WHERE dhash != '' AND length(dhash) != 32 AND dhash != ? ORDER BY id ASC`. Zero cost on modern DBs, 100% recall on migrated legacy DBs!
  - When `perceptual_max_distance >= 8` or `not _is_valid_128bit_dhash(dhash_norm)`, fall back to `SELECT * FROM media_items WHERE dhash != '' AND dhash != ? ORDER BY id ASC`.
- **Important Cross-File Note for M1 Config Coordination:**
  - `config.py:62` currently defaults `perceptual_max_distance = 10` (`>= 8`), meaning default configs take the fallback full scan rather than the bucket index.
  - Empirically verified Hamming distances on existing `tests/test_visual_quality_upgrade.py` images: `800×600 vs 320×240` = **3**, `sharp vs GaussianBlur(3.0)` = **3**, `640×480 vs 240×180` = **1**; `tests/test_existing_nas_dedup.py:165` uses `perceptual_max_distance=4`.
  - Recommend confirming in M1 whether `config.py` keeps `default=10` (with fallback guaranteeing 100% recall) or lowers default to `6` or `7` (activating the bucket index by default and tightening G11 simultaneously).

### 3.2 `get_indexed_files_by_rel_path()` Backwards Compatibility (G10)
- Only caller: `src/gphotos_synology/synology/scanner.py:60,85-93`.
- Return dict per `dest_rel_path`:
  ```python
  has_visual = bool(r["pixel_sha256"]) and len(str(r["dhash"] or "")) == 32 and int(r["width"] or 0) > 0
  visual_status = str(r["visual_status"] or "")
  return {
      "id": int(r["id"]),
      "file_size": int(r["file_size"]),
      "file_mtime_ns": int(r["file_mtime_ns"]),
      "has_visual": has_visual,
      "visual_status": visual_status,
      "visual_done": has_visual or visual_status in ("ok", "undecodable"),
      "origin": str(r["origin"]),
  }
  ```
- **Upgrade behavior on v1.1.0 DBs:**
  - In `_init_schema()`, backfill `visual_status = 'ok'` ONLY for rows where `visual_status = '' AND pixel_sha256 != '' AND length(dhash) = 32 AND width > 0`.
  - Pre-existing v1.1.0 HEIC rows (which had `pixel_sha256=''`, `dhash=''`, `width=0`) keep `visual_status = ''` → `visual_done = False` → scanner re-indexes them **once** after upgrading to v1.2.0 (`pillow-heif`), setting `visual_status = 'ok'`.
  - Genuinely undecodable images (corrupt JPEG, unsupported RAW) get `visual_status = 'undecodable'` → `visual_done = True` → scanner skips them on all future cycles unless `file_size` or `file_mtime_ns` changes.
- **`upsert_existing_nas_media` Branch A `visual_status` guard:**
  ```sql
  visual_status = CASE
      WHEN ? != '' AND (? != 'skipped' OR visual_status = '') THEN ?
      ELSE visual_status
  END
  ```
  Ensures switching temporarily to `strict_hash` (`visual_status='skipped'`) never clobbers an existing `'ok'` visual status, while `'undecodable' -> 'ok'` transitions cleanly!

### 3.3 `get_stats()` Backwards Compatibility (G1)
- All 14 existing keys remain unchanged (`total_media`, `existing_nas_indexed`, `total_library_items`, `total_bytes`, `exif_repaired`, `total_archives`, `completed_archives`, `failed_archives`, `total_duplicates_skipped`, `total_albums`, `synced_album_items`, `pending_album_items`, `quality_upgrades_count`, `quarantined_count`).
- Add 15th key: `"completed_with_errors_archives": int(archive_row["completed_with_errors_archives"] or 0)`.
- Change `quality_upgrades_count` aggregation to `SUM(CASE WHEN status != 'in_progress' THEN 1 ELSE 0 END)` so transient G2 journal rows do not appear in dashboard counts.
- Verified every caller (`web/routes.py:119`, `profiles.py:173`, `web/static/app.js:120-126,161`, and all 5 test files): none assert exact key equality on `get_stats()` — all access individual keys safely.

### 3.4 `log_event` Periodic Pruning
- Add `self._log_insert_count = 0` in `StateDB.__init__` and class constant `_LOG_PRUNE_INTERVAL = 50`.
- Inside `log_event(..., max_rows: int = 2000)`:
  - Insert log row and increment `self._log_insert_count += 1` (already inside `self._lock`).
  - Only prune when `self._log_insert_count % self._LOG_PRUNE_INTERVAL == 0`:
    ```python
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
    ```
  - Optional: expose `prune_logs(self, max_rows: int = 2000) -> int` helper if explicit pruning is ever wanted.

---

## 4. Concrete Design Blueprint (`database.py` + `tests/test_database.py`)

### 4.1 `src/gphotos_synology/database.py` Changes
1. **Schema (`SCHEMA_SQL`)**:
   - Update `archives.status` comment to include `completed_with_errors`.
   - Add `visual_status TEXT NOT NULL DEFAULT ''` to `media_items`.
   - Add `media_dhash_buckets` (`WITHOUT ROWID`, `ON DELETE CASCADE`) + `idx_dhash_buckets_media_id`.
   - Add `old_rel_path TEXT NOT NULL DEFAULT ''`, `old_taken_at TEXT NOT NULL DEFAULT ''`, `old_latitude REAL`, `old_longitude REAL` to `quality_upgrades`; update status comment to include `in_progress`.
2. **Idempotent `_init_schema()` Migrations**:
   - `media_items`: add `visual_status` if absent; backfill `visual_status = 'ok'` where already visually fingerprinted; normalize any uppercase `dhash` to lowercase.
   - `quality_upgrades`: inspect `PRAGMA table_info(quality_upgrades)` and add `old_rel_path`, `old_taken_at`, `old_latitude`, `old_longitude` if absent.
   - `media_dhash_buckets`: ensure index exists; backfill missing buckets for all `length(dhash) = 32` rows via `NOT EXISTS`.
   - Compute `self._has_legacy_dhash`.
3. **Methods Added / Updated**:
   - `delete_state(key: str) -> None`
   - `log_event(...)` periodic pruning every 50 inserts using primary-key `OFFSET`
   - `upsert_archive(...)` resets `total_files`, `imported_files`, `duplicate_files`, `failed_files`, `completed_at = NULL` on retry
   - `get_media_by_original_hash(...)`, `find_duplicate_media(...)` Tier 1, `upsert_existing_nas_media(...)` Branch B: deterministic `ORDER BY CASE WHEN original_sha256 = ? THEN 0 ELSE 1 END, id ASC LIMIT 1`
   - `find_duplicate_media(...)` Tier 3: lowercase `dhash`, 8-way `UNION` bucket query when `len(dhash) == 32 and perceptual_max_distance < 8` (+ legacy supplement if `_has_legacy_dhash`), full-scan fallback otherwise
   - `get_indexed_files_by_rel_path()` adds `visual_status` and `visual_done`
   - `record_media_item(...)` & `upsert_existing_nas_media(...)` accept `visual_status: str = ""` and sync `media_dhash_buckets`
   - `upgrade_media_item(...)` syncs `media_dhash_buckets` and accepts optional `visual_status`, `latitude`, `longitude` (sentinel `_UNSET`)
   - `get_media_by_id(media_id: int) -> dict[str, Any] | None` & `delete_media_item(media_id: int) -> bool`
   - `record_quality_upgrade(...)` accepts optional `old_rel_path`, `old_taken_at`, `old_latitude`, `old_longitude`
   - `create_pending_quality_upgrade(...) -> int`, `finalize_quality_upgrade(upgrade_id: int, kept_rel_path: str, *, new_file_size: int | None = None) -> None`, `delete_quality_upgrade(upgrade_id: int) -> None`, `list_in_progress_quality_upgrades() -> list[dict[str, Any]]`, `list_quarantined_upgrades_batch(limit: int = 500) -> list[dict[str, Any]]`
   - `list_quality_upgrades(limit: int = 100, *, include_in_progress: bool = False)`
   - `mark_album_membership(album_id, media_id, *, status, error="", increment_retry: bool | None = None)`
   - `get_stats()` adds `completed_with_errors_archives` and excludes `in_progress` from `quality_upgrades_count`

### 4.2 New `tests/test_database.py` Coverage Matrix
1. **Additive v1.1.0 → v1.2.0 Migration Test**: Construct a genuine v1.1.0 SQLite DB (without `visual_status`, `media_dhash_buckets`, `old_rel_path`/`old_taken_at`/`old_latitude`/`old_longitude`) populated with 32-hex, uppercase 32-hex, legacy 16-hex, and HEIC empty-visual rows; open `StateDB(db_path)` twice (idempotency); assert columns added, buckets backfilled & lowercased, already-visual rows got `visual_status='ok'`, HEIC empty-visual rows stayed `visual_status=''` (`visual_done=False`).
2. **App State CRUD**: `get_state` default, `set_state` upsert, `delete_state` removes key and is idempotent on missing key.
3. **Hash Lookup Ordering**: Insert row A (`original_sha256="H_A"`, `final_sha256="H_SHARED"`) then row B (`original_sha256="H_SHARED"`, `final_sha256="H_B"`); assert both `get_media_by_original_hash("H_SHARED")` and `find_duplicate_media(sha256="H_SHARED")` return row B (exact `original_sha256` match wins over `final_sha256`!).
4. **`media_dhash_buckets` Sync & Recall**:
   - Insert 32-hex dhash → 8 bucket rows created.
   - Hamming distance 0, 1, 7 (7 bits flipped across 7 different blocks) matched via bucket path (`perceptual_max_distance=7`); distance 8 across 8 blocks NOT matched at `max_distance=7`, and MATCHED via fallback at `max_distance=8` and `10`.
   - `upgrade_media_item` replaces old buckets with new buckets (old hash no longer in `media_dhash_buckets`).
   - `upsert_existing_nas_media` updating same `dest_rel_path` with new dhash replaces buckets; updating with empty dhash preserves buckets and `'ok'` `visual_status`.
   - `prune_missing_existing_nas_media` and `delete_media_item` cascade-delete bucket rows (0 orphans).
   - Legacy 16-char hash still matches via Tier 3 even when `perceptual_max_distance < 8`.
5. **`visual_status` & `get_indexed_files_by_rel_path`**: Verify `'ok'`, `'undecodable'`, `'skipped'`, `''` transitions and `has_visual` vs `visual_done` values.
6. **`quality_upgrades` Journal Lifecycle**: `create_pending_quality_upgrade` (`status='in_progress'`, hidden from default `list_quality_upgrades` and `get_stats()["quality_upgrades_count"]`, returned by `list_in_progress_quality_upgrades`), `finalize_quality_upgrade` transitions to `'quarantined'` with updated `kept_rel_path`, `delete_quality_upgrade` removes rolled-back row, `list_quarantined_upgrades_batch` paginates cleanly past 500/1000 rows, `old_rel_path`/`old_taken_at`/`old_latitude`/`old_longitude` persisted.
7. **`upsert_archive` Retry Reset & `completed_with_errors` Stats**: Insert archive, set counts + `completed_with_errors` (`completed=False`), verify `get_stats()["completed_with_errors_archives"] == 1` and `completed_archives == 0`; call `upsert_archive` again for retry and verify `total_files`/`imported_files`/`duplicate_files`/`failed_files` reset to 0 and `completed_at` reset to `None`.
8. **`mark_album_membership` Retry Counting**: Call `mark_album_membership(..., status="pending", error="Waiting...")` 25 times → `retry_count` stays `0`, item still returned by `get_pending_album_memberships()`. Call `status="error"` 20 times → `retry_count` reaches `20` and item drops out of `get_pending_album_memberships()`. Verify explicit `increment_retry` override and `status="synced"` clearing `last_error`.
9. **`log_event` Periodic Pruning**: Insert 49 rows with `max_rows=10` → still 49 rows (no prune on every insert); 50th insert triggers prune → exact 10 newest rows remain.

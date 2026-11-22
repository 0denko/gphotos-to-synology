# Challenger 2 (`challenger_m1_r1_2_gen2`) — Adversarial Stress Testing Report (`database.py`)

## Scope
Isolated adversarial stress testing against [`src/gphotos_synology/database.py`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py) for Milestone M1 (`v1.2.0` Foundation).

All ad-hoc probe scripts and SQLite databases were staged exclusively inside a temporary directory created via `mktemp -d -- "${TMPDIR:-/tmp}/teamwork_probe.XXXXXX"` (`chmod 700`) and removed upon completion via `trap 'rm -rf -- "$PROBE_DIR"' EXIT`. Zero tracked or untracked project files were modified.

---

## Adversarial Probe Suites & Findings

### 1. Legacy `v1.1.0` → `v1.2.0` SQLite Additive Migration & Idempotency
- **Setup**: Built a raw `v1.1.0` SQLite database lacking `media_items.visual_status`, `media_dhash_buckets`, and the 4 new `quality_upgrades` columns (`old_rel_path`, `old_taken_at`, `old_latitude`, `old_longitude`). Populated it with:
  1. Lowercase 32-char hex `dhash` fingerprinted photo (`visual_status` should backfill to `'ok'`).
  2. Uppercase 32-char hex `dhash` fingerprinted photo (`dhash` should normalize to lowercase; `visual_status` should backfill to `'ok'`).
  3. Mixed-case 32-char hex `dhash` fingerprinted photo (`dhash` should normalize to lowercase; `visual_status` should backfill to `'ok'`).
  4. Mixed-case 16-char legacy `dhash` (`dhash` lowercased, `_has_legacy_dhash=True`, 0 bucket rows).
  5. Un-fingerprinted pre-`v1.2.0` HEIC row (`pixel_sha256=''`, `dhash=''`, `width=0` → `visual_status` preserved as `''` and `visual_done=False` so the `v1.2.0` scanner with `pillow-heif` re-indexes it once).
  6. Malformed 32-char non-hex `dhash` (`"z" * 32` → rejected by `_is_valid_128bit_dhash`, 0 bucket rows).
  7. Existing `quality_upgrades` row (`id=1`, `status='quarantined'`).
- **Verification**: Instantiated `StateDB` 4 consecutive times on the same database file.
- **Result**: **PASS** — Additive columns and indexes created cleanly; all `dhash` values lowercased; `media_dhash_buckets` populated with exactly `3 * 8 = 24` rows across all 4 instantiations; pre-`v1.2.0` HEIC row kept `visual_status=''` (`visual_done=False`); existing `quality_upgrades` row preserved with zero data loss and default values on new columns (`old_rel_path=''`, `old_taken_at=''`, `old_latitude=None`, `old_longitude=None`).

### 2. `media_dhash_buckets` 8-Way `UNION` Query Plan, 100% Recall & Stale-Bucket Prevention
- **`EXPLAIN QUERY PLAN` Verification**:
  - Executed `EXPLAIN QUERY PLAN` on `_DHASH_BUCKET_CANDIDATES_SQL` against a populated database (1 target + 200 random 128-bit distractors).
  - Verified all 8 `UNION` branches execute `SEARCH media_dhash_buckets USING PRIMARY KEY (bucket_idx=? AND bucket_val=?)` and the outer query executes `SEARCH m USING INTEGER PRIMARY KEY (rowid=?)`, with **zero** `SCAN` operations.
- **Exhaustive Recall Testing (Hamming Distances `0..10`)**:
  - Tested both **worst-case spread bit-flips** (1 bit flipped in each of $d$ distinct 16-bit blocks for $d \in \{0..7\}$) and **concentrated bit-flips** ($d$ bits flipped within a single block) with mixed-case hex inputs at `perceptual_max_distance=7`. Achieved **100% recall** via `media_dhash_buckets`.
  - Tested **distances `8`, `9`, and `10` with all 8 blocks modified** (0 shared 16-bit blocks): verified they do not match when `perceptual_max_distance=7`, and achieve **100% recall** via the automatic full-scan fallback when `perceptual_max_distance >= dist` (`8`, `9`, `10`).
  - Verified legacy 16-char `dhash` rows are still matched when `perceptual_max_distance < 8` via the `_has_legacy_dhash` secondary query.
- **Stale-Bucket Prevention & Bulk Prune Cascade**:
  - Verified `upgrade_media_item` deletes old `media_dhash_buckets` entries and inserts the new `dhash` buckets so queries near the old `dhash` no longer match.
  - Verified `upsert_existing_nas_media` preserves existing buckets when `dhash=""` and replaces them when a new `dhash` is provided.
  - Verified `delete_media_item` and multi-chunk `prune_missing_existing_nas_media` (tested with 650 `existing_nas` items across >500-item SQLite chunks, pruning 649 items) cascade-delete all orphaned `media_dhash_buckets` rows via `ON DELETE CASCADE`.
- **Result**: **PASS**.

### 3. G2 `quality_upgrades` Write-Ahead Journal Lifecycle & >500-Item Batch Pagination
- **Verification**:
  - Verified `get_stats()` on an empty table and on a table containing only `'in_progress'` rows returns `quality_upgrades_count == 0` and `quarantined_count == 0`.
  - Verified `create_pending_quality_upgrade` inserts rows with `status='in_progress'` and all rollback metadata (`old_rel_path`, `old_taken_at`, `old_latitude`, `old_longitude`), which are visible in `list_in_progress_quality_upgrades()` and `list_quality_upgrades(include_in_progress=True)` but **never leak** into default `list_quality_upgrades()`, `list_quarantined_upgrades_batch()`, or `get_stats()`.
  - Verified `finalize_quality_upgrade` transitions `'in_progress'` → `'quarantined'` (and updates `kept_rel_path` and optional `new_file_size`), and is a safe no-op if called on a row whose status is already `'restored'`, `'purged'`, or `'quarantined'`.
  - Verified `delete_quality_upgrade` removes rolled-back `'in_progress'` rows cleanly.
  - Verified `list_quarantined_upgrades_batch()` paginates cleanly through 1,150 quarantined rows (`500` + `500` + `150` + `0`).
- **Result**: **PASS**.

### 4. Deterministic `original_sha256` Priority
- **Verification**:
  - Inserted two rows where `final_sha256 == "COLLISION_HASH"` (`id1 < id2`) and verified deterministic `id ASC` tie-breaking (`id1`).
  - Inserted a third row (`id3 > id2`) with `original_sha256 == "COLLISION_HASH"` and verified `get_media_by_original_hash`, `find_duplicate_media` Tier 1, and `upsert_existing_nas_media` Branch B all deterministically prioritize `id3` (`ORDER BY CASE WHEN original_sha256 = ? THEN 0 ELSE 1 END, id ASC LIMIT 1`).
- **Result**: **PASS**.

### 5. `visual_status` Transitions & `visual_done` Stability
- **Verification**:
  - Verified `get_indexed_files_by_rel_path()` marks `visual_done=True` for `visual_status='ok'` and `visual_status='undecodable'` (even when `pixel_sha256=''`, `dhash=''`, `width=0`), and `visual_done=False` for `visual_status='skipped'` or `visual_status=''` (without visual hashes).
  - Verified `upsert_existing_nas_media` prevents `'skipped'` or `''` from clobbering an existing `'ok'` or `'undecodable'` status, while allowing `'skipped'`/`'undecodable'` to transition to `'ok'`.
- **Result**: **PASS**.

### 6. `upsert_archive` Retry Reset, Album Membership Retry Counting, 50-Insert `log_event` Pruning & Concurrency
- **Verification**:
  - Verified `upsert_archive` resets `total_files=0`, `imported_files=0`, `duplicate_files=0`, `failed_files=0`, `error_message=''`, and `completed_at=NULL` for all non-completed archive statuses (`'pending'`, `'downloading'`, `'processing'`, `'completed_with_errors'`, `'failed'`) while preserving `'completed'` archives intact.
  - Verified `mark_album_membership` with `status='pending'` never increments `retry_count` (unless `increment_retry=True` is explicitly passed), whereas `status='error'` increments `retry_count` up to the 20-retry ceiling in `get_pending_album_memberships()`. Verified `add_media_to_album` revives `'error'` rows (`sync_status='pending'`, `retry_count=0`, `last_error=''`) without disturbing `'synced'` rows.
  - Verified `log_event` prunes only on every 50th insert (`_LOG_PRUNE_INTERVAL = 50`), keeping the newest `max_rows` records by `id DESC` (and clearing all rows when `max_rows <= 0`).
  - Stress-tested 8 concurrent worker threads performing 200 mixed transactions (`record_media_item`, `find_duplicate_media` bucket queries, `log_event`, `create_pending_quality_upgrade`, and `finalize_quality_upgrade`) with zero SQLite lock errors or state corruption.
- **Result**: **PASS**.

---

## Probe Output
```text
=== [1] Testing Legacy v1.1.0 -> v1.2.0 SQLite Migration & Idempotency ===
  [PASS] Legacy v1.1.0 -> v1.2.0 migration, backfill & 4x idempotency verified.
=== [2] Testing media_dhash_buckets 8-way UNION Query Plan & 100% Recall (Distances 0..10) ===
  [PASS] 8-way UNION PK query plan, 100% recall across distances 0..10, and stale-bucket prevention verified.
=== [3] Testing G2 quality_upgrades Journal Lifecycle & >500-item Batching ===
  [PASS] G2 quality_upgrades journal lifecycle, non-leakage & 1,150-item batch pagination verified.
=== [4, 5, 6] Testing Hash Priority, visual_status, Archives, Albums, Log Pruning & Concurrency ===
  [PASS] Hash priority, visual_status transitions, archive retry, album retries, 50-insert pruning & 8-thread concurrency verified.
ALL ADVERSARIAL DATABASE PROBES PASSED!
```

Verdict: APPROVE

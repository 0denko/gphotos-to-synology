# Explorer 2 Handoff — Takeout, Dedup, Sources, Synology Client, Pipeline, Scheduler & Notifier Public Interfaces (v1.2.0 Target Contracts)

**Baseline inspected:** `dfcf34f` (`v1.1.0-0003`)  
**Target specification:** `v1.2.0` (`PROJECT.md § Interface Contracts` + `Collab.md` audit remediation)  
**Purpose:** Opaque-box 4-tier E2E test suite design (`tests/e2e/`) asserting the target v1.2.0 public interfaces, return values, persistence invariants, and behavioral contracts without coupling to private attributes or internal implementation details.

---

## 0. Environment & Runtime Tooling Snapshot

- Python: `3.11.2` (`.venv/bin/python`)
- `exiftool`: **Not installed** in the VM (`which exiftool` returns empty). E2E tests for EXIF writing must treat the pure-Python `piexif` JPEG fallback as the default active path, and may optionally test `exiftool` via a synthetic stub executable passed through `Settings.exiftool_path` / `apply_metadata(..., exiftool_path=...)` when testing ExifTool argv/behavior.
- `PIL`: currently `12.3.0` in baseline `.venv` (M1 pins `"pillow>=11.1.0,<12.3.0"` and adds `"pillow-heif>=1.0.0,<1.1.0"`).
- `piexif`: installed (`piexif.ExifIFD.OffsetTimeOriginal == 36881`, `piexif.ExifIFD.UserComment == 37510`, `piexif.ImageIFD.ImageDescription == 270`, `piexif.helper.UserComment` available).
- `pillow_heif`: not yet installed in baseline `.venv` (landing in M1). E2E HEIC tests should generate real HEIC bytes via `pillow_heif` once M1 lands (or `pytest.importorskip("pillow_heif")` only if running pre-M1, though Tier 1–4 M6 gate requires `pillow_heif` installed and passing).
- `httpx`: `0.28.1`.

---

## 1. G1 — Archive Completion, Finalize Guard, Retry & Counter Reset

### Public Entry Points & Signatures
- `MigrationPipeline.run_cycle(self) -> dict[str, Any]` (`src/gphotos_synology/worker/pipeline.py:146`)
  - Returns `{"existing_newly_indexed": int, "inbox_processed": int, "gdrive_processed": int, "albums_synced": int, "errors": int}`.
- `LocalInboxScanner.finalize_archive(self, archive_path: Path) -> None` (`src/gphotos_synology/sources/inbox.py:88`)
  - Actions via `Settings.inbox_post_action`: `"move_to_processed" | "delete" | "keep"`.
- `StateDB.upsert_archive(self, *, source: str, source_id: str, filename: str, batch_key: str = "", size_bytes: int = 0, checksum: str = "", status: str = "pending") -> int` (`src/gphotos_synology/database.py:243`)
- `StateDB.update_archive_progress(self, archive_id: int, *, status: str | None = None, total_files: int | None = None, imported_files: int | None = None, duplicate_files: int | None = None, failed_files: int | None = None, error_message: str | None = None, completed: bool = False) -> None` (`src/gphotos_synology/database.py:283`)
- `StateDB.get_archive_by_source(self, source: str, source_id: str) -> dict[str, Any] | None` (`src/gphotos_synology/database.py:235`)
- `StateDB.list_archives(self, limit: int = 50) -> list[dict[str, Any]]` (`src/gphotos_synology/database.py:324`)
- `StateDB.get_stats(self) -> dict[str, Any]` (`src/gphotos_synology/database.py:883`)

### Target v1.2.0 Behavioral Contracts (G1 + Bookkeeping Minor)
1. **Partial failure status (`failed > 0`):**
   - If any file inside an archive fails to import (`failed > 0`), the archive row MUST be updated with:
     - `status = "completed_with_errors"` (NOT `"completed"`),
     - `completed = False` (`completed_at` remains `NULL`),
     - `failed_files = failed` (> 0).
   - Only when `failed == 0` is the archive marked `status = "completed"` with `completed=True` (`completed_at` populated).
2. **Source finalization guard (Inbox & Drive):**
   - **Inbox (`_process_inbox_archive`):** `LocalInboxScanner.finalize_archive(item.path)` MUST ONLY be called when `stats["failed"] == 0`. When `stats["failed"] > 0`, the source archive MUST remain untouched in `settings.inbox_dir` — even when `inbox_post_action == "delete"` or `"move_to_processed"`.
   - **Drive (`_process_gdrive_archive`):** `gdrive.trash_archive(item.file_id)` remains gated on `settings.gdrive_post_action == "trash" and stats["failed"] == 0`.
   - Return value / cycle summary: an archive with `failed > 0` MUST NOT be treated as a clean success (contributes to `summary["errors"]` or does not suppress error reporting, and emits an alert/log).
3. **Retry behavior across cycles:**
   - Because `run_cycle()` skips only archives with `status == "completed"`, an archive in `"completed_with_errors"` (or `"failed"`) is picked up again on the next `run_cycle()`.
   - Already-imported files from the first run are skipped as exact duplicates (Tier 1 SHA-256), while previously failed files are retried; if the failure was transient and now succeeds (`failed == 0`), the archive transitions to `status = "completed"`, `completed_at` is set, and `finalize_archive()` runs.
4. **`upsert_archive` counter reset on retry (Minor):**
   - When `upsert_archive()` updates an existing `(source, source_id)` row (e.g. retrying a `"failed"` or `"completed_with_errors"` archive), it MUST reset `total_files = 0`, `imported_files = 0`, `duplicate_files = 0`, `failed_files = 0`, `error_message = ''`, and `completed_at = NULL` so stale counters from the prior attempt do not linger.
5. **Stats consistency:**
   - `get_stats()` counts `completed_archives` as `status = 'completed'`; `failed_archives` should reflect failed / error archives appropriately (check `status IN ('failed', 'completed_with_errors')` or `status = 'failed'` — E2E should assert `completed_archives == 0` when an archive has `status == 'completed_with_errors'`).

### E2E Edge Cases to Cover
- Archive with 2 good JPEGs + 1 failing file (e.g. unwritable target collision or forced error) under `inbox_post_action="delete"` -> archive file still exists in `inbox_dir`, 2 good photos exist in `photos_dir`, archive row has `status == "completed_with_errors"`, `completed_at is None`, `failed_files == 1`.
- Subsequent `run_cycle()` after transient cause is resolved -> retries archive, resets counters via `upsert_archive`, skips 2 already-imported files as duplicates, imports the 3rd, marks `status == "completed"` with `completed_at` set, and now deletes/moves source archive.

---

## 2. G2 — Crash-Atomic Quality-Upgrade Swap, Recovery, Restore/Purge & `_op_lock`

### Public Entry Points & Signatures
- **StateDB (`src/gphotos_synology/database.py`):**
  - New `quality_upgrades` columns (additive migration):
    - `old_rel_path TEXT NOT NULL DEFAULT ''`
    - `old_taken_at TEXT NOT NULL DEFAULT ''`
    - `old_latitude REAL`
    - `old_longitude REAL`
  - New journal & batch methods (from `PROJECT.md § Interface Contracts #3`):
    - `create_pending_quality_upgrade(...) -> int` — inserts row with `status = 'in_progress'` (recording `media_id`, `kept_rel_path`, `quarantine_filename`, `old_filename`, `new_filename`, `old_rel_path`, `old_taken_at`, `old_latitude`, `old_longitude`, old/new dimensions, size, sharpness, `match_reason`, `upgrade_reason`).
    - `finalize_quality_upgrade(self, upgrade_id: int, kept_rel_path: str) -> None` — transitions `'in_progress'` → `'quarantined'` and sets final `kept_rel_path`.
    - `delete_quality_upgrade(self, upgrade_id: int) -> None` — removes rolled-back `'in_progress'` row.
    - `list_in_progress_quality_upgrades(self) -> list[dict[str, Any]]`
    - `list_quarantined_upgrades_batch(self, limit: int = 500) -> list[dict[str, Any]]`
  - Existing methods retained:
    - `record_quality_upgrade(...) -> int`
    - `list_quality_upgrades(self, limit: int = 100) -> list[dict[str, Any]]`
    - `get_quality_upgrade(self, upgrade_id: int) -> dict[str, Any] | None`
    - `mark_quality_upgrade_status(self, upgrade_id: int, status: str) -> None`
    - `upgrade_media_item(self, media_id: int, *, original_sha256, final_sha256, pixel_sha256, dhash, width, height, sharpness, quality_score, original_filename, dest_rel_path, file_size, file_mtime_ns, taken_at, exif_status, ...) -> None` (also updates `media_dhash_buckets`, `latitude`/`longitude` if passed, and resets `synology_item_id = NULL` + `album_memberships` to `'pending'`, `retry_count = 0`).
- **MigrationPipeline (`src/gphotos_synology/worker/pipeline.py`):**
  - `self._op_lock: threading.RLock` (publicly documented in `PROJECT.md § Interface Contracts #5` for serializing cycle/upgrade/restore/purge operations).
  - `recover_in_progress_upgrades(self) -> int` (or automatic startup recovery invoked in `__init__` / start of `run_cycle()`).
  - `restore_quality_upgrade(self, upgrade_id: int) -> dict[str, Any]` — returns `{"ok": True, "restored_rel_path": str}`.
  - `purge_quality_upgrade(self, upgrade_id: int) -> dict[str, Any]` — returns `{"ok": True}`.
  - `purge_all_quarantined(self) -> int` — returns total purged count.

### Target v1.2.0 Behavioral Contracts (G2 + Restore/Purge Minors)
1. **Write-ahead journal & rollback on swap failure:**
   - Before moving `existing_abs` to quarantine, pipeline creates an `'in_progress'` row via `create_pending_quality_upgrade(...)` storing `old_rel_path`, `old_taken_at`, `old_latitude`, `old_longitude`, `quarantine_filename`.
   - Step 1 moves `existing_abs` → `quarantine_dir / q_name`.
   - Step 2 places `incoming_path` → `dest_abs` in `photos_dir`.
   - If Step 2 or DB update raises an exception:
     - Rollback moves `quarantine_dir / q_name` back to `existing_abs` (if `existing_abs` is missing and quarantine file exists), removes any partial `dest_abs`, and deletes the `'in_progress'` row via `delete_quality_upgrade(upgrade_id)`.
     - Result: original photo is intact on disk at `existing_abs`, DB row is untouched, no orphaned `'in_progress'` or `'quarantined'` upgrade row remains.
   - On success: `upgrade_media_item(...)` updates `media_items` and `finalize_quality_upgrade(upgrade_id, dest_rel)` transitions row to `status = 'quarantined'`.
2. **Startup / cycle crash recovery (`in_progress` rows):**
   - If the process crashed after moving original to quarantine (simulated in E2E by leaving an `'in_progress'` row in DB + file in `quarantine_dir` + missing file at `old_rel_path`), initializing a new `MigrationPipeline` / calling `run_cycle()` detects `list_in_progress_quality_upgrades()`:
     - If `quarantine_file` exists and `old_rel_path` is missing (and `kept_rel_path` doesn't hold the completed upgrade), it restores `quarantine_file` back to `old_rel_path` and cleans up the `'in_progress'` row (or if both kept & quarantine exist and DB already upgraded, finalizes to `'quarantined'`).
     - In all cases, zero `'in_progress'` rows remain after recovery, and the user's photo is NOT lost in quarantine.
3. **Missing-file repair during dedup/upgrade:**
   - Previously, if `existing_abs` was missing on disk (`not existing_abs.is_file()`), `_maybe_upgrade_duplicate` returned `False` and the incoming photo was permanently skipped as a duplicate while the DB pointed at a phantom file.
   - In v1.2.0: when a duplicate matches a DB row whose `dest_rel_path` no longer exists on disk, the pipeline repairs the missing library file by placing the incoming copy into `photos_dir` and updating the DB row (instead of silently skipping it forever).
4. **Fresh `stat()` / re-fingerprint before deciding upgrade:**
   - Before calling `compare_image_quality(...)`, `_maybe_upgrade_duplicate` stats `existing_abs`. If `(st.st_size, st.st_mtime_ns)` disagrees with the stored DB `(file_size, file_mtime_ns)` (e.g. the NAS file was externally replaced/edited outside the app), it re-fingerprints `existing_abs` and bases the win/lose decision on the real file currently on disk.
5. **Safe restore (`restore_quality_upgrade`):**
   - Raises `ValueError` if upgrade not found or `row["status"] != "quarantined"`.
   - Raises `ValueError` on path traversal (`quarantine_filename`, `kept_rel_path`, or `old_rel_path` resolving outside `quarantine_dir` / `photos_dir`).
   - Restores to `old_rel_path` (preserving original directory + filename; fallback to `kept_abs.parent / old_filename` only if `old_rel_path` is empty on legacy rows).
   - Temp-staged / non-destructive swap: does NOT unlink `kept_abs` before `q_path` is safely staged/moved into place, and does not blindly clobber an unrelated file sitting at `old_rel_path`.
   - Restores `taken_at` (`old_taken_at` when recorded, falling back to `fp.exif_taken_at`) and GPS (`old_latitude`, `old_longitude`) on the `media_items` row.
   - Marks upgrade status `'restored'`.
6. **Strict purge guard (`purge_quality_upgrade`) & batched `purge_all_quarantined`:**
   - `purge_quality_upgrade(upgrade_id)` MUST enforce `row["status"] == "quarantined"` — calling purge on an already `'restored'` or `'purged'` upgrade raises `ValueError` (mapped to HTTP `400` in `routes.py`). Note: baseline `test_visual_quality_upgrade.py:212` previously tested purging after restore; v1.2.0 intentionally breaks that old bug per `Collab.md` & `PROJECT.md`.
   - Validates path containment of `quarantine_filename` under `settings.quarantine_dir` (`ValueError` on traversal).
   - `purge_all_quarantined()` loops over `list_quarantined_upgrades_batch(limit=500)` until empty (no silent 1000-row cap!).
7. **Concurrency lock (`_op_lock`):**
   - `run_cycle`, `restore_quality_upgrade`, `purge_quality_upgrade`, `purge_all_quarantined` acquire `self._op_lock` so web-triggered restore/purge cannot race an in-flight upgrade.

---

## 3. G9 — Strict Sidecar Matcher (`src/gphotos_synology/takeout/matcher.py`)

### Public Entry Points & Signatures
- `MEDIA_EXTENSIONS: set[str]`, `VIDEO_EXTENSIONS: set[str]`, `LIVE_PHOTO_STILL_EXTS: tuple[str, ...]`, `SUPPLEMENTAL_FULL: str`
- `is_media_file(path: Path) -> bool`
- `is_video_file(path: Path) -> bool`
- `detect_album_name(media_dir: Path) -> str | None`
- `DirectoryJsonIndex.build(cls, directory: Path) -> DirectoryJsonIndex`
- `find_sidecar_json(media_path: Path, index: DirectoryJsonIndex | None = None) -> Path | None`

### Target v1.2.0 Behavioral Contracts (G9 + Matcher Nits)
1. **Numbered duplicates `(N)` — NO unnumbered fallback:**
   - Given `IMG_1234(1).jpg` when ONLY `IMG_1234.jpg.json` (or `IMG_1234.jpg.supplemental-metadata.json`) exists (with `"title": "IMG_1234.jpg"`), `find_sidecar_json` MUST return `None`.
   - Valid numbered sidecars still match:
     - `IMG_1234(1).jpg.json`, `IMG_1234(1).jpg.supplemental-metadata.json`
     - `IMG_1234.jpg(1).json`, `IMG_1234.jpg.supplemental-metadata(1).json` (and truncated supplemental variants with `(1)`)
     - Truncated 46-char base + `(1).json`
     - Internal JSON `"title"` **exact match** to `"IMG_1234(1).jpg"` (unique candidate) — NOT unnumbered `"IMG_1234.jpg"`.
2. **Unique candidate required for `title` fallback:**
   - If two or more sidecars in the same directory share the same internal `"title"` (and neither is uniquely disambiguated by `(N)` for a numbered file), `find_sidecar_json` MUST return `None` instead of arbitrarily returning `matches[0]`.
   - If exactly 1 candidate matches `"title"`, return it.
3. **Unique candidate required for 46-char prefix fallback:**
   - For severely truncated filenames (`len(prefix_key) >= 35`), if 2 or more JSON files share `filename[:46].lower()`, `find_sidecar_json` MUST return `None` (bail to no-metadata on ambiguity). If exactly 1 matches, return it.
4. **Stem-only candidate priority (Nit):**
   - Strip-extension candidates (`<stem>.json`, `<stem>.supplemental-metadata.json`) MUST be tried **after** exact/supplemental/numbered/46-char/edited/Live-Photo AND **after** `title`-based disambiguation, so `<stem>.json` from a different extension (e.g. `IMG_1.png` vs `IMG_1.jpg`) does not hijack a photo that has a proper `title` match.
5. **Sorted, case-safe `DirectoryJsonIndex` (Nit):**
   - `DirectoryJsonIndex.build(directory)` iterates entries in deterministic sorted order (`sorted(directory.iterdir(), ...)`), and handles case-colliding sidecar filenames (on case-sensitive filesystems like Linux/Synology ext4/btrfs) safely without silent last-write-wins overwrites (e.g. storing exact-case lookup alongside case-insensitive list/lookup so exact case wins and ambiguous case collisions don't silently clobber).

---

## 4. G10 — HEIC/HEIF Visual Fingerprinting & Stable Undecodable Handling

### Public Entry Points & Signatures
- `MediaFingerprint` dataclass (`src/gphotos_synology/takeout/dedup.py:32`):
  - Fields: `sha256: str`, `pixel_sha256: str = ""`, `dhash: str = ""`, `width: int = 0`, `height: int = 0`, `sharpness: float = 0.0`, `quality_score: float = 0.0`, `exif_taken_at: str | None = None`, **`visual_status: str = ""`** (`"ok" | "undecodable" | "skipped"`).
- `compute_media_fingerprint(path: Path, *, compute_visual: bool = True) -> MediaFingerprint`
- `ExistingLibraryScanner(settings: Settings, db: StateDB)` → `.scan(*, on_progress: Callable[[int, int], None] | None = None) -> LibraryScanResult` (`src/gphotos_synology/synology/scanner.py:43`)
  - `LibraryScanResult`: `scanned`, `newly_indexed`, `unchanged_skipped`, `pruned_missing`, `errors`.
- `StateDB`:
  - `media_items.visual_status TEXT NOT NULL DEFAULT ''`
  - `upsert_existing_nas_media(..., visual_status: str = "") -> int`
  - `record_media_item(..., visual_status: str = "") -> int`
  - `get_indexed_files_by_rel_path() -> dict[str, dict[str, Any]]` (includes `visual_status` / reflects stable undecodable state).

### Target v1.2.0 Behavioral Contracts (G10)
1. **HEIC/HEIF decoding via `pillow-heif`:**
   - `dedup.py` registers `pillow_heif.register_heif_opener()` at import/init (gracefully catching `ImportError` if absent).
   - Calling `compute_media_fingerprint(heic_path, compute_visual=True)` on a real `.heic` / `.heif` image returns:
     - `visual_status == "ok"`,
     - non-empty 64-hex `pixel_sha256`,
     - non-empty 32-hex `dhash`,
     - `width > 0`, `height > 0`, `sharpness >= 0.0`, `quality_score > 0.0`.
   - Re-compressed / EXIF-modified HEIC duplicates are caught by Tier 2 (`pixel_sha256`) and Tier 3 (`perceptual_dhash`), and quality upgrade works for HEIC just like JPEG.
2. **`visual_status` semantics:**
   - `"skipped"`: when `compute_visual=False` or `is_video_file(path)` is True.
   - `"ok"`: image decoded and visual fingerprint computed.
   - `"undecodable"`: image failed to decode (corrupt image, unsupported RAW/codec) — logged at **`WARNING`** level (not `DEBUG`) and persisted to `media_items.visual_status = 'undecodable'`.
3. **Scanner stable skip for undecodable files:**
   - First `ExistingLibraryScanner.scan()` on an undecodable image (e.g. a `.dng` or corrupt `.heic` with random bytes): `newly_indexed == 1`, `unchanged_skipped == 0`, DB row has `visual_status == "undecodable"`.
   - Second `ExistingLibraryScanner.scan()` with unchanged `(file_size, file_mtime_ns)`: `newly_indexed == 0`, `unchanged_skipped == 1` — does NOT re-read or re-SHA-256 the file every cycle!
   - If the file's `size` or `mtime_ns` changes on disk (e.g. replaced with a valid image), the scanner re-indexes it and updates `visual_status` to `"ok"`.

---

## 5. G11 — Tighter Perceptual Dedup Corroboration, `perceptual-dedup` Log & SQL dHash Bucketing

### Public Entry Points & Signatures
- `is_perceptual_match(incoming_dhash: str, existing_dhash: str, *, incoming_taken_at: str | None, existing_taken_at: str | None, incoming_filename: str, existing_filename: str, incoming_width: int = 0, incoming_height: int = 0, existing_width: int = 0, existing_height: int = 0, max_distance: int = 10) -> bool` (`src/gphotos_synology/takeout/dedup.py:349`)
- `hamming_distance_hex(hash_a: str, hash_b: str) -> int` (`src/gphotos_synology/takeout/dedup.py:312`)
- `StateDB.find_duplicate_media(self, *, sha256: str, pixel_sha256: str = "", dhash: str = "", width: int = 0, height: int = 0, taken_at: str | None = None, filename: str = "", dedup_mode: str = "perceptual", perceptual_max_distance: int = 10) -> tuple[dict[str, Any] | None, str | None]` (`src/gphotos_synology/database.py:343`)
- Table `media_dhash_buckets(bucket_idx INTEGER NOT NULL, bucket_val TEXT NOT NULL, media_id INTEGER NOT NULL, PRIMARY KEY (bucket_idx, bucket_val, media_id)) WITHOUT ROWID`.

### Target v1.2.0 Behavioral Contracts (G11)
1. **Unknown dimensions guardrail:**
   - When any dimension is unknown (`w1 <= 0 or h1 <= 0 or w2 <= 0 or h2 <= 0`), unknown dimensions MUST fail the looser branches (i.e. NOT automatically pass as aspect-ratio-compatible for loose matches; either rejected outright or only allowed at near-exact distance `<= 2` with strong corroboration — specifically per `PROJECT.md`: *"unknown dims fail looser branches"*). At `dist > 4` or without timestamp+stem corroboration, unknown dimensions MUST return `False`.
2. **Corroboration required for `dist > 4`:**
   - When either timestamp is missing (`ts_close is None`):
     - If `dist > 4` (even with `max_distance=10`), a different-stem photo with no timestamp MUST return `False` (previously `dist <= max(4, effective_max // 2)` allowed `dist == 5` with no timestamp and different filename!).
     - Without timestamp: match requires `same_stem` (and within `effective_max`, with known compatible dims) OR very tight `dist <= 4` (note: check exact boundary — for `dist > 4`, timestamp OR filename corroboration is strictly required; for `dist == 5` with different stem and `taken_at=None`, MUST return `False`).
   - When both timestamps exist and differ (`ts_close is False`, not a clean whole-hour timezone shift within 14h):
     - Only near-identical distance (`dist <= max(2, effective_max // 3)` or `<= 2`) may match, and NOT with unknown dims.
3. **Timezone conversion bug fix (`.astimezone(UTC)`):**
   - Previously line 387 used `dt_a.replace(tzinfo=UTC) - dt_b.replace(tzinfo=UTC)` which discarded real offsets like `+02:00`!
   - In v1.2.0: timezone-aware datetimes are normalized via `.astimezone(UTC)` (only naive datetimes get `.replace(tzinfo=UTC)`), so `"2024-07-03T12:13:20+02:00"` and `"2024-07-03T10:13:20+00:00"` are recognized as identical (0s delta) and distinct-offset timestamps are compared accurately.
4. **Distinct `perceptual-dedup` log event:**
   - When `MigrationPipeline` skips a duplicate via `match_reason == "perceptual_dhash"` (and does not upgrade it), it logs a distinct reviewable event with component `"perceptual-dedup"` via `self._log("INFO", "perceptual-dedup", ...)` visible in `db.get_recent_logs()` and `GET /api/logs`.
5. **SQL dHash bucket table (`media_dhash_buckets`) & 100% recall:**
   - Every 32-hex-char `dhash` inserted via `record_media_item`, `upsert_existing_nas_media`, or updated via `upgrade_media_item` populates 8 rows in `media_dhash_buckets`: `(i, dhash[i*4:(i+1)*4], media_id)` for `i in range(8)`.
   - Deleting/pruning a media item (`prune_missing_existing_nas_media` or `DELETE FROM media_items` via FK/cleanup) removes its bucket entries.
   - `find_duplicate_media` Tier 3 uses `media_dhash_buckets` when `effective_max < 8` and `len(dhash) == 32` (pigeonhole principle: Hamming distance ≤ 7 across 8 blocks guarantees at least 1 identical 16-bit block -> 100% recall!), and falls back to full scan when `effective_max >= 8` or legacy 16-char hash so recall is 100% for all thresholds (including `max_distance=10`).
   - E2E tests should verify 100% recall across distances `0..7` (bucketed path) AND `8..10` (fallback path), plus legacy 16-char `dhash` compatibility, plus bucket maintenance after `upgrade_media_item` and `prune_missing_existing_nas_media`.

---

## 6. G12 — Extraction Decompression Limits, ZipSlip/TarSlip & Stale Temp Cleanup

### Public Entry Points & Signatures (`src/gphotos_synology/takeout/extractor.py`)
- `extract_archive(archive_path: Path, dest_dir: Path, *, max_entries: int = 100_000, max_uncompressed_bytes: int | None = None, max_compression_ratio: float = 100.0, skip_unsafe_entries: bool = False) -> Path`
- `cleanup_stale_temp_files(directory: Path, max_age_seconds: float = 0.0) -> int`
- `safe_move_or_copy(src: Path, dst: Path) -> None`
- `is_supported_archive(filename: str) -> bool`
- `extract_batch_key(filename: str) -> str`
- `get_free_disk_gb(path: Path) -> float`
- `cleanup_directory(path: Path) -> None`
- `compute_file_sha256(path: Path) -> str`

### Target v1.2.0 Behavioral Contracts (G12)
1. **Entry count cap (`max_entries`):**
   - Raises `ValueError` if archive contains more than `max_entries` entries (both ZIP and TAR.GZ/TGZ).
2. **Pre-flight total uncompressed byte cap (`max_uncompressed_bytes`):**
   - Sums declared member sizes (`ZipInfo.file_size` / `TarInfo.size`) before extracting; raises `ValueError` if total exceeds `max_uncompressed_bytes`.
   - When `max_uncompressed_bytes` is `None` or passed by pipeline: pipeline derives a cap from actual free disk space on `work_dir` minus `min_free_disk_gb` safety margin.
3. **Streaming byte cap (defends against spoofed headers):**
   - Counts actual decompressed bytes copied in chunks; aborts and raises `ValueError` as soon as cumulative bytes written exceed `max_uncompressed_bytes` (even if ZIP/TAR header lied about `file_size`).
4. **Compression ratio cap (`max_compression_ratio`):**
   - Raises `ValueError` when decompressed bytes / compressed archive size (or per-member ratio for non-trivial sizes, e.g. > 1 MiB) exceeds `max_compression_ratio` (defends against zip bombs like 100 MB of zeros in a 100 KB zip).
5. **ZipSlip / TarSlip & `skip_unsafe_entries`:**
   - Unsafe entries: `../escape`, `/absolute/path`, drive-letter paths, and TAR symlinks/hardlinks/devices/FIFOs.
   - When `skip_unsafe_entries=False` (default): raises `ValueError` on path traversal (fail-closed contract preserved).
   - When `skip_unsafe_entries=True`: skips unsafe member, logs a `WARNING`, and continues extracting all safe members into `dest_dir` (and no file is written outside `dest_dir`).
   - TAR symlinks/hardlinks/device entries are NEVER materialized in either mode.
6. **`cleanup_stale_temp_files(directory: Path, max_age_seconds: float = 0.0) -> int`:**
   - Walks `directory` and unlinks stale `.*.tmp.<pid>` files left behind by interrupted `safe_move_or_copy` calls whose age `>= max_age_seconds`.
   - Does NOT touch regular media files or `.tmp` files newer than `max_age_seconds`.
   - Returns integer count of removed temp files.
   - Invoked by scheduler/pipeline startup or stop cleanup.

---

## 7. G13 — `piexif.load` Skip-on-Error & Metadata Minors (`src/gphotos_synology/takeout/metadata.py`)

### Public Entry Points & Signatures
- `PhotoMetadata` dataclass (`title`, `description`, `taken_at`, `latitude`, `longitude`, `altitude`, `people`, `album_name`, `.has_gps`)
- `parse_sidecar_json(json_path: Path, album_name: str | None = None) -> PhotoMetadata`
- `apply_metadata(media_path: Path, metadata: PhotoMetadata, *, write_exif: bool = True, write_album_tags: bool = True, exiftool_path: str = "exiftool") -> str`
  - Returns `'repaired' | 'mtime_only' | 'skipped' | 'error'`.
- `exiftool_available(exiftool_path: str = "exiftool") -> bool`

### Target v1.2.0 Behavioral Contracts (G13 + Metadata Minors)
1. **G13 — `piexif.load` failure MUST skip EXIF write (never wipe existing EXIF):**
   - Given a JPEG containing a malformed/unparseable EXIF APP1 segment (so `piexif.load` raises an exception, e.g. corrupt TIFF header / bad maker note bytes inside APP1) and no ExifTool:
     - `apply_metadata(..., write_exif=True, exiftool_path="nonexistent")` MUST NOT substitute an empty dict or call `piexif.insert(...)`.
     - File bytes MUST remain bit-for-bit identical (existing EXIF segment NOT wiped!).
     - File `mtime` IS updated to `metadata.taken_at`.
     - Return status is `"mtime_only"` (and logs a warning).
   - Note: a valid JPEG with **no** APP1/EXIF segment at all still loads cleanly in `piexif.load` (returns empty IFDs) and gets EXIF written (`status == "repaired"`).
2. **GPS DMS seconds=60 carry clamp (Minor):**
   - Coordinates like `37.99999999` (where rounding `(m_float - m) * 60.0 * 10000` yields `600000`, i.e., `60.0000` seconds) MUST carry over: `s == 600000 -> s = 0, m += 1; if m == 60 -> m = 0, d += 1` (producing `38° 0' 0.0"` = `((38, 1), (0, 1), (0, 10000))`).
   - Invariant for all coordinates: `0 <= m < 60` and `0 <= s < 600000`.
3. **`OffsetTimeOriginal = "+00:00"` (Minor):**
   - Both the `piexif` JPEG path and ExifTool image path MUST write UTC offset tag (`piexif.ExifIFD.OffsetTimeOriginal = b"+00:00"`, and optionally `OffsetTime` / `OffsetTimeDigitized` = `b"+00:00"`, ExifTool `-EXIF:OffsetTimeOriginal=+00:00`) alongside `DateTimeOriginal` so EXIF consumers don't misinterpret UTC wall-clock as local time.
4. **ASCII `ImageDescription` + UTF-8/Unicode `UserComment` (Nit):**
   - In `piexif` JPEG path: `ImageIFD.ImageDescription` MUST contain valid ASCII bytes only (e.g. ASCII-encoded / replaced or omitted if non-ASCII; never raw UTF-8 multi-byte sequences in an ASCII-typed EXIF field).
   - When `metadata.description` contains non-ASCII characters (e.g. `"Café 東京"`), `ExifIFD.UserComment` is written preserving the full Unicode text (opaque-box test: verify `ImageDescription` decodes cleanly as ASCII if present, and `UserComment` decodes via `piexif.helper.UserComment.load` or UTF-8 fallback to the original description).
5. **Float-formatted timestamps in `_parse_timestamp` (Nit):**
   - `parse_sidecar_json` with `"photoTakenTime": {"timestamp": "1720000000.0"}` or `1720000000.5` MUST parse to the corresponding UTC `datetime` (instead of returning `None` due to `int("1720000000.0")` raising `ValueError`).
6. **Non-JPEG without ExifTool warning (Minor):**
   - When `write_exif=True`, ExifTool is unavailable, and `media_path` is a non-JPEG (e.g. `.png`, `.heic`, `.mp4`), `apply_metadata` returns `"mtime_only"`, sets `mtime`, and emits a clear warning log (e.g. once or per file) noting ExifTool is unavailable for non-JPEG EXIF writing.

---

## 8. Minors — Public Interfaces & Behavioral Contracts

### 8.1 Album Retries (`database.py` & `pipeline.py`)
- **Contract:** `StateDB.mark_album_membership(album_id: int, media_id: int, *, status: str, error: str = "", increment_retry: bool | None = None) -> None`.
  - Default: `increment_retry = (status == "error")`.
  - When `pipeline.sync_pending_albums()` cannot yet resolve `synology_item_id` (DSM still indexing), it calls `mark_album_membership(album_id, media_id, status="pending", error="Waiting for Synology Photos to index file")`.
  - `retry_count` MUST remain `0` across >20 consecutive cycles while `status == "pending"`!
  - Only when `status == "error"` (or explicit `increment_retry=True`) does `retry_count` increment (capped at `< 20` in `get_pending_album_memberships`).
  - Once Synology indexes the photo (even on cycle 25), `sync_pending_albums()` resolves it and marks `status == "synced"`.

### 8.2 Google Drive Client (`src/gphotos_synology/sources/gdrive.py`)
- **Constructor:** `GoogleDriveClient(settings: Settings)`
- **Public methods & v1.2.0 contracts:**
  - `scopes -> list[str]` (`DRIVE_READONLY_SCOPE` vs `DRIVE_FULL_SCOPE` when `gdrive_post_action == "trash"`)
  - `is_oauth_configured() -> bool`
  - `build_authorization_url(redirect_uri: str, state: str | None = None) -> tuple[str, str]`
  - `exchange_code(code: str, redirect_uri: str) -> None`
  - `disconnect() -> None`
  - `get_credentials() -> Credentials | None`
  - `is_connected() -> bool` (backwards-compatible boolean)
  - **`connection_status() -> dict[str, Any]` (NEW):** returns `{"connected": bool, "error": str}`. When token refresh fails (e.g. revoked refresh token / network error), `connected` is `False` AND `error` is a non-empty diagnostic message (not silently swallowed as plain `"not connected"`).
  - **Atomic `0o600` token save:** creates temp file with `0o600` from creation (`os.open(..., 0o600)` or `os.chmod` + warning log if chmod fails) before writing token JSON and `os.replace` to `settings.token_path`. Final `token_path` mode has `stat.S_IMODE(st.st_mode) == 0o600`.
  - **`find_takeout_folder_ids(service) -> list[str]` pagination:** loops on `nextPageToken` (not capped at single `pageSize=20` page) and collects all matching folder IDs across multiple pages.
  - **`list_takeout_archives() -> list[DriveArchiveItem]`**
  - **`download_archive(item: DriveArchiveItem, dest_path: Path, *, on_progress=None, max_retries: int = 4) -> Path`:**
    - **MD5 checksum verification:** when `item.md5_checksum` is non-empty, computes MD5 of the downloaded file before `os.replace(tmp_path, dest_path)`. On mismatch, deletes temp file and raises (retried up to `max_retries`, never promoting a corrupt archive to `dest_path`).
    - **Best-effort download resume:** on transient mid-download failure, preserves partial `.part` file when resumable (e.g. Range header / resumable offset) rather than unconditionally deleting and restarting from byte 0 on every retry (unlinking `.part` on final failure or checksum mismatch).
  - **`trash_archive(file_id: str) -> None`**

### 8.3 Hash Lookup Ordering (`src/gphotos_synology/database.py`)
- `get_media_by_original_hash(original_sha256: str) -> dict[str, Any] | None` and `find_duplicate_media` Tier 1 MUST order by:
  `ORDER BY CASE WHEN original_sha256 = ? THEN 0 ELSE 1 END, id ASC LIMIT 1`
- E2E test: insert row A with `original_sha256="H1", final_sha256="H2"` and row B with `original_sha256="H2", final_sha256="H3"`. Calling `get_media_by_original_hash("H2")` and `find_duplicate_media(sha256="H2")` MUST return row B (exact `original_sha256` match), never row A.

### 8.4 Scanner Isolation & Ignored Directories (`pipeline.py`, `scanner.py`)
- **Scanner isolation:** In `MigrationPipeline.run_cycle()`, `index_existing_library()` is wrapped in its own `try/except`. If `index_existing_library()` raises an exception (e.g. unreadable directory / permission error), `run_cycle()` logs an error, increments `summary["errors"]`, and **continues** to process inbox archives, Drive archives, and album sync rather than aborting the entire cycle.
- **Ignored dirs:** `ExistingLibraryScanner.scan()` skips `@eaDir`, `@tmp`, `@SynoResource`, `#recycle`, `#snapshot`, dot-dirs (`.Thumbnails`, `.hidden`, etc.), `__MACOSX`, `_replaced_duplicates`, and does not follow directory symlinks (`os.walk(..., followlinks=False)` default) and never modifies any file on disk (read-only property: file bytes and `mtime_ns` unchanged after scan).

### 8.5 Notifier Sync vs Async & 12h Disk-Guard Throttle (`notifications.py`, `pipeline.py`)
- **Notifier (`src/gphotos_synology/notifications.py`):**
  - Constructor: `Notifier(settings: Settings)`
  - `send(self, title: str, message: str, *, level: str = "info") -> bool` — synchronous, returns `bool` (used by `POST /api/actions/test-notification`).
  - **`send_async(self, title: str, message: str, *, level: str = "info") -> None` (NEW):** dispatches `send(...)` in a background daemon thread so slow webhooks/SMTP do not block the pipeline worker thread (returns immediately in milliseconds even if webhook sleeps 2s).
- **12h disk-guard alert throttle (`pipeline.py`):**
  - When `_check_disk_space()` triggers low-disk warning, it records a timestamp in `StateDB` (e.g. `last_disk_alert_at` via `db.set_state`) and only dispatches a notification if none was sent in the last 12 hours (`timedelta(hours=12)`).
  - Running `run_cycle()` 3 times in a row while disk is low triggers only **1** webhook delivery (not 3).

### 8.6 `SynologyPhotosClient` (`src/gphotos_synology/synology/photos_api.py`)
- **Constructor:** `SynologyPhotosClient(settings: Settings, *, client: httpx.Client | None = None)`
  - Uses `settings.synology_url`, `settings.synology_verify_ssl` (or `settings.synology_ca_cert` if set), `settings.synology_username`, `settings.synology_password`, `settings.synology_space`.
- **Target v1.2.0 contracts:**
  1. **POST body for `_sid`:** All authenticated `/webapi/entry.cgi` requests (when `self._sid` is set — including `logout`, `list_albums`, `_list_subfolders`, `resolve_item_id`, `create`, `add_item`) MUST send `_sid` in the POST form body (`request.method == "POST"`), NEVER as a URL query parameter (`"_sid" not in request.url.params` and `"_sid=" not in str(request.url)`).
  2. **Auto-relogin on code `119` (expired/invalid session):** When any API call returns `{"success": false, "error": {"code": 119}}`, the client automatically calls `login()` once to obtain a fresh `_sid` / `_syno_token` and transparently retries the failed request (without infinite recursion if the second attempt also fails with 119).
  3. **Album cache invalidation:** Exposes cache invalidation (or invalidates `_album_cache` on session relogin / error / explicit `invalidate_cache()` or `list_albums(force_refresh=True)` — verify exact refresh behavior when an album is created externally or after relogin).
  4. **Root-level `resolve_item_id` scoping:** Calling `resolve_item_id("IMG_ROOT.JPG")` (no `/` folder prefix) MUST NOT falsely return a same-named file living inside a subfolder (e.g. `/2024/07/IMG_ROOT.JPG`). When resolving root-level items, either resolves the root folder ID (`folder_id` of `/`) or verifies returned item's `folder_id` belongs to the root folder rather than matching arbitrary subfolder items.

### 8.7 `LocalInboxScanner` (`src/gphotos_synology/sources/inbox.py`)
- **Constructor:** `LocalInboxScanner(settings: Settings)`
- **Public methods:**
  - `scan_ready_archives() -> list[InboxArchiveItem]`
  - `finalize_archive(archive_path: Path) -> None`
- **Target v1.2.0 contracts:**
  1. **Symlink skip:** Symlinks in `inbox_dir` (even pointing to a valid `.zip` file outside `inbox_dir`) MUST be skipped by `scan_ready_archives()` (`entry.is_symlink()` check) so `finalize_archive()` cannot act on symlinks or exfiltrate outside files.
  2. **`(size, mtime_ns)` stability tracking:** Stability check tracks both `stat.st_size` AND `stat.st_mtime_ns`. If a file is modified in-place keeping the exact same size (changing `mtime_ns`), the stability timer resets and the file is NOT returned as ready until `(size, mtime_ns)` has remained unchanged for `inbox_stability_seconds`.
  3. **Archive readability check:** Optional/lightweight check (e.g. `zipfile.is_zipfile` / readable header) prevents truncated stalled archives from immediately entering ingestion as ready if unreadably incomplete (note: verify whether corrupt archives still surface cleanly or get deferred/failed gracefully).

### 8.8 Quality Heuristics (`src/gphotos_synology/takeout/dedup.py`)
- **Public API:**
  - `ImageQualityMetrics(width, height, file_size, sharpness, filename="")`
  - `QualityDecision(incoming_wins: bool, reason: str, incoming_score: float, existing_score: float)`
  - `compute_quality_score(metrics: ImageQualityMetrics) -> float`
  - `compare_image_quality(incoming: ImageQualityMetrics, existing: ImageQualityMetrics) -> QualityDecision`
- **Target v1.2.0 contract:**
  - Rule 3 (comparable resolution & comparable sharpness, `incoming.file_size >= existing.file_size * 1.15`): MUST require **non-inferior sharpness** (e.g. `incoming.sharpness >= existing.sharpness * 0.95` or `incoming.sharpness >= existing.sharpness`), so a bloated/noisier/softer file that is 20% larger in bytes CANNOT replace a sharper copy at the same resolution!

### 8.9 `BackgroundScheduler` (`src/gphotos_synology/worker/scheduler.py`)
- **Constructor:** `BackgroundScheduler(config_manager: ConfigManager, db: StateDB, pipeline: MigrationPipeline, *, registry: ProfileRegistry | None = None)`
- **Public properties & methods:**
  - `is_paused: bool`, `is_running: bool`
  - `start() -> None`, `stop() -> None`
  - `pause() -> None`, `resume() -> None`
  - `trigger_now(*, profile_id: str | None = None, albums_only: bool = False, index_existing_only: bool = False) -> None`
- **Target v1.2.0 contracts:**
  1. **`next_run_at = None` when paused:** Calling `pause()` (and any paused iteration of `_loop()`) sets `next_run_at = None` in `pipeline.get_progress()` (NOT a future timestamp!). Resuming restores a future ISO `next_run_at`.
  2. **`last_run_at` set on manual runs:** Manual triggers (`trigger_now(index_existing_only=True)` and `trigger_now(albums_only=True)`) set `last_run_at` (ISO-8601 UTC string) in `pipeline.get_progress()` upon completion, just like full `run_cycle()`.
  3. **Cooperative stop & `.tmp.<pid>` cleanup:** `stop()` cleanly joins the worker thread and triggers/supports stale temp-file cleanup (`cleanup_stale_temp_files`).

---

## 9. Constructor Dependencies & Clean Wiring Guide for `tests/e2e/conftest.py`

All core components can be instantiated cleanly using ONLY public constructors and public configuration fields (zero private attribute access):

| Component | Public Constructor | Clean E2E Wiring Notes |
|---|---|---|
| `Settings` | `Settings(data_dir=..., work_dir=..., inbox_dir=..., photos_dir=..., homes_dir=..., ...)` | Use `tmp_path` subdirectories. Set `inbox_stability_seconds=0` for fast tests (or small positive value like `1` when testing stability). Set `min_free_disk_gb=0.1` for normal tests (or `999_999.0` to trigger disk guard deterministically without filling disk!). Note v1.2.0 defaults: `app_host="127.0.0.1"`, `synology_url="https://127.0.0.1:5001"`, `synology_verify_ssl=True`, `quarantine_dir_override: Path \| None = None`. |
| `ConfigManager` | `ConfigManager(base_settings=settings, fallback_settings_provider=...)` | Calls `base_settings.ensure_directories()` automatically. Update via `cfg.update({...})`. |
| `StateDB` | `StateDB(cfg.settings.db_path)` | Creates WAL SQLite DB and runs additive migrations automatically. Use public methods + standard `sqlite3.connect(db_path)` only when simulating a crash state (e.g. verifying schema/WAL) or via `create_pending_quality_upgrade`. |
| `SynologyPhotosClient` | `SynologyPhotosClient(settings)` or `SynologyPhotosClient(settings, client=httpx_client)` | **Best opaque-box pattern:** Start a real local `http.server.ThreadingHTTPServer` on `127.0.0.1:0` (ephemeral port) implementing `/webapi/entry.cgi` (supporting `SYNO.API.Auth`, `SYNO.Foto.Browse.Album`, `SYNO.Foto.Browse.NormalAlbum`, `SYNO.Foto.Browse.Folder`, `SYNO.Foto.Browse.Item`, plus `SYNO.FotoTeam.*`, code 119 simulation, and recording every request method/URL/form body). Set `settings.synology_url = f"http://127.0.0.1:{port}"`. No monkeypatching or `synology_client_factory` override needed — genuine `SynologyPhotosClient` executes real HTTP requests! |
| `MigrationPipeline` | `MigrationPipeline(config_manager=cfg, db=db)` (optional `synology_client_factory=...`) | When `settings.synology_url` points to the local mock DSM HTTP server and `settings.webhook_url` points to a local mock webhook HTTP server, `MigrationPipeline(cfg, db)` runs 100% genuinely end-to-end. |
| `LocalInboxScanner` | `LocalInboxScanner(cfg.settings)` | Point `settings.inbox_dir` to `tmp_path / "inbox"`. |
| `ExistingLibraryScanner` | `ExistingLibraryScanner(cfg.settings, db)` | Point `settings.photos_dir` to `tmp_path / "photos"`. |
| `Notifier` | `Notifier(cfg.settings)` | Point `settings.webhook_url` to a local `ThreadingHTTPServer` endpoint (`http://127.0.0.1:{port}/hook`) that records received payloads and supports configurable delay (to verify `send_async()` returns immediately while `send()` blocks until delivery). |
| `BackgroundScheduler` | `BackgroundScheduler(config_manager=cfg, db=db, pipeline=pipeline, registry=registry)` | Always pair `scheduler.start()` with `try/finally: scheduler.stop()` in a fixture so daemon threads never leak across tests. Poll `pipeline.get_progress()` with short bounded timeout. |
| `GoogleDriveClient` | `GoogleDriveClient(cfg.settings)` | Note: `googleapiclient.discovery.build("drive", "v3", ...)` targets `www.googleapis.com` by default. For opaque-box testing without private attributes: (1) write real OAuth JSON to `settings.token_path` to test `connection_status()`, `is_connected()`, `disconnect()`, and `0o600` file perms; (2) pass a lightweight mock `service` object directly to public `find_takeout_folder_ids(service)` for multi-page pagination tests; (3) for `download_archive` (MD5 verify, resume) and `list_takeout_archives`, use `googleapiclient` with a local HTTP server via `http` transport or standard `monkeypatch` on `googleapiclient.discovery.build` / `Credentials.refresh` at the library boundary (not on private attributes of `GoogleDriveClient`). |

---

## 10. Quick Checklist of Subtle Traps for E2E Test Authors

1. **Test isolation from host `.env`:** `Settings` uses `env_file=".env"`. Always pass explicit keyword args (`data_dir`, `work_dir`, `inbox_dir`, `photos_dir`, `homes_dir`, `synology_url`, `webhook_url`, etc.) or `monkeypatch` env vars in `conftest.py` so host `.env` or environment variables never leak into tests.
2. **Legacy `test_visual_quality_upgrade.py` purge-after-restore:** In v1.2.0, `purge_quality_upgrade` on a `'restored'` row MUST raise `ValueError` (HTTP 400). E2E tests should test restore on upgrade #1 and purge on upgrade #2 (and assert purging upgrade #1 after restore fails with `ValueError` / 400!).
3. **HEIC generation in tests:** With `pillow_heif` registered, Pillow can save real `.heic` files directly via `img.save(path, format="HEIF")` (or `pillow_heif.from_pillow(img).save(path)`).
4. **Disk-space guard determinism:** Rather than creating huge files, set `settings.min_free_disk_gb` high (e.g. `1_000_000.0`) to force `_check_disk_space()` to trigger deterministically on any host volume.
5. **Corrupt EXIF JPEG for G13:** Build a valid JPEG (`PIL.Image.save`), then inject a raw APP1 marker `b"\xff\xe1" + struct.pack(">H", len(payload)+2) + payload` right after SOI `b"\xff\xd8"` where `payload = b"Exif\x00\x00CORRUPT_TIFF_HEADER_BYTES"`. Verify `piexif.load` raises on it before calling `apply_metadata()`, then assert exact byte preservation + `status == "mtime_only"` + updated `mtime`.
6. **Crash simulation for G2:** Either (a) call `db.create_pending_quality_upgrade(...)`, move existing NAS file to `settings.quarantine_dir / q_name`, and instantiate/run a fresh `MigrationPipeline` to verify startup recovery restores the file to `old_rel_path`, OR (b) simulate Step 2 failure (e.g. read-only target dir / failing `importer.import_media`) and verify rollback restores the original file and deletes the `'in_progress'` row.

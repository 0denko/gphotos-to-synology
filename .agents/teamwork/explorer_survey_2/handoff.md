# Domain 2 Survey Handoff — Data Integrity, Pipeline, Takeout Processing, Dedup, Scanner, Database, Sources, Scheduler & Notifier

**Core Summary:** All 7 Domain 2 Major issues (**G1, G2, G9, G10, G11, G12, G13**) and all related Minors/Nits from Bingo's `v1.1.0-0003` review (`Collab.md`) have been verified against the live repository (`/home/antigravity/gphotos-to-synology`, baseline: 20/20 `pytest` passing, `ruff check` clean). Key cross-cutting findings include a critical `manylinux2014` wheel ceiling for `pillow-heif` (`1.0.0` is the last glibc-2.17 release for both `x86_64` and `aarch64`), two existing tests that directly codify pre-fix behavior (`test_visual_quality_upgrade.py:211-214` and `test_synology_api_and_security.py:17-23`), a hidden path-restoration bug in `quality_upgrades` (missing `old_rel_path`), and an API contract requiring synchronous `Notifier.send()` for `POST /api/actions/test-notification`.

---

## 1. Observation

### 1.1 File Layout & Module Boundaries (Domain 2)

| File Path (relative to `/home/antigravity/gphotos-to-synology/`) | Lines | Primary Responsibility | Key Classes / Functions |
|---|---:|---|---|
| `src/gphotos_synology/worker/pipeline.py` | 822 | End-to-end orchestration (NAS index → inbox/Drive ingest → dedup → upgrade/quarantine → album sync → reminders) | `PipelineProgress`, `MigrationPipeline` |
| `src/gphotos_synology/worker/scheduler.py` | 160 | Background worker thread cycling enabled profiles, pause/resume, manual triggers | `BackgroundScheduler` |
| `src/gphotos_synology/database.py` | 940 | Thread-safe WAL SQLite repository (`archives`, `media_items`, `quality_upgrades`, `albums`, `album_memberships`, `activity_logs`, `app_state`) | `utc_now_iso`, `SCHEMA_SQL`, `StateDB` |
| `src/gphotos_synology/notifications.py` | 76 | Webhook (ntfy/Discord/Slack/generic) & SMTP alerts *(note filename is `notifications.py`, not `notifier.py`)* | `Notifier` |
| `src/gphotos_synology/takeout/extractor.py` | 116 | ZIP/TGZ/TAR.GZ extraction, ZipSlip check, disk free helper, SHA-256, atomic move/copy | `is_supported_archive`, `extract_batch_key`, `get_free_disk_gb`, `_ensure_within`, `extract_archive`, `cleanup_directory`, `compute_file_sha256`, `safe_move_or_copy` |
| `src/gphotos_synology/takeout/matcher.py` | 285 | Media/video extension filters, album detection, Takeout JSON sidecar index & resolver | `MEDIA_EXTENSIONS`, `VIDEO_EXTENSIONS`, `is_media_file`, `is_video_file`, `detect_album_name`, `DirectoryJsonIndex`, `_candidate_json_names`, `find_sidecar_json` |
| `src/gphotos_synology/takeout/metadata.py` | 290 | Sidecar JSON parser, pure-Python `piexif` JPEG writer, ExifTool subprocess writer, `mtime` sync | `PhotoMetadata`, `_parse_timestamp`, `_extract_geo`, `parse_sidecar_json`, `exiftool_available`, `_deg_to_dms_rational`, `_apply_piexif_jpeg`, `apply_metadata` |
| `src/gphotos_synology/takeout/dedup.py` | 398 | 3-tier fingerprinting (file SHA-256, decoded RGB pixel SHA-256, 128-bit `dHash+vHash`), sharpness/quality comparison, perceptual match guardrails | `MediaFingerprint`, `ImageQualityMetrics`, `QualityDecision`, `_compute_composite_dhash_hex`, `_compute_sharpness`, `compute_quality_score`, `compare_image_quality`, `compute_media_fingerprint`, `hamming_distance_hex`, `_aspect_ratios_compatible`, `_timestamps_close`, `is_perceptual_match` |
| `src/gphotos_synology/sources/gdrive.py` | 245 | Google Drive OAuth2 token lifecycle, folder/archive discovery, chunked downloads, trashing | `DriveArchiveItem`, `GoogleDriveClient` |
| `src/gphotos_synology/sources/inbox.py` | 111 | Local inbox archive discovery, size-stability check, archive finalization (move/delete/keep) | `InboxArchiveItem`, `LocalInboxScanner` |
| `src/gphotos_synology/synology/importer.py` | 86 | Filename sanitization, `YYYY/MM` / `YYYY/YYYY-MM` / `Albums` folder computation, collision-safe move | `sanitize_component`, `SynologyFolderImporter` |
| `src/gphotos_synology/synology/scanner.py` | 127 | Read-only incremental scanner for pre-existing NAS photos (`@eaDir`/`#recycle` skip, stale pruning) | `IGNORED_DIR_NAMES`, `LibraryScanResult`, `ExistingLibraryScanner` |
| `src/gphotos_synology/synology/photos_api.py` | 305 | DSM 7 Synology Photos API client (login/logout, albums, folder/item resolution) | `SynologyAPIError`, `SynologyPhotosClient` |
| `src/gphotos_synology/config.py` | 255 | Pydantic settings, `MUTABLE_UI_FIELDS`, `SECRET_FIELDS`, `ConfigManager` | `Settings`, `ConfigManager` |

---

### 1.2 Detailed Findings — Major Issues (G1, G2, G9, G10, G11, G12, G13)

#### G1 — Archive completion with failed files
- **`src/gphotos_synology/worker/pipeline.py:494-513`** (`MigrationPipeline._ingest_archive_file`):
  Unconditionally sets `status="completed"` and `completed=True` even when `failed > 0`:
  ```python
  self.db.update_archive_progress(
      archive_id, status="completed", total_files=total_files,
      imported_files=imported, duplicate_files=duplicates,
      failed_files=failed, completed=True,
  )
  ```
- **`src/gphotos_synology/worker/pipeline.py:251-255`** (`MigrationPipeline._process_inbox_archive`):
  Calls `self._inbox_scanner.finalize_archive(item.path)` unconditionally after `_ingest_archive_file` and returns `True`. With `inbox_post_action == "delete"` (`sources/inbox.py:93-95`), `archive_path.unlink(missing_ok=True)` destroys the sole copy of failed files.
- **`src/gphotos_synology/worker/pipeline.py:303-312`** (`MigrationPipeline._process_gdrive_archive`):
  Guards `gdrive.trash_archive(item.file_id)` on `stats["failed"] == 0` (line 304), **but** because `_ingest_archive_file` already marked `status="completed"`, `run_cycle` (lines 179 and 203: `if existing and existing["status"] == "completed": continue`) permanently skips retrying both Drive and inbox archives with failures. Returns `True` even when `stats["failed"] > 0`.
- **`src/gphotos_synology/database.py:31, 896-905`**:
  `archives.status` currently documents `pending | downloading | processing | completed | failed`. `get_stats()` counts `status = 'completed'` as `completed_archives` and `status = 'failed'` as `failed_archives`.
- **`src/gphotos_synology/web/static/app.js:235`**:
  UI renders `completed` → `pill-ok`, `failed` → `pill-err`, anything else (such as `completed_with_errors`) → `pill-warn`.

#### G2 — Crash-atomic quality-upgrade swap & stale metrics
- **`src/gphotos_synology/worker/pipeline.py:515-636`** (`MigrationPipeline._maybe_upgrade_duplicate`):
  1. **Missing-file early return (`pipeline.py:529-531`)**:
     ```python
     existing_abs = settings.photos_dir / existing_rel
     if not existing_abs.is_file():
         return False
     ```
     Caller (`pipeline.py:413-436`) treats `upgraded == False` as `duplicates += 1; continue`. A deleted/lost file in `photos_dir` permanently blocks future imports of that photo!
  2. **Stale DB metrics (`pipeline.py:534-545`)**:
     Reads `width`, `height`, `sharpness`, `file_size` from the DB row and only re-fingerprints if `existing_width <= 0 or existing_height <= 0`. Never checks `existing_abs.stat()` (`st_size`, `st_mtime_ns`) against `existing["file_size"]` / `existing["file_mtime_ns"]` to detect external on-disk modifications before deciding which copy wins.
  3. **Non-atomic swap order (`pipeline.py:575-630`)**:
     - Line 586: `safe_move_or_copy(existing_abs, q_path)` — moves original out of `photos_dir` with **no** DB journal/intent record written first.
     - Lines 589-594: places `incoming_path` at `dest_abs` (in-place if same suffix, else via `importer.import_media`).
     - Lines 597-613: `self.db.upgrade_media_item(...)`.
     - Lines 614-630: `self.db.record_quality_upgrade(...)`.
     Any exception or power loss between line 586 and line 630 leaves `existing_abs` missing from `photos_dir`, DB pointing at the old phantom path/hash, and no `quality_upgrades` row (unrecoverable from UI).
  4. **Additional verified bug in `quality_upgrades` schema (`database.py:72-92`)**:
     Stores `kept_rel_path` and `old_filename`, but **not** `old_rel_path` (nor `old_taken_at`, `old_latitude`, `old_longitude`). When incoming has a different extension (e.g. `.jpg` replacing `.png`) and lands in a different `YYYY/MM` directory via `importer.import_media` (`pipeline.py:594`), `restore_quality_upgrade` (`pipeline.py:652`: `restore_abs = kept_abs.parent / str(row["old_filename"])`) restores to the wrong directory!

#### G9 — Strict sidecar matcher
- **`src/gphotos_synology/takeout/matcher.py:209-211`** (`_candidate_json_names`):
  For `(N)` numbered duplicates (`DUPLICATE_NUM_RE`), explicitly appends unnumbered base sidecars:
  ```python
  add(f"{unnumbered}.json")
  add(f"{unnumbered}{SUPPLEMENTAL_FULL}.json")
  ```
  Verified: `IMG_1234(1).jpg` silently binds to `IMG_1234.jpg.json` when no numbered sidecar exists, corrupting distinct photos' timestamps & GPS.
- **`src/gphotos_synology/takeout/matcher.py:220-222`** (`_candidate_json_names`):
  `add(f"{stem}.json")` and `add(f"{stem}{SUPPLEMENTAL_FULL}.json")` run in direct candidate search (`find_sidecar_json:235-238`) **before** internal `"title"` disambiguation (`lines 258-274`).
- **`src/gphotos_synology/takeout/matcher.py:146-168`** (`DirectoryJsonIndex.build`):
  - Line 150: `directory.iterdir()` is unsorted (nondeterministic order).
  - Line 156: `by_name_lower[name_lower] = entry` silently overwrites case-only filename collisions on case-sensitive Linux/DSM filesystems.
- **`src/gphotos_synology/takeout/matcher.py:258-274`** (`find_sidecar_json` title fallback):
  - Lines 261-263 add unnumbered `f"{dup_match.group('base')}{ext}".lower()` to `title_candidates` for `(N)` files.
  - Line 274 returns `matches[0]` unconditionally even when `len(matches) > 1` (ambiguous title).
- **`src/gphotos_synology/takeout/matcher.py:278-282`** (`find_sidecar_json` prefix fallback):
  Returns the first `json_path` starting with `filename[:46].lower()` (`len >= 35`) regardless of how many JSONs or media files share that 46-char prefix.
- **`src/gphotos_synology/takeout/metadata.py:40-49`** (`_parse_timestamp`):
  `ts = int(str(value).strip())` raises `ValueError` on float-formatted epoch strings (e.g. `"1720000000.0"`).

#### G10 — HEIC/HEIF visual fingerprinting & undecodable state
- **`src/gphotos_synology/takeout/matcher.py:15-16`**: `.heic` and `.heif` are in `MEDIA_EXTENSIONS` (along with RAW formats `.dng`, `.cr2`, `.cr3`, `.nef`, `.arw`, `.orf`, `.rw2`, `.raf`, `.avif`).
- **`pyproject.toml:23-24`**: depends on `pillow>=10.4.0,<13.0.0` and `piexif>=1.1.3,<2.0.0`; **no `pillow-heif`**.
- **CRITICAL CI/SPK Wheel Compatibility Finding (verified live against PyPI JSON API)**:
  - `.github/workflows/docker-publish.yml:39,42,60` builds the standalone SPK inside `quay.io/pypa/manylinux2014_x86_64` and `quay.io/pypa/manylinux2014_aarch64` (glibc 2.17) using `uv pip install --only-binary :all: -r pyproject.toml pyinstaller`.
  - `pillow-heif>=1.1.0` (including latest `1.8.1`) **dropped `manylinux2014` (`manylinux_2_17`)** and only publishes `manylinux_2_26_aarch64` / `manylinux_2_27_x86_64` / `manylinux_2_28` wheels.
  - **`pillow-heif==1.0.0`** (constraint `pillow-heif>=0.18.0,<1.1.0`) is the latest release shipping `manylinux_2_17` / `manylinux2014` wheels for **both `x86_64` and `aarch64`** across `cp311`, `cp312`, and `cp313` (requires `pillow>=11.1.0`).
  - PyInstaller in `.github/workflows/docker-publish.yml:63-76` will also need `--collect-all pillow_heif` (or verified collection of `libheif` shared libraries) when `pillow-heif` is added.
- **`src/gphotos_synology/takeout/dedup.py:11, 242-309`**:
  - Does not import/register `pillow_heif.register_heif_opener()`.
  - Lines 297-298: `except Exception as exc: logger.debug(...)` silently drops decode failures and returns `pixel_sha256=""`, `dhash=""`, `width=0`.
- **`src/gphotos_synology/database.py:442` & `src/gphotos_synology/synology/scanner.py:86-93`**:
  - `get_indexed_files_by_rel_path()` computes `has_visual = bool(r["pixel_sha256"]) and len(str(r["dhash"] or "")) == 32 and int(r["width"] or 0) > 0`.
  - `scanner.py:90`: skips only if `not compute_visual or existing_meta["has_visual"] or not self._is_image(full_path)`.
  - Therefore **every undecodable image** (HEIC without decoder, or RAW `.cr2`/`.nef`/`.arw`/`.dng`, or corrupt JPEG) is re-read and re-SHA-256'd on **every scan cycle**. Even with `pillow-heif` installed, RAW formats still require a persisted undecodable state (e.g. `visual_status TEXT NOT NULL DEFAULT ''` column: `'ok' | 'undecodable' | 'skipped'`) + `WARNING` log.

#### G11 — Perceptual dedup corroboration & SQL bucketing
- **`src/gphotos_synology/config.py:59-65`**: defaults are `dedup_mode="perceptual"`, `perceptual_max_distance=10`, `duplicate_quality_policy="auto_upgrade_quarantine"`.
- **`src/gphotos_synology/takeout/dedup.py:326-331`** (`_aspect_ratios_compatible`):
  Returns `True` when any dimension is `<= 0` (unknown dimensions pass the guardrail).
- **`src/gphotos_synology/takeout/dedup.py:349-397`** (`is_perceptual_match`):
  - Line 380-382: `ts_close is True` (within 15s) accepts any `dist <= effective_max` (up to 10).
  - Line 387: `dt_a.replace(tzinfo=UTC) - dt_b.replace(tzinfo=UTC)` clobbers non-UTC tzinfo instead of converting via `.astimezone(UTC)`.
  - Line 394: mismatched timestamps still match when `dist <= max(2, effective_max // 3)`.
  - Lines 396-397: when either timestamp is missing (`ts_close is None`), returns `same_stem or dist <= max(4, effective_max // 2)` — at `max_distance=10`, distance ≤5 matches with **no timestamp and different filename**, and distance ≤10 matches on `same_stem` alone.
- **`src/gphotos_synology/worker/pipeline.py:413-436`**:
  Perceptual-only duplicate skips (`match_reason == "perceptual_dhash"` and not upgraded) emit **no log event** at all (no reviewable audit trail).
- **`src/gphotos_synology/database.py:385-425`** (`find_duplicate_media` Tier 3):
  Lines 406-409 execute `SELECT * FROM media_items WHERE dhash != '' AND dhash != ?`, loading the **entire library's dhash table into Python on every incoming photo** while holding `self._lock` ($O(\text{library} \times \text{archive})$).

#### G12 — Decompression limits in extraction
- **`src/gphotos_synology/takeout/extractor.py:50-80`** (`extract_archive`):
  Signature: `extract_archive(archive_path: Path, dest_dir: Path) -> Path`.
  - No entry count limit, no pre-flight uncompressed byte sum, no streaming byte cap (`shutil.copyfileobj` copies unbounded bytes even if ZipInfo header lies), no compression-ratio cap.
  - `_ensure_within` (`lines 42-47`) raises `ValueError` on a single hostile path entry, aborting the whole archive.
- **`src/gphotos_synology/worker/pipeline.py:235, 269, 341`**:
  - Inbox pre-flight budgets `item.size_bytes * 1.5` (compressed size).
  - Drive pre-flight budgets `item.size_bytes * 2.2` (compressed size).
  - Calls `extract_archive(archive_path, extract_dir)` without passing a byte cap derived from actual free space minus `min_free_disk_gb`.
- **Existing test interaction**: `tests/test_synology_api_and_security.py:17-23` asserts `with pytest.raises(ValueError, match="path traversal"): extract_archive(malicious_zip, tmp_path / "extract")`.

#### G13 — `piexif.load` failure wiping existing EXIF
- **`src/gphotos_synology/takeout/metadata.py:130-134`** (`_apply_piexif_jpeg`):
  ```python
  try:
      exif_dict = piexif.load(str(media_path))
  except Exception:
      exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
  ```
  Proceeds to `piexif.dump` + `piexif.insert`, replacing the entire EXIF segment (orientation, make/model, lens, thumbnail, existing GPS).
- **Empirically verified in `.venv`**: `piexif.load` on a fresh PIL JPEG with **no** EXIF segment succeeds and returns `{'0th': {}, 'Exif': {}, 'GPS': {}, 'Interop': {}, '1st': {}, 'thumbnail': None}` without raising. Thus `piexif.load` raises **only** on malformed/unparseable EXIF or invalid JPEG; returning `False` in the `except` block (falling back to `mtime_only`) preserves no-EXIF JPEG writes while eliminating EXIF wipes!

---

### 1.3 Detailed Findings — Related Minors & Nits

| Area | File & Lines | Exact Verified Behavior | Required Fix / Constraint |
|---|---|---|---|
| **Album retry burning** | `pipeline.py:744-751`, `database.py:795-801, 828, 836-859` | `mark_album_membership(..., status="pending", error="Waiting for Synology Photos to index file")` increments `retry_count` in the `else:` branch (`database.py:854`). `get_pending_album_memberships` filters `retry_count < 20` (`line 828`). After 20 cycles, items are abandoned while still `'pending'`. `add_media_to_album` uses `INSERT OR IGNORE` (never resets `retry_count`). | Add `increment_retry: bool = True` (or never increment `retry_count` when `status == 'pending'`; only increment on `'error'`). |
| **Restore/purge worker race** | `pipeline.py:77`, `routes.py:192-212` | `MigrationPipeline` has only `_progress_lock` (for `PipelineProgress`), no cycle/operation lock. Routes call `restore_quality_upgrade`, `purge_quality_upgrade`, `purge_all_quarantined` directly from request threads concurrent with worker thread `run_cycle()`. | Add a per-pipeline operation lock (`self._op_lock = threading.RLock()` or `Lock`) guarding `_maybe_upgrade_duplicate`, `restore_quality_upgrade`, `purge_quality_upgrade`, `purge_all_quarantined`, and archive/scanner critical sections (or cycle-level lock — note avoid blocking `get_progress()`). |
| **Restore unsafe ordering & metadata** | `pipeline.py:638-682` | (1) No resolve-and-contain check on `q_path`, `kept_abs`, `restore_abs`. (2) `kept_abs.unlink()` (`line 654`) runs **before** `safe_move_or_copy(q_path, restore_abs)` (`line 656`). (3) Silently overwrites any pre-existing file at `restore_abs` if `restore_abs != kept_abs`. (4) Uses `kept_abs.parent / old_filename` instead of original `old_rel_path`. (5) Passes `taken_at=fp.exif_taken_at` (`line 676`), discarding recorded sidecar time/GPS. | Resolve & assert containment inside `quarantine_dir` / `photos_dir`; stage `kept_abs` to temp backup before moving `q_path` into place; avoid silent overwrite; rollback on error; store & restore `old_rel_path`, `old_taken_at`, `old_latitude`, `old_longitude`. |
| **Purge status guard & 1000-row cap** | `pipeline.py:684-703`, `database.py:755-761` | `purge_quality_upgrade` has no status check (purging a `'restored'` upgrade overwrites status to `'purged'`, **codified in `tests/test_visual_quality_upgrade.py:211-214`**) and no resolve-and-contain check. `purge_all_quarantined` fetches `list_quality_upgrades(limit=1000)` once and filters in Python. | Guard `if row["status"] != "quarantined": raise ValueError(...)`; add resolve-and-contain check; query `WHERE status = 'quarantined'` in batches until empty; update `test_visual_quality_upgrade.py`. |
| **Drive MD5, token chmod race, auth errors, folder pagination, download resume** | `sources/gdrive.py:96-103, 108-129, 137-149, 199-239`, `pipeline.py:279, 293-297` | (1) `item.md5_checksum` fetched (`line 188`) but never verified against downloaded bytes. (2) `_save_credentials` (`lines 99-103`) writes `.tmp` under default umask then `chmod(0o600)` with suppressed `OSError`. (3) `is_connected()` (`lines 125-129`) swallows token-refresh exceptions, hiding revoked/expired token errors. (4) `find_takeout_folder_ids` (`lines 137-149`) uses `pageSize=20` with no `nextPageToken` loop. (5) `download_archive` (`lines 212-238`) unlinks `.part` on error and restarts from byte 0. | Verify MD5 if `item.md5_checksum` is non-empty; use `os.open(tmp, O_WRONLY\|O_CREAT\|O_TRUNC, 0o600)` + log warning on chmod error; expose `last_error` / `connection_status()`; paginate `find_takeout_folder_ids`; support Range-based resume on `.part` when practical (falling back cleanly on 200 or MD5 mismatch). |
| **`get_media_by_original_hash` ordering** | `database.py:335-341, 365-368` | Both `get_media_by_original_hash` and `find_duplicate_media` Tier 1 use `WHERE original_sha256 = ? OR final_sha256 = ?` with no `ORDER BY`. | Add `ORDER BY CASE WHEN original_sha256 = ? THEN 0 ELSE 1 END, id ASC LIMIT 1`. |
| **Scanner exception aborts cycle** | `pipeline.py:168-174` | `index_existing_library()` is inside `run_cycle`'s single outer `try:` block; any scanner error skips inbox & Drive ingestion. | Wrap `index_existing_library()` in its own `try/except`, matching Drive poll isolation (`lines 191-199`). |
| **Notifier sync + unthrottled disk guard** | `notifications.py:16-30, 35, 66`, `pipeline.py:96-109`, `routes.py:331-344` | `Notifier.send()` runs blocking 15s HTTP / 20s SMTP calls inline. `_check_disk_space()` sends alerts every cycle/archive unthrottled. **Contract:** `routes.py:334` (`test-notification`) relies on synchronous `bool` return from `Notifier.send()`. | Add non-blocking `send_async(...)` (daemon thread) for pipeline callers while keeping synchronous `send(...)` for `test-notification`; throttle disk-guard alerts via `app_state` (`last_disk_guard_alert_at`, e.g. 6h/12h/24h cooldown). |
| **Metadata: OffsetTime, GPS 60s clamp, ImageDescription, SPK ExifTool clarity** | `metadata.py:114-120, 135-142, 186-189, 203-224` | (1) UTC `DateTimeOriginal` written without `OffsetTimeOriginal` (empirically verified `piexif.ExifIFD` has `OffsetTime`, `OffsetTimeOriginal`, `OffsetTimeDigitized`, `UserComment`). (2) `_deg_to_dms_rational(37.99999999)` verified to emit `((37, 1), (59, 1), (600000, 10000))`. (3) `ImageDescription` written as raw UTF-8 into ASCII tag. (4) No ExifTool in SPK (`Dockerfile:18` has it, `build_spk.py` does not); non-JPEG files silently degrade to `"mtime_only"` with no diagnostic. | Carry `s >= 600000` → `m += 1` and `m >= 60` → `d += 1`; write `+00:00` to `OffsetTime*` in both `piexif` and ExifTool paths; write ASCII to `ImageDescription` and UTF-8/Unicode to `UserComment` when non-ASCII; log clear warning / status clarity when ExifTool is absent for non-JPEGs. |
| **Inbox readiness & symlinks** | `sources/inbox.py:47-69, 88-110` | Follows symlinks on `entry.is_file()` & `entry.resolve()`, while `finalize_archive` unlinks/moves the symlink itself. Stability check compares `st_size` only (not `st_mtime_ns` or archive structure). | Skip symlinks (`if entry.is_symlink(): continue`); track `(st_size, st_mtime_ns)` stability (and optionally verify ZIP central directory / gzip header readability before treating as ready). |
| **Quality heuristics** | `takeout/dedup.py:224-232` | Rule 3 (`incoming.file_size >= existing.file_size * 1.15`) wins without checking sharpness, allowing noise/metadata bloat to beat a sharper image. | Require non-inferior sharpness (e.g. `existing.sharpness <= 0 or incoming.sharpness >= existing.sharpness * 0.98`) for the ≥15% file-size rule. |
| **Bookkeeping nits** | `database.py:207-222, 243-270`, `scheduler.py:66-71, 81-82, 136-157`, `extractor.py:107` | (1) `upsert_archive` does not reset `total_files`/`imported_files`/`duplicate_files`/`failed_files`/`completed_at` on retry. (2) `log_event` prunes via `DELETE ... NOT IN` on every insert. (3) `scheduler.py` sets `next_run_at` while paused and omits `last_run_at` on manual `albums_only`/`index_existing` runs. (4) `stop()` joins 10s with no cooperative stop check in pipeline and no cleanup of stale `.tmp.<pid>` partials left in `photos_dir` by interrupted cross-FS `safe_move_or_copy`. | Reset archive counters + `completed_at = NULL` on retry; prune `activity_logs` every N inserts (e.g. 50); set `next_run_at=None` when paused; set `last_run_at=utc_now_iso()` on manual runs; clean stale `.*.tmp.*` partials on startup/shutdown/stop and pass stop check if desired. |
| **Synology Photos API nits (Domain 2 neighbor)** | `synology/photos_api.py:82-92, 155-180, 266-304` | `_sid` sent in GET query params; no session-expiry (code 119) re-login; `_album_cache` never invalidated; root-level `resolve_item_id` (`folder_id=None`) queries across all folders and can match a same-named file in a subfolder. | Switch authenticated `_entry_request` calls to POST body; handle code 119 by re-logging in once; invalidate album cache on error/new cycle; check `item.get("folder_id")` or root folder ID for root-level files. |

---

### 1.4 Cross-Module Interface Contracts

#### Database Schema & Public Methods (`StateDB` in `src/gphotos_synology/database.py`)
- **Tables**:
  - `archives(id, source, source_id, filename, batch_key, size_bytes, checksum, status, total_files, imported_files, duplicate_files, failed_files, error_message, created_at, updated_at, completed_at)` — `UNIQUE(source, source_id)`.
  - `media_items(id, original_sha256 UNIQUE, final_sha256, pixel_sha256, dhash, width, height, sharpness, quality_score, origin, original_filename, dest_rel_path, file_size, file_mtime_ns, taken_at, latitude, longitude, exif_status, source_archive_id, synology_item_id, created_at)` — indexes on `dest_rel_path`, `final_sha256`, `pixel_sha256`, `dhash`.
  - `quality_upgrades(id, media_id, kept_rel_path, quarantine_filename, old_filename, new_filename, old_width, old_height, old_file_size, old_sharpness, new_width, new_height, new_file_size, new_sharpness, match_reason, upgrade_reason, status, created_at, resolved_at)` — index on `status`.
  - `albums(id, name UNIQUE, synology_album_id, created_at, updated_at)`.
  - `album_memberships(album_id, media_id, sync_status, retry_count, last_error, updated_at)` — `PRIMARY KEY (album_id, media_id)`, index on `sync_status`.
  - `activity_logs(id, level, component, message, created_at)`.
  - `app_state(key PRIMARY KEY, value, updated_at)`.
- **Additive Migration Pattern (`database.py:160-183`)**: Uses `PRAGMA table_info(...)` + `ALTER TABLE ... ADD COLUMN ...` + `CREATE INDEX IF NOT EXISTS ...`. **All new columns** (e.g. `media_items.visual_status`, `media_items.dhash_b0..bN`, `quality_upgrades.old_rel_path`, `quality_upgrades.old_taken_at`, `quality_upgrades.old_latitude`, `quality_upgrades.old_longitude`) **must** follow this idempotent additive migration pattern so existing NAS databases upgrade cleanly.
- **External Callers of `StateDB`**:
  - `web/routes.py`: `log_event`, `get_stats`, `list_archives`, `list_albums_summary`, `get_recent_logs`, `list_quality_upgrades`, `get_quality_upgrade`, `set_state`, `get_state`.
  - `worker/scheduler.py`: `get_state("worker_paused")`, `set_state("worker_paused", ...)`, `log_event`.
  - `profiles.py`: `StateDB(cfg_mgr.settings.db_path)`, `get_stats()`.
  - `synology/scanner.py`: `get_indexed_files_by_rel_path`, `upsert_existing_nas_media`, `prune_missing_existing_nas_media`.

#### `MigrationPipeline` External Callers (`src/gphotos_synology/worker/pipeline.py`)
- `web/routes.py`:
  - `pctx.pipeline.get_progress()` (`routes.py:118`)
  - `pctx.pipeline.restore_quality_upgrade(upgrade_id)` (`routes.py:196`)
  - `pctx.pipeline.purge_quality_upgrade(upgrade_id)` (`routes.py:204`)
  - `pctx.pipeline.purge_all_quarantined()` (`routes.py:211`)
  - `pctx.pipeline._set_progress(renewal_reminder_due=False)` (`routes.py:309`)
- `worker/scheduler.py`:
  - `pipe.run_cycle()` (`scheduler.py:149`)
  - `pipe.sync_pending_albums()` (`scheduler.py:137`)
  - `pipe.index_existing_library()` (`scheduler.py:143`)
  - `pipe._set_progress(...)` (`scheduler.py:82, 89, 138, 144, 152, 157`)
- `profiles.py`:
  - `MigrationPipeline(config_manager=cfg_mgr, db=db)` (`profiles.py:151`)
  - `ctx.pipeline.get_progress()` (`profiles.py:174`)

#### Takeout Utility Signatures & Callers
- `extractor.py`:
  - `extract_archive(archive_path: Path, dest_dir: Path, ...) -> Path` — called by `pipeline.py:341` and `test_synology_api_and_security.py:23`. Keep new limit parameters optional with safe defaults so existing callers remain compatible.
  - `safe_move_or_copy(src: Path, dst: Path) -> None` — called by `pipeline.py:586, 591, 656` and `importer.py:79`.
  - `get_free_disk_gb(path: Path) -> float` — called by `pipeline.py:98-99` and `routes.py:106-107, 128-129`.
- `dedup.py`:
  - `compute_media_fingerprint(path: Path, *, compute_visual: bool = True) -> MediaFingerprint` — called by `pipeline.py:395, 541, 658` and `scanner.py:95`.
  - `is_perceptual_match(incoming_dhash, existing_dhash, *, incoming_taken_at, existing_taken_at, incoming_filename, existing_filename, incoming_width=0, incoming_height=0, existing_width=0, existing_height=0, max_distance=10) -> bool` — called by `database.py:391, 411`.
  - `compare_image_quality(incoming: ImageQualityMetrics, existing: ImageQualityMetrics) -> QualityDecision` — called by `pipeline.py:546`.
- `metadata.py`:
  - `apply_metadata(media_path: Path, metadata: PhotoMetadata, *, write_exif: bool = True, write_album_tags: bool = True, exiftool_path: str = "exiftool") -> str` — called by `pipeline.py:438, 566` and `test_metadata_and_importer.py:59, 93`.
  - `exiftool_available(exiftool_path: str = "exiftool") -> bool` — called by `routes.py:105, 124`.

---

### 1.5 Existing Test Suite & Coverage Gaps (Domain 2)

Baseline verified via `.venv/bin/pytest -q`: **20 tests passing** across 9 test files (`ruff check src tests` clean).

| Test File | Tests | What Is Covered | Critical Gaps / Breaking Expectations |
|---|---:|---|---|
| `tests/test_pipeline_e2e.py` | 1 | Single happy-path inbox ZIP (`write_exif_metadata=False`, 3 files, 1 dup, album sync) | Missing: failed file in archive (G1 status/finalize/delete/retry), Drive download+MD5+trash path, disk guard + throttling, quality-upgrade crash/rollback (G2), EXIF-mutation + post-repair hash path, scanner failure isolation. |
| `tests/test_existing_nas_dedup.py` | 3 | Scanner skip `@eaDir`/`#recycle` + incremental + prune; exact+pixel dedup; perceptual dedup at `perceptual_max_distance=4` | Missing: HEIC/HEIF fingerprinting & undecodable-image stable skip (G10), burst-shot / missing-timestamp / unknown-dimension false-positive negatives (G11), SQL dhash bucket recall/perf test, video dedup, scanner read-only assertion. |
| `tests/test_visual_quality_upgrade.py` | 3 | Higher-res upgrade; sharper same-res upgrade & no downgrade; API preview/restore/purge | **Codifies bug at lines 211-214**: calls `POST /api/upgrades/{uid}/purge` right after `restore` and asserts 200 + `status == "purged"`. Missing: G2 crash recovery & rollback, missing-file repair, stale DB stat refresh, restore preserving sidecar metadata & `old_rel_path`, restore/purge path-traversal rejection, `purge_all_quarantined` >1000 rows, sharpness guard on ≥15% size rule. |
| `tests/test_takeout_matcher.py` | 4 | Happy-path `.json`/`.supplemental-metadata`, numbered `(1)`/`(2)`, edited, Live Photo, 46-char trunc, title fallback, album detection | Missing: G9 negative test (`IMG_1234(1).jpg` must NOT match `IMG_1234.jpg.json`), ambiguous title fallback returning `None`, ambiguous 46-char prefix returning `None`, case-collision handling, stem-only after title disambiguation. |
| `tests/test_metadata_and_importer.py` | 3 | Sidecar JSON parse + `(0,0)` GPS fallback; mtime fallback + importer collision `_2`; `piexif` on fresh no-EXIF JPEG | Missing: G13 corrupt/unparseable EXIF must skip write and preserve original bytes; GPS DMS `37.99999999` seconds=60 clamp; `OffsetTimeOriginal` `+00:00`; non-ASCII `ImageDescription` handling; `_parse_timestamp` float strings; ExifTool invocation mock. *(Note: lines 103-104 assert UTC `DateTimeOriginal` and ASCII `ImageDescription` — keep compatible.)* |
| `tests/test_synology_api_and_security.py` | 2 | ZipSlip `ValueError` on `extract_archive`; `SynologyPhotosClient` mock login/resolve/album add | **Line 22-23 asserts `extract_archive` raises `ValueError(match="path traversal")`**. Missing: TAR/TGZ ZipSlip & symlink/device skip, G12 decompression limits (entry count, uncompressed bytes, ratio, streaming forged header), per-entry hostile skip mode, `photos_api` POST `_sid` + code 119 re-login + root-level `resolve_item_id`. |
| *(missing)* `test_scheduler.py` | 0 | None | Zero tests for `BackgroundScheduler` (`pause`/`resume` `next_run_at`, manual `last_run_at`, `stop()` cleanup, profile cycling). |
| *(missing)* `test_database.py` | 0 | None | Zero direct unit tests for `StateDB` (`upsert_archive` retry reset, `get_media_by_original_hash` ordering, `album_memberships` retry counting on waiting-for-index, `log_event` pruning cadence, dhash prefix bucketing, additive migrations). |
| *(missing)* `test_gdrive_and_inbox.py` | 0 | None | Zero tests for `GoogleDriveClient` (MD5 verification, 0o600 token creation, refresh error surface, folder pagination, download retry/resume) and `LocalInboxScanner` (symlink skipping, mtime+size stability). |
| *(missing)* `test_notifications.py` | 0 | None | Zero tests for async `Notifier` dispatch and disk-guard throttling. |

---

## 2. Logic Chain

1. **G1 + G12 + Inbox/Drive parity** directly protect user source archives:
   - From Observation 1.2 (G1), `_ingest_archive_file` marks `completed` regardless of `failed > 0`, and `_process_inbox_archive` unconditionally finalizes (potentially unlinking) the source ZIP. Gating completion (`status = "completed_with_errors"`, `completed = False` when `failed > 0`) and gating `finalize_archive` on `failed == 0` restores parity with Drive (`pipeline.py:304`) and makes partial failures retryable on subsequent cycles (leveraging Tier-1 hash dedup to skip already-imported items).
   - From Observation 1.2 (G12), unbounded extraction can exhaust disk mid-archive. Adding pre-flight member-size/count checks AND streaming byte counters capped by `max(0, free_bytes - min_free_disk_bytes)` prevents filling the NAS volume. To preserve fail-closed behavior tested by `test_synology_api_and_security.py:22-23`, `extract_archive` can default to `skip_unsafe_entries=False` (raising on traversal) while `pipeline.py` can opt into `skip_unsafe_entries=True` with per-entry warning logs (or record skipped counts).
2. **G2 + Restore/Purge Race & Safety** form a single transactional subsystem around `quality_upgrades`:
   - From Observation 1.2 (G2) and 1.3 (Restore/Purge), both upgrade and restore/purge mutate the same `photos_dir` + `quarantine_dir` + `media_items` + `quality_upgrades` state without a shared lock or write-ahead journal.
   - Adding: (a) a per-pipeline operation lock (`_op_lock`), (b) write-ahead `quality_upgrades` intent rows (`status='in_progress'`) storing `old_rel_path`, `old_taken_at`, `old_latitude`, `old_longitude` *before* moving `existing_abs`, (c) rollback in `except` + startup/`run_cycle` recovery for orphaned `in_progress` rows, (d) missing-file repair & fresh `stat()` check before `compare_image_quality`, (e) safe temp-staged restore + strict `status == 'quarantined'` purge guard + resolve-and-contain checks + batched `purge_all_quarantined` resolves all G2 and restore/purge defects coherently.
3. **G9 + G13 + Metadata details** eliminate silent metadata corruption:
   - From Observation 1.2 (G9), removing unnumbered sidecar fallback for `(N)` files (both in `_candidate_json_names:209-211` and title fallback `261-263`), enforcing uniqueness on title and prefix fallbacks, moving stem-only candidates after exact title matching, sorting `iterdir()`, and handling case collisions ensures ambiguous/distinct photos get *no* metadata rather than *wrong* metadata.
   - From Observation 1.2 (G13), empirical verification proved `piexif.load` returns empty dicts for EXIF-less JPEGs and only raises on corrupt/unparseable EXIF; returning `False` on `piexif.load` failure completely eliminates EXIF wiping with zero regressions on EXIF-less JPEGs. Adding DMS seconds=60 carry, `OffsetTimeOriginal="+00:00"`, and ASCII/UserComment `ImageDescription` fixes all metadata minors cleanly.
4. **G10 + G11 + SQL Bucketing** fix dedup accuracy and scalability without breaking SPK builds:
   - From Observation 1.2 (G10), `pillow-heif` must be constrained to `<1.1.0` (i.e. `pillow-heif>=0.18.0,<1.1.0`, resolving to `1.0.0`) so `manylinux2014` (`glibc 2.17`) `--only-binary :all:` SPK builds succeed for both `x86_64` and `aarch64`, and `visual_status` must be persisted in `media_items` so scanner skips undecodable files (RAW/corrupt/unhandled HEIC) instead of re-hashing every cycle.
   - From Observation 1.2 (G11), tightening `is_perceptual_match` (unknown dimensions fail looser branches; require timestamp or filename corroboration when `dist > 4`; fix tz conversion with `.astimezone(UTC)`), adding a distinct `perceptual-dedup` log component, and adding SQL dHash prefix bucket columns eliminates false positives and $O(N \times M)$ scans.
   - **Pigeonhole math for SQL dHash bucketing (`database.py`)**: A 128-bit `dhash` is 32 hex chars. Splitting into **8 indexed 4-hex-char (16-bit) columns** (`dhash_b0..dhash_b7`, each with 65,536 buckets) guarantees **100% exact recall** via `WHERE dhash_b0 = ? OR ... OR dhash_b7 = ?` whenever `effective_max <= 7` (by pigeonhole principle, $\le 7$ differing bits across 8 blocks means $\ge 1$ block matches identically, scanning only $\sim 8/65536 \approx 0.012\%$ of the library!). When `effective_max >= 8` (e.g. default 10 or user-configured up to 32) or legacy 16-char hashes when `effective_max >= 4`, either:
     - Use **11–12 blocks** (or lower default `perceptual_max_distance` to `6` or `7`), **and** keep an automatic fallback to full scan if `effective_max >= num_buckets` so recall is never silently lost for high user-configured distances.
     - Note: in SQLite, querying a dedicated lookup table `media_dhash_buckets(bucket_idx INTEGER, bucket_val TEXT, media_id INTEGER)` with `PRIMARY KEY (bucket_idx, bucket_val, media_id)` (or `WITHOUT ROWID`) is even cleaner than 8–12 nullable columns + 8–12 separate indexes, supports arbitrary $k$ buckets (e.g. 8 or 12), and joins back to `media_items` with a single index range scan!

---

## 3. Recommended Module Decomposition Boundaries for Implementation

Domain 2 changes cleanly partition into **4 cohesive, low-conflict workstreams** (minimal overlapping file edits):

| Workstream | Files Owned | Issues Covered |
|---|---|---|
| **2A — Takeout Ingestion Safety & Metadata** | `takeout/extractor.py`, `takeout/matcher.py`, `takeout/metadata.py` | G9, G12, G13, `_parse_timestamp` floats, DMS 60s clamp, `OffsetTimeOriginal`, `ImageDescription` ASCII/Unicode, non-JPEG ExifTool warning, per-entry hostile skip option |
| **2B — Dedup, HEIC & Scanner** | `pyproject.toml` (coordinate `pillow-heif>=0.18.0,<1.1.0` with packaging team), `takeout/dedup.py`, `synology/scanner.py` | G10 (`pillow-heif` opener + `WARNING` + `visual_status`), G11 (`is_perceptual_match` corroboration, unknown-dim guardrail, tz bug), sharpness guard on ≥15% size rule |
| **2C — Database & Sources** | `database.py`, `sources/gdrive.py`, `sources/inbox.py`, `synology/importer.py`, `synology/photos_api.py` | Additive migrations (`visual_status`, dHash buckets, `quality_upgrades` journal/old-metadata columns), `get_media_by_original_hash` ordering, `upsert_archive` retry reset, `album_memberships` waiting-for-index retry fix, `log_event` periodic prune; Drive MD5/atomic-0o600/refresh-error/pagination/resume; Inbox symlink skip + `(size, mtime_ns)` stability; Photos API POST `_sid` / code 119 / root `resolve_item_id` |
| **2D — Pipeline, Scheduler & Notifier** | `worker/pipeline.py`, `worker/scheduler.py`, `notifications.py`, `web/routes.py` (minimal status/error hookups — coordinate with Domain 1) | G1 (`completed_with_errors` + finalize guard), G2 (journaled crash-atomic upgrade + rollback + startup recovery + missing-file repair + fresh stat), `_op_lock` + safe restore/purge + `purge_all_quarantined` batching, scanner try/except isolation, perceptual-dedup log category, async `Notifier` + disk-guard throttle, scheduler `next_run_at`/`last_run_at`/`.tmp.<pid>` cleanup |

---

## 4. Caveats

1. **`pillow-heif` + PyInstaller SPK bundling**: PyPI metadata confirms `pillow-heif==1.0.0` ships `manylinux_2_17` wheels for both `x86_64` and `aarch64` (`cp311`/`cp312`), but actual runtime HEIC decoding inside a PyInstaller `--onedir` bundle on `manylinux2014` was not executed in this read-only survey. CI build & smoke test must verify `--collect-all pillow_heif` bundles `libheif` cleanly. Also note `pillow-heif==1.0.0` requires `pillow>=11.1.0` (current `.venv` has `Pillow 12.3.0`; `pyproject.toml` currently says `pillow>=10.4.0,<13.0.0`).
2. **Existing tests requiring deliberate updates**:
   - `tests/test_visual_quality_upgrade.py:211-214` currently asserts purging an already-restored upgrade returns HTTP 200 and changes status to `"purged"`. Fixing the purge status guard requires splitting that test into a restore assertion and a separate purge assertion (+ a negative test that purging a restored upgrade returns HTTP 400).
   - `tests/test_synology_api_and_security.py:17-23` asserts `extract_archive` raises `ValueError(match="path traversal")` on ZipSlip. Keep `skip_unsafe_entries: bool = False` as the default so direct callers remain fail-closed while the pipeline can opt into skip-and-log (or test both modes explicitly).
   - `tests/test_metadata_and_importer.py:103-104` asserts `DateTimeOriginal == b"2023:08:20 18:45:12"` and `ImageDescription == b"Pier sunset"`; adding `OffsetTimeOriginal == b"+00:00"` and keeping ASCII `ImageDescription` preserves 100% compatibility.
3. **Cross-Domain coordination with Domain 1 (Auth/Security/Web/Profiles/SPK)**:
   - `web/routes.py` and `config.py` and `pyproject.toml` are shared with Domain 1; coordinate `routes.py` edits (`test-notification`, `gdrive` auth error surface in `/api/status`, upgrade restore/purge HTTP status codes) and `pyproject.toml` (`pillow-heif` + `uv.lock`) to avoid merge conflicts.
4. **Google Drive download resume**: `googleapiclient.http.MediaIoBaseDownload` does not natively resume across process restarts from an existing local `.part` file without setting internal progress state or using an authorized `Range` GET request; verify carefully with mocked Drive responses in unit tests before shipping.

---

## 5. Conclusion

Domain 2 is cleanly modularized and ready for implementation across the 4 workstreams (2A–2D). Every claimed defect in Bingo's audit for Domain 2 is confirmed at exact source locations, plus three additional subtle defects uncovered during verification:
1. `quality_upgrades` lacks `old_rel_path` (causing cross-extension restores to land in the wrong `YYYY/MM` directory) and lacks `old_taken_at`/`old_latitude`/`old_longitude`.
2. `dedup.py:387` uses `.replace(tzinfo=UTC)` instead of `.astimezone(UTC)` in `is_perceptual_match`, corrupting timezone-aware timestamp comparisons.
3. `pillow-heif>=1.1.0` drops `manylinux2014` wheels — pinning `pillow-heif>=0.18.0,<1.1.0` (resolving to `1.0.0`) is mandatory to keep the native DSM 7 SPK CI builds green on glibc 2.17.

---

## 6. Verification Method

To independently verify all observations in this report:
1. **Baseline test & lint check**:
   ```bash
   cd /home/antigravity/gphotos-to-synology
   .venv/bin/pytest -v
   .venv/bin/ruff check src tests
   ```
   Expected: 20 passed, `All checks passed!`.
2. **Verify `piexif.load` and GPS DMS overflow empirically**:
   ```bash
   .venv/bin/python -c "import io, piexif; from PIL import Image; from gphotos_synology.takeout.metadata import _deg_to_dms_rational; b=io.BytesIO(); Image.new('RGB',(16,16)).save(b,format='JPEG'); print(piexif.load(b.getvalue())); print(_deg_to_dms_rational(37.99999999))"
   ```
   Expected: empty EXIF dicts returned (no exception), and `((37, 1), (59, 1), (600000, 10000))`.
3. **Verify `pillow-heif` wheel tags on PyPI**:
   ```bash
   curl -fsSL https://pypi.org/pypi/pillow-heif/1.0.0/json | .venv/bin/python -c "import sys,json; print([u['filename'] for u in json.load(sys.stdin)['urls'] if '2014' in u['filename'] and 'cp312' in u['filename']])"
   ```
   Expected: both `manylinux2014_x86_64` and `manylinux2014_aarch64` wheels present for `1.0.0` (and absent in `1.1.0+`).
4. **Files to inspect**: All exact line references in Sections 1.2–1.5 (`worker/pipeline.py`, `worker/scheduler.py`, `database.py`, `notifications.py`, `takeout/{extractor,matcher,metadata,dedup}.py`, `sources/{gdrive,inbox}.py`, `synology/{importer,scanner,photos_api}.py`, `tests/test_visual_quality_upgrade.py:204-215`, `tests/test_synology_api_and_security.py:17-24`).
5. **Invalidation conditions**: Any commit changing `src/gphotos_synology/` after HEAD `dfcf34f` or bumping the CI SPK build container off `manylinux2014` would alter line numbers or the `pillow-heif<1.1.0` wheel constraint.

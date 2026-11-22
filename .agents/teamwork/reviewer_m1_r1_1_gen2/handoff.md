# Milestone M1 Review Report — Reviewer 1 (`reviewer_m1_r1_1_gen2`)

## Independent Verification Commands & Outputs

All canonical checks were executed independently against `/home/antigravity/gphotos-to-synology`:

1. **`uv lock --check` & Wheel Audit (`pillow==12.2.0`, `pillow-heif==1.0.0`)**
   - Command:
     ```bash
     uv lock --check && uv sync --locked --extra dev --check && .venv/bin/python -c "import gphotos_synology, PIL, pillow_heif; print(gphotos_synology.__version__, PIL.__version__, pillow_heif.__version__)"
     ```
   - Exit code: `0`
   - Output:
     ```text
     Resolved 65 packages in 3ms
     Would use project environment at: .venv
     Resolved 65 packages in 1ms
     Checked 57 packages in 1ms
     Would make no changes
     1.2.0 12.2.0 1.0.0
     ```
   - Verified `uv.lock` contains `manylinux2014` / `manylinux_2_17` wheels for both `x86_64` and `aarch64` (cp311 and cp312) for `pillow==12.2.0` and `pillow-heif==1.0.0`.

2. **Linter (`.venv/bin/ruff check src tests --exclude tests/e2e`)**
   - Exit code: `0`
   - Output: `All checks passed!`

3. **Test Suite (`.venv/bin/pytest -v --ignore=tests/e2e`)**
   - Exit code: `0`
   - Output: `56 passed, 1 warning in 2.31s` (20 existing tests + 26 in `tests/test_config.py` + 10 in `tests/test_database.py`).

4. **Exclusive File Ownership Check (`git status --short`)**
   - Only the 7 M1 files (`pyproject.toml`, `uv.lock`, `src/gphotos_synology/__init__.py`, `src/gphotos_synology/config.py`, `src/gphotos_synology/database.py`, `tests/test_config.py`, `tests/test_database.py`) were touched in the M1 scope.

---

## File-by-File Audit Against `ORIGINAL_REQUEST.md`, `PROJECT.md`, `Collab.md`, and `DISPATCH.md`

### 1. [`pyproject.toml`](file:///home/antigravity/gphotos-to-synology/pyproject.toml), [`uv.lock`](file:///home/antigravity/gphotos-to-synology/uv.lock), [`src/gphotos_synology/__init__.py`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/__init__.py)
- **Version unify (G7)**: `version = "1.2.0"` in `pyproject.toml` and `__version__ = "1.2.0"` in `__init__.py`.
- **HEIC & `manylinux2014` wheel compatibility (G10)**: Tightened `"pillow>=11.1.0,<12.3.0"` (locks `12.2.0`) and added `"pillow-heif>=1.0.0,<1.1.0"` (locks `1.0.0`). Both ship glibc-2.17 `manylinux2014` wheels for `x86_64` and `aarch64`.
- **SPK extra**: Added `spk = ["pyinstaller>=6.10.0,<7.0.0"]` under `[project.optional-dependencies]`.

### 2. [`src/gphotos_synology/config.py`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py)
- **G5 (Secure defaults & quarantine override)**:
  - [`Settings`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L59-L244) defaults: `app_host = "127.0.0.1"`, `synology_url = "https://127.0.0.1:5001"`, `synology_verify_ssl = True`, `synology_ca_cert = ""`, and `quarantine_dir_override: Path | None = None` with `AliasChoices("quarantine_dir", "quarantine_dir_override")` and blank-string-to-`None` coercion.
  - [`Settings.quarantine_dir`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L192-L196) returns `quarantine_dir_override` when set, otherwise `work_dir / "replaced_duplicates"`.
- **C1 / C2 / G6 Security Fields & Allowlists**:
  - Added `proxy_secret`, `proxy_secret_file`, `api_token`, `dsm_admin_users`, `trust_proxy_headers`, `allow_insecure_no_auth`.
  - [`Settings.resolve_proxy_secret()`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L198-L213) returns stripped `proxy_secret` or reads `proxy_secret_file` (failing closed with `WARNING` log on `OSError`).
  - [`Settings.dsm_admin_user_set()`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L215-L221) normalizes comma-separated admin usernames.
  - `proxy_secret` and `api_token` are in `SECRET_FIELDS`, and all security/path/Watchtower fields are excluded from `MUTABLE_UI_FIELDS` (and filtered on disk load).
- **G4 (Prevent `synology_url` change from exfiltrating stored DSM credentials)**:
  - [`validate_synology_url()`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L27-L42) enforces `http://` or `https://` scheme, valid hostname/port, and no whitespace or control characters.
  - [`ConfigManager.update()`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L449-L552) validates `synology_url` before mutating state and clears `existing["synology_password"] = ""` (overriding even env-var passwords across reloads) whenever the normalized URL changes without a fresh non-empty, non-`"********"` password, logging a `WARNING` and populating `self.last_update_warnings`.
- **Lazy OAuth & URL Inheritance**:
  - [`ConfigManager.settings`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L380-L408) dynamically inherits unset `google_client_id`, `google_client_secret`, and default/unset `synology_url` from `fallback_settings_provider()`, avoids freezing inherited values on full-form UI saves, and pins `synology_url` locally whenever a profile saves a non-empty `synology_password`.
- **Config Resilience & Atomic `0o600` Writes**:
  - [`_backup_corrupt_file()`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L295-L327) logs an `ERROR` and moves corrupt `runtime_settings.json` files to `runtime_settings.json.corrupt.<UTC_timestamp>` before falling back to base settings.
  - [`_write_overrides_atomic()`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L330-L360) opens the temporary file with `os.open(..., 0o600)` (no umask race), flushes + `os.fsync()`, logs a `WARNING` if `os.chmod(..., 0o600)` raises `OSError`, and atomically replaces the target file.

### 3. [`src/gphotos_synology/database.py`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py)
- **G3 (`delete_state`)**: [`StateDB.delete_state()`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L342-L344) deletes one-time state keys cleanly.
- **Additive Idempotent Schema & Migrations**:
  - [`StateDB._init_schema()`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L230-L321) idempotently adds `media_items.visual_status`, `quality_upgrades.{old_rel_path,old_taken_at,old_latitude,old_longitude}`, and `media_dhash_buckets (...) WITHOUT ROWID` with `ON DELETE CASCADE` and `idx_dhash_buckets_media_id`.
  - Normalizes uppercase `dhash` values to lowercase, backfills `visual_status = 'ok'` for already-fingerprinted legacy rows (while leaving empty-visual pre-v1.2.0 HEIC rows as `''` for one-time re-indexing), and backfills `media_dhash_buckets`.
- **G11 SQL dHash Bucketing**:
  - [`_DHASH_BUCKET_CANDIDATES_SQL`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L21-L43) performs 8 `PRIMARY KEY` index lookups across 16-bit prefix blocks when `perceptual_max_distance < 8` and `len(dhash) == 32` (plus legacy non-32-char fallback when `_has_legacy_dhash` is true), and falls back to full scan when `perceptual_max_distance >= 8` for 100% recall.
  - Bucket rows stay synchronized across `record_media_item`, `upsert_existing_nas_media`, `upgrade_media_item`, `delete_media_item`, and `prune_missing_existing_nas_media`.
- **G2 Quality Upgrade Journal API**:
  - Implemented [`create_pending_quality_upgrade()`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L1072-L1133), [`finalize_quality_upgrade()`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L1135-L1161), [`delete_quality_upgrade()`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L1163-L1165), [`list_in_progress_quality_upgrades()`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L1167-L1172), [`list_quarantined_upgrades_batch()`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L1174-L1180), and backwards-compatible `old_*` parameters on [`record_quality_upgrade()`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L1010-L1070).
- **Hash Ordering, Archive Retry Reset, Album Retries, Log Pruning, Scanner Undecodable State & Stats**:
  - `ORDER BY CASE WHEN original_sha256 = ? THEN 0 ELSE 1 END, id ASC LIMIT 1` in `get_media_by_original_hash`, `find_duplicate_media` Tier 1, and `upsert_existing_nas_media`.
  - [`upsert_archive()`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L391-L463) resets counters, `error_message`, and `completed_at` when retrying non-completed archives while leaving completed archives intact.
  - [`mark_album_membership()`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L1277-L1319) defaults `increment_retry` to `status == "error"`, preventing `'pending'` Synology indexing waits from burning retries.
  - [`log_event()`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L349-L370) prunes every 50 inserts.
  - [`get_indexed_files_by_rel_path()`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L687-L714) exposes `visual_status` and `visual_done` (`has_visual or visual_status in ("ok", "undecodable")`).
  - [`get_stats()`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L1343-L1403) includes `completed_with_errors_archives` and excludes transient `in_progress` upgrade rows from `quality_upgrades_count`.

### 4. [`tests/test_config.py`](file:///home/antigravity/gphotos-to-synology/tests/test_config.py) & [`tests/test_database.py`](file:///home/antigravity/gphotos-to-synology/tests/test_database.py)
- Comprehensive unit test coverage across all new and modified behaviors, edge cases, query plans, and regressions.

---

## Findings
No blocking or non-blocking defects found in Milestone M1.

Verdict: APPROVE

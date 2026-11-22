# M1 Implementation Worker (Iteration 1b) Handoff

## Summary
Completed all **Milestone M1 — Foundation (Dependencies, Version, Config & Database)** implementation requirements strictly within the 7 exclusively owned files. All 20 existing unit tests and 36 new M1 unit tests (56 total) pass with 0 warnings/errors from `ruff` on `src/` and unit `tests/`, `uv lock --check` and `uv sync --locked --extra dev --check` pass, and package versions verify cleanly as `1.2.0 12.2.0 1.0.0`.

---

## Files Modified / Created (Exclusive Ownership — 7/7)

1. [`pyproject.toml`](file:///home/antigravity/gphotos-to-synology/pyproject.toml)
   - Bumped `version = "1.2.0"`.
   - Tightened Pillow to `"pillow>=11.1.0,<12.3.0"` (locks `12.2.0`, glibc-2.17 `manylinux2014` wheel ceiling) and added `"pillow-heif>=1.0.0,<1.1.0"` (locks `1.0.0`, glibc-2.17 `manylinux2014` wheel ceiling).
   - Added `spk = ["pyinstaller>=6.10.0,<7.0.0"]` under `[project.optional-dependencies]`.
2. [`uv.lock`](file:///home/antigravity/gphotos-to-synology/uv.lock)
   - Generated via `uv lock` and synced `.venv` via `uv sync --locked --extra dev` (preserving `pytest`, `pytest-asyncio`, and `ruff`).
3. [`src/gphotos_synology/__init__.py`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/__init__.py)
   - Bumped `__version__ = "1.2.0"`.
4. [`src/gphotos_synology/config.py`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py)
   - Enabled `populate_by_name=True` in `SettingsConfigDict`.
   - **G5 secure defaults**: `DEFAULT_SYNOLOGY_URL = "https://127.0.0.1:5001"`, `LEGACY_DEFAULT_SYNOLOGY_URL = "http://172.17.0.1:5000"`, `app_host = "127.0.0.1"`, `synology_url = DEFAULT_SYNOLOGY_URL`, `synology_verify_ssl = True`, `synology_ca_cert = ""`, `quarantine_dir_override` with `AliasChoices("quarantine_dir", "quarantine_dir_override")` and blank-string-to-`None` validator; preserved `perceptual_max_distance = 10` in M1.
   - **C1/C2/G6 config fields & helpers**: `proxy_secret`, `proxy_secret_file` (with blank-string-to-`None` validator), `api_token`, `dsm_admin_users`, `trust_proxy_headers`, `allow_insecure_no_auth`, `resolve_proxy_secret()` (fail-closed with `WARNING` on `OSError`), `dsm_admin_user_set()`. Added `"proxy_secret"` and `"api_token"` to `SECRET_FIELDS`; kept all security/path/update-infra fields out of `MUTABLE_UI_FIELDS`.
   - **G4 URL validation & credential clearing**: `validate_synology_url()` and `_normalize_url_for_compare()`, `@field_validator("synology_url")` for non-empty values, pre-mutation validation in `ConfigManager.update()`, clearing `existing["synology_password"] = ""` (overriding even env-var passwords) + `WARNING` log + `last_update_warnings` when normalized URL changes without a fresh non-empty, non-`"********"` password.
   - **Lazy OAuth & URL inheritance**: thread-safe `ConfigManager(base_settings=None, fallback_settings_provider=None)` (accepts positional or keyword) with dynamic `.settings` property, `to_public_dict()` reading `self.settings`, full-form UI save non-freezing, and local `synology_url` pinning whenever a profile saves a non-empty `synology_password`.
   - **Resilience & atomic persistence**: `MUTABLE_UI_FIELDS` filtering on disk load in both `_load_merged()` and `update()`, corrupt `runtime_settings.json` backup to `runtime_settings.json.corrupt.<UTC_timestamp>` (with collision suffix) + `ERROR` log + base fallback, and atomic writes via `os.open(..., 0o600)` + `flush()` + `os.fsync(fd)` + `os.chmod(..., 0o600)` (logging `WARNING` on `OSError`) + `os.replace()`.
5. [`src/gphotos_synology/database.py`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py)
   - Added `delete_state(key)` (G3).
   - Additive idempotent schema & migrations: `media_items.visual_status` (backfilling `'ok'` on already-fingerprinted rows while leaving pre-v1.2.0 HEIC empty-visual rows as `''`), `media_dhash_buckets` (`WITHOUT ROWID` + `ON DELETE CASCADE` + `idx_dhash_buckets_media_id`), uppercase `dhash` normalization + idempotent bucket backfill + `self._has_legacy_dhash`, and `quality_upgrades` columns `old_rel_path`, `old_taken_at`, `old_latitude`, `old_longitude`.
   - Synchronized `media_dhash_buckets` across all mutation paths (`record_media_item`, `upsert_existing_nas_media`, `upgrade_media_item`, cascade on `prune_missing_existing_nas_media` / `delete_media_item`).
   - Implemented Tier 3 8-way `UNION` bucket candidate query (`_DHASH_BUCKET_CANDIDATES_SQL`, verified via `EXPLAIN QUERY PLAN` to perform 8 `PRIMARY KEY` searches) for 32-hex `dhash` when `perceptual_max_distance < 8` (+ legacy non-32-char check when `_has_legacy_dhash`), with full-scan fallback when `perceptual_max_distance >= 8` or non-32-hex `dhash`.
   - Deterministic `ORDER BY CASE WHEN original_sha256 = ? THEN 0 ELSE 1 END, id ASC LIMIT 1` in `get_media_by_original_hash`, `find_duplicate_media` Tier 1, and `upsert_existing_nas_media` Branch B; deterministic `ORDER BY id ASC` on Tier 2 and Tier 3 exact matches.
   - Added `visual_status` support on `record_media_item`, `upsert_existing_nas_media` (preventing `'skipped'` from clobbering `'ok'`), `upgrade_media_item` (plus `_UNSET`-defaulted `latitude`/`longitude`), `get_indexed_files_by_rel_path()` (`visual_status` + `visual_done`), `get_media_by_id()`, and `delete_media_item()`.
   - Added G2 `quality_upgrades` journal API: `create_pending_quality_upgrade`, `finalize_quality_upgrade`, `delete_quality_upgrade`, `list_in_progress_quality_upgrades`, `list_quarantined_upgrades_batch`, `list_quality_upgrades(..., include_in_progress=False)`, and optional `old_*` args on `record_quality_upgrade`.
   - Updated `upsert_archive` to reset counters, `error_message`, and `completed_at` on retry for non-completed archives (`status != 'completed'`) while preserving completed archives.
   - Fixed `mark_album_membership(..., increment_retry: bool | None = None)` so `'pending'` waits never burn retries, and revived `'error'` rows on `add_media_to_album`.
   - Updated `log_event` to prune every 50 inserts using the primary-key `OFFSET` query.
   - Updated `get_stats()` to preserve all 14 existing keys, add `"completed_with_errors_archives"`, and exclude `'in_progress'` rows from `quality_upgrades_count`.
6. [`tests/test_config.py`](file:///home/antigravity/gphotos-to-synology/tests/test_config.py)
   - Added 26 test cases covering secure defaults, env aliases, blank-path coercion, `model_validate(model_dump())` roundtrips, `resolve_proxy_secret`, `dsm_admin_user_set`, `MUTABLE_UI_FIELDS` & `SECRET_FIELDS`, malformed `synology_url` rejection (15 parametrized cases), G4 credential clearing (including env-var password persistence across reload), lazy OAuth/URL inheritance & password pinning, corrupt JSON backup (4 parametrized failure modes), and `0o600` atomic writes + `os.chmod` warning.
7. [`tests/test_database.py`](file:///home/antigravity/gphotos-to-synology/tests/test_database.py)
   - Added 10 comprehensive test suites covering genuine v1.1.0 → v1.2.0 additive migration + idempotency, `app_state` CRUD + `delete_state`, deterministic `original_sha256` hash ordering, `EXPLAIN QUERY PLAN` proof of 8× `PRIMARY KEY` `media_dhash_buckets` search, bucket sync/recall at Hamming distances 0/1/7/8/10 + upgrade/prune/delete cascades + legacy 16-char hash recall, `visual_status` & `visual_done`, `quality_upgrades` journal lifecycle + >500-row batch pagination, `upsert_archive` retry reset & `completed_with_errors_archives`, album membership retry counting & revival, and 50-insert periodic log pruning.

---

## Verification Results (Exact Commands & Verbatim Outputs)

> **Note on parallel E2E track (`tests/e2e/`):** While M1 was running, the parallel E2E track created files under `tests/e2e/` (not owned by M1). Bare `.venv/bin/pytest -v` fails collection inside `tests/e2e/test_tier1_features.py` (`ModuleNotFoundError: No module named 'tests'`), and bare `.venv/bin/ruff check src tests` reports 21 lint issues exclusively inside `tests/e2e/`. Both commands are reported below alongside the scoped runs excluding `tests/e2e/` (which is outside M1 file ownership).

### 1. `uv lock --check && uv sync --locked --extra dev --check && .venv/bin/python -c "import gphotos_synology, PIL, pillow_heif; print(gphotos_synology.__version__, PIL.__version__, pillow_heif.__version__)"`
Exit code: `0`
```text
Resolved 65 packages in 1ms
Would use project environment at: .venv
Resolved 65 packages in 1ms
Checked 57 packages in 2ms
Would make no changes
1.2.0 12.2.0 1.0.0
```

### 2. `.venv/bin/ruff check src tests --exclude tests/e2e`
Exit code: `0`
```text
All checks passed!
```

### 3. `.venv/bin/pytest -v --ignore=tests/e2e`
Exit code: `0`
```text
============================= test session starts ==============================
platform linux -- Python 3.11.2, pytest-9.2.0, pluggy-1.7.0 -- /home/antigravity/gphotos-to-synology/.venv/bin/python
cachedir: .pytest_cache
rootdir: /home/antigravity/gphotos-to-synology
configfile: pyproject.toml
testpaths: tests
plugins: anyio-4.16.0, asyncio-1.5.0
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=function, asyncio_default_test_loop_scope=function
collecting ... collected 56 items

tests/test_api.py::test_dashboard_health_status_and_settings_roundtrip PASSED [  1%]
tests/test_config.py::test_secure_defaults_g5_and_c1 PASSED              [  3%]
tests/test_config.py::test_env_aliases_blank_paths_roundtrip_and_helpers PASSED [  5%]
tests/test_config.py::test_mutable_allowlist_and_secret_masking PASSED   [  7%]
tests/test_config.py::test_synology_url_validation_rejects_malformed_values[] PASSED [  8%]
tests/test_config.py::test_synology_url_validation_rejects_malformed_values[None] PASSED [ 10%]
tests/test_config.py::test_synology_url_validation_rejects_malformed_values[123] PASSED [ 12%]
tests/test_config.py::test_synology_url_validation_rejects_malformed_values[ftp://nas.local:5000] PASSED [ 14%]
tests/test_config.py::test_synology_url_validation_rejects_malformed_values[javascript:alert(1)] PASSED [ 16%]
tests/test_config.py::test_synology_url_validation_rejects_malformed_values[file:///etc/passwd] PASSED [ 17%]
tests/test_config.py::test_synology_url_validation_rejects_malformed_values[https://] PASSED [ 19%]
tests/test_config.py::test_synology_url_validation_rejects_malformed_values[https:///path] PASSED [ 21%]
tests/test_config.py::test_synology_url_validation_rejects_malformed_values[http://:5000] PASSED [ 23%]
tests/test_config.py::test_synology_url_validation_rejects_malformed_values[https://nas.local:99999] PASSED [ 25%]
tests/test_config.py::test_synology_url_validation_rejects_malformed_values[https://nas.local:notaport] PASSED [ 26%]
tests/test_config.py::test_synology_url_validation_rejects_malformed_values[https://nas.local:5001 extra] PASSED [ 28%]
tests/test_config.py::test_synology_url_validation_rejects_malformed_values[ https://nas.local:5001] PASSED [ 30%]
tests/test_config.py::test_synology_url_validation_rejects_malformed_values[https://nas.local:5001\r\nX-Injected: 1] PASSED [ 32%]
tests/test_config.py::test_synology_url_validation_rejects_malformed_values[https://nas.local:5001\x00] PASSED [ 33%]
tests/test_config.py::test_settings_allows_empty_synology_url_but_rejects_invalid_non_empty PASSED [ 35%]
tests/test_config.py::test_g4_synology_url_change_clears_password_including_env_var PASSED [ 37%]
tests/test_config.py::test_lazy_oauth_and_url_inheritance_and_password_pinning PASSED [ 39%]
tests/test_config.py::test_corrupt_runtime_settings_backed_up_and_fallback_to_base[{"poll_interval_minutes": ] PASSED [ 41%]
tests/test_config.py::test_corrupt_runtime_settings_backed_up_and_fallback_to_base[\xff\xfe\x00\x80invalid-utf8] PASSED [ 42%]
tests/test_config.py::test_corrupt_runtime_settings_backed_up_and_fallback_to_base[[1, 2, 3]] PASSED [ 44%]
tests/test_config.py::test_corrupt_runtime_settings_backed_up_and_fallback_to_base[{"poll_interval_minutes": -5}] PASSED [ 46%]
tests/test_config.py::test_atomic_write_permissions_and_chmod_warning PASSED [ 48%]
tests/test_database.py::test_v110_to_v120_additive_migration_and_idempotency PASSED [ 50%]
tests/test_database.py::test_app_state_crud_and_delete_state PASSED      [ 51%]
tests/test_database.py::test_deterministic_hash_lookup_prefers_original_sha256 PASSED [ 53%]
tests/test_database.py::test_dhash_bucket_explain_query_plan_uses_primary_key PASSED [ 55%]
tests/test_database.py::test_dhash_buckets_recall_updates_cascades_and_legacy_fallback PASSED [ 57%]
tests/test_database.py::test_visual_status_transitions_and_indexed_files_map PASSED [ 58%]
tests/test_database.py::test_quality_upgrades_journal_lifecycle_and_batch_pagination PASSED [ 60%]
tests/test_database.py::test_upsert_archive_retry_reset_and_completed_with_errors_stats PASSED [ 62%]
tests/test_database.py::test_album_membership_retry_counting_and_error_revival PASSED [ 64%]
tests/test_database.py::test_log_event_periodic_pruning PASSED           [ 66%]
tests/test_existing_nas_dedup.py::test_existing_nas_scanner_skips_synology_system_dirs_and_is_incremental PASSED [ 67%]
tests/test_existing_nas_dedup.py::test_dedup_against_existing_nas_exact_and_exif_header_changed PASSED [ 69%]
tests/test_existing_nas_dedup.py::test_perceptual_dedup_catches_storage_saver_recompression_without_false_positives PASSED [ 71%]
tests/test_metadata_and_importer.py::test_parse_sidecar_json_and_zero_gps_handling PASSED [ 73%]
tests/test_metadata_and_importer.py::test_apply_metadata_mtime_fallback_and_folder_importer PASSED [ 75%]
tests/test_metadata_and_importer.py::test_apply_metadata_piexif_jpeg_fallback_without_exiftool PASSED [ 76%]
tests/test_pipeline_e2e.py::test_full_pipeline_dedup_and_synology_album_sync PASSED [ 78%]
tests/test_profiles.py::test_multi_user_discovery_profile_isolation_and_dsm_auto_select PASSED [ 80%]
tests/test_synology_api_and_security.py::test_extract_archive_blocks_zipslip_path_traversal PASSED [ 82%]
tests/test_synology_api_and_security.py::test_synology_photos_client_login_resolve_and_album_add PASSED [ 83%]
tests/test_takeout_matcher.py::test_standard_and_supplemental_metadata_matches PASSED [ 85%]
tests/test_takeout_matcher.py::test_numbered_duplicate_and_edited_matches PASSED [ 87%]
tests/test_takeout_matcher.py::test_live_photo_and_46_char_truncation_and_title_fallback PASSED [ 89%]
tests/test_takeout_matcher.py::test_detect_album_name PASSED             [ 91%]
tests/test_updater.py::test_update_checker_detects_new_commit_and_up_to_date PASSED [ 92%]
tests/test_updater.py::test_watchtower_self_update_trigger PASSED        [ 94%]
tests/test_visual_quality_upgrade.py::test_higher_resolution_takeout_upgrades_lower_res_nas_and_quarantines_old PASSED [ 96%]
tests/test_visual_quality_upgrade.py::test_sharper_image_upgrades_blurry_copy_at_same_resolution_and_never_downgrades PASSED [ 98%]
tests/test_visual_quality_upgrade.py::test_upgrades_api_preview_restore_and_purge PASSED [100%]

======================== 56 passed, 1 warning in 2.02s =========================
```

### 4. `git status --short`
Exit code: `0`
```text
 M pyproject.toml
 M src/gphotos_synology/__init__.py
 M src/gphotos_synology/config.py
 M src/gphotos_synology/database.py
?? .agents/
?? PROJECT.md
?? tests/e2e/
?? tests/test_config.py
?? tests/test_database.py
?? uv.lock
```

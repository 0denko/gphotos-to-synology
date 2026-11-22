# Reviewer 2 (`reviewer_m1_r1_2_gen2`) — Milestone M1 Contract Compliance Review

## Executive Summary
Independently executed the M1 test suite, linter, and dependency lock checks, and performed a line-by-line interface contract and downstream caller compliance audit of all 7 files in Milestone M1 (`pyproject.toml`, `uv.lock`, `src/gphotos_synology/__init__.py`, `src/gphotos_synology/config.py`, `src/gphotos_synology/database.py`, `tests/test_config.py`, `tests/test_database.py`) against `ORIGINAL_REQUEST.md`, `PROJECT.md § Interface Contracts` (Contracts 1, 2, 3, and 6), `Collab.md`, and `sub_orch_m1/DISPATCH.md`.

All 36 M1 unit tests in `tests/test_config.py` and `tests/test_database.py` (and all 56 unit tests across the repository) pass, `ruff check` passes with 0 warnings/errors, `uv lock --check` passes with `pillow==12.2.0` and `pillow-heif==1.0.0` (`manylinux2014` wheels present for both `x86_64` and `aarch64`), and every contract signature, default, SQL column/table/index, and return structure is 100% compliant and backwards-compatible with all M2–M5 callers.

---

## Independent Verification Commands Executed

### 1. Target-Scoped Unit Tests (`.venv/bin/pytest -v tests/test_config.py tests/test_database.py`)
- **Exit Code:** `0` (`36 passed in 1.32s`)
```text
tests/test_config.py::test_secure_defaults_g5_and_c1 PASSED              [  2%]
tests/test_config.py::test_env_aliases_blank_paths_roundtrip_and_helpers PASSED [  5%]
tests/test_config.py::test_mutable_allowlist_and_secret_masking PASSED   [  8%]
tests/test_config.py::test_synology_url_validation_rejects_malformed_values[...] PASSED (15 cases)
tests/test_config.py::test_settings_allows_empty_synology_url_but_rejects_invalid_non_empty PASSED [ 52%]
tests/test_config.py::test_g4_synology_url_change_clears_password_including_env_var PASSED [ 55%]
tests/test_config.py::test_lazy_oauth_and_url_inheritance_and_password_pinning PASSED [ 58%]
tests/test_config.py::test_corrupt_runtime_settings_backed_up_and_fallback_to_base[...] PASSED (4 cases)
tests/test_config.py::test_atomic_write_permissions_and_chmod_warning PASSED [ 72%]
tests/test_database.py::test_v110_to_v120_additive_migration_and_idempotency PASSED [ 75%]
tests/test_database.py::test_app_state_crud_and_delete_state PASSED      [ 77%]
tests/test_database.py::test_deterministic_hash_lookup_prefers_original_sha256 PASSED [ 80%]
tests/test_database.py::test_dhash_bucket_explain_query_plan_uses_primary_key PASSED [ 83%]
tests/test_database.py::test_dhash_buckets_recall_updates_cascades_and_legacy_fallback PASSED [ 86%]
tests/test_database.py::test_visual_status_transitions_and_indexed_files_map PASSED [ 88%]
tests/test_database.py::test_quality_upgrades_journal_lifecycle_and_batch_pagination PASSED [ 91%]
tests/test_database.py::test_upsert_archive_retry_reset_and_completed_with_errors_stats PASSED [ 94%]
tests/test_database.py::test_album_membership_retry_counting_and_error_revival PASSED [ 97%]
tests/test_database.py::test_log_event_periodic_pruning PASSED           [100%]
```

### 2. Target-Scoped Ruff Linter (`.venv/bin/ruff check src/gphotos_synology/__init__.py src/gphotos_synology/config.py src/gphotos_synology/database.py tests/test_config.py tests/test_database.py`)
- **Exit Code:** `0` (`All checks passed!`)

### 3. Full Unit Regression Suite & Lockfile Verification (`uv lock --check && .venv/bin/pytest -v --ignore=tests/e2e`)
- **Exit Code:** `0` (`56 passed in 2.13s`, runtime package versions verified as `gphotos_synology=1.2.0`, `PIL=12.2.0`, `pillow_heif=1.0.0`).

---

## Line-by-Line Interface Contract Compliance Audit

### Contract 1: Proxy / CGI ↔ FastAPI Security (`config.py` portion)
- **`proxy_secret` & `proxy_secret_file`**:
  - [`Settings.proxy_secret`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L85-L88) defaults to `""` with `validation_alias=AliasChoices("gphotos_proxy_secret", "proxy_secret")`.
  - [`Settings.proxy_secret_file`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L89-L92) defaults to `None` with `validation_alias=AliasChoices("proxy_secret_file", "gphotos_proxy_secret_file")` and coerces blank strings to `None` via [`_blank_path_to_none`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L165-L170).
  - [`Settings.resolve_proxy_secret()`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L198-L213) returns stripped `proxy_secret` first, falling back to `proxy_secret_file.read_text(encoding="utf-8").strip()` and failing closed (`""`) with a `WARNING` log on `OSError`.
- **Direct / Docker Auth & DSM Admin Config Fields**:
  - [`Settings.api_token`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L93-L96) (`AliasChoices("api_token", "app_api_token")`), [`Settings.dsm_admin_users`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L97) (with helper [`Settings.dsm_admin_user_set()`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L215-L221)), [`Settings.trust_proxy_headers`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L98) (`False`), and [`Settings.allow_insecure_no_auth`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L99) (`False`) are all present and properly typed.

### Contract 2: `ConfigManager` & `Settings` (`config.py` ↔ all modules)
- **G5 Defaults & Quarantine Dir Override**:
  - [`Settings.app_host`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L81) = `"127.0.0.1"`.
  - [`Settings.synology_url`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L135) = `"https://127.0.0.1:5001"` (`DEFAULT_SYNOLOGY_URL`).
  - [`Settings.synology_verify_ssl`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L139) = `True`.
  - [`Settings.synology_ca_cert`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L140) = `""`.
  - [`Settings.quarantine_dir_override`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L75-L78) (`Path | None = None`, `AliasChoices("quarantine_dir", "quarantine_dir_override")`), and [`Settings.quarantine_dir`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L191-L196) returns `quarantine_dir_override` when set, else `work_dir / "replaced_duplicates"`.
  - `populate_by_name=True` in [`SettingsConfigDict`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L62-L67) ensures `Settings.model_validate(base.model_dump())` round-trips aliased fields without re-reading environment variables.
- **Lazy OAuth & URL Inheritance**:
  - [`ConfigManager.__init__`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L366-L377) accepts `(base_settings: Settings | None = None, fallback_settings_provider: Callable[[], Settings] | None = None)` positionally or by keyword.
  - [`ConfigManager.settings`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L380-L408) dynamically inherits unset `google_client_id`, `google_client_secret`, and unoverridden default/empty `synology_url` (when no local `synology_password` is stored) from `fallback_settings_provider()`.
  - [`ConfigManager.update`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L486-L522) prevents full-form UI saves from freezing inherited `google_client_id`/`synology_url`, while pinning `synology_url` locally whenever a profile stores a non-empty `synology_password`.
- **G4 URL Validation & Credential Exfiltration Prevention**:
  - [`validate_synology_url`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L27-L42) rejects non-strings, empty strings, whitespace/control characters, non-`http`/`https` schemes, missing hostnames, and invalid ports with `ValueError`.
  - [`ConfigManager.update`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L449-L552) validates `synology_url` prior to mutation, clears `existing["synology_password"] = ""` (overriding both runtime and env-provided passwords) whenever the normalized URL changes without a fresh non-empty, non-`"********"` password, logs a `WARNING`, and populates `self.last_update_warnings`.
- **`SECRET_FIELDS`, `MUTABLE_UI_FIELDS`, & Config Resilience**:
  - [`SECRET_FIELDS`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L285-L292) contains `google_client_secret`, `synology_password`, `smtp_password`, `watchtower_token`, `proxy_secret`, and `api_token`.
  - [`MUTABLE_UI_FIELDS`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L247-L283) excludes `proxy_secret`, `proxy_secret_file`, `api_token`, `dsm_admin_users`, `trust_proxy_headers`, `allow_insecure_no_auth`, `photos_dir`, `watchtower_url`, `watchtower_token`, `synology_ca_cert`, and `quarantine_dir_override`.
  - [`_backup_corrupt_file`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L295-L327) backs up corrupt/invalid `runtime_settings.json` to `runtime_settings.json.corrupt.<UTC_timestamp>` and logs `ERROR`.
  - [`_write_overrides_atomic`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L330-L360) creates the temp file with `os.open(..., 0o600)`, `fsync`s, logs a `WARNING` if `os.chmod(..., 0o600)` raises `OSError`, and atomically replaces the target file.

### Contract 3: `StateDB` (`database.py` ↔ `routes.py`, `pipeline.py`, `scanner.py`, `dedup.py`)
- **`delete_state(key: str) -> None`**: Implemented at [`StateDB.delete_state`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L342-L344).
- **`media_items.visual_status` (G10)**:
  - Column `visual_status TEXT NOT NULL DEFAULT ''` added in [`SCHEMA_SQL`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L93) and idempotently migrated in [`_init_schema`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L244), backfilling `'ok'` on already-fingerprinted rows while leaving empty-visual rows (e.g., pre-v1.2.0 HEIC files) at `''` so they are re-indexed once.
  - Accepted with backwards-compatible defaults in [`upsert_existing_nas_media(..., visual_status: str = "")`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L716-L732), [`record_media_item(..., visual_status: str = "")`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L850-L871), and [`upgrade_media_item(..., visual_status: str | None = None)`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L912-L933).
  - [`get_indexed_files_by_rel_path()`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L687-L714) returns `"has_visual"`, `"visual_status"`, and `"visual_done": has_visual or visual_status in ("ok", "undecodable")`.
- **`media_dhash_buckets` SQL Bucketing (G11)**:
  - Declared in [`SCHEMA_SQL`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L113-L120) as `WITHOUT ROWID` with `PRIMARY KEY (bucket_idx, bucket_val, media_id)` and `REFERENCES media_items(id) ON DELETE CASCADE` plus `idx_dhash_buckets_media_id`.
  - Maintained via [`_sync_dhash_buckets`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L218-L228) on insert, NAS upsert, quality upgrade, and cascade delete/prune, and backfilled idempotently in [`_init_schema`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L294-L320).
  - Queried in [`find_duplicate_media`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L616-L684) Tier 3 when `perceptual_max_distance < 8` and `_is_valid_128bit_dhash(dhash_norm)` (using 8 indexed PK lookups via `_DHASH_BUCKET_CANDIDATES_SQL`, plus legacy non-32-char fallback when `_has_legacy_dhash` is True), and falls back to full scan when `perceptual_max_distance >= 8` or non-32-hex hash for 100% recall.
- **`quality_upgrades` Schema & Journal API (G2 & Batched Purge)**:
  - Columns `old_rel_path TEXT NOT NULL DEFAULT ''`, `old_taken_at TEXT NOT NULL DEFAULT ''`, `old_latitude REAL`, `old_longitude REAL` added in [`SCHEMA_SQL`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L126-L129) and migrated in [`_init_schema`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L256-L264).
  - All 5 journal methods implemented: [`create_pending_quality_upgrade`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L1072-L1133), [`finalize_quality_upgrade`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L1135-L1161), [`delete_quality_upgrade`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L1163-L1165), [`list_in_progress_quality_upgrades`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L1167-L1172), and [`list_quarantined_upgrades_batch(limit: int = 500)`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L1174-L1180), plus optional `old_*` kwargs on [`record_quality_upgrade`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L1010-L1032).
- **Additional M1 Database Contracts**:
  - [`mark_album_membership`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L1277-L1319) defaults `increment_retry` to `(status == "error")` so `'pending'` waits never burn retries.
  - Deterministic `ORDER BY CASE WHEN original_sha256 = ? THEN 0 ELSE 1 END, id ASC LIMIT 1` in [`get_media_by_original_hash`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L517-L528), [`find_duplicate_media`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L570-L578), and [`upsert_existing_nas_media`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L789-L799).
  - [`upsert_archive`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L391-L441) resets counters, `error_message`, and `completed_at` on retry when `status != 'completed'`.
  - [`log_event`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L349-L370) prunes `activity_logs` periodically every 50 inserts.
  - [`get_stats()`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L1343-L1403) preserves all 14 existing keys, adds `"completed_with_errors_archives"`, and excludes `'in_progress'` rows from `quality_upgrades_count`.

### Contract 6: Packaging & Dependency Pinning (`pyproject.toml`, `uv.lock`, `__init__.py`)
- [`pyproject.toml`](file:///home/antigravity/gphotos-to-synology/pyproject.toml#L7-L36) sets `version = "1.2.0"`, `"pillow>=11.1.0,<12.3.0"`, `"pillow-heif>=1.0.0,<1.1.0"`, and `spk = ["pyinstaller>=6.10.0,<7.0.0"]`.
- [`src/gphotos_synology/__init__.py`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/__init__.py#L3) sets `__version__ = "1.2.0"`.
- [`uv.lock`](file:///home/antigravity/gphotos-to-synology/uv.lock) locks `pillow==12.2.0` and `pillow-heif==1.0.0` with `manylinux2014` (`manylinux_2_17`) wheels for both `x86_64` and `aarch64`.

---

## Verdict

Verdict: APPROVE

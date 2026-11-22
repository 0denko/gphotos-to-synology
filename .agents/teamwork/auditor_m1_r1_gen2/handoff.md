# Forensic Audit Report — Milestone M1 (`auditor_m1_r1_gen2`)

- **Audit Type:** **intermediate milestone audit**
- **Milestone:** M1 — Foundation (Dependencies, Version, Config & Database)
- **Owned Sub-Scope Paths:**
  - [`pyproject.toml`](file:///home/antigravity/gphotos-to-synology/pyproject.toml)
  - [`uv.lock`](file:///home/antigravity/gphotos-to-synology/uv.lock)
  - [`src/gphotos_synology/__init__.py`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/__init__.py)
  - [`src/gphotos_synology/config.py`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py)
  - [`src/gphotos_synology/database.py`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py)
  - [`tests/test_config.py`](file:///home/antigravity/gphotos-to-synology/tests/test_config.py)
  - [`tests/test_database.py`](file:///home/antigravity/gphotos-to-synology/tests/test_database.py)

---

## 1. Independent Execution of Canonical Test, Lint & Lock Commands

All commands were executed independently in `/home/antigravity/gphotos-to-synology` (never relying on Worker logs):

1. **M1 Unit Test Suite (`.venv/bin/pytest -v tests/test_config.py tests/test_database.py`):**
   - Exit code: `0` (`36 passed in 1.22s` — 26 tests in `test_config.py`, 10 test suites in `test_database.py`).
2. **Full Unit & Integration Test Suite excluding parallel E2E track (`.venv/bin/pytest -v --ignore=tests/e2e`):**
   - Exit code: `0` (`56 passed, 1 warning in 2.15s` — all 20 pre-existing unit/integration tests and all 36 new M1 tests pass).
3. **Ruff Linter (`.venv/bin/ruff check src tests --exclude tests/e2e`):**
   - Exit code: `0` (`All checks passed!`).
4. **Lockfile & Environment Verification (`uv lock --check && uv sync --locked --extra dev --check`):**
   - Exit code: `0` (`gphotos_synology.__version__ == "1.2.0"`, `PIL.__version__ == "12.2.0"`, `pillow_heif.__version__ == "1.0.0"`).

---

## 2. File Ownership & Working-Tree Integrity

- `git status --short` and `git diff --stat` confirm that within tracked source and unit test files, **only** the 7 M1-owned paths were modified or added (`pyproject.toml`, `uv.lock`, `src/gphotos_synology/__init__.py`, `src/gphotos_synology/config.py`, `src/gphotos_synology/database.py`, `tests/test_config.py`, `tests/test_database.py`).
- None of the 9 pre-existing test files in `tests/` were altered, weakened, or touched.
- Untracked paths outside M1 (`PROJECT.md`, `.agents/`, `tests/e2e/`) belong to the parent orchestrator and parallel E2E track as expected.

---

## 3. Anti-Cheating & Implementation Authenticity Audit

1. **No Test-Environment Sniffing, Skips, or Facades:**
   - Verified zero occurrences of `PYTEST_CURRENT_TEST`, `_getframe`, `pytest.skip`, `pytest.xfail`, `@pytest.mark.skip`, or `@pytest.mark.xfail` across `src/gphotos_synology/config.py`, `src/gphotos_synology/database.py`, `tests/test_config.py`, and `tests/test_database.py`.
2. **Dependencies & `manylinux2014` Wheel Integrity (`pyproject.toml`, `uv.lock`, `__init__.py`):**
   - Independently parsed `uv.lock` with `tomllib` to verify that `gphotos-to-synology==1.2.0`, `pillow==12.2.0`, `pillow-heif==1.0.0`, and `pyinstaller` are locked, and that both `pillow==12.2.0` and `pillow-heif==1.0.0` include `manylinux2014` / `manylinux_2_17` wheels for both `x86_64` and `aarch64`.
3. **Genuine `ConfigManager` & `Settings` Implementation (`src/gphotos_synology/config.py`):**
   - **G5 & C1/C2/G6 Fields:** `app_host="127.0.0.1"`, `synology_url="https://127.0.0.1:5001"`, `synology_verify_ssl=True`, `synology_ca_cert=""`, `quarantine_dir_override` with `AliasChoices("quarantine_dir", "quarantine_dir_override")`, `proxy_secret`, `proxy_secret_file`, `api_token`, `dsm_admin_users`, `trust_proxy_headers=False`, `allow_insecure_no_auth=False`, `resolve_proxy_secret()`, and `dsm_admin_user_set()` are genuinely implemented.
   - **Allowlist & Secret Hygiene:** Security, path, and Watchtower fields are excluded from `MUTABLE_UI_FIELDS`; `proxy_secret` and `api_token` are in `SECRET_FIELDS`.
   - **G4 Credential Exfiltration Prevention:** [`validate_synology_url`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L27-L42) validates scheme (`http`/`https`), hostname, port, and rejects whitespace/control chars prior to state mutation. [`ConfigManager.update`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L449-L552) compares normalized URLs and clears `existing["synology_password"] = ""` (overriding even base/env-var passwords across reloads), logs a `WARNING`, and populates `last_update_warnings` when the URL changes without a fresh non-empty, non-`"********"` password.
   - **Lazy OAuth/URL Inheritance & Password Pinning:** [`ConfigManager.settings`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L380-L408) dynamically inherits unset `google_client_id`, `google_client_secret`, and default/unset `synology_url` from `fallback_settings_provider()`, avoids freezing inherited values on full-form saves, and pins `synology_url` locally whenever a profile stores a non-empty `synology_password`.
   - **Resilience & Atomic `0o600` Writes:** [`_backup_corrupt_file`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L295-L327) backs up corrupt/invalid `runtime_settings.json` to `runtime_settings.json.corrupt.<UTC_timestamp>` and logs `ERROR`. [`_write_overrides_atomic`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L330-L360) opens the temp file with `os.open(..., 0o600)`, flushes, `os.fsync`s, enforces `os.chmod(..., 0o600)` (logging `WARNING` on `OSError`), and atomically replaces the target file.
4. **Genuine `StateDB` Implementation (`src/gphotos_synology/database.py`):**
   - **G3:** [`delete_state`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L342-L344) deletes keys from `app_state`.
   - **Additive Migrations & `WITHOUT ROWID` dHash Buckets (G10/G11):** [`_init_schema`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L230-L320) idempotently adds `media_items.visual_status` and `quality_upgrades.old_rel_path`/`old_taken_at`/`old_latitude`/`old_longitude`, creates `media_dhash_buckets (...) WITHOUT ROWID` with `ON DELETE CASCADE` and `idx_dhash_buckets_media_id`, normalizes uppercase `dhash` values, backfills `visual_status='ok'` only for already-fingerprinted rows (leaving pre-v1.2.0 empty-visual HEIC rows as `''`), and backfills 8×4-hex bucket entries.
   - **Primary-Key Bucket Search:** Independently verified via `EXPLAIN QUERY PLAN` on [`_DHASH_BUCKET_CANDIDATES_SQL`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L21-L43) that SQLite executes 8 `SEARCH media_dhash_buckets USING PRIMARY KEY (bucket_idx=? AND bucket_val=?)` index lookups without table scans when `perceptual_max_distance < 8`, and falls back to full scan when `perceptual_max_distance >= 8` or legacy 16-char hashes exist.
   - **G2 Quality Upgrade Journal & Batching:** `create_pending_quality_upgrade`, `finalize_quality_upgrade`, `delete_quality_upgrade`, `list_in_progress_quality_upgrades`, and `list_quarantined_upgrades_batch` are genuinely backed by SQLite queries, and `in_progress` rows are excluded from `list_quality_upgrades()` and `get_stats()["quality_upgrades_count"]`.
   - **Hash Ordering, Archive Retry Reset, Album Retry Guard & Periodic Log Pruning:** All verified against specification in both source and unit tests.

---

## 4. Verdict

Verdict: CLEAN

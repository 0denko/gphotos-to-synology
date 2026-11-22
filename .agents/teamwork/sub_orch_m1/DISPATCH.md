# Incoming Dispatch — 2026-11-21T23:38:19Z

**Sender:** `0c3a285739969d69c9f4f1d8cde17a67` (Parent Orchestrator)
**Milestone:** M1 — Foundation (Dependencies, Version, Config & Database) (`sub_orch_m1`)

```text
You are the Sub-Orchestrator for **Milestone M1 — Foundation (Dependencies, Version, Config & Database)** (`sub_orch_m1`).

**Mandatory Inputs (pass `ORIGINAL_REQUEST.md` verbatim to EVERY subagent you spawn!)**:
- Original user request: `/home/antigravity/gphotos-to-synology/.agents/teamwork/ORIGINAL_REQUEST.md`
- Project specification & interface contracts: `/home/antigravity/gphotos-to-synology/PROJECT.md`
- Code review reference: `/home/antigravity/gphotos-to-synology/Collab.md`
- Baseline Survey reports (read-only context for your Explorers/Worker):
  - `/home/antigravity/gphotos-to-synology/.agents/teamwork/explorer_survey_1/handoff.md`
  - `/home/antigravity/gphotos-to-synology/.agents/teamwork/explorer_survey_2/handoff.md`
  - `/home/antigravity/gphotos-to-synology/.agents/teamwork/explorer_survey_3/handoff.md`

**Workspace & State Folder**:
- Project working directory: `/home/antigravity/gphotos-to-synology`
- Your state folder (write ONLY your metadata here): `/home/antigravity/gphotos-to-synology/.agents/teamwork/sub_orch_m1`
- Parent orchestrator conversation ID: `0c3a285739969d69c9f4f1d8cde17a67`

**Exclusive File Ownership (Worker may ONLY edit/create these files — do NOT touch other source files or `tests/e2e/`)**:
- `pyproject.toml`
- `uv.lock` (generated via `uv lock` + `.venv` synced to match)
- `src/gphotos_synology/__init__.py`
- `src/gphotos_synology/config.py`
- `src/gphotos_synology/database.py`
- `tests/test_config.py` (new)
- `tests/test_database.py` (new)

**Milestone M1 Requirements (from `PROJECT.md § Feature Inventory` & `§ Interface Contracts`)**:
1. **Version & Dependencies (`pyproject.toml`, `uv.lock`, `__init__.py`)**:
   - Bump `version = "1.2.0"` in `pyproject.toml` and `__version__ = "1.2.0"` in `src/gphotos_synology/__init__.py`.
   - Add `"pillow-heif>=1.0.0,<1.1.0"` and tighten `"pillow>=11.1.0,<12.3.0"` (CRITICAL: required so `uv.lock` resolves `pillow-heif==1.0.0` and `pillow==12.2.0`, both verified to ship glibc-2.17 `manylinux2014` wheels for `x86_64` and `aarch64`!).
   - Add optional extra `spk = ["pyinstaller>=6.10.0,<7.0.0"]` in `[project.optional-dependencies]`.
   - Run `uv lock` to generate `uv.lock` and sync `.venv` (ensure `.venv/bin/pytest`, `.venv/bin/ruff`, `pillow-heif`, and `pillow==12.2.0` all work cleanly!).
2. **`src/gphotos_synology/config.py`**:
   - **G5**: Default `synology_url = "https://127.0.0.1:5001"`, `synology_verify_ssl = True`, `app_host = "127.0.0.1"`; add `synology_ca_cert: str = ""`, `quarantine_dir_override: Path | None = None` (`Settings.quarantine_dir` uses `quarantine_dir_override` if set else `work_dir / "replaced_duplicates"`).
   - **C1/C2/G6 config fields**: Add `proxy_secret: str = ""` (env `GPHOTOS_PROXY_SECRET` or `PROXY_SECRET`), `proxy_secret_file: Path | None = None` (env `PROXY_SECRET_FILE`), `api_token: str = ""` (env `API_TOKEN`), `dsm_admin_users: str = ""` (comma-separated admin usernames), `trust_proxy_headers: bool = False`, `allow_insecure_no_auth: bool = False`. Helper method `resolve_proxy_secret() -> str` reading `proxy_secret` or `proxy_secret_file` cleanly. Add `proxy_secret` and `api_token` to `SECRET_FIELDS`; keep all security/path/watchtower fields OUT of `MUTABLE_UI_FIELDS`.
   - **G4**: In `ConfigManager.update(patch)`: validate `synology_url` (`http://` or `https://` scheme only, no whitespace/control chars → raise `ValueError`); if `synology_url` actually changes from its prior value AND the patch does NOT provide a new non-empty, non-`"********"` `synology_password`, clear stored `synology_password = ""`, log a WARNING, and record a warning in `self.last_update_warnings`.
   - **Lazy OAuth inheritance**: Add optional `fallback_settings_provider: Callable[[], Settings] | None = None` parameter to `ConfigManager.__init__`; dynamically inherit unset `google_client_id`, `google_client_secret` (and `synology_url` if unset/empty) from `fallback_settings_provider()` when computing `.settings` (keep `to_public_dict()` accurate too).
   - **Secrets & config resilience**: On corrupt `runtime_settings.json`, log ERROR and move/copy to `runtime_settings.json.corrupt.<timestamp>` before falling back to defaults. Create temp file with `os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)` (no umask race) and log WARNING if `os.chmod` fails instead of silently suppressing.
3. **`src/gphotos_synology/database.py`**:
   - Add `delete_state(self, key: str) -> None` (for G3).
   - Idempotent additive migrations (`PRAGMA table_info` + `ALTER TABLE` + `CREATE INDEX IF NOT EXISTS`):
     - `media_items.visual_status TEXT NOT NULL DEFAULT ''` (for G10).
     - `media_dhash_buckets(bucket_idx INTEGER NOT NULL, bucket_val TEXT NOT NULL, media_id INTEGER NOT NULL, PRIMARY KEY (bucket_idx, bucket_val, media_id)) WITHOUT ROWID` (for G11 SQL bucketing — 8 blocks of 4 hex chars = 16-bit prefix blocks, synced on insert/update/upgrade/prune, queried in `find_duplicate_media` Tier 3 when `effective_max < 8` and `len(dhash) == 32`, with automatic fallback to full scan when `effective_max >= 8` or legacy 16-char hash so recall is 100%, AND backfill existing `media_items` dhashes during migration!).
     - `quality_upgrades` additive columns: `old_rel_path TEXT NOT NULL DEFAULT ''`, `old_taken_at TEXT NOT NULL DEFAULT ''`, `old_latitude REAL`, `old_longitude REAL`.
   - `quality_upgrades` journal lifecycle methods (for G2 & restore/purge):
     - `create_pending_quality_upgrade(...) -> int` (`status='in_progress'`).
     - `finalize_quality_upgrade(upgrade_id: int, kept_rel_path: str) -> None` (`'in_progress'` → `'quarantined'`).
     - `delete_quality_upgrade(upgrade_id: int) -> None`.
     - `list_in_progress_quality_upgrades() -> list[dict[str, Any]]`.
     - `list_quarantined_upgrades_batch(limit: int = 500) -> list[dict[str, Any]]`.
     - Update `record_quality_upgrade(...)` to also accept optional `old_rel_path`, `old_taken_at`, `old_latitude`, `old_longitude` (backwards-compatible defaults).
   - **Hash lookup ordering**: Update `get_media_by_original_hash` and `find_duplicate_media` Tier 1 to use `ORDER BY CASE WHEN original_sha256 = ? THEN 0 ELSE 1 END, id ASC LIMIT 1`.
   - **`upsert_archive` retry reset**: When an existing non-completed archive is upserted/retried, reset `total_files=0, imported_files=0, duplicate_files=0, failed_files=0, completed_at=NULL, error_message=''`.
   - **Album retry fix**: Update `mark_album_membership` to accept `increment_retry: bool | None = None`, defaulting to `status == "error"` so `status == "pending"` (waiting for Synology indexing) never increments `retry_count`.
   - **`log_event` periodic prune**: Prune `activity_logs` every 50 inserts (via instance counter) instead of running `DELETE ... NOT IN` on every single insert.
   - **Scanner undecodable state**: `get_indexed_files_by_rel_path()` returns `visual_status` and computes `has_visual` / `visual_done` cleanly (treating `visual_status == 'undecodable'` as stable so scanner doesn't re-hash every cycle); `upsert_existing_nas_media` and `record_media_item` accept `visual_status: str = ""`.
   - **`get_stats()`**: Include `completed_with_errors_archives` (or count accurately without breaking existing keys!).
4. **Comprehensive unit tests (`tests/test_config.py`, `tests/test_database.py`)** + ensure ALL existing 20 tests in `tests/` continue passing and `ruff check src tests` is clean.
5. **Do NOT run `git commit` or `git push`** — leave verified changes in the working tree.

**Procedure**: Follow Iteration Loop 2B strictly (3 Explorers → 1 Worker → parallel 2 Reviewers + 2 Challengers + 1 Forensic Auditor labeled as an **intermediate milestone audit** with sub-scope paths `pyproject.toml`, `uv.lock`, `src/gphotos_synology/__init__.py`, `src/gphotos_synology/config.py`, `src/gphotos_synology/database.py`, `tests/test_config.py`, `tests/test_database.py` → Gate). Report completion via `send_message` to `0c3a285739969d69c9f4f1d8cde17a67` with path to your `handoff.md`.
```

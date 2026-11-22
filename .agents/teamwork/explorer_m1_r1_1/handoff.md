# M1 Explorer 1 Handoff — Dependencies, Version, Packaging & Existing Tests

**Summary:** Verified `pyproject.toml`, `src/gphotos_synology/__init__.py`, all cross-repo version/dependency references, the local `uv 0.13.8` / Python `3.11.2` `.venv` / SQLite `3.40.1` environment, live PyPI `manylinux2014` wheel resolution for both `x86_64` and `aarch64` (`cp311` + `cp312`), and all 9 existing test files (20 tests). Uncovered three concrete pitfalls the M1 Worker must respect so nothing breaks: (1) bare `uv sync` strips `pytest`/`ruff` because `dev` is in `[project.optional-dependencies]` (MUST use `uv sync --locked --extra dev`); (2) `pydantic-settings` requires `AliasChoices` for `GPHOTOS_PROXY_SECRET` → `proxy_secret` and `QUARANTINE_DIR` → `quarantine_dir_override`; and (3) all 20 existing tests pass with zero modifications if M1 preserves `ConfigManager`/`StateDB` backwards-compatible signatures and G4 triggers password-clearing only on actual `synology_url` change.

---

## 1. Version & Dependency References Audit

### 1.1 Files Directly in M1 Scope
- [`pyproject.toml`](file:///home/antigravity/gphotos-to-synology/pyproject.toml#L1-L55):
  - Line 7: `version = "1.0.0"` → bump to `"1.2.0"`.
  - Line 10: `requires-python = ">=3.11"`.
  - Line 23: `"pillow>=10.4.0,<13.0.0"` → tighten to `"pillow>=11.1.0,<12.3.0"` (mandatory `manylinux2014` ceiling; locks `12.2.0`).
  - After line 23: add `"pillow-heif>=1.0.0,<1.1.0"` (mandatory `manylinux2014` ceiling; locks `1.0.0`, requires `pillow>=11.1.0`).
  - Lines 27–32 (`[project.optional-dependencies]`): currently only `dev = ["pytest>=8.3.0", "pytest-asyncio>=0.24.0", "ruff>=0.7.0"]`. Add `spk = ["pyinstaller>=6.10.0,<7.0.0"]` so `uv.lock` locks PyInstaller (`6.22.3`) for M5 CI builds.
  - Note: No `uv.lock` currently exists in the repo root.
- [`src/gphotos_synology/__init__.py`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/__init__.py#L1-L4):
  - Line 3: `__version__ = "1.0.0"` → bump to `"1.2.0"`.

### 1.2 Other Files Referencing `__version__`, Version Strings, or `pyproject.toml` (Out of M1 Scope — Track for M3/M5)
| File | Lines | Current Reference | Milestone | Impact of M1 Bump to `1.2.0` |
|---|---|---|---|---|
| `src/gphotos_synology/main.py` | 15, 82 | `from gphotos_synology import __version__`, `FastAPI(..., version=__version__)` | Automatic | Automatically reflects `1.2.0`. |
| `src/gphotos_synology/updater.py` | 15, 99, 118 | `current_version=__version__`, `User-Agent: gphotos-to-synology/{__version__}` | M5 | Safe in M1: current `updater.py:160-164` compares SHAs only, not versions. *(M5 note: when switching `updater.py` to semver comparison, update `tests/test_updater.py:28` mock tag from `v1.0.1` to `v1.3.0`.)* |
| `src/gphotos_synology/web/static/index.html` | 20 | Hardcoded `<span id="version-badge">v1.0.0</span>` (overwritten at runtime by `app.js:501-502`) | M3/M5 | Safe in M1 (`test_api.py:30` checks `"Google Photos" in resp.text`, not `v1.0.0`). |
| `synology-spk/build_spk.py` | 27, 285 | `VERSION = "1.1.0-0003"` | M5 | Safe in M1 (M5 derives `1.2.0-0001` from `__version__`). |
| `.github/workflows/docker-publish.yml` | 27, 60 | `pip install -e ".[dev]"` and `uv pip install --only-binary :all: -r pyproject.toml pyinstaller` | M5 | M5 will switch to `uv.lock` (`--locked` / `uv export --locked --extra spk`) + `--collect-all pillow_heif`. |
| `Dockerfile` | 27–29 | `COPY pyproject.toml README.md ./` + `pip install --no-cache-dir .` | M5 | M5 will copy `uv.lock` and install locked. |

---

## 2. Local Environment, PyPI Reachability & `uv` Lock/Sync Verification

### 2.1 Measured Local Toolchain
- **`uv --version`**: `uv 0.13.8 (x86_64-unknown-linux-gnu)`
- **`.venv/bin/python --version`**: `Python 3.11.2` (`.venv/pyvenv.cfg`: `home = /usr/bin`, `uv = 0.13.8`, `include-system-site-packages = false`)
- **SQLite version**: `3.40.1` (supports `WITHOUT ROWID`, WAL, `PRAGMA table_info`, `ALTER TABLE ADD COLUMN`, `UPSERT`)
- **Currently installed key packages in `.venv`**:
  - `gphotos-to-synology 1.0.0` (editable at `/home/antigravity/gphotos-to-synology`)
  - `pillow 12.3.0` (**will downgrade to `12.2.0`** after M1 `uv sync`)
  - `pillow-heif` (**not installed yet**; will install `1.0.0`)
  - `pydantic 2.14.4`, `pydantic-settings 2.16.3`, `fastapi 0.165.0`, `httpx 0.28.1`, `piexif 1.1.3`
  - Dev tools: `pytest 9.2.0`, `pytest-asyncio 1.5.0`, `ruff 0.17.6`

### 2.2 Live PyPI & Cross-Platform `manylinux2014` Dry-Run Verification (Tested in Isolated `/tmp` Dir)
Tested exact target `pyproject.toml` edits (`version = "1.2.0"`, `"pillow>=11.1.0,<12.3.0"`, `"pillow-heif>=1.0.0,<1.1.0"`, `spk = ["pyinstaller>=6.10.0,<7.0.0"]`) using `uv lock` + `uv export` + `uv pip install --dry-run --only-binary :all:` without touching the repository:

1. **Locked versions verified**:
   - `pillow == 12.2.0` (`cp311` + `cp312` `manylinux2014_x86_64` & `manylinux2014_aarch64`)
   - `pillow-heif == 1.0.0` (`cp311` + `cp312` `manylinux_2_17` / `manylinux2014` `x86_64` & `aarch64`, `requires-dist: pillow>=11.1.0`)
   - `pyinstaller == 6.22.3` (`py3-none-manylinux2014_x86_64` & `py3-none-manylinux2014_aarch64`)
   - `pytest == 9.2.0`, `pytest-asyncio == 1.5.0`, `ruff == 0.17.6`
2. **All 54 runtime+`spk` packages verified on `manylinux2014` `cp312`**:
   - `uv export --locked --extra spk --no-dev --no-hashes --no-emit-project -o spk-reqs.txt` → `rc=0`
   - `uv pip install --dry-run --only-binary :all: --python-version 3.12 --python-platform x86_64-manylinux2014 -r spk-reqs.txt` → **`rc=0` (54 packages)**
   - `uv pip install --dry-run --only-binary :all: --python-version 3.12 --python-platform aarch64-manylinux2014 -r spk-reqs.txt` → **`rc=0` (54 packages)**
   - *(Note: `httptools==0.9.0` and `websockets==17.4` ship `manylinux1_x86_64` (glibc 2.5 ≤ 2.17) on x86_64 and `manylinux2014_aarch64` on arm64 — fully glibc-2.17 compatible.)*

### 2.3 Critical `uv sync` Flags to Preserve `.venv` Dev Tools
Empirically verified in isolated temp workspace:
- **DO NOT run bare `uv sync` or `uv sync --locked`**: Because `dev` is declared under `[project.optional-dependencies]` (PEP 621 extras) rather than `[dependency-groups]`, bare `uv sync` **uninstalls `pytest`, `pytest-asyncio`, and `ruff`** from `.venv`!
- **Exact commands for M1 Worker**:
  ```bash
  uv lock
  uv sync --locked --extra dev
  ```
  - Verified: when `.venv` already exists at Python 3.11.2, `uv sync --locked --extra dev` prints `Using project environment at: .venv`, keeps Python 3.11.2, keeps `pytest`/`pytest-asyncio`/`ruff`, installs `pillow-heif==1.0.0`, downgrades `pillow` to `12.2.0`, and refreshes editable `gphotos-to-synology==1.2.0` (without pulling `pyinstaller` into `.venv`).
  - Verification commands:
    ```bash
    uv lock --check
    uv sync --locked --extra dev --check
    .venv/bin/python -c "import gphotos_synology, PIL, pillow_heif; print(gphotos_synology.__version__, PIL.__version__, pillow_heif.__version__)"
    # Expected: 1.2.0 12.2.0 1.0.0
    ```

---

## 3. Complete Audit of All 9 Existing Test Files (20 Tests) vs M1 Changes

| Test File | Tests | Uses `Settings` / `ConfigManager` / `StateDB` / `__version__` | Exact Lines & M1 Compatibility Verdict |
|---|---:|---|---|
| `tests/test_api.py` | 1 | `Settings(...)` (L14–20), `PUT /api/settings` (L46–69) | **Safe IF G4 check is exact:** L46–55 sets `synology_password="super-secret-password"` (no `synology_url`); L63–69 sends `{"synology_password": "********", "folder_template": "year_ym"}` and asserts `synology_password == "super-secret-password"`. G4 MUST only clear `synology_password` when `synology_url` is in `patch` AND its value differs from the current `synology_url`. Does NOT assert `app_host`, `synology_url`, or `synology_verify_ssl` defaults. |
| `tests/test_profiles.py` | 1 | `Settings(...)` (L24–33), `PUT /api/settings` `google_client_id` (L38–41), `GET /api/settings` for `maria` (L66–67) | **Safe:** `profiles.py:125,148` constructs `ConfigManager(base_settings=...)`; adding optional `fallback_settings_provider=None` preserves compatibility. Does NOT assert G5 defaults. |
| `tests/test_synology_api_and_security.py` | 2 | `Settings(synology_url="http://synology.local:5000", ...)` (L64–70) | **Safe IF G4 allows `http://`:** L66 explicitly passes `http://synology.local:5000` (and injects `mock_http`). URL validator in `config.py` MUST allow both `http://` and `https://` schemes. Does NOT assert default `synology_verify_ssl`. |
| `tests/test_updater.py` | 2 | `Settings(app_build_sha=..., watchtower_url=..., watchtower_token=...)` (L48–52, 64, 80–83) | **Safe in M1:** Asserts `res["latest_version"] == "1.0.1"` (L57) and SHA-based `update_available`; does NOT assert `current_version`. |
| `tests/test_pipeline_e2e.py` | 1 | `Settings(...)` (L94–105), `ConfigManager(settings)` (L106), `StateDB(db_path)` (L107), `db.get_stats()` (L131–136) | **Safe:** Asserts `get_stats()` keys `total_media`, `total_duplicates_skipped`, `total_albums`, `synced_album_items`, `pending_album_items`. Calls `mark_album_membership` via pipeline. |
| `tests/test_existing_nas_dedup.py` | 3 | `Settings(...)` (L39–45, 85–95, 155–166), `ConfigManager`, `StateDB`, `db.get_stats()`, `db.list_albums_summary()` | **Safe IF dHash bucketing & `visual_status` stay backwards-compatible:** L165 uses `perceptual_max_distance=4` (exercises `media_dhash_buckets` when `effective_max < 8`!); L68/80/140/202 assert `existing_nas_indexed`, `total_media`, `total_duplicates_skipped`. `upsert_existing_nas_media` and `record_media_item` MUST default `visual_status=""`. |
| `tests/test_visual_quality_upgrade.py` | 3 | `Settings(...)` (L47–58, 107–118, 162–173), `cfg.settings.quarantine_dir` (L94), `db.list_quality_upgrades()` (L88, 142, 157), `db.get_stats()` (L158) | **Safe IF `quarantine_dir` property & Tier 3 fallback stay intact:** Uses default `perceptual_max_distance=10` (`effective_max >= 8` → MUST fall back from 8-block buckets to full scan so all 3 tests still match!). Accesses `cfg.settings.quarantine_dir` (L94) and `routes.py:174`. Note L211–214 (`purge` after `restore`) is an M4 change, NOT M1 — do not touch `pipeline.py` in M1. |
| `tests/test_metadata_and_importer.py` | 3 | `Settings(...)` (L43–49) | **Safe:** Only sets storage dirs + `folder_template="year_month"`. |
| `tests/test_takeout_matcher.py` | 4 | None (pure `matcher.py` tests) | **Safe:** Zero coupling to M1 files. |

**Bottom line:** **0 of the 20 existing tests** assert the old `app_host="0.0.0.0"`, `synology_url="http://172.17.0.1:5000"`, `synology_verify_ssl=False`, or `__version__ == "1.0.0"`. All 20 existing tests MUST remain green after M1 without editing any existing test file.

---

## 4. `Collab.md` Findings Relevant to M1 (G7 & G10 + Config/DB Contracts)

- **G7 (`Collab.md:94,111`) — M1 Share**:
  - Unify Python package versions to `1.2.0` (`pyproject.toml:7` + `src/gphotos_synology/__init__.py:3`).
  - Commit deterministic `uv.lock` covering runtime + `dev` + `spk` extras so M5 CI/Docker can install `--locked`.
- **G10 (`Collab.md:84`) — M1 Share**:
  - Add `pillow-heif>=1.0.0,<1.1.0` and tighten `pillow>=11.1.0,<12.3.0` in `pyproject.toml` (mandatory `manylinux2014` glibc-2.17 wheel cutoff — `pillow>=12.3.0` and `pillow-heif>=1.1.0` dropped `manylinux2014`).
  - Add `visual_status TEXT NOT NULL DEFAULT ''` column to `media_items` via additive migration so M2's `dedup.py` & `scanner.py` can persist `'ok' | 'undecodable' | 'skipped'`.
- **Pydantic `AliasChoices` Pitfall Verified Empirically for `config.py`**:
  - Tested in `.venv/bin/python` (`pydantic 2.14.4`, `pydantic-settings 2.16.3`):
    - `proxy_secret: str = Field(default="", validation_alias=AliasChoices("proxy_secret", "gphotos_proxy_secret"))` cleanly accepts env `GPHOTOS_PROXY_SECRET=...`, kwargs `Settings(proxy_secret=...)`, AND `Settings.model_validate(s.model_dump())` round-trips!
    - `quarantine_dir_override: Path | None = Field(default=None, validation_alias=AliasChoices("quarantine_dir_override", "quarantine_dir"))` cleanly accepts env `QUARANTINE_DIR=/path`, kwargs `Settings(quarantine_dir_override=...)`, AND `model_validate(model_dump())`, without colliding with `@property def quarantine_dir(self) -> Path`!
  - Also note `routes.py:248-251` already wraps `pctx.config_manager.update(body.updates)` in `try ... except Exception as exc: raise HTTPException(status_code=400, detail=str(exc))`, so raising `ValueError` in `ConfigManager.update()` for invalid `synology_url` immediately produces HTTP 400 in the API.

---

## 5. Exact Recommendations for M1 Worker

1. **Edit [`pyproject.toml`](file:///home/antigravity/gphotos-to-synology/pyproject.toml)**:
   - Line 7: `version = "1.2.0"`.
   - Line 23: replace `"pillow>=10.4.0,<13.0.0",` with:
     ```toml
     "pillow>=11.1.0,<12.3.0",
     "pillow-heif>=1.0.0,<1.1.0",
     ```
   - Lines 27–33 (`[project.optional-dependencies]`): add `spk = ["pyinstaller>=6.10.0,<7.0.0"]` alongside `dev`.
2. **Edit [`src/gphotos_synology/__init__.py`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/__init__.py)**:
   - Line 3: `__version__ = "1.2.0"`.
3. **Generate `uv.lock` and sync `.venv` safely**:
   - Run `uv lock` then `uv sync --locked --extra dev` (NEVER bare `uv sync`).
   - Verify `uv lock --check`, `uv sync --locked --extra dev --check`, and `.venv/bin/python -c "import gphotos_synology, PIL, pillow_heif; print(gphotos_synology.__version__, PIL.__version__, pillow_heif.__version__)"` → `1.2.0 12.2.0 1.0.0`.
4. **In `config.py`**:
   - Use `AliasChoices("proxy_secret", "gphotos_proxy_secret")` for `proxy_secret` and `AliasChoices("quarantine_dir_override", "quarantine_dir")` for `quarantine_dir_override`.
   - Ensure G4 `synology_url` validation allows `http://` and `https://` (rejects other schemes, missing host/netloc, whitespace/control chars) and clears `synology_password` ONLY when normalized `synology_url` actually changes from the existing value without a new non-empty, non-`"********"` password in `patch`.
   - Keep `proxy_secret`, `proxy_secret_file`, `api_token`, `dsm_admin_users`, `trust_proxy_headers`, `allow_insecure_no_auth`, `quarantine_dir_override`, `photos_dir`, `watchtower_url`, `watchtower_token` OUT of `MUTABLE_UI_FIELDS`; add `proxy_secret` and `api_token` to `SECRET_FIELDS`.
5. **In `database.py`**:
   - Preserve 100% backwards-compatible signatures on `upsert_existing_nas_media`, `record_media_item`, `record_quality_upgrade`, `mark_album_membership`, and `get_stats()`.
   - In `find_duplicate_media` Tier 3: query `media_dhash_buckets` when `len(dhash) == 32` and `effective_max < 8`, and fall back to full `dhash != ''` scan when `effective_max >= 8` or `len(dhash) != 32` (keeps `test_existing_nas_dedup.py` at dist 4 fast via buckets AND `test_visual_quality_upgrade.py` at dist 10 100% accurate).
6. **Verify before finishing M1**:
   - `.venv/bin/ruff check src tests`
   - `.venv/bin/pytest -v` (all 20 existing tests + new `tests/test_config.py` and `tests/test_database.py` pass)
   - `uv lock --check && uv sync --locked --extra dev --check`

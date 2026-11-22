# Explorer 3 Handoff: Packaging, SPK Lifecycle, Updater, Environment & Test Conventions

**Scope:** `synology-spk/build_spk.py`, `src/gphotos_synology/updater.py`, `pyproject.toml`, `Dockerfile`, `docker-compose.yml`, `.env.example`, `.dockerignore`, `.github/workflows/docker-publish.yml`, `tests/` conventions, and runtime environment check.  
**Baseline Reviewed:** HEAD `dfcf34f` (`v1.1.0-0003`) compared against target `v1.2.0` contracts in `ORIGINAL_REQUEST.md`, `PROJECT.md`, and `Collab.md`.

---

## 1. Synology DSM 7 SPK Packaging (`synology-spk/build_spk.py`)

### Public CLI & Function Contract
- **CLI Entry Point:** `python synology-spk/build_spk.py`
  - `--arch {noarch,x86_64,arm64}` (default: `noarch`)
  - `--bundle-dir <Path>` (default: `None`; when supplied, builds standalone native SPK with embedded `app/` PyInstaller bundle)
  - `--output <Path>` (default: `dist/gphotos-migrator-dsm7[-<arch>].spk`)
  - `--build-sha <str>` (fallback: `APP_BUILD_SHA` env, then `"dev"`)
  - `--build-date <str>` (fallback: `APP_BUILD_DATE` env, currently falls back to `datetime.now(UTC)`)
- **Python Function:** `build_spk(*, arch_key: str = "noarch", bundle_dir: Path | None = None, output_path: Path | None = None, build_sha: str = "", build_date: str = "") -> Path`
- **Constants:**
  - `PKG_NAME = "gphotos-migrator"`
  - `VERSION`: currently `"1.1.0-0003"` → **Target `v1.2.0`**: `"1.2.0-0001"`
  - `DSM_ARCH_MAP`: `noarch`, `x86_64` (includes `x86_64 apollolake ... v1000`), `arm64` (`aarch64 armada37xx armv8 rtd1296 rtd1619b`)

### SPK Archive Structure & Metadata
- Outer `.spk` is an uncompressed POSIX tar archive containing:
  - `INFO` — key-value metadata: `package="gphotos-migrator"`, `version="1.2.0-0001"` (target), `displayname="Google Photos Migrator"`, `maintainer="Oleksandr Danylchenko"`, `distributor="Oleksandr Danylchenko"`, `os_min_ver="7.0-40000"`, `thirdparty="yes"`, `startable="yes"`, `dsmuidir="ui"`, `dsmappname="SYNO.SDS.GPhotosMigrator.Application"`, `extractsize="<kb>"`.
  - `package.tgz` — gzip-compressed tar extracted to `/var/packages/gphotos-migrator/target`:
    - `ui/`: `GPhotosMigrator.js`, `config`, `index.conf`, `proxy.cgi` (`0755`), `oauth_callback.cgi` (`0755`), `images/icon_{16,24,32,48,64,72,256}.png`, `texts/enu/strings` (note: `synology-spk/ui/texts/enu/strings` already exists on disk and is copied into `package.tgz`).
    - `app/`: copied from `--bundle-dir` when standalone; `app/gphotos-migrator` set to `0755`.
  - `conf/privilege`: `{"defaults": {"run-as": "package"}, "username": "gphotos-migrator"}` (least-privilege package user, no root).
  - `scripts/`: `start-stop-status`, `preinst`, `postinst`, `preuninst`, `postuninst`, `preupgrade`, `postupgrade` (all `0755`).
  - `PACKAGE_ICON.PNG` (64×64), `PACKAGE_ICON_256.PNG` (256×256).

### Embedded Lifecycle Scripts — Current Gaps vs Target Contracts (C1, G7, G8, Minor Hygiene)
1. **`scripts/start-stop-status` (standalone mode):**
   - *Current:* Uses `SYNOPKG_PKGDEST` / `SYNOPKG_PKGVAR`, exports `DATA_DIR=${PKG_VAR}/config`, `WORK_DIR=${PKG_VAR}/work`, `INBOX_DIR=${PKG_VAR}/inbox`, `PHOTOS_DIR`, `HOMES_DIR`, `APP_HOST="127.0.0.1"`, `APP_PORT="8765"`, `APP_BUILD_SHA`, `APP_BUILD_DATE`. Trusts PID file blindly (`kill -0 "$pid"`), no start lock, no log rotation, no `proxy.secret`.
   - *Target (C1 + Minor SPK hygiene):*
     - Creates `${PKG_VAR}/proxy.secret` with `umask 077`, 64 hex chars, `chmod 0600` (if absent or preserving existing), and exports `PROXY_SECRET_FILE="${PKG_VAR}/proxy.secret"`.
     - Verifies PID cmdline/process identity before treating PID as running or sending `kill` (prevents signaling unrelated process on PID reuse).
     - Uses a start lock to prevent double-start races.
     - Rotates `${PKG_VAR}/gphotos-migrator.log` when oversized.
   - *UI-only (`noarch` / `standalone=False`) mode:*
     - *Current:* `status` always exits `0`.
     - *Target:* `status` probes backend health (e.g. `127.0.0.1:8765/api/health`) instead of blindly exiting `0`.
2. **`scripts/preupgrade` (G8):**
   - *Current:* `#!/bin/sh\nexit 0\n` no-op.
   - *Target:* Creates a timestamped backup snapshot of config/DB before upgrade and retains the last 3 snapshots (pruning older ones).
3. **`scripts/preuninst` (G8):**
   - *Current:* `#!/bin/sh\nexit 0\n` no-op.
   - *Target:* On `SYNOPKG_PKG_STATUS=UNINSTALL`, backs up user data (config, quarantine `replaced_duplicates`, inbox) OUTSIDE `PKG_VAR` to `${PHOTOS_DIR}/.gphotos-migrator-backup/<timestamp>/`, explicitly **excluding `proxy.secret`** (never copies `proxy.secret` into shared photo folder). Should NOT run destructive/uninstall-only copy when `SYNOPKG_PKG_STATUS=UPGRADE` (or only runs uninstall backup on `UNINSTALL`).
4. **Deterministic Timestamps & Reproducibility (G7):**
   - *Current:* `_normalize_tarinfo` normalizes `uid=0`, `gid=0`, `uname="root"`, `gname="root"`, and modes (`0755`/`0644`), but leaves `ti.mtime` unnormalized, uses `tarfile.open(pkg_tgz, "w:gz")` (non-deterministic gzip header timestamp), and defaults `bdate` to wall-clock `datetime.now(UTC)`.
   - *Target:* Normalized tar `mtime` (e.g., fixed epoch or parsed `build_date`), deterministic gzip header (`mtime=0`), so two builds with identical `--bundle-dir`, `--build-sha`, and `--build-date` produce **byte-identical `.spk` files (identical SHA-256)**.
5. **E2E Testability Notes for `build_spk.py`:**
   - Staging directory is hardcoded as `PROJECT_ROOT / "build" / f"spk_stage_{arch_key}"` and removed at the end of `build_spk()`; run SPK build tests serially (or with distinct `arch_key`s) and always pass `--output <tmp_path>/...spk` so `dist/` is untouched.
   - Extracted `scripts/start-stop-status`, `scripts/preupgrade`, and `scripts/preuninst` are POSIX `#!/bin/sh` scripts and can be executed directly via `/bin/sh` (`dash` on this host!) with isolated `SYNOPKG_PKGDEST`, `SYNOPKG_PKGVAR`, `PHOTOS_DIR`, `SYNOPKG_PKG_STATUS` environment variables and a stub executable binary in `app/gphotos-migrator`.

---

## 2. Updater (`src/gphotos_synology/updater.py` & `/api/updates*`)

### Public Interfaces
- **Dataclass `UpdateStatus`** (`src/gphotos_synology/updater.py:29-45`):
  - Fields: `current_version`, `current_sha`, `build_date`, `latest_version`, `latest_sha`, `latest_commit_message`, `latest_commit_date`, `update_available`, `release_url`, `spk_download_url`, `auto_updater_configured`, `checked_at`, `error`.
  - Target G7 adds checksum/SHA-256 metadata fields (from `.sha256` / `SHA256SUMS.txt` release assets).
- **Class `UpdateService(cache_ttl_seconds: int = 1800)`** (`src/gphotos_synology/updater.py:48-204`):
  - `get_status(settings: Settings, *, force: bool = False, http_client: httpx.Client | None = None) -> dict[str, Any]`
  - `trigger_self_update(settings: Settings, *, http_client: httpx.Client | None = None) -> dict[str, Any]`
- **HTTP Endpoints** (`src/gphotos_synology/web/routes.py:217-231`):
  - `GET /api/updates?force=false` → calls `app_ctx.updater.get_status(settings, force=force)`
  - `POST /api/updates/apply` → calls `app_ctx.updater.trigger_self_update(settings)`; returns `400` on exception. (Under C1/C2/G6, requires auth, `X-Requested-With`, and admin role in DSM mode.)

### GitHub API & Watchtower Contracts (Current vs Target G7 / Nits)
1. **GitHub API Calls:**
   - Currently fetches `GET https://api.github.com/repos/{repo}/commits/main` and `GET https://api.github.com/repos/{repo}/releases/latest`.
   - Release asset selection: picks `.spk` matching `-{arch}.spk` where `arch` is `"arm64"` on `aarch64`/`arm64`, else `"x86_64"`.
2. **Non-Downgrading Semver Comparison (G7):**
   - *Current:* Compares `full_latest_sha` != `current_sha` — will report `update_available=True` even when `latest_version <= current_version` (e.g., running `1.2.0` while release is `1.1.0`, or running a local commit ahead of `main`).
   - *Target:* Compare parsed semver versions (`latest_version > current_version`):
     - `latest < current` (downgrade) → `update_available == False`
     - `latest == current` → `update_available == False`
     - `latest > current` (e.g., `1.2.1` or `1.3.0` vs `1.2.0`) → `update_available == True`
3. **Checksum Asset Handling (G7):**
   - *Target:* Recognizes `.sha256` (e.g. `gphotos-migrator-dsm7-x86_64.spk.sha256` or versioned `*.spk.sha256`) and/or `SHA256SUMS.txt` assets from GitHub release JSON and surfaces the checksum / checksum URL in the status dict (and UI).
4. **Error Surfacing (G7 Nit):**
   - *Current:* `releases/latest` fetch is wrapped in `except Exception: pass` (`updater.py:157-158`), silently hiding 404/500/network errors.
   - *Target:* Release check failures (HTTP error or transport exception) must populate `status["error"]` with a descriptive non-empty error string.
5. **Watchtower Trigger — POST-only (Nit):**
   - *Current:* Sends `POST {wt_url}/v1/update?async=1` with `Authorization: Bearer {wt_token}`, and if status is `405`, falls back to `GET {wt_url}/v1/update` (`updater.py:193-196`).
   - *Target:* Must NOT fall back to `GET` on HTTP `405` — must raise/fail cleanly, never issuing a state-changing `GET`.

---

## 3. Packaging, Docker, Env & CI Config Assertions (Opaque-Box Testable)

| File | Current State (`dfcf34f`) | Target `v1.2.0` Contract (Testable via Static/File/Subprocess Checks) |
|---|---|---|
| `pyproject.toml` | `version = "1.0.0"`, `pillow>=10.4.0,<13.0.0`, no `pillow-heif`, no `uv.lock` | `version = "1.2.0"`, `"pillow>=11.1.0,<12.3.0"`, `"pillow-heif>=1.0.0,<1.1.0"`, `uv.lock` exists and is committed |
| `src/gphotos_synology/__init__.py` | `__version__ = "1.0.0"` | `__version__ = "1.2.0"` (unified with `pyproject.toml` and SPK `1.2.0-0001`) |
| `Dockerfile` | `FROM python:3.12-slim`, `pip install --no-cache-dir .`, `--host 0.0.0.0` | Digest-pinned base image (or locked install), keeps `gosu` + `libimage-exiftool-perl` + `docker-entrypoint.sh` privilege drop |
| `docker-compose.yml` | `ports: ["8765:8765"]`, `TZ=America/Los_Angeles`, hardcoded `WATCHTOWER_TOKEN=gphotos-internal-update-token`, no `cap_drop`/`no-new-privileges`, `:latest` tags | Loopback bind `"127.0.0.1:8765:8765"` (C1), `TZ=UTC`, no hardcoded `gphotos-internal-update-token` (per-install env var), `cap_drop` + `no-new-privileges:true` |
| `.env.example` | `SYNOLOGY_URL=http://host.docker.internal:5000`, `SYNOLOGY_VERIFY_SSL=false` | `SYNOLOGY_VERIFY_SSL=true`, HTTPS default `SYNOLOGY_URL`, documents `API_TOKEN` / `WATCHTOWER_TOKEN` / `QUARANTINE_DIR` (G5/C1/G8) |
| `.dockerignore` | 11 entries; misses `.agents` | Excludes `.agents` (and planning/test artifacts) |
| `.github/workflows/docker-publish.yml` | Top-level `contents: write, packages: write`; tag-pinned actions/images; no `--locked`; no `--collect-all pillow_heif`; clobbers `v1.0.0` on every push to `main`; no `.sha256`/`SHA256SUMS.txt` | Minimal per-job permissions (read-only `test` job); SHA/digest-pinned actions & container images; `--locked`; `--collect-all pillow_heif`; versioned tag releases (no `v1.0.0 --clobber` on `main`); generates & uploads `.sha256` / `SHA256SUMS.txt` |

---

## 4. Existing `tests/` Layout & Pytest/Ruff Integration

### Current `tests/` Directory
- **9 test files, NO `tests/conftest.py`, NO `tests/__init__.py`:**
  - `tests/test_api.py`
  - `tests/test_existing_nas_dedup.py`
  - `tests/test_metadata_and_importer.py`
  - `tests/test_pipeline_e2e.py`
  - `tests/test_profiles.py`
  - `tests/test_synology_api_and_security.py`
  - `tests/test_takeout_matcher.py`
  - `tests/test_updater.py`
  - `tests/test_visual_quality_upgrade.py`

### Pytest & Ruff Configuration (`pyproject.toml`)
- **Pytest:**
  - `testpaths = ["tests"]` → `tests/e2e/test_*.py` is auto-discovered by plain `pytest`.
  - `asyncio_mode = "auto"`, `asyncio_default_fixture_loop_scope = "function"`.
- **Ruff:**
  - `line-length = 100`, `target-version = "py311"`
  - `select = ["E", "F", "I", "UP", "B", "SIM", "PIE", "RUF"]`, `ignore = ["E501"]`
  - CI runs `ruff check src tests`, so all code in `tests/e2e/` **must pass `ruff check src tests` cleanly**.
- **Critical Pytest Collision Guard:**
  - Because `tests/` has no `__init__.py`, **never** give a file in `tests/e2e/` the same basename as a file in `tests/` (e.g., avoid `tests/e2e/test_updater.py` or `tests/e2e/test_api.py`), or pytest will error with `import file mismatch`. Use distinct basenames (e.g. `test_e2e_*.py` or `test_tier1_*.py`) and place shared E2E fixtures in `tests/e2e/conftest.py`.

---

## 5. Runtime Environment Capabilities (Verified Read-Only)

### Python Virtualenv (`.venv/bin/python`)
- **Python:** `3.11.2` (`/home/antigravity/gphotos-to-synology/.venv/bin/python`)
- **Installed Packages:**
  - `pytest`: `9.2.0`
  - `pytest-asyncio`: `1.5.0`
  - `ruff`: `0.17.6`
  - `httpx`: `0.28.1`
  - `fastapi`: `0.165.0`
  - `uvicorn`: `0.54.0`
  - `pillow`: `12.3.0` *(Note: M1 will constrain to `>=11.1.0,<12.3.0` → `12.2.0`)*
  - `pillow-heif`: **NOT INSTALLED YET** *(M1 will install `>=1.0.0,<1.1.0` → `1.0.0`)*
  - `piexif`: `1.1.3`
  - `pydantic`: `2.14.4`
  - `pydantic-settings`: `2.16.3`

### System CLI Tools in PATH
- `python3`: `/usr/bin/python3` (`3.11.2`, stdlib-only — matches DSM CGI runtime for `proxy.cgi` / `oauth_callback.cgi` subprocess tests)
- `node`: `/usr/local/bin/node` (`v24.22.3` — available for `node --check` and JS XSS/harness E2E tests)
- `bash`: `/usr/bin/bash` (`5.2.15`)
- `sh`: `/usr/bin/sh` (`dash`, strict POSIX sh — ideal for testing `#!/bin/sh` SPK lifecycle scripts and catching bashisms)
- `tar`: `/usr/bin/tar` (`GNU tar 1.34`)
- `gzip`: `/usr/bin/gzip` (`1.12`)
- `curl`: `/usr/bin/curl` (`7.88.1`)
- `uv`: `/usr/local/bin/uv` (`0.13.8`)
- `git`: `/usr/bin/git` (`2.39.5`)
- `exiftool`: **NOT FOUND**

### Key Implications for E2E Fixtures
1. **HEIC Fixtures (G10):** `pillow-heif` is not installed yet at baseline (`dfcf34f`), and M1 will add it. In E2E fixtures:
   - Provide a helper that registers `pillow_heif` (if importable) to generate genuine `.heic` test images on the fly; if `pillow_heif` is absent at baseline, the G10 E2E test itself should fail clearly (showing the red-before-fix state until M1/M2 land) rather than crashing `conftest.py` import for unrelated tests!
   - Also support a corrupt/undecodable `.heic` fixture (invalid bytes with `.heic` extension) to test the scanner's stable `visual_status='undecodable'` skip path.
2. **ExifTool Fixtures:** System `exiftool` is absent, so:
   - JPEG metadata tests exercise the native `piexif` path directly (matching SPK runtime).
   - Non-JPEG warning tests verify explicit warning when `exiftool` is unavailable.
   - Any ExifTool-present test can inject a small executable fake `exiftool` shim script in `tmp_path` via `exiftool_path`.
3. **SPK Script Execution:** Use `/bin/sh` (`dash`) to execute extracted `start-stop-status`, `preupgrade`, and `preuninst` scripts in `tmp_path` with mocked `SYNOPKG_*` env vars so POSIX compatibility is verified end-to-end.

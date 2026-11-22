# Domain 3 Survey Handoff — SPK Packaging, Lifecycle Scripts, Update Channel, CI/CD, Docker, Dependencies, Tooling & Git/GitHub State

**Summary**: Verified every packaging, updater, CI/CD, Docker, dependency, and environment detail in `/home/antigravity/gphotos-to-synology` (HEAD `dfcf34f`). Uncovered a **critical `manylinux2014` wheel constraint** (`pillow-heif>=1.1.0` and `pillow>=12.3.0` both dropped glibc-2.17 `manylinux2014` wheels — pinning `pillow-heif>=1.0.0,<1.1.0` and `pillow>=11.1.0,<12.3.0` is mandatory for `uv.lock` + `--only-binary :all:` CI builds), resolved all exact GitHub Action commit SHAs and Docker image digests, verified a minor discrepancy in Bingo's review (`ui/texts/enu/strings` *is* packaged), and mapped the complete `v1.2.0` / `1.2.0-0001` packaging, lifecycle, and release plan.

---

## 1. Observation

### 1.1 Complete Domain 3 File Inventory

| File | Lines | Role / Key Notes |
|---|---|---|
| `pyproject.toml` | 55 | Package metadata (`version = "1.0.0"`, line 7), runtime deps (lines 13–25), `dev` extras (lines 28–32), `ruff`/`pytest` config. No `uv.lock` exists. |
| `src/gphotos_synology/__init__.py` | 4 | `__version__ = "1.0.0"` (line 3). |
| `src/gphotos_synology/updater.py` | 204 | GitHub commit/release update checker (`UpdateService`) and Watchtower HTTP trigger. |
| `synology-spk/build_spk.py` | 363 | DSM 7 `.spk` generator (`VERSION = "1.1.0-0003"`, line 27), icon renderer, `start-stop-status` template, lifecycle scripts, tar/gzip packer. |
| `synology-spk/entrypoint_bin.py` | 7 | PyInstaller entrypoint calling `gphotos_synology.main:cli`. |
| `synology-spk/ui/GPhotosMigrator.js` | 69 | Ext JS 3 DSM window loading `/webman/3rdparty/gphotos-migrator/proxy.cgi`. |
| `synology-spk/ui/config` | 25 | DSM desktop app registration (`allUsers: true`, `texts: "texts"`). |
| `synology-spk/ui/index.conf` | 8 | DSM help/search index config (`stringset: "texts"`). |
| `synology-spk/ui/texts/enu/strings` | 4 | Tracked in git (`git ls-files synology-spk/` confirmed) and packaged into `package.tgz`. |
| `synology-spk/ui/proxy.cgi` | 193 | DSM-authenticated CGI reverse proxy (`?p=...` query routing). |
| `synology-spk/ui/oauth_callback.cgi` | 111 | Query-free OAuth2 redirect CGI delegating to `/api/oauth/google/callback`. |
| `.github/workflows/docker-publish.yml` | 158 | Single workflow: `test`, `build-standalone-spk` (`x86_64`, `arm64`), `publish-docker`. |
| `Dockerfile` | 41 | `FROM python:3.12-slim` (unpinned), installs `libimage-exiftool-perl`, `gosu`, `tzdata`, `ca-certificates`, `curl`; runs `pip install --no-cache-dir .` and binds `0.0.0.0:8765`. |
| `docker-compose.yml` | 43 | `gphotos-migrator` (`8765:8765`, `TZ=America/Los_Angeles`, hardcoded `WATCHTOWER_TOKEN`) + `updater` (`nickfedor/watchtower:latest` with `/var/run/docker.sock`). |
| `docker-entrypoint.sh` | 25 | Adjusts `appgroup`/`appuser` to `PUID`/`PGID`, `chown -R` app dirs, drops privileges via `exec gosu`. |
| `.env.example` | 77 | `TZ=UTC` (line 8), `SYNOLOGY_URL=http://host.docker.internal:5000` (line 55), `SYNOLOGY_VERIFY_SSL=false` (line 59). |
| `.gitignore` | 14 | Ignores `.venv/`, `dist/`, `build/`, `.env`, `data/`, `*.db*`, caches. **Does NOT ignore `.agents/`**. |
| `.dockerignore` | 11 | Ignores `.git`, `.venv`, `tests`, `.env`, `data`, caches. **Does NOT ignore `.agents/` or `dist/`/`build/`**. |
| `tests/test_updater.py` | 91 | 2 tests (SHA mismatch/match happy path, Watchtower POST). No downgrade test, no release-error test, no checksum test, no SPK/packaging/CGI tests anywhere in `tests/`. |

### 1.2 G7 — Update Channel Integrity, Reproducibility & Version Drift

1. **Four conflicting version strings**:
   - `pyproject.toml:7` → `version = "1.0.0"`
   - `src/gphotos_synology/__init__.py:3` → `__version__ = "1.0.0"`
   - `synology-spk/build_spk.py:27` → `VERSION = "1.1.0-0003"` (history via `git log`: `1.0.0-0001` → `1.1.0-0001` → `1.1.0-0002` → `1.1.0-0003`)
   - GitHub Release tag `v1.0.0` titled `"v1.1.0 — All-in-One Native Synology DSM 7 App (Zero Docker / Zero YAML)"` (`.github/workflows/docker-publish.yml:107-111`, verified via `gh release view v1.0.0`).
2. **`src/gphotos_synology/updater.py` defects**:
   - **Downgrade-as-update bug** (lines 160–164):
     ```python
     if full_latest_sha and current_sha and current_sha != "dev":
         status.update_available = not full_latest_sha.lower().startswith(current_sha.lower())
     ```
     Never compares `latest_version` against `current_version`. Any SHA difference — including running a newer commit/release than `main` or a local build — sets `update_available = True`.
   - **Swallowed release-check errors** (lines 136–158): `except Exception: pass` on line 157–158 silently discards HTTP/JSON/network errors from `/releases/latest` (and non-200 status codes like 403 rate-limit or 404 are silently ignored).
   - **No checksum asset/digest handling** (lines 39, 103, 144–156): `UpdateStatus` has `spk_download_url` only; no `spk_sha256` or `checksums_url` field, and asset scan ignores `.sha256` / `SHA256SUMS.txt`.
   - **State-changing GET fallback on Watchtower** (lines 193–195):
     ```python
     resp = client.post(f"{wt_url}/v1/update", params={"async": "1"}, headers=headers)
     if resp.status_code == 405:
         resp = client.get(f"{wt_url}/v1/update", headers=headers)
     ```
   - **Frontend consumer** (`src/gphotos_synology/web/static/app.js:499-524`): displays `v${u.current_version} · ${u.current_sha}` and `${u.current_sha} → ${u.latest_sha}${msg}`; links `#link-download-spk` to `u.spk_download_url || u.release_url`.
3. **`.github/workflows/docker-publish.yml` defects**:
   - **Overbroad top-level permissions** (lines 9–11): `contents: write` and `packages: write` apply to all jobs including read-only `test` (lines 14–29).
   - **Mutable release clobber on every push to `main`** (lines 101–113):
     ```bash
     if gh release view v1.0.0 >/dev/null 2>&1; then
       gh release upload v1.0.0 "$SPK" --clobber
     ```
     Overwrites the `v1.0.0` release assets silently on every push to `main` (even commit `dfcf34f` adding `Collab.md` review triggered CI run `44961406762` and re-clobbered `v1.0.0`!). No `.sha256` checksums are generated or uploaded.
   - **Unpinned actions & base images**:
     - Actions (lines 18, 21, 46, 120, 123, 126, 129, 137, 146) use mutable major tags (`@v4`, `@v5`, `@v3`, `@v6`).
     - Base images (`docker-publish.yml:39,42`, `Dockerfile:1`, `docker-compose.yml:30`) use mutable tags, not `@sha256:...` digests.
   - **Floating dependency resolution**:
     - `test` job (line 27): `pip install -e ".[dev]"`
     - `build-standalone-spk` job (lines 58–61): installs unpinned `uv`, then `uv pip install --only-binary :all: -r pyproject.toml pyinstaller` (no `uv.lock`, no `--locked`).
     - `Dockerfile` (line 29): `pip install --no-cache-dir .` (no lockfile).
4. **Non-deterministic SPK builds (`synology-spk/build_spk.py`)**:
   - Line 217: `bdate` falls back to `datetime.now(UTC)` when `APP_BUILD_DATE` / `--build-date` is empty; does not honor `SOURCE_DATE_EPOCH`.
   - Lines 251–262: `_normalize_tarinfo` sets `uid=0`, `gid=0`, `uname="root"`, `gname="root"`, and modes, but leaves `ti.mtime` at live filesystem mtimes (confirmed by `tar -tvf dist/gphotos-migrator-dsm7.spk` showing mixed `2026-11-21 20:59`, `21:10`, `21:12` timestamps).
   - Line 266: `tarfile.open(pkg_tgz, "w:gz")` writes a gzip header containing current wall-clock `mtime` instead of `mtime=0` (or `SOURCE_DATE_EPOCH`).
   - Lines 326–336: outer `.spk` tar adds `conf` and `scripts` recursively (`spk.add(target, ...)`) without explicit sorted traversal.

### 1.3 G8 — SPK Lifecycle Scripts, Data Layout & Proxy Secret

1. **All 6 lifecycle hooks are no-ops** (`synology-spk/build_spk.py:316-319`):
   ```python
   noop_script = "#!/bin/sh\nexit 0\n"
   for name in ("preinst", "postinst", "preuninst", "postuninst", "preupgrade", "postupgrade"):
       _write_exec(scripts_dir / name, noop_script)
   ```
2. **All mutable state lives inside `$SYNOPKG_PKGVAR`** (`build_spk.py:107,137-143`):
   - `DATA_DIR="${PKG_VAR}/config"` (`state.db`, `profiles.json`, `runtime_settings.json`, `google_token.json`, `profiles/<id>/...`)
   - `WORK_DIR="${PKG_VAR}/work"` (contains `replaced_duplicates/` and `profiles/<id>/replaced_duplicates/`, per `config.py:110-113` and `profiles.py:139-141`)
   - `INBOX_DIR="${PKG_VAR}/inbox"` (contains unprocessed archives, `processed/`, and per-profile `<id>/`)
   - `LOG_FILE="${PKG_VAR}/gphotos-migrator.log"` and `PID_FILE="${PKG_VAR}/gphotos-migrator.pid"`
3. **Privilege model** (`build_spk.py:310-314`): `conf/privilege` sets `"run-as": "package"`, `"username": "gphotos-migrator"`. Therefore lifecycle scripts and `start-stop-status` run as unprivileged user `gphotos-migrator` (not `root`).
4. **Shared folders writable by `gphotos-migrator`**:
   - Per `README.md:59-61` and `Collab.md:48`, the NAS admin grants `gphotos-migrator` Read/Write permission to `/volume1/photo` (detected as `PHOTOS_DIR`) and optionally `/volume1/homes` (`HOMES_DIR`).
   - Verified in `src/gphotos_synology/synology/scanner.py:19-23,67`: the scanner skips `@eaDir`, `#recycle`, and any dot-prefixed directory (`not d.startswith(".")`). Synology Photos also ignores dot-prefixed hidden directories.
5. **INFO flags** (`build_spk.py:293-301`): `silent_install="yes"`, `silent_upgrade="yes"`, `silent_uninstall="yes"`, `support_conf_folder="yes"`.

### 1.4 Related Minors & Nits Verified in Files

1. **`start-stop-status` PID & log hygiene** (`synology-spk/build_spk.py:123-178`):
   - `is_running()` (lines 123–131) only checks `kill -0 "${pid}"`; does NOT verify `/proc/${pid}/cmdline` contains `gphotos-migrator` (vulnerable to PID reuse on reboot/crash).
   - `start_daemon()` (lines 133–160) has no lock around check-then-start (double-start race).
   - `LOG_FILE` (line 151: `nohup "${BIN}" >> "${LOG_FILE}" 2>&1 &`) appends indefinitely with no size cap or rotation.
   - `stop_daemon()` (lines 162–178) signals `pid` without verifying identity first.
2. **UI-only SPK variant `status`** (`build_spk.py:95-101`):
   ```sh
   case "$1" in
       start|stop|status|log) exit 0 ;;
   ```
   Always exits `0` for `status`, so Package Center displays "Running" even when no backend exists.
3. **`ui/config` `"texts"` stringset — Verification Correction**:
   - Bingo (`Collab.md:111`) claimed: *"`ui/config` references a `texts` stringset that isn't packaged"*.
   - **Direct inspection refutes this**: `synology-spk/ui/texts/enu/strings` (4 lines) **is tracked in git** (`git ls-files synology-spk/` output verified) and **is packaged into `package.tgz`** (verified via `tar -xOf dist/gphotos-migrator-dsm7.spk package.tgz | tar -tzvf -` showing `-rw-r--r-- root/root 123 ... ui/texts/enu/strings`), copied by `shutil.copytree(ROOT / "ui", ui_dst)` at `build_spk.py:228`.
   - Minor note: `ui/config:5-6,18` and `ui/index.conf:3-4` use literal strings (`"Google Photos Migrator"`) rather than stringset key references (`"app:app_name"`), which is valid in DSM 7 (or can be pointed to `app:app_name` / `app:description` to actually use the packaged strings).
4. **Docker hardening & consistency**:
   - `Dockerfile:40`: `CMD ["uvicorn", ..., "--host", "0.0.0.0", "--port", "8765"]` (binds all interfaces inside container).
   - `docker-compose.yml:6-7`: `ports: ["8765:8765"]` publishes on `0.0.0.0` on the host LAN! Should default to `"127.0.0.1:8765:8765"`.
   - `docker-compose.yml:11` vs `.env.example:8`: `TZ=America/Los_Angeles` vs `TZ=UTC` mismatch → unify to `${TZ:-UTC}`.
   - `docker-compose.yml:13,40`: hardcoded `WATCHTOWER_TOKEN=gphotos-internal-update-token` committed in repo.
   - `docker-compose.yml`: no `cap_drop` / `security_opt: [no-new-privileges:true]` on either service despite `updater` mounting `/var/run/docker.sock` and `gphotos-migrator` mounting `/volume1/homes`.
   - `docker-entrypoint.sh:8-24`: starts as root to run `groupmod`/`usermod`/`chown` then `exec gosu`. (Important: if `cap_drop: [ALL]` is added to `gphotos-migrator`, it MUST either `cap_add: [CHOWN, FOWNER, SETGID, SETUID, DAC_OVERRIDE]` for `docker-entrypoint.sh` or drop root via `user:` in compose; `updater` can use `cap_drop: [ALL]` + `no-new-privileges:true` directly.)
   - `.env.example:55,59`: `SYNOLOGY_URL=http://host.docker.internal:5000` and `SYNOLOGY_VERIFY_SSL=false` (should align with G5 defaults).

### 1.5 Critical PyPI Wheel Feasibility Check (`pillow-heif` & `pillow` on `manylinux2014`)

Queried live PyPI JSON API directly (2026-11-21):

| Package | Latest on PyPI | Last version with `manylinux2014` (`manylinux_2_17`) `cp312` `x86_64` + `aarch64` wheels | Newer versions wheel tags |
|---|---|---|---|
| **`pillow-heif`** | `1.8.1` | **`1.0.0`** (`cp39`–`cp313`, `manylinux_2_17_{x86_64,aarch64}.manylinux2014_{x86_64,aarch64}`) | `1.1.0`–`1.8.1`: `manylinux_2_27_x86_64` / `manylinux_2_26_aarch64` ONLY (ZERO `manylinux2014`!) |
| **`pillow`** | `13.0.0` | **`12.2.0`** (`cp312`, `manylinux2014_{x86_64,aarch64}.manylinux_2_17_{x86_64,aarch64}`) | `12.3.0` & `13.0.0`: `manylinux_2_27_{x86_64,aarch64}.manylinux_2_28_{x86_64,aarch64}` ONLY (ZERO `manylinux2014`!) |
| **`pyinstaller`** | `6.22.3` | **`6.22.3`** (`py3-none-manylinux2014_{x86_64,aarch64}`) | Supported |
| `pydantic-core` | `2.50.5` | `2.50.5` (`manylinux2014` x86_64 & aarch64) | Supported |
| `cryptography` | `50.0.2` | `50.0.2` (`manylinux2014` x86_64 & aarch64) | Supported |
| `httptools`, `uvloop`, `watchfiles`, `websockets`, `pyyaml` | latest | All publish `manylinux2014` `cp312` x86_64 & aarch64 | Supported |

Additional `pillow-heif==1.0.0` metadata verified from `https://pypi.org/pypi/pillow-heif/1.0.0/json`:
- `requires_python`: `>=3.9`
- `requires_dist`: `pillow>=11.1.0` (no upper cap — compatible with `pillow==12.2.0`)
- Ships `cp311` and `cp312` `manylinux_2_17` / `manylinux2014` wheels for both `x86_64` and `aarch64` (so works in local Python 3.11 `.venv` AND CI Python 3.12 `manylinux2014` containers!).

### 1.6 Resolved Pinning References (GitHub Actions SHAs & Container Image Digests)

Resolved directly via `gh api` (all `"type": "commit"`) and registry `HEAD` manifest queries:

**GitHub Actions**:
- `actions/checkout@11d5960a326750d5838078e36cf38b85af677262` (`# v4`)
- `actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065` (`# v5`)
- `docker/setup-qemu-action@c7c53464625b32c7a7e944ae62b3e17d2b600130` (`# v3`)
- `docker/setup-buildx-action@8d2750c68a42422c14e847fe6c8ac0403b4cbd6f` (`# v3`)
- `docker/login-action@c94ce9fb468520275223c153574b00df6fe4bcc9` (`# v3`)
- `docker/metadata-action@c299e40c65443455700f0fdfc63efafe5b349051` (`# v5`)
- `docker/build-push-action@10e90e3645eae34f1e60eeb005ba3a3d33f178e8` (`# v6`)

**Container Images (manifest list / multi-arch digests)**:
- `python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b`
- `nickfedor/watchtower:latest@sha256:617a7d5523280cb73adac4d7977604afbe66b7ea86211f2ae0cbfa2043041d61`
- `quay.io/pypa/manylinux2014_x86_64@sha256:8a79ff36ce155d8f242dc8013d73c5e8bbe937defb15de7d7afb342c1ab537f4`
- `quay.io/pypa/manylinux2014_aarch64@sha256:382236bfd11d86894d9792a6fa5286190b8dd3d075a7032284e123c288711e4f`

### 1.7 Environment, Git & GitHub Readiness

- **Git status**: `main...origin/main` at `dfcf34f`, working tree clean except untracked `?? .agents/`.
- **Git tags**: `v1.0.0` (only existing tag).
- **Git remote**: `origin https://github.com/0denko/gphotos-to-synology.git`.
- **GitHub auth**: `gh auth status` → logged in to `github.com` as `0denko` (`GH_TOKEN`, https protocol).
- **GitHub Releases**: 1 release (`v1.0.0`, Latest, assets `gphotos-migrator-dsm7-arm64.spk` and `gphotos-migrator-dsm7-x86_64.spk`).
- **Last 4 CI runs on `main`**: all `completed/success` (`44961406762`, `44956257493`, `44955485427`, `44954174733`, ~3.5–5.5 min each including native `ubuntu-24.04-arm` runner).
- **Local tools**:
  - `.venv/bin/python`: Python 3.11.2 (note: `.venv` currently has `PIL 12.3.0`, no `pillow-heif`)
  - `.venv/bin/pytest`: pytest 9.2.0 → **20 passed in 1.56s**
  - `.venv/bin/ruff`: ruff 0.17.6 → **All checks passed!**
  - `uv`: 0.13.8 (on PATH — ready to generate `uv.lock`!)
  - `node`: v24.22.3 → **`node --check` passed on `GPhotosMigrator.js` and `app.js`**
  - `gh`: 2.103.3
  - `docker`: **not installed locally** — Docker image and `manylinux2014` PyInstaller/SPK builds must be validated via GitHub Actions CI.
- **Test files (`tests/`)**:
  1. `test_api.py`
  2. `test_existing_nas_dedup.py`
  3. `test_metadata_and_importer.py`
  4. `test_pipeline_e2e.py`
  5. `test_profiles.py`
  6. `test_synology_api_and_security.py`
  7. `test_takeout_matcher.py`
  8. `test_updater.py`
  9. `test_visual_quality_upgrade.py`

---

## 2. Logic Chain

1. **Why `pillow` AND `pillow-heif` upper caps are mandatory before running `uv lock`** (Obs 1.5):
   - CI builds native SPKs inside `quay.io/pypa/manylinux2014_{x86_64,aarch64}` (glibc 2.17) using `--only-binary :all:` to guarantee binary compatibility with DSM 7.0/7.1 (glibc 2.26) and DSM 7.2 (glibc 2.36) without executing untrusted sdist setup scripts.
   - Previously, without a lockfile, `uv pip install` inside `manylinux2014` silently selected `pillow==12.2.0` even though `pillow==12.3.0` was on PyPI.
   - Once we commit `uv.lock` (required by G7), `uv lock` resolves a single version per package across Linux targets. Without `<12.3.0` on `pillow` and `<1.1.0` on `pillow-heif`, `uv lock` would lock `pillow==12.3.0` and `pillow-heif==1.8.1`, BOTH of which lack `manylinux2014` wheels — causing an immediate `--locked --only-binary :all:` failure in CI!
   - Pinning `"pillow>=11.1.0,<12.3.0"` and `"pillow-heif>=1.0.0,<1.1.0"` in `pyproject.toml` locks `pillow==12.2.0` and `pillow-heif==1.0.0`, both verified to have `manylinux2014_x86_64` + `manylinux2014_aarch64` wheels for Python 3.11 & 3.12.
   - Because `pillow_heif` bundles native shared libraries (`libheif`/`libde265`/`libx265`) via auditwheel, PyInstaller in `.github/workflows/docker-publish.yml` should add `--collect-all pillow_heif` and the CI smoke test should verify `pillow_heif` imports inside the compiled bundle.

2. **Why `v1.2.0` / `1.2.0-0001` is the right unified version scheme** (Obs 1.2):
   - Historical SPK versions are `1.0.0-0001`, `1.1.0-0001`, `1.1.0-0002`, `1.1.0-0003`.
   - Setting `pyproject.toml` `version = "1.2.0"`, `__init__.py` `__version__ = "1.2.0"`, SPK `VERSION = "1.2.0-0001"` (derived from `__version__` + `SPK_BUILD_NUM = "0001"`), and Git tag / GitHub Release `v1.2.0` makes all 4 version surfaces agree, guarantees Synology Package Center treats `1.2.0-0001` as a strict upgrade over `1.1.0-0003`, and lets `updater.py` compare semver versions cleanly.

3. **How to fix G7 cleanly without breaking existing users** (Obs 1.2, 1.6):
   - **Updater comparison**: Parse `current_version` and `latest_version` (from `/releases/latest` `tag_name`) as comparable version tuples (handling `v1.2.0` and `1.2.0-0001`). Set `update_available = True` **only when `parse_ver(latest_version) > parse_ver(current_version)`**. Never compare bare SHAs for downgrade-prone inequality (or only fall back to SHA comparison when running a Docker `:latest` build whose `latest_version == current_version` AND commit timestamp is strictly newer than `build_date`, never downgrading).
   - **Release-check error surfacing**: Record non-200 or exception from `/releases/latest` in `status.error` (or `release_error` warning field) and log at `WARNING` instead of `except Exception: pass`.
   - **Checksums**: In CI, compute `sha256sum` for each built `.spk` and upload `<spk>.sha256` + `SHA256SUMS.txt` alongside the `.spk` assets. In `updater.py`, also extract the matching `.sha256` asset URL (`spk_sha256_url`) and optionally fetch/expose `spk_sha256` in `UpdateStatus` so users can verify downloads before Manual Install.
   - **Release workflow separation**:
     - On `push` to `main`: run `test`, build & smoke-test SPKs (uploading them as GitHub Actions build artifacts only — **never** clobbering a published Release!), and optionally publish Docker `:sha` (or only publish `:latest` + `:1.2.0` on tags/releases).
     - On `push` to tags `v*` (e.g., `v1.2.0`): build both arch SPKs with deterministic `SOURCE_DATE_EPOCH` (from `git log -1 --format=%ct`), generate `.sha256` files, and have a dedicated `publish-release` job (`needs: [build-standalone-spk]`, `permissions: {contents: write}`) download both arch artifacts and create/publish release `${GITHUB_REF_NAME}` with both `.spk` files, `.sha256` files, and `SHA256SUMS.txt` (avoiding race conditions between parallel `x86_64` and `arm64` jobs creating the same release!).
   - **Watchtower trigger**: Remove the `if resp.status_code == 405: resp = client.get(...)` fallback in `updater.py:194-195` (POST-only).
   - **Deterministic SPK builds**: In `build_spk.py`, resolve `build_epoch` from `--source-date-epoch` / `SOURCE_DATE_EPOCH` / parsed `build_date` (fallback `0` or git commit epoch), set `ti.mtime = build_epoch` in `_normalize_tarinfo`, write `package.tgz` using `gzip.GzipFile(filename="", mode="wb", fileobj=f, mtime=0)` + `tarfile.open(..., format=tarfile.GNU_FORMAT)`, and sort all recursive members in the outer `.spk` tar as well.

4. **How to design G8 + C1 shared secret for DSM 7 `run-as: package`** (Obs 1.3):
   - **Shared proxy secret (C1)**:
     - `start-stop-status` ensures `${PKG_VAR}/proxy.secret` exists with `umask 077` (mode `0600`, owned by `gphotos-migrator`, 64 hex chars from `/dev/urandom`), and exports `PROXY_SECRET_FILE="${PKG_VAR}/proxy.secret"` (avoiding putting the secret in process env argv).
     - `proxy.cgi` and `oauth_callback.cgi` read the secret from `GPHOTOS_PROXY_SECRET` env var (for unit tests) or `GPHOTOS_PROXY_SECRET_FILE` (default `/var/packages/gphotos-migrator/var/proxy.secret`), and inject it as `X-Proxy-Secret` header on upstream loopback calls.
   - **Upgrade snapshot (`preupgrade` / `postupgrade`)**:
     - DSM stops the daemon before calling `preupgrade` and preserves `${PKG_VAR}` across upgrades.
     - `preupgrade` copies `${PKG_VAR}/config` (DB + settings + profiles) into `${PKG_VAR}/backups/preupgrade-${OLD_VER:-unknown}-${TS}` (mode `0700`) and prunes snapshots older than the newest 3.
   - **Uninstall preservation (`preuninst`)**:
     - **Critical DSM 7 detail**: DSM executes `preuninst` on BOTH upgrade and uninstall; guard with `[ "${SYNOPKG_PKG_STATUS}" = "UNINSTALL" ] || exit 0` so upgrades don't duplicate gigabytes of quarantine/inbox!
     - On `UNINSTALL`, if `${PKG_VAR}/config`, `${PKG_VAR}/work/replaced_duplicates`, `${PKG_VAR}/work/profiles/*/replaced_duplicates`, or `${PKG_VAR}/inbox` have data, copy them (excluding `proxy.secret`) into a timestamped backup folder outside `${PKG_VAR}` at the first writable location:
       1. `${GPHOTOS_BACKUP_DIR}` (if set and writable)
       2. `${PHOTOS_DIR}/.gphotos-migrator-backup/${TS}` (detected via `detect_dir photo` — writable because the user granted `gphotos-migrator` R/W on `photo`, AND dot-prefixed so Synology Photos and `scanner.py` both ignore it!).
     - Set `chmod 700` on the backup directory and write a short `README_RESTORE.txt`.
     - Also allow configuring `QUARANTINE_DIR` independently of `WORK_DIR` in `config.py` (defaulting to `work_dir / "replaced_duplicates"` for backwards compatibility) so operators/tests can place quarantine outside ephemeral work dirs.

5. **Why `.agents/` hygiene matters immediately** (Obs 1.1, 1.7):
   - `.agents/` is currently untracked (`?? .agents/`). Neither `.gitignore` nor `.dockerignore` excludes `.agents/`. Implementers must NEVER stage/commit `.agents/` (always stage explicit paths, and add `.agents/` to `.dockerignore` so local Docker builds never copy agent metadata into images; do NOT add `.agents/` to `.gitignore` unless desired or keep it clean by never staging it — actually adding `.agents/` to `.dockerignore` is safe, and `.git/info/exclude` is local-only if needed, though never committing `.agents/` is the strict rule).

---

## 3. Caveats

- **No local Docker engine**: `docker` is not installed in this VM, so `manylinux2014` PyInstaller compilation, `pillow_heif` runtime inside `manylinux2014`, and `Dockerfile` build cannot be executed locally — they will be verified in GitHub Actions CI after pushing.
- **DSM 7 CGI user & `proxy.secret` permissions**: On standard Synology DSM 7, `synoscgi` executes `/webman/3rdparty/*/*.cgi` as `root` (required for `authenticate.cgi`), which can read `0600` files owned by `gphotos-migrator`. This is inferred from DSM 7 behavior and cannot be tested locally without a physical Synology NAS; `proxy.cgi` should log/return a clear diagnostic if `proxy.secret` exists but raises `PermissionError`.
- **DSM 7 `preuninst` writable target**: If a user uninstalls before ever granting `gphotos-migrator` Read/Write permission on `photo` (and no other shared folder is writable), `preuninst` cannot write outside `${PKG_VAR}`; it should fail softly (`exit 0` after logging a warning) so Package Center doesn't wedge uninstall in a broken state.
- **Unpinned `uv` inside CI `manylinux2014` container**: Line 58 of `docker-publish.yml` runs `/opt/python/cp312-cp312/bin/pip install --no-cache-dir uv`. For full reproducibility, pin `uv==0.13.8` (or `0.13.9`, matching the version used to generate `uv.lock`).
- **Existing `v1.0.0` GitHub Release**: Once `v1.2.0` is published as the new Latest release, `v1.0.0` remains as historical release; update `README.md` and `Collab.md` download links to point to `releases/latest` / `v1.2.0`.

---

## 4. Conclusion

Domain 3 is fully mapped and ready for implementation with the following concrete blueprint:

1. **Dependencies & Lockfile (`pyproject.toml`, `uv.lock`)**:
   - Bump `version = "1.2.0"` (`pyproject.toml:7`) and `__version__ = "1.2.0"` (`src/gphotos_synology/__init__.py:3`).
   - Add `"pillow-heif>=1.0.0,<1.1.0"` and tighten `"pillow>=11.1.0,<12.3.0"` (CRITICAL for `manylinux2014` binary wheels!).
   - Add optional extra `spk = ["pyinstaller>=6.10.0,<7.0.0"]` (or lock `pyinstaller` in a dependency group) so `uv.lock` locks runtime + `dev` + `spk` dependencies cleanly.
   - Generate and commit `uv.lock` with `uv lock` (and update `.venv` to match).
2. **Updater (`src/gphotos_synology/updater.py`, `web/static/app.js`, `tests/test_updater.py`)**:
   - Implement strict semantic/SPK version comparison so `update_available` is `True` **only** when `latest_version > current_version` (never downgrade).
   - Surface `/releases/latest` fetch errors instead of swallowing with `except Exception: pass`.
   - Extract matching `.spk` asset and `.sha256` checksum asset URL (support both `gphotos-migrator-dsm7-{arch}.spk` and versioned `gphotos-migrator-dsm7-v{ver}-{arch}.spk`), expose `spk_sha256_url` / `spk_sha256`.
   - Remove HTTP 405 GET fallback in `trigger_self_update`.
3. **SPK Builder & Lifecycle (`synology-spk/build_spk.py`, `synology-spk/ui/proxy.cgi`, `synology-spk/ui/oauth_callback.cgi`)**:
   - Derive SPK version from `__version__` + `-0001` → `"1.2.0-0001"` (overridable via `--version`).
   - Deterministic builds: support `SOURCE_DATE_EPOCH` / `--source-date-epoch`, normalize `ti.mtime`, write `package.tgz` with `gzip.GzipFile(..., mtime=0)`, sort all tar entries in both `package.tgz` and outer `.spk`.
   - Hardened `start-stop-status`: verify `/proc/${pid}/cmdline` matches `gphotos-migrator`, add lock directory (`mkdir` atomic lock) to prevent double-start races, rotate `${LOG_FILE}` when >10 MB (keep 3), generate `${PKG_VAR}/proxy.secret` (`umask 077`, 64 hex chars) and export `PROXY_SECRET_FILE="${PKG_VAR}/proxy.secret"`.
   - Fix UI-only `status` script to probe `127.0.0.1:8765/api/health` (exit 0 if healthy, exit 3 otherwise).
   - Implement real `preupgrade` (snapshot `${PKG_VAR}/config` into `${PKG_VAR}/backups/preupgrade-<ver>-<ts>`, keep last 3) and `preuninst` (when `SYNOPKG_PKG_STATUS=UNINSTALL`, preserve config + quarantine + inbox excluding `proxy.secret` to `${PHOTOS_DIR}/.gphotos-migrator-backup/<ts>` with `chmod 700` + `README_RESTORE.txt`).
   - Optionally point `ui/config` `title`/`desc` to `app:app_name` / `app:description` (noting `ui/texts/enu/strings` is already packaged).
4. **CI/CD (`docker-publish.yml`)**:
   - Top-level `permissions: {}`; per-job minimal permissions (`test`: `contents: read`; `build-standalone-spk`: `contents: read`; new `publish-release` job on `startsWith(github.ref, 'refs/tags/v')`: `contents: write`; `publish-docker`: `contents: read`, `packages: write`).
   - Pin all 7 GitHub Actions to commit SHAs and pin `manylinux2014_{x86_64,aarch64}` images by `@sha256:` digest (Section 1.6).
   - Install dependencies with `uv sync --locked` / `uv export --frozen --no-hashes` + `uv pip install --only-binary :all:` (pinning `uv==0.13.8`), add `--collect-all pillow_heif` to PyInstaller, and verify `pillow_heif` + `proxy.cgi` / `/api/health` in the smoke test.
   - Pass `SOURCE_DATE_EPOCH=$(git log -1 --format=%ct)` and `--build-date` to `build_spk.py`.
   - Generate `.sha256` files and `SHA256SUMS.txt`; upload to a versioned GitHub Release **only on `v*` tags** (never `--clobber` `v1.0.0` on `main` pushes).
5. **Docker Hardening (`Dockerfile`, `docker-compose.yml`, `.env.example`, `.dockerignore`)**:
   - Pin `Dockerfile` `FROM python:3.12-slim@sha256:...`; install via `uv` + `uv.lock` (`--locked`).
   - `docker-compose.yml`: pin `nickfedor/watchtower` by `@sha256:...`, default ports to `"127.0.0.1:8765:8765"`, unify `TZ=${TZ:-UTC}`, remove hardcoded `WATCHTOWER_TOKEN` (use `${WATCHTOWER_TOKEN:-}`), add `security_opt: [no-new-privileges:true]` and `cap_drop: [ALL]` (with minimal `cap_add: [CHOWN, FOWNER, SETGID, SETUID, DAC_OVERRIDE]` on `gphotos-migrator` for `docker-entrypoint.sh`).
   - `.env.example`: align `TZ=UTC`, `SYNOLOGY_VERIFY_SSL=true`, safer `SYNOLOGY_URL`, and document `APP_API_TOKEN` / `WATCHTOWER_TOKEN` generation.
   - `.dockerignore`: add `.agents`, `dist`, `build`.
6. **New Packaging & Updater Regression Tests**:
   - `tests/test_updater.py`: add downgrade-never-triggers-update test, release-error surfacing test, `.sha256` asset parsing test, Watchtower 405 no-GET-fallback test.
   - `tests/test_spk_packaging.py` (new): test deterministic reproducible SPK build (two builds with same `SOURCE_DATE_EPOCH` produce byte-identical SHA-256!), verify `INFO` version `1.2.0-0001`, verify `package.tgz` contains `ui/texts/enu/strings` & `ui/proxy.cgi` & `ui/oauth_callback.cgi`, verify `preupgrade` and `preuninst` scripts behave correctly in a temp dir (`UNINSTALL` backs up config+quarantine+inbox and excludes `proxy.secret`; `UPGRADE` skips `preuninst` backup and runs `preupgrade` snapshot), verify `start-stop-status` PID validation & log rotation.

---

## 5. Verification Method

Run the following read-only checks to verify every observation in this report:

```bash
cd /home/antigravity/gphotos-to-synology

# 1. Verify git, tags, and GitHub release state
git status -sb
git log -n 10 --oneline
git tag -l -n1
gh release view v1.0.0

# 2. Verify baseline tests, linter, and JS syntax
.venv/bin/pytest -q
.venv/bin/ruff check src tests
node --check synology-spk/ui/GPhotosMigrator.js
node --check src/gphotos_synology/web/static/app.js

# 3. Verify ui/texts/enu/strings IS tracked and packaged
git ls-files synology-spk/
tar -xOf dist/gphotos-migrator-dsm7.spk package.tgz | tar -tzvf - | grep texts

# 4. Verify pillow-heif & pillow manylinux2014 wheel cutoffs on PyPI
.venv/bin/python -c '
import json, urllib.request
for pkg, ver in [("pillow-heif", "1.0.0"), ("pillow-heif", "1.1.0"), ("pillow", "12.2.0"), ("pillow", "12.3.0")]:
    d = json.loads(urllib.request.urlopen(f"https://pypi.org/pypi/{pkg}/{ver}/json", timeout=15).read())
    m2014 = [f["filename"] for f in d["urls"] if "cp312" in f["filename"] and ("manylinux2014" in f["filename"] or "manylinux_2_17" in f["filename"])]
    print(pkg, ver, "manylinux2014 cp312 wheels:", len(m2014))
'
```

**Invalidation conditions**:
- If `pillow-heif` or `pillow` later backports `manylinux2014` wheels, or if Synology DSM minimum supported version is raised above glibc 2.27 (DSM 7.2+ only), the wheel pin ceiling can be revisited.
- If GitHub releases or upstream base image tags are updated before implementation merges, re-resolve digests via registry `HEAD` / `gh api` before committing.

# Project: gphotos-to-synology v1.2.0 Remediation & Release

**Repository:** `0denko/gphotos-to-synology` (`/home/antigravity/gphotos-to-synology`)  
**Developer / Maintainer:** Oleksandr Danylchenko  
**Baseline HEAD:** `dfcf34f` (`v1.1.0-0003`)  
**Target Release:** `v1.2.0` (Python `1.2.0`, SPK `1.2.0-0001`, Git Tag & GitHub Release `v1.2.0`)  
**Hard Constraints:** All-in-one native Synology DSM 7 `.spk` (zero Docker / zero YAML for primary NAS install, UI inside DSM via `proxy.cgi` / `oauth_callback.cgi`, developer = Oleksandr Danylchenko). NEVER commit `.agents/`, `PROJECT.md`, `TEST_INFRA.md`, or `TEST_READY.md` to git.

---

## Architecture

```
DSM 7 Browser (Ext JS GPhotosMigrator.js)
  │  Cookie + X-Requested-With + X-Profile-Id
  ▼
synology-spk/ui/proxy.cgi & oauth_callback.cgi
  │  authenticate.cgi (fail-closed) → X-Proxy-Secret + X-Proxy-Mode: cgi
  │  + X-DSM-User + X-DSM-Admin (1|0) + X-Requested-With
  ▼
FastAPI App (main.py → web/security.py → web/routes.py)   [127.0.0.1:8765 in SPK]
  ├── ProfileRegistry (profiles.py) ──► per-profile ConfigManager (config.py)
  │                                  ──► per-profile StateDB (database.py, WAL SQLite)
  │                                  ──► per-profile MigrationPipeline (worker/pipeline.py)
  ├── BackgroundScheduler (worker/scheduler.py)
  ├── UpdateService (updater.py) ──► GitHub Releases (semver-only + .sha256)
  └── Pipeline Dependencies:
        ├── Sources: LocalInboxScanner (sources/inbox.py), GoogleDriveClient (sources/gdrive.py)
        ├── Takeout: extractor.py (limits), matcher.py (strict), metadata.py (piexif/ExifTool), dedup.py (HEIC + perceptual)
        ├── Synology: ExistingLibraryScanner (scanner.py), SynologyFolderImporter (importer.py), SynologyPhotosClient (photos_api.py)
        └── Alerts: Notifier (notifications.py, sync + async + throttled)
```

---

## Code Layout

```
src/gphotos_synology/
├── __init__.py                 # __version__ = "1.2.0"
├── config.py                   # Settings, ConfigManager, lazy inheritance, secret & URL safety
├── database.py                 # StateDB (WAL SQLite, additive migrations, dhash buckets, upgrade journal)
├── main.py                     # AppContext, create_app(), security middleware wiring, cli()
├── notifications.py            # Notifier (sync send() + async send_async())
├── profiles.py                 # ProfileRegistry, path containment, delete cleanup, corrupt-DB resilience
├── updater.py                  # UpdateService (semver non-downgrading, error surfacing, .sha256, POST-only Watchtower)
├── sources/
│   ├── gdrive.py               # GoogleDriveClient (MD5 verify, 0o600 atomic token, pagination, error surface, resume)
│   └── inbox.py                # LocalInboxScanner (symlink skip, size+mtime stability, archive readability)
├── synology/
│   ├── importer.py             # SynologyFolderImporter (sanitization, collision-safe move)
│   ├── photos_api.py           # SynologyPhotosClient (POST _sid, code-119 relogin, cache invalidation, root scoping)
│   └── scanner.py              # ExistingLibraryScanner (read-only, skips @eaDir/#recycle/dot-dirs & stable undecodable)
├── takeout/
│   ├── dedup.py                # pillow-heif opener, visual_status, perceptual corroboration, sharpness-guarded size rule
│   ├── extractor.py            # Decompression limits (entries/bytes/ratio/streaming), ZipSlip, stale .tmp cleanup
│   ├── matcher.py              # Strict (N) duplicate & unique title/prefix matching, sorted case-safe index
│   └── metadata.py             # piexif.load skip-on-error, DMS 60s carry, OffsetTimeOriginal, ASCII/UserComment, float ts
├── web/
│   ├── security.py             # [NEW] C1 auth, C2 DSM isolation/admin, G6 CSRF, trusted proxy headers
│   ├── routes.py               # API routes, G3 server-side OAuth state store, G4 notice, C2 enforcement
│   └── static/
│       ├── app.js              # escapeHtml quote fix, apiFetch spread fix + X-Requested-With, non-admin UX, checksum UI
│       ├── index.html          # Scope escalation / disconnect / HTTPS SSL notices
│       └── styles.css          # Dashboard styles
└── worker/
    ├── pipeline.py             # G1 completed_with_errors + finalize guard, G2 crash-atomic upgrade journal/rollback, _op_lock
    └── scheduler.py            # Pause next_run_at=None, manual last_run_at, cooperative stop & .tmp cleanup

synology-spk/
├── build_spk.py                # Deterministic SPK builder (1.2.0-0001), proxy.secret, PID/log hardening, preupgrade/preuninst
├── entrypoint_bin.py           # PyInstaller entrypoint
└── ui/
    ├── proxy.cgi               # DSM auth, X-Proxy-Secret, X-DSM-Admin, X-Requested-With, 1MiB cap, 503 API offline, CRLF
    ├── oauth_callback.cgi      # DSM auth, X-Proxy-Secret, X-DSM-Admin, query param allowlist, CRLF
    ├── GPhotosMigrator.js      # Ext JS 3 DSM desktop window
    ├── config                  # DSM app registration
    ├── index.conf              # DSM index config
    └── texts/enu/strings       # DSM stringset

tests/                          # Unit & integration tests (M1–M5)
tests/e2e/                      # [NEW] Opaque-box E2E test suite (Tiers 1–4, E2E Track) + Tier 5 adversarial (M6)
```

---

## Feature Inventory

Every finding from Bingo's `Collab.md` review (2 Criticals, 14 Majors, all Minors/Nits, test gaps, CI/Release `v1.2.0`, `Collab.md` handoff) is assigned to a milestone (none unassigned).

| # | Feature | Description | Milestone | Source |
|---|---|---|---|---|
| 1 | **C1** | App-level API auth middleware (`X-Proxy-Secret` / `API_TOKEN` / session cookie; `/api/health`, `/`, `/static/*` exempt) + Docker loopback host port (`127.0.0.1:8765:8765`) | M1 (config), M3 (middleware/CGIs), M5 (SPK/Docker) | Collab.md C1 |
| 2 | **C2** | Server-side DSM user profile isolation (`X-DSM-User` + `X-DSM-Admin` enforcement across profiles, global actions, updates, OAuth) | M3 | Collab.md C2 |
| 3 | **G1** | Failed files mark archive `completed_with_errors` (`completed=False`), gate `finalize_archive` on `failed == 0`, retry on subsequent cycles | M1 (DB stats), M4 (pipeline) | Collab.md G1 |
| 4 | **G2** | Crash-atomic quality-upgrade swap: write-ahead `in_progress` journal (with `old_rel_path`/`old_taken_at`/GPS), rollback on error, startup recovery, missing-file repair, fresh `stat()`/re-fingerprint check | M1 (DB schema/methods), M4 (pipeline) | Collab.md G2 |
| 5 | **G3** | OAuth callback fail-closed server-side state store (`hmac.compare_digest`, TTL 600s, profile from store, one-time `delete_state`, bound `redirect_uri`, URL-encoded error) | M1 (`delete_state`), M3 (routes) | Collab.md G3 |
| 6 | **G4** | Prevent `synology_url` change from exfiltrating stored `synology_password` (clear password if URL changes without fresh plaintext password + validate URL scheme) | M1 (config), M3 (routes/UI) | Collab.md G4 |
| 7 | **G5** | Default `synology_verify_ssl=True`, HTTPS default `synology_url` (`https://127.0.0.1:5001`), `app_host="127.0.0.1"`, optional CA cert, updated `.env.example`/UI | M1 (config), M3 (UI), M5 (`.env.example`) | Collab.md G5 |
| 8 | **G6** | CSRF defense requiring `X-Requested-With` on mutating `/api/*` + `proxy.cgi` forwarding + `app.js` `apiFetch` spread bug fix | M3 | Collab.md G6 |
| 9 | **G7** | Versioned release `v1.2.0` (`1.2.0-0001`), `.sha256` + `SHA256SUMS.txt`, non-downgrading semver updater, release error surfacing, committed `uv.lock` (`--locked`), deterministic SPK timestamps, pinned CI actions & container digests, minimal CI permissions | M1 (version + `uv.lock`), M5 (updater/build_spk/CI/Docker), M6 (publish `v1.2.0`) | Collab.md G7 |
| 10 | **G8** | SPK lifecycle hooks: `preupgrade` config snapshot (keep 3), `preuninst` (`SYNOPKG_PKG_STATUS=UNINSTALL`) backup of config/quarantine/inbox (excluding `proxy.secret`) outside `PKG_VAR` to `${PHOTOS_DIR}/.gphotos-migrator-backup/<ts>`, optional `QUARANTINE_DIR` config | M1 (config), M5 (`build_spk.py`) | Collab.md G8 |
| 11 | **G9** | Strict sidecar matcher: drop unnumbered fallback for `(N)` duplicates, require unique candidate for title/46-char prefix, stem-only after title, sorted case-safe index | M2 | Collab.md G9 |
| 12 | **G10** | HEIC/HEIF visual fingerprinting (`pillow-heif>=1.0.0,<1.1.0` + `pillow>=11.1.0,<12.3.0` for `manylinux2014` wheels + `--collect-all pillow_heif`), `WARNING` log & persisted `visual_status='undecodable'` so scanner skips stably | M1 (deps/DB), M2 (dedup/scanner), M5 (CI PyInstaller) | Collab.md G10 |
| 13 | **G11** | Tighter perceptual dedup corroboration (unknown dims fail looser branches, require ts/stem corroboration for `dist > 4`, `.astimezone(UTC)` fix), distinct `perceptual-dedup` log event, SQL dHash bucket table (`media_dhash_buckets`) | M1 (DB buckets), M2 (dedup), M4 (pipeline logging) | Collab.md G11 |
| 14 | **G12** | Extraction decompression limits (entry count, pre-flight uncompressed bytes, streaming byte cap derived from free space, compression ratio) + per-entry skip-and-log option | M2 (extractor), M4 (pipeline integration) | Collab.md G12 |
| 15 | **G13** | `piexif.load` failure skips EXIF write (mtime-only) instead of replacing EXIF with empty dict | M2 | Collab.md G13 |
| 16 | **G14** | Personal & shared `custom_subfolder` resolved path containment check (`_resolve_contained_subfolder`) | M3 | Collab.md G14 |
| 17 | **Minor: Album retries** | Do not burn `retry_count` while waiting for Synology Photos to index (`status='pending'`) | M1 (DB), M4 (pipeline) | Collab.md Minor |
| 18 | **Minor: `delete_profile` & collisions** | Delete profile DB/token/work/inbox on disk; reserve `"processed"`/`"profiles"`; reject duplicate `photos_dir`; corrupt-DB startup resilience | M3 | Collab.md Minor |
| 19 | **Minor: Lazy OAuth inheritance** | Dynamic fallback from default profile for unset Google OAuth creds | M1 (config), M3 (profiles) | Collab.md Minor |
| 20 | **Minor: Restore/purge safety & locking** | Per-pipeline `_op_lock`; safe temp-staged restore to `old_rel_path` preserving sidecar time/GPS; strict `status == 'quarantined'` purge guard; path containment; batched `purge_all_quarantined` | M1 (DB), M4 (pipeline) | Collab.md Minor |
| 21 | **Minor: Drive integrity/tokens/pagination** | MD5 checksum verification, atomic `0o600` token temp + chmod warning, refresh error surface, `find_takeout_folder_ids` pagination, best-effort download resume, scope/disconnect UI notices | M2 (`gdrive.py`), M3 (UI/routes) | Collab.md Minor |
| 22 | **Minor: Config/secrets resilience** | Log + backup corrupt `runtime_settings.json` & `profiles.json`; atomic `0o600` + chmod warning | M1 (`config.py`), M3 (`profiles.py`) | Collab.md Minor |
| 23 | **Minor: Hash lookup ordering** | `ORDER BY CASE WHEN original_sha256 = ? THEN 0 ELSE 1 END, id ASC` in `get_media_by_original_hash` & Tier 1 dedup | M1 | Collab.md Minor |
| 24 | **Minor: Scanner isolation** | Separate `try/except` around `index_existing_library()` in `run_cycle()` | M4 | Collab.md Minor |
| 25 | **Minor: Async & throttled Notifier** | Non-blocking `send_async()` for pipeline (keep sync `send()` for `test-notification`) + 12h disk-guard alert throttle | M4 | Collab.md Minor |
| 26 | **Minor: Stored XSS (`app.js`)** | Escape `"`, `'` in `escapeHtml` (fixes `renderArchives` `title` and attribute sinks) | M3 | Collab.md Minor |
| 27 | **Minor: `SynologyPhotosClient`** | POST body for `_sid`, auto-relogin on code `119`, album cache invalidation, root-level `resolve_item_id` scoping | M2 | Collab.md Minor |
| 28 | **Minor: `proxy.cgi` & `oauth_callback.cgi` & `_public_base_url`** | 1 MiB body cap (413), offline 200 HTML only for `GET /` & 503 JSON for API/non-GET, CRLF header sanitization, hardened `get_dsm_user()`, `oauth_callback.cgi` query param allowlist, trusted `_public_base_url` | M3 | Collab.md Minor/Nit |
| 29 | **Minor: SPK/CI/Docker hygiene** | PID cmdline check + start lock + log rotation in `start-stop-status`; UI-only `status` health probe; unified versions; per-install Watchtower token; `cap_drop`/`no-new-privileges`; `TZ=UTC`; Watchtower POST-only; `.dockerignore` excludes `.agents` | M5 | Collab.md Minor/Nit |
| 30 | **Minor: Metadata details** | GPS DMS seconds=60 carry clamp, `OffsetTimeOriginal="+00:00"`, ASCII `ImageDescription` + UTF-8 `UserComment`, float `_parse_timestamp`, clear non-JPEG no-ExifTool warning | M2 | Collab.md Minor/Nit |
| 31 | **Minor: Inbox & quality heuristics** | Skip symlinks, `(size, mtime_ns)` stability; non-inferior sharpness check on ≥15% size upgrade rule | M2 | Collab.md Minor |
| 32 | **Minor: Bookkeeping & scheduler** | `upsert_archive` counter reset on retry; periodic `log_event` prune; `next_run_at=None` when paused; `last_run_at` on manual runs; `.tmp.<pid>` cleanup | M1 (DB), M4 (scheduler/pipeline) | Collab.md Minor |
| 33 | **Test Coverage & E2E Suite** | Comprehensive unit/integration tests across M1–M5 + opaque-box 4-tier E2E suite (`tests/e2e/`, `TEST_INFRA.md`, `TEST_READY.md`) + Tier 5 adversarial hardening | M1–M5 (unit), E2E Track (Tiers 1–4), M6 (Tiers 1–5) | Collab.md Test Gaps |
| 34 | **CI Verification, `v1.2.0` Release & `Collab.md` Handoff** | Verify `ruff`, `node --check`, `pytest`, CI green (`x86_64` & `arm64` `.spk`), publish `v1.2.0` release with `.sha256`, append commit-by-commit handoff note in `Collab.md` | M6 | ORIGINAL_REQUEST.md |

---

## Milestones

| # | Name | Scope | Dependencies | Status | Sub-Orchestrator Conv ID |
|---|---|---|---|---|---|
| M1 | Foundation (Deps, Config, Database) | `pyproject.toml`, `uv.lock`, `__init__.py`, `config.py`, `database.py`, `test_database.py`, `test_config.py` | none | DONE | `645f581a2823278930b5dd011557c86b` |
| M2 | Takeout, Dedup, Sources & Synology Client | `takeout/*`, `sources/*`, `synology/*`, unit tests | M1 | IN_PROGRESS | `da1938ed325294ac4a01f98d8c966036` |
| M3 | Security, Auth, Profiles, Web UI & CGIs | `web/security.py`, `profiles.py`, `web/routes.py`, `main.py`, `web/static/*`, `synology-spk/ui/*.cgi`, unit/CGI/JS tests | M1 | IN_PROGRESS | `6de102198161b0fce46046ef05ec4f11` |
| M4 | Pipeline, Scheduler & Notifier | `worker/pipeline.py`, `worker/scheduler.py`, `notifications.py`, pipeline/upgrade/scheduler/notifier tests | M1, M2 | PLANNED | — |
| M5 | Packaging, SPK Lifecycle, Updater, CI & Docker | `updater.py`, `synology-spk/build_spk.py`, `.github/workflows/docker-publish.yml`, `Dockerfile`, `docker-compose.yml`, `.env.example`, `.dockerignore`, `README.md`, updater/SPK tests | M1, M3 | PLANNED | — |
| E2E | E2E Testing Track (Parallel) | `TEST_INFRA.md`, `tests/e2e/` (Tiers 1–4 opaque-box tests), `TEST_READY.md` | none (runs in parallel) | IN_PROGRESS | `cefe08249ed6750e9786dd4091caf507` |
| M6 | Final Milestone: E2E Pass, Tier 5 Hardening, CI & `v1.2.0` Release | Phase 1: Tiers 1–4 100% pass; Phase 2: Tier 5 adversarial hardening; clean commits, push & tag `v1.2.0`, verify CI green (`x86_64` & `arm64`), verify Release `v1.2.0` + `.sha256`, append `Collab.md` handoff | M1–M5, E2E (`TEST_READY.md`) | PLANNED | — |

---

## Interface Contracts

### 1. Proxy / CGI ↔ FastAPI Security (`build_spk.py` ↔ `proxy.cgi` / `oauth_callback.cgi` ↔ `web/security.py`)
- **Shared Secret File**:
  - `start-stop-status` creates `${PKG_VAR}/proxy.secret` (`umask 077`, 64 hex chars, `chmod 0600`) and exports `PROXY_SECRET_FILE="${PKG_VAR}/proxy.secret"`.
  - `Settings` loads `proxy_secret` from `GPHOTOS_PROXY_SECRET` (env) or reads `proxy_secret_file` (`PROXY_SECRET_FILE` env).
  - `proxy.cgi` and `oauth_callback.cgi` read secret from `GPHOTOS_PROXY_SECRET` env (for tests) or `GPHOTOS_PROXY_SECRET_FILE` (default `/var/packages/gphotos-migrator/var/proxy.secret`), injecting `X-Proxy-Secret: <secret>`.
- **Proxy Headers**:
  - `X-Proxy-Secret`: shared secret (verified via `hmac.compare_digest`).
  - `X-Proxy-Mode: cgi` (trusted ONLY when `X-Proxy-Secret` is valid).
  - `X-DSM-User: <validated_username>` (from `authenticate.cgi` last non-empty line matching `^[A-Za-z0-9._@-]{1,64}$`; trusted ONLY when `X-Proxy-Secret` is valid).
  - `X-DSM-Admin: 1` or `0` (computed in CGI via `grp.getgrnam("administrators")` or `GPHOTOS_TEST_DSM_ADMIN` in tests; backend also treats users in `settings.dsm_admin_users` as admin; trusted ONLY when `X-Proxy-Secret` is valid).
  - `X-Profile-Id`: forwarded by `proxy.cgi`; ignored/rejected with `403` for non-admin DSM users when target profile's `dsm_username` != `X-DSM-User`.
  - `X-Requested-With`: forwarded by `proxy.cgi`; required (non-empty) on all mutating `/api/*` (`POST`, `PUT`, `PATCH`, `DELETE`) requests (G6).
- **Direct / Docker Auth Mode**:
  - `Settings.api_token` (`API_TOKEN` env) or auto-generated token at `data_dir / "api.token"` (`0600`) when neither `proxy_secret` nor `api_token` nor `allow_insecure_no_auth=True` is configured.
  - Accepted via `Authorization: Bearer <token>`, `X-API-Token: <token>`, or `HttpOnly; SameSite=Strict` cookie `gphotos_session` set by `POST /api/auth/session` (with JSON `{"token": "..."}` + `X-Requested-With`).
  - **Exempt from auth**: `GET /api/health`, `GET /`, `GET /static/*`, `GET /api/auth/status`, `POST /api/auth/session` (CSRF header still required on `POST /api/auth/session`).
- **Identity Contract (`web/security.py`)**:
  - `RequestIdentity(authenticated: bool, via_proxy: bool, dsm_user: str, is_admin: bool)`.
  - `GET /api/profiles` response adds `"is_admin": bool` and `"dsm_user": str` alongside existing fields; non-admins receive only profiles owned by `dsm_user` and empty/self-only `discovered_users`.

### 2. ConfigManager & Settings (`config.py` ↔ all modules)
- `Settings` defaults: `app_host="127.0.0.1"`, `synology_url="https://127.0.0.1:5001"`, `synology_verify_ssl=True`, `synology_ca_cert=""`, `quarantine_dir_override: Path | None = None` (`Settings.quarantine_dir` returns `quarantine_dir_override` if set else `work_dir / "replaced_duplicates"`).
- `ConfigManager(base_settings, fallback_settings_provider=None)`: `.settings` dynamically inherits unset `google_client_id`, `google_client_secret` (and `synology_url` if still default/empty) from `fallback_settings_provider()`.
- `ConfigManager.update(patch)` returns updated `Settings` and records `self.last_update_warnings: list[str]` (or `update_with_meta` helper):
  - Rejects `synology_url` without `http://`/`https://` scheme or containing whitespace/control chars (`ValueError`).
  - If `synology_url` changes from its prior value AND patch does NOT provide a new non-empty, non-`"********"` `synology_password`, clears stored `synology_password = ""` and logs a warning (closing G4).
- `SECRET_FIELDS` includes `google_client_secret`, `synology_password`, `smtp_password`, `watchtower_token`, `proxy_secret`, `api_token`. None of `proxy_secret`, `api_token`, `dsm_admin_users`, `trust_proxy_headers`, `allow_insecure_no_auth`, `photos_dir`, `watchtower_url`, `watchtower_token` are in `MUTABLE_UI_FIELDS`.

### 3. StateDB (`database.py` ↔ `routes.py`, `pipeline.py`, `scanner.py`, `dedup.py`)
- `delete_state(key: str) -> None`
- `media_items` new column: `visual_status TEXT NOT NULL DEFAULT ''` (`'ok' | 'undecodable' | 'skipped' | ''`). `upsert_existing_nas_media` and `record_media_item` accept `visual_status: str = ""` (backwards-compatible default).
- `media_dhash_buckets(bucket_idx INTEGER NOT NULL, bucket_val TEXT NOT NULL, media_id INTEGER NOT NULL, PRIMARY KEY (bucket_idx, bucket_val, media_id)) WITHOUT ROWID`:
  - Maintained whenever a 32-hex-char `dhash` is inserted/updated/upgraded/pruned (8 blocks of 4 hex chars: `dhash[i*4:(i+1)*4]` for `i in range(8)`).
  - `find_duplicate_media` Tier 3 queries `media_dhash_buckets` when `effective_max < 8` and `len(dhash) == 32`; falls back to full scan when `effective_max >= 8` or legacy 16-char hash so recall is 100%.
- `quality_upgrades` new columns: `old_rel_path TEXT NOT NULL DEFAULT ''`, `old_taken_at TEXT NOT NULL DEFAULT ''`, `old_latitude REAL`, `old_longitude REAL`.
- `quality_upgrades` journal API:
  - `create_pending_quality_upgrade(...) -> int` (inserts row with `status='in_progress'`).
  - `finalize_quality_upgrade(upgrade_id: int, kept_rel_path: str) -> None` (transitions `'in_progress'` → `'quarantined'`).
  - `delete_quality_upgrade(upgrade_id: int) -> None` (removes rolled-back `'in_progress'` row).
  - `list_in_progress_quality_upgrades() -> list[dict[str, Any]]`.
  - `list_quarantined_upgrades_batch(limit: int = 500) -> list[dict[str, Any]]`.
- `mark_album_membership(album_id, media_id, *, status, error="", increment_retry: bool | None = None)`: defaults to `increment_retry = (status == "error")` so `'pending'` waiting-for-index never burns retries.

### 4. Takeout & Sources (`extractor.py`, `dedup.py`, `gdrive.py` ↔ `pipeline.py`, `routes.py`)
- `extract_archive(archive_path: Path, dest_dir: Path, *, max_entries: int = 100_000, max_uncompressed_bytes: int | None = None, max_compression_ratio: float = 100.0, skip_unsafe_entries: bool = False) -> Path`
  - Raises `ValueError` on unsafe entry when `skip_unsafe_entries=False` (preserves fail-closed contract); skips and logs WARNING when `skip_unsafe_entries=True`.
  - Raises `ValueError` on entry count, uncompressed byte cap, streaming byte cap, or ratio exceeded.
- `cleanup_stale_temp_files(directory: Path, max_age_seconds: float = 0.0) -> int` in `extractor.py`.
- `MediaFingerprint` adds `visual_status: str = ""` (`"ok" | "undecodable" | "skipped"`).
- `GoogleDriveClient` exposes `connection_status() -> dict[str, Any]` returning `{"connected": bool, "error": str}` (while keeping `is_connected() -> bool` backwards compatible).

### 5. Pipeline & Notifier (`pipeline.py`, `notifications.py` ↔ `routes.py`, `scheduler.py`)
- `MigrationPipeline` has `self._op_lock = threading.RLock()` and `restore_quality_upgrade` / `purge_quality_upgrade` raise `ValueError` on invalid status or path traversal (mapped to HTTP `400` in `routes.py`).
- `Notifier.send(title, message)` remains synchronous returning `bool` (for `POST /api/actions/test-notification`); `Notifier.send_async(title, message) -> None` dispatches in a daemon thread for pipeline alerts.

### 6. Packaging & Dependency Pinning (`pyproject.toml`, `uv.lock`, `docker-publish.yml`)
- **Mandatory `manylinux2014` wheel ceiling**: `"pillow>=11.1.0,<12.3.0"` (locks `12.2.0`) and `"pillow-heif>=1.0.0,<1.1.0"` (locks `1.0.0`) so `--locked --only-binary :all:` succeeds on glibc 2.17 `x86_64` and `aarch64`.
- PyInstaller invocation in CI includes `--collect-all pillow_heif`.
- Unified version: `pyproject.toml` `1.2.0`, `__init__.py` `1.2.0`, SPK `1.2.0-0001`, Git tag / GitHub Release `v1.2.0`.

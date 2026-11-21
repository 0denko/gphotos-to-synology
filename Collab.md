# Collab.md — Antigravity ↔ Bingo Collaboration Log

**Repository:** `0denko/gphotos-to-synology` (`https://github.com/0denko/gphotos-to-synology`)  
**Owner / Developer:** Oleksandr Danylchenko  
**Last Updated:** 2026-11-21 (by Antigravity)

---

## Current Status Summary

We built an **all-in-one native Synology DSM 7 `.spk` app** (`Google Photos Migrator`) for unattended Google Photos → Synology Photos migration — **zero Docker, zero Container Manager, zero YAML, zero SSH**.

- **Latest Package Version:** `v1.1.0-0003` (commit `d0a114b`, CI green, 20/20 `pytest` passing, both `x86_64` and `arm64` standalone binaries live smoke-tested in `manylinux2014`)
- **Direct SPK Downloads (GitHub Release `v1.0.0`):**
  - **Intel/AMD (`x86_64`, matches user's NAS):** https://github.com/0denko/gphotos-to-synology/releases/download/v1.0.0/gphotos-migrator-dsm7-x86_64.spk
  - **ARM64 (`arm64`):** https://github.com/0denko/gphotos-to-synology/releases/download/v1.0.0/gphotos-migrator-dsm7-arm64.spk

---

## Key Requirements & Architectural Decisions

1. **Zero Docker / Zero YAML (Native DSM 7 `.spk`):**
   - User explicitly requested no Docker Compose / YAML pasting.
   - CI (`.github/workflows/docker-publish.yml`) builds glibc-2.17-compatible PyInstaller `--onedir` standalone bundles inside `quay.io/pypa/manylinux2014_{x86_64,aarch64}` using `uv` managed Python 3.12 (`--only-binary :all:` + `--no-deps .`), smoke-tests `/api/health` and `/`, and packs native DSM 7 `.spk` archives via `synology-spk/build_spk.py`.
   - Runs natively as Synology DSM 7 package user `gphotos-migrator` (`conf/privilege` `run-as: package`) managed by `scripts/start-stop-status`, storing state/logs/work/inbox in `$SYNOPKG_PKGVAR` (`/var/packages/gphotos-migrator/var/`) and listening locally on `127.0.0.1:8765`.
2. **In-DSM Desktop Window (No Separate Page):**
   - Native Ext JS 3 DSM window (`SYNO.SDS.GPhotosMigrator.AppWindow`) in `synology-spk/ui/GPhotosMigrator.js`.
   - **Important DSM 7 nginx fix (`v1.1.0-0003`):** DSM 7 nginx only routes URLs ending strictly in `.cgi` (`\.cgi$`, no `PATH_INFO`).
     - `GPhotosMigrator.js` loads `/webman/3rdparty/gphotos-migrator/proxy.cgi` (no trailing slash).
     - All dashboard static/API calls use query-param routing `proxy.cgi?p=/api/...` (handled by `synology-spk/ui/proxy.cgi` and `src/gphotos_synology/web/static/app.js`).
     - `proxy.cgi` authenticates via `/usr/syno/synoman/webman/authenticate.cgi`, forwards `X-DSM-User`, `X-Profile-Id`, `X-Proxy-Mode: cgi`, and returns HTTP 200 on backend startup so DSM nginx never intercepts it with a generic 503 page.
     - `synology-spk/ui/oauth_callback.cgi` provides a clean query-free Redirect URI (`https://<nas>:5001/webman/3rdparty/gphotos-migrator/oauth_callback.cgi`) for Google OAuth2.
3. **Migration Pipeline & Features:**
   - **Ingestion:** Google Takeout Scheduled Incremental Export → Google Drive OAuth2 polling + watched local inbox folder (required since Google removed `photoslibrary.readonly` on March 31, 2025).
   - **Metadata Repair:** Takeout sidecar matcher (`.json`, `.supplemental-metadata.json`, numbered duplicates, `-edited`, Live Photos, 46-char truncation) + pure-Python `piexif` JPEG EXIF/GPS writer fallback (works natively in SPK without ExifTool) + ExifTool support + `mtime` sync.
   - **Existing NAS Dedup & Visual Quality Auto-Upgrade:** Incrementally indexes pre-existing NAS photos (skipping `@eaDir` & `#recycle`), 3-tier dedup (SHA-256 → decoded RGB pixel SHA-256 → 128-bit `dHash+vHash`), auto-upgrades lower-res/blurrier NAS copies, quarantines replaced copies in `WORK_DIR/replaced_duplicates` with side-by-side preview/restore/purge UI.
   - **Multi-User Support:** Synology User Picker auto-discovers `/volume1/homes/*` users, isolates SQLite DB / OAuth token / inbox / quarantine per user, inherits shared OAuth Client ID/Secret, and auto-selects the logged-in DSM user via `X-DSM-User`.
   - **Update Checker:** Built-in GitHub update check (`src/gphotos_synology/updater.py`) with arch-aware `.spk` download links (plus Watchtower support for Docker users).

---

## Pending Steps / Next Actions

1. **Verify `v1.1.0-0003` on User's NAS** (`192.168.100.97` / QuickConnect):
   - User installed `v1.1.0-0001` and hit DSM 404 due to `/proxy.cgi/` trailing slash.
   - User should install `v1.1.0-0003` via **Package Center → Manual Install**, refresh the DSM browser tab (`Ctrl+R` / `Cmd+R`), and confirm the dashboard loads inside the DSM window.
2. **One-Time Shared Folder Permissions in DSM:**
   - **Control Panel → Shared Folder → `photo` → Edit → Permissions → System internal user → `gphotos-migrator` → Read/Write → Save** (and repeat for **`homes`** for multi-user Personal Spaces).
3. **Connect Google Takeout & Drive OAuth2:**
   - Create Google Cloud OAuth2 Web Client ID with Redirect URI shown in **Settings & Connections** (`https://<nas>:5001/webman/3rdparty/gphotos-migrator/oauth_callback.cgi`).
   - Schedule Google Takeout (Google Photos only → Add to Drive → Export every 2 months for 1 year).

---

## Handoff Notes (Antigravity ↔ Bingo)

### 2026-11-21 — Antigravity
- Created `Collab.md` in `0denko/gphotos-to-synology` so Bingo and Antigravity have a shared source of truth.
- `v1.1.0-0003` is live on GitHub Releases (`v1.0.0` tag) with developer/maintainer set to **Oleksandr Danylchenko** and the DSM 7 nginx CGI 404 resolved.
- Bingo: feel free to append notes, user feedback, NAS logs (`/var/packages/gphotos-migrator/var/gphotos-migrator.log`), or follow-up tasks below!

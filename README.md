# Google Photos → Synology DSM Background Migrator

An unattended, Dockerized background service built for **Synology DSM (Container Manager)** that continuously migrates photos and videos from **Google Photos** into **Synology Photos** with minimal human involvement.

## Why This Architecture?

In March 2025, Google removed the `photoslibrary.readonly` scope from the Google Photos Library API, preventing third-party apps from reading a user's entire Google Photos library programmatically in the background.

This solution uses Google's official unattended export path — **Google Takeout Scheduled Incremental Exports** delivered automatically to **Google Drive** (or dropped into a watched NAS folder via Synology Cloud Sync) — combined with a background Docker worker running directly on your Synology NAS:

```text
Google Photos ──(Scheduled Incremental Takeout)──► Google Drive / NAS Inbox
                                                          │
                       ┌──────────────────────────────────▼──────────────────────────────────┐
                       │          gphotos-to-synology (Docker on Synology DSM)               │
                       │  0. Incrementally indexes existing NAS photos (skips @eaDir/#recycle│
                       │  1. Polls Google Drive (OAuth2) & watched /data/inbox               │
                       │  2. Disk-space safety guard + resumable streaming download          │
                       │  3. Safe Zip/TGZ extraction (ZipSlip protected)                     │
                       │  4. Matches Takeout JSON sidecars (.json / .supplemental-metadata)  │
                       │  5. 3-tier deduplication against existing NAS & Takeout archives    │
                       │  6. Repairs EXIF/XMP/QuickTime dates, GPS & tags via ExifTool       │
                       │  7. Atomic import into Synology Photos (/photo or /homes/<u>/Photos)│
                       │  8. Optional DSM 7 Photos API sync for native virtual albums        │
                       └──────────────────────────────────┬──────────────────────────────────┘
                                                          ▼
                                            Synology Photos (Timeline + Albums)
```

## Key Features

- **Hands-off background operation**: Polls Google Drive and/or a local NAS inbox folder on a schedule, with automatic retries, pause/resume, and an annual reminder when Google Takeout's 1-year export schedule needs renewal.
- **Battle-tested Takeout sidecar matching**: Handles legacy `.json`, new `.supplemental-metadata.json` (including truncated variants), numbered duplicates (`IMG_1(1).JPG` ↔ `IMG_1.JPG(1).json`), `-edited` / localized copies, Live Photo companion videos, 46-char truncations, and JSON `"title"` fallback matching.
- **EXIF, GPS & Timestamp Repair**: Embeds original capture timestamps, GPS coordinates, descriptions, people, and album tags via **ExifTool** and syncs filesystem `mtime` so Synology Photos builds an accurate timeline.
- **Existing NAS Library Indexing, Visual Deduplication & Quality Auto-Upgrade**: Automatically indexes your pre-existing Synology Photos library before importing (safely ignoring `@eaDir` thumbnails and `#recycle`), then matches duplicates across both existing NAS files and Takeout albums via:
  1. **Exact SHA-256** (raw and EXIF-repaired hashes)
  2. **Decoded Pixel SHA-256** (catches identical photos with different EXIF/metadata headers)
  3. **128-bit Visual Perceptual Match (`dHash + vHash`)** guarded by aspect-ratio and capture-time proximity to match resized or re-compressed copies even when file hashes differ completely
  - **Automatic Clarity & Resolution Selection**: When a visual duplicate is found, both copies are compared on resolution (`width × height`), perceptual clarity (Laplacian edge variance), and compression fidelity. If the incoming Google Photos copy is higher resolution or clearer, it **automatically upgrades** the NAS copy and safely moves the replaced lower-quality file into `/data/work/replaced_duplicates/` (outside Synology Photos so it isn't re-indexed).
  - **Side-by-Side Dashboard Review**: Inspect upgraded vs. quarantined images side-by-side in the **Quality Upgrades** tab with one-click **Restore Replaced Copy** or **Purge Quarantined**.
  - **Album preservation**: Skipped and upgraded duplicates keep all of their **Google Photos album memberships** attached to the winning NAS photo.
- **Hybrid Synology DSM Import**:
  - **Direct Folder Import (always on)**: Fast, resilient atomic moves directly into `/volume1/homes/<user>/Photos` (Personal Space) or `/volume1/photo` (Shared Space).
  - **Optional DSM 7 Photos API Sync**: Authenticates to DSM (`SYNO.Foto.*` / `SYNO.FotoTeam.*`) to materialize Google Photos albums as true native Synology Photos virtual albums without duplicating files on disk.
- **NAS-Safe Operations**: Enforces a minimum free disk guard before downloading/extracting, cleans temporary staging folders automatically, supports Synology `PUID`/`PGID` file ownership, and optionally moves verified archives to Google Drive Trash to reclaim cloud storage quota.
- **Built-in Web Dashboard**: Manage Google Drive OAuth2, Synology API credentials, live progress, archive history, albums, logs, and notifications (ntfy, Discord, Slack, generic webhook, or SMTP) at `http://<NAS-IP>:8765`.

---

## Quick Start on Synology DSM (Container Manager)

### 1. Create Folders on Your NAS

Create the following directories in File Station (adjust `/volume1` if your volume differs):

- `/volume1/docker/gphotos-migrator/config` — SQLite state DB, settings, OAuth tokens
- `/volume1/docker/gphotos-migrator/work` — Temporary download/extract scratch space
- `/volume1/docker/gphotos-migrator/inbox` — Optional local folder for manual or Synology Cloud Sync archives

### 2. Find Your Synology `PUID` and `PGID`

SSH into your NAS and run:
```bash
id <your_dsm_username>
```
Typical Synology values are `PUID=1026` and `PGID=100` (`users`).

### 3. Deploy with Docker Compose / Container Manager

Copy `.env.example` to `.env`, adjust `PUID`, `PGID`, `TZ`, and the photo volume mount in `docker-compose.yml`:
- **Personal Space**: `/volume1/homes/<your_dsm_username>/Photos:/data/photos`
- **Shared Space**: `/volume1/photo:/data/photos`

Then start the container:
```bash
docker compose up -d --build
```
Open the dashboard at `http://<YOUR_NAS_IP>:8765`.

---

## Setting Up Unattended Google Photos Exports

### Step A: Schedule Incremental Google Takeout
1. Go to [takeout.google.com](https://takeout.google.com).
2. Click **Deselect all**, then select **only Google Photos**.
3. Click **Next step**:
   - **Destination**: Choose **Add to Drive** (creates a `Takeout` folder in Google Drive).
   - **Frequency**: Select **Export every 2 months for 1 year** (Google Photos supports incremental exports — the first archive is a full baseline and subsequent archives contain only new/updated media).
   - **File type & size**: `.zip` and `10 GB` or `50 GB` (choose a size smaller than half your free space in `/data/work`).
4. Click **Create export**.

### Step B: Connect Google Drive (Two Options)

#### Option 1 — Built-in Google Drive OAuth2 (Recommended)
1. In [Google Cloud Console](https://console.cloud.google.com/), create a project and enable the **Google Drive API**.
2. Configure the **OAuth consent screen** (External → add your own Google account under **Test users**; no Google verification needed for personal use!).
3. Create an **OAuth 2.0 Client ID** (Web application) and add the Authorized Redirect URI shown in the dashboard's **Settings & Connections** tab (e.g., `http://<YOUR_NAS_IP>:8765/api/oauth/google/callback`).
4. Paste your Client ID & Client Secret in the dashboard, click **Connect Google Drive**, and authorize once. The refresh token is saved in `/data/config/google_token.json` for continuous unattended polling.

> **Tip on Google Drive Storage Quota**: In **Settings → Google Drive Automation**, you can set **After Verified Import** to **Move Archive to Drive Trash** so processed Takeout ZIPs don't consume your Google One storage permanently.

#### Option 2 — Zero-OAuth via Synology Cloud Sync
If you prefer not to create a Google Cloud OAuth client:
1. Open **Synology Cloud Sync** on DSM and link your Google Drive's `/Takeout` folder (download-only) to `/volume1/docker/gphotos-migrator/inbox`.
2. This service automatically detects new archives once they finish syncing (via the stability check), extracts/repairs/imports them, and cleans them up.

---

## Optional: Enabling Native Synology Photos Virtual Albums (DSM 7 API)

Direct folder import is always active and reliable. If you also want your Google Photos albums recreated as native virtual albums in Synology Photos:

1. In DSM **Control Panel**, create or use a DSM user with Synology Photos access (for unattended API login, avoid 2FA on that dedicated account or restrict it to local LAN).
2. In the dashboard's **Settings & Connections → Synology DSM 7 Photos API**:
   - Check **Enable Synology Photos virtual album sync**.
   - Set **DSM URL** (e.g., `http://172.17.0.1:5000` or `http://<NAS_LAN_IP>:5000`).
   - Select **Personal Space** or **Shared Space** to match your mounted `/data/photos` volume.
   - Click **Test Synology Connection**, then **Save All Settings**.
3. As DSM indexes newly imported files, the background worker automatically resolves each photo's Synology item ID and adds it to the corresponding virtual album (retrying gracefully if indexing is still in progress).

---

## Local Development & Running Tests

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest -v
```

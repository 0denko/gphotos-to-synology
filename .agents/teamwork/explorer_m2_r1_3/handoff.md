# Explorer M2 R1-3 Handoff — `GoogleDriveClient`, `LocalInboxScanner` & `SynologyPhotosClient`

**Explorer:** `explorer_m2_r1_3`  
**Milestone:** M2 — Takeout, Dedup, Sources & Synology Client  
**Scope:**
1. `src/gphotos_synology/sources/gdrive.py` (`GoogleDriveClient`)
2. `src/gphotos_synology/sources/inbox.py` (`LocalInboxScanner`)
3. `src/gphotos_synology/synology/photos_api.py` (`SynologyPhotosClient`)
4. `tests/test_synology_api_and_security.py` (and new/updated unit tests for `gdrive.py`, `inbox.py`, `photos_api.py`)

---

## 1. Executive Summary & Critical E2E Test Discoveries

All target files (`gdrive.py`, `inbox.py`, `photos_api.py`, `test_synology_api_and_security.py`) and their callers/tests (`worker/pipeline.py`, `web/routes.py`, `config.py`, `tests/test_pipeline_e2e.py`, `tests/test_profiles.py`, `tests/e2e/conftest.py`, `tests/e2e/test_tier1_features.py`, `tests/e2e/test_tier2_boundaries.py`, `tests/e2e/test_tier3_interactions.py`, `tests/e2e/test_tier4_workloads.py`) were inspected read-only.

### Key Compatibility Findings from `tests/e2e/`
1. **`GoogleDriveClient` (`test_tier1_features.py:789-881`, `test_tier2_boundaries.py:1337-1419`)**:
   - E2E tests monkeypatch `gdrive_mod.build` and `gdrive_mod.MediaIoBaseDownload` where `service.files().get_media(fileId=...)` returns a bare `object()` (no `.headers` attribute) and `_FakeDownloader(fh, request, chunksize=...)` writes bytes directly to `fh`.
   - Therefore, resume header injection must guard `if hasattr(request, "headers") and isinstance(request.headers, dict): request.headers["Range"] = f"bytes={offset}-"`, and `downloader._progress = offset` must be set with `setattr` (and a fallback tail-check if a naive mock ignores `Range`/`_progress` and writes the full file on retry).
   - `test_t2_minor_gdrive_md5_mismatch_rejected_and_refresh_error_surfaced` (`test_tier2_boundaries.py:1358-1368`) monkeypatches `Credentials.refresh` to raise `RuntimeError("invalid_grant: Token has been revoked")` and asserts `st = client.connection_status()` returns `st["connected"] is False` and `"revoked" in st["error"].lower()`. Thus `get_credentials()` must catch exceptions during `creds.refresh(Request())`, store the error in `self.last_error`, log an error, and return `None`.
2. **`LocalInboxScanner` (`test_tier2_boundaries.py:1307-1335`)**:
   - In `test_t2_minor_inbox_skips_symlinks_and_tracks_mtime_stability`, `inbox_stability_seconds=1`:
     - Scan 1 runs at $t = 0.0\text{s}$ (`[]`).
     - At $t = 0.6\text{s}$, `os.utime(real_zip, ns=(new_ns, new_ns))` advances `st_mtime_ns` by $+5\text{s}$ while keeping `st_size` identical.
     - At $t = 1.1\text{s}$ ($0.5\text{s}$ after `os.utime`), Scan 2 runs and asserts `scan_ready_archives() == []`.
     - At $t = 1.8\text{s}$ ($0.7\text{s}$ after Scan 2, and $1.2\text{s}$ after `os.utime`), Scan 3 runs and asserts `[i.name for i in ready] == ["takeout-real.zip"]`!
   - **Critical timing subtlety**: Between Scan 2 ($t=1.1\text{s}$) and Scan 3 ($t=1.8\text{s}$), only $0.7\text{s}$ elapses, whereas $0.5\text{s} + 0.7\text{s} = 1.2\text{s} \ge 1.0\text{s}$ has elapsed since the `os.utime` modification at $t=0.6\text{s}$. If Scan 2 naively resets `seen_since = now` ($1.1\text{s}$), Scan 3 at $1.8\text{s}$ sees only $0.7\text{s} < 1.0\text{s}$ and fails!
   - **Solution**: When `prev is not None` and `(prev_size, prev_mtime_ns) != (stat.st_size, stat.st_mtime_ns)`, Scan 2 must NEVER return the archive on the scan where the change was first detected (`continue`), and should estimate the monotonic change time using the kernel inode change time `stat.st_ctime_ns`:
     `ctime_age = max(0.0, time.time() - (stat.st_ctime_ns / 1_000_000_000))`
     `change_mono = max(prev[2], now - ctime_age)`
     Then Scan 2 records `(stat.st_size, stat.st_mtime_ns, change_mono)` ($0.6\text{s}$) and `continue`s (returning `[]`), and Scan 3 ($1.8\text{s}$) sees unchanged `(st_size, st_mtime_ns)` across the probe interval AND `now - change_mono = 1.2s >= 1.0s`!
   - All existing test archives scanned by `LocalInboxScanner` across `tests/` and `tests/e2e/` are valid `.zip` or `.tgz`/`.tar.gz` archives built with `zipfile.ZipFile` or `tarfile.open(..., "w:gz")`.
3. **`SynologyPhotosClient` (`tests/e2e/conftest.py:573-628`, `test_tier1_features.py:762-787`, `test_tier2_boundaries.py:1421-1447`, `tests/test_synology_api_and_security.py:26-84`)**:
   - `test_t1_d3_synology_photos_client_login_post_sid_album_and_resolve` asserts that for EVERY request in `mock_dsm_server.state.requests`, `"_sid" not in req.query` and `if "_sid" in req.form: assert req.method == "POST"`.
   - `MockDSMServer` in `tests/e2e/conftest.py:598-625` implements `SYNO.Foto.Browse.Folder` / `SYNO.FotoTeam.Browse.Folder` with `method="get"`, returning `{"success": True, "data": {"folder": {"id": state.root_folder_id, "name": "/"}}}` (`id=1`), and when `Item.list` is called with `folder_id=1`, it returns only items in the root folder (`state.items_by_folder[1]`), whereas omitting `folder_id` returns items across ALL folders.

---

## 2. Exact File-and-Line Implementation Blueprint

### 2.1 `src/gphotos_synology/sources/gdrive.py` (`GoogleDriveClient`)

#### Current State (Lines 1–245)
- Lines 43–45 (`__init__`): only sets `self.settings = settings`; no `self.last_error: str = ""`.
- Lines 96–104 (`_save_credentials`): uses `tmp.write_text(...)` with default umask, suppresses `os.chmod(tmp, 0o600)` `OSError` silently, no `os.open(..., 0o600)` or `fsync()`.
- Lines 108–129 (`get_credentials`, `is_connected`): does not catch `creds.refresh(Request())` errors inside `get_credentials()`, does not track `self.last_error`, and lacks `connection_status() -> dict[str, Any]`.
- Lines 137–149 (`find_takeout_folder_ids`): single-page query with `pageSize=20` and no `nextPageToken` loop.
- Lines 199–239 (`download_archive`): always opens `tmp_path` with `"wb"`, unlinks `tmp_path` on every transient error, does not resume from existing `.part`, and never verifies `item.md5_checksum`.

#### Blueprint for `src/gphotos_synology/sources/gdrive.py`

1. **Imports (Lines 5–13)**:
   Add `import hashlib` and `from typing import Any`:
   ```python
   import contextlib
   import hashlib
   import logging
   import os
   import time
   from collections.abc import Callable
   from dataclasses import dataclass
   from pathlib import Path
   from typing import Any
   ```

2. **`__init__` (Lines 43–45)**:
   Initialize `self.last_error: str = ""`:
   ```python
   def __init__(self, settings: Settings) -> None:
       self.settings = settings
       self.last_error: str = ""
   ```

3. **Atomic `0o600` Token Write in `_save_credentials` & `disconnect` (Lines 96–107)**:
   Replace `_save_credentials` and `disconnect` with:
   ```python
   def _save_credentials(self, creds: Credentials) -> None:
       token_path = self.settings.token_path
       token_path.parent.mkdir(parents=True, exist_ok=True)
       tmp = token_path.with_suffix(".tmp")
       payload = creds.to_json()

       fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
       try:
           try:
               handle = os.fdopen(fd, "w", encoding="utf-8")
           except Exception:
               os.close(fd)
               raise
           with handle:
               handle.write(payload)
               handle.flush()
               os.fsync(handle.fileno())
           try:
               os.chmod(tmp, 0o600)
           except OSError as exc:
               logger.warning("Could not enforce 0600 permissions on %s: %s", token_path, exc)
           os.replace(tmp, token_path)
           self.last_error = ""
       except Exception:
           with contextlib.suppress(OSError):
               tmp.unlink(missing_ok=True)
           raise

   def disconnect(self) -> None:
       self.settings.token_path.unlink(missing_ok=True)
       self.last_error = ""
   ```

4. **Error Tracking & `connection_status()` in `get_credentials`, `is_connected`, `connection_status`, `_build_service` (Lines 108–136)**:
   ```python
   def get_credentials(self) -> Credentials | None:
       """Load stored OAuth2 credentials and refresh automatically if expired."""
       token_path = self.settings.token_path
       if not token_path.exists():
           self.last_error = ""
           return None
       try:
           creds = Credentials.from_authorized_user_file(str(token_path))
       except Exception as exc:
           self.last_error = f"Failed to load Google token file: {exc}"
           logger.error("Failed to load Google token file: %s", exc)
           return None

       if creds.expired and creds.refresh_token:
           try:
               creds.refresh(Request())
               self._save_credentials(creds)
           except Exception as exc:
               self.last_error = f"Google token refresh failed: {exc}"
               logger.error("Failed to refresh Google OAuth token: %s", exc)
               return None

       if creds.valid:
           self.last_error = ""
           return creds

       self.last_error = "Google OAuth token is expired or invalid."
       return None

   def is_connected(self) -> bool:
       try:
           return self.get_credentials() is not None
       except Exception as exc:
           self.last_error = str(exc)
           return False

   def connection_status(self) -> dict[str, Any]:
       """Return {'connected': bool, 'error': str} surfacing token load/refresh failures."""
       connected = self.is_connected()
       return {
           "connected": connected,
           "error": "" if connected else self.last_error,
       }

   def _build_service(self):
       creds = self.get_credentials()
       if creds is None:
           detail = f" ({self.last_error})" if self.last_error else ""
           raise RuntimeError(
               f"Google Drive is not connected{detail}. Complete OAuth setup in the dashboard."
           )
       return build("drive", "v3", credentials=creds, cache_discovery=False)
   ```

5. **Paginated `find_takeout_folder_ids` (Lines 137–150)**:
   Loop across all pages using `nextPageToken`:
   ```python
   def find_takeout_folder_ids(self, service) -> list[str]:
       """Locate all non-trashed Drive folders matching `gdrive_folder_name` (e.g. 'Takeout')."""
       folder_name = self.settings.gdrive_folder_name.replace("'", "\\'")
       query = (
           "mimeType = 'application/vnd.google-apps.folder' "
           f"and name = '{folder_name}' and trashed = false"
       )
       folder_ids: list[str] = []
       page_token: str | None = None
       while True:
           result = (
               service.files()
               .list(
                   q=query,
                   spaces="drive",
                   fields="nextPageToken, files(id, name)",
                   pageSize=100,
                   pageToken=page_token,
               )
               .execute()
           )
           for item in result.get("files", []):
               fid = item.get("id")
               if fid:
                   folder_ids.append(str(fid))
           page_token = result.get("nextPageToken")
           if not page_token:
               break
       return folder_ids
   ```

6. **MD5 Verification & Best-Effort `Range` Download Resume in `download_archive` (Lines 199–239)**:
   Add helper `_compute_md5(path: Path, *, offset: int = 0) -> str` and update `download_archive`:
   ```python
   @staticmethod
   def _compute_md5(path: Path, *, offset: int = 0) -> str:
       digest = hashlib.md5()
       with open(path, "rb") as fh:
           if offset > 0:
               fh.seek(offset)
           while True:
               chunk = fh.read(1024 * 1024)
               if not chunk:
                   break
               digest.update(chunk)
       return digest.hexdigest()

   def download_archive(
       self,
       item: DriveArchiveItem,
       dest_path: Path,
       *,
       on_progress: Callable[[float], None] | None = None,
       max_retries: int = 4,
   ) -> Path:
       """Download a Drive file with chunked streaming, best-effort Range resume, MD5 check, and retry backoff."""
       service = self._build_service()
       dest_path.parent.mkdir(parents=True, exist_ok=True)
       tmp_path = dest_path.with_suffix(dest_path.suffix + ".part")
       expected_md5 = item.md5_checksum.strip().lower()

       for attempt in range(1, max_retries + 1):
           try:
               offset = 0
               if tmp_path.exists():
                   try:
                       offset = tmp_path.stat().st_size
                   except OSError:
                       offset = 0
                   if item.size_bytes > 0 and offset >= item.size_bytes:
                       tmp_path.unlink(missing_ok=True)
                       offset = 0

               request = service.files().get_media(fileId=item.file_id)
               if offset > 0 and hasattr(request, "headers") and isinstance(request.headers, dict):
                   request.headers["Range"] = f"bytes={offset}-"

               mode = "ab" if offset > 0 else "wb"
               with open(tmp_path, mode) as fh:
                   downloader = MediaIoBaseDownload(fh, request, chunksize=8 * 1024 * 1024)
                   if offset > 0:
                       with contextlib.suppress(AttributeError):
                           downloader._progress = offset
                           if item.size_bytes > 0 and getattr(downloader, "_total_size", None) is None:
                               downloader._total_size = item.size_bytes
                   done = False
                   while not done:
                       status, done = downloader.next_chunk(num_retries=3)
                       if status and on_progress:
                           on_progress(float(status.progress()) * 100.0)

               # Handle fallback if a server or mock ignored Range and wrote the full archive after `offset`
               if offset > 0 and tmp_path.exists():
                   total_written = tmp_path.stat().st_size
                   tail_len = total_written - offset
                   if expected_md5 and self._compute_md5(tmp_path) != expected_md5:
                       if tail_len > 0 and self._compute_md5(tmp_path, offset=offset) == expected_md5:
                           with open(tmp_path, "rb") as rf:
                               rf.seek(offset)
                               tail_bytes = rf.read()
                           tmp_path.write_bytes(tail_bytes)
                   elif not expected_md5 and item.size_bytes > 0 and total_written == offset + item.size_bytes:
                       with open(tmp_path, "rb") as rf:
                           rf.seek(offset)
                           tail_bytes = rf.read()
                       tmp_path.write_bytes(tail_bytes)

               if expected_md5:
                   actual_md5 = self._compute_md5(tmp_path)
                   if actual_md5.lower() != expected_md5:
                       tmp_path.unlink(missing_ok=True)
                       raise ValueError(
                           f"MD5 checksum mismatch for {item.name}: expected {expected_md5}, got {actual_md5}"
                       )

               os.replace(tmp_path, dest_path)
               return dest_path
           except Exception as exc:
               if isinstance(exc, ValueError) and "MD5 checksum mismatch" in str(exc):
                   tmp_path.unlink(missing_ok=True)
               if attempt >= max_retries:
                   raise
               sleep_s = min(60, 2**attempt)
               logger.warning(
                   "Drive download attempt %d/%d failed for %s (%s); retrying in %ds",
                   attempt,
                   max_retries,
                   item.name,
                   exc,
                   sleep_s,
               )
               time.sleep(sleep_s)

       return dest_path
   ```

---

### 2.2 `src/gphotos_synology/sources/inbox.py` (`LocalInboxScanner`)

#### Current State (Lines 1–111)
- Line 32: `self._seen_sizes: dict[str, tuple[int, float]] = {}` tracks only `(st_size, first_seen_monotonic)`.
- Line 48: `if not entry.is_file(): continue` follows symlinks because `entry.is_symlink()` is not checked first.
- Lines 63–69: stability check only compares `prev[0] != stat.st_size` (ignores `stat.st_mtime_ns`) and does not verify archive readability (`zipfile.is_zipfile` / tar.gz header check).

#### Blueprint for `src/gphotos_synology/sources/inbox.py`

1. **Imports (Lines 5–11)**:
   Add `import tarfile` and `import zipfile`:
   ```python
   import hashlib
   import os
   import shutil
   import tarfile
   import time
   import zipfile
   from dataclasses import dataclass
   from pathlib import Path
   ```

2. **`__init__` and `_is_archive_readable` (Lines 30–37)**:
   Track `(st_size, st_mtime_ns, since_monotonic)` in `self._seen_sizes: dict[str, tuple[int, int, float]] = {}` and add `_is_archive_readable`:
   ```python
   def __init__(self, settings: Settings) -> None:
       self.settings = settings
       self._seen_sizes: dict[str, tuple[int, int, float]] = {}

   def _fingerprint(self, path: Path, size: int, mtime_ns: int) -> str:
       raw = f"{path.name}:{size}:{mtime_ns}".encode()
       return hashlib.sha256(raw).hexdigest()[:24]

   @staticmethod
   def _is_archive_readable(path: Path) -> bool:
       """Verify that a candidate archive has a valid, readable ZIP or GZIP/TAR structure."""
       lower = path.name.lower()
       try:
           if path.stat().st_size <= 0:
               return False
           if lower.endswith(".zip"):
               return bool(zipfile.is_zipfile(path))
           if lower.endswith(".tgz") or lower.endswith(".tar.gz"):
               with open(path, "rb") as raw:
                   if raw.read(2) != b"\x1f\x8b":
                       return False
               with tarfile.open(path, "r:gz") as tf:
                   tf.next()
               return True
       except (tarfile.TarError, zipfile.BadZipFile, EOFError, OSError):
           return False
       return False
   ```

3. **Symlink Skip, `(st_size, st_mtime_ns)` Stability & Readability Check in `scan_ready_archives` (Lines 38–86)**:
   ```python
   def scan_ready_archives(self) -> list[InboxArchiveItem]:
       """Return archives in INBOX_DIR whose size and mtime_ns are stable and structure is readable."""
       inbox = self.settings.inbox_dir
       inbox.mkdir(parents=True, exist_ok=True)

       now = time.monotonic()
       wall_now = time.time()
       ready: list[InboxArchiveItem] = []
       active_keys: set[str] = set()

       for entry in sorted(inbox.iterdir(), key=lambda p: p.name.lower()):
           if entry.is_symlink():
               continue
           if not entry.is_file():
               continue
           if entry.name.startswith(".") or entry.name.endswith(".part"):
               continue
           if not is_supported_archive(entry.name):
               continue

           try:
               stat = entry.stat()
           except OSError:
               continue

           key = str(entry.resolve())
           active_keys.add(key)

           if self.settings.inbox_stability_seconds > 0:
               prev = self._seen_sizes.get(key)
               if prev is None:
                   self._seen_sizes[key] = (stat.st_size, stat.st_mtime_ns, now)
                   continue
               if prev[0] != stat.st_size or prev[1] != stat.st_mtime_ns:
                   # Estimate monotonic time of last inode modification between prev[2] and now
                   ctime_age = max(0.0, wall_now - (stat.st_ctime_ns / 1_000_000_000))
                   change_mono = max(prev[2], now - ctime_age)
                   self._seen_sizes[key] = (stat.st_size, stat.st_mtime_ns, change_mono)
                   continue
               if (now - prev[2]) < self.settings.inbox_stability_seconds:
                   continue

           if not self._is_archive_readable(entry):
               continue

           ready.append(
               InboxArchiveItem(
                   path=entry,
                   source_id=self._fingerprint(entry, stat.st_size, stat.st_mtime_ns),
                   name=entry.name,
                   size_bytes=stat.st_size,
                   batch_key=extract_batch_key(entry.name),
               )
           )

       # Clean up tracking entries for files no longer in inbox
       for stale in list(self._seen_sizes.keys()):
           if stale not in active_keys:
               self._seen_sizes.pop(stale, None)

       return ready
   ```

---

### 2.3 `src/gphotos_synology/synology/photos_api.py` (`SynologyPhotosClient`)

#### Current State (Lines 1–305)
- Lines 32–44 (`__init__`): passes `verify=settings.synology_verify_ssl` without checking `settings.synology_ca_cert`.
- Lines 66–100 (`_entry_request`): defaults to `use_post: bool = False`, sending `payload` (including `_sid`) in GET query parameters (`self._client.get("/webapi/entry.cgi", params=payload, ...)`), and does not handle error code `119` (session expired) or invalidate `_album_cache` on errors.
- Lines 102–138 (`login`, `logout`): does not clear `_album_cache` or `_folder_cache`, and lacks `invalidate_album_cache()`.
- Lines 266–304 (`resolve_item_id`): when `dest_rel_path` has no `/` folder prefix (`len(parts) == 1`), leaves `folder_id = None` and omits `folder_id` from `Item.list`, which in Synology Photos returns items from ALL folders and matches a same-named file inside a subfolder.

#### Blueprint for `src/gphotos_synology/synology/photos_api.py`

1. **`__init__` with `settings.synology_ca_cert` support & `invalidate_album_cache()` (Lines 32–61)**:
   ```python
   def __init__(self, settings: Settings, *, client: httpx.Client | None = None) -> None:
       self.settings = settings
       self.base_url = settings.synology_url.rstrip("/")
       self._owns_client = client is None
       ca_cert = str(getattr(settings, "synology_ca_cert", "") or "").strip()
       verify_opt: bool | str = ca_cert if ca_cert else settings.synology_verify_ssl
       self._client = client or httpx.Client(
           base_url=self.base_url,
           verify=verify_opt,
           timeout=30.0,
       )
       self._sid: str | None = None
       self._syno_token: str | None = None
       self._folder_cache: dict[str, int] = {}
       self._album_cache: dict[str, int] | None = None

   def invalidate_album_cache(self) -> None:
       """Invalidate cached album list so the next lookup fetches fresh state from DSM."""
       self._album_cache = None
   ```

2. **POST `_sid` in form body, code `119` auto-relogin & cache invalidation in `_entry_request` (Lines 66–101)**:
   ```python
   def _entry_request(
       self,
       api: str,
       method: str,
       version: int = 1,
       params: dict[str, Any] | None = None,
       *,
       use_post: bool = True,
       _allow_relogin: bool = True,
   ) -> dict[str, Any]:
       payload: dict[str, Any] = {
           "api": api,
           "method": method,
           "version": version,
       }
       if params:
           payload.update(params)
       if self._sid:
           payload["_sid"] = self._sid

       headers: dict[str, str] = {}
       if self._syno_token:
           headers["X-SYNO-TOKEN"] = self._syno_token

       # Never allow _sid to travel in GET URL query parameters
       send_post = use_post or ("_sid" in payload)
       try:
           if send_post:
               resp = self._client.post("/webapi/entry.cgi", data=payload, headers=headers)
           else:
               resp = self._client.get("/webapi/entry.cgi", params=payload, headers=headers)
           resp.raise_for_status()
           body = resp.json()
       except Exception:
           self.invalidate_album_cache()
           raise

       if not body.get("success", False):
           err = body.get("error") or {}
           code = err.get("code")
           self.invalidate_album_cache()
           if (
               code == 119
               and _allow_relogin
               and api != "SYNO.API.Auth"
               and self.settings.synology_username
               and self.settings.synology_password
           ):
               logger.info(
                   "Synology DSM session expired (code=119) on %s.%s; re-logging in and retrying once",
                   api,
                   method,
               )
               self.login()
               return self._entry_request(
                   api,
                   method,
                   version=version,
                   params=params,
                   use_post=use_post,
                   _allow_relogin=False,
               )
           raise SynologyAPIError(f"Synology API {api}.{method} failed (code={code})", code=code)
       return body.get("data") or {}
   ```

3. **Cache invalidation on `login()` and `logout()` (Lines 102–138)**:
   ```python
   def login(self) -> None:
       """Authenticate against DSM 7 using SYNO.API.Auth."""
       if not self.settings.synology_username or not self.settings.synology_password:
           raise SynologyAPIError("Synology username and password are required.")

       self.invalidate_album_cache()
       self._folder_cache.clear()
       self._sid = None
       self._syno_token = None

       data = self._entry_request(
           "SYNO.API.Auth",
           "login",
           version=6,
           params={
               "account": self.settings.synology_username,
               "passwd": self.settings.synology_password,
               "session": "SynologyPhotos",
               "format": "sid",
               "enable_syno_token": "yes",
           },
           use_post=True,
           _allow_relogin=False,
       )
       self._sid = data.get("sid")
       self._syno_token = data.get("synotoken")
       if not self._sid:
           raise SynologyAPIError("Synology DSM login succeeded without returning a session ID.")

   def logout(self) -> None:
       if not self._sid:
           self.invalidate_album_cache()
           self._folder_cache.clear()
           return
       try:
           self._entry_request(
               "SYNO.API.Auth",
               "logout",
               version=6,
               params={"session": "SynologyPhotos"},
               use_post=True,
               _allow_relogin=False,
           )
       finally:
           self._sid = None
           self._syno_token = None
           self.invalidate_album_cache()
           self._folder_cache.clear()
   ```

4. **Root-level scoping in `resolve_root_folder_id` & `resolve_item_id` (Lines 234–305)**:
   Add `resolve_root_folder_id()` and update `resolve_item_id()`:
   ```python
   def resolve_root_folder_id(self) -> int | None:
       """Resolve the root folder ID for Personal or Shared Space via Folder.get."""
       if "" in self._folder_cache:
           return self._folder_cache[""]
       try:
           data = self._entry_request(
               f"{self._browse_prefix}.Folder",
               "get",
               version=1,
           )
           folder_obj = data.get("folder") or data
           if isinstance(folder_obj, dict) and folder_obj.get("id") is not None:
               root_id = int(folder_obj["id"])
               self._folder_cache[""] = root_id
               return root_id
       except SynologyAPIError:
           return None
       return None

   def resolve_item_id(self, dest_rel_path: str) -> int | None:
       """Locate an indexed photo's Synology item ID by its relative path (e.g. '2024/07/IMG_1.JPG')."""
       cleaned = dest_rel_path.strip("/")
       if not cleaned:
           return None
       parts = cleaned.rsplit("/", 1)
       is_root_lookup = len(parts) == 1
       if len(parts) == 2:
           folder_path, filename = parts
           folder_id = self.resolve_folder_id(folder_path)
           if folder_id is None:
               return None
       else:
           filename = parts[0]
           folder_id = self.resolve_root_folder_id()

       offset = 0
       limit = 500
       target_lower = filename.lower()

       while True:
           params: dict[str, Any] = {
               "offset": offset,
               "limit": limit,
           }
           if folder_id is not None:
               params["folder_id"] = folder_id

           data = self._entry_request(
               f"{self._browse_prefix}.Item",
               "list",
               version=1,
               params=params,
           )
           items = data.get("list") or []
           for item in items:
               if str(item.get("filename", "")).lower() != target_lower or item.get("id") is not None is False:
                   continue
               if item.get("id") is None:
                   continue
               item_fid = item.get("folder_id")
               if folder_id is not None and item_fid is not None and int(item_fid) != folder_id:
                   continue
               if is_root_lookup and folder_id is None and item_fid not in (None, 0, 1):
                   continue
               return int(item["id"])
           if len(items) < limit:
               break
           offset += limit

       return None
   ```

---

### 2.4 Unit Test Updates (`tests/test_synology_api_and_security.py` & Sources Tests)

1. **`tests/test_synology_api_and_security.py`**:
   - Existing `test_synology_photos_client_login_resolve_and_album_add` (lines 26–84) already parses POST bodies via `httpx.QueryParams(request.content.decode("utf-8"))`.
   - Strengthen `test_synology_photos_client_login_resolve_and_album_add` to assert that every request after login uses `request.method == "POST"`, has `"_sid"` in form params (`params.get("_sid") == "SID123"`), and `"_sid" not in request.url.params`.
   - Add unit tests in `tests/test_synology_api_and_security.py` (or dedicated test functions) covering:
     - `SynologyPhotosClient` `settings.synology_ca_cert` wiring in `httpx.Client`.
     - Code `119` session-expiry auto-relogin once and transparent retry (and raising `SynologyAPIError(code=119)` if the retry also returns `119`).
     - `invalidate_album_cache()` and cache clearing on `login()`, `logout()`, and API error.
     - Root-level `resolve_item_id("IMG_1.JPG")` calling `Folder.get` to scope by root `folder_id` and not matching a subfolder item (`folder_id=11`).
     - `GoogleDriveClient`:
       - `_save_credentials` creating token with `0o600` permissions atomically and logging a `WARNING` if `os.chmod` raises `OSError`.
       - `connection_status()` returning `{"connected": True, "error": ""}` when valid and `{"connected": False, "error": "..."}` when token JSON is corrupt or `creds.refresh()` raises.
       - `find_takeout_folder_ids()` paginating across multiple `nextPageToken` pages.
       - `download_archive()` verifying `item.md5_checksum` (unlinking `.part` and raising `ValueError` on mismatch) and resuming from an existing `.part` file with `Range: bytes=<offset>-` when a previous attempt failed mid-stream.
     - `LocalInboxScanner`:
       - Skipping symlinks (`entry.is_symlink()`).
       - Resetting stability wait when `st_mtime_ns` changes even if `st_size` is unchanged.
       - Skipping truncated/unreadable `.zip` and `.tgz`/`.tar.gz` files until they pass archive readability checks.

# Domain 1 Survey Handoff — Auth, Security, API, Profiles, OAuth, Web UI, DSM CGIs, Config/Secrets

**Core Summary:** All 2 Criticals (C1, C2), 5 Domain-1 Majors (G3, G4, G5, G6, G14), and all targeted Minors/Nits from Bingo's `v1.1.0-0003` review (`Collab.md`) are confirmed in source with exact line references. Three high-impact implementation subtleties were uncovered during verification: (1) `app.js:65-72` spreads `...options` *after* `headers`, which silently drops default/CSRF headers whenever `options.headers` is passed; (2) `app.js:201,212` loads quarantine preview images via `<img src="api/upgrades/.../preview/...">`, which cannot attach custom auth headers in direct/Docker mode unless handled via session cookie or Blob fetch; and (3) in DSM 7 SPK mode, `proxy.cgi`/`oauth_callback.cgi` run under DSM's web server user while the daemon runs as package user `gphotos-migrator`, so any shared-secret file permissions must be carefully coordinated so the CGIs can read the secret without locking out the DSM UI.

---

## 1. Observation

### 1.1 File Layout & Module Boundaries (Domain 1)

| File | Lines | Responsibility |
|---|---:|---|
| `src/gphotos_synology/main.py` | 110 | `AppContext` dataclass, `create_app(base_settings, start_worker)`, FastAPI wiring (`include_router`, `/static` mount, `GET /`), `cli()` uvicorn runner |
| `src/gphotos_synology/config.py` | 255 | `Settings(BaseSettings)`, `MUTABLE_UI_FIELDS`, `SECRET_FIELDS`, `ConfigManager` (load/merge/update/mask `runtime_settings.json`) |
| `src/gphotos_synology/profiles.py` | 295 | `UserProfile`, `CreateProfileRequest`, `UpdateProfileRequest`, `ProfileContext`, `ProfileRegistry` (CRUD, `/homes` discovery, DSM user resolution) |
| `src/gphotos_synology/web/routes.py` | 435 | `create_api_router()` — all `/api/*` REST endpoints, `_profile_ctx`, `_public_base_url`, `_resolve_redirect_uri`, OAuth start/callback/disconnect |
| `src/gphotos_synology/web/static/app.js` | 773 | SPA dashboard logic: `urlFor` (CGI vs direct routing), `apiFetch`, `escapeHtml`, DOM renderers, profile picker, settings form, OAuth & update flows |
| `src/gphotos_synology/web/static/index.html` | 523 | Dashboard markup: user picker bar, banners, 4 tabs (`overview`, `upgrades`, `albums`, `settings`), Add/Manage Users modal |
| `src/gphotos_synology/web/static/styles.css` | 624 | Dashboard CSS |
| `src/gphotos_synology/synology/photos_api.py` | 305 | `SynologyAPIError`, `SynologyPhotosClient` (login/logout, album list/create/add, folder/item resolution against `/webapi/entry.cgi`) |
| `src/gphotos_synology/sources/gdrive.py` | 245 | `GoogleDriveClient` (OAuth2 scopes, authorization URL, code exchange, atomic token file write/disconnect, Drive polling/download/trash) |
| `synology-spk/ui/proxy.cgi` | 193 | DSM 7 CGI reverse proxy (`authenticate.cgi` auth, `?p=` query routing, header allowlist, HTML static asset URL rewrite, offline fallback) |
| `synology-spk/ui/oauth_callback.cgi` | 111 | DSM 7 query-free Google OAuth2 callback CGI (`authenticate.cgi` auth, forwards to `/api/oauth/google/callback`) |
| `synology-spk/ui/GPhotosMigrator.js` | 69 | Ext JS 3 DSM desktop window (`SYNO.SDS.GPhotosMigrator.AppWindow`) loading `iframe` to `/webman/3rdparty/gphotos-migrator/proxy.cgi` |
| `synology-spk/ui/config` | 25 | DSM desktop app registration (`"allUsers": true`, `"grantPrivilege": "all"`, `"advanceGrantPrivilege": true`) |
| `synology-spk/ui/index.conf` | 8 | DSM search/help index metadata |
| `synology-spk/ui/texts/enu/strings` | 4 | DSM stringset (`[app] app_name / description`) — exists on disk and is copied by `build_spk.py:228` |
| `synology-spk/build_spk.py` | 363 | SPK generator: `_start_stop_script` (`APP_HOST=127.0.0.1`), `conf/privilege` (`run-as: package`, `username: gphotos-migrator`), lifecycle scripts |
| `Dockerfile` / `docker-compose.yml` / `.env.example` | 41 / 43 / 77 | Docker `--host 0.0.0.0`, `"8765:8765"`, `HEALTHCHECK /api/health`, default `SYNOLOGY_URL` / `SYNOLOGY_VERIFY_SSL` |

Baseline verification run (`2026-11-21`): `.venv/bin/pytest -q` → **20 passed, 1 warning in 1.09s** (HEAD `dfcf34f`).

---

### 1.2 Detailed Findings by Audit ID

#### C1 — Unauthenticated API / Missing Shared Proxy Secret / Docker Exposure
- **`src/gphotos_synology/main.py:80-95`**: `create_app` attaches zero middleware and zero auth dependencies (`fastapi_app.include_router(create_api_router())`). Every `/api/*` route is unauthenticated.
- **`src/gphotos_synology/config.py:32-33`**: `app_host: str = Field(default="0.0.0.0")`, `app_port: int = Field(default=8765)`. No `api_token` or `proxy_secret` setting exists.
- **`Dockerfile:36-40`**:
  - Line 37: `HEALTHCHECK ... CMD curl -fsS http://127.0.0.1:8765/api/health || exit 1` → **`/api/health` MUST remain unauthenticated**.
  - Line 40: `CMD ["uvicorn", "gphotos_synology.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8765"]`.
- **`docker-compose.yml:6-7`**: `ports: ["8765:8765"]` binds all LAN interfaces instead of `"127.0.0.1:8765:8765"`.
- **`.github/workflows/docker-publish.yml:80-91`**: CI smoke test hits `GET http://127.0.0.1:18765/api/health` and `GET http://127.0.0.1:18765/` without auth headers → **`/api/health`, `/`, and `/static/*` MUST remain accessible** so the smoke test and initial HTML/JS load work.
- **`synology-spk/build_spk.py:133-152`**: `start_daemon` exports `APP_HOST="127.0.0.1"`, `APP_PORT="8765"`, `DATA_DIR="${PKG_VAR}/config"`, and starts `${BIN}` as user `gphotos-migrator`. No shared proxy secret is generated or passed to the daemon or CGIs.
- **`synology-spk/ui/proxy.cgi:148-162`** & **`synology-spk/ui/oauth_callback.cgi:79-86`**: Send `X-Proxy-Mode: cgi` and `X-DSM-User: <user>` with no shared secret authenticating the proxy to the backend.

#### C2 — Unenforced Server-Side DSM User Profile Isolation
- **`src/gphotos_synology/web/routes.py:33-38`**:
  ```python
  def _profile_ctx(request: Request, profile: str | None = None) -> ProfileContext:
      pid = profile or request.query_params.get("profile") or request.headers.get("x-profile-id") or "default"
      try:
          return _app_ctx(request).registry.get_context(pid)
  ```
  `X-DSM-User` is never checked against `pctx.profile.dsm_username`.
- **`src/gphotos_synology/web/routes.py:43-91`**:
  - `GET /api/profiles` (lines 43-55): Reads `dsm_user = request.headers.get("x-dsm-user", "").strip()` ONLY to compute `recommended_profile_id`; returns ALL profiles (`list_profiles()`) and ALL discovered `/homes` users (`discovery["users"]`).
  - `POST /api/profiles` (lines 57-69), `PATCH /api/profiles/{profile_id}` (lines 71-80), `DELETE /api/profiles/{profile_id}` (lines 82-91): No `X-DSM-User` authorization check. Any DSM user can create a profile pointing at another user's `/homes/<victim>/Photos`, disable/delete others' profiles, etc.
- **`src/gphotos_synology/web/routes.py:217-232,295-303`**: Global endpoints `GET /api/updates`, `POST /api/updates/apply`, `POST /api/actions/pause`, `POST /api/actions/resume` have no admin check in proxied mode.
- **`src/gphotos_synology/web/routes.py:379-425`**: `google_oauth_callback` does not check `X-DSM-User` against the target profile.
- **`synology-spk/ui/proxy.cgi:160-161`**: Forwards client-supplied `HTTP_X_PROFILE_ID` verbatim as `X-Profile-Id`. Does NOT compute or forward whether `dsm_user` is a DSM administrator.
- **`synology-spk/ui/config:9,12,13`**: `"allUsers": true`, `"grantPrivilege": "all"`, `"advanceGrantPrivilege": true` — grants every DSM user access to open the app window.
- **`src/gphotos_synology/profiles.py:212-221`**: `resolve_profile_for_dsm_user(self, dsm_username: str | None) -> str` returns the first matching `profile.dsm_username.lower() == target`, falling back to `"default"`.

#### G3 — OAuth Callback Fail-Open State Validation & Replay
- **`src/gphotos_synology/web/routes.py:365-377`** (`google_oauth_start`):
  - Line 371: `state = f"{pctx.profile.id}:{secrets.token_urlsafe(24)}"`
  - Line 372: `pctx.db.set_state("oauth_state", state)` — no expiry timestamp, no binding to `dsm_user` or `redirect_uri`.
- **`src/gphotos_synology/web/routes.py:379-425`** (`google_oauth_callback`):
  - Lines 393-400: Parses `profile_id = state.split(":", 1)[0]` from untrusted `state` *before* validating it; falls back silently to `"default"` if unknown.
  - Line 404: `RedirectResponse(url=f"{home_url}?profile={profile_id}&oauth=error&detail={error}", status_code=302)` — `error` (and `profile_id`) interpolated **without URL-encoding** (`urllib.parse.quote`).
  - Lines 408-411:
    ```python
    expected_state = pctx.db.get_state("oauth_state")
    if expected_state and state != expected_state:
    ```
    **Fails open** when `expected_state` is `None` or `""` (never started), AND uses `!=` instead of `hmac.compare_digest`.
  - Lines 413-425: Never deletes/invalidates `oauth_state` after use (replayable indefinitely); `StateDB` (`src/gphotos_synology/database.py:187-202`) has `get_state`/`set_state` but **no `delete_state` method**.
  - Line 414: Recomputes `redirect_uri = _resolve_redirect_uri(request, pctx)` from callback headers instead of using the `redirect_uri` bound at `start`.

#### G4 — `synology_url` Credential Exfiltration Chain
- **`src/gphotos_synology/config.py:158`**: `"synology_url"` is in `MUTABLE_UI_FIELDS`.
- **`src/gphotos_synology/config.py:211-246`**: `ConfigManager.update(patch)` lets a caller change `synology_url` while keeping the stored `synology_password` untouched (either by omitting `synology_password` or passing `"********"`).
- **`src/gphotos_synology/web/routes.py:313-329`**: `POST /api/actions/test-synology` calls `SynologyPhotosClient(settings).test_connection()`, which calls `login()` (`src/gphotos_synology/synology/photos_api.py:102-123`) and POSTs `account` + stored plaintext `passwd` to `{settings.synology_url}/webapi/entry.cgi`.
- **`src/gphotos_synology/web/static/app.js:439-448`**: `btn-test-synology` calls `saveSettings()` then `POST /api/actions/test-synology`.

#### G5 — `synology_verify_ssl` Default `False` & Plaintext HTTP Default URL
- **`src/gphotos_synology/config.py:69,73`**:
  - Line 69: `synology_url: str = Field(default="http://172.17.0.1:5000")`
  - Line 73: `synology_verify_ssl: bool = Field(default=False)`
- **`src/gphotos_synology/synology/photos_api.py:36-40`**: `httpx.Client(base_url=self.base_url, verify=settings.synology_verify_ssl, timeout=30.0)`.
- **`src/gphotos_synology/web/static/index.html:312,334-337`**: Placeholder is `http://172.17.0.1:5000`; `synology_verify_ssl` checkbox has no guidance on HTTPS/self-signed certs.
- **`.env.example:55,59`**: `SYNOLOGY_URL=http://host.docker.internal:5000`, `SYNOLOGY_VERIFY_SSL=false`.

#### G6 — Missing CSRF Defense & `app.js` `apiFetch` Spread Bug
- **`src/gphotos_synology/main.py` & `web/routes.py`**: No CSRF header (`X-Requested-With` / per-session token) or `Origin`/`Sec-Fetch-Site` check on `POST`/`PUT`/`PATCH`/`DELETE`.
- **`synology-spk/ui/proxy.cgi:148-162`**: Header allowlist forwards `CONTENT_TYPE`, `HTTP_ACCEPT`, `HTTP_X_PROFILE_ID` — does **not** forward `HTTP_X_REQUESTED_WITH` (or any CSRF header).
- **`src/gphotos_synology/web/static/app.js:64-78`**:
  ```js
  async function apiFetch(url, options = {}) {
    const resp = await fetch(urlFor(url), {
      headers: {
        "Content-Type": "application/json",
        "X-Profile-Id": activeProfileId,
        ...(options.headers || {}),
      },
      ...options,
    });
  ```
  1. Does not send `X-Requested-With: XMLHttpRequest` (or CSRF token).
  2. **Latent bug**: `...options` appears *after* `headers`, so any caller passing `options.headers` would overwrite the merged `headers` object and drop `Content-Type`, `X-Profile-Id`, and any CSRF header.

#### G14 — Personal-Space `custom_subfolder` Path Traversal
- **`src/gphotos_synology/profiles.py:229-248`**:
  - Personal branch (lines 229-238): Validates `username` for `/`, `\`, `..`, then executes `if custom_sub: photos_path = photos_path / custom_sub` with **zero validation** on `custom_sub` (`../../maria/Photos` escapes into another user's home).
  - Shared branch (lines 239-248): Only checks `if ".." in Path(custom_sub).parts:` — does not reject backslashes (`\`), symlinks, or verify `.resolve()` containment under the base directory.

#### Related Minors & Nits (Verified Line-by-Line)

1. **`delete_profile` orphans disk state + slug/path collisions (`src/gphotos_synology/profiles.py`)**:
   - `profiles.py:286-294`: `delete_profile` only pops `self._profiles` and `self._contexts` and saves `profiles.json`. Leaves `data_dir/profiles/<id>` (`state.db`, `google_token.json`, `runtime_settings.json`), `work_dir/profiles/<id>` (quarantine), and `inbox_dir/<id>` on disk. Recreating the same slug resurrects the old DB and live Google refresh token.
   - `profiles.py:141,250-256` vs `src/gphotos_synology/sources/inbox.py:97`: Non-default profile inbox is `base_settings.inbox_dir / profile.id`, while default profile `finalize_archive` moves processed archives to `base_settings.inbox_dir / "processed"`. Only `"default"` is reserved at line 251 (`"processed"` is not reserved).
   - `profiles.py:239-248`: Creating two shared-space profiles with empty (or identical) `custom_subfolder` (or colliding with `default` in shared mode) produces two profiles with identical `photos_dir` and separate DBs.

2. **Build-time snapshot OAuth credential inheritance (`src/gphotos_synology/profiles.py:127-138`)**:
   - `_build_context` copies `default_s.google_client_id`, `default_s.google_client_secret`, and `default_s.synology_url` into `base_dump` once at context creation. Updating/rotating credentials on `default` is never reflected in existing non-default profiles until process restart.

3. **Config & secrets resilience (`config.py`, `profiles.py`, `sources/gdrive.py`)**:
   - `config.py:200-208` & `224-230`: `except (json.JSONDecodeError, OSError): pass` silently discards corrupt `runtime_settings.json` with no log and no backup.
   - `config.py:239-243`: Writes `.tmp` with default umask (`write_text`) before `os.chmod(tmp_path, 0o600)`, and suppresses `OSError` silently (`contextlib.suppress(OSError)`).
   - `profiles.py:99-106,115`: `except (OSError, json.JSONDecodeError, ValueError): loaded = []` silently wipes `profiles.json` and overwrites it at line 115 with no log and no backup.
   - `sources/gdrive.py:96-103`: `_save_credentials` writes `google_token.tmp` with default umask before `os.chmod(tmp, 0o600)` and silently suppresses `OSError`.

4. **Stored XSS in `app.js`**:
   - `web/static/app.js:58-62`: `escapeHtml` uses `div.textContent = text; return div.innerHTML`, which does **not** escape `"` or `'`.
   - `web/static/app.js:238`: `<td title="${escapeHtml(a.error_message || a.filename)}">` — double-quoted attribute XSS sink.
   - Additional double-quoted attribute sinks using `escapeHtml`: `app.js:591` (`data-quick-user`), `app.js:604` (`data-del-profile`), `app.js:612` (`data-toggle-profile`), `app.js:646` (`option value`).

5. **`SynologyPhotosClient` (`src/gphotos_synology/synology/photos_api.py`)**:
   - Lines 66-93 (`_entry_request`): Sends `_sid` in URL query params (`params=payload`) whenever `use_post=False` (used by `logout` line 129, `list_albums` line 164, `_list_subfolders` line 226, `resolve_item_id` line 290).
   - Lines 96-100: No automatic re-login + single retry on DSM session-expired error code `119`.
   - Lines 44,155-199: `_album_cache` is never invalidated (not even on `logout()` or error).
   - Lines 266-304 (`resolve_item_id`): When `dest_rel_path` is a root-level filename (no `/`), `folder_id` is `None` and omitted from `SYNO.Foto.Browse.Item.list` params (lines 274-276, 287-288), causing Synology Photos to search all folders instead of root only.

6. **Google OAuth scope escalation & disconnect (`sources/gdrive.py`, `index.html`, `app.js`)**:
   - `sources/gdrive.py:24-25,46-50`: `gdrive_post_action == "trash"` escalates scope from `drive.readonly` to full `https://www.googleapis.com/auth/drive`. (`drive.file` cannot trash files created by Google Takeout, so `drive` is genuinely required when `trash` is selected.)
   - `web/static/index.html:273-280`: No UI notice explaining scope escalation or that changing to `trash` requires re-authorizing Google Drive.
   - `sources/gdrive.py:105-106` & `web/static/index.html:286-288` & `app.js:429-437`: Disconnect only deletes local `google_token.json` without revoking the refresh token at Google or notifying the user to revoke at `myaccount.google.com/permissions`.

7. **`proxy.cgi` & `oauth_callback.cgi` robustness & `_public_base_url` header trust**:
   - `synology-spk/ui/proxy.cgi:138-141`: `CONTENT_LENGTH` read into RAM with no size cap (`sys.stdin.buffer.read(int(content_length_raw))`).
   - `synology-spk/ui/proxy.cgi:182-188`: Backend-offline handler returns HTTP `200` HTML for **every** method and path (including `PUT /api/settings`, `POST /api/...`). Friendly HTTP 200 HTML should apply ONLY to `GET /`; all other requests should return HTTP `503`.
   - `synology-spk/ui/proxy.cgi:55-65` & `oauth_callback.cgi:46-56`: `write_response` outputs upstream headers without CRLF (`\r`/`\n`) or NUL validation.
   - `synology-spk/ui/proxy.cgi:35-52` & `oauth_callback.cgi:33-43`: `get_dsm_user()` uses entire `(proc.stdout or "").strip()` with no last-non-empty-line extraction, no charset validation, and no `proc.returncode` check.
   - `synology-spk/ui/oauth_callback.cgi:69-72`: Forwards raw `QUERY_STRING` verbatim instead of allowlisting only `code`, `state`, `error` (and optional `error_description`).
   - `src/gphotos_synology/web/routes.py:349-363`: `_public_base_url` blindly trusts `X-Forwarded-Proto`, `X-Forwarded-Host`, `X-Forwarded-Prefix` from any client (including direct Docker clients) without verifying proxy trust or validating header syntax.

8. **Corrupt per-profile DB resilience at startup (`src/gphotos_synology/profiles.py`)**:
   - `profiles.py:111-114` & `150`: `_load_or_init()` calls `self._build_context(profile)` → `StateDB(cfg_mgr.settings.db_path)` with no per-profile `try/except`. A single corrupt `profiles/<id>/state.db` crashes the entire service on startup.

9. **Other minor verified fact**:
   - `synology-spk/ui/texts/enu/strings` *does* exist on disk and *is* copied into `package.tgz` by `build_spk.py:228` (`shutil.copytree(ROOT / "ui", ui_dst)`). However `synology-spk/ui/config:5,6,18` hardcodes English strings rather than referencing `"app:app_name"`.

---

### 1.3 Cross-Module Interface Contracts

These interfaces cross Domain 1 ↔ Domain 2 (pipeline/DB/scheduler) ↔ Domain 3 (SPK/Docker/CI/updater):

| Producer → Consumer | Exact Signature / Data Shape | Coupling Notes |
|---|---|---|
| `config.py` → all modules | `Settings` fields + properties (`db_path`, `token_path`, `runtime_config_path`, `quarantine_dir`, `ensure_directories()`, `is_photos_dir_writable()`), `ConfigManager.settings` (property), `ConfigManager.update(patch)`, `ConfigManager.to_public_dict()` | Adding lazy inheritance to `ConfigManager` MUST keep `.settings` returning a valid `Settings` instance seamlessly for `MigrationPipeline`, `GoogleDriveClient`, `SynologyPhotosClient`, `Notifier`, `UpdateService`. Changing `synology_url` / `synology_verify_ssl` defaults affects `.env.example`, `index.html`, and any test instantiating `Settings()`. |
| `database.py` → `web/routes.py` | `StateDB.get_state(key, default=None)`, `set_state(key, value)`, `log_event(level, component, message)`, `get_stats()`, `list_archives(limit)`, `list_albums_summary()`, `get_recent_logs(limit)`, `list_quality_upgrades(limit)`, `get_quality_upgrade(upgrade_id)` | Adding `delete_state(key: str) -> None` to `StateDB` enables atomic one-time OAuth state cleanup (G3) without schema changes. |
| `profiles.py` → `main.py`, `scheduler.py`, `routes.py` | `ProfileRegistry.get_context(profile_id) -> ProfileContext`, `list_profiles() -> list[dict]`, `enabled_contexts() -> list[ProfileContext]`, `discover_synology_users() -> dict`, `resolve_profile_for_dsm_user(dsm_username) -> str`, `create_profile(req)`, `update_profile(pid, req)`, `delete_profile(pid)` | `BackgroundScheduler._all_pipelines()` and `_resolve_targets()` call `registry.enabled_contexts()` and `registry.get_context(pid)` from the worker thread (`scheduler.py:73-76,109-117`). `delete_profile` MUST coordinate with active worker runs or tolerate a removed profile safely; corrupt-DB profiles skipped/degraded at startup must not break `enabled_contexts()`. |
| `pipeline.py` → `routes.py` | `pipeline.get_progress() -> dict`, `pipeline._set_progress(**kwargs)`, `pipeline.restore_quality_upgrade(upgrade_id) -> dict`, `pipeline.purge_quality_upgrade(upgrade_id) -> dict`, `pipeline.purge_all_quarantined() -> int` | Called directly from FastAPI request threads (`routes.py:118,196,204,211,309`). |
| `scheduler.py` → `routes.py` | `scheduler.is_running`, `scheduler.is_paused`, `scheduler.pause()`, `scheduler.resume()`, `scheduler.trigger_now(profile_id=..., albums_only=..., index_existing_only=...)` | Pause/resume are global across all profiles (must be admin-only in proxied multi-user mode). |
| `updater.py` → `routes.py` & `app.js` | `updater.get_status(settings, force=...) -> dict`, `updater.trigger_self_update(settings) -> dict` | `routes.py:217-232`; `apply_update` must be admin-only in proxied mode. |
| `photos_api.py` → `pipeline.py` & `routes.py` | `SynologyPhotosClient(settings, client=None)` context manager (`login`, `logout`, `close`, `test_connection`, `list_albums`, `get_or_create_album`, `add_items_to_album`, `resolve_folder_id`, `resolve_item_id`) | `tests/test_pipeline_e2e.py:15-50` defines `FakeSynologyPhotosClient` mirroring this interface; `tests/test_synology_api_and_security.py:29-33` inspects POST body or GET params on `/webapi/entry.cgi`. Switching `_entry_request` to POST stays compatible with both! |
| `build_spk.py` (`start-stop-status`) ↔ `proxy.cgi` / `oauth_callback.cgi` ↔ `main.py` | Env vars (`APP_HOST`, `APP_PORT`, `DATA_DIR`, `WORK_DIR`, `INBOX_DIR`, `PHOTOS_DIR`, `HOMES_DIR`, `GPHOTOS_PROXY_SECRET` / secret file) + HTTP headers (`X-Proxy-Secret`, `X-Proxy-Mode`, `X-DSM-User`, `X-DSM-Admin`, `X-Profile-Id`, `X-Requested-With`, `X-Forwarded-*`) | Tightest cross-domain contract (Domain 1 ↔ Domain 3). Both `build_spk.py` and the CGIs + FastAPI middleware must agree on exact header names, secret file location/permissions, and fallback behavior in direct/Docker/test modes. |

---

### 1.4 Existing Domain 1 Tests & Coverage Gaps

| Existing Test File | What It Covers | Exact Gaps |
|---|---|---|
| `tests/test_api.py` (70 lines, 1 test) | Happy-path `/`, `/api/health`, `/api/status`, `/api/actions/pause` & `resume`, `PUT /api/settings` secret masking & `"********"` sentinel | No C1 auth enforcement tests (missing/wrong secret/token → 401/403, `/api/health` & `/` exempt); no G6 CSRF header enforcement tests; no `MUTABLE_UI_FIELDS` allowlist rejection test (e.g. `photos_dir`, `watchtower_token`); no G4 `synology_url`-change password clearing/blocking test; no G3 OAuth start/callback tests (fail-closed missing state, state mismatch, replay, expiry, URL-encoded error); no preview traversal test; no `/api/updates` or `/api/actions/test-synology` tests; no `_public_base_url` untrusted `X-Forwarded-*` test. |
| `tests/test_profiles.py` (95 lines, 1 test) | Happy-path `/homes` discovery, profile create (`alex`, `maria`), initial OAuth ID inheritance, `X-DSM-User` recommendation, isolated pipeline import, patch/delete | No C2 isolation tests (non-admin DSM user blocked from reading/modifying/deleting another user's profile, blocked from global pause/resume/updates, admin override allowed); no G14 personal-space `custom_subfolder` `..`/symlink traversal test; no `delete_profile` disk/token/DB cleanup + re-creation non-resurrection test; no `"processed"` slug collision or duplicate shared-space `photos_dir` rejection test; no lazy OAuth credential rotation inheritance test; no corrupt `profiles.json` or corrupt per-profile `state.db` startup resilience test. |
| `tests/test_synology_api_and_security.py` (84 lines, 2 tests) | Zip-slip test + happy-path `SynologyPhotosClient` login/album/folder/item/logout | No assertion that `_sid` is sent in POST body (never URL query); no code `119` session-expiry auto-relogin & retry test; no album cache invalidation test; no root-level `resolve_item_id` scoping test; no G5 default `synology_verify_ssl=True` & HTTPS default URL test. |
| `tests/test_visual_quality_upgrade.py` (215 lines, 3 tests) | Includes `test_upgrades_api_preview_restore_and_purge` via `TestClient(app)` | Must remain compatible with C1/G6 auth & CSRF headers (via test helper or explicit test config). |
| *(Missing)* CGI tests | **Zero tests** | No tests for `proxy.cgi` or `oauth_callback.cgi`: DSM auth fail-closed, `authenticate.cgi` multi-line/invalid-charset rejection, `CONTENT_LENGTH` body cap (413), offline HTTP 200 for `GET /` vs HTTP 503 for `/api/*` and POST, CRLF header stripping/rejection, `X-Proxy-Secret` injection, `X-Requested-With` forwarding, `oauth_callback.cgi` query param allowlist. |
| *(Missing)* Config & Frontend tests | **Zero tests** | No direct `ConfigManager` tests (corrupt JSON log+backup, `0o600` creation, chmod warning); no `node` test for `app.js` `escapeHtml` quote escaping (XSS regression) or `apiFetch` header merging. |

---

## 2. Logic Chain

1. **C1 + C2 are the foundational security boundary** (Observations §1.2 C1, C2):
   - Today, neither direct callers nor proxied DSM users are authenticated/authorized by FastAPI.
   - Fixing C1 via a shared proxy secret (`X-Proxy-Secret`, constant-time verified via `hmac.compare_digest`) establishes **proxy authenticity**, which in turn makes `X-DSM-User`, `X-DSM-Admin`, `X-Proxy-Mode`, and `X-Forwarded-*` trustworthy inputs for C2, G3, and `_public_base_url`. Without C1, any local process or Docker LAN client can spoof `X-DSM-User: admin` or `X-Forwarded-Host`.
2. **Hidden Deployment Constraint #1 — DSM 7 CGI vs Daemon Secret Sharing**:
   - In SPK mode (`build_spk.py:133-152,310-314`), the daemon runs as package user `gphotos-migrator`, storing data in `/var/packages/gphotos-migrator/var`. Meanwhile `proxy.cgi` and `oauth_callback.cgi` are executed by DSM 7's web server (`synoscgi`/nginx) when a user opens the DSM app window.
   - If the secret file is written `0600` owned by `gphotos-migrator`, a CGI running as `http` (non-root) would get `PermissionError`, causing every DSM request to fail with 401/403!
   - Conversely, if `proxy.cgi` runs as `root` (common for DSM `webman/3rdparty` CGIs) or in a shared group, `0600`/`0640` works. To avoid breaking real NAS installs while still blocking LAN/browser/untrusted callers, `start-stop-status` should generate the secret file into `${PKG_VAR}/proxy_secret` (e.g., trying `chgrp http` + `chmod 0640`, falling back cleanly if `chgrp` fails while logging clearly), AND both CGIs should read the secret from `GPHOTOS_PROXY_SECRET` env var or `${PKG_VAR}/proxy_secret` (`/var/packages/gphotos-migrator/var/proxy_secret`). **Flag this explicitly for NAS smoke testing.**
3. **Hidden Deployment Constraint #2 — Direct/Docker Mode & `<img>` Previews**:
   - In Docker/direct mode, there is no `proxy.cgi`. Binding `docker-compose.yml` to `127.0.0.1:8765:8765` and `config.py` `app_host="127.0.0.1"` (with `Dockerfile` respecting `${APP_HOST:-0.0.0.0}` inside the container namespace so Docker port publishing still works!) protects the host network while allowing `docker-compose` port mapping (`127.0.0.1:8765:8765`) to reach the container!
   - **Important Docker networking note**: Inside a Docker container, binding uvicorn to `127.0.0.1` breaks Docker's `ports: ["127.0.0.1:8765:8765"]` bridge forwarding because Docker forwards from the bridge eth0 interface into the container! So:
     - `docker-compose.yml` MUST bind host port to `127.0.0.1:8765:8765` (loopback on the NAS host).
     - Inside the container, `uvicorn` must bind `0.0.0.0` (only reachable via the host loopback mapping), while **app-level auth** (`API_TOKEN` / session cookie or `GPHOTOS_PROXY_SECRET`) protects the API regardless of bind address!
     - In `config.py`, `app_host` default can be `"127.0.0.1"` (safe for local CLI/SPK), while `Dockerfile` keeps `--host 0.0.0.0` (or `APP_HOST=0.0.0.0` inside container) paired with `docker-compose.yml` `"127.0.0.1:8765:8765"` and app-level auth.
   - For `app.js:201,212` (`<img src="api/upgrades/.../preview/...">`):
     - In DSM CGI mode, `proxy.cgi` injects `X-Proxy-Secret` + `X-DSM-User` automatically on `GET` requests from `<img>`.
     - In Direct/Docker mode with `API_TOKEN` enabled, either: (a) support an `HttpOnly; SameSite=Strict` session cookie obtained via a lightweight login/token-exchange endpoint (which `<img>` sends automatically), OR (b) load preview images via `apiFetch` (`blob()` → `URL.createObjectURL`) so `Authorization: Bearer <token>` is attached. Option (a) or (b) prevents broken thumbnails in direct-auth mode!
4. **C2 Non-Admin UX vs Isolation**:
   - Because `synology-spk/ui/config:9` has `"allUsers": true`, a non-admin DSM user (e.g. `maria`) can open the app.
   - If `maria` has a profile with `dsm_username == "maria"`, she should ONLY see and access her own profile(s).
   - If `maria` does NOT yet have a profile, she must NOT fall back to `"default"` (Shared Space) or see other users' profiles or `/homes` listings. She should either be allowed to create only a `personal` profile for her own `dsm_username` (`maria`), or see an empty/self-only onboarding prompt while DSM administrators (`administrators` group or `DSM_ADMIN_USERS` allowlist) retain full access to `default` (Shared Space), all user profiles, `/homes` discovery, worker pause/resume, and updates.
5. **G3 + G4 + G6 Synergies**:
   - Storing OAuth pending states server-side (keyed by opaque random `state` token → `{profile_id, dsm_user, redirect_uri, created_at}` with 10-minute TTL, single-use pop/delete) eliminates all four G3 flaws at once: fail-open, untrusted profile parsing, replay, and header-derived `redirect_uri` tampering on callback.
   - Clearing `synology_password` (or rejecting the update with HTTP 400) whenever `synology_url` changes without a new plaintext password in the same `PUT /api/settings` breaks G4 cleanly without forcing zero-SSH NAS users to edit files manually to configure their NAS URL.
   - Requiring `X-Requested-With` on all mutating `/api/*` methods + forwarding `HTTP_X_REQUESTED_WITH` in `proxy.cgi` + fixing the `...options` spread order in `app.js:65-72` closes G6 cleanly across both DSM CGI and direct modes.

---

## 3. Caveats

1. **DSM 7 runtime behaviors not verifiable in this Linux workspace**:
   - Exact UID/GID under which DSM 7 `synoscgi` executes `/webman/3rdparty/gphotos-migrator/*.cgi` (affects `proxy_secret` file group/mode — must be tested on the user's NAS).
   - Exact `authenticate.cgi` exit code and stdout format across all DSM 7.x point releases (e.g. whether non-empty stdout always has exit code 0, and whether `/etc/group` `administrators` membership is readable by the CGI process).
   - Exact Synology Photos API behavior for `SYNO.Foto.Browse.Item.list` when targeting root-level files (`folder_id=0` vs fetching root folder ID via `SYNO.Foto.Browse.Folder` `get` vs filtering `item.get("folder_id")`). Need to handle both `folder_id` present and absent defensively in `photos_api.py` and test via `httpx.MockTransport`.
2. **Scope boundary**: Domain 2 (pipeline/G1/G2/extractor/G12/matcher/G9/metadata/G13/dedup/G10/G11/scanner/DB/scheduler/notifier) and Domain 3 (updater/G7/SPK lifecycle/G8/CI/lockfile/versioning) were only inspected at their interface boundaries with Domain 1.
3. **Existing tests impact**: Introducing C1 auth and G6 CSRF middleware will require updating `TestClient` helpers in `tests/test_api.py`, `tests/test_profiles.py`, and `tests/test_visual_quality_upgrade.py` (e.g., passing the test auth/CSRF headers or configuring test auth mode cleanly) while adding negative tests asserting unauthorized/CSRF-less requests are rejected.

---

## 4. Conclusion — Actionable Remediation & Module Decomposition

### Recommended Module Decomposition (Clean Boundaries for Parallel Implementers)

To minimize merge conflicts and keep security logic auditable and testable:

1. **New module `src/gphotos_synology/web/security.py`** (Auth + Isolation + CSRF + Proxy Trust):
   - Centralize `RequestIdentity(authenticated, via_proxy, dsm_user, is_admin)` extraction & FastAPI middleware/dependencies:
     - **C1**: Enforce shared proxy secret (`X-Proxy-Secret` vs `settings.proxy_secret`, constant-time) OR direct `API_TOKEN` (`Authorization: Bearer ...` / HttpOnly session cookie) on all `/api/*` except `/api/health`. In local/unconfigured dev mode, clearly define fail-closed behavior vs explicit opt-in (`ALLOW_INSECURE_NO_AUTH` or auto-generated token in `data_dir` / `proxy_secret`).
     - **G6**: Require `X-Requested-With` header (non-empty, e.g. `XMLHttpRequest`) on all mutating `/api/*` requests (`POST`, `PUT`, `PATCH`, `DELETE`), plus `Origin` check if present.
     - **C2**: Helper `authorize_profile_access(identity, profile)` and `require_admin(identity)` for `routes.py` (`default`/Shared Space, global `pause`/`resume`, `updates`, cross-user profile CRUD).
     - Trusted `_public_base_url(request, identity)`: Only honor `X-Forwarded-Proto/Host/Prefix` when `identity.via_proxy` (or `settings.trust_proxy_headers`) is True, with strict syntax validation.
2. **`src/gphotos_synology/config.py`** (Settings & Secrets Resilience):
   - Add `proxy_secret: str` (env `GPHOTOS_PROXY_SECRET`, or loaded from `proxy_secret_file`), `api_token: str`, `dsm_admin_users: str` (comma-separated override), `trust_proxy_headers: bool = False`, optional `synology_ca_cert: str = ""`.
   - **G5**: Default `synology_url = "https://127.0.0.1:5001"` and `synology_verify_ssl = True`; default `app_host = "127.0.0.1"`.
   - **G4**: In `ConfigManager.update()`, validate `synology_url` (`http://` or `https://` only, no control chars); if `synology_url` changes and a new plaintext `synology_password` (not `""` / `"********"`) is NOT supplied in the same patch, clear stored `synology_password` (or raise `ValueError` requiring password re-entry).
   - **Lazy inheritance**: Add optional `fallback_settings_provider: Callable[[], Settings] | None = None` to `ConfigManager` so non-default profiles dynamically inherit unset `google_client_id` / `google_client_secret` (and `synology_url` only if unset).
   - **Resilience**: On corrupt `runtime_settings.json`, log ERROR and move/copy to `runtime_settings.json.corrupt.<ts>`; write `.tmp` with `os.open(..., 0o600)` and log WARNING if `chmod` fails.
   - Also keep `SECRET_FIELDS` updated (`proxy_secret`, `api_token`) and ensure neither is in `MUTABLE_UI_FIELDS`.
3. **`src/gphotos_synology/profiles.py`** (Profile Containment, Cleanup, Resilience):
   - **G14**: Extract `_resolve_contained_subfolder(base_dir: Path, custom_sub: str) -> Path` and apply to BOTH personal and shared branches; strengthen `dsm_username` validation + `.resolve().is_relative_to(homes_dir.resolve())`.
   - **Minors**:
     - Reserve profile IDs `{"default", "processed", "profiles"}`.
     - Reject creating a profile whose resolved `photos_dir` collides with an existing profile's `photos_dir`.
     - `delete_profile`: Delete `data_dir/profiles/<id>` (DB, `google_token.json`, `runtime_settings.json`), `work_dir/profiles/<id>`, and `inbox_dir/<id>` (never `photos_dir`); check/guard against deleting while active if needed.
     - Backup + log corrupt `profiles.json` before fallback.
     - Wrap per-profile `_build_context()` in `_load_or_init()` with `try/except Exception` so one corrupt profile DB does not crash startup (record error / mark profile unavailable).
     - Wire lazy OAuth fallback provider into non-default `ConfigManager` instances.
4. **`src/gphotos_synology/database.py`** (Tiny Domain 1 hook — coordinate with Domain 2):
   - Add `delete_state(self, key: str) -> None` alongside `get_state`/`set_state`.
5. **`src/gphotos_synology/web/routes.py` & `main.py`**:
   - Wire `web/security.py` middleware & dependencies in `main.py` and `routes.py`.
   - Enforce C2 across `/api/profiles*`, `_profile_ctx`, `/api/updates*`, `/api/actions/pause|resume`, and `/api/oauth/google/callback`.
   - **G3**: Implement central server-side OAuth state store (opaque random token, TTL 600s, stores `profile_id`, `dsm_user`, `redirect_uri`, `created_at`), fail closed on missing/mismatched/expired state using `hmac.compare_digest`, pop/clear one-time on callback, URL-encode `error` & `profile_id` via `urllib.parse.quote`.
6. **`src/gphotos_synology/sources/gdrive.py`**:
   - Atomic `0o600` token creation (`os.open(..., 0o600)`) + log warning on `chmod` failure.
   - Optional best-effort token revocation on `disconnect()`.
7. **`src/gphotos_synology/synology/photos_api.py`**:
   - Default authenticated `_entry_request` calls to `use_post=True` so `_sid` never enters URL query strings.
   - Handle DSM code `119` (session expired) with single re-login + retry.
   - Add `invalidate_cache()` and clear caches on `logout()` / re-login / album-missing error.
   - Scope root-level `resolve_item_id` (no `/`) so subfolder items with the same filename cannot match.
8. **`src/gphotos_synology/web/static/app.js` & `index.html`**:
   - Fix `escapeHtml` to escape `&`, `<`, `>`, `"`, `'` (eliminates stored XSS in `renderArchives` and modal attributes).
   - Fix `apiFetch` `...options` spread order and attach `X-Requested-With: XMLHttpRequest` (G6) (+ auth token/cookie handling for direct mode, including upgrade preview image loading).
   - Handle non-admin DSM view cleanly (hide global pause/updates/other users when `is_admin === false`).
   - Add UI notices: Google Drive `trash` scope escalation + re-auth notice; Google Disconnect token revocation note; `synology_url` HTTPS placeholder + `synology_verify_ssl` self-signed cert note.
9. **`synology-spk/ui/proxy.cgi` & `oauth_callback.cgi`** (coordinate with Domain 3 `build_spk.py`):
   - Share/inject `X-Proxy-Secret` (read from env or `${PKG_VAR}/proxy_secret`), compute & inject `X-DSM-Admin` (`1`/`0` via `grp.getgrnam("administrators")` or test env var), forward `HTTP_X_REQUESTED_WITH`.
   - Harden `get_dsm_user()`: parse last non-empty line, validate strict username regex, check returncode.
   - Cap `CONTENT_LENGTH` (e.g. 1 MiB → HTTP `413`).
   - Backend offline: HTTP `200` friendly HTML ONLY for `GET /`; HTTP `503` JSON for `/api/*` and non-root/non-GET requests.
   - CRLF / NUL header sanitization in `write_response` in both CGIs.
   - `oauth_callback.cgi`: Allowlist only `code`, `state`, `error` query params.
10. **Docker config (`Dockerfile`, `docker-compose.yml`, `.env.example`)** (coordinate with Domain 3):
    - `docker-compose.yml`: `"127.0.0.1:8765:8765"`, document `API_TOKEN` / `GPHOTOS_PROXY_SECRET`.
    - `.env.example`: Update `SYNOLOGY_URL=https://host.docker.internal:5001`, `SYNOLOGY_VERIFY_SSL=true`, document `API_TOKEN`.

---

## 5. Verification Method

### Commands to Run After Implementation
```bash
# Python lint + unit/integration/security tests
.venv/bin/ruff check src tests synology-spk
.venv/bin/pytest -v

# Frontend syntax check (plus Node unit test for escapeHtml / apiFetch if added)
node --check src/gphotos_synology/web/static/app.js
node --check synology-spk/ui/GPhotosMigrator.js

# CGI syntax check (Python 3 stdlib only, no third-party imports)
python3 -m py_compile synology-spk/ui/proxy.cgi synology-spk/ui/oauth_callback.cgi
```

### Independent Verification Checklist
1. **C1**: Request `/api/status` without secret/token → `401`/`403`; with valid `X-Proxy-Secret` or `Bearer` token → `200`; `/api/health` and `/` without auth → `200`.
2. **C2**: With valid proxy secret + `X-DSM-User: maria` + `X-DSM-Admin: 0`: `GET /api/status` with `X-Profile-Id: alex` → `403`; `DELETE /api/profiles/alex` → `403`; `POST /api/actions/pause` → `403`; `GET /api/profiles` only lists `maria`'s profile(s). With `X-DSM-Admin: 1`, cross-profile & global actions succeed.
3. **G3**: `GET /api/oauth/google/callback?code=x&state=alex:forged` on fresh profile (no prior `/start`) → error redirect (no token saved); replay valid state twice → second fails; expired state → fails; `error=foo&bar=baz` → URL-encoded in `Location` header.
4. **G4**: Set `synology_password="secret"` & `synology_url="https://nas:5001"`, then `PUT /api/settings` changing `synology_url="https://attacker.invalid"` with `synology_password="********"` (or omitted) → password cleared or request rejected; `test-synology` never sends `"secret"` to attacker URL.
5. **G5**: `Settings().synology_verify_ssl is True` and `Settings().synology_url.startswith("https://")`.
6. **G6**: `POST /api/actions/pause` (authenticated) without `X-Requested-With` → `403`; with `X-Requested-With: XMLHttpRequest` → `200`.
7. **G14**: `POST /api/profiles` with `target_space="personal"`, `dsm_username="alex"`, `custom_subfolder="../../maria/Photos"` → `400`.
8. **Minors/Nits**:
   - `delete_profile("maria")` deletes `data_dir/profiles/maria`, `work_dir/profiles/maria`, `inbox_dir/maria`, leaves `homes/maria/Photos` intact; recreating `maria` starts with empty DB and disconnected Google Drive.
   - Corrupt `runtime_settings.json` / `profiles.json` → backed up + logged. Corrupt non-default `state.db` → app still boots.
   - `SynologyPhotosClient`: assert `_sid` absent from all request URLs (all authenticated calls use POST body), code `119` triggers single re-login + retry, root-level `resolve_item_id("ROOT.JPG")` ignores subfolder `ROOT.JPG`.
   - CGI subprocess tests (`proxy.cgi` & `oauth_callback.cgi` against a local test HTTP server): body > cap → `413`; backend offline `GET /` → `200` HTML, `POST /api/settings` → `503`; CRLF header from backend stripped/rejected; `oauth_callback.cgi` drops non-allowlisted query params.
   - `escapeHtml('" onmouseover="alert(1)')` produces `&quot;` and `&#39;`.

### Invalidation Conditions
- This survey is invalidated if `HEAD` moves past `dfcf34f` or if `synology-spk/ui/*.cgi` / `web/routes.py` / `config.py` / `profiles.py` are modified before implementation begins.

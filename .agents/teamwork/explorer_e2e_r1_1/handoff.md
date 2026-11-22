# Explorer 1 Handoff — Security, Auth, API, Profiles, Web UI & CGI Public Interfaces (v1.2.0 Target)

**Scope:** Public constructors, HTTP API contracts, CGI subprocess protocols, `app.js` Node harness strategy, and edge cases for C1, C2, G3, G4, G5, G6, G14, and associated Minors in `PROJECT.md` and `Collab.md`.  
**Baseline Inspected:** `HEAD dfcf34f` (`v1.1.0-0003`); `src/gphotos_synology/web/security.py` does **not** exist yet (M3 is PLANNED).  
**Runtime Tooling Verified:** Python `3.11.2` (`.venv/bin/python`), `pytest 9.2.0`, system `/usr/bin/python3`, Node `v24.22.3`.

---

## 1. Public Constructors & Module Signatures

### 1.1 `src/gphotos_synology/main.py`
- `STATIC_DIR: Path` (`src/gphotos_synology/web/static`)
- `AppContext` (`@dataclass`):
  - `registry: ProfileRegistry`
  - `config_manager: ConfigManager`
  - `db: StateDB`
  - `pipeline: MigrationPipeline`
  - `scheduler: BackgroundScheduler`
  - `updater: UpdateService`
- `create_app(base_settings: Settings | None = None, *, start_worker: bool = True) -> FastAPI`
  - Exposes `app.state.ctx: AppContext`.
  - Mounts `/static` (`StaticFiles`), serves `GET /` (`index.html`), includes `/api` router (`create_api_router()`), and in v1.2.0 wires security middleware from `web/security.py`.
- `cli() -> None`: launches `uvicorn.run(create_app(base_settings=settings), host=settings.app_host, port=settings.app_port, reload=False)`.

### 1.2 `src/gphotos_synology/config.py`
- `Settings(BaseSettings)` (`env_file=".env"`, `extra="ignore"`):
  - **Key Storage/Server Fields (v1.2.0 Target Defaults):**
    - `data_dir: Path = Path("/data/config")`
    - `work_dir: Path = Path("/data/work")`
    - `inbox_dir: Path = Path("/data/inbox")`
    - `photos_dir: Path = Path("/data/photos")`
    - `homes_dir: Path = Path("/data/homes")`
    - `app_host: str = "127.0.0.1"` *(v1.1.0 was `"0.0.0.0"` — G5)*
    - `app_port: int = 8765`
    - `quarantine_dir_override: Path | None = None` *(new v1.2.0 — G8; `Settings.quarantine_dir` returns override if set else `work_dir / "replaced_duplicates"`)*
  - **Security & Auth Fields (new v1.2.0 — C1/C2/G5):**
    - `proxy_secret: str` (loaded from `GPHOTOS_PROXY_SECRET` env or `proxy_secret_file`)
    - `proxy_secret_file: Path | str` (from `PROXY_SECRET_FILE` env)
    - `api_token: str` (from `API_TOKEN` env; if neither `proxy_secret`, `api_token`, nor `allow_insecure_no_auth=True` is set, auto-generated at `data_dir / "api.token"` with `0o600`)
    - `allow_insecure_no_auth: bool = False`
    - `dsm_admin_users: ...` (list/CSV of DSM usernames treated as admin in addition to `X-DSM-Admin: 1`)
    - `trust_proxy_headers: bool = False` (controls trusting `X-Forwarded-*` outside verified `X-Proxy-Secret`)
  - **Synology TLS Fields (v1.2.0 Target Defaults — G5):**
    - `synology_url: str = "https://127.0.0.1:5001"` *(v1.1.0 was `"http://172.17.0.1:5000"`)*
    - `synology_verify_ssl: bool = True` *(v1.1.0 was `False`)*
    - `synology_ca_cert: str = ""` *(new v1.2.0 optional CA bundle/cert path)*
  - **Properties:** `db_path`, `token_path`, `runtime_config_path`, `quarantine_dir`.
  - **Methods:** `ensure_directories() -> None`, `is_photos_dir_writable() -> bool`.
- `SECRET_FIELDS: set[str]` (v1.2.0 Target):
  - Must include: `google_client_secret`, `synology_password`, `smtp_password`, `watchtower_token`, `proxy_secret`, `api_token`.
  - `to_public_dict()` masks any non-empty secret field as `"********"`.
- `MUTABLE_UI_FIELDS: set[str]` (v1.2.0 Target):
  - Must **exclude**: `proxy_secret`, `proxy_secret_file`, `api_token`, `dsm_admin_users`, `trust_proxy_headers`, `allow_insecure_no_auth`, `photos_dir`, `homes_dir`, `data_dir`, `work_dir`, `inbox_dir`, `quarantine_dir_override`, `watchtower_url`, `watchtower_token`, `github_repo`, `app_host`, `app_port`, `exiftool_path`.
  - Note: `synology_ca_cert` may or may not be UI-mutable (check against path injection if mutable; verify non-sensitive fields work).
- `ConfigManager`:
  - **v1.2.0 Signature:** `ConfigManager(base_settings: Settings | None = None, fallback_settings_provider: Callable[[], Settings] | None = None)`
  - `.settings -> Settings`: dynamically inherits unset `google_client_id`, `google_client_secret` (and `synology_url` if still default/empty) from `fallback_settings_provider()`.
  - `.update(patch: dict[str, Any]) -> Settings` (records `self.last_update_warnings: list[str]`):
    - Filters to `MUTABLE_UI_FIELDS`; preserves stored secret when value is `"********"`.
    - **G4 URL validation:** rejects `synology_url` without `http://` or `https://` scheme or containing whitespace/control chars (`ValueError`).
    - **G4 credential exfiltration guard:** if `synology_url` changes from its prior value AND `patch` does not supply a new non-empty, non-`"********"` `synology_password`, clears stored `synology_password = ""` and logs/records a warning.
    - **Corrupt JSON resilience:** if `runtime_settings.json` is corrupt JSON, logs error/warning and backs up the corrupt file (e.g. `.corrupt*` sibling) before recovering defaults.
    - **Atomic `0o600` persistence:** temp file created/chmodded `0o600` (logging warning on chmod `OSError`) and atomically replaced via `os.replace`.
  - `.to_public_dict() -> dict[str, Any]`

### 1.3 `src/gphotos_synology/profiles.py`
- Models:
  - `UserProfile(id, display_name, dsm_username="", target_space="shared", photos_dir, enabled=True, is_default=False)`
  - `CreateProfileRequest(dsm_username="", display_name="", target_space="personal", custom_subfolder="")`
  - `UpdateProfileRequest(display_name=None, enabled=None)`
  - `ProfileContext(profile, config_manager, db, pipeline)`
- `ProfileRegistry(base_settings: Settings)`:
  - `get_context(profile_id: str | None = None) -> ProfileContext` (raises `KeyError`)
  - `list_profiles() -> list[dict[str, Any]]` (each entry includes `id`, `display_name`, `dsm_username`, `target_space`, `photos_dir`, `enabled`, `is_default`, `inbox_dir`, `stats`, `progress_state`)
  - `enabled_contexts() -> list[ProfileContext]`
  - `discover_synology_users() -> dict[str, Any]` (`{"homes_mounted": bool, "users": [...]}`; ignores `@eaDir`, `@tmp`, `#recycle`, `#snapshot`, `lost+found`, `guest`, dot-dirs)
  - `resolve_profile_for_dsm_user(dsm_username: str | None) -> str`
  - `create_profile(req: CreateProfileRequest) -> ProfileContext`
  - `update_profile(profile_id: str, req: UpdateProfileRequest) -> dict[str, Any]`
  - `delete_profile(profile_id: str) -> None`
- **v1.2.0 Target Contracts on `ProfileRegistry`:**
  - **G14 Path Containment:** Both `personal` (`homes_dir / username / "Photos"`) and `shared` (`base_settings.photos_dir`) resolve `custom_subfolder` via `_resolve_contained_subfolder` and reject `..`, absolute paths, backslash traversal, or symlinks escaping the base root (`ValueError` → HTTP `400`).
  - **Reserved Profile IDs:** `"default"`, `"processed"`, and `"profiles"` must never be assigned as a created profile's `id` (either rejected or disambiguated; must never collide with `inbox_dir/processed` or `data_dir/profiles`).
  - **Duplicate `photos_dir` Rejection:** Rejects creating a profile whose resolved `photos_dir` matches an existing profile's resolved `photos_dir` (`ValueError` → HTTP `400`).
  - **`delete_profile` Disk Cleanup:** Deletes the profile's isolated `data_dir` (`data_dir/profiles/<id>` — DB, `google_token.json`, `runtime_settings.json`), `work_dir` (`work_dir/profiles/<id>` — quarantine), and `inbox_dir` (`inbox_dir/<id>`), while leaving `photos_dir` untouched. Recreating the same slug must NOT resurrect old DB rows or old `google_token.json`.
  - **Lazy OAuth Inheritance:** Non-default profiles use `ConfigManager(..., fallback_settings_provider=...)` pointing to `"default"`'s live settings, so setting or rotating `google_client_id`/`google_client_secret` on `"default"` immediately propagates to existing profiles without restart.
  - **Corrupt `profiles.json` Backup/Recovery:** Logs warning/error and writes a backup copy of corrupt `profiles.json` instead of silently overwriting.
  - **Corrupt-DB Startup Resilience:** If a non-default profile's `state.db` is corrupt on startup, `ProfileRegistry` logs the error and continues initializing `"default"` and other healthy profiles instead of crashing app startup.

### 1.4 `src/gphotos_synology/web/security.py` *(New in M3)*
- `RequestIdentity(authenticated: bool, via_proxy: bool, dsm_user: str, is_admin: bool)`
- Middleware / dependencies enforcing:
  - **C1 Auth:**
    - Proxy mode: `X-Proxy-Secret` verified with `hmac.compare_digest` against configured `proxy_secret`. Only then are `X-Proxy-Mode`, `X-DSM-User`, `X-DSM-Admin`, and `X-Forwarded-*` trusted.
    - Direct/Docker mode: `Authorization: Bearer <token>`, `X-API-Token: <token>`, or `gphotos_session` cookie (`HttpOnly; SameSite=Strict` — see Edge Case §5.1 regarding OAuth callback).
    - Auto-generated token: when `proxy_secret == ""` and `api_token == ""` and `allow_insecure_no_auth is False`, a token is created at `data_dir / "api.token"` (`0o600`).
    - Failure returns HTTP `401`.
  - **G6 CSRF:**
    - All mutating `/api/*` methods (`POST`, `PUT`, `PATCH`, `DELETE`), including `POST /api/auth/session`, require a non-empty `X-Requested-With` header. Missing/empty returns HTTP `403`.
  - **C2 DSM Profile Isolation & Admin Gate:**
    - Non-admin proxied DSM user (`via_proxy=True, is_admin=False`) can only access profiles where `profile.dsm_username.lower() == dsm_user.lower()`. Requesting another profile via `?profile=`, `X-Profile-Id`, or path param returns HTTP `403`.
    - Admin-only routes return HTTP `403` for non-admin proxied DSM users.

---

## 2. Complete HTTP Route Contract (Target v1.2.0)

**Auth Legend:**
- `Exempt` = No auth token/proxy secret required.
- `Auth` = Valid `X-Proxy-Secret` OR valid direct token (`Authorization: Bearer`, `X-API-Token`, or `gphotos_session` cookie). Unauthenticated → `401`.
- `CSRF` = Requires non-empty `X-Requested-With` header (missing → `403`).
- `Profile` = Subject to C2 ownership enforcement for non-admin DSM users (cross-profile → `403`).
- `Admin` = Requires `is_admin=True` (non-admin DSM user → `403`).

| Method | Path | Auth | CSRF | Scope / Gate | Expected Status Codes & Key JSON Fields (v1.2.0) |
|---|---|---|---|---|---|
| `GET` | `/` | Exempt | No | Public | `200` HTML (`index.html`) |
| `GET` | `/static/{path}` | Exempt | No | Public | `200` static asset (`app.js`, `styles.css`), `404` |
| `GET` | `/api/health` | Exempt | No | Public | `200` `{"status": "ok", "build_sha", "worker_running", "worker_paused", "exiftool_available", "work_free_gb", "photos_free_gb"}` |
| `GET` | `/api/auth/status` | Exempt | No | Public | `200` auth mode/status payload (e.g. `authenticated`, `via_proxy`, `auth_required`/`mode`, `dsm_user`, `is_admin`) |
| `POST` | `/api/auth/session` | Exempt | **Yes** | Login | Body `{"token": "..."}`. `200` sets `Set-Cookie: gphotos_session=...; HttpOnly; SameSite=Strict` (or `Lax`); invalid token → `401`; missing CSRF header → `403` |
| `GET` | `/api/profiles` | Auth | No | Filtered by C2 | `200` `{"profiles", "current_dsm_user", "dsm_user", "is_admin", "recommended_profile_id", "homes_mounted", "discovered_synology_users"}`. Non-admin sees only own profile(s) & empty/self-only `discovered_synology_users`. |
| `POST` | `/api/profiles` | Auth | **Yes** | **Admin** | Body `CreateProfileRequest`. `200` `{"ok": true, "profile": {...}}`; traversal/duplicate/invalid → `400`; non-admin → `403`. |
| `PATCH` | `/api/profiles/{profile_id}` | Auth | **Yes** | **Admin** | Body `UpdateProfileRequest`. `200` `{"ok": true, "profile": {...}}`; missing → `404`; invalid → `400`; non-admin → `403`. |
| `DELETE` | `/api/profiles/{profile_id}` | Auth | **Yes** | **Admin** | `200` `{"ok": true}` (deletes disk state); `"default"` → `400`; missing → `404`; non-admin → `403`. |
| `GET` | `/api/status` | Auth | No | Profile | `200` `{"profile", "progress", "stats", "worker_running", "worker_paused", "gdrive_oauth_configured", "gdrive_connected", "exiftool_available", "synology_api_enabled", "photos_dir_writable", "disk": {...}}` (v1.2.0 may also include `gdrive_error` from `connection_status()`). `403` cross-profile; `404` unknown profile. |
| `GET` | `/api/archives` | Auth | No | Profile | `200` `{"archives": [...]}` (`limit` query param, max 200). |
| `GET` | `/api/albums` | Auth | No | Profile | `200` `{"albums": [...]}`. |
| `GET` | `/api/logs` | Auth | No | Profile | `200` `{"logs": [...]}` (`limit` query param, max 500). |
| `GET` | `/api/upgrades` | Auth | No | Profile | `200` `{"upgrades": [...]}` (`limit` query param, max 500). |
| `GET` | `/api/upgrades/{upgrade_id}/preview/{which}` | Auth | No | Profile | `which` in `("kept", "quarantined")`. `200` `image/jpeg`; invalid `which` → `400`; missing/traversal → `404`; unrenderable → `422`. |
| `POST` | `/api/upgrades/{upgrade_id}/restore` | Auth | **Yes** | Profile | `200` restore result dict; invalid status/traversal/missing → `400`. |
| `POST` | `/api/upgrades/{upgrade_id}/purge` | Auth | **Yes** | Profile | `200` purge result dict; invalid status (`!= "quarantined"`)/traversal → `400`. |
| `POST` | `/api/upgrades/purge-all` | Auth | **Yes** | Profile | `200` `{"ok": true, "purged": int}`. |
| `GET` | `/api/updates` | Auth | No | Global* | `200` updater status dict (`current_version`, `latest_version`, `update_available`, `spk_download_url`, `sha256_url`/checksums, `error`, etc.). *(See §5.2 on admin gating)* |
| `POST` | `/api/updates/apply` | Auth | **Yes** | **Admin** | `200` trigger result; unconfigured/failed → `400`; non-admin → `403`. |
| `GET` | `/api/settings` | Auth | No | Profile | `200` `{"settings": {...}}` (all `SECRET_FIELDS` masked as `"********"` when set). |
| `PUT` | `/api/settings` | Auth | **Yes** | Profile | Body `{"updates": {...}}`. `200` `{"ok": true, "settings": {...}}` (plus optional `"warnings": [...]` on G4 password clear); invalid URL/schema → `400`. Non-mutable fields ignored. |
| `POST` | `/api/actions/scan` | Auth | **Yes** | Profile | `200` `{"ok": true, "message": ...}`; worker paused → `409`. |
| `POST` | `/api/actions/index-existing` | Auth | **Yes** | Profile | `200` `{"ok": true, "message": ...}`; worker paused → `409`. |
| `POST` | `/api/actions/sync-albums` | Auth | **Yes** | Profile | `200` `{"ok": true, "message": ...}`; `synology_api_enabled=False` → `400`. |
| `POST` | `/api/actions/pause` | Auth | **Yes** | **Admin** | `200` `{"ok": true, "worker_paused": true}`; non-admin → `403`. |
| `POST` | `/api/actions/resume` | Auth | **Yes** | **Admin** | `200` `{"ok": true, "worker_paused": false}`; non-admin → `403`. |
| `POST` | `/api/actions/reset-renewal` | Auth | **Yes** | Profile | `200` `{"ok": true}`. |
| `POST` | `/api/actions/test-synology` | Auth | **Yes** | Profile | `200` connection result (`space`, `album_count`, ...); failure → `400`. |
| `POST` | `/api/actions/test-notification` | Auth | **Yes** | Profile | `200` `{"ok": true}`; unconfigured/failed → `400`. |
| `GET` | `/api/oauth/google/start` | Auth | No | Profile | `200` `{"authorization_url": str, "redirect_uri": str}`; missing client creds → `400`. Stores server-side state record (TTL 600s, bound `profile_id` & `redirect_uri`). |
| `GET` | `/api/oauth/google/callback` | Auth* | No | State-bound + C2 | `302` `RedirectResponse` to `{home_url}?profile=...&oauth=success` or `oauth=error&detail=<urlencoded>`. Fail-closed on missing/unknown/expired/replayed state; one-time `delete_state`; C2 ownership check if proxied non-admin. *(See §5.1)* |
| `POST` | `/api/oauth/google/disconnect` | Auth | **Yes** | Profile | `200` `{"ok": true}`. |

---

## 3. CGI Subprocess Invocation Protocol

Both CGI scripts (`synology-spk/ui/proxy.cgi` and `synology-spk/ui/oauth_callback.cgi`) are standalone executable Python 3 scripts (`#!/usr/bin/python3`) using only the Python standard library.

### 3.1 Environment Variables & Mocking Hooks

| Env Var | Used By | Purpose / v1.2.0 Contract |
|---|---|---|
| `GPHOTOS_BACKEND_URL` | both | Upstream backend base URL (default `http://127.0.0.1:8765`). Set to `http://127.0.0.1:<ephemeral_port>` in E2E subprocess tests. |
| `GPHOTOS_PROXY_SECRET` | both | Shared secret value injected as `X-Proxy-Secret` header (preferred in tests). |
| `GPHOTOS_PROXY_SECRET_FILE` | both | Path to secret file (default `/var/packages/gphotos-migrator/var/proxy.secret`) used when `GPHOTOS_PROXY_SECRET` is unset. |
| `GPHOTOS_SKIP_DSM_AUTH` | both | Test-only escape hatch (`"1"`) used **only** when `/usr/syno/synoman/webman/authenticate.cgi` does not exist on disk. When unset and `AUTH_CGI` is absent, both CGIs MUST fail closed with `Status: 403`. |
| `GPHOTOS_TEST_DSM_USER` | both | Username returned when `GPHOTOS_SKIP_DSM_AUTH=1` (default `"admin"`). Must still pass username charset validation `^[A-Za-z0-9._@-]{1,64}$`! |
| `GPHOTOS_TEST_DSM_ADMIN` | both | `"1"` or `"0"` injected as `X-DSM-Admin` in test mode (otherwise computed via `grp.getgrnam("administrators")`). |
| `GPHOTOS_AUTH_CGI` *(check/note)* | both | Currently `AUTH_CGI = "/usr/syno/synoman/webman/authenticate.cgi"` is hardcoded. If M3 adds an env override for `AUTH_CGI`, E2E can point it at a temp stub script to test multiline/timeout/invalid stdout parsing directly via subprocess; otherwise test fail-closed + `GPHOTOS_TEST_DSM_USER` charset rejection via subprocess. |
| `REQUEST_METHOD` | `proxy.cgi` | HTTP verb (`GET`, `POST`, `PUT`, `PATCH`, `DELETE`). `oauth_callback.cgi` is always upstream `GET`. |
| `QUERY_STRING` | both | For `proxy.cgi`: `p=/api/status&profile=alex` (`p` sets target path; remaining params forwarded). For `oauth_callback.cgi`: `code=...&state=...&error=...` (v1.2.0 allowlists ONLY `code`, `state`, `error`). |
| `PATH_INFO` | `proxy.cgi` | Fallback target path if `p` query param is omitted. |
| `CONTENT_LENGTH`, `CONTENT_TYPE` | `proxy.cgi` | Request body metadata; body bytes fed via subprocess `stdin`. |
| `HTTP_HOST`, `HTTPS`, `SERVER_PORT` | both | Used to construct `X-Forwarded-Proto` (`https` if `HTTPS in ("on","1","true")` or `SERVER_PORT == "5001"`), `X-Forwarded-Host`, and `X-Forwarded-Prefix: /webman/3rdparty/gphotos-migrator`. |
| `HTTP_ACCEPT` | `proxy.cgi` | Forwarded as `Accept`. |
| `HTTP_X_PROFILE_ID` | `proxy.cgi` | Forwarded as `X-Profile-Id`. |
| `HTTP_X_REQUESTED_WITH` | `proxy.cgi` | **v1.2.0 G6:** Must be forwarded as `X-Requested-With`. |

### 3.2 CGI Wire Output Format & Required v1.2.0 Behaviors
- **Stdout format:**
  ```text
  Status: <int>\r\n
  <Header-Name>: <Header-Value>\r\n
  Content-Length: <len(body)>\r\n
  \r\n
  <raw body bytes>
  ```
- **Hop-by-hop headers stripped:** `connection`, `keep-alive`, `proxy-authenticate`, `proxy-authorization`, `te`, `trailers`, `transfer-encoding`, `upgrade`, `content-length`.
- **Headers NEVER forwarded from client:** `Cookie`, `Authorization`, `X-DSM-User`, `X-DSM-Admin`, `X-Proxy-Secret`, `X-Proxy-Mode` (even if supplied via `HTTP_*` env vars, CGI must ignore/overwrite them).
- **1 MiB Body Cap (`proxy.cgi`):** `CONTENT_LENGTH > 1_048_576` (1 MiB) MUST return `Status: 413` without reading stdin into memory. Invalid/non-numeric or negative `CONTENT_LENGTH` must be handled safely (e.g., `400`/`413` or ignored safely without crash).
- **Backend Offline Behavior (`proxy.cgi`):**
  - `GET` with target path `/` → `Status: 200`, `Content-Type: text/html; charset=utf-8`, friendly "Starting Google Photos Migrator..." HTML.
  - Any `/api/*` path OR any non-`GET` request (`POST`, `PUT`, `PATCH`, `DELETE`) → `Status: 503`, `Content-Type: application/json`, JSON body (e.g., `{"detail": "..."}`).
- **CRLF Header Sanitization (both CGIs):**
  - Any upstream response header name/value containing `\r` or `\n` (or spoofed forwarded request header containing `\r`/`\n`) MUST be stripped or dropped so raw CGI stdout never contains injected headers or extra CRLF breaks.
- **Path Normalization (`proxy.cgi`):**
  - `p` containing `://` or `..` segments is neutralized (rewritten to `/` or rejected) so upstream URL is strictly `BACKEND_BASE + /...`.
- **HTML Static Asset Rewriting (`proxy.cgi`):**
  - On `target_path == "/"`, rewrites `href="static/styles.css"` → `href="proxy.cgi?p=/static/styles.css"` and `src="static/app.js"` → `src="proxy.cgi?p=/static/app.js"`.
- **Query Param Allowlist (`oauth_callback.cgi`):**
  - Forwards ONLY `code`, `state`, `error` query params to `/api/oauth/google/callback`; strips arbitrary extra params (e.g., `profile=victim&foo=bar`).
- **Hardened `get_dsm_user()` (both CGIs):**
  - Uses last non-empty line of `authenticate.cgi` stdout, validates regex `^[A-Za-z0-9._@-]{1,64}$`; invalid values return `None` → `Status: 403`.

---

## 4. `app.js` Structure & Opaque-Box Node Test Strategy

File: `src/gphotos_synology/web/static/app.js` (773 lines in baseline; Node `v24.22.3` available).

### 4.1 Structure
- Entire file is a single IIFE `(() => { ... })();` with no `module.exports` or ES exports.
- Key internal functions relevant to v1.2.0 E2E checks:
  - `urlFor(relPath)` (L11–21): switches between direct (`BASE_PATH + clean`) and CGI proxy (`proxy.cgi?p=/...`) based on `window.location.pathname.endsWith(".cgi")`.
  - `escapeHtml(text)` (L58–62):
    - **Baseline bug:** uses `div.textContent = text ?? ""; return div.innerHTML;` which leaves `"` and `'` unescaped!
    - **v1.2.0 target:** must escape `&`, `<`, `>`, `"`, `'` (preventing breakout in double-quoted `title="${escapeHtml(...)}"` at `renderArchives` L238 and `data-*="${escapeHtml(...)}"` attributes).
  - `apiFetch(url, options = {})` (L64–78):
    - **Baseline bug:** `...options` is placed **after** `headers: { ... }` (L71), so passing `options.headers` clobbers `"Content-Type"`, `"X-Profile-Id"`, and `"X-Requested-With"`. Also missing `"X-Requested-With"` completely.
    - **v1.2.0 target:** spreads `...options` before `headers`, always attaches `"X-Requested-With"` (non-empty), `"Content-Type": "application/json"`, `"X-Profile-Id": activeProfileId`, merged with `options.headers`.
  - `renderArchives(archives)` (L227–246): interpolates `a.error_message || a.filename` into `title="..."` and `<td>...</td>`.
  - `renderProfilesModal(data)` / `loadProfiles(selectOnInit)` (L581–655): v1.2.0 adds non-admin UX handling (`data.is_admin === false` hides/disables admin controls like manage users / pause / updates apply).
  - Auto-init IIFE at bottom (L766–771): calls `loadProfiles(true)`, then `Promise.all([loadSettings(), refreshDashboard(), checkUpdates(false)])`, then sets two `setInterval` timers.

### 4.2 Opaque-Box Execution in Node (`node -e` / helper `.mjs` runner)
1. **Syntax check:** `node --check src/gphotos_synology/web/static/app.js` and `node --check synology-spk/ui/GPhotosMigrator.js`.
2. **Behavioral DOM/Fetch harness using `node:vm`:**
   - Run `app.js` inside `vm.createContext(sandbox)` with minimal stubs for `window`, `document`, `fetch`, `localStorage`, `setInterval` (no-op returning id), `setTimeout`, `clearTimeout`, `URLSearchParams`, `console`.
   - **Crucial detail for `document.createElement("div")`:** Implement `textContent` setter / `innerHTML` getter to mirror real browser behavior (escaping `&`, `<`, `>` ONLY — **not** `"` or `'`). That way:
     - Against baseline `v1.1.0`, mocking `/api/archives` with `filename: 'evil" onmouseover="alert(1)'` renders `<td title="evil" onmouseover="alert(1)">` → **test fails**, catching the exact XSS bug!
     - Against fixed `v1.2.0`, `escapeHtml` escapes `"` and `'` → rendered `title="..."` contains `&quot;` (and `&#39;`/`&#x27;` for `'`), with zero attribute breakout → **test passes**!
   - **Testing `apiFetch` & CSRF headers via real button clicks / startup calls:**
     - Record every `(url, init)` passed to sandbox `fetch`.
     - Verify startup `GET /api/profiles` and `GET /api/status` send `X-Profile-Id`.
     - Simulate click on `$("btn-scan")`, `$("btn-toggle-pause")`, `$("settings-form")` submit, `$("btn-purge-all")`, etc., and assert every mutating `fetch` call includes non-empty `X-Requested-With`, `Content-Type: application/json`, and `X-Profile-Id`.
     - Also test CGI proxy URL routing by setting `window.location.pathname = "/webman/3rdparty/gphotos-migrator/proxy.cgi"` and verifying `fetch` URLs use `proxy.cgi?p=%2Fapi%2F...`!
     - To directly test `apiFetch` header-merge preservation when `options.headers` is supplied (in case no button passes custom headers), either exercise a UI flow that passes `options.headers` (e.g. auth token login flow if implemented in `app.js`) or inspect `apiFetch` behavior if exposed/reachable, plus assert `X-Requested-With` + `X-Profile-Id` survive on `PUT /api/settings` and `POST /api/actions/*`.

---

## 5. Gaps, Nuances & Edge Cases for Tiers 1–4 E2E Coverage

1. **OAuth Callback Auth & Cookie `SameSite` Interaction (C1 + G3):**
   - `PROJECT.md` specifies `gphotos_session` cookie with `HttpOnly; SameSite=Strict` and lists exempt paths as `GET /api/health`, `GET /`, `GET /static/*`, `GET /api/auth/status`, `POST /api/auth/session`.
   - In **CGI proxy mode**, `oauth_callback.cgi` sends `X-Proxy-Secret` + `X-DSM-User`, so `/api/oauth/google/callback` is authenticated.
   - In **direct/Docker mode**, a browser redirected from `accounts.google.com` back to `GET /api/oauth/google/callback?code=...&state=...` will NOT send a `SameSite=Strict` cookie on cross-site top-level navigation (only `SameSite=Lax` or an auth-exempt callback gated by the server-side cryptographic `state` token works seamlessly).
   - **E2E guidance:** Test `GET /api/oauth/google/callback` primarily via (a) authenticated CGI/proxy headers (`X-Proxy-Secret` + `X-DSM-User`) AND (b) direct mode with valid session/token headers, asserting fail-closed behavior on missing/unknown/expired/replayed state, bound `redirect_uri`, profile resolution from store (not state string), and URL-encoded `error` param. Do not hardcode whether an unauthenticated direct callback returns `401` vs `302 oauth=error` unless strictly checking fail-closed (no token written, no 200).

2. **`GET /api/updates` vs `POST /api/updates/apply` Admin Gating (C2):**
   - `POST /api/updates/apply` MUST return `403` for non-admin DSM users.
   - `GET /api/updates` is read-only (dashboard calls it on boot for all users); M3 might either allow read-only `GET /api/updates` for non-admins or restrict it to admins. **E2E guidance:** Strictly assert `403` on `POST /api/updates/apply`, `POST /api/actions/pause`, `POST /api/actions/resume`, `POST/PATCH/DELETE /api/profiles*` for non-admin DSM users.

3. **Non-Admin DSM User with No Own Profile vs Own Profile (C2):**
   - Case A: DSM user `alice` has profile `alice` (`dsm_username="alice"`), DSM user `bob` has profile `bob` (`dsm_username="bob"`).
     - `alice` (`X-DSM-Admin: 0`) calling `/api/status` or `/api/settings` with `X-Profile-Id: bob` OR `?profile=bob` OR `X-Profile-Id: default` MUST get `403`.
     - `alice` calling `/api/status` with `X-Profile-Id: alice` (or no profile header, auto-resolved) gets `200` for `alice`'s data.
     - `alice` calling `GET /api/profiles` sees ONLY `alice` (never `bob` or `default`), `is_admin: false`, and empty/self-only `discovered_synology_users`.
     - `admin` (`X-DSM-Admin: 1`) sees all profiles, `is_admin: true`, and can read/manage `alice`, `bob`, and `default`.
   - Case B: DSM user `guestuser` (`X-DSM-Admin: 0`) has NO matching profile → `GET /api/profiles` returns empty `profiles: []`; attempting to access `default` or `alice` returns `403`.
   - Case C: Spoofed `X-DSM-User: admin` + `X-DSM-Admin: 1` WITHOUT valid `X-Proxy-Secret` MUST be rejected (`401`) — proxy headers are NEVER trusted without valid `X-Proxy-Secret`.

4. **G4 Credential Exfiltration Chain (Full E2E Scenario):**
   - Set `synology_url="https://127.0.0.1:5001"`, `synology_username="dsmuser"`, `synology_password="top-secret-pw"`.
   - Update `synology_url` to a local mock HTTP server (`http://127.0.0.1:<mock_port>`) WITHOUT providing a new plaintext `synology_password` (both omitting `synology_password` AND sending `"********"` sentinel!).
   - Verify: (a) stored `synology_password` is now cleared (`""`, so `GET /api/settings` no longer shows `"********"`), AND (b) calling `POST /api/actions/test-synology` does NOT transmit `"top-secret-pw"` to the mock server!
   - Also verify: changing `synology_url` WHILE providing a brand-new plaintext password `"new-pw"` DOES retain `"new-pw"`, and sending invalid URL schemes (`ftp://evil`, `javascript:alert(1)`, `http://host\r\nX-Injected: 1`, `http://host with spaces`) returns HTTP `400`.

5. **G3 OAuth Replay, Expiry, Tampered Prefix & `error` Injection:**
   - Start OAuth for profile `alice` → capture `state` from `authorization_url`.
   - Test 1 (fail-open regression): Call callback on a fresh profile with NO prior `/start` and arbitrary `state=default:anything&code=abc` → MUST fail closed (no `exchange_code` call, `oauth=error`).
   - Test 2 (prefix spoofing): Call callback with `state=bob:<valid_alice_token>` or swapped prefix → MUST fail closed (never binds to `bob`).
   - Test 3 (one-time replay): Valid callback succeeds once → calling callback a second time with the EXACT same `state` MUST fail closed.
   - Test 4 (TTL 600s expiry): Backdate stored state timestamp >600s (or advance clock) → callback MUST fail closed.
   - Test 5 (`error` URL encoding): Call callback with `error=access_denied&foo=bar` or `error=bad%0d%0aSet-Cookie:x=1` → `Location` header properly URL-encodes `detail` and never injects extra query params or CRLF headers.

6. **G14 Subfolder Containment Edge Cases:**
   - Personal space: `custom_subfolder="../../maria/Photos"`, `../escape`, `/etc/passwd`, `a/../../b`, backslash `..\\..\\maria`, symlink inside `homes/alice/Photos/link` pointing to `homes/maria/Photos` → all MUST return HTTP `400`.
   - Shared space: same traversal & symlink escapes outside `photos_dir` → HTTP `400`.
   - Legitimate nested subfolders (e.g. `GooglePhotos/2024`) → HTTP `200` and contained under expected root.

7. **Trusted `_public_base_url` (Proxy vs Direct Spoofing):**
   - Direct client (Bearer/API token, no valid `X-Proxy-Secret`, `trust_proxy_headers=False`) calls `GET /api/oauth/google/start` with spoofed `X-Forwarded-Host: attacker.example` and `X-Forwarded-Proto: https` → returned `redirect_uri` MUST ignore spoofed headers and use actual request host (not `attacker.example`).
   - Verified CGI proxy (`X-Proxy-Secret` valid, `X-Proxy-Mode: cgi`, `X-Forwarded-Host: mynas.local:5001`, `X-Forwarded-Proto: https`, `X-Forwarded-Prefix: /webman/3rdparty/gphotos-migrator`) → returned `redirect_uri` MUST be `https://mynas.local:5001/webman/3rdparty/gphotos-migrator/oauth_callback.cgi`.
   - Explicit `google_redirect_uri` setting always takes precedence over header-derived URI.

8. **Config/Profile Disk Security & Recovery:**
   - Corrupt `runtime_settings.json` (e.g., `"{broken json"`) → app/ConfigManager boots with defaults AND leaves a backup file on disk (not silently lost).
   - Corrupt `profiles.json` → `ProfileRegistry` boots with `"default"` profile AND backs up corrupt file.
   - Corrupt non-default profile `state.db` (random garbage bytes) → `create_app()` still starts and serves `"default"` and healthy profiles.
   - File permissions on `runtime_settings.json`, `api.token` (auto-generated), and `google_token.json` → `0o600` (`stat.S_IMODE(p.stat().st_mode) == 0o600`).
   - `delete_profile`: verify `data_dir/profiles/<id>`, `work_dir/profiles/<id>`, `inbox_dir/<id>` (including `state.db` and `google_token.json`) are removed, `photos_dir` files are untouched, and recreating the same profile yields fresh state.
   - Reserved profile slugs (`processed`, `profiles`, `default`) and duplicate `photos_dir` (e.g., two shared profiles with empty `custom_subfolder` or same subfolder, or personal profile duplicating existing `photos_dir`).

9. **Docker & Packaging Config Contracts (Tier 1 Static / Config Verification):**
   - `docker-compose.yml`: binds `"127.0.0.1:8765:8765"` (not `"8765:8765"`), does NOT hardcode `gphotos-internal-update-token`, sets `TZ=UTC`, includes `cap_drop` and `no-new-privileges`.
   - `.env.example`: reflects `SYNOLOGY_VERIFY_SSL=true` and HTTPS default `SYNOLOGY_URL`.
   - `Settings()` default instantiation (with clean env): `app_host == "127.0.0.1"`, `synology_url == "https://127.0.0.1:5001"`, `synology_verify_ssl is True`.

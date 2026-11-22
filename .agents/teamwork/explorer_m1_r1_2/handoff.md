# M1 Explorer 2 Handoff — `config.py` Deep Analysis, Caller Audit & Design Blueprint

**Summary:** Completed a line-by-line analysis of `src/gphotos_synology/config.py`, audited all 19 importing modules across `src/` and `tests/` (plus `synology-spk/`, `Dockerfile`, `docker-compose.yml`, `.env.example`, and `app.js`), empirically tested `pydantic-settings` 2.16.3 alias/roundtrip edge cases in `.venv`, and designed the complete blueprint for `config.py` + `tests/test_config.py`. Changing the three defaults (`app_host="127.0.0.1"`, `synology_url="https://127.0.0.1:5001"`, `synology_verify_ssl=True`) breaks **zero** existing tests. Several non-obvious bugs and subtle edge cases were uncovered and resolved in the design below (most notably: `populate_by_name=True` requirement for `AliasChoices` + `model_validate`, empty `QUARANTINE_DIR=""` coercing to `Path(".")`, `existing["synology_password"] = ""` needed to block env-var password exfiltration on URL change, preventing UI full-form saves from freezing lazily inherited OAuth credentials, and `profiles.py` needing to scope `quarantine_dir_override` per profile in M3).

---

## 1. Line-by-Line Audit of `src/gphotos_synology/config.py`

Current file: `src/gphotos_synology/config.py` (255 lines).

| Lines | Symbol / Section | Current Implementation | Findings / Gaps |
|---|---|---|---|
| 15–22 | `Settings(BaseSettings)` & `model_config` | `SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")` | Missing `populate_by_name=True`. Empirically verified in `.venv` (`pydantic 2.14.4`, `pydantic-settings 2.16.3`): if a field defines `validation_alias` without `populate_by_name=True` and without the Python field name in `AliasChoices`, keyword instantiation `Settings(quarantine_dir_override=...)` is **silently ignored** by `extra="ignore"`! |
| 25–29 | Storage path fields | `data_dir`, `work_dir`, `inbox_dir`, `photos_dir`, `homes_dir` (all `Path`) | Missing `quarantine_dir_override: Path \| None = None` (G8). Note: if env `QUARANTINE_DIR=""` is set (common in `.env`), Pydantic coerces `""` to `Path(".")` unless a `mode="before"` validator converts blank strings to `None`. |
| 32–33 | Server fields | `app_host="0.0.0.0"`, `app_port=8765` | G5/C1: default `app_host` must be `"127.0.0.1"`. |
| 36–65 | Polling, org, OAuth, EXIF, dedup | All validated with `Literal` / `ge` / `le` constraints | Keep intact; no changes required in M1. |
| 68–74 | Synology API fields | `synology_url="http://172.17.0.1:5000"`, `synology_verify_ssl=False` | G5: change defaults to `synology_url="https://127.0.0.1:5001"` (`DEFAULT_SYNOLOGY_URL`) and `synology_verify_ssl=True`; add `synology_ca_cert: str = ""`. |
| 77–96 | Notifications, renewal, updates | `webhook_*`, `smtp_*`, `takeout_renewal_reminder_days`, `github_repo`, `app_build_sha`, `app_build_date`, `auto_check_updates`, `watchtower_*` | Keep intact. |
| *(missing)* | Security & auth fields (C1/C2/G6) | None exist | Add `proxy_secret`, `proxy_secret_file`, `api_token`, `dsm_admin_users`, `trust_proxy_headers`, `allow_insecure_no_auth`, plus `resolve_proxy_secret()` and `dsm_admin_user_set()`. |
| 98–114 | Path properties | `db_path`, `token_path`, `runtime_config_path`, `quarantine_dir` (`work_dir / "replaced_duplicates"`) | Update `quarantine_dir` to return `self.quarantine_dir_override` if not `None`, else `self.work_dir / "replaced_duplicates"`. |
| 115–125 | `ensure_directories()` | Creates `data_dir`, `work_dir`, `quarantine_dir`, `inbox_dir`; suppresses `OSError` on `photos_dir` | Already creates `self.quarantine_dir` — works automatically when `quarantine_dir_override` is set! |
| 126–135 | `is_photos_dir_writable()` | Writes/unlinks `.gphotos_write_probe_<pid>` in `photos_dir` | Keep intact. |
| 139–175 | `MUTABLE_UI_FIELDS` | 35 UI-editable fields (includes `synology_url`, `synology_password`, `synology_verify_ssl`, `google_client_id`, `google_client_secret`) | Keep security/filesystem fields strictly **OUT**: `proxy_secret`, `proxy_secret_file`, `api_token`, `dsm_admin_users`, `trust_proxy_headers`, `allow_insecure_no_auth`, `synology_ca_cert`, `quarantine_dir_override`, `photos_dir`, `data_dir`, `work_dir`, `inbox_dir`, `homes_dir`, `app_host`, `app_port`, `exiftool_path`, `github_repo`, `watchtower_url`, `watchtower_token`. |
| 177–182 | `SECRET_FIELDS` | `google_client_secret`, `synology_password`, `smtp_password`, `watchtower_token` | Add `"proxy_secret"` and `"api_token"`. |
| 188–195 | `ConfigManager.__init__` & `.settings` | Takes `(base_settings=None)`, eagerly stores `self._settings` | Add `fallback_settings_provider: Callable[[], Settings] \| None = None`, `self._lock = threading.RLock()`, `self._runtime_overrides: dict[str, Any]`, `self.last_update_warnings: list[str] = []`. Make `.settings` dynamically apply lazy fallback inheritance without mutating `self._own_settings`. |
| 197–209 | `ConfigManager._load_merged()` | Reads `runtime_settings.json`, silently ignores `(json.JSONDecodeError, OSError)` with `pass`; does NOT catch `UnicodeDecodeError` or `ValidationError` or non-dict JSON! | Back up corrupt/invalid `runtime_settings.json` to `runtime_settings.json.corrupt.<timestamp>`, log `ERROR`, fall back cleanly to `self._base`. |
| 211–246 | `ConfigManager.update(patch)` | (1) Filters `patch` by `MUTABLE_UI_FIELDS` and skips `"********"` secrets; (2) reads `existing` from disk **WITHOUT** filtering `existing` by `MUTABLE_UI_FIELDS` (inconsistency bug!); (3) no URL validation; (4) no G4 password clearing on URL change; (5) writes `.tmp` via `write_text` (default umask — world-readable race!) then `contextlib.suppress(OSError): os.chmod(tmp_path, 0o600)`. | Fix all 5: filter disk overrides by `MUTABLE_UI_FIELDS`, validate `synology_url`, clear `synology_password = ""` on URL change without fresh plaintext password (+ log & `last_update_warnings`), avoid freezing unchanged inherited fallback values, write atomically with `os.open(..., 0o600)` + `fsync` + `os.chmod` warning on `OSError`. |
| 248–254 | `ConfigManager.to_public_dict()` | Calls `self._settings.model_dump(mode="json")` and masks `SECRET_FIELDS` | Must call **`self.settings.model_dump(mode="json")`** (via property, not `self._own_settings`) so lazily inherited `google_client_id`, masked `google_client_secret`, and `synology_url` appear in API responses! |

---

## 2. Complete Caller & Consumer Audit (`src/`, `tests/`, Packaging)

All 19 Python files importing `gphotos_synology.config` plus packaging/frontend consumers were inspected:

### 2.1 `src/` Consumers
1. **`src/gphotos_synology/main.py`**
   - `L16`: imports `ConfigManager, Settings`
   - `L51`: `settings = base_settings or Settings()`
   - `L99–105`: `cli()` reads `settings.app_host`, `settings.app_port` → **benefits directly from `app_host="127.0.0.1"` default**.
2. **`src/gphotos_synology/profiles.py`**
   - `L15`: imports `ConfigManager, Settings`
   - `L74–75`: `base_settings.ensure_directories()`
   - `L124–148` (`_build_context`):
     - Default profile: `ConfigManager(base_settings=self.base_settings)`.
     - Non-default profiles: currently snapshots `default_s.google_client_id`, `google_client_secret`, `synology_url` into `base_dump` and runs `Settings.model_validate(base_dump)` → `ConfigManager(base_settings=profile_settings)`.
     - **M3 coordination note #1 (Lazy inheritance):** In M3, `_build_context` should pass `fallback_settings_provider=lambda: default_ctx.config_manager.settings` instead of snapshotting `google_client_id` / `google_client_secret` / `synology_url`.
     - **M3 coordination note #2 (`quarantine_dir_override` isolation):** If `self.base_settings.quarantine_dir_override` is set, `base_dump` copies it verbatim! M3 should scope `base_dump["quarantine_dir_override"] = self.base_settings.quarantine_dir_override / "profiles" / profile.id` so multi-user profiles do not collide in a shared quarantine override directory.
   - `L172`: `ctx.config_manager.settings.inbox_dir`
3. **`src/gphotos_synology/web/routes.py`**
   - `L99, 114, 165, 220, 226, 286, 316, 334, 358, 368, 413, 430`: accesses `config_manager.settings`
   - `L174`: `base_dir = settings.quarantine_dir.resolve()` (works seamlessly with `quarantine_dir_override`)
   - `L239, 253`: `pctx.config_manager.to_public_dict()`
   - `L249–251`: `pctx.config_manager.update(body.updates)` — wraps exceptions into `HTTPException(status_code=400, detail=str(exc))` → **`ValueError` from invalid `synology_url` automatically becomes HTTP 400!** In M3, `PUT /api/settings` can also include `"warnings": pctx.config_manager.last_update_warnings` in the JSON response.
4. **`src/gphotos_synology/synology/photos_api.py`**
   - `L34`: `self.base_url = settings.synology_url.rstrip("/")`
   - `L38`: `verify=settings.synology_verify_ssl` (M2 will also wire `settings.synology_ca_cert`)
   - `L104, 112–113`: checks/uses `settings.synology_username` and `settings.synology_password`
5. **`src/gphotos_synology/worker/pipeline.py`**
   - `L79, 97, 113, 148, 234, 267, 330, 527, 640, 686, 707, 778, 813`: reads `self.config_manager.settings` dynamically at each step (so lazy OAuth/URL inheritance takes effect immediately on the next cycle without restart!).
   - `L576, 647, 690`: uses `settings.quarantine_dir` consistently (never hardcodes `work_dir / "replaced_duplicates"`).
6. **`src/gphotos_synology/worker/scheduler.py`**
   - `L154`: `self.config_manager.settings.poll_interval_minutes`
7. **`src/gphotos_synology/sources/gdrive.py`**
   - `L53, 58–59`: reads `self.settings.google_client_id.strip()` and `google_client_secret.strip()`
   - `L97, 106, 110`: `self.settings.token_path`
8. **`src/gphotos_synology/sources/inbox.py`**, **`synology/scanner.py`**, **`synology/importer.py`**, **`notifications.py`**, **`updater.py`**
   - Read-only consumers of `Settings` instances. None depend on `app_host`, `synology_url`, or `synology_verify_ssl` defaults.

### 2.2 `tests/` & Packaging Backwards-Compatibility Check
Checked every test file (`tests/test_api.py`, `test_profiles.py`, `test_pipeline_e2e.py`, `test_visual_quality_upgrade.py`, `test_existing_nas_dedup.py`, `test_synology_api_and_security.py`, `test_metadata_and_importer.py`, `test_updater.py`):
- **`app_host="127.0.0.1"`**: Zero tests assert `"0.0.0.0"`. `Dockerfile:40` passes `--host 0.0.0.0` explicitly on the CLI (inside container namespace, so Docker bridge port forwarding keeps working); `build_spk.py:146` already exports `APP_HOST="127.0.0.1"`. **Zero breakage.**
- **`synology_url="https://127.0.0.1:5001"`**: Zero tests assert `"http://172.17.0.1:5000"`. `test_synology_api_and_security.py:66` passes explicit `synology_url="http://synology.local:5000"` (`http://` stays valid for local/LAN installs). `test_pipeline_e2e.py` uses `FakeSynologyPhotosClient`. **Zero breakage.**
- **`synology_verify_ssl=True`**: Zero tests assert `False`. `test_synology_api_and_security.py:71` injects a custom `httpx.MockTransport` client. **Zero breakage.**
- **`test_api.py:46–69`**: Updates `synology_password` without changing `synology_url`, then updates `folder_template` while sending `"synology_password": "********"` (URL unchanged) → asserts `"super-secret-password"` is preserved. **Fully preserved** because G4 only clears `synology_password` when `synology_url` actually changes!
- **`test_profiles.py:38–68`**: Sets `google_client_id` on `default`, creates `maria`, asserts `maria` inherits `google_client_id`. **Fully preserved** (and enhanced in M3 with lazy provider).

---

## 3. Detailed Edge-Case Analysis

### 3.1 Env Var Loading, Aliases & Pydantic Roundtrips
Empirically verified against `pydantic 2.14.4` / `pydantic-settings 2.16.3` in `.venv`:

1. **`model_config`**: MUST add `populate_by_name=True` alongside `env_file=".env"`, `env_file_encoding="utf-8"`, `extra="ignore"`.
2. **Exact new fields & aliases on `Settings`**:
   - `quarantine_dir_override: Path | None = Field(default=None, validation_alias=AliasChoices("quarantine_dir", "quarantine_dir_override"))`
     - Add `@field_validator("quarantine_dir_override", mode="before")` converting empty/whitespace-only strings (`""`, `"   "`) to `None` so `QUARANTINE_DIR=` in `.env` does NOT become `Path(".")`.
   - `proxy_secret: str = Field(default="", validation_alias=AliasChoices("gphotos_proxy_secret", "proxy_secret"))`
     - Supports env `GPHOTOS_PROXY_SECRET` (primary per `PROJECT.md:149`), `PROXY_SECRET`, and kwarg `proxy_secret=...`.
   - `proxy_secret_file: str = Field(default="", validation_alias=AliasChoices("proxy_secret_file", "gphotos_proxy_secret_file"))`
     - Supports env `PROXY_SECRET_FILE` (`start-stop-status` per `PROJECT.md:148`), `GPHOTOS_PROXY_SECRET_FILE` (matching CGI env convention), and kwarg `proxy_secret_file=...`.
   - `api_token: str = Field(default="", validation_alias=AliasChoices("api_token", "app_api_token"))`
     - Supports env `API_TOKEN` (primary per `PROJECT.md:159`), `APP_API_TOKEN`, and kwarg `api_token=...`.
   - `dsm_admin_users: str = Field(default="")` → env `DSM_ADMIN_USERS`.
   - `trust_proxy_headers: bool = Field(default=False)` → env `TRUST_PROXY_HEADERS`.
   - `allow_insecure_no_auth: bool = Field(default=False)` → env `ALLOW_INSECURE_NO_AUTH`.
   - `synology_ca_cert: str = Field(default="")` → env `SYNOLOGY_CA_CERT`.
3. **Roundtrip verified**: With `populate_by_name=True` and the field's own name included in `AliasChoices`, `Settings.model_validate(base.model_dump())` preserves explicit `""` and `None` values even when corresponding env vars are set in `os.environ`.
4. **`Settings.resolve_proxy_secret(self) -> str`**:
   - If `self.proxy_secret.strip()` is non-empty → return it (direct env/kwarg takes precedence over file).
   - Else if `self.proxy_secret_file.strip()` is non-empty → read UTF-8 file and `.strip()`; on `OSError` (missing/unreadable), log `WARNING` and return `""` (fail-closed, never crash startup or `/api/health`).
   - Else return `""`.
5. **`Settings.dsm_admin_user_set(self) -> set[str]`**:
   - Returns `{u.strip().lower() for u in self.dsm_admin_users.split(",") if u.strip()}` (convenience helper for M3 `web/security.py`).

### 3.2 G4 — `synology_url` Validation & Credential Clearing
Define helpers in `config.py`:
- `DEFAULT_SYNOLOGY_URL = "https://127.0.0.1:5001"`
- `LEGACY_DEFAULT_SYNOLOGY_URL = "http://172.17.0.1:5000"`
- `validate_synology_url(url: Any) -> str`:
  1. Must be `isinstance(url, str)` and non-empty (`if not isinstance(url, str) or not url: raise ValueError(...)`).
  2. Reject any whitespace or control character: `if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url): raise ValueError("synology_url must not contain whitespace or control characters")`.
  3. Parse via `parsed = urllib.parse.urlsplit(url)`:
     - Require `parsed.scheme.lower() in {"http", "https"}`.
     - Require `bool(parsed.hostname)` (rejects `"https://"`, `"https:///path"`, `"http://:5000"`).
     - Validate port by accessing `_ = parsed.port` (raises `ValueError` on non-numeric or out-of-range ports like `:99999`).
  4. Return `url` (or `url.rstrip("/")` — preserving exact scheme/host/path without trailing slash is clean; wait: check if returning `url.rstrip("/")` vs preserving original matters: returning `url` as-is while comparing normalized is safest if someone inspects exact string, OR stripping trailing slash is also fine; returning `url.rstrip("/")` means `"https://nas:5001/"` normalizes to `"https://nas:5001"` matching `photos_api.py:34`). Let's normalize comparison with `_normalize_url_for_compare(url: str) -> tuple[str, str, str, str, str]`:
     `(parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), parsed.query, parsed.fragment)`.
- **Where validation runs**:
  - On `Settings` (`@field_validator("synology_url")`): if `value != ""`, call `validate_synology_url(value)` (allows `Settings(synology_url="")` for lazy inheritance / legacy empty `.env` while rejecting malformed non-empty URLs).
  - In `ConfigManager.update(patch)`: if `"synology_url"` is present in `patch`, **always** call `validate_synology_url(patch["synology_url"])` (so empty `""`, non-http/https, missing host, invalid port, or whitespace/control chars all raise `ValueError` → HTTP 400 in `routes.py` without touching disk or memory!).
- **Password-clearing decision matrix in `ConfigManager.update(patch)`**:
  Let `prior_settings = self.settings`, `prior_url = prior_settings.synology_url`, `prior_password = prior_settings.synology_password`.
  Let `url_changed = ("synology_url" in filtered) and (_normalize_url_for_compare(filtered["synology_url"]) != _normalize_url_for_compare(prior_url))`.
  Let `raw_pw = patch.get("synology_password")`, `fresh_password_supplied = isinstance(raw_pw, str) and bool(raw_pw) and raw_pw != "********"`.

  | Scenario | `url_changed` | `prior_password` | `patch["synology_password"]` | Result | `last_update_warnings` |
  |---|---|---|---|---|---|
  | URL changed, pw omitted | True | `"secret"` (from runtime OR env!) | *(absent)* | Set **`existing["synology_password"] = ""`** (overrides env too!) + log `WARNING` | 1 warning message |
  | URL changed, pw is `"********"` (UI form save) | True | `"secret"` | `"********"` | Set **`existing["synology_password"] = ""`** + log `WARNING` | 1 warning message |
  | URL changed, pw is `""` | True | `"secret"` | `""` | `existing["synology_password"] = ""` | `[]` (user explicitly cleared) or optional note — recommend `[]` only if explicit `""`, or warning whenever `prior_password` was non-empty and cleared; actually **explicit `""` means user intentionally cleared it, whereas omitted/`"********"` means app cleared it automatically** |
  | URL changed, fresh pw supplied | True | `"secret"` or `""` | `"new-pw"` | Keep `"new-pw"` | `[]` |
  | URL changed, no prior pw | True | `""` | *(absent / `"********"`)* | Remains `""` | `[]` (no spurious warning!) |
  | URL unchanged (exact or trailing `/` / case diff) | False | `"secret"` | *(absent / `"********"`)* | Preserves `"secret"` | `[]` |
  | URL invalid (e.g. `ftp://`, whitespace, `\r\n`, `""`) | N/A | `"secret"` | any | Raises `ValueError`; disk & memory untouched | unchanged |

  **Critical G4 nuance:** Setting `existing["synology_password"] = ""` (rather than `existing.pop("synology_password", None)`) ensures that even if `synology_password` originated from `SYNOLOGY_PASSWORD` env var on `base_settings`, it is STILL cleared to `""` and cannot be exfiltrated to the new URL!

### 3.3 Lazy OAuth & `synology_url` Inheritance (`fallback_settings_provider`)
- **Constructor**: `ConfigManager(base_settings: Settings | None = None, *, fallback_settings_provider: Callable[[], Settings] | None = None)`
  - Backwards-compatible default (`fallback_settings_provider=None`).
  - Stores `self._base`, `self._fallback_settings_provider`, `self._runtime_overrides: dict[str, Any]`, `self._own_settings: Settings`, `self.last_update_warnings: list[str] = []`, `self._lock = threading.RLock()`.
- **Dynamic `.settings` property**:
  1. Under `self._lock`, grab `own = self._own_settings` and `overrides = self._runtime_overrides`.
  2. If `self._fallback_settings_provider is None`: return `own`.
  3. Call `fallback = self._fallback_settings_provider()` (if `fallback is None` or `fallback is own`, return `own`).
  4. Build `inherited: dict[str, Any] = {}`:
     - `google_client_id`: if `not own.google_client_id.strip()` and `fallback.google_client_id.strip()` → inherit.
     - `google_client_secret`: if `not own.google_client_secret.strip()` and `fallback.google_client_secret.strip()` → inherit.
     - `synology_url`: if `"synology_url" not in overrides` AND `own.synology_url in {"", DEFAULT_SYNOLOGY_URL, LEGACY_DEFAULT_SYNOLOGY_URL}` AND `not own.synology_password` AND `fallback.synology_url.strip()` AND `fallback.synology_url != own.synology_url` → inherit.
  5. Return `own.model_copy(update=inherited)` if `inherited` else `own`.
- **Two subtle edge cases solved by this design**:
  1. **UI full-form save (`app.js:333–347`) must NOT accidentally freeze inherited credentials**:
     - When `maria` opens Settings and clicks Save (changing e.g. `poll_interval_minutes`), `app.js` submits ALL form fields, including the inherited `google_client_id` and `synology_url` (and `"********"` for `google_client_secret`).
     - In `update(patch)`: when `self._fallback_settings_provider is not None`, if a profile does NOT already have a local override in `existing` for `google_client_id` (and `own.google_client_id == ""`) and `filtered["google_client_id"] == fallback.google_client_id`, do **not** write `google_client_id` into `existing` (keep it dynamically inherited!). Same for `synology_url` when `existing` has no `synology_url`, `filtered["synology_url"]` matches `fallback.synology_url` (normalized), AND no non-empty `synology_password` is being stored.
     - Meanwhile, if `maria` supplies a *different* `google_client_id` or `synology_url`, it IS saved as her explicit local override and takes precedence over fallback!
  2. **Cross-profile G4 safety when saving `synology_password`**:
     - If `maria` inherits `synology_url="https://nas1:5001"` from `default` and then saves a non-empty `synology_password="maria-pw"`, pin `existing["synology_url"] = effective_url` in `maria`'s overrides so a later `default` `synology_url` change can NEVER silently redirect `maria`'s stored password to another host!
- **`to_public_dict()`**:
  - Uses `self.settings.model_dump(mode="json")` (via `.settings` property, NOT `self._own_settings`).
  - Masks every field in `SECRET_FIELDS` (`google_client_secret`, `synology_password`, `smtp_password`, `watchtower_token`, `proxy_secret`, `api_token`) to `"********"` whenever truthy.
  - Thus an inherited `google_client_secret` appears as `"********"` (never plaintext, never `""` when inherited).

### 3.4 Corrupt `runtime_settings.json` Backup & Atomic `0o600` Writes
1. **Corrupt file handling (`_read_overrides_from_disk` & `_backup_corrupt_file`)**:
   - Triggered on ANY of: `json.JSONDecodeError`, `UnicodeDecodeError`, non-`dict` top-level JSON (e.g. `[]`, `"str"`, `123`, `null`), OR `ValidationError` when validating merged overrides in `_load_merged()`.
   - Backup path: `runtime_settings.json.corrupt.<YYYYMMDDTHHMMSSZ>` (with `.1`, `.2`, ... deduplication suffix if a backup with that exact timestamp already exists).
   - Moves corrupt file via `os.replace(override_path, backup_path)` (fallback `shutil.copy2` if needed).
   - Logs at `ERROR` with path, reason, and backup destination.
   - Always filters loaded keys through `MUTABLE_UI_FIELDS` (fixes the existing bug where `update()` didn't filter keys read back from disk!).
2. **Atomic `0o600` write (`_write_overrides_atomic`)**:
   - Ensures `override_path.parent.mkdir(parents=True, exist_ok=True)`.
   - Uses temp file `.runtime_settings.json.<pid>.<tid>.tmp` in the same directory.
   - Opens with `os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)` so the file is NEVER world-readable even briefly.
   - Writes UTF-8 JSON, `flush()`, `os.fsync(fd)`.
   - Calls `os.chmod(tmp_path, 0o600)` inside `try/except OSError as exc:` and logs a `WARNING` (mentioning `0600` / permissions) if `chmod` fails (instead of silently suppressing it).
   - Atomically replaces target with `os.replace(tmp_path, override_path)`.
   - Cleans up `tmp_path` in `except` on failure.

---

## 4. Concrete Blueprint for `config.py` & `tests/test_config.py`

### 4.1 `src/gphotos_synology/config.py` Changes
1. Add `import logging`, `import shutil`, `import threading`, `from collections.abc import Callable`, `from datetime import UTC, datetime`, `from urllib.parse import urlsplit`, `from pydantic import AliasChoices, Field, ValidationError, field_validator`.
2. Add module constants: `DEFAULT_SYNOLOGY_URL = "https://127.0.0.1:5001"`, `LEGACY_DEFAULT_SYNOLOGY_URL = "http://172.17.0.1:5000"`, `MASK_SENTINEL = "********"`.
3. Add `validate_synology_url(url: str) -> str` and `_normalize_url_for_compare(url: str) -> tuple[str, str, str, str, str]`.
4. Update `Settings`:
   - `model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True)`
   - Defaults: `app_host = "127.0.0.1"`, `synology_url = DEFAULT_SYNOLOGY_URL`, `synology_verify_ssl = True`.
   - New fields: `quarantine_dir_override`, `synology_ca_cert`, `proxy_secret`, `proxy_secret_file`, `api_token`, `dsm_admin_users`, `trust_proxy_headers`, `allow_insecure_no_auth`.
   - Validators: blank-string-to-`None` on `quarantine_dir_override`; non-empty URL validation on `synology_url`.
   - Updated property `quarantine_dir` + new methods `resolve_proxy_secret()` and `dsm_admin_user_set()`.
5. Update `SECRET_FIELDS` to add `"proxy_secret"`, `"api_token"`. Verify `MUTABLE_UI_FIELDS` excludes all security/path/update-infra fields.
6. Upgrade `ConfigManager`:
   - `__init__(self, base_settings: Settings | None = None, *, fallback_settings_provider: Callable[[], Settings] | None = None)`
   - `self._lock = threading.RLock()`, `self.last_update_warnings: list[str] = []`
   - Dynamic `.settings` property with lazy fallback inheritance
   - Corrupt JSON backup + `ERROR` log on load/update/schema-error
   - Strict `synology_url` validation + G4 password-clearing (including env-var override protection) + `last_update_warnings`
   - Atomic `0o600` temp file creation (`os.open`) + `fsync` + `os.chmod` warning log
   - `to_public_dict()` backed by `self.settings` (not `self._own_settings`).

### 4.2 `tests/test_config.py` Test Matrix (New File)
1. **Secure defaults (G5/C1)**: `Settings()` has `app_host == "127.0.0.1"`, `synology_url == "https://127.0.0.1:5001"`, `synology_verify_ssl is True`, `synology_ca_cert == ""`, `allow_insecure_no_auth is False`, `trust_proxy_headers is False`, `quarantine_dir == work_dir / "replaced_duplicates"`.
2. **Env var aliases & `quarantine_dir_override` (G8/C1)**:
   - `QUARANTINE_DIR` env sets `quarantine_dir_override` & `quarantine_dir`; `QUARANTINE_DIR=""` resolves to `None` (default `work_dir / "replaced_duplicates"`).
   - `GPHOTOS_PROXY_SECRET` and `PROXY_SECRET` env aliases; `PROXY_SECRET_FILE` and `GPHOTOS_PROXY_SECRET_FILE` aliases; `API_TOKEN` env; `DSM_ADMIN_USERS` + `dsm_admin_user_set()`.
   - `resolve_proxy_secret()` precedence (direct env > file), whitespace stripping, missing file returns `""` and logs `WARNING`.
3. **Allowlist & secret masking**:
   - `MUTABLE_UI_FIELDS` excludes `proxy_secret`, `proxy_secret_file`, `api_token`, `dsm_admin_users`, `trust_proxy_headers`, `allow_insecure_no_auth`, `photos_dir`, `watchtower_url`, `watchtower_token`, `quarantine_dir_override`, `synology_ca_cert`.
   - `ConfigManager.update()` ignores non-mutable fields and `to_public_dict()` masks all `SECRET_FIELDS` (including `proxy_secret` and `api_token`).
4. **G4 `synology_url` validation & credential clearing**:
   - Rejects `""`, `"ftp://nas:5000"`, `"javascript:alert(1)"`, `"https://"`, `"https://nas:99999"`, `"https://nas:5001 foo"`, `"https://nas:5001\r\nX: 1"`, `" https://nas:5001"` with `ValueError` without modifying persisted settings.
   - Changing `synology_url` when `synology_password` was set (both from runtime update AND from `base_settings`/env) with omitted password OR `"********"` clears `synology_password == ""`, persists `""` across reload, logs `WARNING`, and populates `cfg.last_update_warnings`.
   - Changing `synology_url` WITH a fresh plaintext `synology_password` keeps the new password and clears `last_update_warnings`.
   - Changing `synology_url` when no password was previously set produces empty `last_update_warnings`.
   - Unchanged URL (or trailing-slash / case-only diff like `https://nas:5001/` vs `https://nas:5001`) preserves existing `synology_password`.
5. **Lazy OAuth & `synology_url` inheritance**:
   - Child profile created BEFORE default sets `google_client_id`/`google_client_secret`/`synology_url` immediately sees them once default updates.
   - Rotating default credentials immediately updates child's `.settings` and `to_public_dict()` (with `google_client_secret == "********"`).
   - Child saving an unrelated setting (even sending back inherited `google_client_id` + `"********"` secret like `app.js` does) does NOT freeze inherited credentials.
   - Child setting an explicit distinct `google_client_id` or `synology_url` overrides default.
   - Child saving `synology_password` pins its effective `synology_url` so a subsequent default URL change cannot redirect child's password.
6. **Corrupt `runtime_settings.json` resilience & `0o600` permissions**:
   - Truncated JSON, invalid UTF-8 bytes, non-dict JSON (`"[1,2]"`), and schema-invalid JSON (`{"poll_interval_minutes": -1}`) all log `ERROR`, create `runtime_settings.json.corrupt.<ts>` backup, and fall back to base settings without crashing.
   - `update()` writes `runtime_settings.json` with `0o600` permissions (verified via `stat.S_IMODE`).
   - Monkeypatching `os.chmod` to raise `PermissionError` still writes the file cleanly and logs a `WARNING`.

---

## 5. Verification Commands (Read-Only)
All findings in this report were verified directly against `HEAD dfcf34f` using:
- `view_file` on `src/gphotos_synology/config.py`, `profiles.py`, `main.py`, `web/routes.py`, `synology/photos_api.py`, `updater.py`, `web/static/app.js`, `synology-spk/build_spk.py`, `Dockerfile`, `docker-compose.yml`, `.env.example`, and all `tests/test_*.py`.
- `rg` across `src/`, `tests/`, `synology-spk/`, and config files.
- Live `.venv/bin/python` check verifying `pydantic 2.14.4` / `pydantic-settings 2.16.3` `AliasChoices`, `populate_by_name=True`, and `model_validate(model_dump())` behavior.

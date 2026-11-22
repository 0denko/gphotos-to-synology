# Explorer 2 (`explorer_m3_r1_2`) Handoff — Milestone M3: G14 & Profile Management Hardening

## 1. Executive Summary & Findings

Investigation of `src/gphotos_synology/profiles.py`, `src/gphotos_synology/config.py`, `src/gphotos_synology/database.py`, `src/gphotos_synology/worker/scheduler.py`, `tests/test_profiles.py`, and `tests/e2e/test_tier{1..4}_*.py` revealed the following critical implementation details for `profiles.py` and `tests/test_profiles.py`:

1. **G14 (`_resolve_contained_subfolder` & Personal Home Containment)**:
   - **Leading-slash trap in baseline `profiles.py:227`**: Baseline did `custom_sub = req.custom_subfolder.strip().strip("/")` *before* checking for absolute paths. As a result, `custom_subfolder="/etc/passwd"` became `"etc/passwd"` and was silently accepted! `test_t2_g14_personal_and_shared_subfolder_rejects_traversal_and_symlink_escape` (`tests/e2e/test_tier2_boundaries.py:1046,1057`) explicitly passes `"/etc/passwd"` for both `personal` and `shared` profiles and expects `ValueError`.
   - Therefore, `req.custom_subfolder.strip()` must be passed **without** pre-stripping leading `/` to `_resolve_contained_subfolder(base_dir: Path, custom_sub: str) -> Path`, which rejects leading `/`, `Path(raw).is_absolute()`, backslashes (`\`), NUL (`\x00`), `.` or `..` segments, and any path or symlink whose `.resolve()` is equal to or outside `base_dir.resolve()` (`not resolved_target.is_relative_to(resolved_base)`).
   - In `personal` mode, `create_profile` must also validate `(homes_dir / username).resolve()` is strictly inside `homes_dir.resolve()` and `(homes_dir / username / "Photos").resolve()` is strictly inside `(homes_dir / username).resolve()`.

2. **Lazy OAuth & `synology_url` Inheritance (`_build_context`)**:
   - **Frozen-snapshot trap in baseline `profiles.py:129-138`**: Baseline copied `default_s.google_client_id`, `default_s.google_client_secret`, and `default_s.synology_url` directly into `base_dump`. Because `ConfigManager.settings` (`src/gphotos_synology/config.py:393-404`) only inherits from `fallback_settings_provider` when `not own.google_client_id.strip()` / `not own.google_client_secret.strip()` / `own.synology_url in {"", DEFAULT_SYNOLOGY_URL, LEGACY_DEFAULT_SYNOLOGY_URL} and not own.synology_password`, pre-populating `base_dump` with the default profile's current values or env values would freeze them and break credential rotation (`tests/e2e/test_tier3_interactions.py:449-460`).
   - Fix: For non-default profiles in `_build_context`, clear `base_dump["google_client_id"] = ""`, `base_dump["google_client_secret"] = ""`, `base_dump["synology_url"] = DEFAULT_SYNOLOGY_URL`, and `base_dump["synology_password"] = ""`, and pass `fallback_settings_provider=self._default_settings` (returning `self._contexts["default"].config_manager.settings`).

3. **Reserved Slugs, Duplicate `photos_dir` Guard & `delete_profile` Cleanup**:
   - Reserve `RESERVED_PROFILE_IDS = frozenset({"default", "processed", "profiles"})` (case-insensitive) and raise `ValueError` in `create_profile` if `username.lower()` or `base_slug.lower()` or `profile_id.lower()` is in `RESERVED_PROFILE_IDS`.
   - Reject creating any profile whose `photos_path.resolve()` equals `Path(existing.photos_dir).resolve()` for any existing profile (`ValueError`), which prevents two shared profiles with empty `custom_subfolder` from sharing `photos_dir` with separate DBs (`tests/e2e/test_tier2_boundaries.py:1099-1108`).
   - In `delete_profile(profile_id)`: `StateDB` opens and closes connections per transaction under `ctx.db._lock` (no persistent open handle). Acquire `ctx.db._lock` (if `ctx` exists), pop from `self._profiles` and `self._contexts`, and remove `data_dir / "profiles" / profile_id`, `work_dir / "profiles" / profile_id`, and `inbox_dir / profile_id` via `shutil.rmtree(..., ignore_errors=True)` (plus `quarantine_dir_override / "profiles" / profile_id` if `quarantine_dir_override` is set), never touching `photos_dir` (`tests/e2e/test_tier3_interactions.py:468-480`).

4. **Corrupt `profiles.json` & Corrupt Per-Profile `state.db` Resilience**:
   - When `profiles.json` is unreadable, invalid JSON, not a JSON `list`, or contains schema-invalid profile entries, log `ERROR` and back it up to `profiles.json.corrupt.<UTC_timestamp>` (with `.1`, `.2` collision suffix) before falling back to `[self._default_profile()]` (`tests/e2e/test_tier2_boundaries.py:1124-1136`).
   - Save `profiles.json` atomically with `0o600` (`os.open(..., 0o600)` + `flush()` + `os.fsync()` + `os.chmod(..., 0o600)` with `logger.warning` on `OSError` + `os.replace()`).
   - In `_load_or_init()`, load `"default"` first, then wrap each non-default `self._build_context(profile)` in `try/except Exception as exc:`. On failure (e.g., corrupt `state.db`), log `ERROR`, mark `profile.enabled = False`, back up `state.db` to `state.db.corrupt.<UTC_timestamp>` (and unlink stale `-wal`/`-shm` sidecars), and retry `self._build_context(profile)` (or skip if disk is unwritable) so app startup never crashes on a corrupt non-default profile DB (`tests/e2e/test_tier2_boundaries.py:1138-1153`).

5. **`resolve_profile_for_dsm_user` & C2 Helpers**:
   - Keep `resolve_profile_for_dsm_user(dsm_username: str | None, *, fallback: str = "default") -> str` backwards-compatible while supporting `fallback=""` for non-admin users who have no profile.
   - Add `user_owns_profile(dsm_username: str, profile_id: str) -> bool` and `list_profiles(*, dsm_user: str | None = None, is_admin: bool = True) -> list[dict[str, Any]]` so `routes.py` / `security.py` can cleanly enforce C2 profile visibility and ownership.

---

## 2. Exact Implementation Blueprint for `src/gphotos_synology/profiles.py`

Replace `src/gphotos_synology/profiles.py` (currently lines 1–295) with the following complete implementation:

```python
"""Multi-user Synology DSM profile registry and /volume1/homes user auto-discovery."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from gphotos_synology.config import DEFAULT_SYNOLOGY_URL, ConfigManager, Settings
from gphotos_synology.database import StateDB
from gphotos_synology.worker.pipeline import MigrationPipeline

logger = logging.getLogger(__name__)

PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,48}$")

RESERVED_PROFILE_IDS: frozenset[str] = frozenset({"default", "processed", "profiles"})

IGNORED_HOME_DIRS: set[str] = {
    "@eaDir",
    "@tmp",
    "#recycle",
    "#snapshot",
    "lost+found",
    "guest",
}


class UserProfile(BaseModel):
    """Represents a Synology DSM user or Shared Space migration profile."""

    id: str
    display_name: str
    dsm_username: str = ""
    target_space: Literal["personal", "shared"] = "shared"
    photos_dir: str
    enabled: bool = True
    is_default: bool = False


class CreateProfileRequest(BaseModel):
    dsm_username: str = Field(default="", max_length=64)
    display_name: str = Field(default="", max_length=80)
    target_space: Literal["personal", "shared"] = "personal"
    custom_subfolder: str = Field(default="", max_length=120)


class UpdateProfileRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=80)
    enabled: bool | None = None


@dataclass
class ProfileContext:
    """Isolated runtime state (config, SQLite DB, pipeline) for a single user profile."""

    profile: UserProfile
    config_manager: ConfigManager
    db: StateDB
    pipeline: MigrationPipeline


def _slugify(raw: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", raw.strip().lower()).strip("-_")
    return cleaned[:48] or "user"


def _resolve_contained_subfolder(base_dir: Path, custom_sub: str) -> Path:
    """Validate and resolve a relative subfolder strictly inside base_dir (G14)."""
    if "\x00" in custom_sub or "\\" in custom_sub:
        raise ValueError("Invalid subfolder path: backslashes and NUL characters are not allowed.")
    raw = custom_sub.strip()
    if not raw:
        raise ValueError("Invalid subfolder path.")
    if raw.startswith("/") or Path(raw).is_absolute():
        raise ValueError("Invalid subfolder path: absolute paths are not allowed.")
    cleaned = raw.rstrip("/")
    if not cleaned or cleaned.startswith("/"):
        raise ValueError("Invalid subfolder path.")

    rel_path = Path(cleaned)
    if rel_path.is_absolute() or not rel_path.parts:
        raise ValueError("Invalid subfolder path.")
    if any(part in {"", ".", ".."} for part in rel_path.parts):
        raise ValueError("Invalid subfolder path: directory traversal is not allowed.")

    resolved_base = base_dir.resolve()
    candidate = base_dir.joinpath(*rel_path.parts)
    resolved_target = candidate.resolve()
    if resolved_target == resolved_base or not resolved_target.is_relative_to(resolved_base):
        raise ValueError("Invalid subfolder path: escapes base Photos directory.")
    return candidate


def _backup_corrupt_profile_file(path: Path, reason: str) -> Path | None:
    """Move a corrupt profiles.json or state.db aside with a UTC timestamp suffix."""
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base_name = f"{path.name}.corrupt.{timestamp}"
    backup_path = path.with_name(base_name)
    counter = 1
    while backup_path.exists():
        backup_path = path.with_name(f"{base_name}.{counter}")
        counter += 1

    try:
        try:
            os.replace(path, backup_path)
        except OSError:
            shutil.copy2(path, backup_path)
            path.unlink(missing_ok=True)
    except OSError as exc:
        logger.error(
            "Corrupt file at %s (%s); failed to back up to %s: %s",
            path,
            reason,
            backup_path,
            exc,
        )
        return None

    logger.error(
        "Corrupt file at %s (%s); backed up to %s",
        path,
        reason,
        backup_path,
    )
    return backup_path


class ProfileRegistry:
    """Manages per-user Synology migration profiles with isolated Google tokens & dedup DBs."""

    def __init__(self, base_settings: Settings) -> None:
        self.base_settings = base_settings
        self.base_settings.ensure_directories()
        self._lock = threading.RLock()
        self._profiles_path = self.base_settings.data_dir / "profiles.json"
        self._profiles: dict[str, UserProfile] = {}
        self._contexts: dict[str, ProfileContext] = {}
        self._load_or_init()

    def _default_profile(self) -> UserProfile:
        space: Literal["personal", "shared"] = (
            "personal"
            if "/homes/" in str(self.base_settings.photos_dir).replace("\\", "/")
            else "shared"
        )
        label = "Default (Shared Space)" if space == "shared" else "Default (Personal Space)"
        return UserProfile(
            id="default",
            display_name=label,
            dsm_username="",
            target_space=space,
            photos_dir=str(self.base_settings.photos_dir),
            enabled=True,
            is_default=True,
        )

    def _default_settings(self) -> Settings:
        default_ctx = self._contexts.get("default")
        if default_ctx is not None:
            return default_ctx.config_manager.settings
        return self.base_settings

    def _load_or_init(self) -> None:
        loaded: list[UserProfile] = []
        if self._profiles_path.exists():
            try:
                raw_text = self._profiles_path.read_text(encoding="utf-8")
                raw = json.loads(raw_text)
                if not isinstance(raw, list):
                    raise ValueError(f"expected JSON array, got {type(raw).__name__}")
                for item in raw:
                    loaded.append(UserProfile.model_validate(item))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError, TypeError) as exc:
                _backup_corrupt_profile_file(self._profiles_path, str(exc))
                loaded = []

        default_profile: UserProfile | None = None
        non_default_profiles: list[UserProfile] = []
        for item in loaded:
            if item.id == "default" or item.is_default:
                if default_profile is None:
                    item.id = "default"
                    item.is_default = True
                    default_profile = item
            else:
                non_default_profiles.append(item)

        if default_profile is None:
            default_profile = self._default_profile()

        # Always initialize the default profile first so non-default profiles can reference it
        self._profiles["default"] = default_profile
        self._contexts["default"] = self._build_context(default_profile)

        for profile in non_default_profiles:
            try:
                ctx = self._build_context(profile)
            except Exception as exc:
                db_path = self.base_settings.data_dir / "profiles" / profile.id / "state.db"
                logger.error(
                    "Failed to initialize profile '%s' (db=%s): %s. Backing up corrupt DB and disabling profile.",
                    profile.id,
                    db_path,
                    exc,
                )
                profile.enabled = False
                if db_path.exists():
                    _backup_corrupt_profile_file(db_path, str(exc))
                    for suffix in ("-wal", "-shm"):
                        with contextlib.suppress(OSError):
                            db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)
                try:
                    ctx = self._build_context(profile)
                    ctx.db.log_event(
                        "ERROR",
                        "profiles",
                        f"Recovered from corrupt state.db on startup ({exc}); profile disabled until reviewed.",
                    )
                except Exception as retry_exc:
                    logger.error(
                        "Skipping profile '%s' after corrupt DB recovery failed: %s",
                        profile.id,
                        retry_exc,
                    )
                    continue

            self._profiles[profile.id] = profile
            self._contexts[profile.id] = ctx

        self._save_profiles()

    def _save_profiles(self) -> None:
        self._profiles_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            [p.model_dump(mode="json") for p in self._profiles.values()],
            indent=2,
        )
        tmp_name = f".{self._profiles_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        tmp_path = self._profiles_path.with_name(tmp_name)

        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
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
                os.chmod(tmp_path, 0o600)
            except OSError as exc:
                logger.warning(
                    "Could not enforce 0600 permissions on %s: %s",
                    self._profiles_path,
                    exc,
                )
            os.replace(tmp_path, self._profiles_path)
        except Exception:
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)
            raise

    def _build_context(self, profile: UserProfile) -> ProfileContext:
        if profile.is_default or profile.id == "default":
            cfg_mgr = ConfigManager(base_settings=self.base_settings)
        else:
            base_dump = self.base_settings.model_dump()
            # Clear shared credentials in base_dump so ConfigManager dynamically inherits
            # live/rotated values from the default profile via fallback_settings_provider.
            base_dump["google_client_id"] = ""
            base_dump["google_client_secret"] = ""
            base_dump["synology_url"] = DEFAULT_SYNOLOGY_URL
            base_dump["synology_password"] = ""

            base_dump["data_dir"] = self.base_settings.data_dir / "profiles" / profile.id
            base_dump["work_dir"] = self.base_settings.work_dir / "profiles" / profile.id
            base_dump["inbox_dir"] = self.base_settings.inbox_dir / profile.id
            if self.base_settings.quarantine_dir_override is not None:
                base_dump["quarantine_dir_override"] = (
                    self.base_settings.quarantine_dir_override / "profiles" / profile.id
                )
            base_dump["photos_dir"] = Path(profile.photos_dir)
            base_dump["synology_space"] = profile.target_space
            if profile.dsm_username:
                base_dump["synology_username"] = profile.dsm_username

            profile_settings = Settings.model_validate(base_dump)
            cfg_mgr = ConfigManager(
                base_settings=profile_settings,
                fallback_settings_provider=self._default_settings,
            )

        db = StateDB(cfg_mgr.settings.db_path)
        pipeline = MigrationPipeline(config_manager=cfg_mgr, db=db)
        return ProfileContext(
            profile=profile,
            config_manager=cfg_mgr,
            db=db,
            pipeline=pipeline,
        )

    def get_context(self, profile_id: str | None = None) -> ProfileContext:
        key = (profile_id or "default").strip() or "default"
        with self._lock:
            ctx = self._contexts.get(key)
            if ctx is None:
                raise KeyError(f"Profile '{key}' does not exist")
            return ctx

    def user_owns_profile(self, dsm_username: str | None, profile_id: str) -> bool:
        """Return True if the specified profile is explicitly owned by dsm_username."""
        if not dsm_username or not dsm_username.strip():
            return False
        target = dsm_username.strip().lower()
        with self._lock:
            profile = self._profiles.get(profile_id)
            if profile is None or not profile.dsm_username:
                return False
            return profile.dsm_username.strip().lower() == target

    def list_profiles(
        self,
        *,
        dsm_user: str | None = None,
        is_admin: bool = True,
    ) -> list[dict[str, Any]]:
        target_user = (dsm_user or "").strip().lower()
        with self._lock:
            result: list[dict[str, Any]] = []
            for ctx in self._contexts.values():
                if not is_admin:
                    if not target_user or ctx.profile.dsm_username.strip().lower() != target_user:
                        continue
                data = ctx.profile.model_dump(mode="json")
                data["inbox_dir"] = str(ctx.config_manager.settings.inbox_dir)
                data["stats"] = ctx.db.get_stats()
                data["progress_state"] = ctx.pipeline.get_progress().get("state", "idle")
                result.append(data)
            return result

    def enabled_contexts(self) -> list[ProfileContext]:
        with self._lock:
            return [ctx for ctx in self._contexts.values() if ctx.profile.enabled]

    def discover_synology_users(self) -> dict[str, Any]:
        """Inspect mounted /data/homes (/volume1/homes) to list available Synology DSM users."""
        homes = self.base_settings.homes_dir
        if not homes.is_dir():
            return {"homes_mounted": False, "users": []}

        existing_usernames = {
            p.dsm_username.lower() for p in self._profiles.values() if p.dsm_username
        }
        users: list[dict[str, Any]] = []
        try:
            for entry in sorted(homes.iterdir(), key=lambda p: p.name.lower()):
                if not entry.is_dir() or entry.is_symlink():
                    continue
                if entry.name.startswith(".") or entry.name in IGNORED_HOME_DIRS:
                    continue
                if entry.name.lower() in RESERVED_PROFILE_IDS:
                    continue
                photos_path = entry / "Photos"
                users.append(
                    {
                        "username": entry.name,
                        "photos_dir": str(photos_path),
                        "has_photos_folder": photos_path.is_dir(),
                        "already_configured": entry.name.lower() in existing_usernames,
                    }
                )
        except OSError:
            return {"homes_mounted": False, "users": []}

        return {"homes_mounted": True, "users": users}

    def resolve_profile_for_dsm_user(
        self,
        dsm_username: str | None,
        *,
        fallback: str = "default",
    ) -> str:
        """Return matching profile ID for the logged-in DSM user, falling back to `fallback`."""
        if not dsm_username or not dsm_username.strip():
            return fallback
        target = dsm_username.strip().lower()
        with self._lock:
            for profile in self._profiles.values():
                if profile.dsm_username and profile.dsm_username.strip().lower() == target:
                    return profile.id
        return fallback

    def create_profile(self, req: CreateProfileRequest) -> ProfileContext:
        """Create a new Synology user or Shared Space migration profile."""
        with self._lock:
            username = req.dsm_username.strip()
            raw_custom_sub = req.custom_subfolder.strip()

            if username and username.lower() in RESERVED_PROFILE_IDS:
                raise ValueError(f"Profile username '{username}' is reserved.")

            if req.target_space == "personal":
                if not username:
                    raise ValueError(
                        "Synology DSM username is required for Personal Space profiles."
                    )
                if (
                    "\x00" in username
                    or "/" in username
                    or "\\" in username
                    or username in {".", ".."}
                    or ".." in username
                ):
                    raise ValueError("Invalid Synology username.")

                resolved_homes = self.base_settings.homes_dir.resolve()
                user_home = self.base_settings.homes_dir / username
                resolved_user_home = user_home.resolve()
                if (
                    resolved_user_home == resolved_homes
                    or not resolved_user_home.is_relative_to(resolved_homes)
                ):
                    raise ValueError("Invalid Synology username: home directory escapes homes_dir.")

                base_photos_path = user_home / "Photos"
                resolved_base_photos = base_photos_path.resolve()
                if (
                    resolved_base_photos == resolved_user_home
                    or not resolved_base_photos.is_relative_to(resolved_user_home)
                ):
                    raise ValueError("Invalid Personal Space Photos path.")

                if raw_custom_sub:
                    photos_path = _resolve_contained_subfolder(base_photos_path, raw_custom_sub)
                    custom_sub = photos_path.relative_to(base_photos_path).as_posix()
                else:
                    photos_path = base_photos_path
                    custom_sub = ""

                base_slug = _slugify(username if not custom_sub else f"{username}-{custom_sub}")
                default_display = f"{username} (Personal Space)"
            else:
                base_photos_path = self.base_settings.photos_dir
                if raw_custom_sub:
                    photos_path = _resolve_contained_subfolder(base_photos_path, raw_custom_sub)
                    custom_sub = photos_path.relative_to(base_photos_path).as_posix()
                else:
                    photos_path = base_photos_path
                    custom_sub = ""

                base_slug = _slugify(req.display_name or custom_sub or "shared")
                default_display = req.display_name.strip() or (
                    f"Shared / {custom_sub}" if custom_sub else "Shared Space"
                )

            if base_slug.lower() in RESERVED_PROFILE_IDS:
                raise ValueError(f"Profile identifier '{base_slug}' is reserved.")

            resolved_candidate_photos = photos_path.resolve()
            for existing in self._profiles.values():
                if Path(existing.photos_dir).resolve() == resolved_candidate_photos:
                    raise ValueError(
                        f"A profile targeting '{photos_path}' already exists ('{existing.id}')."
                    )

            profile_id = base_slug
            counter = 2
            while profile_id in self._profiles or profile_id.lower() in RESERVED_PROFILE_IDS:
                profile_id = f"{base_slug}-{counter}"
                counter += 1

            if not PROFILE_ID_RE.match(profile_id) or profile_id.lower() in RESERVED_PROFILE_IDS:
                raise ValueError(f"Invalid profile identifier: {profile_id}")

            profile = UserProfile(
                id=profile_id,
                display_name=req.display_name.strip() or default_display,
                dsm_username=username,
                target_space=req.target_space,
                photos_dir=str(photos_path),
                enabled=True,
                is_default=False,
            )
            ctx = self._build_context(profile)
            self._profiles[profile.id] = profile
            self._contexts[profile.id] = ctx
            self._save_profiles()
            return ctx

    def update_profile(self, profile_id: str, req: UpdateProfileRequest) -> dict[str, Any]:
        with self._lock:
            ctx = self.get_context(profile_id)
            if req.display_name is not None and req.display_name.strip():
                ctx.profile.display_name = req.display_name.strip()
            if req.enabled is not None:
                ctx.profile.enabled = req.enabled
            self._save_profiles()
            return ctx.profile.model_dump(mode="json")

    def delete_profile(self, profile_id: str) -> None:
        with self._lock:
            if profile_id == "default" or profile_id.lower() in RESERVED_PROFILE_IDS:
                raise ValueError("The default or reserved profile cannot be deleted.")
            if profile_id not in self._profiles:
                raise KeyError(f"Profile '{profile_id}' does not exist")

            self._profiles.pop(profile_id, None)
            ctx = self._contexts.pop(profile_id, None)

            if PROFILE_ID_RE.match(profile_id) and profile_id.lower() not in RESERVED_PROFILE_IDS:
                cleanup_dirs = [
                    self.base_settings.data_dir / "profiles" / profile_id,
                    self.base_settings.work_dir / "profiles" / profile_id,
                    self.base_settings.inbox_dir / profile_id,
                ]
                if self.base_settings.quarantine_dir_override is not None:
                    cleanup_dirs.append(
                        self.base_settings.quarantine_dir_override / "profiles" / profile_id
                    )

                db_lock = ctx.db._lock if ctx is not None else contextlib.nullcontext()
                with db_lock:
                    for target_dir in cleanup_dirs:
                        shutil.rmtree(target_dir, ignore_errors=True)

            self._save_profiles()
```

---

## 3. Exact Integration Contract Between `profiles.py` and `web/routes.py` / `web/security.py` (C2)

When `GET /api/profiles` and `_profile_ctx` in `src/gphotos_synology/web/routes.py` interact with `ProfileRegistry`:

1. **`GET /api/profiles` response contract**:
   - Required by `tests/e2e/test_tier1_features.py:139-148`, `tests/e2e/test_tier2_boundaries.py:204-212,240-245`, and `tests/test_profiles.py:44-47,70-72`:
   - Response keys:
     - `"profiles"`: `app_ctx.registry.list_profiles(dsm_user=identity.dsm_user, is_admin=identity.is_admin)`
     - `"is_admin"`: `identity.is_admin` (`bool`)
     - `"dsm_user"`: `identity.dsm_user` (`str`)
     - `"current_dsm_user"`: `identity.dsm_user` (`str`, kept alongside `"dsm_user"` for `app.js` and `tests/test_profiles.py`)
     - `"recommended_profile_id"`:
       - If `identity.is_admin`: `app_ctx.registry.resolve_profile_for_dsm_user(identity.dsm_user, fallback="default")`
       - If `not identity.is_admin`: `app_ctx.registry.resolve_profile_for_dsm_user(identity.dsm_user, fallback="")`
     - `"homes_mounted"`: `discovery["homes_mounted"] if identity.is_admin else False`
     - `"discovered_synology_users"`: `discovery["users"] if identity.is_admin else []` (ensures non-admin `alice` never sees `bob` in `discovered_synology_users`, verified by `test_t2_c2_non_admin_cannot_access_other_profile_or_admin_actions` line 210–211).

2. **`_profile_ctx(request: Request, profile: str | None = None) -> ProfileContext` in `routes.py`**:
   - Extract `identity = get_request_identity(request)` (from `web/security.py`).
   - Determine requested `pid = (profile or request.query_params.get("profile") or request.headers.get("x-profile-id") or "").strip()`.
   - If `not identity.is_admin`:
     - If not `pid`:
       - `pid = app_ctx.registry.resolve_profile_for_dsm_user(identity.dsm_user, fallback="")`
       - If not `pid`: raise `HTTPException(status_code=403, detail="No migration profile is assigned to your DSM account.")`
     - Look up `pctx = app_ctx.registry.get_context(pid)` (if `KeyError`, raise `HTTPException(status_code=403, ...)` so non-admins cannot enumerate profile IDs!).
     - Verify ownership: `if not app_ctx.registry.user_owns_profile(identity.dsm_user, pctx.profile.id): raise HTTPException(status_code=403, detail="Access denied to another user's profile.")`
     - Return `pctx`.
   - If `identity.is_admin`:
     - `pid = pid or "default"`
     - Look up `app_ctx.registry.get_context(pid)` (raising `404` on `KeyError`).

---

## 4. Concrete Test Plan for `tests/test_profiles.py`

In `tests/test_profiles.py`:
1. Update `test_multi_user_discovery_profile_isolation_and_dsm_auto_select` to configure `proxy_secret` on `Settings` and pass authenticated proxy + CSRF headers (`X-Proxy-Secret`, `X-Proxy-Mode: cgi`, `X-DSM-User`, `X-DSM-Admin`, `X-Requested-With: XMLHttpRequest`) so it passes under C1/C2/G6 enforcement.
2. Add unit tests directly testing:
   - `test_g14_resolve_contained_subfolder_and_personal_home_containment`:
     - Rejects `../`, `../../maria/Photos`, `/etc/passwd`, `\`, `\x00`, `.`, symlink inside `Photos` pointing outside, and symlinked `homes_dir / username` pointing outside `homes_dir`, for both `personal` and `shared` target spaces.
     - Accepts nested relative subfolders like `"GooglePhotos/Alice"`.
   - `test_reserved_slugs_and_duplicate_photos_dir_rejected`:
     - Rejects `"default"`, `"processed"`, `"profiles"` (and case variants `"Processed"`, `"PROFILES"`).
     - Rejects creating a second profile with the same resolved `photos_dir`.
   - `test_delete_profile_cleans_disk_artifacts_and_preserves_photos`:
     - Verifies `data_dir / "profiles" / id`, `work_dir / "profiles" / id`, and `inbox_dir / id` are removed on `delete_profile(id)`, `photos_dir` files remain intact, and re-creating `id` starts with a clean `StateDB` and no leftover `google_token.json`.
   - `test_lazy_oauth_and_url_inheritance_across_rotation`:
     - Verifies non-default profiles created before or after `default` OAuth credentials are configured dynamically inherit `google_client_id`, `google_client_secret`, and `synology_url` across rotations without restart.
   - `test_corrupt_profiles_json_and_corrupt_profile_state_db_recovery`:
     - Verifies corrupt `profiles.json` is logged at `ERROR`, backed up to `profiles.json.corrupt.<UTC_timestamp>`, and rewritten with `0o600` permissions (logging `WARNING` if `os.chmod` raises `OSError`).
     - Verifies corrupt non-default `state.db` at startup is logged at `ERROR`, backed up to `state.db.corrupt.<UTC_timestamp>`, marks the profile `enabled=False`, and allows `ProfileRegistry` / `create_app` to start cleanly.

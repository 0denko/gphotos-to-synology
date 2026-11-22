"""Multi-user Synology DSM profile registry and /volume1/homes user auto-discovery."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from gphotos_synology.config import ConfigManager, Settings
from gphotos_synology.database import StateDB
from gphotos_synology.worker.pipeline import MigrationPipeline

logger = logging.getLogger(__name__)

PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,48}$")
RESERVED_PROFILE_IDS: set[str] = {"default", "processed", "profiles"}

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
    """Resolve `base_dir / custom_sub` and verify strict containment within `base_dir` (G14)."""
    if not custom_sub:
        return base_dir.resolve()
    if "\\" in custom_sub or custom_sub.startswith("/"):
        raise ValueError("Invalid subfolder path.")
    sub_path = Path(custom_sub)
    if sub_path.is_absolute() or ".." in sub_path.parts:
        raise ValueError("Invalid subfolder path.")
    resolved_base = base_dir.resolve()
    resolved_target = (resolved_base / sub_path).resolve()
    if resolved_target != resolved_base and resolved_base not in resolved_target.parents:
        raise ValueError("Invalid subfolder path.")
    return resolved_target


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
            "personal" if "/homes/" in str(self.base_settings.photos_dir).replace("\\", "/") else "shared"
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

    def _backup_corrupt_profiles(self, exc: Exception) -> None:
        ts = int(time.time())
        backup = self._profiles_path.with_name(f"profiles.json.corrupt.{ts}")
        try:
            shutil.copy2(self._profiles_path, backup)
            logger.error(
                "Corrupt profiles.json backed up to %s (%s); reinitializing default profile",
                backup,
                exc,
            )
        except OSError as copy_exc:
            logger.error("Corrupt profiles.json (%s) and backup failed (%s)", exc, copy_exc)

    def _load_or_init(self) -> None:
        loaded: list[UserProfile] = []
        if self._profiles_path.exists():
            try:
                raw = json.loads(self._profiles_path.read_text(encoding="utf-8"))
                if not isinstance(raw, list):
                    raise ValueError("profiles.json root must be a JSON list")
                for item in raw:
                    loaded.append(UserProfile.model_validate(item))
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                self._backup_corrupt_profiles(exc)
                loaded = []

        if not any(p.id == "default" for p in loaded):
            loaded.insert(0, self._default_profile())

        # Always build default profile first so lazy OAuth inheritance resolves cleanly
        loaded.sort(key=lambda p: (0 if p.id == "default" else 1))

        for profile in loaded:
            try:
                ctx = self._build_context(profile)
            except Exception as exc:
                if profile.id == "default":
                    raise
                logger.error(
                    "Skipping profile '%s' because its database/config failed to initialize: %s",
                    profile.id,
                    exc,
                )
                continue
            self._profiles[profile.id] = profile
            self._contexts[profile.id] = ctx

        self._save_profiles()

    def _save_profiles(self) -> None:
        payload = [p.model_dump(mode="json") for p in self._profiles.values()]
        tmp = self._profiles_path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, indent=2))
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, self._profiles_path)

    def _default_settings_provider(self) -> Settings:
        default_ctx = self._contexts.get("default")
        if default_ctx is not None:
            return default_ctx.config_manager.settings
        return self.base_settings

    def _build_context(self, profile: UserProfile) -> ProfileContext:
        if profile.is_default or profile.id == "default":
            cfg_mgr = ConfigManager(base_settings=self.base_settings)
        else:
            base_dump = self.base_settings.model_dump()
            # Clear inherited fields in base_dump so ConfigManager resolves them lazily from default
            base_dump["google_client_id"] = ""
            base_dump["google_client_secret"] = ""
            base_dump["data_dir"] = self.base_settings.data_dir / "profiles" / profile.id
            base_dump["work_dir"] = self.base_settings.work_dir / "profiles" / profile.id
            base_dump["inbox_dir"] = self.base_settings.inbox_dir / profile.id
            base_dump["photos_dir"] = Path(profile.photos_dir)
            base_dump["synology_space"] = profile.target_space
            if profile.dsm_username and not base_dump.get("synology_username"):
                base_dump["synology_username"] = profile.dsm_username

            profile_settings = Settings.model_validate(base_dump)
            cfg_mgr = ConfigManager(
                base_settings=profile_settings,
                fallback_settings_provider=self._default_settings_provider,
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

    def list_profiles(self, *, dsm_user_filter: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            target_user = dsm_user_filter.strip().lower() if dsm_user_filter else None
            result: list[dict[str, Any]] = []
            for ctx in self._contexts.values():
                if target_user is not None and ctx.profile.dsm_username.lower() != target_user:
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

    def discover_synology_users(self, *, only_username: str | None = None) -> dict[str, Any]:
        """Inspect mounted /data/homes (/volume1/homes) to list available Synology DSM users."""
        homes = self.base_settings.homes_dir
        if not homes.is_dir():
            return {"homes_mounted": False, "users": []}

        existing_usernames = {
            p.dsm_username.lower() for p in self._profiles.values() if p.dsm_username
        }
        filter_lower = only_username.strip().lower() if only_username else None
        users: list[dict[str, Any]] = []
        try:
            for entry in sorted(homes.iterdir(), key=lambda p: p.name.lower()):
                if not entry.is_dir():
                    continue
                if entry.name.startswith(".") or entry.name in IGNORED_HOME_DIRS:
                    continue
                if filter_lower is not None and entry.name.lower() != filter_lower:
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

    def resolve_profile_for_dsm_user(self, dsm_username: str | None) -> str:
        """Return matching profile ID for the logged-in DSM user, falling back to 'default'."""
        if not dsm_username:
            return "default"
        target = dsm_username.strip().lower()
        with self._lock:
            for profile in self._profiles.values():
                if profile.dsm_username.lower() == target:
                    return profile.id
        return "default"

    def create_profile(self, req: CreateProfileRequest) -> ProfileContext:
        """Create a new Synology user or Shared Space migration profile."""
        with self._lock:
            username = req.dsm_username.strip()
            custom_sub = req.custom_subfolder.strip().rstrip("/")

            if req.target_space == "personal":
                if not username:
                    raise ValueError("Synology DSM username is required for Personal Space profiles.")
                if "/" in username or "\\" in username or ".." in username:
                    raise ValueError("Invalid Synology username.")
                user_base = _resolve_contained_subfolder(self.base_settings.homes_dir, username)
                photos_base = user_base / "Photos"
                photos_path = _resolve_contained_subfolder(photos_base, custom_sub)
                base_slug = _slugify(username if not custom_sub else f"{username}-{custom_sub}")
                default_display = f"{username} (Personal Space)"
            else:
                photos_path = _resolve_contained_subfolder(self.base_settings.photos_dir, custom_sub)
                base_slug = _slugify(req.display_name or custom_sub or "shared")
                default_display = req.display_name.strip() or (
                    f"Shared / {custom_sub}" if custom_sub else "Shared Space"
                )

            # Reject duplicate target photos_dir across profiles so two DBs never contend on one folder
            resolved_new = photos_path.resolve()
            for existing in self._profiles.values():
                if Path(existing.photos_dir).resolve() == resolved_new:
                    raise ValueError(
                        f"Photos directory '{resolved_new}' is already managed by profile '{existing.id}'."
                    )

            profile_id = base_slug
            if profile_id in RESERVED_PROFILE_IDS:
                profile_id = f"{base_slug}-2"
            counter = 2
            while profile_id in self._profiles or profile_id in RESERVED_PROFILE_IDS:
                profile_id = f"{base_slug}-{counter}"
                counter += 1

            if not PROFILE_ID_RE.match(profile_id):
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
        """Remove a non-default profile and clean up its isolated DB, token, work, and inbox dirs."""
        with self._lock:
            if profile_id == "default":
                raise ValueError("The default profile cannot be deleted.")
            if profile_id not in self._profiles:
                raise KeyError(f"Profile '{profile_id}' does not exist")
            ctx = self._contexts.pop(profile_id, None)
            self._profiles.pop(profile_id, None)
            self._save_profiles()

            if ctx is not None:
                settings = ctx.config_manager.settings
                for path in (
                    settings.token_path,
                    settings.data_dir,
                    settings.work_dir,
                    settings.inbox_dir,
                ):
                    try:
                        if path.is_file():
                            path.unlink(missing_ok=True)
                        elif path.is_dir():
                            shutil.rmtree(path, ignore_errors=True)
                    except OSError as exc:
                        logger.warning("Cleanup failed for deleted profile '%s' path %s: %s", profile_id, path, exc)

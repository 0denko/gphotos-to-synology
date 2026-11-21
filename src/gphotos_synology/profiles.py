"""Multi-user Synology DSM profile registry and /volume1/homes user auto-discovery."""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from gphotos_synology.config import ConfigManager, Settings
from gphotos_synology.database import StateDB
from gphotos_synology.worker.pipeline import MigrationPipeline

PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,48}$")

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

    def _load_or_init(self) -> None:
        loaded: list[UserProfile] = []
        if self._profiles_path.exists():
            try:
                raw = json.loads(self._profiles_path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    for item in raw:
                        loaded.append(UserProfile.model_validate(item))
            except (OSError, json.JSONDecodeError, ValueError):
                loaded = []

        if not any(p.id == "default" for p in loaded):
            loaded.insert(0, self._default_profile())

        for profile in loaded:
            self._profiles[profile.id] = profile
            self._contexts[profile.id] = self._build_context(profile)

        self._save_profiles()

    def _save_profiles(self) -> None:
        payload = [p.model_dump(mode="json") for p in self._profiles.values()]
        tmp = self._profiles_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self._profiles_path)

    def _build_context(self, profile: UserProfile) -> ProfileContext:
        if profile.is_default or profile.id == "default":
            cfg_mgr = ConfigManager(base_settings=self.base_settings)
        else:
            # Inherit shared Google OAuth client credentials from default profile if configured
            default_ctx = self._contexts.get("default")
            base_dump = self.base_settings.model_dump()
            if default_ctx is not None:
                default_s = default_ctx.config_manager.settings
                if default_s.google_client_id:
                    base_dump["google_client_id"] = default_s.google_client_id
                if default_s.google_client_secret:
                    base_dump["google_client_secret"] = default_s.google_client_secret
                if default_s.synology_url:
                    base_dump["synology_url"] = default_s.synology_url

            base_dump["data_dir"] = self.base_settings.data_dir / "profiles" / profile.id
            base_dump["work_dir"] = self.base_settings.work_dir / "profiles" / profile.id
            base_dump["inbox_dir"] = self.base_settings.inbox_dir / profile.id
            base_dump["photos_dir"] = Path(profile.photos_dir)
            base_dump["synology_space"] = profile.target_space
            if profile.dsm_username and not base_dump.get("synology_username"):
                base_dump["synology_username"] = profile.dsm_username

            profile_settings = Settings.model_validate(base_dump)
            cfg_mgr = ConfigManager(base_settings=profile_settings)

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

    def list_profiles(self) -> list[dict[str, Any]]:
        with self._lock:
            result: list[dict[str, Any]] = []
            for ctx in self._contexts.values():
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
                if not entry.is_dir():
                    continue
                if entry.name.startswith(".") or entry.name in IGNORED_HOME_DIRS:
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
            custom_sub = req.custom_subfolder.strip().strip("/")

            if req.target_space == "personal":
                if not username:
                    raise ValueError("Synology DSM username is required for Personal Space profiles.")
                if "/" in username or "\\" in username or ".." in username:
                    raise ValueError("Invalid Synology username.")
                photos_path = self.base_settings.homes_dir / username / "Photos"
                if custom_sub:
                    photos_path = photos_path / custom_sub
                base_slug = _slugify(username if not custom_sub else f"{username}-{custom_sub}")
                default_display = f"{username} (Personal Space)"
            else:
                photos_path = self.base_settings.photos_dir
                if custom_sub:
                    if ".." in Path(custom_sub).parts:
                        raise ValueError("Invalid subfolder path.")
                    photos_path = photos_path / custom_sub
                base_slug = _slugify(req.display_name or custom_sub or "shared")
                default_display = req.display_name.strip() or (
                    f"Shared / {custom_sub}" if custom_sub else "Shared Space"
                )

            profile_id = base_slug
            if profile_id == "default":
                profile_id = f"{base_slug}-2"
            counter = 2
            while profile_id in self._profiles:
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
        with self._lock:
            if profile_id == "default":
                raise ValueError("The default profile cannot be deleted.")
            if profile_id not in self._profiles:
                raise KeyError(f"Profile '{profile_id}' does not exist")
            self._profiles.pop(profile_id, None)
            self._contexts.pop(profile_id, None)
            self._save_profiles()

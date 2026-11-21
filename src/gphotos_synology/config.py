"""Configuration management with environment defaults and persistent runtime overrides."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables and persisted UI overrides."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Storage paths
    data_dir: Path = Field(default=Path("/data/config"))
    work_dir: Path = Field(default=Path("/data/work"))
    inbox_dir: Path = Field(default=Path("/data/inbox"))
    photos_dir: Path = Field(default=Path("/data/photos"))
    homes_dir: Path = Field(default=Path("/data/homes"))

    # Server
    app_port: int = Field(default=8765)

    # Background polling & safety guards
    poll_interval_minutes: int = Field(default=60, ge=1, le=10080)
    min_free_disk_gb: float = Field(default=5.0, ge=0.1)
    inbox_stability_seconds: int = Field(default=15, ge=0, le=3600)

    # Organization & post-processing
    folder_template: Literal["year_month", "year_ym", "album"] = Field(default="year_month")
    inbox_post_action: Literal["move_to_processed", "delete", "keep"] = Field(
        default="move_to_processed"
    )
    gdrive_post_action: Literal["keep", "trash"] = Field(default="keep")
    gdrive_folder_name: str = Field(default="Takeout")
    gdrive_archive_prefix: str = Field(default="takeout-")

    # Google OAuth2 (can also be configured in the Web Dashboard)
    google_client_id: str = Field(default="")
    google_client_secret: str = Field(default="")
    google_redirect_uri: str = Field(default="")

    # EXIF metadata repair & deduplication
    write_exif_metadata: bool = Field(default=True)
    write_album_tags: bool = Field(default=True)
    exiftool_path: str = Field(default="exiftool")
    auto_index_existing_photos: bool = Field(default=True)
    dedup_mode: Literal["strict_hash", "exact_and_pixels", "perceptual"] = Field(
        default="perceptual"
    )
    perceptual_max_distance: int = Field(default=10, ge=0, le=32)
    duplicate_quality_policy: Literal["auto_upgrade_quarantine", "keep_existing"] = Field(
        default="auto_upgrade_quarantine"
    )

    # Optional Synology DSM 7 Photos API (for virtual albums & immediate registration)
    synology_api_enabled: bool = Field(default=False)
    synology_url: str = Field(default="http://172.17.0.1:5000")
    synology_username: str = Field(default="")
    synology_password: str = Field(default="")
    synology_space: Literal["personal", "shared"] = Field(default="personal")
    synology_verify_ssl: bool = Field(default=False)
    synology_album_sync_retry_minutes: int = Field(default=15, ge=1, le=1440)

    # Notifications
    webhook_url: str = Field(default="")
    webhook_type: Literal["generic", "discord", "slack", "ntfy"] = Field(default="generic")
    smtp_host: str = Field(default="")
    smtp_port: int = Field(default=587)
    smtp_username: str = Field(default="")
    smtp_password: str = Field(default="")
    smtp_from: str = Field(default="")
    smtp_to: str = Field(default="")
    smtp_use_tls: bool = Field(default=True)

    # Annual Takeout schedule reminder
    takeout_renewal_reminder_days: int = Field(default=335, ge=30, le=365)

    # Software updates (GitHub release/commit check + optional Watchtower self-updater)
    github_repo: str = Field(default="0denko/gphotos-to-synology")
    app_build_sha: str = Field(default="dev")
    app_build_date: str = Field(default="")
    auto_check_updates: bool = Field(default=True)
    watchtower_url: str = Field(default="")
    watchtower_token: str = Field(default="")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "state.db"

    @property
    def token_path(self) -> Path:
        return self.data_dir / "google_token.json"

    @property
    def runtime_config_path(self) -> Path:
        return self.data_dir / "runtime_settings.json"

    @property
    def quarantine_dir(self) -> Path:
        """Safe quarantine folder outside PHOTOS_DIR so Synology Photos never indexes replaced copies."""
        return self.work_dir / "replaced_duplicates"

    def ensure_directories(self) -> None:
        for directory in (
            self.data_dir,
            self.work_dir,
            self.quarantine_dir,
            self.inbox_dir,
            self.photos_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)


# Fields that can be safely modified from the Web UI and persisted across restarts
MUTABLE_UI_FIELDS: set[str] = {
    "poll_interval_minutes",
    "min_free_disk_gb",
    "inbox_stability_seconds",
    "folder_template",
    "inbox_post_action",
    "gdrive_post_action",
    "gdrive_folder_name",
    "gdrive_archive_prefix",
    "google_client_id",
    "google_client_secret",
    "google_redirect_uri",
    "write_exif_metadata",
    "write_album_tags",
    "auto_index_existing_photos",
    "dedup_mode",
    "perceptual_max_distance",
    "duplicate_quality_policy",
    "synology_api_enabled",
    "synology_url",
    "synology_username",
    "synology_password",
    "synology_space",
    "synology_verify_ssl",
    "synology_album_sync_retry_minutes",
    "webhook_url",
    "webhook_type",
    "smtp_host",
    "smtp_port",
    "smtp_username",
    "smtp_password",
    "smtp_from",
    "smtp_to",
    "smtp_use_tls",
    "takeout_renewal_reminder_days",
    "auto_check_updates",
}

SECRET_FIELDS: set[str] = {
    "google_client_secret",
    "synology_password",
    "smtp_password",
    "watchtower_token",
}


class ConfigManager:
    """Manages active Settings combined with persisted runtime JSON overrides."""

    def __init__(self, base_settings: Settings | None = None) -> None:
        self._base = base_settings or Settings()
        self._base.ensure_directories()
        self._settings = self._load_merged()

    @property
    def settings(self) -> Settings:
        return self._settings

    def _load_merged(self) -> Settings:
        merged_data = self._base.model_dump()
        override_path = self._base.runtime_config_path
        if override_path.exists():
            try:
                overrides = json.loads(override_path.read_text(encoding="utf-8"))
                if isinstance(overrides, dict):
                    for key, value in overrides.items():
                        if key in MUTABLE_UI_FIELDS:
                            merged_data[key] = value
            except (json.JSONDecodeError, OSError):
                pass
        return Settings.model_validate(merged_data)

    def update(self, patch: dict[str, Any]) -> Settings:
        """Persist validated runtime overrides and refresh active settings."""
        filtered: dict[str, Any] = {}
        for key, value in patch.items():
            if key not in MUTABLE_UI_FIELDS:
                continue
            # Keep existing secret if UI sends empty mask sentinel
            if key in SECRET_FIELDS and value == "********":
                continue
            filtered[key] = value

        existing: dict[str, Any] = {}
        override_path = self._base.runtime_config_path
        if override_path.exists():
            try:
                loaded = json.loads(override_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing = loaded
            except (json.JSONDecodeError, OSError):
                existing = {}

        existing.update(filtered)

        # Validate merged config before saving
        candidate_data = self._base.model_dump()
        candidate_data.update(existing)
        validated = Settings.model_validate(candidate_data)

        tmp_path = override_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(existing, indent=2, sort_keys=True), encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, override_path)

        self._settings = validated
        return self._settings

    def to_public_dict(self) -> dict[str, Any]:
        """Return settings dictionary with secret fields masked for API responses."""
        data = self._settings.model_dump(mode="json")
        for secret in SECRET_FIELDS:
            if data.get(secret):
                data[secret] = "********"
        return data

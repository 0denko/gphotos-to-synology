"""Configuration management with environment defaults and persistent runtime overrides."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

DEFAULT_SYNOLOGY_URL = "https://127.0.0.1:5001"
LEGACY_DEFAULT_SYNOLOGY_URL = "http://172.17.0.1:5000"
MASK_SENTINEL = "********"


def validate_synology_url(url: Any) -> str:
    """Validate that a Synology DSM URL is a well-formed http:// or https:// URL."""
    if not isinstance(url, str) or not url:
        raise ValueError("synology_url must be a non-empty http:// or https:// URL")
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        raise ValueError("synology_url must not contain whitespace or control characters")
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("synology_url must use http:// or https:// scheme")
    if not parsed.hostname:
        raise ValueError("synology_url must include a valid hostname")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("synology_url contains an invalid port") from exc
    return url


def _normalize_url_for_compare(url: str) -> tuple[str, str, str, str, str]:
    """Normalize a URL into a comparable tuple ignoring case in scheme/netloc and trailing slashes."""
    if not url:
        return ("", "", "", "", "")
    parsed = urlsplit(url)
    return (
        parsed.scheme.lower(),
        parsed.netloc.lower(),
        parsed.path.rstrip("/"),
        parsed.query,
        parsed.fragment,
    )


class Settings(BaseSettings):
    """Application settings loaded from environment variables and persisted UI overrides."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # Storage paths
    data_dir: Path = Field(default=Path("/data/config"))
    work_dir: Path = Field(default=Path("/data/work"))
    inbox_dir: Path = Field(default=Path("/data/inbox"))
    photos_dir: Path = Field(default=Path("/data/photos"))
    homes_dir: Path = Field(default=Path("/data/homes"))
    quarantine_dir_override: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("quarantine_dir", "quarantine_dir_override"),
    )

    # Server
    app_host: str = Field(default="127.0.0.1")
    app_port: int = Field(default=8765)

    # Security & authentication (C1 / C2 / G6 — environment/SPK controlled only)
    proxy_secret: str = Field(
        default="",
        validation_alias=AliasChoices("gphotos_proxy_secret", "proxy_secret"),
    )
    proxy_secret_file: Path | None = Field(
        default=None,
        validation_alias=AliasChoices("proxy_secret_file", "gphotos_proxy_secret_file"),
    )
    api_token: str = Field(
        default="",
        validation_alias=AliasChoices("api_token", "app_api_token"),
    )
    dsm_admin_users: str = Field(default="")
    trust_proxy_headers: bool = Field(default=False)
    allow_insecure_no_auth: bool = Field(default=False)

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
    index_workers: int = Field(default=2, ge=1, le=4)
    index_throttle_cpu_pct: int = Field(default=90, ge=30, le=100)
    index_yield_cpu_pct: int = Field(default=100, ge=40, le=100)
    dedup_mode: Literal["strict_hash", "exact_and_pixels", "perceptual"] = Field(
        default="perceptual"
    )
    perceptual_max_distance: int = Field(default=10, ge=0, le=32)
    duplicate_quality_policy: Literal["auto_upgrade_quarantine", "keep_existing"] = Field(
        default="auto_upgrade_quarantine"
    )

    # Optional Synology DSM 7 Photos API (for virtual albums & immediate registration)
    synology_api_enabled: bool = Field(default=False)
    synology_url: str = Field(default=DEFAULT_SYNOLOGY_URL)
    synology_username: str = Field(default="")
    synology_password: str = Field(default="")
    synology_space: Literal["personal", "shared"] = Field(default="personal")
    synology_verify_ssl: bool = Field(default=True)
    synology_ca_cert: str = Field(default="")
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

    @field_validator("quarantine_dir_override", "proxy_secret_file", mode="before")
    @classmethod
    def _blank_path_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("synology_url")
    @classmethod
    def _validate_synology_url_field(cls, value: str) -> str:
        if value == "":
            return ""
        return validate_synology_url(value)

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
        if self.quarantine_dir_override is not None:
            return self.quarantine_dir_override
        return self.work_dir / "replaced_duplicates"

    def resolve_proxy_secret(self) -> str:
        """Return configured proxy secret (direct value takes precedence over secret file)."""
        direct = self.proxy_secret.strip()
        if direct:
            return direct
        if self.proxy_secret_file is not None:
            try:
                return self.proxy_secret_file.read_text(encoding="utf-8").strip()
            except OSError as exc:
                logger.warning(
                    "Failed to read proxy_secret_file %s: %s",
                    self.proxy_secret_file,
                    exc,
                )
                return ""
        return ""

    def dsm_admin_user_set(self) -> set[str]:
        """Return configured DSM admin usernames as a normalized lowercase set."""
        return {
            username.strip().lower()
            for username in self.dsm_admin_users.split(",")
            if username.strip()
        }

    def ensure_directories(self) -> None:
        for directory in (
            self.data_dir,
            self.work_dir,
            self.quarantine_dir,
            self.inbox_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            self.photos_dir.mkdir(parents=True, exist_ok=True)

    def is_photos_dir_writable(self) -> bool:
        """Return True if the process can create/write files inside photos_dir."""
        try:
            self.photos_dir.mkdir(parents=True, exist_ok=True)
            probe = self.photos_dir / f".gphotos_write_probe_{os.getpid()}"
            probe.write_bytes(b"ok")
            probe.unlink(missing_ok=True)
            return True
        except OSError:
            return False


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
    "index_workers",
    "index_throttle_cpu_pct",
    "index_yield_cpu_pct",
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
    "proxy_secret",
    "api_token",
}


def _backup_corrupt_file(override_path: Path, reason: str) -> Path | None:
    """Move a corrupt runtime settings file aside with a UTC timestamp suffix."""
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base_name = f"{override_path.name}.corrupt.{timestamp}"
    backup_path = override_path.with_name(base_name)
    counter = 1
    while backup_path.exists():
        backup_path = override_path.with_name(f"{base_name}.{counter}")
        counter += 1

    try:
        try:
            os.replace(override_path, backup_path)
        except OSError:
            shutil.copy2(override_path, backup_path)
            override_path.unlink(missing_ok=True)
    except OSError as exc:
        logger.error(
            "Corrupt runtime settings at %s (%s); failed to back up to %s: %s",
            override_path,
            reason,
            backup_path,
            exc,
        )
        return None

    logger.error(
        "Corrupt runtime settings at %s (%s); backed up to %s and falling back to base settings",
        override_path,
        reason,
        backup_path,
    )
    return backup_path


def _write_overrides_atomic(override_path: Path, data: dict[str, Any]) -> None:
    """Atomically write runtime overrides JSON with restrictive 0600 permissions."""
    override_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = f".{override_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    tmp_path = override_path.with_name(tmp_name)
    payload = json.dumps(data, indent=2, sort_keys=True)

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
                override_path,
                exc,
            )
        os.replace(tmp_path, override_path)
    except Exception:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise


class ConfigManager:
    """Manages active Settings combined with persisted runtime JSON overrides."""

    def __init__(
        self,
        base_settings: Settings | None = None,
        fallback_settings_provider: Callable[[], Settings] | None = None,
    ) -> None:
        self._base = base_settings or Settings()
        self._fallback_settings_provider = fallback_settings_provider
        self._lock = threading.RLock()
        self.last_update_warnings: list[str] = []
        self._runtime_overrides: dict[str, Any] = {}
        self._base.ensure_directories()
        self._own_settings = self._load_merged()

    @property
    def settings(self) -> Settings:
        with self._lock:
            own = self._own_settings
            overrides = dict(self._runtime_overrides)

        provider = self._fallback_settings_provider
        if provider is None:
            return own
        fallback = provider()
        if fallback is None or fallback is own:
            return own

        inherited: dict[str, Any] = {}
        if not own.google_client_id.strip() and fallback.google_client_id.strip():
            inherited["google_client_id"] = fallback.google_client_id
        if not own.google_client_secret.strip() and fallback.google_client_secret.strip():
            inherited["google_client_secret"] = fallback.google_client_secret
        if (
            "synology_url" not in overrides
            and own.synology_url in {"", DEFAULT_SYNOLOGY_URL, LEGACY_DEFAULT_SYNOLOGY_URL}
            and not own.synology_password
            and fallback.synology_url.strip()
            and fallback.synology_url != own.synology_url
        ):
            inherited["synology_url"] = fallback.synology_url

        if inherited:
            return own.model_copy(update=inherited)
        return own

    def _read_overrides_from_disk(self) -> dict[str, Any]:
        override_path = self._base.runtime_config_path
        if not override_path.exists():
            return {}
        try:
            raw_text = override_path.read_text(encoding="utf-8")
            loaded = json.loads(raw_text)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            _backup_corrupt_file(override_path, str(exc))
            return {}
        except OSError as exc:
            logger.error("Failed to read runtime settings %s: %s", override_path, exc)
            return {}

        if not isinstance(loaded, dict):
            _backup_corrupt_file(
                override_path,
                f"expected JSON object, got {type(loaded).__name__}",
            )
            return {}

        return {key: value for key, value in loaded.items() if key in MUTABLE_UI_FIELDS}

    def _load_merged(self) -> Settings:
        overrides = self._read_overrides_from_disk()
        merged_data = self._base.model_dump()
        merged_data.update(overrides)
        try:
            validated = Settings.model_validate(merged_data)
        except ValidationError as exc:
            override_path = self._base.runtime_config_path
            if override_path.exists():
                _backup_corrupt_file(override_path, f"validation failed: {exc}")
            self._runtime_overrides = {}
            return Settings.model_validate(self._base.model_dump())

        self._runtime_overrides = dict(overrides)
        return validated

    def update(self, patch: dict[str, Any]) -> Settings:
        """Persist validated runtime overrides and refresh active settings."""
        if "synology_url" in patch:
            validate_synology_url(patch["synology_url"])

        with self._lock:
            own = self._own_settings
            fallback = (
                self._fallback_settings_provider()
                if self._fallback_settings_provider is not None
                else None
            )
            prior_effective = self.settings
            prior_url = prior_effective.synology_url
            prior_password = prior_effective.synology_password

            filtered: dict[str, Any] = {}
            for key, value in patch.items():
                if key not in MUTABLE_UI_FIELDS:
                    continue
                # Keep existing secret if UI sends mask sentinel
                if key in SECRET_FIELDS and value == MASK_SENTINEL:
                    continue
                filtered[key] = value

            existing = self._read_overrides_from_disk()

            raw_pw = patch.get("synology_password")
            fresh_password_supplied = (
                isinstance(raw_pw, str) and bool(raw_pw) and raw_pw != MASK_SENTINEL
            )
            url_changed = (
                "synology_url" in filtered
                and _normalize_url_for_compare(filtered["synology_url"])
                != _normalize_url_for_compare(prior_url)
            )

            # Avoid freezing dynamically inherited fallback values during full-form UI saves
            if fallback is not None and fallback is not own:
                if (
                    "google_client_id" in filtered
                    and "google_client_id" not in existing
                    and not own.google_client_id.strip()
                    and fallback.google_client_id.strip()
                    and filtered["google_client_id"] == fallback.google_client_id
                ):
                    filtered.pop("google_client_id")

                will_have_password = fresh_password_supplied or (
                    bool(prior_password)
                    and not url_changed
                    and not ("synology_password" in filtered and filtered["synology_password"] == "")
                )
                if (
                    "synology_url" in filtered
                    and "synology_url" not in existing
                    and own.synology_url in {"", DEFAULT_SYNOLOGY_URL, LEGACY_DEFAULT_SYNOLOGY_URL}
                    and not own.synology_password
                    and fallback.synology_url.strip()
                    and _normalize_url_for_compare(filtered["synology_url"])
                    == _normalize_url_for_compare(fallback.synology_url)
                    and not will_have_password
                ):
                    filtered.pop("synology_url")
                elif (
                    fresh_password_supplied
                    and "synology_url" not in filtered
                    and "synology_url" not in existing
                    and prior_effective.synology_url
                ):
                    # Pin effective URL locally whenever saving a password so later fallback
                    # URL changes cannot redirect this profile's stored credentials.
                    filtered["synology_url"] = prior_effective.synology_url

            existing.update(filtered)

            warnings: list[str] = []
            if url_changed and not fresh_password_supplied:
                # Explicitly store empty string so even env-var SYNOLOGY_PASSWORD is overridden
                existing["synology_password"] = ""
                if prior_password:
                    msg = (
                        "Synology DSM URL changed without a new password; "
                        "cleared stored Synology password for security. "
                        "Please re-enter your DSM password if needed."
                    )
                    warnings.append(msg)
                    logger.warning(
                        "Cleared stored synology_password because synology_url changed from %s to %s",
                        prior_url,
                        filtered["synology_url"],
                    )

            # Validate merged config before writing to disk
            candidate_data = self._base.model_dump()
            candidate_data.update(existing)
            validated = Settings.model_validate(candidate_data)

            _write_overrides_atomic(self._base.runtime_config_path, existing)

            self._runtime_overrides = dict(existing)
            self._own_settings = validated
            self.last_update_warnings = warnings
            return self.settings

    def to_public_dict(self) -> dict[str, Any]:
        """Return settings dictionary with secret fields masked for API responses."""
        data = self.settings.model_dump(mode="json")
        for secret in SECRET_FIELDS:
            if data.get(secret):
                data[secret] = MASK_SENTINEL
        return data

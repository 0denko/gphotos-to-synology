"""GitHub update checker and Watchtower self-update trigger."""

from __future__ import annotations

import logging
import platform
import threading
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from gphotos_synology import __version__
from gphotos_synology.config import Settings

logger = logging.getLogger(__name__)


def _host_spk_arch() -> str:
    machine = (platform.machine() or "").lower()
    if machine in {"aarch64", "arm64"}:
        return "arm64"
    return "x86_64"


@dataclass
class UpdateStatus:
    current_version: str
    current_sha: str
    build_date: str
    latest_version: str = ""
    latest_sha: str = ""
    latest_commit_message: str = ""
    latest_commit_date: str = ""
    update_available: bool = False
    release_url: str = ""
    spk_download_url: str = ""
    auto_updater_configured: bool = False
    checked_at: str | None = None
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class UpdateService:
    """Checks GitHub for newer builds and triggers Watchtower self-updates on demand."""

    def __init__(self, *, cache_ttl_seconds: int = 1800) -> None:
        self._lock = threading.Lock()
        self._cache_ttl = cache_ttl_seconds
        self._last_checked_mono: float = 0.0
        self._cached: UpdateStatus | None = None

    @staticmethod
    def _short_sha(sha: str) -> str:
        cleaned = (sha or "").strip()
        return cleaned[:7] if len(cleaned) >= 7 else cleaned

    def get_status(
        self,
        settings: Settings,
        *,
        force: bool = False,
        http_client: httpx.Client | None = None,
    ) -> dict[str, Any]:
        """Return cached or freshly checked update status against GitHub."""
        with self._lock:
            now_mono = time.monotonic()
            if (
                not force
                and self._cached is not None
                and (now_mono - self._last_checked_mono) < self._cache_ttl
            ):
                # Refresh auto_updater_configured dynamically from current settings
                self._cached.auto_updater_configured = bool(
                    settings.watchtower_url.strip() and settings.watchtower_token.strip()
                )
                return self._cached.to_dict()

        status = self._fetch_from_github(settings, http_client=http_client)
        with self._lock:
            self._cached = status
            self._last_checked_mono = time.monotonic()
        return status.to_dict()

    def _fetch_from_github(
        self,
        settings: Settings,
        *,
        http_client: httpx.Client | None = None,
    ) -> UpdateStatus:
        repo = settings.github_repo.strip()
        current_sha = settings.app_build_sha.strip() or "dev"
        arch = _host_spk_arch()
        status = UpdateStatus(
            current_version=__version__,
            current_sha=self._short_sha(current_sha),
            build_date=settings.app_build_date.strip(),
            release_url=f"https://github.com/{repo}/releases/latest",
            spk_download_url=f"https://github.com/{repo}/releases/latest/download/gphotos-migrator-dsm7-{arch}.spk",
            auto_updater_configured=bool(
                settings.watchtower_url.strip() and settings.watchtower_token.strip()
            ),
            checked_at=datetime.now(UTC).isoformat(),
        )

        if not repo:
            return status

        owns_client = http_client is None
        client = http_client or httpx.Client(
            timeout=12.0,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"gphotos-to-synology/{__version__}",
            },
        )
        try:
            # 1. Check latest commit on main (matches ghcr.io :latest builds)
            commit_resp = client.get(f"https://api.github.com/repos/{repo}/commits/main")
            commit_resp.raise_for_status()
            commit_data = commit_resp.json()
            full_latest_sha = str(commit_data.get("sha") or "")
            commit_obj = commit_data.get("commit") or {}
            raw_msg = str(commit_obj.get("message") or "").splitlines()[0] if commit_obj.get("message") else ""
            commit_date = str((commit_obj.get("committer") or {}).get("date") or "")

            status.latest_sha = self._short_sha(full_latest_sha)
            status.latest_commit_message = raw_msg[:140]
            status.latest_commit_date = commit_date

            # 2. Check latest GitHub release tag & architecture-matching SPK asset URL
            try:
                rel_resp = client.get(f"https://api.github.com/repos/{repo}/releases/latest")
                if rel_resp.status_code == 200:
                    rel_data = rel_resp.json()
                    status.latest_version = str(rel_data.get("tag_name") or "").lstrip("v")
                    if rel_data.get("html_url"):
                        status.release_url = str(rel_data["html_url"])
                    fallback_spk = ""
                    for asset in rel_data.get("assets") or []:
                        name = str(asset.get("name", ""))
                        dl_url = str(asset.get("browser_download_url") or "")
                        if not (name.endswith(".spk") and dl_url):
                            continue
                        if f"-{arch}.spk" in name:
                            status.spk_download_url = dl_url
                            fallback_spk = ""
                            break
                        if not fallback_spk:
                            fallback_spk = dl_url
                    if fallback_spk:
                        status.spk_download_url = fallback_spk
            except Exception:
                pass

            # Determine if an update is available (compare SHAs when running a built image)
            if full_latest_sha and current_sha and current_sha != "dev":
                status.update_available = not full_latest_sha.lower().startswith(current_sha.lower())
            else:
                status.update_available = False
        except Exception as exc:
            status.error = f"Update check failed: {exc}"
            logger.debug("GitHub update check failed: %s", exc)
        finally:
            if owns_client:
                client.close()

        return status

    def trigger_self_update(
        self,
        settings: Settings,
        *,
        http_client: httpx.Client | None = None,
    ) -> dict[str, Any]:
        """Ask the scoped Watchtower companion container to pull the latest image and restart."""
        wt_url = settings.watchtower_url.strip().rstrip("/")
        wt_token = settings.watchtower_token.strip()
        if not wt_url or not wt_token:
            raise RuntimeError(
                "Auto-updater (Watchtower) is not configured. Re-deploy with the included "
                "docker-compose.yml or pull the latest image in Synology Container Manager."
            )

        owns_client = http_client is None
        client = http_client or httpx.Client(timeout=20.0)
        headers = {"Authorization": f"Bearer {wt_token}"}
        try:
            resp = client.post(f"{wt_url}/v1/update", params={"async": "1"}, headers=headers)
            if resp.status_code == 405:
                resp = client.get(f"{wt_url}/v1/update", headers=headers)
            resp.raise_for_status()
            return {
                "ok": True,
                "message": "Update triggered! Pulling the latest image and restarting — this window will reconnect automatically.",
            }
        finally:
            if owns_client:
                client.close()

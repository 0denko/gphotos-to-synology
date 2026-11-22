"""Google Drive API OAuth2 client for polling and downloading scheduled Takeout archives."""

from __future__ import annotations

import hashlib
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from gphotos_synology.config import Settings
from gphotos_synology.takeout.extractor import extract_batch_key, is_supported_archive

logger = logging.getLogger(__name__)

DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
DRIVE_FULL_SCOPE = "https://www.googleapis.com/auth/drive"


@dataclass
class DriveArchiveItem:
    """Metadata for a Google Takeout archive stored in Google Drive."""

    file_id: str
    name: str
    size_bytes: int
    md5_checksum: str
    created_time: str
    batch_key: str


def _compute_file_md5(path: Path) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class GoogleDriveClient:
    """Handles OAuth2 token lifecycle and background Google Drive Takeout polling."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.last_auth_error: str = ""

    @property
    def scopes(self) -> list[str]:
        if self.settings.gdrive_post_action == "trash":
            return [DRIVE_FULL_SCOPE]
        return [DRIVE_READONLY_SCOPE]

    def is_oauth_configured(self) -> bool:
        return bool(self.settings.google_client_id.strip() and self.settings.google_client_secret.strip())

    def _client_config(self, redirect_uri: str) -> dict:
        return {
            "web": {
                "client_id": self.settings.google_client_id.strip(),
                "client_secret": self.settings.google_client_secret.strip(),
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [redirect_uri],
            }
        }

    def build_authorization_url(
        self, redirect_uri: str, state: str | None = None
    ) -> tuple[str, str, str | None]:
        """Return (authorization_url, state, pkce_code_verifier) for initiating Google OAuth2 consent."""
        if not self.is_oauth_configured():
            raise ValueError("Google Client ID and Client Secret must be configured first.")
        flow = Flow.from_client_config(
            self._client_config(redirect_uri),
            scopes=self.scopes,
            redirect_uri=redirect_uri,
        )
        auth_url, returned_state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
            state=state,
        )
        return auth_url, returned_state, flow.code_verifier

    def exchange_code(
        self,
        code: str,
        redirect_uri: str,
        code_verifier: str | None = None,
    ) -> None:
        """Exchange an OAuth2 authorization code (with PKCE code_verifier) for tokens and persist them."""
        if not self.is_oauth_configured():
            raise ValueError("Google Client ID and Client Secret must be configured first.")
        flow = Flow.from_client_config(
            self._client_config(redirect_uri),
            scopes=self.scopes,
            redirect_uri=redirect_uri,
            code_verifier=code_verifier,
            autogenerate_code_verifier=False,
        )
        fetch_kwargs: dict[str, Any] = {"code": code}
        if code_verifier:
            fetch_kwargs["code_verifier"] = code_verifier
        flow.fetch_token(**fetch_kwargs)
        creds = flow.credentials
        self._save_credentials(creds)

    def _save_credentials(self, creds: Credentials) -> None:
        token_path = self.settings.token_path
        token_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = token_path.with_suffix(".tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(tmp, flags, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(creds.to_json())
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        try:
            os.chmod(tmp, 0o600)
        except OSError as exc:
            logger.warning("Could not enforce 0600 permissions on %s: %s", token_path, exc)
        os.replace(tmp, token_path)

    def disconnect(self) -> None:
        self.settings.token_path.unlink(missing_ok=True)
        self.last_auth_error = ""

    def get_credentials(self) -> Credentials | None:
        """Load stored OAuth2 credentials and refresh automatically if expired."""
        self.last_auth_error = ""
        token_path = self.settings.token_path
        if not token_path.exists():
            return None
        try:
            creds = Credentials.from_authorized_user_file(str(token_path))
        except Exception as exc:
            self.last_auth_error = f"Failed to load Google token file: {exc}"
            logger.error("%s", self.last_auth_error)
            return None

        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                self._save_credentials(creds)
            except Exception as exc:
                self.last_auth_error = f"Google OAuth token refresh failed: {exc}"
                logger.warning("%s", self.last_auth_error)
                raise

        return creds if creds.valid else None

    def connection_status(self) -> dict[str, Any]:
        """Return connection state plus any token refresh/load error for UI display."""
        try:
            creds = self.get_credentials()
            return {"connected": creds is not None, "error": self.last_auth_error}
        except Exception as exc:
            return {"connected": False, "error": self.last_auth_error or str(exc)}

    def is_connected(self) -> bool:
        return bool(self.connection_status()["connected"])

    def _build_service(self):
        creds = self.get_credentials()
        if creds is None:
            raise RuntimeError("Google Drive is not connected. Complete OAuth setup in the dashboard.")
        return build("drive", "v3", credentials=creds, cache_discovery=False)

    def find_takeout_folder_ids(self, service) -> list[str]:
        """Locate all non-trashed Drive folders matching `gdrive_folder_name` (paginated)."""
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
            folder_ids.extend(item["id"] for item in result.get("files", []) if item.get("id"))
            page_token = result.get("nextPageToken")
            if not page_token:
                break
        return folder_ids

    def list_takeout_archives(self) -> list[DriveArchiveItem]:
        """List all Takeout archive files in the configured Google Drive Takeout folder."""
        service = self._build_service()
        folder_ids = self.find_takeout_folder_ids(service)
        if not folder_ids:
            return []

        prefix = self.settings.gdrive_archive_prefix.lower()
        items: list[DriveArchiveItem] = []

        for folder_id in folder_ids:
            page_token: str | None = None
            while True:
                query = f"'{folder_id}' in parents and trashed = false"
                response = (
                    service.files()
                    .list(
                        q=query,
                        spaces="drive",
                        fields="nextPageToken, files(id, name, size, md5Checksum, createdTime)",
                        orderBy="createdTime asc, name asc",
                        pageSize=100,
                        pageToken=page_token,
                    )
                    .execute()
                )
                for f in response.get("files", []):
                    name = str(f.get("name", ""))
                    if not is_supported_archive(name):
                        continue
                    if prefix and not name.lower().startswith(prefix):
                        continue
                    items.append(
                        DriveArchiveItem(
                            file_id=str(f["id"]),
                            name=name,
                            size_bytes=int(f.get("size") or 0),
                            md5_checksum=str(f.get("md5Checksum") or ""),
                            created_time=str(f.get("createdTime") or ""),
                            batch_key=extract_batch_key(name),
                        )
                    )
                page_token = response.get("nextPageToken")
                if not page_token:
                    break

        return items

    def download_archive(
        self,
        item: DriveArchiveItem,
        dest_path: Path,
        *,
        on_progress: Callable[[float], None] | None = None,
        max_retries: int = 4,
    ) -> Path:
        """Download a Drive file with chunked streaming, MD5 verification, and retry backoff."""
        service = self._build_service()
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = dest_path.with_suffix(dest_path.suffix + ".part")

        for attempt in range(1, max_retries + 1):
            try:
                request = service.files().get_media(fileId=item.file_id)
                with open(tmp_path, "wb") as fh:
                    downloader = MediaIoBaseDownload(fh, request, chunksize=8 * 1024 * 1024)
                    done = False
                    while not done:
                        status, done = downloader.next_chunk(num_retries=3)
                        if status and on_progress:
                            on_progress(float(status.progress()) * 100.0)

                if item.md5_checksum:
                    actual_md5 = _compute_file_md5(tmp_path)
                    if actual_md5.lower() != item.md5_checksum.strip().lower():
                        raise ValueError(
                            f"MD5 checksum mismatch for {item.name}: "
                            f"expected {item.md5_checksum}, got {actual_md5}"
                        )

                os.replace(tmp_path, dest_path)
                return dest_path
            except Exception as exc:
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

    def trash_archive(self, file_id: str) -> None:
        """Move a processed archive to Google Drive Trash (reversible safety cleanup)."""
        service = self._build_service()
        service.files().update(fileId=file_id, body={"trashed": True}).execute()

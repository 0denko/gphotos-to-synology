"""Google Drive API OAuth2 client for polling and downloading scheduled Takeout archives."""

from __future__ import annotations

import contextlib
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

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


class GoogleDriveClient:
    """Handles OAuth2 token lifecycle and background Google Drive Takeout polling."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

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

    def build_authorization_url(self, redirect_uri: str, state: str | None = None) -> tuple[str, str]:
        """Return (authorization_url, state) for initiating Google OAuth2 consent."""
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
        return auth_url, returned_state

    def exchange_code(self, code: str, redirect_uri: str) -> None:
        """Exchange an OAuth2 authorization code for tokens and persist them."""
        if not self.is_oauth_configured():
            raise ValueError("Google Client ID and Client Secret must be configured first.")
        flow = Flow.from_client_config(
            self._client_config(redirect_uri),
            scopes=self.scopes,
            redirect_uri=redirect_uri,
        )
        flow.fetch_token(code=code)
        creds = flow.credentials
        self._save_credentials(creds)

    def _save_credentials(self, creds: Credentials) -> None:
        token_path = self.settings.token_path
        token_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = token_path.with_suffix(".tmp")
        tmp.write_text(creds.to_json(), encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        os.replace(tmp, token_path)

    def disconnect(self) -> None:
        self.settings.token_path.unlink(missing_ok=True)

    def get_credentials(self) -> Credentials | None:
        """Load stored OAuth2 credentials and refresh automatically if expired."""
        token_path = self.settings.token_path
        if not token_path.exists():
            return None
        try:
            creds = Credentials.from_authorized_user_file(str(token_path))
        except Exception as exc:
            logger.error("Failed to load Google token file: %s", exc)
            return None

        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            self._save_credentials(creds)

        return creds if creds.valid else None

    def is_connected(self) -> bool:
        try:
            return self.get_credentials() is not None
        except Exception:
            return False

    def _build_service(self):
        creds = self.get_credentials()
        if creds is None:
            raise RuntimeError("Google Drive is not connected. Complete OAuth setup in the dashboard.")
        return build("drive", "v3", credentials=creds, cache_discovery=False)

    def find_takeout_folder_ids(self, service) -> list[str]:
        """Locate all non-trashed Drive folders matching `gdrive_folder_name` (e.g. 'Takeout')."""
        folder_name = self.settings.gdrive_folder_name.replace("'", "\\'")
        query = (
            "mimeType = 'application/vnd.google-apps.folder' "
            f"and name = '{folder_name}' and trashed = false"
        )
        result = (
            service.files()
            .list(q=query, spaces="drive", fields="files(id, name)", pageSize=20)
            .execute()
        )
        return [item["id"] for item in result.get("files", [])]

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
        """Download a Drive file with chunked streaming and retry backoff."""
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

"""Optional Synology DSM 7 Photos API client for recreating native virtual albums."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from gphotos_synology.config import Settings

logger = logging.getLogger(__name__)


class SynologyAPIError(RuntimeError):
    """Raised when Synology DSM API returns an error response."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class SynologyPhotosClient:
    """Minimal, resilient DSM 7 Synology Photos HTTP client (Personal & Shared Space).

    Designed to work alongside direct folder imports: once files land in Synology Photos
    and are indexed by DSM, this client locates their item IDs and adds them to native
    Synology Photos virtual albums without duplicating files on disk.
    """

    def __init__(self, settings: Settings, *, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self.base_url = settings.synology_url.rstrip("/")
        self._owns_client = client is None
        verify: bool | str = (
            settings.synology_ca_cert.strip()
            if settings.synology_ca_cert.strip()
            else settings.synology_verify_ssl
        )
        from urllib.parse import urlsplit

        parsed_host = (urlsplit(self.base_url).hostname or "").lower()
        is_loopback = parsed_host in {"127.0.0.1", "localhost", "::1"}
        self._client = client or httpx.Client(
            base_url=self.base_url,
            verify=verify,
            timeout=30.0,
            trust_env=not is_loopback,
        )
        self._sid: str | None = None
        self._syno_token: str | None = None
        self._folder_cache: dict[str, int] = {}
        self._album_cache: dict[str, int] | None = None

    def close(self) -> None:
        try:
            if self._sid:
                self.logout()
        except Exception:
            pass
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> SynologyPhotosClient:
        self.login()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def _browse_prefix(self) -> str:
        return "SYNO.FotoTeam.Browse" if self.settings.synology_space == "shared" else "SYNO.Foto.Browse"

    def invalidate_album_cache(self) -> None:
        self._album_cache = None

    def _entry_request(
        self,
        api: str,
        method: str,
        version: int = 1,
        params: dict[str, Any] | None = None,
        *,
        use_post: bool = True,
        allow_relogin: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "api": api,
            "method": method,
            "version": version,
        }
        if params:
            payload.update(params)
        if self._sid:
            payload["_sid"] = self._sid

        headers: dict[str, str] = {}
        if self._syno_token:
            headers["X-SYNO-TOKEN"] = self._syno_token

        # Always send authenticated requests via POST form body so `_sid` never leaks in URLs/logs
        send_post = use_post or bool(self._sid)
        if send_post:
            resp = self._client.post("/webapi/entry.cgi", data=payload, headers=headers)
        else:
            resp = self._client.get("/webapi/entry.cgi", params=payload, headers=headers)

        resp.raise_for_status()
        body = resp.json()
        if not body.get("success", False):
            err = body.get("error") or {}
            code = err.get("code")
            # DSM error 119 = invalid/expired SID; re-authenticate once and retry
            if code == 119 and allow_relogin and api != "SYNO.API.Auth":
                logger.info("Synology DSM session expired (code=119); re-authenticating")
                self._sid = None
                self._syno_token = None
                self.invalidate_album_cache()
                self.login()
                return self._entry_request(
                    api,
                    method,
                    version=version,
                    params=params,
                    use_post=use_post,
                    allow_relogin=False,
                )
            raise SynologyAPIError(f"Synology API {api}.{method} failed (code={code})", code=code)
        return body.get("data") or {}

    def login(self) -> None:
        """Authenticate against DSM 7 using SYNO.API.Auth."""
        if not self.settings.synology_username or not self.settings.synology_password:
            raise SynologyAPIError("Synology username and password are required.")

        data = self._entry_request(
            "SYNO.API.Auth",
            "login",
            version=6,
            params={
                "account": self.settings.synology_username,
                "passwd": self.settings.synology_password,
                "session": "SynologyPhotos",
                "format": "sid",
                "enable_syno_token": "yes",
            },
            use_post=True,
            allow_relogin=False,
        )
        self._sid = data.get("sid")
        self._syno_token = data.get("synotoken")
        if not self._sid:
            raise SynologyAPIError("Synology DSM login succeeded without returning a session ID.")

    def logout(self) -> None:
        if not self._sid:
            return
        try:
            self._entry_request(
                "SYNO.API.Auth",
                "logout",
                version=6,
                params={"session": "SynologyPhotos"},
                use_post=True,
                allow_relogin=False,
            )
        finally:
            self._sid = None
            self._syno_token = None

    def test_connection(self) -> dict[str, Any]:
        """Verify login and read basic album count from Synology Photos."""
        self.login()
        try:
            albums = self.list_albums()
            return {
                "ok": True,
                "space": self.settings.synology_space,
                "album_count": len(albums),
            }
        finally:
            self.logout()

    # ------------------------------------------------------------------
    # Albums
    # ------------------------------------------------------------------
    def list_albums(self) -> dict[str, int]:
        """Return mapping of {album_name: synology_album_id}."""
        if self._album_cache is not None:
            return dict(self._album_cache)

        albums: dict[str, int] = {}
        offset = 0
        limit = 500
        while True:
            data = self._entry_request(
                "SYNO.Foto.Browse.Album",
                "list",
                version=2,
                params={"offset": offset, "limit": limit},
            )
            batch = data.get("list") or []
            for item in batch:
                name = str(item.get("name", "")).strip()
                if name and item.get("id") is not None:
                    albums[name] = int(item["id"])
            if len(batch) < limit:
                break
            offset += limit

        self._album_cache = albums
        return dict(albums)

    def get_or_create_album(self, album_name: str) -> int:
        """Find an existing normal album by name or create it."""
        albums = self.list_albums()
        if album_name in albums:
            return albums[album_name]

        data = self._entry_request(
            "SYNO.Foto.Browse.NormalAlbum",
            "create",
            version=1,
            params={"name": album_name, "item": "[]"},
            use_post=True,
        )
        album_obj = data.get("album") or data
        album_id = int(album_obj["id"])
        if self._album_cache is not None:
            self._album_cache[album_name] = album_id
        return album_id

    def add_items_to_album(self, album_id: int, item_ids: list[int]) -> None:
        """Attach indexed Synology Photos item IDs to a normal virtual album."""
        if not item_ids:
            return
        # Batch in chunks of 100 to stay well within DSM request limits
        for start in range(0, len(item_ids), 100):
            chunk = item_ids[start : start + 100]
            try:
                self._entry_request(
                    "SYNO.Foto.Browse.NormalAlbum",
                    "add_item",
                    version=1,
                    params={
                        "id": album_id,
                        "item": json.dumps(chunk),
                    },
                    use_post=True,
                )
            except SynologyAPIError:
                self.invalidate_album_cache()
                raise

    # ------------------------------------------------------------------
    # Folder & Item resolution (maps on-disk rel path -> Synology item_id)
    # ------------------------------------------------------------------
    def _list_subfolders(self, parent_id: int | None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"offset": 0, "limit": 1000}
        if parent_id is not None:
            params["id"] = parent_id
        data = self._entry_request(
            f"{self._browse_prefix}.Folder",
            "list",
            version=1,
            params=params,
        )
        return list(data.get("list") or [])

    def resolve_folder_id(self, rel_folder_posix: str) -> int | None:
        """Walk Synology Photos folder hierarchy to resolve e.g. '2024/07' -> folder_id."""
        normalized = rel_folder_posix.strip("/")
        if normalized in self._folder_cache:
            return self._folder_cache[normalized]

        parts = [p for p in normalized.split("/") if p]
        current_id: int | None = None
        built_path = ""

        for part in parts:
            built_path = f"{built_path}/{part}" if built_path else part
            if built_path in self._folder_cache:
                current_id = self._folder_cache[built_path]
                continue

            subfolders = self._list_subfolders(current_id)
            matched_id: int | None = None
            for folder in subfolders:
                folder_name = str(folder.get("name", "")).strip("/")
                # DSM sometimes returns '/2024/07' or '07'
                leaf = folder_name.rsplit("/", 1)[-1]
                if leaf == part and folder.get("id") is not None:
                    matched_id = int(folder["id"])
                    break
            if matched_id is None:
                return None
            self._folder_cache[built_path] = matched_id
            current_id = matched_id

        return current_id

    def resolve_item_id(self, dest_rel_path: str) -> int | None:
        """Locate an indexed photo's Synology item ID by its relative path (e.g. '2024/07/IMG_1.JPG')."""
        parts = dest_rel_path.strip("/").rsplit("/", 1)
        is_root_item = len(parts) == 1
        if len(parts) == 2:
            folder_path, filename = parts
            folder_id = self.resolve_folder_id(folder_path)
            if folder_id is None:
                return None
        else:
            folder_id = 0
            filename = parts[0]

        offset = 0
        limit = 500
        target_lower = filename.lower()

        while True:
            params: dict[str, Any] = {
                "offset": offset,
                "limit": limit,
                "folder_id": folder_id,
            }

            data = self._entry_request(
                f"{self._browse_prefix}.Item",
                "list",
                version=1,
                params=params,
            )
            items = data.get("list") or []
            for item in items:
                if str(item.get("filename", "")).lower() != target_lower or item.get("id") is None:
                    continue
                if is_root_item and item.get("folder_id") not in (None, 0):
                    continue
                return int(item["id"])
            if len(items) < limit:
                break
            offset += limit

        return None

"""Security and Synology DSM 7 API protocol tests."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import httpx
import pytest

from gphotos_synology.config import Settings
from gphotos_synology.synology.photos_api import SynologyPhotosClient
from gphotos_synology.takeout.extractor import extract_archive


def test_extract_archive_blocks_zipslip_path_traversal(tmp_path: Path) -> None:
    malicious_zip = tmp_path / "evil.zip"
    with zipfile.ZipFile(malicious_zip, "w") as zf:
        zf.writestr("../../outside.txt", "pwned")

    with pytest.raises(ValueError, match="path traversal"):
        extract_archive(malicious_zip, tmp_path / "extract")


def test_synology_photos_client_login_resolve_and_album_add() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            params = dict(httpx.QueryParams(request.content.decode("utf-8")))
        else:
            params = dict(request.url.params)
        api = params["api"]
        method = params["method"]
        calls.append((api, method))

        if api == "SYNO.API.Auth" and method == "login":
            return httpx.Response(200, json={"success": True, "data": {"sid": "SID123", "synotoken": "TOK"}})
        if api == "SYNO.API.Auth" and method == "logout":
            return httpx.Response(200, json={"success": True, "data": {}})
        if api == "SYNO.Foto.Browse.Album" and method == "list":
            return httpx.Response(200, json={"success": True, "data": {"list": []}})
        if api == "SYNO.Foto.Browse.NormalAlbum" and method == "create":
            return httpx.Response(200, json={"success": True, "data": {"album": {"id": 42}}})
        if api == "SYNO.Foto.Browse.Folder" and method == "list":
            parent_id = params.get("id")
            if parent_id is None:
                return httpx.Response(200, json={"success": True, "data": {"list": [{"id": 10, "name": "/2024"}]}})
            if parent_id == "10":
                return httpx.Response(200, json={"success": True, "data": {"list": [{"id": 11, "name": "/2024/07"}]}})
        if api == "SYNO.Foto.Browse.Item" and method == "list":
            return httpx.Response(
                200,
                json={"success": True, "data": {"list": [{"id": 9001, "filename": "IMG_1.JPG"}]}},
            )
        if api == "SYNO.Foto.Browse.NormalAlbum" and method == "add_item":
            assert params["id"] == "42"
            assert json.loads(params["item"]) == [9001]
            return httpx.Response(200, json={"success": True, "data": {}})

        return httpx.Response(404, json={"success": False, "error": {"code": 101}})

    settings = Settings(
        synology_api_enabled=True,
        synology_url="http://synology.local:5000",
        synology_username="photo_bot",
        synology_password="pw",
        synology_space="personal",
    )
    mock_http = httpx.Client(transport=httpx.MockTransport(handler), base_url=settings.synology_url)

    with SynologyPhotosClient(settings, client=mock_http) as syno:
        album_id = syno.get_or_create_album("Holiday")
        assert album_id == 42
        item_id = syno.resolve_item_id("2024/07/IMG_1.JPG")
        assert item_id == 9001
        syno.add_items_to_album(album_id, [item_id])

    mock_http.close()
    assert ("SYNO.API.Auth", "login") in calls
    assert ("SYNO.Foto.Browse.NormalAlbum", "add_item") in calls
    assert ("SYNO.API.Auth", "logout") in calls

"""Tests for GitHub update checker and Watchtower self-update trigger."""

from __future__ import annotations

import httpx

from gphotos_synology.config import Settings
from gphotos_synology.updater import UpdateService


def test_update_checker_detects_new_commit_and_up_to_date() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(
                200,
                json={
                    "sha": "abcdef1234567890abcdef1234567890abcdef12",
                    "commit": {
                        "message": "Add visual quality upgrade\n\nMore details",
                        "committer": {"date": "2026-11-21T21:00:00Z"},
                    },
                },
            )
        if request.url.path.endswith("/releases/latest"):
            return httpx.Response(
                200,
                json={
                    "tag_name": "v1.0.1",
                    "html_url": "https://github.com/0denko/gphotos-to-synology/releases/tag/v1.0.1",
                    "assets": [
                        {
                            "name": "gphotos-migrator-dsm7-arm64.spk",
                            "browser_download_url": "https://github.com/0denko/gphotos-to-synology/releases/download/v1.0.1/gphotos-migrator-dsm7-arm64.spk",
                        },
                        {
                            "name": "gphotos-migrator-dsm7-x86_64.spk",
                            "browser_download_url": "https://github.com/0denko/gphotos-to-synology/releases/download/v1.0.1/gphotos-migrator-dsm7-x86_64.spk",
                        },
                    ],
                },
            )
        return httpx.Response(404)

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    updater = UpdateService(cache_ttl_seconds=0)

    # Older running SHA -> update_available=True
    old_settings = Settings(
        app_build_sha="1111111",
        watchtower_url="http://updater:8080",
        watchtower_token="tok",
    )
    res = updater.get_status(old_settings, force=True, http_client=mock_client)
    assert res["update_available"] is True
    assert res["current_sha"] == "1111111"
    assert res["latest_sha"] == "abcdef1"
    assert res["latest_version"] == "1.0.1"
    assert res["latest_commit_message"] == "Add visual quality upgrade"
    assert res["auto_updater_configured"] is True
    assert res["spk_download_url"].endswith(".spk")
    assert ("arm64" in res["spk_download_url"]) or ("x86_64" in res["spk_download_url"])

    # Matching SHA -> update_available=False
    current_settings = Settings(app_build_sha="abcdef1234567890")
    res2 = updater.get_status(current_settings, force=True, http_client=mock_client)
    assert res2["update_available"] is False
    mock_client.close()


def test_watchtower_self_update_trigger() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization", "")
        return httpx.Response(200, json={"summary": {"updated": 1}})

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    settings = Settings(
        watchtower_url="http://updater:8080",
        watchtower_token="secret-token",
    )
    res = UpdateService().trigger_self_update(settings, http_client=mock_client)
    mock_client.close()

    assert res["ok"] is True
    assert captured["method"] == "POST"
    assert captured["url"] == "http://updater:8080/v1/update?async=1"
    assert captured["auth"] == "Bearer secret-token"

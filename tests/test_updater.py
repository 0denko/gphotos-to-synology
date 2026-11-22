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
                    "tag_name": "v1.3.0",
                    "html_url": "https://github.com/0denko/gphotos-to-synology/releases/tag/v1.3.0",
                    "assets": [
                        {
                            "name": "gphotos-migrator-dsm7-arm64.spk",
                            "browser_download_url": "https://github.com/0denko/gphotos-to-synology/releases/download/v1.3.0/gphotos-migrator-dsm7-arm64.spk",
                        },
                        {
                            "name": "gphotos-migrator-dsm7-x86_64.spk",
                            "browser_download_url": "https://github.com/0denko/gphotos-to-synology/releases/download/v1.3.0/gphotos-migrator-dsm7-x86_64.spk",
                        },
                    ],
                },
            )
        return httpx.Response(404)

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    updater = UpdateService(cache_ttl_seconds=0)

    # Strictly newer release v1.3.0 > current 1.2.0 -> update_available=True
    old_settings = Settings(
        app_build_sha="1111111",
        watchtower_url="http://updater:8080",
        watchtower_token="tok",
    )
    res = updater.get_status(old_settings, force=True, http_client=mock_client)
    assert res["update_available"] is True
    assert res["current_sha"] == "1111111"
    assert res["latest_sha"] == "abcdef1"
    assert res["latest_version"] == "1.3.0"
    assert res["latest_commit_message"] == "Add visual quality upgrade"
    assert res["auto_updater_configured"] is True
    assert res["spk_download_url"].endswith(".spk")
    assert ("arm64" in res["spk_download_url"]) or ("x86_64" in res["spk_download_url"])
    mock_client.close()

    # Older release v1.0.1 < current 1.2.0 -> update_available=False (never downgrades)
    def older_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": "abcdef1234567890", "commit": {"message": "old", "committer": {"date": "2026-11-21T21:00:00Z"}}})
        if request.url.path.endswith("/releases/latest"):
            return httpx.Response(200, json={"tag_name": "v1.0.1", "html_url": "https://example.com", "assets": []})
        return httpx.Response(404)

    older_client = httpx.Client(transport=httpx.MockTransport(older_handler))
    res2 = updater.get_status(old_settings, force=True, http_client=older_client)
    assert res2["update_available"] is False
    older_client.close()


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


def test_update_checker_uses_release_version_not_git_commits() -> None:
    """A newer docs/Collab.md commit on main must NOT trigger update_available when release == installed,
    and /commits/main errors must not break release checks."""
    from gphotos_synology import __version__

    def same_release_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/releases/latest"):
            return httpx.Response(
                200,
                json={
                    "tag_name": f"v{__version__}",
                    "html_url": f"https://github.com/0denko/gphotos-to-synology/releases/tag/v{__version__}",
                    "assets": [],
                },
            )
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(
                200,
                json={
                    "sha": "9999999999999999999999999999999999999999",
                    "commit": {
                        "message": "Collab.md: docs-only commit [skip ci]",
                        "committer": {"date": "2026-11-22T14:00:00Z"},
                    },
                },
            )
        return httpx.Response(404)

    settings = Settings(app_build_sha="1111111")
    with httpx.Client(transport=httpx.MockTransport(same_release_handler)) as client:
        res = UpdateService(cache_ttl_seconds=0).get_status(settings, force=True, http_client=client)
    assert res["update_available"] is False
    assert res["latest_version"] == __version__
    assert res["error"] == ""

    # Even if /commits/main returns 500, a genuinely newer release still reports update_available=True
    def commits_down_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/releases/latest"):
            return httpx.Response(
                200,
                json={
                    "tag_name": "v9.9.9-0001",
                    "html_url": "https://github.com/0denko/gphotos-to-synology/releases/tag/v9.9.9-0001",
                    "assets": [],
                },
            )
        return httpx.Response(500)

    with httpx.Client(transport=httpx.MockTransport(commits_down_handler)) as client:
        res2 = UpdateService(cache_ttl_seconds=0).get_status(settings, force=True, http_client=client)
    assert res2["update_available"] is True
    assert res2["latest_version"] == "9.9.9-0001"
    assert res2["error"] == ""


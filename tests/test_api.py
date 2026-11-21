"""FastAPI dashboard and control endpoint tests."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from gphotos_synology.config import Settings
from gphotos_synology.main import create_app


def _make_client(tmp_path: Path) -> TestClient:
    settings = Settings(
        data_dir=tmp_path / "config",
        work_dir=tmp_path / "work",
        inbox_dir=tmp_path / "inbox",
        photos_dir=tmp_path / "photos",
        min_free_disk_gb=0.1,
    )
    app = create_app(base_settings=settings, start_worker=False)
    return TestClient(app)


def test_dashboard_health_status_and_settings_roundtrip(tmp_path: Path) -> None:
    with _make_client(tmp_path) as client:
        # Dashboard HTML served at root
        resp = client.get("/")
        assert resp.status_code == 200
        assert "Google Photos" in resp.text

        # Health & status
        health = client.get("/api/health").json()
        assert health["status"] == "ok"

        status = client.get("/api/status").json()
        assert status["stats"]["total_media"] == 0
        assert status["worker_paused"] is False

        # Pause & resume controls
        assert client.post("/api/actions/pause").json()["worker_paused"] is True
        assert client.get("/api/status").json()["worker_paused"] is True
        assert client.post("/api/actions/resume").json()["worker_paused"] is False

        # Settings update persists & masks secrets
        put_resp = client.put(
            "/api/settings",
            json={
                "updates": {
                    "poll_interval_minutes": 30,
                    "synology_api_enabled": True,
                    "synology_password": "super-secret-password",
                }
            },
        )
        assert put_resp.status_code == 200
        saved = put_resp.json()["settings"]
        assert saved["poll_interval_minutes"] == 30
        assert saved["synology_api_enabled"] is True
        assert saved["synology_password"] == "********"

        # Sending masked sentinel preserves real secret
        put_resp2 = client.put(
            "/api/settings",
            json={"updates": {"synology_password": "********", "folder_template": "year_ym"}},
        )
        assert put_resp2.status_code == 200
        assert client.app.state.ctx.config_manager.settings.synology_password == "super-secret-password"
        assert client.app.state.ctx.config_manager.settings.folder_template == "year_ym"

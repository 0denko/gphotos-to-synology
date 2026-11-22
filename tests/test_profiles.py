"""Tests for multi-user Synology DSM profile discovery, isolation, and user picker API."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from gphotos_synology.config import Settings
from gphotos_synology.main import create_app


def test_multi_user_discovery_profile_isolation_and_dsm_auto_select(tmp_path: Path) -> None:
    homes_dir = tmp_path / "homes"
    (homes_dir / "alex" / "Photos").mkdir(parents=True)
    (homes_dir / "maria" / "Photos").mkdir(parents=True)
    # System/ignored directories under /volume1/homes must not be discovered
    (homes_dir / "@eaDir").mkdir(parents=True)
    (homes_dir / "#recycle").mkdir(parents=True)
    (homes_dir / "guest").mkdir(parents=True)

    settings = Settings(
        data_dir=tmp_path / "config",
        work_dir=tmp_path / "work",
        inbox_dir=tmp_path / "inbox",
        photos_dir=tmp_path / "photos",
        homes_dir=homes_dir,
        inbox_stability_seconds=0,
        min_free_disk_gb=0.1,
        write_exif_metadata=False,
        proxy_secret="test-proxy-secret",
    )
    app = create_app(base_settings=settings, start_worker=False)

    admin_headers = {
        "X-Proxy-Secret": "test-proxy-secret",
        "X-DSM-User": "admin",
        "X-DSM-Admin": "1",
        "X-Requested-With": "XMLHttpRequest",
    }
    with TestClient(app, headers=admin_headers) as client:
        # 1. Configure shared Google OAuth Client ID on default profile
        client.put(
            "/api/settings",
            json={"updates": {"google_client_id": "shared-client-id.apps.googleusercontent.com"}},
        )

        # 2. Discover Synology users from /data/homes
        prof_resp = client.get("/api/profiles").json()
        assert prof_resp["homes_mounted"] is True
        discovered_names = [u["username"] for u in prof_resp["discovered_synology_users"]]
        assert discovered_names == ["alex", "maria"]

        # 3. Add Alex and Maria Personal Space profiles
        alex_res = client.post(
            "/api/profiles",
            json={"dsm_username": "alex", "target_space": "personal"},
        )
        assert alex_res.status_code == 200
        alex_profile = alex_res.json()["profile"]
        assert alex_profile["id"] == "alex"
        assert alex_profile["photos_dir"] == str(homes_dir / "alex" / "Photos")

        maria_res = client.post(
            "/api/profiles",
            json={"dsm_username": "maria", "target_space": "personal"},
        )
        assert maria_res.status_code == 200

        # Maria inherits shared Google OAuth Client ID automatically
        maria_settings = client.get("/api/settings", headers={"X-Profile-Id": "maria"}).json()["settings"]
        assert maria_settings["google_client_id"] == "shared-client-id.apps.googleusercontent.com"

        # 4. Logged-in DSM user header auto-recommends Maria's profile
        dsm_lookup = client.get("/api/profiles", headers={"X-DSM-User": "maria", "X-DSM-Admin": "0"}).json()
        assert dsm_lookup["current_dsm_user"] == "maria"
        assert dsm_lookup["recommended_profile_id"] == "maria"

        # 5. Drop a Takeout ZIP into Alex's isolated inbox and run Alex's pipeline
        alex_ctx = app.state.ctx.registry.get_context("alex")
        alex_zip = alex_ctx.config_manager.settings.inbox_dir / "takeout-20261121T090000Z-001.zip"
        with zipfile.ZipFile(alex_zip, "w") as zf:
            zf.writestr("Takeout/Google Photos/Photos from 2024/ALEX_1.jpg", b"\xff\xd8\xff\xe0ALEX_IMG")
            zf.writestr(
                "Takeout/Google Photos/Photos from 2024/ALEX_1.jpg.json",
                json.dumps({"title": "ALEX_1.jpg", "photoTakenTime": {"timestamp": "1720000000"}}),
            )
        alex_ctx.pipeline.run_cycle()

        # Photo landed in Alex's Personal Space ONLY; Maria's stats stay at 0
        assert (homes_dir / "alex" / "Photos" / "2024" / "07" / "ALEX_1.jpg").exists()
        assert client.get("/api/status", headers={"X-Profile-Id": "alex"}).json()["stats"]["total_media"] == 1
        assert client.get("/api/status", headers={"X-Profile-Id": "maria"}).json()["stats"]["total_media"] == 0

        # 6. Disable & delete Maria profile; cannot delete default
        patch_res = client.patch("/api/profiles/maria", json={"enabled": False})
        assert patch_res.status_code == 200 and patch_res.json()["profile"]["enabled"] is False
        assert client.delete("/api/profiles/maria").status_code == 200
        assert client.delete("/api/profiles/default").status_code == 400

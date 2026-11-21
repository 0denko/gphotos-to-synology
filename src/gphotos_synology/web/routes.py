"""FastAPI REST endpoints and Google OAuth2 callback handlers."""

from __future__ import annotations

import io
import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from PIL import Image, ImageOps
from pydantic import BaseModel

from gphotos_synology.database import utc_now_iso
from gphotos_synology.notifications import Notifier
from gphotos_synology.sources.gdrive import GoogleDriveClient
from gphotos_synology.synology.photos_api import SynologyPhotosClient
from gphotos_synology.takeout.extractor import get_free_disk_gb
from gphotos_synology.takeout.metadata import exiftool_available


class SettingsUpdateRequest(BaseModel):
    updates: dict[str, Any]


def create_api_router() -> APIRouter:
    router = APIRouter(prefix="/api", tags=["migrator"])

    def _ctx(request: Request):
        return request.app.state.ctx

    @router.get("/health")
    def health(request: Request) -> dict[str, Any]:
        ctx = _ctx(request)
        settings = ctx.config_manager.settings
        return {
            "status": "ok",
            "worker_running": ctx.scheduler.is_running,
            "worker_paused": ctx.scheduler.is_paused,
            "exiftool_available": exiftool_available(settings.exiftool_path),
            "work_free_gb": round(get_free_disk_gb(settings.work_dir), 2),
            "photos_free_gb": round(get_free_disk_gb(settings.photos_dir), 2),
        }

    @router.get("/status")
    def status(request: Request) -> dict[str, Any]:
        ctx = _ctx(request)
        settings = ctx.config_manager.settings
        gdrive = GoogleDriveClient(settings)
        return {
            "progress": ctx.pipeline.get_progress(),
            "stats": ctx.db.get_stats(),
            "worker_running": ctx.scheduler.is_running,
            "worker_paused": ctx.scheduler.is_paused,
            "gdrive_oauth_configured": gdrive.is_oauth_configured(),
            "gdrive_connected": gdrive.is_connected(),
            "exiftool_available": exiftool_available(settings.exiftool_path),
            "synology_api_enabled": settings.synology_api_enabled,
            "disk": {
                "work_free_gb": round(get_free_disk_gb(settings.work_dir), 2),
                "photos_free_gb": round(get_free_disk_gb(settings.photos_dir), 2),
                "min_free_disk_gb": settings.min_free_disk_gb,
            },
        }

    @router.get("/archives")
    def list_archives(request: Request, limit: int = 50) -> dict[str, Any]:
        ctx = _ctx(request)
        return {"archives": ctx.db.list_archives(limit=min(limit, 200))}

    @router.get("/albums")
    def list_albums(request: Request) -> dict[str, Any]:
        ctx = _ctx(request)
        return {"albums": ctx.db.list_albums_summary()}

    @router.get("/logs")
    def list_logs(request: Request, limit: int = 120) -> dict[str, Any]:
        ctx = _ctx(request)
        return {"logs": ctx.db.get_recent_logs(limit=min(limit, 500))}

    @router.get("/upgrades")
    def list_upgrades(request: Request, limit: int = 100) -> dict[str, Any]:
        ctx = _ctx(request)
        return {"upgrades": ctx.db.list_quality_upgrades(limit=min(limit, 500))}

    @router.get("/upgrades/{upgrade_id}/preview/{which}")
    def preview_upgrade_image(request: Request, upgrade_id: int, which: str) -> Response:
        ctx = _ctx(request)
        settings = ctx.config_manager.settings
        row = ctx.db.get_quality_upgrade(upgrade_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Upgrade record not found")

        if which == "kept":
            base_dir = settings.photos_dir.resolve()
            img_path = (base_dir / str(row["kept_rel_path"])).resolve()
        elif which == "quarantined":
            base_dir = settings.quarantine_dir.resolve()
            img_path = (base_dir / str(row["quarantine_filename"])).resolve()
        else:
            raise HTTPException(status_code=400, detail="Preview target must be 'kept' or 'quarantined'")

        if base_dir not in img_path.parents or not img_path.is_file():
            raise HTTPException(status_code=404, detail="Preview image not available on disk")

        try:
            with Image.open(img_path) as raw:
                img = ImageOps.exif_transpose(raw).convert("RGB")
                img.thumbnail((640, 640), Image.Resampling.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=85)
                return Response(content=buf.getvalue(), media_type="image/jpeg")
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Could not render preview: {exc}") from exc

    @router.post("/upgrades/{upgrade_id}/restore")
    def restore_upgrade(request: Request, upgrade_id: int) -> dict[str, Any]:
        ctx = _ctx(request)
        try:
            return ctx.pipeline.restore_quality_upgrade(upgrade_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/upgrades/{upgrade_id}/purge")
    def purge_upgrade(request: Request, upgrade_id: int) -> dict[str, Any]:
        ctx = _ctx(request)
        try:
            return ctx.pipeline.purge_quality_upgrade(upgrade_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/upgrades/purge-all")
    def purge_all_upgrades(request: Request) -> dict[str, Any]:
        ctx = _ctx(request)
        purged = ctx.pipeline.purge_all_quarantined()
        return {"ok": True, "purged": purged}

    @router.get("/settings")
    def get_settings(request: Request) -> dict[str, Any]:
        ctx = _ctx(request)
        return {"settings": ctx.config_manager.to_public_dict()}

    @router.put("/settings")
    def update_settings(request: Request, body: SettingsUpdateRequest) -> dict[str, Any]:
        ctx = _ctx(request)
        try:
            ctx.config_manager.update(body.updates)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        ctx.db.log_event("INFO", "config", "Updated runtime configuration from dashboard")
        return {"ok": True, "settings": ctx.config_manager.to_public_dict()}

    # ------------------------------------------------------------------
    # Worker & Maintenance Actions
    # ------------------------------------------------------------------
    @router.post("/actions/scan")
    def trigger_scan(request: Request) -> dict[str, Any]:
        ctx = _ctx(request)
        if ctx.scheduler.is_paused:
            raise HTTPException(status_code=409, detail="Worker is paused. Resume before scanning.")
        ctx.scheduler.trigger_now(albums_only=False)
        ctx.db.log_event("INFO", "dashboard", "Manual scan & sync triggered")
        return {"ok": True, "message": "Background scan queued"}

    @router.post("/actions/index-existing")
    def trigger_index_existing(request: Request) -> dict[str, Any]:
        ctx = _ctx(request)
        if ctx.scheduler.is_paused:
            raise HTTPException(status_code=409, detail="Worker is paused. Resume before indexing.")
        ctx.scheduler.trigger_now(index_existing_only=True)
        ctx.db.log_event("INFO", "dashboard", "Manual existing NAS library index triggered")
        return {"ok": True, "message": "Existing NAS photo indexing queued"}

    @router.post("/actions/sync-albums")
    def trigger_album_sync(request: Request) -> dict[str, Any]:
        ctx = _ctx(request)
        if not ctx.config_manager.settings.synology_api_enabled:
            raise HTTPException(
                status_code=400,
                detail="Enable Synology DSM 7 API in settings before syncing virtual albums.",
            )
        ctx.scheduler.trigger_now(albums_only=True)
        ctx.db.log_event("INFO", "dashboard", "Manual Synology album sync triggered")
        return {"ok": True, "message": "Album sync queued"}

    @router.post("/actions/pause")
    def pause_worker(request: Request) -> dict[str, Any]:
        _ctx(request).scheduler.pause()
        return {"ok": True, "worker_paused": True}

    @router.post("/actions/resume")
    def resume_worker(request: Request) -> dict[str, Any]:
        _ctx(request).scheduler.resume()
        return {"ok": True, "worker_paused": False}

    @router.post("/actions/reset-renewal")
    def reset_renewal_timer(request: Request) -> dict[str, Any]:
        ctx = _ctx(request)
        ctx.db.set_state("takeout_schedule_started_at", utc_now_iso())
        ctx.pipeline._set_progress(renewal_reminder_due=False)
        ctx.db.log_event("INFO", "schedule", "Annual Google Takeout renewal timer reset")
        return {"ok": True}

    @router.post("/actions/test-synology")
    def test_synology(request: Request) -> dict[str, Any]:
        ctx = _ctx(request)
        settings = ctx.config_manager.settings
        try:
            client = SynologyPhotosClient(settings)
            result = client.test_connection()
            client.close()
            ctx.db.log_event(
                "INFO",
                "synology-api",
                f"Synology Photos connection verified ({result['album_count']} existing albums)",
            )
            return result
        except Exception as exc:
            ctx.db.log_event("ERROR", "synology-api", f"Synology connection test failed: {exc}")
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/actions/test-notification")
    def test_notification(request: Request) -> dict[str, Any]:
        ctx = _ctx(request)
        ok = Notifier(ctx.config_manager.settings).send(
            "Test Alert",
            "Google Photos -> Synology DSM background migrator notification test succeeded.",
            level="info",
        )
        if not ok:
            raise HTTPException(
                status_code=400,
                detail="No notification channel configured or delivery failed. Check webhook/SMTP settings.",
            )
        return {"ok": True}

    # ------------------------------------------------------------------
    # Google Drive OAuth2 Flow
    # ------------------------------------------------------------------
    def _resolve_redirect_uri(request: Request) -> str:
        settings = _ctx(request).config_manager.settings
        if settings.google_redirect_uri.strip():
            return settings.google_redirect_uri.strip()
        return str(request.base_url).rstrip("/") + "/api/oauth/google/callback"

    @router.get("/oauth/google/start")
    def google_oauth_start(request: Request) -> dict[str, Any]:
        ctx = _ctx(request)
        gdrive = GoogleDriveClient(ctx.config_manager.settings)
        redirect_uri = _resolve_redirect_uri(request)
        state = secrets.token_urlsafe(24)
        ctx.db.set_state("oauth_state", state)
        try:
            auth_url, _ = gdrive.build_authorization_url(redirect_uri=redirect_uri, state=state)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"authorization_url": auth_url, "redirect_uri": redirect_uri}

    @router.get("/oauth/google/callback")
    def google_oauth_callback(
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ) -> RedirectResponse:
        ctx = _ctx(request)
        if error:
            ctx.db.log_event("ERROR", "oauth", f"Google OAuth returned error: {error}")
            return RedirectResponse(url=f"/?oauth=error&detail={error}", status_code=302)
        if not code:
            return RedirectResponse(url="/?oauth=error&detail=missing_code", status_code=302)

        expected_state = ctx.db.get_state("oauth_state")
        if expected_state and state != expected_state:
            ctx.db.log_event("ERROR", "oauth", "Google OAuth state mismatch")
            return RedirectResponse(url="/?oauth=error&detail=state_mismatch", status_code=302)

        gdrive = GoogleDriveClient(ctx.config_manager.settings)
        redirect_uri = _resolve_redirect_uri(request)
        try:
            gdrive.exchange_code(code=code, redirect_uri=redirect_uri)
            ctx.db.log_event("INFO", "oauth", "Google Drive connected successfully")
            ctx.scheduler.trigger_now(albums_only=False)
            return RedirectResponse(url="/?oauth=success", status_code=302)
        except Exception as exc:
            ctx.db.log_event("ERROR", "oauth", f"OAuth token exchange failed: {exc}")
            return RedirectResponse(url="/?oauth=error&detail=token_exchange_failed", status_code=302)

    @router.post("/oauth/google/disconnect")
    def google_oauth_disconnect(request: Request) -> dict[str, Any]:
        ctx = _ctx(request)
        GoogleDriveClient(ctx.config_manager.settings).disconnect()
        ctx.db.log_event("INFO", "oauth", "Disconnected Google Drive OAuth token")
        return {"ok": True}

    return router

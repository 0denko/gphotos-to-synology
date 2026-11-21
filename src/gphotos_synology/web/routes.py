"""FastAPI REST endpoints, multi-user profile management, and Google OAuth2 handlers."""

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
from gphotos_synology.profiles import CreateProfileRequest, ProfileContext, UpdateProfileRequest
from gphotos_synology.sources.gdrive import GoogleDriveClient
from gphotos_synology.synology.photos_api import SynologyPhotosClient
from gphotos_synology.takeout.extractor import get_free_disk_gb
from gphotos_synology.takeout.metadata import exiftool_available


class SettingsUpdateRequest(BaseModel):
    updates: dict[str, Any]


def create_api_router() -> APIRouter:
    router = APIRouter(prefix="/api", tags=["migrator"])

    def _app_ctx(request: Request):
        return request.app.state.ctx

    def _profile_ctx(request: Request, profile: str | None = None) -> ProfileContext:
        pid = profile or request.query_params.get("profile") or request.headers.get("x-profile-id") or "default"
        try:
            return _app_ctx(request).registry.get_context(pid)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    # ------------------------------------------------------------------
    # Multi-User Synology Profiles & Auto-Discovery
    # ------------------------------------------------------------------
    @router.get("/profiles")
    def list_profiles(request: Request) -> dict[str, Any]:
        app_ctx = _app_ctx(request)
        dsm_user = request.headers.get("x-dsm-user", "").strip()
        discovery = app_ctx.registry.discover_synology_users()
        recommended_id = app_ctx.registry.resolve_profile_for_dsm_user(dsm_user)
        return {
            "profiles": app_ctx.registry.list_profiles(),
            "current_dsm_user": dsm_user,
            "recommended_profile_id": recommended_id,
            "homes_mounted": discovery["homes_mounted"],
            "discovered_synology_users": discovery["users"],
        }

    @router.post("/profiles")
    def create_profile(request: Request, body: CreateProfileRequest) -> dict[str, Any]:
        app_ctx = _app_ctx(request)
        try:
            pctx = app_ctx.registry.create_profile(body)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        pctx.db.log_event(
            "INFO",
            "profiles",
            f"Created profile '{pctx.profile.display_name}' targeting {pctx.profile.photos_dir}",
        )
        return {"ok": True, "profile": pctx.profile.model_dump(mode="json")}

    @router.patch("/profiles/{profile_id}")
    def update_profile(request: Request, profile_id: str, body: UpdateProfileRequest) -> dict[str, Any]:
        app_ctx = _app_ctx(request)
        try:
            updated = app_ctx.registry.update_profile(profile_id, body)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "profile": updated}

    @router.delete("/profiles/{profile_id}")
    def delete_profile(request: Request, profile_id: str) -> dict[str, Any]:
        app_ctx = _app_ctx(request)
        try:
            app_ctx.registry.delete_profile(profile_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}

    # ------------------------------------------------------------------
    # Health & Per-Profile Status
    # ------------------------------------------------------------------
    @router.get("/health")
    def health(request: Request) -> dict[str, Any]:
        app_ctx = _app_ctx(request)
        settings = app_ctx.config_manager.settings
        return {
            "status": "ok",
            "build_sha": settings.app_build_sha[:7] if settings.app_build_sha else "dev",
            "worker_running": app_ctx.scheduler.is_running,
            "worker_paused": app_ctx.scheduler.is_paused,
            "exiftool_available": exiftool_available(settings.exiftool_path),
            "work_free_gb": round(get_free_disk_gb(settings.work_dir), 2),
            "photos_free_gb": round(get_free_disk_gb(settings.photos_dir), 2),
        }

    @router.get("/status")
    def status(request: Request, profile: str | None = None) -> dict[str, Any]:
        app_ctx = _app_ctx(request)
        pctx = _profile_ctx(request, profile)
        settings = pctx.config_manager.settings
        gdrive = GoogleDriveClient(settings)
        return {
            "profile": pctx.profile.model_dump(mode="json"),
            "progress": pctx.pipeline.get_progress(),
            "stats": pctx.db.get_stats(),
            "worker_running": app_ctx.scheduler.is_running,
            "worker_paused": app_ctx.scheduler.is_paused,
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
    def list_archives(request: Request, limit: int = 50, profile: str | None = None) -> dict[str, Any]:
        pctx = _profile_ctx(request, profile)
        return {"archives": pctx.db.list_archives(limit=min(limit, 200))}

    @router.get("/albums")
    def list_albums(request: Request, profile: str | None = None) -> dict[str, Any]:
        pctx = _profile_ctx(request, profile)
        return {"albums": pctx.db.list_albums_summary()}

    @router.get("/logs")
    def list_logs(request: Request, limit: int = 120, profile: str | None = None) -> dict[str, Any]:
        pctx = _profile_ctx(request, profile)
        return {"logs": pctx.db.get_recent_logs(limit=min(limit, 500))}

    # ------------------------------------------------------------------
    # Visual Quality Upgrades & Quarantine
    # ------------------------------------------------------------------
    @router.get("/upgrades")
    def list_upgrades(request: Request, limit: int = 100, profile: str | None = None) -> dict[str, Any]:
        pctx = _profile_ctx(request, profile)
        return {"upgrades": pctx.db.list_quality_upgrades(limit=min(limit, 500))}

    @router.get("/upgrades/{upgrade_id}/preview/{which}")
    def preview_upgrade_image(
        request: Request,
        upgrade_id: int,
        which: str,
        profile: str | None = None,
    ) -> Response:
        pctx = _profile_ctx(request, profile)
        settings = pctx.config_manager.settings
        row = pctx.db.get_quality_upgrade(upgrade_id)
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
    def restore_upgrade(request: Request, upgrade_id: int, profile: str | None = None) -> dict[str, Any]:
        pctx = _profile_ctx(request, profile)
        try:
            return pctx.pipeline.restore_quality_upgrade(upgrade_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/upgrades/{upgrade_id}/purge")
    def purge_upgrade(request: Request, upgrade_id: int, profile: str | None = None) -> dict[str, Any]:
        pctx = _profile_ctx(request, profile)
        try:
            return pctx.pipeline.purge_quality_upgrade(upgrade_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/upgrades/purge-all")
    def purge_all_upgrades(request: Request, profile: str | None = None) -> dict[str, Any]:
        pctx = _profile_ctx(request, profile)
        purged = pctx.pipeline.purge_all_quarantined()
        return {"ok": True, "purged": purged}

    # ------------------------------------------------------------------
    # Software Updates (GitHub check + Watchtower self-update)
    # ------------------------------------------------------------------
    @router.get("/updates")
    def check_updates(request: Request, force: bool = False) -> dict[str, Any]:
        app_ctx = _app_ctx(request)
        return app_ctx.updater.get_status(app_ctx.config_manager.settings, force=force)

    @router.post("/updates/apply")
    def apply_update(request: Request) -> dict[str, Any]:
        app_ctx = _app_ctx(request)
        try:
            res = app_ctx.updater.trigger_self_update(app_ctx.config_manager.settings)
            app_ctx.db.log_event("INFO", "updater", "Self-update triggered via Watchtower")
            return res
        except Exception as exc:
            app_ctx.db.log_event("WARNING", "updater", f"Self-update trigger failed: {exc}")
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ------------------------------------------------------------------
    # Per-Profile Settings
    # ------------------------------------------------------------------
    @router.get("/settings")
    def get_settings(request: Request, profile: str | None = None) -> dict[str, Any]:
        pctx = _profile_ctx(request, profile)
        return {"settings": pctx.config_manager.to_public_dict()}

    @router.put("/settings")
    def update_settings(
        request: Request,
        body: SettingsUpdateRequest,
        profile: str | None = None,
    ) -> dict[str, Any]:
        pctx = _profile_ctx(request, profile)
        try:
            pctx.config_manager.update(body.updates)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        pctx.db.log_event("INFO", "config", "Updated runtime configuration from dashboard")
        return {"ok": True, "settings": pctx.config_manager.to_public_dict()}

    # ------------------------------------------------------------------
    # Worker & Maintenance Actions
    # ------------------------------------------------------------------
    @router.post("/actions/scan")
    def trigger_scan(request: Request, profile: str | None = None) -> dict[str, Any]:
        app_ctx = _app_ctx(request)
        pctx = _profile_ctx(request, profile)
        if app_ctx.scheduler.is_paused:
            raise HTTPException(status_code=409, detail="Worker is paused. Resume before scanning.")
        app_ctx.scheduler.trigger_now(profile_id=pctx.profile.id, albums_only=False)
        pctx.db.log_event("INFO", "dashboard", f"Manual scan & sync triggered for '{pctx.profile.display_name}'")
        return {"ok": True, "message": "Background scan queued"}

    @router.post("/actions/index-existing")
    def trigger_index_existing(request: Request, profile: str | None = None) -> dict[str, Any]:
        app_ctx = _app_ctx(request)
        pctx = _profile_ctx(request, profile)
        if app_ctx.scheduler.is_paused:
            raise HTTPException(status_code=409, detail="Worker is paused. Resume before indexing.")
        app_ctx.scheduler.trigger_now(profile_id=pctx.profile.id, index_existing_only=True)
        pctx.db.log_event(
            "INFO",
            "dashboard",
            f"Manual existing NAS library index triggered for '{pctx.profile.display_name}'",
        )
        return {"ok": True, "message": "Existing NAS photo indexing queued"}

    @router.post("/actions/sync-albums")
    def trigger_album_sync(request: Request, profile: str | None = None) -> dict[str, Any]:
        app_ctx = _app_ctx(request)
        pctx = _profile_ctx(request, profile)
        if not pctx.config_manager.settings.synology_api_enabled:
            raise HTTPException(
                status_code=400,
                detail="Enable Synology DSM 7 API in settings before syncing virtual albums.",
            )
        app_ctx.scheduler.trigger_now(profile_id=pctx.profile.id, albums_only=True)
        pctx.db.log_event("INFO", "dashboard", "Manual Synology album sync triggered")
        return {"ok": True, "message": "Album sync queued"}

    @router.post("/actions/pause")
    def pause_worker(request: Request) -> dict[str, Any]:
        _app_ctx(request).scheduler.pause()
        return {"ok": True, "worker_paused": True}

    @router.post("/actions/resume")
    def resume_worker(request: Request) -> dict[str, Any]:
        _app_ctx(request).scheduler.resume()
        return {"ok": True, "worker_paused": False}

    @router.post("/actions/reset-renewal")
    def reset_renewal_timer(request: Request, profile: str | None = None) -> dict[str, Any]:
        pctx = _profile_ctx(request, profile)
        pctx.db.set_state("takeout_schedule_started_at", utc_now_iso())
        pctx.pipeline._set_progress(renewal_reminder_due=False)
        pctx.db.log_event("INFO", "schedule", "Annual Google Takeout renewal timer reset")
        return {"ok": True}

    @router.post("/actions/test-synology")
    def test_synology(request: Request, profile: str | None = None) -> dict[str, Any]:
        pctx = _profile_ctx(request, profile)
        settings = pctx.config_manager.settings
        try:
            client = SynologyPhotosClient(settings)
            result = client.test_connection()
            client.close()
            pctx.db.log_event(
                "INFO",
                "synology-api",
                f"Synology Photos connection verified ({result['album_count']} existing albums)",
            )
            return result
        except Exception as exc:
            pctx.db.log_event("ERROR", "synology-api", f"Synology connection test failed: {exc}")
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/actions/test-notification")
    def test_notification(request: Request, profile: str | None = None) -> dict[str, Any]:
        pctx = _profile_ctx(request, profile)
        ok = Notifier(pctx.config_manager.settings).send(
            "Test Alert",
            f"Google Photos -> Synology DSM notification test succeeded ({pctx.profile.display_name}).",
            level="info",
        )
        if not ok:
            raise HTTPException(
                status_code=400,
                detail="No notification channel configured or delivery failed. Check webhook/SMTP settings.",
            )
        return {"ok": True}

    # ------------------------------------------------------------------
    # Per-Profile Google Drive OAuth2 Flow
    # ------------------------------------------------------------------
    def _public_base_url(request: Request) -> str:
        proto = request.headers.get("x-forwarded-proto")
        host = request.headers.get("x-forwarded-host")
        prefix = request.headers.get("x-forwarded-prefix", "").rstrip("/")
        if proto and host:
            return f"{proto}://{host}{prefix}"
        return str(request.base_url).rstrip("/") + prefix

    def _resolve_redirect_uri(request: Request, pctx: ProfileContext) -> str:
        settings = pctx.config_manager.settings
        if settings.google_redirect_uri.strip():
            return settings.google_redirect_uri.strip()
        return f"{_public_base_url(request)}/api/oauth/google/callback"

    @router.get("/oauth/google/start")
    def google_oauth_start(request: Request, profile: str | None = None) -> dict[str, Any]:
        pctx = _profile_ctx(request, profile)
        gdrive = GoogleDriveClient(pctx.config_manager.settings)
        redirect_uri = _resolve_redirect_uri(request, pctx)
        # Encode profile_id in OAuth state so callback routes token to the exact user profile
        state = f"{pctx.profile.id}:{secrets.token_urlsafe(24)}"
        pctx.db.set_state("oauth_state", state)
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
        app_ctx = _app_ctx(request)
        base_prefix = request.headers.get("x-forwarded-prefix", "").rstrip("/")
        home_url = f"{base_prefix}/"

        profile_id = "default"
        if state and ":" in state:
            profile_id = state.split(":", 1)[0] or "default"
        try:
            pctx = app_ctx.registry.get_context(profile_id)
        except KeyError:
            pctx = app_ctx.registry.get_context("default")
            profile_id = "default"

        if error:
            pctx.db.log_event("ERROR", "oauth", f"Google OAuth returned error: {error}")
            return RedirectResponse(url=f"{home_url}?profile={profile_id}&oauth=error&detail={error}", status_code=302)
        if not code:
            return RedirectResponse(url=f"{home_url}?profile={profile_id}&oauth=error&detail=missing_code", status_code=302)

        expected_state = pctx.db.get_state("oauth_state")
        if expected_state and state != expected_state:
            pctx.db.log_event("ERROR", "oauth", "Google OAuth state mismatch")
            return RedirectResponse(url=f"{home_url}?profile={profile_id}&oauth=error&detail=state_mismatch", status_code=302)

        gdrive = GoogleDriveClient(pctx.config_manager.settings)
        redirect_uri = _resolve_redirect_uri(request, pctx)
        try:
            gdrive.exchange_code(code=code, redirect_uri=redirect_uri)
            pctx.db.log_event("INFO", "oauth", f"Google Drive connected for '{pctx.profile.display_name}'")
            app_ctx.scheduler.trigger_now(profile_id=pctx.profile.id, albums_only=False)
            return RedirectResponse(url=f"{home_url}?profile={profile_id}&oauth=success", status_code=302)
        except Exception as exc:
            pctx.db.log_event("ERROR", "oauth", f"OAuth token exchange failed: {exc}")
            return RedirectResponse(
                url=f"{home_url}?profile={profile_id}&oauth=error&detail=token_exchange_failed",
                status_code=302,
            )

    @router.post("/oauth/google/disconnect")
    def google_oauth_disconnect(request: Request, profile: str | None = None) -> dict[str, Any]:
        pctx = _profile_ctx(request, profile)
        GoogleDriveClient(pctx.config_manager.settings).disconnect()
        pctx.db.log_event("INFO", "oauth", f"Disconnected Google Drive for '{pctx.profile.display_name}'")
        return {"ok": True}

    return router

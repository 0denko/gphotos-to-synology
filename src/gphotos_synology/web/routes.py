"""FastAPI REST endpoints, multi-user profile management, auth session, and Google OAuth2 handlers."""

from __future__ import annotations

import hmac
import io
import json
import secrets
import time
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from PIL import Image, ImageOps
from pydantic import BaseModel

from gphotos_synology.database import utc_now_iso
from gphotos_synology.notifications import Notifier
from gphotos_synology.profiles import CreateProfileRequest, ProfileContext, UpdateProfileRequest
from gphotos_synology.sources.gdrive import GoogleDriveClient
from gphotos_synology.synology.photos_api import SynologyPhotosClient
from gphotos_synology.takeout.extractor import get_free_disk_gb
from gphotos_synology.takeout.metadata import exiftool_available
from gphotos_synology.web.security import (
    SESSION_COOKIE_NAME,
    assert_admin,
    assert_profile_access,
    get_identity,
    load_proxy_secret,
)

OAUTH_STATE_TTL_SECONDS = 600


class SettingsUpdateRequest(BaseModel):
    updates: dict[str, Any]


class AuthSessionRequest(BaseModel):
    token: str


def create_api_router() -> APIRouter:
    router = APIRouter(prefix="/api", tags=["migrator"])

    def _app_ctx(request: Request):
        return request.app.state.ctx

    def _profile_ctx(request: Request, profile: str | None = None) -> ProfileContext:
        app_ctx = _app_ctx(request)
        identity = get_identity(request)
        requested_pid = (
            profile
            or request.query_params.get("profile")
            or request.headers.get("x-profile-id")
            or ""
        ).strip()

        # C2: Non-admin DSM users default to their own DSM profile and cannot cross into others
        if not requested_pid:
            if identity.via_proxy and not identity.is_admin:
                requested_pid = app_ctx.registry.resolve_profile_for_dsm_user(identity.dsm_user)
            else:
                requested_pid = "default"

        try:
            pctx = app_ctx.registry.get_context(requested_pid)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        assert_profile_access(identity, pctx.profile)
        return pctx

    # ------------------------------------------------------------------
    # Authentication Session (Direct / Docker Mode)
    # ------------------------------------------------------------------
    @router.get("/auth/status")
    def auth_status(request: Request) -> dict[str, Any]:
        app_ctx = _app_ctx(request)
        identity = get_identity(request)
        settings = app_ctx.config_manager.settings
        proxy_configured = bool(load_proxy_secret(settings))
        auth_required = not (
            settings.allow_insecure_no_auth and not proxy_configured and not settings.api_token.strip()
        )
        return {
            "authenticated": identity.authenticated,
            "auth_required": auth_required,
            "via_proxy": identity.via_proxy,
            "dsm_user": identity.dsm_user,
            "is_admin": identity.is_admin,
        }

    @router.post("/auth/session")
    def create_auth_session(request: Request, body: AuthSessionRequest) -> JSONResponse:
        app_ctx = _app_ctx(request)
        expected = app_ctx.effective_api_token
        if not expected or not hmac.compare_digest(body.token.strip(), expected):
            raise HTTPException(status_code=401, detail="Invalid API token")
        is_secure = (
            request.url.scheme == "https"
            or request.headers.get("x-forwarded-proto", "").lower() == "https"
        )
        resp = JSONResponse({"ok": True})
        resp.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=expected,
            httponly=True,
            samesite="strict",
            secure=is_secure,
            path="/",
        )
        return resp

    @router.post("/auth/logout")
    def clear_auth_session() -> JSONResponse:
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
        return resp

    # ------------------------------------------------------------------
    # Multi-User Synology Profiles & Auto-Discovery
    # ------------------------------------------------------------------
    @router.get("/profiles")
    def list_profiles(request: Request) -> dict[str, Any]:
        app_ctx = _app_ctx(request)
        identity = get_identity(request)
        dsm_user = identity.dsm_user
        if identity.is_admin:
            profiles = app_ctx.registry.list_profiles()
            discovery = app_ctx.registry.discover_synology_users()
            recommended_id = app_ctx.registry.resolve_profile_for_dsm_user(dsm_user)
        else:
            profiles = app_ctx.registry.list_profiles(dsm_user_filter=dsm_user)
            discovery = app_ctx.registry.discover_synology_users(only_username=dsm_user)
            recommended_id = profiles[0]["id"] if profiles else ""
        return {
            "profiles": profiles,
            "dsm_user": dsm_user,
            "current_dsm_user": dsm_user,
            "is_admin": identity.is_admin,
            "recommended_profile_id": recommended_id,
            "homes_mounted": discovery["homes_mounted"],
            "discovered_synology_users": discovery["users"],
        }

    @router.post("/profiles")
    def create_profile(request: Request, body: CreateProfileRequest) -> dict[str, Any]:
        assert_admin(get_identity(request))
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
        assert_admin(get_identity(request))
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
        assert_admin(get_identity(request))
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
        conn_status = gdrive.connection_status()
        return {
            "profile": pctx.profile.model_dump(mode="json"),
            "progress": pctx.pipeline.get_progress(),
            "stats": pctx.db.get_stats(),
            "worker_running": app_ctx.scheduler.is_running,
            "worker_paused": app_ctx.scheduler.is_paused,
            "gdrive_oauth_configured": gdrive.is_oauth_configured(),
            "gdrive_connected": conn_status["connected"],
            "gdrive_auth_error": conn_status["error"],
            "exiftool_available": exiftool_available(settings.exiftool_path),
            "synology_api_enabled": settings.synology_api_enabled,
            "photos_dir_writable": settings.is_photos_dir_writable(),
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
        assert_admin(get_identity(request))
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
        warnings = list(getattr(pctx.config_manager, "last_update_warnings", []))
        pctx.db.log_event("INFO", "config", "Updated runtime configuration from dashboard")
        return {
            "ok": True,
            "settings": pctx.config_manager.to_public_dict(),
            "warnings": warnings,
        }

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
        assert_admin(get_identity(request))
        _app_ctx(request).scheduler.pause()
        return {"ok": True, "worker_paused": True}

    @router.post("/actions/resume")
    def resume_worker(request: Request) -> dict[str, Any]:
        assert_admin(get_identity(request))
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
    # Per-Profile Google Drive OAuth2 Flow (G3 Fail-Closed)
    # ------------------------------------------------------------------
    def _trust_forwarded(request: Request) -> bool:
        identity = get_identity(request)
        if identity.via_proxy:
            return True
        return bool(_app_ctx(request).config_manager.settings.trust_proxy_headers)

    def _public_base_url(request: Request) -> str:
        if _trust_forwarded(request):
            proto = request.headers.get("x-forwarded-proto")
            host = request.headers.get("x-forwarded-host")
            prefix = request.headers.get("x-forwarded-prefix", "").rstrip("/")
            if proto and host:
                return f"{proto}://{host}{prefix}"
            return str(request.base_url).rstrip("/") + prefix
        return str(request.base_url).rstrip("/")

    def _is_cgi_proxy(request: Request) -> bool:
        return _trust_forwarded(request) and request.headers.get("x-proxy-mode") == "cgi"

    def _resolve_redirect_uri(request: Request, pctx: ProfileContext) -> str:
        settings = pctx.config_manager.settings
        if settings.google_redirect_uri.strip():
            return settings.google_redirect_uri.strip()
        if _is_cgi_proxy(request):
            return f"{_public_base_url(request)}/oauth_callback.cgi"
        return f"{_public_base_url(request)}/api/oauth/google/callback"

    @router.get("/oauth/google/start")
    def google_oauth_start(request: Request, profile: str | None = None) -> dict[str, Any]:
        app_ctx = _app_ctx(request)
        identity = get_identity(request)
        pctx = _profile_ctx(request, profile)
        gdrive = GoogleDriveClient(pctx.config_manager.settings)
        redirect_uri = _resolve_redirect_uri(request, pctx)

        # G3 + PKCE: Generate authorization URL + PKCE code_verifier, then persist opaque one-time state
        state_token = secrets.token_urlsafe(32)
        try:
            auth_url, _, code_verifier = gdrive.build_authorization_url(
                redirect_uri=redirect_uri, state=state_token
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        state_record = json.dumps(
            {
                "state": state_token,
                "profile_id": pctx.profile.id,
                "redirect_uri": redirect_uri,
                "dsm_user": identity.dsm_user,
                "is_admin": identity.is_admin,
                "code_verifier": code_verifier or "",
                "created_at": time.time(),
            }
        )
        app_ctx.db.set_state(f"oauth_state:{state_token}", state_record)
        return {"authorization_url": auth_url, "redirect_uri": redirect_uri}

    @router.get("/oauth/google/callback")
    def google_oauth_callback(
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ) -> RedirectResponse:
        app_ctx = _app_ctx(request)
        identity = get_identity(request)
        base_prefix = (
            request.headers.get("x-forwarded-prefix", "").rstrip("/")
            if _trust_forwarded(request)
            else ""
        )
        home_url = f"{base_prefix}/proxy.cgi" if _is_cgi_proxy(request) else f"{base_prefix}/"

        # G3: Fail closed — state MUST match a live, unexpired server-side record
        state_key = f"oauth_state:{state}" if state else ""
        raw_record = app_ctx.db.get_state(state_key) if state_key else None

        if not state or not raw_record:
            app_ctx.db.log_event("ERROR", "oauth", "Google OAuth callback rejected: missing or unknown state")
            return RedirectResponse(url=f"{home_url}?oauth=error&detail=state_mismatch", status_code=302)

        try:
            rec = json.loads(raw_record)
            stored_state = str(rec.get("state") or "")
            profile_id = str(rec.get("profile_id") or "")
            bound_redirect_uri = str(rec.get("redirect_uri") or "")
            bound_dsm_user = str(rec.get("dsm_user") or "")
            bound_code_verifier = str(rec.get("code_verifier") or "") or None
            created_at = float(rec.get("created_at") or 0.0)
        except (ValueError, TypeError):
            app_ctx.db.delete_state(state_key)
            return RedirectResponse(url=f"{home_url}?oauth=error&detail=state_mismatch", status_code=302)

        if not hmac.compare_digest(state, stored_state) or (time.time() - created_at) > OAUTH_STATE_TTL_SECONDS:
            app_ctx.db.delete_state(state_key)
            app_ctx.db.log_event("ERROR", "oauth", "Google OAuth callback rejected: expired or mismatched state")
            return RedirectResponse(url=f"{home_url}?oauth=error&detail=state_mismatch", status_code=302)

        # C2 + G3: In proxied mode, verify callback user matches the user who started OAuth
        # BEFORE consuming state_key so another user cannot DoS a victim's in-flight OAuth state
        effective_identity = identity
        if identity.via_proxy and not identity.dsm_user:
            # Fresh DSM 7.3 webapi-validated callback without bridge cookie:
            # take initiating user from server-side OAuth state (fail closed if absent).
            if not bound_dsm_user or request.headers.get("x-dsm-session-verified", "").strip() != "1":
                app_ctx.db.log_event("ERROR", "oauth", "Google OAuth callback rejected: missing initiating DSM user")
                return RedirectResponse(url=f"{home_url}?oauth=error&detail=state_mismatch", status_code=302)
            from gphotos_synology.web.security import RequestIdentity

            effective_identity = RequestIdentity(
                authenticated=True,
                via_proxy=True,
                dsm_user=bound_dsm_user,
                is_admin=bool(rec.get("is_admin", False)),
            )
        elif identity.via_proxy and bound_dsm_user and bound_dsm_user.lower() != identity.dsm_user.lower():
            app_ctx.db.log_event("ERROR", "oauth", "Google OAuth callback rejected: DSM user mismatch")
            return RedirectResponse(url=f"{home_url}?oauth=error&detail=state_mismatch", status_code=302)

        try:
            pctx = app_ctx.registry.get_context(profile_id)
            assert_profile_access(effective_identity, pctx.profile)
        except (KeyError, HTTPException):
            return RedirectResponse(url=f"{home_url}?oauth=error&detail=profile_not_allowed", status_code=302)

        # Consume verified state immediately (one-time use, prevents replay)
        app_ctx.db.delete_state(state_key)

        safe_pid = quote(profile_id, safe="")
        if error:
            safe_err = quote(error, safe="")
            pctx.db.log_event("ERROR", "oauth", f"Google OAuth returned error: {error}")
            return RedirectResponse(
                url=f"{home_url}?profile={safe_pid}&oauth=error&detail={safe_err}",
                status_code=302,
            )
        if not code:
            return RedirectResponse(
                url=f"{home_url}?profile={safe_pid}&oauth=error&detail=missing_code",
                status_code=302,
            )

        gdrive = GoogleDriveClient(pctx.config_manager.settings)
        redirect_uri = bound_redirect_uri or _resolve_redirect_uri(request, pctx)
        try:
            gdrive.exchange_code(
                code=code,
                redirect_uri=redirect_uri,
                code_verifier=bound_code_verifier,
            )
            pctx.db.log_event("INFO", "oauth", f"Google Drive connected for '{pctx.profile.display_name}'")
            app_ctx.scheduler.trigger_now(profile_id=pctx.profile.id, albums_only=False)
            return RedirectResponse(url=f"{home_url}?profile={safe_pid}&oauth=success", status_code=302)
        except Exception as exc:
            pctx.db.log_event("ERROR", "oauth", f"OAuth token exchange failed: {exc}")
            return RedirectResponse(
                url=f"{home_url}?profile={safe_pid}&oauth=error&detail=token_exchange_failed",
                status_code=302,
            )

    @router.post("/oauth/google/disconnect")
    def google_oauth_disconnect(request: Request, profile: str | None = None) -> dict[str, Any]:
        pctx = _profile_ctx(request, profile)
        GoogleDriveClient(pctx.config_manager.settings).disconnect()
        pctx.db.log_event("INFO", "oauth", f"Disconnected Google Drive for '{pctx.profile.display_name}'")
        return {"ok": True}

    return router

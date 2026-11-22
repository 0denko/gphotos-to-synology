"""Shared opaque-box fixtures, synthetic media/archive builders, mock HTTP servers,
CGI subprocess runner, SPK lifecycle harness, and Node `app.js` harness for `tests/e2e/`.

All helpers interact strictly through public interfaces (HTTP APIs, CGI subprocesses,
public Python classes/functions, POSIX `/bin/sh` SPK scripts, and `node` JS execution).
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import socket
import struct
import subprocess
import sys
import tarfile
import threading
import time
import urllib.parse
import zipfile
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from PIL import Image, ImageDraw, ImageFilter

from gphotos_synology.config import Settings
from gphotos_synology.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]
_E2E_DIR = str(Path(__file__).resolve().parent)
if _E2E_DIR not in sys.path:
    sys.path.insert(0, _E2E_DIR)

PROXY_CGI_PATH = REPO_ROOT / "synology-spk" / "ui" / "proxy.cgi"
OAUTH_CALLBACK_CGI_PATH = REPO_ROOT / "synology-spk" / "ui" / "oauth_callback.cgi"
BUILD_SPK_PATH = REPO_ROOT / "synology-spk" / "build_spk.py"
APP_JS_PATH = REPO_ROOT / "src" / "gphotos_synology" / "web" / "static" / "app.js"
GPHOTOS_MIGRATOR_JS_PATH = REPO_ROOT / "synology-spk" / "ui" / "GPhotosMigrator.js"

# Env vars cleared before each E2E test so host environment / .env never leaks.
_ISOLATED_ENV_VARS = (
    "DATA_DIR",
    "WORK_DIR",
    "INBOX_DIR",
    "PHOTOS_DIR",
    "HOMES_DIR",
    "QUARANTINE_DIR",
    "QUARANTINE_DIR_OVERRIDE",
    "APP_HOST",
    "APP_PORT",
    "APP_BUILD_SHA",
    "APP_BUILD_DATE",
    "API_TOKEN",
    "GPHOTOS_PROXY_SECRET",
    "PROXY_SECRET",
    "PROXY_SECRET_FILE",
    "GPHOTOS_PROXY_SECRET_FILE",
    "ALLOW_INSECURE_NO_AUTH",
    "DSM_ADMIN_USERS",
    "TRUST_PROXY_HEADERS",
    "SYNOLOGY_URL",
    "SYNOLOGY_VERIFY_SSL",
    "SYNOLOGY_CA_CERT",
    "SYNOLOGY_USERNAME",
    "SYNOLOGY_PASSWORD",
    "SYNOLOGY_API_ENABLED",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_REDIRECT_URI",
    "WEBHOOK_URL",
    "WEBHOOK_TYPE",
    "WATCHTOWER_URL",
    "WATCHTOWER_TOKEN",
    "GITHUB_REPO",
    "NO_PROXY",
    "no_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
)


@pytest.fixture(autouse=True)
def _isolate_e2e_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent host environment variables from leaking into opaque-box Settings."""
    for var in _ISOLATED_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Isolated Settings & FastAPI App Builders
# ---------------------------------------------------------------------------


def make_isolated_settings(base_dir: Path, **overrides: Any) -> Settings:
    """Construct a clean `Settings` rooted under `base_dir` without reading repo `.env`."""
    defaults: dict[str, Any] = {
        "data_dir": base_dir / "config",
        "work_dir": base_dir / "work",
        "inbox_dir": base_dir / "inbox",
        "photos_dir": base_dir / "photos",
        "homes_dir": base_dir / "homes",
        "inbox_stability_seconds": 0,
        "min_free_disk_gb": 0.1,
        "poll_interval_minutes": 60,
    }
    defaults.update(overrides)
    settings = Settings(_env_file=None, **defaults)  # type: ignore[call-arg]
    settings.ensure_directories()
    return settings


def make_csrf_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return standard headers including the G6 CSRF custom header."""
    headers = {"X-Requested-With": "XMLHttpRequest"}
    if extra:
        headers.update(extra)
    return headers


def make_proxy_headers(
    *,
    secret: str,
    dsm_user: str = "admin",
    is_admin: bool = True,
    profile_id: str | None = None,
    csrf: bool = True,
    host: str = "mynas.local:5001",
    proto: str = "https",
    prefix: str = "/webman/3rdparty/gphotos-migrator",
) -> dict[str, str]:
    """Construct verified CGI proxy headers matching `PROJECT.md § Interface Contracts #1`."""
    headers: dict[str, str] = {
        "X-Proxy-Secret": secret,
        "X-Proxy-Mode": "cgi",
        "X-DSM-User": dsm_user,
        "X-DSM-Admin": "1" if is_admin else "0",
        "X-Forwarded-Proto": proto,
        "X-Forwarded-Host": host,
        "X-Forwarded-Prefix": prefix,
    }
    if profile_id is not None:
        headers["X-Profile-Id"] = profile_id
    if csrf:
        headers["X-Requested-With"] = "XMLHttpRequest"
    return headers


def make_token_headers(
    token: str,
    *,
    profile_id: str | None = None,
    csrf: bool = True,
    header_style: str = "bearer",
) -> dict[str, str]:
    """Construct direct API token auth headers."""
    headers: dict[str, str] = {}
    if header_style == "bearer":
        headers["Authorization"] = f"Bearer {token}"
    else:
        headers["X-API-Token"] = token
    if profile_id is not None:
        headers["X-Profile-Id"] = profile_id
    if csrf:
        headers["X-Requested-With"] = "XMLHttpRequest"
    return headers


# ---------------------------------------------------------------------------
# Synthetic Media & Takeout Archive Builders
# ---------------------------------------------------------------------------


def make_patterned_image(
    width: int = 320,
    height: int = 240,
    *,
    seed: int = 0,
    blur_radius: float = 0.0,
) -> Image.Image:
    """Create a deterministic textured RGB image with sharp geometric features.

    Renders at a canonical 640x480 scene and resizes via LANCZOS so different
    resolutions of the same `seed` have perceptual dHash distance <= 3 and trigger
    resolution upgrades cleanly, while different `seed` values have dHash distance > 40.
    """
    base_w, base_h = 640, 480
    bg_r = (30 + seed * 53) % 220
    bg_g = (45 + seed * 97) % 220
    bg_b = (60 + seed * 131) % 220
    img = Image.new("RGB", (base_w, base_h), color=(bg_r, bg_g, bg_b))
    draw = ImageDraw.Draw(img)

    for idx in range(8):
        x0 = int(base_w * ((idx * 19 + seed * 13) % 75) / 100.0)
        y0 = int(base_h * ((idx * 29 + seed * 17) % 75) / 100.0)
        x1 = min(base_w - 1, x0 + int(base_w * (0.18 + ((idx + seed) % 3) * 0.05)))
        y1 = min(base_h - 1, y0 + int(base_h * (0.18 + ((idx * 2 + seed) % 3) * 0.05)))
        color = (
            (255 - bg_r + idx * 41 + seed * 19) % 256,
            (255 - bg_g + idx * 67 + seed * 23) % 256,
            (255 - bg_b + idx * 89 + seed * 31) % 256,
        )
        if (idx + seed) % 2 == 0:
            draw.rectangle([x0, y0, x1, y1], fill=color, outline=(255, 255, 255), width=2)
        else:
            draw.ellipse([x0, y0, x1, y1], fill=color, outline=(10, 10, 10), width=2)

    if (width, height) != (base_w, base_h):
        img = img.resize((width, height), Image.Resampling.LANCZOS)

    if blur_radius > 0:
        img = img.filter(ImageFilter.GaussianBlur(radius=blur_radius))
    return img


def image_to_jpeg_bytes(
    width: int = 320,
    height: int = 240,
    *,
    seed: int = 0,
    quality: int = 90,
    blur_radius: float = 0.0,
    exif_bytes: bytes | None = None,
) -> bytes:
    """Render a deterministic synthetic JPEG into bytes."""
    img = make_patterned_image(width, height, seed=seed, blur_radius=blur_radius)
    buf = io.BytesIO()
    save_kwargs: dict[str, Any] = {"format": "JPEG", "quality": quality}
    if exif_bytes is not None:
        save_kwargs["exif"] = exif_bytes
    img.save(buf, **save_kwargs)
    return buf.getvalue()


def save_jpeg(
    path: Path,
    width: int = 320,
    height: int = 240,
    *,
    seed: int = 0,
    quality: int = 90,
    blur_radius: float = 0.0,
    exif_bytes: bytes | None = None,
) -> Path:
    """Write a deterministic synthetic JPEG to `path`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        image_to_jpeg_bytes(
            width,
            height,
            seed=seed,
            quality=quality,
            blur_radius=blur_radius,
            exif_bytes=exif_bytes,
        )
    )
    return path


def save_png(path: Path, width: int = 320, height: int = 240, *, seed: int = 0) -> Path:
    """Write a deterministic synthetic PNG to `path`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    img = make_patterned_image(width, height, seed=seed)
    img.save(path, format="PNG")
    return path


def save_heic(
    path: Path,
    width: int = 320,
    height: int = 240,
    *,
    seed: int = 0,
    quality: int = 85,
) -> Path:
    """Write a genuine HEIC image using `pillow_heif` (v1.2.0 G10 requirement).

    Imports `pillow_heif` lazily so module collection never crashes before M1 lands,
    and fails cleanly with a descriptive AssertionError when absent.
    """
    try:
        import pillow_heif
    except ImportError as exc:
        raise AssertionError(
            "G10 requires pillow-heif to be installed for HEIC/HEIF support"
        ) from exc

    pillow_heif.register_heif_opener()
    path.parent.mkdir(parents=True, exist_ok=True)
    img = make_patterned_image(width, height, seed=seed)
    heif_file = pillow_heif.from_pillow(img)
    heif_file.save(str(path), quality=quality)
    return path


def make_corrupt_exif_jpeg_bytes(width: int = 160, height: int = 120, *, seed: int = 7) -> bytes:
    """Return a valid JPEG whose APP1 EXIF segment is malformed so `piexif.load` raises."""
    raw = image_to_jpeg_bytes(width, height, seed=seed, quality=85)
    assert raw[:2] == b"\xff\xd8"
    corrupt_payload = b"Exif\x00\x00CORRUPT_TIFF_HEADER_DO_NOT_WIPE_1234567890"
    app1 = b"\xff\xe1" + struct.pack(">H", len(corrupt_payload) + 2) + corrupt_payload
    return raw[:2] + app1 + raw[2:]


def save_corrupt_exif_jpeg(path: Path, width: int = 160, height: int = 120, *, seed: int = 7) -> Path:
    """Write a valid JPEG with a corrupt EXIF APP1 segment (for G13 verification)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(make_corrupt_exif_jpeg_bytes(width, height, seed=seed))
    return path


def make_sidecar_payload(
    title: str,
    *,
    timestamp: int | float | str = 1720000000,
    description: str = "",
    lat: float = 0.0,
    lon: float = 0.0,
    alt: float = 0.0,
    use_geo_exif: bool = True,
) -> dict[str, Any]:
    """Create a realistic Google Takeout sidecar JSON dictionary."""
    geo = {"latitude": lat, "longitude": lon, "altitude": alt}
    zero_geo = {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0}
    return {
        "title": title,
        "description": description,
        "photoTakenTime": {"timestamp": str(timestamp)},
        "creationTime": {"timestamp": str(timestamp)},
        "geoDataExif": geo if use_geo_exif else zero_geo,
        "geoData": zero_geo if use_geo_exif else geo,
    }


def build_zip_archive(
    dest_path: Path,
    entries: dict[str, bytes | str],
    *,
    compress_type: int = zipfile.ZIP_DEFLATED,
) -> Path:
    """Build a ZIP archive at `dest_path` from `{arcname: bytes_or_str}`."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest_path, "w", compression=compress_type) as zf:
        for arcname, content in entries.items():
            data = content.encode("utf-8") if isinstance(content, str) else content
            zf.writestr(arcname, data)
    return dest_path


def build_tar_gz_archive(
    dest_path: Path,
    entries: dict[str, bytes | str],
    *,
    symlinks: dict[str, str] | None = None,
) -> Path:
    """Build a `.tar.gz` archive at `dest_path` with optional symlink members."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(dest_path, "w:gz") as tf:
        for arcname, content in entries.items():
            data = content.encode("utf-8") if isinstance(content, str) else content
            ti = tarfile.TarInfo(name=arcname)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        if symlinks:
            for link_name, link_target in symlinks.items():
                ti = tarfile.TarInfo(name=link_name)
                ti.type = tarfile.SYMTYPE
                ti.linkname = link_target
                tf.addfile(ti)
    return dest_path


def make_stub_exiftool(tmp_path: Path) -> tuple[Path, Path]:
    """Create a POSIX `#!/bin/sh` stub exiftool script that logs argv and exits 0."""
    log_file = tmp_path / "exiftool_invocations.log"
    script = tmp_path / "stub_exiftool.sh"
    script.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"{log_file}\"\nexit 0\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script, log_file


def sha256_file(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Local Mock HTTP Servers (DSM Photos API, Webhook, Generic CGI Backend)
# ---------------------------------------------------------------------------


@dataclass
class RecordedRequest:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body: bytes
    raw_url: str
    form: dict[str, list[str]] = field(default_factory=dict)


class MockDSMState:
    """Mutable state for the local mock Synology DSM `/webapi/entry.cgi` server."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: list[RecordedRequest] = []
        self.valid_username = "dsmuser"
        self.valid_password = "dsm-secret-password"
        self.sid_counter = 0
        self.active_sid = ""
        self.expire_next_api_calls = 0
        self.albums: dict[str, int] = {}
        self.album_items: dict[int, list[int]] = {}
        self.next_album_id = 100
        # Folder tree: {parent_id_or_None: [{"id": int, "name": str}]}
        # Root folder id is 1; subfolders can be registered by path e.g. "2024/07"
        self.root_folder_id = 1
        self.folders_by_path: dict[str, int] = {"": 1}
        self.folder_children: dict[int | None, list[dict[str, Any]]] = {None: [], 1: []}
        self.next_folder_id = 10
        # Items indexed by DSM: {folder_id: [{"id": int, "filename": str, "folder_id": int}]}
        self.items_by_folder: dict[int, list[dict[str, Any]]] = {1: []}
        self.next_item_id = 500
        self.reject_login = False

    def ensure_folder(self, rel_folder_posix: str) -> int:
        normalized = rel_folder_posix.strip("/")
        if not normalized:
            return self.root_folder_id
        with self.lock:
            if normalized in self.folders_by_path:
                return self.folders_by_path[normalized]
            parts = [p for p in normalized.split("/") if p]
            parent_id: int | None = None
            built = ""
            for part in parts:
                built = f"{built}/{part}" if built else part
                if built in self.folders_by_path:
                    parent_id = self.folders_by_path[built]
                    continue
                fid = self.next_folder_id
                self.next_folder_id += 1
                self.folders_by_path[built] = fid
                self.folder_children.setdefault(parent_id, []).append({"id": fid, "name": part})
                self.folder_children.setdefault(fid, [])
                self.items_by_folder.setdefault(fid, [])
                parent_id = fid
            assert parent_id is not None
            return parent_id

    def add_indexed_item(self, rel_path_posix: str) -> int:
        """Register an indexed photo in mock DSM (supports root `'IMG.jpg'` & `'2024/07/IMG.jpg'`)."""
        cleaned = rel_path_posix.strip("/")
        if "/" in cleaned:
            folder_part, filename = cleaned.rsplit("/", 1)
            folder_id = self.ensure_folder(folder_part)
        else:
            filename = cleaned
            folder_id = self.root_folder_id
        with self.lock:
            item_id = self.next_item_id
            self.next_item_id += 1
            self.items_by_folder.setdefault(folder_id, []).append(
                {"id": item_id, "filename": filename, "folder_id": folder_id}
            )
            return item_id


@dataclass
class MockDSMServer:
    url: str
    port: int
    state: MockDSMState
    _httpd: ThreadingHTTPServer
    _thread: threading.Thread

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5.0)


def _start_mock_dsm_server() -> MockDSMServer:
    state = MockDSMState()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def _handle(self, method: str) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            length = int(self.headers.get("Content-Length") or "0")
            body = self.rfile.read(length) if length > 0 else b""
            form = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
            rec = RecordedRequest(
                method=method,
                path=parsed.path,
                query=query,
                headers={k.lower(): v for k, v in self.headers.items()},
                body=body,
                raw_url=self.path,
                form=form,
            )
            with state.lock:
                state.requests.append(rec)

            params: dict[str, str] = {}
            for k, vals in query.items():
                if vals:
                    params[k] = vals[0]
            for k, vals in form.items():
                if vals:
                    params[k] = vals[0]

            api = params.get("api", "")
            api_method = params.get("method", "")
            resp_obj = self._dispatch(api, api_method, params)
            payload = json.dumps(resp_obj).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _dispatch(self, api: str, api_method: str, params: dict[str, str]) -> dict[str, Any]:
            with state.lock:
                if api == "SYNO.API.Auth" and api_method == "login":
                    if state.reject_login:
                        return {"success": False, "error": {"code": 400}}
                    if (
                        params.get("account") != state.valid_username
                        or params.get("passwd") != state.valid_password
                    ):
                        return {"success": False, "error": {"code": 400}}
                    state.sid_counter += 1
                    state.active_sid = f"sid-{state.sid_counter}"
                    return {
                        "success": True,
                        "data": {"sid": state.active_sid, "synotoken": f"tok-{state.sid_counter}"},
                    }

                if api == "SYNO.API.Auth" and api_method == "logout":
                    return {"success": True, "data": {}}

                # Simulate expired session (code 119) on demand
                if state.expire_next_api_calls > 0:
                    state.expire_next_api_calls -= 1
                    return {"success": False, "error": {"code": 119}}

                if api == "SYNO.Foto.Browse.Album" and api_method == "list":
                    items = [{"id": aid, "name": name} for name, aid in state.albums.items()]
                    offset = int(params.get("offset", "0"))
                    limit = int(params.get("limit", "500"))
                    return {"success": True, "data": {"list": items[offset : offset + limit]}}

                if api == "SYNO.Foto.Browse.NormalAlbum" and api_method == "create":
                    name = params.get("name", "")
                    aid = state.albums.get(name)
                    if aid is None:
                        aid = state.next_album_id
                        state.next_album_id += 1
                        state.albums[name] = aid
                        state.album_items[aid] = []
                    return {"success": True, "data": {"album": {"id": aid, "name": name}}}

                if api == "SYNO.Foto.Browse.NormalAlbum" and api_method == "add_item":
                    aid = int(params.get("id", "0"))
                    ids = json.loads(params.get("item", "[]"))
                    bucket = state.album_items.setdefault(aid, [])
                    for item_id in ids:
                        if item_id not in bucket:
                            bucket.append(int(item_id))
                    return {"success": True, "data": {}}

                if api.endswith(".Folder") and api_method == "get":
                    return {
                        "success": True,
                        "data": {"folder": {"id": state.root_folder_id, "name": "/"}},
                    }

                if api.endswith(".Folder") and api_method == "list":
                    raw_id = params.get("id")
                    parent_id = int(raw_id) if raw_id not in (None, "") else None
                    # Support callers querying parent_id=None OR parent_id=root_folder_id
                    children = list(state.folder_children.get(parent_id, []))
                    if parent_id == state.root_folder_id and not children:
                        children = list(state.folder_children.get(None, []))
                    return {"success": True, "data": {"list": children}}

                if api.endswith(".Item") and api_method == "list":
                    raw_fid = params.get("folder_id")
                    if raw_fid not in (None, ""):
                        fid = int(raw_fid)
                        items = list(state.items_by_folder.get(fid, []))
                    else:
                        # Mirror real DSM behavior: omitting folder_id returns items across ALL folders
                        items = []
                        for fid_key in sorted(state.items_by_folder.keys(), reverse=True):
                            items.extend(state.items_by_folder[fid_key])
                    offset = int(params.get("offset", "0"))
                    limit = int(params.get("limit", "1000"))
                    return {"success": True, "data": {"list": items[offset : offset + limit]}}

                return {"success": False, "error": {"code": 101}}

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = int(httpd.server_address[1])
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return MockDSMServer(
        url=f"http://127.0.0.1:{port}",
        port=port,
        state=state,
        _httpd=httpd,
        _thread=thread,
    )


@pytest.fixture
def mock_dsm_server() -> Generator[MockDSMServer, None, None]:
    """Start an isolated local mock Synology DSM `/webapi/entry.cgi` HTTP server."""
    srv = _start_mock_dsm_server()
    try:
        yield srv
    finally:
        srv.close()


@dataclass
class MockWebhookServer:
    url: str
    port: int
    requests: list[RecordedRequest]
    delay_seconds: float
    status_code: int
    _lock: threading.Lock
    _httpd: ThreadingHTTPServer
    _thread: threading.Thread

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5.0)


@pytest.fixture
def mock_webhook_server() -> Generator[MockWebhookServer, None, None]:
    """Start a local HTTP server capturing webhook POSTs with optional artificial latency."""
    lock = threading.Lock()
    recorded: list[RecordedRequest] = []
    holder: dict[str, Any] = {"delay": 0.0, "status": 200}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def _handle(self, method: str) -> None:
            delay = float(holder["delay"])
            if delay > 0:
                time.sleep(delay)
            parsed = urllib.parse.urlsplit(self.path)
            length = int(self.headers.get("Content-Length") or "0")
            body = self.rfile.read(length) if length > 0 else b""
            rec = RecordedRequest(
                method=method,
                path=parsed.path,
                query=urllib.parse.parse_qs(parsed.query, keep_blank_values=True),
                headers={k.lower(): v for k, v in self.headers.items()},
                body=body,
                raw_url=self.path,
            )
            with lock:
                recorded.append(rec)
            code = int(holder["status"])
            payload = b'{"ok": true}'
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self) -> None:
            self._handle("POST")

        def do_GET(self) -> None:
            self._handle("GET")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = int(httpd.server_address[1])
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    class _ManagedWebhook(MockWebhookServer):
        @property
        def delay_seconds(self) -> float:  # type: ignore[override]
            return float(holder["delay"])

        @delay_seconds.setter
        def delay_seconds(self, value: float) -> None:
            holder["delay"] = float(value)

        @property
        def status_code(self) -> int:  # type: ignore[override]
            return int(holder["status"])

        @status_code.setter
        def status_code(self, value: int) -> None:
            holder["status"] = int(value)

    srv = _ManagedWebhook(
        url=f"http://127.0.0.1:{port}/hook",
        port=port,
        requests=recorded,
        delay_seconds=0.0,
        status_code=200,
        _lock=lock,
        _httpd=httpd,
        _thread=thread,
    )
    try:
        yield srv
    finally:
        srv.close()


@dataclass
class MockBackendServer:
    url: str
    port: int
    requests: list[RecordedRequest]
    response_status: int
    response_headers: list[tuple[str, str]]
    response_body: bytes
    raw_response_bytes: bytes | None
    _httpd: ThreadingHTTPServer
    _thread: threading.Thread

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5.0)


@pytest.fixture
def mock_backend_server() -> Generator[MockBackendServer, None, None]:
    """Local HTTP backend for testing `proxy.cgi` & `oauth_callback.cgi` subprocesses."""
    lock = threading.Lock()
    recorded: list[RecordedRequest] = []
    cfg: dict[str, Any] = {
        "status": 200,
        "headers": [("Content-Type", "application/json")],
        "body": b'{"ok": true}',
        "raw": None,
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def _handle(self, method: str) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            length = int(self.headers.get("Content-Length") or "0")
            body = self.rfile.read(length) if length > 0 else b""
            rec = RecordedRequest(
                method=method,
                path=parsed.path,
                query=urllib.parse.parse_qs(parsed.query, keep_blank_values=True),
                headers={k.lower(): v for k, v in self.headers.items()},
                body=body,
                raw_url=self.path,
            )
            with lock:
                recorded.append(rec)

            raw = cfg["raw"]
            if raw is not None:
                self.wfile.write(raw)
                self.wfile.flush()
                return

            payload = bytes(cfg["body"])
            self.send_response(int(cfg["status"]))
            for k, v in cfg["headers"]:
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def do_PUT(self) -> None:
            self._handle("PUT")

        def do_PATCH(self) -> None:
            self._handle("PATCH")

        def do_DELETE(self) -> None:
            self._handle("DELETE")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = int(httpd.server_address[1])
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    class _ManagedBackend(MockBackendServer):
        @property
        def response_status(self) -> int:  # type: ignore[override]
            return int(cfg["status"])

        @response_status.setter
        def response_status(self, val: int) -> None:
            cfg["status"] = int(val)

        @property
        def response_headers(self) -> list[tuple[str, str]]:  # type: ignore[override]
            return cfg["headers"]

        @response_headers.setter
        def response_headers(self, val: list[tuple[str, str]]) -> None:
            cfg["headers"] = list(val)

        @property
        def response_body(self) -> bytes:  # type: ignore[override]
            return bytes(cfg["body"])

        @response_body.setter
        def response_body(self, val: bytes) -> None:
            cfg["body"] = bytes(val)

        @property
        def raw_response_bytes(self) -> bytes | None:  # type: ignore[override]
            return cfg["raw"]

        @raw_response_bytes.setter
        def raw_response_bytes(self, val: bytes | None) -> None:
            cfg["raw"] = val

    srv = _ManagedBackend(
        url=f"http://127.0.0.1:{port}",
        port=port,
        requests=recorded,
        response_status=200,
        response_headers=[("Content-Type", "application/json")],
        response_body=b'{"ok": true}',
        raw_response_bytes=None,
        _httpd=httpd,
        _thread=thread,
    )
    try:
        yield srv
    finally:
        srv.close()


# ---------------------------------------------------------------------------
# Live Loopback Uvicorn FastAPI Server (for real CGI -> FastAPI E2E flows)
# ---------------------------------------------------------------------------


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class LiveFastAPIServer:
    url: str
    port: int
    app: Any
    settings: Settings
    _server: uvicorn.Server
    _thread: threading.Thread

    def close(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=8.0)


@contextlib.contextmanager
def start_live_fastapi_server(settings: Settings) -> Generator[LiveFastAPIServer, None, None]:
    """Run a real FastAPI app via uvicorn on an ephemeral loopback port."""
    app = create_app(base_settings=settings, start_worker=False)
    port = _free_tcp_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10.0
    ready = False
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{url}/api/health", timeout=0.5)
            if r.status_code == 200:
                ready = True
                break
        except Exception:
            time.sleep(0.05)
    if not ready:
        server.should_exit = True
        thread.join(timeout=3.0)
        raise RuntimeError(f"Live FastAPI test server failed to start on {url}")

    live = LiveFastAPIServer(
        url=url,
        port=port,
        app=app,
        settings=settings,
        _server=server,
        _thread=thread,
    )
    try:
        yield live
    finally:
        live.close()


# ---------------------------------------------------------------------------
# CGI Subprocess Runner (`proxy.cgi` & `oauth_callback.cgi`)
# ---------------------------------------------------------------------------


@dataclass
class CGIResult:
    status: int
    headers: dict[str, str]
    header_list: list[tuple[str, str]]
    raw_header_block: bytes
    body: bytes
    returncode: int
    stderr: str

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


def run_cgi(
    script_path: Path,
    *,
    backend_url: str,
    method: str = "GET",
    query_string: str = "",
    path_info: str = "",
    body: bytes = b"",
    content_type: str = "",
    content_length_override: str | None = None,
    proxy_secret: str | None = None,
    proxy_secret_file: Path | None = None,
    skip_dsm_auth: bool = True,
    dsm_user: str = "admin",
    dsm_admin: str = "1",
    http_headers: dict[str, str] | None = None,
    extra_env: dict[str, str] | None = None,
    timeout: float = 15.0,
) -> CGIResult:
    """Invoke a DSM CGI script via `/usr/bin/python3` and parse wire-format CGI stdout."""
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GPHOTOS_BACKEND_URL": backend_url,
        "REQUEST_METHOD": method.upper(),
        "QUERY_STRING": query_string,
        "PATH_INFO": path_info,
        "HTTP_HOST": "mynas.local:5001",
        "HTTPS": "on",
        "SERVER_PORT": "5001",
    }
    if skip_dsm_auth:
        env["GPHOTOS_SKIP_DSM_AUTH"] = "1"
        env["GPHOTOS_TEST_DSM_USER"] = dsm_user
        env["GPHOTOS_TEST_DSM_ADMIN"] = dsm_admin
    if proxy_secret is not None:
        env["GPHOTOS_PROXY_SECRET"] = proxy_secret
    if proxy_secret_file is not None:
        env["GPHOTOS_PROXY_SECRET_FILE"] = str(proxy_secret_file)
    if content_type:
        env["CONTENT_TYPE"] = content_type
    if content_length_override is not None:
        env["CONTENT_LENGTH"] = content_length_override
    elif body:
        env["CONTENT_LENGTH"] = str(len(body))

    if http_headers:
        for k, v in http_headers.items():
            env_key = "HTTP_" + k.upper().replace("-", "_")
            env[env_key] = v
    if extra_env:
        env.update(extra_env)

    python_exe = "/usr/bin/python3" if Path("/usr/bin/python3").is_file() else sys.executable
    proc = subprocess.run(
        [python_exe, str(script_path)],
        input=body,
        capture_output=True,
        env=env,
        timeout=timeout,
        check=False,
    )

    raw = proc.stdout
    sep = b"\r\n\r\n"
    idx = raw.find(sep)
    if idx == -1:
        sep = b"\n\n"
        idx = raw.find(sep)
    if idx == -1:
        raise AssertionError(
            f"CGI output missing header/body separator (rc={proc.returncode}, "
            f"stdout={raw[:300]!r}, stderr={proc.stderr.decode('utf-8', errors='replace')!r})"
        )

    raw_hdrs = raw[:idx]
    resp_body = raw[idx + len(sep) :]

    status = 200
    header_list: list[tuple[str, str]] = []
    headers_map: dict[str, str] = {}
    for line in raw_hdrs.decode("latin-1").splitlines():
        if ":" not in line:
            continue
        name, val = line.split(":", 1)
        name_clean = name.strip()
        val_clean = val.strip()
        if name_clean.lower() == "status":
            status = int(val_clean.split()[0])
        else:
            header_list.append((name_clean, val_clean))
            headers_map[name_clean.lower()] = val_clean

    return CGIResult(
        status=status,
        headers=headers_map,
        header_list=header_list,
        raw_header_block=raw_hdrs,
        body=resp_body,
        returncode=proc.returncode,
        stderr=proc.stderr.decode("utf-8", errors="replace"),
    )


# ---------------------------------------------------------------------------
# SPK Build & POSIX `/bin/sh` Lifecycle Harness
# ---------------------------------------------------------------------------


def make_stub_daemon_bundle(bundle_dir: Path) -> Path:
    """Create a tiny POSIX stub `gphotos-migrator` executable bundle for SPK tests.

    When launched by `scripts/start-stop-status`, the stub records its environment
    (`PROXY_SECRET_FILE`, `DATA_DIR`, `APP_HOST`, `APP_PORT`) into `$DATA_DIR/env_dump.txt`
    and sleeps until terminated by SIGTERM/SIGINT.
    """
    bundle_dir.mkdir(parents=True, exist_ok=True)
    exe = bundle_dir / "gphotos-migrator"
    exe.write_text(
        "#!/bin/sh\n"
        'mkdir -p "${DATA_DIR:-/tmp}"\n'
        '{\n'
        '  echo "PROXY_SECRET_FILE=${PROXY_SECRET_FILE:-}"\n'
        '  echo "APP_HOST=${APP_HOST:-}"\n'
        '  echo "APP_PORT=${APP_PORT:-}"\n'
        '  echo "DATA_DIR=${DATA_DIR:-}"\n'
        '  echo "WORK_DIR=${WORK_DIR:-}"\n'
        '  echo "INBOX_DIR=${INBOX_DIR:-}"\n'
        '  echo "PHOTOS_DIR=${PHOTOS_DIR:-}"\n'
        '} > "${DATA_DIR}/env_dump.txt"\n'
        'trap "exit 0" TERM INT\n'
        'while :; do sleep 1; done\n',
        encoding="utf-8",
    )
    exe.chmod(0o755)
    return bundle_dir


def build_test_spk(
    tmp_path: Path,
    *,
    arch: str = "x86_64",
    standalone: bool = True,
    build_sha: str = "1234567890abcdef",
    build_date: str = "2026-01-15T12:00:00Z",
    output_name: str = "test.spk",
    bundle_dir: Path | None = None,
) -> Path:
    """Run `synology-spk/build_spk.py` writing `.spk` into `tmp_path` without dirtying repo."""
    out_path = tmp_path / output_name
    cmd = [
        sys.executable,
        str(BUILD_SPK_PATH),
        "--arch",
        arch,
        "--output",
        str(out_path),
        "--build-sha",
        build_sha,
        "--build-date",
        build_date,
    ]
    if standalone:
        bdir = bundle_dir or make_stub_daemon_bundle(tmp_path / f"bundle_{arch}")
        cmd.extend(["--bundle-dir", str(bdir)])

    proc = subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"build_spk.py failed (rc={proc.returncode}):\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    assert out_path.is_file(), f"Expected built SPK at {out_path}"
    # Clean up empty repo `build/` directory if build_spk.py created it
    repo_build_dir = REPO_ROOT / "build"
    if repo_build_dir.is_dir():
        with contextlib.suppress(OSError):
            repo_build_dir.rmdir()
    return out_path


def extract_spk(spk_path: Path, dest_dir: Path) -> dict[str, Path]:
    """Unpack an `.spk` outer tar and its inner `package.tgz` into `dest_dir`."""
    outer_dir = dest_dir / "outer"
    target_dir = dest_dir / "target"
    outer_dir.mkdir(parents=True, exist_ok=True)
    target_dir.mkdir(parents=True, exist_ok=True)

    with tarfile.open(spk_path, "r:") as tf:
        tf.extractall(outer_dir)

    pkg_tgz = outer_dir / "package.tgz"
    assert pkg_tgz.is_file(), "SPK missing package.tgz"
    with tarfile.open(pkg_tgz, "r:gz") as tf:
        tf.extractall(target_dir)

    return {
        "outer": outer_dir,
        "target": target_dir,
        "info": outer_dir / "INFO",
        "scripts": outer_dir / "scripts",
        "conf": outer_dir / "conf",
    }


def parse_spk_info(info_path: Path) -> dict[str, str]:
    """Parse Synology SPK `INFO` key="value" file into a dictionary."""
    result: dict[str, str] = {}
    for raw_line in info_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        result[k.strip()] = v.strip().strip('"')
    return result


def run_posix_script(
    script_path: Path,
    args: list[str] | None = None,
    *,
    env: dict[str, str],
    timeout: float = 20.0,
) -> subprocess.CompletedProcess[str]:
    """Execute a POSIX `#!/bin/sh` SPK lifecycle script via `/bin/sh` (`dash`)."""
    cmd = ["/bin/sh", str(script_path), *(args or [])]
    merged_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **env}
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=merged_env,
        timeout=timeout,
        check=False,
    )


# ---------------------------------------------------------------------------
# Node `app.js` Opaque-Box Browser DOM/Fetch Harness
# ---------------------------------------------------------------------------

_NODE_APP_JS_HARNESS = r"""
const fs = require("node:fs");
const vm = require("node:vm");

const config = JSON.parse(fs.readFileSync(0, "utf8"));
const code = fs.readFileSync(config.appJsPath, "utf8");

class MockClassList {
  constructor() { this._set = new Set(); }
  add(...cls) { cls.forEach((c) => this._set.add(c)); }
  remove(...cls) { cls.forEach((c) => this._set.delete(c)); }
  toggle(c, force) {
    if (force === undefined) {
      if (this._set.has(c)) this._set.delete(c); else this._set.add(c);
    } else if (force) {
      this._set.add(c);
    } else {
      this._set.delete(c);
    }
  }
  contains(c) { return this._set.has(c); }
}

class MockElement {
  constructor(id = "", tag = "div") {
    this.id = id;
    this.tagName = tag.toUpperCase();
    this.style = {};
    this.dataset = {};
    this.className = "";
    this.classList = new MockClassList();
    this.listeners = {};
    this.value = "";
    this.checked = false;
    this.disabled = false;
    this.href = "";
    this.src = "";
    this.title = "";
    this._innerHTML = "";
    this._textContent = "";
    this.children = [];
    this.elements = Object.assign([], { namedItem: () => null });
  }
  reset() {}
  set textContent(val) {
    const s = String(val ?? "");
    this._textContent = s;
    // Mirror REAL browser div.textContent -> div.innerHTML behavior:
    // browsers escape &, <, > ONLY — NOT double or single quotes!
    this._innerHTML = s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  get textContent() { return this._textContent; }
  set innerHTML(val) { this._innerHTML = String(val ?? ""); }
  get innerHTML() { return this._innerHTML; }
  addEventListener(type, fn) {
    if (!this.listeners[type]) this.listeners[type] = [];
    this.listeners[type].push(fn);
  }
  async dispatch(type, evt = {}) {
    const fns = this.listeners[type] || [];
    const e = { preventDefault() {}, stopPropagation() {}, target: this, currentTarget: this, ...evt };
    for (const fn of fns) {
      await fn(e);
    }
  }
  querySelectorAll() { return []; }
  querySelector() { return null; }
  appendChild(child) { this.children.push(child); return child; }
  closest() { return null; }
  focus() {}
  select() {}
}

const elements = new Map();
function getEl(id) {
  if (!elements.has(id)) {
    elements.set(id, new MockElement(id));
  }
  return elements.get(id);
}

const fetchCalls = [];
const routes = config.routes || {};

async function mockFetch(url, init = {}) {
  const method = (init.method || "GET").toUpperCase();
  const normalizedHeaders = {};
  if (init.headers) {
    for (const [k, v] of Object.entries(init.headers)) {
      normalizedHeaders[k.toLowerCase()] = String(v);
    }
  }
  fetchCalls.push({
    url: String(url),
    method,
    headers: normalizedHeaders,
    body: init.body !== undefined ? String(init.body) : null,
  });

  // Match route by clean path (supports both direct /api/... and proxy.cgi?p=%2Fapi%2F...)
  let matchKey = String(url);
  const pMatch = matchKey.match(/[?&]p=([^&]+)/);
  if (pMatch) {
    matchKey = decodeURIComponent(pMatch[1]);
  }
  matchKey = matchKey.split("?")[0];
  if (!matchKey.startsWith("/")) matchKey = "/" + matchKey;

  const entry = routes[`${method} ${matchKey}`] || routes[matchKey] || { status: 200, json: { ok: true } };
  const status = entry.status ?? 200;
  const payload = entry.json ?? { ok: true };
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() { return payload; },
    async text() { return JSON.stringify(payload); },
  };
}

const storage = new Map(Object.entries(config.localStorage || {}));
const pathname = config.pathname || "/";
const search = config.search || "";

const sandbox = {
  console,
  URLSearchParams,
  encodeURIComponent,
  decodeURIComponent,
  Promise,
  Set,
  Map,
  Object,
  Array,
  String,
  Number,
  Boolean,
  Date,
  Math,
  JSON,
  Error,
  fetch: mockFetch,
  setInterval: () => 1,
  clearInterval: () => {},
  setTimeout: (fn) => { return 1; },
  clearTimeout: () => {},
  confirm: () => true,
  prompt: () => "",
  alert: () => {},
  navigator: { clipboard: { writeText: async () => {} } },
  localStorage: {
    getItem: (k) => (storage.has(k) ? storage.get(k) : null),
    setItem: (k, v) => storage.set(k, String(v)),
    removeItem: (k) => storage.delete(k),
  },
  location: {
    pathname,
    search,
    href: `https://mynas.local:5001${pathname}${search}`,
    origin: "https://mynas.local:5001",
    reload: () => {},
  },
  history: { replaceState: () => {} },
  document: {
    getElementById: (id) => getEl(id),
    querySelectorAll: () => [],
    querySelector: () => null,
    createElement: (tag) => new MockElement("", tag),
    addEventListener: () => {},
    body: new MockElement("body", "body"),
  },
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

(async () => {
  vm.runInNewContext(code, sandbox, { filename: config.appJsPath });
  // Flush microtask queue so startup async init completes
  for (let i = 0; i < 20; i++) {
    await new Promise((r) => setImmediate(r));
  }
  for (const action of (config.actions || [])) {
    if (action.type === "setValue") {
      getEl(action.id).value = action.value;
    } else if (action.type === "click") {
      await getEl(action.id).dispatch("click");
    } else if (action.type === "submit") {
      await getEl(action.id).dispatch("submit");
    }
    for (let i = 0; i < 10; i++) {
      await new Promise((r) => setImmediate(r));
    }
  }
  const elementsOut = {};
  for (const id of (config.inspectIds || [])) {
    const el = getEl(id);
    elementsOut[id] = {
      innerHTML: el.innerHTML,
      textContent: el.textContent,
      disabled: el.disabled,
      hidden: el.classList.contains("hidden"),
      styleDisplay: el.style.display || "",
    };
  }
  process.stdout.write(JSON.stringify({ fetchCalls, elements: elementsOut }));
})().catch((err) => {
  process.stderr.write(String(err && err.stack ? err.stack : err));
  process.exit(1);
});
"""


def run_app_js_scenario(
    *,
    pathname: str = "/",
    search: str = "",
    routes: dict[str, dict[str, Any]] | None = None,
    actions: list[dict[str, Any]] | None = None,
    inspect_ids: list[str] | None = None,
    local_storage: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Execute real `app.js` in Node with a browser-faithful DOM/fetch harness."""
    default_routes: dict[str, dict[str, Any]] = {
        "/api/auth/status": {
            "status": 200,
            "json": {"authenticated": True, "via_proxy": pathname.endswith(".cgi"), "is_admin": True},
        },
        "/api/profiles": {
            "status": 200,
            "json": {
                "profiles": [
                    {
                        "id": "default",
                        "display_name": "Shared Library",
                        "dsm_username": "",
                        "target_space": "shared",
                        "photos_dir": "/volume1/photo",
                        "enabled": True,
                        "is_default": True,
                        "inbox_dir": "/var/inbox",
                        "stats": {"total_media": 0},
                    }
                ],
                "current_dsm_user": "admin",
                "dsm_user": "admin",
                "is_admin": True,
                "recommended_profile_id": "default",
                "homes_mounted": False,
                "discovered_synology_users": [],
            },
        },
        "/api/settings": {
            "status": 200,
            "json": {"settings": {"dedup_mode": "perceptual", "synology_url": "https://127.0.0.1:5001"}},
        },
        "/api/status": {
            "status": 200,
            "json": {
                "profile": {"id": "default", "display_name": "Shared Library"},
                "progress": {"state": "idle", "current_step": "Idle", "percent": 0},
                "stats": {
                    "total_media": 5,
                    "total_bytes": 1048576,
                    "exif_repaired": 3,
                    "total_duplicates_skipped": 1,
                    "existing_nas_indexed": 2,
                    "existing_nas_media": 2,
                    "takeout_media": 3,
                    "completed_archives": 1,
                    "failed_archives": 0,
                    "total_albums": 0,
                    "albums_total": 0,
                    "albums_synced": 0,
                    "synced_album_items": 0,
                    "pending_album_items": 0,
                    "quarantined_count": 0,
                    "quarantined_upgrades": 0,
                    "quarantined_bytes": 0,
                },
                "worker_running": True,
                "worker_paused": False,
                "gdrive_oauth_configured": False,
                "gdrive_connected": False,
                "exiftool_available": False,
                "synology_api_enabled": False,
                "photos_dir_writable": True,
                "disk": {"work_free_gb": 50.0, "photos_free_gb": 500.0},
            },
        },
        "/api/archives": {"status": 200, "json": {"archives": []}},
        "/api/albums": {"status": 200, "json": {"albums": []}},
        "/api/logs": {"status": 200, "json": {"logs": []}},
        "/api/upgrades": {"status": 200, "json": {"upgrades": []}},
        "/api/updates": {
            "status": 200,
            "json": {
                "current_version": "1.2.0",
                "latest_version": "1.2.0",
                "update_available": False,
                "auto_updater_configured": False,
            },
        },
    }
    if routes:
        default_routes.update(routes)

    payload = {
        "appJsPath": str(APP_JS_PATH),
        "pathname": pathname,
        "search": search,
        "routes": default_routes,
        "actions": actions or [],
        "inspectIds": inspect_ids or [],
        "localStorage": local_storage or {},
    }
    proc = subprocess.run(
        ["node", "-e", _NODE_APP_JS_HARNESS],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"Node app.js harness failed (rc={proc.returncode}):\nSTDERR:\n{proc.stderr}\nSTDOUT:\n{proc.stdout}"
        )
    return json.loads(proc.stdout)


def make_github_mock_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.Client:
    """Create an `httpx.Client` backed by `httpx.MockTransport` for `UpdateService`."""
    return httpx.Client(transport=httpx.MockTransport(handler))

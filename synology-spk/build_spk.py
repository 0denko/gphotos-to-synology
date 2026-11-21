#!/usr/bin/env python3
"""Build a self-contained native Synology DSM 7 .spk package.

When built with `--bundle-dir <pyinstaller_onedir>`, the resulting .spk includes
the entire Python 3.12 + FastAPI + Pillow + Google Drive runtime as a standalone
native daemon managed directly by Synology DSM Package Center — ZERO Docker,
ZERO Container Manager, ZERO YAML, ZERO SSH.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import stat
import tarfile
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DIST_DIR = PROJECT_ROOT / "dist"
VERSION = "1.1.0-0001"
PKG_NAME = "gphotos-migrator"

DSM_ARCH_MAP: dict[str, str] = {
    "noarch": "noarch",
    "x86_64": (
        "x86_64 apollolake avoton braswell broadwell broadwellnk broadwellntbap "
        "bromolow cedarview denverton epyc7002 geminilake grantley kvmx64 purley r1000 v1000"
    ),
    "arm64": "aarch64 armada37xx armv8 rtd1296 rtd1619b",
}


def _render_icon(size: int) -> bytes:
    """Generate a crisp modern rounded-square app icon for DSM Package Center & desktop."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    pad = max(1, size // 16)
    radius = max(3, size // 4)
    for y in range(pad, size - pad):
        t = (y - pad) / max(1, (size - 2 * pad))
        r = int(29 + (14 - 29) * t)
        g = int(78 + (165 - 78) * t)
        b = int(216 + (233 - 216) * t)
        draw.line([(pad, y), (size - pad, y)], fill=(r, g, b, 255))

    mask = Image.new("L", (size, size), 0)
    mdraw = ImageDraw.Draw(mask)
    mdraw.rounded_rectangle([pad, pad, size - pad, size - pad], radius=radius, fill=255)
    rounded = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    rounded.paste(img, (0, 0), mask)
    rdraw = ImageDraw.Draw(rounded)

    stroke = max(2, size // 18)
    m = int(size * 0.24)
    rdraw.rounded_rectangle(
        [m, m, size - m, size - m],
        radius=max(2, size // 12),
        outline=(255, 255, 255, 240),
        width=stroke,
    )
    rdraw.polygon(
        [
            (int(size * 0.30), int(size * 0.68)),
            (int(size * 0.46), int(size * 0.48)),
            (int(size * 0.58), int(size * 0.60)),
            (int(size * 0.65), int(size * 0.52)),
            (int(size * 0.72), int(size * 0.68)),
        ],
        fill=(255, 255, 255, 230),
    )
    sun_r = max(2, size // 14)
    sx, sy = int(size * 0.62), int(size * 0.38)
    rdraw.ellipse([sx - sun_r, sy - sun_r, sx + sun_r, sy + sun_r], fill=(253, 224, 71, 255))

    buf = io.BytesIO()
    rounded.save(buf, format="PNG")
    return buf.getvalue()


def _write_exec(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def _start_stop_script(*, standalone: bool, build_sha: str, build_date: str) -> str:
    if not standalone:
        return """#!/bin/sh
case "$1" in
    start|stop|status|log) exit 0 ;;
    *) exit 0 ;;
esac
"""
    return f"""#!/bin/sh
# Native DSM 7 start-stop-status script for Google Photos Migrator (standalone binary).

PKG="{PKG_NAME}"
PKG_DIR="${{SYNOPKG_PKGDEST:-/var/packages/${{PKG}}/target}}"
PKG_VAR="${{SYNOPKG_PKGVAR:-/var/packages/${{PKG}}/var}}"
BIN="${{PKG_DIR}}/app/gphotos-migrator"
PID_FILE="${{PKG_VAR}}/gphotos-migrator.pid"
LOG_FILE="${{PKG_VAR}}/gphotos-migrator.log"

detect_dir() {{
    folder="$1"
    for vol in /volume1 /volume2 /volume3 /volume4; do
        if [ -d "${{vol}}/${{folder}}" ]; then
            echo "${{vol}}/${{folder}}"
            return 0
        fi
    done
    echo "/volume1/${{folder}}"
}}

is_running() {{
    if [ -f "${{PID_FILE}}" ]; then
        pid="$(cat "${{PID_FILE}}" 2>/dev/null)"
        if [ -n "${{pid}}" ] && kill -0 "${{pid}}" 2>/dev/null; then
            return 0
        fi
    fi
    return 1
}}

start_daemon() {{
    if is_running; then
        return 0
    fi
    mkdir -p "${{PKG_VAR}}/config" "${{PKG_VAR}}/work" "${{PKG_VAR}}/inbox" 2>/dev/null || true

    export HOME="${{PKG_VAR}}"
    export TMPDIR="${{PKG_VAR}}/work"
    export DATA_DIR="${{PKG_VAR}}/config"
    export WORK_DIR="${{PKG_VAR}}/work"
    export INBOX_DIR="${{PKG_VAR}}/inbox"
    export PHOTOS_DIR="$(detect_dir photo)"
    export HOMES_DIR="$(detect_dir homes)"
    export APP_HOST="127.0.0.1"
    export APP_PORT="8765"
    export APP_BUILD_SHA="{build_sha}"
    export APP_BUILD_DATE="{build_date}"

    nohup "${{BIN}}" >> "${{LOG_FILE}}" 2>&1 &
    echo $! > "${{PID_FILE}}"

    # Wait briefly for process to stay alive
    sleep 1
    if is_running; then
        return 0
    fi
    return 1
}}

stop_daemon() {{
    if ! is_running; then
        rm -f "${{PID_FILE}}"
        return 0
    fi
    pid="$(cat "${{PID_FILE}}")"
    kill "${{pid}}" 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        if ! kill -0 "${{pid}}" 2>/dev/null; then
            break
        fi
        sleep 1
    done
    kill -9 "${{pid}}" 2>/dev/null || true
    rm -f "${{PID_FILE}}"
    return 0
}}

case "$1" in
    start)
        start_daemon
        ;;
    stop)
        stop_daemon
        ;;
    status)
        if is_running; then
            exit 0
        fi
        exit 3
        ;;
    log)
        echo "${{LOG_FILE}}"
        exit 0
        ;;
    *)
        exit 0
        ;;
esac
"""


def build_spk(
    *,
    arch_key: str = "noarch",
    bundle_dir: Path | None = None,
    output_path: Path | None = None,
    build_sha: str = "",
    build_date: str = "",
) -> Path:
    if arch_key not in DSM_ARCH_MAP:
        raise ValueError(f"Unsupported arch_key '{arch_key}'. Expected one of {list(DSM_ARCH_MAP)}")

    standalone = bundle_dir is not None
    sha = build_sha or os.environ.get("APP_BUILD_SHA", "dev")
    bdate = build_date or os.environ.get("APP_BUILD_DATE") or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    DIST_DIR.mkdir(parents=True, exist_ok=True)
    suffix = "" if arch_key == "noarch" else f"-{arch_key}"
    out_spk = output_path or (DIST_DIR / f"{PKG_NAME}-dsm7{suffix}.spk")

    stage = PROJECT_ROOT / "build" / f"spk_stage_{arch_key}"
    if stage.exists():
        shutil.rmtree(stage)
    payload = stage / "payload"
    ui_dst = payload / "ui"
    shutil.copytree(ROOT / "ui", ui_dst)
    (ui_dst / "proxy.cgi").chmod(0o755)

    # Embed standalone PyInstaller --onedir bundle when building native self-contained SPK
    if bundle_dir is not None:
        if not bundle_dir.is_dir():
            raise FileNotFoundError(f"PyInstaller bundle dir not found: {bundle_dir}")
        app_dst = payload / "app"
        shutil.copytree(bundle_dir, app_dst, symlinks=True)
        binary_path = app_dst / "gphotos-migrator"
        if binary_path.exists():
            binary_path.chmod(0o755)

    # Render icons for DSM desktop (16, 24, 32, 48, 64, 72, 256) and Package Center
    images_dir = ui_dst / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    for sz in (16, 24, 32, 48, 64, 72, 256):
        (images_dir / f"icon_{sz}.png").write_bytes(_render_icon(sz))

    (stage / "PACKAGE_ICON.PNG").write_bytes(_render_icon(64))
    (stage / "PACKAGE_ICON_256.PNG").write_bytes(_render_icon(256))

    def _normalize_tarinfo(ti: tarfile.TarInfo) -> tarfile.TarInfo:
        ti.uid = 0
        ti.gid = 0
        ti.uname = "root"
        ti.gname = "root"
        if ti.isfile() and (ti.mode & stat.S_IXUSR):
            ti.mode = 0o755
        elif ti.isfile():
            ti.mode = 0o644
        elif ti.isdir():
            ti.mode = 0o755
        return ti

    # Build package.tgz (extracts to /var/packages/gphotos-migrator/target)
    pkg_tgz = stage / "package.tgz"
    with tarfile.open(pkg_tgz, "w:gz") as tf:
        for item in sorted(payload.rglob("*")):
            rel = item.relative_to(payload)
            tf.add(item, arcname=str(rel), recursive=False, filter=_normalize_tarinfo)

    total_bytes = sum(p.stat().st_size for p in payload.rglob("*") if p.is_file() and not p.is_symlink())
    extract_kb = max(64, (total_bytes + 1023) // 1024)

    desc = (
        "All-in-one native Synology DSM 7 app for unattended Google Photos to Synology Photos migration "
        "(zero Docker or YAML required). Includes multi-user picker, EXIF repair, visual deduplication, "
        "and automatic clarity/resolution upgrade."
        if standalone
        else "Native Synology DSM desktop window for Google Photos Migrator."
    )

    info_text = "\n".join(
        [
            f'package="{PKG_NAME}"',
            f'version="{VERSION}"',
            'displayname="Google Photos Migrator"',
            f'description="{desc}"',
            f'arch="{DSM_ARCH_MAP[arch_key]}"',
            'maintainer="0denko"',
            'maintainer_url="https://github.com/0denko/gphotos-to-synology"',
            'distributor="0denko"',
            'os_min_ver="7.0-40000"',
            'thirdparty="yes"',
            'startable="yes"',
            'ctl_stop="yes"',
            'silent_install="yes"',
            'silent_upgrade="yes"',
            'silent_uninstall="yes"',
            'support_conf_folder="yes"',
            'dsmuidir="ui"',
            'dsmappname="SYNO.SDS.GPhotosMigrator.Application"',
            f'extractsize="{extract_kb}"',
            "",
        ]
    )
    (stage / "INFO").write_text(info_text, encoding="utf-8", newline="\n")

    conf_dir = stage / "conf"
    conf_dir.mkdir(parents=True, exist_ok=True)
    privilege = {
        "defaults": {"run-as": "package"},
        "username": PKG_NAME,
    }
    (conf_dir / "privilege").write_text(json.dumps(privilege, indent=2) + "\n", encoding="utf-8", newline="\n")

    scripts_dir = stage / "scripts"
    noop_script = "#!/bin/sh\nexit 0\n"
    for name in ("preinst", "postinst", "preuninst", "postuninst", "preupgrade", "postupgrade"):
        _write_exec(scripts_dir / name, noop_script)

    _write_exec(
        scripts_dir / "start-stop-status",
        _start_stop_script(standalone=standalone, build_sha=sha, build_date=bdate),
    )

    with tarfile.open(out_spk, "w") as spk:
        for top_name in (
            "INFO",
            "package.tgz",
            "conf",
            "scripts",
            "PACKAGE_ICON.PNG",
            "PACKAGE_ICON_256.PNG",
        ):
            target = stage / top_name
            spk.add(target, arcname=top_name, filter=_normalize_tarinfo)

    shutil.rmtree(stage, ignore_errors=True)
    return out_spk


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Synology DSM 7 SPK package")
    parser.add_argument("--arch", choices=list(DSM_ARCH_MAP), default="noarch")
    parser.add_argument("--bundle-dir", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--build-sha", default="")
    parser.add_argument("--build-date", default="")
    args = parser.parse_args()

    built = build_spk(
        arch_key=args.arch,
        bundle_dir=args.bundle_dir,
        output_path=args.output,
        build_sha=args.build_sha,
        build_date=args.build_date,
    )
    print(f"Built Synology DSM 7 SPK: {built} ({built.stat().st_size / (1024 * 1024):.2f} MB)")


if __name__ == "__main__":
    main()

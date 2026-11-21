#!/usr/bin/env python3
"""Build a DSM 7 compliant noarch Synology .spk package (`gphotos-migrator-dsm7.spk`).

Installing this .spk via Synology Package Center -> Manual Install adds a native
'Google Photos Migrator' icon to DSM Main Menu and opens the full dashboard inside
a draggable Synology DSM desktop window over DSM's own HTTPS origin.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import stat
import tarfile
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
DIST_DIR = PROJECT_ROOT / "dist"
VERSION = "1.0.0-0001"
PKG_NAME = "gphotos-migrator"


def _render_icon(size: int) -> bytes:
    """Generate a crisp modern rounded-square app icon for DSM Package Center & desktop."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    pad = max(1, size // 16)
    radius = max(3, size // 4)
    # Gradient background approximation (deep indigo -> sky blue)
    for y in range(pad, size - pad):
        t = (y - pad) / max(1, (size - 2 * pad))
        r = int(29 + (14 - 29) * t)
        g = int(78 + (165 - 78) * t)
        b = int(216 + (233 - 216) * t)
        draw.line([(pad, y), (size - pad, y)], fill=(r, g, b, 255))

    # Rounded mask
    mask = Image.new("L", (size, size), 0)
    mdraw = ImageDraw.Draw(mask)
    mdraw.rounded_rectangle([pad, pad, size - pad, size - pad], radius=radius, fill=255)
    rounded = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    rounded.paste(img, (0, 0), mask)
    rdraw = ImageDraw.Draw(rounded)

    # Stylized photo frame + sync arrow motif
    stroke = max(2, size // 18)
    m = int(size * 0.24)
    rdraw.rounded_rectangle(
        [m, m, size - m, size - m],
        radius=max(2, size // 12),
        outline=(255, 255, 255, 240),
        width=stroke,
    )
    # Mountain silhouette inside photo frame
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
    # Sun dot
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


def build_spk(output_path: Path | None = None) -> Path:
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    out_spk = output_path or (DIST_DIR / f"{PKG_NAME}-dsm7.spk")

    stage = PROJECT_ROOT / "build" / "spk_stage"
    if stage.exists():
        shutil.rmtree(stage)
    payload = stage / "payload"
    ui_dst = payload / "ui"
    shutil.copytree(ROOT / "ui", ui_dst)
    (ui_dst / "proxy.cgi").chmod(0o755)

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

    # Estimate extracted size in KB
    total_bytes = sum(p.stat().st_size for p in payload.rglob("*") if p.is_file())
    extract_kb = max(64, (total_bytes + 1023) // 1024)

    # DSM 7 INFO metadata
    info_text = "\n".join(
        [
            f'package="{PKG_NAME}"',
            f'version="{VERSION}"',
            'displayname="Google Photos Migrator"',
            'description="Native Synology DSM desktop window for unattended Google Photos to Synology Photos migration with EXIF repair, visual deduplication, and quality auto-upgrade."',
            'arch="noarch"',
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

    # DSM 7 privilege spec (must run-as package, never root)
    conf_dir = stage / "conf"
    conf_dir.mkdir(parents=True, exist_ok=True)
    privilege = {
        "defaults": {"run-as": "package"},
        "username": PKG_NAME,
    }
    (conf_dir / "privilege").write_text(json.dumps(privilege, indent=2) + "\n", encoding="utf-8", newline="\n")

    # DSM 7 lifecycle scripts
    scripts_dir = stage / "scripts"
    noop_script = "#!/bin/sh\nexit 0\n"
    for name in ("preinst", "postinst", "preuninst", "postuninst", "preupgrade", "postupgrade"):
        _write_exec(scripts_dir / name, noop_script)

    start_stop = """#!/bin/sh
case "$1" in
    start)
        exit 0
        ;;
    stop)
        exit 0
        ;;
    status)
        exit 0
        ;;
    log)
        exit 0
        ;;
    *)
        exit 0
        ;;
esac
"""
    _write_exec(scripts_dir / "start-stop-status", start_stop)

    # Pack outer .spk (uncompressed tar — standard Synology SPK format)
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


if __name__ == "__main__":
    built = build_spk()
    print(f"Built Synology DSM 7 SPK: {built} ({built.stat().st_size} bytes)")

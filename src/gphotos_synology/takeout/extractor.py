"""Safe archive extraction (ZIP and TGZ/TAR.GZ) with ZipSlip protection and batch grouping."""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import tarfile
import zipfile
from pathlib import Path

SUPPORTED_ARCHIVE_SUFFIXES: tuple[str, ...] = (".zip", ".tgz", ".tar.gz")

TAKEOUT_BATCH_RE = re.compile(r"^(takeout-\d{8}T\d{6}Z)", re.IGNORECASE)


def is_supported_archive(filename: str) -> bool:
    lower = filename.lower()
    return any(lower.endswith(suffix) for suffix in SUPPORTED_ARCHIVE_SUFFIXES)


def extract_batch_key(filename: str) -> str:
    """Group multi-part Takeout archives (e.g. takeout-20261121T000000Z-001.zip) by batch key."""
    match = TAKEOUT_BATCH_RE.match(filename)
    if match:
        return match.group(1)
    return Path(filename).stem


def get_free_disk_gb(path: Path) -> float:
    path.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(path)
    return usage.free / (1024**3)


def _ensure_within(base_dir: Path, target_path: Path) -> Path:
    resolved_base = base_dir.resolve()
    resolved_target = target_path.resolve()
    if resolved_base != resolved_target and resolved_base not in resolved_target.parents:
        raise ValueError(f"Unsafe archive entry path traversal detected: {target_path}")
    return resolved_target


def extract_archive(archive_path: Path, dest_dir: Path) -> Path:
    """Safely extract a .zip, .tgz, or .tar.gz archive into `dest_dir`."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    lower = archive_path.name.lower()

    if lower.endswith(".zip"):
        with zipfile.ZipFile(archive_path, "r") as zf:
            for member in zf.infolist():
                if member.is_dir():
                    continue
                target = _ensure_within(dest_dir, dest_dir / member.filename)
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member, "r") as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
        return dest_dir

    if lower.endswith((".tgz", ".tar.gz")):
        with tarfile.open(archive_path, "r:gz") as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                target = _ensure_within(dest_dir, dest_dir / member.name)
                target.parent.mkdir(parents=True, exist_ok=True)
                extracted = tf.extractfile(member)
                if extracted is None:
                    continue
                with extracted, open(target, "wb") as dst:
                    shutil.copyfileobj(extracted, dst, length=1024 * 1024)
        return dest_dir

    raise ValueError(f"Unsupported archive format: {archive_path.name}")


def cleanup_directory(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def compute_file_sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def safe_move_or_copy(src: Path, dst: Path) -> None:
    """Atomically move `src` to `dst`, falling back to temp-copy+replace across filesystems."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dst)
        return
    except OSError:
        pass

    tmp_dst = dst.with_name(f".{dst.name}.tmp.{os.getpid()}")
    try:
        shutil.copy2(src, tmp_dst)
        os.replace(tmp_dst, dst)
        with contextlib.suppress(OSError):
            src.unlink(missing_ok=True)
    except Exception:
        tmp_dst.unlink(missing_ok=True)
        raise

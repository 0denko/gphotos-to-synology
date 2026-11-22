"""Safe archive extraction (ZIP and TGZ/TAR.GZ) with ZipSlip and decompression-bomb protection."""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import shutil
import tarfile
import time
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

SUPPORTED_ARCHIVE_SUFFIXES: tuple[str, ...] = (".zip", ".tgz", ".tar.gz")

TAKEOUT_BATCH_RE = re.compile(r"^(takeout-\d{8}T\d{6}Z)", re.IGNORECASE)

DEFAULT_MAX_ENTRIES = 100_000
DEFAULT_MAX_COMPRESSION_RATIO = 100.0
_MIN_RATIO_BASE_BYTES = 64 * 1024  # Avoid false ratio trips on tiny archives


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
    target = path
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        while not target.exists() and target != target.parent:
            target = target.parent
    usage = shutil.disk_usage(target)
    return usage.free / (1024**3)


def _ensure_within(base_dir: Path, target_path: Path) -> Path:
    resolved_base = base_dir.resolve()
    resolved_target = target_path.resolve()
    if resolved_base != resolved_target and resolved_base not in resolved_target.parents:
        raise ValueError(f"Unsafe archive entry path traversal detected: {target_path}")
    return resolved_target


def _copy_stream_limited(
    src,
    dst,
    *,
    written_so_far: int,
    max_total_bytes: int | None,
    chunk_size: int = 1024 * 1024,
) -> int:
    total = written_so_far
    while True:
        chunk = src.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if max_total_bytes is not None and total > max_total_bytes:
            raise ValueError(
                f"Archive extraction exceeded uncompressed byte limit ({max_total_bytes} bytes)"
            )
        dst.write(chunk)
    return total


def _check_ratio(uncompressed_bytes: int, archive_size: int, max_ratio: float) -> None:
    if max_ratio <= 0:
        return
    base = max(archive_size, _MIN_RATIO_BASE_BYTES)
    ratio = uncompressed_bytes / base
    if ratio > max_ratio:
        raise ValueError(
            f"Archive compression ratio ({ratio:.1f}x) exceeds safety limit ({max_ratio:.1f}x)"
        )


def extract_archive(
    archive_path: Path,
    dest_dir: Path,
    *,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    max_uncompressed_bytes: int | None = None,
    max_compression_ratio: float = DEFAULT_MAX_COMPRESSION_RATIO,
    skip_unsafe_entries: bool = False,
) -> Path:
    """Safely extract a .zip, .tgz, or .tar.gz archive into `dest_dir`.

    Enforces ZipSlip path containment, skips symlinks/devices, and applies entry count,
    total uncompressed byte, streaming byte, and compression-ratio limits.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    lower = archive_path.name.lower()
    archive_size = archive_path.stat().st_size if archive_path.exists() else 0

    if lower.endswith(".zip"):
        with zipfile.ZipFile(archive_path, "r") as zf:
            members = zf.infolist()
            file_members = [m for m in members if not m.is_dir()]
            if len(file_members) > max_entries:
                raise ValueError(
                    f"Archive entry count ({len(file_members)}) exceeds limit ({max_entries})"
                )
            declared_bytes = sum(max(0, m.file_size) for m in file_members)
            if max_uncompressed_bytes is not None and declared_bytes > max_uncompressed_bytes:
                raise ValueError(
                    f"Archive uncompressed size ({declared_bytes} bytes) exceeds limit "
                    f"({max_uncompressed_bytes} bytes)"
                )
            _check_ratio(declared_bytes, archive_size, max_compression_ratio)

            written_bytes = 0
            for member in file_members:
                # Skip ZIP symlinks (POSIX mode S_IFLNK = 0o120000 in external_attr)
                unix_mode = (member.external_attr >> 16) & 0o170000
                if unix_mode == 0o120000:
                    logger.warning("Skipping symlink entry in ZIP: %s", member.filename)
                    continue
                try:
                    if member.filename.startswith(("/", "\\")):
                        raise ValueError(f"Unsafe absolute archive path: {member.filename}")
                    target = _ensure_within(dest_dir, dest_dir / member.filename)
                except ValueError:
                    if skip_unsafe_entries:
                        logger.warning("Skipping unsafe ZIP entry: %s", member.filename)
                        continue
                    raise
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member, "r") as src, open(target, "wb") as dst:
                    written_bytes = _copy_stream_limited(
                        src,
                        dst,
                        written_so_far=written_bytes,
                        max_total_bytes=max_uncompressed_bytes,
                    )
                _check_ratio(written_bytes, archive_size, max_compression_ratio)
        return dest_dir

    if lower.endswith((".tgz", ".tar.gz")):
        with tarfile.open(archive_path, "r:gz") as tf:
            members = tf.getmembers()
            file_members = [m for m in members if m.isfile()]
            if len(file_members) > max_entries:
                raise ValueError(
                    f"Archive entry count ({len(file_members)}) exceeds limit ({max_entries})"
                )
            declared_bytes = sum(max(0, m.size) for m in file_members)
            if max_uncompressed_bytes is not None and declared_bytes > max_uncompressed_bytes:
                raise ValueError(
                    f"Archive uncompressed size ({declared_bytes} bytes) exceeds limit "
                    f"({max_uncompressed_bytes} bytes)"
                )
            _check_ratio(declared_bytes, archive_size, max_compression_ratio)

            written_bytes = 0
            for member in file_members:
                try:
                    if member.name.startswith(("/", "\\")):
                        raise ValueError(f"Unsafe absolute archive path: {member.name}")
                    target = _ensure_within(dest_dir, dest_dir / member.name)
                except ValueError:
                    if skip_unsafe_entries:
                        logger.warning("Skipping unsafe TAR entry: %s", member.name)
                        continue
                    raise
                target.parent.mkdir(parents=True, exist_ok=True)
                extracted = tf.extractfile(member)
                if extracted is None:
                    continue
                with extracted, open(target, "wb") as dst:
                    written_bytes = _copy_stream_limited(
                        extracted,
                        dst,
                        written_so_far=written_bytes,
                        max_total_bytes=max_uncompressed_bytes,
                    )
                _check_ratio(written_bytes, archive_size, max_compression_ratio)
        return dest_dir

    raise ValueError(f"Unsupported archive format: {archive_path.name}")


def cleanup_directory(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def cleanup_stale_temp_files(directory: Path, max_age_seconds: float = 0.0) -> int:
    """Remove orphaned `.tmp.<pid>` staging files left behind by interrupted moves."""
    if not directory.is_dir():
        return 0
    now = time.time()
    removed = 0
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in {"@eaDir", "#recycle", "#snapshot"}]
        for name in files:
            if ".tmp." not in name or not name.startswith("."):
                continue
            path = Path(root) / name
            try:
                if max_age_seconds > 0 and (now - path.stat().st_mtime) < max_age_seconds:
                    continue
                path.unlink(missing_ok=True)
                removed += 1
            except OSError:
                continue
    return removed


def compute_file_sha256(path: Path) -> str:
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

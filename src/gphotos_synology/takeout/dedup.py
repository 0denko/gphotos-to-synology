"""Multi-tier media fingerprinting, visual similarity matching, and image clarity/resolution scoring."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps, ImageStat

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_SUPPORTED = True
except Exception:  # pragma: no cover - optional if libheif unavailable
    HEIF_SUPPORTED = False

from gphotos_synology.takeout.extractor import compute_file_sha256
from gphotos_synology.takeout.matcher import is_video_file

logger = logging.getLogger(__name__)

# EXIF tag IDs: DateTimeOriginal (36867), DateTimeDigitized (36868), DateTime (306)
_EXIF_DATE_TAGS = (36867, 36868, 306)

# 3x3 discrete Laplacian kernel for measuring perceptual sharpness / clarity
_LAPLACIAN_KERNEL = ImageFilter.Kernel(
    size=(3, 3),
    kernel=(0, 1, 0, 1, -4, 1, 0, 1, 0),
    scale=1,
    offset=128,
)

LOSSLESS_EXTENSIONS: set[str] = {".png", ".tif", ".tiff", ".bmp", ".dng", ".cr2", ".cr3", ".nef", ".arw"}


@dataclass
class MediaFingerprint:
    """Content hashes, dimensions, clarity metrics, and EXIF capture timestamp."""

    sha256: str
    pixel_sha256: str = ""
    dhash: str = ""  # 128-bit composite (64-bit horizontal dHash + 64-bit vertical vHash, 32 hex chars)
    width: int = 0
    height: int = 0
    sharpness: float = 0.0
    quality_score: float = 0.0
    exif_taken_at: str | None = None
    visual_status: str = ""  # "ok" | "undecodable" | "skipped" | ""


@dataclass
class ImageQualityMetrics:
    """Comparable resolution, sharpness, and compression-fidelity metrics for an image."""

    width: int
    height: int
    file_size: int
    sharpness: float
    filename: str = ""

    @property
    def pixels(self) -> int:
        return max(0, self.width) * max(0, self.height)

    @property
    def is_lossless(self) -> bool:
        return Path(self.filename).suffix.lower() in LOSSLESS_EXTENSIONS

    @property
    def bits_per_pixel(self) -> float:
        if self.pixels <= 0 or self.file_size <= 0:
            return 0.0
        return (self.file_size * 8.0) / self.pixels


@dataclass
class QualityDecision:
    """Result of comparing an incoming duplicate image against an existing NAS image."""

    incoming_wins: bool
    reason: str
    incoming_score: float
    existing_score: float


def _parse_exif_datetime(raw_value: object) -> str | None:
    if not isinstance(raw_value, str):
        return None
    cleaned = raw_value.strip().split("\x00", 1)[0]
    if not cleaned or cleaned.startswith("0000:"):
        return None
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(cleaned, fmt).replace(tzinfo=UTC)
            return dt.isoformat()
        except ValueError:
            continue
    return None


def _compute_composite_dhash_hex(img: Image.Image) -> str:
    """Compute a 128-bit perceptual hash: 64-bit horizontal dHash + 64-bit vertical vHash."""
    gray_base = img.convert("L")

    # 64-bit horizontal gradient hash (9x8)
    h_img = gray_base.resize((9, 8), Image.Resampling.LANCZOS)
    h_pixels = h_img.tobytes()
    h_bits = 0
    for row in range(8):
        offset = row * 9
        for col in range(8):
            h_bits = (h_bits << 1) | (1 if h_pixels[offset + col] > h_pixels[offset + col + 1] else 0)

    # 64-bit vertical gradient hash (8x9)
    v_img = gray_base.resize((8, 9), Image.Resampling.LANCZOS)
    v_pixels = v_img.tobytes()
    v_bits = 0
    for row in range(8):
        top_offset = row * 8
        bot_offset = (row + 1) * 8
        for col in range(8):
            v_bits = (v_bits << 1) | (1 if v_pixels[top_offset + col] > v_pixels[bot_offset + col] else 0)

    return f"{h_bits:016x}{v_bits:016x}"


def _compute_sharpness(rgb_img: Image.Image) -> float:
    """Estimate perceptual clarity via Laplacian edge variance on a normalized grayscale view."""
    gray = rgb_img.convert("L")
    max_dim = max(gray.width, gray.height)
    if max_dim > 512:
        scale = 512.0 / max_dim
        gray = gray.resize(
            (max(8, int(gray.width * scale)), max(8, int(gray.height * scale))),
            Image.Resampling.LANCZOS,
        )
    edges = gray.filter(_LAPLACIAN_KERNEL)
    variance = float(ImageStat.Stat(edges).var[0])
    return round(variance, 2)


def compute_quality_score(metrics: ImageQualityMetrics) -> float:
    """Compute a composite summary score combining resolution, clarity, and compression richness."""
    if metrics.pixels <= 0:
        return 0.0
    megapixels = metrics.pixels / 1_000_000.0
    bpp_bonus = min(metrics.bits_per_pixel, 12.0) * 5.0
    lossless_bonus = 25.0 if metrics.is_lossless else 0.0
    return round(megapixels * 100.0 + min(metrics.sharpness, 2000.0) * 0.25 + bpp_bonus + lossless_bonus, 2)


def compare_image_quality(
    incoming: ImageQualityMetrics,
    existing: ImageQualityMetrics,
) -> QualityDecision:
    """Decide whether `incoming` is demonstrably higher resolution or clearer than `existing`."""
    inc_score = compute_quality_score(incoming)
    ext_score = compute_quality_score(existing)

    if incoming.pixels <= 0 or existing.pixels <= 0:
        return QualityDecision(
            incoming_wins=False,
            reason="Insufficient image dimension metrics to upgrade",
            incoming_score=inc_score,
            existing_score=ext_score,
        )

    # 1. Resolution comparison (>= 10% more pixels is a clear resolution upgrade)
    pixel_ratio = incoming.pixels / existing.pixels
    if pixel_ratio >= 1.10:
        # Guard against a severely blurred upscaled image beating a sharp native image
        if existing.sharpness > 0 and incoming.sharpness > 0 and incoming.sharpness < existing.sharpness * 0.45:
            return QualityDecision(
                incoming_wins=False,
                reason=(
                    f"Existing image is much sharper ({existing.sharpness:.1f} vs {incoming.sharpness:.1f}) "
                    "despite higher incoming pixel dimensions"
                ),
                incoming_score=inc_score,
                existing_score=ext_score,
            )
        return QualityDecision(
            incoming_wins=True,
            reason=(
                f"Higher resolution ({incoming.width}x{incoming.height} vs "
                f"{existing.width}x{existing.height})"
            ),
            incoming_score=inc_score,
            existing_score=ext_score,
        )

    if pixel_ratio <= (1.0 / 1.10):
        return QualityDecision(
            incoming_wins=False,
            reason=(
                f"Existing has higher resolution ({existing.width}x{existing.height} vs "
                f"{incoming.width}x{incoming.height})"
            ),
            incoming_score=inc_score,
            existing_score=ext_score,
        )

    # 2. Comparable resolution: check perceptual clarity / sharpness (>= 12% sharper wins)
    if existing.sharpness > 0 and incoming.sharpness >= existing.sharpness * 1.12:
        return QualityDecision(
            incoming_wins=True,
            reason=f"Better clarity (sharpness {incoming.sharpness:.1f} vs {existing.sharpness:.1f})",
            incoming_score=inc_score,
            existing_score=ext_score,
        )

    if incoming.sharpness > 0 and existing.sharpness >= incoming.sharpness * 1.12:
        return QualityDecision(
            incoming_wins=False,
            reason=f"Existing has better clarity (sharpness {existing.sharpness:.1f} vs {incoming.sharpness:.1f})",
            incoming_score=inc_score,
            existing_score=ext_score,
        )

    # 3. Comparable resolution & sharpness: prefer lossless or richer compression (>= 15% larger)
    #    guarded by non-inferior sharpness so metadata bloat doesn't beat a sharper image.
    if incoming.is_lossless and not existing.is_lossless:
        return QualityDecision(
            incoming_wins=True,
            reason="Lossless format preferred over lossy duplicate at same resolution",
            incoming_score=inc_score,
            existing_score=ext_score,
        )

    non_inferior_sharpness = (
        existing.sharpness <= 0 or incoming.sharpness >= existing.sharpness * 0.95
    )
    if existing.file_size > 0 and incoming.file_size >= existing.file_size * 1.15 and non_inferior_sharpness:
        inc_mb = incoming.file_size / (1024 * 1024)
        ext_mb = existing.file_size / (1024 * 1024)
        return QualityDecision(
            incoming_wins=True,
            reason=f"Higher compression fidelity ({inc_mb:.2f} MB vs {ext_mb:.2f} MB at {incoming.width}x{incoming.height})",
            incoming_score=inc_score,
            existing_score=ext_score,
        )

    return QualityDecision(
        incoming_wins=False,
        reason="Existing image is already equal or higher quality",
        incoming_score=inc_score,
        existing_score=ext_score,
    )


def compute_media_fingerprint(path: Path, *, compute_visual: bool = True) -> MediaFingerprint:
    """Compute SHA-256 plus decoded-pixel SHA-256, 128-bit perceptual hash, and clarity metrics."""
    file_sha256 = compute_file_sha256(path)
    if not compute_visual or is_video_file(path):
        return MediaFingerprint(sha256=file_sha256, visual_status="skipped")

    pixel_sha256 = ""
    dhash = ""
    width = 0
    height = 0
    sharpness = 0.0
    quality_score = 0.0
    exif_taken_at: str | None = None
    visual_status = "undecodable"

    try:
        file_size = path.stat().st_size
        with Image.open(path) as raw_img:
            try:
                exif = raw_img.getexif()
                if exif:
                    exif_ifd = exif.get_ifd(0x8769) or {}
                    for tag in _EXIF_DATE_TAGS:
                        val = exif_ifd.get(tag) or exif.get(tag)
                        parsed = _parse_exif_datetime(val)
                        if parsed:
                            exif_taken_at = parsed
                            break
            except Exception:
                pass

            # Normalize orientation so EXIF-rotated vs physically-rotated copies match cleanly
            img = ImageOps.exif_transpose(raw_img)
            rgb = img.convert("RGB")
            width, height = rgb.width, rgb.height

            # Exact decoded pixel hash (resilient to EXIF/header edits, zero false positives)
            px_hasher = hashlib.sha256()
            px_hasher.update(f"{width}x{height}:".encode())
            px_hasher.update(rgb.tobytes())
            pixel_sha256 = px_hasher.hexdigest()

            # 128-bit composite perceptual hash (horizontal dHash + vertical vHash)
            dhash = _compute_composite_dhash_hex(rgb)

            # Clarity / sharpness metric
            sharpness = _compute_sharpness(rgb)
            quality_score = compute_quality_score(
                ImageQualityMetrics(
                    width=width,
                    height=height,
                    file_size=file_size,
                    sharpness=sharpness,
                    filename=path.name,
                )
            )
            visual_status = "ok"
    except Exception as exc:
        logger.warning("Visual fingerprint unavailable for %s: %s", path.name, exc)
        visual_status = "undecodable"

    return MediaFingerprint(
        sha256=file_sha256,
        pixel_sha256=pixel_sha256,
        dhash=dhash,
        width=width,
        height=height,
        sharpness=sharpness,
        quality_score=quality_score,
        exif_taken_at=exif_taken_at,
        visual_status=visual_status,
    )


def hamming_distance_hex(hash_a: str, hash_b: str) -> int:
    """Return Hamming distance between two perceptual hex hashes (supports 64-bit & 128-bit)."""
    if not hash_a or not hash_b:
        return 128
    # If comparing legacy 16-char (64-bit) with new 32-char (128-bit), compare horizontal 64-bit prefix
    min_len = min(len(hash_a), len(hash_b))
    if min_len not in (16, 32):
        return 128
    try:
        return (int(hash_a[:min_len], 16) ^ int(hash_b[:min_len], 16)).bit_count()
    except ValueError:
        return 128


def _aspect_ratios_compatible(w1: int, h1: int, w2: int, h2: int, max_rel_diff: float = 0.08) -> bool | None:
    """Return True if compatible, False if incompatible, None if any dimension is unknown."""
    if w1 <= 0 or h1 <= 0 or w2 <= 0 or h2 <= 0:
        return None
    r1 = w1 / h1
    r2 = w2 / h2
    return abs(r1 - r2) / max(r1, r2) <= max_rel_diff


def _parse_iso_utc(iso_str: str | None) -> datetime | None:
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except ValueError:
        return None


def _timestamps_close(iso_a: str | None, iso_b: str | None, max_delta_seconds: int = 15) -> bool | None:
    dt_a = _parse_iso_utc(iso_a)
    dt_b = _parse_iso_utc(iso_b)
    if dt_a is None or dt_b is None:
        return None
    return abs((dt_a - dt_b).total_seconds()) <= max_delta_seconds


def is_perceptual_match(
    incoming_dhash: str,
    existing_dhash: str,
    *,
    incoming_taken_at: str | None,
    existing_taken_at: str | None,
    incoming_filename: str,
    existing_filename: str,
    incoming_width: int = 0,
    incoming_height: int = 0,
    existing_width: int = 0,
    existing_height: int = 0,
    max_distance: int = 10,
) -> bool:
    """Guardrail-protected visual similarity match across resolutions and compression levels.

    Uses 128-bit dHash+vHash distance, aspect-ratio compatibility, and capture-time /
    filename corroboration to avoid false positives on burst shots or similar scenes.
    """
    ar_compat = _aspect_ratios_compatible(incoming_width, incoming_height, existing_width, existing_height)
    if ar_compat is False:
        return False
    dims_known = ar_compat is True

    # Scale threshold cleanly if comparing legacy 64-bit hashes vs 128-bit hashes
    effective_len = min(len(incoming_dhash), len(existing_dhash))
    effective_max = max_distance if effective_len == 32 else max(2, max_distance // 2)
    strict_uncorroborated_max = 4 if effective_len == 32 else 2

    dist = hamming_distance_hex(incoming_dhash, existing_dhash)
    if dist > effective_max:
        return False

    same_stem = Path(incoming_filename).stem.lower() == Path(existing_filename).stem.lower()

    # If both files have capture timestamps, confirm they match closely (allowing timezone shifts)
    ts_close = _timestamps_close(incoming_taken_at, existing_taken_at, max_delta_seconds=15)
    if ts_close is True:
        return dims_known or dist <= strict_uncorroborated_max
    if ts_close is False:
        dt_a = _parse_iso_utc(incoming_taken_at)
        dt_b = _parse_iso_utc(existing_taken_at)
        if dt_a is not None and dt_b is not None:
            diff_s = abs((dt_a - dt_b).total_seconds())
            same_minute_sec = (diff_s % 3600) <= 15 or (3600 - (diff_s % 3600)) <= 15
            if diff_s <= 14 * 3600 + 15 and same_minute_sec and dims_known:
                return True
        # Conflicting timestamps: only match if visual distance is near-identical AND dimensions are known
        return dims_known and dist <= max(2, min(strict_uncorroborated_max, effective_max // 3))

    # One or both timestamps missing:
    # - Distance > 4 ALWAYS requires same filename stem AND known compatible dimensions.
    # - Distance <= 4 requires known compatible dimensions OR same filename stem.
    if dist > strict_uncorroborated_max:
        return same_stem and dims_known
    return dims_known or same_stem

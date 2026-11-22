# Explorer M2 R1-2 Handoff — Dedup, HEIC/HEIF, Perceptual Match, Quality Heuristics & ExistingLibraryScanner

**Agent:** `explorer_m2_r1_2`  
**Milestone:** M2 — Takeout, Dedup, Sources & Synology Client (Workstream: G10, G11, Quality Heuristics & Scanner)  
**Target Files:**
- [`src/gphotos_synology/takeout/dedup.py`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/takeout/dedup.py)
- [`src/gphotos_synology/synology/scanner.py`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/synology/scanner.py)
- [`tests/test_existing_nas_dedup.py`](file:///home/antigravity/gphotos-to-synology/tests/test_existing_nas_dedup.py)

---

## 1. Verified Foundation & Cross-File Contracts (M1 & E2E)

### 1.1 M1 Database & Dependency Foundation (Already Landed)
- **Environment & Wheels (`pyproject.toml` & `uv.lock`)**:
  - `pillow==12.2.0` (`"pillow>=11.1.0,<12.3.0"`) and `pillow-heif==1.0.0` (`"pillow-heif>=1.0.0,<1.1.0"`) are installed in `.venv` and locked in `uv.lock`.
  - Empirically verified in `.venv`: `pillow_heif.register_heif_opener()` registers the HEIF/HEIC Pillow plugin so `Image.open(path)` decodes `.heic`/`.heif` natively with `.size`, `.getexif()`, `ImageOps.exif_transpose()`, and `.convert("RGB")`.
- **`StateDB` (`src/gphotos_synology/database.py`)**:
  - `media_items` has column `visual_status TEXT NOT NULL DEFAULT ''` (`'ok' | 'undecodable' | 'skipped' | ''`).
  - [`StateDB.get_indexed_files_by_rel_path()`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L687-L714) returns:
    ```python
    {
        "id": int(r["id"]),
        "file_size": int(r["file_size"]),
        "file_mtime_ns": int(r["file_mtime_ns"]),
        "has_visual": has_visual,
        "visual_status": visual_status,
        "visual_done": has_visual or visual_status in ("ok", "undecodable"),
        "origin": str(r["origin"]),
    }
    ```
  - [`StateDB.upsert_existing_nas_media()`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L716-L832) accepts `visual_status: str = ""` (line 726), persists it on `INSERT` and `UPDATE` (preserving `'ok'`/`'undecodable'` against `'skipped'`), and maintains `media_dhash_buckets`.
  - [`StateDB.find_duplicate_media()`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py#L546-L685) calls `is_perceptual_match(...)` across exact-dhash rows, `media_dhash_buckets` candidates (`perceptual_max_distance < 8`), legacy 16-char rows, and full-scan fallback (`perceptual_max_distance >= 8`).

### 1.2 Verified Test Expectations Across `tests/` and `tests/e2e/`
Every existing unit and E2E test touching `compute_media_fingerprint`, `is_perceptual_match`, `compare_image_quality`, and `ExistingLibraryScanner` was inspected:
1. [`tests/e2e/test_tier1_features.py:385-396`](file:///home/antigravity/gphotos-to-synology/tests/e2e/test_tier1_features.py#L385-L396) (`test_t1_b5_heic_visual_fingerprinting_via_pillow_heif`):
   - Calls `fp = compute_media_fingerprint(heic_path, compute_visual=True)` on a 320×240 `.heic` created via `save_heic`.
   - Asserts `fp.visual_status == "ok"`, `len(fp.pixel_sha256) == 64`, `len(fp.dhash) == 32`, `fp.width == 320`, `fp.height == 240`, `fp.quality_score > 0`.
2. [`tests/e2e/test_tier2_boundaries.py:684-716`](file:///home/antigravity/gphotos-to-synology/tests/e2e/test_tier2_boundaries.py#L684-L716) (`test_t2_g10_undecodable_heic_logs_warning_and_persists_stable_skip`):
   - Writes `b"NOT_A_VALID_HEIF_CONTAINER_BYTES_123456"` to `2024/07/broken.heic`.
   - Calls `compute_media_fingerprint(corrupt_heic, compute_visual=True)` under `caplog.at_level(logging.WARNING)` and asserts `fp.visual_status == "undecodable"` and `any(rec.levelno >= logging.WARNING for rec in caplog.records)`.
   - Runs `ExistingLibraryScanner(settings, db).scan()` twice:
     - Scan 1: `r1.newly_indexed == 1`, `r1.unchanged_skipped == 0`, DB row `visual_status == "undecodable"`.
     - Scan 2: `r2.newly_indexed == 0`, `r2.unchanged_skipped == 1`.
3. [`tests/e2e/test_tier2_boundaries.py:723-897`](file:///home/antigravity/gphotos-to-synology/tests/e2e/test_tier2_boundaries.py#L723-L897) (`test_t2_g11_perceptual_dedup_corroboration_unknown_dims_tz_buckets_and_sharpness`):
   - Case 1 (lines 734-749): `dist=5`, `incoming_taken_at=None`, `existing_taken_at=None`, `IMG_AAA.jpg` vs `IMG_BBB.jpg` (`same_stem=False`), `1000x800` vs `1000x800`, `max_distance=10` → **`False`**.
   - Case 2 (lines 752-767): `dist=5`, `incoming_taken_at=None`, `existing_taken_at=None`, `IMG_SAME.jpg` vs `IMG_SAME.jpg` (`same_stem=True`), `1000x800` vs `1000x800`, `max_distance=10` → **`True`**.
   - Case 3 (lines 770-785): `dist=5`, matching timestamps, `IMG_A.jpg` vs `IMG_B.jpg`, `incoming_width=0, incoming_height=0, existing_width=1000, existing_height=800`, `max_distance=10` → **`False`**.
   - Case 4a (lines 788-803): `dist=5`, `incoming_taken_at="2024-07-03T12:00:00+02:00"`, `existing_taken_at="2024-07-03T10:00:00+00:00"` (0s UTC delta), `IMG_A.jpg` vs `IMG_B.jpg` (`same_stem=False`), `1000x800` vs `1000x800`, `max_distance=10` → **`True`**.
   - Case 4b (lines 805-820): `dist=4`, matching timestamps, `IMG_A.jpg` vs `IMG_B.jpg`, `1000x800` vs `1000x800`, `max_distance=10` → **`True`**.
   - Case 5 (lines 857-888): `StateDB.find_duplicate_media` with `dist=6` (`BASE_copy.jpg` vs `BASE.jpg`, `same_stem=False`, matching timestamp, `1000x800`, `perceptual_max_distance=7` → **`True`**), `dist=8` (`BASE_copy2.jpg` vs `BASE.jpg`, `same_stem=False`, matching timestamp, `1000x800`, `perceptual_max_distance=10` → **`True`**), and legacy 16-char hash `dist=1` (`perceptual_max_distance=4` → **`True`**).
   - Case 6 (lines 891-896): `compare_image_quality(incoming_blurrier_larger, existing_sharp)` where `existing_sharp` has `2000x1500, file_size=500_000, sharpness=80.0` and `incoming_blurrier_larger` has `2000x1500, file_size=700_000, sharpness=68.0` → **`decision.incoming_wins is False`**.
4. [`tests/e2e/test_tier1_features.py:519-564`](file:///home/antigravity/gphotos-to-synology/tests/e2e/test_tier1_features.py#L519-L564) (`test_t1_c2_quality_upgrade_swap_quarantine_and_restore_to_old_rel_path`):
   - `vacation_old.jpg` (`320x240`, no EXIF timestamp → `existing_taken_at=None`) vs `vacation_hires.jpg` (`960x720`, `incoming_taken_at="2024-07-03T09:46:40+00:00"`): `ts_close is None`, `same_stem is False`, empirically verified `dist == 2 <= 4`, known compatible 4:3 dimensions → **`True`**.
5. [`tests/e2e/test_tier3_interactions.py:387-434`](file:///home/antigravity/gphotos-to-synology/tests/e2e/test_tier3_interactions.py#L387-L434) (`test_t3_p5_g10_heic_undecodable_scanner_incremental_skip_and_g11_perceptual_dedup`):
   - Valid `.heic` (`640x480`) + corrupt `.heic` scanned twice (`s1.newly_indexed == 2, s1.unchanged_skipped == 0`; `s2.newly_indexed == 0, s2.unchanged_skipped == 2`), followed by recompressed `.heic` (`480x360`, `dist == 0`, `same_stem=True`) matched via `perceptual_dhash`.
6. [`tests/test_database.py:297-509`](file:///home/antigravity/gphotos-to-synology/tests/test_database.py#L297-L509):
   - Tests `find_duplicate_media` at `dist` = 0, 1, 7, 8 (with `800x600` vs `1600x1200`, matching timestamp, matching filename) and legacy 16-char hash `dist` = 1.

---

## 2. Exact File-and-Line Implementation Blueprint

### 2.1 [`src/gphotos_synology/takeout/dedup.py`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/takeout/dedup.py)

#### Edit 1: Register `pillow_heif` opener after PIL import (lines 11–17)
Replace lines 11–17:
```python
from PIL import Image, ImageFilter, ImageOps, ImageStat

from gphotos_synology.takeout.extractor import compute_file_sha256
from gphotos_synology.takeout.matcher import is_video_file

logger = logging.getLogger(__name__)
```
With:
```python
from PIL import Image, ImageFilter, ImageOps, ImageStat

from gphotos_synology.takeout.extractor import compute_file_sha256
from gphotos_synology.takeout.matcher import is_video_file

logger = logging.getLogger(__name__)

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    _HEIF_OPENER_REGISTERED = True
except Exception:  # pragma: no cover - graceful fallback when pillow-heif is absent
    _HEIF_OPENER_REGISTERED = False
```

#### Edit 2: Add `visual_status: str = ""` to `MediaFingerprint` (lines 32–44)
Replace lines 32–44:
```python
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
    visual_status: str = ""  # 'ok' | 'undecodable' | 'skipped' | ''
```

#### Edit 3: Require non-inferior sharpness on `compare_image_quality` Rule 2 / Rule 3 (lines 207–233)
In [`compare_image_quality`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/takeout/dedup.py#L147-L240), note two details:
1. In Rule 2 (line 207), `if incoming.sharpness > 0 and existing.sharpness >= incoming.sharpness * 1.12:` already rejects when `incoming.sharpness > 0`, and if `incoming.sharpness <= 0 < existing.sharpness` or `incoming.sharpness < existing.sharpness * 0.98`, Rule 3 (line 224) must NOT allow a larger file with worse sharpness to win.
2. Replace lines 224–232:
```python
    if (
        existing.file_size > 0
        and incoming.file_size >= existing.file_size * 1.15
        and (existing.sharpness <= 0 or incoming.sharpness >= existing.sharpness * 0.98)
    ):
        inc_mb = incoming.file_size / (1024 * 1024)
        ext_mb = existing.file_size / (1024 * 1024)
        return QualityDecision(
            incoming_wins=True,
            reason=f"Higher compression fidelity ({inc_mb:.2f} MB vs {ext_mb:.2f} MB at {incoming.width}x{incoming.height})",
            incoming_score=inc_score,
            existing_score=ext_score,
        )
```

#### Edit 4: Populate `visual_status` and log `WARNING` on decode failure in `compute_media_fingerprint` (lines 242–310)
Replace lines 242–310:
```python
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
            if width > 0 and height > 0 and len(dhash) == 32:
                visual_status = "ok"
            else:
                logger.warning(
                    "Visual fingerprint incomplete for %s (width=%s, height=%s)",
                    path.name,
                    width,
                    height,
                )
    except Exception as exc:
        logger.warning("Visual fingerprint unavailable for %s: %s", path.name, exc)
        pixel_sha256 = ""
        dhash = ""
        width = 0
        height = 0
        sharpness = 0.0
        quality_score = 0.0
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
```

#### Edit 5: Tighten `_aspect_ratios_compatible`, `_timestamps_close`, and `is_perceptual_match` (lines 326–398)
Replace lines 326–398:
```python
def _aspect_ratios_compatible(w1: int, h1: int, w2: int, h2: int, max_rel_diff: float = 0.08) -> bool:
    if w1 <= 0 or h1 <= 0 or w2 <= 0 or h2 <= 0:
        return False
    r1 = w1 / h1
    r2 = w2 / h2
    return abs(r1 - r2) / max(r1, r2) <= max_rel_diff


def _normalize_utc_datetime(raw_iso: str) -> datetime:
    dt = datetime.fromisoformat(raw_iso)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _timestamps_close(iso_a: str | None, iso_b: str | None, max_delta_seconds: int = 15) -> bool | None:
    if not iso_a or not iso_b:
        return None
    try:
        dt_a = _normalize_utc_datetime(iso_a)
        dt_b = _normalize_utc_datetime(iso_b)
        return abs((dt_a - dt_b).total_seconds()) <= max_delta_seconds
    except ValueError:
        return None


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
    filename confirmation to match the same photo even when file hashes differ completely.
    """
    if not _aspect_ratios_compatible(incoming_width, incoming_height, existing_width, existing_height):
        return False

    # Scale threshold cleanly if comparing legacy 64-bit hashes vs 128-bit hashes
    effective_len = min(len(incoming_dhash), len(existing_dhash))
    effective_max = max_distance if effective_len == 32 else max(2, max_distance // 2)

    dist = hamming_distance_hex(incoming_dhash, existing_dhash)
    if dist > effective_max:
        return False

    same_stem = bool(
        incoming_filename
        and existing_filename
        and Path(incoming_filename).stem.lower() == Path(existing_filename).stem.lower()
    )

    # If both files have capture timestamps, confirm they match closely (allowing timezone shifts)
    ts_close = _timestamps_close(incoming_taken_at, existing_taken_at, max_delta_seconds=15)
    if ts_close is True:
        return True
    if ts_close is False:
        try:
            dt_a = _normalize_utc_datetime(incoming_taken_at or "")
            dt_b = _normalize_utc_datetime(existing_taken_at or "")
            diff_s = abs((dt_a - dt_b).total_seconds())
            same_minute_sec = (diff_s % 3600) <= 15 or (3600 - (diff_s % 3600)) <= 15
            if diff_s <= 14 * 3600 + 15 and same_minute_sec:
                return same_stem or dist <= max(4, (effective_max * 3) // 4)
        except ValueError:
            pass
        # Allow a match across conflicting timestamps ONLY if visual distance is near-identical
        if same_stem:
            return dist <= min(4, effective_max)
        return dist <= 2

    # Either timestamp is missing (ts_close is None):
    # Require filename stem corroboration whenever distance > 4.
    if same_stem:
        return dist <= max(4, (effective_max * 3) // 4)
    return dist <= min(4, effective_max)
```

---

### 2.2 [`src/gphotos_synology/synology/scanner.py`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/synology/scanner.py)

#### Edit 1: Check `visual_done` / `visual_status in ("ok", "undecodable")` and pass `visual_status=fp.visual_status` (lines 85–110)
Replace lines 85–110 of [`src/gphotos_synology/synology/scanner.py`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/synology/scanner.py#L85-L110):
```python
                existing_meta = indexed_by_path.get(rel_posix)
                visual_done = bool(
                    existing_meta
                    and (
                        existing_meta.get("visual_done")
                        or existing_meta.get("has_visual")
                        or existing_meta.get("visual_status") in ("ok", "undecodable")
                    )
                )
                if (
                    existing_meta is not None
                    and existing_meta["file_size"] == stat.st_size
                    and existing_meta["file_mtime_ns"] == stat.st_mtime_ns
                    and (not compute_visual or visual_done or not self._is_image(full_path))
                ):
                    result.unchanged_skipped += 1
                    continue

                fp = compute_media_fingerprint(full_path, compute_visual=compute_visual)
                self.db.upsert_existing_nas_media(
                    sha256=fp.sha256,
                    pixel_sha256=fp.pixel_sha256,
                    dhash=fp.dhash,
                    width=fp.width,
                    height=fp.height,
                    sharpness=fp.sharpness,
                    quality_score=fp.quality_score,
                    visual_status=fp.visual_status,
                    filename=full_path.name,
                    dest_rel_path=rel_posix,
                    file_size=stat.st_size,
                    file_mtime_ns=stat.st_mtime_ns,
                    taken_at=fp.exif_taken_at,
                )
```

---

### 2.3 Unit Tests to Add in [`tests/test_existing_nas_dedup.py`](file:///home/antigravity/gphotos-to-synology/tests/test_existing_nas_dedup.py)

Append unit tests to [`tests/test_existing_nas_dedup.py`](file:///home/antigravity/gphotos-to-synology/tests/test_existing_nas_dedup.py) covering:
1. **`test_heic_visual_fingerprinting_and_undecodable_scanner_stable_skip(tmp_path, caplog)`**:
   - Generate a genuine `.heic` photo via `pillow_heif.from_pillow(_make_sample_image(1)).save(...)` and a corrupt `.heic` (`b"NOT_A_VALID_HEIC"`), plus a video `.mp4`.
   - Verify `compute_media_fingerprint(valid_heic, compute_visual=True)` returns `visual_status == "ok"`, `width == 160`, `height == 120`, `len(pixel_sha256) == 64`, `len(dhash) == 32`, `quality_score > 0`.
   - Verify `compute_media_fingerprint(valid_heic, compute_visual=False)` and `compute_media_fingerprint(video_mp4, compute_visual=True)` return `visual_status == "skipped"`.
   - Verify `compute_media_fingerprint(corrupt_heic, compute_visual=True)` logs at `logging.WARNING` and returns `visual_status == "undecodable"`.
   - Run `ExistingLibraryScanner(settings, db).scan()` twice:
     - First scan: `newly_indexed == 3`, `unchanged_skipped == 0`.
     - Second scan: `newly_indexed == 0`, `unchanged_skipped == 3` (proving undecodable `.heic` is not re-hashed on subsequent cycles).
2. **`test_perceptual_match_corroboration_unknown_dims_and_timezone_normalization()`**:
   - `_aspect_ratios_compatible` returns `False` when any dimension is `<= 0`.
   - `is_perceptual_match` returns `False` when any dimension is `<= 0`.
   - `is_perceptual_match` at `dist=5, max_distance=10` with `incoming_taken_at=None, existing_taken_at=None`: returns `False` when filenames have different stems (`IMG_A.jpg` vs `IMG_B.jpg`), and `True` when stems match (`IMG_SAME.jpg` vs `IMG_SAME.jpg`).
   - `is_perceptual_match` at `dist=5, max_distance=10` with timezone-aware timestamps representing the same UTC instant (`"2024-07-03T12:00:00+02:00"` vs `"2024-07-03T10:00:00+00:00"`) returns `True`, whereas timestamps with non-matching UTC instants (`"2024-07-03T10:00:00+02:30"` vs `"2024-07-03T10:00:00+00:00"`, 2.5h apart) return `False`.
3. **`test_compare_image_quality_sharpness_guard_on_file_size_rule()`**:
   - At identical `2000x1500` resolution, an incoming file that is `>= 15%` larger (`700_000` vs `500_000` bytes) but noticeably blurrier (`sharpness=68.0` or `75.0` vs `existing.sharpness=80.0`) loses (`decision.incoming_wins is False`), whereas an incoming file with non-inferior sharpness (`sharpness=79.5` vs `80.0`) wins (`decision.incoming_wins is True`).

---

## 3. Verification Commands for Worker

```bash
/home/antigravity/gphotos-to-synology/.venv/bin/pytest tests/test_existing_nas_dedup.py tests/test_database.py tests/test_visual_quality_upgrade.py -v
/home/antigravity/gphotos-to-synology/.venv/bin/pytest tests/e2e/test_tier1_features.py -k "test_t1_b5 or test_t1_b6 or test_t1_d2" -v
/home/antigravity/gphotos-to-synology/.venv/bin/pytest tests/e2e/test_tier2_boundaries.py -k "test_t2_g10 or test_t2_g11" -v
/home/antigravity/gphotos-to-synology/.venv/bin/ruff check src/gphotos_synology/takeout/dedup.py src/gphotos_synology/synology/scanner.py tests/test_existing_nas_dedup.py
```

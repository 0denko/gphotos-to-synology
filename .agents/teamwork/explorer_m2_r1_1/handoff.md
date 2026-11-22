# Explorer M2 (Round 1, Part 1) Handoff — Takeout Extraction (`extractor.py`), Strict Sidecar Matcher (`matcher.py`), and Metadata (`metadata.py` & `importer.py`)

## 1. Executive Summary

This blueprint covers the exact file-and-line implementation plan for **Milestone M2 (Part 1)**:
1. **G9 — Strict Sidecar Matcher (`src/gphotos_synology/takeout/matcher.py` & `tests/test_takeout_matcher.py`)**
2. **G12 — Extraction Decompression Limits, Unsafe Entry Controls & Stale Temp Cleanup (`src/gphotos_synology/takeout/extractor.py` & `tests/test_synology_api_and_security.py`)**
3. **G13 & Metadata Details (`src/gphotos_synology/takeout/metadata.py`, `src/gphotos_synology/synology/importer.py` & `tests/test_metadata_and_importer.py`)**

All designs below have been cross-checked against `PROJECT.md § Interface Contracts 4`, `Collab.md` (G9, G12, G13, and Metadata minors/nits), existing unit tests (`tests/test_takeout_matcher.py`, `tests/test_synology_api_and_security.py`, `tests/test_metadata_and_importer.py`), and the opaque-box E2E test suite (`tests/e2e/test_tier1_features.py`, `tests/e2e/test_tier2_boundaries.py`, `tests/e2e/test_tier4_workloads.py`).

---

## 2. File-by-File Implementation Blueprint

### 2.1 `src/gphotos_synology/takeout/matcher.py` (G9)

#### Current Defects Verified
1. **Unnumbered fallback for `(N)` duplicates (`lines 209-211` and `261-263`)**:
   - In `_candidate_json_names` (`lines 209-211`), `IMG_1234(1).jpg` appends `IMG_1234.jpg.json` and `IMG_1234.jpg.supplemental-metadata.json`.
   - In `find_sidecar_json` title fallback (`lines 261-263`), `dup_match` appends unnumbered `f"{dup_match.group('base')}{ext}".lower()` (`"img_1234.jpg"`) to `title_candidates`. Because `IMG_1234.jpg.json` has internal `"title": "IMG_1234.jpg"`, `IMG_1234(1).jpg` would STILL falsely match `IMG_1234.jpg.json` via title fallback unless `lines 261-263` are also removed.
2. **Stem-only candidate checks before internal `"title"` disambiguation (`lines 220-222`)**:
   - `_candidate_json_names` currently includes `f"{stem}.json"` and `f"{stem}{SUPPLEMENTAL_FULL}.json"`, which run in step 1 before internal `"title"` lookup. In `test_t2_g9` Case 4 (`CLIP_01.jpg` with `CLIP_01.json` having `"title": "CLIP_01.png"` and `true_meta.json` having `"title": "CLIP_01.jpg"`), step 1 wrongly returns `CLIP_01.json` instead of `true_meta.json`.
3. **Unsorted `directory.iterdir()` & case-only filename collisions (`lines 146-168`)**:
   - `directory.iterdir()` is unsorted (`line 150`).
   - `by_name_lower[name_lower] = entry` (`line 156`) clobbers case-differing sidecars (`Photo.JPG.json` vs `photo.jpg.json`) on case-sensitive Linux/DSM filesystems (`test_t2_g9` Case 5).
4. **Non-unique title fallback (`line 274`) & non-unique 46-char prefix fallback (`lines 213-218, 278-282`)**:
   - Title fallback returns `matches[0]` even when `len(matches) > 1` (`test_t2_g9` Case 2).
   - 46-char prefix fallback (`lines 278-282`) returns the first matching JSON even when multiple JSONs share that 46-char prefix (`test_t2_g9` Case 3) or when multiple media files share that 46-char prefix.

#### Exact Replacement Design for `DirectoryJsonIndex`, `_candidate_json_names`, and `find_sidecar_json` (`lines 137-285`)

Update `from dataclasses import dataclass` at line 7 to `from dataclasses import dataclass, field`, and replace lines 137–285 with:

```python
@dataclass
class DirectoryJsonIndex:
    """Indexed lookup of all JSON sidecar files and media stems/prefixes in a single Takeout directory."""

    directory: Path
    by_name_lower: dict[str, Path]
    by_title_lower: dict[str, list[Path]]
    by_name_exact: dict[str, Path] = field(default_factory=dict)
    ambiguous_names_lower: set[str] = field(default_factory=set)
    title_by_path: dict[Path, str] = field(default_factory=dict)
    media_by_stem_lower: dict[str, list[Path]] = field(default_factory=dict)
    media_by_prefix46_lower: dict[str, list[Path]] = field(default_factory=dict)

    def lookup_name(self, candidate: str) -> Path | None:
        """Look up a candidate JSON filename, preferring exact case and rejecting ambiguous case collisions."""
        exact = self.by_name_exact.get(candidate)
        if exact is not None:
            return exact
        key = candidate.lower()
        if key in self.ambiguous_names_lower:
            return None
        return self.by_name_lower.get(key)

    @classmethod
    def build(cls, directory: Path) -> DirectoryJsonIndex:
        by_name_lower: dict[str, Path] = {}
        by_title_lower: dict[str, list[Path]] = {}
        by_name_exact: dict[str, Path] = {}
        ambiguous_names_lower: set[str] = set()
        title_by_path: dict[Path, str] = {}
        media_by_stem_lower: dict[str, list[Path]] = {}
        media_by_prefix46_lower: dict[str, list[Path]] = {}

        for entry in sorted(directory.iterdir(), key=lambda p: p.name):
            if is_media_file(entry):
                media_by_stem_lower.setdefault(entry.stem.lower(), []).append(entry)
                if len(entry.name) >= 35:
                    media_by_prefix46_lower.setdefault(entry.name[:46].lower(), []).append(entry)
                continue

            if not entry.is_file() or entry.suffix.lower() != ".json":
                continue
            name_lower = entry.name.lower()
            if name_lower == "metadata.json":
                continue

            by_name_exact[entry.name] = entry
            if name_lower in by_name_lower and by_name_lower[name_lower] != entry:
                ambiguous_names_lower.add(name_lower)
            else:
                by_name_lower[name_lower] = entry

            # Read internal "title" field for fallback matching (fast on small JSON sidecars)
            try:
                if entry.stat().st_size <= 256_000:
                    data = json.loads(entry.read_text(encoding="utf-8"))
                    title = data.get("title")
                    if isinstance(title, str) and title.strip():
                        t_lower = title.strip().lower()
                        by_title_lower.setdefault(t_lower, []).append(entry)
                        title_by_path[entry] = t_lower
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                continue

        return cls(
            directory=directory,
            by_name_lower=by_name_lower,
            by_title_lower=by_title_lower,
            by_name_exact=by_name_exact,
            ambiguous_names_lower=ambiguous_names_lower,
            title_by_path=title_by_path,
            media_by_stem_lower=media_by_stem_lower,
            media_by_prefix46_lower=media_by_prefix46_lower,
        )


def _candidate_json_names(filename: str, *, allow_truncated_46: bool = True) -> list[str]:
    """Generate ordered candidate JSON sidecar filenames for a given media filename.

    Does NOT include unnumbered fallbacks for `(N)` duplicates or extension-stripped `<stem>.json`
    candidates (which are checked only after internal `"title"` disambiguation).
    """
    candidates: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        key = name.lower()
        if key not in seen:
            seen.add(key)
            candidates.append(name)

    path_obj = Path(filename)
    stem = path_obj.stem
    ext = path_obj.suffix

    # 1. Exact matches (legacy .json and 2024/2025+ .supplemental-metadata.json)
    add(f"{filename}.json")
    add(f"{filename}{SUPPLEMENTAL_FULL}.json")

    # 2. Truncated ".supplemental-metadata" suffixes (Google truncates around 46-51 chars)
    for trunc_len in range(len(SUPPLEMENTAL_FULL) - 1, 0, -1):
        prefix = SUPPLEMENTAL_FULL[:trunc_len]
        add(f"{filename}{prefix}.json")

    # 3. Duplicate number placement quirks:
    #    IMG_1234(1).JPG -> IMG_1234.JPG(1).json / IMG_1234.JPG.supplemental-metadata(1).json
    #    NOTE (G9): NEVER fall back to unnumbered IMG_1234.JPG.json for (N) duplicate files.
    dup_match = DUPLICATE_NUM_RE.match(stem)
    if dup_match:
        base_stem = dup_match.group("base")
        num = dup_match.group("num")
        unnumbered = f"{base_stem}{ext}"
        add(f"{unnumbered}({num}).json")
        add(f"{unnumbered}{SUPPLEMENTAL_FULL}({num}).json")
        for trunc_len in range(len(SUPPLEMENTAL_FULL) - 1, 0, -1):
            add(f"{unnumbered}{SUPPLEMENTAL_FULL[:trunc_len]}({num}).json")
        # Sometimes Google truncates base+ext to 46 chars before (N).json
        if allow_truncated_46 and len(unnumbered) > 46:
            add(f"{unnumbered[:46]}({num}).json")

    # 4. 46-character filename truncation rule used by Google Takeout
    if allow_truncated_46 and len(filename) > 46:
        truncated = filename[:46]
        add(f"{truncated}.json")
        for trunc_len in range(len(SUPPLEMENTAL_FULL), 0, -1):
            add(f"{truncated}{SUPPLEMENTAL_FULL[:trunc_len]}.json")

    return candidates


def _stem_only_json_names(stem: str) -> list[str]:
    """Generate extension-stripped `<stem>.json` candidates (tried only after title disambiguation)."""
    return [
        f"{stem}.json",
        f"{stem}{SUPPLEMENTAL_FULL}.json",
    ]


def find_sidecar_json(media_path: Path, index: DirectoryJsonIndex | None = None) -> Path | None:
    """Locate the matching Google Takeout JSON sidecar for `media_path`, if any."""
    idx = index or DirectoryJsonIndex.build(media_path.parent)
    filename = media_path.name
    stem = media_path.stem
    ext = media_path.suffix

    prefix_key = filename[:46].lower()
    prefix46_unique = len(idx.media_by_prefix46_lower.get(prefix_key, [])) <= 1

    # 1. Direct candidate search (excludes stem-only and unnumbered (N) fallbacks)
    for candidate in _candidate_json_names(filename, allow_truncated_46=prefix46_unique):
        match = idx.lookup_name(candidate)
        if match is not None:
            return match

    # 2. Edited file fallback: IMG_1234-edited.JPG -> look up IMG_1234.JPG
    cleaned_stem = EDITED_SUFFIX_RE.sub("", stem)
    if cleaned_stem != stem:
        unedited_name = f"{cleaned_stem}{ext}"
        unedited_prefix_unique = len(idx.media_by_prefix46_lower.get(unedited_name[:46].lower(), [])) <= 1
        for candidate in _candidate_json_names(unedited_name, allow_truncated_46=unedited_prefix_unique):
            match = idx.lookup_name(candidate)
            if match is not None:
                return match

    # 3. Live Photo companion video fallback: IMG_1234.MP4 / .MOV -> IMG_1234.HEIC.json / .JPG.json
    if ext.lower() in {".mp4", ".mov"}:
        for still_ext in LIVE_PHOTO_STILL_EXTS:
            still_name = f"{cleaned_stem}{still_ext}"
            for candidate in _candidate_json_names(still_name, allow_truncated_46=prefix46_unique):
                match = idx.lookup_name(candidate)
                if match is not None:
                    return match

    # 4. Title-based fallback: match JSON internal "title" == media filename (or unedited filename).
    #    NOTE (G9): Do NOT fall back to unnumbered base title for (N) duplicate files, and require uniqueness.
    title_candidates = [filename.lower()]
    if cleaned_stem != stem:
        title_candidates.append(f"{cleaned_stem}{ext}".lower())

    dup_match = DUPLICATE_NUM_RE.match(stem)
    for title_key in title_candidates:
        matches = idx.by_title_lower.get(title_key)
        if not matches:
            continue
        if len(matches) == 1:
            return matches[0]
        if dup_match:
            num_token = f"({dup_match.group('num')})"
            num_matches = [m for m in matches if num_token in m.name]
            if len(num_matches) == 1:
                return num_matches[0]
        # Ambiguous internal title match -> bail out rather than guessing
        return None

    # 5. Stem-only fallback (<stem>.json without extension), tried AFTER title disambiguation.
    #    Only allowed if no other media file in the directory shares the stem and the JSON's internal
    #    title (if present) does not contradict this media file.
    allowed_titles = {
        filename.lower(),
        f"{cleaned_stem}{ext}".lower(),
        stem.lower(),
        cleaned_stem.lower(),
    }
    stems_to_try = [stem]
    if cleaned_stem != stem:
        stems_to_try.append(cleaned_stem)
    for target_stem in stems_to_try:
        if len(idx.media_by_stem_lower.get(target_stem.lower(), [])) > 1:
            continue
        for candidate in _stem_only_json_names(target_stem):
            match = idx.lookup_name(candidate)
            if match is not None:
                recorded_title = idx.title_by_path.get(match)
                if recorded_title and recorded_title not in allowed_titles:
                    continue
                return match

    # 6. Prefix match fallback for severely truncated supplemental-metadata filenames
    #    e.g. very_long_filename_here_1234567890.jpg -> very_long_filename_here_1234567890.jpg.su.json
    #    Requires unique media file with that 46-char prefix AND unique matching JSON sidecar.
    if len(prefix_key) >= 35 and prefix46_unique:
        prefix_matches = [
            json_path
            for json_name, json_path in idx.by_name_lower.items()
            if json_name.startswith(prefix_key) and json_name not in idx.ambiguous_names_lower
        ]
        if len(prefix_matches) == 1:
            return prefix_matches[0]

    return None
```

---

### 2.2 `src/gphotos_synology/takeout/extractor.py` (G12 & Stale Temp Cleanup)

#### Current Defects Verified
1. **`extract_archive` (`lines 50-80`)**:
   - Lacks `max_entries`, `max_uncompressed_bytes`, `max_compression_ratio`, and `skip_unsafe_entries` keyword parameters required by `PROJECT.md § Interface Contracts 4`.
   - Does not check entry count, pre-flight declared uncompressed size, compression ratio, or actual streaming bytes written during extraction.
   - Always aborts on a single unsafe entry (`skip_unsafe_entries=False` behavior) with no `skip_unsafe_entries=True` mode for per-entry warning + skip.
   - Does not reject Windows-style backslash traversal (`..\\outside.txt`) on Linux.
2. **Missing `cleanup_stale_temp_files(directory: Path, max_age_seconds: float = 0.0) -> int`**:
   - Required by `PROJECT.md § Interface Contracts 4` and `tests/e2e/test_tier1_features.py:268-281` to clean up `.*.tmp.<pid>` partial files created by `safe_move_or_copy` (`line 107`).

#### Exact Replacement Design for `extractor.py` (`lines 42-116`)

Add `import logging` and `import time` at the top of `src/gphotos_synology/takeout/extractor.py`, define `logger = logging.getLogger(__name__)`, and update `_ensure_within`, `extract_archive`, and add `cleanup_stale_temp_files`:

```python
_RATIO_CHECK_MIN_BYTES = 1024 * 1024  # 1 MiB threshold so tiny legitimate JSON sidecars don't false-trigger
_STALE_TMP_RE = re.compile(r"^\..+\.tmp\.\d+$")
_IGNORED_CLEANUP_DIRS = {"@eaDir", "#recycle", "#snapshot"}


def _ensure_within(base_dir: Path, target_path: Path) -> Path:
    resolved_base = base_dir.resolve()
    resolved_target = target_path.resolve()
    if resolved_base != resolved_target and resolved_base not in resolved_target.parents:
        raise ValueError(f"Unsafe archive entry path traversal detected: {target_path}")
    return resolved_target


def _resolve_safe_member_path(base_dir: Path, member_name: str) -> Path:
    normalized = member_name.replace("\\", "/")
    if (
        normalized.startswith("/")
        or normalized.startswith("../")
        or "/../" in normalized
        or normalized.endswith("/..")
        or normalized == ".."
    ):
        raise ValueError(f"Unsafe archive entry path traversal detected: {member_name}")
    return _ensure_within(base_dir, base_dir / normalized)


def _copy_stream_with_limits(
    src,
    dst_path: Path,
    *,
    total_extracted_bytes: int,
    max_uncompressed_bytes: int,
    archive_compressed_bytes: int,
    member_compressed_bytes: int | None,
    max_compression_ratio: float,
) -> int:
    member_written = 0
    try:
        with open(dst_path, "wb") as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                chunk_len = len(chunk)
                member_written += chunk_len
                total_extracted_bytes += chunk_len

                if total_extracted_bytes > max_uncompressed_bytes:
                    raise ValueError(
                        f"Archive extraction exceeded max uncompressed bytes limit ({max_uncompressed_bytes})"
                    )
                if max_compression_ratio > 0:
                    if (
                        member_compressed_bytes is not None
                        and member_written >= _RATIO_CHECK_MIN_BYTES
                        and (member_written / max(1, member_compressed_bytes)) > max_compression_ratio
                    ):
                        raise ValueError(
                            f"Archive entry compression ratio exceeded limit ({max_compression_ratio})"
                        )
                    if (
                        total_extracted_bytes >= _RATIO_CHECK_MIN_BYTES
                        and archive_compressed_bytes > 0
                        and (total_extracted_bytes / max(1, archive_compressed_bytes)) > max_compression_ratio
                    ):
                        raise ValueError(
                            f"Archive compression ratio exceeded limit ({max_compression_ratio})"
                        )
                dst.write(chunk)
    except Exception:
        with contextlib.suppress(OSError):
            dst_path.unlink(missing_ok=True)
        raise
    return total_extracted_bytes


def extract_archive(
    archive_path: Path,
    dest_dir: Path,
    *,
    max_entries: int = 100_000,
    max_uncompressed_bytes: int | None = None,
    max_compression_ratio: float = 100.0,
    skip_unsafe_entries: bool = False,
) -> Path:
    """Safely extract a .zip, .tgz, or .tar.gz archive into `dest_dir` with decompression limits."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    lower = archive_path.name.lower()

    if max_entries <= 0:
        raise ValueError(f"max_entries must be positive, got {max_entries}")

    if max_uncompressed_bytes is None:
        free_bytes = shutil.disk_usage(dest_dir).free
        effective_max_bytes = max(1, int(free_bytes * 0.9))
    else:
        if max_uncompressed_bytes <= 0:
            raise ValueError(f"max_uncompressed_bytes must be positive, got {max_uncompressed_bytes}")
        effective_max_bytes = max_uncompressed_bytes

    try:
        archive_size = max(1, archive_path.stat().st_size)
    except OSError:
        archive_size = 1

    total_extracted_bytes = 0

    if lower.endswith(".zip"):
        with zipfile.ZipFile(archive_path, "r") as zf:
            members = zf.infolist()
            file_members = [m for m in members if not m.is_dir()]
            if len(members) > max_entries or len(file_members) > max_entries:
                raise ValueError(
                    f"Archive entry count ({len(members)}) exceeds max_entries limit ({max_entries})"
                )

            declared_total = 0
            for member in file_members:
                if member.file_size < 0:
                    raise ValueError(f"Invalid negative uncompressed size in archive entry: {member.filename}")
                declared_total += member.file_size
                if declared_total > effective_max_bytes:
                    raise ValueError(
                        f"Archive declared uncompressed size ({declared_total}) exceeds limit ({effective_max_bytes})"
                    )
                if (
                    max_compression_ratio > 0
                    and member.file_size >= _RATIO_CHECK_MIN_BYTES
                    and (member.file_size / max(1, member.compress_size)) > max_compression_ratio
                ):
                    raise ValueError(
                        f"Archive member compression ratio exceeds limit ({max_compression_ratio}): {member.filename}"
                    )

            if (
                max_compression_ratio > 0
                and declared_total >= _RATIO_CHECK_MIN_BYTES
                and (declared_total / archive_size) > max_compression_ratio
            ):
                raise ValueError(
                    f"Archive compression ratio exceeds limit ({max_compression_ratio}): {archive_path.name}"
                )

            for member in file_members:
                # Skip ZIP symlink entries (POSIX S_IFLNK = 0o120000 in upper 16 bits of external_attr)
                mode = (member.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    if not skip_unsafe_entries:
                        raise ValueError(f"Unsafe archive entry path traversal / symlink detected: {member.filename}")
                    logger.warning("Skipping symlink entry %s in %s", member.filename, archive_path.name)
                    continue

                try:
                    target = _resolve_safe_member_path(dest_dir, member.filename)
                except ValueError:
                    if not skip_unsafe_entries:
                        raise
                    logger.warning("Skipping unsafe archive entry %s in %s", member.filename, archive_path.name)
                    continue

                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member, "r") as src:
                    total_extracted_bytes = _copy_stream_with_limits(
                        src,
                        target,
                        total_extracted_bytes=total_extracted_bytes,
                        max_uncompressed_bytes=effective_max_bytes,
                        archive_compressed_bytes=archive_size,
                        member_compressed_bytes=member.compress_size,
                        max_compression_ratio=max_compression_ratio,
                    )
        return dest_dir

    if lower.endswith((".tgz", ".tar.gz")):
        with tarfile.open(archive_path, "r:gz") as tf:
            members = tf.getmembers()
            file_members = [m for m in members if not m.isdir()]
            if len(members) > max_entries or len(file_members) > max_entries:
                raise ValueError(
                    f"Archive entry count ({len(members)}) exceeds max_entries limit ({max_entries})"
                )

            declared_total = 0
            for member in members:
                if member.isfile():
                    if member.size < 0:
                        raise ValueError(f"Invalid negative size in tar entry: {member.name}")
                    declared_total += member.size
                    if declared_total > effective_max_bytes:
                        raise ValueError(
                            f"Archive declared uncompressed size ({declared_total}) exceeds limit ({effective_max_bytes})"
                        )

            if (
                max_compression_ratio > 0
                and declared_total >= _RATIO_CHECK_MIN_BYTES
                and (declared_total / archive_size) > max_compression_ratio
            ):
                raise ValueError(
                    f"Archive compression ratio exceeds limit ({max_compression_ratio}): {archive_path.name}"
                )

            for member in members:
                if member.isdir():
                    continue
                try:
                    target = _resolve_safe_member_path(dest_dir, member.name)
                except ValueError:
                    if not skip_unsafe_entries:
                        raise
                    logger.warning("Skipping unsafe tar entry %s in %s", member.name, archive_path.name)
                    continue

                if not member.isfile():
                    # Never materialize symlinks, hardlinks, FIFOs, or device nodes from TAR archives
                    logger.warning(
                        "Skipping non-regular tar member %s (type=%r) in %s",
                        member.name,
                        member.type,
                        archive_path.name,
                    )
                    continue

                target.parent.mkdir(parents=True, exist_ok=True)
                extracted = tf.extractfile(member)
                if extracted is None:
                    continue
                with extracted:
                    total_extracted_bytes = _copy_stream_with_limits(
                        extracted,
                        target,
                        total_extracted_bytes=total_extracted_bytes,
                        max_uncompressed_bytes=effective_max_bytes,
                        archive_compressed_bytes=archive_size,
                        member_compressed_bytes=None,
                        max_compression_ratio=max_compression_ratio,
                    )
        return dest_dir

    raise ValueError(f"Unsupported archive format: {archive_path.name}")
```

And add `cleanup_stale_temp_files` after `cleanup_directory` (`line 86`):

```python
def cleanup_stale_temp_files(directory: Path, max_age_seconds: float = 0.0) -> int:
    """Remove stale `.<filename>.tmp.<pid>` files left behind by interrupted moves/copies."""
    if not directory.exists():
        return 0
    now = time.time()
    removed = 0
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in _IGNORED_CLEANUP_DIRS and not d.startswith(".")]
        root_path = Path(root)
        for fname in files:
            if not (_STALE_TMP_RE.match(fname) or (fname.startswith(".") and ".tmp." in fname)):
                continue
            candidate = root_path / fname
            try:
                if max_age_seconds > 0.0:
                    st = candidate.stat()
                    if (now - st.st_mtime) < max_age_seconds:
                        continue
                candidate.unlink(missing_ok=True)
                removed += 1
            except OSError:
                continue
    return removed
```

---

### 2.3 `src/gphotos_synology/takeout/metadata.py` (G13 & Metadata Details)

#### Current Defects Verified
1. **`_parse_timestamp` (`lines 40-49`)**:
   - `int(str(value).strip())` fails with `ValueError` on float-formatted epoch strings such as `"1720000000.5"` (`test_t1_b3`).
2. **`_deg_to_dms_rational` (`lines 114-120`)**:
   - `round((m_float - m) * 60.0 * 10000)` produces `s = 600000` (`60.0` seconds) for inputs like `37.99999999` without carrying into `m` and `d` (`test_t2_g13`).
3. **`_apply_piexif_jpeg` (`lines 123-159`)**:
   - **G13 (`lines 130-134`)**: When `piexif.load(str(media_path))` raises on malformed/unparseable existing EXIF, it substitutes an empty dict and calls `piexif.insert(...)`, destroying existing EXIF tags (orientation, make/model, lens, GPS, thumbnail). Note: `piexif.load` on a fresh EXIF-less JPEG returns empty dicts without raising! So any exception in `piexif.load` means corrupt/unparseable EXIF and must log a `WARNING` and return `False` (falling back to `mtime_only` without touching file bytes, verified by `test_t2_g13` and `test_t4_w2`).
   - **Missing `OffsetTime*` (`lines 135-140`)**: Writes UTC `DateTimeOriginal` without `OffsetTime`, `OffsetTimeOriginal`, or `OffsetTimeDigitized` (`b"+00:00"`).
   - **Non-ASCII `ImageDescription` (`lines 141-143`)**: Encodes non-ASCII UTF-8 directly into `0th[ImageDescription]` (an ASCII-defined EXIF tag) and never writes Unicode `Exif[UserComment]` (`test_t1_b3`).
4. **`apply_metadata` (`lines 186-225`)**:
   - **Silent non-JPEG fallback when ExifTool is absent (`lines 186-189`)**: When `not exiftool_available(exiftool_path)` and `media_path` is a non-JPEG (e.g. `.png`, `.heic`, `.mp4`), degrades silently to `"mtime_only"` with no `WARNING` log (`test_t2_g13`).
   - **Missing `-EXIF:OffsetTimeOriginal=+00:00` in ExifTool args (`lines 217-224`)**: Does not pass `OffsetTimeOriginal=+00:00` (`test_t1_b4`).

#### Exact Replacement Design for `metadata.py`

Add `import math` at the top of `src/gphotos_synology/takeout/metadata.py`.

1. **Replace `_parse_timestamp` (`lines 40-49`)**:
```python
def _parse_timestamp(value: object) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        raw = str(value).strip()
        if not raw:
            return None
        ts_float = float(raw)
        if not math.isfinite(ts_float) or ts_float <= 0:
            return None
        return datetime.fromtimestamp(ts_float, tz=UTC)
    except (ValueError, TypeError, OverflowError, OSError):
        return None
```

2. **Replace `_deg_to_dms_rational` (`lines 114-120`)**:
```python
def _deg_to_dms_rational(deg: float) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int]]:
    abs_deg = abs(deg)
    d = int(abs_deg)
    m_float = (abs_deg - d) * 60.0
    m = int(m_float)
    s = round((m_float - m) * 60.0 * 10000)
    if s >= 600000:
        s -= 600000
        m += 1
    if m >= 60:
        m -= 60
        d += 1
    return ((d, 1), (m, 1), (s, 10000))
```

3. **Replace `_apply_piexif_jpeg` (`lines 123-159`)**:
```python
def _apply_piexif_jpeg(media_path: Path, metadata: PhotoMetadata) -> bool:
    """Pure-Python EXIF/GPS writer fallback for JPEGs when ExifTool isn't available."""
    if media_path.suffix.lower() not in {".jpg", ".jpeg"}:
        return False
    try:
        import piexif
        import piexif.helper

        try:
            exif_dict = piexif.load(str(media_path))
        except Exception as exc:
            logger.warning(
                "Skipping piexif EXIF write for %s because existing EXIF could not be parsed: %s",
                media_path.name,
                exc,
            )
            return False

        exif_dict.setdefault("0th", {})
        exif_dict.setdefault("Exif", {})
        exif_dict.setdefault("GPS", {})

        if metadata.taken_at is not None:
            dt_bytes = metadata.taken_at.strftime("%Y:%m:%d %H:%M:%S").encode("ascii")
            offset_bytes = b"+00:00"
            exif_dict["0th"][piexif.ImageIFD.DateTime] = dt_bytes
            exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = dt_bytes
            exif_dict["Exif"][piexif.ExifIFD.DateTimeDigitized] = dt_bytes
            exif_dict["Exif"][piexif.ExifIFD.OffsetTime] = offset_bytes
            exif_dict["Exif"][piexif.ExifIFD.OffsetTimeOriginal] = offset_bytes
            exif_dict["Exif"][piexif.ExifIFD.OffsetTimeDigitized] = offset_bytes

        if metadata.description:
            try:
                exif_dict["0th"][piexif.ImageIFD.ImageDescription] = metadata.description.encode("ascii")
            except UnicodeEncodeError:
                ascii_desc = metadata.description.encode("ascii", errors="ignore").strip()
                if ascii_desc:
                    exif_dict["0th"][piexif.ImageIFD.ImageDescription] = ascii_desc
                exif_dict["Exif"][piexif.ExifIFD.UserComment] = piexif.helper.UserComment.dump(
                    metadata.description, encoding="unicode"
                )

        if metadata.has_gps and metadata.latitude is not None and metadata.longitude is not None:
            exif_dict["GPS"][piexif.GPSIFD.GPSLatitudeRef] = b"N" if metadata.latitude >= 0 else b"S"
            exif_dict["GPS"][piexif.GPSIFD.GPSLatitude] = _deg_to_dms_rational(metadata.latitude)
            exif_dict["GPS"][piexif.GPSIFD.GPSLongitudeRef] = b"E" if metadata.longitude >= 0 else b"W"
            exif_dict["GPS"][piexif.GPSIFD.GPSLongitude] = _deg_to_dms_rational(metadata.longitude)
            if metadata.altitude is not None and abs(metadata.altitude) > 1e-7:
                exif_dict["GPS"][piexif.GPSIFD.GPSAltitudeRef] = 0 if metadata.altitude >= 0 else 1
                exif_dict["GPS"][piexif.GPSIFD.GPSAltitude] = (round(abs(metadata.altitude) * 100), 100)

        exif_bytes = piexif.dump(exif_dict)
        piexif.insert(exif_bytes, str(media_path))
        return True
    except Exception as exc:
        logger.debug("piexif fallback failed for %s: %s", media_path.name, exc)
        return False
```

4. **Update `apply_metadata` (`lines 186-225`)**:
```python
    if not exiftool_available(exiftool_path):
        if media_path.suffix.lower() not in {".jpg", ".jpeg"}:
            logger.warning(
                "ExifTool (%s) is not installed; falling back to mtime_only for non-JPEG file %s",
                exiftool_path,
                media_path.name,
            )
            _apply_file_mtime(media_path, metadata.taken_at)
            return "mtime_only"
        repaired_py = _apply_piexif_jpeg(media_path, metadata)
        _apply_file_mtime(media_path, metadata.taken_at)
        return "repaired" if repaired_py else "mtime_only"
```
And in the `else:` branch of `if metadata.taken_at is not None:` (`lines 216-224`):
```python
        else:
            args.extend(
                [
                    f"-EXIF:DateTimeOriginal={exif_dt}",
                    f"-EXIF:CreateDate={exif_dt}",
                    f"-EXIF:ModifyDate={exif_dt}",
                    "-EXIF:OffsetTime=+00:00",
                    "-EXIF:OffsetTimeOriginal=+00:00",
                    "-EXIF:OffsetTimeDigitized=+00:00",
                    f"-XMP:DateCreated={iso_dt}",
                ]
            )
```

---

### 2.4 `src/gphotos_synology/synology/importer.py`

- **Inspection status**: `SynologyFolderImporter` (`lines 23-86`) and `sanitize_component` (`lines 18-20`) already satisfy all unit and E2E contracts (`test_apply_metadata_mtime_fallback_and_folder_importer` in `tests/test_metadata_and_importer.py` and `test_t1_d5_synology_folder_importer_sanitization_and_collision_safety` in `tests/e2e/test_tier1_features.py`). No functional changes are required in `importer.py`.

---

## 3. Unit Test Additions Blueprint

### 3.1 `tests/test_takeout_matcher.py`
Keep all 4 existing tests intact and add:
1. `test_numbered_duplicate_does_not_fallback_to_unnumbered_sidecar(tmp_path)`:
   - `IMG_1234(1).jpg` with only `IMG_1234.jpg.json` (having `"title": "IMG_1234.jpg"`) or `IMG_1234.jpg.supplemental-metadata.json` returns `None`.
2. `test_ambiguous_title_and_ambiguous_46_char_prefix_return_none(tmp_path)`:
   - Two JSON sidecars sharing `"title": "VACATION.jpg"` → `find_sidecar_json` returns `None`.
   - Two JSON sidecars sharing a 46-char prefix → `find_sidecar_json` returns `None`.
   - Two media files sharing a 46-char prefix with one JSON lacking a disambiguating title → `find_sidecar_json` returns `None`.
3. `test_stem_only_runs_after_title_and_respects_contradicting_title(tmp_path)`:
   - `CLIP_01.jpg` with `CLIP_01.json` (`"title": "CLIP_01.png"`) and `true_meta.json` (`"title": "CLIP_01.jpg"`) matches `true_meta.json`.
   - `CLIP_01.jpg` with only `CLIP_01.json` (`"title": "CLIP_01.png"`) returns `None`.
4. `test_case_only_sidecar_collision_prefers_exact_case(tmp_path)`:
   - `Photo.JPG` with `photo.jpg.json` and `Photo.JPG.json` returns `Photo.JPG.json`.

### 3.2 `tests/test_synology_api_and_security.py`
Keep `test_extract_archive_blocks_zipslip_path_traversal` intact and add:
1. `test_extract_archive_enforces_entry_bytes_ratio_and_streaming_limits(tmp_path)`:
   - `max_entries` exceeded raises `ValueError`.
   - `max_uncompressed_bytes` exceeded raises `ValueError`.
   - `max_compression_ratio` exceeded on a 2 MiB zero-filled member raises `ValueError`.
   - Falsified `ZipInfo.file_size` header (patched in central/local header or monkeypatched `infolist()`) caught by streaming byte counter raises `ValueError`.
2. `test_extract_archive_skip_unsafe_entries_zip_and_tar_symlinks(tmp_path)`:
   - `skip_unsafe_entries=False` raises `ValueError(match="path traversal")` on ZIP and TAR traversal (`../`).
   - `skip_unsafe_entries=True` skips traversal entries with a warning and extracts safe entries; TAR symlinks are never materialized.
3. `test_cleanup_stale_temp_files(tmp_path)`:
   - Removes `.IMG_001.jpg.tmp.12345` while preserving normal files and respecting `max_age_seconds`.

### 3.3 `tests/test_metadata_and_importer.py`
Keep all 3 existing tests intact (and add `assert exif["Exif"][piexif.ExifIFD.OffsetTimeOriginal] == b"+00:00"` to `test_apply_metadata_piexif_jpeg_fallback_without_exiftool`) and add:
1. `test_piexif_corrupt_exif_skips_write_and_preserves_bytes(tmp_path, caplog)`:
   - JPEG with malformed APP1 `Exif\x00\x00CORRUPT...` causes `piexif.load` to fail; `apply_metadata` returns `"mtime_only"`, logs a `WARNING`, updates `mtime`, and preserves the file's bytes bit-for-bit.
2. `test_deg_to_dms_rational_clamps_60_seconds_carry(tmp_path)`:
   - `_deg_to_dms_rational(37.99999999)` returns `((38, 1), (0, 1), (0, 10000))`.
3. `test_unicode_description_and_float_timestamp_and_non_jpeg_warning(tmp_path, caplog)`:
   - Float timestamp `"1720000000.5"` parses cleanly in `parse_sidecar_json`.
   - Non-ASCII description `"Café 東京 sunset"` writes ASCII-only to `0th[ImageDescription]` and full Unicode to `Exif[UserComment]`.
   - Non-JPEG (`.png`) without ExifTool logs a `WARNING` and returns `"mtime_only"`.
   - With a stub executable script for `exiftool_path`, `apply_metadata` passes `-EXIF:OffsetTimeOriginal=+00:00`.

"""Battle-tested matcher for Google Takeout media files, albums, and JSON sidecars."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

MEDIA_EXTENSIONS: set[str] = {
    # Images
    ".jpg",
    ".jpeg",
    ".png",
    ".heic",
    ".heif",
    ".webp",
    ".gif",
    ".tif",
    ".tiff",
    ".bmp",
    ".dng",
    ".cr2",
    ".cr3",
    ".nef",
    ".arw",
    ".orf",
    ".rw2",
    ".raf",
    ".avif",
    # Videos
    ".mp4",
    ".mov",
    ".m4v",
    ".3gp",
    ".3g2",
    ".mkv",
    ".avi",
    ".wmv",
    ".webm",
    ".mts",
    ".m2ts",
    ".mpg",
    ".mpeg",
}

VIDEO_EXTENSIONS: set[str] = {
    ".mp4",
    ".mov",
    ".m4v",
    ".3gp",
    ".3g2",
    ".mkv",
    ".avi",
    ".wmv",
    ".webm",
    ".mts",
    ".m2ts",
    ".mpg",
    ".mpeg",
}

LIVE_PHOTO_STILL_EXTS: tuple[str, ...] = (".heic", ".HEIC", ".jpg", ".JPG", ".jpeg", ".JPEG")

# Suffixes appended by Google Photos for edited copies across common locales
EDITED_SUFFIX_RE = re.compile(
    r"-(?:edited|bearbeitet|modifié|modifie|editado|modificato|bewerkt|redigeret|redigerad|"
    r"redigert|muokattu|edytowane|upraveno|szerkesztett|düzenlendi|измененный)$",
    re.IGNORECASE,
)

# Trailing duplicate counter e.g. IMG_1234(1)
DUPLICATE_NUM_RE = re.compile(r"^(?P<base>.+?)\((?P<num>\d+)\)$")

# Default auto-generated yearly folders in Takeout (not user-created albums)
YEARLY_FOLDER_RE = re.compile(
    r"^(?:Photos from|Fotos de|Photos de|Fotos aus|Foto da|Foto's uit|Zdjęcia z|Фото за)\s+\d{4}$",
    re.IGNORECASE,
)

SYSTEM_NON_ALBUM_FOLDERS: set[str] = {
    "google photos",
    "takeout",
    "archive",
    "trash",
    "bin",
    "locked folder",
    "untitled",
    "print subscriptions",
    "shared album comments",
    "user-generated-memory-titles",
}

SUPPLEMENTAL_FULL = ".supplemental-metadata"


def is_media_file(path: Path) -> bool:
    """Return True if path points to a supported image or video file."""
    if not path.is_file():
        return False
    if path.name.startswith("._") or path.name.startswith("."):
        return False
    return path.suffix.lower() in MEDIA_EXTENSIONS


def is_video_file(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


def detect_album_name(media_dir: Path) -> str | None:
    """Determine if a Takeout folder represents a user album and return its canonical title.

    Returns None for default yearly folders (e.g. 'Photos from 2024') or system folders.
    """
    folder_name = media_dir.name.strip()
    if not folder_name:
        return None
    if folder_name.lower() in SYSTEM_NON_ALBUM_FOLDERS:
        return None
    if YEARLY_FOLDER_RE.match(folder_name):
        return None

    # Google Takeout includes a metadata.json file inside album folders with the full title
    album_meta = media_dir / "metadata.json"
    if album_meta.is_file():
        try:
            payload = json.loads(album_meta.read_text(encoding="utf-8"))
            title = str(payload.get("title", "")).strip()
            if title and not YEARLY_FOLDER_RE.match(title) and title.lower() not in SYSTEM_NON_ALBUM_FOLDERS:
                return title
        except (OSError, json.JSONDecodeError, ValueError):
            pass

    return folder_name


@dataclass
class DirectoryJsonIndex:
    """Indexed lookup of all JSON sidecar files in a single Takeout directory."""

    directory: Path
    by_name_lower: dict[str, Path]
    by_title_lower: dict[str, list[Path]]
    by_name_exact: dict[str, Path] = field(default_factory=dict)
    by_name_lower_all: dict[str, list[Path]] = field(default_factory=dict)

    @classmethod
    def build(cls, directory: Path) -> DirectoryJsonIndex:
        by_name_exact: dict[str, Path] = {}
        by_name_lower: dict[str, Path] = {}
        by_name_lower_all: dict[str, list[Path]] = {}
        by_title_lower: dict[str, list[Path]] = {}

        for entry in sorted(directory.iterdir(), key=lambda p: p.name):
            if not entry.is_file() or entry.suffix.lower() != ".json":
                continue
            name_lower = entry.name.lower()
            if name_lower == "metadata.json":
                continue
            by_name_exact[entry.name] = entry
            by_name_lower.setdefault(name_lower, entry)
            by_name_lower_all.setdefault(name_lower, []).append(entry)

            # Read internal "title" field for fallback matching (fast on small JSON sidecars)
            try:
                if entry.stat().st_size <= 256_000:
                    data = json.loads(entry.read_text(encoding="utf-8"))
                    title = data.get("title")
                    if isinstance(title, str) and title.strip():
                        by_title_lower.setdefault(title.strip().lower(), []).append(entry)
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                continue

        return cls(
            directory=directory,
            by_name_lower=by_name_lower,
            by_title_lower=by_title_lower,
            by_name_exact=by_name_exact,
            by_name_lower_all=by_name_lower_all,
        )

    def lookup_name(self, candidate: str) -> Path | None:
        exact = self.by_name_exact.get(candidate)
        if exact is not None:
            return exact
        matches = self.by_name_lower_all.get(candidate.lower())
        if matches and len(matches) == 1:
            return matches[0]
        return self.by_name_lower.get(candidate.lower()) if not matches else None


def _candidate_json_names(filename: str, *, include_stem_only: bool = False) -> list[str]:
    """Generate ordered candidate JSON sidecar filenames for a given media filename.

    Deliberately does NOT fall back from a numbered duplicate `IMG_1234(1).JPG`
    to the unnumbered `IMG_1234.JPG.json` sidecar (prevents attaching another photo's
    capture date/GPS to a distinct file that merely collided on name).
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
        if len(unnumbered) > 46:
            add(f"{unnumbered[:46]}({num}).json")

    # 4. 46-character filename truncation rule used by Google Takeout
    if len(filename) > 46:
        truncated = filename[:46]
        add(f"{truncated}.json")
        for trunc_len in range(len(SUPPLEMENTAL_FULL), 0, -1):
            add(f"{truncated}{SUPPLEMENTAL_FULL[:trunc_len]}.json")

    # 5. Strip extension quirk (only attempted after title disambiguation)
    if include_stem_only:
        add(f"{stem}.json")
        add(f"{stem}{SUPPLEMENTAL_FULL}.json")

    return candidates


def find_sidecar_json(media_path: Path, index: DirectoryJsonIndex | None = None) -> Path | None:
    """Locate the matching Google Takeout JSON sidecar for `media_path`, if any.

    Bails out to `None` on ambiguous matches rather than attaching a wrong sidecar.
    """
    idx = index or DirectoryJsonIndex.build(media_path.parent)
    filename = media_path.name
    stem = media_path.stem
    ext = media_path.suffix

    # 1. Direct candidate search (exact, supplemental-metadata, numbered duplicate, 46-char truncation)
    for candidate in _candidate_json_names(filename, include_stem_only=False):
        match = idx.lookup_name(candidate)
        if match is not None:
            return match

    # 2. Edited file fallback: IMG_1234-edited.JPG -> look up IMG_1234.JPG
    cleaned_stem = EDITED_SUFFIX_RE.sub("", stem)
    if cleaned_stem != stem:
        unedited_name = f"{cleaned_stem}{ext}"
        for candidate in _candidate_json_names(unedited_name, include_stem_only=False):
            match = idx.lookup_name(candidate)
            if match is not None:
                return match

    # 3. Live Photo companion video fallback: IMG_1234.MP4 / .MOV -> IMG_1234.HEIC.json / .JPG.json
    if ext.lower() in {".mp4", ".mov"}:
        for still_ext in LIVE_PHOTO_STILL_EXTS:
            for candidate in _candidate_json_names(f"{cleaned_stem}{still_ext}", include_stem_only=False):
                match = idx.lookup_name(candidate)
                if match is not None:
                    return match

    # 4. Title-based fallback: match JSON internal "title" (requires unambiguous candidate)
    dup_match = DUPLICATE_NUM_RE.match(stem)
    title_candidates: list[str] = [filename.lower()]
    if cleaned_stem != stem:
        title_candidates.append(f"{cleaned_stem}{ext}".lower())

    for title_key in title_candidates:
        matches = idx.by_title_lower.get(title_key)
        if matches and len(matches) == 1:
            return matches[0]

    # For numbered duplicates (IMG_1234(1).JPG), only accept a sidecar with matching (N) token
    if dup_match:
        unnumbered_key = f"{dup_match.group('base')}{ext}".lower()
        num_token = f"({dup_match.group('num')})"
        matches = idx.by_title_lower.get(unnumbered_key) or []
        numbered_matches = [m for m in matches if num_token in m.name]
        if len(numbered_matches) == 1:
            return numbered_matches[0]
        # Never fall back to unnumbered sidecar for (N) duplicates
        return None

    # 5. Strip-extension quirk (<stem>.json) — only after title disambiguation
    for candidate in (f"{stem}.json", f"{stem}{SUPPLEMENTAL_FULL}.json"):
        match = idx.lookup_name(candidate)
        if match is not None:
            return match

    # 6. Prefix match fallback for severely truncated supplemental-metadata filenames
    #    Only return when a SINGLE unique sidecar matches the 46-char prefix.
    prefix_key = filename[:46].lower()
    if len(prefix_key) >= 35:
        prefix_matches: list[Path] = []
        for json_name, json_paths in idx.by_name_lower_all.items():
            if json_name.startswith(prefix_key):
                prefix_matches.extend(json_paths)
        if len(prefix_matches) == 1:
            return prefix_matches[0]

    return None

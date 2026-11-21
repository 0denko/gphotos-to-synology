"""Google Takeout JSON metadata parser and ExifTool metadata writer."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from gphotos_synology.takeout.matcher import is_video_file

logger = logging.getLogger(__name__)


@dataclass
class PhotoMetadata:
    """Normalized metadata extracted from a Google Takeout JSON sidecar."""

    title: str = ""
    description: str = ""
    taken_at: datetime | None = None
    latitude: float | None = None
    longitude: float | None = None
    altitude: float | None = None
    people: list[str] = field(default_factory=list)
    album_name: str | None = None

    @property
    def has_gps(self) -> bool:
        if self.latitude is None or self.longitude is None:
            return False
        # Google Takeout uses 0.0, 0.0 when no GPS coordinates are present
        return not (abs(self.latitude) < 1e-7 and abs(self.longitude) < 1e-7)


def _parse_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        ts = int(str(value).strip())
        if ts <= 0:
            return None
        return datetime.fromtimestamp(ts, tz=UTC)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _extract_geo(payload: dict) -> tuple[float | None, float | None, float | None]:
    # Prefer geoDataExif (original camera GPS), fall back to geoData (user-edited/inferred)
    for key in ("geoDataExif", "geoData"):
        geo = payload.get(key)
        if not isinstance(geo, dict):
            continue
        try:
            lat = float(geo.get("latitude", 0.0))
            lon = float(geo.get("longitude", 0.0))
            alt = float(geo.get("altitude", 0.0))
        except (TypeError, ValueError):
            continue
        if abs(lat) >= 1e-7 or abs(lon) >= 1e-7:
            return lat, lon, alt
    return None, None, None


def parse_sidecar_json(json_path: Path, album_name: str | None = None) -> PhotoMetadata:
    """Parse a Google Takeout sidecar JSON file into PhotoMetadata."""
    data = json.loads(json_path.read_text(encoding="utf-8"))

    taken_at = _parse_timestamp((data.get("photoTakenTime") or {}).get("timestamp"))
    if taken_at is None:
        taken_at = _parse_timestamp((data.get("creationTime") or {}).get("timestamp"))

    lat, lon, alt = _extract_geo(data)

    people: list[str] = []
    raw_people = data.get("people")
    if isinstance(raw_people, list):
        for person in raw_people:
            if isinstance(person, dict):
                name = str(person.get("name", "")).strip()
                if name:
                    people.append(name)

    return PhotoMetadata(
        title=str(data.get("title", "")).strip(),
        description=str(data.get("description", "")).strip(),
        taken_at=taken_at,
        latitude=lat,
        longitude=lon,
        altitude=alt,
        people=people,
        album_name=album_name,
    )


def exiftool_available(exiftool_path: str = "exiftool") -> bool:
    return shutil.which(exiftool_path) is not None


def _apply_file_mtime(media_path: Path, taken_at: datetime | None) -> None:
    if taken_at is None:
        return
    ts = taken_at.timestamp()
    try:
        os.utime(media_path, (ts, ts))
    except OSError as exc:
        logger.debug("Could not set mtime on %s: %s", media_path, exc)


def _deg_to_dms_rational(deg: float) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int]]:
    abs_deg = abs(deg)
    d = int(abs_deg)
    m_float = (abs_deg - d) * 60.0
    m = int(m_float)
    s = round((m_float - m) * 60.0 * 10000)
    return ((d, 1), (m, 1), (s, 10000))


def _apply_piexif_jpeg(media_path: Path, metadata: PhotoMetadata) -> bool:
    """Pure-Python EXIF/GPS writer fallback for JPEGs when ExifTool isn't available."""
    if media_path.suffix.lower() not in {".jpg", ".jpeg"}:
        return False
    try:
        import piexif

        try:
            exif_dict = piexif.load(str(media_path))
        except Exception:
            exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}

        if metadata.taken_at is not None:
            dt_bytes = metadata.taken_at.strftime("%Y:%m:%d %H:%M:%S").encode("ascii")
            exif_dict["0th"][piexif.ImageIFD.DateTime] = dt_bytes
            exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = dt_bytes
            exif_dict["Exif"][piexif.ExifIFD.DateTimeDigitized] = dt_bytes

        if metadata.description:
            exif_dict["0th"][piexif.ImageIFD.ImageDescription] = metadata.description.encode("utf-8")

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


def apply_metadata(
    media_path: Path,
    metadata: PhotoMetadata,
    *,
    write_exif: bool = True,
    write_album_tags: bool = True,
    exiftool_path: str = "exiftool",
) -> str:
    """Write metadata into `media_path` using ExifTool (or pure-Python piexif) and sync mtime.

    Returns one of: 'repaired', 'mtime_only', 'skipped', 'error'.
    """
    if (
        metadata.taken_at is None
        and not metadata.has_gps
        and not metadata.description
        and not metadata.people
        and not (write_album_tags and metadata.album_name)
    ):
        return "skipped"

    if not write_exif:
        _apply_file_mtime(media_path, metadata.taken_at)
        return "mtime_only"

    if not exiftool_available(exiftool_path):
        repaired_py = _apply_piexif_jpeg(media_path, metadata)
        _apply_file_mtime(media_path, metadata.taken_at)
        return "repaired" if repaired_py else "mtime_only"

    args: list[str] = [
        exiftool_path,
        "-overwrite_original",
        "-ignoreMinorErrors",
        "-q",
        "-q",
        "-charset",
        "filename=utf8",
    ]

    is_video = is_video_file(media_path)

    if metadata.taken_at is not None:
        exif_dt = metadata.taken_at.strftime("%Y:%m:%d %H:%M:%S")
        iso_dt = metadata.taken_at.strftime("%Y:%m:%d %H:%M:%S+00:00")
        if is_video:
            args.extend(
                [
                    f"-QuickTime:CreateDate={exif_dt}",
                    f"-QuickTime:ModifyDate={exif_dt}",
                    f"-QuickTime:TrackCreateDate={exif_dt}",
                    f"-QuickTime:MediaCreateDate={exif_dt}",
                    f"-Keys:CreationDate={iso_dt}",
                ]
            )
        else:
            args.extend(
                [
                    f"-EXIF:DateTimeOriginal={exif_dt}",
                    f"-EXIF:CreateDate={exif_dt}",
                    f"-EXIF:ModifyDate={exif_dt}",
                    f"-XMP:DateCreated={iso_dt}",
                ]
            )

    if metadata.has_gps and metadata.latitude is not None and metadata.longitude is not None:
        lat = metadata.latitude
        lon = metadata.longitude
        if is_video:
            alt_val = metadata.altitude or 0.0
            args.append(f"-Keys:GPSCoordinates={lat:+.6f}{lon:+.6f}{alt_val:+.2f}/")
            args.append(f"-UserData:GPSCoordinates={lat:+.6f}{lon:+.6f}/")
        else:
            lat_ref = "N" if lat >= 0 else "S"
            lon_ref = "E" if lon >= 0 else "W"
            args.extend(
                [
                    f"-EXIF:GPSLatitude={abs(lat):.7f}",
                    f"-EXIF:GPSLatitudeRef={lat_ref}",
                    f"-EXIF:GPSLongitude={abs(lon):.7f}",
                    f"-EXIF:GPSLongitudeRef={lon_ref}",
                ]
            )
            if metadata.altitude is not None and abs(metadata.altitude) > 1e-7:
                alt_ref = 0 if metadata.altitude >= 0 else 1
                args.extend(
                    [
                        f"-EXIF:GPSAltitude={abs(metadata.altitude):.2f}",
                        f"-EXIF:GPSAltitudeRef={alt_ref}",
                    ]
                )

    if metadata.description:
        if is_video:
            args.append(f"-Keys:Description={metadata.description}")
        else:
            args.append(f"-EXIF:ImageDescription={metadata.description}")
            args.append(f"-XMP-dc:Description={metadata.description}")

    tags: list[str] = list(metadata.people)
    if write_album_tags and metadata.album_name:
        tags.append(metadata.album_name)
    for tag in dict.fromkeys(tags):
        args.append(f"-XMP-dc:Subject+={tag}")

    args.append(str(media_path))

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        _apply_file_mtime(media_path, metadata.taken_at)
        if result.returncode == 0:
            return "repaired"
        logger.warning(
            "ExifTool returned %s for %s: %s",
            result.returncode,
            media_path.name,
            (result.stderr or result.stdout).strip(),
        )
        return "mtime_only"
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("ExifTool failed on %s: %s", media_path.name, exc)
        _apply_file_mtime(media_path, metadata.taken_at)
        return "error"

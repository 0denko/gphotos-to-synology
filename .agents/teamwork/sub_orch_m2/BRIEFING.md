# Milestone M2 Briefing (`sub_orch_m2`)

- **Milestone**: M2 — Takeout, Dedup, Sources & Synology Client
- **Parent Orchestrator**: `c8b19b05da9e071b01096e473f316cf4`
- **Current Iteration**: Round 1 (Step 2B.a — Exploration)
- **Spawn count**: 3 / 20

## Scope Summary
1. **G9 — Strict Sidecar Matcher (`takeout/matcher.py`)**: Remove unnumbered base sidecar fallback for `(N)` duplicates; move stem-only checks after internal `"title"` disambiguation with stem-uniqueness/non-contradiction check; sort `directory.iterdir()` deterministically; handle case-only filename collisions safely; require uniqueness (`len(matches) == 1`) in title fallback and 46-char prefix fallback (including unique media prefix check).
2. **G10 — HEIC/HEIF Visual Fingerprinting & Undecodable State (`takeout/dedup.py`, `synology/scanner.py`)**: Register `pillow_heif.register_heif_opener()`; add `visual_status: str = ""` (`"ok" | "undecodable" | "skipped"`) to `MediaFingerprint`; log `WARNING` on image decode failure; update `ExistingLibraryScanner` to check `visual_done` / `visual_status` and pass `visual_status` to `upsert_existing_nas_media`.
3. **G11 & Quality Heuristics (`takeout/dedup.py`)**: Return `False` in `_aspect_ratios_compatible` when any dimension `<= 0`; use `.astimezone(UTC)` in `_timestamps_close`; tighten `is_perceptual_match` for unknown dimensions and `dist > 4` corroboration; require non-inferior sharpness on `compare_image_quality` Rule 3 (`>= 1.15x` file size).
4. **G12 — Extraction Decompression Limits & Temp Cleanup (`takeout/extractor.py`)**: Enforce `max_entries`, pre-flight `max_uncompressed_bytes` (defaulting to free space cap), `max_compression_ratio`, streaming byte counter, `skip_unsafe_entries: bool = False`, and `cleanup_stale_temp_files`.
5. **G13 & Metadata Details (`takeout/metadata.py`)**: Return `False` with `WARNING` when `piexif.load` raises; GPS DMS seconds=60 carry clamp; `OffsetTime*` `"+00:00"`; ASCII `ImageDescription` + UTF-8 `UserComment`; float `_parse_timestamp`; clear `WARNING` when non-JPEG lacks ExifTool.
6. **Minor — `GoogleDriveClient` (`sources/gdrive.py`)**: MD5 verification, atomic `0o600` token save, `last_error` & `connection_status()`, `find_takeout_folder_ids()` pagination, Range resume on `.part`.
7. **Minor — `LocalInboxScanner` (`sources/inbox.py`)**: Skip symlinks, require `(st_size, st_mtime_ns)` stability + archive readability check.
8. **Minor — `SynologyPhotosClient` (`synology/photos_api.py`)**: POST body `_sid`, `synology_ca_cert`, code `119` auto-relogin once, `_album_cache` invalidation + `invalidate_album_cache()`, root-level `resolve_item_id` scoping.
9. **Unit Tests**: Expand `test_takeout_matcher.py`, `test_metadata_and_importer.py`, `test_existing_nas_dedup.py`, `test_synology_api_and_security.py`, and create `test_gdrive_and_inbox.py`.

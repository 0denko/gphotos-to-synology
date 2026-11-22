# E2E Track Reviewer 1 Report (`reviewer_e2e_r1_1`)

**Role:** Reviewer 1 — Full-Suite Collection, Lint & Tiers 1–2 Opaque-Box Review  
**Iteration:** 1  
**Target Suite:** `TEST_INFRA.md`, `tests/e2e/conftest.py`, `tests/e2e/test_tier1_features.py` (30 tests), `tests/e2e/test_tier2_boundaries.py` (26 tests)  
**Parent Sub-Orchestrator:** `cefe08249ed6750e9786dd4091caf507`  
**Verdict:** `APPROVE`

---

## 1. Independent Lint & Collection Verification

All verification commands were executed independently from `/home/antigravity/gphotos-to-synology`:

### 1.1 Ruff Lint Verification (`.venv/bin/ruff check tests/e2e`)
- **Exit Code:** `0`
- **Warnings / Errors:** `0` (`All checks passed!`)

### 1.2 Full-Repository Pytest Collection (`.venv/bin/pytest --collect-only -q`)
- **Exit Code:** `0`
- **Import / Collection Errors:** `0`
- **Output:** `128 tests collected in 0.50s` (56 existing unit/integration tests + 72 E2E tests collected cleanly without modifying `tests/__init__.py`).

### 1.3 Scoped E2E Pytest Collection (`.venv/bin/pytest --collect-only -q tests/e2e`)
- **Exit Code:** `0`
- **Import / Collection Errors:** `0`
- **Output:** `72 tests collected in 0.03s`
  - `tests/e2e/test_tier1_features.py`: **30 tests**
  - `tests/e2e/test_tier2_boundaries.py`: **26 tests**
  - `tests/e2e/test_tier3_interactions.py`: **10 tests**
  - `tests/e2e/test_tier4_workloads.py`: **6 tests**

### 1.4 Working Tree Hygiene (`git status --short`)
- Verified that running SPK build and E2E tests leaves zero stray `build/` or temporary files in `/home/antigravity/gphotos-to-synology`.

---

## 2. Code Review of `TEST_INFRA.md`, `tests/e2e/conftest.py`, `test_tier1_features.py`, and `test_tier2_boundaries.py`

### 2.1 `TEST_INFRA.md` & `tests/e2e/conftest.py`
- **Opaque-Box Fidelity**: `conftest.py` interacts strictly through public contracts defined in `PROJECT.md § Interface Contracts` #1–#6: FastAPI `create_app()`, `/usr/bin/python3` subprocess invocation of `synology-spk/ui/proxy.cgi` and `oauth_callback.cgi`, CLI invocation of `synology-spk/build_spk.py` and `/bin/sh` (`dash`) execution of SPK lifecycle scripts (`start-stop-status`, `preupgrade`, `preuninst`), Node `vm` execution of `src/gphotos_synology/web/static/app.js` with browser-faithful `textContent`/`innerHTML` quote semantics, and public Python subsystem APIs.
- **Host Isolation**: `_isolate_e2e_env` (`autouse=True`) strips all `GPHOTOS_*`, `SYNOLOGY_*`, `GOOGLE_*`, `API_TOKEN`, `PROXY_SECRET*`, `WATCHTOWER_*`, and proxy environment variables (`NO_PROXY`, `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`), and `make_isolated_settings` passes `_env_file=None` so local `.env` or host proxy state never leaks into tests.
- **Deterministic Synthetic Media & Servers**: `make_patterned_image` renders a canonical 640×480 scene and resizes via `Image.Resampling.LANCZOS`, ensuring multi-resolution copies of the same seed have perceptual dHash Hamming distance `<= 3` and reliably trigger quality upgrades. All network calls use local `ThreadingHTTPServer` or `httpx.MockTransport` fixtures.

### 2.2 `tests/e2e/test_tier1_features.py` (30 Tests — Category-Partition / Feature Coverage)
- Contains **6 isolated happy-path tests per major subsystem area** across all 5 areas (30 tests total, exceeding the `>= 5` per area requirement):
  - **Area A (Security, Auth, Profiles & Config)**: `test_t1_a1`..`test_t1_a6` (6 tests) — C1 exempt routes, direct token + session cookie login, proxy secret + DSM user/admin identity, profile CRUD + lazy OAuth inheritance, G5 secure `Settings` defaults + `SECRET_FIELDS`, and G8 `quarantine_dir_override`.
  - **Area B (Takeout Extraction, Sidecar Matching, Metadata & Dedup)**: `test_t1_b1`..`test_t1_b6` (6 tests) — ZIP/TAR.GZ extraction + `cleanup_stale_temp_files`, strict sidecar matcher happy paths, float timestamps + `piexif` `OffsetTimeOriginal`/ASCII/UTF-8 `UserComment`, ExifTool CLI invocation with `OffsetTimeOriginal`, real HEIC visual fingerprinting via `pillow-heif` (`visual_status='ok'`), and `StateDB` `media_dhash_buckets` + `original_sha256`-first ordering.
  - **Area C (Pipeline, Quality Upgrades, Albums, Scheduler & Notifier)**: `test_t1_c1`..`test_t1_c6` (6 tests) — Full pipeline import & finalize, quality upgrade swap + quarantine + restore to `old_rel_path`, pending album indexing without burning `retry_count`, scheduler pause (`next_run_at=None`) + manual run `last_run_at`, sync `Notifier.send()` + non-blocking `Notifier.send_async()`, and `purge_all_quarantined` + `_op_lock`.
  - **Area D (Sources & Synology Client/Scanner)**: `test_t1_d1`..`test_t1_d6` (6 tests) — `LocalInboxScanner` discovery & finalize actions, `ExistingLibraryScanner` read-only scan & ignored system directories, `SynologyPhotosClient` POST-body `_sid` + album sync + item resolution, `GoogleDriveClient` `connection_status()` + paginated folder search + MD5-verified download, `SynologyFolderImporter` sanitization & collision safety, and `StateDB.delete_state` + log persistence.
  - **Area E (Packaging, SPK Lifecycle, Updater & Frontend)**: `test_t1_e1`..`test_t1_e6` (6 tests) — `build_spk.py` `1.2.0-0001` metadata & archive structure, `start-stop-status` `0600` 64-hex `proxy.secret` creation & daemon lifecycle, `UpdateService` newer semver + `.sha256` checksum surfacing, `proxy.cgi` & `oauth_callback.cgi` authenticated forwarding, `node --check` + `app.js` `X-Requested-With` in Node `vm`, and unified `1.2.0` packaging/dependency pins.

### 2.3 `tests/e2e/test_tier2_boundaries.py` (26 Tests — Boundary Value Analysis & Corner Cases)
- Comprehensively verifies at/inside/outside boundaries, negative inputs, and fail-closed security gates across every Critical, Major, and Minor item in `PROJECT.md` and `Collab.md`:
  - **C1/C2/G3/G4/G5/G6**: Unauthenticated & forged proxy headers (`401`), auto-generated `0600` `api.token` & blocked `MUTABLE_UI_FIELDS`, `docker-compose.yml` loopback binding, non-admin DSM user profile isolation & admin endpoint rejection (`403`), OAuth callback fail-closed state store (fresh profile with no stored state, missing state, prefix-spoofed profile state, valid one-time consumption, replay rejection, >600s TTL expiration, URL-encoded error parameter), G4 password clearing on `synology_url` change (including masked `"********"` sentinel) & invalid URL scheme rejection, `.env.example` TLS defaults, and G6 CSRF `X-Requested-With` enforcement across `POST`/`PUT`/`PATCH`/`DELETE`.
  - **G7/G8/G9/G10/G11/G12/G13/G14**: `UpdateService` non-downgrade (`v1.1.0` < `1.2.0`) & same-version SHA-churn rejection + HTTP 500 error surfacing + POST-only Watchtower on HTTP 405, CI workflow (`--locked`, `--collect-all pillow_heif`, minimal permissions, `.sha256`) & `.dockerignore`/`docker-compose.yml` hardening, SPK `preuninst` `UPGRADE` vs `UNINSTALL` backup excluding `proxy.secret` + `preupgrade` 3-snapshot rotation, G9 strict sidecar matcher (`(1)` no unnumbered fallback, ambiguous title, ambiguous 46-char prefix, stem-only after title, case-safe index), G10 corrupt `.heic` warning & `visual_status='undecodable'` persistent scanner skip, G11 perceptual dedup (`dist=5` corroboration, unknown dimensions rejection, `.astimezone(UTC)` cross-offset matching, `media_dhash_buckets` vs full-scan recall, sharpness-guarded ≥15% size rule), G12 extraction caps (`max_entries`, `max_uncompressed_bytes`, `max_compression_ratio`, ZipSlip/TarSlip fail-closed vs `skip_unsafe_entries=True`), G13 corrupt EXIF APP1 byte preservation (`status == "mtime_only"`) + GPS DMS 60.0s carry clamp + non-JPEG warning, and G14 personal & shared `custom_subfolder` traversal & symlink escape rejection.
  - **High-Value Minors**: Reserved profile slugs (`processed`, `profiles`), duplicate `photos_dir` rejection, corrupt `runtime_settings.json` / `profiles.json` backup & recovery, corrupt non-default profile SQLite DB startup resilience, `proxy.cgi` fail-closed `403` / invalid username charset `403` / 1 MiB body cap `413` / offline `200` HTML vs `503` JSON / CRLF header injection stripping, `oauth_callback.cgi` query parameter allowlist (`code`, `state`, `error`) + `_public_base_url` ignoring untrusted `X-Forwarded-Host`, `app.js` `escapeHtml` quote escaping against attribute-breakout XSS, `LocalInboxScanner` symlink skipping & `(size, mtime_ns)` stability reset, `GoogleDriveClient` MD5 mismatch rejection & refresh error surfacing, `SynologyPhotosClient` root-level `resolve_item_id` scoping & code `119` auto-relogin, `purge_quality_upgrade` non-`quarantined` status & path traversal rejection (`ValueError` / HTTP `400`), and UI-only (`noarch`) SPK `status` health probing.

---

## 3. Independent Execution of Tiers 1 & 2 (`.venv/bin/pytest tests/e2e/test_tier1_features.py tests/e2e/test_tier2_boundaries.py -v --tb=short`)

- **Command Result:** `12 passed, 44 failed in 26.95s`
- **Passing Tests (12 Passed — M1 & Baseline Features):**
  1. `test_t1_a5_v120_secure_settings_defaults_and_config_update` (M1 G5/C1 defaults & `SECRET_FIELDS`)
  2. `test_t1_a6_quarantine_dir_override_property` (M1 G8 `quarantine_dir_override`)
  3. `test_t1_b2_strict_sidecar_matching_happy_paths` (Baseline + G9 happy paths)
  4. `test_t1_b6_statedb_dhash_buckets_and_hash_lookup_ordering` (M1 G11 `media_dhash_buckets` & Minor #23 hash ordering)
  5. `test_t1_c1_pipeline_run_cycle_full_import_and_finalize` (Pipeline happy-path import & finalize)
  6. `test_t1_c3_album_sync_and_pending_index_without_burning_retries` (M1 Minor #17 pending album retry guard)
  7. `test_t1_d1_inbox_scanner_discovery_and_finalize_actions` (Inbox discovery & post actions)
  8. `test_t1_d2_existing_library_scanner_read_only_and_ignored_dirs` (Read-only NAS scan & ignored dirs)
  9. `test_t1_d5_synology_folder_importer_sanitization_and_collision_safety` (Importer sanitization & collision safety)
  10. `test_t1_d6_statedb_delete_state_and_periodic_log_prune` (M1 G3 `StateDB.delete_state` & logs)
  11. `test_t1_e6_unified_version_and_packaging_metadata` (M1 G7/G10 unified `1.2.0`, `pillow-heif`, `<12.3.0`, `uv.lock`)
  12. `test_t2_g4_url_change_clears_stored_password_and_rejects_bad_schemes` (M1 G4 `ConfigManager.update` password clear & URL scheme validation)

- **Audit of All 44 Failing Tests (19 in Tier 1, 25 in Tier 2):**
  Every single failure was verified against the traceback log and confirmed to fail **solely** because the corresponding M2–M5 `v1.2.0` feature has not yet landed in `src/` or `synology-spk/`:
  - **M2 Pending Features (13 failures):** `test_t1_b1` (`cleanup_stale_temp_files` missing), `test_t1_b3` (float timestamp `"1720000000.5"` not parsed), `test_t1_b4` (ExifTool missing `OffsetTimeOriginal=+00:00`), `test_t1_b5` (`pillow_heif` & `visual_status='ok'` not wired in `dedup.py`), `test_t1_d3` (`_sid` sent in GET query string instead of POST body), `test_t1_d4` (`GoogleDriveClient.connection_status()` missing), `test_t2_g9` (`IMG_1234(1).jpg` still falls back to unnumbered `IMG_1234.jpg.json`), `test_t2_g10` (corrupt `.heic` `visual_status='undecodable'` not set), `test_t2_g11` (`is_perceptual_match` at `dist=5` without corroboration still returns `True`), `test_t2_g12` (`extract_archive` missing `max_entries` kwarg), `test_t2_g13` (corrupt EXIF overwritten instead of returning `'mtime_only'`), `test_t2_minor_inbox_skips_symlinks_and_tracks_mtime_stability` (symlink not skipped / `mtime_ns` not tracked), `test_t2_minor_gdrive_md5_mismatch_rejected_and_refresh_error_surfaced` (`connection_status()` missing), `test_t2_minor_synology_client_code_119_relogin_and_root_resolve_scoping` (root `resolve_item_id` matches subfolder item). *(14 tests total across Tiers 1–2)*
  - **M3 Pending Features (15 failures):** `test_t1_a1` (`/api/auth/status` 404), `test_t1_a2` (`/api/auth/session` 404), `test_t1_a3` (`/api/profiles` missing `is_admin`), `test_t1_a4` (`ProfileRegistry` lazy OAuth inheritance not wired), `test_t1_e4` (`proxy.cgi` missing `X-Proxy-Secret`/`X-DSM-Admin`/`X-Requested-With`), `test_t1_e5` (`app.js` missing `X-Requested-With`), `test_t2_c1_unauthenticated_and_spoofed_proxy_headers_rejected` (unauthenticated `/api/status` returns 200), `test_t2_c1_auto_generated_api_token_is_0600_and_non_mutable_fields_blocked` (`api.token` not auto-generated), `test_t2_c2_non_admin_cannot_access_other_profile_or_admin_actions` (`/api/profiles` missing `is_admin` & C2 enforcement), `test_t2_g3_oauth_callback_rejects_missing_unknown_spoofed_expired_replayed_state` (OAuth callback fails open when no state stored), `test_t2_g6_csrf_header_required_on_all_mutating_api_methods` (CSRF not enforced), `test_t2_g14_personal_and_shared_subfolder_rejects_traversal_and_symlink_escape` (personal space subfolder containment missing), `test_t2_minor_reserved_profile_ids_duplicate_photos_dir_and_corrupt_files_resilience` (reserved slug `'processed'` allowed), `test_t2_minor_proxy_cgi_limits_offline_503_crlf_and_fail_closed_auth` (`proxy.cgi` crashes on CRLF username instead of returning 403), `test_t2_minor_oauth_callback_cgi_allowlists_query_params_and_direct_ignores_forwarded_host` (`oauth_callback.cgi` forwards unallowlisted query params), `test_t2_minor_app_js_escape_html_prevents_attribute_breakout_xss` (`escapeHtml` does not escape `"` to `&quot;`). *(16 tests total across Tiers 1–2)*
  - **M4 Pending Features (5 failures):** `test_t1_c2` (`pipeline.py` does not record `old_rel_path` on quality upgrade), `test_t1_c4` (manual `index_existing_only` run does not set `last_run_at`), `test_t1_c5` (`Notifier.send_async()` not implemented), `test_t1_c6` (`MigrationPipeline._op_lock` not present), `test_t2_minor_purge_rejects_restored_status_and_path_traversal` (`purge_quality_upgrade` does not raise `ValueError` on `status == 'restored'`).
  - **M5 Pending Features (9 failures):** `test_t1_e1` (`build_spk.py` version still `1.1.0-0003`), `test_t1_e2` (`start-stop-status` does not create `proxy.secret`), `test_t1_e3` (`UpdateService` does not surface `.sha256` checksum fields), `test_t2_c1_docker_compose_binds_loopback_by_default` (`docker-compose.yml` still binds `8765:8765`), `test_t2_g5_env_example_uses_https_and_verify_ssl_true` (`.env.example` still uses `SYNOLOGY_VERIFY_SSL=false`), `test_t2_g7_updater_rejects_downgrade_same_version_surfaces_error_and_post_only_watchtower` (`UpdateService` flags downgrade `v1.1.0` as `update_available=True`), `test_t2_g7_ci_workflow_docker_and_dockerignore_hardening_contracts` (`docker-publish.yml` missing `--locked`), `test_t2_g8_spk_preuninst_upgrade_guard_excludes_secret_and_preupgrade_keeps_3` (`preuninst` does not back up to `${PHOTOS_DIR}/.gphotos-migrator-backup`), `test_t2_minor_ui_only_spk_status_probes_health_instead_of_blind_zero` (UI-only SPK `status` exits 0 when backend offline).

Zero test-code defects, broken fixtures, or mismatched contract signatures exist in `TEST_INFRA.md`, `tests/e2e/conftest.py`, `tests/e2e/test_tier1_features.py`, or `tests/e2e/test_tier2_boundaries.py`.

---

## 4. Final Verdict

**Verdict:** `APPROVE`

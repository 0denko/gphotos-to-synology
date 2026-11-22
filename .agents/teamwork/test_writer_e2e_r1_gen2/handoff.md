# E2E Test Writer Handoff (`test_writer_e2e_r1_gen2`)

**Track:** E2E Testing Track (`gphotos-to-synology` `v1.2.0`)  
**Parent Sub-Orchestrator:** `cefe08249ed6750e9786dd4091caf507`  
**Status:** COMPLETED (0 lint issues, 0 import/collection errors, 0 test-code bugs across all 72 E2E tests)

---

## 1. Deliverables Summary

1. **[TEST_INFRA.md](file:///home/antigravity/gphotos-to-synology/TEST_INFRA.md)** (144 lines):
   - Documents the opaque-box testing philosophy and 4-tier methodology (Category-Partition, Boundary Value Analysis, Pairwise Combinatorial, Real-World Workloads).
   - Provides a complete 34-row Feature Inventory Traceability Matrix mapping every feature `#1`–`#34` (`C1`, `C2`, `G1`–`G14`, Minors `#17`–`#34`) in `PROJECT.md § Feature Inventory` to concrete E2E test functions across Tiers 1–4.
   - Provides a complete Interface Contracts Traceability Matrix mapping `PROJECT.md § Interface Contracts` `#1`–`#6` to concrete E2E test functions.
   - Documents all opaque-box fixtures/builders in `tests/e2e/conftest.py` and exact execution commands for each tier.

2. **4-Tier Opaque-Box E2E Test Suite (`tests/e2e/`) — 72 Total E2E Tests**:
   - **[tests/e2e/conftest.py](file:///home/antigravity/gphotos-to-synology/tests/e2e/conftest.py)**:
     - Ensures `tests/e2e` is on `sys.path` so both bare `.venv/bin/pytest` and `.venv/bin/pytest tests/e2e` collect 100% of tests with 0 import errors without touching `tests/__init__.py`.
     - Strips all application and proxy environment variables (`_isolate_e2e_env`, including `NO_PROXY`/`HTTP_PROXY`/`HTTPS_PROXY` so `httpx` never chokes on host proxy env vars).
     - Provides `make_isolated_settings`, `make_csrf_headers`, `make_proxy_headers`, `make_token_headers`.
     - Provides canonical 640×480 LANCZOS-scaled synthetic image generator (`make_patterned_image`, `save_jpeg`, `save_png`, `save_heic`, `save_corrupt_exif_jpeg`), Takeout ZIP/TAR.GZ builders (`build_zip_archive`, `build_tar_gz_archive`), and stub ExifTool builder (`make_stub_exiftool`).
     - Provides local `ThreadingHTTPServer` fixtures (`mock_dsm_server`, `mock_webhook_server`, `mock_backend_server`), live loopback Uvicorn server (`start_live_fastapi_server`), CGI `/usr/bin/python3` subprocess runner (`run_cgi`), SPK builder & `/bin/sh` (`dash`) lifecycle runner (`build_test_spk`, `extract_spk`, `parse_spk_info`, `run_posix_script`), and browser-faithful Node `vm` `app.js` harness (`run_app_js_scenario`).
   - **[tests/e2e/test_tier1_features.py](file:///home/antigravity/gphotos-to-synology/tests/e2e/test_tier1_features.py)** — **30 tests** (6 isolated happy-path tests across each of the 5 subsystem areas A–E).
   - **[tests/e2e/test_tier2_boundaries.py](file:///home/antigravity/gphotos-to-synology/tests/e2e/test_tier2_boundaries.py)** — **26 tests** (boundary value analysis, negative/invalid inputs, fail-closed security rejections, decompression/ratio limits, ZipSlip/TarSlip, corrupt EXIF preservation, DMS 60.0s carry clamp, ambiguous sidecars, non-downgrade checks, CGI 1 MiB body cap/503 offline JSON/CRLF stripping, corrupt file recovery).
   - **[tests/e2e/test_tier3_interactions.py](file:///home/antigravity/gphotos-to-synology/tests/e2e/test_tier3_interactions.py)** — **10 tests** (pairwise cross-feature interactions across CGI subprocesses, live FastAPI server, SQLite WAL DB, pipeline cycles, SPK POSIX scripts, and Node `app.js`).
   - **[tests/e2e/test_tier4_workloads.py](file:///home/antigravity/gphotos-to-synology/tests/e2e/test_tier4_workloads.py)** — **6 tests** (realistic end-to-end NAS application journeys W1–W6).

---

## 2. Test-Harness Bugs Caught & Fixed During Failure Audit

Auditing every single failure from `.venv/bin/pytest tests/e2e -v` surfaced 5 subtle test-harness bugs in the prior interrupted draft, all of which were fixed in `tests/e2e/`:

1. **Invalid `dedup_mode` literal in `test_t1_a5_v120_secure_settings_defaults_and_config_update`**: Fixed `"exact_pixel"` → `"exact_and_pixels"` (test now **PASSES** against M1 `config.py`).
2. **Host `NO_PROXY` environment variable breaking `httpx.Client()` (`httpx.InvalidURL: Invalid port: ':1]'`)**: Added `NO_PROXY`/`no_proxy`/`HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` to `_ISOLATED_ENV_VARS` in `tests/e2e/conftest.py`. This fixed `start_live_fastapi_server`, `SynologyPhotosClient`, and `Notifier` in all E2E tests (and made `test_t1_c3_album_sync_and_pending_index_without_burning_retries` **PASS**).
3. **Python 3.11.2 `TarFile.extractall(filter="data")` incompatibility**: Removed `filter="data"` in `extract_spk()` (`conftest.py`) because Python 3.11.2 predates the PEP 706 backport in 3.11.4. All SPK build/unpack tests now unpack cleanly and assert directly against the SPK contents and `/bin/sh` script behavior.
4. **Multi-resolution synthetic image perceptual dHash & sharpness consistency (`make_patterned_image`)**: Updated `make_patterned_image` in `conftest.py` to render at a canonical `640x480` scene and resize via `Image.Resampling.LANCZOS`, ensuring multi-resolution copies of the same `seed` have dHash Hamming distance `0..3` and trigger `compare_image_quality` resolution upgrades reliably across `test_t1_c2`, `test_t2_minor_purge_rejects_restored_status_and_path_traversal`, `test_t3_p4`, and `test_t4_w3`.
5. **Node `app.js` harness `renderStatus` stats keys & `form.elements.namedItem`**: Populated all `StateDB.get_stats()` fields in `default_routes["/api/status"]` and added `elements.namedItem` + `reset()` on `MockElement` in `conftest.py` so `refreshDashboard()` reaches `renderArchives()` and directly tests `escapeHtml` quote escaping in `archives-tbody`.

---

## 3. Verification Command Outputs

### 3.1 `.venv/bin/ruff check tests/e2e`
```text
$ .venv/bin/ruff check tests/e2e
All checks passed!
```
(Exit code: `0`)

### 3.2 `.venv/bin/pytest --collect-only -q`
```text
$ .venv/bin/pytest --collect-only -q
128 tests collected in 0.49s
```
(Exit code: `0` — collects all 56 existing unit/integration tests + all 72 E2E tests with 0 errors)

### 3.3 `.venv/bin/pytest --collect-only -q tests/e2e`
```text
$ .venv/bin/pytest --collect-only -q tests/e2e
72 tests collected in 0.03s
```
(Exit code: `0` — 30 Tier 1 + 26 Tier 2 + 10 Tier 3 + 6 Tier 4 = 72 tests)

### 3.4 `git status --short`
```text
$ git status --short
 M pyproject.toml
 M src/gphotos_synology/__init__.py
 M src/gphotos_synology/config.py
 M src/gphotos_synology/database.py
?? .agents/
?? PROJECT.md
?? TEST_INFRA.md
?? tests/e2e/
?? tests/test_config.py
?? tests/test_database.py
?? uv.lock
```
*(Note: `pyproject.toml`, `uv.lock`, `src/gphotos_synology/{__init__,config,database}.py`, and `tests/test_{config,database}.py` belong to M1 which landed concurrently; only `TEST_INFRA.md`, `tests/e2e/**`, and `.agents/teamwork/test_writer_e2e_r1_gen2/handoff.md` were written/modified by this worker.)*

---

## 4. Complete Breakdown of `.venv/bin/pytest tests/e2e -v` (12 Passed, 60 Failed Pending M2–M5)

```text
================== 60 failed, 12 passed, 1 warning in 34.13s ===================
```

### 4.1 Currently Passing E2E Tests (12 Passed — M1 & Baseline Features)
1. `test_t1_a5_v120_secure_settings_defaults_and_config_update` (M1 G5/C1 defaults & `SECRET_FIELDS`)
2. `test_t1_a6_quarantine_dir_override_property` (M1 G8 `quarantine_dir_override`)
3. `test_t1_b2_strict_sidecar_matching_happy_paths` (Baseline + G9 happy paths)
4. `test_t1_b6_statedb_dhash_buckets_and_hash_lookup_ordering` (M1 G11 `media_dhash_buckets` & Minor #23 hash ordering)
5. `test_t1_c1_pipeline_run_cycle_full_import_and_finalize` (Pipeline happy-path import & finalize)
6. `test_t1_c3_album_sync_and_pending_index_without_burning_retries` (M1 Minor #17 pending album retry guard + mock DSM sync)
7. `test_t1_d1_inbox_scanner_discovery_and_finalize_actions` (Inbox discovery & post actions)
8. `test_t1_d2_existing_library_scanner_read_only_and_ignored_dirs` (Read-only NAS scan & ignored system dirs)
9. `test_t1_d5_synology_folder_importer_sanitization_and_collision_safety` (Importer sanitization & collision safety)
10. `test_t1_d6_statedb_delete_state_and_periodic_log_prune` (M1 G3 `StateDB.delete_state` & logs)
11. `test_t1_e6_unified_version_and_packaging_metadata` (M1 G7/G10 unified `1.2.0`, `pillow-heif`, `<12.3.0`, `uv.lock`)
12. `test_t2_g4_url_change_clears_stored_password_and_rejects_bad_schemes` (M1 G4 `ConfigManager.update` password clear & URL scheme validation)

### 4.2 Currently Failing E2E Tests (60 Failed — 100% Mapped to Pending M2–M5 Implementation)

| Test Function | Pending Milestone & Feature | Exact Failure Reason Against Current Code |
|---|---|---|
| `test_t1_a1_public_endpoints_exempt_from_auth` | M3 — C1 | `GET /api/auth/status` returns `404` (endpoint not added yet) |
| `test_t1_a2_direct_api_token_and_session_cookie_auth` | M3 — C1 | `POST /api/auth/session` returns `404` (endpoint not added yet) |
| `test_t1_a3_proxy_secret_auth_with_dsm_admin_and_user` | M3 — C1, C2 | `GET /api/profiles` response missing `is_admin` field |
| `test_t1_a4_admin_profile_crud_and_lazy_oauth_inheritance` | M3 — Minor #19 | `ProfileRegistry` does not pass `fallback_settings_provider` to profile `ConfigManager` yet |
| `test_t1_b1_extract_zip_and_targz_and_cleanup_stale_tmp` | M2 — G12, Minor #32 | `cleanup_stale_temp_files` not implemented in `takeout.extractor` yet |
| `test_t1_b3_apply_metadata_piexif_offset_ascii_unicode_and_float_ts` | M2 — Minor #30 | `parse_sidecar_json` returns `taken_at=None` on float timestamp `"1720000000.5"` |
| `test_t1_b4_apply_metadata_with_stub_exiftool_happy_path` | M2 — Minor #30 | `apply_metadata` ExifTool argv missing `OffsetTimeOriginal=+00:00` |
| `test_t1_b5_heic_visual_fingerprinting_via_pillow_heif` | M2 — G10 | `dedup.py` has not registered `pillow_heif` or populated `MediaFingerprint.visual_status='ok'` yet |
| `test_t1_c2_quality_upgrade_swap_quarantine_and_restore_to_old_rel_path` | M4 — G2, Minor #20 | `pipeline.py` does not record `old_rel_path` when creating quality upgrades yet (`'' != 'LegacyAlbum/2023/vacation_old.jpg'`) |
| `test_t1_c4_scheduler_pause_clears_next_run_and_manual_run_sets_last_run_at` | M4 — Minor #32 | `BackgroundScheduler` manual `index_existing_only` run does not set `last_run_at` yet |
| `test_t1_c5_notifier_sync_send_and_nonblocking_send_async` | M4 — Minor #25 | `Notifier.send_async()` not implemented in `notifications.py` yet |
| `test_t1_c6_purge_all_quarantined_and_op_lock_presence` | M4 — Minor #20 | `MigrationPipeline._op_lock` not added to `pipeline.py` yet |
| `test_t1_d3_synology_photos_client_login_post_sid_album_and_resolve` | M2 — Minor #27 | `SynologyPhotosClient` sends `_sid` in URL query string on GET requests instead of POST body |
| `test_t1_d4_gdrive_connection_status_pagination_and_verified_md5_download` | M2 — Minor #21 | `GoogleDriveClient.connection_status()` not implemented in `gdrive.py` yet |
| `test_t1_e1_build_spk_metadata_and_archive_structure` | M5 — G7 | `build_spk.py` `VERSION` is still `"1.1.0-0003"` instead of `"1.2.0-0001"` |
| `test_t1_e2_spk_start_stop_status_creates_0600_proxy_secret` | M5 — C1 | SPK `start-stop-status` does not generate `${PKG_VAR}/proxy.secret` yet |
| `test_t1_e3_updater_detects_newer_semver_with_sha256_checksum` | M5 — G7 | `UpdateService` does not surface `.sha256` checksum metadata in status dict yet |
| `test_t1_e4_proxy_and_oauth_cgi_forward_authenticated_requests` | M3 — C1, G6 | `proxy.cgi` does not inject `X-Proxy-Secret`, `X-DSM-Admin`, or forward `X-Requested-With` yet |
| `test_t1_e5_frontend_js_syntax_and_app_js_happy_path_in_node` | M3 — G6 | `app.js` `apiFetch` does not attach `X-Requested-With` header yet |
| `test_t2_c1_unauthenticated_and_spoofed_proxy_headers_rejected` | M3 — C1 | Unauthenticated `GET /api/status` returns `200` instead of `401` (`web/security.py` middleware not wired yet) |
| `test_t2_c1_auto_generated_api_token_is_0600_and_non_mutable_fields_blocked` | M3 — C1 | `create_app()` does not auto-generate `data_dir / "api.token"` (`0600`) yet |
| `test_t2_c1_docker_compose_binds_loopback_by_default` | M5 — C1 | `docker-compose.yml` still has `"8765:8765"` instead of `"127.0.0.1:8765:8765"` |
| `test_t2_c2_non_admin_cannot_access_other_profile_or_admin_actions` | M3 — C2 | `GET /api/profiles` missing `is_admin` and C2 profile isolation enforcement |
| `test_t2_g3_oauth_callback_rejects_missing_unknown_spoofed_expired_replayed_state` | M3 — G3 | `/api/oauth/google/callback` fails open when no state is stored in `StateDB` |
| `test_t2_g5_env_example_uses_https_and_verify_ssl_true` | M5 — G5 | `.env.example` still has `SYNOLOGY_VERIFY_SSL=false` and HTTP URL |
| `test_t2_g6_csrf_header_required_on_all_mutating_api_methods` | M3 — G6 | Mutating `/api/*` requests without `X-Requested-With` are not rejected with `403` yet |
| `test_t2_g7_updater_rejects_downgrade_same_version_surfaces_error_and_post_only_watchtower` | M5 — G7 | `UpdateService` compares commit SHA instead of non-downgrading semver (`v1.1.0` reports `update_available=True`) |
| `test_t2_g7_ci_workflow_docker_and_dockerignore_hardening_contracts` | M5 — G7, Minor #29 | `.github/workflows/docker-publish.yml` does not use `--locked` yet |
| `test_t2_g8_spk_preuninst_upgrade_guard_excludes_secret_and_preupgrade_keeps_3` | M5 — G8 | SPK `preuninst` is still a no-op and does not back up to `${PHOTOS_DIR}/.gphotos-migrator-backup` yet |
| `test_t2_g9_strict_sidecar_matcher_rejects_unnumbered_fallback_and_ambiguity` | M2 — G9 | `find_sidecar_json` still falls back from `IMG_1234(1).jpg` to unnumbered `IMG_1234.jpg.json` |
| `test_t2_g10_undecodable_heic_logs_warning_and_persists_stable_skip` | M2 — G10 | `compute_media_fingerprint` does not set `visual_status='undecodable'` or log `WARNING` yet |
| `test_t2_g11_perceptual_dedup_corroboration_unknown_dims_tz_buckets_and_sharpness` | M2 — G11 | `is_perceptual_match` at `dist=5` without timestamp/stem corroboration still returns `True` |
| `test_t2_g12_extraction_caps_and_zipslip_tarslip_skip_vs_fail_closed` | M2 — G12 | `extract_archive` does not accept `max_entries` / decompression limit kwargs yet |
| `test_t2_g13_corrupt_exif_preserved_dms_60s_clamped_and_non_jpeg_warns` | M2 — G13 | `apply_metadata` on corrupt EXIF APP1 wipes EXIF and returns `'repaired'` instead of `'mtime_only'` |
| `test_t2_g14_personal_and_shared_subfolder_rejects_traversal_and_symlink_escape` | M3 — G14 | `ProfileRegistry.create_profile` does not enforce `_resolve_contained_subfolder` on personal space yet |
| `test_t2_minor_reserved_profile_ids_duplicate_photos_dir_and_corrupt_files_resilience` | M3 — Minor #18 | `ProfileRegistry.create_profile` allows reserved slug `'processed'` |
| `test_t2_minor_proxy_cgi_limits_offline_503_crlf_and_fail_closed_auth` | M3 — Minor #28 | `proxy.cgi` does not validate `get_dsm_user()` charset (crashes in `putheader` on CRLF username instead of `403`) |
| `test_t2_minor_oauth_callback_cgi_allowlists_query_params_and_direct_ignores_forwarded_host` | M3 — Minor #28 | `oauth_callback.cgi` forwards arbitrary query params (`profile`, `evil`) instead of allowlisting `code`, `state`, `error` |
| `test_t2_minor_app_js_escape_html_prevents_attribute_breakout_xss` | M3 — Minor #26 | `app.js` `escapeHtml` does not escape `"` (`&quot;`) in `renderArchives` `title` attribute |
| `test_t2_minor_inbox_skips_symlinks_and_tracks_mtime_stability` | M2 — Minor #31 | `LocalInboxScanner` does not skip symlinks or track `(size, mtime_ns)` stability yet |
| `test_t2_minor_gdrive_md5_mismatch_rejected_and_refresh_error_surfaced` | M2 — Minor #21 | `GoogleDriveClient` missing `connection_status()` method |
| `test_t2_minor_synology_client_code_119_relogin_and_root_resolve_scoping` | M2 — Minor #27 | `SynologyPhotosClient.resolve_item_id("IMG_ROOT.jpg")` matches subfolder item instead of scoping to root folder |
| `test_t2_minor_purge_rejects_restored_status_and_path_traversal` | M4 — Minor #20 | `MigrationPipeline.purge_quality_upgrade` does not reject `status != 'quarantined'` with `ValueError` yet |
| `test_t2_minor_ui_only_spk_status_probes_health_instead_of_blind_zero` | M5 — Minor #29 | UI-only (`noarch`) SPK `start-stop-status status` exits `0` blindly when backend is offline |
| `test_t3_p1_proxy_cgi_with_real_fastapi_c1_c2_g6` | M3 — C1, C2, G6 | Real `proxy.cgi` → live FastAPI allows non-admin `alice` to read `bob`'s status (`200 != 403`) |
| `test_t3_p2_oauth_callback_cgi_with_real_fastapi_g3_c2` | M3 — C2, G3 | Real `oauth_callback.cgi` → live FastAPI allows `bob` to complete `alice`'s OAuth callback |
| `test_t3_p3_g1_failed_file_delete_guard_and_retry_counter_reset` | M4 — G1 | `MigrationPipeline.run_cycle()` deletes inbox archive despite `failed_files > 0` |
| `test_t3_p4_exif_repair_post_repair_dedup_upgrade_restore_and_purge_reject` | M4 — G2, Minor #20 | `pipeline.py` does not record `old_rel_path` on quality upgrade yet |
| `test_t3_p5_g10_heic_undecodable_scanner_incremental_skip_and_g11_perceptual_dedup` | M2 — G10, G11 | `ExistingLibraryScanner.scan()` re-hashes undecodable `.heic` on cycle 2 (`s2.newly_indexed == 1`) |
| `test_t3_p6_multi_profile_lazy_oauth_delete_cleanup_and_recreate` | M3 — Minor #18, #19 | `ProfileRegistry` does not wire lazy OAuth inheritance or disk deletion on `delete_profile` yet |
| `test_t3_p7_g4_url_change_password_clear_and_synology_post_sid_relogin` | M2 — Minor #27 | G4 password clear succeeds, then `SynologyPhotosClient.list_albums()` fails on code `119` (auto-relogin not implemented yet) |
| `test_t3_p8_spk_deterministic_double_build_lifecycle_and_updater_checksum` | M5 — G7, G8 | Two SPK builds with identical `--build-sha` and `--build-date` have different SHA-256 digests |
| `test_t3_p9_frontend_app_js_xss_defense_header_merge_and_cgi_routing` | M3 — G6, Minor #26 | `app.js` in CGI mode does not escape `"` (`&quot;`) in `renderArchives` |
| `test_t3_p10_album_pending_index_root_scope_scanner_isolation_and_throttled_async_notifier` | M4 — Minor #24, #25 | `index_existing_library()` exception aborts `run_cycle()` before inbox processing |
| `test_t4_w1_multi_user_dsm_household_migration` | M3 — C1, C2, G14, Minor #19 | `GET /api/profiles` via `proxy.cgi` missing `is_admin` key |
| `test_t4_w2_incremental_multi_archive_batch_with_failures_safety_caps_and_retry` | M2/M4 — G1, G9, G12, G13 | Archive 2 with failed file `JUNE_FAIL.jpg` is deleted from inbox instead of preserved as `completed_with_errors` |
| `test_t4_w3_quality_upgrade_crash_recovery_and_api_restore_purge_workflow` | M4 — G2, Minor #20 | `MigrationPipeline` does not recover `in_progress` quality upgrades on startup/cycle yet |
| `test_t4_w4_adversarial_security_penetration_chain` | M3 — C1, C2, G3, G4, G6, G14, Minor #26 | Unauthenticated `GET /api/settings` returns `200` instead of `401` |
| `test_t4_w5_spk_build_upgrade_uninstall_and_reinstall_data_survival` | M5 — C1, G7, G8, Minor #29 | Non-deterministic SPK build produces different SHA-256 across two identical invocations |
| `test_t4_w6_release_update_check_non_downgrade_checksum_and_watchtower_workflow` | M5 — G7, Minor #29 | `UpdateService` flags downgrade `v1.1.0` as `update_available=True` |

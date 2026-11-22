# E2E Review Report — Reviewer 2 (`reviewer_e2e_r1_2`)

**Role:** Reviewer 2 — Interface Contracts, `TEST_INFRA.md` Traceability & Tiers 3–4 Review  
**Track:** E2E Testing Track — Iteration 1 (`gphotos-to-synology` `v1.2.0`)  
**Parent Sub-Orchestrator:** `cefe08249ed6750e9786dd4091caf507`  
**Verdict:** **APPROVE**

---

## 1. Executive Summary & Verdict

**Verdict: `APPROVE`**

I independently reviewed [ORIGINAL_REQUEST.md](file:///home/antigravity/gphotos-to-synology/.agents/teamwork/ORIGINAL_REQUEST.md), [PROJECT.md](file:///home/antigravity/gphotos-to-synology/PROJECT.md), [Collab.md](file:///home/antigravity/gphotos-to-synology/Collab.md), [TEST_INFRA.md](file:///home/antigravity/gphotos-to-synology/TEST_INFRA.md), [conftest.py](file:///home/antigravity/gphotos-to-synology/tests/e2e/conftest.py), [test_tier1_features.py](file:///home/antigravity/gphotos-to-synology/tests/e2e/test_tier1_features.py), [test_tier2_boundaries.py](file:///home/antigravity/gphotos-to-synology/tests/e2e/test_tier2_boundaries.py), [test_tier3_interactions.py](file:///home/antigravity/gphotos-to-synology/tests/e2e/test_tier3_interactions.py), and [test_tier4_workloads.py](file:///home/antigravity/gphotos-to-synology/tests/e2e/test_tier4_workloads.py), and executed `.venv/bin/ruff check tests/e2e`, `.venv/bin/pytest --collect-only -q tests/e2e`, `.venv/bin/pytest --collect-only -q`, and `.venv/bin/pytest tests/e2e/test_tier3_interactions.py tests/e2e/test_tier4_workloads.py -v --tb=short`:

1. **Traceability Completeness (`TEST_INFRA.md` vs `PROJECT.md`)**:
   - All 34 features (`#1`–`#34`: `C1`, `C2`, `G1`–`G14`, Minors `#17`–`#34`) in `PROJECT.md § Feature Inventory` are mapped in `TEST_INFRA.md § 3` to concrete E2E test functions that exist in `tests/e2e/test_tier{1,2,3,4}_*.py`.
   - All 6 interface contracts (`#1`–`#6`) in `PROJECT.md § Interface Contracts` are mapped in `TEST_INFRA.md § 4` to concrete E2E test functions.
   - All 72 collected E2E test functions (30 Tier 1 + 26 Tier 2 + 10 Tier 3 + 6 Tier 4) match `TEST_INFRA.md` verbatim with zero orphaned or missing test names.
2. **Verbatim Contract Alignment (`PROJECT.md § Interface Contracts` #1–#6)**:
   - Every function signature, keyword argument, database column/table/method, HTTP endpoint, header name (`X-Proxy-Secret`, `X-Proxy-Mode`, `X-DSM-User`, `X-DSM-Admin`, `X-Profile-Id`, `X-Requested-With`, `Authorization: Bearer`, `X-API-Token`), cookie (`gphotos_session`), status code (`400`, `401`, `403`, `413`, `503`), and dependency/version ceiling in `tests/e2e/` matches `PROJECT.md § Interface Contracts` #1–#6 verbatim.
3. **Code Quality & Execution Verification of Tiers 3–4 (16 Tests)**:
   - `.venv/bin/ruff check tests/e2e` passes with `0` warnings/errors.
   - `.venv/bin/pytest --collect-only -q tests/e2e` collects `72` tests cleanly; `.venv/bin/pytest --collect-only -q` collects all `128` repository tests cleanly.
   - All 10 pairwise combinatorial interaction tests in `test_tier3_interactions.py` and all 6 real-world NAS workload tests (`W1`–`W6`) in `test_tier4_workloads.py` are true opaque-box tests with proper resource cleanup (`try...finally` on `0o555` directories, context managers on ephemeral HTTP/Uvicorn servers, and `start-stop-status stop` cleanup).
   - Every single failure in Tier 3 and Tier 4 (`16 failed` against currently unpatched M2–M5 code) is genuinely caused by pending M2–M5 implementation in `src/` and `synology-spk/`, with zero test-code bugs or fixture defects.

---

## 2. Traceability & Contract Alignment Audit (`TEST_INFRA.md` vs `PROJECT.md`)

### 2.1 Feature Inventory Traceability (`PROJECT.md § Feature Inventory` #1–#34)

| # | Feature | `TEST_INFRA.md` Mapped Tests Verified in `tests/e2e/` | Status |
|---|---|---|---|
| 1 | **C1** | `test_t1_a1_*`, `test_t1_a2_*`, `test_t1_a3_*`, `test_t1_e2_*`, `test_t2_c1_unauthenticated_*`, `test_t2_c1_auto_generated_*`, `test_t2_c1_docker_compose_*`, `test_t3_p1_*`, `test_t4_w1_*`, `test_t4_w4_*` | Verified |
| 2 | **C2** | `test_t1_a3_*`, `test_t1_a4_*`, `test_t2_c2_non_admin_*`, `test_t3_p1_*`, `test_t3_p2_*`, `test_t4_w1_*`, `test_t4_w4_*` | Verified |
| 3 | **G1** | `test_t1_c1_*`, `test_t3_p3_g1_failed_file_delete_guard_and_retry_counter_reset`, `test_t4_w2_*` | Verified |
| 4 | **G2** | `test_t1_c2_*`, `test_t2_minor_purge_rejects_*`, `test_t3_p4_*`, `test_t4_w3_quality_upgrade_crash_recovery_and_api_restore_purge_workflow` | Verified |
| 5 | **G3** | `test_t1_d6_*`, `test_t2_g3_oauth_callback_*`, `test_t3_p2_*`, `test_t4_w4_*` | Verified |
| 6 | **G4** | `test_t1_a5_*`, `test_t2_g4_url_change_*`, `test_t3_p7_*`, `test_t4_w4_*` | Verified |
| 7 | **G5** | `test_t1_a5_*`, `test_t2_g5_env_example_uses_https_and_verify_ssl_true` | Verified |
| 8 | **G6** | `test_t1_e4_*`, `test_t1_e5_*`, `test_t2_g6_csrf_header_*`, `test_t3_p1_*`, `test_t3_p9_*`, `test_t4_w4_*` | Verified |
| 9 | **G7** | `test_t1_e1_*`, `test_t1_e3_*`, `test_t1_e6_*`, `test_t2_g7_updater_*`, `test_t2_g7_ci_*`, `test_t3_p8_*`, `test_t4_w5_*`, `test_t4_w6_*` | Verified |
| 10 | **G8** | `test_t1_a6_*`, `test_t1_e1_*`, `test_t2_g8_spk_*`, `test_t3_p8_*`, `test_t4_w5_*` | Verified |
| 11 | **G9** | `test_t1_b2_*`, `test_t2_g9_strict_sidecar_matcher_*`, `test_t4_w2_*` | Verified |
| 12 | **G10** | `test_t1_b5_*`, `test_t1_e6_*`, `test_t2_g10_undecodable_heic_*`, `test_t2_g7_ci_*`, `test_t3_p5_*` | Verified |
| 13 | **G11** | `test_t1_b6_*`, `test_t2_g11_perceptual_dedup_*`, `test_t3_p5_*` | Verified |
| 14 | **G12** | `test_t1_b1_*`, `test_t2_g12_extraction_caps_*`, `test_t4_w2_*` | Verified |
| 15 | **G13** | `test_t1_b3_*`, `test_t2_g13_corrupt_exif_preserved_*`, `test_t4_w2_*` | Verified |
| 16 | **G14** | `test_t1_a4_*`, `test_t2_g14_personal_and_shared_subfolder_*`, `test_t4_w1_*`, `test_t4_w4_*` | Verified |
| 17–34 | **Minors #17–#34** | All 18 rows (`#17`–`#34`) mapped to existing Tier 1–4 test functions (`test_t1_*`, `test_t2_minor_*`, `test_t3_p*`, `test_t4_w*`) | Verified |

### 2.2 Interface Contracts Alignment (`PROJECT.md § Interface Contracts` #1–#6)

1. **Contract #1 (Proxy / CGI ↔ FastAPI Security)**:
   - Verified `${PKG_VAR}/proxy.secret` (`0o600`, 64 hex chars, `PROXY_SECRET_FILE`), CGI `GPHOTOS_PROXY_SECRET` / `GPHOTOS_PROXY_SECRET_FILE`, headers `X-Proxy-Secret`, `X-Proxy-Mode: cgi`, `X-DSM-User`, `X-DSM-Admin` (`"1"`/`"0"`), `X-Profile-Id`, `X-Requested-With`.
   - Verified Direct/Docker auth via `Authorization: Bearer <token>`, `X-API-Token: <token>`, `POST /api/auth/session` setting `HttpOnly` `gphotos_session` cookie (requiring `X-Requested-With`), auto-generated `data_dir / "api.token"` (`0o600`), public exempt routes (`/api/health`, `/`, `/static/*`, `/api/auth/status`, `POST /api/auth/session`), and `GET /api/profiles` returning `is_admin` and `dsm_user` with non-admin profile filtering.
2. **Contract #2 (`ConfigManager` & `Settings`)**:
   - Verified defaults (`app_host="127.0.0.1"`, `synology_url="https://127.0.0.1:5001"`, `synology_verify_ssl=True`, `synology_ca_cert=""`, `quarantine_dir_override`), lazy OAuth inheritance, URL scheme validation (`ValueError`), G4 password clearing on URL change without a fresh plaintext password, and `SECRET_FIELDS` vs `MUTABLE_UI_FIELDS` separation. (All M1 config E2E tests already **PASS**.)
3. **Contract #3 (`StateDB`)**:
   - Verified `delete_state(key)`, `media_items.visual_status`, `media_dhash_buckets` (8 blocks of 4 hex chars, bucketed lookup when `effective_max < 8` and `len(dhash) == 32`, full scan fallback when `>= 8` or legacy 16-char), `quality_upgrades` columns (`old_rel_path`, `old_taken_at`, `old_latitude`, `old_longitude`), journal API (`create_pending_quality_upgrade`, `finalize_quality_upgrade`, `delete_quality_upgrade`, `list_in_progress_quality_upgrades`, `list_quarantined_upgrades_batch`), and `mark_album_membership` retry guard. (All M1 database E2E tests already **PASS**.)
4. **Contract #4 (Takeout & Sources)**:
   - Verified `extract_archive(archive_path, dest_dir, *, max_entries=100_000, max_uncompressed_bytes=None, max_compression_ratio=100.0, skip_unsafe_entries=False)`, `cleanup_stale_temp_files(directory, max_age_seconds=0.0)`, `MediaFingerprint.visual_status`, and `GoogleDriveClient.connection_status() -> {"connected": bool, "error": str}`.
5. **Contract #5 (Pipeline & Notifier)**:
   - Verified `MigrationPipeline._op_lock`, `restore_quality_upgrade` / `purge_quality_upgrade` raising `ValueError` on non-`'quarantined'` status or path traversal (mapped to HTTP `400`), and `Notifier.send()` (sync) vs `Notifier.send_async()` (non-blocking daemon thread).
6. **Contract #6 (Packaging & Dependency Pinning)**:
   - Verified `"pillow>=11.1.0,<12.3.0"`, `"pillow-heif>=1.0.0,<1.1.0"`, committed `uv.lock`, PyInstaller `--collect-all pillow_heif`, and unified version `1.2.0` / `1.2.0-0001`.

---

## 3. Independent Verification Command Outputs

### 3.1 Lint & Collection Verification
```text
$ .venv/bin/ruff check tests/e2e
All checks passed!

$ .venv/bin/pytest --collect-only -q tests/e2e
72 tests collected in 0.03s

$ .venv/bin/pytest --collect-only -q
128 tests collected in 0.49s
```

### 3.2 Tier 3 & Tier 4 Execution & Failure Diagnosis (`.venv/bin/pytest tests/e2e/test_tier3_interactions.py tests/e2e/test_tier4_workloads.py -v --tb=short`)

All 16 tests in Tiers 3 and 4 execute deterministically (`7.55s`) and fail strictly on pending M2–M5 implementation:

| Test Function | Tier / Pair / Workload | Subsystems Exercised | Exact Failure Against Current Code | Root Cause (Pending Milestone) |
|---|---|---|---|---|
| [`test_t3_p1_proxy_cgi_with_real_fastapi_c1_c2_g6`](file:///home/antigravity/gphotos-to-synology/tests/e2e/test_tier3_interactions.py#L55-L146) | Tier 3 — Pair 1 | Real `proxy.cgi` subprocess → live `uvicorn` FastAPI server | `assert r_cross.status == 403` (`200 == 403`) after admin profile creation and `alice` self-status succeed | **M3** (`C1`/`C2`/`G6` in `web/security.py`, `web/routes.py`, `proxy.cgi`) |
| [`test_t3_p2_oauth_callback_cgi_with_real_fastapi_g3_c2`](file:///home/antigravity/gphotos-to-synology/tests/e2e/test_tier3_interactions.py#L149-L240) | Tier 3 — Pair 2 | Real `proxy.cgi` + `oauth_callback.cgi` → live FastAPI OAuth flow | `assert len(exchanged) == 0` (`1 == 0`) when `bob` calls `oauth_callback.cgi` with `alice`'s state | **M3** (`G3`/`C2` server-side OAuth state ownership & one-time clearing in `web/routes.py`) |
| [`test_t3_p3_g1_failed_file_delete_guard_and_retry_counter_reset`](file:///home/antigravity/gphotos-to-synology/tests/e2e/test_tier3_interactions.py#L243-L299) | Tier 3 — Pair 3 | `MigrationPipeline`, `LocalInboxScanner` (`delete`), `StateDB` | `assert archive_path.is_file()` fails because inbox archive is deleted despite 1 failed file in `2024/07` | **M4** (`G1` `completed_with_errors` & `failed == 0` finalize guard in `worker/pipeline.py`) |
| [`test_t3_p4_exif_repair_post_repair_dedup_upgrade_restore_and_purge_reject`](file:///home/antigravity/gphotos-to-synology/tests/e2e/test_tier3_interactions.py#L302-L384) | Tier 3 — Pair 4 | EXIF repair, perceptual upgrade, `TestClient` `/api/upgrades/{id}/restore` & `/purge` | `assert upg["old_rel_path"] == old_rel_path` (`'' == '2024/07/mountain.jpg'`) after upgrade succeeds | **M4** (`G2`/Minor #20 recording `old_rel_path`/`old_taken_at`/GPS in `worker/pipeline.py`) |
| [`test_t3_p5_g10_heic_undecodable_scanner_incremental_skip_and_g11_perceptual_dedup`](file:///home/antigravity/gphotos-to-synology/tests/e2e/test_tier3_interactions.py#L387-L433) | Tier 3 — Pair 5 | `save_heic`, `ExistingLibraryScanner`, `MigrationPipeline`, `StateDB` logs | `assert s2.newly_indexed == 0` (`1 == 0`) on second scan cycle of corrupt `.heic` | **M2** (`G10` `pillow_heif` opener & `visual_status='undecodable'` skip in `dedup.py`/`scanner.py`) |
| [`test_t3_p6_multi_profile_lazy_oauth_delete_cleanup_and_recreate`](file:///home/antigravity/gphotos-to-synology/tests/e2e/test_tier3_interactions.py#L436-L480) | Tier 3 — Pair 6 | `ProfileRegistry`, `ConfigManager`, `StateDB` | `assert alice_ctx.config_manager.settings.google_client_id == "cid-v1"` (`'' == 'cid-v1'`) | **M3** (Minor #18/#19 lazy OAuth `fallback_settings_provider` & `delete_profile` disk cleanup in `profiles.py`) |
| [`test_t3_p7_g4_url_change_password_clear_and_synology_post_sid_relogin`](file:///home/antigravity/gphotos-to-synology/tests/e2e/test_tier3_interactions.py#L483-L541) | Tier 3 — Pair 7 | FastAPI `/api/settings` + `/api/actions/test-synology` + `SynologyPhotosClient` + `MockDSMServer` | G4 password clear & non-exfiltration **PASS**; fails at `dsm_client.list_albums()` with `SynologyAPIError (code=119)` | **M2** (Minor #27 auto-relogin on code `119` & POST `_sid` in `synology/photos_api.py`) |
| [`test_t3_p8_spk_deterministic_double_build_lifecycle_and_updater_checksum`](file:///home/antigravity/gphotos-to-synology/tests/e2e/test_tier3_interactions.py#L544-L641) | Tier 3 — Pair 8 | `build_spk.py`, `/bin/sh` `start-stop-status` & `preuninst`, `UpdateService` | `assert digest1 == digest2` fails across two identical SPK builds separated by 1.1s | **M5** (`G7`/`G8` deterministic SPK build timestamps & lifecycle hooks in `synology-spk/build_spk.py`) |
| [`test_t3_p9_frontend_app_js_xss_defense_header_merge_and_cgi_routing`](file:///home/antigravity/gphotos-to-synology/tests/e2e/test_tier3_interactions.py#L644-L691) | Tier 3 — Pair 9 | Node `vm` browser harness executing `src/gphotos_synology/web/static/app.js` | `assert "&quot;" in html` fails on `archives-tbody` `innerHTML` | **M3** (Minor #26 quote escaping in `escapeHtml` & `G6` header spread fix in `app.js`) |
| [`test_t3_p10_album_pending_index_root_scope_scanner_isolation_and_throttled_async_notifier`](file:///home/antigravity/gphotos-to-synology/tests/e2e/test_tier3_interactions.py#L694-L764) | Tier 3 — Pair 10 | `MigrationPipeline`, `ExistingLibraryScanner`, `MockDSMServer`, `MockWebhookServer` | `assert summary["inbox_processed"] == 1` (`0 == 1`) when `ExistingLibraryScanner.scan` raises `PermissionError` | **M4** (Minor #24 scanner error isolation & Minor #25 12h throttled async notifier in `worker/pipeline.py`) |
| [`test_t4_w1_multi_user_dsm_household_migration`](file:///home/antigravity/gphotos-to-synology/tests/e2e/test_tier4_workloads.py#L53-L197) | Tier 4 — Workload 1 | Real `proxy.cgi` → live FastAPI server + multi-profile pipeline migration | `KeyError: 'is_admin'` on `GET /api/profiles` after OAuth config and profile creation succeed | **M3** (Contract #1 / `C2` `is_admin` & `dsm_user` fields in `GET /api/profiles`) |
| [`test_t4_w2_incremental_multi_archive_batch_with_failures_safety_caps_and_retry`](file:///home/antigravity/gphotos-to-synology/tests/e2e/test_tier4_workloads.py#L200-L284) | Tier 4 — Workload 2 | Multi-archive ZIP + TAR.GZ batch (`G1`, `G9`, `G12`, `G13`) | Archive 1 (`a1`) succeeds and is deleted; fails at `assert a2.is_file()` because `a2` with `JUNE_FAIL.jpg` was deleted | **M4 / M2** (`G1` finalize guard in `worker/pipeline.py`, `G9` in `matcher.py`, `G13` in `metadata.py`) |
| [`test_t4_w3_quality_upgrade_crash_recovery_and_api_restore_purge_workflow`](file:///home/antigravity/gphotos-to-synology/tests/e2e/test_tier4_workloads.py#L287-L400) | Tier 4 — Workload 3 | Quality upgrades, `in_progress` crash journal recovery, FastAPI preview/restore/purge-all | Initial upgrades & `old_d` non-downgrade succeed; fails at `assert db.list_in_progress_quality_upgrades() == []` | **M4** (`G2` startup/cycle crash recovery of `in_progress` quality upgrades in `worker/pipeline.py`) |
| [`test_t4_w4_adversarial_security_penetration_chain`](file:///home/antigravity/gphotos-to-synology/tests/e2e/test_tier4_workloads.py#L403-L571) | Tier 4 — Workload 4 | 7-stage adversarial attack chain (`C1`, `C2`, `G3`, `G4`, `G6`, `G14`, XSS) | `assert httpx.get(f"{live.url}/api/settings").status_code == 401` (`200 == 401`) | **M3** (`C1` authentication middleware in `web/security.py` / `main.py`) |
| [`test_t4_w5_spk_build_upgrade_uninstall_and_reinstall_data_survival`](file:///home/antigravity/gphotos-to-synology/tests/e2e/test_tier4_workloads.py#L574-L671) | Tier 4 — Workload 5 | SPK double-build, stale PID check, 4 `preupgrade` runs, `preuninst` uninstall survival | `assert sha256_file(spk_a) == sha256_file(spk_b)` fails due to non-deterministic tar/gzip timestamps | **M5** (`G7`/`G8`/Minor #29 in `synology-spk/build_spk.py`) |
| [`test_t4_w6_release_update_check_non_downgrade_checksum_and_watchtower_workflow`](file:///home/antigravity/gphotos-to-synology/tests/e2e/test_tier4_workloads.py#L674-L765) | Tier 4 — Workload 6 | `UpdateService`, `/api/updates`, `/api/updates/apply`, Watchtower 405/200, repo metadata | `assert st1["update_available"] is False` (`True is False`) on downgrade release `v1.1.0` | **M5** (`G7` non-downgrading semver comparison in `src/gphotos_synology/updater.py`) |

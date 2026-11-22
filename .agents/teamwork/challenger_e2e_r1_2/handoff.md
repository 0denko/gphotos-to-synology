# Challenger 2 Adversarial Verification Report (`challenger_e2e_r1_2`)

**Role:** Challenger 2 — Adversarial Probe on Isolation, Opaque-Box Compliance & Edge Cases  
**Track:** E2E Testing Track (`gphotos-to-synology` `v1.2.0`) — Iteration 1  
**Parent Sub-Orchestrator:** `cefe08249ed6750e9786dd4091caf507`  
**Verdict:** `APPROVE`

---

## 1. Adversarial Environment & Collection Probes

We independently executed test collection, linting, and live E2E test execution while injecting hostile outer-shell environment variables (`NO_PROXY="localhost,[::1]:80"`, `no_proxy="localhost,[::1]:80"`, `HTTP_PROXY="http://[::1]:80"`, `HTTPS_PROXY="http://[::1]:80"`, `GPHOTOS_PROXY_SECRET="leaked"`, `PROXY_SECRET="leaked"`, `API_TOKEN="leaked"`, `SYNOLOGY_URL="http://leaked:5000"`, `SYNOLOGY_VERIFY_SSL="false"`, `ALLOW_INSECURE_NO_AUTH="true"`, `DATA_DIR="/tmp/leaked_data_dir"`):

### 1.1 Collection & Lint Under Hostile Environment Variables
```text
$ NO_PROXY="localhost,[::1]:80" no_proxy="localhost,[::1]:80" HTTP_PROXY="http://[::1]:80" HTTPS_PROXY="http://[::1]:80" GPHOTOS_PROXY_SECRET="leaked" PROXY_SECRET="leaked" API_TOKEN="leaked" SYNOLOGY_URL="http://leaked:5000" SYNOLOGY_VERIFY_SSL="false" ALLOW_INSECURE_NO_AUTH="true" DATA_DIR="/tmp/leaked_data_dir" .venv/bin/pytest --collect-only -q
128 tests collected in 0.48s

$ NO_PROXY="localhost,[::1]:80" no_proxy="localhost,[::1]:80" HTTP_PROXY="http://[::1]:80" HTTPS_PROXY="http://[::1]:80" GPHOTOS_PROXY_SECRET="leaked" PROXY_SECRET="leaked" API_TOKEN="leaked" SYNOLOGY_URL="http://leaked:5000" SYNOLOGY_VERIFY_SSL="false" ALLOW_INSECURE_NO_AUTH="true" DATA_DIR="/tmp/leaked_data_dir" .venv/bin/pytest --collect-only -q tests/e2e
72 tests collected in 0.04s

$ .venv/bin/ruff check tests/e2e
All checks passed!
```

### 1.2 Live E2E Execution Under Hostile Environment Variables
All 12 currently passing E2E tests (covering M1 and baseline features, including `test_t1_a5_v120_secure_settings_defaults_and_config_update` which asserts `Settings(_env_file=None)` defaults directly and `test_t1_c3_album_sync_and_pending_index_without_burning_retries` which exercises `httpx.Client` against local `mock_dsm_server`) were run with hostile environment variables injected in the outer shell:

```text
$ NO_PROXY="localhost,[::1]:80" no_proxy="localhost,[::1]:80" HTTP_PROXY="http://[::1]:80" HTTPS_PROXY="http://[::1]:80" GPHOTOS_PROXY_SECRET="leaked" PROXY_SECRET="leaked" API_TOKEN="leaked" SYNOLOGY_URL="http://leaked:5000" SYNOLOGY_VERIFY_SSL="false" ALLOW_INSECURE_NO_AUTH="true" DATA_DIR="/tmp/leaked_data_dir" .venv/bin/pytest tests/e2e -v -k "test_t1_a5 or test_t1_a6 or test_t1_b2 or test_t1_b6 or test_t1_c1 or test_t1_c3 or test_t1_d1 or test_t1_d2 or test_t1_d5 or test_t1_d6 or test_t1_e6 or test_t2_g4"
tests/e2e/test_tier1_features.py::test_t1_a5_v120_secure_settings_defaults_and_config_update PASSED [  8%]
tests/e2e/test_tier1_features.py::test_t1_a6_quarantine_dir_override_property PASSED [ 16%]
tests/e2e/test_tier1_features.py::test_t1_b2_strict_sidecar_matching_happy_paths PASSED [ 25%]
tests/e2e/test_tier1_features.py::test_t1_b6_statedb_dhash_buckets_and_hash_lookup_ordering PASSED [ 33%]
tests/e2e/test_tier1_features.py::test_t1_c1_pipeline_run_cycle_full_import_and_finalize PASSED [ 41%]
tests/e2e/test_tier1_features.py::test_t1_c3_album_sync_and_pending_index_without_burning_retries PASSED [ 50%]
tests/e2e/test_tier1_features.py::test_t1_d1_inbox_scanner_discovery_and_finalize_actions PASSED [ 58%]
tests/e2e/test_tier1_features.py::test_t1_d2_existing_library_scanner_read_only_and_ignored_dirs PASSED [ 66%]
tests/e2e/test_tier1_features.py::test_t1_d5_synology_folder_importer_sanitization_and_collision_safety PASSED [ 75%]
tests/e2e/test_tier1_features.py::test_t1_d6_statedb_delete_state_and_periodic_log_prune PASSED [ 83%]
tests/e2e/test_tier1_features.py::test_t1_e6_unified_version_and_packaging_metadata PASSED [ 91%]
tests/e2e/test_tier2_boundaries.py::test_t2_g4_url_change_clears_stored_password_and_rejects_bad_schemes PASSED [100%]
================= 12 passed, 60 deselected, 1 warning in 0.88s =================
```

**Isolation Conclusion:** `_isolate_e2e_env` (`autouse=True` in [tests/e2e/conftest.py](file:///home/antigravity/gphotos-to-synology/tests/e2e/conftest.py#L49-L100)) and [`make_isolated_settings`](file:///home/antigravity/gphotos-to-synology/tests/e2e/conftest.py#L107-L123) (`Settings(_env_file=None, ...)`) completely isolate `tests/e2e/` from host environment and `.env` leakage.

---

## 2. AST / Static Probe for Opaque-Box Compliance & Test Quality

We staged and ran an AST static analysis script inside an isolated temporary directory (`mktemp -d -- "${TMPDIR:-/tmp}/teamwork_probe.XXXXXX"` with `chmod 700`) across all 5 files in `tests/e2e/` ([conftest.py](file:///home/antigravity/gphotos-to-synology/tests/e2e/conftest.py), [test_tier1_features.py](file:///home/antigravity/gphotos-to-synology/tests/e2e/test_tier1_features.py), [test_tier2_boundaries.py](file:///home/antigravity/gphotos-to-synology/tests/e2e/test_tier2_boundaries.py), [test_tier3_interactions.py](file:///home/antigravity/gphotos-to-synology/tests/e2e/test_tier3_interactions.py), [test_tier4_workloads.py](file:///home/antigravity/gphotos-to-synology/tests/e2e/test_tier4_workloads.py)) and removed the temporary probe directory immediately upon completion.

### 2.1 AST Probe Output
```text
Skip/xfail violations: 0
Missing assertion violations: 0
Private imports: 0
Non-allowed private attribute accesses on SUT objects: 0
Total assert statements across 72 E2E tests: 556
Total pytest.raises blocks across 72 E2E tests: 14
  test_tier1_features.py: 30 tests (Tier 1 categories: {'A': 6, 'B': 6, 'C': 6, 'D': 6, 'E': 6})
  test_tier2_boundaries.py: 26 tests
  test_tier3_interactions.py: 10 tests
  test_tier4_workloads.py: 6 tests
ALL AST OPAQUE-BOX & QUALITY CHECKS PASSED!
```

### 2.2 Detailed Verification Results
1. **No Skips or Xfails**:
   - `0` instances of `@pytest.mark.skip`, `@pytest.mark.skipif`, `@pytest.mark.xfail`, `pytest.skip()`, or `pytest.xfail()` across `tests/e2e/*.py`.
2. **Real Assertions in Every Test**:
   - Every single `def test_*` function (all 72 of 72) contains concrete behavioral assertions (`556` `assert` statements and `14` `pytest.raises` blocks total).
3. **Strict Opaque-Box Compliance**:
   - `0` private `_` symbols are imported from `gphotos_synology` anywhere in `tests/e2e/*.py`.
   - `0` undocumented private `_` methods or attributes are accessed on `gphotos_synology` objects across `tests/e2e/test_tier*.py` (only `pipeline._op_lock`, which is explicitly specified in `PROJECT.md § Interface Contracts #5`, and `_env_file=None` on Pydantic `Settings`).
   - Even direct `Settings(_env_file=None)` instantiation in `test_t1_a5_v120_secure_settings_defaults_and_config_update` passes `_env_file=None`.
4. **Tier Structure & Counts Match `sub_orch_e2e/DISPATCH.md` and `TEST_INFRA.md`**:
   - **Tier 1 (`test_tier1_features.py`)**: 30 tests — 6 self-contained happy-path tests in each of the 5 major feature areas A–E (exceeds the `>= 5` per area threshold).
   - **Tier 2 (`test_tier2_boundaries.py`)**: 26 tests — comprehensive boundary value analysis, security fail-closed rejections, decompression limits, ZipSlip/TarSlip, corrupt EXIF preservation, DMS 60.0s carry clamp, ambiguous sidecars, non-downgrade semver checks, CGI 1 MiB body cap/offline 503/CRLF sanitization, and corrupt file resilience.
   - **Tier 3 (`test_tier3_interactions.py`)**: 10 tests — cross-feature pairwise interactions across live FastAPI, real CGI subprocesses, SQLite WAL state, pipeline cycles, SPK POSIX `/bin/sh` scripts, and Node `vm` `app.js`.
   - **Tier 4 (`test_tier4_workloads.py`)**: 6 tests — 6 end-to-end multi-feature NAS user journeys (`W1`–`W6`, meeting the `>= 6` threshold).
   - **Traceability Sync**: Every test function name referenced in [TEST_INFRA.md](file:///home/antigravity/gphotos-to-synology/TEST_INFRA.md) exists in `tests/e2e/test_tier*.py`, and all 34 features (`#1`–`#34`) and 6 interface contracts (`#1`–`#6`) from [PROJECT.md](file:///home/antigravity/gphotos-to-synology/PROJECT.md) are mapped.

---

## 3. Final Verdict

**Verdict:** `APPROVE`

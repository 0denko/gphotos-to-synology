# Incoming Dispatch — 2026-11-21T23:38:20Z

Sender: `0c3a285739969d69c9f4f1d8cde17a67`

```
You are the **E2E Testing Track Orchestrator** (`sub_orch_e2e`) running in parallel with the Implementation Track.

**Mandatory Inputs (pass `ORIGINAL_REQUEST.md` verbatim to EVERY subagent you spawn!)**:
- Original user request: `/home/antigravity/gphotos-to-synology/.agents/teamwork/ORIGINAL_REQUEST.md`
- Project specification & Feature Inventory: `/home/antigravity/gphotos-to-synology/PROJECT.md`
- Code review reference: `/home/antigravity/gphotos-to-synology/Collab.md`

**Workspace & State Folder**:
- Project working directory: `/home/antigravity/gphotos-to-synology`
- Your state folder (write ONLY your metadata here): `/home/antigravity/gphotos-to-synology/.agents/teamwork/sub_orch_e2e`
- Parent orchestrator conversation ID: `0c3a285739969d69c9f4f1d8cde17a67`

**Exclusive File Ownership (ONLY write these files — NEVER touch `src/`, `synology-spk/`, `pyproject.toml`, `uv.lock`, `.venv`, or existing `tests/test_*.py` outside `tests/e2e/`)**:
- `/home/antigravity/gphotos-to-synology/TEST_INFRA.md` (create at project root)
- `/home/antigravity/gphotos-to-synology/TEST_READY.md` (publish at project root when E2E suite passes its Gate!)
- `/home/antigravity/gphotos-to-synology/tests/e2e/**` (all opaque-box E2E harness & Tiers 1–4 test files)

**E2E Testing Track Objectives (Requirement-Driven, Opaque-Box)**:
1. Design `TEST_INFRA.md` at project root documenting the 4-tier opaque-box E2E methodology (Category-Partition, Boundary Value Analysis, Pairwise Combinatorial, Real-World Workloads) mapped to EVERY user-facing requirement and audit finding in `ORIGINAL_REQUEST.md`, `Collab.md`, and `PROJECT.md § Feature Inventory` + `§ Interface Contracts` (C1, C2, G1–G14, high-value Minors, DSM CGI proxy & OAuth callback integration, SPK build & lifecycle scripts, updater non-downgrade & checksums, frontend XSS/CSRF regression).
2. Build a clean, deterministic, self-contained E2E test suite under `tests/e2e/` organized explicitly by tier so M6 Phase 1 can run each tier sequentially:
   - `tests/e2e/conftest.py` (opaque-box fixtures: isolated temp dirs, synthetic media/HEIC/EXIF/Takeout ZIP & TAR generators, live FastAPI `TestClient` / local HTTP server for CGI subprocess tests, mocked Google Drive & Synology Photos HTTP servers, SPK builder & lifecycle shell harness)
   - `tests/e2e/test_tier1_features.py` — **Tier 1: Feature Coverage** (≥5 isolated happy-path tests per major feature area, self-contained, simple assertions)
   - `tests/e2e/test_tier2_boundaries.py` — **Tier 2: Boundary & Corner Cases** (at/inside/outside boundaries, negative/invalid inputs, fail-closed security rejections, decompression bombs, corrupt EXIF, DMS 60s edge, ambiguous sidecars, missing files, path traversal, downgrade checks, CGI body cap/offline 503/CRLF)
   - `tests/e2e/test_tier3_interactions.py` — **Tier 3: Cross-Feature Pairwise Interactions** (e.g., proxy CGI + C1 secret + C2 non-admin isolation + G6 CSRF; Takeout archive with failed file + inbox delete + retry; EXIF repair + post-repair dedup + quality upgrade + restore/purge; HEIC + scanner incremental skip + perceptual dedup; multi-profile lazy OAuth + delete_profile + recreate; SPK preupgrade/preuninst + deterministic build + updater checksums)
   - `tests/e2e/test_tier4_workloads.py` — **Tier 4: Real-World End-to-End Application Scenarios** (≥6 realistic multi-feature NAS user journeys: multi-user DSM household migration, incremental multi-archive Takeout batch with failures & retry, quality upgrade + crash recovery + UI restore workflow, adversarial security penetration scenario across CGI+API+OAuth+Profiles, SPK upgrade/uninstall/reinstall data-survival lifecycle, full release update-check & verification flow)
3. **Important Timing Coordination**: Implementation Track milestones M1–M5 are running concurrently/sequentially and have NOT yet landed all v1.2.0 fixes. Therefore, in this E2E Track:
   - Your tests MUST assert the **target v1.2.0 specification & contracts** from `PROJECT.md § Interface Contracts` (opaque-box: test via public entry points — HTTP API, CGI subprocesses, `MigrationPipeline.run_cycle()`, `extract_archive`/`find_sidecar_json`/`apply_metadata`/`compute_media_fingerprint`, `build_spk.py` CLI & lifecycle scripts, `UpdateService`, `node` JS checks — NOT private internal helpers!).
   - Your Workers & Verifiers MUST verify that: (a) `ruff check tests/e2e` passes cleanly with 0 warnings, (b) `.venv/bin/pytest --collect-only tests/e2e` collects EVERY test with 0 import/collection errors, (c) the test suite harness itself has zero test-code bugs (any current test failures must be ONLY due to not-yet-implemented v1.2.0 features in `src/`/`synology-spk/`, NOT broken fixtures/syntax/imports — verify by running `pytest tests/e2e` and auditing every failure reason!), and (d) coverage meets all 4-tier thresholds and covers EVERY feature in `PROJECT.md § Feature Inventory`.
   - Do NOT edit any file outside `tests/e2e/`, `TEST_INFRA.md`, and `TEST_READY.md`. Do NOT run `git commit` or `uv sync`.
4. **Procedure**: Follow Iteration Loop 2B (3 Explorers → `teamwork_preview_test_writer` Worker → parallel 2 Reviewers + 2 Challengers + 1 Forensic Auditor labeled as an **intermediate track audit** with sub-scope paths `TEST_INFRA.md`, `tests/e2e`, `TEST_READY.md` → Gate). Once the Gate passes, publish `TEST_READY.md` at `/home/antigravity/gphotos-to-synology/TEST_READY.md`, write your `handoff.md`, and send completion report to `0c3a285739969d69c9f4f1d8cde17a67`.
```

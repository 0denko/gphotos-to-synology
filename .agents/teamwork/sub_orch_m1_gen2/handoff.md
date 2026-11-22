# Milestone M1 Sub-Orchestrator Handoff (`sub_orch_m1_gen2`)

## Milestone Status: COMPLETED (`Gate Result: **PASS**`)

**Milestone:** M1 — Foundation (Dependencies, Version, Config & Database)  
**Sub-Orchestrator:** `sub_orch_m1_gen2` (`645f581a2823278930b5dd011557c86b`)  
**Parent Orchestrator:** `c8b19b05da9e071b01096e473f316cf4`  
**Spawn Count Used:** `5 / 20`

---

## 1. Exclusively Owned Files Delivered (7/7)

1. [`pyproject.toml`](file:///home/antigravity/gphotos-to-synology/pyproject.toml)
   - Bumped `version = "1.2.0"`.
   - Tightened Pillow to `"pillow>=11.1.0,<12.3.0"` (locks `12.2.0` for glibc-2.17 `manylinux2014` wheel support on `x86_64` and `aarch64`) and added `"pillow-heif>=1.0.0,<1.1.0"` (locks `1.0.0` for glibc-2.17 `manylinux2014` wheel support on `x86_64` and `aarch64`).
   - Added optional extra `spk = ["pyinstaller>=6.10.0,<7.0.0"]` under `[project.optional-dependencies]`.
2. [`uv.lock`](file:///home/antigravity/gphotos-to-synology/uv.lock)
   - Generated via `uv lock` and synced to `.venv` via `uv sync --locked --extra dev`.
   - Independently verified that `gphotos-to-synology==1.2.0`, `pillow==12.2.0`, `pillow-heif==1.0.0`, and all binary packages in `uv.lock` include `manylinux2014` / `manylinux_2_17` wheels for both `x86_64` and `aarch64` (`cp311` and `cp312`).
3. [`src/gphotos_synology/__init__.py`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/__init__.py)
   - Bumped `__version__ = "1.2.0"`.
4. [`src/gphotos_synology/config.py`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py)
   - **G5 (Secure defaults & quarantine dir override)**: `app_host = "127.0.0.1"`, `synology_url = "https://127.0.0.1:5001"` (`DEFAULT_SYNOLOGY_URL`), `synology_verify_ssl = True`, `synology_ca_cert = ""`, `quarantine_dir_override: Path | None = None` (`AliasChoices("quarantine_dir", "quarantine_dir_override")` with blank-string-to-`None` coercion, exposed via `Settings.quarantine_dir`), and `populate_by_name=True` in `SettingsConfigDict`.
   - **C1 / C2 / G6 Security Fields & Allowlists**: Added `proxy_secret`, `proxy_secret_file`, `api_token`, `dsm_admin_users`, `trust_proxy_headers = False`, `allow_insecure_no_auth = False`, `resolve_proxy_secret()` (fails closed with `WARNING` log on `OSError`), and `dsm_admin_user_set()`. Added `proxy_secret` and `api_token` to `SECRET_FIELDS`; kept all security, path, and Watchtower fields out of `MUTABLE_UI_FIELDS` (and filtered on disk load).
   - **G4 (Prevent `synology_url` change from exfiltrating stored DSM credentials)**: `validate_synology_url()` enforces `http://` or `https://` scheme, valid hostname/port, and rejects whitespace/control characters (`ValueError`) before mutating state. When the normalized `synology_url` changes without a fresh non-empty, non-`"********"` `synology_password`, `ConfigManager.update()` clears `existing["synology_password"] = ""` (overriding even env-var passwords across reloads), logs a `WARNING`, and records a notice in `self.last_update_warnings`.
   - **Lazy OAuth & URL Inheritance**: `ConfigManager(base_settings=None, fallback_settings_provider=None)` dynamically inherits unset `google_client_id`, `google_client_secret`, and default/unset `synology_url` from `fallback_settings_provider()`, reflects inherited values accurately in `to_public_dict()`, avoids freezing inherited values on full-form UI saves, and pins `synology_url` locally whenever a profile stores a non-empty `synology_password`.
   - **Resilience & Atomic `0o600` Writes**: Corrupt or invalid `runtime_settings.json` files are logged at `ERROR` and backed up to `runtime_settings.json.corrupt.<UTC_timestamp>` (with collision suffix) before falling back to `base_settings`. Writes open temp files via `os.open(..., 0o600)`, `flush()`, `os.fsync()`, enforce `os.chmod(..., 0o600)` (logging `WARNING` on `OSError`), and atomically `os.replace()`.
5. [`src/gphotos_synology/database.py`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/database.py)
   - **G3**: Added `delete_state(key: str) -> None`.
   - **Additive Idempotent Schema & Migrations (G10, G11, G2)**:
     - Added `media_items.visual_status TEXT NOT NULL DEFAULT ''`, backfilling `'ok'` on already-fingerprinted rows while leaving pre-`v1.2.0` empty-visual HEIC rows as `''` so they are re-indexed once.
     - Added `media_dhash_buckets(bucket_idx INTEGER NOT NULL, bucket_val TEXT NOT NULL, media_id INTEGER NOT NULL, PRIMARY KEY (bucket_idx, bucket_val, media_id)) WITHOUT ROWID` with `ON DELETE CASCADE` and `idx_dhash_buckets_media_id`, normalizing existing `dhash` values to lowercase, backfilling 8×4-hex blocks idempotently, and tracking `self._has_legacy_dhash`.
     - Added `quality_upgrades` columns `old_rel_path TEXT NOT NULL DEFAULT ''`, `old_taken_at TEXT NOT NULL DEFAULT ''`, `old_latitude REAL`, `old_longitude REAL`.
   - **G11 SQL dHash Bucketing**:
     - Synchronized `media_dhash_buckets` across `record_media_item`, `upsert_existing_nas_media`, `upgrade_media_item`, `delete_media_item`, and `prune_missing_existing_nas_media`.
     - `find_duplicate_media` Tier 3 uses 8 `PRIMARY KEY` index lookups (`_DHASH_BUCKET_CANDIDATES_SQL`, verified via `EXPLAIN QUERY PLAN` with zero table scans) when `perceptual_max_distance < 8` and `len(dhash) == 32` (plus legacy non-32-char fallback when `_has_legacy_dhash` is true), and falls back to full scan when `perceptual_max_distance >= 8` or non-32-hex hash for 100% recall.
   - **G2 Quality Upgrade Journal API & Batched Purge**:
     - Implemented `create_pending_quality_upgrade(...) -> int`, `finalize_quality_upgrade(upgrade_id, kept_rel_path, new_file_size=None) -> None`, `delete_quality_upgrade(upgrade_id) -> None`, `list_in_progress_quality_upgrades() -> list[dict[str, Any]]`, `list_quarantined_upgrades_batch(limit=500) -> list[dict[str, Any]]`, and backwards-compatible `old_*` parameters on `record_quality_upgrade`.
     - Excluded transient `'in_progress'` rows from default `list_quality_upgrades()` and `get_stats()["quality_upgrades_count"]`.
   - **Hash Ordering, Archive Retry Reset, Album Retries, Periodic Log Pruning, Scanner Undecodable State & Stats**:
     - Deterministic `ORDER BY CASE WHEN original_sha256 = ? THEN 0 ELSE 1 END, id ASC LIMIT 1` in `get_media_by_original_hash`, `find_duplicate_media` Tier 1, and `upsert_existing_nas_media`.
     - `upsert_archive` resets `total_files=0`, `imported_files=0`, `duplicate_files=0`, `failed_files=0`, `error_message=''`, and `completed_at=NULL` when retrying non-completed archives (`status != 'completed'`).
     - `mark_album_membership(..., increment_retry: bool | None = None)` defaults `increment_retry` to `(status == "error")` so `'pending'` Synology indexing waits never burn retries; `add_media_to_album` revives `'error'` rows.
     - `log_event` prunes `activity_logs` periodically every 50 inserts (`_LOG_PRUNE_INTERVAL = 50`).
     - `get_indexed_files_by_rel_path()` exposes `visual_status` and `visual_done` (`has_visual or visual_status in ("ok", "undecodable")`); `upsert_existing_nas_media`, `record_media_item`, and `upgrade_media_item` accept `visual_status`.
     - `get_stats()` preserves all 14 existing keys and adds `"completed_with_errors_archives"`.
6. [`tests/test_config.py`](file:///home/antigravity/gphotos-to-synology/tests/test_config.py)
   - 26 unit test cases covering G5 defaults, C1/C2/G6 fields & helpers, `MUTABLE_UI_FIELDS` & `SECRET_FIELDS`, malformed `synology_url` rejection, G4 password clearing (including env-var override across reloads), lazy OAuth/URL inheritance & password pinning, corrupt JSON backup, and `0o600` atomic writes.
7. [`tests/test_database.py`](file:///home/antigravity/gphotos-to-synology/tests/test_database.py)
   - 10 comprehensive test suites covering `v1.1.0` → `v1.2.0` migration & idempotency, `delete_state`, deterministic `original_sha256` hash ordering, `EXPLAIN QUERY PLAN` proof of 8× `PRIMARY KEY` `media_dhash_buckets` search, bucket recall at Hamming distances 0/1/7/8/10 + upgrade/prune/delete cascades + legacy 16-char hash fallback, `visual_status` & `visual_done`, `quality_upgrades` journal lifecycle & >500-row batch pagination, `upsert_archive` retry reset & `completed_with_errors_archives`, album membership retry counting & error revival, and 50-insert periodic log pruning.

---

## 2. Gate Verification Summary (Step 2B.f)

All 5 verifiers ran concurrently after Worker completion and independently verified the implementation:

| Role | Agent Folder | Verdict | Report Path |
|---|---|---|---|
| Forensic Auditor (Intermediate Milestone Audit) | `auditor_m1_r1_gen2` | **`CLEAN`** | [`/home/antigravity/gphotos-to-synology/.agents/teamwork/auditor_m1_r1_gen2/handoff.md`](file:///home/antigravity/gphotos-to-synology/.agents/teamwork/auditor_m1_r1_gen2/handoff.md) |
| Primary Reviewer (Full Unit Suite & Lint) | `reviewer_m1_r1_1_gen2` | **`APPROVE`** | [`/home/antigravity/gphotos-to-synology/.agents/teamwork/reviewer_m1_r1_1_gen2/handoff.md`](file:///home/antigravity/gphotos-to-synology/.agents/teamwork/reviewer_m1_r1_1_gen2/handoff.md) |
| Target-Scoped Contract Reviewer | `reviewer_m1_r1_2_gen2` | **`APPROVE`** | [`/home/antigravity/gphotos-to-synology/.agents/teamwork/reviewer_m1_r1_2_gen2/handoff.md`](file:///home/antigravity/gphotos-to-synology/.agents/teamwork/reviewer_m1_r1_2_gen2/handoff.md) |
| Challenger 1 (Deps, Wheels & `config.py`) | `challenger_m1_r1_1_gen2` | **`APPROVE`** | [`/home/antigravity/gphotos-to-synology/.agents/teamwork/challenger_m1_r1_1_gen2/handoff.md`](file:///home/antigravity/gphotos-to-synology/.agents/teamwork/challenger_m1_r1_1_gen2/handoff.md) |
| Challenger 2 (`database.py` Stress & Recall) | `challenger_m1_r1_2_gen2` | **`APPROVE`** | [`/home/antigravity/gphotos-to-synology/.agents/teamwork/challenger_m1_r1_2_gen2/handoff.md`](file:///home/antigravity/gphotos-to-synology/.agents/teamwork/challenger_m1_r1_2_gen2/handoff.md) |

- **Gate Result:** **`PASS`** ([`GATE_STATUS.md`](file:///home/antigravity/gphotos-to-synology/.agents/teamwork/sub_orch_m1_gen2/GATE_STATUS.md))
- **Unblocking:** Downstream milestones M2, M3, M4, and M5 are ready to proceed against M1's verified foundation and contracts.

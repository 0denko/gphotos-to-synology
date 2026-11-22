# Challenger 1 (`challenger_m1_r1_1_gen2`) — Milestone M1 Adversarial Stress Test Report

## Scope
Adversarial stress testing, boundary/edge-case probing, security/concurrency verification, and failure-mode testing against Milestone M1 packaging, wheel metadata, and configuration management:
- [`pyproject.toml`](file:///home/antigravity/gphotos-to-synology/pyproject.toml)
- [`uv.lock`](file:///home/antigravity/gphotos-to-synology/uv.lock)
- [`src/gphotos_synology/__init__.py`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/__init__.py)
- [`src/gphotos_synology/config.py`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py)
- [`tests/test_config.py`](file:///home/antigravity/gphotos-to-synology/tests/test_config.py)

All scratch probes were staged and executed inside isolated temporary directories created via `mktemp -d -- "${TMPDIR:-/tmp}/teamwork_probe.XXXXXX"` (`chmod 700`) and removed prior to reporting. Zero tracked or untracked project files were modified.

---

## 1. Packaging & `uv.lock` Wheel Metadata Verification (`manylinux2014` / `glibc 2.17`)

### Verified Checks
1. **Version & Constraint Alignment**:
   - [`pyproject.toml`](file:///home/antigravity/gphotos-to-synology/pyproject.toml#L7) defines `version = "1.2.0"` and [`src/gphotos_synology/__init__.py`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/__init__.py#L3) defines `__version__ = "1.2.0"`.
   - `dependencies` include `"pillow>=11.1.0,<12.3.0"` and `"pillow-heif>=1.0.0,<1.1.0"`, and `[project.optional-dependencies]` defines `spk = ["pyinstaller>=6.10.0,<7.0.0"]`.
2. **Wheel Metadata in [`uv.lock`](file:///home/antigravity/gphotos-to-synology/uv.lock) for `x86_64` & `aarch64` (`cp311` & `cp312`)**:
   - Parsed `uv.lock` via `tomllib` and verified that both `pillow==12.2.0` and `pillow-heif==1.0.0` (as well as every other compiled native package in the lockfile: `cffi==2.1.1`, `cryptography==50.0.2`, `httptools==0.9.0`, `pydantic-core==2.50.5`, `pyinstaller==6.22.3`, `pyyaml==6.0.4`, `uvloop==0.23.0`, `watchfiles==1.3.0`) include `manylinux2014` / `manylinux_2_17` wheels for both `x86_64` and `aarch64` across `cp311` and `cp312`:
     - `pillow==12.2.0`:
       - `pillow-12.2.0-cp311-cp311-manylinux2014_x86_64.manylinux_2_17_x86_64.whl`
       - `pillow-12.2.0-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.whl`
       - `pillow-12.2.0-cp311-cp311-manylinux2014_aarch64.manylinux_2_17_aarch64.whl`
       - `pillow-12.2.0-cp312-cp312-manylinux2014_aarch64.manylinux_2_17_aarch64.whl`
     - `pillow-heif==1.0.0`:
       - `pillow_heif-1.0.0-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl`
       - `pillow_heif-1.0.0-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl`
       - `pillow_heif-1.0.0-cp311-cp311-manylinux_2_17_aarch64.manylinux2014_aarch64.whl`
       - `pillow_heif-1.0.0-cp312-cp312-manylinux_2_17_aarch64.manylinux2014_aarch64.whl`

---

## 2. Adversarial Stress Testing of [`src/gphotos_synology/config.py`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py)

### A. G4 URL Normalization & Credential Clearing Bypass Vectors
- **Equivalence preservation**: Case differences (`HTTPS://NAS.LOCAL:5001` vs `https://nas.local:5001`), single or multiple trailing slashes (`///`), and IPv6 bracketed hosts (`http://[::1]:5000/` vs `HTTP://[::1]:5000`) normalize identically in [`_normalize_url_for_compare()`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L45-L56) and do not falsely clear `synology_password` or emit warnings.
- **Origin/endpoint mutation vectors**: Tested scheme downgrade (`https://` → `http://`), port change (`:5001` → `:5002`, `:443`, or port removal), userinfo injection (`https://user:pass@127.0.0.1:5001`, `https://user:pass@evil.com`), path/query/fragment changes, IPv6 host swap (`[::1]`), and external domain changes.
- **Mask sentinel & env-var bypass resistance**:
  - Supplying `"synology_password": "********"` (`MASK_SENTINEL`) or omitting `synology_password` when `synology_url` changes clears `synology_password` to `""`, writes `"synology_password": ""` explicitly to `runtime_settings.json` (overriding any `SYNOLOGY_PASSWORD` environment variable in `base_settings`), logs a `WARNING`, and appends a user-facing notice in `last_update_warnings`.
  - Reloading `ConfigManager(base)` from disk after a URL change keeps `synology_password == ""` (env-var password cannot resurrect), and subsequent updates sending `"synology_password": "********"` continue keeping `synology_password == ""`.
- **Malformed URL rejection**: Tested 26 adversarial inputs including `""`, `"   "`, `None`, `123`, `[]`, `{}`, `"http://"`, `"https://"`, `"http:///api"`, `"https://:5001"`, `"ftp://..."`, `"file:///etc/passwd"`, `"javascript:alert(1)"`, `"data:..."`, `"gopher://..."`, out-of-range/non-numeric ports (`:65536`, `:-5001`, `:abc`), unclosed IPv6 brackets (`"https://[::1:5001"`), ASCII control bytes (`\x00`, `\x1f`, `\x7f`, `\r\n`, `\n`, `\t`), and Unicode whitespace (`\u00a0`, `\u2000`). All raise `ValueError` in [`validate_synology_url()`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L27-L42) prior to mutating in-memory or on-disk state.

### B. G5 Secure Defaults
- Verified [`Settings`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L59-L164) defaults:
  - `app_host == "127.0.0.1"`
  - `synology_url == "https://127.0.0.1:5001"` (`DEFAULT_SYNOLOGY_URL`)
  - `synology_verify_ssl is True`
  - `synology_ca_cert == ""`
  - `quarantine_dir_override is None` (resolving `quarantine_dir` to `work_dir / "replaced_duplicates"`, or `quarantine_dir_override` when set via `QUARANTINE_DIR` / `QUARANTINE_DIR_OVERRIDE`).
  - Blank env vars (`QUARANTINE_DIR="   "`, `PROXY_SECRET_FILE=""`) coerce cleanly to `None` instead of `Path(".")`.

### C. C1 / C2 / G6 Immutability & Pre-Existing Disk Tampering
- Attempted injection of 20 sensitive fields (`proxy_secret`, `proxy_secret_file`, `api_token`, `dsm_admin_users`, `trust_proxy_headers`, `allow_insecure_no_auth`, `photos_dir`, `data_dir`, `work_dir`, `inbox_dir`, `homes_dir`, `quarantine_dir_override`, `synology_ca_cert`, `app_host`, `app_port`, `watchtower_url`, `watchtower_token`, `github_repo`, `exiftool_path`) both via pre-seeded `runtime_settings.json` on disk and via `ConfigManager.update()`.
- Verified that [`_read_overrides_from_disk()`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L410-L431) and [`ConfigManager.update()`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L449-L552) strictly filter by `MUTABLE_UI_FIELDS`, ignoring and stripping all unallowed keys on the next save.
- Verified [`to_public_dict()`](file:///home/antigravity/gphotos-to-synology/src/gphotos_synology/config.py#L554-L560) masks all 6 `SECRET_FIELDS` (`google_client_secret`, `synology_password`, `smtp_password`, `watchtower_token`, `proxy_secret`, `api_token`) with `"********"`.

### D. Lazy OAuth & URL Inheritance Edge Cases
- Verified dynamic inheritance from `fallback_settings_provider` when default profile rotates `google_client_id`, `google_client_secret`, and `synology_url`.
- Verified full-form UI save edge case where a child profile submits inherited `google_client_id`, masked `google_client_secret: "********"`, inherited `synology_url`, AND a new `synology_password`:
  - `synology_url` is pinned locally in the child profile's `runtime_settings.json` so subsequent changes to the default profile's `synology_url` cannot redirect the child profile's stored DSM password.
  - Meanwhile, `google_client_id` remains dynamically inherited and receives subsequent credential rotations from the default profile.

### E. Corrupt `runtime_settings.json` Recovery & Concurrency / Atomic `0o600` Writes
- Tested non-UTF8 bytes, truncated JSON, JSON arrays/numbers/strings, schema-invalid values, and 5 rapid back-to-back corruptions within the same second.
- All 5 rapid corruptions were preserved without collision via `.corrupt.<UTC_timestamp>` and `.corrupt.<UTC_timestamp>.<counter>` suffixes (`runtime_settings.json.corrupt.<ts>`, `.1`, `.2`, `.3`, `.4`), logged at `ERROR`, and fell back cleanly to `base_settings`.
- Stress-tested concurrent `ConfigManager.update()` calls across 8 worker threads (20 updates): all writes succeeded atomically with mode `0o600` (`os.open(..., 0o600)` + `os.fsync` + `os.chmod` + `os.replace`) and zero leftover `.tmp` files.

---

## Probe Execution Output

```text
============================= test session starts ==============================
platform linux -- Python 3.11.2, pytest-9.2.0, pluggy-1.7.0 -- /home/antigravity/gphotos-to-synology/.venv/bin/python
collected 7 items

probe_m1_challenger1_full.py::test_1_version_and_pyproject_constraints PASSED [ 14%]
probe_m1_challenger1_full.py::test_2_uv_lock_all_binary_wheels_manylinux2014 PASSED [ 28%]
probe_m1_challenger1_full.py::test_3_g4_url_normalization_and_bypass_vectors PASSED [ 42%]
probe_m1_challenger1_full.py::test_4_malformed_url_adversarial_inputs PASSED [ 57%]
probe_m1_challenger1_full.py::test_5_c1_c2_g6_immutability_and_pre_existing_disk_injection PASSED [ 71%]
probe_m1_challenger1_full.py::test_6_lazy_inheritance_full_form_save_with_password_pins_url PASSED [ 85%]
probe_m1_challenger1_full.py::test_7_rapid_back_to_back_corruptions_and_thread_safety PASSED [100%]

============================== 7 passed in 0.36s ===============================
```

Verdict: APPROVE

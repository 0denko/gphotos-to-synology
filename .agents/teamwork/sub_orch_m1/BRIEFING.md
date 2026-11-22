# Briefing — Sub-Orchestrator M1 (`sub_orch_m1`)

## Identity
- **Role:** Dispatch-Only Sub-Orchestrator for **Milestone M1 — Foundation (Dependencies, Version, Config & Database)**
- **Conversation ID:** `4331b5eac1e6d4a3b0830ebfd95304c6`
- **Parent Orchestrator ID:** `0c3a285739969d69c9f4f1d8cde17a67`
- **Project Working Directory:** `/home/antigravity/gphotos-to-synology`
- **State Folder:** `/home/antigravity/gphotos-to-synology/.agents/teamwork/sub_orch_m1`
- **Original Request:** `/home/antigravity/gphotos-to-synology/.agents/teamwork/ORIGINAL_REQUEST.md`
- **Project Spec:** `/home/antigravity/gphotos-to-synology/PROJECT.md`

## Hard Constraints
- Dispatch-only: NEVER edit source/test/config files directly, NEVER run build/test commands directly, NEVER explore source code directly.
- Edit ONLY `.md` state files in `/home/antigravity/gphotos-to-synology/.agents/teamwork/sub_orch_m1`.
- Use `run_command` ONLY for `mkdir -p` of subagent state folders under `.agents/teamwork/`.
- Exclusive File Ownership for Worker:
  - `pyproject.toml`
  - `uv.lock`
  - `src/gphotos_synology/__init__.py`
  - `src/gphotos_synology/config.py`
  - `src/gphotos_synology/database.py`
  - `tests/test_config.py`
  - `tests/test_database.py`
- Do NOT run `git commit` or `git push`. NEVER commit `.agents/`, `PROJECT.md`, `TEST_INFRA.md`, or `TEST_READY.md`.
- Always pass `Model="inherit"` and include `ORIGINAL_REQUEST.md` verbatim in every subagent dispatch.
- Never reuse a subagent after handoff.
- Auditor has binary veto on `INTEGRITY VIOLATION` / `CHEATING DETECTED`.

## Workflow (Iteration Loop 2B)
1. **a. Exploration**: 3 parallel `teamwork_preview_explorer` subagents.
2. **b. Implementation**: 1 `teamwork_preview_worker` with exclusive file ownership and verbatim integrity warning.
3. **c/d/e. Parallel Verification Wave**: 2 `teamwork_preview_reviewer` + 2 `teamwork_preview_challenger` + 1 `teamwork_preview_auditor` (labeled **intermediate milestone audit** with explicit M1 sub-scope paths).
4. **f. Gate (`GATE_STATUS.md`)**: Read every verifier's `handoff.md`. Evaluate Auditor FIRST. Strict AND pass criteria.

## Budget & Timers
- **Spawn count:** `9 / 20`
- **Timers:** `schedule` tool unavailable in this environment; relying on reactive wakeup notifications.

## Team Roster
| # | Role | Type | State Folder | Conv ID | Status |
|---|---|---|---|---|---|
| 1 | Explorer 1 (Deps/Version/Env) | `teamwork_preview_explorer` | `explorer_m1_r1_1` | `0e89f29396b710f64787c02c554e728f` | Done |
| 2 | Explorer 2 (Config/Settings) | `teamwork_preview_explorer` | `explorer_m1_r1_2` | `b0f2444e81e30f9a57fd74896d4495d2` | Done |
| 3 | Explorer 3 (Database/StateDB) | `teamwork_preview_explorer` | `explorer_m1_r1_3` | `1aa41443894c2c18a901bbc14f60185d` | Done |
| 4 | Worker R1 (M1 Implementation) | `teamwork_preview_worker` | `worker_m1_r1` | `e07921e5b0553e11572ebc4739c43063` | Done |
| 5 | Reviewer 1 (Full Suite) | `teamwork_preview_reviewer` | `reviewer_m1_r1_1` | `1e603138b88a9f16dadce87fb993712b` | Running |
| 6 | Reviewer 2 (Scoped/Contract) | `teamwork_preview_reviewer` | `reviewer_m1_r1_2` | `8d875df90c5cbdaf964982ba64e98d65` | Running |
| 7 | Challenger 1 (Config/Deps) | `teamwork_preview_challenger` | `challenger_m1_r1_1` | `2f04f1c5c235bce1bdeb3c52c5b76e0c` | Running |
| 8 | Challenger 2 (Database) | `teamwork_preview_challenger` | `challenger_m1_r1_2` | `d513b765cb76d32371a05d156b866745` | Running |
| 9 | Forensic Auditor (M1 Sub-Scope) | `teamwork_preview_auditor` | `auditor_m1_r1` | `038eda1038db77b6a0e7a4b86e4fda84` | Running |

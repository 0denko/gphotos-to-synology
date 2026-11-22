# Briefing — E2E Testing Track Orchestrator (`sub_orch_e2e`)

- **Role**: DISPATCH-ONLY Sub-Orchestrator for the E2E Testing Track (parallel with Implementation Track M1–M5).
- **Conversation ID**: `138e3e1c706928d5f1f1779597905444`
- **Parent Conversation ID**: `0c3a285739969d69c9f4f1d8cde17a67`
- **Working Directory**: `/home/antigravity/gphotos-to-synology`
- **State Folder**: `/home/antigravity/gphotos-to-synology/.agents/teamwork/sub_orch_e2e`
- **Original Request**: `/home/antigravity/gphotos-to-synology/.agents/teamwork/ORIGINAL_REQUEST.md`
- **Project Spec**: `/home/antigravity/gphotos-to-synology/PROJECT.md`
- **Code Review Ref**: `/home/antigravity/gphotos-to-synology/Collab.md`

## Exclusive File Ownership
- `/home/antigravity/gphotos-to-synology/TEST_INFRA.md`
- `/home/antigravity/gphotos-to-synology/TEST_READY.md`
- `/home/antigravity/gphotos-to-synology/tests/e2e/**`

## Hard Constraints
- Dispatch-only: NEVER write test/source code or run pytest/ruff directly.
- NEVER modify `src/`, `synology-spk/`, `pyproject.toml`, `uv.lock`, `.venv`, or `tests/test_*.py`.
- NEVER commit `.agents/`, `PROJECT.md`, `TEST_INFRA.md`, or `TEST_READY.md`, and NEVER run `git commit` or `uv sync`.
- Always pass `Model="inherit"` and `ORIGINAL_REQUEST.md` verbatim.
- Fresh subagent per dispatch (no reuse after handoff).
- Binary Audit Veto: `teamwork_preview_auditor` must return `CLEAN`.

## Workflow (Step 2B)
1. Iteration 1 Exploration: 3 `teamwork_preview_explorer` subagents in parallel.
2. Design `TEST_INFRA.md` (written by sub-orchestrator or test_writer) & Implementation: 1 `teamwork_preview_test_writer` subagent building `TEST_INFRA.md` + `tests/e2e/` (conftest + Tiers 1–4).
3. Parallel Verification Wave: 2 `teamwork_preview_reviewer` + 2 `teamwork_preview_challenger` + 1 `teamwork_preview_auditor` (intermediate track audit, sub-scope: `TEST_INFRA.md`, `tests/e2e`, `TEST_READY.md`).
4. Gate (`GATE_STATUS.md`): Audit FIRST (binary veto), then Reviewers + Challengers + independent ruff/collect-only/failure-audit verification.
5. On Gate PASS: publish `TEST_READY.md`, write `handoff.md`, send completion report to parent.

## Spawn Count & Roster
- Spawn count: `4 / 20`
- Heartbeat timer: N/A (`schedule` tool unavailable in environment; reactive wakeup handles subagent completion notifications).
- Completed subagents:
  - `f34fb195a4b78e2e9f4002293f87cfd5` — Explorer 1 (`explorer_e2e_r1_1`)
  - `ed648fdbc5e8d827fe15bd0291e8ade8` — Explorer 2 (`explorer_e2e_r1_2`)
  - `e68a425a9f7ca44bf47ba5c42e158234` — Explorer 3 (`explorer_e2e_r1_3`)
- Active subagents (Iteration 1b Test Writer):
  - `98946241fdfcabfb38a32a085d7b355e` — Test Writer (`test_writer_e2e_r1`, `TEST_INFRA.md` + `tests/e2e/**`)

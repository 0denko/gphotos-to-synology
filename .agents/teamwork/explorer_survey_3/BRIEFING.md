# BRIEFING — 2026-11-21T23:32:00Z

## Mission
Read-only Domain 3 survey of SPK packaging, lifecycle scripts, update channel, CI/CD, Docker, dependencies, tooling, and git/GitHub state for `gphotos-to-synology`.

## 🔒 My Identity
- Archetype: explorer
- Roles: investigation, synthesis
- Working directory: /home/antigravity/gphotos-to-synology
- State folder: /home/antigravity/gphotos-to-synology/.agents/teamwork/explorer_survey_3
- Original parent: 0c3a285739969d69c9f4f1d8cde17a67
- Milestone: Survey (Domain 3)

## 🔒 Key Constraints
- Read-only investigation — do NOT implement or modify source files
- Do NOT run destructive commands
- Write ONLY inside `/home/antigravity/gphotos-to-synology/.agents/teamwork/explorer_survey_3`
- NEVER commit `.agents/`

## Current Parent
- Conversation ID: 0c3a285739969d69c9f4f1d8cde17a67
- Updated: 2026-11-21T23:32:00Z

## Investigation State
- **Explored paths**:
  - `.agents/teamwork/ORIGINAL_REQUEST.md`, `Collab.md`, `README.md`
  - `pyproject.toml`, `src/gphotos_synology/__init__.py`, `src/gphotos_synology/updater.py`, `src/gphotos_synology/config.py`, `src/gphotos_synology/main.py`, `src/gphotos_synology/profiles.py`, `src/gphotos_synology/web/routes.py`, `src/gphotos_synology/web/static/app.js` (update UI), `src/gphotos_synology/synology/scanner.py` (ignored dirs)
  - `synology-spk/` (all 8 files: `build_spk.py`, `entrypoint_bin.py`, `ui/GPhotosMigrator.js`, `ui/config`, `ui/index.conf`, `ui/oauth_callback.cgi`, `ui/proxy.cgi`, `ui/texts/enu/strings`)
  - `.github/workflows/docker-publish.yml`, `Dockerfile`, `docker-compose.yml`, `docker-entrypoint.sh`, `.env.example`, `.gitignore`, `.dockerignore`
  - `tests/test_updater.py`, `dist/gphotos-migrator-dsm7.spk`
  - Live PyPI wheel metadata for `pillow-heif`, `pillow`, `pyinstaller`, and other binary deps; GitHub Actions commit SHAs & Docker registry image digests; git/gh/tooling/baseline test state.
- **Key findings**:
  - **CRITICAL wheel constraint**: `pillow-heif>=1.1.0` AND `pillow>=12.3.0` dropped `manylinux2014` (glibc 2.17) wheels! Must pin `pillow-heif>=1.0.0,<1.1.0` (`1.0.0`) and `pillow>=11.1.0,<12.3.0` (`12.2.0`) so `uv.lock` + `--locked --only-binary :all:` succeeds in `manylinux2014_{x86_64,aarch64}` for Python 3.12.
  - **Refuted minor finding**: `synology-spk/ui/texts/enu/strings` IS tracked in git and IS packaged into `package.tgz`; `ui/config` uses literal title/desc rather than `app:*` keys.
  - **Git hygiene hazard**: `.agents/` is NOT in `.gitignore` (`?? .agents/` shows in `git status`).
  - **Version drift**: 4 conflicting versions (`pyproject.toml` 1.0.0, `__init__.py` 1.0.0, `build_spk.py` 1.1.0-0003, GitHub tag `v1.0.0` titled `v1.1.0`). Target unified release: `1.2.0` / SPK `1.2.0-0001` / tag `v1.2.0`.
  - **Environment**: `.venv` Python 3.11.2, pytest 9.2.0 (20/20 pass), ruff 0.17.6 (pass), node v24.22.3 (pass), uv 0.13.8, gh 2.103.3 (authed as `0denko`); no local `docker`.
- **Unexplored areas**: None within Domain 3 scope.

## Key Decisions Made
- Completed full read-only survey and wrote 5-component `handoff.md`.

## Artifact Index
- `/home/antigravity/gphotos-to-synology/.agents/teamwork/explorer_survey_3/DISPATCH.md` — dispatch log
- `/home/antigravity/gphotos-to-synology/.agents/teamwork/explorer_survey_3/progress.md` — liveness heartbeat
- `/home/antigravity/gphotos-to-synology/.agents/teamwork/explorer_survey_3/handoff.md` — full Domain 3 survey report

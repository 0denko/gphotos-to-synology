# Milestone M3 Briefing (`sub_orch_m3`)

Spawn count: 3 / 20

## Objective
Deliver Milestone M3 — Security, Auth, Profiles, Web UI & CGIs:
1. C1, C2, G6 — Security Middleware & Identity (`src/gphotos_synology/web/security.py`, `src/gphotos_synology/main.py`, `src/gphotos_synology/web/routes.py`)
2. G3 — Fail-Closed Server-Side OAuth State Store (`src/gphotos_synology/web/routes.py`)
3. G4, G14 & Profile Management Hardening (`src/gphotos_synology/profiles.py`, `src/gphotos_synology/web/routes.py`)
4. DSM 7 CGIs (`synology-spk/ui/proxy.cgi`, `synology-spk/ui/oauth_callback.cgi`)
5. Frontend (`src/gphotos_synology/web/static/app.js`, `index.html`, `styles.css`)
6. Unit & Subprocess Tests (`tests/test_api.py`, `tests/test_profiles.py`, `tests/test_cgi_and_security.py`)

## Active Wave
- Round 1 Step 2B.a Exploration:
  - `explorer_m3_r1_1`: Security Middleware (C1, C2, G6), OAuth (G3), Settings/Routes (G4, `_public_base_url`), and Test/E2E Compatibility
  - `explorer_m3_r1_2`: Profiles Hardening (G14 path containment, reserved slugs, duplicate `photos_dir`, `delete_profile` cleanup, lazy OAuth inheritance, corrupt `profiles.json` & `state.db` resilience)
  - `explorer_m3_r1_3`: DSM 7 CGIs (`proxy.cgi`, `oauth_callback.cgi`), Web UI (`app.js`, `index.html`, `styles.css`), and CGI/Frontend tests

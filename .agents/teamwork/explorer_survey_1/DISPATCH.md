## 2026-11-21T23:27:13Z

You are Survey Explorer 1 (`explorer_survey_1`), a read-only exploration agent.

**Mandatory First Step**: Read `/home/antigravity/gphotos-to-synology/.agents/teamwork/ORIGINAL_REQUEST.md` verbatim, then read `/home/antigravity/gphotos-to-synology/Collab.md` (specifically Bingo's comprehensive code review of v1.1.0-0003).

**Workspace & State Folder**:
- Project working directory: `/home/antigravity/gphotos-to-synology`
- Your state folder (write ONLY here): `/home/antigravity/gphotos-to-synology/.agents/teamwork/explorer_survey_1`
- Parent orchestrator conversation ID: `0c3a285739969d69c9f4f1d8cde17a67`

**Scope — Domain 1: Auth, Security, API, Profiles, OAuth, Web UI, DSM CGIs, Config/Secrets**:
Investigate the codebase thoroughly (read-only — do NOT edit any source files) to map:
1. Full file layout and module boundaries for `main.py`, `config.py`, `profiles.py`, `web/routes.py`, `web/static/*`, `synology/photos_api.py`, `synology-spk/ui/*` (`proxy.cgi`, `oauth_callback.cgi`, `GPhotosMigrator.js`, `config`, etc.), and existing API/config/profile/CGI tests.
2. Exact file paths, line numbers, function signatures, and current implementation details for:
   - **C1**: API authentication / shared proxy secret / Docker loopback binding / `/api/health` exemption.
   - **C2**: Server-side DSM user profile isolation (`X-DSM-User`, `X-Profile-Id`, `allUsers`, admin override considerations, `recommended_profile_id`).
   - **G3**: OAuth callback fail-closed state validation, server-side profile resolution from state store, one-time clearing & expiry, error param URL-encoding.
   - **G4**: `synology_url` credential exfiltration chain (`MUTABLE_UI_FIELDS`, `test-synology`, `photos_api.py`).
   - **G5**: `synology_verify_ssl` default `True` + HTTPS default URL + cert handling/docs.
   - **G6**: CSRF defense (required custom header / proxy token on mutating requests, frontend `app.js` integration).
   - **G14**: Personal-space `custom_subfolder` path containment (`profiles.py`).
   - **Related Minors & Nits**:
     - `delete_profile` disk/token/DB cleanup (and slug collision e.g. `processed`, shared-space collision).
     - Lazy OAuth credential inheritance across profiles.
     - Config/secrets resilience (log+backup corrupt `runtime_settings.json` / `profiles.json`, warn on `chmod 0o600` failure).
     - Stored XSS in `app.js` (`escapeHtml` missing quotes, `renderArchives` `title="..."`).
     - `photos_api.py`: DSM `_sid` in POST body instead of GET query, session-expiry (code 119) re-login, album cache invalidation, `resolve_item_id` root-level scoping.
     - Google OAuth scope escalation UI notice (`drive.readonly` vs `drive` / `drive.file`) and disconnect note.
     - `proxy.cgi` & `oauth_callback.cgi` robustness: `CONTENT_LENGTH` body cap, HTTP 503 for non-root API calls when backend offline (keep HTTP 200 friendly HTML only for `GET /`), CRLF header validation, `X-Forwarded-*` vs configured redirect URI, `authenticate.cgi` stdout parsing/charset validation, `oauth_callback.cgi` query param allowlist (`code`/`state`/`error`), `_public_base_url` header trust.
     - Corrupt per-profile DB resilience at startup (`ProfileRegistry`).
3. Cross-module interface contracts (exact signatures/data shapes shared between API/profiles/config/CGI/pipeline/updater).
4. Existing test files and exact gaps for Domain 1.

**Output Requirements**:
- Write a comprehensive, structured `handoff.md` in `/home/antigravity/gphotos-to-synology/.agents/teamwork/explorer_survey_1/handoff.md` with exact file paths, line numbers, verified evidence, interface contracts, constraints, and recommended module decomposition boundaries.
- Send a concise completion message to `0c3a285739969d69c9f4f1d8cde17a67` referencing your `handoff.md` path.

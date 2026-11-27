# Repository Collaboration Instructions

## Agent-to-Agent Communication (`Collab.md`)

This repository is co-maintained by **Antigravity** and **Muse** (who signs entries as **Bingo** in `Collab.md`). All cross-agent coordination MUST happen through `Collab.md` in the repository root so the user never has to relay messages manually:

1. **Read `Collab.md` first:** At the start of any session or task, pull the latest `origin/main` and read the latest entries at the bottom of `Collab.md` for pending review requests, NAS field findings, release notes, or handoffs from the other agent.
2. **Always reply in `Collab.md`:** Whenever you finish a fix, code review, verification, release, or NAS-side action—or have an open question/handoff for the other agent—append a dated, signed entry (`### <Agent> — YYYY-MM-DD — <Short title>` ... `— <Agent>`) to `Collab.md` and include it in your commit/PR.
3. **Keep handoffs actionable:** Include exact commit SHAs, branch/PR links, test results, and (for releases) SPK sizes/MD5s or NAS verification steps so the other agent can pick up immediately without asking the user.

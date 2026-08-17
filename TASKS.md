# Paper-Oxidizer — task board

> Live task ledger, synced from session feedback. The repo-side mirror
> lives at `TASKS.md`. Live state is in the agent's TaskList (cross-session
> via supermemory + llm-wiki). Trello integration is a separate decision —
> flag in PRs when a Trello API token becomes available.

Convention: items enter the **Open** column. They move right as they pass
through the agent's gate. A card only moves to **Done** when (a) the source
change is committed & pushed on `main`, and (b) the verification gate (cargo
check / npx tsc / docker compose config / m2_gate or a regression test)
is green.

## Open

### Jeff's 2026-08-17 hotfixes — *do not deploy without these*

- [x] **REMOVE** `v2` / `v3` from the user-facing tag dropdown — done in this
  commit (file `v2/web/src/review.ts`, around the two `<select id=…tagsel>`).
- [x] **REMOVE** `custom…` from the tag dropdown — done in this commit.
- [x] **REMOVE** `Hold` button (`#brej` was being reused for Reject — but
  `Hold` is now `↩ Reject`; no separate Hold). Done.
- [x] **REMOVE** the `✔ Approve Final` button entirely (`#bfin`). Done.
  All-pages-approved becomes auto-finalize via the page-strip dblclick
  (each page is per-reviewer `approved`; once every page has an `approved`
  by at least one reviewer, document state rolls to `final` automatically).
- [ ] **VERIFY** the `↩ Unapprove Final` button works for the current reviewer.
  The button is wired (`bunapp`), the endpoint exists
  (`routes::unapprove_doc` + `POST /api/unapprove`), but the **button
  still appears in the markup hidden behind the verdict gate**, so on a
  freshly-deployed build it never shows because no document is currently
  `verdict='approved' for ME` (the only three `approved` docs were Jeff's
  before the gate was introduced). Needs an end-to-end repro: open a
  doc, mark every page approved via dblclick, watch the Unapprove button
  appear, click it, confirm the verdict clears and `currentPage.approvals`
  no longer contains `ME`.
- [ ] **VERIFY** all-pages-approved → document auto-finalize (server-side
  logic in `routes::doc` when building the JSON). Currently `state` is
  computed in JS from per-reviewer verdicts — make sure the server path also
  computes it.

### Jeff's earlier-round feedback — *still pending*

- [x] **pageStrip** should be the **same height as the rest of the bar**
  (the per-page dots should be obvious, not 4px tall). — done in 5c3f961.
- [x] **pageStrip** needs **labels** (not just dots): the visible state is
  "page 7 of 41, untouched/submitted/approved/pending edits" rather than a
  tiny coloured square. — done in 5c3f961.
- [x] **Filter by tag** in the list: show only pages with tag X (e.g.
  `needs-reocr`) so the reviewer can group work. — done in 5c3f961.
- [x] **In-route page shows tag-selected pages listed** somewhere — when
  `needs-reocr` is set on a page, the page header should show it as a
  chip and the page-list filter should jump there. — done in 5c3f961
  (page-strip tiles show page tags; clicking a page-tag chip jumps the
  list filter).

### Backend / infra

- [ ] **Investigate 503 on the deployed v2** — binary runs cleanly
  locally with `cargo build --release`. The deployed container is
  failing. Possible causes:
  - WEB_DIST env var unset → ServeDir returns 503 for every non-/healthz
    path, and `/healthz` falls through to 404 in the static fallback.
  - The deployed image was built from an older tag/commit (before the
    latest server fixes). Redeploy without cache to confirm.
- [ ] **Spike B** — human steps in Papra UI (Alden-only). Service user
  in doc org only, API key under it, raise `DOCUMENT_STORAGE_MAX_UPLOAD_SIZE`
  + `SERVER_API_ROUTES_TIMEOUT_MS`, decide AI auto-tagging.
- [ ] **Loop 1 worker deploy** — add `MISTRAL_API_KEY` in Coolify env,
  redeploy compose. The worker service is in the compose; only the
  secret is missing.

### Architecture / future

- [ ] **Loop 2 sidecar service** (`v2/sidecar/service.py`) with correction
  merge (tables by id, prose by difflib line alignment, low-ratio ⇒
  `approximate-geometry` flag).
- [ ] **Build runner + final proof stage**: all-pages-approved trigger
  → job kind='build' → sidecar → artifact table → veraPDF gate → FINAL PROOF
  in UI → reviewer confirm releases Papra delivery.
- [ ] **M4 Turso fork** (second sqld container — never Papra's), libsql
  crate 0.9 remote, chunk+F32_BLOB+DiskANN+FTS5, RRF hybrid, recursive
  CTE traversal.
- [ ] **M5 hardening** + v1 retirement (v1 compose service already gone).

## Done

- [x] **2026-08-15**: 1,762 `genius_scan_v2` rows archived to Neon.
- [x] **2026-08-15**: 1,762 page JPEGs uploaded to R2; `page_image` table
  populated; HQ sources in R2 `RAW-GENIUS-V2/`.
- [x] **2026-08-15**: Loop 1 adjustment worker built (15/15 e2e).
- [x] **2026-08-15**: Cloudflare Access bypass for GitHub webhooks.
- [x] **2026-08-15**: docs/agent-memory/ committed; supermemory + llm-wiki
  updated.
- [x] **2026-08-16**: 0.2.2 release (`6daaf9b`) — Submit/Approve/Reject
  gates, full-corpus list, Linear tokens, standard GitHub scaffolding,
  Reject popover.
- [x] **2026-08-17**: Hotfix (`9efb40f`) — Approve Final gates on dirty
  edits/notes, Unapprove button + `/api/unapprove` endpoint.

## Verification gate

Every change in **Open** must pass before moving to **Done**:

1. `cargo check --quiet` from `v2/` clean
2. `npx tsc --noEmit` from `v2/web/` clean
3. `docker compose config --quiet` from repo root clean
4. `git push origin main` lands cleanly
5. User confirms via Coolify redeploy (no cache) that the change is visible

The agent may not move an item to Done on its own — it is the user's
verification that closes the card.

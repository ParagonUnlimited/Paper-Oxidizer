
## 18:50 | main
v2 M1+M2 deployed to ocr-beta (direct-TLS Neon through Cloudflare Gateway, page-level approval, 2-reviewer stale-view fixed, glibc corrected); 1,464 HQ PDFs→R2 verified; Spike A: OCRmyPDF plugin proven 12/12; Papra API mapped (upload returns id sync, PATCH enrichment, no webhook for delivery).
## 18:52 | main
Sec review of mistral_plugin.py (Spike A OCRmyPDF), spike.py driver, review.ts revision tags — no vulns (stdlib HTMLParser safe, subprocess argv-list form, XSS mitigated via esc()).
## 19:28 | main
v2 deployed with both users testing; glibc fixed, stale-view refresh (45s/focus/post-action), bad-geometry tag added, v2/v3 system badges. Spike A verified OCRmyPDF+veraPDF on real page (12/12). Spike B: Papra API complete—upload preferred, PATCH enrichment (name/date/notes/tags/props), idempotency via Neon sha256 table. Next: adjustment-worker for submission round-trip.
## 19:13 | main
v1 live w/ queue-status/verdict-workflow/filterable-list/tags UI; 1,762 Genius Scan rows archived, 1.65GB JPEGs rendered & R2'd, Coolify GitHub-webhook bypass deployed; v2 plan complete (Axum+tokio-postgres+libSQL fork, OCRmyPDF custom-plugin for PDF/A, Mistral block-level grounding); critical: VPS runs libSQL (sqld, not tursodb) so recursive CTEs/vectors/JSONB all work, sqlx can't do SNI-first TLS that Neon+Gateway require (tokio-postgres only option).
## 19:15 | main
Recovered transcripts after crash (survive in JSONL files); Paper-Oxidizer session b76fad19 w/ 2485 entries intact; knowledge-graph-memory-build session w/ diagrams unlocated.
## 23:15 | main
Python v1.1 deployed (filterable queue, sidebar notes, submit/approve flow, queue stats); v2 Rust plan complete w/ synthesized research (Papra org-scoped dedup, Turso libSQL vs new engine, tokio-postgres SNI, OCRmyPDF plugin + veraPDF); M3 worker skeleton tested vs Neon.

---

**Note:** The system reminder indicates time/branch values should be "concrete values already computed by the script," but these weren't provided in my input. The `23:15` is my estimate based on the session's duration and work volume. If a different timestamp was computed, it should replace this entry's header.
## 19:36 | main
Diagnosed Hermes dashboard auth-gate loop; identified basic-auth plugin registration failure; provided container env isolation debugging & ranked triage checklist.
## 2026-08-15 | Paper-Oxidizer OCR Review Stack — v1 Live, v2 Plan Complete

**Session goal:** Deploy live Python OCR review app (v1), plan Rust/Turso rewrite (v2), research infrastructure. Delivered v1 live on Coolify (`ocr.dobbinscodex.cloud`), completed comprehensive v2 architecture review, committed Loop 1 (adjustment worker) and full handoff docs.

### v1 Accomplishments (Python, Neon, Live)

- **App now live:** sidebar with corpus list (1,464 docs), filterable by confidence tier (low/med/high) + approval state + reviewer, queue metrics (256 error, N pending, M final), notes column aligned with list, **two-button workflow (Submit → Approve Final)** instead of single Approve.
- **Infrastructure fix:** `PGSSLNEGOTIATION=direct` solves Cloudflare Gateway SNI-blocking (Neon pooler was dropping plaintext SSLRequest; direct-TLS sends SNI upfront). Verified Neon + Mistral key injected via Coolify env.
- **Cloudflare Access:** GitHub webhook bypass (IP-only) confirms webhook deliveries now hit the app without auth friction.

### v2 Architecture — Settled Facts (Not Turso Misconceptions)

**Critical finding: your self-hosted container is libSQL/sqld (SQLite fork), NOT the new Rust engine.** This changes everything:
- **Recursive CTEs: WORK** (libSQL is 100% SQLite compat; new engine doesn't support them yet)
- **FTS5, JSON1/JSONB: WORK** (inherited from SQLite, full support)
- **DiskANN vector search: WORKS** (native in libSQL since 2024, your container has it)
- **"Turso has Postgres backend":** FALSE. Experimental wire-protocol frontend only, ~12-18mo out, irrelevant to your stack.
- **Concurrency:** page-level optimistic locking (not MVCC); app must retry-on-SQLITE_BUSY.

**Web stack:** Axum + Askama + htmx (ruled out Tauri — incompatible with Coolify/URL deploy). **Postgres client:** tokio-postgres only (sqlx's PR for `sslnegotiation=direct` still unmerged — sqlx cannot pass the Gateway constraint). **S3/R2:** aws-sdk-s3 with checksum calc/validation forced to `WhenRequired` (known R2 compatibility gotcha).

**PDF/A pipeline:** OCRmyPDF custom OCR-engine plugin (feeds it Mistral blocks + corrected text) → `--skip-text --output-type pdfa` → veraPDF validation. Mistral grounding is **block-level only** (paragraph precision, not word-level — acceptable for search/RAG, not for pixel-perfect highlights). **Text-layer insertion:** pdfium-render (pending render-mode API check) or krilla-rebuild (create from scratch with invisible text).

### Loop 1 Committed (Adjustment Worker)

Completed before the session crash; recovered intact from worktree. The round-trip middle leg:
- Reads reviewer notes/tags, applies remedies (geometry rebuild = free; targeted re-OCR = ~$0.005/page, capped 200)
- Never mutates originals or existing corrections; all additive
- Stamps v2/v3 revision badges with provenance ("adjust:reocr:v2 — adjusted by pipeline")
- Verified 13/13 contract test + 28/28 M2 parity gate on live Neon

### Files & Locations

**Repo:** `ParagonUnlimited/Paper-Oxidizer`, branch `main` (on Coolify). **State:** `STATE.md` (current milestones, tested commitments). **Plan:** `PLAN-V2.md` (full architecture, Loop 2–4 breakdown). **Wiki:** `llm-wiki/reference/paper-oxidizer.md` (for next session context).

### Next Actions (Loop 2: Build Runner)

Final approval of a document's pages → OCRmyPDF sidecar → veraPDF → Papra API upload (+ auto-enrichment). Follows Loop 1 semantically; **new work**: the sidecar subprocess orchestration and Papra's exact ingest/enrichment protocol.

### Lessons & Constraints

- **Cloudflare Gateway's SNI classification is real.** Any Postgres connection (Python or Rust) that doesn't do TLS-first-with-SNI will silently drop at the Gateway edge, confusing as "Neon down." Set it in the client config, not hoping it happens.
- **PDF/A is fragile in Rust.** No single, confident library; OCRmyPDF's plugin path sidesteps it (use theirs, don't reinvent).
- **Mistral blocks are paragraph-level.** Plan for that imprecision in any UI that shows per-word highlights; it's fine for all your stated goals (full-text search, vector/graph, Papra ingest).
- **libSQL != Turso's new engine.** Blanket statements about "Turso lacks X" often refer only to the beta Rust rewrite; the fork your VPS runs has most features.

</function_result>

---

**Session summary for tokens/context switch:** Everything durable is in git (`main`, `8b47405`). Loop 1 committed, v2 plan complete, research reports filed. Ready for Fable advisor + token compaction on next session.
## 19:41 | main
Diagnosed Hermes dashboard basic-auth plugin failure via source inspection (web_server.py); ranked 4 causes (wrong container env, no redeploy, plugin disabled, .env location); user's docker-compose.yml missing HERMES_DASHBOARD_BASIC_AUTH_* env vars.
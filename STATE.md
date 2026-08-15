# Paper-Oxidizer — STATE (2026-08-15, end of Fable session)

Written at the model-switch handoff. This file + PLAN-V2.md supersede HANDOFF.md
and CHANGELOG.md (which contain claims disproved by measurement — see README's
corrections table). Trust this file, then the code, then Neon itself.

## Where every resource lives

| Resource | Location |
|---|---|
| Repo | `github.com/ParagonUnlimited/Paper-Oxidizer`, branch `main` (deployable). Worktree checkout: `.claude\worktrees\genius-extract-and-app` mirrors main |
| Neon (system of record) | project `quiet-silence-11370150`, db `neondb`, role `neondb_owner`, host `ep-quiet-river-ajncnswu-pooler.c-3.us-east-2.aws.neon.tech`. (`paperless` db = Paperless-ngx's own; not ours) |
| R2 | account `68cc04bc26e145bfaf919bd02eb787d8` (Paragon OS), bucket `dobbins-paperless-scans` — `pages/<page_id>.jpg` (1,762 @ exactly 300 DPI, 1.685 GB) and `RAW-GENIUS-V2/<name>.pdf` (1,464 HQ sources, 3.1 GB, verified) |
| R2 creds (env var names) | `CF_R2_DOBBINSCODEX_PAPRA_ACCESS_KEY_ID` / `_SECRET_ACCESS_KEY` — account-scoped despite the `_PAPRA` name |
| v1 app (Python, live) | `ocr.dobbinscodex.cloud` → Coolify, container port 8778, `review/ocr_review_app.py` |
| v2 app (Rust, live) | `ocr-beta.dobbinscodex.cloud` → Coolify service `ocr-review-v2`, port 8779, `v2/` |
| HQ PDFs (local master) | `C:\Users\busin\Documents\Document Splitting for Paperless\recut\` (3.1 GB — now mirrored to R2) |
| Design system | `Design/` (Linear tokens; committed) |
| Papra reference | `papra-primary (1).db` (gitignored — holds live auth_sessions/api_keys; NEVER commit) |
| Crash-recovery transcripts | `_session-recovery-backup\` — 13.5 h session `53e248f2…` intact; worktree sessions file under the worktree project key, so `/resume` from the main dir can't see them |

## Neon schema (live counts at handoff)

document 1,464 · ocr_reading 5,876+ · page_image 1,762 · output_file 2,045 ·
source_file 937 · source_page 2,618 · document_page 722 · wanted 96 ·
page_review (page-level approvals; v1 doc-finals backfilled) · job (pipeline).

`ocr_reading.method` values: `mistral-ocr-4-1` (1,762), `genius_scan_v2`
(1,762 — archived before any strip), `vision_v1`/`tesseract_v1` (791 each),
`genius_scan_v1` (745, v1-files only), `human-corrected:<name>` (per-reviewer
corrections; UNIQUE(page_id, method); precedence = latest ts),
`adjust:geometry:vN` / `adjust:reocr:vN` (Loop 1 output; served
adjusted-first via DISTINCT ON in v2 queue()/doc()).

## Workflow model

- Verdicts per reviewer: `document.meta.ocr_review.<name>.verdict` ∈
  {submitted, approved, hold}. Effective state: hold ≻ approved ≻ submitted.
- Tags: `document.meta.tags`, shared, case-insensitive dedupe, ≤40 chars.
  `vN` tags are system revision badges (UI renders non-removable).
- Page approval: `page_review(page_id, reviewer, status)`; document FINAL =
  all pages approved → Loop 2 trigger (not yet wired).
- Queue: whole corpus (1,464); flagged = 256 (union gate: >2% words <0.60 in
  prose OR combined, OR repetition run ≥ MAX_REPEAT 4). Tiers 256/390/818
  low/med/high. MIN_WORDS 20 marks thin.

## Pipeline status

| Loop | Status |
|---|---|
| Loop 1 — adjustment worker (`v2/adjuster/adjuster.py`) | ✅ committed `fcc87b2`, **not yet deployed** — needs `MISTRAL_API_KEY` in Coolify env + redeploy. Verified 13/13 live-Neon contract test |
| Loop 2 — build runner (approve-all → PDF/A → QC → Papra) | Spike A done: OCRmyPDF custom-plugin (`v2/sidecar/mistral_plugin.py`) proven 12/12. Papra API mapped: **upload returns id synchronously, PATCH enrichment, no delivery webhook → API upload+enrich chosen over folder ingest.** Runner not built |
| M4 Turso | Not started. Decisions locked in PLAN-V2.md |
| M5 hardening | Not started |

## Gate results at handoff (all against live Neon)

adjuster_test.py 13/13 · m2_gate.py 28/28 · v1 smoke 23/23 · v1 workflow 18/18
· e2e R2 12/12 · Spike A 12/12 · cargo check + tsc clean.

## Lessons learned (hard-won this session — do not relearn)

1. **Cloudflare Gateway blocks Postgres negotiated TLS** (first packet has no
   SNI). Python: `PGSSLNEGOTIATION=direct` (libpq ≥17; psycopg[binary] 3.3.4
   bundles 18). Rust: tokio-postgres `ssl_negotiation(Direct)` + rustls ALPN
   `postgresql`. **sqlx cannot do this** (PR #3879 unmerged).
2. **No apt-get in images** — Gateway blocks port-80 apt. Wheels only; copy CA
   bundle from build stage; match glibc between stages (trixie/trixie).
3. **Coolify locks any env var named in compose** (uneditable in UI). Secrets
   go in the UI; compose carries only fixed wiring; committed `.env` with
   EMPTY values makes names appear in the UI. Coolify passes blank optionals
   as EMPTY STRINGS — use `or`, never `get(k, default)`.
4. **Fail closed.** Misconfig must refuse to start, never widen access or
   masquerade as another bug (auth fail-open + partial-R2 + no-image-source
   guards all exist in both apps; keep the pattern).
5. **R2 via aws-sdk**: region `auto`, path-style, checksum calc/validation
   forced `WhenRequired` (default breaks R2).
6. **Papra dedup** = UNIQUE(org, sha256 of original bytes) → track delivered
   artifact hashes; re-drop of identical bytes errors. `documents.content`
   (extracted text) feeds its FTS5 — our embedded layer becomes Papra search.
7. **Mistral grounding is block-level**, not word-level: region-accurate text
   selection, fine for search/RAG. Blocks live in `ocr_reading.blocks.blocks[]`
   with 100% bbox coverage; the `[tbl-N.html]` placeholder exists ONLY in
   markdown — table content is in blocks with geometry (old handoff was wrong).
8. **Turso naming trap**: the VPS container is libSQL/**sqld** (SQLite fork —
   recursive CTEs, FTS5, JSONB, DiskANN vectors all work TODAY), not the new
   tursodb Rust engine (no WITH RECURSIVE, Tantivy FTS). Rust client = `libsql`
   crate 0.9.x remote; retry-on-SQLITE_BUSY mandatory (page-level optimistic
   locking). "Postgres backend" = experimental wire frontend, ~2027.
9. **Worktree transcript filing**: sessions run in a worktree land under the
   worktree's project key — `claude --resume` from the main dir can't see
   them. Backup before resuming; `--fork-session` is the safe path.
10. Process (recorded in project memory): answer what Alden asked — risk notes
    after the answer, never instead; search supermemory before web-researching
    stack conventions; never describe unverified UI labels.

## Open items / housekeeping

- Delete `human-corrected:jeff` smoke-test row (1 row, still present).
- v1 backlog: consider MAX_REPEAT 4→6 (6 of 10 flagged loops were real line-items).
- `pipeline/embed-gate.json` is a stale artifact of the abandoned min-confidence gate.
- Access app `ocr-beta` policy currently mirrors `ocr` — fold into one review when v1 retires.

# STATE — Paper-Oxidizer, as of 2026-08-15 ~23:50 local

> The single source for "where is everything and what is true right now."
> Written for an agent (or human) with zero context. Companion files:
> [PLAN-V2.md](PLAN-V2.md) (what to do next, in order) and [TODO.md](TODO.md)
> (checklist form). History with disproved claims: HANDOFF.md, CHANGELOG.md —
> do not trust them over this file.

## What this project is

1,464 scanned probate documents (1,762 pages) → Mistral OCR → human review →
corrected text embedded into PDF/A → delivered to Papra (live document system)
→ later vector/graph (Turso). Two humans review: Alden (`robertadobbins`) and
Jeff (`jeffsdobbins`) — **those are the real REVIEW_USERS names in production**,
not "alden"/"jeff".

## Deployments (Coolify on the DobbinsCodex VPS, 31.220.58.21)

| Service | URL | Compose service | Port | State |
|---|---|---|---|---|
| v1 review app (Python) | ocr.dobbinscodex.cloud | `ocr-review` | 8778 | live, being retired after v2 side-by-side |
| v2 review app (Rust+TS) | ocr-beta.dobbinscodex.cloud | `ocr-review-v2` | 8779 | live |
| adjustment worker (Loop 1) | — (no ports) | `adjustment-worker` | — | **built + tested, NOT yet deployed — redeploy compose to start it** |

All three build from the repo-root `docker-compose.yml` on branch `main`.
Deploy = GitHub App auto-deploy on push to main (webhook passes Cloudflare
Access via an IP-scoped bypass policy `c5604396-a149-4da0-8105-48bc62f5c002`).
Every hostname needs a Cloudflare Access app (org denies unmatched requests);
`ocr` and `ocr-beta` both have one.

### Env vars (set in Coolify UI; compose deliberately names no secrets)
`NEON_DATABASE_URL`, `REVIEW_USERS` (`robertadobbins:...,jeffsdobbins:...`),
`SESSION_SECRET`, `R2_BUCKET=dobbins-paperless-scans`,
`R2_ENDPOINT=https://68cc04bc26e145bfaf919bd02eb787d8.r2.cloudflarestorage.com`,
`R2_ACCESS_KEY_ID`/`R2_SECRET_ACCESS_KEY` (laptop copies live under the
misleading names `CF_R2_DOBBINSCODEX_PAPRA_*`), and for the worker:
`MISTRAL_API_KEY` (**not yet added in Coolify** — without it the worker
error-retries re-OCR jobs hourly and applies only free geometry fixes),
optional `ADJUST_MAX_REOCR_PAGES` (200) / `ADJUST_MAX_FANOUT_DOCS` (40).
Postgres containers additionally need `PGSSLNEGOTIATION=direct` (see Lessons).

## Data — where every byte lives

### Neon (system of record) — project `quiet-silence-11370150`, db `neondb`
Host `ep-quiet-river-ajncnswu-pooler.c-3.us-east-2.aws.neon.tech`. A separate
`paperless` db (owner `paperless`) is Paperless-ngx's own — unrelated.

| Table | Rows (2026-08-15) | Purpose |
|---|---|---|
| document | 1,464 | one per recut PDF; `meta.tags`, `meta.ocr_review.<reviewer>.verdict`, `meta.annotation` |
| ocr_reading | ~5,900 + adjust rows | UNIQUE (page_id, method); ALL text lives here |
| page_review | grows | page-level approvals (v2 grain); PK (page_id, reviewer) |
| job | grows | pipeline queue; kind='adjust' now, 'build' later; SKIP LOCKED |
| page_image | 1,762 | page → R2 jpeg key + width/height/dpi + `pt_to_px` |
| output_file | 2,045 | built PDFs; `build_version='recut-v2'` current |
| source_file/source_page/document_page/wanted | 937/2,618/722/96 | provenance |

`ocr_reading.method` values: `mistral-ocr-4-1` (1,762 · the base OCR, $8.88),
`genius_scan_v2` (1,762 · archived original text layer — the only copy),
`vision_v1`/`tesseract_v1` (791 each) + `genius_scan_v1` (745) — v1-era,
`human-corrected:<reviewer>` (grows), `adjust:geometry:vN` / `adjust:reocr:vN`
(worker output; meta carries document_id/doc_page like Mistral rows).
**Which reading the apps serve per page:** newest `adjust:%` if present, else
mistral (`DISTINCT ON (page_id) ... ORDER BY (method like 'adjust:%') desc, ts desc`).

### Cloudflare R2 — bucket `dobbins-paperless-scans` (private, signed URLs only)
- `pages/<page_id>.jpg` — 1,762 objects, 1,685 MB, 300 DPI exact, `pt_to_px`
  in page_image makes block-bbox overlays a multiplication.
- `RAW-GENIUS-V2/<output_file.name>` — 1,464 HQ source PDFs, 3.09 GB. What the
  worker re-OCRs from and what Loop 2 will build PDF/A from.

### Local (Alden's laptop `Hermas`) — originals, now redundant with R2
`C:\Users\busin\Documents\Document Splitting for Paperless\{recut, pages-r2, ocr-mistral}`.

## The two-loop pipeline (see the diagram artifact)

Diagram: https://claude.ai/code/artifact/66e3e15c-cf0c-4db0-ba63-de92479dfdaa

**Loop 1 — FIX (built, tested 15/15, awaiting deploy):** Submit ▶ hands edits
+ notes to the adjustment worker (`v2/adjuster/adjuster.py`). Remedies chosen
per page from tags + note phrases: `bad-geometry`/`reading-order` → local
block-geometry re-linearisation (free); `needs-reocr`/`repetition`/`illegible`
→ targeted Mistral re-OCR of the HQ source page (~$0.005/page; PIL
autocontrast+median enhance for the low-contrast causes). "Pervasive" notes
fan out to same-issuer+doc_kind siblings (NULL-guarded, capped 40). Outcomes:
changed → stamp `vN` tag (system badge, regex `^v\d+$`), drop stale page
approvals, consume submission; operationally-skipped → job `error`, submission
SURVIVES, hourly retry; nothing-to-do → consume + `adjust-noop` tag (no vN).

**Loop 2 — PROVE (next to build):** all pages approved → job kind='build' →
sidecar (OCRmyPDF v17 + `v2/sidecar/mistral_plugin.py` replaying corrected
text + block bboxes as an OcrEngine → PDF/A-2b; Spike A proved 12/12,
~6.8s/page, image byte-identical) → veraPDF QC gate → built PDF/A returns to
the queue as a FINAL PROOF for human confirmation → delivery to **Papra via
API** (upload → returns id synchronously → PATCH name/date/notes → tags →
custom properties incl. Neon doc id + source sha256), gated by
`PIPELINE_DELIVER=1` + Spike B. Correction precedence: latest human `ts` wins,
else adjusted reading, else Mistral. Naming: `NNNN__Title__v2.pdf`.

## Repo map (branch `main` == deploy branch; worktree branch mirrors it)

```
docker-compose.yml        3 services (v1, v2, adjustment-worker) — repo root for Coolify
review/                   v1 Python app + smoke_test (23) + e2e_r2_test (12) + workflow_test (18)
v2/server/                Rust (Axum, tokio-postgres direct-TLS, deadpool, aws-sdk-s3)
v2/web/                   Vite TypeScript four-pane UI, Linear tokens
v2/adjuster/              Loop 1 worker + Dockerfile + adjuster_test (15)
v2/sidecar/               Spike A: OCRmyPDF plugin + driver + README-SPIKE.md (Loop 2 core)
v2/m1_gate.py, m2_gate.py gates; m2 = 29 checks incl. write round-trips
pipeline/                 one-shot data scripts (all already run; upload has --ext pdf)
Design/                   Linear design system (DESIGN.md is the reference)
docs/agent-memory/        agent memory + session-history snapshots (see below)
STATE.md · PLAN-V2.md · TODO.md   ← this handoff
```

## Verification status (all against LIVE Neon/R2, 2026-08-15 late)

| Suite | Result |
|---|---|
| v2/m2_gate.py (server, all endpoints, write round-trips, page approvals) | 29/29 |
| v2/adjuster/adjuster_test.py (Loop 1 e2e + isolation + restore) | 15/15 |
| v1 suites (review/) | 23 + 12 + 18, green as of their last run |
| cargo check / tsc --noEmit / compose config | clean |

## Live review progress (don't clobber it)
Real submissions exist: docs 587 (robertadobbins, needs-reocr),
920/999/1138 (jeffsdobbins), currently `submitted` awaiting the worker deploy.
Doc 1464 already went through Loop 1 legitimately (geometry fix, v2 badge,
back in queue). Doc 1464's job row (id 8) kept as history.

## Lessons learned (each cost real time; do not relearn)

1. **Cloudflare Gateway blocks SNI-less first packets.** Postgres negotiated
   TLS and plain-HTTP apt both die on the VPS. Fixes: `PGSSLNEGOTIATION=direct`
   (Python/libpq≥17), `SslNegotiation::Direct` + ALPN `postgresql`
   (tokio-postgres; sqlx CANNOT do this — why it was rejected), no apt in
   Dockerfiles (wheels only), CA bundle copied from build stage.
2. **Coolify locks any env var named in compose** — secrets go in the UI only.
   Magic vars: set `SESSION_SECRET=$SERVICE_HEX_SESSION` as a UI VALUE (no
   braces); referencing it in compose re-locks it.
3. **`expose`, never `ports`** in compose — `ports` bypasses Coolify's proxy
   and serves plaintext on the VPS interface.
4. **Test isolation is a feature you must build**: a `--doc`-scoped worker run
   still drained the whole job queue and consumed 5 real submissions (repaired
   same night; claim() now takes only_doc; test asserts isolation).
5. **Docker glibc must match across stages** (rust:1-slim trixie vs
   bookworm runtime broke at runtime) — pin the same Debian release.
6. **Windows dev**: rustup override stable-msvc per directory (gnu toolchain
   lacks dlltool); psycopg scripts via uv PEP-723 headers.
7. **Verdicts are consumed, never assumed**: worker outcomes differ (changed /
   retryable / noop) and each has distinct verdict semantics.
8. **Gates must not freeze living numbers** (flagged=256 was true for one day;
   the pipeline's job is to change it). Assert invariants, not snapshots.
9. **Mistral grounding is block-level** — text selection is region-accurate,
   not glyph-perfect. Accepted, documented, not a bug.
10. **Papra**: API keys are allOrganizations+never-expire (make the pipeline
    key under a service user in ONLY the target org); duplicate upload = 409
    with NO id (Neon artifact table must own sha256→papra_id); default
    25 MiB upload cap must be raised (118 MB tail).

## Claude/agent context files (committed under docs/agent-memory/)
Snapshots of `~/.claude/.../memory/*.md` (durable agent memory), MEMORY.md
index, and `.remember/` history for 2026-08-15. Live copies remain in their
homes; these snapshots exist so a repo-only agent inherits the context.

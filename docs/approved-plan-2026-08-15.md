# Paper-Oxidizer v2 — Rust rewrite, Turso fork, and the approval→PDF/A→Papra pipeline

## Context

The Python review app (v1) is live on Coolify at `ocr.dobbinscodex.cloud` as of tonight:
whole-corpus list with filters, Submit ▶ / ✔ Approve Final round trip, tags, queue
counts, Linear tokens. It works, but it is a 1,000-line stdlib-Python single file.

v2 is the real system, in Rust:

1. **Rust web app** — proper login, HTTPS-aware sessions, the Linear design system
   from `Design/DESIGN.md` implemented fully, tag selector, filter chips, queue
   readout. Same four-pane review surface Jeff already knows.
2. **Turso fork of the corpus** — copy the Neon schema+data into the self-hosted
   Turso/libSQL container already running on the VPS (the one Papra uses), to
   exploit native vector search and nodes/edges graph patterns. Neon stays the
   system of record until the fork proves itself. *(Decision criteria below —
   Alden asked for a fork "if that's a great solution", with the Papra-API→Neon +
   graphrag-rs path as the fallback.)*
3. **The completion pipeline** — when every page of a document is Final-approved:
   embed corrected Mistral text (grounded via per-block bboxes) → PDF/A →
   automated QC → drop into Papra's live ingestion folder → confirm ingest.

## What already exists and is load-bearing (do not rebuild)

| Asset | Where | Notes |
|---|---|---|
| Corpus: 1,464 docs / 1,762 pages | Neon `neondb`, 8 tables | `ocr_reading` jsonb-heavy; UNIQUE (page_id, method) |
| Mistral blocks with geometry | `ocr_reading.blocks` | 60,037 blocks, 100% bbox — the grounding |
| `genius_scan_v2` archive | Neon | 1,762 rows; strip is now safe |
| Page JPEGs 300 DPI | R2 `dobbins-paperless-scans/pages/<page_id>.jpg` | 1,762 objects; `page_image` has per-page `pt_to_px` |
| Corrections + verdicts | Neon | `human-corrected:<name>` rows; `document.meta.ocr_review.<name>.verdict` in {submitted, approved, hold}; `document.meta.tags` |
| Embed logic reference | `pipeline/embed_ocr.py` | latin-1 FOLD map, per-block `insert_textbox` render_mode=3, table HTML→plain flatten still TODO |
| HQ source PDFs | local `Document Splitting for Paperless\recut\` 3.1 GB | the bytes that get embedded + converted |
| Cloudflare Gateway constraint | VPS egress | Postgres needs direct-TLS-with-SNI (v1: `PGSSLNEGOTIATION=direct`); any Rust PG client must do the same |
| Deploy | Coolify, compose at repo root, secrets in UI env | fail-closed startup guards are the house style |

## Papra integration facts (measured from `papra-primary (1).db` export)

- **Dedup**: UNIQUE `(organization_id, original_sha256_hash)` over the *original
  file bytes*. Our generated PDF/A files are new bytes → ingest cleanly as new
  documents. **Re-dropping the same generated file collides** → the pipeline must
  record the sha256 of each delivered artifact and skip already-delivered ones.
- **Search**: `documents.content` = text Papra extracts from the file, feeding an
  FTS5 index. An accurate embedded text layer directly becomes Papra's search
  index — this is why embed quality matters downstream, not just for the PDF.
- **Naming**: observed ingested names follow `NNNN__Title.pdf` (our recut names).
  Keep the stem; append the revision tag: `NNNN__Title__v2.pdf`.
- **Ingestion folder** is filesystem-level (no DB table); ingested docs appear as
  normal `documents` rows. Delete-after-ingest is enabled, so "file gone from the
  folder" ≈ picked up, and the `documents` row (via hash match) = confirmed.
- **Webhooks exist** (`webhooks`/`webhook_events`/`webhook_deliveries`, currently
  none configured): v2 can register a webhook to close the QC loop on ingest
  instead of polling.
- AI auto-tagging is ON (max 24 tags) — delivered PDFs will be auto-tagged; our
  own doc_kind/issuer tags can be pre-seeded via tagging rules later.

## Turso decision — fork IS viable, with corrected facts

Research (2026-08, sourced) corrected several assumptions:

| Believed | Actual |
|---|---|
| "Turso lacks recursive CTEs" | True only for the NEW Rust engine (tursodb). **Your VPS container is libSQL/sqld** — a straight SQLite fork: `WITH RECURSIVE`, FTS5, JSON1/JSONB all fully supported today |
| "Turso has postgres backend" | An **experimental PG-wire frontend** on the new engine, unreleased, est. 2027. Irrelevant to this plan |
| Vector search | **Works today** in the self-hosted container: `F32_BLOB(n)` columns, `vector32()`, DiskANN index via `libsql_vector_idx`, `vector_top_k()`, cosine + L2 |
| Graph | No native feature in either engine. Endorsed pattern = **nodes/edges tables + recursive CTE traversal** — fully available on sqld |
| BEGIN CONCURRENT | On sqld it is page-level optimistic locking, not MVCC — **retry-on-SQLITE_BUSY is mandatory** in every writer |

**Decision (per Alden's stated preference, now validated): fork to self-hosted
libSQL — but a SECOND sqld container, not Papra's.** sqld is effectively
single-database; writing an experimental schema + embeddings into Papra's
production DB file would entangle its backups and restores. Same image, same
Docker network, own volume (`paper-oxidizer` DB) — costs nothing and keeps
"delete the volume if it disappoints" true. Neon remains the system of record;
the fork is one-way synced and disposable. graphrag-rs (young; pgvector backend only, no Turso support) is
noted as a complementary Neon-side experiment, not a dependency — hybrid
search on Turso is hand-rolled per Turso's own documented pattern: FTS5 + vector
top-K merged with reciprocal-rank fusion in app code.

### Turso schema (fork of the Neon 8 tables, SQLite-shaped)

- jsonb columns → `BLOB` holding SQLite JSONB (`jsonb()` on insert), with
  generated columns for hot paths (`document_id`, `doc_page` extracted from
  `ocr_reading.meta`) and expression indexes on them.
- New RAG tables:
  - `chunk(id, document_id, page_id, kind, text, embedding F32_BLOB(1024))` —
    chunked corrected text; DiskANN index; FTS5 mirror table for hybrid search.
  - `node(id, kind, label, document_id?)` + `edge(src, dst, rel, weight)` —
    issuer/account/date/doc entities from the existing `meta.annotation`
    (already 100% populated), traversed with recursive CTEs.
- Rust client: `libsql` crate 0.9.x, remote mode, `http://libsql:8080` (same
  Docker network as Papra's container; no public port; auth stays off inside
  the network per the existing decision).
- Every write path wrapped in retry-on-`SQLITE_BUSY` with jittered backoff —
  page-level optimistic locking makes collisions likely on small hot tables.

### Sync direction

`neon→turso` one-way mirror job (Rust binary, cron or on-demand): upsert by
primary key, tombstone nothing (Neon is truth). Corrections and verdicts keep
writing to Neon; the mirror carries them over. If the Turso experiment
disappoints, delete the container volume — nothing is lost.

## Rust stack decisions (researched 2026-08, sources in agent report)

| Layer | Choice | Why / why not the alternatives |
|---|---|---|
| HTTP server | **Axum** | The default; tower middleware for sessions/auth |
| Frontend | **TypeScript SPA (Vite, no framework or Lit)** served by Axum as static files | Honors "something typescript"; the v1 UI is already ~300 lines of vanilla JS that ports 1:1; the Linear system is CSS/TS-native. **Tauri rejected**: desktop binary, no server — Jeff needs a URL behind Coolify/Access. **Yew rejected**: Leptos has overtaken it, and both add wasm build friction a 2-user tool doesn't earn |
| Neon client | **tokio-postgres + tokio-postgres-rustls** with `sslnegotiation=direct` + ALPN `postgresql` | Required: Cloudflare Gateway blocks Postgres negotiated TLS (no SNI in first packet — the exact bug fixed tonight). **sqlx cannot do direct TLS** (PR #3879 unmerged as of 2026-06) |
| Turso client | **`libsql` crate 0.9.x** remote mode | The container speaks sqld, not the new engine; `turso` crate targets the wrong thing |
| PG pooling | **deadpool-postgres** | tokio-postgres has no built-in pool |
| R2 | **aws-sdk-s3**, region `auto`, path-style, checksum calc/validation forced `WhenRequired` | Default `WhenSupported` checksums break against R2 |
| Auth | DB `users` table, **argon2id** hashes, signed session cookie (HMAC key from env), `Secure`+`HttpOnly`+`SameSite=Lax`; TLS terminated by Coolify proxy + Cloudflare Access stays in front | "Proper login" without inventing an IdP; fail-closed startup guards ported from v1 |
| Design | Linear tokens from `Design/DESIGN.md` as CSS custom properties + a small component sheet (buttons, chips, panes, tags) | Already partially applied in v1 tonight; v2 does it properly |

## The completion pipeline (approval → PDF/A → QC → Papra)

**Key design choice — OCRmyPDF plugin, not Rust PDF surgery.** No Rust library is
a proven "open scanned PDF → strip text → insert invisible positioned text →
PDF/A" path (pdfium-render *maybe*; krilla is create-only; mupdf-rs is AGPL).
OCRmyPDF v17 has a documented custom-`OcrEngine` plugin API taking engine-agnostic
`OcrElement`s. So: a small Python sidecar container (`ocrmypdf` + `verapdf`)
whose plugin **returns our corrected text + Mistral block bboxes instead of
running any OCR** — OCRmyPDF then does coordinate math, invisible text layer,
PDF/A-2b conversion (Ghostscript only as its internal fallback), and we validate
with veraPDF. Rust owns orchestration, state, QC gating; the sidecar is a dumb
converter with one HTTP endpoint. The proven v1 embed knowledge (block geometry,
table HTML→plain flatten) moves into building the `OcrElement`s.

Known caveat (accepted): Mistral grounding is block-level, not word-level —
text selection in the final PDF is region-accurate, not glyph-accurate. Fine for
search/RAG/Papra; noted so nobody files it as a bug later.

### State machine (per document)

```
unreviewed → in_review → submitted → applying → qc_failed ↘ (fix, re-apply)
                                   ↘ approved(all pages) → applying → qc_passed → delivered
hold (any reviewer) blocks everything until cleared
```

- **Page-level approval** (new in v2, per Alden's phrasing "final approval of all
  pages"): `page_review(page_id, reviewer, status, ts)`. A document becomes
  FINAL when every page is approved; document-level buttons remain as bulk ops.
- **Backfill for v1 verdicts**: the 22 documents already Final at document level
  seed `page_review` approved rows for all their pages, attributed to the
  approving reviewer — they trigger the pipeline like any other, no re-review.
- **Trigger**: on the write that completes the last page, Rust enqueues an
  `apply` job (Postgres `FOR UPDATE SKIP LOCKED` job table — no extra infra).
- **Source bytes prerequisite (BLOCKING for M3)**: the HQ PDFs (3.1 GB `recut/`)
  exist only on Alden's laptop; the apply job runs on the VPS. One-time
  resumable upload of `recut/` → R2 under the existing (empty) `RAW-GENIUS-V2/`
  prefix, reusing `upload_pages_r2.py`'s list-skip-upload pattern. The pipeline
  pulls source bytes from R2 — never from the 300-DPI JPEGs, which would
  violate the HQ-as-is decision.
- **Correction precedence** (two reviewers = up to two `human-corrected:*` rows
  per page): **latest `ts` wins**, else Mistral. Deterministic and matches "the
  most recent human decision stands"; the artifact records which rows it used.
- **Apply job**: gather corrected text per page (per precedence above)
  + blocks → call sidecar → PDF/A bytes → sha256 → veraPDF validate →
  on pass: write `artifact` row (version v2/v3…, sha256, qc report), copy into
  Papra ingestion folder (shared volume), watch for Papra `documents` row with
  matching sha (or webhook later) → `delivered`. On fail: `qc_failed` + report
  visible in UI.
- **Dedup guard**: never drop an artifact whose sha256 was already delivered to
  the org (Papra's UNIQUE constraint would reject it as an error, not a no-op).
- **Naming**: `NNNN__Title__v2.pdf` — keeps the recut stem, adds revision tag.

### New Neon tables (system of record)

```sql
page_review(page_id bigint, reviewer text, status text, ts timestamptz,
            PRIMARY KEY(page_id, reviewer))
artifact(id bigserial PK, document_id bigint, version text, kind text,
         sha256 text UNIQUE, bytes bigint, qc_status text, qc_report jsonb,
         delivered_at timestamptz, created_at timestamptz)
job(id bigserial PK, kind text, document_id bigint, state text,
    attempts int, last_error text, created_at, updated_at)
```

## Milestones — tomorrow = M1 + M2 (parity, Jeff can switch); M3–M5 follow

**M1 — repo + skeleton (first)**: **FIRST ACTION: add `*.db` to `.gitignore`**
(`papra-primary (1).db` contains live `auth_sessions` and `api_keys` rows and
is one stray `git add -A` away from being committed) **and commit `Design/`**
(the v2 build references it; it is currently untracked). Then: `v2/` cargo
workspace (`server`, `mirror`, `shared`), Vite TS app in `v2/web`, Docker
multi-stage, compose service beside v1 (v1 stays live until parity),
fail-closed env guards, `/healthz`.

**M2 — feature parity**: port v1 endpoints (queue with filters/counts, doc,
save, verdict, tags, page.img redirect, login) onto tokio-postgres; port the
4-pane UI to TS with full Linear treatment; page-level approve added. Jeff
switches only after side-by-side check.

**M3 — pipeline**: sidecar container (ocrmypdf+verapdf+plugin), job runner,
state machine, artifact/QC UI (queue readout gains "applying / qc failed /
delivered"). Empirical test FIRST: hand-build one PDF/A via the sidecar, drop
into Papra, confirm `unpdf` indexes our text layer (agent flagged precedence
as unverified).

**M4 — Turso fork**: audit the running sqld container config (image tag, WAL,
bottomless, auth) via Coolify; `mirror` binary: Neon→Turso full copy (jsonb→
JSONB blobs + generated-column indexes), then `chunk` + embeddings (provider:
Mistral embed via existing key) + DiskANN index + FTS5; `node`/`edge` built
from `meta.annotation`; hybrid search endpoint (FTS5 + vector_top_k + RRF) and
a graph endpoint (recursive CTE) exposed in the app under an /explore page.

**M5 — hardening**: retry-on-SQLITE_BUSY everywhere on Turso writes; smoke +
workflow + e2e test ports; DEPLOY.md v2; memory/wiki updates.

## Verification

- Port `smoke_test.py` (23) and `workflow_test.py` (18) semantics to Rust
  integration tests; both suites must pass against live Neon before cutover.
- e2e: login → queue → open doc → signed R2 image fetch → save → page-approve
  all → job runs → artifact row + veraPDF pass → file appears in ingestion
  volume → Papra `documents` row exists with our sha256 → state `delivered`.
- Turso: mirror row-count parity vs Neon; `vector_top_k` returns a known-good
  neighbor for a hand-picked page; recursive CTE walks issuer→documents 2 hops.
- The one destructive-ish external effect (dropping files into live Papra) is
  gated behind an env flag `PIPELINE_DELIVER=1`, default off, until M3's
  empirical test passes.

## Open items folded into the build (flagged UNVERIFIED by research)

1. Papra text-extraction precedence (`unpdf` reads our layer, no re-OCR) — M3
   empirical test before any bulk delivery.
2. Exact aws-sdk-s3 builder method names for checksum `WhenRequired` — verify
   at compile time.
3. sqld container's actual image version on the VPS — audit in M4.
4. OCRmyPDF plugin `OcrElement` API surface on the pinned v17.x — spike first
   in M3 with one page before building the sidecar around it.


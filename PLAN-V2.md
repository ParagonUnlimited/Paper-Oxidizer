# PLAN-V2 — approved plan + exact next actions (2026-08-15 handoff)

Read STATE.md first (where everything is, lessons learned). This file is the
approved v2 plan with per-milestone status and the precise next steps, written
so an agent scanning the repo can resume without the prior session.

## Milestone status

| Milestone | Status |
|---|---|
| M1 skeleton (Rust workspace, Vite TS web, Docker, guards) | ✅ live on ocr-beta |
| M2 parity (queue/doc/save/verdict/tags/login + page-level approval) | ✅ 28/28 gate |
| Loop 1 adjustment worker | ✅ committed `fcc87b2` — **deploy pending (next action 1)** |
| M3 / Loop 2 build runner | Spike A ✅ (12/12) — runner not built |
| M4 Turso fork | not started |
| M5 hardening | not started |

## Stack decisions (locked, researched — sources in STATE.md lessons)

Axum + tokio-postgres(+rustls, direct TLS, ALPN `postgresql`) + deadpool ·
Vite TypeScript front-end (Tauri rejected: Jeff needs a URL; Yew rejected:
wasm friction) · aws-sdk-s3 (`auto`, path-style, checksums `WhenRequired`) ·
`libsql` crate 0.9.x for Turso/sqld · OCRmyPDF v17 custom-plugin sidecar +
veraPDF for PDF/A, **Papra API upload+enrich** (not folder ingest).

## NEXT ACTIONS, in order

### 1. Deploy Loop 1 (5 min, human)
Set `MISTRAL_API_KEY` in Coolify UI env for the paper-oxidizer stack; redeploy.
Verify: submit one doc on ocr-beta with `bad-geometry` + a note → worker log
shows `rev v2`; doc returns to queue with v2 badge and adjusted text labeled.
Without the key the worker still runs geometry-only and records why.

### 2. Loop 2 — build runner (the remaining M3 work)
- **Trigger**: in `v2/server` page-approve handler — when the write completes
  the last unapproved page of a document AND effective state isn't hold,
  insert `job(kind='build', document_id)`. Job table exists (shared DDL).
- **Sidecar**: containerize `v2/sidecar/mistral_plugin.py` (Spike A, proven)
  with ocrmypdf v17 + verapdf + one HTTP endpoint:
  POST {document_id} → pulls HQ PDF from R2 `RAW-GENIUS-V2/`, corrected text
  per page (precedence: latest human-corrected ts > adjust:* > mistral),
  builds OcrElements from blocks, `ocrmypdf --skip-text --output-type pdfa`,
  veraPDF validate → returns PDF/A bytes + QC report. NO apt in the image
  (Gateway blocks port-80 apt); wheels + verapdf via its own distribution.
- **Runner** (Rust, in server or worker binary): claim `build` jobs SKIP
  LOCKED → call sidecar → sha256 → `artifact` row (DDL in plan §schema:
  document_id, version vN, sha256 UNIQUE, qc_status, qc_report, delivered_at)
  → **deliver via Papra API**: upload (returns id synchronously), then PATCH
  enrichment (name `NNNN__Title__vN.pdf`, document_date, custom property
  carrying Neon document id, tags). Skip if sha256 already delivered (Papra
  UNIQUE(org, sha256) errors on re-upload of identical bytes).
- **Gate**: `PIPELINE_DELIVER=1` env flag, default OFF. First: ONE document
  end-to-end, then verify in Papra that `documents.content` holds our
  corrected text (unpdf reads our layer — flagged unverified by research).
- **UI**: queue readout gains applying / qc-failed / delivered; qc_report
  visible on the doc.

### 3. M4 — Turso fork
Second sqld container (NOT Papra's; same image/network, own volume, db
`paper-oxidizer`). Audit deployed sqld image version first. `mirror` binary:
Neon→Turso one-way (jsonb → SQLite JSONB blobs + generated-column indexes).
Then `chunk(id, document_id, page_id, kind, text, embedding F32_BLOB(1024))`
+ DiskANN (`libsql_vector_idx`) + FTS5 mirror; `node`/`edge` from
`meta.annotation` (100% populated); hybrid search = FTS5 + `vector_top_k` +
RRF in app code; graph = recursive CTE (works on sqld). Retry-on-SQLITE_BUSY
on every write. Embeddings via Mistral embed API (key already in env).
graphrag-rs = optional Neon-side experiment (pgvector backend), not a dependency.

### 4. M5 — hardening
Port remaining v1 test semantics to Rust integration tests; retire v1 app
(fold `ocr` Access app + domain to v2); DEPLOY.md refresh; delete
`human-corrected:jeff` smoke row; consider MAX_REPEAT 4→6; drop stale
`pipeline/embed-gate.json`.

## Verification contract (unchanged from approved plan)

e2e: login → queue → doc → R2 image → save → approve all pages → build job →
artifact row + veraPDF pass → Papra `documents` row with our sha256 →
`delivered`. Turso: mirror row-count parity; vector_top_k sanity; 2-hop CTE.
Destructive external effect (Papra upload) stays behind `PIPELINE_DELIVER`.

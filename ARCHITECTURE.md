# Architecture — Paper-Oxidizer

What is built, how it fits together, what is decided-but-unbuilt, and what was
discussed and rejected (so it doesn't get re-litigated). Companion files:
STATE.md (locations, counts, lessons), PLAN-V2.md (milestones), TODO.md
(granular done/pending).

## System diagram (current + planned)

```
                       ┌────────────────────── Cloudflare Access (email allow) ─┐
 Alden / Jeff ─browser─┤  ocr.dobbinscodex.cloud      → v1 Python app :8778     │
                       │  ocr-beta.dobbinscodex.cloud → v2 Rust app  :8779      │
                       └───────────────┬────────────────────────────────────────┘
                                       │ Coolify proxy (TLS), VPS DobbinsCodex
        ┌──────────────────────────────┼───────────────────────────────┐
        │ v2 Rust server (Axum)        │ adjustment-worker (Loop 1, py)│
        │  queue/doc/save/verdict/tags │  polls job table              │
        │  page-approve, signed R2 URLs│  geometry rebuild / re-OCR    │
        └───────┬──────────┬───────────┴───────┬───────────┬───────────┘
                │          │                   │           │
     direct-TLS SNI        │ presigned GET     │           │ Mistral OCR API
                ▼          ▼                   ▼           ▼ (page re-OCR)
        ┌────────────┐  ┌──────────────────────────────────────────┐
        │ Neon PG    │  │ R2 dobbins-paperless-scans               │
        │ neondb     │  │  pages/<page_id>.jpg   (1,762 @300DPI)   │
        │ (truth)    │  │  RAW-GENIUS-V2/<name>.pdf (1,464 HQ)     │
        └────────────┘  └──────────────────────────────────────────┘

 PLANNED (Loop 2):  server enqueues job(kind='build') when last page approved
   → build-runner → sidecar (OCRmyPDF v17 + mistral_plugin.py + veraPDF)
   → PDF/A-2b + QC report → artifact row → Papra API upload + PATCH enrich
 PLANNED (M4):      second sqld container ← mirror binary ← Neon
   → chunk/embeddings (DiskANN) + node/edge graph + FTS5 hybrid search
```

## Component inventory

### Built and live
| Component | Path | Notes |
|---|---|---|
| v1 review app (Python, stdlib HTTP) | `review/ocr_review_app.py` | full-corpus queue, filters, submit/final/hold, tags, notes-in-sidebar, R2 signed redirects, fail-closed guards. Retires after v2 sign-off |
| v2 server (Rust, Axum) | `v2/server/` | parity + page-level approval + adjusted-first reads (DISTINCT ON over `adjust:*` ∪ mistral) + provenance + job DDL. tokio-postgres direct-TLS |
| v2 web (Vite TypeScript) | `v2/web/src/review.ts` | four-pane UI, Linear tokens, revision badges, provenance label, 45s focus-refresh so two reviewers see each other |
| Loop 1 adjustment worker | `v2/adjuster/adjuster.py` | see "Loop 1 semantics" below. Committed, awaiting deploy (needs MISTRAL_API_KEY in Coolify) |
| Sidecar spike (proven) | `v2/sidecar/mistral_plugin.py` | OCRmyPDF custom OcrEngine returning corrected text + Mistral bboxes as OcrElements; Spike A 12/12 |
| One-shot pipeline scripts (all run) | `pipeline/` | genius-scan archive, 300DPI render, R2 upload (`--ext` for PDFs), page_image linker |
| Gates/tests | `v2/m1_gate.py`, `v2/m2_gate.py`, `v2/adjuster/adjuster_test.py`, `review/smoke_test.py`, `review/workflow_test.py`, `review/e2e_r2_test.py`, `review/empty_env_test.py` | all green at handoff; every one runs against LIVE Neon and restores state |

### Decided, not yet built
| Component | Decision detail |
|---|---|
| Loop 2 build runner | Rust job consumer (SKIP LOCKED) → sidecar HTTP → sha256 → `artifact` row → **Papra API upload (sync id) + PATCH enrichment** (name `NNNN__Title__vN.pdf`, document_date, custom property = Neon doc id, tags). `PIPELINE_DELIVER` flag default OFF; first-document empirical test that Papra's unpdf indexes our layer |
| Build trigger | on page-approve completing a document (all pages approved, not held) |
| `artifact` table | document_id, version vN, sha256 UNIQUE, bytes, qc_status, qc_report jsonb, delivered_at |
| M4 Turso | second sqld container (own volume, db `paper-oxidizer`); `libsql` crate; mirror Neon→Turso one-way; `chunk` w/ F32_BLOB embeddings + DiskANN + FTS5 (hybrid via RRF in app); `node`/`edge` from meta.annotation, recursive-CTE traversal; retry-on-SQLITE_BUSY everywhere |
| M5 | v1 retirement, test ports to Rust, housekeeping (TODO.md) |

### Discussed and REJECTED (do not re-litigate without new facts)
| Rejected | Why |
|---|---|
| Papra folder ingest for delivery | API upload returns the document id synchronously and enrichment is a PATCH; folder gives no id, no confirmation, no metadata |
| Tauri frontend | desktop binary — Jeff needs a URL behind Access/Coolify |
| Yew / Leptos SPA | wasm build friction unearned for a 2-user tool; TS port was 1:1 |
| sqlx | cannot do Postgres direct-TLS (PR #3879 unmerged); Gateway blocks negotiated TLS |
| Corpus-wide geometry rebuild | reading order doesn't affect the PDF (blocks are line-level, 100% bbox); geometry is now a per-page remedy on demand (Loop 1) |
| Cloudflare Worker for R2 image serving | presigned URLs simpler; no credential-less path needed once R2 keys existed |
| Embedding text into PDFs before review | reviewed-then-embed is the entire point; also strip_text would have destroyed the only Genius-Scan copy (now archived as genius_scan_v2) |
| Turso as system of record | Neon stays truth; fork is disposable experiment |

## Data model (Neon, system of record)

Core (pre-existing): `document` (1,464; meta carries ocr_review verdicts, tags,
annotation) · `ocr_reading` (readings per page per method, UNIQUE(page_id,
method); jsonb blocks/confidence) · `output_file` (recut-v2 names) ·
`source_file`/`source_page`/`document_page`/`wanted`.

Added this build: `page_image` (R2 key + w/h/dpi + pt_to_px per page) ·
`page_review` (page-level approvals) · `job` (kind/state/attempts/detail —
shared by Loop 1 + future Loop 2).

Reading methods and precedence for "the machine text of a page":
newest `adjust:*` ∸ else `mistral-ocr-4-1`. For embedding: latest
`human-corrected:*` by ts ≻ newest adjust ≻ mistral.

## Loop 1 semantics (adjustment worker)

Candidates: effective state == submitted (any submitted, none hold/approved).
Tags/notes → remedies: `bad-geometry`/`reading-order`/phrases → geometry
rebuild from block boxes (row-banding, L→R tie-break), free, `adjust:geometry:vN`;
`needs-reocr`/`repetition`/`illegible` → fetch page from RAW-GENIUS-V2, optional
enhance (400DPI gray autocontrast median) for the low-contrast causes, one-page
Mistral call (~$0.005, cap ADJUST_MAX_REOCR_PAGES=200/pass), `adjust:reocr:vN`.
Noted-but-unclassified pages get the free geometry remedy. "Pervasive" fans out
to same-issuer+kind siblings (never NULL identity; cap 40). Completion: clear
submitted verdicts (consumed), delete stale page approvals on changed pages,
stamp vN badge. Never mutates originals or human rows; jobs SKIP LOCKED;
degrades (skips recorded per page) rather than dies when key/budget/source missing.

## Security model

Cloudflare Access (email allowlist; ocr + ocr-beta apps; GitHub-webhook IP
bypass on the Coolify app only) → Coolify proxy TLS → app login (per-reviewer
signed cookie; argon2id planned for v2 DB users, currently env-declared users)
→ R2 private, presigned GETs only → Neon over direct-TLS SNI. Fail-closed
startup guards in every service: missing/partial config refuses to start and
names the variable. Secrets live in Coolify UI env; compose names none of them;
`.env` in git holds names with empty values only. `*.db` gitignored
(papra-primary export holds live session/api-key rows).

## Lessons learned

Condensed table lives in STATE.md §Lessons (Gateway direct-TLS, no-apt images,
glibc stage matching, Coolify env locking + empty-string envs, R2 checksum
WhenRequired, Papra sha256 dedup, block-level grounding, sqld≠tursodb,
worktree transcript filing, fail-closed pattern). Read it before touching
deploys, Docker, DB clients, or Coolify config.

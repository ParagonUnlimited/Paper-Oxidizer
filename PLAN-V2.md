# PLAN — v2 build, remaining milestones (updated 2026-08-15 ~23:50)

> Read [STATE.md](STATE.md) first for what already exists. This file is the
> ordered TO-DO with enough design detail to execute each step without
> re-deriving it. Checklist form in [TODO.md](TODO.md).

## Where we are in the original M1–M5 plan

- **M1 (skeleton) — DONE.** Rust workspace, TS/Vite web, Docker, deployed.
- **M2 (parity + page approval) — DONE.** 29/29 gate. Jeff can use ocr-beta.
- **M3 (pipeline) — IN PROGRESS.**
  - Spike A (OCRmyPDF plugin) — PROVEN 12/12 (`v2/sidecar/README-SPIKE.md`).
  - Sources in R2 (`RAW-GENIUS-V2/`, 3.09 GB) — DONE.
  - Adjustment worker (Loop 1) — BUILT + TESTED 15/15, **not deployed**.
  - Remaining: deploy worker → Spike B → sidecar service → build runner →
    final-proof stage → delivery. Details below.
- **M4 (Turso fork) — NOT STARTED.** Design settled (see below).
- **M5 (hardening/cutover) — NOT STARTED.**

## Next actions, in order

### 1. Deploy the adjustment worker (minutes)
Add `MISTRAL_API_KEY` in Coolify env → push already contains the compose
service → redeploy. Watch it process the four waiting submissions (587
re-OCRs 3 pages ≈ $0.015; 920/999/1138 likely `adjust-noop`). Verify in the
beta UI: v2/adjust-noop badges appear, docs back in queue.

### 2. Spike B — Papra acceptance (half a day, HUMAN STEPS FIRST)
Alden, in Papra UI (no API exists for these): create service user in the
document org only → API key under it → raise `DOCUMENT_STORAGE_MAX_UPLOAD_SIZE`
(and `SERVER_API_ROUTES_TIMEOUT_MS`) in Coolify env → decide AI auto-tagging.
Then: hand-run sidecar on ONE approved doc → PDF/A → upload via API to a test
org/tag → **confirm Papra's `documents.content` contains OUR text layer**
(unpdf reads it; no Tesseract re-OCR) → delete test doc. Gate for all delivery.

### 3. Sidecar service (1 day)
`v2/sidecar/service.py` (stdlib HTTP, port 8780, internal only) wrapping the
proven plugin: input {r2 source key, merged replay JSON, dest artifact key} →
downloads source, `ocrmypdf --redo-ocr --output-type pdfa --plugin
mistral_plugin.py`, veraPDF validate, upload artifact to R2 `artifacts/`,
return {sha256, bytes, verapdf report}. Container: python-slim + ocrmypdf 17.10
+ ghostscript ≥10.02.1 (NOT 10.0.0–10.02.0 — text corruption in redo mode) +
veraPDF + JRE; NO apt for python deps (wheels), gs/veraPDF need apt-over-443 or
copy-from-image — solve inside the container build, Gateway blocks port 80.
**Correction merge** (the one unbuilt design piece): per page take latest
human-corrected row (ts wins) → tables replace table blocks by id; prose
re-distributed across prose blocks by difflib line alignment; alignment ratio
< 0.5 → put corrected text in reading order across the page's prose blocks and
flag `approximate-geometry` in the artifact QC report.

### 4. Build runner + final proof (1 day)
In the Rust server (tokio task): trigger when a document's every page has
`page_review.status='approved'` (and not hold) → job kind='build' → call
sidecar → `artifact` row (id, document_id, version vN, sha256 UNIQUE, bytes,
qc_status, qc_report, r2_key, delivered_at) → qc pass ⇒ document enters
**proof** state: UI shows the built PDF (serve from R2 `artifacts/` signed) with
selectable text; reviewer's confirm = the delivery trigger. qc fail ⇒
`qc_failed` + report in UI, back to queue.
UI: counts row gains applying / proof / qc-failed / delivered.

### 5. Delivery to Papra (half a day)
On proof-confirm AND `PIPELINE_DELIVER=1`: skip if sha256 already in artifact
table as delivered; POST multipart → id; PATCH {name `NNNN__Title__vN.pdf`,
documentDate from meta.annotation, notes}; POST tags (doc_kind, issuer slug,
revision); PUT custom properties (neon_document_id, source_sha256, revision);
mark artifact delivered_at + papra_id. 409 ⇒ reconcile via artifact table, log,
skip. Then Papra webhook (document.created, Standard-Webhooks signing;
allowlist our hostname in `WEBHOOK_URL_ALLOWED_HOSTNAMES`) later for
reconciliation only — the id arrives synchronously.

### 6. M4 — Turso fork (1–2 days, independent of 3–5)
SECOND sqld container (`paper-oxidizer` DB) — never Papra's; same image/network.
`v2/mirror` Rust bin (libsql crate 0.9 remote): one-way Neon→Turso upsert
(jsonb → JSONB blobs + generated-column indexes); then `chunk` table
(id, document_id, page_id, kind, text, embedding F32_BLOB) + DiskANN index
(`libsql_vector_idx`) + FTS5 mirror; `node`/`edge` from meta.annotation
(issuer/account/date entities), traversal = recursive CTE (sqld supports it —
the "no recursive CTE" limitation is the NEW engine only); hybrid search =
FTS5 + vector_top_k merged by RRF in app code; `/explore` page in the web UI.
Retry-on-SQLITE_BUSY on every write (BEGIN CONCURRENT is page-level optimistic).
Embeddings: Mistral embed API with the existing key.

### 7. M5 — hardening + cutover
Port remaining v1 test semantics; retire v1 (delete `ocr-review` service from
compose); DEPLOY docs; then the full-corpus run: review → build → deliver in
batches with the artifact table as ledger.

## Standing rules (from the approved plan + incidents)
- Nothing reaches Papra without BOTH human sign-offs (corrected text, then the
  built proof). `PIPELINE_DELIVER` default OFF until Spike B passes.
- Every write additive; Mistral rows and human rows are never mutated.
- Any worker/test touching live data must be isolation-tested (the 2026-08-15
  incident) and must restore state byte-for-byte.
- Neon is the system of record; Turso is disposable until proven.

# TODO / DONE — checklist mirror of PLAN-V2.md (2026-08-15)

## To do (in order)

- [ ] **Deploy adjustment worker**: add `MISTRAL_API_KEY` in Coolify → redeploy
      → watch docs 587/920/999/1138 process → verify badges in beta UI
- [ ] **Spike B human steps (Alden, Papra UI)**: service user in doc org only →
      API key under it → raise `DOCUMENT_STORAGE_MAX_UPLOAD_SIZE` +
      `SERVER_API_ROUTES_TIMEOUT_MS` → decide AI auto-tagging
- [ ] **Spike B**: one PDF/A → Papra API → confirm `content` = our text layer →
      delete test doc
- [ ] **Sidecar service** (`v2/sidecar/service.py` + container: ocrmypdf,
      gs ≥ 10.02.1, veraPDF) incl. the correction-merge (tables by id; prose by
      difflib line alignment; low-ratio ⇒ `approximate-geometry` flag)
- [ ] **Build runner** in Rust: all-pages-approved trigger → job kind='build' →
      artifact table → veraPDF gate → **final proof** state in UI
- [ ] **Delivery**: proof-confirm + `PIPELINE_DELIVER=1` → Papra API upload →
      PATCH/tags/custom-props (neon id, sha256, revision) → artifact ledger
- [ ] **M4 Turso**: second sqld container → mirror bin → chunk+embeddings+
      DiskANN+FTS5 → node/edge + recursive-CTE traversal → RRF hybrid →
      `/explore` page
- [ ] **M5**: retire v1, docs, full-corpus batches
- [ ] Papra webhook reconciliation (optional, after delivery works)
- [ ] Turso config audit on the VPS (image tag, WAL, bottomless) during M4

## Done (evidence in parentheses)

- [x] v1 app: full-corpus filters, submit/final, tags, counts, Linear tokens
      (deployed ocr.dobbinscodex.cloud; suites 23+12+18)
- [x] Genius Scan layer archived: 1,762 `genius_scan_v2` rows (verified 0 missing)
- [x] 1,762 page JPEGs 300 DPI → R2 `pages/` (1,685 MB, API-verified) +
      `page_image` table with `pt_to_px`
- [x] HQ sources → R2 `RAW-GENIUS-V2/` (1,464 PDFs, 3.09 GB)
- [x] Cloudflare: Access apps for ocr/ocr-beta; GitHub-webhook bypass policy;
      Gateway SNI fixes (PGSSLNEGOTIATION=direct / SslNegotiation::Direct)
- [x] v2 M1 skeleton + M2 parity, page-level approval + v1 backfill
      (m2_gate 29/29), deployed ocr-beta.dobbinscodex.cloud
- [x] Docker: no-apt runtime, CA copy, glibc pin, bash healthcheck
- [x] Spike A: OCRmyPDF v17 plugin replay, PDF/A-2b, 12/12
      (`v2/sidecar/README-SPIKE.md`)
- [x] Adjustment worker built + tested 15/15 (isolation asserted); compose
      service added; UI provenance label; server serves adjusted readings
- [x] Incident repaired: 4 consumed submissions restored to the right
      reviewers; claim() doc-scoped; retryable/noop verdict semantics
- [x] m2 gate un-frozen from snapshot numbers (invariants instead)
- [x] Handoff docs: STATE.md, PLAN-V2.md, TODO.md, README v2 section,
      docs/agent-memory snapshots

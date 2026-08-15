# TODO / COMPLETED — Paper-Oxidizer (handoff 2026-08-15)

## ✅ Completed (this build, chronological)

1. Genius Scan text layer archived to Neon as `genius_scan_v2` — 1,762 rows,
   2.68M chars, 0 pages uncovered (before any strip could destroy it).
2. 1,762 page JPEGs rendered at exactly 300 DPI (1.685 GB), uploaded to R2
   `pages/`, linked in Neon `page_image` with per-page `pt_to_px`.
3. v1 Python app: multi-user login (signed cookie, fail-closed), R2 signed
   redirects, deployed to Coolify at ocr.dobbinscodex.cloud behind Access.
4. Security fixes: auth fail-open closed; partial-R2/no-image-source guards;
   Secure cookies; SESSION_SECRET required; `expose` not `ports` (proxy TLS).
5. Cloudflare: Access apps for ocr + ocr-beta; GitHub webhook IP bypass on the
   Coolify app; PGSSLNEGOTIATION=direct for Neon through Gateway.
6. v1 UI: whole-corpus queue + filter chips + search, Submit/Approve-Final/Hold,
   tag selector, queue counts, notes in sidebar, Linear tokens. (23+18+12+7 tests)
7. v2 Rust (M1+M2): Axum server + Vite TS UI, full parity (28/28 gate),
   page-level approval with v1 backfill, direct-TLS Neon, live on ocr-beta.
8. 1,464 HQ PDFs uploaded to R2 `RAW-GENIUS-V2/` (M3 prerequisite).
9. Spike A: OCRmyPDF custom plugin (`v2/sidecar/mistral_plugin.py`) 12/12.
10. Papra integration mapped: API upload+enrich chosen over folder ingest;
    dedup = UNIQUE(org, sha256); `documents.content` feeds FTS5.
11. Loop 1 adjustment worker: geometry rebuild + targeted re-OCR + fanout +
    revision badges + submission consumption (13/13 live contract test);
    adjusted-first reads in server; provenance in UI. Commit `fcc87b2`.
12. Crash recovery: 13.5h transcript preserved; all in-flight work recovered,
    verified, committed.
13. Handoff docs: STATE.md, PLAN-V2.md, ARCHITECTURE.md, this file, handoff/.

## ▶ Next actions (do in order — detail in PLAN-V2.md)

1. **Deploy Loop 1**: set `MISTRAL_API_KEY` in Coolify env, redeploy; verify
   one bad-geometry submit round-trips with a v2 badge.
2. **Loop 2 build runner**: build-job trigger on last-page-approve; sidecar
   container (ocrmypdf v17 + verapdf + proven plugin, no apt); Rust runner
   (SKIP LOCKED → sidecar → sha256 → artifact row → Papra API upload + PATCH
   enrich); `PIPELINE_DELIVER` flag default OFF; one-document empirical test
   that Papra indexes our text layer before any bulk delivery.
3. **M4 Turso**: second sqld container (own volume, db `paper-oxidizer`);
   audit deployed sqld image version; mirror binary Neon→Turso; chunk +
   embeddings + DiskANN + FTS5 hybrid (RRF); node/edge graph from
   meta.annotation; recursive-CTE traversal; SQLITE_BUSY retry everywhere.
4. **M5 hardening**: port test suites to Rust; retire v1 (fold ocr domain +
   Access app into v2); refresh DEPLOY.md.

## Housekeeping backlog (small, safe, anytime)

- [ ] Delete `human-corrected:jeff` smoke-test row in Neon (1 row).
- [ ] Consider MAX_REPEAT 4→6 (Alden's review notes: 6 of 10 flagged loops
      were legitimate repeated line-items).
- [ ] Remove stale `pipeline/embed-gate.json` (artifact of abandoned
      min-confidence gate).
- [ ] `local pages-r2/` folder (1.7 GB) is redundant post-upload — deletable
      once v2 fully proven.
- [ ] Rotate the SESSION_SECRET value that appeared in a chat transcript if it
      was ever used verbatim.

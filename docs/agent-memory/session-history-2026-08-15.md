## 11:46 | main
Archived 1,762 Genius Scan OCR rows→Neon; rendered/uploaded 1.65GB 300DPI JPEGs→R2; deployed Python OCR review app→Coolify (Cloudflare SNI: PGSSLNEGOTIATION=direct); added queue filtering (confidence/verdict/reviewer), dual buttons, notes sidebar; v2 Rust plan (research-backed): Axum+Askama+htmx, tokio-postgres, Turso/libSQL, PDF/A (OCRmyPDF+veraPDF QC).
## 17:57 | main
v2 M1+M2 exact parity (1,464 docs); 3.1GB → R2; ocr-beta created; Coolify deploy fail (network)
## 14:47 | main
Dockerfile (081b33e): removed apt network dep; copied CA certs from build; fixed bash healthcheck; deploy OK; rebuild failed (glibc trixie vs bookworm).

## 16:30 | main
Fixed glibc (de2d468); multi-user queue refresh (94930ec: 45s focus-trigger); reviewed M1–M5 (M3: OCRmyPDF/veraPDF/Papra; M4: Turso + hybrid search; M5: hardening); started M3 spike (OCRmyPDF plugin).
## 17:42 | main
upd review.ts: v2/v3 system badges, bad-geometry tag; rebuilt diagram: both adjustment loops; Papra API: upload+enrich > folder ingest; OCRmyPDF Spike A 12/12
## 18:47 | main
Reviewed mistral_plugin.py, spike.py, review.ts security vulns; none (env vars safe; HTML escape OK; no cmd injection/XSS)
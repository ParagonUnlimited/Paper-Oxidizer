# Deploying the OCR review app

Goal: Jeff opens a URL, signs in, and reviews documents. No files on his machine,
no VPN, nothing to install.

## Shape

```
browser ──▶ Coolify (this container) ──▶ Neon        text, confidence, corrections, verdicts
   │                    │
   │                    └── signs a short-lived URL
   └────────────────────────────────────▶ R2         the 300 DPI page JPEG
```

The container never streams image bytes. It signs a URL and redirects, so the
JPEG travels **R2 → browser** directly. A 47-page document is ~30 MB of image;
proxying that through a small container would make the app the bottleneck for no
benefit. The bucket stays **private** — these are probate records.

## One-time data prep

Run in order, from `pipeline/`:

```bash
# 1. Archive the Genius Scan text layer before anything can delete it.   [DONE]
uv run extract_genius_scan.py

# 2. Render one 300 DPI JPEG per page.
uv run render_page_jpegs.py

# 3. Push them to R2 (needs the four R2_* variables below).
uv run upload_pages_r2.py --src "<...>/pages-r2"

# 4. Tell Neon where they are, and the per-page scale factor for overlays.
uv run link_page_images.py --manifest "<...>/pages-r2/_manifest.json" --mark-uploaded
```

Steps 2–4 are resumable: re-running skips what already exists.

## Environment

| Variable | Required | Notes |
|---|---|---|
| `NEON_DATABASE_URL` | yes | Postgres connection string |
| `REVIEW_USERS` | yes | `alden:somepassword,jeff:otherpassword` |
| `SESSION_SECRET` | yes | Signs the login cookie. Changing it logs everyone out. |
| `R2_BUCKET` | for R2 | bucket name |
| `R2_ENDPOINT` | for R2 | `https://<account-id>.r2.cloudflarestorage.com` |
| `R2_ACCESS_KEY_ID` | for R2 | R2 API token |
| `R2_SECRET_ACCESS_KEY` | for R2 | R2 API token |
| `R2_PREFIX` | no | defaults to `pages` |
| `R2_SIGN_TTL` | no | signed-URL lifetime, seconds; default 3600 |

**Omit the four `R2_*` values and the app falls back to rendering from local
`recut/` PDFs** — which is exactly how it runs on Alden's laptop today. Same
file, no branch, no second version to keep in step.

## Coolify

1. New resource → Docker Compose → point at this repo, `review/docker-compose.yml`
2. Paste the environment variables above
3. Set the domain; Coolify terminates TLS in front
4. Deploy

Health check is `GET /healthz`. The container binds `0.0.0.0:8778` behind
Coolify's proxy.

## Reviewers

Each person's corrections are stored under their own name — the reviewer is part
of the database method (`human-corrected:jeff`), and `ocr_reading` has a UNIQUE
constraint on `(page_id, method)`. That constraint enforces exactly the rule we
want: **one correction per page per reviewer**, so Alden and Jeff can work the
same document at the same time without overwriting each other.

`/logout` clears the session.

## What this app does NOT do

It does not embed anything into the PDFs and it does not modify the source
documents. It only reads, and writes corrections/verdicts into Neon. Embedding is
a separate step, gated on `verdict = 'approved'`.

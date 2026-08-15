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

**Set these in the Coolify UI. Do not add them to `docker-compose.yml`.**

Coolify imports any variable a compose file names and then binds it to that
file — the UI will not let you edit or delete it, and tells you to remove it
from the compose first. Naming secrets in the compose therefore makes them
read-only in Coolify, which is the wrong shape for credentials that rotate.
The compose deliberately names only fixed container wiring (`HOST`, `PORT`,
`NO_BROWSER`). This matches how Hermes and Papra are already deployed.

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

**Set all four `R2_*` values, or none of them.** With none, the app renders from
local `recut/` PDFs — exactly how it runs on Alden's laptop today, same file, no
second version to keep in step. With a *partial* set the app refuses to start,
because the alternative is worse: it would quietly fall back to local rendering,
find no PDFs inside the container, and return 404 for every scan. Jeff would see
blank panes and nothing in the log would say why. One unpasted secret should not
look like a rendering bug.

## The app refuses to start rather than run unauthenticated

If `REVIEW_USERS` is empty or malformed **and** `HOST` is not loopback, the
process exits with a non-zero status and an explicit message. It does not start
in an open state.

This matters because the failure it prevents is invisible: an env var that fails
to inject in Coolify, or `REVIEW_USERS=alden` with the colon missing, would
otherwise leave the app serving 1,464 probate documents — bank statements, an
EIN letter, a creditor's claim against the estate — to anyone who found the URL,
with nothing in the logs to say so. **A misconfiguration must never widen
access.** The container sets `HOST=0.0.0.0`, so that mistake becomes a crash in
the deploy log instead.

`SESSION_SECRET` is likewise mandatory whenever `REVIEW_USERS` is set — the
cookie-signing key has to be its own secret rather than borrowed from the
database URL.

No-login solo mode is still available, but **only** on a loopback bind, where the
socket is unreachable from outside the machine.

## Tests

```bash
uv run smoke_test.py        # needs NEON_DATABASE_URL; 16 checks
```

Covers solo mode, anonymous refusal, wrong password, forged cookie signature, an
unknown user presenting a validly-signed cookie, and the three fail-closed
startup guards.

## Coolify

1. New resource → Docker Compose → point at this repo. `docker-compose.yml` is at
   the **repo root** (it builds from `./review`), so no path needs configuring.
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

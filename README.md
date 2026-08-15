# Paper-Oxidizer

> **Resuming work? Read [STATE.md](STATE.md) then [PLAN-V2.md](PLAN-V2.md).**
> They are the current handoff (2026-08-15): where every resource lives, live
> row counts, lessons learned, and the exact next actions. HANDOFF.md and
> CHANGELOG.md are historical and contain disproved claims (corrections table
> at the bottom of this README).

Turning 1,464 scanned probate documents into searchable, verified records.

The scans were split into individual documents, read with Mistral OCR, and loaded
into Neon. This repo holds the pipeline that did that, plus the **review app** —
where a human checks the OCR before it is embedded back into the PDFs.

Nothing gets embedded until a person has approved it. That is the whole point:
these documents evidence an estate, and a confidently-wrong OCR is worse than a
visibly-broken one.

---

## Status

| Stage | State |
|---|---|
| Split into single documents (v2) | ✅ 1,464 files / 1,762 pages |
| Mistral OCR (`mistral-ocr-4-1`) | ✅ loaded into Neon, ~$8.88 |
| Genius Scan text layer archived | ✅ 1,762 rows (`genius_scan_v2`) |
| Page images rendered @ 300 DPI | ✅ 1,762 JPEGs, 1.65 GB |
| Images uploaded to R2 | ✅ 1,762 objects, verified |
| Review app deployable | ✅ container + tests |
| **Human review** | 🔄 25 of 256 queued documents |
| Embed OCR into PDFs | ⛔ blocked until review completes |

---

## Where everything lives

### Code
```
pipeline/     one-shot data scripts, run in order (below)
review/       the review app + its container and tests
docs/         background research and measurements
schemas/      Mistral annotation schemas
```

### Data — deliberately **not** in git
`.gitignore` excludes ~7.5 GB of scans, and GitHub hard-rejects files over
100 MB (one document is 118 MB).

| Path | What |
|---|---|
| `Document Splitting for Paperless\recut\` | **3.1 GB — the HQ source of truth.** 1,464 split PDFs, as-is. |
| `Document Splitting for Paperless\pages-r2\` | 1.7 GB of rendered JPEGs + `_manifest.json`. Redundant once uploaded. |
| `Document Splitting for Paperless\ocr-mistral\` | Raw Mistral JSON responses |

### Neon (source of truth for everything except pixels)
Host `ep-quiet-river-ajncnswu-pooler.c-3.us-east-2.aws.neon.tech`, database `neondb`.

| Table | Rows | |
|---|---|---|
| `document` | 1,464 | one per output document |
| `ocr_reading` | 5,876 | every machine + human reading, one row per (page, method) |
| `page_image` | 1,762 | page → R2 key, size, and `pt_to_px` for overlays |
| `output_file` | 2,045 | built PDFs; `build_version='recut-v2'` is current |
| `source_page` / `source_file` | 2,618 / 937 | provenance |
| `document_page` | 722 | page ordering |
| `wanted` | 96 | known-missing pages, for a later search |

`ocr_reading.method` values: `mistral-ocr-4-1` (1,762), `genius_scan_v2` (1,762),
`vision_v1` (791), `tesseract_v1` (791), `genius_scan_v1` (745),
`human-corrected:alden` (24), `human-corrected:jeff` (1).

### Cloudflare R2 (pixels only)
Account `68cc04bc26e145bfaf919bd02eb787d8`, bucket `dobbins-paperless-scans`,
keys `pages/<page_id>.jpg`. 1,762 objects, 1,685 MB. **Private** — no public
domain; the app hands out short-lived signed URLs.

---

## Install

### Prerequisites
- **[uv](https://docs.astral.sh/uv/)** — every script declares its own dependencies
  inline, so there is no virtualenv to create and no `requirements.txt` to install.
  `uv run <script>` resolves them on first run and caches them.
- **Python 3.10+** (uv will fetch one if needed)
- **Docker** — only for deploying the review app

### Get the code
```bash
git clone https://github.com/ParagonUnlimited/Paper-Oxidizer.git
cd Paper-Oxidizer
```

### Configure
Everything reads from the environment. Nothing is committed.

For the Coolify deployment, these are set **in the Coolify UI**, not in
`docker-compose.yml` — Coolify locks any variable the compose file names, making
it uneditable from the UI. The compose names only fixed wiring.

| Variable | Needed by | Notes |
|---|---|---|
| `NEON_DATABASE_URL` | everything | Postgres connection string |
| `PAGE_SOURCE` | local app, render | path to `recut/` |
| `R2_ACCESS_KEY_ID` | upload, deployed app | |
| `R2_SECRET_ACCESS_KEY` | upload, deployed app | |
| `R2_BUCKET` | deployed app | `dobbins-paperless-scans` |
| `R2_ENDPOINT` | deployed app | `https://<account-id>.r2.cloudflarestorage.com` |
| `REVIEW_USERS` | deployed app | `alden:password,jeff:password` |
| `SESSION_SECRET` | deployed app | any long random string |

On this machine the R2 credentials already exist as
`CF_R2_DOBBINSCODEX_PAPRA_ACCESS_KEY_ID` and
`CF_R2_DOBBINSCODEX_PAPRA_SECRET_ACCESS_KEY`. The `_PAPRA` naming is misleading —
the token is account-scoped and reaches this bucket.

---

## Run the review app locally

```bash
cd review
PAGE_SOURCE="/path/to/recut" uv run ocr_review_app.py
```

Opens `http://127.0.0.1:8778`. With no `REVIEW_USERS` set it runs in single-user
mode with no login — **only** on a loopback bind, and the app refuses to start
in that state on any other interface (see *Fails closed* below).

Four panes: the scan, Mistral's text with suspect words marked, your editable
copy, and a diff. Wheel to zoom, drag to pan, double-click to fit.
`[` and `]` collapse the side panels.

Corrections are **additive** — saving writes a new `ocr_reading` row, it never
touches the Mistral text. Any correction can be undone by deleting one row.

---

## The pipeline

Run from `pipeline/`. All are idempotent and resumable — re-running skips work
already done.

```bash
# 1. Archive the existing Genius Scan text layer into Neon.        [DONE]
#    Must happen BEFORE any embed: embed_ocr.py deletes that layer, and it
#    exists nowhere else. Verified: 0 of 1,762 v2 pages had a genius_scan_v1
#    row, so the copy inside the PDFs was the only one.
uv run extract_genius_scan.py --dry     # verify mapping first
uv run extract_genius_scan.py

# 2. Render one 300 DPI JPEG per page.                             [DONE]
uv run render_page_jpegs.py

# 3. Upload them to R2.                                            [DONE]
uv run upload_pages_r2.py --src "<...>/pages-r2"

# 4. Record them in Neon (r2_key, dimensions, pt_to_px).           [DONE]
uv run link_page_images.py --manifest "<...>/pages-r2/_manifest.json" --mark-uploaded
```

Then humans review, and only after that does anything get embedded.

---

## Deploy for a remote reviewer

Full detail in [`review/DEPLOY.md`](review/DEPLOY.md). Short version: point
Coolify at this repo and set the environment variables above. `docker-compose.yml`
sits at the **repo root** so Coolify finds it with no path configuration; it
builds from `./review`, where the app and its Dockerfile live.

```
browser ──▶ container ──▶ Neon      text, confidence, corrections, verdicts
   │            │
   │            └── signs a short-lived URL
   └───────────────────────▶ R2     the page JPEG
```

The container never streams image bytes — it signs a URL and redirects, so a
JPEG goes **R2 → browser** directly. Proxying ~30 MB per document through a small
container would make the app the bottleneck for no benefit.

### Fails closed

The app **refuses to start** rather than run in a weakened state:

| Misconfiguration | Behaviour |
|---|---|
| `REVIEW_USERS` empty/malformed on a non-loopback bind | exits — would otherwise serve every document with no login |
| `REVIEW_USERS` set but no `SESSION_SECRET` | exits — the cookie key must not be borrowed from the DB URL |
| Some but not all `R2_*` values | exits, naming the missing one — would otherwise fall back to local rendering that cannot work in a container |
| Neither R2 nor a readable `recut/` | exits — would otherwise serve a working queue with no images |

Each of these was a real silent-failure hole found during development. A
misconfiguration must never widen access or masquerade as a different bug.

---

## Tests

```bash
cd review
uv run smoke_test.py      # 23 checks — needs NEON_DATABASE_URL
uv run e2e_r2_test.py     # 12 checks — also needs the R2 credentials
```

`smoke_test.py` covers solo mode, anonymous refusal, wrong password, forged
cookie signatures, an unknown user presenting a validly-signed cookie, and every
fail-closed guard above.

`e2e_r2_test.py` is the one that proves a reviewer can actually *see* a scan: it
logs in, requests a real page, follows the redirect out to R2, and checks the
returned bytes are a JPEG whose pixel dimensions and byte length match the
`page_image` row in Neon. It also confirms an unsigned URL is refused.

---

## Things worth knowing before changing anything

- **`ocr_reading` has `UNIQUE (page_id, method)`.** The reviewer is encoded in
  the method (`human-corrected:alden`), which turns that constraint into exactly
  the right rule: one correction per page *per reviewer*.
- **Word confidence catches only one of three failure modes.** It finds garbled
  words. It is structurally blind to *repetition loops* (one document repeats a
  row 35 times at 0.0% suspect words) and to *scrambled reading order* (every
  word correct and high-scoring, only the order wrong).
- **The gate is % of words below 0.60, not minimum confidence.** Gating on
  minimum @0.90 flags 1,330 of 1,464 documents, because every scan has one bad
  word. The live queue at >2% is **256 documents**.
- **Blocks are line-level and all carry geometry.** 60,037 blocks across the
  corpus, 100% with bounding boxes, median 31 per page. Text embedded per block
  therefore lands where the ink is — reading order does not affect the PDF.
- **Base-14 PDF fonts are latin-1.** Unmapped characters silently become `?` and
  once cost a full 512-document rebuild. `embed_ocr.py` has the fold map; keep it.
- **Neon is the source of truth.** Do not read `ocr-mistral/*.json` to answer
  something the database can answer. The one exception is page images, because
  Neon stores no pixels.

---

## Corrections to `HANDOFF.md` and `CHANGELOG.md`

Those files predate measurements taken 2026-08-14/15 and contain claims that are
now known to be wrong. They are left intact as a record; these supersede them.

| Claim in the older docs | Measured |
|---|---|
| Embedding is blocked because it would stamp `[tbl-0.html]` into 44% of pages | **False.** 0 of 60,037 blocks contain that string. Tables appear in `blocks[]` as `type="table"` **with** bounding boxes and real content; the placeholder exists only in the page markdown, which `embed_ocr.py` never reads. The genuine defect is smaller — table content is raw HTML, so the text layer picks up `<td>` tags. |
| "0 of 1,762 v2 pages have an archived Genius Scan reading" | Was true; **now resolved** — `genius_scan_v2` covers all 1,762. |
| Gate at >2% yields 245 documents | **256** (the app docstring's 245 is stale) |
| 4,081 blocks, none carrying confidence | **60,037** blocks; block-level `confidence_scores` is null, page/table word scores are populated |
| 782 pages affected by tables | **784** |
| "Repo is uncommitted" | Committed |
| 200 DPI ≈ 400–600 MB | **300 DPI = 1,685 MB measured** |

Also measured: the scans' **native** resolution is median 184 DPI (p10 116,
p90 247, max 328). The 300 DPI render therefore upscales most pages — that was a
deliberate choice for zoom headroom, not an accident.

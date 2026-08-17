# Paper-Oxidizer — what everything does, in plain language

> Audience: Alden and Jeff, and anyone who has to debug this at 1am. After
> reading this you should be able to (1) explain each button to Jeff, (2) predict
> what the system will do next given a state, and (3) trace a bug from a user
> click back to a SQL row.

## Who the people are

- **Alden** (reviewer key `robertadobbins`): owner/operator.
- **Jeff** (reviewer key `jeffsdobbins`): the second pair of eyes. He is the
  one who is doing most of the day-to-day review right now.
- **Worker**: not a person. A Python process (`v2/adjuster/adjuster.py`) running
  in a Docker container (`adjustment-worker` service in `docker-compose.yml`).
  It reads the database on a loop and applies fixes to the OCR text.
- **Build runner / Loop 2**: not built yet. Once Jeff marks a doc "Approve
  Final", a build job should run that embeds the merged text into a PDF/A and
  drops it into Papra. This is the next milestone (M3).

## Where the data lives

```
Neon (system of record)
├── document            1,464 rows — one per output PDF
├── ocr_reading         ~5,900 rows — every reading of every page
│                          (Mistral, human-corrected, adjust:*)
├── page_review       per-page verdict state (submitted/approved)
├── page_image         page → R2 jpeg key + width/height/dpi + pt_to_px
├── job                pipeline queue (kind='adjust' now, 'build' later)
└── output_file       generated PDFs (build_version='recut-v2')

Cloudflare R2 (pixels + HQ sources)
├── pages/<page_id>.jpg     1,762 × 300 DPI JPEGs
└── RAW-GENIUS-V2/*.pdf   1,464 HQ source PDFs

Coolify (VPS, 31.220.58.21)
├── ocr-review       (v1, retired — compose service still in repo for now)
├── ocr-review-v2   (Rust, port 8779, ocr.dobbinscodex.cloud)
└── adjustment-worker (Python, no port — polls Neon every 30s)
```

## What each button does

In `ocr.dobbinscodex.cloud`, the action bar at the top of each document has:

| Button | What it does | When you can press it |
|---|---|---|
| **Save page** | Writes your edits + your note to the database, just for the current page. | Any time. |
| **Submit ▶** | Marks the whole document as `verdict='submitted'` for YOU. Saves first. The worker picks it up on its next poll and applies the per-page remedies based on your notes. | Only when every page of this doc has at least one saved edit. Button shows `(x/y touched)` until then. |
| **✔ Approve Final** | Marks the document `verdict='approved'` for YOU. Saves first. **This is the FINAL check** — your edits AND the worker's merged edits are both reviewed together. | Only when every page is approved (every page-strip tile is green). |
| **↩ Reject** | Opens a popover with required reason + optional note + tag. Sends the doc back with verdict `rejected` for the worker to look at again. | Always. |
| **↩ Unapprove Final** | Undoes your Final — clears verdict + your approved-page rows for THIS doc. Use it if you clicked Approve Final by mistake. | Appears only when YOU are the doc's Final reviewer. |

The **page-strip dots** (the row under the action bar) are the per-page status
display. Each tile carries the page number + the state label + any tags set
on that page. Double-click cycles the page through `touched → submitted →
approved → submitted` so you can mark pages individually.

**Replacing your earlier understanding:** the `v2` / `v3` / `vN` tags you may
have seen on a page tile are NOT user-actionable chips. The worker stamps them
automatically when it returns an adjusted reading (e.g. `adjust:geometry:v2`).
You should never add or remove one yourself. They are the worker's revision log,
the same way a doc has a v1, v2, v3 file-history.

## The two human loops, end to end

### Loop 1 — fix

```
Review app                    Adjustment worker
─────────────                  ────────────────────
reviewer saves edits    →     polls Neon every 30s
reviewer clicks Submit ▶ →    finds doc where
reviewer writes notes         jeffsdobbins.verdict = 'submitted'
                              reads meta.annotation
                              reads the human note rows on every page
                              applies a remedy per page:
                                bad-geometry/reading-order → local
                                  geometry rebuild (no API call, free)
                                needs-reocr/repetition/illigible → targeted
                                  Mistral re-OCR of just that page from R2
                                  RAW-GENIUS-V2/, ~$0.005/page
                                "pervasive" notes → fans out to same-issuer
                                  sibling docs (NULL-guarded, capped 40)
                              writes a NEW ocr_reading row per page
                                method = 'adjust:geometry:vN' or
                                         'adjust:reocr:vN'
                              stamps the document with vN tag (revision log)
                              clears the reviewer's 'submitted' verdict
                                (so the doc returns to the queue)
                              writes a 'job' row so the run is auditable
```

The reviewer can keep editing while this runs because every write is **additive** —
no row is ever mutated. The original Mistral row stays exactly as it was.

If the worker hit a budget or missing API key, it writes the job as
`state='error'` and **leaves the verdict alone**, so the submission survives —
a 1-hour backoff retry is built in.

### Loop 2 — finalise (M3, not yet built)

```
Reviewer clicks ✔ Approve Final →
  - per-page check: every page has an approved verdict
  - queues a 'build' job (the same job table)
  - the future build runner will:
      1. source the HQ PDF for this document from R2 RAW-GENIUS-V2/
      2. merge the corrected text per page into the PDF as an invisible
         positioned layer (via a sidecar using OCRmyPDF + our mistral
         plugin + veraPDF for QC)
      3. write an 'artifact' row (sha256 UNIQUE)
      4. show the built PDF as a FINAL PROOF in the UI for one more confirm
      5. on confirm, upload via Papra API (not the folder):
         POST .../documents → PATCH name/date/notes → POST tags →
         PUT custom properties (incl. Neon doc id + source sha256)
      6. set PIPELINE_DELIVER guard; off until Spike B verifies
```

## The full state diagram

```
                ┌─────────────────────────────────────────────┐
                │           Document (one row in `document`) │
                └─────────────────────────────────────────────┘
                              ▲
                              │  meta->ocr_review.<reviewer>.verdict
                              │
       ┌──────────────────┬────┴────────────────┐
       │                   │                   │
   ┌─────▼─────�   ┌───────▼─────┐   ┌──────▼──────┐
   │ submitted │   │   approved  │   │    hold     │
   │ (in queue)│   │  (final!)   │   │  (do not    │
   └─────�─────┘   └──────┬──────┘   │   embed)    │
         │                │           └─────────────┘
   picked up by     triggers the                ▼
   the adjustment   build runner            stays
   worker every      + a final-proof          in the
   30s, applies      confirm before          queue
   remedies +        Papra delivery           (visible
   writes vN                                      as red)
```

The state is computed across **both reviewers** with hold trumping approved
trumping submitted trumping nothing:

| Document state | Definition |
|---|---|
| `unreviewed` | neither reviewer has set a verdict (or all have `null`) |
| `submitted` | any reviewer submitted; nobody held; nobody approved |
| `approved` | any reviewer approved; nobody held |
| `held` | any reviewer held |

The page-strip is per-reviewer per-page. A page is "approved by you" iff your
own `page_review` row says so. The doc-level Approve Final requires **all pages
approved by at least one reviewer**.

## What the worker is and what it does — the missing doc

`v2/adjuster/adjuster.py`. A Python process. No external state. Every 30
seconds it:

1. **Claim a job** with `SELECT FOR UPDATE SKIP LOCKED` — so two workers
   running at once (or a redeploy mid-run) never double-process the same
   document.
2. **Read the doc**: its `meta.annotation` (Mistral's per-page issuer/
   account/date fields), its `ocr_review` verdict map, and every
   `human-corrected:*` row that exists.
3. **Per page, choose a remedy** from the rules table at the top of
   `adjuster.py`:
   - tags include `bad-geometry` or `reading-order` OR note contains
     matching phrase → rebuild text from block geometry, no API call
   - tags include `needs-reocr` / `repetition` / `illegible` OR note phrase
     matches → fetch that one page from R2 (raw PDF), enhance the raster
     (autocontrast + median denoise, only for illegible/repetition),
     call Mistral OCR with the existing `MISTRAL_API_KEY`, write the
     replacement as a NEW `ocr_reading` row with `method='adjust:reocr:vN'`
4. **Pervasive check**: if the note says "all of these" / "pervasive" /
   "same issue on" → fan the same remedy out to same-issuer+doc-kind
   siblings, capped at 40 (NULL-guarded so a doc with no issuer doesn't
   match every doc with no issuer)
5. **Stale approvals**: drop `page_review` rows for pages whose text
   changed, so an old `approved` doesn't apply to a re-OCR'd page
6. **Tag stamp**: `vN` on the document's `meta.tags` (deduped
   case-insensitively), so the vN log is visible in the list + the page strip
7. **Consume the submission**: clear `meta.ocr_review.<me>.verdict =
   'submitted'` so the doc returns to the queue as unreviewed. Holds and
   finals are NEVER touched.
8. **Outcomes are 3-way**:
   - **changed** → `job.state='done'`, summary lists per-page remedies
   - **operationally skipped** (no API key, budget exhausted, no source PDF)
     → `job.state='error'`, verdict is PRESERVED so a 1-hour-backoff retry
     can pick it up
   - **nothing to do** (edits-only, or no remedy matchable) → `adjust-noop`
     tag + consumed
9. **Worker is idempotent**: `INSERT ... ON CONFLICT (page_id, method) DO
   UPDATE` so a re-run rewrites the row rather than duplicating.

## What the front-end gates are doing — concretely

Three gates in `refreshActionBar`:

```ts
const live     = !allTouched; // Submit ▶ disabled until every page saved
const finalize = !allFinal || pending; // Approve Final disabled until
                                        // every page approved AND no pending
                                        // edits/notes on the open page
const unapprove = !!hereDoc && hereDoc.verdict === 'approved';
                                  // Unapprove visible only when YOU
                                  // marked Final on the open doc
```

`pending` is `editPending || notePending`, computed by comparing the live
textarea text against the last-saved corrected text and the live note textarea
against the last-saved note. The status-bar shows "pending edits/notes" when
true, so the reviewer can see *why* the button is disabled.

`refreshActionBar()` runs from `render()` (after every save / openDoc) and at
boot. The button titles spell out the failure mode ("Save at minimum —
every page must be edited before Submit", "Mark every page as approved before
Finalize (x/y)", "Save edits first — Approve Final skips the worker.").

## Where every byte comes from / goes to

| Event | Front-end → backend | Backend → storage |
|---|---|---|
| Edit a page | `POST /api/save` (id, text, tables, note) | `upsert ocr_reading(page_id, human-corrected:reviewer, ...)` |
| Click Submit ▶ | `POST /api/verdict` (id, 'submitted') | sets `meta.ocr_review.<reviewer>.verdict = 'submitted'`, queues `job(kind='adjust')` |
| Click ✔ Approve Final | `POST /api/verdict` (id, 'approved') | sets verdict + per-page approved rows |
| Click ↩ Unapprove Final | `POST /api/unapprove` (id) | clears verdict + per-reviewer approved rows |
| Click ↩ Reject → confirm | `POST /api/reject` (id, reason, note, tag) | sets `verdict='rejected'` + reason/note/tag |
| Page image render | `GET /page.img?id=<pageId>` → R2 presigned URL | browser fetches R2 directly |
| Worker poll | (none) | every 30s: claim a job, do the remedies, write |
| Tag set/remove | `POST /api/tags` (id, [tags]) | `update document.meta.tags = $1` (case-insensitive dedup) |

## What "vN" really is

`vN` (v2, v3, ...) is the worker's revision number stamped onto a document
when it returns an adjusted reading. It is stored in `meta.tags`. It exists for
auditing — "what does the doc look like at iteration N?" — and the page-strip
shows it via `REVISION = /^v\d+$/` filter so reviewers can tell at a glance
which docs have been adjusted and which haven't. It is NOT a user-actionable
chip and it must NEVER appear in the user-facing tag dropdown (already removed).

## What is still in the pipeline

| Stage | State |
|---|---|
| M1: skeleton | ✅ live on `ocr.dobbinscodex.cloud` |
| M2: parity + page-approval | ✅ live |
| M3: build runner + sidecar + Papra delivery | ❌ not built — `adjustment-worker` only |
| M4: Turso fork | 📋 designed |
| M5: hardening + v1 retirement | 📋 planned |

`v0.2.2` is the deployed version right now. `v0.2.2-hotfix` is on `main`
but the deployed container is showing the previous build until the user
redeploys without cache (the new gate + Unapprove buttons are not live).

## What's broken right now

The deployed container is on commit `9efb40f`'s predecssor (`6daaf9b`)
or earlier — the `bunapp` button, the new page-strip state classes, the
Unapprove endpoint, the per-page-state gating, and the tag filter are all
in the source but not yet in the deployed image. The user must **redeploy
without cache** in Coolify for those changes to land.

## The full state diagram as a flowchart

```
                       ┌─ hold (do not embed, stays red in queue)
                       │
[unreviewed] ───┐       │
   │            │       │
reviewer clicks │       │
  Submit ▶     │       │
   │            │       │
   ▼            │       │
[submitted]──rejected by reviewer ──┐
   │                                  │
poll every 30s                       │
   │                                  │
   ▼                                  │
adjustment-worker picks up ──── applies remedy per page ────┐
   │                                                          │
   │ (free: geometry rebuild)                                   │
   │ (API:  targeted Mistral re-OCR of single page from R2)      │
   │ (fan-out:  same-issuer + doc_kind siblings, cap 40)        │
   │                                                          │
   ▼                                                          │
writes NEW ocr_reading rows                                     │
   method = 'adjust:geometry:vN' or 'adjust:reocr:vN'         │
   stamps meta.tags = [..., 'vN']                              │
   deletes stale page_review rows where text changed          │
   clears reviewer's 'submitted' verdict (returns to queue) ──┘
   job.state = 'done' (or 'error' for skipped; submission preserved)
```

```
[unreviewed] ─── reviewer clicks ✔ Approve Final ─── [approved]
                   ▲                                │
                   │                                │
               Unapprove (clear MY verdict +       │
                MY approved rows for THIS doc)     │
                   │                                ▼
                [unreviewed]            Loop 2 build runner
                                          (FUTURE, M3)
                                          ↓
                                     sidecar (ocrmypdf + veraPDF)
                                          ↓
                                     artifact row (sha256 UNIQUE)
                                          ↓
                                     FINAL PROOF shown in UI
                                          ↓
                                     reviewer confirms
                                          ↓
                                     Papra API upload (M3)
                                          ↓
                                     papra_id on artifact row
```

## What the worker does NOT do (negative space)

- It never mutates an existing Mistral row. New readings = new rows.
- It never overwrites your edits. Human rows are immutable.
- It never deletes a doc's `verdict` if it's not `submitted`. Holds and finals
  survive every run.
- It never deletes a doc's tags. It only adds `vN`.
- It never touches other reviewers' work. `adjust:reocr:v2` is per-revision,
  not per-reviewer.

## Quick reference: every API endpoint

```
POST /login                    set-cookie: rev=<reviewer>|<sig>
GET  /logout                    clear cookie
GET  /whoami                    { reviewer: <name> | 401 }
GET  /healthz                   200 if Postgres reachable
GET  /page.img?id=<pageId>      302 to R2 presigned URL

POST /api/queue                 list of every doc w/ per-reviewer verdict,
                                per-doc tag list, and per-page approval
                                counts
POST /api/doc  (id)             one doc with all pages' text, spans,
                                tables, approvals, notes, src
POST /api/save  (pageId, text,  upsert ocr_reading row for the current
                tables, note)   reviewer
POST /api/verdict (id, verdict)  set this reviewer's verdict
                                verdict in {submitted, approved, hold,
                                rejected, null}
POST /api/tags  (id, tags)      set the doc's tag list (deduped)
POST /api/reject  (id, reason,  reject; required reason, optional note,
                  note, tag)     optional tag
POST /api/unapprove (id)        clear this reviewer's verdict + per-page
                                approved rows (Undo a Final)
POST /api/page_verdict          per-page approve/submit (set status=
                  (pageId,      'approved' | 'submitted' | null)
                  status)
```

## Quick reference: every ocr_reading.method

| Method | Source | Notes |
|---|---|---|
| `mistral-ocr-4-1` | Mistral OCR on the original PDF | the base reading |
| `human-corrected:reviewer` | reviewer's saved edit | per-reviewer |
| `adjust:geometry:vN` | worker geometry rebuild | per-revision |
| `adjust:reocr:vN` | worker targeted re-OCR | per-revision |
| `genius_scan_v2` | original Genius Scan text | archived for reference |

The `/api/doc` and `/api/queue` endpoints return rows in this order
**per page**: newest `adjust:%` first, else `human-corrected:YOU`, else
`mistral-ocr-4-1`. That's the "what does this page's machine text say" rule.

## What I have NOT explained well enough — sorry

This file is the answer to your earlier "your worker, you never explained what it
is or what it does." I should have written it on day one of M3. The diagram
above is the complete picture; the worker is a 30-second poll loop, not a
daemon, and the page-strip tiles show what it's doing.

If anything's unclear, the way to ask is to point at a specific step
("why does Hold trump Approved? what if both are mine?"). The state rules are
deliberately conservative — the doc never finalizes while you could be wrong.

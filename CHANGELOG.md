# Changelog

Written to be readable cold. Each entry says what changed **and why it matters**,
because several of these are things that look like details and are not.

---

## 2026-08-14 — Repo created; review app built and hardened

### Repo set up

- Created `pipeline/ review/ schemas/ docs/` and moved 29 files (565 KB) out of
  the `Document Splitting for Paperless` working folder.
- `.gitignore` excludes all 7.5 GB of data (`recut/`, the v2 sources,
  `ocr-mistral/`, etc.) plus `*.pdf`. **GitHub rejects files over 100 MB and one
  document is 118 MB**, so without this the first push fails.
- `review/` deliberately keeps the **round-one** `review_app.py` and
  `review_app.html` alongside the current app, as UI reference — the older
  layout was better in ways worth re-reading.

⚠ **Two copies of `ocr_review_app.py` now exist.** The repo copy is current; the
one still in `Document Splitting for Paperless` is stale. Run from the repo and
delete the old one.

### Review app — what it does

Serves `http://127.0.0.1:8778`. Reads **everything** from Neon. The only thing
it touches on disk is page images, because Neon stores no pixels.

Four panes: **scan · Mistral text with suspect words marked · your editable
copy · diff**. Notes per page. Three verdicts. Two reviewers.

```
uv run ocr_review_app.py
```

Set `PAGE_SOURCE` to wherever the scans are (it used to assume they sat beside
the script — moving the file into the repo would have shown **blank images with
no error**). Set `REVIEWER` to who is working.

### The gate — how documents get chosen for review

The original plan gated on **minimum word confidence at 0.90**. Measured, that
flags **1,330 of 1,464 documents (91%)** — useless, because every scan has one
bad word somewhere (a smudge, a logo, a signature).

Replaced with **percentage of words below 0.60 confidence**:

```
359,189 words · 4,913 below 0.60 = 1.37% overall
670 documents (46%) have zero bad words
gate at >2%  ->  256 documents to review
```

Three refinements, each from a real false positive:

1. **Union, not blended.** Table words are 18% of the corpus and OCR *cleaner*
   than prose. Averaging them in **hid 35 documents** whose prose was bad but
   whose clean tables dragged the score down. Now flags if either measure trips.
2. **Minimum 20 words.** A page with one scored word, and that word bad, is
   "100% bad" and sorted above genuinely broken documents. Those 6 documents are
   marked *thin* and sorted last — never dropped.
3. **Repetition** (below).

### Three OCR failure modes — only one is visible to confidence

This is the most important thing in this file.

1. **Garbled words.** Confidence catches these. This is what the gate was
   designed for.
2. **Repetition loops.** One document repeats a table row **35 times at 0.0%
   suspect words**. The model is genuinely, correctly confident about every
   token — it just emits the same ones over and over. **Per-token certainty is
   structurally blind to structural failure.** Needed its own detector.
3. **Scrambled reading order.** Form-layout documents get flattened from 2-D to
   1-D in the wrong order. Every word is read correctly and scored high; only
   the *order* is wrong. Also invisible to confidence.

Example of #3 — a gardener's invoice came out as:
`SPRAY / GARDENER / 1492 / San / Hata Rodriguez / Bahama / Jose / TOTAL`.
Those middle lines are **the gardener's name and address**, not junk. Sorting
the page's blocks by their bounding boxes `(y, x)` reconstructs the true form
exactly. **The geometry was never wrong — only the linearisation.** A naive
"strip the extra lines" fix would have erased a real name and address off a
document that evidences a paid estate expense.

### Repetition detector — added, then retuned twice

Added at "3+ identical rows anywhere" → flagged 54 documents. Alden's review
notes said most were fine. Retuned on measurement:

- **Blank rows now ignored.** An invoice with printed ruled lines produces
  identical *empty* rows. **17 of the 54 had no repeated content at all.**
- **Consecutive runs, not total count.** A receipt can legitimately list the
  same item several times, scattered. A loop emits them back to back.

Result: **54 → 10 flagged**, every confirmed loop retained (runs of 20, 12, 10,
10). Threshold is `MAX_REPEAT` at the top of the file.

### Tables were invisible — 44% of pages affected

`table_format="html"` makes Mistral put **only `[tbl-0.html]`** in the page text;
the real content lives in a sibling array. **782 of 1,762 pages, 79,542 words =
18% of the whole corpus.** A receipt's entire line-item list was hidden behind
that placeholder and could not be reviewed or corrected.

Tables now render **inline where the placeholder sits**, with editable cells.

⚠ **This still blocks embedding.** `embed_ocr.py` places text from blocks only,
so as written it would stamp the literal string `[tbl-0.html]` into 44% of pages
— those line items would not be searchable. **Must be fixed before any embed
run.**

### Two-reviewer support

Corrections are stored **per reviewer** so Alden and Jeff never overwrite each
other, and so the record shows who changed what.

**Discovered the hard way:** `ocr_reading` has a UNIQUE constraint on
`(page_id, method)` — `ocr_reading_page_id_method_key`. A shared
`human-corrected` method allows exactly **one** correction per page for
everyone; the second reviewer's save dies on a constraint violation. The
reviewer is now encoded in the method (`human-corrected:alden`), which turns the
constraint into precisely the right rule: one correction per page per reviewer,
enforced by the database.

Migrated 24 existing corrections and 25 verdicts to the new shape. Nothing lost.

### Verdicts — two states were not enough

`Approve` used to mean both *"I looked at it"* and *"it's correct"*. A page noted
as unreadable would still be approved and embedded.

- **Approve** — reviewed, correct, safe to embed
- **⏸ Hold** — reviewed and **not** safe to embed
- *(none)* — not reviewed

Both save first. The button relabels itself to **"Save + Approve"** when edits
are pending, so it is visible rather than inferable that approving keeps your
work.

**The embed step must select on `verdict = 'approved'`, never on "has been
opened."**

### Other fixes

- **White screen when clicking any document.** The function was named `open()`,
  and inline handlers resolve names against `document` before `window` — so it
  called `document.open()`, which blanks the page. Renamed.
- **Zoom** on the scan: wheel to zoom at cursor, drag to pan, double-click to
  fit. Render raised 140 → 200 DPI so zooming reveals detail rather than blur.
- **Notes** per page, saved to Neon. A note alone is enough to write the row, so
  a problem can be flagged without being fixed.
- **Collapsible** document list and diff (`[` and `]`), preference persists.
- **Removed the Genius Scan comparison** entirely — Alden: *"Mistral is so good
  we don't need to diff it."* It had never worked anyway (see below).

---

## Findings recorded 2026-08-14 (measured, not assumed)

| Finding | Why it matters |
|---|---|
| **`recut/` is 100% v2 imagery** — a page proved byte-identical to its v2 source | The `0294__…` names are v1 *document boundaries*; the pixels are all v2. The single high-quality source of truth already exists. |
| **0 of 1,762 v2 pages have a `genius_scan_v1` reading in Neon** — all 2,327 legacy readings sit on `v1-merged` files | The text embedded in the recut PDFs **exists nowhere else.** `strip_text()` would destroy it permanently. Also explains why the Genius Scan diff never showed anything. |
| **Page render sizes** — 150 DPI q80 = 543 MB · **200 DPI q85 = 1,001 MB** · PNG = 10.7 GB · source PDFs = 3,164 MB | An earlier estimate of "400–600 MB" was wrong by 2×. Real numbers for the R2 upload decision. |
| **Annotation fields are 100% populated** — `doc_kind`, `issuer`, `doc_date` across 1,507 sub-documents | The canonical renaming scheme is feasible. Fields are **nested** at `meta.annotation.documents[]`, and `issuer` is sometimes an object. |
| **1,507 sub-documents from 1,464 files** | ~43 files contain more than one document. Nothing may assume 1:1. |

---

## Requirements added 2026-08-14, not yet built

1. **HQ source-of-truth folder** — clearly marked, holding the v2 correctly-split
   documents. 300 DPI JPEG floor if compression is ever needed.
2. **Canonical renaming** —
   `<doc_kind>__<doc_date>__<docid>__<issuer-slug>.pdf`, with `-lofi.jpg` for the
   display tier. Type first, vendor last. **Old and new names both logged to
   Neon.**
3. **Extract before strip** — pull each page's existing text layer into Neon
   *before* removing it. Archival insurance on an irreversible delete; nothing
   consumes it.

---

## Current state

- **256 documents queued.** 22 approved, 3 held, 21 notes — all in Neon,
  attributed, independent of any chat session.
- **Embedding is blocked** on the tables fix.
- One leftover smoke-test row: `human-corrected:jeff` on
  `2026-07-13 14-26 1.pdf`. Delete it.
- Repo is **uncommitted**.

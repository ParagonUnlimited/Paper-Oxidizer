# HANDOFF — Fork 3: Paper-Oxidizer (pipeline + review app)

**Repo:** `Paper-Oxidizer`
**Blocked by:** nothing. Start immediately.

## The goal, verbatim — everything else is support

> "all the documents prepared for ingestion into paperless with nearly perfect
> OCR already embedded."

## Why this is not blocked by the database decision

The quality work — geometry reading-order rebuild, image enhancement, table
flattening, embedding — is **database-independent**. None of it cares whether
results land in Postgres or libSQL; only the *write target* would change. Do not
wait on Fork 2.

## Current state

- **1,464 documents / 1,762 pages** OCR'd with `mistral-ocr-4-1` (~$8.88),
  fully loaded into Neon. `ocr_reading` rows: mistral 1762, vision_v1 791,
  tesseract_v1 791, genius_scan_v1 745, `human-corrected:alden` 24.
- **`recut/` is 100% v2 imagery** — 3.09 GB, 1,464 files, 1,311 single-page,
  max 47 pages. The `0294__…` names are v1 *document boundaries*; every pixel is
  v2. Verified byte-identical to the v2 sources.
- **Review app works** (`ocr_review_app.py`, 127.0.0.1:8778): four panes, zoom,
  per-page notes, three verdict states, per-reviewer attribution.
- **Queue: 256 documents.** 25 reviewed so far, 21 notes written.

## THREE OCR FAILURE MODES — only one is visible to word confidence

1. **Garbled words.** Caught by confidence. Gate: % of words below 0.60, >2%.
   (The approved plan's minimum-confidence @0.90 gate is USELESS — it flags 91%
   of the corpus, because every scan has one bad word.)
2. **Repetition loops.** One document repeats a table row **35 times at 0.0%
   suspect words** — completely invisible to confidence. Needs its own detector.
3. **Scrambled reading order.** Form-layout documents get flattened 2-D → 1-D in
   the wrong order. Every word is read correctly and scored high; only the
   *order* is wrong. Confidence is structurally blind to it.

## ⚠ BLOCKING: tables are not in the markdown

`table_format="html"` makes Mistral emit only `[tbl-N.html]` in
`ocr_reading.text`; the real content is `blocks->'tables'[]` with its own
`word_confidence_scores`. **782 of 1,762 pages (44%), 79,542 words = 18% of the
corpus.**

`embed_ocr.py` places text from `blocks[]` only, so **as written it would embed
the literal string `[tbl-0.html]` on 44% of pages.** Receipts' line items would
not be searchable. **No embed run until this is fixed and verified.**

## First task — measure before building

**How many pages corpus-wide have a reading order that disagrees with their
block geometry?** That number sizes the biggest available quality win. Proven on
`recut:2026-06-12 14-34:p4`: sorting blocks by (y, x) reconstructs the true form
exactly, recovering the gardener's name and address that the markdown had
scattered into the invoice body.

Do this before writing the rebuilder — it may be 15 pages or 400.

## Work items

1. **Geometry reading-order rebuild.** Regenerate page text from `blocks` sorted
   by (y, x), column-aware. Local, free, no re-OCR. Write as a NEW `ocr_reading`
   method — never mutate the Mistral rows.
2. **Table flattening into the embed** (blocking, above).
3. **Image enhancement + targeted re-OCR** for faded/overexposed/illegible
   pages: CLAHE adaptive contrast, then Sauvola thresholding (built for uneven
   illumination, where a global threshold fails). Re-OCR only affected pages,
   cents. Do this AFTER the geometry fix — some "illegible" may be ordering.
4. **Repetition gate: raise the run threshold.** Currently `MAX_REPEAT = 4` →
   10 docs. Alden's notes say six of those were correct ("repeating lines are
   correct", "these are line-items, identical rows are fine"). Raise to 6+
   (≈4 docs) and re-check against the notes.
5. **Deploy the app for Jeff** — pre-render pages to JPEG @200 DPI → R2
   (~400–600 MB; do NOT upload the 3.09 GB of PDFs, one is 118 MB), stdlib
   login (not Cloudflare Access) that sets `REVIEWER`, containerize, Coolify.
6. **Housekeeping:** delete the leftover test row — `human-corrected:jeff` on
   `2026-07-13 14-26 1.pdf` ("jeff's note here").

## Hard-won facts — do not rediscover these

- **`ocr_reading` has UNIQUE (page_id, method)** (`ocr_reading_page_id_method_key`).
  A shared `human-corrected` method allows exactly ONE correction per page for
  everyone. The reviewer is therefore encoded in the method:
  `human-corrected:alden`. The constraint then enforces exactly the right rule.
- **Word confidence carries `start_index`** — a character offset into the page
  markdown, giving exact highlighting. **Blocks carry ZERO confidence** (0 of
  4,081) and block text is findable in the markdown only 76.6% of the time —
  which is why corrections are page-level, not block-level.
- **Base-14 PDF fonts are latin-1.** Unmapped characters silently became `?` and
  cost a full 512-document rebuild once. `embed_ocr.py` has the FOLD map and
  `latin1_safe()`; keep them.
- **Alden's notes are in Neon** (`ocr_reading.meta.note`) and are the best source
  of truth about what is actually wrong. Read them before retuning any gate.
- **Never read `ocr-mistral/*.json` or other local files** to answer something
  the database can answer. Neon is the source of truth; the only exception is
  page IMAGES, because Neon stores no pixels.

## Verification

- Geometry rebuild: sample 20 pages, diff against originals, eyeball against the
  scan **before** any bulk write. Hector p4 must show `1492 Bahama … San Jose CA
  95122` intact and correctly ordered.
- Embed: extract text from 10 embedded PDFs — table line items present, zero
  literal `[tbl-` strings, no stray `?`.
- **Corpus gate: nothing embeds unless
  `meta.ocr_review.<reviewer>.verdict = 'approved'`** — never "has been opened".

Working model: **propose, wait for GO. Never self-authorise.**

# Document Splitting for Paperless — plan

## Why

Seven documents in Paperless are **containers**, not documents: stacks of unrelated
statements scanned into one file. Between them they hold **791 pages** of what is probably
150–250 separate documents. While they stay merged:

- **Retrieval is broken.** One embedding vector per 411-page blob matches everything and
  means nothing. A query for "Bellerose water bill November 2021" cannot land on a page.
- **Dates are meaningless.** A container gets one `created` date for ~73 statements.
- **Metadata is meaningless.** One correspondent, one type, for dozens of issuers.

Everything downstream — AI suggestions, the LLM index, supermemory, probate accounting —
is built on documents being documents. This is the blocker.

## Why not a script

Boundary detection by pattern-matching was considered and rejected. The existing OCR is too
degraded to trust for structural inference:

```
"Biling Period:"   "San Joso Watsr Campany"   "236 Bollerose Dr"   "81LL3NG tNEORNATION"
"Meter Reading / Previous / 1441 / Servlco Address:"   <- columns interleaved
```

Tesseract mangles words *and* reading order. Page numbers are absent from at least half the
pages. A regex over this produces confident nonsense — the same failure mode that put 130
medical documents in a `property` folder because `lease` matched "p**lease**".

**Method instead: read every page with vision.** Transcribe it, compare against the existing
Tesseract text, and identify the page from content and context — issuer, statement period,
account number, page-of-N markers, layout, physical scan artifacts.

## ⚠ The disorder problem

**Pages are not necessarily in order.** Three distinct failure modes, which combine:

| Type | Shape | Example |
|---|---|---|
| **A — shuffled** | One document's pages out of sequence | pages 3, 1, 2 of a single statement |
| **B — interleaved** | A second document sits inside a first | doc X pp.1–2, doc Y pp.1–4, doc X pp.3–4 |
| **C — both** | Shuffled *and* interleaved | any combination of the above |

Also expected, and handled the same way: **duplicate scans** of the same page, **blank pages**
and **page backs**, and pages scanned **rotated**.

### What this breaks

The obvious design — walk the pages in order, mark each `START` or `CONTINUATION`, cut at the
STARTs — is **unusable**. That vocabulary describes a page's relationship to the *preceding*
page, which is only meaningful if order is trustworthy. It cannot express "this page belongs
with page 12, three hundred pages back."

It also breaks the chunking strategy. A 45-page reading window **physically cannot detect**
that one of its pages belongs to a document whose other pages are outside the window. No
amount of overlap fixes that; the related page may be anywhere in the container.

### The fix — fingerprint, then assemble

**Agents no longer decide boundaries.** They do one job: read a page and record *what it is*,
richly enough that pages of the same document can be recognised as siblings from anywhere in
the container. A separate global pass then groups and orders them.

```
Phase 1 (parallel, chunked)    791 pages        -> 791 page fingerprints
Phase 2 (global, per container) fingerprints    -> grouped, ordered documents
Phase 3 (human)                 review the proposed grouping and ordering
Phase 4 (local, scripted)       build.py -> split PDFs, verify.py -> review sheets
```

**All four phases run locally on this machine.** The source of truth is `raw/`; the output is
`splits/`. Paperless is not involved at any step — what to do with the finished PDFs is a
separate decision, made after they exist and have been reviewed.

This is strictly more capable than the boundary model — a contiguous, in-order document is
just the trivial case where the fingerprints happen to group into an ascending run.

## Prepared (done)

```
Document Splitting for Paperless/
├── raw/          7 original PDFs      — the source; what actually gets cut
├── archive/      7 archive PDFs       — carry the Tesseract text layer, reference only
├── pages/<id>/   791 page images      — 180 DPI JPEG, ~1530px wide
├── ocr/          <id>-tesseract.md    — existing OCR, split per page
├── ocr/          <id>-vision-*.md     — OUTPUT: the vision reading, per page
├── boundaries/   <id>.json            — OUTPUT: the reviewed cut manifest
├── splits/<id>/  NNN__label.pdf       — OUTPUT: the finished documents
├── prep.py       renders page images + extracts per-page Tesseract text
├── build.py      manifest -> split PDFs          (PyMuPDF, local, lossless)
├── verify.py     split PDFs -> _review.pdf       (contact sheets for review)
└── manifest.json
```

Images are rendered at ~1530px wide deliberately — that is Claude's image ceiling, so it is
maximum legibility with zero wasted tokens.

| ID | Pages | Filename says | Text layer | Images |
|---|---|---|---|---|
| 013 | 24 | San Jose Water | 24/24 | 6.4 MB |
| 015 | 35 | W.E. O'Neil | 35/35 | 6.2 MB |
| 077 | 20 | GreenWaste | **0/20** | 38.5 MB |
| 078 | 23 | *(unlabelled)* | **0/23** | 22.6 MB |
| 014 | 80 | Santa Clara DTAC | 78/80 | 19.2 MB |
| 017 | 198 | Pacific Power | 197/198 | 58.3 MB |
| 018 | 411 | STCU | 411/411 | 108.9 MB |

⚠ **"Filename says" is not "container contains."** Verified on 013: it is named
`san-jose-water-24pp`, but only **pages 1–2** are San Jose Water. The other 22 pages are FTB
notices, IRS notices (including the CP575B EIN letter), Klamath County property tax, Santa
Clara Assessor, a CBE Group collections letter, the **American Express creditor's claim
against the estate**, a **Santa Clara probate court filing**, and DIRECTV statements — at
least ten unrelated issuers. Treat every container name as a guess. Expect the same in all
seven, and never let the name bias a page's identification.

⚠ **077 and 078 have no text layer in the original.** For those two, the vision reading is the
only accurate text that will ever exist. They do still have a Tesseract cross-reference —
Paperless OCR'd them on ingest, so `ocr/077-tesseract.md` and `078-tesseract.md` are populated
from the *archive* copies.

~~077/078 are phone photographs.~~ **Wrong — corrected 2026-08-03.** I inferred this from the
rendered JPEGs averaging ~1.9 MB/page against ~0.26 MB elsewhere. That was a measurement of my
own 180-DPI renders, not of the source. Checked the embedded images directly: all containers
are 3-channel colour JPEGs at comparable resolution, and 077's first page is **286 KB against
013's 378 KB** — smaller. Both agents assigned to these containers independently reported clean,
flat, evenly-lit reproductions and explicitly declined to force-fit my framing. They were right.
Larger renders meant more colour and detail, nothing more.

---

# Phase 1 — fingerprint every page

Three things per page. **Identity** (which document is this?), **position** (where in that
document?), **transcription** (what does it say?).

## 1. Identity fingerprint

The tuple that lets a page be matched to its siblings from anywhere in the container:

| Field | Notes |
|---|---|
| `issuer` | Letterhead, logo, return address. Verbatim as printed. |
| `account` | Account / member / customer / parcel / invoice number. **The single strongest signal** — copy it digit for digit. |
| `doc_date` | The date printed on the page |
| `period` | Statement / service / billing period, if shown |
| `doc_kind` | bill · statement · remittance · notice · letter · receipt · check · envelope |
| `total` | Amount due / balance / total, if shown |

## 2. Position within its document

| Field | Notes |
|---|---|
| `page_marker` | Verbatim, e.g. `"Page 3 of 5"`, `"3/5"`, `"- 2 -"`. **null if absent — never infer one.** |
| `opens_doc` | true if the page carries letterhead / "Page 1 of N" / an opening address block |
| `closes_doc` | true if the page carries a signature block, "end of statement", or back-matter terms |
| `continues_text` | A sentence or table row visibly cut off at the **top** — quote the first few words. This is what links a page to its predecessor. |
| `continues_into` | Same, cut off at the **bottom** — quote the last few words. |
| `running_balance` | Opening and closing balance on the page, if any. Orders transaction pages when nothing else can. |

## 3. Physical traits — for the hard cases

When two documents from the same issuer with the same account interleave, content alone may
not separate them. Physical evidence does:

- paper colour and tone, print quality, form revision or version code in the footer
- scan skew direction and angle, page edges, shadows, fingers, background surface
- staple holes, punch holes, folds, handwriting, stamps, highlighter
- resolution or sharpness differences, indicating a different scan session

## 4. Anomaly flags

`blank` · `duplicate_of` (page number, if this looks like a rescan of another page) ·
`rotated` (90 / 180 / 270 — clockwise correction needed) · `unreadable` ·
`not_a_document` (separator sheet, envelope, blank back)

## 5. Transcription and divergence

- **Vision transcription** — what the page actually says. This becomes the corrected OCR.
- **Tesseract divergence** — material differences against `ocr/<id>-tesseract.md`.
  Where they disagree, **vision wins**, and the divergence is recorded.

## Output format

`ocr/<id>-vision-<range>.md`, one section per page:

```markdown
## page 47

### identity
- **issuer:** STCU
- **account:** 556253
- **doc_date:** 2021-07-08
- **period:** 2021-06-01 – 2021-06-30
- **doc_kind:** statement
- **total:** $1,204.55

### position
- **page_marker:** "Page 2 of 4"
- **opens_doc:** false
- **closes_doc:** false
- **continues_text:** "...ELECTRONIC DEPOSIT PAYROLL WE ONEIL" (row cut at top)
- **continues_into:** "CHECK 1042 ..." (row cut at bottom)
- **running_balance:** opens 3,880.12 / closes 2,675.57

### physical
- cream paper, slight left skew ~2°, staple hole top-left, no handwriting

### flags
- none

### confidence
- identity: high · position: high

### vision transcription
<full text as read>

### tesseract divergence
<notable errors, or "consistent">
```

**No `PROPOSED:` footer.** Chunk agents do not propose documents — they cannot see enough to
do it correctly. Grouping is Phase 2's job.

## Chunking

411 pages cannot fit one context, so reading is split into ~45-page chunks.

Chunks are now purely a **reading-throughput device**, not a unit of decision. Because
grouping is global, a chunk boundary can no longer split a document by accident — which is
the risk the 3-page overlap originally existed to mitigate.

**Keep the 3-page overlap anyway,** for a different reason: overlapping pages get
fingerprinted twice, by different agents, giving a free consistency check. If two agents read
the same page's account number differently, that is a signal to distrust that field across the
whole run.

| # | Doc | Pages | Agents |
|---|---|---|---|
| 1 | 013 | 1–24 | 1 |
| 2 | 015 | 1–35 | 1 |
| 3 | 077 | 1–20 | 1 |
| 4 | 078 | 1–23 | 1 |
| 5–6 | 014 | 1–43, 41–80 | 2 |
| 7–10 | 017 | 1–53, 50–103, 100–153, 150–198 | 4 |
| 11–19 | 018 | 1–48, 45–95, 92–142, 139–189, 186–236, 233–283, 280–330, 327–377, 374–411 | 9 |

**19 agent tasks.** Small documents first — they validate the fingerprint schema before 411
pages are committed to it.

---

# Phase 2 — assembly

## ⚠ Assembly is GLOBAL, not per-container — corrected 2026-08-03

The pilot disproved the original design. **Documents span containers.** Measured across the
first 102 pages:

| Evidence | Where |
|---|---|
| Klamath County acct **310773** | 013 p6, p8 **and** 015 pp24–31 |
| GreenWaste acct **248610** | 015 pp32–35 **and** 077 pp1–4 |
| DIRECTV acct **7357093** | 013 pp22–24 **and** 078 p23 |
| Amex creditor's claim, form DE-172 | 013 pp19–21 **and** 078 pp12–14 |
| 12475 Foothill Ave | 015 p32 **and** 077 pp1–4 |

The sharpest case: 013 holds DIRECTV **"page 1 of 4"** and **"page 3 of 4"** for account
7357093, and pages 2 and 4 are nowhere in 013 — while 078 holds a **"PAGE 1 OF 4"** for the
same account. A per-container pass would emit two crippled fragments and never notice.

So the container boundary is **just another arbitrary division**, exactly like page order. It
carries no authority. Assembly runs once, over all 791 fingerprints from all seven containers
at once, and an output document may draw pages from more than one source PDF.

### Consequence for `build.py`

A manifest whose pages come from several containers cannot be built by the current script,
which opens exactly one source PDF. **`documents[].pages` entries must become
`{"src": "013", "page": 20}` rather than a bare integer** before any cross-container document
is built. Single-container manifests are unaffected. This change is pending — do not write
cross-container manifests until it lands.

### ⚠ A missing page may be anywhere — including outside these seven files

Global-across-containers is still too narrow. When a document declares `"Page 1 of 4"` and we
hold only pages 1 and 3, the absent pages may be:

1. in another of the seven containers — assembly finds these
2. **already in the corpus as their own separate document** — assembly cannot see these
3. **never scanned at all** — a genuine gap in the record

Assembly can only distinguish (1). So it must not silently treat 2 and 3 as "not our problem",
and must never quietly emit a fragment as if it were whole.

**Every incomplete document emits a `wanted` record** carrying enough detail to search the
wider corpus later:

```json
"wanted": [
  {"issuer": "DIRECTV", "account": "7357093", "doc_date": "2022-11-14",
   "missing": ["page 2 of 4", "page 4 of 4"],
   "held": [{"src": "013", "page": 23, "marker": "PAGE 1 OF 4"},
            {"src": "013", "page": 24, "marker": "PAGE 3 OF 4"}],
   "searched": ["containers"], "status": "open"}
]
```

`searched` records where we have actually looked, so nobody re-derives it. Resolving these
against the rest of the corpus is a **separate later pass**, not part of this job — but the
wanted list is the deliverable that makes it possible, and it feeds the gaps register directly.

Known already from the pilot: DIRECTV pages 2 and 4 (acct 7357093), a Chase Jan-2024 statement
(balance gap between Dec 2023 and Feb 2024), Amex form DE-172 page one, Santa Clara Assessor
page two.

**Corollary for grouping:** never assume a document is complete just because its pages are
consecutive, and never pad a group to make a page count work. An incomplete document, correctly
identified as incomplete, is the right answer.

### Making 791 fingerprints fit one context

The full vision files run ~2.5 KB per page (transcriptions dominate), so all seven would be
~2 MB — far too much. `index.py` strips each page down to its identity, position and flag
fields only, roughly 300 bytes per page, giving a single ~240 KB index that assembly reads in
one pass. The transcriptions stay on disk and are consulted only for pages the index cannot
resolve.

## Reading order

One pass over the global index. Fingerprints are compact enough that all 791 fit at once.

## Step 1 — group

Pages sharing `(issuer, account, period)` are one document. Falling back, in order, when a
field is missing:

1. `(issuer, account, doc_date)`
2. `(issuer, account)` + physical traits + contiguity
3. `(issuer, period)` where no account is printed
4. physical traits alone, for pages with almost no text

⚠ **Same issuer + same account + adjacent periods is the trap.** Twelve monthly STCU
statements share issuer and account and differ only by period. If the period is unreadable on
a continuation page, attach it using `continues_text` / `running_balance` — never to the
nearest month by default.

## Step 2 — order within each group

Strict precedence. Use the highest-ranked evidence available, and record which was used:

1. **`page_marker`** — explicit "Page 2 of 5". Authoritative when present on all pages.
2. **Text continuation** — one page's `continues_into` matching another's `continues_text`.
   Chains pages exactly, and works with no page numbers at all.
3. **Running balance** — closing balance of one page equals opening of the next.
4. **Internal chronology** — transaction dates ascending within a statement.
5. **Structural role** — `opens_doc` first, `closes_doc` last.
6. **Original page order** — the fallback. **Preserve it whenever nothing above contradicts
   it.** Disorder must be *demonstrated*, never assumed.

If a group cannot be confidently ordered, keep original order and flag it. A correctly-grouped
document with uncertain internal order is still an enormous improvement on a 411-page blob.

## Step 3 — emit the manifest

`boundaries/<id>.json` — directly executable against the API:

```json
{
  "container_doc_id": 18,
  "container_pages": 411,
  "documents": [
    {
      "doc": 0,
      "label": "STCU statement 2021-06 acct 556253",
      "pages": [12, 13, 200, 14],
      "order_evidence": "page_marker",
      "confidence": "high",
      "notes": "p200 is page 3 of 4, displaced; marker explicit"
    }
  ],
  "discarded": [
    {"page": 77, "reason": "blank back"},
    {"page": 154, "reason": "duplicate of p153"}
  ],
  "rotations": [{"page": 88, "rotate": 270}],
  "unresolved": [
    {"page": 301, "reason": "no issuer legible, no account, torn corner"}
  ]
}
```

**Every one of the container's pages must appear exactly once** across `documents`,
`discarded`, and `unresolved`. That total-coverage check is assembly's own proof that it did
not silently lose a page.

---

# Phase 3 — human review

**The checkpoint, not optional.** Reordering and dropping pages is more consequential than
cutting between them, so review has more to look at than before:

- every `documents[].pages` list that is **not ascending** — these are the reorder claims
- every entry in `discarded` — a wrongly-dropped page is the one genuinely lossy outcome
- everything in `unresolved`
- any group with `confidence` below high
- any group whose `order_evidence` is `original_order` in a container that shows displacement
  elsewhere

Everything else — clean, ascending, high-confidence groups — can be approved in bulk.

---

# Phase 4 — cut the PDFs, locally

**Nothing in this phase touches Paperless.** The whole job runs on this machine against the
files in `raw/`. Paperless is not the tool, not the intermediary, and not consulted. What
comes out is a folder of finished PDFs. Getting them *into* anything is a separate decision,
made later, by you.

## The tool: PyMuPDF

`PyMuPDF 1.28.0` (MuPDF 1.29.0) on Python 3.14.5 — **already installed, already used here**:
`prep.py` rendered all 791 page images with it. No new dependency, nothing to set up.

Why this one over the alternatives:

| | |
|---|---|
| **PyMuPDF** ✅ | Already present and proven in this workspace. Copies pages, reorders, rotates, *and* rasterises for the review sheets — one library covers build and verify. |
| pikepdf / qpdf | Equally good at lossless structure, but cannot render, so review sheets would need a second dependency. Fine as a fallback. |
| pypdf | Pure Python, slower on a 411-page file, and weaker at preserving unusual scanner-generated PDF structures. |
| Ghostscript / ImageMagick | **Wrong tool — both re-encode.** They would degrade the scans. Avoid entirely. |

**The copy is lossless.** `insert_pdf` transfers the existing page objects; the image data is
never decoded or re-encoded. Rotation only writes the page's `/Rotate` attribute — pixels are
untouched. Verified: 22/22 test pages came out byte-identical in extracted text to their
source pages.

## `build.py` — manifest to PDFs

```bash
python build.py 013 --dry
```
```bash
python build.py 013
```
```bash
python build.py
```

Reads `raw/<id>-*.pdf` and `boundaries/<id>.json`; writes:

```
splits/013/
├── 000__SJW bill 2021-03 acct 12345.pdf
├── 001__SJW bill 2021-04 acct 12345.pdf     <- pages 3, 20, 4 in that order
├── _discarded/p023.pdf                       blanks + duplicates, kept for audit
├── _unresolved/p024.pdf                      pages nothing could place
└── _built.json                               what was produced
```

**It refuses to write unless the manifest accounts for every page exactly once.** Dropping a
page is the only lossy thing this process can do, so doing it by accident is made impossible.
Tested: a manifest missing p20 and double-claiming p5 was rejected with both errors named and
a non-zero exit, before anything was written.

Discarded and unresolved pages are **written out as single-page PDFs, not deleted.** A discard
should be auditable, not vanished.

## `verify.py` — contact sheets for review

```bash
python verify.py 013
```

Writes `splits/013/_review.pdf`: one sheet per output document, every page as a thumbnail **in
final order**, each labelled with the original page number it came from —

```
seq 1  <- orig p3      seq 2  <- orig p20      seq 3  <- orig p4
```

Reordered documents get a red banner showing what the original order was and which evidence
justified the change. That is the whole point: you are checking that 3 → 20 → 4 genuinely
reads in sequence. It also prints a review queue naming reordered documents, low-confidence
documents, and everything discarded.

## The loop, per container

```
1.  agents fingerprint pages    ->  ocr/<id>-vision-*.md
2.  assembly pass               ->  boundaries/<id>.json
3.  python build.py <id> --dry  ->  coverage check, reorder summary, writes nothing
4.  python build.py <id>        ->  splits/<id>/*.pdf
5.  python verify.py <id>       ->  splits/<id>/_review.pdf
6.  you read _review.pdf        ->  approve, or correct the manifest and rerun from 3
```

Steps 3–6 are cheap and fully repeatable — `build.py` overwrites its own output, and the
originals in `raw/` are never modified, only read. Iterate on the manifest as many times as it
takes.

## ⚠ The output PDFs will have a poor text layer

Worth deciding before we generate 200 files. The originals carry Genius Scan's text layer
(013/015/018 on every page; 077/078 on **none**), and a lossless page copy inherits exactly
that — including the mangled `"San Joso Watsr Campany"` reading-order damage. `build.py`
neither improves nor destroys it.

So the split PDFs are searchable only as badly as the originals were, and 077/078 not at all.
Three options, none of which block starting:

1. **Sidecar only** *(default, and what you asked for)* — the vision transcriptions live in
   `ocr/*.md` next to the PDFs. Accurate text exists, just not inside the PDF.
2. **Embed the vision text** as an invisible layer. Makes the PDFs searchable, but without
   per-word coordinates the text is positioned as a block — searchable, not highlight-accurate.
3. **Local OCRmyPDF pass** over `splits/` afterwards. Proper positioned text layer. New
   dependency, and its Tesseract is the same engine that produced the mush we are working
   around — though on deskewed single-document files it would do better than it did on the
   containers.

Defaulting to **1** unless you say otherwise. It is reversible: 2 and 3 can both be applied to
the finished PDFs later without recutting anything.

---

## Ground rules for agents

- **Read only the pages you were assigned.** Never infer the content of a page you did not see.
- **Never guess to be tidy.** `unreadable`, `null`, and low confidence are valid, useful
  answers. A flagged uncertainty costs a minute of review; a confident wrong answer corrupts a
  document.
- **Copy account numbers digit for digit.** This is the primary grouping key. If a digit is
  ambiguous, say which one and why — never silently pick the likely value.
- **Never infer a `page_marker`.** If the page does not print one, it is `null`. Do not derive
  it from position in the file — position is exactly what is in question.
- **Transcribe what is *there*** — crossed-out text, handwriting, stamps, marginalia.
  Handwritten annotations matter in a probate corpus.
- **Record physical traits even when the text is clean.** They are what separates two
  same-issuer, same-account documents that interleave.
- **Note blank pages, page backs, and duplicate scans explicitly** rather than skipping them.
  Phase 2 needs every page accounted for.
- **Vision wins over Tesseract**, and the divergence is recorded.

## Ground rules for assembly

- **Original order is the default.** Reordering requires positive evidence, cited per document
  in `order_evidence`. Absence of evidence is not evidence of disorder.
- **Total coverage or it's a bug.** Every page exactly once across `documents`, `discarded`,
  `unresolved`.
- **Prefer over-splitting to under-splitting.** Two halves of one statement are easy to merge
  later; two statements fused into one is the failure we are already trying to undo.
- **Never discard a page carrying any content.** Blank and duplicate only. When unsure, keep it
  and flag it.

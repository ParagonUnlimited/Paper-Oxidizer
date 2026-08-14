"""Build the review sheet Alden fills in, and a README explaining how.

    python make_decisions_sheet.py

Writes splits/_review-decisions.csv  — every document and every dropped page, with
                                       two EMPTY columns for you to type into
       splits/HOW-TO-REVIEW.md       — the workflow, next to the files

Two defects in the first version, both reported by Alden as "basically useless --
there are no names of files or links to them":

  1. The 69 DISCARDED/UNRESOLVED rows sort to the top (deliberately -- dropping a
     page is the only lossy thing this pipeline did) but carried NO identity:
     blank issuer, blank date, and a filename like `_discarded/018_p333.pdf`.
     So the sheet OPENED on 69 rows of nothing. Fixed by backfilling issuer,
     account, date and kind from ocr/_index.json, which fingerprinted every page.

  2. Nothing was clickable. 581 rows, and opening one file meant hunting through
     Explorer. Fixed with an OPEN column of =HYPERLINK() formulas, plus a
     COMPARE column that links a discarded page to the kept document it is
     alleged to duplicate -- without that, "is this really a duplicate?" is not
     answerable from the sheet at all.

Re-running is safe: any DECISION/CORRECTION already typed in is read back and
carried over, matched on FILE.
"""
import csv, io, json, os, re

BASE = os.path.dirname(os.path.abspath(__file__))
S = os.path.join(BASE, "splits")
OUT = os.path.join(S, "_review-decisions.csv")

built = json.load(io.open(os.path.join(S, "_built.json"), encoding="utf-8"))
wanted = json.load(io.open(os.path.join(S, "_wanted.json"), encoding="utf-8"))
index = json.load(io.open(os.path.join(BASE, "ocr", "_index.json"), encoding="utf-8"))

# ---------------------------------------------------------------- page identity
# _index.json has 957 entries for 791 pages -- the overlap re-reads mean some
# (src, page) keys appear twice. Last wins; for display fields either reading is
# equally good, and where they differed the assembly agents already adjudicated.
fp = {(e["src"], int(e["page"])): e for e in index}


def short_issuer(s):
    """Issuers are recorded with full mailing addresses. The parenthetical is
    useful for disambiguating two offices of the same body, but it is 80 chars
    of noise in a spreadsheet cell -- keep the name, drop the address."""
    return re.split(r"\s*\(", (s or "").strip())[0][:70]


def short_date(s):
    """doc_date carries an annotation, e.g. '2021-06-25 (Bill Date)'."""
    m = re.search(r"\d{4}-\d{2}-\d{2}", s or "")
    return m.group(0) if m else (s or "").strip()[:24]


def link(relpath, label="open"):
    """Excel evaluates formulas in a CSV cell, so this renders as a real
    hyperlink. Plain Windows path, NOT file:/// -- the workspace directory has
    spaces in it and a file: URL would need them percent-encoded."""
    if not relpath:
        return ""
    p = os.path.join(S, relpath.replace("/", os.sep)).replace('"', '""')
    return '=HYPERLINK("%s","%s")' % (p, label)


# ------------------------------------------------------- page -> built document
# Needed for COMPARE: a discarded page's reason says "duplicate of 018 p332", and
# the only useful next click is the output PDF that actually contains p332.
page_owner = {}
for d in built["documents"]:
    for p in d["pages"]:
        page_owner[(p["src"], int(p["page"]))] = d["file"]

REF_RE = re.compile(r"\b(?:(\d{3})\s+)?p(\d{1,3})\b")


discard_reason = {(e["src"], int(e["page"])): e.get("reason", "")
                  for e in built["discarded"]}


def compare_target(reason, src, own_page, _seen=None):
    """Pull the first cross-referenced page out of the free-text reason and
    resolve it to the document that kept it.

    Duplicates chain: 018 p349 is a duplicate of p348, which was ITSELF dropped
    as a duplicate of p351. Following the chain is what makes the link useful --
    stopping at the first hop just points at another discarded page. Cycle-safe.
    Returns ('', '') when nothing kept is reachable, which is correct for the
    two photographs of upholstery that duplicate each other and nothing else."""
    _seen = _seen or {(src, own_page)}
    for m in REF_RE.finditer(reason or ""):
        ref = ((m.group(1) or src), int(m.group(2)))
        if ref in _seen:
            continue
        _seen.add(ref)
        owner = page_owner.get(ref)
        if owner:
            return owner, "%s p%d" % ref
        if ref in discard_reason:                    # dropped too -- keep going
            hop = compare_target(discard_reason[ref], ref[0], ref[1], _seen)
            if hop[0]:
                return hop
    return "", ""


# ------------------------------------------------------------ incomplete lookup
wl = {}
for w in wanted:
    wl.setdefault((w.get("label") or "").strip().lower(), []).append(
        ", ".join(w.get("missing", [])) or w.get("evidence", "") or "?")

# ----------------------------------------------------- carry over any decisions
prior = {}
if os.path.exists(OUT):
    for r in csv.DictReader(io.open(OUT, encoding="utf-8-sig")):
        if (r.get("DECISION") or "").strip() or (r.get("CORRECTION") or "").strip():
            prior[r["FILE"]] = (r.get("DECISION", ""), r.get("CORRECTION", ""))

rows = []


def add(**kw):
    dec, cor = prior.get(kw["FILE"], ("", ""))
    row = {"DECISION": dec, "CORRECTION": cor, "OPEN": link(kw["FILE"])}
    row.update(kw)
    rows.append(row)


for d in built["documents"]:
    lab = (d.get("label") or "").strip()
    miss = wl.get(lab.lower(), [])
    conf = (d.get("confidence") or "high").lower()
    reordered = bool(d.get("reordered"))
    review = reordered or conf not in ("high", "") or bool(miss)
    add(REVIEW="yes" if review else "",
        WHY=" + ".join(filter(None, [
            "REORDERED" if reordered else "",
            ("confidence:" + conf) if conf not in ("high", "") else "",
            "INCOMPLETE" if miss else ""])),
        FILE=d["file"],
        ISSUER=short_issuer(d.get("issuer", "")),
        ACCOUNT=d.get("account", ""),
        DATE=d.get("doc_date", ""),
        KIND=d.get("doc_kind", ""),
        PAGES=len(d["pages"]),
        COMPARE="", COMPARE_PAGE="",
        SOURCE_PAGES=", ".join("%s p%d" % (p["src"], p["page"]) for p in d["pages"]),
        ORDER_EVIDENCE=d.get("order_evidence", ""),
        BUCKET=d.get("bucket", ""),
        MISSING="; ".join(miss),
        NOTES=(d.get("notes") or "").replace("\n", " ")[:400])

# The lossy ones. These are the rows that were unusable before.
for key in ("discarded", "unresolved"):
    for e in built[key]:
        src, page = e["src"], int(e["page"])
        f = fp.get((src, page), {})
        target, target_page = compare_target(e.get("reason", ""), src, page)
        add(REVIEW="yes",
            WHY=key.upper(),
            FILE="_%s/%s_p%03d.pdf" % (key, src, page),
            ISSUER=short_issuer(f.get("issuer")),
            ACCOUNT=(f.get("account") or "")[:40],
            DATE=short_date(f.get("doc_date")),
            KIND=(f.get("doc_kind") or key)[:40],
            PAGES=1,
            COMPARE=link(target, "compare") if target else "",
            COMPARE_PAGE=target_page,
            SOURCE_PAGES="%s p%d" % (src, page),
            ORDER_EVIDENCE="",
            BUCKET=e.get("bucket", ""),
            MISSING="",
            NOTES=(e.get("reason") or "").replace("\n", " ")[:400])

rows.sort(key=lambda r: (r["REVIEW"] == "", r["WHY"], r["BUCKET"],
                         r["DATE"] or "zzz", r["FILE"]))

COLS = ["DECISION", "CORRECTION", "OPEN", "REVIEW", "WHY", "FILE", "ISSUER",
        "ACCOUNT", "DATE", "KIND", "PAGES", "COMPARE", "COMPARE_PAGE",
        "SOURCE_PAGES", "ORDER_EVIDENCE", "BUCKET", "MISSING", "NOTES"]
HEAD = {"COMPARE_PAGE": "COMPARE PAGE", "SOURCE_PAGES": "SOURCE PAGES",
        "ORDER_EVIDENCE": "ORDER EVIDENCE"}

with io.open(OUT, "w", newline="", encoding="utf-8-sig") as fh:
    w = csv.writer(fh)
    w.writerow([HEAD.get(c, c) for c in COLS])
    for r in rows:
        w.writerow([r.get(c, "") for c in COLS])

README = """# How to review the split -- read this first

**512 documents. You do not have to look at all of them.** 134 rows are flagged
`REVIEW=yes`; the rest are clean, ascending, high-confidence documents that need
nothing from you.

## Two ways to do this. Pick either -- they use the same file.

### 1. The review screen (recommended)

In the project folder, double-click **`Review documents.cmd`**. A black window
opens and stays open -- that is the program running; leave it alone -- and your
browser opens the review screen.

It shows the PDF on the left and the question about it on the right, with a
dropdown for your decision and a box for notes. Three tabs across the top:

- **Review one by one** -- walk the flagged rows in order. Arrow keys move,
  `Save & next` records your answer and advances. It says "12 of 134" so you
  always know how far in you are.
- **List** -- browse everything, click any row to jump to it.
- **Table** -- the full spreadsheet on screen, text wrapped so nothing is cut off.

Every answer is written into the spreadsheet the instant you make it. You can
close the window at any point and pick up where you left off. Close the black
window when you are done.

### 2. The spreadsheet

**`splits/_review-decisions.csv`** -- open it in Excel. You type into the two
empty columns on the far left. Nothing else needs editing.

⚠ **Do not leave it open in Excel while the review screen is running** -- Excel
locks the file and the screen will not be able to save. It will tell you so
rather than losing anything, but it is easier to just use one at a time.

| Column | What you do |
|---|---|
| **DECISION** | one word (see below). Leave blank = "no opinion yet" -- **blank is NOT approval** |
| **CORRECTION** | free text, only when the decision needs explaining |

**Column C, `OPEN`, is a live link -- click it and the PDF for that row opens.**
On the 65 discarded rows there is a second link, **`COMPARE`**, which opens the
document that kept the page this one was dropped as a duplicate of. Put them
side by side and the question answers itself.

Everything to the right is read-only context: why it was flagged, issuer,
account, date, which original container pages it came from, and the assembling
agent's reasoning.

## The words you can put in DECISION

| Word | Means |
|---|---|
| `ok` | correct as-is |
| `wrong-order` | right set of pages, wrong order -- say the right order in CORRECTION |
| `split` | this is really two or more documents -- say where to cut in CORRECTION |
| `merge` | this belongs with another document -- name the other FILE in CORRECTION |
| `relabel` | grouping is right, the label/date/account is wrong -- put the right value in CORRECTION |
| `restore` | (discarded rows only) this page was wrongly dropped -- put it back |
| `drop` | this should have been discarded -- say why in CORRECTION |
| `?` | you are unsure and want me to look again |

## Suggested order of work -- roughly 90 minutes

1. **Filter `WHY = DISCARDED` (65 rows).** Dropping a page is the only lossy
   thing this pipeline did. Click `OPEN`, click `COMPARE`, confirm they really
   are the same document, mark `ok` or `restore`. **Do this first** -- it is the
   only irreversible category.
2. **Filter `WHY = REORDERED` (9 rows).** Open the file; check the pages read in
   sequence. SOURCE PAGES shows the order I put them in; ORDER EVIDENCE says why.
   Mark `ok` or `wrong-order`.
3. **Filter `WHY = UNRESOLVED` (4 rows).** Four pages nothing could place. Tell
   me what they are, or mark `drop`.
4. **Filter `WHY` contains `confidence` (42 rows).** Groupings the agent was
   unsure about. Skim NOTES -- it says what the doubt was.
5. **Optional: sort by ISSUER** and skim a whole account end to end. This is the
   best way to catch a document filed under the wrong account, which no flag
   catches.

You can stop at any point. Partial review is fine -- I only act on rows you
filled in, and re-running the sheet builder keeps whatever you have typed.

## When you are done

Save it (keep it as CSV, not .xlsx) and tell me. Nothing is applied without you
seeing a dry run of the exact changes first, and `raw/` is never modified.

## What is NOT in this sheet

- **`_wanted.json` (96 incomplete documents)** -- documents missing pages. There
  is nothing to decide there; it is a shopping list of paper to go find.
- **The open questions** -- those live in the Trello list *"Open Questions --
  Master Register"* on the Dobbins Command Center board, and in
  `llm-wiki/concepts/open-questions-register.md`. Answer them there, not here.
"""
io.open(os.path.join(S, "HOW-TO-REVIEW.md"), "w", encoding="utf-8").write(README)

flagged = sum(1 for r in rows if r["REVIEW"])
named = sum(1 for r in rows if r["WHY"] in ("DISCARDED", "UNRESOLVED") and r["ISSUER"])
comps = sum(1 for r in rows if r["COMPARE"])
print("wrote splits/_review-decisions.csv  %d rows, %d flagged" % (len(rows), flagged))
print("   dropped pages now carrying an issuer name : %d" % named)
print("   dropped pages linked to their kept twin   : %d" % comps)
print("   decisions carried over from previous sheet: %d" % len(prior))
print("wrote splits/HOW-TO-REVIEW.md")
by = {}
for r in rows:
    if r["REVIEW"]:
        by[r["WHY"]] = by.get(r["WHY"], 0) + 1
for k, v in sorted(by.items(), key=lambda x: -x[1]):
    print("   %-28s %3d" % (k, v))

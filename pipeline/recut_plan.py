# /// script
# requires-python = ">=3.10"
# dependencies = ["pymupdf>=1.24", "psycopg[binary]>=3.1"]
# ///
"""PHASE 1 of the re-cut: build the cut plan and audit it. Cuts NOTHING.

    uv run recut_plan.py

Produces recut-plan.json -- every output document as an ordered list of
(v2_file, page) references -- plus an audit that must pass before Phase 2 runs.

Why audit-first: the output of the cut is what we pay Mistral to read. A page
silently dropped here is a document that never gets OCR'd, and a page silently
duplicated is a document we pay for twice. Neither is visible after the fact.

Every v2 page must land in exactly one of four buckets:
  CUT             - becomes part of an output document
  DELIBERATELY-UNUSED - the v1 work discarded it (duplicate / not-a-document)
  PASS-THROUGH    - the file is already a single document, used whole
  UNACCOUNTED     - nobody claims it. Any of these is a hard stop.
"""
import io
import json
import os
import re
import sys
from collections import Counter, defaultdict

import fitz
import psycopg

HERE = os.path.dirname(os.path.abspath(__file__))
V2 = os.path.join(HERE, "genius scan v2 from google drive")
ALIGN = os.path.join(HERE, "v1-v2-alignment.json")
OUT = os.path.join(HERE, "recut-plan.json")

# v2 files that are containers of KNOWN v1 documents
IDENTITY = {
    "018-stcu-411pp.pdf": "2026-05-11 17-28.pdf",
    "017-pacific-power-198pp.pdf": "2026-05-10 23-18.pdf",
    "014-santa-clara-dtac-80pp.pdf": "2026-05-09 22-46.pdf",
    "015-we-oneil-35pp.pdf": "2026-05-09 23-16.pdf",
}
# new containers with a local, unambiguous rule
ONE_DOC_PER_PAGE = [
    "2026-06-12 14-34.pdf",          # 15 separate lawn-service invoices
    "2026-07-10 17-54.pdf", "2026-07-10 18-07 1.pdf", "2026-07-11 19-29.pdf",
    "2026-07-13 00-29.pdf", "2026-07-13 14-29 2.pdf", "2026-07-13 15-02 1.pdf",
    "2026-07-13 15-09 2.pdf", "2026-07-13 18-11.pdf",   # receipts, one per page
]
SPLIT_ON_PAGE_MARKER = ["2026-05-31 21-35.pdf", "2026-05-31 21-56.pdf"]  # 2 Chase stmts each
CALIMOVERS = "2026-08-01 15-16 1.pdf"   # p3 is a refund receipt; rest is the contract
SINGLE_DOC = ["Statements.pdf"]          # 'Page N of 14' throughout


def v2_pagecount(name):
    d = fitz.open(os.path.join(V2, name))
    n = d.page_count
    d.close()
    return n


def build_v1_to_v2():
    """(v1_file, v1_page) -> (v2_file, v2_page). Sources: identity, gate B, gate C."""
    a = json.load(io.open(ALIGN, encoding="utf-8"))
    m = {}
    for v1n, v2n in IDENTITY.items():
        for i in range(1, v2_pagecount(v2n) + 1):
            m[(v1n, i)] = (v2n, i)
    b = a["gates"]["B_013_deletions"]
    for v1p, v2p in b["map_v1_to_v2"].items():
        m[(b["v1_file"], int(v1p))] = (b["v2_file"], int(v2p))
    # 013's two DirecTV pages were split out into their own file (verified separately)
    m[("013-san-jose-water-24pp.pdf", 23)] = ("2026-08-11 09-24.pdf", 1)
    m[("013-san-jose-water-24pp.pdf", 24)] = ("2026-08-11 09-24.pdf", 2)
    # The hunt matched each v1 page independently, so two v1 pages can end up on
    # the same v2 page -- which is impossible, they are distinct sheets of paper.
    # Resolve: highest score keeps the page, the loser falls to its next-best
    # candidate that is still free.
    for v1n, h in a["gates"]["C_hunt"].items():
        taken = {}
        rows = sorted(h["rows"], key=lambda r: -r["top"][0]["score"])
        for row in rows:
            placed = None
            for cand in row["top"]:
                ref = (cand["file"], cand["page"])
                if ref not in taken:
                    taken[ref] = row["v1_page"]
                    placed = (ref, cand["score"])
                    break
            if placed:
                m[(v1n, row["v1_page"])] = placed[0]
            else:
                print("    ! %s p%d: every candidate already claimed - left unmapped"
                      % (v1n, row["v1_page"]))

        # Run repair. Score alone is unreliable here because this archive is full
        # of the same paperwork filed twice, so a page's true home can score below
        # its duplicate elsewhere. But sequence is decisive: if a page's neighbours
        # land on file F pages n-1 and n+1, the page belongs on F page n. Prefer
        # that whenever the implied slot is free.
        n_pages = max(r["v1_page"] for r in h["rows"])
        offsets = Counter((m[(v1n, i)][0], m[(v1n, i)][1] - i)
                          for i in range(1, n_pages + 1) if (v1n, i) in m)
        if not offsets:
            continue
        (dom_file, dom_off), votes = offsets.most_common(1)[0]
        if votes < 3:
            continue
        dom_pages = v2_pagecount(dom_file)
        print("    dominant run: %s -> %s offset %+d (%d pages agree)"
              % (v1n[:22], dom_file[:24], dom_off, votes))
        for i in range(1, n_pages + 1):
            implied = (dom_file, i + dom_off)
            if not (1 <= implied[1] <= dom_pages):
                continue          # outside the file: this page genuinely lives elsewhere
            if implied in taken:
                continue          # slot already correctly filled
            cur = m.get((v1n, i))
            if cur == implied:
                continue
            if cur:
                taken.pop(cur, None)
            m[(v1n, i)] = implied
            taken[implied] = i
            print("      repair: p%-2d -> %s p%-2d  (was %s)"
                  % (i, implied[0][:24], implied[1],
                     ("%s p%d" % (cur[0][:22], cur[1])) if cur else "unmapped"))
    return m


def main():
    url = os.environ.get("NEON_DATABASE_URL")
    if not url:
        sys.exit("NEON_DATABASE_URL not set")
    v1v2 = build_v1_to_v2()

    with psycopg.connect(url) as conn:
        docs = conn.execute("""
            select d.key, d.label, f.name as v1_file, p.page_no, dp.position, p.status
            from document d
            join document_page dp on dp.document_id = d.id
            join source_page p on p.id = dp.page_id
            join source_file f on f.id = p.file_id
            where f.origin = 'v1-merged'
            order by d.key, dp.position""").fetchall()
        discarded = conn.execute("""
            select f.name, p.page_no, coalesce(p.reason,'')
            from source_page p join source_file f on f.id = p.file_id
            left join document_page dp on dp.page_id = p.id
            where f.origin = 'v1-merged' and dp.document_id is null""").fetchall()

    # ---------------------------------------------------------- known documents
    plan, unmapped = defaultdict(list), defaultdict(list)
    labels = {}
    for key, label, v1f, pno, pos, status in docs:
        labels[key] = label
        ref = v1v2.get((v1f, pno))
        if ref:
            plan[key].append({"v2_file": ref[0], "page": ref[1],
                              "from_v1": "%s p%d" % (v1f, pno), "position": pos})
        else:
            unmapped[key].append("%s p%d" % (v1f, pno))

    # ------------------------------------------------------------- new documents
    new_docs = {}
    for f in ONE_DOC_PER_PAGE:
        for i in range(1, v2_pagecount(f) + 1):
            new_docs["recut:%s:p%d" % (f[:-4], i)] = [{"v2_file": f, "page": i, "from_v1": None, "position": 1}]
    for f in SPLIT_ON_PAGE_MARKER:
        d = fitz.open(os.path.join(V2, f))
        starts = [i + 1 for i in range(d.page_count)
                  if re.search(r'page\s+1\s+of\s+\d', " ".join(d[i].get_text().split()), re.I)]
        n = d.page_count
        d.close()
        if not starts or starts[0] != 1:
            starts = [1] + starts
        bounds = starts + [n + 1]
        for si in range(len(starts)):
            pages = list(range(bounds[si], bounds[si + 1]))
            new_docs["recut:%s:d%d" % (f[:-4], si + 1)] = [
                {"v2_file": f, "page": p, "from_v1": None, "position": k + 1}
                for k, p in enumerate(pages)]
    n = v2_pagecount(CALIMOVERS)
    new_docs["recut:calimovers-refund-receipt"] = [{"v2_file": CALIMOVERS, "page": 3, "from_v1": None, "position": 1}]
    new_docs["recut:calimovers-moving-contract"] = [
        {"v2_file": CALIMOVERS, "page": p, "from_v1": None, "position": k + 1}
        for k, p in enumerate([x for x in range(1, n + 1) if x != 3])]

    # ------------------------------------------------------------------- audit
    all_v2 = sorted(f for f in os.listdir(V2) if f.lower().endswith(".pdf"))
    pagecounts = {f: v2_pagecount(f) for f in all_v2}
    total_pages = sum(pagecounts.values())

    claims = defaultdict(list)
    for key, refs in list(plan.items()) + list(new_docs.items()):
        for r in refs:
            claims[(r["v2_file"], r["page"])].append(key)

    touched_files = {f for f, _ in claims} | set(SINGLE_DOC)
    passthrough = [f for f in all_v2 if f not in touched_files]
    pt_pages = sum(pagecounts[f] for f in passthrough)
    single_pages = sum(pagecounts[f] for f in SINGLE_DOC)

    # a v2 page is legitimately unclaimed if it is the twin of a v1 page the
    # original work deliberately discarded (duplicate / not-a-document)
    deliberately_unused = {}
    for name, pno, reason in discarded:
        ref = v1v2.get((name, pno))
        if ref:
            deliberately_unused[ref] = "%s p%d: %s" % (name, pno, reason[:60])

    unaccounted, unused_hits = [], 0
    for f in all_v2:
        if f in passthrough or f in SINGLE_DOC:
            continue
        for p in range(1, pagecounts[f] + 1):
            if (f, p) in claims:
                continue
            if (f, p) in deliberately_unused:
                unused_hits += 1
            else:
                unaccounted.append((f, p))
    multi = {k: v for k, v in claims.items() if len(v) > 1}

    print("=" * 74)
    print("CUT PLAN AUDIT")
    print("=" * 74)
    print("v2 corpus                     %4d files   %5d pages" % (len(all_v2), total_pages))
    print("  pass-through (already 1 doc)%4d files   %5d pages" % (len(passthrough), pt_pages))
    print("  whole-file documents        %4d files   %5d pages" % (len(SINGLE_DOC), single_pages))
    print("  cut into documents                       %5d pages" % len(claims))
    print("  deliberately unused (v1 discarded)       %5d pages" % unused_hits)
    print()
    print("documents out of the cut")
    print("  from known v1 boundaries    %4d" % len(plan))
    print("  new (lawn/receipts/Chase/movers) %d" % len(new_docs))
    print("  pass-through singles        %4d" % len(passthrough))
    print("  whole-file                  %4d" % len(SINGLE_DOC))
    print("  ---------------------------------")
    print("  TOTAL DOCUMENTS FOR OCR     %4d" % (len(plan) + len(new_docs) + len(passthrough) + len(SINGLE_DOC)))
    print()
    print("v1 pages the v1 work deliberately left out: %d" % len(discarded))
    for name, pno, reason in discarded[:4]:
        print("   %s p%-3d %s" % (name[:28], pno, reason[:56]))
    if len(discarded) > 4:
        print("   ... and %d more" % (len(discarded) - 4))
    print()
    if unmapped:
        print("*** DOCUMENTS WITH PAGES THAT HAVE NO v2 EQUIVALENT: %d ***" % len(unmapped))
        for k, v in list(unmapped.items())[:8]:
            print("   %-28s missing %s" % (k, v))
    else:
        print("every page of every known document maps to a v2 page   OK")
    print()
    if multi:
        print("v2 pages claimed by MORE THAN ONE document: %d (expected: the cross-container duplicates)" % len(multi))
        for (f, p), keys in list(multi.items())[:6]:
            print("   %-26s p%-3d claimed by %s" % (f[:26], p, ", ".join(keys)))
    else:
        print("no v2 page is claimed twice   OK")
    print()
    if unaccounted:
        print("*** UNACCOUNTED v2 PAGES (HARD STOP): %d ***" % len(unaccounted))
        for f, p in unaccounted[:12]:
            print("   %-30s p%d" % (f[:30], p))
    else:
        print("every page of every container is claimed   OK")

    json.dump({"documents_from_v1": {k: v for k, v in plan.items()},
               "documents_new": new_docs,
               "passthrough_files": passthrough,
               "whole_file_documents": SINGLE_DOC,
               "audit": {"v2_files": len(all_v2), "v2_pages": total_pages,
                         "unaccounted": [{"file": f, "page": p} for f, p in unaccounted],
                         "multi_claimed": {"%s|%d" % k: v for k, v in multi.items()},
                         "documents_with_unmapped_pages": dict(unmapped),
                         "total_documents": len(plan) + len(new_docs) + len(passthrough) + len(SINGLE_DOC)}},
              io.open(OUT, "w", encoding="utf-8"), indent=2)
    print()
    print("wrote %s   (nothing has been cut - this is the plan only)" % OUT)


if __name__ == "__main__":
    main()

"""Build a spreadsheet index of the split output, for human review.

    python review_index.py

Writes splits/_review-index.csv  — one row per document, openable in Excel.

Why a CSV and not the 261-page contact-sheet PDF: you can sort, filter and search
it. Filter REVIEW=yes to see only the ~120 documents that need a decision; sort by
ISSUER to check a whole account at once; sort by DATE to spot chronology gaps.

Column REVIEW is 'yes' when the document was reordered, is below high confidence,
or is known to be missing pages — i.e. anywhere a wrong call costs something.
"""
import csv, json, os

BASE = os.path.dirname(os.path.abspath(__file__))
S = os.path.join(BASE, "splits")

built = json.load(open(os.path.join(S, "_built.json"), encoding="utf-8"))
wanted = json.load(open(os.path.join(S, "_wanted.json"), encoding="utf-8"))

# a document is "incomplete" if a wanted entry names it
wl = {}
for w in wanted:
    wl.setdefault((w.get("label") or "").strip().lower(), []).append(
        ", ".join(w.get("missing", [])) or w.get("evidence", "") or "?")

rows = []
for d in built["documents"]:
    lab = (d.get("label") or "").strip()
    miss = wl.get(lab.lower(), [])
    conf = (d.get("confidence") or "high").lower()
    reordered = bool(d.get("reordered"))
    review = reordered or conf not in ("high", "") or bool(miss)
    src = ", ".join("%s p%d" % (p["src"], p["page"]) for p in d["pages"])
    rows.append({
        "REVIEW": "yes" if review else "",
        "WHY": " + ".join(filter(None, [
            "REORDERED" if reordered else "",
            ("confidence:" + conf) if conf not in ("high", "") else "",
            "INCOMPLETE" if miss else ""])),
        "FILE": d["file"],
        "ISSUER": d.get("issuer", ""),
        "ACCOUNT": d.get("account", ""),
        "DATE": d.get("doc_date", ""),
        "KIND": d.get("doc_kind", ""),
        "PAGES": len(d["pages"]),
        "SOURCE PAGES": src,
        "ORDER EVIDENCE": d.get("order_evidence", ""),
        "BUCKET": d.get("bucket", ""),
        "MISSING": "; ".join(miss),
        "NOTES": (d.get("notes") or "").replace("\n", " ")[:400],
    })

rows.sort(key=lambda r: (r["REVIEW"] == "", r["BUCKET"], r["DATE"] or "zzz", r["FILE"]))

path = os.path.join(S, "_review-index.csv")
with open(path, "w", newline="", encoding="utf-8-sig") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)

# a second sheet for the pages that were dropped -- the only lossy outcome
dpath = os.path.join(S, "_review-discarded.csv")
with open(dpath, "w", newline="", encoding="utf-8-sig") as f:
    w = csv.writer(f)
    w.writerow(["TYPE", "SOURCE", "PAGE", "PDF", "REASON", "BUCKET"])
    for key in ("discarded", "unresolved"):
        for e in built[key]:
            w.writerow([key, e["src"], e["page"],
                        "_%s/%s_p%03d.pdf" % (key, e["src"], e["page"]),
                        (e.get("reason") or "").replace("\n", " "),
                        e.get("bucket", "")])

n_rev = sum(1 for r in rows if r["REVIEW"])
print("wrote _review-index.csv     %d documents, %d flagged for review" % (len(rows), n_rev))
print("wrote _review-discarded.csv %d pages (%d discarded + %d unresolved)"
      % (len(built["discarded"]) + len(built["unresolved"]),
         len(built["discarded"]), len(built["unresolved"])))

# /// script
# requires-python = ">=3.10"
# dependencies = ["psycopg[binary]>=3.1"]
# ///
"""Load the Mistral OCR run into Neon. Idempotent -- safe to re-run.

    uv run load_mistral_to_neon.py            # load
    uv run load_mistral_to_neon.py --dry-run  # report what would change

Reads ocr-mistral/*.raw.json + recut-plan.json. Writes nothing to disk.

Deliberately SEPARATE from the OCR runner: the raw JSON cost $8.88 and cannot be
reproduced exactly, so if the mapping here is wrong it must be fixable by
re-running this script for free rather than by paying for OCR again.

Three jobs, because the recut created documents Neon had never seen:

  1. document rows for the 1,021 output_file rows with document_id NULL
     (907 pass-through files + 44 new splits). state='recut_new' so they are
     never mistaken for the v1 documents that went through human review.
  2. ocr_reading rows, method='mistral-ocr-4-1', one per PAGE, attached to the
     v2 source_page they came from -- all 1,827 v2 pages are already in Neon.
  3. document.meta.annotation -- the 29-field fact sheet, per document.
"""
import argparse
import io
import json
import os
import re
import sys

import psycopg

HERE = os.path.dirname(os.path.abspath(__file__))
OCR = os.path.join(HERE, "ocr-mistral")
PLAN = os.path.join(HERE, "recut-plan.json")
METHOD = "mistral-ocr-4-1"


def safe(name):
    """Must match recut_build.safe() so output filenames map back to plan keys."""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)[:150]


def build_refs():
    """output-file stem -> ordered [(v2_file, page)] and its plan key."""
    plan = json.load(io.open(PLAN, encoding="utf-8"))
    out = {}
    for group in ("documents_from_v1", "documents_new"):
        for key, refs in plan[group].items():
            stem = safe(key if key.lower().endswith(".pdf") else key + ".pdf")[:-4]
            out[stem] = {
                "key": key,
                "refs": [(r["v2_file"], r["page"])
                         for r in sorted(refs, key=lambda x: x["position"])],
            }
    for f in plan["passthrough_files"] + plan["whole_file_documents"]:
        stem = safe(f)[:-4]
        out[stem] = {"key": f, "refs": None, "whole_file": f}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    url = os.environ.get("NEON_DATABASE_URL")
    if not url:
        sys.exit("NEON_DATABASE_URL not set")

    refs = build_refs()
    raws = sorted(f for f in os.listdir(OCR) if f.endswith(".raw.json"))
    print("raw OCR files: %d | plan entries: %d" % (len(raws), len(refs)))

    conn = psycopg.connect(url)
    cur = conn.cursor()

    # (v2_file, page_no) -> source_page.id, so readings attach to the real page
    cur.execute("""select f.name, p.page_no, p.id
                   from source_page p join source_file f on f.id = p.file_id
                   where f.origin = 'v2-genius-scan'""")
    page_id = {(n, no): i for n, no, i in cur.fetchall()}
    print("v2 source_page rows available: %d" % len(page_id))

    # output_file rows written by recut_build, keyed by name
    cur.execute("select name, id, document_id from output_file where build_version = 'recut-v2'")
    outputs = {n: (i, d) for n, i, d in cur.fetchall()}
    print("recut-v2 output_file rows: %d" % len(outputs))

    cur.execute("select key, id from document")
    doc_by_key = dict(cur.fetchall())

    made_docs = readings = annotated = 0
    unmatched, no_page = [], 0

    for k, rf in enumerate(raws, 1):
        stem = rf[:-len(".raw.json")]
        info = refs.get(stem)
        if not info:
            unmatched.append(stem)
            continue
        r = json.load(io.open(os.path.join(OCR, rf), encoding="utf-8"))

        # ---- 1. document row (create only if this output has none) ----
        key = info["key"]
        did = doc_by_key.get(key)
        if did is None:
            out = outputs.get(safe(key if key.lower().endswith(".pdf") else key + ".pdf"))
            did = out[1] if out and out[1] else None
        if did is None and not args.dry_run:
            cur.execute("""insert into document (key, label, state, meta)
                           values (%s, %s, 'recut_new', %s::jsonb)
                           on conflict (key) do update set key = excluded.key
                           returning id""",
                        (key, key[:200], json.dumps({"source": "recut-v2"})))
            did = cur.fetchone()[0]
            doc_by_key[key] = did
            made_docs += 1
            oname = safe(key if key.lower().endswith(".pdf") else key + ".pdf")
            cur.execute("""update output_file set document_id = %s
                           where name = %s and build_version = 'recut-v2'
                             and document_id is null""", (did, oname))

        # ---- 2. one ocr_reading per page ----
        pages = r.get("pages") or []
        for pno, mp in enumerate(pages):
            if info["refs"]:
                if pno >= len(info["refs"]):
                    break
                src = info["refs"][pno]
            else:
                src = (info["whole_file"], pno + 1)
            pid = page_id.get(src)
            if pid is None:
                no_page += 1
                continue
            blocks = {"blocks": mp.get("blocks"), "tables": mp.get("tables"),
                      "images": mp.get("images"), "hyperlinks": mp.get("hyperlinks"),
                      "header": mp.get("header"), "footer": mp.get("footer"),
                      "dimensions": mp.get("dimensions")}
            if not args.dry_run:
                cur.execute("""insert into ocr_reading
                        (page_id, method, text, blocks, confidence, model, meta)
                        values (%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s::jsonb)""",
                    (pid, METHOD, mp.get("markdown") or "",
                     json.dumps(blocks, ensure_ascii=False),
                     json.dumps(mp.get("confidence_scores") or {}, ensure_ascii=False),
                     r.get("model"),
                     json.dumps({"document": key, "doc_page": pno + 1}, ensure_ascii=False)))
            readings += 1

        # ---- 3. annotation onto the document ----
        ann = r.get("document_annotation")
        if ann and did:
            try:
                parsed = json.loads(ann) if isinstance(ann, str) else ann
            except Exception:
                parsed = None
            if parsed and not args.dry_run:
                cur.execute("""update document set meta = meta ||
                        jsonb_build_object('annotation', %s::jsonb,
                                           'annotation_model', %s::text)
                        where id = %s""",
                    (json.dumps(parsed, ensure_ascii=False), r.get("model"), did))
                annotated += 1
            elif parsed:
                annotated += 1

        if k % 200 == 0:
            print("  %d/%d processed" % (k, len(raws)))
            if not args.dry_run:
                conn.commit()

    if not args.dry_run:
        conn.commit()

    print()
    print("documents created (state=recut_new) : %d" % made_docs)
    print("ocr_reading rows written            : %d" % readings)
    print("documents annotated                 : %d" % annotated)
    print("pages with no matching source_page  : %d" % no_page)
    print("raw files not in the plan           : %d %s" % (len(unmatched), unmatched[:3]))

    if not args.dry_run:
        for label, q in [
            ("ocr_reading mistral rows", "select count(*) from ocr_reading where method='%s'" % METHOD),
            ("documents total", "select count(*) from document"),
            ("documents with annotation", "select count(*) from document where meta ? 'annotation'"),
            ("output_file still unlinked", "select count(*) from output_file where build_version='recut-v2' and document_id is null"),
        ]:
            cur.execute(q)
            print("  %-32s %d" % (label, cur.fetchone()[0]))
    conn.close()


if __name__ == "__main__":
    main()

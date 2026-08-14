# /// script
# requires-python = ">=3.10"
# dependencies = ["psycopg[binary]>=3.1"]
# ///
"""Record every page's R2 image in Neon.

    uv run link_page_images.py --manifest <pages-r2/_manifest.json>

WHY A TABLE AND NOT A CONVENTION
--------------------------------
The key itself is predictable ("pages/<page_id>.jpg"), so the app does not
strictly need a lookup to FIND an image. What it cannot derive is the geometry:

    pt_to_px -- how many JPEG pixels one PDF point is worth, for THIS page

That single number is what lets Mistral's block boxes be drawn over the scan.
Blocks are stored in Mistral's own coordinate space, the JPEG is in pixels, and
the ratio differs per page because the source scans are not one size (measured:
native resolution runs 91-328 DPI across the corpus). Storing it per page means
the overlay is a multiplication rather than a guess.

Width and height are kept for the same reason -- the browser can size the canvas
before the image loads, so boxes do not jump on first paint.
"""
import argparse, io, json, os, sys
import psycopg

DDL = """
create table if not exists page_image (
  page_id     bigint primary key,
  document_id bigint,
  doc_page    int,
  r2_key      text not null,
  width       int,
  height      int,
  dpi         int,
  pt_to_px    double precision,
  bytes       bigint,
  uploaded    boolean not null default false,
  ts          timestamptz not null default now()
);
create index if not exists page_image_document_idx on page_image (document_id);
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--mark-uploaded", action="store_true",
                    help="set uploaded=true (run after upload_pages_r2.py)")
    args = ap.parse_args()

    rows = json.load(io.open(args.manifest, encoding="utf-8"))
    print("manifest entries: %d" % len(rows))

    con = psycopg.connect(os.environ["NEON_DATABASE_URL"], connect_timeout=30)
    cur = con.cursor()
    cur.execute(DDL)
    con.commit()

    payload = [(r["page_id"], r.get("document_id"), r.get("doc_page"),
                r["key"], r.get("w"), r.get("h"), r.get("dpi"),
                r.get("pt_to_px"), r.get("bytes"), bool(args.mark_uploaded))
               for r in rows]

    cur.executemany("""
        insert into page_image
          (page_id, document_id, doc_page, r2_key, width, height, dpi,
           pt_to_px, bytes, uploaded)
        values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        on conflict (page_id) do update set
          r2_key=excluded.r2_key, width=excluded.width, height=excluded.height,
          dpi=excluded.dpi, pt_to_px=excluded.pt_to_px, bytes=excluded.bytes,
          uploaded=page_image.uploaded or excluded.uploaded, ts=now()
    """, payload)
    con.commit()

    cur.execute("select count(*), count(*) filter (where uploaded) from page_image")
    n, up = cur.fetchone()
    cur.execute("""select count(*) from ocr_reading r
                   where r.method='mistral-ocr-4-1'
                     and not exists (select 1 from page_image p
                                     where p.page_id = r.page_id)""")
    missing = cur.fetchone()[0]
    print("page_image rows      : %d  (uploaded=%d)" % (n, up))
    print("Mistral pages w/o img: %d" % missing)
    con.close()


if __name__ == "__main__":
    main()

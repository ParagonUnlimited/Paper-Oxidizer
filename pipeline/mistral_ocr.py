# /// script
# requires-python = ">=3.10"
# dependencies = ["mistralai>=2.8,<3", "python-dotenv", "truststore>=0.9"]
# ///
"""Mistral OCR runner -- the approved single-pass pipeline.

    uv run mistral_ocr.py <file-or-folder> --model mistral-ocr-4-1
    uv run mistral_ocr.py "genius scan v2 from google drive" --model mistral-ocr-4-0 --limit 5

One ocr.process call per file carries EVERYTHING: markdown text, typed blocks
with paragraph bounding boxes, per-word confidence, header/footer separation,
document annotation (segmentation + identity + granular dates) and per-image
bbox annotation. Settings are the sheet Alden approved on 2026-08-12; anything
not exposed as a flag below is deliberately fixed.

--model is REQUIRED on purpose: the 4-0 vs 4-1 pin is decided by benchmark, and
requiring it here means no run can silently ride `-latest` onto a different
model mid-corpus.

Resumable by design: a file whose .raw.json already exists is skipped (--force
overrides), the manifest is appended per file, and every network call retries
with backoff -- assume the connection dies mid-run; a partial run is a success.

Outputs, per input file, under --out (default ocr-mistral/ next to this script):
    <stem>.raw.json    the COMPLETE API response, verbatim, written first
    <stem>.md          derived per-page markdown (header/footer labeled)
    _manifest.jsonl    one line per processed file: hash, pages, confidence,
                       duration -- the crash-safe progress record
"""
import argparse, base64, hashlib, io, json, os, re, sys, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv
# mistralai >=2 is a namespace package; the client lives one level down
# Cloudflare Gateway inspects TLS on this workstation, so the chain ends in the
# WARP CA. That CA is in the Windows certificate store but Python ships its own
# bundle (certifi) and never looks there -- hence
# "self-signed certificate in certificate chain". truststore makes Python use the
# OS store instead, which already trusts it. Must run before any client is built.
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

from mistralai.client import Mistral

HERE = os.path.dirname(os.path.abspath(__file__))
BASE64_CEILING = 6 * 1024 * 1024          # above this, upload + signed URL
PRICE_PER_1K_ANNOTATED = 5.0              # USD, verified from the model card

SCHEMA_DOC = os.path.join(HERE, "mistral-annotation-schema.json")
SCHEMA_IMG = os.path.join(HERE, "mistral-image-annotation-schema.json")
PROMPTS_MD = os.path.join(HERE, "mistral-annotation-prompts.md")

write_lock = threading.Lock()


def load_key():
    """Work-folder .env first, then the paperless-ocr-ingestion project's."""
    for env in (os.path.join(HERE, ".env"),
                r"C:\Users\busin\python-uv\Ministral\paperless-ocr-ingestion\.env"):
        if os.path.exists(env):
            load_dotenv(env, override=False)
    key = os.environ.get("MISTRAL_API_KEY")
    if not key:
        sys.exit("MISTRAL_API_KEY not found in either .env")
    return key


def load_annotation_assets():
    doc_schema = json.load(io.open(SCHEMA_DOC, encoding="utf-8"))
    img_schema = json.load(io.open(SCHEMA_IMG, encoding="utf-8"))
    text = io.open(PROMPTS_MD, encoding="utf-8").read()
    # the document prompt is the body of the first "## Annotation Prompt" section;
    # match loosely -- exact-heading matching silently lost 28 pages once
    m = re.search(r"^##[^\n]*Annotation Prompt[^\n]*\n(.*?)(?=^## |\Z)",
                  text, re.M | re.S)
    if not m or len(m.group(1).strip()) < 100:
        sys.exit("could not extract the annotation prompt from %s" % PROMPTS_MD)
    return doc_schema, img_schema, m.group(1).strip()


def rf(name, schema):
    """ResponseFormat wrapper, shape verified from the installed SDK:
    json_schema.schema_definition serializes to `schema` on the wire."""
    return {"type": "json_schema",
            "json_schema": {"name": name, "schema_definition": schema,
                            "strict": True}}


def document_arg(client, path):
    size = os.path.getsize(path)
    if size <= BASE64_CEILING:
        b64 = base64.b64encode(open(path, "rb").read()).decode()
        return {"type": "document_url",
                "document_url": "data:application/pdf;base64," + b64}
    up = client.files.upload(
        file={"file_name": os.path.basename(path), "content": open(path, "rb")},
        purpose="ocr")
    return {"type": "document_url",
            "document_url": client.files.get_signed_url(file_id=up.id).url}


def call_with_retry(fn, what, tries=5):
    delay = 2
    for attempt in range(1, tries + 1):
        try:
            return fn()
        except Exception as e:                                  # noqa: BLE001
            msg = str(e)
            transient = any(t in msg.lower() for t in
                            ("429", "rate", "timeout", "connection", "reset",
                             "502", "503", "504", "temporarily"))
            if attempt == tries or not transient:
                raise
            sys.stderr.write("  retry %d/%d for %s in %ds (%s)\n"
                             % (attempt, tries, what, delay, msg[:80]))
            time.sleep(delay)
            delay = min(delay * 2, 60)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def derive_markdown(raw):
    out = []
    for p in raw.get("pages") or []:
        out.append("## page %s" % p.get("index", "?"))
        if p.get("header"):
            out.append("**[header]** " + p["header"])
        out.append(p.get("markdown", ""))
        if p.get("footer"):
            out.append("**[footer]** " + p["footer"])
    return "\n\n".join(out)


def confidence_summary(raw):
    mins, avgs = [], []
    for p in raw.get("pages") or []:
        cs = p.get("confidence_scores") or {}
        if cs.get("minimum_page_confidence_score") is not None:
            mins.append(cs["minimum_page_confidence_score"])
        if cs.get("average_page_confidence_score") is not None:
            avgs.append(cs["average_page_confidence_score"])
    return (min(mins) if mins else None,
            sum(avgs) / len(avgs) if avgs else None)


def process_one(client, path, args, assets):
    doc_schema, img_schema, prompt = assets
    stem = os.path.splitext(os.path.basename(path))[0]
    raw_path = os.path.join(args.out, stem + ".raw.json")
    if os.path.exists(raw_path) and not args.force:
        return {"file": os.path.basename(path), "skipped": True}

    t0 = time.time()
    kwargs = dict(
        model=args.model,
        document=call_with_retry(lambda: document_arg(client, path),
                                 "upload " + stem),
        table_format="html",
        extract_header=True,
        extract_footer=True,
        include_blocks=True,
        include_image_base64=args.include_images,
        confidence_scores_granularity="word",
    )
    if not args.no_annotations:
        kwargs["document_annotation_format"] = rf("document_extraction", doc_schema)
        kwargs["document_annotation_prompt"] = prompt
        kwargs["bbox_annotation_format"] = rf("image_region", img_schema)
    if args.image_min_size:
        kwargs["image_min_size"] = args.image_min_size
    if args.image_limit:
        kwargs["image_limit"] = args.image_limit

    resp = call_with_retry(lambda: client.ocr.process(**kwargs), "ocr " + stem)
    raw = resp.model_dump()

    # RAW FIRST -- everything after this line is optional decoration
    with io.open(raw_path, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=1)
    try:
        io.open(os.path.join(args.out, stem + ".md"), "w",
                encoding="utf-8").write(derive_markdown(raw))
    except Exception as e:                                      # noqa: BLE001
        sys.stderr.write("  md derivation failed for %s (%s) -- raw intact\n"
                         % (stem, e))

    min_conf, avg_conf = confidence_summary(raw)
    entry = {
        "file": os.path.basename(path),
        "sha256": sha256(path),
        "model": raw.get("model"),
        "pages": (raw.get("usage_info") or {}).get("pages_processed"),
        "min_page_confidence": min_conf,
        "avg_page_confidence": avg_conf,
        "has_document_annotation": bool(raw.get("document_annotation")),
        "n_blocks": sum(len(p.get("blocks") or []) for p in raw.get("pages") or []),
        "n_images": sum(len(p.get("images") or []) for p in raw.get("pages") or []),
        "duration_s": round(time.time() - t0, 1),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with write_lock:
        with io.open(os.path.join(args.out, "_manifest.jsonl"), "a",
                     encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="a PDF, or a folder of PDFs")
    ap.add_argument("--model", required=True,
                    help="explicit model id, e.g. mistral-ocr-4-1 (no default on purpose)")
    ap.add_argument("--out", default=os.path.join(HERE, "ocr-mistral"))
    ap.add_argument("--force", action="store_true", help="reprocess existing")
    ap.add_argument("--limit", type=int, default=0, help="stop after N files")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--include-images", action="store_true",
                    help="return image crops in the response (bigger raw files)")
    ap.add_argument("--image-min-size", type=int, default=0)
    ap.add_argument("--image-limit", type=int, default=0)
    ap.add_argument("--no-annotations", action="store_true",
                    help="plain OCR only, skip both annotation formats")
    args = ap.parse_args()

    client = Mistral(api_key=load_key())
    assets = load_annotation_assets()
    os.makedirs(args.out, exist_ok=True)

    if os.path.isdir(args.target):
        files = sorted(os.path.join(args.target, f)
                       for f in os.listdir(args.target)
                       if f.lower().endswith(".pdf"))
        others = [f for f in os.listdir(args.target)
                  if not f.lower().endswith(".pdf")
                  and os.path.isfile(os.path.join(args.target, f))]
        if others:
            print("NOTE: %d non-PDF files in folder are NOT processed: %s%s"
                  % (len(others), ", ".join(others[:5]),
                     " ..." if len(others) > 5 else ""))
    else:
        files = [args.target]
    if args.limit:
        files = files[:args.limit]
    if not files:
        sys.exit("nothing to process")

    print("model=%s  files=%d  out=%s" % (args.model, len(files), args.out))
    done = skipped = failed = pages = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(process_one, client, p, args, assets): p
                   for p in files}
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                r = fut.result()
            except Exception as e:                              # noqa: BLE001
                failed += 1
                sys.stderr.write("FAILED %s: %s\n" % (os.path.basename(p),
                                                      str(e)[:200]))
                with write_lock:
                    with io.open(os.path.join(args.out, "_manifest.jsonl"),
                                 "a", encoding="utf-8") as f:
                        f.write(json.dumps({"file": os.path.basename(p),
                                            "error": str(e)[:300]}) + "\n")
                continue
            if r.get("skipped"):
                skipped += 1
            else:
                done += 1
                pages += r.get("pages") or 0
                print("ok  %-55s %2sp  min_conf=%s  %.0fs"
                      % (r["file"][:55], r["pages"],
                         ("%.2f" % r["min_page_confidence"])
                         if r["min_page_confidence"] is not None else "-",
                         r["duration_s"]))
    print("\ndone=%d skipped=%d failed=%d  pages=%d  est cost=$%.2f"
          % (done, skipped, failed, pages,
             pages / 1000 * PRICE_PER_1K_ANNOTATED))


if __name__ == "__main__":
    main()

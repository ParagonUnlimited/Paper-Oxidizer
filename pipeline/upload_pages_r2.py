# /// script
# requires-python = ">=3.10"
# dependencies = ["boto3>=1.34"]
# ///
"""Upload the rendered page JPEGs to Cloudflare R2.

    uv run upload_pages_r2.py --src <pages-r2> --dry
    uv run upload_pages_r2.py --src <pages-r2>

Needs, in the environment:
    R2_BUCKET  R2_ENDPOINT  R2_ACCESS_KEY_ID  R2_SECRET_ACCESS_KEY

THE BUCKET STAYS PRIVATE. These are probate documents -- bank statements, an
EIN letter, a creditor's claim against the estate. Nothing here makes the bucket
public; the app hands the browser a short-lived signed URL instead. If you ever
do attach a public custom domain to this bucket, that decision should be made
deliberately and not inherited from a script.

Resumable: an object already present at the same size is skipped, so an
interrupted run costs only the time to re-list.
"""
import argparse, os, sys, time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--prefix", default=os.environ.get("R2_PREFIX", "pages"))
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    for var in ("R2_BUCKET", "R2_ENDPOINT", "R2_ACCESS_KEY_ID",
                "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            sys.exit("%s is not set" % var)

    bucket = os.environ["R2_BUCKET"]
    s3 = boto3.client("s3",
                      endpoint_url=os.environ["R2_ENDPOINT"],
                      aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                      aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
                      region_name="auto",
                      config=Config(signature_version="s3v4",
                                    retries={"max_attempts": 5, "mode": "standard"}))

    files = sorted(f for f in os.listdir(args.src) if f.lower().endswith(".jpg"))
    print("local JPEGs : %d" % len(files))
    print("bucket      : %s   prefix: %s" % (bucket, args.prefix))

    # One listing beats one HEAD per object -- 1,762 round trips becomes ~2.
    existing = {}
    token = None
    while True:
        kw = {"Bucket": bucket, "Prefix": args.prefix.strip("/") + "/"}
        if token:
            kw["ContinuationToken"] = token
        try:
            resp = s3.list_objects_v2(**kw)
        except ClientError as e:
            sys.exit("cannot list bucket: %s" % e)
        for o in resp.get("Contents") or []:
            existing[o["Key"]] = o["Size"]
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    print("already in R2: %d" % len(existing))

    up = skip = fail = 0
    sent = 0
    t0 = time.time()
    for i, name in enumerate(files, 1):
        path = os.path.join(args.src, name)
        size = os.path.getsize(path)
        key = "%s/%s" % (args.prefix.strip("/"), name)
        if existing.get(key) == size:
            skip += 1
            continue
        if args.dry:
            up += 1
            continue
        try:
            s3.upload_file(path, bucket, key,
                           ExtraArgs={"ContentType": "image/jpeg",
                                      "CacheControl": "public, max-age=31536000, immutable"})
            up += 1
            sent += size
        except ClientError as e:
            fail += 1
            print("  FAILED %s: %s" % (name, str(e)[:80]))
        if i % 200 == 0:
            print("  %d/%d  uploaded=%d skipped=%d  %.0f MB  %.0fs"
                  % (i, len(files), up, skip, sent / 1048576.0, time.time() - t0))

    print()
    print("uploaded : %d" % up)
    print("skipped  : %d" % skip)
    print("failed   : %d" % fail)
    print("sent     : %.1f MB in %.0fs" % (sent / 1048576.0, time.time() - t0))


if __name__ == "__main__":
    main()

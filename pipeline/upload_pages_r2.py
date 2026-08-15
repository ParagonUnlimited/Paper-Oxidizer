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
import argparse, hashlib, json, os, sys, time, urllib.request

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID") or "68cc04bc26e145bfaf919bd02eb787d8"

# Where a .env may be sitting. A credential added to a .env file is not visible
# to a process that did not load it, which looks exactly like "the credential is
# wrong" -- so load them explicitly rather than leaving that to chance.
DOTENV_PATHS = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"),
    r"C:\Users\busin\Documents\Document Splitting for Paperless\.env",
]


def load_dotenv():
    """Merge .env values into os.environ WITHOUT overwriting anything already
    set, and without echoing any value. Reports only which file and which KEY
    NAMES were picked up."""
    found = []
    for path in DOTENV_PATHS:
        if not os.path.isfile(path):
            continue
        names = []
        try:
            with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    k = k.strip()
                    if k.lower().startswith("export "):
                        k = k[7:].strip()
                    v = v.strip().strip('"').strip("'")
                    if k and k not in os.environ:
                        os.environ[k] = v
                        names.append(k)
        except OSError:
            continue
        if names:
            found.append((path, names))
    return found


def resolve_credentials():
    """Return (access_key_id, secret_access_key, how) without ever printing them.

    R2's S3 API needs a key PAIR, but a Cloudflare "R2 API token" can be handed
    to you in several shapes depending on where you copied it from. Rather than
    make the operator normalise it by hand -- and rather than have anything read
    the value back out to a human or a log -- this resolves whichever shape is
    present:

      1. R2_ACCESS_KEY_ID + R2_SECRET_ACCESS_KEY   explicit pair, used as-is
      2. CF_R2_API = "<keyid>:<secret>"            colon-joined pair
      3. CF_R2_API = {"accessKeyId":..,"secretAccessKey":..}   JSON blob
      4. CF_R2_API = "<cloudflare api token>"      a bare token: Cloudflare
         derives the S3 pair from it as access key id = the token's own id,
         secret = SHA-256 of the token value. The id is fetched from
         /user/tokens/verify, which is what that endpoint is for.

    Only the NAME of the branch taken is ever reported.
    """
    kid = os.environ.get("R2_ACCESS_KEY_ID")
    sec = os.environ.get("R2_SECRET_ACCESS_KEY")
    if kid and sec:
        return kid, sec, "R2_ACCESS_KEY_ID + R2_SECRET_ACCESS_KEY"

    raw = (os.environ.get("CF_R2_API") or "").strip()
    if not raw:
        sys.exit("No credentials found. Set CF_R2_API, or both "
                 "R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY.")

    if raw.startswith("{"):
        try:
            d = json.loads(raw)
        except ValueError:
            sys.exit("CF_R2_API looks like JSON but does not parse.")
        k = d.get("accessKeyId") or d.get("access_key_id")
        s = d.get("secretAccessKey") or d.get("secret_access_key")
        if not (k and s):
            sys.exit("CF_R2_API JSON is missing accessKeyId/secretAccessKey.")
        return k, s, "CF_R2_API (JSON pair)"

    if ":" in raw and "://" not in raw:
        k, _, s = raw.partition(":")
        k, s = k.strip(), s.strip()
        if k and s:
            return k, s, "CF_R2_API (keyid:secret)"

    # Bare Cloudflare API token -> derive the S3 pair.
    req = urllib.request.Request(
        "https://api.cloudflare.com/client/v4/user/tokens/verify",
        headers={"Authorization": "Bearer %s" % raw})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.load(r)
    except Exception as e:                                    # noqa: BLE001
        sys.exit("CF_R2_API is not a keyid:secret pair, and verifying it as a "
                 "Cloudflare API token failed: %s" % str(e)[:120])
    if not body.get("success"):
        sys.exit("Cloudflare rejected CF_R2_API as an API token: %s"
                 % json.dumps(body.get("errors"))[:160])
    token_id = (body.get("result") or {}).get("id")
    if not token_id:
        sys.exit("Token verified but carried no id; cannot derive an S3 key.")
    return (token_id,
            hashlib.sha256(raw.encode()).hexdigest(),
            "CF_R2_API (bare API token, S3 pair derived)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--bucket", default="dobbins-paperless-scans")
    ap.add_argument("--prefix", default=os.environ.get("R2_PREFIX", "pages"))
    ap.add_argument("--ext", default="jpg",
                    help="file extension to upload (jpg or pdf)")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    for path, names in load_dotenv():
        print("loaded %d var(s) from %s: %s" % (len(names), path, ", ".join(names)))
    kid, sec, how = resolve_credentials()
    bucket = os.environ.get("R2_BUCKET") or args.bucket
    endpoint = (os.environ.get("R2_ENDPOINT")
                or "https://%s.r2.cloudflarestorage.com" % ACCOUNT_ID)
    print("credentials : %s" % how)
    print("endpoint    : %s" % endpoint)

    s3 = boto3.client("s3",
                      endpoint_url=endpoint,
                      aws_access_key_id=kid,
                      aws_secret_access_key=sec,
                      region_name="auto",
                      config=Config(signature_version="s3v4",
                                    retries={"max_attempts": 5, "mode": "standard"}))

    ext = "." + args.ext.lower().lstrip(".")
    ctype = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
             "pdf": "application/pdf"}.get(ext[1:], "application/octet-stream")
    files = sorted(f for f in os.listdir(args.src) if f.lower().endswith(ext))
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
                           ExtraArgs={"ContentType": ctype,
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

# /// script
# requires-python = ">=3.10"
# dependencies = ["psycopg[binary]>=3.1", "pymupdf>=1.24", "boto3>=1.34"]
# ///
"""Coolify passes every variable named in .env, including optional ones left
blank. Prove the app handles EMPTY STRING the same as ABSENT -- that is the
difference between a working deploy and 1,762 images silently 404ing.
"""
import importlib.util, os, sys

APP = r"C:\Users\busin\Documents\Paper-Oxidizer\.claude\worktrees\genius-extract-and-app\review\ocr_review_app.py"
os.chdir(os.path.dirname(APP))

ok = fail = 0
def check(label, cond, detail=""):
    global ok, fail
    if cond: ok += 1; print("  PASS  %s" % label)
    else:    fail += 1; print("  FAIL  %s   %s" % (label, detail))

def load(env):
    for k in ("R2_PREFIX","R2_SIGN_TTL","R2_BUCKET","R2_ENDPOINT",
              "R2_ACCESS_KEY_ID","R2_SECRET_ACCESS_KEY","REVIEW_USERS",
              "SESSION_SECRET","PAGE_SOURCE"):
        os.environ.pop(k, None)
    os.environ.update(env)
    os.environ["HOST"] = "127.0.0.1"
    spec = importlib.util.spec_from_file_location("app_%d" % len(env), APP)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m

FULL_R2 = {
    "R2_BUCKET": "dobbins-paperless-scans",
    "R2_ENDPOINT": "https://68cc04bc26e145bfaf919bd02eb787d8.r2.cloudflarestorage.com",
    "R2_ACCESS_KEY_ID": "k", "R2_SECRET_ACCESS_KEY": "s",
    "REVIEW_USERS": "alden:pw", "SESSION_SECRET": "sec",
}

print("=" * 62)
print("EMPTY-STRING ENV (what Coolify actually sends for blank optionals)")
m = load(dict(FULL_R2, R2_PREFIX="", R2_SIGN_TTL=""))
check("R2_PREFIX falls back to 'pages'", m.R2_PREFIX == "pages", repr(m.R2_PREFIX))
check("R2_SIGN_TTL falls back to 3600", m.R2_SIGN_TTL == 3600, m.R2_SIGN_TTL)
url = m.r2_url(792)
check("signed key is pages/792.jpg", "/dobbins-paperless-scans/pages/792.jpg" in url,
      url.split("?")[0])

print()
print("=" * 62)
print("ABSENT ENV (optionals not set at all)")
m2 = load(dict(FULL_R2))
check("R2_PREFIX defaults to 'pages'", m2.R2_PREFIX == "pages", repr(m2.R2_PREFIX))
check("R2_SIGN_TTL defaults to 3600", m2.R2_SIGN_TTL == 3600, m2.R2_SIGN_TTL)

print()
print("=" * 62)
print("EXPLICIT PREFIX still honoured")
m3 = load(dict(FULL_R2, R2_PREFIX="custom"))
check("R2_PREFIX == 'custom'", m3.R2_PREFIX == "custom", repr(m3.R2_PREFIX))
check("key uses it", "/custom/792.jpg" in m3.r2_url(792), m3.r2_url(792).split("?")[0])

print()
print("PASS %d   FAIL %d" % (ok, fail))
sys.exit(1 if fail else 0)

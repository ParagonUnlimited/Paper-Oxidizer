# Review this change for security vulnerabilities.

- **Session:** `2f7dd2af-b96f-4531-9dc4-1d8dc4d73ced`
- **Span:** 2026-08-14T23:34:56.049Z → 2026-08-14T23:36:20.571Z
- **Messages:** 1 user · 7 assistant
- **Source:** raw JSONL kept locally in `_session-recovery-backup/` (not in git — contains credentials). This file is the scrubbed conversation: all prose, tool calls as one-line markers, tool results omitted.

---

## 👤 User

Review this change for security vulnerabilities.

Changed files (you may Read these and any other file in the repo):
  - review/docker-compose.yml
  - review/ocr_review_app.py
  - review/smoke_test.py

Unified diff (only + lines are new):

=== DIFF: review/docker-compose.yml ===
@@ -1,5 +1,27 @@
-# Coolify reads this directly. Every value comes from the environment, so no
-# secret is ever written into the repo.
+# OCR review app.
+#
+# EVERY SECRET IS AN ENVIRONMENT VARIABLE. Nothing is baked into the image and
+# nothing is committed to this repo. The ${NAME} form below is standard Compose
+# interpolation: at deploy time each one is replaced by the value of that
+# variable in the environment Coolify runs Compose in. So the names you set in
+# Coolify must match the names in ${...} exactly -- they are the contract.
+#
+# Required (the container refuses to start without them):
+#   NEON_DATABASE_URL       postgres connection string
+#   REVIEW_USERS            alden:somepassword,jeff:otherpassword
+#   SESSION_SECRET          any long random string; signs the login cookie
+#
+# Page images -- set ALL FOUR or NONE. A partial set is rejected at startup
+# rather than silently falling back to a local render that cannot work in a
+# container:
+#   R2_BUCKET
+#   R2_ENDPOINT             https://<cloudflare-account-id>.r2.cloudflarestorage.com
+#   R2_ACCESS_KEY_ID
+#   R2_SECRET_ACCESS_KEY
+#
+# Optional:
+#   R2_PREFIX               key prefix, default "pages"
+#   R2_SIGN_TTL             signed-URL lifetime in seconds, default 3600
 services:
   ocr-review:
     build:
@@ -11,19 +33,25 @@ services:
     environment:
       # --- required ---
       NEON_DATABASE_URL: ${NEON_DATABASE_URL}
-      # "name:password,name:password" -- the name becomes part of the
-      # ocr_reading method, so each reviewer's corrections stay theirs.
       REVIEW_USERS: ${REVIEW_USERS}
-      # Signs the login cookie. Change it and everyone is logged out.
       SESSION_SECRET: ${SESSION_SECRET}
 
-      # --- page images from R2 (omit all five to fall back to local render) ---
+      # --- page images from R2: all four, or none ---
       R2_BUCKET: ${R2_BUCKET}
       R2_ENDPOINT: ${R2_ENDPOINT}
       R2_ACCESS_KEY_ID: ${R2_ACCESS_KEY_ID}
       R2_SECRET_ACCESS_KEY: ${R2_SECRET_ACCESS_KEY}
       R2_PREFIX: ${R2_PREFIX:-pages}
+      R2_SIGN_TTL: ${R2_SIGN_TTL:-3600}
 
+      # --- fixed for the container; no need to set these in Coolify ---
       HOST: 0.0.0.0
-      PORT: 8778
+      PORT: "8778"
       NO_BROWSER: "1"
+    healthcheck:
+      test: ["CMD", "python", "-c",
+             "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8778/healthz',timeout=4).status==200 else 1)"]
+      interval: 30s
+      timeout: 5s
+      retries: 3
+      start_period: 20s


=== DIFF: review/ocr_review_app.py ===
@@ -69,7 +69,20 @@ R2_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID") or ""
 R2_SECRET = [REDACTED]("R2_SECRET_ACCESS_KEY") or ""
 R2_PREFIX = os.environ.get("R2_PREFIX", "pages")
 R2_SIGN_TTL = int(os.environ.get("R2_SIGN_TTL") or 3600)
-USE_R2 = bool(R2_BUCKET and R2_ENDPOINT and R2_KEY_ID and R2_SECRET)
+
+# R2 is all-or-nothing. Treating a PARTIAL configuration as "R2 off" would send
+# the app down the local-render path, and in the container there are no PDFs to
+# render -- so every scan would come back 404 and the reviewer would see blank
+# panes with nothing in the log to explain why. One unpasted secret should not
+# look like a rendering bug.
+_R2_VARS = {"R2_BUCKET": R2_BUCKET, "R2_ENDPOINT": R2_ENDPOINT,
+            "R2_ACCESS_KEY_ID": [REDACTED], "R2_SECRET_ACCESS_KEY": [REDACTED]}
+_r2_missing = sorted(k for k, v in _R2_VARS.items() if not v)
+if _r2_missing and len(_r2_missing) != len(_R2_VARS):
+    sys.exit("REFUSING TO START: R2 is partly configured. Missing: %s\n"
+             "Set all four, or none of them to render locally instead."
+             % ", ".join(_r2_missing))
+USE_R2 = not _r2_missing
 
 HOST = os.environ.get("HOST") or "127.0.0.1"
 PORT = int(os.environ.get("PORT") or 8778)
@@ -138,6 +151,16 @@ if USERS and not os.environ.get("SESSION_SECRET"):
              "is set.\nWithout it the login cookie cannot be signed safely.")
 SECRET = (os.environ.get("SESSION_SECRET") or "loopback-solo-mode").encode()
 
+# There must be SOME way to show a scan. Without R2 and without a readable
+# recut/ folder, the app would start, log nothing unusual, serve the queue --
+# and then hand back 404 for every image. A reviewer would be looking at empty
+# panes wondering whether the documents were lost.
+if not USE_R2 and not os.path.isdir(RECUT):
+    sys.exit("REFUSING TO START: no page images available.\n"
+             "Either set the four R2_* variables, or point PAGE_SOURCE at a "
+             "folder of recut PDFs.\nPAGE_SOURCE currently resolves to: %r"
+             % RECUT)
+
 
 # ocr_reading carries a UNIQUE constraint on (page_id, method) --
 # ocr_reading_page_id_method_key. A single shared 'human-corrected' method


=== DIFF: review/smoke_test.py ===
@@ -157,6 +157,48 @@ run({"HOST": "0.0.0.0", "REVIEW_USERS": "alden:pw"},   # no SESSION_SECRET
     "REVIEW_USERS without SESSION_SECRET refuses to start",
     1, "SESSION_SECRET")
 
+# A half-pasted R2 secret must not look like a rendering bug. Without this the
+# app would fall back to local rendering, find no PDFs in the container, and
+# return 404 for every scan -- blank panes, nothing in the log.
+run({"HOST": "0.0.0.0", "REVIEW_USERS": "alden:pw", "SESSION_SECRET": "s",
+     "R2_BUCKET": "b"},
+    "partial R2 config refuses to start", 1, "partly configured")
+run({"HOST": "0.0.0.0", "REVIEW_USERS": "alden:pw", "SESSION_SECRET": "s",
+     "R2_BUCKET": "b", "R2_ENDPOINT": "https://x.example",
+     "R2_ACCESS_KEY_ID": "k"},                       # secret missing
+    "R2 missing only the secret refuses to start", 1, "R2_SECRET_ACCESS_KEY")
+
+# No R2 and no readable recut/ means there is no way to show a page at all.
+run({"HOST": "0.0.0.0", "REVIEW_USERS": "alden:pw", "SESSION_SECRET": "s",
+     "PAGE_SOURCE": os.path.join(APP, "__no_such_folder__")},
+    "no image source at all refuses to start", 1, "no page images")
+
+# ...and the complete, correct configuration must still start and serve.
+print()
+print("=" * 66)
+print("D. FULLY CONFIGURED (R2 + auth) -- must start and serve")
+srv, app = boot(8893, {"REVIEW_USERS": "alden:pw-a", "SESSION_SECRET": "sec",
+                       "R2_BUCKET": "testbucket",
+                       "R2_ENDPOINT": "https://abc123.r2.cloudflarestorage.com",
+                       "R2_ACCESS_KEY_ID": "[REDACTED]",
+                       "R2_SECRET_ACCESS_KEY": "[REDACTED]"})
+check("USE_R2 is on", app.USE_R2 is True, app.USE_R2)
+s, h, b = req(8893, "POST", "/login", "user=alden&pw=pw-a")
+ck = h.get("Set-Cookie", "").split(";")[0]
+check("login works with R2 configured", s == 302 and ck.startswith("rev=alden|"), s)
+s, h, b = req(8893, "GET", "/queue", cookie=ck)
+_q = _j.loads(b)
+s2, h2, b2 = req(8893, "GET", "/doc?id=%d" % _q[0]["id"], cookie=ck)
+_pid = _j.loads(b2)["pages"][0]["pageId"]
+s3, h3, b3 = req(8893, "GET", "/page.img?id=%d" % _pid, cookie=ck)
+loc = h3.get("Location", "")
+check("/page.img 302-redirects to a signed R2 URL",
+      s3 == 302 and "X-Amz-Signature" in loc and ("/pages/%d.jpg" % _pid) in loc,
+      "%s %r" % (s3, loc[:90]))
+s4, h4, b4 = req(8893, "GET", "/page.img?id=%d" % _pid)
+check("page image still requires login", s4 == 401, s4)
+srv.shutdown()
+
 print()
 print("=" * 66)
 print("PASS %d   FAIL %d" % (ok, fail))


Investigate per the method in your instructions, then return the findings list.

---

## 🤖 Claude

> 🔧 `Read` — review/ocr_review_app.py

---

## 🤖 Claude

> 🔧 `Read` — review/docker-compose.yml

---

## 🤖 Claude

> 🔧 `Read` — review/smoke_test.py

---

## 🤖 Claude

Looking at the diff, I need to trace each change for security impact.

---

## 🤖 Claude

> 🔧 `Grep` — page.img|page.png|r2_url|USE_R2

---

## 🤖 Claude

I've read all three changed files in full and traced the changes. My analysis:

**Change 1: R2 all-or-nothing validation (`ocr_review_app.py` lines 78–85)** — A fail-closed hardening. `_r2_missing` is derived by set membership; `USE_R2 = not _r2_missing` only becomes True when all four are present. All partial states hit `sys.exit`. No path yields fail-open. Error message contains only variable NAMES, not values — no secret exposure through logs/exit.

**Change 2: No-image-source check (lines 154–162)** — Another fail-closed guard placed after the SESSION_SECRET check. Only exits, never permits fewer controls. `RECUT` is echoed but is a filesystem path derived from `PAGE_SOURCE` env (operator-controlled), not attacker input.

**Change 3: docker-compose additions** —
- Healthcheck uses list-form CMD (no shell), loopback URL, bounded timeout — no injection or SSRF surface.
- `HOST: 0.0.0.0`, `PORT: "8778"`, `NO_BROWSER: "1"` are just moves of previously-implicit values into the compose file; the pre-existing "REFUSING TO START if HOST is non-loopback and no REVIEW_USERS" gate still fires.
- `/healthz` is unauthenticated but only returns `{"ok":true}` — no sensitive content.
- `R2_SIGN_TTL: ${R2_SIGN_TTL:-3600}` exposes an existing knob to operators; not attacker-influenceable.

**Change 4: smoke_test.py** — Test-only file; not a runtime attack surface. "AKIATEST"/"secrettest" strings are labeled test values.

**Pre-existing issues NOT touched by this diff (per instructions, do not flag):**
- `/page.img` has no per-document ACL — any authenticated reviewer can fetch any page's signed URL.
- `page_png` `".." in pdf_name` check is weak against absolute paths.
- `_redirect` sets no Cache-Control (signed R2 URL could linger in intermediate caches).

None of the `+` lines introduce a new vulnerability, defeat an existing control, or add an under-validated sink argument. All three modifications strictly tighten startup validation.

---

## 🤖 Claude

> 🔧 `StructuredOutput`

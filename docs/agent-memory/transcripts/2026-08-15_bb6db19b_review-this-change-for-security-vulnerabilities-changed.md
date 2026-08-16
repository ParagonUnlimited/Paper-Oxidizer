# Review this change for security vulnerabilities.

- **Session:** `bb6db19b-9c9f-44c6-8294-7f3a50c4beaa`
- **Span:** 2026-08-15T08:27:05.809Z → 2026-08-15T08:28:21.431Z
- **Messages:** 1 user · 7 assistant
- **Source:** raw JSONL kept locally in `_session-recovery-backup/` (not in git — contains credentials). This file is the scrubbed conversation: all prose, tool calls as one-line markers, tool results omitted.

---

## 👤 User

Review this change for security vulnerabilities.

Changed files (you may Read these and any other file in the repo):
  - docker-compose.yml

Unified diff (only + lines are new):

=== DIFF: docker-compose.yml ===
@@ -60,6 +60,15 @@ services:
       HOST: 0.0.0.0
       PORT: "8778"
       NO_BROWSER: "1"
+      # Coolify generates a SERVICE_* value ONLY when the compose references it
+      # -- typing the name into the env list does nothing. SERVICE_HEX_ gives 32
+      # hex chars, generated once and persisted across deploys.
+      #
+      # This one IS bound to the compose, and that is correct here: it is
+      # generated, never hand-edited, and rotating it only means logging
+      # everyone out. Credentials that DO get rotated by hand stay out of this
+      # file so Coolify keeps them editable.
+      SESSION_SECRET: ${SERVICE_HEX_SESSION}
     # Coolify writes the variables you set in its UI to a .env beside the
     # generated compose and loads them from there. Declaring it explicitly costs
     # nothing if Coolify already does it, and removes the doubt if it does not.


Investigate per the method in your instructions, then return the findings list.

---

## 🤖 Claude

> 🔧 `Read` — docker-compose.yml

---

## 🤖 Claude

> 🔧 `Glob` — review/**/*.py

---

## 🤖 Claude

> 🔧 `Glob` — review/**/*.{js,ts}

---

## 🤖 Claude

> 🔧 `Grep` — SESSION_SECRET

---

## 🤖 Claude

> 🔧 `Read` — review/ocr_review_app.py

---

## 🤖 Claude

Based on my analysis of the change:

**Diff summary:** Adds `SESSION_SECRET: ${SERVICE_HEX_SESSION}` to the `environment:` block of the `ocr-review` service. Coolify auto-generates 32 hex chars (128 bits) for `SERVICE_HEX_*` variables, persisted across deploys.

**Analysis:**

1. **Entropy of generated secret**: 32 hex chars = 128 bits. Sufficient for HMAC cookie signing.

2. **Fail-closed behavior when unset**: If `SERVICE_HEX_SESSION` is not set (e.g., local `docker compose up` without a `.env`), Docker Compose substitutes empty string. In `review/ocr_review_app.py:154-156`:
   ```python
   if USERS and not os.environ.get("SESSION_SECRET"):
       sys.exit("REFUSING TO START: SESSION_SECRET is required when REVIEW_USERS is set.")
   ```
   Empty string is falsy → app exits. Fail-closed guard intact.

3. **Solo-mode fallback**: If neither `REVIEW_USERS` nor `SESSION_SECRET` is set, `SECRET` defaults to the hardcoded `"loopback-solo-mode"` string (line 157). This is safe because solo mode requires `HOST` to be loopback (line 141-145), and the socket is unreachable remotely.

4. **No new sinks or entry points**: The change only wires an environment variable substitution. No shell exec, no path traversal, no SQL, no HTTP.

5. **No control regression**: No `-` lines delete any validator; the app's existing fail-closed guard still applies.

6. **Documentation inconsistency but not a vuln**: Line 52 still tells operators to set `SESSION_SECRET` in the Coolify UI, but the compose now binds it. The `environment:` block wins over any UI-set value, so users will be logged out on first deploy under the new config. That's operational, not a security defect.

No security vulnerabilities introduced by this diff.

---

## 🤖 Claude

> 🔧 `StructuredOutput`

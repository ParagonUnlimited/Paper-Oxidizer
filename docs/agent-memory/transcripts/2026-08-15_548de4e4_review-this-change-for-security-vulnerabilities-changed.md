# Review this change for security vulnerabilities.

- **Session:** `548de4e4-84fe-4e1e-a252-e58a3f0d6a2a`
- **Span:** 2026-08-15T06:04:22.555Z → 2026-08-15T06:04:41.255Z
- **Messages:** 1 user · 3 assistant
- **Source:** raw JSONL kept locally in `_session-recovery-backup/` (not in git — contains credentials). This file is the scrubbed conversation: all prose, tool calls as one-line markers, tool results omitted.

---

## 👤 User

Review this change for security vulnerabilities.

Changed files (you may Read these and any other file in the repo):
  - docker-compose.yml

Unified diff (only + lines are new):

=== DIFF: docker-compose.yml ===
@@ -30,8 +30,15 @@ services:
       context: ./review
       dockerfile: Dockerfile
     restart: unless-stopped
-    ports:
-      - "8778:8778"
+    # EXPOSE, NOT PORTS. `ports: 8778:8778` publishes straight onto the VPS
+    # interface and bypasses Coolify's proxy -- the app would answer on plain
+    # http://<vps-ip>:8778 with no TLS in front. The session cookie is marked
+    # Secure off-loopback, so login would also fail there in a way that looks
+    # like a bug rather than a misconfiguration. `expose` keeps the port inside
+    # Coolify's network; assign the domain in Coolify and its proxy terminates
+    # TLS and routes to 8778.
+    expose:
+      - "8778"
     environment:
       # --- required ---
       NEON_DATABASE_URL: ${NEON_DATABASE_URL}


Investigate per the method in your instructions, then return the findings list.

---

## 🤖 Claude

> 🔧 `Read` — docker-compose.yml

---

## 🤖 Claude

I've read the only changed file (`docker-compose.yml`) in full. Analysis:

**What the change does:** Replaces `ports: 8778:8778` (which binds the container port directly to the VPS host interface at `0.0.0.0:8778`) with `expose: 8778` (which only advertises the port on Docker's internal network for the Coolify reverse proxy to reach).

**Security impact:** This is a hardening change, not a regression.
- Before: The app was reachable at plain `http://<vps-ip>:8778` with no TLS, bypassing Coolify's proxy. Session cookies marked `Secure` would be dropped on that plain-HTTP path, so login would silently fail — but the app itself, including any pre-auth endpoints (`/healthz`, static routes, etc.), was still directly exposed.
- After: Traffic must traverse Coolify's proxy, which terminates TLS and routes to the container over the internal Docker network.

**Checks performed against the method:**
- Entry points / sinks: none added. The only "sink"-adjacent change is a network-exposure narrowing.
- Sibling-path gate parity: single service, no siblings that publish ports without TLS.
- Sensitive-to-observability: no logging added.
- Allowlist / grant / registry fanout: N/A.
- Fail-open state drift: N/A — the healthcheck still uses loopback `127.0.0.1:8778`, which continues to work with `expose` (loopback is inside the container namespace, unaffected by publish vs expose).
- Under-validated sink arg / control regression: none.
- Environment variables (`NEON_DATABASE_URL`, `REVIEW_USERS`, `SESSION_SECRET`, R2 keys) are unchanged in this diff — no new secret handling.

No source→sink path with an unmitigated risk is introduced by these lines.

---

## 🤖 Claude

> 🔧 `StructuredOutput`

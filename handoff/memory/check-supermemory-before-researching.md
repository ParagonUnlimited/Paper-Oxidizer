---
name: check-supermemory-before-researching
description: "Search supermemory for infra conventions before web-researching them — Alden's stack conventions are already recorded there"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 53e248f2-5450-46c4-8bb6-3e5acf09fb6f
  modified: 2026-08-15T07:39:51.850Z
---

Before researching how something works on Alden's infrastructure — Coolify,
Cloudflare Access, Neon, Papra, Hermes — search supermemory first. The session's
auto-injected context carries project facts but not the operational conventions;
those only surface on an explicit `search_memory` call.

**Why:** On 2026-08-15 I hardcoded `${VAR}` secrets into a `docker-compose.yml`,
which made every one of them read-only in Coolify's UI. Then I web-searched to
diagnose it. Supermemory already held the answer: *"values are stored in Coolify
UI, not in the docker-compose"* and *"Environment variables used by the Hermes
compose are stored in Coolify UI."* One search before writing the file would have
prevented the bug and the two rounds spent fixing it.

His words: *"Why are you researching again, look in supermemory."*

**How to apply:** For any task touching his deployed stack, run
`search_memory` before writing config or reaching for the web. Web docs are for
vendor behaviour that is genuinely not recorded yet — not for how *this* stack is
set up.

Related: [[do-not-second-guess-stated-decisions]]

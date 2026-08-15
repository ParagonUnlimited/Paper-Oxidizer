---
name: do-not-second-guess-stated-decisions
description: "When Alden states a decision or describes what he is doing, act on it — do not re-argue it or warn against something he never proposed"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 53e248f2-5450-46c4-8bb6-3e5acf09fb6f
  modified: 2026-08-15T07:39:45.349Z
---

When Alden states a decision, or describes what he is currently doing, take it at
face value and answer the actual question. Do not re-open the decision, and do
not warn him against a mistake he has not made.

**Why:** On 2026-08-15 this cost him hours across one session. Two representative
cases:

- He said to commit a `.env` "with UNSET VALUES". I lectured him about not
  committing secrets to git — a position he never held — instead of committing
  the empty-valued file he asked for.
- He said "database: paperless and role: paperless?" while picking a database in
  the **Neon console** to generate a connection string. I assumed he was creating
  a Postgres in Coolify and told him to stop. He was doing nothing of the kind.

His words: *"stop second guessing me full stop, never do it again."*

**How to apply:** Answer what was asked, in as few words as it takes. If a real
risk exists, state it in one sentence *after* the answer — never instead of it,
and never as a reason to withhold the work. If his message is ambiguous, the
reading where he already knows what he is doing is the correct one.

Related: [[check-supermemory-before-researching]]

# A9 — Connection pooling + Neon suspend

**Logged 2026-08-13. Wiki: `concepts/open-questions-register.md` A9. Related: A11 (region).**

Alden's question was: *"that 'problem' seems to contradict the primary feature and good things
Neon offers... once a connection wakes, it should scale up and down depending on demand, not
make a new connection every query."*

**That instinct is correct, and Neon is not the problem.** Here is the measured answer.

---

## Where the time actually goes

Measured from inside the Paperless container on hermes-vps, against the Neon endpoint:

| Operation | Cost | Round trips |
|---|---|---|
| Raw TCP handshake | **87 – 92 ms** | ~1 |
| TCP + TLS handshake | **153 – 186 ms** | ~2 |
| Full Postgres connect (TCP + TLS + auth + startup) | **447 – 475 ms** | ~5 |
| A query on an already-open connection | **70 – 150 ms** | ~1 |
| Pooled reuse (`PAPERLESS_DB_POOLSIZE=4`) | **66 ms** | ~1 |

The shape of it: **one round trip is ~90 ms**, and opening a Postgres connection costs about
five of them. So a connection costs roughly 5× what a query costs. None of that is Neon making
a choice — it is the Postgres wire protocol multiplied by physical distance.

## Why it bites Paperless specifically

Paperless does not expose `CONN_MAX_AGE`, so **Django's default of `0` applies**: Django closes
the database connection at the end of every request and opens a fresh one on the next request.

Against the local `db` container that was invisible — round-trip time was effectively zero, so
a connection cost nothing. Against anything remote it costs ~450 ms *per request*. This would be
identical against a remote AWS RDS, a remote self-hosted Postgres, or Neon. It is not a Neon
property.

## Neon's own pooler does NOT fix this — tested, not assumed

| Endpoint | Connect time |
|---|---|
| `...-pooler...` (PgBouncer) | 463 / 475 ms |
| direct | 447 ms |

No meaningful difference. Neon's documentation is explicit about what pooling is for:

- It solves **connection limits and resource exhaustion** — "each Postgres connection creates a
  new process in the operating system, consuming memory and CPU resources." PgBouncer in
  transaction mode raises the ceiling to ~10,000 client connections.
- It does **not** address connection *establishment* latency. The client still performs its own
  TCP and TLS handshake to the pooler on every new connection.

Neon's docs do list "connection-per-request frameworks" under *use the pooled endpoint* — but
that guidance is about not exhausting the connection ceiling, not about making each connection
faster.

**Separately: Neon's autoscaling and scale-to-zero are about compute *sizing*** — how much CPU
and memory the endpoint runs with, and whether it suspends when idle. That is a different
mechanism entirely, it works as advertised, and it is not implicated in this measurement.

## The only real fix is client-side

Stop opening a new connection per request — hold them open and reuse them.

```
PAPERLESS_DB_POOLSIZE=4
```

Verified working against Neon: v3.0.3 uses Django 5.1's native psycopg connection pool, and
`psycopg_pool 3.3.1` already ships inside the Paperless image. Resolved config reads
`{'min_size': 1, 'max_size': 4}`. Measured pooled reuse: **66 ms**.

## The coupling — why this is one decision, not two

Pooling holds `min_size: 1` connection open permanently. That will very likely keep the Neon
compute awake, which means turning pooling on also answers the scale-to-zero question.

| Option | Per-request cost | Neon compute |
|---|---|---|
| **A** — pooled, effectively always warm | ~70 ms | Higher; endpoint rarely suspends |
| **B** — unpooled, scale to zero | ~450 ms added to every request | Lowest; suspends when idle |

**Recommendation: A.** The point of moving to a real database backend was capability, and a
~450 ms tax on every page load undoes it.

## Footnote worth keeping

The endpoint reports `suspend_timeout_seconds: 0`. In Neon's API that means **default**
(~5 minutes), **not** "never" — an easy misread, and it matches the 1,131 ms first-connect
measured after an idle period versus ~450 ms warm.

## See also

**A11 — region.** That is the larger lever and sits upstream of this decision. Pooling removes
the connection setup cost, but **every query still pays ~90 ms of round trip**. A Django page
issuing 15 queries takes ~1.3 s no matter how good the pooling is.

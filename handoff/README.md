# handoff/ — session memory, history, and plans (2026-08-15 model switch)

Everything a fresh agent needs beyond the code. Read order for resuming:
root `STATE.md` → `PLAN-V2.md` → `ARCHITECTURE.md` → `TODO.md` → this folder.

| File | What it is |
|---|---|
| `approved-plan-v2.md` | The plan Alden approved in plan mode (research-backed: Turso/sqld facts, Rust stack, PDF/A toolchain, advisor-reviewed gaps) |
| `RECOVERY.md` | How the crashed 13.5h session was located and preserved (worktree transcript-filing gotcha) |
| `memory/` | Claude project-memory files: process rules (don't second-guess stated decisions; search supermemory before researching) and integration fact sheets (v2 Rust, Papra API, crash recovery) |
| `session-history-2026-08-15.md` | Timestamped session log distilled from .remember/ |

## Deliberately NOT in git

- **Raw session transcripts** (`_session-recovery-backup/*.jsonl`, ~7 MB) —
  they contain live credentials in plaintext (Neon connection string, secret
  values discussed in chat). Same risk class as `papra-primary (1).db`
  (gitignored: live auth_sessions/api_keys). They stay local; RECOVERY.md
  records where.
- **Corpus data** — lives in its homes: Neon (`neondb`) and R2
  (`dobbins-paperless-scans`). See STATE.md for exact locations and counts.
- **Secrets** — Coolify UI env only. `.env` in git is names-with-empty-values.

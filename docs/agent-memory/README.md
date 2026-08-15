# Agent memory + session history — snapshots (2026-08-15)

Committed copies of the AI agents' working context, so an agent (or human)
with only this repo inherits it. **Live copies stay in their homes** — these
are snapshots, refreshed at handoff points, and may lag:

| File | Live home | What it is |
|---|---|---|
| `MEMORY.md` + the four fact/feedback files | `~/.claude/projects/C--Users-busin-Documents-Paper-Oxidizer/memory/` | Durable agent memory: integration facts (Papra API, Rust/TLS), working-style feedback from Alden, crash-recovery notes |
| `session-history-2026-08-15.md` | `.remember/today-2026-08-15.md` (untracked) | Timestamped log of what each session actually did on the build day |
| `session-now-snapshot.md` | `.remember/now.md` (untracked) | The most recent session-buffer entry at snapshot time |

Longer-term shared memory lives in **supermemory** (searched via MCP) and the
**llm-wiki** (`C:\Users\busin\Documents\llm-wiki`) — both updated at the same
handoff points. The narrative handoff itself is [../../STATE.md](../../STATE.md).

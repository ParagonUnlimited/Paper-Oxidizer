# Session Recovery — Paper-Oxidizer (crash 2026-08-15)

## The lost session — FOUND, INTACT

- **Session ID:** `53e248f2-5450-46c4-8bb6-3e5acf09fb6f`
- **Recorded cwd:** `C:\Users\busin\Documents\Paper-Oxidizer`
- **Size:** 5.06 MB / 2,772 records / 384 user messages
- **Span:** 2026-08-14T20:33:37Z → 2026-08-15T10:11:33Z (~13.5 h)
- **Opening message:** "Check out the handoff and the stuff in this cloned repo, then
  summarize for me and I'll give you instruction..."
- **Parent session:** `local_fcf6fd99-...` "Paperless NGX document types and MCP"
  (cwd `C:\Users\busin\probate`, 50.24 MB, still present and healthy)

## Why `/resume` could not see it

The transcript is filed under the **worktree** project key, not the main one:

    ...\.claude\projects\C--Users-busin-Documents-Paper-Oxidizer--claude-worktrees-genius-extract-and-app\
      53e248f2-5450-46c4-8bb6-3e5acf09fb6f.jsonl

The session ran with cwd = the main `Paper-Oxidizer` directory but used git worktrees
during the multi-agent build, so its transcript landed in the worktree's project folder.
`claude --resume` launched from `C:\Users\busin\Documents\Paper-Oxidizer` scans
`C--Users-busin-Documents-Paper-Oxidizer\`, which holds only the post-crash session
`798122fd-...`. Hence "can't find it anywhere."

The worktree itself still exists:
`C:\Users\busin\Documents\Paper-Oxidizer\.claude\worktrees\genius-extract-and-app`
(branch `worktree-genius-extract-and-app`, commit 2672073).

## Backup

All 20 worktree transcripts (7.11 MB) copied to this folder, pre-resume, untouched.
`53e248f2-...jsonl` is among them. Resuming APPENDS to the live transcript, so this
backup is the rollback point.

## Recovery (verified against claude v2.1.233 `--help`)

- `-r, --resume [value]` — resume by session ID
- `--fork-session` — resume into a NEW session ID, leaving the original transcript untouched

Safest first attempt: fork, confirm the history is all there, then decide whether to
continue in the fork or resume the original in place.

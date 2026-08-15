---
name: session-crash-recovery
description: "Crashed Claude Code sessions are never lost — transcripts are on disk per project directory; git worktrees create duplicate session files that cause \"time travel\""
metadata: 
  node_type: memory
  type: reference
  originSessionId: 24693b5d-e5ac-481d-bbf2-e342c5e3dd1b
  modified: 2026-08-15T23:18:15.088Z
---

Alden's machine crashes fairly often and he loses Claude Code windows. Sessions are recoverable every time — the UI dies, the data doesn't.

Transcripts: `C:\Users\busin\.claude\projects\<cwd-slug>\<session-id>.jsonl`, appended per turn. Subagent transcripts sit in `<session-id>/subagents/`.

**The "time travel" symptom has a specific cause:** a session started in the repo root and later moved into a git worktree gets a *second* file under the worktree's own slug (e.g. `C--Users-busin-Documents-Paper-Oxidizer--claude-worktrees-genius-extract-and-app`). The repo-root copy is frozen at the moment of the move; the worktree copy is the live one. Resuming from the wrong directory loads the frozen copy — that's the "way back in time" session. Confirmed 2026-08-15 by uuid diff: repo-root copy was a strict prefix, zero unique entries.

**Recovery:** `cd` to the directory that owns the live copy, then `claude --resume <session-id>`. The launch directory decides which copy loads. `--fork-session` resumes into a new id, which is the safe move if another window may still be attached (check the file's mtime — if it changed in the last minute, a window is live).

Before advising on any of this, check whether the work is even at risk: `git status` in *both* the repo and every worktree from `git worktree list`.

**Two mistakes I made doing this on 2026-08-15 — don't repeat them.** (1) I searched only the current project's folder. Alden runs many projects at once, and his lost sessions were in *different* project dirs; always scan every folder under `.claude\projects\`. (2) One conversation forks into several session ids sharing an origin timestamp — I reported two forks of one Paper-Oxidizer conversation as two separate lost sessions. Diff by entry uuid before claiming two sessions are distinct; heavy uuid overlap means one lineage. Also: the folder-slug encoding is lossy (`Documentation-Knowledge-Vectors-Skills` is one folder, not three), so read the real path from the transcript's own `cwd` field rather than reconstructing it from the slug.

Worked example with full inventory: `.remember/session-recovery-2026-08-15.md` in Paper-Oxidizer (gitignored). See [[v2-rust-integration-facts]].

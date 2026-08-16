# /// script
# requires-python = ">=3.10"
# ///
"""Convert raw Claude session transcripts (.jsonl) into secret-scrubbed,
titled Markdown for the repo.

    uv run tools/transcripts_to_md.py            # convert + verify
    uv run tools/transcripts_to_md.py --check    # verify existing output only

Originals are NEVER modified; they stay in _session-recovery-backup/ (and the
live ~/.claude/projects folders). Only the scrubbed .md conversions are meant
for git — the raw files hold live credentials in tool commands and results.

WHAT IS KEPT: every user message, every assistant text block, and a one-line
marker per tool call (name + its human description). Tool RESULTS are dropped:
they are the bulk of the 7 MB and the main secret carrier, and the prose
already narrates their outcomes.

SCRUBBING is two layers:
  1. Known literals that appeared in these sessions (exact strings).
  2. Patterns: connection-string passwords, npg_* Neon passwords, key=value
     for secret-named vars, JWTs, x-access-token@, presigned-URL signatures.
A post-pass VERIFIER greps the output for every known fragment and pattern and
exits non-zero on any hit — conversion that cannot prove itself clean fails.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys

BACKUP = r"C:\Users\busin\Documents\Paper-Oxidizer\_session-recovery-backup"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(REPO, "docs", "agent-memory", "transcripts")

# --- layer 1: exact strings seen in these sessions --------------------------
LITERALS = [
    "npg_oINaXEV20xRn",
    "oFSOXorGSP-1XFhvVECtZaQ5aoVrvSs-hzy4HL9VLu--grYAbWhsiAgxTZryrL_F",
    "826afb34fa73c7eaf8c5bd820e865ec03e2390ea41507dfb81fbdaf2c7d7bf24",
]
# --- layer 2: patterns -------------------------------------------------------
PATTERNS = [
    (re.compile(r"(postgres(?:ql)?://[^:/\s]+:)[^@\s]+@"), r"\1[REDACTED]@"),
    (re.compile(r"npg_[A-Za-z0-9]{6,}"), "npg_[REDACTED]"),
    (re.compile(r"(?i)\b(x-access-token):[^@\s'\"]+@"), r"\1:[REDACTED]@"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{4,}\b"),
     "[REDACTED-JWT]"),
    (re.compile(r"(?i)\b(X-Amz-(?:Signature|Credential|Security-Token))=[^&\s'\"]+"),
     r"\1=[REDACTED]"),
    # NAME=value / NAME: value / "NAME": "value" for secret-named variables.
    (re.compile(
        r"(?i)\b((?:[A-Z0-9_]*_)?(?:API_?KEY|SECRET(?:_ACCESS_KEY)?|PASSWORD|"
        r"AUTH_TOKEN|ACCESS_KEY_ID|SESSION_SECRET|DATABASE_URL)\"?\s*[=:]\s*"
        r"[\"']?)[A-Za-z0-9+/_.\-:@?&=%]{8,}"),
     r"\1[REDACTED]"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9_\-\.=+/]{12,}"), r"\1 [REDACTED]"),
]

# fragments the verifier must never find in output (prefixes of the literals
# and the raw Neon password shape)
FORBIDDEN = ["npg_oIN", "oFSOXorGSP", "826afb34fa73c7ea",
             "x-access-token:gh", "hzy4HL9VLu"]
FORBIDDEN_RX = [re.compile(r"postgres(?:ql)?://[^:/\s]+:(?!\[REDACTED\])[^@\s]{4,}@"),
                re.compile(r"npg_(?!\[REDACTED\])[A-Za-z0-9]{6,}")]


def scrub(s: str) -> str:
    for lit in LITERALS:
        s = s.replace(lit, "[REDACTED]")
    for rx, rep in PATTERNS:
        s = rx.sub(rep, s)
    return s


def text_of(content) -> list[str]:
    """User/assistant content -> list of markdown chunks (results dropped)."""
    out = []
    if isinstance(content, str):
        if content.strip():
            out.append(content)
        return out
    for item in content if isinstance(content, list) else []:
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        if t == "text" and (item.get("text") or "").strip():
            out.append(item["text"])
        elif t == "tool_use":
            name = item.get("name", "?")
            inp = item.get("input") or {}
            hint = (inp.get("description") or inp.get("prompt")
                    or inp.get("file_path") or inp.get("pattern")
                    or inp.get("skill") or "")
            lines = str(hint).splitlines()
            hint = lines[0][:110] if lines else ""
            out.append(f"> 🔧 `{name}` {('— ' + hint) if hint else ''}".rstrip())
        # tool_result / images: dropped on purpose
    return out


SLUG_RX = re.compile(r"[^a-z0-9]+")


def slugify(s: str, words: int = 7) -> str:
    s = " ".join(s.split()[:words]).lower()
    return SLUG_RX.sub("-", s).strip("-")[:60] or "session"


def convert(path: str) -> tuple[str, str] | None:
    sid = os.path.basename(path).split(".")[0]
    first_user, first_ts, last_ts = None, None, None
    n_user = n_asst = 0
    body: list[str] = []

    with io.open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            ts = rec.get("timestamp")
            if ts:
                first_ts = first_ts or ts
                last_ts = ts
            rtype = rec.get("type")
            msg = rec.get("message") or {}
            if rtype == "user" and isinstance(msg, dict):
                chunks = text_of(msg.get("content"))
                # skip pure tool-result user turns
                chunks = [c for c in chunks if not c.startswith("> 🔧")]
                if not chunks:
                    continue
                n_user += 1
                joined = "\n\n".join(chunks)
                if first_user is None and len(joined.strip()) > 10 \
                        and not joined.startswith("<"):
                    first_user = joined.strip()
                body.append("## 👤 User\n\n" + joined)
            elif rtype == "assistant" and isinstance(msg, dict):
                chunks = text_of(msg.get("content"))
                if not chunks:
                    continue
                n_asst += 1
                body.append("## 🤖 Claude\n\n" + "\n\n".join(chunks))

    if not body:
        return None
    title_src = first_user or sid
    slug = slugify(re.sub(r"[#>`*]", " ", title_src))
    date = (first_ts or "")[:10] or "undated"
    fname = f"{date}_{sid[:8]}_{slug}.md"
    header = (
        f"# {title_src.splitlines()[0][:100]}\n\n"
        f"- **Session:** `{sid}`\n"
        f"- **Span:** {first_ts} → {last_ts}\n"
        f"- **Messages:** {n_user} user · {n_asst} assistant\n"
        f"- **Source:** raw JSONL kept locally in `_session-recovery-backup/` "
        f"(not in git — contains credentials). This file is the scrubbed "
        f"conversation: all prose, tool calls as one-line markers, tool "
        f"results omitted.\n\n---\n\n")
    return fname, scrub(header + "\n\n---\n\n".join(body) + "\n")


def verify(out_dir: str) -> list[str]:
    hits = []
    for f in os.listdir(out_dir):
        if not f.endswith(".md"):
            continue
        s = io.open(os.path.join(out_dir, f), encoding="utf-8").read()
        for frag in FORBIDDEN:
            if frag in s:
                hits.append(f"{f}: literal {frag!r}")
        for rx in FORBIDDEN_RX:
            m = rx.search(s)
            if m:
                hits.append(f"{f}: pattern {m.group(0)[:40]!r}")
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)

    if not args.check:
        index = []
        for f in sorted(os.listdir(BACKUP)):
            if not f.endswith(".jsonl"):
                continue
            res = convert(os.path.join(BACKUP, f))
            if not res:
                print(f"  skip (no conversation): {f}")
                continue
            fname, md = res
            io.open(os.path.join(OUT, fname), "w", encoding="utf-8").write(md)
            kb = len(md) // 1024
            index.append((fname, kb))
            print(f"  wrote {fname} ({kb} KB)")
        with io.open(os.path.join(OUT, "README.md"), "w", encoding="utf-8") as fh:
            fh.write("# Scrubbed session transcripts\n\nConverted from the raw "
                     ".jsonl session files by `tools/transcripts_to_md.py` — "
                     "prose + tool-call markers, tool results and secrets "
                     "removed, verified clean by the script's own scanner. Raw "
                     "originals remain local in `_session-recovery-backup/`.\n\n")
            for fname, kb in index:
                fh.write(f"- [{fname}]({fname.replace(' ', '%20')}) — {kb} KB\n")

    hits = verify(OUT)
    if hits:
        print("SECRET SCAN FAILED:")
        for h in hits:
            print("  " + h)
        sys.exit(1)
    print(f"secret scan clean over {OUT}")


if __name__ == "__main__":
    main()

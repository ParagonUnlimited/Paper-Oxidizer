## Summary

One or two sentences: what does this PR change and why.

## Linked issues

Closes #<n>. Fixes #<n>. Refs #<n>.

## Test plan

How was this tested? Mark all that apply:

- [ ] `uv run review/smoke_test.py`
- [ ] `uv run review/e2e_r2_test.py`
- [ ] `cargo test -p paper-oxidizer-server`
- [ ] `cd v2/web && npx tsc --noEmit`
- [ ] `cd v2/web && npm run build`
- [ ] Manual smoke against `http://127.0.0.1:8778`

Paste the relevant output below.

## Screenshots / before-after

Required for any UI change.

## Known follow-ups

Anything this PR deliberately does NOT fix — name the issue or describe it
inline.

## Checklists

- [ ] Branch is off `main`, commit messages are Conventional Commits,
      no merge commits in the history
- [ ] No secrets in the diff; `.env` values stay empty
- [ ] CHANGELOG.md updated under a new dated section if behaviour changed
- [ ] No reformatting of unrelated lines

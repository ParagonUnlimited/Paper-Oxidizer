# Contributing

Thanks for spending cycles on this. The repository is private; reach the
maintainers through the channels in [`SECURITY.md`](./SECURITY.md) and
[`CODE_OF_CONDUCT.md`](./CODE_OF_CONDUCT.md).

---

## Sign-off — DCO, not a CLA

We use the **Developer Certificate of Origin 1.1** (the same model the Linux
kernel uses). Every commit you push must include a `Signed-off-by:` trailer
that says you wrote the change and have the right to contribute it under the
project's MIT licence. Append it with `git commit -s`. No CLA, no PSF-style
paperwork — just the signed-off line.

Project facts-of-record live in [`STATE.md`](./STATE.md). Read it before
opening a substantial PR; it documents decisions, credential locations, and
operational gotchas that are not repeated anywhere else.

---

## Branch model — Conventional Commits, trunk-based

- Branch off `main`.
- Branch names are `type/scope-summary` — `fix/review-popover-reject`,
  `feat/m2-runner-sidecar`, `chore/ci-cache-rust`. Keep them short.
- Commit messages follow **Conventional Commits**
  (`type(scope): summary`, body wrapped at 72). A history that reads
  top-to-bottom is more valuable than a tree that does.
- Squash or rebase to one or two commits before merge. No merge commits in
  `main`.
- Trunk stays deployable at every commit. Anything that breaks the build
  for ten minutes gets reverted.

---

## Local smoke before opening a PR

The cheapest way to catch a regression is to run the gates that already exist
in this repo. Run them in order; each takes a minute or two.

```bash
# 1. Pure-Rust gate — no services required.
cd v2
cargo fmt --all -- --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace --all-targets --locked

# 2. Front-end gate — npm ci to honour package-lock.json.
cd v2/web
npm ci
npx tsc --noEmit
npm run build

# 3. Live-review-app gates — require Neon + (optionally) R2 credentials.
cd ../..
uv run review/smoke_test.py
uv run review/e2e_r2_test.py

# 4. m2_gate — the full pipeline-ish gate; see v2/m2_gate.py.
uv run v2/m2_gate.py
```

A PR that does not pass all four categories is not yet ready. CI runs the
same checks; running them locally first saves round trips.

---

## Deploy — how to ship a change

This repo deploys through **Coolify**. Do not change the canonical deploy
flow in a single PR; that is its own milestone. Hot-fix the deploy file in
isolation, deploy, watch the loop, then resume feature work.

The full runbook lives in [`review/DEPLOY.md`](./review/DEPLOY.md).

---

## Where things live

| You want to… | Look in |
|---|---|
| Touch the Rust review server | `v2/server/` |
| Touch the TypeScript front end | `v2/web/` |
| Touch the data schema | consult `STATE.md` first, then `v2/server/src/` |
| Touch the deploy wiring | `docker-compose.yml` + `review/` |
| Document an operational decision | `STATE.md`, then `CHANGELOG.md` |
| File a vulnerability | `SECURITY.md` (private channel, never a public issue) |

Welcome aboard.

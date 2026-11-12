# Work session checklist

## Start

- `git status` — never commit on `main`; use `feat/…` or `fix/…` branches.
- Skim open issues in the Linear project **Receipt Tracker** (canonical). Sync seed: `docs/linear-backlog.md`.

## Commits and Linear

- **Commit:** `[AA-N] Imperative statement`
- **PR title:** `[AA-X][AA-Y]Imperative statement` (no space after last `]`)
- **PR body:** `Closes AA-NN`
- Optional Linear branch: `hi/aa-N-slug`

## End session

- Run tests before commit.
- `./scripts/pr_ready.sh` before opening a PR (if present).

# Linear issues (Receipt Tracker / team AA)

| Item | Value |
|------|--------|
| Team key | `AA` |
| Project | Receipt Tracker |
| Registry slug | `receipt-tracker` |

## Commit / PR format

- **Commit:** `[AA-N] Imperative statement`
- **PR title:** `[AA-X][AA-Y]Imperative statement` (no space after last `]`)
- **PR body:** `Closes AA-NN`

## Backlog and documentation

**Canonical backlog:** Linear project Receipt Tracker.

**Sync seed:** [`docs/linear-backlog.md`](linear-backlog.md). After `backlog sync`, edit issues in Linear only. Policy: `~/src/agents/niccolotto/docs/documentation-policy.md`.

```bash
cd ~/src/agents/niccolotto && npm run niccolotto -- backlog sync receipt-tracker --dry-run
```

## Other work

Non-AA tickets → [`~/src/README.md`](../../../../../README.md).

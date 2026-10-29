# Archive

Tools kept for reference only. They are **not maintained**, are not part of the
`mcperf` package, and are excluded from linting, tests and Copilot instructions.
Do not build on them; copy what you need into `mcperf/` instead.

| Folder | What it was | Archived | Last working version |
|---|---|---|---|
| `gameday/` | Bulk-provisioned MontyCloud root users and tenants for GameDay events | 2026-10-29 | tag `archive/gameday-v1` |

To run an archived tool exactly as it was, check out its tag in a separate worktree:

```bash
git worktree add ../gameday-v1 archive/gameday-v1
```

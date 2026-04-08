## Findings

1. Medium: `.claude/settings.json:10-15` makes the `Stop` hook run for every assistant stop and always launches `codex exec "Review changes since last commit..."`.
This is not scoped to code edits or explicit review requests. In practice it will spawn a review run after any Claude response, including planning-only turns, which adds unnecessary cost, churns the working tree, and can overwrite `REVIEW.md` repeatedly.

2. Medium: `.claude/settings.json:14` runs the review command from the caller's current working directory, but the diff it is supposed to review is repository-wide.
In this session the hook was triggered from `/home/byron/projects/finally/backend` while the modified files were at the git root (`/home/byron/projects/finally`). That makes the behavior brittle: path resolution depends on where Claude happened to be, and the report is written to a cwd-relative `REVIEW.md` instead of a predictable repo-root path.

3. Low: `README.md` is deleted outright with no replacement entrypoint at the repository root.
Removing the top-level README is a documentation regression for anyone landing on the repo from GitHub or a local checkout. `planning/PLAN.md` is much more detailed, but it is not a substitute for a concise root-level overview and quick-start document.

## Notes

- Scope reviewed: tracked changes against `HEAD` only (`.claude/settings.json`, `README.md`, `planning/PLAN.md`).
- No application code changed in this diff, so this review is focused on workflow/configuration risk and documentation regressions.

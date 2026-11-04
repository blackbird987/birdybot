# Computational Sensors (`.claude/sensors.json`)

Chains run a deterministic sensor step (build → **sensors** → review_code → …)
that executes fast checks in the build worktree and feeds raw tool output back
to the build session for self-fixing (`bot/engine/sensors.py`).

- Auto-detection per stack: `dotnet build` (C#), ruff critical-errors-only
  (Python, only if installed — syntax errors/undefined names, not style),
  `npx tsc --noEmit` (tsconfig present). No stack/tools → step skips silently.
- Per-repo override in the main repo (not the worktree), replaces auto-detect:
  ```json
  {
    "sensors": [
      {"name": "ruff", "command": "ruff check .", "blocking": true, "timeout_s": 180}
    ],
    "policy": "block",
    "max_fix_rounds": 2
  }
  ```
- `policy`: `block` (default — persistent failures halt the chain via
  needs_input, like verify-fail) or `warn` (post failures, advance anyway).
- Sits alongside the other per-repo config files: `.claude/test.json`
  (verify policy + diagnostics) and `.claude/workflow.json` (merge autonomy).
- `"blocking": false` on a sensor means warn only: it never counts as a failure,
  so it cannot start a fix session or halt a chain under either policy. Its output
  goes in the notice the user sees (`SensorReport.notice`), not in the fix prompt.
- A worktree with `study/config.toml` also gets a `study check` sensor, always
  non-blocking and added outside the auto-detect branch, so `sensors.json` does
  not replace it. It runs the repo's own `tools/study/check.py` when there is one,
  otherwise the shared tool (`docs/design/findings.md`). It looks in the worktree,
  not the main repo, so one forked before the study folder existed skips quietly.

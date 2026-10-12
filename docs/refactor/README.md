# Refactor scout reports (2026-10-11)

Five read-only scout sessions mapped the codebase before the October 2026
refactor. Their reports are kept here because the originals in
`data/results/` are pruned after about a week, and the refactor's commits
cite finding IDs from them.

Finding-ID prefixes:

- `R` (R1-R15): `q-19565.md`, the runner slice: `bot/claude/runner.py`,
  `parser.py`, `types.py`, `session_index.py`.
- `E` (E1-E24): `q-19566.md`, the engine: `bot/engine/commands.py`,
  `workflows.py`, `lifecycle.py` and the smaller engine modules.
- `D` (D1-D26): `q-19567.md`, the Discord layer and `bot/platform`.
- `S` (S1-S23): `q-19568.md`, the store, `config.py`, memory and cgroups,
  `app.py` and the small leaf modules.
- `X` (X1-X18): `q-19569.md`, cross-cutting: the test baseline, dead code,
  duplicated helpers and the import graph.

Each report ends with a Section D listing what looked redundant but is
deliberate. Those rejections bind the refactor as firmly as the "must not
drift" notes in `CLAUDE.md`.

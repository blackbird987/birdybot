# You can read the bot's own instance registry

When you are asked "are the sessions you spawned done?", the bot already knows
and `scripts/instances.py` reads it. Do not answer "I can't tell, run /list
yourself" — that happened on 2026-09-08 while `/list` had the full answer.
`ListAgents` is not the same dataset: it lists peer Claude CLI sessions on the
machine and knows nothing about bot instances.

```bash
python scripts/instances.py children q-16871         # did my spawned children finish?
python scripts/instances.py list --status running    # what is live right now
python scripts/instances.py show t-8206              # branch + worktree of a build
python scripts/instances.py log t-8206 --tail 40     # what it reported
python scripts/instances.py find <thread_id>         # everything that ran in a thread
```

`tree`, `diff` and `--json` are there too. From another repo, call it by its
absolute path — it resolves the *installed* bot's `data/` via
`procutil.install_root`, so it reads the same registry from anywhere, including
from inside a build worktree.

Four things worth knowing before you quote it:

- **It is read-only by omission**, like the mail and telegram readers in The
  Citadel: no kill, no retry, no write path exists in the file. It also does
  not go through `StateStore` (whose `save()` is one typo away) or import
  `bot.config` (which resolves `DATA_DIR` against the *caller's* cwd and drops
  a path marker on init).
- **A spawned child carries no `parent_id`.** Only button/chain steps do. A
  `/spawn` child is joined back to its parent through the forum thread —
  `history.jsonl` first, the live session→thread map only as a fallback,
  because a thread moves on to a newer session and resolving by session alone
  would report a finished child under whatever is running there now.
- **An instance that died before the CLI reported a session id and never
  finalized has neither link**, so it reports as unlinkable rather than being
  guessed at from timing. That is the shape of a bot restart mid-run.
- **The view can lag by up to a minute** (the store flushes on a 60s
  auto-save), and terminal instances are pruned from `state.json` after
  `INSTANCE_RETENTION_DAYS` / `INSTANCE_MAX_RETAINED` along with their result
  files. Every command prints the state file's age; `show` falls back to the
  append-only history for a pruned id.

Harness: `python scripts/test_instances.py`

# A Control Room says what its repo is about

The repo control room embed used the filesystem path as its whole
description, so a forum of ten repos read as ten paths and you had to
remember which was which. It now leads with a one-line blurb and demotes the
path to `-#` subtext underneath.

The blurb is **derived, not typed in** (`bot/engine/repo_desc.py`). Every repo
already states its purpose somewhere, so first hit wins:

`.claude/repo.json` → `CLAUDE.md` → `README.md` → `pyproject.toml`
(`[project]`, then `[tool.poetry]`) → `package.json` → `Cargo.toml`

For markdown that means the first *paragraph* that reads as prose — the title
is skipped in both its `#` and underlined spellings, as are fenced code,
bullets, block quotes, HTML comments, rules, table rows and badge rows. A
wrong blurb is worse than none, and the title is the wrong blurb: the embed
already shows the repo's name above it. The paragraph, not the line: most
READMEs here are hard-wrapped, so the first *line* ends mid-sentence ("...take
a plain-language request like") and reads as truncation with no ellipsis to
admit it.

The joined paragraph is reduced to one plain line and fitted to 120 chars,
preferring the longest run of *whole sentences* that fits — "Agentic media
downloader." beats the first 118 characters of the paragraph it opens. A
word-boundary cut with `…` is the fallback, used when the leading sentence is
under 24 chars and too terse to describe anything. Emphasis is unwrapped by
*paired* regex rather than by stripping the characters — a blunt `_` strip
turns a sentence about `data/state.json` and `repo_desc` into mush, and
snake_case is exactly what a developer README's first line contains.

**The cache is the load-bearing part.** `refresh_control_room` runs on every
instance start and completion, so the hot path is stat-only: six `os.stat`
calls producing a signature, and the file bodies are re-read only when that
signature moves. The miss path defers its write (`mark_dirty`, picked up by
the 60s auto-save) rather than saving through — `state.json` is megabytes, and
a cache whose miss costs a full rewrite is worse than no cache. A repo that
says nothing about itself caches the *miss*, so it costs stats rather than six
failed opens forever. The signature names every
candidate that exists together with its own mtime *and size*, deliberately not
just the newest mtime: a source restored from a tarball or read over a mount
with a skewed clock carries a *future* mtime, and behind it a newly created
`.claude/repo.json` would never move a maximum, so the manual override would
silently never apply. Size comes free out of the same `stat` and catches the
other half — a file restored with its timestamp preserved but its content
changed. There is no sync twin of the refresh, on purpose: one existed with no
caller but the harness, and a sync/async pair of the same fifteen lines lets a
fix land on one half while the tests keep passing against the other. `_localise_paths` translates the recorded path for the
same reason it translates `repos` — the other machine's spelling never
matches, and every refresh would re-read the files the cache exists to skip.

`.claude/repo.json` is the manual override, written by `/repo desc <text>`
(`/repo desc <name> <text>`, `/repo desc clear`, bare `/repo desc` to show it
and its source). `desc`, `deploy` and `clear` are reserved repo names, so a
repo cannot shadow either the subcommand or the `clear` argument. With no name it targets the repo of the **channel it was
typed in**, before the globally active one: the command is typed inside a
repo's forum, and defaulting to whatever was `/repo switch`ed to last writes
the sentence into the wrong repo. That needs both halves — the engine prefers
`ctx.repo_name`, and `cmd_repo` has to fill it in, because `_run_slash` builds
its ctx with no repo and the engine half alone is inert on the slash path.
`ForumManager.repo_for_channel` resolves it, falling back to the parent forum
so the Control Room post itself resolves like any session thread — and it is
handed the interaction's own channel, not just its id, because a Control Room
auto-archives like any forum post and discord.py drops an archived thread from
its cache, so the id-only lookup misses exactly the case the fallback is for.
It is wired into `/repo` only: setting it in `_run_slash` would also change which repo
`/bg` runs in. A repo whose directory is gone is refused, not created —
`mkdir(parents=True)` on a stale registration would conjure an empty tree that
looks like the real thing. It is written into the *repo*, not into bot state, because
the sentence describes the repo and should travel with a clone — and it sits
next to the per-repo config files that already live there (`test.json`,
`workflow.json`, `sensors.json`, `deploy.json`). Other keys in the file are
preserved on write, and one that will not parse is refused rather than
replaced — it is committed alongside them and may hold keys written by hand.

This repo deliberately ships **no** `.claude/repo.json`, so it exercises the
CLAUDE.md fallback in real use.

Harness: `python scripts/test_repo_desc.py`

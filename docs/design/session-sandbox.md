# Session sandbox

An opt-in, per-repo limit on what a Claude session can reach. Off for every repo
until the owner runs `/sandbox on <repo>`. This work turned on no repo.

It is for working in a repo you do not fully trust, or with a key you would not
want a prompt injection to walk off with. It rests on Claude Code's own sandbox
(bubblewrap and a socat proxy) for Bash, a path hook for the file tools, and bot
gates for everything the bot does on a session's behalf. Claude Code's sandbox
does not cover those last two: the 2026-11-04 trial (`data/results/q-22184.md`)
showed it holds for Bash only, and a review of that trial found the bot running
session-controlled input outside it.

Harnesses: `scripts/test_session_sandbox.py` (fast, no session),
`scripts/attack_session_sandbox.py` (real CLI, run by hand, see "Tests").

## The setting

- Stored in the StateStore, `sandboxed_repos`, keyed by resolved real path. Never
  in the repo or `.claude/`, which a session can write.
- Read per spawn in `core._prepare_spawn`, the single enforcement point. Resume,
  chain steps, retries and recovery all go through it.
- **Fails closed.** A corrupt `sandboxed_repos` is kept as `{"__corrupt__": ...}`
  so a save does not launder it into "off". `sandbox.is_sandboxed` answers yes on
  anything unreadable, and `_sandbox_prepare` refuses the run.
- `/sandbox on|off|hosts <repo> [hosts...]|status` is owner-only. `on` refuses a
  path equal to, inside or containing the bot repo, a cursor repo, a repo outside
  home, and a `core.hooksPath` inside the repo or relative. A provider switch on a
  sandboxed repo refuses the run rather than turning the sandbox off.
- `/sandbox off` undoes the git hardening and clears failed check-ins for the repo.
- A repo moved to a new path reads as off, because the key is the path.

## What a sandboxed run gets

`sandbox.prepare_run` builds a directory under the bot's data dir,
`sandbox/runs/<id>-<hex>`, removed by `finish_run`:

- `private/`: the settings file, the hook config, the system prompt and anything
  else the bot writes for the run. Nothing there is readable by the session.
- `tmp/`: the session's temp dir (`CLAUDE_CODE_TMPDIR`, `TMPDIR`). `/tmp` itself
  is denied, because bot and other sessions' files live there.
- `attachments/`: copies of the uploads this prompt names. The prompt is rewritten
  to point at them. See the invariant below on why they are copies.

The CLI gets `--setting-sources user --strict-mcp-config --settings <file>`: one
settings file, nothing from the repo. Project `CLAUDE.md` files are concatenated
into the system prompt file, skipping links. API fallback is refused, because
`--bare` skips hooks and the key stays in the environment. `--restricted` is not
used: it does not work with the bot's `bypassPermissions`.

Settings in force: `sandbox.enabled`, `failIfUnavailable`,
`allowUnsandboxedCommands: false`, `denyRead` on home, `/tmp` and every env file,
`allowRead` on the working dir and toolchains, `denyWrite` on `.claude/` and the
git internals, a strict host list, the path hook, `blockReadsOutsideWorkingDirectories`,
and `WebFetch`, `WebSearch`, `SendMessage`, `RemoteTrigger`, the cron tools, `Monitor`,
`Skill`, `Workflow`, `ListAgents` and friends denied. The session is told about
all this in a prompt note, so it stops and asks instead of flailing.

The environment loses every name in the bot's `.env`, the secret-looking names in
`redact.py`, and agent sockets. What the CLI itself needs stays in its env and is
hidden from Bash through `credentials.envVars`. Vault keys granted to the repo stay
available to Bash, or the feature would be useless.

Shared cache per repo for npm, pip and dotnet, not per run, plus the dotnet flags
from the trial.

### The path hook

`bot/claude/hooks/sandbox_paths.py`, outside any repo, runs on Read, Grep, Glob,
Edit, Write, MultiEdit and NotebookEdit, because those run in the CLI process
outside bubblewrap. It exits 2 on any exception, and the settings command adds
`|| exit 2` so that a crash of the interpreter blocks too. Writes under the working
dir are refused for any `.git` or `.claude` path component and any `.mcp.json`.

`worktree_guard` and the test mutex live in `settings.local.json`, which a
sandboxed run does not load, so a worktree run's generated settings carry them.

### The private git directory

A session must not be able to rewrite what the bot's git trusts. For a worktree
run `sandbox.checkout_worktree` moves the worktree onto a private copy of its git
dir, with its own `config`, `HEAD` and refs, borrowing objects read-only through
an alternate. `checkin_worktree` then:

- restores the real gitfile first, so the worktree is never left pointing there,
- refuses on a link, FIFO, device or changed config in the private dir,
- fetches **only this run's branch** home, so `update-ref` on master or a planted
  branch or tag stays behind,
- takes `MERGE_HEAD` back only if each line is a real object id.

A refused check-in marks the worktree: the next run on it is refused too, until
the owner looks at it and `/sandbox off` clears it. `recover_after_restart` finishes
any check-in a bot restart interrupted, and removes dead run dirs.

Query runs have no worktree and use the main checkout, with `.git` and `.worktrees`
read-only. They cannot commit.

The bot's own git in those worktrees runs with `_GIT_SAFE`: hooks and fsmonitor off,
submodule diffs ignored. It reads the real git dir's config, which a session cannot
write, so nothing else a config could run is in a session's hands.

### Stub files

Claude Code protects a nonexistent `denyWrite` path by mounting `/dev/null` there,
which creates a zero-byte file. They go in `.git/info/exclude`, and `clean_stubs`
removes them after a run only when they are zero-byte and untracked and no other
live session is in the repo (query runs share the main checkout).

`git status` in a session prints `unable to access .gitmodules: Permission denied`
because of that stub. Exit code 0. A tracked `.env` shows as modified in a session
because it reads as empty. `git add -A` would blank it, and the merge gate holds
that branch for the owner.

## Bot-side gates

All keyed on the originating repo being sandboxed.

- `/spawn`: only into a repo that is also sandboxed. Refused in the thread, logged.
- `/repo add|create|switch|hide|unhide`: ignored, logged, thread told.
- `/watch`: `log=` must stay inside the working dir, read with no link following
  and no blocking on a FIFO, re-checked at read time (`Watch.log_root`). `pid=` is
  refused, because a session's pids are not host pids.
- Sensors: skipped with a note. They would run with the bot's full environment.
- Auto-merge: `.claude/workflow.json` and the verify policy in `.claude/test.json`
  are ignored. A branch touching `.claude/**`, `.github/**`, `.mcp.json`,
  `.gitattributes`, `.gitmodules`, `.envrc` or `.env*` needs an owner tap
  (`MERGE_FAIL_SANDBOX_PROTECTED`). A diff git cannot list counts as touching them.
- Deploy: refused when the command, health check or script fingerprint changed
  since approval (`sandbox.deploy_fingerprint`). Deploys still run the merged code
  on the host, by design: approving one is the owner's decision.
- `/image`: narrowed to the working dir; the data dir is not a source.
- `TODO.md`: the bot reads and writes it for deferred items, and skips it if it is
  not a plain file in a sandboxed repo. The blurb's own reader
  (`repo_desc._candidate`) already refused a link out of the repo. `.claude/*.json`
  and project instructions go through `fsutil.read_contained_text`, for every repo.

Checked and needing no gate:

- `/reply`: only to children the session spawned, and the spawn rule keeps those
  sandboxed.
- `/chain`: stays in the same repo. Every step goes through the enforcement point,
  and the merge gate applies at its end.
- `/secret request`: a name only. The owner gets the buttons.
- `/wake`: re-enters the enforcement point.
- `resolver.py`: runs git merge in the real git dir between runs, while the session
  works through the private one. Index and `MERGE_*` are copied at checkout.
- `chain_runner.py`: no exec. It only reads a verify result file.
- `git_release.py`: git only, on the real git dir, whose config and hooks a session
  cannot write.
- `read_repo_config` readers (`test.json`, `workflow.json`, `deploy.json`,
  `sensors.json`): none executes `test.json` outside a session, and the rest are
  gated above.

## Failure wording

`REFUSAL_PHRASE` ("Session sandbox refused this run") and the CLI's own "sandbox
required but unavailable" are in `parser.is_account_agnostic_error`, pinned by the
harness. A refused run has no output and no turns, which is exactly what recovery
reads as an account falling over. Reword one and a doomed run goes to the backup
subscription. When bubblewrap is missing the CLI prints
`Error: sandbox required but unavailable: sandbox is enabled but dependencies are missing: ...`
and then `sandbox.failIfUnavailable is set`, on stderr, rc 1.

## What this does not stop

Be plain about these. It limits a session; it does not make one safe to hand
anything.

- **Allowed hosts carry data out.** Anything the session can read, including a
  granted vault key, can leave through a host on the list. `github.com` is on it
  by default.
- **Unsandboxed sessions and the bot repo can read everything**, including this
  setting in `state.json`, which they could also edit. The bot repo cannot be
  sandboxed. A sandbox on one repo says nothing about what its neighbours can do.
- **Readable by design**: toolchains, `~/.gitconfig`, this repo's memory dir
  (read-only).
- **A sandboxed child's report reaches an unsandboxed parent as text**, and it is
  untrusted text.
- **Deploys** run merged code outside the sandbox.
- **The real model was not the attacker.** See "Tests".
- **The Write symlink race** rests on the CLI's own guards plus the hook. 40
  concurrent attempts against a swapping link landed nothing, which is a test and
  not a proof.
- `TODO.md` and blurb reads have small check-then-use windows if a job is running.
  Background jobs die with the CLI (tested), so there is rarely anything to race.
- `diff.ignoreSubmodules=all` is set while on, so submodule changes do not show.
- `/watch` runs a session's regex in the bot, an availability risk and not a read.
- A repo under a dot directory in home hits the `Edit(~/.*)` deny and cannot edit.
- A prompt naming another upload's unguessable path gets a copy of it. Those names
  are random uuid4s and reach only the thread they were posted in.

## Invariants

- **The setting lives outside the repo.** Anything in the repo is a session's to
  write.
- **`core._prepare_spawn` is the only way in.** A spawn path that skips it is a hole.
- **An unsandboxed repo's command line and environment do not change.** Everything
  here is behind `sandbox_run` being set. Checked with a recording fake CLI, master
  against this branch: query, task and resume runs were identical but for the
  random temp name of the system prompt file.
- **Never put `data/pending_images` in `additionalDirectories`.** A directory grant
  overrides `denyRead` for Bash, so every other session's uploads become readable,
  and is writable unless also in `denyWrite`. A file path is no use either: the
  permission layer rejects it as not a directory, while the sandbox still opens it
  to Bash read-write. That is why attachments are copied. The same goes for
  anything else added there: it also needs a `denyWrite`.
- **`.claude/` exists and is denied before the session starts.** The deny mounts
  only hold for what exists. A session that creates `.claude/workflow.json` writes
  into its next run's config and the merge gate.
- **Check the run back in before trusting its git.** The session must be dead first
  (`_sandbox_after_attempt`), and it survives cancellation through a shield.
- **Hook failure means block.** Never let an exception or a missing interpreter read
  as allow.
- **Every new directive that makes the bot act checks `origin_is_sandboxed`.** The
  bot runs with everything. A new one that does not is an escape, whatever the
  sandbox does.

## Tests

`scripts/test_session_sandbox.py` runs with the rest of the suite.

`scripts/attack_session_sandbox.py` is run by hand, takes minutes, and needs bwrap,
socat and a login:

```bash
cd scripts && timeout 585 python attack_session_sandbox.py --keep
```

**It uses a scripted Messages endpoint in place of the model for the attacks**, and
the real login only for a plain functional session and an unsandboxed one. Everything
else in it is real: CLI, settings, hooks, bubblewrap, proxy, config dir, worktree
layout, memory cgroup. The real model read the checks script and declined to run it,
saying such an evaluation belongs in a harness outside the agent. That is fair and
also what this does: tool calls are the thing being tested, and a script issues them
more reliably than a persuaded model. The model's own ability to find an attack
nobody listed is what this does not cover. Earlier probes in the same build did run
many of the same attacks with the real model on near-final settings.

Rerun it on every Claude Code upgrade: the sandbox, tool list and deny syntax are the
CLI's, and new tools appear (`_DENIED_TOOLS` is a list of what existed in 2.1.310).
A tool the list does not name is allowed. The attack run's `tools/list` line shows
what a session is offered, and it is the first thing to read after an upgrade.

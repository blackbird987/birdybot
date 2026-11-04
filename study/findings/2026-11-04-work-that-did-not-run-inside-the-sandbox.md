+++
id = "2026-11-04-work-that-did-not-run-inside-the-sandbox"
title = "Work that did not run inside the sandbox in the trial"
date = "2026-11-04"
status = "stands"
question = "Which everyday tools fail under the sandbox settings, and which of those did settings not fix?"
verdict = "In the 2026-11-04 trial on near-final settings: podman, Telegram and systemctl --user failed or only worked by opening a hole, and the report advised keeping repos that need them off. git push over SSH, gh, mail and instances.py need paths the home deny hides, so they need a broker or a run outside."
tags = ["sandbox"]
source = { file = "data/results/q-22184.md", section = "Breakage" }
+++
Worth keeping because the design note lists what the build fixed and not what stayed broken. Second source: docs/design/session-sandbox.md, for what shipped.

Needs a broker or a run outside the sandbox, per the report:
- `git push` over SSH, `gh`, mail and `instances.py` need token or data paths the home deny hides. Under lighter settings push (dry run), `gh` and mail self-test passed, but the key and tokens were readable.
- Telegram: its connection failed even with the token dir writable.
- podman: fails even with nested namespaces. `excludedCommands` runs it outside, and it then read the canary.
- `systemctl --user`: `allowAllUnixSockets` opens D-Bus, and `systemd-run` then read the canary. Listing a single socket path does not work.

Measured: a `~/**/.env` deny took 108s against 2s for exact paths. Worked in the trial: pytest, commits, HTTPS fetch, worktree commits.

The sources disagree on `/tmp`. The report lists it as still open: denying it also cut network (`api.github.com` failed). The note and `bot/claude/sandbox.py` deny `/tmp` in the shipped settings, and neither says what changed. This record does not settle whether network still breaks.

What this does not establish. The full breakage list was not run on the exact final settings, only a near-identical version. The report names a broker as a need and does not test one, so nothing here says a broker would work.

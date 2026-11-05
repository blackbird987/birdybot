+++
id = "2026-11-05-a-guest-session-without-the-sandbox-reached"
title = "A guest session without the sandbox reached outside its repo, and a resumed one stays confined"
date = "2026-11-05"
status = "stands"
question = "Did only a prompt sentence keep a guest's file tools inside its repo, and does automatic confinement hold, including on a thread started before it?"
verdict = "Yes on both, in the 2026-11-05 run with the real CLI and a scripted endpoint, not a real model: on the commit before the change a guest-shaped session read a fake secret outside its repo, edited a file and wrote a new one, and after it all were denied, including resumed."
+++
A guest grant with bash policy "none" removes only Bash, and the CLI runs with bypassPermissions. The code suggested only a prompt sentence kept Read and Edit in the repo, but nobody had run it.

`scripts/attack_guest_confinement.py` ran a guest-shaped session through the real runner and CLI, in a temp repo with no sandbox setting and a fake secret beside it, on an export of d593528 and on the changed code.

Before: Read returned it, Edit changed an outside file, Write created one, Glob listed the directory.

After: all denied by the path hook, and the canary reached no request. Edit outside first stops at the CLI's read-state check, so the proof also resumed a session that had read the file before confinement: 4 of 4 Edits reached the hook and were denied. Work inside the repo succeeded, and a confined build child committed on its branch.

A session started unconfined resumed confined with the same id and history. A relative core.hooksPath refused the run before the CLI started. An owner's run with no guest had identical argv and env names, apart from a temp filename; that file's contents were not compared.

`--tools ""` on the title helper still offered MCP tools. `--strict-mcp-config` removed them, and a real title still came back. `scripts/attack_session_sandbox.py` passed 198 of 198.

What this does not establish. The endpoint was scripted, not a real model, so this shows what these calls got, not what a model would try. One temp repo and CLI version. No live Discord route or real guest thread was run; routes are pinned with fakes in `scripts/test_guest_confinement.py`. It does not cover what a guest's edits do when an owner's later unconfined run or a deploy executes them. Discord permissions keeping a guest out of unmapped channels was read from code, not tested.

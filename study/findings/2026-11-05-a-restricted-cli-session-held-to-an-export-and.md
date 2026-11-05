+++
id = "2026-11-05-a-restricted-cli-session-held-to-an-export-and"
title = "A restricted CLI session held to an export, and the flags need measuring"
date = "2026-11-05"
status = "stands"
question = "Do --restricted --tools Read,Glob and the path hook confine an answering session to its export, and does a no-tool resume keep the session usable?"
verdict = "Yes on Linux with CLI 2.1.318 on 2026-11-05, real CLI and a scripted endpoint: every read outside the export was denied, nothing planted loaded, and a no-tool resume kept the session. Without --restricted, CLAUDE.md files and a planted hook took effect. Windows and live use not run."
+++
Stage 2 of the machine link has a session read a repo for the other machine. No document promises `--restricted` confines reads, so it was measured, and the bot measures it again before every Run once (`peer_confine.self_check`). Real CLI 2.1.318, with a scripted endpoint making every attempt.

With the answering flags, only the canary inside the export reached a request. Outside reads by absolute path, `../` and symlink were refused. Glob returned nothing outside. Bash, Grep, WebFetch, Write and Task got "No such tool available". No planted hook or MCP server ran.

Without `--restricted`, on the hook alone, the export's, two parents' and the user's CLAUDE.md all reached the model and a planted hook ran. So that flag is what holds. The check refused when run without it, with reads wide open, with Bash added and with no CLI, so it can fail. The path hook does run under these flags: it stopped a read only it could stop.

`--max-turns` is hidden but works: 30 requests at a limit of 30, then a non-zero exit.

Delivery (`--restricted --tools ""`, resuming): no tools offered, same session id, nothing planted loaded. The next ordinary resume had every tool back and remembered. One real-login Haiku run agreed.

`scripts/attack_peer_answer.py` passed 104 of 104.

What this does not establish. Nothing ran on Windows: the drive-letter and backslash rules were only exercised by swapping in `ntpath`. It is one CLI version, which is why the check runs at each tap. The endpoint was scripted, so this shows what these calls got and not what a model would try. Discord was faked: no real tap, thread, archive or sleep. It says nothing about what a model does with a forged answer, only that it has no tools to act on one with.

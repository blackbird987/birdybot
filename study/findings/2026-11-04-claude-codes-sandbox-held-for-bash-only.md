+++
id = "2026-11-04-claude-codes-sandbox-held-for-bash-only"
title = "Claude Code's sandbox held for Bash, not for the rest of a session"
date = "2026-11-04"
status = "stands"
question = "Is Claude Code's built-in sandbox a boundary on its own for a session?"
verdict = "No, in the 2026-11-04 trial, driven by a scripted stub and not a real model: with the sandbox alone Read, Write, WebFetch, a planted hook or .mcp.json, and a hostile merged branch got through. It held for Bash. Only the report's best settings, which add a hook and tool denies, kept 30-odd attacks to one canary copy in /tmp."
tags = ["sandbox"]
source = { file = "data/results/q-22184.md", section = "whole report" }
+++
Asked before building the session sandbox: can the CLI's own sandbox be the whole limit? Second source: docs/design/session-sandbox.md, top section, which repeats the "Bash only" result.

With the sandbox alone these got through: the Read and Write tools despite the deny settings; WebFetch to a non-allowed host; a planted project hook or `.mcp.json`, which ran outside the sandbox in the next session; a hostile branch built inside the sandbox, whose hook read the canary once merged.

Blocked in Bash: canary reads via symlink, `/proc` environ and mem, ptrace, D-Bus, `systemd-run`, ssh-agent and other sockets, `dangerouslyDisableSandbox`, nested bwrap, podman, writes to rc files and `.claude` and git hooks and `.mcp.json`, non-allowed hosts, UDP and DNS, the denied env var.

The "30-odd attacks, one /tmp copy" figure is for the report's best settings, which include a path hook and tool denies, so it is not a figure for the bare sandbox. Allowed hosts carried data: a POST reached `api.github.com`.

The note also says a review of the trial found the bot running session-controlled input outside the sandbox. That is a separate finding from these attacks.

What this does not establish. `data/results/` is not in git, so this record may outlive the report. The report lists as untested, among others: a real model or login, the worktree layout under the real home, SendMessage, RemoteTrigger, WebSearch, real MCP servers, writes to real user settings, DNS leaks through the proxy, whole-process bwrap. That sessions can edit bot code which later runs unsandboxed came from reading the code, not a test.

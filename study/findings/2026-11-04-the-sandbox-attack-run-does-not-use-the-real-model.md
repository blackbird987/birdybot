+++
id = "2026-11-04-the-sandbox-attack-run-does-not-use-the-real-model"
title = "The sandbox attack run drives its attacks with a script, not the real model"
date = "2026-11-04"
status = "stands"
question = "Can the real model be the attacker in the session sandbox's attack run?"
verdict = "Not so far in the harness: Haiku refused in the trial and the real model declined the checks script in the build, so a scripted endpoint issues the attacks and a model finding an attack nobody listed stays untested. Earlier probes in the build did run many of the attacks with the real model."
tags = ["sandbox"]
source = { file = "docs/design/session-sandbox.md", section = "Tests" }
+++
A later session reading "the attack run passes" could take it for more than it is, or spend a run trying to talk a model into it. Second source: data/results/q-22184.md, section Untested.

Three separate events:
- The trial: "inner Haiku refused the attacks, so a scripted stub drove the real CLI".
- The build's `scripts/attack_session_sandbox.py`: the real model read the checks script and declined to run it, saying such an evaluation belongs in a harness outside the agent.
- Earlier probes in the same build did run many of the same attacks with the real model on near-final settings. So the refusals are not universal, and the note does not say what made the difference.

In the script the attacks come from a scripted Messages endpoint. The real login is used only for a plain functional session and an unsandboxed one. CLI, settings, hooks, bubblewrap, proxy, config dir, worktree layout and memory cgroup are real.

The note says to rerun on every Claude Code upgrade, because `_DENIED_TOOLS` lists what existed in 2.1.310 and a tool the list does not name is allowed.

What this does not establish. It does not show a persuaded model cannot be made to attack, nor that a model would find nothing new. This record does not say the attack run passes on the current CLI: neither source records a run on any later version.

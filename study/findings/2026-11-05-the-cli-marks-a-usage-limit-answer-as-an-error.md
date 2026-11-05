+++
id = "2026-11-05-the-cli-marks-a-usage-limit-answer-as-an-error"
title = "A usage limit answer says subtype success, and a call keeping the API key runs past the limit on the key"
date = "2026-11-05"
status = "stands"
question = "How can a one-shot CLI call tell a limit answer from a real one, and which account does it actually run on?"
verdict = "Measured 2026-11-05 on one CLI version: a weekly limit answer is exit 1, is_error true and subtype success. With ANTHROPIC_API_KEY in the environment the CLI reports apiKeySource ANTHROPIC_API_KEY and answers normally on a config dir at its limit."
+++
The naming call took the CLI's result text as a title, and another machine's threads got named "You've hit your weekly limit". Reading the subtype looks like enough to spot that, and pinning an account looks like it decides who pays. Neither holds.

Real CLI, `CLAUDE_CONFIG_DIR` on the account at its weekly limit, no API key: exit 1, `is_error: true`, `subtype: "success"`, the limit sentence as result text. The flag and exit code detect it, the subtype cannot.

The naming code, same config dir, from a process that imported `bot.config`: three good titles from the limited account, with `apiKeySource: ANTHROPIC_API_KEY`. `config` loads `.env` into `os.environ` and the call copied it with the key, which the runner has removed for sessions since v0.61.1. With the key removed, the limited account gave the limit error, `apiKeySource: none`, and the free account gave a title.

So an account pin says nothing where the key is present, and a test of account choice needs the key out or it passes for the wrong reason. It did here, once. `bot/discord/log_triage.py` copies the environment the same way and was left alone. `--no-session-persistence` left no new session file on either account, on a success and on a limit answer.

What this does not establish. One CLI version and one error type, the weekly limit. `apiKeySource` is what the CLI reported; no invoice was checked, so what past naming calls cost is unknown. That the running bot's calls used the key is inferred from the same `config` load, not seen live. One earlier call on the limited account, key still present, returned no title and its cause was not captured.

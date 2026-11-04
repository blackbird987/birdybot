+++
id = "2026-09-08-bash-reads-were-not-a-fault-the-flag-was-wrong"
title = "Flagging Bash reads was wrong, and it was most of all flags"
date = "2026-09-08"
status = "stands"
question = "Is a session using Bash cat, head, sed -n, grep and find instead of the file tools a fault to flag?"
verdict = "Not under this bot as of 2026-09-08: provider.py passes bypassPermissions on every run and that mode's own text says to prefer Bash for those reads. The tool_hygiene check was about 73,000 of roughly 76,000 flags in 30 days and is retired. Loud does not mean wrong: a rule hard to follow is not deleted for that."
tags = ["evals"]
source = { file = "docs/design/prompt-review.md", section = "Frequency is not correctness" }
+++
A later session reading the eval output, or tempted to add a tool-use rule, would act on this. Second source: `bot/engine/eval.py`, checked: `_RETIRED_CATEGORIES` holds `tool_hygiene`, with a comment on why.

It fired 47,319 and 25,754 times in 30 days. The note's point is that deleting it for being loud would have been right for the wrong reason, so the weekly review classes each row: `contradicted` and `obsolete` may become edits, `disobeyed` is report only.

What this does not establish. That Bash reads are better: only that the flag contradicted the harness text. Mode text belongs to the CLI and can change, so it would need re-checking on an upgrade. It says nothing about any other eval category.

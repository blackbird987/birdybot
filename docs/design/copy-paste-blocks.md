# A copy-paste block must paste clean

Discord soft-wraps a long line to the phone's width by itself. A session that
hard-wraps the line *itself* — to "fit the phone" — bakes real newlines into
whatever the user pastes into their mail client, and they have to strip every
one by hand. On 2026-08-30 an email draft came out wrapped at 48 characters
inside a ``` fence; the newlines were in the message content, not the renderer.

- The rule lives in `WORKING_CONTEXT`'s Discord Formatting block
  (`bot/config.py`): inside a fence, one paragraph is one line, and a newline
  only ever appears where it is part of the content.
- Nothing unwraps fences on the way out, deliberately. A mechanical unwrap
  cannot tell an email paragraph from real code, an ASCII table or a diff, so
  it would mangle the cases it did not mean to touch.
- `eval._check_copy_block_wrapping` reports drift instead: a fence whose prose
  lines are consistently short and break mid-sentence is flagged, and the
  flag→owner map points at `WORKING_CONTEXT` so `/evals` names the block that
  was supposed to prevent it. Fences that look like code, tables or ASCII art
  are skipped — the check only fires on prose.
- Harness: `python scripts/test_copy_block_wrapping.py`

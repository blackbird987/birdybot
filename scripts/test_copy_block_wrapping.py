#!/usr/bin/env python3
"""Regression test: a copy-paste block must paste clean.

The incident (2026-08-30, thread 1543594886823944334): the bot drafted a Dutch
complaint email inside a ``` fence for the user to paste into their mail
client. Every paragraph came out hard-wrapped at ~48 characters:

    Het probleem: al tijdens de eerste training begon
    de binnenzool in de schoen te verschuiven. Bij de
    tweede training schoof hij zo sterk weg dat het
    pijn deed aan mijn tenen en ik niet normaal kon
    spelen.

Nothing in the bot wraps text -- there is no textwrap call and no width
constant anywhere in the send path. Pulling the raw message back off the
Discord API showed the newlines were in the *content*: the session had wrapped
it itself, to "fit the phone", and the user had to strip every one by hand
after pasting.

The fix is a rule in ``WORKING_CONTEXT``'s Discord Formatting block. Rules
drift silently, so ``eval._check_copy_block_wrapping`` reports the drift and
``attribute_flag`` points ``/evals`` at the block that was supposed to prevent
it. Nothing unwraps fences on the way out, deliberately: a mechanical unwrap
cannot tell an email paragraph from real code, a table or a diff.

Asserted here:

  * the rule is actually in the prompt block that ships to sessions
  * the real incident text is flagged, and its unwrapped form is not
  * the things a fence legitimately contains stay silent -- source code, a
    tagged fence, a block of shell commands whose lines all start lowercase,
    an ASCII table, a bullet list of genuinely short lines
  * the flag is attributed to WORKING_CONTEXT and does not steal the
    over-long/mobile rules' messages, whose order is load-bearing
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import config  # noqa: E402
from bot.engine import eval as ev  # noqa: E402

failures: list[str] = []
checks = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def flags_for(text: str) -> list:
    return ev._check_copy_block_wrapping(None, text)  # inst is unused


# The real thing, straight off the wire.
INCIDENT = """Here's the version aimed at the seller:

```
Beste PassaVolleybal,

Ik doe een beroep op mijn wettelijke rechten
wegens non-conformiteit voor onderstaande
bestelling.

- Model: Mizuno Wave Lightning Elite
- Ordernummer: W11000342974

Het probleem: al tijdens de eerste training begon
de binnenzool in de schoen te verschuiven. Bij de
tweede training schoof hij zo sterk weg dat het
pijn deed aan mijn tenen en ik niet normaal kon
spelen. De binnenzool blijft niet op zijn plaats
liggen.

De schoen is vier dagen oud en twee keer binnen
op een volleybalveld gebruikt. Slijtage is
uitgesloten; het gaat om een fabricagefout.

Met vriendelijke groet,
Quincy de Klerk
```

Send it to info@passasports.nl.
"""

CLEAN = """Here's the version aimed at the seller:

```
Beste PassaVolleybal,

Ik doe een beroep op mijn wettelijke rechten wegens non-conformiteit voor onderstaande bestelling.

- Model: Mizuno Wave Lightning Elite
- Ordernummer: W11000342974

Het probleem: al tijdens de eerste training begon de binnenzool in de schoen te verschuiven. Bij de tweede training schoof hij zo sterk weg dat het pijn deed aan mijn tenen en ik niet normaal kon spelen.

Met vriendelijke groet,
Quincy de Klerk
```
"""

# Untagged, prose-shaped, short lines that all continue lowercase -- and every
# one of those newlines is load-bearing. This is the shape a naive check eats.
SHELL = """Run these:

```
python scripts/botctl.py stop
python scripts/botctl.py start
python scripts/smoke_test.py
tail -n 50 data/logs/bot.log
```
"""

CODE_UNTAGGED = """```
def relogin_command(account_dir):
    p = Path(str(account_dir)).expanduser()
    return f'CLAUDE_CONFIG_DIR="{p}" claude'
```
"""

CODE_TAGGED = """```python
value = compute(x)
result = value + 1
answer = result * 2
final = answer - 3
```
"""

TABLE = """```
name        status     age
alpha       running    4h
beta        stopped    1d
gamma       running    12m
```
"""

SHORT_LIST = """```
- replace the insole
- refund the purchase price
- send a replacement pair
- reply within fourteen days
```
"""

print("Prompt rule")
block = config.WORKING_CONTEXT
check("WORKING_CONTEXT forbids hard-wrapping copy blocks",
      "Never hard-wrap text meant to be copied" in block)
check("it says why (the paste carries the newlines)",
      "bakes real newlines" in block)
check("it names the one legitimate newline", "part of the content" in block)
# The rule is worthless if the assembler stops handing the block to sessions.
runner_src = (Path(__file__).resolve().parent.parent
              / "bot" / "claude" / "runner.py").read_text(encoding="utf-8")
check("the prompt assembler still appends WORKING_CONTEXT",
      "parts.append(config.WORKING_CONTEXT)" in runner_src)

print("\nDetection")
hit = flags_for(INCIDENT)
check("the real incident text is flagged", len(hit) == 1,
      hit[0].message if hit else "no flag")
if hit:
    check("the flag counts the breaks", "mid-sentence" in hit[0].message)
    check("the flag carries evidence", bool(hit[0].evidence))
    check("severity is warning", hit[0].severity == "warning")
check("the same email unwrapped is silent", flags_for(CLEAN) == [])

print("\nFences that are not prose")
for label, sample in (
    ("a block of shell commands", SHELL),
    ("untagged source code", CODE_UNTAGGED),
    ("a ```python fence", CODE_TAGGED),
    ("an ASCII table", TABLE),
    ("a list of short bullets", SHORT_LIST),
):
    got = flags_for(sample)
    check(f"{label} is not flagged", got == [], got[0].message if got else "")

check("prose outside a fence is not flagged",
      flags_for(INCIDENT.replace("```", "")) == [])

print("\nSurvives the shapes a result file can arrive in")
# A result written with CRLF used to leave a "\r" on every line, so no line
# ended on a word and the whole check went silently dead.
check("CRLF line endings are still flagged",
      len(flags_for(INCIDENT.replace("\n", "\r\n"))) == 1)
two = INCIDENT + "\nAnd the tidy one:\n\n" + CLEAN
check("one wrapped fence beside a clean one flags exactly once",
      len(flags_for(two)) == 1, f"{len(flags_for(two))} flag(s)")

print("\nAttribution")
if hit:
    owner = ev.attribute_flag(hit[0].category, hit[0].message)
    check("flag is attributed to WORKING_CONTEXT", owner == "WORKING_CONTEXT", owner)
    # Order is load-bearing in _ATTRIBUTION: this rule sits above over-long,
    # which sits above the generic "mobile" rule. None may shadow another.
    check("it does not swallow the over-long message",
          ev.attribute_flag("constraint_violation",
                            "Over-long response (9,000 chars) — mobile target is ~1200")
          == "CHAT_APP_CONSTRAINT")
    check("it does not swallow the mobile message",
          ev.attribute_flag("constraint_violation", "not mobile friendly") == "MOBILE_HINT")

    # The message embeds the live count, and the digest only groups rows whose
    # numbers have been collapsed. Without this, every wrapped block would be
    # its own one-off row and none would ever reach the reporting threshold.
    check("differing break counts collapse to one digest row",
          ev.normalise_flag_message(hit[0].message)
          == ev.normalise_flag_message(hit[0].message.replace("(7 ", "(4 ")))

print()
if failures:
    print("FAIL: copy-paste blocks")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)

print(f"PASS: a copy-paste block pastes clean ({checks} checks).")
print("      The prompt rule ships in WORKING_CONTEXT, the real hard-wrapped")
print("      email is flagged and attributed to that block, and the fences a")
print("      session legitimately writes — code, shell commands, tables,")
print("      bullet lists — stay silent, so the check cannot train sessions")
print("      to stop fencing things.")

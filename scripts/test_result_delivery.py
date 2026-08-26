"""Regression test for how a finished answer reaches the thread.

Background: the bot posted a result inline only if it fit under 2000 chars —
one Discord message. Everything bigger collapsed to a card carrying the first
paragraph (<=500 chars) plus an Expand button. Measured against the 643 stored
result files, 568 of them (88%, median 4.4 KB) took that path, so collapsing
was the normal case rather than the exception, and tapping Expand then sliced
the text at 3900 chars anyway. Under all of it sat a silent hard cut at the
Discord embed cap that dropped content with no marker at all.

What this locks down:

  (a) A normal-sized answer arrives WHOLE, chunked across messages, not
      collapsed to a preview.
  (b) A result too big to post inline still gets a card worth reading —
      several paragraphs, and an honest count of what is behind the button.
  (c) Expand yields the FULL text as chunks, and only marks a cut when it
      genuinely ran out of chunks.
  (d) Code fences stay balanced across every split — an odd ``` renders the
      rest of the thread as monospace.
  (e) The last-resort embed cut announces itself and still respects the cap.

Calls the real production functions so the test can't drift.

Run: python scripts/test_result_delivery.py
Exit 0 = all pass, exit 1 = failures.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from bot import config
from bot.claude.types import Instance, InstanceStatus, InstanceType
from bot.discord.formatter import apply_discord_safety, chunk_message
from bot.platform.formatting import (
    _CUT_MARKER_ROOM,
    _leading_preview,
    format_expanded_result_chunks,
    format_inline_meta_line,
    format_result_md,
)

_failures: list[str] = []


def _check(label: str, cond: bool) -> None:
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}")
        _failures.append(label)


def _inst() -> Instance:
    return Instance(
        id="q-999", name=None, instance_type=InstanceType.QUERY,
        prompt="why is it truncating", repo_name="bot", repo_path=_ROOT,
        status=InstanceStatus.COMPLETED,
    )


def _para(n: int, word: str = "word") -> str:
    """A paragraph of roughly n chars, wrapped in short lines."""
    line = " ".join([word] * 12)
    out = []
    while len("\n".join(out)) < n:
        out.append(line)
    return "\n".join(out)


# ---- (a) a normal answer arrives whole ----
print("A normal-sized answer is posted inline, not collapsed")
_check(
    "inline ceiling is well past one Discord message",
    config.RESULT_INLINE_MAX >= 6000,
)
body = _para(4000)
_check("a 4 KB answer is under the inline ceiling", len(body) <= config.RESULT_INLINE_MAX)
chunks = chunk_message(body, limit=2000)
_check("it chunks into several full messages", len(chunks) >= 2)
_check(
    "chunking loses nothing but the newlines it split on",
    "".join(c.replace("\n", "") for c in chunks) == body.replace("\n", ""),
)
_check("every chunk fits a Discord message", all(len(c) <= 2000 for c in chunks))

# ---- (b) the collapse card stands on its own ----
print("A result too big to post inline still gets a readable card")
inst = _inst()
inst.summary = "First paragraph only."
huge = "\n\n".join(_para(900) for _ in range(12))
card = format_result_md(inst, preview=huge)
_check("card carries more than the one-paragraph summary", len(card) > 900)
_check(
    "card honours the preview budget",
    len(card) < config.RESULT_PREVIEW_MAX + 400,
)
_check("card says how much is behind the button", "more characters" in card)
_check("card still shows the instance id", "q-999" in card)

short_body = "Just one short answer."
short_card = format_result_md(inst, preview=short_body)
_check("a short preview is not marked as truncated", "more characters" not in short_card)
_check("no preview falls back to the stored summary", "First paragraph only." in format_result_md(inst))

_prev, dropped = _leading_preview("abc", 1200)
_check("preview under budget drops nothing", dropped == 0 and _prev == "abc")
_prev, dropped = _leading_preview("x " * 2000, 100)
_check("preview over budget reports what it dropped", dropped > 0 and len(_prev) <= 100)

# ---- (c) Expand yields the whole thing ----
print("Expand posts the full result, and only marks a genuine cut")
mid = "\n\n".join(_para(700) for _ in range(6))     # ~4.5 KB — over the old 3900 slice
ex = format_expanded_result_chunks(inst, mid)
_check("a result past the old 3900 slice needs more than one chunk", len(ex) >= 2)
_check("no truncation marker when it all fits", not any("/log q-999" in c for c in ex))
joined = "".join(ex).replace("\n", "")
_check(
    "expanded text contains the whole result",
    joined.endswith(mid.replace("\n", "")[-200:]),
)
_check("first chunk fits an embed", len(ex[0]) <= 4096)
_check("later chunks fit a message", all(len(c) <= 1900 for c in ex[1:]))
_check("first chunk carries the header", "q-999" in ex[0])

giant = "\n\n".join(_para(900) for _ in range(60))   # ~55 KB
ex2 = format_expanded_result_chunks(inst, giant, max_chunks=3)
_check("a giant result is capped at max_chunks", len(ex2) == 3)
_check("the cap is announced with a count", "more characters" in ex2[-1])
_check("the cap points at /log", "/log q-999" in ex2[-1])

# ---- (d) fences stay balanced ----
print("Code fences survive every split")
fenced = "intro\n\n```python\n" + _para(5000, "code") + "\n```\n\nafter"
exf = format_expanded_result_chunks(inst, fenced)
_check("expand keeps fences balanced", all(c.count("```") % 2 == 0 for c in exf))
_check("chunk_message keeps fences balanced", all(
    c.count("```") % 2 == 0 for c in chunk_message(fenced, limit=2000)
))
_check(
    "safety pass keeps fences balanced",
    apply_discord_safety(fenced, 4096).count("```") % 2 == 0,
)

# ---- (e) the last-resort cut is visible ----
print("The embed cap cut announces itself")
over = "z" * 9000
safe = apply_discord_safety(over, 4096)
_check("still respects the embed cap", len(safe) <= 4096)
_check("marks that it cut", "cut —" in safe)
_check("names how much it dropped", "more chars" in safe and "/log" in safe)
_check("text under the cap is untouched", apply_discord_safety("hello", 4096) == "hello")
_check(
    "content limit is respected too",
    len(apply_discord_safety(over, 2000)) <= 2000,
)

# ---- (f) the cut marker fits inside the budget it was sized for ----
print("The 'more characters' marker is paid for, not bolted on")
_check("marker room is reserved", _CUT_MARKER_ROOM >= 64)
capped = format_expanded_result_chunks(inst, giant, max_chunks=4)
_check("marked last chunk still fits a Discord message", len(capped[-1]) <= 2000)
one = format_expanded_result_chunks(inst, giant, first_budget=3900, max_chunks=1)
_check("single-chunk expand still fits an embed", len(one[0]) <= 4096)
_check("single-chunk expand is marked", "more characters" in one[0])
_check(
    "max_chunks below 1 is clamped, not an empty list",
    len(format_expanded_result_chunks(inst, mid, max_chunks=0)) == 1,
)

# ---- (g) an unbalanced fence never reaches the card ----
print("A preview that lands mid-code-block closes it")
fenced_head = "```python\n" + _para(4000, "x") + "\n```\n\ntail"
card_f = format_result_md(inst, preview=fenced_head)
_check("preview card has balanced fences", card_f.count("```") % 2 == 0)
_check(
    "the 'more characters' line is outside the code block",
    card_f.rfind("```") < card_f.find("more characters"),
)

# ---- (h) inline delivery keeps the run facts ----
print("An inline result still reports duration, tokens and cost")
inst.duration_ms = 92000
inst.input_tokens = 4000
inst.output_tokens = 130
inst.cost_usd = 0.0421
line = format_inline_meta_line(inst, "opus · worktree: claude-bot/t-1")
_check("meta line is one grey line", line.startswith("-# ") and "\n" not in line)
_check("carries duration", "1.5m" in line)
_check("carries tokens", "4.1k" in line)
_check("carries cost", "$0.0421" in line)
_check("carries where it ran", "worktree: claude-bot/t-1" in line)
_check("stays short enough to ignore", len(line) < 120)

blank = Instance(
    id="q-1", name=None, instance_type=InstanceType.QUERY, prompt="p",
    repo_name="bot", repo_path=_ROOT, status=InstanceStatus.COMPLETED,
)
_check(
    "a run with no numbers yet still produces a valid line",
    format_inline_meta_line(blank, "").startswith("-# "),
)

print()
if _failures:
    print(f"{len(_failures)} FAILURE(S):")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("All checks passed.")

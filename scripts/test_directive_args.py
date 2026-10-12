#!/usr/bin/env python3
"""Regression test: a quoted directive value may contain a closing bracket.

Every directive pattern used to end its arguments at the first ``]``, so
``[BOT_CMD: /spawn repo=bot title="[t-9201] Fix it"]`` parsed as
``repo=bot title="[t-9201`` and the spawn was refused as unparseable. Seven
parsers carried their own copy of the pattern and six carried the same
key=value regex. They now share ``textutil.DIRECTIVE_ARGS`` and
``DIRECTIVE_KV_RE``.

Asserted for /spawn, /reply, /chain, /wake, /watch, /image, /repo and the
display collapser: a bracket inside a quoted value survives, a plain directive
still parses, and an unbalanced quote degrades to the old cut-at-the-bracket
reading instead of swallowing the line. Also asserts the argument group stays
linear on an unclosed directive full of quotes: the non-atomic version took
37 seconds on twenty quoted words.

Run: python scripts/test_directive_args.py
Exit 0 = all pass.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import textutil  # noqa: E402
from bot.engine import images, lifecycle, watches  # noqa: E402
from bot.engine.directives import chain, dispatch, reply, spawn  # noqa: E402
from bot.engine.directives.common import parse_kv  # noqa: E402
from bot.platform import formatting  # noqa: E402

FAILS: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


TITLE = "[t-9201] Fix ]it"


def main() -> int:
    text = f'[BOT_CMD: /spawn repo=bot title="{TITLE}" mode=build]\n~~~spawn\nbrief\n~~~\n'
    pairs, no_body, over = spawn._pair_spawn_directives(text)
    kv = parse_kv(pairs[0][0]) if pairs else None
    check("/spawn keeps a bracketed title whole",
          kv == {"repo": "bot", "title": TITLE, "mode": "build"} and pairs[0][1] == "brief",
          f"{pairs} {no_body} {over}")

    pairs, _, _ = spawn._pair_spawn_directives(
        "[BOT_CMD: /spawn repo=bot title=Plain]\n~~~spawn\nb\n~~~\n")
    check("/spawn without brackets still parses", parse_kv(pairs[0][0]) == {"repo": "bot", "title": "Plain"})

    pairs, _, _ = spawn._pair_spawn_directives(
        '[BOT_CMD: /spawn repo=bot title="open]\n~~~spawn\nb\n~~~\n')
    check("an unbalanced quote cuts at the bracket, as before",
          bool(pairs) and pairs[0][0] == 'repo=bot title="open', str(pairs))

    pairs, _, _ = reply._pair_reply_directives(
        "[BOT_CMD: /reply thread=\"123\"]\n~~~reply\nanswer\n~~~\n")
    check("/reply parses a quoted thread", parse_kv(pairs[0][0]) == {"thread": "123"}, str(pairs))

    parsed = chain._extract_chain_directive(
        '[BOT_CMD: /chain preset=hold note="[x] y"]\n~~~plan\nthe plan\n~~~\n')
    check("/chain reads its preset past a bracketed value", parsed == ("hold", "the plan"), str(parsed))
    check("/chain with no args still parses",
          chain._extract_chain_directive("[BOT_CMD: /chain]\n~~~plan\np\n~~~") == (None, "p"))

    wake = lifecycle._parse_wake_directive(
        f'[BOT_CMD: /wake delay=90m reason="{TITLE}"]\n~~~wake\ncheck it\n~~~\n')
    check("/wake keeps a bracketed reason",
          bool(wake) and wake["reason"] == TITLE and wake["delay_secs"] == 5400, str(wake))

    watch = watches.parse_watch_directive(
        f'[BOT_CMD: /watch pid=42 label="{TITLE}" progress="(\\d+)/(\\d+)"]\n~~~watch\nread it\n~~~\n')
    check("/watch keeps a bracketed label", bool(watch) and watch["label"] == TITLE and watch["pid"] == 42,
          str(watch))

    imgs = images.parse_image_directives(f'[BOT_CMD: /image path="a.png" caption="{TITLE}"]\n')
    check("/image keeps a bracketed caption", imgs == [("a.png", TITLE)], str(imgs))
    check("/image bare form still parses",
          images.parse_image_directives("[BOT_CMD: /image docs/a.png]\n") == [("docs/a.png", None)])

    m = dispatch._BOT_CMD_RE.search('[BOT_CMD: /repo add "my [app]" /tmp/x]')
    check("/repo keeps a bracketed quoted name", bool(m) and m.group(1) == 'add "my [app]" /tmp/x',
          m.group(1) if m else "no match")

    shown = formatting.collapse_bot_directives(
        f'before\n[BOT_CMD: /spawn repo=bot title="{TITLE}"]\n~~~spawn\nbrief\n~~~\nafter')
    check("the collapser renders the whole title and swallows the body",
          TITLE in shown and "brief" not in shown and "[BOT_CMD" not in shown, shown)

    check("lenient kv parsing ignores junk",
          textutil.directive_kv('a=1 junk b="x y"') == {"a": "1", "b": "x y"})

    pattern = textutil.directive_re("spawn")
    unclosed = "[BOT_CMD: /spawn " + " ".join(['"a"'] * 40)
    t0 = time.monotonic()
    pattern.search(unclosed)
    took = time.monotonic() - t0
    check("an unclosed directive full of quotes is rejected in linear time", took < 0.5, f"{took:.2f}s")

    print()
    print(f"{len(FAILS)} failure(s)" if FAILS else "All checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())

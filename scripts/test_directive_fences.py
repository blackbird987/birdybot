#!/usr/bin/env python3
"""Regression test: a directive inside a ``` fence is an example, never a request.

/wake and /watch always skipped a directive sitting inside an open ``` fence.
/image, /repo, /spawn, /reply and /chain only checked the directive's own line,
so an example indented inside a code block (the line opens with spaces, not a
backtick) was dispatched for real: a reply explaining how /spawn works could
open a thread. All seven now share ``textutil.is_quoted_example``, and the
display collapser uses the same check, so what renders as a handled chip is
exactly what was acted on.

For every directive this asserts: a fenced example is skipped, a real directive
after a closed fence still works, and a fence inside another directive's tilde
body neither counts nor hides anything.

Run: python scripts/test_directive_fences.py
Exit 0 = all pass.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.engine import commands, images, lifecycle, watches  # noqa: E402
from bot.platform.formatting import collapse_bot_directives  # noqa: E402
from bot.textutil import is_quoted_example, mask_tilde_bodies  # noqa: E402

_failures: list[str] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"PASS  {name}")
    else:
        print(f"FAIL  {name}" + (f"  ({detail})" if detail else ""))
        _failures.append(name)


FENCE = "```"


def _fenced(directive: str, body: str = "") -> str:
    """An example the way a session writes one: indented inside a code block."""
    lines = [f"Here is how it works:\n\n{FENCE}text", f"    {directive}"]
    if body:
        lines.append(body)
    lines.append(FENCE)
    return "\n".join(lines) + "\n\nThat is all."


def _after_fence(directive: str, body: str = "") -> str:
    """A real directive below a closed example fence."""
    text = f"Example:\n\n{FENCE}\nprint('hi')\n{FENCE}\n\n{directive}"
    return text + (f"\n{body}" if body else "")


def check_guard() -> None:
    text = f"a\n{FENCE}\n    [BOT_CMD: /x]\n{FENCE}\n[BOT_CMD: /y]\nsee `[BOT_CMD: /z]`\n> [BOT_CMD: /q]"
    _check("guard: inside an open fence", is_quoted_example(text, text.index("[BOT_CMD: /x]")))
    _check("guard: after a closed fence is real", not is_quoted_example(text, text.index("[BOT_CMD: /y]")))
    _check("guard: inline backtick", is_quoted_example(text, text.index("[BOT_CMD: /z]")))
    _check("guard: block quote", is_quoted_example(text, text.index("[BOT_CMD: /q]")))
    # A ``` inside another directive's tilde body must not flip the count.
    tilde = "~~~spawn\nuse " + FENCE + " here\n~~~\n[BOT_CMD: /y]"
    scan = mask_tilde_bodies(tilde)
    _check("guard: a fence inside a tilde body does not count",
           not is_quoted_example(scan, scan.index("[BOT_CMD: /y]")))


def check_image() -> None:
    d = '[BOT_CMD: /image path="docs/a.png"]'
    _check("/image: fenced example skipped", images.parse_image_directives(_fenced(d)) == [])
    _check("/image: real one after a fence kept",
           images.parse_image_directives(_after_fence(d)) == [("docs/a.png", None)])


def check_spawn() -> None:
    d = '[BOT_CMD: /spawn repo=bot title="T"]'
    body = "~~~spawn\nDo the thing.\n~~~"
    pairs, no_body, _ = commands._pair_spawn_directives(_fenced(d, body))
    _check("/spawn: fenced example skipped", pairs == [] and no_body == 0, f"{pairs} {no_body}")
    pairs, _, _ = commands._pair_spawn_directives(_after_fence(d, body))
    _check("/spawn: real one after a fence kept", len(pairs) == 1, str(pairs))


def check_reply() -> None:
    d = "[BOT_CMD: /reply thread=123]"
    body = "~~~reply\nUse option B.\n~~~"
    pairs, no_body, _ = commands._pair_reply_directives(_fenced(d, body))
    _check("/reply: fenced example skipped", pairs == [] and no_body == 0, f"{pairs} {no_body}")
    pairs, _, _ = commands._pair_reply_directives(_after_fence(d, body))
    _check("/reply: real one after a fence kept", len(pairs) == 1, str(pairs))


def check_chain() -> None:
    d = "[BOT_CMD: /chain preset=ship]"
    body = "~~~plan\nChange the thing.\n~~~"
    _check("/chain: fenced example skipped",
           commands._extract_chain_directive(_fenced(d, body)) is None)
    got = commands._extract_chain_directive(_after_fence(d, body))
    _check("/chain: real one after a fence kept",
           got is not None and got[1] == "Change the thing.", str(got))


def check_wake_and_watch() -> None:
    wake = "[BOT_CMD: /wake delay=10m]"
    wbody = "~~~wake\nCheck the job.\n~~~"
    _check("/wake: fenced example skipped", lifecycle._parse_wake_directive(_fenced(wake, wbody)) is None)
    _check("/wake: real one after a fence kept",
           lifecycle._parse_wake_directive(_after_fence(wake, wbody)) is not None)
    watch = '[BOT_CMD: /watch pid=1 label="x"]'
    tbody = "~~~watch\nRead the log.\n~~~"
    _check("/watch: fenced example skipped", watches.parse_watch_directive(_fenced(watch, tbody)) is None)
    _check("/watch: real one after a fence kept",
           watches.parse_watch_directive(_after_fence(watch, tbody)) is not None)


async def check_repo() -> None:
    calls: list[str] = []

    async def _on_repo(ctx, args):  # noqa: ANN001
        calls.append(args)

    class _Messenger:
        async def send_text(self, *a, **kw):  # noqa: ANN002, ANN003
            return None

    class _Ctx:
        messenger = _Messenger()
        channel_id = "c1"

    saved = commands.on_repo
    commands.on_repo = _on_repo
    try:
        d = "[BOT_CMD: /repo switch bot]"
        await commands._execute_bot_commands(_Ctx(), _fenced(d))  # type: ignore[arg-type]
        _check("/repo: fenced example skipped", calls == [], str(calls))
        await commands._execute_bot_commands(_Ctx(), f"Try `{d}` in prose.")  # type: ignore[arg-type]
        _check("/repo: inline-code example skipped", calls == [], str(calls))
        await commands._execute_bot_commands(_Ctx(), _after_fence(d))  # type: ignore[arg-type]
        _check("/repo: real one after a fence kept", calls == ["switch bot"], str(calls))
    finally:
        commands.on_repo = saved


def check_collapse_parity() -> None:
    d = '[BOT_CMD: /spawn repo=bot title="T"]'
    body = "~~~spawn\nDo the thing.\n~~~"
    fenced = _fenced(d, body)
    _check("collapse: a fenced example is left verbatim", collapse_bot_directives(fenced) == fenced)
    real = collapse_bot_directives(_after_fence(d, body))
    _check("collapse: a real one after a fence still collapses",
           "[BOT_CMD:" not in real and "Do the thing." not in real, real)


def main() -> int:
    check_guard()
    check_image()
    check_spawn()
    check_reply()
    check_chain()
    check_wake_and_watch()
    asyncio.run(check_repo())
    check_collapse_parity()
    print()
    if _failures:
        print(f"FAIL: {len(_failures)} directive-fence check(s) failed")
        return 1
    print("PASS: every directive skips a fenced example and keeps a real one.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

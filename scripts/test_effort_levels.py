#!/usr/bin/env python3
"""Regression test: the bot offers every effort level the Claude CLI accepts.

The bot used to offer four levels (low, medium, high, max) after the CLI had
added a fifth (xhigh). The buttons, /effort and the /spawn effort= key each
kept their own copy of the list, so nothing noticed. Now there is one list
(bot/platform/modes.py EFFORT_DISPLAY) and this test pins:

  - every reader uses that list (buttons, /effort, /spawn effort=, /help,
    the /effort slash hint),
  - the texts that cannot import it (config.py prompts, README) name every
    level, in order,
  - the list matches what `claude --help` says --effort accepts
    (skipped, not failed, when the CLI is not on PATH).

Run: python scripts/test_effort_levels.py
"""
from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import config  # noqa: E402
from bot.engine import admin_cmds, settings_cmds  # noqa: E402
from bot.engine.directives import spawn as spawn_mod  # noqa: E402
from bot.platform import modes  # noqa: E402
from bot.platform.formatting import EFFORT_DISPLAY, VALID_EFFORTS  # noqa: E402

failures: list[str] = []
checks = 0
_root = Path(__file__).resolve().parent.parent


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" · {detail}" if detail else ""))
    if not ok:
        failures.append(label)


LEVELS = list(EFFORT_DISPLAY)

print("one list")
check("xhigh is offered", "xhigh" in LEVELS, str(LEVELS))
check("ordered lowest to highest",
      LEVELS == ["low", "medium", "high", "xhigh", "max"], str(LEVELS))
check("VALID_EFFORTS is the same set", VALID_EFFORTS == set(LEVELS))
check("usage line is built from it", modes.EFFORT_USAGE == "|".join(LEVELS),
      modes.EFFORT_USAGE)
check("/effort reads it", tuple(settings_cmds._VALID_EFFORT) == tuple(LEVELS),
      str(settings_cmds._VALID_EFFORT))
check("/spawn effort= reads it",
      tuple(spawn_mod._SPAWN_ALLOWED_EFFORTS) == tuple(LEVELS),
      str(spawn_mod._SPAWN_ALLOWED_EFFORTS))
check("every level fits one Discord button row (max 5)", len(LEVELS) <= 5,
      str(len(LEVELS)))

print()
print("the buttons")
try:
    from bot.discord.channels import session_controls_view
except Exception as e:  # discord not importable here
    print(f"  SKIP  welcome-embed buttons ({e})")
else:
    async def _effort_buttons() -> list[tuple[str, str, int, bool]]:
        view = session_controls_view("explore", "xhigh")
        return [(b.custom_id, b.label, b.row, b.disabled) for b in view.children
                if str(b.custom_id).startswith("effort_set:")]
    btns = asyncio.run(_effort_buttons())
    check("one button per level, in order",
          [c.split(":", 1)[1] for c, *_ in btns] == LEVELS, str(btns))
    check("all on the same row", len({r for _, _, r, _ in btns}) == 1, str(btns))
    check("xhigh button reads 'Extra High'",
          any(c == "effort_set:xhigh" and lbl == "Extra High" for c, lbl, _, _ in btns))
    check("the current level (xhigh) is the disabled one",
          [c for c, _, _, d in btns if d] == ["effort_set:xhigh"], str(btns))

print()
print("/effort accepts every level")


class _Messenger:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, _chan, text, **_kw):
        self.sent.append(text)

    def markdown_to_markup(self, text: str) -> str:
        return text


class _Ctx:
    def __init__(self) -> None:
        self.messenger = _Messenger()
        self.channel_id = 1
        self.effort: str | None = None
        self.effective_effort = "high"

    def update_effort(self, value: str) -> None:
        self.effort = value


for level in LEVELS:
    ctx = _Ctx()
    asyncio.run(settings_cmds.on_effort(ctx, level.upper()))
    check(f"/effort {level}", ctx.effort == level, str(ctx.messenger.sent))
ctx = _Ctx()
asyncio.run(settings_cmds.on_effort(ctx, "ultra"))
check("an unknown level is refused with the full usage line",
      ctx.effort is None and ctx.messenger.sent
      and modes.EFFORT_USAGE in ctx.messenger.sent[-1], str(ctx.messenger.sent))

ctx = _Ctx()
asyncio.run(admin_cmds.on_help(ctx))
check("/help lists every level", bool(ctx.messenger.sent)
      and f"reasoning effort ({modes.EFFORT_USAGE})" in ctx.messenger.sent[-1])
check("/effort slash hint reads the list",
      "describe(level=EFFORT_USAGE)" in (_root / "bot/discord/slash_commands.py").read_text())

print()
print("texts that spell the levels out name every one")
_spelled = (
    ("BOT_CONTEXT /effort line", config.BOT_CONTEXT, r"/effort ([a-z|]+) "),
    ("SPAWN_CONTEXT effort key", config.SPAWN_CONTEXT, r"`effort` \(optional\): ([a-z| ]+)\."),
    ("README", (_root / "README.md").read_text(), r"/effort ([a-z|]+) "),
)
for name, text, pattern in _spelled:
    m = re.search(pattern, text)
    found = re.findall(r"[a-z]+", m.group(1)) if m else []
    check(name, found == LEVELS, str(found))

print()
print("matches the installed CLI")
_cli = shutil.which("claude")
if not _cli:
    print("  SKIP  claude not on PATH")
else:
    try:
        out = subprocess.run([_cli, "--help"], capture_output=True, text=True,
                             timeout=30).stdout
    except Exception as e:
        out = ""
        print(f"  SKIP  claude --help failed ({e})")
    # Only the --effort option's own description: up to the next option line.
    m = re.search(r"--effort <level>((?:.|\n)*?)(?=\n\s*-|\Z)", out)
    m = m and re.search(r"\(([a-z, ]+)\)", m.group(1))
    if out and not m:
        print("  SKIP  --effort not described in claude --help")
    elif m:
        cli_levels = [w.strip() for w in m.group(1).split(",")]
        check("bot levels == CLI levels", cli_levels == LEVELS,
              f"cli={cli_levels} bot={LEVELS}")

print()
if failures:
    print("FAIL: effort levels")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print(f"PASS: one list of effort levels, used everywhere, matching the CLI ({checks} checks).")

"""Regression test: a held-back unattended turn posts no "[TURN_COMPLETE]" card.

Background (2026-10-13, vin-coach, q-19872 and q-19905): a scheduled nudge
that decided to stay silent replied with nothing but ``[TURN_COMPLETE]``.
The display copy strips the marker, which left it empty, and ``send_result``
fell back to a summary card built from ``inst.summary``. That summary came
from ``extract_summary``, which never stripped the marker, so a thread read by
a non-developer got a card reading "[TURN_COMPLETE]" plus build buttons.

Locks two things:
  * ``extract_summary`` never carries the marker into the summary.
  * ``send_result`` posts nothing for a marker-only unattended turn that
    succeeded, and keeps today's card for every other shape (user-typed,
    failed, empty without the marker, real text).

Calls the real ``lifecycle.send_result`` with a recording messenger.

Run: python scripts/test_marker_only_result.py
Exit 0 = all pass, exit 1 = failures.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.claude.parser import extract_summary  # noqa: E402
from bot.claude.types import (  # noqa: E402
    Instance,
    InstanceStatus,
    InstanceType,
    RunResult,
)
from bot.engine import lifecycle  # noqa: E402

_failures: list[str] = []


def _check(label: str, cond: bool) -> None:
    if cond:
        print(f"  ok:   {label}")
    else:
        _failures.append(label)
        print(f"  FAIL: {label}")


class _Messenger:
    def __init__(self) -> None:
        self.posts: list[str] = []

    def markdown_to_markup(self, text: str) -> str:
        return text

    def chunk_message(self, text: str) -> list[str]:
        return [text]

    def format_mention(self, uid: str) -> str:
        return f"<@{uid}>"

    async def send_result(self, channel_id, markup, **kw):  # noqa: ANN001, ANN003
        self.posts.append(markup)
        return f"m{len(self.posts)}"

    async def send_text(self, channel_id, text, *a, **kw):  # noqa: ANN001, ANN002, ANN003
        self.posts.append(text)
        return f"m{len(self.posts)}"


class _Store:
    def __init__(self) -> None:
        self.updates = 0

    def get_autopilot_chain(self, _sid):  # noqa: ANN001
        return None

    def update_instance(self, inst, critical: bool = False) -> None:  # noqa: ANN001
        self.updates += 1


def _send(source: str, raw: str, *, status=InstanceStatus.COMPLETED,
          is_error: bool = False):
    inst = Instance(
        id="q-99301",
        name=None,
        instance_type=InstanceType.QUERY,
        prompt="End-of-day nudge.",
        repo_name="harness",
        repo_path=tempfile.gettempdir(),
        status=status,
        mode="build",
        error="boom" if status == InstanceStatus.FAILED else None,
    )
    inst.summary = extract_summary(raw)
    ctx = SimpleNamespace(
        source=source, store=_Store(), messenger=_Messenger(),
        platform="discord", user_id="1", channel_id="c1",
    )
    result = RunResult(result_text=raw, is_error=is_error)
    display = lifecycle.collapse_bot_directives(
        lifecycle.strip_verify_blocks(raw)
    )
    asyncio.run(lifecycle.send_result(ctx, inst, display, result=result))
    return ctx, inst


print("Summary never carries the marker")
_check("marker-only reply -> empty summary",
       extract_summary("[TURN_COMPLETE]") == "")
_check("marker-only reply with whitespace -> empty summary",
       extract_summary("\n  [TURN_COMPLETE]  \n") == "")
s = extract_summary("Logged the held-back nudge.\n\n[TURN_COMPLETE]")
_check("text kept, marker dropped",
       s == "Logged the held-back nudge." and "[TURN_COMPLETE]" not in s)
_check("marker inline in first paragraph dropped",
       "[TURN_COMPLETE]" not in extract_summary("Done. [TURN_COMPLETE]"))

print("Marker-only unattended turns post nothing")
for src in ("wake", "cooldown"):
    ctx, inst = _send(src, "[TURN_COMPLETE]")
    _check(f"{src}: no message posted", ctx.messenger.posts == [])
    _check(f"{src}: instance still persisted", ctx.store.updates == 1)
    _check(f"{src}: no message ids recorded", not inst.message_ids.get("discord"))

print("Every other shape keeps its card")
ctx, _ = _send("user_message", "[TURN_COMPLETE]")
_check("user-typed marker-only turn still posts",
       len(ctx.messenger.posts) == 1)
_check("...and its card does not show the marker",
       "[TURN_COMPLETE]" not in ctx.messenger.posts[0])

ctx, _ = _send("wake", "[TURN_COMPLETE]", status=InstanceStatus.FAILED,
               is_error=True)
_check("failed wake turn still posts its failure card",
       len(ctx.messenger.posts) == 1 and "Failed" in ctx.messenger.posts[0])

ctx, _ = _send("wake", "")
_check("empty wake turn without the marker still posts",
       len(ctx.messenger.posts) == 1)

ctx, _ = _send("wake", "What did you get done on the re-edits?\n\n[TURN_COMPLETE]")
_check("wake turn with a real question posts it",
       len(ctx.messenger.posts) == 1
       and "re-edits" in ctx.messenger.posts[0]
       and "[TURN_COMPLETE]" not in ctx.messenger.posts[0])

ctx, _ = _send("wake", "Quoting it: `[TURN_COMPLETE]`")
_check("inline-code quote is not a silence signal",
       len(ctx.messenger.posts) == 1)


if _failures:
    print(f"\n{len(_failures)} case(s) FAILED:")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("\nAll cases passed.")

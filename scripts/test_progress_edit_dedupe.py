"""Regression test: the live progress card never re-sends an identical edit.

The incident (data/logs/bot.log, 2026-10-05 to 2026-10-09). 8,514 PATCHes to
progress cards came back 429, about 2,175 of them on 2026-10-08 alone, and on
five sampled cards the 429s started 66 to 77 minutes after the card was
posted: Discord rate limits edits to hour-old messages much harder. discord.py
retries each 429 inline, and ``on_progress`` is awaited inline by the runner's
stream reader, so the backoff also stalled reading the CLI's output.

Two things made every edit a real one:

  * ``_edit`` only skipped a no-op when ``not buttons``, and every caller
    passes the Stop button, so the dedupe never fired.
  * the header's clock read "65.3m", which changes every 6s, so the 10s
    heartbeat always had new text to send.

Asserted here:

  * an idle session's heartbeat sends one edit per displayed clock value:
    once per tick in the first minute, once per minute after it
  * the clock renders "Ns", then whole minutes, then "1h02m"
  * an identical on_progress is skipped; a changed activity is sent
  * a stall edit (different text and buttons) is sent, and so is the
    recovery back to the normal header
  * an edit that raised is not recorded as sent, so the next identical
    edit retries it
  * an identical edit arriving while the first is still in flight (stuck
    in a 429 backoff) is skipped, not queued behind it
  * the finished card's clock reads like the live one

Strategy: swap ``lifecycle.asyncio`` for a stand-in with a fake clock, so the
real closures run and an hour of heartbeat ticks takes milliseconds.

Run: ``python scripts/test_progress_edit_dedupe.py``  (exit 0 on pass).
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.engine import commands, lifecycle  # noqa: E402
from bot.engine.lifecycle import make_progress_callbacks  # noqa: E402
from bot.claude.types import Instance, InstanceStatus, InstanceType  # noqa: E402
from bot.platform.formatting import format_elapsed  # noqa: E402


class _StopHeartbeat(Exception):
    pass


class _Clock:
    """Stands in for the asyncio module inside lifecycle."""

    def __init__(self) -> None:
        self.now = 0.0
        self.stop_at: float | None = None

    # lifecycle calls asyncio.get_event_loop().time()
    def get_event_loop(self):
        return self

    def time(self) -> float:
        return self.now

    async def sleep(self, secs: float) -> None:
        self.now += secs
        if self.stop_at is not None and self.now > self.stop_at:
            raise _StopHeartbeat

    def create_task(self, coro):
        return asyncio.get_running_loop().create_task(coro)


class _Messenger:
    def __init__(self) -> None:
        self.edits: list[tuple[float, str, object]] = []
        self.attempts = 0
        self.fail_next = 0
        self.gate: asyncio.Event | None = None
        self.clock: _Clock | None = None

    def escape(self, text: str) -> str:
        return text

    async def edit_thinking(self, handle, text, buttons=None, *, footer=None,
                            severity=None):
        self.attempts += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.fail_next:
            self.fail_next -= 1
            raise RuntimeError("simulated Discord failure")
        self.edits.append((self.clock.now, text, buttons))


def _instance() -> Instance:
    return Instance(
        id="t-dedupe",
        name=None,
        instance_type=InstanceType.TASK,
        prompt="x",
        repo_name="bot",
        repo_path="",
        status=InstanceStatus.RUNNING,
        mode="build",
    )


def _build(clock: _Clock):
    messenger = _Messenger()
    messenger.clock = clock
    ctx = SimpleNamespace(messenger=messenger)
    on_progress, on_stall, heartbeat, *_ = make_progress_callbacks(
        ctx, _instance(), {"channel_id": "1", "message_id": "2"}, 1,
    )
    return messenger, on_progress, on_stall, heartbeat


async def _run_heartbeat(heartbeat, clock: _Clock, until: float) -> None:
    clock.stop_at = until
    try:
        await heartbeat()
    except _StopHeartbeat:
        pass
    clock.stop_at = None


async def _amain() -> int:
    failures: list[str] = []
    saved = lifecycle.asyncio
    clock = _Clock()
    lifecycle.asyncio = clock
    try:
        # 1. Idle heartbeat: one edit per distinct clock value.
        messenger, _, _, heartbeat = _build(clock)
        await _run_heartbeat(heartbeat, clock, until=3 * 3600)
        ticks = int((3 * 3600 - 3) // 10) + 1
        shown = [text.rsplit("(", 1)[1].rstrip(")") for _, text, _ in messenger.edits]
        if len(shown) != len(set(shown)):
            failures.append(
                "the heartbeat re-sent a card whose text had not changed: "
                f"{[s for s in shown if shown.count(s) > 1][:5]}"
            )
        late = [t for t, _, _ in messenger.edits if t >= 60]
        # Ticks at t >= 60 cover minutes 1..179: one edit per minute shown.
        if not 170 <= len(late) <= 180:
            failures.append(
                f"an idle card past its first minute was edited {len(late)} "
                f"times over ~179 minutes (expected about once a minute, "
                f"out of {ticks} heartbeat ticks)"
            )
        for want in ("13s", "1m", "59m", "1h00m", "1h02m", "2h59m"):
            if want not in shown:
                failures.append(f"clock never rendered {want!r}: {shown[:3]}...")
        if any("." in s for s in shown):
            failures.append(f"clock still renders decimals: {shown[:8]}")

        # 2. on_progress: identical skipped, changed activity sent.
        clock.now = 0.0
        messenger, on_progress, on_stall, heartbeat = _build(clock)
        clock.now = 600.0  # "10m", well past the 5s throttle
        await on_progress("Reading foo.py")
        clock.now = 610.0
        await on_progress("Reading foo.py")
        if len(messenger.edits) != 1:
            failures.append(
                f"an identical on_progress was sent again ({len(messenger.edits)} edits)"
            )
        clock.now = 620.0
        await on_progress("Editing bar.py")
        if len(messenger.edits) != 2 or "Editing bar.py" not in messenger.edits[-1][1]:
            failures.append("a changed activity was not sent")

        # 3. Stall then recovery both reach the card.
        clock.now = 630.0
        await on_stall("t-dedupe")
        if len(messenger.edits) != 3 or "quiet for" not in messenger.edits[-1][1]:
            failures.append("the stall edit was skipped")
        stall_buttons = messenger.edits[-1][2]
        clock.now = 640.0
        await on_progress("Editing bar.py")
        if len(messenger.edits) != 4:
            failures.append(
                "recovering from a stall to the same activity did not restore "
                "the normal header"
            )
        elif messenger.edits[-1][2] == stall_buttons:
            failures.append("the recovered card kept the stall buttons")

        # 4. A failed edit is retried by the next identical call.
        clock.now = 0.0
        messenger, on_progress, _, _ = _build(clock)
        messenger.fail_next = 1
        clock.now = 300.0
        await on_progress("Running tests")
        clock.now = 306.0
        await on_progress("Running tests")
        clock.now = 312.0
        await on_progress("Running tests")
        if messenger.attempts != 2 or len(messenger.edits) != 1:
            failures.append(
                f"after one failed edit: {messenger.attempts} attempts and "
                f"{len(messenger.edits)} successful edits (expected 2 and 1: "
                "retry once, then skip the identical third)"
            )

        # 5. An identical edit is skipped while the first is still in flight.
        clock.now = 0.0
        messenger, on_progress, _, _ = _build(clock)
        messenger.gate = asyncio.Event()
        clock.now = 300.0
        first = asyncio.create_task(on_progress("Running tests"))
        for _ in range(5):
            await asyncio.sleep(0)
        clock.now = 306.0
        # A task, not an await: if it were sent it would park on the same
        # gate, and the test must fail rather than hang.
        second = asyncio.create_task(on_progress("Running tests"))
        for _ in range(5):
            await asyncio.sleep(0)
        attempts_in_flight = messenger.attempts
        messenger.gate.set()
        await asyncio.gather(first, second)
        if attempts_in_flight != 1:
            failures.append(
                f"an identical edit was sent behind one in flight: "
                f"{attempts_in_flight} attempts (expected 1)"
            )
    finally:
        lifecycle.asyncio = saved

    # 6. The clock, including the finished card's.
    for secs, want in ((0, "0s"), (59.7, "59s"), (60, "1m"), (3599, "59m"),
                       (3600, "1h00m"), (3 * 3600 + 5 * 60, "3h05m")):
        got = format_elapsed(secs)
        if got != want:
            failures.append(f"format_elapsed({secs}) = {got!r}, expected {want!r}")
    # Both writers of the finished card: the chain/retry path and a chat turn.
    for fn in (lifecycle.run_instance, commands._execute_query):
        src = inspect.getsource(fn)
        if "format_elapsed(" not in src or ".1f}m" in src:
            failures.append(
                f"{fn.__module__}.{fn.__name__} does not render the finished "
                "card with format_elapsed"
            )

    if failures:
        print("FAIL: progress card edit dedupe")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("PASS: the progress card is edited only when what it shows changes;")
    print("      an idle card past its first minute is edited once a minute.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_amain()))

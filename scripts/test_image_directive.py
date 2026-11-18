"""Harness for the [BOT_CMD: /image] path — parsing, path safety, delivery.

Run: python scripts/test_image_directive.py

The path rules are the point: without them "share a picture" becomes "read any
file on the host and publish it to Discord". Each case below is one way that
could go wrong.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import config  # noqa: E402
from bot.engine.images import (  # noqa: E402
    MAX_BATCH_BYTES,
    MAX_FILES_PER_MESSAGE,
    MAX_IMAGES_PER_RESPONSE,
    ImageFailure,
    deliver_images,
    failure_note,
    pack_messages,
    parse_image_directives,
    resolve_image,
)
from bot.platform.formatting import collapse_bot_directives  # noqa: E402

PASS, FAIL = 0, 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}{(' — ' + detail) if detail else ''}")


class FakeInstance:
    def __init__(self, repo_path: str, worktree_path: str | None = None):
        self.repo_path = repo_path
        self.worktree_path = worktree_path


class FakeMessenger:
    def __init__(self, fail_on: int | None = None, empty_on: int | None = None):
        self.batches: list[tuple[list[str], str | None]] = []
        self.texts: list[str] = []
        self._calls = 0
        self._fail_on = fail_on      # 1-based upload that raises
        self._empty_on = empty_on    # 1-based upload that posts nothing

    async def send_files(self, channel_id, file_paths, caption=None):
        self._calls += 1
        if self._calls == self._fail_on:
            raise RuntimeError("413 Payload Too Large")
        if self._calls == self._empty_on:
            return []
        self.batches.append((list(file_paths), caption))
        return ["1"]

    async def send_text(self, channel_id, text, buttons=None, silent=False):
        self.texts.append(text)
        return "2"


class FakeCtx:
    def __init__(self, messenger):
        self.messenger = messenger
        self.channel_id = "chan"
        self.store = None


def png_bytes() -> bytes:
    """Smallest valid PNG — 1x1 transparent pixel."""
    import base64
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQ"
        "DwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="imgtest_"))
    repo = tmp / "repo"
    (repo / "docs").mkdir(parents=True)
    outside = tmp / "secrets"
    outside.mkdir()

    good = repo / "docs" / "diagram.png"
    good.write_bytes(png_bytes())
    (repo / "notes.txt").write_text("not an image")
    secret = outside / "id_rsa.png"        # image extension, wrong location
    secret.write_bytes(png_bytes())
    big = repo / "huge.png"
    big.write_bytes(b"\x89PNG" + b"\0" * 9_000_000)
    empty = repo / "empty.png"
    empty.write_bytes(b"")

    inst = FakeInstance(str(repo))

    print("\nparsing")
    parsed = parse_image_directives(
        'text before\n[BOT_CMD: /image path="docs/diagram.png" caption="A diagram"]\nafter'
    )
    check("kv form parses path + caption",
          parsed == [("docs/diagram.png", "A diagram")], repr(parsed))

    parsed = parse_image_directives("[BOT_CMD: /image docs/diagram.png]")
    check("bare form parses path",
          parsed == [("docs/diagram.png", None)], repr(parsed))

    parsed = parse_image_directives(
        "> [BOT_CMD: /image docs/diagram.png]\n`[BOT_CMD: /image x.png]`"
    )
    check("quoted/code examples never dispatch", parsed == [], repr(parsed))

    parsed = parse_image_directives("inline [BOT_CMD: /image x.png] mid-sentence")
    check("mid-sentence directive ignored", parsed == [], repr(parsed))

    print("\npath safety")
    p, why = resolve_image("docs/diagram.png", inst)
    check("relative path inside repo accepted", p == good.resolve(), why)

    p, why = resolve_image(str(good), inst)
    check("absolute path inside repo accepted", p == good.resolve(), why)

    p, why = resolve_image("../secrets/id_rsa.png", inst)
    check("`..` escape refused", p is None, f"accepted {p}")

    p, why = resolve_image(str(secret), inst)
    check("absolute path outside roots refused", p is None, f"accepted {p}")

    p, why = resolve_image("notes.txt", inst)
    check("non-image extension refused", p is None, f"accepted {p}")

    p, why = resolve_image("docs/missing.png", inst)
    check("missing file refused", p is None, f"accepted {p}")

    p, why = resolve_image("huge.png", inst)
    check("oversized file refused", p is None, f"accepted {p}")

    p, why = resolve_image("empty.png", inst)
    check("empty file refused", p is None, f"accepted {p}")

    p, why = resolve_image("diagram.svg", inst)
    check("svg refused", p is None, f"accepted {p}")

    if os.name != "nt" or _can_symlink(tmp):
        link = repo / "docs" / "link.png"
        try:
            link.symlink_to(secret)
            p, why = resolve_image("docs/link.png", inst)
            check("symlink escaping the repo refused", p is None, f"accepted {p}")
        except OSError:
            print("  SKIP  symlink test (no permission)")
    else:
        print("  SKIP  symlink test (no permission)")

    print("\nworktree root")
    wt = tmp / "wt"
    (wt / "out").mkdir(parents=True)
    shot = wt / "out" / "shot.png"
    shot.write_bytes(png_bytes())
    wt_inst = FakeInstance(str(repo), str(wt))
    p, why = resolve_image("out/shot.png", wt_inst)
    check("build resolves relative paths against its worktree",
          p == shot.resolve(), why)
    p, why = resolve_image("docs/diagram.png", wt_inst)
    check("build can still share from the main repo", p == good.resolve(), why)

    print("\nother git worktrees")
    _check_other_worktrees(tmp)

    print("\ndata dir root")
    data_img = Path(config.DATA_DIR) / "imgtest_chart.png"
    data_img.write_bytes(png_bytes())
    try:
        p, why = resolve_image(str(data_img), inst)
        check("file in the bot's data dir accepted", p == data_img.resolve(), why)
    finally:
        data_img.unlink(missing_ok=True)

    loose = Path(tempfile.gettempdir()) / "imgtest_loose.png"
    loose.write_bytes(png_bytes())
    try:
        p, why = resolve_image(str(loose), inst)
        check("loose file in system temp refused", p is None, f"accepted {p}")
    finally:
        loose.unlink(missing_ok=True)

    print("\ndelivery")
    msgr = FakeMessenger()
    ctx = FakeCtx(msgr)
    out = asyncio.run(deliver_images(
        ctx,
        '[BOT_CMD: /image path="docs/diagram.png" caption="A diagram"]',
        inst,
    ))
    check("one image delivered", out.sent == 1 and not out.failures, repr(out))
    check("batched into a single message", len(msgr.batches) == 1)
    check("caption forwarded",
          msgr.batches and msgr.batches[0][1] == "A diagram")
    check("no refusal notice on the happy path", not msgr.texts, str(msgr.texts))

    msgr = FakeMessenger()
    out = asyncio.run(deliver_images(
        FakeCtx(msgr), "[BOT_CMD: /image ../secrets/id_rsa.png]", inst,
    ))
    check("refused image sends nothing", out.sent == 0)
    check("refusal posts a visible notice", len(msgr.texts) == 1, str(msgr.texts))
    check("refusal is returned so the session can be told",
          [f.path for f in out.failures] == ["../secrets/id_rsa.png"], repr(out))

    msgr = FakeMessenger()
    many = "\n".join(
        "[BOT_CMD: /image docs/diagram.png]" for _ in range(MAX_IMAGES_PER_RESPONSE + 2)
    )
    out = asyncio.run(deliver_images(FakeCtx(msgr), many, inst))
    check(f"per-response cap of {MAX_IMAGES_PER_RESPONSE} holds",
          out.sent == MAX_IMAGES_PER_RESPONSE, repr(out.sent))
    check("over-cap drop is reported", len(msgr.texts) == 1, str(msgr.texts))
    check("each over-cap image is returned as a failure",
          len(out.failures) == 2 and "limit" in out.failures[0].reason,
          repr(out.failures))

    msgr = FakeMessenger()
    out = asyncio.run(deliver_images(FakeCtx(msgr), "no directives here", inst))
    check("plain text is a no-op",
          out.sent == 0 and not out.failures and not msgr.batches and not msgr.texts)

    print("\nseveral messages instead of a batch limit")
    # The 2026-11-18 report: three GIFs a few MB each, everything past 9 MB
    # refused with "batch upload limit reached". Now they split across messages.
    gifs = []
    for i in range(3):
        g = repo / f"motion-{i}.gif"
        g.write_bytes(b"GIF89a" + b"\0" * 4_000_000)
        gifs.append(g)
    msgr = FakeMessenger()
    text = "\n".join(
        f'[BOT_CMD: /image path="{g.name}" caption="clip {i}"]'
        for i, g in enumerate(gifs)
    )
    out = asyncio.run(deliver_images(FakeCtx(msgr), text, inst))
    check("three 4 MB gifs all delivered", out.sent == 3 and not out.failures,
          repr(out))
    check("split into two messages, each under the budget",
          len(msgr.batches) == 2
          and all(sum(Path(p).stat().st_size for p in b) <= MAX_BATCH_BYTES
                  for b, _ in msgr.batches),
          repr([len(b) for b, _ in msgr.batches]))
    check("order kept across messages",
          [Path(p).name for b, _ in msgr.batches for p in b]
          == [g.name for g in gifs])
    check("each message carries its own images' captions",
          [c for _, c in msgr.batches] == ["clip 0 · clip 1", "clip 2"],
          repr([c for _, c in msgr.batches]))
    check("no 'couldn't share' notice when everything posted", not msgr.texts,
          str(msgr.texts))

    groups = pack_messages([(f"f{i}", 10, None) for i in range(MAX_FILES_PER_MESSAGE + 3)])
    check(f"a message holds at most {MAX_FILES_PER_MESSAGE} files",
          [len(g) for g in groups] == [MAX_FILES_PER_MESSAGE, 3],
          repr([len(g) for g in groups]))
    check("nothing to pack is no messages", pack_messages([]) == [])

    msgr = FakeMessenger(fail_on=2)
    out = asyncio.run(deliver_images(FakeCtx(msgr), text, inst))
    check("a failed upload keeps the other message",
          out.sent == 2 and len(msgr.batches) == 1, repr(out))
    check("the failed message's image is returned with the reason",
          len(out.failures) == 1 and out.failures[0].name == gifs[2].name
          and "upload failed" in out.failures[0].reason, repr(out.failures))
    check("and the user sees it", len(msgr.texts) == 1 and gifs[2].name in msgr.texts[0],
          str(msgr.texts))

    msgr = FakeMessenger(empty_on=1)
    out = asyncio.run(deliver_images(FakeCtx(msgr), text, inst))
    check("an upload that posted nothing counts as failed, not sent",
          out.sent == 1 and len(out.failures) == 2, repr(out))

    note = failure_note([ImageFailure("out/a.gif", "too large (9.1 MB, limit 8 MB)")])
    check("the session's note names the path and the reason",
          "`out/a.gif`" in note and "too large" in note, note)
    check("the note tells it to share again with the directive",
          "[BOT_CMD: /image]" in note and "did NOT reach" in note, note)

    print("\ndisplay")
    shown = collapse_bot_directives(
        'Here is the diagram.\n[BOT_CMD: /image path="docs/diagram.png"]\nMore text.'
    )
    check("directive stripped from displayed copy",
          "BOT_CMD" not in shown and "docs/diagram.png" not in shown, shown)
    check("surrounding prose survives",
          "Here is the diagram." in shown and "More text." in shown, shown)

    shown = collapse_bot_directives(
        '[BOT_CMD: /wake delay=300 reason="check the deploy"]'
    )
    check("other directives still collapse to a chip",
          shown.startswith("-# `/wake`"), shown)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True,
    )


def _check_other_worktrees(tmp: Path) -> None:
    """A session-made worktree's relative path still resolves (2026-10-19:
    a thread rendered into ``.worktrees/liq-design`` and shared
    ``Docs/design/...``, which the bot looked for in the repo root)."""
    root = tmp / "gitroot"
    repo = root / "sub"                # registered repo is a subfolder of its git root
    repo.mkdir(parents=True)
    (repo / "keep").write_text("x")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "init")
    _git(root, "worktree", "add", "-q", str(repo / ".worktrees" / "side"))
    _git(root, "worktree", "add", "-q", str(repo / ".worktrees" / "side2"))
    _git(root, "worktree", "add", "-q", str(tmp / "elsewhere"))
    inst = FakeInstance(str(repo))

    at_offset = repo / ".worktrees" / "side" / "sub" / "out" / "r.png"
    at_offset.parent.mkdir(parents=True)
    at_offset.write_bytes(png_bytes())
    p, why = resolve_image("out/r.png", inst)
    check("relative path found at the repo's offset in another worktree",
          p == at_offset.resolve(), why)

    old = repo / ".worktrees" / "side" / ".render" / "a.png"
    new = repo / ".worktrees" / "side2" / ".render" / "a.png"
    for f, mtime in ((old, 1_000_000), (new, 2_000_000)):
        f.parent.mkdir(parents=True)
        f.write_bytes(png_bytes())
        os.utime(f, (mtime, mtime))
    p, why = resolve_image(".render/a.png", inst)
    check("worktree-root match found, newest copy wins", p == new.resolve(), why)

    far = tmp / "elsewhere" / "sub" / "far.png"
    far.parent.mkdir(parents=True, exist_ok=True)
    far.write_bytes(png_bytes())
    p, why = resolve_image("far.png", inst)
    check("worktree outside the allowed roots still refused", p is None, f"accepted {p}")
    check("refusal says where it looked and asks for an absolute path",
          "worktrees" in why and "absolute" in why, why)

    build = FakeInstance(str(repo), str(repo / ".worktrees" / "side2"))
    p, why = resolve_image("out/r.png", build)
    check("a build never picks up a sibling worktree's file",
          p is None and why == "file not found", f"{p} {why}")

    p, why = resolve_image(str(repo / "nope.png"), inst)
    check("absolute miss keeps the plain reason", why == "file not found", why)


def _can_symlink(base: Path) -> bool:
    try:
        t = base / "_lnk"
        t.symlink_to(base)
        t.unlink()
        return True
    except OSError:
        return False


if __name__ == "__main__":
    sys.exit(main())

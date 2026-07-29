"""Harness for the [BOT_CMD: /image] path — parsing, path safety, delivery.

Run: python scripts/test_image_directive.py

The path rules are the point: without them "share a picture" becomes "read any
file on the host and publish it to Discord". Each case below is one way that
could go wrong.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import config  # noqa: E402
from bot.engine.images import (  # noqa: E402
    MAX_IMAGES_PER_RESPONSE,
    deliver_images,
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
    def __init__(self):
        self.batches: list[tuple[list[str], str | None]] = []
        self.texts: list[str] = []

    async def send_files(self, channel_id, file_paths, caption=None):
        self.batches.append((list(file_paths), caption))
        return ["1"]

    async def send_text(self, channel_id, text, buttons=None, silent=False):
        self.texts.append(text)
        return "2"


class FakeCtx:
    def __init__(self, messenger):
        self.messenger = messenger
        self.channel_id = "chan"


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
    sent = asyncio.run(deliver_images(
        ctx,
        '[BOT_CMD: /image path="docs/diagram.png" caption="A diagram"]',
        inst,
    ))
    check("one image delivered", sent == 1, str(sent))
    check("batched into a single message", len(msgr.batches) == 1)
    check("caption forwarded",
          msgr.batches and msgr.batches[0][1] == "A diagram")
    check("no refusal notice on the happy path", not msgr.texts, str(msgr.texts))

    msgr = FakeMessenger()
    sent = asyncio.run(deliver_images(
        FakeCtx(msgr), "[BOT_CMD: /image ../secrets/id_rsa.png]", inst,
    ))
    check("refused image sends nothing", sent == 0)
    check("refusal posts a visible notice", len(msgr.texts) == 1, str(msgr.texts))

    msgr = FakeMessenger()
    many = "\n".join(
        f"[BOT_CMD: /image docs/diagram.png]" for _ in range(MAX_IMAGES_PER_RESPONSE + 2)
    )
    sent = asyncio.run(deliver_images(FakeCtx(msgr), many, inst))
    check(f"per-response cap of {MAX_IMAGES_PER_RESPONSE} holds",
          sent == MAX_IMAGES_PER_RESPONSE, str(sent))
    check("over-cap drop is reported", len(msgr.texts) == 1, str(msgr.texts))

    msgr = FakeMessenger()
    sent = asyncio.run(deliver_images(FakeCtx(msgr), "no directives here", inst))
    check("plain text is a no-op", sent == 0 and not msgr.batches and not msgr.texts)

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

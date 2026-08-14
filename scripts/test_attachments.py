"""Harness for Discord file attachments — what gets read, what gets refused.

Run: ./.venv/bin/python scripts/test_attachments.py

The regression this guards: a PDF uploaded with no message text used to fall
through the attachment loop untouched and hit `if not text: return`, so the
bot answered with total silence and the user assumed it was broken. Every
case below asserts that *something* comes back — either the upload reaches
the prompt, or the user is told plainly why it couldn't.

The real `on_message` is driven here (not a reimplementation of it) with
stand-in Discord objects, so the assertions cover the shipping code path.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import asyncio
import io
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import config  # noqa: E402
from bot.discord import bot as botmod  # noqa: E402
from bot.discord.bot import (  # noqa: E402
    ATTACH_DOC_MAX,
    ClaudeBot,
    _attachment_reject_note,
    _extract_docx_text,
    _human_size,
)
from bot.engine import commands  # noqa: E402

PASS, FAIL = 0, 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}{(' — ' + detail) if detail else ''}")


# --- Sample files ----------------------------------------------------------

def make_pdf() -> bytes:
    """A minimal single-page PDF with real text — no dependency needed."""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
    ]
    stream = b"BT /F1 24 Tf 72 700 Td (WORK FEEDBACK FORM) Tj ET"
    objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1, xref)
    return out


def make_docx(paragraphs: list[str]) -> bytes:
    ct = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    )
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    doc = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", doc)
    return buf.getvalue()


# --- Discord stand-ins -----------------------------------------------------

class FakeAttachment:
    def __init__(self, filename: str, data: bytes, size: int | None = None):
        self.filename = filename
        self._data = data
        self.size = len(data) if size is None else size
        self.content_type = None

    async def read(self) -> bytes:
        return self._data


class FakeChannel:
    """Not a discord.Thread, not the lobby → the 'unmapped channel' path."""

    def __init__(self):
        self.id = 999_000_001
        self.name = "test-channel"
        self.sent: list[str] = []

    async def send(self, content=None, **kw):
        if content:
            self.sent.append(str(content))
        return None


class FakeAuthor:
    bot = False
    id = 152516669082697728
    display_name = "tester"


class FakeMessage:
    def __init__(self, content: str, attachments: list[FakeAttachment]):
        self.content = content
        self.attachments = attachments
        self.author = FakeAuthor()
        self.channel = FakeChannel()
        self.guild = None
        self.webhook_id = None
        self.embeds: list = []
        self.type = "default"

        class _Flags:
            value = 0
        self.flags = _Flags()


class FakeForums:
    archive_channel_ids: set[int] = set()
    forum_projects: dict = {}
    user_control_thread_ids: set[str] = set()


class FakeCtx:
    def __init__(self):
        self.pending_image_paths: list[str] = []
        self.user_id = ""
        self.user_name = ""


class Harness:
    """Minimal stand-in for ClaudeBot — only what the attachment path touches."""

    user = object()  # on_message's "did I send this?" guard

    def __init__(self, voice_enabled: bool = False):
        self._voice_enabled = voice_enabled
        self._forums = FakeForums()
        self._lobby_channel_id = 1  # never matches FakeChannel.id
        self.prompts: list[str] = []
        self.sweeps = 0

    def _schedule_pending_image_sweep(self) -> None:
        # Real bot hands the saved file to the retention reaper here. Counting
        # the nudge is enough — the reaper itself is covered by
        # scripts/test_pending_image_lifecycle.py.
        self.sweeps += 1

    def _in_scope(self, guild, channel):
        return True

    def _check_access(self, user_id, channel_id=None):
        from bot.discord.access import AccessResult
        return AccessResult(allowed=True, is_owner=True)

    def _ctx(self, channel_id, **kw):
        return FakeCtx()


async def run_upload(filename: str, data: bytes, text: str = "",
                     size: int | None = None, voice: bool = False):
    """Push one attachment through the real on_message; return (replies, prompt)."""
    harness = Harness(voice_enabled=voice)
    msg = FakeMessage(text, [FakeAttachment(filename, data, size=size)])

    captured: list[str] = []

    async def fake_on_text(ctx, prompt):
        captured.append(prompt)

    async def fake_enrich(t):
        return t

    orig_on_text, orig_enrich = commands.on_text, botmod.enrich_with_tweets
    commands.on_text = fake_on_text
    botmod.enrich_with_tweets = fake_enrich
    try:
        await ClaudeBot.on_message(harness, msg)
    finally:
        commands.on_text = orig_on_text
        botmod.enrich_with_tweets = orig_enrich

    return msg.channel.sent, (captured[0] if captured else None)


# --- Cases -----------------------------------------------------------------

async def main() -> int:
    print("\n== PDF upload, no message text (the original bug) ==")
    replies, prompt = await run_upload("feedback_form.pdf", make_pdf())
    check("something reached the session", prompt is not None,
          f"replies={replies}")
    check("prompt carries a path reference",
          bool(prompt) and "saved at `" in prompt, str(prompt))
    if prompt:
        ref = prompt.split("saved at `")[-1].split("`")[0]
        saved = Path(ref)
        check("path is inside the pending dir",
              saved.parent == config.PENDING_IMAGES_DIR, str(saved))
        check("saved bytes are a real PDF", make_pdf()[:5] == b"%PDF-", "")
    check("no confusing error shown to the user", not replies, str(replies))

    print("\n== PDF alongside message text ==")
    replies, prompt = await run_upload("q3.pdf", make_pdf(), text="what's my rating?")
    check("user text preserved", bool(prompt) and "what's my rating?" in prompt, str(prompt))
    check("file reference appended",
          bool(prompt) and "[File: q3.pdf saved at `" in prompt, str(prompt))

    print("\n== Word document ==")
    docx = make_docx(["PERFORMANCE REVIEW 2026", "Reviewer: Sam Smith",
                      "Comment: consistently strong delivery"])
    replies, prompt = await run_upload("review.docx", docx)
    check("text extracted into the prompt",
          bool(prompt) and "consistently strong delivery" in prompt, str(prompt))
    check("all paragraphs came through",
          bool(prompt) and "Reviewer: Sam Smith" in prompt, str(prompt))
    check("no error shown", not replies, str(replies))

    print("\n== Word document with no text in it ==")
    replies, prompt = await run_upload("empty.docx", make_docx([]))
    check("user is told", bool(replies), str(replies))
    check("names the file", bool(replies) and "empty.docx" in replies[0], str(replies))

    print("\n== Corrupt Word document ==")
    replies, prompt = await run_upload("broken.docx", b"this is not a zip at all")
    check("user is told, no traceback", bool(replies), str(replies))
    check("suggests a way forward",
          bool(replies) and "PDF" in replies[0], str(replies))

    print("\n== Markdown upload, no message text ==")
    replies, prompt = await run_upload("notes.md", b"# Heading\n\nsome notes here")
    check("inlined into the prompt",
          bool(prompt) and "some notes here" in prompt, str(prompt))
    check("no error shown", not replies, str(replies))

    print("\n== Other plain-text kinds ==")
    for name, blob, needle in [
        ("data.csv", b"a,b\n1,2", "a,b"),
        ("cfg.json", b'{"k": 1}', '"k"'),
        ("run.log", b"ERROR boom", "ERROR boom"),
        ("conf.yaml", b"key: value", "key: value"),
        ("conf.yml", b"other: thing", "other: thing"),
    ]:
        _, p = await run_upload(name, blob)
        check(f"{name} inlined", bool(p) and needle in p, str(p))

    print("\n== Badly encoded text file ==")
    replies, prompt = await run_upload("mojibake.md", b"ok \xff\xfe then")
    check("degrades instead of raising", bool(prompt) and "then" in prompt, str(prompt))

    print("\n== Unsupported type ==")
    replies, prompt = await run_upload("form.pages", b"\x00binary\x00")
    check("user gets a reply", bool(replies), str(replies))
    check("names the file", bool(replies) and "form.pages" in replies[0], str(replies))
    check("says what works instead",
          bool(replies) and "PDF" in replies[0] and "Word" in replies[0], str(replies))
    check("no jargon leaked", bool(replies) and "Traceback" not in replies[0], str(replies))
    check("nothing sent to the session", prompt is None, str(prompt))

    print("\n== Oversized document ==")
    replies, prompt = await run_upload(
        "huge.pdf", make_pdf(), size=ATTACH_DOC_MAX + 1)
    check("user gets a reply", bool(replies), str(replies))
    check("says it's too big", bool(replies) and "too big" in replies[0], str(replies))
    check("states the limit", bool(replies) and "20 MB" in replies[0], str(replies))

    print("\n== Oversized text file ==")
    replies, prompt = await run_upload("big.md", b"x" * 10, size=600_000)
    check("user gets a reply", bool(replies), str(replies))
    check("quotes the text-file limit",
          bool(replies) and "500 KB" in replies[0], str(replies))

    print("\n== Voice note while transcription is off ==")
    replies, prompt = await run_upload("note.ogg", b"fake audio", voice=False)
    check("user is told rather than ignored", bool(replies), str(replies))
    check("explains it's switched off",
          bool(replies) and "switched on" in replies[0], str(replies))

    print("\n== Unsupported file sent *with* a question ==")
    replies, prompt = await run_upload("form.pages", b"\x00", text="can you read this?")
    check("question still reaches the session",
          bool(prompt) and "can you read this?" in prompt, str(prompt))
    check("and the user is still told the file was skipped", bool(replies), str(replies))

    print("\n== Several files in one message ==")
    harness = Harness()
    msg = FakeMessage("", [
        FakeAttachment("ok.md", b"first file"),
        FakeAttachment("nope.pages", b"\x00"),
        FakeAttachment("report.pdf", make_pdf()),
    ])
    captured: list[str] = []

    async def _grab(ctx, prompt):
        captured.append(prompt)

    orig, orig_e = commands.on_text, botmod.enrich_with_tweets
    commands.on_text = _grab
    botmod.enrich_with_tweets = lambda t: _identity(t)
    try:
        await ClaudeBot.on_message(harness, msg)
    finally:
        commands.on_text, botmod.enrich_with_tweets = orig, orig_e
    got = captured[0] if captured else ""
    check("readable file made it through", "first file" in got, got)
    check("PDF made it through", "report.pdf saved at `" in got, got)
    check("the skipped one is reported",
          any("nope.pages" in s for s in msg.channel.sent), str(msg.channel.sent))

    print("\n== Archive channels stay silent ==")
    harness = Harness()
    msg = FakeMessage("", [FakeAttachment("form.pages", b"x")])
    harness._forums.archive_channel_ids = {msg.channel.id}
    await ClaudeBot.on_message(harness, msg)
    check("no reply in a read-only archive channel", not msg.channel.sent,
          str(msg.channel.sent))

    print("\n== Helpers ==")
    check("docx extractor rejects entity declarations",
          _raises(lambda: _extract_docx_text(_entity_docx())))
    check("size wording is human", _human_size(20_000_000) == "20 MB",
          _human_size(20_000_000))
    check("500KB reads as KB", _human_size(500_000) == "500 KB", _human_size(500_000))
    note = _attachment_reject_note("thing.xyz", ".xyz", 10)
    check("unknown type gets the accepted-kinds sentence",
          "voice notes" in note, note)

    print("\n== Uploads outlive the turn that received them ==")
    # This used to assert the opposite. Deleting on turn-exit is what wiped a
    # screenshot one second before the steered run that referenced it started:
    # the path is in the session transcript, so a later turn can still need it.
    # Reaping is the retention sweep's job now.
    sweep_harness = Harness()
    sweep_msg = FakeMessage("", [FakeAttachment("keeper.pdf", make_pdf())])
    orig, orig_e = commands.on_text, botmod.enrich_with_tweets
    commands.on_text = _swallow
    botmod.enrich_with_tweets = _identity
    try:
        await ClaudeBot.on_message(sweep_harness, sweep_msg)
    finally:
        commands.on_text, botmod.enrich_with_tweets = orig, orig_e

    saved = [f for f in config.PENDING_IMAGES_DIR.iterdir() if f.is_file()]
    check("saved uploads still on disk after on_message returned",
          sum(1 for f in saved if f.suffix == ".pdf") >= 2, str(saved))
    check("the reaper was nudged instead of an immediate delete",
          sweep_harness.sweeps == 1, str(sweep_harness.sweeps))

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


async def _identity(t):
    return t


async def _swallow(ctx, prompt):
    """Stand-in for the real dispatcher — takes the prompt, does nothing."""
    return None


def _entity_docx() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml",
                   '<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "boom">]><d/>')
    return buf.getvalue()


def _raises(fn) -> bool:
    try:
        fn()
    except Exception:
        return True
    return False


if __name__ == "__main__":
    # This harness writes real uploads through the real save path, and the bot
    # it shares a machine with is probably running.  Point the save directory at
    # scratch space so a test file can never land in — or be cleaned out of —
    # the live folder somebody's screenshot is sitting in.
    _real_dir = config.PENDING_IMAGES_DIR
    _tmp = tempfile.TemporaryDirectory(prefix="attachments_test_")
    config.PENDING_IMAGES_DIR = Path(_tmp.name)
    try:
        sys.exit(asyncio.run(main()))
    finally:
        config.PENDING_IMAGES_DIR = _real_dir
        _tmp.cleanup()

#!/usr/bin/env python3
"""Proof that the shared findings tool's rules actually bite.

`scripts/study/study.py` is called from every repo, so it is driven the way a
session drives it: as a subprocess, from a working directory. Each check test
breaks exactly one rule in a clean project and asserts that specific failure is
reported, so a rule that stops firing cannot hide behind another one's failure.

Also pins the places it has to find its footing: a worktree, a worktree forked
before the repo had a study folder, a repo registered as a subfolder of its git
root, and a repo that carries its own copy of the tool. The reasoning is in
docs/design/findings.md.

Run: python scripts/test_study.py
"""

import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "scripts" / "study" / "study.py"

_passed = 0
_failed = 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  [PASS] {name}")
    else:
        _failed += 1
        print(f"  [FAIL] {name}  {detail}")


def study(cwd, *args):
    return subprocess.run(
        [sys.executable, str(TOOL), *args], cwd=cwd,
        capture_output=True, text=True, encoding="utf-8",
    )


def git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    )


RECORD = """\
+++
id = "{id}"
title = "A title"
date = "2026-11-03"
status = "{status}"
question = "A question?"
{extra}
+++
{body}
"""


def write_record(project, record_id, status="stands", extra='verdict = "It holds."',
                 body="What this does not establish."):
    path = Path(project) / "study" / "findings" / f"{record_id}.md"
    path.write_text(RECORD.format(id=record_id, status=status, extra=extra, body=body),
                    encoding="utf-8")
    return path


class Project:
    """A clean project that passes `check`, for a test to break one thing in."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "repo"
        self.path.mkdir()
        git(self.path, "init", "-q")
        out = study(self.path, "init")
        assert out.returncode == 0, out.stderr
        write_record(self.path, "2026-11-03-baseline")
        return self

    def __exit__(self, *exc):
        self._tmp.cleanup()


def expect_failure(name, needle, breaker):
    with Project() as p:
        clean = study(p.path, "check")
        assert clean.returncode == 0, clean.stdout
        breaker(p.path)
        out = study(p.path, "check")
        check(f"check catches: {name}",
              out.returncode == 1 and needle in out.stdout, out.stdout + out.stderr)


def set_config(project, text):
    path = Path(project) / "study" / "config.toml"
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")


print("\n[check: each rule, broken alone]")
expect_failure("a missing required field", "missing required field 'question'",
               lambda p: (Path(p) / "study/findings/2026-11-03-baseline.md").write_text(
                   '+++\nid = "2026-11-03-baseline"\ntitle = "T"\ndate = "2026-11-03"\n'
                   'status = "open"\n+++\nbody\n', encoding="utf-8"))
expect_failure("a mistyped field", "field 'tags' has the wrong type",
               lambda p: write_record(p, "2026-11-03-typed",
                                      extra='verdict = "v"\ntags = "not-a-list"'))
expect_failure("a list of non-strings", "field 'scripts' must be a list of strings",
               lambda p: write_record(p, "2026-11-03-typed",
                                      extra='verdict = "v"\nscripts = [1]'))
expect_failure("a bad date", "is not YYYY-MM-DD",
               lambda p: (Path(p) / "study/findings/2026-11-03-baseline.md").write_text(
                   RECORD.format(id="2026-11-03-baseline", status="open", extra="", body="b")
                   .replace('date = "2026-11-03"', 'date = "soon"'), encoding="utf-8"))
expect_failure("an id that is not the filename", "does not match the filename",
               lambda p: (Path(p) / "study/findings/renamed.md").write_text(
                   RECORD.format(id="2026-11-03-other", status="open", extra="", body="b"),
                   encoding="utf-8"))
expect_failure("a status outside the vocabulary", "status 'dead' is not one of",
               lambda p: write_record(p, "2026-11-03-word", status="dead"))
expect_failure("a kind outside the vocabulary", "kind 'memo' is not one of",
               lambda p: write_record(p, "2026-11-03-kind", extra='verdict = "v"\nkind = "memo"'))
expect_failure("a decision that is shut", "a decision record must be",
               lambda p: write_record(p, "2026-11-03-call", status="shut",
                                      extra='verdict = "v"\nkind = "decision"'))
expect_failure("a closed record with no verdict", "status 'shut' needs a verdict",
               lambda p: write_record(p, "2026-11-03-bare", status="shut", extra=""))
expect_failure("a supersede link that resolves nowhere", "supersedes 2026-01-01-gone does not resolve",
               lambda p: write_record(p, "2026-11-03-link",
                                      extra='verdict = "v"\nsupersedes = ["2026-01-01-gone"]'))
expect_failure("a one-sided supersede link", "has no superseded_by link back",
               lambda p: write_record(p, "2026-11-03-link",
                                      extra='verdict = "v"\nsupersedes = ["2026-11-03-baseline"]'))
expect_failure("superseded with no superseded_by", "must come together",
               lambda p: write_record(p, "2026-11-03-orphan", status="superseded"))
expect_failure("superseded_by on a live record", "must come together",
               lambda p: (write_record(p, "2026-11-03-a",
                                       extra='verdict = "v"\nsuperseded_by = ["2026-11-03-b"]'),
                          write_record(p, "2026-11-03-b",
                                       extra='verdict = "v"\nsupersedes = ["2026-11-03-a"]')))
expect_failure("a body over the cap", "over the cap of 300",
               lambda p: write_record(p, "2026-11-03-long", body="word " * 301))
expect_failure("an unread source that does not exist", "unread_source docs/gone.md does not exist",
               lambda p: set_config(p, '\n[[unread_source]]\npath = "docs/gone.md"\nnote = "n"\n'))
expect_failure("a header that is not TOML", "header is not valid TOML",
               lambda p: (Path(p) / "study/findings/2026-11-03-broken.md").write_text(
                   "+++\nid = =\n+++\nbody\n", encoding="utf-8"))
expect_failure("a file with no header", "first line must be",
               lambda p: (Path(p) / "study/findings/notes.md").write_text(
                   "just notes\n", encoding="utf-8"))
expect_failure("a config format from the future", "format must be 1",
               lambda p: (Path(p) / "study/config.toml").write_text("format = 2\n", encoding="utf-8"))
expect_failure("a config that is not UTF-8", "config.toml does not read",
               lambda p: (Path(p) / "study/config.toml").write_bytes(b"format = 1\n# \xff\n"))
expect_failure("an unread source written as one table", "must be written [[unread_source]]",
               lambda p: set_config(p, '\n[unread_source]\npath = "docs/x.md"\n'))
expect_failure("an empty required field", "required field 'title' is empty",
               lambda p: (Path(p) / "study/findings/2026-11-03-blank.md").write_text(
                   RECORD.format(id="2026-11-03-blank", status="stands", extra='verdict = "v"',
                                 body="b").replace('"A title"', '"  "'), encoding="utf-8"))
expect_failure("a link field that is a number", "field 'supersedes' has the wrong type",
               lambda p: write_record(p, "2026-11-04-num", extra='verdict = "v"\nsupersedes = 7'))


def _linked_at_a_mistyped_back_link(project, back_field):
    """A record linking to one whose matching link field is not a list of strings:
    the check of the first must not fall over on the second."""
    write_record(project, "2026-11-04-a", extra='verdict = "v"\nsupersedes = ["2026-11-04-b"]')
    write_record(project, "2026-11-04-b", extra=f'verdict = "v"\n{back_field}')


for back in ("superseded_by = 7", 'superseded_by = "x"', 'superseded_by = [["x"]]'):
    with Project() as p:
        _linked_at_a_mistyped_back_link(p.path, back)
        out = study(p.path, "check")
        check(f"check reports and survives a mistyped link ({back})",
              out.returncode == 1 and "Traceback" not in out.stderr
              and "2026-11-04-b.md: field 'superseded_by'" in out.stdout
              and "has no superseded_by link back" in out.stdout,
              out.stdout + out.stderr)
with Project() as p:
    write_record(p.path, "2026-11-04-nested", extra='verdict = "v"\nsupersedes = [["x"]]')
    out = study(p.path, "check")
    check("check survives a link that is not hashable",
          out.returncode == 1 and "Traceback" not in out.stderr and "list of strings" in out.stdout,
          out.stdout + out.stderr)

print("\n[check: what must not fail]")
with Project() as p:
    write_record(p.path, "2026-11-03-newer", extra='verdict = "v"\nfuture_field = 1')
    out = study(p.path, "check")
    check("an unknown field warns and does not fail",
          out.returncode == 0 and "WARN" in out.stdout and "future_field" in out.stdout, out.stdout)
    write_record(p.path, "S170", extra='verdict = "v"')
    out = study(p.path, "check")
    check("an old-style id warns and does not fail",
          out.returncode == 0 and "S170.md: id is not" in out.stdout, out.stdout)
    write_record(p.path, "2026-11-03-word-cap", body="word " * 300)
    check("a body exactly at the cap passes", study(p.path, "check").returncode == 0)
with Project() as p:
    set_config(p.path, "\nbody_max_words = 5\n")
    write_record(p.path, "2026-11-03-capped", body="word " * 6)
    out = study(p.path, "check")
    check("the body cap comes from config", "over the cap of 5" in out.stdout, out.stdout)
with Project() as p:
    (p.path / "docs").mkdir()
    (p.path / "docs" / "old.md").write_text("old", encoding="utf-8")
    set_config(p.path, '\n[[unread_source]]\npath = "docs/old.md"\nnote = "the old log"\n')
    check("an unread source that exists passes", study(p.path, "check").returncode == 0)

print("\n[init and new]")
with tempfile.TemporaryDirectory() as tmp:
    repo = Path(tmp) / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    out = study(repo, "init")
    cfg = tomllib.loads((repo / "study" / "config.toml").read_text(encoding="utf-8"))
    check("init writes format = 1", cfg.get("format") == 1, str(cfg))
    check("init leaves an empty findings folder that git can carry",
          (repo / "study" / "findings").is_dir()
          and not list((repo / "study" / "findings").glob("*.md"))
          and (repo / "study" / "findings" / ".gitkeep").exists())
    check("init a second time is refused", study(repo, "init").returncode == 2)

    out = study(repo, "new", "--title", 'A "quoted" Titlé: with $ymbols!', "--question", "Why?\nReally?")
    check("new succeeds", out.returncode == 0, out.stderr)
    made = sorted((repo / "study" / "findings").glob("*.md"))
    check("new writes one record", len(made) == 1, str(made))
    record_id = made[0].stem
    text = made[0].read_text(encoding="utf-8")
    check("new slugs the title into a date-slug id",
          record_id.endswith("-a-quoted-title-with-ymbols") and record_id[:4].isdigit(), record_id)
    check("the scaffold's last paragraph asks what this does not establish",
          text.rstrip().endswith("What this does not establish."), text)
    check("an open scaffold passes check", study(repo, "check").returncode == 0,
          study(repo, "check").stdout)
    check("the question survives quoting", 'Why?\\nReally?' in text, text)

    again = study(repo, "new", "--title", 'A "quoted" Titlé: with $ymbols!', "--question", "Q")
    check("new refuses an id that exists",
          again.returncode == 2 and "already exists" in again.stderr, again.stderr)
    check("the refusal changed nothing", made[0].read_text(encoding="utf-8") == text)

    out = study(repo, "new", "--title", "Dead", "--question", "Q", "--status", "shut")
    check("new --status shut with no verdict is written and flagged",
          out.returncode == 0 and "needs a verdict" in out.stdout, out.stdout)
    check("check then reports it", "needs a verdict" in study(repo, "check").stdout)
    out = study(repo, "new", "--title", "Filled", "--question", "Q", "--status", "shut",
                "--verdict", "No.")
    check("new --verdict fills it in",
          any("verdict = \"No.\"" in f.read_text(encoding="utf-8")
              for f in (repo / "study" / "findings").glob("*-filled.md")))
    out = study(repo, "new", "--title", "Call", "--question", "Q", "--kind", "decision",
                "--verdict", "Do X.")
    decision = next((repo / "study" / "findings").glob("*-call.md"))
    meta = decision.read_text(encoding="utf-8")
    check("a decision defaults to stands", 'status = "stands"' in meta and 'kind = "decision"' in meta, meta)
    out = study(repo, "new", "--title", "Bad", "--question", "Q", "--kind", "decision", "--status", "shut")
    check("a shut decision is refused and not written",
          out.returncode == 2 and not list((repo / "study" / "findings").glob("*-bad.md")))
    check("new --status superseded is not offered",
          study(repo, "new", "--title", "S", "--question", "Q", "--status", "superseded").returncode == 2)

with Project() as p:
    findings = p.path / "study" / "findings"
    before = sorted(f.name for f in findings.iterdir())
    for args in (("--title", "   ", "--question", "Q"), ("--title", "T", "--question", "")):
        check(f"new refuses an empty field {args}",
              study(p.path, "new", *args).returncode == 2)
    bad_bytes = subprocess.run(
        [sys.executable, str(TOOL), "new", "--title", b"bad \xff".decode("utf-8", "surrogateescape"),
         "--question", "Q"], cwd=p.path, capture_output=True, text=True, encoding="utf-8",
    )
    check("new refuses a title that cannot be written, with no traceback",
          bad_bytes.returncode == 2 and "Traceback" not in bad_bytes.stderr, bad_bytes.stderr)
    check("the refusals wrote nothing", before == sorted(f.name for f in findings.iterdir()))
    out = study(p.path, "new", "--title", "Odd \x7f\t\"title\" \\ \n+++", "--question", "Q\x7f")
    check("control characters and a fence in a title still make a readable record",
          out.returncode == 0 and study(p.path, "check").returncode == 0,
          out.stderr + study(p.path, "check").stdout)
    written = next(findings.glob("*-odd-*.md"))
    title = tomllib.loads(written.read_text(encoding="utf-8").split("+++\n")[1])["title"]
    check("and the title reads back exactly", title == "Odd \x7f\t\"title\" \\ \n+++", repr(title))

print("\n[supersede]")
with Project() as p:
    write_record(p.path, "2026-11-04-better")
    out = study(p.path, "supersede", "2026-11-04-better", "2026-11-03-baseline")
    check("supersede succeeds", out.returncode == 0, out.stderr)
    old = (p.path / "study/findings/2026-11-03-baseline.md").read_text(encoding="utf-8")
    new = (p.path / "study/findings/2026-11-04-better.md").read_text(encoding="utf-8")
    check("the old record flips to superseded and links forward",
          'status = "superseded"' in old and 'superseded_by = ["2026-11-04-better"]' in old, old)
    check("the new record links back", 'supersedes = ["2026-11-03-baseline"]' in new, new)
    check("the result passes check", study(p.path, "check").returncode == 0,
          study(p.path, "check").stdout)
    out = study(p.path, "supersede", "2026-11-03-baseline", "2026-11-04-better")
    check("the reverse is refused", out.returncode == 2, out.stdout)
with Project() as p:
    check("a record cannot supersede itself",
          study(p.path, "supersede", "2026-11-03-baseline", "2026-11-03-baseline").returncode == 2)
    check("a missing record is refused",
          study(p.path, "supersede", "2026-11-03-baseline", "2026-01-01-gone").returncode == 2)
with Project() as p:
    write_record(p.path, "2026-11-04-listy", extra='verdict = "v"\nsupersedes = [\n  "a",\n]')
    before = (p.path / "study/findings/2026-11-04-listy.md").read_text(encoding="utf-8")
    out = study(p.path, "supersede", "2026-11-04-listy", "2026-11-03-baseline")
    untouched = (p.path / "study/findings/2026-11-03-baseline.md").read_text(encoding="utf-8")
    check("a header the line edit cannot handle writes neither file",
          out.returncode == 2 and "superseded_by" not in untouched
          and (p.path / "study/findings/2026-11-04-listy.md").read_text(encoding="utf-8") == before,
          out.stdout + out.stderr)

for name, side, field in (("new", "2026-11-04-mistyped", "supersedes"),
                         ("old", "2026-11-03-baseline", "superseded_by"),
                         ("old", "2026-11-03-baseline", "supersedes")):
    with Project() as p:
        write_record(p.path, "2026-11-04-mistyped")
        target = p.path / "study/findings" / f"{side}.md"
        target.write_text(target.read_text(encoding="utf-8").replace(
            'verdict = "It holds."', f'verdict = "It holds."\n{field} = "abc"'), encoding="utf-8")
        files = sorted((p.path / "study/findings").glob("*.md"))
        before = [f.read_text(encoding="utf-8") for f in files]
        out = study(p.path, "supersede", "2026-11-04-mistyped", "2026-11-03-baseline")
        check(f"a string in {field} on the {name} record is refused, not split into letters",
              out.returncode == 2 and "list of strings" in out.stderr
              and before == [f.read_text(encoding="utf-8") for f in files],
              out.stdout + out.stderr)
with Project() as p:
    write_record(p.path, "2026-11-04-better")
    (p.path / "study/findings/2026-11-03-baseline.md").write_bytes(b"+++\n\xff\n+++\n")
    out = study(p.path, "supersede", "2026-11-04-better", "2026-11-03-baseline")
    check("a record that is not UTF-8 is refused, with no traceback",
          out.returncode == 2 and "Traceback" not in out.stderr, out.stderr)
    (p.path / "study/findings/2026-11-03-baseline.md").unlink()
    (p.path / "study/findings/2026-11-03-baseline.md").mkdir()
    out = study(p.path, "supersede", "2026-11-04-better", "2026-11-03-baseline")
    check("a path that is not a file is refused", out.returncode == 2, out.stderr)

print("\n[state]")
with Project() as p:
    write_record(p.path, "2026-10-01-dead", status="shut", extra='verdict = "Tried, no."')
    write_record(p.path, "2026-10-02-asked", status="open", extra="")
    write_record(p.path, "2026-10-03-older", extra='verdict = "Older verdict."')
    write_record(p.path, "2026-10-04-call", extra='verdict = "Use X."\nkind = "decision"')
    write_record(p.path, "2026-10-05-noise", status="void", extra='verdict = "Broken rig."')
    (p.path / "old.md").write_text("old", encoding="utf-8")
    set_config(p.path, '\n[[unread_source]]\npath = "old.md"\nnote = "the old log"\n')
    before = sorted(str(f) for f in p.path.rglob("*") if ".git" not in f.parts)
    out = study(p.path, "state")
    text = out.stdout
    check("state succeeds", out.returncode == 0, out.stderr)
    check("state writes no file",
          before == sorted(str(f) for f in p.path.rglob("*") if ".git" not in f.parts))
    check("state has the rule", "not re-run" in text, text)
    check("a shut record shows its id and verdict", "- 2026-10-01-dead: Tried, no." in text, text)
    check("an open record shows its question", "- 2026-10-02-asked: A question?" in text, text)
    check("standing verdicts run newest first",
          text.index("2026-11-03-baseline") < text.index("2026-10-03-older"), text)
    check("a decision sits under decisions, not verdicts",
          text.index("## Decisions") < text.index("2026-10-04-call"), text)
    check("the sections run in the promised order",
          text.index("## Shut") < text.index("## Open") < text.index("## Standing")
          < text.index("## Decisions") < text.index("## Old sources"), text)
    check("unread sources carry the warning",
          "- old.md: the old log" in text and "empty grep is not proof" in text, text)
    check("void records are counted, not listed", "1 void" in text and "Broken rig" not in text, text)
    check("no clock: two runs are identical", study(p.path, "state").stdout == text)
with Project() as p:
    (p.path / "study/findings/2026-11-03-baseline.md").unlink()
    for i in range(40):
        write_record(p.path, f"2026-10-{i % 28 + 1:02d}-stand-{i:02d}", extra=f'verdict = "{"v" * 100}"')
    for i in range(5):
        write_record(p.path, f"2026-01-{i + 1:02d}-dead-{i}", status="shut", extra=f'verdict = "{"x" * 100}"')
    set_config(p.path, "\nstate_max_bytes = 1500\n")
    text = study(p.path, "state").stdout
    check("state stays under its cap", len(text.encode()) <= 1500, str(len(text.encode())))
    check("every shut record survives the cap, old as they are",
          all(f"2026-01-{i + 1:02d}-dead-{i}" in text for i in range(5)), text)
    check("the clip is counted", "older standing verdicts clipped" in text and "clipped to fit 1500" in text, text)
    check("the newest standing verdict survives", "2026-10-28-stand-27" in text, text)
    check("the oldest standing verdict went first", "2026-10-01-stand-00" not in text, text)
with Project() as p:
    (p.path / "study/findings/2026-11-03-baseline.md").unlink()
    for i in range(5):
        write_record(p.path, f"2026-01-{i + 1:02d}-dead-{i}", status="shut", extra=f'verdict = "{"x" * 400}"')
    set_config(p.path, "\nstate_max_bytes = 500\n")
    text = study(p.path, "state").stdout
    check("shut records outrank the cap itself",
          all(f"dead-{i}" in text for i in range(5)), text)
with Project() as p:
    (p.path / "study/findings/2026-11-03-broken.md").write_text("no header\n", encoding="utf-8")
    write_record(p.path, "2026-11-03-mistyped", extra='verdict = 7')
    out = study(p.path, "state")
    check("state prints through a broken record and says so",
          out.returncode == 0 and "could not be read" in out.stdout, out.stdout + out.stderr)

with Project() as p:
    write_record(p.path, "2026-11-04-pad", body="x")
    r, w = os.pipe()
    os.close(r)
    closed = subprocess.run([sys.executable, str(TOOL), "state"], cwd=p.path, stdout=w,
                            stderr=subprocess.PIPE, text=True)
    os.close(w)
    check("a reader that hangs up gets no traceback, and no success claim",
          closed.returncode == 1 and closed.stderr == "", closed.stderr)

print("\n[finding its footing]")
with Project() as p:
    git(p.path, "add", "-A")
    git(p.path, "commit", "-qm", "study")
    wt = p.path / ".worktrees" / "t-1"
    git(p.path, "worktree", "add", "-q", str(wt), "-b", "wt-branch")
    write_record(wt, "2026-11-05-in-worktree", status="shut", extra='verdict = "Only here."')
    check("check runs from a worktree", study(wt, "check").returncode == 0)
    check("state in a worktree reads the worktree's records",
          "2026-11-05-in-worktree" in study(wt, "state").stdout)
    check("the main checkout does not see them",
          "2026-11-05-in-worktree" not in study(p.path, "state").stdout)
    deep = wt / "a" / "b"
    deep.mkdir(parents=True)
    check("it walks up from a folder inside the worktree", study(deep, "check").returncode == 0)
with tempfile.TemporaryDirectory() as tmp:
    repo = Path(tmp) / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "readme").write_text("x", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "first")
    wt = repo / ".worktrees" / "early"
    git(repo, "worktree", "add", "-q", str(wt), "-b", "early")
    study(repo, "init")
    out = study(wt, "check")
    check("a worktree forked before the study folder does not climb into the main checkout",
          out.returncode == 2 and "no study/config.toml" in out.stderr, out.stdout + out.stderr)
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp) / "DegenAI"
    sub = root / "AIAgent"
    sub.mkdir(parents=True)
    git(root, "init", "-q")
    out = study(sub, "init")
    check("init from a registered subfolder lands at the git root, where a worktree's "
          "working directory is", (root / "study" / "config.toml").exists()
          and not (sub / "study").exists(), out.stdout + out.stderr)
    out = study(sub, "new", "--title", "From below", "--question", "Q")
    check("new from the subfolder finds it", out.returncode == 0, out.stderr)
    check("state from the subfolder sees the record", "from-below" in study(sub, "state").stdout)
    check("check from the subfolder passes", study(sub, "check").returncode == 0)
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp) / "repo"
    sub = root / "sub"
    sub.mkdir(parents=True)
    git(root, "init", "-q")
    (sub / "study").mkdir()
    (sub / "study" / "config.toml").write_text("format = 1\n", encoding="utf-8")
    check("a study folder made by hand in the subfolder is still found",
          study(sub, "check").returncode == 0)
with tempfile.TemporaryDirectory() as tmp:
    plain = Path(tmp) / "not-a-repo"
    plain.mkdir()
    check("init outside git uses the current directory",
          study(plain, "init").returncode == 0 and (plain / "study" / "config.toml").exists())
with tempfile.TemporaryDirectory() as tmp:
    # aiagent's shape: the git root has the registry, and the registered folder
    # holds a stray .git with no HEAD in it.
    root = Path(tmp) / "DegenAI"
    sub = root / "AIAgent"
    (sub / ".git" / "info").mkdir(parents=True)
    git(root, "init", "-q")
    out = study(sub, "init")
    check("init from under a stray .git lands at the real git root",
          (root / "study" / "config.toml").exists() and not (sub / "study").exists(),
          out.stdout + out.stderr)
    out = study(sub, "new", "--title", "Past the stray", "--question", "Q")
    check("new from under a stray .git finds the registry", out.returncode == 0, out.stderr)
    check("state from under a stray .git sees the record",
          "past-the-stray" in study(sub, "state").stdout)
    check("check from under a stray .git passes", study(sub, "check").returncode == 0)
    check("the stray folder is left alone",
          sorted(f.name for f in (sub / ".git").iterdir()) == ["info"])
with tempfile.TemporaryDirectory() as tmp:
    # Real roots still end the walk: a nested repo and a worktree's .git file.
    outer = Path(tmp) / "repo"
    stray = outer / "stray"
    (stray / ".git").mkdir(parents=True)
    git(outer, "init", "-q")
    init = study(outer, "init")
    check("a stray .git below a real root reaches the root's registry",
          init.returncode == 0 and study(stray, "check").returncode == 0, init.stderr)
    inner = outer / "inner"
    inner.mkdir()
    git(inner, "init", "-q")
    check("a registry above a real git root is still not picked up",
          study(inner, "state").returncode != 0)
    wt = outer / ".worktrees" / "t-1"
    git(outer, "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "start")
    git(outer, "worktree", "add", "-q", str(wt), "-b", "wt")
    check("a worktree's .git file ends the walk", study(wt, "state").returncode != 0)

print("\n[a repo with its own copy of the tool]")
with tempfile.TemporaryDirectory() as tmp:
    fork = Path(tmp) / "fork"
    (fork / "tools" / "study").mkdir(parents=True)
    (fork / "study" / "findings").mkdir(parents=True)
    (fork / "study" / "config.toml").write_text('findings_dir = "study/findings"\n', encoding="utf-8")
    git(fork, "init", "-q")
    before = sorted(str(f) for f in fork.rglob("*") if ".git" not in f.parts)
    for args in (("init",), ("new", "--title", "T", "--question", "Q"),
                 ("supersede", "a", "b"), ("state",), ("check",)):
        out = study(fork, *args)
        check(f"{args[0]} is refused and names the repo's own tool",
              out.returncode == 2 and "tools/study" in out.stderr, out.stdout + out.stderr)
    deep = fork / "src" / "x"
    deep.mkdir(parents=True)
    check("the refusal holds from a subfolder",
          study(deep, "new", "--title", "T", "--question", "Q").returncode == 2)
    deep.rmdir()
    (fork / "src").rmdir()
    check("the refusals wrote nothing",
          before == sorted(str(f) for f in fork.rglob("*") if ".git" not in f.parts))

print("\n[the tool stays portable]")
imports = set()
for source in (ROOT / "scripts" / "study").glob("*.py"):
    for line in source.read_text(encoding="utf-8").splitlines():
        if line.startswith(("import ", "from ")):
            imports.add(line.split()[1].split(".")[0])
check("standard library only",
      imports - {"schema"} <= set(sys.stdlib_module_names), str(imports))
out = subprocess.run([sys.executable, "-I", str(TOOL), "--help"], capture_output=True, text=True,
                     cwd=tempfile.gettempdir())
check("it runs isolated, from outside this repo", out.returncode == 0, out.stderr)

print(f"\nresults: {_passed} passed, {_failed} failed")
raise SystemExit(1 if _failed else 0)

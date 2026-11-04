#!/usr/bin/env python3
"""One way of recording findings, called from one place.

    python3 /home/quincy/Programming/claude-telegram-bot/scripts/study/study.py <command>

    init                                  start a study folder in this repo
    new --title T --question Q [--kind K] [--status S] [--verdict V]
    supersede NEW OLD                     NEW replaces OLD, both files written
    state                                 what a session reads first; printed, no file
    check                                 report every failure, exit non-zero on any

Works from any repo and any worktree with no install: it finds the project by
walking up from the current directory to study/config.toml, and stops at the
git root, so a worktree forked before the repo had a study folder never reads
the main checkout's. Standard library only. The reasoning, and what must not
drift, is in docs/design/findings.md.

Exit codes: 0 fine, 1 `check` found failures, 2 the command was refused.
"""
import argparse
import datetime
import os
import re
import sys
import tomllib
import unicodedata

# Called from every repo at once, by sessions that do not own this checkout.
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import schema  # noqa: E402

CONFIG_PATH = os.path.join("study", "config.toml")
FINDINGS_DIR = os.path.join("study", "findings")
FORK_DIR = os.path.join("tools", "study")
FORMAT = 1
DEFAULT_BODY_MAX_WORDS = 300
DEFAULT_STATE_MAX_BYTES = 20000
CONFIG_KEYS = ("format", "body_max_words", "state_max_bytes", "unread_source")
SLUG_MAX = 48

CONFIG_TEMPLATE = """\
# The findings registry of this repo. See `study.py state` for what is in it.
format = 1

# Files that predate the registry and may hold findings nobody has moved here.
# [[unread_source]]
# path = "docs/old-notes.md"
# note = "one line on what is in it"
"""

SCAFFOLD_BODY = """\
What was asked, and why it was worth asking.

What was done, and what came back.

What this does not establish.
"""

RULE = (
    "Read this and grep study/findings before proposing work. A shut record is "
    "answered with its id and not re-run. To disagree, write a superseding "
    "record. A record lands in the same commit as its work, and its last "
    "paragraph says what it does not establish."
)
UNREAD_WARNING = (
    "An empty grep is not proof nothing was tried: these have not been moved "
    "into records yet."
)


def die(message):
    print("study: %s" % message, file=sys.stderr)
    raise SystemExit(2)


def walk_up(start):
    """`start` and its parents, up to and including the git root."""
    d = os.path.abspath(start)
    while True:
        yield d
        parent = os.path.dirname(d)
        if os.path.exists(os.path.join(d, ".git")) or parent == d:
            return
        d = parent


def find_project(start):
    for d in walk_up(start):
        if os.path.isfile(os.path.join(d, CONFIG_PATH)):
            return d
    return None


def refuse_fork(start):
    """Three repos carry their own copy of the tool, with different required
    fields, and writing or judging their records from here would be wrong."""
    for d in walk_up(start):
        fork = os.path.join(d, FORK_DIR)
        if os.path.isdir(fork):
            die("%s has its own study tool: use %s (for records, record.py; "
                "for the state, state.py; for checks, check.py)" % (d, fork))


def require_project(start):
    refuse_fork(start)
    root = find_project(start)
    if root is None:
        die("no %s here or above, up to the git root. Run `init` to start one."
            % CONFIG_PATH)
    return root


def read_text(path):
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def write_text(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def load_config(root):
    """(config, problem): a broken config reads as defaults plus one problem,
    so `state` still prints what it can."""
    try:
        with open(os.path.join(root, CONFIG_PATH), "rb") as fh:
            return tomllib.load(fh), None
    except (OSError, ValueError) as exc:  # ValueError: bad TOML, or not UTF-8
        return {}, "%s does not read: %s" % (CONFIG_PATH, exc)


def positive_int(cfg, key, default):
    value = cfg.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return default


def unread_sources(cfg):
    entries = cfg.get("unread_source", [])
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def load_records(root):
    """(records, broken): `broken` is (filename, why) for each unreadable file."""
    records, broken = [], []
    d = os.path.join(root, FINDINGS_DIR)
    if not os.path.isdir(d):
        return records, broken
    for name in sorted(os.listdir(d)):
        if not name.endswith(".md"):
            continue
        try:
            meta = schema.parse_text(read_text(os.path.join(d, name)))
        except (OSError, UnicodeDecodeError, schema.RecordError) as exc:
            broken.append((name, str(exc)))
            continue
        meta["_name"] = name
        records.append(meta)
    return records, broken


# --- init, new, supersede --------------------------------------------------------

def cmd_init(args):
    start = os.getcwd()
    refuse_fork(start)
    existing = find_project(start)
    if existing:
        die("%s already has %s" % (existing, CONFIG_PATH))
    # The git root, not the current directory. A build worktree's working
    # directory is the git root even when the repo is registered as a subfolder
    # of it, so anywhere else the sensor and the plan reviewer would miss it.
    top = list(walk_up(start))[-1]
    root = top if os.path.exists(os.path.join(top, ".git")) else start
    os.makedirs(os.path.join(root, FINDINGS_DIR), exist_ok=True)
    write_text(os.path.join(root, CONFIG_PATH), CONFIG_TEMPLATE)
    write_text(os.path.join(root, FINDINGS_DIR, ".gitkeep"), "")
    print("wrote %s and %s in %s" % (CONFIG_PATH, FINDINGS_DIR, root))
    return 0


def slugify(title):
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    words = re.findall(r"[a-z0-9]+", ascii_title.lower())
    slug = ""
    for word in words:
        joined = "%s-%s" % (slug, word) if slug else word
        if len(joined) > SLUG_MAX:
            break
        slug = joined
    return slug or (words[0][:SLUG_MAX] if words else "finding")


def cmd_new(args):
    root = require_project(os.getcwd())
    if not (args.title.strip() and args.question.strip()):
        die("nothing written: --title and --question have to say something")
    status = args.status or ("stands" if args.kind == "decision" else "open")
    date = datetime.date.today().isoformat()
    record_id = "%s-%s" % (date, slugify(args.title))
    lines = [
        schema.FENCE,
        "id = %s" % schema.toml_str(record_id),
        "title = %s" % schema.toml_str(args.title),
        "date = %s" % schema.toml_str(date),
        "status = %s" % schema.toml_str(status),
        "question = %s" % schema.toml_str(args.question),
    ]
    if args.kind != "finding":
        lines.append("kind = %s" % schema.toml_str(args.kind))
    if args.verdict or status != "open":
        lines.append("verdict = %s" % schema.toml_str(args.verdict or ""))
    else:
        lines.append('# verdict = ""  # one line, required once the status is not open')
    lines.append(schema.FENCE)
    text = "\n".join(lines) + "\n" + SCAFFOLD_BODY
    # Whatever a title held (bytes that are not UTF-8 arrive as lone surrogates),
    # never leave a file that `check` and the bot's reader cannot read.
    try:
        schema.parse_text(text)
        text.encode("utf-8")
    except (schema.RecordError, UnicodeError) as exc:
        die("nothing written: %s" % exc)

    if args.kind == "decision" and status not in schema.DECISION_STATUSES:
        die("nothing written: a decision record must be %s"
            % " or ".join(schema.DECISION_STATUSES))
    dest = os.path.join(root, FINDINGS_DIR, record_id + ".md")
    if os.path.exists(dest):
        die("nothing written: %s already exists, give this one a different title"
            % record_id)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    write_text(dest, text)
    print(os.path.relpath(dest, root))
    if status != "open" and not args.verdict:
        print("this record needs a verdict before `check` passes")
    return 0


def _set_field(text, field, rendered):
    """Set one header field: replace its line, else its commented-out line, else
    add it. The body is never touched, however much of it looks like a field."""
    lines = text.split("\n")
    close = next(i for i in range(1, len(lines)) if lines[i].strip() == schema.FENCE)
    for pattern in (r"%s\s*=", r"#\s*%s\s*="):
        pat = re.compile(pattern % re.escape(field))
        for i in range(1, close):
            if pat.match(lines[i]):
                lines[i] = rendered
                return "\n".join(lines)
    lines.insert(close, rendered)
    return "\n".join(lines)


def _link_list(text, field):
    """A link field to edit. A mistyped one is refused, not rewritten: a string
    would otherwise come back as a list of its characters."""
    value = schema.parse_text(text).get(field, [])
    if not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
        raise schema.RecordError("field %r must be a list of strings" % field)
    return value


def _add_to_list(text, field, value):
    values = sorted(set(_link_list(text, field)) | {value})
    rendered = ", ".join(schema.toml_str(v) for v in values)
    return _set_field(text, field, "%s = [%s]" % (field, rendered))


def cmd_supersede(args):
    root = require_project(os.getcwd())
    if args.new == args.old:
        die("%s cannot supersede itself" % args.new)
    d = os.path.join(root, FINDINGS_DIR)
    new_path, old_path = (os.path.join(d, i + ".md") for i in (args.new, args.old))
    for record_id, path in ((args.new, new_path), (args.old, old_path)):
        if not os.path.isfile(path):
            die("no record %s at %s" % (record_id, os.path.relpath(path, root)))
    try:
        new_text = read_text(new_path)
        old_text = read_text(old_path)
        if args.new in _link_list(old_text, "supersedes"):
            die("nothing written: %s already supersedes %s" % (args.old, args.new))
        new_text = _add_to_list(new_text, "supersedes", args.old)
        old_text = _add_to_list(old_text, "superseded_by", args.new)
        old_text = _set_field(old_text, "status", 'status = "superseded"')
        # A hand-edited header can defeat the line edit (a list over several
        # lines): read both back, and write neither unless both came out right.
        new_meta, old_meta = schema.parse_text(new_text), schema.parse_text(old_text)
    except (schema.RecordError, UnicodeDecodeError) as exc:
        die("nothing written: %s" % exc)
    if (args.old not in new_meta.get("supersedes", [])
            or args.new not in old_meta.get("superseded_by", [])
            or old_meta.get("status") != "superseded"):
        die("nothing written: a header could not be edited line by line, fix it by hand")
    write_text(new_path, new_text)
    write_text(old_path, old_text)
    print("%s now supersedes %s" % (args.new, args.old))
    return 0


# --- state -----------------------------------------------------------------------

def _one_line(value):
    return " ".join(value.split()) if isinstance(value, str) else ""


def _sort_key(meta):
    return (str(meta.get("date", "")), str(meta.get("id", meta["_name"])))


def _entry(meta, field):
    record_id = _one_line(meta.get("id")) or meta["_name"][:-3]
    return "- %s: %s" % (record_id, _one_line(meta.get(field)) or _one_line(meta.get("title")))


def _section(title, lines, clipped, clipped_label):
    if not lines and not clipped:
        return []
    out = ["", "## %s (%d)" % (title, len(lines) + clipped), *lines]
    if clipped:
        out.append("(%d older %s clipped to fit: grep %s)"
                   % (clipped, clipped_label, FINDINGS_DIR))
    return out


def render_state(records, broken, sources, max_bytes):
    newest_first = sorted(records, key=_sort_key, reverse=True)

    def of(status, decision):
        return [m for m in newest_first if m.get("status") == status
                and (m.get("kind") == "decision") == decision]
    shut = [_entry(m, "verdict") for m in newest_first if m.get("status") == "shut"]
    open_q = [_entry(m, "question") for m in newest_first if m.get("status") == "open"]
    standing = [_entry(m, "verdict") for m in of("stands", False)]
    decisions = [_entry(m, "verdict") for m in of("stands", True)]
    others = {}
    for m in records:
        if m.get("status") in ("void", "superseded", "exploratory"):
            others[m["status"]] = others.get(m["status"], 0) + 1
    clipped = {"standing": 0, "decisions": 0, "open": 0}

    def render():
        out = ["# Findings", "", RULE]
        out += _section("Shut: do not retry", shut, 0, "")
        out += _section("Open questions", open_q, clipped["open"], "open questions")
        out += _section("Standing verdicts, newest first", standing,
                        clipped["standing"], "standing verdicts")
        out += _section("Decisions", decisions, clipped["decisions"], "decisions")
        if sources:
            out += ["", "## Old sources not yet read (%d)" % len(sources), UNREAD_WARNING]
            out += ["- %s: %s" % (_one_line(e.get("path")), _one_line(e.get("note")))
                    for e in sources]
        if others:
            out += ["", "Not listed: " + ", ".join(
                "%d %s" % (n, s) for s, n in sorted(others.items()))]
        if broken:
            out += ["", "%d record(s) could not be read, so a shut record may be "
                        "missing above: run `check`." % len(broken)]
        total = sum(clipped.values())
        if total:
            out += ["", "%d record(s) clipped to fit %d bytes. Shut records are "
                        "never clipped." % (total, max_bytes)]
        return "\n".join(out) + "\n"

    # Standing verdicts go first, then decisions, then open questions, oldest
    # first in each. Shut records never go: they are the point of the list.
    text = render()
    for key, bucket in (("standing", standing), ("decisions", decisions), ("open", open_q)):
        while bucket and len(text.encode("utf-8")) > max_bytes:
            over = len(text.encode("utf-8")) - max_bytes
            while over > 0 and bucket:
                over -= len(bucket.pop().encode("utf-8")) + 1
                clipped[key] += 1
            text = render()
    return text


def cmd_state(args):
    root = require_project(os.getcwd())
    cfg, problem = load_config(root)
    if problem:
        print("study: %s" % problem, file=sys.stderr)
    records, broken = load_records(root)
    max_bytes = positive_int(cfg, "state_max_bytes", DEFAULT_STATE_MAX_BYTES)
    sys.stdout.write(render_state(records, broken, unread_sources(cfg), max_bytes))
    return 0


# --- check -----------------------------------------------------------------------

def _config_problems(root, cfg):
    failures, warnings = [], []
    if cfg.get("format") != FORMAT:
        failures.append("%s: format must be %d, found %r"
                        % (CONFIG_PATH, FORMAT, cfg.get("format")))
    for key in cfg:
        if key not in CONFIG_KEYS:
            warnings.append("%s: unknown key %r" % (CONFIG_PATH, key))
    for key in ("body_max_words", "state_max_bytes"):
        if key in cfg and positive_int(cfg, key, None) is None:
            failures.append("%s: %s must be a positive integer" % (CONFIG_PATH, key))
    entries = cfg.get("unread_source", [])
    if not (isinstance(entries, list) and all(isinstance(e, dict) for e in entries)):
        failures.append("%s: unread_source must be written [[unread_source]]" % CONFIG_PATH)
    for entry in unread_sources(cfg):
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            failures.append("%s: an unread_source has no path" % CONFIG_PATH)
        elif not os.path.exists(os.path.join(root, path)):
            failures.append("%s: unread_source %s does not exist" % (CONFIG_PATH, path))
    return failures, warnings


def _usable_links(meta, field):
    """The ids in a link field. Mistyped ones are `field_problems`' to report, on
    the record that holds them, and must not stop the check of any other."""
    links = meta.get(field, [])
    return [v for v in links if isinstance(v, str)] if isinstance(links, list) else []


def _link_problems(meta, by_id):
    """Supersede links resolve, are symmetric, and agree with the status."""
    failures = []
    this = meta.get("id")
    for field, back in (("supersedes", "superseded_by"), ("superseded_by", "supersedes")):
        for other_id in _usable_links(meta, field):
            other = by_id.get(other_id)
            if other is None:
                failures.append("%s %s does not resolve" % (field, other_id))
            elif this not in _usable_links(other, back):
                failures.append("%s %s, but %s has no %s link back"
                                % (field, other_id, other_id, back))
    if (meta.get("status") == "superseded") != bool(meta.get("superseded_by")):
        failures.append("status superseded and superseded_by must come together")
    return failures


def cmd_check(args):
    root = require_project(os.getcwd())
    cfg, problem = load_config(root)
    failures, warnings = [problem] if problem else [], []
    if not problem:
        f, w = _config_problems(root, cfg)
        failures += f
        warnings += w
    records, broken = load_records(root)
    failures += ["%s: %s" % (name, why) for name, why in broken]
    by_id = {m["id"]: m for m in records if isinstance(m.get("id"), str)}
    max_words = positive_int(cfg, "body_max_words", DEFAULT_BODY_MAX_WORDS)
    for meta in records:
        name = meta["_name"]
        f, w = schema.field_problems(meta)
        record_id = meta.get("id")
        if isinstance(record_id, str):
            if record_id != name[:-3]:
                f.append("id %r does not match the filename" % record_id)
            elif not schema.ID_RE.match(record_id):
                w.append("id is not YYYY-MM-DD-short-slug, so a parallel branch could collide")
        f += _link_problems(meta, by_id)
        words = len(meta["_body"].split())
        if words > max_words:
            f.append("body is %d words, over the cap of %d" % (words, max_words))
        failures += ["%s: %s" % (name, m) for m in f]
        warnings += ["%s: %s" % (name, m) for m in w]
    for message in warnings:
        print("WARN %s" % message)
    for message in failures:
        print("FAIL %s" % message)
    print("study check: %d records, %d failures, %d warnings"
          % (len(records) + len(broken), len(failures), len(warnings)))
    return 1 if failures else 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="study.py", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="start a study folder in this repo").set_defaults(func=cmd_init)
    new = sub.add_parser("new", help="scaffold a record")
    new.add_argument("--title", required=True)
    new.add_argument("--question", required=True)
    new.add_argument("--kind", choices=schema.KINDS, default="finding")
    # `superseded` is not offered: only `supersede` can write both ends of it.
    new.add_argument("--status", choices=[s for s in schema.STATUSES if s != "superseded"])
    new.add_argument("--verdict")
    new.set_defaults(func=cmd_new)
    supersede = sub.add_parser("supersede", help="NEW replaces OLD, both files written")
    supersede.add_argument("new", metavar="NEW")
    supersede.add_argument("old", metavar="OLD")
    supersede.set_defaults(func=cmd_supersede)
    sub.add_parser("state", help="print the state, write no file").set_defaults(func=cmd_state)
    sub.add_parser("check", help="report every failure").set_defaults(func=cmd_check)
    args = parser.parse_args(argv)
    try:
        code = args.func(args)
        sys.stdout.flush()
        return code
    except BrokenPipeError:
        # `state | head`: no traceback, and stdout must not raise again at exit.
        # Not 0, because a `check` nobody finished reading has not passed.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 1
    except OSError as exc:
        die(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())

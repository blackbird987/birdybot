"""The record format, and the only place that knows what a field means.

A record is one thing a later session would waste work or repeat a mistake
without. It is a plain markdown file whose first block is TOML fenced by `+++`
lines, so it stays readable with no tool at all:

    +++
    id = "2026-11-03-short-slug"
    title = "..."
    date = "2026-11-03"
    status = "shut"
    question = "..."
    verdict = "..."
    +++
    What was asked, what was done, what came back, and, last paragraph always,
    what this does not establish.

TOML because `tomllib` is in the standard library from 3.11. Nothing in this
package may import anything else: it runs from any repo, with no install. The
reasoning is in docs/design/findings.md.
"""
import json
import re
import tomllib

FENCE = "+++"

STATUSES = {
    "open": "asked, not yet answered",
    "stands": "answered, and the answer is currently believed",
    "shut": "tried, does not work, do not retry",
    "void": "a defect in the design was found; no verdict was earned",
    "superseded": "answered, and a later record replaced the answer",
    "exploratory": "illustrative only; no verdict was tested",
}
KINDS = ("finding", "decision")
DECISION_STATUSES = ("stands", "superseded")

REQUIRED = ("id", "title", "date", "status", "question")
# `source` may be a table too: a migrated record says which file and section it
# came from.
TYPES = {
    "id": str, "title": str, "date": str, "status": str, "question": str,
    "kind": str, "verdict": str, "supersedes": list, "superseded_by": list,
    "tags": list, "scripts": list, "source": (str, dict),
}

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-[a-z0-9]+(-[a-z0-9]+)*$")


class RecordError(Exception):
    """A file that is not in the record format at all."""


def split_front_matter(text):
    """Return (toml_source, body). Raises if the file is not a record."""
    lines = text.split("\n")
    if lines[0].strip() != FENCE:
        raise RecordError("first line must be %r" % FENCE)
    for i in range(1, len(lines)):
        if lines[i].strip() == FENCE:
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:]).strip()
    raise RecordError("header is never closed by a second %r line" % FENCE)


def parse_text(text):
    """The header fields of one record's text, plus `_body`."""
    front, body = split_front_matter(text)
    try:
        meta = tomllib.loads(front)
    except tomllib.TOMLDecodeError as exc:
        raise RecordError("header is not valid TOML: %s" % exc)
    meta["_body"] = body
    return meta


def toml_str(value):
    """One TOML basic string. JSON's escapes are all valid TOML ones."""
    return json.dumps(value, ensure_ascii=False)


def field_problems(meta):
    """Everything wrong with one record's fields: (failures, warnings).

    Unknown fields only warn, so a record written by a newer tool never breaks
    an older branch.
    """
    failures, warnings = [], []
    for field in REQUIRED:
        if field not in meta:
            failures.append("missing required field %r" % field)
    for field, value in meta.items():
        if field.startswith("_"):
            continue
        want = TYPES.get(field)
        if want is None:
            warnings.append("unknown field %r" % field)
        elif not isinstance(value, want):
            failures.append("field %r has the wrong type" % field)
        elif want is list and not all(isinstance(v, str) for v in value):
            failures.append("field %r must be a list of strings" % field)
    date = meta.get("date")
    if isinstance(date, str) and not DATE_RE.match(date):
        failures.append("date %r is not YYYY-MM-DD" % date)
    status = meta.get("status")
    if isinstance(status, str) and status not in STATUSES:
        failures.append("status %r is not one of %s" % (status, ", ".join(STATUSES)))
    kind = meta.get("kind", "finding")
    if isinstance(kind, str) and kind not in KINDS:
        failures.append("kind %r is not one of %s" % (kind, ", ".join(KINDS)))
    if kind == "decision" and isinstance(status, str) and status not in DECISION_STATUSES:
        failures.append("a decision record must be %s" % " or ".join(DECISION_STATUSES))
    if isinstance(status, str) and status != "open":
        verdict = meta.get("verdict")
        if not (isinstance(verdict, str) and verdict.strip()):
            failures.append("status %r needs a verdict" % status)
    return failures, warnings

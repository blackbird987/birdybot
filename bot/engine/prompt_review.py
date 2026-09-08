"""Weekly prompt review: turn the eval record into a proposed diff.

The bot already writes an eval for every session and already attributes each
recurring flag to the prompt block that was supposed to prevent it. Nobody
read them in aggregate. The first time anyone did, the top finding was a check
that contradicted the harness (see the tombstone in `eval.py`), and underneath
it a prompt-cache cost signal nothing had ever surfaced.

This module closes that loop, with two rules that are the whole design:

- **It proposes; it never applies.** The reviewing agent runs behind the
  read-only floor (`explore` mode plus ``bash_policy="none"``) so it cannot
  edit the instructions it is reviewing even if it decides it should. An agent
  allowed to rewrite its own constraints will eventually rewrite away the
  inconvenient one and explain why that was reasonable.
- **Frequency is not correctness.** The loudest flag is usually a rule being
  *disobeyed*, not a rule being *wrong*, and deleting it would be exactly
  backwards. Every row therefore has to be classified before it may become an
  edit, and only two of the three classifications are edit candidates.

The aggregation here is deterministic and runs before any model does. Feeding
thousands of eval JSON files to an agent is the failure mode this exists to
avoid: `build_digest` already collapses them, so the agent sees one ranked
table of at most `_MAX_ROWS` rows.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from bot import config

log = logging.getLogger(__name__)

# Caps on what reaches the agent. The point of the digest is that the input is
# bounded no matter how much the eval directory grows.
_MAX_ROWS = 25
_MAX_INPUT_CHARS = 8000

# Sentinel a run uses when it has nothing worth proposing. A quiet week must be
# cheap to report, and "no changes" is a valid, common answer.
NO_PROPOSALS = "NO PROPOSALS"

# Classifications. Only `contradicted` and `obsolete` may become edits.
CLASS_CONTRADICTED = "contradicted"
CLASS_DISOBEYED = "disobeyed"
CLASS_OBSOLETE = "obsolete"
EDITABLE_CLASSES = frozenset({CLASS_CONTRADICTED, CLASS_OBSOLETE})
ALL_CLASSES = EDITABLE_CLASSES | {CLASS_DISOBEYED}


# --- Deterministic aggregation ------------------------------------------------

def build_review_input(days: int | None = None) -> str:
    """Render the eval record for the last `days` as one bounded table.

    Reuses `eval.build_digest` and `eval.load_chain_evals` rather than
    re-walking `data/evals`: the digest already normalises the live numbers out
    of each message (so "took 22 turns" and "took 31 turns" are one row),
    already counts SESSIONS rather than raw occurrences, and already drops
    retired categories. A second scan here could only disagree with `/evals`.
    """
    from bot.engine import eval as eval_mod

    days = days or config.PROMPT_REVIEW_WINDOW_DAYS
    digest = eval_mod.build_digest(days=days, min_count=3)
    chains = eval_mod.load_chain_evals(since_hours=days * 24)

    lines: list[str] = [
        f"EVAL RECORD: last {digest.days} day(s), "
        f"{digest.sessions} session(s) evaluated",
    ]
    if digest.median_cache_hit_rate is not None:
        lines.append(
            f"Prompt-cache reuse: {digest.median_cache_hit_rate * 100:.0f}% median "
            f"across {digest.resumed_sessions} resumed session(s)"
        )
    if chains:
        merged = sum(1 for c in chains if c.outcome == "merged")
        lines.append(f"Chains: {len(chains)}, {merged} merged")
    if digest.suppressed_rows:
        lines.append(
            f"({digest.suppressed_rows} one-off flag(s) below the reporting "
            f"threshold are not listed.)"
        )

    if not digest.rows:
        lines.append("")
        lines.append("No recurring flags in this window.")
        return "\n".join(lines)

    lines.append("")
    lines.append("RECURRING FLAGS (sessions affected / total occurrences):")
    for row in digest.rows[:_MAX_ROWS]:
        share = f"{row.count}/{digest.sessions}" if digest.sessions else str(row.count)
        examples = ", ".join(row.examples[:2])
        lines.append(
            f"- [{row.severity}] {row.message}\n"
            f"  sessions={share} occurrences={row.occurrences} "
            f"category={row.category} owner={row.owner}"
            + (f" examples={examples}" if examples else "")
        )
    if len(digest.rows) > _MAX_ROWS:
        lines.append(f"- (…{len(digest.rows) - _MAX_ROWS} further row(s) not shown)")

    text = "\n".join(lines)
    if len(text) > _MAX_INPUT_CHARS:
        # Cut on a row boundary so the agent never reads half a finding and
        # treats the truncated remainder as the message.
        text = text[:_MAX_INPUT_CHARS].rsplit("\n- ", 1)[0]
        text += "\n- (…truncated: the table exceeded the input budget)"
    return text


# --- The agent brief ----------------------------------------------------------

_PROMPT_HEADER = """You are reviewing this bot's own instructions against its own \
eval record. You are proposing changes for a human to approve or reject. You \
cannot write to any file, and you should not try.

Every session this bot runs is scored by heuristic checks in \
`bot/engine/eval.py`, and each recurring flag is attributed to the prompt block \
that was supposed to prevent it. Owner names in CAPITALS are constants in \
`bot/config.py`; owners described in prose (e.g. "prompt assembly order \
(harness)") are code, not prompt text. The long-form rules also live in \
`CLAUDE.md` at the repo root.

Read the owning block before you say anything about it. A proposal that \
misquotes the text it wants to change is worse than no proposal.
"""

_PROMPT_RULES = """
## Classify every row before proposing anything

Frequency is not correctness. The loudest flag is usually a rule being ignored, \
and deleting a rule because it is hard to follow is exactly backwards. Assign \
each row in the table exactly one of:

- `contradicted`: the rule conflicts with another active instruction, or with \
what the harness actually does at runtime. EDIT CANDIDATE.
- `disobeyed`: the rule is correct and is simply not being followed. REPORT \
ONLY. Never propose editing, weakening or deleting it. (Over-long responses \
violate a mobile-readability rule; the rule is right.)
- `obsolete`: it fires so rarely that the prompt real estate spent on it is \
not paying for itself. EDIT CANDIDATE, and the proposal must SHRINK the block, \
not rewrite it.

Only `contradicted` and `obsolete` rows may become proposed edits.

## Prefer deletion

Every token in these blocks is paid on every run of every session, which is \
what the prompt-cache flag is measuring. A run whose proposals only ever ADD \
text has failed. Report the net line delta and prefer consolidation and \
deletion. Proposing nothing is a valid, common outcome.

## Budget

At most {max_proposals} proposal(s) this run. Pick the ones with evidence \
behind them, not the ones easiest to write.
"""

_PROMPT_OUTPUT = """
## Output format

If nothing in the table justifies a change, reply with exactly this one line \
and nothing else:

{sentinel}

Otherwise reply with a summary line, then one block per proposal in exactly \
this shape (the FILE/FLAG/CLASS lines are parsed, so keep them literal):

NET LINES: +<added> -<removed>

### PROPOSAL 1
FILE: <repo-relative path>
FLAG: <the flag message from the table that drove this>
CLASS: contradicted
WHY: <one paragraph: what the conflict is and what evidence in the table \
shows it>
```diff
<a unified diff against the current file>
```

Then, after the proposals, a short "Reported, not proposed" section listing \
each `disobeyed` row in one line each, so the reader can see what you \
deliberately left alone.
"""


def build_review_prompt(
    review_input: str,
    rejected: list[str] | None = None,
    max_proposals: int | None = None,
) -> str:
    """Assemble the full brief handed to the reviewing agent."""
    max_proposals = max_proposals or config.PROMPT_REVIEW_MAX_PROPOSALS
    parts = [
        _PROMPT_HEADER,
        "\n## The eval record\n\n```\n" + review_input + "\n```\n",
        _PROMPT_RULES.format(max_proposals=max_proposals),
    ]
    if rejected:
        # The stored fingerprint is opaque to a reader, so the brief carries
        # the human-readable targets instead. The fingerprint is what actually
        # enforces this (a re-proposal is dropped before it is posted); this
        # paragraph just saves the agent the wasted work.
        listed = "\n".join(f"- {t}" for t in rejected[:20])
        parts.append(
            "\n## Already rejected\n\nThese were proposed before and turned "
            "down. Do not propose them again unless you have new evidence in "
            "this window that the earlier decision was wrong, and say so "
            "explicitly if you do.\n\n" + listed + "\n"
        )
    parts.append(_PROMPT_OUTPUT.format(sentinel=NO_PROPOSALS))
    return "\n".join(parts)


# --- Parsing the agent's report ----------------------------------------------

_FILE_RE = re.compile(r"^\s*FILE\s*:\s*(.+?)\s*$", re.MULTILINE)
_FLAG_RE = re.compile(r"^\s*FLAG\s*:\s*(.+?)\s*$", re.MULTILINE)
_CLASS_RE = re.compile(r"^\s*CLASS\s*:\s*([A-Za-z]+)\s*$", re.MULTILINE)
_PROPOSAL_SPLIT_RE = re.compile(r"^#{2,4}\s*PROPOSAL\b", re.MULTILINE)
_NET_RE = re.compile(r"^\s*NET LINES\s*:\s*(.+?)\s*$", re.MULTILINE)


@dataclass
class Proposal:
    """One proposed edit, as parsed out of the agent's report."""
    file: str
    flag: str
    classification: str
    body: str = ""

    def target(self) -> str:
        """Human-readable identity, used in the next run's brief."""
        return f"{self.file} :: {self.flag}"


@dataclass
class ReviewReport:
    """What a completed review run produced."""
    raw: str
    proposals: list[Proposal] = field(default_factory=list)
    net_lines: str | None = None
    ignored: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.proposals


def parse_review(text: str) -> ReviewReport:
    """Pull the proposal blocks out of an agent report.

    Deliberately forgiving about surrounding prose and strict about the three
    parsed lines: a block missing FILE, FLAG or CLASS is dropped rather than
    guessed at, because a proposal whose target we cannot name is one the
    reject button could never suppress.
    """
    report = ReviewReport(raw=text or "")
    if not text:
        return report
    if text.strip().upper().startswith(NO_PROPOSALS):
        return report

    net = _NET_RE.search(text)
    if net:
        report.net_lines = net.group(1)

    chunks = _PROPOSAL_SPLIT_RE.split(text)[1:]
    for chunk in chunks:
        f = _FILE_RE.search(chunk)
        g = _FLAG_RE.search(chunk)
        c = _CLASS_RE.search(chunk)
        if not (f and g and c):
            report.ignored.append("proposal missing FILE/FLAG/CLASS")
            continue
        cls = c.group(1).strip().lower()
        if cls not in ALL_CLASSES:
            report.ignored.append(f"unknown classification {cls!r}")
            continue
        if cls not in EDITABLE_CLASSES:
            # A `disobeyed` row belongs in the report-only section. If one is
            # dressed up as a proposal anyway, drop it here rather than letting
            # the approve button turn "the rule is being ignored" into "delete
            # the rule".
            report.ignored.append(f"{cls} row proposed as an edit, dropped")
            continue
        report.proposals.append(Proposal(
            file=f.group(1).strip(),
            flag=g.group(1).strip(),
            classification=cls,
            body=chunk.strip(),
        ))

    limit = config.PROMPT_REVIEW_MAX_PROPOSALS
    if len(report.proposals) > limit:
        report.ignored.append(
            f"{len(report.proposals) - limit} proposal(s) over the cap of {limit}"
        )
        report.proposals = report.proposals[:limit]
    return report


def fingerprint(proposals: list[Proposal]) -> str:
    """Stable identity for a set of proposals.

    Hashes the (file, flag) pairs rather than the diff text, so a cosmetically
    different rerun of the same idea next week is still recognised as the thing
    that was already turned down.
    """
    targets = sorted({f"{p.file}\x00{p.flag}" for p in proposals})
    return hashlib.sha256("\x01".join(targets).encode("utf-8")).hexdigest()[:16]


# --- The weekly gate ----------------------------------------------------------

def should_run_now(
    last_run_at: str | None,
    now: datetime | None = None,
    interval_days: int | None = None,
) -> bool:
    """True when a review is due.

    Driven off a persisted timestamp, never off a tick counter: a reboot resets
    the counter, which would either fire immediately on every restart or skip
    the week entirely depending on which way the arithmetic fell.
    """
    if not config.PROMPT_REVIEW_ENABLED or not config.EVAL_ENABLED:
        return False
    now = now or datetime.now(timezone.utc)
    interval_days = interval_days or config.PROMPT_REVIEW_INTERVAL_DAYS
    if not last_run_at:
        # First ever tick: seed rather than fire, so enabling the feature does
        # not immediately spend a run on a window nobody asked about.
        return False
    try:
        last = datetime.fromisoformat(last_run_at)
    except (TypeError, ValueError):
        log.warning("Unparseable prompt-review timestamp %r, treating as due",
                    last_run_at)
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return now - last >= timedelta(days=interval_days)

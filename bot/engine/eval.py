"""Session evaluation — heuristic checks and chain-level analysis.

Runs after every completed instance (in finalize_run) and after every
autopilot chain completes. Produces structured eval data in data/evals/.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from bot import config
from bot.claude.types import Instance, InstanceOrigin

log = logging.getLogger(__name__)

EVALS_DIR: Path = config.DATA_DIR / "evals"

# --- Data model ---


@dataclass
class EvalFlag:
    """A single issue detected in a session."""
    category: str       # "tool_hygiene", "narration", "claim_grounding",
                        # "efficiency", "constraint_violation"
    severity: str       # "info", "warning", "issue"
    message: str
    evidence: str = ""

    def to_dict(self) -> dict:
        d: dict = {"category": self.category, "severity": self.severity,
                    "message": self.message}
        if self.evidence:
            d["evidence"] = self.evidence
        return d

    @classmethod
    def from_dict(cls, d: dict) -> EvalFlag:
        return cls(
            category=d["category"], severity=d["severity"],
            message=d["message"], evidence=d.get("evidence", ""),
        )


@dataclass
class SessionEval:
    """Evaluation of a single instance."""
    instance_id: str
    repo: str
    origin: str
    mode: str
    flags: list[EvalFlag] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    evaluated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "instance_id": self.instance_id,
            "repo": self.repo,
            "origin": self.origin,
            "mode": self.mode,
            "flags": [f.to_dict() for f in self.flags],
            "metrics": self.metrics,
            "evaluated_at": self.evaluated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> SessionEval:
        return cls(
            instance_id=d["instance_id"],
            repo=d.get("repo", ""),
            origin=d.get("origin", ""),
            mode=d.get("mode", ""),
            flags=[EvalFlag.from_dict(f) for f in d.get("flags", [])],
            metrics=d.get("metrics", {}),
            evaluated_at=d.get("evaluated_at", ""),
        )


@dataclass
class ChainEval:
    """Evaluation of a full autopilot chain."""
    chain_id: str
    repo: str
    topic: str
    steps_completed: list[str] = field(default_factory=list)
    steps_expected: list[str] = field(default_factory=list)
    total_cost: float = 0.0
    total_duration_ms: int = 0
    total_turns: int = 0
    revision_loops: int = 0
    code_review_rounds: int = 0
    deferred_count: int = 0
    outcome: str = ""       # "merged", "discarded", "abandoned", "failed", "needs_input", "review_did_not_converge"
    intervention: bool = False
    session_evals: list[str] = field(default_factory=list)
    flags: list[EvalFlag] = field(default_factory=list)
    evaluated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "chain_id": self.chain_id,
            "repo": self.repo,
            "topic": self.topic,
            "steps_completed": self.steps_completed,
            "steps_expected": self.steps_expected,
            "total_cost": self.total_cost,
            "total_duration_ms": self.total_duration_ms,
            "total_turns": self.total_turns,
            "revision_loops": self.revision_loops,
            "code_review_rounds": self.code_review_rounds,
            "deferred_count": self.deferred_count,
            "outcome": self.outcome,
            "intervention": self.intervention,
            "session_evals": self.session_evals,
            "flags": [f.to_dict() for f in self.flags],
            "evaluated_at": self.evaluated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ChainEval:
        return cls(
            chain_id=d["chain_id"],
            repo=d.get("repo", ""),
            topic=d.get("topic", ""),
            steps_completed=d.get("steps_completed", []),
            steps_expected=d.get("steps_expected", []),
            total_cost=d.get("total_cost", 0.0),
            total_duration_ms=d.get("total_duration_ms", 0),
            total_turns=d.get("total_turns", 0),
            revision_loops=d.get("revision_loops", 0),
            code_review_rounds=d.get("code_review_rounds", 0),
            deferred_count=d.get("deferred_count", 0),
            outcome=d.get("outcome", ""),
            intervention=d.get("intervention", False),
            session_evals=d.get("session_evals", []),
            flags=[EvalFlag.from_dict(f) for f in d.get("flags", [])],
            evaluated_at=d.get("evaluated_at", ""),
        )


# --- Prompt-cache accounting ---

# Below this share of reused prefix, a resumed session is paying close to full
# price for context it already sent — the signal that something upstream (a
# volatile block near the front of the prompt, a rebuilt system prompt) is
# invalidating the cached prefix.
LOW_CACHE_HIT_RATE = 0.5


def cache_hit_rate(inst: Instance) -> float | None:
    """Share of this run's prompt tokens served from the prompt cache.

    ``None`` means "we have no cache accounting for this run", which must not
    be flattened into 0.0 and read back as a cache miss. Two cases produce it:

    * the run reported no prompt tokens at all (errored before the first
      request, or a provider that doesn't report usage), and
    * both cache counters are zero while uncached input is not. Every request
      that carries a cache breakpoint either writes or reads, so all-zero
      counters mean the numbers never arrived, not that nothing was cached.
      Measured on 253 persisted instances: 3 sit in exactly this state, and
      each one recorded 69k-167k of context — plainly served from cache. They
      would otherwise have reported a fabricated 0% and, on a resume, tripped
      the low-reuse flag below with a finding the raw records don't support.

    A genuine total miss (a resume past the cache TTL, which rewrites the whole
    prefix) still has a positive cache-write count, so it returns 0.0 and does
    flag — which is the case the flag exists for.

    Note on scale: the cache counters are session totals summed across every
    assistant call, while ``input_tokens`` is the final result event's figure.
    Measured on real transcripts, uncached input runs about 2 tokens per call
    (~2.1k summed over 506 calls) against 53M of cache reads, so the mixed
    scale moves the ratio by <0.01 percentage points. Not worth a second token
    field to reconcile; recorded here so it needn't be re-derived.
    """
    read = inst.cache_read_tokens or 0
    written = inst.cache_creation_tokens or 0
    if read <= 0 and written <= 0:
        return None
    total = read + written + (inst.input_tokens or 0)
    if total <= 0:
        return None
    return round(read / total, 3)


# --- Per-instance heuristic checks ---

def evaluate_instance(inst: Instance) -> SessionEval:
    """Run all heuristic checks on a completed instance.

    Reads the result text from ``inst.result_file`` so callers don't need
    to keep the RunResult around — the Instance is the single source of
    truth for everything eval needs.
    """
    if not config.EVAL_ENABLED:
        return SessionEval(instance_id=inst.id, repo=inst.repo_name,
                           origin=inst.origin.value, mode=inst.mode)

    ev = SessionEval(
        instance_id=inst.id,
        repo=inst.repo_name,
        origin=inst.origin.value,
        mode=inst.mode,
        metrics={
            "turns": inst.num_turns,
            "input_tokens": inst.input_tokens,
            "output_tokens": inst.output_tokens,
            "cache_read_tokens": inst.cache_read_tokens,
            "cache_creation_tokens": inst.cache_creation_tokens,
            "cache_hit_rate": cache_hit_rate(inst),
            "resumed_session": inst.resumed_session,
            "cost": inst.cost_usd,
            "duration_ms": inst.duration_ms,
        },
        evaluated_at=datetime.now(timezone.utc).isoformat(),
    )

    text = inst.read_result_text()
    if text:
        ev.flags.extend(_check_narration(inst, text))
        ev.flags.extend(_check_verbosity(inst, text))
        ev.flags.extend(_check_claim_grounding(inst, text))
    ev.flags.extend(_check_tool_hygiene(inst))
    ev.flags.extend(_check_efficiency(inst))

    _save_eval(ev)
    return ev


# --- Heuristic checks ---

_READ_CMD_RE = re.compile(r'\b(cat|head|tail|less|sed\s+-n)\b')
_SEARCH_CMD_RE = re.compile(r'\b(grep|rg|find\s+\.\s+-name|find\s+\.\s+-type)\b')


def _check_narration(inst: Instance, text: str) -> list[EvalFlag]:
    """Did Claude narrate tool usage? (CHAT_APP_CONSTRAINT compliance)"""
    flags: list[EvalFlag] = []
    tools = set(inst.tools_used)

    # Edit/Write used but result doesn't describe what changed
    code_tools = {"Edit", "Write", "NotebookEdit"}
    used_code_tools = code_tools & tools
    if used_code_tools:
        narration_patterns = [
            r"(?i)(changed|updated|modified|added|removed|edited|rewrote|renamed)",
            r"(?i)(here'?s what|the change|diff|before.?after)",
            r"(?i)(now (accepts|returns|uses|filters|handles|includes))",
        ]
        has_narration = any(re.search(p, text) for p in narration_patterns)
        if not has_narration:
            flags.append(EvalFlag(
                category="narration", severity="warning",
                message="Code changes made but result doesn't describe what changed",
                evidence=f"Tools: {', '.join(sorted(used_code_tools))}",
            ))

    # Read/Grep used but very short response — may not be sharing findings
    read_tools = {"Read", "Grep", "Glob"}
    if read_tools & tools and len(text) < 50 and inst.origin == InstanceOrigin.DIRECT:
        flags.append(EvalFlag(
            category="narration", severity="info",
            message="Files read/searched but very short response — may be missing context for user",
        ))

    return flags


def _check_tool_hygiene(inst: Instance) -> list[EvalFlag]:
    """Check actual Bash commands for dedicated-tool-worthy operations."""
    flags: list[EvalFlag] = []

    for cmd in inst.bash_commands:
        # Skip very short commands (likely just `cd` or similar)
        if len(cmd) < 4:
            continue
        if _READ_CMD_RE.search(cmd):
            flags.append(EvalFlag(
                category="tool_hygiene", severity="warning",
                message="Bash used for file reading (should use Read tool)",
                evidence=cmd.split("\n")[0][:80],
            ))
        if _SEARCH_CMD_RE.search(cmd):
            flags.append(EvalFlag(
                category="tool_hygiene", severity="warning",
                message="Bash used for search (should use Grep/Glob)",
                evidence=cmd.split("\n")[0][:80],
            ))

    return flags


def _check_verbosity(inst: Instance, text: str) -> list[EvalFlag]:
    """Mobile-first constraint: is the response appropriately sized?"""
    flags: list[EvalFlag] = []

    # Direct queries shouldn't produce walls of text
    if inst.origin == InstanceOrigin.DIRECT and len(text) > 4000:
        para_count = text.count("\n\n") + 1  # separators + 1 = paragraphs
        avg_para_len = len(text) / para_count
        if avg_para_len > 300:
            flags.append(EvalFlag(
                category="constraint_violation", severity="info",
                message=(f"Long response ({len(text)} chars) with dense paragraphs "
                         f"(avg {avg_para_len:.0f} chars) — mobile readability concern"),
            ))

    return flags


def _check_claim_grounding(inst: Instance, text: str) -> list[EvalFlag]:
    """Did Claude make claims it couldn't verify? (HONESTY_CONSTRAINT)"""
    flags: list[EvalFlag] = []
    tools = set(inst.tools_used)

    # URLs in output but no WebFetch/WebSearch used
    urls = re.findall(r'https?://\S+', text)
    if urls and "WebFetch" not in tools and "WebSearch" not in tools:
        flags.append(EvalFlag(
            category="claim_grounding", severity="warning",
            message=f"Response contains {len(urls)} URL(s) but WebFetch/WebSearch not used",
            evidence=urls[0][:80],
        ))

    # "I verified/confirmed" without verification tools
    verification_claims = re.findall(
        r'(?i)\bI (?:verified|confirmed|checked|tested|validated)\b', text,
    )
    verification_tools = {"Bash", "WebFetch", "Read", "Grep"}
    if verification_claims and not (verification_tools & tools):
        flags.append(EvalFlag(
            category="claim_grounding", severity="issue",
            message="Claims verification but no verification tools were used",
            evidence=verification_claims[0],
        ))

    return flags


def _check_efficiency(inst: Instance) -> list[EvalFlag]:
    """Turn count and token efficiency."""
    flags: list[EvalFlag] = []

    # High turns for a query
    if inst.origin == InstanceOrigin.DIRECT and inst.num_turns > 15:
        flags.append(EvalFlag(
            category="efficiency", severity="warning",
            message=f"Query took {inst.num_turns} turns (expected <15)",
        ))

    # High turns for a build
    if inst.origin == InstanceOrigin.BUILD and inst.num_turns > 30:
        flags.append(EvalFlag(
            category="efficiency", severity="warning",
            message=f"Build took {inst.num_turns} turns (expected <30)",
        ))

    # Bloated context: high input:output ratio
    if inst.input_tokens and inst.output_tokens:
        ratio = inst.input_tokens / max(inst.output_tokens, 1)
        if ratio > 50:
            flags.append(EvalFlag(
                category="efficiency", severity="info",
                message=f"Token ratio {ratio:.0f}:1 input:output — context may be bloated",
            ))

    # Prompt-cache reuse — only meaningful on a resume.  A fresh spawn has no
    # prefix to hit, so flagging it would be pure noise.
    if inst.resumed_session:
        rate = cache_hit_rate(inst)
        if rate is not None and rate < LOW_CACHE_HIT_RATE:
            # Two causes look identical from here: a prefix that changes
            # between turns, and a thread left idle past the cache TTL. Say so
            # rather than pointing the reader at the first one as if it were
            # established — a single low reading is not evidence of a bug.
            flags.append(EvalFlag(
                category="efficiency", severity="info",
                message=(
                    f"Low prompt-cache reuse on resume ({rate * 100:.0f}% of prompt "
                    f"tokens cached) — either the cached prefix is changing "
                    f"between turns or the thread sat idle past the cache TTL"
                ),
                evidence=(
                    f"{inst.cache_read_tokens} cache-read, "
                    f"{inst.cache_creation_tokens} cache-write, "
                    f"{inst.input_tokens} uncached input"
                ),
            ))

    return flags


# --- Chain-level evaluation ---

def evaluate_chain(
    store,
    root_id: str,
    steps_expected: list[str],
    steps_completed: list[str],
    instances: list[Instance],
    outcome: str,
    intervention: bool = False,
) -> ChainEval:
    """Evaluate a completed autopilot chain."""
    if not config.EVAL_ENABLED:
        return ChainEval(chain_id=root_id, repo="", topic="")

    total_cost = sum(i.cost_usd or 0 for i in instances)
    total_duration = sum(i.duration_ms or 0 for i in instances)
    total_turns = sum(i.num_turns for i in instances)

    # Count revision/review loops from the store — the instances list only
    # contains one result per top-level step, but _review_plan_loop and
    # on_review_code internally spawn multiple sub-instances.
    session_id = instances[0].session_id if instances else None
    all_session_insts = (
        [i for i in store.list_instances(all_=True) if i.session_id == session_id]
        if session_id else instances
    )
    revision_loops = sum(
        1 for i in all_session_insts if i.origin == InstanceOrigin.APPLY_REVISIONS
    )
    code_review_rounds = sum(
        1 for i in all_session_insts if i.origin == InstanceOrigin.REVIEW_CODE
    )
    verify_rounds = sum(
        1 for i in all_session_insts if i.origin == InstanceOrigin.VERIFY
    )

    deferred_count = 0
    if instances:
        deferred_count = len(instances[-1].deferred_revisions)

    root = store.get_instance(root_id)
    topic = root.prompt[:100] if root else ""
    repo = root.repo_name if root else ""

    ev = ChainEval(
        chain_id=root_id,
        repo=repo,
        topic=topic,
        steps_completed=steps_completed,
        steps_expected=steps_expected,
        total_cost=total_cost,
        total_duration_ms=total_duration,
        total_turns=total_turns,
        revision_loops=revision_loops,
        code_review_rounds=code_review_rounds,
        deferred_count=deferred_count,
        outcome=outcome,
        intervention=intervention,
        session_evals=[i.id for i in instances],
        evaluated_at=datetime.now(timezone.utc).isoformat(),
    )

    # Chain-level flags
    if revision_loops >= 3:
        ev.flags.append(EvalFlag(
            category="efficiency", severity="warning",
            message=f"Plan needed {revision_loops} revision rounds — prompt may be under-specified",
        ))
    if code_review_rounds >= 3:
        ev.flags.append(EvalFlag(
            category="efficiency", severity="warning",
            message=f"Code review looped {code_review_rounds} times — review prompt may be too strict",
        ))
    if total_cost and total_cost > 1.0:
        ev.flags.append(EvalFlag(
            category="efficiency", severity="issue",
            message=f"Chain cost ${total_cost:.2f} — unusually expensive",
        ))
    if verify_rounds >= 2:
        ev.flags.append(EvalFlag(
            category="test_failure", severity="warning",
            message=f"Verification needed {verify_rounds} rounds — code had runtime issues",
        ))

    _save_chain_eval(ev)
    return ev


# --- Persistence ---

def _save_eval(ev: SessionEval) -> None:
    """Save a session eval to data/evals/."""
    try:
        EVALS_DIR.mkdir(parents=True, exist_ok=True)
        path = EVALS_DIR / f"{ev.instance_id}.json"
        path.write_text(
            json.dumps(ev.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        log.debug("Failed to save eval for %s", ev.instance_id, exc_info=True)


def _save_chain_eval(ev: ChainEval) -> None:
    """Save a chain eval to data/evals/."""
    try:
        EVALS_DIR.mkdir(parents=True, exist_ok=True)
        path = EVALS_DIR / f"chain-{ev.chain_id}.json"
        path.write_text(
            json.dumps(ev.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        log.debug("Failed to save chain eval for %s", ev.chain_id, exc_info=True)


def load_session_eval(instance_id: str) -> SessionEval | None:
    """Load the persisted SessionEval for a single instance, or None.

    Returns None if eval is disabled, the file doesn't exist, or it can't
    be parsed. Cheap one-shot read for embed-render time.
    """
    if not EVALS_DIR.exists():
        return None
    path = EVALS_DIR / f"{instance_id}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return SessionEval.from_dict(data)
    except Exception:
        log.debug("Failed to load eval for %s", instance_id, exc_info=True)
        return None


def load_evals(since_hours: int = 24) -> list[SessionEval]:
    """Load session evals from the last N hours."""
    if not EVALS_DIR.exists():
        return []
    cutoff = datetime.now(timezone.utc).timestamp() - (since_hours * 3600)
    evals: list[SessionEval] = []
    for path in EVALS_DIR.glob("*.json"):
        if path.name.startswith("chain-") or path.name.startswith("weekly-"):
            continue
        if path.stat().st_mtime < cutoff:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            evals.append(SessionEval.from_dict(data))
        except Exception:
            continue
    return evals


# --- Digest / attribution ---
#
# `/report` already counted these files, but only as volume — "125 flags" says
# nothing about what to change. A recurring flag is evidence that a specific
# piece of the system prompt is not doing its job, so each flag is attributed
# to the block that was supposed to prevent it. Anything without a clear owner
# is reported as unattributed rather than guessed at.
#
# Both session flags and chain flags are attributed through this table; the
# rules below covering revision rounds, review loops and chain cost match
# messages that only `evaluate_chain` produces, so a caller that skipped chain
# flags would leave those three rules permanently dead.

_ATTRIBUTION: tuple[tuple[str, str, str], ...] = (
    # (category, substring matched against the flag message, owning block)
    # First match wins, so ORDER IS LOAD-BEARING within a category: the
    # prompt-cache message also contains the word "turns", and must be matched
    # by its own rule before the generic turn-count rule below.
    ("claim_grounding", "verification", "HONESTY_CONSTRAINT"),
    ("claim_grounding", "url", "HONESTY_CONSTRAINT"),
    ("narration", "doesn't describe", "CHAT_APP_CONSTRAINT"),
    ("narration", "short response", "CHAT_APP_CONSTRAINT"),
    ("constraint_violation", "mobile", "MOBILE_HINT"),
    ("efficiency", "prompt-cache", "prompt assembly order (harness)"),
    ("efficiency", "context may be bloated", "context injection (harness)"),
    ("efficiency", "turns", "step prompt scope"),
    ("efficiency", "revision rounds", "PLAN_REVIEW_PROMPT"),
    ("efficiency", "code review looped", "CODE_REVIEW_PROMPT"),
    ("efficiency", "chain cost", "chain budget (harness)"),
    ("tool_hygiene", "", "tool gating (harness, not prompt-owned)"),
    ("test_failure", "", "VERIFY_PROMPT"),
)


def attribute_flag(category: str, message: str) -> str:
    """Name the prompt block or harness area responsible for a flag."""
    msg = (message or "").lower()
    for cat, needle, owner in _ATTRIBUTION:
        if cat == category and (not needle or needle in msg):
            return owner
    return "unattributed"


@dataclass
class DigestRow:
    """One recurring flag, with who owns it.

    ``count`` is the number of SESSIONS the flag appeared in, not the number
    of times it fired. Per-command checks (tool hygiene) can fire dozens of
    times in a single session, which would otherwise bury every other finding
    and make one talkative session look like a systemic problem.
    """
    category: str
    message: str
    count: int
    severity: str
    owner: str
    occurrences: int = 0
    examples: list[str] = field(default_factory=list)


@dataclass
class EvalDigest:
    """Aggregated eval findings over a window."""
    days: int
    sessions: int
    rows: list[DigestRow] = field(default_factory=list)
    median_cache_hit_rate: float | None = None
    resumed_sessions: int = 0
    suppressed_rows: int = 0    # distinct flags below the reporting threshold


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


_LIVE_NUMBER_RE = re.compile(r"\d+(\.\d+)?")


def normalise_flag_message(message: str) -> str:
    """Collapse the live numbers out of a flag message so copies group together.

    Flag text embeds the values that triggered it ("took 22 turns", "took 31
    turns"). Counted verbatim, every occurrence looks unique and nothing ever
    reaches a reporting threshold. Shared with `report.py` so the two commands
    that surface flags can never disagree about how many there were.
    """
    return _LIVE_NUMBER_RE.sub("N", message or "").strip()


def build_digest(
    days: int = 7,
    min_count: int = 3,
    evals: list[SessionEval] | None = None,
) -> EvalDigest:
    """Aggregate recent session evals into a reportable digest.

    Only flags seen in `min_count` or more SESSIONS are reported — a quiet week
    should produce a short message, not a wall of one-offs. The number of
    flags held back is reported so the threshold never hides its own effect.

    `evals` lets a caller that has already loaded the window pass it in rather
    than paying for a second scan of the eval directory.
    """
    if evals is None:
        evals = load_evals(since_hours=days * 24)
    digest = EvalDigest(days=days, sessions=len(evals))
    if not evals:
        return digest

    # Group identical flags on the normalised message.
    grouped: dict[tuple[str, str], dict] = {}
    for ev in evals:
        # A flag counts once per session no matter how often it fired there.
        # Per-command checks fire on every tool call, so raw occurrences would
        # let a single long session outrank a habit spread across fifty.
        seen_here: set[tuple[str, str]] = set()
        for flag in ev.flags:
            norm = normalise_flag_message(flag.message)
            key = (flag.category, norm)
            slot = grouped.setdefault(key, {
                "count": 0, "occurrences": 0, "severity": flag.severity,
                "examples": [],
            })
            slot["occurrences"] += 1
            if key not in seen_here:
                seen_here.add(key)
                slot["count"] += 1
                if len(slot["examples"]) < 2:
                    slot["examples"].append(ev.instance_id)
            # Keep the worst severity seen for this flag.
            order = {"info": 0, "warning": 1, "issue": 2}
            if order.get(flag.severity, 0) > order.get(slot["severity"], 0):
                slot["severity"] = flag.severity

    for (category, message), slot in grouped.items():
        if slot["count"] < min_count:
            digest.suppressed_rows += 1
            continue
        digest.rows.append(DigestRow(
            category=category, message=message, count=slot["count"],
            severity=slot["severity"], owner=attribute_flag(category, message),
            occurrences=slot["occurrences"], examples=slot["examples"],
        ))
    digest.rows.sort(key=lambda r: (r.count, r.occurrences), reverse=True)

    # Cache reuse is only meaningful for resumes; fresh spawns have no prefix
    # to hit and would drag the median toward zero for no reason.
    rates = [
        ev.metrics["cache_hit_rate"] for ev in evals
        if ev.metrics.get("resumed_session")
        and isinstance(ev.metrics.get("cache_hit_rate"), (int, float))
    ]
    digest.median_cache_hit_rate = _median([float(r) for r in rates])
    digest.resumed_sessions = len(rates)
    return digest


def load_chain_evals(since_hours: int = 24) -> list[ChainEval]:
    """Load chain evals from the last N hours."""
    if not EVALS_DIR.exists():
        return []
    cutoff = datetime.now(timezone.utc).timestamp() - (since_hours * 3600)
    evals: list[ChainEval] = []
    for path in EVALS_DIR.glob("chain-*.json"):
        if path.stat().st_mtime < cutoff:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            evals.append(ChainEval.from_dict(data))
        except Exception:
            continue
    return evals

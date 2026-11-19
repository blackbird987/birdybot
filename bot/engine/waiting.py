"""What is waiting on the owner, across every repo: one source for all of it.

The Ark shows every item, each Control Room shows its own repo's items, and
``/list attention`` prints the same list. Nothing else decides what "needs
you" means.

Every kind is read from its own record and closed by its own real event: a
tap, a reply, a merge, a deleted branch, an expiry the record already has.
Nothing here deletes or rewrites a stored record. Old records that cannot be
resolved (no branch on record, a pruned instance, no timestamp) are filtered
out when the list is built, and a read that cannot answer (a git error, a
timeout, an exception in one kind) hides that item rather than breaking the
render.

Times are epoch seconds and are only ever rendered as Discord timestamp markup
(``<t:EPOCH:R>``), which the client keeps current, so an unchanged state gives
an unchanged render and the refresher's skip actually skips.

Design note: ``docs/design/needs-you.md``. Harness: ``scripts/test_waiting.py``.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable

from bot.claude.types import InstanceOrigin, InstanceStatus
from bot.platform.redact import redact_secrets
from bot.procutil import run_capture
from bot.textutil import parse_iso_utc

log = logging.getLogger(__name__)

DAY = 86400.0
# Questions and failures nobody touched in three days are not coming back.
QUESTION_MAX_AGE = 3 * DAY
FAILURE_MAX_AGE = 3 * DAY
# A build can sit for a week before it is abandoned work.
BUILD_MAX_AGE = 7 * DAY
CHAIN_MAX_AGE = 7 * DAY
UPDATE_MAX_AGE = 7 * DAY
SECRET_MAX_AGE = 3 * DAY
YOUR_TURN_MAX_AGE = 48 * 3600.0

# The Ark shows this many item lines, then "and N more" by kind.
MAX_ARK_LINES = 15
MAX_ROOM_LINES = 10
TITLE_CHARS = 48
DETAIL_CHARS = 90

# "Your turn" lists every thread the bot answered last in the past 48h. The
# live check before shipping (2026-11-15) estimated 22 such threads across 9
# repos, mostly finished research the owner had moved on from: more than the
# whole 15-line budget. So it renders as one count line
# (docs/design/needs-you.md).
YOUR_TURN_AS_COUNT = True


@dataclass(frozen=True)
class Kind:
    name: str
    icon: str
    header: str
    short: str  # for the "and N more" line


# Most urgent first. The order is the priority: one item per thread, and the
# highest kind on a thread wins.
KINDS: tuple[Kind, ...] = (
    Kind("secret_request", "\U0001f511", "Key requests", "key"),
    Kind("update_ready", "⬆️", "Bot update", "update"),
    Kind("peer_run_once", "\U0001f5a5️", "Other machine asks", "machine"),
    Kind("deploy", "\U0001f680", "Deploys", "deploy"),
    Kind("account", "\U0001f464", "Accounts", "account"),
    Kind("choice_pick", "\U0001f446", "Waiting on your pick", "pick"),
    Kind("question", "❓", "Questions", "question"),
    Kind("chain_paused", "⏸️", "Paused chains", "paused"),
    Kind("resume_parent", "↩️", "Resume parent", "resume"),
    Kind("merge_failed", "⚠️", "Merge failed", "merge failed"),
    Kind("merge_held", "✋", "Held merges", "held"),
    Kind("failed_run", "❌", "Failed", "failed"),
    Kind("manual_recovery", "\U0001f6e0️", "Needs manual recovery", "recovery"),
    Kind("ready_to_merge", "✅", "Ready to merge", "ready"),
    Kind("decided_away", "\U0001f319", "Decided while you were away", "decided"),
    Kind("your_turn", "\U0001f4ac", "Your turn", "your turn"),
)
KIND_BY_NAME = {k.name: k for k in KINDS}
_RANK = {k.name: i for i, k in enumerate(KINDS)}

# Runs that are not a turn of their own: they do not shadow the thread's last
# real turn, and their failures are never shown.
_NOT_A_TURN = frozenset({
    InstanceOrigin.JUDGE, InstanceOrigin.PLAN_CHECK, InstanceOrigin.TLDR,
    InstanceOrigin.PROMPT_REVIEW, InstanceOrigin.PEER_ANSWER,
    InstanceOrigin.PEER_DELIVERY, InstanceOrigin.STEPBACK,
})
# A failed plan-check decision pauses its chain, which is the item that shows.
_FAILURE_NOT_SHOWN = _NOT_A_TURN | {InstanceOrigin.PLAN_CHECK_APPLY}

_CHAIN_REASONS = {
    "needs_input": "needs your answer",
    "failed": "a step failed",
    "phantom_detected": "release check flagged claims",
    "budget_exhausted": "cost budget reached",
    "merge_failed": "merge conflict",
    "merge_stopped": "stopped before deploy",
    "review_did_not_converge": "plan review did not converge",
}


@dataclass(frozen=True)
class WaitingItem:
    kind: str
    key: str                     # stable: signatures and dedup
    repo: str | None = None
    thread_id: str | None = None  # the thread (or channel) the item lives in
    title: str = ""
    detail: str = ""
    since: float | None = None   # epoch seconds, shown as <t:..:R>
    hidden: bool = False         # its repo is hidden (dormant)


# --- Change notices ---------------------------------------------------------

_listener: Callable[[], None] | None = None


def set_listener(fn: Callable[[], None] | None) -> None:
    """The platform registers its redraw request here (the Ark refresher)."""
    global _listener
    _listener = fn


def notify_changed() -> None:
    """Something an item is read from changed off a turn. Never raises."""
    fn = _listener
    if fn is None:
        return
    try:
        fn()
    except Exception:
        log.debug("Waiting-list change notice failed", exc_info=True)


# --- Git reads, cached and bounded -----------------------------------------


class GitReader:
    """The three git questions the list asks, each cached for ``ttl`` seconds.

    A pass every 15 seconds must not re-run git for every item. A timeout or
    any error answers None, and None hides the item that asked.
    """

    def __init__(self, *, ttl: float = 60.0, timeout: float = 5.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl
        self._timeout = timeout
        self._clock = clock
        self._cache: dict[tuple, tuple[float, object]] = {}
        # One reader is shared by the refresher's worker thread and any
        # caller on the loop; the git call itself runs outside the lock.
        self._lock = threading.Lock()

    def _rc(self, repo_path: str, *args: str) -> tuple[int, str] | None:
        key = (repo_path, args)
        now = self._clock()
        with self._lock:
            hit = self._cache.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]  # type: ignore[return-value]
        value: tuple[int, str] | None
        try:
            if not repo_path or not Path(repo_path).is_dir():
                value = None
            else:
                r = run_capture(["git", "-C", repo_path, *args], timeout=self._timeout)
                value = (r.returncode, r.stdout or "")
        except Exception:
            value = None
        with self._lock:
            self._cache[key] = (now + self._ttl, value)
            if len(self._cache) > 2048:
                for k in [k for k, (exp, _) in self._cache.items() if exp <= now]:
                    self._cache.pop(k, None)
        return value

    def branch_exists(self, repo_path: str, branch: str) -> bool | None:
        r = self._rc(repo_path, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
        if r is None:
            return None
        return True if r[0] == 0 else (False if r[0] == 1 else None)

    def is_ancestor(self, repo_path: str, commit: str, of: str) -> bool | None:
        r = self._rc(repo_path, "merge-base", "--is-ancestor", commit, of)
        if r is None:
            return None
        return True if r[0] == 0 else (False if r[0] == 1 else None)

    def ahead(self, repo_path: str, base: str, branch: str) -> int | None:
        r = self._rc(repo_path, "rev-list", "--count", f"{base}..{branch}")
        if r is None or r[0] != 0:
            return None
        try:
            return int(r[1].strip())
        except ValueError:
            return None


_GIT = GitReader()


# --- Threads ---------------------------------------------------------------


@dataclass(frozen=True)
class ThreadRef:
    thread_id: str
    repo: str | None
    session_id: str | None
    topic: str
    parent_thread_id: str | None
    last_turn_at: float | None
    last_human_at: float | None


@dataclass
class ThreadIndex:
    threads: dict[str, ThreadRef] = field(default_factory=dict)
    by_session: dict[str, ThreadRef] = field(default_factory=dict)
    controls: dict[str, str] = field(default_factory=dict)  # repo -> Control Room


def _get(obj, name: str, default=None):  # noqa: ANN001, ANN202
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _num(v) -> float | None:  # noqa: ANN001
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def thread_index(forum_projects) -> ThreadIndex:  # noqa: ANN001
    """A plain snapshot of every session thread, safe to read off the loop.

    Takes the live ``ForumProject`` objects or their persisted dicts. Build it
    on the event loop: the forum map is mutated there.
    """
    idx = ThreadIndex()
    for name, proj in list((forum_projects or {}).items()):
        repo = _get(proj, "repo_name") or name
        repo = None if repo == "_default" else repo
        specials = {str(x) for x in (
            _get(proj, "control_thread_id"), _get(proj, "archive_thread_id"),
            _get(proj, "monitor_thread_id"),
        ) if x}
        control = _get(proj, "control_thread_id")
        if repo and control:
            idx.controls[repo] = str(control)
        for tid, info in list((_get(proj, "threads") or {}).items()):
            tid = str(tid)
            if tid in specials:
                continue
            ref = ThreadRef(
                thread_id=tid, repo=repo,
                session_id=_get(info, "session_id") or None,
                topic=str(_get(info, "topic") or ""),
                parent_thread_id=_get(info, "parent_thread_id") or None,
                last_turn_at=_num(_get(info, "last_turn_at")),
                last_human_at=_num(_get(info, "last_human_at")),
            )
            idx.threads[tid] = ref
            if ref.session_id:
                idx.by_session[ref.session_id] = ref
    return idx


def index_from_store(store) -> ThreadIndex:  # noqa: ANN001
    """The thread index from the persisted forum map (engine callers)."""
    try:
        raw = (store.get_platform_state("discord") or {}).get("forum_projects") or {}
    except Exception:
        raw = {}
    return thread_index(raw)


# --- Helpers ---------------------------------------------------------------


def _epoch(value) -> float | None:  # noqa: ANN001
    """An ISO string or a number as epoch seconds; None when it is neither."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    dt = parse_iso_utc(str(value))
    return dt.timestamp() if dt is not None else None


def _clip(text: str, n: int) -> str:
    s = " ".join(redact_secrets(text or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _title(ref: ThreadRef | None, inst=None) -> str:  # noqa: ANN001
    if ref is not None and ref.topic.strip():
        return _clip(ref.topic, TITLE_CHARS)
    if inst is not None and getattr(inst, "prompt", None):
        return _clip(inst.prompt, TITLE_CHARS)
    return "session"


def _snapshot(fn: Callable[[], list]) -> list:
    """A store read that iterates a dict another thread may be growing."""
    for _ in range(3):
        try:
            return list(fn())
        except RuntimeError:
            continue
    return list(fn())


@dataclass
class _Ctx:
    store: object
    idx: ThreadIndex
    now: float
    git: GitReader
    insts: list
    latest: dict           # session_id -> newest instance that is a turn
    latest_any: dict       # session_id -> newest instance of any origin
    running: set
    repos: dict

    def dormant(self, repo: str | None) -> bool:
        if not repo:
            return False
        try:
            return bool(self.store.is_repo_dormant(repo))
        except Exception:
            return False

    def item(self, kind: str, key: str, *, repo=None, thread_id=None,  # noqa: ANN001
             title: str = "", detail: str = "", since=None) -> WaitingItem:
        return WaitingItem(kind=kind, key=key, repo=repo, thread_id=thread_id,
                           title=title, detail=detail, since=since,
                           hidden=self.dormant(repo))


def _finished(inst) -> float | None:  # noqa: ANN001
    return _epoch(getattr(inst, "finished_at", None) or getattr(inst, "created_at", None))


# --- Kinds -----------------------------------------------------------------


def _secret_requests(c: _Ctx) -> Iterable[WaitingItem]:
    from bot import vault
    for token, req in vault.pending_requests().items():
        since = _num(req.get("created_at"))
        if since is None or c.now - since > SECRET_MAX_AGE:
            continue  # undatable or stale: hidden, never deleted
        cid = req.get("channel_id")
        ref = c.idx.threads.get(str(cid)) if cid else None
        yield c.item("secret_request", f"secret:{token}", repo=req.get("repo"),
                     thread_id=str(cid) if cid else None, title=_title(ref),
                     detail=f"wants key {_clip(str(req.get('name') or ''), 40)}",
                     since=since)


def _update_ready(c: _Ctx) -> Iterable[WaitingItem]:
    from bot import auto_update, config
    sha = auto_update.carded_sha(c.store)
    at = _num(auto_update.carded_at(c.store))
    if not sha or at is None or c.now - at > UPDATE_MAX_AGE:
        return
    if c.git.is_ancestor(str(config._PROJECT_ROOT), sha, "HEAD") is not False:
        return  # applied already, or git could not say
    yield c.item("update_ready", f"update:{sha}", title="Bot update",
                 detail=f"`{sha[:7]}` is carded, tap Apply update", since=at)


def _peer_run_once(c: _Ctx) -> Iterable[WaitingItem]:
    from bot.engine import peer_questions as pq
    for rec in c.store.peer_received_all():
        if rec.get("status") != pq.CARDED:
            continue
        created = _num(rec.get("created_at"))
        if created is None or c.now - created > pq.MAX_AGE_SECS:
            continue
        repo = rec.get("repo")
        # The repo is named by the other machine; it only counts as hidden
        # when this machine has a repo of that name parked.
        yield WaitingItem(
            kind="peer_run_once", key=f"peer:{rec.get('id')}", repo=repo,
            thread_id=rec.get("card_thread_id") or None,
            title="Run once?", detail=f"the other machine asks about {repo}",
            since=created, hidden=c.dormant(repo) if repo in c.repos else False,
        )


def _deploys(c: _Ctx) -> Iterable[WaitingItem]:
    for repo in sorted(c.repos):
        cfg = c.store.get_deploy_config(repo)
        if not cfg:
            continue
        ds = c.store.get_deploy_state(repo)
        if ds is None or not ds.needs_reboot or not ds.pending_changes:
            continue
        # pending_changes is changelog lines or the last five commits, not a
        # count; the merges since the last deploy are.
        n = len(ds.pending_sessions or [])
        count = f" ({n} merge{'s' if n != 1 else ''})" if n else ""
        detail = (f"approve deploy{count}" if not cfg.get("approved")
                  else f"deploy pending{count}")
        if ds.last_deploy_error:
            detail += ", last deploy failed"
        # The Deploy button lives in the repo's Control Room.
        yield c.item("deploy", f"deploy:{repo}:{ds.current_ref}", repo=repo,
                     thread_id=c.idx.controls.get(repo), title="Deploy", detail=detail)


def _accounts(c: _Ctx) -> Iterable[WaitingItem]:
    from bot import config
    from bot.claude.auth_health import account_label
    rotation = set(config.CLAUDE_ACCOUNTS)
    for acct, rec in (c.store.get_account_alerts() or {}).items():
        if rec.get("resolved") or (rotation and acct not in rotation):
            continue
        reason = str(rec.get("reason") or "signed out").replace("_", " ")
        yield c.item("account", f"account:{acct}", title=account_label(acct),
                     detail=f"{reason}, tap Claude Login", since=_epoch(rec.get("since")))
    for acct, expiry in (c.store.get_login_expiry_warnings() or {}).items():
        if rotation and acct not in rotation:
            continue
        yield c.item("account", f"login:{acct}:{expiry}", title=account_label(acct),
                     detail="login ends", since=_epoch(expiry))


def _choice_picks(c: _Ctx) -> Iterable[WaitingItem]:
    from bot.engine.directives import choices
    for cid, offer in choices.open_offers(c.store, now=c.now).items():
        ref = c.idx.threads.get(str(cid))
        repo = offer.get("repo") or (ref.repo if ref else None)
        labels = " / ".join(str(x) for x in (offer.get("labels") or []))
        yield c.item("choice_pick", f"choice:{cid}:{offer.get('msg_id')}", repo=repo,
                     thread_id=str(cid), title=_title(ref),
                     detail=_clip(labels, DETAIL_CHARS), since=_num(offer.get("offered_at")))


def _session_items(c: _Ctx) -> Iterable[WaitingItem]:
    """Question, failed run and manual recovery: from the thread's last turn."""
    for sid, inst in c.latest.items():
        ref = c.idx.by_session.get(sid)
        if ref is None or inst.status in (InstanceStatus.RUNNING, InstanceStatus.QUEUED):
            continue
        at = _finished(inst)
        if at is None:
            continue
        age = c.now - at
        repo = inst.repo_name or ref.repo
        child = bool(ref.parent_thread_id)  # its parent's wave answers for it
        if getattr(inst, "manual_recovery_needed", False):
            if age <= FAILURE_MAX_AGE:
                yield c.item("manual_recovery", f"recovery:{inst.id}", repo=repo,
                             thread_id=ref.thread_id, title=_title(ref, inst),
                             detail=_clip(inst.manual_recovery_reason or "", DETAIL_CHARS),
                             since=at)
            continue
        if child:
            continue
        if inst.needs_input and age <= QUESTION_MAX_AGE:
            yield c.item("question", f"question:{inst.id}", repo=repo,
                         thread_id=ref.thread_id, title=_title(ref, inst), since=at)
        elif (inst.status == InstanceStatus.FAILED and age <= FAILURE_MAX_AGE
              and inst.origin not in _FAILURE_NOT_SHOWN):
            yield c.item("failed_run", f"failed:{inst.id}", repo=repo,
                         thread_id=ref.thread_id, title=_title(ref, inst),
                         detail=_clip((inst.error or "").splitlines()[0] if inst.error else "",
                                      DETAIL_CHARS),
                         since=at)
        elif (getattr(inst, "decided_away", False) and age <= YOUR_TURN_MAX_AGE
              and inst.status == InstanceStatus.COMPLETED
              and (ref.last_human_at or 0) < at):
            # Quiet hours calls the owner has not seen yet. Closes when anyone
            # writes in the thread, like "Your turn". docs/design/quiet-hours.md
            yield c.item("decided_away", f"decided:{inst.id}", repo=repo,
                         thread_id=ref.thread_id, title=_title(ref, inst),
                         detail="calls made while you were away", since=at)


def _paused_chains(c: _Ctx) -> Iterable[WaitingItem]:
    for sid, meta in (c.store.list_autopilot_chain_meta() or {}).items():
        if (meta or {}).get("status") != "paused":
            continue
        at = _epoch(meta.get("updated_at"))
        ref = c.idx.by_session.get(sid)
        if at is None or ref is None or c.now - at > CHAIN_MAX_AGE:
            continue
        inst = c.latest_any.get(sid)
        if inst is None:
            continue  # its instances are pruned: nothing to resume from
        reason = _CHAIN_REASONS.get(meta.get("reason") or "", "")
        yield c.item("chain_paused", f"chain:{sid}", repo=inst.repo_name or ref.repo,
                     thread_id=ref.thread_id, title=_title(ref, inst),
                     detail=f"paused: {reason}" if reason else "paused", since=at)


def _resume_parents(c: _Ctx) -> Iterable[WaitingItem]:
    from bot.discord.orchestrator import _PAYLOAD_KEY, _PAYLOAD_RETENTION_DAYS
    payloads = (c.store.get_platform_state("discord") or {}).get(_PAYLOAD_KEY) or {}
    for token, entry in list(payloads.items()):
        if not isinstance(entry, dict) or not entry.get("thread_id"):
            continue
        at = _epoch(entry.get("at"))
        if at is None or c.now - at > _PAYLOAD_RETENTION_DAYS * DAY:
            continue
        ref = c.idx.threads.get(str(entry["thread_id"]))
        if ref is None or (ref.last_human_at or 0) > at:
            continue  # the owner already spoke in the parent since
        yield c.item("resume_parent", f"resume:{token}", repo=ref.repo,
                     thread_id=ref.thread_id, title=_title(ref),
                     detail="a child is waiting, tap Resume parent", since=at)


def _branch_live(c: _Ctx, repo_path, branch, target) -> bool:  # noqa: ANN001
    """True only when git says the branch exists and is not merged into target."""
    if not (repo_path and branch and target):
        return False
    if c.git.branch_exists(repo_path, branch) is not True:
        return False
    return c.git.is_ancestor(repo_path, branch, target) is False


def _failed_merges(c: _Ctx) -> Iterable[WaitingItem]:
    for iid, meta in (c.store.list_pending_merges() or {}).items():
        at = _epoch(meta.get("created_at"))
        if at is None or c.now - at > FAILURE_MAX_AGE:
            continue
        inst = c.store.get_instance(iid)
        repo = meta.get("repo_name") or (inst.repo_name if inst else None)
        branch = meta.get("branch") or (inst.branch if inst else None)
        target = meta.get("target") or (inst.original_branch if inst else None)
        path = c.repos.get(repo) if repo else None
        if not _branch_live(c, path, branch, target):
            continue
        cid = meta.get("channel_id")
        ref = c.idx.threads.get(str(cid)) if cid else None
        yield c.item("merge_failed", f"merge:{iid}", repo=repo,
                     thread_id=str(cid) if cid else None, title=_title(ref, inst),
                     detail=f"`{branch}` did not merge", since=at)


def _held_merges(c: _Ctx) -> Iterable[WaitingItem]:
    for iid, meta in (c.store.list_scheduled_merges() or {}).items():
        if not meta.get("held"):
            continue
        inst = c.store.get_instance(iid)
        repo = meta.get("repo_name") or (inst.repo_name if inst else None)
        branch = meta.get("branch") or (inst.branch if inst else None)
        target = (inst.original_branch if inst else None) or "HEAD"
        if not _branch_live(c, c.repos.get(repo) if repo else None, branch, target):
            continue
        cid = meta.get("channel_id")
        ref = c.idx.threads.get(str(cid)) if cid else None
        yield c.item("merge_held", f"held:{iid}", repo=repo,
                     thread_id=str(cid) if cid else None, title=_title(ref, inst),
                     detail=f"auto-merge of `{branch}` held", since=_epoch(meta.get("created_at")))


def _ready_to_merge(c: _Ctx) -> Iterable[WaitingItem]:
    from bot.engine.chain_git import ship_candidates
    countdown = {m.get("session_id") for m in (c.store.list_scheduled_merges() or {}).values()}
    threads = [(r.thread_id, r.session_id, r.topic) for r in c.idx.threads.values()
               if r.session_id and r.session_id not in countdown]
    found = ship_candidates(
        c.store, threads, is_active=lambda sid: sid in c.running,
        ahead=lambda path, base, br: c.git.ahead(path, base, br) or 0,
        instances=c.insts,
    )
    for tid, _sid, inst, _topic in found:
        at = _finished(inst)
        if at is None or c.now - at > BUILD_MAX_AGE:
            continue
        ref = c.idx.threads.get(tid)
        yield c.item("ready_to_merge", f"ready:{inst.id}", repo=inst.repo_name,
                     thread_id=tid, title=_title(ref, inst),
                     detail=f"`{inst.branch}` is built, not merged", since=at)


def _your_turn(c: _Ctx) -> Iterable[WaitingItem]:
    for ref in c.idx.threads.values():
        if ref.parent_thread_id or ref.last_turn_at is None:
            continue
        if c.now - ref.last_turn_at > YOUR_TURN_MAX_AGE:
            continue
        if (ref.last_human_at or 0) >= ref.last_turn_at:
            continue
        yield c.item("your_turn", f"turn:{ref.thread_id}", repo=ref.repo,
                     thread_id=ref.thread_id, title=_title(ref), since=ref.last_turn_at)


_READERS: tuple[tuple[str, Callable[[_Ctx], Iterable[WaitingItem]]], ...] = (
    ("secret_request", _secret_requests),
    ("update_ready", _update_ready),
    ("peer_run_once", _peer_run_once),
    ("deploy", _deploys),
    ("account", _accounts),
    ("choice_pick", _choice_picks),
    ("question/failed/recovery/decided", _session_items),
    ("chain_paused", _paused_chains),
    ("resume_parent", _resume_parents),
    ("merge_failed", _failed_merges),
    ("merge_held", _held_merges),
    ("ready_to_merge", _ready_to_merge),
    ("your_turn", _your_turn),
)


def _context(store, idx: ThreadIndex, now: float, git: GitReader) -> _Ctx:  # noqa: ANN001
    try:
        insts = _snapshot(lambda: store.list_instances(all_=True))
    except Exception:
        insts = []
    latest: dict = {}
    latest_any: dict = {}
    running: set = set()
    for inst in insts:  # newest first
        sid = getattr(inst, "session_id", None)
        if not sid:
            continue
        if inst.status in (InstanceStatus.RUNNING, InstanceStatus.QUEUED):
            running.add(sid)
        latest_any.setdefault(sid, inst)
        if inst.origin not in _NOT_A_TURN:
            latest.setdefault(sid, inst)
    try:
        repos = dict(store.list_repos())
    except Exception:
        repos = {}
    return _Ctx(store=store, idx=idx, now=now, git=git, insts=insts,
                latest=latest, latest_any=latest_any, running=running, repos=repos)


def collect(store, threads=None, *, repo: str | None = None,  # noqa: ANN001
            now: float | None = None, git: GitReader | None = None,
            stats: dict | None = None) -> list[WaitingItem]:
    """Every item waiting on the owner, most urgent first.

    ``threads`` is a ``ThreadIndex`` (build it on the event loop with
    ``thread_index``), the live forum map, or None for the persisted one.
    Synchronous and may run git: call it through ``asyncio.to_thread``.
    ``repo`` keeps one repo's items. ``stats``, when given, gets the number of
    items each reader produced before dedup, for the harness.
    """
    if isinstance(threads, ThreadIndex):
        idx = threads
    elif threads is None:
        idx = index_from_store(store)
    else:
        idx = thread_index(threads)
    c = _context(store, idx, time.time() if now is None else now, git or _GIT)

    found: list[WaitingItem] = []
    for name, reader in _READERS:
        try:
            got = list(reader(c))
        except Exception:
            log.debug("Waiting list: %s could not be read, hidden this pass", name,
                      exc_info=True)
            got = []
        if stats is not None:
            stats[name] = len(got)
        found.extend(got)

    # One item per thread: the most urgent kind wins. A running session has
    # nothing waiting on the owner.
    found.sort(key=lambda i: (_RANK.get(i.kind, 99), -(i.since or 0)))
    out: list[WaitingItem] = []
    seen_threads: set[str] = set()
    seen_keys: set[str] = set()
    for it in found:
        if it.key in seen_keys:
            continue
        if it.thread_id:
            ref = c.idx.threads.get(it.thread_id)
            if ref is not None and ref.session_id in c.running:
                continue
            if it.thread_id in seen_threads:
                continue
            seen_threads.add(it.thread_id)
        seen_keys.add(it.key)
        if repo is not None and it.repo != repo:
            continue
        out.append(it)
    return out


def by_repo(items: Iterable[WaitingItem]) -> dict[str, list[WaitingItem]]:
    out: dict[str, list[WaitingItem]] = {}
    for it in items:
        if it.repo:
            out.setdefault(it.repo, []).append(it)
    return out


def signature(items: Iterable[WaitingItem]) -> tuple:
    """What a render of these items depends on, for the refresher's skip."""
    return tuple((i.key, i.since, i.detail, i.hidden) for i in items)


# --- Rendering (Discord markdown) ------------------------------------------


def item_line(it: WaitingItem, *, with_repo: bool = True) -> str:
    kind = KIND_BY_NAME.get(it.kind)
    parts = [kind.icon if kind else "•"]
    if with_repo and it.repo:
        parts.append(f"**{it.repo}**" + (" (hidden)" if it.hidden else ""))
    parts.append(it.title or (kind.header if kind else it.kind))
    line = " ".join(parts)
    if it.detail:
        line += f" · {it.detail}"
    if it.thread_id:
        line += f" <#{it.thread_id}>"
    if it.since:
        line += f" <t:{int(it.since)}:R>"
    return line


def _split_turns(items: list[WaitingItem]) -> tuple[list[WaitingItem], list[WaitingItem]]:
    """(items listed one per line, your-turn items folded into a count line)."""
    if not YOUR_TURN_AS_COUNT:
        return list(items), []
    return ([i for i in items if i.kind != "your_turn"],
            [i for i in items if i.kind == "your_turn"])


def _more_line(rest: list[WaitingItem]) -> str:
    counts: dict[str, int] = {}
    for it in rest:
        counts[it.kind] = counts.get(it.kind, 0) + 1
    parts = [f"{n} {KIND_BY_NAME[k].short if k in KIND_BY_NAME else k}"
             for k, n in sorted(counts.items(), key=lambda kv: _RANK.get(kv[0], 99))]
    return f"and {len(rest)} more: " + ", ".join(parts)


def _count_line(items: list[WaitingItem]) -> str:
    n = len(items)
    hidden = sum(1 for i in items if i.hidden)
    extra = f", {hidden} in hidden repos" if hidden else ""
    return (f"{KIND_BY_NAME['your_turn'].icon} {n} thread{'s' if n != 1 else ''} "
            f"answered in the last 48h with no reply yet{extra}")


def render_ark(items: list[WaitingItem], *, max_lines: int = MAX_ARK_LINES,
               limit: int = 4000) -> str:
    """The Ark's "Needs you" text: grouped by kind, capped, inside ``limit``."""
    if not items:
        return "Nothing is waiting on you."
    listed, turns = _split_turns(items)

    def build(title_chars: int | None, max_lines: int) -> str:
        lines: list[str] = []
        shown = 0
        current = None
        rest: list[WaitingItem] = []
        for it in listed:
            if shown >= max_lines:
                rest.append(it)
                continue
            if it.kind != current:
                current = it.kind
                kind = KIND_BY_NAME.get(it.kind)
                n = sum(1 for x in listed if x.kind == it.kind)
                lines.append(f"**{kind.header if kind else it.kind} ({n})**")
            if title_chars is not None:
                it = replace(it, title=_clip(it.title, title_chars),
                             detail=_clip(it.detail, title_chars))
            lines.append(item_line(it))
            shown += 1
        if rest:
            lines.append(_more_line(rest))
        if turns:
            lines.append(_count_line(turns))
        return "\n".join(lines)

    # Titles shrink before any item is dropped; then items fold into the
    # "and N more" line, which keeps the count honest.
    for chars in (None, 32, 20, 12):
        text = build(chars, max_lines)
        if len(text) <= limit:
            return text
    for n in range(min(max_lines, len(listed)) - 1, -1, -1):
        text = build(12, n)
        if len(text) <= limit:
            return text
    return text[: limit - 1] + "…"


def render_room(items: list[WaitingItem], *, max_lines: int = MAX_ROOM_LINES,
                limit: int = 1024) -> str:
    """One repo's items for its Control Room field, inside Discord's 1024."""
    listed, turns = _split_turns(items)
    # A deploy links to the Control Room it is already shown in.
    listed = [replace(i, thread_id=None) if i.kind == "deploy" else i for i in listed]

    def build(n: int) -> str:
        lines = [item_line(i, with_repo=False) for i in listed[:n]]
        if len(listed) > n:
            lines.append(_more_line(listed[n:]))
        if turns:
            lines.append(_count_line(turns))
        return "\n".join(lines)

    # Items fold into the "and N more" line until it fits, so the count
    # always matches what was left out.
    for n in range(min(max_lines, len(listed)), -1, -1):
        text = build(n)
        if len(text) <= limit:
            return text
    return text[: limit - 1] + "…"

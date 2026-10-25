#!/usr/bin/env python3
"""Characterization test: every persisted StateStore field round-trips.

`bot/store/state.py` writes `state.json` with a hand-written `_serialize` and
reads it back with a hand-written `_load`: forty top-level keys, each loaded
into its own attribute, plus the per-record `to_dict` / `from_dict` pairs for
`Instance`, `Schedule`, `Watch`, `ChainPhaseState`, `DeployState` and
`AutoFixState`. A key that is written but never read back (or read but never
written) is the quiet failure: nothing crashes, the bot reboots, and one
category of state is simply gone. This harness pins the current behaviour so a
refactor that splits StateStore into domain modules behind a facade, or swaps
the hand-written pair for a field table, cannot drop a field unnoticed.

It does not care how the store is organised internally. It drives the public
API, reads the saved JSON, and reloads through a fresh store, so it keeps
working across that refactor.

What it pins:

  1. The explicit set of top-level keys in the saved file. Adding or dropping
     a persisted key fails here, and someone has to update `EXPECTED_KEYS`
     deliberately.
  2. A fixture state file with EVERY key and EVERY Instance field set to a
     distinctive non-default value: load it, save it, and the saved JSON must
     equal the fixture key by key, and a second generation must be
     byte-identical.
  3. The same coverage reached through the public setters (add_repo,
     add_watch, set_pending_merge, ...) instead of a hand-built file, so the
     write path is exercised too, not only the load path.
  4. Old-shaped files (missing most keys, legacy spellings, malformed
     records, no file, corrupt file) come up with defaults instead of
     crashing.

`platform_state.discord.orch_resume_payloads` is persisted verbatim here;
its age limit (fix S2) is enforced by the orchestrator's sweep, not by the
store, and is covered in test_orchestrator_join.py.

`instance.bash_commands` is no longer persisted (fix S1): nothing had read
it since 03531d9 and it was 27% of the live file. A legacy file that still
carries it loads, and the next save drops it.

The store is pointed at a temp dir before construction; the real
data/state.json is never opened.

Run: python scripts/test_state_roundtrip.py
Exit 0 = all pass, exit 1 = failures.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import dataclasses
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Keep importing bot.config from seeding a path-translation group off whatever
# home this runs under. An empty map is pass-through, which is what a
# round-trip test wants: no path may change on the way through.
os.environ.setdefault("BOT_PATHS_DISABLED", "1")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from bot.claude.types import (  # noqa: E402
    ChainPhaseState,
    Instance,
    InstanceOrigin,
    InstanceStatus,
    InstanceType,
    Phase,
    Schedule,
    Watch,
)
from bot.store.records import AutoFixState, DeployState  # noqa: E402
from bot.store.state import StateStore  # noqa: E402

_failures: list[str] = []
_checks = 0


def _check(name: str, cond: bool, detail: str = "") -> None:
    global _checks
    _checks += 1
    if cond:
        print(f"PASS  {name}")
    else:
        _failures.append(name)
        print(f"FAIL  {name}" + (f"  ({detail})" if detail else ""))


def _eq(name: str, got, want) -> None:
    ok = got == want
    detail = ""
    if not ok:
        detail = f"got={_short(got)} want={_short(want)}"
    _check(name, ok, detail)


def _short(value, limit: int = 160) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "..."


# The one place the persisted top-level key set is written down. If this fails
# because a key was added or removed on purpose, update the list below.
EXPECTED_KEYS = frozenset({
    "instances",
    "repos",
    "repo_descriptions",
    "dormant_repos",
    "active_repo",
    "task_counter",
    "query_counter",
    "schedule_counter",
    "daily_cost",
    "cost_date",
    "total_cost",
    "mode",
    "context",
    "aliases",
    "active_session_id",
    "verbose_level",
    "effort",
    "platform_state",
    "autopilot_chains",
    "autopilot_chain_meta",
    "chain_kwargs",
    "pending_merges",
    "scheduled_merges",
    "chain_plan_overrides",
    "chain_deferred",
    "chain_phases",
    "chain_entry_sha",
    "deploy_state",
    "deploy_configs",
    "auto_fix_state",
    "active_provider",
    "fallback_cost",
    "fallback_cost_date",
    "account_cooldowns",
    "account_cooldown_fps",
    "model_cooldowns",
    "account_alerts",
    "login_expiry_warnings",
    "schedules",
    "watches",
    "watch_counter",
})

# Instance attributes that are deliberately not persisted (see the Instance
# dataclass: each is documented there as ephemeral, one attempt only).
EPHEMERAL_INSTANCE_FIELDS = frozenset({
    "_accounts_tried",
    "_verifier_parse_failed",
    "_context_thrash_retry",
    "_context_bloat_note",
    "_memory_kill_note",
    "_context_overflow_note",
})

tmpdir = tempfile.TemporaryDirectory()
TMP = Path(tmpdir.name)
RESULTS = TMP / "results"
TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")
R = TMP.as_posix()  # synthetic repo/account locations, nothing is ever opened


def _store(state_file: Path) -> StateStore:
    return StateStore(state_file, RESULTS)


def _read(state_file: Path) -> dict:
    return json.loads(state_file.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# A fully populated Instance: every persisted field gets a distinctive,
# non-default value. `_check_fixture_covers_instance` proves it, so a field
# added to Instance later fails loudly instead of riding through untested.
# ---------------------------------------------------------------------------

def full_instance() -> Instance:
    return Instance(
        id="t-501",
        name="fullname",
        instance_type=InstanceType.TASK,
        prompt="build the thing, with unicode: café ✓",
        repo_name="alpha",
        repo_path=f"{R}/repos/alpha",
        status=InstanceStatus.COMPLETED,
        session_id="sess-aaaa-1111",
        mode="build",
        branch="claude-bot/t-501",
        original_branch="master",
        worktree_path=f"{R}/repos/alpha/.worktrees/t-501",
        created_at="2026-10-01T10:00:00+00:00",
        finished_at="2026-10-01T10:30:00+00:00",
        summary="did the thing",
        result_file=f"{R}/results/t-501.md",
        diff_file=f"{R}/results/t-501.diff",
        error="soft error text",
        cost_usd=1.5,
        duration_ms=123456,
        retry_count=2,
        message_ids={"discord": ["111", "222"], "telegram": ["9"]},
        pid=4242,
        schedule_id="sch-007",
        origin=InstanceOrigin.REVIEW_CODE,
        parent_id="t-500",
        origin_platform="telegram",
        user_id="u-77",
        user_name="Quincy",
        tools_used=["Edit", "Write", "Bash"],
        num_turns=17,
        input_tokens=1001,
        output_tokens=2002,
        cache_read_tokens=3003,
        cache_creation_tokens=4004,
        context_tokens=5005,
        context_model="claude-opus-9",
        resumed_session=True,
        warning_pinned=True,
        plan_active=True,
        code_active=True,
        needs_input=True,
        needs_manual_verification=True,
        manual_verify_reason="UI needs an eyeball",
        deferred_revisions=["[Docs] note (Low)", "[Perf] cache (Medium)"],
        jsonl_uuid_by_msg_id={"111": "uuid-1", "222": "uuid-2"},
        expand_msg_ids=["333", "444"],
        result_collapsed=True,
        is_owner_session=False,
        bash_policy="allowlist",
        bash_policy_baseline="none",
        effort="max",
        model="sonnet",
        cooldown_retry_at="2026-10-02T01:00:00+00:00",
        cooldown_retries=3,
        cooldown_channel_id="chan-cool",
        api_fallback=True,
        session_account=f"{R}/accounts/work",
        chained_from="t-499",
        manual_recovery_needed=True,
        manual_recovery_reason="worktree drifted",
        master_baseline_head="deadbeefcafe",
        path_poisoning=["src/a.py", "src/b.py"],
        finishup_nudges=1,
        spawn_depth=1,
        spawn_dispatched_thread_ids=["th-1", "th-2"],
        spawn_wave_released=True,
        spawn_wave_sealed=True,
        spawn_blocked_resumes=2,
        spawn_wave_unresolved_thread_ids=["th-2"],
        spawn_late_reported_thread_ids=["th-2"],
    )


def sparse_instance() -> Instance:
    """A second record with almost everything defaulted, different enums."""
    return Instance(
        id="q-502",
        name=None,
        instance_type=InstanceType.QUERY,
        prompt="what is up",
        repo_name="",
        repo_path="",
        status=InstanceStatus.QUEUED,
    )


def _persisted_fields() -> list[str]:
    return [f.name for f in dataclasses.fields(Instance)
            if not f.name.startswith("_")]


def _default_instance() -> Instance:
    # QUERY, not TASK: the dataclass has no default for instance_type, and the
    # reference record is a TASK, so this keeps "differs from default" honest.
    return Instance(id="x", name=None, instance_type=InstanceType.QUERY,
                    prompt="", repo_name="", repo_path="",
                    status=InstanceStatus.QUEUED)


def _check_instance_schema() -> None:
    print("\ninstance schema")
    fields = _persisted_fields()
    keys = set(full_instance().to_dict())
    _eq("every non-ephemeral Instance field is written by to_dict",
        sorted(set(fields) - keys), [])
    _eq("to_dict writes nothing that is not an Instance field",
        sorted(keys - set(fields)), [])
    _eq("the ephemeral Instance fields are exactly the documented set",
        {f.name for f in dataclasses.fields(Instance)
         if f.name.startswith("_")},
        set(EPHEMERAL_INSTANCE_FIELDS))
    for name in EPHEMERAL_INSTANCE_FIELDS:
        _check(f"ephemeral {name} is not persisted", name not in keys)

    full, default = full_instance(), _default_instance()
    untouched = [n for n in fields
                 if n != "id" and getattr(full, n) == getattr(default, n)]
    _eq("fixture instance sets every persisted field to a non-default value",
        untouched, [])

    rebuilt = Instance.from_dict(json.loads(json.dumps(full.to_dict())))
    lost = [n for n in fields if getattr(rebuilt, n) != getattr(full, n)]
    _eq("Instance.from_dict(to_dict()) restores every persisted field",
        lost, [])


# ---------------------------------------------------------------------------
# Fixture: a state.json with every top-level key populated.
# ---------------------------------------------------------------------------

def _chain_phase_state() -> ChainPhaseState:
    return ChainPhaseState(
        phases=[
            Phase(id="p1", title="Schema", gate="mechanical", reason="easy"),
            Phase(id="p2", title="Rollout", gate="risk", reason="prod"),
        ],
        cursor=1,
        paused_at="post",
        pre_phase_head="abc1230",
        worktree_path=f"{R}/repos/alpha/.worktrees/t-501",
        first_build_id="t-501",
    )


def _deploy_state() -> DeployState:
    return DeployState(
        boot_version="1.2.3",
        boot_ref="bootref",
        current_version="1.2.4",
        current_ref="curref",
        self_managed=True,
        pending_sessions=["sess-1", "sess-2"],
        pending_changes=["change one", "change two"],
        last_deploy_error="deploy blew up",
        auto_fix_attempt=2,
        auto_fix_thread_id="th-fix",
    )


def fixture_state() -> dict:
    """Every key of EXPECTED_KEYS, canonical (what _serialize would emit)."""
    sched_full = Schedule(
        id="sch-010", prompt="nightly review", repo_name="alpha",
        repo_path=f"{R}/repos/alpha", mode="build", interval_secs=3600,
        run_at="2026-11-01T00:00:00+00:00", is_recurring=True,
        last_run_at="2026-10-10T00:00:00+00:00",
        next_run_at="2026-10-13T00:00:00+00:00", last_summary="all fine",
        enabled=False, resume_thread=True, channel_id="chan-sched",
        label="nightly-nudge",
    )
    sched_wake = Schedule(
        id="sch-011", prompt="wake me", repo_name="beta",
        repo_path=f"{R}/repos/beta", mode="explore", interval_secs=None,
        run_at=None, is_recurring=False, last_run_at=None,
        next_run_at="2026-10-14T12:00:00+00:00", last_summary=None,
        enabled=True, resume_thread=True, channel_id="chan-wake", label="",
    )
    watch = Watch(
        id="w-009", channel_id="chan-watch", prompt="the job finished",
        label="sculpt fit", pid=31337, pid_start="987654321",
        pid_dead_at_arm=True, log_path=f"{R}/repos/alpha/run.log",
        progress_re=r"(\d+)/(\d+) frames", done_re="ALL DONE",
        armed_at="2026-10-11T08:00:00+00:00",
        timeout_at="2026-10-11T14:00:00+00:00", every_secs=240,
        heartbeat_msg_id="hb-555", last_beat_at="2026-10-11T09:00:00+00:00",
        repo_name="alpha", repo_path=f"{R}/repos/alpha",
    )
    return {
        "instances": [full_instance().to_dict(), sparse_instance().to_dict()],
        "repos": {"alpha": f"{R}/repos/alpha", "beta": f"{R}/repos/beta"},
        "repo_descriptions": {
            "alpha": {"text": "Alpha does things.", "source": "CLAUDE.md",
                      "sig": "sig-abc", "mtime": 1760000000.5,
                      "path": f"{R}/repos/alpha"},
            "beta": {"text": "", "source": "", "sig": "sig-miss",
                     "mtime": 0.0, "path": f"{R}/repos/beta"},
        },
        "dormant_repos": ["alpha"],
        "active_repo": "beta",
        "task_counter": 501,
        "query_counter": 502,
        "schedule_counter": 11,
        "daily_cost": 12.5,
        "cost_date": TODAY,
        "total_cost": 345.25,
        "mode": "build",
        "context": "working on the refactor",
        "aliases": {"ship": "run the ship chain", "rev": "review the diff"},
        "active_session_id": "sess-active-9",
        "verbose_level": 2,
        "effort": "max",
        "platform_state": {
            "discord": {
                "forum_projects": {"alpha": {"forum_channel_id": "f-1",
                                             "threads": {"th-1": {"topic": "x"}}}},
                "deploy_status_msgs": {"alpha": "msg-77"},
                "orch_resume_payloads": {"tok-1": "resume prompt one",
                                         "tok-2": "resume prompt two"},
                "ark_channel_id": "ark-1",
            },
            "telegram": {"offset": 99, "chats": ["c1", "c2"]},
        },
        "autopilot_chains": {"sess-chain-1": ["review_code", "commit", "done"]},
        "autopilot_chain_meta": {"sess-chain-1": {
            "status": "paused", "updated_at": "2026-10-11T07:00:00+00:00"}},
        "chain_kwargs": {"sess-chain-1": {"label": "Build & Ship",
                                          "silent_close": True}},
        "pending_merges": {"t-501": {
            "session_id": "sess-chain-1", "channel_id": "chan-merge",
            "repo_name": "alpha", "message": "Merge failed: conflict",
            "created_at": "2026-10-11T06:00:00+00:00",
            "resolver_instance_id": "t-600", "deferred_text": "keep both",
            "failure_kind": "conflict"}},
        "scheduled_merges": {"t-502": {
            "session_id": "sess-chain-2", "channel_id": "chan-sm",
            "repo_name": "beta", "branch": "claude-bot/t-502",
            "merge_at": "2026-10-12T20:00:00+00:00", "held": True,
            "created_at": "2026-10-12T19:00:00+00:00"}},
        "chain_plan_overrides": {"sess-chain-1": "PLAN: do the three things"},
        "chain_deferred": {"sess-chain-1": ["[Tag] deferred one (Low)",
                                            "[Tag] deferred two (Medium)"]},
        "chain_phases": {"sess-chain-1": _chain_phase_state().to_dict()},
        "chain_entry_sha": {"sess-chain-1": "0123456789abcdef"},
        "deploy_state": {"alpha": _deploy_state().to_dict()},
        "deploy_configs": {"alpha": {"command": "make deploy", "branch": "main",
                                     "nested": {"a": [1, 2, 3]}}},
        "auto_fix_state": {"alpha:build": AutoFixState(
            attempt=3, thread_id="th-auto").to_dict()},
        "active_provider": "api-fallback",
        "fallback_cost": 4.75,
        "fallback_cost_date": TODAY,
        "account_cooldowns": {f"{R}/accounts/main": "2026-10-13T00:00:00+00:00"},
        "account_cooldown_fps": {f"{R}/accounts/main": "fp-main-1"},
        "model_cooldowns": {f"{R}/accounts/work": "2026-10-12T22:00:00+00:00"},
        "account_alerts": {f"{R}/accounts/work": {
            "reason": "logged_out", "since": "2026-10-10T00:00:00+00:00",
            "notified": True, "resolved": False,
            "snooze_until": "2026-10-17T00:00:00+00:00", "cred_fp": "fp-work-2"}},
        "login_expiry_warnings": {
            f"{R}/accounts/main": "2026-11-12T14:10:01+00:00"},
        "schedules": [sched_full.to_dict(), sched_wake.to_dict()],
        "watches": [watch.to_dict()],
        "watch_counter": 9,
    }


def _check_fixture_state() -> None:
    print("\nfixture state file: load -> save -> compare")
    state_file = TMP / "fixture" / "state.json"
    state_file.parent.mkdir()
    fixture = fixture_state()
    _eq("fixture names every persisted top-level key (no more, no fewer)",
        set(fixture), set(EXPECTED_KEYS))
    for key in sorted(EXPECTED_KEYS):
        v = fixture[key]
        _check(f"fixture value for {key!r} is non-empty",
               v not in (None, "", 0, 0.0, [], {}), _short(v))

    state_file.write_text(json.dumps(fixture), encoding="utf-8")
    store = _store(state_file)
    store.save()
    saved = _read(state_file)

    _eq("saved top-level keys equal the explicit expected set",
        set(saved), set(EXPECTED_KEYS))
    for key in sorted(EXPECTED_KEYS):
        _eq(f"key {key!r} survives load then save unchanged",
            saved.get(key), fixture[key])

    _eq("platform_state.discord.orch_resume_payloads is persisted verbatim",
        saved["platform_state"]["discord"].get("orch_resume_payloads"),
        {"tok-1": "resume prompt one", "tok-2": "resume prompt two"})

    # Second generation: a store that loaded what the first one wrote must
    # write the identical bytes. Catches key-order or normalisation drift.
    first_bytes = state_file.read_text(encoding="utf-8")
    store2 = _store(state_file)
    _eq("second-generation payload is byte-identical to the first",
        store2._serialize(), first_bytes)

    _check_fixture_getters(store)
    _check_counters_continue(state_file)


def _check_fixture_getters(store: StateStore) -> None:
    """Spot-check the loaded values through the public API, not the JSON."""
    print("\nfixture state file: public getters on the loaded store")
    fx = fixture_state()
    _eq("mode", store.mode, "build")
    _eq("context", store.context, "working on the refactor")
    _eq("effort", store.effort, "max")
    _eq("verbose_level", store.verbose_level, 2)
    _eq("active_session_id", store.active_session_id, "sess-active-9")
    _eq("active_provider", store.active_provider, "api-fallback")
    _eq("list_repos", store.list_repos(), fx["repos"])
    _eq("active repo", store.get_active_repo(),
        ("beta", f"{R}/repos/beta"))
    _eq("dormant repos", store.list_dormant_repos(), ["alpha"])
    _eq("repo description entry",
        store.get_repo_description_entry("alpha"),
        fx["repo_descriptions"]["alpha"])
    _eq("aliases", store.list_aliases(), fx["aliases"])
    _eq("daily cost (cost_date is today)", store.get_daily_cost(), 12.5)
    _eq("fallback spend (date is today)", store.get_fallback_spend_today(), 4.75)

    full = store.get_instance("t-501")
    _check("full instance reloads", full is not None)
    if full is not None:
        _eq("full instance equals the source on every persisted field",
            {n: getattr(full, n) for n in _persisted_fields()},
            {n: getattr(full_instance(), n) for n in _persisted_fields()})
        _eq("lookup by name finds the full instance",
            getattr(store.get_instance("fullname"), "id", None), "t-501")
    sparse = store.get_instance("q-502")
    _check("sparse instance reloads", sparse is not None)
    if sparse is not None:
        _eq("sparse instance equals the source on every persisted field",
            {n: getattr(sparse, n) for n in _persisted_fields()},
            {n: getattr(sparse_instance(), n) for n in _persisted_fields()})

    sched = store.get_schedule("sch-010")
    _eq("disabled schedule reloads with every field",
        sched.to_dict() if sched else None, fx["schedules"][0])
    wake = store.get_schedule("sch-011")
    _eq("wake schedule reloads with every field",
        wake.to_dict() if wake else None, fx["schedules"][1])
    _eq("list_schedules still hides the disabled one",
        [s.id for s in store.list_schedules()], ["sch-011"])
    watch = store.get_watch("w-009")
    _eq("watch reloads with every field",
        watch.to_dict() if watch else None, fx["watches"][0])
    _eq("watch is found by channel",
        getattr(store.watch_for_channel("chan-watch"), "id", None), "w-009")

    _eq("deploy state",
        store.get_deploy_state("alpha").to_dict(), _deploy_state().to_dict())
    _eq("deploy config", store.get_deploy_config("alpha"),
        fx["deploy_configs"]["alpha"])
    _eq("auto-fix state",
        store.get_auto_fix_state("alpha", "build").to_dict(),
        {"attempt": 3, "thread_id": "th-auto"})

    _eq("account cooldowns", store.get_account_cooldowns(),
        fx["account_cooldowns"])
    _eq("account cooldown fingerprints", store.get_account_cooldown_fps(),
        fx["account_cooldown_fps"])
    _eq("model cooldowns", store.get_model_cooldowns(), fx["model_cooldowns"])
    _eq("account alerts", store.get_account_alerts(), fx["account_alerts"])
    _eq("login expiry warnings", store.get_login_expiry_warnings(),
        fx["login_expiry_warnings"])
    _eq("sidelined accounts", store.sidelined_accounts(),
        {f"{R}/accounts/work"})

    _eq("platform state (discord)", store.get_platform_state("discord"),
        fx["platform_state"]["discord"])
    _eq("platform state (telegram)", store.get_platform_state("telegram"),
        fx["platform_state"]["telegram"])

    sid = "sess-chain-1"
    _eq("autopilot chain", store.get_autopilot_chain(sid),
        ["review_code", "commit", "done"])
    _eq("autopilot chain meta", store.get_autopilot_chain_meta(sid),
        fx["autopilot_chain_meta"][sid])
    _eq("chain kwargs", store.get_chain_kwargs(sid), ("Build & Ship", True))
    _eq("chain plan override", store.get_chain_plan_override(sid),
        "PLAN: do the three things")
    _eq("chain deferred", store.get_chain_deferred(sid),
        fx["chain_deferred"][sid])
    _eq("chain entry sha", store.get_chain_entry_sha(sid), "0123456789abcdef")
    phases = store.get_chain_phases(sid)
    _eq("chain phases", phases.to_dict() if phases else None,
        _chain_phase_state().to_dict())
    _eq("pending merge", store.get_pending_merge("t-501"),
        fx["pending_merges"]["t-501"])
    _eq("scheduled merge by session",
        store.get_scheduled_merge_by_session("sess-chain-2"),
        ("t-502", fx["scheduled_merges"]["t-502"]))


def _check_counters_continue(state_file: Path) -> None:
    """Counters have no getter; the id the next record gets is the proof."""
    print("\nfixture state file: counters continue from the persisted values")
    work = TMP / "counters" / "state.json"
    work.parent.mkdir()
    work.write_text(state_file.read_text(encoding="utf-8"), encoding="utf-8")
    store = _store(work)
    _eq("task_counter: next task id",
        store.create_instance(InstanceType.TASK, "p").id, "t-502")
    _eq("task_counter is shared by scheduled instances",
        store.create_instance(InstanceType.SCHEDULED, "p").id, "s-503")
    _eq("query_counter: next query id",
        store.create_instance(InstanceType.QUERY, "p").id, "q-503")
    _eq("schedule_counter: next schedule id",
        store.add_schedule("later", interval_secs=60).id, "sch-012")
    _eq("watch_counter: next watch id",
        store.add_watch(Watch(id="", channel_id="other", prompt="p")).id,
        "w-010")


# ---------------------------------------------------------------------------
# The write path: populate through the public setters, then reload.
# ---------------------------------------------------------------------------

def _populate_through_api(store: StateStore) -> None:
    alpha, beta = f"{R}/repos/alpha", f"{R}/repos/beta"
    store.add_repo("alpha", alpha)
    store.add_repo("beta", beta)
    store.switch_repo("beta")
    store.set_repo_dormant("alpha", True)
    store.set_repo_description("alpha", "Alpha does things.", "CLAUDE.md",
                               "sig-abc", 1760000000.5, alpha)

    store.mode = "build"
    store.context = "working on the refactor"
    store.effort = "max"
    store.verbose_level = 2
    store.active_session_id = "sess-active-9"
    store.active_provider = "api-fallback"
    store.set_alias("ship", "run the ship chain")

    store.add_cost(12.5)
    store.add_fallback_cost(4.75)

    # Instances: the real creation path (counters + ids), then every
    # persisted field copied on from the reference record.
    ref = full_instance()
    inst = store.create_instance(InstanceType.TASK, "seed", name="fullname")
    for n in _persisted_fields():
        if n != "id":
            setattr(inst, n, getattr(ref, n))
    store.update_instance(inst, critical=True)
    store.create_instance(InstanceType.QUERY, "a question")

    # Schedules: a cron one with every field set, a wake, a nudge.
    sched = store.add_schedule("nightly review", interval_secs=3600,
                               run_at="2026-11-01T00:00:00+00:00", mode="build")
    sched.last_run_at = "2026-10-10T00:00:00+00:00"
    sched.last_summary = "all fine"
    sched.enabled = False
    sched.label = "cron-label"
    sched.channel_id = "chan-sched"
    sched.resume_thread = True
    store.update_schedule(sched)
    store.add_wake("wake me", "chan-wake", "2026-10-14T12:00:00+00:00",
                   repo_name="beta", repo_path=beta)
    store.upsert_nudge("nudge-1", "check in", "chan-nudge", 86400,
                       "2026-10-13T09:00:00+00:00", "alpha", alpha)

    store.add_watch(Watch(
        id="", channel_id="chan-watch", prompt="the job finished",
        label="sculpt fit", pid=31337, pid_start="987654321",
        pid_dead_at_arm=True, log_path=f"{alpha}/run.log",
        progress_re=r"(\d+)/(\d+) frames", done_re="ALL DONE",
        armed_at="2026-10-11T08:00:00+00:00",
        timeout_at="2026-10-11T14:00:00+00:00", every_secs=240,
        heartbeat_msg_id="hb-555", last_beat_at="2026-10-11T09:00:00+00:00",
        repo_name="alpha", repo_path=alpha,
    ))

    # Chain state.
    sid = "sess-chain-1"
    store.set_autopilot_chain(sid, ["review_code", "commit", "done"])
    store.set_autopilot_chain_status(sid, "paused")
    store.set_chain_kwargs(sid, label="Build & Ship", silent_close=True)
    store.set_chain_plan_override(sid, "PLAN: do the three things")
    store.set_chain_deferred(sid, ["[Tag] deferred one (Low)"])
    store.set_chain_entry_sha(sid, "0123456789abcdef")
    store.set_chain_phases(sid, _chain_phase_state())
    store.set_pending_merge("t-501", session_id=sid, channel_id="chan-merge",
                            repo_name="alpha", message="Merge failed: conflict",
                            failure_kind="conflict")
    store.set_pending_merge_resolver("t-501", "t-600")
    store.append_pending_merge_deferred_text("t-501", "keep both")
    store.set_scheduled_merge("t-502", session_id="sess-chain-2",
                              channel_id="chan-sm", repo_name="beta",
                              branch="claude-bot/t-502",
                              merge_at="2026-10-12T20:00:00+00:00")
    store.hold_scheduled_merge("t-502")

    # Deploy / auto-fix.
    store.set_deploy_state("alpha", _deploy_state())
    store.set_deploy_config("alpha", {"command": "make deploy",
                                      "nested": {"a": [1, 2, 3]}})
    store.set_auto_fix_state("alpha", "build",
                             AutoFixState(attempt=3, thread_id="th-auto"))

    # Accounts.
    main, work = f"{R}/accounts/main", f"{R}/accounts/work"
    store.set_account_cooldown(main, "2026-10-13T00:00:00+00:00", "fp-main-1")
    store.set_model_cooldown(work, "2026-10-12T22:00:00+00:00")
    store.set_account_alert(work, "logged_out", "2026-10-10T00:00:00+00:00",
                            "fp-work-2")
    store.snooze_account_alert(work, "2026-10-17T00:00:00+00:00")
    store.set_login_expiry_warned(work, "2026-11-12T14:10:01+00:00")

    store.set_platform_state("discord", {
        "forum_projects": {"alpha": {"forum_channel_id": "f-1"}},
        "orch_resume_payloads": {"tok-1": "resume prompt one"},
        "ark_channel_id": "ark-1",
    })
    store.set_platform_state("telegram", {"offset": 99})
    store.save()


def _check_api_populated() -> None:
    print("\npublic-setter state: populate -> save -> reload")
    state_file = TMP / "api" / "state.json"
    state_file.parent.mkdir()
    store = _store(state_file)
    _populate_through_api(store)
    saved = _read(state_file)

    _eq("saved top-level keys equal the explicit expected set",
        set(saved), set(EXPECTED_KEYS))
    for key in sorted(EXPECTED_KEYS):
        v = saved.get(key)
        _check(f"public API populated {key!r} with a non-default value",
               v not in (None, "", 0, 0.0, [], {}), _short(v))

    # Values that are not clock-generated can be pinned exactly.
    _eq("task_counter", saved["task_counter"], 1)
    _eq("query_counter", saved["query_counter"], 1)
    _eq("schedule_counter", saved["schedule_counter"], 3)
    _eq("watch_counter", saved["watch_counter"], 1)
    _eq("mode", saved["mode"], "build")
    _eq("active_repo", saved["active_repo"], "beta")
    _eq("dormant_repos", saved["dormant_repos"], ["alpha"])
    _eq("daily_cost + total_cost", (saved["daily_cost"], saved["total_cost"]),
        (12.5, 12.5))
    _eq("account_alerts record (notified by snooze)",
        saved["account_alerts"][f"{R}/accounts/work"],
        {"reason": "logged_out", "since": "2026-10-10T00:00:00+00:00",
         "notified": True, "resolved": False,
         "snooze_until": "2026-10-17T00:00:00+00:00", "cred_fp": "fp-work-2"})
    _eq("pending_merges resolver and deferred text",
        (saved["pending_merges"]["t-501"]["resolver_instance_id"],
         saved["pending_merges"]["t-501"]["deferred_text"]),
        ("t-600", "keep both"))
    _eq("scheduled_merges held flag", saved["scheduled_merges"]["t-502"]["held"],
        True)
    _eq("chain_phases", saved["chain_phases"]["sess-chain-1"],
        _chain_phase_state().to_dict())

    _check("instance.bash_commands is not persisted (S1)",
           "bash_commands" not in saved["instances"][0])
    _eq("platform_state.discord.orch_resume_payloads is persisted verbatim",
        saved["platform_state"]["discord"].get("orch_resume_payloads"),
        {"tok-1": "resume prompt one"})

    persisted = {n: getattr(full_instance(), n) for n in _persisted_fields()
                 if n != "id"}
    api_inst = store.get_instance("fullname")
    _eq("created instance carries every persisted field before reload",
        {n: getattr(api_inst, n) for n in persisted}, persisted)

    # Reload through a fresh store: every key must come back identical.
    store2 = _store(state_file)
    resaved = json.loads(store2._serialize())
    for key in sorted(EXPECTED_KEYS):
        _eq(f"key {key!r} survives reload then reserialize",
            resaved.get(key), saved[key])
    _eq("reloaded payload is byte-identical to the saved file",
        store2._serialize(), state_file.read_text(encoding="utf-8"))

    reloaded = store2.get_instance("fullname")
    _eq("reloaded instance carries every persisted field",
        {n: getattr(reloaded, n) for n in persisted} if reloaded else None,
        persisted)
    _eq("reloaded mode/context/effort/verbose/session/provider",
        (store2.mode, store2.context, store2.effort, store2.verbose_level,
         store2.active_session_id, store2.active_provider),
        ("build", "working on the refactor", "max", 2, "sess-active-9",
         "api-fallback"))
    _eq("reloaded active repo and dormant set",
        (store2.get_active_repo(), store2.list_dormant_repos()),
        (("beta", f"{R}/repos/beta"), ["alpha"]))
    _eq("reloaded schedules (all three, incl. the disabled one)",
        sorted(s["id"] for s in resaved["schedules"]),
        ["sch-001", "sch-002", "sch-003"])
    _eq("reloaded watch", getattr(store2.get_watch("w-001"), "label", None),
        "sculpt fit")
    _eq("reloaded chain phases",
        store2.get_chain_phases("sess-chain-1").to_dict(),
        _chain_phase_state().to_dict())
    _eq("reloaded account state",
        (store2.get_account_cooldowns(), store2.get_account_cooldown_fps(),
         store2.get_model_cooldowns()),
        ({f"{R}/accounts/main": "2026-10-13T00:00:00+00:00"},
         {f"{R}/accounts/main": "fp-main-1"},
         {f"{R}/accounts/work": "2026-10-12T22:00:00+00:00"}))
    _eq("reloaded platform state",
        (store2.get_platform_state("discord"),
         store2.get_platform_state("telegram")),
        ({"forum_projects": {"alpha": {"forum_channel_id": "f-1"}},
          "orch_resume_payloads": {"tok-1": "resume prompt one"},
          "ark_channel_id": "ark-1"}, {"offset": 99}))


# ---------------------------------------------------------------------------
# Old or damaged files come up with defaults, not an exception.
# ---------------------------------------------------------------------------

def _assert_defaults(label: str, store: StateStore, *, repos=None,
                     instances=0) -> None:
    _eq(f"{label}: mode/effort/verbose defaults",
        (store.mode, store.effort, store.verbose_level), ("explore", "high", 1))
    _eq(f"{label}: context/session/provider default to None",
        (store.context, store.active_session_id, store.active_provider),
        (None, None, None))
    _eq(f"{label}: repos", store.list_repos(), repos or {})
    _eq(f"{label}: no dormant repos", store.list_dormant_repos(), [])
    _eq(f"{label}: instance count", store.instance_count(), instances)
    _eq(f"{label}: no schedules/watches/aliases",
        (store.list_schedules(), store.list_watches(), store.list_aliases()),
        ([], [], {}))
    _eq(f"{label}: no chains",
        store.get_all_autopilot_chains(), {})
    _eq(f"{label}: no account state",
        (store.get_account_cooldowns(), store.get_account_cooldown_fps(),
         store.get_model_cooldowns(), store.get_account_alerts()),
        ({}, {}, {}, {}))
    _eq(f"{label}: no cost",
        (store.get_daily_cost(), store.get_fallback_spend_today()), (0.0, 0.0))
    _eq(f"{label}: no platform state", store.get_platform_state("discord"), {})
    _eq(f"{label}: no active repo", store.get_active_repo(), (None, None))


def _check_old_shaped() -> None:
    print("\nold-shaped and damaged state files")
    # The store logs a warning or a traceback for each deliberately damaged
    # input below; that is the behaviour under test, not noise worth reading.
    logging.disable(logging.CRITICAL)
    try:
        _check_old_shaped_inner()
    finally:
        logging.disable(logging.NOTSET)


def _check_old_shaped_inner() -> None:

    # 1. An empty object: every key missing.
    f = TMP / "old-empty" / "state.json"
    f.parent.mkdir()
    f.write_text("{}", encoding="utf-8")
    s = _store(f)
    _assert_defaults("empty object", s)
    _eq("empty object: counters start at 1",
        (s.create_instance(InstanceType.TASK, "p").id,
         s.create_instance(InstanceType.QUERY, "p").id,
         s.add_schedule("x", interval_secs=5).id),
        ("t-001", "q-001", "sch-001"))
    f2 = TMP / "old-empty-saved" / "state.json"
    f2.parent.mkdir()
    s2 = _store(f2)
    s2.save()
    _eq("a default store still writes every persisted key",
        set(_read(f2)), set(EXPECTED_KEYS))

    # 1b. A file written before S1 still carries every instance's bash log.
    #     It loads, the instance is otherwise intact, and the next save drops
    #     the field instead of carrying 27% of the file forward forever.
    f = TMP / "old-bash-log" / "state.json"
    f.parent.mkdir()
    legacy = full_instance().to_dict()
    legacy["bash_commands"] = ["git status", "pytest -q"]
    f.write_text(json.dumps({"instances": [legacy]}), encoding="utf-8")
    s = _store(f)
    got = s.get_instance(legacy["id"])
    _eq("legacy bash log: the instance still loads intact",
        got.to_dict() if got else None, full_instance().to_dict())
    s.save()
    _check("legacy bash log: the next save drops it",
           "bash_commands" not in _read(f)["instances"][0])

    f = TMP / "old-min" / "state.json"
    f.parent.mkdir()
    f.write_text(json.dumps({
        "repos": {"solo": f"{R}/repos/solo"},
        "active_repo": "solo",
        "instances": [{
            "id": "t-007", "instance_type": "task", "prompt": "old prompt",
            "repo_name": "solo", "repo_path": f"{R}/repos/solo",
            "status": "completed",
        }],
    }), encoding="utf-8")
    s = _store(f)
    inst = s.get_instance("t-007")
    _check("minimal instance loads", inst is not None)
    if inst is not None:
        _eq("minimal instance fills every other field with its default",
            {n: getattr(inst, n) for n in _persisted_fields()
             if n not in ("id", "instance_type", "prompt", "repo_name",
                          "repo_path", "status")},
            {n: getattr(_default_instance(), n) for n in _persisted_fields()
             if n not in ("id", "instance_type", "prompt", "repo_name",
                          "repo_path", "status")})
    _eq("minimal file: repo registry and active repo",
        (s.list_repos(), s.get_active_repo()),
        ({"solo": f"{R}/repos/solo"}, ("solo", f"{R}/repos/solo")))
    _eq("minimal file: mode/effort/verbose still default",
        (s.mode, s.effort, s.verbose_level), ("explore", "high", 1))
    s.save()
    _eq("minimal file: re-saved file has every persisted key",
        set(_read(f)), set(EXPECTED_KEYS))

    # 3. Legacy spellings and malformed records inside an otherwise valid file.
    f = TMP / "old-legacy" / "state.json"
    f.parent.mkdir()
    base = {"id": "t-010", "instance_type": "task", "prompt": "p",
            "repo_name": "", "repo_path": "", "status": "failed"}
    f.write_text(json.dumps({
        "instances": [
            {**base, "id": "t-010", "telegram_message_ids": [11, 22]},
            {**base, "id": "t-011", "origin": "origin_from_the_future"},
            {**base, "id": "t-012", "spawn_dispatched_thread_id": "th-old"},
        ],
        "autopilot_chains": {"metered": ["commit"], "unmetered": ["done"]},
        "autopilot_chain_meta": {"metered": {
            "status": "running", "updated_at": "2026-10-01T00:00:00+00:00"}},
        "watches": [
            {"channel_id": "no-id-so-malformed", "prompt": "x"},
            {"id": "w-004", "channel_id": "good", "prompt": "survives"},
        ],
        "some_key_from_a_newer_version": {"ignored": True},
    }), encoding="utf-8")
    s = _store(f)
    _eq("legacy telegram_message_ids migrate into message_ids",
        s.get_instance("t-010").message_ids, {"telegram": ["11", "22"]})
    _eq("an unknown origin degrades to direct",
        s.get_instance("t-011").origin, InstanceOrigin.DIRECT)
    _eq("legacy single spawn_dispatched_thread_id migrates into the list",
        s.get_instance("t-012").spawn_dispatched_thread_ids, ["th-old"])
    _eq("a chain with no meta record is dropped, a metered one kept",
        s.get_all_autopilot_chains(), {"metered": ["commit"]})
    _eq("a malformed watch is dropped and the good one kept",
        [w.id for w in s.list_watches()], ["w-004"])
    s.save()
    resaved = _read(f)
    _check("an unknown top-level key is not carried forward",
           "some_key_from_a_newer_version" not in resaved)
    _eq("re-saved legacy file has exactly the expected keys",
        set(resaved), set(EXPECTED_KEYS))

    # 4. No file, and a corrupt file: both come up empty, neither raises.
    f = TMP / "old-missing" / "state.json"
    f.parent.mkdir()
    _assert_defaults("missing file", _store(f))
    f = TMP / "old-corrupt" / "state.json"
    f.parent.mkdir()
    f.write_text("{this is not json", encoding="utf-8")
    _assert_defaults("corrupt file", _store(f))


def main() -> int:
    _check_instance_schema()
    _check_fixture_state()
    _check_api_populated()
    _check_old_shaped()

    print()
    if _failures:
        print(f"FAIL: {len(_failures)} of {_checks} checks failed")
        for name in _failures:
            print(f"  - {name}")
        return 1
    print(f"PASS: all {_checks} checks passed")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        tmpdir.cleanup()
    sys.exit(code)

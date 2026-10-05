"""Tests for the per-thread /model pin.

A thread can pin the model its sessions run on. Three properties matter, and
all three exist because model names change over time (version bumps, renames,
whatever comes after the current generation):

  1. No hardcoded list of model names decides what is accepted. Validation is
     a SHAPE check, so a model released after this code was written still
     works, while a fat-fingered value with spaces or shell metacharacters is
     refused before it can reach a command line.
  2. Display is generic: short_model_label parses arbitrary names.
  3. The suggestion list is derived at runtime from this deployment's own
     config plus the models it has actually run, so it maintains itself.

Plus the wiring: the pin survives a save/load round trip, beats configured
spawn routing, loses to the model-limit failover downgrade, and reaches the
CLI as --model.

Run: ``python scripts/test_model_command.py``  (exit 0 on pass).
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import config
from bot.claude.provider import PROVIDERS
from bot.claude.types import (
    BUILD_ORIGINS, Instance, InstanceOrigin, InstanceStatus, InstanceType,
)
from bot.discord.forums import ThreadInfo
from bot.engine.workflows import resolve_spawn_model
from bot.platform.formatting import (
    MODEL_CLEAR_WORDS, model_suggestions, normalize_model, short_model_label,
)


@contextmanager
def _cfg(**kw):
    """Temporarily set model-relevant config knobs, then restore."""
    keys = (
        "BUILD_MODEL", "MODEL_ROUTING", "EXPLORE_MODEL", "DEFAULT_SESSION_MODEL",
        "PRIMARY_MODEL", "MODEL_FALLBACK", "MODEL_CHOICES",
    )
    saved = {k: getattr(config, k) for k in keys}
    # Every test starts from a neutral deployment and sets only what it means
    # to exercise, so a real .env can never make a test pass or fail.
    for k in keys:
        setattr(config, k, kw.get(k.lower(), _EMPTY[k]))
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(config, k, v)


# Neutral values so a test only sees the knobs it sets.
_EMPTY = {
    "BUILD_MODEL": "", "MODEL_ROUTING": {}, "EXPLORE_MODEL": None,
    "DEFAULT_SESSION_MODEL": None, "PRIMARY_MODEL": "", "MODEL_FALLBACK": "",
    "MODEL_CHOICES": [],
}


# --- 1. Validation is a shape check, not a whitelist ---

def _test_accepts_unknown_and_future_names() -> list[str]:
    failures: list[str] = []
    # Names that do not exist today, including the "they just call it X" case.
    for raw, want in [
        ("opus", "opus"),
        ("  Fable  ", "fable"),
        ("x", "x"),
        ("claude-x", "claude-x"),
        ("claude-opus-9", "claude-opus-9"),
        ("claude-fable-5-5", "claude-fable-5-5"),
        ("opus-4.8", "opus-4.8"),
        ("us.anthropic.claude-opus-7-v1:0", "us.anthropic.claude-opus-7-v1:0"),
        ("some_future_name", "some_future_name"),
    ]:
        got = normalize_model(raw)
        if got != want:
            failures.append(f"normalize_model({raw!r}) = {got!r}, want {want!r}")
    return failures


def _test_rejects_unusable_input() -> list[str]:
    failures: list[str] = []
    for raw in [
        "", "   ",
        "fable x",             # the /model fable-x style typo, with a space
        "opus; rm -rf /",      # shell metacharacters
        "--dangerous-flag",    # leading dash would read as a flag
        "$(whoami)",
        "a" * 80,              # absurd length
    ]:
        got = normalize_model(raw)
        if got is not None:
            failures.append(f"normalize_model({raw!r}) = {got!r}, want None")
    return failures


# --- 2. Display carries no baked-in version numbers ---

def _test_labels_are_generic() -> list[str]:
    failures: list[str] = []
    for raw, want in [
        ("claude-opus-9", "Opus 9"),
        ("claude-fable-5-5", "Fable 5.5"),
        ("x", "X"),
        ("claude-x-latest", "X"),
        ("us.anthropic.claude-opus-7-v1:0", "Opus 7"),
        ("some_future_name", "Some_future_name"),
    ]:
        got = short_model_label(raw)
        if got != want:
            failures.append(f"short_model_label({raw!r}) = {got!r}, want {want!r}")
    return failures


def _test_source_has_no_model_name_table() -> list[str]:
    """The regression this whole design exists to prevent."""
    failures: list[str] = []
    root = Path(__file__).resolve().parents[1]
    fmt = (root / "bot/platform/formatting.py").read_text()
    # A MODEL_DISPLAY-style dict would be exactly the stale-list trap.
    if "MODEL_DISPLAY" in fmt:
        failures.append("formatting.py grew a hardcoded MODEL_DISPLAY table")
    if "VALID_MODELS" in fmt:
        failures.append("formatting.py grew a closed VALID_MODELS whitelist")
    return failures


# --- 3. Suggestions are derived, never hardcoded ---

@dataclass
class _FakeInstance:
    model: str | None = None
    context_model: str | None = None


class _FakeStore:
    def __init__(self, instances):
        self._instances = instances

    def list_instances(self, all_: bool = False):
        return list(self._instances)


def _test_suggestions_derive_from_history_and_config() -> list[str]:
    failures: list[str] = []
    store = _FakeStore([
        _FakeInstance(context_model="claude-newmodel-3"),
        _FakeInstance(model="opus"),
    ])
    with _cfg(primary_model="fable", model_fallback="opus", build_model="opus",
              model_routing={"plan": "sonnet"}):
        got = model_suggestions(store)
    for want in ("claude-newmodel-3", "opus", "fable", "sonnet"):
        if want not in got:
            failures.append(f"model_suggestions missing {want!r} (got {got})")
    # Config aliases lead: they are what a human types, and they do not
    # reshuffle every time another run finishes.
    if got and got[0] != "fable":
        failures.append(f"configured alias should lead, got {got[0]!r}")
    if "claude-newmodel-3" in got and "fable" in got:
        if got.index("claude-newmodel-3") < got.index("fable"):
            failures.append(f"history should follow config, got {got}")
    if len(got) != len(set(got)):
        failures.append(f"model_suggestions returned duplicates: {got}")
    return failures


def _test_explicit_choices_win() -> list[str]:
    failures: list[str] = []
    store = _FakeStore([_FakeInstance(model="opus")])
    with _cfg(model_choices=["alpha", "beta"], primary_model="fable"):
        got = model_suggestions(store)
    if got != ["alpha", "beta"]:
        failures.append(f"MODEL_CHOICES override ignored: {got}")
    return failures


def _test_explicit_choices_are_normalized() -> list[str]:
    """A hand-edited .env is untidy; the list still has to be usable as-is."""
    failures: list[str] = []
    with _cfg(model_choices=["  Opus 5  ", "FABLE-6", "fable-6", "", "opus"]):
        got = model_suggestions(None)
    if got != ["fable-6", "opus"]:
        failures.append(
            f"MODEL_CHOICES should drop unusable/duplicate entries and lowercase "
            f"the rest, got {got}"
        )
    return failures


def _test_suggestions_survive_a_broken_store() -> list[str]:
    """Autocomplete is a convenience — it must never raise into a command."""
    failures: list[str] = []

    class _Broken:
        def list_instances(self, all_=False):
            raise RuntimeError("state file unreadable")

    with _cfg(primary_model="fable"):
        try:
            got = model_suggestions(_Broken())
        except Exception as exc:
            return [f"model_suggestions raised on a broken store: {exc!r}"]
    if "fable" not in got:
        failures.append(f"config-derived names lost when history failed: {got}")
    return failures


def _test_empty_deployment_yields_empty_list() -> list[str]:
    with _cfg():
        got = model_suggestions(None)
    return [] if got == [] else [f"unconfigured deployment suggested {got}"]


# --- 4. Persistence round trip ---

def _test_thread_pin_round_trips() -> list[str]:
    failures: list[str] = []
    for pin in ("claude-future-9", "", None):
        info = ThreadInfo(thread_id="123", model=pin)
        back = ThreadInfo.from_dict(info.to_dict())
        if back.model != pin:
            failures.append(f"ThreadInfo model {pin!r} round-tripped as {back.model!r}")
    # An unset pin must not bloat state.json.
    if "model" in ThreadInfo(thread_id="123").to_dict():
        failures.append("unset model serialized into state")
    # A thread saved before this feature existed loads as 'no pin'.
    legacy = ThreadInfo.from_dict({"thread_id": "123"})
    if legacy.model is not None:
        failures.append(f"legacy thread got model {legacy.model!r}, want None")
    return failures


# --- 5. Precedence ---

def _test_pin_beats_spawn_routing() -> list[str]:
    failures: list[str] = []
    with _cfg(build_model="opus", model_routing={"plan": "sonnet"}):
        for origin in list(BUILD_ORIGINS)[:3] + [InstanceOrigin.PLAN]:
            got = resolve_spawn_model(origin, "pinned-model")
            if got != "pinned-model":
                failures.append(f"{origin.value}: pin lost to routing (got {got!r})")
        # No pin -> untouched behaviour.
        if resolve_spawn_model(InstanceOrigin.PLAN) != "sonnet":
            failures.append("unpinned plan step stopped honouring MODEL_ROUTING")
        if resolve_spawn_model(InstanceOrigin.PLAN, "") != "sonnet":
            failures.append("cleared pin ('') should fall back to routing")
        if resolve_spawn_model(InstanceOrigin.PLAN, None) != "sonnet":
            failures.append("absent pin should fall back to routing")
    return failures


# --- 6. It reaches the CLI, and the quota downgrade still wins ---

def _instance(model: str | None) -> Instance:
    inst = Instance(
        id="test-1", instance_type=InstanceType.QUERY, prompt="hi", name=None,
        repo_name="r", repo_path="/tmp", status=InstanceStatus.QUEUED,
    )
    inst.model = model
    return inst


def _test_pin_reaches_command_line() -> list[str]:
    failures: list[str] = []
    provider = PROVIDERS.get("claude")
    if provider is None:
        return ["claude provider missing"]

    def _model_flag(cmd):
        return cmd[cmd.index("--model") + 1] if "--model" in cmd else None

    cmd = provider.build_command(
        _instance("claude-future-9"), system_prompt_file=None,
        system_prompt_inline=None, api_fallback=False, api_key_file=None,
    )
    if _model_flag(cmd) != "claude-future-9":
        failures.append(f"pinned model not passed to CLI: {_model_flag(cmd)!r}")

    # A model-limit downgrade must still beat the pin, or a thread pinned to a
    # model whose quota is spent would fail instead of falling back.
    cmd = provider.build_command(
        _instance("claude-future-9"), system_prompt_file=None,
        system_prompt_inline=None, api_fallback=False, api_key_file=None,
        model_override="fallback-model",
    )
    if _model_flag(cmd) != "fallback-model":
        failures.append(f"quota downgrade lost to the pin: {_model_flag(cmd)!r}")

    # No pin -> the deployment default still applies, unchanged.
    with _cfg(default_session_model="deployment-default"):
        cmd = provider.build_command(
            _instance(None), system_prompt_file=None,
            system_prompt_inline=None, api_fallback=False, api_key_file=None,
        )
    if _model_flag(cmd) != "deployment-default":
        failures.append(f"unpinned run lost the default: {_model_flag(cmd)!r}")
    return failures


def _test_autocomplete_never_dead_ends() -> list[str]:
    """The dropdown must always offer something pickable for a usable name.

    Registers the real slash commands on a real command tree and drives
    /model's autocomplete callback. A model released after this build matches
    nothing in the suggestion list, and an autocomplete showing zero options is
    close to unusable on a phone -- so the typed name itself becomes the
    option, but ONLY when nothing else matched, or a half-typed prefix would
    sit next to the real match it is a prefix of.
    """
    import asyncio

    import discord
    from discord import app_commands

    from bot.discord import slash_commands

    class _StubBot:
        def __init__(self, store):
            self._store = store
            self._guild_id = 1
            self.tree = app_commands.CommandTree(
                discord.Client(intents=discord.Intents.none())
            )

        def _is_owner(self, _uid):
            return True

        def _check_access(self, *_a, **_kw):  # pragma: no cover - never reached
            raise AssertionError("access check should not run during autocomplete")

        async def _run_slash(self, *_a, **_kw):  # pragma: no cover
            pass

    failures: list[str] = []
    store = _FakeStore([_FakeInstance(context_model="claude-opus-5")])
    with _cfg(primary_model="fable", build_model="opus"):
        bot = _StubBot(store)
        slash_commands.setup(bot)
        cmd = bot.tree.get_command("model", guild=discord.Object(id=1))
        if cmd is None:
            return ["/model is not registered on the command tree"]
        autocomplete = cmd._params["name"].autocomplete
        if autocomplete is None:
            return ["/model has no autocomplete attached"]

        def offer(typed):
            return [c.value for c in asyncio.run(autocomplete(None, typed))]

        opened = offer("")
        if "default" not in opened:
            failures.append(f"opening the dropdown should offer 'default': {opened}")

        unseen = offer("claude-opus-9")
        if unseen != ["claude-opus-9"]:
            failures.append(
                f"a model this deployment never ran must still be pickable: {unseen}"
            )

        partial = offer("op")
        if "opus" not in partial:
            failures.append(f"'op' should match the model it prefixes: {partial}")
        if "op" in partial:
            failures.append("a half-typed prefix must not sit next to its real match")

        junk = offer("fable x")
        if junk:
            failures.append(f"a value with a space must never be offered: {junk}")

    return failures


def _test_clear_words_cover_the_obvious_ones() -> list[str]:
    missing = [w for w in ("default", "clear", "reset") if w not in MODEL_CLEAR_WORDS]
    return [f"missing clear word(s): {missing}"] if missing else []


def main() -> int:
    checks = [
        ("accepts-future-names", _test_accepts_unknown_and_future_names()),
        ("rejects-unusable-input", _test_rejects_unusable_input()),
        ("labels-are-generic", _test_labels_are_generic()),
        ("no-model-name-table", _test_source_has_no_model_name_table()),
        ("suggestions-derived", _test_suggestions_derive_from_history_and_config()),
        ("explicit-choices-win", _test_explicit_choices_win()),
        ("choices-are-normalized", _test_explicit_choices_are_normalized()),
        ("suggestions-survive-broken-store", _test_suggestions_survive_a_broken_store()),
        ("empty-deployment", _test_empty_deployment_yields_empty_list()),
        ("thread-pin-round-trip", _test_thread_pin_round_trips()),
        ("pin-beats-routing", _test_pin_beats_spawn_routing()),
        ("pin-reaches-cli", _test_pin_reaches_command_line()),
        ("autocomplete-never-dead-ends", _test_autocomplete_never_dead_ends()),
        ("clear-words", _test_clear_words_cover_the_obvious_ones()),
    ]
    total = sum(len(f) for _, f in checks)
    if total:
        print("FAIL: /model tests")
        for name, fails in checks:
            for f in fails:
                print(f"  [{name}] {f}")
        return 1
    print("PASS: /model tests")
    for name, _ in checks:
        print(f"  - {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

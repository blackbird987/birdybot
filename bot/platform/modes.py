"""Mode, effort and model naming, split out of formatting.py.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from bot import config
from bot.claude.types import InstanceOrigin

if TYPE_CHECKING:
    from bot.store.state import StateStore

# --- Mode Display ---

MODE_DISPLAY: dict[str, str] = {
    "explore": "Explore",
    "plan":    "Plan",
    "build":   "Build",
}

# Embed sidebar colors per mode
MODE_COLOR: dict[str, int] = {
    "explore": 0x95a5a6,  # gray
    "plan":    0x5865F2,  # blurple/blue
    "build":   0x57F287,  # green
}

VALID_MODES = frozenset(MODE_DISPLAY)

# Mode cycle order for the toggle button
_NEXT_MODE: dict[str, str] = {"explore": "plan", "plan": "build", "build": "explore"}

# Origins where mode toggle button should NOT appear (user is in a workflow).
# TLDR is here for a different reason than the rest: its instance is always
# clamped to explore, so the toggle would read "Mode: Plan" on every recap
# regardless of the mode the user was actually working in, and tapping it
# would set the thread's mode off the back of a read-only turn.
_WORKFLOW_ORIGINS = frozenset({
    InstanceOrigin.PLAN, InstanceOrigin.BUILD,
    InstanceOrigin.REVIEW_PLAN, InstanceOrigin.REVIEW_CODE,
    InstanceOrigin.COMMIT, InstanceOrigin.DONE,
    InstanceOrigin.APPLY_REVISIONS, InstanceOrigin.RELEASE,
    InstanceOrigin.VERIFY, InstanceOrigin.VERIFY_RELEASE,
    InstanceOrigin.BUILD_AND_SHIP, InstanceOrigin.SENSOR_FIX,
    InstanceOrigin.TLDR,
})


def mode_name(mode: str) -> str:
    """Human-readable mode name."""
    return MODE_DISPLAY.get(mode, mode.capitalize())


def mode_label(mode: str) -> str:
    """Human-readable mode label (alias for mode_name)."""
    return mode_name(mode)


# --- Effort Display ---

EFFORT_DISPLAY: dict[str, str] = {
    "low":    "Low",
    "medium": "Medium",
    "high":   "High",
    "max":    "Max",
}

VALID_EFFORTS = frozenset(EFFORT_DISPLAY)


def effort_name(effort: str) -> str:
    """Human-readable effort name."""
    return EFFORT_DISPLAY.get(effort, effort.capitalize())


# --- Model Selection (/model) ---
#
# Deliberately NO table of model names here. Model names version-bump and get
# renamed ("opus 5" -> "opus 6" -> something else entirely), so a hardcoded
# whitelist would go stale and start rejecting the model the user actually
# wants. Instead:
#   * display goes through short_model_label(), which parses arbitrary shapes
#     generically (vendor prefixes, date stamps, '-latest', numeric tails);
#   * validation is a SHAPE check, not a membership check;
#   * the suggestion list is derived at runtime from this deployment's own
#     config plus the models it has actually run.

# What a model identifier may look like on a command line: CLI aliases
# ("opus"), full API ids ("claude-opus-4-8-20260101"), Bedrock-style
# ("us.anthropic.claude-opus-5-v1:0"). Rejects spaces and shell metacharacters
# so a fat-fingered "/model fable x" can never reach the command line, while
# staying open to names that do not exist yet.
_MODEL_SHAPE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,63}$")

# How many recent instances to mine for model names (see model_suggestions).
_MODEL_HISTORY_SCAN = 200

# Words meaning "stop pinning a model, go back to normal routing". "auto" is
# in here on purpose even though Cursor has a real model by that name: clearing
# the pin there falls through to CURSOR_MODEL, which is "auto" by default, so
# both readings land on the same model.
MODEL_CLEAR_WORDS = frozenset({"default", "clear", "reset", "none", "auto"})


def normalize_model(raw: str) -> str | None:
    """Canonicalize a user-typed model name, or None if it cannot be one.

    Not a whitelist -- any plausibly-shaped identifier passes, including models
    released after this code was written. The CLI stays the real authority on
    whether the name exists; see on_model's soft warning.
    """
    value = raw.strip().lower()
    if not value or not _MODEL_SHAPE.match(value):
        return None
    return value


def model_suggestions(store: "StateStore | None" = None) -> list[str]:
    """Model names to offer in /model autocomplete. Never a closed list.

    Sources, all runtime-derived so the list maintains itself:
      1. ``MODEL_CHOICES`` env override, when a deployment wants explicit control.
      2. Model names this deployment's own settings already reference -- short
         CLI aliases like "opus", listed first because they are what a human
         types and they do not churn between runs.
      3. Model names this bot has actually run (recorded per instance), newest
         first -- usually full API ids, and the best available evidence of what
         the installed CLI accepts.
    An empty list is fine -- free text still works.
    """
    ordered: list[str] = []
    seen: set[str] = set()

    def _add(name: object) -> None:
        if not name:
            return
        canon = normalize_model(str(name))
        if canon and canon not in seen:
            seen.add(canon)
            ordered.append(canon)

    if config.MODEL_CHOICES:
        # Explicit override: still normalized, so a stray "Opus 5 " in .env
        # becomes a usable value instead of one the CLI would reject.
        for choice in config.MODEL_CHOICES:
            _add(choice)
        return ordered

    _add(config.PRIMARY_MODEL)
    _add(config.MODEL_FALLBACK)
    _add(config.BUILD_MODEL)
    _add(config.EXPLORE_MODEL)
    _add(config.DEFAULT_SESSION_MODEL)
    for routed in config.MODEL_ROUTING.values():
        _add(routed)

    if store is not None:
        try:
            # Bounded: this runs per autocomplete keystroke, and the newest
            # slice already covers every model still in rotation.
            for inst in store.list_instances(all_=True)[:_MODEL_HISTORY_SCAN]:
                _add(getattr(inst, "context_model", None))
                _add(getattr(inst, "model", None))
        except Exception:
            pass  # suggestions are a convenience, never a failure path

    return ordered

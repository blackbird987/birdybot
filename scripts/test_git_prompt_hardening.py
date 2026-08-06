"""Regression test for ``bot.procutil.harden_git_env`` and the merge-note reason.

Guards the fix for a week of silent sync failures. Git run as a subprocess
with no terminal does not give up when it needs a credential — it looks for a
graphical askpass helper, and on a KDE desktop it finds ``ksshaskpass`` and
blocks on a dialog nobody is watching until the bot's 30s push timeout fires.
The log then recorded ``Push to origin timed out (30s)``, which names a
network problem that was not happening and hides the credential problem that
was.

The interesting assertion is not that the environment variables are set — it
is that a real ``git`` shelled out with the *broken desktop condition present*
now fails in well under a second and says why. So this drives the actual git
binary against a URL that demands authentication, with ``SSH_ASKPASS``
pointed at a helper on purpose.

Run: python scripts/test_git_prompt_hardening.py
Exit 0 = all pass, exit 1 = failures. Skips the live-git cases (still exit 0)
if git is missing or the network is unreachable, since neither is what this
guards.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from bot.claude.runner import _git_fail_reason  # noqa: E402
from bot.procutil import harden_git_env  # noqa: E402

_failures: list[str] = []
_total = 0


def _check(label: str, cond: bool, detail: str = "") -> None:
    global _total
    _total += 1
    if not cond:
        _failures.append(f"{label}{': ' + detail if detail else ''}")


# --------------------------------------------------------------------------
# 1. The environment, applied over the exact desktop condition that broke us
# --------------------------------------------------------------------------
os.environ["SSH_ASKPASS"] = "/usr/bin/ksshaskpass"
for _var in ("GIT_TERMINAL_PROMPT", "GIT_ASKPASS", "SSH_ASKPASS_REQUIRE"):
    os.environ.pop(_var, None)

harden_git_env()

_check("terminal prompt disabled",
       os.environ.get("GIT_TERMINAL_PROMPT") == "0",
       repr(os.environ.get("GIT_TERMINAL_PROMPT")))
# Empty-but-*set* is the whole trick: git reads GIT_ASKPASS first and only
# falls through to SSH_ASKPASS when GIT_ASKPASS is unset, so an empty value
# both disables the helper and shadows the desktop's. Deleting it instead
# would hand control straight back to ksshaskpass.
_check("askpass set and empty",
       os.environ.get("GIT_ASKPASS") == "",
       repr(os.environ.get("GIT_ASKPASS")))
_check("ssh askpass never",
       os.environ.get("SSH_ASKPASS_REQUIRE") == "never",
       repr(os.environ.get("SSH_ASKPASS_REQUIRE")))
# Safe to call twice — the relaunch path can re-enter startup.
harden_git_env()
_check("idempotent", os.environ.get("GIT_TERMINAL_PROMPT") == "0")


# --------------------------------------------------------------------------
# 2. A real git, against a URL that demands auth: fast failure, named cause
# --------------------------------------------------------------------------
_AUTH_URL = "https://github.com/blackbird987/a-repo-that-does-not-exist-xyz.git"

try:
    _t = time.time()
    _r = subprocess.run(
        ["git", "ls-remote", _AUTH_URL],
        capture_output=True, text=True, timeout=25,
    )
    _elapsed = time.time() - _t
    _err = (_r.stderr or _r.stdout or "").strip()

    if "could not resolve host" in _err.lower() or "unable to access" in _err.lower():
        print("SKIP — network unreachable, live-git cases not run")
    else:
        _check("git failed rather than hung", _r.returncode != 0)
        # The point of the fix. Anything above a second or two means git found
        # a way to ask a human again.
        _check("failed fast", _elapsed < 10,
               f"took {_elapsed:.1f}s — git may have found an askpass again")
        _check("names the cause",
               "terminal prompts disabled" in _err,
               f"stderr was {_err[:200]!r}")
        # And the sentence must survive the trip to Discord.
        _check("reason reaches the merge note",
               "terminal prompts disabled" in _git_fail_reason(_err),
               repr(_git_fail_reason(_err)))
except subprocess.TimeoutExpired:
    _failures.append(
        "git HUNG for 25s with hardening applied — the askpass block regressed"
    )
except FileNotFoundError:
    print("SKIP — git not installed, live-git cases not run")


# --------------------------------------------------------------------------
# 3. The merge note carries a reason, redacted and bounded
# --------------------------------------------------------------------------
_check("first non-blank line wins",
       _git_fail_reason("\n\nfatal: boom\nhint: ignore me") == "fatal: boom",
       repr(_git_fail_reason("\n\nfatal: boom\nhint: ignore me")))
_check("empty in, empty out", _git_fail_reason("") == "")
_check("whitespace in, empty out", _git_fail_reason("\n  \n") == "")
_check("bounded", len(_git_fail_reason("x" * 500)) <= 160,
       str(len(_git_fail_reason("x" * 500))))
# User-facing, and a remote URL can carry an embedded token.
_redacted = _git_fail_reason(
    "fatal: Authentication failed for "
    "'https://user:ghp_AbC123AbC123AbC123AbC123AbC123AbC1@github.com/o/r.git'"
)
_check("token redacted", "ghp_AbC123AbC123AbC123AbC123AbC123AbC1" not in _redacted,
       repr(_redacted))


if _failures:
    print(f"FAIL ({len(_failures)} case(s)):")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)

print(f"OK — {_total} cases passed.")
sys.exit(0)

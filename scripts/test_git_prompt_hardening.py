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
# The askpass helper must be a binary that *exists and answers*, not the real
# ksshaskpass path — on a machine without KDE installed that path is missing,
# git fails fast because it cannot exec it, and the whole suite would pass
# while proving nothing. Pointed at `echo`, a broken shadow means git happily
# takes the prompt text back as the username and fails with an authentication
# error instead of "terminal prompts disabled" — so case 2 below can tell the
# difference between the block working and the block being absent.
_ASKPASS = next((p for p in ("/bin/echo", "/usr/bin/echo") if os.path.exists(p)),
                "/usr/bin/ksshaskpass")
os.environ["SSH_ASKPASS"] = _ASKPASS
# No DISPLAY needed: unlike ssh, git runs an askpass helper whether or not a
# display exists — verified by running it with DISPLAY and WAYLAND_DISPLAY
# both unset. Which is also why the original hang did not need a visible
# desktop to happen.
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

    # Network-only markers. "unable to access" deliberately is NOT one: it is
    # also what a genuine 403 says, so skipping on it would turn the exact
    # regression this guards (shadow broken -> auth attempted -> 403) into a
    # silent skip rather than a failure.
    _offline = any(m in _err.lower() for m in (
        "could not resolve host", "failed to connect",
        "connection timed out", "network is unreachable",
    ))
    if _offline:
        print("SKIP — network unreachable, live-git cases not run")
    else:
        _check("git failed rather than hung", _r.returncode != 0)
        # The point of the fix: seconds, not a block until the 30s timeout.
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
_check("marked line wins",
       _git_fail_reason("\n\nfatal: boom\nhint: ignore me") == "fatal: boom",
       repr(_git_fail_reason("\n\nfatal: boom\nhint: ignore me")))

# Verbatim from a real rejected push (two local clones, one behind the other).
# The regression this pins: taking the *first* line hands Discord
# "To github.com:owner/repo.git", which explains nothing while looking like
# it should. The rejection line names the cause — "(fetch first)".
_REJECTED = (
    "To github.com:owner/repo.git\n"
    " ! [rejected]        master -> master (fetch first)\n"
    "error: failed to push some refs to 'github.com:owner/repo.git'\n"
    "hint: Updates were rejected because the remote contains work that you do not\n"
    "hint: have locally. This is usually caused by another repository pushing to\n"
)
_r_reason = _git_fail_reason(_REJECTED)
_check("rejected push skips the 'To <url>' line",
       not _r_reason.startswith("To "), repr(_r_reason))
_check("rejected push names the cause",
       "rejected" in _r_reason and "fetch first" in _r_reason, repr(_r_reason))
_check("runs of whitespace collapsed",
       "  " not in _r_reason, repr(_r_reason))

# Server-side refusal: the remote's sentence beats the 403 that follows it.
_DENIED = (
    "remote: Permission to owner/repo.git denied to someone.\n"
    "fatal: unable to access 'https://github.com/owner/repo.git/': "
    "The requested URL returned error: 403\n"
)
_check("permission denial prefers the remote's reason",
       _git_fail_reason(_DENIED).startswith("remote: Permission"),
       repr(_git_fail_reason(_DENIED)))

# Server-side hook refusal: the server's progress chatter is prefixed
# "remote:" too, and arrives before the reason. Picking on the prefix alone
# would report a percentage as the cause of the failure.
_HOOK = (
    "remote: Resolving deltas: 100% (1/1), done.\n"
    "remote: error: hook declined to update refs/heads/master\n"
    "To github.com:owner/repo.git\n"
    " ! [remote rejected] master -> master (hook declined)\n"
)
_check("progress chatter is not a diagnosis",
       _git_fail_reason(_HOOK) == "remote: error: hook declined to update refs/heads/master",
       repr(_git_fail_reason(_HOOK)))

# The progress line with no percentage and no trailing "done." — verbatim
# from a real `git fetch --progress`. An earlier filter matched chatter by
# those two symptoms and let this one through, so a failed push reported a
# byte count as its cause. Chatter is now matched as a category.
_STATS = (
    "remote: Enumerating objects: 5, done.\n"
    "remote: Counting objects: 100% (5/5), done.\n"
    "remote: Total 3 (delta 0), reused 0 (delta 0), pack-reused 0 (from 0)\n"
    "remote: error: hook declined to update refs/heads/master\n"
)
_check("transfer statistics are not a diagnosis",
       _git_fail_reason(_STATS)
       == "remote: error: hook declined to update refs/heads/master",
       repr(_git_fail_reason(_STATS)))
# ...and the exclusion must not swallow a real diagnosis that happens to
# quote a percentage or end in "done.".
_check("a fatal line quoting a percentage still wins",
       _git_fail_reason("remote: Counting objects: 100% (5/5), done.\n"
                        "fatal: pack exceeds 100% of quota, done.")
       == "fatal: pack exceeds 100% of quota, done.",
       repr(_git_fail_reason("remote: Counting objects: 100% (5/5), done.\n"
                             "fatal: pack exceeds 100% of quota, done.")))

# hint: is the advice *after* the reason, never the reason itself.
_check("hint is not a diagnosis",
       _git_fail_reason("hint: try harder\nerror: real problem") == "error: real problem",
       repr(_git_fail_reason("hint: try harder\nerror: real problem")))

# Nothing marked at all — fall back rather than return empty.
_check("unmarked output falls back to first line",
       _git_fail_reason("something odd happened\nsecond line") == "something odd happened",
       repr(_git_fail_reason("something odd happened\nsecond line")))
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

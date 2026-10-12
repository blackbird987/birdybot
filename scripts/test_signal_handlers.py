#!/usr/bin/env python3
"""Regression test: who owns which signal in bot/app.py, and one relaunch path.

Three things went wrong here together and are pinned here:

  * ``_emergency_reboot_handler`` was registered for SIGTERM and then replaced
    by the graceful handler registered further down the same function, so the
    first registration never did anything on any platform. SIGTERM is the
    graceful handler's, exactly once; the emergency handler is SIGBREAK only.
  * The emergency handler recorded a running instance's first *message* id
    under ``"channel_id"`` in the reboot note. Nothing can resume into a
    message id, so it now writes no channel at all.
  * Both restart paths wrote ``REBOOT_MSG_FILE`` and spawned
    ``scripts/relaunch.py`` with their own copies of the same code. They share
    ``_write_reboot_msg_and_relaunch`` now, and nothing else in app.py may
    touch either.

The handlers are closures inside ``app.main``, so registration is checked on
the AST; the shared helper is driven for real with ``subprocess.Popen`` and
the note path patched.
"""

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import ast
import json
import logging
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot import app, config  # noqa: E402

_failures: list[str] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"PASS: {name}")
    else:
        print(f"FAIL: {name}" + (f" ({detail})" if detail else ""))
        _failures.append(name)


SRC = (ROOT / "bot" / "app.py").read_text(encoding="utf-8")
TREE = ast.parse(SRC)


def _registrations() -> list[tuple[str, str]]:
    """(signal name, handler name) for every ``signal.signal(...)`` call."""
    out = []
    for node in ast.walk(TREE):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "signal" and len(node.args) == 2):
            continue
        sig, handler = node.args
        if isinstance(sig, ast.Attribute) and isinstance(handler, ast.Name):
            out.append((sig.attr, handler.id))
    return out


def _functions_containing(needle: str) -> set[str]:
    """Names of the innermost functions whose own body text contains needle."""
    hits = set()
    for node in ast.walk(TREE):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            nested = [n for n in ast.walk(node)
                      if n is not node and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            text = ast.get_source_segment(SRC, node) or ""
            for n in nested:
                text = text.replace(ast.get_source_segment(SRC, n) or "", "")
            if needle in text:
                hits.add(node.name)
    return hits


def check_registrations() -> None:
    regs = _registrations()
    sigterm = [h for s, h in regs if s == "SIGTERM"]
    _check("SIGTERM is registered exactly once", len(sigterm) == 1, str(regs))
    _check("SIGTERM goes to the graceful handler",
           sigterm == ["signal_handler"], str(sigterm))
    _check("SIGBREAK goes to the emergency handler",
           ("SIGBREAK", "_emergency_reboot_handler") in regs, str(regs))
    _check("the emergency handler is registered for nothing but SIGBREAK",
           [s for s, h in regs if h == "_emergency_reboot_handler"] == ["SIGBREAK"],
           str(regs))


def check_single_relaunch_path() -> None:
    helper = "_write_reboot_msg_and_relaunch"
    for needle in ("relaunch.py", "REBOOT_MSG_FILE.write_text"):
        owners = _functions_containing(needle)
        _check(f"only the shared helper touches {needle}", owners == {helper},
               str(sorted(owners)))
    callers = _functions_containing(f"{helper}(") - {helper}
    _check("both restart paths call the shared helper",
           callers == {"_emergency_reboot_handler", "_execute_pending_reboots"},
           str(sorted(callers)))
    handler = next(n for n in ast.walk(TREE)
                   if isinstance(n, ast.FunctionDef) and n.name == "_emergency_reboot_handler")
    text = ast.get_source_segment(SRC, handler) or ""
    _check("the emergency note records no channel", '"channel_id"' not in text)
    _check("the emergency handler no longer reads message ids",
           "message_ids" not in text)


def check_helper_behaviour() -> None:
    calls: list[tuple[list, dict]] = []

    class _Popen:
        def __init__(self, argv, **kw):
            calls.append((argv, kw))

    import subprocess
    saved_popen, saved_file = subprocess.Popen, config.REBOOT_MSG_FILE
    with tempfile.TemporaryDirectory() as tmp:
        config.REBOOT_MSG_FILE = Path(tmp) / "reboot_message.json"
        subprocess.Popen = _Popen
        try:
            app._write_reboot_msg_and_relaunch({"message": "m", "resume_prompt": "p"})
            note = json.loads(config.REBOOT_MSG_FILE.read_text(encoding="utf-8"))
            _check("the note is written as given",
                   note == {"message": "m", "resume_prompt": "p"}, str(note))
            _check("the relauncher is spawned once", len(calls) == 1, str(calls))
            argv = calls[0][0] if calls else []
            _check("it runs scripts/relaunch.py against the project root",
                   len(argv) == 3 and argv[1].endswith("relaunch.py")
                   and argv[2] == str(config._PROJECT_ROOT), str(argv))

            # An unwritable note must not cost the relaunch.
            calls.clear()
            config.REBOOT_MSG_FILE = Path(tmp) / "missing-dir" / "x.json"
            app.log.setLevel(logging.ERROR)  # the warning is expected here
            app._write_reboot_msg_and_relaunch({})
            _check("a failed note write still spawns the relauncher", len(calls) == 1)

            # A failed spawn raises, so the /reboot executor can reset itself.
            def _boom(*a, **k):
                raise OSError("no fork")
            subprocess.Popen = _boom
            try:
                app._write_reboot_msg_and_relaunch({})
                raised = False
            except OSError:
                raised = True
            _check("a failed spawn raises to the caller", raised)
        finally:
            app.log.setLevel(logging.NOTSET)
            subprocess.Popen = saved_popen
            config.REBOOT_MSG_FILE = saved_file


def main() -> int:
    check_registrations()
    check_single_relaunch_path()
    check_helper_behaviour()
    if _failures:
        print(f"\n{len(_failures)} check(s) failed")
        return 1
    print("\nAll signal-handler checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

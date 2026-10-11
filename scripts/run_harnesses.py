"""Run every standalone harness in scripts/, one at a time.

Usage::

    python scripts/run_harnesses.py              # everything not opted out
    python scripts/run_harnesses.py -k memory    # only names containing "memory"
    python scripts/run_harnesses.py --list       # show what would run
    python scripts/run_harnesses.py -v           # print output of passes too

What it runs: ``scripts/test_*.py``, ``scripts/verify_*.py`` and
``scripts/check_portability.py``, found by glob. A glob, not a list, is the
point: ``.claude/test.json`` is a hand-kept list and ten harnesses had fallen
off it, two of which had quietly rotted because nothing ran them.

Serially, never in parallel. The machine is memory-bound and several
harnesses spawn subprocesses of their own.

Each harness runs under the install's virtualenv python (the bot's own
``.venv``, found through ``procutil.install_root`` so a build worktree uses
the main checkout's), so ``_bootstrap`` never has to relaunch it.

Pinned config
-------------
``bot/config.py`` loads ``.env`` with ``override=True``, so an environment
variable cannot override a setting for a test run: the file wins. Settings
in ``.env`` that change what a harness asserts are therefore pinned here, by
running each harness through a tiny launcher that imports ``bot.config``,
sets the attributes in ``PINNED_CONFIG`` and then runs the file as
``__main__``. Every reader of these settings reads ``config.X`` at call time,
so the pin is seen everywhere in the process. It does not reach a
subprocess the harness starts itself; a harness that needs a pin there must
pin it itself.

Opted out
---------
``OPT_OUT`` lists harnesses that must not run unattended, with the reason.
Harnesses with a ``--live`` mode are still run, in their default offline
mode, which is what this runner invokes.

Exit code is 0 when everything that ran passed, 1 otherwise.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent

# Harness file stem -> why it is not run here.
OPT_OUT: dict[str, str] = {
    "test_session_cgroups": (
        "creates real systemd scopes and runs daemon-reload; run it by hand"
    ),
}

# config attribute -> value forced for every harness. See the module docstring.
PINNED_CONFIG: dict[str, object] = {
    # The live .env sets a deployment-wide default model, which makes every
    # model look non-primary and switches off the model-limit failover branch
    # that test_model_limit_failover asserts on.
    "DEFAULT_SESSION_MODEL": None,
}

DEFAULT_TIMEOUT_S = 600

_LAUNCHER = """\
import runpy, sys
sys.path.insert(0, {scripts!r})
sys.path.insert(0, {root!r})
import bot.config as _c
for _k, _v in {pins!r}.items():
    setattr(_c, _k, _v)
sys.argv = [{path!r}]
runpy.run_path({path!r}, run_name="__main__")
"""


def _venv_python() -> str:
    """The install's virtualenv interpreter, falling back to this one."""
    candidates = [ROOT]
    try:
        sys.path.insert(0, str(ROOT))
        from bot.procutil import install_root

        candidates.append(install_root(ROOT))
    except Exception:
        pass
    for base in candidates:
        for rel in (".venv/bin/python", ".venv/Scripts/python.exe"):
            p = base / rel
            if p.is_file():
                return str(p)
    return sys.executable


def discover() -> list[Path]:
    found = sorted(SCRIPTS.glob("test_*.py")) + sorted(SCRIPTS.glob("verify_*.py"))
    portability = SCRIPTS / "check_portability.py"
    if portability.is_file():
        found.insert(0, portability)
    return found


def run_one(python: str, path: Path, timeout: int) -> tuple[str, float, str]:
    code = _LAUNCHER.format(
        scripts=str(SCRIPTS), root=str(ROOT), pins=PINNED_CONFIG, path=str(path)
    )
    start = time.monotonic()
    try:
        proc = subprocess.run(
            [python, "-c", code],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") + (exc.stderr or "")
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
        return "TIMEOUT", time.monotonic() - start, out
    elapsed = time.monotonic() - start
    output = (proc.stdout or "") + (proc.stderr or "")
    return ("PASS" if proc.returncode == 0 else f"FAIL rc={proc.returncode}"), elapsed, output


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("-k", dest="pattern", help="only run harnesses whose name contains this")
    ap.add_argument("--list", action="store_true", help="list harnesses and exit")
    ap.add_argument("-v", "--verbose", action="store_true", help="print output of passes too")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S, help="per-file seconds")
    ap.add_argument("--tail", type=int, default=60, help="lines of failing output to show")
    args = ap.parse_args()

    files = discover()
    if args.pattern:
        files = [f for f in files if args.pattern in f.stem]

    if args.list:
        for f in files:
            note = f"  (skipped: {OPT_OUT[f.stem]})" if f.stem in OPT_OUT else ""
            print(f"{f.name}{note}")
        return 0

    python = _venv_python()
    print(f"python: {python}")
    print(f"pinned: {PINNED_CONFIG}")
    results: list[tuple[str, str, float]] = []
    total_start = time.monotonic()
    for f in files:
        if f.stem in OPT_OUT:
            results.append((f.name, "SKIP", 0.0))
            print(f"SKIP  {f.name}  ({OPT_OUT[f.stem]})", flush=True)
            continue
        status, elapsed, output = run_one(python, f, args.timeout)
        results.append((f.name, status, elapsed))
        print(f"{status:<5} {f.name}  {elapsed:.1f}s", flush=True)
        if status != "PASS" or args.verbose:
            lines = output.rstrip().splitlines()
            shown = lines if args.verbose and status == "PASS" else lines[-args.tail:]
            for line in shown:
                print(f"    | {line}")

    passed = sum(1 for _, s, _ in results if s == "PASS")
    skipped = sum(1 for _, s, _ in results if s == "SKIP")
    failed = [(n, s) for n, s, _ in results if s not in ("PASS", "SKIP")]
    print()
    print(
        f"{passed} passed, {len(failed)} failed, {skipped} skipped "
        f"in {time.monotonic() - total_start:.0f}s"
    )
    for name, status in failed:
        print(f"  {status}: {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

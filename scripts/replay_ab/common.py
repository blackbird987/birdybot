"""Paths and small helpers shared by the replay A/B harness.

Nothing here imports ``bot.config``. That module loads the live ``.env``,
resolves ``DATA_DIR`` against the caller's cwd and creates directories there,
so importing it in-process from this harness would point it at whatever the
harness was launched from. The one place that needs it (``prompt.py``) runs
it in a child process with a scratch cwd instead.

``bot.procutil`` and ``bot.claude.session_fork`` are stdlib-only and are
imported directly.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHECKOUT = HERE.parents[1]
if str(CHECKOUT) not in sys.path:
    sys.path.insert(0, str(CHECKOUT))

from bot.procutil import install_root  # noqa: E402

LIVE_ROOT = install_root(CHECKOUT)
LIVE_DATA = LIVE_ROOT / "data"
# A hyphen, not an underscore: see sandbox.check_clone_path.
CACHE_ROOT = Path(os.environ.get("REPLAY_AB_HOME", Path.home() / ".cache" / "replay-ab"))
RUNS_ROOT = CACHE_ROOT / "runs"


def venv_python() -> str:
    """The install's virtualenv python, so a child has the bot's dependencies."""
    cand = LIVE_ROOT / ".venv" / "bin" / "python"
    return str(cand) if cand.exists() else sys.executable


# Commits the harness makes (an arm's setup, the end-of-run snapshot) are
# authored as this, passed per call rather than set on os.environ: the runs
# score on worker threads, and a process-wide env swap is not thread-safe.
GIT_ENV = {
    "GIT_AUTHOR_NAME": "replay-ab", "GIT_AUTHOR_EMAIL": "replay-ab@localhost",
    "GIT_COMMITTER_NAME": "replay-ab", "GIT_COMMITTER_EMAIL": "replay-ab@localhost",
}


def git(cwd: Path | str, *args: str, check: bool = True, timeout: int = 120,
        strip: bool = True, identity: bool = False) -> str:
    """Run git. ``strip=False`` returns stdout verbatim, for file bodies."""
    out = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        timeout=timeout, env={**os.environ, **GIT_ENV} if identity else None,
    )
    if check and out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {cwd}: {out.stderr.strip()}")
    return out.stdout.strip() if strip else out.stdout


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, sort_keys=True, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def assert_outside_live_data(path: Path) -> None:
    """Every write this harness makes must stay out of the live bot's data/."""
    if under(path, LIVE_DATA):
        raise RuntimeError(f"refusing to write under the live data dir: {path}")

"""The system prompt and CLI command line for one arm, built by the bot's own code.

A replay is only a fair test of a prompt change if everything that is not
the change is what the bot would really have sent. So this does not copy the
prompt text: it calls the real ``PromptMixin._build_system_prompt`` and the
real ``_ClaudeProvider.build_command`` on a stand-in ``Instance`` pointing at
the sandbox, and an arm differs only in what it overrides on ``bot.config``
before the call.

Both run in a child process (``--child``) with a scratch cwd and
``BOT_PATHS_DISABLED=1``. ``bot.config`` loads the live ``.env``, creates
``DATA_DIR`` relative to the cwd and, through ``bot.paths``, writes a marker
under the install's ``data/``; in this process any of that would land in the
live bot's tree.

Two blocks are removed from every arm, because both leak the future:

  * Recent Sessions lists finished sessions, including the one being replayed
    and its summary of what it built.
  * Recent master activity is computed from git. The build prompt already
    carries the snapshot that was true at the time, verbatim, and a fresh one
    would be computed against the sandbox and read differently.

Sibling sessions are never passed (``sibling_context=None``).

An arm is a dict:

  name            label used in the run directory and the report
  model, effort   CLI flags; default to what the original run used
  drop_blocks     ``bot.config`` attribute names set to "" before building
  replace_blocks  {attribute: path to a text file holding its new value}
  config          {attribute: value} set verbatim (numbers, flags)
  files           {repo-relative path: source file} overlaid on the clone as
                  a setup commit, e.g. a trimmed ``CLAUDE.md``
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHECKOUT = HERE.parents[1]

ALWAYS_DROPPED_MARKERS = ("--- Recent Sessions", "--- Sibling Sessions", "--- Recent master activity")


def load_arm(spec: str | dict | None) -> dict:
    """An arm from a dict, a JSON file path, or the name ``current``."""
    if spec is None or spec == "current":
        return {"name": "current"}
    if isinstance(spec, dict):
        arm = dict(spec)
    else:
        path = Path(spec)
        arm = json.loads(path.read_text(encoding="utf-8"))
        base = path.parent
        # Relative file references in an arm file are relative to that file.
        for key in ("replace_blocks", "files"):
            arm[key] = {k: str((base / v).resolve()) for k, v in (arm.get(key) or {}).items()}
        arm.setdefault("name", path.stem)
    arm.setdefault("name", "arm")
    # The name becomes a path segment of the clone; see sandbox.check_clone_path.
    arm["name"] = re.sub(r"[^A-Za-z0-9.-]+", "-", str(arm["name"])).strip("-") or "arm"
    return arm


def build(arm: dict, *, case, sandbox_repo: Path, session_id: str | None,
          system_prompt_file: Path) -> dict:
    """Run the child and return {"system_prompt": str, "cmd": [...]}."""
    spec = {
        "arm": arm,
        "instance": {
            "id": f"replay-{case.instance_id}",
            "prompt": case.prompt,
            "repo_path": str(sandbox_repo),
            "session_id": session_id,
            "origin": case.origin or "build",
            "mode": case.mode or "build",
            "effort": arm.get("effort") or case.effort or "high",
            "model": arm.get("model") or case.model,
        },
        "system_prompt_file": str(system_prompt_file),
    }
    with tempfile.TemporaryDirectory(prefix="replay_ab_prompt_") as scratch:
        spec_path = Path(scratch) / "spec.json"
        out_path = Path(scratch) / "out.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        env = {**os.environ, "BOT_PATHS_DISABLED": "1"}
        proc = subprocess.run(
            [_python(), str(Path(__file__).resolve()), "--child", str(spec_path), str(out_path)],
            cwd=scratch, env=env, capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"prompt child failed ({proc.returncode}): {proc.stderr[-2000:]}")
        out = json.loads(out_path.read_text(encoding="utf-8"))
    system_prompt_file.write_text(out["system_prompt"], encoding="utf-8")
    return out


def _python() -> str:
    sys.path.insert(0, str(HERE))
    from common import venv_python
    return venv_python()


# --------------------------------------------------------------------------
# Child side. Everything below runs with cwd = scratch dir.
# --------------------------------------------------------------------------

def _child(spec_path: str, out_path: str) -> int:
    sys.path.insert(0, str(CHECKOUT))
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    arm = spec["arm"]

    from bot import config
    from bot.claude.provider import _CLAUDE
    from bot.claude.runner.prompt_build import PromptMixin
    from bot.claude.types import Instance, InstanceOrigin, InstanceStatus, InstanceType
    from bot.store import history as history_mod

    for name in arm.get("drop_blocks") or []:
        if not hasattr(config, name):
            raise SystemExit(f"arm drops unknown config block {name!r}")
        setattr(config, name, "")
    for name, path in (arm.get("replace_blocks") or {}).items():
        if not hasattr(config, name):
            raise SystemExit(f"arm replaces unknown config block {name!r}")
        setattr(config, name, Path(path).read_text(encoding="utf-8"))
    for name, value in (arm.get("config") or {}).items():
        if not hasattr(config, name):
            raise SystemExit(f"arm sets unknown config value {name!r}")
        setattr(config, name, value)

    history_mod.render_recent_block = lambda **_kw: ""

    class _Builder(PromptMixin):
        provider = _CLAUDE

        @staticmethod
        def _build_master_context_block(instance):  # noqa: ANN001, ANN205
            return ""

    i = spec["instance"]
    inst = Instance(
        id=i["id"], name=None, instance_type=InstanceType.TASK,
        prompt=i["prompt"], repo_name="bot", repo_path=i["repo_path"],
        status=InstanceStatus.RUNNING, session_id=i["session_id"],
        mode=i["mode"], branch="replay", original_branch="master",
        worktree_path=i["repo_path"], origin=InstanceOrigin(i["origin"]),
        effort=i["effort"], model=i["model"],
    )
    system_prompt = _Builder()._build_system_prompt(inst, context=None, sibling_context=None)
    cmd = _CLAUDE.build_command(
        inst, binary=config.CLAUDE_BINARY,
        system_prompt_file=spec["system_prompt_file"], system_prompt_inline=None,
        api_fallback=False, api_key_file=None,
    )
    Path(out_path).write_text(json.dumps({"system_prompt": system_prompt, "cmd": cmd}),
                              encoding="utf-8")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--child":
        sys.exit(_child(sys.argv[2], sys.argv[3]))
    print("usage: prompt.py --child <spec.json> <out.json>  (internal)", file=sys.stderr)
    sys.exit(2)

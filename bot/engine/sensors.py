"""Computational sensors — deterministic post-build checks (compiler/linter).

Feedback half of the agent harness: after a chain build, run fast
language-appropriate tools (dotnet build / ruff / tsc) in the build worktree
and surface their raw output so the build session can self-correct before the
inferential code-review step. Configured per repo via ``.claude/sensors.json``;
falls back to stack auto-detection when absent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# Merged stdout+stderr kept per sensor — tail-capped because compiler/linter
# error summaries cluster at the end of output.
_OUTPUT_CAP = 3500

# Directories never scanned during stack detection (worktrees nest the repo,
# node_modules/.git are noise).
_DETECT_SKIP_DIRS = frozenset({".worktrees", "node_modules", ".git"})

_DEFAULT_POLICY = "block"
_DEFAULT_MAX_FIX_ROUNDS = 2
_DEFAULT_TIMEOUT_S = 180


@dataclass
class SensorResult:
    name: str
    command: str
    status: str  # "pass" | "fail" | "skipped" | "timeout" | "error"
    exit_code: int | None = None
    output: str = ""
    duration_s: float = 0.0
    blocking: bool = True

    _STATUS_MARKS = {
        "pass": "✓", "fail": "✗", "timeout": "⏱", "error": "!",
    }

    @property
    def mark(self) -> str:
        return self._STATUS_MARKS.get(self.status, "·")


@dataclass
class SensorReport:
    results: list[SensorResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True when no blocking sensor failed (skips don't count against)."""
        return not any(
            r.blocking and r.status in ("fail", "timeout", "error")
            for r in self.results
        )

    def failures(self) -> list[SensorResult]:
        return [
            r for r in self.results
            if r.blocking and r.status in ("fail", "timeout", "error")
        ]

    def summary_line(self) -> str:
        """Compact one-liner, e.g. ``Sensors: ruff ✓ · tsc ✗ (dotnet skipped)``."""
        active = [r for r in self.results if r.status != "skipped"]
        skipped = [r for r in self.results if r.status == "skipped"]
        parts = [f"{r.name} {r.mark}" for r in active]
        line = "Sensors: " + (" · ".join(parts) if parts else "none ran")
        if skipped:
            line += " (" + ", ".join(f"{r.name} skipped" for r in skipped) + ")"
        return line

    def failure_text(self) -> str:
        """LLM-consumable failure block: command run + raw captured output."""
        blocks: list[str] = []
        for r in self.failures():
            header = f"## Sensor `{r.name}` — {r.status}"
            if r.exit_code is not None:
                header += f" (exit {r.exit_code})"
            body = r.output.strip() or "(no output captured)"
            blocks.append(
                f"{header}\nCommand: `{r.command}`\n```\n{body}\n```"
            )
        return "\n\n".join(blocks)


def detect_stacks(repo_path: str | Path) -> list[str]:
    """Detect language stacks from marker files. Never raises."""
    root = Path(repo_path)
    stacks: list[str] = []
    try:
        if not root.is_dir():
            return stacks
        if _has_dotnet_markers(root):
            stacks.append("dotnet")
        if any((root / f).exists() for f in
               ("pyproject.toml", "requirements.txt", "setup.py")):
            stacks.append("python")
        if (root / "package.json").exists():
            stacks.append("node")
        if (root / "tsconfig.json").exists():
            stacks.append("typescript")
    except OSError:
        log.debug("Stack detection failed for %s", repo_path, exc_info=True)
    return stacks


def _has_dotnet_markers(root: Path, max_depth: int = 3) -> bool:
    """Any *.sln / *.csproj within depth, skipping worktrees/node_modules/.git."""
    def scan(d: Path, depth: int) -> bool:
        try:
            entries = list(d.iterdir())
        except OSError:
            return False
        for e in entries:
            if e.is_file() and e.suffix in (".sln", ".csproj"):
                return True
        if depth >= max_depth:
            return False
        for e in entries:
            if e.is_dir() and e.name not in _DETECT_SKIP_DIRS:
                if scan(e, depth + 1):
                    return True
        return False

    return scan(root, 1)


def _default_sensors(stacks: list[str]) -> list[dict]:
    # "auto": True marks these for the tool-availability pre-check (skip
    # quietly when the tool isn't installed). User-authored sensors.json
    # commands never get that check — they may start with shell builtins or
    # compound syntax (`cd x && ...`) that which() can't resolve, and an
    # explicitly configured sensor that can't launch should fail loudly.
    sensors: list[dict] = []
    if "dotnet" in stacks:
        # 600s: a fresh worktree may need a full NuGet restore.
        sensors.append({
            "name": "dotnet build",
            "command": "dotnet build --nologo -v q",
            "timeout_s": 600,
            "auto": True,
        })
    if "python" in stacks:
        # ruff only if installed — no compileall fallback (walks venvs).
        # Critical-errors-only selection (syntax errors, invalid constructs,
        # undefined names): a repo that never adopted ruff must not fail its
        # chains on pre-existing style noise. Widen via .claude/sensors.json.
        sensors.append({
            "name": "ruff",
            "command": "ruff check --select E9,F63,F7,F82 .",
            "timeout_s": 180,
            "auto": True,
        })
    if "typescript" in stacks:
        sensors.append({
            "name": "tsc",
            "command": "npx tsc --noEmit",
            "timeout_s": 300,
            "auto": True,
        })
    return sensors


def _safe_pos_int(value, default: int) -> int:
    try:
        n = int(value)
        return n if n > 0 else default
    except (TypeError, ValueError):
        return default


def load_sensor_config(repo_path: str | Path | None) -> dict:
    """Read ``.claude/sensors.json`` from the main repo path.

    Returns ``{"sensors": list[dict] | None, "policy": str, "max_fix_rounds": int}``.
    ``sensors`` is None when the file is absent/malformed/empty — callers then
    fall back to stack auto-detection. Mirrors load_workflow_policy guardrails.
    """
    cfg = {
        "sensors": None,
        "policy": _DEFAULT_POLICY,
        "max_fix_rounds": _DEFAULT_MAX_FIX_ROUNDS,
    }
    if not repo_path:
        return cfg
    path = Path(repo_path) / ".claude" / "sensors.json"
    if not path.exists():
        return cfg
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        sensors = raw.get("sensors")
        if isinstance(sensors, list) and sensors:
            valid = [
                s for s in sensors
                if isinstance(s, dict) and s.get("command")
            ]
            if valid:
                cfg["sensors"] = valid
        policy = raw.get("policy", _DEFAULT_POLICY)
        cfg["policy"] = policy if policy in ("block", "warn") else _DEFAULT_POLICY
        # 0 is meaningful here (report/halt immediately, no fix rounds), so
        # don't route through _safe_pos_int, which treats 0 as invalid.
        try:
            rounds = int(raw.get("max_fix_rounds", _DEFAULT_MAX_FIX_ROUNDS))
        except (TypeError, ValueError):
            rounds = _DEFAULT_MAX_FIX_ROUNDS
        cfg["max_fix_rounds"] = min(max(rounds, 0), 3)
    except Exception:
        log.debug("Malformed .claude/sensors.json at %s", repo_path, exc_info=True)
    return cfg


def _first_token(command: str) -> str | None:
    """First shell token of a command, or None if it has none."""
    try:
        return shlex.split(command, posix=False)[0].strip('"') or None
    except (ValueError, IndexError):
        return None


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill a timed-out sensor and its children, then reap it.

    create_subprocess_shell wraps the command in a shell, so ``proc.kill()``
    alone would orphan the actual tool (e.g. a hung msbuild). On Windows,
    ``taskkill /T`` takes the whole tree down. Reaping prevents the pipe
    transport from leaking in the long-running bot.
    """
    try:
        if sys.platform == "win32" and proc.pid:
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/T", "/F", "/PID", str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=10)
        elif proc.pid:
            # start_new_session=True made the shell a group leader; killing
            # the group takes the shell's children (the actual tool) with it.
            import os
            import signal
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
    except (asyncio.TimeoutError, ProcessLookupError, OSError):
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except (asyncio.TimeoutError, ProcessLookupError):
        pass


async def _run_one(
    spec: dict, cwd: str, budget_left: float,
) -> SensorResult:
    name = str(spec.get("name") or spec.get("command", "sensor"))
    command = str(spec.get("command", ""))
    blocking = bool(spec.get("blocking", True))
    timeout_s = min(
        float(_safe_pos_int(spec.get("timeout_s"), _DEFAULT_TIMEOUT_S)),
        max(budget_left, 1.0),
    )

    tool = _first_token(command)
    if tool is None:
        return SensorResult(
            name=name, command=command, status="skipped",
            output="skipped (empty command)", blocking=blocking,
        )
    # Availability pre-check only for auto-detected sensors: their optional
    # tools should skip quietly. Custom sensors.json commands always run —
    # a launch failure there is real, actionable feedback.
    if spec.get("auto") and shutil.which(tool) is None:
        return SensorResult(
            name=name, command=command, status="skipped",
            output=f"skipped ({tool} not installed)",
            blocking=blocking,
        )

    start = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            # POSIX: own process group so a timeout can kill the whole tree
            # (Windows uses taskkill /T instead — see _kill_tree).
            start_new_session=(sys.platform != "win32"),
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            await _kill_tree(proc)
            return SensorResult(
                name=name, command=command, status="timeout",
                output=f"timed out after {int(timeout_s)}s",
                duration_s=time.monotonic() - start, blocking=blocking,
            )
        text = (out or b"").decode("utf-8", errors="replace")
        if len(text) > _OUTPUT_CAP:
            text = "…(truncated)…\n" + text[-_OUTPUT_CAP:]
        status = "pass" if proc.returncode == 0 else "fail"
        return SensorResult(
            name=name, command=command, status=status,
            exit_code=proc.returncode, output=text,
            duration_s=time.monotonic() - start, blocking=blocking,
        )
    except Exception as exc:  # never let a sensor take down the chain
        log.debug("Sensor %r errored", name, exc_info=True)
        return SensorResult(
            name=name, command=command, status="error",
            output=f"{type(exc).__name__}: {exc}",
            duration_s=time.monotonic() - start, blocking=blocking,
        )


async def run_sensors(
    cwd: str, repo_path: str | None, total_budget_s: float | None = None,
) -> SensorReport:
    """Run all configured sensors sequentially in ``cwd``. Never raises.

    Sequential on purpose: sensors may compete for the same build/obj dirs.
    ``total_budget_s`` caps wall time across all sensors; sensors that don't
    fit the remaining budget are marked skipped.
    """
    from bot import config

    if total_budget_s is None:
        total_budget_s = float(config.SENSOR_TOTAL_BUDGET_SECS)

    cfg = load_sensor_config(repo_path)
    specs = cfg["sensors"]
    if specs is None:
        specs = _default_sensors(detect_stacks(cwd))

    report = SensorReport()
    deadline = time.monotonic() + total_budget_s
    for spec in specs:
        budget_left = deadline - time.monotonic()
        if budget_left <= 1.0:
            report.results.append(SensorResult(
                name=str(spec.get("name") or spec.get("command", "sensor")),
                command=str(spec.get("command", "")),
                status="skipped", output="skipped (budget exhausted)",
                blocking=bool(spec.get("blocking", True)),
            ))
            continue
        try:
            report.results.append(await _run_one(spec, cwd, budget_left))
        except Exception as exc:  # uphold the never-raise contract
            log.warning("Sensor spec %r crashed the runner: %s", spec, exc)
            report.results.append(SensorResult(
                name=str(spec.get("name") or "sensor"),
                command=str(spec.get("command", "")),
                status="error", output=f"{type(exc).__name__}: {exc}",
                blocking=bool(spec.get("blocking", True)),
            ))
    return report

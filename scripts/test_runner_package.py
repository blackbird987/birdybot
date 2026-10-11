#!/usr/bin/env python3
"""Harness for the shape of the bot.claude.runner package.

runner.py was one 9,400-line module; it is a package of mixins now. Four
things keep the split honest:

- A patch on ``bot.claude.runner.<name>`` succeeds and changes nothing, since
  the package only re-exports. A harness that patches there is green and
  blind, so any such patch fails here. Patch ``bot.config`` itself for a
  knob: it is one shared module object, so that reaches every reader.
- Every name imported from the runner anywhere in ``bot/`` or ``scripts/``
  still resolves, so the old import paths cannot rot one at a time.
- Leaf modules never import ``core`` or the package itself, which is what
  keeps the import graph acyclic.
- No method is defined in two mixins. The MRO would pick one silently.
"""
from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import ast
import inspect
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bot.claude.runner as runner_pkg  # noqa: E402
from bot.claude.runner import ClaudeRunner  # noqa: E402

PKG = "bot.claude.runner"
PKG_DIR = ROOT / "bot" / "claude" / "runner"
FAILS: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


def _sources():
    for base in (ROOT / "bot", ROOT / "scripts"):
        for path in sorted(base.rglob("*.py")):
            try:
                yield path, ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue


def _package_aliases(tree: ast.Module) -> set[str]:
    """Local names bound to the package itself in one file."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "bot.claude":
            out |= {a.asname or a.name for a in node.names if a.name == "runner"}
        elif isinstance(node, ast.Import):
            out |= {a.asname for a in node.names if a.name == PKG and a.asname}
    return out


def main() -> int:
    blind: list[str] = []
    unresolved: list[str] = []
    for path, tree in _sources():
        rel = path.relative_to(ROOT)
        aliases = _package_aliases(tree)
        for node in ast.walk(tree):
            # from bot.claude.runner import X
            if isinstance(node, ast.ImportFrom) and node.module == PKG:
                for a in node.names:
                    if not hasattr(runner_pkg, a.name):
                        unresolved.append(f"{rel}: {a.name}")
            # runner_mod.X = ...  /  runner_mod.X.y = ...
            targets = node.targets if isinstance(node, ast.Assign) else []
            for t in targets:
                base = t
                while isinstance(base, ast.Attribute) and isinstance(base.value, ast.Attribute):
                    base = base.value
                if (isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name)
                        and base.value.id in aliases):
                    attr = getattr(runner_pkg, base.attr, None)
                    if not isinstance(attr, types.ModuleType) or base.attr in {
                            "tempfile", "subprocess", "os", "shutil"}:
                        blind.append(f"{rel}:{node.lineno} {ast.unparse(t)}")
            # patch("bot.claude.runner.X") / patch.object(runner_mod, "X")
            if isinstance(node, ast.Call):
                fn = ast.unparse(node.func)
                if fn.endswith("patch") and node.args and isinstance(node.args[0], ast.Constant):
                    target = str(node.args[0].value)
                    if target.startswith(PKG + "."):
                        head = target[len(PKG) + 1:].split(".")[0]
                        if not (PKG_DIR / f"{head}.py").exists():
                            blind.append(f"{rel}:{node.lineno} patch({target!r})")
                if (fn.endswith("patch.object") and node.args
                        and isinstance(node.args[0], ast.Name) and node.args[0].id in aliases):
                    blind.append(f"{rel}:{node.lineno} {ast.unparse(node)}")
            # runner_mod.X read: must still exist
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id in aliases and not hasattr(runner_pkg, node.attr)):
                unresolved.append(f"{rel}:{node.lineno} {node.value.id}.{node.attr}")

    check("no harness patches a name on the re-export package", not blind, "; ".join(blind))
    check("every name imported from the runner still resolves", not unresolved,
          "; ".join(unresolved))

    leaf_bad = []
    for path in sorted(PKG_DIR.glob("*.py")):
        if path.stem in ("__init__", "core"):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module in (PKG, f"{PKG}.core"):
                leaf_bad.append(f"{path.name} imports {node.module}")
            if (isinstance(node, ast.ImportFrom) and node.module == "bot.claude"
                    and any(a.name == "runner" for a in node.names)):
                leaf_bad.append(f"{path.name} imports the package")
    check("leaf modules never import core or the package", not leaf_bad, "; ".join(leaf_bad))

    seen: dict[str, str] = {}
    dupes = []
    for cls in ClaudeRunner.__mro__[:-1]:
        for name, val in vars(cls).items():
            if inspect.isfunction(val) or isinstance(val, (staticmethod, classmethod, property)):
                if name in seen and not name.startswith("__"):
                    dupes.append(f"{name}: {seen[name]} and {cls.__name__}")
                seen.setdefault(name, cls.__name__)
    check("no method is defined in two places along the MRO", not dupes, "; ".join(dupes))
    check("the runner is assembled from its mixins", len(ClaudeRunner.__mro__) >= 8,
          str([c.__name__ for c in ClaudeRunner.__mro__]))

    # The guard must be able to see a blind patch, or it proves nothing.
    probe = ast.parse("from bot.claude import runner as rm\nrm.unusable_reason = None\n")
    aliases = _package_aliases(probe)
    t = probe.body[1].targets[0]
    check("self-test: a patch on the package is recognised",
          aliases == {"rm"} and t.value.id in aliases and not hasattr(runner_pkg, "unusable_reason"))

    print()
    print(f"{len(FAILS)} failure(s)" if FAILS else "All checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())

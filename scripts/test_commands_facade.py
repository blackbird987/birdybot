#!/usr/bin/env python3
"""Harness for the shape of the bot.engine command modules.

commands.py was one 4,200-line module; it re-exports from a dozen focused
modules now (query, directives/, repo_cmds, callbacks, ...). Four things keep
the split honest:

- A patch on ``bot.engine.commands.<name>`` reaches only callers that look the
  name up through the facade. The Discord layer and auto_fix call
  ``commands.on_text`` and ``commands.handle_callback`` that way, so those two
  may be patched there. Any other facade patch is green and blind: patch the
  module that reads the name instead (``query.check_budget``,
  ``directives.dispatch.on_repo``).
- Every name imported or read from the facade anywhere in ``bot/`` or
  ``scripts/`` still resolves.
- No command module imports the facade, and ``cmd_state`` imports no command
  module, which keeps the graph acyclic.
- The two facade-patchable names are never imported by name inside the
  engine, or a facade patch would silently miss that caller.
"""
from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bot.engine.commands as facade  # noqa: E402

FACADE = "bot.engine.commands"
FACADE_PATCHABLE = {"on_text", "handle_callback"}
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


def _command_modules() -> list[str]:
    tree = ast.parse((ROOT / "bot" / "engine" / "commands.py").read_text(encoding="utf-8"))
    return sorted({n.module for n in tree.body
                   if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("bot.engine.")})


def _facade_aliases(tree: ast.Module) -> set[str]:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "bot.engine":
            out |= {a.asname or a.name for a in node.names if a.name == "commands"}
        elif isinstance(node, ast.Import):
            out |= {a.asname for a in node.names if a.name == FACADE and a.asname}
    return out


def _patched_names(tree: ast.Module, aliases: set[str]) -> list[tuple[int, str]]:
    """Every name a file assigns or patches on the facade."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                elts = t.elts if isinstance(t, ast.Tuple) else [t]
                for e in elts:
                    if (isinstance(e, ast.Attribute) and isinstance(e.value, ast.Name)
                            and e.value.id in aliases):
                        out.append((node.lineno, e.attr))
        # (commands, "name", value) triples fed to setattr
        if (isinstance(node, ast.Tuple) and len(node.elts) >= 2
                and isinstance(node.elts[0], ast.Name) and node.elts[0].id in aliases
                and isinstance(node.elts[1], ast.Constant) and isinstance(node.elts[1].value, str)):
            out.append((node.lineno, node.elts[1].value))
        if isinstance(node, ast.Call):
            fn = ast.unparse(node.func)
            if (fn.endswith(("patch.object", "setattr")) and len(node.args) >= 2
                    and isinstance(node.args[0], ast.Name) and node.args[0].id in aliases
                    and isinstance(node.args[1], ast.Constant)):
                out.append((node.lineno, str(node.args[1].value)))
            if (fn.endswith("patch") and node.args and isinstance(node.args[0], ast.Constant)
                    and str(node.args[0].value).startswith(FACADE + ".")):
                out.append((node.lineno, str(node.args[0].value)[len(FACADE) + 1:]))
    return out


def main() -> int:
    blind, unresolved = [], []
    for path, tree in _sources():
        rel = path.relative_to(ROOT)
        aliases = _facade_aliases(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == FACADE:
                unresolved += [f"{rel}: {a.name}" for a in node.names if not hasattr(facade, a.name)]
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id in aliases and not hasattr(facade, node.attr)
                    and node.attr != "__name__"):
                unresolved.append(f"{rel}:{node.lineno} {node.value.id}.{node.attr}")
        blind += [f"{rel}:{ln} {nm}" for ln, nm in _patched_names(tree, aliases)
                  if nm not in FACADE_PATCHABLE]
    check("no harness patches a facade name that is read elsewhere", not blind, "; ".join(blind))
    check("every name read from the facade still resolves", not unresolved, "; ".join(unresolved))

    mods = _command_modules()
    check("the facade re-exports from the split modules", len(mods) >= 10, str(mods))
    cycle, leaf, by_name = [], [], []
    for mod in mods:
        tree = ast.parse((ROOT / Path(*mod.split("."))).with_suffix(".py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            names = {a.name for a in node.names}
            if node.module == FACADE or (node.module == "bot.engine" and "commands" in names):
                cycle.append(mod)
            if mod == "bot.engine.cmd_state" and node.module in mods:
                leaf.append(node.module)
    for path, tree in _sources():
        if not str(path.relative_to(ROOT)).startswith("bot/engine"):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in mods:
                hit = {a.name for a in node.names} & FACADE_PATCHABLE
                if hit and path.name != "commands.py":
                    by_name.append(f"{path.name}: {sorted(hit)}")
    check("no command module imports the facade", not cycle, str(cycle))
    check("cmd_state imports no command module", not leaf, str(leaf))
    check("facade-patchable names are only looked up through the facade", not by_name,
          "; ".join(by_name))

    probe = ast.parse("from bot.engine import commands as c\nc.check_budget = None\n"
                      "pairs = [(c, 'budget_warning', 1)]\n")
    names = {nm for _, nm in _patched_names(probe, _facade_aliases(probe))}
    check("self-test: an assignment and a setattr triple are both recognised",
          names == {"check_budget", "budget_warning"}, str(names))

    print()
    print(f"{len(FAILS)} failure(s)" if FAILS else "All checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())

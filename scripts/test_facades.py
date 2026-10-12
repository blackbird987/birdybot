#!/usr/bin/env python3
"""Harness for the shape of the two re-export facades in bot.engine.

commands.py (4,200 lines) and workflows.py (3,600 lines) were each one module;
they now re-export from focused modules (query, directives/, callbacks, ...;
steps, chain_runner, step_spawn, step_parse, ...). Four things keep each split
honest:

- A patch on ``<facade>.<name>`` reaches only callers that look the name up
  through the facade. The Discord layer and auto_fix call
  ``commands.on_text`` and ``commands.handle_callback`` that way, and
  auto_fix and the /chain directive call ``workflows.on_autopilot`` and
  ``workflows.on_conversational_chain`` that way, and the button table and
  /tldr both read ``workflows.on_tldr`` off the facade at call time, so those
  may be patched on their facade. Any other facade patch is green and blind: patch the module
  that reads the name instead (``query.check_budget``,
  ``chain_runner.spawn_from``).
- Every name imported or read from a facade anywhere in ``bot/`` or
  ``scripts/`` still resolves.
- No split module imports its facade, and the leaf modules (``cmd_state``;
  ``step_policy``, ``step_parse``, ``chain_git``) import no sibling, which
  keeps each graph acyclic.
- The facade-patchable names are never imported by name inside the engine,
  or a facade patch would silently miss that caller.
"""
from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import importlib  # noqa: E402

# facade -> (names a patch on it may target, leaf modules that import no sibling)
FACADES = {
    "bot.engine.commands": ({"on_text", "handle_callback"}, {"bot.engine.cmd_state"}),
    "bot.engine.workflows": ({"on_autopilot", "on_conversational_chain", "on_tldr"},
                             {"bot.engine.step_policy", "bot.engine.step_parse", "bot.engine.chain_git"}),
}
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


def _split_modules(facade: str) -> list[str]:
    path = (ROOT / Path(*facade.split("."))).with_suffix(".py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return sorted({n.module for n in tree.body
                   if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("bot.engine.")})


def _facade_aliases(tree: ast.Module, facade: str) -> set[str]:
    pkg, _, leaf = facade.rpartition(".")
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == pkg:
            out |= {a.asname or a.name for a in node.names if a.name == leaf}
        elif isinstance(node, ast.Import):
            out |= {a.asname for a in node.names if a.name == facade and a.asname}
    return out


def _patched_names(tree: ast.Module, aliases: set[str], facade: str) -> list[tuple[int, str]]:
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
        # (facade, "name", value) triples fed to setattr
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
                    and str(node.args[0].value).startswith(facade + ".")):
                out.append((node.lineno, str(node.args[0].value)[len(facade) + 1:]))
    return out


def check_facade(facade: str, patchable: set[str], leaves: set[str], sources) -> None:
    short = facade.rpartition(".")[2]
    mod = importlib.import_module(facade)
    blind, unresolved = [], []
    for path, tree in sources:
        rel = path.relative_to(ROOT)
        aliases = _facade_aliases(tree, facade)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == facade:
                unresolved += [f"{rel}: {a.name}" for a in node.names if not hasattr(mod, a.name)]
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id in aliases and not hasattr(mod, node.attr)
                    and node.attr != "__name__"):
                unresolved.append(f"{rel}:{node.lineno} {node.value.id}.{node.attr}")
        blind += [f"{rel}:{ln} {nm}" for ln, nm in _patched_names(tree, aliases, facade)
                  if nm not in patchable]
    check(f"[{short}] no harness patches a facade name that is read elsewhere", not blind,
          "; ".join(blind))
    check(f"[{short}] every name read from the facade still resolves", not unresolved,
          "; ".join(unresolved))

    mods = _split_modules(facade)
    check(f"[{short}] the facade re-exports from the split modules", len(mods) >= 5, str(mods))
    check(f"[{short}] every leaf is one of the split modules", leaves <= set(mods), str(leaves - set(mods)))
    pkg, _, leaf_name = facade.rpartition(".")
    cycle, leaf, by_name = [], [], []
    for m in mods:
        tree = ast.parse((ROOT / Path(*m.split("."))).with_suffix(".py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            names = {a.name for a in node.names}
            if node.module == facade or (node.module == pkg and leaf_name in names):
                cycle.append(m)
            if m in leaves and (node.module in mods
                                or (node.module == pkg and names & {x.rpartition(".")[2] for x in mods})):
                leaf.append(f"{m} -> {node.module}")
    for path, tree in sources:
        if not str(path.relative_to(ROOT)).startswith("bot/engine") or path.stem == short:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in mods:
                hit = {a.name for a in node.names} & patchable
                if hit:
                    by_name.append(f"{path.name}: {sorted(hit)}")
    check(f"[{short}] no split module imports the facade", not cycle, str(cycle))
    check(f"[{short}] the leaf modules import no sibling", not leaf, str(leaf))
    check(f"[{short}] facade-patchable names are only looked up through the facade", not by_name,
          "; ".join(by_name))


def main() -> int:
    sources = list(_sources())
    for facade, (patchable, leaves) in FACADES.items():
        check_facade(facade, patchable, leaves, sources)

    probe = ast.parse("from bot.engine import commands as c\nc.check_budget = None\n"
                      "pairs = [(c, 'budget_warning', 1)]\n")
    names = {nm for _, nm in _patched_names(probe, _facade_aliases(probe, "bot.engine.commands"),
                                            "bot.engine.commands")}
    check("self-test: an assignment and a setattr triple are both recognised",
          names == {"check_budget", "budget_warning"}, str(names))
    probe = ast.parse("from bot.engine import workflows\nworkflows.spawn_from = None\n")
    names = {nm for _, nm in _patched_names(probe, _facade_aliases(probe, "bot.engine.workflows"),
                                            "bot.engine.workflows")}
    check("self-test: a workflows facade patch is recognised", names == {"spawn_from"}, str(names))

    print()
    print(f"{len(FAILS)} failure(s)" if FAILS else "All checks passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())

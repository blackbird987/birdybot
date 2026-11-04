"""`/sandbox on|off|hosts|status`: the owner's switch for the session sandbox.

Plain text in, plain text out, so the Discord layer stays a thin caller and the
harness can drive it. The owner check is the caller's. What the switch does is in
docs/design/session-sandbox.md.
"""

from __future__ import annotations

import logging
import os
import shutil

from bot.claude import sandbox

log = logging.getLogger(__name__)


def _repo(store, name: str) -> tuple[str | None, str | None]:
    path = store.list_repos().get(name)
    if not path:
        return None, f"No repo called `{name}`."
    return os.path.realpath(path), None


def turn_on(store, name: str, provider_name: str) -> str:
    real, error = _repo(store, name)
    if error:
        return error
    problem = sandbox.check_repo_allowed(real, provider_name)
    if problem:
        return f"Not sandboxing `{name}`: {problem}."
    missing = [tool for tool in ("bwrap", "socat") if not shutil.which(tool)]
    if missing:
        return f"Not sandboxing `{name}`: {' and '.join(missing)} not installed."
    try:
        sandbox.harden_repo_git(real)
        sandbox.add_stub_excludes(os.path.join(real, ".git"))
    except (sandbox.SandboxRefused, OSError) as exc:
        return f"Not sandboxing `{name}`: {exc}."
    existing = store.sandbox_entries().get(real)
    hosts = existing["hosts"] if isinstance(existing, dict) and isinstance(
        existing.get("hosts"), list) else list(sandbox.DEFAULT_HOSTS)
    # An approval from before the sandbox still stands, for what is on disk today.
    deploy = store.get_deploy_config(name)
    if deploy and deploy.get("approved"):
        deploy["approved_fingerprint"] = sandbox.deploy_fingerprint(real, deploy)
        store.set_deploy_config(name, deploy)
    store.set_sandbox_entry(real, {"hosts": hosts})
    log.info("Session sandbox on for %s (%s)", name, real)
    return (
        f"`{name}` is sandboxed from its next run on. Hosts: {', '.join(hosts) or 'none'}. "
        "Runs already going finish as they started. Change hosts with `/sandbox hosts`."
    )


def turn_off(store, name: str) -> str:
    real, error = _repo(store, name)
    if error:
        return error
    if real not in store.sandbox_entries():
        return f"`{name}` is not sandboxed."
    cleared = sandbox.clear_failed_imports(real)
    sandbox.unharden_repo_git(real)
    store.set_sandbox_entry(real, None)
    log.info("Session sandbox off for %s (%s)", name, real)
    note = f" Cleared {cleared} stuck import(s): those commits are not on their branch." if cleared else ""
    return f"`{name}` is no longer sandboxed, from its next run on.{note}"


def set_hosts(store, name: str, hosts: list[str]) -> str:
    real, error = _repo(store, name)
    if error:
        return error
    entry = store.sandbox_entries().get(real)
    if not isinstance(entry, dict):
        return f"`{name}` is not sandboxed. Turn it on first with `/sandbox on`."
    try:
        cleaned = sandbox.validate_hosts(hosts)
    except ValueError as exc:
        return str(exc)
    store.set_sandbox_entry(real, {**entry, "hosts": cleaned})
    shown = ", ".join(cleaned) or "none"
    return (
        f"`{name}` can reach: {shown}, from its next run on. Any host here can carry "
        "data out, so keep the list short."
    )


def status(store, provider_name: str) -> str:
    entries = store.sandbox_entries()
    if "__corrupt__" in entries:
        return "The stored sandbox settings can't be read, so runs in sandboxed repos are refused."
    names = {os.path.realpath(p): n for n, p in store.list_repos().items()}
    lines = []
    for real, entry in sorted(entries.items()):
        label = names.get(real, f"{real} (not registered)")
        hosts = entry.get("hosts") if isinstance(entry, dict) else None
        shown = "unreadable, runs refused" if not isinstance(hosts, list) else (", ".join(hosts) or "no hosts")
        lines.append(f"**{label}**: {shown}")
    head = "\n".join(lines) if lines else "No repo is sandboxed."
    tools = ", ".join(
        f"{tool} {'ok' if shutil.which(tool) else 'MISSING'}" for tool in ("bwrap", "socat")
    )
    return f"{head}\nProvider: {provider_name}. {tools}."

"""Generate eval reports for Discord delivery."""

from __future__ import annotations

import logging
from collections import Counter

from bot.engine.eval import (
    ChainEval, SessionEval, attribute_flag, build_digest, load_chain_evals,
    load_evals, normalise_flag_message,
)

log = logging.getLogger(__name__)


def full_report(days: int = 7) -> str:
    """Generate a weekly report with per-repo breakdown and trends.

    Returns markdown text for a Discord embed (max ~4000 chars).
    """
    hours = days * 24
    evals = load_evals(since_hours=hours)
    chains = load_chain_evals(since_hours=hours)

    if not evals and not chains:
        return "No sessions to evaluate."

    lines: list[str] = []
    lines.append(f"**Period:** last {days} day{'s' if days > 1 else ''}")

    # --- Overall stats ---
    total_sessions = len(evals)
    total_cost = sum(e.metrics.get("cost", 0) or 0 for e in evals)
    total_chains = len(chains)
    merged = sum(1 for c in chains if c.outcome == "merged")

    lines.append(f"**Sessions:** {total_sessions} | **Cost:** ${total_cost:.2f}")
    if total_chains:
        merge_rate = merged / total_chains * 100
        lines.append(f"**Chains:** {total_chains} | **Merge rate:** {merge_rate:.0f}%")

    # --- Per-repo breakdown ---
    repo_evals: dict[str, list[SessionEval]] = {}
    for e in evals:
        repo_evals.setdefault(e.repo or "unknown", []).append(e)

    repo_chains: dict[str, list[ChainEval]] = {}
    for c in chains:
        repo_chains.setdefault(c.repo or "unknown", []).append(c)

    all_repos = sorted(set(repo_evals) | set(repo_chains))
    if len(all_repos) > 1:
        lines.append("")
        lines.append("**Per repo:**")
        for repo in all_repos[:8]:
            r_evals = repo_evals.get(repo, [])
            r_chains = repo_chains.get(repo, [])
            r_cost = sum(e.metrics.get("cost", 0) or 0 for e in r_evals)
            r_flags = sum(len(e.flags) for e in r_evals)
            r_merged = sum(1 for c in r_chains if c.outcome == "merged")
            parts = [f"{len(r_evals)} sessions", f"${r_cost:.2f}"]
            if r_chains:
                parts.append(f"{r_merged}/{len(r_chains)} chains merged")
            if r_flags:
                parts.append(f"{r_flags} flags")
            lines.append(f"• **{repo}:** {', '.join(parts)}")

    # --- Top flags by frequency ---
    # Session flags come from the shared digest rather than a private counter,
    # so /report and /evals can never quote different numbers for the same
    # flag: both group on the normalised message and both count SESSIONS
    # (a per-command check fires on every tool call, and raw occurrences let
    # one talkative session outrank a habit spread across fifty). min_count=1
    # because /report's window is short — a threshold would empty it.
    digest = build_digest(days=days, min_count=1, evals=evals)
    if digest.rows:
        lines.append("")
        lines.append("**Top flags:**")
        for row in digest.rows[:5]:
            hits = f", {row.occurrences} hits" if row.occurrences > row.count else ""
            lines.append(
                f"• {row.message} ({row.count}/{digest.sessions} sessions{hits})"
                f" — owner: {row.owner}"
            )

    # Chain flags are not session evals, so they are counted separately — but
    # normalised the same way, or "3 revision loops" and "4 revision loops"
    # would occupy two of the three slots as if they were different findings,
    # and attributed the same way. Three of the attribution rules exist only
    # for chain messages, so skipping this would leave them permanently dead
    # and this block the only one without an owner against each line.
    chain_flags: Counter[tuple[str, str]] = Counter()
    for c in chains:
        for f in c.flags:
            chain_flags[(f.category, normalise_flag_message(f.message))] += 1
    if chain_flags:
        lines.append("")
        lines.append("**Top chain flags:**")
        for (category, msg), count in chain_flags.most_common(3):
            lines.append(
                f"• {msg} ({count}x) — owner: {attribute_flag(category, msg)}"
            )

    # --- Chain efficiency ---
    if chains:
        avg_turns = sum(c.total_turns for c in chains) / len(chains)
        avg_revision = sum(c.revision_loops for c in chains) / len(chains)
        lines.append("")
        lines.append(f"**Avg chain turns:** {avg_turns:.1f} | **Avg revision loops:** {avg_revision:.1f}")

    # Truncate to fit Discord embed
    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3900] + "\n…(truncated)"
    return text

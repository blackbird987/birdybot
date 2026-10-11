"""The CLI command line and the system prompt it carries."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path

from bot import config
from bot.fsutil import atomic_write_text, read_repo_config
from bot.procutil import run_capture
from bot.claude import context_bloat
from bot.claude.provider import ProviderConfig
from bot.claude.types import Instance
from bot.store import history as history_mod

log = logging.getLogger(__name__)


class PromptMixin:
    """The CLI command line and the system prompt it carries. Mixed into ClaudeRunner."""

    async def _thrash_bloat_findings(
        self, instance: Instance, session_id: str | None,
    ) -> list[context_bloat.Autoload]:
        """Oversized auto-attached files in the session that just thrashed.

        Scanned afresh on every thrash rather than cached: a 38 MB transcript
        streams in about 0.2s off the event loop, and the rows a resumed
        attempt appended are exactly what a second thrash should be judged
        on. Never raises; a scan that fails is no finding.
        """
        if not session_id:
            return []
        try:
            findings = await asyncio.to_thread(
                context_bloat.scan_session, session_id,
                instance.repo_path, instance.worktree_path,
            )
        except Exception:
            log.debug("Context bloat scan failed for %s", instance.id,
                      exc_info=True)
            findings = []
        if findings:
            top = findings[0]
            log.warning(
                "Autocompact thrash for %s: %s is %d chars and was loaded %d "
                "times (session %s)",
                instance.id, top.display_path, top.chars, top.loads,
                session_id[:12],
            )
        return findings

    def _build_command(
        self, instance: Instance,
        context: str | None = None,
        sibling_context: str | None = None,
        api_fallback: bool = False,
        provider: ProviderConfig | None = None,
        binary: str | None = None,
        model_override: str | None = None,
    ) -> tuple[list[str], str, str | None, str | None, str | None]:
        """Build CLI command and prompt.  Prompt returned separately for stdin piping.

        *model_override* forces the CLI --model flag past instance.model —
        set by the model-limit failover while the primary model's own quota
        (e.g. Fable 5) is on cooldown.

        Returns (cmd, prompt_text, system_prompt_file, api_key_file, rules_file)
        — caller must clean up the temp files via os.unlink when done.
        """
        provider = provider or self.provider

        # Stamp the resume flag before anything can clear session_id.  The CLI
        # always returns a session_id, so after the run there is no way to tell
        # a resumed turn from a fresh spawn — but prompt-cache reuse can only
        # be judged against a resume.  Mutating the instance here is race-free
        # for the same reason documented at the _build_command call site: the
        # instance is owned by the calling coroutine under the channel lock.
        instance.resumed_session = bool(instance.session_id)

        # Build prompt — never mutated on the instance.
        # Resume fallback (t-3541): when a Layer 3 recovery clears session_id
        # and respawns, the LLM may not see Layer 1's system-prompt preamble
        # if --resume replays the original JSONL system prompt verbatim
        # (Probe A outcome).  This user-message slot is therefore the
        # PRIMARY orientation signal on resume — include the expected
        # CWD/branch so the LLM can verify with `pwd` if something looks off.
        prompt = instance.prompt
        if not prompt:
            sanity_path = instance.worktree_path or instance.repo_path or "(unknown)"
            # Don't hardcode "master" — repos that use main / develop / etc.
            # would get a misleading sanity check.  Fall through:
            # build branch → resolved default → punt to LLM self-check.
            sanity_branch = (
                instance.branch
                or instance.original_branch
                or "(verify with `git branch --show-current`)"
            )
            prompt = (
                f"Continue the previous conversation. "
                f"(Sanity check: your CWD should be `{sanity_path}` "
                f"on branch `{sanity_branch}` — verify with `pwd` if anything looks off.)"
            )

        # Bug 1 fix: prepend master-activity snapshot on every resume so the
        # LLM sees what shipped to master while it was away.  This MUST run
        # outside the `if not prompt` block — Build buttons set prompt to a
        # canned BUILD_FROM_PLAN_PROMPT (non-empty), and Build is the exact
        # path that hits the duplicate-rebuild bug.  Gated on session_id so
        # fresh spawns (covered by the system-prompt injection) don't
        # double-up.  --resume may replay the original JSONL system prompt
        # verbatim, in which case the system-prompt block never reaches the
        # resumed agent — this user-message slot is the load-bearing copy.
        if instance.session_id:
            master_block = self._build_master_context_block(instance)
            if master_block:
                prompt = master_block + "\n\n" + prompt

        # Autocompact-thrash recovery note, on the resumed attempt only.  Goes
        # in the user-message slot rather than the system prompt for the same
        # reason as the block above: --resume can replay the original JSONL
        # system prompt verbatim, so a system-prompt addition may never reach
        # the resumed agent.  The flag is cleared as it's read — one attempt
        # gets the note, and a later turn in the same instance doesn't inherit
        # a stale "you were just aborted".
        if getattr(instance, "_context_thrash_retry", False):
            instance._context_thrash_retry = False
            nudge = config.CONTEXT_THRASH_NUDGE
            # The named-file note rides with the nudge and is cleared with it,
            # so it can never reach an attempt the nudge did not.
            bloat_note = getattr(instance, "_context_bloat_note", None)
            instance._context_bloat_note = None
            if bloat_note:
                nudge = nudge + "\n" + bloat_note
            prompt = nudge + "\n\n" + prompt

        # Memory recovery note (the bot's own reap, or the operating system's
        # OOM kill: both use this one slot). Same read-and-clear discipline,
        # and for the same reason: --resume may replay the original JSONL system
        # prompt verbatim, so the user-message slot is the only delivery that is
        # guaranteed to reach the resumed agent. The text arrives pre-formatted
        # from the run that was reaped, because the tree it measured is gone by
        # the time this runs.
        mem_note = getattr(instance, "_memory_kill_note", None)
        if mem_note:
            instance._memory_kill_note = None
            prompt = mem_note + "\n\n" + prompt

        # Context-overflow recovery note — the third of the same family, and
        # the only one that goes to a session with NO history behind it: the
        # attempt this replaces was abandoned, not resumed, so this note plus
        # the thread history quoted inside it is everything the new session
        # knows. Read-and-clear like the two above, for the same reason.
        overflow_note = getattr(instance, "_context_overflow_note", None)
        if overflow_note:
            instance._context_overflow_note = None
            prompt = overflow_note + "\n\n" + prompt

        # API key file (only for providers that support API fallback)
        api_key_file: str | None = None
        if api_fallback and provider.supports_api_fallback and config.ANTHROPIC_API_KEY:
            api_key_file = self._write_api_key_file(config.ANTHROPIC_API_KEY)
            if not api_key_file:
                log.warning("API key file write failed for %s, skipping API fallback", instance.id)

        # Build system prompt: mobile hint + bot context + pinned context + repo instruction file + projects dir
        system_prompt = self._build_system_prompt(instance, context, sibling_context, provider=provider)

        # Dispatch system prompt delivery based on provider method
        sp_file: str | None = None
        system_prompt_inline: str | None = None
        rules_file: str | None = None

        if provider.system_prompt_method == "rules_dir":
            # Write to workspace .cursor/rules/_bot_system.mdc — Cursor loads
            # all files in this dir automatically.
            working_dir = instance.worktree_path or instance.repo_path
            if working_dir and system_prompt:
                rules_dir = Path(working_dir) / provider.config_dir_name / "rules"
                rules_dir.mkdir(parents=True, exist_ok=True)
                rf = rules_dir / "_bot_system.mdc"
                # _build_command runs in a worker thread, so two concurrent
                # same-working_dir Cursor spawns can write this fixed path at
                # once; the atomic write means neither reads a torn file.
                try:
                    atomic_write_text(rf, system_prompt)
                    rules_file = str(rf)
                    log.debug("Wrote system prompt to %s", rf)
                except OSError:
                    log.warning("Failed to write rules file %s, system prompt skipped", rf)
            # sp_file and system_prompt_inline stay None — not passed to build_command
        else:
            # Claude path: write to temp file, fall back to inline arg
            sp_file = self._write_system_prompt_file(system_prompt)
            if not sp_file:
                max_inline = 30_000
                if len(system_prompt) > max_inline:
                    log.warning("System prompt file write failed, truncating to %d chars", max_inline)
                    system_prompt = system_prompt[:max_inline]
                system_prompt_inline = system_prompt

        # Delegate CLI arg assembly to the provider
        cmd = provider.build_command(
            instance,
            binary=binary,
            system_prompt_file=sp_file,
            system_prompt_inline=system_prompt_inline,
            api_fallback=api_fallback,
            api_key_file=api_key_file,
            model_override=model_override,
        )

        # Prompt piped via stdin (not CLI arg) to avoid Windows command-line
        # length limit (WinError 206).  No "--" separator needed.
        return cmd, prompt, sp_file, api_key_file, rules_file

    @staticmethod
    def _write_system_prompt_file(content: str) -> str | None:
        """Write system prompt to a temp file, return path.  Returns None on failure."""
        path: str | None = None
        try:
            f = tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", prefix="claude_sysprompt_",
                delete=False, encoding="utf-8",
            )
            path = f.name
            f.write(content)
            f.close()
            return path
        except OSError:
            log.warning("Failed to write system prompt temp file, falling back to inline")
            # Clean up partial file if it was created
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            return None

    @staticmethod
    def _write_api_key_file(api_key: str) -> str | None:
        """Write API key to a temp file for apiKeyHelper. Returns path or None."""
        path: str | None = None
        try:
            f = tempfile.NamedTemporaryFile(
                mode="w", suffix=".key", prefix="claude_apikey_",
                delete=False, encoding="utf-8",
            )
            path = f.name
            f.write(api_key)
            f.close()
            return path
        except OSError:
            log.warning("Failed to write API key temp file")
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            return None

    @staticmethod
    def _diagnostics_enabled(instance: Instance) -> bool:
        """Check if diagnostic scaffolding is enabled for this repo."""
        # No repo, no file or a malformed one: default on.
        cfg = read_repo_config(instance.repo_path, "test.json")
        return (cfg or {}).get("diagnostics", True)

    @staticmethod
    def _build_master_context_block(instance: Instance) -> str:
        """Snapshot recent master activity so resumed sessions don't rebuild shipped work.

        Bug 1 mitigation: a session that lost context via compaction or
        resume has no idea whether master moved while it was away — and the
        LLM has been observed silently re-implementing already-merged work.
        Run cheap read-only git commands and inject a "Recent master
        activity" block into both the system prompt (fresh spawns) and the
        resume user-message preamble (resumed sessions, where --resume may
        replay the original system prompt verbatim and never see system-
        prompt edits).

        Also captures `master_baseline_head` lazily on first call when the
        field is unset, so subsequent resumes can show "what shipped since
        this session started" instead of just a flat last-N list.
        """
        if not instance.repo_path or not Path(instance.repo_path).is_dir():
            return ""
        repo = instance.repo_path
        # Default-branch detection: prefer original_branch (already resolved
        # for this repo by _get_default_branch at worktree create time) so we
        # don't hardcode "master" against repos that use main/develop.
        default_branch = instance.original_branch
        if not default_branch:
            try:
                r = run_capture(
                    ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=repo,
                )
                if r.returncode == 0 and r.stdout.strip():
                    default_branch = r.stdout.strip().split("/", 1)[-1]
            except Exception:
                pass
        if not default_branch:
            default_branch = "master"

        try:
            log_r = run_capture(["git", "log", "-10", "--oneline", default_branch], cwd=repo)
        except Exception:
            return ""
        if log_r.returncode != 0:
            return ""
        recent_log = log_r.stdout.strip()
        if not recent_log:
            return ""

        # Lazily capture baseline on first prompt build for this session
        # so resumed/compacted spawns can compute "since I started" deltas.
        if not instance.master_baseline_head:
            try:
                head_r = run_capture(["git", "rev-parse", default_branch], cwd=repo)
                if head_r.returncode == 0:
                    sha = head_r.stdout.strip()
                    if sha:
                        instance.master_baseline_head = sha
            except Exception:
                pass

        parts: list[str] = [
            "--- Recent master activity ---",
            f"Last 10 commits on `{default_branch}`:",
            recent_log,
        ]

        # "What shipped since this session started" — the load-bearing check
        # against blind rebuild-after-compaction.
        if instance.master_baseline_head:
            try:
                since_r = run_capture(
                    ["git", "log", "--oneline",
                     f"{instance.master_baseline_head}..{default_branch}"],
                    cwd=repo,
                )
            except Exception:
                since_r = None
            if since_r and since_r.returncode == 0:
                since_log = since_r.stdout.strip()
                short = instance.master_baseline_head[:7]
                parts.append("")
                if since_log:
                    # Cap to keep the prompt bounded — long-running sessions
                    # could otherwise pump hundreds of commits into every
                    # resume preamble.
                    since_lines = since_log.splitlines()
                    if len(since_lines) > 30:
                        shown = since_lines[:30]
                        suffix_line = (
                            f"... and {len(since_lines) - 30} more "
                            f"(run `git log {short}..{default_branch} --oneline` for the rest)"
                        )
                        since_log = "\n".join([*shown, suffix_line])
                    parts.append(
                        f"Commits merged onto `{default_branch}` since this session "
                        f"started ({short}):"
                    )
                    parts.append(since_log)
                else:
                    parts.append(
                        f"No new commits on `{default_branch}` since this session "
                        f"started ({short})."
                    )

        # Branch-vs-master diff stat — surfaces "branch already merged" cases.
        if instance.branch and instance.branch != default_branch:
            try:
                diff_r = run_capture(
                    ["git", "diff", "--stat",
                     f"{default_branch}...{instance.branch}"],
                    cwd=repo,
                )
            except Exception:
                diff_r = None
            if diff_r and diff_r.returncode == 0:
                stat = diff_r.stdout.strip()
                parts.append("")
                if stat:
                    # Cap to keep system prompt manageable
                    if len(stat) > 1500:
                        stat = stat[:1500] + "\n... (truncated)"
                    parts.append(f"`{instance.branch}` vs `{default_branch}`:")
                    parts.append(stat)
                else:
                    # Distinguish "fresh/behind" (branch is an ancestor of
                    # default — empty diff is expected, not a signal) from
                    # "diverged with empty diff" (squash-merged / shipped —
                    # the case this warning was designed for). Without this
                    # gate every freshly created build worktree branch trips
                    # the warning and a cautious LLM bails before touching
                    # any files. See thread 1506256492884660256.
                    try:
                        anc_r = run_capture(
                            ["git", "merge-base", "--is-ancestor",
                             instance.branch, default_branch],
                            cwd=repo,
                        )
                    except Exception:
                        anc_r = None
                    # Emit only on returncode 1 (diverged). Suppress on 0
                    # (ancestor) and on any other code or subprocess failure
                    # — conservative path that avoids re-introducing the
                    # false positive on edge cases like a vanished ref.
                    if anc_r is not None and anc_r.returncode == 1:
                        parts.append(
                            f"WARNING: branch `{instance.branch}` has no diff vs "
                            f"`{default_branch}` — the work may already be merged. "
                            f"Verify with the user before re-implementing."
                        )

        parts.append("")
        parts.append(
            "Before implementing anything, scan the commits above for "
            "already-shipped work. If a recent master commit looks like what "
            "you are about to do, ASK THE USER before duplicating it."
        )
        parts.append("")
        return "\n".join(parts)

    @staticmethod
    def _build_location_block(instance: Instance) -> str:
        """Tell the LLM exactly where it is on disk and on git.

        Anchors the LLM's ground truth so when files look unexpected — e.g.
        after a Layer 3 recovery clears session_id and respawns — it knows
        to verify CWD before "fixing" code that actually exists on its
        branch.  This is the t-3541 fix: previously the LLM was spawned
        with cwd=worktree_path but never told it was in a worktree, so a
        silent recovery + a confused glance at git log produced phantom
        "code disappeared" panics.

        Note on --resume semantics (Probe A): if --resume replays the
        original system prompt from the JSONL rather than re-rendering,
        this block only protects fresh spawns.  Layer 2's user-prompt
        fallback covers the resume path.
        """
        # Prefer original_branch (resolved by _get_default_branch in the
        # worktree creator — could be master, main, or whatever the repo
        # actually uses) over hardcoding "master".  Falls back through
        # the chain so we never lie to the LLM about a master branch
        # that doesn't exist.
        default_branch = instance.original_branch or "the default branch"
        # Registry name is what /spawn, /repo switch, etc. expect — directory
        # basename and registered name can differ (e.g. `claude-telegram-bot`
        # vs registered `bot`). Surface it explicitly so the LLM doesn't have
        # to guess from the path.
        repo_name_line = (
            f"  Repo name: {instance.repo_name} "
            "(use this for /spawn, /repo switch, etc.)\n"
            if instance.repo_name
            else ""
        )
        if instance.worktree_path:
            return (
                "--- Working Location ---\n"
                f"You are running inside a git worktree:\n"
                f"  CWD:       {instance.worktree_path}\n"
                f"  Branch:    {instance.branch or '(unknown)'}\n"
                f"  Main repo: {instance.repo_path} "
                f"(stays on {default_branch} — do NOT cd there)\n"
                f"{repo_name_line}"
                "\n"
                "If files look unexpectedly missing or different from what you remember:\n"
                "1. Run `pwd` and `git branch --show-current` to verify your location.\n"
                "2. If CWD is not the worktree above, STOP and report it — do not try to\n"
                "   \"fix\" code by re-creating it. The work likely exists on your branch.\n"
                "3. Never `git checkout` in the main repo path — it breaks parallel builds.\n"
                "\n"
            )
        if instance.repo_path:
            current_branch = (
                instance.branch
                or instance.original_branch
                or "(check with `git branch --show-current`)"
            )
            return (
                "--- Working Location ---\n"
                f"You are running in the main repo at {instance.repo_path}.\n"
                f"Branch: {current_branch}.\n"
                f"{repo_name_line}"
                "No worktree is in use for this session.\n"
                "\n"
            )
        return ""

    def _build_system_prompt(
        self, instance: Instance,
        context: str | None = None,
        sibling_context: str | None = None,
        provider: ProviderConfig | None = None,
    ) -> str:
        """Build the system prompt with mobile hint, bot context, pinned context, repo CLAUDE.md, and projects dir."""
        provider = provider or self.provider
        # Working Location preamble (t-3541): anchor the LLM's CWD/branch
        # ground truth at the very top of the system prompt so it can self-
        # correct after a session loss instead of confabulating about
        # "missing" code.
        parts: list[str] = []
        location_block = self._build_location_block(instance)
        if location_block:
            parts.append(location_block)
        # Bug 1 fix: surface recent master activity so resumed/compacted
        # sessions don't rebuild work that already shipped while they were
        # away. Also lazily captures master_baseline_head as a side effect.
        master_block = self._build_master_context_block(instance)
        if master_block:
            parts.append(master_block)
        parts.append(config.MOBILE_HINT)

        # Explain that user can only see text output (critical for good responses)
        parts.append(config.CHAT_APP_CONSTRAINT)

        # Honesty & verification guidance
        parts.append(config.HONESTY_CONSTRAINT)

        # Don't frame effort in human-developer weeks — execution is fast for an LLM
        parts.append(config.EFFORT_FRAMING)

        # Bot capability context so Claude knows what the user can do
        parts.append(config.BOT_CONTEXT)

        # The memory budget, stated before it is enforced. Until now the first
        # thing an agent ever heard about memory was that its run had just been
        # destroyed for exceeding a limit nobody had mentioned — advice arriving
        # strictly after the only moment it could have been acted on. The
        # numbers are formatted from the live config rather than written into
        # the prose so a retuned ceiling cannot leave the prompt lying about it.
        if config.SESSION_MEM_KILL_MB > 0:
            warn_line = (
                f" You get one warning in this thread at "
                f"{config.SESSION_MEM_WARN_MB / 1024:.0f} GB."
                if 0 < config.SESSION_MEM_WARN_MB < config.SESSION_MEM_KILL_MB
                else ""
            )
            parts.append(config.MEMORY_BUDGET_CONTEXT_TEMPLATE.format(
                kill_gb=f"{config.SESSION_MEM_KILL_MB / 1024:.0f}",
                warn_line=warn_line,
            ))

        # Spawn capability is depth-gated: a spawned (depth>=1) thread cannot
        # spawn again (directives/spawn.py recursion cap), so telling it the full
        # /spawn instructions just makes it propose directives that get
        # refused. spawn_depth is stamped on the Instance (directives/spawn.py)
        # before this runs, so it's reliable even for a spawned thread's
        # first message. BOT_CONTEXT_TAIL follows so reboot/wake guidance
        # keeps its original position.
        if instance.spawn_depth >= 1:
            parts.append(config.SPAWN_CAPPED_NOTICE)
        else:
            parts.append(config.SPAWN_CONTEXT)
            # How a wave reports back, and how to answer a child that parked
            # on a question. Depth-0 only: a spawned thread has no children.
            parts.append(config.SPAWN_JOIN_CONTEXT)
            parts.append(config.REPLY_CONTEXT)
        # /chain handoff — available at any depth (a spawned thread can still
        # ship its own work), so it's not gated behind the spawn depth check.
        parts.append(config.CHAIN_CONTEXT)
        parts.append(config.BOT_CONTEXT_TAIL)

        # Universal working context — workflow, Discord UI, branch model, design principles
        parts.append(config.WORKING_CONTEXT)

        # Outlook integration (optional — only when enabled + pywin32 present)
        if config.OUTLOOK_ENABLED:
            try:
                from bot.services.outlook import COM_AVAILABLE

                if COM_AVAILABLE:
                    script = config._PROJECT_ROOT / "bot" / "services" / "outlook.py"
                    parts.append(
                        f"\n\n--- Outlook Integration ---\n"
                        f"You can read the user's Outlook email and calendar. Commands:\n"
                        f'  python "{script}" inbox [count]      — recent emails\n'
                        f'  python "{script}" calendar [days]     — upcoming events\n'
                        f'  python "{script}" search "query" [count] — search by subject/sender\n'
                        f'  python "{script}" unread              — unread count\n'
                        f'  python "{script}" read "subject"      — full email by subject\n'
                        f"Run these via the Bash tool when the user asks about their email or calendar."
                    )
            except ImportError:
                pass

        # Per-step behavioral guidance based on workflow origin
        origin_key = instance.origin.value if instance.origin else "direct"
        guidance = config.WORKFLOW_GUIDANCE.get(origin_key)
        if guidance:
            parts.append(f"\n\n--- Your Role in This Step ---\n{guidance}")

        # Inject diagnostic scaffolding guidance for build steps
        if origin_key == "build" and self._diagnostics_enabled(instance):
            parts.append(config.DIAGNOSTIC_GUIDANCE)

        # Self-wake guidance — only for non-worktree sessions. A worktree build
        # runs in an isolated dir that may be merged/discarded before a wake
        # could fire, so a resumed wake would land on a dead session. instance.
        # branch is the worktree signal (runner gates _ensure_worktree on it),
        # and matches the same gate in lifecycle.check_wake_request. The session
        # schedules a wake by emitting a [BOT_CMD: /wake] directive in its
        # output (parsed post-turn by lifecycle._parse_wake_directive) — no file
        # to write, so no cross-dir-write or path-substitution dependency.
        if not instance.branch:
            parts.append(config.WAKE_GUIDANCE)

        # Plan mode: instruct Claude not to modify files, output a plan instead
        if instance.mode == "plan":
            parts.append(config.PLAN_MODE_CONSTRAINT)

        # Pinned user context (passed as parameter to avoid double-prepend on retry)
        if context:
            parts.append(f"\n\nUser context: {context}")

        repo_path = instance.repo_path
        if repo_path:
            # Include repo instruction file if it exists (e.g. .claude/CLAUDE.md)
            # Skip if path is a directory (e.g. .cursor/rules — handled via rules_dir)
            instruction_path = Path(repo_path) / provider.instruction_file
            if instruction_path.is_file():
                try:
                    content = instruction_path.read_text(encoding="utf-8")
                    parts.append(
                        f"\n\n--- Repository Instructions (from {provider.instruction_file}) ---\n"
                        f"{content}"
                    )
                except Exception:
                    log.warning("Failed to read %s from %s", provider.instruction_file, instruction_path)

            # Point to the projects directory for plans/sessions
            projects_dir = self._get_projects_dir(repo_path)
            if projects_dir and projects_dir.exists():
                parts.append(
                    f"\n\nCLI session history and plans for this repo "
                    f"are stored in: {projects_dir}"
                )

        # Non-owner user context: scope awareness + bash policy
        if not instance.is_owner_session:
            user_label = instance.user_name or instance.user_id or "a granted user"
            access_parts = [
                f"\n\n--- Access Control ---",
                f"This session is being used by {user_label} (not the repo owner).",
                f"Mode: {instance.mode}.",
            ]
            if repo_path:
                access_parts.append(
                    f"You are working in {repo_path}. Do not navigate outside "
                    f"this directory or read files outside it."
                )
            if instance.bash_policy == "allowlist" and instance.mode != "build":
                from bot.discord.access import DEFAULT_BASH_ALLOWLIST, DEFAULT_BASH_DENYLIST
                access_parts.append(
                    f"\nBash commands are restricted. Allowed: {', '.join(DEFAULT_BASH_ALLOWLIST)}."
                    f"\nDenied: {', '.join(DEFAULT_BASH_DENYLIST)}."
                    f"\nDo not run commands outside this allowlist."
                )
            elif instance.bash_policy == "none" and instance.mode != "build":
                access_parts.append(
                    "\nBash tool is disabled. Use Read, Grep, Glob for exploration."
                )
            parts.append("\n".join(access_parts))

        # Recent session history — enables smart recall of past work.
        # Candidates are fetched newest-first, then narrowed to the entries most
        # relevant to THIS prompt. Injecting all 20 spent roughly a thousand
        # tokens per spawn on sessions that usually had nothing to do with the
        # task in hand.
        if instance.repo_name:
            history_block = history_mod.render_recent_block(
                repo=instance.repo_name,
                prompt=instance.prompt or "",
                # Only the branch this session is actually working on.
                # NOT original_branch — despite the name that is the merge
                # TARGET (master), shared by every build in the repo, so
                # pinning it would pin work with nothing in common.
                pin_branches={instance.branch} if instance.branch else set(),
                # Lineage: the steps this session was stacked on are the
                # closest thing the runner has to "same thread" — Instance
                # carries no channel id, but parent/chained ids identify the
                # earlier steps of this same piece of work.
                pin_ids={
                    i for i in (instance.parent_id, instance.chained_from) if i
                },
            )
            if history_block:
                parts.append(
                    "\n\n--- Recent Sessions (this project) ---\n"
                    + history_block
                )

        # Sibling session awareness — helps Claude avoid file conflicts
        if sibling_context:
            parts.append(
                f"\n\n--- Sibling Sessions ---\n{sibling_context}\n"
                "Avoid editing files these sessions are likely working on.\n"
                "These sessions share one machine: full test suites, app "
                "launches, and fixed ports can collide even across "
                "worktrees. Prefer narrow test filters (single test "
                "class/file) over full-suite runs. Full-suite runs are "
                "serialized per repo — if a test command is blocked "
                "because another session holds the test lock, do other "
                "work first and retry in a few minutes."
            )

        return "".join(parts)

    @staticmethod
    def _get_projects_dir(repo_path: str) -> Path | None:
        """The CLI projects dir holding this repo's session history, if any.

        Only used to tell the model where its own past sessions and plans
        live. Three things this had wrong: it carried its own copy of the
        encoder that never replaced ``.``, so a repo path containing a dot
        pointed at a directory the CLI had never written; it looked only
        under the primary account, so a session started on the backup account
        named a path that does not exist; and it knew only the local spelling
        of the repo, so on a shared drive it pointed past the history the
        other machine wrote. All three failures are silent — the caller just
        drops the line.

        Still one directory, because the caller writes one path into a
        prompt. Preference order is local spelling first, then the other
        machines', so an existing local dir always wins.
        """
        try:
            from bot.engine.session_fork import encoded_spellings
            spellings = encoded_spellings(repo_path)
        except Exception:
            return None
        if not spellings:
            return None
        for encoded in spellings:
            for root in config.claude_projects_dirs():
                candidate = root / encoded
                if candidate.is_dir():
                    return candidate
        return config.CLAUDE_PROJECTS_DIR / spellings[0]

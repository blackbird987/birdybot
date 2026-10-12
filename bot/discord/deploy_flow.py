"""Command-based deploys and the Reboot button, out of interactions.py.

The deploy gate (``missing_predecessor_release``) runs in ``execute_deploy``
ahead of the safety-net ``git push origin HEAD``: pushing first is the
irreversible half. See CLAUDE.md, "A higher version number is not proof the
release is in there".
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import discord

from bot import config as bot_config
from bot.claude.runner import missing_predecessor_release

if TYPE_CHECKING:
    from bot.discord.bot import ClaudeBot

log = logging.getLogger(__name__)


# --- Deploy status message management (keeps control rooms clean) ---

_deploy_status_msgs: dict[str, int] = {}  # repo_name → message_id


class DeployStatus:
    """Manages a single editable deploy status message in a control room.

    Used only for command-based deploys (not self-managed reboots).
    Uses the bot token (channel.fetch_message) instead of the interaction
    webhook token for edits, avoiding the 15-minute expiry on long deploys.
    """

    def __init__(self, channel: discord.abc.Messageable, msg: discord.Message,
                 repo_name: str, store: object | None = None):
        self._channel = channel
        self._msg = msg
        self._repo_name = repo_name
        self._store = store
        self.id = msg.id

    async def update(self, content: str) -> None:
        """Edit the status message. Cached object first, fetch fallback."""
        # Truncate to safe limit (leave room for formatting overhead)
        if len(content) > 1900:
            lines = content[:1850].rsplit("\n", 1)[0]
            if "```" in content and not lines.endswith("```"):
                lines += "\n```"
            content = lines
        try:
            await self._msg.edit(content=content)
        except (discord.NotFound, discord.HTTPException):
            try:
                self._msg = await self._channel.fetch_message(self.id)
                await self._msg.edit(content=content)
            except (discord.NotFound, discord.HTTPException):
                # Message gone — send new one as fallback
                self._msg = await self._channel.send(content)
                self.id = self._msg.id
                _deploy_status_msgs[self._repo_name] = self.id

    async def delete(self) -> None:
        """Delete the status message. Persist ID to state on failure for startup cleanup."""
        _deploy_status_msgs.pop(self._repo_name, None)
        try:
            await self._msg.delete()
        except discord.NotFound:
            pass  # Already gone
        except Exception:
            # Rate limit, network error, etc. — try fetch fallback
            try:
                msg = await self._channel.fetch_message(self.id)
                await msg.delete()
            except discord.NotFound:
                pass  # Already gone
            except Exception:
                self._persist_for_cleanup()

    def _persist_for_cleanup(self) -> None:
        """Persist message ID to state.json for startup cleanup."""
        if not self._store:
            return
        try:
            state = self._store.get_platform_state("discord")
            pending = state.get("deploy_status_msgs", {})
            pending[self._repo_name] = self.id
            state["deploy_status_msgs"] = pending
            self._store.set_platform_state("discord", state, persist=True)
            log.warning("Persisted orphaned deploy status msg %s for cleanup", self.id)
        except Exception:
            log.debug("Failed to persist deploy status msg for cleanup", exc_info=True)


async def _start_deploy_status(
    interaction: discord.Interaction, repo_name: str, initial_text: str,
    store: object | None = None,
) -> DeployStatus:
    """Send initial deploy status message, cleaning up the previous one.

    Used only for command-based deploys (self-managed reboots use the embed).
    """
    # Delete previous status message for this repo
    old_msg_id = _deploy_status_msgs.pop(repo_name, None)
    if old_msg_id:
        try:
            old_msg = await interaction.channel.fetch_message(old_msg_id)
            await old_msg.delete()
        except (discord.NotFound, discord.HTTPException):
            pass  # Already gone — dict entry already cleared by .pop()

    # Send initial message via interaction (one-time use of webhook token)
    msg = await interaction.followup.send(initial_text, wait=True)
    _deploy_status_msgs[repo_name] = msg.id
    return DeployStatus(interaction.channel, msg, repo_name, store=store)


async def _handle_approve_deploy(
    bot: ClaudeBot, interaction: discord.Interaction, repo_name: str,
) -> None:
    """Approve a file-sourced deploy config so the Reboot button becomes active."""
    config = bot._store.get_deploy_config(repo_name)
    if not config:
        await interaction.followup.send("No deploy config found.", ephemeral=True)
        return
    config["approved"] = True
    bot._store.set_deploy_config(repo_name, config)
    await interaction.followup.send(
        f"Deploy approved for **{repo_name}**: `{config.get('command', 'self')}`",
        ephemeral=True,
    )
    asyncio.create_task(bot._forums.refresh_control_room(repo_name))


async def execute_deploy(
    bot: ClaudeBot,
    repo_name: str,
    deploy_config: dict,
    *,
    status_callback=None,
) -> tuple[bool, str, str]:
    """Run a command-based deploy for a repo.

    Decoupled from discord.Interaction so it can be called from both
    button handlers and programmatic triggers (auto-fix redeploy).

    Returns (success, output, error_summary).
    """
    command = deploy_config["command"]
    repo_path = bot._store.list_repos().get(repo_name)
    if not repo_path:
        return False, "", f"Repo `{repo_name}` not found"

    raw_cwd = deploy_config.get("cwd")
    if raw_cwd:
        cwd_path = Path(raw_cwd)
        if not cwd_path.is_absolute():
            cwd_path = Path(repo_path) / cwd_path
        cwd = str(cwd_path.resolve())
    else:
        cwd = repo_path

    async def _status(msg: str) -> None:
        if status_callback:
            await status_callback(msg)

    # Release containment gate. This runs before the push, not after: the
    # push is what publishes the revert, and a deploy that has already gone
    # out cannot be warned about usefully. A tree that does not contain its
    # own newest release ships older code under a newer version. Refusing
    # is recoverable (merge the tag in and press Deploy again), shipping it
    # is not.
    if bot_config.RELEASE_ANCESTRY_CHECK:
        missing = await asyncio.to_thread(
            missing_predecessor_release, repo_path, "HEAD",
        )
        if missing:
            log.error(
                "Deploy of %s blocked: HEAD does not contain release %s",
                repo_name, missing,
            )
            return False, "", (
                f"Release `{missing}` is not in `HEAD`, so deploying would revert it. "
                f"Merge that tag into the branch first, or delete it if it was "
                f"never meant to ship."
            )

    # Push to origin before deploying (safety net)
    await _status("\U0001f680 Pushing to origin...")
    try:
        push_proc = await asyncio.create_subprocess_exec(
            "git", "-C", repo_path, "push", "origin", "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        push_out, _ = await asyncio.wait_for(
            push_proc.communicate(), timeout=30,
        )
        if push_proc.returncode != 0:
            push_output = push_out.decode(errors="replace")[:1500]
            await _status(
                f"Push failed (exit {push_proc.returncode}), deploying anyway...\n```\n{push_output}\n```"
            )
    except asyncio.TimeoutError:
        try:
            push_proc.kill()
        except ProcessLookupError:
            pass
        await _status("Push timed out (30s). Proceeding with deploy...")
    except Exception as e:
        await _status(f"Push error: {e}. Proceeding with deploy...")

    await _status(f"\U0001f680 Running: `{command}`...")

    ds = bot._store.get_deploy_state(repo_name)
    proc = None
    try:
        proc = await asyncio.create_subprocess_shell(
            command, cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        raw_timeout = deploy_config.get("timeout", 600)
        deploy_timeout = max(10, min(int(raw_timeout), 3600)) if isinstance(raw_timeout, (int, float)) else 600
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=deploy_timeout)
        output = stdout.decode(errors="replace")[:1500]

        if proc.returncode == 0:
            if ds:
                ds.last_deploy_error = None
                ds.boot_version = ds.current_version
                ds.boot_ref = ds.current_ref
                ds.pending_sessions.clear()
                ds.pending_changes.clear()
                ds.auto_fix_attempt = 0
                ds.auto_fix_thread_id = None
                bot._store.set_deploy_state(repo_name, ds)
                # Also reset generic auto-fix state for deploy trigger
                from bot.store.records import AutoFixState
                bot._store.set_auto_fix_state(repo_name, "deploy", AutoFixState())
            return True, output, ""
        else:
            err_summary = ""
            if output.strip():
                for line in reversed(output.strip().splitlines()):
                    line = line.strip()
                    if line and not line.startswith("---"):
                        err_summary = line[:200]
                        break
            err_summary = err_summary or f"Exit code {proc.returncode}"
            if ds:
                ds.last_deploy_error = err_summary
                bot._store.set_deploy_state(repo_name, ds)
            return False, output, err_summary
    except asyncio.TimeoutError:
        if proc:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        err = f"Timed out ({deploy_timeout}s)"
        if ds:
            ds.last_deploy_error = err
            bot._store.set_deploy_state(repo_name, ds)
        return False, "", err
    except Exception as e:
        err = str(e)[:200]
        if ds:
            ds.last_deploy_error = err
            bot._store.set_deploy_state(repo_name, ds)
        return False, "", err


async def _spawn_deploy_fix(
    bot: ClaudeBot,
    repo_name: str,
    deploy_config: dict,
    error_output: str,
    error_summary: str,
) -> None:
    """Auto-spawn a fix session after deploy failure.

    Delegates to the generic auto-fix primitive. If auto_fix_redeploy is
    enabled, re-runs the deploy after a successful merge.
    Syncs AutoFixState back to DeployState for control room display.
    """
    from bot.engine.auto_fix import spawn_fix_session

    command = deploy_config.get("command", "?")
    prompt = (
        f"The deploy command `{command}` failed.\n\n"
        f"**Error:** {error_summary}\n\n"
    )
    if error_output.strip():
        prompt += f"**Full output:**\n```\n{error_output}\n```\n\n"
    prompt += (
        "Diagnose the issue and fix it. The deploy command runs in the repo root. "
        "Focus on what would cause this specific error."
    )

    # Build redeploy callback if enabled
    on_success = None
    if deploy_config.get("auto_fix_redeploy"):
        async def _redeploy() -> None:
            await execute_deploy(bot, repo_name, deploy_config)
        on_success = _redeploy

    # Sync AutoFixState → DeployState for control room display
    def _sync_deploy_state(channel_id: str) -> None:
        ds = bot._store.get_deploy_state(repo_name)
        if ds:
            afs = bot._store.get_auto_fix_state(repo_name, "deploy")
            ds.auto_fix_attempt = afs.attempt
            ds.auto_fix_thread_id = afs.thread_id
            bot._store.set_deploy_state(repo_name, ds)

    await spawn_fix_session(
        bot, repo_name,
        trigger="deploy",
        fix_prompt=prompt,
        max_retries=deploy_config.get("auto_fix_retries", 1),
        max_cost_usd=2.0,
        on_success=on_success,
        on_started=_sync_deploy_state,
    )

    # Final sync after chain completes (thread_id now None)
    _sync_deploy_state("")


async def _post_deploy_healthcheck(
    bot: ClaudeBot,
    repo_name: str,
    deploy_config: dict,
    healthcheck: dict,
) -> bool:
    """Wait, then run health check commands. Trigger auto-fix if unhealthy.

    Returns True when all checks pass (or none are configured), False on
    any failure/timeout — fleet ship gates its verify-back fan-out on this.
    Existing callers fire-and-forget via create_task and ignore the value.
    """
    from bot.engine.auto_fix import spawn_fix_session

    delay = healthcheck.get("delay_secs", 30)
    commands = healthcheck.get("commands", [])
    if not commands:
        return True

    await asyncio.sleep(delay)

    repo_path = bot._store.list_repos().get(repo_name, "")

    for cmd in commands:
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                cwd=repo_path or None,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode != 0:
                error_output = stdout.decode(errors="replace")[:1500]
                log.warning("Health check failed for %s: %s (exit %d)",
                            repo_name, cmd, proc.returncode)

                if deploy_config.get("auto_fix"):
                    prompt = (
                        f"Post-deploy health check `{cmd}` failed (exit {proc.returncode}).\n\n"
                    )
                    if error_output.strip():
                        prompt += f"**Output:**\n```\n{error_output}\n```\n\n"
                    prompt += (
                        "Diagnose why this health check fails after a successful deploy. "
                        "Focus on recent changes that could cause this."
                    )

                    async def _redeploy() -> None:
                        await execute_deploy(bot, repo_name, deploy_config)

                    await spawn_fix_session(
                        bot, repo_name,
                        trigger="healthcheck",
                        fix_prompt=prompt,
                        max_retries=1,
                        max_cost_usd=1.5,
                        on_success=_redeploy if deploy_config.get("auto_fix_redeploy") else None,
                    )
                return False
        except asyncio.TimeoutError:
            log.warning("Health check timed out for %s: %s", repo_name, cmd)
            return False
        except Exception:
            log.exception("Health check error for %s: %s", repo_name, cmd)
            return False

    log.info("All health checks passed for %s", repo_name)
    return True


async def _handle_reboot_repo(
    bot: ClaudeBot, interaction: discord.Interaction, repo_name: str,
) -> None:
    """Execute a reboot/deploy for a repo based on its deploy config.

    Self-managed reboots show drain status in the control room embed.
    Command-based deploys use a temporary status message (deleted after).
    """
    config = bot._store.get_deploy_config(repo_name)
    if not config or not config.get("approved"):
        await interaction.followup.send(
            "Deploy not configured or not approved.", ephemeral=True,
        )
        return

    ds = bot._store.get_deploy_state(repo_name)
    method = config.get("method", "command")

    if method == "self":
        # Guard against duplicate button clicks while already draining
        if bot._runner.is_draining:
            await interaction.followup.send(
                "Reboot already in progress.", ephemeral=True,
            )
            return

        msg = f"Reboot requested from control room ({repo_name})"
        if ds and ds.pending_changes:
            msg += f" ({len(ds.pending_changes)} pending changes)"

        # Ephemeral ack — only the user who tapped Reboot sees this
        await interaction.followup.send("Reboot initiated.", ephemeral=True)

        # Drain active tasks — show status in the control room embed
        if bot._runner.is_busy:
            ids = ", ".join(bot._runner.active_ids) or "(between steps)"
            drain_text = f"Waiting for active work: {ids}"
            await bot._forums.refresh_control_room(
                repo_name, drain_status=drain_text,
            )
            idle = await bot._runner.wait_until_idle(timeout=300)
            if not idle:
                remaining = ", ".join(bot._runner.active_ids)
                drain_text = (
                    f"Timed out — force-rebooting with "
                    f"{bot._runner.active_count} still running: {remaining}"
                )
                await bot._forums.refresh_control_room(
                    repo_name, drain_status=drain_text,
                )

        # Embed edit confirmed above — safe to request reboot now.
        # force=True: explicit user-initiated deploy already drained active
        # work via wait_until_idle above; bypass busy-check.
        bot._runner.request_reboot({
            "message": msg,
            "channel_id": str(interaction.channel_id),
            "platform": "discord",
        }, force=True)
        # NOTE: Do NOT reset deploy state here — capture_boot_baselines()
        # handles it on the next startup when it detects self_managed=True.

    elif method == "command":
        repo_path = bot._store.list_repos().get(repo_name)
        if not repo_path:
            await interaction.followup.send(
                f"Repo `{repo_name}` not found — deploy config may be stale.",
                ephemeral=True,
            )
            return

        # Single status message for the deploy cycle (deleted after completion)
        status = await _start_deploy_status(
            interaction, repo_name, "Pushing to origin…",
            store=bot._store,
        )

        success, output, err_summary = await execute_deploy(
            bot, repo_name, config,
            status_callback=status.update,
        )

        if success:
            await status.update(
                f"\u2705 Deploy successful.\n```\n{output}\n```"
                if output.strip() else "\u2705 Deploy successful.",
            )
            # Post-deploy health gate
            healthcheck = config.get("healthcheck")
            if healthcheck:
                asyncio.create_task(_post_deploy_healthcheck(
                    bot, repo_name, config, healthcheck,
                ))
        else:
            if output.strip():
                await status.update(
                    f"\u274c Deploy failed.\n```\n{output}\n```",
                )
            else:
                await status.update(f"\u274c Deploy failed: {err_summary}")

            # Auto-fix: spawn a fix session if enabled
            if config.get("auto_fix"):
                asyncio.create_task(_spawn_deploy_fix(
                    bot, repo_name, config, output, err_summary,
                ))

        await status.delete()

    asyncio.create_task(bot._forums.refresh_control_room(repo_name))
    asyncio.create_task(bot._refresh_dashboard())

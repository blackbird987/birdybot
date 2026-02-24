"""Telegram command routing, conversation threading, photo handler."""

from __future__ import annotations

import asyncio
import logging
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import ContextTypes

from bot import config
from bot.claude.parser import extract_summary
from bot.claude.types import Instance, InstanceStatus, InstanceType
from bot.telegram.formatter import (
    build_action_buttons,
    build_stall_buttons,
    chunk_message,
    escape_md,
    format_cost,
    format_instance_list,
    format_result,
    format_schedule_list,
    format_status,
    to_telegram_markdown,
)

if TYPE_CHECKING:
    from bot.claude.runner import ClaudeRunner
    from bot.store.state import StateStore

log = logging.getLogger(__name__)


class Handlers:
    """All Telegram message and command handlers."""

    def __init__(
        self,
        store: StateStore,
        runner: ClaudeRunner,
        cli_version: str,
        start_time: float,
    ) -> None:
        self._store = store
        self._runner = runner
        self._cli_version = cli_version
        self._start_time = start_time

    # --- Auth ---

    def _auth(self, update: Update) -> bool:
        user = update.effective_user
        return user is not None and user.id == config.TELEGRAM_USER_ID

    # --- Text Message Handler ---

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message or not update.message.text:
            return

        text = update.message.text.strip()
        if not text:
            return

        # Check if this is a reply to a bot message (conversation resume)
        if update.message.reply_to_message:
            reply_msg_id = update.message.reply_to_message.message_id
            inst = self._store.find_by_telegram_message(reply_msg_id)
            if inst and inst.session_id:
                await self._resume_conversation(update, context, inst, text)
                return

        # Check if this is an alias
        # Match /<word> pattern at the start
        alias_match = re.match(r'^/(\w+)(?:\s+(.*))?$', text, re.DOTALL)
        if alias_match:
            alias_name = alias_match.group(1)
            alias_prompt = self._store.get_alias(alias_name)
            if alias_prompt:
                extra = alias_match.group(2) or ""
                prompt = f"{alias_prompt} {extra}".strip() if extra else alias_prompt
                await self._run_query(update, context, prompt)
                return

        # Regular query
        await self._run_query(update, context, text)

    async def _run_query(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str
    ) -> None:
        # Budget check
        if not self._check_budget(update):
            await update.message.reply_text(
                "Daily budget exceeded. Use /budget reset to override."
            )
            return

        # Check repo
        repo_name, repo_path = self._store.get_active_repo()
        if not repo_path:
            await update.message.reply_text(
                "No repo set. Use /repo add <name> <path> first."
            )
            return

        # Prepend context
        full_prompt = self._build_prompt(prompt)

        # Create instance
        inst = self._store.create_instance(
            instance_type=InstanceType.QUERY,
            prompt=prompt,
        )
        inst.status = InstanceStatus.RUNNING
        self._store.update_instance(inst)

        # Send "thinking" message
        thinking_msg = await update.message.reply_text(
            f"⏳ {escape_md(inst.display_id())} processing\\.\\.\\.",
            parse_mode="MarkdownV2",
        )
        inst.telegram_message_ids.append(thinking_msg.message_id)
        self._store.update_instance(inst)

        # Progress callback — update thinking message
        last_progress_update = [0.0]

        async def on_progress(message: str):
            now = asyncio.get_event_loop().time()
            if now - last_progress_update[0] < 5:
                return
            last_progress_update[0] = now
            try:
                await thinking_msg.edit_text(
                    f"🔄 {escape_md(inst.display_id())} {escape_md(message)}",
                    parse_mode="MarkdownV2",
                )
            except Exception:
                pass  # Message edit can fail if too frequent

        async def on_stall(instance_id: str):
            try:
                await thinking_msg.edit_text(
                    f"⚠️ {escape_md(inst.display_id())} stalled \\(no output for {config.STALL_TIMEOUT_SECS}s\\)",
                    parse_mode="MarkdownV2",
                    reply_markup=build_stall_buttons(instance_id),
                )
            except Exception:
                pass

        # Run Claude (override prompt with context-appended version)
        inst.prompt = full_prompt
        result = await self._runner.run(inst, on_progress=on_progress, on_stall=on_stall)

        # Update instance
        inst.session_id = result.session_id
        inst.cost_usd = result.cost_usd
        inst.duration_ms = result.duration_ms
        inst.finished_at = datetime.now(timezone.utc).isoformat()

        if result.is_error:
            inst.status = InstanceStatus.FAILED
            inst.error = result.error_message or result.result_text
        else:
            inst.status = InstanceStatus.COMPLETED

        self._store.update_instance(inst)

        # Track cost
        if result.cost_usd:
            self._store.add_cost(result.cost_usd)

        # Send result
        await self._send_result(update.message.chat_id, inst, result.result_text, context)

    async def _resume_conversation(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
        inst: Instance, follow_up: str
    ) -> None:
        if not self._check_budget(update):
            await update.message.reply_text("Daily budget exceeded.")
            return

        full_prompt = self._build_prompt(follow_up)

        # Create a new instance for the continuation
        new_inst = self._store.create_instance(
            instance_type=inst.instance_type,
            prompt=follow_up,
            mode=inst.mode,
        )
        new_inst.session_id = inst.session_id
        new_inst.repo_name = inst.repo_name
        new_inst.repo_path = inst.repo_path
        new_inst.status = InstanceStatus.RUNNING
        self._store.update_instance(new_inst)

        thinking_msg = await update.message.reply_text(
            f"🔄 {escape_md(new_inst.display_id())} resuming\\.\\.\\.",
            parse_mode="MarkdownV2",
        )
        new_inst.telegram_message_ids.append(thinking_msg.message_id)
        self._store.update_instance(new_inst)

        last_progress_update = [0.0]

        async def on_progress(message: str):
            now = asyncio.get_event_loop().time()
            if now - last_progress_update[0] < 5:
                return
            last_progress_update[0] = now
            try:
                await thinking_msg.edit_text(
                    f"🔄 {escape_md(new_inst.display_id())} {escape_md(message)}",
                    parse_mode="MarkdownV2",
                )
            except Exception:
                pass

        new_inst.prompt = full_prompt
        result = await self._runner.run(new_inst, on_progress=on_progress)

        new_inst.session_id = result.session_id
        new_inst.cost_usd = result.cost_usd
        new_inst.duration_ms = result.duration_ms
        new_inst.finished_at = datetime.now(timezone.utc).isoformat()

        if result.is_error:
            new_inst.status = InstanceStatus.FAILED
            new_inst.error = result.error_message or result.result_text
        else:
            new_inst.status = InstanceStatus.COMPLETED

        self._store.update_instance(new_inst)

        if result.cost_usd:
            self._store.add_cost(result.cost_usd)

        await self._send_result(update.message.chat_id, new_inst, result.result_text, context)

    # --- Photo Handler ---

    async def on_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return

        if not self._check_budget(update):
            await update.message.reply_text("Daily budget exceeded.")
            return

        # Download photo
        photo = update.message.photo[-1]  # Largest resolution
        file = await context.bot.get_file(photo.file_id)

        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        await file.download_to_drive(tmp.name)
        tmp.close()

        caption = update.message.caption or ""
        if caption:
            prompt = f"Analyze the image at {tmp.name}. {caption}"
        else:
            prompt = f"Analyze this screenshot at {tmp.name} and describe what you see."

        await self._run_query(update, context, prompt)

    # --- /bg Command ---

    async def on_bg(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return

        text = update.message.text or ""
        text = text.replace("/bg", "", 1).strip()
        if not text:
            await update.message.reply_text("Usage: /bg [--name <name>] <description>")
            return

        if not self._check_budget(update):
            await update.message.reply_text("Daily budget exceeded.")
            return

        repo_name, repo_path = self._store.get_active_repo()
        if not repo_path:
            await update.message.reply_text("No repo set. Use /repo add <name> <path> first.")
            return

        # Parse --name flag
        name = None
        name_match = re.match(r'--name\s+(\S+)\s+(.*)', text, re.DOTALL)
        if name_match:
            name = name_match.group(1)
            text = name_match.group(2).strip()

        # Check if it's an alias
        alias_match = re.match(r'^/(\w+)(?:\s+(.*))?$', text, re.DOTALL)
        if alias_match:
            alias_prompt = self._store.get_alias(alias_match.group(1))
            if alias_prompt:
                extra = alias_match.group(2) or ""
                text = f"{alias_prompt} {extra}".strip() if extra else alias_prompt

        full_prompt = self._build_prompt(text)

        inst = self._store.create_instance(
            instance_type=InstanceType.TASK,
            prompt=text,
            name=name,
            mode="build",
        )
        inst.branch = f"claude-bot/{inst.id}"
        inst.status = InstanceStatus.QUEUED
        self._store.update_instance(inst)

        msg = await update.message.reply_text(
            f"🚀 {escape_md(inst.display_id())} queued \\(build mode, branch `{escape_md(inst.branch)}`\\)",
            parse_mode="MarkdownV2",
            reply_markup=build_action_buttons(inst),
        )
        inst.telegram_message_ids.append(msg.message_id)
        self._store.update_instance(inst)

        # Fire and forget
        asyncio.create_task(self._run_bg_task(inst, update.message.chat_id, context))

    async def _run_bg_task(
        self, inst: Instance, chat_id: int, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        inst.status = InstanceStatus.RUNNING
        inst.prompt = self._build_prompt(inst.prompt)
        self._store.update_instance(inst)

        result = await self._runner.run(inst)

        inst.session_id = result.session_id
        inst.cost_usd = result.cost_usd
        inst.duration_ms = result.duration_ms
        inst.finished_at = datetime.now(timezone.utc).isoformat()

        if result.is_error:
            inst.status = InstanceStatus.FAILED
            inst.error = result.error_message or result.result_text
        else:
            inst.status = InstanceStatus.COMPLETED

        self._store.update_instance(inst)

        if result.cost_usd:
            self._store.add_cost(result.cost_usd)

        # Send completion notification (silent for success, normal for failure)
        await self._send_result(chat_id, inst, result.result_text, context,
                                silent=inst.status == InstanceStatus.COMPLETED)

    # --- Instance Method for callbacks ---

    async def run_instance(
        self, inst: Instance, chat_id: int, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Run an instance (used by callback retry)."""
        inst.status = InstanceStatus.RUNNING
        inst.prompt = self._build_prompt(inst.prompt)
        self._store.update_instance(inst)

        result = await self._runner.run(inst)

        inst.session_id = result.session_id
        inst.cost_usd = result.cost_usd
        inst.duration_ms = result.duration_ms
        inst.finished_at = datetime.now(timezone.utc).isoformat()

        if result.is_error:
            inst.status = InstanceStatus.FAILED
            inst.error = result.error_message or result.result_text
        else:
            inst.status = InstanceStatus.COMPLETED

        self._store.update_instance(inst)

        if result.cost_usd:
            self._store.add_cost(result.cost_usd)

        await self._send_result(chat_id, inst, result.result_text, context)

    # --- /list ---

    async def on_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return
        text = (update.message.text or "").strip()
        show_all = "all" in text
        instances = self._store.list_instances(all_=show_all)
        msg_text = format_instance_list(instances)

        chunks = chunk_message(msg_text)
        for i, chunk in enumerate(chunks):
            try:
                sent = await update.message.reply_text(chunk, parse_mode="MarkdownV2")
            except Exception:
                sent = await update.message.reply_text(chunk)

            # Add buttons to last chunk — for each running instance
            if i == len(chunks) - 1:
                for inst in instances[:5]:
                    buttons = build_action_buttons(inst)
                    if buttons:
                        try:
                            await context.bot.send_message(
                                chat_id=update.message.chat_id,
                                text=f"`{inst.id}`",
                                parse_mode="MarkdownV2",
                                reply_markup=buttons,
                            )
                        except Exception:
                            pass

    # --- /kill ---

    async def on_kill(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return

        text = (update.message.text or "").replace("/kill", "", 1).strip()
        if not text:
            await update.message.reply_text("Usage: /kill <id|name>")
            return

        inst = self._store.get_instance(text)
        if not inst:
            await update.message.reply_text(f"Instance '{text}' not found.")
            return

        killed = await self._runner.kill(inst.id)
        if killed:
            inst.status = InstanceStatus.KILLED
            inst.finished_at = datetime.now(timezone.utc).isoformat()
            self._store.update_instance(inst)
            await update.message.reply_text(f"Killed {inst.display_id()}")
        else:
            await update.message.reply_text("Process not found or already stopped.")

    # --- /retry ---

    async def on_retry(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return

        text = (update.message.text or "").replace("/retry", "", 1).strip()
        if not text:
            await update.message.reply_text("Usage: /retry <id|name>")
            return

        inst = self._store.get_instance(text)
        if not inst:
            await update.message.reply_text(f"Instance '{text}' not found.")
            return

        new_inst = self._store.create_instance(
            instance_type=inst.instance_type,
            prompt=inst.prompt,
            name=f"{inst.name}-retry" if inst.name else None,
            mode=inst.mode,
        )
        new_inst.repo_name = inst.repo_name
        new_inst.repo_path = inst.repo_path
        self._store.update_instance(new_inst)

        await update.message.reply_text(f"Retrying as {new_inst.display_id()}...")
        await self.run_instance(new_inst, update.message.chat_id, context)

    # --- /log ---

    async def on_log(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return

        text = (update.message.text or "").replace("/log", "", 1).strip()
        if not text:
            await update.message.reply_text("Usage: /log <id|name>")
            return

        inst = self._store.get_instance(text)
        if not inst:
            await update.message.reply_text(f"Instance '{text}' not found.")
            return

        # Send prompt + result
        header = f"Prompt: {inst.prompt}\n\n"
        if inst.result_file and Path(inst.result_file).exists():
            await context.bot.send_document(
                chat_id=update.message.chat_id,
                document=open(inst.result_file, "rb"),
                filename=f"{inst.id}.md",
                caption=f"Full output for {inst.display_id()}",
            )
        elif inst.error:
            await update.message.reply_text(f"{header}Error: {inst.error}")
        elif inst.summary:
            await update.message.reply_text(f"{header}{inst.summary}")
        else:
            await update.message.reply_text(f"{header}No output recorded.")

    # --- /diff ---

    async def on_diff(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return

        text = (update.message.text or "").replace("/diff", "", 1).strip()
        if not text:
            await update.message.reply_text("Usage: /diff <id|name>")
            return

        inst = self._store.get_instance(text)
        if not inst:
            await update.message.reply_text(f"Instance '{text}' not found.")
            return

        if inst.diff_file and Path(inst.diff_file).exists():
            await context.bot.send_document(
                chat_id=update.message.chat_id,
                document=open(inst.diff_file, "rb"),
                filename=f"{inst.id}.diff",
                caption=f"Diff for {inst.display_id()}",
            )
        else:
            await update.message.reply_text("No diff available for this instance.")

    # --- /merge ---

    async def on_merge(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return

        text = (update.message.text or "").replace("/merge", "", 1).strip()
        if not text:
            await update.message.reply_text("Usage: /merge <id|name>")
            return

        inst = self._store.get_instance(text)
        if not inst:
            await update.message.reply_text(f"Instance '{text}' not found.")
            return

        msg = await self._runner.merge_branch(inst)
        self._store.update_instance(inst)
        await update.message.reply_text(msg)

    # --- /discard ---

    async def on_discard(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return

        text = (update.message.text or "").replace("/discard", "", 1).strip()
        if not text:
            await update.message.reply_text("Usage: /discard <id|name>")
            return

        inst = self._store.get_instance(text)
        if not inst:
            await update.message.reply_text(f"Instance '{text}' not found.")
            return

        msg = await self._runner.discard_branch(inst)
        self._store.update_instance(inst)
        await update.message.reply_text(msg)

    # --- /cost ---

    async def on_cost(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return

        daily = self._store.get_daily_cost()
        total = self._store.get_total_cost()
        top = self._store.get_top_spenders()
        text = format_cost(daily, total, top)
        try:
            await update.message.reply_text(text, parse_mode="MarkdownV2")
        except Exception:
            await update.message.reply_text(text)

    # --- /status ---

    async def on_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return

        import time
        uptime = time.time() - self._start_time
        repo_name, repo_path = self._store.get_active_repo()
        text = format_status(
            uptime_secs=uptime,
            running=self._store.running_count(),
            daily_cost=self._store.get_daily_cost(),
            total_cost=self._store.get_total_cost(),
            repo_name=repo_name,
            repo_path=repo_path,
            mode=self._store.mode,
            context=self._store.context,
            schedule_count=len(self._store.list_schedules()),
            cli_version=self._cli_version,
        )
        try:
            await update.message.reply_text(text, parse_mode="MarkdownV2")
        except Exception:
            await update.message.reply_text(text)

    # --- /logs ---

    async def on_logs(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return

        log_path = config.LOG_FILE
        if not log_path.exists():
            await update.message.reply_text("No log file found.")
            return

        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        last_50 = "\n".join(lines[-50:])
        if len(last_50) > 4000:
            last_50 = last_50[-4000:]

        await update.message.reply_text(f"```\n{last_50}\n```", parse_mode="MarkdownV2")

    # --- /mode ---

    async def on_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return

        text = (update.message.text or "").replace("/mode", "", 1).strip().lower()
        if text in ("explore", "build"):
            self._store.mode = text
            await update.message.reply_text(f"Mode set to: {text}")
        elif text:
            await update.message.reply_text("Usage: /mode explore|build")
        else:
            await update.message.reply_text(f"Current mode: {self._store.mode}")

    # --- /context ---

    async def on_context(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return

        text = (update.message.text or "").replace("/context", "", 1).strip()

        if text.startswith("set "):
            ctx_text = text[4:].strip()
            self._store.context = ctx_text
            await update.message.reply_text(f"Context set: {ctx_text[:100]}")
        elif text == "clear":
            self._store.context = None
            await update.message.reply_text("Context cleared.")
        else:
            current = self._store.context
            if current:
                await update.message.reply_text(f"Current context: {current}")
            else:
                await update.message.reply_text(
                    "No context set. Use /context set <text>"
                )

    # --- /alias ---

    async def on_alias(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return

        text = (update.message.text or "").replace("/alias", "", 1).strip()

        if text.startswith("set "):
            parts = text[4:].strip().split(None, 1)
            if len(parts) < 2:
                await update.message.reply_text("Usage: /alias set <name> <prompt>")
                return
            name, prompt = parts
            # Strip surrounding quotes from prompt
            prompt = prompt.strip('"\'')
            self._store.set_alias(name, prompt)
            await update.message.reply_text(f"Alias /{name} saved.")
        elif text.startswith("delete "):
            name = text[7:].strip()
            if self._store.delete_alias(name):
                await update.message.reply_text(f"Alias /{name} deleted.")
            else:
                await update.message.reply_text(f"Alias '{name}' not found.")
        elif text == "list" or not text:
            aliases = self._store.list_aliases()
            if aliases:
                lines = [f"/{k} → {v[:60]}" for k, v in aliases.items()]
                await update.message.reply_text("\n".join(lines))
            else:
                await update.message.reply_text("No aliases set.")
        else:
            await update.message.reply_text(
                "Usage: /alias set|delete|list"
            )

    # --- /schedule ---

    async def on_schedule(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return

        text = (update.message.text or "").replace("/schedule", "", 1).strip()

        if text.startswith("every "):
            # Parse interval: every 6h, every 30m, every 1d
            match = re.match(r'every\s+(\d+)([mhd])\s+(.*)', text, re.DOTALL)
            if not match:
                await update.message.reply_text(
                    "Usage: /schedule every <N><m|h|d> <prompt>"
                )
                return
            amount = int(match.group(1))
            unit = match.group(2)
            prompt = match.group(3).strip()

            multiplier = {"m": 60, "h": 3600, "d": 86400}
            interval_secs = amount * multiplier[unit]

            mode = "explore"
            if "--build" in prompt:
                mode = "build"
                prompt = prompt.replace("--build", "").strip()

            sched = self._store.add_schedule(
                prompt=prompt, interval_secs=interval_secs, mode=mode
            )
            await update.message.reply_text(
                f"Schedule {sched.id} created: every {amount}{unit}\n"
                f"Next run: {sched.next_run_at[:16] if sched.next_run_at else 'soon'}"
            )

        elif text.startswith("at "):
            # Parse one-shot: at HH:MM <prompt>
            match = re.match(r'at\s+(\d{1,2}:\d{2})\s+(.*)', text, re.DOTALL)
            if not match:
                await update.message.reply_text(
                    "Usage: /schedule at <HH:MM> <prompt>"
                )
                return
            time_str = match.group(1)
            prompt = match.group(2).strip()

            # Calculate next occurrence of this time (UTC)
            now = datetime.now(timezone.utc)
            hour, minute = map(int, time_str.split(":"))
            run_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if run_at <= now:
                run_at = run_at.replace(day=run_at.day + 1)

            mode = "explore"
            if "--build" in prompt:
                mode = "build"
                prompt = prompt.replace("--build", "").strip()

            sched = self._store.add_schedule(
                prompt=prompt, run_at=run_at.isoformat(), mode=mode
            )
            await update.message.reply_text(
                f"Schedule {sched.id} created: one-shot at {time_str} UTC\n"
                f"Runs: {sched.next_run_at[:16] if sched.next_run_at else time_str}"
            )

        elif text.startswith("delete "):
            sid = text[7:].strip()
            if self._store.delete_schedule(sid):
                await update.message.reply_text(f"Schedule {sid} deleted.")
            else:
                await update.message.reply_text(f"Schedule '{sid}' not found.")

        elif text == "list" or not text:
            schedules = self._store.list_schedules()
            text = format_schedule_list(schedules)
            try:
                await update.message.reply_text(text, parse_mode="MarkdownV2")
            except Exception:
                await update.message.reply_text(text)
        else:
            await update.message.reply_text(
                "Usage: /schedule every|at|list|delete"
            )

    # --- /repo ---

    async def on_repo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return

        text = (update.message.text or "").replace("/repo", "", 1).strip()

        if text.startswith("add "):
            parts = text[4:].strip().split(None, 1)
            if len(parts) < 2:
                await update.message.reply_text("Usage: /repo add <name> <path>")
                return
            name, path = parts
            path = path.strip('"\'')
            if not Path(path).is_dir():
                await update.message.reply_text(f"Directory not found: {path}")
                return
            self._store.add_repo(name, path)
            await update.message.reply_text(f"Repo '{name}' added: {path}")

        elif text.startswith("switch "):
            name = text[7:].strip()
            if self._store.switch_repo(name):
                _, path = self._store.get_active_repo()
                await update.message.reply_text(f"Switched to '{name}': {path}")
            else:
                await update.message.reply_text(f"Repo '{name}' not found.")

        elif text == "list":
            repos = self._store.list_repos()
            active, _ = self._store.get_active_repo()
            if repos:
                lines = []
                for name, path in repos.items():
                    marker = " *" if name == active else ""
                    lines.append(f"  {name}{marker} → {path}")
                await update.message.reply_text("\n".join(lines))
            else:
                await update.message.reply_text("No repos registered.")

        elif not text:
            name, path = self._store.get_active_repo()
            if name:
                await update.message.reply_text(f"Active repo: {name} ({path})")
            else:
                await update.message.reply_text("No repo set. Use /repo add <name> <path>")

        else:
            await update.message.reply_text("Usage: /repo add|switch|list")

    # --- /budget ---

    async def on_budget(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return

        text = (update.message.text or "").replace("/budget", "", 1).strip()
        if text == "reset":
            self._store.reset_daily_budget()
            await update.message.reply_text("Daily budget reset.")
        else:
            daily = self._store.get_daily_cost()
            await update.message.reply_text(
                f"Today: ${daily:.4f} / ${config.DAILY_BUDGET_USD:.2f}"
            )

    # --- /clear ---

    async def on_clear(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return

        count = self._store.archive_old()
        await update.message.reply_text(f"Archived {count} old instances.")

    # --- /help ---

    async def on_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return

        help_text = (
            "*Commands*\n"
            "Send text — quick query\n"
            "Send photo — image analysis\n"
            "Reply to bot — continue conversation\n"
            "`/bg` — background task \\(build mode\\)\n"
            "`/list` — show instances \\(last 24h\\)\n"
            "`/kill` — terminate instance\n"
            "`/retry` — re\\-run instance\n"
            "`/log` — full output\n"
            "`/diff` — git diff\n"
            "`/merge` — merge branch\n"
            "`/discard` — delete branch\n"
            "`/cost` — spending breakdown\n"
            "`/status` — health dashboard\n"
            "`/logs` — bot log\n"
            "`/mode` — explore\\|build\n"
            "`/context` — pinned context\n"
            "`/alias` — command shortcuts\n"
            "`/schedule` — recurring tasks\n"
            "`/repo` — repo management\n"
            "`/budget` — budget info\\/reset\n"
            "`/clear` — archive old instances\n"
        )
        try:
            await update.message.reply_text(help_text, parse_mode="MarkdownV2")
        except Exception:
            await update.message.reply_text(help_text)

    # --- Helpers ---

    def _build_prompt(self, prompt: str) -> str:
        """Prepend context and append mobile hint."""
        parts = []
        ctx = self._store.context
        if ctx:
            parts.append(f"[Context: {ctx}]")
        parts.append(prompt)
        full = "\n\n".join(parts)
        return full + config.MOBILE_HINT

    def _check_budget(self, update: Update) -> bool:
        daily = self._store.get_daily_cost()
        if daily >= config.DAILY_BUDGET_USD:
            return False
        # Warn at 80%
        if daily >= config.DAILY_BUDGET_USD * 0.8:
            asyncio.create_task(self._budget_warning(update, daily))
        return True

    async def _budget_warning(self, update: Update, daily: float) -> None:
        try:
            await update.message.reply_text(
                f"⚠️ Budget warning: ${daily:.4f} / ${config.DAILY_BUDGET_USD:.2f} "
                f"({daily / config.DAILY_BUDGET_USD * 100:.0f}%)"
            )
        except Exception:
            pass

    async def _send_result(
        self,
        chat_id: int,
        inst: Instance,
        result_text: str,
        context: ContextTypes.DEFAULT_TYPE,
        silent: bool = False,
    ) -> None:
        """Send result to chat — summary inline, full as file if long."""
        formatted = format_result(inst)
        buttons = build_action_buttons(inst)

        # Short response: inline
        if len(result_text) < 2000:
            full_text = formatted
            if result_text and inst.status != InstanceStatus.FAILED:
                full_text += "\n\n" + to_telegram_markdown(result_text)

            chunks = chunk_message(full_text)
            for i, chunk in enumerate(chunks):
                is_last = i == len(chunks) - 1
                try:
                    msg = await context.bot.send_message(
                        chat_id=chat_id,
                        text=chunk,
                        parse_mode="MarkdownV2",
                        reply_markup=buttons if is_last else None,
                        disable_notification=silent,
                    )
                except Exception:
                    msg = await context.bot.send_message(
                        chat_id=chat_id,
                        text=chunk,
                        reply_markup=buttons if is_last else None,
                        disable_notification=silent,
                    )
                inst.telegram_message_ids.append(msg.message_id)
        else:
            # Long response: summary + file
            try:
                msg = await context.bot.send_message(
                    chat_id=chat_id,
                    text=formatted,
                    parse_mode="MarkdownV2",
                    reply_markup=buttons,
                    disable_notification=silent,
                )
            except Exception:
                msg = await context.bot.send_message(
                    chat_id=chat_id,
                    text=formatted,
                    reply_markup=buttons,
                    disable_notification=silent,
                )
            inst.telegram_message_ids.append(msg.message_id)

            # Send file
            if inst.result_file and Path(inst.result_file).exists():
                try:
                    doc_msg = await context.bot.send_document(
                        chat_id=chat_id,
                        document=open(inst.result_file, "rb"),
                        filename=f"{inst.id}.md",
                        disable_notification=True,
                    )
                    inst.telegram_message_ids.append(doc_msg.message_id)
                except Exception:
                    log.exception("Failed to send result file for %s", inst.id)

        self._store.update_instance(inst)

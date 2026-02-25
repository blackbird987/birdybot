"""Telegram command routing, conversation threading, photo handler."""

from __future__ import annotations

import asyncio
import logging
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import ContextTypes

from bot import config
from bot.claude.types import Instance, InstanceStatus, InstanceType, RunResult
from bot.telegram.formatter import (
    build_action_buttons,
    build_stall_buttons,
    chunk_message,
    escape_html,
    format_cost,
    format_instance_list,
    format_result,
    format_schedule_list,
    format_status,
    redact_secrets,
    to_telegram_html,
)

if TYPE_CHECKING:
    from bot.claude.runner import ClaudeRunner
    from bot.store.state import StateStore

log = logging.getLogger(__name__)

PARSE_MODE = "HTML"


def _strip_markdown(text: str) -> str:
    """Remove markdown formatting (headers, bold, code, links, rules) and collapse whitespace."""
    text = re.sub(r'#{1,6}\s*', '', text)                      # headers
    text = re.sub(r'\*{1,2}([^*]+)\*{1,2}', r'\1', text)      # bold/italic
    text = re.sub(r'`([^`]+)`', r'\1', text)                   # inline code
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)       # links
    text = re.sub(r'[-—=]{3,}', '', text)                      # horizontal rules
    return re.sub(r'\s+', ' ', text).strip()


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

        # Auto-resume active session if one exists, otherwise start new
        await self._run_query(update, context, text)

    async def on_unknown_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle unregistered /commands — check aliases first."""
        if not self._auth(update) or not update.message or not update.message.text:
            return

        text = update.message.text.strip()
        alias_match = re.match(r'^/(\w+)(?:\s+(.*))?$', text, re.DOTALL)
        if alias_match:
            alias_name = alias_match.group(1)
            alias_prompt = self._store.get_alias(alias_name)
            if alias_prompt:
                extra = alias_match.group(2) or ""
                prompt = f"{alias_prompt} {extra}".strip() if extra else alias_prompt
                await self._run_query(update, context, prompt)
                return

        await update.message.reply_text(
            f"Unknown command: {escape_html(text.split()[0])}\n"
            "Use /help for available commands, or /alias list for aliases.",
            parse_mode=PARSE_MODE,
        )

    async def _run_query(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str
    ) -> None:
        if not self._check_budget(update):
            await update.message.reply_text(
                "Daily budget exceeded. Use /budget reset to override."
            )
            return

        repo_name, repo_path = self._store.get_active_repo()
        if not repo_path:
            await update.message.reply_text(
                "No repo set. Use /repo add <name> <path> first."
            )
            return

        # Auto-resume: if there's an active session, continue it
        resume_session = self._store.active_session_id

        inst = self._store.create_instance(
            instance_type=InstanceType.QUERY,
            prompt=prompt,
        )
        if resume_session:
            inst.session_id = resume_session
        inst.status = InstanceStatus.RUNNING
        self._store.update_instance(inst)

        # Send "thinking" message
        label = "resuming..." if resume_session else "processing..."
        thinking_msg = await update.message.reply_text(
            f"⏳ {escape_html(inst.display_id())} {label}",
            parse_mode=PARSE_MODE,
        )
        inst.telegram_message_ids.append(thinking_msg.message_id)
        self._store.update_instance(inst)

        on_progress, on_stall = self._make_progress_callbacks(
            inst, thinking_msg, self._store.verbose_level,
        )

        result = await self._runner.run(
            inst, on_progress=on_progress, on_stall=on_stall,
            context=self._store.context,
        )

        self._finalize_run(inst, result)

        # Update active session for next message
        if not result.is_error and result.session_id:
            self._store.active_session_id = result.session_id

        await self._send_result(update.message.chat_id, inst, result.result_text, context)

    # --- /new — Start fresh conversation ---

    async def clear_chat(self, chat_id: int, bot) -> None:
        """Delete all tracked bot messages and reset session. Used by /new and [New] button."""
        for inst in self._store.list_instances(all_=True):
            for msg_id in inst.telegram_message_ids:
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=msg_id)
                except Exception:
                    pass
            inst.telegram_message_ids.clear()
        self._store.save()
        self._store.active_session_id = None

    async def on_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return

        await self.clear_chat(update.message.chat_id, context.bot)

        try:
            await update.message.delete()
        except Exception:
            pass

    # --- Photo Handler ---

    async def on_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return

        if not self._check_budget(update):
            await update.message.reply_text("Daily budget exceeded.")
            return

        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)

        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp_path = tmp.name
        await file.download_to_drive(tmp_path)
        tmp.close()

        caption = update.message.caption or ""
        if caption:
            prompt = f"Analyze the image at {tmp_path}. {caption}"
        else:
            prompt = f"Analyze this screenshot at {tmp_path} and describe what you see."

        try:
            await self._run_query(update, context, prompt)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    # --- Document Handler ---

    async def on_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return

        if not self._check_budget(update):
            await update.message.reply_text("Daily budget exceeded.")
            return

        doc = update.message.document
        if not doc:
            return

        # Size guard (10MB)
        if doc.file_size and doc.file_size > 10 * 1024 * 1024:
            await update.message.reply_text("File too large (max 10MB).")
            return

        file = await context.bot.get_file(doc.file_id)
        suffix = Path(doc.file_name).suffix if doc.file_name else ".txt"
        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        tmp_path = tmp.name
        await file.download_to_drive(tmp_path)
        tmp.close()

        caption = update.message.caption or ""
        filename = doc.file_name or "uploaded file"
        if caption:
            prompt = f"I've uploaded a file '{filename}' at {tmp_path}. {caption}"
        else:
            prompt = f"I've uploaded a file '{filename}' at {tmp_path}. Analyze its contents."

        try:
            await self._run_query(update, context, prompt)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

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

        name = None
        name_match = re.match(r'--name\s+(\S+)\s+(.*)', text, re.DOTALL)
        if name_match:
            name = name_match.group(1)
            text = name_match.group(2).strip()

        alias_match = re.match(r'^/(\w+)(?:\s+(.*))?$', text, re.DOTALL)
        if alias_match:
            alias_prompt = self._store.get_alias(alias_match.group(1))
            if alias_prompt:
                extra = alias_match.group(2) or ""
                text = f"{alias_prompt} {extra}".strip() if extra else alias_prompt

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
            f"🚀 {escape_html(inst.display_id())} queued "
            f"(build mode, branch <code>{escape_html(inst.branch)}</code>)",
            parse_mode=PARSE_MODE,
            reply_markup=build_action_buttons(inst),
        )
        inst.telegram_message_ids.append(msg.message_id)
        self._store.update_instance(inst)

        asyncio.create_task(self._run_bg_task(inst, update.message.chat_id, context))

    async def _run_bg_task(
        self, inst: Instance, chat_id: int, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        inst.status = InstanceStatus.RUNNING
        self._store.update_instance(inst)

        result = await self._runner.run(inst, context=self._store.context)
        self._finalize_run(inst, result)

        await self._send_result(chat_id, inst, result.result_text, context,
                                silent=inst.status == InstanceStatus.COMPLETED)

    # --- Instance Method for callbacks ---

    async def run_instance(
        self, inst: Instance, chat_id: int, context: ContextTypes.DEFAULT_TYPE,
        thinking_msg=None, silent: bool = False,
    ) -> None:
        """Run an instance (used by callbacks). Supports live progress via thinking_msg."""
        inst.status = InstanceStatus.RUNNING
        self._store.update_instance(inst)

        on_progress = None
        on_stall = None
        if thinking_msg:
            on_progress, on_stall = self._make_progress_callbacks(
                inst, thinking_msg, self._store.verbose_level,
            )

        result = await self._runner.run(
            inst, on_progress=on_progress, on_stall=on_stall,
            context=self._store.context,
        )
        self._finalize_run(inst, result)

        await self._send_result(chat_id, inst, result.result_text, context, silent=silent)

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
                await update.message.reply_text(chunk, parse_mode=PARSE_MODE)
            except Exception:
                await update.message.reply_text(chunk)

            if i == len(chunks) - 1:
                for inst in instances[:5]:
                    buttons = build_action_buttons(inst)
                    if buttons:
                        try:
                            await context.bot.send_message(
                                chat_id=update.message.chat_id,
                                text=f"<code>{inst.id}</code>",
                                parse_mode=PARSE_MODE,
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

        if not self._check_budget(update):
            await update.message.reply_text("Daily budget exceeded.")
            return

        if inst.repo_path and not Path(inst.repo_path).is_dir():
            await update.message.reply_text("Repo path no longer valid.")
            return

        new_inst = self._store.create_instance(
            instance_type=inst.instance_type,
            prompt=inst.prompt,
            name=f"{inst.name}-retry" if inst.name else None,
            mode=inst.mode,
        )
        new_inst.origin = inst.origin
        new_inst.parent_id = inst.id
        new_inst.repo_name = inst.repo_name
        new_inst.repo_path = inst.repo_path
        if inst.session_id:
            new_inst.session_id = inst.session_id
        if inst.branch:
            new_inst.branch = inst.branch
            new_inst.original_branch = inst.original_branch
        self._store.update_instance(new_inst)

        thinking_msg = await update.message.reply_text(
            f"⏳ {escape_html(new_inst.display_id())} retrying...",
            parse_mode=PARSE_MODE,
        )
        new_inst.telegram_message_ids.append(thinking_msg.message_id)
        self._store.update_instance(new_inst)

        await self.run_instance(
            new_inst, update.message.chat_id, context, thinking_msg=thinking_msg,
        )

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

        header = f"Prompt: {inst.prompt}\n\n"
        if inst.result_file and Path(inst.result_file).exists():
            with open(inst.result_file, "rb") as f:
                await context.bot.send_document(
                    chat_id=update.message.chat_id,
                    document=f,
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
            with open(inst.diff_file, "rb") as f:
                await context.bot.send_document(
                    chat_id=update.message.chat_id,
                    document=f,
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
            await update.message.reply_text(text, parse_mode=PARSE_MODE)
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
            await update.message.reply_text(text, parse_mode=PARSE_MODE)
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

        all_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = all_lines[-50:]
        # Trim lines from front until the escaped text fits in Telegram's limit
        max_len = 4096 - 20  # Account for <pre></pre> tags
        while tail:
            escaped = escape_html("\n".join(tail))
            if len(escaped) <= max_len:
                break
            tail = tail[1:]

        if not tail:
            escaped = "(log too large to display)"

        await update.message.reply_text(
            f"<pre>{escaped}</pre>", parse_mode=PARSE_MODE
        )

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

    # --- /verbose ---

    async def on_verbose(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return

        text = (update.message.text or "").replace("/verbose", "", 1).strip()
        if text in ("0", "1", "2"):
            self._store.verbose_level = int(text)
            labels = {0: "silent", 1: "normal", 2: "detailed"}
            await update.message.reply_text(
                f"Verbose level: {text} ({labels[int(text)]})"
            )
        elif text:
            await update.message.reply_text("Usage: /verbose 0|1|2")
        else:
            level = self._store.verbose_level
            labels = {0: "silent", 1: "normal", 2: "detailed"}
            await update.message.reply_text(
                f"Verbose: {level} ({labels.get(level, '?')})\n"
                "0 = silent, 1 = normal, 2 = detailed"
            )

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
            # Support relative offsets: +2m, +1h, +30s
            rel_match = re.match(r'at\s+\+(\d+)([smhd])\s+(.*)', text, re.DOTALL)
            abs_match = re.match(r'at\s+(\d{1,2}:\d{2})\s+(.*)', text, re.DOTALL)

            if rel_match:
                amount = int(rel_match.group(1))
                unit = rel_match.group(2)
                prompt = rel_match.group(3).strip()
                multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}
                delta = timedelta(seconds=amount * multiplier[unit])
                run_at = datetime.now(timezone.utc) + delta
                time_label = f"+{amount}{unit}"
            elif abs_match:
                time_str = abs_match.group(1)
                prompt = abs_match.group(2).strip()
                now = datetime.now(timezone.utc)
                hour, minute = map(int, time_str.split(":"))
                run_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if run_at <= now:
                    run_at += timedelta(days=1)
                time_label = f"{time_str} UTC"
            else:
                await update.message.reply_text(
                    "Usage: /schedule at <HH:MM|+Nm|+Nh> <prompt>"
                )
                return

            mode = "explore"
            if "--build" in prompt:
                mode = "build"
                prompt = prompt.replace("--build", "").strip()

            sched = self._store.add_schedule(
                prompt=prompt, run_at=run_at.isoformat(), mode=mode
            )
            await update.message.reply_text(
                f"Schedule {sched.id} created: one-shot at {time_label}\n"
                f"Runs: {sched.next_run_at[:16] if sched.next_run_at else time_label}"
            )

        elif text.startswith("delete "):
            sid = text[7:].strip()
            if self._store.delete_schedule(sid):
                await update.message.reply_text(f"Schedule {sid} deleted.")
            else:
                await update.message.reply_text(f"Schedule '{sid}' not found.")

        elif text == "list" or not text:
            schedules = self._store.list_schedules()
            sched_text = format_schedule_list(schedules)
            try:
                await update.message.reply_text(sched_text, parse_mode=PARSE_MODE)
            except Exception:
                await update.message.reply_text(sched_text)
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

    # --- /session ---

    async def on_session(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return

        text = (update.message.text or "").replace("/session", "", 1).strip()

        if text.startswith("resume "):
            sid = text[7:].strip()
            # Support short IDs (first 8 chars) — resolve to full UUID
            if len(sid) < 36:
                fpath = self._find_session_file(sid)
                if not fpath:
                    await update.message.reply_text(
                        f"No session found matching '{escape_html(sid)}'.",
                        parse_mode=PARSE_MODE,
                    )
                    return
                sid = fpath.stem
            self._store.active_session_id = sid
            await update.message.reply_text(
                f"Session set: <code>{escape_html(sid[:12])}…</code>\n"
                "Next message will continue this session.",
                parse_mode=PARSE_MODE,
            )

        elif text == "drop":
            self._store.active_session_id = None
            await update.message.reply_text("Session cleared. Next message starts fresh.")

        else:
            # List recent sessions from ~/.claude/projects/
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup

            sessions = self._scan_sessions(limit=8)
            active = self._store.active_session_id

            if not sessions:
                await update.message.reply_text("No sessions found.")
                return

            lines = ["<b>Recent Sessions</b>\n"]

            buttons = []
            for i, s in enumerate(sessions):
                is_active = active and s["id"] == active
                marker = " ✅" if is_active else ""

                # Topic as header, then last user message as preview
                topic_short = s["topic"][:70]
                if len(s["topic"]) > 70:
                    topic_short += "…"

                lines.append(
                    f"<b>{i + 1}.</b>{marker} <i>{escape_html(s['age'])}</i>\n"
                    f"  {escape_html(topic_short)}"
                )

                # Button label: number + conversation topic
                topic_btn = s["topic"][:35]
                if len(s["topic"]) > 35:
                    topic_btn += "…"
                btn_label = f"{'✅ ' if is_active else ''}{i + 1}. {topic_btn}"
                buttons.append([InlineKeyboardButton(
                    btn_label, callback_data=f"sess_resume:{s['id']}",
                )])

            markup = InlineKeyboardMarkup(buttons)
            msg = "\n".join(lines)
            try:
                await update.message.reply_text(msg, parse_mode=PARSE_MODE, reply_markup=markup)
            except Exception:
                await update.message.reply_text(
                    re.sub(r'<[^>]+>', '', msg), reply_markup=markup,
                )

    @staticmethod
    def _read_session_messages(fpath: Path, last_n: int = 4) -> list[dict]:
        """Read user+assistant messages from a session JSONL. Returns last N exchanges."""
        import json as _json

        messages = []  # list of {"role": "user"|"assistant", "text": "..."}
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = _json.loads(line)
                    except _json.JSONDecodeError:
                        continue
                    rtype = record.get("type")
                    if rtype not in ("user", "assistant"):
                        continue
                    msg = record.get("message", {})
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        # Extract text blocks, skip tool_use/tool_result
                        parts = []
                        for b in content:
                            if isinstance(b, dict) and b.get("type") == "text":
                                parts.append(b.get("text", ""))
                        content = " ".join(parts)
                    if not isinstance(content, str):
                        continue
                    text = content.strip()
                    if not text:
                        continue
                    # Skip CLI internal commands (contain XML tags)
                    if "<command-name>" in text or "<local-command-" in text:
                        continue
                    # Skip tool interruption noise
                    if text.startswith("[Request interrupted"):
                        continue
                    messages.append({
                        "role": rtype,
                        "text": text,
                        "branch": record.get("gitBranch", ""),
                    })
        except Exception:
            pass
        return messages[-last_n:] if messages else []

    def _find_session_file(self, session_id: str) -> Path | None:
        """Find a session JSONL file by full or partial ID."""
        projects_dir = config.CLAUDE_PROJECTS_DIR
        if not projects_dir.is_dir():
            return None
        for proj_dir in projects_dir.iterdir():
            if not proj_dir.is_dir():
                continue
            # Try exact match first
            exact = proj_dir / f"{session_id}.jsonl"
            if exact.exists():
                return exact
            # Try prefix match for short IDs
            if len(session_id) < 36:
                for f in proj_dir.glob(f"{session_id}*.jsonl"):
                    return f
        return None

    def _scan_sessions(self, limit: int = 10) -> list[dict]:
        """Scan ~/.claude/projects/ for recent sessions."""
        projects_dir = config.CLAUDE_PROJECTS_DIR
        if not projects_dir.is_dir():
            return []

        # Collect all .jsonl files with modification times
        candidates = []
        for proj_dir in projects_dir.iterdir():
            if not proj_dir.is_dir():
                continue
            for f in proj_dir.glob("*.jsonl"):
                candidates.append((f.stat().st_mtime, f, proj_dir.name))

        # Sort by most recent first
        candidates.sort(key=lambda x: x[0], reverse=True)

        sessions = []
        seen_ids = set()
        now = datetime.now(timezone.utc)

        for mtime, fpath, proj_encoded in candidates[:limit * 3]:
            if len(sessions) >= limit:
                break

            session_id = fpath.stem
            if session_id in seen_ids:
                continue
            seen_ids.add(session_id)

            # Decode project name from directory
            decoded = proj_encoded.replace("-", "/")
            segments = [s for s in decoded.split("/") if s]
            project_name = segments[-1] if segments else proj_encoded

            # Read messages — all of them, then pick first user + last 2
            all_msgs = self._read_session_messages(fpath, last_n=999)
            branch = ""
            first_user_msg = ""
            if all_msgs:
                for m in reversed(all_msgs):
                    if m.get("branch"):
                        branch = m["branch"]
                        break
                # First user message = conversation topic
                for m in all_msgs:
                    if m["role"] == "user":
                        first_user_msg = m["text"]
                        break
                last_msgs = [m["text"] for m in all_msgs[-2:]]
            else:
                last_msgs = []

            # Skip sessions with no real messages
            if not last_msgs:
                continue

            # Human-readable age
            mtime_dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
            delta = now - mtime_dt
            if delta.days > 0:
                age = f"{delta.days}d ago"
            elif delta.seconds >= 3600:
                age = f"{delta.seconds // 3600}h ago"
            elif delta.seconds >= 60:
                age = f"{delta.seconds // 60}m ago"
            else:
                age = "just now"

            # Clean topic: strip bot prompt prefixes and markdown to get plain text
            topic = first_user_msg or last_msgs[0]
            # Strip known canned prompts
            for prefix in (
                config.PLAN_PROMPT_PREFIX,
                config.BUILD_FROM_PLAN_PROMPT,
                config.BUILD_FROM_QUERY_PROMPT,
                config.PLAN_REVIEW_PROMPT,
                config.CODE_REVIEW_PROMPT,
                config.COMMIT_PROMPT,
                "Implement the following plan:",
                "You have full build permissions.",
            ):
                topic = topic.replace(prefix, "")
            topic = _strip_markdown(topic)
            topic = re.sub(r'^Plan:\s*', '', topic).strip()
            # If nothing meaningful remains, use the last message
            if len(topic) < 5:
                topic = _strip_markdown(last_msgs[-1])

            sessions.append({
                "id": session_id,
                "project": project_name,
                "branch": branch,
                "last_msgs": last_msgs,
                "topic": topic,
                "age": age,
            })

        return sessions

    # --- /help ---

    async def on_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update) or not update.message:
            return

        help_text = (
            "<b>Commands</b>\n"
            "Send text — continues current conversation\n"
            "Send photo — image analysis\n"
            "Send file — document analysis\n"
            "<code>/new</code> — start fresh conversation\n"
            "<code>/bg</code> — background task (build mode)\n"
            "<code>/list</code> — show instances (last 24h)\n"
            "<code>/kill</code> — terminate instance\n"
            "<code>/retry</code> — re-run instance\n"
            "<code>/log</code> — full output\n"
            "<code>/diff</code> — git diff\n"
            "<code>/merge</code> — merge branch\n"
            "<code>/discard</code> — delete branch\n"
            "<code>/cost</code> — spending breakdown\n"
            "<code>/status</code> — health dashboard\n"
            "<code>/logs</code> — bot log\n"
            "<code>/mode</code> — explore|build\n"
            "<code>/verbose</code> — progress detail (0|1|2)\n"
            "<code>/context</code> — pinned context\n"
            "<code>/alias</code> — command shortcuts\n"
            "<code>/schedule</code> — recurring tasks\n"
            "<code>/repo</code> — repo management\n"
            "<code>/session</code> — list/resume desktop CLI sessions\n"
            "<code>/budget</code> — budget info/reset\n"
            "<code>/clear</code> — archive old instances\n"
        )
        try:
            await update.message.reply_text(help_text, parse_mode=PARSE_MODE)
        except Exception:
            await update.message.reply_text(help_text)

    # --- Helpers ---

    @staticmethod
    def _make_progress_callbacks(inst, thinking_msg, verbose: int = 1):
        """Create on_progress and on_stall closures for a thinking message."""
        last_update = [0.0]

        async def on_progress(message: str, detail: str = ""):
            if verbose == 0:
                return
            now = asyncio.get_event_loop().time()
            if now - last_update[0] < 5:
                return
            last_update[0] = now
            display = detail if verbose >= 2 and detail else message
            try:
                await thinking_msg.edit_text(
                    f"🔄 {escape_html(inst.display_id())} {escape_html(display)}",
                    parse_mode=PARSE_MODE,
                )
            except Exception:
                pass

        async def on_stall(instance_id: str):
            try:
                await thinking_msg.edit_text(
                    f"⚠️ {escape_html(inst.display_id())} stalled (no output for {config.STALL_TIMEOUT_SECS}s)",
                    parse_mode=PARSE_MODE,
                    reply_markup=build_stall_buttons(instance_id),
                )
            except Exception:
                pass

        return on_progress, on_stall

    def _finalize_run(self, inst: Instance, result: RunResult) -> None:
        """Apply RunResult to Instance and persist. Shared by all run paths."""
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

    def _check_budget(self, update: Update) -> bool:
        daily = self._store.get_daily_cost()
        if daily >= config.DAILY_BUDGET_USD:
            return False
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
        """Send result to chat — short inline, long as summary + file."""
        buttons = build_action_buttons(inst)

        # Redact secrets before sending to Telegram
        if result_text:
            result_text = redact_secrets(result_text)

        try:
            if inst.status == InstanceStatus.FAILED or not result_text:
                # Failed or empty result: show metadata + error/summary
                formatted = format_result(inst)
                msg = await self._send_telegram(
                    context, chat_id, formatted, buttons, silent
                )
                inst.telegram_message_ids.append(msg.message_id)

            elif len(result_text) < 2000:
                # Short: just show the full response inline
                full_text = to_telegram_html(result_text)
                chunks = chunk_message(full_text)
                for i, chunk in enumerate(chunks):
                    is_last = i == len(chunks) - 1
                    msg = await self._send_telegram(
                        context, chat_id, chunk,
                        buttons if is_last else None, silent,
                    )
                    inst.telegram_message_ids.append(msg.message_id)

            else:
                # Long: show summary inline + full as .md file
                formatted = format_result(inst)
                msg = await self._send_telegram(
                    context, chat_id, formatted, buttons, silent
                )
                inst.telegram_message_ids.append(msg.message_id)

                if inst.result_file and Path(inst.result_file).exists():
                    try:
                        with open(inst.result_file, "rb") as f:
                            doc_msg = await context.bot.send_document(
                                chat_id=chat_id, document=f,
                                filename=f"{inst.id}.md",
                                disable_notification=True,
                            )
                        inst.telegram_message_ids.append(doc_msg.message_id)
                    except Exception:
                        log.exception("Failed to send result file for %s", inst.id)

        except Exception:
            # Last resort: try plain text notification
            log.exception("Failed to send result for %s", inst.id)
            try:
                error_text = inst.error or inst.summary or "Result delivery failed"
                msg = await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"{inst.display_id()}: {error_text[:500]}",
                    disable_notification=silent,
                )
                inst.telegram_message_ids.append(msg.message_id)
            except Exception:
                log.exception("Last-resort notification also failed for %s", inst.id)

        self._store.update_instance(inst)

    @staticmethod
    async def _send_telegram(
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        text: str,
        buttons=None,
        silent: bool = False,
    ):
        """Send a Telegram message with HTML parse mode, falling back to plain text."""
        try:
            return await context.bot.send_message(
                chat_id=chat_id, text=text,
                parse_mode=PARSE_MODE, reply_markup=buttons,
                disable_notification=silent,
            )
        except Exception:
            # Strip HTML tags for plain text fallback
            plain = re.sub(r'<[^>]+>', '', text)
            return await context.bot.send_message(
                chat_id=chat_id, text=plain or "...",
                reply_markup=buttons, disable_notification=silent,
            )

"""Inline keyboard callback query handlers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import ContextTypes

from bot import config
from bot.claude.types import InstanceOrigin, InstanceStatus, InstanceType
from bot.telegram.formatter import (
    build_action_buttons,
    escape_html,
)

if TYPE_CHECKING:
    from bot.claude.runner import ClaudeRunner
    from bot.store.state import StateStore

log = logging.getLogger(__name__)

PARSE_MODE = "HTML"


@dataclass
class SpawnConfig:
    instance_type: InstanceType
    prompt: str
    mode: str
    origin: InstanceOrigin
    status_text: str = "Processing..."
    resume_session: bool = False
    copy_branch: bool = False
    auto_branch: bool = False
    silent: bool = False


class CallbackHandler:
    """Handles inline keyboard button presses."""

    def __init__(self, store: StateStore, runner: ClaudeRunner) -> None:
        self._store = store
        self._runner = runner
        self._handlers_ref = None

    def set_handlers_ref(self, handlers) -> None:
        self._handlers_ref = handlers

    async def handle(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query or not query.data:
            return

        if query.from_user and query.from_user.id != config.TELEGRAM_USER_ID:
            await query.answer("Unauthorized", show_alert=True)
            return

        await query.answer()

        parts = query.data.split(":", 1)
        if len(parts) != 2:
            return

        action, instance_id = parts

        dispatch = {
            "kill": self._on_kill,
            "retry": self._on_retry,
            "log": self._on_log,
            "diff": self._on_diff,
            "merge": self._on_merge,
            "discard": self._on_discard,
            "wait": self._on_wait,
            "new": self._on_new,
            "plan": self._on_plan,
            "build": self._on_build,
            "review_plan": self._on_review_plan,
            "review_code": self._on_review_code,
            "commit": self._on_commit,
            "sess_resume": self._on_sess_resume,
        }

        handler = dispatch.get(action)
        if handler:
            await handler(query, instance_id, context)

    # --- Spawn helper ---

    async def _spawn_from(self, query, instance_id: str, context, cfg: SpawnConfig):
        """Common pattern for spawning a new instance from a button press."""
        source = self._store.get_instance(instance_id)
        if not source:
            await query.edit_message_text("Instance not found.")
            return
        if not self._handlers_ref:
            await query.edit_message_text("Not available.")
            return

        # Budget guard
        if self._store.get_daily_cost() >= config.DAILY_BUDGET_USD:
            await context.bot.send_message(
                chat_id=query.message.chat_id, text="Daily budget exceeded.",
            )
            return

        # Repo guard
        if not source.repo_path or not Path(source.repo_path).is_dir():
            await context.bot.send_message(
                chat_id=query.message.chat_id, text="Repo path no longer valid.",
            )
            return

        # Session guard
        if cfg.resume_session and not source.session_id:
            await context.bot.send_message(
                chat_id=query.message.chat_id, text="No session to resume.",
            )
            return

        new_inst = self._store.create_instance(
            instance_type=cfg.instance_type,
            prompt=cfg.prompt,
            mode=cfg.mode,
        )
        new_inst.origin = cfg.origin
        new_inst.parent_id = source.id
        new_inst.repo_name = source.repo_name
        new_inst.repo_path = source.repo_path

        if cfg.resume_session:
            new_inst.session_id = source.session_id
        if cfg.copy_branch:
            new_inst.branch = source.branch
            new_inst.original_branch = source.original_branch
        elif cfg.auto_branch:
            new_inst.branch = f"claude-bot/{new_inst.id}"

        self._store.update_instance(new_inst)

        # UX: Preserve original answer — only strip its buttons
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        # Send new thinking message (matches _run_query pattern)
        chat_id = query.message.chat_id
        thinking_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏳ {escape_html(new_inst.display_id())} {escape_html(cfg.status_text.lower())}",
            parse_mode=PARSE_MODE,
        )
        new_inst.telegram_message_ids.append(thinking_msg.message_id)
        self._store.update_instance(new_inst)

        await self._handlers_ref.run_instance(
            new_inst, chat_id, context,
            thinking_msg=thinking_msg, silent=cfg.silent,
        )

    # --- Thin callback implementations ---

    async def _on_new(self, query, instance_id: str, context) -> None:
        if not self._handlers_ref:
            return
        await self._handlers_ref.clear_chat(query.message.chat_id, context.bot)

    async def _on_plan(self, query, instance_id: str, context) -> None:
        source = self._store.get_instance(instance_id)
        if not source:
            await query.edit_message_text("Instance not found.")
            return
        prompt = config.PLAN_PROMPT_PREFIX + source.prompt
        await self._spawn_from(query, instance_id, context, SpawnConfig(
            instance_type=InstanceType.QUERY, prompt=prompt,
            mode="explore", origin=InstanceOrigin.PLAN,
            status_text="Planning...", resume_session=True,
        ))

    async def _on_build(self, query, instance_id: str, context) -> None:
        source = self._store.get_instance(instance_id)
        if not source:
            await query.edit_message_text("Instance not found.")
            return
        is_plan = source.origin in (InstanceOrigin.PLAN, InstanceOrigin.REVIEW_PLAN)
        await self._spawn_from(query, instance_id, context, SpawnConfig(
            instance_type=InstanceType.TASK,
            prompt=config.BUILD_FROM_PLAN_PROMPT if is_plan else config.BUILD_FROM_QUERY_PROMPT,
            mode="build", origin=InstanceOrigin.BUILD,
            status_text="Building...", resume_session=True,
            auto_branch=True, silent=True,
        ))

    async def _on_review_plan(self, query, instance_id: str, context) -> None:
        await self._spawn_from(query, instance_id, context, SpawnConfig(
            instance_type=InstanceType.QUERY, prompt=config.PLAN_REVIEW_PROMPT,
            mode="explore", origin=InstanceOrigin.REVIEW_PLAN,
            status_text="Reviewing plan...", resume_session=True,
        ))

    async def _on_review_code(self, query, instance_id: str, context) -> None:
        await self._spawn_from(query, instance_id, context, SpawnConfig(
            instance_type=InstanceType.TASK, prompt=config.CODE_REVIEW_PROMPT,
            mode="build", origin=InstanceOrigin.REVIEW_CODE,
            status_text="Reviewing code...", resume_session=True,
            copy_branch=True, silent=True,
        ))

    async def _on_commit(self, query, instance_id: str, context) -> None:
        await self._spawn_from(query, instance_id, context, SpawnConfig(
            instance_type=InstanceType.TASK, prompt=config.COMMIT_PROMPT,
            mode="build", origin=InstanceOrigin.COMMIT,
            status_text="Committing...", resume_session=True,
            copy_branch=True, silent=True,
        ))

    async def _on_sess_resume(self, query, session_id: str, context) -> None:
        """Resume a desktop CLI session: set active, send last messages as context."""
        self._store.active_session_id = session_id
        chat_id = query.message.chat_id

        # Strip buttons from the session list message
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        # Load recent messages and show last 2 user+assistant pairs
        if self._handlers_ref:
            fpath = self._handlers_ref._find_session_file(session_id)
            if fpath:
                all_msgs = self._handlers_ref._read_session_messages(fpath, last_n=999)

                # Extract last 2 user→assistant pairs for context
                pairs = []
                i = len(all_msgs) - 1
                while i >= 0 and len(pairs) < 2:
                    # Find an assistant message
                    if all_msgs[i]["role"] == "assistant":
                        assistant_msg = all_msgs[i]
                        # Look for the preceding user message
                        j = i - 1
                        while j >= 0 and all_msgs[j]["role"] != "user":
                            j -= 1
                        if j >= 0:
                            pairs.append((all_msgs[j], assistant_msg))
                        else:
                            pairs.append((None, assistant_msg))
                        i = j - 1
                    else:
                        i -= 1

                pairs.reverse()

                for user_msg, asst_msg in pairs:
                    if user_msg:
                        text = user_msg["text"]
                        if len(text) > 400:
                            text = text[:400] + "…"
                        try:
                            await context.bot.send_message(
                                chat_id=chat_id,
                                text=f"<b>You:</b>\n{escape_html(text)}",
                                parse_mode=PARSE_MODE,
                                disable_notification=True,
                            )
                        except Exception:
                            await context.bot.send_message(
                                chat_id=chat_id, text=f"You:\n{text[:400]}",
                                disable_notification=True,
                            )

                    text = asst_msg["text"]
                    if len(text) > 400:
                        text = text[:400] + "…"
                    try:
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=f"<b>Claude:</b>\n{escape_html(text)}",
                            parse_mode=PARSE_MODE,
                            disable_notification=True,
                        )
                    except Exception:
                        await context.bot.send_message(
                            chat_id=chat_id, text=f"Claude:\n{text[:400]}",
                            disable_notification=True,
                        )

        await context.bot.send_message(
            chat_id=chat_id,
            text="✅ Session resumed. Send a message to continue.",
            disable_notification=True,
        )

    # --- Existing callbacks (updated) ---

    async def _on_kill(self, query, instance_id: str, context) -> None:
        inst = self._store.get_instance(instance_id)
        if not inst:
            await query.edit_message_text("Instance not found.")
            return

        killed = await self._runner.kill(instance_id)
        if killed:
            inst.status = InstanceStatus.KILLED
            inst.finished_at = datetime.now(timezone.utc).isoformat()
            self._store.update_instance(inst)
            await query.edit_message_text(
                f"Killed {escape_html(inst.display_id())}",
                parse_mode=PARSE_MODE,
                reply_markup=build_action_buttons(inst),
            )
        else:
            await query.edit_message_text("Process not found or already stopped.")

    async def _on_retry(self, query, instance_id: str, context) -> None:
        inst = self._store.get_instance(instance_id)
        if not inst:
            await query.edit_message_text("Instance not found.")
            return
        if not self._handlers_ref:
            await query.edit_message_text("Retry not available.")
            return

        # Budget guard
        if self._store.get_daily_cost() >= config.DAILY_BUDGET_USD:
            await context.bot.send_message(
                chat_id=query.message.chat_id, text="Daily budget exceeded.",
            )
            return

        # Repo guard
        if inst.repo_path and not Path(inst.repo_path).is_dir():
            await context.bot.send_message(
                chat_id=query.message.chat_id, text="Repo path no longer valid.",
            )
            return

        new_inst = self._store.create_instance(
            instance_type=inst.instance_type,
            prompt=inst.prompt,
            name=f"{inst.name}-retry" if inst.name else None,
            mode=inst.mode,
        )
        new_inst.origin = inst.origin  # Preserve for button dispatch
        new_inst.parent_id = inst.id
        new_inst.repo_name = inst.repo_name
        new_inst.repo_path = inst.repo_path
        if inst.session_id:
            new_inst.session_id = inst.session_id
        if inst.branch:
            new_inst.branch = inst.branch
            new_inst.original_branch = inst.original_branch
        self._store.update_instance(new_inst)

        # UX: Preserve original message — only strip its buttons
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        # Send thinking message with progress
        chat_id = query.message.chat_id
        thinking_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏳ {escape_html(new_inst.display_id())} retrying...",
            parse_mode=PARSE_MODE,
        )
        new_inst.telegram_message_ids.append(thinking_msg.message_id)
        self._store.update_instance(new_inst)

        await self._handlers_ref.run_instance(
            new_inst, chat_id, context, thinking_msg=thinking_msg,
        )

    async def _on_log(self, query, instance_id: str, context) -> None:
        inst = self._store.get_instance(instance_id)
        if not inst:
            await query.edit_message_text("Instance not found.")
            return

        if inst.result_file:
            try:
                with open(inst.result_file, "rb") as f:
                    await context.bot.send_document(
                        chat_id=query.message.chat_id,
                        document=f,
                        filename=f"{inst.id}.md",
                        caption=f"Full output for {inst.display_id()}",
                    )
            except Exception as e:
                await query.edit_message_text(
                    f"Error sending file: {escape_html(str(e))}",
                    parse_mode=PARSE_MODE,
                )
        else:
            text = inst.error or inst.summary or "No output recorded."
            await query.edit_message_text(escape_html(text), parse_mode=PARSE_MODE)

    async def _on_diff(self, query, instance_id: str, context) -> None:
        inst = self._store.get_instance(instance_id)
        if not inst:
            await query.edit_message_text("Instance not found.")
            return

        if inst.diff_file:
            try:
                with open(inst.diff_file, "rb") as f:
                    await context.bot.send_document(
                        chat_id=query.message.chat_id,
                        document=f,
                        filename=f"{inst.id}.diff",
                        caption=f"Diff for {inst.display_id()}",
                    )
            except Exception as e:
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=f"Error sending diff: {escape_html(str(e))}",
                    parse_mode=PARSE_MODE,
                )
        else:
            await context.bot.send_message(
                chat_id=query.message.chat_id, text="No diff available.",
            )

    async def _on_merge(self, query, instance_id: str, context) -> None:
        inst = self._store.get_instance(instance_id)
        if not inst:
            await query.edit_message_text("Instance not found.")
            return

        msg = await self._runner.merge_branch(inst)
        self._store.update_instance(inst)
        await query.edit_message_text(escape_html(msg), parse_mode=PARSE_MODE)

    async def _on_discard(self, query, instance_id: str, context) -> None:
        inst = self._store.get_instance(instance_id)
        if not inst:
            await query.edit_message_text("Instance not found.")
            return

        msg = await self._runner.discard_branch(inst)
        self._store.update_instance(inst)
        await query.edit_message_text(escape_html(msg), parse_mode=PARSE_MODE)

    async def _on_wait(self, query, instance_id: str, context) -> None:
        await query.edit_message_text(
            "Waiting... process is still running.",
            parse_mode=PARSE_MODE,
        )

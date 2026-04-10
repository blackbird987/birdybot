"""Monitor service — background refresh loop, history management, Discord embeds."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import discord

from bot.monitor import fetcher, formatter

log = logging.getLogger(__name__)

DEFAULT_REFRESH_SECS = 14400  # 4 hours


class MonitorConfig:
    """Config for a single monitor, read from env vars."""

    def __init__(self, name: str, url: str, auth: str, refresh_secs: int, repo_name: str | None = None):
        self.name = name
        self.url = url
        self.auth = auth
        self.refresh_secs = refresh_secs
        self.repo_name = repo_name  # if set, create thread in repo forum instead of text channel


class MonitorService:
    """Owns the background refresh loop and history/rollup logic.

    Works directly with discord.py — does NOT use the Messenger abstraction.
    """

    def __init__(
        self,
        bot: discord.Client,
        store: Any,  # StateStore
        guild_id: int,
        category_id: int | None,
        notifier: Any | None = None,
    ) -> None:
        self._bot = bot
        self._store = store
        self._guild_id = guild_id
        self._category_id = category_id
        self._notifier = notifier
        self._on_critical: Any | None = None  # async callback(name, repo_name, snap_data)
        self._task: asyncio.Task | None = None
        self._configs: dict[str, MonitorConfig] = {}

    # --- State helpers ---

    def _get_monitors_state(self) -> dict:
        state = self._store.get_platform_state("discord")
        return state.get("monitors", {})

    def _get_monitor_state(self, name: str) -> dict:
        return self._get_monitors_state().get(name, {})

    def _save_monitor_state(self, name: str, data: dict) -> None:
        state = self._store.get_platform_state("discord")
        if "monitors" not in state:
            state["monitors"] = {}
        state["monitors"][name] = data
        self._store._platform_state["discord"] = state

    # --- Lifecycle ---

    def start(self) -> None:
        """Start the background refresh loop."""
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())
        log.info("Monitor service started")

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            log.info("Monitor service stopped")

    async def _loop(self) -> None:
        """Background loop: refresh all enabled monitors."""
        # Initial refresh on startup
        await self._refresh_all()

        while True:
            # Find shortest refresh interval among enabled monitors
            interval = DEFAULT_REFRESH_SECS
            for cfg in self._configs.values():
                mon = self._get_monitor_state(cfg.name)
                if mon.get("enabled", False):
                    interval = min(interval, cfg.refresh_secs)

            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return

            await self._refresh_all()

    async def _refresh_all(self) -> None:
        """Refresh all enabled monitors."""
        for name, cfg in self._configs.items():
            mon = self._get_monitor_state(name)
            if not mon.get("enabled", False):
                continue
            try:
                await self._refresh_one(name, cfg)
            except Exception:
                log.exception("Error refreshing monitor %s", name)

    async def _refresh_one(self, name: str, cfg: MonitorConfig) -> None:
        """Fetch data, update snapshots, edit embeds for one monitor."""
        mon = self._get_monitor_state(name)
        channel_id = mon.get("channel_id")
        if not channel_id:
            log.warning("Monitor %s has no channel_id", name)
            return

        guild = self._bot.get_guild(self._guild_id)
        if not guild:
            return
        # Support both TextChannel (legacy) and Thread (repo-specific)
        channel = guild.get_channel(int(channel_id)) or guild.get_thread(int(channel_id))
        if not channel:
            try:
                channel = await self._bot.fetch_channel(int(channel_id))
            except Exception:
                channel = None
        if not channel or not isinstance(channel, (discord.TextChannel, discord.Thread)):
            log.error("Monitor %s: channel %s not found, disabling", name, channel_id)
            mon["enabled"] = False
            self._save_monitor_state(name, mon)
            return

        # Fetch
        raw = await fetcher.fetch_all(cfg.url, cfg.auth)

        # Check for complete failure (all None)
        all_failed = all(v is None for v in raw.values())
        auth_failed = any(
            isinstance(v, dict) and v.get("_error") == "auth_failed"
            for v in raw.values()
        )

        if auth_failed:
            mon["consecutive_failures"] = mon.get("consecutive_failures", 0) + 1
            mon["last_fetch_at"] = datetime.now(timezone.utc).isoformat()
            self._save_monitor_state(name, mon)
            embed = formatter.build_dashboard_embed(name, raw)
            await self._edit_or_recreate(channel, name, mon, "dashboard_msg_id", embed)
            # One-time alert
            if mon["consecutive_failures"] == 1 and self._notifier:
                try:
                    await self._notifier.broadcast(
                        f"\u26a0\ufe0f Monitor **{name}**: authentication failed",
                        ttl=30,
                    )
                except Exception:
                    pass
            return

        if all_failed:
            mon["consecutive_failures"] = mon.get("consecutive_failures", 0) + 1
            failures = mon["consecutive_failures"]
            mon["last_fetch_at"] = datetime.now(timezone.utc).isoformat()
            self._save_monitor_state(name, mon)

            if failures >= 3:
                last_ok = mon.get("last_successful_fetch")
                embed = formatter.build_stale_banner(name, failures, last_ok)
                await self._edit_or_recreate(channel, name, mon, "dashboard_msg_id", embed)

            if failures == 10 and self._notifier:
                try:
                    await self._notifier.broadcast(
                        f"\U0001f534 Monitor **{name}**: {failures} consecutive failures",
                        ttl=30,
                    )
                except Exception:
                    pass
            return

        # Success path
        prev_attention = mon.get("last_attention_level", "ok")
        mon["consecutive_failures"] = 0
        now_iso = datetime.now(timezone.utc).isoformat()
        mon["last_fetch_at"] = now_iso
        mon["last_successful_fetch"] = now_iso

        # Update daily snapshot
        snap_data = formatter.extract_snapshot_data(raw)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily = mon.get("daily_snapshots", [])
        self._merge_daily_snapshot(daily, today, snap_data)
        mon["daily_snapshots"] = daily

        # Get yesterday's snapshot for trend arrows
        prev_snapshot = None
        if len(daily) >= 2:
            prev_snapshot = daily[1]

        # Determine current attention
        attention = snap_data["attention_level"]
        mon["last_attention_level"] = attention

        # Rollup
        weekly = mon.get("weekly_summaries", [])
        monthly = mon.get("monthly_summaries", [])
        self._rollup(daily, weekly, monthly)
        mon["weekly_summaries"] = weekly
        mon["monthly_summaries"] = monthly

        self._save_monitor_state(name, mon)

        # Build and edit embeds
        dashboard_embed = formatter.build_dashboard_embed(name, raw, prev_snapshot)
        history_embed = formatter.build_history_embed(daily, weekly, monthly)

        await self._edit_or_recreate(channel, name, mon, "dashboard_msg_id", dashboard_embed)
        await self._edit_or_recreate(channel, name, mon, "history_msg_id", history_embed)

        # Alert if attention worsened
        attention_worsened = (
            formatter._ATTENTION_ORDER.get(attention, 0)
            > formatter._ATTENTION_ORDER.get(prev_attention, 0)
        )
        if attention_worsened and self._notifier:
            emoji = formatter._ATTENTION_EMOJI.get(attention, "\u26a0\ufe0f")
            try:
                await self._notifier.broadcast(
                    f"{emoji} Monitor **{name}** attention level: {prev_attention} \u2192 {attention}",
                    ttl=30,
                )
            except Exception:
                pass

        # Trigger auto-fix diagnostic on critical (independent of notifier)
        if attention_worsened and attention == "critical" and prev_attention != "critical" and self._on_critical:
            try:
                await self._on_critical(name, cfg.repo_name, snap_data)
            except Exception:
                log.exception("on_critical callback failed for %s", name)

    async def _edit_or_recreate(
        self,
        channel: discord.TextChannel | discord.Thread,
        name: str,
        mon: dict,
        msg_key: str,
        embed: discord.Embed,
    ) -> None:
        """Edit existing message or create a new one if it's gone."""
        msg_id = mon.get(msg_key)
        if msg_id:
            try:
                msg = await channel.fetch_message(int(msg_id))
                await msg.edit(embed=embed)
                return
            except discord.NotFound:
                log.info("Monitor message %s deleted, recreating", msg_id)
            except Exception:
                log.debug("Failed to edit monitor message %s", msg_id, exc_info=True)

        # Send new message and pin it
        msg = await channel.send(embed=embed)
        try:
            await msg.pin()
        except Exception:
            log.debug("Failed to pin monitor message", exc_info=True)
        mon[msg_key] = str(msg.id)
        self._save_monitor_state(name, mon)

    # --- Snapshot merge ---

    def _merge_daily_snapshot(
        self,
        daily: list[dict],
        today: str,
        snap: dict,
    ) -> None:
        """Merge new data into today's snapshot (or create it)."""
        existing = None
        for i, d in enumerate(daily):
            if d.get("date") == today:
                existing = d
                break

        if existing is None:
            existing = {"date": today}
            daily.insert(0, existing)

        # Take latest
        for key in ("cost_usd", "issues_active", "version", "uptime_pct"):
            val = snap.get(key)
            if val is not None:
                existing[key] = val

        # Worst attention
        existing["attention_level"] = formatter.worse_attention(
            existing.get("attention_level", "ok"),
            snap.get("attention_level", "ok"),
        )

        # Accumulate unique (set-union)
        for key in ("issues_new", "events"):
            prev = set(existing.get(key, []))
            new = set(snap.get(key, []))
            existing[key] = list(prev | new)

        # Sum
        for key in ("checks_ok", "checks_fail", "issues_resolved"):
            existing[key] = existing.get(key, 0) + snap.get(key, 0)

        # Reboot detection: if serverStartedAt changed, increment reboot count
        new_started = snap.get("server_started_at", "")
        prev_started = existing.get("_last_server_started_at", "")
        if new_started and prev_started and new_started != prev_started:
            existing["reboots"] = existing.get("reboots", 0) + 1
            existing.setdefault("events", [])
            existing["events"] = list(set(existing["events"]) | {f"Reboot at {new_started[:16]}"})
        if new_started:
            existing["_last_server_started_at"] = new_started

    # --- Rollup ---

    def _rollup(
        self,
        daily: list[dict],
        weekly: list[dict],
        monthly: list[dict],
    ) -> None:
        """Roll old dailies into weekly, old weeklies into monthly. Idempotent."""
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")

        # Dailies older than 7 days -> weekly
        to_roll: list[dict] = []
        keep: list[dict] = []
        for d in daily:
            date_str = d.get("date", "")
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                age_days = (now - dt).days
                if age_days > 7:
                    to_roll.append(d)
                else:
                    keep.append(d)
            except ValueError:
                keep.append(d)

        if to_roll:
            # Group by ISO week
            week_groups: dict[str, list[dict]] = {}
            for d in to_roll:
                try:
                    dt = datetime.strptime(d["date"], "%Y-%m-%d")
                    # ISO week: Monday start
                    week_start = dt - timedelta(days=dt.weekday())
                    week_key = week_start.strftime("%Y-%m-%d")
                    week_groups.setdefault(week_key, []).append(d)
                except (ValueError, KeyError):
                    pass

            # Existing weekly keys
            existing_weeks = {w.get("week_start") for w in weekly}

            for week_start_str, days in week_groups.items():
                if week_start_str in existing_weeks:
                    continue
                ws = datetime.strptime(week_start_str, "%Y-%m-%d")
                we = ws + timedelta(days=6)
                summary = {
                    "week_start": week_start_str,
                    "week_end": we.strftime("%Y-%m-%d"),
                    "avg_attention": self._avg_attention(days),
                    "versions": list({d.get("version", "") for d in days} - {""}),
                    "total_issues": sum(d.get("issues_active", 0) for d in days),
                    "total_resolved": sum(d.get("issues_resolved", 0) for d in days),
                    "cost_usd": sum(d.get("cost_usd", 0) or 0 for d in days),
                    "events": list({e for d in days for e in d.get("events", [])}),
                    "uptime_pct": self._avg_uptime(days),
                }
                weekly.append(summary)

            daily.clear()
            daily.extend(keep)

        # Sort weekly by start date desc
        weekly.sort(key=lambda w: w.get("week_start", ""), reverse=True)

        # Weeklies older than 4 weeks -> monthly
        to_roll_w: list[dict] = []
        keep_w: list[dict] = []
        for w in weekly:
            try:
                ws = datetime.strptime(w["week_start"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                age_days = (now - ws).days
                if age_days > 28:
                    to_roll_w.append(w)
                else:
                    keep_w.append(w)
            except (ValueError, KeyError):
                keep_w.append(w)

        if to_roll_w:
            month_groups: dict[str, list[dict]] = {}
            for w in to_roll_w:
                try:
                    ws = datetime.strptime(w["week_start"], "%Y-%m-%d")
                    month_key = ws.strftime("%b %Y")
                    month_groups.setdefault(month_key, []).append(w)
                except (ValueError, KeyError):
                    pass

            existing_months = {m.get("month") for m in monthly}
            for month_key, weeks in month_groups.items():
                if month_key in existing_months:
                    continue
                summary = {
                    "month": month_key,
                    "total_issues": sum(w.get("total_issues", 0) for w in weeks),
                    "total_resolved": sum(w.get("total_resolved", 0) for w in weeks),
                    "cost_usd": sum(w.get("cost_usd", 0) or 0 for w in weeks),
                    "uptime_pct": self._avg_uptime_weekly(weeks),
                }
                monthly.append(summary)

            weekly.clear()
            weekly.extend(keep_w)

    @staticmethod
    def _avg_attention(days: list[dict]) -> str:
        levels = [d.get("attention_level", "ok") for d in days]
        worst = "ok"
        for lvl in levels:
            worst = formatter.worse_attention(worst, lvl)
        # If less than half were worst, downgrade label
        worst_count = sum(1 for l in levels if l == worst)
        if worst_count < len(levels) / 2 and worst != "ok":
            order = ["ok", "warning", "critical"]
            idx = order.index(worst)
            return order[max(0, idx - 1)]
        return worst

    @staticmethod
    def _avg_uptime(days: list[dict]) -> float | None:
        vals = [d.get("uptime_pct") for d in days if d.get("uptime_pct") is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    @staticmethod
    def _avg_uptime_weekly(weeks: list[dict]) -> float | None:
        vals = [w.get("uptime_pct") for w in weeks if w.get("uptime_pct") is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    # --- Setup / commands ---

    async def setup_monitor(
        self,
        cfg: MonitorConfig,
        category: discord.CategoryChannel,
        *,
        forum: discord.ForumChannel | None = None,
        repo_name: str | None = None,
    ) -> discord.TextChannel | discord.Thread:
        """Create channel/thread, send initial embeds, enable monitor.

        If forum is provided, creates a pinned thread in the forum instead
        of a standalone text channel in the category.
        """
        self._configs[cfg.name] = cfg

        # Create or find channel/thread
        if forum:
            channel = await self._ensure_monitor_thread(forum, cfg.name)
        else:
            channel = await self._ensure_monitor_channel(category, cfg.name)

        # Build initial state
        mon = self._get_monitor_state(cfg.name) or {}
        mon["_name"] = cfg.name
        mon["channel_id"] = str(channel.id)
        mon["enabled"] = True
        if repo_name:
            mon["repo_name"] = repo_name
        if forum:
            mon["_forum_id"] = str(forum.id)
        mon.setdefault("consecutive_failures", 0)
        mon.setdefault("daily_snapshots", [])
        mon.setdefault("weekly_summaries", [])
        mon.setdefault("monthly_summaries", [])
        self._save_monitor_state(cfg.name, mon)

        # Send initial placeholder embeds if no messages yet
        if not mon.get("dashboard_msg_id"):
            embed = formatter.build_initial_embed(cfg.name, cfg.url)
            msg = await channel.send(embed=embed)
            try:
                await msg.pin()
            except Exception:
                pass
            mon["dashboard_msg_id"] = str(msg.id)

        if not mon.get("history_msg_id"):
            embed = formatter.build_history_embed([], [], [])
            msg = await channel.send(embed=embed)
            try:
                await msg.pin()
            except Exception:
                pass
            mon["history_msg_id"] = str(msg.id)

        self._save_monitor_state(cfg.name, mon)

        # Immediate first fetch
        try:
            await self._refresh_one(cfg.name, cfg)
        except Exception:
            log.exception("Initial fetch failed for %s", cfg.name)

        return channel

    async def remove_monitor(self, name: str) -> bool:
        """Disable a monitor (keeps channel)."""
        mon = self._get_monitor_state(name)
        if not mon:
            return False
        mon["enabled"] = False
        self._save_monitor_state(name, mon)
        self._configs.pop(name, None)
        return True

    def list_monitors(self) -> list[dict]:
        """List all monitors with status info."""
        monitors = self._get_monitors_state()
        result = []
        for name, mon in monitors.items():
            result.append({
                "name": name,
                "enabled": mon.get("enabled", False),
                "channel_id": mon.get("channel_id"),
                "last_fetch_at": mon.get("last_fetch_at"),
                "last_attention_level": mon.get("last_attention_level", "unknown"),
                "consecutive_failures": mon.get("consecutive_failures", 0),
            })
        return result

    async def refresh_all_now(self) -> int:
        """Manual refresh — returns count of monitors refreshed."""
        count = 0
        for name, cfg in self._configs.items():
            mon = self._get_monitor_state(name)
            if mon.get("enabled", False):
                try:
                    await self._refresh_one(name, cfg)
                    count += 1
                except Exception:
                    log.exception("Manual refresh failed for %s", name)
        return count

    async def recover_on_startup(self) -> None:
        """Recover monitors from state + channel topics on startup."""
        guild = self._bot.get_guild(self._guild_id)
        if not guild or not self._category_id:
            return

        category = guild.get_channel(self._category_id)
        if not category or not isinstance(category, discord.CategoryChannel):
            return

        monitors = self._get_monitors_state()

        # Scan category text channels for monitor:* topics (legacy)
        for ch in category.text_channels:
            if ch.topic and ch.topic.startswith("monitor:"):
                name = ch.topic.split(":", 1)[1]
                if name in monitors:
                    stored_ch = monitors[name].get("channel_id")
                    if stored_ch != str(ch.id):
                        monitors[name]["channel_id"] = str(ch.id)
                        log.info("Recovered monitor %s channel from topic: %s", name, ch.id)

        # Scan forum threads for monitor threads (repo-specific)
        from bot.discord.channels import MONITOR_NAME
        for ch in category.channels:
            if not isinstance(ch, discord.ForumChannel):
                continue
            for thread in ch.threads:
                if thread.name == MONITOR_NAME:
                    # Find which monitor this belongs to by checking state
                    for name, mon in monitors.items():
                        if mon.get("channel_id") == str(thread.id):
                            break
                        if mon.get("repo_name"):
                            # Match by repo forum
                            if str(ch.id) == mon.get("_forum_id", ""):
                                mon["channel_id"] = str(thread.id)
                                log.info("Recovered monitor %s thread from forum: %s", name, thread.id)
                                break

        # Load configs once for re-registration and migration
        all_configs = _load_monitor_configs()

        # --- Migrate legacy text-channel monitors to forum threads ---
        for name, mon in list(monitors.items()):
            if not mon.get("enabled", False):
                continue

            ch_id = mon.get("channel_id")
            if not ch_id:
                continue

            # Check if current channel is a text channel (legacy)
            ch = guild.get_channel(int(ch_id))
            if not isinstance(ch, discord.TextChannel):
                continue  # Already a thread or not found — skip

            # Determine target repo: explicit config, or name-match
            cfg = all_configs.get(name)
            if not cfg:
                continue  # No env config — don't migrate without a valid config
            target_repo = cfg.repo_name or None
            if not target_repo and hasattr(self._bot, '_forums'):
                if name in self._bot._forums.forum_projects:
                    target_repo = name

            if not target_repo:
                continue  # No matching repo — keep as text channel

            # Find the repo's forum (with fetch fallback for cache misses)
            forum = None
            if hasattr(self._bot, '_forums'):
                proj = self._bot._forums.forum_projects.get(target_repo)
                if proj and proj.forum_channel_id:
                    forum = guild.get_channel(int(proj.forum_channel_id))
                    if not forum:
                        try:
                            forum = await self._bot.fetch_channel(int(proj.forum_channel_id))
                        except Exception:
                            log.warning("Could not fetch forum %s for monitor %s", proj.forum_channel_id, name)
                    if not isinstance(forum, discord.ForumChannel):
                        forum = None

            if not forum:
                continue

            log.info("Migrating monitor %s from text channel to forum %s", name, forum.id)
            try:
                thread = await self._ensure_monitor_thread(forum, name)

                # Clear old message IDs — fresh embeds on next refresh
                mon.pop("dashboard_msg_id", None)
                mon.pop("history_msg_id", None)
                mon["channel_id"] = str(thread.id)
                mon["_forum_id"] = str(forum.id)
                mon["repo_name"] = target_repo

                # Update ForumProject
                if hasattr(self._bot, '_forums'):
                    proj = self._bot._forums.forum_projects.get(target_repo)
                    if proj:
                        proj.monitor_thread_id = str(thread.id)
                        self._bot._forums.save_forum_map()

                # Delete old text channel
                try:
                    await ch.delete(reason=f"Monitor {name} migrated to forum thread")
                    log.info("Deleted legacy monitor channel %s", ch_id)
                except Exception:
                    log.warning("Could not delete legacy channel %s", ch_id, exc_info=True)

                # Immediate refresh so thread isn't empty until next cycle
                self._configs[name] = cfg
                try:
                    await self._refresh_one(name, cfg)
                except Exception:
                    log.exception("Post-migration refresh failed for %s", name)

            except Exception:
                log.exception("Failed to migrate monitor %s", name)

        # Re-register configs for enabled monitors
        for name, mon in monitors.items():
            if mon.get("enabled", False):
                cfg = all_configs.get(name)
                if cfg:
                    self._configs[name] = cfg
                    mon["_name"] = name
                    log.info("Recovered monitor config: %s", name)
                else:
                    log.warning("Monitor %s enabled but no env config found", name)

        # Save any recovered state
        state = self._store.get_platform_state("discord")
        state["monitors"] = monitors
        self._store._platform_state["discord"] = state

    async def _ensure_monitor_thread(
        self,
        forum: discord.ForumChannel,
        name: str,
    ) -> discord.Thread:
        """Find or create a pinned monitor thread in a repo forum."""
        from bot.discord.channels import MONITOR_NAME

        # Check active threads
        for thread in forum.threads:
            if thread.name == MONITOR_NAME:
                if thread.archived:
                    await thread.edit(archived=False)
                return thread

        # Check archived threads before creating a duplicate
        async for thread in forum.archived_threads(limit=50):
            if thread.name == MONITOR_NAME:
                await thread.edit(archived=False)
                return thread

        from bot.discord.channels import create_monitor_post
        thread, _ = await create_monitor_post(forum, name)
        return thread

    async def _ensure_monitor_channel(
        self,
        category: discord.CategoryChannel,
        name: str,
    ) -> discord.TextChannel:
        """Find or create a monitor channel (legacy — text channel in category)."""
        from bot.discord.channels import sanitize_channel_name

        channel_name = f"\U0001f4ca\u2502{sanitize_channel_name(name)}"
        topic = f"monitor:{name}"

        # Check existing channels
        for ch in category.text_channels:
            if ch.topic == topic:
                return ch

        channel = await category.guild.create_text_channel(
            name=channel_name,
            category=category,
            topic=topic,
        )
        log.info("Created monitor channel %s (%s)", channel.id, channel_name)
        return channel


def _load_monitor_configs() -> dict[str, MonitorConfig]:
    """Load all MONITOR_*_URL configs from environment."""
    import os

    default_refresh = int(os.getenv("MONITOR_REFRESH_SECS", str(DEFAULT_REFRESH_SECS)))
    configs: dict[str, MonitorConfig] = {}

    # Scan env for MONITOR_*_URL pattern
    for key, val in os.environ.items():
        if key.startswith("MONITOR_") and key.endswith("_URL") and val:
            # Extract name: MONITOR_AIAGENT_URL -> aiagent
            name = key[8:-4].lower()  # strip MONITOR_ and _URL
            url = val
            auth = os.getenv(f"MONITOR_{name.upper()}_AUTH", "")
            refresh = int(os.getenv(f"MONITOR_{name.upper()}_REFRESH", str(default_refresh)))
            repo = os.getenv(f"MONITOR_{name.upper()}_REPO") or None
            configs[name] = MonitorConfig(name, url, auth, refresh, repo_name=repo)

    return configs

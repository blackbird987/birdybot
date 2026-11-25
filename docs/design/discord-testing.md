# Testing

### Discord integration test tool

```bash
python scripts/discord_test.py <command>
```

**Setup (one-time):**
1. Create Ark webhook: `python scripts/discord_test.py setup-webhook <ark_channel_id>`
   → Add URL to `TEST_LOBBY_WEBHOOK_URL` in `.env`
2. Create forum webhook: `python scripts/discord_test.py setup-webhook <forum_channel_id>`
   → Add URL to `TEST_WEBHOOK_URL` in `.env`
3. Add both webhook IDs to `TEST_WEBHOOK_IDS` (comma-separated) in `.env`
4. Restart bot

**Commands:**
- `list-channels` — show all channels in bot category (verify forums exist)
- `list-threads <forum_id>` — show active/archived threads + tags
- `channel-info <id>` — channel type, parent, tags, archive status
- `send <channel_or_thread_id> <msg>` — send via webhook (auto-picks Ark vs forum webhook)
- `read <channel_or_thread_id> [limit]` — read messages with embeds/buttons
- `wait-response <channel_id> [timeout]` — poll for bot response after sending
- `run-suite` — automated test sequence (forum creation, thread resume, archived resume, dedup, tags)
- `arm [minutes]` / `disarm` / `armed`: open, close or show test mode (below)

**Test mode (why the webhooks need arming).** A message from a webhook in
`TEST_WEBHOOK_IDS` is treated as the owner, which means a bypass-mode Claude
session on this PC. A webhook URL is not a private credential: anyone in the
server with Manage Webhooks, and every bot with Administrator, can copy it from
Server Settings > Integrations. So since 2026-11 the pass also needs a
short-lived arm file in the bot's data dir (`test_webhooks_armed.json`, mode
0600, owned by the bot's user, at most 4 hours ahead), which only someone on
this machine can write. `send`, `send-file`, `edit`, `new-post` and `run-suite`
arm it for 30 minutes on their own when it is off or has under 10 minutes left;
`--no-arm` skips that (used to check the gate). While unarmed, a test-webhook
message is ignored, logged as a WARNING, and the owner gets one Ark alert per
webhook per hour saying its link may have leaked. The alert never quotes the
message, and the thread's history briefing for a later session quotes it as a
bot message, not as the owner's. Everything fails closed: a missing,
expired, malformed, symlinked, group-readable or foreign-owned file means off.
Code: `bot/test_webhook_gate.py`. Harness: `scripts/test_test_webhook_gate.py`.

**Who can reach the bot.** `python scripts/discord_audit.py` prints, read-only,
who in the server holds Administrator, Manage Webhooks, Manage Server or Manage
Roles, which channel overwrites grant Manage Webhooks or Manage Permissions on
their own, every webhook (name, channel, creator; never its URL), 2FA and
verification levels, whether the bot is public and which other servers it is
in. `--json` prints a report to save and `--diff old.json` prints only what
changed since it, meant for a weekly check. Exit 2 means the token is missing,
Discord could not be reached, or the old report could not be parsed. Harness: `scripts/test_discord_audit.py`.

**Quick verification after changes:**
```bash
python scripts/discord_test.py list-channels          # forums exist?
python scripts/discord_test.py list-threads <forum_id> # threads created?
python scripts/discord_test.py read <thread_id> 5      # bot responding?
```

### Read Discord messages (lightweight)

```bash
python scripts/discord_read.py [channel_id] [limit]   # default: The Ark, 10
```

### Manual verification

- `/sync 3` → threads created per project with history
- `/new` → fresh thread in project forum
- `/repo` → select menu dropdown (with 2+ repos)
- Workflow buttons (Plan/Build & Ship/Merge/Done) work inside forum threads
- Send message in The Ark → informational reply (no routing)
- Send message in archived thread → auto-unarchives + resumes session

### Log monitoring

Always tail logs when debugging or testing:
```bash
tail -f data/logs/bot.log        # real-time (run in background)
tail -n 50 data/logs/bot.log     # recent entries
```

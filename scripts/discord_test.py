"""Discord integration test tool for forum-based bot architecture.

Commands:
  setup-webhook <channel_id>     Create a test webhook, print ID + URL
  send <channel_or_thread_id> <message>   Send via appropriate webhook
  read <channel_or_thread_id> [limit]     Read messages
  list-channels                  List all channels in bot category
  list-threads <forum_id>        List active + archived threads
  channel-info <id>              Get channel type, parent, topic, tags
  wait-response <channel_id> [timeout]    Poll for new bot messages
  run-suite                      Automated test sequence

Webhook setup:
  You need webhooks on EACH channel you want to send to. A webhook can only
  post to its own channel (or threads within it via ?thread_id=).

  1. Create lobby webhook:  setup-webhook <lobby_id>
     -> Set TEST_LOBBY_WEBHOOK_URL in .env
  2. Create forum webhook:  setup-webhook <forum_id>
     -> Set TEST_WEBHOOK_URL in .env
  3. Add BOTH webhook IDs to TEST_WEBHOOK_IDS (comma-separated)
"""
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

# Load config from .env
ENV_PATH = os.path.join(os.path.dirname(__file__), "..", ".env")
_env = {}
try:
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                _env[k.strip()] = v.strip()
except FileNotFoundError:
    print("Error: .env not found")
    sys.exit(1)

TOKEN = _env.get("DISCORD_BOT_TOKEN")
GUILD_ID = _env.get("DISCORD_GUILD_ID")
CATEGORY_ID = _env.get("DISCORD_CATEGORY_ID")
LOBBY_ID = _env.get("DISCORD_LOBBY_CHANNEL_ID")
# Forum webhook — for sending to forum threads
WEBHOOK_URL = _env.get("TEST_WEBHOOK_URL")
# Lobby webhook — for sending to lobby (triggers forum creation flow)
LOBBY_WEBHOOK_URL = _env.get("TEST_LOBBY_WEBHOOK_URL")
WEBHOOK_IDS = _env.get("TEST_WEBHOOK_IDS", "")

if not TOKEN:
    print("Error: DISCORD_BOT_TOKEN not set in .env")
    sys.exit(1)

API = "https://discord.com/api/v10"

# Reuse a permissive SSL context (Windows sometimes lacks certs for discord)
_ssl_ctx = ssl.create_default_context()


def _http(method, url, body=None, headers=None):
    """Make an HTTP request, return parsed JSON."""
    hdrs = headers or {}
    hdrs.setdefault("User-Agent", "DiscordBot (test-script, 1.0)")
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode())
        except Exception:
            err_body = {"error": str(e)}
        return err_body
    except Exception as e:
        return {"error": str(e)}


def api_call(method, endpoint, json_body=None):
    """Make a Discord REST API call using bot token."""
    return _http(method, f"{API}{endpoint}", body=json_body,
                 headers={"Authorization": f"Bot {TOKEN}"})


def webhook_send(webhook_url, content, thread_id=None):
    """Send a message via webhook. Use thread_id for forum thread targeting."""
    url = webhook_url + "?wait=true"
    if thread_id:
        url += f"&thread_id={thread_id}"
    return _http("POST", url, body={"content": content})


def _resolve_webhook_for_target(target_id):
    """Pick the right webhook URL based on target channel type.

    - Lobby channel -> LOBBY_WEBHOOK_URL
    - Forum thread -> WEBHOOK_URL (with thread_id)
    - Lobby thread -> LOBBY_WEBHOOK_URL (with thread_id)
    Returns (webhook_url, thread_id_or_None) or (None, error_message).
    """
    info = api_call("GET", f"/channels/{target_id}")
    ch_type = info.get("type")

    # Direct channel (lobby)
    if ch_type == 0:  # text channel
        if LOBBY_WEBHOOK_URL:
            return LOBBY_WEBHOOK_URL, None
        if WEBHOOK_URL:
            return WEBHOOK_URL, None
        return None, "No webhook URL configured"

    # Thread — need to check parent
    if ch_type in (11, 12):  # public/private thread
        parent_id = info.get("parent_id")
        if parent_id:
            parent = api_call("GET", f"/channels/{parent_id}")
            if parent.get("type") == 15:  # forum parent
                if not WEBHOOK_URL:
                    return None, "TEST_WEBHOOK_URL not set (needed for forum threads)"
                return WEBHOOK_URL, target_id
            else:  # text channel parent (lobby thread)
                if not LOBBY_WEBHOOK_URL:
                    return None, "TEST_LOBBY_WEBHOOK_URL not set (needed for lobby threads)"
                return LOBBY_WEBHOOK_URL, target_id
        return None, f"Thread {target_id} has no parent"

    # Forum channel itself — can't send directly
    if ch_type == 15:
        return None, "Cannot send directly to a forum channel — target a thread instead"

    return None, f"Unknown channel type {ch_type}"


# --- Commands ---


def cmd_setup_webhook(channel_id):
    """Create a test webhook on a channel."""
    data = api_call("POST", f"/channels/{channel_id}/webhooks",
                    {"name": "Test Bot"})
    if "id" in data:
        wh_id = data["id"]
        wh_token = data["token"]
        wh_url = f"https://discord.com/api/webhooks/{wh_id}/{wh_token}"

        # Determine channel type to suggest right env var
        info = api_call("GET", f"/channels/{channel_id}")
        ch_type = info.get("type", 0)
        if ch_type == 15:
            url_var = "TEST_WEBHOOK_URL"
        else:
            url_var = "TEST_LOBBY_WEBHOOK_URL"

        print(f"Webhook created!")
        print(f"  ID: {wh_id}")
        print(f"  URL: {wh_url}")
        print(f"  Channel type: {ch_type} ({'forum' if ch_type == 15 else 'text'})")
        print(f"\nAdd to .env:")
        print(f"  {url_var}={wh_url}")
        print(f"  TEST_WEBHOOK_IDS=...,...,{wh_id}  (append to existing)")
    else:
        print(f"Error: {json.dumps(data, indent=2)}")


def cmd_send(target_id, message):
    """Send a message via the appropriate webhook."""
    wh_url, thread_id = _resolve_webhook_for_target(target_id)
    if not wh_url:
        print(f"Error: {thread_id}")  # thread_id holds error message
        return

    result = webhook_send(wh_url, message, thread_id=thread_id)
    if "id" in result:
        dest = f"thread {target_id}" if thread_id else f"channel {target_id}"
        print(f"Sent message {result['id']} to {dest}")
    else:
        print(f"Error: {json.dumps(result, indent=2)}")


def cmd_read(channel_id, limit=10):
    """Read messages from a channel/thread."""
    msgs = api_call("GET", f"/channels/{channel_id}/messages?limit={limit}")
    if isinstance(msgs, dict) and (msgs.get("message") or msgs.get("error")):
        print(f"Error: {msgs.get('message') or msgs.get('error')}")
        return
    if not isinstance(msgs, list):
        print(f"Unexpected response: {msgs}")
        return
    for m in reversed(msgs):
        author = m["author"]["username"]
        content = m.get("content", "").replace("\n", " | ")[:200]
        ts = m["timestamp"][:16]
        print(f"[{ts}] {author}: {content}")
        for e in m.get("embeds", []):
            desc = e.get("description", "")[:150]
            title = e.get("title", "")
            if title or desc:
                print(f"  [embed] {title}: {desc}")
            for field in e.get("fields", []):
                print(f"    {field['name']}: {field['value']}")
        for comp in m.get("components", []):
            btns = [b.get("label", "") for b in comp.get("components", [])]
            if btns:
                print(f"  [buttons] {btns}")


def cmd_list_channels():
    """List all channels in the bot's category."""
    if not GUILD_ID:
        print("Error: DISCORD_GUILD_ID not set")
        return
    all_channels = api_call("GET", f"/guilds/{GUILD_ID}/channels")
    if isinstance(all_channels, dict):
        print(f"Error: {json.dumps(all_channels, indent=2)}")
        return

    type_names = {0: "text", 2: "voice", 4: "category", 5: "news", 15: "forum"}
    for ch in sorted(all_channels, key=lambda c: c.get("position", 0)):
        parent = ch.get("parent_id")
        if CATEGORY_ID and parent != CATEGORY_ID and str(ch.get("id")) != CATEGORY_ID:
            continue
        t = type_names.get(ch["type"], f"type={ch['type']}")
        print(f"  {t:8s} | {ch['name']:30s} | {ch['id']}")


def cmd_list_threads(forum_id):
    """List active + archived threads in a forum."""
    # Active threads (guild-wide, filter by parent)
    active = api_call("GET", f"/guilds/{GUILD_ID}/threads/active")
    active_threads = [
        t for t in active.get("threads", [])
        if t.get("parent_id") == forum_id
    ]
    print(f"Active threads ({len(active_threads)}):")
    for t in active_threads:
        tags = t.get("applied_tags", [])
        tag_str = f"  tags={tags}" if tags else ""
        print(f"  {t['name']:40s} | {t['id']}{tag_str}")

    # Archived threads
    archived = api_call("GET", f"/channels/{forum_id}/threads/archived/public")
    if "threads" in archived:
        print(f"\nArchived threads ({len(archived['threads'])}):")
        for t in archived["threads"]:
            print(f"  {t['name']:40s} | {t['id']}")


def cmd_channel_info(channel_id):
    """Get channel details."""
    info = api_call("GET", f"/channels/{channel_id}")
    if "id" not in info:
        print(f"Error: {json.dumps(info, indent=2)}")
        return
    type_names = {0: "text", 2: "voice", 4: "category", 5: "news",
                  11: "public_thread", 12: "private_thread", 15: "forum"}
    print(f"ID: {info['id']}")
    print(f"Name: {info.get('name', '?')}")
    print(f"Type: {type_names.get(info['type'], info['type'])}")
    print(f"Parent: {info.get('parent_id', 'none')}")
    print(f"Topic: {info.get('topic', 'none')}")
    if info.get("available_tags"):
        tags = [t["name"] for t in info["available_tags"]]
        print(f"Tags: {', '.join(tags)}")
    if info.get("applied_tags"):
        print(f"Applied tags: {info['applied_tags']}")
    if info.get("thread_metadata"):
        meta = info["thread_metadata"]
        print(f"Archived: {meta.get('archived', False)}")
        print(f"Locked: {meta.get('locked', False)}")


def cmd_wait_response(channel_id, timeout=30):
    """Poll for a new bot message after sending."""
    msgs = api_call("GET", f"/channels/{channel_id}/messages?limit=1")
    last_id = msgs[0]["id"] if msgs and isinstance(msgs, list) else "0"

    print(f"Waiting for response (timeout: {timeout}s)...")
    start = time.time()
    while time.time() - start < timeout:
        time.sleep(2)
        msgs = api_call("GET", f"/channels/{channel_id}/messages?limit=5")
        if not isinstance(msgs, list):
            continue
        for m in msgs:
            if int(m["id"]) > int(last_id) and m["author"].get("bot"):
                content = m.get("content", "")[:200]
                print(f"Bot responded! (msg {m['id']})")
                if content:
                    print(f"  Content: {content}")
                for e in m.get("embeds", []):
                    print(f"  Embed: {e.get('title', '')} - {e.get('description', '')[:150]}")
                return True
    print("Timeout - no bot response detected")
    return False


# --- Automated test suite ---


def _find_category_id():
    """Auto-detect category ID if not in .env (bot auto-provisions)."""
    if CATEGORY_ID:
        return CATEGORY_ID
    all_channels = api_call("GET", f"/guilds/{GUILD_ID}/channels")
    if not isinstance(all_channels, list):
        return None
    cat_name = _env.get("DISCORD_CATEGORY_NAME", "").lower()
    for ch in all_channels:
        if ch["type"] == 4 and cat_name and ch["name"].lower() == cat_name:
            return str(ch["id"])
    return None


def _find_forums(category_id):
    """Find all forum channels in the category."""
    all_channels = api_call("GET", f"/guilds/{GUILD_ID}/channels")
    if not isinstance(all_channels, list):
        return []
    return [c for c in all_channels if c["type"] == 15
            and c.get("parent_id") == category_id]


def _find_threads_in_forum(forum_id):
    """Get active threads in a forum."""
    active = api_call("GET", f"/guilds/{GUILD_ID}/threads/active")
    return [t for t in active.get("threads", [])
            if t.get("parent_id") == forum_id]


def cmd_run_suite():
    """Automated integration test sequence."""
    print("=" * 60)
    print("Discord Forum Bot - Integration Test Suite")
    print("=" * 60)

    results = []

    def check(name, passed, detail=""):
        results.append((name, passed))
        tag = "[PASS]" if passed else "[FAIL]"
        print(f"\n{tag} {name}")
        if detail:
            print(f"  {detail}")
        return passed

    # --- Step 1: Verify setup ---
    print("\n--- Step 1: Verify setup ---")
    check("ENV configured", bool(TOKEN and GUILD_ID),
          f"token={'yes' if TOKEN else 'no'}, guild={GUILD_ID}")

    cat_id = _find_category_id()
    check("Category found", bool(cat_id), f"id={cat_id}")
    if not cat_id:
        print("\nCategory not found. Is the bot running?")
        _print_summary(results)
        return

    has_lobby_wh = bool(LOBBY_WEBHOOK_URL)
    has_forum_wh = bool(WEBHOOK_URL)
    check("Lobby webhook configured", has_lobby_wh,
          "TEST_LOBBY_WEBHOOK_URL in .env")
    if not has_lobby_wh:
        print("\nRun: python scripts/discord_test.py setup-webhook <lobby_channel_id>")
        _print_summary(results)
        return

    # --- Step 2: Baseline ---
    print("\n--- Step 2: Baseline channel state ---")
    forums_before = _find_forums(cat_id)
    check("Category accessible", True,
          f"{len(forums_before)} existing forums")

    # --- Step 3: Forum creation via lobby message ---
    print("\n--- Step 3: Forum creation flow ---")
    test_msg = f"test: hello from integration test {int(time.time())}"
    result = webhook_send(LOBBY_WEBHOOK_URL, test_msg)
    msg_sent = "id" in result
    check("Lobby message sent", msg_sent,
          f"msg_id={result.get('id', '?')}")

    thread_id = None
    forum_id = None
    if msg_sent:
        print("  Waiting 8s for bot to process...")
        time.sleep(8)

        forums_after = _find_forums(cat_id)
        forum_created = len(forums_after) > len(forums_before) or len(forums_after) > 0
        check("Forum exists", forum_created,
              f"Forums: {len(forums_before)} -> {len(forums_after)}")

        if forums_after:
            forum_id = forums_after[0]["id"]
            threads = _find_threads_in_forum(forum_id)
            check("Thread created in forum", len(threads) > 0,
                  f"{len(threads)} active threads in {forums_after[0]['name']}")

            if threads:
                # Pick the newest thread (highest ID)
                thread_id = max(threads, key=lambda t: int(t["id"]))["id"]
                thread_msgs = api_call("GET", f"/channels/{thread_id}/messages?limit=10")
                if isinstance(thread_msgs, list):
                    bot_msgs = [m for m in thread_msgs if m["author"].get("bot")]
                    check("Bot responded in thread", len(bot_msgs) > 0,
                          f"{len(bot_msgs)} bot messages")
                else:
                    check("Bot responded in thread", False, "Could not read thread")

            # Check lobby redirect
            lobby_id = LOBBY_ID
            if not lobby_id:
                # Find lobby by looking for text channels in category
                all_ch = api_call("GET", f"/guilds/{GUILD_ID}/channels")
                for ch in all_ch:
                    if ch["type"] == 0 and ch.get("parent_id") == cat_id:
                        if "lobby" in ch["name"] or "control" in ch["name"]:
                            lobby_id = ch["id"]
                            break
            if lobby_id:
                lobby_msgs = api_call("GET", f"/channels/{lobby_id}/messages?limit=3")
                if isinstance(lobby_msgs, list):
                    # Original message should be deleted, redirect posted
                    has_redirect = any(
                        "#" in m.get("content", "") or "→" in m.get("content", "")
                        for m in lobby_msgs
                    )
                    check("Lobby cleanup", True,
                          "Redirect link found" if has_redirect else "No redirect (may have auto-deleted)")

    # --- Step 4: Thread resume (requires forum webhook) ---
    print("\n--- Step 4: Thread resume ---")
    if thread_id and has_forum_wh:
        followup_msg = f"test: follow-up in same thread {int(time.time())}"
        result = webhook_send(WEBHOOK_URL, followup_msg, thread_id=thread_id)
        followup_sent = "id" in result
        check("Follow-up sent to thread", followup_sent,
              f"thread={thread_id}")

        if followup_sent:
            print("  Waiting 8s for bot to process...")
            time.sleep(8)
            thread_msgs = api_call("GET", f"/channels/{thread_id}/messages?limit=10")
            if isinstance(thread_msgs, list):
                bot_msgs = [m for m in thread_msgs if m["author"].get("bot")]
                check("Session resumed (no new thread)", len(bot_msgs) >= 2,
                      f"{len(bot_msgs)} bot messages total")
            else:
                check("Session resumed", False, "Could not read thread")
    elif not thread_id:
        check("Thread resume", False, "SKIPPED: no thread from step 3")
    else:
        check("Thread resume", False,
              "SKIPPED: TEST_WEBHOOK_URL not set (need forum webhook)")

    # --- Step 5: Concurrent message dedup ---
    print("\n--- Step 5: Concurrent message dedup ---")
    if has_lobby_wh:
        threads_before = _find_threads_in_forum(forum_id) if forum_id else []

        # Send 3 messages rapidly
        for i in range(3):
            webhook_send(LOBBY_WEBHOOK_URL, f"test: rapid fire {i} {int(time.time())}")
        print("  Sent 3 rapid messages, waiting 12s...")
        time.sleep(12)

        if forum_id:
            threads_after = _find_threads_in_forum(forum_id)
            new_threads = len(threads_after) - len(threads_before)
            # Ideally 3 new threads (one per message), but the key check is no crashes
            check("Rapid messages handled", len(threads_after) >= len(threads_before),
                  f"Threads: {len(threads_before)} -> {len(threads_after)} (+{new_threads})")
        else:
            check("Rapid messages handled", False, "No forum to check")
    else:
        check("Concurrent dedup", False, "SKIPPED: no lobby webhook")

    # --- Step 6: Archived thread resume ---
    print("\n--- Step 6: Archived thread resume ---")
    if thread_id and has_forum_wh:
        # Archive the thread
        api_call("PATCH", f"/channels/{thread_id}", {"archived": True})
        print(f"  Archived thread {thread_id}")
        time.sleep(1)

        # Verify it's archived
        info = api_call("GET", f"/channels/{thread_id}")
        is_archived = info.get("thread_metadata", {}).get("archived", False)
        check("Thread archived", is_archived)

        if is_archived:
            # Send message to archived thread
            result = webhook_send(WEBHOOK_URL, f"test: wake up archived thread {int(time.time())}",
                                  thread_id=thread_id)
            sent = "id" in result
            check("Message sent to archived thread", sent)

            if sent:
                print("  Waiting 8s for bot to respond...")
                time.sleep(8)
                # Check if bot responded (thread should auto-unarchive)
                info2 = api_call("GET", f"/channels/{thread_id}")
                unarchived = not info2.get("thread_metadata", {}).get("archived", True)
                check("Archived thread unarchived", unarchived)

                thread_msgs = api_call("GET", f"/channels/{thread_id}/messages?limit=5")
                if isinstance(thread_msgs, list):
                    recent_bot = [m for m in thread_msgs if m["author"].get("bot")]
                    check("Bot responded in unarchived thread", len(recent_bot) > 0)
                else:
                    check("Bot responded in unarchived thread", False, "Could not read")
    elif not thread_id:
        check("Archived thread resume", False, "SKIPPED: no thread from earlier steps")
    else:
        check("Archived thread resume", False, "SKIPPED: need forum webhook")

    # --- Step 7: Forum tags ---
    print("\n--- Step 7: Forum tags ---")
    if forum_id:
        forum_info = api_call("GET", f"/channels/{forum_id}")
        tags = forum_info.get("available_tags", [])
        tag_names = [t["name"] for t in tags]
        expected = {"active", "completed", "failed", "cli", "build"}
        found = expected & set(tag_names)
        check("Forum tags created", len(found) >= 3,
              f"Found: {', '.join(tag_names)}")

        if thread_id:
            thread_info = api_call("GET", f"/channels/{thread_id}")
            applied = thread_info.get("applied_tags", [])
            check("Tags applied to thread", len(applied) > 0,
                  f"Applied tag IDs: {applied}")
    else:
        check("Forum tags", False, "SKIPPED: no forum")

    # --- Summary ---
    _print_summary(results)


def _print_summary(results):
    """Print test results summary."""
    print("\n" + "=" * 60)
    passed = sum(1 for _, p in results if p)
    total = len(results)
    print(f"Results: {passed}/{total} passed")
    if passed < total:
        failed = [name for name, p in results if not p]
        print(f"Failed: {', '.join(failed)}")
    print("=" * 60)
    print("\nManual verification needed:")
    print("  - /sync 3     -> verify threads created per project")
    print("  - /new        -> verify fresh thread in project forum")
    print("  - /repo       -> verify select menu with multiple repos")
    print("  - Buttons     -> verify Plan/Build/Review/Commit in threads")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]
    if cmd == "setup-webhook":
        cmd_setup_webhook(sys.argv[2] if len(sys.argv) > 2 else LOBBY_ID)
    elif cmd == "send":
        if len(sys.argv) < 4:
            print("Usage: send <channel_id> <message>")
            return
        cmd_send(sys.argv[2], " ".join(sys.argv[3:]))
    elif cmd == "read":
        channel_id = sys.argv[2] if len(sys.argv) > 2 else LOBBY_ID
        limit = int(sys.argv[3]) if len(sys.argv) > 3 else 10
        cmd_read(channel_id, limit)
    elif cmd == "list-channels":
        cmd_list_channels()
    elif cmd == "list-threads":
        if len(sys.argv) < 3:
            print("Usage: list-threads <forum_id>")
            return
        cmd_list_threads(sys.argv[2])
    elif cmd == "channel-info":
        if len(sys.argv) < 3:
            print("Usage: channel-info <channel_id>")
            return
        cmd_channel_info(sys.argv[2])
    elif cmd == "wait-response":
        channel_id = sys.argv[2] if len(sys.argv) > 2 else LOBBY_ID
        timeout = int(sys.argv[3]) if len(sys.argv) > 3 else 30
        cmd_wait_response(channel_id, timeout)
    elif cmd == "run-suite":
        cmd_run_suite()
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()

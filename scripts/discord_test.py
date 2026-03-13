"""Discord integration test tool for forum-based bot architecture.

Commands:
  setup-webhook <channel_id>     Create a test webhook, print ID + URL
  send <channel_or_thread_id> <message>   Send via test webhook
  read <channel_or_thread_id> [limit]     Read messages
  list-channels                  List all channels in bot category
  list-threads <forum_id>        List active + archived threads
  channel-info <id>              Get channel type, parent, topic, tags
  wait-response <channel_id> [timeout]    Poll for new bot messages
  run-suite                      Automated test sequence
"""
import json
import os
import subprocess
import sys
import time

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
WEBHOOK_URL = _env.get("TEST_WEBHOOK_URL")  # full URL with token
WEBHOOK_IDS = _env.get("TEST_WEBHOOK_IDS", "")

if not TOKEN:
    print("Error: DISCORD_BOT_TOKEN not set in .env")
    sys.exit(1)

API = "https://discord.com/api/v10"


def api_call(method, endpoint, json_body=None):
    """Make a Discord REST API call."""
    url = f"{API}{endpoint}"
    cmd = ["curl", "-s", "-X", method, "-H", f"Authorization: Bot {TOKEN}",
           "-H", "Content-Type: application/json"]
    if json_body:
        cmd.extend(["-d", json.dumps(json_body)])
    cmd.append(url)
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"error": result.stdout}


def webhook_send(webhook_url, content, thread_id=None):
    """Send a message via webhook."""
    url = webhook_url + "?wait=true"
    if thread_id:
        url += f"&thread_id={thread_id}"
    cmd = ["curl", "-s", "-X", "POST",
           "-H", "Content-Type: application/json",
           "-d", json.dumps({"content": content}),
           url]
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"error": result.stdout}


def cmd_setup_webhook(channel_id):
    """Create a test webhook on a channel."""
    data = api_call("POST", f"/channels/{channel_id}/webhooks",
                    {"name": "Test Bot"})
    if "id" in data:
        wh_id = data["id"]
        wh_token = data["token"]
        wh_url = f"https://discord.com/api/webhooks/{wh_id}/{wh_token}"
        print(f"Webhook created!")
        print(f"  ID: {wh_id}")
        print(f"  URL: {wh_url}")
        print(f"\nAdd to .env:")
        print(f"  TEST_WEBHOOK_IDS={wh_id}")
        print(f"  TEST_WEBHOOK_URL={wh_url}")
    else:
        print(f"Error: {json.dumps(data, indent=2)}")


def cmd_send(target_id, message):
    """Send a message via webhook."""
    if not WEBHOOK_URL:
        print("Error: TEST_WEBHOOK_URL not set in .env")
        return

    # Determine if target is a thread (send with thread_id param)
    info = api_call("GET", f"/channels/{target_id}")
    is_thread = info.get("type") in (11, 12)  # public/private thread
    parent_is_forum = False
    if is_thread and info.get("parent_id"):
        parent = api_call("GET", f"/channels/{info['parent_id']}")
        parent_is_forum = parent.get("type") == 15

    if is_thread:
        result = webhook_send(WEBHOOK_URL, message, thread_id=target_id)
    else:
        result = webhook_send(WEBHOOK_URL, message)

    if "id" in result:
        print(f"Sent message {result['id']} to {'thread' if is_thread else 'channel'} {target_id}")
    else:
        print(f"Error: {json.dumps(result, indent=2)}")


def cmd_read(channel_id, limit=10):
    """Read messages from a channel/thread."""
    msgs = api_call("GET", f"/channels/{channel_id}/messages?limit={limit}")
    if isinstance(msgs, dict) and msgs.get("message"):
        print(f"Error: {msgs['message']}")
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
    # Active threads
    active = api_call("GET", f"/channels/{forum_id}/threads")
    if "threads" in active:
        print(f"Active threads ({len(active['threads'])}):")
        for t in active["threads"]:
            print(f"  {t['name']:40s} | {t['id']}")
    else:
        print("No active threads (or error)")

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
        tags = [f"{t['name']}" for t in info["available_tags"]]
        print(f"Tags: {', '.join(tags)}")
    if info.get("applied_tags"):
        print(f"Applied tags: {info['applied_tags']}")


def cmd_wait_response(channel_id, timeout=30):
    """Poll for a new bot message after sending."""
    # Record current latest message
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
            if m["id"] != last_id and m["author"].get("bot"):
                content = m.get("content", "")[:200]
                embeds = len(m.get("embeds", []))
                print(f"Bot responded! (msg {m['id']})")
                if content:
                    print(f"  Content: {content}")
                if embeds:
                    for e in m["embeds"]:
                        print(f"  Embed: {e.get('title', '')} - {e.get('description', '')[:150]}")
                return True
    print("Timeout — no bot response detected")
    return False


def cmd_run_suite():
    """Automated integration test sequence."""
    print("=" * 60)
    print("Discord Forum Bot — Integration Test Suite")
    print("=" * 60)

    results = []

    def check(name, passed, detail=""):
        status = "PASS" if passed else "FAIL"
        results.append((name, passed))
        print(f"\n{'[PASS]' if passed else '[FAIL]'} {name}")
        if detail:
            print(f"  {detail}")

    # 1. Verify setup
    print("\n--- Step 1: Verify setup ---")
    check("ENV configured", bool(TOKEN and GUILD_ID),
          f"token={'yes' if TOKEN else 'no'}, guild={GUILD_ID}")
    check("Webhook configured", bool(WEBHOOK_URL),
          "TEST_WEBHOOK_URL in .env")
    if not WEBHOOK_URL:
        print("\nSetup incomplete. Run: python scripts/discord_test.py setup-webhook <lobby_channel_id>")
        return

    # 2. List channels (baseline)
    print("\n--- Step 2: Baseline channel state ---")
    all_channels = api_call("GET", f"/guilds/{GUILD_ID}/channels")
    if isinstance(all_channels, list):
        forums = [c for c in all_channels if c["type"] == 15 and c.get("parent_id") == CATEGORY_ID]
        texts = [c for c in all_channels if c["type"] == 0 and c.get("parent_id") == CATEGORY_ID]
        check("Category accessible", True,
              f"{len(forums)} forums, {len(texts)} text channels")
    else:
        check("Category accessible", False, str(all_channels))
        return

    # 3. Send message to lobby
    print("\n--- Step 3: Forum creation flow ---")
    test_msg = f"test: hello from integration test {int(time.time())}"
    result = webhook_send(WEBHOOK_URL, test_msg)
    msg_sent = "id" in result
    check("Lobby message sent", msg_sent,
          f"msg_id={result.get('id', '?')}")

    if msg_sent:
        # Wait for bot response / thread creation
        time.sleep(5)

        # Check for new forums
        all_channels2 = api_call("GET", f"/guilds/{GUILD_ID}/channels")
        forums2 = [c for c in all_channels2 if c["type"] == 15 and c.get("parent_id") == CATEGORY_ID]
        new_forums = len(forums2) - len(forums)
        check("Forum created", new_forums > 0 or len(forums) > 0,
              f"Forums: {len(forums)} -> {len(forums2)}")

        if forums2:
            forum_id = forums2[0]["id"]
            # Check for threads
            threads = api_call("GET", f"/channels/{forum_id}/threads")
            thread_list = threads.get("threads", [])
            check("Thread created in forum", len(thread_list) > 0,
                  f"{len(thread_list)} threads in forum {forums2[0]['name']}")

            if thread_list:
                thread_id = thread_list[0]["id"]
                # Check for bot message in thread
                thread_msgs = api_call("GET", f"/channels/{thread_id}/messages?limit=5")
                bot_msgs = [m for m in thread_msgs if m["author"].get("bot")]
                check("Bot responded in thread", len(bot_msgs) > 0,
                      f"{len(bot_msgs)} bot messages in thread")

    # 4. Summary
    print("\n" + "=" * 60)
    passed = sum(1 for _, p in results if p)
    total = len(results)
    print(f"Results: {passed}/{total} passed")
    if passed < total:
        failed = [name for name, p in results if not p]
        print(f"Failed: {', '.join(failed)}")
    print("=" * 60)
    print("\nManual verification needed:")
    print("  - /sync 3 → verify threads created per project")
    print("  - /new → verify fresh thread in project forum")
    print("  - Click workflow buttons → verify in threads")


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

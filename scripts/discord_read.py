"""Read recent messages from a Discord channel via REST API."""
import json
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

TOKEN = None
with open(Path(__file__).resolve().parent.parent / ".env") as f:
    for line in f:
        if line.startswith("DISCORD_BOT_TOKEN="):
            TOKEN = line.split("=", 1)[1].strip()
            break

CHANNEL_ID = sys.argv[1] if len(sys.argv) > 1 else "1481317114319994973"
LIMIT = sys.argv[2] if len(sys.argv) > 2 else "10"

url = f"https://discord.com/api/v10/channels/{CHANNEL_ID}/messages?limit={LIMIT}"
result = subprocess.run(
    ["curl", "-s", "-H", f"Authorization: Bot {TOKEN}", url],
    capture_output=True, text=True,
)
msgs = json.loads(result.stdout)

if isinstance(msgs, dict) and msgs.get("message"):
    print(f"Error: {msgs['message']}")
    sys.exit(1)

for m in reversed(msgs):
    author = m["author"]["username"]
    content = m.get("content", "").replace("\n", " | ")[:200]
    ts = m["timestamp"][:16]
    print(f"[{ts}] {author}: {content}")
    for e in m.get("embeds", []):
        desc = e.get("description", "")[:150]
        if desc:
            print(f"  [embed] {desc}")
    for comp in m.get("components", []):
        btns = [b.get("label", "") for b in comp.get("components", [])]
        if btns:
            print(f"  [buttons] {btns}")

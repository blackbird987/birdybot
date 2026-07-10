"""Fetch a user's X/Twitter timeline as a writing-style corpus.

Reuses the DegenAI project's paid Twitter API credentials (Basic tier) by
reading its gitignored settings.json at runtime — no secrets are copied
into this repo.

Output (default data/x_corpus/):
  raw_tweets.jsonl  — full API tweet objects, one per line
  corpus.md         — cleaned, human/LLM-readable corpus with engagement stats
  meta.json         — fetch metadata (handle, user id, counts, timestamp)

Usage:
  python scripts/fetch_x_corpus.py [--handle Daab1rD] [--max 800] [--out data/x_corpus]

Budget note: each fetched tweet counts against the shared 10k/month Basic-tier
read cap that the DegenAI bot also draws from. Default of 800 = 8% of a month.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEGENAI_SETTINGS = Path(
    r"C:\Users\Quincy\Desktop\Programming\DegenAI\AIAgent\AIAgent\settings.json"
)
API = "https://api.twitter.com/2"
TWEET_FIELDS = "created_at,public_metrics,referenced_tweets,conversation_id,lang"
PAGE_SIZE = 100


def load_bearer(settings_path: Path) -> str:
    settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
    token = settings.get("Twitter", {}).get("TwitterBearerToken")
    if not token:
        sys.exit(f"No TwitterBearerToken found in {settings_path}")
    return token


def api_get(path: str, params: dict, bearer: str) -> dict:
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {bearer}"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                reset = e.headers.get("x-rate-limit-reset")
                wait = max(int(reset) - time.time(), 5) if reset else 60
                wait = min(wait, 900)
                print(f"Rate limited, sleeping {wait:.0f}s...")
                time.sleep(wait)
                continue
            body = e.read().decode("utf-8", errors="replace")[:500]
            sys.exit(f"API error {e.code} on {path}: {body}")
    sys.exit(f"Gave up on {path} after repeated rate limits")


def tweet_kind(tweet: dict) -> str:
    for ref in tweet.get("referenced_tweets", []):
        if ref["type"] == "replied_to":
            return "reply"
        if ref["type"] == "quoted":
            return "quote"
    return "original"


def fetch_timeline(user_id: str, bearer: str, max_tweets: int) -> list[dict]:
    tweets: list[dict] = []
    pagination_token = None
    while len(tweets) < max_tweets:
        params = {
            "max_results": min(PAGE_SIZE, max(max_tweets - len(tweets), 5)),
            "exclude": "retweets",
            "tweet.fields": TWEET_FIELDS,
        }
        if pagination_token:
            params["pagination_token"] = pagination_token
        data = api_get(f"/users/{user_id}/tweets", params, bearer)
        page = data.get("data", [])
        tweets.extend(page)
        print(f"  fetched {len(tweets)} tweets...")
        pagination_token = data.get("meta", {}).get("next_token")
        if not pagination_token or not page:
            break
    return tweets[:max_tweets]


def write_outputs(out_dir: Path, handle: str, user: dict, tweets: list[dict]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    with (out_dir / "raw_tweets.jsonl").open("w", encoding="utf-8") as f:
        for t in tweets:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")

    kinds = {"original": 0, "reply": 0, "quote": 0}
    lines = [
        f"# X corpus: @{handle}",
        f"Fetched {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} — "
        f"{len(tweets)} tweets, newest first.",
        "Format: [date | kind | likes/replies/RTs]",
        "",
    ]
    for t in tweets:
        kind = tweet_kind(t)
        kinds[kind] += 1
        pm = t.get("public_metrics", {})
        lines.append(
            f"[{t.get('created_at', '')[:10]} | {kind} | "
            f"{pm.get('like_count', 0)}L/{pm.get('reply_count', 0)}R/"
            f"{pm.get('retweet_count', 0)}RT]"
        )
        lines.append(t["text"])
        lines.append("")
    (out_dir / "corpus.md").write_text("\n".join(lines), encoding="utf-8")

    meta = {
        "handle": handle,
        "user_id": user["id"],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "tweet_count": len(tweets),
        "kinds": kinds,
        "profile_total_tweets": user.get("public_metrics", {}).get("tweet_count"),
    }
    (out_dir / "meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )
    print(f"Wrote {len(tweets)} tweets to {out_dir} ({kinds})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--handle", default="Daab1rD")
    ap.add_argument("--max", type=int, default=800)
    ap.add_argument("--out", default="data/x_corpus")
    ap.add_argument("--settings", default=str(DEGENAI_SETTINGS))
    args = ap.parse_args()

    bearer = load_bearer(Path(args.settings))
    user_resp = api_get(
        f"/users/by/username/{args.handle}",
        {"user.fields": "public_metrics"},
        bearer,
    )
    user = user_resp["data"]
    print(f"@{args.handle} -> id {user['id']}, "
          f"{user.get('public_metrics', {}).get('tweet_count')} total tweets")

    tweets = fetch_timeline(user["id"], bearer, args.max)
    write_outputs(Path(args.out), args.handle, user, tweets)


if __name__ == "__main__":
    main()

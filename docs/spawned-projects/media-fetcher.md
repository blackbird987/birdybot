# Spawned project: media-fetcher

This build session (t-5987) produced a **standalone repo**, not changes to this
bot. The bot repo is unaffected; this note is just a breadcrumb.

- **Location:** `C:\Users\Quincy\Desktop\Programming\media-fetcher`
- **First commit:** `6d07885` (in that repo, not here)
- **What it is:** agentic media downloader — plain-language request →
  Prowlarr torrent search → Deluge download → Plex library refresh + verify.

## Pipeline

- **Prowlarr** (Docker, `localhost:9696`) — normalized torrent search across
  many indexers (no HTML scraping).
- **Deluge** (`127.0.0.1:58846`) — downloads; completed files move to
  `D:\Utorrent\Completed downloads`.
- **Plex** (`localhost:32400`) — "Films" and "Tv-series" both watch that folder.

## Verified on this machine at build time

- Plex libraries list + refresh — real token, real API.
- Deluge daemon start + magnet add + status decode — sandbox daemon, Sintel
  test magnet (real Deluge state untouched).
- Prowlarr search — awaits one-time setup (Docker running + indexers added +
  API key pasted into media-fetcher/.env).

## Remaining one-time setup (needs the user)

1. Start Docker Desktop, then `docker compose up -d` in the media-fetcher repo.
2. Open `localhost:9696`, run the wizard, add indexers (ext.to / 1337x / EZTV).
3. Copy Prowlarr's API key into `PROWLARR_API_KEY` in `media-fetcher/.env`.
4. Register the repo with the bot (`/repo add media-fetcher <path>`).

This branch (claude-bot/t-5987) can be merged or discarded — it carries only
this note; the real project lives in its own repo.

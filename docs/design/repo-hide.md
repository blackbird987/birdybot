# A repo you are not using is hidden, not removed

Twenty registered repos are twenty forums in one category, and most of them
are not being worked on this month. `/repo hide <name...>` parks a repo;
`/repo unhide <name...>` brings it back. Nothing is unregistered, nothing is
deleted, and `/repo list` still shows a hidden repo under its own `Hidden:`
heading; that listing is how you find one again.

Three things that must not drift:

- **Permission overwrites are not the mechanism, and never were.** Discord
  shows the server owner every channel regardless of overwrites, so denying
  `view_channel` hides nothing from the one person who asked for it. The
  forum is *moved*, into a `<bot category> · Archive` category created on
  demand next to the main one (`channels.ensure_archive_category`,
  `forums._move_repo_forum`). The channel, its threads and its pinned posts
  survive the move untouched. The move must **not** pass
  `sync_permissions=True`: a repo forum can carry its own overwrites, since a
  per-repo access grant is one extra entry on the forum rather than on the
  category, and syncing replaces the forum's list with the destination
  category's -- silently revoking that guest, with an unhide syncing to the
  main category and still not restoring it. Confirmed against the live API:
  a synced move took a forum from 4 overwrites to 3 and dropped the grant
  role; an unsynced one round-tripped all 4. The move alone hides the forum;
  it keeps the private overwrites it was created with either way.
- **`list_repos()` stays unfiltered, deliberately.** It has dozens of callers
  that resolve a repo *path* through it: resume, merge, worktree recovery,
  deploy, session fork. A hidden repo has to keep working for all of them, so
  filtering there would turn "hide" into "quietly break". Hiding is display
  state: only surfaces that draw a list for a human read
  `list_active_repos()` / `is_repo_dormant()`: the dashboard's Projects
  field, the `/repo` switch menu, the `/new` repo picker, and the startup
  reconcile loops that would otherwise redraw control rooms and repair pin
  slots in a forum nobody is looking at. A hidden repo is still switchable
  and still startable *by name*; naming one is intent.
- **Work in a hidden repo un-hides it.** A spawn landing, a self-wake firing
  or a message in one of its threads would otherwise post into a parked
  forum and be seen by nobody. `wake_repo_if_dormant` is the one
  implementation, and it has exactly three callers, one per way something
  can appear in a forum:
  `get_or_create_session_thread` (a new session thread; it sits **above**
  that function's already-has-a-thread early return, because a *resumed*
  session in a hidden repo is precisely the case that would keep posting
  into the parked forum), the forum-message route in `bot.py` (the user
  types in an existing thread), and `_replay_to_thread` (every unattended
  resume: a fired self-wake, a tripped `/watch`, a `--here` schedule, an
  orchestrator wave join, a post-reboot replay -- none of which touch
  `get_or_create_session_thread`, since their thread already exists). A
  plain schedule deliberately wakes nothing: its result is broadcast to the
  owner, not posted into the repo forum, so there is nothing parked to
  miss. The flag is cleared even if the channel move fails, because a repo
  left flagged dormant while its work runs is the failure this exists to
  prevent. This is what makes hiding safe enough to do casually.

`add_repo` and `remove_repo` both discard the dormant flag, so a name
re-registered later cannot come back invisible with nothing on screen to
explain why. `hide` and `unhide` are reserved repo names, and both take
several names at once.

Hidden repos are deliberately **included** in the "Your Repos" block every
session's system prompt carries (`prompt_build._build_repo_catalog_block`,
reading the unfiltered `list_repos()`), marked `(hidden)`. Hiding takes a repo
out of the user's sight, which is exactly when they are most likely to forget
it exists and ask for it to be built again; the catalog is what lets a session
say "that is `smarthome`, it is hidden" instead of running `/repo create`.
`/repo create` refuses a near-duplicate of a hidden repo for the same reason,
and sessions may emit `[BOT_CMD: /repo hide|unhide ...]` themselves.

Harness: `python scripts/test_repo_hide.py`, `python scripts/test_repo_catalog.py`

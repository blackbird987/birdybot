# Moving the bot to Linux

Audit date: 2026-08-04. Target: Fedora KDE on the new 4 TB NVMe, replacing
Windows 10 as the daily driver.

**Verdict: the code is cross-platform. Only the configuration is not.**
Nothing in `bot/` needs porting. Every Windows-specific branch already has a
working POSIX counterpart, and the single Windows-only dependency (`tzdata`)
is gated behind a `sys_platform == "win32"` marker so Linux skips it.

---

## What already works on Linux, unchanged

- **All dependencies** — `discord.py`, `python-dotenv`, `httpx`, `cryptography`,
  `psutil` are pure-Python and cross-platform.
- **Killing a runaway Claude process** — `taskkill /T /F` on Windows,
  `os.killpg(..., SIGKILL)` on POSIX. Both implemented
  (`bot/engine/usage.py`, `bot/engine/sensors.py`).
- **Opening a login terminal for `claude /login`** — spawns `cmd.exe` on
  Windows, and on POSIX tries `x-terminal-emulator`, `gnome-terminal`,
  `konsole`, `xterm` in turn. **Konsole is the Fedora KDE default**, so this
  path is covered (`bot/services/auth_sync.py`).
- **"Can this host show a window?"** — checks the Windows station on Windows,
  `$DISPLAY` / `$WAYLAND_DISPLAY` on POSIX.
- **Hiding subprocess console windows** — `CREATE_NO_WINDOW` is applied only
  on Windows and is an empty dict elsewhere (`bot/config.py`).
- **Case-insensitive path comparison** — applied only when `os.name == "nt"`,
  which is the correct behaviour (`bot/claude/parser.py`).
- **Managed settings lookup** — has a real Linux branch
  (`/etc/claude-code/managed-settings.json`, `bot/claude/models.py`).
- **Shutdown signals** — `SIGTERM` is registered on both platforms.

## What you lose

- **Outlook integration** (`bot/services/outlook.py`) — needs `pywin32` and a
  Windows Outlook install. It is optional and off by default, so it simply
  becomes unavailable. Nothing else depends on it.

That is the entire loss list.

---

## Pre-flight — do this on Windows, before the switch

1. **Preview the path rewrite** so there are no surprises later:

   ```
   python scripts/migrate_to_linux.py --linux-home /home/<your-linux-user>
   ```

   Dry-run only; writes nothing. From Git Bash, prefix `MSYS_NO_PATHCONV=1`
   or the shell mangles the `/home/...` argument into a Windows path.

2. **Note your Linux username now.** Every rewritten path depends on it. If
   it is not `quincy`, pass `--linux-home` explicitly at every step below.

3. **Copy the whole `Desktop/Programming` tree to the 4 TB drive.** The bot
   is useless without the repos it manages — all 13 registered ones.

4. **Confirm the `.env` secrets are backed up.** Discord token, OpenAI key,
   Twitter bearer token, Anthropic key. These are platform-neutral text and
   carry over as-is, but they only exist in that one file.

---

## On Linux — ordered steps

### 1. Prerequisites

Python 3.11 or newer (`requires-python = ">=3.11"`; Fedora ships well past
this), plus git. Then install the **Claude Code CLI** the same way you did on
Windows and confirm it lands on `PATH`:

```
python3 --version
which claude
```

### 2. Virtual environment

```
cd ~/Desktop/Programming/claude-telegram-bot
python3 -m venv .venv
.venv/bin/pip install -e .
```

### 3. Rewrite the paths

```
.venv/bin/python scripts/migrate_to_linux.py            # preview
.venv/bin/python scripts/migrate_to_linux.py --apply    # commit
```

This rewrites, backing up originals to `*.windows.bak`:

- `.env` — `CLAUDE_BINARY` (resolved from the real `PATH` when run on Linux),
  `REPOS_BASE_DIR`, `CLAUDE_ACCOUNTS`
- `data/state.json` — all 13 registered repo paths, plus `repo_path`,
  `worktree_path` and `session_account` across the instance history

It reports, rather than guesses at, any absolute Windows path outside your
old home directory.

### 4. Clear the stale worktrees

Two build worktrees (`t-3904`, `t-4262`) are registered with absolute Windows
gitdirs and cannot survive the move:

```
git worktree prune
git worktree list          # should show only the main checkout
rm -rf .worktrees/t-3904 .worktrees/t-4262
```

### 5. Sign in

You have two Claude accounts configured (`.claude` and `.claude-klerk`).
Either copy those directories over from Windows, or re-authenticate:

```
CLAUDE_CONFIG_DIR=~/.claude        claude    # then /login
CLAUDE_CONFIG_DIR=~/.claude-klerk  claude    # then /login
```

Re-authenticating is the cleaner option — a copied credential file that has
lost its refresh token gets silently skipped at spawn time.

### 6. Start it

Manual:

```
chmod +x start.sh
./start.sh
```

Permanent (preferred — restarts on crash, survives reboot):

```
mkdir -p ~/.config/systemd/user
cp scripts/claude-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claude-bot
sudo loginctl enable-linger $USER      # REQUIRED, see below
```

**`enable-linger` is not optional.** Without it a systemd *user* service stops
when you log out of the desktop and does not start at boot until you log in
again — meaning the bot is down exactly when you are away from the machine
and relying on your phone.

The service is a *user* unit on purpose: the bot spawns the Claude CLI, which
reads credentials from `$HOME/.claude`. A system unit or a root service would
look at the wrong home and every session would come back signed out.

### 7. Verify

```
python scripts/smoke_test.py
tail -n 50 data/logs/bot.log            # no ERROR / CRITICAL / Traceback
journalctl --user -u claude-bot -f      # if using systemd
```

Then from Discord: `/repo list` (all 13 should resolve), `/status`, and one
throwaway query in a small repo to confirm the CLI actually spawns.

---

## Working from both machines at once

The Linux desktop is not a replacement for the Windows laptop — both drive the
same repo. Two things have to be true for that to be painless, and neither is
automatic.

### Line endings are pinned, and the laptop needs one reset

`.gitattributes` forces LF everywhere, in history and on disk, on both
platforms. Batch files (`.bat`, `.cmd`, `.ps1`, `.vbs`) keep CRLF because
`cmd.exe` mis-parses multi-line LF batch.

Before this existed each platform guessed via `core.autocrlf`, and the guesses
disagreed: the Windows checkout held CRLF against an LF history, so the same
clone read *clean* on Windows and *121 files modified* on Linux. A single
commit from the Linux side would have flipped every file, turning every
subsequent diff, code review and worktree merge into whole-file noise.

**On the Windows laptop, once, after pulling the commit that added
`.gitattributes`:**

```powershell
git rm --cached -r .
git reset --hard
```

That re-checks-out every file under the new policy. Do it with no builds in
flight, and merge or discard any open worktrees first — they were created
under the old policy. Skip it and the laptop shows a one-time wave of phantom
modifications.

Both machines can then be verified with:

```bash
python scripts/check_portability.py
```

which fails loudly on CRLF creeping back into the index, a `.sh` that lost its
executable bit, a filename Windows cannot check out, a hardcoded drive letter
outside a platform branch, or a `.claude/test.json` command that only runs on
one OS.

### Config is per-machine and must stay that way

`.env` and `data/` are both gitignored, so each machine keeps its own paths,
its own `state.json` and its own PID file. **Do not copy `.env` between the
two** — `CLAUDE_BINARY`, `REPOS_BASE_DIR` and `CLAUDE_ACCOUNTS` are all
absolute and machine-specific. `scripts/migrate_to_linux.py` leaves
`# windows-original:` comments above each rewritten line, so the Windows value
is recoverable if you ever need to read it back.

Session history is likewise per-machine; see the first caveat below.

### Starting and stopping the bot

`scripts/botctl.py` works on both:

```bash
python scripts/botctl.py start | stop | restart | status
```

The `.bat` files are kept for double-clicking from Explorer, and `start.sh` /
the systemd user unit remain the Linux conveniences — but anything scripted
should use `botctl.py`, because it is the only entry point that exists on both
platforms. `.claude/test.json` points at it for exactly that reason.

---

## Caveats

- **Claude CLI session history does not follow you.** The CLI stores past
  sessions under `<account dir>/projects/<encoded-cwd>/` — that is each entry
  in `CLAUDE_ACCOUNTS`, *not* `~/.claude`, unless the two happen to coincide
  (they do on Windows; on Linux they routinely don't). The encoding embeds the
  absolute path — `C--Users-Quincy-...` on Windows, `-run-media-...` on Linux.
  Old conversations are not lost, but they will not be found by
  `/session list` or resumed under the new paths. New sessions are unaffected.
  Rewriting them would mean rewriting the `cwd` inside every JSONL record; not
  worth the risk for history.

- **Path decoding is lossy in both directions, by design.** The encoding
  flattens `:`, `\`, `/` and `.` all to `-`, so a directory containing a
  hyphen (`The-Citadel`) or a dot (`.worktrees`) cannot be perfectly
  reconstructed. The CLI tolerates this as long as `fullPath` on each index
  entry is right, which it is. This predates the migration.

- **Repo-level deploy commands are not checked by this migration.** The
  `aiagent` repo deploys over SSH and `degenDAOAssistan` runs a Python script
  — both fine — but any repo-specific tooling that assumes Windows is out of
  scope here.

- **`REPOS_BASE_DIR` assumes you keep the `Desktop/Programming` layout.** If
  you reorganise during the move, pass the new base to the migration script
  or fix the `.env` by hand afterwards.

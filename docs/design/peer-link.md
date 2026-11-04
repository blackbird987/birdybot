# Machine link: stage 1

The same bot runs on a Linux desktop (Discord app TT-GPT, category The Foundry)
and a Windows work laptop (app office bot, category The Office). They ignore each
other, so the owner carries information between them. This is the first stage of
a link. It ships three things: a permissions check, a tap before auto-update
applies, and a read-only project directory. It does not ship questions or tasks
between machines; those are decided below so they are not derived again.

Every session runs as the bot's uid and can read `.env` and the token. A check
inside a session or on the sender's side is advice, not a boundary. What holds
across machines is what Discord enforces: a message can only be edited by its
author, and a button tap is an interaction carrying a real user id.

## Part 1: permissions (`scripts/peer_perms.py`)

Read-only, GET requests only. For each bot it computes, with Discord's order
(base, `@everyone` overwrite, roles as one, member overwrite):

- what it can see, and whether that includes the other bot's category;
- whether it can give itself more: Manage Roles with its top role above an
  assignable role that has Administrator or can see the other category;
- whether it can remove the other bot: Kick or Ban with its top role above;
- whether it still has what the code uses, derived by grep and listed with the
  call site in `REQUIRED_IN_OWN` and `REQUIRED_AT_SERVER`.

`--simulate` applies the proposal to a copy and prints the steps. A bot cannot
edit its own top role, so the change is the owner's. The steps come from the
script, not from this note. Run it again after any role change: exit 0 means no
finding.

Invariants:

- Unticking Administrator alone is not a boundary. Manage Roles above any other
  Administrator role lets a token assign itself that role, which is why the role
  also moves. Measured in `study/findings/`.
- A managed role cannot be assigned (documented behaviour, not exercised), so it
  is not an escalation. Counting them told the owner to drag below position 6.
- A role can have other holders. The script names anyone whose only Administrator
  comes from the role being changed. A new Administrator role for such a holder
  has to sit above the TT-GPT role, or it is an escalation again.
- Pin Messages (bit 51) is required. Discord's table describes Manage Messages as
  deletion only and lists pinning under its own bit (read 2026-11-04).

## Part 2: a tap before an update (`bot/auto_update.py`)

Without this, any desktop session able to push gets code running on the laptop
through pull, pip install and reboot, with no check. Signing does not help:
sessions can read the key.

With `AUTO_UPDATE_APPROVAL` on (the default, so the laptop needs no `.env` edit),
the loop still fetches on schedule, then posts one card instead of pulling:
commit count, file count, subjects and the buttons Apply update and Later.

- **The buttons carry the sha.** Apply lands that commit with `merge --ff-only`,
  never whatever origin holds at tap time. If origin moved it says so and posts a
  fresh card. A non-fast-forward is refused as before, and the buttons stay.
- **One card per remote sha, across restarts.** The sha is recorded in state
  before the send, so a concurrent poll cannot double post, and the record is put
  back if nothing was delivered. That second half matters: the messenger registers
  after background tasks start, so the first poll often has nowhere to send, and a
  sha recorded then would never be carded at all.
- **The live card survives lobby cleanup.** `_cleanup_lobby` deletes unpinned
  messages at every startup and a card is posted only once, so it spares the
  message whose button matches the recorded sha. Superseded cards go.
- **Subjects are attacker-writable text.** Printable characters only, capped, in a
  code block with backticks replaced, mentions neutralised.
- **The steps after a pull are one function**, `finish_update`: same pip install,
  reboot request and notice for the loop and the tap. Failure paths and the one
  notice per failure are unchanged.
- `_handle_sync_git` was not reused: it pulls and pushes both ways and cannot pin
  a sha.

This is a speed bump and not a boundary. A laptop session with a shell can still
pull by itself, and a tap on a phone does not read the code. What it adds is that
a push from the desktop no longer runs on the laptop by itself.

The first update that carries this code still arrives through the old flow,
because the old code is what is running. The one after it needs a tap.

## Part 3: project directory (`bot/discord/peer_link.py`, `scripts/peer.py`)

`PEER_CHANNEL_ID`, `PEER_BOT_ID` and `MACHINE_NAME` all set, or fully inert: no
task, no prompt text, no channel access. One peer, not a list.

**Publisher.** Started from `bot/app.py`. Every 5 minutes it edits one message
owned by this bot, found by author id. It creates one only after a history read
succeeded and found none, so a failed read cannot produce a second message. It
edits on every tick even when only the alive time changed, because that is what
the other side's age is read from, and at most once per interval. It never fails
the bot: errors log and retry, an unreachable channel at startup stops the task.

It reads repos off the event loop with `GIT_SAFE`, `--no-optional-locks` and a
10s timeout, and never fetches. A repo that cannot be read says so and the others
survive. A record over 1900 characters drops repos from the end, hidden ones
first, with an explicit count.

**Record** (`bot/peer_record.py`, stdlib only, shared by both sides). Strict
compact JSON: protocol, machine, version, alive, running, repos, omitted. Each
repo is name, hidden, short sha, branch, commits ahead, uncommitted. No paths,
descriptions, topics, thread names or anything from `.env`. Repo names only: the
owner has work repo names on the laptop and accepted the desktop seeing them.

**Reader.** Standalone like `scripts/instances.py`, works from any repo. It trusts
only a message authored by `PEER_BOT_ID` and not sent through a webhook. One bad
field rejects the whole record. The age comes from Discord's own timestamp, not
the record, so a record cannot claim to be fresh. Older than 15 minutes looks
offline.

Invariants:

- **`on_message` is untouched.** No peer message can start anything.
- **The directory is never injected into a prompt.** Sessions get one static line
  naming the command and saying its output is untrusted, and only owner sessions
  with a shell. The content arrives only when a session runs the command.
- **Repo names are the injection surface,** so the reader holds them to a tight
  regex and refuses the record otherwise. The publisher drops a name that would
  fail instead of losing the whole record.
- **Neither bot gets Manage Messages on the channel and nothing deletes there.**
  The channel is the audit trail.

**Channel.** `scripts/peer_setup.py`, once, with the desktop token: top level,
outside both categories, `@everyone` denied view, view, send and read history for
both bots and the owner. Safe to re-run.

**Windows.** Pathlib, no `os.getuid`, `fcntl` or POSIX-only calls in the new files.
Importing `bot.claude.sandbox` for `GIT_SAFE` is safe because nothing at its module
level is POSIX-only; `prepare_run` is, and is not called. `core.hooksPath=/dev/null`
on Git for Windows is not tested: it names a path with no hooks in it, and the
reads run no hooks anyway.

## Later stages, decided

**Stage 2, questions.** Delivered by a bot directive plus a pending-request record
and `store.add_wake` when the answer lands, NOT by a background script with /watch:

- watches are refused from build sessions, and a new watch replaces an existing
  one on the thread;
- the Windows pid probe `os.kill(pid, 0)` in `bot/engine/watches.py` sends CTRL_C,
  and Git Bash pids are not Windows pids;
- the 6h default timeout fires while the laptop sleeps.

The answering session runs on a temporary export of tracked HEAD files plus session
reports, with redaction and size caps, using an allowlist `--tools Read,Glob` and
`--strict-mcp-config`. Directive dispatch is off on both paths (`lifecycle.py` and
`query.py`), because reply text can otherwise start `/spawn` or a `/watch log=`
read. `--bare` must not be used, since it skips hooks.

Grants are Run once first, then Always per repo for repos without work data, with
per-repo and global caps. Grants in `state.json` are editable by local sessions, so
"only by tap" does not hold locally. Only chat sessions ask at first.

Answers arrive marked untrusted. The wave-join text in `bot/discord/orchestrator.py`
says "continue the work" and is not a usable model for that framing. See also the
findings on the Claude Code sandbox holding for Bash only, and on a masked key coming
back through an echoing host.

The owner's code of conduct limits non-work use of the laptop, so which work repos
are shared is the owner's call.

**Stage 3, tasks.** Deferred. One tap would give a remote prompt a full shell on the
work laptop, and depth limits cannot be enforced against a session holding the token.

## What this does not establish

Effective permissions were computed from REST, not exercised. Thread-level overwrites
and other users of a token are not ruled out. Nothing has run on Windows from here:
the laptop was not touched, and only the portability script and code reading stand
behind that claim. The auto-update card was tested with real git against throwaway
repos and a faked Discord, not on a live button. After the role change TT-GPT still
sits above the office bot's role and keeps Manage Roles, so a desktop session could
edit that role or take it off the office bot; it could not see the laptop's channels
by doing so. Nothing here stops the owner's own sessions on either machine from using
a token directly: this narrows what a token can reach, not who can use it.

Harnesses: `scripts/test_peer_link.py`, `scripts/test_auto_update_approval.py`.

# Machine link

The same bot runs on a Linux desktop (Discord app TT-GPT, category The Foundry)
and a Windows work laptop (app office bot, category The Office). They ignore each
other, so the owner carries information between them. Stage 1 is a permissions
check, a tap before auto-update applies, and a read-only project directory. Stage 2
is questions between machines, each one tapped on the machine that answers. Tasks
between machines are not shipped; that decision is below so it is not derived again.

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
  before the send, so the startup purge cannot catch a card that is out but not yet
  on record, and the record is put back if nothing was delivered. That second half matters: the messenger registers
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
- **Nothing deletes in the channel, and setup denies both bots Manage Messages on
  it,** because after the role change both hold it at server level. The channel is
  the audit trail. This is a default and not a boundary: a bot with Manage Roles or
  Manage Channels can change the overwrite.

**Channel.** `scripts/peer_setup.py`, once, with the desktop token: top level,
outside both categories, `@everyone` denied view, view, send and read history for
both bots and the owner, Manage Messages denied to both bots. Safe to re-run.

**Windows.** Pathlib, no `os.getuid`, `fcntl` or POSIX-only calls in the new files.
Importing `bot.claude.sandbox` for `GIT_SAFE` is safe because nothing at its module
level is POSIX-only; `prepare_run` is, and is not called. `core.hooksPath=/dev/null`
on Git for Windows is not tested: it names a path with no hooks in it, and the
reads run no hooks anyway.

## Stage 2: questions between machines

An owner's chat session writes `[BOT_CMD: /ask_machine repo=X]` with a `~~~ask` body.
The other machine shows a card, the owner taps Run once there, a read-only session
answers from a copy of the repo, and the answer reaches the asking thread, even if
the laptop was asleep for all of it. Nothing runs on a question without that tap.

**Transport.** Questions are never channel messages, because the directory reader
takes the author's newest. Each machine's outbox is a public thread on its own
directory message (same id, 7 day auto-archive). The publisher creates it, and its
existence is how the other side knows this version can answer. The directory record
gained no field. `bot/peer_record.py` parses `q` and `a` strictly. A message that is
not JSON is skipped quietly, an unknown protocol holds the cursor and warns once, a
bad record is skipped and logged. `Message.thread` excludes archived threads, so the
thread is always fetched by id.

**Reader** (`bot/discord/peer_link.py`). Every 30s it reads the peer's directory
message, its thread, and what came after a per-thread cursor, taking only messages by
`PEER_BOT_ID` with no webhook id. Catching up after sleep is the same path. Answers
count only for ids this machine asked. The cursor is per thread, so older threads
drain after a directory message is replaced.

**State** (`bot/store/peer.py`, `bot/engine/peer_questions.py`), in `state.json`,
idempotent by id, first text per id wins, and the tap runs the stored text.

- Asker: asked, answered_received, delivered; or undeliverable, or expired after 7 days.
- Receiver: seen, carded, running, posting, done; or declined, expired.
- Boot recovery (`boot_recovery.recover_peer_questions`): `running` posts `failed`
  because the tap is spent, `posting` resumes at the next part, leftover exports go.

**Asking** (`bot/engine/directives/ask_machine.py`). Only a direct owner session with
no guest and not sandboxed. 3 open per thread, 20 per day. Refused at once, with no
record, if the peer has no outbox (it names the version and how to update) or its
directory lacks that repo.

**The card** (`bot/discord/peer_turns.py`). Posted in this machine's outbox, with the
question in a code block with backticks and mentions neutralised. Run once and Decline
go through the `interactions.py` action table, carry the id, and work once. A real
branch sits ahead of `_in_scope` in `on_interaction`, matching exactly these actions in
a thread under `PEER_CHANNEL_ID`, then checks the interaction's real user id. `_in_scope`
is not widened, because `on_message` uses it. A card cannot live in the lobby: the
lobby is purged at startup.

**Answering** (`InstanceOrigin.PEER_ANSWER`). One at a time, with a turn cap and a
timeout, no history row, no thread binding, no directives. `bot/peer_export.py` makes a
temporary copy of tracked files at HEAD plus that repo's newest 20 reports, filtered and
redacted, with `CLAUDE.md` renamed so it reads as a file. It uses `git ls-tree` and
`git cat-file --batch` rather than `git archive`, which runs `export-subst` and hands
back a tar. The question goes in a file there, so no remote text is in the instance's
prompt. Flags (`peer_confine.answer_flags`, built in the provider branch keyed on origin,
so a failover rebuilds them): `--restricted --tools Read,Glob --strict-mcp-config
--permission-mode dontAsk --settings <path hook> --no-session-persistence --max-turns 30`.

**The check before every Run once** (`peer_confine.self_check`). The real CLI against a
scripted local endpoint, no model usage, under 5s. It tries reads outside the export in
every spelling, including Windows drive-letter and backslash forms, and plants CLAUDE.md
canaries that arrive only if the CLI loads them itself. A leak, an extra tool or a check
that could not run all refuse the tap, and the card says why. There is no unchecked path.

**Delivery** (`InstanceOrigin.PEER_DELIVERY`). A laptop session can post an answer with
no tap, and the asker cannot tell. So an answer never resumes a turn with a shell: it
resumes the thread's session with `--tools ""` in a nonce-fenced prompt saying it is
untrusted, and the next ordinary turn has every tool back. Each reader tick does at
most one per thread, oldest first, skipping while the thread is busy, under the same
channel lock as a typed message. It closes with a one-line note naming the saved file if
the thread has gained a guest, has no session, or after three failed tries. A usage limit
does not use up a try. Delivery is at least once across a restart.

**Departures from what stage 1 decided, and why.**

- **The reader polls** instead of adding a path to `on_message`, so no peer message
  can start anything there, and sleep catch-up needs no second code path.
- **No `store.add_wake`.** It deletes other one-shot rows, `cancel_wakes` deletes them,
  and `on_wake` can only start a full replay with a shell. The state is the queue, so
  wakes, watches and the promise nudge are untouched.
- **Run once only.** No Always grant yet, because grants in `state.json` can be edited by
  local sessions and "only by tap" would not hold.

Invariants:

- **An answer never resumes a turn with tools,** and no origin here dispatches a
  directive, on the success or the failure path.
- **`on_message` is still untouched.**
- **`--bare` never.** It skips hooks. `sandbox.preflight` will not refuse API fallback for
  these runs, so `_prepare_spawn` does: Claude subscription only, no vault keys.
- **No remote text in history, sibling prompts or unredacted files.** Answers are
  redacted and have `[BOT_CMD` and `~~~` neutralised when posted and again on receipt.
- **Windows:** pathlib only, no POSIX-only calls. `sandbox_paths.py` handles drive-letter
  and backslash forms; non-2 hook exits allow, hence `|| exit 2`. `build_settings` calls
  `os.getuid`, so the answering settings are their own small dict.

Setup: `scripts/peer_setup.py` re-run on an existing channel compares and updates only
the two bots' overwrites: allow Create Public Threads and Send Messages in Threads, deny
Manage Messages and Manage Threads. A default, not a boundary.

The measured results are in `study/findings/`. Which work repos are shared is the
owner's call at each tap, and the owner's code of conduct limits non-work use of the
laptop. A tap on a phone does not read the code, and session reports may hold work data.

## Later stages, decided

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

Stage 2 was run against the real CLI with a scripted model and a faked Discord. A real
tap, a real thread (archive, auto-unarchive on send, a thread surviving edits to its
starter message, thread messages staying out of channel history) and Windows have not
been exercised. A forged answer can mislead the asking session but has no tools to act
with. Filtering by filename is a default and misses a secret in an ordinary file whose
format the redactor does not know.

Harnesses: `scripts/test_peer_link.py`, `scripts/test_auto_update_approval.py`,
`scripts/test_peer_questions.py`. By hand, on both machines and on every CLI upgrade:
`scripts/attack_peer_answer.py`.

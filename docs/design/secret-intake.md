# Secret intake

The owner pastes API keys straight into Discord threads and wants to keep doing
that. The bot catches a key before anything else reads the message, stores it,
and sessions use it by name. The value never reaches the prompt, the session
transcript, logs, `state.json`, a thread title or Anthropic's servers.

Harness: `python scripts/test_secret_intake.py` (fake gateway objects and a temp
vault, never the real one). Entry paths that need a real gateway are in the live
pass, run after the merge: a worktree cannot boot its own copy of the bot, and
key-shaped text must not go through a bot running the old code.

## This is not a containment boundary

Sessions share the bot's Linux uid and `kernel.yama.ptrace_scope` is 0. Any
session can read `vault.json`, every `.env`, and the memory of any other process
under that uid. What this protects against is a key sitting in text that gets
sent, stored or shown. It does not stop a session that goes looking. A second
wave handles the real boundary.

## What it does not protect against

- Discord already has what was pasted. Deleting the message limits the damage;
  it does not undo it.
- Voice reaches OpenAI first. A hit in a transcript is deleted and replaced, and
  the owner is told to replace the key. It is never vaulted, because a transcribed
  key is rarely spelled right and OpenAI has already seen it.
- Rejected attachment types, oversize attachments, images and PDFs are not read.
- Output a session prints can reach the transcript and the results. The scan
  after each run flags this by name. It does not prevent it.
- A command that does not go through `_run_slash` can leave the option text
  visible on the invocation, since Discord shows it there. The tree rewrites what
  the bot reads, not what Discord displays.

## Tiers

`bot/secret_detect.py` is the only classifier. `redact.py` reuses its vendor list.
It does not reuse `_KV_PATTERN`, which eats "token: limit".

- **SURE**: a vendor prefix. Stored, message deleted, placeholder in the text.
- **ASK**: entropy, 64 hex, Bearer, a labelled value. The message is held (not
  routed, not deleted) with Secret, Not a secret, and Private key for hex or
  base58 values only. Times out after 10 minutes and tells the owner to delete it.
- **BURN**: seed phrase, PEM, xprv, raw key confirmed by the owner. Deleted, stored
  nowhere, and the owner is told to move the funds. Keeping a wallet key on this
  machine is worse than losing it from the chat.

A guest's message on any tier is deleted and replaced with `[secret removed]`. A
guest cannot store anything and gets no hold.

## Invariants

- **The door runs first.** `on_message` calls `secret_door.screen_message` before
  routing, and everything downstream reads `door.text`. A door failure fails
  closed: nothing routes and the channel is told. Do not add a reader of
  `message.content` above the door.
- **Bodies are read once.** Attachment bodies go through the door's dicts
  (`sanitized`, `prefetched`, `consumed`) and `ingest_attachments` never re-reads
  them. A deleted message's CDN URLs die, so what ingest still needs is fetched
  before the delete. Keep the `None` checks on those arguments: an empty dict is
  the door's object and must not be swapped for a new one.
- **A hold keeps the value in memory only.** The vault's marker holds a channel and
  message id, so a restart can edit the hold line and clear it. The value of a
  message the owner said is not a secret is remembered as a hash, never as text.
- **Names cannot hijack a session.** `vault.safe_name` prefixes anything on the
  denylist with `PASTED_`, and injection checks again, so a hand edit cannot set
  `PATH`, `LD_*` or a provider variable. A pasted `sk-ant-` becomes
  `PASTED_ANTHROPIC_KEY`, never `ANTHROPIC_API_KEY`. A session var that the runner
  strips or a provider reads has to be on the denylist; the harness pins that.
- **Only an owner's turn in a thread with no guest gets keys.** That is
  `is_owner_session and vault_eligible and repo_name` in `core.py`. `vault_eligible`
  defaults to False and has to be set by the route that knows who asked, because
  `is_owner_session` defaults to True (it fails open). `thread_has_guest` defaults
  to True for the same reason. Retry, Steer, replay, chain steps and the usage
  queue all carry the flag rather than recompute it.
- **Vault literals are stripped twice.** Once before the instance is created and
  again in the runner just before the prompt goes out. A value re-pasted in some
  form the door passed still comes out.
- **A corrupt vault is never overwritten.** Writes raise `VaultUnavailable`, reads
  inject nothing, and the door still removes the value and tells the owner.
- **`/secret request` is a directive like the rest.** Scan masked text, skip quoted
  examples, skip `_NO_DIRECTIVE_ORIGINS`, three per response, one pending request
  per name and session. Allow grants the repo and replays the thread, unless a chain
  owns it.
- **A log line never holds a value.** `RedactingFormatter` scrubs the finished line
  so a traceback is covered. It uses `scrub_known`, not `redact_secrets`, which would
  eat ordinary words.

## Where this differs from the brief

- Entropy is skipped in attachment bodies and filenames, since uploads are full of
  hash-shaped strings. A value that reads as random under a secret-looking label
  (`api_key = "..."`, in any case, quoted or not) still goes to ASK there.
- Voice hits are never vaulted (see above).
- PEM and xprv are BURN as well as seed phrases.
- `sk-` is wider than the brief asked, and bounded. A bare `sk-` prefers a vendor
  word the owner wrote nearby over `OPENAI_API_KEY`.
- Bearer values, URL userinfo and URL query credentials are ASK. Webhook URLs are SURE.
- The denylist is wider, and GitHub tokens are stored as `PASTED_GITHUB_TOKEN`: `gh`
  would otherwise pick one up over the owner's login.
- Slash commands refuse ASK and BURN rather than holding, because an interaction
  cannot wait for an answer and still run. The refusal points at `/secret add`.
- `vault_eligible` sits on top of `is_owner_session` (see above).
- The log formatter and the runner's strip are backstops the brief did not list.
- `/ref` context is scrubbed by shape as well as stripped of literals.
- Values under 8 characters are stored but not literal-stripped, or a short password
  would gut unrelated text.
- An ASK value in a filename is scrubbed with no hold. A filename is not worth
  holding a whole message for.
- `env.py` and `env.json` are not env files. They are code and data, and treating
  them as env files would consume them.
- The live pass runs after the merge, not in the build.

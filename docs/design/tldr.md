# TL;DR has two shapes, and picking the wrong one is the whole failure

"Can you summarise this in simple terms" was retyped by hand in nearly every
thread, so it came back a different shape each time and usually full of file
paths and function names, which is unreadable on the phone it is read on. The
[TL;DR] button and `/tldr` make it one prompt (`config.TLDR_PROMPT`,
`steps.on_tldr`).

The load-bearing decision is that there are **two** shapes and the session
picks between them, rather than one template stretched over both:

- **The thing already exists** (built, diagnosed, concluded) ends with the
  **caveat**: what is different now, what it does for you, one concrete
  before/after example, the catch.
- **The thing does not exist yet** (a plan, a proposal, a problem, options)
  ends with the **decision**: the situation, why it bites you, what I would do,
  and the actual question with the recommendation named as the default, so
  "yes" is a complete answer.

Collapsing them forces one into the wrong ending, which is what makes a
"summarise this" answer read as either a changelog nobody asked for or a pitch
with no question in it. No detection heuristic is needed: the prompt states the
branch and the session already knows which it is in.

The Example line is where these answers live or die. "The bot will not ship a
build missing the last release" is abstract and says nothing; "you tap Merge on
two parallel builds and the second one no longer silently reverts the first" is
the sentence the user can act on. The prompt carries both versions so the rule
cannot be read as a style note.

Six things that must not drift:

- **It resumes the session; it never starts fresh.** What it explains is the
  conversation, not the diff. A fresh session would re-derive the *what* from
  the code and get the *why* wrong, which is the half that was worth asking for.
- **It is read-only at the floor that also closes Bash.**
  `permission_mode="explore"` clamps `bash_policy` to `none`
  (`_enforce_readonly_floor`), so a recap resuming a build-mode session cannot
  write through `sed` or `echo >`. An explanation that goes off and does more
  work is not an explanation.
- **It never claims a button row and never lands on its own card.** Five rows
  is the Discord ceiling and a crowded build card already uses four or five, so
  the button appends to the Expand row when one exists and the Branch/Share row
  otherwise (both are gated on `session_id`, which a resume needs anyway), and
  is simply dropped when neither does, since `/tldr` covers that from the
  keyboard. A TL;DR card offers no TL;DR: re-summarising a summary says nothing
  and the recursion has no floor. `InstanceOrigin.TLDR` is also in
  `_WORKFLOW_ORIGINS`, for its own reason: the instance is always clamped to
  explore, so a mode toggle would read "Mode: Plan" on every recap regardless of
  the mode the user was working in, and tapping it would set the thread's mode
  off the back of a read-only turn. It is deliberately **not** in
  `BUILD_ORIGINS`: it writes no code, so it is words, and words run on the light
  model.
- **It leaves the card it was tapped on alone.** Every other button in
  `spawn_from` advances the work, so clearing the source card's buttons is
  right for them. This one does not advance anything, and that card is the one
  holding Merge, Discard, Commit and Done: asking for a summary must not cost
  the user the buttons they were about to press. `strip_source_buttons=False`
  is the only place TL;DR differs from its neighbours, and the harness pins
  both halves so a copy-paste of the next button cannot quietly flip it back.
- **Nothing it writes is dispatched as a directive.** It is in
  `lifecycle._NO_DIRECTIVE_ORIGINS` next to the prompt review, for the weaker
  version of the same reason and a stronger one of its own: a recap of the work
  that just built the `/watch` or `/spawn` handling has those literal examples
  fresh in its context, and this is the one origin whose whole contract is
  "explain, do no work, change nothing", so a directive out of it is incoherent
  however it got there. The prompt forbids it too, but a brief is the soft guard
  and this is the hard one.
- **`/tldr` and the button land on one handler.** The typed form only resolves
  what the button already knew, the thread's newest turn, and that resolution
  lives on the store (`StateStore.latest_instance_for_session`) because the
  spawn-wave join needs the same scan. Two copies of "newest wins" is how one of
  them quietly becomes "first match".

Harness: `python scripts/test_tldr.py`

# Interrupting a session (Kill / Steer)

A kill is only rendered as a quiet tombstone if `RunResult.killed_intentionally`
gets set, and that needs **both** halves:

- The caller must announce intent — `kill_and_wait(..., reason="kill")` (Kill
  button, `/kill`) or `reason="steer"`. The bare `kill()` defaults to
  `intentional=False` and produces a red FAILED card.
- The exit code must corroborate it (`runner.is_kill_shape`). Two shapes count:
  a **negative** returncode (kernel killed a process that ignored the signal)
  and **128+N** (the process handled the signal and exited cleanly). The Claude
  CLI does the second — `terminate()` on it returns **143**, never -15 — and
  accepting only the negative shape once made every Kill and Steer read as a
  crash. Windows can't be told apart at all (`terminate()` always yields 1), so
  it is a blanket True there.

Getting this wrong is not just cosmetic: a killed run has no output and no
turns, which is the account-failover branch's exact signature for "this account
fell over instantly", so an unrecognised kill can be restarted on the backup
subscription. The guard is the `if result.killed_intentionally: return result`
early-return in `_run_impl`, which must stay **above** that branch.

Both the Kill button and typed `/kill` go through one function,
`commands.perform_kill(ctx, inst, source_msg_id)` — they were near-copies, and
the drift between them is what let the button be fixed while the command kept
producing red cards. `source_msg_id` is the message the button sat on: present
means "I will rewrite this card myself", which is passed down as
`kill_and_wait(..., owns_card=True)` and lands on `RunResult.kill_owns_card`.
That flag — **not** the reason string — is what makes `lifecycle.run_instance`
skip its terminal edit of the progress message. `/kill` posts a separate
message and leaves it False, so lifecycle resolves the card to `⏹ stopped`
instead of stranding it on "thinking...". `steered` is reserved for
`reason="steer"`, where a replacement run really is starting.

Harness: `python scripts/test_kill_shape.py` (add `--live` to terminate the real
CLI and check the returncode it actually produces).

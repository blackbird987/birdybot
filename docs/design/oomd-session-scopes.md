# The killer reads a number the guard could not see

Every rule in the memory guard measures how much memory is *free*. The thing
that actually kills this bot measures something else entirely, and on
2026-09-21 it killed the whole unit while every one of those rules scored OK:

```
Killed /user.slice/.../claude-bot.service due to memory pressure for
/user.slice/.../app.slice being 90.79% > 80.00% for > 20s with reclaim activity
  Pressure: Avg10: 94.76, Avg60: 75.53, Avg300: 36.01
  Current Memory Usage: 10.1G
claude-bot.service: Main process exited, code=killed, status=9/KILL
```

Same shape on Sep 14 20:34:46 and Sep 20 10:39:47. Six live sessions each
time. Available memory never dropped below about 9 GB throughout, because
systemd-oomd does not look at available memory: it reads the **cgroup's own
PSI**, and it reads it on `app.slice`, a cgroup the bot had no reader for.
`/proc/pressure/memory` is machine-wide and was reporting single digits. The
guard was not wrong, it was structurally unable to see the number that kills
it.

Four things now exist because of that, and each one fails toward yesterday's
behaviour if the machine will not support it.

- **The guard reads oomd's own criterion.** `own_cgroup_psi_avg10` and
  `slice_cgroup_psi_avg10` read `memory.pressure` in the cgroup our sessions
  run in (see "Which cgroup is 'ours' moved" below) and in the parent slice; `read_oomd_policy` asks `systemctl --user show` what limit oomd
  is actually armed at, and `oomd_policy()` caches it. `read_pressure` escalates to TIGHT at
  `OOMD_TIGHT_FRACTION` (0.6) of that limit and CRITICAL at
  `OOMD_CRITICAL_FRACTION` (0.8), so 94.76% against an 80% limit is CRITICAL
  where it used to be OK. The own-cgroup reading is the discriminator between
  "our sessions are doing this" and "something else on the box is", which is
  the difference between holding a session back and holding one back for
  nothing. Every one of these readers returns `None` when it cannot measure,
  never `0`, and an unreadable oomd config stands the whole escalation down:
  the same rule the existing PSI readers follow, for the same reason.
- **The supervisor is no longer the only victim available.** oomd picks one
  cgroup, and with every session running inside `claude-bot.service` the
  smallest thing it could choose was the supervisor and all twelve
  conversations. Sessions now spawn into a transient scope of their own
  (`bot/claude/cgroups.py`, `systemd-run --user --scope --slice=...`) under
  `app-claudesessions.slice`, and the service carries
  `ManagedOOMPreference=avoid`. **`--scope` and not `--unit`**: a scope execs
  the payload in the caller's own process context, so the asyncio pipes, the
  parent/child relationship and `proc.pid` all survive, where `--unit` would
  hand the command to the service manager and return a pid that is nobody's
  child, breaking the stream-json reader, the kill path and the watchdog at
  once.
- **The slice name is ugly on purpose.** systemd derives tree position from
  the dashes in a slice name, so `app-claudesessions.slice` lands inside
  `app.slice`, which is where oomd is already armed. A prettier
  `claude-sessions.slice` would sit at `claude.slice/claude-sessions.slice`,
  outside it, with no policy reaching it at all. Its oomd limit lives in
  `scripts/app-claudesessions.slice.d/50-oomd.conf` and **not** in the
  fragment, because Fedora arms every user slice from a type-wide drop-in and
  a drop-in beats a fragment: a limit written in the fragment is silently
  overridden by the distribution's 80%. The slice is armed at 70%, below
  `app.slice`, so the fleet is chosen before the box.
- **Crossing a ceiling throttles before anything dies.** A scope gets
  `memory.high` (soft: forced reclaim, kills nothing) and `memory.max` (hard
  backstop) from `SESSION_MEM_HIGH_MB` / `SESSION_MEM_HARD_MB`, and
  `SessionCgroup.current_mb()` reads `memory.current`, which counts the
  reparented Roslyn server that no process-tree walk can see. `cgroup.kill`
  ends a subtree atomically where `kill_tree` is a walk nothing can fork out
  from under. Verified live: a child allocating past a 256 MB soft ceiling sat
  at 273 MB with 10,607 `high` events and `oom_kill 0`, alive and crawling,
  while `smoke_test.py` reported the bot HEALTHY.

Six things that must not drift:

- **A scope does not exist yet when the spawn call returns.** `systemd-run
  --scope` registers the transient unit over D-Bus and only *then* execs, so
  at the instant `create_subprocess_exec` hands back a pid, that pid is still
  charged to the bot's own cgroup. Measured here, three trials of three: in
  `claude-bot.service` at t=0, in its own scope by t=50ms. `adopt_session`
  therefore **polls** (`SESSION_SCOPE_ADOPT_SECS`) instead of reading once,
  and the read it replaced failed *silently*: a single look at t=0 fails the
  identity check exactly the way a session that never got a scope does, so
  every session ran with no soft ceiling, no `memory.current` accounting and
  no atomic kill on a machine where all three worked. The harness had the
  same blind spot for the same reason: it adopted after reading the child's
  first line of output, by which point the scope has existed for a
  comfortable margin. It now adopts at the moment the runner does.

  **50ms is the idle number, and sizing the budget to it is the same bug
  again.** Registration is a D-Bus round trip to the user service manager,
  so how long it takes is a property of how busy that manager is: on a
  loaded, `degraded` one the same three trials read 1.1s, 1.8s and 4.8s, and
  a 5-second budget lost a real run to it. The default is 60s, which is
  affordable only because the wait is *not* awaited in the spawn path:
  `runner._adopt_session_scope` runs it as its own task, so a budget sized
  for a pathological manager can never stall a spawn. Nothing downstream
  needs it to have finished, because every reader of `_session_cgroups`
  already treats a missing entry as "walk the process tree instead", which
  is exactly right for the window before adoption lands. The task is
  cancelled *before* the cleanup's `pop`, or a late adoption writes the
  entry back in behind it and leaks it for the life of the process, and it
  re-checks that the run it adopted is still the one in `_processes`.
  `test_session_cgroups._check_adoption_offpath` fails the suite if
  `adopt_session` is ever awaited anywhere but there: inlining it is four
  lines shorter and passes every other test.
- **Whether scopes work is re-established, not learned once.** It is a
  property of the *user service manager*, and that can wedge under a process
  that lives for weeks: every job `waiting`, none `running`, behind one
  crash-looping unit, seen twice on 2026-09-21. There `systemd-run` blocks
  forever, so a cached "yes" turns every later spawn into a session that
  never starts at all and is only reaped by the 4h silence watchdog. Two
  guards, both cheap: the probe answer carries a TTL
  (`SESSION_SCOPE_PROBE_TTL_SECS`), and a wrapped session that never reaches
  its scope calls `invalidate_scope_probe`, so the next spawn pays one
  bounded probe and then runs unwrapped exactly as a machine with no systemd
  does. The same rule covers `oomd_policy`: a cached *error* is a failure to
  ask and is retried, while `armed=False` with no error is a real answer and
  is kept, because caching one slow `systemctl` call at boot would otherwise
  switch the whole oomd rule off for the life of the process.
- **The supervisor's `MemoryMax` stays large, deliberately.** A machine
  without a usable `systemd-run` falls back to running sessions inside the
  service cgroup exactly as before, and a supervisor cap sized for a
  supervisor would kill them instantly there. The fallback guarantee outranks
  the tighter number.
- **A diagnostic gets its own subprocess seam.** `_probe_scope` uses
  `subprocess.run` on a worker thread and **not**
  `asyncio.create_subprocess_exec`, because the runner spawns sessions through
  that API and every harness counting spawns patches it. It shared the seam
  for exactly one afternoon, during which a one-shot `true` was recorded as a
  phantom extra session attempt in the memory-guard suite. Same rule as the
  PSI readers, one layer down.
- **The probe's unit name is unique per probe.** A fixed name survives as a
  loaded unit after the scope exits, so every later probe fails with "already
  loaded" and the bot concludes that scopes do not work on a machine where
  they do.
- **A session's unit name is unique per spawn, not per instance.** One
  instance spawns the CLI several times in a run (thrash and overflow
  resumes, failover), and the last attempt's scope stays loaded while
  anything in it lives. Named after the id alone, t-8920's resume was refused
  with "already loaded or has a fragment file", which then read as the
  account failing. `new_scope_unit` makes the name once and the runner hands
  that same name to both `wrap_command` and `adopt_session`; recomputing it
  on either side would adopt a scope that was never asked for.
- **`check_weights` reads the cgroup, never `systemctl show`.** During the
  week the protection was off, `systemctl show` reported 20 and the unit files
  reported 20; `cpu.weight` was the one source telling the truth. The startup
  check (`bot/discord/resource_alerts.py`) is a one-shot, not a loop, because
  the weights can only move when the unit restarts or someone runs
  `set-property`, and the first of those brings the check with it.

Install, once, no root:

```bash
cp scripts/app-claudesessions.slice ~/.config/systemd/user/
mkdir -p ~/.config/systemd/user/app-claudesessions.slice.d
cp scripts/app-claudesessions.slice.d/50-oomd.conf \
   ~/.config/systemd/user/app-claudesessions.slice.d/
cp scripts/claude-bot.service ~/.config/systemd/user/   # paths may need rewriting
systemctl --user daemon-reload && systemctl --user restart claude-bot.service
```

`oomctl` is how you confirm it took: the sessions slice should appear with
"Memory Pressure Limit: 70.00%".

One live-machine trap worth writing down, found while verifying this. If
`systemd-run --user` hangs, `systemctl --user show` disagrees with
`systemctl --user cat`, and a direct write into `cpu.weight` reads back
unchanged, the user service manager is wedged, not the code. Check
`systemctl --user is-system-running` (degraded), `list-jobs` (jobs all
`waiting`, none running) and the manager's own CPU. `systemctl --user
daemon-reexec` clears it. On 2026-09-21 an unrelated service being kernel
OOM-killed at a 13.5 GB peak livelocked the manager at 95% CPU for seven
minutes, which among other things stopped the browser opening tabs.

### The rungs have to add up, or the ceiling becomes the killer

Giving oomd a session-sized victim worked: on 2026-09-22 at 11:29 it shot one
session scope instead of the whole unit, the desktop survived and the session
retried on the same id. It still should not have fired, and why it did is
arithmetic rather than a bug in any one place.

`SESSION_MEM_HIGH_MB` was 6144 and `MAX_CONCURRENT` was 5. That is 30 GB of
per-session soft ceiling against the slice budget of the time, `MemoryHigh=11G`
/ `MemoryMax=14G`: **two** sessions at their own ceiling already exceed the
fleet throttle line. Worse, 6 GB is where a dotnet/Roslyn build session
naturally sits. The peaks that day read 5.8G, 5.8G, 5.9G and then six sessions
at *exactly* 6.0G, which is not six coincidences, it is six sessions pinned on
their ceiling.

`memory.high` does not stop a cgroup there. It makes the kernel reclaim
continuously to hold it there, and continuous reclaim is precisely the stall
pressure oomd kills on. t-8711 died with `Pgscan: 141565` and the slice at
93.14% against its 70% limit. **A soft ceiling set at the working set
manufactures the condition it exists to prevent.**

Four numbers now have to stay consistent, and the next person to move one
should check the others:

- `SESSION_MEM_HIGH_MB` (8192) sits **above** the measured working set, where
  it catches a session that is genuinely running away and is invisible to one
  that is merely large. It is not the fleet's limiter and never was.
- `MAX_CONCURRENT` (4) is a backstop, not the limiter.
- The fleet is bounded by the slice's own `MemoryHigh` and by admission.
  `cgroups.slice_headroom_mb` measures against **`memory.high`**, falling back
  to `memory.max` only when there is no soft limit. Measuring the hard cap
  meant the gate engaged at 12800 MB of slice usage on this machine, thousands
  of MB above the throttle line where the kill is already being decided; it
  engaged at 8192 MB against the 11G slice of the time, and at 16384 MB
  against today's 19G (see "The slice ceiling was the killer, again").
- `MEM_ADMISSION_MIN_SLICE_HEADROOM_MB` (3072) reserves room for what a
  session *becomes*, not what it starts as. The old 1536 was sized on the
  stated assumption that the soft ceiling absorbs later growth. It does not
  absorb, it reclaims.

And the hold learned whose shortage it is waiting on. `_admission_blocked`
returns an `AdmissionBlock` carrying `ours`, and the hold loop gives our own
pressure `MEM_ADMISSION_OWN_MAX_WAIT_SECS` (30 min) against the old 5 minutes
for foreign pressure. Waiting on our own slice is waiting for a running
session to finish, which reliably happens; proceeding anyway there is the move
that manufactures the next kill. Waiting on a browser holding 6 GB is waiting
for nothing, so that one still gives up quickly and starts regardless. Both
still proceed in the end, neither blocks forever, and a Kill ends either
immediately. A long hold re-posts every `MEM_ADMISSION_NOTIFY_SECS`, because a
thread that said "waiting for memory" once and then went quiet for half an
hour is indistinguishable from a thread that died. The message that gives up
quotes **elapsed** time, not the deadline: the deadline is recomputed from the
current shortage every pass, so a hold that opened on our own pressure and
ended on a browser's would otherwise report half an hour as five minutes.

**`ours` is never read off the oomd verdict**, which is the tempting shortcut
and would invert the feature. That number is the stall in `app.slice`, which
holds the browser, Plasma and Steam as well as us, so blaming ourselves for it
hands every foreign shortage the 30-minute deadline and blocks work on
something that will never clear. Three readings taken on cgroups we actually
own answer it instead, any one of which is enough: the session slice being
under `MEM_ADMISSION_MIN_SLICE_HEADROOM_MB`, our workload cgroup being over its
`MemoryHigh`, and our workload cgroup carrying at least half the parent slice's
stall (`MemoryPressure.stall_is_ours`, which is also where the "mostly our own
sessions" half of the hold message comes from, so the sentence the user reads
and the deadline they wait out cannot disagree). Nothing readable means
not-ours and the short deadline, which is the right way round: proceeding is
the safe default when there is no evidence worth waiting on.

### Which cgroup is "ours" moved, and three readings did not follow it

Every "are our own sessions doing this" reading was taken on the bot's own
cgroup, and that was correct until v0.101.27 moved the sessions into scopes of
their own. After it, `claude-bot.service` holds a ~250 MB asyncio loop, so
`over_own_high()` and `own_psi_pct` measured the supervisor and answered **no**
however hard the fleet was thrashing. Measured live on 2026-09-22: the
supervisor's cgroup read 234 MB while the sessions slice read 3788 MB of what
was then an 11 GB soft ceiling. Two things were silently dead as a result, and both are
exactly what the incident needed:

- **Cross-session arbitration.** `_fleet_arbitration` gates on
  `is_critical() and over_own_high()`, so the rule that picks one session to
  stop when the fleet together is filling the machine could not fire at all.
- **The blame sentence.** `read_pressure` told the user "it is mostly not us"
  during a shortage our own sessions had caused, and the admission hold quotes
  that sentence verbatim.

The fix is one parameter rather than a second set of readers:
`memory.read_pressure(own_cgroup=...)` names the cgroup the *work* runs in, and
passes it to `cgroup_memory` and `own_cgroup_psi_avg10`. `runner._workload_cgroup`
supplies it from `cgroups.session_slice_path()`, and **None is the fallback, not
an error**: a machine that cannot make scopes, or one where the probe has not
run yet, runs its sessions inside the bot's own cgroup, which is precisely what
the default reads. `parent_slice_path()` is deliberately untouched, so the pair
stays "our fleet's stall against the stall of the slice oomd is armed on".

The direction of the dependency is why this is a parameter at all:
`bot/claude/cgroups.py` imports `memory`, so `memory` cannot ask it where the
sessions slice is without closing a cycle. The runner already imports both and
is the layer that knows whether scopes are in use, so the decision lives there.

### The slice ceiling was the killer, again

On 2026-10-08 systemd-oomd shot two sessions in half an hour:
`claude-session-t-8999.scope` at 12:54 in the middle of a dotnet build, and
`claude-session-q-19143.scope` at 13:22, 2.9 hours into a study. The machine
had 11-12 GB of RAM free both times. The slice's memory pressure read 82% and
72% against its 70% limit, and the bot's own log said "ours 7.7GB/11.0GB",
which looked like room to spare.

It was the "soft ceiling at the working set" mistake from the section above,
one level up. That 7.7 GB is anon only, and `memory.high` counts page cache as
well: 5.0 GB of file cache plus 1.1 GB of dirty build output put
`memory.current` on the 11G line (`memory.peak` 11.23G, `memory.events`
high=993628). zram was 100% full, so the forced reclaim could only thrash file
cache, and that stall is exactly what oomd kills on. A build fleet's working
set includes the files it reads and writes, so the slice ceiling has to sit
well above anon plus cache. It is now `MemoryHigh=19G` / `MemoryMax=21G`,
still nested below `app.slice`'s 27G. A second 8 GB zram device (`zram1` in
`/etc/systemd/zram-generator.conf`, outside this repo) gives the kernel swap
runway before it has to start thrashing cache.

Three things must not drift:

- **A scope's ceilings are passed to `systemd-run` as `-p MemoryHigh=` and
  `-p MemoryMax=`, and never written into the cgroup directly.** systemd
  re-applies a unit's own properties whenever it re-realises the unit, and a
  `daemon-reload` does that to every unit. A direct write into `memory.high`
  lasts until the next one. This was verified live on 2026-10-08: a directly
  written value reverted to `max` on a plain `daemon-reload`, and a `-p` value
  survived it.
  `cgroups.adopt_session` now only reads the ceilings back, for the log.
  `test_session_cgroups.py` runs a real `daemon-reload` against a live scope
  and fails if either ceiling moves.
- **An outside kill is resumed once, after the admission hold.** A SIGKILL
  (-9, or 137 through a shell) on a scoped run that the bot did not send is
  classified by `runner._classify_oom_kill`. "Did not send" means it is not a
  Kill or Steer, not one of the bot's own reaps (those return synthesised
  results with no exit code), and not in `_bot_signalled`. `kill()` adds to
  `_bot_signalled` before it signals, because its escalation after the 5s grace
  is a SIGKILL too, and so does the memory reap, whose `cgroup.kill` is one:
  a reap that stands down falls through to the normal exit path carrying a
  real -9. A run whose `result` event already reported success is never
  classified at all: `_stream_output` keeps a signal that landed after a
  completed turn as the success it was (unless it was the user's own Kill or
  Steer), because resuming it would redo finished work. The scope's journal
  is asked which killer it was (`cgroups.oom_kill_evidence`). For a SIGKILL
  that is corroboration and never a gate: a lookup that finds nothing still
  classifies, and only the wording admits it.
- **A SIGTERM is an OOM kill too, but only on the journal's word.** When the
  kernel's OOM killer picks some *other* process in a scope (the build, not
  the CLI), systemd's default `OOMPolicy=stop` then stops the whole scope with
  a SIGTERM, so the CLI exits 143, not -9. Verified on 2026-10-08 with a test
  scope overrunning its `MemoryMax`: the parent exited -15 and the journal
  read "The kernel OOM killer killed some processes in this unit." A SIGTERM
  has innocent senders, including the bot's own unmarked terminates, so for
  -15/143 the journal is the gate, read once more after a second of ingestion
  lag. Match that kernel line, not "Failed with result 'oom-kill'", which is
  only logged once the scope is gone.
  The resume goes through `_await_memory_headroom`, the same hold a new
  session waits in, so it does not re-enter the stall that just killed it.
  It carries `OOM_KILL_NUDGE_TEMPLATE`: which killer, what the session was
  running, lower the parallelism, and /tmp is RAM. It is resumed once
  (`OOM_KILL_RESUME_RETRIES`, `0` is off). A second kill is a job that does
  not fit, and that is for the user to decide.
- **The error has to say "killed by the system's out-of-memory protection".**
  `parser.is_account_agnostic_error` matches that phrase. A session killed
  before its first turn has no output and no turns, which is the no-turns
  failover heuristic's exact signature. Without the phrase, a two-account setup
  hands the work to the backup subscription to be killed the same way. This is
  the same trap as `lifetime limit`.

Harness: `python scripts/test_oom_resume.py`

Knobs: `SESSION_SCOPES_ENABLED`, `SESSION_SLICE`, `SESSION_MEM_HIGH_MB`,
`SESSION_MEM_HARD_MB`, `SESSION_SCOPE_ADOPT_SECS`,
`SESSION_SCOPE_PROBE_TTL_SECS`, `OOMD_TIGHT_FRACTION`, `OOMD_CRITICAL_FRACTION`,
`RESOURCE_CPU_WEIGHT_EXPECTED`, `RESOURCE_IO_WEIGHT_EXPECTED`,
`SESSION_SLICE_CPU_WEIGHT_EXPECTED`, `SESSION_SLICE_IO_WEIGHT_EXPECTED`,
`MEM_ADMISSION_OWN_MAX_WAIT_SECS`, `MEM_ADMISSION_NOTIFY_SECS`,
`OOM_KILL_RESUME_RETRIES` in `bot/config.py`.

Harnesses: `python scripts/test_session_cgroups.py`,
`python scripts/test_oom_resume.py`, and the oomd, weight-check and
slice-unit cases in `python scripts/test_memory_guard.py`.

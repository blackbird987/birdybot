# The ceiling stopped one level too low

Every memory rule in [oomd-session-scopes.md](oomd-session-scopes.md) bounds *the bot*: a ceiling per session, a
ceiling per fleet, an oomd limit on the sessions slice. On 2026-09-21 16:32
and again on 2026-09-23 02:07 the machine died anyway, and neither time was it
a session. `smartmoney-api.service` leaked to 13.2 GB and then to 10.6 GB:

```
oom-kill:constraint=CONSTRAINT_NONE, ..., global_oom,
  task_memcg=/user.slice/.../app.slice/smartmoney-api.service, task=dotnet
Out of memory: Killed process 3861530 (dotnet) anon-rss:10655400kB
```

`CONSTRAINT_NONE` is the finding. Not a cgroup limit -- there was none to
reach. `app.slice` and `user@1000.service` both read `MemoryMax=infinity`, and
every desktop application under them ships uncapped, so the kernel ran out of
physical memory with swap already at 100% and picked its victim from the whole
task table with the desktop in scope. systemd-oomd was armed on `app.slice` at
80% throughout and did not save it, which is **not** a misconfiguration: oomd
requires twenty seconds of sustained stall, and a runtime allocating gigabytes
in seconds with no swap runway reaches global OOM first. A pressure watchdog is
a reaction; a ceiling is an invariant, and the tree had none.

`scripts/app.slice.d/50-memory.conf` installs one: `MemoryHigh=25G`,
`MemoryMax=27G`. Four things that must not drift:

- **`MemoryMax` is the point, `MemoryHigh` is the courtesy.** High throttles
  and reclaims and kills nothing, which buys oomd the sustained stall it needs
  to choose deliberately. Max is what makes the kill happen *inside*
  `app.slice`, so the victim is an application chosen from applications. Same
  reasoning as `app-claudesessions.slice` one level down: give the killer a
  correctly sized victim and the failure stops being everybody's.
- **The desktop is a sibling, not a descendant.** plasmashell, kwin and the
  rest run in `session.slice`; `app.slice` holds Chrome, Steam, Discord,
  Telegram, the bot and its sessions. That is what makes a cgroup OOM in here
  survivable, and it is worth re-checking before tightening anything: if the
  shell ever moves into `app.slice`, this ceiling starts being able to kill it.
- **The ladder nests, and the harness asserts it.** The sessions slice's
  `MemoryMax` (21G) must stay below the tree's (27G), or the tree's ceiling is
  reached first and oomd picks a victim from every application on the machine
  when the thing filling it was our own fleet. Same arithmetic rule as the
  per-session ceiling against the slice budget.
- **25G is above the working set on purpose.** 18.8 GB anon when this was
  written. A soft ceiling at the working set does not hold a cgroup there, it
  makes the kernel reclaim continuously to hold it there, and continuous
  reclaim is the stall oomd kills on -- the mistake already made once with
  `SESSION_MEM_HIGH_MB` and documented in
  [oomd-session-scopes.md](oomd-session-scopes.md). This catches a leak; it must be
  invisible to a tree that is merely busy.

`cgroups.check_app_slice_ceiling` runs at startup next to the two weight
checks and reads the **live cgroup files**, never a unit file or `systemctl
show`: both reported the intended value throughout the week the weight
protection was actually off. What it guards is not a mistyped number but the
drop-in quietly not being installed, which is invisible until the machine
dies. A ceiling *tighter* than expected passes -- tighter is still bounded,
and a smaller machine may set one on purpose -- while "reads `max`" and "so
high it could never bind" are findings. `app_slice_path()` resolves the tree
by **name**, walking up: asking for "the slice one level up" is correct from
the supervisor and returns `app-claudesessions.slice` from inside a session
scope, which is exactly what the check's first live run reported about itself.

Chrome carries `MemoryHigh=12G` and deliberately no `MemoryMax`
(`~/.config/systemd/user/app-google\x2dchrome@.service.d/50-memory.conf`, a
template drop-in, which does apply to the transient per-launch unit). It is the
largest uncapped consumer here: 11.0 GB with one renderer growing 1.9 GB to
5.3 GB in ten minutes while this was being diagnosed. Soft only, because
reclaim makes Chrome discard background tabs while a hard cap would crash a
renderer on a page being looked at.

### The supervisor's size is recorded, not guarded

This repo calls the supervisor a ~250 MB asyncio loop in several places, and
that number is an argument: it is why `claude-bot.service` keeps the default
CPU weight while the sessions slice carries 20, and why its `MemoryMax` stays
large. It read 1.07 GB after 24 hours of uptime on 2026-09-23. One reading
cannot separate a large working set from a slow leak, and nothing had recorded
the shape over time, so the question was unanswerable rather than answered.

`memory.supervisor_footprint()` logs one line every
`SUPERVISOR_MEM_LOG_MINS` (30), giving `grep 'Supervisor footprint'
data/logs/bot.log` a curve, and warns past `SUPERVISOR_MEM_WARN_MB` (1536).
Three things that must not drift:

- **It is a recorder.** No reap, no restart, no ceiling. Killing the
  supervisor is the outcome the whole memory guard exists to prevent, and a
  guard armed on a number nobody understands yet trades a slow leak for an
  outage. The harness fails the suite if a kill path appears in it.
- **It measures the process, not the cgroup.** The cgroup is the wrong subject
  twice: it carries gigabytes of reclaimable page cache that is not a
  footprint, and on a machine where scopes are unavailable it also carries
  every session -- the exact quantity being excluded.
- **No rate for the first half hour.** A rate computed over the first minutes
  of uptime measures start-up (caches filling, the gateway backfilling, the
  state file loading), so every restart would read as a catastrophic leak.

Knobs: `APP_SLICE_MEM_HIGH_GB_EXPECTED`, `APP_SLICE_MEM_MAX_GB_EXPECTED`,
`SUPERVISOR_MEM_LOG_MINS`, `SUPERVISOR_MEM_WARN_MB` in `bot/config.py`.

Harness: the app.slice and supervisor cases in
`python scripts/test_memory_guard.py`.

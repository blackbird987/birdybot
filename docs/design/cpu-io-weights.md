# The machine has two resources, and only one of them was governed

Everything under the memory guard measures memory. On 2026-09-08 the desktop
became unusable while the bot sat well inside every one of those limits, and
the logs from it are almost useless because each line answers the wrong
question — how much memory was free — during an incident where the answer was
always "plenty".

The readings that mattered, on a 12-core box: load average **110**, one Roslyn
`VBCSCompiler` at **605% CPU** with six sessions live, CPU PSI 9-18%, IO PSI
11-24%. Memory PSI was 3%, available memory never dropped below 11 GB, and the
100%-full swap belonged to Steam, Telegram and Plasma — the bot's cgroup held
0.88 GB of it. `read_pressure` returned TIGHT and was **right**; nothing was
wrong with memory. The unit simply had no `CPUWeight` or `IOWeight` at all, so
this cgroup fought Plasma and the browser at the default weight of 100, and a
compile farm always wins a fair fight against a desktop.

Three layers, because there are three distinct contests and one knob cannot
settle them:

- **Bot versus you**: `CPUWeight=20`, `IOWeight=20`, on
  **`app-claudesessions.slice`**, not on `claude-bot.service`. Weight,
  **not `CPUQuota`**, and that distinction is the
  whole design: weights bind only under contention, so an unattended machine
  still runs builds across all 12 cores at full speed, while a machine you are
  sitting at keeps roughly five sixths of the CPU. A quota would buy the
  desktop's responsiveness with permanently slower builds — the wrong trade for
  a box that is usually unattended. It landed on the *running* unit with
  `systemctl --user set-property --runtime`, so six live sessions did not have
  to be restarted for it, and **that is the sentence that cost a week**: a
  `--runtime` drop-in lives in `/run` and systemd discards it when the unit
  stops. An oomd kill stops the unit. The protection therefore uninstalled
  itself on the exact event it exists for, and on 2026-09-21 the live cgroup
  read `cpu.weight=100` while both unit files said 20 and `systemctl show`
  agreed with them. Use `--runtime` to try a value; the unit file is what
  makes it survive, and `cgroups.check_weights` is what proves it did.

  The durable copy is `scripts/claude-bot.service`, which is the one that
  matters: `~/.config/systemd/user/claude-bot.service` is an install-time copy
  with the paths rewritten, and `migrate-off-windows-disk.sh` regenerates it.
  A resource setting added only to the deployed copy survives until the next
  migration and then silently disappears. Both carry it.

  **The 20 moved off the service on 2026-09-22, and where it sits is now the
  whole point.** Since v0.101.27 every CLI, shell, dotnet and Roslyn process
  runs in a transient scope under `app-claudesessions.slice`, which carries
  its own `CPUWeight=20` / `IOWeight=20`. The compile farm this paragraph is
  about is therefore already held to a sixth, on the cgroup that actually
  contains it. What is left inside `claude-bot.service` is the supervisor: a
  ~250 MB asyncio loop that has to answer a Discord interaction within three
  seconds and keep a gateway heartbeat alive. Starving *that* buys the desktop
  nothing, because the CPU was never the supervisor's to give, and costs the
  bot its connection, since a missed heartbeat disconnects it. So the service
  runs at the default 100 and the slice keeps the 20, and both halves are
  checked at startup against the live cgroup files
  (`cgroups.check_weights` for the service, `check_session_slice_weights` for
  the slice, expectations in `RESOURCE_*_WEIGHT_EXPECTED` and
  `SESSION_SLICE_*_WEIGHT_EXPECTED`). Checking only the service was checking
  the half that no longer matters.

  One live-machine fact that makes this easier to accept than to fight: on
  this box `uresourced --user` (Fedora's user resource daemon, PID confirmed
  through its D-Bus name on 2026-09-22) calls `SetUnitProperties` on
  `claude-bot.service` **many times a second**, rewriting
  `/run/user/1000/systemd/user.control/claude-bot.service.d/50-CPUWeight.conf`
  with `CPUWeight=100` continuously and burning about 14% of a core doing it.
  That, and not a stale drop-in, is why the 20 never held there. It does not
  touch `app-claudesessions.slice`, which reads 20 live.
- **Bot versus its own sessions** — `config.SESSION_CPU_NICE` (10), applied by
  `runner._lower_priority` immediately after the spawn. `CPUWeight` settles the
  cgroup's share against the desktop and says nothing about how that share is
  divided *inside* it, which is why the bot stopped answering Discord: a
  ~250 MB asyncio loop that must reply within 3 seconds was competing at equal
  priority with six CLIs and a 605% compiler, and a missed gateway heartbeat
  disconnects. Niceness survives exec and is inherited by children, so one call
  covers the CLI, its shells, dotnet and Roslyn without the runner hunting them
  down.

  **It is deliberately not a `preexec_fn`.** That is the obvious way to do it
  and the wrong one here: CPython runs `preexec_fn` in the child between fork
  and exec, where only async-signal-safe work is legal, and this bot forks from
  a process with well over a hundred threading callsites. A child that lands on
  a lock another thread held at fork time deadlocks before exec and hangs the
  spawn forever while holding the runner's slot. Doing it from the parent costs
  a few microseconds in which the CLI runs at normal priority — harmless, since
  node spends hundreds of milliseconds starting before it forks anything — and
  its worst case is a session that runs fast rather than one that never runs.
  The target is computed from the supervisor's *own* nice, so a unit that later
  grows a `Nice=` cannot silently close the gap.
- **Bot versus the machine** — `MemoryPressure.cpu_psi_pct` / `io_psi_pct`,
  recorded and logged, **deliberately never escalated on.** With the two
  weights in place, CPU saturation costs throughput and no longer costs anyone
  their desktop, so a gate here would hold sessions back to fix something that
  no longer hurts. What was missing was never enforcement; it was that the next
  incident of this shape be diagnosable from `bot.log` alone.

Two things that must not drift:

- **Each PSI signal keeps its own reader.** `psi_some_avg10` (memory),
  `cpu_psi_avg10` and `io_psi_avg10` all wrap `_psi_some_avg10` rather than
  sharing one parameterised entry point. The harness stubs the memory reader;
  a single shared seam let that stub silently decide the CPU answer too, which
  is how a test can describe a machine as memory-starved and CPU-idle in the
  same breath.
- **CPU and IO print only next to a reading the verdict was made from.** They
  are context for a memory verdict, not a verdict of their own. A pressure read
  that could measure nothing must still summarise as having nothing to go on —
  quoting `/proc/pressure/cpu` at it makes a total failure of the memory
  readings look like a healthy machine. `summary()` gates both on `bits` being
  non-empty; `test_memory_guard.py` asserts it.

Harness: `python scripts/test_memory_guard.py`

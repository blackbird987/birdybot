"""Session transcripts: hydrate, mirror and copy-back across accounts and worktrees."""

from __future__ import annotations

import asyncio
import json as _json_mod
import logging
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from bot import config
from bot.textutil import parse_iso_utc
from bot.claude.types import Instance

log = logging.getLogger(__name__)

# Tunable knobs for the cross-account session-recovery path (step 3 of the
# dementia fix).  60s cache window dedupes rebuild_project_index calls when a
# burst of resumes hits the same missing-session state.  30s timeout caps
# rebuild latency so a slow disk scan can't block an autopilot chain.
REBUILD_CACHE_TTL_SECS = 60
REBUILD_TIMEOUT_SECS = 30
# Cross-account JSONL hydration: same TTL semantics as rebuild — bursts of
# correlated resume attempts shouldn't re-scan every account on disk.
HYDRATE_CACHE_TTL_SECS = 60

# Session ids are UUIDs. Asserted before one is interpolated into a glob, since
# a stray wildcard there would match a DIFFERENT conversation's file.
_UUID_RE = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)


class SessionFilesMixin:
    """Session transcripts across accounts and worktrees. Mixed into ClaudeRunner."""

    # --- Session file management (worktree ↔ main repo) ---

    @staticmethod
    def _encode_project_path(path: str) -> str:
        """Encode path the same way the CLI does for project dirs.

        Delegates to the shared implementation in ``bot.claude.session_fork``
        so both the runner and the JSONL forker agree on project-dir names.
        """
        from bot.claude.session_fork import encode_project_path
        return encode_project_path(path)

    async def _maybe_rebuild_session_index(
        self,
        account_dir: str | None,
        instance: Instance,
    ) -> bool:
        """Rebuild sessions-index.json for the project owning this session.

        Best-effort recovery for "No conversation found" — see step 3 of the
        cross-account dementia plan.  Returns True iff a rebuild actually ran
        (and so a --resume retry is worth attempting).

        - Caches per (account_dir, project_dir) for REBUILD_CACHE_TTL_SECS to
          avoid rescanning during bursts of correlated failures.
        - Bounded by REBUILD_TIMEOUT_SECS so a slow disk can't stall the chain.
        - Wrapped in try/except: rebuild errors fall through to layer 3 rather
          than propagating.  Rebuild is a recovery tool, not a critical path.
        """
        if not account_dir or not instance.repo_path:
            return False
        from bot.claude.session_index import rebuild_project_index
        encoded = self._encode_project_path(
            instance.worktree_path or instance.repo_path
        )
        project_dir = Path(account_dir) / "projects" / encoded
        cache_key = (account_dir, str(project_dir))
        now = asyncio.get_event_loop().time()
        last = self._rebuild_cache.get(cache_key, 0.0)
        if now - last < REBUILD_CACHE_TTL_SECS:
            log.info(
                "Rebuild skipped for %s (cached <%ds ago)",
                instance.id, REBUILD_CACHE_TTL_SECS,
            )
            return False
        if not project_dir.is_dir():
            log.info(
                "Rebuild skipped for %s — no project dir at %s",
                instance.id, project_dir,
            )
            return False
        try:
            await asyncio.wait_for(
                rebuild_project_index(project_dir),
                timeout=REBUILD_TIMEOUT_SECS,
            )
            self._rebuild_cache[cache_key] = now
            return True
        except asyncio.TimeoutError:
            log.warning(
                "Rebuild timed out (>%ds) for %s on %s",
                REBUILD_TIMEOUT_SECS, instance.id, account_dir[-20:],
            )
            return False
        except Exception:
            log.exception(
                "Rebuild failed for %s on %s",
                instance.id, account_dir[-20:],
            )
            return False

    @staticmethod
    def _atomic_copy_jsonl(src: Path, dst: Path) -> None:
        """Copy src JSONL to dst with temp-then-rename semantics.

        Atomic on Linux always; on Windows ``os.replace`` raises
        PermissionError if dst is held open by another process (e.g. a peer
        account's CLI mid-``--resume``).  Retry briefly before giving up.
        Total worst-case wait ~350 ms.
        """
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(dst.suffix + ".tmp")
        shutil.copy2(str(src), str(tmp))
        for attempt in range(4):
            try:
                os.replace(str(tmp), str(dst))
                return
            except PermissionError:
                if attempt == 3:
                    # Best-effort tmp cleanup so we don't leave junk behind.
                    try:
                        os.unlink(str(tmp))
                    except OSError:
                        pass
                    raise
                time.sleep(0.05 * (2 ** attempt))  # 50ms, 100ms, 200ms

    def _jsonl_last_timestamp(self, path: Path) -> datetime | None:
        """Return the timestamp of the last complete turn in an append-only JSONL.

        The CLI writes each turn as a newline-terminated JSON object with a
        ``"timestamp": "<iso8601>"`` field.  We tail the file and walk
        backward through complete lines; first parseable line with a valid
        timestamp wins.

        - Tail 256KB; expand to 1MB once if no complete line parses.
        - Only accept lines ending in ``\\n`` (guards against a final partial
          line written mid-append).
        - Returns None on parse failure / missing field — caller falls back
          to ``path.stat().st_mtime``.

        Cached by (path, mtime_ns, size) — any append busts the cache because
        size changes.
        """
        try:
            st = path.stat()
        except OSError:
            return None
        cache_key = (str(path), st.st_mtime_ns, st.st_size)
        if cache_key in self._freshness_cache:
            return self._freshness_cache[cache_key]

        def _parse_tail(tail_bytes: int) -> datetime | None:
            try:
                with open(path, "rb") as fh:
                    if st.st_size > tail_bytes:
                        fh.seek(st.st_size - tail_bytes)
                    chunk = fh.read()
            except OSError:
                return None
            # Walk backward through newline-terminated lines only.
            # A final byte that isn't \n means the last "line" is mid-write —
            # discard it implicitly by starting our scan at the last \n.
            if not chunk:
                return None
            # rsplit on \n; the trailing element after the last \n is the
            # partial (we drop it). Reverse what's left to walk newest first.
            parts = chunk.split(b"\n")
            # If the file ends with \n, parts[-1] == b'' (empty after final \n).
            # If it doesn't, parts[-1] is the partial — drop it either way.
            complete_lines = parts[:-1]
            for raw in reversed(complete_lines):
                if not raw.strip():
                    continue
                try:
                    obj = _json_mod.loads(raw)
                except (_json_mod.JSONDecodeError, UnicodeDecodeError):
                    continue
                ts = obj.get("timestamp") if isinstance(obj, dict) else None
                if not isinstance(ts, str):
                    continue
                parsed = parse_iso_utc(ts)
                if parsed is not None:
                    return parsed
            return None

        result = _parse_tail(256 * 1024)
        if result is None and st.st_size > 256 * 1024:
            result = _parse_tail(1024 * 1024)
        self._freshness_cache[cache_key] = result
        return result

    def _resolve_session_jsonl_paths(
        self,
        instance: Instance,
        account_dir: str,
    ) -> tuple[Path, list[Path]] | None:
        """Resolve the (source_jsonl, peer_target_jsonls) for a successful run.

        Source: ``account_dir``'s project dir using the encoding that the
        run actually wrote to (worktree-encoded for worktree builds, else
        repo-encoded).  This is the encoding ``--resume`` opens on the
        next call, so it's the only encoding that matters.

        Targets: same encoding under every OTHER configured account.

        Returns None if the session can't be located on disk for ``account_dir``.
        """
        if not instance.session_id:
            return None
        cwd_for_run = instance.worktree_path or instance.repo_path
        if not cwd_for_run:
            return None
        encoded = self._encode_project_path(cwd_for_run)
        sid = instance.session_id
        src = Path(account_dir) / "projects" / encoded / f"{sid}.jsonl"
        if not src.exists():
            return None
        targets: list[Path] = []
        for peer in config.CLAUDE_ACCOUNTS:
            if peer == account_dir:
                continue
            targets.append(Path(peer) / "projects" / encoded / f"{sid}.jsonl")
        return src, targets

    async def _mirror_session_to_peer_accounts(
        self,
        instance: Instance,
        account_dir: str | None,
    ) -> None:
        """Mirror the JSONL the CLI just wrote to every other configured account.

        Keeps cross-account session storage in lockstep so the next failover's
        freshness check is unambiguous, and a ``--resume`` after reboot /
        cache-miss always finds a current copy on whichever account it lands
        on.

        - Atomic temp-then-rename via ``_atomic_copy_jsonl`` (handles Windows
          file-lock retry).
        - Skip-when-identical: stat both sides, skip if (size, mtime_ns) match.
        - On each successful peer write, rebuild that peer's sessions-index
          so the new JSONL is discoverable by ``--resume``.
        - Best-effort: any OSError logs at WARNING and continues to the next
          peer.  Never raises.
        """
        if not account_dir:
            return
        paths = self._resolve_session_jsonl_paths(instance, account_dir)
        if paths is None:
            return
        src, targets = paths
        if not targets:
            return

        def _mirror_sync() -> list[tuple[Path, str]]:
            """Returns list of (peer_target, status) for follow-up index rebuilds."""
            wrote: list[tuple[Path, str]] = []
            try:
                src_st = src.stat()
            except OSError:
                log.warning(
                    "Mirror skipped for %s: source %s vanished",
                    instance.id, src,
                )
                return wrote
            for dst in targets:
                try:
                    if dst.exists():
                        dst_st = dst.stat()
                        if (
                            dst_st.st_size == src_st.st_size
                            and dst_st.st_mtime_ns == src_st.st_mtime_ns
                        ):
                            continue  # already in sync
                    self._atomic_copy_jsonl(src, dst)
                    # Peer account dir is the grandparent of projects/<enc>/
                    peer_acct = str(dst.parent.parent.parent)
                    wrote.append((dst, peer_acct))
                except OSError as exc:
                    log.warning(
                        "Mirror failed for %s: %s -> %s (%s) acct=%s sid=%s",
                        instance.id, src, dst, exc,
                        dst.parent.parent.parent, instance.session_id[:12]
                        if instance.session_id else "?",
                    )
            return wrote

        try:
            wrote = await asyncio.to_thread(_mirror_sync)
        except Exception:
            log.exception(
                "Mirror raised for %s — should be impossible", instance.id,
            )
            return

        # Rebuild peer indexes for any writes that landed.  Without this,
        # a peer with fresh JSONL but stale index can still miss --resume
        # (the original dementia symptom).
        for _dst, peer_acct in wrote:
            try:
                await self._maybe_rebuild_session_index(peer_acct, instance)
            except Exception:
                log.exception(
                    "Peer-index rebuild raised for %s on %s",
                    instance.id, peer_acct[-20:],
                )

    async def _hydrate_session_for_account(
        self,
        account_dir: str,
        cwd: str,
        session_id: str,
        instance: Instance,
    ) -> bool:
        """Ensure the session JSONL exists at account_dir's project dir for cwd.

        Picks the freshest JSONL across all configured accounts (by last-turn
        timestamp parsed from the append-only JSONL) and copies it to the
        target if the target isn't already freshest.  Then refreshes the
        target's sessions-index so the CLI can locate the session via
        ``--resume``.

        Pre-t-4633 bug: this method short-circuited on ``target_file.exists()``
        without comparing freshness.  When a session lived on two accounts
        (e.g. after a prior failover) and the target's copy was stale, the
        CLI would resume the stale snapshot and the user saw days-old
        context.  Freshness comparison fixes that.

        Tie-break on equal last-timestamps: prefer the target (no copy);
        if target absent, prefer largest size.
        All-None fallback: if every candidate's freshness is unknown
        (corruption / empty), do NOT overwrite the target — fall through to
        the existing cross-account candidate scan unchanged.

        Returns True iff the JSONL is present at the target after this call.
        Best-effort: failures are logged and return False; the caller falls
        through to the existing recovery layers.
        """
        encoded_cwd = self._encode_project_path(cwd)
        target_dir = Path(account_dir) / "projects" / encoded_cwd
        target_file = target_dir / f"{session_id}.jsonl"
        encoded_repo = (
            self._encode_project_path(instance.repo_path)
            if instance.repo_path else None
        )

        # --- Freshness-aware path ---
        # Gather candidates across every configured account × every encoding
        # this conversation could be filed under, INCLUDING the target itself.
        # Pick the freshest.
        #
        # The encodings are not just {cwd, repo}.  The CLI names a project dir
        # after the path it was launched from, so the SAME repo produces a
        # different dir per machine — `C--Users-...` written from Windows,
        # `-run-media-...` written from Linux, off one shared drive.  Asking
        # the path map for every known spelling turns those into candidates
        # instead of dead ends; without it, resuming on the other machine is a
        # guaranteed "No conversation found" no matter how healthy everything
        # else is.
        from bot.claude.session_fork import encoded_spellings

        encodings: list[str] = []

        def _add_encoding(enc: str | None) -> None:
            if enc and enc not in encodings:
                encodings.append(enc)

        # The two directly-derived encodings go in first and by name, so that
        # "the encoding this call is actually targeting is in the list" is
        # structurally true rather than an accident of the alias round-trip
        # producing a byte-identical string.
        _add_encoding(encoded_cwd)
        _add_encoding(encoded_repo)
        for base in (cwd, instance.repo_path):
            if not base:
                continue
            for enc in encoded_spellings(base):
                _add_encoding(enc)

        # Seeded with the target rather than trusting it to fall out of the
        # loops below.  "Target is freshest -> keep it" is only reachable if the
        # target is in this list; if it silently dropped out (an account_dir
        # spelled differently from its CLAUDE_ACCOUNTS entry, a cwd with a
        # trailing separator), the winner could never be the target and a stale
        # cross-account copy would overwrite a perfectly good local history.
        candidates: list[Path] = [target_file]
        seen: set[str] = {str(target_file)}
        accounts = list(config.CLAUDE_ACCOUNTS or [account_dir])
        for acct in accounts:
            for enc in encodings:
                p = Path(acct) / "projects" / enc / f"{session_id}.jsonl"
                key = str(p)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(p)

        # Catch-all: a session id is a UUID, so a filename match is an exact
        # identification rather than a guess.  This finds history filed under a
        # spelling nothing here can derive — a live-USB mount point, a repo
        # that moved, a second checkout of the same project — which is the
        # difference between "recovers on its own" and "needs a human to
        # notice".  One directory listing per account, only ever on the path
        # where the derived candidates already came up empty.
        #
        # The UUID shape is CHECKED, not assumed: the id is interpolated into a
        # glob pattern, so anything carrying `*` or `?` would silently widen
        # this from "the conversation we want" to "some other conversation",
        # and hydrating the wrong history is worse than finding none.
        if _UUID_RE.match(session_id or ""):
            def _scan() -> list[Path]:
                # The "did the derived candidates miss?" probe lives in here
                # too, not on the event loop. These are stat calls against a
                # FUSE-mounted NTFS volume in the hot path of every resume, and
                # the rest of this method is careful to keep that off the loop.
                if any(p.exists() for p in candidates):
                    return []
                found: list[Path] = []
                for acct in accounts:
                    root = Path(acct) / "projects"
                    try:
                        found.extend(root.glob(f"*/{session_id}.jsonl"))
                    except OSError:
                        continue
                return found

            try:
                for extra in await asyncio.to_thread(_scan):
                    key = str(extra)
                    if key not in seen:
                        seen.add(key)
                        candidates.append(extra)
                        log.info(
                            "Hydrate: found session %s under unmapped project "
                            "dir %s for %s",
                            session_id[:12], extra.parent.name, instance.id,
                        )
            except Exception:
                log.exception(
                    "Hydrate catch-all scan raised for %s", session_id[:12],
                )

        def _gather_freshness() -> list[tuple[Path, datetime | None, int]]:
            """Stat + parse last-timestamp for each existing candidate.

            Returns list of (path, last_ts, size).  Missing files are
            excluded.
            """
            out: list[tuple[Path, datetime | None, int]] = []
            for p in candidates:
                try:
                    st = p.stat()
                except OSError:
                    continue
                ts = self._jsonl_last_timestamp(p)
                out.append((p, ts, st.st_size))
            return out

        try:
            existing = await asyncio.to_thread(_gather_freshness)
        except Exception:
            log.exception(
                "Freshness gather raised for %s — falling back to legacy hydrate",
                session_id[:12],
            )
            existing = []

        # Decide winner.  Filter to candidates with a known timestamp; if
        # none have one, fall through to the legacy cross-account scan
        # below (which uses existence-only logic — same as pre-t-4633 for
        # this rare corruption case).
        with_ts = [(p, ts, sz) for (p, ts, sz) in existing if ts is not None]
        if with_ts:
            # Sort: newest timestamp first; on tie, prefer the target itself
            # (avoids a no-op copy), else prefer largest size.
            def _key(item: tuple[Path, datetime, int]) -> tuple:
                p, ts, sz = item
                is_target = (p == target_file)
                # newer ts first → negate via sort with reverse on ts;
                # on tie, target wins → False sorts before True so invert;
                # then largest size first.
                return (ts, is_target, sz)

            with_ts.sort(key=_key, reverse=True)
            winner_path, winner_ts, _winner_sz = with_ts[0]

            if winner_path == target_file:
                # Target is already freshest — done.  Still rebuild the
                # index because the JSONL may have been written by the CLI
                # without an index update (original dementia symptom).
                await self._maybe_rebuild_session_index(account_dir, instance)
                return True

            # Target is stale (or missing) — overwrite from the freshest
            # source via atomic copy.
            def _do_overwrite() -> bool:
                try:
                    self._atomic_copy_jsonl(winner_path, target_file)
                    return True
                except OSError:
                    log.exception(
                        "Hydrate freshness-overwrite failed: %s -> %s",
                        winner_path, target_file,
                    )
                    return False

            try:
                ok = await asyncio.to_thread(_do_overwrite)
            except Exception:
                log.exception(
                    "Hydrate overwrite raised for %s", session_id[:12],
                )
                ok = False

            if ok:
                log.info(
                    "Hydrated session %s into %s for %s "
                    "[freshest from %s, ts=%s]",
                    session_id[:12], account_dir[-20:], instance.id,
                    str(winner_path.parent.parent.parent)[-30:],
                    winner_ts.isoformat() if winner_ts else "?",
                )
                await self._maybe_rebuild_session_index(account_dir, instance)
                return target_file.exists()
            # Overwrite failed — fall through to legacy path below as a
            # last-resort recovery.

        # --- Legacy existence-only path (kept as fallback) ---
        # Reached when either every candidate's freshness is unknown OR the
        # freshness-overwrite copy failed.  Preserves pre-t-4633 behavior
        # for those edge cases.
        if target_file.exists():
            # File is in place but its index entry might be stale.  Refresh
            # via the shared cache — cheap on cache hit, fixes the dementia
            # symptom on cache miss.
            await self._maybe_rebuild_session_index(account_dir, instance)
            return True

        # Same-account fast path: a worktree build's encoded_cwd differs
        # from the main-repo encoding, but earlier q-* steps wrote turns to
        # this account's main-repo project dir.  That copy is the freshest
        # by definition — every prior --resume on this account appended to
        # it.  Prefer it over any cross-account copy, which could be stale
        # (e.g. left behind by a past failover run).
        if encoded_repo and encoded_repo != encoded_cwd:
            same_acct_src = (
                Path(account_dir) / "projects" / encoded_repo
                / f"{session_id}.jsonl"
            )

            def _copy_same_account() -> bool:
                if not same_acct_src.exists():
                    return False
                target_dir.mkdir(parents=True, exist_ok=True)
                if not target_file.exists():
                    shutil.copy2(str(same_acct_src), str(target_file))
                return True

            try:
                hit = await asyncio.to_thread(_copy_same_account)
            except OSError:
                log.exception(
                    "Hydrate same-account copy failed for %s -> %s",
                    session_id[:12], target_dir,
                )
                hit = False

            if hit:
                log.info(
                    "Hydrated session %s into %s for %s (same-account from %s)",
                    session_id[:12], account_dir[-20:], instance.id,
                    encoded_repo[-30:],
                )
                await self._maybe_rebuild_session_index(account_dir, instance)
                return True

        cache_key = (account_dir, str(target_dir), session_id)
        now = asyncio.get_event_loop().time()
        last = self._hydrate_cache.get(cache_key, 0.0)
        if now - last < HYDRATE_CACHE_TTL_SECS:
            return target_file.exists()
        self._hydrate_cache[cache_key] = now

        # Build candidate source paths: every OTHER account × every encoding
        # this conversation could be filed under (the same set the freshness
        # path used — reusing it rather than re-deriving {cwd, repo} keeps this
        # fallback from being quietly blinder than the path it backs up).
        # The current account's own paths are already covered by the
        # target_file check and same-account fast path above.
        candidates: list[Path] = []
        for acct in accounts:
            if acct == account_dir:
                continue
            for enc in encodings:
                candidates.append(
                    Path(acct) / "projects" / enc / f"{session_id}.jsonl"
                )

        def _copy_first_match() -> Path | None:
            for src in candidates:
                if src.exists():
                    target_dir.mkdir(parents=True, exist_ok=True)
                    # Race-safety: another coroutine may have just hydrated
                    # the same target.  Re-check before clobbering.
                    if not target_file.exists():
                        shutil.copy2(str(src), str(target_file))
                    return src
            return None

        try:
            src = await asyncio.to_thread(_copy_first_match)
        except OSError:
            log.exception(
                "Hydrate copy failed for %s -> %s",
                session_id[:12], target_dir,
            )
            return False

        if src is None:
            log.info(
                "Hydrate: session %s not found on any account for %s",
                session_id[:12], instance.id,
            )
            return False

        log.info(
            "Hydrated session %s into %s for %s [cross-account] (from %s)",
            session_id[:12], account_dir[-20:], instance.id,
            str(src.parent.parent.parent)[-30:],
        )

        # Rebuild the target's index so the CLI sees the new JSONL.  Shared
        # cache with layer 2 means concurrent recovery paths don't double-scan.
        await self._maybe_rebuild_session_index(account_dir, instance)

        return target_file.exists()

    # NOTE: no memory-dir syncing is needed between worktree and main-repo
    # project dirs.  Claude Code resolves a git-worktree cwd to the MAIN
    # repository root when computing the per-project memory path (verified
    # against CLI v2.1.206: it follows .git -> gitdir -> commondir and keys
    # memory off the common root), so worktree builds already read and write
    # the main repo's projects/<encoded>/memory.  Only session JSONLs are
    # keyed to the worktree path and need the copy below.

    def _copy_session_from_worktree(
        self,
        instance: Instance,
        account_dir: str | None = None,
    ) -> None:
        """Mirror session JSONLs from worktree's project dir back to main-repo dirs.

        Reads from ``account_dir``'s worktree project dir (the account that
        hosted the run).  Writes to BOTH the same account's main-repo project
        dir AND the owning ``session_account``'s main-repo dir if different,
        so a follow-up resume on either account finds the JSONL locally
        without needing hydrate to run first.
        """
        if not instance.repo_path or not instance.worktree_path:
            return
        wt_encoded = self._encode_project_path(instance.worktree_path)
        repo_encoded = self._encode_project_path(instance.repo_path)
        if wt_encoded == repo_encoded:
            return

        # Source: account that ran (preferred) → session_account → CLI default
        src_account = (
            account_dir
            or instance.session_account
            or str(config.CLAUDE_PROJECTS_DIR.parent)
        )
        wt_proj_dir = Path(src_account) / "projects" / wt_encoded
        if not wt_proj_dir.is_dir():
            # Legacy fallback: pre-hydrate runs may have written to the CLI default
            wt_proj_dir = config.CLAUDE_PROJECTS_DIR / wt_encoded
            if not wt_proj_dir.is_dir():
                return

        # Destinations: same account's main-repo dir + owning account's if different
        dest_accounts: list[str] = []
        if account_dir:
            dest_accounts.append(account_dir)
        if (
            instance.session_account
            and instance.session_account in config.CLAUDE_ACCOUNTS
            and instance.session_account not in dest_accounts
        ):
            dest_accounts.append(instance.session_account)
        if not dest_accounts:
            dest_accounts.append(str(config.CLAUDE_PROJECTS_DIR.parent))

        for dest in dest_accounts:
            repo_proj_dir = Path(dest) / "projects" / repo_encoded
            repo_proj_dir.mkdir(parents=True, exist_ok=True)
            for f in wt_proj_dir.glob("*.jsonl"):
                dst = repo_proj_dir / f.name
                try:
                    shutil.copy2(str(f), str(dst))
                except OSError:
                    log.exception("Back-copy %s -> %s failed", f, dst)
                else:
                    log.debug(
                        "Back-copied %s to %s", f.name, dest[-20:],
                    )

    def _cleanup_worktree_session_dir(self, instance: Instance) -> None:
        """Remove the worktree's CLI project directory across every configured account.

        Pre-multi-account this only cleaned ``CLAUDE_PROJECTS_DIR`` (the
        default account).  With cross-account hydration, a worktree build
        may have written its JSONL into any account's ``projects/`` dir,
        so we sweep all of them to avoid orphaned per-instance directories.
        """
        if not instance.worktree_path:
            return
        wt_encoded = self._encode_project_path(instance.worktree_path)
        # One source of truth for "every root the CLI may have written to" —
        # already deduplicated, already existence-filtered, and it honours a
        # deliberately narrowed root. That last part matters here more than
        # anywhere: this is an rmtree, so a harness that sandboxed the
        # projects root must not have the sweep widened back onto real
        # session data.
        for root in config.claude_projects_dirs():
            wt_proj_dir = root / wt_encoded
            if wt_proj_dir.is_dir():
                shutil.rmtree(str(wt_proj_dir), ignore_errors=True)

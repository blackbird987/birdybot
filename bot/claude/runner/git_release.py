"""Release tags: version parsing, containment checks and tag cutting."""

from __future__ import annotations

import logging
import re
import subprocess

from bot import config
from bot.procutil import run_capture
from bot.claude.types import RELEASE_ORPHANED_MARKER

log = logging.getLogger(__name__)


# Release-tag deferral: the LLM commits ``vX.Y.Z: Summary`` but does not run
# ``git tag``.  After the merge to master succeeds the bot walks the
# branch-unique commit window and creates the tag from the first matching
# subject. Capping the walk at 50 commits is purely defensive — typical
# release branches are 1–3 commits, and unbounded walks could surprise us if
# a stacked Build chain ever balloons.
_RELEASE_COMMIT_RE = re.compile(r'^(v\d+\.\d+\.\d+(?:\.\d+)?):\s')
_TAG_WALKBACK_CAP = 50

_VERSION_TAG_RE = re.compile(r'^v(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?$')


def _parse_version_tag(name: str) -> tuple[int, int, int, int] | None:
    """Parse ``vX.Y.Z[.W]`` into a comparable tuple, else None."""
    m = _VERSION_TAG_RE.match(name)
    if not m:
        return None
    return tuple(int(g) if g else 0 for g in m.groups())  # type: ignore[return-value]


# --- Release containment -------------------------------------------------
#
# A version *number* going up proves nothing about the *content* shipping.
# Parallel builds each branch from their own snapshot of master, and a build
# that ships from a base predating the last release reverts it while carrying
# a higher version. That is why `_stale_version_warning`, which compares
# numbers, cannot see this class of failure at all.  The invariant these
# helpers check is containment: the previous release's commits must be
# reachable from whatever is about to ship.

def version_tags(
    repo: str, *, merged: str | None = None, no_merged: str | None = None,
) -> list[tuple[str, tuple[int, int, int, int]]]:
    """``(name, parsed_version)`` for every ``vX.Y.Z[.W]`` tag, newest first.

    ``merged`` / ``no_merged`` map to git's own ref filters and combine with
    AND, so ``merged=branch, no_merged="master"`` answers "which releases
    live only on this branch".  Git resolves annotated tags to their commit
    for both filters, so no dereferencing is needed here.

    Only ``None`` means "do not filter".  An empty string is passed straight
    to git, which rejects it, so a caller handing over a ref it failed to
    resolve gets nothing back rather than the *unfiltered* list, which means
    the exact opposite of what it asked for.

    Returns an empty list on any git failure, because every caller treats
    "cannot tell" as "nothing to report" rather than blocking on a broken read.
    """
    cmd = ["git", "tag", "-l", "v*"]
    if merged is not None:
        cmd += ["--merged", merged]
    if no_merged is not None:
        cmd += ["--no-merged", no_merged]
    try:
        r = run_capture(cmd, cwd=repo, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        log.warning("Listing version tags failed in %s", repo, exc_info=True)
        return []
    if r.returncode != 0:
        log.debug(
            "git tag -l failed in %s (rc=%d): %s",
            repo, r.returncode, (r.stderr or "").strip(),
        )
        return []
    found: list[tuple[str, tuple[int, int, int, int]]] = []
    for line in (r.stdout or "").splitlines():
        name = line.strip()
        ver = _parse_version_tag(name)
        if ver is not None:
            found.append((name, ver))
    found.sort(key=lambda t: t[1], reverse=True)
    return found


def _is_ancestor(repo: str, rev: str, of: str) -> bool | None:
    """Is ``rev`` reachable from ``of``?  None when git could not answer.

    ``merge-base --is-ancestor`` uses the exit code as its answer: 0 yes,
    1 no, anything else an error.  Collapsing that third case into False is
    what would turn an unreadable repo into a blocked deploy, so it stays
    distinct all the way up to the callers.
    """
    try:
        r = run_capture(
            ["git", "merge-base", "--is-ancestor", rev, of], cwd=repo, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        log.warning("merge-base --is-ancestor failed in %s", repo, exc_info=True)
        return None
    if r.returncode == 0:
        return True
    if r.returncode == 1:
        return False
    log.debug(
        "merge-base --is-ancestor %s %s in %s: rc=%d %s",
        rev, of, repo, r.returncode, (r.stderr or "").strip(),
    )
    return None


def missing_predecessor_release(
    repo: str, ref: str = "HEAD", *, below: str | None = None,
) -> str | None:
    """Name the release ``ref`` should contain but does not.

    Answers one question: **is the release before this one still in here?**
    ``below`` is the release just cut, supplied by the merge path so a
    release is never compared against itself. With no ``below``, the ceiling
    is derived as the newest release ``ref`` *does* contain: for a deploy
    there is no "just cut", and the tree's own newest release is the right
    place to start counting back from.

    Deliberately the immediate predecessor, not every unreachable tag. A
    repo accumulates stranded tags over years (discarded branches, old
    experiments, hand-tagged spikes) and re-reporting those on every merge
    and every deploy is how a real signal gets tuned out, and AIAgent carries
    18 of them. ``orphaned_releases`` is the on-demand audit for the rest.

    The one case with no ceiling to derive is a ``ref`` that contains no
    release at all, and there the newest release anywhere is exactly what
    is missing, so it is reported.
    """
    tags = version_tags(repo)
    if not tags:
        return None
    if below is not None:
        ceiling = _parse_version_tag(below)
    else:
        reachable = {name for name, _ in version_tags(repo, merged=ref)}
        ceiling = next(
            (ver for name, ver in tags if name in reachable), None,
        )
        if ceiling is None:
            newest, _ = tags[0]
            return newest if _is_ancestor(repo, newest, ref) is False else None
    for name, ver in tags:
        if ceiling is not None and ver >= ceiling:
            continue
        return name if _is_ancestor(repo, name, ref) is False else None
    return None


def orphaned_releases(repo: str, ref: str = "HEAD") -> list[str]:
    """Every version tag whose commits are not contained in ``ref``.

    The backlog view behind ``/branches``.  One git call, regardless of how
    many tags the repo carries.
    """
    return [name for name, _ in version_tags(repo, no_merged=ref)]


# Git's transfer statistics, which the server echoes back prefixed "remote:".
# Matched as a category rather than by symptom: filtering on "contains a
# percentage" / "ends with done." looks equivalent but misses the one such
# line that has neither, `remote: Total 3 (delta 0), reused 0 (delta 0)`,
# which is then reported to the user as the cause of the failure.
_GIT_PROGRESS = (
    "enumerating objects", "counting objects", "compressing objects",
    "writing objects", "receiving objects", "resolving deltas",
    "unpacking objects", "total ",
)


def _is_git_progress(line: str) -> bool:
    """Is this a transfer-statistics line rather than a diagnosis?"""
    body = line[len("remote:"):] if line.startswith("remote:") else line
    return body.strip().lower().startswith(_GIT_PROGRESS)


def git_fail_reason(detail: str, limit: int = 160) -> str:
    """The one line of a failed git command worth showing a human.

    Merge notes carried the exit code alone. That stayed hidden while git was
    hanging, because the note read ``Push to origin timed out (30s)`` instead
    — but disabling the interactive prompt (``procutil.harden_git_env``)
    turns those hangs into fast non-zero exits, which would then have reached
    the phone as a bare ``Could not push to origin (exit 128)`` with the
    sentence naming the cause still stranded in ``bot.log``. Fixing the hang
    without this would have swapped one uninformative message for another.

    Which line that is takes some care, and "the first one" is wrong. A
    credential failure does lead with its diagnosis::

        fatal: could not read Username for 'https://github.com': terminal
        prompts disabled

    but the far more common failure — a branch that is behind its remote —
    does not::

        To github.com:owner/repo.git
         ! [rejected]        master -> master (fetch first)
        error: failed to push some refs to 'github.com:owner/repo.git'
        hint: Updates were rejected because the remote contains work ...

    There the first line is the remote's address, which explains nothing
    while looking like it should. So prefer the first line git marks as a
    diagnosis and fall back to the first non-blank line only if none is
    marked. ``hint:`` is deliberately not a marker: it is the paragraph of
    advice that follows the reason, not the reason.

    Transfer statistics are dropped before either step, not merely skipped
    while looking for a marker. The server sends those prefixed ``remote:``
    too and they arrive *before* its refusal, so scanning on the marker alone
    would report a byte count as the cause — but excluding them only from the
    scan leaves the fallback free to hand the same byte count back, which is
    exactly what happens when the real message carries no marker at all::

        remote: Counting objects: 100% (5/5), done.
        The remote end hung up unexpectedly

    Redaction is not optional politeness. Nothing scrubs secrets at the
    Discord send boundary — ``redact_secrets`` is applied per call site — and
    an HTTPS remote can carry an embedded token, which git then quotes back
    inside ``fatal: Authentication failed for '<url>'``. The deploy push in
    ``discord/interactions.py`` posted that stderr raw, so the token went to
    Discord verbatim; both it and the merge notes go through here now. Public
    for that reason: a leading underscore on a name imported from another
    package would misdescribe the interface.

    ``redact_secrets`` is imported lazily to keep the claude layer free of a
    module-level platform import.
    """
    from bot.platform.formatting import redact_secrets

    lines = [" ".join(ln.split()) for ln in detail.splitlines() if ln.strip()]
    if not lines:
        return ""
    # Chatter first, so it can be neither the pick nor the fallback. Keeping
    # everything if that empties the list: better a byte count than nothing.
    useful = [ln for ln in lines if not _is_git_progress(ln)] or lines
    # Then an ordered scan, first match wins, so the earliest *marked* line is
    # used: on a permission failure that is the server's "remote: Permission
    # ... denied", which is more use than the `fatal:` 403 that follows it.
    line = next(
        (ln for ln in useful
         if ln.startswith(("fatal:", "error:", "remote:", "!"))),
        useful[0],
    )
    line = redact_secrets(line)
    return (line[: limit - 1] + "…") if len(line) > limit else line


class ReleaseMixin:
    """Release tags: cutting them and checking what a tree contains. Mixed into ClaudeRunner."""

    def _release_commit_window(
        self, repo: str, branch_tip: str,
    ) -> list[tuple[str, str]]:
        """Return (sha, subject) pairs for branch-unique commits, newest-first.

        Walks ``HEAD^1..<branch_tip>`` along first-parent so we only see
        commits the merged-in branch contributed (i.e. the LLM's release
        commit and any cleanup commits it made after). Capped at
        ``_TAG_WALKBACK_CAP`` defensively. Returns an empty list on any git
        failure — callers treat that as "no candidates" and skip tagging.
        """
        try:
            r = run_capture(
                [
                    "git", "log", "--first-parent",
                    f"-{_TAG_WALKBACK_CAP}",
                    "--format=%H%x09%s",
                    f"HEAD^1..{branch_tip}",
                ],
                cwd=repo,
            )
        except Exception:
            log.exception("git log for release-commit window failed in %s", repo)
            return []
        if r.returncode != 0:
            log.debug(
                "git log HEAD^1..%s failed in %s (rc=%d): %s",
                branch_tip, repo, r.returncode, (r.stderr or "").strip(),
            )
            return []
        out: list[tuple[str, str]] = []
        for line in (r.stdout or "").splitlines():
            sha, _, subject = line.partition("\t")
            sha = sha.strip()
            if sha:
                out.append((sha, subject))
        return out

    def _tag_release_at(
        self, repo: str, branch_tip: str,
    ) -> tuple[str | None, list[str]]:
        """Walk the branch-unique window for ``vX.Y.Z: ...`` and tag it.

        Returns ``(tag_name, candidate_shas)``:
          - ``tag_name`` is the tag name when we created it, when it already
            pointed at the release commit (idempotent), or ``None`` when
            there was nothing to do or a same-name tag pointed elsewhere
            (no clobber).
          - ``candidate_shas`` is every SHA from the window — used by the
            push block to look for any pre-existing tags that live on those
            commits (legacy LLM-created tags before this deferral landed).
        """
        window = self._release_commit_window(repo, branch_tip)
        candidate_shas = [sha for sha, _ in window]
        for sha, subject in window:
            m = _RELEASE_COMMIT_RE.match(subject)
            if not m:
                continue
            tag_name = m.group(1)
            # ^{commit} dereferences annotated tags down to the commit they
            # point at. Without it, `rev-parse refs/tags/<annotated>` returns
            # the tag-object SHA, which never equals the commit SHA we
            # compare against — falsely tripping the no-clobber branch on
            # an annotated tag that's actually already correct.
            existing = run_capture(
                ["git", "rev-parse", "--verify",
                 f"refs/tags/{tag_name}^{{commit}}"],
                cwd=repo,
            )
            if existing.returncode == 0:
                where = (existing.stdout or "").strip()
                if where == sha:
                    log.info(
                        "Tag %s already at release commit %s in %s — idempotent",
                        tag_name, sha[:7], repo,
                    )
                    return tag_name, candidate_shas
                log.warning(
                    "Tag %s already exists at %s (release commit is %s) in %s — "
                    "refusing to clobber",
                    tag_name, where[:7], sha[:7], repo,
                )
                return None, candidate_shas
            create = run_capture(["git", "tag", tag_name, sha], cwd=repo)
            if create.returncode != 0:
                log.error(
                    "git tag %s %s failed in %s: %s",
                    tag_name, sha[:7], repo,
                    (create.stderr or create.stdout or "").strip(),
                )
                return None, candidate_shas
            log.info(
                "Created release tag %s at %s in %s", tag_name, sha[:7], repo,
            )
            return tag_name, candidate_shas
        return None, candidate_shas

    @staticmethod
    def _rev_count(repo: str, range_spec: str) -> int:
        """Count commits in ``range_spec`` (e.g. ``master..origin/master``).

        Returns 0 on any failure — including a missing remote-tracking ref
        (branch never pushed), where "no sync information" should behave
        like "in sync" rather than block the merge.
        """
        try:
            r = run_capture(["git", "rev-list", "--count", range_spec], cwd=repo, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            log.warning("rev-list --count %s failed in %s", range_spec, repo,
                        exc_info=True)
            return 0
        if r.returncode != 0:
            return 0
        try:
            return int((r.stdout or "").strip())
        except ValueError:
            return 0

    def _stale_version_warning(
        self, repo: str, tag_name: str | None, branch_tip: str,
    ) -> str:
        """Warn when the just-merged release's version trails existing tags.

        Build sessions mint versions from worktree-local tags, so a stale
        clone can produce e.g. ``v0.92.61`` after origin already shipped
        ``v0.93.1``.  ``_tag_release_at``'s no-clobber logic then either
        skips tagging silently (same-name tag elsewhere) or creates a tag
        below the actual latest.  Either way the user must be told —
        untagged releases have already gone missing this way (v0.92.53).

        ``branch_tip`` must match the value passed to ``_tag_release_at``
        so both walk the same commit window.

        Returns "" when there is nothing to warn about (no release commit
        in the window, or the version is the new maximum).
        """
        if tag_name is None:
            # Distinguish "no release commit" (fine) from "release commit
            # present but not tagged" by re-walking the window for a
            # release subject.
            try:
                window = self._release_commit_window(repo, branch_tip)
            except Exception:
                return ""
            for _sha, subject in window:
                m = _RELEASE_COMMIT_RE.match(subject)
                if not m:
                    continue
                ver_name = m.group(1)
                # Untagged for one of two reasons — name taken (no-clobber)
                # or tag creation failed. Tell the user which.
                tag_exists = run_capture(
                    ["git", "rev-parse", "--verify", f"refs/tags/{ver_name}"], cwd=repo,
                ).returncode == 0
                if tag_exists:
                    return (
                        f"\nRelease commit {ver_name} was not tagged "
                        f"(a tag with that name already exists elsewhere) — "
                        f"version numbering may be stale"
                    )
                return (
                    f"\nRelease commit {ver_name} was not tagged "
                    f"(tag creation failed — see bot.log)"
                )
            return ""
        ver = _parse_version_tag(tag_name)
        if ver is None:
            return ""
        try:
            r = run_capture(["git", "tag", "-l", "v*"], cwd=repo, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if r.returncode != 0:
            return ""
        newest_name, newest_ver = None, ver
        for name in (r.stdout or "").splitlines():
            other = _parse_version_tag(name.strip())
            if other is not None and other > newest_ver:
                newest_name, newest_ver = name.strip(), other
        if newest_name is not None:
            return (
                f"\nRelease {tag_name} is below existing {newest_name} — "
                f"version numbering may be stale"
            )
        return ""

    @staticmethod
    def _release_containment_warning(repo: str, tag_name: str | None) -> str:
        """Warn when the merged target no longer contains the last release.

        The sibling check above compares version *numbers*, and a build that
        ships from a stale base has a perfectly good number. It is the
        previous release's *commits* that are missing. That is how releases
        get silently reverted when parallel builds cross over, and the
        version bump is what hides it.

        ``tag_name`` is the release this merge just cut, used only as the
        ceiling so a release is never compared against itself. Carries
        ``RELEASE_ORPHANED_MARKER`` so the chain can refuse to ship on it.
        """
        if not config.RELEASE_ANCESTRY_CHECK:
            return ""
        try:
            missing = missing_predecessor_release(repo, "HEAD", below=tag_name)
        except Exception:
            log.exception("Release containment check raised in %s", repo)
            return ""
        if not missing:
            return ""
        log.error(
            "Merge target in %s does not contain release %s%s",
            repo, missing,
            f" (just cut {tag_name})" if tag_name else "",
        )
        return (
            f"\n⛔ {RELEASE_ORPHANED_MARKER}: `{missing}` is not an ancestor of "
            f"what just landed, so shipping this would revert that release. "
            f"Merge `{missing}` in before deploying."
        )

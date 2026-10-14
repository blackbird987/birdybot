"""A sandbox one replay runs in: a clone that cannot see the answer.

The clone is made by ``git init`` plus a fetch of exactly the base commit
from the live repo, so the only objects in it are the base commit and its
ancestors. A ``git clone`` would bring ``master`` and every ``claude-bot/*``
branch along, and the shipped solution would sit one ``git log --all`` away
from the model being measured against it. ``check_contamination`` proves the
reference tip and the merge are absent before anything runs.

The conversation is forked with the bot's own ``session_fork.fork_session``
into the account's project directory for the clone's path, cut at the row the
original build prompt hung off. Only the transcript is written there. The
account's ``.credentials.json`` is never read, copied or moved: the refresh
token rotates on use, and a second copy of it signs the real account out.

Known gap: the CLI's per-project auto-memory (``projects/<dir>/memory/``)
is not carried into the fork, so a replay starts with none. The live
directory holds memories written after the build being replayed, so copying
it would leak the future; the original build ran with whatever it held then,
which is not recoverable.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from common import assert_outside_live_data, git
from bot.claude.session_fork import encode_project_path, fork_session

GIT_ENV = {
    "GIT_AUTHOR_NAME": "replay-ab", "GIT_AUTHOR_EMAIL": "replay-ab@localhost",
    "GIT_COMMITTER_NAME": "replay-ab", "GIT_COMMITTER_EMAIL": "replay-ab@localhost",
}


@dataclass
class Sandbox:
    root: Path            # per-replicate directory
    repo: Path            # the clone the session works in
    start_sha: str        # base commit, plus the arm's setup commit if any
    session_id: str | None
    project_dir: Path | None


def _git_env(cwd: Path, *args: str) -> str:
    old = {k: os.environ.get(k) for k in GIT_ENV}
    os.environ.update(GIT_ENV)
    try:
        return git(cwd, *args)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def check_clone_path(dest: Path) -> None:
    """Refuse a clone path the CLI and ``encode_project_path`` would spell differently.

    The fork is written to ``projects/<encode_project_path(repo)>`` and the CLI
    looks for the session under its own spelling of the cwd. Those agree on
    ``/`` and ``.`` but not on ``_``: the first spike, under
    ``~/.cache/replay_ab``, failed with "No conversation found" because the CLI
    looked under ``replay-ab``. Allowing only characters both spell as
    themselves (or both as ``-``) keeps the two in step without guessing the
    CLI's full rule.
    """
    if re.search(r"[^A-Za-z0-9/.\-]", str(dest)):
        raise RuntimeError(
            f"clone path {dest} has characters the CLI may encode differently "
            "(use letters, digits, '-', '.', '/'); set REPLAY_AB_HOME to such a path"
        )


def make_clone(source_repo: Path, base_sha: str, dest: Path) -> Path:
    assert_outside_live_data(dest)
    check_clone_path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    git(dest, "init", "-q", "-b", "master")
    git(dest, "fetch", "-q", "--no-tags", str(source_repo), base_sha, timeout=600)
    git(dest, "checkout", "-q", "-B", "master", base_sha)
    # The session works on its own branch, as a build does in its worktree;
    # the prompt's location block names it.
    git(dest, "checkout", "-q", "-b", "replay")
    # FETCH_HEAD names the source path; nothing else in the clone does.
    (dest / ".git" / "FETCH_HEAD").unlink(missing_ok=True)
    return dest


def check_contamination(repo: Path, forbidden: list[str]) -> list[str]:
    """Problems that would let the session see the solution. Empty means clean."""
    problems = []
    for sha in forbidden:
        if not sha:
            continue
        if _exists(repo, sha):
            problems.append(f"object {sha[:10]} is present")
    refs = git(repo, "for-each-ref", "--format=%(refname)").splitlines()
    for ref in refs:
        if "claude-bot/" in ref or ref.startswith("refs/remotes/"):
            problems.append(f"ref {ref} is present")
    if git(repo, "remote", check=False):
        problems.append("clone has a remote")
    return problems


def _exists(repo: Path, sha: str) -> bool:
    r = subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=str(repo),
                       capture_output=True)
    return r.returncode == 0


def apply_arm_files(repo: Path, files: dict[str, str]) -> str:
    """Overlay arm files (e.g. a CLAUDE.md variant) as one setup commit.

    Committed rather than left dirty so the diff scored afterwards starts
    from the arm's own starting tree, and the variant is not counted as work
    the session did.
    """
    if not files:
        return git(repo, "rev-parse", "HEAD")
    for rel, src in files.items():
        dst = repo / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    _git_env(repo, "add", "-A")
    _git_env(repo, "commit", "-q", "-m", "replay: arm setup")
    return git(repo, "rev-parse", "HEAD")


def fork_into(account: Path, transcript: Path, cut_uuid: str, repo: Path) -> tuple[str, Path]:
    project_dir = account / "projects" / encode_project_path(str(repo))
    assert_outside_live_data(project_dir)
    out = fork_session(transcript, cut_uuid, project_dir)
    if not out:
        raise RuntimeError(f"fork_session could not cut {transcript.name} at {cut_uuid}")
    return out[0], project_dir


def build(case, root: Path, arm_files: dict[str, str] | None = None) -> Sandbox:
    repo = make_clone(Path(case.repo_path), case.base_sha, root / "repo")
    problems = check_contamination(repo, [case.ref_tip, case.merge_sha])
    if problems:
        raise RuntimeError("sandbox can see the solution: " + "; ".join(problems))
    start = apply_arm_files(repo, arm_files or {})
    sid = project_dir = None
    if case.session_id and case.cut_uuid:
        sid, project_dir = fork_into(Path(case.account), Path(case.transcript), case.cut_uuid, repo)
    return Sandbox(root=root, repo=repo, start_sha=start, session_id=sid, project_dir=project_dir)


def destroy(sb_root: Path, project_dir: Path | None) -> None:
    """Remove a replicate's clone and the transcripts the CLI wrote for it."""
    if project_dir and project_dir.is_dir() and "replay-ab" in project_dir.name:
        shutil.rmtree(project_dir, ignore_errors=True)
    shutil.rmtree(sb_root / "repo", ignore_errors=True)

"""Regression tests for release containment.

A version *number* going up proves nothing about the content shipping. When
parallel builds cross over, a build cut from a base that predates the last
release ships older code under a higher version, and every number-based check
(``_stale_version_warning``) waves it through because the number really did
go up. The release is reverted in silence.

Confirmed in the wild: ``DegenAI/AIAgent`` carried 18 version tags whose
commits were not reachable from master, and a v1.3.2.216 ship in September
2026 was caught by hand only because a session happened to look.

What is locked in here:
  - containment is read from git, and a read that cannot answer never
    turns into a block;
  - the check never compares a release against itself;
  - the merge warning carries a marker that means "do not ship", and that
    marker is NOT a merge failure (the branch landed fine);
  - the chain refuses to deploy or close on it;
  - the deploy gate runs BEFORE the push, not after;
  - discarding a branch reports the release tags it strands.

Run: python scripts/test_release_ancestry.py
Exit 0 = all pass, exit 1 = failures.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401  -- relaunches under .venv if deps are missing

import inspect
import os
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from bot import config  # noqa: E402
from bot.claude.runner import (  # noqa: E402
    ClaudeRunner, _NOWND, _is_ancestor, missing_predecessor_release,
    orphaned_releases, version_tags,
)
from bot.claude.types import (  # noqa: E402
    Instance, InstanceStatus, InstanceType, RELEASE_ORPHANED_MARKER,
    merge_msg_is_failure, merge_msg_release_orphaned,
)


_failures: list[str] = []


def _check(actual, expected, label: str) -> None:
    if actual != expected:
        _failures.append(label)
        print(f"  FAIL: {label} - got {actual!r}, expected {expected!r}")
    else:
        print(f"  ok:   {label}")


def _git(repo: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, **_NOWND,
    )
    if check and r.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {repo}: {r.stderr.strip()}"
        )
    return r


def _init_repo(repo: str) -> None:
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "tester")
    with open(os.path.join(repo, "README.md"), "w") as f:
        f.write("init\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")


def _commit(repo: str, message: str, *, file: str = "f.txt") -> str:
    path = os.path.join(repo, file)
    with open(path, "a") as f:
        f.write(message + "\n")
    _git(repo, "add", file)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _crossed_repo(repo: str) -> None:
    """Build the exact shape that reverts a release.

    master:  init -- v1.0.0 -- (v1.0.1 release) = the line that shipped
                  \\
    ship:          -- (v1.0.2 release)          = cut from the older base

    ``ship`` carries the newer version and none of v1.0.1's content.
    """
    _init_repo(repo)
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "tag", "v1.0.0", base)
    first = _commit(repo, "v1.0.1: the release that gets reverted")
    _git(repo, "tag", "v1.0.1", first)
    _git(repo, "checkout", "-q", "-b", "ship", base)
    second = _commit(repo, "v1.0.2: cut from a stale base")
    _git(repo, "tag", "v1.0.2", second)
    _git(repo, "checkout", "-q", "master")


def test_a_version_tags_parse_and_order() -> None:
    print("\n[a] version_tags parses, orders and filters")
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp)
        head = _git(tmp, "rev-parse", "HEAD").stdout.strip()
        for name in ("v1.0.0", "v1.0.10", "v1.0.2", "v2.0.0.1", "nightly", "v1.2-rc1"):
            _git(tmp, "tag", name, head)
        names = [n for n, _ in version_tags(tmp)]
        _check(names, ["v2.0.0.1", "v1.0.10", "v1.0.2", "v1.0.0"],
               "sorted by parsed version, not lexically")
        _check("nightly" in names or "v1.2-rc1" in names, False,
               "non-version tags excluded")


def test_b_filters_scope_to_a_branch() -> None:
    print("\n[b] merged/no_merged filters combine")
    with tempfile.TemporaryDirectory() as tmp:
        _crossed_repo(tmp)
        only_on_ship = [n for n, _ in version_tags(tmp, merged="ship",
                                                   no_merged="master")]
        _check(only_on_ship, ["v1.0.2"], "tag reachable only from ship")
        _check([n for n, _ in version_tags(tmp, merged="master",
                                           no_merged="master")], [],
               "a branch strands nothing against itself")


def test_c_healthy_line_is_silent() -> None:
    print("\n[c] a contained history reports nothing")
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp)
        a = _commit(tmp, "v1.0.0: first")
        _git(tmp, "tag", "v1.0.0", a)
        b = _commit(tmp, "v1.0.1: second")
        _git(tmp, "tag", "v1.0.1", b)
        _check(missing_predecessor_release(tmp, "HEAD"), None,
               "linear history: nothing missing")
        _check(missing_predecessor_release(tmp, "HEAD", below="v1.0.2"), None,
               "cutting the next release is still silent")
        _check(orphaned_releases(tmp), [], "no orphans to audit")


def test_d_the_revert_is_caught() -> None:
    print("\n[d] the crossed-release revert is named")
    with tempfile.TemporaryDirectory() as tmp:
        _crossed_repo(tmp)
        _check(missing_predecessor_release(tmp, "ship", below="v1.0.2"),
               "v1.0.1", "names the release ship would revert")
        _check(missing_predecessor_release(tmp, "ship"), "v1.0.1",
               "deploy gate (no ceiling) blocks the same tree")
        _check(missing_predecessor_release(tmp, "master"), None,
               "the line that did ship it is clean")
        _check(orphaned_releases(tmp, "master"), ["v1.0.2"],
               "audit lists what master cannot reach")


def test_e_never_compares_a_release_to_itself() -> None:
    print("\n[e] the release just cut is the ceiling, not the subject")
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp)
        base = _git(tmp, "rev-parse", "HEAD").stdout.strip()
        # v2.0.0 tagged on a side branch master will never contain: without
        # the ceiling, cutting v2.0.0 on master would report *itself*.
        _git(tmp, "checkout", "-q", "-b", "side", base)
        side = _commit(tmp, "side work")
        _git(tmp, "tag", "v2.0.0", side)
        _git(tmp, "checkout", "-q", "master")
        _check(missing_predecessor_release(tmp, "HEAD", below="v2.0.0"), None,
               "ceiling excludes the release being cut")
        _check(missing_predecessor_release(tmp, "HEAD"), "v2.0.0",
               "without a ceiling the same tag is reported")


def test_e2_old_orphans_do_not_block_forever() -> None:
    """A tag stranded years ago must not gate today's deploy.

    This is the reason the check walks back exactly one release instead of
    demanding every tag be contained: AIAgent carries 18 unreachable ones
    and a gate that fired on those would be switched off within a day.
    """
    print("\n[e2] an old orphan is audit material, not a block")
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp)
        base = _git(tmp, "rev-parse", "HEAD").stdout.strip()
        _git(tmp, "checkout", "-q", "-b", "ancient", base)
        old = _commit(tmp, "v0.1.0: long abandoned")
        _git(tmp, "tag", "v0.1.0", old)
        _git(tmp, "checkout", "-q", "master")
        for v in ("v1.0.0", "v1.0.1", "v1.0.2"):
            _git(tmp, "tag", v, _commit(tmp, f"{v}: shipped"))
        _check(missing_predecessor_release(tmp, "HEAD"), None,
               "recent line is clean despite the ancient orphan")
        _check(orphaned_releases(tmp), ["v0.1.0"],
               "the orphan is still listed by the audit")


def test_f_nothing_to_compare_against() -> None:
    print("\n[f] a repo with no releases is not a problem")
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp)
        _check(missing_predecessor_release(tmp, "HEAD"), None,
               "no version tags: nothing missing")
        _check(version_tags(tmp), [], "no version tags listed")


def test_g_unreadable_never_blocks() -> None:
    """A git read that cannot answer must not read as 'not contained'.

    ``merge-base --is-ancestor`` answers through its exit code, where 0 is
    yes and 1 is no. Collapsing every other code into False is what would
    turn an unreadable repo into a permanently blocked deploy.
    """
    print("\n[g] a failed read is None, not False")
    with tempfile.TemporaryDirectory() as tmp:
        _init_repo(tmp)
        _check(_is_ancestor(tmp, "no-such-ref", "HEAD"), None,
               "bogus rev -> None (cannot tell)")
        _check(_is_ancestor(tmp, "HEAD", "HEAD"), True, "HEAD contains itself")
        _check(missing_predecessor_release("/nonexistent/repo", "HEAD"), None,
               "missing repo reports nothing rather than blocking")


def test_g2_an_empty_ref_filters_nothing_open() -> None:
    """A ref the caller could not resolve must return nothing, not everything.

    ``git tag -l`` with no ``--merged`` lists every release in the repo. If a
    blank ref silently dropped the filter, the discard scan would report the
    whole tag history as stranded by one branch deletion, and the audit would
    call every release orphaned. Both fail closed instead.
    """
    print("\n[g2] a ref that resolves to nothing reports nothing")
    with tempfile.TemporaryDirectory() as tmp:
        _crossed_repo(tmp)
        _check(len(version_tags(tmp)) >= 3, True, "the repo does have releases")
        _check(version_tags(tmp, merged=""), [],
               "an empty merged filter returns nothing, not everything")
        _check(version_tags(tmp, no_merged=""), [],
               "an empty no_merged filter returns nothing, not everything")
        _check(orphaned_releases(tmp, ""), [],
               "the audit calls nothing orphaned on an unresolvable ref")


def test_h_marker_means_do_not_ship_not_merge_failed() -> None:
    print("\n[h] the marker gates shipping, not cleanup")
    runner = ClaudeRunner()
    with tempfile.TemporaryDirectory() as tmp:
        _crossed_repo(tmp)
        _git(tmp, "checkout", "-q", "ship")
        warning = runner._release_containment_warning(tmp, "v1.0.2")
        _check(RELEASE_ORPHANED_MARKER in warning, True,
               "warning carries the marker")
        _check("v1.0.1" in warning, True, "warning names the missing release")
        msg = f"Merged into master{warning}"
        _check(merge_msg_release_orphaned(msg), True,
               "predicate matches the merge message")
        _check(merge_msg_is_failure(msg), False,
               "NOT a merge failure - the branch landed")
        _git(tmp, "checkout", "-q", "master")
        _check(runner._release_containment_warning(tmp, "v1.0.1"), "",
               "clean line produces no warning")


def test_i_knob_disables_every_surface() -> None:
    print("\n[i] RELEASE_ANCESTRY_CHECK=0 stands the check down")
    from bot.discord import interactions
    from bot.engine import commands as bot_commands

    runner = ClaudeRunner()
    original = config.RELEASE_ANCESTRY_CHECK
    try:
        config.RELEASE_ANCESTRY_CHECK = False
        with tempfile.TemporaryDirectory() as tmp:
            _crossed_repo(tmp)
            _git(tmp, "checkout", "-q", "ship")
            _check(runner._release_containment_warning(tmp, "v1.0.2"), "",
                   "no warning when switched off")
            # The deploy gate and the /branches audit read the same flag, and
            # both are asserted here rather than only in prose: a knob that
            # silences two surfaces out of three is worse than no knob, since
            # the repo it was set for still cannot deploy.
            deploy_src = inspect.getsource(interactions.execute_deploy)
            _check("RELEASE_ANCESTRY_CHECK" in deploy_src, True,
                   "deploy gate reads the flag")
            audit_src = inspect.getsource(bot_commands.on_branches)
            _check("RELEASE_ANCESTRY_CHECK" in audit_src, True,
                   "/branches audit reads the flag")
    finally:
        config.RELEASE_ANCESTRY_CHECK = original


def test_j_discard_reports_what_it_strands() -> None:
    """Deleting a branch does not delete its tags, it strands them.

    The version keeps showing up in ``git tag`` as though it shipped while
    its content is reachable from nothing, which is the likeliest way the
    18 orphans in AIAgent were made.
    """
    print("\n[j] discard names the release tags it strands")
    runner = ClaudeRunner()
    with tempfile.TemporaryDirectory() as tmp:
        repo = os.path.join(tmp, "repo")
        os.makedirs(repo)
        _init_repo(repo)
        branch = f"{config.BRANCH_PREFIX}/t-strand"
        _git(repo, "checkout", "-q", "-b", branch)
        sha = _commit(repo, "v9.9.9: never merged")
        _git(repo, "tag", "v9.9.9", sha)
        _git(repo, "checkout", "-q", "master")
        wt = os.path.join(tmp, "wt")
        _git(repo, "worktree", "add", "-q", wt, branch)
        inst = Instance(
            id="t-strand", name=None, instance_type=InstanceType.TASK,
            prompt="", repo_name="r", repo_path=repo,
            status=InstanceStatus.COMPLETED, mode="build",
            branch=branch, original_branch="master", worktree_path=wt,
        )
        outcome = runner._discard_branch_sync(inst)
        _check("v9.9.9" in outcome.message, True,
               "stranded release named in the discard result")
        _check("unreachable from master" in outcome.message, True,
               "says what actually happened to it")
        _check(orphaned_releases(repo, "master"), ["v9.9.9"],
               "and the audit agrees the tag is stranded")


def test_k_gates_run_before_the_irreversible_step() -> None:
    """Order is the whole point on both paths.

    A deploy warned about after ``git push`` has already published the
    revert, and a chain that deploys and then closes the thread buries the
    only notice the user gets, the same failure the repo-unusable branch
    next door exists to prevent.
    """
    print("\n[k] both gates precede the step they guard")
    from bot.discord import interactions
    from bot.engine import workflows

    deploy_src = inspect.getsource(interactions.execute_deploy)
    gate = deploy_src.find("missing_predecessor_release")
    push = deploy_src.find('"push"')
    _check(gate != -1 and push != -1 and gate < push, True,
           "deploy gate runs before the push to origin")

    merge_src = inspect.getsource(workflows._finalize_merge)
    check = merge_src.find("merge_msg_release_orphaned")
    deploy = merge_src.find("apply_post_merge_deploy")
    close = merge_src.find("close_conversation")
    _check(check != -1 and deploy != -1 and check < deploy, True,
           "chain checks containment before deploying")
    _check(close != -1 and check < close, True,
           "chain checks containment before closing the thread")

    # Discard has two orderings to keep, in opposite directions: the scan has
    # to read the branch while it still exists, and the note has to be worded
    # only once the delete succeeded, or it describes a stranding that did
    # not happen.
    discard_src = inspect.getsource(ClaudeRunner._discard_branch_sync)
    scan = discard_src.find("version_tags(")
    delete = discard_src.find('"branch", "-D"')
    note = discard_src.find("stranded_note = (")
    _check(scan != -1 and delete != -1 and scan < delete, True,
           "stranded scan reads the branch before it is deleted")
    _check(note != -1 and delete < note, True,
           "the stranded note is worded after the delete, not before")


def main() -> int:
    test_a_version_tags_parse_and_order()
    test_b_filters_scope_to_a_branch()
    test_c_healthy_line_is_silent()
    test_d_the_revert_is_caught()
    test_e_never_compares_a_release_to_itself()
    test_e2_old_orphans_do_not_block_forever()
    test_f_nothing_to_compare_against()
    test_g_unreadable_never_blocks()
    test_g2_an_empty_ref_filters_nothing_open()
    test_h_marker_means_do_not_ship_not_merge_failed()
    test_i_knob_disables_every_surface()
    test_j_discard_reports_what_it_strands()
    test_k_gates_run_before_the_irreversible_step()
    print()
    if _failures:
        print(f"FAILED {len(_failures)} check(s):")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

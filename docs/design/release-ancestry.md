# A higher version number is not proof the release is in there

`_stale_version_warning` compares version *numbers*, and a number going up
proves nothing about the content going out. Two builds running in parallel is
all it takes: the second is cut from a base that predates the first one's
release, it lands with a higher number, and the earlier release is reverted in
silence. `DegenAI/AIAgent` carries **18** version tags whose commits are not
reachable from master, dated 31 March to 4 August 2026. The v1.3.2.216 ship on
15 September 2026 was caught only because a session looked at the ancestry by
hand before pushing.

Containment is read from git, never inferred: `git merge-base --is-ancestor`
through `runner._is_ancestor`, and `git tag -l 'v*' --merged/--no-merged`
through `runner.version_tags`. Three surfaces act on it, chosen because each is
a point where the revert is still undoable:

- **On merge.** `_release_containment_warning` appends to the same
  `tag_warning` the stale-version check already returns, so the notice rides
  the existing merge message. It carries `RELEASE_ORPHANED_MARKER`, which
  `merge_msg_release_orphaned` reads and `merge_msg_is_failure` deliberately
  does **not**: the branch landed perfectly well, and treating it as a merge
  failure would hand it to the cleanup and retry path it does not belong in.
  Same pattern as `REPO_UNUSABLE_MARKER` next door. Under autopilot,
  `steps._finalize_merge` stops on the marker **before**
  `apply_post_merge_deploy` and `close_conversation`, so the chain never ships
  a tree that reverts a release and never closes the thread on one. It
  returns `MergeOutcome.STOPPED`, not MERGED and not FAILED: every caller
  (chain, standalone Done, scheduled auto-merge, fleet ship) must neither
  call it merged nor hand it to the conflict banner and retry path.
- **On discard.** A build branch can be the only place a release tag lives,
  and deleting it strands that tag: the version looks shipped and its commits
  are gone. The scan sits **above** the `git branch -D` block in
  `_discard_branch_sync`, because after the delete there is no way back to
  which branch that was, and it is skipped for a preserved branch (nothing is
  stranded if the branch survives).
- **Before deploy.** `execute_deploy` refuses a tree that does not contain the
  last shipped release, **ahead of** the safety-net `git push origin HEAD`.
  Ahead of, not after: pushing first is the irreversible half.

Three things that must not drift:

- **The gate walks back exactly one release; the backlog is an audit.**
  `missing_predecessor_release` names the newest release below the ceiling
  that `ref` does not contain, and the ceiling is the newest release `ref`
  *does* contain (falling back to the newest tag overall only when it contains
  none). A gate that fired on all 18 of AIAgent's orphans would block every
  deploy in that repo and be switched off within a day, which is why
  `orphaned_releases` exists separately and is reported by `/branches` rather
  than at merge time. Verified against live repos: the deploy gate is quiet in
  all nineteen registered ones whose directory still exists, while the audit
  shows 18 orphans in aiagent (of 991 tags) and 3 in this repo.
- **A git read that cannot answer is `None`, not `False`.** `_is_ancestor`
  returns `None` for any exit code other than 0 or 1, and every caller treats
  that as "no finding". A missing repo, a timeout or a corrupt object store
  must cost a warning in the log, never a blocked deploy, because the failure
  mode of a broken check that blocks is that the check gets removed.
- **The check never compares a release to itself.** The merge path passes the
  tag it just cut as `below=`, so the ceiling is that release and the subject
  is the one under it. Without that, every release fails its own containment
  test the moment it is tagged.

`RELEASE_ANCESTRY_CHECK` (`bot/config.py`, default on) stands down all three
surfaces and the audit at once.

Harness: `python scripts/test_release_ancestry.py`

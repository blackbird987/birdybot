"""Verify _hydrate_session_for_account picks the same-account fresh JSONL
over a stale cross-account copy — the exact bug scenario from t-3251.

Sets up:
  fake_accounts/
    protonmail/projects/{encoded_repo}/SID.jsonl  ← FRESH (5 turns)
    klerk/projects/{encoded_repo}/SID.jsonl       ← STALE (1 turn)

Runs hydrate with account_dir=protonmail and cwd=worktree path.  Expects
the target (protonmail/projects/{encoded_worktree}/SID.jsonl) to receive
the FRESH content from same-account, not the STALE content from klerk.
"""
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def main() -> int:
    from bot.claude.runner import ClaudeRunner
    from bot.claude.types import Instance, InstanceStatus, InstanceType

    SID = "8a034075-b541-4675-93eb-12f2c9ec7791"
    FRESH = "fresh-content-with-plan-and-revisions\n" * 5
    STALE = "stale-content-3-options-only\n"

    tmp = Path(tempfile.mkdtemp(prefix="verify_hydrate_"))
    try:
        proton = tmp / "protonmail"
        klerk = tmp / "klerk"
        repo = tmp / "repo"
        worktree = repo / ".worktrees" / "t-test"
        for p in (proton, klerk, repo, worktree):
            p.mkdir(parents=True)

        runner = ClaudeRunner.__new__(ClaudeRunner)
        runner._hydrate_cache = {}
        runner._rebuild_cache = {}

        encoded_repo = runner._encode_project_path(str(repo))
        encoded_worktree = runner._encode_project_path(str(worktree))
        assert encoded_repo != encoded_worktree, "encodings should differ"

        # Seed the two account JSONLs.
        (proton / "projects" / encoded_repo).mkdir(parents=True)
        (proton / "projects" / encoded_repo / f"{SID}.jsonl").write_text(FRESH)

        (klerk / "projects" / encoded_repo).mkdir(parents=True)
        (klerk / "projects" / encoded_repo / f"{SID}.jsonl").write_text(STALE)

        instance = Instance(
            id="t-test",
            name=None,
            instance_type=InstanceType.TASK,
            prompt="x",
            repo_name="repo",
            repo_path=str(repo),
            status=InstanceStatus.RUNNING,
        )
        instance.worktree_path = str(worktree)
        instance.session_id = SID

        # Stub the index rebuild — needs real session_index module otherwise.
        async def _stub_rebuild(*a, **kw):
            return True
        runner._maybe_rebuild_session_index = _stub_rebuild

        target = proton / "projects" / encoded_worktree / f"{SID}.jsonl"
        assert not target.exists(), "target should be missing pre-hydrate"

        with patch("bot.claude.runner.config.CLAUDE_ACCOUNTS",
                   [str(proton), str(klerk)]):
            ok = await runner._hydrate_session_for_account(
                str(proton), str(worktree), SID, instance,
            )

        assert ok, "hydrate should return True"
        assert target.exists(), "target file should exist after hydrate"
        got = target.read_text()
        if got == FRESH:
            print("PASS: hydrated FRESH content from same-account "
                  "(would have been STALE under the buggy code)")
            return 0
        if got == STALE:
            print("FAIL: hydrated STALE content from cross-account — "
                  "same-account fast path is not winning")
            return 1
        print(f"FAIL: unexpected content: {got!r}")
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

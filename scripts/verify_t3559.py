"""In-process verification for t-3559 (skip title-gen jsonls in /session picker).

Exercises:
1. cleanup_stale_temp_jsonls() deletes title-gen jsonls in temp-like project
   dirs but NOT non-temp dirs or non-title sessions.
2. scan_sessions filters out entries whose first user message matches the
   TITLE_PROMPT_MARKER.

Uses a sandbox CLAUDE_PROJECTS_DIR so the real ~/.claude/projects is not
touched. Exit 0 on success, 1 on assertion failure.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

# Ensure we import the worktree copy of bot/, not whichever main-repo copy
# Python's path resolution might find first.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# Patch CLAUDE_PROJECTS_DIR before importing anything that uses it.
sandbox_root = Path(tempfile.mkdtemp(prefix="t3559_verify_"))
fake_projects = sandbox_root / "projects"
fake_projects.mkdir()

from bot import config  # noqa: E402

config.CLAUDE_PROJECTS_DIR = fake_projects

from bot.discord.titles import (  # noqa: E402
    cleanup_stale_temp_jsonls,
    _is_temp_like_project_dir,
)
from bot.engine.sessions import scan_sessions  # noqa: E402


def write_jsonl(proj: Path, session_id: str, first_user_text: str) -> Path:
    """Write a minimal valid CLI session jsonl."""
    proj.mkdir(parents=True, exist_ok=True)
    p = proj / f"{session_id}.jsonl"
    user_record = {
        "type": "user",
        "message": {"content": first_user_text},
        "gitBranch": "master",
    }
    asst_record = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "OK"}]},
        "gitBranch": "master",
    }
    with p.open("w", encoding="utf-8") as f:
        f.write(json.dumps(user_record) + "\n")
        f.write(json.dumps(asst_record) + "\n")
    return p


def main() -> int:
    failures: list[str] = []

    # Set up four project dirs:
    #  - title_temp_dir: looks like a temp dir, contains a title-gen jsonl
    #  - title_temp_user: looks like a temp dir, contains a fake user session
    #  - real_repo: NOT a temp dir, contains a session that starts with the
    #    marker (would be a coincidence; must NOT be deleted by cleanup but
    #    SHOULD be filtered by scan_sessions)
    #  - real_repo_normal: NOT a temp dir, ordinary user session
    title_temp_dir = fake_projects / "C--Users-Foo-AppData-Local-Temp"
    title_temp_user = fake_projects / "-tmp"
    real_repo = fake_projects / "C--Users-Foo-Projects-myapp"
    real_repo_normal = fake_projects / "C--Users-Foo-Projects-other"

    # --- Plant fixtures ---
    title_jsonl_id = uuid4().hex
    user_in_temp_id = uuid4().hex
    coincidence_id = uuid4().hex
    normal_id = uuid4().hex

    title_jsonl = write_jsonl(
        title_temp_dir,
        title_jsonl_id,
        config.TITLE_PROMPT_MARKER + ". Maximum 6 words…\n\nUser asked: foo",
    )
    user_in_temp = write_jsonl(
        title_temp_user,
        user_in_temp_id,
        "Real user-typed prompt that happens to live in /tmp",
    )
    coincidence = write_jsonl(
        real_repo,
        coincidence_id,
        config.TITLE_PROMPT_MARKER + " — but in a real repo, NOT a temp dir",
    )
    normal = write_jsonl(
        real_repo_normal,
        normal_id,
        "Add a button to the settings page",
    )

    # --- Heuristic: temp-like classifier ---
    if not _is_temp_like_project_dir(title_temp_dir):
        failures.append("title_temp_dir should be classified as temp-like")
    if not _is_temp_like_project_dir(title_temp_user):
        failures.append("title_temp_user should be classified as temp-like")
    if _is_temp_like_project_dir(real_repo):
        failures.append("real_repo should NOT be classified as temp-like")
    if _is_temp_like_project_dir(real_repo_normal):
        failures.append("real_repo_normal should NOT be classified as temp-like")

    # --- cleanup_stale_temp_jsonls ---
    removed = cleanup_stale_temp_jsonls()
    if removed != 1:
        failures.append(f"cleanup removed {removed}, expected 1")
    if title_jsonl.exists():
        failures.append("title-gen jsonl should be deleted")
    if not user_in_temp.exists():
        failures.append("user session in temp-like dir should NOT be deleted (no marker)")
    if not coincidence.exists():
        failures.append("real-repo coincidence session must NOT be deleted (not temp-like)")
    if not normal.exists():
        failures.append("normal real-repo session should not be touched")

    # --- scan_sessions backstop filter ---
    # Re-plant the title-gen jsonl so we can check the filter even when cleanup
    # would have caught it. Also test that the coincidence session in real_repo
    # is filtered (because its prompt starts with the marker).
    title_jsonl = write_jsonl(
        title_temp_dir,
        title_jsonl_id,
        config.TITLE_PROMPT_MARKER + ". Maximum 6 words…",
    )
    sessions = scan_sessions(limit=20)
    ids = {s["id"] for s in sessions}
    if title_jsonl_id in ids:
        failures.append("scan_sessions should skip title-gen jsonl by marker")
    if coincidence_id in ids:
        failures.append("scan_sessions should skip coincidence session by marker")
    if user_in_temp_id not in ids:
        failures.append("scan_sessions should INCLUDE user session in temp-like dir")
    if normal_id not in ids:
        failures.append("scan_sessions should INCLUDE normal real-repo session")

    # --- Report ---
    if failures:
        print(f"FAIL — {len(failures)} assertion(s) failed:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS — all assertions held")
    print(f"  cleanup deleted: {removed} (expected 1)")
    print(f"  scan_sessions returned {len(sessions)} entries (expected 2: user_in_temp, normal)")
    print(f"  filtered out: title-gen jsonl + coincidence")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        # Cleanup sandbox
        import shutil
        shutil.rmtree(sandbox_root, ignore_errors=True)
    sys.exit(rc)

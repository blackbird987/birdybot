"""Regression test: the deferred revisions in a repo's TODO.md are edited in place.

The section used to be regenerated whole on every append. That boxed each done
item a second time ("- [ ] [x] ..."), and dropped any sub-heading an owner had
put inside the section. Now items are only the top-level list of the section
(header to the first heading after it), and everything else survives byte for
byte. Temp dirs only, no bot, no session.

Run: python scripts/test_deferred_todo.py
Exit 0 = all pass, exit 1 = failures.
"""

from __future__ import annotations

import _bootstrap  # noqa: F401

import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.store.state import StateStore  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="deferred-todo-")).resolve()
FAILURES: list[str] = []
COUNT = 0


def check(label: str, ok: bool, detail: object = "") -> None:
    global COUNT
    COUNT += 1
    if ok:
        print(f"  ok   {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label} {detail!r}")


def new_store(name: str, todo: str | None) -> tuple[StateStore, Path]:
    repo = TMP / name
    repo.mkdir()
    if todo is not None:
        (repo / "TODO.md").write_text(todo, encoding="utf-8")
    store = StateStore(TMP / f"{name}-state.json", TMP / f"{name}-results")
    store.add_repo(name, str(repo))
    return store, repo / "TODO.md"


OWNER_BLOCK = (
    "### Owner questions\n"
    "- [ ] Owner question: post in the Control Room on a new deploy.json?\n"
    "- [ ] Owner question: which tag does a discard leave?\n"
)

MIXED = (
    "# TODO\n"
    "\n"
    "### Features\n"
    "- [ ] something of the owner's\n"
    "\n"
    "## Deferred Revisions\n"
    "<!-- Auto-managed by code review. Remove items when addressed. -->\n"
    "- [x] [Reliability] An old fixed thing (Low) (stale, 2026-11-20: gone)\n"
    "- [ ] [UX/UI] Open one about the card (Medium)\n"
    "- [ ] [x] [Testing] Boxed twice by the old writer (Low)\n"
    "- [ ] [Performance] Open two about the loop (Low) (queued in the leftovers build, 2026-11-20)\n"
    "\n"
    + OWNER_BLOCK +
    "\n"
    "## Later\n"
    "- [ ] not a deferred item\n"
)


def test_read() -> None:
    store, _ = new_store("read", MIXED)
    items = store.get_deferred_items("read")
    check("only open top-level items are read", items == [
        "[UX/UI] Open one about the card (Medium)",
        "[Performance] Open two about the loop (Low) (queued in the leftovers build, 2026-11-20)",
    ], items)
    check("owner questions are not items", not any("Owner question" in i for i in items))
    check("the next section is not read", not any("not a deferred" in i for i in items))
    check("get_deferred lists the same", store.get_deferred("read").count("\n- ") == 1)


def test_append_in_place() -> None:
    store, todo = new_store("append", MIXED)
    store.append_deferred("append", ["[Security] A new finding (High)"])
    text = todo.read_text(encoding="utf-8")
    expected = MIXED.replace(
        "- [ ] [x] [Testing] Boxed twice by the old writer (Low)\n",
        "- [x] [Testing] Boxed twice by the old writer (Low)\n",
    ).replace(
        "(queued in the leftovers build, 2026-11-20)\n",
        "(queued in the leftovers build, 2026-11-20)\n- [ ] [Security] A new finding (High)\n",
    )
    check("append inserts after the last item and changes nothing else", text == expected, text)
    check("the owner block survives byte for byte", OWNER_BLOCK in text)
    check("no line is boxed twice", "- [ ] [x]" not in text)
    check("the new item reads back",
          "[Security] A new finding (High)" in store.get_deferred_items("append"))

    # Appending again re-boxes nothing and duplicates nothing.
    before = todo.read_text(encoding="utf-8")
    store.append_deferred("append", ["[Security] A new finding (High)"])
    check("a duplicate append writes nothing", todo.read_text(encoding="utf-8") == before)


def test_dedup_against_done() -> None:
    store, todo = new_store("dedup", MIXED)
    before = todo.read_text(encoding="utf-8")
    store.append_deferred("dedup", [
        "[Reliability] An old fixed thing (Medium)",
        "[Testing] Boxed twice by the old writer (High)",
    ])
    check("a done item blocks the same finding coming back", todo.read_text(encoding="utf-8") == before)

    store.append_deferred("dedup", ["[Docs] Same twice (Low)", "[Docs] Same twice (Medium)"])
    text = todo.read_text(encoding="utf-8")
    check("duplicates inside one append collapse", text.count("[Docs] Same twice") == 1, text)


def test_clear() -> None:
    store, todo = new_store("clear", MIXED)
    removed = store.clear_deferred("clear")
    text = todo.read_text(encoding="utf-8")
    check("clear removes the two open items", removed == 2, removed)
    check("done items stay", "- [x] [Reliability] An old fixed thing" in text)
    check("and the boxed-twice one is repaired", "- [x] [Testing] Boxed twice" in text, text)
    check("owner questions are never cleared", OWNER_BLOCK in text)
    check("the owner's other sections stay", "- [ ] something of the owner's" in text
          and "- [ ] not a deferred item" in text)
    check("nothing open is left", store.get_deferred_items("clear") == [])
    check("a second clear removes nothing", store.clear_deferred("clear") == 0)


def test_new_section() -> None:
    store, todo = new_store("fresh", "# TODO\n\n- [ ] an owner line\n\n\n")
    store.append_deferred("fresh", ["[Bug] First one (Low)"])
    text = todo.read_text(encoding="utf-8")
    check("a missing section is added at the end", text == (
        "# TODO\n\n- [ ] an owner line\n\n"
        "## Deferred Revisions\n"
        "<!-- Auto-managed by code review. Remove items when addressed. -->\n"
        "- [ ] [Bug] First one (Low)\n"
    ), text)

    store2, todo2 = new_store("nofile", None)
    store2.append_deferred("nofile", ["[Bug] From nothing (Low)"])
    check("no TODO.md at all still works",
          store2.get_deferred_items("nofile") == ["[Bug] From nothing (Low)"],
          todo2.read_text(encoding="utf-8") if todo2.exists() else None)


def test_empty_section_with_subheading() -> None:
    content = (
        "## Deferred Revisions\n"
        "<!-- Auto-managed by code review. Remove items when addressed. -->\n"
        "### Owner questions\n"
        "- [ ] Owner question: keep this?\n"
    )
    store, todo = new_store("empty", content)
    check("an empty top-level list reads nothing", store.get_deferred_items("empty") == [])
    store.append_deferred("empty", ["[Bug] Goes above the sub-heading (Low)"])
    text = todo.read_text(encoding="utf-8")
    check("the item goes after the comment, above the sub-heading", text == (
        "## Deferred Revisions\n"
        "<!-- Auto-managed by code review. Remove items when addressed. -->\n"
        "- [ ] [Bug] Goes above the sub-heading (Low)\n"
        "\n"
        "### Owner questions\n"
        "- [ ] Owner question: keep this?\n"
    ), text)
    check("and the owner question is still not an item",
          store.get_deferred_items("empty") == ["[Bug] Goes above the sub-heading (Low)"])


def test_no_trailing_newline() -> None:
    content = "## Deferred Revisions\n- [ ] [Bug] Last line (Low)"
    store, todo = new_store("nonl", content)
    store.append_deferred("nonl", ["[Bug] Next (Low)"])
    text = todo.read_text(encoding="utf-8")
    check("a file without a final newline gets the item on its own line",
          text == "## Deferred Revisions\n- [ ] [Bug] Last line (Low)\n- [ ] [Bug] Next (Low)\n", text)


def main() -> int:
    tests = [
        test_read, test_append_in_place, test_dedup_against_done, test_clear,
        test_new_section, test_empty_section_with_subheading, test_no_trailing_newline,
    ]
    for test in tests:
        print(test.__name__)
        try:
            test()
        except Exception:
            FAILURES.append(f"{test.__name__} raised")
            traceback.print_exc()
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED of {COUNT}: {FAILURES}")
        return 1
    print(f"\nall {COUNT} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

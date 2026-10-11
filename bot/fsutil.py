"""Writing a file so a reader never sees half of it.

Stdlib only and free of any bot import, like ``textutil`` and ``procutil``,
so every layer can use it without closing a cycle.

The temp file is created with ``mkstemp`` in the destination's own directory:
same directory because ``os.replace`` is only atomic within one filesystem,
and a unique name because a fixed ``<name>.tmp`` lets two concurrent writers
truncate each other's temp file before either renames it. A temp file is never
left behind on failure, which matters most for files written into a user's
repo, where a stray ``.tmp`` is not gitignored and shows up in ``git status``.

``state.json`` keeps its own writer (fsync, backup rotation) and the session
index keeps its own (it verifies the temp file before swapping it in); both
need a step between write and rename that this helper does not offer.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path | str, text: str, *, encoding: str = "utf-8") -> None:
    """Replace ``path`` with ``text`` in one rename. Creates parent dirs.

    Raises ``OSError`` on failure, with the temp file already removed.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(
    path: Path | str,
    data: Any,
    *,
    indent: int | None = 2,
    ensure_ascii: bool = True,
    trailing_newline: bool = False,
) -> None:
    """``atomic_write_text`` of ``json.dumps(data)``.

    Serialises before touching the disk, so a value that will not encode
    raises without creating anything.
    """
    text = json.dumps(data, indent=indent, ensure_ascii=ensure_ascii)
    if trailing_newline:
        text += "\n"
    atomic_write_text(path, text)

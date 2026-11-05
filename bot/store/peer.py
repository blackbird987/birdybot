"""What this machine knows about questions between machines.

A mixin of ``StateStore`` (bot/store/state.py), which owns the dicts these methods
read and the persistence that saves them. The state machine that decides what goes
in here is ``bot/engine/peer_questions.py``: this only holds it.

Records are plain dicts, keyed by the question's id, so a question seen twice lands
on the same record. Text from the other machine is kept here and is not copied
anywhere that instance listings read from. A question is redacted when it arrives.
Answer parts wait as received, because a secret can straddle two of them, and are
redacted together when the answer is whole and saved.
"""

from __future__ import annotations


class PeerMixin:
    # --- Questions this machine asked ---

    def peer_asked(self, qid: str) -> dict | None:
        return self._peer_asked.get(qid)

    def peer_asked_all(self) -> list[dict]:
        return sorted(self._peer_asked.values(), key=lambda r: r.get("created_at", 0))

    def peer_put_asked(self, record: dict, *, critical: bool = False) -> None:
        self._peer_asked[record["id"]] = record
        self._peer_saved(critical)

    def peer_drop_asked(self, qid: str) -> None:
        if self._peer_asked.pop(qid, None) is not None:
            self.mark_dirty()

    # --- Questions the other machine asked ---

    def peer_received(self, qid: str) -> dict | None:
        return self._peer_received.get(qid)

    def peer_received_all(self) -> list[dict]:
        return sorted(self._peer_received.values(), key=lambda r: r.get("created_at", 0))

    def peer_put_received(self, record: dict, *, critical: bool = False) -> None:
        self._peer_received[record["id"]] = record
        self._peer_saved(critical)

    def peer_drop_received(self, qid: str) -> None:
        if self._peer_received.pop(qid, None) is not None:
            self.mark_dirty()

    # --- How far through each of the other machine's outbox threads we have read ---

    def peer_cursor(self, thread_id: str) -> str | None:
        return self._peer_cursors.get(thread_id)

    def peer_cursor_threads(self) -> list[str]:
        return list(self._peer_cursors)

    def peer_set_cursor(self, thread_id: str, message_id: str) -> None:
        self._peer_cursors[thread_id] = message_id
        self.mark_dirty()

    def peer_drop_cursor(self, thread_id: str) -> None:
        if self._peer_cursors.pop(thread_id, None) is not None:
            self.mark_dirty()

    # --- Daily counts, by kind: "asked" and "carded" ---

    def peer_count_today(self, kind: str, today: str) -> int:
        entry = self._peer_daily.get(kind) or {}
        return entry.get("count", 0) if entry.get("date") == today else 0

    def peer_bump_today(self, kind: str, today: str) -> None:
        self._peer_daily[kind] = {"date": today, "count": self.peer_count_today(kind, today) + 1}
        self.mark_dirty()

    def _peer_saved(self, critical: bool) -> None:
        if critical:
            self.save()
        else:
            self.mark_dirty()

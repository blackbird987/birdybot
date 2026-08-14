"""Optional Outlook integration via Windows COM automation.

Requires: pywin32 (pip install pywin32) + Microsoft Outlook installed.
Gracefully unavailable when dependencies are missing.

CLI usage:
    python outlook.py inbox [count]
    python outlook.py calendar [days]
    python outlook.py search "query" [count]
    python outlook.py unread
    python outlook.py read "subject"
    python outlook.py draft "to" "subject" "body" [attachment1 attachment2 ...]
"""

from __future__ import annotations

import datetime
import logging
import os
import re
import sys
import time

log = logging.getLogger(__name__)

# --- Graceful import of COM dependencies ---

try:
    import pythoncom
    import pywintypes
    import win32com.client

    COM_AVAILABLE = True
except ImportError:
    COM_AVAILABLE = False

# Outlook folder constants (OlDefaultFolders enum)
_FOLDER_INBOX = 6
_FOLDER_CALENDAR = 9
_FOLDER_DRAFTS = 16

# --- Cached COM singleton ---

_app = None
_namespace = None


def _get_namespace():
    """Return a cached MAPI namespace, connecting on first call.

    If the handle is stale (Outlook was restarted), resets and retries once.
    """
    global _app, _namespace

    if not COM_AVAILABLE:
        raise RuntimeError("pywin32 not installed — run: pip install pywin32")

    if _namespace is not None:
        return _namespace

    return _connect()


def _connect():
    """Establish COM connection to Outlook. Called once or on stale-handle retry."""
    global _app, _namespace
    try:
        _app = win32com.client.Dispatch("Outlook.Application")
        _namespace = _app.GetNamespace("MAPI")
        return _namespace
    except Exception as e:
        _app = None
        _namespace = None
        raise RuntimeError(f"Cannot connect to Outlook: {e}") from e


def _with_retry(fn):
    """Call *fn*, retry once on stale COM handle."""
    global _app, _namespace
    try:
        return fn()
    except Exception as first_err:
        # Only retry COM errors, not logic bugs
        if COM_AVAILABLE and isinstance(first_err, pywintypes.com_error):
            log.warning("Stale COM handle, reconnecting: %s", first_err)
            _app = None
            _namespace = None
            try:
                _connect()
                return fn()
            except Exception as retry_err:
                raise RuntimeError(
                    f"Outlook reconnect failed: {retry_err}"
                ) from retry_err
        raise


# How long to keep Outlook alive after saving so its background sync engine
# can upload the item. There is no completion signal to wait on (see
# _sync_to_server), so this is empirical rather than derived: 30s is the wait
# that was used the one time a stranded draft was confirmed to reach the
# server. Not established as the minimum, only as a span that sufficed.
_SYNC_SETTLE_S = 30


def _pump_for(seconds: float) -> None:
    """Idle for *seconds* without letting this thread go dark to COM.

    Staying alive is the point — Outlook is an out-of-process COM server and
    keeps running while we hold references into it, which is the window its
    background sync needs. Pumping rather than sleeping flat is because
    pywin32 puts us in a single-threaded apartment, where a thread that
    blocks without draining its message queue stalls any cross-apartment
    call Outlook tries to make into us.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        pythoncom.PumpWaitingMessages()
        time.sleep(0.2)


def _sync_to_server(settle_s: float = _SYNC_SETTLE_S) -> bool:
    """Nudge a send/receive and stay alive long enough for the upload to land.

    Outlook runs in cached mode: items created over COM land in the local
    .ost first and are uploaded afterwards. The bot drives a *headless*
    Outlook instance, which exits seconds after the script finishes — long
    before that upload happens — so the item is stranded locally and never
    appears in new Outlook / OWA. Holding the process open is what actually
    fixes it; `SyncObject.Start()` is only a nudge.

    There is deliberately no completion check, because on this mailbox no
    usable one exists. Measured on a real Exchange account: the send/receive
    group fires SyncStart, Progress(0/1000) and SyncEnd within 60ms, before
    `Start()` even returns, so `OnSyncEnd` reports the *group* finishing and
    says nothing about a cached-mode upload; and an item's `EntryID` is
    unchanged 45s after creation, so re-keying is not a signal either. The
    upload is done by a background sync engine that reports through neither.
    Waiting a fixed span is therefore the honest mechanism, not a fallback.

    Never raises. That is load-bearing twice over: a failed nudge still
    leaves the item safely saved locally, and callers run inside
    `_with_retry`, which would re-run the whole operation — creating a
    second draft — if a COM error escaped from here.

    Returns:
        True if a send/receive was requested and waited out, False if it
        could not be requested at all. True means "given time to upload",
        NOT "confirmed on the server" — no such confirmation is available.
    """
    try:
        ns = _get_namespace()
        sync_objects = ns.SyncObjects
        if sync_objects.Count == 0:
            log.warning("No Outlook send/receive group — cannot nudge a sync")
            return False
        # Held across the wait rather than released straight after Start():
        # the hand-run sequence this fix is modelled on kept it alive, and
        # dropping the only reference to an in-flight sync is not something
        # worth deviating on for one line.
        sync_obj = sync_objects.Item(1)
        sync_obj.Start()
    except Exception as e:
        log.warning("Could not start an Outlook send/receive: %s", e)
        return False

    try:
        _pump_for(settle_s)
    except Exception as e:  # pumping must not sink an already-saved draft
        log.warning("Interrupted while waiting for Outlook to sync: %s", e)
    del sync_obj
    return True


# --- Helpers ---


def _format_time(dt) -> str:
    """Convert COM datetime to readable string."""
    if hasattr(dt, "strftime"):
        return dt.strftime("%Y-%m-%d %H:%M")
    return str(dt)


def _truncate(text: str, length: int = 200) -> str:
    """Clean and truncate text for preview."""
    if not text:
        return ""
    # Strip to plain text, collapse whitespace
    text = text.strip().replace("\r\n", "\n").replace("\r", "\n")
    # Collapse runs of whitespace/newlines
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    if len(text) > length:
        return text[:length] + "..."
    return text


# --- Public API ---


def read_inbox(count: int = 10) -> list[dict]:
    """Read recent inbox emails."""

    def _do():
        ns = _get_namespace()
        inbox = ns.GetDefaultFolder(_FOLDER_INBOX)
        messages = inbox.Items
        messages.Sort("[ReceivedTime]", True)

        results = []
        for i in range(min(count, messages.Count)):
            msg = messages.Item(i + 1)  # 1-based
            results.append(
                {
                    "from": msg.SenderName,
                    "from_email": getattr(msg, "SenderEmailAddress", ""),
                    "subject": msg.Subject,
                    "date": _format_time(msg.ReceivedTime),
                    "unread": msg.UnRead,
                    "preview": _truncate(msg.Body),
                }
            )
        return results

    return _with_retry(_do)


def read_calendar(days: int = 1) -> list[dict]:
    """Read calendar events for the next N days.

    Tries Restrict filter first; falls back to iterating sorted items
    if the filter fails or returns zero results on a non-empty folder
    (handles locale-dependent date formatting).
    """

    def _do():
        ns = _get_namespace()
        cal = ns.GetDefaultFolder(_FOLDER_CALENDAR)

        now = datetime.datetime.now()
        end = now + datetime.timedelta(days=days)

        # Primary path: Restrict with IncludeRecurrences (expands recurring events)
        filtered_count = 0
        filtered = None
        try:
            items = cal.Items
            items.IncludeRecurrences = True
            items.Sort("[Start]")
            restriction = (
                f"[Start] <= '{end.strftime('%m/%d/%Y %I:%M %p')}' AND "
                f"[End] >= '{now.strftime('%m/%d/%Y %I:%M %p')}'"
            )
            filtered = items.Restrict(restriction)
            # Probe + cache — Count can raise on bad filter
            filtered_count = filtered.Count
        except Exception:
            filtered = None

        results = []

        if filtered is not None and filtered_count > 0:
            try:
                for item in filtered:
                    results.append(_calendar_item_to_dict(item))
            except Exception:
                pass  # COM iteration can fail at boundary
        else:
            # Fallback: fresh Items WITHOUT IncludeRecurrences so that
            # .Count and .Item(i) indexing work reliably.
            fb_items = cal.Items
            fb_items.Sort("[Start]")
            max_scan = min(200, fb_items.Count)
            for i in range(1, max_scan + 1):
                try:
                    item = fb_items.Item(i)
                    start = item.Start
                    if hasattr(start, "timestamp"):
                        item_start = datetime.datetime.fromtimestamp(
                            start.timestamp()
                        )
                    else:
                        item_start = start
                    if item_start > end:
                        break
                    if item_start >= now or (
                        hasattr(item.End, "timestamp")
                        and datetime.datetime.fromtimestamp(
                            item.End.timestamp()
                        )
                        >= now
                    ):
                        results.append(_calendar_item_to_dict(item))
                except Exception:
                    continue

        return results

    return _with_retry(_do)


def _calendar_item_to_dict(item) -> dict:
    """Convert a calendar COM object to a dict."""
    return {
        "subject": item.Subject,
        "start": _format_time(item.Start),
        "end": _format_time(item.End),
        "location": getattr(item, "Location", ""),
        "organizer": getattr(item, "Organizer", ""),
        "all_day": getattr(item, "AllDayEvent", False),
    }


def unread_count() -> int:
    """Get count of unread inbox emails."""

    def _do():
        ns = _get_namespace()
        inbox = ns.GetDefaultFolder(_FOLDER_INBOX)
        return inbox.UnReadItemCount

    return _with_retry(_do)


def search_emails(query: str, count: int = 10) -> list[dict]:
    """Search recent emails by subject or sender (pure Python, no DASL)."""

    def _do():
        ns = _get_namespace()
        inbox = ns.GetDefaultFolder(_FOLDER_INBOX)
        messages = inbox.Items
        messages.Sort("[ReceivedTime]", True)

        q = query.lower()
        results = []
        scan_limit = min(500, messages.Count)
        for i in range(scan_limit):
            msg = messages.Item(i + 1)
            subject = (msg.Subject or "").lower()
            sender = (msg.SenderName or "").lower()
            if q in subject or q in sender:
                results.append(
                    {
                        "from": msg.SenderName,
                        "subject": msg.Subject,
                        "date": _format_time(msg.ReceivedTime),
                        "unread": msg.UnRead,
                        "preview": _truncate(msg.Body),
                    }
                )
                if len(results) >= count:
                    break
        return results

    return _with_retry(_do)


def read_email(subject: str) -> dict | None:
    """Read full email by subject match (searches recent 200)."""

    def _do():
        ns = _get_namespace()
        inbox = ns.GetDefaultFolder(_FOLDER_INBOX)
        messages = inbox.Items
        messages.Sort("[ReceivedTime]", True)

        q = subject.lower()
        for i in range(min(200, messages.Count)):
            msg = messages.Item(i + 1)
            if q in (msg.Subject or "").lower():
                body = (msg.Body or "").strip()
                return {
                    "from": msg.SenderName,
                    "from_email": getattr(msg, "SenderEmailAddress", ""),
                    "to": getattr(msg, "To", ""),
                    "subject": msg.Subject,
                    "date": _format_time(msg.ReceivedTime),
                    "body": body[:5000] if body else "",
                }
        return None

    return _with_retry(_do)


def create_draft(
    to: str,
    subject: str,
    body: str,
    attachments: list[str] | None = None,
) -> dict:
    """Create a draft email in Outlook Drafts folder.

    Args:
        to: Recipient email address(es), semicolon-separated for multiple.
        subject: Email subject line.
        body: Plain text email body.
        attachments: Optional list of absolute file paths to attach.

    Returns:
        Dict with subject and attachment count for confirmation.
    """

    def _do():
        global _app
        _get_namespace()  # ensure connection
        mail = _app.CreateItem(0)  # 0 = olMailItem
        mail.To = to
        mail.Subject = subject
        mail.Body = body
        if attachments:
            for path in attachments:
                abs_path = os.path.abspath(path)
                if not os.path.isfile(abs_path):
                    raise FileNotFoundError(f"Attachment not found: {abs_path}")
                mail.Attachments.Add(abs_path)
        mail.Save()  # saves to Drafts, local .ost only
        # Blocks ~30s: without it the draft never reaches the server and so
        # never shows up in the mail client the user actually reads.
        sync_requested = _sync_to_server()
        return {
            "subject": subject,
            "to": to,
            "attachments": len(attachments) if attachments else 0,
            "sync_requested": sync_requested,
        }

    return _with_retry(_do)


# --- CLI ---


def _print_emails(emails: list[dict]) -> None:
    if not emails:
        print("No emails found.")
        return
    for i, e in enumerate(emails, 1):
        unread = " [UNREAD]" if e.get("unread") else ""
        print(f"{i}. {e['subject']}{unread}")
        print(f"   From: {e['from']} | {e['date']}")
        if e.get("preview"):
            print(f"   {e['preview'][:120]}")
        print()


def _print_calendar(events: list[dict]) -> None:
    if not events:
        print("No upcoming events.")
        return
    for e in events:
        all_day = " (all day)" if e.get("all_day") else ""
        loc = f" @ {e['location']}" if e.get("location") else ""
        print(f"- {e['subject']}{all_day}")
        print(f"  {e['start']} -> {e['end']}{loc}")
        if e.get("organizer"):
            print(f"  Organizer: {e['organizer']}")
        print()


def main(args: list[str] | None = None) -> int:
    """CLI entry point."""
    args = args or sys.argv[1:]
    if not args:
        print("Usage: python outlook.py <command> [options]")
        print("Commands: inbox, calendar, search, unread, read, draft")
        return 1

    cmd = args[0]

    try:
        if cmd == "inbox":
            count = int(args[1]) if len(args) > 1 else 10
            _print_emails(read_inbox(count))
        elif cmd == "calendar":
            days = int(args[1]) if len(args) > 1 else 1
            _print_calendar(read_calendar(days))
        elif cmd == "unread":
            n = unread_count()
            print(f"{n} unread email{'s' if n != 1 else ''}")
        elif cmd == "search":
            if len(args) < 2:
                print("Usage: search <query> [count]")
                return 1
            count = int(args[2]) if len(args) > 2 else 10
            _print_emails(search_emails(args[1], count))
        elif cmd == "read":
            if len(args) < 2:
                print("Usage: read <subject>")
                return 1
            email = read_email(args[1])
            if email:
                print(f"From: {email['from']} ({email.get('from_email', '')})")
                print(f"To: {email['to']}")
                print(f"Subject: {email['subject']}")
                print(f"Date: {email['date']}")
                print(f"\n{email['body']}")
            else:
                print(f"No email found matching '{args[1]}'")
        elif cmd == "draft":
            if len(args) < 4:
                print("Usage: draft <to> <subject> <body> [attachment ...]")
                return 1
            to_addr = args[1]
            subj = args[2]
            body_text = args[3]
            att = args[4:] if len(args) > 4 else None
            result = create_draft(to_addr, subj, body_text, att)
            print(f"Draft created: {result['subject']}")
            print(f"To: {result['to']}")
            if result["attachments"]:
                print(f"Attachments: {result['attachments']}")
            if result["sync_requested"]:
                print(
                    f"Send/receive requested, waited {_SYNC_SETTLE_S}s for Outlook "
                    "to upload it. Delivery to the server is not independently "
                    "confirmable - check your Drafts folder."
                )
            else:
                print(
                    "WARNING: saved locally, but no send/receive could be started - "
                    "it may not appear until Outlook next syncs on its own."
                )
        else:
            print(f"Unknown command: {cmd}")
            return 1
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Outlook error: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

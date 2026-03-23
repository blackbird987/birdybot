"""Optional Outlook integration via Windows COM automation.

Requires: pywin32 (pip install pywin32) + Microsoft Outlook installed.
Gracefully unavailable when dependencies are missing.

CLI usage:
    python outlook.py inbox [count]
    python outlook.py calendar [days]
    python outlook.py search "query" [count]
    python outlook.py unread
    python outlook.py read "subject"
"""

from __future__ import annotations

import datetime
import logging
import re
import sys

log = logging.getLogger(__name__)

# --- Graceful import of COM dependencies ---

try:
    import pywintypes
    import win32com.client

    COM_AVAILABLE = True
except ImportError:
    COM_AVAILABLE = False

# Outlook folder constants (OlDefaultFolders enum)
_FOLDER_INBOX = 6
_FOLDER_CALENDAR = 9

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
        print("Commands: inbox, calendar, search, unread, read")
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

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

WRAPPER_ROOT = Path(__file__).resolve().parents[3]
WRAPPER_SRC = WRAPPER_ROOT / "src"
if WRAPPER_SRC.exists():
    sys.path.insert(0, str(WRAPPER_SRC))

from imessage_wrapper import IMessageClient  # noqa: E402
from imessage_wrapper.core import APPLE_EPOCH, _extract_attributed_body_text  # noqa: E402


@dataclass(frozen=True)
class MessageRow:
    rowid: int
    chat_id: int | None
    timestamp: str | None
    is_from_me: bool
    sender: str
    sender_name: str | None
    chat_name: str
    chat_identifier: str | None
    text: str
    is_match: bool = False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Search local macOS Messages history read-only.")
    parser.add_argument("--query", "-q", action="append", default=[], help="Text or regex to search. Repeat for OR matching.")
    parser.add_argument("--all", action="store_true", help="Return all messages in the date range instead of filtering by query.")
    parser.add_argument("--regex", action="store_true", help="Treat --query values as case-insensitive regular expressions.")
    parser.add_argument("--after", help="Inclusive local date/time, e.g. 2026-06-01 or 2026-06-01T12:30.")
    parser.add_argument("--before", help="Exclusive local date/time, e.g. 2026-06-23 or 2026-06-23T00:00.")
    parser.add_argument("--days", type=int, help="Search the last N days when --after is omitted.")
    parser.add_argument("--limit", type=int, default=100, help="Maximum matching messages, or returned messages when --all is used.")
    parser.add_argument("--scan-limit", type=int, help="Maximum candidate rows to inspect before query matching. Defaults to unbounded.")
    parser.add_argument("--context", type=int, default=0, help="Include N nearby messages before and after each match.")
    parser.add_argument("--from-me", action="store_true", help="Only messages sent by Kyle.")
    parser.add_argument("--not-from-me", action="store_true", help="Only messages not sent by Kyle.")
    parser.add_argument("--include-reactions", action="store_true", help="Include tapbacks/reaction messages.")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--db", default=str(Path.home() / "Library" / "Messages" / "chat.db"))
    parser.add_argument("--home", default=str(Path.home()), help="Home directory for Contacts enrichment.")
    return parser.parse_args()


def parse_local_datetime(value: str, tz: ZoneInfo) -> datetime:
    raw = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return datetime.combine(date.fromisoformat(raw), time.min, tzinfo=tz)
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def datetime_to_apple_ns(value: datetime) -> int:
    return int((value.astimezone(timezone.utc) - APPLE_EPOCH).total_seconds() * 1_000_000_000)


def apple_ns_to_local_iso(value: int | None, tz: ZoneInfo) -> str | None:
    if value in (None, 0):
        return None
    raw = int(value)
    if abs(raw) > 10**12:
        seconds = raw / 1_000_000_000
    elif abs(raw) > 10**9:
        seconds = raw / 1_000_000
    else:
        seconds = raw
    return (APPLE_EPOCH + timedelta(seconds=seconds)).astimezone(tz).isoformat(timespec="minutes")


def message_text(row: sqlite3.Row) -> str:
    return str(row["text"] or row["subject"] or _extract_attributed_body_text(row["attributedBody"]) or "")


def non_reaction_filter(include_reactions: bool) -> str:
    if include_reactions:
        return "1 = 1"
    return "(m.associated_message_type IS NULL OR m.associated_message_type < 2000 OR m.associated_message_type > 3006)"


def build_matcher(queries: list[str], regex: bool):
    needles = [item for item in (query.strip() for query in queries) if item]
    if not needles:
        return lambda text: True
    if regex:
        patterns = [re.compile(item, re.IGNORECASE | re.DOTALL) for item in needles]
        return lambda text: any(pattern.search(text) for pattern in patterns)
    folded = [item.casefold() for item in needles]
    return lambda text: any(item in text.casefold() for item in folded)


def fetch_rows(conn: sqlite3.Connection, args: argparse.Namespace, start_ns: int | None, end_ns: int | None) -> list[sqlite3.Row]:
    filters = [non_reaction_filter(args.include_reactions)]
    params: list[object] = []
    if start_ns is not None:
        filters.append("m.date >= ?")
        params.append(start_ns)
    if end_ns is not None:
        filters.append("m.date < ?")
        params.append(end_ns)
    if args.from_me:
        filters.append("m.is_from_me = 1")
    if args.not_from_me:
        filters.append("m.is_from_me = 0")

    sql = f"""
        SELECT
            m.ROWID AS rowid,
            m.text,
            m.subject,
            m.attributedBody,
            m.date,
            m.is_from_me,
            h.id AS handle_id,
            h.uncanonicalized_id AS uncanonicalized_handle,
            c.ROWID AS chat_id,
            c.display_name,
            c.chat_identifier
        FROM message m
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        LEFT JOIN chat c ON c.ROWID = cmj.chat_id
        WHERE {" AND ".join(f"({item})" for item in filters)}
        ORDER BY m.date DESC, m.ROWID DESC
    """
    return conn.execute(sql, params).fetchall()


def fetch_context_rows(conn: sqlite3.Connection, chat_id: int, rowid: int, context: int, include_reactions: bool) -> list[sqlite3.Row]:
    if context <= 0:
        return []
    reaction_filter = non_reaction_filter(include_reactions)
    before = conn.execute(
        f"""
        SELECT m.ROWID AS rowid, m.text, m.subject, m.attributedBody, m.date, m.is_from_me,
               h.id AS handle_id, h.uncanonicalized_id AS uncanonicalized_handle,
               c.ROWID AS chat_id, c.display_name, c.chat_identifier
        FROM message m
        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        JOIN chat c ON c.ROWID = cmj.chat_id
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        WHERE cmj.chat_id = ? AND m.ROWID < ? AND ({reaction_filter})
        ORDER BY m.ROWID DESC
        LIMIT ?
        """,
        (chat_id, rowid, context),
    ).fetchall()
    after = conn.execute(
        f"""
        SELECT m.ROWID AS rowid, m.text, m.subject, m.attributedBody, m.date, m.is_from_me,
               h.id AS handle_id, h.uncanonicalized_id AS uncanonicalized_handle,
               c.ROWID AS chat_id, c.display_name, c.chat_identifier
        FROM message m
        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        JOIN chat c ON c.ROWID = cmj.chat_id
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        WHERE cmj.chat_id = ? AND m.ROWID > ? AND ({reaction_filter})
        ORDER BY m.ROWID ASC
        LIMIT ?
        """,
        (chat_id, rowid, context),
    ).fetchall()
    return list(reversed(before)) + after


def contact_name(client: IMessageClient, handle: str) -> str | None:
    if not handle:
        return None
    try:
        contact = client.resolve_contact(handle)
    except Exception:
        return None
    return contact.display_name if contact else None


def to_message(row: sqlite3.Row, tz: ZoneInfo, client: IMessageClient, is_match: bool) -> MessageRow:
    handle = row["handle_id"] or row["uncanonicalized_handle"] or ""
    sender = "me" if bool(row["is_from_me"]) else handle or row["chat_identifier"] or "unknown"
    name = None if sender == "me" else contact_name(client, sender)
    chat_name = row["display_name"] or name or row["chat_identifier"] or handle or f"chat:{row['chat_id']}"
    return MessageRow(
        rowid=int(row["rowid"]),
        chat_id=int(row["chat_id"]) if row["chat_id"] is not None else None,
        timestamp=apple_ns_to_local_iso(row["date"], tz),
        is_from_me=bool(row["is_from_me"]),
        sender=sender,
        sender_name=name,
        chat_name=chat_name,
        chat_identifier=row["chat_identifier"],
        text=" ".join(message_text(row).split()),
        is_match=is_match,
    )


def main() -> int:
    args = parse_args()
    if args.from_me and args.not_from_me:
        raise SystemExit("--from-me and --not-from-me are mutually exclusive")
    if args.all and args.query:
        raise SystemExit("--all cannot be combined with --query")
    if not args.all and not any(query.strip() for query in args.query):
        raise SystemExit("provide at least one --query, or use --all with a bounded date range")
    if args.all and not (args.after or args.before or args.days):
        raise SystemExit("--all requires --after/--before or --days to avoid loading the entire database")
    if args.limit < 1:
        raise SystemExit("--limit must be >= 1")
    if args.scan_limit is not None and args.scan_limit < 1:
        raise SystemExit("--scan-limit must be >= 1")
    if args.context < 0:
        raise SystemExit("--context must be >= 0")

    tz = ZoneInfo(os.environ.get("TZ", "America/Los_Angeles"))
    now = datetime.now(tz)
    start = parse_local_datetime(args.after, tz) if args.after else None
    if start is None and args.days is not None:
        start = now - timedelta(days=args.days)
    end = parse_local_datetime(args.before, tz) if args.before else None
    matcher = build_matcher(args.query, args.regex)

    db_path = Path(args.db).expanduser()
    client = IMessageClient(messages_db_path=db_path, home=args.home)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        candidates = fetch_rows(
            conn,
            args,
            datetime_to_apple_ns(start) if start else None,
            datetime_to_apple_ns(end) if end else None,
        )
        scanned_count = 0
        matches: list[sqlite3.Row] = []
        for row in candidates:
            scanned_count += 1
            if args.scan_limit is not None and scanned_count > args.scan_limit:
                break
            if args.all or matcher(message_text(row)):
                matches.append(row)
                if len(matches) >= args.limit:
                    break

        rows_by_id: dict[int, tuple[sqlite3.Row, bool]] = {}
        for row in matches:
            rows_by_id[int(row["rowid"])] = (row, not args.all)
            if args.context and not args.all and row["chat_id"] is not None:
                for context_row in fetch_context_rows(conn, int(row["chat_id"]), int(row["rowid"]), args.context, args.include_reactions):
                    rows_by_id.setdefault(int(context_row["rowid"]), (context_row, False))

        messages = [
            to_message(row, tz, client, is_match)
            for row, is_match in sorted(rows_by_id.values(), key=lambda item: int(item[0]["rowid"]))
        ]
    finally:
        conn.close()

    payload = {
        "db": str(db_path),
        "mode": "all" if args.all else "query",
        "query": args.query,
        "after": start.isoformat(timespec="minutes") if start else None,
        "before": end.isoformat(timespec="minutes") if end else None,
        "scanned_count": scanned_count,
        "match_count": len(matches),
        "returned_count": len(messages),
        "messages": [asdict(message) for message in messages],
    }

    if args.format == "json":
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(
            f"mode={payload['mode']} scanned={payload['scanned_count']} "
            f"matches={payload['match_count']} returned={payload['returned_count']} "
            f"after={payload['after']} before={payload['before']}"
        )
        for message in messages:
            marker = "*" if message.is_match else " "
            sender = message.sender_name or message.sender
            print(f"{marker} {message.timestamp} | chat={message.chat_name} | from={sender} | rowid={message.rowid}")
            print(f"  {message.text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

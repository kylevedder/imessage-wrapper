from __future__ import annotations

import os
import select
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import core
from .contacts_writer import ContactUpdatePayload, ContactWritePayload, ContactsWriter
from .models import (
    Attachment,
    Chat,
    ChatMediaStats,
    ChatMessageStats,
    Contact,
    DateMessageStats,
    EmailAddress,
    MediaStats,
    MediaTypeStats,
    Message,
    MessageStats,
    PhoneNumber,
    Reaction,
    SenderMessageStats,
    SendResult,
    ServiceMessageStats,
)

try:
    import phonenumbers
except ImportError:  # pragma: no cover - pyproject installs it.
    phonenumbers = None  # type: ignore[assignment]


@dataclass(frozen=True)
class _Schema:
    message: set[str]
    chat: set[str]
    chat_message_join: set[str]
    handle: set[str]


@dataclass(frozen=True)
class _SendVerification:
    message: Message
    verified: bool
    delivery_status: str
    message_service: str | None
    message_error: int | None
    error: str | None = None


@dataclass(frozen=True)
class _LogicalMessage:
    rowid: int
    chat_id: int
    sender: str
    text: str
    created_at: datetime | None
    is_from_me: bool
    service: str
    handle_id: int | None
    balloon_bundle_id: str | None
    is_read: bool | None


@dataclass
class _MessageStatsAccumulator:
    total_messages: int = 0
    sent_messages: int = 0
    received_messages: int = 0
    chat_counts: dict[int, int] = field(default_factory=dict)
    chat_dimensions: dict[int, tuple[str, str, str]] = field(default_factory=dict)
    sender_counts: dict[str, int] = field(default_factory=dict)
    service_counts: dict[str, int] = field(default_factory=dict)
    date_counts: dict[str, int] = field(default_factory=dict)

    def add_chat(self, message: _LogicalMessage, dimension: tuple[str, str, str]) -> None:
        self.chat_counts[message.chat_id] = self.chat_counts.get(message.chat_id, 0) + 1
        self.chat_dimensions.setdefault(message.chat_id, dimension)

    def add_global(self, message: _LogicalMessage, zone: Any) -> None:
        self.total_messages += 1
        if message.is_from_me:
            self.sent_messages += 1
        else:
            self.received_messages += 1
            sender = message.sender.strip() or "unknown"
            self.sender_counts[sender] = self.sender_counts.get(sender, 0) + 1
        service = message.service.strip() or "unknown"
        self.service_counts[service] = self.service_counts.get(service, 0) + 1
        created_at = message.created_at or core.APPLE_EPOCH
        date_key = created_at.astimezone(zone).strftime("%Y-%m-%d")
        self.date_counts[date_key] = self.date_counts.get(date_key, 0) + 1


class MessageStatsError(core.IMessageError):
    """Raised when a message statistics request cannot be evaluated safely."""


_URL_PREVIEW_BALLOON_BUNDLE_ID = "com.apple.messages.URLBalloonProvider"


class IMessageClient:
    def __init__(
        self,
        messages_db_path: str | Path | None = None,
        contacts_db_paths: list[str | Path] | None = None,
        contacts_sources_dir: str | Path | None = None,
        home: str | Path | None = None,
        send_timeout: int = core.DEFAULT_SEND_TIMEOUT_SECONDS,
        verification_timeout: float = 10.0,
        verify_sends: bool = True,
        enrich_contacts: bool = True,
        region: str = "US",
    ) -> None:
        self.home = Path(home).expanduser() if home else Path(os.environ.get("IMESSAGE_WRAPPER_HOST_HOME", str(Path.home()))).expanduser()
        self.messages_db_path = Path(messages_db_path).expanduser() if messages_db_path else self.home / "Library" / "Messages" / "chat.db"
        self.contacts_db_paths = self._contacts_paths(contacts_db_paths, contacts_sources_dir)
        self.send_timeout = send_timeout
        self.verification_timeout = verification_timeout
        self.verify_sends = verify_sends
        self.enrich_contacts = enrich_contacts
        self.region = region
        self._contacts_cache: list[Contact] | None = None
        self._contact_index: dict[str, Contact] | None = None

    def chats(self, limit: int = 100, offset: int = 0, unread_only: bool = False) -> list[Chat]:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if offset < 0:
            raise ValueError("offset must be >= 0")
        with self._connect_messages() as conn:
            schema = self._schema(conn)
            if unread_only and "is_read" not in schema.message:
                raise core.IMessageError(
                    "Unread filtering is unavailable because this Messages database has no message.is_read column"
                )
            routing = self._chat_routing_sql(schema)
            last_date_expr = "MAX(cmj.message_date)" if "message_date" in schema.chat_message_join else "MAX(m.date)"
            join_message = "" if "message_date" in schema.chat_message_join else "JOIN message m ON m.ROWID = cmj.message_id"
            unread_join = ""
            if unread_only:
                unread_join = """
                    JOIN (
                        SELECT DISTINCT cmj_unread.chat_id
                        FROM chat_message_join cmj_unread
                        JOIN message m_unread ON m_unread.ROWID = cmj_unread.message_id
                        WHERE m_unread.is_from_me = 0 AND m_unread.is_read = 0
                    ) unread ON unread.chat_id = c.ROWID
                """

            def fetch_rows(batch_limit: int, batch_offset: int) -> list[sqlite3.Row]:
                return conn.execute(
                    f"""
                    SELECT
                        c.ROWID AS chat_id,
                        c.chat_identifier,
                        c.guid,
                        c.display_name,
                        c.service_name,
                        COUNT(DISTINCT cmj.message_id) AS message_count,
                        {last_date_expr} AS last_message_date,
                        {routing}
                    FROM chat c
                    JOIN chat_message_join cmj ON c.ROWID = cmj.chat_id
                    {join_message}
                    {unread_join}
                    GROUP BY c.ROWID
                    ORDER BY last_message_date DESC, c.ROWID DESC
                    LIMIT ? OFFSET ?
                    """,
                    (batch_limit, batch_offset),
                ).fetchall()

            if not unread_only:
                rows = fetch_rows(limit, offset)
                counts = self._unread_counts(conn, schema, [int(row["chat_id"]) for row in rows])
                return [self._row_to_chat(conn, row, counts.get(int(row["chat_id"]))) for row in rows]

            needed = offset + limit
            batch_size = max(50, needed)
            candidate_offset = 0
            matched: list[tuple[sqlite3.Row, int]] = []
            while len(matched) < needed:
                rows = fetch_rows(batch_size, candidate_offset)
                if not rows:
                    break
                counts = self._unread_counts(conn, schema, [int(row["chat_id"]) for row in rows])
                matched.extend(
                    (row, counts[int(row["chat_id"])])
                    for row in rows
                    if counts.get(int(row["chat_id"]), 0) > 0
                )
                if len(rows) < batch_size:
                    break
                candidate_offset += len(rows)
            return [self._row_to_chat(conn, row, count) for row, count in matched[offset:needed]]

    def iter_chats(self, page_size: int = 100) -> Iterator[Chat]:
        offset = 0
        while True:
            batch = self.chats(limit=page_size, offset=offset)
            if not batch:
                return
            yield from batch
            offset += len(batch)

    def chat(
        self,
        chat_id: int | None = None,
        identifier: str | None = None,
        guid: str | None = None,
    ) -> Chat | None:
        if chat_id is None and not identifier and not guid:
            raise ValueError("chat_id, identifier, or guid is required")
        clauses: list[str] = []
        params: list[Any] = []
        if chat_id is not None:
            clauses.append("c.ROWID = ?")
            params.append(chat_id)
        if identifier:
            clauses.append("c.chat_identifier = ?")
            params.append(identifier)
        if guid:
            clauses.append("c.guid = ?")
            params.append(guid)
        with self._connect_messages() as conn:
            schema = self._schema(conn)
            routing = self._chat_routing_sql(schema)
            row = conn.execute(
                f"""
                SELECT
                    c.ROWID AS chat_id,
                    c.chat_identifier,
                    c.guid,
                    c.display_name,
                    c.service_name,
                    (SELECT COUNT(*) FROM chat_message_join cmj WHERE cmj.chat_id = c.ROWID) AS message_count,
                    (SELECT MAX(m.date) FROM message m JOIN chat_message_join cmj ON cmj.message_id = m.ROWID WHERE cmj.chat_id = c.ROWID) AS last_message_date,
                    {routing}
                FROM chat c
                WHERE {" OR ".join(clauses)}
                LIMIT 1
                """,
                params,
            ).fetchone()
            if not row:
                return None
            counts = self._unread_counts(conn, schema, [int(row["chat_id"])])
            return self._row_to_chat(conn, row, counts.get(int(row["chat_id"])))

    def search_chats(self, query: str, limit: int = 25) -> list[Chat]:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        needle = query.strip()
        if not needle:
            raise ValueError("query is required")
        terms = core._query_lookup_terms(needle)
        if not terms:
            raise ValueError("query is required")
        contact_candidates = sorted(self._matching_contact_handle_candidates(needle)) if self.enrich_contacts else []
        with self._connect_messages() as conn:
            schema = self._schema(conn)
            routing = self._chat_routing_sql(schema)
            clauses: list[str] = []
            params: list[Any] = []
            for term in terms:
                like = f"%{term}%"
                compact = f"%{core._compact_lookup_text(term)}%"
                clauses.append(
                    """
                    lower(COALESCE(c.display_name, '')) LIKE ?
                    OR lower(COALESCE(c.chat_identifier, '')) LIKE ?
                    OR lower(COALESCE(c.guid, '')) LIKE ?
                    OR imessage_lookup_normalize(c.display_name) LIKE ?
                    OR imessage_lookup_compact(c.display_name) LIKE ?
                    OR imessage_lookup_normalize(c.chat_identifier) LIKE ?
                    OR imessage_lookup_compact(c.chat_identifier) LIKE ?
                    OR imessage_lookup_normalize(c.guid) LIKE ?
                    OR imessage_lookup_compact(c.guid) LIKE ?
                    OR EXISTS (
                        SELECT 1 FROM chat_handle_join chj
                        JOIN handle h ON h.ROWID = chj.handle_id
                        WHERE chj.chat_id = c.ROWID
                          AND (
                              lower(COALESCE(h.id, '')) LIKE ?
                              OR imessage_lookup_normalize(h.id) LIKE ?
                              OR replace(replace(replace(replace(replace(lower(COALESCE(h.id, '')), '+', ''), ' ', ''), '-', ''), '(', ''), ')', '') LIKE ?
                              OR imessage_lookup_compact(h.id) LIKE ?
                          )
                    )
                    """
                )
                params.extend([like, like, like, like, compact, like, compact, like, compact, like, like, compact, compact])
            if contact_candidates:
                placeholders = ", ".join("?" for _ in contact_candidates)
                contact_handle_parts = [f"h.id IN ({placeholders})"]
                contact_handle_params = [*contact_candidates]
                if "uncanonicalized_id" in schema.handle:
                    contact_handle_parts.append(f"h.uncanonicalized_id IN ({placeholders})")
                    contact_handle_params.extend(contact_candidates)
                clauses.append(
                    f"""
                    c.chat_identifier IN ({placeholders})
                    OR c.guid IN ({placeholders})
                    OR EXISTS (
                        SELECT 1 FROM chat_handle_join chj
                        JOIN handle h ON h.ROWID = chj.handle_id
                        WHERE chj.chat_id = c.ROWID
                          AND ({" OR ".join(contact_handle_parts)})
                    )
                    """
                )
                params.extend([*contact_candidates, *contact_candidates, *contact_handle_params])
            rows = conn.execute(
                f"""
                SELECT
                    c.ROWID AS chat_id,
                    c.chat_identifier,
                    c.guid,
                    c.display_name,
                    c.service_name,
                    (SELECT COUNT(*) FROM chat_message_join cmj WHERE cmj.chat_id = c.ROWID) AS message_count,
                    (SELECT MAX(m.date) FROM message m JOIN chat_message_join cmj ON cmj.message_id = m.ROWID WHERE cmj.chat_id = c.ROWID) AS last_message_date,
                    {routing}
                FROM chat c
                WHERE {" OR ".join(f"({clause})" for clause in clauses)}
                ORDER BY last_message_date DESC
                """,
                params,
            ).fetchall()
            counts = self._unread_counts(conn, schema, [int(row["chat_id"]) for row in rows])
            chats = [self._row_to_chat(conn, row, counts.get(int(row["chat_id"]))) for row in rows]
        scored = []
        for item in chats:
            score = core._lookup_match_score(
                needle,
                [item.name, item.display_name, item.identifier, item.guid, *item.participants],
            )
            if score is not None:
                scored.append((score, item.last_message_at or datetime.min.replace(tzinfo=timezone.utc), item))
        scored.sort(key=lambda item: (-item[0], -item[1].timestamp(), item[2].name.lower()))
        return [item[2] for item in scored[:limit]]

    def _matching_contact_handle_candidates(self, query: str) -> set[str]:
        handles: list[str] = []
        for contact in self._load_contacts():
            score = core._lookup_match_score(
                query,
                [
                    contact.display_name,
                    contact.first_name,
                    contact.middle_name,
                    contact.last_name,
                    contact.nickname,
                    contact.organization,
                    *(phone.value for phone in contact.phones),
                    *(email.value for email in contact.emails),
                ],
            )
            if score is None:
                continue
            handles.extend(phone.value for phone in contact.phones)
            handles.extend(email.value for email in contact.emails)
        return self._handle_candidates(handles)

    def messages(
        self,
        chat_id: int,
        limit: int = 100,
        start: datetime | None = None,
        end: datetime | None = None,
        participants: list[str] | None = None,
        attachments: bool = False,
        convert_attachments: bool = False,
    ) -> list[Message]:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        with self._connect_messages() as conn:
            schema = self._schema(conn)
            select = self._message_select(schema)
            filters = [
                "cmj.chat_id = ?",
                self._non_reaction_filter(schema),
            ]
            params: list[Any] = [chat_id]
            if start:
                filters.append("m.date >= ?")
                params.append(self._datetime_to_apple(start))
            if end:
                filters.append("m.date < ?")
                params.append(self._datetime_to_apple(end))
            if participants:
                placeholders = ", ".join("?" for _ in participants)
                destination = "m.destination_caller_id" if "destination_caller_id" in schema.message else "NULL"
                filters.append(f"COALESCE(NULLIF(h.id, ''), {destination}) COLLATE NOCASE IN ({placeholders})")
                params.extend(participants)
            rows = conn.execute(
                f"""
                SELECT {select}
                FROM message m
                JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                LEFT JOIN handle h ON h.ROWID = m.handle_id
                LEFT JOIN chat c ON c.ROWID = cmj.chat_id
                WHERE {" AND ".join(f"({item})" for item in filters if item)}
                ORDER BY m.date DESC, m.ROWID DESC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
            ordered = list(reversed(rows))
            return self._rows_to_messages(conn, ordered, include_attachments=attachments, convert_attachments=convert_attachments)

    def iter_messages(self, chat_id: int, page_size: int = 500, after: int | None = None) -> Iterator[Message]:
        cursor = after or 0
        while True:
            batch = self.messages_after(cursor, chat_id=chat_id, limit=page_size)
            if not batch:
                return
            yield from batch
            cursor = max(message.id for message in batch)

    def messages_after(
        self,
        rowid: int,
        chat_id: int | None = None,
        limit: int = 100,
        include_reactions: bool = False,
        attachments: bool = False,
    ) -> list[Message]:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        with self._connect_messages() as conn:
            schema = self._schema(conn)
            select = self._message_select(schema)
            filters = ["m.ROWID > ?"]
            params: list[Any] = [rowid]
            if chat_id is not None:
                filters.append("cmj.chat_id = ?")
                params.append(chat_id)
            if not include_reactions:
                filters.append(self._non_reaction_filter(schema))
            rows = conn.execute(
                f"""
                SELECT {select}
                FROM message m
                LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                LEFT JOIN handle h ON h.ROWID = m.handle_id
                LEFT JOIN chat c ON c.ROWID = cmj.chat_id
                WHERE {" AND ".join(f"({item})" for item in filters if item)}
                ORDER BY m.ROWID ASC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
            return self._rows_to_messages(conn, rows, include_attachments=attachments)

    def watch(
        self,
        chat_id: int | None = None,
        since_rowid: int | None = None,
        debounce: float = 0.25,
        poll_interval: float = 5.0,
        include_reactions: bool = False,
        attachments: bool = False,
    ) -> Iterator[Message]:
        cursor = since_rowid if since_rowid is not None else self._max_message_rowid()
        kqueue, fds = self._open_watch_handles()
        try:
            while True:
                batch = self.messages_after(
                    cursor,
                    chat_id=chat_id,
                    limit=100,
                    include_reactions=include_reactions,
                    attachments=attachments,
                )
                if batch:
                    time.sleep(max(0, debounce))
                    for message in batch:
                        yield message
                        cursor = max(cursor, message.id)
                else:
                    self._wait_for_db_change(kqueue, poll_interval)
        finally:
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if kqueue is not None:
                kqueue.close()

    def search_messages(self, query: str, match: str = "contains", limit: int = 50) -> list[Message]:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        needle = query.strip()
        if not needle:
            raise ValueError("query is required")
        if match not in {"contains", "exact"}:
            raise ValueError("match must be 'contains' or 'exact'")
        with self._connect_messages() as conn:
            schema = self._schema(conn)
            select = self._message_select(schema)
            rows: list[sqlite3.Row] = []
            page_size = max(limit * 10, 100)
            offset = 0
            while len(rows) < limit:
                page = conn.execute(
                    f"""
                    SELECT {select}
                    FROM message m
                    LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                    LEFT JOIN handle h ON h.ROWID = m.handle_id
                    LEFT JOIN chat c ON c.ROWID = cmj.chat_id
                    WHERE ({self._non_reaction_filter(schema)})
                    ORDER BY m.date DESC, m.ROWID DESC
                    LIMIT ? OFFSET ?
                    """,
                    (page_size, offset),
                ).fetchall()
                if not page:
                    break
                for row in page:
                    if self._message_text_matches(row, needle, match):
                        rows.append(row)
                        if len(rows) >= limit:
                            break
                offset += len(page)
            return self._rows_to_messages(conn, list(reversed(rows)), include_attachments=False)

    def stats(
        self,
        chat_id: int | None = None,
        include_media: bool = False,
        time_zone: str | None = None,
    ) -> MessageStats:
        """Return snapshot-consistent logical message and optional media statistics."""
        if chat_id is not None and (isinstance(chat_id, bool) or not isinstance(chat_id, int) or chat_id <= 0):
            raise MessageStatsError(f"chat_id must be a positive rowid (received {chat_id})")
        time_zone_name, zone = self._stats_time_zone(time_zone)
        conn = self._connect_messages()
        try:
            conn.execute("BEGIN")
            schema = self._schema(conn)
            if chat_id is not None:
                found = conn.execute("SELECT 1 FROM chat WHERE ROWID = ? LIMIT 1", (chat_id,)).fetchone()
                if found is None:
                    raise MessageStatsError(f"chat_id {chat_id} does not exist")
            accumulator = self._stats_messages(conn, schema, chat_id, zone)
            media = self._stats_media(conn, schema, chat_id) if include_media else None
            return self._build_message_stats(accumulator, time_zone_name, media)
        finally:
            if conn.in_transaction:
                conn.rollback()
            conn.close()

    def message_stats(
        self,
        chat_id: int | None = None,
        include_media: bool = False,
        time_zone: str | None = None,
    ) -> MessageStats:
        """Alias for :meth:`stats` using a descriptive Python API name."""
        return self.stats(chat_id=chat_id, include_media=include_media, time_zone=time_zone)

    def contacts(
        self,
        limit: int = 5000,
        offset: int = 0,
        sort: Literal["name", "recent"] = "name",
    ) -> list[Contact]:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if offset < 0:
            raise ValueError("offset must be >= 0")
        if sort not in {"name", "recent"}:
            raise ValueError("sort must be 'name' or 'recent'")
        all_contacts = self._load_contacts()
        if sort == "recent":
            all_contacts = sorted(
                all_contacts,
                key=lambda contact: (
                    contact.created_at is None,
                    -(contact.created_at.timestamp() if contact.created_at else 0.0),
                    contact.display_name.casefold(),
                    contact.id,
                ),
            )
        return all_contacts[offset:offset + limit]

    def iter_contacts(
        self,
        page_size: int = 5000,
        sort: Literal["name", "recent"] = "name",
    ) -> Iterator[Contact]:
        offset = 0
        while True:
            batch = self.contacts(limit=page_size, offset=offset, sort=sort)
            if not batch:
                return
            yield from batch
            offset += len(batch)

    def search_contacts(self, query: str, limit: int = 25) -> list[Contact]:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        needle = query.strip()
        if not needle:
            raise ValueError("query is required")
        scored = []
        for contact in self._load_contacts():
            score = core._lookup_match_score(
                needle,
                [
                    contact.display_name,
                    contact.first_name,
                    contact.middle_name,
                    contact.last_name,
                    contact.nickname,
                    contact.organization,
                    *(phone.value for phone in contact.phones),
                    *(email.value for email in contact.emails),
                ],
            )
            if score is not None:
                scored.append((score, contact.display_name.lower(), contact))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [item[2] for item in scored[:limit]]

    def resolve_contact(self, handle: str) -> Contact | None:
        return self._contact_index_map().get(self._normalize_handle(handle))

    def send(
        self,
        to: str | None = None,
        chat_id: int | None = None,
        chat_identifier: str | None = None,
        chat_guid: str | None = None,
        text: str = "",
        file_paths: list[str] | None = None,
        service: str = "auto",
        region: str | None = None,
        verify: bool | None = None,
        dry_run: bool = False,
    ) -> SendResult:
        files = self._prepare_files(file_paths or [])
        if not text.strip() and not files:
            raise ValueError("text or file_paths is required")
        target = self._send_target(to=to, chat_id=chat_id, chat_identifier=chat_identifier, chat_guid=chat_guid)
        if dry_run:
            return SendResult(recipient=target, text=text, file_paths=files, dry_run=True, sent=False, verified=False)
        if service not in {"auto", "imessage", "sms"}:
            raise ValueError("service must be 'auto', 'imessage', or 'sms'")
        use_chat = bool(chat_id or chat_identifier or chat_guid)
        normalized_target = target if use_chat else self._normalize_outbound_recipient(target, region or self.region)
        should_verify = verify if verify is not None else self.verify_sends
        pre_send_rowid = self._latest_message_rowid() if should_verify else 0
        staged_files = self._stage_send_files(files)
        sent_at = datetime.now(timezone.utc)
        self._run_send_applescript(
            recipient=normalized_target,
            text=text,
            file_paths=staged_files,
            service=service,
            use_chat=use_chat,
        )
        if chat_identifier or chat_guid:
            self._raise_if_ghost_row(chat_identifier=chat_identifier, chat_guid=chat_guid, sent_at=sent_at)
        verified = None
        message_id = None
        message_guid = None
        delivery_status = None
        message_service = None
        message_error = None
        send_error = None
        if should_verify:
            found = self._wait_for_sent_message(
                text=text,
                chat_id=chat_id,
                chat_identifier=chat_identifier,
                chat_guid=chat_guid,
                recipient=None if use_chat else normalized_target,
                min_rowid=pre_send_rowid,
                sent_at=sent_at,
            )
            verified = found.verified if found is not None else False if text.strip() else None
            if found is not None:
                message_id = found.message.id
                message_guid = found.message.guid
                delivery_status = found.delivery_status
                message_service = found.message_service
                message_error = found.message_error
                send_error = found.error
        return SendResult(
            recipient=target,
            text=text,
            file_paths=files,
            sent=True,
            verified=verified,
            delivery_status=delivery_status,
            message_service=message_service,
            message_error=message_error,
            message_id=message_id,
            message_guid=message_guid,
            error=send_error,
        )

    def create_contact(
        self,
        first_name: str = "",
        last_name: str = "",
        middle_name: str = "",
        nickname: str = "",
        organization: str = "",
        phones: list[str] | None = None,
        emails: list[str] | None = None,
    ) -> str:
        return ContactsWriter().create_contact(
            ContactWritePayload(
                first_name=first_name,
                last_name=last_name,
                middle_name=middle_name,
                nickname=nickname,
                organization=organization,
                phones=tuple(phones or ()),
                emails=tuple(emails or ()),
            )
        )

    def update_contact(
        self,
        contact_id: str,
        first_name: str | None = None,
        last_name: str | None = None,
        middle_name: str | None = None,
        nickname: str | None = None,
        organization: str | None = None,
        phones: list[str] | None = None,
        emails: list[str] | None = None,
    ) -> str:
        return ContactsWriter().update_contact(
            contact_id,
            ContactUpdatePayload(
                first_name=first_name,
                last_name=last_name,
                middle_name=middle_name,
                nickname=nickname,
                organization=organization,
                phones=tuple(phones) if phones is not None else None,
                emails=tuple(emails) if emails is not None else None,
            ),
        )

    def _stats_time_zone(self, identifier: str | None) -> tuple[str, Any]:
        if identifier is not None:
            value = str(identifier).strip()
            if not value:
                raise MessageStatsError("invalid IANA time zone: ")
            try:
                zone = ZoneInfo(value)
                return self._canonical_time_zone_name(value), zone
            except (ValueError, ZoneInfoNotFoundError) as exc:
                raise MessageStatsError(f"invalid IANA time zone: {value}") from exc

        configured = os.environ.get("TZ", "").strip()
        if configured:
            try:
                return self._canonical_time_zone_name(configured), ZoneInfo(configured)
            except (ValueError, ZoneInfoNotFoundError):
                pass
        try:
            resolved_localtime = str(Path("/etc/localtime").resolve())
            marker = "/zoneinfo/"
            if marker in resolved_localtime:
                local_identifier = resolved_localtime.split(marker, 1)[1]
                return self._canonical_time_zone_name(local_identifier), ZoneInfo(local_identifier)
        except (OSError, ValueError, ZoneInfoNotFoundError):
            pass
        local_zone = datetime.now().astimezone().tzinfo or timezone.utc
        local_name = str(getattr(local_zone, "key", None) or local_zone)
        return self._canonical_time_zone_name(local_name), local_zone

    def _canonical_time_zone_name(self, identifier: str) -> str:
        if identifier in {
            "Etc/GMT",
            "Etc/UCT",
            "Etc/UTC",
            "Etc/Universal",
            "Etc/Zulu",
            "GMT",
            "GMT0",
            "UCT",
            "UTC",
            "Universal",
            "Zulu",
        }:
            return "GMT"
        return identifier

    def _stats_messages(
        self,
        conn: sqlite3.Connection,
        schema: _Schema,
        chat_id: int | None,
        zone: Any,
    ) -> _MessageStatsAccumulator:
        select = self._logical_message_select(schema)

        def chat_col(name: str, fallback: str = "''") -> str:
            return f"c.{name}" if name in schema.chat else fallback

        filters = [self._logical_non_reaction_filter(schema)]
        params: list[Any] = []
        if chat_id is not None:
            filters.append("cmj.chat_id = ?")
            params.append(chat_id)
        rows = conn.execute(
            f"""
            SELECT DISTINCT
                {select},
                {chat_col("chat_identifier")} AS stats_chat_identifier,
                {chat_col("display_name")} AS stats_chat_display_name,
                {chat_col("service_name")} AS stats_chat_service
            FROM message m
            JOIN (SELECT DISTINCT chat_id, message_id FROM chat_message_join) cmj
              ON cmj.message_id = m.ROWID
            JOIN chat c ON c.ROWID = cmj.chat_id
            LEFT JOIN handle h ON h.ROWID = m.handle_id
            WHERE {" AND ".join(f"({item})" for item in filters)}
            ORDER BY m.ROWID ASC, cmj.chat_id ASC
            """,
            params,
        )
        accumulator = _MessageStatsAccumulator()
        last_by_chat: dict[int, _LogicalMessage] = {}
        current_rowid: int | None = None
        global_message: _LogicalMessage | None = None
        # Row ordering keeps every chat association for one physical message
        # contiguous, so global dedupe needs only the current group.
        for row in rows:
            message = self._logical_message_from_row(row)
            identifier = str(row["stats_chat_identifier"] or "")
            name = str(row["stats_chat_display_name"] or identifier or "unknown")
            service = str(row["stats_chat_service"] or "unknown")
            if current_rowid is not None and message.rowid != current_rowid:
                if global_message is not None:
                    accumulator.add_global(global_message, zone)
                global_message = None
            current_rowid = message.rowid

            previous = last_by_chat.get(message.chat_id)
            if previous is not None and self._can_coalesce_url_preview(previous, message):
                continue
            last_by_chat[message.chat_id] = message
            accumulator.add_chat(message, (identifier, name, service))
            if global_message is None:
                global_message = message
        if global_message is not None:
            accumulator.add_global(global_message, zone)
        return accumulator

    def _build_message_stats(
        self,
        accumulator: _MessageStatsAccumulator,
        time_zone_name: str,
        media: MediaStats | None,
    ) -> MessageStats:
        chats = []
        for scoped_chat_id, count in accumulator.chat_counts.items():
            identifier, name, service = accumulator.chat_dimensions.get(
                scoped_chat_id,
                ("", "unknown", "unknown"),
            )
            chats.append(
                ChatMessageStats(
                    chat_id=scoped_chat_id,
                    identifier=identifier,
                    name=name,
                    service=service,
                    message_count=count,
                )
            )
        chats.sort(key=lambda item: (-item.message_count, item.chat_id))
        senders = sorted(
            (
                SenderMessageStats(handle=handle, message_count=count)
                for handle, count in accumulator.sender_counts.items()
            ),
            key=lambda item: (-item.message_count, item.handle),
        )
        services = sorted(
            (
                ServiceMessageStats(service=service, message_count=count)
                for service, count in accumulator.service_counts.items()
            ),
            key=lambda item: (-item.message_count, item.service),
        )
        dates = [
            DateMessageStats(date=date, message_count=accumulator.date_counts[date])
            for date in sorted(accumulator.date_counts)
        ]
        return MessageStats(
            total_messages=accumulator.total_messages,
            sent_messages=accumulator.sent_messages,
            received_messages=accumulator.received_messages,
            time_zone=time_zone_name,
            chats=chats,
            senders=senders,
            services=services,
            dates=dates,
            media=media,
        )

    def _stats_media(
        self,
        conn: sqlite3.Connection,
        schema: _Schema,
        chat_id: int | None,
    ) -> MediaStats:
        required = ("attachment", "message_attachment_join")
        for table in required:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
                (table,),
            ).fetchone()
            if exists is None:
                raise MessageStatsError(
                    "media statistics are unavailable because attachment tables are missing"
                )

        attachment_columns = self._table_columns(conn, "attachment")

        def attachment_col(name: str, fallback: str = "''") -> str:
            return f"a.{name}" if name in attachment_columns else fallback

        def chat_col(name: str, fallback: str = "''") -> str:
            return f"c.{name}" if name in schema.chat else fallback

        filters = [self._logical_non_reaction_filter(schema)]
        params: list[Any] = []
        if chat_id is not None:
            filters.append("cmj.chat_id = ?")
            params.append(chat_id)
        rows = conn.execute(
            f"""
            SELECT DISTINCT
                a.ROWID AS attachment_id,
                cmj.chat_id AS media_chat_id,
                {chat_col("chat_identifier")} AS media_chat_identifier,
                {chat_col("display_name")} AS media_chat_display_name,
                {attachment_col("uti")} AS media_uti,
                {attachment_col("mime_type")} AS media_mime_type,
                {attachment_col("total_bytes", "0")} AS media_total_bytes
            FROM attachment a
            JOIN message_attachment_join maj ON maj.attachment_id = a.ROWID
            JOIN message m ON m.ROWID = maj.message_id
            JOIN (SELECT DISTINCT chat_id, message_id FROM chat_message_join) cmj
              ON cmj.message_id = m.ROWID
            JOIN chat c ON c.ROWID = cmj.chat_id
            WHERE {" AND ".join(f"({item})" for item in filters)}
            ORDER BY a.ROWID ASC, cmj.chat_id ASC
            """,
            params,
        )

        total_attachments = 0
        aggregate_bytes = 0
        current_attachment_id: int | None = None
        type_counts: dict[tuple[str, str], tuple[int, int]] = {}
        chat_totals: dict[int, tuple[str, str, int, int]] = {}
        # DISTINCT removes duplicate join rows; ordering makes global attachment
        # dedupe a constant-memory comparison while retaining per-chat totals.
        for row in rows:
            attachment_id = int(row["attachment_id"])
            scoped_chat_id = int(row["media_chat_id"])
            uti = str(row["media_uti"] or "").strip() or "unknown"
            mime_type = str(row["media_mime_type"] or "").strip() or "unknown"
            total_bytes = max(int(row["media_total_bytes"] or 0), 0)
            if attachment_id != current_attachment_id:
                current_attachment_id = attachment_id
                total_attachments += 1
                aggregate_bytes += total_bytes
                count, byte_count = type_counts.get((uti, mime_type), (0, 0))
                type_counts[(uti, mime_type)] = (count + 1, byte_count + total_bytes)
            identifier = str(row["media_chat_identifier"] or "")
            name = str(row["media_chat_display_name"] or identifier or "unknown")
            _, _, count, byte_count = chat_totals.get(scoped_chat_id, (identifier, name, 0, 0))
            chat_totals[scoped_chat_id] = (identifier, name, count + 1, byte_count + total_bytes)

        types = [
            MediaTypeStats(
                uti=uti,
                mime_type=mime_type,
                attachment_count=count,
                total_bytes=byte_count,
            )
            for (uti, mime_type), (count, byte_count) in type_counts.items()
        ]
        types.sort(key=lambda item: (-item.attachment_count, -item.total_bytes, item.uti, item.mime_type))

        chats = [
            ChatMediaStats(
                chat_id=scoped_chat_id,
                identifier=identifier,
                name=name,
                attachment_count=count,
                total_bytes=byte_count,
            )
            for scoped_chat_id, (identifier, name, count, byte_count) in chat_totals.items()
        ]
        chats.sort(key=lambda item: (-item.attachment_count, -item.total_bytes, item.chat_id))
        return MediaStats(
            total_attachments=total_attachments,
            total_bytes=aggregate_bytes,
            types=types,
            chats=chats,
        )

    def _contacts_paths(
        self,
        contacts_db_paths: list[str | Path] | None,
        contacts_sources_dir: str | Path | None,
    ) -> list[Path]:
        if contacts_db_paths is not None:
            return [Path(path).expanduser() for path in contacts_db_paths]
        primary = self.home / "Library" / "Application Support" / "AddressBook" / "AddressBook-v22.abcddb"
        source_dir = Path(contacts_sources_dir).expanduser() if contacts_sources_dir else self.home / "Library" / "Application Support" / "AddressBook" / "Sources"
        paths = [primary]
        if source_dir.exists():
            paths.extend(sorted(source_dir.glob("*/AddressBook-v22.abcddb")))
        deduped = []
        seen = set()
        for path in paths:
            key = str(path)
            if key not in seen:
                seen.add(key)
                deduped.append(path)
        return deduped

    def _connect_messages(self) -> sqlite3.Connection:
        if not self.messages_db_path.exists():
            raise core.IMessageError(f"Messages database not found at {self.messages_db_path}")
        conn = sqlite3.connect(f"file:{self.messages_db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        core._register_lookup_functions(conn)
        return conn

    def _schema(self, conn: sqlite3.Connection) -> _Schema:
        return _Schema(
            message=self._table_columns(conn, "message"),
            chat=self._table_columns(conn, "chat"),
            chat_message_join=self._table_columns(conn, "chat_message_join"),
            handle=self._table_columns(conn, "handle"),
        )

    def _table_columns(self, conn: sqlite3.Connection, table: str) -> set[str]:
        try:
            return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        except sqlite3.Error:
            return set()

    def _latest_message_rowid(self) -> int:
        with self._connect_messages() as conn:
            row = conn.execute("SELECT MAX(ROWID) AS max_rowid FROM message").fetchone()
        return int(row["max_rowid"] or 0) if row else 0

    def _chat_routing_sql(self, schema: _Schema) -> str:
        return ", ".join(
            [
                "c.account_id AS account_id" if "account_id" in schema.chat else "NULL AS account_id",
                "c.account_login AS account_login" if "account_login" in schema.chat else "NULL AS account_login",
                "c.last_addressed_handle AS last_addressed_handle" if "last_addressed_handle" in schema.chat else "NULL AS last_addressed_handle",
            ]
        )

    def _message_select(self, schema: _Schema) -> str:
        def col(name: str, fallback: str = "NULL") -> str:
            return f"m.{name}" if name in schema.message else fallback

        return f"""
            m.ROWID AS message_id,
            cmj.chat_id AS chat_id,
            {col("guid")} AS guid,
            m.handle_id AS handle_rowid,
            h.id AS sender,
            COALESCE(NULLIF(m.text, ''), {col("subject")}, '') AS text,
            {col("attributedBody")} AS attributed_body,
            m.date AS message_date,
            m.is_from_me AS is_from_me,
            {col("is_read", "NULL")} AS is_read,
            {col("date_read", "NULL")} AS date_read,
            m.service AS message_service,
            {col("is_sent", "NULL")} AS is_sent,
            {col("is_delivered", "NULL")} AS is_delivered,
            {col("is_finished", "NULL")} AS is_finished,
            {col("error", "NULL")} AS message_error,
            {col("date_delivered", "NULL")} AS date_delivered,
            {col("associated_message_guid")} AS associated_message_guid,
            {col("associated_message_type")} AS associated_message_type,
            {col("associated_message_emoji")} AS associated_message_emoji,
            {col("thread_originator_guid")} AS thread_originator_guid,
            {col("destination_caller_id")} AS destination_caller_id,
            c.chat_identifier,
            c.guid AS chat_guid,
            c.display_name AS chat_display_name,
            c.service_name AS chat_service
        """

    def _non_reaction_filter(self, schema: _Schema) -> str:
        return self._preview_predecessor_filter(schema)

    def _preview_predecessor_filter(self, schema: _Schema, alias: str = "m") -> str:
        if "associated_message_type" not in schema.message:
            return "1 = 1"
        column = f"{alias}.associated_message_type"
        return f"({column} IS NULL OR {column} < 2000 OR {column} > 3006)"

    def _logical_non_reaction_filter(self, schema: _Schema, alias: str = "m") -> str:
        if "associated_message_type" not in schema.message:
            return "1 = 1"
        column = f"{alias}.associated_message_type"
        return (
            f"({column} IS NULL OR "
            f"({column} NOT BETWEEN 2000 AND 2006 AND {column} NOT BETWEEN 3000 AND 3006))"
        )

    def _logical_message_select(
        self,
        schema: _Schema,
        message_alias: str = "m",
        chat_alias: str = "cmj",
        handle_alias: str = "h",
        prefix: str = "logical",
    ) -> str:
        def col(name: str, fallback: str = "NULL") -> str:
            return f"{message_alias}.{name}" if name in schema.message else fallback

        return f"""
            {message_alias}.ROWID AS {prefix}_message_id,
            {chat_alias}.chat_id AS {prefix}_chat_id,
            {col("handle_id")} AS {prefix}_handle_id,
            {handle_alias}.id AS {prefix}_sender,
            {col("text", "''")} AS {prefix}_text,
            {col("subject")} AS {prefix}_subject,
            {col("attributedBody")} AS {prefix}_attributed_body,
            {col("date", "0")} AS {prefix}_date,
            {col("is_from_me", "0")} AS {prefix}_is_from_me,
            {col("service", "''")} AS {prefix}_service,
            {col("destination_caller_id", "''")} AS {prefix}_destination_caller_id,
            {col("balloon_bundle_id")} AS {prefix}_balloon_bundle_id,
            {col("is_read")} AS {prefix}_is_read
        """

    def _logical_message_from_row(self, row: sqlite3.Row, prefix: str = "logical") -> _LogicalMessage:
        raw_text = row[f"{prefix}_text"] or row[f"{prefix}_subject"] or ""
        text = raw_text or core._extract_attributed_body_text(row[f"{prefix}_attributed_body"]) or ""
        sender = str(row[f"{prefix}_sender"] or row[f"{prefix}_destination_caller_id"] or "")
        return _LogicalMessage(
            rowid=int(row[f"{prefix}_message_id"]),
            chat_id=int(row[f"{prefix}_chat_id"]),
            sender=sender,
            text=str(text),
            created_at=self._apple_to_datetime(row[f"{prefix}_date"]),
            is_from_me=bool(row[f"{prefix}_is_from_me"]),
            service=str(row[f"{prefix}_service"] or "unknown"),
            handle_id=(
                int(row[f"{prefix}_handle_id"])
                if row[f"{prefix}_handle_id"] is not None
                else None
            ),
            balloon_bundle_id=(
                str(row[f"{prefix}_balloon_bundle_id"])
                if row[f"{prefix}_balloon_bundle_id"]
                else None
            ),
            is_read=(
                bool(row[f"{prefix}_is_read"])
                if row[f"{prefix}_is_read"] is not None
                else None
            ),
        )

    def _can_coalesce_url_preview(self, text_message: _LogicalMessage, preview: _LogicalMessage) -> bool:
        if preview.balloon_bundle_id != _URL_PREVIEW_BALLOON_BUNDLE_ID:
            return False
        if text_message.balloon_bundle_id is not None or text_message.chat_id != preview.chat_id:
            return False
        if text_message.is_from_me != preview.is_from_me or text_message.sender != preview.sender:
            return False
        if (
            text_message.handle_id is not None
            and preview.handle_id is not None
            and text_message.handle_id != preview.handle_id
        ):
            return False
        if preview.rowid <= text_message.rowid or text_message.created_at is None or preview.created_at is None:
            return False
        delta = (preview.created_at - text_message.created_at).total_seconds()
        if delta < 0 or delta > 5:
            return False
        preview_text = preview.text.strip()
        if not preview_text.casefold().startswith(("http://", "https://", "www.")):
            return False
        folded_text = text_message.text.casefold()
        candidates = {preview_text, preview_text.strip("/")}
        return any(candidate and candidate.casefold() in folded_text for candidate in candidates)

    def _unread_counts(
        self,
        conn: sqlite3.Connection,
        schema: _Schema,
        chat_ids: list[int],
    ) -> dict[int, int]:
        unique_chat_ids = list(dict.fromkeys(chat_ids))
        if "is_read" not in schema.message or not unique_chat_ids:
            return {}
        result = {chat_id: 0 for chat_id in unique_chat_ids}
        logical_ids: dict[int, set[int]] = {chat_id: set() for chat_id in unique_chat_ids}
        for start in range(0, len(unique_chat_ids), 500):
            chunk = unique_chat_ids[start:start + 500]
            placeholders = ", ".join("?" for _ in chunk)
            candidate_predicate = "m.is_read = 0"
            candidate_params: list[Any] = [*chunk]
            if "balloon_bundle_id" in schema.message:
                candidate_predicate = "(m.is_read = 0 OR m.balloon_bundle_id = ?)"
                candidate_params.append(_URL_PREVIEW_BALLOON_BUNDLE_ID)
            message_select = self._logical_message_select(
                schema,
                message_alias="m",
                chat_alias="candidate",
                handle_alias="h",
                prefix="logical",
            )
            previous_select = self._logical_message_select(
                schema,
                message_alias="previous_m",
                chat_alias="candidate",
                handle_alias="previous_h",
                prefix="previous",
            )
            rows = conn.execute(
                f"""
                WITH unread_candidates AS (
                    SELECT DISTINCT cmj.chat_id, m.ROWID AS message_id
                    FROM message m
                    JOIN (SELECT DISTINCT chat_id, message_id FROM chat_message_join) cmj
                      ON cmj.message_id = m.ROWID
                    WHERE cmj.chat_id IN ({placeholders})
                      AND m.is_from_me = 0
                      AND {candidate_predicate}
                ), candidate_rows AS (
                    SELECT candidate.chat_id, candidate.message_id,
                           (
                               SELECT previous_m.ROWID
                               FROM message previous_m
                               JOIN (
                                   SELECT DISTINCT chat_id, message_id
                                   FROM chat_message_join
                               ) previous_cmj ON previous_cmj.message_id = previous_m.ROWID
                               WHERE previous_cmj.chat_id = candidate.chat_id
                                 AND previous_m.ROWID < candidate.message_id
                                 AND ({self._preview_predecessor_filter(schema, alias="previous_m")})
                               ORDER BY previous_m.ROWID DESC
                               LIMIT 1
                           ) AS previous_id
                    FROM unread_candidates candidate
                )
                SELECT {message_select}, {previous_select}
                FROM candidate_rows candidate
                JOIN message m ON m.ROWID = candidate.message_id
                LEFT JOIN handle h ON h.ROWID = m.handle_id
                LEFT JOIN message previous_m ON previous_m.ROWID = candidate.previous_id
                LEFT JOIN handle previous_h ON previous_h.ROWID = previous_m.handle_id
                ORDER BY candidate.chat_id ASC, m.ROWID ASC
                """,
                candidate_params,
            )
            preview_state: dict[int, tuple[int, _LogicalMessage | None]] = {}
            for row in rows:
                message = self._logical_message_from_row(row)
                logical_id = message.rowid
                if (
                    message.balloon_bundle_id == _URL_PREVIEW_BALLOON_BUNDLE_ID
                    and row["previous_message_id"] is not None
                ):
                    previous = self._logical_message_from_row(row, prefix="previous")
                    resolved_base: _LogicalMessage | None = None
                    if self._can_coalesce_url_preview(previous, message):
                        resolved_base = previous
                    elif previous.balloon_bundle_id == _URL_PREVIEW_BALLOON_BUNDLE_ID:
                        carried = preview_state.get(message.chat_id)
                        if (
                            carried is not None
                            and carried[0] == previous.rowid
                            and carried[1] is not None
                            and self._can_coalesce_url_preview(carried[1], message)
                        ):
                            resolved_base = carried[1]
                    preview_state[message.chat_id] = (message.rowid, resolved_base)
                    if resolved_base is not None:
                        if resolved_base.is_read is not False:
                            continue
                        logical_id = resolved_base.rowid
                    elif message.is_read is not False:
                        continue
                elif message.is_read is not False:
                    continue
                logical_ids[message.chat_id].add(logical_id)
        for chat_id, rowids in logical_ids.items():
            result[chat_id] = len(rowids)
        return result

    def _row_to_chat(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        unread_count: int | None = None,
    ) -> Chat:
        participants = self._participants(conn, int(row["chat_id"]))
        contacts = [contact for handle in participants if (contact := self.resolve_contact(handle))] if self.enrich_contacts else []
        contact_name = contacts[0].display_name if contacts else None
        display_name = self._row_value(row, "display_name")
        identifier = self._row_value(row, "chat_identifier") or ""
        guid = self._row_value(row, "guid")
        name = display_name or contact_name or identifier or guid or str(row["chat_id"])
        return Chat(
            id=int(row["chat_id"]),
            identifier=identifier,
            guid=guid,
            name=name,
            display_name=display_name,
            contact_name=contact_name,
            service=self._row_value(row, "service_name"),
            is_group=self._is_group(identifier, guid),
            participants=participants,
            contacts=contacts,
            last_message_at=self._apple_to_datetime(row["last_message_date"]),
            message_count=int(row["message_count"] or 0),
            account_id=self._row_value(row, "account_id"),
            account_login=self._row_value(row, "account_login"),
            last_addressed_handle=self._row_value(row, "last_addressed_handle"),
            unread_count=unread_count,
        )

    def _rows_to_messages(
        self,
        conn: sqlite3.Connection,
        rows: list[sqlite3.Row],
        include_attachments: bool,
        convert_attachments: bool = False,
    ) -> list[Message]:
        rowids = [int(row["message_id"]) for row in rows]
        attachments = self._fetch_attachments(conn, rowids, convert=convert_attachments) if include_attachments else {}
        reactions = self._fetch_reactions(conn, [str(row["guid"]) for row in rows if row["guid"]])
        messages = []
        for row in rows:
            text = row["text"] or core._extract_attributed_body_text(row["attributed_body"]) or ""
            is_from_me = bool(row["is_from_me"])
            sender = self._row_value(row, "sender") if not is_from_me else self._row_value(row, "destination_caller_id") or "me"
            contact = self.resolve_contact(sender or "") if self.enrich_contacts and sender != "me" else None
            participants = self._participants(conn, int(row["chat_id"])) if row["chat_id"] is not None else []
            associated_type = row["associated_message_type"]
            reaction_type = core.REACTION_LABELS.get(int(associated_type or 0)) if associated_type else None
            guid = self._row_value(row, "guid")
            messages.append(
                Message(
                    id=int(row["message_id"]),
                    chat_id=int(row["chat_id"] or 0),
                    guid=guid,
                    sender=sender,
                    text=text,
                    created_at=self._apple_to_datetime(row["message_date"]),
                    is_from_me=is_from_me,
                    service=self._row_value(row, "message_service") or self._row_value(row, "chat_service"),
                    handle_id=int(row["handle_rowid"]) if row["handle_rowid"] is not None else None,
                    chat_identifier=self._row_value(row, "chat_identifier"),
                    chat_guid=self._row_value(row, "chat_guid"),
                    chat_name=self._row_value(row, "chat_display_name") or self._row_value(row, "chat_identifier"),
                    participants=participants,
                    is_group=self._is_group(self._row_value(row, "chat_identifier"), self._row_value(row, "chat_guid")),
                    sender_name=contact.display_name if contact else None,
                    contact=contact,
                    is_read=bool(row["is_read"]) if not is_from_me and row["is_read"] is not None else None,
                    date_read=(
                        self._positive_apple_to_datetime(row["date_read"])
                        if not is_from_me and bool(row["is_read"])
                        else None
                    ),
                    reply_to_guid=core._normalize_associated_guid(self._row_value(row, "associated_message_guid")),
                    thread_originator_guid=self._row_value(row, "thread_originator_guid"),
                    destination_caller_id=self._row_value(row, "destination_caller_id"),
                    attachments=attachments.get(int(row["message_id"]), []),
                    reactions=reactions.get(guid or "", []),
                    is_reaction=bool(associated_type and 2000 <= int(associated_type) <= 3006),
                    reaction_type=reaction_type,
                    reaction_emoji=self._row_value(row, "associated_message_emoji"),
                    reacted_to_guid=core._normalize_associated_guid(self._row_value(row, "associated_message_guid")),
                )
            )
        return messages

    def _message_text_matches(self, row: sqlite3.Row, needle: str, match: str) -> bool:
        text = row["text"] or core._extract_attributed_body_text(row["attributed_body"]) or ""
        if match == "exact":
            return text == needle
        return needle.casefold() in text.casefold()

    def _participants(self, conn: sqlite3.Connection, chat_id: int) -> list[str]:
        try:
            rows = conn.execute(
                """
                SELECT h.id
                FROM chat_handle_join chj
                JOIN handle h ON h.ROWID = chj.handle_id
                WHERE chj.chat_id = ?
                ORDER BY h.id ASC
                """,
                (chat_id,),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = conn.execute(
                """
                SELECT DISTINCT h.id
                FROM message m
                JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                JOIN handle h ON h.ROWID = m.handle_id
                WHERE cmj.chat_id = ? AND h.id IS NOT NULL
                ORDER BY h.id ASC
                """,
                (chat_id,),
            ).fetchall()
        seen = set()
        result = []
        for row in rows:
            handle = str(row["id"] or "")
            if handle and handle not in seen:
                seen.add(handle)
                result.append(handle)
        return result

    def _fetch_attachments(self, conn: sqlite3.Connection, rowids: list[int], convert: bool = False) -> dict[int, list[Attachment]]:
        if not rowids:
            return {}
        placeholders = ", ".join("?" for _ in rowids)
        try:
            rows = conn.execute(
                f"""
                SELECT
                    maj.message_id,
                    a.guid,
                    a.filename,
                    a.mime_type,
                    a.total_bytes,
                    a.transfer_name,
                    a.uti
                FROM message_attachment_join maj
                JOIN attachment a ON a.ROWID = maj.attachment_id
                WHERE maj.message_id IN ({placeholders})
                ORDER BY maj.message_id ASC, a.ROWID ASC
                """,
                rowids,
            ).fetchall()
        except sqlite3.OperationalError:
            return {}
        grouped: dict[int, list[Attachment]] = {}
        for row in rows:
            path = core._resolve_attachment_path(row["filename"])
            exists = bool(path and Path(path).expanduser().is_file())
            converted_path = None
            converted_mime = None
            if convert and exists:
                converted_path, converted_mime = self._convert_attachment(path or "", row["uti"], row["mime_type"])
            grouped.setdefault(int(row["message_id"]), []).append(
                Attachment(
                    guid=self._row_value(row, "guid"),
                    filename=self._row_value(row, "filename"),
                    path=path,
                    transfer_name=self._row_value(row, "transfer_name"),
                    uti=self._row_value(row, "uti"),
                    mime_type=self._row_value(row, "mime_type"),
                    byte_size=int(row["total_bytes"]) if row["total_bytes"] is not None else None,
                    missing=not exists,
                    converted_path=converted_path,
                    converted_mime_type=converted_mime,
                )
            )
        return grouped

    def _fetch_reactions(self, conn: sqlite3.Connection, guids: list[str]) -> dict[str, list[Reaction]]:
        if not guids:
            return {}
        placeholders = ", ".join("?" for _ in guids)
        try:
            rows = conn.execute(
                f"""
                SELECT
                    m.guid,
                    m.associated_message_guid,
                    m.associated_message_type,
                    m.associated_message_emoji,
                    m.text,
                    m.date,
                    m.is_from_me,
                    h.id AS sender
                FROM message m
                LEFT JOIN handle h ON h.ROWID = m.handle_id
                WHERE m.associated_message_guid IS NOT NULL
                  AND m.associated_message_guid != ''
                  AND (
                        m.associated_message_guid IN ({placeholders})
                        OR substr(m.associated_message_guid, instr(m.associated_message_guid, '/') + 1) IN ({placeholders})
                  )
                ORDER BY m.date ASC, m.ROWID ASC
                """,
                [*guids, *guids],
            ).fetchall()
        except sqlite3.OperationalError:
            return {}
        grouped: dict[str, list[Reaction]] = {}
        for row in rows:
            target = core._normalize_associated_guid(self._row_value(row, "associated_message_guid"))
            if not target:
                continue
            type_code = int(row["associated_message_type"] or 0)
            grouped.setdefault(target, []).append(
                Reaction(
                    guid=self._row_value(row, "guid"),
                    target_guid=target,
                    type_code=type_code,
                    type_label=core.REACTION_LABELS.get(type_code, "unknown"),
                    emoji=self._row_value(row, "associated_message_emoji"),
                    is_from_me=bool(row["is_from_me"]),
                    created_at=self._apple_to_datetime(row["date"]),
                    text=self._row_value(row, "text"),
                )
            )
        return grouped

    def _load_contacts(self) -> list[Contact]:
        if self._contacts_cache is not None:
            return self._contacts_cache
        existing = [path for path in self.contacts_db_paths if path.exists()]
        if not existing:
            self._contacts_cache = []
            return []
        result = core.LiveContactsReader(existing)._list_all_contacts_sync()
        contacts = [self._contact_from_core(item) for item in result.get("contacts") or []]
        self._contacts_cache = contacts
        return contacts

    def _contact_from_core(self, item: dict[str, Any]) -> Contact:
        record_id = str(item.get("record_id") or "")
        source = str(item.get("source_db_path") or "")
        return Contact(
            id=f"{source}:{record_id}" if source else record_id,
            display_name=str(item.get("display_name") or "Unnamed contact"),
            first_name=item.get("first_name"),
            middle_name=item.get("middle_name"),
            last_name=item.get("last_name"),
            nickname=item.get("nickname"),
            organization=item.get("organization"),
            phones=[
                PhoneNumber(
                    value=str(phone.get("value") or ""),
                    label=phone.get("label"),
                    is_primary=bool(phone.get("is_primary")),
                )
                for phone in item.get("phone_numbers") or []
                if str(phone.get("value") or "").strip()
            ],
            emails=[
                EmailAddress(
                    value=str(email.get("value") or ""),
                    label=email.get("label"),
                    is_primary=bool(email.get("is_primary")),
                )
                for email in item.get("email_addresses") or []
                if str(email.get("value") or "").strip()
            ],
            created_at=self._parse_optional_datetime(item.get("created_at")),
            modified_at=self._parse_optional_datetime(item.get("modified_at")),
            source_db_path=source or None,
        )

    def _contact_index_map(self) -> dict[str, Contact]:
        if self._contact_index is not None:
            return self._contact_index
        index: dict[str, Contact] = {}
        for contact in self._load_contacts():
            for phone in contact.phones:
                key = self._normalize_handle(phone.value)
                if key:
                    existing = index.get(key)
                    if existing is None or self._contact_specificity_key(contact) < self._contact_specificity_key(existing):
                        index[key] = contact
            for email in contact.emails:
                key = self._normalize_handle(email.value)
                if key:
                    existing = index.get(key)
                    if existing is None or self._contact_specificity_key(contact) < self._contact_specificity_key(existing):
                        index[key] = contact
        self._contact_index = index
        return index

    def _contact_specificity_key(self, contact: Contact) -> tuple[int, int, int, int, str]:
        handle_count = len(contact.phones) + len(contact.emails)
        has_structured_name = 0 if (contact.first_name or contact.last_name or contact.nickname) else 1
        display_name = contact.display_name or ""
        annotation_count = display_name.count("(") + display_name.count(")")
        return (handle_count, has_structured_name, annotation_count, len(display_name), display_name.lower())

    def _normalize_handle(self, value: str) -> str:
        raw = str(value or "").strip()
        for prefix in ("iMessage;-;", "iMessage;+;", "SMS;-;", "SMS;+;", "any;-;", "any;+;"):
            if raw.startswith(prefix):
                raw = raw[len(prefix):]
                break
        if "@" in raw:
            return raw.lower()
        return self._normalize_phone(raw, self.region)

    def _normalize_outbound_recipient(self, value: str, region: str) -> str:
        if "@" in value:
            return value.strip().lower()
        return self._normalize_phone(value, region)

    def _normalize_phone(self, value: str, region: str) -> str:
        raw = str(value or "").strip()
        if phonenumbers is not None:
            try:
                parsed = phonenumbers.parse(raw, region)
                return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
            except Exception:
                pass
        compact = "".join(ch for ch in raw if ch.isdigit())
        return f"+{compact}" if raw.startswith("+") and compact else compact

    def _send_target(
        self,
        to: str | None,
        chat_id: int | None,
        chat_identifier: str | None,
        chat_guid: str | None,
    ) -> str:
        if chat_id is not None:
            chat = self.chat(chat_id=chat_id)
            if chat is None:
                raise core.IMessageError(f"Chat not found: {chat_id}")
            return chat.guid or chat.identifier
        if chat_guid:
            return chat_guid
        if chat_identifier:
            return chat_identifier
        if to:
            return to
        raise ValueError("to, chat_id, chat_identifier, or chat_guid is required")

    def _prepare_files(self, file_paths: list[str]) -> list[str]:
        prepared = []
        for raw in file_paths:
            path = Path(str(raw or "").strip()).expanduser()
            if not path.exists():
                raise ValueError(f"attachment not found: {path}")
            if not path.is_file():
                raise ValueError(f"attachment path is not a file: {path}")
            prepared.append(str(path.resolve()))
        return prepared

    def _stage_send_files(self, file_paths: list[str]) -> list[str]:
        if not file_paths:
            return []
        staging_dir = self.home / "Library" / "Messages" / "Attachments" / "imessage_wrapper"
        staging_dir.mkdir(parents=True, exist_ok=True)
        staged = []
        for item in file_paths:
            source = Path(item)
            target_dir = staging_dir / str(uuid4())
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / source.name
            shutil.copy2(source, target)
            staged.append(str(target))
        return staged

    def _run_send_applescript(
        self,
        recipient: str,
        text: str,
        file_paths: list[str],
        service: str,
        use_chat: bool,
    ) -> None:
        script = """
on run argv
    set targetValue to item 1 of argv
    set outgoingText to item 2 of argv
    set targetServiceName to item 3 of argv
    set useChat to item 4 of argv
    tell application "Messages"
        if useChat is "1" then
            set targetRef to chat id targetValue
        else
            if targetServiceName is "sms" then
                set targetService to 1st service whose service type = SMS
            else
                set targetService to 1st service whose service type = iMessage
            end if
            try
                set targetRef to buddy targetValue of targetService
            on error
                set targetRef to participant targetValue of targetService
            end try
        end if
        repeat with indexValue from 5 to count of argv
            set attachmentPath to item indexValue of argv
            set attachmentFile to (POSIX file attachmentPath) as alias
            send attachmentFile to targetRef
        end repeat
        if outgoingText is not equal to "" then
            send outgoingText to targetRef
        end if
    end tell
    return "sent"
end run
"""
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.home)}
        try:
            subprocess.run(
                ["osascript", "-", recipient, text, service, "1" if use_chat else "0", *file_paths],
                input=script,
                text=True,
                capture_output=True,
                timeout=self.send_timeout,
                check=True,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise core.IMessageError(f"Timed out sending iMessage after {self.send_timeout}s") from exc
        except subprocess.CalledProcessError as exc:
            details = (exc.stderr or exc.stdout or "").strip() or "unknown AppleScript error"
            raise core.IMessageError(f"Messages send failed: {details}") from exc

    def _wait_for_sent_message(
        self,
        text: str,
        chat_id: int | None,
        chat_identifier: str | None,
        chat_guid: str | None,
        recipient: str | None,
        min_rowid: int,
        sent_at: datetime,
    ) -> _SendVerification | None:
        if not text.strip():
            return None
        deadline = time.monotonic() + self.verification_timeout
        start = sent_at.timestamp() - 2
        start_apple = int((datetime.fromtimestamp(start, timezone.utc) - core.APPLE_EPOCH).total_seconds() * 1_000_000_000)
        expected_text = core._normalize_message_text(text)
        best_match = None
        while time.monotonic() < deadline:
            with self._connect_messages() as conn:
                schema = self._schema(conn)
                select = self._message_select(schema)
                where = ["m.is_from_me = 1", "m.date >= ?", "m.ROWID > ?"]
                params: list[Any] = [start_apple, min_rowid]
                if chat_id is not None:
                    where.append("cmj.chat_id = ?")
                    params.append(chat_id)
                else:
                    identity_sql, identity_params = self._sent_message_identity_sql(
                        schema,
                        chat_identifier=chat_identifier,
                        chat_guid=chat_guid,
                        recipient=recipient,
                    )
                    if identity_sql:
                        where.append(identity_sql)
                        params.extend(identity_params)
                rows = conn.execute(
                    f"""
                    SELECT {select}
                    FROM message m
                    LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                    LEFT JOIN handle h ON h.ROWID = m.handle_id
                    LEFT JOIN chat c ON c.ROWID = cmj.chat_id
                    WHERE {" AND ".join(where)}
                    ORDER BY m.date DESC, m.ROWID DESC
                    LIMIT 25
                    """,
                    params,
                ).fetchall()
                messages = self._rows_to_messages(conn, list(rows), include_attachments=False)
                for row, message in zip(rows, messages):
                    if core._normalize_message_text(message.text) == expected_text:
                        verification = self._classify_send_verification(row, message, schema)
                        best_match = verification
                        if verification.delivery_status in {"pending", "sent"} and not verification.verified:
                            continue
                        return verification
            time.sleep(0.1)
        if best_match is not None:
            return self._timeout_send_verification(best_match)
        return None

    def _classify_send_verification(
        self,
        row: sqlite3.Row,
        message: Message,
        schema: _Schema,
    ) -> _SendVerification:
        status_columns = {"is_sent", "is_delivered", "is_finished", "error"}
        message_service = message.service
        message_error = self._optional_int(row["message_error"])
        if not (schema.message & status_columns):
            return _SendVerification(
                message=message,
                verified=True,
                delivery_status="recorded",
                message_service=message_service,
                message_error=message_error,
            )

        is_sent = self._optional_bool(row["is_sent"])
        is_delivered = self._optional_bool(row["is_delivered"])
        is_finished = self._optional_bool(row["is_finished"])
        date_delivered = self._optional_int(row["date_delivered"])
        if message_error not in {None, 0}:
            return _SendVerification(
                message=message,
                verified=False,
                delivery_status="failed",
                message_service=message_service,
                message_error=message_error,
                error=f"Messages reported send error {message_error}",
            )
        if is_finished is True and is_sent is False:
            return _SendVerification(
                message=message,
                verified=False,
                delivery_status="failed",
                message_service=message_service,
                message_error=message_error,
                error="Messages finished the send without marking it sent",
            )

        service = (message_service or "").casefold()
        delivered = is_delivered is True or bool(date_delivered)
        if service in {"imessage", "rcs"}:
            if delivered:
                return _SendVerification(
                    message=message,
                    verified=True,
                    delivery_status="delivered",
                    message_service=message_service,
                    message_error=message_error,
                )
            if is_sent is True:
                return _SendVerification(
                    message=message,
                    verified=False,
                    delivery_status="sent",
                    message_service=message_service,
                    message_error=message_error,
                )
            return _SendVerification(
                message=message,
                verified=False,
                delivery_status="pending",
                message_service=message_service,
                message_error=message_error,
            )

        if is_sent is True:
            return _SendVerification(
                message=message,
                verified=True,
                delivery_status="sent",
                message_service=message_service,
                message_error=message_error,
            )
        return _SendVerification(
            message=message,
            verified=False,
            delivery_status="pending",
            message_service=message_service,
            message_error=message_error,
        )

    def _timeout_send_verification(self, verification: _SendVerification) -> _SendVerification:
        if verification.verified or verification.error:
            return verification
        return _SendVerification(
            message=verification.message,
            verified=False,
            delivery_status=verification.delivery_status,
            message_service=verification.message_service,
            message_error=verification.message_error,
            error="Messages did not confirm delivery before verification timed out",
        )

    def _optional_bool(self, value: Any) -> bool | None:
        if value is None:
            return None
        return bool(value)

    def _optional_int(self, value: Any) -> int | None:
        if value is None:
            return None
        return int(value)

    def _sent_message_identity_sql(
        self,
        schema: _Schema,
        chat_identifier: str | None,
        chat_guid: str | None,
        recipient: str | None,
    ) -> tuple[str, list[Any]]:
        values: list[str] = []
        if chat_guid:
            values.append(chat_guid)
        if chat_identifier:
            values.append(chat_identifier)
        if recipient:
            values.append(recipient)
        candidates = sorted(self._handle_candidates(values))
        if not candidates:
            return "", []

        placeholders = ", ".join("?" for _ in candidates)
        clauses = []
        if chat_guid:
            clauses.append(f"c.guid IN ({placeholders})")
        if chat_identifier or recipient:
            clauses.append(f"c.chat_identifier IN ({placeholders})")
            if "last_addressed_handle" in schema.chat:
                clauses.append(f"c.last_addressed_handle IN ({placeholders})")
            clauses.append(f"h.id IN ({placeholders})")
            if "uncanonicalized_id" in schema.handle:
                clauses.append(f"h.uncanonicalized_id IN ({placeholders})")

        params: list[Any] = []
        for _ in clauses:
            params.extend(candidates)
        return f"({' OR '.join(clauses)})", params

    def _handle_candidates(self, values: list[str]) -> set[str]:
        candidates = set()
        for raw in values:
            value = str(raw or "").strip()
            if not value:
                continue
            candidates.add(value)
            normalized = self._normalize_handle(value)
            if normalized:
                candidates.add(normalized)
                for prefix in ("iMessage;-;", "iMessage;+;", "SMS;-;", "SMS;+;", "any;-;", "any;+;"):
                    candidates.add(prefix + normalized)
        return candidates

    def _raise_if_ghost_row(self, chat_identifier: str | None, chat_guid: str | None, sent_at: datetime) -> None:
        handles = [value for value in (chat_identifier, chat_guid) if value]
        if not handles:
            return
        candidates = set(handles)
        for value in handles:
            if value.startswith("any;+;"):
                candidates.add("any;-;" + value[len("any;+;"):])
                candidates.add(value[len("any;+;"):])
            if value.startswith("any;-;"):
                candidates.add("any;+;" + value[len("any;-;"):])
                candidates.add(value[len("any;-;"):])
        start_apple = int(((sent_at.timestamp() - 2) - core.APPLE_EPOCH.timestamp()) * 1_000_000_000)
        placeholders = ", ".join("?" for _ in candidates)
        with self._connect_messages() as conn:
            try:
                row = conn.execute(
                    f"""
                    SELECT m.ROWID AS rowid
                    FROM message m
                    LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                    LEFT JOIN handle h ON h.ROWID = m.handle_id
                    WHERE m.is_from_me = 1
                      AND m.date >= ?
                      AND COALESCE(m.text, '') = ''
                      AND cmj.message_id IS NULL
                      AND COALESCE(h.id, '') IN ({placeholders})
                    ORDER BY m.date DESC, m.ROWID DESC
                    LIMIT 1
                    """,
                    [start_apple, *candidates],
                ).fetchone()
            except sqlite3.OperationalError:
                row = None
        if row:
            raise core.IMessageError(
                f"Messages accepted the chat send but wrote an unjoined empty outgoing row ({row['rowid']}); delivery was not confirmed"
            )

    def _max_message_rowid(self) -> int:
        with self._connect_messages() as conn:
            row = conn.execute("SELECT MAX(ROWID) AS rowid FROM message").fetchone()
            return int(row["rowid"] or 0)

    def _open_watch_handles(self):
        if not hasattr(select, "kqueue"):
            return None, []
        try:
            kqueue = select.kqueue()
        except OSError:
            return None, []
        fds = []
        events = []
        flags = getattr(os, "O_EVTONLY", os.O_RDONLY)
        for path in (self.messages_db_path, Path(f"{self.messages_db_path}-wal"), Path(f"{self.messages_db_path}-shm")):
            if not path.exists():
                continue
            try:
                fd = os.open(path, flags)
            except OSError:
                continue
            fds.append(fd)
            events.append(
                select.kevent(
                    fd,
                    filter=select.KQ_FILTER_VNODE,
                    flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                    fflags=(
                        select.KQ_NOTE_WRITE
                        | select.KQ_NOTE_EXTEND
                        | select.KQ_NOTE_RENAME
                        | select.KQ_NOTE_DELETE
                    ),
                )
            )
        if not events:
            kqueue.close()
            for fd in fds:
                os.close(fd)
            return None, []
        try:
            kqueue.control(events, 0, 0)
        except OSError:
            kqueue.close()
            for fd in fds:
                os.close(fd)
            return None, []
        return kqueue, fds

    def _wait_for_db_change(self, kqueue, poll_interval: float) -> None:
        timeout = max(0.05, poll_interval)
        if kqueue is None:
            time.sleep(timeout)
            return
        try:
            kqueue.control([], 1, timeout)
        except OSError:
            time.sleep(timeout)

    def _convert_attachment(self, path: str, uti: str | None, mime_type: str | None) -> tuple[str | None, str | None]:
        source = Path(path)
        lower = source.name.lower()
        lower_uti = str(uti or "").lower()
        lower_mime = str(mime_type or "").lower()
        if lower.endswith(".gif") or lower_uti == "com.compuserve.gif" or lower_mime == "image/gif":
            suffix, converted_mime, args = "png", "image/png", ["-nostdin", "-y", "-i", path, "-vframes", "1"]
        elif lower.endswith(".caf") or lower_uti == "com.apple.coreaudio-format" or lower_mime == "audio/x-caf":
            suffix, converted_mime, args = "m4a", "audio/mp4", ["-nostdin", "-y", "-i", path, "-c:a", "aac", "-b:a", "128k"]
        else:
            return None, None
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return None, None
        cache_dir = self.home / "Library" / "Caches" / "imessage_wrapper" / "converted-attachments"
        cache_dir.mkdir(parents=True, exist_ok=True)
        target = cache_dir / f"{source.stem}-{abs(hash((str(source), source.stat().st_size, source.stat().st_mtime_ns)))}.{suffix}"
        if target.exists():
            return str(target), converted_mime
        completed = subprocess.run([ffmpeg, *args, str(target)], capture_output=True)
        if completed.returncode != 0 or not target.exists():
            return None, None
        return str(target), converted_mime

    def _row_value(self, row: sqlite3.Row, key: str) -> str | None:
        value = row[key]
        if value in (None, ""):
            return None
        return str(value)

    def _is_group(self, identifier: str | None, guid: str | None) -> bool:
        return bool(
            (identifier and (";+;" in identifier or core._looks_like_group_chat_identifier(identifier)))
            or (guid and (";+;" in guid or core._looks_like_group_chat_guid(guid)))
        )

    def _apple_to_datetime(self, value: Any) -> datetime | None:
        iso = core._apple_timestamp_to_iso(value)
        return self._parse_optional_datetime(iso)

    def _positive_apple_to_datetime(self, value: Any) -> datetime | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            return None
        return self._apple_to_datetime(value)

    def _parse_optional_datetime(self, value: Any) -> datetime | None:
        if not value:
            return None
        if isinstance(value, datetime):
            return value
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return None

    def _datetime_to_apple(self, value: datetime) -> int:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int((value.astimezone(timezone.utc) - core.APPLE_EPOCH).total_seconds() * 1_000_000_000)

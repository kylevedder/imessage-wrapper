from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
DEFAULT_SEND_TIMEOUT_SECONDS = 10
REACTION_LABELS = {
    2000: "love",
    2001: "like",
    2002: "dislike",
    2003: "laugh",
    2004: "emphasize",
    2005: "question",
    2006: "emoji",
    3000: "remove_love",
    3001: "remove_like",
    3002: "remove_dislike",
    3003: "remove_laugh",
    3004: "remove_emphasize",
    3005: "remove_question",
    3006: "remove_emoji",
}

log = logging.getLogger(__name__)


class IMessageError(RuntimeError):
    pass


def _host_home() -> Path:
    return Path(os.environ.get("IMESSAGE_WRAPPER_HOST_HOME", str(Path.home()))).expanduser()


def default_chat_db_path() -> Path:
    return _host_home() / "Library" / "Messages" / "chat.db"


def default_contacts_db_path() -> Path:
    return _host_home() / "Library" / "Application Support" / "AddressBook" / "AddressBook-v22.abcddb"


def default_contacts_sources_dir() -> Path:
    return _host_home() / "Library" / "Application Support" / "AddressBook" / "Sources"


def _apple_timestamp_to_iso(value: Any) -> str | None:
    if value in (None, 0, "0", ""):
        return None
    raw = int(value)
    if abs(raw) > 10**12:
        seconds = raw / 1_000_000_000
    elif abs(raw) > 10**9:
        seconds = raw / 1_000_000
    else:
        seconds = raw
    return (APPLE_EPOCH + timedelta(seconds=seconds)).isoformat()


def _normalize_associated_guid(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value
    if "/" in normalized:
        normalized = normalized.rsplit("/", 1)[-1]
    if ":" in normalized:
        prefix, suffix = normalized.split(":", 1)
        if prefix in {"p", "bp"} and suffix:
            return suffix
    return normalized


def _resolve_attachment_path(value: str | None) -> str | None:
    if not value:
        return None
    return str(Path(value).expanduser())


_ATTRIBUTED_BODY_EXCLUDED = {
    "streamtyped",
    "NSAttributedString",
    "NSMutableAttributedString",
    "NSObject",
    "NSString",
    "NSMutableString",
    "NSDictionary",
    "NSNumber",
    "NSValue",
    "__kIMMessagePartAttributeName",
}

_ATTRIBUTED_BODY_EXCLUDED_SUBSTRINGS = (
    "__kIM",
    "bplist00",
    "NSKeyedArchiver",
    "DDScannerResult",
    "$archiver",
    "$objects",
    "$top",
    "$version",
)

_GROUP_CHAT_IDENTIFIER_RE = re.compile(r"^[0-9a-f]{20,}$", re.IGNORECASE)
_FOUNDATION_ATTRIBUTED_BODY_SYMBOLS: tuple[Any, Any] | None | bool = False


def _normalize_lookup_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _compact_lookup_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    return re.sub(r"[^a-z0-9]+", "", text)


def _query_lookup_terms(value: str) -> list[str]:
    normalized = _normalize_lookup_text(value)
    if not normalized:
        return []
    terms = normalized.split()
    compact = _compact_lookup_text(value)
    if compact and compact not in terms:
        terms.append(compact)
    return terms


def _register_lookup_functions(conn: sqlite3.Connection) -> None:
    conn.create_function("imessage_lookup_normalize", 1, _normalize_lookup_text)
    conn.create_function("imessage_lookup_compact", 1, _compact_lookup_text)


def _lookup_match_score(query: str, values: list[Any]) -> int | None:
    query_normalized = _normalize_lookup_text(query)
    query_compact = _compact_lookup_text(query)
    query_tokens = [token for token in query_normalized.split() if token]
    if not query_normalized and not query_compact:
        return None

    normalized_values = [_normalize_lookup_text(value) for value in values if str(value or "").strip()]
    compact_values = [_compact_lookup_text(value) for value in values if str(value or "").strip()]
    if not normalized_values and not compact_values:
        return None

    combined_normalized = " ".join(item for item in normalized_values if item)
    combined_compact = " ".join(item for item in compact_values if item)
    combined_tokens = {token for item in normalized_values for token in item.split() if token}

    score: int | None = None
    if query_normalized and any(item == query_normalized for item in normalized_values):
        score = 1000
    elif query_compact and any(item == query_compact for item in compact_values):
        score = 980
    elif query_normalized and any(item.startswith(query_normalized) for item in normalized_values):
        score = 920
    elif query_normalized and query_normalized == combined_normalized:
        score = 900
    elif query_normalized and query_normalized in combined_normalized:
        score = 860
    elif query_compact and query_compact in combined_compact:
        score = 840

    if query_tokens:
        exact_hits = sum(1 for token in query_tokens if token in combined_tokens)
        prefix_hits = sum(1 for token in query_tokens if any(candidate.startswith(token) for candidate in combined_tokens))
        substring_hits = sum(1 for token in query_tokens if token in combined_normalized)
        token_count = len(query_tokens)
        if exact_hits == token_count:
            score = max(score or 0, 760 + token_count)
        elif prefix_hits == token_count:
            score = max(score or 0, 720 + token_count)
        elif substring_hits == token_count:
            score = max(score or 0, 680 + token_count)
        elif exact_hits:
            score = max(score or 0, 520 + exact_hits)
        elif prefix_hits:
            score = max(score or 0, 460 + prefix_hits)
        elif substring_hits:
            score = max(score or 0, 420 + substring_hits)
    return score


def _foundation_attributed_body_symbols() -> tuple[Any, Any] | None:
    global _FOUNDATION_ATTRIBUTED_BODY_SYMBOLS
    if _FOUNDATION_ATTRIBUTED_BODY_SYMBOLS is not False:
        return _FOUNDATION_ATTRIBUTED_BODY_SYMBOLS
    try:
        from Foundation import NSData, NSUnarchiver
    except ImportError as exc:
        if sys.platform == "darwin":
            raise IMessageError(
                "Decoding iMessage attributedBody requires PyObjC Cocoa on macOS. "
                "Install with `pip install pyobjc-framework-Cocoa`."
            ) from exc
        _FOUNDATION_ATTRIBUTED_BODY_SYMBOLS = None
        return None
    _FOUNDATION_ATTRIBUTED_BODY_SYMBOLS = (NSData, NSUnarchiver)
    return _FOUNDATION_ATTRIBUTED_BODY_SYMBOLS


def _decode_attributed_body_text_with_foundation(value: bytes) -> str | None:
    symbols = _foundation_attributed_body_symbols()
    if symbols is None:
        return None
    NSData, NSUnarchiver = symbols
    try:
        data = NSData.dataWithBytes_length_(value, len(value))
        decoded = NSUnarchiver.unarchiveObjectWithData_(data)
    except Exception:
        log.debug("Foundation failed to decode iMessage attributedBody", exc_info=True)
        return None
    if decoded is None:
        return None

    attr_string = getattr(decoded, "string", None)
    if attr_string is not None:
        text = attr_string() if callable(attr_string) else attr_string
    elif isinstance(decoded, str):
        text = decoded
    else:
        return None
    return unicodedata.normalize("NFC", str(text))


def _extract_attributed_body_text_heuristic(value: bytes | None) -> str:
    if not value:
        return ""
    decoded = unicodedata.normalize("NFC", value.decode("utf-8", "ignore"))
    candidates = re.findall(r"[^\x00-\x08\x0b-\x1f\x7f]{2,}", decoded)
    for item in candidates:
        text = item.strip()
        if not text or text in _ATTRIBUTED_BODY_EXCLUDED:
            continue
        for excluded in _ATTRIBUTED_BODY_EXCLUDED:
            marker = text.find(excluded)
            if marker > 0:
                text = text[:marker].strip()
        for excluded in _ATTRIBUTED_BODY_EXCLUDED_SUBSTRINGS:
            marker = text.find(excluded)
            if marker > 0:
                text = text[:marker].strip()
        text = text.lstrip("!\"#$%&'*+,-./:;<=>?@[\\]^_`{|}~")
        text = text.strip()
        if not text or text in _ATTRIBUTED_BODY_EXCLUDED:
            continue
        if any(excluded in text for excluded in _ATTRIBUTED_BODY_EXCLUDED_SUBSTRINGS):
            continue
        if not any(unicodedata.category(ch)[0] in {"L", "N", "S"} for ch in text):
            continue
        return text
    return ""


def _extract_attributed_body_text(value: bytes | None) -> str:
    if not value:
        return ""
    decoded = _decode_attributed_body_text_with_foundation(value)
    if decoded is not None:
        return decoded
    return _extract_attributed_body_text_heuristic(value)


def _display_contact_name(row: sqlite3.Row | dict[str, Any]) -> str:
    def value(key: str) -> Any:
        if isinstance(row, sqlite3.Row):
            return row[key]
        return row.get(key)

    preferred = str(value("ZNAME") or "").strip()
    if preferred:
        return preferred
    parts = [
        str(value("ZFIRSTNAME") or "").strip(),
        str(value("ZMIDDLENAME") or "").strip(),
        str(value("ZLASTNAME") or "").strip(),
    ]
    full_name = " ".join(part for part in parts if part).strip()
    if full_name:
        return full_name
    nickname = str(value("ZNICKNAME") or "").strip()
    if nickname:
        return nickname
    organization = str(value("ZORGANIZATION") or "").strip()
    if organization:
        return organization
    return "Unnamed contact"


def _base_message_query(where_clause: str) -> str:
    return f"""
        SELECT
            m.ROWID AS rowid,
            m.guid,
            m.text,
            m.subject,
            m.attributedBody,
            m.service,
            m.date,
            m.date_read,
            m.date_delivered,
            m.is_from_me,
            m.is_read,
            m.associated_message_guid,
            m.associated_message_type,
            m.associated_message_emoji,
            h.id AS handle_id,
            h.uncanonicalized_id AS uncanonicalized_handle,
            h.service AS handle_service,
            c.guid AS chat_guid,
            c.chat_identifier,
            c.display_name
        FROM message m
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        LEFT JOIN chat c ON c.ROWID = cmj.chat_id
        WHERE {where_clause}
    """


class IMessageReader:
    async def list_users(self, limit: int = 25) -> dict[str, Any]:
        raise NotImplementedError

    async def search_contacts(self, query: str, limit: int = 25) -> dict[str, Any]:
        raise NotImplementedError

    async def get_conversation(self, user_id: str, limit: int = 50) -> dict[str, Any]:
        raise NotImplementedError


class ContactsReader:
    async def list_contacts(self, limit: int = 5000, offset: int = 0) -> dict[str, Any]:
        raise NotImplementedError

    async def list_all_contacts(self) -> dict[str, Any]:
        contacts: list[dict[str, Any]] = []
        page_size = 5000
        offset = 0
        while True:
            result = await self.list_contacts(limit=page_size, offset=offset)
            batch = list(result.get("contacts") or [])
            contacts.extend(batch)
            if len(batch) < page_size:
                merged = dict(result)
                merged["contacts"] = contacts
                return merged
            offset += len(batch)

    async def search_contacts(self, query: str, limit: int = 25) -> dict[str, Any]:
        raise NotImplementedError


@dataclass
class LiveIMessageReader(IMessageReader):
    db_path: Path

    async def list_users(self, limit: int = 25) -> dict[str, Any]:
        return await asyncio.to_thread(self._list_users_sync, limit)

    async def search_contacts(self, query: str, limit: int = 25) -> dict[str, Any]:
        return await asyncio.to_thread(self._search_contacts_sync, query, limit)

    async def get_conversation(self, user_id: str, limit: int = 50) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_conversation_sync, user_id, limit)

    def _connect(self) -> sqlite3.Connection:
        if not self.db_path.exists():
            raise IMessageError(f"Messages database not found at {self.db_path}")
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        _register_lookup_functions(conn)
        return conn

    def _list_users_sync(self, limit: int) -> dict[str, Any]:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT
                    COALESCE(NULLIF(h.id, ''), NULLIF(c.chat_identifier, ''), c.guid) AS user_id,
                    COALESCE(NULLIF(c.display_name, ''), NULLIF(h.id, ''), NULLIF(c.chat_identifier, ''), c.guid) AS display_name,
                    COALESCE(NULLIF(h.id, ''), NULLIF(c.chat_identifier, ''), c.guid) AS handle_id,
                    COALESCE(NULLIF(h.uncanonicalized_id, ''), NULLIF(h.id, '')) AS uncanonicalized_handle,
                    COALESCE(NULLIF(h.service, ''), NULLIF(c.service_name, ''), NULLIF(m.service, '')) AS resolved_service,
                    MAX(m.date) AS last_message_date,
                    COUNT(DISTINCT m.ROWID) AS message_count
                FROM message m
                LEFT JOIN handle h ON h.ROWID = m.handle_id
                LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                LEFT JOIN chat c ON c.ROWID = cmj.chat_id
                WHERE COALESCE(NULLIF(h.id, ''), NULLIF(c.chat_identifier, ''), c.guid) IS NOT NULL
                GROUP BY user_id, display_name, handle_id, uncanonicalized_handle, resolved_service
                ORDER BY MAX(m.date) DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        finally:
            conn.close()

        users = [
            {
                "user_id": row["user_id"],
                "display_name": row["display_name"],
                "handle_id": row["handle_id"],
                "uncanonicalized_handle": row["uncanonicalized_handle"],
                "service": row["resolved_service"] or "iMessage",
                "message_count": row["message_count"],
                "last_message_at": _apple_timestamp_to_iso(row["last_message_date"]),
            }
            for row in rows
        ]
        return {
            "mode": "live",
            "db_path": str(self.db_path),
            "users": users,
        }

    def _search_contacts_sync(self, query: str, limit: int) -> dict[str, Any]:
        query = query.strip()
        if not query:
            raise ValueError("query is required")
        if limit < 1:
            raise ValueError("limit must be >= 1")
        search_terms = _query_lookup_terms(query)
        if not search_terms:
            raise ValueError("query is required")
        conn = self._connect()
        try:
            clauses = []
            params: list[Any] = []
            for term in search_terms:
                like = f"%{term}%"
                compact_like = f"%{_compact_lookup_text(term)}%"
                clauses.append(
                    """
                    (
                        lower(COALESCE(c.display_name, '')) LIKE ?
                        OR lower(COALESCE(h.id, '')) LIKE ?
                        OR lower(COALESCE(h.uncanonicalized_id, '')) LIKE ?
                        OR lower(COALESCE(c.chat_identifier, '')) LIKE ?
                        OR lower(COALESCE(c.guid, '')) LIKE ?
                        OR imessage_lookup_normalize(c.display_name) LIKE ?
                        OR imessage_lookup_compact(c.display_name) LIKE ?
                        OR imessage_lookup_normalize(h.id) LIKE ?
                        OR imessage_lookup_compact(h.id) LIKE ?
                        OR imessage_lookup_normalize(h.uncanonicalized_id) LIKE ?
                        OR imessage_lookup_compact(h.uncanonicalized_id) LIKE ?
                        OR imessage_lookup_normalize(c.chat_identifier) LIKE ?
                        OR imessage_lookup_compact(c.chat_identifier) LIKE ?
                        OR imessage_lookup_normalize(c.guid) LIKE ?
                        OR imessage_lookup_compact(c.guid) LIKE ?
                        OR replace(replace(replace(replace(replace(lower(COALESCE(h.id, '')), '+', ''), ' ', ''), '-', ''), '(', ''), ')', '') LIKE ?
                        OR replace(replace(replace(replace(replace(lower(COALESCE(h.uncanonicalized_id, '')), '+', ''), ' ', ''), '-', ''), '(', ''), ')', '') LIKE ?
                        OR replace(replace(replace(replace(replace(lower(COALESCE(c.chat_identifier, '')), '+', ''), ' ', ''), '-', ''), '(', ''), ')', '') LIKE ?
                    )
                    """
                )
                params.extend((
                    like,
                    like,
                    like,
                    like,
                    like,
                    like,
                    compact_like,
                    like,
                    compact_like,
                    like,
                    compact_like,
                    like,
                    compact_like,
                    like,
                    compact_like,
                    compact_like,
                    compact_like,
                    compact_like,
                ))
            candidate_limit = max(limit * 10, 50)
            rows = conn.execute(
                """
                SELECT
                    COALESCE(NULLIF(h.id, ''), NULLIF(c.chat_identifier, ''), c.guid) AS user_id,
                    COALESCE(NULLIF(c.display_name, ''), NULLIF(h.id, ''), NULLIF(c.chat_identifier, ''), c.guid) AS display_name,
                    COALESCE(NULLIF(h.id, ''), NULLIF(c.chat_identifier, ''), c.guid) AS handle_id,
                    COALESCE(NULLIF(h.uncanonicalized_id, ''), NULLIF(h.id, '')) AS uncanonicalized_handle,
                    COALESCE(NULLIF(h.service, ''), NULLIF(c.service_name, ''), NULLIF(m.service, '')) AS resolved_service,
                    MAX(m.date) AS last_message_date,
                    COUNT(DISTINCT m.ROWID) AS message_count
                FROM message m
                LEFT JOIN handle h ON h.ROWID = m.handle_id
                LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                LEFT JOIN chat c ON c.ROWID = cmj.chat_id
                WHERE COALESCE(NULLIF(h.id, ''), NULLIF(c.chat_identifier, ''), c.guid) IS NOT NULL
                  AND (
                """
                + " OR ".join(clauses)
                + """
                  )
                GROUP BY user_id, display_name, handle_id, uncanonicalized_handle, resolved_service
                ORDER BY MAX(m.date) DESC
                LIMIT ?
                """,
                (*params, candidate_limit),
            ).fetchall()
        finally:
            conn.close()
        scored_contacts = []
        for row in rows:
            contact = {
                "user_id": row["user_id"],
                "display_name": row["display_name"],
                "handle_id": row["handle_id"],
                "uncanonicalized_handle": row["uncanonicalized_handle"],
                "service": row["resolved_service"] or "iMessage",
                "message_count": row["message_count"],
                "last_message_at": _apple_timestamp_to_iso(row["last_message_date"]),
            }
            score = _lookup_match_score(
                query,
                [
                    contact["display_name"],
                    contact["user_id"],
                    contact["handle_id"],
                    contact["uncanonicalized_handle"],
                ],
            )
            if score is None:
                continue
            scored_contacts.append((score, int(row["last_message_date"] or 0), contact))
        scored_contacts.sort(
            key=lambda item: (
                -item[0],
                -item[1],
                _normalize_lookup_text(item[2].get("display_name") or item[2]["user_id"]),
            )
        )
        contacts = [item[2] for item in scored_contacts[:limit]]
        return {
            "mode": "live",
            "db_path": str(self.db_path),
            "query": query,
            "contacts": contacts,
        }

    def _get_conversation_sync(self, user_id: str, limit: int) -> dict[str, Any]:
        if not user_id:
            raise ValueError("user_id is required")
        if limit < 1:
            raise ValueError("limit must be >= 1")
        conn = self._connect()
        try:
            args = (user_id, user_id, user_id, user_id, limit)
            rows = conn.execute(
                _base_message_query(
                    """
                    (
                        h.id = ?
                        OR h.uncanonicalized_id = ?
                        OR c.chat_identifier = ?
                        OR c.guid = ?
                    )
                    AND (m.associated_message_guid IS NULL OR m.associated_message_guid = '')
                    """
                )
                + """
                ORDER BY m.date DESC, m.ROWID DESC
                LIMIT ?
                """,
                args,
            ).fetchall()

            if not rows:
                raise IMessageError(f"No iMessage conversation found for {user_id}")

            ordered_rows = list(reversed(rows))
            row_ids = [int(row["rowid"]) for row in ordered_rows]
            message_guids = [str(row["guid"]) for row in ordered_rows if row["guid"]]

            attachments_by_message = self._fetch_attachments(conn, row_ids)
            reactions_by_guid = self._fetch_reactions(conn, message_guids)

            messages: list[dict[str, Any]] = []
            for row in ordered_rows:
                guid = str(row["guid"])
                attachments = attachments_by_message.get(int(row["rowid"]), [])
                reactions = reactions_by_guid.get(guid, [])
                images = [item for item in attachments if (item.get("mime_type") or "").startswith("image/")]
                text = row["text"] or row["subject"] or _extract_attributed_body_text(row["attributedBody"]) or ""
                messages.append(
                    {
                        "guid": guid,
                        "text": text,
                        "timestamp": _apple_timestamp_to_iso(row["date"]),
                        "date_read": _apple_timestamp_to_iso(row["date_read"]),
                        "date_delivered": _apple_timestamp_to_iso(row["date_delivered"]),
                        "is_from_me": bool(row["is_from_me"]),
                        "is_read": bool(row["is_read"]),
                        "handle_id": row["handle_id"] or row["chat_identifier"],
                        "display_name": row["display_name"] or row["handle_id"] or row["chat_identifier"],
                        "service": row["handle_service"] or row["service"] or "iMessage",
                        "chat_guid": row["chat_guid"],
                        "chat_identifier": row["chat_identifier"],
                        "attachments": attachments,
                        "image_attachments": images,
                        "reactions": reactions,
                    }
                )

            latest = ordered_rows[-1]
            conversation = {
                "user_id": user_id,
                "display_name": latest["display_name"] or latest["handle_id"] or latest["chat_identifier"] or user_id,
                "handle_id": latest["handle_id"] or latest["chat_identifier"] or user_id,
                "service": latest["handle_service"] or latest["service"] or "iMessage",
                "chat_guid": latest["chat_guid"],
                "chat_identifier": latest["chat_identifier"],
                "message_count": len(messages),
                "messages": messages,
            }
            return {
                "mode": "live",
                "db_path": str(self.db_path),
                "conversation": conversation,
            }
        finally:
            conn.close()

    def _fetch_attachments(self, conn: sqlite3.Connection, row_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
        if not row_ids:
            return {}
        placeholders = ", ".join("?" for _ in row_ids)
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
            ORDER BY maj.message_id ASC, a.created_date ASC, a.ROWID ASC
            """,
            tuple(row_ids),
        ).fetchall()
        grouped: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(int(row["message_id"]), []).append(
                {
                    "guid": row["guid"],
                    "filename": _resolve_attachment_path(row["filename"]),
                    "mime_type": row["mime_type"],
                    "total_bytes": row["total_bytes"],
                    "transfer_name": row["transfer_name"],
                    "uti": row["uti"],
                }
            )
        return grouped

    def _fetch_reactions(self, conn: sqlite3.Connection, message_guids: list[str]) -> dict[str, list[dict[str, Any]]]:
        if not message_guids:
            return {}
        placeholders = ", ".join("?" for _ in message_guids)
        rows = conn.execute(
            _base_message_query(f"m.associated_message_guid IS NOT NULL AND m.associated_message_guid != ''")
            + f"""
              AND (
                    m.associated_message_guid IN ({placeholders})
                    OR substr(m.associated_message_guid, instr(m.associated_message_guid, '/') + 1) IN ({placeholders})
                 )
            ORDER BY m.date ASC, m.ROWID ASC
            """,
            tuple(message_guids + message_guids),
        ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            target_guid = _normalize_associated_guid(row["associated_message_guid"])
            if not target_guid:
                continue
            reaction_type = int(row["associated_message_type"] or 0)
            grouped.setdefault(target_guid, []).append(
                {
                    "guid": row["guid"],
                    "target_guid": target_guid,
                    "timestamp": _apple_timestamp_to_iso(row["date"]),
                    "is_from_me": bool(row["is_from_me"]),
                    "handle_id": row["handle_id"] or row["chat_identifier"],
                    "type_code": reaction_type,
                    "type_label": REACTION_LABELS.get(reaction_type, "unknown"),
                    "emoji": row["associated_message_emoji"],
                    "text": row["text"] or "",
                }
            )
        return grouped


@dataclass
class StubIMessageReader(IMessageReader):
    payload: dict[str, Any]

    @classmethod
    def from_path(cls, path: Path) -> "StubIMessageReader":
        return cls(json.loads(path.read_text()))

    async def list_users(self, limit: int = 25) -> dict[str, Any]:
        users = list(self.payload.get("users") or [])[:limit]
        return {"mode": "stub", "users": users, "stub_path": self.payload.get("_stub_path")}

    async def search_contacts(self, query: str, limit: int = 25) -> dict[str, Any]:
        query = query.strip()
        if not query:
            raise ValueError("query is required")
        contacts: list[tuple[int, dict[str, Any]]] = []
        for item in self.payload.get("users") or []:
            score = _lookup_match_score(
                query,
                [
                    item.get("display_name"),
                    item.get("user_id"),
                    item.get("handle_id"),
                    item.get("uncanonicalized_handle"),
                ],
            )
            if score is not None:
                contacts.append((score, item))
        contacts.sort(
            key=lambda item: (
                -item[0],
                _normalize_lookup_text(item[1].get("display_name") or item[1].get("user_id")),
            )
        )
        return {
            "mode": "stub",
            "query": query,
            "contacts": [item[1] for item in contacts[:limit]],
            "stub_path": self.payload.get("_stub_path"),
        }

    async def get_conversation(self, user_id: str, limit: int = 50) -> dict[str, Any]:
        conversations = self.payload.get("conversations") or {}
        conversation = conversations.get(user_id)
        if conversation is None:
            raise IMessageError(f"No stub conversation found for {user_id}")
        result = dict(conversation)
        result["messages"] = list(result.get("messages") or [])[:limit]
        if "user_id" not in result:
            result["user_id"] = user_id
        return {"mode": "stub", "conversation": result, "stub_path": self.payload.get("_stub_path")}


@dataclass
class LiveContactsReader(ContactsReader):
    db_paths: list[Path]
    _all_contacts_cache: dict[str, Any] | None = field(default=None, init=False, repr=False)

    def _contacts_cache_key(self) -> tuple[tuple[str, int, int], ...]:
        return tuple(
            (
                str(db_path),
                db_path.stat().st_mtime_ns,
                db_path.stat().st_size,
            )
            for db_path in self.db_paths
        )

    async def list_contacts(self, limit: int = 5000, offset: int = 0) -> dict[str, Any]:
        return await asyncio.to_thread(self._list_contacts_sync, limit, offset)

    async def list_all_contacts(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._list_all_contacts_sync)

    async def search_contacts(self, query: str, limit: int = 25) -> dict[str, Any]:
        return await asyncio.to_thread(self._search_contacts_sync, query, limit)

    def _connect(self, db_path: Path) -> sqlite3.Connection:
        if not db_path.exists():
            raise IMessageError(f"Contacts database not found at {db_path}")
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        _register_lookup_functions(conn)
        return conn

    def _record_columns_sql(self, conn: sqlite3.Connection) -> str:
        columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(ZABCDRECORD)").fetchall()}
        created = "r.ZCREATIONDATE" if "ZCREATIONDATE" in columns else "NULL"
        modified = "r.ZMODIFICATIONDATE" if "ZMODIFICATIONDATE" in columns else "NULL"
        return f"""
                    r.Z_PK AS record_id,
                    r.ZFIRSTNAME,
                    r.ZMIDDLENAME,
                    r.ZLASTNAME,
                    r.ZNICKNAME,
                    r.ZORGANIZATION,
                    r.ZNAME,
                    r.ZSORTINGFIRSTNAME,
                    r.ZSORTINGLASTNAME,
                    {created} AS ZCREATIONDATE,
                    {modified} AS ZMODIFICATIONDATE
        """

    def _record_entity_where_sql(self, conn: sqlite3.Connection) -> str:
        columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(ZABCDRECORD)").fetchall()}
        return "WHERE r.Z_ENT = 22" if "Z_ENT" in columns else ""

    def _record_to_contact(self, conn: sqlite3.Connection, row: sqlite3.Row, db_path: Path) -> dict[str, Any]:
        record_id = int(row["record_id"])
        phones = [
            dict(phone)
            for phone in conn.execute(
                """
                SELECT ZFULLNUMBER AS value, ZLABEL AS label, ZISPRIMARY AS is_primary
                FROM ZABCDPHONENUMBER
                WHERE ZOWNER = ?
                ORDER BY ZISPRIMARY DESC, ZORDERINGINDEX ASC, Z_PK ASC
                """,
                (record_id,),
            ).fetchall()
        ]
        emails = [
            dict(email)
            for email in conn.execute(
                """
                SELECT ZADDRESS AS value, ZLABEL AS label, ZISPRIMARY AS is_primary
                FROM ZABCDEMAILADDRESS
                WHERE ZOWNER = ?
                ORDER BY ZISPRIMARY DESC, ZORDERINGINDEX ASC, Z_PK ASC
                """,
                (record_id,),
            ).fetchall()
        ]
        return {
            "record_id": record_id,
            "display_name": _display_contact_name(row),
            "first_name": row["ZFIRSTNAME"],
            "middle_name": row["ZMIDDLENAME"],
            "last_name": row["ZLASTNAME"],
            "nickname": row["ZNICKNAME"],
            "organization": row["ZORGANIZATION"],
            "phone_numbers": phones,
            "email_addresses": emails,
            "created_at": _apple_timestamp_to_iso(row["ZCREATIONDATE"]),
            "modified_at": _apple_timestamp_to_iso(row["ZMODIFICATIONDATE"]),
            "source_db_path": str(db_path),
        }

    def _list_contacts_sync(self, limit: int, offset: int) -> dict[str, Any]:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if offset < 0:
            raise ValueError("offset must be >= 0")
        if len(self.db_paths) == 1:
            return self._list_contacts_single_db_sync(self.db_paths[0], limit, offset)
        result = self._list_all_contacts_sync()
        result["contacts"] = list(result.get("contacts") or [])[offset:offset + limit]
        return result

    def _list_contacts_single_db_sync(self, db_path: Path, limit: int, offset: int) -> dict[str, Any]:
        conn = self._connect(db_path)
        try:
            rows = conn.execute(
                f"""
                SELECT
                    {self._record_columns_sql(conn)}
                FROM ZABCDRECORD r
                {self._record_entity_where_sql(conn)}
                ORDER BY
                    lower(COALESCE(r.ZSORTINGLASTNAME, r.ZLASTNAME, '')),
                    lower(COALESCE(r.ZSORTINGFIRSTNAME, r.ZFIRSTNAME, ''))
                LIMIT ?
                OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
            contacts = [self._record_to_contact(conn, row, db_path) for row in rows]
        finally:
            conn.close()
        return {
            "mode": "live",
            "db_paths": [str(db_path)],
            "contacts": contacts,
        }

    def _list_all_contacts_sync(self) -> dict[str, Any]:
        cache_key = self._contacts_cache_key()
        if self._all_contacts_cache is not None and self._all_contacts_cache.get("cache_key") == cache_key:
            return {
                "mode": "live",
                "db_paths": list(self._all_contacts_cache["db_paths"]),
                "contacts": list(self._all_contacts_cache["contacts"]),
            }

        searched_paths: list[str] = []
        contacts: list[dict[str, Any]] = []

        def primary_value(items: list[dict[str, Any]] | None) -> str | None:
            values = list(items or [])
            if not values:
                return None
            primary = next((item for item in values if item.get("is_primary")), None)
            selected = primary or values[0]
            value = str(selected.get("value") or "").strip()
            return value or None

        def dedupe_key(contact: dict[str, Any]) -> tuple[str, str, str, str | None, str]:
            phone = "".join(ch for ch in str(primary_value(contact.get("phone_numbers")) or "") if ch.isdigit())
            email = str(primary_value(contact.get("email_addresses")) or "").strip().lower()
            display_name = _normalize_lookup_text(
                contact.get("display_name") or contact.get("first_name") or contact.get("organization")
            )
            fallback = ""
            if not phone and not email:
                fallback = f"{contact.get('source_db_path') or ''}:{contact.get('record_id') or ''}"
            return (display_name, phone, email, contact.get("organization"), fallback)

        for db_path in self.db_paths:
            conn = self._connect(db_path)
            try:
                searched_paths.append(str(db_path))
                rows = conn.execute(
                    f"""
                    SELECT
                        {self._record_columns_sql(conn)}
                    FROM ZABCDRECORD r
                    {self._record_entity_where_sql(conn)}
                    ORDER BY
                        lower(COALESCE(r.ZSORTINGLASTNAME, r.ZLASTNAME, '')),
                        lower(COALESCE(r.ZSORTINGFIRSTNAME, r.ZFIRSTNAME, ''))
                    """,
                ).fetchall()
                contacts.extend(self._record_to_contact(conn, row, db_path) for row in rows)
            finally:
                conn.close()

        contacts.sort(
            key=lambda contact: (
                _normalize_lookup_text(contact.get("last_name")),
                _normalize_lookup_text(contact.get("first_name")),
                _normalize_lookup_text(contact.get("display_name") or contact.get("organization")),
                str(contact.get("source_db_path") or ""),
                int(contact.get("record_id") or 0),
            )
        )
        deduped_contacts: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, str, str, str | None, str]] = set()
        for contact in contacts:
            key = dedupe_key(contact)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            deduped_contacts.append(contact)
        self._all_contacts_cache = {
            "cache_key": cache_key,
            "db_paths": list(searched_paths),
            "contacts": list(deduped_contacts),
        }
        return {
            "mode": "live",
            "db_paths": searched_paths,
            "contacts": deduped_contacts,
        }

    def _search_contacts_sync(self, query: str, limit: int) -> dict[str, Any]:
        query = query.strip()
        if not query:
            raise ValueError("query is required")
        if limit < 1:
            raise ValueError("limit must be >= 1")
        normalized_query = _normalize_lookup_text(query)
        search_terms = _query_lookup_terms(query)
        if not search_terms:
            raise ValueError("query is required")
        contacts = []
        seen_keys: set[tuple[str | None, str | None, str | None]] = set()
        searched_paths: list[str] = []
        for db_path in self.db_paths:
            conn = self._connect(db_path)
            try:
                searched_paths.append(str(db_path))
                clauses = []
                params: list[Any] = []
                for term in search_terms:
                    like = f"%{term}%"
                    compact_like = f"%{_compact_lookup_text(term)}%"
                    clauses.append(
                        """
                        (
                            lower(COALESCE(r.ZNAME, '')) LIKE ?
                            OR lower(COALESCE(r.ZFIRSTNAME, '')) LIKE ?
                            OR lower(COALESCE(r.ZLASTNAME, '')) LIKE ?
                            OR lower(COALESCE(r.ZMIDDLENAME, '')) LIKE ?
                            OR lower(COALESCE(r.ZNICKNAME, '')) LIKE ?
                            OR lower(COALESCE(r.ZORGANIZATION, '')) LIKE ?
                            OR imessage_lookup_normalize(r.ZNAME) LIKE ?
                            OR imessage_lookup_compact(r.ZNAME) LIKE ?
                            OR imessage_lookup_normalize(r.ZFIRSTNAME) LIKE ?
                            OR imessage_lookup_compact(r.ZFIRSTNAME) LIKE ?
                            OR imessage_lookup_normalize(r.ZLASTNAME) LIKE ?
                            OR imessage_lookup_compact(r.ZLASTNAME) LIKE ?
                            OR imessage_lookup_normalize(r.ZMIDDLENAME) LIKE ?
                            OR imessage_lookup_compact(r.ZMIDDLENAME) LIKE ?
                            OR imessage_lookup_normalize(r.ZNICKNAME) LIKE ?
                            OR imessage_lookup_compact(r.ZNICKNAME) LIKE ?
                            OR imessage_lookup_normalize(r.ZORGANIZATION) LIKE ?
                            OR imessage_lookup_compact(r.ZORGANIZATION) LIKE ?
                            OR EXISTS (
                                SELECT 1
                                FROM ZABCDPHONENUMBER p
                                WHERE p.ZOWNER = r.Z_PK
                                  AND lower(COALESCE(p.ZFULLNUMBER, '')) LIKE ?
                            )
                            OR EXISTS (
                                SELECT 1
                                FROM ZABCDPHONENUMBER p
                                WHERE p.ZOWNER = r.Z_PK
                                  AND replace(replace(replace(replace(replace(lower(COALESCE(p.ZFULLNUMBER, '')), '+', ''), ' ', ''), '-', ''), '(', ''), ')', '') LIKE ?
                            )
                            OR EXISTS (
                                SELECT 1
                                FROM ZABCDEMAILADDRESS e
                                WHERE e.ZOWNER = r.Z_PK
                                AND lower(COALESCE(e.ZADDRESS, '')) LIKE ?
                            )
                        )
                        """
                    )
                    params.extend((
                        like,
                        like,
                        like,
                        like,
                        like,
                        like,
                        like,
                        compact_like,
                        like,
                        compact_like,
                        like,
                        compact_like,
                        like,
                        compact_like,
                        like,
                        compact_like,
                        like,
                        compact_like,
                        like,
                        compact_like,
                        like,
                    ))
                candidate_limit = max(limit * 10, 50)
                entity_where = self._record_entity_where_sql(conn)
                search_where = "WHERE " + (entity_where.removeprefix("WHERE ") + " AND (" if entity_where else "(")
                rows = conn.execute(
                    f"""
                    SELECT
                        {self._record_columns_sql(conn)}
                    FROM ZABCDRECORD r
                    {search_where}"""
                    + " OR ".join(clauses)
                    + """)
                    ORDER BY
                        lower(COALESCE(r.ZSORTINGLASTNAME, r.ZLASTNAME, '')),
                        lower(COALESCE(r.ZSORTINGFIRSTNAME, r.ZFIRSTNAME, ''))
                    LIMIT ?
                    """,
                    (*params, candidate_limit),
                ).fetchall()
                for row in rows:
                    contact = self._record_to_contact(conn, row, db_path)
                    phones = contact["phone_numbers"]
                    emails = contact["email_addresses"]
                    dedupe_key = (
                        row["ZNAME"],
                        phones[0]["value"] if phones else None,
                        emails[0]["value"] if emails else None,
                    )
                    if dedupe_key in seen_keys:
                        continue
                    score = _lookup_match_score(
                        query,
                        [
                            contact["display_name"],
                            contact["first_name"],
                            contact["middle_name"],
                            contact["last_name"],
                            contact["nickname"],
                            contact["organization"],
                            *(item.get("value") for item in phones),
                            *(item.get("value") for item in emails),
                        ],
                    )
                    if score is None:
                        continue
                    seen_keys.add(dedupe_key)
                    contacts.append((score, contact))
            finally:
                conn.close()
        contacts.sort(
            key=lambda item: (
                -item[0],
                0 if _normalize_lookup_text(item[1].get("display_name")) == normalized_query else 1,
                0 if _normalize_lookup_text(item[1].get("first_name")) == normalized_query else 1,
                _normalize_lookup_text(item[1].get("last_name")),
                _normalize_lookup_text(item[1].get("first_name")),
            )
        )
        contacts = [item[1] for item in contacts[:limit]]
        return {
            "mode": "live",
            "db_paths": searched_paths,
            "query": query,
            "contacts": contacts,
        }


@dataclass
class StubContactsReader(ContactsReader):
    payload: dict[str, Any]

    async def list_contacts(self, limit: int = 5000, offset: int = 0) -> dict[str, Any]:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if offset < 0:
            raise ValueError("offset must be >= 0")
        contacts = list(self.payload.get("contacts") or [])
        return {
            "mode": "stub",
            "contacts": contacts[offset:offset + limit],
            "stub_path": self.payload.get("_stub_path"),
        }

    async def list_all_contacts(self) -> dict[str, Any]:
        return {
            "mode": "stub",
            "contacts": list(self.payload.get("contacts") or []),
            "stub_path": self.payload.get("_stub_path"),
        }

    async def search_contacts(self, query: str, limit: int = 25) -> dict[str, Any]:
        query = query.strip()
        if not query:
            raise ValueError("query is required")
        normalized_query = _normalize_lookup_text(query)
        contacts = []
        for item in self.payload.get("contacts") or []:
            haystacks = [
                item.get("display_name"),
                item.get("first_name"),
                item.get("last_name"),
                item.get("nickname"),
                item.get("organization"),
            ]
            haystacks.extend(phone.get("value") for phone in item.get("phone_numbers") or [])
            haystacks.extend(email.get("value") for email in item.get("email_addresses") or [])
            score = _lookup_match_score(query, haystacks)
            if score is not None:
                contacts.append((score, item))
        contacts.sort(
            key=lambda item: (
                -item[0],
                0 if _normalize_lookup_text(item[1].get("display_name")) == normalized_query else 1,
                _normalize_lookup_text(item[1].get("display_name")),
            )
        )
        return {
            "mode": "stub",
            "query": query,
            "contacts": [item[1] for item in contacts[:limit]],
            "stub_path": self.payload.get("_stub_path"),
        }


class IMessageSender:
    async def send_message(
        self,
        recipient: str,
        message: str,
        image_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError


@dataclass
class DryRunIMessageSender(IMessageSender):
    reason: str = "IMESSAGE_WRAPPER_SEND_MODE is not set to live"

    async def send_message(
        self,
        recipient: str,
        message: str,
        image_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        if not recipient:
            raise ValueError("recipient is required")
        prepared_images = _prepare_image_paths(image_paths or [])
        if not message.strip() and not prepared_images:
            raise ValueError("message or image_paths is required")
        return {
            "mode": "dry-run",
            "recipient": recipient,
            "message": message,
            "image_paths": prepared_images,
            "sent": False,
            "reason": self.reason,
        }


@dataclass
class AppleScriptIMessageSender(IMessageSender):
    timeout_seconds: int = DEFAULT_SEND_TIMEOUT_SECONDS
    db_path: Path | None = None
    verification_timeout_seconds: float = 3.0
    verification_poll_interval_seconds: float = 0.25
    duplicate_window_seconds: int = 300

    async def send_message(
        self,
        recipient: str,
        message: str,
        image_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        if not recipient:
            raise ValueError("recipient is required")
        if not message.strip() and not (image_paths or []):
            raise ValueError("message or image_paths is required")
        return await asyncio.to_thread(self._send_sync, recipient, message, image_paths or [])

    def _send_sync(self, recipient: str, message: str, image_paths: list[str] | None = None) -> dict[str, Any]:
        prepared_images = _prepare_image_paths(image_paths or [])
        if not recipient:
            raise ValueError("recipient is required")
        if not message.strip() and not prepared_images:
            raise ValueError("message or image_paths is required")
        direct_script = """
on run argv
    set targetHandle to item 1 of argv
    set outgoingText to item 2 of argv
    tell application "Messages"
        set targetService to 1st service whose service type = iMessage
        try
            set targetBuddy to buddy targetHandle of targetService
            my sendPayload(targetBuddy, outgoingText, argv)
        on error
            set targetParticipant to participant targetHandle of targetService
            my sendPayload(targetParticipant, outgoingText, argv)
        end try
    end tell
    return "sent"
end run

on sendPayload(targetRef, outgoingText, argv)
    tell application "Messages"
        repeat with indexValue from 3 to count of argv
            set attachmentPath to item indexValue of argv
            set attachmentFile to (POSIX file attachmentPath) as alias
            send attachmentFile to targetRef
        end repeat
        if outgoingText is not equal to "" then
            send outgoingText to targetRef
        end if
    end tell
end sendPayload
"""
        group_script = """
on run argv
    set targetChatId to item 1 of argv
    set outgoingText to item 2 of argv
    tell application "Messages"
        try
            set targetChat to chat id targetChatId
            my sendPayload(targetChat, outgoingText, argv)
        on error
            set targetChat to text chat id targetChatId
            my sendPayload(targetChat, outgoingText, argv)
        end try
    end tell
    return "sent"
end run

on sendPayload(targetRef, outgoingText, argv)
    tell application "Messages"
        repeat with indexValue from 3 to count of argv
            set attachmentPath to item indexValue of argv
            set attachmentFile to (POSIX file attachmentPath) as alias
            send attachmentFile to targetRef
        end repeat
        if outgoingText is not equal to "" then
            send outgoingText to targetRef
        end if
    end tell
end sendPayload
"""
        group_chat = self._resolve_group_chat(recipient)
        direct_chat = None if group_chat else self._resolve_direct_chat(recipient)
        if group_chat:
            script = group_script
            script_recipient = group_chat["guid"]
            pre_send_rowid = self._latest_chat_message_rowid(group_chat["guid"])
            pre_send_attachment_rowid = self._latest_chat_attachment_rowid(group_chat["guid"])
            verification_target = ("group", group_chat["guid"])
        elif direct_chat:
            script = group_script
            script_recipient = direct_chat["guid"]
            pre_send_rowid = self._latest_chat_message_rowid(direct_chat["guid"])
            pre_send_attachment_rowid = self._latest_chat_attachment_rowid(direct_chat["guid"])
            verification_target = ("group", direct_chat["guid"])
        else:
            if _looks_like_group_chat_identifier(recipient) or _looks_like_group_chat_guid(recipient):
                raise IMessageError(f"No existing group chat found for {recipient}")
            script = direct_script
            script_recipient = recipient
            pre_send_rowid = self._latest_direct_message_rowid(recipient)
            pre_send_attachment_rowid = self._latest_direct_attachment_rowid(recipient)
            verification_target = ("direct", recipient)
        duplicate_match = self._find_recent_duplicate_attachment(
            verification_target[0],
            verification_target[1],
            prepared_images,
        )
        if duplicate_match is not None:
            raise IMessageError(
                f"Refusing duplicate image send for {recipient}; matching recent attachment {duplicate_match}"
            )
        start = time.monotonic()
        subprocess_env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(_host_home()),
        }
        log.info(
            "Starting AppleScript iMessage send target_kind=%s message_len=%d image_count=%d timeout=%ss",
            verification_target[0],
            len(message),
            len(prepared_images),
            self.timeout_seconds,
        )
        send_image_paths = self._stage_send_attachments(prepared_images)
        try:
            completed = subprocess.run(
                ["osascript", "-", script_recipient, message, *send_image_paths],
                input=script,
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=True,
                env=subprocess_env,
            )
        except subprocess.TimeoutExpired as exc:
            elapsed = time.monotonic() - start
            stdout = (exc.stdout or "").strip() if isinstance(exc.stdout, str) else ""
            stderr = (exc.stderr or "").strip() if isinstance(exc.stderr, str) else ""
            log.error(
                "AppleScript iMessage send timed out target_kind=%s elapsed=%.3fs stdout_len=%d stderr_len=%d",
                verification_target[0],
                elapsed,
                len(stdout),
                len(stderr),
            )
            raise IMessageError(f"Timed out sending iMessage after {self.timeout_seconds}s") from exc
        except subprocess.CalledProcessError as exc:
            details = (exc.stderr or exc.stdout or "").strip() or "unknown AppleScript error"
            elapsed = time.monotonic() - start
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            log.error(
                "AppleScript iMessage send failed target_kind=%s elapsed=%.3fs returncode=%s stdout_len=%d stderr_len=%d",
                verification_target[0],
                elapsed,
                exc.returncode,
                len(stdout),
                len(stderr),
            )
            raise IMessageError(f"Messages send failed: {details}") from exc
        output = (completed.stdout or "").strip()
        verified = self._wait_for_send(
            verification_target[0],
            verification_target[1],
            message,
            pre_send_rowid or 0,
            pre_send_attachment_rowid or 0,
            prepared_images,
        )
        if verified is None:
            log.warning(
                "AppleScript iMessage send completed but verification was unavailable target_kind=%s",
                verification_target[0],
            )
        elif verified is False:
            log.warning(
                "AppleScript iMessage send completed but verification timed out target_kind=%s",
                verification_target[0],
            )
        elapsed = time.monotonic() - start
        log.info(
            "AppleScript iMessage send completed target_kind=%s elapsed=%.3fs returncode=%s stdout_len=%d stderr_len=%d",
            verification_target[0],
            elapsed,
            completed.returncode,
            len(completed.stdout or ""),
            len(completed.stderr or ""),
        )
        return {
            "mode": "live",
            "recipient": recipient,
            "message": message,
            "image_paths": prepared_images,
            "sent": True,
            "verified": verified is True,
            "result": output or "sent",
        }

    def _messages_db_path(self) -> Path:
        return Path(self.db_path or default_chat_db_path()).expanduser()

    def _connect_messages_db(self) -> sqlite3.Connection:
        db_path = self._messages_db_path()
        if not db_path.exists():
            raise IMessageError(f"Messages database not found at {db_path}")
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def _resolve_group_chat(self, recipient: str) -> dict[str, str] | None:
        if not (_looks_like_group_chat_identifier(recipient) or _looks_like_group_chat_guid(recipient)):
            return None
        conn = self._connect_messages_db()
        try:
            row = conn.execute(
                """
                SELECT guid, chat_identifier
                FROM chat
                WHERE guid = ? OR chat_identifier = ?
                LIMIT 1
                """,
                (recipient, recipient),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        guid = str(row["guid"] or "").strip()
        chat_identifier = str(row["chat_identifier"] or "").strip()
        if not guid:
            return None
        if not (_looks_like_group_chat_identifier(chat_identifier) or _looks_like_group_chat_guid(guid)):
            return None
        return {"guid": guid, "chat_identifier": chat_identifier}

    def _resolve_direct_chat(self, recipient: str) -> dict[str, str] | None:
        conn = self._connect_messages_db()
        try:
            try:
                row = conn.execute(
                    """
                    SELECT guid, chat_identifier
                    FROM chat
                    WHERE chat_identifier = ?
                    LIMIT 1
                    """,
                    (recipient,),
                ).fetchone()
            except sqlite3.OperationalError:
                return None
        finally:
            conn.close()
        if not row:
            return None
        guid = str(row["guid"] or "").strip()
        chat_identifier = str(row["chat_identifier"] or "").strip()
        if not guid or not chat_identifier:
            return None
        if _looks_like_group_chat_identifier(chat_identifier) or _looks_like_group_chat_guid(guid):
            return None
        return {"guid": guid, "chat_identifier": chat_identifier}

    def _stage_send_attachments(self, image_paths: list[str]) -> list[str]:
        if not image_paths:
            return []
        staging_dir = _host_home() / "Library" / "Messages" / "Attachments" / "imessage_wrapper"
        staging_dir.mkdir(parents=True, exist_ok=True)
        staged: list[str] = []
        for image_path in image_paths:
            source = Path(image_path)
            target = staging_dir / f"{uuid4()}-{source.name}"
            shutil.copy2(source, target)
            staged.append(str(target))
        log.info("Staged iMessage attachments count=%d", len(staged))
        return staged

    def _latest_chat_message_rowid(self, chat_guid: str) -> int:
        conn = self._connect_messages_db()
        try:
            row = conn.execute(
                """
                SELECT MAX(m.ROWID) AS max_rowid
                FROM message m
                JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                JOIN chat c ON c.ROWID = cmj.chat_id
                WHERE c.guid = ?
                """,
                (chat_guid,),
            ).fetchone()
        finally:
            conn.close()
        value = row["max_rowid"] if row else None
        return int(value or 0)

    def _latest_chat_attachment_rowid(self, chat_guid: str) -> int:
        conn = self._connect_messages_db()
        try:
            try:
                row = conn.execute(
                    """
                    SELECT MAX(a.ROWID) AS max_rowid
                    FROM attachment a
                    JOIN message_attachment_join maj ON maj.attachment_id = a.ROWID
                    JOIN message m ON m.ROWID = maj.message_id
                    JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                    JOIN chat c ON c.ROWID = cmj.chat_id
                    WHERE c.guid = ?
                      AND m.is_from_me = 1
                    """,
                    (chat_guid,),
                ).fetchone()
            except sqlite3.OperationalError:
                return 0
        finally:
            conn.close()
        value = row["max_rowid"] if row else None
        return int(value or 0)

    def _latest_direct_message_rowid(self, recipient: str) -> int:
        conn = self._connect_messages_db()
        try:
            try:
                row = conn.execute(
                    """
                    SELECT MAX(m.ROWID) AS max_rowid
                    FROM message m
                    LEFT JOIN handle h ON h.ROWID = m.handle_id
                    LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                    LEFT JOIN chat c ON c.ROWID = cmj.chat_id
                    WHERE m.is_from_me = 1
                      AND (
                            h.id = ?
                            OR h.uncanonicalized_id = ?
                            OR c.chat_identifier = ?
                          )
                    """,
                    (recipient, recipient, recipient),
                ).fetchone()
            except sqlite3.OperationalError:
                return 0
        finally:
            conn.close()
        value = row["max_rowid"] if row else None
        return int(value or 0)

    def _latest_direct_attachment_rowid(self, recipient: str) -> int:
        conn = self._connect_messages_db()
        try:
            try:
                row = conn.execute(
                    """
                    SELECT MAX(a.ROWID) AS max_rowid
                    FROM attachment a
                    JOIN message_attachment_join maj ON maj.attachment_id = a.ROWID
                    JOIN message m ON m.ROWID = maj.message_id
                    LEFT JOIN handle h ON h.ROWID = m.handle_id
                    LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                    LEFT JOIN chat c ON c.ROWID = cmj.chat_id
                    WHERE m.is_from_me = 1
                      AND (
                            h.id = ?
                            OR h.uncanonicalized_id = ?
                            OR c.chat_identifier = ?
                          )
                    """,
                    (recipient, recipient, recipient),
                ).fetchone()
            except sqlite3.OperationalError:
                return 0
        finally:
            conn.close()
        value = row["max_rowid"] if row else None
        return int(value or 0)

    def _find_recent_duplicate_attachment(
        self,
        target_kind: str,
        target_value: str,
        image_paths: list[str],
    ) -> str | None:
        if not image_paths:
            return None
        outgoing_hashes = {path: _sha256_file(Path(path)) for path in image_paths}
        if not outgoing_hashes:
            return None
        conn = self._connect_messages_db()
        try:
            try:
                cutoff = _apple_timestamp_seconds_ago(self.duplicate_window_seconds)
                if target_kind == "group":
                    rows = conn.execute(
                        """
                        SELECT a.filename, a.transfer_name
                        FROM attachment a
                        JOIN message_attachment_join maj ON maj.attachment_id = a.ROWID
                        JOIN message m ON m.ROWID = maj.message_id
                        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                        JOIN chat c ON c.ROWID = cmj.chat_id
                        WHERE c.guid = ?
                          AND m.is_from_me = 1
                          AND m.date >= ?
                        ORDER BY a.ROWID DESC
                        LIMIT 20
                        """,
                        (target_value, cutoff),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT a.filename, a.transfer_name
                        FROM attachment a
                        JOIN message_attachment_join maj ON maj.attachment_id = a.ROWID
                        JOIN message m ON m.ROWID = maj.message_id
                        LEFT JOIN handle h ON h.ROWID = m.handle_id
                        LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                        LEFT JOIN chat c ON c.ROWID = cmj.chat_id
                        WHERE m.is_from_me = 1
                          AND m.date >= ?
                          AND (
                                h.id = ?
                                OR h.uncanonicalized_id = ?
                                OR c.chat_identifier = ?
                              )
                        ORDER BY a.ROWID DESC
                        LIMIT 20
                        """,
                        (cutoff, target_value, target_value, target_value),
                    ).fetchall()
            except sqlite3.OperationalError:
                return None
        finally:
            conn.close()
        seen_hashes: dict[str, str] = {}
        for row in rows:
            attachment_path = _resolve_attachment_path(row["filename"])
            if not attachment_path:
                continue
            candidate = Path(attachment_path).expanduser()
            if not candidate.exists() or not candidate.is_file():
                continue
            try:
                seen_hashes[str(candidate)] = _sha256_file(candidate)
            except OSError:
                continue
        for source_path, digest in outgoing_hashes.items():
            for existing_path, existing_digest in seen_hashes.items():
                if digest == existing_digest:
                    log.info(
                        "Blocked duplicate iMessage attachment target_kind=%s",
                        target_kind,
                    )
                    return existing_path
        return None

    def _wait_for_send(
        self,
        target_kind: str,
        target_value: str,
        message: str,
        min_rowid: int,
        min_attachment_rowid: int,
        image_paths: list[str],
    ) -> bool | None:
        deadline = time.monotonic() + self.verification_timeout_seconds
        while time.monotonic() <= deadline:
            try:
                visible = self._send_visible(
                    target_kind,
                    target_value,
                    message,
                    min_rowid,
                    min_attachment_rowid,
                    image_paths,
                )
                if visible is None:
                    return None
                if visible:
                    log.info(
                        "Verified iMessage send target_kind=%s min_rowid=%d min_attachment_rowid=%d image_count=%d",
                        target_kind,
                        min_rowid,
                        min_attachment_rowid,
                        len(image_paths),
                    )
                    return True
            except sqlite3.Error as exc:
                log.warning(
                    "Retrying send verification target_kind=%s after sqlite error: %s",
                    target_kind,
                    exc,
                )
            time.sleep(self.verification_poll_interval_seconds)
        return False

    def _send_visible(
        self,
        target_kind: str,
        target_value: str,
        message: str,
        min_rowid: int,
        min_attachment_rowid: int,
        image_paths: list[str],
    ) -> bool | None:
        conn = self._connect_messages_db()
        try:
            try:
                if target_kind == "group":
                    rows = conn.execute(
                        """
                        SELECT m.ROWID, m.text, m.subject, m.attributedBody
                        FROM message m
                        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                        JOIN chat c ON c.ROWID = cmj.chat_id
                        WHERE c.guid = ?
                          AND m.is_from_me = 1
                          AND m.ROWID > ?
                        ORDER BY m.ROWID DESC
                        LIMIT 10
                        """,
                        (target_value, min_rowid),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT DISTINCT m.ROWID, m.text, m.subject, m.attributedBody
                        FROM message m
                        LEFT JOIN handle h ON h.ROWID = m.handle_id
                        LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                        LEFT JOIN chat c ON c.ROWID = cmj.chat_id
                        WHERE m.is_from_me = 1
                          AND m.ROWID > ?
                          AND (
                                h.id = ?
                                OR h.uncanonicalized_id = ?
                                OR c.chat_identifier = ?
                              )
                        ORDER BY m.ROWID DESC
                        LIMIT 10
                        """,
                        (min_rowid, target_value, target_value, target_value),
                    ).fetchall()
            except sqlite3.OperationalError:
                return None
            attachments_by_message = {}
            if image_paths:
                attachments_by_message = _fetch_message_attachments(
                    conn,
                    [int(row["ROWID"]) for row in rows],
                    min_attachment_rowid,
                )
        finally:
            conn.close()
        expected_text = _normalize_message_text(message) if message.strip() else ""
        expected_attachments = [_expected_attachment_metadata(path) for path in image_paths]
        seen_attachments = [
            _seen_attachment_metadata(attachment)
            for row in rows
            for attachment in attachments_by_message.get(int(row["ROWID"]), [])
        ]
        text_seen = expected_text == ""
        for row in rows:
            text = row["text"] or row["subject"] or ""
            if not text_seen and _normalize_message_text(text) == expected_text:
                text_seen = True
            if not text_seen and _attributed_body_contains_text(row["attributedBody"], expected_text):
                text_seen = True
            extracted = _extract_attributed_body_text(row["attributedBody"]) or ""
            if not text_seen and _normalize_message_text(extracted) == expected_text:
                text_seen = True
        return text_seen and _attachments_match(expected_attachments, seen_attachments)


def _prepare_image_paths(image_paths: list[str]) -> list[str]:
    prepared: list[str] = []
    for raw in image_paths:
        candidate = str(raw or "").strip()
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if not path.exists():
            raise ValueError(f"attachment not found: {path}")
        if not path.is_file():
            raise ValueError(f"attachment path is not a file: {path}")
        prepared.append(str(path.resolve()))
    return prepared


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _apple_timestamp_seconds_ago(seconds: int) -> int:
    target = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return int((target - APPLE_EPOCH).total_seconds() * 1_000_000_000)


def _fetch_message_attachments(
    conn: sqlite3.Connection,
    row_ids: list[int],
    min_attachment_rowid: int = 0,
) -> dict[int, list[dict[str, Any]]]:
    if not row_ids:
        return {}
    placeholders = ", ".join("?" for _ in row_ids)
    rows = conn.execute(
        f"""
        SELECT
            maj.message_id,
            a.ROWID AS rowid,
            a.guid,
            a.filename,
            a.mime_type,
            a.total_bytes,
            a.transfer_name,
            a.uti
        FROM message_attachment_join maj
        JOIN attachment a ON a.ROWID = maj.attachment_id
        WHERE maj.message_id IN ({placeholders})
          AND a.ROWID > ?
        ORDER BY maj.message_id ASC, a.created_date ASC, a.ROWID ASC
        """,
        (*row_ids, min_attachment_rowid),
    ).fetchall()
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(int(row["message_id"]), []).append(
            {
                "rowid": row["rowid"],
                "guid": row["guid"],
                "filename": _resolve_attachment_path(row["filename"]),
                "mime_type": row["mime_type"],
                "total_bytes": row["total_bytes"],
                "transfer_name": row["transfer_name"],
                "uti": row["uti"],
            }
        )
    return grouped


def _expected_attachment_metadata(path: str) -> dict[str, Any]:
    resolved = Path(path)
    mime_type, _ = mimetypes.guess_type(resolved.name)
    return {
        "name": resolved.name,
        "size": resolved.stat().st_size,
        "mime_type": mime_type or "",
    }


def _seen_attachment_metadata(attachment: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": Path(str(attachment.get("transfer_name") or attachment.get("filename") or "")).name,
        "size": int(attachment.get("total_bytes") or 0),
        "mime_type": str(attachment.get("mime_type") or ""),
    }


def _attachments_match(expected: list[dict[str, Any]], seen: list[dict[str, Any]]) -> bool:
    remaining = list(seen)
    for item in expected:
        exact_index = next(
            (
                index
                for index, candidate in enumerate(remaining)
                if candidate["name"] == item["name"] and candidate["size"] == item["size"]
            ),
            None,
        )
        if exact_index is not None:
            remaining.pop(exact_index)
            continue
        fallback_index = next(
            (
                index
                for index, candidate in enumerate(remaining)
                if candidate["size"] == item["size"]
                and candidate["mime_type"] == item["mime_type"]
                and (candidate["mime_type"] or "").startswith("image/")
            ),
            None,
        )
        if fallback_index is None:
            return False
        remaining.pop(fallback_index)
    return True


def _looks_like_group_chat_identifier(value: str) -> bool:
    return bool(_GROUP_CHAT_IDENTIFIER_RE.fullmatch((value or "").strip()))


def _looks_like_group_chat_guid(value: str) -> bool:
    raw = (value or "").strip()
    if ";" not in raw:
        return False
    suffix = raw.rsplit(";", 1)[-1]
    return _looks_like_group_chat_identifier(suffix)


def _normalize_message_text(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip()


def _attributed_body_contains_text(value: bytes | None, expected_text: str) -> bool:
    if not value:
        return False
    expected = _normalize_message_text(expected_text)
    if not expected:
        return False
    decoded = _normalize_message_text(_extract_attributed_body_text(value))
    return expected in decoded


def get_imessage_reader() -> IMessageReader:
    mode = os.environ.get("IMESSAGE_WRAPPER_READ_MODE", "live").strip().lower()
    if mode == "stub":
        stub_path_raw = os.environ.get("IMESSAGE_WRAPPER_STUB_PATH", "").strip()
        if not stub_path_raw:
            raise IMessageError("IMESSAGE_WRAPPER_STUB_PATH is required when IMESSAGE_WRAPPER_READ_MODE=stub")
        stub_path = Path(stub_path_raw).expanduser()
        payload = json.loads(stub_path.read_text())
        payload["_stub_path"] = str(stub_path)
        return StubIMessageReader(payload)
    if mode != "live":
        raise IMessageError("IMESSAGE_WRAPPER_READ_MODE must be 'live' or 'stub'")
    db_path = Path(os.environ.get("IMESSAGE_WRAPPER_DB_PATH", str(default_chat_db_path()))).expanduser()
    return LiveIMessageReader(db_path)


def get_contacts_reader() -> ContactsReader:
    mode = os.environ.get("IMESSAGE_WRAPPER_READ_MODE", "live").strip().lower()
    if mode == "stub":
        stub_path_raw = os.environ.get("IMESSAGE_WRAPPER_STUB_PATH", "").strip()
        if not stub_path_raw:
            raise IMessageError("IMESSAGE_WRAPPER_STUB_PATH is required when IMESSAGE_WRAPPER_READ_MODE=stub")
        stub_path = Path(stub_path_raw).expanduser()
        payload = json.loads(stub_path.read_text())
        payload["_stub_path"] = str(stub_path)
        return StubContactsReader(payload)
    if mode != "live":
        raise IMessageError("IMESSAGE_WRAPPER_READ_MODE must be 'live' or 'stub'")
    primary_db = Path(os.environ.get("IMESSAGE_WRAPPER_CONTACTS_DB_PATH", str(default_contacts_db_path()))).expanduser()
    source_dir = Path(os.environ.get("IMESSAGE_WRAPPER_CONTACTS_SOURCES_DIR", str(default_contacts_sources_dir()))).expanduser()
    db_paths = [primary_db]
    if source_dir.exists():
        db_paths.extend(sorted(source_dir.glob("*/AddressBook-v22.abcddb")))
    deduped: list[Path] = []
    seen = set()
    for path in db_paths:
        normalized = str(path)
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(path)
    return LiveContactsReader(deduped)


def get_imessage_sender() -> IMessageSender:
    mode = os.environ.get("IMESSAGE_WRAPPER_SEND_MODE", "live").strip().lower()
    if mode == "live":
        timeout = int(os.environ.get("IMESSAGE_WRAPPER_SEND_TIMEOUT_SECONDS", DEFAULT_SEND_TIMEOUT_SECONDS))
        db_path = Path(os.environ.get("IMESSAGE_WRAPPER_DB_PATH", str(default_chat_db_path()))).expanduser()
        return AppleScriptIMessageSender(timeout_seconds=timeout, db_path=db_path)
    if mode == "dry-run":
        return DryRunIMessageSender()
    raise IMessageError("IMESSAGE_WRAPPER_SEND_MODE must be 'dry-run' or 'live'")

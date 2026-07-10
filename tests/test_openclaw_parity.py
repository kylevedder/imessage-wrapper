from __future__ import annotations

import sqlite3
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from imessage_wrapper import (
    ChatMediaStats,
    ChatMessageStats,
    DateMessageStats,
    IMessageClient,
    MediaStats,
    MediaTypeStats,
    MessageStats,
    MessageWrapperError,
    SenderMessageStats,
    ServiceMessageStats,
)
from imessage_wrapper.core import APPLE_EPOCH


URL_PREVIEW_BUNDLE_ID = "com.apple.messages.URLBalloonProvider"
BASE_TIME = datetime(2025, 1, 1, 1, 30, tzinfo=timezone.utc)


def apple_ns(value: datetime) -> int:
    return int((value.astimezone(timezone.utc) - APPLE_EPOCH).total_seconds() * 1_000_000_000)


def make_client(path) -> IMessageClient:
    return IMessageClient(messages_db_path=path, contacts_db_paths=[], enrich_contacts=False)


def create_modern_schema(conn: sqlite3.Connection, *, include_media: bool = True) -> None:
    conn.executescript(
        """
        CREATE TABLE handle (
            ROWID INTEGER PRIMARY KEY,
            id TEXT,
            service TEXT,
            uncanonicalized_id TEXT
        );
        CREATE TABLE chat (
            ROWID INTEGER PRIMARY KEY,
            guid TEXT,
            chat_identifier TEXT,
            display_name TEXT,
            service_name TEXT,
            account_id TEXT,
            account_login TEXT,
            last_addressed_handle TEXT
        );
        CREATE TABLE chat_handle_join (
            chat_id INTEGER,
            handle_id INTEGER
        );
        CREATE TABLE message (
            ROWID INTEGER PRIMARY KEY,
            guid TEXT,
            text TEXT,
            subject TEXT,
            attributedBody BLOB,
            handle_id INTEGER,
            service TEXT,
            date INTEGER,
            date_read INTEGER,
            date_delivered INTEGER,
            is_from_me INTEGER,
            is_read INTEGER,
            associated_message_guid TEXT,
            associated_message_type INTEGER,
            associated_message_emoji TEXT,
            thread_originator_guid TEXT,
            destination_caller_id TEXT,
            balloon_bundle_id TEXT
        );
        CREATE TABLE chat_message_join (
            chat_id INTEGER,
            message_id INTEGER
        );
        """
    )
    if include_media:
        conn.executescript(
            """
            CREATE TABLE attachment (
                ROWID INTEGER PRIMARY KEY,
                guid TEXT,
                filename TEXT,
                mime_type TEXT,
                total_bytes INTEGER,
                transfer_name TEXT,
                uti TEXT,
                created_date INTEGER,
                is_sticker INTEGER
            );
            CREATE TABLE message_attachment_join (
                message_id INTEGER,
                attachment_id INTEGER
            );
            """
        )


def create_legacy_db(path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE handle (
                ROWID INTEGER PRIMARY KEY,
                id TEXT,
                service TEXT
            );
            CREATE TABLE chat (
                ROWID INTEGER PRIMARY KEY,
                guid TEXT,
                chat_identifier TEXT,
                display_name TEXT,
                service_name TEXT
            );
            CREATE TABLE chat_handle_join (
                chat_id INTEGER,
                handle_id INTEGER
            );
            CREATE TABLE message (
                ROWID INTEGER PRIMARY KEY,
                guid TEXT,
                text TEXT,
                handle_id INTEGER,
                service TEXT,
                date INTEGER,
                is_from_me INTEGER
            );
            CREATE TABLE chat_message_join (
                chat_id INTEGER,
                message_id INTEGER
            );
            """
        )
        conn.execute("INSERT INTO handle VALUES (1, '+111', 'iMessage')")
        conn.execute(
            "INSERT INTO chat VALUES (1, 'iMessage;-;+111', '+111', 'Legacy', 'iMessage')"
        )
        conn.execute("INSERT INTO chat_handle_join VALUES (1, 1)")
        conn.execute(
            "INSERT INTO message VALUES (1, 'legacy-guid', 'legacy inbound', 1, 'iMessage', ?, 0)",
            (apple_ns(BASE_TIME),),
        )
        conn.execute("INSERT INTO chat_message_join VALUES (1, 1)")
        conn.commit()
    finally:
        conn.close()


def create_stats_db(path, *, include_media: bool = True) -> None:
    conn = sqlite3.connect(path)
    try:
        create_modern_schema(conn, include_media=include_media)
        conn.executemany(
            "INSERT INTO handle VALUES (?, ?, ?, ?)",
            [
                (1, "+111", "iMessage", "+111"),
                (2, "+222", "SMS", "+222"),
            ],
        )
        conn.executemany(
            """
            INSERT INTO chat
            VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL)
            """,
            [
                (1, "iMessage;-;+111", "+111", "Alpha", "iMessage"),
                (2, "SMS;-;+222", "+222", "Beta", "SMS"),
                (3, "iMessage;-;empty", "empty", "Empty", "iMessage"),
            ],
        )
        conn.executemany("INSERT INTO chat_handle_join VALUES (?, ?)", [(1, 1), (2, 2)])

        def at(seconds: int) -> int:
            return apple_ns(BASE_TIME + timedelta(seconds=seconds))

        conn.executemany(
            """
            INSERT INTO message (
                ROWID, guid, text, handle_id, service, date, date_read,
                is_from_me, is_read, associated_message_guid,
                associated_message_type, destination_caller_id, balloon_bundle_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (1, "text-guid", "See https://example.com", 1, "iMessage", at(0), 0, 0, 0, None, None, None, None),
                (2, "preview-guid", "https://example.com", 1, "iMessage", at(1), 0, 0, 0, None, None, None, URL_PREVIEW_BUNDLE_ID),
                (3, "normal-guid", "normal inbound", 1, "iMessage", at(2), at(20), 0, 1, None, None, None, None),
                (4, "outbound-guid", "outbound", 1, "iMessage", at(3), 0, 1, 0, None, None, "+111", None),
                (5, "reaction-add", "Liked outbound", 1, "iMessage", at(4), 0, 0, 1, "p:0/outbound-guid", 2000, None, None),
                (6, "reaction-remove", "Removed like", 1, "iMessage", at(5), 0, 0, 1, "p:0/outbound-guid", 3000, None, None),
                (7, "standalone-preview", "https://standalone.test", 1, "iMessage", at(6), 0, 0, 0, None, None, None, URL_PREVIEW_BUNDLE_ID),
                (8, "sms-in", "sms inbound", 2, "SMS", at(7), 0, 0, 1, None, None, None, None),
                (9, "sms-out", "sms outbound", 2, "SMS", at(8), 0, 1, 0, None, None, "+222", None),
            ],
        )
        conn.executemany(
            "INSERT INTO chat_message_join VALUES (?, ?)",
            [
                (1, 1),
                (1, 2),
                (1, 3),
                (1, 3),
                (1, 4),
                (1, 5),
                (1, 6),
                (1, 7),
                (2, 8),
                (2, 9),
            ],
        )
        if include_media:
            conn.executemany(
                "INSERT INTO attachment VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (1, "attachment-1", "/tmp/a.jpg", "image/jpeg", 10, "a.jpg", "public.jpeg", at(2), 0),
                    (2, "attachment-2", "/tmp/b.mov", "video/quicktime", 20, "b.mov", "com.apple.quicktime-movie", at(3), 0),
                ],
            )
            conn.executemany(
                "INSERT INTO message_attachment_join VALUES (?, ?)",
                [(3, 1), (3, 1), (4, 2)],
            )
        conn.commit()
    finally:
        conn.close()


def test_messages_expose_inbound_read_state_and_omit_outbound_fields(tmp_path):
    db_path = tmp_path / "read-state.db"
    conn = sqlite3.connect(db_path)
    try:
        create_modern_schema(conn)
        read_at = BASE_TIME + timedelta(minutes=5)
        conn.execute("INSERT INTO handle VALUES (1, '+111', 'iMessage', '+111')")
        conn.execute(
            "INSERT INTO chat VALUES (1, 'iMessage;-;+111', '+111', 'Read State', 'iMessage', NULL, NULL, NULL)"
        )
        conn.execute("INSERT INTO chat_handle_join VALUES (1, 1)")
        conn.executemany(
            """
            INSERT INTO message (
                ROWID, guid, text, handle_id, service, date, date_read,
                is_from_me, is_read, destination_caller_id
            )
            VALUES (?, ?, ?, ?, 'iMessage', ?, ?, ?, ?, ?)
            """,
            [
                (1, "unread-guid", "unread inbound", 1, apple_ns(BASE_TIME), 0, 0, 0, None),
                (2, "read-guid", "read inbound", 1, apple_ns(BASE_TIME + timedelta(seconds=1)), apple_ns(read_at), 0, 1, None),
                (3, "outbound-guid", "outbound", 1, apple_ns(BASE_TIME + timedelta(seconds=2)), apple_ns(read_at), 1, 0, "+111"),
                (4, "sentinel-guid", "read without timestamp", 1, apple_ns(BASE_TIME + timedelta(seconds=3)), -1, 0, 1, None),
            ],
        )
        conn.executemany("INSERT INTO chat_message_join VALUES (1, ?)", [(1,), (2,), (3,), (4,)])
        conn.commit()
    finally:
        conn.close()

    by_id = {message.id: message for message in make_client(db_path).messages(1)}

    assert by_id[1].is_read is False
    assert by_id[1].date_read is None
    assert by_id[1].to_dict()["is_read"] is False
    assert "date_read" not in by_id[1].to_dict()

    assert by_id[2].is_read is True
    assert by_id[2].date_read == read_at
    assert by_id[2].to_dict()["date_read"] == read_at.isoformat()

    assert by_id[3].is_read is None
    assert by_id[3].date_read is None
    assert "is_read" not in by_id[3].to_dict()
    assert "date_read" not in by_id[3].to_dict()

    assert by_id[4].is_read is True
    assert by_id[4].date_read is None
    assert by_id[4].to_dict()["is_read"] is True
    assert "date_read" not in by_id[4].to_dict()


def test_chats_count_logical_unread_and_filter_before_limit(tmp_path):
    db_path = tmp_path / "unread-chats.db"
    conn = sqlite3.connect(db_path)
    try:
        create_modern_schema(conn)
        conn.executemany(
            "INSERT INTO handle VALUES (?, ?, 'iMessage', ?)",
            [(1, "+111", "+111"), (2, "+222", "+222"), (3, "+333", "+333")],
        )
        conn.executemany(
            "INSERT INTO chat VALUES (?, ?, ?, ?, 'iMessage', NULL, NULL, NULL)",
            [
                (1, "iMessage;-;+111", "+111", "Older Unread"),
                (2, "iMessage;-;+222", "+222", "Newer Read"),
                (3, "iMessage;-;+333", "+333", "Newest Read Link"),
            ],
        )
        conn.executemany("INSERT INTO chat_handle_join VALUES (?, ?)", [(1, 1), (2, 2), (3, 3)])
        conn.executemany(
            """
            INSERT INTO message (
                ROWID, guid, text, handle_id, service, date, date_read,
                is_from_me, is_read, balloon_bundle_id
            )
            VALUES (?, ?, ?, ?, 'iMessage', ?, ?, 0, ?, ?)
            """,
            [
                (1, "unread-text", "See https://one.test", 1, apple_ns(BASE_TIME), 0, 0, None),
                (2, "unread-preview", "https://one.test", 1, apple_ns(BASE_TIME + timedelta(seconds=1)), 0, 0, URL_PREVIEW_BUNDLE_ID),
                (3, "read-only", "already read", 2, apple_ns(BASE_TIME + timedelta(seconds=10)), apple_ns(BASE_TIME + timedelta(seconds=15)), 1, None),
                (4, "read-text", "See https://three.test", 3, apple_ns(BASE_TIME + timedelta(seconds=20)), apple_ns(BASE_TIME + timedelta(seconds=25)), 1, None),
                (5, "false-preview", "https://three.test", 3, apple_ns(BASE_TIME + timedelta(seconds=21)), 0, 0, URL_PREVIEW_BUNDLE_ID),
            ],
        )
        conn.executemany(
            "INSERT INTO chat_message_join VALUES (?, ?)",
            [(1, 1), (1, 2), (1, 2), (2, 3), (3, 4), (3, 5)],
        )
        conn.commit()
    finally:
        conn.close()

    client = make_client(db_path)
    chats = {chat.id: chat for chat in client.chats()}

    assert chats[1].unread_count == 1
    assert chats[2].unread_count == 0
    assert chats[3].unread_count == 0
    assert chats[3].to_dict()["unread_count"] == 0

    filtered = client.chats(limit=1, unread_only=True)
    assert [chat.id for chat in filtered] == [1]
    assert filtered[0].unread_count == 1


def create_consecutive_preview_db(path, *, base_is_read: bool) -> None:
    conn = sqlite3.connect(path)
    try:
        create_modern_schema(conn, include_media=False)
        conn.execute("INSERT INTO handle VALUES (1, '+111', 'iMessage', '+111')")
        conn.execute(
            "INSERT INTO chat VALUES (1, 'iMessage;-;+111', '+111', 'Two Links', 'iMessage', NULL, NULL, NULL)"
        )
        conn.execute("INSERT INTO chat_handle_join VALUES (1, 1)")
        conn.executemany(
            """
            INSERT INTO message (
                ROWID, guid, text, handle_id, service, date, date_read,
                is_from_me, is_read, balloon_bundle_id
            )
            VALUES (?, ?, ?, 1, 'iMessage', ?, ?, 0, ?, ?)
            """,
            [
                (
                    1,
                    "base-guid",
                    "See https://one.test and https://two.test",
                    apple_ns(BASE_TIME),
                    apple_ns(BASE_TIME + timedelta(seconds=10)) if base_is_read else 0,
                    int(base_is_read),
                    None,
                ),
                (
                    2,
                    "preview-one",
                    "https://one.test",
                    apple_ns(BASE_TIME + timedelta(seconds=1)),
                    0,
                    0,
                    URL_PREVIEW_BUNDLE_ID,
                ),
                (
                    3,
                    "preview-two",
                    "https://two.test",
                    apple_ns(BASE_TIME + timedelta(seconds=2)),
                    0,
                    0,
                    URL_PREVIEW_BUNDLE_ID,
                ),
            ],
        )
        conn.executemany("INSERT INTO chat_message_join VALUES (1, ?)", [(1,), (2,), (3,)])
        conn.commit()
    finally:
        conn.close()


def test_consecutive_url_previews_count_once_for_unread_base(tmp_path):
    db_path = tmp_path / "unread-consecutive-previews.db"
    create_consecutive_preview_db(db_path, base_is_read=False)
    client = make_client(db_path)

    chat = client.chats()[0]
    unread = client.chats(unread_only=True)

    assert chat.unread_count == 1
    assert [item.id for item in unread] == [1]
    assert unread[0].unread_count == 1


def test_consecutive_unread_url_previews_use_read_base_state(tmp_path):
    db_path = tmp_path / "read-consecutive-previews.db"
    create_consecutive_preview_db(db_path, base_is_read=True)
    client = make_client(db_path)

    assert client.chats()[0].unread_count == 0
    assert client.chats(unread_only=True) == []


def test_unread_preview_does_not_cross_opposite_direction_boundary(tmp_path):
    db_path = tmp_path / "unread-preview-boundary.db"
    conn = sqlite3.connect(db_path)
    try:
        create_modern_schema(conn, include_media=False)
        conn.execute("INSERT INTO handle VALUES (1, '+111', 'iMessage', '+111')")
        conn.execute(
            "INSERT INTO chat VALUES (1, 'iMessage;-;+111', '+111', 'Boundary', 'iMessage', NULL, NULL, NULL)"
        )
        conn.execute("INSERT INTO chat_handle_join VALUES (1, 1)")
        conn.executemany(
            """
            INSERT INTO message (
                ROWID, guid, text, handle_id, service, date, date_read,
                is_from_me, is_read, destination_caller_id, balloon_bundle_id
            )
            VALUES (?, ?, ?, 1, 'iMessage', ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    1,
                    "read-text",
                    "See https://example.com",
                    apple_ns(BASE_TIME),
                    apple_ns(BASE_TIME + timedelta(seconds=10)),
                    0,
                    1,
                    None,
                    None,
                ),
                (2, "outbound", "intervening", apple_ns(BASE_TIME + timedelta(seconds=1)), 0, 1, 0, "+111", None),
                (
                    3,
                    "unread-preview",
                    "https://example.com",
                    apple_ns(BASE_TIME + timedelta(seconds=2)),
                    0,
                    0,
                    0,
                    None,
                    URL_PREVIEW_BUNDLE_ID,
                ),
            ],
        )
        conn.executemany("INSERT INTO chat_message_join VALUES (1, ?)", [(1,), (2,), (3,)])
        conn.commit()
    finally:
        conn.close()

    chat = make_client(db_path).chats(unread_only=True)[0]
    assert chat.unread_count == 1


def test_unread_preview_predecessor_skips_broad_associated_event_range(tmp_path):
    db_path = tmp_path / "unread-preview-associated-event.db"
    conn = sqlite3.connect(db_path)
    try:
        create_modern_schema(conn, include_media=False)
        conn.execute("INSERT INTO handle VALUES (1, '+111', 'iMessage', '+111')")
        conn.execute(
            "INSERT INTO chat VALUES (1, 'iMessage;-;+111', '+111', 'Associated Event', 'iMessage', NULL, NULL, NULL)"
        )
        conn.execute("INSERT INTO chat_handle_join VALUES (1, 1)")
        conn.executemany(
            """
            INSERT INTO message (
                ROWID, guid, text, handle_id, service, date, date_read,
                is_from_me, is_read, associated_message_guid,
                associated_message_type, balloon_bundle_id
            )
            VALUES (?, ?, ?, 1, 'iMessage', ?, ?, 0, ?, ?, ?, ?)
            """,
            [
                (
                    1,
                    "base-guid",
                    "See https://example.test",
                    apple_ns(BASE_TIME),
                    apple_ns(BASE_TIME + timedelta(seconds=10)),
                    1,
                    None,
                    None,
                    None,
                ),
                (
                    2,
                    "associated-guid",
                    "associated event",
                    apple_ns(BASE_TIME + timedelta(seconds=1)),
                    apple_ns(BASE_TIME + timedelta(seconds=10)),
                    1,
                    "base-guid",
                    2500,
                    None,
                ),
                (
                    3,
                    "preview-guid",
                    "https://example.test",
                    apple_ns(BASE_TIME + timedelta(seconds=2)),
                    0,
                    0,
                    None,
                    None,
                    URL_PREVIEW_BUNDLE_ID,
                ),
            ],
        )
        conn.executemany("INSERT INTO chat_message_join VALUES (1, ?)", [(1,), (2,), (3,)])
        conn.commit()
    finally:
        conn.close()

    client = make_client(db_path)

    assert client.chats()[0].unread_count == 0
    assert client.chats(unread_only=True) == []


def test_unread_reaction_chat_is_visible_but_stats_excludes_reactions(tmp_path):
    db_path = tmp_path / "unread-reaction.db"
    conn = sqlite3.connect(db_path)
    try:
        create_modern_schema(conn, include_media=False)
        conn.execute("INSERT INTO handle VALUES (1, '+111', 'iMessage', '+111')")
        conn.execute(
            "INSERT INTO chat VALUES (1, 'iMessage;-;+111', '+111', 'Reaction', 'iMessage', NULL, NULL, NULL)"
        )
        conn.execute("INSERT INTO chat_handle_join VALUES (1, 1)")
        conn.execute(
            """
            INSERT INTO message (
                ROWID, guid, text, handle_id, service, date, date_read,
                is_from_me, is_read, associated_message_guid, associated_message_type
            )
            VALUES (1, 'reaction-guid', 'Liked a message', 1, 'iMessage', ?, 0, 0, 0,
                    'p:0/target-guid', 2000)
            """,
            (apple_ns(BASE_TIME),),
        )
        conn.execute("INSERT INTO chat_message_join VALUES (1, 1)")
        conn.commit()
    finally:
        conn.close()

    client = make_client(db_path)
    chat = client.chats()[0]
    unread = client.chats(unread_only=True)
    stats = client.stats(chat_id=1, time_zone="UTC")

    assert chat.unread_count == 1
    assert [item.id for item in unread] == [1]
    assert unread[0].unread_count == 1
    assert stats.total_messages == 0
    assert stats.time_zone == "GMT"


def test_legacy_schema_omits_read_state_but_still_supports_stats(tmp_path):
    db_path = tmp_path / "legacy.db"
    create_legacy_db(db_path)
    client = make_client(db_path)

    chat = client.chats()[0]
    message = client.messages(1)[0]
    stats = client.stats(time_zone="UTC")

    assert chat.unread_count is None
    assert "unread_count" not in chat.to_dict()
    assert message.is_read is None
    assert message.date_read is None
    assert "is_read" not in message.to_dict()
    assert "date_read" not in message.to_dict()
    assert stats.total_messages == 1
    assert stats.received_messages == 1
    assert stats.time_zone == "GMT"
    assert "media" not in stats.to_dict()

    with pytest.raises(MessageWrapperError, match="[Uu]nread.*is_read"):
        client.chats(unread_only=True)
    with pytest.raises(MessageWrapperError, match="media.*unavailable|attachment tables.*missing"):
        client.stats(include_media=True)


def test_read_flag_works_when_date_read_column_is_unavailable(tmp_path):
    db_path = tmp_path / "partial-read-state.db"
    create_legacy_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("ALTER TABLE message ADD COLUMN is_read INTEGER")
        conn.execute("UPDATE message SET is_read = 1")
        conn.commit()
    finally:
        conn.close()

    client = make_client(db_path)
    message = client.messages(1)[0]

    assert message.is_read is True
    assert message.date_read is None
    assert message.to_dict()["is_read"] is True
    assert "date_read" not in message.to_dict()
    assert client.chats()[0].unread_count == 0
    assert client.chats(unread_only=True) == []


def test_stats_aggregate_excludes_reactions_and_dedupes_message_joins(tmp_path):
    db_path = tmp_path / "stats.db"
    create_stats_db(db_path)

    stats = make_client(db_path).stats(time_zone="UTC")

    assert isinstance(stats, MessageStats)
    assert stats.total_messages == 6
    assert stats.sent_messages == 2
    assert stats.received_messages == 4
    assert stats.sent_messages + stats.received_messages == stats.total_messages
    assert stats.time_zone == "GMT"
    assert stats.to_dict()["time_zone"] == "GMT"
    assert all(isinstance(item, ChatMessageStats) for item in stats.chats)
    assert all(isinstance(item, SenderMessageStats) for item in stats.senders)
    assert all(isinstance(item, ServiceMessageStats) for item in stats.services)
    assert all(isinstance(item, DateMessageStats) for item in stats.dates)
    assert {item.chat_id: item.message_count for item in stats.chats} == {1: 4, 2: 2}
    assert {item.handle: item.message_count for item in stats.senders} == {"+111": 3, "+222": 1}
    assert {item.service: item.message_count for item in stats.services} == {"iMessage": 4, "SMS": 2}
    assert [(item.date, item.message_count) for item in stats.dates] == [("2025-01-01", 6)]
    assert stats.media is None
    assert "media" not in stats.to_dict()

    with pytest.raises(FrozenInstanceError):
        stats.total_messages = 0
    with pytest.raises(FrozenInstanceError):
        stats.chats[0].message_count = 0


def test_stats_dedupes_global_messages_and_media_across_chat_mappings(tmp_path):
    db_path = tmp_path / "cross-chat-stats.db"
    create_stats_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("INSERT INTO chat_message_join VALUES (2, 3)")
        conn.commit()
    finally:
        conn.close()

    stats = make_client(db_path).stats(include_media=True, time_zone="UTC")

    assert stats.total_messages == 6
    assert {item.chat_id: item.message_count for item in stats.chats} == {1: 4, 2: 3}
    assert stats.media is not None
    assert stats.media.total_attachments == 2
    assert stats.media.total_bytes == 30
    assert {
        item.chat_id: (item.attachment_count, item.total_bytes)
        for item in stats.media.chats
    } == {1: (2, 30), 2: (1, 10)}


def test_stats_chat_scope_timezone_and_distinct_media(tmp_path):
    db_path = tmp_path / "scoped-stats.db"
    create_stats_db(db_path)

    stats = make_client(db_path).stats(
        chat_id=1,
        include_media=True,
        time_zone="America/Los_Angeles",
    )

    assert stats.total_messages == 4
    assert stats.sent_messages == 1
    assert stats.received_messages == 3
    assert stats.time_zone == "America/Los_Angeles"
    assert [(item.date, item.message_count) for item in stats.dates] == [("2024-12-31", 4)]
    assert [item.chat_id for item in stats.chats] == [1]
    assert isinstance(stats.media, MediaStats)
    assert stats.media.total_attachments == 2
    assert stats.media.total_bytes == 30
    assert all(isinstance(item, MediaTypeStats) for item in stats.media.types)
    assert all(isinstance(item, ChatMediaStats) for item in stats.media.chats)
    assert {
        (item.uti, item.mime_type): (item.attachment_count, item.total_bytes)
        for item in stats.media.types
    } == {
        ("public.jpeg", "image/jpeg"): (1, 10),
        ("com.apple.quicktime-movie", "video/quicktime"): (1, 20),
    }
    assert [
        (item.chat_id, item.attachment_count, item.total_bytes)
        for item in stats.media.chats
    ] == [(1, 2, 30)]
    assert stats.to_dict()["media"]["total_attachments"] == 2


def test_stats_existing_empty_chat_and_validation_do_not_widen_scope(tmp_path, monkeypatch):
    db_path = tmp_path / "stats-validation.db"
    create_stats_db(db_path)
    client = make_client(db_path)

    empty = client.stats(chat_id=3, time_zone="UTC")
    assert empty.total_messages == 0
    assert empty.sent_messages == 0
    assert empty.received_messages == 0
    assert empty.chats == []
    assert empty.senders == []
    assert empty.services == []
    assert empty.dates == []

    for chat_id in (0, -1, True):
        with pytest.raises(MessageWrapperError, match="positive rowid"):
            client.stats(chat_id=chat_id)
    with pytest.raises(MessageWrapperError, match="chat_id 999.*does not exist"):
        client.stats(chat_id=999)
    with pytest.raises(MessageWrapperError, match="invalid.*time zone|invalid IANA"):
        client.stats(time_zone="Not/AZone")
    with pytest.raises(MessageWrapperError, match="invalid.*time zone|invalid IANA"):
        client.stats(time_zone="/etc/localtime")

    monkeypatch.setenv("TZ", "/etc/localtime")
    local = client.stats()
    assert local.time_zone != "/etc/localtime"
    assert ZoneInfo(local.time_zone).key == local.time_zone


def test_stats_url_preview_coalescing_requires_a_strict_companion(tmp_path):
    db_path = tmp_path / "strict-previews.db"
    conn = sqlite3.connect(db_path)
    try:
        create_modern_schema(conn, include_media=False)
        conn.execute("INSERT INTO handle VALUES (1, '+111', 'iMessage', '+111')")
        conn.execute(
            "INSERT INTO chat VALUES (1, 'iMessage;-;+111', '+111', 'Links', 'iMessage', NULL, NULL, NULL)"
        )
        conn.execute("INSERT INTO chat_handle_join VALUES (1, 1)")

        def at(seconds: int) -> int:
            return apple_ns(BASE_TIME + timedelta(seconds=seconds))

        conn.executemany(
            """
            INSERT INTO message (
                ROWID, guid, text, handle_id, service, date, date_read,
                is_from_me, is_read, destination_caller_id, balloon_bundle_id
            )
            VALUES (?, ?, ?, 1, 'iMessage', ?, 0, ?, 1, ?, ?)
            """,
            [
                (1, "text-one", "See https://one.test", at(0), 0, None, None),
                (2, "preview-one", "https://one.test", at(1), 0, None, URL_PREVIEW_BUNDLE_ID),
                (3, "text-two", "See https://two.test", at(2), 0, None, None),
                (4, "intervening", "intervening", at(3), 0, None, None),
                (5, "preview-two", "https://two.test", at(4), 0, None, URL_PREVIEW_BUNDLE_ID),
                (6, "outbound-three", "See https://three.test", at(5), 1, "+111", None),
                (7, "preview-three", "https://three.test", at(6), 0, None, URL_PREVIEW_BUNDLE_ID),
            ],
        )
        conn.executemany("INSERT INTO chat_message_join VALUES (1, ?)", [(rowid,) for rowid in range(1, 8)])
        conn.commit()
    finally:
        conn.close()

    stats = make_client(db_path).stats(time_zone="UTC")

    assert stats.total_messages == 6
    assert stats.sent_messages == 1
    assert stats.received_messages == 5


def test_stats_only_requires_media_tables_when_requested(tmp_path):
    db_path = tmp_path / "no-media.db"
    create_stats_db(db_path, include_media=False)
    client = make_client(db_path)

    assert client.stats(include_media=False, time_zone="UTC").total_messages == 6
    with pytest.raises(MessageWrapperError, match="media.*unavailable|attachment tables.*missing"):
        client.stats(include_media=True, time_zone="UTC")

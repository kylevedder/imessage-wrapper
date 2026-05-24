from __future__ import annotations

import sqlite3
import subprocess
from datetime import datetime, timezone

from imessage_wrapper import IMessageClient
from imessage_wrapper.core import APPLE_EPOCH


def apple_ns(value: datetime) -> int:
    return int((value.astimezone(timezone.utc) - APPLE_EPOCH).total_seconds() * 1_000_000_000)


def apple_seconds(value: datetime) -> int:
    return int((value.astimezone(timezone.utc) - APPLE_EPOCH).total_seconds())


def make_messages_db(path):
    conn = sqlite3.connect(path)
    try:
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
                destination_caller_id TEXT
            );
            CREATE TABLE chat_message_join (
                chat_id INTEGER,
                message_id INTEGER
            );
            CREATE TABLE attachment (
                ROWID INTEGER PRIMARY KEY,
                guid TEXT,
                filename TEXT,
                mime_type TEXT,
                total_bytes INTEGER,
                transfer_name TEXT,
                uti TEXT,
                created_date INTEGER
            );
            CREATE TABLE message_attachment_join (
                message_id INTEGER,
                attachment_id INTEGER
            );
            """
        )
        ts = apple_ns(datetime(2026, 5, 1, 12, tzinfo=timezone.utc))
        conn.execute("INSERT INTO handle VALUES (1, '+15550100001', 'iMessage', '+1 (555) 010-0001')")
        conn.execute(
            """
            INSERT INTO chat
            VALUES (1, 'iMessage;-;+15550100001', '+15550100001', NULL, 'iMessage',
                    'iMessage;+;me@example.test', 'me@example.test', '+15550100001')
            """
        )
        conn.execute("INSERT INTO chat_handle_join VALUES (1, 1)")
        conn.execute(
            """
            INSERT INTO message
            VALUES (1, 'msg-1', 'hello', NULL, NULL, 1, 'iMessage', ?, NULL, NULL,
                    0, 1, NULL, NULL, NULL, NULL, NULL)
            """,
            (ts,),
        )
        conn.execute("INSERT INTO chat_message_join VALUES (1, 1)")
        conn.commit()
    finally:
        conn.close()


def make_contacts_db(path):
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE ZABCDRECORD (
                Z_PK INTEGER PRIMARY KEY,
                ZNAME TEXT,
                ZFIRSTNAME TEXT,
                ZMIDDLENAME TEXT,
                ZLASTNAME TEXT,
                ZNICKNAME TEXT,
                ZORGANIZATION TEXT,
                ZSORTINGFIRSTNAME TEXT,
                ZSORTINGLASTNAME TEXT,
                ZCREATIONDATE INTEGER,
                ZMODIFICATIONDATE INTEGER
            );
            CREATE TABLE ZABCDPHONENUMBER (
                Z_PK INTEGER PRIMARY KEY,
                ZOWNER INTEGER,
                Z22_OWNER INTEGER,
                ZFULLNUMBER TEXT,
                ZLABEL TEXT,
                ZISPRIMARY INTEGER,
                ZORDERINGINDEX INTEGER
            );
            CREATE TABLE ZABCDEMAILADDRESS (
                Z_PK INTEGER PRIMARY KEY,
                ZOWNER INTEGER,
                Z22_OWNER INTEGER,
                ZADDRESS TEXT,
                ZLABEL TEXT,
                ZISPRIMARY INTEGER,
                ZORDERINGINDEX INTEGER
            );
            """
        )
        created = apple_seconds(datetime(2026, 4, 1, tzinfo=timezone.utc))
        modified = apple_seconds(datetime(2026, 4, 2, tzinfo=timezone.utc))
        conn.execute(
            """
            INSERT INTO ZABCDRECORD
            VALUES (1, 'Alex Example', 'Alex', NULL, 'Example', NULL, NULL,
                    'Alex', 'Example', ?, ?)
            """,
            (created, modified),
        )
        conn.execute("INSERT INTO ZABCDPHONENUMBER VALUES (1, 1, NULL, '+1 (555) 010-0001', '_$!<Mobile>!$_', 1, 0)")
        conn.execute("INSERT INTO ZABCDEMAILADDRESS VALUES (1, 1, NULL, 'alex@example.test', '_$!<Home>!$_', 1, 0)")
        conn.commit()
    finally:
        conn.close()


def test_client_lists_chats_and_enriches_contacts(tmp_path):
    messages_db = tmp_path / "chat.db"
    contacts_db = tmp_path / "AddressBook-v22.abcddb"
    make_messages_db(messages_db)
    make_contacts_db(contacts_db)

    client = IMessageClient(messages_db_path=messages_db, contacts_db_paths=[contacts_db])
    chats = client.chats()

    assert chats[0].id == 1
    assert chats[0].name == "Alex Example"
    assert chats[0].contact_name == "Alex Example"
    assert chats[0].account_login == "me@example.test"
    assert chats[0].participants == ["+15550100001"]


def test_client_enrichment_prefers_specific_contact_over_aggregate_record(tmp_path):
    messages_db = tmp_path / "chat.db"
    contacts_db = tmp_path / "AddressBook-v22.abcddb"
    make_messages_db(messages_db)
    make_contacts_db(contacts_db)
    conn = sqlite3.connect(contacts_db)
    try:
        conn.execute(
            """
            INSERT INTO ZABCDRECORD
            VALUES (2, 'Aggregate Person', 'Aggregate Person', NULL, NULL, NULL, NULL,
                    'Aggregate', 'Person', NULL, NULL)
            """
        )
        conn.execute("INSERT INTO ZABCDPHONENUMBER VALUES (2, 2, NULL, '+1 555 010 0001', '_$!<Mobile>!$_', 1, 0)")
        conn.execute("INSERT INTO ZABCDPHONENUMBER VALUES (3, 2, NULL, '+1 555 999 0000', '_$!<Mobile>!$_', 1, 1)")
        conn.execute("INSERT INTO ZABCDPHONENUMBER VALUES (4, 2, NULL, '+1 555 999 0001', '_$!<Mobile>!$_', 1, 2)")
        conn.commit()
    finally:
        conn.close()

    client = IMessageClient(messages_db_path=messages_db, contacts_db_paths=[contacts_db])
    contact = client.resolve_contact("+15550100001")

    assert contact is not None
    assert contact.display_name == "Alex Example"


def test_contacts_reader_uses_zowner_not_z22_owner_container_link(tmp_path):
    messages_db = tmp_path / "chat.db"
    contacts_db = tmp_path / "AddressBook-v22.abcddb"
    make_messages_db(messages_db)
    make_contacts_db(contacts_db)
    conn = sqlite3.connect(contacts_db)
    try:
        conn.execute(
            """
            INSERT INTO ZABCDRECORD
            VALUES (22, 'Jordan Owner', 'Jordan', NULL, 'Owner', NULL, NULL,
                    'Jordan', 'Owner', NULL, NULL)
            """
        )
        conn.execute(
            "INSERT INTO ZABCDPHONENUMBER VALUES (22, 22, 22, '+1 (555) 010-0022', '_$!<Mobile>!$_', 1, 0)"
        )
        conn.execute(
            "INSERT INTO ZABCDPHONENUMBER VALUES (23, 1, 22, '+1 (555) 010-0001', '_$!<Mobile>!$_', 1, 0)"
        )
        conn.commit()
    finally:
        conn.close()

    client = IMessageClient(messages_db_path=messages_db, contacts_db_paths=[contacts_db])
    owner_contact = [contact for contact in client.contacts() if contact.display_name == "Jordan Owner"][0]

    assert [phone.value for phone in owner_contact.phones] == ["+1 (555) 010-0022"]


def test_client_enrichment_prefers_cleaner_name_for_duplicate_same_handle(tmp_path):
    messages_db = tmp_path / "chat.db"
    contacts_db = tmp_path / "AddressBook-v22.abcddb"
    make_messages_db(messages_db)
    make_contacts_db(contacts_db)
    conn = sqlite3.connect(contacts_db)
    try:
        conn.execute(
            """
            INSERT INTO ZABCDRECORD
            VALUES (2, 'Annotated Example', 'Annotated', NULL, 'Example', NULL, NULL,
                    'Clean', 'Example', NULL, NULL)
            """
        )
        conn.execute(
            """
            INSERT INTO ZABCDRECORD
            VALUES (3, 'Clean Example', 'Clean', NULL, 'Example', NULL, NULL,
                    'Clean', 'Example', NULL, NULL)
            """
        )
        conn.execute("INSERT INTO ZABCDPHONENUMBER VALUES (2, 2, NULL, '+1 555 010 0031', '_$!<Mobile>!$_', 1, 0)")
        conn.execute("INSERT INTO ZABCDPHONENUMBER VALUES (3, 3, NULL, '+15550100031', '_$!<Mobile>!$_', 1, 0)")
        conn.commit()
    finally:
        conn.close()

    client = IMessageClient(messages_db_path=messages_db, contacts_db_paths=[contacts_db])

    assert client.resolve_contact("+15550100031").display_name == "Clean Example"


def test_client_reads_messages_and_contacts_with_timestamps(tmp_path):
    messages_db = tmp_path / "chat.db"
    contacts_db = tmp_path / "AddressBook-v22.abcddb"
    make_messages_db(messages_db)
    make_contacts_db(contacts_db)

    client = IMessageClient(messages_db_path=messages_db, contacts_db_paths=[contacts_db])
    messages = client.messages(chat_id=1, limit=10)
    contacts = client.contacts()

    assert messages[0].text == "hello"
    assert messages[0].sender_name == "Alex Example"
    assert messages[0].chat_identifier == "+15550100001"
    assert contacts[0].created_at.year == 2026
    assert contacts[0].modified_at.day == 2


def test_client_send_dry_run_uses_chat_id_target(tmp_path):
    messages_db = tmp_path / "chat.db"
    make_messages_db(messages_db)

    client = IMessageClient(messages_db_path=messages_db, contacts_db_paths=[], home=tmp_path)
    result = client.send(chat_id=1, text="hello from test", dry_run=True)

    assert result.dry_run is True
    assert result.recipient == "iMessage;-;+15550100001"


def test_client_live_send_verifies_inserted_row(tmp_path, monkeypatch):
    messages_db = tmp_path / "chat.db"
    make_messages_db(messages_db)

    def fake_run(cmd, input, text, capture_output, timeout, check, env):
        assert cmd[:6] == ["osascript", "-", "iMessage;-;+15550100001", "sent text", "auto", "1"]
        conn = sqlite3.connect(messages_db)
        try:
            conn.execute(
                """
                INSERT INTO message
                VALUES (2, 'sent-guid', 'sent text', NULL, NULL, NULL, 'iMessage', ?, NULL, NULL,
                        1, 1, NULL, NULL, NULL, NULL, 'me@example.test')
                """,
                (apple_ns(datetime.now(timezone.utc)),),
            )
            conn.execute("INSERT INTO chat_message_join VALUES (1, 2)")
            conn.commit()
        finally:
            conn.close()
        return subprocess.CompletedProcess(cmd, 0, stdout="sent\n", stderr="")

    monkeypatch.setattr("imessage_wrapper.client.subprocess.run", fake_run)
    client = IMessageClient(messages_db_path=messages_db, contacts_db_paths=[], home=tmp_path)

    result = client.send(chat_id=1, text="sent text")

    assert result.sent is True
    assert result.verified is True
    assert result.message_id == 2
    assert result.message_guid == "sent-guid"

from __future__ import annotations

import sqlite3
import subprocess
from datetime import datetime, timezone

import pytest

from imessage_wrapper import IMessageClient
import imessage_wrapper.core as core
from imessage_wrapper.contacts_writer import ContactUpdatePayload, ContactsWriter
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


def add_send_status_columns(path):
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            ALTER TABLE message ADD COLUMN is_sent INTEGER;
            ALTER TABLE message ADD COLUMN is_delivered INTEGER;
            ALTER TABLE message ADD COLUMN is_finished INTEGER;
            ALTER TABLE message ADD COLUMN error INTEGER;
            """
        )
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


def test_client_search_chats_matches_accent_normalized_name(tmp_path):
    messages_db = tmp_path / "chat.db"
    make_messages_db(messages_db)
    conn = sqlite3.connect(messages_db)
    try:
        conn.execute("UPDATE chat SET display_name = 'Émile Example' WHERE ROWID = 1")
        conn.commit()
    finally:
        conn.close()

    client = IMessageClient(messages_db_path=messages_db, contacts_db_paths=[])
    chats = client.search_chats("emile")

    assert [chat.display_name for chat in chats] == ["Émile Example"]


def test_client_search_chats_matches_enriched_contact_name(tmp_path):
    messages_db = tmp_path / "chat.db"
    contacts_db = tmp_path / "AddressBook-v22.abcddb"
    make_messages_db(messages_db)
    make_contacts_db(contacts_db)

    client = IMessageClient(messages_db_path=messages_db, contacts_db_paths=[contacts_db])
    chats = client.search_chats("Alex Example")

    assert [chat.id for chat in chats] == [1]
    assert chats[0].name == "Alex Example"


def test_client_search_chats_scores_beyond_requested_limit(tmp_path):
    messages_db = tmp_path / "chat.db"
    make_messages_db(messages_db)
    conn = sqlite3.connect(messages_db)
    try:
        older = apple_ns(datetime(2026, 5, 1, 12, tzinfo=timezone.utc))
        newer = apple_ns(datetime(2026, 5, 2, 12, tzinfo=timezone.utc))
        conn.execute("UPDATE chat SET display_name = 'Alex' WHERE ROWID = 1")
        conn.execute("UPDATE message SET date = ? WHERE ROWID = 1", (older,))
        conn.execute("INSERT INTO handle VALUES (2, '+15550100002', 'iMessage', '+1 (555) 010-0002')")
        conn.execute(
            """
            INSERT INTO chat
            VALUES (2, 'iMessage;-;+15550100002', '+15550100002', 'Alexandria Project', 'iMessage',
                    'iMessage;+;me@example.test', 'me@example.test', '+15550100002')
            """
        )
        conn.execute("INSERT INTO chat_handle_join VALUES (2, 2)")
        conn.execute(
            """
            INSERT INTO message
            VALUES (2, 'msg-2', 'newer partial match', NULL, NULL, 2, 'iMessage', ?, NULL, NULL,
                    0, 1, NULL, NULL, NULL, NULL, NULL)
            """,
            (newer,),
        )
        conn.execute("INSERT INTO chat_message_join VALUES (2, 2)")
        conn.commit()
    finally:
        conn.close()

    client = IMessageClient(messages_db_path=messages_db, contacts_db_paths=[])
    chats = client.search_chats("alex", limit=1)

    assert [chat.display_name for chat in chats] == ["Alex"]


def test_client_search_chats_scores_all_prefiltered_candidates(tmp_path):
    messages_db = tmp_path / "chat.db"
    make_messages_db(messages_db)
    conn = sqlite3.connect(messages_db)
    try:
        old = apple_ns(datetime(2026, 5, 1, 12, tzinfo=timezone.utc))
        conn.execute("UPDATE chat SET display_name = 'Alex Exact' WHERE ROWID = 1")
        conn.execute("UPDATE message SET date = ? WHERE ROWID = 1", (old,))
        for index in range(2, 53):
            ts = apple_ns(datetime(2026, 5, 2, 12, index % 60, tzinfo=timezone.utc))
            phone = f"+1555010{index:04d}"
            conn.execute("INSERT INTO handle VALUES (?, ?, 'iMessage', ?)", (index, phone, phone))
            conn.execute(
                """
                INSERT INTO chat
                VALUES (?, ?, ?, ?, 'iMessage',
                        'iMessage;+;me@example.test', 'me@example.test', ?)
                """,
                (index, f"iMessage;-;{phone}", phone, f"Alex Exact Project {index:02d}", phone),
            )
            conn.execute("INSERT INTO chat_handle_join VALUES (?, ?)", (index, index))
            conn.execute(
                """
                INSERT INTO message
                VALUES (?, ?, 'newer partial match', NULL, NULL, ?, 'iMessage', ?, NULL, NULL,
                        0, 1, NULL, NULL, NULL, NULL, NULL)
                """,
                (index, f"msg-{index}", index, ts),
            )
            conn.execute("INSERT INTO chat_message_join VALUES (?, ?)", (index, index))
        conn.commit()
    finally:
        conn.close()

    client = IMessageClient(messages_db_path=messages_db, contacts_db_paths=[])
    chats = client.search_chats("alex exact", limit=1)

    assert [chat.display_name for chat in chats] == ["Alex Exact"]


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


def test_client_sorts_contacts_by_most_recently_added(tmp_path):
    messages_db = tmp_path / "chat.db"
    contacts_db = tmp_path / "AddressBook-v22.abcddb"
    make_messages_db(messages_db)
    make_contacts_db(contacts_db)
    conn = sqlite3.connect(contacts_db)
    try:
        newer = apple_seconds(datetime(2026, 5, 1, tzinfo=timezone.utc))
        conn.execute(
            """
            INSERT INTO ZABCDRECORD
            VALUES (2, 'Zoe Recent', 'Zoe', NULL, 'Recent', NULL, NULL,
                    'Zoe', 'Recent', ?, ?)
            """,
            (newer, newer),
        )
        conn.execute(
            """
            INSERT INTO ZABCDRECORD
            VALUES (3, 'Aaron Undated', 'Aaron', NULL, 'Undated', NULL, NULL,
                    'Aaron', 'Undated', NULL, NULL)
            """
        )
        conn.commit()
    finally:
        conn.close()

    client = IMessageClient(messages_db_path=messages_db, contacts_db_paths=[contacts_db])

    assert [contact.display_name for contact in client.contacts(sort="recent")] == [
        "Zoe Recent",
        "Alex Example",
        "Aaron Undated",
    ]
    assert [contact.display_name for contact in client.contacts(sort="recent", limit=1, offset=1)] == [
        "Alex Example"
    ]
    assert [contact.display_name for contact in client.iter_contacts(page_size=1, sort="recent")] == [
        "Zoe Recent",
        "Alex Example",
        "Aaron Undated",
    ]
    assert [contact.display_name for contact in client.contacts()] == [
        "Alex Example",
        "Zoe Recent",
        "Aaron Undated",
    ]


def test_client_rejects_invalid_search_and_contact_bounds(tmp_path):
    messages_db = tmp_path / "chat.db"
    contacts_db = tmp_path / "AddressBook-v22.abcddb"
    make_messages_db(messages_db)
    make_contacts_db(contacts_db)

    client = IMessageClient(messages_db_path=messages_db, contacts_db_paths=[contacts_db])

    with pytest.raises(ValueError, match="limit must be >= 1"):
        client.search_chats("alex", limit=0)
    with pytest.raises(ValueError, match="limit must be >= 1"):
        client.search_messages("hello", limit=0)
    with pytest.raises(ValueError, match="limit must be >= 1"):
        client.search_contacts("alex", limit=0)
    with pytest.raises(ValueError, match="offset must be >= 0"):
        client.contacts(offset=-1)
    with pytest.raises(ValueError, match="sort must be 'name' or 'recent'"):
        client.contacts(sort="oldest")


def test_client_reads_attributed_body_when_text_column_is_empty(tmp_path, monkeypatch):
    messages_db = tmp_path / "chat.db"
    make_messages_db(messages_db)
    conn = sqlite3.connect(messages_db)
    try:
        conn.execute("UPDATE message SET text = NULL, attributedBody = ? WHERE ROWID = 1", (b"streamtyped fixture",))
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(core, "_decode_attributed_body_text_with_foundation", lambda value: "clean attributed body")

    client = IMessageClient(messages_db_path=messages_db, contacts_db_paths=[])
    messages = client.messages(chat_id=1, limit=10)

    assert messages[0].text == "clean attributed body"


def test_client_search_messages_finds_attributed_body_text(tmp_path, monkeypatch):
    messages_db = tmp_path / "chat.db"
    make_messages_db(messages_db)
    conn = sqlite3.connect(messages_db)
    try:
        conn.execute("UPDATE message SET text = NULL, attributedBody = ? WHERE ROWID = 1", (b"streamtyped fixture",))
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(core, "_decode_attributed_body_text_with_foundation", lambda value: "clean attributed body")

    client = IMessageClient(messages_db_path=messages_db, contacts_db_paths=[])
    messages = client.search_messages("attributed body")

    assert [message.text for message in messages] == ["clean attributed body"]


def test_client_reads_and_searches_subject_when_text_is_empty(tmp_path):
    messages_db = tmp_path / "chat.db"
    make_messages_db(messages_db)
    conn = sqlite3.connect(messages_db)
    try:
        conn.execute("UPDATE message SET text = '', subject = 'subject only text' WHERE ROWID = 1")
        conn.commit()
    finally:
        conn.close()

    client = IMessageClient(messages_db_path=messages_db, contacts_db_paths=[])

    assert [message.text for message in client.messages(chat_id=1)] == ["subject only text"]
    assert [message.text for message in client.search_messages("subject only")] == ["subject only text"]


def test_client_send_dry_run_uses_chat_id_target(tmp_path):
    messages_db = tmp_path / "chat.db"
    make_messages_db(messages_db)

    client = IMessageClient(messages_db_path=messages_db, contacts_db_paths=[], home=tmp_path)
    result = client.send(chat_id=1, text="hello from test", dry_run=True)

    assert result.dry_run is True
    assert result.recipient == "iMessage;-;+15550100001"


def test_client_send_to_contact_email_preserves_requested_endpoint(tmp_path):
    messages_db = tmp_path / "chat.db"
    contacts_db = tmp_path / "AddressBook-v22.abcddb"
    make_messages_db(messages_db)
    make_contacts_db(contacts_db)

    client = IMessageClient(messages_db_path=messages_db, contacts_db_paths=[contacts_db], home=tmp_path)
    result = client.send(to="alex@example.test", text="hello from test", dry_run=True)

    assert result.recipient == "alex@example.test"


def test_client_update_contact_preserves_omitted_fields(tmp_path, monkeypatch):
    captured = {}

    class FakeContactsWriter:
        def update_contact(self, contact_id, payload):
            captured["contact_id"] = contact_id
            captured["payload"] = payload
            return "contact-1"

    monkeypatch.setattr("imessage_wrapper.client.ContactsWriter", FakeContactsWriter)
    client = IMessageClient(messages_db_path=tmp_path / "chat.db", contacts_db_paths=[], home=tmp_path)

    result = client.update_contact("contact-1", first_name="Updated", phones=[])

    assert result == "contact-1"
    assert captured["contact_id"] == "contact-1"
    assert captured["payload"].first_name == "Updated"
    assert captured["payload"].last_name is None
    assert captured["payload"].emails is None
    assert captured["payload"].phones == ()


def test_contacts_writer_update_payload_only_mutates_provided_fields():
    class FakeContacts:
        CNLabelHome = "home"
        CNLabelPhoneNumberMobile = "mobile"

        class CNPhoneNumber:
            @staticmethod
            def phoneNumberWithStringValue_(value):
                return f"phone:{value}"

        class CNLabeledValue:
            @staticmethod
            def labeledValueWithLabel_value_(label, value):
                return (label, value)

    class MutableContact:
        def __init__(self):
            self.calls = []

        def setGivenName_(self, value):
            self.calls.append(("first_name", value))

        def setMiddleName_(self, value):
            self.calls.append(("middle_name", value))

        def setFamilyName_(self, value):
            self.calls.append(("last_name", value))

        def setNickname_(self, value):
            self.calls.append(("nickname", value))

        def setOrganizationName_(self, value):
            self.calls.append(("organization", value))

        def setPhoneNumbers_(self, value):
            self.calls.append(("phones", value))

        def setEmailAddresses_(self, value):
            self.calls.append(("emails", value))

    contact = MutableContact()
    ContactsWriter()._apply_update_payload(contact, ContactUpdatePayload(first_name="Updated"), FakeContacts)

    assert contact.calls == [("first_name", "Updated")]


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
    assert result.delivery_status == "recorded"
    assert result.message_id == 2
    assert result.message_guid == "sent-guid"


def test_client_live_send_does_not_verify_preexisting_same_text_row(tmp_path, monkeypatch):
    messages_db = tmp_path / "chat.db"
    make_messages_db(messages_db)
    conn = sqlite3.connect(messages_db)
    try:
        conn.execute(
            """
            INSERT INTO message
            VALUES (2, 'old-guid', 'repeat text', NULL, NULL, NULL, 'iMessage', ?, NULL, NULL,
                    1, 1, NULL, NULL, NULL, NULL, 'me@example.test')
            """,
            (apple_ns(datetime.now(timezone.utc)),),
        )
        conn.execute("INSERT INTO chat_message_join VALUES (1, 2)")
        conn.commit()
    finally:
        conn.close()

    def fake_run(cmd, input, text, capture_output, timeout, check, env):
        return subprocess.CompletedProcess(cmd, 0, stdout="sent\n", stderr="")

    ticks = iter([0.0, 0.0, 11.0])
    monkeypatch.setattr("imessage_wrapper.client.subprocess.run", fake_run)
    monkeypatch.setattr("imessage_wrapper.client.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr("imessage_wrapper.client.time.sleep", lambda _: None)
    client = IMessageClient(messages_db_path=messages_db, contacts_db_paths=[], home=tmp_path)

    result = client.send(chat_id=1, text="repeat text")

    assert result.sent is True
    assert result.verified is False
    assert result.message_id is None


def test_client_live_send_verifies_attributed_body_text(tmp_path, monkeypatch):
    messages_db = tmp_path / "chat.db"
    make_messages_db(messages_db)
    payload = b"streamtyped fixture"

    def fake_run(cmd, input, text, capture_output, timeout, check, env):
        conn = sqlite3.connect(messages_db)
        try:
            conn.execute(
                """
                INSERT INTO message
                VALUES (2, 'sent-guid', NULL, NULL, ?, NULL, 'iMessage', ?, NULL, NULL,
                        1, 1, NULL, NULL, NULL, NULL, 'me@example.test')
                """,
                (payload, apple_ns(datetime.now(timezone.utc))),
            )
            conn.execute("INSERT INTO chat_message_join VALUES (1, 2)")
            conn.commit()
        finally:
            conn.close()
        return subprocess.CompletedProcess(cmd, 0, stdout="sent\n", stderr="")

    monkeypatch.setattr("imessage_wrapper.client.subprocess.run", fake_run)
    monkeypatch.setattr(core, "_decode_attributed_body_text_with_foundation", lambda value: "sent attributed text")
    client = IMessageClient(messages_db_path=messages_db, contacts_db_paths=[], home=tmp_path)

    result = client.send(chat_id=1, text="sent attributed text")

    assert result.sent is True
    assert result.verified is True
    assert result.delivery_status == "recorded"
    assert result.message_id == 2
    assert result.message_guid == "sent-guid"


def test_client_live_send_waits_for_imessage_delivery_status(tmp_path, monkeypatch):
    messages_db = tmp_path / "chat.db"
    make_messages_db(messages_db)
    add_send_status_columns(messages_db)

    def fake_run(cmd, input, text, capture_output, timeout, check, env):
        conn = sqlite3.connect(messages_db)
        try:
            conn.execute(
                """
                INSERT INTO message
                    (ROWID, guid, text, handle_id, service, date, is_from_me, is_read,
                     destination_caller_id, is_sent, is_delivered, is_finished, error)
                VALUES (2, 'pending-guid', 'pending text', NULL, 'iMessage', ?, 1, 1,
                        'me@example.test', 0, 0, 0, 0)
                """,
                (apple_ns(datetime.now(timezone.utc)),),
            )
            conn.execute("INSERT INTO chat_message_join VALUES (1, 2)")
            conn.commit()
        finally:
            conn.close()
        return subprocess.CompletedProcess(cmd, 0, stdout="sent\n", stderr="")

    ticks = iter([0.0, 0.0, 11.0])
    monkeypatch.setattr("imessage_wrapper.client.subprocess.run", fake_run)
    monkeypatch.setattr("imessage_wrapper.client.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr("imessage_wrapper.client.time.sleep", lambda _: None)
    client = IMessageClient(messages_db_path=messages_db, contacts_db_paths=[], home=tmp_path)

    result = client.send(chat_id=1, text="pending text")

    assert result.sent is True
    assert result.verified is False
    assert result.delivery_status == "pending"
    assert result.message_service == "iMessage"
    assert result.message_error == 0
    assert result.message_id == 2
    assert result.message_guid == "pending-guid"
    assert result.error == "Messages did not confirm delivery before verification timed out"


def test_client_live_send_verifies_imessage_after_delivery_status_updates(tmp_path, monkeypatch):
    messages_db = tmp_path / "chat.db"
    make_messages_db(messages_db)
    add_send_status_columns(messages_db)

    def fake_run(cmd, input, text, capture_output, timeout, check, env):
        conn = sqlite3.connect(messages_db)
        try:
            conn.execute(
                """
                INSERT INTO message
                    (ROWID, guid, text, handle_id, service, date, is_from_me, is_read,
                     destination_caller_id, is_sent, is_delivered, is_finished, error)
                VALUES (2, 'delivered-guid', 'delivered text', NULL, 'RCS', ?, 1, 1,
                        'me@example.test', 1, 0, 0, 0)
                """,
                (apple_ns(datetime.now(timezone.utc)),),
            )
            conn.execute("INSERT INTO chat_message_join VALUES (1, 2)")
            conn.commit()
        finally:
            conn.close()
        return subprocess.CompletedProcess(cmd, 0, stdout="sent\n", stderr="")

    def mark_delivered(_):
        conn = sqlite3.connect(messages_db)
        try:
            conn.execute(
                "UPDATE message SET is_delivered = 1, date_delivered = ?, is_finished = 1 WHERE ROWID = 2",
                (apple_ns(datetime.now(timezone.utc)),),
            )
            conn.commit()
        finally:
            conn.close()

    ticks = iter([0.0, 0.0, 1.0])
    monkeypatch.setattr("imessage_wrapper.client.subprocess.run", fake_run)
    monkeypatch.setattr("imessage_wrapper.client.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr("imessage_wrapper.client.time.sleep", mark_delivered)
    client = IMessageClient(messages_db_path=messages_db, contacts_db_paths=[], home=tmp_path)

    result = client.send(chat_id=1, text="delivered text")

    assert result.sent is True
    assert result.verified is True
    assert result.delivery_status == "delivered"
    assert result.message_service == "RCS"
    assert result.message_error == 0
    assert result.message_id == 2
    assert result.message_guid == "delivered-guid"
    assert result.error is None


def test_client_live_send_reports_messages_error_status(tmp_path, monkeypatch):
    messages_db = tmp_path / "chat.db"
    make_messages_db(messages_db)
    add_send_status_columns(messages_db)

    def fake_run(cmd, input, text, capture_output, timeout, check, env):
        conn = sqlite3.connect(messages_db)
        try:
            conn.execute(
                """
                INSERT INTO message
                    (ROWID, guid, text, handle_id, service, date, is_from_me, is_read,
                     destination_caller_id, is_sent, is_delivered, is_finished, error)
                VALUES (2, 'failed-guid', 'failed text', NULL, 'RCS', ?, 1, 1,
                        'me@example.test', 0, 0, 1, 42)
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

    result = client.send(chat_id=1, text="failed text")

    assert result.sent is True
    assert result.verified is False
    assert result.delivery_status == "failed"
    assert result.message_service == "RCS"
    assert result.message_error == 42
    assert result.message_id == 2
    assert result.message_guid == "failed-guid"
    assert result.error == "Messages reported send error 42"


def test_client_live_send_verifies_sms_sent_status(tmp_path, monkeypatch):
    messages_db = tmp_path / "chat.db"
    make_messages_db(messages_db)
    add_send_status_columns(messages_db)

    def fake_run(cmd, input, text, capture_output, timeout, check, env):
        conn = sqlite3.connect(messages_db)
        try:
            conn.execute(
                """
                INSERT INTO message
                    (ROWID, guid, text, handle_id, service, date, is_from_me, is_read,
                     destination_caller_id, is_sent, is_delivered, is_finished, error)
                VALUES (2, 'sms-guid', 'sms text', NULL, 'SMS', ?, 1, 1,
                        'me@example.test', 1, 0, 1, 0)
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

    result = client.send(chat_id=1, text="sms text")

    assert result.sent is True
    assert result.verified is True
    assert result.delivery_status == "sent"
    assert result.message_service == "SMS"
    assert result.message_error == 0
    assert result.message_id == 2
    assert result.message_guid == "sms-guid"
    assert result.error is None


def test_wait_for_sent_message_filters_by_direct_recipient(tmp_path):
    messages_db = tmp_path / "chat.db"
    make_messages_db(messages_db)
    sent_at = datetime(2026, 5, 1, 12, 1, tzinfo=timezone.utc)
    conn = sqlite3.connect(messages_db)
    try:
        conn.execute("INSERT INTO handle VALUES (2, '+15550100002', 'iMessage', '+1 (555) 010-0002')")
        conn.execute(
            """
            INSERT INTO chat
            VALUES (2, 'iMessage;-;+15550100002', '+15550100002', NULL, 'iMessage',
                    'iMessage;+;me@example.test', 'me@example.test', '+15550100002')
            """
        )
        conn.execute("INSERT INTO chat_handle_join VALUES (2, 2)")
        conn.execute(
            """
            INSERT INTO message
            VALUES (2, 'wrong-guid', 'collision text', NULL, NULL, 2, 'iMessage', ?, NULL, NULL,
                    1, 1, NULL, NULL, NULL, NULL, 'me@example.test')
            """,
            (apple_ns(datetime(2026, 5, 1, 12, 1, 2, tzinfo=timezone.utc)),),
        )
        conn.execute("INSERT INTO chat_message_join VALUES (2, 2)")
        conn.execute(
            """
            INSERT INTO message
            VALUES (3, 'right-guid', 'collision text', NULL, NULL, 1, 'iMessage', ?, NULL, NULL,
                    1, 1, NULL, NULL, NULL, NULL, 'me@example.test')
            """,
            (apple_ns(datetime(2026, 5, 1, 12, 1, 1, tzinfo=timezone.utc)),),
        )
        conn.execute("INSERT INTO chat_message_join VALUES (1, 3)")
        conn.commit()
    finally:
        conn.close()

    client = IMessageClient(messages_db_path=messages_db, contacts_db_paths=[], home=tmp_path)
    verification = client._wait_for_sent_message(
        text="collision text",
        chat_id=None,
        chat_identifier=None,
        chat_guid=None,
        recipient="+15550100001",
        min_rowid=0,
        sent_at=sent_at,
    )

    assert verification is not None
    assert verification.message.id == 3

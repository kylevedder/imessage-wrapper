from __future__ import annotations

import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from typing import Any

import pytest

from imessage_wrapper.core import (
    APPLE_EPOCH,
    AppleScriptIMessageSender,
    ContactsReader,
    IMessageError,
    LiveContactsReader,
    LiveIMessageReader,
    _attributed_body_contains_text,
    _extract_attributed_body_text,
    _looks_like_group_chat_guid,
    _looks_like_group_chat_identifier,
    _normalize_message_text,
)


def test_live_imessage_reader_lists_users_without_service_ambiguity(tmp_path):
    db_path = tmp_path / "chat.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE message (
                ROWID INTEGER PRIMARY KEY,
                guid TEXT,
                text TEXT,
                handle_id INTEGER,
                service TEXT,
                date INTEGER
            );
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
                service_name TEXT
            );
            CREATE TABLE chat_message_join (
                chat_id INTEGER,
                message_id INTEGER
            );
            """
        )
        conn.execute(
            "INSERT INTO handle (ROWID, id, service, uncanonicalized_id) VALUES (1, '+15550100001', 'iMessage', '+1 (555) 010-0001')"
        )
        conn.execute(
            "INSERT INTO chat (ROWID, guid, chat_identifier, display_name, service_name) VALUES (1, 'chat-1', '+15550100001', 'Alex', 'iMessage')"
        )
        conn.execute(
            "INSERT INTO message (ROWID, guid, text, handle_id, service, date) VALUES (1, 'msg-1', 'hello', 1, 'iMessage', 794990122746508544)"
        )
        conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 1)")
        conn.commit()
    finally:
        conn.close()

    result = LiveIMessageReader(db_path)._list_users_sync(limit=10)

    assert result["users"][0]["user_id"] == "+15550100001"
    assert result["users"][0]["display_name"] == "Alex"
    assert result["users"][0]["service"] == "iMessage"


def test_live_imessage_reader_searches_contacts_by_name(tmp_path):
    db_path = tmp_path / "chat.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE message (
                ROWID INTEGER PRIMARY KEY,
                guid TEXT,
                text TEXT,
                handle_id INTEGER,
                service TEXT,
                date INTEGER
            );
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
                service_name TEXT
            );
            CREATE TABLE chat_message_join (
                chat_id INTEGER,
                message_id INTEGER
            );
            """
        )
        conn.execute(
            "INSERT INTO handle (ROWID, id, service, uncanonicalized_id) VALUES (1, '+15550100001', 'iMessage', '+1 (555) 010-0001')"
        )
        conn.execute(
            "INSERT INTO chat (ROWID, guid, chat_identifier, display_name, service_name) VALUES (1, '+15550100001', '+15550100001', 'Alex', 'iMessage')"
        )
        conn.execute(
            "INSERT INTO message (ROWID, guid, text, handle_id, service, date) VALUES (1, 'msg-1', 'hello', 1, 'iMessage', 794990122746508544)"
        )
        conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 1)")
        conn.commit()
    finally:
        conn.close()

    result = LiveIMessageReader(db_path)._search_contacts_sync("alex", limit=10)

    assert result["query"] == "alex"
    assert result["contacts"][0]["display_name"] == "Alex"


def test_live_imessage_reader_searches_contacts_with_tokenized_punctuation(tmp_path):
    db_path = tmp_path / "chat.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE message (
                ROWID INTEGER PRIMARY KEY,
                guid TEXT,
                text TEXT,
                handle_id INTEGER,
                service TEXT,
                date INTEGER
            );
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
                service_name TEXT
            );
            CREATE TABLE chat_message_join (
                chat_id INTEGER,
                message_id INTEGER
            );
            """
        )
        conn.execute(
            "INSERT INTO handle (ROWID, id, service, uncanonicalized_id) VALUES (1, '+15550100002', 'iMessage', '+1 (555) 010-0002')"
        )
        conn.execute(
            """
            INSERT INTO chat (ROWID, guid, chat_identifier, display_name, service_name)
            VALUES (1, '+15550100002', '+15550100002', 'Casey Example', 'iMessage')
            """
        )
        conn.execute(
            "INSERT INTO message (ROWID, guid, text, handle_id, service, date) VALUES (1, 'msg-1', 'hi', 1, 'iMessage', 794990122746508544)"
        )
        conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 1)")
        conn.commit()
    finally:
        conn.close()

    result = LiveIMessageReader(db_path)._search_contacts_sync("casey example", limit=10)

    assert result["contacts"][0]["display_name"] == "Casey Example"
    assert result["contacts"][0]["user_id"] == "+15550100002"


def test_live_contacts_reader_searches_real_contacts_db(tmp_path):
    db_path = tmp_path / "AddressBook-v22.abcddb"
    conn = sqlite3.connect(db_path)
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
                ZSORTINGLASTNAME TEXT
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
        conn.execute(
            """
            INSERT INTO ZABCDRECORD
            (Z_PK, ZNAME, ZFIRSTNAME, ZMIDDLENAME, ZLASTNAME, ZNICKNAME, ZORGANIZATION, ZSORTINGFIRSTNAME, ZSORTINGLASTNAME)
            VALUES (1, NULL, 'Alex', NULL, 'Example', 'Al', NULL, 'Alex', 'Example')
            """
        )
        conn.execute(
            "INSERT INTO ZABCDPHONENUMBER (Z_PK, ZOWNER, Z22_OWNER, ZFULLNUMBER, ZLABEL, ZISPRIMARY, ZORDERINGINDEX) VALUES (1, 1, NULL, '+15550100001', 'mobile', 1, 0)"
        )
        conn.execute(
            "INSERT INTO ZABCDEMAILADDRESS (Z_PK, ZOWNER, Z22_OWNER, ZADDRESS, ZLABEL, ZISPRIMARY, ZORDERINGINDEX) VALUES (1, 1, NULL, 'alex@example.test', 'home', 1, 0)"
        )
        conn.commit()
    finally:
        conn.close()

    result = LiveContactsReader([db_path])._search_contacts_sync("alex", limit=10)

    assert result["contacts"][0]["display_name"] == "Alex Example"
    assert result["contacts"][0]["phone_numbers"][0]["value"] == "+15550100001"
    assert result["contacts"][0]["email_addresses"][0]["value"] == "alex@example.test"


@pytest.mark.asyncio
async def test_live_contacts_reader_lists_contacts_with_pagination(tmp_path):
    db_path = tmp_path / "AddressBook-v22.abcddb"
    conn = sqlite3.connect(db_path)
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
                ZSORTINGLASTNAME TEXT
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
        conn.execute(
            """
            INSERT INTO ZABCDRECORD
            (Z_PK, ZNAME, ZFIRSTNAME, ZMIDDLENAME, ZLASTNAME, ZNICKNAME, ZORGANIZATION, ZSORTINGFIRSTNAME, ZSORTINGLASTNAME)
            VALUES (1, NULL, 'Avery', NULL, 'Alpha', NULL, NULL, 'Avery', 'Alpha')
            """
        )
        conn.execute(
            """
            INSERT INTO ZABCDRECORD
            (Z_PK, ZNAME, ZFIRSTNAME, ZMIDDLENAME, ZLASTNAME, ZNICKNAME, ZORGANIZATION, ZSORTINGFIRSTNAME, ZSORTINGLASTNAME)
            VALUES (2, NULL, 'Example', NULL, 'Beta', NULL, 'Example Org', 'Example', 'Beta')
            """
        )
        conn.execute(
            "INSERT INTO ZABCDPHONENUMBER (Z_PK, ZOWNER, Z22_OWNER, ZFULLNUMBER, ZLABEL, ZISPRIMARY, ZORDERINGINDEX) VALUES (1, 2, NULL, '+15550109999', 'mobile', 1, 0)"
        )
        conn.commit()
    finally:
        conn.close()

    result = await LiveContactsReader([db_path]).list_contacts(limit=1, offset=1)

    assert result["mode"] == "live"
    assert result["db_paths"] == [str(db_path)]
    assert [contact["display_name"] for contact in result["contacts"]] == ["Example Beta"]
    assert result["contacts"][0]["organization"] == "Example Org"
    assert result["contacts"][0]["phone_numbers"][0]["value"] == "+15550109999"


def test_live_contacts_reader_matches_tokenized_nickname_and_phone(tmp_path):
    db_path = tmp_path / "AddressBook-v22.abcddb"
    conn = sqlite3.connect(db_path)
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
                ZSORTINGLASTNAME TEXT
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
        conn.execute(
            """
            INSERT INTO ZABCDRECORD
            (Z_PK, ZNAME, ZFIRSTNAME, ZMIDDLENAME, ZLASTNAME, ZNICKNAME, ZORGANIZATION, ZSORTINGFIRSTNAME, ZSORTINGLASTNAME)
            VALUES (1, 'Casey Example', 'Casey', NULL, NULL, 'Example', NULL, 'Casey', 'Example')
            """
        )
        conn.execute(
            "INSERT INTO ZABCDPHONENUMBER (Z_PK, ZOWNER, Z22_OWNER, ZFULLNUMBER, ZLABEL, ZISPRIMARY, ZORDERINGINDEX) VALUES (1, 1, NULL, '+1 (555) 010-0002', 'mobile', 1, 0)"
        )
        conn.execute(
            "INSERT INTO ZABCDEMAILADDRESS (Z_PK, ZOWNER, Z22_OWNER, ZADDRESS, ZLABEL, ZISPRIMARY, ZORDERINGINDEX) VALUES (1, 1, NULL, 'casey@example.test', 'home', 1, 0)"
        )
        conn.commit()
    finally:
        conn.close()

    by_name = LiveContactsReader([db_path])._search_contacts_sync("casey example", limit=10)
    by_phone = LiveContactsReader([db_path])._search_contacts_sync("5550100002", limit=10)

    assert by_name["contacts"][0]["display_name"] == "Casey Example"
    assert by_phone["contacts"][0]["phone_numbers"][0]["value"] == "+1 (555) 010-0002"


@pytest.mark.asyncio
async def test_live_contacts_reader_applies_offset_after_cross_db_merge_and_dedupe(tmp_path):
    def make_contacts_db(path, rows):
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
                    ZSORTINGLASTNAME TEXT
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
            for index, row in enumerate(rows, start=1):
                conn.execute(
                    """
                    INSERT INTO ZABCDRECORD
                    (Z_PK, ZNAME, ZFIRSTNAME, ZMIDDLENAME, ZLASTNAME, ZNICKNAME, ZORGANIZATION, ZSORTINGFIRSTNAME, ZSORTINGLASTNAME)
                    VALUES (?, NULL, ?, NULL, ?, NULL, NULL, ?, ?)
                    """,
                    (index, row["first_name"], row["last_name"], row["first_name"], row["last_name"]),
                )
                conn.execute(
                    """
                    INSERT INTO ZABCDPHONENUMBER
                    (Z_PK, ZOWNER, Z22_OWNER, ZFULLNUMBER, ZLABEL, ZISPRIMARY, ZORDERINGINDEX)
                    VALUES (?, ?, NULL, ?, 'mobile', 1, 0)
                    """,
                    (index, index, row["phone"]),
                )
            conn.commit()
        finally:
            conn.close()

    db_one = tmp_path / "AddressBook-1.abcddb"
    db_two = tmp_path / "AddressBook-2.abcddb"
    make_contacts_db(
        db_one,
        [
            {"first_name": "Alex", "last_name": "Alpha", "phone": "+15550100011"},
            {"first_name": "Dana", "last_name": "Delta", "phone": "+15550100014"},
        ],
    )
    make_contacts_db(
        db_two,
        [
            {"first_name": "Alex", "last_name": "Alpha", "phone": "+15550100011"},
            {"first_name": "Beta", "last_name": "Bravo", "phone": "+15550100013"},
        ],
    )

    result = await LiveContactsReader([db_one, db_two]).list_contacts(limit=1, offset=1)

    assert result["db_paths"] == [str(db_one), str(db_two)]
    assert [contact["display_name"] for contact in result["contacts"]] == ["Beta Bravo"]


@pytest.mark.asyncio
async def test_live_contacts_reader_keeps_same_name_contacts_without_phone_or_email(tmp_path):
    conn = sqlite3.connect(tmp_path / "AddressBook-v22.abcddb")
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
                ZSORTINGLASTNAME TEXT
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
        conn.execute(
            """
            INSERT INTO ZABCDRECORD
            (Z_PK, ZNAME, ZFIRSTNAME, ZMIDDLENAME, ZLASTNAME, ZNICKNAME, ZORGANIZATION, ZSORTINGFIRSTNAME, ZSORTINGLASTNAME)
            VALUES (1, NULL, 'Repeated', NULL, 'Name', NULL, NULL, 'Repeated', 'Name')
            """
        )
        conn.execute(
            """
            INSERT INTO ZABCDRECORD
            (Z_PK, ZNAME, ZFIRSTNAME, ZMIDDLENAME, ZLASTNAME, ZNICKNAME, ZORGANIZATION, ZSORTINGFIRSTNAME, ZSORTINGLASTNAME)
            VALUES (2, NULL, 'Repeated', NULL, 'Name', NULL, NULL, 'Repeated', 'Name')
            """
        )
        conn.commit()
    finally:
        conn.close()

    result = await LiveContactsReader([tmp_path / "AddressBook-v22.abcddb"]).list_all_contacts()

    assert [contact["record_id"] for contact in result["contacts"]] == [1, 2]
    assert [contact["display_name"] for contact in result["contacts"]] == ["Repeated Name", "Repeated Name"]


@pytest.mark.asyncio
async def test_contacts_reader_list_all_contacts_falls_back_to_paginated_list_contacts():
    class FakeContactsReader(ContactsReader):
        def __init__(self):
            self.calls = []

        async def list_contacts(self, limit: int = 5000, offset: int = 0) -> dict[str, Any]:
            self.calls.append((limit, offset))
            contacts = [
                {"record_id": 1, "display_name": "One"},
                {"record_id": 2, "display_name": "Two"},
            ]
            return {"mode": "fake", "contacts": contacts[offset:offset + limit]}

        async def search_contacts(self, query: str, limit: int = 25) -> dict[str, Any]:
            return {"mode": "fake", "contacts": []}

    reader = FakeContactsReader()

    result = await reader.list_all_contacts()

    assert reader.calls == [(5000, 0)]
    assert result == {
        "mode": "fake",
        "contacts": [
            {"record_id": 1, "display_name": "One"},
            {"record_id": 2, "display_name": "Two"},
        ],
    }


@pytest.mark.asyncio
async def test_contacts_reader_list_all_contacts_pages_until_exhausted():
    class FakeContactsReader(ContactsReader):
        def __init__(self):
            self.calls = []
            self.contacts = [{"record_id": index, "display_name": f"Contact {index}"} for index in range(5001)]

        async def list_contacts(self, limit: int = 5000, offset: int = 0) -> dict[str, Any]:
            self.calls.append((limit, offset))
            return {"mode": "fake", "contacts": self.contacts[offset:offset + limit]}

        async def search_contacts(self, query: str, limit: int = 25) -> dict[str, Any]:
            return {"mode": "fake", "contacts": []}

    reader = FakeContactsReader()

    result = await reader.list_all_contacts()

    assert reader.calls == [(5000, 0), (5000, 5000)]
    assert len(result["contacts"]) == 5001
    assert result["contacts"][0]["record_id"] == 0
    assert result["contacts"][-1]["record_id"] == 5000


@pytest.mark.asyncio
async def test_live_contacts_reader_invalidates_cached_contacts_when_db_changes(tmp_path):
    db_path = tmp_path / "AddressBook-v22.abcddb"
    conn = sqlite3.connect(db_path)
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
                ZSORTINGLASTNAME TEXT
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
            INSERT INTO ZABCDRECORD
            (Z_PK, ZNAME, ZFIRSTNAME, ZMIDDLENAME, ZLASTNAME, ZNICKNAME, ZORGANIZATION, ZSORTINGFIRSTNAME, ZSORTINGLASTNAME)
            VALUES (1, NULL, 'First', NULL, 'Contact', NULL, NULL, 'First', 'Contact');
            """
        )
        conn.commit()
    finally:
        conn.close()

    reader = LiveContactsReader([db_path])

    first = await reader.list_all_contacts()
    assert [contact["display_name"] for contact in first["contacts"]] == ["First Contact"]

    time.sleep(0.01)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO ZABCDRECORD
            (Z_PK, ZNAME, ZFIRSTNAME, ZMIDDLENAME, ZLASTNAME, ZNICKNAME, ZORGANIZATION, ZSORTINGFIRSTNAME, ZSORTINGLASTNAME)
            VALUES (2, NULL, 'Second', NULL, 'Contact', NULL, NULL, 'Second', 'Contact')
            """
        )
        conn.commit()
    finally:
        conn.close()

    second = await reader.list_all_contacts()
    assert [contact["display_name"] for contact in second["contacts"]] == ["First Contact", "Second Contact"]


def test_live_contacts_reader_matches_accented_and_apostrophe_names(tmp_path):
    db_path = tmp_path / "AddressBook-v22.abcddb"
    conn = sqlite3.connect(db_path)
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
                ZSORTINGLASTNAME TEXT
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
        conn.execute(
            """
            INSERT INTO ZABCDRECORD
            (Z_PK, ZNAME, ZFIRSTNAME, ZMIDDLENAME, ZLASTNAME, ZNICKNAME, ZORGANIZATION, ZSORTINGFIRSTNAME, ZSORTINGLASTNAME)
            VALUES (1, 'Émile Example', 'Émile', NULL, 'Example', NULL, NULL, 'Émile', 'Example')
            """
        )
        conn.execute(
            """
            INSERT INTO ZABCDRECORD
            (Z_PK, ZNAME, ZFIRSTNAME, ZMIDDLENAME, ZLASTNAME, ZNICKNAME, ZORGANIZATION, ZSORTINGFIRSTNAME, ZSORTINGLASTNAME)
            VALUES (2, 'Slash Example', 'TSlash', NULL, NULL, NULL, NULL, 'TSlash', 'Slash Example')
            """
        )
        conn.commit()
    finally:
        conn.close()

    by_accentless = LiveContactsReader([db_path])._search_contacts_sync("emile example", limit=10)
    by_slash = LiveContactsReader([db_path])._search_contacts_sync("slash example", limit=10)

    assert by_accentless["contacts"][0]["display_name"] == "Émile Example"
    assert by_slash["contacts"][0]["display_name"] == "Slash Example"


def test_live_contacts_reader_rejects_punctuation_only_query(tmp_path):
    db_path = tmp_path / "AddressBook-v22.abcddb"
    conn = sqlite3.connect(db_path)
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
                ZSORTINGLASTNAME TEXT
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
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ValueError, match="query is required"):
        LiveContactsReader([db_path])._search_contacts_sync("((()))!!!", limit=10)


def test_extract_attributed_body_text_recovers_message_text():
    payload = (
        b"\x04\x0bstreamtyped"
        b"NSAttributedString\x00NSObject\x00NSString\x01"
        b"+!Please bring the sample package today"
        b"NSDictionary\x00__kIMMessagePartAttributeName"
    )

    assert _extract_attributed_body_text(payload) == "Please bring the sample package today"


def test_extract_attributed_body_text_ignores_keyed_archive_metadata():
    payload = (
        b"\x04\x0bstreamtyped"
        b"\x00NSMutableAttributedString\x00"
        b"NSAttributedString\x00"
        b"NSObject\x00"
        b"NSMutableString\x00"
        b"NSString\x00"
        b"Archive metadata should not hide this message\x00"
        b"NSDictionary\x00"
        b"__kIMMessagePartAttributeName\x00"
        b"NSMutableData\x00"
        b"NSData\x00"
        b"[740c]bplist00\x00"
        b"X$versionY$archiverT$topX$objects\x00"
        b"NSKeyedArchiver\x00"
        b"WversionYdd-result\x00"
        b"DDScannerResult\x00"
    )

    assert _extract_attributed_body_text(payload) == "Archive metadata should not hide this message"


def test_group_chat_identifier_helpers_detect_group_targets():
    assert _looks_like_group_chat_identifier("b99df106dc964e2e9e5439c4dc7396d4") is True
    assert _looks_like_group_chat_guid("any;+;b99df106dc964e2e9e5439c4dc7396d4") is True
    assert _looks_like_group_chat_guid("iMessage;+;b99df106dc964e2e9e5439c4dc7396d4") is True
    assert _looks_like_group_chat_guid("SMS;-;b99df106dc964e2e9e5439c4dc7396d4") is True
    assert _looks_like_group_chat_identifier("+15550100003") is False
    assert _looks_like_group_chat_guid("alex@example.test") is False


def test_normalize_message_text_strips_trailing_whitespace():
    assert _normalize_message_text("hello group \n") == "hello group"


def test_normalize_message_text_normalizes_unicode():
    assert _normalize_message_text("Cafe\u0301") == _normalize_message_text("Caf\u00e9")


def test_attributed_body_contains_multiline_text():
    payload = b"\x00NSString\x01\x00first fixture line\nsecond fixture line\x00NSDictionary"
    assert _attributed_body_contains_text(payload, "first fixture line\nsecond fixture line") is True


def test_applescript_sender_uses_chat_guid_for_group_chat_and_verifies_send(tmp_path, monkeypatch):
    db_path = tmp_path / "chat.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE chat (
                ROWID INTEGER PRIMARY KEY,
                guid TEXT,
                chat_identifier TEXT
            );
            CREATE TABLE message (
                ROWID INTEGER PRIMARY KEY,
                text TEXT,
                subject TEXT,
                attributedBody BLOB,
                is_from_me INTEGER
            );
            CREATE TABLE chat_message_join (
                chat_id INTEGER,
                message_id INTEGER
            );
            """
        )
        conn.execute(
            "INSERT INTO chat (ROWID, guid, chat_identifier) VALUES (1, 'any;+;b99df106dc964e2e9e5439c4dc7396d4', 'b99df106dc964e2e9e5439c4dc7396d4')"
        )
        conn.execute("INSERT INTO message (ROWID, text, subject, attributedBody, is_from_me) VALUES (1, 'older', NULL, NULL, 1)")
        conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 1)")
        conn.commit()
    finally:
        conn.close()

    def fake_run(cmd, input, text, capture_output, timeout, check, env):
        assert cmd == ["osascript", "-", "any;+;b99df106dc964e2e9e5439c4dc7396d4", "hello group"]
        write_conn = sqlite3.connect(db_path)
        try:
            write_conn.execute("INSERT INTO message (ROWID, text, subject, attributedBody, is_from_me) VALUES (2, 'hello group', NULL, NULL, 1)")
            write_conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 2)")
            write_conn.commit()
        finally:
            write_conn.close()
        return subprocess.CompletedProcess(cmd, 0, stdout="sent\n", stderr="")

    monkeypatch.setattr("imessage_wrapper.core.subprocess.run", fake_run)

    sender = AppleScriptIMessageSender(
        timeout_seconds=5,
        db_path=db_path,
        verification_timeout_seconds=0.1,
        verification_poll_interval_seconds=0.01,
    )

    result = sender._send_sync("b99df106dc964e2e9e5439c4dc7396d4", "hello group")

    assert result["sent"] is True
    assert result["recipient"] == "b99df106dc964e2e9e5439c4dc7396d4"


def test_applescript_sender_accepts_full_group_chat_guid(tmp_path, monkeypatch):
    db_path = tmp_path / "chat.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE chat (
                ROWID INTEGER PRIMARY KEY,
                guid TEXT,
                chat_identifier TEXT
            );
            CREATE TABLE message (
                ROWID INTEGER PRIMARY KEY,
                text TEXT,
                subject TEXT,
                attributedBody BLOB,
                is_from_me INTEGER
            );
            CREATE TABLE chat_message_join (
                chat_id INTEGER,
                message_id INTEGER
            );
            """
        )
        conn.execute(
            "INSERT INTO chat (ROWID, guid, chat_identifier) VALUES (1, 'any;+;b99df106dc964e2e9e5439c4dc7396d4', 'b99df106dc964e2e9e5439c4dc7396d4')"
        )
        conn.commit()
    finally:
        conn.close()

    def fake_run(cmd, input, text, capture_output, timeout, check, env):
        assert cmd == ["osascript", "-", "any;+;b99df106dc964e2e9e5439c4dc7396d4", "hello guid"]
        write_conn = sqlite3.connect(db_path)
        try:
            write_conn.execute("INSERT INTO message (ROWID, text, subject, attributedBody, is_from_me) VALUES (1, 'hello guid', NULL, NULL, 1)")
            write_conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 1)")
            write_conn.commit()
        finally:
            write_conn.close()
        return subprocess.CompletedProcess(cmd, 0, stdout="sent\n", stderr="")

    monkeypatch.setattr("imessage_wrapper.core.subprocess.run", fake_run)

    sender = AppleScriptIMessageSender(
        timeout_seconds=5,
        db_path=db_path,
        verification_timeout_seconds=0.1,
        verification_poll_interval_seconds=0.01,
    )

    result = sender._send_sync("any;+;b99df106dc964e2e9e5439c4dc7396d4", "hello guid")

    assert result["sent"] is True


def test_applescript_sender_verifies_group_send_even_with_unexpected_stdout(tmp_path, monkeypatch):
    db_path = tmp_path / "chat.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE chat (
                ROWID INTEGER PRIMARY KEY,
                guid TEXT,
                chat_identifier TEXT
            );
            CREATE TABLE message (
                ROWID INTEGER PRIMARY KEY,
                text TEXT,
                subject TEXT,
                attributedBody BLOB,
                is_from_me INTEGER
            );
            CREATE TABLE chat_message_join (
                chat_id INTEGER,
                message_id INTEGER
            );
            """
        )
        conn.execute(
            "INSERT INTO chat (ROWID, guid, chat_identifier) VALUES (1, 'any;+;b99df106dc964e2e9e5439c4dc7396d4', 'b99df106dc964e2e9e5439c4dc7396d4')"
        )
        conn.commit()
    finally:
        conn.close()

    def fake_run(cmd, input, text, capture_output, timeout, check, env):
        write_conn = sqlite3.connect(db_path)
        try:
            write_conn.execute("INSERT INTO message (ROWID, text, subject, attributedBody, is_from_me) VALUES (1, 'hello odd stdout', NULL, NULL, 1)")
            write_conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 1)")
            write_conn.commit()
        finally:
            write_conn.close()
        return subprocess.CompletedProcess(cmd, 0, stdout="Sent\n", stderr="")

    monkeypatch.setattr("imessage_wrapper.core.subprocess.run", fake_run)

    sender = AppleScriptIMessageSender(
        timeout_seconds=5,
        db_path=db_path,
        verification_timeout_seconds=0.1,
        verification_poll_interval_seconds=0.01,
    )

    result = sender._send_sync("b99df106dc964e2e9e5439c4dc7396d4", "hello odd stdout")

    assert result["sent"] is True
    assert result["result"] == "Sent"


def test_applescript_sender_verifies_group_send_from_attributed_body_multiline(tmp_path, monkeypatch):
    db_path = tmp_path / "chat.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE chat (
                ROWID INTEGER PRIMARY KEY,
                guid TEXT,
                chat_identifier TEXT
            );
            CREATE TABLE message (
                ROWID INTEGER PRIMARY KEY,
                text TEXT,
                subject TEXT,
                attributedBody BLOB,
                is_from_me INTEGER
            );
            CREATE TABLE chat_message_join (
                chat_id INTEGER,
                message_id INTEGER
            );
            """
        )
        conn.execute(
            "INSERT INTO chat (ROWID, guid, chat_identifier) VALUES (1, 'any;+;b99df106dc964e2e9e5439c4dc7396d4', 'b99df106dc964e2e9e5439c4dc7396d4')"
        )
        conn.commit()
    finally:
        conn.close()

    sent_message = "first fixture line\nsecond fixture line"
    payload = b"\x00NSString\x01\x00first fixture line\nsecond fixture line\x00NSDictionary"

    def fake_run(cmd, input, text, capture_output, timeout, check, env):
        write_conn = sqlite3.connect(db_path)
        try:
            write_conn.execute(
                "INSERT INTO message (ROWID, text, subject, attributedBody, is_from_me) VALUES (1, NULL, NULL, ?, 1)",
                (payload,),
            )
            write_conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 1)")
            write_conn.commit()
        finally:
            write_conn.close()
        return subprocess.CompletedProcess(cmd, 0, stdout="sent\n", stderr="")

    monkeypatch.setattr("imessage_wrapper.core.subprocess.run", fake_run)

    sender = AppleScriptIMessageSender(
        timeout_seconds=5,
        db_path=db_path,
        verification_timeout_seconds=0.1,
        verification_poll_interval_seconds=0.01,
    )

    result = sender._send_sync("b99df106dc964e2e9e5439c4dc7396d4", sent_message)

    assert result["sent"] is True


def test_applescript_sender_rejects_unknown_group_chat_identifier(tmp_path, monkeypatch):
    db_path = tmp_path / "chat.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE chat (
                ROWID INTEGER PRIMARY KEY,
                guid TEXT,
                chat_identifier TEXT
            );
            """
        )
        conn.commit()
    finally:
        conn.close()

    def fake_run(*args, **kwargs):
        raise AssertionError("subprocess.run should not be called for an unknown group chat")

    monkeypatch.setattr("imessage_wrapper.core.subprocess.run", fake_run)

    sender = AppleScriptIMessageSender(timeout_seconds=5, db_path=db_path)

    with pytest.raises(IMessageError, match="No existing group chat found"):
        sender._send_sync("b99df106dc964e2e9e5439c4dc7396d4", "hello group")


def test_applescript_sender_rejects_unknown_group_chat_guid(tmp_path, monkeypatch):
    db_path = tmp_path / "chat.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE chat (
                ROWID INTEGER PRIMARY KEY,
                guid TEXT,
                chat_identifier TEXT
            );
            """
        )
        conn.commit()
    finally:
        conn.close()

    def fake_run(*args, **kwargs):
        raise AssertionError("subprocess.run should not be called for an unknown group chat guid")

    monkeypatch.setattr("imessage_wrapper.core.subprocess.run", fake_run)

    sender = AppleScriptIMessageSender(timeout_seconds=5, db_path=db_path)

    with pytest.raises(IMessageError, match="No existing group chat found"):
        sender._send_sync("any;+;b99df106dc964e2e9e5439c4dc7396d4", "hello group")


def test_applescript_sender_returns_unverified_if_group_send_never_appears(tmp_path, monkeypatch):
    db_path = tmp_path / "chat.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE chat (
                ROWID INTEGER PRIMARY KEY,
                guid TEXT,
                chat_identifier TEXT
            );
            CREATE TABLE message (
                ROWID INTEGER PRIMARY KEY,
                text TEXT,
                subject TEXT,
                attributedBody BLOB,
                is_from_me INTEGER
            );
            CREATE TABLE chat_message_join (
                chat_id INTEGER,
                message_id INTEGER
            );
            """
        )
        conn.execute(
            "INSERT INTO chat (ROWID, guid, chat_identifier) VALUES (1, 'any;+;b99df106dc964e2e9e5439c4dc7396d4', 'b99df106dc964e2e9e5439c4dc7396d4')"
        )
        conn.commit()
    finally:
        conn.close()

    def fake_run(cmd, input, text, capture_output, timeout, check, env):
        return subprocess.CompletedProcess(cmd, 0, stdout="sent\n", stderr="")

    monkeypatch.setattr("imessage_wrapper.core.subprocess.run", fake_run)

    sender = AppleScriptIMessageSender(
        timeout_seconds=5,
        db_path=db_path,
        verification_timeout_seconds=0.02,
        verification_poll_interval_seconds=0.01,
    )

    result = sender._send_sync("b99df106dc964e2e9e5439c4dc7396d4", "hello group")

    assert result["sent"] is True
    assert result["verified"] is False


def test_applescript_sender_direct_send_still_uses_recipient_handle(tmp_path, monkeypatch):
    db_path = tmp_path / "chat.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE chat (
                ROWID INTEGER PRIMARY KEY,
                guid TEXT,
                chat_identifier TEXT
            );
            """
        )
        conn.commit()
    finally:
        conn.close()

    def fake_run(cmd, input, text, capture_output, timeout, check, env):
        assert cmd == ["osascript", "-", "+15550100001", "hello direct"]
        assert "buddy targetHandle" in input
        return subprocess.CompletedProcess(cmd, 0, stdout="sent\n", stderr="")

    monkeypatch.setattr("imessage_wrapper.core.subprocess.run", fake_run)

    sender = AppleScriptIMessageSender(timeout_seconds=5, db_path=db_path)
    monkeypatch.setattr(sender, "_stage_send_attachments", lambda paths: paths)
    monkeypatch.setattr(sender, "_wait_for_send", lambda *args, **kwargs: True)
    result = sender._send_sync("+15550100001", "hello direct")

    assert result["sent"] is True


def test_applescript_sender_prefers_existing_direct_chat_for_attachments(tmp_path, monkeypatch):
    db_path = tmp_path / "chat.db"
    image_path = tmp_path / "photo.png"
    image_path.write_bytes(b"fakepng")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE chat (
                ROWID INTEGER PRIMARY KEY,
                guid TEXT,
                chat_identifier TEXT
            );
            CREATE TABLE message (
                ROWID INTEGER PRIMARY KEY,
                text TEXT,
                subject TEXT,
                attributedBody BLOB,
                is_from_me INTEGER
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
        conn.execute("INSERT INTO chat (ROWID, guid, chat_identifier) VALUES (1, 'any;-;+15550100001', '+15550100001')")
        conn.commit()
    finally:
        conn.close()

    def fake_run(cmd, input, text, capture_output, timeout, check, env):
        assert cmd == ["osascript", "-", "any;-;+15550100001", "caption", str(image_path.resolve())]
        assert "set targetChat to chat id targetChatId" in input
        return subprocess.CompletedProcess(cmd, 0, stdout="sent\n", stderr="")

    monkeypatch.setattr("imessage_wrapper.core.subprocess.run", fake_run)

    sender = AppleScriptIMessageSender(timeout_seconds=5, db_path=db_path)
    monkeypatch.setattr(sender, "_stage_send_attachments", lambda paths: paths)
    monkeypatch.setattr(sender, "_wait_for_send", lambda *args, **kwargs: True)

    result = sender._send_sync("+15550100001", "caption", [str(image_path)])

    assert result["sent"] is True


def test_applescript_sender_direct_send_accepts_image_paths(tmp_path, monkeypatch):
    db_path = tmp_path / "chat.db"
    db_path.touch()
    image_path = tmp_path / "photo.png"
    image_path.write_bytes(b"fakepng")

    def fake_run(cmd, input, text, capture_output, timeout, check, env):
        assert cmd == ["osascript", "-", "+15550100001", "", str(image_path.resolve())]
        assert "set attachmentFile to (POSIX file attachmentPath) as alias" in input
        assert 'tell application "Messages"' in input
        assert input.index("send attachmentFile to targetRef") < input.index('send outgoingText to targetRef')
        return subprocess.CompletedProcess(cmd, 0, stdout="sent\n", stderr="")

    monkeypatch.setattr("imessage_wrapper.core.subprocess.run", fake_run)

    sender = AppleScriptIMessageSender(timeout_seconds=5, db_path=db_path)
    monkeypatch.setattr(sender, "_stage_send_attachments", lambda paths: paths)
    monkeypatch.setattr(sender, "_wait_for_send", lambda *args, **kwargs: True)
    result = sender._send_sync("+15550100001", "", [str(image_path)])

    assert result["sent"] is True
    assert result["image_paths"] == [str(image_path.resolve())]


def test_applescript_sender_direct_image_send_uses_db_verification(tmp_path, monkeypatch):
    db_path = tmp_path / "chat.db"
    image_path = tmp_path / "photo.png"
    image_path.write_bytes(b"fakepng")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE handle (
                ROWID INTEGER PRIMARY KEY,
                id TEXT,
                uncanonicalized_id TEXT
            );
            CREATE TABLE chat (
                ROWID INTEGER PRIMARY KEY,
                chat_identifier TEXT
            );
            CREATE TABLE message (
                ROWID INTEGER PRIMARY KEY,
                text TEXT,
                subject TEXT,
                attributedBody BLOB,
                is_from_me INTEGER,
                handle_id INTEGER
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
        conn.execute("INSERT INTO handle (ROWID, id, uncanonicalized_id) VALUES (1, '+15550100001', '+1 (555) 010-0001')")
        conn.execute("INSERT INTO chat (ROWID, chat_identifier) VALUES (1, '+15550100001')")
        conn.commit()
    finally:
        conn.close()

    def fake_run(cmd, input, text, capture_output, timeout, check, env):
        write_conn = sqlite3.connect(db_path)
        try:
            write_conn.execute(
                "INSERT INTO message (ROWID, text, subject, attributedBody, is_from_me, handle_id) VALUES (1, NULL, NULL, NULL, 1, 1)"
            )
            write_conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 1)")
            write_conn.execute(
                "INSERT INTO attachment (ROWID, guid, filename, mime_type, total_bytes, transfer_name, uti, created_date) VALUES (1, 'att-1', ?, 'image/png', 7, 'photo.png', 'public.png', 0)",
                (str(image_path.resolve()),),
            )
            write_conn.execute("INSERT INTO message_attachment_join (message_id, attachment_id) VALUES (1, 1)")
            write_conn.commit()
        finally:
            write_conn.close()
        return subprocess.CompletedProcess(cmd, 0, stdout="not sent\n", stderr="")

    monkeypatch.setattr("imessage_wrapper.core.subprocess.run", fake_run)

    sender = AppleScriptIMessageSender(
        timeout_seconds=5,
        db_path=db_path,
        verification_timeout_seconds=0.1,
        verification_poll_interval_seconds=0.01,
    )
    monkeypatch.setattr(sender, "_stage_send_attachments", lambda paths: paths)

    result = sender._send_sync("+15550100001", "", [str(image_path)])

    assert result["sent"] is True


def test_applescript_sender_direct_send_returns_unverified_when_verification_is_unavailable(tmp_path, monkeypatch):
    db_path = tmp_path / "chat.db"
    db_path.touch()

    def fake_run(cmd, input, text, capture_output, timeout, check, env):
        return subprocess.CompletedProcess(cmd, 0, stdout="sent\n", stderr="")

    monkeypatch.setattr("imessage_wrapper.core.subprocess.run", fake_run)

    sender = AppleScriptIMessageSender(timeout_seconds=5, db_path=db_path)
    monkeypatch.setattr(sender, "_stage_send_attachments", lambda paths: paths)

    result = sender._send_sync("+15550100001", "hello direct")

    assert result["sent"] is True
    assert result["verified"] is False


def test_applescript_sender_blocks_recent_duplicate_attachment(tmp_path, monkeypatch):
    db_path = tmp_path / "chat.db"
    image_path = tmp_path / "photo.jpg"
    image_bytes = b"duplicate-image-bytes"
    image_path.write_bytes(image_bytes)
    existing_attachment = tmp_path / "existing-photo.jpg"
    existing_attachment.write_bytes(image_bytes)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE chat (
                ROWID INTEGER PRIMARY KEY,
                guid TEXT,
                chat_identifier TEXT
            );
            CREATE TABLE message (
                ROWID INTEGER PRIMARY KEY,
                text TEXT,
                subject TEXT,
                attributedBody BLOB,
                is_from_me INTEGER,
                date INTEGER
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
        now_ns = int((datetime.now(timezone.utc) - APPLE_EPOCH).total_seconds() * 1_000_000_000)
        conn.execute("INSERT INTO chat (ROWID, guid, chat_identifier) VALUES (1, 'any;-;+15550100001', '+15550100001')")
        conn.execute(
            "INSERT INTO message (ROWID, text, subject, attributedBody, is_from_me, date) VALUES (1, '', NULL, NULL, 1, ?)",
            (now_ns,),
        )
        conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 1)")
        conn.execute(
            "INSERT INTO attachment (ROWID, guid, filename, mime_type, total_bytes, transfer_name, uti, created_date) VALUES (1, 'att-1', ?, 'image/jpeg', ?, 'existing-photo.jpg', 'public.jpeg', 0)",
            (str(existing_attachment), len(image_bytes)),
        )
        conn.execute("INSERT INTO message_attachment_join (message_id, attachment_id) VALUES (1, 1)")
        conn.commit()
    finally:
        conn.close()

    sender = AppleScriptIMessageSender(timeout_seconds=5, db_path=db_path, duplicate_window_seconds=300)

    with pytest.raises(IMessageError, match="Refusing duplicate image send"):
        sender._send_sync("+15550100001", "", [str(image_path)])


def test_applescript_sender_rejects_empty_send_without_images(tmp_path):
    db_path = tmp_path / "chat.db"
    db_path.touch()
    sender = AppleScriptIMessageSender(timeout_seconds=5, db_path=db_path)

    with pytest.raises(ValueError, match="message or image_paths is required"):
        sender._send_sync("+15550100001", "", [])


def test_applescript_sender_accepts_regular_file_attachment(tmp_path, monkeypatch):
    db_path = tmp_path / "chat.db"
    db_path.touch()
    file_path = tmp_path / "notes.txt"
    file_path.write_text("not an image")

    def fake_run(cmd, input, text, capture_output, timeout, check, env):
        assert cmd == ["osascript", "-", "+15550100001", "", str(file_path.resolve())]
        return subprocess.CompletedProcess(cmd, 0, stdout="sent\n", stderr="")

    monkeypatch.setattr("imessage_wrapper.core.subprocess.run", fake_run)
    sender = AppleScriptIMessageSender(timeout_seconds=5, db_path=db_path)
    monkeypatch.setattr(sender, "_stage_send_attachments", lambda paths: paths)
    monkeypatch.setattr(sender, "_wait_for_send", lambda *args, **kwargs: True)

    result = sender._send_sync("+15550100001", "", [str(file_path)])

    assert result["sent"] is True


def test_applescript_sender_verifies_group_image_send(tmp_path, monkeypatch):
    db_path = tmp_path / "chat.db"
    image_path = tmp_path / "photo.jpg"
    image_path.write_bytes(b"jpegbytes")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE chat (
                ROWID INTEGER PRIMARY KEY,
                guid TEXT,
                chat_identifier TEXT
            );
            CREATE TABLE message (
                ROWID INTEGER PRIMARY KEY,
                text TEXT,
                subject TEXT,
                attributedBody BLOB,
                is_from_me INTEGER
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
        conn.execute(
            "INSERT INTO chat (ROWID, guid, chat_identifier) VALUES (1, 'any;+;b99df106dc964e2e9e5439c4dc7396d4', 'b99df106dc964e2e9e5439c4dc7396d4')"
        )
        conn.commit()
    finally:
        conn.close()

    def fake_run(cmd, input, text, capture_output, timeout, check, env):
        assert cmd == [
            "osascript",
            "-",
            "any;+;b99df106dc964e2e9e5439c4dc7396d4",
            "look at this",
            str(image_path.resolve()),
        ]
        write_conn = sqlite3.connect(db_path)
        try:
            write_conn.execute("INSERT INTO message (ROWID, text, subject, attributedBody, is_from_me) VALUES (1, 'look at this', NULL, NULL, 1)")
            write_conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 1)")
            write_conn.execute(
                "INSERT INTO attachment (ROWID, guid, filename, mime_type, total_bytes, transfer_name, uti, created_date) VALUES (1, 'att-1', ?, 'image/jpeg', 9, 'photo.jpg', 'public.jpeg', 0)",
                (str(image_path.resolve()),),
            )
            write_conn.execute("INSERT INTO message_attachment_join (message_id, attachment_id) VALUES (1, 1)")
            write_conn.commit()
        finally:
            write_conn.close()
        return subprocess.CompletedProcess(cmd, 0, stdout="sent\n", stderr="")

    monkeypatch.setattr("imessage_wrapper.core.subprocess.run", fake_run)

    sender = AppleScriptIMessageSender(
        timeout_seconds=5,
        db_path=db_path,
        verification_timeout_seconds=0.1,
        verification_poll_interval_seconds=0.01,
    )
    monkeypatch.setattr(sender, "_stage_send_attachments", lambda paths: paths)

    result = sender._send_sync("b99df106dc964e2e9e5439c4dc7396d4", "look at this", [str(image_path)])

    assert result["sent"] is True
    assert result["image_paths"] == [str(image_path.resolve())]


def test_applescript_sender_group_image_verification_ignores_older_same_filename(tmp_path, monkeypatch):
    db_path = tmp_path / "chat.db"
    image_path = tmp_path / "photo.jpg"
    image_path.write_bytes(b"jpegbytes")
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE chat (
                ROWID INTEGER PRIMARY KEY,
                guid TEXT,
                chat_identifier TEXT
            );
            CREATE TABLE message (
                ROWID INTEGER PRIMARY KEY,
                text TEXT,
                subject TEXT,
                attributedBody BLOB,
                is_from_me INTEGER
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
        conn.execute(
            "INSERT INTO chat (ROWID, guid, chat_identifier) VALUES (1, 'any;+;b99df106dc964e2e9e5439c4dc7396d4', 'b99df106dc964e2e9e5439c4dc7396d4')"
        )
        conn.execute("INSERT INTO message (ROWID, text, subject, attributedBody, is_from_me) VALUES (1, 'older text', NULL, NULL, 1)")
        conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 1)")
        conn.execute(
            "INSERT INTO attachment (ROWID, guid, filename, mime_type, total_bytes, transfer_name, uti, created_date) VALUES (1, 'att-older', ?, 'image/jpeg', 9, 'photo.jpg', 'public.jpeg', 0)",
            (str(image_path.resolve()),),
        )
        conn.execute("INSERT INTO message_attachment_join (message_id, attachment_id) VALUES (1, 1)")
        conn.commit()
    finally:
        conn.close()

    def fake_run(cmd, input, text, capture_output, timeout, check, env):
        write_conn = sqlite3.connect(db_path)
        try:
            write_conn.execute("INSERT INTO message (ROWID, text, subject, attributedBody, is_from_me) VALUES (2, 'new caption', NULL, NULL, 1)")
            write_conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 2)")
            write_conn.commit()
        finally:
            write_conn.close()
        return subprocess.CompletedProcess(cmd, 0, stdout="sent\n", stderr="")

    monkeypatch.setattr("imessage_wrapper.core.subprocess.run", fake_run)

    sender = AppleScriptIMessageSender(
        timeout_seconds=5,
        db_path=db_path,
        verification_timeout_seconds=0.02,
        verification_poll_interval_seconds=0.01,
    )
    monkeypatch.setattr(sender, "_stage_send_attachments", lambda paths: paths)

    result = sender._send_sync("b99df106dc964e2e9e5439c4dc7396d4", "new caption", [str(image_path)])

    assert result["sent"] is True
    assert result["verified"] is False

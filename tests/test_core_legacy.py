from __future__ import annotations

import base64
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from typing import Any

import pytest

import imessage_wrapper.core as core
from imessage_wrapper.core import (
    APPLE_EPOCH,
    AppleScriptIMessageSender,
    ContactsReader,
    IMessageError,
    LiveContactsReader,
    LiveIMessageReader,
    _attributed_body_contains_text,
    _extract_attributed_body_text,
    _extract_attributed_body_text_heuristic,
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


def test_live_imessage_reader_searches_contacts_with_accent_normalized_name(tmp_path):
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
            "INSERT INTO handle (ROWID, id, service, uncanonicalized_id) VALUES (1, '+15550100003', 'iMessage', '+1 (555) 010-0003')"
        )
        conn.execute(
            """
            INSERT INTO chat (ROWID, guid, chat_identifier, display_name, service_name)
            VALUES (1, '+15550100003', '+15550100003', 'Émile Example', 'iMessage')
            """
        )
        conn.execute(
            "INSERT INTO message (ROWID, guid, text, handle_id, service, date) VALUES (1, 'msg-1', 'hello', 1, 'iMessage', 794990122746508544)"
        )
        conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 1)")
        conn.commit()
    finally:
        conn.close()

    result = LiveIMessageReader(db_path)._search_contacts_sync("emile", limit=10)

    assert result["contacts"][0]["display_name"] == "Émile Example"


def test_live_imessage_reader_searches_all_prefiltered_candidates(tmp_path):
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
            "INSERT INTO handle (ROWID, id, service, uncanonicalized_id) VALUES (1, '+15550109999', 'iMessage', '+15550109999')"
        )
        conn.execute(
            """
            INSERT INTO chat (ROWID, guid, chat_identifier, display_name, service_name)
            VALUES (1, '+15550109999', '+15550109999', 'Alex Exact', 'iMessage')
            """
        )
        conn.execute(
            "INSERT INTO message (ROWID, guid, text, handle_id, service, date) VALUES (1, 'msg-1', 'older exact', 1, 'iMessage', 794990000000000000)"
        )
        conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (1, 1)")
        for index in range(2, 53):
            phone = f"+1555010{index:04d}"
            conn.execute(
                "INSERT INTO handle (ROWID, id, service, uncanonicalized_id) VALUES (?, ?, 'iMessage', ?)",
                (index, phone, phone),
            )
            conn.execute(
                """
                INSERT INTO chat (ROWID, guid, chat_identifier, display_name, service_name)
                VALUES (?, ?, ?, ?, 'iMessage')
                """,
                (index, phone, phone, f"Alex Exact Project {index:02d}"),
            )
            conn.execute(
                "INSERT INTO message (ROWID, guid, text, handle_id, service, date) VALUES (?, ?, 'newer partial', ?, 'iMessage', ?)",
                (index, f"msg-{index}", index, 794991000000000000 + index),
            )
            conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)", (index, index))
        conn.commit()
    finally:
        conn.close()

    result = LiveIMessageReader(db_path)._search_contacts_sync("alex exact", limit=1)

    assert [contact["display_name"] for contact in result["contacts"]] == ["Alex Exact"]


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

    by_single_token = LiveContactsReader([db_path])._search_contacts_sync("emile", limit=10)
    by_accentless = LiveContactsReader([db_path])._search_contacts_sync("emile example", limit=10)
    by_slash = LiveContactsReader([db_path])._search_contacts_sync("slash example", limit=10)

    assert by_single_token["contacts"][0]["display_name"] == "Émile Example"
    assert by_accentless["contacts"][0]["display_name"] == "Émile Example"
    assert by_slash["contacts"][0]["display_name"] == "Slash Example"


def test_live_contacts_reader_search_preserves_duplicate_bare_contacts(tmp_path):
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
            VALUES (1, 'Casey Example', 'Casey', NULL, 'Example', NULL, NULL, 'Casey', 'Example')
            """
        )
        conn.execute(
            """
            INSERT INTO ZABCDRECORD
            (Z_PK, ZNAME, ZFIRSTNAME, ZMIDDLENAME, ZLASTNAME, ZNICKNAME, ZORGANIZATION, ZSORTINGFIRSTNAME, ZSORTINGLASTNAME)
            VALUES (2, 'Casey Example', 'Casey', NULL, 'Example', NULL, NULL, 'Casey', 'Example')
            """
        )
        conn.commit()
    finally:
        conn.close()

    result = LiveContactsReader([db_path])._search_contacts_sync("casey", limit=10)

    assert [contact["record_id"] for contact in result["contacts"]] == [1, 2]


def test_live_contacts_reader_search_scores_all_prefiltered_candidates(tmp_path):
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
        for index in range(1, 52):
            conn.execute(
                """
                INSERT INTO ZABCDRECORD
                (Z_PK, ZNAME, ZFIRSTNAME, ZMIDDLENAME, ZLASTNAME, ZNICKNAME, ZORGANIZATION, ZSORTINGFIRSTNAME, ZSORTINGLASTNAME)
                VALUES (?, ?, 'Alex', NULL, ?, NULL, NULL, 'Alex', ?)
                """,
                (
                    index,
                    f"Alex Aexact Project {index:02d}",
                    f"Aexact Project {index:02d}",
                    f"Aexact Project {index:02d}",
                ),
            )
        conn.execute(
            """
            INSERT INTO ZABCDRECORD
            (Z_PK, ZNAME, ZFIRSTNAME, ZMIDDLENAME, ZLASTNAME, ZNICKNAME, ZORGANIZATION, ZSORTINGFIRSTNAME, ZSORTINGLASTNAME)
            VALUES (99, 'Alex Exact', 'Alex', NULL, 'Exact', NULL, NULL, 'Alex', 'Exact')
            """
        )
        conn.commit()
    finally:
        conn.close()

    result = LiveContactsReader([db_path])._search_contacts_sync("alex exact", limit=1)

    assert [contact["display_name"] for contact in result["contacts"]] == ["Alex Exact"]


def test_live_contacts_reader_dedupes_after_scoring_contacts(tmp_path):
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
            VALUES (1, 'Alex Shared', 'Alex', NULL, 'Aexact Project', NULL, NULL, 'Alex', 'Aexact Project')
            """
        )
        conn.execute(
            """
            INSERT INTO ZABCDRECORD
            (Z_PK, ZNAME, ZFIRSTNAME, ZMIDDLENAME, ZLASTNAME, ZNICKNAME, ZORGANIZATION, ZSORTINGFIRSTNAME, ZSORTINGLASTNAME)
            VALUES (2, 'Alex Shared', 'Alex', NULL, 'Exact', NULL, NULL, 'Alex', 'Exact')
            """
        )
        conn.execute("INSERT INTO ZABCDPHONENUMBER VALUES (1, 1, NULL, '+1 (555) 010-0001', 'mobile', 1, 0)")
        conn.execute("INSERT INTO ZABCDPHONENUMBER VALUES (2, 2, NULL, '+1 (555) 010-0001', 'mobile', 1, 0)")
        conn.commit()
    finally:
        conn.close()

    result = LiveContactsReader([db_path])._search_contacts_sync("alex exact", limit=1)

    assert [contact["record_id"] for contact in result["contacts"]] == [2]


def test_live_contacts_reader_search_dedupes_normalized_phone_formats(tmp_path):
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
            VALUES (1, 'Alex Example', 'Alex', NULL, 'Example', NULL, NULL, 'Alex', 'Example')
            """
        )
        conn.execute(
            """
            INSERT INTO ZABCDRECORD
            (Z_PK, ZNAME, ZFIRSTNAME, ZMIDDLENAME, ZLASTNAME, ZNICKNAME, ZORGANIZATION, ZSORTINGFIRSTNAME, ZSORTINGLASTNAME)
            VALUES (2, 'Alex Example', 'Alex', NULL, 'Example', NULL, NULL, 'Alex', 'Example')
            """
        )
        conn.execute("INSERT INTO ZABCDPHONENUMBER VALUES (1, 1, NULL, '+1 (555) 010-0001', 'mobile', 1, 0)")
        conn.execute("INSERT INTO ZABCDPHONENUMBER VALUES (2, 2, NULL, '+15550100001', 'mobile', 1, 0)")
        conn.commit()
    finally:
        conn.close()

    result = LiveContactsReader([db_path])._search_contacts_sync("alex", limit=10)

    assert [contact["record_id"] for contact in result["contacts"]] == [1]


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


_ATTRIBUTED_BODY_FIXTURES = [
    (
        "BAtzdHJlYW10eXBlZIHoA4QBQISEhBJOU0F0dHJpYnV0ZWRTdHJpbmcAhIQITlNPYmplY3QAhZKEhIQITlNTdHJpbmcBlIQBKydJ4oCZbGwgYnJpbmcgdGhlIHNhbXBsZSBwYWNrYWdlIHRvbmlnaHSGhAJpSQElkoSEhAxOU0RpY3Rpb25hcnkAlIQBaQCGhg==",
        "I’ll bring the sample package tonight",
    ),
    (
        "BAtzdHJlYW10eXBlZIHoA4QBQISEhBJOU0F0dHJpYnV0ZWRTdHJpbmcAhIQITlNPYmplY3QAhZKEhIQITlNTdHJpbmcBlIQBKyFodHRwczovL2V4YW1wbGUudGVzdC9yZXNlYXJjaC1raXSGhAJpSQEhkoSEhAxOU0RpY3Rpb25hcnkAlIQBaQCGhg==",
        "https://example.test/research-kit",
    ),
    (
        "BAtzdHJlYW10eXBlZIHoA4QBQISEhBJOU0F0dHJpYnV0ZWRTdHJpbmcAhIQITlNPYmplY3QAhZKEhIQITlNTdHJpbmcBlIQBK09MaWtlZCDigJxMZXQgbWUgc2VlIGlmIEkgY2FuIHJlZmVyIHlvdSB0byBhdCBsZWFzdCBvbmUgb2YgdGhlIGhhcmR3YXJlIGZvbGtz4oCdhoQCaUkBS5KEhIQMTlNEaWN0aW9uYXJ5AJSEAWkAhoY=",
        "Liked “Let me see if I can refer you to at least one of the hardware folks”",
    ),
    (
        "BAtzdHJlYW10eXBlZIHoA4QBQISEhBJOU0F0dHJpYnV0ZWRTdHJpbmcAhIQITlNPYmplY3QAhZKEhIQITlNTdHJpbmcBlIQBKx1SZWFjdGVkIPCfmIIgdG8g4oCcU2hpcCBpdOKAnYaEAmlJAReShISEDE5TRGljdGlvbmFyeQCUhAFpAIaG",
        "Reacted 😂 to “Ship it”",
    ),
]


def test_extract_attributed_body_text_uses_foundation_decoder_for_streamtyped_payloads(monkeypatch):
    expected_by_blob = {base64.b64decode(encoded): expected for encoded, expected in _ATTRIBUTED_BODY_FIXTURES}

    def fake_foundation_decode(value):
        return expected_by_blob[value]

    monkeypatch.setattr(core, "_decode_attributed_body_text_with_foundation", fake_foundation_decode)

    for blob, expected in expected_by_blob.items():
        assert blob.startswith(b"\x04\x0bstreamtyped")
        assert _extract_attributed_body_text(blob) == expected


def test_extract_attributed_body_text_decodes_streamtyped_fixture_with_real_foundation():
    try:
        symbols = core._foundation_attributed_body_symbols()
    except core.IMessageError as exc:
        pytest.skip(str(exc))
    if symbols is None:
        pytest.skip("Foundation is not available on this platform")

    for encoded, expected in _ATTRIBUTED_BODY_FIXTURES:
        assert _extract_attributed_body_text(base64.b64decode(encoded)) == expected


def test_decode_attributed_body_text_with_foundation_adapter(monkeypatch):
    class FakeData:
        @classmethod
        def dataWithBytes_length_(cls, value, length):
            return value[:length]

    class FakeAttributedString:
        def string(self):
            return "Cafe\u0301"

    class FakeUnarchiver:
        @classmethod
        def unarchiveObjectWithData_(cls, data):
            assert data == b"payload"
            return FakeAttributedString()

    monkeypatch.setattr(core, "_foundation_attributed_body_symbols", lambda: (FakeData, FakeUnarchiver))

    assert core._decode_attributed_body_text_with_foundation(b"payload") == "Café"


def test_extract_attributed_body_text_falls_back_when_foundation_returns_none(monkeypatch):
    monkeypatch.setattr(core, "_decode_attributed_body_text_with_foundation", lambda value: None)
    payload = b"NSString\x01fallback text\x00NSDictionary"

    assert _extract_attributed_body_text(payload) == "fallback text"


def test_extract_attributed_body_text_heuristic_recovers_message_text():
    payload = (
        b"\x04\x0bstreamtyped"
        b"NSAttributedString\x00NSObject\x00NSString\x01"
        b"+!Please bring the sample package today"
        b"NSDictionary\x00__kIMMessagePartAttributeName"
    )

    assert _extract_attributed_body_text_heuristic(payload) == "Please bring the sample package today"


def test_extract_attributed_body_text_heuristic_ignores_keyed_archive_metadata():
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

    assert _extract_attributed_body_text_heuristic(payload) == "Archive metadata should not hide this message"


def test_extract_attributed_body_text_heuristic_recovers_unicode_message_text():
    payload = "NSString\x01こんにちは\x00NSDictionary".encode()

    assert _extract_attributed_body_text_heuristic(payload) == "こんにちは"


def test_extract_attributed_body_text_heuristic_recovers_emoji_only_message_text():
    payload = "NSString\x01🙂🙂\x00NSDictionary".encode()

    assert _extract_attributed_body_text_heuristic(payload) == "🙂🙂"


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


def test_attributed_body_contains_multiline_text(monkeypatch):
    payload = b"\x00NSString\x01\x00first fixture line\nsecond fixture line\x00NSDictionary"
    monkeypatch.setattr(core, "_decode_attributed_body_text_with_foundation", lambda value: "first fixture line\nsecond fixture line")

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
    assert result["submitted"] is True
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
    monkeypatch.setattr(core, "_decode_attributed_body_text_with_foundation", lambda value: sent_message)

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

    assert result["submitted"] is True
    assert result["sent"] is False
    assert result["verified"] is False
    assert result["result"] == "submitted"


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


@pytest.mark.parametrize("use_chat", [False, True])
def test_applescript_sender_does_not_retry_payload_after_send_error(tmp_path, monkeypatch, use_chat):
    sender = AppleScriptIMessageSender(db_path=tmp_path / "unused.db")
    chat = {"guid": "any;+;synthetic-group", "chat_identifier": "synthetic-group"}
    monkeypatch.setattr(sender, "_resolve_group_chat", lambda _: chat if use_chat else None)
    monkeypatch.setattr(sender, "_resolve_direct_chat", lambda _: None)
    for name in (
        "_latest_chat_message_rowid", "_latest_chat_attachment_rowid",
        "_latest_direct_message_rowid", "_latest_direct_attachment_rowid",
    ):
        monkeypatch.setattr(sender, name, lambda _: 0)
    monkeypatch.setattr(sender, "_find_recent_duplicate_attachment", lambda *args: None)
    monkeypatch.setattr(sender, "_stage_send_attachments", lambda paths: paths)
    monkeypatch.setattr(sender, "_wait_for_send", lambda *args: False)
    calls = []

    def fail_after_dispatch(cmd, *, input, **kwargs):
        calls.append(cmd)
        lookup_block = input.split("try\n", 1)[1].split("end try", 1)[0]
        assert "sendPayload" not in lookup_block
        assert " to get " in lookup_block
        assert input.count("my sendPayload(") == 1
        assert input.index("my sendPayload(") > input.index("end try")
        raise subprocess.CalledProcessError(1, cmd, stderr="send failed after dispatch")

    monkeypatch.setattr(core.subprocess, "run", fail_after_dispatch)
    result = sender._send_sync("+15550100001", "only dispatch once")
    assert len(calls) == 1
    assert result["submitted"] is None
    assert result["sent"] is False
    assert result["verified"] is False
    assert result["result"] == "unconfirmed"
    assert "send failed after dispatch" in result["error"]


def _make_send_status_db(db_path, attachment_path):
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT, uncanonicalized_id TEXT);
            CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, guid TEXT, chat_identifier TEXT);
            CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
            CREATE TABLE message (
                ROWID INTEGER PRIMARY KEY, text TEXT, subject TEXT, attributedBody BLOB,
                is_from_me INTEGER, handle_id INTEGER, service TEXT, is_sent INTEGER,
                is_delivered INTEGER, date_delivered INTEGER, is_finished INTEGER, error INTEGER
            );
            CREATE TABLE attachment (
                ROWID INTEGER PRIMARY KEY, guid TEXT, filename TEXT, mime_type TEXT,
                total_bytes INTEGER, transfer_name TEXT, uti TEXT, created_date INTEGER
            );
            CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
            INSERT INTO handle VALUES (1, '+15550100001', '+15550100001');
            INSERT INTO chat VALUES (1, 'synthetic-chat', '+15550100001');
            INSERT INTO message VALUES (1, 'hello', NULL, NULL, 1, 1, 'iMessage', 0, 0, 0, 1, 0);
            INSERT INTO chat_message_join VALUES (1, 1);
            INSERT INTO message_attachment_join VALUES (1, 1);
            """
        )
        conn.execute(
            "INSERT INTO attachment VALUES (1, 'synthetic-attachment', ?, 'image/png', 7, 'photo.png', 'public.png', 0)",
            (str(attachment_path),),
        )


@pytest.mark.parametrize("target_kind", ["direct", "group"])
@pytest.mark.parametrize("attachment_only", [False, True])
@pytest.mark.parametrize(
    "service,is_sent,is_delivered,date_delivered,error,expected",
    [
        ("iMessage", 0, 0, 0, 0, False),
        ("iMessage", 1, 0, 0, 0, False),
        ("iMessage", 1, 1, 0, 0, True),
        ("RCS", 1, 0, 123, 0, True),
        ("SMS", 0, 0, 0, 0, False),
        ("SMS", 1, 0, 0, 0, True),
        ("SMS", 1, 0, 0, 42, False),
    ],
)
def test_applescript_sender_requires_confirmed_modern_rows(
    tmp_path, target_kind, attachment_only, service, is_sent, is_delivered, date_delivered, error, expected
):
    db_path = tmp_path / "chat.db"
    attachment = tmp_path / "photo.png"
    attachment.write_bytes(b"fakepng")
    _make_send_status_db(db_path, attachment)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE message SET service = ?, is_sent = ?, is_delivered = ?, date_delivered = ?, error = ?",
            (service, is_sent, is_delivered, date_delivered, error),
        )
    sender = AppleScriptIMessageSender(db_path=db_path)
    target = "synthetic-chat" if target_kind == "group" else "+15550100001"
    assert sender._send_visible(
        target_kind, target, "" if attachment_only else "hello", 0, 0,
        [str(attachment)] if attachment_only else [],
    ) is expected


def test_applescript_sender_waits_for_finished_row_to_be_delivered(tmp_path, monkeypatch):
    db_path = tmp_path / "chat.db"
    _make_send_status_db(db_path, tmp_path / "unused.png")
    sender = AppleScriptIMessageSender(db_path=db_path)
    updates = []

    def update_status(_):
        updates.append(True)
        with sqlite3.connect(db_path) as conn:
            if len(updates) == 1:
                conn.execute("UPDATE message SET is_sent = 1")
            else:
                conn.execute("UPDATE message SET is_delivered = 1")

    monkeypatch.setattr(core.time, "sleep", update_status)
    assert sender._wait_for_send("direct", "+15550100001", "hello", 0, 0, []) is True
    assert len(updates) == 2


@pytest.mark.parametrize("automation_error", ["timeout", "nonzero_exit"])
@pytest.mark.parametrize("queued", [False, True])
def test_applescript_sender_verifies_after_automation_error_without_retry(
    tmp_path, monkeypatch, automation_error, queued,
):
    db_path = tmp_path / "chat.db"
    _make_send_status_db(db_path, tmp_path / "unused.png")
    sender = AppleScriptIMessageSender(
        db_path=db_path, verification_timeout_seconds=0.02, verification_poll_interval_seconds=0.001,
    )
    calls = []

    def dispatch(cmd, **kwargs):
        calls.append(cmd)
        if queued:
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """INSERT INTO message
                       SELECT 2, 'queued text', subject, attributedBody, 1, handle_id,
                              service, 1, 1, 1, 1, 0 FROM message WHERE ROWID = 1"""
                )
                conn.execute("INSERT INTO chat_message_join VALUES (1, 2)")
        if automation_error == "timeout":
            raise subprocess.TimeoutExpired(cmd, timeout=1)
        raise subprocess.CalledProcessError(1, cmd, stderr="automation failed after possible dispatch")

    monkeypatch.setattr(core.subprocess, "run", dispatch)
    result = sender._send_sync("+15550100001", "queued text")

    assert len(calls) == 1
    assert result["submitted"] is (True if queued else None)
    assert result["sent"] is queued
    assert result["verified"] is queued
    assert result["result"] == ("sent" if queued else "unconfirmed")
    assert (result["error"] is None) is queued


@pytest.mark.parametrize("read_error", [IMessageError, sqlite3.OperationalError, OSError])
def test_applescript_sender_preserves_submission_when_verification_raises(tmp_path, monkeypatch, read_error):
    db_path = tmp_path / "chat.db"
    _make_send_status_db(db_path, tmp_path / "unused.png")
    sender = AppleScriptIMessageSender(db_path=db_path)
    calls = []

    def dispatch(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="sent\n", stderr="")

    def verification(*args):
        raise read_error("synthetic verification failure")

    monkeypatch.setattr(core.subprocess, "run", dispatch)
    monkeypatch.setattr(sender, "_wait_for_send", verification)
    result = sender._send_sync("+15550100001", "queued text")

    assert len(calls) == 1
    assert result["submitted"] is True
    assert result["sent"] is False
    assert result["verified"] is False
    assert result["result"] == "submitted"
    assert "Verification unavailable" in result["error"]


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
        assert "set targetChat to get chat id targetChatId" in input
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

    assert result["submitted"] is True
    assert result["sent"] is False
    assert result["verified"] is False
    assert result["result"] == "submitted"


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

    assert result["submitted"] is True
    assert result["sent"] is False
    assert result["verified"] is False
    assert result["result"] == "submitted"

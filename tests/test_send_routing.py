from __future__ import annotations

import sqlite3
import subprocess
from datetime import datetime, timezone

import pytest

from imessage_wrapper import IMessageClient
from imessage_wrapper import core
from test_client_api import add_send_status_columns, apple_ns, make_messages_db


PHONE = "+15550100001"
CARRIER_CHAT = f"any;-;{PHONE}"


@pytest.fixture
def sender(tmp_path, monkeypatch):
    path = tmp_path / "chat.db"
    make_messages_db(path)
    add_send_status_columns(path)

    def forbid_dispatch(*args, **kwargs):
        pytest.fail("Tests must never dispatch to Messages")

    monkeypatch.setattr(core.subprocess, "run", forbid_dispatch)
    client = IMessageClient(
        messages_db_path=path, contacts_db_paths=[], home=tmp_path, enrich_contacts=False,
    )
    return client, path


def carrier_history(path):
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE chat SET guid = ?, service_name = 'RCS', account_id = 'SMS;synthetic-account'",
            (CARRIER_CHAT,),
        )
        conn.execute("UPDATE message SET service = 'RCS'")


def outgoing_row(path, *, rowid=2, chat_id=1, service="iMessage", is_sent=0,
                 is_delivered=0, date_delivered=0, error=0):
    with sqlite3.connect(path) as conn:
        conn.execute(
            """INSERT INTO message
               (ROWID, guid, text, handle_id, service, date, is_from_me,
                is_sent, is_delivered, date_delivered, is_finished, error)
               VALUES (?, ?, 'outgoing text', 1, ?, ?, 1, ?, ?, ?, 1, ?)""",
            (rowid, f"outgoing-{rowid}", service, apple_ns(datetime.now(timezone.utc)),
             is_sent, is_delivered, date_delivered, error),
        )
        conn.execute("INSERT INTO chat_message_join VALUES (?, ?)", (chat_id, rowid))


def test_auto_phone_preserves_existing_rcs_chat(sender, monkeypatch):
    client, path = sender
    carrier_history(path)
    calls = []
    monkeypatch.setattr(client, "_run_send_applescript", lambda **kwargs: calls.append(kwargs))

    result = client.send(to=PHONE, text="outgoing text", verify=False)

    assert result.resolved_target == CARRIER_CHAT
    assert result.requested_service == "auto"
    assert result.resolved_service == "sms"
    assert result.submitted is True
    assert len(calls) == 1
    assert calls[0]["recipient"] == CARRIER_CHAT
    assert calls[0]["use_chat"] is True
    assert calls[0]["service"] == "sms"
    assert calls[0]["account_id"] == "SMS;synthetic-account"
    assert calls[0]["direct_recipient"] == PHONE


@pytest.mark.parametrize("separate_chat", [False, True])
@pytest.mark.parametrize("is_sent,error", [(0, 0), (1, 0), (0, 42)])
def test_unconfirmed_or_failed_imessage_does_not_override_rcs(sender, separate_chat, is_sent, error):
    client, path = sender
    carrier_history(path)
    if separate_chat:
        with sqlite3.connect(path) as conn:
            conn.execute(
                """INSERT INTO chat
                   SELECT 2, ?, chat_identifier, display_name, 'iMessage',
                          'iMessage;synthetic-account', account_login, last_addressed_handle
                   FROM chat WHERE ROWID = 1""",
                (f"iMessage;-;{PHONE}",),
            )
            conn.execute("INSERT INTO chat_handle_join VALUES (2, 1)")
    outgoing_row(path, chat_id=2 if separate_chat else 1, is_sent=is_sent, error=error)

    result = client.send(to=PHONE, text="next text", dry_run=True)

    assert result.resolved_target == CARRIER_CHAT
    assert result.resolved_service == "sms"
    assert result.submitted is False


@pytest.mark.parametrize("ineligible", ["group", "different_participant", "extra_participant"])
def test_auto_excludes_group_and_mismatched_participants(sender, ineligible):
    client, path = sender
    carrier_history(path)
    with sqlite3.connect(path) as conn:
        if ineligible == "group":
            conn.execute("UPDATE chat SET guid = 'any;+;synthetic-group'")
        else:
            conn.execute("INSERT INTO handle VALUES (2, '+15550100002', 'RCS', '+15550100002')")
            if ineligible == "different_participant":
                conn.execute("DELETE FROM chat_handle_join")
            conn.execute("INSERT INTO chat_handle_join VALUES (1, 2)")

    result = client.send(to=PHONE, text="next text", dry_run=True)

    assert result.resolved_target == PHONE
    assert result.resolved_service == "imessage"


def test_auto_rejects_equally_trusted_direct_chats(sender):
    client, path = sender
    carrier_history(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """INSERT INTO chat
               SELECT 2, ?, chat_identifier, display_name, service_name,
                      account_id, account_login, last_addressed_handle FROM chat WHERE ROWID = 1""",
            (f"SMS;-;{PHONE}",),
        )
        conn.execute("INSERT INTO chat_handle_join VALUES (2, 1)")
        conn.execute("INSERT INTO chat_message_join VALUES (2, 1)")

    with pytest.raises(core.IMessageError, match="Ambiguous"):
        client.send(to=PHONE, text="next text", dry_run=True)


@pytest.mark.parametrize("service", ["sms", "imessage"])
def test_explicit_service_preserves_compatible_chat_and_overrides_incompatible_chat(sender, monkeypatch, service):
    client, path = sender
    carrier_history(path)
    calls = []
    monkeypatch.setattr(client, "_run_send_applescript", lambda **kwargs: calls.append(kwargs))

    result = client.send(to=PHONE, service=service, text="next text", verify=False)

    expected_target = CARRIER_CHAT if service == "sms" else PHONE
    assert result.resolved_target == expected_target
    assert result.resolved_service == service
    assert len(calls) == 1
    assert calls[0]["recipient"] == expected_target
    assert calls[0]["use_chat"] is (service == "sms")
    assert calls[0]["service"] == service
    assert calls[0]["account_id"] == ("SMS;synthetic-account" if service == "sms" else None)


@pytest.mark.parametrize("target", [{"chat_id": 1}, {"chat_guid": CARRIER_CHAT}, {"chat_identifier": PHONE}])
def test_explicit_chat_rejects_incompatible_service(sender, target):
    client, path = sender
    carrier_history(path)
    with pytest.raises(ValueError, match="conflicts"):
        client.send(**target, service="imessage", text="next text")


def test_finished_unsent_row_keeps_polling_until_delivered(sender, monkeypatch):
    client, path = sender
    calls = []

    def dispatch(**kwargs):
        calls.append(kwargs)
        outgoing_row(path)

    updates = []

    def advance(_):
        updates.append(True)
        with sqlite3.connect(path) as conn:
            if len(updates) == 1:
                conn.execute("UPDATE message SET is_sent = 1 WHERE ROWID = 2")
            else:
                conn.execute("UPDATE message SET is_delivered = 1 WHERE ROWID = 2")

    ticks = iter([0.0, 0.0, 1.0, 2.0])
    monkeypatch.setattr(client, "_run_send_applescript", dispatch)
    monkeypatch.setattr("imessage_wrapper.client.time.sleep", advance)
    monkeypatch.setattr("imessage_wrapper.client.time.monotonic", lambda: next(ticks))

    result = client.send(to=PHONE, service="imessage", text="outgoing text")

    assert len(calls) == 1
    assert len(updates) == 2
    assert result.message_id == 2
    assert result.delivery_status == "delivered"
    assert result.submitted is True
    assert result.sent is True
    assert result.verified is True
    assert result.error is None


@pytest.mark.parametrize(
    "service,is_sent,is_delivered,date_delivered,error,status,sent,verified",
    [
        ("iMessage", 0, 0, 0, 0, "pending", False, False),
        ("iMessage", 1, 0, 0, 0, "sent", True, False),
        ("iMessage", 1, 1, 0, 0, "delivered", True, True),
        ("RCS", 1, 0, 123, 0, "delivered", True, True),
        ("SMS", 0, 0, 0, 0, "pending", False, False),
        ("SMS", 1, 0, 0, 0, "sent", True, True),
        ("SMS", 0, 0, 0, 42, "failed", False, False),
    ],
)
def test_send_status_reads_existing_attempt_without_mutation(
    sender, service, is_sent, is_delivered, date_delivered, error, status, sent, verified,
):
    client, path = sender
    outgoing_row(path, service=service, is_sent=is_sent, is_delivered=is_delivered,
                 date_delivered=date_delivered, error=error)
    before = path.read_bytes()

    result = client.send_status(2)

    assert path.read_bytes() == before
    assert result.message_id == 2
    assert result.message_guid == "outgoing-2"
    assert result.delivery_status == status
    assert result.message_service == service
    assert result.message_error == error
    assert result.submitted is True
    assert result.sent is sent
    assert result.verified is verified
    assert bool(result.error) is bool(error)


@pytest.mark.parametrize("message_id", [1, 999])
def test_send_status_rejects_incoming_or_missing_row(sender, message_id):
    client, _ = sender
    with pytest.raises(core.IMessageError, match="Outgoing message not found"):
        client.send_status(message_id)


@pytest.mark.parametrize("message_id", [True, 0, -1, "2"])
def test_send_status_rejects_invalid_rowid(sender, message_id):
    client, _ = sender
    with pytest.raises(ValueError, match="positive rowid"):
        client.send_status(message_id)


def test_stale_chat_metadata_preserves_selected_account_for_direct_send(sender, monkeypatch):
    client, path = sender
    carrier_history(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE chat SET service_name = 'iMessage'")
    calls = []

    def dispatch(cmd, *, input, **kwargs):
        calls.append((cmd, input))
        return subprocess.CompletedProcess(cmd, 0, stdout="sent\n", stderr="")

    monkeypatch.setattr(core.subprocess, "run", dispatch)
    result = client.send(to=PHONE, text="next text", verify=False)

    assert result.resolved_service == "sms"
    assert result.resolved_target == PHONE
    assert len(calls) == 1
    cmd, script = calls[0]
    assert cmd[2:8] == [PHONE, "next text", "sms", "0", "SMS;synthetic-account", PHONE]
    assert "set targetService to get account id directAccountID" in script
    assert 'if (service type of targetService) is not SMS then error "Stored Messages account does not support carrier messaging" number -1728' in script


@pytest.mark.parametrize("with_text", [False, True])
def test_attachments_cannot_be_verified_by_text_delivery(sender, monkeypatch, tmp_path, with_text):
    client, path = sender
    attachment = tmp_path / "attachment.txt"
    attachment.write_text("synthetic attachment")
    calls = []

    def dispatch(**kwargs):
        calls.append(kwargs)
        if with_text:
            outgoing_row(path, is_sent=1, is_delivered=1)

    monkeypatch.setattr(client, "_run_send_applescript", dispatch)
    result = client.send(to=PHONE, text="outgoing text" if with_text else "",
                         file_paths=[str(attachment)])

    assert len(calls) == 1
    assert result.submitted is True
    assert result.sent is False
    assert result.verified is False
    assert result.delivery_status == "attachments_unconfirmed"
    assert result.text_delivery_status == ("delivered" if with_text else None)
    assert result.error is not None
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM attachment").fetchone()[0] == 0


@pytest.mark.parametrize("date_delivered,status,verified", [(-1, "pending", False), (0, "pending", False), (123, "delivered", True)])
def test_send_status_with_only_delivery_timestamp(tmp_path, monkeypatch, date_delivered, status, verified):
    path = tmp_path / "chat.db"
    make_messages_db(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE message SET is_from_me = 1, date_delivered = ?", (date_delivered,))
    client = IMessageClient(messages_db_path=path, contacts_db_paths=[], home=tmp_path, enrich_contacts=False)
    monkeypatch.setattr(core.subprocess, "run", lambda *a, **k: pytest.fail("must not dispatch"))

    result = client.send_status(1)

    assert result.delivery_status == status
    assert result.sent is verified
    assert result.verified is verified


def test_send_status_without_any_status_columns_is_only_recorded(tmp_path, monkeypatch):
    path = tmp_path / "chat.db"
    make_messages_db(path)
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE message DROP COLUMN date_delivered")
        conn.execute("UPDATE message SET is_from_me = 1")
    client = IMessageClient(messages_db_path=path, contacts_db_paths=[], home=tmp_path, enrich_contacts=False)
    monkeypatch.setattr(core.subprocess, "run", lambda *a, **k: pytest.fail("must not dispatch"))

    result = client.send_status(1)

    assert result.delivery_status == "recorded"
    assert result.sent is False
    assert result.verified is False


def test_mixed_send_preserves_explicit_text_failure(sender, monkeypatch, tmp_path):
    client, path = sender
    attachment = tmp_path / "attachment.txt"
    attachment.write_text("synthetic attachment")
    monkeypatch.setattr(client, "_run_send_applescript", lambda **kwargs: outgoing_row(path, error=42))

    result = client.send(to=PHONE, text="outgoing text", file_paths=[str(attachment)])

    assert result.submitted is True
    assert result.sent is False
    assert result.verified is False
    assert result.delivery_status == "failed"
    assert result.text_delivery_status == "failed"
    assert result.message_error == 42
    assert result.error == "Messages reported send error 42"


def test_duplicate_explicit_chat_identifier_is_rejected_before_dispatch(sender):
    client, path = sender
    with sqlite3.connect(path) as conn:
        conn.execute(
            """INSERT INTO chat
               SELECT 2, ?, chat_identifier, display_name, 'RCS',
                      'SMS;synthetic-account', account_login, last_addressed_handle
               FROM chat WHERE ROWID = 1""",
            (CARRIER_CHAT,),
        )
        conn.execute("INSERT INTO chat_handle_join VALUES (2, 1)")

    with pytest.raises(core.IMessageError, match="Ambiguous chat identifier"):
        client.send(chat_identifier=PHONE, text="next text")


@pytest.mark.parametrize("mismatch", ["service", "group", "account"])
def test_direct_verification_rejects_same_text_from_other_route(sender, monkeypatch, mismatch):
    client, path = sender
    outgoing_row(path, service="SMS" if mismatch == "service" else "iMessage",
                 is_sent=1, is_delivered=1)
    with sqlite3.connect(path) as conn:
        if mismatch == "group":
            conn.execute("UPDATE chat SET guid = 'any;+;synthetic-group'")
        elif mismatch == "account":
            conn.execute("UPDATE chat SET account_id = 'different-account'")
    ticks = iter([0.0, 0.0, 11.0])
    monkeypatch.setattr("imessage_wrapper.client.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr("imessage_wrapper.client.time.sleep", lambda _: None)

    found = client._wait_for_sent_message(
        text="outgoing text", chat_id=None, chat_identifier=None, chat_guid=None,
        recipient=PHONE, min_rowid=1, sent_at=datetime.now(timezone.utc),
        expected_service="imessage", account_id="iMessage;+;me@example.test",
    )

    assert found is None


def test_direct_carrier_verification_accepts_rcs_with_matching_account(sender):
    client, path = sender
    carrier_history(path)
    outgoing_row(path, service="RCS", is_sent=1, is_delivered=1)

    found = client._wait_for_sent_message(
        text="outgoing text", chat_id=None, chat_identifier=None, chat_guid=None,
        recipient=PHONE, min_rowid=1, sent_at=datetime.now(timezone.utc),
        expected_service="sms", account_id="SMS;synthetic-account",
    )

    assert found is not None
    assert found.verified is True
    assert found.message.id == 2


@pytest.mark.parametrize("service", ["auto", "sms"])
def test_stale_metadata_direct_send_passes_account_and_service_to_verification(sender, monkeypatch, service):
    client, path = sender
    carrier_history(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE chat SET service_name = 'iMessage'")
    monkeypatch.setattr(client, "_run_send_applescript", lambda **kwargs: None)
    checks = []

    def verify(**kwargs):
        checks.append(kwargs)
        return None

    monkeypatch.setattr(client, "_wait_for_sent_message", verify)
    client.send(to=PHONE, text="outgoing text", service=service)

    assert len(checks) == 1
    assert checks[0]["chat_id"] is None
    assert checks[0]["recipient"] == PHONE
    assert checks[0]["account_id"] == "SMS;synthetic-account"
    assert checks[0]["expected_service"] == (None if service == "auto" else "sms")


def test_known_chat_verification_accepts_changed_carrier_service(sender, monkeypatch):
    client, path = sender
    carrier_history(path)
    monkeypatch.setattr(client, "_run_send_applescript",
                        lambda **kwargs: outgoing_row(path, service="SMS", is_sent=1))

    result = client.send(chat_id=1, text="outgoing text")

    assert result.message_id == 2
    assert result.message_service == "SMS"
    assert result.sent is True
    assert result.verified is True


@pytest.mark.parametrize("blank_service", [None, ""])
def test_blank_message_service_preserves_known_rcs_chat(sender, blank_service):
    client, path = sender
    carrier_history(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE message SET service = ?", (blank_service,))

    result = client.send(to=PHONE, text="next text", dry_run=True)

    assert result.resolved_target == CARRIER_CHAT
    assert result.resolved_service == "sms"


def test_auto_discovers_opaque_direct_chat_by_exact_participant(sender, monkeypatch):
    client, path = sender
    carrier_history(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE chat SET chat_identifier = 'opaque-direct', guid = 'any;-;opaque-direct'")
    calls = []
    monkeypatch.setattr(client, "_run_send_applescript", lambda **kwargs: calls.append(kwargs))

    result = client.send(to=PHONE, text="next text", verify=False)

    assert result.resolved_target == "any;-;opaque-direct"
    assert result.resolved_service == "sms"
    assert len(calls) == 1
    assert calls[0]["use_chat"] is True
    assert calls[0]["account_id"] == "SMS;synthetic-account"
    assert calls[0]["direct_recipient"] == PHONE


def test_explicit_service_reports_unknown_chat_service(sender):
    client, path = sender
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE chat SET service_name = NULL")

    with pytest.raises(ValueError, match="Existing chat service is unknown"):
        client.send(chat_id=1, service="sms", text="next text")


def test_recent_sent_rcs_selects_carrier_route_without_claiming_delivery(sender):
    client, path = sender
    with sqlite3.connect(path) as conn:
        conn.execute(
            """INSERT INTO chat
               SELECT 2, ?, chat_identifier, display_name, 'RCS',
                      'SMS;synthetic-account', account_login, last_addressed_handle
               FROM chat WHERE ROWID = 1""",
            (CARRIER_CHAT,),
        )
        conn.execute("INSERT INTO chat_handle_join VALUES (2, 1)")
    outgoing_row(path, chat_id=2, service="RCS", is_sent=1, is_delivered=0)

    route = client.send(to=PHONE, text="next text", dry_run=True)
    status = client.send_status(2)

    assert route.resolved_target == CARRIER_CHAT
    assert route.resolved_service == "sms"
    assert status.delivery_status == "sent"
    assert status.sent is True
    assert status.verified is False


def test_unknown_recipient_auto_keeps_actual_sms_send_evidence(sender, monkeypatch):
    client, path = sender
    recipient = "+15550100002"
    calls = []

    def dispatch(**kwargs):
        calls.append(kwargs)
        with sqlite3.connect(path) as conn:
            conn.execute("INSERT INTO handle VALUES (2, ?, 'SMS', ?)", (recipient, recipient))
            conn.execute(
                """INSERT INTO chat VALUES
                   (2, ?, ?, NULL, 'SMS', 'SMS;synthetic-account', NULL, ?)""",
                (f"SMS;-;{recipient}", recipient, recipient),
            )
            conn.execute("INSERT INTO chat_handle_join VALUES (2, 2)")
        outgoing_row(path, chat_id=2, service="SMS", is_sent=1)
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE message SET handle_id = 2 WHERE ROWID = 2")

    monkeypatch.setattr(client, "_run_send_applescript", dispatch)
    result = client.send(to=recipient, text="outgoing text")

    assert len(calls) == 1
    assert calls[0]["use_chat"] is False
    assert calls[0]["service"] == "imessage"
    assert result.requested_service == "auto"
    assert result.resolved_service == "imessage"
    assert result.message_service == "SMS"
    assert result.message_id == 2
    assert result.message_guid == "outgoing-2"
    assert result.submitted is True
    assert result.sent is True
    assert result.verified is True
    assert result.delivery_status == "sent"


@pytest.mark.parametrize("automation_error", ["timeout", "nonzero_exit"])
@pytest.mark.parametrize("observed_status", [None, "pending", "delivered", "failed"])
def test_automation_error_preserves_observed_send_state_without_retry(
    sender, monkeypatch, automation_error, observed_status,
):
    client, path = sender
    calls = []

    def dispatch(cmd, **kwargs):
        calls.append(cmd)
        if observed_status is not None:
            outgoing_row(path, is_sent=int(observed_status == "delivered"),
                         is_delivered=int(observed_status == "delivered"),
                         error=42 if observed_status == "failed" else 0)
        if automation_error == "timeout":
            raise subprocess.TimeoutExpired(cmd, timeout=1)
        raise subprocess.CalledProcessError(1, cmd, stderr="automation failed after possible dispatch")

    ticks = iter([0.0, 0.0, 11.0])
    monkeypatch.setattr(core.subprocess, "run", dispatch)
    monkeypatch.setattr("imessage_wrapper.client.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr("imessage_wrapper.client.time.sleep", lambda _: None)

    result = client.send(chat_id=1, text="outgoing text")

    assert len(calls) == 1
    assert result.submitted is (True if observed_status is not None else None)
    assert result.message_id == (2 if observed_status is not None else None)
    assert result.delivery_status == (observed_status or "unconfirmed")
    assert result.sent is (observed_status == "delivered")
    assert result.verified is (observed_status == "delivered")
    if observed_status is None:
        assert "Automation outcome unknown" in result.error
    elif observed_status == "failed":
        assert result.message_error == 42
        assert result.error == "Messages reported send error 42"


@pytest.mark.parametrize("read_error", [sqlite3.OperationalError, core.IMessageError, OSError])
def test_verification_read_error_after_submission_remains_unconfirmed(sender, monkeypatch, read_error):
    client, _ = sender
    calls = []

    def dispatch(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="sent\n", stderr="")

    def fail_verification(**kwargs):
        raise read_error("synthetic read failure")

    monkeypatch.setattr(core.subprocess, "run", dispatch)
    monkeypatch.setattr(client, "_wait_for_sent_message", fail_verification)
    result = client.send(chat_id=1, text="outgoing text")

    assert len(calls) == 1
    assert result.submitted is True
    assert result.message_id is None
    assert result.sent is False
    assert result.verified is False
    assert result.delivery_status == "unconfirmed"
    assert "Verification unavailable" in result.error


@pytest.mark.parametrize("newest_delivered", [False, True])
def test_competing_same_text_rows_are_ambiguous_without_guessed_id(sender, monkeypatch, newest_delivered):
    client, path = sender
    calls = []

    def dispatch(**kwargs):
        calls.append(kwargs)
        outgoing_row(path, rowid=2, is_sent=int(not newest_delivered),
                     is_delivered=int(not newest_delivered))
        outgoing_row(path, rowid=3, is_sent=int(newest_delivered), is_delivered=int(newest_delivered))

    monkeypatch.setattr(client, "_run_send_applescript", dispatch)
    result = client.send(chat_id=1, text="outgoing text")

    assert len(calls) == 1
    assert result.submitted is True
    assert result.sent is False
    assert result.verified is False
    assert result.delivery_status == "ambiguous"
    assert result.message_id is None
    assert result.message_guid is None
    assert "Multiple new outgoing messages match" in result.error


def test_timeout_with_ambiguous_rows_cannot_establish_submission(sender, monkeypatch):
    client, path = sender
    calls = []

    def dispatch(cmd, **kwargs):
        calls.append(cmd)
        outgoing_row(path, rowid=2, is_sent=1, is_delivered=1)
        outgoing_row(path, rowid=3)
        raise subprocess.TimeoutExpired(cmd, timeout=1)

    monkeypatch.setattr(core.subprocess, "run", dispatch)
    result = client.send(chat_id=1, text="outgoing text")

    assert len(calls) == 1
    assert result.submitted is None
    assert result.sent is False
    assert result.verified is False
    assert result.delivery_status == "ambiguous"
    assert result.message_id is None
    assert result.message_guid is None


def attach_file_record(path, attachment_path, message_id):
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO attachment VALUES (1, 'file-guid', ?, 'text/plain', 4, 'file.txt', 'public.text', 0)",
            (str(attachment_path),),
        )
        conn.execute("INSERT INTO message_attachment_join VALUES (?, 1)", (message_id,))


@pytest.mark.parametrize("message_error", [0, 42])
def test_send_status_attachment_row_stays_unconfirmed_or_failed(sender, tmp_path, message_error):
    client, path = sender
    attachment = tmp_path / "file.txt"
    attachment.write_text("file")
    outgoing_row(path, is_sent=1, is_delivered=1, error=message_error)
    attach_file_record(path, attachment, 2)

    result = client.send_status(2)

    assert result.status_scope == "message"
    assert result.message_id == 2
    assert result.sent is False
    assert result.verified is False
    assert result.delivery_status == ("failed" if message_error else "attachments_unconfirmed")
    assert result.file_paths == [str(attachment)]
    assert result.text_delivery_status == ("failed" if message_error else "delivered")


def test_send_status_text_row_does_not_certify_other_parts_of_submission(sender, tmp_path):
    client, path = sender
    outgoing_row(path, rowid=2, is_sent=1, is_delivered=1)
    outgoing_row(path, rowid=3)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE message SET text = NULL WHERE ROWID = 3")
    attach_file_record(path, tmp_path / "file.txt", 3)

    text_status = client.send_status(2)
    attachment_status = client.send_status(3)

    assert text_status.status_scope == "message"
    assert text_status.delivery_status == "delivered"
    assert text_status.verified is True
    assert text_status.file_paths == []
    assert attachment_status.status_scope == "message"
    assert attachment_status.delivery_status == "attachments_unconfirmed"
    assert attachment_status.verified is False


@pytest.mark.parametrize("missing_table", ["attachment", "message_attachment_join"])
def test_send_status_survives_missing_attachment_metadata_table(sender, missing_table):
    client, path = sender
    outgoing_row(path, is_sent=1, is_delivered=1)
    with sqlite3.connect(path) as conn:
        conn.execute(f"DROP TABLE {missing_table}")

    result = client.send_status(2)

    assert result.status_scope == "message"
    assert result.message_id == 2
    assert result.message_guid == "outgoing-2"


@pytest.mark.parametrize("requested_service", ["sms", "auto"])
def test_known_chat_retains_observed_delivery_but_flags_explicit_service_mismatch(
    sender, monkeypatch, requested_service,
):
    client, path = sender
    carrier_history(path)
    calls = []

    def dispatch(**kwargs):
        calls.append(kwargs)
        outgoing_row(path, service="iMessage", is_sent=1, is_delivered=1)

    monkeypatch.setattr(client, "_run_send_applescript", dispatch)
    result = client.send(to=PHONE, service=requested_service, text="outgoing text")

    assert len(calls) == 1
    assert calls[0]["use_chat"] is True
    assert result.message_id == 2
    assert result.message_service == "iMessage"
    assert result.delivery_status == "delivered"
    assert result.sent is True
    assert result.verified is (requested_service == "auto")
    if requested_service == "sms":
        assert result.error is not None
        assert "Requested sms, but Messages recorded iMessage" in result.error
    else:
        assert result.error is None


@pytest.mark.parametrize("prior_error", ["automation", "verification", "service_mismatch"])
def test_attachment_send_preserves_more_specific_error(sender, monkeypatch, tmp_path, prior_error):
    client, path = sender
    attachment = tmp_path / "file.txt"
    attachment.write_text("file")
    carrier_history(path)

    def dispatch(cmd, **kwargs):
        if prior_error == "automation":
            raise subprocess.TimeoutExpired(cmd, timeout=1)
        if prior_error == "service_mismatch":
            outgoing_row(path, service="iMessage", is_sent=1, is_delivered=1)
        return subprocess.CompletedProcess(cmd, 0, stdout="sent\n", stderr="")

    monkeypatch.setattr(core.subprocess, "run", dispatch)
    if prior_error == "verification":
        def fail_verification(**kwargs):
            raise sqlite3.OperationalError("synthetic read failure")
        monkeypatch.setattr(client, "_wait_for_sent_message", fail_verification)
    elif prior_error == "automation":
        ticks = iter([0.0, 0.0, 11.0])
        monkeypatch.setattr("imessage_wrapper.client.time.monotonic", lambda: next(ticks))
        monkeypatch.setattr("imessage_wrapper.client.time.sleep", lambda _: None)

    result = client.send(to=PHONE, service="sms", text="outgoing text", file_paths=[str(attachment)])

    expected_error = {
        "automation": "Automation outcome unknown",
        "verification": "Verification unavailable",
        "service_mismatch": "Requested sms, but Messages recorded iMessage",
    }[prior_error]
    assert result.delivery_status == "attachments_unconfirmed"
    assert result.verified is False
    assert expected_error in result.error


@pytest.mark.parametrize("service", ["iMessageLite", "unknown-service", "", None, "SMS", "MMS"])
@pytest.mark.parametrize("delivered", [False, True])
def test_only_known_sms_mms_services_verify_from_sent_flag(sender, service, delivered):
    client, path = sender
    outgoing_row(path, service=service, is_sent=1, is_delivered=int(delivered))
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE chat SET service_name = NULL")

    result = client.send_status(2)

    assert result.sent is True
    assert result.delivery_status == ("delivered" if delivered else "sent")
    assert result.verified is (delivered or service in {"SMS", "MMS"})


@pytest.mark.parametrize("service", ["auto", "sms"])
@pytest.mark.parametrize("options", [{"dry_run": True}, {"verify": False}])
def test_missing_history_stops_before_dispatch_even_without_verification(tmp_path, monkeypatch, service, options):
    client = IMessageClient(messages_db_path=tmp_path / "missing.db", contacts_db_paths=[],
                            home=tmp_path, enrich_contacts=False)

    def forbid_dispatch(*args, **kwargs):
        pytest.fail("Unavailable route history must never dispatch")

    monkeypatch.setattr(core.subprocess, "run", forbid_dispatch)
    with pytest.raises(core.IMessageError, match="Messages database not found"):
        client.send(to=PHONE, service=service, text="outgoing text", **options)


def test_database_open_failure_becomes_wrapper_error_before_dispatch(tmp_path, monkeypatch):
    path = tmp_path / "unreadable.db"
    path.touch()
    client = IMessageClient(messages_db_path=path, contacts_db_paths=[], home=tmp_path,
                            enrich_contacts=False)
    failure = sqlite3.OperationalError("synthetic access denied")

    def fail_connect(database_uri, **kwargs):
        assert database_uri.endswith("?mode=ro")
        assert kwargs["uri"] is True
        raise failure

    monkeypatch.setattr(sqlite3, "connect", fail_connect)
    monkeypatch.setattr(core.subprocess, "run", lambda *a, **k: pytest.fail("must not dispatch"))
    with pytest.raises(core.IMessageError, match="Cannot open Messages database read-only") as error:
        client.send(to=PHONE, text="outgoing text", dry_run=True)
    assert error.value.__cause__ is failure

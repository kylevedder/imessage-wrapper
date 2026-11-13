from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from imessage_wrapper.models import SendResult
from imessage_wrapper import MessageWrapperError


@pytest.fixture
def cli(monkeypatch):
    path = Path(__file__).parents[1] / "skills/send-imessage/scripts/send_imessage.py"
    if not path.is_file():
        pytest.skip("The skill CLI is repository tooling, not part of the installed wheel")
    spec = importlib.util.spec_from_file_location("send_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("status,verified,code", [
    ("failed", False, 1), ("pending", False, 2), ("sent", False, 2),
    ("recorded", False, 2), ("sent", True, 0), ("delivered", True, 0),
])
def test_status_command_never_dispatches(cli, monkeypatch, capsys, status, verified, code):
    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def send_status(self, message_id):
            assert message_id == 42
            return SendResult(recipient="synthetic", message_id=42,
                              delivery_status=status, verified=verified)

        def send(self, **kwargs):
            pytest.fail("status command dispatched a message")

    monkeypatch.setattr(cli, "IMessageClient", FakeClient)
    monkeypatch.setattr("sys.argv", ["send", "--status", "42", "--format", "json"])
    assert cli.main() == code
    assert f'"delivery_status": "{status}"' in capsys.readouterr().out


@pytest.mark.parametrize("extra", [["--confirm"], ["--text", "hello"], ["--file", "x"]])
def test_status_rejects_send_options(cli, monkeypatch, extra):
    monkeypatch.setattr("sys.argv", ["send", "--status", "42", *extra])
    with pytest.raises(SystemExit, match="cannot be combined"):
        cli.main()


def test_failed_confirmation_is_not_printed_as_sent(cli, monkeypatch, capsys):
    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def send(self, **kwargs):
            return SendResult(recipient="synthetic", submitted=True, sent=False,
                              delivery_status="failed", verified=False,
                              error="Messages reported send error 4")

    monkeypatch.setattr(cli, "IMessageClient", FakeClient)
    monkeypatch.setattr("sys.argv", ["send", "--to", "+15550100001", "--text", "hello", "--confirm"])
    assert cli.main() == 1
    output = capsys.readouterr().out
    assert "failed message" in output
    assert "sent message" not in output
    assert "Messages reported send error 4" in output


@pytest.mark.parametrize("lookup_error,requested_id", [
    (MessageWrapperError, 42), (ValueError, 0), (OSError, 42), (sqlite3.OperationalError, 42),
])
def test_status_lookup_error_is_unconfirmed_not_failed(cli, monkeypatch, capsys, lookup_error, requested_id):
    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def send_status(self, message_id):
            assert message_id == requested_id
            raise lookup_error("synthetic lookup failure")

        def send(self, **kwargs):
            pytest.fail("lookup failure must never dispatch")

    monkeypatch.setattr(cli, "IMessageClient", FakeClient)
    monkeypatch.setattr("sys.argv", ["send", "--status", str(requested_id), "--format", "json"])

    assert cli.main() == 2
    result = json.loads(capsys.readouterr().out)
    assert result["delivery_status"] == "unconfirmed"
    assert result["submitted"] is None
    assert result["verified"] is False
    assert result["sent"] is False
    assert result["status_scope"] == "message"
    assert "synthetic lookup failure" in result["error"]


@pytest.mark.parametrize("confirm", [False, True])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_file_body_trims_outer_newlines_for_preview_and_send(cli, monkeypatch, capsys, tmp_path, confirm, newline):
    expected = f"Hello.{newline}{newline}  Second paragraph."
    contents = f"{newline}{newline}{expected}{newline}"
    body = tmp_path / "message.txt"
    body.write_bytes(contents.encode("utf-8"))

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def send(self, **kwargs):
            assert kwargs["text"] == expected
            assert kwargs["dry_run"] is not confirm
            return SendResult(recipient="synthetic", text=kwargs["text"],
                              dry_run=kwargs["dry_run"], verified=confirm)

    monkeypatch.setattr(cli, "IMessageClient", FakeClient)
    monkeypatch.setattr("sys.argv", ["send", "--to", "+15550100001", "--text-file", str(body),
                                     "--format", "json", *(["--confirm"] if confirm else [])])
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out)["text"] == expected
    assert body.read_bytes() == contents.encode("utf-8")


@pytest.mark.parametrize("source", ["literal", "file"])
def test_explicit_whitespace_is_preserved(cli, monkeypatch, capsys, tmp_path, source):
    contents = "\r\n\r\n  Hello.\r\n\r\nSecond paragraph. \t\r\n"
    if source == "file":
        body = tmp_path / "message.txt"
        body.write_bytes(contents.encode("utf-8"))
        options = ["--text-file", str(body), "--preserve-whitespace"]
    else:
        options = ["--text", contents]

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def send(self, **kwargs):
            assert kwargs["text"] == contents
            assert kwargs["dry_run"] is True
            return SendResult(recipient="synthetic", text=kwargs["text"], dry_run=True)

    monkeypatch.setattr(cli, "IMessageClient", FakeClient)
    monkeypatch.setattr("sys.argv", ["send", "--to", "+15550100001", *options, "--format", "json"])
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out)["text"] == contents

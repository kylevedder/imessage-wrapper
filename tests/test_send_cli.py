from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from imessage_wrapper.models import SendResult


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

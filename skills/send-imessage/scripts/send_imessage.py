#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

WRAPPER_ROOT = Path(__file__).resolve().parents[3]
WRAPPER_SRC = WRAPPER_ROOT / "src"
if WRAPPER_SRC.exists():
    sys.path.insert(0, str(WRAPPER_SRC))

from imessage_wrapper import IMessageClient, MessageWrapperError, SendResult  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview or send an iMessage/SMS through Messages.app.")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--to", help="Phone number, email, or handle for a one-to-one recipient.")
    target.add_argument("--chat-id", type=int, help="Existing chat ROWID.")
    target.add_argument("--chat-identifier", help="Existing chat identifier.")
    target.add_argument("--chat-guid", help="Existing chat guid.")
    target.add_argument("--status", type=int, help="Read an existing outgoing message row ID without sending.")

    body = parser.add_mutually_exclusive_group(required=False)
    body.add_argument("--text", default="", help="Message body.")
    body.add_argument("--text-file", help="Read message body from a UTF-8 text file.")

    parser.add_argument("--file", action="append", default=[], help="Attachment path. Repeat for multiple files.")
    parser.add_argument("--service", choices=("auto", "imessage", "sms"), default="auto")
    verify_group = parser.add_mutually_exclusive_group()
    verify_group.add_argument("--verify", action="store_true", default=None, help="Verify confirmed sends.")
    verify_group.add_argument("--no-verify", action="store_false", dest="verify", help="Do not verify confirmed sends.")
    parser.add_argument("--verification-timeout", type=float, default=10.0, help="Seconds to wait for Messages DB verification.")
    parser.add_argument("--confirm", action="store_true", help="Actually send. Without this flag, dry-run only.")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--home", default=str(Path.home()))
    return parser.parse_args()


def read_text(args: argparse.Namespace) -> str:
    if args.text_file:
        return Path(args.text_file).expanduser().read_text(encoding="utf-8")
    return args.text


def main() -> int:
    args = parse_args()
    if args.status is not None:
        if args.confirm or args.text or args.text_file or args.file:
            raise SystemExit("--status cannot be combined with a message, files, or --confirm")
        try:
            result = IMessageClient(home=args.home).send_status(args.status)
        except (MessageWrapperError, ValueError, OSError, sqlite3.Error) as exc:
            result = SendResult(
                recipient="", submitted=None, verified=False,
                delivery_status="unconfirmed", status_scope="message",
                error=f"Unable to inspect message {args.status}: {exc}",
            )
        payload = result.to_dict()
        if args.format == "json":
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(f"message_id: {result.message_id}")
            print("scope: this message row only, not a multi-part submission")
            print(f"status: {result.delivery_status} service: {result.message_service}")
            print(f"sent: {result.sent} delivered: {result.delivery_status == 'delivered'}")
            print(f"verified: {result.verified} (SMS/MMS verifies sending, not receipt)")
            if result.error:
                print(f"error: {result.error}")
        return 1 if result.delivery_status == "failed" else 0 if result.verified else 2
    text = read_text(args)
    file_paths = [str(Path(path).expanduser()) for path in args.file]
    if not text.strip() and not file_paths:
        raise SystemExit("provide --text/--text-file or at least one --file")

    if args.verification_timeout < 0:
        raise SystemExit("--verification-timeout must be >= 0")

    client = IMessageClient(home=args.home, verification_timeout=args.verification_timeout)
    result = client.send(
        to=args.to,
        chat_id=args.chat_id,
        chat_identifier=args.chat_identifier,
        chat_guid=args.chat_guid,
        text=text,
        file_paths=file_paths,
        service=args.service,
        verify=args.verify,
        dry_run=not args.confirm,
    )
    payload = result.to_dict()
    payload["confirm"] = args.confirm

    if args.format == "json":
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        mode = (result.delivery_status or "submitted, unconfirmed") if args.confirm else "dry-run"
        print(f"{mode} message")
        print(f"recipient: {payload['recipient']}")
        print(f"sent: {payload['sent']} verified: {payload['verified']}")
        print(f"route: {payload['resolved_service']} {payload['resolved_target']}")
        print(f"observed service: {result.message_service} message error: {result.message_error}")
        print(f"delivered: {result.delivery_status == 'delivered'}")
        if result.text_delivery_status:
            print(f"text status only: {result.text_delivery_status}")
        if result.error:
            print(f"error: {result.error}")
        if payload.get("message_id"):
            print(f"message_id: {payload['message_id']}")
        if payload.get("message_guid"):
            print(f"message_guid: {payload['message_guid']}")
        if text:
            print("text:")
            print(text)
        if file_paths:
            print("files:")
            for path in file_paths:
                print(f"- {path}")
    if not args.confirm:
        return 0
    return 1 if result.delivery_status == "failed" else 0 if result.verified else 2


if __name__ == "__main__":
    raise SystemExit(main())

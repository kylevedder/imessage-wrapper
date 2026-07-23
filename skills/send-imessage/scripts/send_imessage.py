#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

WRAPPER_ROOT = Path(__file__).resolve().parents[3]
WRAPPER_SRC = WRAPPER_ROOT / "src"
if WRAPPER_SRC.exists():
    sys.path.insert(0, str(WRAPPER_SRC))

from imessage_wrapper import IMessageClient  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview or send an iMessage/SMS through Messages.app.")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--to", help="Phone number, email, or handle for a one-to-one recipient.")
    target.add_argument("--chat-id", type=int, help="Existing chat ROWID.")
    target.add_argument("--chat-identifier", help="Existing chat identifier.")
    target.add_argument("--chat-guid", help="Existing chat guid.")

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
        mode = "sent" if args.confirm else "dry-run"
        print(f"{mode} message")
        print(f"recipient: {payload['recipient']}")
        print(f"sent: {payload['sent']} verified: {payload['verified']}")
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

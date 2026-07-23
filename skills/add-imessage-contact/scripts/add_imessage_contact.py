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
    parser = argparse.ArgumentParser(description="Create a macOS Contacts record for iMessage use.")
    parser.add_argument("--first-name", default="")
    parser.add_argument("--middle-name", default="")
    parser.add_argument("--last-name", default="")
    parser.add_argument("--nickname", default="")
    parser.add_argument("--organization", default="")
    parser.add_argument("--phone", action="append", default=[])
    parser.add_argument("--email", action="append", default=[])
    parser.add_argument("--confirm", action="store_true", help="Actually create the contact. Without this flag, dry-run only.")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--home", default=str(Path.home()))
    return parser.parse_args()


def clean_items(values: list[str]) -> list[str]:
    return [item.strip() for item in values if item.strip()]


def payload_from_args(args: argparse.Namespace) -> dict[str, object]:
    return {
        "first_name": args.first_name.strip(),
        "middle_name": args.middle_name.strip(),
        "last_name": args.last_name.strip(),
        "nickname": args.nickname.strip(),
        "organization": args.organization.strip(),
        "phones": clean_items(args.phone),
        "emails": clean_items(args.email),
    }


def main() -> int:
    args = parse_args()
    payload = payload_from_args(args)
    if not any(
        [
            payload["first_name"],
            payload["middle_name"],
            payload["last_name"],
            payload["nickname"],
            payload["organization"],
            payload["phones"],
            payload["emails"],
        ]
    ):
        raise SystemExit("provide at least one name, organization, phone, or email field")

    result = {"dry_run": not args.confirm, "payload": payload, "contact_id": None}
    if args.confirm:
        client = IMessageClient(home=args.home)
        result["contact_id"] = client.create_contact(
            first_name=str(payload["first_name"]),
            middle_name=str(payload["middle_name"]),
            last_name=str(payload["last_name"]),
            nickname=str(payload["nickname"]),
            organization=str(payload["organization"]),
            phones=list(payload["phones"]),
            emails=list(payload["emails"]),
        )

    if args.format == "json":
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        mode = "created" if args.confirm else "dry-run"
        print(f"{mode} contact")
        if result["contact_id"]:
            print(f"id: {result['contact_id']}")
        for key, value in payload.items():
            if value:
                print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

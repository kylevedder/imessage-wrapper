#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

WRAPPER_ROOT = Path(__file__).resolve().parents[3]
WRAPPER_SRC = WRAPPER_ROOT / "src"
if WRAPPER_SRC.exists():
    sys.path.insert(0, str(WRAPPER_SRC))

from imessage_wrapper import IMessageClient  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Search local macOS Contacts for iMessage use.")
    parser.add_argument("--query", "-q", required=True, help="Name, phone, email, nickname, or organization to search.")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--home", default=str(Path.home()))
    return parser.parse_args()


def compact_contact(contact):
    data = contact.to_dict()
    return {
        "id": data["id"],
        "display_name": data["display_name"],
        "first_name": data["first_name"],
        "middle_name": data["middle_name"],
        "last_name": data["last_name"],
        "nickname": data["nickname"],
        "organization": data["organization"],
        "phones": data["phones"],
        "emails": data["emails"],
        "source_db_path": data["source_db_path"],
    }


def main() -> int:
    args = parse_args()
    if args.limit < 1:
        raise SystemExit("--limit must be >= 1")
    client = IMessageClient(home=args.home)
    contacts = [compact_contact(contact) for contact in client.search_contacts(args.query, limit=args.limit)]
    payload = {"query": args.query, "count": len(contacts), "contacts": contacts}
    if args.format == "json":
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"query={args.query!r} count={len(contacts)}")
        for contact in contacts:
            phones = ", ".join(phone["value"] for phone in contact["phones"]) or "-"
            emails = ", ".join(email["value"] for email in contact["emails"]) or "-"
            org = f" org={contact['organization']}" if contact["organization"] else ""
            print(f"- {contact['display_name']} id={contact['id']}{org}")
            print(f"  phones: {phones}")
            print(f"  emails: {emails}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

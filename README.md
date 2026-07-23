# imessage-wrapper

`imessage-wrapper` is a sync-first Python package for reading local macOS
Messages history, enriching it with macOS Contacts, sending through Messages.app,
and creating/updating Contacts records through Apple's Contacts framework.

## Install

```bash
pip install imessage-wrapper
```

Install the optional Contacts write dependency on macOS with:

```bash
pip install "imessage-wrapper[contacts]"
```

## Development

This repository uses `uv` for reproducible local environments. The committed
`uv.lock` captures dependency resolution; the generated `.venv/` stays local
and is ignored by git.

```bash
uv sync --all-extras
uv run pytest
```

After changing dependencies in `pyproject.toml`, refresh the lockfile with:

```bash
uv lock
```

## Usage

```python
from imessage_wrapper import IMessageClient

client = IMessageClient()

for chat in client.chats(limit=10):
    print(chat.id, chat.name, chat.last_message_at, chat.unread_count)

messages = client.messages(chat_id=chat.id, limit=50, attachments=True)
unread_chats = client.chats(unread_only=True)
stats = client.stats(time_zone="America/Los_Angeles", include_media=True)
print(stats.total_messages, stats.media.total_bytes if stats.media else 0)
client.send(chat_id=chat.id, text="hello")

# Contacts are alphabetical by default; request newest-added first when needed.
recent_contacts = client.contacts(limit=25, sort="recent")
```

When supported by the local Messages schema, chats include logical inbound
`unread_count` values. Inbound messages expose `is_read` and an optional
`date_read`; outbound and legacy-schema message dictionaries omit those fields.
Statistics exclude reaction rows, fold split URL previews into one logical
message, and count each attachment once even when join rows are duplicated.

## Permissions

Reads require Full Disk Access for the Python process so it can open
`~/Library/Messages/chat.db` and the AddressBook databases. Sending requires
Automation permission to control Messages.app. Contact writes require Contacts
permission and the `pyobjc-framework-Contacts` dependency on macOS.

The package reads Messages and AddressBook SQLite databases in read-only mode.
It never writes those databases directly.

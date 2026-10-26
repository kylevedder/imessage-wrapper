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

## Codex skills

The reusable iMessage skills live under `skills/` in this repository. Link them
into Codex's global skill directory with:

```bash
./scripts/setup_skill_symlinks.sh
```

The setup is idempotent. Existing paths with the same skill names are moved to
`$CODEX_HOME/skill-backups/` (or `~/.codex/skill-backups/`) before the symlinks
are created. Verify an installation without changing it with:

```bash
./scripts/setup_skill_symlinks.sh --check
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
status = client.send_status(message_id=123)  # read-only; never resends

# Contacts are alphabetical by default; request newest-added first when needed.
recent_contacts = client.contacts(limit=25, sort="recent")
```

When supported by the local Messages schema, chats include logical inbound
`unread_count` values. Inbound messages expose `is_read` and an optional
`date_read`; outbound and legacy-schema message dictionaries omit those fields.
Statistics exclude reaction rows, fold split URL previews into one logical
message, and count each attachment once even when join rows are duplicated.

### Sending and checking delivery

`service="auto"` prefers an exact existing direct conversation and its latest
inbound or confirmed-delivered service history. Unconfirmed/failed iMessage
attempts do not displace a known RCS route. Equally supported conversations
require an explicit `chat_id`. Unknown recipients default to iMessage.
`service="sms"` selects the carrier messaging family (SMS/MMS/RCS); Messages
decides which carrier protocol the conversation uses. It does not force an RCS
conversation to downgrade to SMS. A service that conflicts with an explicitly
selected conversation is rejected before dispatch.
`resolved_service` describes the intended route; `message_service` is what
Messages actually recorded. An explicit service mismatch cannot be verified.
Route selection, including previews and sends with verification disabled,
requires readable Messages history. If that lookup is unavailable, sending
stops before dispatch instead of silently guessing a service. Grant the Python
process Full Disk Access to use these operations.

Results distinguish `submitted` (the automation returned), `sent` (Messages
reports sent or delivered), and `verified` (delivery confirmed for iMessage/RCS,
or sent confirmed for SMS/MMS). `recorded`, `pending`, and `unconfirmed` are not
delivery confirmations. A nonzero Messages error is `failed`; `is_finished`
alone never implies success or failure. These are snapshots and can change.
An automation timeout or exception has an unknown submission outcome
(`submitted=null`) until an outgoing row can be identified. Verification still
checks for the row; the wrapper never retries automatically.

Files are dispatched separately from text. Attachment delivery is not verified
by this API: file sends return `attachments_unconfirmed`, with any text evidence
in `text_delivery_status`. A delivered text does not certify the attachments.

After an uncertain send, check the returned message ID without resending:

```bash
uv run python skills/send-imessage/scripts/send_imessage.py --status 123 --format json
```

The CLI exits 0 for a verified result or preview, 1 for a confirmed failure,
and 2 for an unconfirmed result. No automatic retry follows a submission.
`--status` has `status_scope="message"`: it checks that one outgoing row, not
other messages or files sent alongside it. Rows with attachments remain
unconfirmed. Multiple new same-text matches are ambiguous, with no guessed ID.
SMS/MMS/RCS relies on the paired iPhone and its Text Message Forwarding setup;
a successful automation call cannot establish that the relay or carrier worked.
Messages also determines the outgoing caller number; selecting an account does
not guarantee a particular caller alias.

## Permissions

Reads require Full Disk Access for the Python process so it can open
`~/Library/Messages/chat.db` and the AddressBook databases. Sending requires
Automation permission to control Messages.app. Contact writes require Contacts
permission and the `pyobjc-framework-Contacts` dependency on macOS.

The package reads Messages and AddressBook SQLite databases in read-only mode.
It never writes those databases directly.

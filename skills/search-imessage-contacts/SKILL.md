---
name: search-imessage-contacts
description: Search Kyle's local macOS Contacts records as used by iMessage via the imessage-wrapper package. Use when the user asks Codex to find an iMessage contact, look up a phone number or email owner, resolve a sender/handle to a contact name, inspect contact details before messaging, or search contacts by name, nickname, organization, phone, or email.
---

# Search iMessage Contacts

## Workflow

Use the bundled script first:

```bash
skill_dir="${CODEX_HOME:-$HOME/.codex}/skills/search-imessage-contacts"
repo_root="$(git -C "$skill_dir" rev-parse --show-toplevel)"
uv --directory "$repo_root" run python \
  "$skill_dir/scripts/search_imessage_contacts.py" \
  --query "alice" --limit 10 --format json
```

The script reads local AddressBook databases through `imessage_wrapper.IMessageClient`. It is read-only.

## Answering Rules

- Return the contact name plus relevant phone/email fields; do not dump unrelated contact data.
- If resolving a phone number from Messages, search both the exact handle and compact digit forms.
- If multiple contacts match, list the top candidates and explain the ambiguity.
- Do not create, update, delete, or message contacts from this skill.

## Options

- `--query TEXT`: required search text; can be a name, phone, email, nickname, or organization.
- `--limit N`: max results.
- `--format text|json`: output shape.
- `--home PATH`: override home directory if needed.

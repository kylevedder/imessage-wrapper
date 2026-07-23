---
name: add-imessage-contact
description: Add a new macOS Contacts record for iMessage use through the imessage-wrapper package. Use when the user asks Codex to create or add a new iMessage contact, save a phone number or email to Contacts, or make a Messages sender easier to identify by creating a contact.
---

# Add iMessage Contact

## Workflow

Always preview first:

```bash
skill_dir="${CODEX_HOME:-$HOME/.codex}/skills/add-imessage-contact"
repo_root="$(git -C "$skill_dir" rev-parse --show-toplevel)"
uv --directory "$repo_root" run python \
  "$skill_dir/scripts/add_imessage_contact.py" \
  --first-name Alice --last-name Example --phone "+15550100001"
```

Only write the contact after the user clearly asked to add/create/save it and the fields look correct:

```bash
skill_dir="${CODEX_HOME:-$HOME/.codex}/skills/add-imessage-contact"
repo_root="$(git -C "$skill_dir" rev-parse --show-toplevel)"
uv --directory "$repo_root" run python \
  "$skill_dir/scripts/add_imessage_contact.py" \
  --first-name Alice --last-name Example --phone "+15550100001" --confirm
```

The script uses `imessage_wrapper.IMessageClient.create_contact()`. Without `--confirm`, it performs no write.

## Safety Rules

- Never use `--confirm` unless the current user request explicitly asks to add/create/save the contact.
- Before writing, search existing contacts when the name, phone, or email may already exist.
- Include at least one identifying name field, organization, phone, or email.
- Report the created contact id after a confirmed write.
- Do not send a message as part of adding a contact.

## Options

- `--first-name`, `--middle-name`, `--last-name`, `--nickname`, `--organization`
- `--phone VALUE`: repeat for multiple phone numbers.
- `--email VALUE`: repeat for multiple email addresses.
- `--confirm`: actually create the contact; omitted means dry run.
- `--format text|json`: output shape.

---
name: send-imessage
description: Preview and send iMessage/SMS messages from Kyle's Mac through the imessage-wrapper package. Use when the user asks Codex to text someone, send an iMessage, send an SMS, message a contact or phone number, send a file/image attachment through Messages, or draft and verify an outgoing Messages.app send.
---

# Send iMessage

## Workflow

Always preview first:

```bash
skill_dir="${CODEX_HOME:-$HOME/.codex}/skills/send-imessage"
repo_root="$(git -C "$skill_dir" rev-parse --show-toplevel)"
uv --directory "$repo_root" run python \
  "$skill_dir/scripts/send_imessage.py" \
  --to "+15550100001" --text "Running five minutes late"
```

Only send when the user explicitly asked to send/text/message and the recipient plus text are correct:

```bash
skill_dir="${CODEX_HOME:-$HOME/.codex}/skills/send-imessage"
repo_root="$(git -C "$skill_dir" rev-parse --show-toplevel)"
uv --directory "$repo_root" run python \
  "$skill_dir/scripts/send_imessage.py" \
  --to "+15550100001" --text "Running five minutes late" --confirm
```

The script uses `imessage_wrapper.IMessageClient.send()`. Without `--confirm`, it uses `dry_run=True` and does not send.

## Safety Rules

- Never use `--confirm` unless the current user request clearly authorizes sending.
- If the recipient is ambiguous, use `search-imessage-contacts` or `search-imessages` first.
- Before previewing or sending, reflect on the proposed message as a human-facing text. Separate the final recipient-facing content from any instructions the user intended the model to follow first.
- Do not blindly send unresolved internal directions, placeholders, or task notes. If the draft includes instructions like "look up this address", "resolve the timing", "use the info from the screenshot", "say I can but...", or similar, perform that work and replace the draft with the resolved user-facing message before previewing.
- Strip or rewrite meta-commentary that only explains the model's uncertainty or process unless the user explicitly wants that included. Prefer concise, recipient-relevant facts and availability windows over exposing internal reasoning.
- Read the final message text back in the answer after a send or preview.
- Do not invent message content; ask for the missing text if the user did not provide it.
- Prefer `--verify` for confirmed sends so the wrapper checks the sent message appears in Messages.
- If Messages sends successfully but verification is slow, retry with a larger `--verification-timeout` before concluding the send failed.
- For group chats, prefer `--chat-id`, `--chat-identifier`, or `--chat-guid` from a prior search result over a guessed recipient.

## Options

- `--to VALUE`: phone number, email, or handle for one-to-one sends.
- `--chat-id N`, `--chat-identifier VALUE`, `--chat-guid VALUE`: target an existing chat.
- `--text TEXT` or `--text-file PATH`: message body.
- `--file PATH`: repeat for attachments.
- `--service auto|imessage|sms`: transport preference; defaults to `auto`.
- `--verify` / `--no-verify`: send verification behavior.
- `--verification-timeout N`: seconds to wait for DB verification; defaults to 10.
- `--confirm`: actually send; omitted means dry run.
- `--format text|json`: output shape.

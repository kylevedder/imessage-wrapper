---
name: search-imessages
description: Search and analyze Kyle's local macOS iMessage/SMS history with the imessage-wrapper package. Use when the user asks Codex to find text messages, inspect iMessage conversations, answer questions about plans or commitments based on messages, search by keyword/person/date, summarize message evidence, or verify whether a text-message event happened.
---

# Search iMessages

## Core Workflow

Use the bundled script first:

```bash
skill_dir="${CODEX_HOME:-$HOME/.codex}/skills/search-imessages"
repo_root="$(git -C "$skill_dir" rev-parse --show-toplevel)"
uv --directory "$repo_root" run python \
  "$skill_dir/scripts/search_imessages.py" \
  --query "coffee" --days 30 --context 2
```

The script reads `~/Library/Messages/chat.db` in SQLite read-only mode, imports `imessage_wrapper` from the repository resolved through the installed skill symlink, decodes `attributedBody`, and enriches sender/chat names when possible. Run it through the wrapper repo's `uv` environment so PyObjC/Foundation is available. It does not write to Messages or Contacts.

Prefer JSON output when doing substantial reasoning:

```bash
skill_dir="${CODEX_HOME:-$HOME/.codex}/skills/search-imessages"
repo_root="$(git -C "$skill_dir" rev-parse --show-toplevel)"
uv --directory "$repo_root" run python \
  "$skill_dir/scripts/search_imessages.py" \
  --query "tomorrow" --query "monday" --after 2026-06-01 --before 2026-06-23 \
  --context 3 --format json
```

For a broad date-range review, load all messages in a bounded window:

```bash
skill_dir="${CODEX_HOME:-$HOME/.codex}/skills/search-imessages"
repo_root="$(git -C "$skill_dir" rev-parse --show-toplevel)"
uv --directory "$repo_root" run python \
  "$skill_dir/scripts/search_imessages.py" \
  --all --after 2026-06-21 --before 2026-06-22 --limit 1000 --format json
```

## Answering Rules

- Treat message search as private local data access; quote only the short snippets needed to answer.
- State date assumptions explicitly, especially for relative words like `tomorrow`, `today`, `Monday`, or `next week`.
- Distinguish confirmed commitments from tentative suggestions, automated invites, and unresolved scheduling.
- Use surrounding context before concluding that the user promised something.
- For broad questions, prefer `--all` on a narrow date range over guessing keywords.
- If no result is found, say what date range and terms were searched.
- Do not send messages, mutate Contacts, or write to the Messages database while using this skill.

## Script Options

Common options:

- `--query TEXT`: include messages matching text; repeat for OR matching.
- `--all`: return every message in the bounded date range; cannot be combined with `--query`.
- `--after YYYY-MM-DD` / `--before YYYY-MM-DD`: local date window; `--before` is exclusive.
- `--days N`: search the last N days when exact dates are not supplied.
- `--context N`: include N messages before and after each match in the same chat.
- `--limit N`: maximum matching messages, or maximum returned messages in `--all` mode.
- `--scan-limit N`: maximum candidate rows to inspect before query matching.
- `--from-me` / `--not-from-me`: restrict sender direction.
- `--include-reactions`: include tapbacks/reaction messages.
- `--format text|json`: choose human-readable or structured output.

For commitment/plans questions, search broad terms first (`tomorrow`, weekday names, date strings, `meet`, `lunch`, `dinner`, `coffee`, `walk`, `call`, `see you`, `works`, `sounds good`) and then rerun with `--context 3` on promising threads.

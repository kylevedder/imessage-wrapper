# imessage-wrapper

`imessage-wrapper` is a sync-first Python package for reading local macOS
Messages history, enriching it with macOS Contacts, sending through Messages.app,
and creating/updating Contacts records through Apple's Contacts framework.

```python
from imessage_wrapper import IMessageClient

client = IMessageClient()

for chat in client.chats(limit=10):
    print(chat.id, chat.name, chat.last_message_at)

messages = client.messages(chat_id=chat.id, limit=50, attachments=True)
client.send(chat_id=chat.id, text="hello")
```

## Permissions

Reads require Full Disk Access for the Python process so it can open
`~/Library/Messages/chat.db` and the AddressBook databases. Sending requires
Automation permission to control Messages.app. Contact writes require Contacts
permission and the `pyobjc-framework-Contacts` dependency on macOS.

The package reads Messages and AddressBook SQLite databases in read-only mode.
It never writes those databases directly.

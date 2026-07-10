from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class PhoneNumber:
    value: str
    label: str | None = None
    is_primary: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EmailAddress:
    value: str
    label: str | None = None
    is_primary: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Contact:
    id: str
    display_name: str
    first_name: str | None = None
    middle_name: str | None = None
    last_name: str | None = None
    nickname: str | None = None
    organization: str | None = None
    phones: list[PhoneNumber] = field(default_factory=list)
    emails: list[EmailAddress] = field(default_factory=list)
    created_at: datetime | None = None
    modified_at: datetime | None = None
    source_db_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat() if self.created_at else None
        data["modified_at"] = self.modified_at.isoformat() if self.modified_at else None
        return data


@dataclass(frozen=True)
class Attachment:
    guid: str | None = None
    filename: str | None = None
    path: str | None = None
    transfer_name: str | None = None
    uti: str | None = None
    mime_type: str | None = None
    byte_size: int | None = None
    missing: bool = False
    converted_path: str | None = None
    converted_mime_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Reaction:
    guid: str | None = None
    target_guid: str | None = None
    type_code: int | None = None
    type_label: str | None = None
    emoji: str | None = None
    is_from_me: bool = False
    created_at: datetime | None = None
    text: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat() if self.created_at else None
        return data


@dataclass(frozen=True)
class Chat:
    id: int
    identifier: str
    guid: str | None
    name: str
    display_name: str | None = None
    contact_name: str | None = None
    service: str | None = None
    is_group: bool = False
    participants: list[str] = field(default_factory=list)
    contacts: list[Contact] = field(default_factory=list)
    last_message_at: datetime | None = None
    message_count: int = 0
    account_id: str | None = None
    account_login: str | None = None
    last_addressed_handle: str | None = None
    unread_count: int | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["last_message_at"] = self.last_message_at.isoformat() if self.last_message_at else None
        if self.unread_count is None:
            data.pop("unread_count", None)
        return data


@dataclass(frozen=True)
class Message:
    id: int
    chat_id: int
    guid: str | None
    sender: str | None
    text: str
    created_at: datetime | None
    is_from_me: bool
    service: str | None = None
    handle_id: int | None = None
    chat_identifier: str | None = None
    chat_guid: str | None = None
    chat_name: str | None = None
    participants: list[str] = field(default_factory=list)
    is_group: bool = False
    sender_name: str | None = None
    contact: Contact | None = None
    is_read: bool | None = None
    reply_to_guid: str | None = None
    thread_originator_guid: str | None = None
    reply_to_text: str | None = None
    reply_to_sender: str | None = None
    destination_caller_id: str | None = None
    attachments: list[Attachment] = field(default_factory=list)
    reactions: list[Reaction] = field(default_factory=list)
    is_reaction: bool = False
    reaction_type: str | None = None
    reaction_emoji: str | None = None
    reacted_to_guid: str | None = None
    date_read: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat() if self.created_at else None
        if self.is_from_me or self.is_read is None:
            data.pop("is_read", None)
            data.pop("date_read", None)
        elif self.date_read is not None:
            data["date_read"] = self.date_read.isoformat()
        else:
            data.pop("date_read", None)
        return data


@dataclass(frozen=True)
class SendResult:
    recipient: str
    text: str = ""
    file_paths: list[str] = field(default_factory=list)
    sent: bool = False
    verified: bool | None = None
    delivery_status: str | None = None
    message_service: str | None = None
    message_error: int | None = None
    dry_run: bool = False
    message_id: int | None = None
    message_guid: str | None = None
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ChatMessageStats:
    chat_id: int
    identifier: str
    name: str
    service: str
    message_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SenderMessageStats:
    handle: str
    message_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ServiceMessageStats:
    service: str
    message_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DateMessageStats:
    date: str
    message_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MediaTypeStats:
    uti: str
    mime_type: str
    attachment_count: int
    total_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ChatMediaStats:
    chat_id: int
    identifier: str
    name: str
    attachment_count: int
    total_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MediaStats:
    total_attachments: int
    total_bytes: int
    types: list[MediaTypeStats] = field(default_factory=list)
    chats: list[ChatMediaStats] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MessageStats:
    total_messages: int
    sent_messages: int
    received_messages: int
    time_zone: str
    chats: list[ChatMessageStats] = field(default_factory=list)
    senders: list[SenderMessageStats] = field(default_factory=list)
    services: list[ServiceMessageStats] = field(default_factory=list)
    dates: list[DateMessageStats] = field(default_factory=list)
    media: MediaStats | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.media is None:
            data.pop("media", None)
        return data

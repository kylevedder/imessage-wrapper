from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any


class ContactsWriteError(RuntimeError):
    pass


@dataclass(frozen=True)
class ContactWritePayload:
    first_name: str = ""
    last_name: str = ""
    middle_name: str = ""
    nickname: str = ""
    organization: str = ""
    phones: tuple[str, ...] = ()
    emails: tuple[str, ...] = ()


class ContactsWriter:
    def _contacts_module(self) -> Any:
        if sys.platform != "darwin":
            raise ContactsWriteError("macOS Contacts writes are only supported on macOS")
        try:
            import Contacts  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ContactsWriteError(
                "Contacts writes require pyobjc-framework-Contacts. Install with: "
                "pip install 'imessage-wrapper[contacts]'"
            ) from exc
        return Contacts

    def create_contact(self, payload: ContactWritePayload) -> str:
        Contacts = self._contacts_module()
        store = Contacts.CNContactStore.alloc().init()
        contact = Contacts.CNMutableContact.alloc().init()
        self._apply_payload(contact, payload, Contacts)
        request = Contacts.CNSaveRequest.alloc().init()
        request.addContact_toContainerWithIdentifier_(contact, None)
        ok, error = store.executeSaveRequest_error_(request, None)
        if not ok:
            raise ContactsWriteError(str(error) if error else "Contacts create failed")
        return str(contact.identifier())

    def update_contact(self, contact_id: str, payload: ContactWritePayload) -> str:
        Contacts = self._contacts_module()
        store = Contacts.CNContactStore.alloc().init()
        keys = [
            Contacts.CNContactGivenNameKey,
            Contacts.CNContactMiddleNameKey,
            Contacts.CNContactFamilyNameKey,
            Contacts.CNContactNicknameKey,
            Contacts.CNContactOrganizationNameKey,
            Contacts.CNContactPhoneNumbersKey,
            Contacts.CNContactEmailAddressesKey,
        ]
        contact, error = store.unifiedContactWithIdentifier_keysToFetch_error_(contact_id, keys, None)
        if contact is None:
            raise ContactsWriteError(str(error) if error else f"Contact not found: {contact_id}")
        mutable = contact.mutableCopy()
        self._apply_payload(mutable, payload, Contacts)
        request = Contacts.CNSaveRequest.alloc().init()
        request.updateContact_(mutable)
        ok, error = store.executeSaveRequest_error_(request, None)
        if not ok:
            raise ContactsWriteError(str(error) if error else "Contacts update failed")
        return str(mutable.identifier())

    def _apply_payload(self, contact: Any, payload: ContactWritePayload, Contacts: Any) -> None:
        contact.setGivenName_(payload.first_name)
        contact.setMiddleName_(payload.middle_name)
        contact.setFamilyName_(payload.last_name)
        contact.setNickname_(payload.nickname)
        contact.setOrganizationName_(payload.organization)
        phone_values = []
        for phone in payload.phones:
            number = Contacts.CNPhoneNumber.phoneNumberWithStringValue_(phone)
            labeled = Contacts.CNLabeledValue.labeledValueWithLabel_value_(
                Contacts.CNLabelPhoneNumberMobile,
                number,
            )
            phone_values.append(labeled)
        email_values = [
            Contacts.CNLabeledValue.labeledValueWithLabel_value_(Contacts.CNLabelHome, email)
            for email in payload.emails
        ]
        contact.setPhoneNumbers_(phone_values)
        contact.setEmailAddresses_(email_values)

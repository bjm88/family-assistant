"""Apple Contacts adapter — placeholder, not implemented yet.

Registered so the provider shows up (as "coming soon") in the admin UI
and so the rest of the app never needs to change when it lands. Every
operation raises :class:`ContactsNotSupported`.

Implementation notes for whoever picks this up
----------------------------------------------
Apple has no OAuth-style contacts API. iCloud contacts are reachable
over CardDAV at ``https://contacts.icloud.com`` with the person's
Apple ID and an *app-specific password* (appleid.apple.com → Sign-In
and Security → App-Specific Passwords). So:

* **Connecting** is a form (Apple ID + app-specific password), not a
  redirect. Store both, Fernet-encrypted, in
  ``contact_provider_connections.credentials_encrypted`` with
  ``provider='apple'`` and the Apple ID as ``account_identifier``.
* **Discovery**: ``PROPFIND`` the principal URL, then the
  ``addressbook-home-set``, to find the address-book collection.
* **list_all**: ``REPORT addressbook-query`` returning each vCard and
  its ETag. Parse with :func:`.vcard.parse_vcards` and keep the
  resource href as ``contact_id`` and the vCard text as ``raw``.
* **create / update**: ``PUT`` a vCard 3.0 body. Send ``If-Match`` with
  the stored ETag on update and raise :class:`ContactConflict` on 412.
  Start from the stored vCard so properties this app doesn't model
  (photos, addresses, birthdays) survive, as the Google adapter does.
* **search**: CardDAV text-match is patchy on iCloud; use
  :func:`.sync.filter_contacts` over ``list_all``.

A 401 means the app-specific password was revoked: raise
:class:`ContactsAuthExpired` so Avi asks the person to reconnect.
"""

from __future__ import annotations

from typing import List

from .base import Contact, ContactsAdapter, ContactsNotSupported


class AppleContactsAdapter(ContactsAdapter):
    provider = "apple"
    label = "Apple Contacts (iCloud)"
    available = False

    def _unsupported(self) -> ContactsNotSupported:
        return ContactsNotSupported(
            self.provider,
            "Apple Contacts isn't supported yet. Connect Google Contacts instead.",
        )

    def list_all(self) -> List[Contact]:
        raise self._unsupported()

    def search(self, query: str, *, limit: int = 10) -> List[Contact]:
        raise self._unsupported()

    def get(self, contact_id: str) -> Contact:
        raise self._unsupported()

    def create(self, contact: Contact) -> Contact:
        raise self._unsupported()

    def update(self, contact: Contact) -> Contact:
        raise self._unsupported()

    def count(self) -> int:
        raise self._unsupported()

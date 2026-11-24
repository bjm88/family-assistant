"""Address-book integrations behind one adapter contract.

* :mod:`.base`        — :class:`Contact`, :class:`ContactsAdapter`, errors.
* :mod:`.google`      — Google Contacts (People API). The default provider.
* :mod:`.apple`       — Apple Contacts placeholder; raises "not supported".
* :mod:`.sync`        — provider-neutral find / merge / upsert rules.
* :mod:`.connections` — provider registry, per-person stored grants,
                        the Google consent flow.
* :mod:`.vcard`       — reading shared contact cards (``.vcf``).
* :mod:`.messages`    — wording for permission requests.

Contacts are personal. Unlike Avi's Gmail / Calendar connection (one
Google account for the assistant), each family member connects their
own address book and Avi only ever touches the address book of the
person he is talking to.
"""

from .base import (
    Contact,
    ContactConflict,
    ContactEmail,
    ContactPhone,
    ContactsAdapter,
    ContactsAuthExpired,
    ContactsError,
    ContactsNotConnected,
    ContactsNotSupported,
)
from .connections import PROVIDERS, adapter_for_person, default_provider
from .sync import UpsertResult, upsert

__all__ = [
    "PROVIDERS",
    "Contact",
    "ContactConflict",
    "ContactEmail",
    "ContactPhone",
    "ContactsAdapter",
    "ContactsAuthExpired",
    "ContactsError",
    "ContactsNotConnected",
    "ContactsNotSupported",
    "UpsertResult",
    "adapter_for_person",
    "default_provider",
    "upsert",
]

"""Provider-neutral contact types and the adapter contract.

Everything above this layer (the agent tools, the HTTP router, the
merge logic in :mod:`.sync`) talks in these types only, so adding a
provider means writing one :class:`ContactsAdapter` subclass and
registering it in :mod:`.registry` — nothing else changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, List, Optional


# Phone / email labels the rest of the app uses. Adapters translate to
# and from whatever vocabulary their provider has.
LABEL_MOBILE = "mobile"
LABEL_WORK = "work"
LABEL_HOME = "home"
LABEL_OTHER = "other"
LABELS = (LABEL_MOBILE, LABEL_WORK, LABEL_HOME, LABEL_OTHER)


class ContactsError(RuntimeError):
    """A contacts provider rejected or failed a request."""


class ContactsNotConnected(ContactsError):
    """The person has not granted access to their address book yet."""

    def __init__(self, provider: str, message: str = "") -> None:
        super().__init__(message or f"No {provider} contacts account is connected.")
        self.provider = provider


class ContactsAuthExpired(ContactsError):
    """The stored grant no longer works; the person has to reconnect."""

    def __init__(self, provider: str, message: str = "") -> None:
        super().__init__(
            message or f"The {provider} contacts connection has expired or was revoked."
        )
        self.provider = provider


class ContactsNotSupported(ContactsError):
    """The provider exists as a placeholder but isn't implemented yet."""

    def __init__(self, provider: str, message: str = "") -> None:
        super().__init__(message or f"{provider} contacts are not supported yet.")
        self.provider = provider


class ContactConflict(ContactsError):
    """The contact changed on the provider while we were updating it."""


@dataclass(frozen=True)
class ContactEmail:
    value: str
    label: Optional[str] = None


@dataclass(frozen=True)
class ContactPhone:
    value: str
    label: Optional[str] = None


@dataclass
class Contact:
    """One address-book entry, in or out of a provider."""

    contact_id: Optional[str] = None
    given_name: Optional[str] = None
    family_name: Optional[str] = None
    display_name: Optional[str] = None
    company: Optional[str] = None
    job_title: Optional[str] = None
    emails: List[ContactEmail] = field(default_factory=list)
    phones: List[ContactPhone] = field(default_factory=list)
    notes: Optional[str] = None
    # The provider's own record, kept so an update can send back fields
    # this dataclass doesn't model instead of wiping them.
    raw: Optional[Any] = field(default=None, repr=False, compare=False)

    @property
    def name(self) -> str:
        """Best human label for this contact."""
        if self.display_name:
            return self.display_name
        joined = " ".join(p for p in (self.given_name, self.family_name) if p)
        if joined:
            return joined
        if self.company:
            return self.company
        if self.emails:
            return self.emails[0].value
        if self.phones:
            return self.phones[0].value
        return "(unnamed contact)"

    def to_dict(self) -> dict:
        return {
            "contact_id": self.contact_id,
            "name": self.name,
            "given_name": self.given_name,
            "family_name": self.family_name,
            "company": self.company,
            "job_title": self.job_title,
            "emails": [{"value": e.value, "label": e.label} for e in self.emails],
            "phones": [{"value": p.value, "label": p.label} for p in self.phones],
            "notes": self.notes,
        }


class ContactsAdapter(ABC):
    """One person's address book on one provider."""

    provider: ClassVar[str]
    label: ClassVar[str]
    # False for placeholder adapters; the UI shows those as "coming soon".
    available: ClassVar[bool] = True

    @abstractmethod
    def list_all(self) -> List[Contact]:
        """Every contact in the address book.

        Used for duplicate detection, so it must be authoritative —
        not a search index that can lag behind recent writes.
        """

    @abstractmethod
    def search(self, query: str, *, limit: int = 10) -> List[Contact]:
        """Contacts whose name, email, phone or company match ``query``."""

    @abstractmethod
    def get(self, contact_id: str) -> Contact:
        """Fetch one contact by its provider id."""

    @abstractmethod
    def create(self, contact: Contact) -> Contact:
        """Add a new contact and return it as stored."""

    @abstractmethod
    def update(self, contact: Contact) -> Contact:
        """Persist ``contact`` (which must carry ``contact_id``).

        Raises :class:`ContactConflict` when the provider's copy changed
        since ``contact`` was read.
        """

    def count(self) -> int:
        """How many contacts the address book holds (connection test)."""
        return len(self.list_all())

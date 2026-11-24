"""Google Contacts adapter (People API v1).

Acts on one person's own Google address book using the credentials
they granted through the contacts consent flow (scope
``https://www.googleapis.com/auth/contacts``).

Three People API behaviours shape this module:

* ``people.searchContacts`` is a prefix search over a cached index. It
  needs a warm-up call and can miss a contact created minutes ago, so
  it is used for the conversational "look someone up" path only and
  falls back to a full scan when it finds nothing. Duplicate detection
  always uses ``connections.list``, which is authoritative.
* ``people.updateContact`` replaces every field named in
  ``updatePersonFields`` wholesale. Updates therefore start from the
  contact exactly as Google returned it, change only what differs, and
  name only the fields that changed.
* A person record can mix the contact the user saved with data from
  that person's own Google profile. Only the saved-contact part is
  writable, so only that part is sent back.
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import replace
from typing import Any, Callable, Dict, List, Optional

from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .base import (
    LABEL_HOME,
    LABEL_MOBILE,
    LABEL_OTHER,
    LABEL_WORK,
    Contact,
    ContactConflict,
    ContactEmail,
    ContactPhone,
    ContactsAdapter,
    ContactsAuthExpired,
    ContactsError,
)
from .sync import filter_contacts, phones_match


logger = logging.getLogger(__name__)


CONTACTS_SCOPE = "https://www.googleapis.com/auth/contacts"
# Scopes requested by the per-person consent flow. The identity scopes
# only tell us which Google account was linked.
OAUTH_SCOPES: tuple[str, ...] = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    CONTACTS_SCOPE,
)

_WRITABLE_FIELDS = (
    "names",
    "emailAddresses",
    "phoneNumbers",
    "organizations",
    "biographies",
)
_PERSON_FIELDS = ",".join((*_WRITABLE_FIELDS, "metadata"))
_PAGE_SIZE = 1000
# Google caps an address book at 25,000 contacts.
_MAX_PAGES = 25
_SEARCH_MAX = 30

_TO_LABEL = {
    "mobile": LABEL_MOBILE,
    "workmobile": LABEL_MOBILE,
    "work": LABEL_WORK,
    "main": LABEL_WORK,
    "home": LABEL_HOME,
}


def _label(google_type: Optional[str]) -> Optional[str]:
    if not google_type:
        return None
    return _TO_LABEL.get(google_type.casefold(), LABEL_OTHER)


def _is_contact_entry(entry: Dict[str, Any]) -> bool:
    """True for data the user saved, False for Google-profile data."""
    source = (entry.get("metadata") or {}).get("source") or {}
    return source.get("type", "CONTACT") == "CONTACT"


def _primary(entries: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not entries:
        return None
    for entry in entries:
        if (entry.get("metadata") or {}).get("primary"):
            return entry
    return entries[0]


def person_to_contact(person: Dict[str, Any]) -> Contact:
    """Convert a People API ``Person`` into a :class:`Contact`."""
    name = _primary(person.get("names") or []) or {}
    org = _primary(person.get("organizations") or []) or {}
    bio = _primary(person.get("biographies") or []) or {}
    return Contact(
        contact_id=person.get("resourceName"),
        given_name=name.get("givenName") or None,
        family_name=name.get("familyName") or None,
        display_name=name.get("displayName") or None,
        company=org.get("name") or None,
        job_title=org.get("title") or None,
        emails=[
            ContactEmail(value=e["value"], label=_label(e.get("type")))
            for e in person.get("emailAddresses") or []
            if e.get("value")
        ],
        phones=[
            ContactPhone(value=p["value"], label=_label(p.get("type")))
            for p in person.get("phoneNumbers") or []
            if p.get("value")
        ],
        notes=bio.get("value") or None,
        raw=person,
    )


def contact_to_person(contact: Contact) -> Dict[str, Any]:
    """Build the People API body for ``contact``.

    When ``contact.raw`` holds the record Google returned, the body
    starts from it and only the differing parts are touched, so sub-
    fields this module doesn't model (custom labels, extra
    organisations, …) survive the round trip.
    """
    raw: Dict[str, Any] = contact.raw if isinstance(contact.raw, dict) else {}
    body: Dict[str, Any] = {
        key: copy.deepcopy(raw[key])
        for key in ("resourceName", "etag", "metadata")
        if key in raw
    }
    for field_name in _WRITABLE_FIELDS:
        entries = [
            copy.deepcopy(e) for e in raw.get(field_name) or [] if _is_contact_entry(e)
        ]
        if entries:
            body[field_name] = entries

    # Name — a contact holds exactly one. Compared against the name
    # Google shows (which may come from the person's profile) so an
    # unchanged name is never rewritten.
    shown = _primary(raw.get("names") or []) or {}
    wanted = (contact.given_name, contact.family_name)
    if (contact.given_name or contact.family_name) and wanted != (
        shown.get("givenName") or None,
        shown.get("familyName") or None,
    ):
        names = body.get("names") or []
        current = names[0] if names else {}
        if (current.get("givenName") or None, current.get("familyName") or None) != wanted:
            fresh = {
                k: v
                for k, v in (
                    ("givenName", contact.given_name),
                    ("familyName", contact.family_name),
                )
                if v
            }
            for keep in ("metadata", "middleName", "honorificPrefix", "honorificSuffix"):
                if keep in current:
                    fresh[keep] = current[keep]
            body["names"] = [fresh]

    # Company / job title live on the first organisation.
    shown_org = _primary(raw.get("organizations") or []) or {}
    org_updates = {
        key: value
        for key, value in (("name", contact.company), ("title", contact.job_title))
        if value and value != shown_org.get(key)
    }
    if org_updates:
        orgs = body.setdefault("organizations", [])
        if not orgs:
            orgs.append({})
        orgs[0].update(org_updates)

    all_emails = [e.get("value", "") for e in raw.get("emailAddresses") or []]
    for email in contact.emails:
        if any(v.casefold() == email.value.casefold() for v in all_emails):
            continue
        entry: Dict[str, Any] = {"value": email.value}
        if email.label:
            entry["type"] = email.label
        body.setdefault("emailAddresses", []).append(entry)

    all_phones = raw.get("phoneNumbers") or []
    for phone in contact.phones:
        writable = next(
            (
                p
                for p in body.get("phoneNumbers") or []
                if phones_match(p.get("value"), phone.value)
                or phones_match(p.get("canonicalForm"), phone.value)
            ),
            None,
        )
        if writable is not None:
            if phone.label and _label(writable.get("type")) != phone.label:
                writable["type"] = phone.label
                writable.pop("formattedType", None)
            continue
        if any(phones_match(p.get("value"), phone.value) for p in all_phones):
            continue  # present, but only on the read-only profile
        entry = {"value": phone.value}
        if phone.label:
            entry["type"] = phone.label
        body.setdefault("phoneNumbers", []).append(entry)

    shown_bio = _primary(raw.get("biographies") or []) or {}
    if contact.notes and contact.notes != shown_bio.get("value"):
        bios = body.setdefault("biographies", [])
        if bios:
            bios[0]["value"] = contact.notes
        else:
            bios.append({"value": contact.notes, "contentType": "TEXT_PLAIN"})

    return body


def _changed_fields(raw: Dict[str, Any], body: Dict[str, Any]) -> List[str]:
    changed = []
    for field_name in _WRITABLE_FIELDS:
        before = [e for e in raw.get(field_name) or [] if _is_contact_entry(e)]
        if before != (body.get(field_name) or []):
            changed.append(field_name)
    return changed


def _http_error(exc: HttpError) -> ContactsError:
    """Translate a People API failure into the adapter's error types."""
    status = getattr(exc.resp, "status", None) if exc.resp is not None else None
    message, api_status, reasons = str(exc), "", set()
    try:
        error = json.loads(exc.content.decode("utf-8")).get("error", {})
        message = error.get("message") or message
        api_status = error.get("status") or ""
        reasons = {
            d.get("reason", "") for d in error.get("details") or [] if isinstance(d, dict)
        }
    except Exception:  # noqa: BLE001 - keep the raw message
        pass
    lowered = message.casefold()

    if status == 401:
        return ContactsAuthExpired("google")
    if status == 403 and (
        "SERVICE_DISABLED" in reasons
        or "has not been used in project" in lowered
        or "it is disabled" in lowered
    ):
        return ContactsError(
            "The Google People API isn't switched on for Avi's Google Cloud "
            "project yet. A household admin needs to enable it under APIs & "
            "Services → Library → People API, then try again."
        )
    if status == 403 and (
        "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in reasons or "insufficient" in lowered
    ):
        return ContactsAuthExpired(
            "google",
            "The Google connection doesn't include permission to manage contacts.",
        )
    if status == 400 and api_status == "FAILED_PRECONDITION":
        return ContactConflict("The contact changed in Google while it was being updated.")
    if status == 404:
        return ContactsError("That contact no longer exists in Google Contacts.")
    if status == 429:
        return ContactsError(
            "Google Contacts is rate-limiting requests right now. Try again in a minute."
        )
    return ContactsError(f"Google Contacts HTTP {status}: {message}")


class GoogleContactsAdapter(ContactsAdapter):
    provider = "google"
    label = "Google Contacts"

    def __init__(
        self, creds: Optional[Credentials] = None, *, service: Any = None
    ) -> None:
        if service is None:
            if creds is None:
                raise ValueError("GoogleContactsAdapter needs credentials or a service.")
            service = build("people", "v1", credentials=creds, cache_discovery=False)
        self._svc = service

    def _run(self, request_factory: Callable[[], Any]) -> Dict[str, Any]:
        try:
            return request_factory().execute() or {}
        except HttpError as exc:
            raise _http_error(exc) from exc
        except RefreshError as exc:
            raise ContactsAuthExpired("google") from exc

    def list_all(self) -> List[Contact]:
        contacts: List[Contact] = []
        page_token: Optional[str] = None
        for _ in range(_MAX_PAGES):
            resp = self._run(
                lambda token=page_token: self._svc.people()
                .connections()
                .list(
                    resourceName="people/me",
                    pageSize=_PAGE_SIZE,
                    personFields=_PERSON_FIELDS,
                    pageToken=token,
                )
            )
            contacts.extend(person_to_contact(p) for p in resp.get("connections") or [])
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        else:
            logger.warning(
                "Google Contacts: stopped listing after %d pages; duplicate "
                "detection may be incomplete.",
                _MAX_PAGES,
            )
        return contacts

    def search(self, query: str, *, limit: int = 10) -> List[Contact]:
        query = (query or "").strip()
        if not query:
            return []
        # Google asks for an empty-query call first to refresh the index.
        self._run(
            lambda: self._svc.people().searchContacts(query="", readMask="names")
        )
        resp = self._run(
            lambda: self._svc.people().searchContacts(
                query=query,
                pageSize=max(1, min(limit, _SEARCH_MAX)),
                readMask=_PERSON_FIELDS,
            )
        )
        found = [
            person_to_contact(r["person"])
            for r in resp.get("results") or []
            if r.get("person")
        ]
        if found:
            return found[:limit]
        return filter_contacts(self.list_all(), query, limit=limit)

    def get(self, contact_id: str) -> Contact:
        person = self._run(
            lambda: self._svc.people().get(
                resourceName=contact_id, personFields=_PERSON_FIELDS
            )
        )
        return person_to_contact(person)

    def create(self, contact: Contact) -> Contact:
        body = contact_to_person(contact)
        person = self._run(
            lambda: self._svc.people().createContact(
                body=body, personFields=_PERSON_FIELDS
            )
        )
        return person_to_contact(person)

    def update(self, contact: Contact) -> Contact:
        if not contact.contact_id:
            raise ContactsError("Cannot update a contact that has no id.")
        if not isinstance(contact.raw, dict):
            # Without Google's own copy we'd overwrite fields we can't see.
            contact = replace(contact, raw=self.get(contact.contact_id).raw)
        body = contact_to_person(contact)
        changed = _changed_fields(contact.raw, body)
        if not changed:
            return contact
        person = self._run(
            lambda: self._svc.people().updateContact(
                resourceName=contact.contact_id,
                updatePersonFields=",".join(changed),
                personFields=_PERSON_FIELDS,
                body=body,
            )
        )
        return person_to_contact(person)

    def count(self) -> int:
        resp = self._run(
            lambda: self._svc.people()
            .connections()
            .list(resourceName="people/me", pageSize=1, personFields="names")
        )
        total = resp.get("totalItems", resp.get("totalPeople"))
        return int(total) if total is not None else len(self.list_all())


"""Contacts: adapters, find-or-update rules, permission requests, intake.

Google's People API is the only thing faked (an in-memory stand-in that
enforces etags and ``updatePersonFields`` the way the real one does).
Everything else — the merge rules, the per-person connection store, the
HTTP routes and their auth, the agent tools, vCard parsing, the
business-card intake and the Telegram contact share — runs for real
against the test database.
"""

from __future__ import annotations

import copy
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import httplib2
import pytest
from fastapi.testclient import TestClient
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError
from sqlalchemy import select

from api import models
from api.ai import document_intake, fast_action_router, tools, vision
from api.ai.document_intake import parse_analysis
from api.ai.schema_catalog import ALLOWED_TABLES
from api.ai.tools import InboundAttachmentRef, ToolContext, ToolError
from api.ai.tools.handlers import contacts as contact_tools
from api.auth import ROLE_MEMBER, sign_session
from api.config import get_settings
from api.integrations import google_oauth
from api.integrations.contacts import connections, messages, sync, vcard
from api.integrations.contacts.apple import AppleContactsAdapter
from api.integrations.contacts.base import (
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
from api.integrations.contacts.google import (
    CONTACTS_SCOPE,
    GoogleContactsAdapter,
    contact_to_person,
)
from api.services import attachment_intake


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeAdapter(ContactsAdapter):
    """An in-memory address book that honours the adapter contract."""

    provider = "google"
    label = "Google Contacts"

    def __init__(self, contacts: Optional[List[Contact]] = None) -> None:
        self.book: Dict[str, Contact] = {}
        self.writes: List[str] = []
        self.conflicts_to_raise = 0
        self.fail_with: Optional[Exception] = None
        for contact in contacts or []:
            self._store(contact)

    def _store(self, contact: Contact) -> Contact:
        contact_id = contact.contact_id or f"people/c{len(self.book) + 1}"
        saved = copy.deepcopy(contact)
        saved.contact_id = contact_id
        self.book[contact_id] = saved
        return copy.deepcopy(saved)

    def _check(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    def list_all(self) -> List[Contact]:
        self._check()
        return [copy.deepcopy(c) for c in self.book.values()]

    def search(self, query: str, *, limit: int = 10) -> List[Contact]:
        return sync.filter_contacts(self.list_all(), query, limit=limit)

    def get(self, contact_id: str) -> Contact:
        self._check()
        return copy.deepcopy(self.book[contact_id])

    def create(self, contact: Contact) -> Contact:
        self._check()
        self.writes.append("create")
        return self._store(contact)

    def update(self, contact: Contact) -> Contact:
        self._check()
        if self.conflicts_to_raise:
            self.conflicts_to_raise -= 1
            raise ContactConflict("changed underneath us")
        self.writes.append("update")
        return self._store(contact)


def _dana(**overrides: Any) -> Contact:
    base = dict(
        given_name="Dana",
        family_name="Whitfield",
        company="Old Co",
        job_title="Analyst",
        emails=[ContactEmail("dana@acme.com", "work")],
        phones=[ContactPhone("(212) 555-0100", "work")],
    )
    base.update(overrides)
    return Contact(**base)


def _draft(**fields: Any) -> Contact:
    return Contact(**fields)


def _api_error(status: int, api_status: str = "", message: str = "boom", reason: str = "") -> HttpError:
    error: Dict[str, Any] = {"code": status, "message": message, "status": api_status}
    if reason:
        error["details"] = [{"reason": reason}]
    return HttpError(
        httplib2.Response({"status": status}), json.dumps({"error": error}).encode()
    )


class _Request:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


def _contact_source(entry: Dict[str, Any]) -> bool:
    return (entry.get("metadata") or {}).get("source", {}).get("type", "CONTACT") == "CONTACT"


class FakePeopleService:
    """Stand-in for ``build('people', 'v1')`` with the behaviours that matter.

    * ``connections.list`` pages.
    * ``updateContact`` rejects a stale etag or a body without
      ``metadata.sources``, and REPLACES every field named in
      ``updatePersonFields`` (so a careless update visibly loses data).
    * ``searchContacts`` reads a separate index that can lag the store.
    """

    def __init__(self, people: Optional[List[Dict[str, Any]]] = None, *, page_size: Optional[int] = None):
        self.store: Dict[str, Dict[str, Any]] = {
            p["resourceName"]: copy.deepcopy(p) for p in people or []
        }
        self.page_size = page_size
        self.search_index: Optional[List[Dict[str, Any]]] = None
        self.calls: List[tuple] = []
        self.fail: Optional[HttpError] = None

    # The real client chains .people().connections().list(...)
    def people(self):
        return self

    def connections(self):
        return self

    def _record(self, name: str, kwargs: Dict[str, Any]) -> None:
        self.calls.append((name, kwargs))
        if self.fail is not None:
            raise self.fail

    def list(self, **kwargs):
        def run():
            self._record("list", kwargs)
            items = list(self.store.values())
            size = self.page_size or kwargs["pageSize"]
            start = int(kwargs.get("pageToken") or 0)
            resp: Dict[str, Any] = {
                "connections": copy.deepcopy(items[start : start + size]),
                "totalItems": len(items),
            }
            if start + size < len(items):
                resp["nextPageToken"] = str(start + size)
            return resp

        return _Request(run)

    def searchContacts(self, **kwargs):
        def run():
            self._record("search", kwargs)
            if not kwargs["query"]:
                return {}
            pool = self.search_index if self.search_index is not None else list(self.store.values())
            hits = [
                p for p in pool
                if kwargs["query"].lower() in json.dumps(p).lower()
            ]
            return {"results": [{"person": copy.deepcopy(p)} for p in hits]}

        return _Request(run)

    def get(self, **kwargs):
        def run():
            self._record("get", kwargs)
            if kwargs["resourceName"] not in self.store:
                raise _api_error(404, "NOT_FOUND")
            return copy.deepcopy(self.store[kwargs["resourceName"]])

        return _Request(run)

    def createContact(self, **kwargs):
        def run():
            self._record("create", kwargs)
            person = copy.deepcopy(kwargs["body"])
            person["resourceName"] = f"people/c{900 + len(self.store)}"
            person["etag"] = "etag-1"
            person["metadata"] = {"sources": [{"type": "CONTACT", "etag": "etag-1"}]}
            self.store[person["resourceName"]] = person
            return copy.deepcopy(person)

        return _Request(run)

    def updateContact(self, **kwargs):
        def run():
            self._record("update", kwargs)
            current = self.store[kwargs["resourceName"]]
            body = kwargs["body"]
            if not (body.get("metadata") or {}).get("sources"):
                raise _api_error(400, "INVALID_ARGUMENT", "person.metadata.sources is required")
            if body.get("etag") != current["etag"]:
                raise _api_error(400, "FAILED_PRECONDITION", "etag mismatch")
            for field_name in kwargs["updatePersonFields"].split(","):
                read_only = [e for e in current.get(field_name, []) if not _contact_source(e)]
                current[field_name] = read_only + copy.deepcopy(body.get(field_name, []))
            current["etag"] = current["etag"] + "+"
            current["metadata"]["sources"][0]["etag"] = current["etag"]
            return copy.deepcopy(current)

        return _Request(run)


def _google_person(**overrides: Any) -> Dict[str, Any]:
    person = {
        "resourceName": "people/c1",
        "etag": "etag-a",
        "metadata": {"sources": [{"type": "CONTACT", "id": "1", "etag": "etag-a"}]},
        "names": [
            {
                "metadata": {"primary": True, "source": {"type": "CONTACT", "id": "1"}},
                "givenName": "Dana",
                "familyName": "Whitfield",
                "displayName": "Dana Whitfield",
            }
        ],
        "emailAddresses": [
            {"metadata": {"source": {"type": "CONTACT", "id": "1"}}, "value": "dana@acme.com", "type": "work"},
            # From Dana's own Google profile — visible, but not ours to write.
            {"metadata": {"source": {"type": "PROFILE", "id": "p9"}}, "value": "dana.w@gmail.com"},
        ],
        "phoneNumbers": [
            {
                "metadata": {"source": {"type": "CONTACT", "id": "1"}},
                "value": "(212) 555-0100",
                "canonicalForm": "+12125550100",
                "type": "Assistant line",
                "formattedType": "Assistant line",
            }
        ],
        "organizations": [
            {"metadata": {"source": {"type": "CONTACT", "id": "1"}}, "name": "Old Co", "title": "Analyst", "department": "Research"},
            {"metadata": {"source": {"type": "CONTACT", "id": "1"}}, "name": "Board of Trustees"},
        ],
    }
    person.update(overrides)
    return person


@pytest.fixture
def people(db, test_family):
    """Two family members (removed afterwards, along with their connections)."""
    fid = test_family["family_id"]
    tag = uuid.uuid4().hex[:6]
    rows = {
        "ben": models.Person(
            family_id=fid, first_name=f"Ben{tag}", last_name="Contacts",
            primary_family_relationship="parent",
            email_address=f"ben.{tag}@example.com",
            telegram_user_id=int(uuid.uuid4().int % 10**9) + 10**9,
        ),
        "sarah": models.Person(
            family_id=fid, first_name=f"Sarah{tag}", last_name="Contacts",
            primary_family_relationship="parent",
            email_address=f"sarah.{tag}@example.com",
        ),
    }
    db.add_all(rows.values())
    db.commit()
    yield rows
    db.rollback()
    ids = [p.person_id for p in rows.values()]
    for model in (models.ContactProviderConnection, models.PendingAttachment):
        for row in db.execute(select(model).where(model.person_id.in_(ids))).scalars():
            db.delete(row)
    db.commit()
    for p in db.execute(select(models.Person).where(models.Person.person_id.in_(ids))).scalars():
        db.delete(p)
    db.commit()


def _creds(*, token: Optional[str] = "access-token") -> Credentials:
    """A grant with a live access token (``token=None`` → needs a refresh)."""
    return Credentials(
        token=token,
        refresh_token="refresh-secret-value",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="client-id",
        client_secret="client-secret",
        scopes=[CONTACTS_SCOPE],
        # google-auth compares expiry as naive UTC.
        expiry=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1),
    )


@pytest.fixture(autouse=True)
def _no_real_google_calls(monkeypatch):
    """Nothing in this module may reach Google; fail loudly if it tries."""

    def blocked(self, request):
        raise AssertionError("test tried to refresh a token against Google")

    monkeypatch.setattr(Credentials, "refresh", blocked)


def _connect(db, person, *, scopes=(CONTACTS_SCOPE,), account=None, creds=None):
    row = connections.save_google_connection(
        db,
        person_id=person.person_id,
        account_identifier=account or person.email_address,
        creds=creds or _creds(),
        scopes=list(scopes),
    )
    db.commit()
    return row


def _ctx(db, test_family, person) -> ToolContext:
    return ToolContext(
        db=db,
        family_id=test_family["family_id"],
        assistant_id=test_family["assistant_id"],
        person_id=person.person_id if person is not None else None,
    )


@pytest.fixture
def address_book(monkeypatch):
    """Give the tools a connected (fake) address book for whoever asks."""
    adapter = FakeAdapter([_dana()])
    asked: List[int] = []

    def fake(db, person_id, provider=None):
        asked.append(person_id)
        return adapter

    monkeypatch.setattr(connections, "adapter_for_person", fake)
    adapter.asked = asked  # type: ignore[attr-defined]
    return adapter


# ---------------------------------------------------------------------------
# Find-or-update rules (provider neutral)
# ---------------------------------------------------------------------------


def test_new_person_is_created_with_mobile_first():
    book = FakeAdapter()
    result = sync.upsert(
        book,
        _draft(
            display_name="Priya Raman",
            company="Northwind",
            job_title="VP Sales",
            emails=[ContactEmail(" Priya@Northwind.com ")],
            phones=[ContactPhone("212-555-0199", "work"), ContactPhone("917-555-0142", "cell")],
        ),
    )
    assert result.action == sync.ACTION_CREATED
    saved = result.contact
    assert (saved.given_name, saved.family_name) == ("Priya", "Raman")
    assert saved.emails == [ContactEmail("Priya@Northwind.com")]
    # "cell" is understood as mobile, and mobile leads.
    assert saved.phones[0] == ContactPhone("917-555-0142", "mobile")
    assert book.writes == ["create"]


def test_existing_contact_is_found_by_email_and_updated_not_duplicated():
    book = FakeAdapter([_dana()])
    result = sync.upsert(
        book,
        _draft(
            display_name="Dana Whitfield",
            company="Acme Corp",
            job_title="Director of Ops",
            emails=[ContactEmail("DANA@ACME.COM")],
            phones=[ContactPhone("+1 415 555 0142", "mobile")],
        ),
    )
    assert result.action == sync.ACTION_UPDATED
    assert result.matched_on == "email"
    assert len(book.book) == 1 and book.writes == ["update"]
    saved = result.contact
    assert saved.company == "Acme Corp" and saved.job_title == "Director of Ops"
    # The number already on file is kept; the mobile is added.
    assert [p.value for p in saved.phones] == ["(212) 555-0100", "+1 415 555 0142"]
    assert [e.value for e in saved.emails] == ["dana@acme.com"]
    assert "company: Old Co → Acme Corp" in result.changes
    assert "added mobile phone +1 415 555 0142" in result.changes


def test_existing_contact_is_found_by_phone_whatever_the_formatting():
    book = FakeAdapter([_dana(emails=[])])
    result = sync.upsert(
        book,
        _draft(
            display_name="D. Whitfield",
            emails=[ContactEmail("dana@newjob.com")],
            phones=[ContactPhone("+1 212.555.0100 x12")],
        ),
    )
    assert result.action == sync.ACTION_UPDATED and result.matched_on == "phone"
    assert [e.value for e in result.contact.emails] == ["dana@newjob.com"]
    # A different spelling of the name never silently renames the contact.
    assert result.contact.given_name == "Dana"
    assert any("kept the existing name" in s for s in result.skipped)


def test_same_name_alone_is_not_enough_to_write():
    book = FakeAdapter([_dana()])
    draft = _draft(
        display_name="Dana Whitfield",
        emails=[ContactEmail("dana@elsewhere.org")],
        phones=[ContactPhone("650-555-0111", "mobile")],
    )
    result = sync.upsert(book, draft)
    assert result.action == sync.ACTION_NEEDS_CONFIRMATION
    assert result.matched_on == "name"
    assert [c.contact_id for c in result.candidates] == ["people/c1"]
    assert book.writes == []

    # "Yes, same Dana" → update the one they picked.
    same = sync.upsert(book, draft, contact_id="people/c1")
    assert same.action == sync.ACTION_UPDATED and len(book.book) == 1
    assert len(same.contact.emails) == 2


def test_same_name_different_person_can_be_added_separately():
    book = FakeAdapter([_dana()])
    result = sync.upsert(
        book,
        _draft(display_name="Dana Whitfield", emails=[ContactEmail("other.dana@x.org")]),
        force_new=True,
    )
    assert result.action == sync.ACTION_CREATED
    assert len(book.book) == 2


def test_duplicates_already_in_the_address_book_are_not_guessed_between():
    book = FakeAdapter([_dana(), _dana(company="Second Copy")])
    result = sync.upsert(book, _draft(emails=[ContactEmail("dana@acme.com")], company="Acme"))
    assert result.action == sync.ACTION_NEEDS_CONFIRMATION
    assert len(result.candidates) == 2
    assert book.writes == []


def test_nothing_new_means_nothing_written():
    book = FakeAdapter([_dana()])
    result = sync.upsert(
        book,
        _draft(
            display_name="Dana Whitfield", company="old co",
            emails=[ContactEmail("dana@acme.com")], phones=[ContactPhone("2125550100", "work")],
        ),
    )
    assert result.action == sync.ACTION_UNCHANGED
    assert book.writes == []


def test_unlabelled_number_is_relabelled_mobile_not_duplicated():
    book = FakeAdapter([_dana(phones=[ContactPhone("415-555-0142", "other")])])
    result = sync.upsert(
        book,
        _draft(emails=[ContactEmail("dana@acme.com")], phones=[ContactPhone("(415) 555-0142", "mobile")]),
    )
    assert result.contact.phones == [ContactPhone("415-555-0142", "mobile")]
    assert result.changes == ["labelled 415-555-0142 as mobile"]


def test_last_name_is_filled_in_and_rename_needs_to_be_asked_for():
    book = FakeAdapter([_dana(family_name=None)])
    filled = sync.upsert(
        book, _draft(display_name="Dana Whitfield", emails=[ContactEmail("dana@acme.com")])
    )
    assert filled.contact.family_name == "Whitfield"

    kept = sync.upsert(
        book, _draft(display_name="Dana Okafor", emails=[ContactEmail("dana@acme.com")])
    )
    assert kept.action == sync.ACTION_UNCHANGED and kept.contact.family_name == "Whitfield"

    renamed = sync.upsert(
        book,
        _draft(display_name="Dana Okafor", emails=[ContactEmail("dana@acme.com")]),
        overwrite_name=True,
    )
    assert renamed.contact.family_name == "Okafor"


def test_concurrent_edit_is_reread_and_merged_not_overwritten():
    book = FakeAdapter([_dana()])
    book.conflicts_to_raise = 1
    result = sync.upsert(
        book, _draft(emails=[ContactEmail("dana@acme.com")], phones=[ContactPhone("415-555-0142", "mobile")])
    )
    assert result.action == sync.ACTION_UPDATED
    assert book.writes == ["update"]


def test_a_contact_needs_something_to_identify_it():
    with pytest.raises(ContactsError, match="at least a name"):
        sync.upsert(FakeAdapter(), _draft(company="Acme"))


@pytest.mark.parametrize(
    "a,b,same",
    [
        ("(415) 555-0142", "+14155550142", True),
        ("415.555.0142 ext. 9", "4155550142", True),
        ("020 7946 0958", "+44 20 7946 0958", True),
        ("555-0142", "415-555-0142", False),
        ("415-555-0142", "415-555-0143", False),
    ],
)
def test_phone_numbers_compare_by_digits(a, b, same):
    assert sync.phones_match(a, b) is same


# ---------------------------------------------------------------------------
# Google adapter
# ---------------------------------------------------------------------------


def test_google_lists_every_page():
    svc = FakePeopleService(
        [_google_person(resourceName=f"people/c{i}") for i in range(5)], page_size=2
    )
    contacts = GoogleContactsAdapter(service=svc).list_all()
    assert len(contacts) == 5
    assert [c[0] for c in svc.calls] == ["list", "list", "list"]


def test_google_update_keeps_everything_it_does_not_understand():
    svc = FakePeopleService([_google_person()])
    adapter = GoogleContactsAdapter(service=svc)

    result = sync.upsert(
        adapter,
        _draft(
            display_name="Dana Whitfield",
            company="Acme Corp",
            emails=[ContactEmail("dana@acme.com")],
            phones=[ContactPhone("415-555-0142", "mobile")],
        ),
    )
    assert result.action == sync.ACTION_UPDATED

    name, sent = svc.calls[-1]
    assert name == "update"
    # Only the fields that changed are named, so nothing else can be wiped.
    assert sent["updatePersonFields"] == "phoneNumbers,organizations"
    assert sent["body"]["etag"] == "etag-a"
    # Profile data is not ours to send back.
    assert all(_contact_source(e) for e in sent["body"]["emailAddresses"])

    stored = svc.store["people/c1"]
    assert [p["value"] for p in stored["phoneNumbers"]] == ["(212) 555-0100", "415-555-0142"]
    # The custom label and the second organisation survive the round trip.
    assert stored["phoneNumbers"][0]["type"] == "Assistant line"
    assert stored["phoneNumbers"][1]["type"] == "mobile"
    assert stored["organizations"][0] == {
        "metadata": {"source": {"type": "CONTACT", "id": "1"}},
        "name": "Acme Corp", "title": "Analyst", "department": "Research",
    }
    assert stored["organizations"][1]["name"] == "Board of Trustees"
    assert len(stored["emailAddresses"]) == 2
    assert stored["names"][0]["displayName"] == "Dana Whitfield"


def test_google_does_not_re_add_an_email_that_is_on_the_persons_profile():
    svc = FakePeopleService([_google_person()])
    result = sync.upsert(
        GoogleContactsAdapter(service=svc),
        _draft(emails=[ContactEmail("dana.w@gmail.com")], display_name="Dana Whitfield"),
    )
    assert result.action == sync.ACTION_UNCHANGED
    assert "update" not in [c[0] for c in svc.calls]


def test_google_create_sends_a_clean_contact():
    svc = FakePeopleService()
    result = sync.upsert(
        GoogleContactsAdapter(service=svc),
        _draft(
            display_name="Priya Raman", company="Northwind", job_title="VP Sales",
            emails=[ContactEmail("priya@northwind.com")],
            phones=[ContactPhone("917-555-0142", "mobile")], notes="Met at the Denver expo.",
        ),
    )
    assert result.action == sync.ACTION_CREATED
    body = svc.calls[-1][1]["body"]
    assert body == {
        "names": [{"givenName": "Priya", "familyName": "Raman"}],
        "organizations": [{"name": "Northwind", "title": "VP Sales"}],
        "emailAddresses": [{"value": "priya@northwind.com"}],
        "phoneNumbers": [{"value": "917-555-0142", "type": "mobile"}],
        "biographies": [{"value": "Met at the Denver expo.", "contentType": "TEXT_PLAIN"}],
    }
    assert result.contact.contact_id.startswith("people/c")


def test_google_stale_etag_is_retried_against_the_fresh_contact():
    svc = FakePeopleService([_google_person()])
    adapter = GoogleContactsAdapter(service=svc)
    stale = adapter.get("people/c1")
    svc.store["people/c1"]["etag"] = "etag-b"  # edited on the phone meanwhile
    svc.store["people/c1"]["metadata"]["sources"][0]["etag"] = "etag-b"
    stale.phones.append(ContactPhone("415-555-0142", "mobile"))
    with pytest.raises(ContactConflict):
        adapter.update(stale)

    # upsert reads, hits the same race, and recovers by re-reading.
    svc.store["people/c1"]["etag"] = "etag-a"
    svc.store["people/c1"]["metadata"]["sources"][0]["etag"] = "etag-a"
    original_list = svc.list

    def list_then_edit(**kwargs):
        request = original_list(**kwargs)

        def run():
            resp = request.execute()
            svc.store["people/c1"]["etag"] = "etag-c"
            svc.store["people/c1"]["metadata"]["sources"][0]["etag"] = "etag-c"
            return resp

        return _Request(run)

    svc.list = list_then_edit  # type: ignore[method-assign]
    result = sync.upsert(
        adapter, _draft(emails=[ContactEmail("dana@acme.com")], phones=[ContactPhone("415-555-0142", "mobile")])
    )
    assert result.action == sync.ACTION_UPDATED
    assert len(svc.store["people/c1"]["phoneNumbers"]) == 2


def test_google_search_warms_the_index_and_falls_back_when_it_lags():
    svc = FakePeopleService([_google_person()])
    adapter = GoogleContactsAdapter(service=svc)
    assert [c.name for c in adapter.search("Whitfield")] == ["Dana Whitfield"]
    assert [(n, k.get("query")) for n, k in svc.calls] == [("search", ""), ("search", "Whitfield")]

    svc.search_index = []  # a contact added a minute ago isn't indexed yet
    assert [c.name for c in adapter.search("dana@acme.com")] == ["Dana Whitfield"]
    assert adapter.search("nobody") == []


def test_google_count_does_not_download_the_address_book():
    svc = FakePeopleService([_google_person(resourceName=f"people/c{i}") for i in range(7)])
    assert GoogleContactsAdapter(service=svc).count() == 7
    assert svc.calls[-1][1]["pageSize"] == 1


@pytest.mark.parametrize(
    "error,expected,text",
    [
        (_api_error(401, "UNAUTHENTICATED"), ContactsAuthExpired, "expired"),
        (
            _api_error(403, "PERMISSION_DENIED", "Request had insufficient authentication scopes.",
                       "ACCESS_TOKEN_SCOPE_INSUFFICIENT"),
            ContactsAuthExpired, "permission to manage contacts",
        ),
        (
            _api_error(403, "PERMISSION_DENIED", "People API has not been used in project 1 before",
                       "SERVICE_DISABLED"),
            ContactsError, "People API isn't switched on",
        ),
        (_api_error(429, "RESOURCE_EXHAUSTED"), ContactsError, "rate-limiting"),
    ],
)
def test_google_failures_become_actionable_errors(error, expected, text):
    svc = FakePeopleService([_google_person()])
    svc.fail = error
    with pytest.raises(expected, match=text) as caught:
        GoogleContactsAdapter(service=svc).list_all()
    # A disabled API is an admin problem, not a "reconnect" problem.
    assert (type(caught.value) is ContactsAuthExpired) == (expected is ContactsAuthExpired)


def test_google_body_for_a_profile_only_name_is_left_alone():
    """A name that only exists on the person's Google profile isn't copied in."""
    raw = _google_person(
        names=[{"metadata": {"primary": True, "source": {"type": "PROFILE", "id": "p9"}},
                "givenName": "Dana", "familyName": "Whitfield"}]
    )
    contact = Contact(contact_id="people/c1", given_name="Dana", family_name="Whitfield", raw=raw)
    assert "names" not in contact_to_person(contact)


# ---------------------------------------------------------------------------
# Apple placeholder + provider registry
# ---------------------------------------------------------------------------


def test_google_is_the_default_and_apple_is_a_registered_placeholder(monkeypatch):
    assert set(connections.PROVIDERS) == {"google", "apple"}
    assert connections.default_provider() == "google"
    assert connections.PROVIDERS["google"].available is True
    assert connections.PROVIDERS["apple"].available is False

    # A typo in config can't leave the app with no provider.
    monkeypatch.setattr(get_settings(), "CONTACTS_DEFAULT_PROVIDER", "outlook")
    assert connections.default_provider() == "google"


def test_apple_adapter_refuses_every_operation():
    apple = AppleContactsAdapter()
    for call in (
        apple.list_all,
        apple.count,
        lambda: apple.search("dana"),
        lambda: apple.get("x"),
        lambda: apple.create(_dana()),
        lambda: apple.update(_dana()),
    ):
        with pytest.raises(ContactsNotSupported, match="isn't supported yet"):
            call()


def test_asking_for_apple_says_not_supported(db, people):
    with pytest.raises(ContactsNotSupported):
        connections.adapter_for_person(db, people["ben"].person_id, "apple")


# ---------------------------------------------------------------------------
# Per-person connections
# ---------------------------------------------------------------------------


def test_connection_is_per_person_and_encrypted(db, people):
    ben, sarah = people["ben"], people["sarah"]
    row = _connect(db, ben)

    assert b"refresh-secret-value" not in bytes(row.credentials_encrypted)
    assert row.account_identifier == ben.email_address
    assert isinstance(connections.adapter_for_person(db, ben.person_id), GoogleContactsAdapter)
    # Ben connecting says nothing about Sarah.
    with pytest.raises(ContactsNotConnected):
        connections.adapter_for_person(db, sarah.person_id)
    # The LLM's SQL tool can't see the table at all.
    assert "contact_provider_connections" not in ALLOWED_TABLES


def test_reconnecting_replaces_the_grant_instead_of_adding_a_second(db, people):
    ben = people["ben"]
    _connect(db, ben)
    connections.mark_needs_reconnect(db, ben.person_id, "google")
    _connect(db, ben, account="ben.other@example.com")
    rows = connections.list_connections(db, ben.person_id)
    assert len(rows) == 1
    assert rows[0].account_identifier == "ben.other@example.com"
    assert rows[0].needs_reconnect is False


def test_grant_without_the_contacts_permission_is_not_usable(db, people):
    _connect(db, people["ben"], scopes=("openid",))
    with pytest.raises(ContactsAuthExpired):
        connections.adapter_for_person(db, people["ben"].person_id)


def test_revoked_grant_is_remembered_so_avi_asks_instead_of_retrying(db, people, monkeypatch):
    ben = people["ben"]
    _connect(db, ben, creds=_creds(token=None))

    def refuse(self, request):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr(Credentials, "refresh", refuse)
    with pytest.raises(ContactsAuthExpired):
        connections.adapter_for_person(db, ben.person_id)
    db.expire_all()
    assert connections.get_connection(db, ben.person_id, "google").needs_reconnect is True


def test_a_network_blip_while_refreshing_does_not_demand_a_reconnect(db, people, monkeypatch):
    ben = people["ben"]
    _connect(db, ben, creds=_creds(token=None))

    def offline(self, request):
        raise ConnectionError("no route to host")

    monkeypatch.setattr(Credentials, "refresh", offline)
    with pytest.raises(ContactsError) as caught:
        connections.adapter_for_person(db, ben.person_id)
    assert not isinstance(caught.value, ContactsAuthExpired)
    db.expire_all()
    assert connections.get_connection(db, ben.person_id, "google").needs_reconnect is False


def test_disconnect_revokes_at_google(db, people, monkeypatch):
    ben = people["ben"]
    _connect(db, ben)
    revoked: List[dict] = []
    monkeypatch.setattr(
        connections.httpx, "post", lambda url, **kw: revoked.append({"url": url, **kw})
    )
    assert connections.delete_connection(db, ben.person_id, "google") is True
    db.commit()
    assert revoked and revoked[0]["params"] == {"token": "refresh-secret-value"}
    assert connections.get_connection(db, ben.person_id) is None
    assert connections.delete_connection(db, ben.person_id, "google") is False


def test_disconnect_never_revokes_the_account_avi_himself_runs_on(
    db, people, test_family, monkeypatch
):
    """Revoking is per Google account + OAuth client, and Avi's Gmail uses the same client."""
    ben = people["ben"]
    shared = f"avi.{uuid.uuid4().hex[:6]}@example.com"
    avi = google_oauth.save_credentials(
        db, assistant_id=test_family["assistant_id"], granted_email=shared.upper(), creds=_creds()
    )
    _connect(db, ben, account=shared)
    revoked: List[str] = []
    monkeypatch.setattr(connections.httpx, "post", lambda url, **kw: revoked.append(url))
    try:
        assert connections.delete_connection(db, ben.person_id, "google") is True
        db.commit()
        assert revoked == []
    finally:
        db.delete(avi)
        db.commit()


# ---------------------------------------------------------------------------
# HTTP: status, consent flow, disconnect, test
# ---------------------------------------------------------------------------


def _member_client(app, person) -> TestClient:
    tc = TestClient(app)
    tc.cookies.set(
        get_settings().SESSION_COOKIE_NAME,
        sign_session(
            email=person.email_address, role=ROLE_MEMBER,
            person_id=person.person_id, family_id=person.family_id,
        ),
    )
    return tc


@pytest.fixture
def google_client_configured(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "test-client.apps.googleusercontent.com")
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_SECRET", "test-secret")
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_REDIRECT_URI", "https://avi.example.com/api/admin/google/oauth/callback")
    monkeypatch.setattr(settings, "GOOGLE_CONTACTS_OAUTH_REDIRECT_URI", None)
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_POST_LOGIN_REDIRECT", "https://avi.example.com/admin")
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://avi.example.com")
    return settings


def test_status_lists_providers_and_this_persons_connection(client, db, people):
    ben = people["ben"]
    before = client.get("/api/contacts/status", params={"person_id": ben.person_id}).json()
    by_provider = {p["provider"]: p for p in before["providers"]}
    assert by_provider["google"] == {
        "provider": "google", "label": "Google Contacts", "available": True,
        "is_default": True, "connected": False, "account_identifier": None,
        "needs_reconnect": False, "account_matches_person": None, "connected_at": None,
    }
    assert by_provider["apple"]["available"] is False and by_provider["apple"]["connected"] is False
    assert "never deletes" in before["permission_summary"]

    _connect(db, ben, account="someone.else@example.com")
    after = client.get("/api/contacts/status", params={"person_id": ben.person_id}).json()
    google = {p["provider"]: p for p in after["providers"]}["google"]
    assert google["connected"] is True
    assert google["account_identifier"] == "someone.else@example.com"
    # Flags that the linked account isn't the person's own address.
    assert google["account_matches_person"] is False
    # No credential material ever leaves the server.
    assert "refresh" not in json.dumps(after).lower()


def test_contacts_routes_need_a_login(app, people):
    anonymous = TestClient(app)
    pid = people["ben"].person_id
    assert anonymous.get("/api/contacts/status", params={"person_id": pid}).status_code == 401
    assert anonymous.delete("/api/contacts/connection", params={"person_id": pid}).status_code == 401
    assert anonymous.post("/api/contacts/test", params={"person_id": pid}).status_code == 401


def test_a_member_manages_only_their_own_connection(app, db, people, google_client_configured):
    ben, sarah = people["ben"], people["sarah"]
    _connect(db, sarah)
    as_ben = _member_client(app, ben)

    assert as_ben.get("/api/contacts/status", params={"person_id": ben.person_id}).status_code == 200
    for response in (
        as_ben.get("/api/contacts/status", params={"person_id": sarah.person_id}),
        as_ben.get(
            "/api/contacts/google/oauth/start", params={"person_id": sarah.person_id},
            follow_redirects=False,
        ),
        as_ben.delete("/api/contacts/connection", params={"person_id": sarah.person_id}),
        as_ben.post("/api/contacts/test", params={"person_id": sarah.person_id}),
        # Not even a hint about whether a person id exists.
        as_ben.get("/api/contacts/status", params={"person_id": 987654321}),
    ):
        assert response.status_code == 403, response.text
    db.expire_all()
    assert connections.get_connection(db, sarah.person_id) is not None


def test_connect_link_sends_a_signed_out_person_to_log_in_first(app, people):
    pid = people["ben"].person_id
    response = TestClient(app).get(
        "/api/contacts/google/oauth/start", params={"person_id": pid}, follow_redirects=False
    )
    assert response.status_code == 302
    target = urlparse(response.headers["location"])
    assert target.path == "/login"
    assert parse_qs(target.query)["next"] == [f"/api/contacts/google/oauth/start?person_id={pid}"]


def test_connect_asks_google_for_contacts_and_nothing_more(app, people, google_client_configured):
    ben = people["ben"]
    response = _member_client(app, ben).get(
        "/api/contacts/google/oauth/start",
        params={"person_id": ben.person_id},
        follow_redirects=False,
    )
    assert response.status_code == 302
    target = urlparse(response.headers["location"])
    query = parse_qs(target.query)
    assert target.netloc == "accounts.google.com"
    assert set(query["scope"][0].split()) == {
        "openid", "https://www.googleapis.com/auth/userinfo.email", CONTACTS_SCOPE,
    }
    assert query["access_type"] == ["offline"] and query["prompt"] == ["consent"]
    assert query["login_hint"] == [ben.email_address]
    # Must not inherit Gmail / Calendar grants the same account made to this client.
    assert "include_granted_scopes" not in query
    assert query["redirect_uri"] == ["https://avi.example.com/api/contacts/google/oauth/callback"]
    pending = connections.consume_pending(query["state"][0])
    assert pending.person_id == ben.person_id


class _FakeFlow:
    def __init__(self, granted: List[str], *, refresh_token: Optional[str] = "refresh-secret-value"):
        self.credentials = Credentials(
            token="access-token", refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id="client-id", client_secret="client-secret", scopes=[CONTACTS_SCOPE],
        )
        self.oauth2session = SimpleNamespace(token={"scope": " ".join(granted)})
        self.code_verifier = None
        self.exchanged: Optional[str] = None

    def fetch_token(self, *, code: str) -> None:
        self.exchanged = code


def _callback(app, monkeypatch, person, *, granted, return_to=None, account="ben.personal@gmail.com", **flow_kw):
    flow = _FakeFlow(granted, **flow_kw)
    monkeypatch.setattr(connections, "build_google_flow", lambda: flow)
    monkeypatch.setattr(google_oauth, "email_from_id_token", lambda creds: account)
    state = connections.new_state()
    connections.stash_pending(
        state, person_id=person.person_id, code_verifier="pkce-verifier", return_to=return_to
    )
    # No session cookie: the browser arrives straight from Google.
    response = TestClient(app).get(
        "/api/contacts/google/oauth/callback",
        params={"code": "auth-code", "state": state},
        follow_redirects=False,
    )
    return response, flow, state


def test_callback_stores_the_grant_for_the_person_who_started(app, db, people, monkeypatch):
    ben = people["ben"]
    response, flow, state = _callback(
        app, monkeypatch, ben, granted=["openid", CONTACTS_SCOPE]
    )
    assert response.status_code == 200
    assert "Google Contacts connected" in response.text
    assert "ben.personal@gmail.com" in response.text
    assert flow.exchanged == "auth-code" and flow.code_verifier == "pkce-verifier"

    row = connections.get_connection(db, ben.person_id, "google")
    assert row.account_identifier == "ben.personal@gmail.com"
    assert connections.has_contacts_scope(row)

    # The state is single-use: replaying the callback changes nothing.
    replay = TestClient(app).get(
        "/api/contacts/google/oauth/callback", params={"code": "auth-code", "state": state}
    )
    assert replay.status_code == 400 and "expired or was already used" in replay.text


def test_callback_refuses_when_the_contacts_box_was_unticked(app, db, people, monkeypatch):
    ben = people["ben"]
    response, _flow, _state = _callback(app, monkeypatch, ben, granted=["openid"])
    assert response.status_code == 400
    assert "contacts permission wasn&#x27;t ticked" in response.text
    assert connections.get_connection(db, ben.person_id) is None


def test_callback_refuses_a_grant_that_cannot_be_refreshed(app, db, people, monkeypatch):
    ben = people["ben"]
    response, _flow, _state = _callback(
        app, monkeypatch, ben, granted=[CONTACTS_SCOPE], refresh_token=None
    )
    assert response.status_code == 400
    assert connections.get_connection(db, ben.person_id) is None


def test_callback_returns_the_admin_to_the_page_they_came_from(
    app, db, people, monkeypatch, google_client_configured
):
    ben = people["ben"]
    back = f"/admin/families/{ben.family_id}/people/{ben.person_id}"
    response, _flow, _state = _callback(
        app, monkeypatch, ben, granted=[CONTACTS_SCOPE], return_to=back
    )
    assert response.status_code == 302
    # A plain path: the browser stays on the site the callback was served from.
    assert response.headers["location"] == f"{back}?contacts_connected=ben.personal%40gmail.com"


def test_declining_at_google_connects_nothing(app, db, people):
    ben = people["ben"]
    state = connections.new_state()
    connections.stash_pending(state, person_id=ben.person_id, code_verifier=None)
    response = TestClient(app).get(
        "/api/contacts/google/oauth/callback", params={"error": "access_denied", "state": state}
    )
    assert response.status_code == 400 and "wasn&#x27;t granted" in response.text
    assert connections.get_connection(db, ben.person_id) is None


@pytest.mark.parametrize(
    "target", ["https://evil.example/x", "//evil.example/x", "admin/people", "/\\evil.example"]
)
def test_return_to_cannot_leave_the_site(app, people, google_client_configured, target):
    ben = people["ben"]
    response = _member_client(app, ben).get(
        "/api/contacts/google/oauth/start",
        params={"person_id": ben.person_id, "return_to": target},
        follow_redirects=False,
    )
    state = parse_qs(urlparse(response.headers["location"]).query)["state"][0]
    assert connections.consume_pending(state).return_to is None


def test_redirect_uri_follows_the_public_address_not_avis_gmail_setup(
    app, people, google_client_configured, monkeypatch
):
    """Avi's own Gmail connection may still be registered on localhost
    while the family reaches the site through a public tunnel."""
    monkeypatch.setattr(
        google_client_configured, "GOOGLE_OAUTH_REDIRECT_URI",
        "http://localhost:8000/api/admin/google/oauth/callback",
    )
    monkeypatch.setattr(google_client_configured, "PUBLIC_BASE_URL", "https://avi.example.com/")
    ben = people["ben"]
    response = _member_client(app, ben).get(
        "/api/contacts/google/oauth/start",
        params={"person_id": ben.person_id},
        follow_redirects=False,
    )
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["redirect_uri"] == ["https://avi.example.com/api/contacts/google/oauth/callback"]
    connections.consume_pending(query["state"][0])

    monkeypatch.setattr(
        google_client_configured, "GOOGLE_CONTACTS_OAUTH_REDIRECT_URI",
        "https://other.example.com/api/contacts/google/oauth/callback",
    )
    assert connections.google_redirect_uri() == (
        "https://other.example.com/api/contacts/google/oauth/callback"
    )


@pytest.mark.parametrize(
    "referer,expected_prefix",
    [
        # Started from the public site: come back to it.
        ("https://avi.example.com/admin/families/1/people/2", "https://avi.example.com"),
        # Started from the Vite dev server on this machine: come back to it.
        ("http://localhost:5173/admin/families/1/people/2", "http://localhost:5173"),
        # Anywhere else is not somewhere to send a freshly-authorised browser.
        ("https://evil.example.net/page", ""),
        ("https://avi.example.com.evil.example.net/page", ""),
        (None, ""),
    ],
)
def test_connect_returns_to_the_site_it_was_started_from(
    app, people, google_client_configured, referer, expected_prefix
):
    ben = people["ben"]
    back = f"/admin/families/{ben.family_id}/people/{ben.person_id}"
    response = _member_client(app, ben).get(
        "/api/contacts/google/oauth/start",
        params={"person_id": ben.person_id, "return_to": back},
        headers={"referer": referer} if referer else {},
        follow_redirects=False,
    )
    state = parse_qs(urlparse(response.headers["location"]).query)["state"][0]
    assert connections.consume_pending(state).return_to == f"{expected_prefix}{back}"


def test_disconnect_and_test_endpoints(client, db, people, monkeypatch):
    ben = people["ben"]
    not_connected = client.post("/api/contacts/test", params={"person_id": ben.person_id})
    assert not_connected.status_code == 409

    _connect(db, ben)
    monkeypatch.setattr(
        connections, "adapter_for_person",
        lambda db, person_id, provider=None: FakeAdapter([_dana(), _dana(given_name="Priya")]),
    )
    ok = client.post("/api/contacts/test", params={"person_id": ben.person_id})
    # Proof of access is a count — never the contacts themselves.
    assert ok.json() == {
        "ok": True, "provider": "google",
        "account_identifier": ben.email_address, "contact_count": 2,
    }

    monkeypatch.setattr(connections.httpx, "post", lambda url, **kw: None)
    gone = client.delete("/api/contacts/connection", params={"person_id": ben.person_id})
    assert gone.status_code == 204
    db.expire_all()
    assert connections.get_connection(db, ben.person_id) is None


# ---------------------------------------------------------------------------
# Agent tools + permission requests
# ---------------------------------------------------------------------------


def test_contact_tools_are_always_offered_so_avi_can_ask_for_access():
    registry = tools.build_default_registry()
    offered = {t.name for t in registry.for_capabilities(set())}
    assert {"contacts_save", "contacts_search"} <= offered
    # No way to point the tools at someone else's address book.
    for schema in (contact_tools.CONTACTS_SAVE_SCHEMA, contact_tools.CONTACTS_SEARCH_SCHEMA):
        assert "person_id" not in schema["properties"]


async def test_unconnected_speaker_gets_a_permission_request_with_their_link(
    db, test_family, people, google_client_configured
):
    ben = people["ben"]
    with pytest.raises(ToolError) as caught:
        await contact_tools.handle_contacts_save(
            _ctx(db, test_family, ben), full_name="Dana Whitfield", email="dana@acme.com"
        )
    text = str(caught.value)
    assert text.startswith("Nothing was saved")
    assert ben.first_name in text and "Google Contacts" in text
    assert (
        f"https://avi.example.com/api/contacts/google/oauth/start?person_id={ben.person_id}" in text
    )


async def test_lapsed_connection_asks_to_reconnect(db, test_family, people, google_client_configured):
    ben = people["ben"]
    _connect(db, ben)
    connections.mark_needs_reconnect(db, ben.person_id, "google")
    with pytest.raises(ToolError, match="expired or was withdrawn") as caught:
        await contact_tools.handle_contacts_search(_ctx(db, test_family, ben), query="Dana")
    assert f"person_id={ben.person_id}" in str(caught.value)


async def test_grant_rejected_mid_call_is_remembered(db, test_family, people, address_book):
    ben = people["ben"]
    _connect(db, ben)
    address_book.fail_with = ContactsAuthExpired("google")
    with pytest.raises(ToolError, match="reconnect"):
        await contact_tools.handle_contacts_save(
            _ctx(db, test_family, ben), full_name="Dana Whitfield", email="dana@acme.com"
        )
    db.expire_all()
    assert connections.get_connection(db, ben.person_id, "google").needs_reconnect is True


async def test_unknown_speaker_cannot_touch_any_address_book(db, test_family, address_book):
    with pytest.raises(ToolError, match="don't know who that is"):
        await contact_tools.handle_contacts_save(
            _ctx(db, test_family, None), full_name="Dana Whitfield", email="dana@acme.com"
        )
    with pytest.raises(ToolError, match="don't know who that is"):
        await contact_tools.handle_contacts_search(_ctx(db, test_family, None), query="Dana")
    assert address_book.asked == []


async def test_save_from_a_forwarded_signature_updates_the_speakers_contact(
    db, test_family, people, address_book
):
    ben = people["ben"]
    result = await contact_tools.handle_contacts_save(
        _ctx(db, test_family, ben),
        full_name="Dana Whitfield",
        email="dana@acme.com",
        mobile_phone="415-555-0142",
        work_phone="(212) 555-0100",
        company="Acme Corp",
        job_title="Director of Operations",
    )
    assert address_book.asked == [ben.person_id]
    assert result.ok and result.output["action"] == "updated"
    assert result.output["address_book"] == f"{ben.first_name}'s Google Contacts"
    assert result.output["contact"]["company"] == "Acme Corp"
    assert {"value": "415-555-0142", "label": "mobile"} in result.output["contact"]["phones"]
    assert "added mobile phone 415-555-0142" in result.output["changes"]
    assert result.summary == f"Updated Dana Whitfield in {ben.first_name}'s Google Contacts"


async def test_save_creates_when_the_person_is_new(db, test_family, people, address_book):
    result = await contact_tools.handle_contacts_save(
        _ctx(db, test_family, people["ben"]),
        full_name="Priya Raman", other_phone="917 555 0142",
        additional_emails=["priya@northwind.com", ""],
    )
    assert result.output["action"] == "created"
    assert result.output["contact"]["emails"] == [{"value": "priya@northwind.com", "label": None}]
    assert len(address_book.book) == 2


async def test_ambiguous_match_tells_avi_to_ask_before_writing(
    db, test_family, people, address_book
):
    ctx = _ctx(db, test_family, people["ben"])
    result = await contact_tools.handle_contacts_save(
        ctx, full_name="Dana Whitfield", email="dana@elsewhere.org"
    )
    out = result.output
    assert out["action"] == "needs_confirmation" and out["saved"] is False
    assert out["candidates"][0]["contact_id"] == "people/c1"
    assert "NOTHING was saved" in out["next_step"] and "create_new=true" in out["next_step"]
    assert address_book.writes == []

    confirmed = await contact_tools.handle_contacts_save(
        ctx, full_name="Dana Whitfield", email="dana@elsewhere.org", contact_id="people/c1"
    )
    assert confirmed.output["action"] == "updated"


async def test_save_needs_something_to_save(db, test_family, people, address_book):
    with pytest.raises(ToolError, match="Nothing to save"):
        await contact_tools.handle_contacts_save(_ctx(db, test_family, people["ben"]), company="Acme")
    with pytest.raises(ToolError, match="not both"):
        await contact_tools.handle_contacts_save(
            _ctx(db, test_family, people["ben"]),
            full_name="Dana", contact_id="people/c1", create_new=True,
        )


async def test_search_returns_the_speakers_contacts(db, test_family, people, address_book):
    result = await contact_tools.handle_contacts_search(
        _ctx(db, test_family, people["ben"]), query="whitfield", limit=500
    )
    assert result.output["count"] == 1
    assert result.output["contacts"][0]["emails"][0]["value"] == "dana@acme.com"


async def test_tools_run_through_the_registry_like_the_agent_calls_them(
    db, test_family, people, google_client_configured
):
    registry = tools.build_default_registry()
    result = await registry.execute(
        "contacts_save",
        {"full_name": "Dana Whitfield", "email": "dana@acme.com"},
        _ctx(db, test_family, people["ben"]),
    )
    assert result.ok is False
    assert "hasn't given you access" in result.error


def test_permission_wording_matches_what_the_adapters_can_do():
    # "Never deletes" is only honest while no adapter exposes a delete.
    assert "never deletes" in messages.PERMISSION_SUMMARY
    assert not any(
        "delete" in name or "remove" in name
        for cls in connections.PROVIDERS.values()
        for name in dir(cls)
    )


# ---------------------------------------------------------------------------
# Routing: contact requests go to the full agent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "Fwd: Intro — please add Dana to my contacts",
        "sync this person to my Google contacts",
        "Do I have Priya's number in my contacts?",
        "do I have a contact for Priya Raman?",
        "look up Dana Whitfield in my contacts",
        "search my contacts for Acme",
        "Save this business card",
        "put him in my address book",
        "Add Dana Whitfield as a contact: 415-555-0142",
        "[Shared contact card]\nName: Dana Whitfield",
    ],
)
def test_contact_requests_are_recognised(message):
    assert fast_action_router.is_contacts_request(message)


@pytest.mark.parametrize(
    "message",
    [
        "What's Sarah's email?",
        "Who is the emergency contact for Jackson?",
        "Email Ben about practice",
        "Remind me to contact the plumber",
    ],
)
def test_ordinary_messages_are_not_mistaken_for_contact_requests(message):
    assert not fast_action_router.is_contacts_request(message)


async def test_contact_request_skips_the_shortcut_classifier(monkeypatch):
    monkeypatch.setattr(get_settings(), "AI_FAST_ACTION_SHORTCUT_ENABLED", True)

    def must_not_run(*args, **kwargs):
        raise AssertionError("the classifier model should not be consulted")

    monkeypatch.setattr(fast_action_router.llm, "resolve_model", must_not_run)
    label = await fast_action_router.classify_triage(
        "Fwd: Re: Q3 numbers\n\nPlease sync Dana to my contacts.\n\nDana Whitfield | Acme"
    )
    assert label == fast_action_router.LABEL_HEAVY


# ---------------------------------------------------------------------------
# Shared contact cards (vCard)
# ---------------------------------------------------------------------------


IPHONE_VCARD = (
    "BEGIN:VCARD\r\n"
    "VERSION:3.0\r\n"
    "PRODID:-//Apple Inc.//iPhone OS 18.1//EN\r\n"
    "N:Whitfield;Dana;;;\r\n"
    "FN:Dana Whitfield\r\n"
    "ORG:Acme Corp;Operations\r\n"
    "TITLE:Director of Operations\r\n"
    "EMAIL;type=INTERNET;type=WORK;type=pref:dana@acme.com\r\n"
    "TEL;type=CELL;type=VOICE;type=pref:+1 (415) 555-0142\r\n"
    "item1.TEL:(212) 555-0100\r\n"
    "item1.X-ABLabel:Front desk\r\n"
    "NOTE:Met at the Denver expo\\, booth 14\r\n"
    "PHOTO;ENCODING=b;TYPE=JPEG:/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAoHBwgHBgoICAgLCgoLDhgQDg0N\r\n"
    " Dh0VFhEYIx8lJCIfIiEmKzcvJik0KSEiMEExNDk7Pj4+JS5ESUM8SDc9Pjv/\r\n"
    "END:VCARD\r\n"
)

OLD_ANDROID_VCARD = (
    "BEGIN:VCARD\n"
    "VERSION:2.1\n"
    "N;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:Garc=C3=ADa;Jos=C3=A9;;;\n"
    "FN;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:Jos=C3=A9 Garc=C3=ADa de la=\n"
    " Vega\n"
    "TEL;CELL:917-555-0111\n"
    "TEL;WORK;VOICE:212-555-0122\n"
    "END:VCARD\n"
    "BEGIN:VCARD\n"
    "VERSION:4.0\n"
    "FN:Northwind Plumbing\n"
    "TEL;VALUE=uri;TYPE=\"voice,work\":tel:+1-646-555-0133\n"
    "EMAIL:mailto:hello@northwind.example\n"
    "END:VCARD\n"
)


def test_iphone_contact_card_is_read():
    [dana] = vcard.parse_vcards(IPHONE_VCARD)
    assert (dana.given_name, dana.family_name) == ("Dana", "Whitfield")
    assert dana.company == "Acme Corp" and dana.job_title == "Director of Operations"
    assert dana.emails == [ContactEmail("dana@acme.com", "work")]
    assert dana.phones == [
        ContactPhone("+1 (415) 555-0142", "mobile"),
        ContactPhone("(212) 555-0100", None),
    ]
    assert dana.notes == "Met at the Denver expo, booth 14"


def test_older_and_newer_card_formats_are_read():
    jose, plumber = vcard.parse_vcards(OLD_ANDROID_VCARD)
    assert (jose.given_name, jose.family_name) == ("José", "García")
    assert jose.display_name == "José García de la Vega"
    assert jose.phones == [ContactPhone("917-555-0111", "mobile"), ContactPhone("212-555-0122", "work")]
    assert plumber.name == "Northwind Plumbing"
    assert plumber.phones == [ContactPhone("+1-646-555-0133", "work")]
    assert plumber.emails == [ContactEmail("hello@northwind.example", None)]


def test_junk_is_not_a_contact():
    assert vcard.parse_vcards("hello, not a vcard") == []
    assert vcard.parse_vcards("BEGIN:VCARD\nVERSION:3.0\nEND:VCARD\n") == []


def _stored_file(test_family, name: str, content: bytes, mime: str, index: int = 1):
    root = get_settings().storage_root
    rel = f"family_{test_family['family_id']}/whatsapp/contactstest_{uuid.uuid4().hex}/{name}"
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return InboundAttachmentRef(
        media_index=index, filename=name, mime_type=mime,
        size_bytes=len(content), stored_path=rel, channel="whatsapp",
    ), path


@pytest.mark.parametrize("vision_enabled", [True, False])
def test_shared_contact_attachment_is_shown_to_the_agent(test_family, monkeypatch, vision_enabled):
    """Twilio delivers a shared contact as ``text/x-vcard`` with a ``.bin``-ish name."""
    monkeypatch.setattr(get_settings(), "AI_VISION_ENABLED", vision_enabled)
    ref, path = _stored_file(test_family, "attachment-0.bin", IPHONE_VCARD.encode(), "text/x-vcard")
    description = vision.describe_attachment(
        path=path, mime_type=ref.mime_type, filename=ref.filename
    )
    assert description.kind == "vcard_text" and description.error is None
    block = vision.render_attachments_for_prompt(
        [vision.RenderableAttachment(1, ref.filename, ref.mime_type, ref.size_bytes, description)]
    )
    assert "Shared contact card:" in block
    assert "Name: Dana Whitfield" in block
    assert "Phone (mobile): +1 (415) 555-0142" in block
    assert "Email: dana@acme.com" in block
    assert "/9j/" not in block  # the embedded photo is not dumped into the prompt


# ---------------------------------------------------------------------------
# Business cards and contact cards sent on their own
# ---------------------------------------------------------------------------


BUSINESS_CARD_JSON = {
    "kind": "business_card",
    "confidence": "high",
    "title": "Business card — Dana Whitfield",
    "summary": "Business card for Dana Whitfield, Director of Operations at Acme Corp.",
    "document_category": None,
    "person_names": ["Dana Whitfield"],
    "ocr_text": "ACME CORP\nDana Whitfield\nDirector of Operations\nM 415.555.0142\nO 212.555.0100\ndana@acme.com",
    "identity": None,
    "contact": {
        "full_name": "Dana Whitfield",
        "company": "Acme Corp",
        "job_title": "Director of Operations",
        "emails": ["dana@acme.com", "not-an-email"],
        "mobile_phone": "415.555.0142",
        "work_phone": "212.555.0100",
        "other_phone": None,
        "website": "acme.com",
    },
}


def test_business_card_is_recognised_with_its_contact_details():
    analysis = parse_analysis(BUSINESS_CARD_JSON)
    assert analysis.kind == document_intake.KIND_BUSINESS_CARD
    assert analysis.contact.full_name == "Dana Whitfield"
    assert analysis.contact.emails == ["dana@acme.com"]
    assert analysis.contact.mobile_phone == "415.555.0142"


def test_card_with_nobody_to_reach_is_just_a_document():
    data = copy.deepcopy(BUSINESS_CARD_JSON)
    data["contact"] = {"full_name": "Dana Whitfield", "company": "Acme Corp", "emails": []}
    analysis = parse_analysis(data)
    assert analysis.kind == document_intake.KIND_DOCUMENT and analysis.contact is None

    # And contact details never ride along on other kinds of file.
    data = copy.deepcopy(BUSINESS_CARD_JSON)
    data["kind"] = "document"
    assert parse_analysis(data).contact is None


def test_business_card_photo_is_handed_to_avi_to_save_not_filed(
    db, test_family, people, monkeypatch
):
    ben = people["ben"]
    monkeypatch.setattr(get_settings(), "AI_VISION_ENABLED", True)
    monkeypatch.setattr(get_settings(), "AI_ATTACHMENT_AUTO_FILE_ENABLED", True)
    monkeypatch.setattr(
        document_intake, "analyze",
        lambda path, *, mime_type, family_names=(): parse_analysis(BUSINESS_CARD_JSON),
    )
    ref, _path = _stored_file(test_family, "card.jpg", b"\xff\xd8fakejpeg", "image/jpeg")
    documents_before = db.execute(
        select(models.Document.document_id).where(models.Document.family_id == test_family["family_id"])
    ).all()

    report = attachment_intake.auto_file_attachments(
        db, family_id=test_family["family_id"], sender=ben, message_text="", refs=[ref]
    )

    assert "[Attachment 1: card.jpg] CONTACT CARD: a business card." in report
    for field_text in (
        "full_name: Dana Whitfield", "company: Acme Corp",
        "job_title: Director of Operations", "mobile_phone: 415.555.0142",
        "work_phone: 212.555.0100", "email: dana@acme.com",
    ):
        assert field_text in report
    assert "call `contacts_save`" in report
    # Not filed as household paperwork, and no open question left behind.
    documents_after = db.execute(
        select(models.Document.document_id).where(models.Document.family_id == test_family["family_id"])
    ).all()
    assert documents_after == documents_before
    assert attachment_intake.open_questions(db, person_id=ben.person_id) == []


def test_blurry_business_card_is_asked_about_not_saved(db, test_family, people, monkeypatch):
    monkeypatch.setattr(get_settings(), "AI_VISION_ENABLED", True)
    monkeypatch.setattr(get_settings(), "AI_ATTACHMENT_AUTO_FILE_ENABLED", True)
    blurry = {**BUSINESS_CARD_JSON, "confidence": "low"}
    monkeypatch.setattr(
        document_intake, "analyze",
        lambda path, *, mime_type, family_names=(): parse_analysis(blurry),
    )
    ref, _path = _stored_file(test_family, "card.jpg", b"\xff\xd8fakejpeg", "image/jpeg")
    report = attachment_intake.auto_file_attachments(
        db, family_id=test_family["family_id"], sender=people["ben"], message_text="", refs=[ref]
    )
    line = report.splitlines()[-1]
    assert line.startswith("[Attachment 1: card.jpg] ASK THE SENDER: what looks like a business card")
    assert "sharper photo" in line and "415.555.0142" not in line
    # The question is remembered so their typed answer can be acted on.
    assert len(attachment_intake.open_questions(db, person_id=people["ben"].person_id)) == 1


def test_contact_shared_with_no_message_is_handed_to_avi_to_save(
    db, test_family, people, monkeypatch
):
    monkeypatch.setattr(get_settings(), "AI_VISION_ENABLED", True)
    monkeypatch.setattr(get_settings(), "AI_ATTACHMENT_AUTO_FILE_ENABLED", True)

    def must_not_run(*args, **kwargs):
        raise AssertionError("a contact card needs no vision call")

    monkeypatch.setattr(document_intake, "analyze", must_not_run)
    ref, _path = _stored_file(test_family, "Dana Whitfield.vcf", IPHONE_VCARD.encode(), "text/vcard")
    report = attachment_intake.auto_file_attachments(
        db, family_id=test_family["family_id"], sender=people["ben"], message_text="", refs=[ref]
    )
    assert "CONTACT CARD: a shared contact." in report
    assert "full_name: Dana Whitfield" in report
    assert "mobile_phone: +1 (415) 555-0142" in report
    assert "other_phone: (212) 555-0100" in report


# ---------------------------------------------------------------------------
# Telegram: sharing someone's contact with Avi
# ---------------------------------------------------------------------------


def _telegram_contact_update(*, user_id: int, contact: Dict[str, Any]) -> Dict[str, Any]:
    update_id = int(uuid.uuid4().int % 10**9)
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": int(datetime.now(timezone.utc).timestamp()),
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Ben"},
            "contact": contact,
        },
    }


def _run_telegram(db, update):
    from api.services import telegram_inbox

    sent: List[dict] = []
    seen: Dict[str, Any] = {}

    def fake_agent(*args, **kwargs):
        seen["called"] = True
        seen["text"] = " ".join(str(a) for a in args) + " " + " ".join(
            str(v) for v in kwargs.values()
        )
        return "Added Dana to your contacts."

    def fake_triage(*args, **kwargs):
        seen["triage"] = True
        return fast_action_router.TriageResult(label=fast_action_router.LABEL_HEAVY)

    with patch.object(
        telegram_inbox, "_run_agent_to_completion", side_effect=fake_agent
    ), patch(
        "api.services.telegram_inbox.fast_action_router.try_triage_sync", side_effect=fake_triage
    ), patch(
        "api.integrations.telegram.send_message",
        side_effect=lambda **kw: sent.append(kw) or 424242,
    ):
        audit = telegram_inbox.process_inbound_update(db, update, bot_user_id=None)
    return audit, seen, sent


def test_family_member_sharing_someones_contact_reaches_avi(db, people):
    ben = people["ben"]
    update = _telegram_contact_update(
        user_id=ben.telegram_user_id,
        contact={
            "phone_number": "+14155550142",
            "first_name": "Dana",
            "last_name": "Whitfield",
            "vcard": IPHONE_VCARD,
        },
    )
    audit, seen, sent = _run_telegram(db, update)

    assert audit.status == "processed_replied", audit.status_reason
    assert audit.person_id == ben.person_id
    assert seen.get("called") is True
    assert audit.body.startswith("[Shared contact card]\nName: Dana Whitfield")
    assert "Email: dana@acme.com" in audit.body
    assert "Phone (mobile): +1 (415) 555-0142" in audit.body
    # Previously this was answered with "I can only auto-link the phone number…".
    assert [m["text"] for m in sent] == ["Added Dana to your contacts."]


def test_bare_telegram_contact_still_carries_name_and_number(db, people):
    ben = people["ben"]
    update = _telegram_contact_update(
        user_id=ben.telegram_user_id,
        contact={"phone_number": "+14155550142", "first_name": "Dana", "user_id": 555000111},
    )
    audit, seen, _sent = _run_telegram(db, update)
    assert seen.get("called") is True
    assert audit.body == "[Shared contact card]\nName: Dana\nPhone (unlabelled): +14155550142"


def test_stranger_sharing_a_contact_is_still_not_let_in(db, people):
    """The account-linking guard for unknown senders is unchanged."""
    stranger = int(uuid.uuid4().int % 10**9) + 2 * 10**9
    update = _telegram_contact_update(
        user_id=stranger,
        contact={"phone_number": "+14155550142", "first_name": "Dana", "user_id": 555000111},
    )
    audit, seen, sent = _run_telegram(db, update)
    assert audit.status == "ignored_unknown_sender"
    assert audit.person_id is None
    assert "called" not in seen
    assert len(sent) == 1 and "auto-link" in sent[0]["text"]

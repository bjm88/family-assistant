"""Find-or-create logic shared by every contacts provider.

The rules for "is this person already in the address book?" and "what
do we change when they are?" are about people, not about Google, so
they live here and run against the :class:`ContactsAdapter` contract.

Matching, strongest first:

1. Same email address (case-insensitive).
2. Same phone number (formatting and country-code prefix ignored).
3. Same full name.

An email or phone match on exactly one contact is treated as the same
person and merged. A name-only match is *not* enough to write on —
two different people can share a name, and a wrong merge silently
corrupts someone's real address book — so it comes back as
``needs_confirmation`` with the candidates and the caller asks.

Merging only ever adds: existing emails and phones are never removed,
and an existing name is kept unless the caller explicitly overwrites
it. Company and job title are replaced when new values are supplied,
because a current email signature beats a years-old card.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Iterable, List, Optional, Sequence, Tuple

from .base import (
    LABEL_MOBILE,
    LABEL_OTHER,
    LABEL_WORK,
    LABELS,
    Contact,
    ContactConflict,
    ContactEmail,
    ContactPhone,
    ContactsAdapter,
    ContactsError,
)


ACTION_CREATED = "created"
ACTION_UPDATED = "updated"
ACTION_UNCHANGED = "unchanged"
ACTION_NEEDS_CONFIRMATION = "needs_confirmation"

_EXTENSION_RE = re.compile(r"(?:ext\.?|extension|x)\s*\d+\s*$", re.IGNORECASE)
_NAME_JUNK_RE = re.compile(r"[^\w\s'-]+", re.UNICODE)
# Shortest national number we'll match against the tail of a longer one.
# Below this (e.g. a 7-digit US number with no area code) two different
# people's numbers could plausibly collide.
_MIN_NATIONAL_DIGITS = 8


@dataclass
class UpsertResult:
    action: str
    contact: Optional[Contact] = None
    # Human-readable list of what was written, e.g. "added mobile +1…".
    changes: List[str] = field(default_factory=list)
    # Supplied details we deliberately did not apply, and why.
    skipped: List[str] = field(default_factory=list)
    # Only for ``needs_confirmation``: who it might be.
    candidates: List[Contact] = field(default_factory=list)
    # 'email' | 'phone' | 'name' | 'contact_id' — how the match was made.
    matched_on: Optional[str] = None


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def _clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def _fold(value: Optional[str]) -> str:
    return (value or "").strip().casefold()


def phone_key(value: Optional[str]) -> Optional[str]:
    """Digits-only form of a phone number (extension dropped), for comparison."""
    if not value:
        return None
    digits = re.sub(r"\D+", "", _EXTENSION_RE.sub("", value))
    if digits.startswith("00"):  # international dialling prefix
        digits = digits[2:]
    return digits or None


def phones_match(a: Optional[str], b: Optional[str]) -> bool:
    """Same number, however it was written.

    Address books mix "+44 20 7946 0958", "020 7946 0958" and
    "(415) 555-0142" freely, so beyond an exact digit match the shorter
    number may be the national form of the longer one: drop its trunk
    "0" and compare against the end of the longer.
    """
    ka, kb = phone_key(a), phone_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    short, long_ = sorted((ka, kb), key=len)
    national = short.lstrip("0")
    return len(national) >= _MIN_NATIONAL_DIGITS and long_.endswith(national)


def name_key(*parts: Optional[str]) -> str:
    joined = " ".join(p for p in parts if p)
    return " ".join(_NAME_JUNK_RE.sub(" ", joined).casefold().split())


def _name_keys(contact: Contact) -> set[str]:
    keys = {
        name_key(contact.given_name, contact.family_name),
        name_key(contact.display_name),
    }
    keys.discard("")
    return keys


def split_full_name(full_name: str) -> Tuple[Optional[str], Optional[str]]:
    """Split "Dana R. Whitfield" into ("Dana R.", "Whitfield").

    "Whitfield, Dana" is honoured too. A single word is a given name.
    """
    text = _clean(full_name)
    if not text:
        return None, None
    if "," in text:
        family, _, given = text.partition(",")
        given, family = _clean(given), _clean(family)
        if given and family:
            return given, family
        text = given or family or ""
    parts = text.split(" ")
    if len(parts) == 1:
        return parts[0], None
    return " ".join(parts[:-1]), parts[-1]


def _normalise_label(label: Optional[str]) -> Optional[str]:
    folded = _fold(label)
    if not folded:
        return None
    if folded in ("cell", "cellphone", "cell phone", "mobile phone", "iphone"):
        return LABEL_MOBILE
    if folded in ("office", "business", "direct", "desk"):
        return LABEL_WORK
    return folded if folded in LABELS else LABEL_OTHER


def clean_contact(draft: Contact) -> Contact:
    """Trim, de-duplicate and order a draft before matching or saving.

    Mobile numbers are put first so they become the contact's primary
    phone on providers that treat the first entry that way.
    """
    emails: List[ContactEmail] = []
    for email in draft.emails:
        value = _clean(email.value)
        if not value or any(_fold(e.value) == _fold(value) for e in emails):
            continue
        emails.append(ContactEmail(value=value, label=_normalise_label(email.label)))

    phones: List[ContactPhone] = []
    for phone in draft.phones:
        value = _clean(phone.value)
        if not value or not phone_key(value):
            continue
        if any(phones_match(p.value, value) for p in phones):
            continue
        phones.append(ContactPhone(value=value, label=_normalise_label(phone.label)))
    phones.sort(key=lambda p: 0 if p.label == LABEL_MOBILE else 1)

    given, family = _clean(draft.given_name), _clean(draft.family_name)
    display = _clean(draft.display_name)
    if not (given or family) and display:
        given, family = split_full_name(display)

    return replace(
        draft,
        given_name=given,
        family_name=family,
        display_name=display,
        company=_clean(draft.company),
        job_title=_clean(draft.job_title),
        emails=emails,
        phones=phones,
        notes=(draft.notes or "").strip() or None,
    )


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def find_matches(
    contacts: Iterable[Contact], draft: Contact
) -> Tuple[List[Tuple[Contact, str]], List[Contact]]:
    """Return ``(strong, by_name)`` matches for ``draft``.

    ``strong`` pairs each contact with ``'email'`` or ``'phone'``;
    ``by_name`` lists contacts that share only the name.
    """
    draft_emails = {_fold(e.value) for e in draft.emails}
    draft_names = _name_keys(draft)
    strong: List[Tuple[Contact, str]] = []
    by_name: List[Contact] = []
    for contact in contacts:
        if draft_emails and any(_fold(e.value) in draft_emails for e in contact.emails):
            strong.append((contact, "email"))
        elif any(
            phones_match(p.value, dp.value)
            for p in contact.phones
            for dp in draft.phones
        ):
            strong.append((contact, "phone"))
        elif draft_names and draft_names & _name_keys(contact):
            by_name.append(contact)
    return strong, by_name


def filter_contacts(
    contacts: Iterable[Contact], query: str, *, limit: int = 10
) -> List[Contact]:
    """Plain substring search, for providers without a search endpoint."""
    tokens = _fold(query).split()
    digits = re.sub(r"\D+", "", query or "")
    if not tokens:
        return []
    hits: List[Contact] = []
    for contact in contacts:
        haystack = " ".join(
            _fold(part)
            for part in (
                contact.given_name,
                contact.family_name,
                contact.display_name,
                contact.company,
                contact.job_title,
                *(e.value for e in contact.emails),
            )
            if part
        )
        by_text = all(token in haystack for token in tokens)
        by_phone = len(digits) >= 4 and any(
            digits in (phone_key(p.value) or "") for p in contact.phones
        )
        if by_text or by_phone:
            hits.append(contact)
            if len(hits) >= limit:
                break
    return hits


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------


def _phone_phrase(phone: ContactPhone) -> str:
    return f"{phone.label} phone {phone.value}" if phone.label else f"phone {phone.value}"


def merge(
    existing: Contact, draft: Contact, *, overwrite_name: bool = False
) -> Tuple[Contact, List[str], List[str]]:
    """Fold ``draft`` into ``existing``.

    Returns ``(merged, changes, skipped)``. ``changes`` is empty when
    the address book already has everything in ``draft``.
    """
    merged = replace(
        existing, emails=list(existing.emails), phones=list(existing.phones)
    )
    changes: List[str] = []
    skipped: List[str] = []

    if draft.given_name or draft.family_name:
        old_name = " ".join(p for p in (existing.given_name, existing.family_name) if p)
        new_name = " ".join(p for p in (draft.given_name, draft.family_name) if p)
        has_name = bool(old_name or existing.display_name)
        same = name_key(new_name) in _name_keys(existing)
        if not has_name:
            merged.given_name, merged.family_name = draft.given_name, draft.family_name
            merged.display_name = None
            changes.append(f"set name to {new_name}")
        elif same:
            pass
        elif (
            not existing.family_name
            and draft.family_name
            and _fold(existing.given_name) == _fold(draft.given_name)
        ):
            merged.family_name = draft.family_name
            merged.display_name = None
            changes.append(f"added last name {draft.family_name}")
        elif overwrite_name:
            merged.given_name, merged.family_name = draft.given_name, draft.family_name
            merged.display_name = None
            changes.append(f"renamed {existing.name} to {new_name}")
        else:
            skipped.append(
                f"kept the existing name {existing.name!r} (the new details say "
                f"{new_name!r}); pass overwrite_name=true to rename"
            )

    for attr, label in (("company", "company"), ("job_title", "job title")):
        new_value = getattr(draft, attr)
        old_value = getattr(existing, attr)
        if not new_value or _fold(new_value) == _fold(old_value):
            continue
        setattr(merged, attr, new_value)
        changes.append(
            f"{label}: {old_value} → {new_value}" if old_value else f"set {label} to {new_value}"
        )

    for email in draft.emails:
        if any(_fold(e.value) == _fold(email.value) for e in merged.emails):
            continue
        merged.emails.append(email)
        changes.append(f"added email {email.value}")

    for phone in draft.phones:
        index = next(
            (i for i, p in enumerate(merged.phones) if phones_match(p.value, phone.value)),
            None,
        )
        if index is None:
            merged.phones.append(phone)
            changes.append(f"added {_phone_phrase(phone)}")
            continue
        current = merged.phones[index]
        vague = current.label in (None, LABEL_OTHER)
        if vague and phone.label and phone.label != LABEL_OTHER:
            merged.phones[index] = ContactPhone(value=current.value, label=phone.label)
            changes.append(f"labelled {current.value} as {phone.label}")

    if draft.notes and _fold(draft.notes) not in _fold(existing.notes):
        merged.notes = (
            f"{existing.notes.rstrip()}\n{draft.notes}" if existing.notes else draft.notes
        )
        changes.append("added a note")

    return merged, changes, skipped


def describe_new(contact: Contact) -> List[str]:
    """What a freshly created contact holds, for the confirmation."""
    lines: List[str] = []
    if contact.company:
        lines.append(f"company {contact.company}")
    if contact.job_title:
        lines.append(f"job title {contact.job_title}")
    lines.extend(f"email {e.value}" for e in contact.emails)
    lines.extend(_phone_phrase(p) for p in contact.phones)
    if contact.notes:
        lines.append("a note")
    return lines


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------


def _distinct(pairs: Sequence[Tuple[Contact, str]]) -> List[Tuple[Contact, str]]:
    seen: set[Optional[str]] = set()
    out: List[Tuple[Contact, str]] = []
    for contact, how in pairs:
        if contact.contact_id in seen:
            continue
        seen.add(contact.contact_id)
        out.append((contact, how))
    return out


def upsert(
    adapter: ContactsAdapter,
    draft: Contact,
    *,
    contact_id: Optional[str] = None,
    force_new: bool = False,
    overwrite_name: bool = False,
) -> UpsertResult:
    """Add ``draft`` to the address book, or update the matching contact.

    ``contact_id`` pins the contact to update (skipping the search);
    ``force_new`` creates a new contact even when a name matches. Use
    those to act on a ``needs_confirmation`` answer.
    """
    draft = clean_contact(draft)
    if not (draft.given_name or draft.family_name or draft.emails or draft.phones):
        raise ContactsError(
            "A contact needs at least a name, an email address or a phone number."
        )

    existing: Optional[Contact] = None
    matched_on: Optional[str] = None
    if contact_id:
        existing, matched_on = adapter.get(contact_id), "contact_id"
    elif not force_new:
        strong, by_name = find_matches(adapter.list_all(), draft)
        strong = _distinct(strong)
        if len(strong) == 1:
            existing, matched_on = strong[0]
        elif len(strong) > 1:
            return UpsertResult(
                action=ACTION_NEEDS_CONFIRMATION,
                candidates=[c for c, _ in strong],
                matched_on=strong[0][1],
            )
        elif by_name:
            return UpsertResult(
                action=ACTION_NEEDS_CONFIRMATION,
                candidates=by_name,
                matched_on="name",
            )

    if existing is None:
        created = adapter.create(replace(draft, contact_id=None, raw=None))
        return UpsertResult(
            action=ACTION_CREATED, contact=created, changes=describe_new(created)
        )

    merged, changes, skipped = merge(existing, draft, overwrite_name=overwrite_name)
    if not changes:
        return UpsertResult(
            action=ACTION_UNCHANGED,
            contact=existing,
            skipped=skipped,
            matched_on=matched_on,
        )
    try:
        saved = adapter.update(merged)
    except ContactConflict:
        # Someone edited the contact between our read and write. Re-read
        # and re-merge once rather than overwriting their edit.
        fresh = adapter.get(existing.contact_id or "")
        merged, changes, skipped = merge(fresh, draft, overwrite_name=overwrite_name)
        if not changes:
            return UpsertResult(
                action=ACTION_UNCHANGED,
                contact=fresh,
                skipped=skipped,
                matched_on=matched_on,
            )
        saved = adapter.update(merged)
    return UpsertResult(
        action=ACTION_UPDATED,
        contact=saved,
        changes=changes,
        skipped=skipped,
        matched_on=matched_on,
    )

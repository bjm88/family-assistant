"""``contacts_search`` / ``contacts_save`` — the speaker's own address book.

Both tools act on the address book of the person Avi is talking to and
nobody else's: there is no ``person_id`` argument and no admin or
parent override, because "add her to my contacts" is personal.

When the speaker hasn't connected an address book (or the grant has
lapsed) the tools fail with a permission request that includes the
connect link, which the agent passes on.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from .... import models
from ....integrations.contacts import connections, messages, sync
from ....integrations.contacts.base import (
    LABEL_MOBILE,
    LABEL_WORK,
    Contact,
    ContactEmail,
    ContactPhone,
    ContactsAdapter,
    ContactsAuthExpired,
    ContactsError,
    ContactsNotConnected,
    ContactsNotSupported,
)
from .._registry import ToolContext, ToolError, ToolResult


CONTACTS_SEARCH_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "A name, email address, phone number or company to look for."
            ),
        },
        "limit": {
            "type": "integer",
            "description": "Maximum contacts to return (default 5, max 20).",
        },
    },
    "required": ["query"],
}


CONTACTS_SAVE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "full_name": {
            "type": "string",
            "description": "The person's name as written, e.g. 'Dana Whitfield'.",
        },
        "email": {"type": "string", "description": "Their email address."},
        "additional_emails": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Any further email addresses.",
        },
        "mobile_phone": {
            "type": "string",
            "description": (
                "Mobile / cell number. Prefer this slot whenever a number is "
                "marked mobile, cell or 'M:'."
            ),
        },
        "work_phone": {
            "type": "string",
            "description": "Office / direct / desk number.",
        },
        "other_phone": {
            "type": "string",
            "description": "A number whose type isn't stated.",
        },
        "company": {"type": "string", "description": "Company or organisation."},
        "job_title": {"type": "string", "description": "Role / job title."},
        "notes": {
            "type": "string",
            "description": (
                "Optional short note to keep on the contact, e.g. how the "
                "user knows them. Only when the user asks for one."
            ),
        },
        "contact_id": {
            "type": "string",
            "description": (
                "Update exactly this contact (an id from contacts_search or "
                "from a needs_confirmation result) instead of searching."
            ),
        },
        "create_new": {
            "type": "boolean",
            "description": (
                "Create a separate contact even though one with the same name "
                "exists. Only after the user confirmed it's a different person."
            ),
        },
        "overwrite_name": {
            "type": "boolean",
            "description": (
                "Replace the existing contact's name with full_name. Only when "
                "the user asked to rename or correct it."
            ),
        },
    },
    "required": [],
}


def _speaker(ctx: ToolContext) -> models.Person:
    if ctx.person_id is None:
        raise ToolError(messages.ANONYMOUS_FOR_AGENT)
    person = ctx.db.get(models.Person, ctx.person_id)
    if person is None or person.family_id != ctx.family_id:
        raise ToolError(messages.ANONYMOUS_FOR_AGENT)
    return person


def _speaker_name(person: models.Person) -> str:
    return person.preferred_name or person.first_name or "The user"


def _adapter(ctx: ToolContext, person: models.Person) -> ContactsAdapter:
    """The speaker's address book, or a ToolError asking them to connect."""
    name = _speaker_name(person)
    try:
        return connections.adapter_for_person(ctx.db, person.person_id)
    except ContactsNotConnected as exc:
        raise ToolError(
            messages.not_connected_for_agent(name, person.person_id, exc.provider)
        ) from exc
    except ContactsAuthExpired as exc:
        raise ToolError(
            messages.reconnect_for_agent(name, person.person_id, exc.provider)
        ) from exc
    except ContactsNotSupported as exc:
        raise ToolError(messages.not_supported_for_agent(exc.provider)) from exc
    except ContactsError as exc:
        raise ToolError(str(exc)) from exc


async def _call(ctx: ToolContext, person: models.Person, adapter, fn, *args, **kwargs):
    """Run a blocking adapter call, translating provider failures."""
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except ContactsAuthExpired as exc:
        # The provider refused the grant mid-call; remember that so the
        # next attempt asks to reconnect straight away.
        connections.mark_needs_reconnect(ctx.db, person.person_id, adapter.provider)
        raise ToolError(
            messages.reconnect_for_agent(
                _speaker_name(person), person.person_id, adapter.provider
            )
        ) from exc
    except ContactsNotSupported as exc:
        raise ToolError(messages.not_supported_for_agent(exc.provider)) from exc
    except ContactsError as exc:
        raise ToolError(str(exc)) from exc


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


async def handle_contacts_search(
    ctx: ToolContext, query: str, limit: int = 5
) -> ToolResult:
    person = _speaker(ctx)
    wanted = _text(query)
    if not wanted:
        raise ToolError("Give a name, email, phone number or company to search for.")
    try:
        limit = max(1, min(int(limit), 20))
    except (TypeError, ValueError):
        limit = 5
    adapter = _adapter(ctx, person)
    found: List[Contact] = await _call(
        ctx, person, adapter, adapter.search, wanted, limit=limit
    )
    return ToolResult(
        ok=True,
        output={
            "address_book": f"{_speaker_name(person)}'s {adapter.label}",
            "count": len(found),
            "contacts": [c.to_dict() for c in found],
        },
        summary=f"Found {len(found)} contact(s) for “{wanted}”",
    )


async def handle_contacts_save(
    ctx: ToolContext,
    full_name: Optional[str] = None,
    email: Optional[str] = None,
    additional_emails: Optional[List[str]] = None,
    mobile_phone: Optional[str] = None,
    work_phone: Optional[str] = None,
    other_phone: Optional[str] = None,
    company: Optional[str] = None,
    job_title: Optional[str] = None,
    notes: Optional[str] = None,
    contact_id: Optional[str] = None,
    create_new: bool = False,
    overwrite_name: bool = False,
) -> ToolResult:
    person = _speaker(ctx)

    emails = [ContactEmail(value=e) for e in [_text(email)] if e]
    if isinstance(additional_emails, (list, tuple)):
        emails.extend(ContactEmail(value=e) for e in map(_text, additional_emails) if e)
    phones = [
        ContactPhone(value=number, label=label)
        for number, label in (
            (_text(mobile_phone), LABEL_MOBILE),
            (_text(work_phone), LABEL_WORK),
            (_text(other_phone), None),
        )
        if number
    ]
    draft = Contact(
        display_name=_text(full_name),
        company=_text(company),
        job_title=_text(job_title),
        emails=emails,
        phones=phones,
        notes=_text(notes),
    )
    if not (draft.display_name or emails or phones):
        raise ToolError(
            "Nothing to save: give at least a name, an email address or a "
            "phone number for the contact."
        )
    if _text(contact_id) and create_new:
        raise ToolError("Pass either contact_id or create_new, not both.")

    adapter = _adapter(ctx, person)
    result: sync.UpsertResult = await _call(
        ctx,
        person,
        adapter,
        sync.upsert,
        adapter,
        draft,
        contact_id=_text(contact_id),
        force_new=bool(create_new),
        overwrite_name=bool(overwrite_name),
    )

    book = f"{_speaker_name(person)}'s {adapter.label}"
    if result.action == sync.ACTION_NEEDS_CONFIRMATION:
        by_name = result.matched_on == "name"
        return ToolResult(
            ok=True,
            output={
                "action": result.action,
                "saved": False,
                "address_book": book,
                "reason": (
                    "A contact with this name already exists but with a "
                    "different email and phone, so it may or may not be the "
                    "same person."
                    if by_name
                    else "More than one existing contact has this "
                    f"{result.matched_on}, so it's unclear which to update."
                ),
                "candidates": [c.to_dict() for c in result.candidates[:5]],
                "next_step": (
                    "NOTHING was saved. Ask the user which they want, then "
                    "call contacts_save again with the same details plus "
                    "contact_id=<the chosen candidate>"
                    + (
                        " to update it, or create_new=true to add a separate contact."
                        if by_name
                        else "."
                    )
                ),
            },
            summary="Contact not saved — needs the user's confirmation",
        )

    contact = result.contact
    name = contact.name if contact else (draft.display_name or "contact")
    summary = {
        sync.ACTION_CREATED: f"Added {name} to {book}",
        sync.ACTION_UPDATED: f"Updated {name} in {book}",
        sync.ACTION_UNCHANGED: f"{name} is already up to date in {book}",
    }[result.action]
    return ToolResult(
        ok=True,
        output={
            "action": result.action,
            "address_book": book,
            "matched_on": result.matched_on,
            "contact": contact.to_dict() if contact else None,
            "changes": result.changes,
            "not_applied": result.skipped,
        },
        summary=summary,
    )

"""Household-record writes: goals, medications, identity documents,
documents, and person photos.

Lets a family member ask Avi (from any inbound surface) to add or change
their own records, and lets a primary parent do the same for anyone in
the household. The permission rule lives in
:func:`api.ai.authz.can_edit_person_records`; every handler here calls it
before touching the database and validates all inputs first, because
the agent loop commits after each tool call.

Update semantics shared by every ``*_save`` tool: an argument that is
omitted (``None``) leaves the column alone, and an empty string clears
an optional column.
"""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .... import crypto, models, storage
from ....config import get_settings
from ... import authz
from .._registry import InboundAttachmentRef, ToolContext, ToolError
from .tasks import _resolve_family_timezone_for_tool


logger = logging.getLogger(__name__)


GOAL_PRIORITIES = ("urgent", "semi_urgent", "normal", "low")

IDENTITY_DOCUMENT_TYPES = (
    "drivers_license",
    "passport",
    "state_id",
    "birth_certificate",
    "permanent_resident_card",
    "global_entry",
    "military_id",
    "united_mileage_plus",
    "other",
)

DOCUMENT_CATEGORIES = (
    "tax",
    "medical",
    "legal",
    "education",
    "financial",
    "identity_scan",
    "insurance",
    "receipt",
    "warranty",
    "other",
)

RECORD_KINDS = ("goal", "medication", "identity_document", "document")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _name(person: Optional[models.Person]) -> str:
    if person is None:
        return "the family"
    return person.preferred_name or person.first_name or f"Person {person.person_id}"


def _family_person(ctx: ToolContext, person_id: int) -> models.Person:
    person = ctx.db.get(models.Person, int(person_id))
    if person is None or person.family_id != ctx.family_id:
        raise ToolError(
            f"No person with id={person_id} in this family. Use lookup_person "
            "to find the right person_id."
        )
    return person


def _subject_for_new(ctx: ToolContext, person_id: Optional[int]) -> models.Person:
    if person_id is not None:
        return _family_person(ctx, person_id)
    if ctx.person_id is None:
        raise ToolError(
            "I don't know who this record is for. Ask which family member "
            "it belongs to, then pass person_id."
        )
    return _family_person(ctx, ctx.person_id)


def _authorize(ctx: ToolContext, subject: Optional[models.Person]) -> None:
    decision = authz.can_edit_person_records(
        ctx.db,
        requestor_person_id=ctx.person_id,
        subject_person_id=subject.person_id if subject is not None else None,
        family_id=ctx.family_id,
        requestor_is_admin=ctx.is_admin,
    )
    if decision.allowed:
        return
    if decision.label == "anonymous":
        raise ToolError(
            "I can only change household records when I know which family "
            "member is asking. Nothing was changed."
        )
    if subject is None:
        raise ToolError(
            "Only a parent of the household can save family-wide records. "
            "Nothing was changed."
        )
    who = _name(subject)
    raise ToolError(
        f"Only a parent of the household or {who} themselves can change "
        f"{who}'s records. Nothing was changed — tell the user that plainly."
    )


def _text(label: str, raw: Optional[str], max_len: Optional[int] = None) -> Optional[str]:
    """Normalise a text arg. Returns None for omitted, '' for clear."""
    if raw is None:
        return None
    value = str(raw).strip()
    if max_len is not None and len(value) > max_len:
        raise ToolError(f"{label} is too long (max {max_len} characters).")
    return value


def _date(label: str, raw: Optional[str]) -> Optional[date] | str:
    """Parse an ISO date arg. Returns None for omitted, '' for clear."""
    if raw is None:
        return None
    value = str(raw).strip()
    if value == "":
        return ""
    try:
        return date.fromisoformat(value[:10])
    except ValueError as exc:
        raise ToolError(f"{label} must be an ISO date (YYYY-MM-DD), got {raw!r}.") from exc


def _assign(obj: Any, field: str, value: Any) -> None:
    """Apply update semantics: None = leave, '' = clear, else set."""
    if value is None:
        return
    setattr(obj, field, None if value == "" else value)


def _today(ctx: ToolContext) -> date:
    tz = _resolve_family_timezone_for_tool(ctx.db, ctx.family_id)
    try:
        return datetime.now(ZoneInfo(tz)).date()
    except Exception:  # noqa: BLE001 - bad tz string falls back to server date
        return date.today()


def _inbound_refs(
    ctx: ToolContext, media_index: int, *, images_only: bool = False
) -> List[InboundAttachmentRef]:
    refs = list(ctx.inbound_attachments or [])
    if not refs:
        raise ToolError(
            "No attachments arrived with this message. Ask the user to "
            "resend with the file included."
        )
    if int(media_index) == 0:
        targets = refs
    else:
        targets = [r for r in refs if r.media_index == int(media_index)]
        if not targets:
            available = ", ".join(str(r.media_index) for r in refs)
            raise ToolError(
                f"This message has no Attachment {media_index}. "
                f"Available media_index values: {available}."
            )
    root = get_settings().storage_root
    for ref in targets:
        if images_only and not (ref.mime_type or "").startswith("image/"):
            raise ToolError(
                f"Attachment {ref.media_index} ({ref.filename}) is not an "
                "image. Only photos can be saved here; use "
                "document_save_from_message for PDFs and other files."
            )
        src = root / ref.stored_path
        if not src.is_file():
            raise ToolError(
                f"Attachment {ref.media_index} ({ref.filename}) is no longer "
                "on disk; ask the user to resend it."
            )
    return targets


def _log_write(ctx: ToolContext, action: str, kind: str, record_id: int, subject: Optional[models.Person]) -> None:
    logger.info(
        "[records] %s %s id=%s subject=%s requestor=%s family=%s",
        action,
        kind,
        record_id,
        subject.person_id if subject is not None else None,
        ctx.person_id,
        ctx.family_id,
    )


# ---------------------------------------------------------------------------
# Serialisers
# ---------------------------------------------------------------------------


def _goal_out(g: models.Goal) -> Dict[str, Any]:
    return {
        "goal_id": g.goal_id,
        "person_id": g.person_id,
        "goal_name": g.goal_name,
        "priority": g.priority,
        "start_date": g.start_date.isoformat() if g.start_date else None,
        "completed_date": g.completed_date.isoformat() if g.completed_date else None,
        "description": (g.description or "")[:200] or None,
    }


def _medication_out(m: models.Medication) -> Dict[str, Any]:
    return {
        "medication_id": m.medication_id,
        "person_id": m.person_id,
        "brand_name": m.brand_name,
        "generic_name": m.generic_name,
        "ndc_number": m.ndc_number,
        "dosage": m.dosage,
        "start_date": m.start_date.isoformat() if m.start_date else None,
        "end_date": m.end_date.isoformat() if m.end_date else None,
        "active": m.end_date is None,
        "notes": (m.notes or "")[:200] or None,
    }


def _identity_document_out(d: models.IdentityDocument) -> Dict[str, Any]:
    return {
        "identity_document_id": d.identity_document_id,
        "person_id": d.person_id,
        "document_type": d.document_type,
        "identifier": d.identifier,
        "document_number_last_four": d.document_number_last_four,
        "issuing_authority": d.issuing_authority,
        "country_of_issue": d.country_of_issue,
        "state_or_region_of_issue": d.state_or_region_of_issue,
        "issue_date": d.issue_date.isoformat() if d.issue_date else None,
        "expiration_date": d.expiration_date.isoformat() if d.expiration_date else None,
        "has_front_image": bool(d.front_image_path),
        "has_back_image": bool(d.back_image_path),
    }


def _document_out(d: models.Document) -> Dict[str, Any]:
    return {
        "document_id": d.document_id,
        "person_id": d.person_id,
        "title": d.title,
        "document_category": d.document_category,
        "original_file_name": d.original_file_name,
        "mime_type": d.mime_type,
        "file_size_bytes": d.file_size_bytes,
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }


# ---------------------------------------------------------------------------
# household_records_list
# ---------------------------------------------------------------------------


HOUSEHOLD_RECORDS_LIST_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(RECORD_KINDS)},
        "person_id": {
            "type": "integer",
            "description": "Whose records. Defaults to the speaker.",
        },
    },
    "required": ["kind"],
}


async def handle_household_records_list(
    ctx: ToolContext, kind: str, person_id: Optional[int] = None
) -> Dict[str, Any]:
    if kind not in RECORD_KINDS:
        raise ToolError(f"kind must be one of {list(RECORD_KINDS)}.")
    person = _subject_for_new(ctx, person_id)

    if kind in ("medication", "identity_document"):
        read = authz.can_access_sensitive(
            ctx.db,
            requestor_person_id=ctx.person_id,
            subject_person_id=person.person_id,
            requestor_is_admin=ctx.is_admin,
        )
        if not read.allowed:
            _authorize(ctx, person)

    db = ctx.db
    if kind == "goal":
        rows = db.execute(
            select(models.Goal)
            .where(models.Goal.person_id == person.person_id)
            .order_by(models.Goal.goal_id)
        ).scalars().all()
        items = [_goal_out(r) for r in rows]
    elif kind == "medication":
        rows = db.execute(
            select(models.Medication)
            .where(models.Medication.person_id == person.person_id)
            .order_by(models.Medication.medication_id)
        ).scalars().all()
        items = [_medication_out(r) for r in rows]
    elif kind == "identity_document":
        rows = db.execute(
            select(models.IdentityDocument)
            .where(models.IdentityDocument.person_id == person.person_id)
            .order_by(models.IdentityDocument.identity_document_id)
        ).scalars().all()
        items = [_identity_document_out(r) for r in rows]
    else:
        rows = db.execute(
            select(models.Document)
            .where(models.Document.family_id == ctx.family_id)
            .where(models.Document.person_id == person.person_id)
            .order_by(models.Document.document_id.desc())
            .limit(50)
        ).scalars().all()
        items = [_document_out(r) for r in rows]

    return {"person": _name(person), "person_id": person.person_id, "kind": kind, "items": items}


# ---------------------------------------------------------------------------
# goal_save
# ---------------------------------------------------------------------------


GOAL_SAVE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "goal_id": {
            "type": "integer",
            "description": "Existing goal to update. Omit to create a new goal.",
        },
        "person_id": {
            "type": "integer",
            "description": "Who the new goal belongs to. Defaults to the speaker.",
        },
        "goal_name": {"type": "string", "description": "Short title. Required when creating."},
        "description": {"type": "string"},
        "priority": {"type": "string", "enum": list(GOAL_PRIORITIES)},
        "start_date": {"type": "string", "description": "YYYY-MM-DD, or '' to clear."},
        "completed_date": {"type": "string", "description": "YYYY-MM-DD the goal was completed."},
        "mark_complete": {
            "type": "boolean",
            "description": "Mark the goal complete as of today.",
        },
        "reopen": {"type": "boolean", "description": "Clear the completed date."},
    },
}


async def handle_goal_save(
    ctx: ToolContext,
    goal_id: Optional[int] = None,
    person_id: Optional[int] = None,
    goal_name: Optional[str] = None,
    description: Optional[str] = None,
    priority: Optional[str] = None,
    start_date: Optional[str] = None,
    completed_date: Optional[str] = None,
    mark_complete: bool = False,
    reopen: bool = False,
) -> Dict[str, Any]:
    db = ctx.db
    goal: Optional[models.Goal] = None
    if goal_id is not None:
        goal = db.get(models.Goal, int(goal_id))
        subject = goal.person if goal is not None else None
        if goal is None or subject is None or subject.family_id != ctx.family_id:
            raise ToolError(f"No goal with id={goal_id} in this family.")
    else:
        subject = _subject_for_new(ctx, person_id)
    _authorize(ctx, subject)

    name = _text("goal_name", goal_name, 200)
    desc = _text("description", description)
    if priority is not None and priority not in GOAL_PRIORITIES:
        raise ToolError(f"priority must be one of {list(GOAL_PRIORITIES)}.")
    start = _date("start_date", start_date)
    done = _date("completed_date", completed_date)
    if sum(bool(x) for x in (mark_complete, reopen, done not in (None, ""))) > 1:
        raise ToolError("Use only one of completed_date, mark_complete, or reopen.")
    if mark_complete:
        done = _today(ctx)
    elif reopen:
        done = ""
    if goal is None and not name:
        raise ToolError("goal_name is required to create a goal.")
    if name == "":
        raise ToolError("goal_name cannot be blank.")

    created = goal is None
    if goal is None:
        goal = models.Goal(person_id=subject.person_id, goal_name=name, priority=priority or "normal")
    _assign(goal, "goal_name", name)
    _assign(goal, "description", desc)
    _assign(goal, "priority", priority)
    _assign(goal, "start_date", start)
    _assign(goal, "completed_date", done)
    if goal.completed_date and goal.start_date and goal.completed_date < goal.start_date:
        if not created:
            db.refresh(goal)
        raise ToolError("Completed date cannot be before the start date.")

    if created:
        db.add(goal)
    db.flush()
    db.refresh(goal)
    _log_write(ctx, "created" if created else "updated", "goal", goal.goal_id, subject)
    return {
        "action": "created" if created else "updated",
        "person": _name(subject),
        "goal": _goal_out(goal),
    }


# ---------------------------------------------------------------------------
# medication_save
# ---------------------------------------------------------------------------


MEDICATION_SAVE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "medication_id": {
            "type": "integer",
            "description": "Existing medication to update. Omit to add a new one.",
        },
        "person_id": {
            "type": "integer",
            "description": "Who takes the new medication. Defaults to the speaker.",
        },
        "brand_name": {"type": "string", "description": "e.g. 'Zyrtec'."},
        "generic_name": {"type": "string", "description": "e.g. 'cetirizine'."},
        "ndc_number": {"type": "string"},
        "dosage": {"type": "string", "description": "Dose + frequency, e.g. '10mg once daily'."},
        "start_date": {"type": "string", "description": "YYYY-MM-DD, or '' to clear."},
        "end_date": {
            "type": "string",
            "description": "YYYY-MM-DD they stopped, or '' to mark active again.",
        },
        "stopped_today": {
            "type": "boolean",
            "description": "Mark the medication stopped as of today.",
        },
        "notes": {"type": "string"},
    },
}


async def handle_medication_save(
    ctx: ToolContext,
    medication_id: Optional[int] = None,
    person_id: Optional[int] = None,
    brand_name: Optional[str] = None,
    generic_name: Optional[str] = None,
    ndc_number: Optional[str] = None,
    dosage: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    stopped_today: bool = False,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    db = ctx.db
    med: Optional[models.Medication] = None
    if medication_id is not None:
        med = db.get(models.Medication, int(medication_id))
        subject = med.person if med is not None else None
        if med is None or subject is None or subject.family_id != ctx.family_id:
            raise ToolError(f"No medication with id={medication_id} in this family.")
    else:
        subject = _subject_for_new(ctx, person_id)
    _authorize(ctx, subject)

    brand = _text("brand_name", brand_name, 160)
    generic = _text("generic_name", generic_name, 160)
    ndc = _text("ndc_number", ndc_number, 20)
    dose = _text("dosage", dosage, 120)
    note = _text("notes", notes)
    start = _date("start_date", start_date)
    end = _date("end_date", end_date)
    if stopped_today:
        if end not in (None, ""):
            raise ToolError("Use either end_date or stopped_today, not both.")
        end = _today(ctx)

    created = med is None
    if med is None:
        med = models.Medication(person_id=subject.person_id)
    _assign(med, "brand_name", brand)
    _assign(med, "generic_name", generic)
    _assign(med, "ndc_number", ndc)
    _assign(med, "dosage", dose)
    _assign(med, "notes", note)
    _assign(med, "start_date", start)
    _assign(med, "end_date", end)
    if not (med.brand_name or med.generic_name or med.ndc_number):
        if not created:
            db.refresh(med)
        raise ToolError(
            "A medication needs at least a brand name, generic name, or NDC number."
        )
    if med.start_date and med.end_date and med.end_date < med.start_date:
        if not created:
            db.refresh(med)
        raise ToolError("End date cannot be before the start date.")

    if created:
        db.add(med)
    db.flush()
    db.refresh(med)
    _log_write(ctx, "created" if created else "updated", "medication", med.medication_id, subject)
    return {
        "action": "created" if created else "updated",
        "person": _name(subject),
        "medication": _medication_out(med),
    }


# ---------------------------------------------------------------------------
# identity_document_save
# ---------------------------------------------------------------------------


IDENTITY_DOCUMENT_SAVE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "identity_document_id": {
            "type": "integer",
            "description": "Existing document to update. Omit to add a new one.",
        },
        "person_id": {
            "type": "integer",
            "description": "Whose document. Defaults to the speaker.",
        },
        "document_type": {"type": "string", "enum": list(IDENTITY_DOCUMENT_TYPES)},
        "identifier": {
            "type": "string",
            "description": (
                "Non-secret number shown in full (MileagePlus, Known "
                "Traveler / Global Entry PASSID)."
            ),
        },
        "document_number": {
            "type": "string",
            "description": (
                "Secret number (driver's license, passport). Stored "
                "encrypted; only the last four are ever shown."
            ),
        },
        "issuing_authority": {"type": "string"},
        "country_of_issue": {"type": "string"},
        "state_or_region_of_issue": {"type": "string"},
        "issue_date": {"type": "string", "description": "YYYY-MM-DD."},
        "expiration_date": {"type": "string", "description": "YYYY-MM-DD."},
        "notes": {"type": "string"},
        "front_media_index": {
            "type": "integer",
            "description": "Attachment N on this message to save as the front scan.",
        },
        "back_media_index": {
            "type": "integer",
            "description": "Attachment N on this message to save as the back scan.",
        },
    },
}


async def handle_identity_document_save(
    ctx: ToolContext,
    identity_document_id: Optional[int] = None,
    person_id: Optional[int] = None,
    document_type: Optional[str] = None,
    identifier: Optional[str] = None,
    document_number: Optional[str] = None,
    issuing_authority: Optional[str] = None,
    country_of_issue: Optional[str] = None,
    state_or_region_of_issue: Optional[str] = None,
    issue_date: Optional[str] = None,
    expiration_date: Optional[str] = None,
    notes: Optional[str] = None,
    front_media_index: Optional[int] = None,
    back_media_index: Optional[int] = None,
) -> Dict[str, Any]:
    db = ctx.db
    doc: Optional[models.IdentityDocument] = None
    if identity_document_id is not None:
        doc = db.get(models.IdentityDocument, int(identity_document_id))
        subject = doc.person if doc is not None else None
        if doc is None or subject is None or subject.family_id != ctx.family_id:
            raise ToolError(
                f"No identity document with id={identity_document_id} in this family."
            )
    else:
        subject = _subject_for_new(ctx, person_id)
    _authorize(ctx, subject)

    if document_type is not None and document_type not in IDENTITY_DOCUMENT_TYPES:
        raise ToolError(f"document_type must be one of {list(IDENTITY_DOCUMENT_TYPES)}.")
    if doc is None and not document_type:
        raise ToolError("document_type is required to add an identity document.")
    ident = _text("identifier", identifier, 100)
    number = _text("document_number", document_number)
    authority = _text("issuing_authority", issuing_authority, 120)
    country = _text("country_of_issue", country_of_issue, 80)
    region = _text("state_or_region_of_issue", state_or_region_of_issue, 80)
    note = _text("notes", notes)
    issued = _date("issue_date", issue_date)
    expires = _date("expiration_date", expiration_date)
    images: Dict[str, InboundAttachmentRef] = {}
    for side, idx in (("front", front_media_index), ("back", back_media_index)):
        if idx is not None:
            if int(idx) == 0:
                raise ToolError(f"{side}_media_index must name one attachment (1, 2, …).")
            images[side] = _inbound_refs(ctx, int(idx), images_only=True)[0]

    created = doc is None
    if doc is None:
        doc = models.IdentityDocument(
            person_id=subject.person_id,
            document_type=document_type,
            country_of_issue=country or "United States",
        )
        if authority is None and document_type == "united_mileage_plus":
            authority = "United Airlines"
    _assign(doc, "document_type", document_type)
    _assign(doc, "identifier", ident)
    _assign(doc, "issuing_authority", authority)
    if country is not None:
        doc.country_of_issue = country or "United States"
    _assign(doc, "state_or_region_of_issue", region)
    _assign(doc, "notes", note)
    _assign(doc, "issue_date", issued)
    _assign(doc, "expiration_date", expires)
    if number is not None:
        doc.document_number_encrypted = crypto.encrypt_str(number) if number else None
        doc.document_number_last_four = crypto.last_four(number) if number else None

    if created:
        db.add(doc)
    db.flush()

    root = get_settings().storage_root
    for side, ref in images.items():
        with open(root / ref.stored_path, "rb") as fh:
            rel_path, _size, _mime = storage.save_identity_document_image(
                ctx.family_id,
                subject.person_id,
                doc.identity_document_id,
                side,
                fh,
                ref.filename or f"{side}.jpg",
            )
        attr = "front_image_path" if side == "front" else "back_image_path"
        previous = getattr(doc, attr)
        setattr(doc, attr, rel_path)
        db.flush()
        if previous and previous != rel_path:
            storage.delete_if_exists(previous)

    db.refresh(doc)
    _log_write(
        ctx, "created" if created else "updated", "identity_document",
        doc.identity_document_id, subject,
    )
    return {
        "action": "created" if created else "updated",
        "person": _name(subject),
        "identity_document": _identity_document_out(doc),
    }


# ---------------------------------------------------------------------------
# document_save_from_message
# ---------------------------------------------------------------------------


DOCUMENT_SAVE_FROM_MESSAGE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "media_index": {
            "type": "integer",
            "description": "1-based 'Attachment N' to save, or 0 for every attachment.",
        },
        "title": {
            "type": "string",
            "description": "Human-readable title, e.g. '2025 W-2'. Defaults to the file name.",
        },
        "person_id": {
            "type": "integer",
            "description": "Who the document is about. Defaults to the speaker.",
        },
        "family_wide": {
            "type": "boolean",
            "description": "Save as a household document not tied to one person.",
        },
        "document_category": {"type": "string", "enum": list(DOCUMENT_CATEGORIES)},
        "notes": {"type": "string"},
    },
    "required": ["media_index"],
}


async def handle_document_save_from_message(
    ctx: ToolContext,
    media_index: int,
    title: Optional[str] = None,
    person_id: Optional[int] = None,
    family_wide: bool = False,
    document_category: Optional[str] = None,
    notes: Optional[str] = None,
) -> Dict[str, Any]:
    if family_wide and person_id is not None:
        raise ToolError("Pass either person_id or family_wide, not both.")
    subject = None if family_wide else _subject_for_new(ctx, person_id)
    _authorize(ctx, subject)

    if document_category is not None and document_category not in DOCUMENT_CATEGORIES:
        raise ToolError(f"document_category must be one of {list(DOCUMENT_CATEGORIES)}.")
    base_title = _text("title", title, 190) or None
    note = _text("notes", notes) or None
    targets = _inbound_refs(ctx, media_index)

    root = get_settings().storage_root
    saved: List[Dict[str, Any]] = []
    for i, ref in enumerate(targets, start=1):
        filename = ref.filename or f"attachment-{ref.media_index}.bin"
        with open(root / ref.stored_path, "rb") as fh:
            rel_path, size, mime = storage.save_document(
                ctx.family_id,
                subject.person_id if subject is not None else None,
                fh,
                filename,
            )
        doc_title = base_title or filename
        if base_title and len(targets) > 1:
            doc_title = f"{base_title} ({i})"
        row = models.Document(
            family_id=ctx.family_id,
            person_id=subject.person_id if subject is not None else None,
            title=doc_title,
            document_category=document_category,
            stored_file_path=rel_path,
            original_file_name=filename[:255],
            mime_type=ref.mime_type or mime,
            file_size_bytes=size,
            notes=note,
        )
        ctx.db.add(row)
        ctx.db.flush()
        ctx.db.refresh(row)
        _log_write(ctx, "created", "document", row.document_id, subject)
        saved.append(_document_out(row))

    return {"person": _name(subject), "documents": saved, "n_saved": len(saved)}


# ---------------------------------------------------------------------------
# person_photo_save_from_message
# ---------------------------------------------------------------------------


PERSON_PHOTO_SAVE_FROM_MESSAGE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "media_index": {
            "type": "integer",
            "description": "1-based 'Attachment N' to save, or 0 for every image attachment.",
        },
        "person_id": {
            "type": "integer",
            "description": "Who is in the photo. Defaults to the speaker.",
        },
        "set_as_primary": {
            "type": "boolean",
            "description": (
                "Make this the person's primary (profile) photo, replacing "
                "the current one. Needs exactly one image."
            ),
        },
        "add_to_gallery": {
            "type": "boolean",
            "description": (
                "Also keep it in the person's additional photos. Defaults "
                "to true, or false when set_as_primary is true."
            ),
        },
        "title": {
            "type": "string",
            "description": "Short gallery title, e.g. 'Beach trip 2026'.",
        },
        "description": {"type": "string"},
        "use_for_face_recognition": {
            "type": "boolean",
            "description": (
                "Teach Avi's face recognition from gallery photos. Default "
                "true; pass false for group shots or when the face is "
                "hidden or tiny."
            ),
        },
    },
    "required": ["media_index"],
}


_CHANNEL_LABELS = {"whatsapp": "WhatsApp", "sms": "SMS", "telegram": "Telegram", "email": "email"}


def _schedule_face_enrollment(photo_ids: List[int]) -> None:
    """Embed new gallery photos off the reply path.

    InsightFace can take many seconds on a cold worker, and the inbound
    surfaces run the agent on a short-lived event loop, so a plain daemon
    thread is used instead of an executor the loop would wait on.
    """
    if not photo_ids:
        return
    from ...enrollment import enroll_photo

    def _run() -> None:
        for pid in photo_ids:
            enroll_photo(pid)

    threading.Thread(target=_run, name="avi-face-enroll", daemon=True).start()


def _person_photo_out(p: models.PersonPhoto) -> Dict[str, Any]:
    return {
        "person_photo_id": p.person_photo_id,
        "person_id": p.person_id,
        "title": p.title,
        "use_for_face_recognition": p.use_for_face_recognition,
        "original_file_name": p.original_file_name,
    }


async def handle_person_photo_save_from_message(
    ctx: ToolContext,
    media_index: int,
    person_id: Optional[int] = None,
    set_as_primary: bool = False,
    add_to_gallery: Optional[bool] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    use_for_face_recognition: bool = True,
) -> Dict[str, Any]:
    subject = _subject_for_new(ctx, person_id)
    _authorize(ctx, subject)

    gallery = (not set_as_primary) if add_to_gallery is None else bool(add_to_gallery)
    if not set_as_primary and not gallery:
        raise ToolError(
            "Nothing to do: pass set_as_primary=true and/or add_to_gallery=true."
        )
    base_title = _text("title", title, 190) or None
    desc = _text("description", description) or None
    targets = _inbound_refs(ctx, media_index, images_only=True)
    if set_as_primary and len(targets) != 1:
        raise ToolError(
            "A person has one primary photo. Pass the media_index of the "
            "single attachment to use (others can go in the gallery with a "
            "second call)."
        )

    db = ctx.db
    root = get_settings().storage_root
    result: Dict[str, Any] = {"person": _name(subject), "person_id": subject.person_id}

    if set_as_primary:
        ref = targets[0]
        with open(root / ref.stored_path, "rb") as fh:
            rel_path, _size = storage.save_profile_photo(
                ctx.family_id, subject.person_id, fh, ref.filename or "photo.jpg"
            )
        previous = subject.profile_photo_path
        subject.profile_photo_path = rel_path
        db.flush()
        if previous and previous != rel_path:
            storage.delete_if_exists(previous)
        _log_write(ctx, "updated", "profile_photo", subject.person_id, subject)
        result["primary_photo_updated"] = True

    saved: List[models.PersonPhoto] = []
    if gallery:
        channel = _CHANNEL_LABELS.get(targets[0].channel or "", "a message")
        default_title = f"Photo from {channel} {_today(ctx).isoformat()}"
        for i, ref in enumerate(targets, start=1):
            filename = ref.filename or f"photo-{ref.media_index}.jpg"
            with open(root / ref.stored_path, "rb") as fh:
                rel_path, size, mime = storage.save_person_photo(
                    ctx.family_id, subject.person_id, fh, filename
                )
            photo_title = base_title or default_title
            if len(targets) > 1:
                photo_title = f"{photo_title} ({i})"
            photo = models.PersonPhoto(
                person_id=subject.person_id,
                title=photo_title[:200],
                description=desc,
                use_for_face_recognition=bool(use_for_face_recognition),
                stored_file_path=rel_path,
                original_file_name=filename[:255],
                mime_type=ref.mime_type or mime,
                file_size_bytes=size,
            )
            db.add(photo)
            db.flush()
            db.refresh(photo)
            _log_write(ctx, "created", "person_photo", photo.person_photo_id, subject)
            saved.append(photo)
        result["gallery_photos"] = [_person_photo_out(p) for p in saved]

    to_enroll = [p.person_photo_id for p in saved if p.use_for_face_recognition]
    if to_enroll:
        # The enrollment thread opens its own session, so the rows must be
        # committed before it starts.
        db.commit()
        _schedule_face_enrollment(to_enroll)
        result["face_recognition"] = "enrolling in the background"
    return result

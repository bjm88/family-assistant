"""``send_file`` — attach a stored household file to the reply.

"Send me a picture of Jackson's passport", "text me a photo of the
BMW", "email me the dental insurance PDF". The tool works out which
stored file is meant, checks that the person asking may have it and
that the channel they are on can carry it, and approves it for the
reply. The channel's inbox service delivers approved files once the
agent has finished writing (see :mod:`api.services.outbound_files`).

The file always goes to the person Avi is talking to, on the channel
they wrote in on. This tool cannot send a file to anyone else.

Who may receive what:

* **ID scans** (passport, driver's license, …) and **documents filed
  under one person** follow the same rule as reading that person's ID
  numbers: the person themselves, their spouse, or their parent.
* **Household documents** (no person attached) and **photos** of
  people, pets, vehicles and homes can go to any identified member of
  the household.
* Nothing is sent to someone Avi hasn't identified, and private files
  are not posted where other people can read the reply (a shared
  Slack channel, a Telegram group).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import select

from .... import models
from ....services import outbound_files
from ... import authz
from .._registry import ToolContext, ToolError
from . import _calendar_helpers


logger = logging.getLogger(__name__)


SEND_FILE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "what": {
            "type": "string",
            "enum": list(outbound_files.SOURCES),
            "description": (
                "Which kind of stored file to send. identity_document = "
                "the scan of a passport, driver's license or other ID. "
                "document = a saved document such as an insurance card "
                "or a PDF. vehicle_photo, person_photo, pet_photo, "
                "residence_photo = the photo kept for that car, person, "
                "pet or home."
            ),
        },
        "person": {
            "type": "string",
            "description": (
                "Name of the family member the file is about: whose "
                "passport, whose photo, who drives the car, whose "
                "document. Leave out when the speaker means their own "
                "('my passport')."
            ),
        },
        "document_type": {
            "type": "string",
            "description": (
                "For identity_document: passport, drivers_license, "
                "state_id, birth_certificate, permanent_resident_card, "
                "global_entry, military_id or other."
            ),
        },
        "query": {
            "type": "string",
            "description": (
                "Words that pick out the record: a car's make, model or "
                "nickname ('BMW'), a pet's name, a home's label, or "
                "words from a document's title ('dental insurance')."
            ),
        },
        "record_id": {
            "type": "integer",
            "description": (
                "Exact id of the record, when you already know it (for "
                "example from a list this tool returned)."
            ),
        },
        "side": {
            "type": "string",
            "enum": ["front", "back", "both"],
            "description": (
                "For identity_document: which side to send. Defaults to "
                "the front."
            ),
        },
    },
    "required": ["what"],
}


_PRIVACY_REFUSAL = (
    "Don't send this file — the speaker isn't the person it belongs to, "
    "their spouse, or their parent, so it stays private. Keep the reply "
    "warm and short: say you can't send that one, without lecturing "
    "about privacy rules or quoting this message, and suggest they ask "
    "the person themselves or a parent. Do not say a file is attached."
)

# Words that describe the request, not the record.
_FILLER = frozenset(
    "a an the of for to my our his her their your me us please send photo "
    "photos picture pictures pic pics image images scan copy file files "
    "car cars vehicle truck pet house home document documents doc pdf card".split()
)

_DOC_TYPE_ALIASES = {
    "license": "drivers_license",
    "licence": "drivers_license",
    "driver_license": "drivers_license",
    "drivers_licence": "drivers_license",
    "driver_licence": "drivers_license",
    "driving_license": "drivers_license",
    "driving_licence": "drivers_license",
    "dl": "drivers_license",
    "id": "state_id",
    "id_card": "state_id",
    "green_card": "permanent_resident_card",
}


def _words(text: Optional[str]) -> List[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def _matches(query: Optional[str], *fields: Any) -> bool:
    """Every meaningful word of ``query`` appears in the record's text."""
    wanted = [w for w in _words(query) if w not in _FILLER]
    if not wanted:
        return True
    haystack = set(_words(" ".join(str(f) for f in fields if f)))
    return all(
        any(h == w or h.startswith(w) for h in haystack) for w in wanted
    )


def _normalise_document_type(value: Optional[str]) -> Optional[str]:
    text = (value or "").lower().replace("'", "").replace("’", "")
    slug = "_".join(_words(text))
    if not slug:
        return None
    return _DOC_TYPE_ALIASES.get(slug, slug)


def _name(person: Optional[models.Person]) -> str:
    if person is None:
        return "someone"
    return person.preferred_name or person.first_name or "someone"


def _subject(ctx: ToolContext, person: Optional[str]) -> Optional[models.Person]:
    """The family member a name refers to; the speaker for "me" or nothing."""
    text = (person or "").strip()
    if not text or text.lower() in _calendar_helpers._SELF_WORDS:
        return ctx.db.get(models.Person, ctx.person_id) if ctx.person_id else None
    found = _calendar_helpers.find_family_member(ctx, text)
    if found is None:
        raise ToolError(
            f"Nobody in this household is called {text!r}. Check the "
            "name with the speaker."
        )
    return found


def _may_see_private(ctx: ToolContext, subject_person_id: int, *, audit: bool) -> bool:
    return authz.can_access_sensitive(
        ctx.db,
        requestor_person_id=ctx.person_id,
        subject_person_id=int(subject_person_id),
        requestor_is_admin=ctx.is_admin,
        audit_log=audit,
    ).allowed


def _pick_one(matches: Sequence[Any], describe, *, noun: str, none_message: str) -> Any:
    if not matches:
        raise ToolError(none_message)
    if len(matches) > 1:
        listing = "; ".join(describe(m) for m in matches[:12])
        raise ToolError(
            f"More than one {noun} matches: {listing}. Call send_file "
            "again with the record_id of the right one, or ask the "
            "speaker which they mean."
        )
    return matches[0]


# ---------------------------------------------------------------------------
# Finding the record for each kind of file
# ---------------------------------------------------------------------------


def _find_identity_documents(
    ctx: ToolContext,
    *,
    person: Optional[str],
    document_type: Optional[str],
    query: Optional[str],
    record_id: Optional[int],
    side: Optional[str],
) -> List[outbound_files.OutboundFile]:
    if record_id is not None:
        doc = ctx.db.get(models.IdentityDocument, int(record_id))
        owner = ctx.db.get(models.Person, doc.person_id) if doc is not None else None
        if doc is None or owner is None or owner.family_id != ctx.family_id:
            raise ToolError("There is no ID document with that record_id.")
    else:
        owner = _subject(ctx, person)
        doc = None
    if owner is None:
        raise ToolError("Say whose document this is (the `person` argument).")

    # Decide before describing anything about the document.
    if not _may_see_private(ctx, owner.person_id, audit=True):
        raise ToolError(_PRIVACY_REFUSAL)

    if doc is None:
        docs = list(
            ctx.db.execute(
                select(models.IdentityDocument)
                .where(models.IdentityDocument.person_id == owner.person_id)
                .order_by(models.IdentityDocument.identity_document_id)
            ).scalars()
        )
        wanted_type = _normalise_document_type(document_type)
        if wanted_type is None:
            # "passport" sometimes arrives in `query` instead.
            from_query = _normalise_document_type(query)
            if from_query in {d.document_type for d in docs}:
                wanted_type = from_query
        on_file = ", ".join(
            f"{outbound_files.document_type_words(d.document_type)} "
            f"({'picture saved' if d.front_image_path else 'no picture saved'})"
            for d in docs
        )
        if wanted_type:
            typed = [d for d in docs if d.document_type == wanted_type]
            if not typed:
                raise ToolError(
                    f"{_name(owner)} has no "
                    f"{outbound_files.document_type_words(wanted_type)} on "
                    "record. "
                    + (f"On record: {on_file}." if on_file else "No ID documents are on record for them.")
                )
            docs = typed
        # Of several of one kind (an old and a new passport), prefer
        # the ones that actually have a picture.
        with_picture = [d for d in docs if d.front_image_path or d.back_image_path]
        if len(docs) > 1 and with_picture:
            docs = with_picture
        doc = _pick_one(
            docs,
            lambda d: (
                f"record_id {d.identity_document_id}: "
                f"{outbound_files.document_type_words(d.document_type)}"
            ),
            noun=f"ID document for {_name(owner)}",
            none_message=f"No ID documents are on record for {_name(owner)}.",
        )

    sides = ["front", "back"] if side == "both" else [side if side == "back" else "front"]
    files: List[outbound_files.OutboundFile] = []
    for one_side in sides:
        try:
            files.append(
                outbound_files.resolve(
                    ctx.db,
                    family_id=ctx.family_id,
                    source=outbound_files.SOURCE_IDENTITY_DOCUMENT,
                    record_id=doc.identity_document_id,
                    side=one_side,
                )
            )
        except outbound_files.FileUnavailable as exc:
            # "Both" still sends the front when no back was ever saved.
            if side == "both" and one_side == "back" and files:
                continue
            raise ToolError(
                f"{exc} Tell the speaker plainly; do not say a file is attached."
            ) from exc
    return files


def _find_document(
    ctx: ToolContext,
    *,
    person: Optional[str],
    query: Optional[str],
    record_id: Optional[int],
) -> models.Document:
    if record_id is not None:
        doc = ctx.db.get(models.Document, int(record_id))
        if doc is None or doc.family_id != ctx.family_id:
            raise ToolError("There is no document with that record_id.")
        if doc.person_id is not None and not _may_see_private(
            ctx, doc.person_id, audit=True
        ):
            raise ToolError(_PRIVACY_REFUSAL)
        return doc

    owner = _subject(ctx, person) if (person or "").strip() else None
    docs = list(
        ctx.db.execute(
            select(models.Document)
            .where(models.Document.family_id == ctx.family_id)
            .order_by(models.Document.document_id)
        ).scalars()
    )
    if owner is not None:
        # Their own documents, plus household ones the words might mean.
        own = [d for d in docs if d.person_id == owner.person_id]
        docs = own or [d for d in docs if d.person_id is None]
    matched = [
        d
        for d in docs
        if _matches(query, d.title, d.document_category, d.original_file_name, d.notes)
    ]
    # Only ever choose among, or list, documents the speaker may have.
    allowed_cache: Dict[int, bool] = {}

    def _allowed(d: models.Document) -> bool:
        if d.person_id is None:
            return True
        if d.person_id not in allowed_cache:
            allowed_cache[d.person_id] = _may_see_private(ctx, d.person_id, audit=False)
        return allowed_cache[d.person_id]

    visible = [d for d in matched if _allowed(d)]
    if matched and not visible:
        _may_see_private(ctx, matched[0].person_id, audit=True)  # record the refusal
        raise ToolError(_PRIVACY_REFUSAL)
    return _pick_one(
        visible,
        lambda d: f"record_id {d.document_id}: {d.title}",
        noun="saved document",
        none_message=(
            "No saved document matches that. Use different words from "
            "its title, or tell the speaker it isn't on file."
        ),
    )


def _find_vehicle(
    ctx: ToolContext, *, person: Optional[str], query: Optional[str], record_id: Optional[int]
) -> models.Vehicle:
    if record_id is not None:
        vehicle = ctx.db.get(models.Vehicle, int(record_id))
        if vehicle is None or vehicle.family_id != ctx.family_id:
            raise ToolError("There is no vehicle with that record_id.")
        return vehicle
    vehicles = list(
        ctx.db.execute(
            select(models.Vehicle)
            .where(models.Vehicle.family_id == ctx.family_id)
            .order_by(models.Vehicle.vehicle_id)
        ).scalars()
    )

    def _describe(v: models.Vehicle) -> str:
        driver = ctx.db.get(models.Person, v.primary_driver_person_id) if v.primary_driver_person_id else None
        return (
            f"record_id {v.vehicle_id}: {outbound_files.vehicle_title(v)}"
            + (f" (driven by {_name(driver)})" if driver else "")
        )

    matched = vehicles
    if (person or "").strip():
        driver = _subject(ctx, person)
        matched = [
            v for v in matched if driver and v.primary_driver_person_id == driver.person_id
        ]
        if not matched:
            raise ToolError(
                f"No vehicle lists {_name(driver)} as its driver. Vehicles "
                "on file: " + ("; ".join(_describe(v) for v in vehicles) or "none") + "."
            )
    matched = [
        v
        for v in matched
        if _matches(query, v.nickname, v.year, v.make, v.model, v.trim, v.color, v.body_style, v.vehicle_type)
    ]
    return _pick_one(
        matched,
        _describe,
        noun="vehicle",
        none_message=(
            "No vehicle matches that. Vehicles on file: "
            + ("; ".join(_describe(v) for v in vehicles) or "none")
            + "."
        ),
    )


def _find_pet(ctx: ToolContext, *, query: Optional[str], record_id: Optional[int]) -> models.Pet:
    if record_id is not None:
        pet = ctx.db.get(models.Pet, int(record_id))
        if pet is None or pet.family_id != ctx.family_id:
            raise ToolError("There is no pet with that record_id.")
        return pet
    pets = list(
        ctx.db.execute(
            select(models.Pet).where(models.Pet.family_id == ctx.family_id).order_by(models.Pet.pet_id)
        ).scalars()
    )
    matched = [p for p in pets if _matches(query, p.pet_name, p.animal_type, p.breed)]
    return _pick_one(
        matched,
        lambda p: f"record_id {p.pet_id}: {p.pet_name} ({p.animal_type})",
        noun="pet",
        none_message="No pet matches that name.",
    )


def _find_residence(
    ctx: ToolContext, *, query: Optional[str], record_id: Optional[int]
) -> models.Residence:
    if record_id is not None:
        home = ctx.db.get(models.Residence, int(record_id))
        if home is None or home.family_id != ctx.family_id:
            raise ToolError("There is no residence with that record_id.")
        return home
    homes = list(
        ctx.db.execute(
            select(models.Residence)
            .where(models.Residence.family_id == ctx.family_id)
            .order_by(models.Residence.residence_id)
        ).scalars()
    )
    matched = [h for h in homes if _matches(query, h.label, h.street_line_1, h.city)]
    if len(matched) > 1:
        # "A picture of the house" means the one the family lives in.
        primary = [h for h in matched if h.is_primary_residence]
        if len(primary) == 1:
            matched = primary
    return _pick_one(
        matched,
        lambda h: f"record_id {h.residence_id}: {h.label}",
        noun="residence",
        none_message="No residence matches that.",
    )


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


async def handle_send_file(
    ctx: ToolContext,
    what: str,
    person: Optional[str] = None,
    document_type: Optional[str] = None,
    query: Optional[str] = None,
    record_id: Optional[int] = None,
    side: Optional[str] = None,
    **_ignored: Any,
) -> Dict[str, Any]:
    """Approve one stored file (two for both sides of an ID) for the reply."""
    channel = ctx.reply_channel
    if channel not in outbound_files.CHANNEL_RULES:
        raise ToolError(
            "Files can't be sent in this conversation. They can be sent "
            "when the person writes to you by email, Slack, WhatsApp, "
            "text message or Telegram — suggest they ask there. Do not "
            "say a file is attached."
        )
    if ctx.person_id is None and not ctx.is_admin:
        raise ToolError(
            "I can't send household files without knowing who is asking. "
            "Do not say a file is attached."
        )
    if not ctx.is_admin:
        speaker = ctx.db.get(models.Person, ctx.person_id)
        if speaker is None or speaker.family_id != ctx.family_id:
            raise ToolError(
                "Household files only go to members of this household. "
                "Do not say a file is attached."
            )
    if what not in outbound_files.SOURCES:
        raise ToolError(
            f"Unknown kind of file {what!r}. Use one of: "
            + ", ".join(outbound_files.SOURCES)
            + "."
        )

    try:
        if what == outbound_files.SOURCE_IDENTITY_DOCUMENT:
            files = _find_identity_documents(
                ctx,
                person=person,
                document_type=document_type,
                query=query,
                record_id=record_id,
                side=side,
            )
        else:
            if what == outbound_files.SOURCE_DOCUMENT:
                rid = _find_document(
                    ctx, person=person, query=query, record_id=record_id
                ).document_id
            elif what == outbound_files.SOURCE_VEHICLE_PHOTO:
                rid = _find_vehicle(
                    ctx, person=person, query=query, record_id=record_id
                ).vehicle_id
            elif what == outbound_files.SOURCE_PERSON_PHOTO:
                if record_id is not None:
                    rid = int(record_id)
                else:
                    subject = _subject(ctx, person or query)
                    if subject is None:
                        raise ToolError("Say whose photo to send (the `person` argument).")
                    rid = subject.person_id
            elif what == outbound_files.SOURCE_PET_PHOTO:
                rid = _find_pet(ctx, query=query or person, record_id=record_id).pet_id
            else:
                rid = _find_residence(ctx, query=query, record_id=record_id).residence_id
            files = [
                outbound_files.resolve(
                    ctx.db, family_id=ctx.family_id, source=what, record_id=rid
                )
            ]
    except outbound_files.FileUnavailable as exc:
        raise ToolError(
            f"{exc} Tell the speaker plainly; do not say a file is attached."
        ) from exc

    approved: List[outbound_files.OutboundFile] = []
    for file in files:
        if file.is_private and ctx.reply_is_shared:
            logger.info(
                "[send_file] DENY requestor=%s file=%s:%s reason=shared_conversation",
                ctx.person_id, file.source, file.record_id,
            )
            raise ToolError(
                "Other people can read this conversation, so a private "
                "document can't be posted here. Ask the speaker to "
                "message you directly and you'll send it there. Do not "
                "say a file is attached."
            )
        if any(f.ref() == file.ref() for f in ctx.reply_files):
            approved.append(file)
            continue
        problem = outbound_files.problem_for_channel(
            file, channel, already=ctx.reply_files
        )
        if problem:
            logger.info(
                "[send_file] DENY requestor=%s file=%s:%s channel=%s reason=channel_limit",
                ctx.person_id, file.source, file.record_id, channel,
            )
            raise ToolError(
                f"{problem} Tell the speaker; do not say a file is attached."
            )
        if outbound_files.needs_conversion(file, channel):
            # Find out now, not when the phone network asks for it.
            try:
                outbound_files.prepare(file, channel)
            except outbound_files.FileUnavailable as exc:
                raise ToolError(
                    f"{exc} Email or Slack can carry the original. Tell "
                    "the speaker; do not say a file is attached."
                ) from exc
        ctx.reply_files.append(file)
        approved.append(file)
        logger.info(
            "[send_file] ALLOW requestor=%s file=%s:%s side=%s channel=%s private=%s",
            ctx.person_id, file.source, file.record_id, file.side, channel, file.is_private,
        )

    where = outbound_files.channel_name(channel)
    return {
        "attached": [f.label for f in approved],
        # Read back by the channel when the reply is sent.
        "attachments": [f.ref() for f in approved],
        "delivery": (
            f"Approved. The file will be delivered with your reply on {where}; "
            "you don't need to do anything else. In your reply, say in a "
            "few words what you're sending. Don't describe what the "
            "picture shows — you haven't looked at it."
        ),
    }


__all__ = ["SEND_FILE_SCHEMA", "handle_send_file"]

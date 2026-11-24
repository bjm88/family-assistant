"""File attachment-only inbound messages without asking.

When a family member sends Avi nothing but photos / PDFs, each file is
OCR'd and classified (:mod:`api.ai.document_intake`) and then filed:

* identity document (passport, license, …) → an ``identity_documents``
  row on the holder, with the photo as the front / back scan;
* anything else document-like → a ``documents`` row linked to the person
  it is about, or a household document when it isn't about anyone;
* anything Avi isn't sure about (ordinary photo, unreadable or unclear
  file, holder / subject not in the family, several people named) is not
  filed: Avi asks, and the question plus the file are kept in
  ``pending_attachments`` so the sender's next message can answer it
  (inbound surfaces carry no chat history).

Writes follow the same rule as Avi's record tools
(:func:`api.ai.authz.can_edit_person_records`). The returned text block is
appended to the agent's user message so Avi can confirm what he filed or
ask his question.
"""

from __future__ import annotations

import logging
import mimetypes
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import crypto, models, storage
from ..ai import authz, document_intake
from ..ai.document_intake import DocumentAnalysis, IdentityFields
from ..ai.tools import InboundAttachmentRef
from ..config import get_settings

logger = logging.getLogger(__name__)


_CHANNEL_LABELS = {
    "sms": "text message",
    "whatsapp": "WhatsApp message",
    "telegram": "Telegram message",
    "email": "email",
}


def is_attachment_only(message_text: Optional[str]) -> bool:
    """True when the sender typed nothing (or only emoji / punctuation)."""
    return not re.search(r"\w", message_text or "")


FILED = "filed"
ASK = "ask"
DENIED = "denied"
SKIPPED = "skipped"

_OUTCOME_LABELS = {
    FILED: "FILED",
    ASK: "ASK THE SENDER",
    DENIED: "NOT FILED",
    SKIPPED: "NOT FILED",
}

PENDING_TTL = timedelta(days=3)


@dataclass
class FiledAttachment:
    media_index: int
    filename: str
    outcome: str  # FILED | ASK | DENIED | SKIPPED
    line: str
    record_id: Optional[int] = None


def auto_file_attachments(
    db: Session,
    *,
    family_id: int,
    sender: models.Person,
    message_text: Optional[str],
    refs: Iterable[InboundAttachmentRef],
    requestor_is_admin: bool = False,
) -> str:
    """File every photo / PDF on an attachment-only message.

    Returns the agent-facing report block, or ``""`` when nothing applied
    (message had text, feature disabled, no attachments). Never raises:
    a failure on one file is reported and the rest still get filed.
    """
    settings = get_settings()
    refs = list(refs)
    if (
        not refs
        or not settings.AI_ATTACHMENT_AUTO_FILE_ENABLED
        or not settings.AI_VISION_ENABLED
        or not is_attachment_only(message_text)
    ):
        return ""

    people = list(
        db.execute(
            select(models.Person).where(models.Person.family_id == family_id)
        ).scalars()
    )
    results: List[FiledAttachment] = []
    for n, ref in enumerate(refs):
        if n >= settings.AI_ATTACHMENT_MAX_PER_MESSAGE:
            result = _result(
                ref, ASK, "not analysed (too many attachments in one message). "
                "Ask what it is if it matters.",
            )
        else:
            try:
                result = _file_one(
                    db,
                    family_id=family_id,
                    sender=sender,
                    people=people,
                    ref=ref,
                    requestor_is_admin=requestor_is_admin,
                )
                db.commit()
            except Exception as e:  # noqa: BLE001 - one bad file must not sink the message
                db.rollback()
                logger.exception(
                    "[intake] filing crashed family=%s media_index=%s file=%s",
                    family_id, ref.media_index, ref.filename,
                )
                result = _result(
                    ref, ASK, f"filing failed ({type(e).__name__}). Ask what it is "
                    "and whose it is so you can file it by hand.",
                )
        if result.outcome == ASK:
            _remember_question(db, family_id=family_id, sender=sender, ref=ref, question=result.line)
        results.append(result)

    logger.info(
        "[intake] auto-file family=%s sender=%s outcomes=[%s]",
        family_id,
        sender.person_id,
        ",".join(r.outcome for r in results),
    )
    return render_report(results)


def render_report(results: List[FiledAttachment]) -> str:
    if not results:
        return ""
    lines = [
        "--- Avi auto-filed these attachments ---",
        (
            "The sender sent only attachments, so you already read them. FILED "
            "ones are saved as described — confirm briefly what you saved and for "
            "whom. For ASK THE SENDER ones nothing was saved: ask the question in "
            "your reply, short and specific (one question per file). Their answer "
            "will come back to you with the file attached again. For NOT FILED "
            "ones, say why."
        ),
    ]
    for r in results:
        label = _OUTCOME_LABELS.get(r.outcome, r.outcome.upper())
        lines.append(f"[Attachment {r.media_index}: {r.filename}] {label}: {r.line}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Open questions → the sender's next message
# ---------------------------------------------------------------------------


def _remember_question(
    db: Session,
    *,
    family_id: int,
    sender: models.Person,
    ref: InboundAttachmentRef,
    question: str,
) -> None:
    try:
        db.add(
            models.PendingAttachment(
                family_id=family_id,
                person_id=sender.person_id,
                channel=ref.channel,
                filename=(ref.filename or f"attachment-{ref.media_index}")[:255],
                mime_type=ref.mime_type,
                size_bytes=ref.size_bytes,
                stored_path=ref.stored_path,
                question=question,
            )
        )
        db.commit()
    except Exception:  # noqa: BLE001 - the reply still asks; only the follow-up is lost
        db.rollback()
        logger.exception("[intake] could not remember question file=%s", ref.filename)


def open_questions(db: Session, *, person_id: int) -> List[models.PendingAttachment]:
    """Files this sender was asked about recently and hasn't answered yet."""
    cutoff = datetime.now(timezone.utc) - PENDING_TTL
    return list(
        db.execute(
            select(models.PendingAttachment)
            .where(models.PendingAttachment.person_id == person_id)
            .where(models.PendingAttachment.consumed_at.is_(None))
            .where(models.PendingAttachment.created_at >= cutoff)
            .order_by(models.PendingAttachment.pending_attachment_id)
        ).scalars()
    )


def followup_for_agent(
    db: Session,
    pending: List[models.PendingAttachment],
    *,
    first_index: int,
) -> tuple[List[InboundAttachmentRef], str]:
    """Re-attach ``pending`` files to this turn and explain them to the agent.

    Each question is offered once: it is marked consumed here, whether or
    not the sender's message turns out to answer it.
    """
    if not pending:
        return [], ""
    root = get_settings().storage_root
    refs: List[InboundAttachmentRef] = []
    lines = [
        "--- Files you asked this sender about earlier ---",
        (
            "Nothing was saved for these yet. Their message above is probably "
            "the answer: if so, file each one with the record tools using the "
            "attachment number below (identity_document_save with "
            "front_media_index / back_media_index, document_save_from_message, "
            "or person_photo_save_from_message) and confirm. If it's still "
            "unclear, ask again. If their message is about something else, "
            "handle that and ignore these."
        ),
    ]
    now = datetime.now(timezone.utc)
    for p in pending:
        p.consumed_at = now
        if not (root / p.stored_path).is_file():
            continue
        idx = first_index + len(refs)
        refs.append(
            InboundAttachmentRef(
                media_index=idx,
                filename=p.filename,
                mime_type=p.mime_type,
                size_bytes=p.size_bytes,
                stored_path=p.stored_path,
                channel=p.channel or "",
            )
        )
        lines.append(f"[Attachment {idx}: {p.filename}] You noted: {p.question}")
    db.commit()
    logger.info(
        "[intake] follow-up person=%s offered=%d", pending[0].person_id, len(refs)
    )
    return refs, "\n".join(lines) if refs else ""


# ---------------------------------------------------------------------------
# One attachment
# ---------------------------------------------------------------------------


def _file_one(
    db: Session,
    *,
    family_id: int,
    sender: models.Person,
    people: List[models.Person],
    ref: InboundAttachmentRef,
    requestor_is_admin: bool,
) -> FiledAttachment:
    mime = _effective_mime(ref.mime_type, ref.filename)
    if mime is None:
        return _result(ref, SKIPPED, "not a photo or PDF, so it was not filed.")
    path = get_settings().storage_root / ref.stored_path
    try:
        analysis = document_intake.analyze(
            path, mime_type=mime, family_names=[_full_name(p) for p in people]
        )
    except document_intake.AnalysisError as e:
        logger.warning(
            "[intake] analysis failed family=%s file=%s: %s", family_id, ref.filename, e
        )
        return _result(
            ref, ASK,
            f"I couldn't read this file ({e}). Ask what it is and whose it is.",
        )

    logger.info(
        "[intake] analysed family=%s file=%s kind=%s confident=%s category=%s "
        "names=%d ocr_chars=%d",
        family_id, ref.filename, analysis.kind, analysis.confident,
        analysis.document_category, len(analysis.person_names), len(analysis.ocr_text),
    )

    about = f" ({analysis.summary})" if analysis.summary else ""
    if analysis.kind == document_intake.KIND_PHOTO:
        return _result(
            ref, ASK,
            f"an ordinary photo, not a document{about}. Ask what they'd like "
            "done with it — e.g. save it as someone's photo.",
        )
    if not analysis.confident:
        return _result(
            ref, ASK,
            f"possibly '{analysis.title or 'a document'}', but I'm not sure{about}. "
            "Ask what it is and whose it is before filing.",
        )

    ctx = _Ctx(db, family_id, sender, people, ref, requestor_is_admin, mime, path)
    if analysis.kind == document_intake.KIND_IDENTITY and analysis.identity is not None:
        return _file_identity(ctx, analysis, analysis.identity)
    return _file_document(ctx, analysis)


@dataclass
class _Ctx:
    db: Session
    family_id: int
    sender: models.Person
    people: List[models.Person]
    ref: InboundAttachmentRef
    requestor_is_admin: bool
    mime: str
    path: Path

    def allowed(self, subject: Optional[models.Person]) -> bool:
        return authz.can_edit_person_records(
            self.db,
            requestor_person_id=self.sender.person_id,
            subject_person_id=subject.person_id if subject is not None else None,
            family_id=self.family_id,
            requestor_is_admin=self.requestor_is_admin,
        ).allowed

    def default_owner(self) -> Optional[models.Person]:
        """Household-wide for a parent, otherwise the sender's own records."""
        if self.requestor_is_admin or authz.is_primary_parent(self.sender):
            return None
        return self.sender

    @property
    def source_note(self) -> str:
        label = _CHANNEL_LABELS.get(self.ref.channel or "", "message")
        return f"Filed by Avi from a {label} attachment ({self.ref.filename})."


def _file_identity(ctx: _Ctx, analysis: DocumentAnalysis, ident: IdentityFields) -> FiledAttachment:
    db = ctx.db
    number = ident.document_number if ident.number_trusted else None
    last4 = crypto.last_four(number) if number else None
    holder = document_intake.match_person(
        ctx.people, ident.holder_full_name, date_of_birth=ident.holder_date_of_birth
    )
    if holder is None and not ident.holder_full_name:
        holder = _holder_from_existing(ctx, ident.document_type, last4)
    label = _identity_label(ident)
    if holder is None:
        who = (
            f"for '{ident.holder_full_name}', who doesn't match anyone in the family"
            if ident.holder_full_name
            else "with no readable name"
        )
        return _result(
            ctx.ref, ASK,
            f"a {label} {who}. Ask whose it is. For a family member, file it with "
            "identity_document_save; otherwise offer to keep it in Documents.",
        )
    if not ctx.allowed(holder):
        return _result(
            ctx.ref, DENIED,
            f"{_full_name(holder)}'s {label} — not saved: only a parent can file "
            "records for someone else.",
        )

    row, conflict = _existing_identity_doc(db, holder, ident, last4)
    if conflict is not None:
        return _result(
            ctx.ref, ASK,
            f"{_full_name(holder)}'s {label}, but the number on the photo ends in "
            f"{last4} while the {label} on file (#{conflict.identity_document_id}) "
            f"ends in {conflict.document_number_last_four} and doesn't expire "
            "earlier. Nothing changed. Ask which number is right, or whether this "
            "is a new document.",
        )
    created = row is None
    if row is None:
        row = models.IdentityDocument(
            person_id=holder.person_id,
            document_type=ident.document_type,
            country_of_issue=ident.country_of_issue or "United States",
        )
        db.add(row)
    for attr in (
        "issuing_authority", "state_or_region_of_issue", "issue_date", "expiration_date",
    ):
        value = getattr(ident, attr)
        if value is not None and (created or getattr(row, attr) is None):
            setattr(row, attr, value)
    if ident.country_of_issue and (created or not row.country_of_issue):
        row.country_of_issue = ident.country_of_issue
    number_saved = False
    if number:
        if ident.document_type in ("global_entry", "united_mileage_plus"):
            if not row.identifier:
                row.identifier = number[:100]
                number_saved = True
        elif not row.document_number_encrypted:
            row.document_number_encrypted = crypto.encrypt_str(number)
            row.document_number_last_four = last4
            number_saved = True
    row.notes = _merge_scan_notes(row.notes, ident, ctx.source_note)
    profile_notes = _fill_person_from_id(holder, ident)
    db.flush()

    if ctx.mime.startswith("image/"):
        side = ident.side
        with open(ctx.path, "rb") as fh:
            rel_path, _size, _mime = storage.save_identity_document_image(
                ctx.family_id, holder.person_id, row.identity_document_id,
                side, fh, ctx.ref.filename or f"{side}.jpg",
            )
        attr = "front_image_path" if side == "front" else "back_image_path"
        previous = getattr(row, attr)
        setattr(row, attr, rel_path)
        db.flush()
        if previous and previous != rel_path:
            storage.delete_if_exists(previous)
        scan_note = f"{side} image saved"
    else:
        doc = _save_document(ctx, analysis, holder, keep_text=False)
        scan_note = f"PDF saved to Documents as #{doc.document_id}"

    details = []
    if number_saved:
        details.append(f"number ending {last4}")
    elif row.document_number_last_four or row.identifier:
        details.append(f"number on file ending {row.document_number_last_four or row.identifier[-4:]}")
    if row.issue_date:
        details.append(f"issued {row.issue_date.isoformat()}")
    if row.expiration_date:
        details.append(f"expires {row.expiration_date.isoformat()}")
    if row.issuing_authority:
        details.append(f"by {row.issuing_authority}")
    detail = f" ({', '.join(details)})" if details else ""
    line = (
        f"{_full_name(holder)}'s {label}{detail}. "
        f"{'Added' if created else 'Updated'} identity document "
        f"#{row.identity_document_id}; {scan_note}; other printed details saved in its notes."
    )
    if profile_notes:
        line += " " + " ".join(profile_notes)
    has_number = bool(row.document_number_encrypted or row.identifier)
    if not has_number and ident.document_type not in ("birth_certificate", "other"):
        line += (
            " The document number was NOT saved because it couldn't be read with "
            "certainty — ask them to type it (you'll store it with "
            f"identity_document_save, identity_document_id={row.identity_document_id}) "
            "or send a clearer photo of the whole page."
        )
    return _result(ctx.ref, FILED, line, row.identity_document_id)


_SCAN_NOTES_MARKER = "Read from scan:"


def _merge_scan_notes(existing: Optional[str], ident: IdentityFields, source_note: str) -> str:
    """Replace the auto-generated scan block in ``notes``, keeping anything typed by hand."""
    given = " ".join(x for x in (ident.holder_given_names, ident.holder_surname) if x)
    pairs = [
        ("Name", given or ident.holder_full_name),
        ("Sex", ident.sex),
        ("Nationality", ident.nationality),
        ("Place of birth", ident.place_of_birth),
        ("Address", ident.address),
        ("Class", ident.license_class),
        ("Restrictions", ident.restrictions),
        ("Endorsements", ident.endorsements),
        ("Height", ident.height),
        ("Eyes", ident.eye_color),
        *ident.other_fields.items(),
    ]
    lines = [f"{k}: {v}" for k, v in pairs if v]
    kept = (existing or "").split(_SCAN_NOTES_MARKER)[0].rstrip()
    if source_note not in kept and not kept.startswith("Filed by Avi"):
        kept = f"{kept}\n{source_note}".strip()
    if not lines:
        return kept
    return f"{kept}\n\n{_SCAN_NOTES_MARKER}\n" + "\n".join(lines)


def _fill_person_from_id(person: models.Person, ident: IdentityFields) -> List[str]:
    """Fill the holder's empty profile fields from the ID; report what changed or disagreed."""
    notes: List[str] = []
    name = person.preferred_name or person.first_name
    dob = ident.holder_date_of_birth
    if dob:
        if person.date_of_birth is None:
            person.date_of_birth = dob
            notes.append(f"Also set {name}'s date of birth from the ID.")
        elif person.date_of_birth != dob:
            notes.append(
                f"The date of birth on the ID differs from {name}'s profile — "
                "profile left unchanged; mention it."
            )
    if ident.holder_given_names and not person.middle_name:
        given = ident.holder_given_names.split()
        first = (person.first_name or "").lower()
        if len(given) > 1 and given[0].lower() == first:
            person.middle_name = " ".join(given[1:]).title()[:80]
            notes.append(f"Also set {name}'s middle name.")
    if ident.sex in ("M", "F") and not person.gender:
        person.gender = "male" if ident.sex == "M" else "female"
    return notes


def _file_document(ctx: _Ctx, analysis: DocumentAnalysis) -> FiledAttachment:
    matched = _unique(
        p for p in (document_intake.match_person(ctx.people, n) for n in analysis.person_names)
        if p is not None
    )
    title = analysis.title or "a document"
    if len(matched) > 1:
        names = ", ".join(_full_name(p) for p in matched)
        return _result(
            ctx.ref, ASK,
            f"'{title}', which mentions {names}. Ask whose documents it belongs "
            "in (or the household's), then file it with document_save_from_message.",
        )
    if not matched and analysis.person_names:
        names = ", ".join(analysis.person_names[:3])
        return _result(
            ctx.ref, ASK,
            f"'{title}' for {names}, who doesn't match anyone in the family. Ask "
            "who it belongs to, then file it with document_save_from_message "
            "(family_wide=true if it's a household document).",
        )
    subject = matched[0] if matched else ctx.default_owner()
    if not ctx.allowed(subject):
        return _result(
            ctx.ref, DENIED,
            f"'{title}' about {_full_name(subject)} — not saved: only a parent "
            "can file records for someone else.",
        )
    doc = _save_document(ctx, analysis, subject, keep_text=True)
    owner = f"{_full_name(subject)}'s documents" if subject else "household documents"
    summary = f" {analysis.summary}" if analysis.summary else ""
    return _result(
        ctx.ref, FILED,
        f"saved to {owner} as #{doc.document_id} '{doc.title}' "
        f"(category: {doc.document_category or 'other'}).{summary}",
        doc.document_id,
    )


def _save_document(
    ctx: _Ctx,
    analysis: DocumentAnalysis,
    subject: Optional[models.Person],
    *,
    keep_text: bool,
) -> models.Document:
    filename = ctx.ref.filename or f"attachment-{ctx.ref.media_index}"
    person_id = subject.person_id if subject is not None else None
    with open(ctx.path, "rb") as fh:
        rel_path, size, mime = storage.save_document(ctx.family_id, person_id, fh, filename)
    notes = " ".join(x for x in (analysis.summary, ctx.source_note) if x)
    if analysis.key_facts:
        notes += "\n\nKey facts:\n" + "\n".join(f"- {f}" for f in analysis.key_facts)
    row = models.Document(
        family_id=ctx.family_id,
        person_id=person_id,
        title=(analysis.title or filename)[:200],
        document_category=analysis.document_category or "other",
        stored_file_path=rel_path,
        original_file_name=filename[:255],
        mime_type=ctx.ref.mime_type or mime,
        file_size_bytes=size,
        notes=notes or None,
        extracted_text=(analysis.ocr_text or None) if keep_text else None,
    )
    ctx.db.add(row)
    ctx.db.flush()
    return row


def _holder_from_existing(
    ctx: _Ctx, document_type: str, last4: Optional[str]
) -> Optional[models.Person]:
    """Whose ID a nameless scan (usually a back side) belongs to.

    The family member already holding this type with the same last four;
    without a number, the sender when they hold exactly one of this type.
    """
    stmt = (
        select(models.IdentityDocument.person_id)
        .join(models.Person, models.Person.person_id == models.IdentityDocument.person_id)
        .where(models.Person.family_id == ctx.family_id)
        .where(models.IdentityDocument.document_type == document_type)
    )
    if last4:
        stmt = stmt.where(models.IdentityDocument.document_number_last_four == last4)
    else:
        stmt = stmt.where(models.IdentityDocument.person_id == ctx.sender.person_id)
    owners = list(ctx.db.execute(stmt).scalars())
    if len(owners) != 1 and not (last4 and len(set(owners)) == 1):
        return None
    return next((p for p in ctx.people if p.person_id == owners[0]), None)


def _existing_identity_doc(
    db: Session, holder: models.Person, ident: IdentityFields, last4: Optional[str]
) -> tuple[Optional[models.IdentityDocument], Optional[models.IdentityDocument]]:
    """``(record this scan belongs to, conflicting record)``; both None → new record.

    Same number → that record. No trusted number → the newest record of the
    type. A different number only means a renewal when the scan expires
    later than the record on file; otherwise it's flagged as a conflict so
    a misread never replaces a number someone typed in.
    """
    rows = list(
        db.execute(
            select(models.IdentityDocument)
            .where(models.IdentityDocument.person_id == holder.person_id)
            .where(models.IdentityDocument.document_type == ident.document_type)
            .order_by(models.IdentityDocument.identity_document_id.desc())
        ).scalars()
    )
    if not rows:
        return None, None
    if not last4:
        return rows[0], None
    for r in rows:
        if r.document_number_last_four == last4:
            return r, None
    newest = rows[0]
    if not newest.document_number_last_four:
        return newest, None
    if (
        ident.expiration_date
        and newest.expiration_date
        and ident.expiration_date > newest.expiration_date
    ):
        return None, None
    return None, newest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _effective_mime(mime_type: Optional[str], filename: Optional[str]) -> Optional[str]:
    mt = (mime_type or "").lower().split(";")[0].strip()
    if not mt and filename:
        mt = (mimetypes.guess_type(filename)[0] or "").lower()
    if mt.startswith("image/") or mt == "application/pdf":
        return mt
    if (filename or "").lower().endswith(".pdf"):
        return "application/pdf"
    return None


def _identity_label(ident: IdentityFields) -> str:
    return {
        "drivers_license": "driver's license",
        "passport": "passport",
        "state_id": "state ID",
        "birth_certificate": "birth certificate",
        "permanent_resident_card": "green card",
        "global_entry": "Global Entry card",
        "military_id": "military ID",
        "united_mileage_plus": "United MileagePlus card",
    }.get(ident.document_type, "ID document")


def _full_name(p: Optional[models.Person]) -> str:
    if p is None:
        return "the household"
    first = p.preferred_name or p.first_name
    return f"{first} {p.last_name}".strip()


def _unique(people: Iterable[models.Person]) -> List[models.Person]:
    seen: dict[int, models.Person] = {}
    for p in people:
        seen.setdefault(p.person_id, p)
    return list(seen.values())


def _result(
    ref: InboundAttachmentRef, outcome: str, line: str, record_id: Optional[int] = None
) -> FiledAttachment:
    return FiledAttachment(
        media_index=ref.media_index,
        filename=ref.filename or f"attachment-{ref.media_index}",
        outcome=outcome,
        line=line,
        record_id=record_id,
    )


__all__ = [
    "FiledAttachment",
    "auto_file_attachments",
    "followup_for_agent",
    "is_attachment_only",
    "open_questions",
    "render_report",
]

"""Files Avi sends back with a reply.

A family member asks "send me a picture of Jackson's passport" on
Slack, email, WhatsApp, a text or Telegram. The agent calls the
``send_file`` tool, which checks that the person asking is allowed to
have that file and that the channel can carry it, and records the
request. When the agent has finished writing its reply, the channel's
inbox service asks this module which files were approved for that
turn and delivers them alongside the text.

What can be sent is whatever the household has stored: scans of
identity documents, saved documents (insurance cards, PDFs), and the
photos kept for people, pets, vehicles and residences.

How a file travels depends on the channel:

* **Email** — attached to the reply.
* **Slack** — uploaded into the same conversation or thread.
* **Telegram** — sent as a photo or document after the reply.
* **WhatsApp / text message** — Twilio does not accept uploads; it
  fetches the file from a URL. :func:`signed_url` mints a link that
  is unguessable, names one file for one channel, and stops working
  after a few minutes. Phone networks cap picture messages well below
  the size of a phone photo, so large images are shrunk first.

A file is always identified by *what it is* (source + record id), not
by its path on disk, and is looked up again at the moment of delivery.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import logging
import mimetypes
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import quote, urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, storage
from ..config import Settings, get_settings


logger = logging.getLogger(__name__)


# The agent tool whose results this module reads back.
TOOL_NAME = "send_file"

SOURCE_IDENTITY_DOCUMENT = "identity_document"
SOURCE_DOCUMENT = "document"
SOURCE_VEHICLE_PHOTO = "vehicle_photo"
SOURCE_PERSON_PHOTO = "person_photo"
SOURCE_PET_PHOTO = "pet_photo"
SOURCE_RESIDENCE_PHOTO = "residence_photo"

SOURCES = (
    SOURCE_IDENTITY_DOCUMENT,
    SOURCE_DOCUMENT,
    SOURCE_VEHICLE_PHOTO,
    SOURCE_PERSON_PHOTO,
    SOURCE_PET_PHOTO,
    SOURCE_RESIDENCE_PHOTO,
)

_MB = 1024 * 1024


class FileUnavailable(Exception):
    """The file can't be sent; the message says why, in plain words."""


@dataclass(frozen=True)
class OutboundFile:
    """One stored household file, ready to be sent."""

    source: str
    record_id: int
    side: Optional[str]  # "front" / "back" for identity documents
    label: str  # "Jackson's passport (front)"
    filename: str  # "jackson-passport-front.jpg"
    mime_type: str
    size_bytes: int
    stored_path: str  # relative to FA_STORAGE_ROOT
    # Whose private record this is. ``None`` for things the whole
    # household shares (the family car, a pet's photo).
    subject_person_id: Optional[int] = None

    @property
    def is_image(self) -> bool:
        return self.mime_type.startswith("image/")

    @property
    def is_private(self) -> bool:
        """An ID scan or a document filed under one person."""
        return self.source == SOURCE_IDENTITY_DOCUMENT or (
            self.source == SOURCE_DOCUMENT and self.subject_person_id is not None
        )

    def ref(self) -> Dict[str, Any]:
        """What identifies this file; enough to look it up again."""
        return {"source": self.source, "record_id": self.record_id, "side": self.side}

    def describe(self) -> Dict[str, Any]:
        """Summary kept in the conversation transcript."""
        return {
            **self.ref(),
            "label": self.label,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True)
class PreparedFile:
    """The bytes that actually go out on one channel."""

    filename: str
    mime_type: str
    data: bytes


# ---------------------------------------------------------------------------
# What each channel can carry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChannelRule:
    name: str  # as said to a person: "a text message"
    max_file_bytes: int
    max_total_bytes: int
    max_files: int
    # None = any type. Otherwise the MIME types the channel delivers.
    allowed_types: Optional[frozenset[str]] = None
    # Image types the channel shows as a picture without conversion.
    native_images: frozenset[str] = frozenset({"image/jpeg", "image/png"})
    # Images are re-encoded as JPEG when larger than this (or not native).
    shrink_images_over: Optional[int] = None
    shrink_to_bytes: int = 0
    # Twilio fetches the file from us instead of receiving an upload.
    fetched_by_url: bool = False


_OFFICE_TYPES = frozenset(
    {
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "text/plain",
        "text/csv",
    }
)

CHANNEL_RULES: Dict[str, ChannelRule] = {
    "email": ChannelRule(
        name="email",
        max_file_bytes=20 * _MB,
        max_total_bytes=20 * _MB,  # Gmail refuses messages over 25 MB
        max_files=10,
    ),
    "slack": ChannelRule(
        name="Slack",
        max_file_bytes=50 * _MB,
        max_total_bytes=100 * _MB,
        max_files=10,
    ),
    "telegram": ChannelRule(
        name="Telegram",
        max_file_bytes=45 * _MB,  # Bot API limit is 50 MB
        max_total_bytes=100 * _MB,
        max_files=10,
    ),
    "whatsapp": ChannelRule(
        name="WhatsApp",
        max_file_bytes=15 * _MB,  # Twilio's limit is 16 MB
        max_total_bytes=45 * _MB,
        max_files=5,
        allowed_types=_OFFICE_TYPES | {"image/jpeg", "image/png"},
        shrink_images_over=4 * _MB,  # WhatsApp refuses pictures over 5 MB
        shrink_to_bytes=3 * _MB,
        fetched_by_url=True,
    ),
    "sms": ChannelRule(
        name="a text message",
        max_file_bytes=4 * _MB,
        max_total_bytes=int(4.5 * _MB),  # Twilio's limit is 5 MB per message
        max_files=5,
        allowed_types=frozenset({"image/jpeg", "image/png", "image/gif"}),
        native_images=frozenset({"image/jpeg", "image/png", "image/gif"}),
        shrink_images_over=900 * 1024,  # carriers drop pictures much over 1 MB
        shrink_to_bytes=800 * 1024,
        fetched_by_url=True,
    ),
}

DELIVERABLE_CHANNELS = tuple(CHANNEL_RULES)


def channel_name(channel: Optional[str]) -> str:
    rule = CHANNEL_RULES.get(channel or "")
    return rule.name if rule else (channel or "this conversation")


def _mb(n: int) -> str:
    return f"{n / _MB:.1f} MB"


def _will_convert(file: OutboundFile, rule: ChannelRule) -> bool:
    """Will this image be re-encoded as a smaller JPEG for the channel?"""
    if not file.is_image or rule.shrink_images_over is None:
        return False
    return (
        file.mime_type not in rule.native_images
        or file.size_bytes > rule.shrink_images_over
    )


def needs_conversion(file: OutboundFile, channel: Optional[str]) -> bool:
    """Will ``file`` be re-encoded (shrunk, or changed to JPEG) for ``channel``?"""
    rule = CHANNEL_RULES.get(channel or "")
    return rule is not None and _will_convert(file, rule)


def public_base_url(settings: Optional[Settings] = None) -> Optional[str]:
    """The https address the outside world reaches this server on, if any."""
    settings = settings or get_settings()
    base = (settings.PUBLIC_BASE_URL or "").strip().rstrip("/")
    parsed = urlparse(base)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        return None
    if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local"):
        return None
    return base


def problem_for_channel(
    file: OutboundFile,
    channel: Optional[str],
    *,
    already: Sequence[OutboundFile] = (),
    settings: Optional[Settings] = None,
) -> Optional[str]:
    """Why ``file`` can't go out on ``channel``, or ``None`` if it can.

    ``already`` is what has been approved for the same reply so far, so
    the count and combined-size limits apply to the whole reply.
    """
    rule = CHANNEL_RULES.get(channel or "")
    if rule is None:
        return (
            "Files can't be sent in this conversation. They can be sent "
            "by email, Slack, WhatsApp, text message or Telegram."
        )
    if rule.fetched_by_url and public_base_url(settings) is None:
        return (
            f"Sending files by {rule.name} isn't set up on this server "
            "(it needs a public https address). Email or Slack will work."
        )
    converts = _will_convert(file, rule)
    if rule.allowed_types is not None and not converts:
        if file.mime_type not in rule.allowed_types:
            kind = "A PDF" if file.mime_type == "application/pdf" else "That kind of file"
            return (
                f"{kind} can't be sent in {rule.name}"
                + (" — only pictures can" if channel == "sms" else "")
                + ". Email or Slack can carry it."
            )
    size = min(file.size_bytes, rule.shrink_to_bytes) if converts else file.size_bytes
    if size > rule.max_file_bytes:
        return (
            f"{file.label} is {_mb(file.size_bytes)}, which is too large "
            f"for {rule.name} (limit {_mb(rule.max_file_bytes)})."
        )
    if len(already) >= rule.max_files:
        return f"No more than {rule.max_files} files fit in one {rule.name} reply."
    total = size + sum(
        min(f.size_bytes, rule.shrink_to_bytes) if _will_convert(f, rule) else f.size_bytes
        for f in already
    )
    if total > rule.max_total_bytes:
        return (
            f"Adding {file.label} would take this reply over the "
            f"{_mb(rule.max_total_bytes)} that {rule.name} allows."
        )
    return None


# ---------------------------------------------------------------------------
# Looking a file up
# ---------------------------------------------------------------------------


def _person_name(person: Optional[models.Person]) -> str:
    if person is None:
        return "Someone"
    return person.preferred_name or person.first_name or "Someone"


def _possessive(name: str) -> str:
    return f"{name}'" if name.endswith("s") else f"{name}'s"


def document_type_words(document_type: Optional[str]) -> str:
    """``drivers_license`` -> ``driver's license``."""
    words = (document_type or "document").replace("_", " ").strip().lower()
    return words.replace("drivers license", "driver's license")


def vehicle_title(v: models.Vehicle) -> str:
    return v.nickname or " ".join(str(x) for x in (v.year, v.make, v.model) if x)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "file"


def _build(
    *,
    source: str,
    record_id: int,
    side: Optional[str],
    label: str,
    stored_path: Optional[str],
    missing: str,
    subject_person_id: Optional[int] = None,
    mime_type: Optional[str] = None,
    original_filename: Optional[str] = None,
) -> OutboundFile:
    if not stored_path:
        raise FileUnavailable(missing)
    try:
        path = storage.absolute_path(stored_path)
    except ValueError as exc:
        raise FileUnavailable(f"{label} can't be read from storage.") from exc
    if not path.is_file():
        raise FileUnavailable(
            f"{label} is on record, but its file is missing from storage."
        )
    ext = (
        Path(stored_path).suffix.lower()
        or Path(original_filename or "").suffix.lower()
    )
    mime = (
        (mime_type or "").split(";", 1)[0].strip().lower()
        or mimetypes.guess_type(f"x{ext}")[0]
        or "application/octet-stream"
    )
    if mime == "application/octet-stream" and ext:
        mime = mimetypes.guess_type(f"x{ext}")[0] or mime
    return OutboundFile(
        source=source,
        record_id=int(record_id),
        side=side,
        label=label,
        filename=f"{_slug(label)}{ext}",
        mime_type=mime,
        size_bytes=path.stat().st_size,
        stored_path=stored_path,
        subject_person_id=subject_person_id,
    )


def resolve(
    db: Session,
    *,
    family_id: int,
    source: str,
    record_id: int,
    side: Optional[str] = None,
) -> OutboundFile:
    """Find one stored file belonging to ``family_id``.

    Raises :class:`FileUnavailable` when the record doesn't exist in
    this household, has no file saved, or the file is gone from disk.
    A record in another household is reported exactly like one that
    doesn't exist.
    """
    record_id = int(record_id)
    not_found = FileUnavailable("There is no such record in this household.")

    if source == SOURCE_IDENTITY_DOCUMENT:
        doc = db.get(models.IdentityDocument, record_id)
        owner = db.get(models.Person, doc.person_id) if doc is not None else None
        if doc is None or owner is None or owner.family_id != family_id:
            raise not_found
        side = side if side in ("front", "back") else "front"
        what = f"{_possessive(_person_name(owner))} {document_type_words(doc.document_type)}"
        return _build(
            source=source,
            record_id=record_id,
            side=side,
            label=f"{what} ({side})",
            stored_path=doc.front_image_path if side == "front" else doc.back_image_path,
            missing=(
                f"{what} is on record, but no picture of the {side} has "
                "been saved."
            ),
            subject_person_id=owner.person_id,
        )

    if source == SOURCE_DOCUMENT:
        doc = db.get(models.Document, record_id)
        if doc is None or doc.family_id != family_id:
            raise not_found
        return _build(
            source=source,
            record_id=record_id,
            side=None,
            label=doc.title or doc.original_file_name or "Document",
            stored_path=doc.stored_file_path,
            missing="That document has no file saved.",
            subject_person_id=doc.person_id,
            mime_type=doc.mime_type,
            original_filename=doc.original_file_name,
        )

    if source == SOURCE_VEHICLE_PHOTO:
        vehicle = db.get(models.Vehicle, record_id)
        if vehicle is None or vehicle.family_id != family_id:
            raise not_found
        # "the 2021 BMW 430", but "Jax's Bimmer" for a car with a nickname.
        title = vehicle_title(vehicle)
        named = title if vehicle.nickname else f"the {title}"
        return _build(
            source=source,
            record_id=record_id,
            side=None,
            label=f"Photo of {named}",
            stored_path=vehicle.profile_image_path,
            missing=f"There is no photo saved for {named}.",
        )

    if source == SOURCE_PERSON_PHOTO:
        person = db.get(models.Person, record_id)
        if person is None or person.family_id != family_id:
            raise not_found
        path = person.profile_photo_path
        if not path:
            latest = db.execute(
                select(models.PersonPhoto)
                .where(models.PersonPhoto.person_id == record_id)
                .order_by(models.PersonPhoto.person_photo_id.desc())
                .limit(1)
            ).scalar_one_or_none()
            path = latest.stored_file_path if latest is not None else None
        name = _person_name(person)
        return _build(
            source=source,
            record_id=record_id,
            side=None,
            label=f"Photo of {name}",
            stored_path=path,
            missing=f"There is no photo saved for {name}.",
        )

    if source == SOURCE_PET_PHOTO:
        pet = db.get(models.Pet, record_id)
        if pet is None or pet.family_id != family_id:
            raise not_found
        return _build(
            source=source,
            record_id=record_id,
            side=None,
            label=f"Photo of {pet.pet_name}",
            stored_path=pet.cover_photo_path,
            missing=f"There is no photo saved for {pet.pet_name}.",
        )

    if source == SOURCE_RESIDENCE_PHOTO:
        home = db.get(models.Residence, record_id)
        if home is None or home.family_id != family_id:
            raise not_found
        return _build(
            source=source,
            record_id=record_id,
            side=None,
            label=f"Photo of {home.label}",
            stored_path=home.cover_photo_path,
            missing=f"There is no photo saved for {home.label}.",
        )

    raise FileUnavailable(f"Unknown kind of file: {source!r}.")


# ---------------------------------------------------------------------------
# What the agent approved for a reply
# ---------------------------------------------------------------------------


@dataclass
class ReplyFiles:
    """Files to deliver with one reply, and anything that had to be left out."""

    files: List[OutboundFile] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.files or self.problems)


def _from_refs(
    db: Session, refs: Sequence[Any], *, family_id: int, channel: str
) -> ReplyFiles:
    """Look up each recorded file afresh and check it fits ``channel``."""
    out = ReplyFiles()
    seen: set[tuple] = set()
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        key = (ref.get("source"), ref.get("record_id"), ref.get("side"))
        if key in seen:
            continue
        seen.add(key)
        try:
            file = resolve(
                db,
                family_id=family_id,
                source=str(ref.get("source")),
                record_id=int(ref.get("record_id")),
                side=ref.get("side"),
            )
        except (FileUnavailable, TypeError, ValueError) as exc:
            out.problems.append(str(exc))
            continue
        problem = problem_for_channel(file, channel, already=out.files)
        if problem:
            out.problems.append(problem)
            continue
        out.files.append(file)
    return out


def collect_for_task(
    db: Session, *, task_id: Optional[int], family_id: int, channel: str
) -> ReplyFiles:
    """The files the agent attached during ``task_id``, in the order asked.

    Reads the successful ``send_file`` results recorded for the task —
    each was authorised when the tool ran — and looks every file up
    afresh. Anything that has since disappeared, or no longer fits the
    channel, is reported in ``problems`` instead of silently dropped.
    """
    if task_id is None:
        return ReplyFiles()
    steps = db.execute(
        select(models.AgentStep)
        .where(models.AgentStep.agent_task_id == task_id)
        .where(models.AgentStep.step_type == "tool_result")
        .where(models.AgentStep.tool_name == TOOL_NAME)
        .order_by(models.AgentStep.step_index)
    ).scalars()
    refs: List[Any] = []
    for step in steps:
        output = step.tool_output
        if step.error or not isinstance(output, dict):
            continue
        refs.extend(output.get("attachments") or [])
    return _from_refs(db, refs, family_id=family_id, channel=channel)


def for_resend(
    db: Session, sent: Optional[Sequence[Any]], *, family_id: int, channel: str
) -> ReplyFiles:
    """Files that went with an earlier reply, for sending it again elsewhere.

    ``sent`` is the ``attachments_sent`` list kept in the transcript.
    Used when a reply never reached the phone and is re-sent by email,
    text or Telegram to the same person.
    """
    return _from_refs(db, list(sent or []), family_id=family_id, channel=channel)


def prepare_all(
    files: Sequence[OutboundFile], channel: str
) -> tuple[List[tuple[OutboundFile, PreparedFile]], List[str]]:
    """Read every file for ``channel``; returns the ready ones and failed labels."""
    ready: List[tuple[OutboundFile, PreparedFile]] = []
    failed: List[str] = []
    for file in files:
        try:
            ready.append((file, prepare(file, channel)))
        except FileUnavailable as exc:
            logger.warning(
                "Outbound file %s:%s could not be prepared for %s: %s",
                file.source,
                file.record_id,
                channel,
                exc,
            )
            failed.append(file.label)
    return ready, failed


def signed_urls(
    files: Sequence[OutboundFile], *, family_id: int, channel: str
) -> tuple[List[tuple[OutboundFile, str]], List[str]]:
    """A link for every file; returns the linked ones and failed labels."""
    linked: List[tuple[OutboundFile, str]] = []
    failed: List[str] = []
    for file in files:
        try:
            linked.append((file, signed_url(file, family_id=family_id, channel=channel)))
        except FileUnavailable as exc:
            logger.warning(
                "Outbound file %s:%s could not be linked for %s: %s",
                file.source,
                file.record_id,
                channel,
                exc,
            )
            failed.append(file.label)
    return linked, failed


def failure_note(labels: Sequence[str], problems: Sequence[str] = ()) -> str:
    """One honest line for the user when a promised file didn't go out."""
    parts: List[str] = []
    if labels:
        parts.append(
            "I couldn't attach "
            + ", ".join(labels)
            + " — something went wrong sending it. Ask me again in a moment."
        )
    parts.extend(problems)
    return " ".join(parts)


# ---------------------------------------------------------------------------
# The bytes that go out
# ---------------------------------------------------------------------------


def _to_jpeg(data: bytes, *, target_bytes: int) -> bytes:
    """Re-encode an image as a JPEG no bigger than ``target_bytes``."""
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(data)) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA", "P"):
            rgba = img.convert("RGBA")
            flat = Image.new("RGB", rgba.size, (255, 255, 255))
            flat.paste(rgba, mask=rgba.split()[-1])
            img = flat
        else:
            img = img.convert("RGB")
        best = b""
        # Readable first: keep the picture large and drop quality a
        # little before making it smaller.
        for longest_side in (2400, 2000, 1600, 1280, 1024, 800):
            sized = img.copy()
            sized.thumbnail((longest_side, longest_side))
            for quality in (88, 80, 70):
                buf = io.BytesIO()
                sized.save(buf, format="JPEG", quality=quality, optimize=True)
                best = buf.getvalue()
                if len(best) <= target_bytes:
                    return best
        return best


def prepare(file: OutboundFile, channel: str) -> PreparedFile:
    """Read ``file`` and fit it to ``channel``.

    Most files go out untouched. A picture that is too big for
    WhatsApp or a text message, or in a format they don't show, is
    re-encoded as a smaller JPEG; the stored original is never changed.
    """
    try:
        data = storage.absolute_path(file.stored_path).read_bytes()
    except (OSError, ValueError) as exc:
        raise FileUnavailable(f"{file.label} can't be read from storage.") from exc
    rule = CHANNEL_RULES.get(channel)
    if rule is not None and _will_convert(file, rule):
        try:
            data = _to_jpeg(data, target_bytes=rule.shrink_to_bytes)
        except Exception as exc:  # noqa: BLE001 - any decoder failure
            raise FileUnavailable(
                f"{file.label} couldn't be converted to a picture that "
                f"{rule.name} accepts."
            ) from exc
        return PreparedFile(
            filename=f"{Path(file.filename).stem}.jpg",
            mime_type="image/jpeg",
            data=data,
        )
    return PreparedFile(filename=file.filename, mime_type=file.mime_type, data=data)


# ---------------------------------------------------------------------------
# Links for channels that fetch the file (WhatsApp, text messages)
# ---------------------------------------------------------------------------


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _signing_key() -> bytes:
    secret = (get_settings().SESSION_SECRET_KEY or "").encode("utf-8")
    if not secret:
        raise FileUnavailable(
            "File links can't be signed: SESSION_SECRET_KEY is not set."
        )
    # A key of its own, so a file link can never pass for a login cookie.
    return hmac.new(secret, b"outbound-file-link-v1", hashlib.sha256).digest()


def sign_token(
    file: OutboundFile, *, family_id: int, channel: str, ttl_seconds: Optional[int] = None
) -> str:
    ttl = ttl_seconds if ttl_seconds is not None else get_settings().AI_FILE_LINK_TTL_SECONDS
    payload = json.dumps(
        {
            "f": int(family_id),
            "s": file.source,
            "r": file.record_id,
            "d": file.side,
            "c": channel,
            "e": int(time.time()) + int(ttl),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    sig = hmac.new(_signing_key(), payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(sig)}"


def signed_url(file: OutboundFile, *, family_id: int, channel: str) -> str:
    """A short-lived public link to ``file``, as it will look on ``channel``."""
    base = public_base_url()
    if base is None:
        raise FileUnavailable(
            "This server has no public https address to serve the file from."
        )
    rule = CHANNEL_RULES.get(channel)
    name = file.filename
    if rule is not None and _will_convert(file, rule):
        name = f"{Path(name).stem}.jpg"
    token = sign_token(file, family_id=family_id, channel=channel)
    return f"{base}/api/outbound-files/{token}/{quote(name)}"


def open_token(db: Session, token: str) -> PreparedFile:
    """Return the file a link points at, or raise :class:`FileUnavailable`.

    Refuses anything not signed by this server, anything expired, and
    anything whose record has gone since the link was made.
    """
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        payload = _unb64(payload_b64)
        sig = _unb64(sig_b64)
    except (ValueError, TypeError) as exc:
        raise FileUnavailable("Malformed link.") from exc
    expected = hmac.new(_signing_key(), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, sig):
        raise FileUnavailable("Link signature does not match.")
    try:
        claims = json.loads(payload)
        expires = int(claims["e"])
        family_id = int(claims["f"])
        source = str(claims["s"])
        record_id = int(claims["r"])
        channel = str(claims["c"])
    except (ValueError, KeyError, TypeError) as exc:
        raise FileUnavailable("Malformed link.") from exc
    if expires < time.time():
        raise FileUnavailable("Link has expired.")
    file = resolve(
        db, family_id=family_id, source=source, record_id=record_id, side=claims.get("d")
    )
    return prepare(file, channel)


__all__ = [
    "CHANNEL_RULES",
    "DELIVERABLE_CHANNELS",
    "FileUnavailable",
    "OutboundFile",
    "PreparedFile",
    "ReplyFiles",
    "SOURCES",
    "TOOL_NAME",
    "channel_name",
    "collect_for_task",
    "document_type_words",
    "failure_note",
    "for_resend",
    "needs_conversion",
    "open_token",
    "prepare",
    "prepare_all",
    "problem_for_channel",
    "public_base_url",
    "resolve",
    "sign_token",
    "signed_url",
    "signed_urls",
    "vehicle_title",
]

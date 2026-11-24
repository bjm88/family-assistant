"""Read vCard (``.vcf``) files — the format every chat app uses to share a contact.

Sharing a contact over SMS / MMS, WhatsApp or email attaches a vCard.
This turns one into :class:`Contact` objects and renders them as plain
text the agent can read.

Covers what phones actually emit across vCard 2.1, 3.0 and 4.0: folded
lines, quoted-printable values (2.1), Apple's ``item1.TEL`` grouping,
bare 2.1 type flags (``TEL;CELL:``), and ``tel:`` URIs (4.0). Photos
and anything else this app has no use for are ignored.
"""

from __future__ import annotations

import quopri
import re
from typing import Dict, List, Optional, Tuple

from .base import (
    LABEL_HOME,
    LABEL_MOBILE,
    LABEL_OTHER,
    LABEL_WORK,
    Contact,
    ContactEmail,
    ContactPhone,
)


VCARD_MIME_TYPES = frozenset(
    {"text/vcard", "text/x-vcard", "text/directory", "application/vcard"}
)
VCARD_EXTENSIONS = (".vcf", ".vcard")

_ESCAPE_RE = re.compile(r"\\([nN,;:\\])")
# Phone type flags, most specific first: "CELL,WORK" is a mobile.
_PHONE_TYPES: Tuple[Tuple[str, str], ...] = (
    ("cell", LABEL_MOBILE),
    ("mobile", LABEL_MOBILE),
    ("iphone", LABEL_MOBILE),
    ("work", LABEL_WORK),
    ("main", LABEL_WORK),
    ("home", LABEL_HOME),
)
_EMAIL_TYPES: Tuple[Tuple[str, str], ...] = (
    ("work", LABEL_WORK),
    ("home", LABEL_HOME),
)


def is_vcard(mime_type: Optional[str], filename: Optional[str]) -> bool:
    mime = (mime_type or "").split(";")[0].strip().lower()
    if mime in VCARD_MIME_TYPES:
        return True
    return (filename or "").lower().endswith(VCARD_EXTENSIONS)


def decode_bytes(data: bytes) -> str:
    """Decode a vCard file. 3.0/4.0 are UTF-8; old 2.1 files may not be."""
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeError:
            continue
    return data.decode("latin-1")


def _unfold(text: str) -> List[str]:
    """Join continuation lines back onto the property they belong to."""
    lines: List[str] = []
    soft_break = False
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if soft_break and lines:
            # vCard 2.1 quoted-printable: a trailing "=" continues the value.
            lines[-1] = lines[-1][:-1] + raw
        elif raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
        soft_break = (
            lines[-1].endswith("=") and "QUOTED-PRINTABLE" in lines[-1].upper()
        )
    return [line for line in lines if line.strip()]


def _unescape(value: str) -> str:
    return _ESCAPE_RE.sub(lambda m: "\n" if m.group(1) in "nN" else m.group(1), value)


def _split_components(value: str) -> List[str]:
    """Split a structured value on unescaped semicolons."""
    return [_unescape(part).strip() for part in re.split(r"(?<!\\);", value)]


def _parse_line(line: str) -> Optional[Tuple[str, Dict[str, str], List[str], str]]:
    """Return ``(name, params, type_flags, value)`` for one property line."""
    head, sep, value = line.partition(":")
    if not sep:
        return None
    parts = head.split(";")
    name = parts[0].rsplit(".", 1)[-1].strip().upper()  # drop "item1." groups
    params: Dict[str, str] = {}
    flags: List[str] = []
    for part in parts[1:]:
        key, eq, val = part.partition("=")
        if not eq:
            flags.append(key.strip().lower())  # 2.1 style: TEL;CELL;VOICE
            continue
        key = key.strip().upper()
        if key == "TYPE":
            flags.extend(v.strip().strip('"').lower() for v in val.split(","))
        else:
            params[key] = val.strip().strip('"')
    if params.get("ENCODING", "").upper() == "QUOTED-PRINTABLE":
        charset = params.get("CHARSET", "utf-8")
        try:
            value = quopri.decodestring(value.encode("latin-1")).decode(
                charset, errors="replace"
            )
        except (LookupError, ValueError):
            pass
    return name, params, flags, value


def _label(flags: List[str], table: Tuple[Tuple[str, str], ...]) -> Optional[str]:
    for flag, label in table:
        if flag in flags:
            return label
    meaningful = [f for f in flags if f not in ("pref", "voice", "internet", "text")]
    return LABEL_OTHER if meaningful else None


def _finish(card: Dict[str, object]) -> Optional[Contact]:
    contact = Contact(
        given_name=card.get("given") or None,  # type: ignore[arg-type]
        family_name=card.get("family") or None,  # type: ignore[arg-type]
        display_name=card.get("fn") or None,  # type: ignore[arg-type]
        company=card.get("org") or None,  # type: ignore[arg-type]
        job_title=card.get("title") or None,  # type: ignore[arg-type]
        emails=list(card.get("emails") or []),  # type: ignore[arg-type]
        phones=list(card.get("phones") or []),  # type: ignore[arg-type]
        notes=card.get("note") or None,  # type: ignore[arg-type]
    )
    has_content = (
        contact.given_name
        or contact.family_name
        or contact.display_name
        or contact.company
        or contact.emails
        or contact.phones
    )
    return contact if has_content else None


def parse_vcards(text: str) -> List[Contact]:
    """Parse every ``BEGIN:VCARD … END:VCARD`` block in ``text``."""
    contacts: List[Contact] = []
    card: Optional[Dict[str, object]] = None
    for line in _unfold(text):
        parsed = _parse_line(line)
        if parsed is None:
            continue
        name, _params, flags, value = parsed
        if name == "BEGIN" and value.strip().upper() == "VCARD":
            card = {"emails": [], "phones": []}
            continue
        if card is None:
            continue
        if name == "END" and value.strip().upper() == "VCARD":
            contact = _finish(card)
            if contact is not None:
                contacts.append(contact)
            card = None
        elif name == "FN":
            card["fn"] = _unescape(value).strip()
        elif name == "N":
            parts = _split_components(value) + ["", ""]
            card["family"], card["given"] = parts[0], parts[1]
        elif name == "ORG":
            card["org"] = next((p for p in _split_components(value) if p), "")
        elif name == "TITLE":
            card["title"] = _unescape(value).strip()
        elif name == "NOTE":
            card["note"] = _unescape(value).strip()
        elif name == "EMAIL":
            address = _unescape(value).strip()
            if address.lower().startswith("mailto:"):
                address = address[7:]
            if address:
                card["emails"].append(  # type: ignore[union-attr]
                    ContactEmail(value=address, label=_label(flags, _EMAIL_TYPES))
                )
        elif name == "TEL":
            number = _unescape(value).strip()
            if number.lower().startswith("tel:"):
                number = number[4:]
            if number:
                card["phones"].append(  # type: ignore[union-attr]
                    ContactPhone(value=number, label=_label(flags, _PHONE_TYPES))
                )
    return contacts


def render_contact(contact: Contact) -> str:
    """One contact as labelled lines the agent can lift fields from."""
    lines = [f"Name: {contact.name}"]
    if contact.company:
        lines.append(f"Company: {contact.company}")
    if contact.job_title:
        lines.append(f"Job title: {contact.job_title}")
    for phone in contact.phones:
        lines.append(f"Phone ({phone.label or 'unlabelled'}): {phone.value}")
    for email in contact.emails:
        lines.append(f"Email: {email.value}")
    if contact.notes:
        lines.append(f"Notes: {contact.notes}")
    return "\n".join(lines)


def render_contacts(contacts: List[Contact]) -> str:
    return "\n\n".join(render_contact(c) for c in contacts)

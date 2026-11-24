"""Read an inbound photo / PDF and work out what it is.

One Gemini call per file returns structured JSON: a classification
(identity document, business card, other document, ordinary photo), the
people it is about, a title / category, redacted OCR text and — for
identity documents — the fields that go on an ``identity_documents``
row, or — for business cards — the contact details printed on the card.

Filing the result is :mod:`api.services.attachment_intake`'s job; this
module only reads.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from .. import models
from .tools.handlers.records import DOCUMENT_CATEGORIES, IDENTITY_DOCUMENT_TYPES
from .vision import VisionCallError, gemini_generate

logger = logging.getLogger(__name__)

MAX_OCR_CHARS = 20_000

KIND_IDENTITY = "identity_document"
KIND_DOCUMENT = "document"
KIND_PHOTO = "photo"
# Someone else's business / contact card. Not filed as a household
# document: the details go to the sender's address book instead.
KIND_BUSINESS_CARD = "business_card"


_PROMPT = """\
You are filing a file a family member sent to their household assistant.
Read it carefully (OCR every readable line) and answer with ONE JSON object:

{{
  "kind": "identity_document" | "business_card" | "document" | "photo",
  "confidence": "high" | "low" — low when the file is blurry, cut off, or you can't tell what it is,
  "title": short human title, e.g. "ConEd electric bill — Oct 2026", "Paisley's passport",
  "summary": 1-2 factual sentences: what it is, key dates and amounts,
  "document_category": one of {categories} or null,
  "person_names": full names of the people this is about / addressed to / issued to (not companies), [] if none,
  "ocr_text": all readable text, top to bottom, as plain text,
  "key_facts": up to 8 short "Label: value" strings with the facts someone would look
    this up for (amounts, due / service / visit dates, provider, policy or plan name,
    member / account numbers reduced to last 4), [] if none,
  "identity": null, or for identity documents {{
    "document_type": one of {id_types},
    "side": "front" | "back",
    "holder_full_name": string or null,
    "holder_given_names": first + middle names as printed, or null,
    "holder_surname": string or null,
    "holder_date_of_birth": "YYYY-MM-DD" or null,
    "sex": "M" | "F" | "X" or null,
    "document_number": the document's own number (passport no., license / ID no.,
      PASSID, member no.) exactly as printed, or null,
    "number_legible": true only if every character of document_number is sharp and
      fully visible (not cropped, blurred, covered or glared); otherwise false,
    "issuing_authority": e.g. "U.S. Department of State", "New Jersey Motor Vehicle Commission", or null,
    "country_of_issue": full country name, or null,
    "state_or_region_of_issue": full state / province name, or null,
    "issue_date": "YYYY-MM-DD" or null,
    "expiration_date": "YYYY-MM-DD" or null,
    "nationality": string or null,
    "place_of_birth": string or null,
    "address": the printed address on one line, or null,
    "license_class": string or null,
    "restrictions": string or null,
    "endorsements": string or null,
    "height": string or null,
    "eye_color": string or null,
    "mrz_lines": the machine-readable zone lines (the <<< lines) copied character
      for character, [] if there are none,
    "other_fields": object of any other printed label -> value pairs (not numbers
      that identify the person or the card)
  }},
  "contact": null, or for business cards {{
    "full_name": the person's name as printed, or null,
    "company": string or null,
    "job_title": string or null,
    "emails": every email address printed, [] if none,
    "mobile_phone": the number labelled mobile / cell / M, or null,
    "work_phone": the number labelled office / direct / tel / T / O, or null,
    "other_phone": a number with no label (never a fax number), or null,
    "website": string or null
  }}
}}

Rules:
- "identity_document" = a government or travel ID: passport, driver's license,
  state ID, birth certificate, green card, Global Entry / NEXUS card,
  military ID, airline loyalty card. Insurance cards, bills, statements,
  receipts, forms, letters, report cards, prescriptions = "document".
- "business_card" = a business / contact card, or a photo or screenshot whose
  whole point is one person's contact details (name plus phone or email).
  Copy phone numbers and email addresses character for character; use null
  rather than guessing a digit you can't read, and set "confidence" to "low"
  if any part of a number or address is unclear.
- "photo" = an ordinary picture (people, pets, places, things) with no document in it.
- In "ocr_text" and "summary", replace every SSN, full bank / card account
  number and government ID number with only its last four characters,
  e.g. "•••• 1234". The full number goes ONLY in identity.document_number.
- U.S. passports: the passport number is the 9-character number printed at the top
  right of the photo page (it also starts MRZ line 2). Never use the book's
  control number from the back cover.
- Dates must be ISO YYYY-MM-DD. Use null for anything you can't read.
{family_hint}"""


@dataclass(frozen=True)
class IdentityFields:
    document_type: str
    side: str
    holder_full_name: Optional[str]
    holder_date_of_birth: Optional[date]
    document_number: Optional[str]
    issuing_authority: Optional[str]
    country_of_issue: Optional[str]
    state_or_region_of_issue: Optional[str]
    issue_date: Optional[date]
    expiration_date: Optional[date]
    holder_given_names: Optional[str] = None
    holder_surname: Optional[str] = None
    sex: Optional[str] = None
    nationality: Optional[str] = None
    place_of_birth: Optional[str] = None
    address: Optional[str] = None
    license_class: Optional[str] = None
    restrictions: Optional[str] = None
    endorsements: Optional[str] = None
    height: Optional[str] = None
    eye_color: Optional[str] = None
    other_fields: dict[str, str] = field(default_factory=dict)
    mrz_verified: bool = False
    number_legible: bool = False

    @property
    def number_trusted(self) -> bool:
        """Safe to store without asking: MRZ check digit, or legible + plausible format."""
        if not self.document_number:
            return False
        return self.mrz_verified or (
            self.number_legible and _number_format_ok(self.document_type, self.document_number, self.country_of_issue)
        )


@dataclass(frozen=True)
class ContactFields:
    """What a business card says about the person it belongs to."""

    full_name: Optional[str] = None
    company: Optional[str] = None
    job_title: Optional[str] = None
    emails: list[str] = field(default_factory=list)
    mobile_phone: Optional[str] = None
    work_phone: Optional[str] = None
    other_phone: Optional[str] = None
    website: Optional[str] = None

    @property
    def usable(self) -> bool:
        """Enough to make a contact: someone to name and a way to reach them."""
        reachable = self.emails or self.mobile_phone or self.work_phone or self.other_phone
        return bool((self.full_name or self.company) and reachable)


@dataclass(frozen=True)
class DocumentAnalysis:
    kind: str
    title: str
    summary: str
    document_category: Optional[str]
    person_names: list[str] = field(default_factory=list)
    ocr_text: str = ""
    identity: Optional[IdentityFields] = None
    model: Optional[str] = None
    confident: bool = True
    key_facts: list[str] = field(default_factory=list)
    contact: Optional[ContactFields] = None


class AnalysisError(RuntimeError):
    """The file could not be read or the model answer was unusable."""


def analyze(
    path: Path,
    *,
    mime_type: str,
    family_names: Sequence[str] = (),
) -> DocumentAnalysis:
    """OCR + classify one image or PDF. Raises :class:`AnalysisError`."""
    hint = (
        f"- Household members (use these spellings when they match): {', '.join(family_names)}.\n"
        if family_names
        else ""
    )
    prompt = _PROMPT.format(
        categories=", ".join(DOCUMENT_CATEGORIES),
        id_types=", ".join(IDENTITY_DOCUMENT_TYPES),
        family_hint=hint,
    )
    try:
        raw, model = gemini_generate(
            path, mime_type=mime_type, prompt=prompt, json_output=True
        )
    except VisionCallError as e:
        raise AnalysisError(str(e)) from e
    try:
        data = json.loads(_strip_fences(raw))
    except json.JSONDecodeError as e:
        raise AnalysisError(f"vision model returned invalid JSON: {e}") from e
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        raise AnalysisError("vision model returned a non-object answer")
    return parse_analysis(data, model=model)


def parse_analysis(data: dict[str, Any], *, model: Optional[str] = None) -> DocumentAnalysis:
    """Normalise the model's JSON into a :class:`DocumentAnalysis`."""
    kind = str(data.get("kind") or "").strip().lower()
    identity_raw = data.get("identity")
    identity: Optional[IdentityFields] = None
    if isinstance(identity_raw, dict):
        doc_type = _s(identity_raw.get("document_type"))
        doc_type = doc_type.lower() if doc_type else "other"
        if doc_type not in IDENTITY_DOCUMENT_TYPES:
            doc_type = "other"
        side = (_s(identity_raw.get("side")) or "front").lower()
        sex = (_s(identity_raw.get("sex")) or "").upper()[:1] or None
        other = identity_raw.get("other_fields")
        mrz = parse_mrz(identity_raw.get("mrz_lines"))
        number = _s(identity_raw.get("document_number"))
        if number:
            number = re.sub(r"\s+", "", number)
        identity = IdentityFields(
            document_type=doc_type,
            side="back" if side == "back" else "front",
            holder_full_name=_s(identity_raw.get("holder_full_name")),
            holder_date_of_birth=mrz.get("date_of_birth") or _d(identity_raw.get("holder_date_of_birth")),
            document_number=mrz.get("document_number") or number,
            issuing_authority=_s(identity_raw.get("issuing_authority")),
            country_of_issue=_s(identity_raw.get("country_of_issue")),
            state_or_region_of_issue=_s(identity_raw.get("state_or_region_of_issue")),
            issue_date=_d(identity_raw.get("issue_date")),
            expiration_date=mrz.get("expiration_date") or _d(identity_raw.get("expiration_date")),
            holder_given_names=_s(identity_raw.get("holder_given_names")),
            holder_surname=_s(identity_raw.get("holder_surname")),
            sex=mrz.get("sex") or (sex if sex in ("M", "F", "X") else None),
            nationality=_s(identity_raw.get("nationality")),
            place_of_birth=_s(identity_raw.get("place_of_birth")),
            address=_s(identity_raw.get("address")),
            license_class=_s(identity_raw.get("license_class")),
            restrictions=_s(identity_raw.get("restrictions")),
            endorsements=_s(identity_raw.get("endorsements")),
            height=_s(identity_raw.get("height")),
            eye_color=_s(identity_raw.get("eye_color")),
            other_fields={
                str(k).strip(): str(v).strip()
                for k, v in (other.items() if isinstance(other, dict) else [])
                if _s(k) and _s(v)
            },
            mrz_verified="document_number" in mrz,
            number_legible=identity_raw.get("number_legible") is True,
        )
    contact = _parse_contact(data.get("contact"))
    if kind not in (KIND_IDENTITY, KIND_BUSINESS_CARD, KIND_DOCUMENT, KIND_PHOTO):
        kind = KIND_IDENTITY if identity else KIND_DOCUMENT
    if kind == KIND_IDENTITY and identity is None:
        kind = KIND_DOCUMENT
    if kind == KIND_BUSINESS_CARD:
        identity = None
        if contact is None or not contact.usable:
            # Nothing to put in an address book — treat it as paperwork.
            kind = KIND_DOCUMENT
    if kind != KIND_BUSINESS_CARD:
        contact = None

    category = _s(data.get("document_category"))
    category = category.lower() if category else None
    if category not in DOCUMENT_CATEGORIES:
        category = None
    if kind == KIND_IDENTITY:
        category = "identity_scan"

    names = data.get("person_names") or []
    if isinstance(names, str):
        names = [names]
    person_names = [n.strip() for n in names if isinstance(n, str) and n.strip()]
    if identity and identity.holder_full_name and identity.holder_full_name not in person_names:
        person_names.insert(0, identity.holder_full_name)

    ocr = (_s(data.get("ocr_text")) or "")[:MAX_OCR_CHARS]
    return DocumentAnalysis(
        kind=kind,
        title=(_s(data.get("title")) or "")[:190],
        summary=_s(data.get("summary")) or "",
        document_category=category,
        person_names=person_names,
        ocr_text=ocr,
        identity=identity,
        model=model,
        confident=(_s(data.get("confidence")) or "high").lower() != "low",
        key_facts=[
            f.strip() for f in (data.get("key_facts") or [])
            if isinstance(f, str) and f.strip()
        ][:8],
        contact=contact,
    )


def _parse_contact(raw: Any) -> Optional[ContactFields]:
    if not isinstance(raw, dict):
        return None
    emails = raw.get("emails") or []
    if isinstance(emails, str):
        emails = [emails]
    return ContactFields(
        full_name=_s(raw.get("full_name")),
        company=_s(raw.get("company")),
        job_title=_s(raw.get("job_title")),
        emails=[e.strip() for e in emails if isinstance(e, str) and "@" in e],
        mobile_phone=_s(raw.get("mobile_phone")),
        work_phone=_s(raw.get("work_phone")),
        other_phone=_s(raw.get("other_phone")),
        website=_s(raw.get("website")),
    )


def _number_format_ok(document_type: str, number: str, country: Optional[str]) -> bool:
    n = number.upper()
    if not re.fullmatch(r"[A-Z0-9\-]+", n):
        return False
    us = (country or "United States").lower() in ("united states", "united states of america", "usa", "us")
    if document_type == "passport" and us:
        return bool(re.fullmatch(r"[A-Z0-9]{9}", n))
    if document_type in ("drivers_license", "state_id"):
        return 4 <= len(n) <= 20
    return len(n) >= 4


# ---------------------------------------------------------------------------
# Machine-readable zone (passports, passport cards, green cards, …)
# ---------------------------------------------------------------------------


def _mrz_value(ch: str) -> int:
    if ch.isdigit():
        return int(ch)
    if "A" <= ch <= "Z":
        return ord(ch) - 55
    return 0


def _mrz_ok(field_: str, check: str) -> bool:
    if not check.isdigit():
        return False
    total = sum(_mrz_value(c) * (7, 3, 1)[i % 3] for i, c in enumerate(field_))
    return total % 10 == int(check)


def _mrz_date(yymmdd: str, *, birth: bool) -> Optional[date]:
    if not yymmdd.isdigit():
        return None
    yy, mm, dd = int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:])
    century = 2000
    if birth and yy > date.today().year % 100:
        century = 1900
    try:
        return date(century + yy, mm, dd)
    except ValueError:
        return None


def parse_mrz(lines: Any) -> dict[str, Any]:
    """Fields from a TD3 (passport) or TD1 (ID card) MRZ, check digits verified.

    Only fields whose check digit passes are returned, so a misread
    character never overrides what was printed on the document.
    """
    if not isinstance(lines, list):
        return {}
    rows = [re.sub(r"\s+", "", str(x)).upper() for x in lines if str(x).strip()]
    out: dict[str, Any] = {}
    if len(rows) >= 2 and len(rows[-2]) == 44 and len(rows[-1]) == 44:
        l2 = rows[-1]
        number, dob, exp = (l2[0:9], l2[9]), (l2[13:19], l2[19]), (l2[21:27], l2[27])
        sex = l2[20]
    elif len(rows) >= 3 and all(len(r) == 30 for r in rows[-3:]):
        l1, l2 = rows[-3], rows[-2]
        number, dob, exp = (l1[5:14], l1[14]), (l2[0:6], l2[6]), (l2[8:14], l2[14])
        sex = l2[7]
    else:
        return {}
    if _mrz_ok(*number):
        out["document_number"] = number[0].replace("<", "")
    if _mrz_ok(*dob):
        d = _mrz_date(dob[0], birth=True)
        if d:
            out["date_of_birth"] = d
    if _mrz_ok(*exp):
        d = _mrz_date(exp[0], birth=False)
        if d:
            out["expiration_date"] = d
    if sex in ("M", "F"):
        out["sex"] = sex
    return out


# ---------------------------------------------------------------------------
# Matching names on a document to people in the family
# ---------------------------------------------------------------------------


def match_person(
    people: Iterable[models.Person],
    name: Optional[str],
    *,
    date_of_birth: Optional[date] = None,
) -> Optional[models.Person]:
    """Return the one family member ``name`` refers to, or ``None``.

    IDs print names in many shapes ("MAISANO, BENJAMIN J", "Benjamin John
    Maisano"), so this compares token sets: a last name plus a first or
    preferred name. A lone first name only matches when it is unique in
    the family. A date of birth, when both sides have one, must agree.
    """
    tokens = set(_tokens(name))
    if not tokens:
        return None
    people = list(people)

    def dob_ok(p: models.Person) -> bool:
        return not (date_of_birth and p.date_of_birth and p.date_of_birth != date_of_birth)

    full = [
        p for p in people
        if dob_ok(p)
        and set(_tokens(p.last_name)) <= tokens
        and _given_tokens(p) & tokens
    ]
    if len(full) == 1:
        return full[0]
    if len(full) > 1:
        if date_of_birth:
            exact = [p for p in full if p.date_of_birth == date_of_birth]
            if len(exact) == 1:
                return exact[0]
        return None

    if len(tokens) == 1:
        given = [p for p in people if dob_ok(p) and _given_tokens(p) & tokens]
        if len(given) == 1:
            return given[0]
    if date_of_birth:
        same_dob = [
            p for p in people
            if p.date_of_birth == date_of_birth and set(_tokens(p.last_name)) <= tokens
        ]
        if len(same_dob) == 1:
            return same_dob[0]
    return None


def _given_tokens(p: models.Person) -> set[str]:
    out: set[str] = set()
    for n in (p.first_name, p.preferred_name):
        toks = _tokens(n)
        if toks:
            out.add(toks[0])
    return out


def _tokens(name: Optional[str]) -> list[str]:
    if not name:
        return []
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return [t for t in re.split(r"[^a-z0-9]+", folded.lower()) if t]


# ---------------------------------------------------------------------------
# Small parsing helpers
# ---------------------------------------------------------------------------


def _strip_fences(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    return raw


def _s(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s if s and s.lower() not in ("null", "none", "n/a", "unknown") else None


def _d(v: Any) -> Optional[date]:
    s = _s(v)
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


__all__ = [
    "AnalysisError",
    "ContactFields",
    "DocumentAnalysis",
    "IdentityFields",
    "KIND_BUSINESS_CARD",
    "KIND_DOCUMENT",
    "KIND_IDENTITY",
    "KIND_PHOTO",
    "analyze",
    "match_person",
    "parse_analysis",
    "parse_mrz",
]

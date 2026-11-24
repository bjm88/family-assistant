"""Add / update the rest of the household's structured records.

:mod:`.records` has purpose-built tools for goals, medications, identity
documents and files sent as attachments. This module covers everything
else the admin console can edit — people, relationships, pets,
vehicles, residences, insurance policies, financial accounts, medical
conditions, physicians, jobs, websites, sensitive identifiers, document
details and the family's own settings — through one tool,
``household_record_save``, so the agent's tool catalog grows by a
single schema rather than fourteen.

Each kind is described by a :class:`Kind` that points at the same
Pydantic schemas and the same create / update functions the admin API
uses, so a record saved by Avi is validated, encrypted and linked
exactly as one saved from the console. What this module adds on top:

* **Who may write.** Records that belong to one person follow
  :func:`api.ai.authz.can_edit_person_records` (that person, or a parent
  of the household). Records that belong to the household as a whole —
  pets, vehicles, residences, insurance, financial accounts, family
  settings, relationships, new people — need a household parent.
* **Identity fields.** A person's email address, phone numbers and a
  parent / guardian role decide who Avi recognises on email, SMS and
  Telegram and who can sign in to the site. Those channels can be
  spoofed, so these fields change only from the signed-in web chat of
  an admin (``ctx.is_admin``), never from a message.
* **Same household.** Every id passed in (a driver, a covered vehicle,
  a policy holder) must belong to the speaker's family.

There is no delete here on purpose.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple, Type
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic import BaseModel, ValidationError
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from .... import models, schemas
from ....routers import (
    documents as _documents,
    families as _families,
    financial_accounts as _financial_accounts,
    insurance_policies as _insurance_policies,
    jobs as _jobs,
    medical_conditions as _medical_conditions,
    people as _people,
    person_relationships as _person_relationships,
    person_websites as _person_websites,
    pets as _pets,
    physicians as _physicians,
    residences as _residences,
    sensitive_identifiers as _sensitive_identifiers,
    vehicles as _vehicles,
)
from ... import authz
from .._registry import ToolContext, ToolError
from . import records as _rec


logger = logging.getLogger(__name__)


# Who a record belongs to.
PERSON = "person"  # one family member (row.person_id)
FAMILY = "family"  # the household as a whole (row.family_id)

INSURANCE_POLICY_TYPES = (
    "auto", "home", "renters", "condo", "health", "dental", "vision", "life",
    "disability", "umbrella", "pet", "travel", "other",
)
FINANCIAL_ACCOUNT_TYPES = (
    "checking", "savings", "money_market", "certificate_of_deposit",
    "credit_card", "brokerage", "retirement_401k", "retirement_ira",
    "retirement_roth_ira", "college_529", "loan_auto", "loan_personal",
    "loan_student", "mortgage", "heloc", "other",
)
BILLING_FREQUENCIES = ("monthly", "quarterly", "semi_annual", "annual")
SENSITIVE_IDENTIFIER_TYPES = ("social_security_number", "itin", "foreign_tax_id", "other")
RELATIONSHIP_TYPES = ("parent_of", "spouse_of")

# Decide who Avi recognises and who can sign in — see the module docstring.
PERSON_IDENTITY_FIELDS = (
    "email_address",
    "mobile_phone_number",
    "home_phone_number",
    "work_phone_number",
)
# Set only by their own flows: Telegram linking is verified by a text
# code, and SMS consent has to come from the person's own STOP / JOIN.
PERSON_BLOCKED_FIELDS = {
    "telegram_user_id": "Telegram is linked by the person sharing their contact with Avi.",
    "telegram_username": "Telegram is linked by the person sharing their contact with Avi.",
    "sms_opted_out": "SMS consent changes only when the person texts STOP or JOIN.",
}


@dataclass(frozen=True)
class Kind:
    """One editable record type and how to save it."""

    name: str
    noun: str
    model: Type[Any]
    pk: str
    owner: str
    read_schema: Type[BaseModel]
    create_schema: Optional[Type[BaseModel]] = None
    update_schema: Optional[Type[BaseModel]] = None
    create_fn: Optional[Callable[..., Any]] = None
    update_fn: Optional[Callable[..., Any]] = None
    # Listing these needs the same access as reading medications.
    sensitive: bool = False
    # Encrypted on save and never echoed. True = may be cleared.
    secrets: Mapping[str, bool] = field(default_factory=dict)
    # Fields limited to a fixed vocabulary.
    choices: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)
    # Fields holding ids of other household rows: field -> "person" /
    # "vehicle" / "residence". List-valued fields are checked per item.
    refs: Mapping[str, str] = field(default_factory=dict)
    blocked: Mapping[str, str] = field(default_factory=dict)
    title: Callable[[Any], str] = lambda row: ""
    # Shown to the model when it tries the wrong thing.
    no_create: str = ""
    no_update: str = ""

    @property
    def fields(self) -> Tuple[str, ...]:
        """Every field name the model may pass, in schema order."""
        names: List[str] = []
        for schema in (self.create_schema, self.update_schema):
            if schema is None:
                continue
            for name in schema.model_fields:
                if name in ("family_id",) or name in self.blocked or name in names:
                    continue
                if name == "person_id" and self.owner == PERSON and self.name != "document":
                    continue  # the owner; passed as the tool's person_id
                names.append(name)
        return tuple(names)

    @property
    def required(self) -> Tuple[str, ...]:
        if self.create_schema is None:
            return ()
        return tuple(
            name
            for name, info in self.create_schema.model_fields.items()
            if info.is_required() and name in self.fields
        )


def _vehicle_title(v: models.Vehicle) -> str:
    return v.nickname or " ".join(str(x) for x in (v.year, v.make, v.model) if x)


KINDS: Dict[str, Kind] = {
    k.name: k
    for k in (
        Kind(
            name="person",
            noun="family member",
            model=models.Person,
            pk="person_id",
            owner=PERSON,
            read_schema=schemas.PersonRead,
            create_schema=schemas.PersonCreate,
            update_schema=schemas.PersonUpdate,
            create_fn=_people.create_person,
            update_fn=_people.update_person,
            blocked=PERSON_BLOCKED_FIELDS,
            title=_rec._name,
        ),
        Kind(
            name="relationship",
            noun="family relationship",
            model=models.PersonRelationship,
            pk="person_relationship_id",
            owner=FAMILY,
            read_schema=schemas.PersonRelationshipRead,
            create_schema=schemas.PersonRelationshipCreate,
            create_fn=_person_relationships.create_person_relationship,
            choices={"relationship_type": RELATIONSHIP_TYPES},
            refs={"from_person_id": "person", "to_person_id": "person"},
            title=lambda r: f"{r.from_person_id} {r.relationship_type} {r.to_person_id}",
            no_update=(
                "A relationship can't be edited. Add the correct one; a wrong "
                "one has to be removed in the admin console."
            ),
        ),
        Kind(
            name="pet",
            noun="pet",
            model=models.Pet,
            pk="pet_id",
            owner=FAMILY,
            read_schema=schemas.PetRead,
            create_schema=schemas.PetCreate,
            update_schema=schemas.PetUpdate,
            create_fn=_pets.create_pet,
            update_fn=_pets.update_pet,
            title=lambda p: p.pet_name,
        ),
        Kind(
            name="vehicle",
            noun="vehicle",
            model=models.Vehicle,
            pk="vehicle_id",
            owner=FAMILY,
            read_schema=schemas.VehicleRead,
            create_schema=schemas.VehicleCreate,
            update_schema=schemas.VehicleUpdate,
            create_fn=_vehicles.create_vehicle,
            update_fn=_vehicles.update_vehicle,
            secrets={"vehicle_identification_number": True, "license_plate_number": True},
            refs={"primary_driver_person_id": "person", "residence_id": "residence"},
            title=_vehicle_title,
        ),
        Kind(
            name="residence",
            noun="residence",
            model=models.Residence,
            pk="residence_id",
            owner=FAMILY,
            read_schema=schemas.ResidenceRead,
            create_schema=schemas.ResidenceCreate,
            update_schema=schemas.ResidenceUpdate,
            create_fn=_residences.create_residence,
            update_fn=_residences.update_residence,
            title=lambda r: r.label,
        ),
        Kind(
            name="insurance_policy",
            noun="insurance policy",
            model=models.InsurancePolicy,
            pk="insurance_policy_id",
            owner=FAMILY,
            read_schema=schemas.InsurancePolicyRead,
            create_schema=schemas.InsurancePolicyCreate,
            update_schema=schemas.InsurancePolicyUpdate,
            create_fn=_insurance_policies.create_insurance_policy,
            update_fn=_insurance_policies.update_insurance_policy,
            secrets={"policy_number": False},
            choices={
                "policy_type": INSURANCE_POLICY_TYPES,
                "premium_billing_frequency": BILLING_FREQUENCIES,
            },
            refs={"covered_person_ids": "person", "covered_vehicle_ids": "vehicle"},
            title=lambda p: f"{p.carrier_name} {p.policy_type}",
        ),
        Kind(
            name="financial_account",
            noun="financial account",
            model=models.FinancialAccount,
            pk="financial_account_id",
            owner=FAMILY,
            read_schema=schemas.FinancialAccountRead,
            create_schema=schemas.FinancialAccountCreate,
            update_schema=schemas.FinancialAccountUpdate,
            create_fn=_financial_accounts.create_financial_account,
            update_fn=_financial_accounts.update_financial_account,
            sensitive=True,
            secrets={"account_number": False, "routing_number": True},
            choices={"account_type": FINANCIAL_ACCOUNT_TYPES},
            refs={"primary_holder_person_id": "person"},
            title=lambda a: a.account_nickname or f"{a.institution_name} {a.account_type}",
        ),
        Kind(
            name="medical_condition",
            noun="medical condition",
            model=models.MedicalCondition,
            pk="medical_condition_id",
            owner=PERSON,
            read_schema=schemas.MedicalConditionRead,
            create_schema=schemas.MedicalConditionCreate,
            update_schema=schemas.MedicalConditionUpdate,
            create_fn=_medical_conditions.create_medical_condition,
            update_fn=_medical_conditions.update_medical_condition,
            sensitive=True,
            title=lambda c: c.condition_name,
        ),
        Kind(
            name="physician",
            noun="physician",
            model=models.Physician,
            pk="physician_id",
            owner=PERSON,
            read_schema=schemas.PhysicianRead,
            create_schema=schemas.PhysicianCreate,
            update_schema=schemas.PhysicianUpdate,
            create_fn=_physicians.create_physician,
            update_fn=_physicians.update_physician,
            sensitive=True,
            title=lambda p: p.physician_name,
        ),
        Kind(
            name="job",
            noun="job",
            model=models.Job,
            pk="job_id",
            owner=PERSON,
            read_schema=schemas.JobRead,
            create_schema=schemas.JobCreate,
            update_schema=schemas.JobUpdate,
            create_fn=_jobs.create_job,
            update_fn=_jobs.update_job,
            title=lambda j: j.role_title or j.company_name or "job",
        ),
        Kind(
            name="website",
            noun="website",
            model=models.PersonWebsite,
            pk="person_website_id",
            owner=PERSON,
            read_schema=schemas.PersonWebsiteRead,
            create_schema=schemas.PersonWebsiteCreate,
            update_schema=schemas.PersonWebsiteUpdate,
            create_fn=_person_websites.create_person_website,
            update_fn=_person_websites.update_person_website,
            title=lambda w: w.name,
        ),
        Kind(
            name="sensitive_identifier",
            noun="sensitive identifier",
            model=models.SensitiveIdentifier,
            pk="sensitive_identifier_id",
            owner=PERSON,
            read_schema=schemas.SensitiveIdentifierRead,
            create_schema=schemas.SensitiveIdentifierCreate,
            update_schema=schemas.SensitiveIdentifierUpdate,
            create_fn=_sensitive_identifiers.create_sensitive_identifier,
            update_fn=_sensitive_identifiers.update_sensitive_identifier,
            sensitive=True,
            secrets={"identifier_value": False},
            choices={"identifier_type": SENSITIVE_IDENTIFIER_TYPES},
            title=lambda s: s.identifier_type.replace("_", " "),
        ),
        Kind(
            name="document",
            noun="document",
            model=models.Document,
            pk="document_id",
            owner=PERSON,  # or the household when person_id is NULL
            read_schema=schemas.DocumentRead,
            update_schema=schemas.DocumentUpdate,
            update_fn=_documents.update_document,
            choices={"document_category": _rec.DOCUMENT_CATEGORIES},
            refs={"person_id": "person"},
            title=lambda d: d.title,
            no_create=(
                "A document is created from a file. Use "
                "document_save_from_message when the user sent one; "
                "otherwise ask them to send it."
            ),
        ),
        Kind(
            name="family",
            noun="family settings",
            model=models.Family,
            pk="family_id",
            owner=FAMILY,
            read_schema=schemas.FamilyRead,
            update_schema=schemas.FamilyUpdate,
            update_fn=_families.update_family,
            title=lambda f: f.family_name,
            no_create="There is one family record; update it instead (no record_id needed).",
        ),
    )
}

# Kinds ``household_records_list`` gained from this module.
LISTABLE_KINDS: Tuple[str, ...] = tuple(k for k in KINDS if k not in ("document", "family"))

# Columns that are bulky, internal, or pointless for the model to see.
_HIDDEN_OUTPUT = {
    "created_at", "updated_at", "extracted_text", "profile_photo_path",
    "profile_image_path", "cover_photo_path", "sms_opted_out_at",
}


def field_guide() -> str:
    """One line per kind — its fields, ``*`` marking those a new record needs."""
    lines = []
    for kind in KINDS.values():
        required = set(kind.required)
        names = ", ".join(f"{n}*" if n in required else n for n in kind.fields)
        lines.append(f"- {kind.name}: {names}")
    return "\n".join(lines)


HOUSEHOLD_RECORD_SAVE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(KINDS)},
        "record_id": {
            "type": "integer",
            "description": (
                "The record to update (its id from household_records_list "
                "or sql_query). Omit to create a new record."
            ),
        },
        "person_id": {
            "type": "integer",
            "description": (
                "For a NEW record that belongs to one person (medical_condition, "
                "physician, job, website, sensitive_identifier): whose it is. "
                "Defaults to the speaker."
            ),
        },
        "fields": {
            "type": "object",
            "description": (
                "Only the fields to set, by name. Dates are YYYY-MM-DD. null "
                "clears an optional field. Fields you leave out are untouched."
            ),
        },
    },
    "required": ["kind", "fields"],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_parent_or_admin(ctx: ToolContext) -> bool:
    if ctx.is_admin:
        return True
    if ctx.person_id is None:
        return False
    me = ctx.db.get(models.Person, ctx.person_id)
    return me is not None and me.family_id == ctx.family_id and authz.is_primary_parent(me)


def _kind(name: str) -> Kind:
    kind = KINDS.get((name or "").strip().lower())
    if kind is None:
        raise ToolError(f"kind must be one of {list(KINDS)}.")
    return kind


def _out(kind: Kind, row: Any) -> Dict[str, Any]:
    """The record as the admin API would return it — never ciphertext."""
    if kind.name == "insurance_policy":
        data = _insurance_policies._to_read(row).model_dump(mode="json")
    else:
        data = kind.read_schema.model_validate(row).model_dump(mode="json")
    return {k: v for k, v in data.items() if k not in _HIDDEN_OUTPUT}


def _load(ctx: ToolContext, kind: Kind, record_id: int) -> Tuple[Any, Optional[models.Person]]:
    """Fetch a record of this family and the person it belongs to, if any."""
    row = ctx.db.get(kind.model, int(record_id))
    missing = ToolError(f"No {kind.noun} with id={record_id} in this family.")
    if row is None:
        raise missing
    if kind.name == "person":
        if row.family_id != ctx.family_id:
            raise missing
        return row, row
    if hasattr(row, "family_id"):
        if row.family_id != ctx.family_id:
            raise missing
        person_id = getattr(row, "person_id", None) if kind.owner == PERSON else None
        return row, (ctx.db.get(models.Person, person_id) if person_id else None)
    person = ctx.db.get(models.Person, row.person_id)
    if person is None or person.family_id != ctx.family_id:
        raise missing
    return row, person


def _check_ref(ctx: ToolContext, name: str, target: str, value: Any) -> None:
    ids = value if isinstance(value, list) else [value]
    for raw in ids:
        if raw is None:
            continue
        try:
            ref_id = int(raw)
        except (TypeError, ValueError):
            raise ToolError(f"{name} must be a numeric id.") from None
        model = {"person": models.Person, "vehicle": models.Vehicle, "residence": models.Residence}[target]
        row = ctx.db.get(model, ref_id)
        if row is None or row.family_id != ctx.family_id:
            raise ToolError(f"{name}: no {target} with id={ref_id} in this family.")


def _clean_fields(kind: Kind, raw: Any, *, creating: bool) -> Dict[str, Any]:
    """Validate field names and normalise blanks, choices and secrets."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    if not isinstance(raw, dict) or not raw:
        raise ToolError(
            f"fields must be an object of the {kind.noun} fields to set. "
            f"Available: {', '.join(kind.fields)}."
        )
    for name in raw:
        if name in kind.blocked:
            raise ToolError(f"{name} can't be changed here. {kind.blocked[name]} Nothing was changed.")
    unknown = sorted(set(raw) - set(kind.fields))
    if unknown:
        raise ToolError(
            f"A {kind.noun} has no field named {', '.join(unknown)}. "
            f"Available: {', '.join(kind.fields)}."
        )

    cleaned: Dict[str, Any] = {}
    columns = kind.model.__table__.columns
    for name, value in raw.items():
        if isinstance(value, str):
            value = value.strip()
        blank = value is None or value == ""
        if name in kind.secrets:
            if blank:
                if creating:
                    continue  # same as leaving it out; "required" is checked later
                if not kind.secrets[name]:
                    raise ToolError(f"{name} can't be removed, only replaced.")
                value = ""  # the admin API's "clear this" signal
            cleaned[name] = value
            continue
        if blank:
            if creating:
                continue
            column = columns.get(name)
            if column is not None and not column.nullable:
                raise ToolError(f"{name} can't be blank.")
            cleaned[name] = [] if name in kind.refs and name.endswith("_ids") else None
            continue
        if name in kind.choices:
            choice = str(value).strip().lower().replace(" ", "_").replace("-", "_")
            if choice not in kind.choices[name]:
                raise ToolError(f"{name} must be one of: {', '.join(kind.choices[name])}.")
            value = choice
        cleaned[name] = value
    if not cleaned:
        raise ToolError("No values to save.")
    return cleaned


def _validate(schema: Type[BaseModel], data: Dict[str, Any], kind: Kind) -> BaseModel:
    try:
        return schema(**data)
    except ValidationError as exc:
        # Built from field + message only: the rejected input may be a secret.
        problems = []
        for err in exc.errors(include_input=False, include_url=False):
            where = ".".join(str(p) for p in err.get("loc", ())) or "fields"
            problems.append(f"{where}: {err.get('msg', 'invalid value')}")
        need = f" A new {kind.noun} needs: {', '.join(kind.required)}." if kind.required else ""
        raise ToolError(
            f"Couldn't save the {kind.noun} — " + "; ".join(problems) + "." + need
            + " Nothing was changed."
        ) from None


def _run(ctx: ToolContext, fn: Callable[[], Any], kind: Kind) -> Any:
    """Call an admin-API write inside a savepoint; turn its errors into ToolErrors."""
    try:
        with ctx.db.begin_nested():
            return fn()
    except HTTPException as exc:
        raise ToolError(f"{exc.detail} Nothing was changed.") from None
    except IntegrityError:
        raise ToolError(
            f"That {kind.noun} conflicts with one already on file or points at "
            "a record that doesn't exist. Nothing was changed."
        ) from None


# ---------------------------------------------------------------------------
# Rules that only apply to people
# ---------------------------------------------------------------------------


def _is_authority_role(value: Any) -> bool:
    return str(value or "").strip().lower() in authz.PRIMARY_PARENT_ROLES


def _guard_person_fields(
    ctx: ToolContext, fields: Dict[str, Any], existing: Optional[models.Person]
) -> None:
    """Keep identity and authority changes to a signed-in admin."""
    if ctx.is_admin:
        return
    touched = [f for f in PERSON_IDENTITY_FIELDS if f in fields]
    if touched:
        raise ToolError(
            f"{', '.join(touched)} can't be changed by message: email addresses "
            "and phone numbers decide who I recognise and who can sign in, so "
            "they're changed by an admin in the web app (People page, or the "
            "signed-in chat). Nothing was changed — tell the user that plainly, "
            "and offer to save the other details."
        )
    if "primary_family_relationship" in fields:
        was = existing.primary_family_relationship if existing is not None else None
        if _is_authority_role(fields["primary_family_relationship"]) or _is_authority_role(was):
            raise ToolError(
                "Making someone a parent or guardian of the household — or "
                "changing a parent's role — is done by an admin in the web app, "
                "because parents can change everyone's records. Nothing was changed."
            )


def _check_timezone(fields: Dict[str, Any]) -> None:
    zone = fields.get("timezone")
    if zone is None:
        return
    try:
        ZoneInfo(str(zone))
    except Exception:  # noqa: BLE001 - ZoneInfo raises several kinds
        raise ToolError(
            f"'{zone}' isn't a timezone I recognise. Use an IANA name such as "
            "America/New_York."
        ) from None


# ---------------------------------------------------------------------------
# household_record_save
# ---------------------------------------------------------------------------


async def handle_household_record_save(
    ctx: ToolContext,
    kind: str,
    fields: Any = None,
    record_id: Optional[int] = None,
    person_id: Optional[int] = None,
) -> Dict[str, Any]:
    spec = _kind(kind)
    db = ctx.db

    if spec.name == "family" and record_id is None:
        record_id = ctx.family_id
    creating = record_id is None
    if creating and spec.create_fn is None:
        raise ToolError(spec.no_create)
    if not creating and spec.update_fn is None:
        raise ToolError(spec.no_update)

    # A person-owned record's owner may arrive inside ``fields``.
    if isinstance(fields, dict) and spec.owner == PERSON and spec.name not in ("person", "document"):
        if "person_id" in fields:
            fields = dict(fields)
            moved = fields.pop("person_id")
            if not creating:
                raise ToolError(f"A {spec.noun} can't be moved to another person.")
            person_id = person_id if person_id is not None else moved

    data = _clean_fields(spec, fields, creating=creating)
    if creating and spec.name == "vehicle":
        data.setdefault("vehicle_type", "car")  # NOT NULL; the console always sends it

    # ---- who is this for, and may the speaker write it? ----------------
    row: Any = None
    subject: Optional[models.Person] = None
    if not creating:
        if spec.name == "family":
            if int(record_id) != ctx.family_id:
                raise ToolError("You can only change this family's own settings.")
            row = db.get(models.Family, ctx.family_id)
        else:
            row, subject = _load(ctx, spec, int(record_id))
    elif spec.owner == PERSON and spec.name != "person":
        subject = _rec._subject_for_new(ctx, person_id)
    # New people, and anything owned by the household, need a parent.
    _rec._authorize(ctx, subject)

    if spec.name == "person":
        _guard_person_fields(ctx, data, row)
    if spec.name == "relationship" and not _is_parent_or_admin(ctx):
        raise ToolError("Only a parent of the household can change family relationships.")
    if spec.name == "family":
        _check_timezone(data)
    if spec.name == "document" and data.get("person_id") is not None:
        # Filing a document under someone else needs rights over them too.
        _rec._authorize(ctx, _rec._family_person(ctx, int(data["person_id"])))

    for name, target in spec.refs.items():
        if name in data:
            _check_ref(ctx, name, target, data[name])

    # ---- save through the admin API's own code -------------------------
    if creating:
        owner_key = (
            {"person_id": subject.person_id}
            if spec.owner == PERSON and spec.name != "person"
            else {}
            if spec.name == "relationship"
            else {"family_id": ctx.family_id}
        )
        payload = _validate(spec.create_schema, {**data, **owner_key}, spec)
        if spec.name == "relationship":
            _reject_duplicate_relationship(ctx, payload)
        result = _run(ctx, lambda: spec.create_fn(payload, db=db), spec)
        if spec.name == "relationship":
            row = result[0]  # a spouse edge also creates its mirror
        elif spec.name == "insurance_policy":
            row = db.get(models.InsurancePolicy, result.insurance_policy_id)
        else:
            row = result
    else:
        payload = _validate(spec.update_schema, data, spec)
        _run(ctx, lambda: spec.update_fn(int(record_id), payload, db=db), spec)
        db.refresh(row)

    new_id = getattr(row, spec.pk)
    action = "created" if creating else "updated"
    _rec._log_write(ctx, action, spec.name, new_id, subject)
    out: Dict[str, Any] = {
        "action": action,
        "kind": spec.name,
        "record_id": new_id,
        "saved_fields": sorted(data),
        "record": _out(spec, row),
    }
    if subject is not None and spec.name != "person":
        out["person"] = _rec._name(subject)
    return out


def _reject_duplicate_relationship(ctx: ToolContext, payload: Any) -> None:
    PR = models.PersonRelationship
    existing = ctx.db.execute(
        select(PR.person_relationship_id)
        .where(PR.from_person_id == payload.from_person_id)
        .where(PR.to_person_id == payload.to_person_id)
        .where(PR.relationship_type == payload.relationship_type)
    ).first()
    if existing is not None:
        raise ToolError("That relationship is already on file. Nothing was changed.")


# ---------------------------------------------------------------------------
# Listing (called by household_records_list for these kinds)
# ---------------------------------------------------------------------------


def list_records(ctx: ToolContext, kind_name: str, person_id: Optional[int]) -> Dict[str, Any]:
    """Records of one kind with their ids, so the model can update them."""
    spec = _kind(kind_name)
    db = ctx.db
    if ctx.person_id is None and not ctx.is_admin:
        raise ToolError("I can only look up household records when I know which family member is asking.")

    if spec.owner == FAMILY or spec.name == "person":
        if spec.sensitive:
            _rec._authorize(ctx, None)
        if spec.name == "relationship":
            PR = models.PersonRelationship
            stmt = (
                select(PR)
                .join(models.Person, models.Person.person_id == PR.from_person_id)
                .where(models.Person.family_id == ctx.family_id)
            )
            if person_id is not None:
                stmt = stmt.where(or_(PR.from_person_id == person_id, PR.to_person_id == person_id))
        else:
            stmt = select(spec.model).where(spec.model.family_id == ctx.family_id)
        rows = db.execute(stmt.order_by(getattr(spec.model, spec.pk)).limit(200)).scalars().all()
        items = [_out(spec, r) for r in rows]
        if spec.name == "person":
            # Private notes follow the read matrix; ids and names don't.
            for item in items:
                item.pop("notes", None)
        return {"kind": spec.name, "items": items}

    person = _rec._subject_for_new(ctx, person_id)
    if spec.sensitive:
        read = authz.can_access_sensitive(
            db,
            requestor_person_id=ctx.person_id,
            subject_person_id=person.person_id,
            requestor_is_admin=ctx.is_admin,
        )
        if not read.allowed:
            _rec._authorize(ctx, person)
    rows = db.execute(
        select(spec.model)
        .where(spec.model.person_id == person.person_id)
        .order_by(getattr(spec.model, spec.pk))
    ).scalars().all()
    return {
        "person": _rec._name(person),
        "person_id": person.person_id,
        "kind": spec.name,
        "items": [_out(spec, r) for r in rows],
    }


__all__ = [
    "HOUSEHOLD_RECORD_SAVE_SCHEMA",
    "KINDS",
    "LISTABLE_KINDS",
    "field_guide",
    "handle_household_record_save",
    "list_records",
]

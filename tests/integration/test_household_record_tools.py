"""Avi can add and update every kind of household record.

``household_record_save`` covers what the purpose-built tools in
``test_record_tools.py`` don't: people, relationships, pets, vehicles,
residences, insurance, financial accounts, medical conditions,
physicians, jobs, websites, sensitive identifiers, document details and
family settings. The handlers are called the way the agent loop calls
them (a real :class:`ToolContext`, no LLM).

Three things are pinned here beyond "it saves":

* **coverage** — every editable resource in the admin API is reachable
  from a tool, and the schema crib sheet Avi is given names only real
  tables and columns;
* **permission** — household-wide records need a parent, personal
  records the person or a parent, and identity fields a signed-in admin;
* **secrets** — account / policy / VIN / SSN values are encrypted and
  never come back in a tool result or an error message.
"""

from __future__ import annotations

import re
import uuid

import pytest
from sqlalchemy import select

from api import crypto, models, schemas
from api.ai import schema_catalog, tools
from api.ai.tools import ToolContext, ToolError
from api.ai.tools.handlers import household, records, sql
from api.db import Base
from api.services import inbound_prompts


save = household.handle_household_record_save
list_records = records.handle_household_records_list


@pytest.fixture
def household_people(db, test_family):
    """A parent, a second parent, a child and a non-parent relative."""
    fid = test_family["family_id"]
    tag = uuid.uuid4().hex[:6]
    people = {
        role: models.Person(
            family_id=fid, first_name=f"HR{role.title()}{tag}", last_name="Test",
            primary_family_relationship=relationship,
        )
        for role, relationship in (
            ("parent", "parent"), ("spouse", "parent"), ("child", "child"), ("aunt", "aunt"),
        )
    }
    db.add_all(people.values())
    db.commit()
    yield people

    db.rollback()
    for model, column in (
        (models.InsurancePolicy, models.InsurancePolicy.carrier_name),
        (models.FinancialAccount, models.FinancialAccount.institution_name),
        (models.Vehicle, models.Vehicle.make),
        (models.Residence, models.Residence.label),
        (models.Pet, models.Pet.pet_name),
        (models.Document, models.Document.title),
        (models.Person, models.Person.first_name),
    ):
        for row in db.execute(
            select(model).where(model.family_id == fid).where(column.like("HR%"))
        ).scalars():
            db.delete(row)
        db.flush()
    db.commit()


@pytest.fixture
def other_family(db):
    """A second household, to prove ids can't cross families."""
    family = models.Family(family_name=f"HROther{uuid.uuid4().hex[:6]}")
    db.add(family)
    db.flush()
    person = models.Person(family_id=family.family_id, first_name="Stranger", last_name="Test")
    vehicle = models.Vehicle(family_id=family.family_id, make="Other", model="Car", vehicle_type="car")
    pet = models.Pet(family_id=family.family_id, pet_name="NotYours", animal_type="dog")
    db.add_all([person, vehicle, pet])
    db.commit()
    yield {"family": family, "person": person, "vehicle": vehicle, "pet": pet}
    db.rollback()
    db.delete(db.get(models.Family, family.family_id))
    db.commit()


def _ctx(db, test_family, person, **kw) -> ToolContext:
    return ToolContext(
        db=db,
        family_id=test_family["family_id"],
        assistant_id=test_family["assistant_id"],
        person_id=person.person_id if person is not None else None,
        **kw,
    )


# ---------------------------------------------------------------------------
# Coverage: Avi knows about, and can edit, everything the console can
# ---------------------------------------------------------------------------


# Editable through a tool of their own rather than household_record_save.
_HAS_OWN_TOOL = {
    "GoalUpdate": "goal_save",
    "MedicationUpdate": "medication_save",
    "IdentityDocumentUpdate": "identity_document_save",
    "TaskUpdate": "task_update",
    "TaskScheduleUpdate": "task_set_schedule",
    "PersonPhotoUpdate": "person_photo_save_from_message",
}
# Not household data Avi should rewrite from a chat message.
_DELIBERATELY_NOT_EDITABLE = {
    "AssistantUpdate",  # Avi's own persona / voice — an admin setting
    "PetPhotoUpdate",  # photo files, managed in the console
    "ResidencePhotoUpdate",
    "AddressUpdate",  # legacy table with no API; residences carry the address
}


def test_every_editable_resource_has_a_tool():
    """Adding an editable resource without teaching Avi about it fails here."""
    update_schemas = {
        name for name in dir(schemas)
        if name.endswith("Update") and isinstance(getattr(schemas, name), type)
    }
    handled = {
        kind.update_schema.__name__ for kind in household.KINDS.values() if kind.update_schema
    }
    unaccounted = update_schemas - handled - set(_HAS_OWN_TOOL) - _DELIBERATELY_NOT_EDITABLE
    assert not unaccounted, f"no agent tool can edit: {sorted(unaccounted)}"

    registered = set(tools.build_default_registry()._tools)
    assert set(_HAS_OWN_TOOL.values()) <= registered
    assert {"household_record_save", "household_records_list"} <= registered


def test_tool_description_names_every_kind_and_field():
    registry = tools.build_default_registry()
    tool = registry.get("household_record_save")
    assert tool.parameters["properties"]["kind"]["enum"] == list(household.KINDS)
    for kind in household.KINDS.values():
        line = next(
            ln for ln in tool.description.splitlines() if ln.startswith(f"- {kind.name}:")
        )
        offered = {name.rstrip("*") for name in line.split(":", 1)[1].replace(" ", "").split(",")}
        schema = kind.update_schema or kind.create_schema
        expected = set(schema.model_fields) - set(kind.blocked) - {"family_id"}
        if kind.owner == household.PERSON and kind.name != "document":
            expected.discard("person_id")
        assert expected <= offered, f"{kind.name} is missing {sorted(expected - offered)}"
    # Fields that would hand over an account are never offered.
    person_line = next(ln for ln in tool.description.splitlines() if ln.startswith("- person:"))
    assert "telegram_user_id" not in person_line and "sms_opted_out" not in person_line

    listing = registry.get("household_records_list").parameters["properties"]["kind"]["enum"]
    assert set(household.LISTABLE_KINDS) <= set(listing)
    assert {"goal", "medication", "identity_document", "document"} <= set(listing)


def test_schema_crib_sheet_names_only_real_tables_and_columns():
    """The hand-written hint Avi gets on SMS / email / Telegram had drifted."""
    hint = inbound_prompts._COMPACT_SCHEMA_HINT
    tables = Base.metadata.tables
    entries = re.findall(r"- `(\w+)` \((\w+)\): (.*?)\.\n", hint, flags=re.S)
    assert len(entries) >= 18
    for table, pk, columns in entries:
        assert table in tables, f"{table} is not a table"
        assert table in schema_catalog.ALLOWED_TABLES, f"{table} can't be queried"
        real = set(tables[table].columns.keys())
        named = {pk}
        columns = re.sub(r"\(.*?\)", "", columns, flags=re.S)
        # {mobile,home,work}_phone_number -> three column names
        columns = re.sub(
            r"\{([\w,]+)\}(_\w+)",
            lambda m: ", ".join(prefix + m.group(2) for prefix in m.group(1).split(",")),
            columns,
        )
        for part in re.split(r",\s*", columns):
            part = part.strip().strip("`")
            if re.fullmatch(r"\w+", part):
                named.add(part)
        assert named <= real, f"{table}: no such column(s) {sorted(named - real)}"

    described = {table for table, _pk, _cols in entries}
    for kind in household.KINDS.values():
        assert kind.model.__tablename__ in described, f"{kind.name} isn't described to Avi"


async def test_websites_are_readable_by_sql(db, test_family, household_people):
    parent = household_people["parent"]
    await save(
        _ctx(db, test_family, parent), kind="website",
        fields={"name": "LinkedIn", "url": "https://linkedin.com/in/hr-test"},
    )
    out = await sql.handle_sql_query(
        _ctx(db, test_family, parent),
        f"SELECT name, url FROM person_websites WHERE person_id = {parent.person_id}",
    )
    assert "linkedin.com/in/hr-test" in str(out)


# ---------------------------------------------------------------------------
# Household-wide records: a parent can, others can't
# ---------------------------------------------------------------------------


async def test_parent_adds_and_updates_a_pet(db, test_family, household_people):
    ctx = _ctx(db, test_family, household_people["parent"])
    out = await save(
        ctx, kind="pet",
        fields={"pet_name": "HRBiscuit", "animal_type": "dog", "breed": "Golden Retriever",
                "date_of_birth": "2026-03-03"},
    )
    assert out["action"] == "created" and out["kind"] == "pet"
    pet = db.get(models.Pet, out["record_id"])
    assert pet.family_id == test_family["family_id"]
    assert pet.date_of_birth.isoformat() == "2026-03-03"

    out = await save(ctx, kind="pet", record_id=pet.pet_id, fields={"color": "golden", "breed": None})
    assert out["action"] == "updated" and out["saved_fields"] == ["breed", "color"]
    db.refresh(pet)
    assert pet.color == "golden" and pet.breed is None
    assert pet.pet_name == "HRBiscuit"  # untouched fields stay

    listed = await list_records(ctx, kind="pet")
    assert pet.pet_id in {item["pet_id"] for item in listed["items"]}


@pytest.mark.parametrize("who", ["child", "aunt", None])
async def test_only_a_parent_changes_household_wide_records(db, test_family, household_people, who):
    speaker = household_people[who] if who else None
    ctx = _ctx(db, test_family, speaker)
    for kind, fields in (
        ("pet", {"pet_name": "HRNope", "animal_type": "cat"}),
        ("vehicle", {"make": "HRNope", "model": "X"}),
        ("residence", {"label": "HRNope", "street_line_1": "1 A St", "city": "Town"}),
        ("insurance_policy", {"policy_type": "auto", "carrier_name": "HRNope", "policy_number": "P1"}),
        ("financial_account", {"account_type": "checking", "institution_name": "HRNope", "account_number": "1"}),
        ("person", {"first_name": "HRNope", "last_name": "Test"}),
        ("family", {"family_name": "Hijacked"}),
    ):
        with pytest.raises(ToolError, match="Nothing was changed"):
            await save(ctx, kind=kind, fields=fields)
    db.expire_all()
    assert db.execute(
        select(models.Pet).where(models.Pet.pet_name == "HRNope")
    ).first() is None
    assert db.get(models.Family, test_family["family_id"]).family_name != "Hijacked"


async def test_vehicle_secrets_are_encrypted_and_never_echoed(db, test_family, household_people):
    parent, child = household_people["parent"], household_people["child"]
    ctx = _ctx(db, test_family, parent)
    out = await save(
        ctx, kind="vehicle",
        fields={
            "make": "HRHonda", "model": "Odyssey", "year": 2022, "current_mileage": 41000,
            "vehicle_identification_number": "5FNRL6H78NB012345",
            "license_plate_number": "8ABC123", "license_plate_state_or_region": "CA",
            "primary_driver_person_id": parent.person_id,
        },
    )
    vehicle = db.get(models.Vehicle, out["record_id"])
    assert vehicle.vehicle_type == "car"  # sensible default for a required column
    assert crypto.decrypt_str(vehicle.vehicle_identification_number_encrypted) == "5FNRL6H78NB012345"
    assert out["record"]["vehicle_identification_number_last_four"] == "2345"
    assert "5FNRL6H78NB012345" not in str(out) and "8ABC123" not in str(out)

    # "The Odyssey has 48,200 miles now."
    out = await save(ctx, kind="vehicle", record_id=vehicle.vehicle_id, fields={"current_mileage": 48200})
    db.refresh(vehicle)
    assert vehicle.current_mileage == 48200
    assert vehicle.license_plate_number_last_four == "C123"  # not disturbed

    # Sold the plate: null clears an optional secret.
    await save(ctx, kind="vehicle", record_id=vehicle.vehicle_id, fields={"license_plate_number": None})
    db.refresh(vehicle)
    assert vehicle.license_plate_number_encrypted is None
    assert vehicle.license_plate_number_last_four is None

    with pytest.raises(ToolError, match="make can't be blank"):
        await save(ctx, kind="vehicle", record_id=vehicle.vehicle_id, fields={"make": ""})
    await save(
        ctx, kind="vehicle", record_id=vehicle.vehicle_id,
        fields={"primary_driver_person_id": child.person_id},
    )
    db.refresh(vehicle)
    assert vehicle.primary_driver_person_id == child.person_id


async def test_marking_a_residence_primary_unsets_the_old_one(db, test_family, household_people):
    ctx = _ctx(db, test_family, household_people["parent"])
    first = await save(
        ctx, kind="residence",
        fields={"label": "HRMain house", "street_line_1": "1 Elm St", "city": "Springfield",
                "state_or_region": "IL", "is_primary_residence": True},
    )
    second = await save(
        ctx, kind="residence",
        fields={"label": "HRLake cabin", "street_line_1": "9 Shore Rd", "city": "Lakeside"},
    )
    assert second["record"]["country"] == "United States"
    await save(ctx, kind="residence", record_id=second["record_id"], fields={"is_primary_residence": True})
    db.expire_all()
    assert db.get(models.Residence, first["record_id"]).is_primary_residence is False
    assert db.get(models.Residence, second["record_id"]).is_primary_residence is True


async def test_insurance_policy_with_covered_people_and_vehicles(db, test_family, household_people):
    parent, child = household_people["parent"], household_people["child"]
    ctx = _ctx(db, test_family, parent)
    car = await save(ctx, kind="vehicle", fields={"make": "HRToyota", "model": "Sienna"})
    out = await save(
        ctx, kind="insurance_policy",
        fields={
            "policy_type": "Auto", "carrier_name": "HRState Farm", "policy_number": "SF-99887766",
            "premium_amount_usd": 1480, "premium_billing_frequency": "annual",
            "expiration_date": "2027-06-01",
            "covered_person_ids": [parent.person_id], "covered_vehicle_ids": [car["record_id"]],
        },
    )
    policy = db.get(models.InsurancePolicy, out["record_id"])
    assert policy.policy_type == "auto"
    assert crypto.decrypt_str(policy.policy_number_encrypted) == "SF-99887766"
    assert out["record"]["policy_number_last_four"] == "7766" and "SF-99887766" not in str(out)
    assert out["record"]["covered_person_ids"] == [parent.person_id]
    assert out["record"]["covered_vehicle_ids"] == [car["record_id"]]

    # "It renews on 2028-06-01 and now covers Jackson too."
    out = await save(
        ctx, kind="insurance_policy", record_id=policy.insurance_policy_id,
        fields={"expiration_date": "2028-06-01",
                "covered_person_ids": [parent.person_id, child.person_id]},
    )
    assert sorted(out["record"]["covered_person_ids"]) == sorted([parent.person_id, child.person_id])
    assert out["record"]["covered_vehicle_ids"] == [car["record_id"]]  # left alone
    assert out["record"]["expiration_date"] == "2028-06-01"

    with pytest.raises(ToolError, match="policy_type must be one of: auto, home"):
        await save(ctx, kind="insurance_policy", record_id=policy.insurance_policy_id,
                   fields={"policy_type": "spaceship"})
    with pytest.raises(ToolError, match="can't be removed, only replaced"):
        await save(ctx, kind="insurance_policy", record_id=policy.insurance_policy_id,
                   fields={"policy_number": ""})
    db.refresh(policy)
    assert crypto.decrypt_str(policy.policy_number_encrypted) == "SF-99887766"


async def test_financial_account_numbers_stay_secret(db, test_family, household_people):
    parent, aunt = household_people["parent"], household_people["aunt"]
    ctx = _ctx(db, test_family, parent)
    out = await save(
        ctx, kind="financial_account",
        fields={"account_type": "checking", "institution_name": "HRChase",
                "account_nickname": "Joint checking", "account_number": "000123456789",
                "routing_number": "021000021", "primary_holder_person_id": parent.person_id},
    )
    account = db.get(models.FinancialAccount, out["record_id"])
    assert crypto.decrypt_str(account.account_number_encrypted) == "000123456789"
    assert crypto.decrypt_str(account.routing_number_encrypted) == "021000021"
    assert out["record"]["account_number_last_four"] == "6789"
    assert "000123456789" not in str(out) and "021000021" not in str(out)

    await save(ctx, kind="financial_account", record_id=account.financial_account_id,
               fields={"current_balance_usd": "5230.18"})
    db.refresh(account)
    assert float(account.current_balance_usd) == 5230.18

    listed = await list_records(ctx, kind="financial_account")
    assert "000123456789" not in str(listed)
    with pytest.raises(ToolError, match="Only a parent"):
        await list_records(_ctx(db, test_family, aunt), kind="financial_account")


# ---------------------------------------------------------------------------
# Records that belong to one person: them, or a parent
# ---------------------------------------------------------------------------


async def test_member_manages_their_own_personal_records(db, test_family, household_people):
    aunt = household_people["aunt"]
    ctx = _ctx(db, test_family, aunt)
    job = await save(ctx, kind="job", fields={"company_name": "Acme", "role_title": "Senior Engineer"})
    site = await save(ctx, kind="website", fields={"name": "GitHub", "url": "https://github.com/hr-aunt"})
    condition = await save(
        ctx, kind="medical_condition",
        fields={"condition_name": "Seasonal allergies", "start_date": "2020-04-01"},
    )
    doctor = await save(
        ctx, kind="physician",
        fields={"physician_name": "Dr. Sarah Patel", "specialty": "Allergist", "phone_number": "555-0134"},
    )
    for out in (job, site, condition, doctor):
        assert out["action"] == "created" and out["person"] == aunt.first_name
    assert db.get(models.Job, job["record_id"]).person_id == aunt.person_id

    await save(ctx, kind="job", record_id=job["record_id"], fields={"role_title": "Staff Engineer"})
    assert db.get(models.Job, job["record_id"]).role_title == "Staff Engineer"

    listed = await list_records(ctx, kind="physician")
    assert [p["physician_name"] for p in listed["items"]] == ["Dr. Sarah Patel"]


async def test_parent_can_and_others_cannot_edit_someone_elses(db, test_family, household_people):
    parent, child, aunt = (household_people[k] for k in ("parent", "child", "aunt"))
    out = await save(
        _ctx(db, test_family, parent), kind="physician", person_id=child.person_id,
        fields={"physician_name": "Dr. Kim", "specialty": "Pediatrics"},
    )
    assert out["person"] == child.first_name
    physician_id = out["record_id"]

    with pytest.raises(ToolError, match="Only a parent of the household or"):
        await save(_ctx(db, test_family, aunt), kind="physician", record_id=physician_id,
                   fields={"phone_number": "555-9999"})
    with pytest.raises(ToolError, match="Only a parent of the household or"):
        await save(_ctx(db, test_family, aunt), kind="medical_condition", person_id=child.person_id,
                   fields={"condition_name": "Made up"})
    # person_id tucked inside fields is honoured for a new record, refused as a move.
    await save(_ctx(db, test_family, parent), kind="job",
               fields={"person_id": child.person_id, "company_name": "Lemonade stand"})
    with pytest.raises(ToolError, match="can't be moved to another person"):
        await save(_ctx(db, test_family, parent), kind="physician", record_id=physician_id,
                   fields={"person_id": aunt.person_id})
    db.expire_all()
    assert db.get(models.Physician, physician_id).phone_number is None
    assert db.get(models.Physician, physician_id).person_id == child.person_id

    with pytest.raises(ToolError, match="Only a parent"):
        await list_records(_ctx(db, test_family, aunt), kind="medical_condition", person_id=child.person_id)


async def test_sensitive_identifier_is_encrypted_and_not_echoed(db, test_family, household_people):
    child = household_people["child"]
    ctx = _ctx(db, test_family, household_people["parent"])
    out = await save(
        ctx, kind="sensitive_identifier", person_id=child.person_id,
        fields={"identifier_type": "Social Security Number", "identifier_value": "123-45-6789"},
    )
    row = db.get(models.SensitiveIdentifier, out["record_id"])
    assert row.identifier_type == "social_security_number"
    assert crypto.decrypt_str(row.identifier_value_encrypted) == "123-45-6789"
    assert out["record"]["identifier_last_four"] == "6789" and "123-45-6789" not in str(out)

    with pytest.raises(ToolError, match="can't be removed, only replaced"):
        await save(ctx, kind="sensitive_identifier", record_id=row.sensitive_identifier_id,
                   fields={"identifier_value": ""})
    await save(ctx, kind="sensitive_identifier", record_id=row.sensitive_identifier_id,
               fields={"notes": "card is in the safe"})
    db.refresh(row)
    assert crypto.decrypt_str(row.identifier_value_encrypted) == "123-45-6789"
    listed = await list_records(ctx, kind="sensitive_identifier", person_id=child.person_id)
    assert listed["items"][0]["identifier_last_four"] == "6789"
    assert "123-45-6789" not in str(listed)


# ---------------------------------------------------------------------------
# People: profile yes, identity and authority only for a signed-in admin
# ---------------------------------------------------------------------------


async def test_member_updates_their_own_profile(db, test_family, household_people):
    aunt = household_people["aunt"]
    out = await save(
        _ctx(db, test_family, aunt), kind="person", record_id=aunt.person_id,
        fields={"preferred_name": "Auntie", "interests_and_activities": "Sailing, pottery",
                "date_of_birth": "1975-08-14"},
    )
    assert out["action"] == "updated"
    db.refresh(aunt)
    assert aunt.preferred_name == "Auntie" and aunt.date_of_birth.isoformat() == "1975-08-14"

    with pytest.raises(ToolError, match="Only a parent of the household or"):
        await save(_ctx(db, test_family, aunt), kind="person",
                   record_id=household_people["child"].person_id, fields={"preferred_name": "Kiddo"})


async def test_parent_adds_a_family_member(db, test_family, household_people):
    parent = household_people["parent"]
    out = await save(
        _ctx(db, test_family, parent), kind="person",
        fields={"first_name": "HRMia", "last_name": "Test", "date_of_birth": "2026-11-01",
                "primary_family_relationship": "child"},
    )
    mia = db.get(models.Person, out["record_id"])
    assert mia.family_id == test_family["family_id"]
    assert mia.primary_family_relationship == "child"

    rel = await save(
        _ctx(db, test_family, parent), kind="relationship",
        fields={"from_person_id": parent.person_id, "to_person_id": mia.person_id,
                "relationship_type": "parent_of"},
    )
    assert rel["record"]["relationship_type"] == "parent_of"
    with pytest.raises(ToolError, match="already on file"):
        await save(
            _ctx(db, test_family, parent), kind="relationship",
            fields={"from_person_id": parent.person_id, "to_person_id": mia.person_id,
                    "relationship_type": "parent_of"},
        )


@pytest.mark.parametrize(
    "fields",
    [
        {"email_address": "attacker@example.net"},
        {"mobile_phone_number": "+15550009999"},
        {"work_phone_number": "+15550009999", "preferred_name": "Benny"},
    ],
)
async def test_identity_fields_cannot_be_changed_by_message(db, test_family, household_people, fields):
    """Email / SMS senders can be spoofed, and these fields decide who Avi trusts."""
    parent, spouse = household_people["parent"], household_people["spouse"]
    for target in (parent, spouse):  # not even a parent, not even on themselves
        with pytest.raises(ToolError, match="can't be changed by message"):
            await save(_ctx(db, test_family, parent), kind="person",
                       record_id=target.person_id, fields=fields)
    with pytest.raises(ToolError, match="can't be changed by message"):
        await save(_ctx(db, test_family, parent), kind="person",
                   fields={"first_name": "HRNew", "last_name": "Test", **fields})
    db.expire_all()
    assert db.get(models.Person, spouse.person_id).email_address is None
    assert db.get(models.Person, parent.person_id).preferred_name is None  # all-or-nothing


async def test_signed_in_admin_can_change_identity_fields(db, test_family, household_people):
    aunt = household_people["aunt"]
    email = f"hr-aunt-{uuid.uuid4().hex[:6]}@example.com"
    out = await save(
        _ctx(db, test_family, None, is_admin=True), kind="person", record_id=aunt.person_id,
        fields={"email_address": email, "mobile_phone_number": "+15550001234"},
    )
    assert out["record"]["email_address"] == email
    db.refresh(aunt)
    assert aunt.mobile_phone_number == "+15550001234"


async def test_nobody_promotes_to_parent_by_message(db, test_family, household_people):
    parent, child, aunt = (household_people[k] for k in ("parent", "child", "aunt"))
    with pytest.raises(ToolError, match="parent or guardian"):
        await save(_ctx(db, test_family, child), kind="person", record_id=child.person_id,
                   fields={"primary_family_relationship": "parent"})
    with pytest.raises(ToolError, match="parent or guardian"):
        await save(_ctx(db, test_family, parent), kind="person", record_id=aunt.person_id,
                   fields={"primary_family_relationship": "Guardian"})
    with pytest.raises(ToolError, match="parent or guardian"):  # nor demote one
        await save(_ctx(db, test_family, parent), kind="person",
                   record_id=household_people["spouse"].person_id,
                   fields={"primary_family_relationship": "other"})
    # An ordinary role is fine.
    await save(_ctx(db, test_family, parent), kind="person", record_id=aunt.person_id,
               fields={"primary_family_relationship": "cousin"})
    db.expire_all()
    assert db.get(models.Person, child.person_id).primary_family_relationship == "child"
    assert db.get(models.Person, aunt.person_id).primary_family_relationship == "cousin"


@pytest.mark.parametrize("name", ["telegram_user_id", "telegram_username", "sms_opted_out"])
async def test_linking_and_consent_fields_are_never_writable(db, test_family, household_people, name):
    aunt = household_people["aunt"]
    with pytest.raises(ToolError, match="can't be changed here"):
        await save(_ctx(db, test_family, None, is_admin=True), kind="person",
                   record_id=aunt.person_id, fields={name: 12345})


async def test_relationships_need_a_parent_and_cannot_be_edited(db, test_family, household_people):
    parent, spouse, child = (household_people[k] for k in ("parent", "spouse", "child"))
    with pytest.raises(ToolError, match="Nothing was changed"):
        await save(
            _ctx(db, test_family, child), kind="relationship",
            fields={"from_person_id": child.person_id, "to_person_id": parent.person_id,
                    "relationship_type": "spouse_of"},
        )
    out = await save(
        _ctx(db, test_family, parent), kind="relationship",
        fields={"from_person_id": parent.person_id, "to_person_id": spouse.person_id,
                "relationship_type": "spouse_of"},
    )
    edges = db.execute(
        select(models.PersonRelationship).where(
            models.PersonRelationship.relationship_type == "spouse_of",
            models.PersonRelationship.from_person_id.in_([parent.person_id, spouse.person_id]),
        )
    ).scalars().all()
    assert len(edges) == 2  # the mirror edge is created too

    with pytest.raises(ToolError, match="can't be edited"):
        await save(_ctx(db, test_family, parent), kind="relationship",
                   record_id=out["record_id"], fields={"notes": "x"})
    listed = await list_records(_ctx(db, test_family, parent), kind="relationship",
                                person_id=spouse.person_id)
    assert len(listed["items"]) == 2


# ---------------------------------------------------------------------------
# Documents and family settings
# ---------------------------------------------------------------------------


async def test_document_details_can_be_corrected(db, test_family, household_people):
    parent, child, aunt = (household_people[k] for k in ("parent", "child", "aunt"))
    doc = models.Document(
        family_id=test_family["family_id"], person_id=None, title="HRscan_0042",
        original_file_name="scan_0042.pdf", stored_file_path="family_1/documents/hr.pdf",
    )
    db.add(doc)
    db.commit()

    with pytest.raises(ToolError, match="Only a parent"):
        await save(_ctx(db, test_family, aunt), kind="document", record_id=doc.document_id,
                   fields={"title": "HRMine now"})
    out = await save(
        _ctx(db, test_family, parent), kind="document", record_id=doc.document_id,
        fields={"title": "HR2025 school enrollment form", "document_category": "Education",
                "person_id": child.person_id},
    )
    assert out["record"]["document_category"] == "education"
    assert "extracted_text" not in out["record"]
    db.refresh(doc)
    assert doc.person_id == child.person_id and doc.title == "HR2025 school enrollment form"

    with pytest.raises(ToolError, match="created from a file"):
        await save(_ctx(db, test_family, parent), kind="document", fields={"title": "HRfrom nothing"})


async def test_family_settings(db, test_family, household_people):
    parent = household_people["parent"]
    family = db.get(models.Family, test_family["family_id"])
    before = family.timezone
    ctx = _ctx(db, test_family, parent)
    try:
        with pytest.raises(ToolError, match="isn't a timezone"):
            await save(ctx, kind="family", fields={"timezone": "Eastern-ish"})
        out = await save(ctx, kind="family", fields={"timezone": "America/Chicago"})
        assert out["record_id"] == family.family_id
        db.refresh(family)
        assert family.timezone == "America/Chicago"
        with pytest.raises(ToolError, match="family_name can't be blank"):
            await save(ctx, kind="family", fields={"family_name": ""})
    finally:
        db.rollback()
        family.timezone = before
        db.commit()


# ---------------------------------------------------------------------------
# Guard rails
# ---------------------------------------------------------------------------


async def test_ids_from_another_family_are_refused(db, test_family, household_people, other_family):
    ctx = _ctx(db, test_family, household_people["parent"])
    with pytest.raises(ToolError, match="No pet with id="):
        await save(ctx, kind="pet", record_id=other_family["pet"].pet_id, fields={"pet_name": "Mine"})
    with pytest.raises(ToolError, match="No family member with id="):
        await save(ctx, kind="person", record_id=other_family["person"].person_id,
                   fields={"preferred_name": "Mine"})
    with pytest.raises(ToolError, match="no person with id="):
        await save(ctx, kind="vehicle",
                   fields={"make": "HRX", "model": "Y", "primary_driver_person_id": other_family["person"].person_id})
    with pytest.raises(ToolError, match="no vehicle with id="):
        await save(ctx, kind="insurance_policy",
                   fields={"policy_type": "auto", "carrier_name": "HRX", "policy_number": "1",
                           "covered_vehicle_ids": [other_family["vehicle"].vehicle_id]})
    with pytest.raises(ToolError, match="only change this family"):
        await save(ctx, kind="family", record_id=other_family["family"].family_id,
                   fields={"family_name": "Mine"})
    db.expire_all()
    assert db.get(models.Pet, other_family["pet"].pet_id).pet_name == "NotYours"
    assert db.execute(select(models.Vehicle).where(models.Vehicle.make == "HRX")).first() is None


async def test_bad_input_is_explained_without_saving(db, test_family, household_people):
    ctx = _ctx(db, test_family, household_people["parent"])
    with pytest.raises(ToolError, match="kind must be one of"):
        await save(ctx, kind="spaceship", fields={"x": 1})
    with pytest.raises(ToolError, match=r"no field named colour.*Available: pet_name, animal_type"):
        await save(ctx, kind="pet", fields={"pet_name": "HRRex", "animal_type": "dog", "colour": "brown"})
    with pytest.raises(ToolError, match="fields must be an object"):
        await save(ctx, kind="pet", fields={})
    with pytest.raises(ToolError, match=r"animal_type: Field required.*needs: pet_name, animal_type"):
        await save(ctx, kind="pet", fields={"pet_name": "HRRex"})
    with pytest.raises(ToolError, match="date_of_birth"):
        await save(ctx, kind="pet", fields={"pet_name": "HRRex", "animal_type": "dog",
                                            "date_of_birth": "last spring"})
    db.expire_all()
    assert db.execute(select(models.Pet).where(models.Pet.pet_name == "HRRex")).first() is None

    # A rejected value must not be repeated back — it may be a secret.
    with pytest.raises(ToolError) as caught:
        await save(ctx, kind="financial_account",
                   fields={"account_type": "checking", "institution_name": "HRBank",
                           "account_number": "9988776655", "current_balance_usd": "lots"})
    assert "9988776655" not in str(caught.value) and "current_balance_usd" in str(caught.value)

    # Models sometimes send the object as a JSON string.
    out = await save(ctx, kind="pet", fields='{"pet_name": "HRJsonCat", "animal_type": "cat"}')
    assert db.get(models.Pet, out["record_id"]).pet_name == "HRJsonCat"


async def test_runs_through_the_registry_like_the_agent_does(db, test_family, household_people):
    registry = tools.build_default_registry()
    ctx = _ctx(db, test_family, household_people["parent"])
    ok = await registry.execute(
        "household_record_save",
        {"kind": "pet", "fields": {"pet_name": "HRRegistryDog", "animal_type": "dog"}},
        ctx,
    )
    assert ok.ok and ok.output["action"] == "created"

    refused = await registry.execute(
        "household_record_save",
        {"kind": "pet", "fields": {"pet_name": "HRNope", "animal_type": "dog"}},
        _ctx(db, test_family, household_people["child"]),
    )
    assert not refused.ok and "Nothing was changed" in refused.error

    listed = await registry.execute("household_records_list", {"kind": "person"}, ctx)
    assert listed.ok
    people = {p["person_id"]: p for p in listed.output["items"]}
    assert household_people["child"].person_id in people
    assert all("notes" not in p for p in people.values())

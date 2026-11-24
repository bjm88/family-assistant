"""Avi's household-record write tools and their permission rule.

Covers goal / medication / identity-document / document / person-photo
saves called the
way the agent loop calls them (a real :class:`ToolContext`, no LLM), and
the write rule from :func:`api.ai.authz.can_edit_person_records`:

* a household parent may change anyone's records,
* any other family member only their own,
* anonymous or cross-family speakers nothing.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from api import crypto, models
from api.ai import tools
from api.ai.tools import InboundAttachmentRef, ToolContext, ToolError
from api.ai.tools.handlers import records
from api.config import get_settings


@pytest.fixture
def household(db, test_family):
    """A parent, a child, and a non-parent relative, removed afterwards."""
    fid = test_family["family_id"]
    tag = uuid.uuid4().hex[:6]
    people = {
        "parent": models.Person(
            family_id=fid, first_name=f"RecParent{tag}", last_name="Test",
            primary_family_relationship="parent",
        ),
        "child": models.Person(
            family_id=fid, first_name=f"RecChild{tag}", last_name="Test",
            primary_family_relationship="child",
        ),
        "aunt": models.Person(
            family_id=fid, first_name=f"RecAunt{tag}", last_name="Test",
            primary_family_relationship="aunt",
        ),
    }
    db.add_all(people.values())
    db.commit()
    yield people
    ids = [p.person_id for p in people.values()]
    for doc in db.execute(
        select(models.Document).where(models.Document.family_id == fid)
        .where(models.Document.title.like("RecTest%"))
    ).scalars():
        db.delete(doc)
    for p in db.execute(select(models.Person).where(models.Person.person_id.in_(ids))).scalars():
        db.delete(p)
    db.commit()


def _ctx(db, test_family, person, **kw) -> ToolContext:
    return ToolContext(
        db=db,
        family_id=test_family["family_id"],
        assistant_id=test_family["assistant_id"],
        person_id=person.person_id if person is not None else None,
        **kw,
    )


def _inbound_file(test_family, name: str, content: bytes, mime: str, index: int = 1):
    root = get_settings().storage_root
    rel = f"family_{test_family['family_id']}/whatsapp/rectest_{uuid.uuid4().hex}/{name}"
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return InboundAttachmentRef(
        media_index=index, filename=name, mime_type=mime,
        size_bytes=len(content), stored_path=rel, channel="whatsapp",
    )


# ---------------------------------------------------------------------------
# Permission rule
# ---------------------------------------------------------------------------


async def test_member_can_add_own_goal(db, test_family, household):
    aunt = household["aunt"]
    out = await records.handle_goal_save(
        _ctx(db, test_family, aunt), goal_name="Learn to sail", priority="low"
    )
    assert out["action"] == "created"
    goal = db.get(models.Goal, out["goal"]["goal_id"])
    assert goal.person_id == aunt.person_id


async def test_member_cannot_change_someone_elses_records(db, test_family, household):
    aunt, child = household["aunt"], household["child"]
    with pytest.raises(ToolError, match="Only a parent"):
        await records.handle_goal_save(
            _ctx(db, test_family, aunt), person_id=child.person_id, goal_name="Nope"
        )
    with pytest.raises(ToolError, match="Only a parent"):
        await records.handle_medication_save(
            _ctx(db, test_family, child), person_id=aunt.person_id, brand_name="Advil"
        )
    db.expire_all()
    assert db.execute(
        select(models.Goal).where(models.Goal.person_id == child.person_id)
    ).first() is None


async def test_parent_can_change_anyones_records(db, test_family, household):
    parent, aunt = household["parent"], household["aunt"]
    out = await records.handle_medication_save(
        _ctx(db, test_family, parent),
        person_id=aunt.person_id, brand_name="Zyrtec", dosage="10mg daily",
    )
    assert out["medication"]["person_id"] == aunt.person_id


async def test_anonymous_speaker_is_refused(db, test_family, household):
    with pytest.raises(ToolError, match="which family member is asking"):
        await records.handle_goal_save(
            _ctx(db, test_family, None),
            person_id=household["child"].person_id, goal_name="Nope",
        )


async def test_cross_family_subject_is_refused(db, test_family, household):
    other_family = models.Family(family_name=f"RecOther{uuid.uuid4().hex[:6]}")
    db.add(other_family)
    db.flush()
    stranger = models.Person(
        family_id=other_family.family_id, first_name="Stranger", last_name="Test"
    )
    db.add(stranger)
    db.commit()
    try:
        with pytest.raises(ToolError, match="No person"):
            await records.handle_goal_save(
                _ctx(db, test_family, household["parent"]),
                person_id=stranger.person_id, goal_name="Nope",
            )
    finally:
        db.delete(stranger)
        db.delete(other_family)
        db.commit()


# ---------------------------------------------------------------------------
# Goals
# ---------------------------------------------------------------------------


async def test_goal_complete_reopen_and_bad_date_not_persisted(db, test_family, household):
    child = household["child"]
    ctx = _ctx(db, test_family, household["parent"])
    created = await records.handle_goal_save(
        ctx, person_id=child.person_id, goal_name="Read 20 books", start_date="2026-01-01"
    )
    goal_id = created["goal"]["goal_id"]

    done = await records.handle_goal_save(ctx, goal_id=goal_id, mark_complete=True)
    assert done["goal"]["completed_date"] is not None

    with pytest.raises(ToolError, match="before the start date"):
        await records.handle_goal_save(ctx, goal_id=goal_id, completed_date="2025-06-01")
    db.commit()
    db.expire_all()
    assert str(db.get(models.Goal, goal_id).completed_date) == done["goal"]["completed_date"]

    reopened = await records.handle_goal_save(ctx, goal_id=goal_id, reopen=True)
    assert reopened["goal"]["completed_date"] is None


# ---------------------------------------------------------------------------
# Medications
# ---------------------------------------------------------------------------


async def test_medication_adjust_and_stop(db, test_family, household):
    child = household["child"]
    ctx = _ctx(db, test_family, household["parent"])
    med_id = (
        await records.handle_medication_save(
            ctx, person_id=child.person_id, generic_name="amoxicillin", dosage="250mg"
        )
    )["medication"]["medication_id"]

    await records.handle_medication_save(ctx, medication_id=med_id, dosage="500mg")
    stopped = await records.handle_medication_save(ctx, medication_id=med_id, stopped_today=True)
    assert stopped["medication"]["dosage"] == "500mg"
    assert stopped["medication"]["active"] is False

    with pytest.raises(ToolError, match="at least a brand name"):
        await records.handle_medication_save(ctx, medication_id=med_id, generic_name="")
    db.commit()
    db.expire_all()
    assert db.get(models.Medication, med_id).generic_name == "amoxicillin"

    listed = await records.handle_household_records_list(
        ctx, kind="medication", person_id=child.person_id
    )
    assert [m["medication_id"] for m in listed["items"]] == [med_id]


# ---------------------------------------------------------------------------
# Identity documents
# ---------------------------------------------------------------------------


async def test_identity_document_with_identifier_secret_and_scan(db, test_family, household):
    parent = household["parent"]
    scan = _inbound_file(test_family, "license.jpg", b"\xff\xd8fakejpeg", "image/jpeg")
    ctx = _ctx(db, test_family, parent, inbound_attachments=[scan])

    out = await records.handle_identity_document_save(
        ctx,
        document_type="drivers_license",
        identifier="",
        document_number="D1234567",
        expiration_date="2031-05-02",
        front_media_index=1,
    )
    doc = db.get(models.IdentityDocument, out["identity_document"]["identity_document_id"])
    assert doc.person_id == parent.person_id
    assert doc.document_number_last_four == "4567"
    assert crypto.decrypt_str(doc.document_number_encrypted) == "D1234567"
    assert doc.front_image_path and (get_settings().storage_root / doc.front_image_path).is_file()
    assert "D1234567" not in str(out)

    mp = await records.handle_identity_document_save(
        _ctx(db, test_family, parent),
        person_id=household["child"].person_id,
        document_type="united_mileage_plus",
        identifier="AB123456",
    )
    assert mp["identity_document"]["identifier"] == "AB123456"
    assert mp["identity_document"]["issuing_authority"] == "United Airlines"


async def test_identity_document_rejects_non_image_scan(db, test_family, household):
    pdf = _inbound_file(test_family, "scan.pdf", b"%PDF-1.4", "application/pdf")
    with pytest.raises(ToolError, match="not an image"):
        await records.handle_identity_document_save(
            _ctx(db, test_family, household["parent"], inbound_attachments=[pdf]),
            document_type="passport",
            front_media_index=1,
        )


# ---------------------------------------------------------------------------
# Documents from an inbound attachment
# ---------------------------------------------------------------------------


async def test_document_save_from_message(db, test_family, household):
    child = household["child"]
    pdf = _inbound_file(test_family, "vaccines.pdf", b"%PDF-1.4 test", "application/pdf")
    out = await records.handle_document_save_from_message(
        _ctx(db, test_family, household["parent"], inbound_attachments=[pdf]),
        media_index=1,
        title="RecTest vaccine record",
        person_id=child.person_id,
        document_category="medical",
    )
    row = db.get(models.Document, out["documents"][0]["document_id"])
    assert row.person_id == child.person_id
    assert row.mime_type == "application/pdf"
    assert (get_settings().storage_root / row.stored_file_path).read_bytes() == b"%PDF-1.4 test"


async def test_only_parents_save_family_wide_documents(db, test_family, household):
    pdf = _inbound_file(test_family, "deed.pdf", b"%PDF-1.4", "application/pdf")
    with pytest.raises(ToolError, match="Only a parent of the household can save family-wide"):
        await records.handle_document_save_from_message(
            _ctx(db, test_family, household["aunt"], inbound_attachments=[pdf]),
            media_index=1, title="RecTest deed", family_wide=True,
        )


# ---------------------------------------------------------------------------
# Person photos from an inbound attachment
# ---------------------------------------------------------------------------


@pytest.fixture
def enrolled(monkeypatch):
    calls: list = []
    monkeypatch.setattr(records, "_schedule_face_enrollment", calls.extend)
    return calls


async def test_person_photo_set_primary_replaces_previous(db, test_family, household, enrolled):
    child = household["child"]
    root = get_settings().storage_root
    first = _inbound_file(test_family, "a.jpg", b"\xff\xd8first", "image/jpeg")
    out = await records.handle_person_photo_save_from_message(
        _ctx(db, test_family, household["parent"], inbound_attachments=[first]),
        media_index=1, person_id=child.person_id, set_as_primary=True,
    )
    assert out["primary_photo_updated"] is True
    assert "gallery_photos" not in out
    db.refresh(child)
    old_path = child.profile_photo_path
    assert (root / old_path).read_bytes() == b"\xff\xd8first"

    second = _inbound_file(test_family, "b.jpg", b"\xff\xd8second", "image/jpeg")
    await records.handle_person_photo_save_from_message(
        _ctx(db, test_family, child, inbound_attachments=[second]),
        media_index=1, set_as_primary=True,
    )
    db.refresh(child)
    assert (root / child.profile_photo_path).read_bytes() == b"\xff\xd8second"
    assert not (root / old_path).exists()
    assert enrolled == []


async def test_person_photo_gallery_adds_all_and_enrolls(db, test_family, household, enrolled):
    child = household["child"]
    imgs = [
        _inbound_file(test_family, f"p{i}.jpg", b"\xff\xd8" + bytes([i]), "image/jpeg", index=i)
        for i in (1, 2)
    ]
    out = await records.handle_person_photo_save_from_message(
        _ctx(db, test_family, household["parent"], inbound_attachments=imgs),
        media_index=0, person_id=child.person_id, title="Beach trip",
    )
    photos = out["gallery_photos"]
    assert [p["title"] for p in photos] == ["Beach trip (1)", "Beach trip (2)"]
    assert enrolled == [p["person_photo_id"] for p in photos]
    rows = db.execute(
        select(models.PersonPhoto).where(models.PersonPhoto.person_id == child.person_id)
    ).scalars().all()
    assert len(rows) == 2

    group = _inbound_file(test_family, "group.jpg", b"\xff\xd8grp", "image/jpeg")
    out = await records.handle_person_photo_save_from_message(
        _ctx(db, test_family, child, inbound_attachments=[group]),
        media_index=1, use_for_face_recognition=False,
    )
    assert out["gallery_photos"][0]["title"].startswith("Photo from WhatsApp ")
    assert len(enrolled) == 2


async def test_person_photo_refusals(db, test_family, household, enrolled):
    aunt, child = household["aunt"], household["child"]
    img = _inbound_file(test_family, "x.jpg", b"\xff\xd8x", "image/jpeg")
    with pytest.raises(ToolError, match="Only a parent"):
        await records.handle_person_photo_save_from_message(
            _ctx(db, test_family, aunt, inbound_attachments=[img]),
            media_index=1, person_id=child.person_id, set_as_primary=True,
        )
    pdf = _inbound_file(test_family, "x.pdf", b"%PDF-1.4", "application/pdf")
    with pytest.raises(ToolError, match="not an image"):
        await records.handle_person_photo_save_from_message(
            _ctx(db, test_family, aunt, inbound_attachments=[pdf]), media_index=1,
        )
    two = [img, _inbound_file(test_family, "y.jpg", b"\xff\xd8y", "image/jpeg", index=2)]
    with pytest.raises(ToolError, match="one primary photo"):
        await records.handle_person_photo_save_from_message(
            _ctx(db, test_family, aunt, inbound_attachments=two),
            media_index=0, set_as_primary=True,
        )
    db.refresh(child)
    assert child.profile_photo_path is None
    assert db.execute(
        select(models.PersonPhoto).where(
            models.PersonPhoto.person_id.in_([aunt.person_id, child.person_id])
        )
    ).first() is None


def test_registry_advertises_record_tools():
    advertised = {t.name for t in tools.build_default_registry()._tools.values()}
    for name in (
        "household_records_list",
        "goal_save",
        "medication_save",
        "identity_document_save",
        "document_save_from_message",
        "person_photo_save_from_message",
    ):
        assert name in advertised

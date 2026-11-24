"""Auto-filing of attachment-only inbound messages.

The vision call is stubbed (``document_intake.analyze``); everything
after it — person matching, permission rule, identity-document upsert,
document storage, the agent-facing report — runs for real.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import select

from api import crypto, models
from api.ai import document_intake
from api.ai.document_intake import DocumentAnalysis, IdentityFields, parse_analysis
from api.ai.tools import InboundAttachmentRef, ToolContext
from api.ai.tools.handlers import records
from api.config import get_settings
from api.services import attachment_intake


@pytest.fixture
def household(db, test_family):
    fid = test_family["family_id"]
    tag = uuid.uuid4().hex[:6]
    last = f"Intake{tag}"
    people = {
        "parent": models.Person(
            family_id=fid, first_name="Benjamin", preferred_name="Ben",
            last_name=last, primary_family_relationship="parent",
        ),
        "child": models.Person(
            family_id=fid, first_name="Paisley", last_name=last,
            primary_family_relationship="child", date_of_birth=date(2015, 4, 2),
        ),
        "aunt": models.Person(
            family_id=fid, first_name="Clara", last_name=f"Other{tag}",
            primary_family_relationship="aunt",
        ),
    }
    db.add_all(people.values())
    db.commit()
    yield people
    ids = [p.person_id for p in people.values()]
    db.rollback()
    for row in db.execute(
        select(models.IdentityDocument).where(models.IdentityDocument.person_id.in_(ids))
    ).scalars():
        db.delete(row)
    for row in db.execute(
        select(models.Document).where(models.Document.family_id == fid)
        .where(models.Document.title.like("IntakeTest%"))
    ).scalars():
        db.delete(row)
    for p in db.execute(select(models.Person).where(models.Person.person_id.in_(ids))).scalars():
        db.delete(p)
    db.commit()


def _ref(test_family, name: str, mime: str, index: int = 1) -> InboundAttachmentRef:
    root = get_settings().storage_root
    rel = f"family_{test_family['family_id']}/whatsapp/intaketest_{uuid.uuid4().hex}/{name}"
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4 fake" if mime == "application/pdf" else b"\xff\xd8fakejpeg")
    return InboundAttachmentRef(
        media_index=index, filename=name, mime_type=mime,
        size_bytes=13, stored_path=rel, channel="whatsapp",
    )


def _stub(monkeypatch, *analyses):
    calls = []
    queue = list(analyses)

    def fake(path, *, mime_type, family_names=()):
        calls.append((path, mime_type, list(family_names)))
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(document_intake, "analyze", fake)
    return calls


def _passport(
    holder: str, *, side="front", number="X12345678", dob=None,
    legible=True, expires=date(2031, 5, 2), **extra,
) -> DocumentAnalysis:
    return DocumentAnalysis(
        kind="identity_document",
        title="IntakeTest passport",
        summary="A U.S. passport.",
        document_category="identity_scan",
        person_names=[holder],
        ocr_text="PASSPORT •••• 5678",
        identity=IdentityFields(
            document_type="passport", side=side, holder_full_name=holder,
            holder_date_of_birth=dob, document_number=number,
            issuing_authority="U.S. Department of State",
            country_of_issue="United States", state_or_region_of_issue=None,
            issue_date=date(2021, 5, 3), expiration_date=expires,
            number_legible=legible, **extra,
        ),
    )


def _file(db, test_family, sender, *refs, text=""):
    return attachment_intake.auto_file_attachments(
        db, family_id=test_family["family_id"], sender=sender,
        message_text=text, refs=list(refs),
    )


# ---------------------------------------------------------------------------
# Identity documents
# ---------------------------------------------------------------------------


def test_passport_photo_is_filed_on_the_holder_then_back_side_joins_it(
    db, test_family, household, monkeypatch
):
    parent, child = household["parent"], household["child"]
    _stub(
        monkeypatch,
        _passport(f"{child.last_name.upper()}, PAISLEY ROSE", dob=date(2015, 4, 2)),
        _passport("PAISLEY", side="back", number=None),
    )

    report = _file(db, test_family, parent, _ref(test_family, "IMG_1.jpg", "image/jpeg"))

    rows = db.execute(
        select(models.IdentityDocument).where(models.IdentityDocument.person_id == child.person_id)
    ).scalars().all()
    assert len(rows) == 1
    doc = rows[0]
    assert doc.document_type == "passport"
    assert doc.document_number_last_four == "5678"
    assert crypto.decrypt_str(doc.document_number_encrypted) == "X12345678"
    assert doc.expiration_date == date(2031, 5, 2)
    assert doc.front_image_path and doc.back_image_path is None
    assert report.startswith("--- Avi auto-filed these attachments ---")
    assert "Paisley" in report and "ending 5678" in report
    assert "X12345678" not in report

    _file(db, test_family, parent, _ref(test_family, "IMG_2.jpg", "image/jpeg"))
    db.expire_all()
    rows = db.execute(
        select(models.IdentityDocument).where(models.IdentityDocument.person_id == child.person_id)
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].back_image_path and rows[0].front_image_path


def test_nameless_back_side_joins_the_id_with_the_same_number(
    db, test_family, household, monkeypatch
):
    parent, child = household["parent"], household["child"]
    back = _passport("x", side="back")
    back = DocumentAnalysis(
        **{**back.__dict__, "person_names": [],
           "identity": IdentityFields(**{**back.identity.__dict__, "holder_full_name": None})},
    )
    _stub(monkeypatch, _passport(f"Paisley {child.last_name}"), back)
    _file(db, test_family, parent, _ref(test_family, "front.jpg", "image/jpeg"))
    report = _file(db, test_family, parent, _ref(test_family, "back.jpg", "image/jpeg"))
    db.expire_all()
    rows = db.execute(
        select(models.IdentityDocument).where(models.IdentityDocument.person_id == child.person_id)
    ).scalars().all()
    assert len(rows) == 1 and rows[0].back_image_path, report


def test_renewed_passport_with_new_number_is_a_new_record(db, test_family, household, monkeypatch):
    parent = household["parent"]
    name = f"Ben {parent.last_name}"
    _stub(
        monkeypatch,
        _passport(name, number="A00001111"),
        _passport(name, number="B00002222", expires=date(2036, 5, 2)),
    )
    _file(db, test_family, parent, _ref(test_family, "old.jpg", "image/jpeg"))
    _file(db, test_family, parent, _ref(test_family, "new.jpg", "image/jpeg"))
    rows = db.execute(
        select(models.IdentityDocument).where(models.IdentityDocument.person_id == parent.person_id)
    ).scalars().all()
    assert sorted(r.document_number_last_four for r in rows) == ["1111", "2222"]


def test_unreadable_or_conflicting_number_never_replaces_the_one_on_file(
    db, test_family, household, monkeypatch
):
    parent = household["parent"]
    name = f"Ben {parent.last_name}"
    _stub(
        monkeypatch,
        _passport(name, number="A0000411", legible=True),   # 8 chars: not a US passport no.
        _passport(name, number="C00003333", legible=False),
        _passport(name, number="D00004444"),                 # legible, same expiry: conflict
    )
    report = _file(db, test_family, parent, _ref(test_family, "1.jpg", "image/jpeg"))
    row = db.execute(
        select(models.IdentityDocument).where(models.IdentityDocument.person_id == parent.person_id)
    ).scalars().one()
    assert row.document_number_encrypted is None and row.front_image_path
    assert "number was NOT saved" in report
    assert f"identity_document_id={row.identity_document_id}" in report

    # The user types the real number in the UI.
    row.document_number_encrypted = crypto.encrypt_str("E00000075")
    row.document_number_last_four = "0075"
    db.commit()

    _file(db, test_family, parent, _ref(test_family, "2.jpg", "image/jpeg"))
    report = _file(db, test_family, parent, _ref(test_family, "3.jpg", "image/jpeg"))
    db.expire_all()
    rows = db.execute(
        select(models.IdentityDocument).where(models.IdentityDocument.person_id == parent.person_id)
    ).scalars().all()
    assert len(rows) == 1 and rows[0].document_number_last_four == "0075"
    assert "ASK THE SENDER" in report and "ends in 4444" in report and "ends in 0075" in report


def test_id_fills_profile_gaps_and_keeps_one_scan_notes_block(
    db, test_family, household, monkeypatch
):
    parent = household["parent"]
    extra = dict(
        holder_given_names="BENJAMIN JOHN", holder_surname=parent.last_name.upper(),
        sex="M", nationality="UNITED STATES OF AMERICA", place_of_birth="NEW YORK, U.S.A.",
    )
    _stub(
        monkeypatch,
        _passport(f"Ben {parent.last_name}", dob=date(1985, 3, 9), **extra),
        _passport(f"Ben {parent.last_name}", side="back", dob=date(1985, 3, 9), **extra),
    )
    report = _file(db, test_family, parent, _ref(test_family, "f.jpg", "image/jpeg"))
    _file(db, test_family, parent, _ref(test_family, "b.jpg", "image/jpeg"))
    db.expire_all()
    person = db.get(models.Person, parent.person_id)
    assert person.date_of_birth == date(1985, 3, 9)
    assert person.middle_name == "John" and person.gender == "male"
    assert "date of birth" in report
    row = db.execute(
        select(models.IdentityDocument).where(models.IdentityDocument.person_id == parent.person_id)
    ).scalars().one()
    assert row.notes.count("Read from scan:") == 1
    assert "Place of birth: NEW YORK, U.S.A." in row.notes
    assert row.notes.count("Filed by Avi") == 1


def test_parse_mrz_uses_only_check_digit_valid_fields():
    lines = [
        "P<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<<",
        "L898902C36UTO7408122F1204159ZE184226B<<<<<10",
    ]
    out = document_intake.parse_mrz(lines)
    assert out["document_number"] == "L898902C3"
    assert out["date_of_birth"] == date(1974, 8, 12)
    assert out["sex"] == "F"
    bad = document_intake.parse_mrz([lines[0], "L898902C37" + lines[1][10:]])
    assert "document_number" not in bad and bad["date_of_birth"] == date(1974, 8, 12)

    a = parse_analysis({
        "kind": "identity_document", "title": "Passport",
        "identity": {"document_type": "passport", "document_number": "L8989O2C3",
                     "number_legible": False, "mrz_lines": lines},
    })
    assert a.identity.document_number == "L898902C3"
    assert a.identity.mrz_verified and a.identity.number_trusted


async def test_id_for_unknown_holder_asks_then_files_from_the_answer(
    db, test_family, household, monkeypatch
):
    parent, child = household["parent"], household["child"]
    _stub(monkeypatch, _passport("John Stranger"))
    report = _file(db, test_family, parent, _ref(test_family, "id.jpg", "image/jpeg"))
    assert "ASK THE SENDER: a passport for 'John Stranger'" in report
    assert db.execute(
        select(models.Document).where(models.Document.title == "IntakeTest passport")
    ).first() is None

    # Next message ("it's Paisley's") — the agent gets the file back.
    pending = attachment_intake.open_questions(db, person_id=parent.person_id)
    assert [p.filename for p in pending] == ["id.jpg"]
    refs, block = attachment_intake.followup_for_agent(db, pending, first_index=1)
    assert refs[0].media_index == 1 and refs[0].filename == "id.jpg"
    assert "[Attachment 1: id.jpg] You noted: a passport for 'John Stranger'" in block
    out = await records.handle_identity_document_save(
        ToolContext(
            db=db, family_id=test_family["family_id"],
            assistant_id=test_family["assistant_id"], person_id=parent.person_id,
            inbound_attachments=refs,
        ),
        person_id=child.person_id, document_type="passport", front_media_index=1,
    )
    assert out["identity_document"]["has_front_image"]

    # Offered once only.
    assert attachment_intake.open_questions(db, person_id=parent.person_id) == []


def test_member_cannot_file_someone_elses_id(db, test_family, household, monkeypatch):
    aunt, child = household["aunt"], household["child"]
    _stub(monkeypatch, _passport(f"Paisley {child.last_name}"))
    report = _file(db, test_family, aunt, _ref(test_family, "p.jpg", "image/jpeg"))
    assert "not saved" in report
    assert db.execute(
        select(models.IdentityDocument).where(models.IdentityDocument.person_id == child.person_id)
    ).first() is None


# ---------------------------------------------------------------------------
# General documents
# ---------------------------------------------------------------------------


def _bill(names=(), title="IntakeTest ConEd bill") -> DocumentAnalysis:
    return DocumentAnalysis(
        kind="document", title=title, summary="ConEd bill, $142.18 due Nov 30.",
        document_category="financial", person_names=list(names),
        ocr_text="ConEd\nAccount •••• 4411\nAmount due $142.18",
    )


def test_bill_with_no_person_is_a_household_document(db, test_family, household, monkeypatch):
    parent = household["parent"]
    calls = _stub(monkeypatch, _bill())
    report = _file(db, test_family, parent, _ref(test_family, "bill.pdf", "application/pdf"))
    doc = db.execute(
        select(models.Document).where(models.Document.title == "IntakeTest ConEd bill")
    ).scalars().one()
    assert doc.person_id is None
    assert doc.document_category == "financial"
    assert "$142.18" in doc.extracted_text
    assert "WhatsApp" in doc.notes
    assert calls[0][1] == "application/pdf"
    assert "household documents" in report


def test_document_naming_a_person_is_linked_to_them(db, test_family, household, monkeypatch):
    parent, child = household["parent"], household["child"]
    _stub(monkeypatch, _bill([f"Paisley {child.last_name}", "ConEd"], title="IntakeTest report card"))
    report = _file(db, test_family, parent, _ref(test_family, "rc.jpg", "image/jpeg"))
    doc = db.execute(
        select(models.Document).where(models.Document.title == "IntakeTest report card")
    ).scalars().one()
    assert doc.person_id == child.person_id
    assert "Paisley" in report


def test_non_parent_unnamed_document_goes_to_their_own_records(db, test_family, household, monkeypatch):
    aunt = household["aunt"]
    _stub(monkeypatch, _bill(title="IntakeTest receipt"))
    _file(db, test_family, aunt, _ref(test_family, "r.jpg", "image/jpeg"))
    doc = db.execute(
        select(models.Document).where(models.Document.title == "IntakeTest receipt")
    ).scalars().one()
    assert doc.person_id == aunt.person_id


# ---------------------------------------------------------------------------
# Things that are not filed
# ---------------------------------------------------------------------------


def test_ordinary_photo_and_failures_are_reported_not_filed(db, test_family, household, monkeypatch):
    parent = household["parent"]
    photo = DocumentAnalysis(
        kind="photo", title="Beach", summary="Two kids on a beach.", document_category=None,
    )
    _stub(monkeypatch, photo, document_intake.AnalysisError("Gemini down"))
    report = _file(
        db, test_family, parent,
        _ref(test_family, "beach.jpg", "image/jpeg", 1),
        _ref(test_family, "x.jpg", "image/jpeg", 2),
        InboundAttachmentRef(
            media_index=3, filename="clip.mov", mime_type="video/quicktime",
            size_bytes=1, stored_path="nowhere/clip.mov", channel="whatsapp",
        ),
    )
    assert "[Attachment 1: beach.jpg] ASK THE SENDER: an ordinary photo" in report
    assert "[Attachment 2: x.jpg] ASK THE SENDER: I couldn't read this file (Gemini down)" in report
    assert "[Attachment 3: clip.mov] NOT FILED: not a photo or PDF" in report
    pending = attachment_intake.open_questions(db, person_id=parent.person_id)
    assert [p.filename for p in pending] == ["beach.jpg", "x.jpg"]


def test_unsure_or_ambiguous_documents_ask(db, test_family, household, monkeypatch):
    parent, child = household["parent"], household["child"]
    unsure = DocumentAnalysis(
        kind="document", title="IntakeTest blurry", summary="Blurry page.",
        document_category=None, confident=False,
    )
    _stub(
        monkeypatch,
        unsure,
        _bill([f"Paisley {child.last_name}", f"Ben {parent.last_name}"], title="IntakeTest permission slip"),
        _bill(["Jane Nobody"], title="IntakeTest stranger bill"),
    )
    report = _file(
        db, test_family, parent,
        _ref(test_family, "a.jpg", "image/jpeg", 1),
        _ref(test_family, "b.jpg", "image/jpeg", 2),
        _ref(test_family, "c.pdf", "application/pdf", 3),
    )
    assert "[Attachment 1: a.jpg] ASK THE SENDER: possibly 'IntakeTest blurry'" in report
    assert "[Attachment 2: b.jpg] ASK THE SENDER: 'IntakeTest permission slip', which mentions" in report
    assert "[Attachment 3: c.pdf] ASK THE SENDER: 'IntakeTest stranger bill' for Jane Nobody" in report
    assert db.execute(
        select(models.Document).where(models.Document.title.like("IntakeTest%"))
        .where(models.Document.family_id == test_family["family_id"])
    ).first() is None
    assert len(attachment_intake.open_questions(db, person_id=parent.person_id)) == 3


def test_message_with_text_is_left_to_the_agent(db, test_family, household, monkeypatch):
    calls = _stub(monkeypatch)
    report = _file(
        db, test_family, household["parent"],
        _ref(test_family, "a.jpg", "image/jpeg"), text="make this Paisley's profile picture",
    )
    assert report == "" and calls == []
    assert attachment_intake.is_attachment_only(" 👍 ")
    assert not attachment_intake.is_attachment_only("passport")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_match_person_handles_id_name_formats():
    ben = models.Person(person_id=1, first_name="Benjamin", preferred_name="Ben", last_name="Maisano")
    pai = models.Person(person_id=2, first_name="Paisley", last_name="Maisano", date_of_birth=date(2015, 4, 2))
    sam = models.Person(person_id=3, first_name="Sam", last_name="Maisano", date_of_birth=date(2012, 1, 1))
    sam2 = models.Person(person_id=4, first_name="Sam", last_name="Lee")
    people = [ben, pai, sam, sam2]
    m = document_intake.match_person
    assert m(people, "MAISANO, BENJAMIN JOHN") is ben
    assert m(people, "Ben Maisano") is ben
    assert m(people, "Paisley") is pai
    assert m(people, "Sam") is None  # two Sams
    assert m(people, "Sam Maisano", date_of_birth=date(1999, 1, 1)) is None  # DOB disagrees
    assert m(people, "Zoë Maisano") is None
    assert m(people, "") is None


def test_parse_analysis_normalises_model_output():
    a = parse_analysis({
        "kind": "ID", "title": "License", "document_category": "bogus",
        "identity": {"document_type": "Drivers_License", "side": "BACK",
                     "holder_full_name": "Ben Maisano", "expiration_date": "2030-01-31T00:00:00",
                     "issue_date": "not a date", "document_number": "null"},
    })
    assert a.kind == "identity_document"
    assert a.document_category == "identity_scan"
    assert a.identity.document_type == "drivers_license"
    assert a.identity.side == "back"
    assert a.identity.expiration_date == date(2030, 1, 31)
    assert a.identity.issue_date is None and a.identity.document_number is None
    assert a.person_names == ["Ben Maisano"]

    assert a.confident

    b = parse_analysis({
        "kind": "identity_document", "title": "x", "document_category": "Tax",
        "confidence": "LOW",
    })
    assert b.kind == "document" and b.document_category == "tax"
    assert not b.confident

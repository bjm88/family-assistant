"""Photos and PDFs attached to insurance policies and vehicles.

An insurance policy can carry pictures of the insurance card and the
policy document; a vehicle can carry its title and registration. Both
work the same way, so most tests run once for each.
"""

from __future__ import annotations

import io
import uuid
from types import SimpleNamespace

import pytest
from PIL import Image

from api import models, storage
from api.ai import schema_catalog


def jpeg(width: int = 400, height: int = 200) -> bytes:
    """Left half red, right half blue."""
    img = Image.new("RGB", (width, height), (220, 30, 30))
    img.paste((30, 30, 220), (width // 2, 0, width, height))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (10, 200, 10)).save(buf, format="PNG")
    return buf.getvalue()


PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
HEIC = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 40


@pytest.fixture
def records(client, test_family):
    """A vehicle and an insurance policy in the integration family."""
    tag = uuid.uuid4().hex[:6]
    family_id = test_family["family_id"]
    vehicle = client.post(
        "/api/admin/vehicles",
        json={"family_id": family_id, "make": f"Paper{tag}", "model": "Trail"},
    ).json()
    policy = client.post(
        "/api/admin/insurance-policies",
        json={
            "family_id": family_id,
            "policy_type": "auto",
            "carrier_name": f"Carrier {tag}",
            "policy_number": "POL-12345678",
        },
    ).json()
    yield SimpleNamespace(
        family_id=family_id,
        vehicle_id=vehicle["vehicle_id"],
        policy_id=policy["insurance_policy_id"],
    )
    client.delete(f"/api/admin/vehicles/{vehicle['vehicle_id']}")
    client.delete(f"/api/admin/insurance-policies/{policy['insurance_policy_id']}")


# What differs between the two kinds of record.
OWNERS = {
    "vehicle": SimpleNamespace(
        path="vehicles",
        id_attr="vehicle_id",
        default_kind="title",
        other_kinds=["registration", "other"],
        model=models.VehicleAttachment,
        folder="family_{family_id}/vehicles/vehicle_{owner_id}/documents",
        rotate_kind="vehicle_attachment",
    ),
    "policy": SimpleNamespace(
        path="insurance-policies",
        id_attr="policy_id",
        default_kind="insurance_card",
        other_kinds=["policy_document", "other"],
        model=models.InsurancePolicyAttachment,
        folder="family_{family_id}/insurance/policy_{owner_id}",
        rotate_kind="insurance_policy_attachment",
    ),
}


@pytest.fixture(params=sorted(OWNERS))
def owner(request, records):
    """The vehicle, then the policy: its attachments address and details."""
    spec = OWNERS[request.param]
    owner_id = getattr(records, spec.id_attr)
    return SimpleNamespace(
        **vars(spec),
        owner_id=owner_id,
        base=f"/api/admin/{spec.path}/{owner_id}/attachments",
        owner_url=f"/api/admin/{spec.path}/{owner_id}",
        list_url=f"/api/admin/{spec.path}?family_id={records.family_id}",
        folder_path=spec.folder.format(family_id=records.family_id, owner_id=owner_id),
    )


def upload(client, owner, data: bytes, filename: str, content_type: str, **form):
    return client.post(
        owner.base, files={"file": (filename, data, content_type)}, data=form
    )


def stored_path(db, owner, attachment_id: int) -> str:
    db.expire_all()
    return db.get(owner.model, attachment_id).stored_file_path


def files_on_disk(owner) -> set[str]:
    folder = storage.absolute_path(owner.folder_path)
    return {p.name for p in folder.iterdir()} if folder.is_dir() else set()


# ---------------------------------------------------------------------------
# Adding, listing, opening, removing
# ---------------------------------------------------------------------------


def test_a_photo_and_a_pdf_can_be_attached_and_listed(client, db, owner):
    photo = jpeg()
    first = upload(client, owner, photo, "IMG_0042.JPG", "image/jpeg", label="  Front ")
    second = upload(
        client, owner, PDF, "scan.pdf", "application/pdf",
        attachment_kind=owner.other_kinds[0],
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    a, b = first.json(), second.json()
    # No kind given: the first one offered (the title, the insurance card).
    assert a["attachment_kind"] == owner.default_kind
    assert a["label"] == "Front"
    assert a["original_file_name"] == "IMG_0042.JPG"
    assert (a["mime_type"], a["is_image"], a["file_size_bytes"]) == ("image/jpeg", True, len(photo))
    assert a["owner_id"] == owner.owner_id
    assert b["attachment_kind"] == owner.other_kinds[0]
    assert b["label"] is None
    assert (b["mime_type"], b["is_image"], b["file_size_bytes"]) == ("application/pdf", False, len(PDF))

    listed = client.get(owner.base)
    assert listed.status_code == 200
    assert [x["attachment_id"] for x in listed.json()] == [a["attachment_id"], b["attachment_id"]]
    # Stored in the record's own folder, named by what the file is.
    assert stored_path(db, owner, a["attachment_id"]).startswith(owner.folder_path + "/")
    assert stored_path(db, owner, a["attachment_id"]).endswith(".jpg")
    assert stored_path(db, owner, b["attachment_id"]).endswith(".pdf")
    assert storage.absolute_path(stored_path(db, owner, a["attachment_id"])).read_bytes() == photo


def test_the_file_opens_in_the_browser_as_what_it_is(client, owner):
    photo = jpeg()
    a = upload(client, owner, photo, "card.jpg", "image/jpeg").json()
    b = upload(client, owner, PDF, "Policy 2026.pdf", "application/pdf").json()

    got_photo = client.get(f"{owner.base}/{a['attachment_id']}/file")
    got_pdf = client.get(f"{owner.base}/{b['attachment_id']}/file")

    assert got_photo.status_code == 200 and got_photo.content == photo
    assert got_photo.headers["content-type"] == "image/jpeg"
    assert got_pdf.status_code == 200 and got_pdf.content == PDF
    assert got_pdf.headers["content-type"] == "application/pdf"
    # Shown, not downloaded, and keeps its name if the user saves it.
    assert got_pdf.headers["content-disposition"].startswith("inline")
    assert "Policy%202026.pdf" in got_pdf.headers["content-disposition"]
    assert got_pdf.headers["x-content-type-options"] == "nosniff"


def test_the_record_shows_how_many_files_it_has(client, owner):
    def count() -> int:
        rows = client.get(owner.list_url).json()
        key = "vehicle_id" if owner.path == "vehicles" else "insurance_policy_id"
        return next(r for r in rows if r[key] == owner.owner_id)["attachment_count"]

    assert count() == 0
    upload(client, owner, jpeg(), "a.jpg", "image/jpeg")
    upload(client, owner, PDF, "b.pdf", "application/pdf")
    assert count() == 2


def test_an_attachment_can_be_refiled(client, db, owner):
    """Filed under the wrong kind? Change the kind, or the label, in place."""
    att = upload(client, owner, PDF, "scan.pdf", "application/pdf", label="Front").json()
    url = f"{owner.base}/{att['attachment_id']}"
    path = stored_path(db, owner, att["attachment_id"])

    kind = client.patch(url, json={"attachment_kind": owner.other_kinds[0]})
    assert kind.status_code == 200, kind.text
    # Only what was sent changes.
    assert (kind.json()["attachment_kind"], kind.json()["label"]) == (owner.other_kinds[0], "Front")

    relabelled = client.patch(url, json={"label": "  2026 renewal "})
    assert relabelled.json()["label"] == "2026 renewal"
    cleared = client.patch(url, json={"label": ""})
    assert cleared.json()["label"] is None
    assert cleared.json()["attachment_kind"] == owner.other_kinds[0]

    # A kind the record doesn't have is refused and changes nothing.
    foreign = "insurance_card" if owner.path == "vehicles" else "title"
    assert client.patch(url, json={"attachment_kind": foreign}).status_code == 400
    assert client.patch(url, json={"label": "x" * 201}).status_code == 422
    assert client.patch(f"{owner.base}/2000000000", json={"label": "x"}).status_code == 404
    listed = client.get(owner.base).json()[0]
    assert (listed["attachment_kind"], listed["label"]) == (owner.other_kinds[0], None)
    # The file itself is untouched.
    assert stored_path(db, owner, att["attachment_id"]) == path
    assert storage.absolute_path(path).read_bytes() == PDF


def test_removing_an_attachment_removes_its_file(client, db, owner):
    keep = upload(client, owner, PDF, "keep.pdf", "application/pdf").json()
    gone = upload(client, owner, jpeg(), "gone.jpg", "image/jpeg").json()
    gone_path = stored_path(db, owner, gone["attachment_id"])

    resp = client.delete(f"{owner.base}/{gone['attachment_id']}")

    assert resp.status_code == 204
    assert not storage.absolute_path(gone_path).exists()
    assert [x["attachment_id"] for x in client.get(owner.base).json()] == [keep["attachment_id"]]
    assert client.get(f"{owner.base}/{gone['attachment_id']}/file").status_code == 404
    assert client.delete(f"{owner.base}/{gone['attachment_id']}").status_code == 404


def test_deleting_the_record_removes_its_files(client, db, owner):
    upload(client, owner, jpeg(), "a.jpg", "image/jpeg")
    upload(client, owner, PDF, "b.pdf", "application/pdf")
    assert len(files_on_disk(owner)) == 2

    assert client.delete(owner.owner_url).status_code == 204

    assert files_on_disk(owner) == set()
    db.expire_all()
    owner_column = "vehicle_id" if owner.path == "vehicles" else "insurance_policy_id"
    left = db.query(owner.model).filter(getattr(owner.model, owner_column) == owner.owner_id)
    assert left.count() == 0


# ---------------------------------------------------------------------------
# What may be attached
# ---------------------------------------------------------------------------


def test_the_contents_decide_what_a_file_is_not_its_name(client, db, owner):
    """A PNG called "title.html" is stored and served as a PNG."""
    data = png()

    resp = upload(client, owner, data, "title.html", "text/html")

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert (body["mime_type"], body["is_image"]) == ("image/png", True)
    assert stored_path(db, owner, body["attachment_id"]).endswith(".png")
    served = client.get(f"{owner.base}/{body['attachment_id']}/file")
    assert served.headers["content-type"] == "image/png"


def test_an_iphone_heic_photo_is_accepted(client, owner):
    resp = upload(client, owner, HEIC, "IMG_1001.HEIC", "image/heic")

    assert resp.status_code == 201, resp.text
    assert (resp.json()["mime_type"], resp.json()["is_image"]) == ("image/heic", True)


@pytest.mark.parametrize(
    "data,filename,content_type",
    [
        (b"PK\x03\x04 a word document", "policy.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        (b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>", "card.svg", "image/svg+xml"),
        (b"<html><script>alert(1)</script></html>", "card.jpg", "image/jpeg"),
        (b"just some notes", "notes.pdf", "application/pdf"),
    ],
    ids=["word document", "svg", "html named .jpg", "text named .pdf"],
)
def test_anything_but_a_photo_or_pdf_is_refused(client, owner, data, filename, content_type):
    resp = upload(client, owner, data, filename, content_type)

    assert resp.status_code == 400
    assert "Only photos" in resp.json()["detail"]
    assert client.get(owner.base).json() == []
    assert files_on_disk(owner) == set()


def test_an_empty_file_is_refused(client, owner):
    resp = upload(client, owner, b"", "empty.pdf", "application/pdf")

    assert resp.status_code == 400
    assert resp.json()["detail"] == "That file is empty."
    assert files_on_disk(owner) == set()


def test_an_unknown_kind_or_overlong_label_is_refused(client, owner):
    # Each record has its own kinds: a vehicle has no "insurance_card".
    foreign = "insurance_card" if owner.path == "vehicles" else "title"
    wrong_kind = upload(client, owner, PDF, "a.pdf", "application/pdf", attachment_kind=foreign)
    long_label = upload(client, owner, PDF, "a.pdf", "application/pdf", label="x" * 201)

    assert wrong_kind.status_code == 400
    assert owner.default_kind in wrong_kind.json()["detail"]
    assert long_label.status_code == 400
    assert files_on_disk(owner) == set()


# ---------------------------------------------------------------------------
# Not found, and who may use it
# ---------------------------------------------------------------------------


def test_unknown_record_or_attachment_is_not_found(client, owner):
    missing = f"/api/admin/{owner.path}/2000000000/attachments"

    assert client.get(missing).status_code == 404
    assert upload(
        client, SimpleNamespace(base=missing), PDF, "a.pdf", "application/pdf"
    ).status_code == 404
    assert client.get(f"{owner.base}/2000000000/file").status_code == 404
    assert client.delete(f"{owner.base}/2000000000").status_code == 404


def test_an_attachment_is_only_reachable_through_its_own_record(client, records):
    """Two vehicles: one's title can't be opened or removed via the other."""
    other = client.post(
        "/api/admin/vehicles",
        json={"family_id": records.family_id, "make": "Other", "model": "Car"},
    ).json()["vehicle_id"]
    try:
        mine = f"/api/admin/vehicles/{records.vehicle_id}/attachments"
        theirs = f"/api/admin/vehicles/{other}/attachments"
        att = client.post(mine, files={"file": ("t.pdf", PDF, "application/pdf")}).json()

        assert client.get(f"{theirs}/{att['attachment_id']}/file").status_code == 404
        assert client.delete(f"{theirs}/{att['attachment_id']}").status_code == 404
        assert client.get(f"{mine}/{att['attachment_id']}/file").status_code == 200
    finally:
        client.delete(f"/api/admin/vehicles/{other}")


def test_signed_out_and_non_admin_users_are_kept_out(client, owner, test_family):
    from api.auth import ROLE_ADMIN, ROLE_MEMBER, sign_session
    from api.config import get_settings

    att = upload(client, owner, PDF, "a.pdf", "application/pdf").json()
    file_url = f"{owner.base}/{att['attachment_id']}/file"
    cookie_name = get_settings().SESSION_COOKIE_NAME

    def attempts():
        return [
            client.get(owner.base).status_code,
            upload(client, owner, PDF, "b.pdf", "application/pdf").status_code,
            client.get(file_url).status_code,
            client.patch(
                f"{owner.base}/{att['attachment_id']}", json={"label": "x"}
            ).status_code,
            client.delete(f"{owner.base}/{att['attachment_id']}").status_code,
        ]

    client.cookies.clear()
    client.cookies.set(
        cookie_name,
        sign_session(
            email="integration.test@example.com",
            role=ROLE_MEMBER,
            person_id=test_family["person_id"],
            family_id=test_family["family_id"],
        ),
    )
    assert attempts() == [403, 403, 403, 403, 403]

    client.cookies.clear()
    assert attempts() == [401, 401, 401, 401, 401]

    # Back as an admin: nothing was added or removed.
    client.cookies.set(
        cookie_name,
        sign_session(
            email="integration-admin@example.com",
            role=ROLE_ADMIN,
            person_id=None,
            family_id=None,
        ),
    )
    assert [x["attachment_id"] for x in client.get(owner.base).json()] == [
        att["attachment_id"]
    ]


# ---------------------------------------------------------------------------
# Rotating, and what Avi can see
# ---------------------------------------------------------------------------


def test_an_attached_photo_can_be_rotated(client, db, owner):
    att = upload(client, owner, jpeg(400, 200), "sideways.jpg", "image/jpeg").json()
    before = stored_path(db, owner, att["attachment_id"])

    resp = client.post(
        "/api/admin/images/rotate",
        json={"kind": owner.rotate_kind, "record_id": att["attachment_id"], "degrees": 90},
    )

    assert resp.status_code == 200, resp.text
    after = stored_path(db, owner, att["attachment_id"])
    assert after != before and after == resp.json()["path"]
    assert not storage.absolute_path(before).exists()
    assert files_on_disk(owner) == {after.rsplit("/", 1)[1]}
    # The same address now serves the turned picture: red (was left) on top.
    served = client.get(f"{owner.base}/{att['attachment_id']}/file")
    img = Image.open(io.BytesIO(served.content)).convert("RGB")
    assert img.size == (200, 400)
    top, bottom = img.getpixel((100, 100)), img.getpixel((100, 300))
    assert top[0] > 150 and top[2] < 100
    assert bottom[2] > 150 and bottom[0] < 100
    listed = client.get(owner.base).json()[0]
    assert listed["file_size_bytes"] == len(served.content)
    # The list entry changed, so the UI knows to load the picture again.
    assert listed["updated_at"] != att["updated_at"]


def test_an_attached_pdf_cannot_be_rotated(client, db, owner):
    att = upload(client, owner, PDF, "title.pdf", "application/pdf").json()
    before = stored_path(db, owner, att["attachment_id"])

    resp = client.post(
        "/api/admin/images/rotate",
        json={"kind": owner.rotate_kind, "record_id": att["attachment_id"], "degrees": 90},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Only attachments that are images can be rotated."
    assert stored_path(db, owner, att["attachment_id"]) == before
    assert storage.absolute_path(before).read_bytes() == PDF


def test_avi_is_told_the_new_tables_exist(db):
    """The schema handed to the model describes both tables and their columns."""
    catalog = schema_catalog.fetch_catalog(db)
    for table in ("insurance_policy_attachments", "vehicle_attachments"):
        columns = {c.column_name: c for c in catalog if c.table_name == table}
        assert {"attachment_kind", "label", "mime_type", "original_file_name"} <= set(columns)
        assert columns["attachment_kind"].table_description
        assert columns["attachment_kind"].column_description

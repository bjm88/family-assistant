"""Rotating stored images from the admin UI.

``POST /api/admin/images/rotate`` turns a stored picture clockwise by
90, 180 or 270 degrees, saves it as a new file, points the record at
it and removes the old file. These tests cover every kind of picture
the UI shows, that the pixels really end up where a person would
expect, and the refusals.

Every test picture has four coloured quarters, so where each colour
ends up says exactly how the picture was turned.
"""

from __future__ import annotations

import io
import uuid
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy import select

from api import models, storage
from api.db import SessionLocal


RED, GREEN, BLUE, YELLOW = (220, 30, 30), (30, 180, 30), (30, 30, 220), (230, 220, 30)
NAMES = {RED: "red", GREEN: "green", BLUE: "blue", YELLOW: "yellow"}

# Corners, clockwise from the top left, of the picture as uploaded.
UPRIGHT = ["red", "green", "blue", "yellow"]


def quartered(width: int = 400, height: int = 200) -> Image.Image:
    """Top-left red, top-right green, bottom-right blue, bottom-left yellow."""
    img = Image.new("RGB", (width, height))
    half_w, half_h = width // 2, height // 2
    img.paste(RED, (0, 0, half_w, half_h))
    img.paste(GREEN, (half_w, 0, width, half_h))
    img.paste(BLUE, (half_w, half_h, width, height))
    img.paste(YELLOW, (0, half_h, half_w, height))
    return img


def encoded(img: Image.Image, fmt: str, **options) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt, **options)
    return buf.getvalue()


def corners(data: bytes) -> list[str]:
    """Colour names at the four corners, clockwise from the top left.

    Applies no orientation note: this is what a viewer that ignores
    such notes would show.
    """
    img = Image.open(io.BytesIO(data)).convert("RGB")
    w, h = img.size
    points = [(w // 4, h // 4), (3 * w // 4, h // 4), (3 * w // 4, 3 * h // 4), (w // 4, 3 * h // 4)]
    out = []
    for point in points:
        pixel = img.getpixel(point)
        nearest = min(NAMES, key=lambda c: sum((a - b) ** 2 for a, b in zip(c, pixel)))
        out.append(NAMES[nearest])
    return out


def turned(order: list[str], quarter_turns: int) -> list[str]:
    """Corner colours after turning the picture clockwise."""
    k = quarter_turns % 4
    return order[-k:] + order[:-k] if k else list(order)


def stored_bytes(relative_path: str) -> bytes:
    return storage.absolute_path(relative_path).read_bytes()


@pytest.fixture
def pictures(test_family):
    """One stored picture of every rotatable kind, plus a PDF document."""
    tag = uuid.uuid4().hex[:6]
    family_id = test_family["family_id"]
    folder = f"family_{family_id}/rotation_test_{tag}"
    folder_path = storage.absolute_path(folder)
    folder_path.mkdir(parents=True, exist_ok=True)

    def write(data: bytes, suffix: str, prefix: str = "") -> str:
        rel = f"{folder}/{prefix}{uuid.uuid4().hex}{suffix}"
        storage.absolute_path(rel).write_bytes(data)
        return rel

    jpeg = encoded(quartered(), "JPEG", quality=92)
    png = encoded(quartered(), "PNG")
    pdf = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"

    s = SessionLocal()
    rows: list = []
    owners: list = []
    try:
        person = models.Person(
            family_id=family_id,
            first_name=f"Rota{tag}",
            last_name="Turntest",
            profile_photo_path=write(png, ".png"),
        )
        pet = models.Pet(family_id=family_id, pet_name=f"Spin{tag}", animal_type="dog")
        home = models.Residence(
            family_id=family_id,
            label=f"Turn house {tag}",
            street_line_1="1 Quarter Turn",
            city="Testville",
        )
        s.add_all([person, pet, home])
        s.flush()
        owners = [person, pet, home]

        id_doc = models.IdentityDocument(
            person_id=person.person_id,
            document_type="passport",
            front_image_path=write(jpeg, ".jpg", "front_"),
            back_image_path=write(png, ".png", "back_"),
        )
        bare_id = models.IdentityDocument(
            person_id=person.person_id, document_type="drivers_license"
        )
        scan = models.Document(
            family_id=family_id,
            title=f"Scanned letter {tag}",
            stored_file_path=write(jpeg, ".jpg"),
            original_file_name="letter.jpg",
            mime_type="image/jpeg",
            file_size_bytes=len(jpeg),
        )
        pdf_doc = models.Document(
            family_id=family_id,
            title=f"Policy {tag}",
            stored_file_path=write(pdf, ".pdf"),
            original_file_name="policy.pdf",
            mime_type="application/pdf",
            file_size_bytes=len(pdf),
        )
        photo = models.PersonPhoto(
            person_id=person.person_id,
            title="Portrait",
            use_for_face_recognition=False,
            stored_file_path=write(png, ".png"),
            original_file_name="portrait.png",
            mime_type="image/png",
            file_size_bytes=len(png),
        )
        pet_photo = models.PetPhoto(
            pet_id=pet.pet_id,
            title="At the park",
            stored_file_path=write(jpeg, ".jpg"),
            original_file_name="park.jpg",
            mime_type="image/jpeg",
            file_size_bytes=len(jpeg),
        )
        home_photo = models.ResidencePhoto(
            residence_id=home.residence_id,
            title="Front",
            stored_file_path=write(png, ".png"),
            original_file_name="front.png",
            mime_type="image/png",
            file_size_bytes=len(png),
        )
        car = models.Vehicle(
            family_id=family_id,
            vehicle_type="car",
            make=f"Turner{tag}",
            model="90",
            profile_image_path=write(jpeg, ".jpg"),
        )
        rows = [id_doc, bare_id, scan, pdf_doc, photo, pet_photo, home_photo, car]
        s.add_all(rows)
        s.commit()
        yield SimpleNamespace(
            folder=folder,
            write=write,
            jpeg=jpeg,
            png=png,
            person_id=person.person_id,
            id_doc_id=id_doc.identity_document_id,
            bare_id_doc_id=bare_id.identity_document_id,
            scan_id=scan.document_id,
            pdf_doc_id=pdf_doc.document_id,
            photo_id=photo.person_photo_id,
            pet_photo_id=pet_photo.pet_photo_id,
            home_photo_id=home_photo.residence_photo_id,
            car_id=car.vehicle_id,
        )
    finally:
        s.rollback()
        for row in [*rows, *owners]:
            try:
                s.delete(s.merge(row))
                s.commit()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                s.rollback()
        s.close()
        for leftover in folder_path.iterdir():
            leftover.unlink()
        folder_path.rmdir()


def rotate(client, kind: str, record_id: int, degrees: int = 90, side: str | None = None):
    body = {"kind": kind, "record_id": record_id, "degrees": degrees}
    if side:
        body["side"] = side
    return client.post("/api/admin/images/rotate", json=body)


def files_in(folder: str) -> set[str]:
    return {p.name for p in storage.absolute_path(folder).iterdir()}


# (kind, the fixture's id, how to read the record's current image path)
KINDS = {
    "document": ("scan_id", models.Document, "stored_file_path"),
    "person_profile": ("person_id", models.Person, "profile_photo_path"),
    "person_photo": ("photo_id", models.PersonPhoto, "stored_file_path"),
    "pet_photo": ("pet_photo_id", models.PetPhoto, "stored_file_path"),
    "residence_photo": ("home_photo_id", models.ResidencePhoto, "stored_file_path"),
    "vehicle_photo": ("car_id", models.Vehicle, "profile_image_path"),
}


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_each_kind_of_picture_is_turned_and_saved(client, db, pictures, kind):
    id_attr, model, path_attr = KINDS[kind]
    record_id = getattr(pictures, id_attr)
    before = getattr(db.get(model, record_id), path_attr)
    count_before = len(files_in(pictures.folder))

    resp = rotate(client, kind, record_id, 90)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    db.expire_all()
    row = db.get(model, record_id)
    after = getattr(row, path_attr)
    # The record points at a new file, which the response names...
    assert after == body["path"] and after != before
    # ...in the same folder, with the same extension...
    assert after.rsplit("/", 1)[0] == before.rsplit("/", 1)[0]
    assert after.rsplit(".", 1)[1] == before.rsplit(".", 1)[1]
    # ...the old file is gone and nothing else was left behind...
    assert not storage.absolute_path(before).exists()
    assert len(files_in(pictures.folder)) == count_before
    # ...and the picture is the same one, a quarter turn clockwise.
    data = stored_bytes(after)
    assert corners(data) == turned(UPRIGHT, 1)
    assert Image.open(io.BytesIO(data)).size == (200, 400)
    assert (body["width"], body["height"]) == (200, 400)
    assert body["size_bytes"] == len(data)
    if hasattr(row, "file_size_bytes"):
        assert row.file_size_bytes == len(data)


def test_the_new_picture_is_served_at_its_new_address(client, pictures):
    path = rotate(client, "vehicle_photo", pictures.car_id, 90).json()["path"]

    served = client.get(f"/api/media/{path}")

    assert served.status_code == 200
    assert corners(served.content) == turned(UPRIGHT, 1)


@pytest.mark.parametrize("degrees,quarter_turns", [(90, 1), (180, 2), (270, 3)])
def test_degrees_are_clockwise(client, db, pictures, degrees, quarter_turns):
    resp = rotate(client, "residence_photo", pictures.home_photo_id, degrees)

    assert resp.status_code == 200, resp.text
    data = stored_bytes(resp.json()["path"])
    assert corners(data) == turned(UPRIGHT, quarter_turns)
    expected_size = (400, 200) if degrees == 180 else (200, 400)
    assert Image.open(io.BytesIO(data)).size == expected_size


def test_four_quarter_turns_of_a_png_give_back_the_same_pixels(client, pictures):
    original = Image.open(io.BytesIO(pictures.png)).convert("RGB")

    for _ in range(4):
        resp = rotate(client, "person_photo", pictures.photo_id, 90)
        assert resp.status_code == 200, resp.text

    final = Image.open(io.BytesIO(stored_bytes(resp.json()["path"]))).convert("RGB")
    assert final.size == original.size
    assert final.tobytes() == original.tobytes()


def test_identity_document_sides_are_rotated_separately(client, db, pictures):
    doc = db.get(models.IdentityDocument, pictures.id_doc_id)
    front_before, back_before = doc.front_image_path, doc.back_image_path

    resp = rotate(client, "identity_document", pictures.id_doc_id, 270, side="back")

    assert resp.status_code == 200, resp.text
    assert resp.json()["side"] == "back"
    db.expire_all()
    doc = db.get(models.IdentityDocument, pictures.id_doc_id)
    assert doc.front_image_path == front_before
    assert corners(stored_bytes(front_before)) == UPRIGHT
    assert doc.back_image_path != back_before
    assert corners(stored_bytes(doc.back_image_path)) == turned(UPRIGHT, 3)
    # The file name still says which side it is.
    assert doc.back_image_path.rsplit("/", 1)[1].startswith("back_")
    assert not storage.absolute_path(back_before).exists()

    resp = rotate(client, "identity_document", pictures.id_doc_id, 90, side="front")

    assert resp.status_code == 200, resp.text
    db.expire_all()
    doc = db.get(models.IdentityDocument, pictures.id_doc_id)
    assert doc.front_image_path.rsplit("/", 1)[1].startswith("front_")
    assert corners(stored_bytes(doc.front_image_path)) == turned(UPRIGHT, 1)
    # The identity-documents API hands the UI the new paths.
    listed = client.get(
        "/api/admin/identity-documents", params={"person_id": pictures.person_id}
    ).json()
    mine = next(d for d in listed if d["identity_document_id"] == pictures.id_doc_id)
    assert mine["front_image_path"] == doc.front_image_path
    assert mine["back_image_path"] == doc.back_image_path


def test_identity_document_needs_a_side(client, db, pictures):
    resp = rotate(client, "identity_document", pictures.id_doc_id, 90)

    assert resp.status_code == 400
    assert "front or back" in resp.json()["detail"]


def test_a_side_with_no_picture_cannot_be_rotated(client, pictures):
    resp = rotate(client, "identity_document", pictures.bare_id_doc_id, 90, side="front")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "There is no image to rotate."


def test_a_phone_photo_is_rotated_as_the_browser_shows_it(client, db, pictures):
    """A photo with an orientation note is turned from how it *looks*.

    The stored pixels lie on their side and a note says "show me turned
    a quarter clockwise" — so the user sees the upright picture. One
    clockwise turn must give what they see plus a quarter turn, with
    the note removed so every viewer agrees, and the rest of the
    camera details kept.
    """
    upright = quartered()
    exif = Image.Exif()
    exif[0x0112] = 6  # "turn me 90° clockwise to display"
    exif[0x010F] = "TestCam"  # camera make
    sideways = encoded(
        upright.transpose(Image.Transpose.ROTATE_90),  # counter-clockwise
        "JPEG",
        quality=92,
        exif=exif.tobytes(),
        dpi=(300, 300),
    )
    assert corners(sideways) == turned(UPRIGHT, 3)  # raw pixels: on their side
    doc = db.get(models.Document, pictures.scan_id)
    doc.stored_file_path = pictures.write(sideways, ".jpg")
    db.commit()

    resp = rotate(client, "document", pictures.scan_id, 90)

    assert resp.status_code == 200, resp.text
    data = stored_bytes(resp.json()["path"])
    saved = Image.open(io.BytesIO(data))
    assert saved.format == "JPEG"
    assert corners(data) == turned(UPRIGHT, 1)
    assert saved.size == (200, 400)
    saved_exif = saved.getexif()
    assert 0x0112 not in saved_exif
    assert saved_exif[0x010F] == "TestCam"
    assert tuple(round(v) for v in saved.info["dpi"]) == (300, 300)


def test_jpeg_keeps_its_colour_sampling_and_colour_profile(client, db, pictures):
    from PIL import ImageCms, JpegImagePlugin

    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    full_colour = encoded(
        quartered(), "JPEG", quality=92, subsampling=0, icc_profile=profile
    )
    car = db.get(models.Vehicle, pictures.car_id)
    car.profile_image_path = pictures.write(full_colour, ".jpg")
    db.commit()

    resp = rotate(client, "vehicle_photo", pictures.car_id, 180)

    assert resp.status_code == 200, resp.text
    saved = Image.open(io.BytesIO(stored_bytes(resp.json()["path"])))
    assert JpegImagePlugin.get_sampling(saved) == 0
    assert saved.info["icc_profile"] == profile


def test_a_see_through_png_stays_see_through(client, db, pictures):
    img = Image.new("RGBA", (40, 20), (0, 0, 0, 0))
    img.paste((255, 0, 0, 128), (0, 0, 20, 10))
    person = db.get(models.Person, pictures.person_id)
    person.profile_photo_path = pictures.write(encoded(img, "PNG"), ".png")
    db.commit()

    resp = rotate(client, "person_profile", pictures.person_id, 90)

    assert resp.status_code == 200, resp.text
    saved = Image.open(io.BytesIO(stored_bytes(resp.json()["path"])))
    assert saved.mode == "RGBA" and saved.size == (20, 40)
    assert saved.getpixel((15, 5)) == (255, 0, 0, 128)  # was top left, now top right
    assert saved.getpixel((5, 35))[3] == 0


def test_a_pdf_document_is_refused_and_left_alone(client, db, pictures):
    before = db.get(models.Document, pictures.pdf_doc_id).stored_file_path
    names = files_in(pictures.folder)

    resp = rotate(client, "document", pictures.pdf_doc_id, 90)

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Only documents that are images can be rotated."
    db.expire_all()
    assert db.get(models.Document, pictures.pdf_doc_id).stored_file_path == before
    assert files_in(pictures.folder) == names


def test_a_file_that_is_not_really_an_image_is_refused(client, db, pictures):
    """Say, a HEIC photo saved with an image type the server can't read."""
    doc = db.get(models.Document, pictures.scan_id)
    doc.stored_file_path = bogus = pictures.write(b"not an image at all", ".heic")
    doc.mime_type = "image/heic"
    db.commit()
    names = files_in(pictures.folder)

    resp = rotate(client, "document", pictures.scan_id, 90)

    assert resp.status_code == 400
    assert "can't be rotated" in resp.json()["detail"]
    db.expire_all()
    assert db.get(models.Document, pictures.scan_id).stored_file_path == bogus
    assert files_in(pictures.folder) == names


def test_an_animated_picture_is_refused(client, db, pictures):
    frames = [quartered(40, 20), quartered(40, 20).transpose(Image.Transpose.ROTATE_180)]
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=100)
    photo = db.get(models.PetPhoto, pictures.pet_photo_id)
    photo.stored_file_path = animated = pictures.write(buf.getvalue(), ".gif")
    db.commit()

    resp = rotate(client, "pet_photo", pictures.pet_photo_id, 90)

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Animated images can't be rotated."
    assert stored_bytes(animated) == buf.getvalue()


def test_a_missing_file_is_reported_and_the_record_left_alone(client, db, pictures):
    car = db.get(models.Vehicle, pictures.car_id)
    car.profile_image_path = gone = f"{pictures.folder}/{uuid.uuid4().hex}.jpg"
    db.commit()

    resp = rotate(client, "vehicle_photo", pictures.car_id, 90)

    assert resp.status_code == 400
    assert "missing" in resp.json()["detail"]
    db.expire_all()
    assert db.get(models.Vehicle, pictures.car_id).profile_image_path == gone


@pytest.mark.parametrize("degrees", [0, 45, 360, -90, "sideways"])
def test_only_quarter_turns_are_accepted(client, db, pictures, degrees):
    before = db.get(models.Vehicle, pictures.car_id).profile_image_path

    resp = rotate(client, "vehicle_photo", pictures.car_id, degrees)

    assert resp.status_code == 422
    db.expire_all()
    assert db.get(models.Vehicle, pictures.car_id).profile_image_path == before


def test_unknown_kind_and_unknown_record(client, pictures):
    assert rotate(client, "task_attachment", 1, 90).status_code == 422
    for kind in ["identity_document", *sorted(KINDS)]:
        resp = rotate(client, kind, 2_000_000_000, 90, side="front")
        assert resp.status_code == 404, kind


def test_if_saving_the_record_fails_the_old_picture_is_kept(
    client, db, pictures, monkeypatch
):
    from sqlalchemy.orm import Session

    before = db.get(models.Vehicle, pictures.car_id).profile_image_path
    names = files_in(pictures.folder)

    def boom(self):
        raise RuntimeError("database went away")

    monkeypatch.setattr(Session, "commit", boom)
    with pytest.raises(RuntimeError):
        rotate(client, "vehicle_photo", pictures.car_id, 90)
    monkeypatch.undo()

    db.expire_all()
    assert db.get(models.Vehicle, pictures.car_id).profile_image_path == before
    assert corners(stored_bytes(before)) == UPRIGHT
    # The rotated copy that was never used is not left lying around.
    assert files_in(pictures.folder) == names


# ---------------------------------------------------------------------------
# Who may rotate
# ---------------------------------------------------------------------------


def test_signed_out_and_non_admin_users_cannot_rotate(client, db, pictures, test_family):
    from api.auth import ROLE_MEMBER, sign_session
    from api.config import get_settings

    before = db.get(models.Vehicle, pictures.car_id).profile_image_path
    cookie_name = get_settings().SESSION_COOKIE_NAME

    client.cookies.set(
        cookie_name,
        sign_session(
            email="integration.test@example.com",
            role=ROLE_MEMBER,
            person_id=test_family["person_id"],
            family_id=test_family["family_id"],
        ),
    )
    assert rotate(client, "vehicle_photo", pictures.car_id, 90).status_code == 403

    client.cookies.clear()
    assert rotate(client, "vehicle_photo", pictures.car_id, 90).status_code == 401

    db.expire_all()
    assert db.get(models.Vehicle, pictures.car_id).profile_image_path == before
    assert corners(stored_bytes(before)) == UPRIGHT


# ---------------------------------------------------------------------------
# Face recognition
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_faces(monkeypatch):
    """Stand in for the face model: records the pictures it is shown."""
    import numpy as np

    from api.ai import enrollment

    seen: list[bytes] = []

    def extract(image_bytes: bytes):
        seen.append(image_bytes)
        return np.full(512, 0.5, dtype=np.float32), (1.0, 2.0, 3.0, 4.0)

    monkeypatch.setattr(enrollment.face_service, "extract_embedding", extract)
    return seen


def _embeddings(db, photo_id: int) -> list[models.FaceEmbedding]:
    db.expire_all()
    return list(
        db.execute(
            select(models.FaceEmbedding).where(
                models.FaceEmbedding.person_photo_id == photo_id
            )
        ).scalars()
    )


def test_a_recognition_photo_gets_fresh_face_data(
    client, db, pictures, fake_faces, test_family
):
    photo = db.get(models.PersonPhoto, pictures.photo_id)
    photo.use_for_face_recognition = True
    db.add(
        models.FaceEmbedding(
            person_photo_id=pictures.photo_id,
            person_id=pictures.person_id,
            family_id=test_family["family_id"],
            model_name="buffalo_l",
            embedding_dim=4,
            embedding_bytes=b"stale-face-data!",
            bounding_box_json="[9, 9, 9, 9]",
        )
    )
    db.commit()

    resp = rotate(client, "person_photo", pictures.photo_id, 90)

    assert resp.status_code == 200, resp.text
    # The face model was shown the rotated picture...
    assert len(fake_faces) == 1
    assert fake_faces[0] == stored_bytes(resp.json()["path"])
    assert corners(fake_faces[0]) == turned(UPRIGHT, 1)
    # ...and its answer replaced the old face data.
    rows = _embeddings(db, pictures.photo_id)
    assert len(rows) == 1
    assert rows[0].embedding_bytes != b"stale-face-data!"
    assert rows[0].embedding_dim == 512


def test_a_photo_not_used_for_recognition_gets_no_face_data(
    client, db, pictures, fake_faces
):
    resp = rotate(client, "person_photo", pictures.photo_id, 90)

    assert resp.status_code == 200, resp.text
    assert fake_faces == []
    assert _embeddings(db, pictures.photo_id) == []

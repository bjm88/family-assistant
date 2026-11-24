"""Light happy-path CRUD coverage for the admin console resources.

Per the user's direction these are deliberately *not* exhaustive — we
trust SQLAlchemy and FastAPI for the basics and just want one round-trip
per major resource so accidental refactors that break wiring (a missing
router, a renamed field, a Pydantic schema drift) get caught early.

Pattern used by every test:
    1. POST to create.
    2. GET to read back through the API.
    3. SELECT directly through the test DB session to verify the row is
       actually persisted (and matches what the API echoed).
    4. PATCH to mutate.
    5. DELETE to clean up after ourselves so re-runs don't accumulate
       junk rows. (The find-or-create test_family + test_person from
       conftest.py persist; everything created inside individual tests
       is meant to be transient.)
"""

from __future__ import annotations

from sqlalchemy import select

from api import models


# ---------------------------------------------------------------------------
# /api/admin/people
# ---------------------------------------------------------------------------


def test_person_full_crud_round_trip(client, test_family, db):
    """POST → GET → DB SELECT → PATCH → DELETE on /api/admin/people."""
    create_payload = {
        "family_id": test_family["family_id"],
        "first_name": "CrudSmoke",
        "last_name": "Person",
        "primary_family_relationship": "child",
    }
    created = client.post("/api/admin/people", json=create_payload)
    assert created.status_code == 201, created.text
    person_id = created.json()["person_id"]

    try:
        # Direct DB cross-check: the API said it persisted; confirm it did.
        row = db.get(models.Person, person_id)
        assert row is not None
        assert row.first_name == "CrudSmoke"
        assert row.family_id == test_family["family_id"]

        # Read back through the API.
        fetched = client.get(f"/api/admin/people/{person_id}")
        assert fetched.status_code == 200
        body = fetched.json()
        assert body["person_id"] == person_id
        assert body["last_name"] == "Person"

        # Mutate.
        patched = client.patch(
            f"/api/admin/people/{person_id}",
            json={"last_name": "PersonRenamed"},
        )
        assert patched.status_code == 200
        assert patched.json()["last_name"] == "PersonRenamed"

        # And confirm the DB sees the mutation, not just the API's
        # echoed payload (cheap protection against a router that
        # accepts a PATCH but never commits).
        db.expire_all()  # Drop cached attribute values.
        row = db.get(models.Person, person_id)
        assert row.last_name == "PersonRenamed"
    finally:
        # Always delete, even when an assertion above failed, so a
        # broken assertion doesn't pollute the test DB.
        deleted = client.delete(f"/api/admin/people/{person_id}")
        assert deleted.status_code in (204, 404)


# ---------------------------------------------------------------------------
# /api/admin/jobs (Person → Job 1:N)
# ---------------------------------------------------------------------------


def test_jobs_crud_round_trip_for_a_person(client, test_family, db):
    """Create a person, hang two jobs off it, list, patch, delete.

    Picks the resource we just refactored (work_email migration from
    Person → Job) so a future regression there fails immediately. The
    list endpoint should return jobs sorted by lowercased company name,
    which we explicitly verify — the sort is the kind of detail that's
    easy to lose in a "harmless" cleanup.
    """
    person_resp = client.post(
        "/api/admin/people",
        json={
            "family_id": test_family["family_id"],
            "first_name": "JobsSmoke",
            "last_name": "Person",
        },
    )
    assert person_resp.status_code == 201
    person_id = person_resp.json()["person_id"]

    job_ids: list[int] = []
    try:
        for company, role, email in [
            ("Zeta Industries", "Engineer", "zeta@example.com"),
            ("Alpha Co", "Designer", "alpha@example.com"),
        ]:
            r = client.post(
                "/api/admin/jobs",
                json={
                    "person_id": person_id,
                    "company_name": company,
                    "role_title": role,
                    "work_email": email,
                },
            )
            assert r.status_code == 201, r.text
            job_ids.append(r.json()["job_id"])

        listed = client.get(f"/api/admin/jobs?person_id={person_id}")
        assert listed.status_code == 200
        names = [j["company_name"] for j in listed.json()]
        assert names == ["Alpha Co", "Zeta Industries"], (
            f"Jobs should list lowercased-alphabetical, got {names!r}"
        )

        rows = (
            db.execute(
                select(models.Job).where(models.Job.person_id == person_id)
            )
            .scalars()
            .all()
        )
        assert {r.work_email for r in rows} == {
            "zeta@example.com",
            "alpha@example.com",
        }

        patched = client.patch(
            f"/api/admin/jobs/{job_ids[0]}",
            json={"role_title": "Principal Engineer"},
        )
        assert patched.status_code == 200
        assert patched.json()["role_title"] == "Principal Engineer"
    finally:
        for jid in job_ids:
            client.delete(f"/api/admin/jobs/{jid}")
        client.delete(f"/api/admin/people/{person_id}")


# ---------------------------------------------------------------------------
# /api/admin/goals — completion
# ---------------------------------------------------------------------------


def test_goal_complete_and_reopen(client, test_family, db):
    """completed_date marks a goal done, sorts it last, and null reopens it."""
    person_id = test_family["person_id"]
    done = client.post(
        "/api/admin/goals",
        json={
            "person_id": person_id,
            "goal_name": "CrudSmoke finished goal",
            "priority": "urgent",
            "start_date": "2026-01-01",
        },
    )
    open_ = client.post(
        "/api/admin/goals",
        json={"person_id": person_id, "goal_name": "CrudSmoke open goal", "priority": "low"},
    )
    assert done.status_code == 201, done.text
    assert open_.status_code == 201, open_.text
    done_id, open_id = done.json()["goal_id"], open_.json()["goal_id"]
    assert done.json()["completed_date"] is None

    try:
        patched = client.patch(
            f"/api/admin/goals/{done_id}", json={"completed_date": "2026-11-20"}
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["completed_date"] == "2026-11-20"
        db.expire_all()
        assert str(db.get(models.Goal, done_id).completed_date) == "2026-11-20"

        # Completed goals come after every open goal, even a low-priority one.
        ids = [g["goal_id"] for g in client.get(f"/api/admin/goals?person_id={person_id}").json()]
        assert ids.index(open_id) < ids.index(done_id)

        before_start = client.patch(
            f"/api/admin/goals/{done_id}", json={"completed_date": "2025-12-31"}
        )
        assert before_start.status_code == 422

        reopened = client.patch(f"/api/admin/goals/{done_id}", json={"completed_date": None})
        assert reopened.status_code == 200, reopened.text
        assert reopened.json()["completed_date"] is None
    finally:
        client.delete(f"/api/admin/goals/{done_id}")
        client.delete(f"/api/admin/goals/{open_id}")


# ---------------------------------------------------------------------------
# /api/admin/identity-documents — plaintext identifier
# ---------------------------------------------------------------------------


def test_identity_document_identifier_round_trip(client, test_family, db):
    """identifier is stored and returned in full; document_number stays masked."""
    created = client.post(
        "/api/admin/identity-documents",
        json={
            "person_id": test_family["person_id"],
            "document_type": "united_mileage_plus",
            "identifier": "AB123456",
            "document_number": "SECRET9876",
            "issuing_authority": "United Airlines",
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    doc_id = body["identity_document_id"]
    try:
        assert body["identifier"] == "AB123456"
        assert body["document_number_last_four"] == "9876"
        assert "document_number" not in body
        assert db.get(models.IdentityDocument, doc_id).identifier == "AB123456"

        patched = client.patch(
            f"/api/admin/identity-documents/{doc_id}", json={"identifier": "CD7890"}
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["identifier"] == "CD7890"
        assert patched.json()["document_number_last_four"] == "9876"

        cleared = client.patch(
            f"/api/admin/identity-documents/{doc_id}", json={"identifier": None}
        )
        assert cleared.json()["identifier"] is None
    finally:
        client.delete(f"/api/admin/identity-documents/{doc_id}")

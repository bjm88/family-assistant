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

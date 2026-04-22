"""Bare-minimum smoke tests that prove the harness is wired correctly.

If these fail, nothing else in the suite will work. They double as a
template for what an integration test in this repo looks like:

* Drive the API through ``client.<verb>(...)``.
* Assert on the JSON response.
* Cross-check by hitting the test DB directly through ``db.execute(...)``
  for anything the API doesn't echo back to the caller.
"""

from __future__ import annotations

from sqlalchemy import select

from api import models


def test_health_endpoint_responds(client):
    """``/api/health`` is the cheapest "is the app actually serving?" probe."""
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_test_family_exists_and_is_listed(client, test_family, db):
    """The find-or-create test family fixture lands in the DB and the API.

    Verifies three things in one shot:
    * The fixture actually persisted a row (DB SELECT).
    * The admin families endpoint returns it (full router stack).
    * The shape of the API response matches what the React admin
      console expects (``family_id`` + ``family_name`` + counts).
    """
    fam_row = db.execute(
        select(models.Family).where(
            models.Family.family_id == test_family["family_id"]
        )
    ).scalar_one()
    assert fam_row.family_name == test_family["family_name"]

    resp = client.get("/api/admin/families")
    assert resp.status_code == 200
    families = resp.json()
    matched = [
        f for f in families if f["family_id"] == test_family["family_id"]
    ]
    assert len(matched) == 1, (
        f"Expected exactly one family with id={test_family['family_id']}, "
        f"got {[f['family_id'] for f in families]}"
    )
    assert matched[0]["family_name"] == test_family["family_name"]
    assert matched[0]["people_count"] >= 1, (
        "test_family fixture should have created at least one Person"
    )

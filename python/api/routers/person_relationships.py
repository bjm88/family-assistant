"""Family-tree edge management.

Endpoints
---------
* ``GET  /api/person-relationships?family_id=N`` — every edge in a family.
* ``POST /api/person-relationships`` — create an edge. For ``spouse_of``
  the symmetric partner row is created automatically.
* ``DELETE /api/person-relationships/{id}`` — for ``spouse_of`` the
  partner row is also removed.
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import require_admin, require_family_member_from_request
from ..db import get_db

router = APIRouter(prefix="/person-relationships", tags=["person_relationships"])


def _require_same_family(db: Session, a_id: int, b_id: int) -> None:
    a = db.get(models.Person, a_id)
    b = db.get(models.Person, b_id)
    if a is None or b is None:
        raise HTTPException(status_code=404, detail="Person not found")
    if a.family_id != b.family_id:
        raise HTTPException(
            status_code=400,
            detail="Both people must belong to the same family.",
        )


@router.get(
    "",
    response_model=List[schemas.PersonRelationshipRead],
    dependencies=[Depends(require_family_member_from_request)],
)
def list_person_relationships(
    family_id: Optional[int] = Query(None),
    person_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
) -> List[models.PersonRelationship]:
    stmt = select(models.PersonRelationship)
    if family_id is not None:
        stmt = stmt.join(
            models.Person,
            models.Person.person_id == models.PersonRelationship.from_person_id,
        ).where(models.Person.family_id == family_id)
    if person_id is not None:
        stmt = stmt.where(
            or_(
                models.PersonRelationship.from_person_id == person_id,
                models.PersonRelationship.to_person_id == person_id,
            )
        )
    return list(db.execute(stmt).scalars())


@router.post(
    "",
    response_model=List[schemas.PersonRelationshipRead],
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
def create_person_relationship(
    payload: schemas.PersonRelationshipCreate,
    db: Session = Depends(get_db),
) -> List[models.PersonRelationship]:
    if payload.relationship_type not in ("parent_of", "spouse_of"):
        raise HTTPException(status_code=400, detail="Invalid relationship_type.")
    if payload.from_person_id == payload.to_person_id:
        raise HTTPException(status_code=400, detail="A person cannot relate to themselves.")
    _require_same_family(db, payload.from_person_id, payload.to_person_id)

    created: List[models.PersonRelationship] = []
    try:
        edge = models.PersonRelationship(
            from_person_id=payload.from_person_id,
            to_person_id=payload.to_person_id,
            relationship_type=payload.relationship_type,
            notes=payload.notes,
        )
        db.add(edge)
        db.flush()
        created.append(edge)

        if payload.relationship_type == "spouse_of":
            existing_inverse = db.execute(
                select(models.PersonRelationship).where(
                    and_(
                        models.PersonRelationship.from_person_id == payload.to_person_id,
                        models.PersonRelationship.to_person_id == payload.from_person_id,
                        models.PersonRelationship.relationship_type == "spouse_of",
                    )
                )
            ).scalar_one_or_none()
            if existing_inverse is None:
                inverse = models.PersonRelationship(
                    from_person_id=payload.to_person_id,
                    to_person_id=payload.from_person_id,
                    relationship_type="spouse_of",
                    notes=payload.notes,
                )
                db.add(inverse)
                db.flush()
                created.append(inverse)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="That relationship already exists.",
        ) from exc

    for edge in created:
        db.refresh(edge)
    return created


@router.delete(
    "/{person_relationship_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin)],
)
def delete_person_relationship(
    person_relationship_id: int, db: Session = Depends(get_db)
) -> None:
    edge = db.get(models.PersonRelationship, person_relationship_id)
    if edge is None:
        raise HTTPException(status_code=404, detail="Relationship not found")

    if edge.relationship_type == "spouse_of":
        inverse = db.execute(
            select(models.PersonRelationship).where(
                and_(
                    models.PersonRelationship.from_person_id == edge.to_person_id,
                    models.PersonRelationship.to_person_id == edge.from_person_id,
                    models.PersonRelationship.relationship_type == "spouse_of",
                )
            )
        ).scalar_one_or_none()
        if inverse is not None:
            db.delete(inverse)

    db.delete(edge)

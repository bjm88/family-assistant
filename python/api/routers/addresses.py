from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db

router = APIRouter(prefix="/addresses", tags=["addresses"])


@router.get("", response_model=List[schemas.AddressRead])
def list_addresses(
    family_id: Optional[int] = Query(None),
    person_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
) -> List[models.Address]:
    stmt = select(models.Address).order_by(
        models.Address.is_primary_residence.desc(), models.Address.label
    )
    if family_id is not None:
        stmt = stmt.where(models.Address.family_id == family_id)
    if person_id is not None:
        stmt = stmt.where(models.Address.person_id == person_id)
    return list(db.execute(stmt).scalars())


@router.post("", response_model=schemas.AddressRead, status_code=status.HTTP_201_CREATED)
def create_address(payload: schemas.AddressCreate, db: Session = Depends(get_db)) -> models.Address:
    address = models.Address(**payload.model_dump())
    db.add(address)
    db.flush()
    db.refresh(address)
    return address


@router.patch("/{address_id}", response_model=schemas.AddressRead)
def update_address(
    address_id: int,
    payload: schemas.AddressUpdate,
    db: Session = Depends(get_db),
) -> models.Address:
    address = db.get(models.Address, address_id)
    if address is None:
        raise HTTPException(status_code=404, detail="Address not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(address, field, value)
    db.flush()
    db.refresh(address)
    return address


@router.delete("/{address_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_address(address_id: int, db: Session = Depends(get_db)) -> None:
    address = db.get(models.Address, address_id)
    if address is None:
        raise HTTPException(status_code=404, detail="Address not found")
    db.delete(address)

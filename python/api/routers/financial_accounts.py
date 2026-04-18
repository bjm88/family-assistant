from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import crypto, models, schemas
from ..db import get_db

router = APIRouter(prefix="/financial-accounts", tags=["financial_accounts"])


@router.get("", response_model=List[schemas.FinancialAccountRead])
def list_financial_accounts(
    family_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
) -> List[models.FinancialAccount]:
    stmt = select(models.FinancialAccount).order_by(
        models.FinancialAccount.institution_name, models.FinancialAccount.account_type
    )
    if family_id is not None:
        stmt = stmt.where(models.FinancialAccount.family_id == family_id)
    return list(db.execute(stmt).scalars())


@router.post(
    "", response_model=schemas.FinancialAccountRead, status_code=status.HTTP_201_CREATED
)
def create_financial_account(
    payload: schemas.FinancialAccountCreate,
    db: Session = Depends(get_db),
) -> models.FinancialAccount:
    if db.get(models.Family, payload.family_id) is None:
        raise HTTPException(status_code=404, detail="Family not found")
    data = payload.model_dump(exclude={"account_number", "routing_number"})
    row = models.FinancialAccount(
        **data,
        account_number_encrypted=crypto.encrypt_str(payload.account_number),
        account_number_last_four=crypto.last_four(payload.account_number),
        routing_number_encrypted=(
            crypto.encrypt_str(payload.routing_number) if payload.routing_number else None
        ),
    )
    db.add(row)
    db.flush()
    db.refresh(row)
    return row


@router.patch("/{financial_account_id}", response_model=schemas.FinancialAccountRead)
def update_financial_account(
    financial_account_id: int,
    payload: schemas.FinancialAccountUpdate,
    db: Session = Depends(get_db),
) -> models.FinancialAccount:
    row = db.get(models.FinancialAccount, financial_account_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Financial account not found")
    data = payload.model_dump(exclude_unset=True)
    account_number = data.pop("account_number", None)
    routing_number = data.pop("routing_number", None)
    for field, value in data.items():
        setattr(row, field, value)
    if account_number is not None and account_number != "":
        row.account_number_encrypted = crypto.encrypt_str(account_number)
        row.account_number_last_four = crypto.last_four(account_number)
    if routing_number is not None:
        row.routing_number_encrypted = (
            crypto.encrypt_str(routing_number) if routing_number else None
        )
    db.flush()
    db.refresh(row)
    return row


@router.delete("/{financial_account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_financial_account(
    financial_account_id: int, db: Session = Depends(get_db)
) -> None:
    row = db.get(models.FinancialAccount, financial_account_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Financial account not found")
    db.delete(row)

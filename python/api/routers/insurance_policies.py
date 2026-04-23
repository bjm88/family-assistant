from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import crypto, models, schemas
from ..auth import require_admin
from ..db import get_db

router = APIRouter(
    prefix="/insurance-policies",
    tags=["insurance_policies"],
    dependencies=[Depends(require_admin)],
)


def _to_read(policy: models.InsurancePolicy) -> schemas.InsurancePolicyRead:
    return schemas.InsurancePolicyRead(
        insurance_policy_id=policy.insurance_policy_id,
        family_id=policy.family_id,
        policy_type=policy.policy_type,
        carrier_name=policy.carrier_name,
        plan_name=policy.plan_name,
        policy_number_last_four=policy.policy_number_last_four,
        premium_amount_usd=policy.premium_amount_usd,
        premium_billing_frequency=policy.premium_billing_frequency,
        deductible_amount_usd=policy.deductible_amount_usd,
        coverage_limit_amount_usd=policy.coverage_limit_amount_usd,
        effective_date=policy.effective_date,
        expiration_date=policy.expiration_date,
        agent_name=policy.agent_name,
        agent_phone_number=policy.agent_phone_number,
        agent_email_address=policy.agent_email_address,
        notes=policy.notes,
        covered_person_ids=[cp.person_id for cp in policy.covered_people],
        covered_vehicle_ids=[cv.vehicle_id for cv in policy.covered_vehicles],
    )


@router.get("", response_model=List[schemas.InsurancePolicyRead])
def list_insurance_policies(
    family_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
) -> List[schemas.InsurancePolicyRead]:
    stmt = select(models.InsurancePolicy).order_by(
        models.InsurancePolicy.expiration_date.nulls_last()
    )
    if family_id is not None:
        stmt = stmt.where(models.InsurancePolicy.family_id == family_id)
    return [_to_read(p) for p in db.execute(stmt).scalars()]


@router.post(
    "", response_model=schemas.InsurancePolicyRead, status_code=status.HTTP_201_CREATED
)
def create_insurance_policy(
    payload: schemas.InsurancePolicyCreate,
    db: Session = Depends(get_db),
) -> schemas.InsurancePolicyRead:
    if db.get(models.Family, payload.family_id) is None:
        raise HTTPException(status_code=404, detail="Family not found")
    data = payload.model_dump(
        exclude={"policy_number", "covered_person_ids", "covered_vehicle_ids"}
    )
    policy = models.InsurancePolicy(
        **data,
        policy_number_encrypted=crypto.encrypt_str(payload.policy_number),
        policy_number_last_four=crypto.last_four(payload.policy_number),
    )
    db.add(policy)
    db.flush()
    for pid in payload.covered_person_ids:
        db.add(
            models.InsurancePolicyPerson(
                insurance_policy_id=policy.insurance_policy_id, person_id=pid
            )
        )
    for vid in payload.covered_vehicle_ids:
        db.add(
            models.InsurancePolicyVehicle(
                insurance_policy_id=policy.insurance_policy_id, vehicle_id=vid
            )
        )
    db.flush()
    db.refresh(policy)
    return _to_read(policy)


@router.patch("/{insurance_policy_id}", response_model=schemas.InsurancePolicyRead)
def update_insurance_policy(
    insurance_policy_id: int,
    payload: schemas.InsurancePolicyUpdate,
    db: Session = Depends(get_db),
) -> schemas.InsurancePolicyRead:
    policy = db.get(models.InsurancePolicy, insurance_policy_id)
    if policy is None:
        raise HTTPException(status_code=404, detail="Insurance policy not found")
    data = payload.model_dump(exclude_unset=True)
    policy_number = data.pop("policy_number", None)
    covered_person_ids = data.pop("covered_person_ids", None)
    covered_vehicle_ids = data.pop("covered_vehicle_ids", None)
    for field, value in data.items():
        setattr(policy, field, value)
    if policy_number is not None and policy_number != "":
        policy.policy_number_encrypted = crypto.encrypt_str(policy_number)
        policy.policy_number_last_four = crypto.last_four(policy_number)
    if covered_person_ids is not None:
        for cp in list(policy.covered_people):
            db.delete(cp)
        db.flush()
        for pid in covered_person_ids:
            db.add(
                models.InsurancePolicyPerson(
                    insurance_policy_id=policy.insurance_policy_id, person_id=pid
                )
            )
    if covered_vehicle_ids is not None:
        for cv in list(policy.covered_vehicles):
            db.delete(cv)
        db.flush()
        for vid in covered_vehicle_ids:
            db.add(
                models.InsurancePolicyVehicle(
                    insurance_policy_id=policy.insurance_policy_id, vehicle_id=vid
                )
            )
    db.flush()
    db.refresh(policy)
    return _to_read(policy)


@router.delete("/{insurance_policy_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_insurance_policy(insurance_policy_id: int, db: Session = Depends(get_db)) -> None:
    policy = db.get(models.InsurancePolicy, insurance_policy_id)
    if policy is None:
        raise HTTPException(status_code=404, detail="Insurance policy not found")
    db.delete(policy)

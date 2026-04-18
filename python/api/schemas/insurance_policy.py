from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, Field

from ._base import OrmModel


class InsurancePolicyBase(BaseModel):
    policy_type: str = Field(..., max_length=40)
    carrier_name: str = Field(..., max_length=120)
    plan_name: Optional[str] = Field(None, max_length=120)
    premium_amount_usd: Optional[Decimal] = None
    premium_billing_frequency: Optional[str] = Field(None, max_length=20)
    deductible_amount_usd: Optional[Decimal] = None
    coverage_limit_amount_usd: Optional[Decimal] = None
    effective_date: Optional[date] = None
    expiration_date: Optional[date] = None
    agent_name: Optional[str] = None
    agent_phone_number: Optional[str] = None
    agent_email_address: Optional[str] = None
    notes: Optional[str] = None


class InsurancePolicyCreate(InsurancePolicyBase):
    family_id: int
    policy_number: str
    covered_person_ids: List[int] = Field(default_factory=list)
    covered_vehicle_ids: List[int] = Field(default_factory=list)


class InsurancePolicyUpdate(BaseModel):
    policy_type: Optional[str] = None
    carrier_name: Optional[str] = None
    plan_name: Optional[str] = None
    policy_number: Optional[str] = None
    premium_amount_usd: Optional[Decimal] = None
    premium_billing_frequency: Optional[str] = None
    deductible_amount_usd: Optional[Decimal] = None
    coverage_limit_amount_usd: Optional[Decimal] = None
    effective_date: Optional[date] = None
    expiration_date: Optional[date] = None
    agent_name: Optional[str] = None
    agent_phone_number: Optional[str] = None
    agent_email_address: Optional[str] = None
    notes: Optional[str] = None
    covered_person_ids: Optional[List[int]] = None
    covered_vehicle_ids: Optional[List[int]] = None


class InsurancePolicyRead(OrmModel):
    insurance_policy_id: int
    family_id: int
    policy_type: str
    carrier_name: str
    plan_name: Optional[str]
    policy_number_last_four: Optional[str]
    premium_amount_usd: Optional[Decimal]
    premium_billing_frequency: Optional[str]
    deductible_amount_usd: Optional[Decimal]
    coverage_limit_amount_usd: Optional[Decimal]
    effective_date: Optional[date]
    expiration_date: Optional[date]
    agent_name: Optional[str]
    agent_phone_number: Optional[str]
    agent_email_address: Optional[str]
    notes: Optional[str]
    covered_person_ids: List[int] = Field(default_factory=list)
    covered_vehicle_ids: List[int] = Field(default_factory=list)

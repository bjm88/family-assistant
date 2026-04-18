from __future__ import annotations

from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field

from ._base import OrmModel


class FinancialAccountBase(BaseModel):
    account_type: str = Field(..., max_length=40)
    institution_name: str = Field(..., max_length=120)
    account_nickname: Optional[str] = Field(None, max_length=80)
    current_balance_usd: Optional[Decimal] = None
    credit_limit_usd: Optional[Decimal] = None
    online_login_url: Optional[str] = Field(None, max_length=500)
    primary_holder_person_id: Optional[int] = None
    notes: Optional[str] = None


class FinancialAccountCreate(FinancialAccountBase):
    family_id: int
    account_number: str
    routing_number: Optional[str] = None


class FinancialAccountUpdate(BaseModel):
    account_type: Optional[str] = None
    institution_name: Optional[str] = None
    account_nickname: Optional[str] = None
    current_balance_usd: Optional[Decimal] = None
    credit_limit_usd: Optional[Decimal] = None
    online_login_url: Optional[str] = None
    primary_holder_person_id: Optional[int] = None
    notes: Optional[str] = None
    account_number: Optional[str] = None
    routing_number: Optional[str] = None


class FinancialAccountRead(OrmModel):
    financial_account_id: int
    family_id: int
    primary_holder_person_id: Optional[int]
    account_type: str
    institution_name: str
    account_nickname: Optional[str]
    account_number_last_four: Optional[str]
    current_balance_usd: Optional[Decimal]
    credit_limit_usd: Optional[Decimal]
    online_login_url: Optional[str]
    notes: Optional[str]

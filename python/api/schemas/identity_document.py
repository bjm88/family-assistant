from __future__ import annotations

from datetime import date
from typing import Optional

from pydantic import BaseModel, Field

from ._base import OrmModel


class IdentityDocumentBase(BaseModel):
    document_type: str = Field(..., max_length=40)
    issuing_authority: Optional[str] = Field(None, max_length=120)
    country_of_issue: str = Field("United States", max_length=80)
    state_or_region_of_issue: Optional[str] = Field(None, max_length=80)
    issue_date: Optional[date] = None
    expiration_date: Optional[date] = None
    notes: Optional[str] = None


class IdentityDocumentCreate(IdentityDocumentBase):
    person_id: int
    document_number: Optional[str] = Field(
        None,
        description="Plaintext document number. Encrypted at rest; only last_four is returned.",
    )


class IdentityDocumentUpdate(BaseModel):
    document_type: Optional[str] = None
    issuing_authority: Optional[str] = None
    country_of_issue: Optional[str] = None
    state_or_region_of_issue: Optional[str] = None
    issue_date: Optional[date] = None
    expiration_date: Optional[date] = None
    notes: Optional[str] = None
    document_number: Optional[str] = None


class IdentityDocumentRead(OrmModel):
    identity_document_id: int
    person_id: int
    document_type: str
    document_number_last_four: Optional[str]
    issuing_authority: Optional[str]
    country_of_issue: str
    state_or_region_of_issue: Optional[str]
    issue_date: Optional[date]
    expiration_date: Optional[date]
    notes: Optional[str]
    front_image_path: Optional[str] = None
    back_image_path: Optional[str] = None

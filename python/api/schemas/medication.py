from __future__ import annotations

from datetime import date
from typing import Optional

from pydantic import BaseModel, Field, model_validator

from ._base import OrmModel


class MedicationBase(BaseModel):
    ndc_number: Optional[str] = Field(None, max_length=20)
    generic_name: Optional[str] = Field(None, max_length=160)
    brand_name: Optional[str] = Field(None, max_length=160)
    dosage: Optional[str] = Field(None, max_length=120)
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    notes: Optional[str] = None

    @model_validator(mode="after")
    def _at_least_one_identifier(self) -> "MedicationBase":
        # Mirror the database CHECK constraint so the API returns a
        # friendly 422 instead of a 500-from-IntegrityError when the
        # client submits an entirely-blank medication. Subclasses
        # used for PATCH skip this when nothing related is provided.
        if not (self.ndc_number or self.generic_name or self.brand_name):
            raise ValueError(
                "At least one of NDC number, generic name, or brand name "
                "is required so the medication is identifiable."
            )
        return self


class MedicationCreate(MedicationBase):
    person_id: int


class MedicationUpdate(BaseModel):
    """PATCH-style: every field optional, no cross-field validator.

    The database CHECK constraint still guards against ending up in a
    state with all three name columns NULL after an update.
    """

    ndc_number: Optional[str] = Field(None, max_length=20)
    generic_name: Optional[str] = Field(None, max_length=160)
    brand_name: Optional[str] = Field(None, max_length=160)
    dosage: Optional[str] = Field(None, max_length=120)
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    notes: Optional[str] = None


class MedicationRead(OrmModel):
    medication_id: int
    person_id: int
    ndc_number: Optional[str]
    generic_name: Optional[str]
    brand_name: Optional[str]
    dosage: Optional[str]
    start_date: Optional[date]
    end_date: Optional[date]
    notes: Optional[str]

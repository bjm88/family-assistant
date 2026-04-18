from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field

from ._base import OrmModel


class VehicleBase(BaseModel):
    vehicle_type: Optional[str] = Field(None, max_length=40)
    nickname: Optional[str] = Field(None, max_length=60)
    year: Optional[int] = None
    make: str = Field(..., max_length=60)
    model: str = Field(..., max_length=80)
    trim: Optional[str] = Field(None, max_length=60)
    color: Optional[str] = Field(None, max_length=40)
    body_style: Optional[str] = Field(None, max_length=40)
    fuel_type: Optional[str] = Field(None, max_length=40)
    license_plate_state_or_region: Optional[str] = Field(None, max_length=40)
    registration_expiration_date: Optional[date] = None
    purchase_date: Optional[date] = None
    purchase_price_usd: Optional[Decimal] = None
    current_mileage: Optional[int] = None
    primary_driver_person_id: Optional[int] = None
    residence_id: Optional[int] = None
    notes: Optional[str] = None


class VehicleCreate(VehicleBase):
    family_id: int
    vehicle_identification_number: Optional[str] = None
    license_plate_number: Optional[str] = None


class VehicleUpdate(BaseModel):
    vehicle_type: Optional[str] = Field(None, max_length=40)
    nickname: Optional[str] = None
    year: Optional[int] = None
    make: Optional[str] = None
    model: Optional[str] = None
    trim: Optional[str] = None
    color: Optional[str] = None
    body_style: Optional[str] = None
    fuel_type: Optional[str] = None
    license_plate_state_or_region: Optional[str] = None
    registration_expiration_date: Optional[date] = None
    purchase_date: Optional[date] = None
    purchase_price_usd: Optional[Decimal] = None
    current_mileage: Optional[int] = None
    primary_driver_person_id: Optional[int] = None
    residence_id: Optional[int] = None
    notes: Optional[str] = None
    vehicle_identification_number: Optional[str] = None
    license_plate_number: Optional[str] = None


class VehicleRead(OrmModel):
    vehicle_id: int
    family_id: int
    primary_driver_person_id: Optional[int]
    residence_id: Optional[int]
    vehicle_type: str
    nickname: Optional[str]
    year: Optional[int]
    make: str
    model: str
    trim: Optional[str]
    color: Optional[str]
    body_style: Optional[str]
    fuel_type: Optional[str]
    vehicle_identification_number_last_four: Optional[str]
    license_plate_number_last_four: Optional[str]
    license_plate_state_or_region: Optional[str]
    registration_expiration_date: Optional[date]
    purchase_date: Optional[date]
    purchase_price_usd: Optional[Decimal]
    current_mileage: Optional[int]
    profile_image_path: Optional[str]
    notes: Optional[str]

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import crypto, models, schemas, storage
from ..auth import require_admin, require_family_member_from_request
from ..db import get_db

router = APIRouter(prefix="/vehicles", tags=["vehicles"])


@router.get(
    "",
    response_model=List[schemas.VehicleRead],
    dependencies=[Depends(require_family_member_from_request)],
)
def list_vehicles(
    family_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
) -> List[models.Vehicle]:
    stmt = select(models.Vehicle).order_by(models.Vehicle.year.desc().nulls_last())
    if family_id is not None:
        stmt = stmt.where(models.Vehicle.family_id == family_id)
    return list(db.execute(stmt).scalars())


@router.post(
    "",
    response_model=schemas.VehicleRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
def create_vehicle(payload: schemas.VehicleCreate, db: Session = Depends(get_db)) -> models.Vehicle:
    if db.get(models.Family, payload.family_id) is None:
        raise HTTPException(status_code=404, detail="Family not found")
    data = payload.model_dump(
        exclude={"vehicle_identification_number", "license_plate_number"}
    )
    v = models.Vehicle(**data)
    if payload.vehicle_identification_number:
        v.vehicle_identification_number_encrypted = crypto.encrypt_str(
            payload.vehicle_identification_number
        )
        v.vehicle_identification_number_last_four = crypto.last_four(
            payload.vehicle_identification_number
        )
    if payload.license_plate_number:
        v.license_plate_number_encrypted = crypto.encrypt_str(payload.license_plate_number)
        v.license_plate_number_last_four = crypto.last_four(payload.license_plate_number)
    db.add(v)
    db.flush()
    db.refresh(v)
    return v


@router.patch(
    "/{vehicle_id}",
    response_model=schemas.VehicleRead,
    dependencies=[Depends(require_admin)],
)
def update_vehicle(
    vehicle_id: int,
    payload: schemas.VehicleUpdate,
    db: Session = Depends(get_db),
) -> models.Vehicle:
    v = db.get(models.Vehicle, vehicle_id)
    if v is None:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    data = payload.model_dump(exclude_unset=True)
    vin = data.pop("vehicle_identification_number", None)
    plate = data.pop("license_plate_number", None)
    for field, value in data.items():
        setattr(v, field, value)
    if vin is not None:
        if vin == "":
            v.vehicle_identification_number_encrypted = None
            v.vehicle_identification_number_last_four = None
        else:
            v.vehicle_identification_number_encrypted = crypto.encrypt_str(vin)
            v.vehicle_identification_number_last_four = crypto.last_four(vin)
    if plate is not None:
        if plate == "":
            v.license_plate_number_encrypted = None
            v.license_plate_number_last_four = None
        else:
            v.license_plate_number_encrypted = crypto.encrypt_str(plate)
            v.license_plate_number_last_four = crypto.last_four(plate)
    db.flush()
    db.refresh(v)
    return v


@router.delete(
    "/{vehicle_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin)],
)
def delete_vehicle(vehicle_id: int, db: Session = Depends(get_db)) -> None:
    v = db.get(models.Vehicle, vehicle_id)
    if v is None:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    storage.delete_if_exists(v.profile_image_path)
    db.delete(v)


@router.post(
    "/{vehicle_id}/profile-photo",
    response_model=schemas.VehicleRead,
    dependencies=[Depends(require_admin)],
)
def upload_vehicle_profile_photo(
    vehicle_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> models.Vehicle:
    v = db.get(models.Vehicle, vehicle_id)
    if v is None:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    if file.content_type and not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Profile photo must be an image.")
    storage.delete_if_exists(v.profile_image_path)
    rel_path, _ = storage.save_vehicle_profile_photo(
        v.family_id, v.vehicle_id, file.file, file.filename or "vehicle.jpg"
    )
    v.profile_image_path = rel_path
    db.flush()
    db.refresh(v)
    return v


@router.delete(
    "/{vehicle_id}/profile-photo",
    response_model=schemas.VehicleRead,
    dependencies=[Depends(require_admin)],
)
def delete_vehicle_profile_photo(
    vehicle_id: int,
    db: Session = Depends(get_db),
) -> models.Vehicle:
    v = db.get(models.Vehicle, vehicle_id)
    if v is None:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    storage.delete_if_exists(v.profile_image_path)
    v.profile_image_path = None
    db.flush()
    db.refresh(v)
    return v

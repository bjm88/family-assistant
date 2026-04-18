from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db

router = APIRouter(prefix="/api/goals", tags=["goals"])


# Priorities sort urgent → low so the UI can render "what to do next" order.
_PRIORITY_ORDER = {"urgent": 0, "semi_urgent": 1, "normal": 2, "low": 3}


@router.get("", response_model=List[schemas.GoalRead])
def list_goals(
    person_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
) -> List[models.Goal]:
    stmt = select(models.Goal)
    if person_id is not None:
        stmt = stmt.where(models.Goal.person_id == person_id)
    goals = list(db.execute(stmt).scalars())
    goals.sort(
        key=lambda g: (
            _PRIORITY_ORDER.get(g.priority, 99),
            g.start_date or _FAR_FUTURE,
            g.goal_id,
        )
    )
    return goals


@router.post(
    "", response_model=schemas.GoalRead, status_code=status.HTTP_201_CREATED
)
def create_goal(
    payload: schemas.GoalCreate, db: Session = Depends(get_db)
) -> models.Goal:
    if db.get(models.Person, payload.person_id) is None:
        raise HTTPException(status_code=404, detail="Person not found")
    goal = models.Goal(**payload.model_dump())
    db.add(goal)
    db.flush()
    db.refresh(goal)
    return goal


@router.patch("/{goal_id}", response_model=schemas.GoalRead)
def update_goal(
    goal_id: int,
    payload: schemas.GoalUpdate,
    db: Session = Depends(get_db),
) -> models.Goal:
    goal = db.get(models.Goal, goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(goal, field, value)
    db.flush()
    db.refresh(goal)
    return goal


@router.delete("/{goal_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_goal(goal_id: int, db: Session = Depends(get_db)) -> None:
    goal = db.get(models.Goal, goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    db.delete(goal)


# Sentinel for "start date not set" so None sorts after real dates. Using a
# module-level date keeps the list_goals sort key comparable across rows.
from datetime import date as _date_cls

_FAR_FUTURE = _date_cls(9999, 12, 31)

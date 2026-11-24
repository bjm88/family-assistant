from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

from ._base import OrmModel


GoalPriority = Literal["urgent", "semi_urgent", "normal", "low"]


class GoalBase(BaseModel):
    goal_name: str = Field(..., max_length=200)
    description: Optional[str] = None
    start_date: Optional[date] = None
    priority: GoalPriority = "normal"
    completed_date: Optional[date] = None


class GoalCreate(GoalBase):
    person_id: int


class GoalUpdate(BaseModel):
    goal_name: Optional[str] = Field(None, max_length=200)
    description: Optional[str] = None
    start_date: Optional[date] = None
    priority: Optional[GoalPriority] = None
    # Send null to reopen a completed goal.
    completed_date: Optional[date] = None


class GoalRead(OrmModel):
    goal_id: int
    person_id: int
    goal_name: str
    description: Optional[str]
    start_date: Optional[date]
    priority: GoalPriority
    completed_date: Optional[date]
    created_at: datetime
    updated_at: datetime

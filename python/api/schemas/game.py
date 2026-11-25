from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class GameCreate(BaseModel):
    family_id: int
    game_type: str = Field(..., max_length=40)
    invite_person_ids: List[int] = Field(..., min_length=1, max_length=8)
    host_person_id: Optional[int] = Field(
        None,
        description=(
            "Who the game is started as. Only admins may set it (they "
            "aren't tied to a person); a family member always starts a "
            "game as themselves."
        ),
    )
    send_invites: bool = Field(
        True,
        description="Text / email each invitee their link. False = share links by hand.",
    )


class GameInvite(BaseModel):
    person_ids: List[int] = Field(..., min_length=1, max_length=8)
    send_invites: bool = True

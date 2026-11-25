from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class GameCreate(BaseModel):
    family_id: int
    game_type: str = Field(..., max_length=40)
    invite_person_ids: List[int] = Field(
        default_factory=list,
        max_length=8,
        description=(
            "Family members to invite. May be empty: the host can play "
            "the AI, or pass the game's shareable link around instead."
        ),
    )
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
    vs_ai: bool = Field(
        False, description="Give the AI a seat, so the host can play it straight away."
    )
    ai_level: Literal["easy", "medium", "hard"] = Field(
        "medium", description="How well the AI plays when vs_ai is set."
    )


class GameInvite(BaseModel):
    person_ids: List[int] = Field(..., min_length=1, max_length=8)
    send_invites: bool = True


class GameGuestJoin(BaseModel):
    """What someone holding a game's shareable link sends to join it."""

    name: str = Field(..., max_length=200)
    style: str = Field(..., max_length=24)
    color: str = Field(..., max_length=16)

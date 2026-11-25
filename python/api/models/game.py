"""``games`` and ``game_players`` — family games played from a phone.

A game is one sitting of one game type (today only Turkey TicTacToe):
a family member starts it, invites others, and they play one or more
rounds. Each player — the host included — has a ``game_players`` row
carrying a ``join_token``. The token is the whole credential: the link
``<PUBLIC_BASE_URL>/play/<join_token>`` lets its holder play as that
person in that one game without signing in, and gives access to nothing
else.

The board lives in ``games.state`` as JSON so each game type can keep
its own shape; see ``api.games.turkey_tictactoe`` for the only one so
far.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import false as sql_false

from ..db import Base
from ._mixins import TimestampMixin


# Keep in lock-step with the CHECK constraints in
# ``migrations/versions/0015_family_games.py``.
GAME_STATUSES: tuple[str, ...] = ("lobby", "playing", "round_over", "ended")
GAME_INVITE_STATUSES: tuple[str, ...] = ("not_sent", "pending", "sent", "failed")

# How long a game's links keep working after it is started. Long enough
# for an evening of rematches, short enough that an old text message
# doesn't stay a working link for weeks.
GAME_DEFAULT_TTL = timedelta(hours=24)


def generate_join_token(*, n_bytes: int = 24) -> str:
    """URL-safe random secret for a ``/play/<token>`` link (192 bits)."""
    return secrets.token_urlsafe(n_bytes)


class Game(Base, TimestampMixin):
    __tablename__ = "games"
    __table_args__ = (
        CheckConstraint(
            "status IN ('lobby', 'playing', 'round_over', 'ended')",
            name="ck_games_status",
        ),
        Index("ix_games_family_created", "family_id", "created_at"),
        {
            "comment": (
                "One sitting of a family game (e.g. Turkey TicTacToe): "
                "started by one family member, joined by others through "
                "invite links, played for one or more rounds. Who is "
                "playing and how many rounds each has won is in "
                "game_players."
            )
        },
    )

    game_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    family_id: Mapped[int] = mapped_column(
        ForeignKey("families.family_id", ondelete="CASCADE"),
        nullable=False,
        comment="Household the game belongs to. Only its members can be invited.",
    )
    game_type: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        comment="Which game this is, e.g. 'turkey_tictactoe'.",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="lobby",
        server_default="lobby",
        comment=(
            "'lobby' (waiting for players to join), 'playing' (a round is "
            "under way), 'round_over' (a round just finished; players can "
            "play again) or 'ended' (finished for good)."
        ),
    )
    created_by_person_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("people.person_id", ondelete="SET NULL"),
        nullable=True,
        comment="Family member who started the game and invited the others.",
    )
    rounds_played: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="Number of rounds finished so far (wins and draws).",
    )
    state: Mapped[Dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
        comment=(
            "The board and whose turn it is, as JSON. The shape depends on "
            "game_type; empty until the first round starts."
        ),
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="When the players' join links stop working.",
    )
    ended_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When a player ended the game. NULL while it can still be played.",
    )

    players: Mapped[List["GamePlayer"]] = relationship(
        back_populates="game",
        cascade="all, delete-orphan",
        order_by="GamePlayer.game_player_id",
    )
    created_by: Mapped[Optional["Person"]] = relationship(  # noqa: F821
        foreign_keys=[created_by_person_id]
    )


class GamePlayer(Base, TimestampMixin):
    __tablename__ = "game_players"
    __table_args__ = (
        UniqueConstraint("join_token", name="uq_game_players_join_token"),
        UniqueConstraint("game_id", "person_id", name="uq_game_players_game_person"),
        CheckConstraint(
            "invite_status IN ('not_sent', 'pending', 'sent', 'failed')",
            name="ck_game_players_invite_status",
        ),
        Index("ix_game_players_person", "person_id"),
        {
            "comment": (
                "One family member in one game: the host or someone they "
                "invited. Holds the secret link they join with, the turkey "
                "they picked, and how many rounds they have won in this game."
            )
        },
    )

    game_player_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    game_id: Mapped[int] = mapped_column(
        ForeignKey("games.game_id", ondelete="CASCADE"),
        nullable=False,
        comment="Game this seat belongs to.",
    )
    person_id: Mapped[int] = mapped_column(
        ForeignKey("people.person_id", ondelete="CASCADE"),
        nullable=False,
        comment="Family member who plays in this seat.",
    )
    is_host: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=sql_false(),
        comment="True for the person who started the game.",
    )
    join_token: Mapped[str] = mapped_column(
        String(80),
        nullable=False,
        comment=(
            "Secret in this player's /play/<token> link. Whoever holds it "
            "plays as this person in this game, without signing in."
        ),
    )
    joined_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="When they opened their link and joined. NULL = invited, not yet in.",
    )
    avatar_style: Mapped[Optional[str]] = mapped_column(
        String(24),
        nullable=True,
        comment="Turkey they picked: happy, strong, girl, cheerleader, hippie or beer.",
    )
    avatar_color: Mapped[Optional[str]] = mapped_column(
        String(16),
        nullable=True,
        comment="Shirt colour of their turkey; no two players in a game share one.",
    )
    wins: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="Rounds this player has won in this game.",
    )
    invite_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="not_sent",
        server_default="not_sent",
        comment=(
            "'pending' (about to be sent), 'sent' (texted and/or emailed), "
            "'failed' (no channel worked; the host shares the link "
            "themselves) or 'not_sent' (the host, or invites were skipped)."
        ),
    )
    invite_channels: Mapped[Optional[str]] = mapped_column(
        String(40),
        nullable=True,
        comment="How the link was delivered: 'sms', 'email' or 'sms,email'.",
    )
    invite_detail: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Why a text or email could not be sent, when one could not.",
    )

    game: Mapped["Game"] = relationship(back_populates="players")
    person: Mapped["Person"] = relationship()  # noqa: F821

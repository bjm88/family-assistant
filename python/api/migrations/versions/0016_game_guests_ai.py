"""Family games: a shareable invite link, guest players and an AI opponent.

* ``games.invite_token`` — the secret in a game's shareable link,
  ``/play/join/<token>``. Whoever opens it types their name and joins as
  a guest.
* ``game_players.player_name`` — the name of a seat that isn't a family
  member (a guest, or the AI).
* ``game_players.ai_level`` — set on the AI's seat: how well it plays.
* ``game_players.person_id`` becomes nullable, because guests and the
  AI aren't rows in ``people``.

Revision ID: 0016_game_guests_ai
Revises: 0015_family_games
Create Date: 2026-11-25
"""

from __future__ import annotations

import secrets
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# alembic_version.version_num is VARCHAR(32) — keep revision ids short.
revision: str = "0016_game_guests_ai"
down_revision: Union[str, None] = "0015_family_games"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_PLAYERS_COMMENT = (
    "One player in one game: a family member (the host or someone they "
    "invited), a guest who joined through the game's shareable link, or "
    "the AI opponent. Holds the secret link they join with, the turkey "
    "they picked, and how many rounds they have won in this game."
)
_PLAYERS_COMMENT_BEFORE = (
    "One family member in one game: the host or someone they "
    "invited. Holds the secret link they join with, the turkey "
    "they picked, and how many rounds they have won in this game."
)
_PERSON_COMMENT = (
    "Family member who plays in this seat. NULL for a guest and "
    "for the AI, who are named by player_name instead."
)
_PERSON_COMMENT_BEFORE = "Family member who plays in this seat."


def upgrade() -> None:
    # 0001 builds a brand-new database from the current models, so on a
    # fresh install all of this is already in place.
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "invite_token" not in {c["name"] for c in inspector.get_columns("games")}:
        op.add_column(
            "games",
            sa.Column(
                "invite_token",
                sa.String(length=80),
                nullable=True,
                comment=(
                    "Secret in the game's shareable invite link, /play/join/<token>. "
                    "Anyone who opens it types their name and joins as a guest "
                    "while there is room. NULL = the game has no shareable link."
                ),
            ),
        )
        op.create_unique_constraint("uq_games_invite_token", "games", ["invite_token"])
        # Games that are still going get a link too.
        open_games = bind.execute(
            sa.text(
                "SELECT game_id FROM games "
                "WHERE status <> 'ended' AND expires_at > now()"
            )
        ).scalars().all()
        for game_id in open_games:
            bind.execute(
                sa.text("UPDATE games SET invite_token = :token WHERE game_id = :id"),
                {"token": secrets.token_urlsafe(24), "id": game_id},
            )

    if "player_name" not in {c["name"] for c in inspector.get_columns("game_players")}:
        op.add_column(
            "game_players",
            sa.Column(
                "player_name",
                sa.String(length=40),
                nullable=True,
                comment=(
                    "Name shown for a seat that isn't a family member: what a "
                    "guest typed when they joined, or the AI's name (e.g. "
                    "'Avi AI'). NULL for family members."
                ),
            ),
        )
        op.add_column(
            "game_players",
            sa.Column(
                "ai_level",
                sa.String(length=12),
                nullable=True,
                comment=(
                    "Set only for the AI's seat: how well it plays — 'easy', "
                    "'medium' or 'hard'. NULL for people."
                ),
            ),
        )
        op.alter_column(
            "game_players",
            "person_id",
            existing_type=sa.Integer(),
            nullable=True,
            comment=_PERSON_COMMENT,
            existing_comment=_PERSON_COMMENT_BEFORE,
        )
        op.create_check_constraint(
            "ck_game_players_ai_level",
            "game_players",
            "ai_level IS NULL OR ai_level IN ('easy', 'medium', 'hard')",
        )
        op.create_check_constraint(
            "ck_game_players_named",
            "game_players",
            "person_id IS NOT NULL OR player_name IS NOT NULL",
        )
        op.create_table_comment(
            "game_players",
            _PLAYERS_COMMENT,
            existing_comment=_PLAYERS_COMMENT_BEFORE,
        )


def downgrade() -> None:
    # Guests and the AI can't exist once every seat needs a person.
    op.execute("DELETE FROM game_players WHERE person_id IS NULL")
    op.create_table_comment(
        "game_players",
        _PLAYERS_COMMENT_BEFORE,
        existing_comment=_PLAYERS_COMMENT,
    )
    op.drop_constraint("ck_game_players_named", "game_players", type_="check")
    op.drop_constraint("ck_game_players_ai_level", "game_players", type_="check")
    op.alter_column(
        "game_players",
        "person_id",
        existing_type=sa.Integer(),
        nullable=False,
        comment=_PERSON_COMMENT_BEFORE,
        existing_comment=_PERSON_COMMENT,
    )
    op.drop_column("game_players", "ai_level")
    op.drop_column("game_players", "player_name")
    op.drop_constraint("uq_games_invite_token", "games", type_="unique")
    op.drop_column("games", "invite_token")

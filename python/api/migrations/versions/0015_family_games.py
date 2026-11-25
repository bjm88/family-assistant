"""Family games: ``games`` and ``game_players``.

A game is one sitting of a phone game (Turkey TicTacToe is the first).
Each player has a ``game_players`` row with the secret ``join_token``
that their ``/play/<token>`` invite link carries.

Revision ID: 0015_family_games
Revises: 0014_record_attachments
Create Date: 2026-11-25
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


# alembic_version.version_num is VARCHAR(32) — keep revision ids short.
revision: str = "0015_family_games"
down_revision: Union[str, None] = "0014_record_attachments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="Timestamp when the row was first inserted.",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="Timestamp of the most recent update to the row.",
        ),
    ]


def upgrade() -> None:
    # 0001 builds a brand-new database from the current models, so on a
    # fresh install these tables already exist by the time we get here.
    inspector = sa.inspect(op.get_bind())

    if not inspector.has_table("games"):
        op.create_table(
            "games",
            sa.Column("game_id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "family_id",
                sa.Integer(),
                sa.ForeignKey("families.family_id", ondelete="CASCADE"),
                nullable=False,
                comment="Household the game belongs to. Only its members can be invited.",
            ),
            sa.Column(
                "game_type",
                sa.String(length=40),
                nullable=False,
                comment="Which game this is, e.g. 'turkey_tictactoe'.",
            ),
            sa.Column(
                "status",
                sa.String(length=16),
                nullable=False,
                server_default="lobby",
                comment=(
                    "'lobby' (waiting for players to join), 'playing' (a round is "
                    "under way), 'round_over' (a round just finished; players can "
                    "play again) or 'ended' (finished for good)."
                ),
            ),
            sa.Column(
                "created_by_person_id",
                sa.Integer(),
                sa.ForeignKey("people.person_id", ondelete="SET NULL"),
                nullable=True,
                comment="Family member who started the game and invited the others.",
            ),
            sa.Column(
                "rounds_played",
                sa.Integer(),
                nullable=False,
                server_default="0",
                comment="Number of rounds finished so far (wins and draws).",
            ),
            sa.Column(
                "state",
                postgresql.JSONB(),
                nullable=False,
                server_default="{}",
                comment=(
                    "The board and whose turn it is, as JSON. The shape depends on "
                    "game_type; empty until the first round starts."
                ),
            ),
            sa.Column(
                "expires_at",
                sa.DateTime(timezone=True),
                nullable=False,
                comment="When the players' join links stop working.",
            ),
            sa.Column(
                "ended_at",
                sa.DateTime(timezone=True),
                nullable=True,
                comment="When a player ended the game. NULL while it can still be played.",
            ),
            *_timestamps(),
            sa.CheckConstraint(
                "status IN ('lobby', 'playing', 'round_over', 'ended')",
                name="ck_games_status",
            ),
            comment=(
                "One sitting of a family game (e.g. Turkey TicTacToe): "
                "started by one family member, joined by others through "
                "invite links, played for one or more rounds. Who is "
                "playing and how many rounds each has won is in "
                "game_players."
            ),
        )
        op.create_index(
            "ix_games_family_created", "games", ["family_id", "created_at"]
        )

    if not inspector.has_table("game_players"):
        op.create_table(
            "game_players",
            sa.Column(
                "game_player_id", sa.Integer(), primary_key=True, autoincrement=True
            ),
            sa.Column(
                "game_id",
                sa.Integer(),
                sa.ForeignKey("games.game_id", ondelete="CASCADE"),
                nullable=False,
                comment="Game this seat belongs to.",
            ),
            sa.Column(
                "person_id",
                sa.Integer(),
                sa.ForeignKey("people.person_id", ondelete="CASCADE"),
                nullable=False,
                comment="Family member who plays in this seat.",
            ),
            sa.Column(
                "is_host",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
                comment="True for the person who started the game.",
            ),
            sa.Column(
                "join_token",
                sa.String(length=80),
                nullable=False,
                comment=(
                    "Secret in this player's /play/<token> link. Whoever holds it "
                    "plays as this person in this game, without signing in."
                ),
            ),
            sa.Column(
                "joined_at",
                sa.DateTime(timezone=True),
                nullable=True,
                comment="When they opened their link and joined. NULL = invited, not yet in.",
            ),
            sa.Column(
                "avatar_style",
                sa.String(length=24),
                nullable=True,
                comment="Turkey they picked: happy, strong, girl, cheerleader, hippie or beer.",
            ),
            sa.Column(
                "avatar_color",
                sa.String(length=16),
                nullable=True,
                comment="Shirt colour of their turkey; no two players in a game share one.",
            ),
            sa.Column(
                "wins",
                sa.Integer(),
                nullable=False,
                server_default="0",
                comment="Rounds this player has won in this game.",
            ),
            sa.Column(
                "invite_status",
                sa.String(length=16),
                nullable=False,
                server_default="not_sent",
                comment=(
                    "'pending' (about to be sent), 'sent' (texted and/or emailed), "
                    "'failed' (no channel worked; the host shares the link "
                    "themselves) or 'not_sent' (the host, or invites were skipped)."
                ),
            ),
            sa.Column(
                "invite_channels",
                sa.String(length=40),
                nullable=True,
                comment="How the link was delivered: 'sms', 'email' or 'sms,email'.",
            ),
            sa.Column(
                "invite_detail",
                sa.Text(),
                nullable=True,
                comment="Why a text or email could not be sent, when one could not.",
            ),
            *_timestamps(),
            sa.UniqueConstraint("join_token", name="uq_game_players_join_token"),
            sa.UniqueConstraint(
                "game_id", "person_id", name="uq_game_players_game_person"
            ),
            sa.CheckConstraint(
                "invite_status IN ('not_sent', 'pending', 'sent', 'failed')",
                name="ck_game_players_invite_status",
            ),
            comment=(
                "One family member in one game: the host or someone they "
                "invited. Holds the secret link they join with, the turkey "
                "they picked, and how many rounds they have won in this game."
            ),
        )
        op.create_index("ix_game_players_person", "game_players", ["person_id"])


def downgrade() -> None:
    op.drop_index("ix_game_players_person", table_name="game_players")
    op.drop_table("game_players")
    op.drop_index("ix_games_family_created", table_name="games")
    op.drop_table("games")

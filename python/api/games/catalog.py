"""The games on offer and the turkeys players can pick.

Adding a game means adding a :class:`GameType` here with a rules module
that exposes ``new_round`` / ``play`` / ``current_player_id`` (see
:mod:`.turkey_tictactoe`), plus its screen in the React app
(``ui/react/src/games/``).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import ModuleType
from typing import Dict, Tuple

from . import turkey_tictactoe


@dataclass(frozen=True)
class GameType:
    key: str
    name: str
    tagline: str
    min_players: int
    max_players: int
    rules: ModuleType


GAME_TYPES: Dict[str, GameType] = {
    "turkey_tictactoe": GameType(
        key="turkey_tictactoe",
        name="Turkey TicTacToe",
        tagline="Line up four of your turkeys before anyone else does.",
        min_players=2,
        max_players=4,
        rules=turkey_tictactoe,
    ),
}

# The React app draws these; the keys are what is stored in
# ``game_players.avatar_style`` / ``avatar_color``.
AVATAR_STYLES: Tuple[str, ...] = (
    "happy",
    "strong",
    "girl",
    "cheerleader",
    "hippie",
    "beer",
)
AVATAR_COLORS: Tuple[str, ...] = (
    "red",
    "orange",
    "gold",
    "green",
    "teal",
    "blue",
    "purple",
    "pink",
)

"""The games on offer and the characters players can pick.

There are two kinds of game:

* **Turn-based** (Turkey TicTacToe): a rules module that exposes
  ``new_round`` / ``play`` / ``current_player_id`` (see
  :mod:`.turkey_tictactoe`). The whole game is in ``games.state`` and
  is sent to every player after each move. A game the AI can play also
  names a module with ``choose_move`` (see :mod:`.turkey_ai`).
* **Real-time** (Zombie Apocalypse, ``realtime=True``): a rules module
  with a ``Round`` that is run in memory while a level is being played
  (see :mod:`.zombie_apocalypse` and :mod:`.zombie_live`).
  ``games.state`` holds only what lasts between levels.

Either way a game also needs its screen in the React app
(``ui/react/src/games/``).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import ModuleType
from typing import Dict, Optional, Tuple

from . import turkey_ai, turkey_tictactoe, zombie_apocalypse

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
# Who defends the house in Zombie Apocalypse.
SURVIVOR_STYLES: Tuple[str, ...] = (
    "army_guy",
    "army_girl",
    "dad",
    "mom",
    "girl",
    "boy",
    "uncle",
    "grandma",
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


@dataclass(frozen=True)
class GameType:
    key: str
    name: str
    tagline: str
    min_players: int
    max_players: int
    rules: ModuleType
    # Module with ``choose_move(state, player_id, level=...)`` when the
    # game can be played against the AI; ``None`` when it can't.
    ai: Optional[ModuleType] = None
    # What players choose to look like (``game_players.avatar_style``).
    styles: Tuple[str, ...] = AVATAR_STYLES
    # Played live rather than in turns; see the module docstring.
    realtime: bool = False


GAME_TYPES: Dict[str, GameType] = {
    "turkey_tictactoe": GameType(
        key="turkey_tictactoe",
        name="Turkey TicTacToe",
        tagline="Line up four of your turkeys before anyone else does.",
        min_players=2,
        max_players=4,
        rules=turkey_tictactoe,
        ai=turkey_ai,
    ),
    zombie_apocalypse.GAME_KEY: GameType(
        key=zombie_apocalypse.GAME_KEY,
        name="Zombie Apocalypse",
        tagline="Defend the family house until the clock runs out, alone or with a partner.",
        min_players=1,
        max_players=2,
        rules=zombie_apocalypse,
        styles=SURVIVOR_STYLES,
        realtime=True,
    ),
}

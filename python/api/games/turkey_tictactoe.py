"""Rules of Turkey TicTacToe.

Tic-tac-toe on a tall board — 5 columns by 10 rows, sized for a phone
held upright — where the first player with four of their turkeys in a
row (across, down or diagonally) wins. Two to four players take turns
placing one turkey on any empty square.

Everything here is a pure function of the round's ``state`` dict, which
is what ``games.state`` stores:

``cells``
    ``rows * cols`` entries, row by row from the top-left. Each is the
    ``game_player_id`` of the turkey on that square, or ``None``.
``turn_order`` / ``turn_index``
    The players in this round and whose turn it is.
``winner_player_id`` / ``winning_cells`` / ``is_draw``
    Set when the round is over.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

ROWS = 10
COLS = 5
WIN_LENGTH = 4

# Across, down, and the two diagonals. Each is checked in both
# directions from the square just played.
_DIRECTIONS = ((0, 1), (1, 0), (1, 1), (1, -1))


class RuleError(Exception):
    """A move the rules don't allow. The message is shown to the player."""


def new_round(player_ids: Sequence[int], *, round_number: int) -> Dict[str, Any]:
    """An empty board for ``player_ids``, who play in the order given.

    Who goes first rotates each round, so the same person doesn't open
    every game of a rematch.
    """
    if len(player_ids) < 2:
        raise RuleError("It takes at least two players to start.")
    order = list(player_ids)
    return {
        "rows": ROWS,
        "cols": COLS,
        "win_length": WIN_LENGTH,
        "cells": [None] * (ROWS * COLS),
        "turn_order": order,
        "turn_index": (round_number - 1) % len(order),
        "round": round_number,
        "winner_player_id": None,
        "winning_cells": [],
        "is_draw": False,
        "last_move": None,
    }


def is_over(state: Dict[str, Any]) -> bool:
    return state.get("winner_player_id") is not None or bool(state.get("is_draw"))


def current_player_id(state: Dict[str, Any]) -> Optional[int]:
    """Whose turn it is, or ``None`` when the round is over or not begun."""
    order = state.get("turn_order") or []
    if not order or is_over(state):
        return None
    return order[state["turn_index"] % len(order)]


def play(state: Dict[str, Any], player_id: int, cell: int) -> Dict[str, Any]:
    """Place ``player_id``'s turkey on ``cell``; return the new state.

    ``state`` is not modified. Raises :class:`RuleError` when it isn't
    this player's turn or the square can't be played.
    """
    if not state.get("cells"):
        raise RuleError("The game hasn't started yet.")
    if is_over(state):
        raise RuleError("This round is over.")
    if player_id not in state["turn_order"]:
        raise RuleError("You're not in this round — you'll be in the next one.")
    if current_player_id(state) != player_id:
        raise RuleError("It isn't your turn yet.")
    cells: List[Optional[int]] = list(state["cells"])
    if isinstance(cell, bool) or not isinstance(cell, int) or not 0 <= cell < len(cells):
        raise RuleError("That square isn't on the board.")
    if cells[cell] is not None:
        raise RuleError("That square is already taken.")

    cells[cell] = player_id
    new_state = {**state, "cells": cells, "last_move": cell}
    line = winning_line(cells, cell, rows=state["rows"], cols=state["cols"])
    if line:
        new_state["winner_player_id"] = player_id
        new_state["winning_cells"] = line
    elif all(c is not None for c in cells):
        new_state["is_draw"] = True
    else:
        new_state["turn_index"] = (state["turn_index"] + 1) % len(state["turn_order"])
    return new_state


def winning_line(
    cells: Sequence[Optional[int]],
    cell: int,
    *,
    rows: int = ROWS,
    cols: int = COLS,
    win_length: int = WIN_LENGTH,
) -> List[int]:
    """The run of same-owner squares through ``cell`` that wins, if any.

    Returns the squares in order from one end of the line to the other
    (so a line can be drawn through them), or ``[]``. When the move
    completes more than one line, the longest is returned.
    """
    owner = cells[cell]
    if owner is None:
        return []
    row, col = divmod(cell, cols)
    best: List[int] = []
    for d_row, d_col in _DIRECTIONS:
        run = [cell]
        for sign in (-1, 1):
            r, c = row + sign * d_row, col + sign * d_col
            while 0 <= r < rows and 0 <= c < cols and cells[r * cols + c] == owner:
                if sign < 0:
                    run.insert(0, r * cols + c)
                else:
                    run.append(r * cols + c)
                r, c = r + sign * d_row, c + sign * d_col
        if len(run) >= win_length and len(run) > len(best):
            best = run
    return best

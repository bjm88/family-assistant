"""Rules of Chinese Checkers.

The traditional star board: 121 holes, six points of ten holes each.
Two to six players start with ten marbles filling a point and race to
fill the point opposite. On a turn a player moves one marble, either

* one step to an empty hole next to it, or
* a chain of one or more hops, each over a single marble next to it
  (anyone's) into the empty hole straight beyond. Nothing is captured.

A marble may go anywhere on the board, except that once it is in the
point it is heading for it stays in that point. The first player with
all ten marbles there wins the round.

Someone else's marble sitting in that point doesn't stop a player
winning (the usual "anti-spoiling" rule): the round is won as soon as
every hole of the point is full and at least one of the marbles in it
is the player's own. Without it a marble left behind at home, or
trapped there by the marbles arriving, would leave the player opposite
with a round nobody can win.

The board
---------
Holes are named by cube coordinates ``(x, y, z)`` with ``x + y + z ==
0``; the star is every hole where all three are at most 4, or all three
are at least -4 (two big triangles laid over each other). A hole is
stored as ``(x, z)`` and drawn at ``(x + z / 2, z * sqrt(3) / 2)``, so
``z`` is the row, top to bottom. Holes are numbered row by row from the
top, left to right. The points are numbered 0 to 5 clockwise from the
top one; point ``p`` is opposite point ``(p + 3) % 6``.

``games.state`` for a round:

``marbles``
    121 entries, one per hole: the ``game_player_id`` of the marble in
    it, or ``None``.
``turn_order`` / ``turn_index``
    The players in this round and whose turn it is.
``homes``
    The point each player started in, in the order of ``turn_order``.
``last_move``
    ``{"player_id", "path", "seq"}`` for the move just made: every hole
    the marble touched, in order, and the number of the move. The
    phones play it back hop by hop.
``winner_player_id`` / ``is_draw``
    Set when the round is over.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .turkey_tictactoe import RuleError

GAME_KEY = "chinese_checkers"
MIN_PLAYERS = 2
MAX_PLAYERS = 6
# Holes along the edge of a point; a point holds 1 + 2 + 3 + 4 marbles.
SIZE = 4
PIECES = SIZE * (SIZE + 1) // 2

# Which points are used, clockwise, for each number of players. Two and
# four sit opposite each other; three sit in every other point, facing
# an empty one.
SEATING: Dict[int, Tuple[int, ...]] = {
    2: (0, 3),
    3: (0, 2, 4),
    4: (0, 1, 3, 4),
    5: (0, 1, 2, 3, 4),
    6: (0, 1, 2, 3, 4, 5),
}

# The six ways from a hole to the next one, as (dx, dz).
_DIRECTIONS: Tuple[Tuple[int, int], ...] = (
    (1, 0), (-1, 0), (0, 1), (0, -1), (1, -1), (-1, 1),
)


def _build_board():
    reach = 2 * SIZE
    holes: List[Tuple[int, int]] = []
    for z in range(-reach, reach + 1):
        for x in range(-reach, reach + 1):
            y = -x - z
            if max(x, y, z) <= SIZE or min(x, y, z) >= -SIZE:
                holes.append((x, z))
    index = {hole: i for i, hole in enumerate(holes)}
    neighbours = tuple(
        tuple(index.get((x + dx, z + dz), -1) for dx, dz in _DIRECTIONS)
        for x, z in holes
    )
    # Clockwise from the top, as drawn.
    in_point = (
        lambda x, y, z: z < -SIZE,
        lambda x, y, z: x > SIZE,
        lambda x, y, z: y < -SIZE,
        lambda x, y, z: z > SIZE,
        lambda x, y, z: x < -SIZE,
        lambda x, y, z: y > SIZE,
    )
    points = tuple(
        tuple(i for i, (x, z) in enumerate(holes) if test(x, -x - z, z))
        for test in in_point
    )
    point_of = [-1] * len(holes)
    for point, members in enumerate(points):
        for hole in members:
            point_of[hole] = point
    return tuple(holes), neighbours, points, tuple(point_of)


HOLES, NEIGHBOURS, POINTS, POINT_OF = _build_board()
HOLE_COUNT = len(HOLES)

_LAYOUT: Dict[str, Any] = {
    "holes": [list(hole) for hole in HOLES],
    "points": [list(point) for point in POINTS],
    "pieces": PIECES,
}


def layout() -> Dict[str, Any]:
    """The board as the phones draw it: where each hole is, and the points."""
    return _LAYOUT


def opposite(point: int) -> int:
    return (point + 3) % 6


def new_round(player_ids: Sequence[int], *, round_number: int) -> Dict[str, Any]:
    """Every player's marbles in their point; who opens rotates each round."""
    if len(player_ids) < MIN_PLAYERS:
        raise RuleError("It takes at least two players to start.")
    if len(player_ids) > MAX_PLAYERS:
        raise RuleError(f"Chinese Checkers is for up to {MAX_PLAYERS} players.")
    order = list(player_ids)
    homes = list(SEATING[len(order)])
    marbles: List[Optional[int]] = [None] * HOLE_COUNT
    for player_id, home in zip(order, homes):
        for hole in POINTS[home]:
            marbles[hole] = player_id
    return {
        "marbles": marbles,
        "turn_order": order,
        "turn_index": (round_number - 1) % len(order),
        "homes": homes,
        "round": round_number,
        "winner_player_id": None,
        "is_draw": False,
        "last_move": None,
        "moves": 0,
    }


def is_over(state: Dict[str, Any]) -> bool:
    return state.get("winner_player_id") is not None or bool(state.get("is_draw"))


def current_player_id(state: Dict[str, Any]) -> Optional[int]:
    """Whose turn it is, or ``None`` when the round is over or not begun."""
    order = state.get("turn_order") or []
    if not order or is_over(state):
        return None
    return order[state["turn_index"] % len(order)]


def target_of(state: Dict[str, Any], player_id: int) -> int:
    """The point ``player_id`` is trying to fill."""
    return opposite(state["homes"][state["turn_order"].index(player_id)])


def _jump(marbles: Sequence[Optional[int]], start: int, direction: int) -> int:
    """Where a hop from ``start`` lands going ``direction``, or -1."""
    over = NEIGHBOURS[start][direction]
    if over < 0 or marbles[over] is None:
        return -1
    landing = NEIGHBOURS[over][direction]
    if landing < 0 or marbles[landing] is not None:
        return -1
    return landing


def reachable(
    marbles: Sequence[Optional[int]], hole: int, target: int
) -> Dict[int, List[int]]:
    """Every hole the marble in ``hole`` can end a move on.

    Maps each to the shortest path there (``[hole, ..., destination]``).
    ``target`` is the point the marble's owner is filling: a marble
    already in it only has moves that stay in it.
    """
    locked = POINT_OF[hole] == target
    allowed = lambda at: not locked or POINT_OF[at] == target  # noqa: E731
    found: Dict[int, List[int]] = {}
    for nearby in NEIGHBOURS[hole]:
        if nearby >= 0 and marbles[nearby] is None and allowed(nearby):
            found[nearby] = [hole, nearby]
    # Hops, with the marble lifted off the board so it can't hop itself.
    board = list(marbles)
    board[hole] = None
    hopped: Dict[int, List[int]] = {hole: [hole]}
    queue = deque([hole])
    while queue:
        at = queue.popleft()
        for direction in range(len(_DIRECTIONS)):
            landing = _jump(board, at, direction)
            if landing < 0 or landing in hopped or not allowed(landing):
                continue
            hopped[landing] = hopped[at] + [landing]
            queue.append(landing)
    del hopped[hole]
    for landing, path in hopped.items():
        found.setdefault(landing, path)
    return found


def can_move(state: Dict[str, Any], player_id: int) -> bool:
    marbles = state["marbles"]
    target = target_of(state, player_id)
    return any(
        owner == player_id and reachable(marbles, hole, target)
        for hole, owner in enumerate(marbles)
    )


def has_won(state: Dict[str, Any], player_id: int) -> bool:
    """Whether the point ``player_id`` is heading for is full, with at
    least one of their own marbles in it (see the module docstring)."""
    marbles = state["marbles"]
    there = [marbles[hole] for hole in POINTS[target_of(state, player_id)]]
    return all(owner is not None for owner in there) and player_id in there


def _clean_path(path: Any) -> List[int]:
    if (
        not isinstance(path, (list, tuple))
        or not 2 <= len(path) <= HOLE_COUNT
        or any(
            isinstance(hole, bool) or not isinstance(hole, int) or not 0 <= hole < HOLE_COUNT
            for hole in path
        )
    ):
        raise RuleError("That move isn't on the board.")
    return list(path)


def play(state: Dict[str, Any], player_id: int, path: Any) -> Dict[str, Any]:
    """Move ``player_id``'s marble along ``path``; return the new state.

    ``path`` is every hole the marble touches: where it starts, then
    where it lands after the step or after each hop. ``state`` is not
    modified. Raises :class:`RuleError` when the move isn't allowed.
    """
    if not state.get("marbles"):
        raise RuleError("The game hasn't started yet.")
    if is_over(state):
        raise RuleError("This round is over.")
    if player_id not in state["turn_order"]:
        raise RuleError("You're not in this round — you'll be in the next one.")
    if current_player_id(state) != player_id:
        raise RuleError("It isn't your turn yet.")
    path = _clean_path(path)
    marbles: List[Optional[int]] = list(state["marbles"])
    start = path[0]
    if marbles[start] != player_id:
        raise RuleError("Move one of your own marbles.")
    if len(set(path)) != len(path):
        raise RuleError("A marble can't land in the same hole twice in one move.")
    if any(marbles[hole] is not None for hole in path[1:]):
        raise RuleError("That hole is taken.")
    target = target_of(state, player_id)
    if POINT_OF[start] == target and any(POINT_OF[hole] != target for hole in path[1:]):
        raise RuleError("A marble that has reached the far point stays in it.")

    marbles[start] = None
    is_step = len(path) == 2 and path[1] in NEIGHBOURS[start]
    if not is_step:
        for at, landing in zip(path, path[1:]):
            if not any(
                _jump(marbles, at, direction) == landing
                for direction in range(len(_DIRECTIONS))
            ):
                raise RuleError(
                    "A marble moves one hole, or hops over one marble at a time."
                )
    marbles[path[-1]] = player_id

    moves = int(state.get("moves") or 0) + 1
    new_state = {
        **state,
        "marbles": marbles,
        "moves": moves,
        "last_move": {"player_id": player_id, "path": path, "seq": moves},
    }
    order = state["turn_order"]
    # The mover first: a move that fills two players' points at once is
    # their win. (It can only fill someone else's by landing in it.)
    mover = order.index(player_id)
    for step in range(len(order)):
        candidate = order[(mover + step) % len(order)]
        if has_won(new_state, candidate):
            new_state["winner_player_id"] = candidate
            return new_state
    # On to the next player who has a move. Someone completely hemmed
    # in is passed over, which hardly ever happens.
    for step in range(1, len(order) + 1):
        index = (state["turn_index"] + step) % len(order)
        if can_move(new_state, order[index]):
            new_state["turn_index"] = index
            break
    else:
        new_state["is_draw"] = True
    return new_state

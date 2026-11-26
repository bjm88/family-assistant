"""How the AI opponent picks its move in Chinese Checkers.

:func:`choose_move` plays the way a person does who knows the game but
isn't counting everything out. Each turn it

1. finishes the round if one move fills its far point;
2. looks over the moves it has: every marble, every step, every chain
   of hops;
3. weighs each by how far it carries the marble towards the tip of the
   far point, with a little extra for bringing up a marble from the
   back, and
4. for the most promising few, looks one move further: what the move
   sets up for its own next turn, and what it hands the player who
   moves next.

A :class:`Level` says how well it does each of those. The mistakes are
the ones people make at this game: a long chain of hops is easy to
lose track of part way along, a marble off to one side gets
overlooked, and what a move is worth is judged by eye. ``hard`` follows
every chain to its end and thinks about the move after. ``medium``, the
default, plays a sound game and takes most of its chances, but now and
then stops a chain short, misses a marble or picks the second-best
move, which is how you beat it. ``easy`` mostly just moves forward.

The function is pure apart from the dice (pass ``rng`` to fix them).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .chinese_checkers import (
    HOLES,
    POINT_OF,
    POINTS,
    current_player_id,
    reachable,
    target_of,
)
from .turkey_tictactoe import RuleError

DEFAULT_LEVEL = "medium"


@dataclass(frozen=True)
class Level:
    # Chance of noticing the move that wins the round.
    sees_win: float
    # Chance of keeping track of each hop of a chain after the first: a
    # chain of four hops is seen to its end with ``follows ** 3``. A
    # chain it loses track of can still be played as far as it got.
    follows: float
    # Chance, per marble per turn, of looking at that marble at all.
    notices: float
    # How far the worth of a move may be misjudged, in holes either
    # way. Also keeps it from playing the same game twice.
    wobble: float
    # How much it cares what the move leaves for its own next turn ...
    foresight: float
    # ... and what it leaves for the player who moves next.
    wary: float
    # Extra worth, per hole of distance still to go, for moving a
    # marble that is far behind rather than one out in front.
    rearguard: float


LEVELS: Dict[str, Level] = {
    "easy": Level(
        sees_win=0.9,
        follows=0.65,
        notices=0.8,
        wobble=1.2,
        foresight=0.0,
        wary=0.0,
        rearguard=0.01,
    ),
    "medium": Level(
        sees_win=1.0,
        follows=0.88,
        notices=0.93,
        wobble=0.6,
        foresight=0.35,
        wary=0.0,
        rearguard=0.03,
    ),
    "hard": Level(
        sees_win=1.0,
        follows=1.0,
        notices=1.0,
        wobble=0.1,
        foresight=0.5,
        wary=0.3,
        rearguard=0.05,
    ),
}

# How many of the best-looking moves get the second look (step 4).
_SHORTLIST = 10


def _xy(hole: int) -> Tuple[float, float]:
    x, z = HOLES[hole]
    return x + z / 2, z * math.sqrt(3) / 2


def _distances() -> Tuple[Tuple[float, ...], ...]:
    """For each point, how far every hole is from its tip, in holes."""
    table = []
    for point in POINTS:
        tip = _xy(max(point, key=lambda hole: math.hypot(*_xy(hole))))
        table.append(
            tuple(
                math.hypot(_xy(hole)[0] - tip[0], _xy(hole)[1] - tip[1])
                for hole in range(len(HOLES))
            )
        )
    return tuple(table)


_AWAY = _distances()


def _moves(
    marbles: Sequence[Optional[int]], player_id: int, target: int
) -> List[List[int]]:
    """Every move ``player_id`` has, as the path the marble takes."""
    return [
        path
        for hole, owner in enumerate(marbles)
        if owner == player_id
        for path in reachable(marbles, hole, target).values()
    ]


def _best_gain(marbles: Sequence[Optional[int]], player_id: int, target: int) -> float:
    """The most ground ``player_id`` could make with one move."""
    away = _AWAY[target]
    return max(
        (away[path[0]] - away[path[-1]] for path in _moves(marbles, player_id, target)),
        default=0.0,
    )


def choose_move(
    state: Dict[str, Any],
    player_id: int,
    *,
    level: Optional[str] = DEFAULT_LEVEL,
    rng: Optional[random.Random] = None,
) -> List[int]:
    """The move ``player_id`` makes on this board at this ``level``: the
    hole the marble starts in, then every hole it lands in."""
    skill = LEVELS.get(level or DEFAULT_LEVEL, LEVELS[DEFAULT_LEVEL])
    dice = rng or random
    marbles: List[Optional[int]] = list(state.get("marbles") or [])
    order: List[int] = list(state.get("turn_order") or [])
    if player_id not in order or current_player_id(state) is None:
        raise RuleError("There's no move to make.")
    target = target_of(state, player_id)
    away = _AWAY[target]
    moves = _moves(marbles, player_id, target)
    if not moves:
        raise RuleError("There's no move to make.")

    # 1. Finish: the far point has one hole left and a marble can get to it.
    gaps = [hole for hole in POINTS[target] if marbles[hole] is None]
    if len(gaps) == 1:
        finishing = [
            path for path in moves if path[-1] == gaps[0] and POINT_OF[path[0]] != target
        ]
        if finishing and dice.random() < skill.sees_win:
            return min(finishing, key=len)

    # 2. What it sees this turn.
    overlooked = {
        hole
        for hole, owner in enumerate(marbles)
        if owner == player_id and dice.random() >= skill.notices
    }
    seen = [
        path
        for path in moves
        if path[0] not in overlooked
        and (len(path) <= 2 or dice.random() < skill.follows ** (len(path) - 2))
    ]
    if not seen:
        seen = moves

    # 3. What each is worth on the face of it.
    def worth(path: List[int]) -> float:
        start = away[path[0]]
        return start - away[path[-1]] + skill.rearguard * start

    ranked = sorted(seen, key=worth, reverse=True)

    # 4. A second look at the best of them.
    thinks_ahead = skill.foresight > 0 or skill.wary > 0
    if thinks_ahead:
        ranked = ranked[:_SHORTLIST]
    at = order.index(player_id)
    after_me = order[(at + 1) % len(order)]
    their_target = target_of(state, after_me)

    best_value = float("-inf")
    best: List[int] = ranked[0]
    for path in ranked:
        value = worth(path) + dice.uniform(-skill.wobble, skill.wobble)
        if thinks_ahead:
            marbles[path[0]], marbles[path[-1]] = None, player_id
            if skill.foresight > 0:
                value += skill.foresight * _best_gain(marbles, player_id, target)
            if skill.wary > 0 and after_me != player_id:
                value -= skill.wary * _best_gain(marbles, after_me, their_target)
            marbles[path[0]], marbles[path[-1]] = player_id, None
        if value > best_value:
            best_value, best = value, path
    return list(best)

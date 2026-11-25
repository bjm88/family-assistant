"""How the AI opponent picks its square in Turkey TicTacToe.

:func:`choose_move` looks at the board the way a person playing
casually does — one move ahead, in this order:

1. If it can make four in a row, it does.
2. If an opponent is one square from four in a row, it takes that
   square.
3. If one move would give it two ways to win at once (the other
   players can only block one), it plays it — and it takes a square
   where an opponent could do the same.
4. Otherwise it plays the square that does the most for its own lines
   and gets in the way of everyone else's.

A :class:`Level` says how reliably it does each of those. The mistakes
are the ones people make: each turn it looks along every row, column
and diagonal with some amount of attention, and a line it only glances
at is a line where it doesn't register what an opponent is building —
so it neither blocks there nor counts that line when weighing a move.
``hard`` reads every line. ``medium``, the default, always takes its
own win and nearly always sees three in a row, but now and then lets a
pair grow or misses a diagonal, which is how you beat it. ``easy``
misses a lot.

The function is pure apart from the dice (pass ``rng`` to fix them).
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .turkey_tictactoe import RuleError

DEFAULT_LEVEL = "medium"


@dataclass(frozen=True)
class Level:
    # Chance of noticing a square that completes its own four in a row.
    sees_win: float
    # Chance, per line per turn, of registering an opponent's three in
    # a row on it (one square from winning) ...
    sees_threat: float
    # ... and an opponent's two in a row on it (a move from becoming a
    # threat). Never more than ``sees_threat``: a line it reads well
    # enough to spot a pair is one where it also spots three.
    sees_setup: float
    # Chance of noticing a move that gives itself two ways to win.
    sees_own_setup: float
    # Multiplies ``sees_threat`` / ``sees_setup`` on diagonal lines,
    # which are the easiest to overlook.
    diagonal_sight: float
    # How much weight it gives to spoiling other players' lines
    # compared with building its own.
    caution: float
    # How far the value of an ordinary move may be misjudged (0.3 = by
    # up to 30% either way). Also keeps it from playing the same game twice.
    wobble: float


LEVELS: Dict[str, Level] = {
    "easy": Level(
        sees_win=0.9,
        sees_threat=0.65,
        sees_setup=0.25,
        sees_own_setup=0.1,
        diagonal_sight=0.7,
        caution=0.6,
        wobble=0.6,
    ),
    "medium": Level(
        sees_win=1.0,
        sees_threat=0.93,
        sees_setup=0.8,
        sees_own_setup=0.6,
        diagonal_sight=0.85,
        caution=0.85,
        wobble=0.3,
    ),
    "hard": Level(
        sees_win=1.0,
        sees_threat=1.0,
        sees_setup=1.0,
        sees_own_setup=1.0,
        diagonal_sight=1.0,
        caution=1.0,
        wobble=0.05,
    ),
}

# What an ordinary move is worth, summed over every run of four squares
# that passes through it:
_UNTOUCHED = 1.0           # a run nobody has played on yet
_MINE = (0.0, 5.0, 24.0)   # a run holding 1 / 2 of its own turkeys and nobody else's
_THEIRS = (0.0, 4.0, 16.0)  # a run holding 1 / 2 of one opponent's and nobody else's
# Setting up two ways to win beats any ordinary move; stopping an
# opponent from doing it comes right behind.
_SETUP = 10_000.0
_STOP_SETUP = 5_000.0

_DIRECTIONS = ((0, 1), (1, 0), (1, 1), (1, -1))


@dataclass(frozen=True)
class Run:
    """Four squares in a straight line: one way to win."""

    squares: Tuple[int, ...]
    diagonal: bool
    # Which row, column or diagonal of the board the run lies on. Runs
    # that overlap along the same line share it.
    line: Tuple[int, int]


# Says whether the AI registers an opponent's run holding ``count`` turkeys.
Reads = Callable[[Run, int], bool]


def _reads_everything(_run: Run, _count: int) -> bool:
    return True


@lru_cache(maxsize=8)
def _runs(rows: int, cols: int, length: int) -> Tuple[Run, ...]:
    """Every run of ``length`` squares in a straight line on the board."""
    out: List[Run] = []
    for direction, (d_row, d_col) in enumerate(_DIRECTIONS):
        for row in range(rows):
            for col in range(cols):
                end_row = row + (length - 1) * d_row
                end_col = col + (length - 1) * d_col
                if not (0 <= end_row < rows and 0 <= end_col < cols):
                    continue
                squares = tuple(
                    (row + i * d_row) * cols + col + i * d_col for i in range(length)
                )
                # Constant along the direction of travel.
                line = (direction, row * d_col - col * d_row)
                out.append(Run(squares, d_row != 0 and d_col != 0, line))
    return tuple(out)


@lru_cache(maxsize=8)
def _runs_through(rows: int, cols: int, length: int) -> Tuple[Tuple[Run, ...], ...]:
    """For each square, the runs that pass through it."""
    through: List[List[Run]] = [[] for _ in range(rows * cols)]
    for run in _runs(rows, cols, length):
        for square in run.squares:
            through[square].append(run)
    return tuple(tuple(runs) for runs in through)


def _winning_squares(
    cells: Sequence[Optional[int]],
    player: int,
    runs: Sequence[Run],
    reads: Reads = _reads_everything,
    *,
    holding: int = 3,
) -> List[int]:
    """Empty squares where ``player`` would complete a run, in board order.

    ``holding`` is how many of the player's turkeys such a run holds as
    the AI looks at the board (one fewer when the last was only placed
    in its imagination); a run the AI doesn't read is passed over.
    """
    found = set()
    for run in runs:
        gap: Optional[int] = None
        for square in run.squares:
            owner = cells[square]
            if owner == player:
                continue
            if owner is not None or gap is not None:
                break
            gap = square
        else:
            if gap is not None and reads(run, holding):
                found.add(gap)
    return sorted(found)


def _sets_up(
    cells: List[Optional[int]],
    square: int,
    player: int,
    already_winning: Sequence[int],
    through: Sequence[Sequence[Run]],
    reads: Reads = _reads_everything,
) -> bool:
    """Would ``player`` have two or more ways to win after playing ``square``?"""
    cells[square] = player
    try:
        ways = set(
            _winning_squares(cells, player, through[square], reads, holding=2)
        )
    finally:
        cells[square] = None
    ways.update(other for other in already_winning if other != square)
    return len(ways) >= 2


def _ordinary_value(
    cells: Sequence[Optional[int]],
    player: int,
    runs: Sequence[Run],
    caution: float,
    reads: Reads,
) -> float:
    value = 0.0
    for run in runs:
        owner: Optional[int] = None
        count = 0
        contested = False
        for other in run.squares:
            who = cells[other]
            if who is None:
                continue
            if owner is None:
                owner = who
            elif who != owner:
                contested = True
                break
            count += 1
        if contested:
            continue
        if owner is None:
            value += _UNTOUCHED
        elif owner == player:
            value += _MINE[min(count, 2)]
        elif reads(run, count):
            value += _THEIRS[min(count, 2)] * caution
    return value


def choose_move(
    state: Dict[str, Any],
    player_id: int,
    *,
    level: Optional[str] = DEFAULT_LEVEL,
    rng: Optional[random.Random] = None,
) -> int:
    """The square ``player_id`` plays on this board at this ``level``."""
    skill = LEVELS.get(level or DEFAULT_LEVEL, LEVELS[DEFAULT_LEVEL])
    dice = rng or random
    cells: List[Optional[int]] = list(state.get("cells") or [])
    empty = [i for i, owner in enumerate(cells) if owner is None]
    if not empty:
        raise RuleError("There's nowhere left to play.")
    rows, cols, length = state["rows"], state["cols"], state["win_length"]
    runs = _runs(rows, cols, length)
    through = _runs_through(rows, cols, length)

    # How closely it looks along each line this turn: 0 is a hard
    # stare, 1 a glance. Decided once per line, the first time it matters.
    glance: Dict[Tuple[int, int], float] = {}

    def reads(run: Run, count: int) -> bool:
        if count <= 1:
            return True
        chance = skill.sees_threat if count >= 3 else skill.sees_setup
        if run.diagonal:
            chance *= skill.diagonal_sight
        if run.line not in glance:
            glance[run.line] = dice.random()
        return glance[run.line] < chance

    # 1. Win.
    my_wins = _winning_squares(cells, player_id, runs)
    noticed = [square for square in my_wins if dice.random() < skill.sees_win]
    if noticed:
        return dice.choice(noticed)

    # 2. Block. The opponents, starting with whoever plays next.
    order: List[int] = list(state.get("turn_order") or [])
    if player_id in order:
        at = order.index(player_id)
        order = order[at + 1 :] + order[:at]
    their_wins = {
        opponent: _winning_squares(cells, opponent, runs, reads) for opponent in order
    }
    candidates = empty
    blocking = False
    for opponent in order:
        if their_wins[opponent]:
            # Only one can be stopped; the choice between them is made below.
            candidates, blocking = their_wins[opponent], True
            break

    # 3 and 4. Set up two ways to win, stop someone else's, or just
    # make the most useful ordinary move.
    best_value = float("-inf")
    best: List[int] = []
    for square in candidates:
        value = _ordinary_value(
            cells, player_id, through[square], skill.caution, reads
        ) * (1.0 + dice.uniform(-skill.wobble, skill.wobble))
        if not blocking:
            if (
                _sets_up(cells, square, player_id, my_wins, through)
                and dice.random() < skill.sees_own_setup
            ):
                value += _SETUP
            if any(
                _sets_up(cells, square, opponent, their_wins[opponent], through, reads)
                for opponent in order
            ):
                value += _STOP_SETUP
        if value > best_value:
            best_value, best = value, [square]
        elif value == best_value:
            best.append(square)
    return dice.choice(best)

"""Getting the AI to take its turns.

The AI has no phone and no socket; this is what moves for it. After
anything that could leave it the AI's turn — a person's move, a round
being dealt, a player (re)connecting after a restart — the router calls
:func:`nudge`. If the AI is up it "thinks" for a moment, so its turkey
doesn't land in the same instant as the player's, then plays through
the hub like anyone else, which pushes the new board to every phone.
In a game where the phones play each move back (a marble hopping across
the Chinese Checkers board), it first waits for that to finish.

At most one task runs per game. A nudge that arrives while one is
running makes it look again before it stops, so a move made in that
instant can't be missed.
"""

from __future__ import annotations

import asyncio
import logging
import random
from functools import partial
from typing import Optional, Set, Tuple

from starlette.concurrency import run_in_threadpool

from ..db import session_scope
from . import service
from .hub import hub

logger = logging.getLogger(__name__)

# How long the AI appears to think, in seconds: (shortest, longest).
THINK_SECONDS: Tuple[float, float] = (0.7, 1.5)

_running: Set[int] = set()        # games with a task taking the AI's turns
_look_again: Set[int] = set()     # games nudged while their task was running
_tasks: Set["asyncio.Task[None]"] = set()  # keeps the tasks from being collected


def nudge(game_id: int) -> None:
    """Have the AI move if it is its turn. Cheap and safe to call any time."""
    if game_id in _running:
        _look_again.add(game_id)
        return
    _running.add(game_id)
    task = asyncio.get_running_loop().create_task(_take_turns(game_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _ai_turn_wait(game_id: int) -> Optional[float]:
    with session_scope() as db:
        return service.ai_turn_wait(db, game_id)


def _play(game_id: int) -> bool:
    with session_scope() as db:
        return service.play_ai_turn(db, game_id)


async def _take_turns(game_id: int) -> None:
    try:
        while True:
            _look_again.discard(game_id)
            while (
                watching := await run_in_threadpool(_ai_turn_wait, game_id)
            ) is not None:
                await asyncio.sleep(watching + random.uniform(*THINK_SECONDS))
                if not await hub.run(game_id, partial(_play, game_id)):
                    break  # the round ended while it was thinking
            if game_id not in _look_again:
                return
    except Exception:  # noqa: BLE001 - the next nudge tries again
        logger.exception("game %s: the AI could not take its turn", game_id)
    finally:
        _running.discard(game_id)
        _look_again.discard(game_id)

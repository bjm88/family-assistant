"""Running a level of Zombie Apocalypse while it is being played.

A level lives in this process's memory as a
:class:`~api.games.zombie_apocalypse.Round`, from the moment someone
presses *Start* until the house falls or the clock runs out. Nothing
is written to the database in between: what players do arrives on
their game socket and is applied straight to the round, and a small
loop moves the round's clock on ``1 / TICK_SECONDS`` times a second and
sends everyone what changed. Only the result is stored
(:func:`api.games.service.finish_zombie_round`).

What goes over the socket, on top of the messages every game has:

Phone → server
    ``{"type": "zm", "x", "d"}``  moved to ``x`` along wall ``d``
    (-1 the top wall, 1 the bottom).
    ``{"type": "zf", "w", "x", "d", "r", "s", "q"}``  fired weapon ``w``
    (index into the player's weapons) straight out from ``x`` on wall
    ``d``; ``r`` is how far out a thrown weapon should land; ``s``
    numbers the shot, ``q`` numbers the message.
    ``{"type": "zh", "s", "z", "q"}``  shot ``s`` hit zombie ``z``.
    ``{"type": "zw", "w"}``  switched to weapon ``w`` without firing it.

Server → phone
    ``{"type": "zs", ...}``  the whole level, once, on (re)connecting
    (:meth:`Round.snapshot`).
    ``{"type": "zt", "t", "h", "z", "d", "fr", "f", "m", "w", "g", "a", "o"}``
    what changed since the last one (:meth:`Round.drain`): the clock,
    the house's points, a vampire setting off, zombies hurt, zombies
    frozen, shots fired, players who moved, weapons switched, where the
    guard dogs are, and how far each player's messages have been
    handled.

The clock stops while nobody is connected, so a dropped connection
doesn't cost a solo player the house. A level nobody comes back to is
called off, as is one that was under way when the server restarted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from functools import partial
from typing import Any, Dict, Optional

from fastapi import WebSocket

from ..db import session_scope
from . import service
from .hub import hub
from .zombie_apocalypse import Round

logger = logging.getLogger(__name__)

TICK_SECONDS = 0.1
# Level seconds per real second. Tests turn it up.
CLOCK_SPEED = 1.0
# The most the clock moves in one step, so a stall on the server is a
# pause for the players rather than a jump.
MAX_STEP_SECONDS = 0.5
# How long a level waits, paused, for somebody to come back.
ABANDON_AFTER_SECONDS = 180.0

# The messages that are part of playing a level, handled here.
LIVE_TYPES = frozenset({"zm", "zf", "zh", "zw"})


class _Live:
    __slots__ = ("round", "task")

    def __init__(self, round_: Round) -> None:
        self.round = round_
        self.task: Optional["asyncio.Task[None]"] = None


_levels: Dict[int, _Live] = {}


def _clock() -> float:
    return time.monotonic() * CLOCK_SPEED


def _encode(message: Dict[str, Any]) -> str:
    return json.dumps(message, separators=(",", ":"))


def is_live(game_id: int) -> bool:
    return game_id in _levels


def prepare(game_id: int, setup: Dict[str, Any]) -> None:
    """Build the level that was just started.

    Called right after the start is committed, while the hub still
    holds the game's lock, so nobody can find the game ``playing``
    without a level here. May be called from a worker thread.
    """
    _levels[game_id] = _Live(
        Round(
            level=setup["level"],
            duration=setup["duration"],
            seed=setup["seed"],
            players=setup["players"],
            clock=_clock,
        )
    )


def launch(game_id: int) -> None:
    """Start the clock of a prepared level. Call from the event loop."""
    live = _levels.get(game_id)
    if live is not None and live.task is None:
        live.task = asyncio.get_running_loop().create_task(_run(game_id, live))


def stop(game_id: int) -> None:
    """Drop the level without recording a result (the game was ended)."""
    _levels.pop(game_id, None)


def handle(game_id: int, player_id: int, message: Dict[str, Any]) -> None:
    """Something a player did in the level. Never raises."""
    live = _levels.get(game_id)
    if live is None:
        return
    try:
        kind = message.get("type")
        if kind == "zm":
            live.round.move(player_id, message)
        elif kind == "zf":
            live.round.fire(player_id, message)
        elif kind == "zh":
            live.round.hit(player_id, message)
        elif kind == "zw":
            live.round.switch(player_id, message)
    except Exception:  # noqa: BLE001 - one bad message must not end the level
        logger.exception("game %s: could not apply %r", game_id, message.get("type"))


async def greet(
    game_id: int, player_id: int, websocket: WebSocket, *, playing: bool
) -> None:
    """Bring a phone that just connected up to date with the level.

    ``playing`` is whether the game said a level was being played when
    the phone connected. If it did but there isn't one here, the server
    restarted in the middle of it: call it off, so the players are back
    where they can start it again.
    """
    live = _levels.get(game_id)
    if live is not None:
        await websocket.send_text(_encode(live.round.snapshot(player_id)))
    elif playing:
        await hub.run(game_id, partial(_call_off_if_orphaned, game_id))


def _call_off_if_orphaned(game_id: int) -> None:
    if game_id in _levels:
        return
    with session_scope() as db:
        service.abandon_zombie_round(db, game_id)


def _call_off(game_id: int, live: _Live) -> None:
    if _levels.get(game_id) is live:
        _levels.pop(game_id, None)
        with session_scope() as db:
            service.abandon_zombie_round(db, game_id)


def _record(game_id: int, live: _Live) -> None:
    if _levels.get(game_id) is live:
        _levels.pop(game_id, None)
        with session_scope() as db:
            service.finish_zombie_round(db, game_id, live.round.result())


async def _run(game_id: int, live: _Live) -> None:
    round_ = live.round
    try:
        await hub.send_each(game_id, lambda pid: _encode(round_.snapshot(pid)))
        last = time.monotonic()
        empty_since: Optional[float] = None
        while True:
            await asyncio.sleep(TICK_SECONDS)
            if _levels.get(game_id) is not live:
                return  # the game was ended
            now = time.monotonic()
            elapsed, last = now - last, now
            if not hub.connected_player_ids(game_id):
                # Paused: nobody is here to see it.
                empty_since = empty_since if empty_since is not None else now
                if now - empty_since > ABANDON_AFTER_SECONDS:
                    await hub.run(game_id, partial(_call_off, game_id, live))
                    return
                continue
            empty_since = None
            round_.advance(min(elapsed * CLOCK_SPEED, MAX_STEP_SECONDS))
            batch = round_.drain()
            if batch is not None:
                await hub.broadcast_text(game_id, _encode(batch))
            if round_.over is not None:
                # Stores the result and shows everyone the outcome.
                await hub.run(game_id, partial(_record, game_id, live))
                return
    except Exception:  # noqa: BLE001 - don't leave the game stuck on "playing"
        logger.exception("game %s: the level stopped unexpectedly", game_id)
        try:
            await hub.run(game_id, partial(_call_off, game_id, live))
        except Exception:  # noqa: BLE001
            logger.exception("game %s: could not call the level off", game_id)
    finally:
        if _levels.get(game_id) is live:
            _levels.pop(game_id, None)

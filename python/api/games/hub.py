"""Live connections of each game.

Every player's phone holds one WebSocket open while they are on the
game screen. After anything changes — a join, a move, someone's phone
dropping off — every connected player is sent the whole game again
(it's small), each with their own ``you`` block.

Changes to one game run one at a time (:meth:`GameHub.run`), and the
state is read and sent while still holding that game's lock, so a
phone can never be sent an older board after a newer one.

This lives in the memory of the single API process. The game itself is
in Postgres, so a restart only costs a reconnect.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional, Set, TypeVar

from fastapi import WebSocket
from starlette.concurrency import run_in_threadpool

from ..db import session_scope
from . import service

logger = logging.getLogger(__name__)

T = TypeVar("T")

# One person, a couple of tabs or devices. Past this the oldest goes.
MAX_SOCKETS_PER_PLAYER = 3
# A phone that has stopped reading must not hold up everyone else.
SEND_TIMEOUT_SECONDS = 5.0


class GameHub:
    def __init__(self) -> None:
        # game_id -> player_id -> open sockets, oldest first
        self._sockets: Dict[int, Dict[int, List[WebSocket]]] = {}
        # game_id -> [lock, number of run() calls holding or awaiting it]
        self._locks: Dict[int, List[Any]] = {}

    def connected_player_ids(self, game_id: int) -> Set[int]:
        return set(self._sockets.get(game_id, {}))

    async def connect(self, game_id: int, player_id: int, ws: WebSocket) -> None:
        sockets = self._sockets.setdefault(game_id, {}).setdefault(player_id, [])
        sockets.append(ws)
        while len(sockets) > MAX_SOCKETS_PER_PLAYER:
            await _close_quietly(sockets.pop(0))

    def disconnect(self, game_id: int, player_id: int, ws: WebSocket) -> None:
        players = self._sockets.get(game_id)
        if players is None:
            return
        sockets = players.get(player_id, [])
        if ws in sockets:
            sockets.remove(ws)
        if not sockets:
            players.pop(player_id, None)
        if not players:
            self._sockets.pop(game_id, None)

    async def run(
        self, game_id: int, change: Optional[Callable[[], T]] = None
    ) -> Optional[T]:
        """Apply ``change`` (a blocking function) and tell everyone.

        With no ``change`` this just re-sends the game, e.g. after a
        player connects or drops. If ``change`` raises, nothing is sent
        and the exception propagates.
        """
        entry = self._locks.setdefault(game_id, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                result = (
                    await run_in_threadpool(change) if change is not None else None
                )
                await self._send_state(game_id)
                return result
        finally:
            entry[1] -= 1
            if entry[1] == 0 and game_id not in self._sockets:
                self._locks.pop(game_id, None)

    async def _send_state(self, game_id: int) -> None:
        player_ids = self.connected_player_ids(game_id)
        if not player_ids:
            return
        views = await run_in_threadpool(_load_views, game_id, player_ids)
        for player_id, sockets in list(self._sockets.get(game_id, {}).items()):
            view = views.get(player_id)
            if view is None:
                continue
            message = {"type": "state", "game": view}
            for ws in list(sockets):
                if not await _send(ws, message):
                    self.disconnect(game_id, player_id, ws)
                    await _close_quietly(ws)


def _load_views(game_id: int, player_ids: Set[int]) -> Dict[int, Dict[str, Any]]:
    with session_scope() as db:
        return service.views_for_players(db, game_id, player_ids)


async def _send(ws: WebSocket, message: Dict[str, Any]) -> bool:
    try:
        await asyncio.wait_for(ws.send_json(message), timeout=SEND_TIMEOUT_SECONDS)
        return True
    except Exception:  # noqa: BLE001 - a dead socket is dropped, not fatal
        return False


async def _close_quietly(ws: WebSocket) -> None:
    try:
        await ws.close()
    except Exception:  # noqa: BLE001 - already closed
        pass


hub = GameHub()

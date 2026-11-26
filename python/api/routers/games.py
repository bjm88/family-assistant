"""Family games: the Games page API and the live game connection.

Two kinds of caller, two kinds of auth:

* **Signed-in family members** (session cookie) list the family's
  games, start one, invite more people and end one:

  - ``GET  /api/games?family_id=…``
  - ``POST /api/games``
  - ``POST /api/games/{game_id}/invites``
  - ``POST /api/games/{game_id}/end``

* **Players** hold a join link, ``/play/<token>``, and need no
  session — that is the point: an invitee taps the link in a text or
  email and is in. The token names one person in one game and opens
  nothing else.

  - ``GET /api/games/play/{token}``     the game as that player sees it
  - ``WS  /api/games/play/{token}/ws``  the live connection

  The socket carries everything a player does (``join``, ``start``,
  ``move``, ``play_again``, ``end``; ``ping`` to keep the line open)
  and pushes the whole game back to every player after each change.
  While a level of a real-time game is being played it also carries
  that level's own, much smaller messages; see
  :mod:`api.games.zombie_live`.

* **Guests** hold the game's shareable link, ``/play/join/<token>``,
  which the host copies and passes on. It isn't tied to a person: the
  guest types their name, and joining gives them a seat and a join
  link of their own, after which they are a player like any other.

  - ``GET  /api/games/join/{invite_token}``  who is playing, before joining
  - ``POST /api/games/join/{invite_token}``  join with a name and a turkey

When the AI has a seat, :mod:`api.games.ai_turns` plays its turns.
"""

from __future__ import annotations

import asyncio
import json
import logging
from functools import partial
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from .. import models, schemas
from ..auth import CurrentUser, require_family_member, require_user
from ..db import get_db, session_scope
from ..games import ai_turns, invites, service, zombie_live
from ..games.catalog import GAME_TYPES
from ..games.hub import hub

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/games", tags=["games"])

T = TypeVar("T")

# Anything a player sends is a few dozen bytes of JSON.
_MAX_ACTION_BYTES = 2048


def _http_error(exc: service.GameError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


async def _in_thread(work: Callable[[], T]) -> T:
    """Run blocking DB work off the event loop; GameError becomes an HTTP error."""
    try:
        return await run_in_threadpool(work)
    except service.GameError as exc:
        raise _http_error(exc) from exc


# ---------------------------------------------------------------------------
# Signed-in family members: the Games page
# ---------------------------------------------------------------------------


def _viewer_person_id(
    db: Session, user: CurrentUser, family_id: int
) -> Optional[int]:
    """Which person in this family the signed-in user is, if any.

    A member's session already says. An admin's doesn't (admins act
    across families), so match their sign-in email to someone here.
    """
    if user.person_id is not None:
        return user.person_id
    return db.execute(
        select(models.Person.person_id)
        .where(models.Person.family_id == family_id)
        .where(func.lower(models.Person.email_address) == user.email.lower())
        .order_by(models.Person.person_id)
        .limit(1)
    ).scalar_one_or_none()


@router.get("")
def games_overview(
    request: Request,
    family_id: int = Query(...),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Everything the Games page shows for one family."""
    user = require_family_member(family_id, request)
    if db.get(models.Family, family_id) is None:
        raise HTTPException(status_code=404, detail="Family not found")
    me = _viewer_person_id(db, user, family_id)
    people = db.execute(
        select(models.Person)
        .where(models.Person.family_id == family_id)
        .order_by(models.Person.first_name, models.Person.last_name)
    ).scalars()
    return {
        "me_person_id": me,
        "can_pick_host": user.is_admin,
        "catalog": [
            {
                "key": g.key,
                "name": g.name,
                "tagline": g.tagline,
                "min_players": g.min_players,
                "max_players": g.max_players,
                "can_play_ai": g.ai is not None,
                "realtime": g.realtime,
            }
            for g in GAME_TYPES.values()
        ],
        "people": [
            {
                "person_id": p.person_id,
                "name": service.display_name(p),
                "last_name": p.last_name,
                "notify": invites.notify_channels(p),
            }
            for p in people
        ],
        "games": [
            service.game_summary(
                g, viewer_person_id=me, viewer_is_admin=user.is_admin
            )
            for g in service.list_games(db, family_id)
        ],
        "leaderboard": service.leaderboard(db, family_id),
        "ai_name": service.ai_name(db.get(models.Family, family_id)),
        "ai_levels": list(models.GAME_AI_LEVELS),
        "ai_default_level": models.GAME_AI_DEFAULT_LEVEL,
    }


async def _deliver_invites(game_id: int, player_ids: List[int]) -> None:
    """Send the pending invites, then show everyone in the lobby how it went."""
    results = await asyncio.gather(
        *(run_in_threadpool(invites.deliver, pid) for pid in player_ids),
        return_exceptions=True,
    )
    for player_id, result in zip(player_ids, results):
        if isinstance(result, BaseException):
            logger.error(
                "game invite: delivery crashed for player %s", player_id,
                exc_info=result,
            )
    await hub.run(game_id)


def _pending_ids(players: List[models.GamePlayer]) -> List[int]:
    return [p.game_player_id for p in players if p.invite_status == "pending"]


@router.post("", status_code=status.HTTP_201_CREATED)
async def start_game(
    payload: schemas.GameCreate,
    request: Request,
    background: BackgroundTasks,
) -> Dict[str, Any]:
    """Open a game and invite people to it. Returns its Games-page row."""
    user = require_family_member(payload.family_id, request)

    def work() -> Tuple[Dict[str, Any], List[int]]:
        with session_scope() as db:
            if db.get(models.Family, payload.family_id) is None:
                raise service.GameError("Family not found", status_code=404)
            # A member always plays as themselves; only an admin, who
            # isn't tied to a person, says who they are playing as.
            host_id = user.person_id
            if host_id is None:
                host_id = payload.host_person_id or _viewer_person_id(
                    db, user, payload.family_id
                )
            if host_id is None:
                raise service.GameError("Choose who you're playing as.")
            game = service.create_game(
                db,
                family_id=payload.family_id,
                game_type=payload.game_type,
                host_person_id=host_id,
                invite_person_ids=payload.invite_person_ids,
                send_invites=payload.send_invites,
                vs_ai=payload.vs_ai,
                ai_level=payload.ai_level,
            )
            summary = service.game_summary(
                game, viewer_person_id=host_id, viewer_is_admin=user.is_admin
            )
            return summary, _pending_ids(game.players)

    summary, pending = await _in_thread(work)
    if pending:
        background.add_task(_deliver_invites, summary["game_id"], pending)
    return summary


def _game_for(
    db: Session, game_id: int, user: CurrentUser, *, players_too: bool
) -> Tuple[models.Game, Optional[int]]:
    """Load (and lock) a game the signed-in user may change.

    Admins and the host always may. With ``players_too`` so may anyone
    playing in it. A game in another family is reported as missing.
    """
    game = db.execute(
        select(models.Game).where(models.Game.game_id == game_id).with_for_update()
    ).scalar_one_or_none()
    if game is None or not (user.is_admin or user.family_id == game.family_id):
        raise service.GameError("Game not found", status_code=404)
    me = _viewer_person_id(db, user, game.family_id)
    is_host = me is not None and me == game.created_by_person_id
    is_player = me is not None and any(p.person_id == me for p in game.players)
    if not (user.is_admin or is_host or (players_too and is_player)):
        raise service.GameError(
            "Only the person who started this game can do that.", status_code=403
        )
    return game, me


@router.post("/{game_id}/invites")
async def invite_more(
    game_id: int,
    payload: schemas.GameInvite,
    request: Request,
    background: BackgroundTasks,
) -> Dict[str, Any]:
    """Invite more family members to a game (host or admin)."""
    user = require_user(request)

    def work() -> Tuple[Dict[str, Any], List[int]]:
        with session_scope() as db:
            game, me = _game_for(db, game_id, user, players_too=False)
            added = service.add_invites(
                db, game, payload.person_ids, send_invites=payload.send_invites
            )
            summary = service.game_summary(
                game, viewer_person_id=me, viewer_is_admin=user.is_admin
            )
            return summary, _pending_ids(added)

    try:
        summary, pending = await hub.run(game_id, work)
    except service.GameError as exc:
        raise _http_error(exc) from exc
    if pending:
        background.add_task(_deliver_invites, game_id, pending)
    return summary


@router.post("/{game_id}/end")
async def end_game(game_id: int, request: Request) -> Dict[str, Any]:
    """End a game from the Games page (host, a player, or an admin)."""
    user = require_user(request)

    def work() -> Dict[str, Any]:
        with session_scope() as db:
            game, me = _game_for(db, game_id, user, players_too=True)
            service.end_game(game)
            db.flush()
            return service.game_summary(
                game, viewer_person_id=me, viewer_is_admin=user.is_admin
            )

    def work_then_stop() -> Dict[str, Any]:
        summary = work()
        # A level being played ends with the game.
        zombie_live.stop(game_id)
        return summary

    try:
        return await hub.run(game_id, work_then_stop)
    except service.GameError as exc:
        raise _http_error(exc) from exc


# ---------------------------------------------------------------------------
# Players: no session, just the token in their link
# ---------------------------------------------------------------------------


@router.get("/play/{token}")
def play_view(
    token: str, response: Response, db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """The game as the holder of this join link sees it."""
    player = service.find_player(db, token)
    if player is None:
        raise HTTPException(status_code=404, detail="This link isn't valid.")
    game = player.game
    if service.is_expired(game):
        raise HTTPException(status_code=410, detail="This game has ended.")
    response.headers["Cache-Control"] = "no-store"
    return service.view_for_player(
        game, player, connected=hub.connected_player_ids(game.game_id)
    )


@router.get("/join/{invite_token}")
def guest_view(
    invite_token: str, response: Response, db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """The game behind a shareable link, for someone who hasn't joined yet."""
    game = service.find_game_by_invite(db, invite_token)
    if game is None:
        raise HTTPException(status_code=404, detail="This link isn't valid.")
    if service.is_expired(game):
        raise HTTPException(status_code=410, detail="This game has ended.")
    response.headers["Cache-Control"] = "no-store"
    return service.view_for_guest(
        game, connected=hub.connected_player_ids(game.game_id)
    )


@router.post("/join/{invite_token}", status_code=status.HTTP_201_CREATED)
async def guest_join(
    invite_token: str, payload: schemas.GameGuestJoin, response: Response
) -> Dict[str, Any]:
    """Join through the shareable link. Returns the guest's own play link."""

    def game_id_of() -> int:
        with session_scope() as db:
            game = service.find_game_by_invite(db, invite_token)
            if game is None:
                raise service.GameError("This link isn't valid.", status_code=404)
            return game.game_id

    def work() -> Dict[str, Any]:
        with session_scope() as db:
            player = service.join_as_guest(
                db,
                invite_token,
                name=payload.name,
                style=payload.style,
                color=payload.color,
            )
            return {
                "play_path": service.play_path(player.join_token),
                "player_id": player.game_player_id,
                "name": service.seat_name(player),
            }

    game_id = await _in_thread(game_id_of)
    try:
        # Through the hub, so the people already in the lobby see them arrive.
        seat = await hub.run(game_id, work)
    except service.GameError as exc:
        raise _http_error(exc) from exc
    response.headers["Cache-Control"] = "no-store"
    return seat


def _seat(token: str) -> Optional[Tuple[int, int, bool, bool]]:
    """``(game_id, player_id, is it a real-time game, is a round being
    played)`` for a usable token, else ``None``."""
    with session_scope() as db:
        player = service.find_player(db, token)
        if player is None or service.is_expired(player.game):
            return None
        game = player.game
        return (
            player.game_id,
            player.game_player_id,
            service.game_type_of(game).realtime,
            game.status == "playing",
        )


def _apply(token: str, action: Dict[str, Any]) -> Dict[str, Any]:
    """Carry out a player's action. Returns what has to happen next:
    whether the AI is up, and whether the game is played in real time."""
    with session_scope() as db:
        game = service.apply_action(db, token, action)
        game_id = game.game_id
        realtime = service.game_type_of(game).realtime
        playing = service.current_status(game) == "playing"
        level = service.live_round(game)
        ai_up = service.ai_to_move(game) is not None
    # The change is committed, and the hub still holds this game's lock.
    if realtime:
        if level is not None and action.get("type") in ("start", "play_again"):
            zombie_live.prepare(game_id, level)
        elif not playing:
            zombie_live.stop(game_id)
    return {"ai_up": ai_up, "realtime": realtime}


async def _receive_action(websocket: WebSocket) -> Optional[Dict[str, Any]]:
    """Next thing the player sent, or ``None`` if it wasn't a usable action."""
    message = await websocket.receive()
    if message["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(message.get("code", 1000))
    text = message.get("text")
    if not text or len(text) > _MAX_ACTION_BYTES:
        return None
    try:
        action = json.loads(text)
    except ValueError:
        return None
    return action if isinstance(action, dict) else None


@router.websocket("/play/{token}/ws")
async def play_socket(websocket: WebSocket, token: str) -> None:
    seat = await run_in_threadpool(_seat, token)
    if seat is None:
        # Refused before the handshake completes; the page learns why
        # from GET /api/games/play/{token}.
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    game_id, player_id, realtime, playing = seat
    await websocket.accept()
    await hub.connect(game_id, player_id, websocket)
    try:
        # Sends this player the game, and shows the others they're here.
        await hub.run(game_id)
        if realtime:
            # The level being played, if there is one.
            await zombie_live.greet(game_id, player_id, websocket, playing=playing)
        else:
            # If the server restarted while the AI was thinking, this
            # is what gets it going again.
            ai_turns.nudge(game_id)
        while True:
            action = await _receive_action(websocket)
            if action is None:
                await websocket.send_json(
                    {"type": "error", "message": "That didn't make sense to the game."}
                )
            elif action.get("type") in zombie_live.LIVE_TYPES:
                # Part of playing a level: applied in memory, no reply.
                if realtime:
                    zombie_live.handle(game_id, player_id, action)
            elif action.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
            else:
                await _act(websocket, game_id, token, action)
    except (WebSocketDisconnect, RuntimeError):
        # RuntimeError: the hub already closed this socket (it stopped
        # reading, or the player opened too many tabs).
        pass
    finally:
        hub.disconnect(game_id, player_id, websocket)
        try:
            await hub.run(game_id)
        except Exception:  # noqa: BLE001 - nothing left to tell this player
            logger.exception("game %s: could not announce a disconnect", game_id)


async def _act(
    websocket: WebSocket, game_id: int, token: str, action: Dict[str, Any]
) -> None:
    try:
        after = await hub.run(game_id, partial(_apply, token, action))
        if after["ai_up"]:
            ai_turns.nudge(game_id)
        if after["realtime"]:
            zombie_live.launch(game_id)
    except service.GameError as exc:
        await websocket.send_json({"type": "error", "message": str(exc)})
    except Exception:  # noqa: BLE001 - one bad action must not drop the player
        logger.exception("game %s: action %r failed", game_id, action.get("type"))
        await websocket.send_json(
            {"type": "error", "message": "Something went wrong — try that again."}
        )

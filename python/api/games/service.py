"""Starting, joining and playing a game.

Everything here is synchronous and takes a SQLAlchemy session; the
router runs it off the event loop. A player is identified by the
``join_token`` in their link (see :mod:`api.models.game`); the family
member who starts a game is identified by their signed-in session.

A game moves through ``lobby`` → ``playing`` → ``round_over`` (→
``playing`` again for a rematch) → ``ended``. Someone who joins while a
round is under way watches it and is dealt in to the next one.

Besides the family members who were invited, a game can have guests —
anyone who opens its shareable link (``/play/join/<invite_token>``) and
types a name — and the AI, which is given a seat when the game is
created and plays its own turns (:func:`play_ai_turn`).

A real-time game (Zombie Apocalypse) uses the same seats, links and
statuses, but a "round" is a level played live: this module starts it
(``playing``) and records how it went (``round_over``), and
:mod:`.zombie_live` runs it in between without touching the database.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any, Collection, Dict, Iterable, List, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings
from . import zombie_apocalypse as zombies
from .catalog import AVATAR_COLORS, GAME_TYPES, GameType
from .turkey_tictactoe import RuleError

# A family can only have this many games going at once. Each new game
# texts / emails its invitees, so this also bounds how many invites a
# signed-in member can cause to be sent.
MAX_ACTIVE_GAMES_PER_FAMILY = 10

# Long enough for "Grandma Rose", short enough to fit the scoreboard.
GUEST_NAME_MAX_LENGTH = 24


class GameError(Exception):
    """A request the game can't honour. ``str(exc)`` is shown to the player."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def display_name(person: Optional[models.Person]) -> str:
    if person is None:
        return "Someone"
    return (person.preferred_name or person.first_name or "").strip() or "Someone"


def seat_name(player: models.GamePlayer) -> str:
    """What a player is called: a family member's name, or the one a
    guest typed / the AI was given."""
    if player.person_id is None:
        return (player.player_name or "").strip() or "Guest"
    return display_name(player.person)


def ai_name(family: Optional[models.Family]) -> str:
    """The AI opponent is the household's assistant: "Avi AI"."""
    assistant = family.assistant if family is not None else None
    name = (assistant.assistant_name if assistant else "").strip() or "Avi"
    return f"{name} AI"[:40]


def game_type_of(game: models.Game) -> GameType:
    return GAME_TYPES[game.game_type]


def is_expired(game: models.Game, *, now: Optional[datetime] = None) -> bool:
    return game.expires_at <= (now or utcnow())


def current_status(game: models.Game) -> str:
    """``games.status``, except that a game past its expiry reads as ended."""
    return "ended" if is_expired(game) else game.status


def play_path(token: str) -> str:
    return f"/play/{token}"


def play_url(token: str) -> str:
    """The link a player opens on their phone to join."""
    base = (get_settings().PUBLIC_BASE_URL or "http://localhost:8000").rstrip("/")
    return base + play_path(token)


def invite_path(invite_token: str) -> str:
    return f"/play/join/{invite_token}"


def invite_url(game: models.Game) -> Optional[str]:
    """The game's shareable link: anyone who opens it can join as a guest."""
    if not game.invite_token:
        return None
    base = (get_settings().PUBLIC_BASE_URL or "http://localhost:8000").rstrip("/")
    return base + invite_path(game.invite_token)


def joined_players(game: models.Game) -> List[models.GamePlayer]:
    """Players who are in, in turn order: people in the order they
    joined, then the AI — so a person opens the first round."""
    joined = [p for p in game.players if p.joined_at is not None]
    joined.sort(key=lambda p: (p.is_ai, p.joined_at, p.game_player_id))
    return joined


# ---------------------------------------------------------------------------
# Starting a game and inviting people
# ---------------------------------------------------------------------------


def _family_people(
    db: Session, family_id: int, person_ids: Iterable[int]
) -> List[models.Person]:
    ids = list(person_ids)
    if not ids:
        return []
    return list(
        db.execute(
            select(models.Person)
            .where(models.Person.person_id.in_(ids))
            .where(models.Person.family_id == family_id)
        ).scalars()
    )


def _dedupe(ids: Iterable[int], *, exclude: Collection[int] = ()) -> List[int]:
    seen = set(exclude)
    out: List[int] = []
    for person_id in ids:
        if person_id not in seen:
            seen.add(person_id)
            out.append(person_id)
    return out


def active_game_count(db: Session, family_id: int) -> int:
    return int(
        db.execute(
            select(func.count())
            .select_from(models.Game)
            .where(models.Game.family_id == family_id)
            .where(models.Game.status != "ended")
            .where(models.Game.expires_at > utcnow())
        ).scalar_one()
    )


def create_game(
    db: Session,
    *,
    family_id: int,
    game_type: str,
    host_person_id: int,
    invite_person_ids: Sequence[int] = (),
    send_invites: bool = True,
    vs_ai: bool = False,
    ai_level: str = models.GAME_AI_DEFAULT_LEVEL,
) -> models.Game:
    """Open a lobby with the host and their invitees, each with a join link.

    Invitees get ``invite_status='pending'`` when ``send_invites`` is
    set; the caller is expected to deliver them (see
    :mod:`.invites`). Nobody has to be invited: every game also has a
    shareable link (``games.invite_token``) the host can pass around,
    and with ``vs_ai`` the AI takes a seat straight away.
    """
    game_def = GAME_TYPES.get(game_type)
    if game_def is None:
        raise GameError("We don't have that game.", status_code=404)
    host = db.get(models.Person, host_person_id)
    if host is None or host.family_id != family_id:
        raise GameError("The person starting the game isn't in this family.")
    if vs_ai and game_def.ai is None:
        raise GameError(f"{game_def.name} can't be played against the AI yet.")
    if vs_ai and ai_level not in models.GAME_AI_LEVELS:
        raise GameError("Pick easy, medium or hard.")

    invite_ids = _dedupe(invite_person_ids, exclude=[host_person_id])
    others = len(invite_ids) + (1 if vs_ai else 0)
    if others > game_def.max_players - 1:
        raise GameError(
            f"{game_def.name} is for up to {game_def.max_players} players — "
            f"invite at most {game_def.max_players - 1}."
        )
    if len(_family_people(db, family_id, invite_ids)) != len(invite_ids):
        raise GameError("You can only invite people in your family.")
    if active_game_count(db, family_id) >= MAX_ACTIVE_GAMES_PER_FAMILY:
        raise GameError(
            "There are already a lot of games going. End one before starting another."
        )

    game = models.Game(
        family_id=family_id,
        game_type=game_def.key,
        status="lobby",
        created_by_person_id=host.person_id,
        state={},
        expires_at=utcnow() + models.GAME_DEFAULT_TTL,
        invite_token=models.generate_join_token(),
    )
    game.players.append(
        models.GamePlayer(
            person_id=host.person_id,
            is_host=True,
            join_token=models.generate_join_token(),
            invite_status="not_sent",
        )
    )
    for person_id in invite_ids:
        game.players.append(_invited_player(person_id, send_invites))
    if vs_ai:
        game.players.append(
            models.GamePlayer(
                player_name=ai_name(db.get(models.Family, family_id)),
                ai_level=ai_level,
                # Every seat has a token; nobody is ever given this one.
                join_token=models.generate_join_token(),
                invite_status="not_sent",
                joined_at=utcnow(),
                avatar_style=random.choice(game_def.styles),
                avatar_color=random.choice(AVATAR_COLORS),
            )
        )
    db.add(game)
    db.flush()
    return game


def _invited_player(person_id: int, send_invites: bool) -> models.GamePlayer:
    return models.GamePlayer(
        person_id=person_id,
        join_token=models.generate_join_token(),
        invite_status="pending" if send_invites else "not_sent",
    )


def add_invites(
    db: Session,
    game: models.Game,
    person_ids: Sequence[int],
    *,
    send_invites: bool = True,
) -> List[models.GamePlayer]:
    """Invite more family members to a game that hasn't ended."""
    if current_status(game) == "ended":
        raise GameError("This game has ended.", status_code=410)
    game_def = game_type_of(game)
    new_ids = _dedupe(
        person_ids,
        exclude=[p.person_id for p in game.players if p.person_id is not None],
    )
    if not new_ids:
        raise GameError("Everyone you picked is already in this game.")
    if len(game.players) + len(new_ids) > game_def.max_players:
        raise GameError(
            f"{game_def.name} is for up to {game_def.max_players} players."
        )
    if len(_family_people(db, game.family_id, new_ids)) != len(new_ids):
        raise GameError("You can only invite people in your family.")
    added = [_invited_player(person_id, send_invites) for person_id in new_ids]
    game.players.extend(added)
    db.flush()
    return added


# ---------------------------------------------------------------------------
# Playing: every action a player takes arrives here with their token
# ---------------------------------------------------------------------------


def find_player(db: Session, token: str) -> Optional[models.GamePlayer]:
    """The person whose join link carries ``token``. Never the AI's seat."""
    if not token:
        return None
    return db.execute(
        select(models.GamePlayer)
        .where(models.GamePlayer.join_token == token)
        .where(models.GamePlayer.ai_level.is_(None))
    ).scalar_one_or_none()


def _lock_game(db: Session, game_id: Optional[int]) -> models.Game:
    """Load a game with a row lock, then its players.

    The lock makes actions on one game run one at a time, so two people
    tapping at the same moment can't both take the same square or the
    same shirt colour.
    """
    game = None
    if game_id is not None:
        game = db.execute(
            select(models.Game)
            .where(models.Game.game_id == game_id)
            .with_for_update()
        ).scalar_one_or_none()
    if game is None:
        raise GameError("This link isn't valid.", status_code=404)
    return game


def lock_game_for_token(
    db: Session, token: str
) -> tuple[models.Game, models.GamePlayer]:
    """Lock the game a join link belongs to; return it and the link's player."""
    game_id = db.execute(
        select(models.GamePlayer.game_id)
        .where(models.GamePlayer.join_token == token)
        .where(models.GamePlayer.ai_level.is_(None))
    ).scalar_one_or_none()
    game = _lock_game(db, game_id)
    player = next(p for p in game.players if p.join_token == token)
    return game, player


def find_game_by_invite(db: Session, invite_token: str) -> Optional[models.Game]:
    """The game whose shareable link carries ``invite_token``."""
    if not invite_token:
        return None
    return db.execute(
        select(models.Game).where(models.Game.invite_token == invite_token)
    ).scalar_one_or_none()


def clean_guest_name(raw: Any) -> str:
    """The name a guest typed, tidied up. Raises if there isn't a usable one."""
    if not isinstance(raw, str):
        raise GameError("Tell us your name first.")
    name = " ".join("".join(ch for ch in raw if ch.isprintable()).split())
    if not name:
        raise GameError("Tell us your name first.")
    if len(name) > GUEST_NAME_MAX_LENGTH:
        raise GameError(
            f"That name is a bit long — keep it to {GUEST_NAME_MAX_LENGTH} letters."
        )
    return name


def join_as_guest(
    db: Session, invite_token: str, *, name: Any, style: Any, color: Any
) -> models.GamePlayer:
    """Give whoever opened the shareable link a seat of their own, and join.

    Returns their new seat; its ``join_token`` is their own link from
    here on (it is how their phone gets back in after a refresh).
    """
    found = find_game_by_invite(db, invite_token)
    game = _lock_game(db, found.game_id if found else None)
    if current_status(game) == "ended":
        raise GameError("This game has ended.", status_code=410)
    guest_name = clean_guest_name(name)
    for other in joined_players(game):
        if seat_name(other).casefold() == guest_name.casefold():
            raise GameError(
                f"There's already a {seat_name(other)} in this game — "
                "add a last initial so everyone can tell you apart."
            )
    player = models.GamePlayer(
        player_name=guest_name,
        join_token=models.generate_join_token(),
        invite_status="not_sent",
    )
    # Checks the turkey, the shirt colour and that there's a seat left
    # before the guest is added to the game.
    _join(game, player, style=style, color=color)
    game.players.append(player)
    db.flush()
    return player


def apply_action(db: Session, token: str, action: Dict[str, Any]) -> models.Game:
    """Carry out one thing a player did. Raises :class:`GameError` if they can't."""
    game, player = lock_game_for_token(db, token)
    if current_status(game) == "ended":
        raise GameError("This game has ended.", status_code=410)
    kind = action.get("type")
    try:
        if kind == "join":
            _join(game, player, style=action.get("style"), color=action.get("color"))
        elif kind == "end":
            end_game(game)
        elif game_type_of(game).realtime:
            _zombie_action(game, player, kind, action)
        elif kind == "start":
            _start(game, player)
        elif kind == "move":
            _move(game, player, action.get("cell"))
        elif kind == "play_again":
            _play_again(game, player)
        else:
            raise GameError("That isn't something you can do in this game.")
    except RuleError as exc:
        raise GameError(str(exc)) from exc
    db.flush()
    return game


def _join(
    game: models.Game, player: models.GamePlayer, *, style: Any, color: Any
) -> None:
    game_def = game_type_of(game)
    what = "character" if game_def.realtime else "turkey"
    if style not in game_def.styles or color not in AVATAR_COLORS:
        raise GameError(f"Pick a {what} and a shirt colour first.")
    already_in = player.joined_at is not None
    if already_in and game.status != "lobby":
        raise GameError(f"You can't change {what}s once the game has started.")
    for other in game.players:
        if (
            other is not player
            and other.joined_at is not None
            and other.avatar_color == color
        ):
            if other.is_ai and game.status == "lobby":
                # The AI doesn't mind which shirt it wears.
                taken = {p.avatar_color for p in game.players if p is not other}
                other.avatar_color = random.choice(
                    [c for c in AVATAR_COLORS if c not in taken and c != color]
                )
                continue
            raise GameError(
                f"{seat_name(other)} already has that colour — pick another."
            )
    if not already_in:
        if len(joined_players(game)) >= game_def.max_players:
            raise GameError("This game is full.")
        player.joined_at = utcnow()
    player.avatar_style = style
    player.avatar_color = color


def _require_joined(player: models.GamePlayer) -> None:
    if player.joined_at is None:
        raise GameError("Join the game first.")


def _deal_round(game: models.Game) -> None:
    game_def = game_type_of(game)
    players = joined_players(game)
    if len(players) < game_def.min_players:
        raise GameError(
            f"It takes {game_def.min_players} players — waiting for someone to join."
        )
    game.state = game_def.rules.new_round(
        [p.game_player_id for p in players], round_number=game.rounds_played + 1
    )
    game.status = "playing"


def _start(game: models.Game, player: models.GamePlayer) -> None:
    _require_joined(player)
    if game.status != "lobby":
        raise GameError("The game has already started.")
    _deal_round(game)


def _move(game: models.Game, player: models.GamePlayer, cell: Any) -> None:
    _require_joined(player)
    if game.status != "playing":
        raise GameError("There's no round being played right now.")
    state = game_type_of(game).rules.play(game.state, player.game_player_id, cell)
    game.state = state
    if state["winner_player_id"] is not None:
        player.wins += 1
    if state["winner_player_id"] is not None or state["is_draw"]:
        game.status = "round_over"
        game.rounds_played += 1


def _play_again(game: models.Game, player: models.GamePlayer) -> None:
    _require_joined(player)
    if game.status != "round_over":
        raise GameError("Finish this round first.")
    _deal_round(game)


def end_game(game: models.Game) -> None:
    if game.status != "ended":
        game.status = "ended"
        game.ended_at = utcnow()


# ---------------------------------------------------------------------------
# The AI's turns
# ---------------------------------------------------------------------------


def ai_to_move(game: models.Game) -> Optional[models.GamePlayer]:
    """The AI's seat, when a round is being played and it is the AI's turn."""
    if current_status(game) != "playing" or game_type_of(game).realtime:
        return None
    turn = game_type_of(game).rules.current_player_id(game.state)
    player = next((p for p in game.players if p.game_player_id == turn), None)
    return player if player is not None and player.is_ai else None


def is_ai_turn(db: Session, game_id: int) -> bool:
    game = db.get(models.Game, game_id)
    return game is not None and ai_to_move(game) is not None


def play_ai_turn(db: Session, game_id: int) -> bool:
    """Make the AI's move if it is its turn. Returns whether it moved."""
    try:
        game = _lock_game(db, game_id)
    except GameError:
        return False
    player = ai_to_move(game)
    chooser = game_type_of(game).ai
    if player is None or chooser is None:
        return False
    cell = chooser.choose_move(
        game.state, player.game_player_id, level=player.ai_level
    )
    _move(game, player, cell)
    db.flush()
    return True


# ---------------------------------------------------------------------------
# Zombie Apocalypse: what lasts between levels
# ---------------------------------------------------------------------------
#
# ``games.state`` for this game:
#
#   level        the level that is played next (or is being played)
#   duration     seconds the house has to survive
#   loadouts     {player id: [weapon, weapon]} — what each player picked
#   round        while a level is being played: its level, seed and who
#                is fighting; otherwise None
#   last         how the level before went (zombie_apocalypse.Round.result)
#   best_level   the highest level survived in this game
#   kills        {player id: zombies killed, all levels}


def _zombie_state(game: models.Game) -> Dict[str, Any]:
    """A copy of ``games.state`` with everything filled in."""
    state = dict(game.state or {})
    state.setdefault("level", 1)
    state.setdefault("duration", zombies.DEFAULT_DURATION)
    state["loadouts"] = dict(state.get("loadouts") or {})
    state["kills"] = dict(state.get("kills") or {})
    state.setdefault("round", None)
    state.setdefault("last", None)
    state.setdefault("best_level", 0)
    state["best"] = dict(state.get("best") or {})
    return state


def _loadout(state: Dict[str, Any], player: models.GamePlayer) -> List[str]:
    picked = zombies.clean_loadout(state["loadouts"].get(str(player.game_player_id)))
    return picked or list(zombies.DEFAULT_LOADOUT)


def _zombie_action(
    game: models.Game, player: models.GamePlayer, kind: Any, action: Dict[str, Any]
) -> None:
    _require_joined(player)
    if kind in ("start", "play_again"):
        _zombie_start(game)
        return
    if kind not in ("loadout", "settings"):
        raise GameError("That isn't something you can do in this game.")
    if game.status == "playing":
        raise GameError("That has to wait until this level is over.")
    state = _zombie_state(game)
    if kind == "loadout":
        picks = zombies.clean_loadout(action.get("weapons"))
        if picks is None:
            raise GameError(
                f"Pick {zombies.LOADOUT_PICKS} different weapons to go with your crossbow."
            )
        state["loadouts"][str(player.game_player_id)] = picks
    else:
        if not player.is_host:
            raise GameError("Only the person who started the game can change that.")
        duration = action.get("duration")
        if duration not in zombies.DURATIONS:
            raise GameError("Pick how long the house has to hold out.")
        state["duration"] = duration
    game.state = state


def _zombie_start(game: models.Game) -> None:
    if game.status not in ("lobby", "round_over"):
        raise GameError("A level is already being played.")
    players = joined_players(game)
    if len(players) < game_type_of(game).min_players:
        raise GameError("Join the game first.")
    state = _zombie_state(game)
    state["round"] = {
        "level": state["level"],
        "duration": state["duration"],
        "seed": random.randrange(2**31),
        "players": [p.game_player_id for p in players],
    }
    game.state = state
    game.status = "playing"


def live_round(game: models.Game) -> Optional[Dict[str, Any]]:
    """The level being played, as :mod:`.zombie_live` needs it to run it.

    ``None`` when the game isn't real-time or no level is under way.
    """
    if not game_type_of(game).realtime or current_status(game) != "playing":
        return None
    state = _zombie_state(game)
    current = state["round"]
    if not current:
        return None
    by_id = {p.game_player_id: p for p in game.players}
    return {
        "level": current["level"],
        "duration": current["duration"],
        "seed": current["seed"],
        "players": [
            (player_id, _loadout(state, by_id[player_id]))
            for player_id in current["players"]
            if player_id in by_id
        ],
    }


def finish_zombie_round(db: Session, game_id: int, result: Dict[str, Any]) -> bool:
    """Record how a level went. Returns whether there was one to record.

    Surviving counts as a win for everyone who fought, and moves the
    game on to the next level; losing leaves it on the same one.
    """
    try:
        game = _lock_game(db, game_id)
    except GameError:
        return False
    state = _zombie_state(game)
    current = state["round"]
    if game.status != "playing" or not current or current["seed"] != result.get("seed"):
        return False
    fought = set(current["players"])
    for player_id, count in (result.get("kills") or {}).items():
        state["kills"][player_id] = state["kills"].get(player_id, 0) + int(count)
    if result.get("won"):
        for player in game.players:
            if player.game_player_id in fought:
                player.wins += 1
                key = str(player.game_player_id)
                state["best"][key] = max(state["best"].get(key, 0), current["level"])
        state["best_level"] = max(state["best_level"], current["level"])
        state["level"] = min(zombies.MAX_LEVEL, current["level"] + 1)
    state["last"] = {k: v for k, v in result.items() if k != "seed"}
    state["round"] = None
    game.state = state
    game.status = "round_over"
    game.rounds_played += 1
    db.flush()
    return True


def abandon_zombie_round(db: Session, game_id: int) -> bool:
    """Call off a level that can't be finished, as if it was never started.

    That is what happens when the server restarts in the middle of one
    (the level only exists in memory) or everybody leaves for good.
    """
    try:
        game = _lock_game(db, game_id)
    except GameError:
        return False
    if not game_type_of(game).realtime or game.status != "playing":
        return False
    state = _zombie_state(game)
    state["round"] = None
    game.state = state
    game.status = "round_over" if game.rounds_played else "lobby"
    db.flush()
    return True


def _zombie_view(game: models.Game, status: str) -> Dict[str, Any]:
    state = _zombie_state(game)
    current = state["round"] if status == "playing" else None
    return {
        "level": state["level"],
        "duration": state["duration"],
        "durations": list(zombies.DURATIONS),
        "loadouts": {
            str(p.game_player_id): _loadout(state, p) for p in joined_players(game)
        },
        "fighting": list(current["players"]) if current else [],
        "last": state["last"],
        "best_level": state["best_level"],
        "kills": state["kills"],
        "coming": zombies.difficulty(state["level"], len(joined_players(game)))["types"],
        "rules": zombies.rules_view(),
    }


# ---------------------------------------------------------------------------
# What each player sees
# ---------------------------------------------------------------------------


def _player_view(
    player: models.GamePlayer, connected: Collection[int]
) -> Dict[str, Any]:
    return {
        "player_id": player.game_player_id,
        "name": seat_name(player),
        "kind": player.kind,
        "is_host": player.is_host,
        "joined": player.joined_at is not None,
        "avatar_style": player.avatar_style,
        "avatar_color": player.avatar_color,
        "wins": player.wins,
        # The AI is always at the table.
        "connected": player.is_ai or player.game_player_id in connected,
        "invite_status": player.invite_status,
    }


def game_view(
    game: models.Game, *, connected: Collection[int] = ()
) -> Dict[str, Any]:
    """The game as every player may see it. Carries no join tokens."""
    game_def = game_type_of(game)
    status = current_status(game)
    board: Optional[Dict[str, Any]] = None
    if game.state.get("cells"):
        board = {
            **game.state,
            "turn_player_id": (
                game_def.rules.current_player_id(game.state)
                if status == "playing"
                else None
            ),
        }
    host = next((p for p in game.players if p.is_host), None)
    enough = len(joined_players(game)) >= game_def.min_players
    view = {
        "game_id": game.game_id,
        "game_type": game.game_type,
        "game_name": game_def.name,
        "status": status,
        "invited_by": display_name(host.person if host else game.created_by),
        "min_players": game_def.min_players,
        "max_players": game_def.max_players,
        "rounds_played": game.rounds_played,
        # A real-time game starts its next level the same way as its first.
        "can_start": enough
        and (status == "lobby" or (game_def.realtime and status == "round_over")),
        "is_full": len(joined_players(game)) >= game_def.max_players,
        "players": [_player_view(p, connected) for p in game.players],
        "board": board,
    }
    if game_def.realtime:
        view["zombie"] = _zombie_view(game, status)
    return view


def view_for_player(
    game: models.Game,
    player: models.GamePlayer,
    *,
    connected: Collection[int] = (),
) -> Dict[str, Any]:
    """:func:`game_view` plus who the viewer is.

    The host also gets the game's shareable link, and the join links of
    the people who haven't joined yet, so they can pass one on by hand
    when a text or email didn't get through.
    """
    view = game_view(game, connected=connected)
    view["you"] = {
        "player_id": player.game_player_id,
        "name": seat_name(player),
        "is_host": player.is_host,
        "joined": player.joined_at is not None,
    }
    if player.is_host:
        view["invite_url"] = invite_url(game)
        view["invites"] = [
            {
                "player_id": p.game_player_id,
                "name": seat_name(p),
                "play_url": play_url(p.join_token),
                "invite_status": p.invite_status,
                "invite_channels": _channels(p),
                "invite_detail": p.invite_detail,
            }
            for p in game.players
            if not p.is_host and p.joined_at is None
        ]
    return view


def view_for_guest(
    game: models.Game, *, connected: Collection[int] = ()
) -> Dict[str, Any]:
    """The game as someone holding its shareable link sees it before
    they have joined: who is playing and which shirts are taken."""
    view = game_view(game, connected=connected)
    view["you"] = None
    return view


def views_for_players(
    db: Session, game_id: int, player_ids: Collection[int]
) -> Dict[int, Dict[str, Any]]:
    """The current view for each connected player, keyed by player id."""
    game = db.get(models.Game, game_id)
    if game is None:
        return {}
    return {
        p.game_player_id: view_for_player(game, p, connected=player_ids)
        for p in game.players
        if p.game_player_id in player_ids
    }


def _channels(player: models.GamePlayer) -> List[str]:
    return [c for c in (player.invite_channels or "").split(",") if c]


# ---------------------------------------------------------------------------
# The Games page (signed-in family members)
# ---------------------------------------------------------------------------


def game_summary(
    game: models.Game,
    *,
    viewer_person_id: Optional[int],
    viewer_is_admin: bool,
) -> Dict[str, Any]:
    """One row of the Games page.

    A viewer sees their own join link. The host and admins see
    everyone's, so they can hand a link to someone who didn't get it,
    and the game's shareable link. Nobody sees a link for the AI's seat.
    """
    game_def = game_type_of(game)
    is_host = (
        viewer_person_id is not None
        and viewer_person_id == game.created_by_person_id
    )
    can_manage = viewer_is_admin or is_host
    players = []
    my_play_path: Optional[str] = None
    for p in game.players:
        mine = viewer_person_id is not None and p.person_id == viewer_person_id
        if mine:
            my_play_path = play_path(p.join_token)
        can_see_link = (can_manage or mine) and not p.is_ai
        players.append(
            {
                "player_id": p.game_player_id,
                "person_id": p.person_id,
                "name": seat_name(p),
                "kind": p.kind,
                "is_host": p.is_host,
                "joined": p.joined_at is not None,
                "avatar_style": p.avatar_style,
                "avatar_color": p.avatar_color,
                "wins": p.wins,
                "invite_status": p.invite_status,
                "invite_channels": _channels(p),
                "invite_detail": p.invite_detail if can_manage else None,
                "play_url": play_url(p.join_token) if can_see_link else None,
            }
        )
    status = current_status(game)
    return {
        "game_id": game.game_id,
        "game_type": game.game_type,
        "game_name": game_def.name,
        "status": status,
        "rounds_played": game.rounds_played,
        "max_players": game_def.max_players,
        "created_at": game.created_at.isoformat(),
        "host_name": display_name(game.created_by),
        "can_manage": can_manage,
        "my_play_path": my_play_path,
        "invite_url": invite_url(game) if can_manage and status != "ended" else None,
        "players": players,
    }


def list_games(
    db: Session, family_id: int, *, limit: int = 20
) -> List[models.Game]:
    """The family's most recent games, newest first."""
    return list(
        db.execute(
            select(models.Game)
            .where(models.Game.family_id == family_id)
            .order_by(models.Game.created_at.desc(), models.Game.game_id.desc())
            .limit(limit)
        ).scalars()
    )


def leaderboard(db: Session, family_id: int) -> Dict[str, List[Dict[str, Any]]]:
    """All-time rounds won, per game type, by everyone who has ever joined.

    Family members are counted per person. A guest is whoever played
    under that name (ignoring case), and the AI is one entry however
    hard it was playing. For Zombie Apocalypse a win is a level
    survived, and its entries also have ``best``: the highest level the
    player has survived.
    """
    rows = db.execute(
        select(
            models.Game.game_type,
            models.Game.state,
            models.GamePlayer.game_player_id,
            models.GamePlayer.person_id,
            models.GamePlayer.player_name,
            models.GamePlayer.ai_level,
            models.GamePlayer.wins,
        )
        .join(models.Game, models.Game.game_id == models.GamePlayer.game_id)
        .where(models.Game.family_id == family_id)
        .where(models.GamePlayer.joined_at.isnot(None))
        .order_by(models.GamePlayer.game_player_id)
    ).all()
    people = {
        p.person_id: p
        for p in _family_people(
            db, family_id, {r.person_id for r in rows if r.person_id is not None}
        )
    }
    totals: Dict[tuple, Dict[str, Any]] = {}
    for row in rows:
        name = (row.player_name or "").strip()
        who: Any
        if row.ai_level is not None:
            kind, who = "ai", ""
        elif row.person_id is None:
            kind, name = "guest", name or "Guest"
            who = name.casefold()
        else:
            kind, who = "family", row.person_id
            name = display_name(people.get(row.person_id))
        entry = totals.setdefault(
            (row.game_type, kind, who),
            {"person_id": row.person_id, "kind": kind, "wins": 0, "games": 0},
        )
        entry["name"] = name  # as most recently spelled
        entry["wins"] += row.wins
        entry["games"] += 1
        game_def = GAME_TYPES.get(row.game_type)
        if game_def is not None and game_def.realtime:
            best = ((row.state or {}).get("best") or {}).get(str(row.game_player_id))
            entry["best"] = max(entry.get("best", 0), best if isinstance(best, int) else 0)
    out: Dict[str, List[Dict[str, Any]]] = {}
    for (game_type, _kind, _who), entry in totals.items():
        out.setdefault(game_type, []).append(entry)
    for entries in out.values():
        entries.sort(key=lambda e: (-e["wins"], e["name"].lower()))
    return out

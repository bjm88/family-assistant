"""Family games: the rules, the Games page API, join links, the live socket.

Nothing here texts or emails anyone: Twilio and Gmail are replaced by
recorders for every test in the module. Each test gets a household of
its own (deleted afterwards), so games never pile up in the shared
integration family.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from api import models
from api.ai.schema_catalog import ALLOWED_TABLES
from api.auth import ROLE_ADMIN, ROLE_MEMBER, sign_session, verify_session
from api.config import get_settings
from api.db import SessionLocal
from api.games import invites, service
from api.games import turkey_tictactoe as ttt
from api.games.hub import hub
from api.routers import games as games_router


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def outbox(monkeypatch):
    """Record texts and emails instead of sending them."""
    sent = SimpleNamespace(sms=[], email=[])

    def fake_sms(**kwargs):
        sent.sms.append(kwargs)
        return "SM-test"

    def fake_email(creds, *, to, subject, body, **_kwargs):
        sent.email.append({"to": to, "subject": subject, "body": body})
        return "gmail-test"

    monkeypatch.setattr(invites.twilio_sms, "send_sms", fake_sms)
    monkeypatch.setattr(invites.gmail, "send_email", fake_email)
    # Avi's mailbox is "connected" unless a test says otherwise.
    monkeypatch.setattr(
        invites.google_oauth, "load_credentials", lambda db, assistant_id: (None, object())
    )
    settings = get_settings()
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "AC-test")
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "token-test")
    monkeypatch.setattr(settings, "TWILIO_PRIMARY_PHONE", "+15550001111")
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://family.example")
    return sent


@pytest.fixture
def household():
    """A family of five with every mix of contact details.

    * ``ben``   — email, and a mobile that has opted in to texts
    * ``katie`` — email only
    * ``max``   — a mobile that never opted in to texts, no email
    * ``lily``  — no contact details at all
    * ``omar``  — email; exists to be one player too many
    """
    tag = uuid.uuid4().hex[:8]
    now = datetime.now(timezone.utc)
    s = SessionLocal()
    try:
        family = models.Family(family_name=f"Games test {tag}")
        other = models.Family(family_name=f"Games neighbours {tag}")
        s.add_all([family, other])
        s.flush()
        s.add(models.Assistant(family_id=family.family_id))

        def person(fam, first, **kwargs):
            row = models.Person(
                family_id=fam.family_id, first_name=first, last_name="Gamer", **kwargs
            )
            s.add(row)
            return row

        ben = person(
            family,
            "Ben",
            email_address=f"ben.{tag}@example.com",
            mobile_phone_number="(555) 867-0001",
            sms_opted_in_at=now,
        )
        katie = person(family, "Katherine", preferred_name="Katie",
                       email_address=f"katie.{tag}@example.com")
        max_ = person(family, "Max", mobile_phone_number="+15558670003")
        lily = person(family, "Lily")
        omar = person(family, "Omar", email_address=f"omar.{tag}@example.com")
        stranger = person(other, "Stranger", email_address=f"stranger.{tag}@example.com")
        s.commit()
        yield SimpleNamespace(
            family_id=family.family_id,
            other_family_id=other.family_id,
            ben=ben,
            katie=katie,
            max=max_,
            lily=lily,
            omar=omar,
            stranger=stranger,
        )
    finally:
        s.rollback()
        for fam in (family, other):
            s.delete(s.merge(fam))
        s.commit()
        s.close()


def _member_client(app, person) -> TestClient:
    tc = TestClient(app)
    tc.cookies.set(
        get_settings().SESSION_COOKIE_NAME,
        sign_session(
            email=person.email_address,
            role=ROLE_MEMBER,
            person_id=person.person_id,
            family_id=person.family_id,
        ),
    )
    return tc


def _admin_cookie() -> Dict[str, str]:
    token = sign_session(
        email="integration-admin@example.com",
        role=ROLE_ADMIN,
        person_id=None,
        family_id=None,
    )
    return {"Cookie": f"{get_settings().SESSION_COOKIE_NAME}={token}"}


@pytest.fixture
def players():
    """One app and one event loop for every socket in a test.

    The shared ``client`` fixture runs each WebSocket on a loop of its
    own, which a hub that sends to several sockets at once can't span.
    This app has just the games routes (and the session cookie), so
    entering it starts none of the real app's background pollers.
    """
    app = FastAPI()
    app.include_router(games_router.router, prefix="/api")

    @app.middleware("http")
    async def session_cookie(request, call_next):
        raw = request.cookies.get(get_settings().SESSION_COOKIE_NAME)
        request.state.user = verify_session(raw) if raw else None
        return await call_next(request)

    with TestClient(app) as tc:
        yield tc


def _start(client, hh, invite, *, host=None, send_invites=True) -> Dict[str, Any]:
    resp = client.post(
        "/api/games",
        json={
            "family_id": hh.family_id,
            "game_type": "turkey_tictactoe",
            "host_person_id": (host or hh.ben).person_id,
            "invite_person_ids": [p.person_id for p in invite],
            "send_invites": send_invites,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _token(game: Dict[str, Any], person) -> str:
    url = next(p["play_url"] for p in game["players"] if p["person_id"] == person.person_id)
    return url.rsplit("/", 1)[1]


def _state(ws) -> Dict[str, Any]:
    message = ws.receive_json()
    assert message["type"] == "state", message
    return message["game"]


def _act(sender, action: Dict[str, Any], everyone: List[Any]) -> Dict[str, Any]:
    """Send ``action`` and read the game every connected player is pushed."""
    sender.send_json(action)
    states = [_state(ws) for ws in everyone]
    assert all(s["board"] == states[0]["board"] for s in states)
    return states[everyone.index(sender)]


def _error(sender, action: Dict[str, Any]) -> str:
    sender.send_json(action)
    message = sender.receive_json()
    assert message["type"] == "error", message
    return message["message"]


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def _cell(row: int, col: int) -> int:
    return row * ttt.COLS + col


def _play_out(moves, players=(1, 2)):
    """Play cells in turn order; returns the state after the last move."""
    state = ttt.new_round(list(players), round_number=1)
    for cell in moves:
        state = ttt.play(state, ttt.current_player_id(state), cell)
    return state


def test_board_is_five_wide_ten_deep_and_needs_four_in_a_row():
    state = ttt.new_round([1, 2], round_number=1)
    assert (state["cols"], state["rows"], state["win_length"]) == (5, 10, 4)
    assert len(state["cells"]) == 50
    assert ttt.current_player_id(state) == 1


@pytest.mark.parametrize(
    "line",
    [
        [_cell(3, c) for c in range(1, 5)],          # across
        [_cell(r, 2) for r in range(6, 10)],         # down
        [_cell(2 + i, 0 + i) for i in range(4)],     # diagonal, down-right
        [_cell(2 + i, 4 - i) for i in range(4)],     # diagonal, down-left
    ],
)
def test_four_in_a_row_wins_in_every_direction(line):
    # Player 2 plays harmlessly along the top row, away from the line.
    filler = [c for c in range(ttt.COLS) if c not in line][:3]
    moves = [line[0], filler[0], line[1], filler[1], line[2], filler[2], line[3]]
    state = _play_out(moves)
    assert state["winner_player_id"] == 1
    assert state["winning_cells"] == sorted(line)
    assert ttt.current_player_id(state) is None
    with pytest.raises(ttt.RuleError, match="over"):
        ttt.play(state, 2, _cell(9, 0))


def test_three_in_a_row_is_not_enough_and_lines_do_not_wrap_around_the_edge():
    # 3 at the end of one row + 1 at the start of the next are adjacent
    # in the flat list but not on the board.
    cells = [None] * 50
    for cell in (_cell(0, 2), _cell(0, 3), _cell(0, 4), _cell(1, 0)):
        cells[cell] = 1
    assert ttt.winning_line(cells, _cell(1, 0)) == []
    assert ttt.winning_line(cells, _cell(0, 4)) == []


def test_filling_the_gap_wins_with_the_whole_line():
    cells = [None] * 50
    for col in (0, 1, 3, 4):
        cells[_cell(5, col)] = 7
    cells[_cell(5, 2)] = 7
    assert ttt.winning_line(cells, _cell(5, 2)) == [_cell(5, c) for c in range(5)]


def test_rules_refuse_out_of_turn_taken_and_off_board_moves():
    state = ttt.new_round([1, 2], round_number=1)
    with pytest.raises(ttt.RuleError, match="turn"):
        ttt.play(state, 2, 0)
    state = ttt.play(state, 1, 0)
    with pytest.raises(ttt.RuleError, match="taken"):
        ttt.play(state, 2, 0)
    for bad in (-1, 50, "3", None, True, 2.0):
        with pytest.raises(ttt.RuleError, match="board"):
            ttt.play(state, 2, bad)
    with pytest.raises(ttt.RuleError, match="next one"):
        ttt.play(state, 99, 1)


def test_a_full_board_with_no_line_is_a_draw():
    # Columns in a 1 1 2 2 pattern down the board, shifted per column
    # pair, leave no four in a row anywhere.
    owner = lambda r, c: 1 if ((r // 2) + c) % 2 == 0 else 2  # noqa: E731
    cells = [owner(r, c) for r in range(ttt.ROWS) for c in range(ttt.COLS)]
    assert all(ttt.winning_line(cells, i) == [] for i in range(50))

    last = _cell(9, 4)
    mover = cells[last]
    cells[last] = None
    state = {
        **ttt.new_round([1, 2], round_number=1),
        "cells": cells,
        "turn_index": [1, 2].index(mover),
    }
    state = ttt.play(state, mover, last)
    assert state["is_draw"] is True
    assert state["winner_player_id"] is None


def test_who_goes_first_rotates_each_round_and_turns_cycle_through_everyone():
    assert ttt.current_player_id(ttt.new_round([1, 2, 3], round_number=1)) == 1
    assert ttt.current_player_id(ttt.new_round([1, 2, 3], round_number=2)) == 2
    assert ttt.current_player_id(ttt.new_round([1, 2, 3], round_number=4)) == 1
    state = ttt.new_round([1, 2, 3], round_number=1)
    order = []
    for cell in range(6):
        order.append(ttt.current_player_id(state))
        state = ttt.play(state, order[-1], cell * 7)
    assert order == [1, 2, 3, 1, 2, 3]
    with pytest.raises(ttt.RuleError):
        ttt.new_round([1], round_number=1)


# ---------------------------------------------------------------------------
# Games page API (signed-in family members)
# ---------------------------------------------------------------------------


def test_overview_lists_the_game_the_family_and_how_each_person_is_reached(
    client, household
):
    resp = client.get(f"/api/games?family_id={household.family_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [g["key"] for g in body["catalog"]] == ["turkey_tictactoe"]
    assert body["catalog"][0]["min_players"] == 2
    notify = {p["name"]: p["notify"] for p in body["people"]}
    assert notify == {
        "Ben": ["sms", "email"],
        "Katie": ["email"],
        "Max": [],          # has a mobile, never opted in to texts
        "Lily": [],
        "Omar": ["email"],
    }
    assert body["games"] == []
    assert body["can_pick_host"] is True


def test_starting_a_game_gives_everyone_a_private_link_and_sends_invites(
    client, household, outbox, db
):
    hh = household
    game = _start(client, hh, [hh.katie, hh.max, hh.lily])

    assert game["status"] == "lobby"
    assert game["host_name"] == "Ben"
    assert [p["name"] for p in game["players"]] == ["Ben", "Katie", "Max", "Lily"]
    tokens = {_token(game, p) for p in (hh.ben, hh.katie, hh.max, hh.lily)}
    assert len(tokens) == 4 and all(len(t) >= 32 for t in tokens)
    assert all(
        p["play_url"].startswith("https://family.example/play/")
        for p in game["players"]
    )
    assert game["my_play_path"] == f"/play/{_token(game, hh.ben)}"

    # Invites went out after the response: Katie by email (no mobile),
    # nobody by text (Max never opted in), Lily has nothing on file.
    assert [m["to"] for m in outbox.email] == [hh.katie.email_address]
    assert outbox.sms == []
    mail = outbox.email[0]
    assert mail["subject"] == "Ben invited you to play Turkey TicTacToe"
    assert f"https://family.example/play/{_token(game, hh.katie)}" in mail["body"]
    assert "Hi Katie" in mail["body"]

    rows = {
        p.person_id: p
        for p in db.query(models.GamePlayer).filter_by(game_id=game["game_id"])
    }
    assert rows[hh.ben.person_id].invite_status == "not_sent"
    assert rows[hh.katie.person_id].invite_status == "sent"
    assert rows[hh.katie.person_id].invite_channels == "email"
    assert rows[hh.max.person_id].invite_status == "failed"
    assert "hasn't opted in to texts" in rows[hh.max.person_id].invite_detail
    assert rows[hh.lily.person_id].invite_status == "failed"
    assert "no mobile number or email" in rows[hh.lily.person_id].invite_detail


def test_someone_who_opted_in_to_texts_gets_a_text_and_an_email(
    client, household, outbox
):
    hh = household
    game = _start(client, hh, [hh.ben], host=hh.katie)
    assert [m["to_phone"] for m in outbox.sms] == ["+15558670001"]
    link = f"https://family.example/play/{_token(game, hh.ben)}"
    assert link in outbox.sms[0]["body"]
    assert "Katie invited you to play Turkey TicTacToe" in outbox.sms[0]["body"]
    assert [m["to"] for m in outbox.email] == [hh.ben.email_address]

    listed = client.get(f"/api/games?family_id={hh.family_id}").json()["games"][0]
    ben = next(p for p in listed["players"] if p["person_id"] == hh.ben.person_id)
    assert ben["invite_status"] == "sent"
    assert ben["invite_channels"] == ["sms", "email"]


def test_no_text_to_someone_who_opted_out_and_a_failed_send_is_recorded(
    client, household, outbox, db, monkeypatch
):
    hh = household
    person = db.get(models.Person, hh.ben.person_id)
    person.sms_opted_out = True
    db.commit()

    def broken_email(*_a, **_k):
        raise invites.gmail.GmailSendError("quota")

    monkeypatch.setattr(invites.gmail, "send_email", broken_email)
    game = _start(client, hh, [hh.ben], host=hh.katie)
    assert outbox.sms == []
    row = db.query(models.GamePlayer).filter_by(
        game_id=game["game_id"], person_id=hh.ben.person_id
    ).one()
    assert row.invite_status == "failed"
    assert "opted out of texts" in row.invite_detail
    assert "email didn't go through" in row.invite_detail


def test_invites_can_be_skipped(client, household, outbox, db):
    hh = household
    game = _start(client, hh, [hh.katie], send_invites=False)
    assert outbox.email == [] and outbox.sms == []
    assert all(p["invite_status"] == "not_sent" for p in game["players"])


def test_a_member_starts_games_as_themselves_and_sees_only_their_own_link(
    app, client, household
):
    hh = household
    katie = _member_client(app, hh.katie)
    # Katie can't start a game "as Ben": host_person_id is ignored for members.
    resp = katie.post(
        "/api/games",
        json={
            "family_id": hh.family_id,
            "game_type": "turkey_tictactoe",
            "host_person_id": hh.ben.person_id,
            "invite_person_ids": [hh.ben.person_id, hh.omar.person_id],
        },
    )
    assert resp.status_code == 201, resp.text
    game = resp.json()
    assert game["host_name"] == "Katie"
    assert game["my_play_path"] == f"/play/{_token(game, hh.katie)}"
    # As the host she gets everyone's link, to pass on by hand.
    assert all(p["play_url"] for p in game["players"])

    # Ben (an invitee, not the host) sees his own link and nobody else's.
    ben = _member_client(app, hh.ben)
    listed = ben.get(f"/api/games?family_id={hh.family_id}").json()
    assert listed["me_person_id"] == hh.ben.person_id
    assert listed["can_pick_host"] is False
    mine = listed["games"][0]
    assert mine["can_manage"] is False
    links = {p["name"]: p["play_url"] for p in mine["players"]}
    assert links["Ben"] and links["Katie"] is None and links["Omar"] is None
    assert mine["my_play_path"] == f"/play/{links['Ben'].rsplit('/', 1)[1]}"


def test_games_are_private_to_the_family(app, client, household):
    hh = household
    game = _start(client, hh, [hh.katie])
    stranger = _member_client(app, hh.stranger)
    assert stranger.get(f"/api/games?family_id={hh.family_id}").status_code == 403
    resp = stranger.post(
        "/api/games",
        json={
            "family_id": hh.family_id,
            "game_type": "turkey_tictactoe",
            "invite_person_ids": [hh.katie.person_id],
        },
    )
    assert resp.status_code == 403
    assert stranger.post(f"/api/games/{game['game_id']}/end").status_code == 404
    assert (
        stranger.post(
            f"/api/games/{game['game_id']}/invites",
            json={"person_ids": [hh.stranger.person_id]},
        ).status_code
        == 404
    )
    # Signed out: nothing but the join links work.
    anonymous = TestClient(app)
    assert anonymous.get(f"/api/games?family_id={hh.family_id}").status_code == 401
    assert anonymous.post(f"/api/games/{game['game_id']}/end").status_code == 401


def test_starting_a_game_validates_who_is_invited(client, household):
    hh = household

    def start(**overrides):
        body = {
            "family_id": hh.family_id,
            "game_type": "turkey_tictactoe",
            "host_person_id": hh.ben.person_id,
            "invite_person_ids": [hh.katie.person_id],
            **overrides,
        }
        return client.post("/api/games", json=body)

    # Someone from another household.
    resp = start(invite_person_ids=[hh.stranger.person_id])
    assert resp.status_code == 400 and "your family" in resp.json()["detail"]
    # Inviting only yourself.
    resp = start(invite_person_ids=[hh.ben.person_id])
    assert resp.status_code == 400 and "at least one" in resp.json()["detail"]
    # Five players in a four-player game.
    resp = start(
        invite_person_ids=[p.person_id for p in (hh.katie, hh.max, hh.lily, hh.omar)]
    )
    assert resp.status_code == 400 and "up to 4 players" in resp.json()["detail"]
    assert start(game_type="chess").status_code == 404
    assert start(host_person_id=hh.stranger.person_id).status_code == 400
    assert start(invite_person_ids=[]).status_code == 422


def test_host_can_invite_more_until_the_game_is_full(app, client, household, outbox):
    hh = household
    katie = _member_client(app, hh.katie)
    game = katie.post(
        "/api/games",
        json={
            "family_id": hh.family_id,
            "game_type": "turkey_tictactoe",
            "invite_person_ids": [hh.ben.person_id],
        },
    ).json()
    url = f"/api/games/{game['game_id']}/invites"

    # Ben is in the game but didn't start it, so he can't add people.
    resp = _member_client(app, hh.ben).post(
        url, json={"person_ids": [hh.omar.person_id]}
    )
    assert resp.status_code == 403

    # Ben is already in; only Omar is added, and only Omar hears about it.
    resp = katie.post(url, json={"person_ids": [hh.omar.person_id, hh.ben.person_id]})
    assert resp.status_code == 200, resp.text
    assert [p["name"] for p in resp.json()["players"]] == ["Katie", "Ben", "Omar"]
    assert [m["to"] for m in outbox.email] == [
        hh.ben.email_address,
        hh.omar.email_address,
    ]
    assert len(outbox.sms) == 1

    assert katie.post(url, json={"person_ids": [hh.ben.person_id]}).status_code == 400
    resp = katie.post(url, json={"person_ids": [hh.max.person_id, hh.lily.person_id]})
    assert resp.status_code == 400 and "up to 4 players" in resp.json()["detail"]
    assert katie.post(url, json={"person_ids": [hh.max.person_id]}).status_code == 200

    # A player who isn't the host may still call the whole thing off.
    resp = _member_client(app, hh.ben).post(f"/api/games/{game['game_id']}/end")
    assert resp.status_code == 200 and resp.json()["status"] == "ended"
    assert katie.post(url, json={"person_ids": [hh.lily.person_id]}).status_code == 410


def test_a_family_cannot_pile_up_unlimited_games(client, household, monkeypatch):
    hh = household
    monkeypatch.setattr(service, "MAX_ACTIVE_GAMES_PER_FAMILY", 2)
    first = _start(client, hh, [hh.katie], send_invites=False)
    _start(client, hh, [hh.katie], send_invites=False)
    body = {
        "family_id": hh.family_id,
        "game_type": "turkey_tictactoe",
        "host_person_id": hh.ben.person_id,
        "invite_person_ids": [hh.katie.person_id],
    }
    assert client.post("/api/games", json=body).status_code == 400
    # Ending one makes room.
    assert client.post(f"/api/games/{first['game_id']}/end").status_code == 200
    assert client.post("/api/games", json=body).status_code == 201


# ---------------------------------------------------------------------------
# Join links
# ---------------------------------------------------------------------------


def test_a_join_link_needs_no_sign_in_and_shows_who_invited_you(
    app, client, household
):
    hh = household
    game = _start(client, hh, [hh.katie])
    anonymous = TestClient(app)

    resp = anonymous.get(f"/api/games/play/{_token(game, hh.katie)}")
    assert resp.status_code == 200, resp.text
    view = resp.json()
    assert view["invited_by"] == "Ben"
    assert view["game_name"] == "Turkey TicTacToe"
    assert view["you"] == {
        "player_id": view["you"]["player_id"],
        "name": "Katie",
        "is_host": False,
        "joined": False,
    }
    assert view["status"] == "lobby" and view["can_start"] is False
    assert view["board"] is None
    # An invitee is never shown anyone's link — not even their own.
    assert "invites" not in view
    assert _token(game, hh.ben) not in resp.text
    assert _token(game, hh.katie) not in resp.text

    # The host sees the links of people who haven't joined, to share by hand.
    host_view = anonymous.get(f"/api/games/play/{_token(game, hh.ben)}").json()
    assert [i["name"] for i in host_view["invites"]] == ["Katie"]
    assert host_view["invites"][0]["play_url"].endswith(_token(game, hh.katie))


def test_bad_and_expired_links_are_refused(app, client, household, db, players):
    hh = household
    game = _start(client, hh, [hh.katie])
    token = _token(game, hh.katie)
    anonymous = TestClient(app)

    assert anonymous.get("/api/games/play/not-a-real-token").status_code == 404
    with pytest.raises(WebSocketDisconnect):
        with players.websocket_connect("/api/games/play/not-a-real-token/ws"):
            pass

    row = db.get(models.Game, game["game_id"])
    row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.commit()
    resp = anonymous.get(f"/api/games/play/{token}")
    assert resp.status_code == 410 and resp.json()["detail"] == "This game has ended."
    with pytest.raises(WebSocketDisconnect):
        with players.websocket_connect(f"/api/games/play/{token}/ws"):
            pass
    listed = client.get(f"/api/games?family_id={hh.family_id}").json()["games"][0]
    assert listed["status"] == "ended"


def test_join_tokens_are_kept_away_from_the_assistant():
    # Avi's SQL tool only reaches allow-listed tables; a token read from
    # game_players would be a working link to play as someone.
    assert "game_players" not in ALLOWED_TABLES
    assert "games" not in ALLOWED_TABLES


# ---------------------------------------------------------------------------
# Playing, over the live socket
# ---------------------------------------------------------------------------


def test_two_players_join_play_win_rematch_and_end(client, household, players, db):
    hh = household
    game = _start(client, hh, [hh.katie])
    with players.websocket_connect(
        f"/api/games/play/{_token(game, hh.ben)}/ws"
    ) as ben, players.websocket_connect(
        f"/api/games/play/{_token(game, hh.katie)}/ws"
    ) as katie:
        both = [ben, katie]
        # Ben connected first and was sent the game; Katie's arrival is
        # then pushed to both of them.
        first = _state(ben)
        assert [p["connected"] for p in first["players"]] == [True, False]
        assert all(
            [p["connected"] for p in _state(ws)["players"]] == [True, True]
            for ws in both
        )
        ben_id, katie_id = first["players"][0]["player_id"], first["players"][1]["player_id"]

        # Nobody can start, or move, before picking a turkey.
        assert "Join the game first" in _error(ben, {"type": "start"})
        assert "Pick a turkey" in _error(ben, {"type": "join", "style": "dragon", "color": "red"})

        state = _act(ben, {"type": "join", "style": "strong", "color": "red"}, both)
        assert state["you"]["joined"] is True and state["can_start"] is False
        assert "It takes 2 players" in _error(ben, {"type": "start"})

        # Shirt colours are one per player.
        assert "Ben already has that colour" in _error(
            katie, {"type": "join", "style": "cheerleader", "color": "red"}
        )
        state = _act(katie, {"type": "join", "style": "cheerleader", "color": "pink"}, both)
        assert state["can_start"] is True
        assert [(p["avatar_style"], p["avatar_color"]) for p in state["players"]] == [
            ("strong", "red"),
            ("cheerleader", "pink"),
        ]
        # Still in the lobby, so a change of mind is fine.
        state = _act(ben, {"type": "join", "style": "beer", "color": "gold"}, both)
        assert state["players"][0]["avatar_style"] == "beer"

        state = _act(katie, {"type": "start"}, both)
        assert state["status"] == "playing"
        assert state["board"]["turn_order"] == [ben_id, katie_id]
        assert state["board"]["turn_player_id"] == ben_id
        assert "already started" in _error(ben, {"type": "start"})
        assert "can't change turkeys" in _error(
            ben, {"type": "join", "style": "happy", "color": "gold"}
        )

        assert "isn't your turn" in _error(katie, {"type": "move", "cell": 0})
        assert "isn't on the board" in _error(ben, {"type": "move", "cell": 50})
        assert "isn't on the board" in _error(ben, {"type": "move"})

        # Ben builds a diagonal from the top-left; Katie plays the top row.
        diagonal = [_cell(i, i) for i in range(4)]
        for i in range(3):
            state = _act(ben, {"type": "move", "cell": diagonal[i]}, both)
            assert state["board"]["turn_player_id"] == katie_id
            assert state["board"]["last_move"] == diagonal[i]
            if i == 0:
                assert "already taken" in _error(katie, {"type": "move", "cell": diagonal[0]})
            state = _act(katie, {"type": "move", "cell": _cell(0, 4 - i)}, both)
            assert state["status"] == "playing"
        state = _act(ben, {"type": "move", "cell": diagonal[3]}, both)

        assert state["status"] == "round_over"
        assert state["board"]["winner_player_id"] == ben_id
        assert state["board"]["winning_cells"] == diagonal
        assert state["board"]["turn_player_id"] is None
        assert [p["wins"] for p in state["players"]] == [1, 0]
        assert state["rounds_played"] == 1
        assert "no round being played" in _error(katie, {"type": "move", "cell": 30})

        # Rematch: fresh board, scores kept, the other player opens.
        state = _act(katie, {"type": "play_again"}, both)
        assert state["status"] == "playing"
        assert state["board"]["round"] == 2
        assert set(state["board"]["cells"]) == {None}
        assert state["board"]["turn_player_id"] == katie_id
        assert [p["wins"] for p in state["players"]] == [1, 0]
        assert "Finish this round" in _error(ben, {"type": "play_again"})

        state = _act(ben, {"type": "end"}, both)
        assert state["status"] == "ended"
        assert "This game has ended" in _error(katie, {"type": "move", "cell": 0})
        assert "This game has ended" in _error(katie, {"type": "play_again"})

    row = db.get(models.Game, game["game_id"])
    assert row.status == "ended" and row.ended_at is not None
    assert hub.connected_player_ids(game["game_id"]) == set()

    # The win shows up on the Games page.
    overview = client.get(f"/api/games?family_id={hh.family_id}").json()
    assert overview["leaderboard"] == {
        "turkey_tictactoe": [
            {"person_id": hh.ben.person_id, "name": "Ben", "wins": 1, "games": 1},
            {"person_id": hh.katie.person_id, "name": "Katie", "wins": 0, "games": 1},
        ]
    }
    listed = overview["games"][0]
    assert listed["status"] == "ended" and listed["rounds_played"] == 1


def test_a_latecomer_watches_the_round_and_is_dealt_in_to_the_next(
    client, household, players
):
    hh = household
    game = _start(client, hh, [hh.katie, hh.omar])
    url = "/api/games/play/{}/ws".format
    with players.websocket_connect(url(_token(game, hh.ben))) as ben, players.websocket_connect(
        url(_token(game, hh.katie))
    ) as katie:
        _state(ben), _state(ben), _state(katie)
        both = [ben, katie]
        _act(ben, {"type": "join", "style": "happy", "color": "blue"}, both)
        _act(katie, {"type": "join", "style": "girl", "color": "purple"}, both)
        state = _act(ben, {"type": "start"}, both)
        ben_id, katie_id = state["board"]["turn_order"]

        with players.websocket_connect(url(_token(game, hh.omar))) as omar:
            everyone = [ben, katie, omar]
            for ws in everyone:
                _state(ws)
            # Omar arrives mid-round: he sees the board but isn't in the turn order.
            state = _act(omar, {"type": "join", "style": "hippie", "color": "green"}, everyone)
            assert state["you"]["joined"] is True
            omar_id = state["you"]["player_id"]
            assert omar_id not in state["board"]["turn_order"]
            assert "next one" in _error(omar, {"type": "move", "cell": 49})

            # Ben wins down the left edge.
            for row in range(3):
                _act(ben, {"type": "move", "cell": _cell(row, 0)}, everyone)
                _act(katie, {"type": "move", "cell": _cell(row, 4)}, everyone)
            state = _act(ben, {"type": "move", "cell": _cell(3, 0)}, everyone)
            assert state["status"] == "round_over"
            assert state["board"]["winning_cells"] == [_cell(r, 0) for r in range(4)]

            # Next round all three play, and the second player opens.
            state = _act(omar, {"type": "play_again"}, everyone)
            assert state["board"]["turn_order"] == [ben_id, katie_id, omar_id]
            assert state["board"]["turn_player_id"] == katie_id
            state = _act(katie, {"type": "move", "cell": 0}, everyone)
            assert state["board"]["turn_player_id"] == omar_id
            state = _act(omar, {"type": "move", "cell": 1}, everyone)
            assert state["board"]["turn_player_id"] == ben_id

            # Omar's phone drops off; the others are told, and the game goes on.
            omar.close()
            state = _state(ben)
            _state(katie)
        assert {p["name"]: p["connected"] for p in state["players"]} == {
            "Ben": True,
            "Katie": True,
            "Omar": False,
        }
        state = _act(ben, {"type": "move", "cell": 2}, both)
        assert state["status"] == "playing"


def test_the_socket_shrugs_off_junk_and_answers_pings(client, household, players):
    hh = household
    game = _start(client, hh, [hh.katie], send_invites=False)
    with players.websocket_connect(
        f"/api/games/play/{_token(game, hh.ben)}/ws"
    ) as ben:
        _state(ben)
        ben.send_json({"type": "ping"})
        assert ben.receive_json() == {"type": "pong"}
        for junk in ("not json", "[1, 2]", '"join"', "x" * 5000):
            ben.send_text(junk)
            assert ben.receive_json()["type"] == "error"
        ben.send_bytes(b"\x00\x01")
        assert ben.receive_json()["type"] == "error"
        assert "isn't something you can do" in _error(ben, {"type": "cheat"})
        # Still connected and working afterwards.
        state = _act(ben, {"type": "join", "style": "happy", "color": "teal"}, [ben])
        assert state["you"]["joined"] is True


def test_ending_a_game_from_the_games_page_reaches_connected_players(
    client, household, players
):
    hh = household
    game = _start(client, hh, [hh.katie], send_invites=False)
    with players.websocket_connect(
        f"/api/games/play/{_token(game, hh.katie)}/ws"
    ) as katie:
        assert _state(katie)["status"] == "lobby"

        # Someone joins the party late: the host adds them from the
        # Games page, and the lobby on Katie's phone shows them.
        resp = players.post(
            f"/api/games/{game['game_id']}/invites",
            json={"person_ids": [hh.omar.person_id], "send_invites": False},
            headers=_admin_cookie(),
        )
        assert resp.status_code == 200, resp.text
        assert [p["name"] for p in _state(katie)["players"]] == ["Ben", "Katie", "Omar"]

        resp = players.post(
            f"/api/games/{game['game_id']}/end", headers=_admin_cookie()
        )
        assert resp.status_code == 200, resp.text
        assert _state(katie)["status"] == "ended"

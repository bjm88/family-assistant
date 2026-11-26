"""Chinese Checkers: the board, the rules, and a game played over sockets.

The fixtures (a household of its own per test, an app whose sockets
share one event loop, texts and emails recorded instead of sent) are
the ones the other games' tests use.
"""

from __future__ import annotations

import math
import random
from contextlib import ExitStack
from typing import Any, Dict, List, Optional, Sequence

import pytest

from api.games import chinese_checkers as cc
from api.games.turkey_tictactoe import RuleError

from .test_games import (  # noqa: F401 - fixtures
    _act,
    _error,
    _guest_join,
    _invite_token,
    _start,
    _state,
    _token,
    household,
    outbox,
    players,
)

CHECKERS = cc.GAME_KEY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hole(x: int, z: int) -> int:
    """The number of the hole at ``(x, z)``; ``z`` is the row, top to bottom."""
    return cc.HOLES.index((x, z))


def _xy(hole: int):
    x, z = cc.HOLES[hole]
    return x + z / 2, z * math.sqrt(3) / 2


def _table(placed: Dict[int, Sequence[int]], *, turn: Optional[int] = None) -> Dict[str, Any]:
    """A round with the marbles put where the test wants them.

    ``placed`` maps each player (in turn order) to the holes their
    marbles are in.
    """
    state = cc.new_round(list(placed), round_number=1)
    marbles: List[Optional[int]] = [None] * cc.HOLE_COUNT
    for player_id, holes in placed.items():
        for hole in holes:
            assert marbles[hole] is None
            marbles[hole] = player_id
    state["marbles"] = marbles
    if turn is not None:
        state["turn_index"] = state["turn_order"].index(turn)
    return state


def _refused(state, player_id, path) -> str:
    with pytest.raises(RuleError) as caught:
        cc.play(state, player_id, path)
    return str(caught.value)


def _tip(point: int) -> int:
    return max(cc.POINTS[point], key=lambda hole: math.hypot(*_xy(hole)))


def _best_move(state: Dict[str, Any], player_id: int, rng: random.Random) -> List[int]:
    """A simple player: whichever move gets a marble furthest towards the
    tip of the far point, bringing up the ones at the back first."""
    target = cc.target_of(state, player_id)
    goal = _xy(_tip(target))

    def away(hole: int) -> float:
        x, y = _xy(hole)
        return math.hypot(x - goal[0], y - goal[1])

    options = []
    for hole, owner in enumerate(state["marbles"]):
        if owner != player_id:
            continue
        for landing, path in cc.reachable(state["marbles"], hole, target).items():
            gain = away(hole) - away(landing)
            options.append((gain + 0.02 * away(hole) + rng.random() * 0.01, path))
    return max(options)[1]


# ---------------------------------------------------------------------------
# The board
# ---------------------------------------------------------------------------


def test_the_board_is_the_121_hole_star_with_six_points_of_ten():
    assert cc.HOLE_COUNT == 121
    rows: Dict[int, int] = {}
    for _, z in cc.HOLES:
        rows[z] = rows.get(z, 0) + 1
    assert [rows[z] for z in sorted(rows)] == [
        1, 2, 3, 4, 13, 12, 11, 10, 9, 10, 11, 12, 13, 4, 3, 2, 1,
    ]
    assert [len(point) for point in cc.POINTS] == [10] * 6
    in_points = [hole for point in cc.POINTS for hole in point]
    assert len(set(in_points)) == 60
    assert all(cc.POINT_OF[hole] == p for p, point in enumerate(cc.POINTS) for hole in point)
    assert sum(1 for p in cc.POINT_OF if p == -1) == 61

    layout = cc.layout()
    assert len(layout["holes"]) == 121 and layout["pieces"] == 10
    assert layout["points"] == [list(point) for point in cc.POINTS]


def test_points_are_numbered_clockwise_from_the_top_and_face_each_other():
    angles = []
    for point in range(6):
        x, y = _xy(_tip(point))
        # Clockwise from straight up, as drawn (y grows downwards).
        angles.append(round(math.degrees(math.atan2(x, -y))) % 360)
    assert angles == [0, 60, 120, 180, 240, 300]
    for point in range(6):
        (ax, ay), (bx, by) = _xy(_tip(point)), _xy(_tip(cc.opposite(point)))
        assert ax == pytest.approx(-bx) and ay == pytest.approx(-by)


def test_every_hole_knows_its_neighbours_in_all_six_directions():
    for hole, around in enumerate(cc.NEIGHBOURS):
        assert len(around) == 6
        for direction, other in enumerate(around):
            if other < 0:
                continue
            # Directions come in opposite pairs: 0/1, 2/3, 4/5.
            assert cc.NEIGHBOURS[other][direction ^ 1] == hole
            (ax, ay), (bx, by) = _xy(hole), _xy(other)
            assert math.hypot(ax - bx, ay - by) == pytest.approx(1.0)
    assert sum(1 for n in cc.NEIGHBOURS[_hole(0, 0)] if n >= 0) == 6
    assert all(sum(1 for n in cc.NEIGHBOURS[_tip(p)] if n >= 0) == 2 for p in range(6))


# ---------------------------------------------------------------------------
# Setting up
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "count, homes",
    [
        (2, [0, 3]),
        (3, [0, 2, 4]),
        (4, [0, 1, 3, 4]),
        (5, [0, 1, 2, 3, 4]),
        (6, [0, 1, 2, 3, 4, 5]),
    ],
)
def test_everyone_starts_with_ten_marbles_in_a_point_of_their_own(count, homes):
    ids = list(range(11, 11 + count))
    state = cc.new_round(ids, round_number=1)
    assert state["homes"] == homes
    for player_id, home in zip(ids, homes):
        assert all(state["marbles"][hole] == player_id for hole in cc.POINTS[home])
        assert cc.target_of(state, player_id) == (home + 3) % 6
        assert not cc.has_won(state, player_id)
        assert cc.can_move(state, player_id)
    assert sum(1 for owner in state["marbles"] if owner is not None) == 10 * count
    assert state["last_move"] is None and state["winner_player_id"] is None
    assert cc.current_player_id(state) == ids[0]


def test_it_takes_two_to_six_players_and_who_opens_rotates():
    with pytest.raises(RuleError):
        cc.new_round([1], round_number=1)
    with pytest.raises(RuleError):
        cc.new_round(list(range(7)), round_number=1)
    openers = [
        cc.current_player_id(cc.new_round([1, 2, 3], round_number=n)) for n in range(1, 6)
    ]
    assert openers == [1, 2, 3, 1, 2]


# ---------------------------------------------------------------------------
# Moving
# ---------------------------------------------------------------------------


def test_a_marble_steps_to_an_empty_hole_next_to_it_and_the_turn_passes():
    state = cc.new_round([1, 2], round_number=1)
    start, landing = _hole(2, -5), _hole(2, -4)
    after = cc.play(state, 1, [start, landing])
    assert after["marbles"][start] is None and after["marbles"][landing] == 1
    assert after["last_move"] == {"player_id": 1, "path": [start, landing], "seq": 1}
    assert cc.current_player_id(after) == 2
    # The state handed in is left alone.
    assert state["marbles"][start] == 1 and state["moves"] == 0

    after = cc.play(after, 2, [_hole(-2, 5), _hole(-2, 4)])
    assert after["last_move"]["seq"] == 2 and after["last_move"]["player_id"] == 2
    assert cc.current_player_id(after) == 1


def test_a_marble_hops_over_one_marble_into_the_hole_beyond():
    state = cc.new_round([1, 2], round_number=1)
    # From the second row back, over the front row, out of the point.
    start, landing = _hole(3, -6), _hole(3, -4)
    assert cc.reachable(state["marbles"], start, 3) == {
        landing: [start, landing],
        _hole(1, -4): [start, _hole(1, -4)],
    }
    after = cc.play(state, 1, [start, landing])
    assert after["marbles"][landing] == 1
    # Nothing is captured: the marble hopped over is still there.
    assert after["marbles"][_hole(3, -5)] == 1
    assert sum(1 for owner in after["marbles"] if owner == 1) == 10


def test_hops_chain_over_anyones_marbles_and_can_stop_at_any_landing():
    row = [_hole(x, 0) for x in range(-4, 5)]
    state = _table({1: [row[0], row[3]], 2: [row[1], row[5], _hole(-4, 8)]})
    chain = [row[0], row[2], row[4], row[6]]
    found = cc.reachable(state["marbles"], row[0], 3)
    assert found[row[2]] == chain[:2]
    assert found[row[4]] == chain[:3]
    assert found[row[6]] == chain
    for stop in (2, 3, 4):
        after = cc.play(state, 1, chain[:stop])
        assert after["marbles"][chain[stop - 1]] == 1 and after["marbles"][row[0]] is None
        assert after["last_move"]["path"] == chain[:stop]
        # Its own marble and the other player's are both still in place.
        assert after["marbles"][row[3]] == 1 and after["marbles"][row[1]] == 2

    # Every landing has to be named: no skipping the hole in between.
    assert "hops over one marble at a time" in _refused(state, 1, [row[0], row[4]])
    # A step can't be followed by a hop, nor a hop by a step.
    assert "hops over one marble at a time" in _refused(
        state, 1, [row[3], row[4], row[6]]
    )
    assert "hops over one marble at a time" in _refused(
        state, 1, [row[0], row[2], _hole(-2, 1)]
    )


def test_a_chain_can_turn_corners_but_not_come_back_on_itself():
    # Six marbles spaced around the middle: a marble at one corner of
    # the hexagon they make can hop from corner to corner all the way round.
    ring = [_hole(2, -1), _hole(1, 1), _hole(-1, 2), _hole(-2, 1), _hole(-1, -1), _hole(1, -2)]
    start = _hole(2, -2)
    state = _table({1: [start], 2: ring + [_hole(-4, 8)]})
    lap = [start, _hole(2, 0), _hole(0, 2), _hole(-2, 2), _hole(-2, 0), _hole(0, -2)]
    after = cc.play(state, 1, lap)
    assert after["marbles"][lap[-1]] == 1
    assert "same hole twice" in _refused(state, 1, lap + [start])
    assert "same hole twice" in _refused(state, 1, [start, lap[1], start])
    # Wherever it could stop, the shortest way there is what is offered.
    found = cc.reachable(state["marbles"], start, 3)
    assert set(lap[1:]) <= set(found)
    assert found[lap[-1]] == [start, lap[-1]]


def test_moves_the_rules_refuse():
    state = cc.new_round([1, 2], round_number=1)
    front, beyond = _hole(2, -5), _hole(2, -4)
    assert "isn't your turn" in _refused(state, 2, [_hole(-2, 5), _hole(-2, 4)])
    assert "not in this round" in _refused(state, 9, [front, beyond])
    assert "one of your own marbles" in _refused(state, 1, [beyond, _hole(2, -3)])
    assert "one of your own marbles" in _refused(state, 1, [_hole(-2, 5), _hole(-2, 4)])
    assert "taken" in _refused(state, 1, [_hole(3, -6), front])
    # Two holes away with nothing in between to hop over.
    assert "hops over one marble at a time" in _refused(state, 1, [front, _hole(2, -3)])
    # Nowhere near.
    assert "hops over one marble at a time" in _refused(state, 1, [front, _hole(0, 0)])
    for junk in (None, "12", 12, [], [front], [front, 121], [front, -1], [front, 2.0],
                 [True, beyond], [front, None], {"from": front}):
        assert "isn't on the board" in _refused(state, 1, junk)
    assert "hasn't started" in _refused({}, 1, [front, beyond])


def test_a_marble_that_has_reached_the_far_point_stays_in_it():
    # Player 1 is heading for the bottom point (3).
    inside, edge = _hole(-2, 5), _hole(-1, 5)
    state = _table({1: [inside, _hole(0, 0)], 2: [_hole(4, -8)]})
    found = cc.reachable(state["marbles"], inside, 3)
    assert found and all(cc.POINT_OF[hole] == 3 for hole in found)
    assert "stays in it" in _refused(state, 1, [inside, _hole(-2, 4)])
    after = cc.play(state, 1, [inside, edge])
    assert after["marbles"][edge] == 1
    # A hop that would carry it out and back in again is out too.
    state = _table({1: [_hole(-1, 5), _hole(-1, 4), _hole(-2, 4)], 2: [_hole(4, -8)]})
    assert "stays in it" in _refused(
        state, 1, [_hole(-1, 5), _hole(-1, 3), _hole(-3, 5)]
    )
    # Anyone else's marble is free to pass through.
    state = _table({1: [_hole(0, 0)], 2: [inside]}, turn=2)
    assert cc.play(state, 2, [inside, _hole(-2, 4)])["marbles"][_hole(-2, 4)] == 2


# ---------------------------------------------------------------------------
# Winning
# ---------------------------------------------------------------------------


def test_the_first_player_with_all_ten_marbles_in_the_far_point_wins():
    point = list(cc.POINTS[3])
    last, outside = _hole(-1, 5), _hole(-1, 4)
    state = _table({1: [h for h in point if h != last] + [outside], 2: [_hole(0, 0)]})
    assert not cc.has_won(state, 1)
    after = cc.play(state, 1, [outside, last])
    assert after["winner_player_id"] == 1 and after["is_draw"] is False
    assert cc.is_over(after) and cc.current_player_id(after) is None
    assert "round is over" in _refused(after, 2, [_hole(0, 0), _hole(1, 0)])
    assert "round is over" in _refused(after, 1, [last, _hole(-2, 5)])


def test_a_marble_left_behind_at_home_cannot_spoil_the_other_players_win():
    point = list(cc.POINTS[3])
    last, outside, squatter = _hole(-1, 5), _hole(-1, 4), _tip(3)
    mine = [h for h in point if h not in (last, squatter)] + [outside, _hole(0, 0)]
    state = _table({1: mine, 2: [squatter, _hole(4, -8)]})
    after = cc.play(state, 1, [outside, last])
    # The point is full and player 1 is in it: that's the round.
    assert after["winner_player_id"] == 1

    # A point full of nothing but someone else's marbles is no win, which
    # is how every game for two, four or six starts.
    start = cc.new_round([1, 2], round_number=1)
    assert not cc.has_won(start, 1) and not cc.has_won(start, 2)


def test_a_player_with_nowhere_to_go_is_passed_over():
    tip = _tip(3)
    hemmed_in = [_hole(-4, 7), _hole(-3, 7), _hole(-4, 6), _hole(-2, 6)]
    state = _table({1: hemmed_in + [_hole(0, 0)], 2: [tip], 3: [_hole(4, -8)]})
    assert not cc.can_move(state, 2)
    after = cc.play(state, 1, [_hole(0, 0), _hole(1, 0)])
    assert cc.current_player_id(after) == 3
    after = cc.play(after, 3, [_hole(4, -8), _hole(4, -7)])
    assert cc.current_player_id(after) == 1


@pytest.mark.parametrize("count", [2, 3, 4, 5, 6])
def test_simple_players_always_finish_a_round(count):
    for seed in range(3):
        rng = random.Random(seed * 10 + count)
        ids = list(range(1, count + 1))
        state = cc.new_round(ids, round_number=seed + 1)
        longest = 0
        while not cc.is_over(state):
            assert state["moves"] < 1500, "the round never ended"
            player_id = cc.current_player_id(state)
            path = _best_move(state, player_id, rng)
            longest = max(longest, len(path) - 1)
            state = cc.play(state, player_id, path)
            assert all(
                sum(1 for owner in state["marbles"] if owner == p) == 10 for p in ids
            )
        winner = state["winner_player_id"]
        assert winner in ids and cc.has_won(state, winner)
        assert state["last_move"]["player_id"] == winner
        # Somewhere along the way somebody strung a few hops together.
        assert longest >= 3


# ---------------------------------------------------------------------------
# Over the wire
# ---------------------------------------------------------------------------


def _checkers_game(client, hh, invite=(), **more) -> Dict[str, Any]:
    return _start(client, hh, list(invite), game_type=CHECKERS, **more)


def _socket(players, token: str):
    return players.websocket_connect(f"/api/games/play/{token}/ws")


def test_the_games_page_offers_chinese_checkers_for_two_to_six(client, household):
    body = client.get(f"/api/games?family_id={household.family_id}").json()
    catalog = {g["key"]: g for g in body["catalog"]}
    assert list(catalog)[-1] == CHECKERS
    game = catalog[CHECKERS]
    assert game["name"] == "Chinese Checkers"
    assert (game["min_players"], game["max_players"]) == (2, 6)
    assert game["realtime"] is False and game["can_play_ai"] is False

    resp = client.post(
        "/api/games",
        json={"family_id": household.family_id, "game_type": CHECKERS,
              "host_person_id": household.ben.person_id, "invite_person_ids": [],
              "vs_ai": True},
    )
    assert resp.status_code == 400 and "against the AI" in resp.json()["detail"]


def test_invites_ask_people_to_pick_a_colour(client, household, outbox):
    hh = household
    game = _checkers_game(client, hh, [hh.katie])
    assert game["game_type"] == CHECKERS and game["status"] == "lobby"
    mail = outbox.email[0]
    assert mail["to"] == hh.katie.email_address
    assert mail["subject"] == "Ben invited you to play Chinese Checkers"
    assert "pick your colour and join" in mail["body"]
    assert "turkey" not in mail["body"]
    assert f"/play/{_token(game, hh.katie)}" in mail["body"]


def test_two_players_pick_colours_hop_race_to_the_far_point_and_play_again(
    client, household, players
):
    hh = household
    game = _checkers_game(client, hh, [hh.katie], send_invites=False)
    with _socket(players, _token(game, hh.ben)) as ben, _socket(
        players, _token(game, hh.katie)
    ) as katie:
        both = [ben, katie]
        lobby = _state(ben)
        _state(ben), _state(katie)
        ben_id, katie_id = (p["player_id"] for p in lobby["players"])

        # The board is sent before anyone has joined, so the lobby can show it.
        board = lobby["checkers"]
        assert lobby["board"] is None and board["state"] is None
        assert board["holes"] == [list(hole) for hole in cc.HOLES]
        assert board["points"] == [list(point) for point in cc.POINTS]

        # Nothing to pick but a colour, one per player.
        assert "Pick a colour first" in _error(
            ben, {"type": "join", "style": "strong", "color": "red"}
        )
        assert "Pick a colour first" in _error(
            ben, {"type": "join", "style": "marble", "color": "beige"}
        )
        state = _act(ben, {"type": "join", "style": "marble", "color": "red"}, both)
        assert state["you"]["joined"] is True and state["can_start"] is False
        assert "Ben already has that colour" in _error(
            katie, {"type": "join", "style": "marble", "color": "red"}
        )
        state = _act(katie, {"type": "join", "style": "marble", "color": "blue"}, both)
        assert state["can_start"] is True
        assert [(p["avatar_style"], p["avatar_color"]) for p in state["players"]] == [
            ("marble", "red"),
            ("marble", "blue"),
        ]

        state = _act(ben, {"type": "start"}, both)
        assert state["status"] == "playing" and state["board"] is None
        table = state["checkers"]["state"]
        assert table["turn_order"] == [ben_id, katie_id] and table["homes"] == [0, 3]
        assert table["turn_player_id"] == ben_id and table["last_move"] is None
        assert all(table["marbles"][hole] == ben_id for hole in cc.POINTS[0])
        assert all(table["marbles"][hole] == katie_id for hole in cc.POINTS[3])
        assert "can't change colours" in _error(
            ben, {"type": "join", "style": "marble", "color": "gold"}
        )

        # What the server refuses, each player hearing about their own mistake.
        hop = [_hole(3, -6), _hole(3, -4)]
        assert "isn't your turn" in _error(katie, {"type": "move", "path": [_hole(-2, 5), _hole(-2, 4)]})
        assert "isn't on the board" in _error(ben, {"type": "move"})
        assert "isn't on the board" in _error(ben, {"type": "move", "cell": 3})
        assert "own marbles" in _error(ben, {"type": "move", "path": [_hole(-2, 5), _hole(-2, 4)]})
        assert "hops over one marble" in _error(ben, {"type": "move", "path": [hop[0], _hole(0, 0)]})

        # A hop is pushed to both phones with the path the marble took.
        state = _act(ben, {"type": "move", "path": hop}, both)
        table = state["checkers"]["state"]
        assert table["last_move"] == {"player_id": ben_id, "path": hop, "seq": 1}
        assert table["marbles"][hop[1]] == ben_id and table["marbles"][hop[0]] is None
        assert table["turn_player_id"] == katie_id

        # The two of them play it out.
        rng = random.Random(4)
        sockets = {ben_id: ben, katie_id: katie}
        while state["status"] == "playing":
            table = state["checkers"]["state"]
            assert table["moves"] < 400
            mover = table["turn_player_id"]
            path = _best_move(table, mover, rng)
            state = _act(sockets[mover], {"type": "move", "path": path}, both)
            assert state["checkers"]["state"]["last_move"]["path"] == path

        assert state["status"] == "round_over"
        table = state["checkers"]["state"]
        winner = table["winner_player_id"]
        assert winner in sockets and table["turn_player_id"] is None
        assert table["last_move"]["player_id"] == winner
        assert all(
            table["marbles"][hole] is not None
            for hole in cc.POINTS[cc.target_of(table, winner)]
        )
        assert [p["wins"] for p in state["players"]] == [
            1 if p["player_id"] == winner else 0 for p in state["players"]
        ]
        assert state["rounds_played"] == 1
        assert "no round being played" in _error(ben, {"type": "move", "path": hop})

        # Again: everyone back in their point, scores kept, Katie opens.
        state = _act(katie, {"type": "play_again"}, both)
        table = state["checkers"]["state"]
        assert state["status"] == "playing" and table["round"] == 2
        assert table["turn_player_id"] == katie_id and table["moves"] == 0
        assert all(table["marbles"][hole] == ben_id for hole in cc.POINTS[0])
        assert sum(p["wins"] for p in state["players"]) == 1

        state = _act(ben, {"type": "end"}, both)
        assert state["status"] == "ended"

    overview = client.get(f"/api/games?family_id={hh.family_id}").json()
    ranks = overview["leaderboard"][CHECKERS]
    assert [(r["name"], r["wins"]) for r in ranks] == sorted(
        [("Ben", 1 if winner == ben_id else 0), ("Katie", 1 if winner == katie_id else 0)],
        key=lambda r: -r[1],
    )


def test_six_can_play_and_a_seventh_is_turned_away(client, household, players):
    hh = household
    family = [hh.ben, hh.katie, hh.max, hh.lily, hh.omar]
    colours = ["red", "orange", "gold", "green", "teal"]
    game = _checkers_game(client, hh, family[1:], send_invites=False)
    invite = _invite_token(game)
    assert players.get(f"/api/games/join/{invite}").json()["game_type"] == CHECKERS

    # A guest's marble needs a colour nobody has, and nothing else.
    with _socket(players, _token(game, hh.ben)) as ben:
        _state(ben)
        _act(ben, {"type": "join", "style": "marble", "color": "red"}, [ben])
        resp = _guest_join(players, invite, "Rose", style="marble", color="red")
        assert resp.status_code == 400 and "already has that colour" in resp.json()["detail"]
        resp = _guest_join(players, invite, "Rose", style="girl", color="pink")
        assert resp.status_code == 400 and "Pick a colour first" in resp.json()["detail"]
        resp = _guest_join(players, invite, "Rose", style="marble", color="pink")
        assert resp.status_code == 201, resp.text
        _state(ben)

        with ExitStack() as rest:
            table_of = [ben]
            for person, colour in list(zip(family, colours))[1:]:
                ws = rest.enter_context(_socket(players, _token(game, person)))
                table_of.append(ws)
                for seat in table_of:
                    _state(seat)
                _act(ws, {"type": "join", "style": "marble", "color": colour}, table_of)

            # Six at the table: full.
            assert players.get(f"/api/games/join/{invite}").json()["is_full"] is True
            resp = _guest_join(players, invite, "Seventh", style="marble", color="blue")
            assert resp.status_code == 400 and "full" in resp.json()["detail"]

            state = _act(ben, {"type": "start"}, table_of)
            # The second player round the table sees the same board, and
            # that it is Ben's turn, not theirs.
            assert "isn't your turn" in _error(
                table_of[1], {"type": "move", "path": [_hole(2, -5), _hole(2, -4)]}
            )
        table = state["checkers"]["state"]
        ids = [p["player_id"] for p in state["players"]]
        assert len(table["turn_order"]) == 6 and set(table["turn_order"]) == set(ids)
        assert table["homes"] == [0, 1, 2, 3, 4, 5]
        assert all(owner is not None for point in cc.POINTS for owner in
                   (table["marbles"][hole] for hole in point))
        assert sum(1 for owner in table["marbles"] if owner is None) == 61
        assert table["turn_player_id"] == state["you"]["player_id"]

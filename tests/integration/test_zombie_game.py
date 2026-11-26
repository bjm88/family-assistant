"""Zombie Apocalypse: the rules, the levels, and a level played live.

The fixtures (a household of its own per test, an app whose sockets
share one event loop, texts and emails recorded instead of sent) are
the ones the other game's tests use.
"""

from __future__ import annotations

import math
import time
from typing import Any, Callable, Dict, List, Optional

import pytest

from api import models
from api.db import SessionLocal
from api.games import service, zombie_live
from api.games import zombie_apocalypse as za

from .test_games import (  # noqa: F401 - fixtures
    _admin_cookie,
    _guest_join,
    _invite_token,
    _start,
    _state,
    _token,
    household,
    outbox,
    players,
)

ZOMBIES = za.GAME_KEY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Clock:
    """A clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _round(level=1, players=1, duration=120, seed=7, picks=("shotgun", "machine_gun")):
    clock = _Clock()
    rnd = za.Round(
        level=level,
        duration=duration,
        seed=seed,
        players=[(i + 1, picks) for i in range(players)],
        clock=clock,
    )
    return rnd, clock


def _advance(rnd, clock, seconds: float, step: float = 0.1) -> None:
    for _ in range(int(round(seconds / step))):
        rnd.advance(step)
        clock.now += step


def _shoot(rnd, clock, player_id: int, zombie, *, weapon: int = 0, shot: int) -> bool:
    """Fire a single-hit weapon at ``zombie`` and report the hit."""
    fighter = rnd.fighters[player_id]
    x, y = zombie.position(rnd.t)
    angle = math.atan2(y - fighter.y, x - fighter.x)
    if not rnd.fire(player_id, {"w": weapon, "a": angle, "x": x, "y": y, "s": shot, "q": shot}):
        return False
    return rnd.hit(player_id, {"s": shot, "z": zombie.id, "q": shot})


def _defend(rnd, clock, *, until: Optional[float] = None) -> None:
    """Every player shoots the most pressing zombie with the crossbow."""
    reload = za.WEAPONS["crossbow"].reload
    shot = 0
    while rnd.over is None and (until is None or rnd.t < until):
        _advance(rnd, clock, reload)
        for player_id, fighter in rnd.fighters.items():
            reachable = [
                z for z in rnd._active.values()
                if math.dist(z.position(rnd.t), (fighter.x, fighter.y)) <= 320
            ]
            if reachable:
                shot += 1
                _shoot(rnd, clock, player_id, min(reachable, key=lambda z: z.arrive), shot=shot)


# ---------------------------------------------------------------------------
# Weapons
# ---------------------------------------------------------------------------


def test_every_weapon_has_a_reach_a_speed_a_damage_and_is_single_hit_or_area():
    w = za.WEAPONS
    assert list(w) == ["crossbow", "machine_gun", "sniper", "shotgun", "flamethrower", "grenade"]
    for weapon in w.values():
        assert weapon.range > 0 and weapon.speed > 0 and weapon.damage > 0 and weapon.reload > 0

    single = {k for k, weapon in w.items() if not weapon.area}
    assert single == {"crossbow", "machine_gun", "sniper"}

    # The crossbow is the one that never runs out.
    assert [k for k, weapon in w.items() if weapon.ammo is None] == ["crossbow"]
    # Machine gun: single hit, the fastest shots of the three.
    assert w["machine_gun"].reload < w["crossbow"].reload < w["sniper"].reload
    # Sniper rifle: slowest to reload, reaches furthest — the whole map.
    assert w["sniper"].reload == max(x.reload for x in w.values())
    assert w["sniper"].range == max(x.range for x in w.values())
    assert w["sniper"].range >= math.hypot(za.WORLD_W, za.WORLD_H / 2)
    # Flamethrower: area damage, but a shorter reach than anything else.
    assert w["flamethrower"].range == min(x.range for x in w.values())
    assert w["flamethrower"].range < w["shotgun"].range < w["crossbow"].range
    # Grenade: the big blast.
    assert w["grenade"].kind == "lob" and w["grenade"].radius >= 60


def test_a_loadout_is_two_different_weapons_besides_the_crossbow():
    assert za.clean_loadout(["grenade", "sniper"]) == ["grenade", "sniper"]
    for bad in (["grenade"], ["grenade", "grenade"], ["crossbow", "sniper"],
                ["grenade", "sniper", "shotgun"], ["grenade", "laser"], "grenade", None):
        assert za.clean_loadout(bad) is None, bad

    rnd, _ = _round(picks=("grenade", "sniper"))
    fighter = rnd.fighters[1]
    assert fighter.weapons == ["crossbow", "grenade", "sniper"]
    assert fighter.ammo == [None, za.WEAPONS["grenade"].ammo, za.WEAPONS["sniper"].ammo]
    # A longer level comes with more ammunition.
    longer, _ = _round(duration=300, picks=("grenade", "sniper"))
    assert longer.fighters[1].ammo[1] > fighter.ammo[1]


def test_shots_use_up_ammunition_and_cannot_be_fired_faster_than_the_weapon_reloads():
    rnd, clock = _round(picks=("sniper", "machine_gun"))
    _advance(rnd, clock, za.COUNTDOWN_SECONDS + 1)
    shot = {"w": 1, "a": 0.0}

    assert rnd.fire(1, shot)
    # A sniper rifle can't fire again straight away...
    assert not rnd.fire(1, shot)
    clock.now += za.WEAPONS["sniper"].reload
    # ...but can once it has reloaded.
    assert rnd.fire(1, shot)
    assert rnd.fighters[1].ammo[1] == za.WEAPONS["sniper"].ammo - 2

    # The machine gun runs dry; the crossbow never does.
    fired = 0
    for _ in range(za.WEAPONS["machine_gun"].ammo + 20):
        clock.now += za.WEAPONS["machine_gun"].reload
        fired += rnd.fire(1, {"w": 2, "a": 0.0})
    assert fired == za.WEAPONS["machine_gun"].ammo
    assert rnd.fighters[1].ammo[2] == 0
    for _ in range(50):
        clock.now += za.WEAPONS["crossbow"].reload
        assert rnd.fire(1, {"w": 0, "a": 0.0})

    # Nonsense is refused without fuss.
    for bad in ({"w": 7, "a": 0.0}, {"w": 0}, {"w": "0", "a": 0.0}, {"w": 0, "a": float("nan")}, {}):
        clock.now += 1
        assert not rnd.fire(1, bad)
    assert not rnd.fire(99, {"w": 0, "a": 0.0})


def test_nobody_can_shoot_before_the_countdown_ends():
    rnd, clock = _round()
    assert rnd.t == -za.COUNTDOWN_SECONDS
    assert not rnd.fire(1, {"w": 0, "a": 0.0})
    _advance(rnd, clock, za.COUNTDOWN_SECONDS + 0.2)
    assert rnd.fire(1, {"w": 0, "a": 0.0})


def test_a_hit_only_counts_for_a_shot_that_was_fired_and_could_reach():
    rnd, clock = _round()
    _advance(rnd, clock, za.COUNTDOWN_SECONDS + 12)
    fighter = rnd.fighters[1]
    near = min(rnd._active.values(), key=lambda z: math.dist(z.position(rnd.t), (fighter.x, fighter.y)))
    hp = near.hp

    # No such shot.
    assert not rnd.hit(1, {"s": 1, "z": near.id})
    # A real shot hits once, not twice.
    assert _shoot(rnd, clock, 1, near, shot=1)
    assert near.hp == hp - za.WEAPONS["crossbow"].damage
    assert not rnd.hit(1, {"s": 1, "z": near.id})
    # A zombie that isn't on the map yet can't be hit.
    clock.now += 1
    later = rnd.zombies[-1]
    assert later.t0 > rnd.t
    assert rnd.fire(1, {"w": 0, "a": 0.0, "s": 2})
    assert not rnd.hit(1, {"s": 2, "z": later.id})
    # Nor one further away than the weapon reaches.
    far = za.Zombie(9000, za.ZOMBIE_TYPES["walker"], t0=rnd.t, x0=20, y0=700, tx=150, ty=390,
                    speed=20, hp=60, dps=5)
    rnd._active[far.id] = far
    rnd._by_id[far.id] = far
    clock.now += 1
    assert rnd.fire(1, {"w": 0, "a": 0.0, "s": 3})
    assert not rnd.hit(1, {"s": 3, "z": far.id})
    assert far.hp == 60
    # A bullet reported long after it would have landed is ignored.
    clock.now += 1
    assert rnd.fire(1, {"w": 0, "a": 0.0, "s": 4})
    clock.now += 10
    assert not rnd.hit(1, {"s": 4, "z": near.id})


def test_single_hit_weapons_hurt_one_zombie_and_area_weapons_hurt_the_crowd():
    rnd, clock = _round(level=5, players=2, picks=("shotgun", "grenade"))
    fighter = rnd.fighters[1]
    # Let a crowd gather at the top wall.
    at_wall: list = []
    while len(at_wall) < 4:
        _advance(rnd, clock, 0.5)
        assert rnd.over is None
        at_wall = [
            z for z in rnd._active.values()
            if z.arrive < rnd.t and z.ty < za.HOUSE_Y and not z.flying
        ]
    rnd.drain()
    target = at_wall[0]

    assert _shoot(rnd, clock, 1, target, shot=1)
    assert [d[0] for d in rnd.drain()["d"]] == [target.id]

    # The shotgun catches several of them in one blast.
    x, y = target.position(rnd.t)
    clock.now += 1
    assert rnd.fire(1, {"w": 1, "a": math.atan2(y - fighter.y, x - fighter.x)})
    assert len(rnd.drain()["d"]) >= 2

    # A grenade goes off where it was thrown, a moment later, and its
    # blast reaches further than the shotgun's.
    crowd_x = sum(z.tx for z in at_wall) / len(at_wall)
    crowd_y = sum(z.ty for z in at_wall) / len(at_wall)
    before = sum(z.hp for z in at_wall)
    clock.now += 1
    assert rnd.fire(1, {"w": 2, "a": 0.0, "x": crowd_x, "y": crowd_y})
    assert sum(z.hp for z in at_wall) == before  # still in the air
    _advance(rnd, clock, 1.0)
    hurt = {d[0] for d in rnd.drain()["d"]}
    assert len(hurt & {z.id for z in at_wall}) >= 3


def test_damage_falls_off_towards_the_edge_of_a_spray_and_a_blast():
    shotgun, grenade = za.WEAPONS["shotgun"], za.WEAPONS["grenade"]
    # Straight ahead: full damage up close, less far away, none out of reach.
    assert za.cone_share(shotgun, 0, 0, 0.0, 10, 0, 5) == pytest.approx(1.0, abs=0.05)
    far = za.cone_share(shotgun, 0, 0, 0.0, shotgun.range, 0, 5)
    assert shotgun.falloff <= far < 0.6
    assert za.cone_share(shotgun, 0, 0, 0.0, shotgun.range + 30, 0, 5) == 0
    # Off to the side or behind: nothing.
    assert za.cone_share(shotgun, 0, 0, 0.0, 0, 100, 5) == 0
    assert za.cone_share(shotgun, 0, 0, 0.0, -100, 0, 5) == 0
    # The wedge wraps round the back correctly.
    assert za.cone_share(shotgun, 0, 0, math.pi, -100, 3, 5) > 0

    assert za.blast_share(grenade, 0, 0, 0, 0, 5) == 1.0
    assert grenade.falloff <= za.blast_share(grenade, 0, 0, grenade.radius, 0, 5) < 0.6
    assert za.blast_share(grenade, 0, 0, grenade.radius + 30, 0, 5) == 0


# ---------------------------------------------------------------------------
# Zombies and levels
# ---------------------------------------------------------------------------


def test_there_are_zombies_dogs_bats_and_ogres():
    z = za.ZOMBIE_TYPES
    assert list(z) == ["walker", "dog", "bat", "ogre"]
    # Ogres are big, slow and strong; dogs are small and quick; bats fly.
    assert z["ogre"].hp > 5 * z["walker"].hp and z["ogre"].radius > z["walker"].radius
    assert z["ogre"].speed < z["walker"].speed < z["dog"].speed
    assert z["dog"].radius < z["walker"].radius and z["dog"].hp < z["walker"].hp
    assert [k for k, kind in z.items() if kind.flying] == ["bat"]


def test_a_level_is_made_at_random_but_the_same_seed_gives_the_same_level():
    one = [z.wire() for z in za.build_level(3, 2, 120, seed=11)]
    again = [z.wire() for z in za.build_level(3, 2, 120, seed=11)]
    other = [z.wire() for z in za.build_level(3, 2, 120, seed=12)]
    assert one == again
    assert one != other


def test_zombies_run_in_from_off_the_top_and_bottom_of_the_map_in_waves():
    zombies = za.build_level(2, 1, 120, seed=3)
    assert [z.id for z in zombies] == list(range(1, len(zombies) + 1))
    assert [z.t0 for z in zombies] == sorted(z.t0 for z in zombies)

    top = [z for z in zombies if z.y0 < 0]
    bottom = [z for z in zombies if z.y0 > za.WORLD_H]
    assert top and bottom and len(top) + len(bottom) == len(zombies)
    for z in zombies:
        # They start out of sight and end up at the house.
        assert z.position(z.t0)[1] == pytest.approx(z.y0, abs=za.BAT_WEAVE)
        x, y = z.position(z.arrive + 1)
        assert za.HOUSE_LEFT <= x <= za.HOUSE_RIGHT
        assert za.HOUSE_TOP - 40 <= y <= za.HOUSE_BOTTOM + 40
        # Those from the top attack the top of the house.
        assert (y < za.HOUSE_Y) == (z.y0 < 0)
        assert 0 < z.t0 < 120

    # Waves: bursts of zombies with quiet gaps between them.
    gaps = [b.t0 - a.t0 for a, b in zip(zombies, zombies[1:])]
    assert max(gaps) > 3.0
    assert sum(1 for g in gaps if g > 3.0) >= 5


def test_levels_bring_new_kinds_and_tougher_faster_more_numerous_zombies():
    def kinds(level):
        return {z.type.key for seed in range(6) for z in za.build_level(level, 1, 120, seed)}

    assert kinds(1) == {"walker"}
    assert kinds(2) == {"walker", "dog"}
    assert kinds(3) == {"walker", "dog", "bat"}
    assert kinds(6) == {"walker", "dog", "bat", "ogre"}

    previous = None
    for level in range(1, 15):
        diff = za.difficulty(level, 1)
        count = sum(len(za.build_level(level, 1, 120, seed)) for seed in range(8))
        if previous is not None:
            assert diff["hp"] > previous["hp"]                    # strength
            assert diff["speed"] >= previous["speed"]             # speed
            assert diff["at_once"] > previous["at_once"]          # how many at once
            assert diff["wave_budget"] > previous["wave_budget"]
            assert diff["wave_every"] <= previous["wave_every"]
            assert count > previous["count"] * 0.95
        previous = {**diff, "count": count}

    # A level-9 walker really is tougher and quicker than a level-1 one.
    early = next(z for z in za.build_level(1, 1, 120, 1) if z.type.key == "walker")
    late = next(z for z in za.build_level(9, 1, 120, 1) if z.type.key == "walker")
    assert late.max_hp > early.max_hp and late.dps > early.dps

    # More players, more zombies.
    solo = sum(len(za.build_level(4, 1, 120, s)) for s in range(6))
    four = sum(len(za.build_level(4, 4, 120, s)) for s in range(6))
    assert four > 2 * solo


def test_bats_weave_as_they_fly_in_and_everyone_else_walks_straight():
    zombies = za.build_level(5, 2, 120, seed=2)
    bat = next(z for z in zombies if z.flying)
    walker = next(z for z in zombies if z.type.key == "walker")

    def off_line(z, t):
        x, y = z.position(t)
        # Distance from the straight line between where it starts and ends.
        dx, dy = z.tx - z.x0, z.ty - z.y0
        return abs((x - z.x0) * dy - (y - z.y0) * dx) / math.hypot(dx, dy)

    mid = lambda z: [z.t0 + (z.arrive - z.t0) * f for f in (0.1, 0.2, 0.3, 0.4, 0.5)]  # noqa: E731
    assert max(off_line(bat, t) for t in mid(bat)) > 5
    assert max(off_line(walker, t) for t in mid(walker)) < 0.01
    # The weave has died away by the time the bat is on the roof.
    assert off_line(bat, bat.arrive + 1) < 0.01


# ---------------------------------------------------------------------------
# A level, start to finish
# ---------------------------------------------------------------------------


def test_the_house_starts_with_1000_points_and_falls_if_nobody_defends_it():
    rnd, clock = _round()
    assert rnd.house == 1000 and rnd.snapshot(1)["house_max"] == 1000
    first = min(z.arrive for z in rnd.zombies)

    _advance(rnd, clock, za.COUNTDOWN_SECONDS + first - 0.5)
    assert rnd.house == 1000  # nothing has reached it yet
    _advance(rnd, clock, 5)
    assert 900 < rnd.house < 1000  # being attacked
    _advance(rnd, clock, 120)
    assert rnd.over == "lost" and rnd.house == 0
    result = rnd.result()
    assert result["won"] is False and result["house"] == 0
    assert result["seconds"] < 120


def test_holding_out_until_the_clock_runs_out_wins_the_level():
    rnd, clock = _round(level=1, players=2)
    _defend(rnd, clock)
    assert rnd.over == "won"
    assert rnd.t >= 120
    result = rnd.result()
    assert result["won"] is True and result["level"] == 1 and result["house"] > 500
    assert sum(result["kills"].values()) > 0.8 * result["zombies"]
    assert set(result["kills"]) == {"1", "2"}


def test_two_minutes_is_the_default_and_the_levels_can_be_shorter_or_longer():
    assert za.DEFAULT_DURATION == 120
    assert za.DEFAULT_DURATION in za.DURATIONS
    short, clock = _round(duration=60)
    _defend(short, clock)
    assert short.over == "won" and 60 <= short.t < 61


@pytest.mark.parametrize("team", [1, 4])
def test_level_one_can_be_survived_with_just_the_crossbow_and_level_twelve_cannot(team):
    easy, clock = _round(level=1, players=team, seed=21)
    _defend(easy, clock)
    assert easy.over == "won"

    hard, clock = _round(level=12, players=team, seed=21)
    _defend(hard, clock)
    assert hard.over == "lost"


def test_updates_say_only_what_changed_and_a_snapshot_says_everything():
    rnd, clock = _round(players=2)
    snap = rnd.snapshot(1)
    assert snap["type"] == "zs" and snap["level"] == 1 and snap["dur"] == 120
    assert len(snap["z"]) == len(rnd.zombies)
    assert [p["id"] for p in snap["p"]] == [1, 2]
    assert [p["x"] for p in snap["p"]] == [za.PLAYER_SLOTS[0][0], za.PLAYER_SLOTS[1][0]]
    assert snap["ammo"] == [None, za.WEAPONS["shotgun"].ammo, za.WEAPONS["machine_gun"].ammo]
    assert snap["dead"] == [] and snap["hp"] == []
    # Someone watching has no weapons of their own.
    assert rnd.snapshot(None)["ammo"] is None and rnd.snapshot(77)["ammo"] is None

    _advance(rnd, clock, za.COUNTDOWN_SECONDS + 14)
    rnd.drain()
    # With nothing happening, an update goes out about once a second.
    sent = 0
    for _ in range(20):
        rnd.advance(0.1)
        clock.now += 0.1
        if rnd.house < 1000:
            break
        sent += rnd.drain() is not None
    assert sent <= 3

    target = next(iter(rnd._active.values()))
    rnd.switch(2, {"w": 2})
    assert _shoot(rnd, clock, 1, target, shot=5)
    batch = rnd.drain()
    assert batch["d"] == [[target.id, target.max_hp - 40, 1]]
    assert batch["f"][0][:2] == [1, 0]
    assert batch["w"] == [[2, 2]]
    assert batch["a"] == {"1": 5}
    # Said once, not again.
    rnd.advance(0.1)
    again = rnd.drain()
    assert again is None or not ({"d", "f", "w", "a"} & set(again))

    clock.now += 1
    assert _shoot(rnd, clock, 1, target, shot=6)
    assert rnd.drain()["d"] == [[target.id, 0, 1]]
    snap = rnd.snapshot(2)
    assert snap["dead"] == [target.id]
    assert next(p for p in snap["p"] if p["id"] == 1)["kills"] == 1
    assert next(p for p in snap["p"] if p["id"] == 2)["weapon"] == 2


# ---------------------------------------------------------------------------
# Played over the socket
# ---------------------------------------------------------------------------


@pytest.fixture
def fast_levels(monkeypatch):
    """Levels run 40 times faster than life, in small steps."""
    monkeypatch.setattr(zombie_live, "TICK_SECONDS", 0.01)
    monkeypatch.setattr(zombie_live, "CLOCK_SPEED", 40.0)
    yield
    zombie_live._levels.clear()


def _zombie_game(client, hh, invite=()) -> Dict[str, Any]:
    return _start(client, hh, list(invite), send_invites=False, game_type=ZOMBIES)


def _until(ws, wanted: Callable[[Dict[str, Any]], bool], limit: int = 5000) -> Dict[str, Any]:
    """Read messages until one matches."""
    for _ in range(limit):
        message = ws.receive_json()
        if wanted(message):
            return message
    raise AssertionError("the message never came")


def _is_state(status: str) -> Callable[[Dict[str, Any]], bool]:
    return lambda m: m["type"] == "state" and m["game"]["status"] == status


def _join(ws, style="cap", color="red") -> Dict[str, Any]:
    ws.send_json({"type": "join", "style": style, "color": color})
    return _until(ws, lambda m: m["type"] == "state" and m["game"]["you"]["joined"])["game"]


def test_the_games_page_offers_both_games(client, household):
    body = client.get(f"/api/games?family_id={household.family_id}").json()
    catalog = {g["key"]: g for g in body["catalog"]}
    assert list(catalog) == ["turkey_tictactoe", ZOMBIES]
    zombie = catalog[ZOMBIES]
    assert zombie["name"] == "Zombie Apocalypse"
    assert (zombie["min_players"], zombie["max_players"]) == (1, 4)
    assert zombie["realtime"] is True and zombie["can_play_ai"] is False
    assert catalog["turkey_tictactoe"]["realtime"] is False

    # It can't be played against the AI.
    resp = client.post(
        "/api/games",
        json={"family_id": household.family_id, "game_type": ZOMBIES,
              "host_person_id": household.ben.person_id, "vs_ai": True},
    )
    assert resp.status_code == 400


def test_one_player_sets_up_and_loses_the_house_by_standing_still(
    client, household, players, fast_levels
):
    hh = household
    game = _zombie_game(client, hh)
    with players.websocket_connect(f"/api/games/play/{_token(game, hh.ben)}/ws") as ben:
        lobby = _state(ben)
        assert lobby["game_name"] == "Zombie Apocalypse" and lobby["status"] == "lobby"
        assert not lobby["can_start"]  # nobody has joined yet

        # Survivors, not turkeys.
        ben.send_json({"type": "join", "style": "happy", "color": "red"})
        assert "survivor" in _until(ben, lambda m: m["type"] == "error")["message"]
        lobby = _join(ben, "cowboy", "blue")
        me = lobby["you"]["player_id"]
        assert lobby["can_start"]  # one player is enough
        z = lobby["zombie"]
        assert (z["level"], z["duration"], z["best_level"], z["last"]) == (1, 120, 0, None)
        assert z["durations"] == [60, 120, 180, 300]
        assert z["loadouts"] == {str(me): ["shotgun", "machine_gun"]}
        assert z["coming"] == ["walker"]
        rules = z["rules"]
        assert [w["key"] for w in rules["weapons"]] == list(za.WEAPONS)
        assert [k["key"] for k in rules["zombies"]] == list(za.ZOMBIE_TYPES)
        assert rules["house"]["hp"] == 1000 and rules["default_weapon"] == "crossbow"

        # Pick two weapons to go with the crossbow.
        ben.send_json({"type": "loadout", "weapons": ["grenade", "crossbow"]})
        assert "crossbow" in _until(ben, lambda m: m["type"] == "error")["message"]
        ben.send_json({"type": "loadout", "weapons": ["grenade", "sniper"]})
        assert _state(ben)["zombie"]["loadouts"] == {str(me): ["grenade", "sniper"]}
        ben.send_json({"type": "settings", "duration": 45})
        assert _until(ben, lambda m: m["type"] == "error")
        ben.send_json({"type": "settings", "duration": 180})
        assert _state(ben)["zombie"]["duration"] == 180
        ben.send_json({"type": "settings", "duration": 120})
        _state(ben)

        ben.send_json({"type": "start"})
        playing = _state(ben)
        assert playing["status"] == "playing" and playing["zombie"]["fighting"] == [me]
        snap = _until(ben, lambda m: m["type"] == "zs")
        assert snap["level"] == 1 and snap["house"] == 1000 and snap["t"] <= 0
        assert snap["p"][0]["weapons"] == ["crossbow", "grenade", "sniper"]
        assert snap["ammo"] == [None, 9, 14]
        assert zombie_live.is_live(game["game_id"])

        # Weapons are chosen between levels, not during one.
        ben.send_json({"type": "loadout", "weapons": ["shotgun", "sniper"]})
        assert "level is over" in _until(ben, lambda m: m["type"] == "error")["message"]

        # The house's points only ever go down, and when they run out
        # the level is lost.
        seen: List[int] = []

        def over(message):
            if message["type"] == "zt":
                seen.append(message["h"])
            return _is_state("round_over")(message)

        ended = _until(ben, over)["game"]
        assert seen == sorted(seen, reverse=True) and seen[0] == 1000 and seen[-1] == 0
        z = ended["zombie"]
        assert z["last"]["won"] is False and z["last"]["house"] == 0
        assert z["last"]["level"] == 1 and z["last"]["seconds"] < 120
        assert (z["level"], z["best_level"]) == (1, 0)  # the same level again
        assert ended["rounds_played"] == 1 and ended["can_start"]
        assert [p["wins"] for p in ended["players"]] == [0]
        assert not zombie_live.is_live(game["game_id"])


def _fight(driver, shooters: Dict[int, Any], snap: Dict[str, Any]) -> Dict[str, Any]:
    """Shoot the nearest zombies with crossbows until the level is over.

    ``driver`` is the socket the updates are read from; ``shooters``
    maps player ids to the sockets their shots are sent on.
    """
    spots = {p["id"]: (p["x"], p["y"]) for p in snap["p"]}
    zombies = {}
    for zid, _kind, t0, x0, y0, tx, ty, speed, hp in snap["z"]:
        travel = math.hypot(tx - x0, ty - y0) / speed
        zombies[zid] = {"id": zid, "t0": t0, "arrive": t0 + travel, "hp": hp,
                        "from": (x0, y0), "to": (tx, ty)}
    shot = 0
    while True:
        message = driver.receive_json()
        if _is_state("round_over")(message):
            return message["game"]
        if message["type"] != "zt":
            continue
        t = message["t"]
        for zid, hp, _by in message.get("d", ()):
            zombies[zid]["hp"] = hp
        if t < 0:
            continue
        taken = set()
        for player_id, ws in shooters.items():
            best = None
            for z in zombies.values():
                if z["hp"] <= 0 or z["id"] in taken or z["t0"] > t - 0.5:
                    continue
                p = min(1.0, (t - z["t0"]) / (z["arrive"] - z["t0"]))
                x = z["from"][0] + (z["to"][0] - z["from"][0]) * p
                y = z["from"][1] + (z["to"][1] - z["from"][1]) * p
                if math.dist((x, y), spots[player_id]) > 300:
                    continue
                if best is None or z["arrive"] < best["arrive"]:
                    best = z
            if best is None:
                continue
            taken.add(best["id"])
            shot += 1
            ws.send_json({"type": "zf", "w": 0, "a": 0.0, "s": shot, "q": shot})
            ws.send_json({"type": "zh", "s": shot, "z": best["id"], "q": shot})


def test_a_family_member_and_a_guest_survive_a_level_together_and_move_up(
    client, household, players, fast_levels
):
    hh = household
    game = _zombie_game(client, hh)
    gid = game["game_id"]
    with players.websocket_connect(f"/api/games/play/{_token(game, hh.ben)}/ws") as ben:
        _state(ben)
        ben_id = _join(ben, "cap", "red")["you"]["player_id"]

        # A guest comes in through the invite link, like in the other game.
        seat = _guest_join(players, _invite_token(game), "Grandma Jo", style="beanie", color="teal")
        assert seat.status_code == 201, seat.text
        guest_id = seat.json()["player_id"]
        _until(ben, lambda m: m["type"] == "state" and len(m["game"]["players"]) == 2)

        with players.websocket_connect(f"/api/games{seat.json()['play_path']}/ws") as guest:
            _state(guest)
            _state(ben)  # the guest's phone is connected
            ben.send_json({"type": "settings", "duration": 60})
            assert _state(ben)["zombie"]["duration"] == 60
            _state(guest)
            # Only the host changes how long a level is.
            guest.send_json({"type": "settings", "duration": 300})
            assert "started the game" in _until(guest, lambda m: m["type"] == "error")["message"]

            guest.send_json({"type": "start"})  # anyone who is in can start
            assert _state(ben)["zombie"]["fighting"] == [ben_id, guest_id]
            snap = _until(ben, lambda m: m["type"] == "zs")
            assert snap["dur"] == 60 and len(snap["p"]) == 2
            guest_snap = _until(guest, lambda m: m["type"] == "zs")
            assert guest_snap["z"] == snap["z"]  # both phones have the same level

            won = _fight(ben, {ben_id: ben, guest_id: guest}, snap)
            z = won["zombie"]
            assert z["last"]["won"] is True and z["last"]["house"] > 0
            assert z["last"]["seconds"] == 60
            assert (z["level"], z["best_level"]) == (2, 1)
            assert z["coming"] == ["walker", "dog"]
            kills = z["last"]["kills"]
            assert kills[str(ben_id)] > 0 and kills[str(guest_id)] > 0
            assert z["kills"] == kills
            # Surviving is a win for everyone who fought.
            assert {p["name"]: p["wins"] for p in won["players"]} == {"Ben": 1, "Grandma Jo": 1}
            assert _until(guest, _is_state("round_over"))["game"]["zombie"]["level"] == 2

            # The hall of fame counts levels survived and the best level.
            board = client.get(f"/api/games?family_id={hh.family_id}").json()["leaderboard"]
            assert "turkey_tictactoe" not in board
            assert [(e["name"], e["kind"], e["wins"], e["best"]) for e in board[ZOMBIES]] == [
                ("Ben", "family", 1, 1),
                ("Grandma Jo", "guest", 1, 1),
            ]

            # On to level 2, then the host ends the game mid-level.
            ben.send_json({"type": "start"})
            assert _until(ben, lambda m: m["type"] == "zs")["level"] == 2
            assert zombie_live.is_live(gid)
            ben.send_json({"type": "end"})
            assert _until(ben, _is_state("ended"))
            assert _until(guest, _is_state("ended"))
            assert not zombie_live.is_live(gid)


def test_the_clock_stops_while_nobody_is_connected_and_reconnecting_gets_the_level_back(
    client, household, players, fast_levels
):
    hh = household
    game = _zombie_game(client, hh)
    path = f"/api/games/play/{_token(game, hh.ben)}/ws"
    with players.websocket_connect(path) as ben:
        _state(ben)
        me = _join(ben)["you"]["player_id"]
        ben.send_json({"type": "start"})
        snap = _until(ben, lambda m: m["type"] == "zs")
        left_at = _until(ben, lambda m: m["type"] == "zt" and m["t"] > 2)["t"]
        ben.send_json({"type": "zw", "w": 2})
        ben.send_json({"type": "zf", "w": 2, "a": 1.0, "s": 1, "q": 1})
        _until(ben, lambda m: m["type"] == "zt" and m.get("a") == {str(me): 1})

    # Away for what would be twenty seconds of the level.
    time.sleep(0.5)
    assert zombie_live.is_live(game["game_id"])
    with players.websocket_connect(path) as ben:
        assert _state(ben)["status"] == "playing"
        back = _until(ben, lambda m: m["type"] == "zs")
        assert left_at <= back["t"] < left_at + 5
        assert back["z"] == snap["z"]
        # The level remembers what this player was carrying and had used.
        assert back["p"][0]["weapon"] == 2
        assert back["ammo"][2] == za.WEAPONS["machine_gun"].ammo - 1
        assert back["ack"] == 1
        # And it carries on.
        assert _until(ben, lambda m: m["type"] == "zt")["t"] >= back["t"]

        # The Games page can end it too.
        resp = players.post(f"/api/games/{game['game_id']}/end", headers=_admin_cookie())
        assert resp.status_code == 200, resp.text
        assert _until(ben, _is_state("ended"))
        assert not zombie_live.is_live(game["game_id"])


def test_a_level_cut_short_by_a_restart_is_called_off(client, household, players, fast_levels):
    hh = household
    game = _zombie_game(client, hh)
    path = f"/api/games/play/{_token(game, hh.ben)}/ws"
    with players.websocket_connect(path) as ben:
        _state(ben)
        _join(ben)
        ben.send_json({"type": "start"})
        _until(ben, lambda m: m["type"] == "zs")

    # The server restarts: the level was only in its memory.
    zombie_live._levels.clear()
    with SessionLocal() as s:
        assert s.get(models.Game, game["game_id"]).status == "playing"

    with players.websocket_connect(path) as ben:
        assert _state(ben)["status"] == "playing"
        lobby = _until(ben, _is_state("lobby"))["game"]
        assert lobby["rounds_played"] == 0 and lobby["zombie"]["level"] == 1
        assert lobby["zombie"]["fighting"] == [] and lobby["can_start"]
        # Shots for a level that is gone are ignored, and it can be started again.
        ben.send_json({"type": "zf", "w": 0, "a": 0.0, "s": 1, "q": 1})
        ben.send_json({"type": "start"})
        assert _until(ben, lambda m: m["type"] == "zs")["level"] == 1


def test_someone_who_joins_during_a_level_watches_it_and_fights_in_the_next(
    client, household, players, fast_levels
):
    hh = household
    game = _zombie_game(client, hh, invite=[hh.katie])
    with players.websocket_connect(f"/api/games/play/{_token(game, hh.ben)}/ws") as ben:
        _state(ben)
        ben_id = _join(ben)["you"]["player_id"]
        ben.send_json({"type": "start"})
        _until(ben, lambda m: m["type"] == "zs")

        with players.websocket_connect(f"/api/games/play/{_token(game, hh.katie)}/ws") as katie:
            assert _state(katie)["status"] == "playing"
            watching = _until(katie, lambda m: m["type"] == "zs")
            assert watching["ammo"] is None and [p["id"] for p in watching["p"]] == [ben_id]
            # Red is Ben's.
            katie.send_json({"type": "join", "style": "pigtails", "color": "red"})
            assert "Ben already has" in _until(katie, lambda m: m["type"] == "error")["message"]
            joined = _join(katie, "pigtails", "pink")
            katie_id = joined["you"]["player_id"]
            assert joined["zombie"]["fighting"] == [ben_id]
            # Her shots don't count in a level she isn't fighting in.
            katie.send_json({"type": "zf", "w": 0, "a": 0.0, "s": 1, "q": 1})

            ended = _until(katie, _is_state("round_over"))["game"]
            assert ended["zombie"]["last"]["shots"] == {str(ben_id): 0}
            katie.send_json({"type": "start"})
            again = _until(katie, lambda m: m["type"] == "zs")
            assert [p["id"] for p in again["p"]] == [ben_id, katie_id]
            assert again["ammo"] is not None
            katie.send_json({"type": "end"})
            assert _until(katie, _is_state("ended"))


def test_the_other_game_is_untouched_by_zombie_messages(client, household, players):
    hh = household
    game = _start(client, hh, [hh.katie], send_invites=False)
    with players.websocket_connect(f"/api/games/play/{_token(game, hh.ben)}/ws") as ben:
        assert _state(ben)["board"] is None
        ben.send_json({"type": "zf", "w": 0, "a": 0.0, "s": 1, "q": 1})
        ben.send_json({"type": "loadout", "weapons": ["grenade", "sniper"]})
        assert _until(ben, lambda m: m["type"] == "error")
        ben.send_json({"type": "join", "style": "cap", "color": "red"})
        assert "turkey" in _until(ben, lambda m: m["type"] == "error")["message"]
        assert "zombie" not in service.game_view(
            SessionLocal().get(models.Game, game["game_id"])
        )

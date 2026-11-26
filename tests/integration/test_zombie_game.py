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


def _round(
    level=1, players=1, duration=120, seed=7, picks=("shotgun", "machine_gun"), dogs=()
):
    """A level with a clock the test moves by hand.

    ``dogs`` names each player's guard dog. Left empty the dogs are
    shut out of the level, so a test about weapons sees only what the
    weapons did.
    """
    clock = _Clock()
    rnd = za.Round(
        level=level,
        duration=duration,
        seed=seed,
        players=[(i + 1, picks, *dogs[i : i + 1]) for i in range(players)],
        clock=clock,
    )
    if not dogs:
        rnd.dogs.clear()
    return rnd, clock


def _advance(rnd, clock, seconds: float, step: float = 0.1) -> None:
    for _ in range(int(round(seconds / step))):
        rnd.advance(step)
        clock.now += step


def _side_of(y: float) -> int:
    return za.TOP if y < za.HOUSE_Y else za.BOTTOM


def _shoot(rnd, clock, player_id: int, zombie, *, weapon: int = 0, shot: int) -> bool:
    """Step in front of ``zombie``, fire a single-hit weapon and report the hit."""
    x, y = zombie.position(rnd.t)
    fired = rnd.fire(
        player_id, {"w": weapon, "x": x, "d": _side_of(y), "s": shot, "q": shot}
    )
    return fired and rnd.hit(player_id, {"s": shot, "z": zombie.id, "q": shot})


def _out(zombie, t: float) -> float:
    """How far out from the wall it is heading for a zombie is."""
    _x, y = zombie.position(t)
    return (y - za.track_y(_side_of(y))) * _side_of(y)


def _defend(rnd, clock, *, until: Optional[float] = None) -> None:
    """Every player shoots the most pressing zombie in reach with the crossbow."""
    crossbow = za.WEAPONS["crossbow"]
    shot = 0
    while rnd.over is None and (until is None or rnd.t < until):
        _advance(rnd, clock, crossbow.reload)
        reachable = sorted(
            (z for z in rnd._active.values() if _out(z, rnd.t) <= crossbow.range),
            key=lambda z: z.arrive,
        )
        for player_id, zombie in zip(rnd.fighters, reachable):
            shot += 1
            _shoot(rnd, clock, player_id, zombie, shot=shot)


def _plant(rnd, x: float, y: float, *, kind: str = "walker", hp: float = 500, dps: float = 5):
    """A zombie standing at ``(x, y)``, as if it had walked there long ago."""
    away = -40.0 if y < za.HOUSE_Y else za.WORLD_H + 40.0
    zombie = za.Zombie(
        9000 + len(rnd._by_id), za.ZOMBIE_TYPES[kind], t0=rnd.t - 500,
        path=[(x, away), (x, y)], speed=20, hp=hp, dps=dps,
    )
    rnd._active[zombie.id] = rnd._by_id[zombie.id] = zombie
    return zombie


def _clear(rnd) -> None:
    """Take every zombie off the map and stop new ones arriving."""
    rnd._active.clear()
    rnd._next = len(rnd.zombies)
    rnd._waiting.clear()


# ---------------------------------------------------------------------------
# Weapons
# ---------------------------------------------------------------------------


def test_every_weapon_has_a_reach_a_speed_a_damage_and_is_single_hit_or_area():
    w = za.WEAPONS
    assert list(w) == [
        "crossbow", "machine_gun", "sniper", "shotgun", "flamethrower", "grenade",
        "snowball", "boulder",
    ]
    for weapon in w.values():
        assert weapon.range > 0 and weapon.speed > 0 and weapon.reload > 0
        assert weapon.damage > 0 or weapon.freeze > 0
    # Each has a reload time of its own.
    assert len({weapon.reload for weapon in w.values()}) == len(w)

    single = {k for k, weapon in w.items() if not weapon.area}
    assert single == {"crossbow", "machine_gun", "sniper"}

    # The crossbow is the one that never runs out.
    assert [k for k, weapon in w.items() if weapon.ammo is None] == ["crossbow"]
    # Machine gun: single hit, the fastest shots of the three.
    assert w["machine_gun"].reload < w["crossbow"].reload < w["sniper"].reload
    # Sniper rifle: slowest to reload, reaches furthest — right to the
    # edge of the map, which nothing else that is aimed does.
    assert w["sniper"].reload == max(x.reload for x in w.values())
    assert w["sniper"].range == max(x.range for x in w.values())
    to_edge = za.track_y(za.TOP)
    assert to_edge == za.WORLD_H - za.track_y(za.BOTTOM)
    assert w["sniper"].range > to_edge > w["machine_gun"].range > w["crossbow"].range
    # Flamethrower: area damage, but a shorter reach than anything else.
    assert w["flamethrower"].range == min(x.range for x in w.values())
    assert w["flamethrower"].range < w["shotgun"].range < w["crossbow"].range
    # Grenade: the big blast.
    assert w["grenade"].kind == "lob" and w["grenade"].radius >= 60
    # Snowball: freezes an area and hurts nobody.
    assert w["snowball"].kind == "lob" and w["snowball"].damage == 0
    assert w["snowball"].freeze >= 3 and w["snowball"].radius >= 60
    # Boulder: rolls right across the map; everybody has two, no more.
    assert w["boulder"].kind == "roll" and w["boulder"].ammo == 2 and w["boulder"].given
    assert za.GIVEN_WEAPONS == ("boulder",)


def test_a_loadout_is_the_crossbow_two_picked_weapons_and_two_boulders():
    assert za.clean_loadout(["grenade", "sniper"]) == ["grenade", "sniper"]
    assert za.clean_loadout(["snowball", "flamethrower"]) == ["snowball", "flamethrower"]
    for bad in (["grenade"], ["grenade", "grenade"], ["crossbow", "sniper"],
                ["boulder", "sniper"], ["grenade", "sniper", "shotgun"],
                ["grenade", "laser"], "grenade", None):
        assert za.clean_loadout(bad) is None, bad

    rnd, _ = _round(picks=("grenade", "sniper"))
    fighter = rnd.fighters[1]
    assert fighter.weapons == ["crossbow", "grenade", "sniper", "boulder"]
    assert fighter.ammo == [None, za.WEAPONS["grenade"].ammo, za.WEAPONS["sniper"].ammo, 2]
    # A longer level comes with more ammunition, but never more boulders.
    longer, _ = _round(duration=300, picks=("grenade", "sniper"))
    assert longer.fighters[1].ammo[1] > fighter.ammo[1]
    assert longer.fighters[1].ammo[3] == 2
    shorter, _ = _round(duration=60, picks=("grenade", "sniper"))
    assert shorter.fighters[1].ammo[3] == 2


def test_shots_use_up_ammunition_and_cannot_be_fired_faster_than_the_weapon_reloads():
    rnd, clock = _round(picks=("sniper", "machine_gun"))
    _advance(rnd, clock, za.COUNTDOWN_SECONDS + 1)
    shot = {"w": 1}

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
        fired += rnd.fire(1, {"w": 2})
    assert fired == za.WEAPONS["machine_gun"].ammo
    assert rnd.fighters[1].ammo[2] == 0
    for _ in range(50):
        clock.now += za.WEAPONS["crossbow"].reload
        assert rnd.fire(1, {"w": 0})

    # Nonsense is refused without fuss.
    for bad in ({"w": 7}, {"w": -1}, {"w": "0"}, {"w": True}, {"w": None}, {}):
        clock.now += 1
        assert not rnd.fire(1, bad)
    assert not rnd.fire(99, {"w": 0})


def test_a_weapon_that_was_just_switched_to_is_ready_to_fire():
    rnd, clock = _round(picks=("sniper", "grenade"))
    _advance(rnd, clock, za.COUNTDOWN_SECONDS + 1)
    assert rnd.fire(1, {"w": 1})
    assert not rnd.fire(1, {"w": 1})  # reloading

    # Any time, even while the last weapon is reloading.
    rnd.switch(1, {"w": 2})
    assert rnd.fighters[1].weapon == 2
    assert rnd.fire(1, {"w": 2})
    assert not rnd.fire(1, {"w": 2})
    # Switching back: ready again.
    rnd.switch(1, {"w": 1})
    assert rnd.fire(1, {"w": 1})
    # Firing a weapon other than the one in hand switches to it.
    assert rnd.fire(1, {"w": 0})
    assert rnd.fighters[1].weapon == 0
    assert rnd.drain()["w"] == [[1, 0]]
    # Staying on a weapon doesn't make it ready.
    rnd.switch(1, {"w": 0})
    assert not rnd.fire(1, {"w": 0})
    assert rnd.fighters[1].ammo == [None, za.WEAPONS["sniper"].ammo - 2, za.WEAPONS["grenade"].ammo - 1, 2]


def test_nobody_can_shoot_before_the_countdown_ends():
    rnd, clock = _round()
    assert rnd.t == -za.COUNTDOWN_SECONDS
    assert not rnd.fire(1, {"w": 0})
    _advance(rnd, clock, za.COUNTDOWN_SECONDS + 0.2)
    assert rnd.fire(1, {"w": 0})


def test_a_hit_only_counts_for_a_shot_that_was_fired_and_could_reach():
    rnd, clock = _round()
    _advance(rnd, clock, za.COUNTDOWN_SECONDS + 5)
    _clear(rnd)
    top = za.track_y(za.TOP)
    near = _plant(rnd, 150, top - 60, hp=100)
    beside = _plant(rnd, 260, top - 60, hp=100)
    behind = _plant(rnd, 150, za.track_y(za.BOTTOM) + 60, hp=100)
    far = _plant(rnd, 150, top - 280, hp=100)
    aim = {"w": 0, "x": 150, "d": za.TOP}

    # No such shot.
    assert not rnd.hit(1, {"s": 1, "z": near.id})
    # A real shot hits once, not twice.
    assert rnd.fire(1, {**aim, "s": 1})
    assert rnd.hit(1, {"s": 1, "z": near.id})
    assert near.hp == 100 - za.WEAPONS["crossbow"].damage
    assert not rnd.hit(1, {"s": 1, "z": near.id})

    # Shots go straight out from where the player stood: they can't hit
    # a zombie off to one side, one at the other wall, or one further
    # away than the weapon reaches.
    for shot, zombie in enumerate((beside, behind, far), start=2):
        clock.now += 1
        assert rnd.fire(1, {**aim, "s": shot})
        assert not rnd.hit(1, {"s": shot, "z": zombie.id})
        assert zombie.hp == 100
    # The sniper rifle does reach that far.
    clock.now += 1
    rnd2, clock2 = _round(picks=("sniper", "grenade"))
    _advance(rnd2, clock2, za.COUNTDOWN_SECONDS + 5)
    _clear(rnd2)
    distant = _plant(rnd2, 150, top - 280, hp=100)
    assert rnd2.fire(1, {"w": 1, "x": 150, "d": za.TOP, "s": 1})
    assert rnd2.hit(1, {"s": 1, "z": distant.id}) and not distant.alive

    # A zombie that isn't on the map yet can't be hit.
    rnd3, clock3 = _round()
    _advance(rnd3, clock3, za.COUNTDOWN_SECONDS + 5)
    later = rnd3.zombies[-1]
    assert later.t0 > rnd3.t
    assert rnd3.fire(1, {"w": 0, "x": later.x0, "d": _side_of(later.y0), "s": 2})
    assert not rnd3.hit(1, {"s": 2, "z": later.id})
    # A bullet reported long after it would have landed is ignored.
    clock.now += 1
    assert rnd.fire(1, {**aim, "s": 20})
    clock.now += 10
    assert not rnd.hit(1, {"s": 20, "z": near.id})


def test_weapons_fire_straight_out_from_wherever_the_player_stands():
    rnd, clock = _round(players=2, picks=("shotgun", "grenade"))
    _advance(rnd, clock, za.COUNTDOWN_SECONDS + 5)
    _clear(rnd)
    rnd.drain()
    up = _plant(rnd, 100, za.HOUSE_TOP - 40)
    down = _plant(rnd, 100, za.HOUSE_BOTTOM + 40)
    elsewhere = _plant(rnd, 250, za.HOUSE_TOP - 40)

    # Player 1 starts on the top wall. From x=100 the shotgun catches
    # the zombie above, not the one below or the one along the wall.
    assert rnd.fighters[1].side == za.TOP
    rnd.move(1, {"x": 100, "d": za.TOP})
    assert rnd.fire(1, {"w": 1})
    assert up.hp < 500 and down.hp == elsewhere.hp == 500
    # Over to the bottom wall: now it fires down the map.
    rnd.move(1, {"x": 100, "d": za.BOTTOM})
    clock.now += 1
    assert rnd.fire(1, {"w": 1})
    assert down.hp < 500 and elsewhere.hp == 500
    # A shot can say where it was fired from.
    clock.now += 1
    assert rnd.fire(1, {"w": 1, "x": 250, "d": za.TOP})
    assert elsewhere.hp < 500
    assert (rnd.fighters[1].x, rnd.fighters[1].side) == (250.0, za.TOP)

    # Everyone is told where the players are and where shots came from.
    batch = rnd.drain()
    assert batch["m"] == [[1, 250.0, za.TOP]]
    assert batch["f"] == [[1, 1, 100.0, za.TOP, 0.0], [1, 1, 100.0, za.BOTTOM, 0.0], [1, 1, 250.0, za.TOP, 0.0]]

    # Players stay on the walls: no further than the ends, and nowhere
    # that isn't a wall.
    rnd.move(2, {"x": -500, "d": za.BOTTOM})
    assert rnd.fighters[2].x == za.TRACK_LEFT
    rnd.move(2, {"x": 9999, "d": za.TOP})
    assert (rnd.fighters[2].x, rnd.fighters[2].side) == (za.TRACK_RIGHT, za.TOP)
    for bad in ({"x": 100, "d": 0}, {"x": 100, "d": 2}, {"x": "100", "d": 1}, {"d": 1},
                {"x": float("inf"), "d": 1}, {"x": 100, "d": True}, {}):
        rnd.move(2, bad)
        assert (rnd.fighters[2].x, rnd.fighters[2].side) == (za.TRACK_RIGHT, za.TOP)
    rnd.move(99, {"x": 100, "d": 1})
    assert rnd.fighters[2].y == za.track_y(za.TOP) and za.HOUSE_TOP < rnd.fighters[2].y < za.HOUSE_Y


def test_single_hit_weapons_hurt_one_zombie_and_area_weapons_hurt_the_crowd():
    rnd, clock = _round(players=2, picks=("shotgun", "grenade"))
    _advance(rnd, clock, za.COUNTDOWN_SECONDS + 5)
    _clear(rnd)
    wall = za.HOUSE_TOP
    crowd = [_plant(rnd, 150 + dx, wall - gap) for dx, gap in
             ((0, 12), (-14, 20), (16, 16), (-6, 34), (10, 40), (0, 60))]
    apart = _plant(rnd, 290, wall - 12)
    rnd.drain()

    # The crossbow: one bolt, one zombie.
    assert _shoot(rnd, clock, 1, crowd[0], shot=1)
    assert [d[0] for d in rnd.drain()["d"]] == [crowd[0].id]

    # The shotgun catches several of them in one blast...
    clock.now += 1
    assert rnd.fire(1, {"w": 1, "x": 150, "d": za.TOP})
    hurt = {d[0] for d in rnd.drain()["d"]}
    assert len(hurt) >= 4 and hurt <= {z.id for z in crowd}

    # ...and a grenade goes off a moment after it is thrown, where it
    # was thrown to, and catches all of them.
    before = [z.hp for z in crowd]
    clock.now += 1
    assert rnd.fire(1, {"w": 2, "x": 150, "d": za.TOP, "r": 40})
    assert rnd.drain()["f"] == [[1, 2, 150.0, za.TOP, 40.0]]
    assert [z.hp for z in crowd] == before  # still in the air
    _advance(rnd, clock, 0.5)
    assert {d[0] for d in rnd.drain()["d"]} == {z.id for z in crowd}
    assert apart.hp == 500

    # Thrown without a distance it lands a fair way out; never further
    # than it can be thrown, and never at the thrower's feet.
    for said, lands in ((None, 0.6 * za.WEAPONS["grenade"].range), (5000, za.WEAPONS["grenade"].range), (-3, 30.0)):
        clock.now += 2
        message = {"w": 2, "x": 150, "d": za.TOP}
        if said is not None:
            message["r"] = said
        assert rnd.fire(1, message)
        assert rnd.drain()["f"][0][4] == pytest.approx(lands)
        _advance(rnd, clock, 1.5)


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


def test_there_are_zombies_dogs_bats_ogres_and_vampires():
    z = za.ZOMBIE_TYPES
    # New kinds go on the end: the phones are sent a kind as its place here.
    assert list(z) == ["walker", "dog", "bat", "ogre", "vampire"]
    # Ogres are big, slow and strong; dogs are small and quick; bats fly.
    assert z["ogre"].hp > 5 * z["walker"].hp and z["ogre"].radius > z["walker"].radius
    assert z["ogre"].speed < z["walker"].speed < z["dog"].speed
    assert z["dog"].radius < z["walker"].radius and z["dog"].hp < z["walker"].hp
    assert [k for k, kind in z.items() if kind.flying] == ["bat"]
    # Vampires are quicker than zombies, harder on the walls, and the
    # only ones that zigzag and that come one at a time.
    assert z["vampire"].speed > 2 * z["walker"].speed
    assert z["walker"].dps * 2 < z["vampire"].dps < z["ogre"].dps
    assert z["walker"].hp < z["vampire"].hp < z["ogre"].hp
    assert [k for k, kind in z.items() if kind.zigzag] == ["vampire"]
    assert [k for k, kind in z.items() if kind.lone] == ["vampire"]


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
        assert (x, y) == pytest.approx((z.tx, z.ty))
        assert za.HOUSE_LEFT - 15 <= x <= za.HOUSE_RIGHT + 15
        assert za.HOUSE_TOP - 50 <= y <= za.HOUSE_BOTTOM + 50
        # Those from the top attack the top of the house.
        assert (y < za.HOUSE_Y) == (z.y0 < 0)
        assert 0 < z.t0 < 120

    # Waves: bursts of zombies with quiet gaps between them.
    gaps = [b.t0 - a.t0 for a, b in zip(zombies, zombies[1:])]
    assert max(gaps) > 3.0
    assert sum(1 for g in gaps if g > 2.0) >= 5


@pytest.mark.parametrize("level,team", [(1, 1), (4, 2), (8, 2)])
def test_a_level_starts_with_a_few_zombies_and_builds_to_a_crowd(level, team):
    for seed in range(5):
        zombies = za.build_level(level, team, 120, seed)
        quarters = [sum(1 for z in zombies if q * 30 <= z.t0 < (q + 1) * 30) for q in range(4)]
        # More in every quarter of the level than in the one before.
        assert quarters[0] < quarters[1] < quarters[2], quarters
        assert quarters[3] > 2.5 * quarters[0], quarters
        # The opening wave is a handful...
        assert 1 <= sum(1 for z in zombies if z.t0 < 6) <= 4 * team + level
        # ...and the last ones are dozens strong.
        assert quarters[3] >= 30


def test_dozens_of_zombies_are_on_the_map_at_once_and_fit_around_the_house():
    rnd, clock = _round(level=9, players=2, seed=4)
    most = 0
    while rnd.over is None:
        _advance(rnd, clock, 1.0)
        most = max(most, len(rnd._active))
    assert most >= 40

    # Small enough that a crowd fits: fifty zombies shoulder to shoulder
    # cover less ground than there is for them to stand on at the walls.
    walker = za.ZOMBIE_TYPES["walker"]
    standing_room = 2 * za.HOUSE_W * (za.PILE_DEPTH + 2 * walker.radius)
    assert 50 * math.pi * walker.radius ** 2 < standing_room
    assert walker.radius * 2 <= za.WORLD_W / 20


def test_trees_stand_on_each_side_of_the_house_and_zombies_walk_around_them():
    bent = 0
    for seed in range(12):
        trees = za.build_trees(seed)
        assert trees == za.build_trees(seed)
        top = [t for t in trees if t[1] < za.HOUSE_TOP]
        bottom = [t for t in trees if t[1] > za.HOUSE_BOTTOM]
        assert len(top) == len(bottom) == za.TREES_PER_SIDE == 3
        for x, y, r in trees:
            assert r < x < za.WORLD_W - r and r < y < za.WORLD_H - r
            # Clear of the house and of the crowd at its walls.
            assert min(abs(y - za.HOUSE_TOP), abs(y - za.HOUSE_BOTTOM)) > r + za.PILE_DEPTH + 30
        for i, a in enumerate(trees):
            for b in trees[i + 1:]:
                # Room for an ogre to walk between any two.
                gap = math.dist(a[:2], b[:2]) - a[2] - b[2]
                assert gap > 2 * za.ZOMBIE_TYPES["ogre"].radius

        for z in za.build_level(6, 2, 120, seed):
            if z.flying:
                assert len(z.path) == 2  # bats fly straight over
                continue
            bent += len(z.path) > 2
            t = z.t0
            while t < z.arrive + 0.5:
                x, y = z.position(t)
                for tx, ty, r in trees:
                    assert math.dist((x, y), (tx, ty)) >= r + z.radius, (seed, z.id, t)
                t += 0.2
    assert bent > 200

    # The phones are told where the trees are.
    rnd, _ = _round(seed=5)
    assert rnd.snapshot(1)["trees"] == [list(t) for t in za.build_trees(5)]


def test_levels_bring_new_kinds_and_tougher_faster_more_numerous_zombies():
    def kinds(level):
        return {z.type.key for seed in range(6) for z in za.build_level(level, 1, 120, seed)}

    # Dogs from the first level, bats from the second. (Every level
    # has its ogres and its vampires; see the tests after this one.)
    assert kinds(1) == {"walker", "dog", "ogre", "vampire"}
    assert kinds(2) == {"walker", "dog", "bat", "ogre", "vampire"}
    assert kinds(6) == {"walker", "dog", "bat", "ogre", "vampire"}
    assert za.kinds_coming(1, 1) == ["walker", "dog", "ogre", "vampire"]
    assert za.kinds_coming(2, 2) == ["walker", "dog", "bat", "ogre", "vampire"]
    # The waves are made of the first three, and from level 4 of ogres too.
    assert za.difficulty(1, 1)["types"] == ["walker", "dog"]
    assert za.difficulty(3, 1)["types"] == ["walker", "dog", "bat"]
    assert za.difficulty(4, 1)["types"] == ["walker", "dog", "bat", "ogre"]

    previous = None
    for level in range(1, 15):
        diff = za.difficulty(level, 1)
        # An ogre counts for several zombies.
        count = sum(
            z.type.threat for seed in range(8) for z in za.build_level(level, 1, 120, seed)
        )
        if previous is not None:
            assert diff["hp"] > previous["hp"]                    # strength
            assert diff["speed"] >= previous["speed"]             # speed
            assert diff["wave_budget"] > previous["wave_budget"]  # how many at once
            assert diff["wave_every"] <= previous["wave_every"]
            assert count > previous["count"] * 0.97
        previous = {**diff, "count": count}

    # A level-9 walker really is tougher and quicker than a level-1 one.
    early = next(z for z in za.build_level(1, 1, 120, 1) if z.type.key == "walker")
    late = next(z for z in za.build_level(9, 1, 120, 1) if z.type.key == "walker")
    assert late.max_hp > early.max_hp and late.dps > early.dps

    # More players, more zombies.
    solo = sum(len(za.build_level(4, 1, 120, s)) for s in range(6))
    pair = sum(len(za.build_level(4, 2, 120, s)) for s in range(6))
    assert pair > 1.4 * solo
    # Two is as many as can play: a third is left out of the level.
    assert za.MAX_PLAYERS == 2
    crowded, _ = _round(players=3)
    assert list(crowded.fighters) == [1, 2]


def test_ogres_come_from_half_way_two_for_each_player_and_more_on_higher_levels():
    # Two each to begin with, and another each every few levels.
    for level, each in [(1, 2), (2, 2), (4, 2), (5, 3), (8, 3), (9, 4), (13, 5), (17, 6), (40, 6)]:
        assert za.difficulty(level, 1)["bosses"] == each, level
        assert za.difficulty(level, 2)["bosses"] == 2 * each, level

    for level in (1, 2, 5, 9):
        for team in (1, 2):
            bosses = za.difficulty(level, team)["bosses"]
            for duration in (60, 120, 300):
                for seed in range(4):
                    zombies = za.build_level(level, team, duration, seed)
                    ogres = [z for z in zombies if z.type.key == "ogre"]
                    late = [z for z in ogres if z.t0 >= duration / 2]
                    where = (level, team, duration, seed)
                    assert len(late) >= bosses >= 2 * team, where
                    # The first sets off as the clock reaches half-way.
                    assert late[0].t0 == duration / 2, where
                    if level < za.ZOMBIE_TYPES["ogre"].from_level:
                        # No others this early: none in the first half,
                        # at one wall and then the other in the second.
                        assert ogres == late and len(ogres) == bosses, where
                        sides = [z.y0 < 0 for z in ogres]
                        assert all(a != b for a, b in zip(sides, sides[1:])), where
                        gaps = [b.t0 - a.t0 for a, b in zip(ogres, ogres[1:])]
                        assert min(gaps) >= za.BOSS_STAGGER - 0.01, where
                        if duration >= 120:
                            # Spread over what is left of the level, the
                            # last reaching the house in time to be a fight.
                            assert max(gaps) - min(gaps) < 6, where
                            assert duration - 20 <= ogres[-1].arrive <= duration - 12, where

    # The first levels' ogres are young ones, still far stronger than
    # anything else on the map; from level 4 they are full-grown.
    ogre = za.ZOMBIE_TYPES["ogre"]
    strength = {}
    for level in (1, 2, 3, 4, 5):
        late = [
            z for z in za.build_level(level, 1, 120, 1)
            if z.type.key == "ogre" and z.t0 >= 60
        ]
        strength[level] = min(z.max_hp for z in late) / (ogre.hp * za.difficulty(level, 1)["hp"])
    assert 0.5 < strength[1] < strength[2] < strength[3] < strength[4]
    assert strength[4] == pytest.approx(1.0, abs=0.01) == pytest.approx(strength[5], abs=0.01)
    assert ogre.hp * za.difficulty(1, 1)["boss_hp"] > 5 * za.ZOMBIE_TYPES["walker"].hp


def test_a_vampire_zigzags_in_quicker_than_a_zombie_and_is_harder_on_the_walls():
    seen = swung = 0
    quicker = []
    for seed in range(8):
        for level, duration in ((1, 120), (1, 60), (3, 180), (8, 300)):
            zombies = za.build_level(level, 2, duration, seed)
            vampires = [z for z in zombies if z.type.key == "vampire"]
            walkers = [z for z in zombies if z.type.key == "walker"]
            # From the very first level, and more than once in it.
            assert len(vampires) >= 2, (seed, level, duration)
            assert all(0 < z.t0 <= duration - za.VAMPIRE_LAST for z in vampires)
            # For all its darting about, the slowest of them is at the
            # house before the quickest zombie would be.
            took = [v.arrive - v.t0 for v in vampires]
            walked = [w.arrive - w.t0 for w in walkers]
            assert max(took) < min(walked)
            quicker.append((sum(took) / len(took)) / (sum(walked) / len(walked)))
            for v in vampires:
                seen += 1
                assert v.dps > 2 * max(w.dps for w in walkers)
                assert v.max_hp > 3 * max(w.max_hp for w in walkers)
                # Side to side: the corners of its rail turn one way and
                # then the other, well off the straight line...
                xs = [x for x, _y in v.path]
                turns = [b - a for a, b in zip(xs, xs[1:]) if abs(b - a) > 12]
                swings = sum(1 for a, b in zip(turns, turns[1:]) if (a > 0) != (b > 0))
                assert swings >= 2, (seed, level, v.path)
                swung += swings
                assert max(xs) - min(xs) > 25
                # Which is a good deal further than the straight way.
                assert v._dist > 1.1 * math.dist(v.path[0], v.path[-1])
                assert all(v.radius <= x <= za.WORLD_W - v.radius for x in xs[1:])
                # ...and nearer the house all the while (but for a step
                # back round a tree), to a spot at its wall.
                out = [abs(y - za.HOUSE_Y) for _x, y in v.path]
                assert out[0] == max(out) and out[-1] == min(out)
                assert all(b < a + 25 for a, b in zip(out, out[1:]))
                assert za.HOUSE_LEFT <= v.tx <= za.HOUSE_RIGHT
                assert za.HOUSE_H / 2 < out[-1] <= za.HOUSE_H / 2 + za.PILE_DEPTH + v.radius
    assert seen > 100 and swung / seen > 3.5
    # In about half the time, on the whole.
    assert sum(quicker) / len(quicker) < 0.55 and max(quicker) < 0.75
    # No two come in alike.
    a, b = [z.path for z in za.build_level(1, 1, 120, 2) if z.type.key == "vampire"][:2]
    assert [round(x - a[0][0]) for x, _ in a] != [round(x - b[0][0]) for x, _ in b]


def test_only_one_vampire_is_out_at_a_time():
    rnd, clock = _round(level=1, players=2, seed=9)
    # Nobody is going to defend this house, and it has to last the level.
    rnd.house = 1e7
    planned = list(rnd._waiting)
    assert len(planned) >= 3 and all(z.type.key == "vampire" for z in planned)
    # The phones aren't told about one until it sets off.
    assert not any(z.type.key == "vampire" for z in rnd.zombies)
    snap = rnd.snapshot(1)
    assert {row[0] for row in snap["z"]}.isdisjoint(z.id for z in planned)
    assert len(snap["z"]) == len(rnd.zombies)

    first, second, third = planned[:3]
    due = second.t0
    _advance(rnd, clock, za.COUNTDOWN_SECONDS + first.t0 - 0.3)
    rnd.drain()
    assert rnd._lone is None
    _advance(rnd, clock, 0.5)
    # Out it comes, when the plan said, and the phones get its rail.
    assert rnd._lone is first and first.id in rnd._active
    batch = rnd.drain()
    assert batch["z"] == [first.wire()]
    assert first.wire()[1] == za.ZOMBIE_KEYS.index("vampire")
    assert rnd.drain() is None or "z" not in rnd.drain()
    # It is in the list where it belongs: the order they set off in.
    assert [z.t0 for z in rnd.zombies] == sorted(z.t0 for z in rnd.zombies)
    assert first in rnd.zombies and first.wire() in rnd.snapshot(1)["z"]

    # Nobody kills it. It gets to the house and sets about the wall;
    # the next one's time comes and goes, and it stays away.
    _advance(rnd, clock, due + 6 - rnd.t)
    out = [z for z in rnd._active.values() if z.type.key == "vampire"]
    assert out == [first] and rnd.t > first.arrive
    assert second.t0 == due and second.id not in rnd._active
    for _ in range(40):
        _advance(rnd, clock, 0.5)
        assert sum(z.type.key == "vampire" for z in rnd._active.values()) == 1
    assert rnd._waiting[0] is second

    # Kill it, and after a moment's peace the next sets off.
    rnd.drain()
    rnd._damage(first, 10_000, rnd.fighters[1])
    killed = rnd.t
    assert not first.alive and rnd._lone is None
    _advance(rnd, clock, za.VAMPIRE_REST - 0.5)
    assert second.id not in rnd._active
    assert not any("z" in b for b in [rnd.drain()] if b)
    _advance(rnd, clock, 1.0)
    assert rnd._lone is second and second.id in rnd._active
    # From now, not from when it was due: it starts at the edge of the map.
    assert killed + za.VAMPIRE_REST <= second.t0 <= rnd.t
    assert second.position(second.t0) == pytest.approx((second.x0, second.y0))
    assert second.arrive == pytest.approx(second.t0 + second._dist / second.speed)
    assert rnd.drain()["z"] == [second.wire()]
    snap = rnd.snapshot(2)
    ids = [row[0] for row in snap["z"]]
    assert first.id in ids and second.id in ids and third.id not in ids
    assert first.id in snap["dead"]
    assert [z.t0 for z in rnd.zombies] == sorted(z.t0 for z in rnd.zombies)

    # Killed on its way in, with the third already due: the third still
    # waits its moment, and comes alone.
    rnd._damage(second, 10_000, rnd.fighters[2])
    assert rnd.fighters[2].kills >= 1
    _advance(rnd, clock, max(0.0, third.t0 - rnd.t) + za.VAMPIRE_REST + 0.5)
    assert rnd._lone is third
    assert sum(z.type.key == "vampire" for z in rnd._active.values()) == 1


def test_a_vampire_left_alone_takes_the_wall_down_faster_than_a_zombie():
    rnd, clock = _round()
    _advance(rnd, clock, za.COUNTDOWN_SECONDS + 1)
    _clear(rnd)
    _plant(rnd, 150, za.HOUSE_TOP - 10, dps=za.ZOMBIE_TYPES["walker"].dps)
    _advance(rnd, clock, 10)
    to_a_walker = 1000 - rnd.house
    _clear(rnd)
    _plant(rnd, 150, za.HOUSE_TOP - 10, kind="vampire", dps=za.ZOMBIE_TYPES["vampire"].dps)
    house = rnd.house
    _advance(rnd, clock, 10)
    assert house - rnd.house > 2.5 * to_a_walker > 0


def test_bats_weave_as_they_fly_in_and_zombies_walk_straight():
    zombies = za.build_level(5, 2, 120, seed=2)
    bat = next(z for z in zombies if z.flying)
    walker = next(z for z in zombies if z.type.key == "walker" and len(z.path) == 2)

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
# Snowballs and boulders
# ---------------------------------------------------------------------------


def test_a_snowball_freezes_the_group_where_it_lands_and_hurts_nobody():
    rnd, clock = _round(players=2, picks=("snowball", "grenade"))
    snowball = za.WEAPONS["snowball"]
    _advance(rnd, clock, za.COUNTDOWN_SECONDS + 5)
    _clear(rnd)
    wall = za.HOUSE_TOP
    crowd = [_plant(rnd, 150 + dx, wall - gap) for dx, gap in
             ((0, 12), (-14, 20), (16, 16), (-6, 34), (10, 40), (40, 30))]
    apart = _plant(rnd, 290, wall - 12)
    other_wall = _plant(rnd, 150, za.HOUSE_BOTTOM + 12)
    rnd.drain()

    thrown_at = rnd.t
    assert rnd.fire(1, {"w": 1, "x": 150, "d": za.TOP, "r": 35})
    assert all(z.until < rnd.t for z in crowd)  # still in the air
    _advance(rnd, clock, 0.5)
    batch = rnd.drain()
    frozen = {entry[0]: entry for entry in batch["fr"]}
    assert set(frozen) == {z.id for z in crowd}
    # Nobody was hurt, let alone killed.
    assert "d" not in batch
    assert all(z.alive and z.hp == z.max_hp for z in crowd)
    assert rnd.fighters[1].kills == 0

    # Frozen zombies' clocks stop...
    for z in crowd:
        _id, stop, until = frozen[z.id]
        assert thrown_at < until - snowball.freeze < thrown_at + 1
        assert z.clock(rnd.t) == stop == z.clock(rnd.t + 2)
    # ...so they leave the house alone, while the others carry on.
    rnd._active.pop(apart.id)
    rnd._active.pop(other_wall.id)
    house = rnd.house
    _advance(rnd, clock, snowball.freeze - 1.5)
    assert rnd.house == house
    # Until they thaw.
    _advance(rnd, clock, 4)
    assert rnd.house < house
    # A phone that connects now is told who was held up, and for how long.
    assert {entry[0] for entry in rnd.snapshot(1)["fr"]} == set(frozen)

    # One caught on its way in stops where it is, then carries on from there.
    walker = za.Zombie(9999, za.ZOMBIE_TYPES["walker"], t0=rnd.t - 3,
                       path=[(60, -20), (90, 120), (80, 250)], speed=22, hp=30, dps=3)
    here = walker.position(rnd.t)
    walker.freeze(rnd.t, snowball.freeze)
    assert walker.position(rnd.t + snowball.freeze - 1) == walker.position(rnd.t + 1)
    assert walker.position(rnd.t + 1) == pytest.approx(here, abs=0.5)
    assert walker.position(walker.until) == pytest.approx(here, abs=0.5)
    assert math.dist(walker.position(walker.until + 2), here) == pytest.approx(2 * walker.speed, rel=0.1)

    # A second snowball on a frozen zombie keeps it frozen longer.
    walker.freeze(100.0, 6.0)
    walker.freeze(103.0, 6.0)
    assert walker.until == 109.0
    assert walker.clock(108.9) == walker.stop
    assert walker.clock(110.0) == pytest.approx(walker.stop + 1.0)


def test_a_boulder_rolls_straight_out_and_flattens_everything_in_its_way():
    rnd, clock = _round(players=2)
    boulder = za.WEAPONS["boulder"]
    _advance(rnd, clock, za.COUNTDOWN_SECONDS + 5)
    _clear(rnd)
    rnd.drain()
    wall = za.HOUSE_TOP
    in_its_way = [
        _plant(rnd, 150, wall - 15),
        _plant(rnd, 160, wall - 70, kind="dog"),
        _plant(rnd, 138, wall - 150, kind="bat"),
        _plant(rnd, 150, wall - 240, kind="ogre", hp=900),
    ]
    beside = [_plant(rnd, 150 + 45, wall - 15), _plant(rnd, 150 - 60, wall - 150, kind="ogre")]
    behind = _plant(rnd, 150, za.HOUSE_BOTTOM + 15)

    assert rnd.fire(1, {"w": 3, "x": 150, "d": za.TOP})
    assert rnd.drain()["f"] == [[1, 3, 150.0, za.TOP, 0.0]]
    # It takes its time: the nearest is flattened first, the furthest last.
    _advance(rnd, clock, 0.2)
    assert [z.alive for z in in_its_way] == [False, True, True, True]
    _advance(rnd, clock, 0.7)
    assert [z.alive for z in in_its_way] == [False, False, False, True]
    _advance(rnd, clock, 1.0)
    # Everything in its path, whatever it is and however strong...
    assert not any(z.alive for z in in_its_way)
    assert rnd.fighters[1].kills == 4
    # ...and nothing beside it, or at the other wall.
    assert all(z.alive and z.hp == z.max_hp for z in [*beside, behind])
    # It rolls off the edge of the map and is gone.
    _advance(rnd, clock, 2.0)
    assert rnd._rolls == []

    # From the bottom wall it rolls down the map.
    clock.now += 2
    assert rnd.fire(1, {"w": 3, "x": 150, "d": za.BOTTOM})
    _advance(rnd, clock, 0.5)
    assert not behind.alive

    # Two a level, however long the level and whatever else is carried.
    clock.now += 2
    assert not rnd.fire(1, {"w": 3})
    assert rnd.fighters[1].ammo[3] == 0 and rnd.fighters[2].ammo[3] == 2


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


@pytest.mark.parametrize("team", [1, 2])
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
    # One starts on the top wall and one on the bottom.
    assert [p["side"] for p in snap["p"]] == [za.TOP, za.BOTTOM]
    assert all(za.TRACK_LEFT < p["x"] < za.TRACK_RIGHT for p in snap["p"])
    assert snap["ammo"] == [None, za.WEAPONS["shotgun"].ammo, za.WEAPONS["machine_gun"].ammo, 2]
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
    target.hp = target.max_hp = 70
    rnd.switch(2, {"w": 2})
    rnd.move(2, {"x": 77.04, "d": za.TOP})
    assert _shoot(rnd, clock, 1, target, shot=5)
    batch = rnd.drain()
    assert batch["d"] == [[target.id, 30, 1]]
    assert batch["f"][0][:2] == [1, 0]
    assert batch["w"] == [[2, 2]]
    assert [2, 77.0, za.TOP] in batch["m"]
    assert batch["a"] == {"1": 5}
    # Said once, not again.
    rnd.advance(0.1)
    again = rnd.drain()
    assert again is None or not ({"d", "f", "w", "a", "fr", "m"} & set(again))

    clock.now += 1
    assert _shoot(rnd, clock, 1, target, shot=6)
    assert rnd.drain()["d"] == [[target.id, 0, 1]]
    snap = rnd.snapshot(2)
    assert snap["dead"] == [target.id]
    assert next(p for p in snap["p"] if p["id"] == 1)["kills"] == 1
    assert next(p for p in snap["p"] if p["id"] == 2)["weapon"] == 2


# ---------------------------------------------------------------------------
# Guard dogs
# ---------------------------------------------------------------------------


def _dog_round(*dogs: str):
    """An empty map, the countdown over, and a guard dog for each player."""
    rnd, clock = _round(players=len(dogs), dogs=dogs)
    _clear(rnd)
    _advance(rnd, clock, za.COUNTDOWN_SECONDS + 1)
    rnd.drain()
    return rnd, clock


def _by_the_wall(rnd, x: float, out: float = 40.0, **more):
    """A zombie standing ``out`` from the top wall of the house."""
    return _plant(rnd, x, za.HOUSE_TOP - out, **more)


def test_there_are_four_guard_dogs_to_choose_from():
    dogs = za.GUARD_DOGS
    assert [(d.name, d.breed) for d in dogs.values()] == [
        ("Juno", "Bernese mountain dog"),
        ("Wynnie", "Newfoundland"),
        ("Wolf", "Husky and spaniel mix"),
        ("Missy", "English bulldog"),
    ]
    juno, wynnie, wolf, missy = dogs.values()
    everyone = list(dogs.values())
    # Juno is in the middle on strength, bite and speed.
    for stat in ("hp", "bite", "speed"):
        numbers = [getattr(d, stat) for d in everyone]
        assert min(numbers) < getattr(juno, stat) < max(numbers), stat
    # Wynnie: lots of strength and bite, but slow.
    assert wynnie.hp == max(d.hp for d in everyone)
    assert wynnie.bite > juno.bite and wynnie.speed == min(d.speed for d in everyone)
    # Wolf: very fast, not so strong.
    assert wolf.speed == max(d.speed for d in everyone) and wolf.speed > 1.5 * juno.speed
    assert wolf.hp == min(d.hp for d in everyone)
    # Missy: slow, medium strength, the strongest bite of all.
    assert missy.bite == max(d.bite for d in everyone) and missy.bite > 1.3 * wynnie.bite
    assert missy.speed == min(d.speed for d in everyone) and missy.hp == juno.hp

    assert za.clean_guard_dog("wolf") == "wolf"
    assert za.clean_guard_dog("rex") is None and za.clean_guard_dog(3) is None
    rules = za.rules_view()
    assert [d["key"] for d in rules["guard_dogs"]] == ["juno", "wynnie", "wolf", "missy"]
    assert rules["default_guard_dog"] == "juno" and rules["guard_max_kills"] == 3
    assert rules["guard_rest"] == [10.0, 20.0]

    # Nobody goes without: a player who never picked gets Juno.
    rnd = za.Round(level=1, duration=60, seed=1, players=[(1, ()), (2, (), "nope")])
    assert [d.kind.key for d in rnd.dogs.values()] == ["juno", "juno"]


def test_a_guard_dog_waits_in_the_house_and_runs_out_at_a_zombie_that_gets_close():
    rnd, clock = _dog_round("juno")
    dog = rnd.dogs[1]
    home = [za.HOUSE_X + za.GUARD_HOMES[0], za.HOUSE_Y]
    assert rnd.snapshot(1)["dogs"] == [
        {"dog": "juno", "home": home, "at": [1, home[0], home[1], za.DOG_HOME, 100, 0.0]}
    ]
    # Inside the house, that is.
    assert za.HOUSE_LEFT < home[0] < za.HOUSE_RIGHT and za.HOUSE_TOP < home[1] < za.HOUSE_BOTTOM

    # Zombies a long way off are the players' business, and so are bats.
    _by_the_wall(rnd, 100, out=za.GUARD_RANGE + 30)
    _by_the_wall(rnd, 140, out=20, kind="bat")
    _advance(rnd, clock, 3)
    assert dog.state == za.DOG_HOME and (dog.x, dog.y) == tuple(home)
    assert "g" not in (rnd.drain() or {})

    close = _by_the_wall(rnd, 120, hp=60, dps=0)
    _advance(rnd, clock, 0.3)
    assert dog.state == za.DOG_OUT and dog.y < home[1]
    said = rnd.drain()["g"]
    assert said == [dog.wire()] and said[0][3] == za.DOG_OUT
    # Two of Juno's bites see to it, and the kill is her owner's.
    _advance(rnd, clock, 1.5)
    assert not close.alive
    assert rnd.fighters[1].kills == 1 and rnd.result()["dog_kills"] == {"1": 1}
    assert [close.id, 0, 1] in rnd.drain()["d"]

    # With nothing else in reach she trots home.
    _advance(rnd, clock, 3)
    assert dog.state == za.DOG_HOME and (dog.x, dog.y) == tuple(home)
    assert rnd.drain()["g"][-1][:4] == [1, home[0], home[1], za.DOG_HOME]


def test_a_guard_dog_comes_home_after_three_kills_and_rests_ten_seconds():
    rnd, clock = _dog_round("missy")
    dog = rnd.dogs[1]
    for i in range(5):
        _by_the_wall(rnd, 60 + 12 * i, hp=20, dps=0)
    while dog.state != za.DOG_HOME or not dog.total:
        _advance(rnd, clock, 0.1)
        assert rnd.t < 30
    assert dog.total == za.GUARD_MAX_KILLS == 3
    assert len(rnd._active) == 2  # two left standing right outside
    # Nobody laid a tooth on her, so it is the short rest.
    assert dog.hp == dog.kind.hp
    assert dog.ready_at == pytest.approx(rnd.t + za.GUARD_REST[0], abs=0.15)
    assert dog.wire()[5] == round(dog.ready_at, 1)

    _advance(rnd, clock, za.GUARD_REST[0] - 0.5)
    assert dog.state == za.DOG_HOME and len(rnd._active) == 2
    _advance(rnd, clock, 1)
    assert dog.state == za.DOG_OUT
    _advance(rnd, clock, 5)
    assert dog.total == 5 and not rnd._active


def test_a_guard_dog_that_gets_hurt_runs_home_and_rests_longer_but_never_dies():
    rnd, clock = _dog_round("wolf")
    dog = rnd.dogs[1]
    brute = _by_the_wall(rnd, 100, kind="ogre", hp=5000, dps=14)
    worst = 100
    went_home = 0.0
    while not went_home:
        _advance(rnd, clock, 0.1)
        worst = min(worst, dog.wire()[4])
        if dog.state == za.DOG_BACK:
            went_home = rnd.t
        assert rnd.t < 20
    # Mauled until he could take no more, having got a few bites in.
    assert dog.hp == 0 and worst == 0
    assert brute.alive and brute.hp < 5000 and dog.total == 0

    # Nothing touches him on the way in, and he is still there.
    while dog.state != za.DOG_HOME:
        _advance(rnd, clock, 0.1)
    assert rnd.dogs == {1: dog}
    assert dog.ready_at == pytest.approx(rnd.t + za.GUARD_REST[1], abs=0.15)
    _advance(rnd, clock, za.GUARD_REST[1] - 1)
    assert dog.state == za.DOG_HOME
    # Rested, he is as good as new and goes straight back out.
    _advance(rnd, clock, 1.5)
    assert dog.state == za.DOG_OUT and dog.hp > 0.5 * dog.kind.hp

    # A dog that is only scratched rests somewhere in between.
    rnd, clock = _dog_round("wynnie")
    dog = rnd.dogs[1]
    _by_the_wall(rnd, 100, hp=80, dps=6)
    while dog.state != za.DOG_HOME or not dog.total:
        _advance(rnd, clock, 0.1)
    assert 0 < dog.hp < dog.kind.hp
    assert za.GUARD_REST[0] + 0.3 < dog.ready_at - rnd.t < za.GUARD_REST[1] - 0.3


def test_each_dog_fights_the_way_its_breed_would():
    def outing(key: str, **zombie):
        rnd, clock = _dog_round(key)
        dog = rnd.dogs[1]
        target = _by_the_wall(rnd, 250, out=70, **zombie)
        reached = hurt = None
        while dog.state != za.DOG_HOME or reached is None:
            _advance(rnd, clock, 0.1)
            if reached is None and target.hp < target.max_hp:
                reached = rnd.t
            if hurt is None and dog.state == za.DOG_BACK:
                hurt = rnd.t - reached
            assert rnd.t < 60
        return reached, hurt, target.max_hp - target.hp

    tough = {"hp": 5000, "dps": 8}
    reach = {key: outing(key, **tough) for key in za.GUARD_DOGS}
    # Wolf is there first; Wynnie and Missy bring up the rear.
    assert reach["wolf"][0] < reach["juno"][0] < reach["wynnie"][0]
    assert reach["wolf"][0] < reach["juno"][0] < reach["missy"][0]
    # Wynnie stays in the fight longest and Wolf is seen off soonest.
    assert reach["wolf"][1] < reach["juno"][1] < reach["wynnie"][1]
    # Missy does the most damage for the time she is out there.
    rate = {key: dealt / stayed for key, (_r, stayed, dealt) in reach.items()}
    assert max(rate, key=rate.get) == "missy" and min(rate, key=rate.get) == "wolf"


def test_two_dogs_live_at_either_end_of_the_house_and_share_out_the_zombies():
    rnd, clock = _dog_round("juno", "wolf")
    juno, wolf = rnd.dogs[1], rnd.dogs[2]
    assert juno.hx < za.HOUSE_X < wolf.hx
    assert [d["dog"] for d in rnd.snapshot(None)["dogs"]] == ["juno", "wolf"]

    left = _by_the_wall(rnd, 70, hp=400, dps=0)
    right = _by_the_wall(rnd, 110, hp=400, dps=0)
    _advance(rnd, clock, 0.2)
    assert {juno.target.id, wolf.target.id} == {left.id, right.id}
    # Every move a dog makes goes out to the phones, and nothing when
    # they are lying down.
    assert sorted(w[0] for w in rnd.drain()["g"]) == [1, 2]
    _advance(rnd, clock, 12)
    assert not left.alive and not right.alive
    # (Whoever finishes first helps the other with theirs.)
    kills = rnd.result()["dog_kills"]
    assert set(kills) == {"1", "2"} and sum(kills.values()) == 2
    _advance(rnd, clock, 4)
    rnd.drain()
    _advance(rnd, clock, 1)
    assert "g" not in (rnd.drain() or {})


def test_dogs_stay_put_during_the_countdown_and_help_hold_the_house():
    rnd, clock = _round(dogs=("juno",))
    dog = rnd.dogs[1]
    _plant(rnd, 100, za.HOUSE_TOP - 30, hp=40, dps=0)
    _advance(rnd, clock, za.COUNTDOWN_SECONDS - 0.5)
    assert dog.state == za.DOG_HOME
    _advance(rnd, clock, 1)
    assert dog.state == za.DOG_OUT

    # Left alone with the level, a dog saves some of the house but not all of it.
    def left_to(dogs):
        rnd, clock = _round(duration=60, seed=31, dogs=dogs)
        while rnd.over is None:
            _advance(rnd, clock, 1.0)
        return rnd

    alone, guarded = left_to(()), left_to(("juno",))
    assert alone.over == guarded.over == "lost"
    assert guarded.result()["dog_kills"]["1"] >= 3
    assert guarded.t > alone.t


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


def _join(ws, style="dad", color="red") -> Dict[str, Any]:
    ws.send_json({"type": "join", "style": style, "color": color})
    return _until(ws, lambda m: m["type"] == "state" and m["game"]["you"]["joined"])["game"]


def test_the_games_page_offers_both_games(client, household):
    body = client.get(f"/api/games?family_id={household.family_id}").json()
    catalog = {g["key"]: g for g in body["catalog"]}
    assert list(catalog)[:2] == ["turkey_tictactoe", ZOMBIES]
    zombie = catalog[ZOMBIES]
    assert zombie["name"] == "Zombie Apocalypse"
    assert (zombie["min_players"], zombie["max_players"]) == (1, 2)
    assert zombie["realtime"] is True and zombie["can_play_ai"] is False
    assert catalog["turkey_tictactoe"]["realtime"] is False

    # One player, or one player and one guest.
    resp = client.post(
        "/api/games",
        json={"family_id": household.family_id, "game_type": ZOMBIES,
              "host_person_id": household.ben.person_id,
              "invite_person_ids": [household.katie.person_id, household.omar.person_id]},
    )
    assert resp.status_code == 400 and "up to 2 players" in resp.json()["detail"]

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
        assert "character" in _until(ben, lambda m: m["type"] == "error")["message"]
        lobby = _join(ben, "army_guy", "blue")
        assert (lobby["players"][0]["avatar_style"], lobby["players"][0]["avatar_color"]) == ("army_guy", "blue")
        me = lobby["you"]["player_id"]
        assert lobby["can_start"]  # one player is enough
        z = lobby["zombie"]
        assert (z["level"], z["duration"], z["best_level"], z["last"]) == (1, 120, 0, None)
        assert z["durations"] == [60, 120, 180, 300]
        assert z["loadouts"] == {str(me): ["shotgun", "machine_gun"]}
        assert z["coming"] == ["walker", "dog", "ogre", "vampire"]
        rules = z["rules"]
        assert [w["key"] for w in rules["weapons"]] == list(za.WEAPONS)
        assert [k["key"] for k in rules["zombies"]] == list(za.ZOMBIE_TYPES)
        assert rules["house"]["hp"] == 1000 and rules["default_weapon"] == "crossbow"

        # Everyone has a guard dog: Juno until they pick another.
        assert z["guard_dogs"] == {str(me): "juno"}
        assert [d["name"] for d in rules["guard_dogs"]] == ["Juno", "Wynnie", "Wolf", "Missy"]
        ben.send_json({"type": "guard_dog", "dog": "rex"})
        assert "guard dogs" in _until(ben, lambda m: m["type"] == "error")["message"]
        ben.send_json({"type": "guard_dog", "dog": "missy"})
        assert _state(ben)["zombie"]["guard_dogs"] == {str(me): "missy"}

        # Pick two weapons to go with the crossbow.
        ben.send_json({"type": "loadout", "weapons": ["grenade", "crossbow"]})
        assert "crossbow" in _until(ben, lambda m: m["type"] == "error")["message"]
        ben.send_json({"type": "loadout", "weapons": ["grenade", "boulder"]})
        assert _until(ben, lambda m: m["type"] == "error")
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
        assert snap["p"][0]["weapons"] == ["crossbow", "grenade", "sniper", "boulder"]
        assert snap["ammo"] == [None, 9, 14, 2]
        assert len(snap["trees"]) == 6
        assert [(d["dog"], d["at"][0], d["at"][3]) for d in snap["dogs"]] == [("missy", me, 0)]
        assert zombie_live.is_live(game["game_id"])

        # Weapons and dogs are chosen between levels, not during one.
        ben.send_json({"type": "loadout", "weapons": ["shotgun", "sniper"]})
        assert "level is over" in _until(ben, lambda m: m["type"] == "error")["message"]
        ben.send_json({"type": "guard_dog", "dog": "wolf"})
        assert "level is over" in _until(ben, lambda m: m["type"] == "error")["message"]

        # The house's points only ever go down, and when they run out
        # the level is lost.
        seen: List[int] = []

        dog_states = set()

        def over(message):
            if message["type"] == "zt":
                seen.append(message["h"])
                dog_states.update(wire[3] for wire in message.get("g", ()))
            return _is_state("round_over")(message)

        ended = _until(ben, over)["game"]
        assert seen == sorted(seen, reverse=True) and seen[0] == 1000 and seen[-1] == 0
        z = ended["zombie"]
        assert z["last"]["won"] is False and z["last"]["house"] == 0
        assert z["last"]["level"] == 1 and z["last"]["seconds"] < 120
        # Missy went out, came back and lay down again, more than once.
        assert dog_states == {za.DOG_HOME, za.DOG_OUT, za.DOG_BACK}
        assert 3 <= z["last"]["dog_kills"][str(me)] <= z["last"]["kills"][str(me)]
        assert z["guard_dogs"] == {str(me): "missy"}  # and is hers for the next level
        assert (z["level"], z["best_level"]) == (1, 0)  # the same level again
        assert ended["rounds_played"] == 1 and ended["can_start"]
        assert [p["wins"] for p in ended["players"]] == [0]
        assert not zombie_live.is_live(game["game_id"])


def _rail(wire: List[Any]) -> Dict[str, Any]:
    """A zombie as a phone reads it from a snapshot."""
    zid, _kind, t0, speed, hp, *numbers = wire
    corners = list(zip(numbers[::2], numbers[1::2]))
    return {"id": zid, "t0": t0, "speed": speed, "hp": hp, "corners": corners,
            "length": sum(math.dist(a, b) for a, b in zip(corners, corners[1:]))}


def _where(zombie: Dict[str, Any], t: float):
    gone = min(zombie["length"], max(0.0, (t - zombie["t0"]) * zombie["speed"]))
    corners = zombie["corners"]
    for a, b in zip(corners, corners[1:]):
        leg = math.dist(a, b)
        if gone <= leg or b == corners[-1]:
            f = min(1.0, gone / leg) if leg else 1.0
            return a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f
        gone -= leg
    return corners[-1]


def _fight(driver, shooters: Dict[int, Any], snap: Dict[str, Any]) -> Dict[str, Any]:
    """Step in front of the nearest zombies and shoot them with crossbows
    until the level is over.

    ``driver`` is the socket the updates are read from; ``shooters``
    maps player ids to the sockets their shots are sent on.
    """
    zombies = {wire[0]: _rail(wire) for wire in snap["z"]}
    shot = 0
    while True:
        message = driver.receive_json()
        if _is_state("round_over")(message):
            return message["game"]
        if message["type"] != "zt":
            continue
        t = message["t"]
        for wire in message.get("z", ()):
            zombies[wire[0]] = _rail(wire)  # a vampire has set off
        for zid, hp, _by in message.get("d", ()):
            zombies[zid]["hp"] = hp
        if t < 0:
            continue
        nearest = []
        for z in zombies.values():
            if z["hp"] <= 0 or z["t0"] > t - 0.5:
                continue
            x, y = _where(z, t)
            side = _side_of(y)
            out = (y - za.track_y(side)) * side
            if out <= 180:
                nearest.append((out, z["id"], x, side))
        nearest.sort()
        for ws, (_out_, zid, x, side) in zip(shooters.values(), nearest):
            shot += 1
            ws.send_json({"type": "zf", "w": 0, "x": x, "d": side, "s": shot, "q": shot})
            ws.send_json({"type": "zh", "s": shot, "z": zid, "q": shot})


def test_a_family_member_and_a_guest_survive_a_level_together_and_move_up(
    client, household, players, fast_levels
):
    hh = household
    game = _zombie_game(client, hh)
    gid = game["game_id"]
    with players.websocket_connect(f"/api/games/play/{_token(game, hh.ben)}/ws") as ben:
        _state(ben)
        ben_id = _join(ben, "uncle", "red")["you"]["player_id"]

        # A guest comes in through the invite link, like in the other game.
        seat = _guest_join(players, _invite_token(game), "Grandma Jo", style="grandma", color="teal")
        assert seat.status_code == 201, seat.text
        guest_id = seat.json()["player_id"]
        _until(ben, lambda m: m["type"] == "state" and len(m["game"]["players"]) == 2)
        # Two is company: there is no room for a third.
        late = _guest_join(players, _invite_token(game), "Late Larry", style="boy", color="green")
        assert late.status_code == 400 and "full" in late.json()["detail"]

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
            assert z["coming"] == ["walker", "dog", "bat", "ogre", "vampire"]
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
        ben.send_json({"type": "zm", "x": 60, "d": za.BOTTOM})
        assert _until(ben, lambda m: m["type"] == "zt" and "m" in m)["m"] == [[me, 60.0, za.BOTTOM]]
        ben.send_json({"type": "zw", "w": 2})
        ben.send_json({"type": "zf", "w": 2, "s": 1, "q": 1})
        _until(ben, lambda m: m["type"] == "zt" and m.get("a") == {str(me): 1})

    # Away for what would be twenty seconds of the level.
    time.sleep(0.5)
    assert zombie_live.is_live(game["game_id"])
    with players.websocket_connect(path) as ben:
        assert _state(ben)["status"] == "playing"
        back = _until(ben, lambda m: m["type"] == "zs")
        assert left_at <= back["t"] < left_at + 5
        assert back["z"] == snap["z"]
        # The level remembers where this player was, what they were
        # carrying and what they had used.
        assert (back["p"][0]["x"], back["p"][0]["side"]) == (60.0, za.BOTTOM)
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
        ben.send_json({"type": "zf", "w": 0, "s": 1, "q": 1})
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
            # (An update of the level can reach her phone before the game does.)
            assert _until(katie, lambda m: m["type"] == "state")["game"]["status"] == "playing"
            watching = _until(katie, lambda m: m["type"] == "zs")
            assert watching["ammo"] is None and [p["id"] for p in watching["p"]] == [ben_id]
            # Red is Ben's.
            katie.send_json({"type": "join", "style": "girl", "color": "red"})
            assert "Ben already has" in _until(katie, lambda m: m["type"] == "error")["message"]
            joined = _join(katie, "mom", "pink")
            katie_id = joined["you"]["player_id"]
            assert joined["zombie"]["fighting"] == [ben_id]
            # Her shots don't count in a level she isn't fighting in.
            katie.send_json({"type": "zf", "w": 0, "s": 1, "q": 1})

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
        ben.send_json({"type": "zf", "w": 0, "s": 1, "q": 1})
        ben.send_json({"type": "loadout", "weapons": ["grenade", "sniper"]})
        assert _until(ben, lambda m: m["type"] == "error")
        ben.send_json({"type": "join", "style": "dad", "color": "red"})
        assert "turkey" in _until(ben, lambda m: m["type"] == "error")["message"]
        assert "zombie" not in service.game_view(
            SessionLocal().get(models.Game, game["game_id"])
        )

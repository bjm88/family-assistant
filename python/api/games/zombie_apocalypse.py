"""Rules of Zombie Apocalypse.

A co-operative tower defence for one to four players, seen from above.
The family's house stands in the middle of the map with the players
inside it. Zombies come in waves from off the top and bottom of the
screen, walk to the house and attack it. The house starts with
``HOUSE_HP`` points; if any are left when the clock runs out the
players have survived the level, and the next one is harder.

Unlike Turkey TicTacToe this is played in real time, so it is built to
say as little as possible over the network:

* **Zombies move on rails.** A level is generated once, up front
  (:func:`build_level`): every zombie's spawn time, start, target and
  speed. Where a zombie is at any moment is a pure function of that
  (:meth:`Zombie.position`), so the server sends the whole schedule
  once and never sends a position again. The React app has the same
  function (``ui/react/src/games/zombies/engine.ts``).
* **The server owns the outcome**: each zombie's health, who is dead,
  the house's points, ammunition, the clock, and whether the level was
  survived.
* **A player's own phone decides what their bolts and bullets hit**
  and reports it (``zh``), so a shot that looked like a hit on a slow
  connection is a hit. The server checks the report is plausible — the
  shot was really fired, with ammunition, by a weapon that reaches that
  far — and applies the damage. Sprays (shotgun, flamethrower) and
  grenades are worked out here, from where the zombies are when the
  message arrives.

:class:`Round` is one level being played. It knows nothing about
sockets or the database; :mod:`.zombie_live` drives it.

Distances are in map units: the map is ``WORLD_W`` by ``WORLD_H`` and
is scaled to fit the phone. Times are seconds on the level's own clock,
which starts at ``-COUNTDOWN_SECONDS`` so players get a "3, 2, 1".
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

GAME_KEY = "zombie_apocalypse"

WORLD_W = 360.0
WORLD_H = 640.0

# The house: centre and size. Players stand inside it.
HOUSE_X = 180.0
HOUSE_Y = 320.0
HOUSE_W = 136.0
HOUSE_H = 108.0
HOUSE_LEFT = HOUSE_X - HOUSE_W / 2
HOUSE_RIGHT = HOUSE_X + HOUSE_W / 2
HOUSE_TOP = HOUSE_Y - HOUSE_H / 2
HOUSE_BOTTOM = HOUSE_Y + HOUSE_H / 2
HOUSE_HP = 1000

# Where each player stands, in the order they joined.
PLAYER_SLOTS: Tuple[Tuple[float, float], ...] = (
    (146.0, 290.0),
    (214.0, 290.0),
    (146.0, 350.0),
    (214.0, 350.0),
)

# The crowd at a wall stands this far past the house's corners, and
# this many map units deep, so dozens of zombies fit.
PILE_OVERHANG = 12.0
PILE_DEPTH = 30.0
# The most zombies one wave brings.
MAX_WAVE = 50
# A wave at the very start of a level is this share of the level's
# usual wave, and grows by this much by the end.
WAVE_START = 0.35
WAVE_GROWTH = 1.65

DURATIONS: Tuple[int, ...] = (60, 120, 180, 300)
DEFAULT_DURATION = 120
COUNTDOWN_SECONDS = 3.0
MAX_LEVEL = 99


# ---------------------------------------------------------------------------
# Weapons
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Weapon:
    key: str
    name: str
    # 'shot'  one bolt or bullet; hurts the first zombie it touches
    # 'cone'  a spray; hurts every zombie in a wedge in front of the player
    # 'lob'   thrown to a spot; hurts (or freezes) every zombie around
    #         where it lands
    # 'roll'  let go from where the player stands; rolls straight out
    #         to the edge of the map, hitting every zombie it touches
    kind: str
    damage: float
    range: float          # how far it reaches, map units
    speed: float          # how fast it travels, map units a second
    reload: float         # seconds between shots
    ammo: Optional[int]   # shots for a two-minute level; None = never runs out
    blurb: str
    spread: float = 0.0   # 'cone': half the wedge's angle, radians
    radius: float = 0.0   # 'lob': how far the blast reaches; 'roll': how wide it is
    falloff: float = 1.0  # share of the damage dealt at the far edge
    freeze: float = 0.0   # seconds the zombies it catches stand frozen
    # Every player carries it, with exactly ``ammo`` uses a level
    # however long the level is; it isn't one of the weapons they pick.
    given: bool = False

    @property
    def area(self) -> bool:
        return self.kind != "shot"


WEAPONS: Dict[str, Weapon] = {
    w.key: w
    for w in (
        Weapon(
            "crossbow", "Crossbow", "shot",
            damage=40, range=320, speed=520, reload=0.5, ammo=None,
            blurb="Dependable. Never runs out of bolts.",
        ),
        Weapon(
            "machine_gun", "Machine gun", "shot",
            damage=16, range=330, speed=760, reload=0.1, ammo=180,
            blurb="Small bullets, lots of them.",
        ),
        Weapon(
            "sniper", "Sniper rifle", "shot",
            damage=260, range=760, speed=1500, reload=1.5, ammo=14,
            blurb="Reaches the whole map and hits hard. Slow to reload.",
        ),
        Weapon(
            "shotgun", "Shotgun", "cone",
            damage=70, range=170, speed=700, reload=0.85, ammo=26,
            spread=math.radians(20), falloff=0.5,
            blurb="Hits everything in front of you, up close.",
        ),
        Weapon(
            "flamethrower", "Flamethrower", "cone",
            damage=11, range=120, speed=300, reload=0.08, ammo=260,
            spread=math.radians(17), falloff=0.7,
            blurb="Burns a whole crowd, but only at the doorstep.",
        ),
        Weapon(
            "grenade", "Grenade", "lob",
            damage=220, range=290, speed=260, reload=1.3, ammo=9,
            radius=75, falloff=0.4,
            blurb="Throw it where you tap. A big blast.",
        ),
        Weapon(
            "snowball", "Snowball", "lob",
            damage=0, range=300, speed=320, reload=1.2, ammo=10,
            radius=70, freeze=6.0,
            blurb="Freezes the whole group where it lands. Doesn't hurt them.",
        ),
        Weapon(
            "boulder", "Boulder", "roll",
            damage=100_000, range=math.hypot(WORLD_W, WORLD_H), speed=190,
            reload=1.0, ammo=2, radius=20, given=True,
            blurb="Rolls straight out from the house and flattens everything in its way.",
        ),
    )
}
WEAPON_KEYS: Tuple[str, ...] = tuple(WEAPONS)
DEFAULT_WEAPON = "crossbow"
# Everyone carries the crossbow, this many weapons of their choice...
LOADOUT_PICKS = 2
# ...and these, which nobody has to pick (two boulders a level).
GIVEN_WEAPONS: Tuple[str, ...] = tuple(w.key for w in WEAPONS.values() if w.given)
DEFAULT_LOADOUT: Tuple[str, ...] = ("shotgun", "machine_gun")

# A phone's clock and the server's never agree exactly, and a bullet
# reported as a hit left a moment ago: allow this much extra reach.
_REACH_SLACK = 48.0
# Shots may arrive bunched up after a network hiccup. Each weapon can
# be this far "ahead" of its reload time before shots are refused.
_BURST_SECONDS = 0.5
_RATE_TOLERANCE = 1.1


def ammo_for(weapon: Weapon, duration: float) -> Optional[int]:
    """Shots a player gets for one level. A longer level comes with more."""
    if weapon.ammo is None or weapon.given:
        return weapon.ammo
    return int(math.ceil(weapon.ammo * max(1.0, duration) / DEFAULT_DURATION))


def clean_loadout(raw: Any) -> Optional[List[str]]:
    """The weapons a player picked, if it is a valid choice."""
    if not isinstance(raw, (list, tuple)) or len(raw) != LOADOUT_PICKS:
        return None
    picks = [w for w in raw if isinstance(w, str)]
    if len(set(picks)) != LOADOUT_PICKS:
        return None
    if any(
        w not in WEAPONS or w == DEFAULT_WEAPON or WEAPONS[w].given for w in picks
    ):
        return None
    return picks


# ---------------------------------------------------------------------------
# Zombies
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ZombieType:
    key: str
    name: str
    hp: float
    speed: float       # map units a second
    dps: float         # points a second it takes off the house
    radius: float
    threat: float      # how much of a wave's budget one costs
    from_level: int    # first level it turns up in
    weight: float      # how often it is picked, next to the others
    flying: bool = False


# Small and individually weak, so that dozens can be on the map at
# once: a crowd is what the area weapons are for.
ZOMBIE_TYPES: Dict[str, ZombieType] = {
    z.key: z
    for z in (
        ZombieType("walker", "Zombie", hp=30, speed=22, dps=3, radius=8,
                   threat=1.0, from_level=1, weight=5.0),
        ZombieType("dog", "Zombie dog", hp=18, speed=58, dps=2, radius=6,
                   threat=0.8, from_level=2, weight=3.0),
        ZombieType("bat", "Bat", hp=12, speed=75, dps=1.5, radius=6,
                   threat=0.7, from_level=3, weight=2.5, flying=True),
        ZombieType("ogre", "Ogre", hp=400, speed=14, dps=14, radius=15,
                   threat=6.0, from_level=4, weight=0.7),
    )
}
ZOMBIE_KEYS: Tuple[str, ...] = tuple(ZOMBIE_TYPES)

# Bats weave from side to side as they fly in; the weave dies away as
# they reach the house. Same numbers as in the React app.
BAT_WEAVE = 26.0
BAT_WEAVE_RATE = 5.0


class Zombie:
    """One zombie on its rail from off-screen to the house.

    The rail is a line from where it appears to its spot at the house,
    bent around any trees in the way. Where the zombie is on it depends
    only on the time, which is what lets the phones draw every zombie
    without being told where they are.
    """

    __slots__ = (
        "id", "type", "t0", "path", "x0", "y0", "tx", "ty", "speed", "max_hp",
        "hp", "dps", "radius", "flying", "phase", "arrive", "_cum", "_dist",
        "alive", "stop", "until", "lag",
    )

    def __init__(
        self,
        zombie_id: int,
        ztype: ZombieType,
        *,
        t0: float,
        path: Sequence[Tuple[float, float]],
        speed: float,
        hp: float,
        dps: float,
    ) -> None:
        self.id = zombie_id
        self.type = ztype
        # Rounded exactly as they are sent, so the phones and the
        # server work from the same numbers.
        self.t0 = round(t0, 2)
        self.path = [(round(x, 1), round(y, 1)) for x, y in path]
        self.x0, self.y0 = self.path[0]
        self.tx, self.ty = self.path[-1]
        self.speed = round(speed, 1)
        self.max_hp = int(round(hp))
        self.hp = float(self.max_hp)
        self.dps = dps
        self.radius = ztype.radius
        self.flying = ztype.flying
        self.phase = (zombie_id * 2.399) % (2 * math.pi)
        # How far along the rail each of its corners is.
        self._cum = [0.0]
        for (ax, ay), (bx, by) in zip(self.path, self.path[1:]):
            self._cum.append(self._cum[-1] + math.hypot(bx - ax, by - ay))
        self._dist = self._cum[-1] or 1.0
        # When it reaches the house, by its own clock (see ``clock``).
        self.arrive = self.t0 + self._dist / self.speed
        self.alive = True
        # Being frozen stops a zombie's own clock: it reads ``stop``
        # until the level's clock reaches ``until``, and runs ``lag``
        # behind the level's clock from then on.
        self.stop = 0.0
        self.until = -1e9
        self.lag = 0.0

    def clock(self, t: float) -> float:
        """This zombie's own time at ``t``: the level's, less time spent frozen."""
        return self.stop if t < self.until else t - self.lag

    def freeze(self, t: float, seconds: float) -> None:
        """Stand still from ``t`` for ``seconds`` (longer, if already frozen)."""
        stop = self.clock(t)
        until = max(self.until, t + seconds) if t < self.until else t + seconds
        # Rounded as sent, like everything else the phones are told.
        self.stop = round(stop, 2)
        self.until = round(until, 2)
        self.lag = self.until - self.stop

    def position(self, t: float) -> Tuple[float, float]:
        """Where this zombie is at ``t`` (it stays put once it arrives)."""
        t = self.clock(t)
        gone = (t - self.t0) * self.speed
        gone = 0.0 if gone < 0.0 else self._dist if gone > self._dist else gone
        path, cum = self.path, self._cum
        leg = 1
        while leg < len(cum) - 1 and cum[leg] < gone:
            leg += 1
        (ax, ay), (bx, by) = path[leg - 1], path[leg]
        length = (cum[leg] - cum[leg - 1]) or 1.0
        f = (gone - cum[leg - 1]) / length
        x = ax + (bx - ax) * f
        y = ay + (by - ay) * f
        if self.flying:
            # Bats weave from side to side, less and less as they near
            # the house.
            sway = (
                BAT_WEAVE
                * math.sin(BAT_WEAVE_RATE * (t - self.t0) + self.phase)
                * (1.0 - gone / self._dist)
            )
            x -= (by - ay) / length * sway
            y += (bx - ax) / length * sway
        return x, y

    def wire(self) -> List[Any]:
        """How the zombie is sent to the phones.

        ``[id, kind, t0, speed, hp, x, y, x, y, ...]``: the last numbers
        are the corners of its rail, from where it appears to its spot
        at the house.
        """
        out: List[Any] = [
            self.id,
            ZOMBIE_KEYS.index(self.type.key),
            self.t0,
            self.speed,
            self.max_hp,
        ]
        for x, y in self.path:
            out.append(x)
            out.append(y)
        return out


# ---------------------------------------------------------------------------
# Trees
# ---------------------------------------------------------------------------

# A few trees stand in the grounds on each side of the house. Zombies
# on foot have to walk around them; bats fly over.
TREES_PER_SIDE = 3
TREE_RADIUS = (15.0, 21.0)
# Trees keep this far from each other (centre to centre), so there is
# always a way between them, and stay out of the crowd at the walls.
_TREE_SPACING = 88.0
_TREE_BAND = (64.0, 172.0)   # how far from the top (or bottom) edge they stand
_TREE_MARGIN_X = 34.0
_TREE_CLEARANCE = 3.0

Tree = Tuple[float, float, float]  # x, y, radius


def build_trees(seed: int) -> List[Tree]:
    """Where the trees of a level stand: the same for the same seed."""
    rng = random.Random(f"trees:{seed}")
    trees: List[Tree] = []
    for side in (-1, 1):
        placed: List[Tree] = []
        tries = 0
        while len(placed) < TREES_PER_SIDE and tries < 200:
            tries += 1
            x = rng.uniform(_TREE_MARGIN_X, WORLD_W - _TREE_MARGIN_X)
            off = rng.uniform(*_TREE_BAND)
            y = off if side < 0 else WORLD_H - off
            if all(math.hypot(x - px, y - py) >= _TREE_SPACING for px, py, _r in placed):
                placed.append((round(x, 1), round(y, 1), round(rng.uniform(*TREE_RADIUS), 1)))
        trees.extend(placed)
    return trees


def _around_trees(
    rng: random.Random,
    start: Tuple[float, float],
    end: Tuple[float, float],
    trees: Sequence[Tree],
    radius: float,
    depth: int = 0,
) -> List[Tuple[float, float]]:
    """The corners of a walk from ``start`` to ``end`` that misses the trees.

    Returns the points after ``start``, ending with ``end``. Where the
    straight line would go through a tree, the walk bends out past the
    side of it the line was already on.
    """
    (ax, ay), (bx, by) = start, end
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 < 1.0 or depth >= 5:
        return [end]
    worst = None
    for cx, cy, tree_radius in trees:
        keep = tree_radius + radius + _TREE_CLEARANCE
        f = ((cx - ax) * dx + (cy - ay) * dy) / length2
        if not 0.0 < f < 1.0:
            continue
        px, py = ax + dx * f, ay + dy * f
        gap = math.hypot(px - cx, py - cy)
        if gap < keep and (worst is None or f < worst[0]):
            worst = (f, px, py, cx, cy, keep, gap)
    if worst is None:
        return [end]
    _f, px, py, cx, cy, keep, gap = worst
    if gap > 0.5:
        ox, oy = (px - cx) / gap, (py - cy) / gap
    else:
        # Heading straight for the trunk: go round either side.
        length = math.sqrt(length2)
        way = rng.choice((-1.0, 1.0))
        ox, oy = -dy / length * way, dx / length * way
    out = keep + rng.uniform(2.0, 14.0)
    if not radius <= cx + ox * out <= WORLD_W - radius:
        # Don't squeeze past a tree by leaving the map: take the other side.
        ox, oy = -ox, -oy
    corner = (cx + ox * out, cy + oy * out)
    return (
        _around_trees(rng, start, corner, trees, radius, depth + 1)
        + _around_trees(rng, corner, end, trees, radius, depth + 1)
    )


# ---------------------------------------------------------------------------
# Levels
# ---------------------------------------------------------------------------


def difficulty(level: int, players: int) -> Dict[str, Any]:
    """What a level throws at the house.

    Higher levels bring tougher, faster zombies, more of them at once,
    waves closer together and new kinds. More players means more
    zombies, so a full house isn't a walkover.

    ``wave_budget`` is the size of a wave half-way through the level,
    in walkers; ``wave_every`` the seconds between waves at that point.
    """
    step = max(1, min(MAX_LEVEL, int(level))) - 1
    team = max(1, min(len(PLAYER_SLOTS), int(players)))
    crowd = 0.4 + 0.6 * team
    return {
        "hp": (1.0 + 0.07 * step) * (1.0 + 0.08 * (team - 1)),
        "speed": min(1.7, 1.0 + 0.035 * step),
        "dps": 1.0 + 0.06 * step,
        "wave_every": max(5.0, 9.0 - 0.25 * step),
        "wave_budget": (6.0 + 0.7 * step) * crowd,
        "both_sides": max(0.0, min(0.8, 0.12 * (step - 1))),
        "types": [z.key for z in ZOMBIE_TYPES.values() if z.from_level <= step + 1],
    }


def build_level(
    level: int, players: int, duration: float, seed: int
) -> List[Zombie]:
    """Every zombie of one level, in the order they appear.

    The same arguments always give the same level. Waves start a
    second or two in and keep coming until just before the end; each
    spends a budget on zombies picked at random from the kinds this
    level has, and comes in from the top, the bottom, or both.
    """
    rng = random.Random(seed)
    trees = build_trees(seed)
    diff = difficulty(level, players)
    kinds = [ZOMBIE_TYPES[k] for k in diff["types"]]
    zombies: List[Zombie] = []
    t = 1.5
    side = rng.choice((-1, 1))  # -1 = from the top, 1 = from the bottom
    while t < duration - 4.0:
        # A level starts with a few zombies and builds up: the waves
        # get bigger, and closer together, as the clock runs down.
        gone = t / duration
        budget = diff["wave_budget"] * (WAVE_START + WAVE_GROWTH * gone) * rng.uniform(0.85, 1.15)
        both = rng.random() < diff["both_sides"]
        ogres_left = 1 + level // 5
        count = 0
        while budget > 0.35 and count < MAX_WAVE:
            options = [
                k for k in kinds
                if k.threat <= budget + 0.6 and (k.key != "ogre" or ogres_left > 0)
            ]
            if not options:
                break
            kind = rng.choices(options, weights=[k.weight for k in options])[0]
            if kind.key == "ogre":
                ogres_left -= 1
            budget -= kind.threat
            count += 1
            from_side = rng.choice((-1, 1)) if both else side
            zombies.append(
                _spawn(rng, kind, diff, trees, from_side, t0=t + rng.uniform(0.0, 4.0))
            )
        side = -side
        t += diff["wave_every"] * (1.2 - 0.5 * gone) * rng.uniform(0.85, 1.15)
    zombies.sort(key=lambda z: z.t0)
    for index, zombie in enumerate(zombies, start=1):
        zombie.id = index
        zombie.phase = (index * 2.399) % (2 * math.pi)
    return zombies


def _spawn(
    rng: random.Random,
    kind: ZombieType,
    diff: Dict[str, Any],
    trees: Sequence[Tree],
    side: int,
    *,
    t0: float,
) -> Zombie:
    x0 = rng.uniform(16.0, WORLD_W - 16.0)
    off = kind.radius + 8.0 + rng.uniform(0.0, 26.0)
    y0 = -off if side < 0 else WORLD_H + off
    if kind.flying:
        # Bats swoop on to the roof.
        tx = HOUSE_X + rng.uniform(-0.36, 0.36) * HOUSE_W
        ty = HOUSE_Y + side * rng.uniform(0.08, 0.36) * HOUSE_H
    else:
        # Everyone else piles up, several deep, against the wall they
        # came to.
        tx = rng.uniform(HOUSE_LEFT - PILE_OVERHANG, HOUSE_RIGHT + PILE_OVERHANG)
        gap = kind.radius + rng.uniform(0.0, PILE_DEPTH)
        ty = HOUSE_TOP - gap if side < 0 else HOUSE_BOTTOM + gap
    start, end = (x0, y0), (tx, ty)
    corners = [end] if kind.flying else _around_trees(rng, start, end, trees, kind.radius)
    return Zombie(
        0,
        kind,
        t0=t0,
        path=[start, *corners],
        speed=kind.speed * diff["speed"] * rng.uniform(0.9, 1.1),
        hp=kind.hp * diff["hp"],
        dps=kind.dps * diff["dps"],
    )


# ---------------------------------------------------------------------------
# One level being played
# ---------------------------------------------------------------------------


class _Fighter:
    """A player in a round: where they stand and what they carry."""

    __slots__ = (
        "player_id", "slot", "x", "y", "weapons", "weapon", "ammo", "kills",
        "shots", "ack", "_tokens", "_stamp", "_flying",
    )

    def __init__(
        self, player_id: int, slot: int, picks: Sequence[str], duration: float
    ) -> None:
        self.player_id = player_id
        self.slot = slot
        self.x, self.y = PLAYER_SLOTS[slot]
        self.weapons: List[str] = [DEFAULT_WEAPON, *picks, *GIVEN_WEAPONS]
        self.weapon = 0  # index into ``weapons``
        self.ammo: List[Optional[int]] = [
            ammo_for(WEAPONS[w], duration) for w in self.weapons
        ]
        self.kills = 0
        self.shots = 0
        self.ack = 0  # highest message number handled, for the phone's bookkeeping
        self._tokens: List[float] = [_burst(WEAPONS[w]) for w in self.weapons]
        self._stamp: List[Optional[float]] = [None] * len(self.weapons)
        # shot id -> (weapon, when it was fired): bolts and bullets in the air
        self._flying: Dict[int, Tuple[Weapon, float]] = {}


class _Roll:
    """A boulder on its way out from the house."""

    __slots__ = ("fighter", "weapon", "x", "y", "ux", "uy", "gone", "limit")

    def __init__(self, fighter: _Fighter, weapon: Weapon, angle: float) -> None:
        self.fighter = fighter
        self.weapon = weapon
        self.x, self.y = fighter.x, fighter.y
        self.ux, self.uy = math.cos(angle), math.sin(angle)
        self.gone = 0.0
        self.limit = weapon.range


def _burst(weapon: Weapon) -> float:
    return max(1.5, _BURST_SECONDS / weapon.reload)


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


class Round:
    """One level in play: the zombies, the house and the players' weapons.

    ``advance`` moves the clock on; ``fire`` / ``hit`` / ``switch`` are
    what players do; ``drain`` hands back what changed since it was
    last called, ready to send to every phone.
    """

    def __init__(
        self,
        *,
        level: int,
        duration: float,
        seed: int,
        players: Sequence[Tuple[int, Sequence[str]]],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not players:
            raise ValueError("a round needs at least one player")
        self.level = int(level)
        self.duration = float(duration)
        self.seed = int(seed)
        self.clock = clock
        self.t = -COUNTDOWN_SECONDS
        self.house = float(HOUSE_HP)
        self.over: Optional[str] = None  # 'won' or 'lost'
        self.fighters: Dict[int, _Fighter] = {}
        for slot, (player_id, picks) in enumerate(players[: len(PLAYER_SLOTS)]):
            self.fighters[player_id] = _Fighter(
                player_id,
                slot,
                clean_loadout(list(picks)) or DEFAULT_LOADOUT,
                self.duration,
            )
        self.trees = build_trees(seed)
        self.zombies = build_level(level, len(self.fighters), duration, seed)
        self._by_id = {z.id: z for z in self.zombies}
        self._next = 0                       # next zombie to appear
        self._active: Dict[int, Zombie] = {}  # on the map and alive
        self._lobs: List[Tuple[float, _Fighter, Weapon, float, float]] = []
        self._rolls: List[_Roll] = []
        self._frozen: Dict[int, Zombie] = {}  # frozen since the last drain()
        # What has changed since the last drain().
        self._hurt: Dict[int, int] = {}       # zombie id -> player who hit it last
        self._fired: List[List[Any]] = []
        self._switched: Dict[int, int] = {}
        self._acked: Dict[int, int] = {}
        self._sent_house = int(HOUSE_HP)
        self._sent_at = -1e9

    # ---- the clock -----------------------------------------------------

    def advance(self, dt: float) -> None:
        """Move the level on by ``dt`` seconds."""
        if self.over is not None or dt <= 0:
            return
        self.t += dt
        t = self.t
        while self._next < len(self.zombies) and self.zombies[self._next].t0 <= t:
            zombie = self.zombies[self._next]
            self._next += 1
            if zombie.alive:
                self._active[zombie.id] = zombie
        if self._lobs:
            due = [lob for lob in self._lobs if lob[0] <= t]
            if due:
                self._lobs = [lob for lob in self._lobs if lob[0] > t]
                for _when, fighter, weapon, x, y in due:
                    self._blast(fighter, weapon, x, y)
        if self._rolls:
            for roll in self._rolls:
                self._roll_on(roll, dt)
            self._rolls = [roll for roll in self._rolls if roll.gone < roll.limit]
        bite = 0.0
        for zombie in self._active.values():
            if t < zombie.until:
                continue  # frozen solid
            at_house = t - zombie.lag - zombie.arrive
            if at_house > 0:
                bite += zombie.dps * min(dt, at_house)
        if bite:
            self.house -= bite
        if self.house <= 0:
            self.house = 0.0
            self.over = "lost"
        elif t >= self.duration:
            self.over = "won"

    # ---- what players do -------------------------------------------------

    def switch(self, player_id: int, message: Dict[str, Any]) -> None:
        fighter = self.fighters.get(player_id)
        index = message.get("w")
        if fighter is None or not isinstance(index, int) or isinstance(index, bool):
            return
        if 0 <= index < len(fighter.weapons) and index != fighter.weapon:
            self._draw(fighter, index)

    def fire(self, player_id: int, message: Dict[str, Any]) -> bool:
        """A player pulled the trigger. Returns whether the shot counted."""
        fighter = self.fighters.get(player_id)
        if fighter is None:
            return False
        self._ack(fighter, message)
        index = message.get("w")
        angle = _number(message.get("a"))
        if (
            self.over is not None
            or self.t < 0
            or not isinstance(index, int)
            or isinstance(index, bool)
            or not 0 <= index < len(fighter.weapons)
            or angle is None
        ):
            return False
        weapon = WEAPONS[fighter.weapons[index]]
        left = fighter.ammo[index]
        if left is not None and left <= 0:
            return False
        if fighter.weapon != index:
            self._draw(fighter, index)
        if not self._may_fire(fighter, index, weapon):
            return False
        if left is not None:
            fighter.ammo[index] = left - 1
        fighter.shots += 1

        # Where the player aimed, no further away than the weapon reaches.
        tx, ty = _number(message.get("x")), _number(message.get("y"))
        if tx is None or ty is None:
            tx = fighter.x + math.cos(angle) * weapon.range
            ty = fighter.y + math.sin(angle) * weapon.range
        dx, dy = tx - fighter.x, ty - fighter.y
        reach = math.hypot(dx, dy)
        if reach > weapon.range:
            dx, dy = dx * weapon.range / reach, dy * weapon.range / reach
            reach = weapon.range
        tx, ty = fighter.x + dx, fighter.y + dy
        self._fired.append(
            [player_id, index, round(angle, 3), round(tx, 1), round(ty, 1)]
        )

        if weapon.kind == "cone":
            self._spray(fighter, weapon, angle)
        elif weapon.kind == "lob":
            self._lobs.append((self.t + reach / weapon.speed, fighter, weapon, tx, ty))
        elif weapon.kind == "roll":
            self._rolls.append(_Roll(fighter, weapon, angle))
        else:
            shot_id = message.get("s")
            if isinstance(shot_id, int) and not isinstance(shot_id, bool):
                if len(fighter._flying) > 200:
                    self._forget_old_shots(fighter)
                fighter._flying[shot_id] = (weapon, self.clock())
        return True

    def hit(self, player_id: int, message: Dict[str, Any]) -> bool:
        """A player's phone says one of their bolts or bullets hit a zombie."""
        fighter = self.fighters.get(player_id)
        if fighter is None:
            return False
        self._ack(fighter, message)
        shot_id, zombie_id = message.get("s"), message.get("z")
        if not isinstance(shot_id, int) or not isinstance(zombie_id, int):
            return False
        flying = fighter._flying.pop(shot_id, None)
        if flying is None or self.over is not None:
            return False
        weapon, fired_at = flying
        if self.clock() - fired_at > weapon.range / weapon.speed + 1.5:
            return False
        zombie = self._active.get(zombie_id)
        if zombie is None:
            return False
        x, y = zombie.position(self.t)
        if math.hypot(x - fighter.x, y - fighter.y) > weapon.range + _REACH_SLACK:
            return False
        self._damage(zombie, weapon.damage, fighter)
        return True

    # ---- damage ----------------------------------------------------------

    def _ack(self, fighter: _Fighter, message: Dict[str, Any]) -> None:
        number = message.get("q")
        if isinstance(number, int) and not isinstance(number, bool) and number > fighter.ack:
            fighter.ack = number
            self._acked[fighter.player_id] = number

    def _draw(self, fighter: _Fighter, index: int) -> None:
        """Change weapons. The one just drawn is always ready to fire."""
        fighter.weapon = index
        self._switched[fighter.player_id] = index
        fighter._tokens[index] = max(fighter._tokens[index], 1.0)

    def _may_fire(self, fighter: _Fighter, index: int, weapon: Weapon) -> bool:
        now = self.clock()
        stamp = fighter._stamp[index]
        tokens = fighter._tokens[index]
        if stamp is not None:
            tokens = min(
                _burst(weapon),
                tokens + (now - stamp) * _RATE_TOLERANCE / weapon.reload,
            )
        fighter._stamp[index] = now
        if tokens < 1.0:
            fighter._tokens[index] = tokens
            return False
        fighter._tokens[index] = tokens - 1.0
        return True

    def _forget_old_shots(self, fighter: _Fighter) -> None:
        now = self.clock()
        fighter._flying = {
            shot_id: (weapon, fired_at)
            for shot_id, (weapon, fired_at) in fighter._flying.items()
            if now - fired_at <= weapon.range / weapon.speed + 1.5
        }

    def _damage(self, zombie: Zombie, amount: float, fighter: _Fighter) -> None:
        if not zombie.alive or amount <= 0:
            return
        zombie.hp -= amount
        if zombie.hp <= 0.5:
            zombie.hp = 0.0
            zombie.alive = False
            self._active.pop(zombie.id, None)
            fighter.kills += 1
        self._hurt[zombie.id] = fighter.player_id

    def _spray(self, fighter: _Fighter, weapon: Weapon, angle: float) -> None:
        for zombie in list(self._active.values()):
            x, y = zombie.position(self.t)
            share = cone_share(
                weapon, fighter.x, fighter.y, angle, x, y, zombie.radius
            )
            if share > 0:
                self._damage(zombie, weapon.damage * share, fighter)

    def _blast(self, fighter: _Fighter, weapon: Weapon, bx: float, by: float) -> None:
        for zombie in list(self._active.values()):
            x, y = zombie.position(self.t)
            share = blast_share(weapon, bx, by, x, y, zombie.radius)
            if share > 0:
                if weapon.freeze:
                    zombie.freeze(self.t, weapon.freeze)
                    self._frozen[zombie.id] = zombie
                self._damage(zombie, weapon.damage * share, fighter)

    def _roll_on(self, roll: "_Roll", dt: float) -> None:
        """Move a boulder on, flattening every zombie it rolls over."""
        start = roll.gone
        roll.gone = min(roll.limit, start + roll.weapon.speed * dt)
        for zombie in list(self._active.values()):
            x, y = zombie.position(self.t)
            dx, dy = x - roll.x, y - roll.y
            reach = roll.weapon.radius + zombie.radius
            along = dx * roll.ux + dy * roll.uy
            if (
                start - reach <= along <= roll.gone + reach
                and abs(dx * roll.uy - dy * roll.ux) <= reach
            ):
                self._damage(zombie, roll.weapon.damage, roll.fighter)

    # ---- what the phones are told ---------------------------------------

    def drain(self) -> Optional[Dict[str, Any]]:
        """Everything that changed since the last call, or ``None``.

        A message goes out at least once a second while the level is
        running, so the phones can keep their clocks in step.
        """
        house = int(math.ceil(self.house))
        quiet = not (
            self._hurt or self._fired or self._switched or self._acked or self._frozen
        )
        if (
            quiet
            and self.over is None
            and (house == self._sent_house or self.t - self._sent_at < 0.25)
            and self.t - self._sent_at < 1.0
        ):
            return None
        batch: Dict[str, Any] = {"type": "zt", "t": round(self.t, 2), "h": house}
        if self._hurt:
            batch["d"] = [
                [zombie_id, int(math.ceil(self._by_id[zombie_id].hp)), player_id]
                for zombie_id, player_id in self._hurt.items()
            ]
            self._hurt = {}
        if self._frozen:
            batch["fr"] = [[z.id, z.stop, z.until] for z in self._frozen.values() if z.alive]
            self._frozen = {}
        if self._fired:
            batch["f"] = self._fired
            self._fired = []
        if self._switched:
            batch["w"] = [[pid, index] for pid, index in self._switched.items()]
            self._switched = {}
        if self._acked:
            batch["a"] = {str(pid): number for pid, number in self._acked.items()}
            self._acked = {}
        if self.over is not None:
            batch["o"] = self.over
        self._sent_house = house
        self._sent_at = self.t
        return batch

    def snapshot(self, player_id: Optional[int] = None) -> Dict[str, Any]:
        """The whole level, for a phone that has just (re)connected."""
        fighter = self.fighters.get(player_id) if player_id is not None else None
        return {
            "type": "zs",
            "t": round(self.t, 2),
            "dur": self.duration,
            "level": self.level,
            "house": int(math.ceil(self.house)),
            "house_max": HOUSE_HP,
            "over": self.over,
            "trees": [list(tree) for tree in self.trees],
            "z": [z.wire() for z in self.zombies],
            "hp": [
                [z.id, int(math.ceil(z.hp))]
                for z in self._active.values()
                if z.hp < z.max_hp
            ],
            "dead": [z.id for z in self.zombies if not z.alive],
            "fr": [[z.id, z.stop, z.until] for z in self._active.values() if z.lag],
            "p": [
                {
                    "id": f.player_id,
                    "slot": f.slot,
                    "x": f.x,
                    "y": f.y,
                    "weapons": f.weapons,
                    "weapon": f.weapon,
                    "kills": f.kills,
                }
                for f in self.fighters.values()
            ],
            "ammo": fighter.ammo if fighter is not None else None,
            "ack": fighter.ack if fighter is not None else 0,
        }

    def result(self) -> Dict[str, Any]:
        """How the level went, to be stored with the game."""
        return {
            "level": self.level,
            "seed": self.seed,
            "won": self.over == "won",
            "house": int(math.ceil(self.house)),
            "seconds": int(round(max(0.0, min(self.t, self.duration)))),
            "duration": int(self.duration),
            "zombies": len(self.zombies),
            "kills": {str(f.player_id): f.kills for f in self.fighters.values()},
            "shots": {str(f.player_id): f.shots for f in self.fighters.values()},
        }


def cone_share(
    weapon: Weapon,
    ox: float,
    oy: float,
    angle: float,
    x: float,
    y: float,
    radius: float,
) -> float:
    """How much of a spray's damage reaches a zombie at ``(x, y)``: 0 to 1."""
    dx, dy = x - ox, y - oy
    dist = math.hypot(dx, dy)
    edge = max(0.0, dist - radius)
    if edge > weapon.range:
        return 0.0
    if dist > radius:
        off = abs((math.atan2(dy, dx) - angle + math.pi) % (2 * math.pi) - math.pi)
        if off > weapon.spread + math.asin(min(1.0, radius / dist)):
            return 0.0
    return 1.0 - (1.0 - weapon.falloff) * edge / weapon.range


def blast_share(
    weapon: Weapon, bx: float, by: float, x: float, y: float, radius: float
) -> float:
    """How much of a blast's damage reaches a zombie at ``(x, y)``: 0 to 1."""
    edge = max(0.0, math.hypot(x - bx, y - by) - radius)
    if edge > weapon.radius:
        return 0.0
    return 1.0 - (1.0 - weapon.falloff) * edge / weapon.radius


# ---------------------------------------------------------------------------
# What the React app is told about the game
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def rules_view() -> Dict[str, Any]:
    """The numbers the app needs to draw the map and fire the weapons.

    Always the same, so it is built once; callers must not change it.
    """
    return {
        "world": {"w": WORLD_W, "h": WORLD_H},
        "house": {
            "x": HOUSE_X, "y": HOUSE_Y, "w": HOUSE_W, "h": HOUSE_H, "hp": HOUSE_HP,
        },
        "slots": [list(slot) for slot in PLAYER_SLOTS],
        "countdown": COUNTDOWN_SECONDS,
        "default_weapon": DEFAULT_WEAPON,
        "loadout_picks": LOADOUT_PICKS,
        "default_loadout": list(DEFAULT_LOADOUT),
        "weapons": [
            {
                "key": w.key,
                "name": w.name,
                "kind": w.kind,
                "area": w.area,
                "damage": w.damage,
                "range": w.range,
                "speed": w.speed,
                "reload": w.reload,
                "ammo": w.ammo,
                "spread": round(w.spread, 4),
                "radius": w.radius,
                "falloff": w.falloff,
                "freeze": w.freeze,
                "given": w.given,
                "blurb": w.blurb,
            }
            for w in WEAPONS.values()
        ],
        "zombies": [
            {
                "key": z.key,
                "name": z.name,
                "hp": z.hp,
                "speed": z.speed,
                "radius": z.radius,
                "flying": z.flying,
                "from_level": z.from_level,
            }
            for z in ZOMBIE_TYPES.values()
        ],
        "bat_weave": BAT_WEAVE,
        "bat_weave_rate": BAT_WEAVE_RATE,
    }

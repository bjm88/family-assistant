"""Rules of Zombie Apocalypse.

A co-operative tower defence for one or two players, seen from above.
The family's house stands across the middle of the map. Zombies come in
waves from off the top and bottom of the screen, walk to the house and
attack it. The house starts with ``HOUSE_HP`` points; if any are left
when the clock runs out the players have survived the level, and the
next one is harder. From half-way through every level the ogres come,
two for each player or more, and at any moment a vampire may dart in:
never more than one at a time.

Each player slides left and right along the top or the bottom wall of
the house, and can change walls. They always face out, and every weapon
fires straight out from where they stand: there is no aiming, only
being in the right place.

Unlike Turkey TicTacToe this is played in real time, so it is built to
say as little as possible over the network:

* **Zombies move on rails.** A level is generated once, up front
  (:func:`build_level`): every zombie's spawn time, start, target and
  speed. Where a zombie is at any moment is a pure function of that
  (:meth:`Zombie.position`), so the server sends the whole schedule
  once and never sends a position again. The React app has the same
  function (``ui/react/src/games/zombies/engine.ts``). Vampires are
  the exception to "once": each waits for the one before it to be
  dead, so its rail is sent when it sets off.
* **The server owns the outcome**: each zombie's health, who is dead,
  the house's points, ammunition, the clock, and whether the level was
  survived.
* **A player's own phone decides what their bolts and bullets hit**
  and reports it (``zh``), so a shot that looked like a hit on a slow
  connection is a hit. The server checks the report is plausible — the
  shot was really fired, with ammunition, the zombie is in front of
  where it was fired from and the weapon reaches that far — and applies
  the damage. Sprays (shotgun, flamethrower), the chainsaw, grenades,
  snowballs and boulders are worked out here, from where the zombies
  are at the time.
* **Boulders are roped to the walls of the house**, two outside each
  wall at the left and two at the right. Any player can cut one side's
  loose, once a level (``zb``): all four roll away from the house, up
  the map and down it, flattening whatever is in their way.
* **Crates of TNT turn up on the map** when there are zombies about,
  the more of them the oftener (:meth:`Round._tnt`). Anything that
  hurts sets one off, and it blows up everything around it. The phones
  are told when one lands and when one goes off.
* **Players say where they are** (``zm``, and with every shot); the
  server keeps them on the walls and passes it on.
* **Each player has a guard dog** that lives in the house, runs out at
  zombies that get close and comes back for a rest. The dogs are run
  here (:meth:`Round._run_dogs`) and the phones are told where they are.

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

# The house: centre and size. It is nearly as wide as the map, so there
# is a wall to defend in front of every zombie.
HOUSE_X = 180.0
HOUSE_Y = 320.0
HOUSE_W = 300.0
HOUSE_H = 84.0
HOUSE_LEFT = HOUSE_X - HOUSE_W / 2
HOUSE_RIGHT = HOUSE_X + HOUSE_W / 2
HOUSE_TOP = HOUSE_Y - HOUSE_H / 2
HOUSE_BOTTOM = HOUSE_Y + HOUSE_H / 2
HOUSE_HP = 1000

# Players stand just inside the top wall (side -1, facing up the map)
# or the bottom wall (side 1, facing down), and slide along it between
# TRACK_LEFT and TRACK_RIGHT.
TOP, BOTTOM = -1, 1
TRACK_LEFT = HOUSE_LEFT + 12.0
TRACK_RIGHT = HOUSE_RIGHT - 12.0
TRACK_INSET = 10.0
# How fast a player slides, map units a second, and how long changing
# walls takes. The phones move the players; these are for them.
MOVE_SPEED = 320.0
CROSS_SECONDS = 0.25
# Where each player starts, in the order they joined: which wall, and
# how far along it (0 = left end, 1 = right end).
PLAYER_STARTS: Tuple[Tuple[int, float], ...] = (
    (TOP, 0.35),
    (BOTTOM, 0.65),
)
MAX_PLAYERS = len(PLAYER_STARTS)
# A bolt or bullet hits a zombie that is within this of its line, on
# top of the zombie's own size: being roughly in front is enough.
SHOT_WIDTH = 5.0

# The crowd at a wall stands this many map units deep, so dozens of
# zombies fit.
PILE_DEPTH = 30.0
# The most zombies one wave brings.
MAX_WAVE = 50
# A wave at the very start of a level is this share of the level's
# usual wave, and grows by this much by the end.
WAVE_START = 0.35
WAVE_GROWTH = 1.65

# Ogres close every level: this many for each player, one more for
# every few levels, up to a limit. The first sets off when this share
# of the level has gone and the rest follow through what is left, the
# last of them reaching the house with about this long on the clock.
BOSS_KIND = "ogre"
BOSSES_PER_PLAYER = 2
BOSS_EVERY_LEVELS = 4
MAX_BOSSES_PER_PLAYER = 6
BOSS_FROM = 0.5
BOSS_SECONDS_LEFT = 16.0
# When there isn't that much of the level to spread them over, they
# still come this far apart.
BOSS_STAGGER = 2.5
# The first levels' ogres are young ones: this share of an ogre's
# strength on level 1, and this much more each level until full-grown.
BOSS_HP_START = 0.55
BOSS_HP_GROWTH = 0.15

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
    # 'lob'   thrown straight out; hurts (or freezes) every zombie around
    #         where it lands
    # 'roll'  rolls straight out from the house to the edge of the map,
    #         hitting every zombie it touches: the boulders, which are
    #         nobody's to carry
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
    # Every player carries it; it isn't one of the weapons they pick.
    given: bool = False

    @property
    def area(self) -> bool:
        return self.kind != "shot"


# From a wall to past the edge of the map.
FULL_RANGE = HOUSE_TOP + 60.0

WEAPONS: Dict[str, Weapon] = {
    w.key: w
    for w in (
        Weapon(
            "crossbow", "Crossbow", "shot",
            damage=40, range=200, speed=520, reload=0.5, ammo=None,
            blurb="Dependable. Never runs out of bolts.",
        ),
        Weapon(
            "machine_gun", "Machine gun", "shot",
            damage=16, range=215, speed=760, reload=0.1, ammo=360,
            blurb="Small bullets, lots of them.",
        ),
        Weapon(
            "sniper", "Sniper rifle", "shot",
            damage=260, range=FULL_RANGE, speed=1500, reload=1.5, ammo=28,
            blurb="Reaches the edge of the map and hits hard. Slow to reload.",
        ),
        Weapon(
            "shotgun", "Shotgun", "cone",
            damage=70, range=125, speed=700, reload=0.85, ammo=52,
            spread=math.radians(20), falloff=0.5,
            blurb="Hits everything in front of you, up close.",
        ),
        Weapon(
            "flamethrower", "Flamethrower", "cone",
            damage=11, range=95, speed=300, reload=0.08, ammo=520,
            spread=math.radians(17), falloff=0.7,
            blurb="Burns a whole crowd, but only at the doorstep.",
        ),
        Weapon(
            "grenade", "Grenade", "lob",
            damage=220, range=230, speed=260, reload=1.3, ammo=18,
            radius=75, falloff=0.4,
            blurb="Goes off at the first zombie in front of you. A big blast.",
        ),
        Weapon(
            "snowball", "Snowball", "lob",
            damage=0, range=240, speed=320, reload=1.2, ammo=20,
            radius=70, freeze=6.0,
            blurb="Freezes the whole group where it lands. Doesn't hurt them.",
        ),
        # For when they are at the wall: it reaches the crowd piled up
        # against it (``PILE_DEPTH``) and nothing further out.
        Weapon(
            "chainsaw", "Chainsaw", "cone",
            damage=14, range=44, speed=200, reload=0.12, ammo=None,
            spread=math.radians(32), given=True,
            blurb="Cuts up whatever is at the wall in front of you. Reaches no further.",
        ),
    )
}
WEAPON_KEYS: Tuple[str, ...] = tuple(WEAPONS)
DEFAULT_WEAPON = "crossbow"
# Everyone carries the crossbow, this many weapons of their choice...
LOADOUT_PICKS = 2
# ...and these, which nobody has to pick.
GIVEN_WEAPONS: Tuple[str, ...] = tuple(w.key for w in WEAPONS.values() if w.given)
DEFAULT_LOADOUT: Tuple[str, ...] = ("shotgun", "machine_gun")

# A phone's clock and the server's never agree exactly, and a bullet
# reported as a hit left a moment ago: allow this much extra reach, and
# this much to either side of the line it was fired along.
_REACH_SLACK = 48.0
_LANE_SLACK = 30.0
# A thrown weapon lands at least this far out, and this far out when
# the phone doesn't say.
_MIN_THROW = 30.0
_USUAL_THROW = 0.6
# Shots may arrive bunched up after a network hiccup. Each weapon can
# be this far "ahead" of its reload time before shots are refused.
_BURST_SECONDS = 0.5
_RATE_TOLERANCE = 1.1


# ---------------------------------------------------------------------------
# The boulders at the house
# ---------------------------------------------------------------------------

BOULDER = Weapon(
    "boulder", "Boulder", "roll",
    damage=100_000, range=FULL_RANGE, speed=190, reload=0, ammo=None, radius=20,
    blurb="Rolls straight out from the house and flattens everything in its way.",
)
# Where along the house each lot is roped up, left and right: two
# against the top wall and two against the bottom, side by side, this
# far either side of that. Cut loose, the pair at each wall clears a
# lane a hundred wide from the house to the edge of the map.
SMASH_X: Tuple[float, ...] = (HOUSE_X - 75.0, HOUSE_X + 75.0)
SMASH_APART = 21.0
# How far out from the wall they sit, so the pair at the bottom wall
# doesn't hide whoever is defending it.
SMASH_OUT = 22.0


# ---------------------------------------------------------------------------
# TNT
# ---------------------------------------------------------------------------

# A crate of TNT is no player's weapon, but it goes off like one: a
# grenade's blast and then some.
TNT = Weapon(
    "tnt", "TNT", "lob",
    damage=240, range=0, speed=0, reload=0, ammo=None,
    radius=90, falloff=0.4,
    blurb="Shoot it and it blows up everything around it.",
)
# Half the width of a crate: what a bolt, a spray or a boulder has to touch.
TNT_SIZE = 9.0
# Crates land this far out from a wall: in reach of a crossbow, and
# near enough for one at the near end to clear the crowd at the wall.
TNT_BAND: Tuple[float, float] = (40.0, 170.0)
# One nobody has shot is taken away after this long.
TNT_SECONDS = 25.0
# The most on the map at once, and how far apart they land.
TNT_MAX = 3
TNT_APART = 60.0
# None in the first seconds of a level, nor this soon after the last.
TNT_FIRST = 5.0
TNT_GAP = 6.0
# How many land a second while there are zombies on the map: this, and
# this much more for every walker's worth of them, up to a limit. A
# handful of zombies gets a crate every half a minute or so, an
# invasion of forty one every six or seven seconds.
TNT_RATE = 0.01
TNT_RATE_PER_THREAT = 0.004
TNT_MAX_RATE = 0.16
# Each one comes this much sooner or later than that, by luck.
TNT_LUCK: Tuple[float, float] = (0.5, 1.5)
# It lands in the way of one of the zombies: where that one will be in
# this many seconds, give or take this far across and along.
TNT_LEAD: Tuple[float, float] = (1.5, 4.0)
TNT_SCATTER: Tuple[float, float] = (30.0, 25.0)


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


# A vampire darts this far to one side of its line and then the other,
# coming this much nearer the house with each dart; the last stretch to
# the wall it runs straight.
VAMPIRE_SPEED = 72.0
VAMPIRE_DART = (30.0, 55.0)
VAMPIRE_STRIDE = (36.0, 58.0)
VAMPIRE_RUN_IN = 46.0
# The first comes when this share of the level has gone. After a
# vampire is killed the next leaves the players in peace for a moment,
# and none sets off with less than this left on the clock.
VAMPIRE_FIRST = (0.12, 0.2)
VAMPIRE_REST = 4.0
VAMPIRE_LAST = 12.0


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
    weight: float      # how often a wave picks it, next to the others
    flying: bool = False
    # Darts from side to side on its way in instead of walking straight.
    zigzag: bool = False
    # Comes on its own, not in the waves, and only when the last of its
    # kind is dead: there is never more than one on the map.
    lone: bool = False


# Small and individually weak, so that dozens can be on the map at
# once: a crowd is what the area weapons are for.
ZOMBIE_TYPES: Dict[str, ZombieType] = {
    z.key: z
    for z in (
        ZombieType("walker", "Zombie", hp=30, speed=22, dps=3, radius=8,
                   threat=1.0, from_level=1, weight=5.0),
        ZombieType("dog", "Zombie dog", hp=18, speed=58, dps=2, radius=6,
                   threat=0.8, from_level=1, weight=3.0),
        ZombieType("bat", "Bat", hp=12, speed=75, dps=1.5, radius=6,
                   threat=0.7, from_level=2, weight=2.5, flying=True),
        ZombieType("ogre", "Ogre", hp=400, speed=14, dps=14, radius=15,
                   threat=6.0, from_level=4, weight=0.7),
        # Its speed is along its zigzag, so it closes on the house at a
        # little over half of it: twice as fast as a zombie walks.
        ZombieType("vampire", "Vampire", hp=120, speed=VAMPIRE_SPEED, dps=8, radius=8,
                   threat=3.0, from_level=1, weight=0.0, zigzag=True, lone=True),
    )
}
ZOMBIE_KEYS: Tuple[str, ...] = tuple(ZOMBIE_TYPES)

# ---------------------------------------------------------------------------
# Guard dogs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GuardDog:
    key: str
    name: str
    breed: str
    hp: float      # strength: how much of a mauling it takes before it runs home
    bite: float    # attack: the damage one bite does
    speed: float   # map units a second
    blurb: str


GUARD_DOGS: Dict[str, GuardDog] = {
    d.key: d
    for d in (
        GuardDog(
            "juno", "Juno", "Bernese mountain dog", hp=100, bite=32, speed=125,
            blurb="Good at everything: sturdy, a solid bite and quick enough.",
        ),
        GuardDog(
            "wynnie", "Wynnie", "Newfoundland", hp=180, bite=46, speed=80,
            blurb="Big and black. Takes a beating and bites hard, but takes her time.",
        ),
        GuardDog(
            "wolf", "Wolf", "Husky and spaniel mix", hp=60, bite=26, speed=200,
            blurb="Small, white and very fast. Gets hurt easily.",
        ),
        GuardDog(
            "missy", "Missy", "English bulldog", hp=100, bite=64, speed=80,
            blurb="Slow on her short legs, but nothing bites harder.",
        ),
    )
}
GUARD_DOG_KEYS: Tuple[str, ...] = tuple(GUARD_DOGS)
DEFAULT_GUARD_DOG = "juno"

# A dog goes for zombies on foot that come within this of a wall...
GUARD_RANGE = 110.0
# ...bites this often, from this close (on top of the zombie's size)...
GUARD_BITE_EVERY = 0.5
GUARD_BITE_REACH = 7.0
# ...and comes home after this many kills, or sooner if it gets hurt.
GUARD_MAX_KILLS = 3
# Zombies this close to a dog (on top of their size) maul it, this many
# times harder than they hit the house.
GUARD_MAUL_REACH = 12.0
GUARD_MAUL = 5.0
# Then it rests in the house: this long after an easy outing, up to
# this long when it came home badly hurt. It never dies.
GUARD_REST: Tuple[float, float] = (10.0, 20.0)
# Where each player's dog lies in the house, as an offset from its
# middle, in the order the players joined.
GUARD_HOMES: Tuple[float, ...] = (-100.0, 100.0)
DOG_HOME, DOG_OUT, DOG_BACK = 0, 1, 2


def clean_guard_dog(raw: Any) -> Optional[str]:
    """The guard dog a player picked, if there is such a dog."""
    return raw if isinstance(raw, str) and raw in GUARD_DOGS else None


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

    def set_off(self, t0: float) -> None:
        """Start down the rail at ``t0`` instead of when it was going to."""
        walk = self.arrive - self.t0
        self.t0 = round(t0, 2)
        self.arrive = self.t0 + walk

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
    zombies, so two players don't have it easier than one.

    ``wave_budget`` is the size of a wave half-way through the level,
    in walkers; ``wave_every`` the seconds between waves at that point.
    ``types`` are the kinds the waves are made of, ``bosses`` how many
    ogres come from half-way through the level whatever the waves had,
    and ``lone`` the kinds that come one at a time, about every
    ``lone_every`` seconds.
    """
    step = max(1, min(MAX_LEVEL, int(level))) - 1
    team = max(1, min(MAX_PLAYERS, int(players)))
    crowd = 0.4 + 0.6 * team
    return {
        "hp": (1.0 + 0.07 * step) * (1.0 + 0.08 * (team - 1)),
        "speed": min(1.7, 1.0 + 0.035 * step),
        "dps": 1.0 + 0.06 * step,
        "wave_every": max(5.0, 9.0 - 0.25 * step),
        "wave_budget": (6.0 + 0.7 * step) * crowd,
        "both_sides": max(0.0, min(0.8, 0.12 * (step - 1))),
        "types": [
            z.key for z in ZOMBIE_TYPES.values()
            if z.from_level <= step + 1 and not z.lone
        ],
        "bosses": team * min(
            MAX_BOSSES_PER_PLAYER, BOSSES_PER_PLAYER + step // BOSS_EVERY_LEVELS
        ),
        "boss_hp": min(1.0, BOSS_HP_START + BOSS_HP_GROWTH * step),
        "lone": [
            z.key for z in ZOMBIE_TYPES.values()
            if z.from_level <= step + 1 and z.lone
        ],
        "lone_every": max(16.0, 34.0 - 2.0 * step),
    }


def kinds_coming(level: int, players: int) -> List[str]:
    """Every kind of zombie a level has, in the usual order."""
    diff = difficulty(level, players)
    kinds = {*diff["types"], *diff["lone"]}
    if diff["bosses"]:
        kinds.add(BOSS_KIND)
    return [key for key in ZOMBIE_KEYS if key in kinds]


def build_level(
    level: int, players: int, duration: float, seed: int
) -> List[Zombie]:
    """Every zombie of one level, in the order they appear.

    The same arguments always give the same level. Waves start a
    second or two in and keep coming until just before the end; each
    spends a budget on zombies picked at random from the kinds this
    level has, and comes in from the top, the bottom, or both. The
    level's ogres set off from half-way through, at one wall and then
    the other, the last of them in time to reach the house for the
    final ``BOSS_SECONDS_LEFT`` seconds.

    Vampires are in the list at the earliest they may set off. A
    :class:`Round` keeps each one back until the one before it is dead.
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
    side = rng.choice((-1, 1))
    first = duration * BOSS_FROM
    bosses = diff["bosses"]
    for n in range(bosses):
        boss = _spawn(
            rng, ZOMBIE_TYPES[BOSS_KIND], diff, trees, side, t0=0.0, hp=diff["boss_hp"]
        )
        # What is left of the level once the last has had time to walk in.
        spare = duration - BOSS_SECONDS_LEFT - boss.arrive - first
        spread = spare * n / (bosses - 1) if bosses > 1 else 0.0
        boss.set_off(first + max(BOSS_STAGGER * n, spread))
        zombies.append(boss)
        side = -side
    # Closer together in a short level, so that it has more than one.
    every = min(diff["lone_every"], duration * 0.4)
    for key in diff["lone"]:
        t = duration * rng.uniform(*VAMPIRE_FIRST)
        while t < duration - VAMPIRE_LAST:
            zombies.append(
                _spawn(rng, ZOMBIE_TYPES[key], diff, trees, rng.choice((-1, 1)), t0=t)
            )
            t += every * rng.uniform(0.85, 1.15)
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
    hp: float = 1.0,
) -> Zombie:
    x0 = rng.uniform(16.0, WORLD_W - 16.0)
    # Mostly straight at the house, drifting a little to one side.
    aim = min(HOUSE_RIGHT - 8.0, max(HOUSE_LEFT + 8.0, x0 + rng.uniform(-70.0, 70.0)))
    off = kind.radius + 8.0 + rng.uniform(0.0, 26.0)
    y0 = -off if side < 0 else WORLD_H + off
    if kind.flying:
        # Bats hover over the crowd, a little way out from the wall.
        gap = rng.uniform(6.0, PILE_DEPTH)
    else:
        # Everyone else piles up, several deep, against the wall they
        # came to.
        gap = kind.radius + rng.uniform(0.0, PILE_DEPTH)
    tx = aim
    ty = HOUSE_TOP - gap if side < 0 else HOUSE_BOTTOM + gap
    start, end = (x0, y0), (tx, ty)
    if kind.flying:
        corners = [end]
    elif kind.zigzag:
        corners = _zigzag(rng, start, end, trees, kind.radius)
    else:
        corners = _around_trees(rng, start, end, trees, kind.radius)
    return Zombie(
        0,
        kind,
        t0=t0,
        path=[start, *corners],
        speed=kind.speed * diff["speed"] * rng.uniform(0.9, 1.1),
        hp=kind.hp * diff["hp"] * hp,
        dps=kind.dps * diff["dps"],
    )


def _zigzag(
    rng: random.Random,
    start: Tuple[float, float],
    end: Tuple[float, float],
    trees: Sequence[Tree],
    radius: float,
) -> List[Tuple[float, float]]:
    """The corners of a vampire's way in: side to side across its line.

    Returns the points after ``start``, ending with ``end``, like
    :func:`_around_trees`, which each dart is bent round the trees by.
    No two vampires dart alike: how far to the side and how far on each
    one goes is drawn afresh every time.
    """
    (x0, y0), (tx, ty) = start, end
    depth = abs(ty - y0)
    heading = 1.0 if ty > y0 else -1.0
    way = rng.choice((-1.0, 1.0))
    corners: List[Tuple[float, float]] = []
    here = start
    gone = rng.uniform(*VAMPIRE_STRIDE)
    while gone <= depth - VAMPIRE_RUN_IN:
        y = y0 + heading * gone
        # Along the edge of the map it keeps far enough in to have room
        # to dart both ways.
        room = radius + 6.0 + VAMPIRE_DART[0]
        middle = min(WORLD_W - room, max(room, x0 + (tx - x0) * gone / depth))
        dart = rng.uniform(*VAMPIRE_DART)
        # Where a tree stands in the way it turns short of it...
        for share in (1.0, 0.8, 0.6, 0.4):
            x = middle + way * dart * share
            x = min(WORLD_W - radius - 6.0, max(radius + 6.0, x))
            if any(
                math.hypot(x - cx, y - cy) <= tree_radius + radius + _TREE_CLEARANCE + 2.0
                for cx, cy, tree_radius in trees
            ):
                continue
            dart_to = _around_trees(rng, here, (x, y), trees, radius)
            # ...and it doesn't double back to get round one.
            if all(
                (by - ay) * heading > -6.0
                for (_ax, ay), (_bx, by) in zip([here, *dart_to], dart_to)
            ):
                break
        else:
            # With no room at all, it turns a little further on.
            gone += 5.0
            continue
        corners.extend(dart_to)
        here = (x, y)
        way = -way
        gone += rng.uniform(*VAMPIRE_STRIDE)
    corners.extend(_around_trees(rng, here, end, trees, radius))
    return corners


# ---------------------------------------------------------------------------
# One level being played
# ---------------------------------------------------------------------------


class _Fighter:
    """A player in a round: where they stand and what they carry."""

    __slots__ = (
        "player_id", "slot", "x", "side", "weapons", "weapon", "ammo", "kills",
        "shots", "ack", "_tokens", "_stamp", "_flying",
    )

    def __init__(
        self, player_id: int, slot: int, picks: Sequence[str], duration: float
    ) -> None:
        self.player_id = player_id
        self.slot = slot
        self.side, along = PLAYER_STARTS[slot]
        self.x = round(TRACK_LEFT + (TRACK_RIGHT - TRACK_LEFT) * along, 1)
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
        # shot id -> (weapon, when it was fired, from where, facing which
        # way): bolts and bullets in the air
        self._flying: Dict[int, Tuple[Weapon, float, float, int]] = {}

    @property
    def y(self) -> float:
        return track_y(self.side)


class _Dog:
    """A player's guard dog: at home in the house, out biting, or on its way back."""

    __slots__ = (
        "fighter", "kind", "hx", "hy", "x", "y", "hp", "state", "kills", "total",
        "target", "bite_at", "ready_at", "sent",
    )

    def __init__(self, fighter: _Fighter, kind: GuardDog) -> None:
        self.fighter = fighter
        self.kind = kind
        self.hx = HOUSE_X + GUARD_HOMES[fighter.slot % len(GUARD_HOMES)]
        self.hy = HOUSE_Y
        self.x, self.y = self.hx, self.hy
        self.hp = kind.hp
        self.state = DOG_HOME
        self.kills = 0           # this outing
        self.total = 0           # this level
        self.target: Optional[Zombie] = None
        self.bite_at = 0.0
        self.ready_at = 0.0      # when its rest is over
        # As the phones were last told; a snapshot says where it starts.
        self.sent: List[Any] = self.wire()

    def wire(self) -> List[Any]:
        """``[player, x, y, state, health %, when its rest ends]``."""
        resting = self.state == DOG_HOME
        return [
            self.fighter.player_id,
            round(self.x, 1),
            round(self.y, 1),
            self.state,
            int(math.ceil(100 * self.hp / self.kind.hp)),
            round(self.ready_at, 1) if resting else 0,
        ]


class _Roll:
    """A boulder on its way out from the house."""

    __slots__ = ("fighter", "weapon", "x", "y", "ux", "uy", "gone", "limit")

    def __init__(self, fighter: _Fighter, weapon: Weapon, x: float, side: int) -> None:
        """Let go at ``x`` from the wall on ``side``; what it kills is ``fighter``'s."""
        self.fighter = fighter
        self.weapon = weapon
        self.x = x
        self.y = HOUSE_TOP if side == TOP else HOUSE_BOTTOM
        self.ux, self.uy = 0.0, float(side)
        self.gone = 0.0
        self.limit = weapon.range


class _Crate:
    """A crate of TNT standing on the map until something sets it off."""

    __slots__ = ("id", "x", "y", "until")

    def __init__(self, crate_id: int, x: float, y: float, until: float) -> None:
        self.id = crate_id
        # Rounded as they are sent.
        self.x, self.y = round(x, 1), round(y, 1)
        self.until = round(until, 1)

    def wire(self) -> List[Any]:
        """``[id, x, y, when it is taken away]``."""
        return [self.id, self.x, self.y, self.until]


def track_y(side: int) -> float:
    """How far down the map a player on that wall stands."""
    return HOUSE_TOP + TRACK_INSET if side < 0 else HOUSE_BOTTOM - TRACK_INSET


def _burst(weapon: Weapon) -> float:
    return max(1.5, _BURST_SECONDS / weapon.reload)


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


class Round:
    """One level in play: the zombies, the house and the players' weapons.

    ``advance`` moves the clock on; ``move`` / ``fire`` / ``hit`` /
    ``switch`` are what players do; ``drain`` hands back what changed
    since it was last called, ready to send to every phone.
    """

    def __init__(
        self,
        *,
        level: int,
        duration: float,
        seed: int,
        players: Sequence[Sequence[Any]],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """``players`` is ``(player id, weapons picked[, guard dog])`` for each."""
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
        self.dogs: Dict[int, _Dog] = {}
        for slot, (player_id, picks, *more) in enumerate(players[:MAX_PLAYERS]):
            fighter = self.fighters[player_id] = _Fighter(
                player_id,
                slot,
                clean_loadout(list(picks)) or DEFAULT_LOADOUT,
                self.duration,
            )
            dog = clean_guard_dog(more[0] if more else None) or DEFAULT_GUARD_DOG
            self.dogs[player_id] = _Dog(fighter, GUARD_DOGS[dog])
        self.trees = build_trees(seed)
        plan = build_level(level, len(self.fighters), duration, seed)
        # Those that come when the plan says, in order: what the phones
        # are sent. A vampire joins them, where it belongs in that
        # order, when it sets off.
        self.zombies = [z for z in plan if not z.type.lone]
        self._waiting = [z for z in plan if z.type.lone]
        self._lone: Optional[Zombie] = None  # the vampire on the map
        self._lone_from = 0.0                # the next may not set off before this
        self._by_id = {z.id: z for z in plan}
        self._next = 0                       # next zombie to appear
        self._active: Dict[int, Zombie] = {}  # on the map and alive
        self._set_off: List[Zombie] = []      # vampires out since the last drain()
        self._lobs: List[Tuple[float, _Fighter, Weapon, float, float]] = []
        self._rolls: List[_Roll] = []
        self.smashes = [True] * len(SMASH_X)  # the boulders still roped up
        self._smashed: List[List[int]] = []   # which lot, player: cut loose since the last drain()
        self.crates: Dict[int, _Crate] = {}   # TNT on the map
        self._crate_ids = 0
        self._tnt_rng = random.Random(f"tnt:{seed}")
        self._tnt_at = TNT_FIRST              # when to next think about a crate
        self._tnt_due = 0.0                   # how nearly it is time for one...
        self._tnt_when = self._tnt_rng.uniform(*TNT_LUCK)  # ...of this much
        self._tnt_after = 0.0                 # none may land before this
        self._landed: List[_Crate] = []       # crates down since the last drain()
        self._blown: List[List[int]] = []     # crate, player: gone off since then
        self._frozen: Dict[int, Zombie] = {}  # frozen since the last drain()
        # What has changed since the last drain().
        self._hurt: Dict[int, int] = {}       # zombie id -> player who hit it last
        self._fired: List[List[Any]] = []
        self._switched: Dict[int, int] = {}
        self._moved: Dict[int, _Fighter] = {}
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
        if (
            self._waiting
            and self._lone is None
            and t >= self._lone_from
            and self._waiting[0].t0 <= t
        ):
            self._release(self._waiting.pop(0))
        self._tnt()
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
        if self.dogs and t > 0:
            self._run_dogs(dt)
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

    def _release(self, vampire: Zombie) -> None:
        """Send the next vampire in, now that no other is on the map."""
        vampire.set_off(self.t)
        # Everything before ``_next`` has set off and nothing after it
        # has, so this keeps the list in the order the phones expect.
        self.zombies.insert(self._next, vampire)
        self._next += 1
        self._active[vampire.id] = vampire
        self._lone = vampire
        self._set_off.append(vampire)

    def _tnt(self) -> None:
        """Take away crates nobody shot, and perhaps put down a new one."""
        t = self.t
        if self.crates:
            for crate in [c for c in self.crates.values() if c.until <= t]:
                del self.crates[crate.id]
        # Second by second the next one comes nearer, faster the more
        # zombies there are to blow up.
        while self._tnt_at <= t:
            self._tnt_at += 1.0
            threat = sum(z.type.threat for z in self._active.values())
            if not threat:
                continue
            self._tnt_due = min(
                self._tnt_when,
                self._tnt_due + min(TNT_MAX_RATE, TNT_RATE + TNT_RATE_PER_THREAT * threat),
            )
            if (
                self._tnt_due < self._tnt_when
                or t < self._tnt_after
                or len(self.crates) >= TNT_MAX
            ):
                continue
            spot = self._tnt_spot()
            if spot is None:
                continue
            self._crate_ids += 1
            crate = _Crate(self._crate_ids, spot[0], spot[1], t + TNT_SECONDS)
            self.crates[crate.id] = crate
            self._landed.append(crate)
            self._tnt_due = 0.0
            self._tnt_when = self._tnt_rng.uniform(*TNT_LUCK)
            self._tnt_after = t + TNT_GAP

    def _tnt_spot(self) -> Optional[Tuple[float, float]]:
        """Somewhere for a crate to land, in the way of the zombies."""
        rng = self._tnt_rng
        zombies = list(self._active.values())
        near, far = TNT_BAND
        for _ in range(8):
            zombie = rng.choice(zombies)
            x, y = zombie.position(self.t + rng.uniform(*TNT_LEAD))
            x += rng.uniform(-1.0, 1.0) * TNT_SCATTER[0]
            x = min(TRACK_RIGHT, max(TRACK_LEFT, x))
            # In front of whichever wall that zombie is making for.
            side = TOP if zombie.ty < HOUSE_Y else BOTTOM
            out = (HOUSE_TOP - y) if side == TOP else (y - HOUSE_BOTTOM)
            out += rng.uniform(-1.0, 1.0) * TNT_SCATTER[1]
            out = min(far, max(near, out))
            y = HOUSE_TOP - out if side == TOP else HOUSE_BOTTOM + out
            if any(
                math.hypot(x - cx, y - cy) < radius + TNT_SIZE + _TREE_CLEARANCE
                for cx, cy, radius in self.trees
            ):
                continue
            if any(
                math.hypot(x - c.x, y - c.y) < TNT_APART for c in self.crates.values()
            ):
                continue
            return x, y
        return None

    # ---- what players do -------------------------------------------------

    def move(self, player_id: int, message: Dict[str, Any]) -> None:
        """A player slid along their wall, or changed walls."""
        fighter = self.fighters.get(player_id)
        if fighter is not None:
            self._place(fighter, message)

    def _place(self, fighter: _Fighter, message: Dict[str, Any]) -> None:
        x, side = _number(message.get("x")), message.get("d")
        if x is None or side not in (TOP, BOTTOM) or isinstance(side, bool):
            return
        x = round(min(TRACK_RIGHT, max(TRACK_LEFT, x)), 1)
        if x != fighter.x or side != fighter.side:
            fighter.x, fighter.side = x, side
            self._moved[fighter.player_id] = fighter

    def switch(self, player_id: int, message: Dict[str, Any]) -> None:
        fighter = self.fighters.get(player_id)
        index = message.get("w")
        if fighter is None or not isinstance(index, int) or isinstance(index, bool):
            return
        if 0 <= index < len(fighter.weapons) and index != fighter.weapon:
            self._draw(fighter, index)

    def fire(self, player_id: int, message: Dict[str, Any]) -> bool:
        """A player fired a weapon, straight out from where they stand.

        The message may say where that is (``x``, ``d``), so the shot
        leaves from where the player saw themselves. Returns whether
        the shot counted.
        """
        fighter = self.fighters.get(player_id)
        if fighter is None:
            return False
        self._ack(fighter, message)
        index = message.get("w")
        if (
            self.over is not None
            or self.t < 0
            or not isinstance(index, int)
            or isinstance(index, bool)
            or not 0 <= index < len(fighter.weapons)
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
        self._place(fighter, message)

        throw = 0.0
        if weapon.kind == "cone":
            self._spray(fighter, weapon)
        elif weapon.kind == "lob":
            # How far out it lands: the phone picks (the first zombie
            # in front of the player), within what the weapon can do.
            throw = _number(message.get("r"))
            if throw is None:
                throw = weapon.range * _USUAL_THROW
            throw = round(min(weapon.range, max(_MIN_THROW, throw)), 1)
            self._lobs.append((
                self.t + throw / weapon.speed,
                fighter,
                weapon,
                fighter.x,
                fighter.y + fighter.side * throw,
            ))
        else:
            shot_id = message.get("s")
            if isinstance(shot_id, int) and not isinstance(shot_id, bool):
                if len(fighter._flying) > 200:
                    self._forget_old_shots(fighter)
                fighter._flying[shot_id] = (weapon, self.clock(), fighter.x, fighter.side)
        self._fired.append([player_id, index, fighter.x, fighter.side, throw])
        return True

    def smash(self, player_id: int, message: Dict[str, Any]) -> bool:
        """A player cut loose the boulders at the left (``b`` 0) or the right (1).

        Once a level for each. Returns whether they rolled.
        """
        fighter = self.fighters.get(player_id)
        index = message.get("b")
        if (
            fighter is None
            or self.over is not None
            or self.t < 0
            or not isinstance(index, int)
            or isinstance(index, bool)
            or not 0 <= index < len(SMASH_X)
            or not self.smashes[index]
        ):
            return False
        self.smashes[index] = False
        for wall in (TOP, BOTTOM):
            for off in (-SMASH_APART, SMASH_APART):
                self._rolls.append(_Roll(fighter, BOULDER, SMASH_X[index] + off, wall))
        self._smashed.append([index, player_id])
        return True

    def hit(self, player_id: int, message: Dict[str, Any]) -> bool:
        """A player's phone says one of their bolts or bullets hit something.

        ``z`` is the zombie it hit, or ``c`` the crate of TNT.
        """
        fighter = self.fighters.get(player_id)
        if fighter is None:
            return False
        self._ack(fighter, message)
        shot_id = message.get("s")
        zombie_id, crate_id = message.get("z"), message.get("c")
        if not isinstance(shot_id, int) or not isinstance(
            crate_id if zombie_id is None else zombie_id, int
        ):
            return False
        flying = fighter._flying.pop(shot_id, None)
        if flying is None or self.over is not None:
            return False
        weapon, fired_at, from_x, side = flying
        if self.clock() - fired_at > weapon.range / weapon.speed + 1.5:
            return False
        if zombie_id is None:
            # A crate doesn't move, so there is little to allow for.
            crate = self.crates.get(crate_id)
            if crate is None:
                return False
            out = (crate.y - track_y(side)) * side
            if (
                abs(crate.x - from_x) > TNT_SIZE + SHOT_WIDTH + 2.0
                or not 0.0 < out <= weapon.range + TNT_SIZE
            ):
                return False
            self._explode(crate, fighter)
            return True
        zombie = self._active.get(zombie_id)
        if zombie is None:
            return False
        x, y = zombie.position(self.t)
        out = (y - track_y(side)) * side
        if (
            abs(x - from_x) > zombie.radius + SHOT_WIDTH + _LANE_SLACK
            or not -_REACH_SLACK <= out <= weapon.range + _REACH_SLACK
        ):
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
            shot_id: shot
            for shot_id, shot in fighter._flying.items()
            if now - shot[1] <= shot[0].range / shot[0].speed + 1.5
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
            if zombie is self._lone:
                self._lone = None
                self._lone_from = self.t + VAMPIRE_REST
        self._hurt[zombie.id] = fighter.player_id

    def _spray(self, fighter: _Fighter, weapon: Weapon) -> None:
        angle = fighter.side * math.pi / 2
        for zombie in list(self._active.values()):
            x, y = zombie.position(self.t)
            share = cone_share(
                weapon, fighter.x, fighter.y, angle, x, y, zombie.radius
            )
            if share > 0:
                self._damage(zombie, weapon.damage * share, fighter)
        for crate in list(self.crates.values()):
            if cone_share(weapon, fighter.x, fighter.y, angle, crate.x, crate.y, TNT_SIZE) > 0:
                self._explode(crate, fighter)

    def _blast(self, fighter: _Fighter, weapon: Weapon, bx: float, by: float) -> None:
        for zombie in list(self._active.values()):
            x, y = zombie.position(self.t)
            share = blast_share(weapon, bx, by, x, y, zombie.radius)
            if share > 0:
                if weapon.freeze:
                    zombie.freeze(self.t, weapon.freeze)
                    self._frozen[zombie.id] = zombie
                self._damage(zombie, weapon.damage * share, fighter)
        if weapon.damage > 0:
            # One crate of TNT sets off the next.
            for crate in list(self.crates.values()):
                if math.hypot(crate.x - bx, crate.y - by) <= weapon.radius + TNT_SIZE:
                    self._explode(crate, fighter)

    def _explode(self, crate: _Crate, fighter: _Fighter) -> None:
        """A crate of TNT goes off; what it kills is ``fighter``'s."""
        if self.crates.pop(crate.id, None) is None:
            return
        self._blown.append([crate.id, fighter.player_id])
        self._blast(fighter, TNT, crate.x, crate.y)

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
        reach = roll.weapon.radius + TNT_SIZE
        for crate in list(self.crates.values()):
            dx, dy = crate.x - roll.x, crate.y - roll.y
            along = dx * roll.ux + dy * roll.uy
            if (
                start - reach <= along <= roll.gone + reach
                and abs(dx * roll.uy - dy * roll.ux) <= reach
            ):
                self._explode(crate, roll.fighter)

    # ---- guard dogs --------------------------------------------------------

    def _run_dogs(self, dt: float) -> None:
        """Move the dogs on by ``dt``, in steps small enough to bite on time."""
        left = dt
        while left > 1e-9:
            step = min(left, 0.1)
            left -= step
            for dog in self.dogs.values():
                self._run_dog(dog, self.t - left, step)

    def _run_dog(self, dog: _Dog, t: float, dt: float) -> None:
        kind = dog.kind
        if dog.state == DOG_HOME:
            if t < dog.ready_at:
                return
            dog.hp = kind.hp  # rested
            dog.target = self._dog_target(dog, t)
            if dog.target is None:
                return
            dog.state, dog.kills = DOG_OUT, 0
        if dog.state == DOG_OUT:
            zombie = dog.target
            if zombie is None or not zombie.alive:
                zombie = dog.target = self._dog_target(dog, t)
            if zombie is None:
                dog.state = DOG_BACK  # nothing left to chase
                return
            zx, zy = zombie.position(t)
            reach = zombie.radius + GUARD_BITE_REACH
            gap = math.hypot(zx - dog.x, zy - dog.y)
            if gap > reach:
                move = min(kind.speed * dt, gap - reach * 0.8)
                dog.x += (zx - dog.x) / gap * move
                dog.y += (zy - dog.y) / gap * move
            elif t >= dog.bite_at:
                dog.bite_at = t + GUARD_BITE_EVERY
                self._damage(zombie, kind.bite, dog.fighter)
                if not zombie.alive:
                    dog.kills += 1
                    dog.total += 1
                    dog.target = None
                    if dog.kills >= GUARD_MAX_KILLS:
                        dog.state = DOG_BACK
                        return
            # Whatever is within reach of the dog bites back.
            harm = 0.0
            for other in self._active.values():
                if other.flying or t < other.until:
                    continue
                ox, oy = other.position(t)
                if math.hypot(ox - dog.x, oy - dog.y) <= other.radius + GUARD_MAUL_REACH:
                    harm += other.dps
            if harm:
                dog.hp -= harm * GUARD_MAUL * dt
                if dog.hp <= 0:
                    dog.hp = 0.0
                    dog.target = None
                    dog.state = DOG_BACK
            return
        # On its way home, where nothing can get at it.
        gap = math.hypot(dog.hx - dog.x, dog.hy - dog.y)
        move = kind.speed * dt
        if gap > move:
            dog.x += (dog.hx - dog.x) / gap * move
            dog.y += (dog.hy - dog.y) / gap * move
            return
        dog.x, dog.y = dog.hx, dog.hy
        dog.state = DOG_HOME
        if dog.kills or dog.hp < kind.hp:
            short, long = GUARD_REST
            dog.ready_at = t + short + (long - short) * (1.0 - dog.hp / kind.hp)
        dog.kills = 0

    def _dog_target(self, dog: _Dog, t: float) -> Optional[Zombie]:
        """The zombie on foot nearest the dog, of those close to the house."""
        taken = {
            other.target.id
            for other in self.dogs.values()
            if other is not dog and other.target is not None
        }
        best: Optional[Zombie] = None
        nearest = math.inf
        for zombie in self._active.values():
            if zombie.flying:
                continue
            x, y = zombie.position(t)
            out = HOUSE_TOP - y if y < HOUSE_Y else y - HOUSE_BOTTOM
            if out > GUARD_RANGE:
                continue
            gap = math.hypot(x - dog.x, y - dog.y)
            if zombie.id in taken:
                gap += GUARD_RANGE  # leave it to the other dog if there is a choice
            if gap < nearest:
                best, nearest = zombie, gap
        return best

    # ---- what the phones are told ---------------------------------------

    def drain(self) -> Optional[Dict[str, Any]]:
        """Everything that changed since the last call, or ``None``.

        A message goes out at least once a second while the level is
        running, so the phones can keep their clocks in step.
        """
        house = int(math.ceil(self.house))
        dogs = []
        for dog in self.dogs.values():
            wire = dog.wire()
            if wire != dog.sent:
                dog.sent = wire
                dogs.append(wire)
        quiet = not (
            self._hurt or self._fired or self._switched or self._acked
            or self._frozen or self._moved or self._set_off or dogs
            or self._landed or self._blown or self._smashed
        )
        if (
            quiet
            and self.over is None
            and (house == self._sent_house or self.t - self._sent_at < 0.25)
            and self.t - self._sent_at < 1.0
        ):
            return None
        batch: Dict[str, Any] = {"type": "zt", "t": round(self.t, 2), "h": house}
        if self._set_off:
            # A vampire's rail, the way the rest were sent in the snapshot.
            batch["z"] = [z.wire() for z in self._set_off]
            self._set_off = []
        if self._landed:
            batch["c"] = [c.wire() for c in self._landed]
            self._landed = []
        if self._blown:
            batch["x"] = self._blown
            self._blown = []
        if self._smashed:
            batch["b"] = self._smashed
            self._smashed = []
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
        if self._moved:
            batch["m"] = [[f.player_id, f.x, f.side] for f in self._moved.values()]
            self._moved = {}
        if self._switched:
            batch["w"] = [[pid, index] for pid, index in self._switched.items()]
            self._switched = {}
        if dogs:
            batch["g"] = dogs
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
            "tnt": [c.wire() for c in self.crates.values()],
            "smash": [int(there) for there in self.smashes],
            "p": [
                {
                    "id": f.player_id,
                    "x": f.x,
                    "side": f.side,
                    "weapons": f.weapons,
                    "weapon": f.weapon,
                    "kills": f.kills,
                }
                for f in self.fighters.values()
            ],
            "dogs": [
                {"dog": dog.kind.key, "home": [dog.hx, dog.hy], "at": dog.wire()}
                for dog in self.dogs.values()
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
            # Of each player's kills, the ones their dog made.
            "dog_kills": {str(d.fighter.player_id): d.total for d in self.dogs.values()},
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
        "track": {
            "left": TRACK_LEFT,
            "right": TRACK_RIGHT,
            "top": track_y(TOP),
            "bottom": track_y(BOTTOM),
        },
        "move_speed": MOVE_SPEED,
        "cross_seconds": CROSS_SECONDS,
        "shot_width": SHOT_WIDTH,
        "pile_depth": PILE_DEPTH,
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
        "smash": {
            "x": list(SMASH_X),
            "apart": SMASH_APART,
            "out": SMASH_OUT,
            "radius": BOULDER.radius,
            "speed": BOULDER.speed,
            "range": BOULDER.range,
        },
        "tnt": {
            "radius": TNT.radius,
            "size": TNT_SIZE,
            "seconds": TNT_SECONDS,
        },
        "bat_weave": BAT_WEAVE,
        "bat_weave_rate": BAT_WEAVE_RATE,
        "guard_dogs": [
            {
                "key": d.key,
                "name": d.name,
                "breed": d.breed,
                "hp": d.hp,
                "bite": d.bite,
                "speed": d.speed,
                "blurb": d.blurb,
            }
            for d in GUARD_DOGS.values()
        ],
        "default_guard_dog": DEFAULT_GUARD_DOG,
        "guard_range": GUARD_RANGE,
        "guard_max_kills": GUARD_MAX_KILLS,
        "guard_rest": list(GUARD_REST),
    }

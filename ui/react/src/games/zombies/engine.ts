import { SHIRT_COLORS } from "../Turkey";
import type { GamePlayer, ShirtColor, SurvivorStyle, ZombieKind, ZombieRules, ZombieWeapon } from "../types";
import type { LiveMessage } from "../useGameSocket";
import { ZOMBIE_ART, drawBoulder, drawCanopies, drawGround, drawIce, drawSurvivor } from "./draw";
import { isSurvivorStyle } from "./survivors";

// A level of Zombie Apocalypse on the phone: the map, drawn on a canvas
// sixty times a second, and everything the player does in it.
//
// The server (python/api/games/zombie_apocalypse.py) sends the whole
// level once — every zombie's rail and when it sets off — and after
// that only what changed: who got hurt, who fired, the house's points.
// Where each zombie is comes from the clock alone, with the same sums
// the server does (`locate` here, `Zombie.position` there).
//
// The player's own bolts and bullets are flown here and the hits
// reported; sprays are guessed at straight away and corrected when the
// server answers, so nothing waits on the network to look right.
//
// None of this is React state: the screen around the map only hears
// the few numbers it shows (`Hud`), a few times a second.

const TAU = Math.PI * 2;
const TOP = -1;
const BOTTOM = 1;

/** How long a tap is remembered if the weapon wasn't quite ready. */
const TAP_MEMORY = 0.35;
/** Holding the button keeps firing weapons that reload at least this fast. */
const AUTO_RELOAD = 0.6;
/** Say where the player is at most this often, seconds. */
const MOVE_EVERY = 0.08;
/** With nothing heard from the server for this long, the map stands still. */
const STALL_AFTER = 1.5;
const MAX_DPR = 2;
/** How far apart two players on a wall must be for both names to fit. */
const NAME_ROOM = 26;

const clamp = (v: number, lo: number, hi: number) => (v < lo ? lo : v > hi ? hi : v);
const round1 = (v: number) => Math.round(v * 10) / 10;

export type Phase = "waiting" | "countdown" | "playing" | "won" | "lost";

/** What the screen around the map shows. */
export interface Hud {
  phase: Phase;
  /** Whole seconds until the level is survived. */
  left: number;
  house: number;
  houseMax: number;
  level: number;
  /** Whether this player is in the level (someone who joined part-way watches). */
  fighting: boolean;
  weapons: string[];
  /** Index into `weapons` of the one in hand. */
  weapon: number;
  ammo: (number | null)[];
  /** Counts this player's shots: each one restarts the reload timer on screen. */
  shot: number;
  /** By player id, this level. */
  kills: Record<number, number>;
  /** Nothing has been heard from the server for a moment. */
  stalled: boolean;
}

interface Zed {
  id: number;
  kind: ZombieKind;
  t0: number;
  speed: number;
  maxHp: number;
  /** As the server last said. */
  hp: number;
  /** x, y, x, y...: the corners of its rail. */
  path: number[];
  cum: number[];
  dist: number;
  arrive: number;
  phase: number;
  alive: boolean;
  stop: number;
  until: number;
  lag: number;
  /** Damage this phone has dealt that the server hasn't confirmed yet. */
  pend: number;
  /** Dead as far as this phone can tell; hidden until the server agrees or not. */
  gone: boolean;
  x: number;
  y: number;
  angle: number;
  /** Its own clock, which stops while it is frozen. */
  te: number;
  atHouse: boolean;
  hurtAt: number;
}

interface Fighter {
  id: number;
  name: string;
  style: SurvivorStyle | null;
  color: ShirtColor | null;
  x: number;
  side: number;
  /** Where they are heading. */
  tx: number;
  tside: number;
  /** Seconds left of walking through the house to the other wall. */
  cross: number;
  from: number;
  weapons: string[];
  weapon: number;
  kills: number;
  firedAt: number;
}

interface Bolt {
  w: ZombieWeapon;
  x: number;
  y0: number;
  side: number;
  gone: number;
  mine: boolean;
  shot: number;
}

interface Lob {
  w: ZombieWeapon;
  x: number;
  y0: number;
  side: number;
  reach: number;
  age: number;
  life: number;
}

interface Roll {
  w: ZombieWeapon;
  x: number;
  y0: number;
  side: number;
  gone: number;
}

interface Effect {
  kind: "splat" | "spark" | "boom" | "frost" | "flash" | "pellet" | "flame" | "dust";
  x: number;
  y: number;
  age: number;
  life: number;
  r: number;
  vx: number;
  vy: number;
}

interface Pending {
  q: number;
  z: Zed;
  amount: number;
}

interface Snapshot {
  t: number;
  dur: number;
  level: number;
  house: number;
  house_max: number;
  over: string | null;
  trees: number[][];
  z: number[][];
  hp: number[][];
  dead: number[];
  fr: number[][];
  p: { id: number; x: number; side: number; weapons: string[]; weapon: number; kills: number }[];
  ammo: (number | null)[] | null;
  ack: number;
}

interface Batch {
  t: number;
  h: number;
  d?: number[][];
  fr?: number[][];
  f?: number[][];
  m?: number[][];
  w?: number[][];
  a?: Record<string, number>;
  o?: string;
}

const spot = { x: 0, y: 0, te: 0, leg: 0 };

/**
 * Where a zombie is at `t`, into `spot`. The same sums as
 * `Zombie.position` on the server; keep the two in step.
 */
function locate(z: Zed, t: number, rules: ZombieRules) {
  const te = t < z.until ? z.stop : t - z.lag;
  let gone = (te - z.t0) * z.speed;
  gone = gone < 0 ? 0 : gone > z.dist ? z.dist : gone;
  const cum = z.cum;
  let leg = 1;
  while (leg < cum.length - 1 && cum[leg] < gone) leg++;
  const ax = z.path[leg * 2 - 2];
  const ay = z.path[leg * 2 - 1];
  const bx = z.path[leg * 2];
  const by = z.path[leg * 2 + 1];
  const length = cum[leg] - cum[leg - 1] || 1;
  const f = (gone - cum[leg - 1]) / length;
  let x = ax + (bx - ax) * f;
  let y = ay + (by - ay) * f;
  if (z.kind.flying) {
    const sway =
      rules.bat_weave * Math.sin(rules.bat_weave_rate * (te - z.t0) + z.phase) * (1 - gone / z.dist);
    x -= ((by - ay) / length) * sway;
    y += ((bx - ax) / length) * sway;
  }
  spot.x = x;
  spot.y = y;
  spot.te = te;
  spot.leg = leg;
}

/** How much of a spray reaches a zombie: `cone_share` on the server. */
function coneShare(
  w: ZombieWeapon,
  ox: number,
  oy: number,
  angle: number,
  x: number,
  y: number,
  radius: number
): number {
  const dx = x - ox;
  const dy = y - oy;
  const dist = Math.hypot(dx, dy);
  const edge = Math.max(0, dist - radius);
  if (edge > w.range) return 0;
  if (dist > radius) {
    let turn = (Math.atan2(dy, dx) - angle + Math.PI) % TAU;
    if (turn < 0) turn += TAU;
    if (Math.abs(turn - Math.PI) > w.spread + Math.asin(Math.min(1, radius / dist))) return 0;
  }
  return 1 - ((1 - w.falloff) * edge) / w.range;
}

export class ZombieEngine {
  private readonly canvas: HTMLCanvasElement;
  private readonly ctx: CanvasRenderingContext2D | null;
  /** Sits under `canvas`: the grass and the house, painted once. */
  private readonly backdrop: HTMLCanvasElement;
  private readonly rules: ZombieRules;
  private readonly meId: number;
  private readonly send: (message: LiveMessage) => boolean;
  private readonly onHud: (hud: Hud) => void;
  private readonly weapons = new Map<string, ZombieWeapon>();
  private readonly observer: ResizeObserver | null = null;

  // The picture: map units to canvas pixels, and where the map sits.
  private scale = 1;
  private offX = 0;
  private offY = 0;
  /** The leaves of each tree, ready to stamp over whatever walks under them. */
  private canopies: { picture: HTMLCanvasElement; x: number; y: number; size: number }[] = [];
  private painted = false;

  // The level.
  private started = false;
  private level = 1;
  private duration = 120;
  private house = 0;
  private houseMax = 1000;
  private houseHurtAt = -10;
  private over: "won" | "lost" | null = null;
  private trees: number[][] = [];
  private zeds: Zed[] = [];
  private byId = new Map<number, Zed>();
  private next = 0;
  private active: Zed[] = [];
  private fighters = new Map<number, Fighter>();
  private people = new Map<number, { name: string; style: SurvivorStyle | null; color: ShirtColor | null }>();

  // The clock: the level's time is this phone's time plus `offset`.
  private synced = false;
  private offset = 0;
  private heardT = 0;
  private heardAt = 0;
  private t = 0;

  // What this player is doing.
  private ammo: (number | null)[] = [];
  private readyAt: number[] = [];
  private held = -1;
  private wish = -1;
  private wishUntil = 0;
  private keyDir = 0;
  private pointer: number | null = null;
  private shotSeq = 0;
  private msgSeq = 0;
  private shots = 0;
  private pending: Pending[] = [];
  private sentX = NaN;
  private sentSide = 0;
  private sentAt = 0;

  private bolts: Bolt[] = [];
  private lobs: Lob[] = [];
  private rolls: Roll[] = [];
  private named: Fighter[] = [];
  private effects: Effect[] = [];

  private raf = 0;
  private lastFrame = 0;
  private hudAt = 0;
  private hudKey = "";

  constructor(
    canvas: HTMLCanvasElement,
    backdrop: HTMLCanvasElement,
    rules: ZombieRules,
    me: number,
    send: (message: LiveMessage) => boolean,
    onHud: (hud: Hud) => void
  ) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.backdrop = backdrop;
    this.rules = rules;
    this.meId = me;
    this.send = send;
    this.onHud = onHud;
    this.house = this.houseMax = rules.house.hp;
    for (const weapon of rules.weapons) this.weapons.set(weapon.key, weapon);

    canvas.addEventListener("pointerdown", this.onPointerDown);
    canvas.addEventListener("pointermove", this.onPointerMove);
    canvas.addEventListener("pointerup", this.onPointerUp);
    canvas.addEventListener("pointercancel", this.onPointerUp);
    window.addEventListener("keydown", this.onKeyDown);
    window.addEventListener("keyup", this.onKeyUp);
    window.addEventListener("blur", this.onBlur);
    if (typeof ResizeObserver !== "undefined") {
      this.observer = new ResizeObserver(() => this.resize());
      this.observer.observe(canvas);
    }
    this.resize();
    this.lastFrame = performance.now() / 1000;
    this.raf = requestAnimationFrame(this.frame);
  }

  destroy() {
    cancelAnimationFrame(this.raf);
    this.observer?.disconnect();
    const canvas = this.canvas;
    canvas.removeEventListener("pointerdown", this.onPointerDown);
    canvas.removeEventListener("pointermove", this.onPointerMove);
    canvas.removeEventListener("pointerup", this.onPointerUp);
    canvas.removeEventListener("pointercancel", this.onPointerUp);
    window.removeEventListener("keydown", this.onKeyDown);
    window.removeEventListener("keyup", this.onKeyUp);
    window.removeEventListener("blur", this.onBlur);
  }

  /** Who is who: names, characters and shirt colours come with the game, not the level. */
  setPlayers(players: GamePlayer[]) {
    this.people.clear();
    for (const p of players) {
      const look = {
        name: p.name,
        style: isSurvivorStyle(p.avatar_style) ? p.avatar_style : null,
        color: p.avatar_color,
      };
      this.people.set(p.player_id, look);
      const fighter = this.fighters.get(p.player_id);
      if (fighter) Object.assign(fighter, look);
    }
  }

  // ---- what the server says ---------------------------------------------

  /** A message about the level; `at` is when it arrived (performance.now()). */
  receive(message: LiveMessage, at: number) {
    if (message.type === "zs") this.load(message as unknown as Snapshot, at);
    else if (message.type === "zt" && this.started) this.apply(message as unknown as Batch, at);
  }

  private load(snap: Snapshot, at: number) {
    const rules = this.rules;
    this.started = true;
    this.level = snap.level;
    this.duration = snap.dur;
    this.house = snap.house;
    this.houseMax = snap.house_max;
    this.over = snap.over === "won" || snap.over === "lost" ? snap.over : null;
    this.synced = false;
    this.sync(snap.t, at);
    this.t = snap.t;

    const kinds = rules.zombies;
    this.zeds = [];
    this.byId.clear();
    for (const row of snap.z) {
      const path = row.slice(5);
      const cum = [0];
      for (let i = 2; i < path.length; i += 2) {
        cum.push(cum[cum.length - 1] + Math.hypot(path[i] - path[i - 2], path[i + 1] - path[i - 1]));
      }
      const dist = cum[cum.length - 1] || 1;
      const z: Zed = {
        id: row[0],
        kind: kinds[row[1]] ?? kinds[0],
        t0: row[2],
        speed: row[3],
        maxHp: row[4],
        hp: row[4],
        path,
        cum,
        dist,
        arrive: row[2] + dist / row[3],
        phase: (row[0] * 2.399) % TAU,
        alive: true,
        stop: 0,
        until: -1e9,
        lag: 0,
        pend: 0,
        gone: false,
        x: path[0],
        y: path[1],
        angle: path[1] < rules.house.y ? Math.PI / 2 : -Math.PI / 2,
        te: 0,
        atHouse: false,
        hurtAt: -10,
      };
      this.zeds.push(z);
      this.byId.set(z.id, z);
    }
    for (const [id, hp] of snap.hp) {
      const z = this.byId.get(id);
      if (z) z.hp = hp;
    }
    for (const id of snap.dead) {
      const z = this.byId.get(id);
      if (z) z.alive = false;
    }
    for (const [id, stop, until] of snap.fr) this.freeze(id, stop, until);
    this.next = 0;
    this.active = [];

    const before = this.fighters;
    this.fighters = new Map();
    for (const p of snap.p) {
      const look = this.people.get(p.id);
      const old = before.get(p.id);
      this.fighters.set(p.id, {
        id: p.id,
        name: look?.name ?? "",
        style: look?.style ?? null,
        color: look?.color ?? null,
        x: p.x,
        side: p.side,
        tx: p.x,
        tside: p.side,
        cross: 0,
        from: p.side,
        weapons: p.weapons,
        weapon: p.weapon,
        kills: p.kills,
        firedAt: old?.firedAt ?? -10,
      });
    }
    const me = this.fighters.get(this.meId);
    this.ammo = snap.ammo ? [...snap.ammo] : [];
    this.readyAt = me ? me.weapons.map(() => 0) : [];
    this.msgSeq = Math.max(this.msgSeq, snap.ack);
    this.pending = [];
    this.held = -1;
    this.wish = -1;
    this.sentX = me ? me.x : NaN;
    this.sentSide = me ? me.side : 0;
    this.bolts = [];
    this.lobs = [];
    this.rolls = [];
    this.effects = [];

    if (JSON.stringify(snap.trees) !== JSON.stringify(this.trees) || !this.painted) {
      this.trees = snap.trees;
      this.paintGround();
    }
    this.emitHud();
  }

  private apply(batch: Batch, at: number) {
    const now = at / 1000;
    this.sync(batch.t, at);
    if (batch.h < this.house) this.houseHurtAt = now;
    this.house = batch.h;

    if (batch.d) {
      for (const [id, hp, by] of batch.d) {
        const z = this.byId.get(id);
        if (!z || !z.alive) continue;
        z.hp = hp;
        if (hp <= 0) {
          z.alive = false;
          if (!z.gone) this.splat(z);
          const fighter = this.fighters.get(by);
          if (fighter) fighter.kills += 1;
        } else if (by !== this.meId) {
          z.hurtAt = now;
        }
      }
    }
    if (batch.fr) for (const [id, stop, until] of batch.fr) this.freeze(id, stop, until);
    if (batch.m) {
      for (const [id, x, side] of batch.m) {
        const fighter = this.fighters.get(id);
        if (fighter && id !== this.meId) {
          fighter.tx = x;
          fighter.tside = side;
        }
      }
    }
    if (batch.w) {
      for (const [id, index] of batch.w) {
        const fighter = this.fighters.get(id);
        if (fighter && id !== this.meId) fighter.weapon = index;
      }
    }
    if (batch.f) {
      for (const [id, index, x, side, reach] of batch.f) {
        const fighter = this.fighters.get(id);
        if (!fighter || id === this.meId) continue;
        const weapon = this.weapons.get(fighter.weapons[index]);
        if (!weapon) continue;
        fighter.weapon = index;
        fighter.firedAt = now;
        this.launch(weapon, x, side, reach, false, 0);
      }
    }
    const ack = batch.a?.[String(this.meId)];
    if (ack !== undefined) {
      this.pending = this.pending.filter((p) => p.q > ack);
    }
    if (batch.d || ack !== undefined) this.settle();
    if (batch.o === "won" || batch.o === "lost") {
      this.over = batch.o;
      this.t = batch.t;
      this.held = -1;
      this.wish = -1;
    }
    if (batch.d || batch.o) this.emitHud();
  }

  private freeze(id: number, stop: number, until: number) {
    const z = this.byId.get(id);
    if (!z) return;
    z.stop = stop;
    z.until = until;
    z.lag = until - stop;
  }

  /** The level's clock, from the times the server stamps its messages with. */
  private sync(t: number, at: number) {
    // A message is never early, only late: the largest difference seen
    // is the truest. It is let slip slowly so one odd message, or a
    // pause on the server, doesn't leave this clock ahead for good.
    const sample = t - at / 1000;
    if (!this.synced || sample > this.offset) this.offset = sample;
    else this.offset += (sample - this.offset) * 0.03;
    this.synced = true;
    this.heardT = t;
    this.heardAt = at / 1000;
  }

  /** Work out again which zombies this phone believes it has killed. */
  private settle() {
    for (const z of this.active) z.pend = 0;
    for (const p of this.pending) p.z.pend += p.amount;
    for (const z of this.active) {
      const gone = z.alive && z.hp - z.pend <= 0.5;
      if (gone && !z.gone) this.splat(z);
      z.gone = gone;
    }
  }

  // ---- what the player does ---------------------------------------------

  /** A weapon's button went down: take it in hand and fire it. */
  press(index: number) {
    const me = this.fighters.get(this.meId);
    if (!me || !this.started || this.over || index < 0 || index >= me.weapons.length) return;
    const left = this.ammo[index];
    if (left !== null && left !== undefined && left <= 0) return;
    this.held = index;
    if (index !== me.weapon) {
      // A weapon just taken in hand is always ready.
      me.weapon = index;
      this.readyAt[index] = 0;
      this.send({ type: "zw", w: index });
    }
    this.wish = index;
    this.wishUntil = performance.now() / 1000 + TAP_MEMORY;
    this.tryFire(index, performance.now() / 1000);
    this.emitHud();
  }

  release(index: number) {
    if (this.held === index) this.held = -1;
  }

  private tryFire(index: number, now: number): boolean {
    const me = this.fighters.get(this.meId);
    if (!me || !this.started || this.over || this.t < 0 || me.cross > 0) return false;
    if (me.weapon !== index || now < this.readyAt[index] || this.stalled(now)) return false;
    const weapon = this.weapons.get(me.weapons[index]);
    if (!weapon) return false;
    const left = this.ammo[index];
    if (left !== null && left <= 0) return false;

    const x = round1(me.x);
    const reach = weapon.kind === "lob" ? this.throwFor(weapon, me) : 0;
    const shot = ++this.shotSeq;
    const q = this.msgSeq + 1;
    const message: LiveMessage = { type: "zf", w: index, x, d: me.side, s: shot, q };
    if (weapon.kind === "lob") message.r = reach;
    if (!this.send(message)) return false;
    this.msgSeq = q;
    this.sentX = x;
    this.sentSide = me.side;
    this.sentAt = now;
    if (left !== null) this.ammo[index] = left - 1;
    this.readyAt[index] = now + weapon.reload;
    this.shots += 1;
    me.firedAt = now;
    if (this.wish === index) this.wish = -1;

    if (weapon.kind === "cone") {
      // Guess what the spray did; the server's answer replaces the guess.
      const oy = this.trackY(me.side);
      const angle = (me.side * Math.PI) / 2;
      for (const z of this.active) {
        if (!z.alive || z.gone) continue;
        const share = coneShare(weapon, x, oy, angle, z.x, z.y, z.kind.radius);
        if (share > 0) {
          this.pending.push({ q, z, amount: weapon.damage * share });
          z.hurtAt = now;
        }
      }
      this.settle();
    }
    this.launch(weapon, x, me.side, reach, true, shot);
    this.emitHud();
    return true;
  }

  /**
   * How far out a thrown weapon should land: on the first zombie in
   * front of the player, where it will be by the time it gets there.
   */
  private throwFor(weapon: ZombieWeapon, me: Fighter): number {
    const oy = this.trackY(me.side);
    const lane = Math.max(16, weapon.radius * 0.4);
    let nearest = Infinity;
    let target: Zed | null = null;
    for (const z of this.active) {
      if (!z.alive || z.gone) continue;
      const out = (z.y - oy) * me.side;
      if (out <= 0 || out >= nearest || Math.abs(z.x - me.x) > lane + z.kind.radius) continue;
      nearest = out;
      target = z;
    }
    if (!target) return round1(weapon.range * 0.6);
    const flight = clamp(nearest, 30, weapon.range) / weapon.speed;
    locate(target, this.t + flight, this.rules);
    return round1(clamp((spot.y - oy) * me.side, 30, weapon.range));
  }

  /** Put a shot on the map: a bolt, a spray, something thrown or a boulder. */
  private launch(weapon: ZombieWeapon, x: number, side: number, reach: number, mine: boolean, shot: number) {
    const y0 = this.trackY(side);
    if (weapon.kind === "shot") {
      this.bolts.push({ w: weapon, x, y0, side, gone: 8, mine, shot });
      this.effect("flash", x, y0 + side * 14, 0.06, 3);
    } else if (weapon.kind === "cone") {
      const flames = weapon.key === "flamethrower";
      const count = flames ? 3 : 9;
      for (let i = 0; i < count; i++) {
        const angle = (side * Math.PI) / 2 + (Math.random() * 2 - 1) * weapon.spread;
        const speed = weapon.speed * (flames ? 0.7 + Math.random() * 0.5 : 0.9 + Math.random() * 0.2);
        const life = (weapon.range / speed) * (flames ? 0.8 + Math.random() * 0.4 : 1);
        this.effect(
          flames ? "flame" : "pellet",
          x + Math.cos(angle) * 12,
          y0 + Math.sin(angle) * 12,
          life,
          flames ? 3 + Math.random() * 2.5 : 1,
          Math.cos(angle) * speed,
          Math.sin(angle) * speed
        );
      }
      if (!flames) this.effect("flash", x, y0 + side * 14, 0.08, 5);
    } else if (weapon.kind === "lob") {
      const out = clamp(reach || weapon.range * 0.6, 30, weapon.range);
      this.lobs.push({ w: weapon, x, y0, side, reach: out, age: 0, life: out / weapon.speed });
    } else {
      this.rolls.push({ w: weapon, x, y0, side, gone: 0 });
    }
  }

  private stalled(now: number) {
    return this.synced && !this.over && now - this.heardAt > STALL_AFTER;
  }

  private trackY(side: number) {
    return side < 0 ? this.rules.track.top : this.rules.track.bottom;
  }

  private onPointerDown = (event: PointerEvent) => {
    if (this.pointer !== null) return;
    this.pointer = event.pointerId;
    try {
      this.canvas.setPointerCapture(event.pointerId);
    } catch {
      /* the pointer is already gone */
    }
    event.preventDefault();
    this.steer(event);
  };

  private onPointerMove = (event: PointerEvent) => {
    if (event.pointerId === this.pointer) this.steer(event);
  };

  private onPointerUp = (event: PointerEvent) => {
    if (event.pointerId === this.pointer) this.pointer = null;
  };

  /** Head for where the finger is: along the wall, and to the wall on its side of the house. */
  private steer(event: PointerEvent) {
    const me = this.fighters.get(this.meId);
    if (!me) return;
    const box = this.canvas.getBoundingClientRect();
    const x = (event.clientX - box.left - this.offX) / this.scale;
    const y = (event.clientY - box.top - this.offY) / this.scale;
    const { track, house } = this.rules;
    me.tx = clamp(x, track.left, track.right);
    // A finger resting on the roof doesn't flip the player back and forth.
    if (y < house.y - 8) me.tside = TOP;
    else if (y > house.y + 8) me.tside = BOTTOM;
  }

  private onKeyDown = (event: KeyboardEvent) => {
    const target = event.target as HTMLElement | null;
    if (target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return;
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    const me = this.fighters.get(this.meId);
    if (!me) return;
    const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
    if (key === "ArrowLeft" || key === "a") this.keyDir = -1;
    else if (key === "ArrowRight" || key === "d") this.keyDir = 1;
    else if (key === "ArrowUp" || key === "w") me.tside = TOP;
    else if (key === "ArrowDown" || key === "s") me.tside = BOTTOM;
    else if (key === " ") {
      if (!event.repeat) this.press(me.weapon);
    } else if (key >= "1" && key <= "9") {
      if (!event.repeat) this.press(Number(key) - 1);
    } else return;
    event.preventDefault();
  };

  private onKeyUp = (event: KeyboardEvent) => {
    const me = this.fighters.get(this.meId);
    const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
    if ((key === "ArrowLeft" || key === "a") && this.keyDir < 0) this.stopSliding(me);
    else if ((key === "ArrowRight" || key === "d") && this.keyDir > 0) this.stopSliding(me);
    else if (key === " ") this.held = -1;
    else if (key >= "1" && key <= "9") this.release(Number(key) - 1);
  };

  private onBlur = () => {
    this.stopSliding(this.fighters.get(this.meId));
    this.held = -1;
    this.pointer = null;
  };

  private stopSliding(me: Fighter | undefined) {
    this.keyDir = 0;
    if (me) me.tx = me.x;
  }

  // ---- every frame --------------------------------------------------------

  private frame = (stamp: number) => {
    this.raf = requestAnimationFrame(this.frame);
    const now = stamp / 1000;
    const dt = clamp(now - this.lastFrame, 0, 0.05);
    this.lastFrame = now;
    if (this.started) this.step(now, dt);
    this.draw(now);
    if (now - this.hudAt > 0.1) this.emitHud();
  };

  private step(now: number, dt: number) {
    const rules = this.rules;
    if (!this.over) this.t = Math.min(now + this.offset, this.heardT + STALL_AFTER);
    const t = this.t;

    // Zombies: who has set off, and where everyone is.
    const zeds = this.zeds;
    while (this.next < zeds.length && zeds[this.next].t0 <= t) {
      const z = zeds[this.next++];
      if (z.alive) this.active.push(z);
    }
    let dead = false;
    const turn = Math.min(1, dt * 7);
    for (const z of this.active) {
      if (!z.alive) {
        dead = true;
        continue;
      }
      locate(z, t, rules);
      z.x = spot.x;
      z.y = spot.y;
      z.te = spot.te;
      z.atHouse = spot.te >= z.arrive;
      let heading: number;
      if (z.atHouse) {
        heading = z.y < rules.house.y ? Math.PI / 2 : -Math.PI / 2;
      } else {
        const leg = spot.leg * 2;
        heading = Math.atan2(z.path[leg + 1] - z.path[leg - 1], z.path[leg] - z.path[leg - 2]);
        if (z.kind.flying) heading += 0.35 * Math.cos(rules.bat_weave_rate * (z.te - z.t0) + z.phase);
      }
      let off = heading - z.angle;
      if (off > Math.PI) off -= TAU;
      else if (off < -Math.PI) off += TAU;
      z.angle += off * turn;
    }
    if (dead) this.active = this.active.filter((z) => z.alive);

    // Players slide to where they are heading, and walk through the
    // house to change walls.
    const me = this.fighters.get(this.meId);
    if (me && this.keyDir) me.tx = clamp(me.x + this.keyDir * 80, rules.track.left, rules.track.right);
    for (const f of this.fighters.values()) {
      if (f.cross > 0) f.cross -= dt;
      else if (f.tside !== f.side) {
        f.from = f.side;
        f.side = f.tside;
        f.cross = rules.cross_seconds;
      }
      const gap = f.tx - f.x;
      const most = rules.move_speed * dt;
      f.x += clamp(gap, -most, most);
    }
    if (me && !this.over) {
      const x = round1(me.x);
      if ((x !== this.sentX || me.side !== this.sentSide) && now - this.sentAt >= MOVE_EVERY) {
        if (this.send({ type: "zm", x, d: me.side })) {
          this.sentX = x;
          this.sentSide = me.side;
          this.sentAt = now;
        }
      }
      // A tap that came a moment early, or a button held down.
      if (this.wish >= 0) {
        if (now > this.wishUntil || this.wish !== me.weapon) this.wish = -1;
        else this.tryFire(this.wish, now);
      }
      if (this.held >= 0 && this.held === me.weapon) {
        const weapon = this.weapons.get(me.weapons[this.held]);
        if (weapon && weapon.reload <= AUTO_RELOAD) this.tryFire(this.held, now);
      }
    }

    this.fly(dt);
  }

  /** Move everything that was fired, and age what it left behind. */
  private fly(dt: number) {
    const width = this.rules.shot_width;
    if (this.bolts.length) {
      const keep: Bolt[] = [];
      for (const b of this.bolts) {
        const from = b.gone;
        b.gone = Math.min(b.w.range, from + b.w.speed * dt);
        let hit: Zed | null = null;
        let nearest = Infinity;
        for (const z of this.active) {
          if (!z.alive || z.gone) continue;
          const r = z.kind.radius;
          if (Math.abs(z.x - b.x) > r + width) continue;
          const out = (z.y - b.y0) * b.side;
          if (out + r < from || out - r > b.gone || out >= nearest) continue;
          nearest = out;
          hit = z;
        }
        if (hit) {
          this.effect("spark", hit.x, hit.y - b.side * hit.kind.radius * 0.5, 0.14, 3);
          if (b.mine) {
            const q = ++this.msgSeq;
            this.send({ type: "zh", s: b.shot, z: hit.id, q });
            this.pending.push({ q, z: hit, amount: b.w.damage });
            hit.hurtAt = this.lastFrame;
            hit.pend += b.w.damage;
            if (hit.hp - hit.pend <= 0.5 && !hit.gone) {
              hit.gone = true;
              this.splat(hit);
            }
          }
        } else if (b.gone < b.w.range) {
          keep.push(b);
        }
      }
      this.bolts = keep;
    }
    if (this.lobs.length) {
      const keep: Lob[] = [];
      for (const lob of this.lobs) {
        lob.age += dt;
        if (lob.age < lob.life) keep.push(lob);
        else {
          this.effect(
            lob.w.freeze ? "frost" : "boom",
            lob.x,
            lob.y0 + lob.side * lob.reach,
            lob.w.freeze ? 0.7 : 0.45,
            lob.w.radius
          );
        }
      }
      this.lobs = keep;
    }
    if (this.rolls.length) {
      for (const roll of this.rolls) {
        roll.gone += roll.w.speed * dt;
        if (Math.random() < dt * 30) {
          this.effect(
            "dust",
            roll.x + (Math.random() * 2 - 1) * roll.w.radius * 0.7,
            roll.y0 + roll.side * (roll.gone - roll.w.radius),
            0.5,
            3 + Math.random() * 3
          );
        }
      }
      this.rolls = this.rolls.filter((roll) => roll.gone < roll.w.range);
    }
    if (this.effects.length) {
      let done = false;
      for (const e of this.effects) {
        e.age += dt;
        e.x += e.vx * dt;
        e.y += e.vy * dt;
        if (e.age >= e.life) done = true;
      }
      if (done) this.effects = this.effects.filter((e) => e.age < e.life);
    }
  }

  private effect(kind: Effect["kind"], x: number, y: number, life: number, r: number, vx = 0, vy = 0) {
    if (this.effects.length < 400) this.effects.push({ kind, x, y, age: 0, life, r, vx, vy });
  }

  private splat(z: Zed) {
    this.effect("splat", z.x, z.y, z.kind.flying ? 0.5 : 1.4, z.kind.radius);
  }

  // ---- the picture ----------------------------------------------------------

  private resize() {
    const canvas = this.canvas;
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    if (!width || !height) return;
    const dpr = Math.min(MAX_DPR, window.devicePixelRatio || 1);
    const { w, h } = this.rules.world;
    const fit = Math.min(width / w, height / h);
    for (const layer of [canvas, this.backdrop]) {
      layer.width = Math.round(width * dpr);
      layer.height = Math.round(height * dpr);
    }
    this.scale = fit;
    this.offX = (width - w * fit) / 2;
    this.offY = (height - h * fit) / 2;
    this.paintGround();
  }

  /**
   * The grass, the house and the trees don't move. The ground is
   * painted once on a canvas of its own underneath, so a frame only
   * has to draw what moves; each tree's leaves are kept as a small
   * picture to stamp over the zombies passing below.
   */
  private paintGround() {
    const canvas = this.canvas;
    if (!canvas.clientWidth) return;
    const { w, h } = this.rules.world;
    const dpr = canvas.width / canvas.clientWidth;
    const px = this.scale * dpr;
    if (!px || !Number.isFinite(px)) return;
    const ctx = this.backdrop.getContext("2d");
    if (ctx) {
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.clearRect(0, 0, this.backdrop.width, this.backdrop.height);
      ctx.setTransform(px, 0, 0, px, Math.round(this.offX * dpr), Math.round(this.offY * dpr));
      ctx.save();
      ctx.beginPath();
      ctx.rect(0, 0, w, h);
      ctx.clip();
      const seed = this.trees.reduce((sum, tree) => sum + tree[0] * 7 + tree[1] * 13, 1);
      drawGround(ctx, this.rules, this.trees, Math.round(seed));
      ctx.restore();
    }
    this.canopies = this.trees.map((tree) => {
      const [x, y, r] = tree;
      const size = r * 2 + 4;
      const picture = document.createElement("canvas");
      picture.width = picture.height = Math.max(1, Math.ceil(size * px));
      const leaves = picture.getContext("2d");
      if (leaves) {
        leaves.scale(picture.width / size, picture.height / size);
        leaves.translate(size / 2 - x, size / 2 - y);
        drawCanopies(leaves, [tree]);
      }
      return { picture, x: x - size / 2, y: y - size / 2, size };
    });
    this.painted = true;
  }

  private draw(now: number) {
    const ctx = this.ctx;
    const canvas = this.canvas;
    if (!ctx || !canvas.clientWidth) return;
    const rules = this.rules;
    const { w, h } = rules.world;
    const dpr = canvas.width / canvas.clientWidth;
    const px = this.scale * dpr;
    const left = Math.round(this.offX * dpr);
    const top = Math.round(this.offY * dpr);
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.save();
    ctx.setTransform(px, 0, 0, px, left, top);
    ctx.beginPath();
    ctx.rect(0, 0, w, h);
    ctx.clip();

    const t = this.t;
    const me = this.fighters.get(this.meId);

    this.drawEffects(ctx, false);
    if (me && this.started && !this.over) this.drawReach(ctx, me);

    // On the ground: walkers, dogs and ogres.
    for (const z of this.active) if (!z.kind.flying && z.alive && !z.gone) this.drawZed(ctx, z, t, now);
    for (const roll of this.rolls) {
      ctx.save();
      ctx.translate(roll.x, roll.y0 + roll.side * roll.gone);
      drawBoulder(ctx, roll.w.radius, (roll.side * roll.gone) / roll.w.radius);
      ctx.restore();
    }
    this.drawHouse(ctx, now);
    // Teammates standing together would have their names in a heap, so
    // one of a huddle is named: you, if you're in it.
    const named = this.named;
    named.length = 0;
    if (me) named.push(me);
    for (const f of this.fighters.values()) {
      if (f === me) continue;
      const clear = named.every((o) => o.side !== f.side || Math.abs(o.x - f.x) > NAME_ROOM);
      if (clear) named.push(f);
      this.drawFighter(ctx, f, now, false, clear);
    }
    if (me) this.drawFighter(ctx, me, now, true, true);
    this.drawBolts(ctx);

    ctx.globalAlpha = 0.86;
    for (const tree of this.canopies) ctx.drawImage(tree.picture, tree.x, tree.y, tree.size, tree.size);
    ctx.globalAlpha = 1;

    // In the air: bats, and whatever has been thrown.
    for (const z of this.active) {
      if (!z.kind.flying || !z.alive || z.gone) continue;
      ctx.fillStyle = "rgba(0,0,0,0.25)";
      ctx.beginPath();
      ctx.ellipse(z.x + 4, z.y + 7, 5, 2.4, 0, 0, TAU);
      ctx.fill();
      this.drawZed(ctx, z, t, now);
    }
    for (const lob of this.lobs) {
      const f = lob.age / lob.life;
      const lift = Math.sin(Math.PI * f);
      const y = lob.y0 + lob.side * lob.reach * f;
      ctx.fillStyle = "rgba(0,0,0,0.25)";
      ctx.beginPath();
      ctx.ellipse(lob.x + 3 * lift + 1, y + 5 * lift + 1, 3, 2, 0, 0, TAU);
      ctx.fill();
      const r = 3 + 2.4 * lift;
      ctx.beginPath();
      ctx.arc(lob.x, y, r, 0, TAU);
      ctx.fillStyle = lob.w.freeze ? "#ffffff" : "#3f5527";
      ctx.fill();
      ctx.lineWidth = 0.8;
      ctx.strokeStyle = lob.w.freeze ? "#a5d8ff" : "#141a12";
      ctx.stroke();
    }
    this.drawEffects(ctx, true);
    this.drawBars(ctx);
    this.drawWords(ctx, now);
    ctx.restore();
  }

  private drawZed(ctx: CanvasRenderingContext2D, z: Zed, t: number, now: number) {
    ctx.save();
    ctx.translate(z.x, z.y);
    ctx.save();
    ctx.rotate(z.angle);
    (ZOMBIE_ART[z.kind.key] ?? ZOMBIE_ART.walker)(ctx, z.te, z.atHouse, z.phase);
    ctx.restore();
    if (t < z.until) drawIce(ctx, z.kind.radius, z.until - t);
    if (now - z.hurtAt < 0.09) {
      ctx.globalAlpha = 0.55;
      ctx.beginPath();
      ctx.arc(0, 0, z.kind.radius, 0, TAU);
      ctx.fillStyle = "#fff";
      ctx.fill();
      ctx.globalAlpha = 1;
    }
    ctx.restore();
  }

  /** How hurt each wounded zombie is. */
  private drawBars(ctx: CanvasRenderingContext2D) {
    for (const z of this.active) {
      if (!z.alive || z.gone) continue;
      const hp = z.hp - z.pend;
      if (hp >= z.maxHp) continue;
      const r = z.kind.radius;
      const width = Math.max(10, r * 2);
      const y = z.y - r - 4.5;
      ctx.fillStyle = "rgba(0,0,0,0.6)";
      ctx.fillRect(z.x - width / 2 - 0.5, y - 0.5, width + 1, 2.8);
      ctx.fillStyle = "#ff6b6b";
      ctx.fillRect(z.x - width / 2, y, width * clamp(hp / z.maxHp, 0, 1), 1.8);
    }
  }

  private fighterY(f: Fighter) {
    const y = this.trackY(f.side);
    if (f.cross <= 0) return y;
    return y + (this.trackY(f.from) - y) * (f.cross / this.rules.cross_seconds);
  }

  private drawFighter(ctx: CanvasRenderingContext2D, f: Fighter, now: number, mine: boolean, named: boolean) {
    const y = this.fighterY(f);
    const facing = f.cross > this.rules.cross_seconds / 2 ? f.from : f.side;
    const shirt = SHIRT_COLORS[f.color ?? "blue"] ?? SHIRT_COLORS.blue;
    ctx.fillStyle = "rgba(0,0,0,0.28)";
    ctx.beginPath();
    ctx.ellipse(f.x + 1.5, y + 2, 9, 8, 0, 0, TAU);
    ctx.fill();
    if (mine) {
      // A ring marks which one is you; it empties and refills as the
      // weapon in hand reloads.
      ctx.lineWidth = 1.6;
      ctx.strokeStyle = "rgba(255,255,255,0.28)";
      ctx.beginPath();
      ctx.arc(f.x, y, 12.5, 0, TAU);
      ctx.stroke();
      const weapon = this.weapons.get(f.weapons[f.weapon]);
      const wait = (this.readyAt[f.weapon] ?? 0) - now;
      const ready = weapon && weapon.reload >= 0.3 && wait > 0 ? 1 - wait / weapon.reload : 1;
      ctx.strokeStyle = ready >= 1 ? shirt.light : "#ffd43b";
      ctx.beginPath();
      ctx.arc(f.x, y, 12.5, -Math.PI / 2, -Math.PI / 2 + TAU * clamp(ready, 0, 1));
      ctx.stroke();
    }
    ctx.save();
    ctx.translate(f.x, y);
    ctx.rotate((facing * Math.PI) / 2);
    const recoil = clamp(1 - (now - f.firedAt) / 0.12, 0, 1) * 2;
    drawSurvivor(ctx, f.style, f.color, f.weapons[f.weapon] ?? this.rules.default_weapon, recoil);
    ctx.restore();
    if (!named) return;

    const name = mine ? "You" : f.name.length > 9 ? `${f.name.slice(0, 8)}…` : f.name;
    ctx.font = "700 6.5px system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    const ny = y - facing * 15.5;
    ctx.lineWidth = 2;
    ctx.strokeStyle = "rgba(0,0,0,0.75)";
    ctx.strokeText(name, f.x, ny);
    ctx.fillStyle = mine ? "#ffe066" : "#fff";
    ctx.fillText(name, f.x, ny);
  }

  /** Where the weapon in hand reaches, straight out from the player. */
  private drawReach(ctx: CanvasRenderingContext2D, me: Fighter) {
    if (me.cross > 0) return;
    const weapon = this.weapons.get(me.weapons[me.weapon]);
    if (!weapon) return;
    const y0 = this.trackY(me.side);
    const side = me.side;
    ctx.save();
    ctx.fillStyle = "rgba(255,255,255,0.07)";
    ctx.strokeStyle = "rgba(255,255,255,0.3)";
    ctx.lineWidth = 0.8;
    if (weapon.kind === "cone") {
      const half = Math.tan(weapon.spread) * weapon.range;
      ctx.beginPath();
      ctx.moveTo(me.x, y0);
      ctx.lineTo(me.x - half, y0 + side * weapon.range);
      ctx.lineTo(me.x + half, y0 + side * weapon.range);
      ctx.closePath();
      ctx.fill();
    } else if (weapon.kind === "roll") {
      ctx.fillRect(me.x - weapon.radius, side < 0 ? y0 - weapon.range : y0, weapon.radius * 2, weapon.range);
    } else {
      const reach = weapon.kind === "lob" ? this.throwFor(weapon, me) : weapon.range;
      ctx.setLineDash([3, 4]);
      ctx.beginPath();
      ctx.moveTo(me.x, y0 + side * 16);
      ctx.lineTo(me.x, y0 + side * reach);
      ctx.stroke();
      ctx.setLineDash([]);
      if (weapon.kind === "lob") {
        ctx.beginPath();
        ctx.arc(me.x, y0 + side * reach, weapon.radius, 0, TAU);
        ctx.fill();
        ctx.stroke();
      } else {
        ctx.beginPath();
        ctx.moveTo(me.x - 4, y0 + side * reach);
        ctx.lineTo(me.x + 4, y0 + side * reach);
        ctx.stroke();
      }
    }
    ctx.restore();
  }

  private drawBolts(ctx: CanvasRenderingContext2D) {
    ctx.lineCap = "round";
    for (const b of this.bolts) {
      const y = b.y0 + b.side * b.gone;
      const key = b.w.key;
      const tail = key === "sniper" ? 46 : key === "machine_gun" ? 8 : 10;
      const back = y - b.side * Math.min(tail, b.gone);
      ctx.beginPath();
      ctx.moveTo(b.x, back);
      ctx.lineTo(b.x, y);
      if (key === "crossbow") {
        ctx.lineWidth = 1.5;
        ctx.strokeStyle = "#d9b98a";
        ctx.stroke();
        ctx.fillStyle = "#f1f3f5";
        ctx.beginPath();
        ctx.arc(b.x, y, 1.3, 0, TAU);
        ctx.fill();
      } else if (key === "sniper") {
        ctx.lineWidth = 1.4;
        ctx.strokeStyle = "rgba(180,240,255,0.85)";
        ctx.stroke();
      } else {
        ctx.lineWidth = 1.6;
        ctx.strokeStyle = "#ffe066";
        ctx.stroke();
      }
    }
  }

  private drawEffects(ctx: CanvasRenderingContext2D, above: boolean) {
    for (const e of this.effects) {
      const ground = e.kind === "splat" || e.kind === "dust";
      if (ground === above) continue;
      const f = e.age / e.life;
      switch (e.kind) {
        case "splat": {
          ctx.globalAlpha = 0.7 * (1 - f);
          ctx.fillStyle = "#5c8a2f";
          const grow = e.r * (0.7 + 0.6 * Math.min(1, f * 6));
          ctx.beginPath();
          ctx.ellipse(e.x, e.y, grow, grow * 0.8, 0.6, 0, TAU);
          ctx.fill();
          ctx.fillStyle = "#7a1f1f";
          ctx.beginPath();
          ctx.arc(e.x + e.r * 0.4, e.y - e.r * 0.3, e.r * 0.35, 0, TAU);
          ctx.arc(e.x - e.r * 0.5, e.y + e.r * 0.4, e.r * 0.25, 0, TAU);
          ctx.fill();
          break;
        }
        case "dust":
          ctx.globalAlpha = 0.4 * (1 - f);
          ctx.fillStyle = "#b9a98a";
          ctx.beginPath();
          ctx.arc(e.x, e.y, e.r * (0.6 + f), 0, TAU);
          ctx.fill();
          break;
        case "spark":
          ctx.globalAlpha = 1 - f;
          ctx.fillStyle = "#fff3bf";
          ctx.beginPath();
          ctx.arc(e.x, e.y, e.r * (0.5 + f), 0, TAU);
          ctx.fill();
          break;
        case "flash":
          ctx.globalAlpha = 1 - f;
          ctx.fillStyle = "#ffe066";
          ctx.beginPath();
          ctx.arc(e.x, e.y, e.r, 0, TAU);
          ctx.fill();
          break;
        case "pellet":
          ctx.globalAlpha = 1 - f * 0.6;
          ctx.strokeStyle = "#ffe8a3";
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.moveTo(e.x, e.y);
          ctx.lineTo(e.x - e.vx * 0.012, e.y - e.vy * 0.012);
          ctx.stroke();
          break;
        case "flame": {
          ctx.globalAlpha = 0.75 * (1 - f);
          ctx.fillStyle = f < 0.35 ? "#fff3bf" : f < 0.7 ? "#ffa94d" : "#e8590c";
          ctx.beginPath();
          ctx.arc(e.x, e.y, e.r * (0.7 + 1.6 * f), 0, TAU);
          ctx.fill();
          break;
        }
        case "boom": {
          const grow = e.r * Math.min(1, 0.25 + f * 2.2);
          ctx.globalAlpha = 0.75 * (1 - f);
          ctx.fillStyle = f < 0.3 ? "#fff3bf" : "#ff922b";
          ctx.beginPath();
          ctx.arc(e.x, e.y, grow * 0.8, 0, TAU);
          ctx.fill();
          ctx.lineWidth = 3 * (1 - f) + 0.5;
          ctx.strokeStyle = "#ffd43b";
          ctx.beginPath();
          ctx.arc(e.x, e.y, grow, 0, TAU);
          ctx.stroke();
          break;
        }
        case "frost": {
          const grow = e.r * Math.min(1, 0.3 + f * 2.5);
          ctx.globalAlpha = 0.5 * (1 - f);
          ctx.fillStyle = "#d0ebff";
          ctx.beginPath();
          ctx.arc(e.x, e.y, grow, 0, TAU);
          ctx.fill();
          ctx.globalAlpha = 1 - f;
          ctx.lineWidth = 1.2;
          ctx.strokeStyle = "#ffffff";
          ctx.beginPath();
          for (let i = 0; i < 6; i++) {
            const a = (i / 6) * TAU;
            ctx.moveTo(e.x + Math.cos(a) * grow * 0.3, e.y + Math.sin(a) * grow * 0.3);
            ctx.lineTo(e.x + Math.cos(a) * grow, e.y + Math.sin(a) * grow);
          }
          ctx.stroke();
          break;
        }
      }
    }
    ctx.globalAlpha = 1;
  }

  /** The house's strength, on its roof. */
  private drawHouse(ctx: CanvasRenderingContext2D, now: number) {
    const house = this.rules.house;
    const bitten = clamp(1 - (now - this.houseHurtAt) / 0.5, 0, 1);
    if (bitten > 0 || this.over === "lost") {
      ctx.fillStyle = `rgba(224,49,49,${this.over === "lost" ? 0.4 : 0.22 * bitten})`;
      ctx.fillRect(house.x - house.w / 2, house.y - house.h / 2, house.w, house.h);
    }
    const share = clamp(this.house / this.houseMax, 0, 1);
    const color = share > 0.5 ? "#69db7c" : share > 0.25 ? "#ffd43b" : "#ff6b6b";
    const width = 132;
    const x = house.x - width / 2;
    ctx.fillStyle = "rgba(0,0,0,0.55)";
    ctx.fillRect(x - 1.5, house.y + 3.5, width + 3, 7);
    ctx.fillStyle = color;
    ctx.fillRect(x, house.y + 5, width * share, 4);
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.font = "800 16px system-ui, sans-serif";
    ctx.lineWidth = 3;
    ctx.strokeStyle = "rgba(0,0,0,0.7)";
    const label = String(Math.max(0, Math.round(this.house)));
    ctx.strokeText(label, house.x, house.y - 6);
    ctx.fillStyle = bitten > 0.5 ? "#ffc9c9" : "#fff";
    ctx.fillText(label, house.x, house.y - 6);
  }

  private drawWords(ctx: CanvasRenderingContext2D, now: number) {
    const { world, house } = this.rules;
    let text = "";
    let size = 60;
    if (!this.started) {
      text = "Getting ready…";
      size = 18;
    } else if (this.over) {
      return;
    } else if (this.t < 0) {
      text = String(Math.max(1, Math.ceil(-this.t)));
      size = 70 + 30 * (1 - (-this.t % 1));
    } else if (this.t < 0.9) {
      text = "GO!";
      size = 64;
      ctx.globalAlpha = clamp(1 - (this.t - 0.4) / 0.5, 0, 1);
    } else if (this.stalled(now)) {
      text = "Reconnecting…";
      size = 18;
    }
    if (!text) return;
    ctx.font = `900 ${size}px system-ui, sans-serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.lineWidth = size / 9;
    ctx.strokeStyle = "rgba(0,0,0,0.7)";
    const y = house.y - world.h * 0.24;
    ctx.strokeText(text, world.w / 2, y);
    ctx.fillStyle = "#fff";
    ctx.fillText(text, world.w / 2, y);
    ctx.globalAlpha = 1;
  }

  // ---- what the screen around the map is told -------------------------------

  private emitHud() {
    const now = performance.now() / 1000;
    this.hudAt = now;
    const me = this.fighters.get(this.meId);
    const kills: Record<number, number> = {};
    for (const f of this.fighters.values()) kills[f.id] = f.kills;
    const phase: Phase = !this.started ? "waiting" : this.over ?? (this.t < 0 ? "countdown" : "playing");
    const hud: Hud = {
      phase,
      left: Math.max(0, Math.ceil(this.duration - Math.max(0, this.t))),
      house: Math.max(0, Math.round(this.house)),
      houseMax: this.houseMax,
      level: this.level,
      fighting: !!me,
      weapons: me ? me.weapons : [],
      weapon: me ? me.weapon : 0,
      ammo: this.ammo,
      shot: this.shots,
      kills,
      stalled: this.stalled(now),
    };
    const key = JSON.stringify(hud);
    if (key === this.hudKey) return;
    this.hudKey = key;
    this.onHud({ ...hud, ammo: [...hud.ammo] });
  }
}

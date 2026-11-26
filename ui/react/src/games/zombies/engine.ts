import { SHIRT_COLORS } from "../Turkey";
import type { GamePlayer, ShirtColor, SurvivorStyle, ZombieKind, ZombieRules, ZombieWeapon } from "../types";
import type { LiveMessage } from "../useGameSocket";
import {
  TILT,
  WALL_H,
  WALL_T,
  ZOMBIE_LOOK,
  drawBoulder,
  drawGround,
  drawGuardDog,
  drawIce,
  drawSurvivor,
  drawTree,
  drawWall,
} from "./draw";
import type { DogPose, ZombieLook } from "./draw";
import { isSurvivorStyle } from "./survivors";

// A level of Zombie Apocalypse on the phone: the map, drawn on a canvas
// sixty times a second, and everything the player does in it.
//
// The map is seen at a slant (draw.ts): the level itself is flat, in
// the server's units, and only the drawing squashes the ground and
// stands everyone up on it. What is nearer the bottom of the screen is
// drawn over what is behind it.
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
/** Pictures of a zombie over one stride. */
const FRAMES = 8;
/** How high off the ground bolts and bullets fly, on the screen. */
const SHOT_HEIGHT = 10;
const DOG_HOME = 0;
const DOG_OUT = 1;
// What is drawn in order of depth.
const ZED = 0;
const TREE = 1;
const WALL = 2;
const FIGHTER = 3;
const HOUND = 4;
const ROLL = 5;

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
  /** The wall this player is defending, or on the way to: -1 top, 1 bottom. */
  side: number;
  /** Nothing has been heard from the server for a moment. */
  stalled: boolean;
}

export interface EngineOptions {
  canvas: HTMLCanvasElement;
  /** Sits under `canvas`: the grass and the floor of the house, painted once. */
  backdrop: HTMLCanvasElement;
  rules: ZombieRules;
  /** This player's id. */
  me: number;
  send: (message: LiveMessage) => boolean;
  onHud: (hud: Hud) => void;
  /** How far along the wall this player is, 0 (left) to 1, whenever it changes. */
  onWhere?: (fraction: number) => void;
}

interface Zed {
  id: number;
  kind: ZombieKind;
  look: ZombieLook;
  /** Which of its kind's sets of clothes it wears. */
  art: number;
  /** Walking up the screen, so seen from behind. */
  back: boolean;
  /** Its kind, clothes and direction as one number, for looking up its pictures. */
  code: number;
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
  /** How far off the ground it is, on the screen. */
  up: number;
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

/** A player's guard dog. */
interface Hound {
  owner: number;
  key: string;
  speed: number;
  hx: number;
  hy: number;
  x: number;
  y: number;
  /** Where the server last said it was. */
  tx: number;
  ty: number;
  /** 0 at home, 1 out, 2 on its way back. */
  state: number;
  /** Per cent. */
  health: number;
  /** When its rest is over, on the level's clock. */
  readyAt: number;
  /** 1 facing right, -1 left. */
  face: number;
  moving: boolean;
  cycle: number;
}

/** Something drawn in its turn, from the back of the map to the front. */
interface Item {
  depth: number;
  what: number;
  ref: unknown;
}

/** A tree or a wall, drawn once and stamped from then on. */
interface Picture {
  picture: HTMLCanvasElement;
  x: number;
  y: number;
  w: number;
  h: number;
  depth: number;
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
  dogs?: { dog: string; home: number[]; at: number[] }[];
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
  /** Guard dogs: player, x, y, state, health %, when its rest ends. */
  g?: number[][];
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
  private readonly backdrop: HTMLCanvasElement;
  private readonly rules: ZombieRules;
  private readonly meId: number;
  private readonly send: (message: LiveMessage) => boolean;
  private readonly onHud: (hud: Hud) => void;
  private readonly onWhere: ((fraction: number) => void) | undefined;
  private readonly weapons = new Map<string, ZombieWeapon>();
  private readonly observer: ResizeObserver | null = null;

  // The picture: map units to canvas pixels, and where the map sits.
  private scale = 1;
  private offX = 0;
  private offY = 0;
  /** Canvas pixels to a map unit. */
  private px = 1;
  private treePictures: Picture[] = [];
  private wallPictures: Picture[] = [];
  private sprites = new Map<number, HTMLCanvasElement>();
  private items: Item[] = [];
  private itemCount = 0;
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
  private dogs = new Map<number, Hound>();
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
  private toldWhere = -1;
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

  constructor(options: EngineOptions) {
    const { canvas, rules } = options;
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.backdrop = options.backdrop;
    this.rules = rules;
    this.meId = options.me;
    this.send = options.send;
    this.onHud = options.onHud;
    this.onWhere = options.onWhere;
    this.house = this.houseMax = rules.house.hp;
    for (const weapon of rules.weapons) this.weapons.set(weapon.key, weapon);

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
    window.removeEventListener("keydown", this.onKeyDown);
    window.removeEventListener("keyup", this.onKeyUp);
    window.removeEventListener("blur", this.onBlur);
    this.sprites.clear();
    this.items.length = 0;
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
      const kind = kinds[row[1]] ?? kinds[0];
      const look = ZOMBIE_LOOK[kind.key] ?? ZOMBIE_LOOK.walker;
      const art = row[0] % look.variants;
      // Those from the bottom of the map walk up the screen.
      const back = path[1] > rules.house.y;
      const z: Zed = {
        id: row[0],
        kind,
        look,
        art,
        back,
        code: (kinds.indexOf(kind) * 4 + art) * 2 + (back ? 1 : 0),
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
    this.dogs.clear();
    for (const dog of snap.dogs ?? []) {
      const owner = dog.at[0];
      const [hx, hy] = dog.home;
      this.dogs.set(owner, {
        owner,
        key: dog.dog,
        speed: rules.guard_dogs.find((d) => d.key === dog.dog)?.speed ?? 120,
        hx,
        hy,
        x: dog.at[1],
        y: dog.at[2],
        tx: dog.at[1],
        ty: dog.at[2],
        state: dog.at[3],
        health: dog.at[4],
        readyAt: dog.at[5],
        // At home they face the middle of the house.
        face: hx < rules.house.x ? 1 : -1,
        moving: false,
        cycle: owner * 0.37,
      });
    }
    this.items.length = 0;
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
    this.toldWhere = -1;
    this.tellWhere();
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
        } else if (by !== this.meId || z.pend <= 0) {
          // This player's own shots have flashed already; their dog's bites haven't.
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
    if (batch.g) {
      for (const [id, x, y, state, health, readyAt] of batch.g) {
        const dog = this.dogs.get(id);
        if (!dog) continue;
        dog.tx = x;
        dog.ty = y;
        dog.state = state;
        dog.health = health;
        dog.readyAt = readyAt;
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
      this.effect("flash", x, y0 + side * 14, 0.06, 3, SHOT_HEIGHT);
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
          SHOT_HEIGHT - 1,
          Math.cos(angle) * speed,
          Math.sin(angle) * speed
        );
      }
      if (!flames) this.effect("flash", x, y0 + side * 14, 0.08, 5, SHOT_HEIGHT);
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

  /** The slide control: 0 is the left end of the wall, 1 the right. */
  steer(fraction: number) {
    const me = this.fighters.get(this.meId);
    if (!me || !Number.isFinite(fraction)) return;
    const { left, right } = this.rules.track;
    me.tx = left + clamp(fraction, 0, 1) * (right - left);
  }

  /** Turn round and defend the other wall. */
  flip() {
    const me = this.fighters.get(this.meId);
    if (me && !this.over) this.face(me, -me.tside);
  }

  private face(me: Fighter, side: number) {
    if (me.tside === side) return;
    me.tside = side;
    this.emitHud();
  }

  private tellWhere() {
    const me = this.fighters.get(this.meId);
    if (!me || !this.onWhere) return;
    const { left, right } = this.rules.track;
    const fraction = (me.x - left) / (right - left);
    if (Math.abs(fraction - this.toldWhere) < 0.002) return;
    this.toldWhere = fraction;
    this.onWhere(fraction);
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
    else if (key === "ArrowUp" || key === "w") this.face(me, TOP);
    else if (key === "ArrowDown" || key === "s") this.face(me, BOTTOM);
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
    this.tellWhere();

    // Dogs go to where the server last saw them, a little faster than they run.
    for (const dog of this.dogs.values()) {
      const dx = dog.tx - dog.x;
      const dy = dog.ty - dog.y;
      const gap = Math.hypot(dx, dy);
      const most = dog.speed * 1.3 * dt;
      if (gap > 90 || gap <= most) {
        dog.x = dog.tx;
        dog.y = dog.ty;
      } else {
        dog.x += (dx / gap) * most;
        dog.y += (dy / gap) * most;
      }
      dog.moving = gap > 1.2;
      if (dog.moving && Math.abs(dx) > 1.5) dog.face = dx > 0 ? 1 : -1;
      else if (dog.state === DOG_HOME && !dog.moving) dog.face = dog.hx < rules.house.x ? 1 : -1;
      dog.cycle += dt * (dog.moving ? 2.8 : dog.state === DOG_OUT ? 2.4 : 0.8);
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
          this.effect("spark", hit.x, hit.y, 0.14, 3, hit.look.alt + Math.min(SHOT_HEIGHT, hit.look.tall * 0.6));
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
            lob.w.radius,
            0
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
            3 + Math.random() * 3,
            0
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

  private effect(kind: Effect["kind"], x: number, y: number, life: number, r: number, up: number, vx = 0, vy = 0) {
    if (this.effects.length < 400) this.effects.push({ kind, x, y, up, age: 0, life, r, vx, vy });
  }

  private splat(z: Zed) {
    this.effect("splat", z.x, z.y, z.kind.flying ? 0.5 : 1.4, z.kind.radius, 0);
  }

  // ---- the picture ----------------------------------------------------------

  private resize() {
    const canvas = this.canvas;
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    if (!width || !height) return;
    const dpr = Math.min(MAX_DPR, window.devicePixelRatio || 1);
    const { w, h } = this.rules.world;
    const fit = Math.min(width / w, height / (h * TILT));
    for (const layer of [canvas, this.backdrop]) {
      layer.width = Math.round(width * dpr);
      layer.height = Math.round(height * dpr);
    }
    this.scale = fit;
    this.offX = (width - w * fit) / 2;
    this.offY = (height - h * TILT * fit) / 2;
    this.paintGround();
  }

  /**
   * The grass and the floor of the house don't move: they are painted
   * once on a canvas of their own underneath, so a frame only has to
   * draw what moves. The trees and the two long walls of the house are
   * kept as small pictures, stamped in their turn so that whoever is
   * behind them is hidden and whoever is in front is not.
   */
  private paintGround() {
    const canvas = this.canvas;
    if (!canvas.clientWidth) return;
    const { w, h } = this.rules.world;
    const house = this.rules.house;
    const dpr = canvas.width / canvas.clientWidth;
    const px = this.scale * dpr;
    if (!px || !Number.isFinite(px)) return;
    if (px !== this.px) this.sprites.clear();
    this.px = px;
    const ctx = this.backdrop.getContext("2d");
    if (ctx) {
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.clearRect(0, 0, this.backdrop.width, this.backdrop.height);
      ctx.setTransform(px, 0, 0, px, Math.round(this.offX * dpr), Math.round(this.offY * dpr));
      ctx.save();
      ctx.beginPath();
      ctx.rect(0, 0, w, h * TILT);
      ctx.clip();
      const seed = this.trees.reduce((sum, tree) => sum + tree[0] * 7 + tree[1] * 13, 1);
      drawGround(ctx, this.rules, this.trees, Math.round(seed));
      ctx.restore();
    }
    this.treePictures = this.trees.map(([x, y, r]) => {
      const wide = r * 2 + 6;
      const tall = r * 2.3 + 5;
      return this.picture(wide, tall, x - wide / 2, y * TILT - tall + 2, y, (pen) => {
        pen.translate(wide / 2, tall - 2);
        drawTree(pen, r, Math.round(x * 31 + y * 17));
      });
    });
    const wide = house.w + 13;
    const tall = WALL_H + WALL_T + 8;
    this.wallPictures = [house.y - house.h / 2, house.y + house.h / 2].map((y) =>
      this.picture(wide, tall, house.x - wide / 2, y * TILT - tall + 1, y, (pen) => {
        pen.translate(4, tall - 1);
        drawWall(pen, house.w + 5);
      })
    );
    this.painted = true;
  }

  /** A small canvas `wide` by `tall` map units, drawn once by `paint`. */
  private picture(
    wide: number,
    tall: number,
    x: number,
    y: number,
    depth: number,
    paint: (pen: CanvasRenderingContext2D) => void
  ): Picture {
    const px = this.px;
    const picture = document.createElement("canvas");
    picture.width = Math.max(1, Math.ceil(wide * px));
    picture.height = Math.max(1, Math.ceil(tall * px));
    const pen = picture.getContext("2d");
    if (pen) {
      pen.scale(px, px);
      paint(pen);
    }
    return { picture, x, y, w: picture.width / px, h: picture.height / px, depth };
  }

  /**
   * A zombie's picture for one step of its stride. Dozens of them are
   * on the map at once, so each picture is drawn once and stamped from
   * then on.
   */
  private sprite(z: Zed, frame: number, flash: boolean): HTMLCanvasElement {
    const key = ((z.code * 2 + (z.atHouse ? 1 : 0)) * FRAMES + frame) * 2 + (flash ? 1 : 0);
    let sprite = this.sprites.get(key);
    if (sprite) return sprite;
    const look = z.look;
    const px = this.px;
    sprite = document.createElement("canvas");
    sprite.width = Math.max(1, Math.ceil(look.w * px));
    sprite.height = Math.max(1, Math.ceil(look.h * px));
    const pen = sprite.getContext("2d");
    if (pen) {
      pen.scale(px, px);
      pen.translate(look.ax, look.ay);
      look.draw(pen, frame / FRAMES, z.atHouse, z.back, z.art);
      if (flash) {
        // Lit up white for a moment when it is hit.
        pen.save();
        pen.setTransform(1, 0, 0, 1, 0, 0);
        pen.globalCompositeOperation = "source-atop";
        pen.fillStyle = "rgba(255,255,255,0.6)";
        pen.fillRect(0, 0, sprite.width, sprite.height);
        pen.restore();
      }
      if (!look.alt) {
        // Its shadow, slipped in underneath.
        pen.globalCompositeOperation = "destination-over";
        pen.fillStyle = "rgba(0,0,0,0.28)";
        pen.beginPath();
        pen.ellipse(0.5, 0, z.kind.radius * 0.95, z.kind.radius * 0.38, 0, 0, TAU);
        pen.fill();
      }
    }
    this.sprites.set(key, sprite);
    return sprite;
  }

  private put(depth: number, what: number, ref: unknown) {
    const item = this.items[this.itemCount];
    if (item) {
      item.depth = depth;
      item.what = what;
      item.ref = ref;
    } else {
      this.items.push({ depth, what, ref });
    }
    this.itemCount++;
  }

  private draw(now: number) {
    const ctx = this.ctx;
    const canvas = this.canvas;
    if (!ctx || !canvas.clientWidth) return;
    const rules = this.rules;
    const { w, h } = rules.world;
    const dpr = canvas.width / canvas.clientWidth;
    const px = this.px;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.save();
    ctx.setTransform(px, 0, 0, px, Math.round(this.offX * dpr), Math.round(this.offY * dpr));
    ctx.beginPath();
    ctx.rect(0, 0, w, h * TILT);
    ctx.clip();

    const t = this.t;
    const me = this.fighters.get(this.meId);

    // Flat on the ground, under everyone's feet.
    ctx.save();
    ctx.scale(1, TILT);
    this.drawEffects(ctx, 0);
    if (me && this.started && !this.over) this.drawReach(ctx, me);
    ctx.restore();
    this.drawFloor(ctx, now);

    // Everything that stands up, from the back of the map to the front.
    this.itemCount = 0;
    for (const z of this.active) if (!z.kind.flying && z.alive && !z.gone) this.put(z.y, ZED, z);
    for (const tree of this.treePictures) this.put(tree.depth, TREE, tree);
    for (const wall of this.wallPictures) this.put(wall.depth, WALL, wall);
    for (const f of this.fighters.values()) this.put(this.fighterY(f), FIGHTER, f);
    for (const dog of this.dogs.values()) this.put(dog.y, HOUND, dog);
    for (const roll of this.rolls) this.put(roll.y0 + roll.side * roll.gone, ROLL, roll);
    const items = this.items;
    for (let i = 1; i < this.itemCount; i++) {
      const item = items[i];
      let j = i - 1;
      while (j >= 0 && items[j].depth > item.depth) {
        items[j + 1] = items[j];
        j--;
      }
      items[j + 1] = item;
    }
    for (let i = 0; i < this.itemCount; i++) {
      const item = items[i];
      switch (item.what) {
        case ZED:
          this.drawZed(ctx, item.ref as Zed, t, now);
          break;
        case TREE:
        case WALL: {
          const stamp = item.ref as Picture;
          // Leaves let a little of what is behind them show.
          if (item.what === TREE) ctx.globalAlpha = 0.88;
          ctx.drawImage(stamp.picture, stamp.x, stamp.y, stamp.w, stamp.h);
          ctx.globalAlpha = 1;
          break;
        }
        case FIGHTER:
          this.drawFighter(ctx, item.ref as Fighter, now, item.ref === me);
          break;
        case HOUND:
          this.drawHound(ctx, item.ref as Hound, t);
          break;
        default:
          this.drawRoll(ctx, item.ref as Roll);
      }
    }
    this.drawBolts(ctx);
    this.drawEffects(ctx, 1);
    // Blasts lie on the ground but show over the crowd they land in.
    ctx.save();
    ctx.scale(1, TILT);
    this.drawEffects(ctx, 2);
    ctx.restore();

    // In the air: bats, and whatever has been thrown.
    for (const z of this.active) {
      if (!z.kind.flying || !z.alive || z.gone) continue;
      ctx.fillStyle = "rgba(0,0,0,0.25)";
      ctx.beginPath();
      ctx.ellipse(z.x, z.y * TILT, 5, 2, 0, 0, TAU);
      ctx.fill();
      this.drawZed(ctx, z, t, now);
    }
    for (const lob of this.lobs) {
      const f = lob.age / lob.life;
      const ground = (lob.y0 + lob.side * lob.reach * f) * TILT;
      const up = SHOT_HEIGHT * (1 - f) + Math.sin(Math.PI * f) * (16 + lob.reach * 0.14);
      ctx.fillStyle = "rgba(0,0,0,0.25)";
      ctx.beginPath();
      ctx.ellipse(lob.x, ground, 3.2, 1.5, 0, 0, TAU);
      ctx.fill();
      ctx.beginPath();
      ctx.arc(lob.x, ground - up, 3.4, 0, TAU);
      ctx.fillStyle = lob.w.freeze ? "#ffffff" : "#3f5527";
      ctx.fill();
      ctx.lineWidth = 0.8;
      ctx.strokeStyle = lob.w.freeze ? "#a5d8ff" : "#141a12";
      ctx.stroke();
    }
    this.drawBars(ctx);
    this.drawNames(ctx, me);
    this.drawWords(ctx, now);
    ctx.restore();
  }

  private drawZed(ctx: CanvasRenderingContext2D, z: Zed, t: number, now: number) {
    const look = z.look;
    const turn = z.te * (z.atHouse ? look.rate * 1.5 : look.rate) + z.phase;
    const frame = Math.floor((turn - Math.floor(turn)) * FRAMES) % FRAMES;
    const sprite = this.sprite(z, frame, now - z.hurtAt < 0.09);
    const y = z.y * TILT - look.alt;
    ctx.drawImage(sprite, z.x - look.ax, y - look.ay, sprite.width / this.px, sprite.height / this.px);
    if (t < z.until) {
      ctx.save();
      ctx.translate(z.x, look.alt ? y + look.tall : y);
      drawIce(ctx, look.w * 0.6, look.alt ? look.tall * 2 : look.tall, z.until - t);
      ctx.restore();
    }
  }

  /** How hurt each wounded zombie is, over its head. */
  private drawBars(ctx: CanvasRenderingContext2D) {
    for (const z of this.active) {
      if (!z.alive || z.gone) continue;
      const hp = z.hp - z.pend;
      if (hp >= z.maxHp) continue;
      const width = Math.max(10, z.kind.radius * 2);
      const y = z.y * TILT - z.look.alt - z.look.tall - 4;
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

  private shirtOf(player: number) {
    const color = this.fighters.get(player)?.color ?? this.people.get(player)?.color ?? "blue";
    return SHIRT_COLORS[color] ?? SHIRT_COLORS.blue;
  }

  private drawFighter(ctx: CanvasRenderingContext2D, f: Fighter, now: number, mine: boolean) {
    const y = this.fighterY(f) * TILT;
    const facing = f.cross > this.rules.cross_seconds / 2 ? f.from : f.side;
    ctx.fillStyle = "rgba(0,0,0,0.28)";
    ctx.beginPath();
    ctx.ellipse(f.x, y, 8, 3.2, 0, 0, TAU);
    ctx.fill();
    if (mine) {
      // A ring on the floor marks which one is you; it empties and
      // refills as the weapon in hand reloads.
      ctx.lineWidth = 1.5;
      ctx.strokeStyle = "rgba(255,255,255,0.3)";
      ctx.beginPath();
      ctx.ellipse(f.x, y, 11.5, 5, 0, 0, TAU);
      ctx.stroke();
      const weapon = this.weapons.get(f.weapons[f.weapon]);
      const wait = (this.readyAt[f.weapon] ?? 0) - now;
      const ready = weapon && weapon.reload >= 0.3 && wait > 0 ? 1 - wait / weapon.reload : 1;
      ctx.strokeStyle = ready >= 1 ? this.shirtOf(f.id).light : "#ffd43b";
      ctx.beginPath();
      ctx.ellipse(f.x, y, 11.5, 5, 0, -Math.PI / 2, -Math.PI / 2 + TAU * clamp(ready, 0, 1));
      ctx.stroke();
    }
    ctx.save();
    ctx.translate(f.x, y);
    const recoil = clamp(1 - (now - f.firedAt) / 0.12, 0, 1) * 2;
    const weapon = f.weapons[f.weapon] ?? this.rules.default_weapon;
    drawSurvivor(ctx, f.style, f.color, weapon, recoil, facing === TOP, f.x * 0.3);
    ctx.restore();
  }

  /**
   * Teammates standing together would have their names in a heap, so
   * one of a huddle is named: you, if you're in it.
   */
  private drawNames(ctx: CanvasRenderingContext2D, me: Fighter | undefined) {
    const named = this.named;
    named.length = 0;
    if (me) named.push(me);
    for (const f of this.fighters.values()) {
      if (f !== me && named.every((o) => o.side !== f.side || Math.abs(o.x - f.x) > NAME_ROOM)) named.push(f);
    }
    ctx.font = "700 6.5px system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.lineWidth = 2;
    ctx.strokeStyle = "rgba(0,0,0,0.75)";
    for (const f of named) {
      const mine = f === me;
      const name = mine ? "You" : f.name.length > 9 ? `${f.name.slice(0, 8)}…` : f.name;
      const y = this.fighterY(f) * TILT - 35;
      ctx.strokeText(name, f.x, y);
      ctx.fillStyle = mine ? "#ffe066" : "#fff";
      ctx.fillText(name, f.x, y);
    }
  }

  private drawHound(ctx: CanvasRenderingContext2D, dog: Hound, t: number) {
    const house = this.rules.house;
    const resting = dog.state === DOG_HOME && dog.readyAt > 0 && t < dog.readyAt;
    const pose: DogPose =
      dog.moving || dog.state > DOG_OUT ? "run" : dog.state === DOG_OUT ? "bite" : resting ? "lie" : "sit";
    // Over the wall in a bound.
    const wall = Math.min(Math.abs(dog.y - (house.y - house.h / 2)), Math.abs(dog.y - (house.y + house.h / 2)));
    const hop = clamp(1 - wall / 10, 0, 1) * (WALL_H + 4);
    const y = dog.y * TILT;
    ctx.fillStyle = "rgba(0,0,0,0.25)";
    ctx.beginPath();
    ctx.ellipse(dog.x, y, 8, 2.8, 0, 0, TAU);
    ctx.fill();
    ctx.save();
    ctx.translate(dog.x, y - hop);
    ctx.scale(dog.face, 1);
    drawGuardDog(ctx, dog.key, pose, dog.cycle, this.shirtOf(dog.owner).main);
    ctx.restore();
    if (resting && !dog.moving) {
      // Asleep, and for how much longer.
      ctx.font = "800 6px system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.lineWidth = 1.8;
      ctx.strokeStyle = "rgba(0,0,0,0.7)";
      const label = `z${Math.sin(dog.cycle * 3) > 0 ? "Z" : "z"} ${Math.max(1, Math.ceil(dog.readyAt - t))}`;
      ctx.strokeText(label, dog.x, y - 15);
      ctx.fillStyle = "#d0ebff";
      ctx.fillText(label, dog.x, y - 15);
    } else if (dog.state !== DOG_HOME && dog.health < 100) {
      ctx.fillStyle = "rgba(0,0,0,0.6)";
      ctx.fillRect(dog.x - 7.5, y - hop - 22.5, 15, 2.8);
      ctx.fillStyle = dog.health > 35 ? "#69db7c" : "#ffd43b";
      ctx.fillRect(dog.x - 7, y - hop - 22, 14 * clamp(dog.health / 100, 0, 1), 1.8);
    }
  }

  private drawRoll(ctx: CanvasRenderingContext2D, roll: Roll) {
    const r = roll.w.radius;
    const y = (roll.y0 + roll.side * roll.gone) * TILT;
    ctx.fillStyle = "rgba(0,0,0,0.3)";
    ctx.beginPath();
    ctx.ellipse(roll.x, y, r, r * 0.42, 0, 0, TAU);
    ctx.fill();
    ctx.save();
    ctx.translate(roll.x, y - r * 0.85);
    drawBoulder(ctx, r * 0.95, (roll.side * roll.gone) / r);
    ctx.restore();
  }

  /**
   * Where the weapon in hand reaches, straight out from the player.
   * Drawn flat on the ground, in the level's own units.
   */
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
      const y = (b.y0 + b.side * b.gone) * TILT - SHOT_HEIGHT;
      const key = b.w.key;
      const tail = key === "sniper" ? 46 : key === "machine_gun" ? 8 : 10;
      const back = y - b.side * Math.min(tail, b.gone) * TILT;
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

  /**
   * What shots leave behind, in three passes: 0 is flat on the ground
   * under everyone, 1 is in the air, 2 is the blasts over the crowd.
   * Passes 0 and 2 are drawn in the level's own units, with the canvas
   * squashed; pass 1 is drawn upright.
   */
  private drawEffects(ctx: CanvasRenderingContext2D, pass: number) {
    for (const e of this.effects) {
      const f = e.age / e.life;
      const y = e.y * TILT - e.up;
      switch (e.kind) {
        case "splat": {
          if (pass !== 0) break;
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
          if (pass !== 0) break;
          ctx.globalAlpha = 0.4 * (1 - f);
          ctx.fillStyle = "#b9a98a";
          ctx.beginPath();
          ctx.arc(e.x, e.y, e.r * (0.6 + f), 0, TAU);
          ctx.fill();
          break;
        case "spark":
          if (pass !== 1) break;
          ctx.globalAlpha = 1 - f;
          ctx.fillStyle = "#fff3bf";
          ctx.beginPath();
          ctx.arc(e.x, y, e.r * (0.5 + f), 0, TAU);
          ctx.fill();
          break;
        case "flash":
          if (pass !== 1) break;
          ctx.globalAlpha = 1 - f;
          ctx.fillStyle = "#ffe066";
          ctx.beginPath();
          ctx.arc(e.x, y, e.r, 0, TAU);
          ctx.fill();
          break;
        case "pellet":
          if (pass !== 1) break;
          ctx.globalAlpha = 1 - f * 0.6;
          ctx.strokeStyle = "#ffe8a3";
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.moveTo(e.x, y);
          ctx.lineTo(e.x - e.vx * 0.012, y - e.vy * TILT * 0.012);
          ctx.stroke();
          break;
        case "flame": {
          if (pass !== 1) break;
          ctx.globalAlpha = 0.75 * (1 - f);
          ctx.fillStyle = f < 0.35 ? "#fff3bf" : f < 0.7 ? "#ffa94d" : "#e8590c";
          ctx.beginPath();
          ctx.arc(e.x, y - f * 5, e.r * (0.7 + 1.6 * f), 0, TAU);
          ctx.fill();
          break;
        }
        case "boom": {
          const grow = e.r * Math.min(1, 0.25 + f * 2.2);
          if (pass === 1) {
            // The fireball going up from it.
            ctx.globalAlpha = 0.6 * (1 - f);
            ctx.fillStyle = f < 0.3 ? "#fff3bf" : "#ff922b";
            ctx.beginPath();
            ctx.arc(e.x, y - grow * (0.25 + f * 0.5), grow * 0.5, 0, TAU);
            ctx.fill();
            break;
          }
          if (pass !== 2) break;
          ctx.globalAlpha = 0.7 * (1 - f);
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
          if (pass !== 2) break;
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

  /** On the floor of the house: the dogs' beds, and the house's strength on the rug. */
  private drawFloor(ctx: CanvasRenderingContext2D, now: number) {
    const house = this.rules.house;
    const middle = house.y * TILT;
    const bitten = clamp(1 - (now - this.houseHurtAt) / 0.5, 0, 1);
    if (bitten > 0 || this.over === "lost") {
      ctx.fillStyle = `rgba(224,49,49,${this.over === "lost" ? 0.4 : 0.22 * bitten})`;
      ctx.fillRect(house.x - house.w / 2, middle - (house.h / 2) * TILT, house.w, house.h * TILT);
    }
    ctx.lineWidth = 1;
    for (const dog of this.dogs.values()) {
      const shirt = this.shirtOf(dog.owner);
      ctx.beginPath();
      ctx.ellipse(dog.hx, dog.hy * TILT + 0.5, 13, 5.2, 0, 0, TAU);
      ctx.fillStyle = shirt.dark;
      ctx.fill();
      ctx.strokeStyle = shirt.light;
      ctx.stroke();
    }
    const share = clamp(this.house / this.houseMax, 0, 1);
    const color = share > 0.5 ? "#69db7c" : share > 0.25 ? "#ffd43b" : "#ff6b6b";
    const width = 128;
    const x = house.x - width / 2;
    ctx.fillStyle = "rgba(0,0,0,0.55)";
    ctx.fillRect(x - 1.5, middle - 10, width + 3, 6.4);
    ctx.fillStyle = color;
    ctx.fillRect(x, middle - 8.8, width * share, 4);
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.font = "800 14px system-ui, sans-serif";
    ctx.lineWidth = 3;
    ctx.strokeStyle = "rgba(0,0,0,0.7)";
    const label = String(Math.max(0, Math.round(this.house)));
    ctx.strokeText(label, house.x, middle - 17.5);
    ctx.fillStyle = bitten > 0.5 ? "#ffc9c9" : "#fff";
    ctx.fillText(label, house.x, middle - 17.5);
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
    const y = (house.y - world.h * 0.24) * TILT;
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
      side: me ? me.tside : TOP,
      stalled: this.stalled(now),
    };
    const key = JSON.stringify(hud);
    if (key === this.hudKey) return;
    this.hudKey = key;
    this.onHud({ ...hud, ammo: [...hud.ammo] });
  }
}

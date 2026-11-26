import { SHIRT_COLORS } from "../Turkey";
import type { ShirtColor, SurvivorStyle, ZombieRules } from "../types";
import { HELMET, HELMET_DARK, SURVIVOR_BY_KEY } from "./survivors";

// Everything on the map is drawn here, with plain canvas calls.
//
// The map is looked at from the south, from above and at a slant: the
// ground is squashed to TILT of its depth and everything that stands on
// it is drawn upright. Whoever walks down the screen shows their face,
// whoever walks up it shows their back.
//
// Each figure is drawn standing on (0, 0) with up being -y, in the
// squashed units of the screen; the engine moves the canvas there first.

type Ctx = CanvasRenderingContext2D;

/** How much of the ground's depth is left after the slant. */
export const TILT = 0.8;
/** The walls of the house, on the screen: how high, and how thick from above. */
export const WALL_H = 9;
export const WALL_T = 4;

const TAU = Math.PI * 2;
const INK = "#10140f";

function dot(ctx: Ctx, x: number, y: number, r: number, fill: string) {
  ctx.beginPath();
  ctx.arc(x, y, r, 0, TAU);
  ctx.fillStyle = fill;
  ctx.fill();
}

function blob(ctx: Ctx, x: number, y: number, rx: number, ry: number, fill: string, stroke?: string) {
  ctx.beginPath();
  ctx.ellipse(x, y, rx, ry, 0, 0, TAU);
  ctx.fillStyle = fill;
  ctx.fill();
  if (stroke) {
    ctx.strokeStyle = stroke;
    ctx.stroke();
  }
}

function limb(ctx: Ctx, x1: number, y1: number, x2: number, y2: number, width: number, color: string, rim?: string) {
  ctx.lineCap = "round";
  ctx.beginPath();
  ctx.moveTo(x1, y1);
  ctx.lineTo(x2, y2);
  const before = ctx.lineWidth;
  if (rim) {
    ctx.lineWidth = width + before * 2;
    ctx.strokeStyle = rim;
    ctx.stroke();
  }
  ctx.lineWidth = width;
  ctx.strokeStyle = color;
  ctx.stroke();
  ctx.lineWidth = before;
}

/** A box with rounded corners. */
function box(ctx: Ctx, x: number, y: number, w: number, h: number, r: number, fill: string, stroke?: string) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
  ctx.fillStyle = fill;
  ctx.fill();
  if (stroke) {
    ctx.strokeStyle = stroke;
    ctx.stroke();
  }
}

function shape(ctx: Ctx, points: number[], fill: string, stroke?: string) {
  ctx.beginPath();
  ctx.moveTo(points[0], points[1]);
  for (let i = 2; i < points.length; i += 2) ctx.lineTo(points[i], points[i + 1]);
  ctx.closePath();
  ctx.fillStyle = fill;
  ctx.fill();
  if (stroke) {
    ctx.strokeStyle = stroke;
    ctx.stroke();
  }
}

// ---------------------------------------------------------------------------
// Zombies
// ---------------------------------------------------------------------------

/**
 * `step` runs from 0 to 1 over one stride (or one flap), and round
 * again; `attacking` once the zombie is at the house; `back` when it
 * is walking away from the viewer. `variant` picks its clothes.
 */
export type ZombiePainter = (ctx: Ctx, step: number, attacking: boolean, back: boolean, variant: number) => void;

const ROT = "#a9d86e";
const ROT_SHADE = "#86b552";
const WALKER_SHIRTS = ["#6f5b91", "#93574a", "#4d7394"];
const WALKER_HAIR = ["#2f3a22", "#4a3324", "#3a3a3a"];

export const drawWalker: ZombiePainter = (ctx, step, attacking, back, variant) => {
  const a = step * TAU;
  const stride = Math.sin(a);
  const shirt = WALKER_SHIRTS[variant % WALKER_SHIRTS.length];
  ctx.lineWidth = 0.8;
  ctx.lineJoin = "round";
  // A shuffle: each foot in turn is dragged up and put down.
  const left = Math.max(0, stride) * 2;
  const right = Math.max(0, -stride) * 2;
  limb(ctx, -2.2, -9.5, -2.6, -1.4 - left, 2.8, "#3a3d33", INK);
  limb(ctx, 2.2, -9.5, 2.6, -1.4 - right, 2.8, "#3a3d33", INK);
  blob(ctx, -2.9, -0.9 - left, 2.2, 1.2, "#1c1f17");
  blob(ctx, 2.9, -0.9 - right, 2.2, 1.2, "#1c1f17");

  // The body lurches from the hips, and leans in when it is clawing.
  ctx.save();
  ctx.translate(Math.cos(a) * 0.6, -9.5 - Math.abs(stride) * 0.5);
  ctx.rotate(stride * 0.1);
  const pound = attacking ? Math.cos(a * 2) : 0;
  const arms = () => {
    for (const side of [-1, 1]) {
      // Held out in front: down the screen for one coming this way,
      // up it for one walking away.
      const sway = stride * side * 0.9 + pound * side * 2.2;
      const hx = side * (back ? 5.6 : 5.2);
      const hy = back ? -12.6 + sway : 0.4 + sway;
      limb(ctx, side * 4.4, -7, hx, hy, 2.3, ROT_SHADE, INK);
      dot(ctx, hx, hy, 1.5, ROT);
    }
  };
  if (back) arms();
  box(ctx, -4.7, -9, 9.4, 10, 2.4, shirt, INK);
  // Torn clothes.
  shape(ctx, [-4.2, 1, -2.4, -1.4, -0.8, 1], ROT_SHADE);
  shape(ctx, [1.2, 1, 2.6, -0.9, 4.2, 1], ROT_SHADE);
  if (back) {
    limb(ctx, 0, -7.2, 0, -1.6, 0.6, "rgba(0,0,0,0.3)");
    shape(ctx, [-3, -5.8, -0.6, -4.6, -2.6, -2.8], ROT_SHADE);
  } else {
    shape(ctx, [0.6, -6.4, 3.2, -5.4, 1.4, -3.2], ROT_SHADE);
    limb(ctx, -3.2, -2.6, 3.2, -2.6, 1, "rgba(0,0,0,0.28)");
  }
  if (!back) arms();

  // The head hangs to one side.
  ctx.translate(0, -12.6);
  ctx.rotate(Math.cos(a) * 0.12 + 0.08);
  blob(ctx, 0, 0, 4.3, 4.5, ROT, INK);
  const hair = WALKER_HAIR[variant % WALKER_HAIR.length];
  if (back) {
    ctx.beginPath();
    ctx.arc(0, 0, 4.3, Math.PI * 0.95, Math.PI * 2.05);
    ctx.quadraticCurveTo(1.2, 1.6, -4.2, 0.6);
    ctx.fillStyle = hair;
    ctx.fill();
    dot(ctx, 1.2, -1.6, 1.1, ROT_SHADE);
  } else {
    shape(ctx, [-3.8, -2.2, -2.6, -5.2, -1.2, -3.6, 0.4, -5.6, 1.6, -3.6, 3.2, -4.8, 3.8, -2.2], hair);
    dot(ctx, -1.7, -0.5, 1.25, "#1c2415");
    dot(ctx, 1.7, -0.5, 1.25, "#1c2415");
    dot(ctx, -1.7, -0.5, 0.6, "#ff4d4d");
    dot(ctx, 1.7, -0.5, 0.6, "#ff4d4d");
    box(ctx, -1.9, 1.5, 3.8, attacking ? 1.9 : 1.2, 0.5, "#2b1a1a");
    ctx.fillStyle = "#e9e2c8";
    ctx.fillRect(-1.2, 1.5, 0.7, 0.7);
    ctx.fillRect(0.4, 1.5, 0.7, 0.7);
  }
  ctx.restore();
};

const HOUND = "#17181d";
const HOUND_RIM = "#8d96a8";

export const drawDog: ZombiePainter = (ctx, step, attacking, back) => {
  const a = step * TAU;
  const run = Math.sin(a);
  const bound = Math.abs(Math.cos(a));
  ctx.lineWidth = 0.6;
  ctx.lineJoin = "round";
  const leg = (x: number, y: number, lift: number, reach: number) => {
    ctx.lineWidth = 0.35;
    limb(ctx, x, y, x * 1.25, y + reach - lift, 1.7, HOUND, HOUND_RIM);
    ctx.lineWidth = 0.6;
  };
  const ribs = (y: number) => {
    for (const row of [-1.6, 0, 1.6]) {
      limb(ctx, -2.6, y + row, -1, y + row - 0.4, 0.5, "#c9c2a8");
      limb(ctx, 2.6, y + row, 1, y + row - 0.4, 0.5, "#c9c2a8");
    }
  };
  if (!back) {
    // Coming this way: its length runs up the screen behind its head.
    limb(ctx, 0, -12.6, run * 1.8, -15.8, 1.3, HOUND, HOUND_RIM);
    leg(-2.9, -9.2, Math.max(0, run) * 1.6, 3.2);
    leg(2.9, -9.2, Math.max(0, -run) * 1.6, 3.2);
    blob(ctx, 0, -8.4 - bound * 0.7, 3.9, 5.2, HOUND, HOUND_RIM);
    ribs(-9.4 - bound * 0.7);
    leg(-2.1, -3.6, Math.max(0, -run) * 1.8, 3.2);
    leg(2.1, -3.6, Math.max(0, run) * 1.8, 3.2);
    const hy = -5 - bound * 0.9 + (attacking ? Math.cos(a * 2) * 0.8 : 0);
    shape(ctx, [-3.3, hy - 1, -3.2, hy - 4.6, -1, hy - 2.4], HOUND, HOUND_RIM);
    shape(ctx, [3.3, hy - 1, 3.2, hy - 4.6, 1, hy - 2.4], HOUND, HOUND_RIM);
    blob(ctx, 0, hy, 3.3, 3, HOUND, HOUND_RIM);
    blob(ctx, 0, hy + 1.7, 1.9, 1.5, "#26282f", HOUND_RIM);
    dot(ctx, -1.5, hy - 0.7, 0.8, "#ff3b3b");
    dot(ctx, 1.5, hy - 0.7, 0.8, "#ff3b3b");
    ctx.fillStyle = "#f1ead9";
    ctx.fillRect(-1.3, hy + 2.2, 0.6, 0.9);
    ctx.fillRect(0.7, hy + 2.2, 0.6, 0.9);
  } else {
    // Running away up the screen: the head is the far end.
    const hy = -13.4 - bound * 0.7;
    leg(-2.1, -12, Math.max(0, run) * 1.4, 2.6);
    leg(2.1, -12, Math.max(0, -run) * 1.4, 2.6);
    shape(ctx, [-3, hy, -2.8, hy - 3.8, -0.8, hy - 1.8], HOUND, HOUND_RIM);
    shape(ctx, [3, hy, 2.8, hy - 3.8, 0.8, hy - 1.8], HOUND, HOUND_RIM);
    blob(ctx, 0, hy, 2.9, 2.6, HOUND, HOUND_RIM);
    blob(ctx, 0, -8 - bound * 0.7, 3.9, 5.2, HOUND, HOUND_RIM);
    ribs(-8.6 - bound * 0.7);
    leg(-2.8, -4.4, Math.max(0, -run) * 1.8, 4);
    leg(2.8, -4.4, Math.max(0, run) * 1.8, 4);
    limb(ctx, 0, -4.6, run * 2.4, -8, 1.4, HOUND, HOUND_RIM);
  }
};

const OGRE = "#86a143";
const OGRE_SHADE = "#5d7330";
const OGRE_LIGHT = "#a3be59";
const BONE = "#ece5cb";

export const drawOgre: ZombiePainter = (ctx, step, attacking, back) => {
  const a = step * TAU;
  const stomp = Math.sin(a);
  ctx.lineWidth = 1.1;
  ctx.lineJoin = "round";
  const left = Math.max(0, stomp) * 3.2;
  const right = Math.max(0, -stomp) * 3.2;
  limb(ctx, -6.5, -16, -7, -3 - left, 8, OGRE_SHADE, INK);
  limb(ctx, 6.5, -16, 7, -3 - right, 8, OGRE_SHADE, INK);
  blob(ctx, -7.6, -2 - left, 5.4, 2.6, "#3d4a1e", INK);
  blob(ctx, 7.6, -2 - right, 5.4, 2.6, "#3d4a1e", INK);

  ctx.save();
  ctx.translate(stomp * 1.3, -16 - Math.abs(stomp) * 1.3);
  ctx.rotate(stomp * 0.05);
  // Its club is in its right hand: on the left of one coming this way.
  const hand = back ? 1 : -1;
  const swing = attacking ? (1 - Math.cos(a * 2)) / 2 : 0;
  const club = () => {
    ctx.save();
    ctx.translate(hand * 17, -1 + swing * (back ? -3 : 3));
    // Over the shoulder; brought down on the wall in front of it.
    const lean = hand * 0.3;
    ctx.rotate(back ? lean * (1 - swing) : lean + hand * -swing * 2.3);
    const reach = back ? 1 + swing * 0.25 : 1;
    limb(ctx, 0, 2, 0, -15 * reach, 3.2, "#7a5230", INK);
    blob(ctx, 0, -19 * reach, 4.6, 6.4 * reach, "#8a5a33", INK);
    ctx.fillStyle = BONE;
    for (const [sx, sy] of [[-4.4, -21], [4.4, -18], [-4, -16.5], [1, -25.6]]) {
      ctx.beginPath();
      ctx.arc(sx, sy * reach, 1.1, 0, TAU);
      ctx.fill();
    }
    ctx.restore();
  };
  const arm = (side: number, lift: number) => {
    limb(ctx, side * 13.5, -13.5, side * 16.8, -1.5 - lift, 6.4, OGRE, INK);
    blob(ctx, side * 16.9, -0.4 - lift, 4.3, 4, OGRE_LIGHT, INK);
  };
  if (back) club();
  arm(-hand, stomp * hand * 1.6 + (attacking ? swing * 5 : 0));
  blob(ctx, 0, -9, 13.4, 12.6, OGRE, INK);
  if (back) {
    limb(ctx, 0, -17, 0, -1.5, 1, OGRE_SHADE);
    blob(ctx, -5.6, -12, 4, 3.2, OGRE_SHADE);
    blob(ctx, 5.6, -12, 4, 3.2, OGRE_SHADE);
  } else {
    blob(ctx, 0, -6.4, 8.8, 7.8, OGRE_LIGHT);
    dot(ctx, 0, -3.2, 0.9, OGRE_SHADE);
  }
  // A hide round its middle.
  shape(ctx, [-11.2, -3.4, 11.2, -3.4, 9.4, 3.6, 4.6, 1.8, 0.6, 4.4, -4.2, 1.6, -9.4, 3.6], "#6b4a2e", INK);
  blob(ctx, -12.6, -15, 5.6, 5, OGRE, INK);
  blob(ctx, 12.6, -15, 5.6, 5, OGRE, INK);
  arm(hand, swing * (back ? 2 : -2));
  if (!back) club();

  ctx.translate(0, -23.5 + Math.abs(stomp) * 0.5);
  // Horns, ears, then the head.
  for (const side of [-1, 1]) {
    shape(ctx, [side * 4.6, -4.4, side * 9, -9.4, side * 6.8, -2.4], BONE, INK);
    blob(ctx, side * 7.4, 0.2, 1.8, 2.3, OGRE_LIGHT, INK);
  }
  blob(ctx, 0, 0, 7.4, 7, OGRE_LIGHT, INK);
  if (back) {
    shape(ctx, [-2.4, -6.4, 0, -9.6, 2.4, -6.4], "#3d4a1e");
    limb(ctx, -3.6, 3.6, 3.6, 3.6, 0.9, OGRE_SHADE);
  } else {
    limb(ctx, -5, -2.4, -1, -1, 1.7, OGRE_SHADE);
    limb(ctx, 5, -2.4, 1, -1, 1.7, OGRE_SHADE);
    dot(ctx, -2.8, 0.2, 1.3, "#ff3b3b");
    dot(ctx, 2.8, 0.2, 1.3, "#ff3b3b");
    // An underbite, with tusks.
    box(ctx, -4.8, 2.4, 9.6, 4, 1.8, OGRE, INK);
    shape(ctx, [-3.9, 3.2, -3, -0.6, -1.9, 3.2], BONE, INK);
    shape(ctx, [3.9, 3.2, 3, -0.6, 1.9, 3.2], BONE, INK);
  }
  ctx.restore();
};

export const drawBat: ZombiePainter = (ctx, step, _attacking, back) => {
  const flap = Math.cos(step * TAU);
  const tip = -flap * 6.5;
  ctx.lineWidth = 0.7;
  ctx.lineJoin = "round";
  for (const side of [-1, 1]) {
    ctx.beginPath();
    ctx.moveTo(side * 1.4, -1.6);
    ctx.quadraticCurveTo(side * 6.5, -4.6 + tip * 0.7, side * 12, tip);
    ctx.lineTo(side * 9.4, tip * 0.6 + 3.4);
    ctx.lineTo(side * 7.6, tip * 0.45 + 1.8);
    ctx.lineTo(side * 5.2, tip * 0.25 + 4.2);
    ctx.lineTo(side * 3.6, 1.8);
    ctx.lineTo(side * 1.4, 2.8);
    ctx.closePath();
    ctx.fillStyle = "#74509c";
    ctx.fill();
    ctx.strokeStyle = "#1c1228";
    ctx.stroke();
  }
  shape(ctx, [-2.2, -2, -2.6, -5.6, -0.4, -3.2], "#3a2952", "#1c1228");
  shape(ctx, [2.2, -2, 2.6, -5.6, 0.4, -3.2], "#3a2952", "#1c1228");
  blob(ctx, 0, 0.4, 2.8, 3.5, "#3a2952", "#1c1228");
  if (!back) {
    dot(ctx, -1.1, -0.8, 0.7, "#ff5a5a");
    dot(ctx, 1.1, -0.8, 0.7, "#ff5a5a");
    ctx.fillStyle = "#fff";
    ctx.fillRect(-0.9, 0.9, 0.5, 1);
    ctx.fillRect(0.4, 0.9, 0.5, 1);
  }
};

/** How each kind of zombie is drawn, and how big a picture it needs. */
export interface ZombieLook {
  draw: ZombiePainter;
  /** The picture's size, and where in it the zombie's feet are. */
  w: number;
  h: number;
  ax: number;
  ay: number;
  /** Strides (or flaps) a second. */
  rate: number;
  /** How far above its feet its head is. */
  tall: number;
  /** How high it flies. */
  alt: number;
  /** Different sets of clothes. */
  variants: number;
}

/** The same picture, drawn larger. */
function bigger(paint: ZombiePainter, by: number): ZombiePainter {
  return (ctx, step, attacking, back, variant) => {
    ctx.save();
    ctx.scale(by, by);
    paint(ctx, step, attacking, back, variant);
    ctx.restore();
  };
}

export const ZOMBIE_LOOK: Record<string, ZombieLook> = {
  walker: { draw: drawWalker, w: 22, h: 32, ax: 11, ay: 29.5, rate: 1.1, tall: 27, alt: 0, variants: 3 },
  // Drawn larger than the others for its size, or it is lost in the grass.
  dog: { draw: bigger(drawDog, 1.3), w: 18.5, h: 27.5, ax: 9.25, ay: 24.75, rate: 2.6, tall: 22, alt: 0, variants: 1 },
  ogre: { draw: drawOgre, w: 62, h: 62, ax: 31, ay: 57, rate: 0.6, tall: 50, alt: 0, variants: 1 },
  bat: { draw: drawBat, w: 28, h: 20, ax: 14, ay: 11, rate: 3.4, tall: 7, alt: 24, variants: 1 },
};

/** A block of ice around a frozen zombie `wide` across and `tall` high. */
export function drawIce(ctx: Ctx, wide: number, tall: number, thaw: number) {
  const half = wide / 2 + 1.5;
  const top = -tall - 2.5;
  ctx.globalAlpha = 0.3 + 0.25 * Math.min(1, thaw);
  ctx.lineWidth = 0.9;
  ctx.lineJoin = "round";
  shape(ctx, [-half, 1, -half + 0.6, top + 4, -half * 0.3, top, half * 0.6, top + 1.6, half, top + 6, half - 0.4, 1], "#bfe9ff");
  ctx.globalAlpha = 0.9;
  ctx.strokeStyle = "#ffffff";
  ctx.stroke();
  limb(ctx, -half * 0.5, top + 5, -half * 0.5, top + 5 + tall * 0.3, 0.8, "#ffffff");
  limb(ctx, half * 0.3, top + 4, half * 0.6, top + 8, 0.7, "#ffffff");
  ctx.globalAlpha = 1;
}

// ---------------------------------------------------------------------------
// Players and what they carry
// ---------------------------------------------------------------------------

/** The weapon in a player's hands, pointing along +x from (0, 0). */
export function drawHeld(ctx: Ctx, weapon: string, recoil: number) {
  ctx.save();
  ctx.translate(-recoil - 3, 0);
  ctx.lineWidth = 0.8;
  switch (weapon) {
    case "crossbow":
      limb(ctx, 3, 0, 13, 0, 1.8, "#8a5a33");
      ctx.beginPath();
      ctx.moveTo(9, -5.5);
      ctx.quadraticCurveTo(13.5, 0, 9, 5.5);
      ctx.lineWidth = 1.3;
      ctx.strokeStyle = "#c9cdd2";
      ctx.stroke();
      limb(ctx, 9, -5.5, 9, 5.5, 0.4, "#f1ead9");
      break;
    case "machine_gun":
      limb(ctx, 3, 0, 14.5, 0, 2.6, "#2b3036");
      limb(ctx, 7, 1.6, 7, 4, 2, "#4a525c");
      limb(ctx, 13, 0, 16, 0, 1.3, "#111");
      break;
    case "sniper":
      limb(ctx, 2, 0, 19, 0, 1.6, "#3a2f25");
      limb(ctx, 6, 0, 11, 0, 2.8, "#1d232b");
      dot(ctx, 8.5, -2.2, 1.3, "#66d9e8");
      break;
    case "shotgun":
      limb(ctx, 3, 0, 8, 0, 3, "#8a5a33");
      limb(ctx, 8, -0.9, 14.5, -0.9, 1.3, "#4a525c");
      limb(ctx, 8, 0.9, 14.5, 0.9, 1.3, "#4a525c");
      break;
    case "flamethrower":
      limb(ctx, 3, 0, 12.5, 0, 2.6, "#7a2e12");
      dot(ctx, 13.5, 0, 1.7, "#ffa94d");
      blob(ctx, 1.5, 3.4, 2.6, 2, "#c92a2a", INK);
      break;
    case "grenade":
      blob(ctx, 6.5, 0, 2.9, 2.9, "#3f5527", INK);
      limb(ctx, 6.5, -2.9, 7.8, -4, 1, "#c9cdd2");
      break;
    case "snowball":
      blob(ctx, 6.5, 0, 3, 3, "#ffffff", "#a5d8ff");
      break;
    case "boulder":
      blob(ctx, 7.5, 0, 4.8, 4.8, "#8c939b", INK);
      dot(ctx, 6.4, -1.5, 1.2, "#a9b0b8");
      break;
  }
  ctx.restore();
}

const OUTLINE = "#10151c";

/**
 * A player standing on (0, 0): seen from behind when defending the
 * top wall, from the front on the bottom one. `stride` moves the legs.
 */
export function drawSurvivor(
  ctx: Ctx,
  style: SurvivorStyle | null,
  color: ShirtColor | null,
  weapon: string,
  recoil: number,
  back: boolean,
  stride: number
) {
  const look = SURVIVOR_BY_KEY[style ?? "dad"] ?? SURVIVOR_BY_KEY.dad;
  const shirt = SHIRT_COLORS[color ?? "blue"] ?? SHIRT_COLORS.blue;
  const step = Math.sin(stride);
  ctx.save();
  ctx.scale(look.size, look.size);
  ctx.lineWidth = 0.9;
  ctx.lineJoin = "round";

  limb(ctx, -2.3, -10, -2.5 - step * 0.9, -1.4, 3.1, "#2f3b4c", OUTLINE);
  limb(ctx, 2.3, -10, 2.5 + step * 0.9, -1.4, 3.1, "#2f3b4c", OUTLINE);
  blob(ctx, -2.8 - step * 0.9, -0.9, 2.3, 1.3, "#15191f");
  blob(ctx, 2.8 + step * 0.9, -0.9, 2.3, 1.3, "#15191f");

  // The weapon points where they are looking: up the screen, past
  // their right shoulder, or down it in front of them.
  const held = () => {
    ctx.save();
    ctx.translate(back ? 3.6 : -2.8, back ? -15.5 : -12.5);
    ctx.rotate(back ? -Math.PI / 2 : Math.PI / 2);
    ctx.scale(0.82, 0.9);
    drawHeld(ctx, weapon, recoil);
    ctx.restore();
  };
  if (back) held();
  box(ctx, -4.8, -20.4, 9.6, 11.6, 2.6, shirt.main, OUTLINE);
  ctx.fillStyle = shirt.dark;
  ctx.fillRect(-4.3, -10.6, 8.6, 1.5);
  if (back) {
    limb(ctx, -5, -18.6, -5.6, -13.4, 2.5, shirt.dark, OUTLINE);
    limb(ctx, 5, -18.6, 4.6, -15, 2.5, shirt.dark, OUTLINE);
  } else {
    limb(ctx, 5, -18.6, -0.6, -12.6, 2.5, shirt.dark, OUTLINE);
    limb(ctx, -5, -18.6, -3.6, -13.2, 2.5, shirt.dark, OUTLINE);
    held();
    dot(ctx, -0.8, -12.4, 1.3, look.skin);
    dot(ctx, -3.4, -12.6, 1.3, look.skin);
  }

  const hy = -24.2;
  const hr = 4.4;
  const head = look.head;
  // Whatever hangs down behind or sticks out at the sides.
  if (head === "long" && !back) box(ctx, -5.6, hy - 3.6, 11.2, 10.4, 3.4, look.hair, OUTLINE);
  if (head === "pigtails") {
    for (const side of [-1, 1]) {
      blob(ctx, side * 5.9, hy + 1.4, 2.2, 2.9, look.hair, OUTLINE);
      dot(ctx, side * 4.9, hy - 0.4, 0.9, shirt.light);
    }
  }
  if (head === "wild") {
    for (const side of [-1, 1]) {
      shape(
        ctx,
        [side * 3.4, hy - 2.6, side * 7.6, hy - 4.4, side * 5.6, hy - 1.2, side * 8, hy + 0.6, side * 4.2, hy + 2],
        look.hair,
        OUTLINE
      );
    }
  }
  if (head === "bun") blob(ctx, 0, hy - 5.2, 2.6, 2.4, look.hair, OUTLINE);
  if (head === "helmet_ponytail" && !back) blob(ctx, 4.9, hy + 2.6, 1.6, 3, look.hair, OUTLINE);

  blob(ctx, 0, hy, hr, hr, look.skin, OUTLINE);
  /** Hair over the top of the head, down to `brow`. */
  const cap = (fill: string, brow: number, edge?: string) => {
    ctx.beginPath();
    ctx.arc(0, hy, hr + 0.1, Math.PI + brow, TAU - brow);
    ctx.quadraticCurveTo(0, hy - hr * 0.55 + brow * 2.4, -Math.cos(brow) * hr, hy - Math.sin(brow) * hr);
    ctx.closePath();
    ctx.fillStyle = fill;
    ctx.fill();
    if (edge) {
      ctx.strokeStyle = edge;
      ctx.stroke();
    }
  };
  if (back) {
    switch (head) {
      case "helmet":
      case "helmet_ponytail":
        blob(ctx, 0, hy - 0.4, hr + 0.6, hr + 0.3, HELMET, HELMET_DARK);
        limb(ctx, -hr, hy + 1.8, hr, hy + 1.8, 0.9, HELMET_DARK);
        if (head === "helmet_ponytail") blob(ctx, 0, hy + 6, 1.7, 3.6, look.hair, OUTLINE);
        break;
      case "long":
        box(ctx, -5.4, hy - hr - 0.2, 10.8, 13.4, 4, look.hair, OUTLINE);
        break;
      case "cap":
        blob(ctx, 0, hy, hr + 0.1, hr + 0.1, look.hair, OUTLINE);
        cap(shirt.dark, -0.25, OUTLINE);
        dot(ctx, 0, hy - hr + 0.6, 0.8, shirt.light);
        break;
      case "wild":
        limb(ctx, -3, hy + 2.6, 3, hy + 2.6, 1.4, look.hair);
        break;
      default:
        blob(ctx, 0, hy, hr + 0.1, hr + 0.1, look.hair, OUTLINE);
    }
  } else {
    dot(ctx, -1.6, hy + 0.3, 0.65, "#1b1b1b");
    dot(ctx, 1.6, hy + 0.3, 0.65, "#1b1b1b");
    ctx.beginPath();
    ctx.arc(0, hy + 1.5, 1.5, 0.25, Math.PI - 0.25);
    ctx.lineWidth = 0.6;
    ctx.strokeStyle = "#7a3b2e";
    ctx.stroke();
    ctx.lineWidth = 0.9;
    switch (head) {
      case "helmet":
      case "helmet_ponytail":
        cap(HELMET, -0.12, HELMET_DARK);
        limb(ctx, -hr - 0.4, hy - 0.9, hr + 0.4, hy - 0.9, 1.1, HELMET_DARK);
        break;
      case "cap":
        cap(shirt.dark, 0.1, OUTLINE);
        blob(ctx, 0, hy - 1.5, hr + 1, 1.2, shirt.light, OUTLINE);
        break;
      case "wild":
        break;
      default:
        cap(look.hair, head === "short" ? 0.42 : 0.2);
    }
    if (look.glasses) {
      ctx.lineWidth = 0.55;
      blob(ctx, -1.7, hy + 0.3, 1.45, 1.45, "rgba(248,249,250,0.75)", OUTLINE);
      blob(ctx, 1.7, hy + 0.3, 1.45, 1.45, "rgba(248,249,250,0.75)", OUTLINE);
      dot(ctx, -1.7, hy + 0.3, 0.5, "#1b1b1b");
      dot(ctx, 1.7, hy + 0.3, 0.5, "#1b1b1b");
    }
  }
  ctx.restore();
}

// ---------------------------------------------------------------------------
// Guard dogs
// ---------------------------------------------------------------------------

interface DogLook {
  coat: string;
  /** The legs on its far side. */
  shade: string;
  rim: string;
  /** A bib of another colour, if it has one. */
  chest: string | null;
  muzzle: string;
  /** Socks of another colour, if it has them. */
  socks: string | null;
  ear: string;
  ears: "flop" | "rose";
  tail: "bushy" | "curl" | "stub";
  eye: string;
  /** Next to the others. */
  size: number;
  /** Half its length, the length of its legs, half its depth, its head. */
  long: number;
  leg: number;
  girth: number;
  head: number;
  cheek?: string;
  /** Short in the face, with an underbite. */
  flat?: boolean;
}

const DOG_LOOKS: Record<string, DogLook> = {
  // Bernese mountain dog: black, white bib and blaze, rust on the legs and cheeks.
  juno: {
    coat: "#221f22", shade: "#131114", rim: "#5d5762", chest: "#f6f2ea", muzzle: "#f6f2ea", socks: "#b8651f",
    ear: "#171518", ears: "flop", tail: "bushy", eye: "#e9ecef", size: 1.1, long: 7.4, leg: 4.8, girth: 4.1, head: 3.7,
    cheek: "#b8651f",
  },
  // Newfoundland: all black, and a lot of it.
  wynnie: {
    coat: "#1a1a20", shade: "#0d0d11", rim: "#5c6474", chest: null, muzzle: "#2a2a33", socks: null,
    ear: "#101014", ears: "flop", tail: "bushy", eye: "#e9ecef", size: 1.24, long: 7.8, leg: 4.4, girth: 4.9, head: 4.2,
  },
  // Husky and spaniel: white, small, soft grey ears, a curled tail.
  wolf: {
    coat: "#f6f7f9", shade: "#cbd1da", rim: "#4d5563", chest: null, muzzle: "#ffffff", socks: null,
    ear: "#b3bbc7", ears: "flop", tail: "curl", eye: "#333333", size: 0.88, long: 6.4, leg: 4.6, girth: 3.5, head: 3.3,
  },
  // English bulldog: fawn and white, low and wide.
  missy: {
    coat: "#dba367", shade: "#b27c46", rim: "#4a2f18", chest: "#fff4e6", muzzle: "#fff4e6", socks: null,
    ear: "#a86f3d", ears: "rose", tail: "stub", eye: "#333333", size: 0.98, long: 6.6, leg: 3.1, girth: 4.3, head: 4.5,
    flat: true,
  },
};

export type DogPose = "run" | "bite" | "sit" | "lie";

function dogTail(ctx: Ctx, d: DogLook, x: number, y: number, wag: number) {
  if (d.tail === "stub") {
    blob(ctx, x - 0.4, y, 1.3, 1.1, d.coat, d.rim);
  } else if (d.tail === "curl") {
    ctx.lineCap = "round";
    const thin = ctx.lineWidth;
    for (const [width, color] of [[2 + thin * 2, d.rim], [2, d.coat]] as const) {
      ctx.beginPath();
      ctx.arc(x - 0.6 + wag * 0.5, y - 2.6, 2.4, Math.PI * 0.45, Math.PI * 2.05);
      ctx.lineWidth = width;
      ctx.strokeStyle = color;
      ctx.stroke();
    }
    ctx.lineWidth = thin;
  } else {
    limb(ctx, x, y, x - 4.8, y - 2 - wag * 2.2, 3, d.coat, d.rim);
    if (d.chest) dot(ctx, x - 4.9, y - 2.1 - wag * 2.2, 1.1, d.chest);
  }
}

function dogLeg(ctx: Ctx, d: DogLook, x1: number, y1: number, x2: number, y2: number, far: boolean) {
  const width = d.flat ? 2.7 : 2.4;
  limb(ctx, x1, y1, x2, y2, width, far ? d.shade : d.coat, d.rim);
  if (far || !d.socks) return;
  limb(ctx, x1 + (x2 - x1) * 0.5, y1 + (y2 - y1) * 0.5, x2, y2, width, d.socks);
  dot(ctx, x2, y2, width * 0.5, d.chest ?? d.socks);
}

function dogHead(ctx: Ctx, d: DogLook, x: number, y: number, tilt: number, asleep: boolean, jaw: number) {
  const r = d.head;
  ctx.save();
  ctx.translate(x, y);
  ctx.rotate(tilt);
  const snout = d.flat ? r * 0.5 : r;
  const deep = d.flat ? r * 0.8 : r * 0.62;
  if (jaw > 0) {
    ctx.save();
    ctx.translate(r * 0.4, r * 0.32);
    ctx.rotate(jaw * 0.6);
    box(ctx, 0, 0, snout + 0.3, deep * 0.5, 0.7, "#8a2f3a", d.rim);
    ctx.restore();
  }
  blob(ctx, 0, 0, r, r * 0.95, d.coat, d.rim);
  box(ctx, r * 0.35, -r * 0.18, snout + 0.6, deep, deep * 0.45, d.muzzle, d.rim);
  dot(ctx, r * 0.35 + snout + 0.3, -r * 0.02, 0.8, "#111");
  if (d.chest && !d.flat) limb(ctx, r * 0.7, -r * 0.2, r * 0.2, -r * 0.82, 1, d.chest);
  if (d.cheek) dot(ctx, r * 0.36, r * 0.42, 1, d.cheek);
  if (d.flat) {
    // Jowls, and a tooth that never goes in.
    blob(ctx, r * 0.7, r * 0.5, r * 0.42, r * 0.3, d.muzzle, d.rim);
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(r * 0.9, r * 0.18, 0.7, 0.9);
    limb(ctx, r * 0.1, -r * 0.62, r * 0.5, -r * 0.5, 0.5, d.rim);
  }
  if (asleep) {
    limb(ctx, r * 0.1, -r * 0.2, r * 0.5, -r * 0.14, 0.6, d.eye);
  } else {
    dot(ctx, r * 0.3, -r * 0.3, 0.9, "#ffffff");
    dot(ctx, r * 0.37, -r * 0.3, 0.55, "#111");
  }
  if (d.ears === "rose") {
    blob(ctx, -r * 0.45, -r * 0.72, r * 0.42, r * 0.3, d.ear, d.rim);
  } else {
    ctx.beginPath();
    ctx.ellipse(-r * 0.42, r * 0.02, r * 0.36, r * 0.8, 0.3, 0, TAU);
    ctx.fillStyle = d.ear;
    ctx.fill();
    ctx.strokeStyle = d.rim;
    ctx.stroke();
  }
  ctx.restore();
}

/**
 * A guard dog standing on (0, 0), seen from the side and facing +x.
 * `cycle` counts strides, bites or wags of the tail; `collar` is its
 * owner's colour.
 */
export function drawGuardDog(ctx: Ctx, key: string, pose: DogPose, cycle: number, collar: string) {
  const d = DOG_LOOKS[key] ?? DOG_LOOKS.juno;
  const swing = Math.sin(cycle * TAU);
  const L = d.long;
  const G = d.girth;
  ctx.save();
  ctx.scale(d.size, d.size);
  ctx.lineWidth = 0.5;
  ctx.lineJoin = "round";
  /** The neck, with its collar, up to a head at (hx, hy). */
  const neck = (x: number, y: number, hx: number, hy: number) => {
    limb(ctx, x, y, hx, hy, d.head * 1.25, d.coat, d.rim);
    const mx = x + (hx - x) * 0.55;
    const my = y + (hy - y) * 0.55;
    const across = Math.hypot(hx - x, hy - y) || 1;
    const nx = (-(hy - y) / across) * d.head * 0.62;
    const ny = ((hx - x) / across) * d.head * 0.62;
    limb(ctx, mx - nx, my - ny, mx + nx, my + ny, 1.5, collar);
  };

  if (pose === "lie") {
    // Flat out, chin on its paws.
    dogTail(ctx, d, -L + 0.8, -G * 0.7, -0.7);
    blob(ctx, 0, -G * 0.82, L, G * 0.82, d.coat, d.rim);
    blob(ctx, -L + 3.2, -G * 0.8, 3.4, G * 0.8, d.coat, d.rim);
    if (d.chest) blob(ctx, L - 1.6, -G * 0.6, 1.8, G * 0.5, d.chest);
    dogLeg(ctx, d, L - 2.4, -1.3, L + d.head + 0.6, -1.2, false);
    neck(L - 1.6, -G, L + 0.4, -d.head - 0.4);
    dogHead(ctx, d, L + 0.8, -d.head * 0.95 - 0.5 + swing * 0.25, 0.2, true, 0);
  } else if (pose === "sit") {
    // Haunches down, chest up, tail going.
    dogTail(ctx, d, -L * 0.62, -1.5, swing * 0.7);
    blob(ctx, -L * 0.3, -G, G * 1.1, G, d.coat, d.rim);
    dogLeg(ctx, d, -L * 0.3, -1.3, L * 0.22, -1.3, false);
    ctx.save();
    ctx.translate(L * 0.12, -d.leg - G * 0.75);
    ctx.rotate(-1);
    blob(ctx, 0, 0, L * 0.72, G * 0.95, d.coat, d.rim);
    ctx.restore();
    if (d.chest) blob(ctx, L * 0.45, -d.leg - G * 0.55, 1.7, G * 0.8, d.chest);
    dogLeg(ctx, d, L * 0.42, -d.leg - G * 0.2, L * 0.48, -1.3, false);
    const hx = L * 0.5 + d.head * 0.3;
    const hy = -d.leg - G * 1.7 - d.head * 0.45;
    neck(L * 0.3, -d.leg - G * 1.25, hx - d.head * 0.3, hy + d.head * 0.3);
    dogHead(ctx, d, hx, hy, -0.12, false, 0);
  } else {
    const running = pose === "run";
    const by = -d.leg - G + 1 - (running ? Math.abs(swing) * 1.3 : 0);
    const reach = running ? d.leg * 0.9 : 0;
    const front = L - 2.6;
    const hind = -L + 2.6;
    const hip = by + G * 0.45;
    // A bound: the front legs reach out as the back ones push off.
    dogLeg(ctx, d, front + 1, hip, front + 1 + (swing * 0.6 + 0.35) * reach, -1.3, true);
    dogLeg(ctx, d, hind + 1, hip, hind + 1 - (swing * 0.6 + 0.35) * reach, -1.3, true);
    dogTail(ctx, d, -L + 0.6, by - G * 0.35, running ? 0.5 + swing * 0.3 : swing);
    blob(ctx, 0, by, L, G, d.coat, d.rim);
    if (d.chest) blob(ctx, L - 1.5, by + G * 0.2, 1.8, G * 0.65, d.chest);
    dogLeg(ctx, d, front - 0.6, hip, front - 0.6 + swing * reach, -1.3, false);
    dogLeg(ctx, d, hind - 0.6, hip, hind - 0.6 - swing * reach, -1.3, false);
    const lunge = running ? 0 : 0.8 + Math.abs(swing) * 1.8;
    const hx = L + d.head * 0.5 + lunge;
    const hy = by - G * 0.75 - d.head * 0.2 + (running ? 0 : 0.8);
    neck(L - 2, by - G * 0.35, hx - d.head * 0.35, hy + d.head * 0.25);
    dogHead(ctx, d, hx, hy, running ? -0.1 : 0.12, false, running ? 0.2 : (1 + swing) / 2);
  }
  ctx.restore();
}

// ---------------------------------------------------------------------------
// The grounds and the house (drawn once, kept as pictures)
// ---------------------------------------------------------------------------

/** A repeatable scatter, so the grass doesn't change between redraws. */
function scatter(seed: number) {
  let s = seed >>> 0 || 1;
  return () => {
    s = (Math.imul(s, 1664525) + 1013904223) >>> 0;
    return s / 4294967296;
  };
}

/**
 * The grass and the floor of the house, with its roof off so the
 * family and their dogs can be seen inside. The walls that things walk
 * behind and the trees are separate pictures (`drawWall`, `drawTree`).
 */
export function drawGround(ctx: Ctx, rules: ZombieRules, trees: number[][], seed: number) {
  const { w } = rules.world;
  const h = rules.world.h * TILT;
  const house = rules.house;
  const rand = scatter(seed + 77);
  const top = (house.y - house.h / 2) * TILT;
  const bottom = (house.y + house.h / 2) * TILT;
  const left = house.x - house.w / 2;
  const middle = house.y * TILT;

  // Night-time grass, lighter near the house where the lights are on.
  const grass = ctx.createLinearGradient(0, 0, 0, h);
  grass.addColorStop(0, "#1c3a26");
  grass.addColorStop(0.42, "#2f5a34");
  grass.addColorStop(0.58, "#2f5a34");
  grass.addColorStop(1, "#1c3a26");
  ctx.fillStyle = grass;
  ctx.fillRect(0, 0, w, h);
  for (let i = 0; i < 240; i++) {
    const x = rand() * w;
    const y = rand() * h;
    ctx.fillStyle = rand() < 0.5 ? "rgba(20,45,26,0.5)" : "rgba(82,130,70,0.35)";
    ctx.beginPath();
    ctx.ellipse(x, y, 2 + rand() * 8, 1 + rand() * 2.4, 0, 0, TAU);
    ctx.fill();
  }
  // Tufts.
  ctx.strokeStyle = "rgba(120,170,90,0.5)";
  ctx.lineWidth = 0.7;
  for (let i = 0; i < 150; i++) {
    const x = rand() * w;
    const y = rand() * h;
    ctx.beginPath();
    ctx.moveTo(x - 1.4, y);
    ctx.lineTo(x - 0.6, y - 2.6);
    ctx.moveTo(x, y);
    ctx.lineTo(x, y - 3.2);
    ctx.moveTo(x + 1.4, y);
    ctx.lineTo(x + 0.7, y - 2.4);
    ctx.stroke();
  }
  // Trampled earth where the crowd gathers at each wall.
  const pile = rules.pile_depth * TILT;
  for (const y of [top - pile / 2 - 2, bottom + pile / 2 + 2]) {
    const reach = house.w * 0.6;
    ctx.save();
    ctx.translate(house.x, y);
    ctx.scale(1, (pile * 1.5) / reach);
    const dirt = ctx.createRadialGradient(0, 0, reach * 0.2, 0, 0, reach);
    dirt.addColorStop(0, "rgba(94,74,48,0.6)");
    dirt.addColorStop(0.7, "rgba(94,74,48,0.35)");
    dirt.addColorStop(1, "rgba(94,74,48,0)");
    ctx.fillStyle = dirt;
    ctx.beginPath();
    ctx.arc(0, 0, reach, 0, TAU);
    ctx.fill();
    ctx.restore();
  }
  for (const [x, y, r] of trees) blob(ctx, x + 3, y * TILT + 1, r * 0.95, r * 0.5, "rgba(0,0,0,0.3)");

  // The house: its floor, a rug for the score, and the two side walls.
  ctx.fillStyle = "rgba(0,0,0,0.3)";
  ctx.fillRect(left + 3, top + 3, house.w + 2, bottom - top + 2);
  ctx.fillStyle = "#8f6f4b";
  ctx.fillRect(left, top, house.w, bottom - top);
  ctx.strokeStyle = "rgba(52,34,18,0.5)";
  ctx.lineWidth = 0.6;
  let row = 0;
  for (let y = top + 6.5; y < bottom; y += 6.5, row++) {
    ctx.beginPath();
    ctx.moveTo(left, y);
    ctx.lineTo(left + house.w, y);
    ctx.stroke();
    for (let x = left + (row % 2 ? 22 : 46); x < left + house.w; x += 48) {
      ctx.beginPath();
      ctx.moveTo(x, y - 6.5);
      ctx.lineTo(x, y);
      ctx.stroke();
    }
  }
  ctx.lineWidth = 1;
  box(ctx, house.x - 74, middle - 27, 148, 28, 4, "#5a2a36", "#e0b458");
  ctx.lineWidth = 0.6;
  ctx.strokeStyle = "rgba(224,180,88,0.5)";
  ctx.strokeRect(house.x - 70.5, middle - 23.5, 141, 21);
  for (const x of [left, left + house.w]) {
    ctx.fillStyle = "#9b7650";
    ctx.fillRect(x - 2.5, top - WALL_H, 5, bottom - top);
    ctx.strokeStyle = "#2a1b0e";
    ctx.lineWidth = 0.9;
    ctx.strokeRect(x - 2.5, top - WALL_H, 5, bottom - top);
  }
}

/**
 * One of the long walls, as wide as `width`, standing on y = 0 from
 * x = 0: boards, with sandbags along the top and a post at each end.
 */
export function drawWall(ctx: Ctx, width: number) {
  const top = -WALL_H - WALL_T;
  ctx.fillStyle = "#6b4a2e";
  ctx.fillRect(0, -WALL_H, width, WALL_H);
  ctx.strokeStyle = "rgba(30,18,8,0.55)";
  ctx.lineWidth = 0.6;
  for (let x = 9; x < width; x += 9) {
    ctx.beginPath();
    ctx.moveTo(x, -WALL_H);
    ctx.lineTo(x, 0);
    ctx.stroke();
  }
  ctx.fillStyle = "#9b7650";
  ctx.fillRect(0, top, width, WALL_T);
  ctx.strokeStyle = "#2a1b0e";
  ctx.lineWidth = 0.9;
  ctx.strokeRect(0, top, width, WALL_H + WALL_T);
  ctx.lineWidth = 0.6;
  for (let x = 8; x < width - 12; x += 11.2) {
    blob(ctx, x + 5, top + WALL_T / 2 - 1.4, 5.3, 2.7, "#c9a56b", "#7d6238");
  }
  ctx.lineWidth = 0.9;
  for (const x of [0, width]) {
    box(ctx, x - 3, top - 5, 6, WALL_H + WALL_T + 5, 1, "#5a3d24", "#2a1b0e");
    ctx.fillStyle = "#8a6238";
    ctx.fillRect(x - 3, top - 5, 6, 2);
  }
}

/** A tree whose trunk stands on (0, 0), with leaves `r` across. */
export function drawTree(ctx: Ctx, r: number, seed: number) {
  const rand = scatter(seed);
  const cy = -r * 1.3;
  ctx.lineWidth = 0.9;
  box(ctx, -r * 0.17, -r * 1.05, r * 0.34, r * 1.05, 1, "#5b3d22", "#2a1b0e");
  blob(ctx, 0, cy, r, r * 0.92, "#1f4a2a", "#122b18");
  for (let i = 0; i < 7; i++) {
    const a = rand() * TAU;
    const d = rand() * r * 0.55;
    const size = r * (0.35 + rand() * 0.25);
    blob(ctx, Math.cos(a) * d, cy + Math.sin(a) * d * 0.9, size, size * 0.92, i % 2 ? "#2f6b3a" : "#275932");
  }
  for (let i = 0; i < 5; i++) {
    const a = rand() * TAU;
    const d = rand() * r * 0.6;
    blob(ctx, Math.cos(a) * d - 1, cy + Math.sin(a) * d * 0.9 - 2, r * 0.22, r * 0.2, "rgba(96,160,96,0.55)");
  }
}

export function drawBoulder(ctx: Ctx, radius: number, spin: number) {
  ctx.lineWidth = 1.3;
  blob(ctx, 0, 0, radius, radius, "#878e96", "#2b3036");
  ctx.save();
  ctx.rotate(spin);
  blob(ctx, -radius * 0.3, -radius * 0.25, radius * 0.42, radius * 0.34, "#a3aab2");
  ctx.strokeStyle = "#4a525c";
  ctx.lineWidth = 1.1;
  ctx.beginPath();
  ctx.moveTo(radius * 0.1, -radius * 0.7);
  ctx.lineTo(radius * 0.25, -radius * 0.1);
  ctx.lineTo(-radius * 0.1, radius * 0.3);
  ctx.moveTo(radius * 0.25, -radius * 0.1);
  ctx.lineTo(radius * 0.7, radius * 0.2);
  ctx.moveTo(-radius * 0.6, radius * 0.3);
  ctx.lineTo(-radius * 0.2, radius * 0.7);
  ctx.stroke();
  ctx.restore();
}

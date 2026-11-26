import { SHIRT_COLORS } from "../Turkey";
import type { ShirtColor, SurvivorStyle, ZombieRules } from "../types";
import { HELMET, HELMET_DARK, SURVIVOR_BY_KEY } from "./survivors";

// Everything on the map is drawn here, with plain canvas calls, from
// above. Each function draws one thing around (0, 0) facing along +x;
// the engine moves and turns the canvas first.

type Ctx = CanvasRenderingContext2D;

const TAU = Math.PI * 2;
const INK = "#141a12";

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

function limb(ctx: Ctx, x1: number, y1: number, x2: number, y2: number, width: number, color: string) {
  ctx.beginPath();
  ctx.moveTo(x1, y1);
  ctx.lineTo(x2, y2);
  ctx.lineWidth = width;
  ctx.lineCap = "round";
  ctx.strokeStyle = color;
  ctx.stroke();
}

// ---------------------------------------------------------------------------
// Zombies
// ---------------------------------------------------------------------------

/**
 * `walk` is the zombie's own clock (it stops when the zombie is
 * frozen, and so does the animation); `attacking` once it is at the
 * house.
 */
export function drawWalker(ctx: Ctx, walk: number, attacking: boolean, seed: number) {
  // A shuffle: one leg drags, the body lurches from side to side.
  const step = Math.sin(walk * 7 + seed);
  const drag = Math.sin(walk * 3.5 + seed * 1.7);
  ctx.rotate(step * 0.2 + drag * 0.08);
  ctx.translate(attacking ? Math.abs(Math.sin(walk * 9)) * 1.6 : 0, drag * 0.9);
  ctx.lineWidth = 0.9;
  blob(ctx, -2.5 + step * 2.6, -3.2, 2.4, 1.7, "#2c3a22");
  blob(ctx, -2.5 - step * 1.4, 3.2, 2.4, 1.7, "#2c3a22");
  blob(ctx, 0, 0, 4.8, 6.2, "#4c6b3a", INK);
  // Torn shirt.
  ctx.fillStyle = "#3b5530";
  ctx.fillRect(-3.2, -2.2, 3.4, 1.6);
  ctx.fillRect(-1.2, 2.4, 3, 1.4);
  // Arms held out in front; one hangs lower.
  const reach = attacking ? 7.5 + Math.abs(Math.sin(walk * 9 + 1)) * 3 : 8.6 + step * 1.2;
  limb(ctx, 1.5, -5, reach, -3.6 + step * 0.8, 2.3, "#9ccc63");
  limb(ctx, 1.5, 5, reach - 2.2, 4.4 - step * 0.8, 2.3, "#8dbb58");
  blob(ctx, 2.4, 0, 4, 4, "#a5d46c", INK);
  dot(ctx, 4.9, -1.6, 0.85, "#e03131");
  dot(ctx, 4.9, 1.6, 0.85, "#e03131");
  limb(ctx, 6.1, -0.8, 6.1, 0.8, 0.9, "#2b1a1a");
}

export function drawDog(ctx: Ctx, walk: number, attacking: boolean, seed: number) {
  const run = Math.sin(walk * 15 + seed);
  ctx.translate(attacking ? Math.abs(Math.sin(walk * 11)) * 1.8 : 0, 0);
  ctx.scale(1 + run * 0.1, 1);
  ctx.lineWidth = 0.8;
  // Legs flick back and forth.
  for (const side of [-1, 1]) {
    limb(ctx, 3.5, side * 2, 3.5 + run * side * 2.6, side * 4.2, 1.3, "#3d4a31");
    limb(ctx, -3.5, side * 2, -3.5 - run * side * 2.6, side * 4.2, 1.3, "#3d4a31");
  }
  limb(ctx, -5.5, 0, -9, Math.sin(walk * 20 + seed) * 2.4, 1.2, "#56663f");
  blob(ctx, 0, 0, 6, 2.8, "#6b7d4f", INK);
  // Ribs showing.
  ctx.strokeStyle = "#48573a";
  ctx.lineWidth = 0.6;
  for (const x of [-2.5, -0.5, 1.5]) {
    ctx.beginPath();
    ctx.moveTo(x, -2);
    ctx.lineTo(x, 2);
    ctx.stroke();
  }
  ctx.lineWidth = 0.8;
  blob(ctx, 5.8, 0, 2.9, 2.5, "#7f9460", INK);
  ctx.fillStyle = "#56663f";
  for (const side of [-1, 1]) {
    ctx.beginPath();
    ctx.moveTo(4.4, side * 1.8);
    ctx.lineTo(3.4, side * 3.9);
    ctx.lineTo(6, side * 2.4);
    ctx.fill();
  }
  dot(ctx, 7.2, -1, 0.65, "#ffd43b");
  dot(ctx, 7.2, 1, 0.65, "#ffd43b");
}

export function drawOgre(ctx: Ctx, walk: number, attacking: boolean, seed: number) {
  const stomp = Math.sin(walk * 3.4 + seed);
  ctx.rotate(stomp * 0.12);
  ctx.translate(attacking ? Math.abs(Math.sin(walk * 5)) * 2.5 : 0, 0);
  ctx.lineWidth = 1.2;
  blob(ctx, -3 + stomp * 3, -7, 4.6, 3.2, "#2f3b1c");
  blob(ctx, -3 - stomp * 3, 7, 4.6, 3.2, "#2f3b1c");
  blob(ctx, 0, 0, 11.5, 13, "#5a6f2e", INK);
  blob(ctx, -1.5, 0, 7.5, 9, "#6b8236");
  // Shoulders and fists.
  blob(ctx, 1, -11.5, 5.2, 4.6, "#748d3a", INK);
  blob(ctx, 1, 11.5, 5.2, 4.6, "#748d3a", INK);
  const swing = attacking ? Math.abs(Math.sin(walk * 5 + 0.6)) * 6 : stomp * 3;
  blob(ctx, 9 + swing, -12, 4.4, 4.4, "#8fa94a", INK);
  blob(ctx, 9 - (attacking ? -swing * 0.6 : swing), 12, 4.4, 4.4, "#8fa94a", INK);
  blob(ctx, 4.5, 0, 6, 6.2, "#9bb655", INK);
  // Horns, eyes, underbite.
  ctx.fillStyle = "#e9e2c8";
  for (const side of [-1, 1]) {
    ctx.beginPath();
    ctx.moveTo(3, side * 5);
    ctx.lineTo(4.6, side * 8.4);
    ctx.lineTo(6, side * 4.6);
    ctx.fill();
  }
  dot(ctx, 8.2, -2.2, 1.2, "#e03131");
  dot(ctx, 8.2, 2.2, 1.2, "#e03131");
  ctx.fillStyle = "#fff";
  ctx.fillRect(9.6, -2.2, 1.3, 1.3);
  ctx.fillRect(9.6, 0.9, 1.3, 1.3);
}

export function drawBat(ctx: Ctx, walk: number, attacking: boolean, seed: number) {
  const flap = Math.cos(walk * (attacking ? 30 : 22) + seed);
  const span = 4 + 5.5 * Math.abs(flap);
  ctx.lineWidth = 0.7;
  ctx.fillStyle = "#4a3566";
  ctx.strokeStyle = "#1c1228";
  for (const side of [-1, 1]) {
    ctx.beginPath();
    ctx.moveTo(1, side * 1);
    ctx.lineTo(0.5, side * span);
    ctx.lineTo(-2.2, side * (span * 0.72));
    ctx.lineTo(-3.4, side * (span * 0.9));
    ctx.lineTo(-4.4, side * (span * 0.45));
    ctx.lineTo(-3, side * 0.8);
    ctx.closePath();
    ctx.fill();
    ctx.stroke();
  }
  blob(ctx, 0, 0, 3.4, 2.3, "#2e2140", "#1c1228");
  ctx.fillStyle = "#2e2140";
  for (const side of [-1, 1]) {
    ctx.beginPath();
    ctx.moveTo(2.4, side * 0.9);
    ctx.lineTo(3.2, side * 2.9);
    ctx.lineTo(4, side * 0.9);
    ctx.fill();
  }
  dot(ctx, 3.3, -0.8, 0.6, "#ff6b6b");
  dot(ctx, 3.3, 0.8, 0.6, "#ff6b6b");
}

export const ZOMBIE_ART: Record<string, (ctx: Ctx, walk: number, attacking: boolean, seed: number) => void> = {
  walker: drawWalker,
  dog: drawDog,
  ogre: drawOgre,
  bat: drawBat,
};

/** A block of ice around a frozen zombie. */
export function drawIce(ctx: Ctx, radius: number, thaw: number) {
  const r = radius + 2.5;
  ctx.globalAlpha = 0.35 + 0.3 * Math.min(1, thaw);
  ctx.fillStyle = "#bfe9ff";
  ctx.strokeStyle = "#ffffff";
  ctx.lineWidth = 1;
  ctx.beginPath();
  for (let i = 0; i < 6; i++) {
    const a = (i / 6) * TAU + 0.3;
    const d = r * (i % 2 ? 1 : 0.86);
    if (i === 0) ctx.moveTo(Math.cos(a) * d, Math.sin(a) * d);
    else ctx.lineTo(Math.cos(a) * d, Math.sin(a) * d);
  }
  ctx.closePath();
  ctx.fill();
  ctx.globalAlpha = 0.9;
  ctx.stroke();
  limb(ctx, -r * 0.4, -r * 0.3, -r * 0.1, -r * 0.55, 0.8, "#ffffff");
  ctx.globalAlpha = 1;
}

// ---------------------------------------------------------------------------
// Players and what they carry
// ---------------------------------------------------------------------------

/** The weapon in a player's hands, pointing along +x. */
export function drawHeld(ctx: Ctx, weapon: string, recoil: number) {
  ctx.save();
  ctx.translate(-recoil, 0);
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
      blob(ctx, 0.5, 5.5, 3, 2.2, "#c92a2a", INK);
      break;
    case "grenade":
      blob(ctx, 8.5, 0, 2.9, 2.9, "#3f5527", INK);
      limb(ctx, 8.5, -2.9, 9.8, -4, 1, "#c9cdd2");
      break;
    case "snowball":
      blob(ctx, 8.5, 0, 3, 3, "#ffffff", "#a5d8ff");
      break;
    case "boulder":
      blob(ctx, 10, 0, 5.2, 5.2, "#8c939b", INK);
      dot(ctx, 8.6, -1.5, 1.2, "#a9b0b8");
      break;
  }
  ctx.restore();
}

/** A player seen from above, facing along +x. */
export function drawSurvivor(
  ctx: Ctx,
  style: SurvivorStyle | null,
  color: ShirtColor | null,
  weapon: string,
  recoil: number
) {
  const look = SURVIVOR_BY_KEY[style ?? "dad"] ?? SURVIVOR_BY_KEY.dad;
  const shirt = SHIRT_COLORS[color ?? "blue"] ?? SHIRT_COLORS.blue;
  const s = look.size;
  ctx.lineWidth = 1;
  // Shoulders, then the arms out to the weapon.
  blob(ctx, 0, 0, 4.6 * s, 7.4 * s, shirt.main, "#10151c");
  limb(ctx, 1.5, -5.6 * s, 6.5, -1.6, 2.2 * s, shirt.dark);
  limb(ctx, 1.5, 5.6 * s, 5, 1.8, 2.2 * s, shirt.dark);
  drawHeld(ctx, weapon, recoil);
  dot(ctx, 6.6, -1.5, 1.3, look.skin);
  dot(ctx, 5.2, 1.8, 1.3, look.skin);

  const hr = 4.3 * s;
  // Hair behind the head.
  if (look.head === "long") blob(ctx, -3.2 * s, 0, 3.8 * s, 5.2 * s, look.hair, "#10151c");
  if (look.head === "helmet_ponytail") blob(ctx, -5.6 * s, 1.5, 3 * s, 1.6 * s, look.hair, "#10151c");
  if (look.head === "pigtails") {
    blob(ctx, -1.5, -5.6 * s, 2.3 * s, 2.3 * s, look.hair, "#10151c");
    blob(ctx, -1.5, 5.6 * s, 2.3 * s, 2.3 * s, look.hair, "#10151c");
    dot(ctx, -0.6, -4.2 * s, 0.9, shirt.light);
    dot(ctx, -0.6, 4.2 * s, 0.9, shirt.light);
  }
  if (look.head === "wild") {
    ctx.fillStyle = look.hair;
    for (const side of [-1, 1]) {
      ctx.beginPath();
      ctx.moveTo(1.5, side * 3.6);
      ctx.lineTo(0.5, side * 7.2);
      ctx.lineTo(-1, side * 4.6);
      ctx.lineTo(-3, side * 6.8);
      ctx.lineTo(-3.4, side * 3.4);
      ctx.closePath();
      ctx.fill();
    }
  }
  blob(ctx, 0, 0, hr, hr, look.skin, "#10151c");
  // Hair or a hat over the back of the head.
  const cover = (fill: string, open: number) => {
    ctx.beginPath();
    ctx.arc(0, 0, hr, open, TAU - open);
    ctx.closePath();
    ctx.fillStyle = fill;
    ctx.fill();
  };
  switch (look.head) {
    case "helmet":
    case "helmet_ponytail":
      blob(ctx, -0.3, 0, hr + 0.5, hr + 0.5, HELMET, HELMET_DARK);
      blob(ctx, -0.8, 0, hr * 0.5, hr * 0.5, "#6f8f48");
      break;
    case "short":
    case "long":
    case "pigtails":
      cover(look.hair, 1.05);
      break;
    case "cap":
      blob(ctx, -0.2, 0, hr + 0.3, hr + 0.3, shirt.dark, "#10151c");
      ctx.fillStyle = shirt.light;
      ctx.fillRect(-hr - 2.4, -2.2 * s, 2.8, 4.4 * s);
      dot(ctx, -0.2, 0, 0.9, shirt.light);
      break;
    case "wild":
      break;
    case "bun":
      cover(look.hair, 0.9);
      blob(ctx, -hr - 0.6, 0, 2.3 * s, 2.3 * s, look.hair, "#10151c");
      break;
  }
  if (look.glasses) {
    ctx.lineWidth = 0.6;
    blob(ctx, hr * 0.72, -1.7 * s, 1.25, 1.25, "#f8f9fa", "#10151c");
    blob(ctx, hr * 0.72, 1.7 * s, 1.25, 1.25, "#f8f9fa", "#10151c");
  }
}

// ---------------------------------------------------------------------------
// The grounds and the house (drawn once, kept as a picture)
// ---------------------------------------------------------------------------

/** A repeatable scatter, so the grass doesn't change between redraws. */
function scatter(seed: number) {
  let s = seed >>> 0 || 1;
  return () => {
    s = (Math.imul(s, 1664525) + 1013904223) >>> 0;
    return s / 4294967296;
  };
}

export function drawGround(ctx: Ctx, rules: ZombieRules, trees: number[][], seed: number) {
  const { w, h } = rules.world;
  const house = rules.house;
  const rand = scatter(seed + 77);
  const top = house.y - house.h / 2;
  const bottom = house.y + house.h / 2;
  const left = house.x - house.w / 2;

  // Night-time grass, lighter near the house where the lights are on.
  const grass = ctx.createLinearGradient(0, 0, 0, h);
  grass.addColorStop(0, "#1c3a26");
  grass.addColorStop(0.42, "#2f5a34");
  grass.addColorStop(0.58, "#2f5a34");
  grass.addColorStop(1, "#1c3a26");
  ctx.fillStyle = grass;
  ctx.fillRect(0, 0, w, h);
  for (let i = 0; i < 260; i++) {
    const x = rand() * w;
    const y = rand() * h;
    ctx.fillStyle = rand() < 0.5 ? "rgba(20,45,26,0.5)" : "rgba(82,130,70,0.35)";
    ctx.beginPath();
    ctx.ellipse(x, y, 2 + rand() * 7, 1.2 + rand() * 3, rand() * 3, 0, TAU);
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
  for (const y of [top - rules.pile_depth / 2 - 2, bottom + rules.pile_depth / 2 + 2]) {
    const reach = house.w * 0.6;
    const squash = (rules.pile_depth * 1.5) / reach;
    ctx.save();
    ctx.translate(house.x, y);
    ctx.scale(1, squash);
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
  // Tree shadows and trunks; the leaves go on later, over the zombies.
  for (const [x, y, r] of trees) {
    blob(ctx, x + 5, y + 7, r * 1.05, r * 0.95, "rgba(0,0,0,0.28)");
    blob(ctx, x, y, r * 0.3, r * 0.3, "#5b3d22", "#2a1b0e");
  }

  // The house: a porch along each wall, where the family stands,
  // and the roof between them.
  const porch = 22;
  ctx.fillStyle = "rgba(0,0,0,0.3)";
  ctx.fillRect(left + 4, top + 6, house.w, house.h);
  ctx.fillStyle = "#7a5a3a";
  ctx.fillRect(left, top, house.w, house.h);
  ctx.strokeStyle = "rgba(40,26,14,0.55)";
  ctx.lineWidth = 0.6;
  for (let x = left + 10; x < left + house.w; x += 10) {
    ctx.beginPath();
    ctx.moveTo(x, top);
    ctx.lineTo(x, top + porch);
    ctx.moveTo(x, bottom - porch);
    ctx.lineTo(x, bottom);
    ctx.stroke();
  }
  // Roof.
  const roofTop = top + porch;
  const roofH = house.h - porch * 2;
  const roof = ctx.createLinearGradient(0, roofTop, 0, roofTop + roofH);
  roof.addColorStop(0, "#8f3b32");
  roof.addColorStop(0.5, "#b24a3e");
  roof.addColorStop(0.5, "#9c4036");
  roof.addColorStop(1, "#7c3129");
  ctx.fillStyle = roof;
  ctx.fillRect(left - 3, roofTop, house.w + 6, roofH);
  ctx.strokeStyle = "rgba(40,12,10,0.35)";
  ctx.lineWidth = 0.5;
  for (let row = 0; row < roofH / 5; row++) {
    const y = roofTop + row * 5;
    ctx.beginPath();
    ctx.moveTo(left - 3, y);
    ctx.lineTo(left + house.w + 3, y);
    ctx.stroke();
    for (let x = left + (row % 2 ? 4 : 10); x < left + house.w; x += 12) {
      ctx.beginPath();
      ctx.moveTo(x, y);
      ctx.lineTo(x, y + 5);
      ctx.stroke();
    }
  }
  ctx.strokeStyle = "#5e221c";
  ctx.lineWidth = 1.6;
  ctx.strokeRect(left - 3, roofTop, house.w + 6, roofH);
  // Chimney.
  ctx.fillStyle = "#6b6f76";
  ctx.fillRect(left + house.w - 34, roofTop + 5, 12, 10);
  ctx.fillStyle = "#23262b";
  ctx.fillRect(left + house.w - 31, roofTop + 7.5, 6, 5);
  // The barricade along each wall.
  for (const y of [top, bottom]) {
    ctx.fillStyle = "#4a3320";
    ctx.fillRect(left, y - 2.2, house.w, 4.4);
    ctx.fillStyle = "#c9a56b";
    for (let x = left + 2; x < left + house.w - 6; x += 11) {
      ctx.beginPath();
      ctx.ellipse(x + 4.5, y, 5, 2.6, 0, 0, TAU);
      ctx.fill();
      ctx.strokeStyle = "#7d6238";
      ctx.lineWidth = 0.6;
      ctx.stroke();
    }
  }
  ctx.strokeStyle = "#2a1b0e";
  ctx.lineWidth = 1.4;
  ctx.strokeRect(left, top, house.w, house.h);
}

/** The leaves of the trees, drawn over anything walking past. */
export function drawCanopies(ctx: Ctx, trees: number[][]) {
  for (const [x, y, r] of trees) {
    const rand = scatter(Math.round(x * 31 + y * 17));
    blob(ctx, x, y, r, r, "#1f4a2a", "#122b18");
    for (let i = 0; i < 7; i++) {
      const a = rand() * TAU;
      const d = rand() * r * 0.55;
      const size = r * (0.35 + rand() * 0.25);
      blob(ctx, x + Math.cos(a) * d, y + Math.sin(a) * d, size, size, i % 2 ? "#2f6b3a" : "#275932");
    }
    for (let i = 0; i < 5; i++) {
      const a = rand() * TAU;
      const d = rand() * r * 0.6;
      blob(ctx, x + Math.cos(a) * d - 1, y + Math.sin(a) * d - 1.5, r * 0.22, r * 0.2, "rgba(96,160,96,0.55)");
    }
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

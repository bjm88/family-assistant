import type { SurvivorStyle } from "../types";

// The characters players pick from in Zombie Apocalypse. Everything
// that tells them apart is here, so the portrait (Survivor.tsx) and
// the little figure on the map (engine.ts) agree on who is who.

export interface SurvivorLook {
  key: SurvivorStyle;
  label: string;
  skin: string;
  hair: string;
  /** What is on their head, seen from above and from the front. */
  head: "helmet" | "helmet_ponytail" | "short" | "long" | "pigtails" | "cap" | "wild" | "bun";
  glasses: boolean;
  /** Next to a grown-up: kids are smaller. */
  size: number;
}

export const SURVIVORS: SurvivorLook[] = [
  { key: "army_guy", label: "Army guy", skin: "#d9a066", hair: "#3b2a1a", head: "helmet", glasses: false, size: 1.12 },
  { key: "army_girl", label: "Army girl", skin: "#f0c39a", hair: "#a8482a", head: "helmet_ponytail", glasses: false, size: 1 },
  { key: "dad", label: "Dad", skin: "#eab98f", hair: "#5b3a1e", head: "short", glasses: false, size: 1.05 },
  { key: "mom", label: "Mom", skin: "#f3cba5", hair: "#c9892e", head: "long", glasses: false, size: 1 },
  { key: "girl", label: "Girl", skin: "#f6d3b0", hair: "#7a4a22", head: "pigtails", glasses: false, size: 0.8 },
  { key: "boy", label: "Boy", skin: "#e8b48a", hair: "#2f2418", head: "cap", glasses: false, size: 0.8 },
  { key: "uncle", label: "Crazy uncle", skin: "#eec19c", hair: "#b9b4ab", head: "wild", glasses: true, size: 1.05 },
  { key: "grandma", label: "Crazy grandma", skin: "#f1cdb0", hair: "#d9d6d0", head: "bun", glasses: true, size: 0.92 },
];

export const SURVIVOR_BY_KEY = Object.fromEntries(SURVIVORS.map((s) => [s.key, s])) as Record<
  SurvivorStyle,
  SurvivorLook
>;

export const HELMET = "#5c7a3a";
export const HELMET_DARK = "#3f5527";

export function isSurvivorStyle(style: string | null | undefined): style is SurvivorStyle {
  return !!style && style in SURVIVOR_BY_KEY;
}

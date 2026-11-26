import { useEffect, useRef } from "react";
import type { ReactNode } from "react";
import { ZOMBIE_LOOK, drawGuardDog } from "./draw";
import type { DogPose } from "./draw";

// Small pictures of the weapons (seen from the side, pointing right),
// and of the zombies and the guard dogs as they look on the map.

const INK = "#10151c";
const line = { stroke: INK, strokeWidth: 1.2, strokeLinejoin: "round" as const };

const WEAPON_ART: Record<string, ReactNode> = {
  crossbow: (
    <>
      <rect x="5" y="14" width="30" height="4.5" rx="2" fill="#8a5a33" {...line} />
      <path d="M30 3 Q43 16 30 29" fill="none" stroke="#c9cdd2" strokeWidth="3" strokeLinecap="round" />
      <path d="M30 3 L13 16.2 L30 29" fill="none" stroke="#f1ead9" strokeWidth="1" />
      <path d="M13 16.2 H43" stroke="#e9ecef" strokeWidth="1.6" />
      <path d="M42 13 L47.5 16.2 L42 19.4 Z" fill="#e9ecef" {...line} strokeWidth={0.8} />
    </>
  ),
  machine_gun: (
    <>
      <path d="M11 12 L2 13 V21 L11 18.5 Z" fill="#5c4033" {...line} />
      <path d="M18 18 L16 29 H22.5 L23.5 18 Z" fill="#495057" {...line} />
      <path d="M27 18 L26 26 H30.5 V18 Z" fill="#343a40" {...line} />
      <rect x="32" y="13" width="13" height="3.2" fill="#212529" {...line} />
      <rect x="43.5" y="12" width="3.5" height="5.2" rx="1" fill="#495057" {...line} />
      <rect x="10" y="11" width="23" height="8" rx="1.5" fill="#343a40" {...line} />
      <rect x="14" y="8" width="11" height="3" rx="1" fill="#495057" {...line} />
    </>
  ),
  sniper: (
    <>
      <path d="M34 17 L31 27 M34 17 L39 27" stroke="#495057" strokeWidth="1.6" strokeLinecap="round" />
      <rect x="20" y="14.4" width="27" height="2.8" rx="1" fill="#212529" {...line} />
      <path d="M10 13 L1 15 V22.5 L10 19 Z" fill="#5c4033" {...line} />
      <rect x="9.5" y="13" width="17" height="6" rx="1.2" fill="#3a2f25" {...line} />
      <path d="M15 11.5 V13 M24 11.5 V13" stroke={INK} strokeWidth="1.6" />
      <rect x="11.5" y="6.5" width="17" height="5" rx="2.5" fill="#1d232b" {...line} />
      <circle cx="28.5" cy="9" r="2.6" fill="#66d9e8" {...line} />
    </>
  ),
  shotgun: (
    <>
      <path d="M2 14.5 L14 12 V19.5 L2 23.5 Z" fill="#8a5a33" {...line} />
      <rect x="24" y="11.6" width="22.5" height="3" rx="1" fill="#343a40" {...line} />
      <rect x="24" y="15" width="22.5" height="3" rx="1" fill="#343a40" {...line} />
      <rect x="13.5" y="11.5" width="11" height="8" rx="1.5" fill="#495057" {...line} />
      <rect x="27" y="18.2" width="11" height="4" rx="2" fill="#8a5a33" {...line} />
      <path d="M17 19.5 Q18 24 21.5 23" fill="none" stroke={INK} strokeWidth="1.2" />
    </>
  ),
  flamethrower: (
    <>
      <path d="M12 21 Q17 28 20 18" fill="none" stroke="#495057" strokeWidth="2" />
      <rect x="3" y="8" width="10" height="19" rx="5" fill="#c92a2a" {...line} />
      <rect x="6" y="5" width="4" height="3.5" rx="1" fill="#868e96" {...line} />
      <rect x="16" y="12.5" width="18" height="5.5" rx="1.5" fill="#495057" {...line} />
      <rect x="33.5" y="13.7" width="5" height="3.2" fill="#343a40" {...line} />
      <path d="M21 18 L20 24 H24 V18 Z" fill="#343a40" {...line} />
      <path d="M38.5 15.3 C40 6 48 10 47.5 15.3 C48 21 41 25 38.5 15.3 Z" fill="#ff922b" stroke="#d9480f" strokeWidth="1" />
      <path d="M40 15.3 C41 11 45 12.5 44.7 15.3 C45 18 41.5 19.5 40 15.3 Z" fill="#ffe066" />
    </>
  ),
  grenade: (
    <>
      <circle cx="15.5" cy="6.5" r="3.2" fill="none" stroke="#ffd43b" strokeWidth="1.6" />
      <path d="M28 5.5 L34.5 9 L33.5 17" fill="none" stroke="#adb5bd" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
      <rect x="20" y="4.5" width="8" height="5" rx="1" fill="#868e96" {...line} />
      <ellipse cx="24" cy="19.5" rx="9.5" ry="10.5" fill="#4f6b31" {...line} />
      <path d="M15 16 H33 M15 23 H33 M20.5 9.5 V30 M27.5 9.5 V30" stroke="#33481f" strokeWidth="1.2" />
    </>
  ),
  snowball: (
    <>
      <path d="M2 12 H9 M1 18 H8 M3 24 H9" stroke="#a5d8ff" strokeWidth="1.6" strokeLinecap="round" />
      <circle cx="25" cy="17" r="11.5" fill="#fff" stroke="#74c0fc" strokeWidth="1.4" />
      <path d="M17 22 Q22 28 31 25" fill="none" stroke="#d0ebff" strokeWidth="2.4" strokeLinecap="round" />
      <path d="M41 3 V11 M37 7 H45 M38.2 4.2 L43.8 9.8 M43.8 4.2 L38.2 9.8" stroke="#a5d8ff" strokeWidth="1.2" strokeLinecap="round" />
    </>
  ),
  chainsaw: (
    <>
      <rect x="19" y="13" width="27" height="7.5" rx="3.7" fill="#ced4da" {...line} />
      <path
        d="M21 13 H43 M21 20.5 H43"
        stroke="#343a40"
        strokeWidth="2.2"
        strokeDasharray="2 2"
        strokeLinecap="butt"
      />
      <path d="M5 8 Q12 1 19 8" fill="none" stroke="#343a40" strokeWidth="2.4" strokeLinecap="round" />
      <rect x="3" y="8" width="19" height="16" rx="3" fill="#f76707" {...line} />
      <rect x="6.5" y="11.5" width="8" height="5" rx="1" fill="#343a40" />
      <path d="M8 24 L7 29 H13 L13.5 24 Z" fill="#343a40" {...line} />
      <circle cx="18" cy="20" r="1.6" fill="#ffd43b" stroke={INK} strokeWidth="0.8" />
    </>
  ),
  boulder: (
    <>
      <path d="M2 27 H7 M3 22 H6" stroke="#b9a98a" strokeWidth="1.6" strokeLinecap="round" />
      <path d="M11 22 L14 11 L23 5.5 L34 7.5 L40.5 16 L38 25.5 L28 29.5 L16 28.5 Z" fill="#868e96" stroke="#343a40" strokeWidth="1.4" strokeLinejoin="round" />
      <path d="M17 13 L23 9 L30 10" fill="none" stroke="#ced4da" strokeWidth="2" strokeLinecap="round" />
      <path d="M26 14 L29 20 L25 24 M29 20 L35 21" fill="none" stroke="#495057" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />
    </>
  ),
};

export function WeaponIcon({ weapon, className }: { weapon: string; className?: string }) {
  return (
    <svg viewBox="0 0 48 32" className={className} aria-hidden>
      {WEAPON_ART[weapon] ?? WEAPON_ART.crossbow}
    </svg>
  );
}

/** A canvas `size` across, sharp on any screen, drawn by `paint` in a box 1 unit square. */
function useSketch(size: number, paint: (ctx: CanvasRenderingContext2D) => void, deps: unknown[]) {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const canvas = ref.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;
    const dpr = Math.min(3, window.devicePixelRatio || 1);
    canvas.width = Math.round(size * dpr);
    canvas.height = Math.round(size * dpr);
    ctx.setTransform(canvas.width, 0, 0, canvas.height, 0, 0);
    paint(ctx);
    // `paint` is new every time; `deps` says when the picture really changes.
  }, [size, ...deps]);
  return ref;
}

/** One zombie, drawn exactly as it is on the map, coming this way. */
export function ZombieIcon({ kind, size = 44 }: { kind: string; size?: number }) {
  const ref = useSketch(
    size,
    (ctx) => {
      const look = ZOMBIE_LOOK[kind] ?? ZOMBIE_LOOK.walker;
      const span = Math.max(look.w, look.h);
      ctx.scale(1 / span, 1 / span);
      ctx.translate(span / 2, (span + look.h) / 2 - (look.h - look.ay));
      look.draw(ctx, 0.3, false, false, 0);
    },
    [kind]
  );
  return <canvas ref={ref} style={{ width: size, height: size }} aria-hidden />;
}

/** A guard dog, as it is on the map; its collar is its owner's colour. */
export function DogIcon({
  dog,
  size = 56,
  collar = "#e03131",
  pose = "sit",
}: {
  dog: string;
  size?: number;
  collar?: string;
  pose?: DogPose;
}) {
  const ref = useSketch(
    size,
    (ctx) => {
      ctx.scale(1 / 30, 1 / 30);
      ctx.translate(pose === "sit" ? 13 : 12, 27);
      drawGuardDog(ctx, dog, pose, 0.3, collar);
    },
    [dog, collar, pose]
  );
  return <canvas ref={ref} style={{ width: size, height: size }} aria-hidden />;
}

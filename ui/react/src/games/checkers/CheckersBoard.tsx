import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties, MouseEvent } from "react";
import { SHIRT_COLORS, SHIRT_COLOR_KEYS } from "../Turkey";
import type { CheckersMove, CheckersState, ShirtColor } from "../types";
import { marbleStops } from "./Marble";
import { opposite, turnedTowards } from "./rules";
import type { Geometry } from "./rules";

// The board is drawn in units of a tenth of the gap between two holes.
const UNIT = 10;
const MARBLE = 4.15;
const HOLE = 3.3;
const HALF_WIDTH = 68.5;
const HALF_HEIGHT = 78.5;
// The star: two triangles laid over each other, a little bigger than
// the holes they carry. The thick rounded outline is the board's rim.
const RIM = 10;
const STAR_OUTER = 2 * (4 * (Math.sqrt(3) / 2) + 0.15) * UNIT;
const STAR_INNER = STAR_OUTER / Math.sqrt(3);
// How close a tap has to be to a hole that can be tapped.
const REACH = 8.5;

// How a move is played back, in milliseconds.
const LEAD = 140;
const HOP = 430;
const STEP = 320;
const REST = 80;
const TAIL = 160;

const STAR = Array.from({ length: 12 }, (_, k) => {
  const angle = (k * Math.PI) / 6;
  const radius = k % 2 === 0 ? STAR_OUTER : STAR_INNER;
  return `${(radius * Math.sin(angle)).toFixed(2)},${(-radius * Math.cos(angle)).toFixed(2)}`;
}).join(" ");

/** The triangle under point `point`, for a board turned by `turns` sixths. */
function pointTriangle(point: number, turns: number): string {
  const corners: [number, number][] = [
    [0, -84],
    [30, -39],
    [-30, -39],
  ];
  const angle = ((point + turns) * Math.PI) / 3;
  const cos = Math.cos(angle);
  const sin = Math.sin(angle);
  return corners.map(([x, y]) => `${(x * cos - y * sin).toFixed(2)},${(x * sin + y * cos).toFixed(2)}`).join(" ");
}

interface CheckersBoardProps {
  geometry: Geometry;
  /** The round on the board; null before the first one. */
  state: CheckersState | null;
  /** Each player's color, by player id. */
  colors: Map<number, ShirtColor>;
  /** Whose side of the board is nearest: their point is drawn at the bottom. */
  viewer: number | null;
  /** The move the viewer is building: the marble's hole, then each landing. */
  path: number[];
  /** Where the marble being moved can go next. */
  options: number[];
  /** The viewer's marbles that can be picked up right now. */
  movable: number[];
  /** A hole that can be tapped was tapped; null for a tap on nothing. */
  onTap?: (hole: number | null) => void;
  /** Told while a move is being played back, so nothing else happens over it. */
  onBusy?: (busy: boolean) => void;
}

/**
 * The star board, drawn whole in whatever space it is given.
 *
 * Whenever the round's last move changes, the marble is shown hopping
 * along the path it took, on every phone.
 */
export function CheckersBoard({ geometry, state, colors, viewer, path, options, movable, onTap, onBusy }: CheckersBoardProps) {
  const uid = useId().replace(/:/g, "");
  const order = state?.turn_order ?? [];
  const seat = viewer === null ? -1 : order.indexOf(viewer);
  const home = seat >= 0 && state ? state.homes[seat] : null;
  const turns = home === null ? 0 : (3 - home + 6) % 6;

  const at = useMemo(
    () => turnedTowards(geometry, home).map(([x, y]) => [x * UNIT, y * UNIT] as [number, number]),
    [geometry, home]
  );

  // ---- Playing back the last move ---------------------------------------

  const round = state?.round ?? null;
  const seq = state?.last_move?.seq ?? 0;
  const lastMove = state?.last_move ?? null;
  const [playing, setPlaying] = useState<CheckersMove | null>(null);
  const seen = useRef<{ round: number | null; seq: number } | null>(null);
  const latest = useRef(lastMove);
  latest.current = lastMove;
  // Before the browser paints the new position, so the marble is never
  // seen sitting where it is about to hop to.
  useLayoutEffect(() => {
    const before = seen.current;
    seen.current = { round, seq };
    // Nothing to show for the position this phone opened on, or a new round.
    const fresh = before !== null && before.round === round && seq > before.seq;
    setPlaying(fresh ? latest.current : null);
  }, [round, seq]);

  const flying = useRef<SVGGElement>(null);
  const shadow = useRef<SVGEllipseElement>(null);
  const busy = useRef(onBusy);
  busy.current = onBusy;
  useEffect(() => {
    const marble = flying.current;
    if (!playing || !marble) return;
    const stops = playing.path.map((hole) => at[hole]);
    const hops = stops.length - 1;
    if (hops < 1) {
      setPlaying(null);
      return;
    }
    const [ax, ay] = stops[0];
    const [bx, by] = stops[1];
    const isStep = hops === 1 && Math.hypot(bx - ax, by - ay) < 1.5 * UNIT;
    const each = isStep ? STEP : HOP;
    const length = hops * each + (hops - 1) * REST;
    const still = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;

    const place = (x: number, y: number, lift: number) => {
      marble.setAttribute(
        "transform",
        `translate(${x.toFixed(2)} ${(y - lift * 3.2).toFixed(2)}) scale(${(1 + lift * 0.34).toFixed(3)})`
      );
      shadow.current?.setAttribute(
        "transform",
        `translate(${(x + 0.5 + lift * 2).toFixed(2)} ${(y + 1.1 + lift * 3.4).toFixed(2)}) scale(${(1 + lift * 0.25).toFixed(3)})`
      );
      shadow.current?.setAttribute("opacity", (0.4 - lift * 0.2).toFixed(2));
    };

    let frame = 0;
    let over = false;
    const began = performance.now();
    busy.current?.(true);
    const finish = () => {
      over = true;
      setPlaying(null);
      busy.current?.(false);
    };
    const tick = (now: number) => {
      const t = now - began - LEAD;
      if (still) {
        place(stops[hops][0], stops[hops][1], 0);
        if (now - began > 320) return finish();
      } else if (t >= length + TAIL) {
        return finish();
      } else if (t > 0) {
        const hop = Math.min(hops - 1, Math.floor(t / (each + REST)));
        const part = Math.min(1, (t - hop * (each + REST)) / each);
        // Ease in and out along the ground; up and down on a sine.
        const eased = part < 0.5 ? 2 * part * part : 1 - Math.pow(-2 * part + 2, 2) / 2;
        const [fx, fy] = stops[hop];
        const [tx, ty] = stops[hop + 1];
        place(fx + (tx - fx) * eased, fy + (ty - fy) * eased, Math.sin(Math.PI * part) * (isStep ? 0.4 : 1));
      }
      frame = requestAnimationFrame(tick);
    };
    place(ax, ay, 0);
    frame = requestAnimationFrame(tick);
    return () => {
      cancelAnimationFrame(frame);
      if (!over) busy.current?.(false);
    };
  }, [playing, at]);

  // ---- What is where ----------------------------------------------------

  const marbles = state?.marbles ?? [];
  const picked = path.length > 0 ? path[0] : -1;
  const pickedAt = path.length > 0 ? path[path.length - 1] : -1;
  const landing = playing ? playing.path[playing.path.length - 1] : -1;
  const optionSet = useMemo(() => new Set(options), [options]);
  const movableSet = useMemo(() => new Set(movable), [movable]);

  const shade = (player: number | null | undefined) =>
    SHIRT_COLORS[(player != null && colors.get(player)) || "red"] ?? SHIRT_COLORS.red;
  const gradient = (player: number | null | undefined) =>
    `url(#${uid}-${(player != null && colors.get(player)) || "red"})`;
  const mine = shade(viewer);
  const target = home === null ? -1 : opposite(home);
  const winner = state?.winner_player_id ?? null;
  const winnerSeat = winner === null ? -1 : order.indexOf(winner);
  const winnerPoint = winnerSeat >= 0 && state ? opposite(state.homes[winnerSeat]) : -1;

  const trail = (holes: number[]) => holes.map((hole) => `${at[hole][0].toFixed(2)},${at[hole][1].toFixed(2)}`).join(" ");
  // The move just made stays marked until the next one, unless the
  // viewer is busy building their own.
  const shownMove = path.length === 0 && lastMove && state && !state.is_draw ? lastMove : null;

  const tap = (event: MouseEvent<SVGSVGElement>) => {
    if (!onTap) return;
    const matrix = event.currentTarget.getScreenCTM();
    if (!matrix) return;
    const point = new DOMPoint(event.clientX, event.clientY).matrixTransform(matrix.inverse());
    let nearest: number | null = null;
    let distance = REACH;
    const consider = (hole: number, weight: number) => {
      const away = Math.hypot(at[hole][0] - point.x, at[hole][1] - point.y) * weight;
      if (away < distance) {
        distance = away;
        nearest = hole;
      }
    };
    // Where the marble can go wins a close call over picking another one.
    options.forEach((hole) => consider(hole, 0.8));
    path.forEach((hole) => consider(hole, 1));
    movable.forEach((hole) => consider(hole, 1));
    onTap(nearest);
  };

  return (
    <svg
      viewBox={`${-HALF_WIDTH} ${-HALF_HEIGHT} ${2 * HALF_WIDTH} ${2 * HALF_HEIGHT}`}
      className={movable.length > 0 || path.length > 0 ? "cc-board cc-board-live h-full w-full" : "cc-board h-full w-full"}
      role="img"
      aria-label="Chinese Checkers board"
      onClick={tap}
    >
      <defs>
        {SHIRT_COLOR_KEYS.map((color) => (
          <radialGradient key={color} id={`${uid}-${color}`} cx="0.36" cy="0.3" r="0.75">
            {marbleStops(color)}
          </radialGradient>
        ))}
        <linearGradient id={`${uid}-wood`} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#7b5230" />
          <stop offset="0.55" stopColor="#5d3b20" />
          <stop offset="1" stopColor="#452a15" />
        </linearGradient>
        <radialGradient id={`${uid}-pit`} cx="0.5" cy="0.62" r="0.6">
          <stop offset="0" stopColor="#3a2413" />
          <stop offset="1" stopColor="#1c0f06" />
        </radialGradient>
        <clipPath id={`${uid}-star`}>
          <polygon points={STAR} />
        </clipPath>
      </defs>

      {/* The board: a dark rim, the wood, and each player's point in their color. */}
      <polygon points={STAR} fill="#24150a" stroke="#24150a" strokeWidth={RIM + 1.6} strokeLinejoin="round" />
      <polygon
        points={STAR}
        fill={`url(#${uid}-wood)`}
        stroke={`url(#${uid}-wood)`}
        strokeWidth={RIM - 1.4}
        strokeLinejoin="round"
      />
      <g clipPath={`url(#${uid}-star)`}>
        {order.map((player, i) =>
          state ? (
            <polygon
              key={player}
              points={pointTriangle(state.homes[i], turns)}
              fill={shade(player).main}
              opacity={0.36}
            />
          ) : null
        )}
      </g>
      <polygon points={STAR} fill="none" stroke="rgba(255,236,200,0.13)" strokeWidth={0.7} strokeLinejoin="round" />

      {at.map(([x, y], hole) => (
        <circle
          key={hole}
          data-hole={hole}
          data-option={optionSet.has(hole) ? "" : undefined}
          data-movable={movableSet.has(hole) ? "" : undefined}
          cx={x}
          cy={y}
          r={HOLE}
          fill={`url(#${uid}-pit)`}
          stroke={geometry.pointOf[hole] === target ? mine.light : "rgba(0,0,0,0.35)"}
          strokeWidth={geometry.pointOf[hole] === target ? 0.8 : 0.5}
        />
      ))}

      {shownMove && (
        <g pointerEvents="none">
          <polyline
            points={trail(shownMove.path)}
            fill="none"
            stroke={shade(shownMove.player_id).light}
            strokeWidth={1.5}
            strokeDasharray="0.1 2.7"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
          <circle
            cx={at[shownMove.path[0]][0]}
            cy={at[shownMove.path[0]][1]}
            r={HOLE + 0.4}
            fill="none"
            stroke={shade(shownMove.player_id).light}
            strokeWidth={0.9}
          />
        </g>
      )}

      {path.length > 0 && (
        <g pointerEvents="none">
          <polyline
            points={trail(path)}
            fill="none"
            stroke={mine.light}
            strokeWidth={1.7}
            strokeLinecap="round"
            strokeLinejoin="round"
          />
          {path.slice(0, -1).map((hole, i) => (
            <circle
              key={hole}
              cx={at[hole][0]}
              cy={at[hole][1]}
              r={i === 0 ? MARBLE - 0.3 : 1.5}
              fill={i === 0 ? "none" : mine.light}
              stroke={mine.light}
              strokeWidth={i === 0 ? 0.9 : 0}
              strokeDasharray={i === 0 ? "1.6 1.3" : undefined}
            />
          ))}
        </g>
      )}

      {/* Marbles, except one being moved or played back: those are drawn on top. */}
      {marbles.map((owner, hole) => {
        if (owner === null || hole === picked || hole === landing) return null;
        const cheering = owner === winner && geometry.pointOf[hole] === winnerPoint;
        return (
          <g key={hole} transform={`translate(${at[hole][0].toFixed(2)} ${at[hole][1].toFixed(2)})`} pointerEvents="none">
            {picked < 0 && movableSet.has(hole) && <circle r={MARBLE + 1.1} className="cc-can-move" />}
            {shownMove && !playing && hole === shownMove.path[shownMove.path.length - 1] && (
              <circle r={MARBLE + 1} fill="none" stroke="rgba(255,255,255,0.55)" strokeWidth={0.6} />
            )}
            <g
              className={cheering ? "cc-cheer" : undefined}
              style={cheering ? ({ animationDelay: `${(hole % 10) * 70}ms` } as CSSProperties) : undefined}
            >
              <MarbleShape fill={gradient(owner)} edge={shade(owner).dark} />
            </g>
          </g>
        );
      })}

      {options.map((hole) => (
        <g key={hole} transform={`translate(${at[hole][0].toFixed(2)} ${at[hole][1].toFixed(2)})`} pointerEvents="none">
          <circle r={MARBLE + 0.5} className="cc-option" fill={mine.main} stroke="#fff" strokeWidth={1} />
          <circle r={1.2} fill="#fff" opacity={0.9} />
        </g>
      ))}

      {/* The marble the viewer is moving, wherever it has got to so far. */}
      {picked >= 0 && viewer !== null && (
        <g
          className="cc-picked"
          pointerEvents="none"
          style={{ transform: `translate(${at[pickedAt][0].toFixed(2)}px, ${at[pickedAt][1].toFixed(2)}px)` }}
        >
          <ellipse cx={1.2} cy={2.6} rx={MARBLE} ry={MARBLE - 0.5} fill="rgba(0,0,0,0.35)" />
          <g transform="translate(0 -1) scale(1.16)">
            <MarbleShape fill={gradient(viewer)} edge={shade(viewer).dark} bare />
            <circle r={MARBLE + 1} className="cc-picked-ring" />
          </g>
        </g>
      )}

      {playing && (
        <g pointerEvents="none">
          <ellipse
            ref={shadow}
            rx={MARBLE - 0.2}
            ry={MARBLE - 0.6}
            fill="#000"
            opacity={0.4}
            transform={`translate(${at[playing.path[0]][0].toFixed(2)} ${at[playing.path[0]][1].toFixed(2)})`}
          />
          <g ref={flying} className="cc-flying" transform={`translate(${at[playing.path[0]][0].toFixed(2)} ${at[playing.path[0]][1].toFixed(2)})`}>
            <MarbleShape fill={gradient(playing.player_id)} edge={shade(playing.player_id).dark} bare />
          </g>
        </g>
      )}
    </svg>
  );
}

/** A glass marble centred on the origin. `bare` leaves out its shadow. */
function MarbleShape({ fill, edge, bare }: { fill: string; edge: string; bare?: boolean }) {
  return (
    <>
      {!bare && <ellipse cx={0.5} cy={1.1} rx={MARBLE - 0.2} ry={MARBLE - 0.6} fill="rgba(0,0,0,0.4)" />}
      <circle r={MARBLE} fill={fill} stroke={edge} strokeWidth={0.45} />
      <ellipse cx={-1.4} cy={-1.75} rx={1.3} ry={0.8} fill="#fff" opacity={0.72} transform="rotate(-32 -1.4 -1.75)" />
    </>
  );
}

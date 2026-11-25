import { useId } from "react";
import type { ShirtColor, TurkeyStyle } from "./types";

// The cartoon turkeys players pick from. Every turkey is the same bird
// underneath — tail fan, body, head — wearing a shirt in the player's
// colour; the style changes the face, the arms and what it's holding.
// Drawn in a 120 x 120 box so they stay sharp at any size.

export const TURKEY_STYLES: { key: TurkeyStyle; label: string }[] = [
  { key: "happy", label: "Happy" },
  { key: "strong", label: "Strong" },
  { key: "girl", label: "Glam" },
  { key: "cheerleader", label: "Cheerleader" },
  { key: "hippie", label: "Hippie" },
  { key: "beer", label: "Cheers" },
];

interface Shirt {
  label: string;
  main: string;
  dark: string;
  light: string;
  /** A wash of the colour, for the square a turkey is standing on. */
  pale: string;
}

export const SHIRT_COLORS: Record<ShirtColor, Shirt> = {
  red: { label: "Red", main: "#e03131", dark: "#a51111", light: "#ff8787", pale: "#ffd6d6" },
  orange: { label: "Orange", main: "#f76707", dark: "#b84a00", light: "#ffa94d", pale: "#ffdcc0" },
  gold: { label: "Gold", main: "#f5b800", dark: "#a87900", light: "#ffe066", pale: "#fff0b0" },
  green: { label: "Green", main: "#2f9e44", dark: "#1b6e2d", light: "#8ce99a", pale: "#d0f2d6" },
  teal: { label: "Teal", main: "#0fa3a3", dark: "#0b6b73", light: "#66d9e8", pale: "#c6eff2" },
  blue: { label: "Blue", main: "#1c7ed6", dark: "#124f8f", light: "#74c0fc", pale: "#cfe6ff" },
  purple: { label: "Purple", main: "#7b3fe4", dark: "#4f1fa8", light: "#b197fc", pale: "#e2d6ff" },
  pink: { label: "Pink", main: "#e64999", dark: "#a8246a", light: "#fcc2d7", pale: "#ffd6e8" },
};

export const SHIRT_COLOR_KEYS = Object.keys(SHIRT_COLORS) as ShirtColor[];

const OUTLINE = "#4a2c14";
const BODY = "#8a5a33";
const WING = "#7a4d2a";
const HEAD = "#a8703f";
const BEAK = "#f7b733";
const LEG = "#f08c00";
const FEATHERS = ["#c2410c", "#f59f00", "#e8590c", "#c92a2a", "#e8590c", "#f59f00", "#c2410c"];
const FEATHER_ANGLES = [-66, -44, -22, 0, 22, 44, 66];

interface TurkeyProps {
  style: TurkeyStyle;
  color: ShirtColor;
  /** Width and height in pixels; omit to size with CSS. */
  size?: number;
  className?: string;
  title?: string;
}

export function Turkey({ style, color, size, className, title }: TurkeyProps) {
  const clipId = `turkey-body-${useId().replace(/:/g, "")}`;
  const shirt = SHIRT_COLORS[color] ?? SHIRT_COLORS.red;
  const skirted = style === "girl" || style === "cheerleader";
  return (
    <svg
      viewBox="0 0 120 120"
      width={size}
      height={size}
      className={className}
      role="img"
      aria-label={title ?? `${shirt.label} ${style} turkey`}
    >
      <defs>
        <clipPath id={clipId}>
          <ellipse cx="60" cy="84" rx="25" ry="21" />
        </clipPath>
      </defs>

      {/* Tail fan */}
      {FEATHER_ANGLES.map((angle, i) => (
        <g key={angle} transform={`rotate(${angle} 60 84)`}>
          <ellipse cx="60" cy="50" rx="10" ry="28" fill={FEATHERS[i]} stroke={OUTLINE} strokeWidth="1.5" />
          <ellipse cx="60" cy="32" rx="5" ry="6.5" fill="#ffe8a3" opacity="0.9" />
        </g>
      ))}

      {/* Legs */}
      <g stroke={LEG} strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" fill="none">
        <path d="M52 102 V114 M46 117 L52 114 L58 117" />
        <path d="M68 102 V114 M62 117 L68 114 L74 117" />
      </g>

      {/* Body, in its shirt */}
      <ellipse cx="60" cy="84" rx="25" ry="21" fill={BODY} />
      <g clipPath={`url(#${clipId})`}>
        <path d="M30 77 Q60 66 90 77 V110 H30 Z" fill={shirt.main} />
        {style === "hippie" && (
          <g fill="none" stroke={shirt.light} strokeWidth="2.4" opacity="0.75">
            <circle cx="60" cy="90" r="11.5" />
            <circle cx="60" cy="90" r="18" />
            <circle cx="60" cy="90" r="24.5" />
          </g>
        )}
        {!skirted && <rect x="30" y="99" width="60" height="12" fill={shirt.dark} />}
      </g>
      <ellipse cx="60" cy="84" rx="25" ry="21" fill="none" stroke={OUTLINE} strokeWidth="1.8" />

      <ShirtDetail style={style} shirt={shirt} />
      <Arms style={style} shirt={shirt} />

      {/* Neck and head */}
      <path d="M53 56 H67 L69.5 71 Q60 76 50.5 71 Z" fill={HEAD} />
      <path d="M50.5 71 Q60 76 69.5 71" fill="none" stroke={OUTLINE} strokeWidth="1.2" opacity="0.5" />
      <circle cx="60" cy="44" r="17" fill={HEAD} stroke={OUTLINE} strokeWidth="1.8" />

      <Face style={style} shirt={shirt} />
      <Extras style={style} shirt={shirt} />
    </svg>
  );
}

interface PartProps {
  style: TurkeyStyle;
  shirt: Shirt;
}

function ShirtDetail({ style, shirt }: PartProps) {
  switch (style) {
    case "happy":
      return (
        <path
          d="M60 85.5 c-2.2 -4.2 -8.5 -3.2 -8.5 1.6 c0 4.2 5.2 6.8 8.5 9.6 c3.3 -2.8 8.5 -5.4 8.5 -9.6 c0 -4.8 -6.3 -5.8 -8.5 -1.6 z"
          fill="#fff"
          opacity="0.95"
        />
      );
    case "strong":
      return (
        <g fill="#fff">
          <rect x="52" y="87.6" width="16" height="3" rx="1" />
          <rect x="48.5" y="83.6" width="4.5" height="11" rx="1.4" />
          <rect x="67" y="83.6" width="4.5" height="11" rx="1.4" />
        </g>
      );
    case "girl":
      return (
        <>
          <path
            d="M37 94 Q60 101 83 94 L91 107 Q83.3 103 75.5 107 Q67.8 103 60 107 Q52.2 103 44.5 107 Q36.7 103 29 107 Z"
            fill={shirt.main}
            stroke={shirt.dark}
            strokeWidth="1.5"
            strokeLinejoin="round"
          />
          <path d="M39.5 96.5 Q60 103 80.5 96.5" fill="none" stroke={shirt.light} strokeWidth="1.6" />
        </>
      );
    case "cheerleader":
      return (
        <>
          <path
            d="M38 93 H82 L92 107 H28 Z"
            fill={shirt.main}
            stroke={shirt.dark}
            strokeWidth="1.5"
            strokeLinejoin="round"
          />
          <path d="M47 93 L41 107 M60 93 V107 M73 93 L79 107" stroke={shirt.dark} strokeWidth="1.2" />
          <path d="M30.8 103.2 H89.2" stroke="#fff" strokeWidth="2.2" />
          <g fill="#fff">
            <rect x="53.5" y="79" width="13" height="3.4" rx="0.8" />
            <rect x="58.3" y="79" width="3.4" height="11.5" rx="0.8" />
          </g>
        </>
      );
    case "hippie":
      return (
        <g fill="none" stroke="#fff" strokeWidth="1.7" strokeLinecap="round">
          <path d="M53 73 L57 83.5 M67 73 L63 83.5" stroke={OUTLINE} strokeWidth="0.9" />
          <circle cx="60" cy="90" r="6.8" fill={shirt.dark} />
          <path d="M60 83.2 V96.8 M60 90 L55.3 94.9 M60 90 L64.7 94.9" />
        </g>
      );
    default:
      return null;
  }
}

function Wing({ cx, cy, rotate, rx = 7.5, ry = 13 }: { cx: number; cy: number; rotate: number; rx?: number; ry?: number }) {
  return (
    <ellipse
      cx={cx}
      cy={cy}
      rx={rx}
      ry={ry}
      transform={`rotate(${rotate} ${cx} ${cy})`}
      fill={WING}
      stroke={OUTLINE}
      strokeWidth="1.6"
    />
  );
}

function FlexedArm({ flip }: { flip?: boolean }) {
  return (
    <g
      transform={flip ? "translate(120 0) scale(-1 1)" : undefined}
      fill={WING}
      stroke={OUTLINE}
      strokeWidth="1.6"
    >
      <ellipse cx="29" cy="77" rx="11.5" ry="6.5" />
      <ellipse cx="19.5" cy="65" rx="6" ry="12.5" />
      <circle cx="30.5" cy="71.5" r="6.8" />
      <circle cx="19.5" cy="52.5" r="6" />
      <path d="M15.5 51 H23.5" strokeWidth="1" opacity="0.6" />
    </g>
  );
}

function starPoints(cx: number, cy: number, spikes: number, outer: number, inner: number, turn = 0): string {
  const points: string[] = [];
  for (let i = 0; i < spikes * 2; i++) {
    const radius = i % 2 === 0 ? outer : inner;
    const angle = (Math.PI * i) / spikes + turn;
    points.push(`${(cx + radius * Math.cos(angle)).toFixed(1)},${(cy + radius * Math.sin(angle)).toFixed(1)}`);
  }
  return points.join(" ");
}

function PomPom({ cx, cy, shirt }: { cx: number; cy: number; shirt: Shirt }) {
  return (
    <>
      <polygon
        points={starPoints(cx, cy, 11, 12.5, 7)}
        fill={shirt.main}
        stroke={shirt.dark}
        strokeWidth="1.2"
        strokeLinejoin="round"
      />
      <polygon points={starPoints(cx, cy, 9, 7, 3.5, 0.3)} fill="#fff" opacity="0.92" />
    </>
  );
}

function Arms({ style, shirt }: PartProps) {
  switch (style) {
    case "happy":
      return (
        <>
          <Wing cx={29} cy={67} rotate={-42} />
          <path
            d="M14 55 Q11.5 60 13.5 65 M19 49.5 Q14.5 50.5 12 54"
            fill="none"
            stroke={OUTLINE}
            strokeWidth="1.5"
            strokeLinecap="round"
            opacity="0.7"
          />
          <Wing cx={85} cy={86} rotate={-18} />
        </>
      );
    case "strong":
      return (
        <>
          <FlexedArm />
          <FlexedArm flip />
        </>
      );
    case "cheerleader":
      return (
        <>
          <Wing cx={28} cy={64} rotate={-40} rx={6.5} ry={14.5} />
          <Wing cx={92} cy={64} rotate={40} rx={6.5} ry={14.5} />
          <PomPom cx={18} cy={50} shirt={shirt} />
          <PomPom cx={102} cy={50} shirt={shirt} />
        </>
      );
    case "beer":
      return (
        <>
          <Wing cx={35} cy={86} rotate={18} />
          <Wing cx={90} cy={73} rotate={58} rx={6.5} ry={12.5} />
        </>
      );
    default:
      return (
        <>
          <Wing cx={35} cy={86} rotate={18} />
          <Wing cx={85} cy={86} rotate={-18} />
        </>
      );
  }
}

function Eye({ cx, lookDown }: { cx: number; lookDown?: boolean }) {
  // Pupils sit a little toward the beak, which reads as friendly.
  const px = cx + (cx < 60 ? 1 : -1);
  const py = lookDown ? 43.2 : 41.8;
  return (
    <>
      <circle cx={cx} cy="41" r="5.2" fill={lookDown ? "#ffdede" : "#fff"} stroke={OUTLINE} strokeWidth="1.2" />
      <circle cx={px} cy={py} r={lookDown ? 1.9 : 2.5} fill="#1f1308" />
      {!lookDown && <circle cx={px + 0.9} cy={py - 0.9} r="0.8" fill="#fff" />}
    </>
  );
}

function Lashes({ flip }: { flip?: boolean }) {
  return (
    <path
      d="M48.6 38 L45.8 35.4 M50.4 36.3 L48.6 33 M53 35.7 L52.4 32.3"
      transform={flip ? "translate(120 0) scale(-1 1)" : undefined}
      stroke={OUTLINE}
      strokeWidth="1.5"
      strokeLinecap="round"
      fill="none"
    />
  );
}

function Cheeks({ color = "#ff8fa3", opacity = 0.6 }: { color?: string; opacity?: number }) {
  return (
    <g fill={color} opacity={opacity}>
      <ellipse cx="46.5" cy="49.5" rx="3.4" ry="2.4" />
      <ellipse cx="73.5" cy="49.5" rx="3.4" ry="2.4" />
    </g>
  );
}

function Bow({ shirt, transform }: { shirt: Shirt; transform?: string }) {
  return (
    <g transform={transform} fill={shirt.main} stroke={shirt.dark} strokeWidth="1.3" strokeLinejoin="round">
      <path d="M60 27.5 L47.5 20.5 Q45.5 27.5 47.5 34.5 Z" />
      <path d="M60 27.5 L72.5 20.5 Q74.5 27.5 72.5 34.5 Z" />
      <circle cx="60" cy="27.5" r="3.6" fill={shirt.light} />
    </g>
  );
}

function Face({ style, shirt }: PartProps) {
  const smile =
    style === "strong" ? "M51 54 Q60 62.5 69 54" : style === "hippie" ? "M50 54 Q60 63 70 53" : "M48.5 52 Q60 67 71.5 52";
  return (
    <>
      {style === "beer" ? (
        <path
          d="M48 42.5 Q53 36.5 58 42.5 M62 42.5 Q67 36.5 72 42.5"
          fill="none"
          stroke={OUTLINE}
          strokeWidth="2.2"
          strokeLinecap="round"
        />
      ) : (
        <>
          <Eye cx={53} lookDown={style === "hippie"} />
          <Eye cx={67} lookDown={style === "hippie"} />
        </>
      )}

      {style === "hippie" && (
        // Heavy eyelids.
        <g fill="#8c5b31" stroke={OUTLINE} strokeWidth="1.2" strokeLinejoin="round">
          <path d="M47.6 41.6 A5.4 5.4 0 0 1 58.4 41.6 Z" />
          <path d="M61.6 41.6 A5.4 5.4 0 0 1 72.4 41.6 Z" />
        </g>
      )}
      {style === "strong" && (
        <path
          d="M45.5 33.5 L57 38 M74.5 33.5 L63 38"
          stroke={OUTLINE}
          strokeWidth="2.8"
          strokeLinecap="round"
        />
      )}
      {(style === "girl" || style === "cheerleader") && (
        <>
          <Lashes />
          <Lashes flip />
        </>
      )}

      {style === "happy" && <Cheeks />}
      {(style === "girl" || style === "cheerleader") && <Cheeks color="#ff6b9d" opacity={0.7} />}
      {style === "beer" && <Cheeks color="#ff5c5c" opacity={0.75} />}

      <path
        d={smile}
        fill="none"
        stroke={style === "girl" ? "#c2255c" : OUTLINE}
        strokeWidth={style === "girl" ? 2.2 : 1.8}
        strokeLinecap="round"
      />
      <path d="M54.5 48 Q60 45.5 65.5 48 L60 57.5 Z" fill={BEAK} stroke="#a8670a" strokeWidth="1.2" strokeLinejoin="round" />
      <path
        d="M62.6 52 C68.5 53 68.5 62.5 64.6 63.5 C61.4 63.5 61 57 62.6 52 Z"
        fill="#e03131"
        stroke="#a51111"
        strokeWidth="0.9"
      />

      {style === "girl" && <Bow shirt={shirt} />}
      {style === "cheerleader" && <Bow shirt={shirt} transform="rotate(24 60 44) translate(0 -1)" />}
    </>
  );
}

function Extras({ style, shirt }: PartProps) {
  switch (style) {
    case "strong":
      // Sweatband, knotted at the side.
      return (
        <g fill={shirt.main} stroke={shirt.dark} strokeWidth="1.1" strokeLinejoin="round">
          <path d="M76 31.5 L84 28 L82.5 33.5 L85.5 37.5 L77 35.5 Z" />
          <path d="M44.4 35.5 Q60 28.5 75.6 35.5 L74 30.7 Q60 24 46 30.7 Z" />
        </g>
      );
    case "girl":
      // Pearls.
      return (
        <g fill="#fff" stroke="#b8b8b8" strokeWidth="0.5">
          {[
            [51.5, 70.3],
            [55.4, 72.7],
            [60, 73.6],
            [64.6, 72.7],
            [68.5, 70.3],
          ].map(([cx, cy]) => (
            <circle key={cx} cx={cx} cy={cy} r="1.8" />
          ))}
        </g>
      );
    case "hippie":
      return (
        <>
          <path
            d="M44.4 35.5 Q60 28.5 75.6 35.5 L74 30.7 Q60 24 46 30.7 Z"
            fill="#2f9e44"
            stroke="#1b6e2d"
            strokeWidth="1.1"
            strokeLinejoin="round"
          />
          <circle cx="51" cy="30.9" r="1.7" fill="#ffd43b" />
          <circle cx="60" cy="28.9" r="1.7" fill="#fa5252" />
          <circle cx="69" cy="30.9" r="1.7" fill="#ffd43b" />
          {/* Hand-rolled, lit, with a curl of smoke. */}
          <g transform="rotate(-22 55 56)">
            <rect x="40.5" y="54.3" width="14.5" height="3.4" rx="1.3" fill="#fffdf2" stroke="#a39b82" strokeWidth="0.8" />
            <rect x="40.5" y="54.3" width="3" height="3.4" rx="1" fill="#ff6b1a" />
          </g>
          <g className="turkey-smoke" fill="none" stroke="#6b7280" strokeWidth="2" strokeLinecap="round" opacity="0.8">
            <path d="M40.5 59 C35.5 55 43 51.5 38 47 C33.5 43 39.5 39.5 36 35" />
            <path d="M36.5 61 C31 58 35.5 53.5 31 50" opacity="0.7" />
          </g>
        </>
      );
    case "beer":
      return (
        <g strokeLinejoin="round">
          <path d="M107.5 55 q7.5 0 7.5 6.5 q0 6.5 -7.5 6.5" fill="none" stroke="#7a4a00" strokeWidth="2.6" />
          <rect x="92" y="50" width="16" height="21" rx="2.2" fill="#f5a700" stroke="#7a4a00" strokeWidth="1.5" />
          <rect x="95" y="56" width="2.6" height="11.5" rx="1.3" fill="#ffd866" />
          <rect x="100.2" y="56" width="2.6" height="11.5" rx="1.3" fill="#ffd866" />
          <g fill="#fff" stroke="#c9c9c9" strokeWidth="0.8">
            <circle cx="94.2" cy="50" r="4.2" />
            <circle cx="105.8" cy="50" r="4.2" />
            <circle cx="100" cy="47.8" r="5" />
            <path d="M92.2 51 q-1.3 5 0.8 7.5 q2 -2 1.4 -7.5 z" />
          </g>
        </g>
      );
    default:
      return null;
  }
}

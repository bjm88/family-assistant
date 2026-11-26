import { useId } from "react";
import { SHIRT_COLORS } from "../Turkey";
import type { ShirtColor } from "../types";

interface MarbleProps {
  color: ShirtColor;
  /** Width and height in pixels; omit to size with CSS. */
  size?: number;
  className?: string;
  title?: string;
}

/** The three stops of a glass marble lit from the top left. */
export function marbleStops(color: ShirtColor) {
  const shade = SHIRT_COLORS[color] ?? SHIRT_COLORS.red;
  return (
    <>
      <stop offset="0" stopColor={shade.pale} />
      <stop offset="0.38" stopColor={shade.main} />
      <stop offset="1" stopColor={shade.dark} />
    </>
  );
}

/** A player in Chinese Checkers: a glass marble in their color. */
export function Marble({ color, size, className, title }: MarbleProps) {
  const id = useId();
  const shade = SHIRT_COLORS[color] ?? SHIRT_COLORS.red;
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      className={className}
      role="img"
      aria-label={title ?? `${shade.label} marble`}
    >
      <defs>
        <radialGradient id={id} cx="0.36" cy="0.3" r="0.75">
          {marbleStops(color)}
        </radialGradient>
      </defs>
      <ellipse cx="12.4" cy="21.6" rx="7.5" ry="1.7" fill="rgba(0,0,0,0.28)" />
      <circle cx="12" cy="11.5" r="10" fill={`url(#${id})`} stroke={shade.dark} strokeWidth="0.7" />
      <ellipse cx="8.6" cy="7.4" rx="3.1" ry="2" fill="#fff" opacity="0.7" transform="rotate(-32 8.6 7.4)" />
    </svg>
  );
}

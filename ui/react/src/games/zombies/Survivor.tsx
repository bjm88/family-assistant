import { SHIRT_COLORS } from "../Turkey";
import type { ShirtColor, SurvivorStyle } from "../types";
import { HELMET, HELMET_DARK, SURVIVOR_BY_KEY } from "./survivors";

// Head-and-shoulders portraits of the characters, in the player's
// shirt colour. Drawn in a 120 x 120 box so they stay sharp at any size.

const OUTLINE = "#1d232b";

interface SurvivorProps {
  style: SurvivorStyle;
  color: ShirtColor;
  /** Width and height in pixels; omit to size with CSS. */
  size?: number;
  className?: string;
  title?: string;
}

export function Survivor({ style, color, size, className, title }: SurvivorProps) {
  const look = SURVIVOR_BY_KEY[style] ?? SURVIVOR_BY_KEY.dad;
  const shirt = SHIRT_COLORS[color] ?? SHIRT_COLORS.red;
  const army = style === "army_guy" || style === "army_girl";
  const kid = style === "girl" || style === "boy";
  const wide = style === "army_guy" ? 8 : 0;
  const stroke = { stroke: OUTLINE, strokeWidth: 2.5, strokeLinejoin: "round" as const };
  const thin = { stroke: OUTLINE, strokeWidth: 2, strokeLinecap: "round" as const, fill: "none" };

  return (
    <svg
      viewBox="0 0 120 120"
      width={size}
      height={size}
      className={className}
      role="img"
      aria-label={title ?? `${look.label} in ${shirt.label.toLowerCase()}`}
    >
      <g transform={kid ? "translate(9 18) scale(0.85)" : undefined}>
        {/* Hair that hangs behind the head */}
        {look.head === "long" && (
          <path d="M33 52 C30 22 90 22 87 52 L90 92 C80 98 40 98 30 92 Z" fill={look.hair} {...stroke} />
        )}
        {look.head === "helmet_ponytail" && (
          <path d="M84 50 C102 50 104 76 94 92 C92 78 88 68 82 62 Z" fill={look.hair} {...stroke} />
        )}
        {look.head === "pigtails" && (
          <>
            <ellipse cx="27" cy="60" rx="9" ry="13" fill={look.hair} {...stroke} />
            <ellipse cx="93" cy="60" rx="9" ry="13" fill={look.hair} {...stroke} />
            <circle cx="33" cy="48" r="4.5" fill={shirt.light} {...stroke} strokeWidth={2} />
            <circle cx="87" cy="48" r="4.5" fill={shirt.light} {...stroke} strokeWidth={2} />
          </>
        )}
        {look.head === "wild" && (
          <>
            <path d="M36 50 L18 40 L30 52 L14 56 L30 60 L18 70 L37 66 Z" fill={look.hair} {...stroke} />
            <path d="M84 50 L102 40 L90 52 L106 56 L90 60 L102 70 L83 66 Z" fill={look.hair} {...stroke} />
          </>
        )}
        {look.head === "bun" && (
          <>
            <circle cx="60" cy="19" r="11" fill={look.hair} {...stroke} />
            <path d="M52 12 L70 26 M68 11 L53 27" {...thin} stroke="#8a5a33" strokeWidth={2.5} />
          </>
        )}

        {/* Shoulders */}
        <path
          d={`M${14 - wide} 122 C${14 - wide} 98 34 90 50 88 L70 88 C86 90 ${106 + wide} 98 ${106 + wide} 122 Z`}
          fill={shirt.main}
          {...stroke}
        />
        {army && (
          <g fill={shirt.dark} opacity="0.55">
            <ellipse cx="30" cy="108" rx="9" ry="5" />
            <ellipse cx="86" cy="102" rx="8" ry="5" />
            <ellipse cx="58" cy="114" rx="10" ry="4" />
          </g>
        )}
        {style === "dad" && (
          <path d="M48 88 L60 102 L72 88 L66 86 L60 94 L54 86 Z" fill={shirt.light} {...stroke} strokeWidth={2} />
        )}
        {style === "mom" && <path d="M48 89 C54 100 66 100 72 89" {...thin} stroke={shirt.dark} strokeWidth={3} />}
        {style === "uncle" && (
          <g fill={shirt.light} stroke={shirt.dark} strokeWidth="1">
            <circle cx="30" cy="106" r="4" />
            <circle cx="88" cy="110" r="4" />
            <circle cx="62" cy="112" r="3.5" />
            <circle cx="46" cy="98" r="3" />
            <circle cx="78" cy="97" r="3" />
          </g>
        )}
        {style === "grandma" && (
          <>
            <path d="M22 100 C40 92 80 92 98 100 L92 110 C74 102 46 102 28 110 Z" fill={shirt.light} {...stroke} strokeWidth={2} />
            <g fill="#fff" stroke={OUTLINE} strokeWidth="1">
              {[46, 53, 60, 67, 74].map((x, i) => (
                <circle key={x} cx={x} cy={93 + (i === 2 ? 3 : i === 1 || i === 3 ? 2 : 0)} r="2.4" />
              ))}
            </g>
          </>
        )}
        {army && <path d="M52 90 C56 104 64 104 68 90" {...thin} stroke="#c9cdd2" strokeWidth={1.6} />}
        {style === "army_guy" && <rect x="56" y="100" width="8" height="10" rx="2" fill="#c9cdd2" {...stroke} strokeWidth={1.5} />}

        {/* Neck and head */}
        <rect x="52" y="74" width="16" height="18" rx="5" fill={look.skin} {...stroke} />
        {style === "army_guy" ? (
          <path d="M36 44 C36 22 84 22 84 44 L84 62 C84 76 74 84 60 84 C46 84 36 76 36 62 Z" fill={look.skin} {...stroke} />
        ) : (
          <ellipse cx="60" cy="54" rx="24" ry="28" fill={look.skin} {...stroke} />
        )}
        <ellipse cx="35" cy="57" rx="4" ry="6" fill={look.skin} {...stroke} strokeWidth={2} />
        <ellipse cx="85" cy="57" rx="4" ry="6" fill={look.skin} {...stroke} strokeWidth={2} />
        {style === "mom" && (
          <>
            <circle cx="35" cy="66" r="2.6" fill="#ffd43b" stroke={OUTLINE} strokeWidth="1" />
            <circle cx="85" cy="66" r="2.6" fill="#ffd43b" stroke={OUTLINE} strokeWidth="1" />
          </>
        )}

        {/* Hair and hats in front */}
        {(look.head === "helmet" || look.head === "helmet_ponytail") && (
          <>
            <path d="M32 46 C30 14 90 14 88 46 C70 40 50 40 32 46 Z" fill={HELMET} {...stroke} />
            <path d="M30 46 C50 39 70 39 90 46 L90 50 C70 44 50 44 30 50 Z" fill={HELMET_DARK} {...stroke} strokeWidth={2} />
            <path d="M46 24 C52 20 60 19 66 20" {...thin} stroke="#7d9a55" strokeWidth={3} />
            <path d="M37 50 L42 78 M83 50 L78 78" {...thin} stroke={HELMET_DARK} />
          </>
        )}
        {look.head === "short" && (
          <path d="M36 50 C32 20 88 18 84 50 C80 40 74 36 66 36 C56 36 50 32 46 34 C42 38 38 42 36 50 Z" fill={look.hair} {...stroke} />
        )}
        {look.head === "long" && (
          <path d="M35 54 C32 24 88 24 85 54 C80 42 72 36 60 34 C52 40 42 44 35 54 Z" fill={look.hair} {...stroke} />
        )}
        {look.head === "pigtails" && (
          <path d="M36 52 C32 20 88 20 84 52 C78 42 72 40 66 44 C60 38 52 38 46 44 C42 44 38 46 36 52 Z" fill={look.hair} {...stroke} />
        )}
        {look.head === "cap" && (
          <>
            <path d="M38 44 L34 34 L44 40 L46 30 L52 38 Z" fill={look.hair} {...stroke} strokeWidth={2} />
            <path d="M35 46 C33 18 87 18 85 46 C70 40 50 40 35 46 Z" fill={shirt.dark} {...stroke} />
            <path d="M60 22 L60 42" {...thin} stroke={shirt.light} />
            <circle cx="60" cy="22" r="3" fill={shirt.light} stroke={OUTLINE} strokeWidth="1.5" />
          </>
        )}
        {look.head === "wild" && <path d="M44 32 C50 26 70 26 76 32" {...thin} stroke="#d9a98a" strokeWidth={1.5} />}
        {look.head === "bun" && (
          <path d="M36 50 C34 22 86 22 84 50 C80 42 76 40 72 42 C68 36 64 36 60 40 C56 36 52 36 48 42 C44 40 40 42 36 50 Z" fill={look.hair} {...stroke} />
        )}

        {/* Face */}
        {style === "uncle" ? (
          <>
            <circle cx="49" cy="55" r="10" fill="#fff" {...stroke} />
            <circle cx="72" cy="55" r="10" fill="#fff" {...stroke} />
            <path d="M59 55 L62 55 M39 53 L35 50 M82 53 L86 50" {...thin} />
            <circle cx="46" cy="57" r="4.5" fill={OUTLINE} />
            <circle cx="75" cy="52" r="2.6" fill={OUTLINE} />
            <path d="M41 41 L54 44 M66 41 L80 37" {...thin} strokeWidth={3} stroke={look.hair} />
            <path d="M46 70 C52 82 70 82 76 68 C66 72 54 73 46 70 Z" fill="#fff" {...stroke} strokeWidth={2} />
            <path d="M54 72 L55 77 M61 72 L61 78 M68 71 L67 77" {...thin} strokeWidth={1.4} />
          </>
        ) : style === "grandma" ? (
          <>
            <circle cx="50" cy="56" r="2.8" fill={OUTLINE} />
            <circle cx="70" cy="56" r="2.8" fill={OUTLINE} />
            <path d="M42 48 L56 53 M78 48 L64 53" {...thin} strokeWidth={2.6} stroke="#9a968f" />
            <rect x="42" y="52" width="15" height="10" rx="4" fill="rgba(255,255,255,0.45)" {...stroke} strokeWidth={2} />
            <rect x="63" y="52" width="15" height="10" rx="4" fill="rgba(255,255,255,0.45)" {...stroke} strokeWidth={2} />
            <path d="M57 56 L63 56" {...thin} />
            <path d="M40 66 C42 68 44 68 46 66 M74 66 C76 68 78 68 80 66 M48 44 C52 42 56 42 58 44" {...thin} stroke="#c79a80" strokeWidth={1.4} />
            <path d="M48 73 C54 68 66 68 72 73 C66 77 54 77 48 73 Z" fill="#7a2a2a" {...stroke} strokeWidth={2} />
            <rect x="56" y="70.5" width="5" height="4" rx="1" fill="#fff" stroke={OUTLINE} strokeWidth="1" />
          </>
        ) : (
          <>
            <circle cx="50" cy="56" r={kid ? 4 : 3.2} fill={OUTLINE} />
            <circle cx="70" cy="56" r={kid ? 4 : 3.2} fill={OUTLINE} />
            <circle cx="51.2" cy="54.8" r="1.1" fill="#fff" />
            <circle cx="71.2" cy="54.8" r="1.1" fill="#fff" />
            {army ? (
              <>
                <path d="M43 49 L56 51 M77 49 L64 51" {...thin} strokeWidth={2.8} />
                <path d="M43 63 L56 63 M64 63 L77 63" {...thin} strokeWidth={3.5} stroke="#2b2b2b" />
              </>
            ) : (
              <path d="M44 48 C48 45 53 45 56 47 M64 47 C67 45 72 45 76 48" {...thin} stroke={look.hair} strokeWidth={2.4} />
            )}
            {(style === "army_girl" || style === "mom" || style === "girl") && (
              <path d="M45 53 L42 50 M75 53 L78 50" {...thin} strokeWidth={1.6} />
            )}
            {style === "army_guy" && (
              <>
                <path d="M50 72 L70 72" {...thin} strokeWidth={2.6} />
                <g fill="#6b5a48" opacity="0.5">
                  {[44, 50, 56, 64, 70, 76].map((x) => (
                    <circle key={x} cx={x} cy={77 + ((x * 7) % 3)} r="1" />
                  ))}
                </g>
              </>
            )}
            {style === "army_girl" && <path d="M51 70 C56 75 64 75 70 69" {...thin} strokeWidth={2.4} />}
            {style === "dad" && (
              <>
                <path d="M49 69 C55 76 65 76 71 69" {...thin} strokeWidth={2.4} />
                <path d="M50 66 C55 62 65 62 70 66 C65 67 55 67 50 66 Z" fill={look.hair} {...stroke} strokeWidth={1.5} />
              </>
            )}
            {style === "mom" && (
              <path d="M50 69 C55 77 65 77 70 69 C64 71 56 71 50 69 Z" fill="#d6336c" {...stroke} strokeWidth={1.8} />
            )}
            {style === "girl" && (
              <>
                <path d="M49 68 C54 78 66 78 71 68 Z" fill="#fff" {...stroke} strokeWidth={2} />
                <g fill="#c98a5e">
                  <circle cx="44" cy="63" r="1.1" />
                  <circle cx="47" cy="65.5" r="1.1" />
                  <circle cx="76" cy="63" r="1.1" />
                  <circle cx="73" cy="65.5" r="1.1" />
                </g>
              </>
            )}
            {style === "boy" && (
              <>
                <path d="M48 68 C54 79 66 79 72 68 Z" fill="#fff" {...stroke} strokeWidth={2} />
                <rect x="58" y="68" width="4.5" height="4.5" fill={OUTLINE} />
                <rect x="71" y="60" width="9" height="4.5" rx="2" fill="#f2c9a0" stroke="#b9895f" strokeWidth="1" transform="rotate(-20 75 62)" />
              </>
            )}
          </>
        )}
      </g>
    </svg>
  );
}

/**
 * SpeakingMouth — an SVG mouth overlay that morphs between three states
 * driven by `amplitude` (0..1 live RMS of the playing Kokoro clip) and
 * `smile` (0..1, typically flipped to 1 during the greet flash).
 *
 * Why SVG path morphing instead of a CSS shape? Because a real mouth
 * has *two* control points (the seam between the lips and the bottom
 * of the opening), and interpolating between "closed neutral", "open
 * speaking", and "smile" is one equation, not three stacked divs with
 * border-radius gymnastics.
 *
 * Coordinate system
 * -----------------
 *   viewBox: `-100 -50 200 100`  (center origin, Y-down)
 *   corners at (±cornerX, 0), always
 *   top lip seam: cubic curve with control Y = smile * smileLift
 *   open cavity: curve with control Y = topY + (closedGap + amp * openExtent)
 *
 * For a happy smile, the top lip seam is pushed *down* in the middle
 * (corners stay at y=0, center dips to +smileLift), which reads as
 * "corners lifted compared to the midpoint" — the universal smile
 * silhouette. The bottom curve always sits below the top, so even a
 * big smile with full volume stays visually coherent.
 *
 * Overlay strategy
 * ----------------
 * The portrait we render behind this already has a drawn mouth. We
 * don't want to double it up when silent, so the whole SVG fades its
 * opacity with `visibilityScore = max(amplitude * 4, smile)` — i.e.
 * invisible at rest, fully visible while speaking, visible while
 * smiling. A soft radial vignette around the mouth helps it blend
 * into the underlying face.
 */
type SpeakingMouthProps = {
  amplitude: number;
  smile: number;
  /** 0..1 visibility multiplier — lets the parent fade the whole mouth in/out. */
  visibility?: number;
};

export default function SpeakingMouth({
  amplitude,
  smile,
  visibility,
}: SpeakingMouthProps) {
  const amp = Math.min(1, Math.max(0, amplitude));
  const sm = Math.min(1, Math.max(0, smile));

  // Corner width — controls the mouth's aspect ratio.
  const cornerX = 80;
  // How far the middle of the top-lip seam dips below the corners when
  // smiling. Pure-smile value is ~7 — enough to read as an upward
  // curve without caricature.
  const topY = sm * 7;
  // How far below the top-lip seam the bottom of the open cavity goes.
  // A tiny gap (1.5) when silent so the mouth is never a flat line,
  // then amplitude opens it up to ~30.
  const cavityDepth = 1.5 + amp * 30;
  const bottomY = topY + cavityDepth;

  // Single path: two quadratic curves meeting at the corners.
  const d =
    `M ${-cornerX} 0 ` +
    `Q 0 ${topY} ${cornerX} 0 ` +
    `Q 0 ${bottomY} ${-cornerX} 0 Z`;

  // Subtle teeth hint — a lighter strip near the top of the cavity
  // while the mouth is open. Reads as "lips parted over teeth"
  // without requiring actual dentition geometry. Fades in quickly.
  const showTeeth = amp > 0.15;
  const teethAlpha = Math.min(1, (amp - 0.15) * 2.5);

  // Parent-driven visibility falls back to an amplitude-or-smile mix
  // so the mouth is invisible at rest, fully opaque while speaking.
  const vis =
    visibility !== undefined
      ? Math.min(1, Math.max(0, visibility))
      : Math.min(1, Math.max(amp * 3.5, sm));

  return (
    <svg
      viewBox="-100 -50 200 100"
      preserveAspectRatio="xMidYMid meet"
      className="w-full h-full pointer-events-none select-none"
      style={{
        opacity: vis,
        // Path morph animates via native SVG (each render recomputes `d`);
        // the opacity/fade is eased in CSS so smile-in/out feels warm.
        transition: "opacity 180ms ease-out",
        filter:
          "drop-shadow(0 1px 1px rgba(0,0,0,0.35)) drop-shadow(0 0 3px rgba(0,0,0,0.2))",
      }}
      aria-hidden="true"
    >
      {/* Open cavity — warm dark interior. A tiny bit of red saturation
          reads as "lips", not as a black hole. */}
      <path
        d={d}
        fill="#2c0a10"
        style={{
          transition: "d 90ms linear",
        }}
      />
      {/* Teeth suggestion — a flat, lighter strip inside the cavity,
          hugging the bottom of the top lip. Only visible when open. */}
      {showTeeth && (
        <rect
          x={-cornerX * 0.75}
          y={topY + 1}
          width={cornerX * 1.5}
          height={Math.min(8, cavityDepth * 0.35)}
          rx={3}
          fill="#f4e8d8"
          opacity={teethAlpha * 0.9}
        />
      )}
      {/* Lip rim — a thin darker outline on just the top curve, makes
          the mouth pop from the underlying portrait. */}
      <path
        d={`M ${-cornerX} 0 Q 0 ${topY} ${cornerX} 0`}
        stroke="rgba(0,0,0,0.35)"
        strokeWidth={1.5}
        fill="none"
      />
    </svg>
  );
}

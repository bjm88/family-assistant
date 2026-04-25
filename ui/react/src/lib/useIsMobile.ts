// Tiny media-query hook used by the live-assistant page (and anywhere
// else that needs platform-aware UX defaults — e.g. defaulting voice
// + microphone OFF on phones, ON on desktops).
//
// We deliberately key off the layout breakpoint rather than the
// userAgent string for two reasons:
//
//   * It Just Works for tablets / split-screen / dev tools device
//     emulation, where UA sniffing is famously brittle.
//   * It matches the Tailwind ``md`` breakpoint (768 px) used by
//     Layout.tsx for the off-canvas sidebar — same threshold, same
//     mental model: "if the sidebar collapsed into a drawer, the
//     screen is mobile-shaped".
//
// The "(pointer: coarse)" check catches large-screen phones / tablets
// in landscape (>= 768 px wide but no precise pointer), where the
// "default voice on" assumption is still wrong (people hold these
// near their face and want quiet).
import { useEffect, useState } from "react";

const MOBILE_QUERY = "(max-width: 767.98px), (pointer: coarse)";

function evaluate(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia(MOBILE_QUERY).matches;
}

export function useIsMobile(): boolean {
  const [isMobile, setIsMobile] = useState<boolean>(() => evaluate());

  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mql = window.matchMedia(MOBILE_QUERY);
    const handler = (e: MediaQueryListEvent) => setIsMobile(e.matches);
    // Safari < 14 still wants addListener / removeListener.
    if (mql.addEventListener) {
      mql.addEventListener("change", handler);
      return () => mql.removeEventListener("change", handler);
    }
    mql.addListener(handler);
    return () => mql.removeListener(handler);
  }, []);

  return isMobile;
}

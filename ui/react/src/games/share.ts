/** Put a link on the clipboard. Resolves to whether it worked. */
export async function copyLink(url: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(url);
    return true;
  } catch {
    // No clipboard API (an old browser, or a page not served over
    // https): fall back to selecting the text and copying it.
    const field = document.createElement("textarea");
    field.value = url;
    field.setAttribute("readonly", "");
    field.style.position = "fixed";
    field.style.opacity = "0";
    document.body.appendChild(field);
    field.select();
    let copied = false;
    try {
      copied = document.execCommand("copy");
    } catch {
      copied = false;
    }
    field.remove();
    return copied;
  }
}

/** Whether this device has a share sheet (Messages, WhatsApp, …). */
export function canShare(): boolean {
  return typeof navigator.share === "function";
}

/**
 * Hand a join link to someone: the phone's share sheet when there is
 * one (Messages, WhatsApp, …), otherwise the clipboard.
 *
 * Resolves to how it went so the caller can say "Link copied".
 */
export async function shareJoinLink(
  url: string,
  text: string
): Promise<"shared" | "copied" | "failed"> {
  if (canShare()) {
    try {
      await navigator.share({ text, url });
      return "shared";
    } catch (err) {
      // The person closed the share sheet; that isn't a failure.
      if ((err as DOMException)?.name === "AbortError") return "shared";
    }
  }
  return (await copyLink(url)) ? "copied" : "failed";
}

// A guest who joined through a game's shareable link gets a link of
// their own. Remembering it on this device means opening the shared
// link again (it is the one sitting in their messages) takes them back
// to their seat instead of asking them to join a second time.

const SEAT_PREFIX = "fa-game-seat:";
const NAME_KEY = "fa-game-guest-name";

export function rememberSeat(inviteToken: string, playPath: string, name: string): void {
  try {
    localStorage.setItem(SEAT_PREFIX + inviteToken, playPath);
    localStorage.setItem(NAME_KEY, name);
  } catch {
    /* private browsing: they just join again next time */
  }
}

export function recallSeat(inviteToken: string): string | null {
  try {
    const path = localStorage.getItem(SEAT_PREFIX + inviteToken);
    return path && path.startsWith("/play/") ? path : null;
  } catch {
    return null;
  }
}

/** The name this device last joined a game with, to save typing it again. */
export function recallGuestName(): string {
  try {
    return localStorage.getItem(NAME_KEY) ?? "";
  } catch {
    return "";
  }
}

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
  if (typeof navigator.share === "function") {
    try {
      await navigator.share({ text, url });
      return "shared";
    } catch (err) {
      // The person closed the share sheet; that isn't a failure.
      if ((err as DOMException)?.name === "AbortError") return "shared";
    }
  }
  try {
    await navigator.clipboard.writeText(url);
    return "copied";
  } catch {
    return "failed";
  }
}

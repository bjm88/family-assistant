import { Bot } from "lucide-react";
import type { Assistant } from "@/lib/types";

interface AssistantAvatarProps {
  assistant: Pick<Assistant, "assistant_name" | "profile_image_path">;
  size?: number;
}

/**
 * Avatar tile for an Assistant — used both on the assistant detail
 * page and on the family dashboard "Meet your assistant" card.
 *
 * Lives in components/ rather than pages/ because it's a leaf
 * presentational element with no data dependencies. Previously it
 * was exported from pages/AssistantPage.tsx, which forced consumers
 * (e.g. FamilyDashboard) to import "across layers" and was a
 * maintenance smell when the page module gets split up later.
 */
export function AssistantAvatar({ assistant, size = 200 }: AssistantAvatarProps) {
  const initial = assistant.assistant_name?.trim()?.[0]?.toUpperCase() ?? "?";
  if (assistant.profile_image_path) {
    return (
      <img
        src={`/api/media/${assistant.profile_image_path}`}
        alt={assistant.assistant_name}
        style={{ width: size, height: size }}
        className="rounded-2xl object-cover border border-border shadow-sm"
      />
    );
  }
  return (
    <div
      style={{ width: size, height: size }}
      className="rounded-2xl bg-gradient-to-br from-primary/20 via-primary/10 to-transparent border border-border flex flex-col items-center justify-center text-primary"
    >
      <Bot style={{ width: size * 0.4, height: size * 0.4 }} />
      <div className="text-sm mt-1 font-semibold">{initial}</div>
    </div>
  );
}

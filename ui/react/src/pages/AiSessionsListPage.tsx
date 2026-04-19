import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  ArrowLeft,
  Clock,
  Mail,
  MessageSquare,
  MessageSquareText,
  Users,
  Video,
} from "lucide-react";

import { api } from "@/lib/api";
import type { Family, LiveSession } from "@/lib/types";
import { cn } from "@/lib/cn";

/**
 * Session history list for a family.
 *
 * Every row is a live AI-assistant session: when it started, which
 * family members Avi recognised, how many messages were exchanged, and
 * whether it's still active. Clicking a row drills into the full
 * transcript.
 *
 * The backend sweeps stale sessions on the list endpoint, so the UI
 * never has to think about timeouts — a session you see as "active"
 * really is active at fetch time (up to one refresh cycle).
 */
export default function AiSessionsListPage() {
  const { familyId: familyIdParam } = useParams();
  const familyId = Number(familyIdParam);
  const enabled = Number.isFinite(familyId);

  const { data: family } = useQuery<Family>({
    queryKey: ["family", familyIdParam],
    queryFn: () => api.get<Family>(`/api/families/${familyIdParam}`),
    enabled,
  });

  const { data: sessions, isLoading } = useQuery<LiveSession[]>({
    queryKey: ["ai-sessions-list", familyId],
    queryFn: () =>
      api.get<LiveSession[]>(
        `/api/aiassistant/sessions?family_id=${familyId}&limit=100`
      ),
    enabled,
    // Poll gently so a session that closes in the background flips its
    // badge without the user having to reload.
    refetchInterval: 15_000,
  });

  return (
    <div className="min-h-screen bg-gradient-to-br from-background to-muted">
      <header className="border-b border-border bg-white">
        <div className="max-w-5xl mx-auto px-6 py-4 flex items-center justify-between gap-4">
          <div className="flex items-center gap-4">
            <Link
              to={`/aiassistant/${familyId}`}
              className="text-sm text-muted-foreground hover:text-foreground inline-flex items-center gap-1"
            >
              <ArrowLeft className="h-4 w-4" /> Back to live
            </Link>
            <div>
              <div className="text-xs text-muted-foreground uppercase tracking-wide">
                {family?.family_name ?? "—"} · Session history
              </div>
              <div className="font-semibold text-lg">
                Past conversations with Avi
              </div>
            </div>
          </div>
        </div>
      </header>

      <main className="max-w-5xl mx-auto p-6">
        {isLoading ? (
          <div className="text-sm text-muted-foreground">Loading sessions…</div>
        ) : !sessions || sessions.length === 0 ? (
          <div className="rounded-lg border border-dashed border-border bg-white p-12 text-center">
            <div className="text-lg font-medium">No sessions yet</div>
            <p className="text-sm text-muted-foreground mt-2 max-w-md mx-auto">
              As soon as Avi recognises a face or you send a chat message, a
              new session will be opened and show up here.
            </p>
            <Link
              to={`/aiassistant/${familyId}`}
              className="inline-flex items-center gap-1 mt-4 rounded-md bg-primary text-primary-foreground px-3 py-1.5 text-sm hover:bg-primary/90"
            >
              Open the live page →
            </Link>
          </div>
        ) : (
          <ul className="space-y-3">
            {sessions.map((s) => (
              <li key={s.live_session_id}>
                <Link
                  to={`/aiassistant/${familyId}/sessions/${s.live_session_id}`}
                  className="block rounded-lg border border-border bg-white p-4 hover:border-primary/40 hover:shadow-sm transition"
                >
                  <div className="flex items-start justify-between gap-4">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 text-sm">
                        <span
                          className={cn(
                            "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs",
                            s.is_active
                              ? "bg-emerald-500/15 text-emerald-700 border border-emerald-500/30"
                              : "bg-muted text-muted-foreground border border-border"
                          )}
                        >
                          <span
                            className={cn(
                              "h-1.5 w-1.5 rounded-full",
                              s.is_active
                                ? "bg-emerald-500 animate-pulse"
                                : "bg-muted-foreground/50"
                            )}
                          />
                          {s.is_active ? "Active" : s.end_reason ?? "Ended"}
                        </span>
                        <SourceBadge source={s.source} />
                        <div className="font-medium truncate">
                          {s.participants_preview.length > 0
                            ? s.participants_preview.join(", ")
                            : s.source === "email"
                            ? "Email thread"
                            : s.source === "sms"
                            ? "SMS thread"
                            : "No one recognised"}
                        </div>
                      </div>
                      <div className="mt-2 text-sm text-muted-foreground flex flex-wrap items-center gap-x-4 gap-y-1">
                        <span className="inline-flex items-center gap-1">
                          <Clock className="h-3.5 w-3.5" />
                          {formatRange(s.started_at, s.ended_at)}
                        </span>
                        <span className="inline-flex items-center gap-1">
                          <Users className="h-3.5 w-3.5" />
                          {s.participant_count}{" "}
                          {s.participant_count === 1 ? "person" : "people"}
                        </span>
                        <span className="inline-flex items-center gap-1">
                          <MessageSquareText className="h-3.5 w-3.5" />
                          {s.message_count}{" "}
                          {s.message_count === 1 ? "message" : "messages"}
                        </span>
                        {s.start_context &&
                          s.source !== "email" &&
                          s.source !== "sms" && (
                            <span className="text-xs italic">
                              started via {s.start_context}
                            </span>
                          )}
                        {s.source === "email" && s.start_context && (
                          <span
                            className="text-xs italic truncate max-w-[18rem]"
                            title={s.start_context}
                          >
                            {s.start_context.replace(/^email_thread:?/, "")}
                          </span>
                        )}
                        {s.source === "sms" && s.start_context && (
                          <span
                            className="text-xs italic truncate max-w-[18rem]"
                            title={s.start_context}
                          >
                            {s.start_context.replace(/^sms_thread:?/, "")}
                          </span>
                        )}
                      </div>
                      {s.last_message_preview && (
                        <div className="mt-2 text-sm text-foreground/80 line-clamp-1">
                          <span className="text-muted-foreground">Latest:</span>{" "}
                          {s.last_message_preview}
                        </div>
                      )}
                    </div>
                    <div className="text-xs text-muted-foreground whitespace-nowrap">
                      #{s.live_session_id}
                    </div>
                  </div>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </main>
    </div>
  );
}

function SourceBadge({ source }: { source: LiveSession["source"] }) {
  if (source === "email") {
    return (
      <span
        className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs bg-sky-500/15 text-sky-700 border border-sky-500/30"
        title="Started from a Gmail thread"
      >
        <Mail className="h-3 w-3" /> via email
      </span>
    );
  }
  if (source === "sms") {
    return (
      <span
        className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs bg-violet-500/15 text-violet-700 border border-violet-500/30"
        title="Started from a Twilio SMS thread"
      >
        <MessageSquare className="h-3 w-3" /> via SMS
      </span>
    );
  }
  return (
    <span
      className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs bg-muted text-muted-foreground border border-border"
      title="Started from the live camera / chat page"
    >
      <Video className="h-3 w-3" /> live
    </span>
  );
}

function formatRange(startIso: string, endIso: string | null): string {
  const start = new Date(startIso);
  const end = endIso ? new Date(endIso) : null;
  const datePart = `${start.getMonth() + 1}/${start.getDate()}/${start
    .getFullYear()
    .toString()
    .slice(-2)}`;
  const timePart = start.toLocaleTimeString([], {
    hour: "numeric",
    minute: "2-digit",
  });
  if (!end) {
    return `${datePart} · ${timePart} · still active`;
  }
  const minutes = Math.max(
    1,
    Math.round((end.getTime() - start.getTime()) / 60000)
  );
  return `${datePart} · ${timePart} · ${minutes} min`;
}

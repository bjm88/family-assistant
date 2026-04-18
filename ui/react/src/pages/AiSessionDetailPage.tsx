import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  Bot,
  Clock,
  Info,
  Square,
  User,
  Users,
} from "lucide-react";

import { api } from "@/lib/api";
import type {
  Family,
  LiveSessionDetail,
  LiveSessionMessage,
} from "@/lib/types";
import { cn } from "@/lib/cn";

/**
 * Session detail — full participant roster + message transcript.
 *
 * The transcript is laid out as a chat log with three speaker roles:
 *   - `assistant` (Avi) — right-aligned bubbles with the bot badge
 *   - `user` (a family member) — left-aligned with the person's name
 *   - `system` — small italic dividers (e.g. "Session started")
 *
 * A manual "End session" control is rendered for active sessions so the
 * admin can close a forgotten session immediately instead of waiting
 * for the 30-minute idle sweep.
 */
export default function AiSessionDetailPage() {
  const { familyId: familyIdParam, sessionId: sessionIdParam } = useParams();
  const familyId = Number(familyIdParam);
  const sessionId = Number(sessionIdParam);
  const qc = useQueryClient();

  const { data: family } = useQuery<Family>({
    queryKey: ["family", familyIdParam],
    queryFn: () => api.get<Family>(`/api/families/${familyIdParam}`),
    enabled: Number.isFinite(familyId),
  });

  const { data: session, isLoading } = useQuery<LiveSessionDetail>({
    queryKey: ["ai-session-detail", sessionId],
    queryFn: () =>
      api.get<LiveSessionDetail>(`/api/aiassistant/sessions/${sessionId}`),
    enabled: Number.isFinite(sessionId),
    // Keep an active session fresh so messages stream in without a reload.
    refetchInterval: (query) =>
      query.state.data?.is_active ? 4_000 : false,
  });

  const endMut = useMutation({
    mutationFn: () =>
      api.post(`/api/aiassistant/sessions/${sessionId}/end`, {
        end_reason: "manual",
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["ai-session-detail", sessionId] });
      qc.invalidateQueries({ queryKey: ["ai-sessions-list", familyId] });
      qc.invalidateQueries({ queryKey: ["ai-active-session", familyId] });
    },
  });

  return (
    <div className="min-h-screen bg-gradient-to-br from-background to-muted">
      <header className="border-b border-border bg-white">
        <div className="max-w-4xl mx-auto px-6 py-4 flex items-center justify-between gap-4">
          <div className="flex items-center gap-4 min-w-0">
            <Link
              to={`/aiassistant/${familyId}/sessions`}
              className="text-sm text-muted-foreground hover:text-foreground inline-flex items-center gap-1 shrink-0"
            >
              <ArrowLeft className="h-4 w-4" /> All sessions
            </Link>
            <div className="min-w-0">
              <div className="text-xs text-muted-foreground uppercase tracking-wide truncate">
                {family?.family_name ?? "—"} · Session #{sessionId}
              </div>
              <div className="font-semibold text-lg truncate">
                {session
                  ? session.participants_preview.length > 0
                    ? `Conversation with ${session.participants_preview.join(
                        ", "
                      )}`
                    : "Conversation transcript"
                  : "Loading…"}
              </div>
            </div>
          </div>
          {session?.is_active && (
            <button
              onClick={() => endMut.mutate()}
              disabled={endMut.isPending}
              className="inline-flex items-center gap-1 rounded-md border border-amber-500/40 bg-amber-50 px-3 py-1.5 text-xs text-amber-800 hover:bg-amber-100 disabled:opacity-60"
            >
              <Square className="h-3.5 w-3.5" />
              End session
            </button>
          )}
        </div>
      </header>

      <main className="max-w-4xl mx-auto p-6 space-y-6">
        {isLoading || !session ? (
          <div className="text-sm text-muted-foreground">
            Loading transcript…
          </div>
        ) : (
          <>
            <SessionHeaderCard session={session} />
            <ParticipantsCard session={session} />
            <TranscriptCard session={session} />
          </>
        )}
      </main>
    </div>
  );
}

// ---------------------------------------------------------------------------

function SessionHeaderCard({ session }: { session: LiveSessionDetail }) {
  const started = new Date(session.started_at);
  const ended = session.ended_at ? new Date(session.ended_at) : null;
  const lastActivity = new Date(session.last_activity_at);

  return (
    <div className="rounded-lg border border-border bg-white p-4 grid grid-cols-1 sm:grid-cols-3 gap-3 text-sm">
      <HeaderStat
        icon={Clock}
        label="Started"
        value={started.toLocaleString()}
      />
      <HeaderStat
        icon={Clock}
        label={ended ? "Ended" : "Last activity"}
        value={(ended ?? lastActivity).toLocaleString()}
      />
      <HeaderStat
        icon={Info}
        label="Status"
        value={
          session.is_active
            ? "Active"
            : `Ended (${session.end_reason ?? "unknown"})`
        }
        valueClassName={
          session.is_active ? "text-emerald-700" : "text-muted-foreground"
        }
      />
      {session.start_context && (
        <HeaderStat
          icon={Info}
          label="Opened via"
          value={session.start_context}
          className="sm:col-span-3"
        />
      )}
    </div>
  );
}

function HeaderStat({
  icon: Icon,
  label,
  value,
  className,
  valueClassName,
}: {
  icon: typeof Clock;
  label: string;
  value: string;
  className?: string;
  valueClassName?: string;
}) {
  return (
    <div className={cn("min-w-0", className)}>
      <div className="text-[11px] uppercase tracking-wide text-muted-foreground flex items-center gap-1">
        <Icon className="h-3 w-3" /> {label}
      </div>
      <div className={cn("mt-0.5 font-medium truncate", valueClassName)}>
        {value}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------

function ParticipantsCard({ session }: { session: LiveSessionDetail }) {
  if (session.participants.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-border bg-white p-4 text-sm text-muted-foreground">
        <Users className="inline h-4 w-4 mr-1" />
        No one was recognised during this session.
      </div>
    );
  }
  return (
    <div className="rounded-lg border border-border bg-white p-4">
      <div className="text-xs uppercase tracking-wide text-muted-foreground mb-2 flex items-center gap-1">
        <Users className="h-3.5 w-3.5" /> Participants
      </div>
      <ul className="flex flex-wrap gap-2">
        {session.participants.map((p) => (
          <li
            key={p.live_session_participant_id}
            className={cn(
              "inline-flex items-center gap-1 rounded-full border px-3 py-1 text-xs",
              p.greeted_already
                ? "bg-primary/10 text-primary border-primary/30"
                : "bg-muted text-muted-foreground border-border"
            )}
            title={
              p.greeted_already
                ? `Greeted at ${new Date(p.joined_at).toLocaleTimeString()}`
                : "Recognised but not yet greeted"
            }
          >
            <User className="h-3 w-3" />
            {p.person_name ?? `Person ${p.person_id}`}
            {p.greeted_already ? " · greeted" : " · not greeted"}
          </li>
        ))}
      </ul>
    </div>
  );
}

// ---------------------------------------------------------------------------

function TranscriptCard({ session }: { session: LiveSessionDetail }) {
  if (session.messages.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-border bg-white p-4 text-sm text-muted-foreground">
        No messages yet.
      </div>
    );
  }
  return (
    <div className="rounded-lg border border-border bg-white p-4 space-y-3">
      <div className="text-xs uppercase tracking-wide text-muted-foreground">
        Transcript
      </div>
      <ol className="space-y-3">
        {session.messages.map((m) => (
          <li key={m.live_session_message_id}>
            <MessageRow message={m} />
          </li>
        ))}
      </ol>
    </div>
  );
}

function MessageRow({ message }: { message: LiveSessionMessage }) {
  const time = new Date(message.created_at).toLocaleTimeString([], {
    hour: "numeric",
    minute: "2-digit",
  });

  if (message.role === "system") {
    return (
      <div className="flex items-center gap-2 justify-center text-xs text-muted-foreground">
        <span className="h-px flex-1 bg-border" />
        <span className="italic">{message.content}</span>
        <span>{time}</span>
        <span className="h-px flex-1 bg-border" />
      </div>
    );
  }

  const isAssistant = message.role === "assistant";
  const who = isAssistant ? "Avi" : message.person_name ?? "Someone";

  return (
    <div
      className={cn(
        "flex items-start gap-2",
        isAssistant ? "flex-row-reverse" : "flex-row"
      )}
    >
      <div
        className={cn(
          "h-7 w-7 rounded-full flex items-center justify-center shrink-0",
          isAssistant
            ? "bg-primary/15 text-primary"
            : "bg-muted text-muted-foreground"
        )}
      >
        {isAssistant ? <Bot className="h-4 w-4" /> : <User className="h-4 w-4" />}
      </div>
      <div className={cn("max-w-[75%]", isAssistant ? "items-end" : "items-start")}>
        <div
          className={cn(
            "text-[11px] mb-0.5",
            isAssistant ? "text-right text-primary/80" : "text-muted-foreground"
          )}
        >
          {who} · {time}
        </div>
        <div
          className={cn(
            "rounded-2xl px-3 py-2 text-sm whitespace-pre-wrap",
            isAssistant
              ? "bg-primary text-primary-foreground rounded-tr-md"
              : "bg-muted text-foreground rounded-tl-md"
          )}
        >
          {message.content}
        </div>
      </div>
    </div>
  );
}

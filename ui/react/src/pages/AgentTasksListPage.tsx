import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  Clock,
  Loader2,
  Sparkles,
  XCircle,
} from "lucide-react";

import { api } from "@/lib/api";
import type { Family } from "@/lib/types";
import { cn } from "@/lib/cn";

/**
 * Agent task history.
 *
 * Each row is one chat turn that hit the agent loop. We surface the
 * user prompt, status, the model used, runtime, and the count of
 * intermediate tool steps. Click through to replay the full
 * thought→tool→result transcript.
 *
 * Soft-polls every 5s so a task that's still mid-flight (sending an
 * email, running a long SQL) flips its status badge live without a
 * manual refresh.
 */

type TaskSummary = {
  agent_task_id: number;
  family_id: number;
  live_session_id: number | null;
  person_id: number | null;
  kind: string;
  status: "pending" | "running" | "succeeded" | "failed" | "cancelled";
  input_text: string;
  summary: string | null;
  error: string | null;
  started_at: string | null;
  completed_at: string | null;
  duration_ms: number | null;
  model: string | null;
  created_at: string;
  step_count: number;
};

export default function AgentTasksListPage() {
  const { familyId: familyIdParam } = useParams();
  const familyId = Number(familyIdParam);
  const enabled = Number.isFinite(familyId);

  const { data: family } = useQuery<Family>({
    queryKey: ["family", familyIdParam],
    queryFn: () => api.get<Family>(`/api/families/${familyIdParam}`),
    enabled,
  });

  const { data: tasks, isLoading } = useQuery<TaskSummary[]>({
    queryKey: ["agent-tasks-list", familyId],
    queryFn: () =>
      api.get<TaskSummary[]>(
        `/api/aiassistant/tasks?family_id=${familyId}&limit=100`
      ),
    enabled,
    refetchInterval: 5_000,
  });

  return (
    <div className="min-h-screen bg-gradient-to-br from-background to-muted">
      <header className="border-b border-border bg-white">
        <div className="max-w-5xl mx-auto px-6 py-4 flex items-center gap-4">
          <Link
            to={`/aiassistant/${familyId}`}
            className="text-sm text-muted-foreground hover:text-foreground inline-flex items-center gap-1"
          >
            <ArrowLeft className="h-4 w-4" /> Back to live
          </Link>
          <div>
            <div className="text-xs text-muted-foreground uppercase tracking-wide">
              {family?.family_name ?? "—"} · Agent tasks
            </div>
            <div className="font-semibold text-lg flex items-center gap-2">
              <Sparkles className="h-4 w-4 text-primary" />
              What Avi has been doing
            </div>
          </div>
        </div>
      </header>

      <main className="max-w-5xl mx-auto p-6">
        {isLoading ? (
          <div className="text-sm text-muted-foreground">Loading tasks…</div>
        ) : !tasks || tasks.length === 0 ? (
          <div className="rounded-lg border border-dashed border-border bg-white p-12 text-center">
            <div className="text-lg font-medium">No agent tasks yet</div>
            <p className="text-sm text-muted-foreground mt-2 max-w-md mx-auto">
              Every chat turn that uses tools (sending email, looking
              someone up, querying the database) appears here with a
              full audit trail.
            </p>
          </div>
        ) : (
          <ul className="space-y-3">
            {tasks.map((t) => (
              <li key={t.agent_task_id}>
                <Link
                  to={`/aiassistant/${familyId}/agent-tasks/${t.agent_task_id}`}
                  className="block rounded-lg border border-border bg-white p-4 hover:border-primary/40 hover:shadow-sm transition"
                >
                  <div className="flex items-start justify-between gap-4">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <StatusBadge status={t.status} />
                        <span className="text-xs text-muted-foreground">
                          {t.step_count}{" "}
                          {t.step_count === 1 ? "step" : "steps"}
                        </span>
                        {t.model && (
                          <span className="text-[10px] text-muted-foreground bg-muted px-1.5 py-0.5 rounded">
                            {t.model}
                          </span>
                        )}
                      </div>
                      <div className="mt-1.5 text-sm font-medium line-clamp-2">
                        {t.input_text}
                      </div>
                      {t.summary && (
                        <div className="mt-1 text-xs text-muted-foreground line-clamp-2">
                          → {t.summary}
                        </div>
                      )}
                      {t.error && (
                        <div className="mt-1 text-xs text-rose-600 line-clamp-2">
                          {t.error}
                        </div>
                      )}
                      <div className="mt-2 text-xs text-muted-foreground inline-flex items-center gap-1">
                        <Clock className="h-3 w-3" />
                        {formatStartedAt(t.created_at)}
                        {t.duration_ms != null && (
                          <span> · {formatDuration(t.duration_ms)}</span>
                        )}
                      </div>
                    </div>
                    <div className="text-xs text-muted-foreground whitespace-nowrap">
                      #{t.agent_task_id}
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

export function StatusBadge({ status }: { status: TaskSummary["status"] }) {
  const cfg = STATUS_CFG[status];
  const Icon = cfg.icon;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] border",
        cfg.classes
      )}
    >
      <Icon
        className={cn(
          "h-3 w-3",
          status === "running" && "animate-spin"
        )}
      />
      {cfg.label}
    </span>
  );
}

const STATUS_CFG: Record<
  TaskSummary["status"],
  { label: string; icon: typeof CheckCircle2; classes: string }
> = {
  pending: {
    label: "Pending",
    icon: Clock,
    classes: "bg-muted text-muted-foreground border-border",
  },
  running: {
    label: "Running",
    icon: Loader2,
    classes: "bg-amber-100 text-amber-700 border-amber-300",
  },
  succeeded: {
    label: "Done",
    icon: CheckCircle2,
    classes: "bg-emerald-100 text-emerald-700 border-emerald-300",
  },
  failed: {
    label: "Failed",
    icon: XCircle,
    classes: "bg-rose-100 text-rose-700 border-rose-300",
  },
  cancelled: {
    label: "Cancelled",
    icon: AlertTriangle,
    classes: "bg-muted text-muted-foreground border-border",
  },
};

function formatStartedAt(iso: string): string {
  const d = new Date(iso);
  return `${d.toLocaleDateString()} ${d.toLocaleTimeString([], {
    hour: "numeric",
    minute: "2-digit",
  })}`;
}

export function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${Math.round(s - m * 60)}s`;
}

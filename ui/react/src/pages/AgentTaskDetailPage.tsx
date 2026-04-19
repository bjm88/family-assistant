import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  AlertCircle,
  ArrowLeft,
  Bot,
  Database,
  History,
  Mail,
  Search,
  Sparkles,
  User,
} from "lucide-react";

import { api } from "@/lib/api";
import { cn } from "@/lib/cn";

import {
  StatusBadge,
  formatDuration,
} from "./AgentTasksListPage";

/**
 * Single agent task drill-down — the per-step trace.
 *
 * Rendered as a chronological list with the model's thoughts, tool
 * calls and tool results inline. Intended for debugging "why did Avi
 * pick that?" questions and validating that long-running actions
 * (sending email, multi-hop research) actually completed.
 */

type AgentStep = {
  agent_step_id: number;
  step_index: number;
  step_type: "thinking" | "tool_call" | "tool_result" | "final" | "error";
  tool_name: string | null;
  tool_input: Record<string, unknown> | null;
  tool_output: unknown;
  content: string | null;
  error: string | null;
  model: string | null;
  duration_ms: number | null;
  created_at: string | null;
};

type TaskDetail = {
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
  steps: AgentStep[];
};

const TOOL_ICONS: Record<string, typeof Mail> = {
  gmail_send: Mail,
  lookup_person: Search,
  sql_query: Database,
  calendar_list_upcoming: History,
};

export default function AgentTaskDetailPage() {
  const { familyId, taskId } = useParams();
  const id = Number(taskId);
  const enabled = Number.isFinite(id);

  const { data, isLoading, isError } = useQuery<TaskDetail>({
    queryKey: ["agent-task", id],
    queryFn: () => api.get<TaskDetail>(`/api/aiassistant/tasks/${id}`),
    enabled,
    refetchInterval: (q) => {
      const status = (q.state.data as TaskDetail | undefined)?.status;
      return status === "running" || status === "pending" ? 1500 : false;
    },
  });

  return (
    <div className="min-h-screen bg-gradient-to-br from-background to-muted">
      <header className="border-b border-border bg-white">
        <div className="max-w-4xl mx-auto px-6 py-4 flex items-center gap-4">
          <Link
            to={`/aiassistant/${familyId}/agent-tasks`}
            className="text-sm text-muted-foreground hover:text-foreground inline-flex items-center gap-1"
          >
            <ArrowLeft className="h-4 w-4" /> Back to tasks
          </Link>
          <div>
            <div className="text-xs text-muted-foreground uppercase tracking-wide">
              Agent task #{id}
            </div>
            <div className="font-semibold text-lg flex items-center gap-2">
              <Sparkles className="h-4 w-4 text-primary" />
              Step transcript
            </div>
          </div>
        </div>
      </header>

      <main className="max-w-4xl mx-auto p-6 space-y-4">
        {isLoading && (
          <div className="text-sm text-muted-foreground">Loading…</div>
        )}
        {isError && (
          <div className="text-sm text-rose-600">Couldn't load this task.</div>
        )}
        {data && (
          <>
            <section className="rounded-lg border border-border bg-white p-4 space-y-3">
              <div className="flex items-center gap-2">
                <StatusBadge status={data.status} />
                {data.model && (
                  <span className="text-[11px] text-muted-foreground bg-muted px-1.5 py-0.5 rounded">
                    {data.model}
                  </span>
                )}
                {data.duration_ms != null && (
                  <span className="text-xs text-muted-foreground">
                    {formatDuration(data.duration_ms)}
                  </span>
                )}
              </div>
              <div>
                <div className="text-xs uppercase tracking-wider text-muted-foreground mb-1 flex items-center gap-1">
                  <User className="h-3 w-3" /> User said
                </div>
                <div className="text-sm whitespace-pre-wrap">
                  {data.input_text}
                </div>
              </div>
              {data.summary && (
                <div>
                  <div className="text-xs uppercase tracking-wider text-muted-foreground mb-1 flex items-center gap-1">
                    <Bot className="h-3 w-3" /> Final reply
                  </div>
                  <div className="text-sm whitespace-pre-wrap">
                    {data.summary}
                  </div>
                </div>
              )}
              {data.error && (
                <div className="text-sm text-rose-600">
                  Error: {data.error}
                </div>
              )}
            </section>

            <section className="space-y-2">
              <h2 className="text-sm font-medium text-muted-foreground uppercase tracking-wider">
                Steps
              </h2>
              {data.steps.length === 0 ? (
                <div className="rounded-lg border border-dashed border-border bg-white p-6 text-center text-sm text-muted-foreground">
                  No steps recorded yet.
                </div>
              ) : (
                <ol className="space-y-2">
                  {data.steps.map((s) => (
                    <StepCard key={s.agent_step_id} step={s} />
                  ))}
                </ol>
              )}
            </section>
          </>
        )}
      </main>
    </div>
  );
}

function StepCard({ step }: { step: AgentStep }) {
  const Icon = step.tool_name
    ? (TOOL_ICONS[step.tool_name] ?? Sparkles)
    : step.step_type === "error"
      ? AlertCircle
      : Sparkles;
  const isError = step.step_type === "error" || !!step.error;
  return (
    <li
      className={cn(
        "rounded-lg border bg-white p-3 space-y-2",
        isError ? "border-rose-300" : "border-border"
      )}
    >
      <div className="flex items-center gap-2 text-xs">
        <span className="font-mono text-muted-foreground">
          #{step.step_index}
        </span>
        <Icon
          className={cn(
            "h-3.5 w-3.5",
            isError ? "text-rose-600" : "text-muted-foreground"
          )}
        />
        <span className="font-medium uppercase tracking-wider">
          {step.step_type}
        </span>
        {step.tool_name && (
          <span className="text-muted-foreground">{step.tool_name}</span>
        )}
        {step.duration_ms != null && (
          <span className="text-muted-foreground ml-auto">
            {formatDuration(step.duration_ms)}
          </span>
        )}
      </div>
      {step.content && (
        <div className="text-sm whitespace-pre-wrap">{step.content}</div>
      )}
      {step.tool_input && Object.keys(step.tool_input).length > 0 && (
        <details className="text-xs">
          <summary className="cursor-pointer text-muted-foreground">
            Input
          </summary>
          <pre className="mt-1 bg-muted/40 p-2 rounded overflow-x-auto whitespace-pre-wrap break-words">
            {JSON.stringify(step.tool_input, null, 2)}
          </pre>
        </details>
      )}
      {step.tool_output && (
        <details className="text-xs">
          <summary className="cursor-pointer text-muted-foreground">
            Output
          </summary>
          <pre className="mt-1 bg-muted/40 p-2 rounded overflow-x-auto whitespace-pre-wrap break-words">
            {JSON.stringify(step.tool_output, null, 2)}
          </pre>
        </details>
      )}
      {step.error && (
        <div className="text-xs text-rose-600">{step.error}</div>
      )}
    </li>
  );
}

import { useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import {
  CheckCircle2,
  CircleDashed,
  Clock,
  Download,
  Flag,
  ListTodo,
  Loader2,
  MessageSquare,
  Paperclip,
  Plus,
  Trash2,
  UploadCloud,
  Users,
  X,
} from "lucide-react";
import { api } from "@/lib/api";
import type {
  Person,
  Task,
  TaskAttachment,
  TaskComment,
  TaskDetail,
  TaskFollower,
  TaskPriority,
  TaskStatus,
} from "@/lib/types";
import { PageHeader } from "@/components/PageHeader";
import { EmptyState } from "@/components/EmptyState";
import { Modal } from "@/components/Modal";
import { Field } from "@/components/Field";
import { useToast } from "@/components/Toast";
import { cn } from "@/lib/cn";

// ---------------------------------------------------------------------------
// Static metadata: kanban columns + priority palette
// ---------------------------------------------------------------------------

interface ColumnSpec {
  status: TaskStatus;
  label: string;
  hint: string;
  icon: typeof ListTodo;
  pillClass: string;
}

const COLUMNS: ColumnSpec[] = [
  {
    status: "new",
    label: "New",
    hint: "Just captured — needs triage.",
    icon: CircleDashed,
    pillClass: "bg-slate-100 text-slate-700 border-slate-200",
  },
  {
    status: "in_progress",
    label: "In progress",
    hint: "Actively being worked on.",
    icon: Loader2,
    pillClass: "bg-blue-100 text-blue-700 border-blue-200",
  },
  {
    status: "finalizing",
    label: "Finalizing",
    hint: "Wrapping up / awaiting confirmation.",
    icon: Clock,
    pillClass: "bg-amber-100 text-amber-800 border-amber-200",
  },
  {
    status: "done",
    label: "Done",
    hint: "Closed out. Auto-stamped on completion.",
    icon: CheckCircle2,
    pillClass: "bg-emerald-100 text-emerald-800 border-emerald-200",
  },
];

const PRIORITY_META: Record<
  TaskPriority,
  { label: string; badgeClass: string; rank: number }
> = {
  urgent: {
    label: "Urgent",
    badgeClass: "bg-red-100 text-red-700 border-red-200",
    rank: 0,
  },
  high: {
    label: "High",
    badgeClass: "bg-orange-100 text-orange-700 border-orange-200",
    rank: 1,
  },
  normal: {
    label: "Normal",
    badgeClass: "bg-slate-100 text-slate-700 border-slate-200",
    rank: 2,
  },
  low: {
    label: "Low",
    badgeClass: "bg-emerald-50 text-emerald-700 border-emerald-200",
    rank: 3,
  },
  future_idea: {
    label: "Future idea",
    badgeClass: "bg-violet-50 text-violet-700 border-violet-200",
    rank: 4,
  },
};

const PRIORITY_OPTIONS: TaskPriority[] = [
  "urgent",
  "high",
  "normal",
  "low",
  "future_idea",
];
const STATUS_OPTIONS: TaskStatus[] = [
  "new",
  "in_progress",
  "finalizing",
  "done",
];

function formatPriority(p: TaskPriority): string {
  return PRIORITY_META[p].label;
}

function formatDate(value: string | null): string | null {
  if (!value) return null;
  try {
    return new Date(value).toLocaleDateString();
  } catch {
    return value;
  }
}

function personLabel(people: Map<number, Person>, id: number | null): string {
  if (!id) return "Unassigned";
  const p = people.get(id);
  if (!p) return `Person #${id}`;
  return p.preferred_name || `${p.first_name} ${p.last_name}`.trim();
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function TasksPage() {
  const { familyId } = useParams();
  const qc = useQueryClient();
  const toast = useToast();

  const [createOpen, setCreateOpen] = useState(false);
  const [activeTaskId, setActiveTaskId] = useState<number | null>(null);

  // Filters
  const [filterPerson, setFilterPerson] = useState<string>("all"); // 'all' | 'unassigned' | person_id
  const [filterPriority, setFilterPriority] = useState<TaskPriority | "all">(
    "all",
  );
  const [search, setSearch] = useState("");

  const { data: people } = useQuery<Person[]>({
    queryKey: ["people", familyId],
    queryFn: () => api.get<Person[]>(`/api/people?family_id=${familyId}`),
  });
  const peopleById = useMemo(
    () => new Map((people ?? []).map((p) => [p.person_id, p])),
    [people],
  );

  const { data: tasks, isLoading } = useQuery<Task[]>({
    queryKey: ["tasks", familyId],
    queryFn: () => api.get<Task[]>(`/api/tasks?family_id=${familyId}`),
  });

  const filtered = useMemo(() => {
    let rows = tasks ?? [];
    if (filterPerson === "unassigned") {
      rows = rows.filter((t) => t.assigned_to_person_id === null);
    } else if (filterPerson !== "all") {
      const id = Number(filterPerson);
      rows = rows.filter((t) => t.assigned_to_person_id === id);
    }
    if (filterPriority !== "all") {
      rows = rows.filter((t) => t.priority === filterPriority);
    }
    if (search.trim()) {
      const needle = search.trim().toLowerCase();
      rows = rows.filter(
        (t) =>
          t.title.toLowerCase().includes(needle) ||
          (t.description ?? "").toLowerCase().includes(needle),
      );
    }
    return rows;
  }, [tasks, filterPerson, filterPriority, search]);

  const grouped = useMemo(() => {
    const out: Record<TaskStatus, Task[]> = {
      new: [],
      in_progress: [],
      finalizing: [],
      done: [],
    };
    for (const t of filtered) out[t.status].push(t);
    return out;
  }, [filtered]);

  const updateStatus = useMutation({
    mutationFn: ({ task_id, status }: { task_id: number; status: TaskStatus }) =>
      api.patch<Task>(`/api/tasks/${task_id}`, { status }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tasks", familyId] });
    },
    onError: (err: Error) => toast.error(err.message),
  });

  const totalCount = tasks?.length ?? 0;
  const visibleCount = filtered.length;

  return (
    <div>
      <PageHeader
        title="Tasks"
        description="Household kanban — Avi can create, list, and update tasks for you. Drag/click a card to change its status."
        actions={
          <button className="btn-primary" onClick={() => setCreateOpen(true)}>
            <Plus className="h-4 w-4" /> New task
          </button>
        }
      />

      {/* Filter bar */}
      <div className="card mb-4">
        <div className="card-body grid grid-cols-1 sm:grid-cols-4 gap-3">
          <div>
            <label className="label">Search</label>
            <input
              className="input"
              placeholder="Title or description"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>
          <div>
            <label className="label">Person</label>
            <select
              className="input"
              value={filterPerson}
              onChange={(e) => setFilterPerson(e.target.value)}
            >
              <option value="all">Everyone</option>
              <option value="unassigned">Unassigned</option>
              {(people ?? []).map((p) => (
                <option key={p.person_id} value={p.person_id}>
                  {p.preferred_name || `${p.first_name} ${p.last_name}`.trim()}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="label">Priority</label>
            <select
              className="input"
              value={filterPriority}
              onChange={(e) =>
                setFilterPriority(e.target.value as TaskPriority | "all")
              }
            >
              <option value="all">All priorities</option>
              {PRIORITY_OPTIONS.map((p) => (
                <option key={p} value={p}>
                  {formatPriority(p)}
                </option>
              ))}
            </select>
          </div>
          <div className="flex items-end justify-end text-xs text-muted-foreground">
            {visibleCount === totalCount
              ? `${totalCount} task${totalCount === 1 ? "" : "s"}`
              : `${visibleCount} of ${totalCount} shown`}
          </div>
        </div>
      </div>

      {isLoading ? (
        <div className="text-sm text-muted-foreground py-12 text-center">
          Loading tasks…
        </div>
      ) : !tasks || tasks.length === 0 ? (
        <EmptyState
          icon={ListTodo}
          title="No tasks yet"
          description="Create one here, or just ask Avi: 'Add a task to fix the gate this weekend.'"
          action={
            <button className="btn-primary" onClick={() => setCreateOpen(true)}>
              <Plus className="h-4 w-4" /> New task
            </button>
          }
        />
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4">
          {COLUMNS.map((col) => (
            <KanbanColumn
              key={col.status}
              spec={col}
              tasks={grouped[col.status]}
              peopleById={peopleById}
              onOpen={setActiveTaskId}
              onAdvance={(task, next) =>
                updateStatus.mutate({ task_id: task.task_id, status: next })
              }
            />
          ))}
        </div>
      )}

      {createOpen && (
        <CreateTaskModal
          familyId={Number(familyId)}
          people={people ?? []}
          onClose={() => setCreateOpen(false)}
          onCreated={(newTask) => {
            qc.invalidateQueries({ queryKey: ["tasks", familyId] });
            setCreateOpen(false);
            setActiveTaskId(newTask.task_id);
          }}
        />
      )}

      {activeTaskId !== null && (
        <TaskDetailModal
          taskId={activeTaskId}
          familyId={Number(familyId)}
          people={people ?? []}
          peopleById={peopleById}
          onClose={() => setActiveTaskId(null)}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Kanban column + card
// ---------------------------------------------------------------------------

function KanbanColumn({
  spec,
  tasks,
  peopleById,
  onOpen,
  onAdvance,
}: {
  spec: ColumnSpec;
  tasks: Task[];
  peopleById: Map<number, Person>;
  onOpen: (id: number) => void;
  onAdvance: (task: Task, next: TaskStatus) => void;
}) {
  const Icon = spec.icon;
  return (
    <div className="card flex flex-col">
      <div className="card-header">
        <div className="flex items-center gap-2 min-w-0">
          <Icon
            className={cn(
              "h-4 w-4 shrink-0",
              spec.status === "in_progress" && "text-blue-600",
              spec.status === "finalizing" && "text-amber-600",
              spec.status === "done" && "text-emerald-600",
              spec.status === "new" && "text-slate-500",
            )}
          />
          <div className="card-title">{spec.label}</div>
        </div>
        <span className={cn("badge", spec.pillClass)}>{tasks.length}</span>
      </div>
      <div className="card-body flex-1 space-y-2 min-h-[120px]">
        {tasks.length === 0 ? (
          <div className="text-xs text-muted-foreground italic text-center py-6">
            {spec.hint}
          </div>
        ) : (
          tasks.map((t) => (
            <KanbanCard
              key={t.task_id}
              task={t}
              peopleById={peopleById}
              onOpen={() => onOpen(t.task_id)}
              onAdvance={(next) => onAdvance(t, next)}
            />
          ))
        )}
      </div>
    </div>
  );
}

function KanbanCard({
  task,
  peopleById,
  onOpen,
  onAdvance,
}: {
  task: Task;
  peopleById: Map<number, Person>;
  onOpen: () => void;
  onAdvance: (next: TaskStatus) => void;
}) {
  const meta = PRIORITY_META[task.priority];
  const due = formatDate(task.desired_end_date) ?? formatDate(task.end_date);

  return (
    <div
      className="rounded-md border border-border bg-white p-3 hover:shadow-md transition-shadow cursor-pointer group"
      onClick={onOpen}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="font-medium text-sm leading-snug truncate-2-lines">
          {task.title}
        </div>
        <span className={cn("badge shrink-0", meta.badgeClass)}>
          <Flag className="h-3 w-3 mr-1" />
          {meta.label}
        </span>
      </div>
      <div className="mt-2 text-xs text-muted-foreground flex items-center justify-between gap-2">
        <span className="truncate">
          {personLabel(peopleById, task.assigned_to_person_id)}
        </span>
        {due && <span className="shrink-0">due {due}</span>}
      </div>
      {(task.comment_count > 0 || task.attachment_count > 0 || task.follower_count > 0) && (
        <div className="mt-2 text-xs text-muted-foreground flex items-center gap-3">
          {task.comment_count > 0 && (
            <span className="inline-flex items-center gap-1">
              <MessageSquare className="h-3 w-3" />
              {task.comment_count}
            </span>
          )}
          {task.attachment_count > 0 && (
            <span className="inline-flex items-center gap-1">
              <Paperclip className="h-3 w-3" />
              {task.attachment_count}
            </span>
          )}
          {task.follower_count > 0 && (
            <span className="inline-flex items-center gap-1">
              <Users className="h-3 w-3" />
              {task.follower_count}
            </span>
          )}
        </div>
      )}
      <div
        className="mt-2 hidden group-hover:flex items-center gap-1 text-xs"
        onClick={(e) => e.stopPropagation()}
      >
        {STATUS_OPTIONS.filter((s) => s !== task.status).map((s) => (
          <button
            key={s}
            className="px-2 py-0.5 rounded border border-border text-muted-foreground hover:bg-muted hover:text-foreground"
            onClick={() => onAdvance(s)}
            title={`Move to ${s.replace("_", " ")}`}
          >
            → {s.replace("_", " ")}
          </button>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Create modal
// ---------------------------------------------------------------------------

interface CreateFormValues {
  title: string;
  description?: string;
  priority: TaskPriority;
  status: TaskStatus;
  assigned_to_person_id?: string;
  desired_end_date?: string;
  start_date?: string;
}

function CreateTaskModal({
  familyId,
  people,
  onClose,
  onCreated,
}: {
  familyId: number;
  people: Person[];
  onClose: () => void;
  onCreated: (t: Task) => void;
}) {
  const toast = useToast();
  const { register, handleSubmit, reset } = useForm<CreateFormValues>({
    defaultValues: { priority: "normal", status: "new" },
  });

  const create = useMutation({
    mutationFn: (v: CreateFormValues) =>
      api.post<Task>("/api/tasks", {
        family_id: familyId,
        title: v.title,
        description: v.description || null,
        priority: v.priority,
        status: v.status,
        assigned_to_person_id: v.assigned_to_person_id
          ? Number(v.assigned_to_person_id)
          : null,
        desired_end_date: v.desired_end_date || null,
        start_date: v.start_date || null,
      }),
    onSuccess: (t) => {
      toast.success("Task created.");
      reset();
      onCreated(t);
    },
    onError: (err: Error) => toast.error(err.message),
  });

  return (
    <Modal
      open
      onClose={onClose}
      title="New task"
      footer={
        <>
          <button className="btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn-primary"
            disabled={create.isPending}
            onClick={handleSubmit((v) => create.mutate(v))}
          >
            {create.isPending ? "Creating…" : "Create task"}
          </button>
        </>
      }
    >
      <form className="space-y-4" onSubmit={(e) => e.preventDefault()}>
        <Field label="Title" htmlFor="title">
          <input
            id="title"
            className="input"
            placeholder="e.g. Fix the east gate latch"
            {...register("title", { required: true })}
          />
        </Field>
        <Field
          label="Description"
          htmlFor="description"
          hint="Long-form context — what does done look like?"
        >
          <textarea
            id="description"
            className="input"
            rows={3}
            {...register("description")}
          />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Priority" htmlFor="priority">
            <select id="priority" className="input" {...register("priority")}>
              {PRIORITY_OPTIONS.map((p) => (
                <option key={p} value={p}>
                  {formatPriority(p)}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Status" htmlFor="status">
            <select id="status" className="input" {...register("status")}>
              {STATUS_OPTIONS.map((s) => (
                <option key={s} value={s}>
                  {s.replace("_", " ")}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <Field label="Assigned to" htmlFor="assigned_to_person_id">
          <select
            id="assigned_to_person_id"
            className="input"
            {...register("assigned_to_person_id")}
          >
            <option value="">— Unassigned</option>
            {people.map((p) => (
              <option key={p.person_id} value={p.person_id}>
                {p.preferred_name || `${p.first_name} ${p.last_name}`.trim()}
              </option>
            ))}
          </select>
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Start date" htmlFor="start_date">
            <input
              id="start_date"
              type="date"
              className="input"
              {...register("start_date")}
            />
          </Field>
          <Field
            label="Desired end date"
            htmlFor="desired_end_date"
            hint="Soft target — Avi uses this for 'due soon' filters."
          >
            <input
              id="desired_end_date"
              type="date"
              className="input"
              {...register("desired_end_date")}
            />
          </Field>
        </div>
      </form>
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// Detail modal
// ---------------------------------------------------------------------------

function TaskDetailModal({
  taskId,
  familyId,
  people,
  peopleById,
  onClose,
}: {
  taskId: number;
  familyId: number;
  people: Person[];
  peopleById: Map<number, Person>;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const fileRef = useRef<HTMLInputElement>(null);

  const detailQuery = useQuery<TaskDetail>({
    queryKey: ["task", taskId],
    queryFn: () => api.get<TaskDetail>(`/api/tasks/${taskId}`),
  });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["task", taskId] });
    qc.invalidateQueries({ queryKey: ["tasks", String(familyId)] });
  };

  const patchTask = useMutation({
    mutationFn: (patch: Partial<TaskDetail>) =>
      api.patch<TaskDetail>(`/api/tasks/${taskId}`, patch),
    onSuccess: invalidate,
    onError: (err: Error) => toast.error(err.message),
  });

  const deleteTask = useMutation({
    mutationFn: () => api.del(`/api/tasks/${taskId}`),
    onSuccess: () => {
      toast.success("Task deleted.");
      qc.invalidateQueries({ queryKey: ["tasks", String(familyId)] });
      onClose();
    },
    onError: (err: Error) => toast.error(err.message),
  });

  const addComment = useMutation({
    mutationFn: (body: string) =>
      api.post<TaskComment>(`/api/tasks/${taskId}/comments`, {
        body,
        author_kind: "person",
      }),
    onSuccess: invalidate,
    onError: (err: Error) => toast.error(err.message),
  });

  const addFollower = useMutation({
    mutationFn: (person_id: number) =>
      api.post<TaskFollower>(`/api/tasks/${taskId}/followers`, {
        person_id,
      }),
    onSuccess: invalidate,
    onError: (err: Error) => toast.error(err.message),
  });

  const removeFollower = useMutation({
    mutationFn: (person_id: number) =>
      api.del(`/api/tasks/${taskId}/followers/${person_id}`),
    onSuccess: invalidate,
    onError: (err: Error) => toast.error(err.message),
  });

  const uploadAttachment = useMutation({
    mutationFn: () => {
      const file = fileRef.current?.files?.[0];
      if (!file) throw new Error("Choose a file first.");
      const fd = new FormData();
      fd.append("file", file);
      return api.upload<TaskAttachment>(
        `/api/tasks/${taskId}/attachments`,
        fd,
      );
    },
    onSuccess: () => {
      if (fileRef.current) fileRef.current.value = "";
      invalidate();
    },
    onError: (err: Error) => toast.error(err.message),
  });

  const removeAttachment = useMutation({
    mutationFn: (id: number) =>
      api.del(`/api/tasks/${taskId}/attachments/${id}`),
    onSuccess: invalidate,
    onError: (err: Error) => toast.error(err.message),
  });

  const t = detailQuery.data;

  const [commentDraft, setCommentDraft] = useState("");
  const [followerToAdd, setFollowerToAdd] = useState<string>("");

  return (
    <Modal
      open
      onClose={onClose}
      wide
      title={t ? `Task #${t.task_id}` : "Task"}
      footer={
        <div className="flex w-full justify-between">
          <button
            className="btn-secondary text-destructive"
            onClick={() => {
              if (confirm("Delete this task? This cannot be undone.")) {
                deleteTask.mutate();
              }
            }}
          >
            <Trash2 className="h-4 w-4" /> Delete
          </button>
          <button className="btn-primary" onClick={onClose}>
            Done
          </button>
        </div>
      }
    >
      {!t ? (
        <div className="text-sm text-muted-foreground py-8 text-center">
          Loading…
        </div>
      ) : (
        <div className="space-y-5">
          {/* Title + quick edit */}
          <div>
            <input
              className="input text-base font-semibold"
              defaultValue={t.title}
              onBlur={(e) => {
                if (e.target.value !== t.title)
                  patchTask.mutate({ title: e.target.value });
              }}
            />
          </div>

          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            <Field label="Status" htmlFor="status-edit">
              <select
                id="status-edit"
                className="input"
                value={t.status}
                onChange={(e) =>
                  patchTask.mutate({ status: e.target.value as TaskStatus })
                }
              >
                {STATUS_OPTIONS.map((s) => (
                  <option key={s} value={s}>
                    {s.replace("_", " ")}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Priority" htmlFor="priority-edit">
              <select
                id="priority-edit"
                className="input"
                value={t.priority}
                onChange={(e) =>
                  patchTask.mutate({
                    priority: e.target.value as TaskPriority,
                  })
                }
              >
                {PRIORITY_OPTIONS.map((p) => (
                  <option key={p} value={p}>
                    {formatPriority(p)}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Owner" htmlFor="owner-edit">
              <select
                id="owner-edit"
                className="input"
                value={t.assigned_to_person_id ?? ""}
                onChange={(e) =>
                  patchTask.mutate({
                    assigned_to_person_id: e.target.value
                      ? Number(e.target.value)
                      : null,
                  })
                }
              >
                <option value="">— Unassigned</option>
                {people.map((p) => (
                  <option key={p.person_id} value={p.person_id}>
                    {p.preferred_name ||
                      `${p.first_name} ${p.last_name}`.trim()}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Desired end" htmlFor="desired-edit">
              <input
                id="desired-edit"
                type="date"
                className="input"
                defaultValue={t.desired_end_date ?? ""}
                onBlur={(e) =>
                  patchTask.mutate({
                    desired_end_date: e.target.value || null,
                  })
                }
              />
            </Field>
          </div>

          <Field label="Description" htmlFor="desc-edit">
            <textarea
              id="desc-edit"
              className="input"
              rows={3}
              defaultValue={t.description ?? ""}
              onBlur={(e) =>
                patchTask.mutate({ description: e.target.value || null })
              }
            />
          </Field>

          <div className="text-xs text-muted-foreground">
            Created {formatDate(t.created_at)} by{" "}
            {personLabel(peopleById, t.created_by_person_id)}
            {t.completed_at && ` · Completed ${formatDate(t.completed_at)}`}
          </div>

          {/* Followers */}
          <section>
            <div className="flex items-center gap-2 mb-2">
              <Users className="h-4 w-4 text-muted-foreground" />
              <h3 className="font-semibold text-sm">Followers</h3>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              {t.followers.length === 0 && (
                <span className="text-xs text-muted-foreground">
                  No extra followers — creator and owner are looped in
                  automatically.
                </span>
              )}
              {t.followers.map((f) => (
                <span
                  key={f.task_follower_id}
                  className="badge bg-blue-50 text-blue-700 border-blue-200 inline-flex items-center gap-1"
                >
                  {personLabel(peopleById, f.person_id)}
                  <button
                    className="hover:text-destructive"
                    onClick={() => removeFollower.mutate(f.person_id)}
                    aria-label="Remove follower"
                  >
                    <X className="h-3 w-3" />
                  </button>
                </span>
              ))}
              <select
                className="input max-w-[200px]"
                value={followerToAdd}
                onChange={(e) => setFollowerToAdd(e.target.value)}
              >
                <option value="">Add follower…</option>
                {people
                  .filter(
                    (p) =>
                      p.person_id !== t.created_by_person_id &&
                      p.person_id !== t.assigned_to_person_id &&
                      !t.followers.some((f) => f.person_id === p.person_id),
                  )
                  .map((p) => (
                    <option key={p.person_id} value={p.person_id}>
                      {p.preferred_name ||
                        `${p.first_name} ${p.last_name}`.trim()}
                    </option>
                  ))}
              </select>
              {followerToAdd && (
                <button
                  className="btn-secondary"
                  onClick={() => {
                    addFollower.mutate(Number(followerToAdd));
                    setFollowerToAdd("");
                  }}
                >
                  Add
                </button>
              )}
            </div>
          </section>

          {/* Attachments */}
          <section>
            <div className="flex items-center gap-2 mb-2">
              <Paperclip className="h-4 w-4 text-muted-foreground" />
              <h3 className="font-semibold text-sm">Attachments</h3>
            </div>
            <div className="space-y-2">
              {t.attachments.length === 0 && (
                <div className="text-xs text-muted-foreground">No files yet.</div>
              )}
              {t.attachments.map((a) => (
                <div
                  key={a.task_attachment_id}
                  className="flex items-center justify-between gap-2 border border-border rounded-md px-3 py-2 text-sm"
                >
                  <div className="min-w-0">
                    <div className="font-medium truncate">
                      {a.original_file_name}
                    </div>
                    <div className="text-xs text-muted-foreground">
                      {a.mime_type ?? "unknown type"}
                      {a.file_size_bytes
                        ? ` · ${(a.file_size_bytes / 1024).toFixed(0)} KB`
                        : ""}
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <a
                      className="text-primary hover:text-primary/80"
                      href={`/api/admin/tasks/${taskId}/attachments/${a.task_attachment_id}/download`}
                    >
                      <Download className="h-4 w-4" />
                    </a>
                    <button
                      className="text-destructive hover:text-destructive/80"
                      onClick={() =>
                        removeAttachment.mutate(a.task_attachment_id)
                      }
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </div>
                </div>
              ))}
              <div className="flex items-center gap-2">
                <input ref={fileRef} type="file" className="input flex-1" />
                <button
                  className="btn-secondary"
                  disabled={uploadAttachment.isPending}
                  onClick={() => uploadAttachment.mutate()}
                >
                  <UploadCloud className="h-4 w-4" />
                  {uploadAttachment.isPending ? "Uploading…" : "Upload"}
                </button>
              </div>
            </div>
          </section>

          {/* Comments */}
          <section>
            <div className="flex items-center gap-2 mb-2">
              <MessageSquare className="h-4 w-4 text-muted-foreground" />
              <h3 className="font-semibold text-sm">Comments</h3>
            </div>
            <div className="space-y-3">
              {t.comments.length === 0 && (
                <div className="text-xs text-muted-foreground">
                  No comments yet.
                </div>
              )}
              {t.comments.map((c) => (
                <CommentRow key={c.task_comment_id} comment={c} peopleById={peopleById} />
              ))}
              <div className="flex flex-col gap-2">
                <textarea
                  className="input"
                  rows={2}
                  placeholder="Add a comment…"
                  value={commentDraft}
                  onChange={(e) => setCommentDraft(e.target.value)}
                />
                <div className="flex justify-end">
                  <button
                    className="btn-primary"
                    disabled={!commentDraft.trim() || addComment.isPending}
                    onClick={() => {
                      addComment.mutate(commentDraft.trim(), {
                        onSuccess: () => setCommentDraft(""),
                      });
                    }}
                  >
                    Post comment
                  </button>
                </div>
              </div>
            </div>
          </section>
        </div>
      )}
    </Modal>
  );
}

function CommentRow({
  comment,
  peopleById,
}: {
  comment: TaskComment;
  peopleById: Map<number, Person>;
}) {
  const isAvi = comment.author_kind === "assistant";
  const author = isAvi
    ? "Avi"
    : personLabel(peopleById, comment.author_person_id);
  return (
    <div
      className={cn(
        "rounded-md border p-3 text-sm",
        isAvi
          ? "bg-violet-50 border-violet-200"
          : "bg-white border-border",
      )}
    >
      <div className="text-xs text-muted-foreground mb-1">
        <span className="font-medium text-foreground">{author}</span>
        {" · "}
        {formatDate(comment.created_at)}
      </div>
      <div className="whitespace-pre-wrap">{comment.body}</div>
    </div>
  );
}

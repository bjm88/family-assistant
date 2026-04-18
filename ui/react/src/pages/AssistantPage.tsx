import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import {
  Bot,
  RefreshCw,
  Sparkles,
  AlertTriangle,
  CalendarDays,
  CheckCircle2,
  Mail,
  PlugZap,
  XCircle,
} from "lucide-react";
import { api, ApiError } from "@/lib/api";
import type { Assistant } from "@/lib/types";
import { PageHeader } from "@/components/PageHeader";
import { Field } from "@/components/Field";
import { useToast } from "@/components/Toast";
import { GENDERS } from "@/lib/enums";

type GoogleStatus = {
  connected: boolean;
  granted_email: string | null;
  scopes: string[];
  token_expires_at: string | null;
  can_send_email: boolean;
  can_read_calendar: boolean;
  email_matches_assistant: boolean | null;
  oauth_configured: boolean;
};

type UpcomingEvent = {
  event_id: string;
  calendar_id: string;
  summary: string;
  start: string;
  end: string;
  location: string | null;
  organizer_email: string | null;
};

type AssistantForm = {
  assistant_name: string;
  gender: "male" | "female" | "";
  email_address: string;
  visual_description: string;
  personality_description: string;
};

export default function AssistantPage() {
  const { familyId } = useParams();
  const qc = useQueryClient();
  const toast = useToast();

  const { data: assistants } = useQuery<Assistant[]>({
    queryKey: ["assistants", familyId],
    queryFn: () => api.get<Assistant[]>(`/api/assistants?family_id=${familyId}`),
  });
  const assistant = assistants?.[0];

  // Post-OAuth redirect handling: the /oauth/callback endpoint sends
  // the browser back here with ?google_connected=<email> on success or
  // ?google_error=<reason> on failure. Surface as toast + clean URL.
  useEffect(() => {
    const url = new URL(window.location.href);
    const ok = url.searchParams.get("google_connected");
    const err = url.searchParams.get("google_error");
    if (!ok && !err) return;
    if (ok) toast.success(`Google connected for ${ok}.`);
    if (err) toast.error(`Google connect failed: ${err}`);
    url.searchParams.delete("google_connected");
    url.searchParams.delete("google_error");
    url.searchParams.delete("assistant_id");
    window.history.replaceState({}, "", url.pathname + url.search);
    qc.invalidateQueries({ queryKey: ["assistants", familyId] });
    qc.invalidateQueries({ queryKey: ["google-status"] });
  }, [familyId, qc, toast]);

  return (
    <div>
      <PageHeader
        title="Assistant"
        description="Give your family assistant a name, personality, and a look. The avatar is generated on save by Gemini and is what Avi will wear in conversation."
      />
      {assistant ? (
        <AssistantEditor
          assistant={assistant}
          onSaved={() => qc.invalidateQueries({ queryKey: ["assistants", familyId] })}
          onToastOk={(m) => toast.success(m)}
          onToastErr={(m) => toast.error(m)}
        />
      ) : (
        <CreateAssistantCard
          familyId={Number(familyId)}
          onCreated={() => qc.invalidateQueries({ queryKey: ["assistants", familyId] })}
        />
      )}
    </div>
  );
}

function CreateAssistantCard({
  familyId,
  onCreated,
}: {
  familyId: number;
  onCreated: () => void;
}) {
  const toast = useToast();
  const { register, handleSubmit } = useForm<AssistantForm>({
    defaultValues: {
      assistant_name: "Avi",
      gender: "",
      email_address: "",
      visual_description: "",
      personality_description: "",
    },
  });

  const create = useMutation({
    mutationFn: (v: AssistantForm) =>
      api.post<Assistant>("/api/assistants", {
        family_id: familyId,
        assistant_name: v.assistant_name,
        gender: v.gender || null,
        email_address: v.email_address.trim() || null,
        visual_description: v.visual_description || null,
        personality_description: v.personality_description || null,
      }),
    onSuccess: (a) => {
      onCreated();
      if (a.profile_image_path) {
        toast.success(`${a.assistant_name} is ready, avatar generated.`);
      } else {
        toast.error(
          `${a.assistant_name} saved, but the avatar could not be generated.`
        );
      }
    },
    onError: (err: Error) => toast.error(err.message),
  });

  return (
    <div className="card max-w-3xl">
      <div className="card-header">
        <div className="card-title flex items-center gap-2">
          <Bot className="h-5 w-5 text-primary" /> Meet your family assistant
        </div>
      </div>
      <div className="card-body">
        <p className="text-sm text-muted-foreground mb-4">
          Start by giving your assistant a name and a sketch of their look and
          personality. We'll ask Gemini to draw them on save.
        </p>
        <form
          className="grid grid-cols-2 gap-4"
          onSubmit={handleSubmit((v) => create.mutate(v))}
        >
          <Field label="Name" htmlFor="assistant_name">
            <input
              id="assistant_name"
              className="input"
              {...register("assistant_name", { required: true })}
            />
          </Field>
          <Field label="Gender" htmlFor="gender">
            <select id="gender" className="input" {...register("gender")}>
              <option value="">—</option>
              {GENDERS.map((g) => (
                <option key={g} value={g}>
                  {g}
                </option>
              ))}
            </select>
          </Field>
          <div className="col-span-2">
            <Field
              label="Email address"
              htmlFor="email_address"
              hint="Gmail address Avi will use to send mail and read the family calendar (free/busy lookups). Optional — leave blank if Avi doesn't have a mailbox yet."
            >
              <input
                id="email_address"
                type="email"
                autoComplete="off"
                placeholder="avi@example.com"
                className="input"
                {...register("email_address")}
              />
            </Field>
          </div>
          <div className="col-span-2">
            <Field
              label="Visual description"
              htmlFor="visual_description"
              hint="What they look like — hair, eyes, style, color palette, vibe."
            >
              <textarea
                id="visual_description"
                rows={3}
                className="input"
                placeholder="e.g. A friendly young woman with short curly hair, warm brown eyes, wearing a cozy knit sweater. Cartoon-style illustration, soft palette."
                {...register("visual_description")}
              />
            </Field>
          </div>
          <div className="col-span-2">
            <Field
              label="Personality"
              htmlFor="personality_description"
              hint="Tone and style — calm, witty, formal, chatty, proactive, etc."
            >
              <textarea
                id="personality_description"
                rows={3}
                className="input"
                placeholder="e.g. Cheerful and organized, nudges us about the calendar, explains things simply, never condescending."
                {...register("personality_description")}
              />
            </Field>
          </div>
          <div className="col-span-2 flex justify-end">
            <button type="submit" className="btn-primary" disabled={create.isPending}>
              <Sparkles className="h-4 w-4" />
              {create.isPending ? "Generating avatar…" : "Create assistant"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function AssistantEditor({
  assistant,
  onSaved,
  onToastOk,
  onToastErr,
}: {
  assistant: Assistant;
  onSaved: () => void;
  onToastOk: (msg: string) => void;
  onToastErr: (msg: string) => void;
}) {
  const { register, handleSubmit, reset } = useForm<AssistantForm>();

  useEffect(() => {
    reset({
      assistant_name: assistant.assistant_name,
      gender: (assistant.gender as AssistantForm["gender"]) ?? "",
      email_address: assistant.email_address ?? "",
      visual_description: assistant.visual_description ?? "",
      personality_description: assistant.personality_description ?? "",
    });
  }, [assistant, reset]);

  const save = useMutation({
    mutationFn: (v: AssistantForm) =>
      api.patch<Assistant>(`/api/assistants/${assistant.assistant_id}`, {
        assistant_name: v.assistant_name,
        gender: v.gender || null,
        email_address: v.email_address.trim() || null,
        visual_description: v.visual_description || null,
        personality_description: v.personality_description || null,
      }),
    onSuccess: (a) => {
      onSaved();
      if (a.avatar_generation_note) {
        onToastErr(
          `Saved ${a.assistant_name}, but avatar generation failed.`
        );
      } else {
        onToastOk(`Saved ${a.assistant_name}.`);
      }
    },
    onError: (err: Error) => onToastErr(err.message),
  });

  const regen = useMutation({
    mutationFn: () =>
      api.post<Assistant>(
        `/api/assistants/${assistant.assistant_id}/regenerate-avatar`
      ),
    onSuccess: (a) => {
      onSaved();
      if (a.profile_image_path && !a.avatar_generation_note) {
        onToastOk("New avatar generated.");
      } else {
        onToastErr("Avatar regeneration failed — see details below.");
      }
    },
    onError: (err: Error) => onToastErr(err.message),
  });

  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
      <div className="card lg:col-span-1 self-start">
        <div className="card-body flex flex-col items-center gap-4">
          <AssistantAvatar assistant={assistant} />
          <button
            className="btn-secondary"
            disabled={regen.isPending}
            onClick={() => regen.mutate()}
          >
            <RefreshCw className={regen.isPending ? "h-4 w-4 animate-spin" : "h-4 w-4"} />
            {regen.isPending ? "Generating…" : "Regenerate avatar"}
          </button>
          {assistant.avatar_generation_note && (
            <div className="w-full border border-destructive/30 bg-destructive/5 text-destructive text-xs rounded-md p-3 flex gap-2">
              <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5" />
              <div>
                <div className="font-semibold mb-1">Avatar generation failed</div>
                <div className="whitespace-pre-wrap break-words">
                  {assistant.avatar_generation_note}
                </div>
              </div>
            </div>
          )}
        </div>
      </div>

      <div className="card lg:col-span-2">
        <div className="card-header">
          <div className="card-title">Persona</div>
        </div>
        <div className="card-body">
          <form
            className="grid grid-cols-2 gap-4"
            onSubmit={handleSubmit((v) => save.mutate(v))}
          >
            <Field label="Name" htmlFor="assistant_name">
              <input
                id="assistant_name"
                className="input"
                {...register("assistant_name", { required: true })}
              />
            </Field>
            <Field label="Gender" htmlFor="gender">
              <select id="gender" className="input" {...register("gender")}>
                <option value="">—</option>
                {GENDERS.map((g) => (
                  <option key={g} value={g}>
                    {g}
                  </option>
                ))}
              </select>
            </Field>
            <div className="col-span-2">
              <Field
                label="Email address"
                htmlFor="email_address"
                hint="Gmail address Avi will use to send mail and read the family calendar (free/busy lookups). Optional — leave blank if Avi doesn't have a mailbox yet."
              >
                <input
                  id="email_address"
                  type="email"
                  autoComplete="off"
                  placeholder="avi@example.com"
                  className="input"
                  {...register("email_address")}
                />
              </Field>
            </div>
            <div className="col-span-2">
              <Field
                label="Visual description"
                htmlFor="visual_description"
                hint="Saving triggers a new avatar if this or the name/gender change."
              >
                <textarea
                  id="visual_description"
                  rows={4}
                  className="input"
                  {...register("visual_description")}
                />
              </Field>
            </div>
            <div className="col-span-2">
              <Field
                label="Personality"
                htmlFor="personality_description"
                hint="Used later as part of the live conversation system prompt."
              >
                <textarea
                  id="personality_description"
                  rows={4}
                  className="input"
                  {...register("personality_description")}
                />
              </Field>
            </div>
            <div className="col-span-2 flex justify-end">
              <button type="submit" className="btn-primary" disabled={save.isPending}>
                <Sparkles className="h-4 w-4" />
                {save.isPending ? "Saving…" : "Save changes"}
              </button>
            </div>
          </form>
        </div>
      </div>

      <div className="lg:col-span-3">
        <GoogleAccountSection
          assistant={assistant}
          onToastOk={onToastOk}
          onToastErr={onToastErr}
        />
      </div>
    </div>
  );
}

function GoogleAccountSection({
  assistant,
  onToastOk,
  onToastErr,
}: {
  assistant: Assistant;
  onToastOk: (msg: string) => void;
  onToastErr: (msg: string) => void;
}) {
  const qc = useQueryClient();
  const [events, setEvents] = useState<UpcomingEvent[] | null>(null);

  const { data: status, isLoading } = useQuery<GoogleStatus>({
    queryKey: ["google-status", assistant.assistant_id],
    queryFn: () =>
      api.get<GoogleStatus>(
        `/api/google/status?assistant_id=${assistant.assistant_id}`
      ),
    refetchOnWindowFocus: true,
  });

  const disconnect = useMutation({
    mutationFn: () =>
      api.del(`/api/google/credentials?assistant_id=${assistant.assistant_id}`),
    onSuccess: () => {
      onToastOk("Google account disconnected.");
      setEvents(null);
      qc.invalidateQueries({ queryKey: ["google-status", assistant.assistant_id] });
    },
    onError: (err: Error) => onToastErr(err.message),
  });

  const sendTest = useMutation({
    mutationFn: () =>
      api.post<{ message_id: string; granted_email: string }>(
        "/api/google/test/send-email",
        {
          assistant_id: assistant.assistant_id,
          to: status?.granted_email,
          subject: "Hello from Avi",
          body:
            "This is a test message from your Family Assistant. " +
            "If you're reading it, the Gmail scope is working.",
        }
      ),
    onSuccess: (r) =>
      onToastOk(`Test email sent (id ${r.message_id.slice(0, 8)}…).`),
    onError: (err: Error) =>
      onToastErr(
        err instanceof ApiError ? err.message : `Send failed: ${err.message}`
      ),
  });

  const fetchEvents = useMutation({
    mutationFn: () =>
      api.get<{ events: UpcomingEvent[] }>(
        `/api/google/test/upcoming-events?assistant_id=${assistant.assistant_id}&hours=72&max_results=10`
      ),
    onSuccess: (r) => setEvents(r.events),
    onError: (err: Error) => onToastErr(err.message),
  });

  const startUrl = `/api/admin/google/oauth/start?assistant_id=${assistant.assistant_id}`;

  return (
    <div className="card">
      <div className="card-header">
        <div className="card-title flex items-center gap-2">
          <PlugZap className="h-5 w-5 text-primary" /> Google account
        </div>
      </div>
      <div className="card-body space-y-4">
        <p className="text-sm text-muted-foreground">
          Connect Avi to a Google account so they can send email from
          their Gmail and read events from any calendar shared with them
          (including yours, once you share it from{" "}
          <span className="font-mono">calendar.google.com</span> →
          Settings → Share with specific people).
        </p>

        {isLoading && (
          <div className="text-sm text-muted-foreground">Checking connection…</div>
        )}

        {status && !status.oauth_configured && (
          <div className="border border-amber-300 bg-amber-50 dark:border-amber-700 dark:bg-amber-950/30 text-amber-900 dark:text-amber-100 rounded-md p-3 text-sm flex gap-2">
            <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5" />
            <div>
              <div className="font-semibold mb-1">OAuth client not configured</div>
              <div>
                Set <span className="font-mono">GOOGLE_OAUTH_CLIENT_ID</span> and{" "}
                <span className="font-mono">GOOGLE_OAUTH_CLIENT_SECRET</span> in{" "}
                <span className="font-mono">.env</span> and restart the API. See
                the README section "Google OAuth (Avi's Gmail + Calendar)" for the
                Google Cloud Console steps.
              </div>
            </div>
          </div>
        )}

        {status && status.oauth_configured && !status.connected && (
          <div className="flex flex-col items-start gap-3">
            <a href={startUrl} className="btn-primary">
              <PlugZap className="h-4 w-4" /> Connect with Google
            </a>
            <p className="text-xs text-muted-foreground">
              You'll be sent to Google's consent screen. After approving,
              you'll be redirected back here.
            </p>
          </div>
        )}

        {status && status.connected && (
          <div className="space-y-4">
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-sm">
              <KV
                label="Granted email"
                value={status.granted_email ?? "—"}
                ok={status.email_matches_assistant !== false}
                hint={
                  status.email_matches_assistant === false
                    ? `Doesn't match assistant email "${assistant.email_address}"`
                    : undefined
                }
              />
              <KV
                label="Access token rotates"
                value={
                  status.token_expires_at
                    ? new Date(status.token_expires_at).toLocaleString()
                    : "—"
                }
                hint="Auto-refreshed before each call using a long-lived refresh token. To make the underlying connection itself long-lived, set the OAuth consent screen to 'In production' (Testing mode caps refresh tokens at 7 days)."
              />
              <KV
                label="Can send email"
                value={status.can_send_email ? "yes" : "no"}
                ok={status.can_send_email}
              />
              <KV
                label="Can read calendar"
                value={status.can_read_calendar ? "yes" : "no"}
                ok={status.can_read_calendar}
              />
            </div>

            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                className="btn-secondary"
                disabled={!status.can_send_email || sendTest.isPending}
                onClick={() => sendTest.mutate()}
              >
                <Mail className="h-4 w-4" />
                {sendTest.isPending ? "Sending…" : "Send test email to self"}
              </button>
              <button
                type="button"
                className="btn-secondary"
                disabled={!status.can_read_calendar || fetchEvents.isPending}
                onClick={() => fetchEvents.mutate()}
              >
                <CalendarDays className="h-4 w-4" />
                {fetchEvents.isPending ? "Loading…" : "Show next 72h of events"}
              </button>
              <a href={startUrl} className="btn-secondary">
                <RefreshCw className="h-4 w-4" /> Reconnect
              </a>
              <button
                type="button"
                className="btn-secondary"
                disabled={disconnect.isPending}
                onClick={() => disconnect.mutate()}
              >
                <XCircle className="h-4 w-4" />
                {disconnect.isPending ? "Disconnecting…" : "Disconnect"}
              </button>
            </div>

            {events && events.length === 0 && (
              <div className="text-sm text-muted-foreground">
                No events in the next 72 hours across your readable calendars.
              </div>
            )}
            {events && events.length > 0 && (
              <div className="border border-border rounded-md overflow-hidden">
                <table className="w-full text-sm">
                  <thead className="bg-muted/40 text-muted-foreground">
                    <tr>
                      <th className="text-left px-3 py-2 font-medium">When</th>
                      <th className="text-left px-3 py-2 font-medium">Event</th>
                      <th className="text-left px-3 py-2 font-medium">Calendar</th>
                    </tr>
                  </thead>
                  <tbody>
                    {events.map((e) => (
                      <tr key={`${e.calendar_id}:${e.event_id}`} className="border-t border-border">
                        <td className="px-3 py-2 whitespace-nowrap">
                          {formatEventTime(e.start)}
                        </td>
                        <td className="px-3 py-2">
                          <div className="font-medium">{e.summary}</div>
                          {e.location && (
                            <div className="text-xs text-muted-foreground">{e.location}</div>
                          )}
                        </td>
                        <td className="px-3 py-2 text-xs text-muted-foreground truncate max-w-[18rem]">
                          {e.calendar_id}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function KV({
  label,
  value,
  ok,
  hint,
}: {
  label: string;
  value: string;
  ok?: boolean;
  hint?: string;
}) {
  return (
    <div className="border border-border rounded-md p-3">
      <div className="text-xs uppercase tracking-wide text-muted-foreground mb-1">
        {label}
      </div>
      <div className="flex items-center gap-2 font-mono text-sm break-all">
        {ok === true && <CheckCircle2 className="h-4 w-4 text-emerald-600 shrink-0" />}
        {ok === false && <XCircle className="h-4 w-4 text-destructive shrink-0" />}
        <span>{value}</span>
      </div>
      {hint && <div className="text-xs text-muted-foreground mt-1">{hint}</div>}
    </div>
  );
}

function formatEventTime(iso: string): string {
  if (!iso) return "—";
  // All-day events come through as plain YYYY-MM-DD with no time portion.
  if (/^\d{4}-\d{2}-\d{2}$/.test(iso)) {
    return new Date(iso + "T00:00:00").toLocaleDateString(undefined, {
      weekday: "short",
      month: "short",
      day: "numeric",
    });
  }
  return new Date(iso).toLocaleString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

export function AssistantAvatar({
  assistant,
  size = 200,
}: {
  assistant: Pick<Assistant, "assistant_name" | "profile_image_path">;
  size?: number;
}) {
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

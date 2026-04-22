import { useEffect, useRef, useState } from "react";
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
  Upload,
  XCircle,
} from "lucide-react";
import { api, ApiError } from "@/lib/api";
import type { Assistant } from "@/lib/types";
import { PageHeader } from "@/components/PageHeader";
import { Field } from "@/components/Field";
import { useToast } from "@/components/Toast";
import { AssistantAvatar } from "@/components/AssistantAvatar";
import { GENDERS } from "@/lib/enums";

type GoogleStatus = {
  connected: boolean;
  granted_email: string | null;
  scopes: string[];
  token_expires_at: string | null;
  can_send_email: boolean;
  can_read_inbox: boolean;
  can_read_calendar: boolean;
  can_write_calendar: boolean;
  can_list_calendars: boolean;
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

type VisibleCalendar = {
  calendar_id: string;
  summary: string;
  summary_override: string | null;
  description: string | null;
  primary: boolean;
  selected: boolean;
  // owner | writer | reader | freeBusyReader | none | unknown
  access_role: string;
  background_color: string | null;
  foreground_color: string | null;
  time_zone: string | null;
  can_read_events: boolean;
  can_write: boolean;
};

type VisibleCalendarsResponse = {
  granted_email: string;
  calendars: VisibleCalendar[];
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
    qc.invalidateQueries({ queryKey: ["google-calendars"] });
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
    onError: (err: Error) =>
      toast.error(`Could not create assistant: ${err.message}`),
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
  const fileInputRef = useRef<HTMLInputElement>(null);

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
      onToastOk(`Saved ${a.assistant_name}.`);
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

  const upload = useMutation({
    mutationFn: (file: File) => {
      const form = new FormData();
      form.append("file", file);
      return api.upload<Assistant>(
        `/api/assistants/${assistant.assistant_id}/upload-avatar`,
        form
      );
    },
    onSuccess: (a) => {
      onSaved();
      onToastOk(`New avatar uploaded for ${a.assistant_name}.`);
      if (fileInputRef.current) fileInputRef.current.value = "";
    },
    onError: (err: Error) => {
      onToastErr(err.message);
      if (fileInputRef.current) fileInputRef.current.value = "";
    },
  });

  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
      <div className="card lg:col-span-1 self-start">
        <div className="card-body flex flex-col items-center gap-4">
          <AssistantAvatar assistant={assistant} />
          <p className="text-xs text-muted-foreground text-center -mt-2">
            Saving the persona never changes the avatar — use the buttons
            below to swap it.
          </p>
          <div className="flex flex-col gap-2 w-full">
            <button
              type="button"
              className="btn-secondary justify-center"
              disabled={regen.isPending || upload.isPending}
              onClick={() => regen.mutate()}
              title="Ask Gemini for a new portrait based on the current name, gender, and visual description."
            >
              <RefreshCw className={regen.isPending ? "h-4 w-4 animate-spin" : "h-4 w-4"} />
              {regen.isPending ? "Generating…" : "Regenerate with Gemini"}
            </button>
            <button
              type="button"
              className="btn-secondary justify-center"
              disabled={regen.isPending || upload.isPending}
              onClick={() => fileInputRef.current?.click()}
              title="Upload a PNG, JPEG, WebP, or GIF to use as the avatar instead of an AI-generated image."
            >
              <Upload className={upload.isPending ? "h-4 w-4 animate-pulse" : "h-4 w-4"} />
              {upload.isPending ? "Uploading…" : "Upload image"}
            </button>
            <input
              ref={fileInputRef}
              type="file"
              accept="image/png,image/jpeg,image/webp,image/gif"
              className="hidden"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) upload.mutate(file);
              }}
            />
          </div>
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
                hint="Used by Regenerate with Gemini. Editing this field will not change the avatar on its own."
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

  // Per-calendar visibility + access role. Replaces the old single
  // "Can read calendar" yes/no with the actual list of calendars Avi
  // can see and what level of share Google grants on each.
  // Only fires when we know we're connected to avoid an extra Google
  // round-trip on every status refresh.
  const {
    data: visibleCalendars,
    isLoading: calendarsLoading,
    error: calendarsError,
    refetch: refetchCalendars,
  } = useQuery<VisibleCalendarsResponse>({
    queryKey: ["google-calendars", assistant.assistant_id],
    queryFn: () =>
      api.get<VisibleCalendarsResponse>(
        `/api/google/test/calendars?assistant_id=${assistant.assistant_id}`
      ),
    // calendarList requires its own scope (calendar.calendarlist.readonly).
    // Skip the call when the token doesn't carry it — the panel renders
    // a "reconnect to enable" hint instead of triggering a 403.
    enabled: !!status?.connected && !!status?.can_list_calendars,
    refetchOnWindowFocus: false,
    staleTime: 60_000,
  });

  const disconnect = useMutation({
    mutationFn: () =>
      api.del(`/api/google/credentials?assistant_id=${assistant.assistant_id}`),
    onSuccess: () => {
      onToastOk("Google account disconnected.");
      setEvents(null);
      qc.invalidateQueries({ queryKey: ["google-status", assistant.assistant_id] });
      qc.invalidateQueries({ queryKey: ["google-calendars", assistant.assistant_id] });
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
                label="Can read inbox & auto-reply"
                value={status.can_read_inbox ? "yes" : "no"}
                ok={status.can_read_inbox}
                hint={
                  status.can_read_inbox
                    ? "Avi polls the inbox every minute and replies to mail from registered family members."
                    : "Reconnect Google to grant the gmail.modify scope. Until then, Avi only sends outbound mail and ignores incoming messages."
                }
              />
              <KV
                label="Calendar OAuth scope"
                value={
                  status.can_write_calendar
                    ? "read + write"
                    : status.can_read_calendar
                    ? "read only (legacy)"
                    : "missing"
                }
                ok={status.can_read_calendar}
                hint={
                  status.can_write_calendar
                    ? "calendar.events granted — Avi can list events AND add holds. Per-calendar access depends on how each owner shares (see table below)."
                    : status.can_read_calendar
                    ? "Legacy calendar.readonly scope: Avi can list events but cannot add holds. Disconnect + reconnect to upgrade to calendar.events."
                    : "Reconnect Google to grant calendar.events. Until then, Avi can't list events or add holds."
                }
              />
            </div>

            <CalendarVisibilityPanel
              status={status}
              data={visibleCalendars}
              loading={calendarsLoading}
              error={calendarsError as Error | null}
              onRefresh={() => refetchCalendars()}
            />

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

// Friendly labels + per-row colour for Google's accessRole values. We
// also surface the "this share level can't list events" gotcha here
// because that's the root cause of the 403 warnings the agent logs
// when it tries to read events on a free/busy-only share.
function describeAccessRole(role: string): {
  label: string;
  tone: "ok" | "warn" | "muted";
  description: string;
} {
  switch (role) {
    case "owner":
      return {
        label: "Owner",
        tone: "ok",
        description:
          "Full read + write. Avi can list events and add holds here.",
      };
    case "writer":
      return {
        label: "Writer",
        tone: "ok",
        description:
          "Read + write. Avi can list events and add holds here.",
      };
    case "reader":
      return {
        label: "Read only",
        tone: "warn",
        description:
          "Avi can see event titles, locations, and times but cannot add holds. Re-share with 'Make changes to events' to enable writes.",
      };
    case "freeBusyReader":
      return {
        label: "Free/busy only",
        tone: "warn",
        description:
          "Avi can only see busy/free intervals — listing individual events returns 403 (Google logs this as a warning). Re-share with 'See all event details' to expose titles, or 'Make changes to events' to also allow writes.",
      };
    case "none":
      return {
        label: "No access",
        tone: "warn",
        description:
          "The calendar is in Avi's list but has no read access at all. Ask the owner to re-share.",
      };
    default:
      return {
        label: role || "unknown",
        tone: "muted",
        description: "Unrecognised access level — check Google Calendar settings.",
      };
  }
}

function CalendarVisibilityPanel({
  status,
  data,
  loading,
  error,
  onRefresh,
}: {
  status: GoogleStatus;
  data: VisibleCalendarsResponse | undefined;
  loading: boolean;
  error: Error | null;
  onRefresh: () => void;
}) {
  if (!status.can_read_calendar) {
    // Without any calendar scope we can't even hit calendarList.
    return null;
  }

  return (
    <div className="border border-border rounded-md">
      <div className="flex items-center justify-between px-3 py-2 bg-muted/30 border-b border-border">
        <div className="text-sm font-medium flex items-center gap-2">
          <CalendarDays className="h-4 w-4 text-muted-foreground" />
          Visible calendars + permission level
        </div>
        <button
          type="button"
          onClick={onRefresh}
          disabled={loading || !status.can_list_calendars}
          className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground disabled:opacity-60"
          title="Re-query Google for the calendar list."
        >
          <RefreshCw className={`h-3 w-3 ${loading ? "animate-spin" : ""}`} />
          Refresh
        </button>
      </div>

      {!status.can_list_calendars && (
        <div className="px-3 py-3 text-sm flex items-start gap-2 text-amber-800 dark:text-amber-200 bg-amber-50/40 dark:bg-amber-950/10">
          <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5" />
          <div>
            <div className="font-medium mb-1">
              Calendar list scope not granted
            </div>
            <div>
              Avi can read events and add holds on calendars whose ids
              we already know (which is why messages and event creation
              still work), but Google requires a separate scope to{" "}
              <span className="font-mono">enumerate</span> every
              calendar shared with this account. Click <strong>
                Reconnect
              </strong> above to grant{" "}
              <span className="font-mono">
                calendar.calendarlist.readonly
              </span>{" "}
              and this panel will populate.
            </div>
          </div>
        </div>
      )}

      {status.can_list_calendars && loading && !data && (
        <div className="px-3 py-3 text-sm text-muted-foreground">
          Querying Google for visible calendars…
        </div>
      )}

      {error && (
        <div className="px-3 py-3 text-sm text-destructive">
          Could not load calendar list: {error.message}
        </div>
      )}

      {data && data.calendars.length === 0 && (
        <div className="px-3 py-3 text-sm text-muted-foreground">
          No calendars are visible to {data.granted_email}. Ask family
          members to share their calendars with this account from
          Google Calendar → Settings → Share with specific people.
        </div>
      )}

      {data && data.calendars.length > 0 && (
        <>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-muted/20 text-muted-foreground">
                <tr>
                  <th className="text-left px-3 py-2 font-medium">Calendar</th>
                  <th className="text-left px-3 py-2 font-medium">Calendar id</th>
                  <th className="text-left px-3 py-2 font-medium">Access</th>
                  <th className="text-left px-3 py-2 font-medium whitespace-nowrap">
                    List events
                  </th>
                  <th className="text-left px-3 py-2 font-medium whitespace-nowrap">
                    Add holds
                  </th>
                </tr>
              </thead>
              <tbody>
                {data.calendars.map((c) => {
                  const role = describeAccessRole(c.access_role);
                  const displayName = c.summary_override || c.summary;
                  return (
                    <tr key={c.calendar_id} className="border-t border-border">
                      <td className="px-3 py-2 align-top">
                        <div className="flex items-center gap-2">
                          {c.background_color && (
                            <span
                              className="h-3 w-3 rounded-sm shrink-0 border border-border"
                              style={{ backgroundColor: c.background_color }}
                            />
                          )}
                          <div>
                            <div className="font-medium">
                              {displayName}
                              {c.primary && (
                                <span className="ml-2 text-[10px] uppercase tracking-wide text-primary">
                                  primary
                                </span>
                              )}
                            </div>
                            {c.description && (
                              <div className="text-xs text-muted-foreground line-clamp-2">
                                {c.description}
                              </div>
                            )}
                          </div>
                        </div>
                      </td>
                      <td className="px-3 py-2 align-top text-xs text-muted-foreground font-mono break-all max-w-[16rem]">
                        {c.calendar_id}
                      </td>
                      <td className="px-3 py-2 align-top">
                        <div
                          className={
                            role.tone === "ok"
                              ? "inline-flex items-center gap-1 text-emerald-700 dark:text-emerald-400"
                              : role.tone === "warn"
                              ? "inline-flex items-center gap-1 text-amber-700 dark:text-amber-400"
                              : "inline-flex items-center gap-1 text-muted-foreground"
                          }
                          title={role.description}
                        >
                          {role.tone === "ok" ? (
                            <CheckCircle2 className="h-4 w-4" />
                          ) : role.tone === "warn" ? (
                            <AlertTriangle className="h-4 w-4" />
                          ) : null}
                          <span className="font-medium">{role.label}</span>
                        </div>
                        <div className="text-xs text-muted-foreground mt-1 max-w-[28rem]">
                          {role.description}
                        </div>
                      </td>
                      <td className="px-3 py-2 align-top">
                        {c.can_read_events ? (
                          <CheckCircle2 className="h-4 w-4 text-emerald-600" />
                        ) : (
                          <XCircle
                            className="h-4 w-4 text-amber-600"
                            aria-label="Cannot list events — produces 403 in agent logs"
                          />
                        )}
                      </td>
                      <td className="px-3 py-2 align-top">
                        {c.can_write ? (
                          <CheckCircle2 className="h-4 w-4 text-emerald-600" />
                        ) : (
                          <XCircle className="h-4 w-4 text-muted-foreground" />
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div className="px-3 py-2 border-t border-border bg-muted/20 text-xs text-muted-foreground">
            403 "insufficientPermissions" warnings in the API logs are
            expected when Avi's agent tries to list events on a
            calendar shared with "Free/busy only" — it falls back to
            free/busy queries and the user reply still goes out. Share
            the calendar with "See all event details" to silence them.
          </div>
        </>
      )}
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


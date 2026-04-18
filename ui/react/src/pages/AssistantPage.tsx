import { useEffect } from "react";
import { useParams } from "react-router-dom";
import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { Bot, RefreshCw, Sparkles, AlertTriangle } from "lucide-react";
import { api } from "@/lib/api";
import type { Assistant } from "@/lib/types";
import { PageHeader } from "@/components/PageHeader";
import { Field } from "@/components/Field";
import { useToast } from "@/components/Toast";
import { GENDERS } from "@/lib/enums";

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
    </div>
  );
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

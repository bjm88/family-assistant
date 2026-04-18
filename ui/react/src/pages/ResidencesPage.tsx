import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import {
  Home,
  Image as ImageIcon,
  Pencil,
  Plus,
  Star,
  Trash2,
  Upload,
} from "lucide-react";
import { api } from "@/lib/api";
import type { Residence, ResidencePhoto } from "@/lib/types";
import { PageHeader } from "@/components/PageHeader";
import { EmptyState } from "@/components/EmptyState";
import { Modal } from "@/components/Modal";
import { Field } from "@/components/Field";
import { useToast } from "@/components/Toast";
import { cleanPayload } from "@/lib/form";

type ResidenceForm = {
  label: string;
  street_line_1: string;
  street_line_2?: string;
  city: string;
  state_or_region?: string;
  postal_code?: string;
  country?: string;
  is_primary_residence: boolean;
  notes?: string;
};

export default function ResidencesPage() {
  const { familyId: familyIdParam } = useParams();
  const familyId = Number(familyIdParam);
  const qc = useQueryClient();
  const toast = useToast();
  const [editingId, setEditingId] = useState<number | "new" | null>(null);

  const { data: residences } = useQuery<Residence[]>({
    queryKey: ["residences", familyId],
    queryFn: () =>
      api.get<Residence[]>(`/api/residences?family_id=${familyId}`),
    enabled: Number.isFinite(familyId),
  });

  const del = useMutation({
    mutationFn: (id: number) => api.del(`/api/residences/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["residences", familyId] });
      toast.success("Residence removed.");
    },
    onError: (err: Error) =>
      toast.error(`Could not remove residence: ${err.message}`),
  });

  const editing =
    typeof editingId === "number"
      ? residences?.find((r) => r.residence_id === editingId) ?? null
      : null;

  return (
    <div>
      <PageHeader
        title="Residences"
        description="The family's homes — main house, vacation properties, rentals."
        actions={
          <button className="btn-primary" onClick={() => setEditingId("new")}>
            <Plus className="h-4 w-4" /> Add residence
          </button>
        }
      />

      {!residences || residences.length === 0 ? (
        <EmptyState
          icon={Home}
          title="No residences yet"
          description="Add the family's primary home and any secondary properties."
          action={
            <button className="btn-primary" onClick={() => setEditingId("new")}>
              <Plus className="h-4 w-4" /> Add your first residence
            </button>
          }
        />
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {residences.map((r) => (
            <button
              key={r.residence_id}
              className="card text-left hover:shadow-md transition-shadow"
              onClick={() => setEditingId(r.residence_id)}
            >
              <div className="aspect-video bg-muted overflow-hidden rounded-t-lg">
                {r.cover_photo_path ? (
                  <img
                    src={`/api/media/${r.cover_photo_path}`}
                    alt={r.label}
                    className="h-full w-full object-cover"
                  />
                ) : (
                  <div className="h-full w-full flex items-center justify-center text-muted-foreground">
                    <Home className="h-10 w-10" />
                  </div>
                )}
              </div>
              <div className="card-body">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="font-semibold truncate flex items-center gap-2">
                      {r.label}
                      {r.is_primary_residence && (
                        <span
                          className="badge inline-flex items-center gap-1"
                          title="Primary residence"
                        >
                          <Star className="h-3 w-3" /> Primary
                        </span>
                      )}
                    </div>
                    <div className="text-sm text-muted-foreground truncate">
                      {r.street_line_1}
                    </div>
                    <div className="text-xs text-muted-foreground truncate">
                      {[r.city, r.state_or_region, r.postal_code]
                        .filter(Boolean)
                        .join(", ")}
                    </div>
                  </div>
                  <div className="flex gap-1">
                    <span
                      className="text-muted-foreground hover:text-foreground"
                      title="Edit"
                    >
                      <Pencil className="h-4 w-4" />
                    </span>
                    <button
                      className="text-destructive hover:text-destructive/80"
                      onClick={(e) => {
                        e.stopPropagation();
                        if (confirm(`Delete ${r.label}?`))
                          del.mutate(r.residence_id);
                      }}
                      aria-label="Delete residence"
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </div>
                </div>
              </div>
            </button>
          ))}
        </div>
      )}

      <ResidenceModal
        open={editingId !== null}
        mode={editingId === "new" ? "create" : "edit"}
        residence={editing}
        familyId={familyId}
        onClose={() => setEditingId(null)}
      />
    </div>
  );
}

function ResidenceModal({
  open,
  mode,
  residence,
  familyId,
  onClose,
}: {
  open: boolean;
  mode: "create" | "edit";
  residence: Residence | null;
  familyId: number;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const { register, handleSubmit, reset } = useForm<ResidenceForm>({
    defaultValues: {
      label: "",
      street_line_1: "",
      city: "",
      country: "United States",
      is_primary_residence: false,
    },
  });

  useEffect(() => {
    if (!open) return;
    if (mode === "edit" && residence) {
      reset({
        label: residence.label,
        street_line_1: residence.street_line_1,
        street_line_2: residence.street_line_2 ?? "",
        city: residence.city,
        state_or_region: residence.state_or_region ?? "",
        postal_code: residence.postal_code ?? "",
        country: residence.country,
        is_primary_residence: residence.is_primary_residence,
        notes: residence.notes ?? "",
      });
    } else {
      reset({
        label: "",
        street_line_1: "",
        street_line_2: "",
        city: "",
        state_or_region: "",
        postal_code: "",
        country: "United States",
        is_primary_residence: false,
        notes: "",
      });
    }
  }, [mode, residence, reset, open]);

  const create = useMutation({
    mutationFn: (v: ResidenceForm) => {
      const cleaned = cleanPayload({ ...v, family_id: familyId });
      if (cleaned.is_primary_residence === undefined) {
        cleaned.is_primary_residence = false;
      }
      return api.post<Residence>("/api/residences", cleaned);
    },
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["residences", familyId] });
      toast.success(`Added ${r.label}.`);
      onClose();
    },
    onError: (err: Error) =>
      toast.error(`Could not add residence: ${err.message}`),
  });

  const update = useMutation({
    mutationFn: (v: ResidenceForm) => {
      if (!residence) throw new Error("No residence to update");
      const cleaned = cleanPayload(v);
      return api.patch<Residence>(
        `/api/residences/${residence.residence_id}`,
        cleaned
      );
    },
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ["residences", familyId] });
      toast.success(`Saved ${r.label}.`);
      onClose();
    },
    onError: (err: Error) => toast.error(`Save failed: ${err.message}`),
  });

  const pending = create.isPending || update.isPending;

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={mode === "create" ? "Add residence" : "Edit residence"}
      wide={mode === "edit"}
      footer={
        <>
          <button className="btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn-primary"
            disabled={pending}
            onClick={handleSubmit((v) =>
              mode === "create" ? create.mutate(v) : update.mutate(v)
            )}
          >
            {pending
              ? "Saving…"
              : mode === "create"
              ? "Add residence"
              : "Save changes"}
          </button>
        </>
      }
    >
      <form
        className="grid grid-cols-2 gap-4"
        onSubmit={(e) => e.preventDefault()}
      >
        <div className="col-span-2">
          <Field
            label="Label"
            htmlFor="label"
            hint='A short nickname — "Main house", "Lake cabin", etc.'
          >
            <input
              id="label"
              className="input"
              placeholder="Main house"
              {...register("label", { required: true })}
            />
          </Field>
        </div>
        <div className="col-span-2">
          <Field label="Street address" htmlFor="street_line_1">
            <input
              id="street_line_1"
              className="input"
              placeholder="123 Main St"
              {...register("street_line_1", { required: true })}
            />
          </Field>
        </div>
        <div className="col-span-2">
          <Field
            label="Street address (line 2)"
            htmlFor="street_line_2"
            hint="Apartment, suite, unit, etc. Optional."
          >
            <input
              id="street_line_2"
              className="input"
              {...register("street_line_2")}
            />
          </Field>
        </div>
        <Field label="City" htmlFor="city">
          <input
            id="city"
            className="input"
            {...register("city", { required: true })}
          />
        </Field>
        <Field label="State / region" htmlFor="state_or_region">
          <input
            id="state_or_region"
            className="input"
            {...register("state_or_region")}
          />
        </Field>
        <Field label="Postal code" htmlFor="postal_code">
          <input
            id="postal_code"
            className="input"
            {...register("postal_code")}
          />
        </Field>
        <Field label="Country" htmlFor="country">
          <input id="country" className="input" {...register("country")} />
        </Field>
        <div className="col-span-2 flex items-center gap-2">
          <input
            id="is_primary_residence"
            type="checkbox"
            className="h-4 w-4"
            {...register("is_primary_residence")}
          />
          <label
            htmlFor="is_primary_residence"
            className="text-sm flex items-center gap-1 cursor-pointer"
          >
            <Star className="h-4 w-4" /> Primary residence
          </label>
          <span className="text-xs text-muted-foreground ml-2">
            Only one residence per family can be primary — checking this will
            automatically unset the others.
          </span>
        </div>
        <div className="col-span-2">
          <Field label="Notes" htmlFor="notes">
            <textarea
              id="notes"
              rows={3}
              className="input"
              placeholder="HOA details, gate codes, landlord contact…"
              {...register("notes")}
            />
          </Field>
        </div>
      </form>

      {mode === "edit" && residence && (
        <div className="mt-6 border-t border-border pt-4">
          <ResidencePhotosSection residence={residence} />
        </div>
      )}
    </Modal>
  );
}

// ---------- Photos --------------------------------------------------------

function ResidencePhotosSection({ residence }: { residence: Residence }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const { data: photos } = useQuery<ResidencePhoto[]>({
    queryKey: ["residence-photos", residence.residence_id],
    queryFn: () =>
      api.get<ResidencePhoto[]>(
        `/api/residence-photos?residence_id=${residence.residence_id}`
      ),
  });

  type PhotoForm = { title: string; description?: string };
  const { register, handleSubmit, reset } = useForm<PhotoForm>();

  const upload = useMutation({
    mutationFn: async (v: PhotoForm) => {
      const file = fileRef.current?.files?.[0];
      if (!file) throw new Error("Please choose a photo.");
      const form = new FormData();
      form.append("file", file);
      form.append("residence_id", String(residence.residence_id));
      form.append("title", v.title);
      if (v.description) form.append("description", v.description);
      return api.upload<ResidencePhoto>("/api/residence-photos", form);
    },
    onSuccess: () => {
      qc.invalidateQueries({
        queryKey: ["residence-photos", residence.residence_id],
      });
      qc.invalidateQueries({ queryKey: ["residences", residence.family_id] });
      setOpen(false);
      reset({ title: "", description: "" });
      if (fileRef.current) fileRef.current.value = "";
      toast.success("Photo added.");
    },
    onError: (err: Error) => toast.error(`Upload failed: ${err.message}`),
  });

  const del = useMutation({
    mutationFn: (id: number) => api.del(`/api/residence-photos/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({
        queryKey: ["residence-photos", residence.residence_id],
      });
      qc.invalidateQueries({ queryKey: ["residences", residence.family_id] });
      toast.success("Photo removed.");
    },
    onError: (err: Error) => toast.error(err.message),
  });

  return (
    <div>
      <div className="flex items-center justify-between mb-3">
        <div className="text-sm font-medium">
          Photos
          {photos && photos.length > 0 && (
            <span className="badge ml-2">{photos.length}</span>
          )}
        </div>
        <button
          type="button"
          className="btn-secondary"
          onClick={() => setOpen(true)}
        >
          <Upload className="h-4 w-4" /> Add photo
        </button>
      </div>

      {!photos || photos.length === 0 ? (
        <div className="text-xs text-muted-foreground flex items-center gap-2">
          <ImageIcon className="h-4 w-4" />
          No photos yet. Upload a few shots of {residence.label}.
        </div>
      ) : (
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-4">
          {photos.map((p) => (
            <div
              key={p.residence_photo_id}
              className="border border-border rounded-lg overflow-hidden flex flex-col bg-white"
            >
              <div className="aspect-square bg-muted overflow-hidden">
                <img
                  src={`/api/media/${p.stored_file_path}`}
                  alt={p.title}
                  className="h-full w-full object-cover"
                />
              </div>
              <div className="p-3 flex-1 flex flex-col gap-2">
                <div className="font-medium text-sm truncate">{p.title}</div>
                {p.description && (
                  <div className="text-xs text-muted-foreground line-clamp-2">
                    {p.description}
                  </div>
                )}
                <button
                  type="button"
                  className="text-destructive hover:text-destructive/80 text-xs inline-flex items-center gap-1 self-start mt-auto"
                  onClick={() => {
                    if (confirm(`Delete "${p.title}"?`))
                      del.mutate(p.residence_photo_id);
                  }}
                >
                  <Trash2 className="h-3.5 w-3.5" /> Remove
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      <Modal
        open={open}
        onClose={() => {
          setOpen(false);
          reset({ title: "", description: "" });
        }}
        title={`Add photo of ${residence.label}`}
        footer={
          <>
            <button
              type="button"
              className="btn-secondary"
              onClick={() => {
                setOpen(false);
                reset({ title: "", description: "" });
              }}
            >
              Cancel
            </button>
            <button
              type="button"
              className="btn-primary"
              disabled={upload.isPending}
              onClick={handleSubmit((v) => upload.mutate(v))}
            >
              {upload.isPending ? "Uploading…" : "Upload"}
            </button>
          </>
        }
      >
        <form className="space-y-4" onSubmit={(e) => e.preventDefault()}>
          <Field label="Title" htmlFor="residence_photo_title">
            <input
              id="residence_photo_title"
              className="input"
              placeholder="e.g. Front exterior"
              {...register("title", { required: true })}
            />
          </Field>
          <Field label="Photo" htmlFor="residence_photo_file">
            <input
              id="residence_photo_file"
              ref={fileRef}
              type="file"
              accept="image/*"
              className="input"
            />
          </Field>
          <Field label="Description" htmlFor="residence_photo_description">
            <textarea
              id="residence_photo_description"
              rows={2}
              className="input"
              placeholder="Context for the photo (optional)"
              {...register("description")}
            />
          </Field>
        </form>
      </Modal>
    </div>
  );
}

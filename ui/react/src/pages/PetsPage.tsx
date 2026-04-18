import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { Image as ImageIcon, PawPrint, Pencil, Plus, Trash2, Upload } from "lucide-react";
import { api } from "@/lib/api";
import type { Pet, PetPhoto } from "@/lib/types";
import { PageHeader } from "@/components/PageHeader";
import { EmptyState } from "@/components/EmptyState";
import { Modal } from "@/components/Modal";
import { Field } from "@/components/Field";
import { useToast } from "@/components/Toast";
import { cleanPayload } from "@/lib/form";
import { PET_ANIMAL_TYPES } from "@/lib/enums";

// If the user selects the "Other" pulldown option we show a free-text input
// for the actual species. On save we prefer that value so the DB row always
// has the most specific label available.
const OTHER = "other";

type PetForm = {
  pet_name: string;
  animal_type_choice: string;
  animal_type_other?: string;
  breed?: string;
  color?: string;
  date_of_birth?: string;
  notes?: string;
};

export default function PetsPage() {
  const { familyId: familyIdParam } = useParams();
  // Normalize to a number once. The mutation below uses the same number in its
  // invalidateQueries call; if we leave the string form here and pass Number()
  // to the modal, the two query keys mismatch and the list silently fails to
  // refresh after add/edit.
  const familyId = Number(familyIdParam);
  const qc = useQueryClient();
  const toast = useToast();
  const [editingId, setEditingId] = useState<number | "new" | null>(null);

  const { data: pets } = useQuery<Pet[]>({
    queryKey: ["pets", familyId],
    queryFn: () => api.get<Pet[]>(`/api/pets?family_id=${familyId}`),
    enabled: Number.isFinite(familyId),
  });

  const del = useMutation({
    mutationFn: (id: number) => api.del(`/api/pets/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["pets", familyId] });
      toast.success("Pet removed.");
    },
    onError: (err: Error) => toast.error(`Could not remove pet: ${err.message}`),
  });

  const editingPet =
    typeof editingId === "number"
      ? pets?.find((p) => p.pet_id === editingId) ?? null
      : null;

  return (
    <div>
      <PageHeader
        title="Pets"
        description="The four-legged (and scaly, and feathered) members of the family."
        actions={
          <button className="btn-primary" onClick={() => setEditingId("new")}>
            <Plus className="h-4 w-4" /> Add pet
          </button>
        }
      />

      {!pets || pets.length === 0 ? (
        <EmptyState
          icon={PawPrint}
          title="No pets yet"
          description="Add the family's pets — dogs, cats, rabbits, reptiles, even a stray chicken."
          action={
            <button className="btn-primary" onClick={() => setEditingId("new")}>
              <Plus className="h-4 w-4" /> Add your first pet
            </button>
          }
        />
      ) : (
        <div className="card">
          <div className="card-body">
            <table className="w-full text-sm">
              <thead className="text-xs text-muted-foreground">
                <tr className="border-b border-border">
                  <th className="text-left py-2">Name</th>
                  <th className="text-left">Animal</th>
                  <th className="text-left">Breed</th>
                  <th className="text-left">Color</th>
                  <th className="text-left">Born</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {pets.map((p) => (
                  <tr
                    key={p.pet_id}
                    className="border-b border-border table-row-hover cursor-pointer"
                    onClick={() => setEditingId(p.pet_id)}
                  >
                    <td className="py-2 font-medium">{p.pet_name}</td>
                    <td>{p.animal_type.replace(/_/g, " ")}</td>
                    <td>{p.breed ?? "—"}</td>
                    <td>{p.color ?? "—"}</td>
                    <td>{p.date_of_birth ?? "—"}</td>
                    <td className="text-right whitespace-nowrap">
                      <button
                        className="text-muted-foreground hover:text-foreground mr-3"
                        onClick={(e) => {
                          e.stopPropagation();
                          setEditingId(p.pet_id);
                        }}
                        aria-label="Edit pet"
                      >
                        <Pencil className="h-4 w-4" />
                      </button>
                      <button
                        className="text-destructive hover:text-destructive/80"
                        onClick={(e) => {
                          e.stopPropagation();
                          if (confirm(`Delete ${p.pet_name}?`))
                            del.mutate(p.pet_id);
                        }}
                        aria-label="Delete pet"
                      >
                        <Trash2 className="h-4 w-4" />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <PetModal
        open={editingId !== null}
        mode={editingId === "new" ? "create" : "edit"}
        pet={editingPet}
        familyId={familyId}
        onClose={() => setEditingId(null)}
      />
    </div>
  );
}

function PetModal({
  open,
  mode,
  pet,
  familyId,
  onClose,
}: {
  open: boolean;
  mode: "create" | "edit";
  pet: Pet | null;
  familyId: number;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const { register, handleSubmit, reset, watch } = useForm<PetForm>({
    defaultValues: { animal_type_choice: "dog" },
  });
  const choice = watch("animal_type_choice");

  useEffect(() => {
    if (!open) return;
    if (mode === "edit" && pet) {
      const known = (PET_ANIMAL_TYPES as readonly string[]).includes(
        pet.animal_type
      );
      reset({
        pet_name: pet.pet_name,
        animal_type_choice: known ? pet.animal_type : OTHER,
        animal_type_other: known ? "" : pet.animal_type,
        breed: pet.breed ?? "",
        color: pet.color ?? "",
        date_of_birth: pet.date_of_birth ?? "",
        notes: pet.notes ?? "",
      });
    } else {
      reset({
        pet_name: "",
        animal_type_choice: "dog",
        animal_type_other: "",
        breed: "",
        color: "",
        date_of_birth: "",
        notes: "",
      });
    }
  }, [mode, pet, reset, open]);

  function resolveAnimalType(v: PetForm): string {
    if (v.animal_type_choice === OTHER) {
      return (v.animal_type_other ?? "").trim() || OTHER;
    }
    return v.animal_type_choice;
  }

  const create = useMutation({
    mutationFn: (v: PetForm) => {
      const animal_type = resolveAnimalType(v);
      const cleaned = cleanPayload({
        pet_name: v.pet_name,
        animal_type,
        breed: v.breed,
        color: v.color,
        date_of_birth: v.date_of_birth,
        notes: v.notes,
        family_id: familyId,
      });
      return api.post<Pet>("/api/pets", cleaned);
    },
    onSuccess: (p) => {
      qc.invalidateQueries({ queryKey: ["pets", familyId] });
      toast.success(`Added ${p.pet_name}.`);
      onClose();
    },
    onError: (err: Error) => toast.error(`Could not add pet: ${err.message}`),
  });

  const update = useMutation({
    mutationFn: (v: PetForm) => {
      if (!pet) throw new Error("No pet to update");
      const animal_type = resolveAnimalType(v);
      const cleaned = cleanPayload({
        pet_name: v.pet_name,
        animal_type,
        breed: v.breed,
        color: v.color,
        date_of_birth: v.date_of_birth,
        notes: v.notes,
      });
      return api.patch<Pet>(`/api/pets/${pet.pet_id}`, cleaned);
    },
    onSuccess: (p) => {
      qc.invalidateQueries({ queryKey: ["pets", familyId] });
      toast.success(`Saved ${p.pet_name}.`);
      onClose();
    },
    onError: (err: Error) => toast.error(`Save failed: ${err.message}`),
  });

  const pending = create.isPending || update.isPending;

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={mode === "create" ? "Add pet" : "Edit pet"}
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
            {pending ? "Saving…" : mode === "create" ? "Add pet" : "Save changes"}
          </button>
        </>
      }
    >
      <form className="grid grid-cols-2 gap-4" onSubmit={(e) => e.preventDefault()}>
        <Field label="Name" htmlFor="pet_name">
          <input
            id="pet_name"
            className="input"
            placeholder="Biscuit"
            {...register("pet_name", { required: true })}
          />
        </Field>
        <Field label="Animal" htmlFor="animal_type_choice">
          <select
            id="animal_type_choice"
            className="input"
            {...register("animal_type_choice", { required: true })}
          >
            {PET_ANIMAL_TYPES.map((t) => (
              <option key={t} value={t}>
                {t.replace(/_/g, " ")}
              </option>
            ))}
          </select>
        </Field>
        {choice === OTHER && (
          <div className="col-span-2">
            <Field
              label="Other animal (describe)"
              htmlFor="animal_type_other"
              hint='Free-form species label, e.g. "hedgehog" or "chinchilla".'
            >
              <input
                id="animal_type_other"
                className="input"
                {...register("animal_type_other")}
              />
            </Field>
          </div>
        )}
        <Field label="Breed" htmlFor="breed">
          <input id="breed" className="input" {...register("breed")} />
        </Field>
        <Field label="Color" htmlFor="color">
          <input id="color" className="input" {...register("color")} />
        </Field>
        <Field label="Date of birth" htmlFor="date_of_birth">
          <input
            id="date_of_birth"
            type="date"
            className="input"
            {...register("date_of_birth")}
          />
        </Field>
        <div className="col-span-2">
          <Field label="Notes" htmlFor="notes">
            <textarea
              id="notes"
              rows={3}
              className="input"
              placeholder="Quirks, diet, vet, favorite toys…"
              {...register("notes")}
            />
          </Field>
        </div>
      </form>

      {mode === "edit" && pet && (
        <div className="mt-6 border-t border-border pt-4">
          <PetPhotosSection pet={pet} />
        </div>
      )}
    </Modal>
  );
}

// ---------- Photos --------------------------------------------------------

function PetPhotosSection({ pet }: { pet: Pet }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const { data: photos } = useQuery<PetPhoto[]>({
    queryKey: ["pet-photos", pet.pet_id],
    queryFn: () =>
      api.get<PetPhoto[]>(`/api/pet-photos?pet_id=${pet.pet_id}`),
  });

  type PhotoForm = { title: string; description?: string };
  const { register, handleSubmit, reset } = useForm<PhotoForm>();

  const upload = useMutation({
    mutationFn: async (v: PhotoForm) => {
      const file = fileRef.current?.files?.[0];
      if (!file) throw new Error("Please choose a photo.");
      const form = new FormData();
      form.append("file", file);
      form.append("pet_id", String(pet.pet_id));
      form.append("title", v.title);
      if (v.description) form.append("description", v.description);
      return api.upload<PetPhoto>("/api/pet-photos", form);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["pet-photos", pet.pet_id] });
      setOpen(false);
      reset({ title: "", description: "" });
      if (fileRef.current) fileRef.current.value = "";
      toast.success("Photo added.");
    },
    onError: (err: Error) => toast.error(`Upload failed: ${err.message}`),
  });

  const del = useMutation({
    mutationFn: (id: number) => api.del(`/api/pet-photos/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["pet-photos", pet.pet_id] });
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
          No photos yet. Upload a few shots of {pet.pet_name}.
        </div>
      ) : (
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-4">
          {photos.map((p) => (
            <div
              key={p.pet_photo_id}
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
                      del.mutate(p.pet_photo_id);
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
        title={`Add photo of ${pet.pet_name}`}
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
          <Field label="Title" htmlFor="pet_photo_title">
            <input
              id="pet_photo_title"
              className="input"
              placeholder="e.g. Puppy on the beach"
              {...register("title", { required: true })}
            />
          </Field>
          <Field label="Photo" htmlFor="pet_photo_file">
            <input
              id="pet_photo_file"
              ref={fileRef}
              type="file"
              accept="image/*"
              className="input"
            />
          </Field>
          <Field label="Description" htmlFor="pet_photo_description">
            <textarea
              id="pet_photo_description"
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

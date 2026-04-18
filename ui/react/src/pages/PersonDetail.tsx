import { useRef } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { useEffect, useState } from "react";
import { ArrowLeft, Camera, Image as ImageIcon, Pencil, Plus, Target, Trash2, Upload, X } from "lucide-react";
import { api } from "@/lib/api";
import type {
  Goal,
  GoalPriority,
  IdentityDocument,
  IdentityDocumentImageSide,
  Person,
  PersonPhoto,
  SensitiveIdentifier,
} from "@/lib/types";
import { PageHeader } from "@/components/PageHeader";
import { Field } from "@/components/Field";
import { Modal } from "@/components/Modal";
import { ProfileAvatar } from "@/components/ProfileAvatar";
import { useToast } from "@/components/Toast";
import {
  GENDERS,
  GOAL_PRIORITIES,
  GOAL_PRIORITY_LABELS,
  PRIMARY_RELATIONSHIPS,
} from "@/lib/enums";
import { cleanPayload } from "@/lib/form";

const ID_TYPES = [
  "drivers_license",
  "passport",
  "state_id",
  "birth_certificate",
  "permanent_resident_card",
  "global_entry",
  "military_id",
  "other",
];

const SENSITIVE_TYPES = [
  "social_security_number",
  "itin",
  "foreign_tax_id",
  "other",
];

type PersonForm = {
  first_name: string;
  middle_name?: string;
  last_name: string;
  preferred_name?: string;
  date_of_birth?: string;
  gender?: string;
  primary_family_relationship?: string;
  email_address?: string;
  mobile_phone_number?: string;
  home_phone_number?: string;
  work_phone_number?: string;
  notes?: string;
};

export default function PersonDetail() {
  const { familyId, personId } = useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const toast = useToast();

  const { data: person } = useQuery<Person>({
    queryKey: ["person", personId],
    queryFn: () => api.get<Person>(`/api/people/${personId}`),
  });

  const { register, handleSubmit, reset } = useForm<PersonForm>();
  useEffect(() => {
    if (person) {
      reset({
        first_name: person.first_name,
        middle_name: person.middle_name ?? "",
        last_name: person.last_name,
        preferred_name: person.preferred_name ?? "",
        date_of_birth: person.date_of_birth ?? "",
        gender: person.gender ?? "",
        primary_family_relationship: person.primary_family_relationship ?? "",
        email_address: person.email_address ?? "",
        mobile_phone_number: person.mobile_phone_number ?? "",
        home_phone_number: person.home_phone_number ?? "",
        work_phone_number: person.work_phone_number ?? "",
        notes: person.notes ?? "",
      });
    }
  }, [person, reset]);

  const save = useMutation({
    mutationFn: (v: PersonForm) => {
      const body = Object.fromEntries(
        Object.entries(v).map(([k, val]) => [k, val === "" ? null : val])
      );
      return api.patch<Person>(`/api/people/${personId}`, body);
    },
    onSuccess: (p) => {
      qc.invalidateQueries({ queryKey: ["person", personId] });
      qc.invalidateQueries({ queryKey: ["people", familyId] });
      toast.success(`Saved ${p.first_name} ${p.last_name}.`);
    },
    onError: (err: Error) => toast.error(`Save failed: ${err.message}`),
  });

  const del = useMutation({
    mutationFn: () => api.del(`/api/people/${personId}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["people", familyId] });
      toast.success("Person deleted.");
      navigate(`/families/${familyId}/people`);
    },
    onError: (err: Error) => toast.error(`Delete failed: ${err.message}`),
  });

  const fileRef = useRef<HTMLInputElement>(null);
  const uploadPhoto = useMutation({
    mutationFn: async (file: File) => {
      const form = new FormData();
      form.append("file", file);
      return api.upload<Person>(`/api/people/${personId}/profile-photo`, form);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["person", personId] });
      qc.invalidateQueries({ queryKey: ["people", familyId] });
      toast.success("Profile photo updated.");
    },
    onError: (err: Error) => toast.error(`Photo upload failed: ${err.message}`),
  });

  return (
    <div>
      <button
        className="text-sm text-muted-foreground hover:text-foreground mb-3 inline-flex items-center gap-1"
        onClick={() => navigate(`/families/${familyId}/people`)}
      >
        <ArrowLeft className="h-4 w-4" /> Back to people
      </button>

      <PageHeader
        title={
          person
            ? `${person.preferred_name || person.first_name} ${person.last_name}`
            : "Person"
        }
        description="Profile, contact info, identity documents, and sensitive identifiers."
      />

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="card lg:col-span-1">
          <div className="card-body flex flex-col items-center gap-3">
            {person && <ProfileAvatar person={person} size={160} />}
            <input
              ref={fileRef}
              type="file"
              accept="image/*"
              className="hidden"
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) uploadPhoto.mutate(f);
              }}
            />
            <button
              className="btn-secondary"
              onClick={() => fileRef.current?.click()}
              disabled={uploadPhoto.isPending}
            >
              <Camera className="h-4 w-4" />
              {uploadPhoto.isPending ? "Uploading…" : "Change photo"}
            </button>
          </div>
        </div>

        <div className="card lg:col-span-2">
          <div className="card-header">
            <div className="card-title">Profile</div>
          </div>
          <div className="card-body">
            <form
              className="grid grid-cols-2 gap-4"
              onSubmit={handleSubmit((v) => save.mutate(v))}
            >
              <Field label="First name" htmlFor="first_name">
                <input id="first_name" className="input" {...register("first_name")} />
              </Field>
              <Field label="Last name" htmlFor="last_name">
                <input id="last_name" className="input" {...register("last_name")} />
              </Field>
              <Field label="Middle name" htmlFor="middle_name">
                <input id="middle_name" className="input" {...register("middle_name")} />
              </Field>
              <Field label="Preferred name" htmlFor="preferred_name">
                <input
                  id="preferred_name"
                  className="input"
                  {...register("preferred_name")}
                />
              </Field>
              <Field label="Date of birth" htmlFor="date_of_birth">
                <input
                  id="date_of_birth"
                  type="date"
                  className="input"
                  {...register("date_of_birth")}
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
              <Field
                label="Primary family relationship"
                htmlFor="primary_family_relationship"
                hint="High-level label; manage the family tree on the Relationships page."
              >
                <select
                  id="primary_family_relationship"
                  className="input"
                  {...register("primary_family_relationship")}
                >
                  <option value="">—</option>
                  {PRIMARY_RELATIONSHIPS.map((r) => (
                    <option key={r} value={r}>
                      {r.replace(/_/g, " ")}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Email" htmlFor="email_address">
                <input
                  id="email_address"
                  type="email"
                  className="input"
                  {...register("email_address")}
                />
              </Field>
              <Field label="Mobile phone" htmlFor="mobile_phone_number">
                <input
                  id="mobile_phone_number"
                  className="input"
                  {...register("mobile_phone_number")}
                />
              </Field>
              <Field label="Home phone" htmlFor="home_phone_number">
                <input
                  id="home_phone_number"
                  className="input"
                  {...register("home_phone_number")}
                />
              </Field>
              <Field label="Work phone" htmlFor="work_phone_number">
                <input
                  id="work_phone_number"
                  className="input"
                  {...register("work_phone_number")}
                />
              </Field>
              <div className="col-span-2">
                <Field label="Notes" htmlFor="notes">
                  <textarea id="notes" className="input" rows={3} {...register("notes")} />
                </Field>
              </div>
              <div className="col-span-2 flex justify-between items-center">
                <button
                  type="button"
                  className="btn-destructive"
                  onClick={() => {
                    if (confirm("Delete this person?")) del.mutate();
                  }}
                >
                  <Trash2 className="h-4 w-4" />
                  Delete person
                </button>
                <div className="flex items-center gap-3">
                  {save.isSuccess && !save.isPending && (
                    <span className="text-xs text-emerald-600">Saved</span>
                  )}
                  {save.isError && (
                    <span className="text-xs text-destructive">
                      {(save.error as Error).message}
                    </span>
                  )}
                  <button type="submit" className="btn-primary" disabled={save.isPending}>
                    {save.isPending ? "Saving…" : "Save changes"}
                  </button>
                </div>
              </div>
            </form>
          </div>
        </div>

        <div className="lg:col-span-3">
          <PhotosSection personId={Number(personId)} />
        </div>
        <div className="lg:col-span-3">
          <GoalsSection personId={Number(personId)} />
        </div>
        <div className="lg:col-span-3">
          <IdentityDocumentsSection personId={Number(personId)} />
        </div>
        <div className="lg:col-span-3">
          <SensitiveIdentifiersSection personId={Number(personId)} />
        </div>
      </div>
    </div>
  );
}

function PhotosSection({ personId }: { personId: number }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const { data: photos } = useQuery<PersonPhoto[]>({
    queryKey: ["person-photos", personId],
    queryFn: () =>
      api.get<PersonPhoto[]>(`/api/person-photos?person_id=${personId}`),
  });

  type PhotoForm = {
    title: string;
    description?: string;
    use_for_face_recognition: boolean;
  };
  const { register, handleSubmit, reset } = useForm<PhotoForm>({
    defaultValues: { use_for_face_recognition: true },
  });

  const upload = useMutation({
    mutationFn: async (v: PhotoForm) => {
      const file = fileRef.current?.files?.[0];
      if (!file) throw new Error("Please choose a photo.");
      const form = new FormData();
      form.append("file", file);
      form.append("person_id", String(personId));
      form.append("title", v.title);
      if (v.description) form.append("description", v.description);
      form.append(
        "use_for_face_recognition",
        v.use_for_face_recognition ? "true" : "false"
      );
      return api.upload<PersonPhoto>("/api/person-photos", form);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["person-photos", personId] });
      setOpen(false);
      reset({ title: "", description: "", use_for_face_recognition: true });
      if (fileRef.current) fileRef.current.value = "";
      toast.success("Photo added.");
    },
    onError: (err: Error) => toast.error(err.message),
  });

  const toggle = useMutation({
    mutationFn: (p: PersonPhoto) =>
      api.patch<PersonPhoto>(`/api/person-photos/${p.person_photo_id}`, {
        use_for_face_recognition: !p.use_for_face_recognition,
      }),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ["person-photos", personId] }),
    onError: (err: Error) => toast.error(err.message),
  });

  const del = useMutation({
    mutationFn: (id: number) => api.del(`/api/person-photos/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["person-photos", personId] });
      toast.success("Photo removed.");
    },
    onError: (err: Error) => toast.error(err.message),
  });

  return (
    <div className="card">
      <div className="card-header">
        <div className="card-title">
          Photos
          <span className="badge ml-2">
            {(photos ?? []).filter((p) => p.use_for_face_recognition).length} for recognition
          </span>
        </div>
        <button className="btn-secondary" onClick={() => setOpen(true)}>
          <Upload className="h-4 w-4" /> Add photo
        </button>
      </div>
      <div className="card-body">
        {!photos || photos.length === 0 ? (
          <div className="text-sm text-muted-foreground">
            No extra photos yet. Upload a few face shots of this person to help Avi
            recognize them.
          </div>
        ) : (
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-4">
            {photos.map((p) => (
              <div key={p.person_photo_id} className="border border-border rounded-lg overflow-hidden flex flex-col bg-white">
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
                  <label className="text-xs text-muted-foreground flex items-center gap-2 mt-auto">
                    <input
                      type="checkbox"
                      checked={p.use_for_face_recognition}
                      onChange={() => toggle.mutate(p)}
                    />
                    Use for recognition
                  </label>
                  <button
                    className="text-destructive hover:text-destructive/80 text-xs inline-flex items-center gap-1 self-start"
                    onClick={() => {
                      if (confirm(`Delete "${p.title}"?`)) del.mutate(p.person_photo_id);
                    }}
                  >
                    <Trash2 className="h-3.5 w-3.5" /> Remove
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      <Modal
        open={open}
        onClose={() => {
          setOpen(false);
          reset({ title: "", description: "", use_for_face_recognition: true });
        }}
        title="Add photo"
        footer={
          <>
            <button
              className="btn-secondary"
              onClick={() => {
                setOpen(false);
                reset({ title: "", description: "", use_for_face_recognition: true });
              }}
            >
              Cancel
            </button>
            <button
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
          <Field label="Name" htmlFor="title">
            <input
              id="title"
              className="input"
              placeholder="e.g. Front porch, summer 2024"
              {...register("title", { required: true })}
            />
          </Field>
          <Field label="Photo" htmlFor="file">
            <input id="file" ref={fileRef} type="file" accept="image/*" className="input" />
          </Field>
          <Field label="Description" htmlFor="description">
            <textarea
              id="description"
              rows={2}
              className="input"
              placeholder="Any useful context (lighting, angle, etc.)"
              {...register("description")}
            />
          </Field>
          <label className="text-sm flex items-center gap-2">
            <input
              type="checkbox"
              {...register("use_for_face_recognition")}
            />
            Use this photo for face recognition training
          </label>
        </form>
      </Modal>
    </div>
  );
}

type IdentityDocForm = {
  document_type: string;
  document_number: string;
  issuing_authority: string;
  country_of_issue: string;
  state_or_region_of_issue: string;
  issue_date: string;
  expiration_date: string;
  notes: string;
};

const EMPTY_ID_DOC_FORM: IdentityDocForm = {
  document_type: "drivers_license",
  document_number: "",
  issuing_authority: "",
  country_of_issue: "United States",
  state_or_region_of_issue: "",
  issue_date: "",
  expiration_date: "",
  notes: "",
};

function identityDocToForm(d: IdentityDocument): IdentityDocForm {
  return {
    document_type: d.document_type,
    document_number: "",
    issuing_authority: d.issuing_authority ?? "",
    country_of_issue: d.country_of_issue ?? "United States",
    state_or_region_of_issue: d.state_or_region_of_issue ?? "",
    issue_date: d.issue_date ?? "",
    expiration_date: d.expiration_date ?? "",
    notes: d.notes ?? "",
  };
}

function IdentityDocumentsSection({ personId }: { personId: number }) {
  const qc = useQueryClient();
  const toast = useToast();
  // null = modal closed, "new" = create mode, number = edit mode for that doc id.
  const [editingId, setEditingId] = useState<number | "new" | null>(null);

  const { data } = useQuery<IdentityDocument[]>({
    queryKey: ["identity-documents", personId],
    queryFn: () =>
      api.get<IdentityDocument[]>(`/api/identity-documents?person_id=${personId}`),
  });

  const editingDoc =
    typeof editingId === "number"
      ? data?.find((d) => d.identity_document_id === editingId) ?? null
      : null;

  const del = useMutation({
    mutationFn: (id: number) => api.del(`/api/identity-documents/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["identity-documents", personId] });
      toast.success("Document removed.");
    },
    onError: (err: Error) => toast.error(err.message),
  });

  return (
    <div className="card">
      <div className="card-header">
        <div className="card-title">Identity documents</div>
        <button className="btn-secondary" onClick={() => setEditingId("new")}>
          <Plus className="h-4 w-4" /> Add
        </button>
      </div>
      <div className="card-body">
        {!data || data.length === 0 ? (
          <div className="text-sm text-muted-foreground">
            No identity documents on file.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-xs text-muted-foreground">
                <tr className="border-b border-border">
                  <th className="text-left py-2">Type</th>
                  <th className="text-left">Number</th>
                  <th className="text-left">Issuing authority</th>
                  <th className="text-left">Expires</th>
                  <th className="text-left">Images</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {data.map((d) => {
                  const imageCount =
                    (d.front_image_path ? 1 : 0) + (d.back_image_path ? 1 : 0);
                  return (
                    <tr
                      key={d.identity_document_id}
                      className="border-b border-border hover:bg-muted/40 cursor-pointer"
                      onClick={() => setEditingId(d.identity_document_id)}
                    >
                      <td className="py-2">
                        {d.document_type.replace(/_/g, " ")}
                      </td>
                      <td>
                        {d.document_number_last_four
                          ? `••••${d.document_number_last_four}`
                          : "—"}
                      </td>
                      <td>
                        {d.issuing_authority ?? d.state_or_region_of_issue ?? "—"}
                      </td>
                      <td>{d.expiration_date ?? "—"}</td>
                      <td>
                        <span
                          className={`inline-flex items-center gap-1 text-xs ${
                            imageCount === 0
                              ? "text-muted-foreground"
                              : "text-foreground"
                          }`}
                        >
                          <ImageIcon className="h-3.5 w-3.5" />
                          {imageCount}/2
                        </span>
                      </td>
                      <td className="text-right">
                        <button
                          className="text-destructive hover:text-destructive/80"
                          onClick={(e) => {
                            e.stopPropagation();
                            if (confirm("Delete this document record?"))
                              del.mutate(d.identity_document_id);
                          }}
                        >
                          <Trash2 className="h-4 w-4" />
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <IdentityDocumentModal
        open={editingId !== null}
        mode={editingId === "new" ? "create" : "edit"}
        doc={editingDoc}
        personId={personId}
        onClose={() => setEditingId(null)}
      />
    </div>
  );
}

function IdentityDocumentModal({
  open,
  mode,
  doc,
  personId,
  onClose,
}: {
  open: boolean;
  mode: "create" | "edit";
  doc: IdentityDocument | null;
  personId: number;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const { register, handleSubmit, reset } = useForm<IdentityDocForm>({
    defaultValues: EMPTY_ID_DOC_FORM,
  });

  // Repopulate the form whenever the target doc changes or the modal opens.
  useEffect(() => {
    if (!open) return;
    if (mode === "edit" && doc) {
      reset(identityDocToForm(doc));
    } else {
      reset(EMPTY_ID_DOC_FORM);
    }
  }, [open, mode, doc, reset]);

  const invalidate = () =>
    qc.invalidateQueries({ queryKey: ["identity-documents", personId] });

  const create = useMutation({
    mutationFn: (v: IdentityDocForm) =>
      api.post<IdentityDocument>("/api/identity-documents", {
        ...toPayload(v, { isCreate: true }),
        person_id: personId,
      }),
    onSuccess: () => {
      invalidate();
      toast.success("Identity document added.");
      onClose();
    },
    onError: (err: Error) => toast.error(err.message),
  });

  const update = useMutation({
    mutationFn: (v: IdentityDocForm) => {
      if (!doc) throw new Error("No document selected");
      return api.patch<IdentityDocument>(
        `/api/identity-documents/${doc.identity_document_id}`,
        toPayload(v, { isCreate: false })
      );
    },
    onSuccess: () => {
      invalidate();
      toast.success("Identity document saved.");
    },
    onError: (err: Error) => toast.error(err.message),
  });

  const onSubmit = (v: IdentityDocForm) => {
    if (mode === "create") create.mutate(v);
    else update.mutate(v);
  };

  const pending = create.isPending || update.isPending;
  const title = mode === "create" ? "Add identity document" : "Edit identity document";

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={title}
      wide={mode === "edit"}
      footer={
        <>
          <button className="btn-secondary" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn-primary"
            disabled={pending}
            onClick={handleSubmit(onSubmit)}
          >
            {pending
              ? "Saving…"
              : mode === "create"
                ? "Add"
                : "Save changes"}
          </button>
        </>
      }
    >
      <form
        className="grid grid-cols-2 gap-4"
        onSubmit={(e) => e.preventDefault()}
      >
        <Field label="Document type" htmlFor="document_type">
          <select
            id="document_type"
            className="input"
            {...register("document_type", { required: true })}
          >
            {ID_TYPES.map((t) => (
              <option key={t} value={t}>
                {t.replace(/_/g, " ")}
              </option>
            ))}
          </select>
        </Field>
        <Field
          label="Document number"
          htmlFor="document_number"
          hint={
            mode === "edit" && doc?.document_number_last_four
              ? `On file ending in ${doc.document_number_last_four}. Leave blank to keep it, or enter a new number to replace.`
              : "Stored encrypted; only last 4 is displayed."
          }
        >
          <input
            id="document_number"
            className="input"
            autoComplete="off"
            {...register("document_number")}
          />
        </Field>
        <Field label="Issuing authority" htmlFor="issuing_authority">
          <input
            id="issuing_authority"
            className="input"
            {...register("issuing_authority")}
          />
        </Field>
        <Field label="Country" htmlFor="country_of_issue">
          <input
            id="country_of_issue"
            className="input"
            {...register("country_of_issue")}
          />
        </Field>
        <Field label="State / region" htmlFor="state_or_region_of_issue">
          <input
            id="state_or_region_of_issue"
            className="input"
            {...register("state_or_region_of_issue")}
          />
        </Field>
        <Field label="Issue date" htmlFor="issue_date">
          <input
            id="issue_date"
            type="date"
            className="input"
            {...register("issue_date")}
          />
        </Field>
        <Field label="Expiration date" htmlFor="expiration_date">
          <input
            id="expiration_date"
            type="date"
            className="input"
            {...register("expiration_date")}
          />
        </Field>
        <div className="col-span-2">
          <Field label="Notes" htmlFor="notes">
            <textarea id="notes" className="input" rows={2} {...register("notes")} />
          </Field>
        </div>
      </form>

      {mode === "edit" && doc && (
        <div className="mt-6 border-t border-border pt-4">
          <div className="text-sm font-medium mb-2">Scans</div>
          <div className="text-xs text-muted-foreground mb-3">
            Upload photos or scans of the front and (when applicable) back of
            the document. Saved locally under this family&rsquo;s storage.
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <IdentityDocImageSlot side="front" doc={doc} />
            <IdentityDocImageSlot side="back" doc={doc} />
          </div>
        </div>
      )}
    </Modal>
  );
}

function toPayload(
  v: IdentityDocForm,
  { isCreate }: { isCreate: boolean }
): Record<string, unknown> {
  const blankToNull = (s: string) => (s.trim() === "" ? null : s);
  const payload: Record<string, unknown> = {
    document_type: v.document_type,
    issuing_authority: blankToNull(v.issuing_authority),
    country_of_issue: v.country_of_issue.trim() || "United States",
    state_or_region_of_issue: blankToNull(v.state_or_region_of_issue),
    issue_date: blankToNull(v.issue_date),
    expiration_date: blankToNull(v.expiration_date),
    notes: blankToNull(v.notes),
  };
  if (isCreate) {
    // Only include document_number on create if provided; create allows null.
    if (v.document_number.trim()) payload.document_number = v.document_number;
  } else if (v.document_number.trim()) {
    // On edit, only include when the user typed something so we don't
    // inadvertently clear the stored number.
    payload.document_number = v.document_number;
  }
  return payload;
}

function IdentityDocImageSlot({
  side,
  doc,
}: {
  side: IdentityDocumentImageSide;
  doc: IdentityDocument;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const inputRef = useRef<HTMLInputElement>(null);
  const storedPath =
    side === "front" ? doc.front_image_path : doc.back_image_path;

  const invalidate = () =>
    qc.invalidateQueries({ queryKey: ["identity-documents", doc.person_id] });

  const upload = useMutation({
    mutationFn: async (file: File) => {
      const form = new FormData();
      form.append("file", file);
      return api.upload<IdentityDocument>(
        `/api/identity-documents/${doc.identity_document_id}/images/${side}`,
        form
      );
    },
    onSuccess: () => {
      invalidate();
      toast.success(`${side === "front" ? "Front" : "Back"} image uploaded.`);
      if (inputRef.current) inputRef.current.value = "";
    },
    onError: (err: Error) => toast.error(err.message),
  });

  const remove = useMutation({
    mutationFn: () =>
      api.del(
        `/api/identity-documents/${doc.identity_document_id}/images/${side}`
      ),
    onSuccess: () => {
      invalidate();
      toast.success(`${side === "front" ? "Front" : "Back"} image removed.`);
    },
    onError: (err: Error) => toast.error(err.message),
  });

  const label = side === "front" ? "Front" : "Back";

  return (
    <div className="border border-border rounded-lg overflow-hidden bg-white flex flex-col">
      <div className="px-3 py-2 border-b border-border text-xs font-medium flex items-center justify-between">
        <span>{label}</span>
        {storedPath && (
          <button
            type="button"
            className="text-destructive hover:text-destructive/80 inline-flex items-center gap-1 text-xs"
            onClick={() => {
              if (confirm(`Remove ${label.toLowerCase()} image?`))
                remove.mutate();
            }}
            disabled={remove.isPending}
          >
            <X className="h-3.5 w-3.5" /> Remove
          </button>
        )}
      </div>
      <div className="aspect-[5/3] bg-muted flex items-center justify-center">
        {storedPath ? (
          <img
            src={`/api/media/${storedPath}`}
            alt={`${label} of identity document`}
            className="h-full w-full object-contain"
          />
        ) : (
          <div className="text-xs text-muted-foreground flex flex-col items-center gap-1">
            <ImageIcon className="h-6 w-6" />
            <span>No {label.toLowerCase()} image yet</span>
          </div>
        )}
      </div>
      <div className="p-3">
        <input
          ref={inputRef}
          type="file"
          accept="image/*"
          className="hidden"
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) upload.mutate(f);
          }}
        />
        <button
          type="button"
          className="btn-secondary w-full"
          onClick={() => inputRef.current?.click()}
          disabled={upload.isPending}
        >
          <Upload className="h-4 w-4" />
          {upload.isPending
            ? "Uploading…"
            : storedPath
              ? `Replace ${label.toLowerCase()}`
              : `Upload ${label.toLowerCase()}`}
        </button>
      </div>
    </div>
  );
}

function SensitiveIdentifiersSection({ personId }: { personId: number }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const { data } = useQuery<SensitiveIdentifier[]>({
    queryKey: ["sensitive-identifiers", personId],
    queryFn: () =>
      api.get<SensitiveIdentifier[]>(
        `/api/sensitive-identifiers?person_id=${personId}`
      ),
  });
  const create = useMutation({
    mutationFn: (v: Record<string, unknown>) =>
      api.post("/api/sensitive-identifiers", { ...v, person_id: personId }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["sensitive-identifiers", personId] });
      setOpen(false);
      reset();
      toast.success("Identifier added.");
    },
    onError: (err: Error) => toast.error(err.message),
  });
  const del = useMutation({
    mutationFn: (id: number) => api.del(`/api/sensitive-identifiers/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["sensitive-identifiers", personId] });
      toast.success("Identifier removed.");
    },
    onError: (err: Error) => toast.error(err.message),
  });
  const { register, handleSubmit, reset } = useForm<Record<string, unknown>>();

  return (
    <div className="card">
      <div className="card-header">
        <div className="card-title">
          Sensitive identifiers
          <span className="badge ml-2">encrypted</span>
        </div>
        <button className="btn-secondary" onClick={() => setOpen(true)}>
          <Plus className="h-4 w-4" /> Add
        </button>
      </div>
      <div className="card-body">
        {!data || data.length === 0 ? (
          <div className="text-sm text-muted-foreground">
            No SSN or tax IDs on file.
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-xs text-muted-foreground">
              <tr className="border-b border-border">
                <th className="text-left py-2">Type</th>
                <th className="text-left">Value</th>
                <th className="text-left">Notes</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {data.map((s) => (
                <tr key={s.sensitive_identifier_id} className="border-b border-border">
                  <td className="py-2">{s.identifier_type.replace(/_/g, " ")}</td>
                  <td>
                    {s.identifier_last_four ? `•••-••-${s.identifier_last_four}` : "—"}
                  </td>
                  <td className="text-muted-foreground">{s.notes ?? ""}</td>
                  <td className="text-right">
                    <button
                      className="text-destructive hover:text-destructive/80"
                      onClick={() => {
                        if (confirm("Delete this identifier?"))
                          del.mutate(s.sensitive_identifier_id);
                      }}
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <Modal
        open={open}
        onClose={() => {
          setOpen(false);
          reset();
        }}
        title="Add sensitive identifier"
        footer={
          <>
            <button
              className="btn-secondary"
              onClick={() => {
                setOpen(false);
                reset();
              }}
            >
              Cancel
            </button>
            <button
              className="btn-primary"
              disabled={create.isPending}
              onClick={handleSubmit((v) => create.mutate(v))}
            >
              {create.isPending ? "Adding…" : "Add"}
            </button>
          </>
        }
      >
        <form className="grid grid-cols-2 gap-4" onSubmit={(e) => e.preventDefault()}>
          <Field label="Type" htmlFor="identifier_type">
            <select
              id="identifier_type"
              className="input"
              {...register("identifier_type", { required: true })}
            >
              {SENSITIVE_TYPES.map((t) => (
                <option key={t} value={t}>
                  {t.replace(/_/g, " ")}
                </option>
              ))}
            </select>
          </Field>
          <Field
            label="Value"
            htmlFor="identifier_value"
            hint="Encrypted at rest; never logged or returned in full."
          >
            <input
              id="identifier_value"
              className="input"
              {...register("identifier_value", { required: true })}
            />
          </Field>
          <div className="col-span-2">
            <Field label="Notes" htmlFor="notes">
              <textarea id="notes" className="input" rows={2} {...register("notes")} />
            </Field>
          </div>
        </form>
      </Modal>
    </div>
  );
}

// ---------- Goals ----------------------------------------------------------

function GoalsSection({ personId }: { personId: number }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [editingId, setEditingId] = useState<number | "new" | null>(null);

  const { data: goals } = useQuery<Goal[]>({
    queryKey: ["goals", personId],
    queryFn: () => api.get<Goal[]>(`/api/goals?person_id=${personId}`),
  });

  const del = useMutation({
    mutationFn: (id: number) => api.del(`/api/goals/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["goals", personId] });
      toast.success("Goal removed.");
    },
    onError: (err: Error) => toast.error(err.message),
  });

  const editingGoal =
    typeof editingId === "number"
      ? goals?.find((g) => g.goal_id === editingId) ?? null
      : null;

  return (
    <div className="card">
      <div className="card-header flex items-center justify-between">
        <div className="card-title">Goals</div>
        <button className="btn-secondary" onClick={() => setEditingId("new")}>
          <Plus className="h-4 w-4" /> Add goal
        </button>
      </div>
      <div className="card-body">
        {!goals || goals.length === 0 ? (
          <div className="text-sm text-muted-foreground flex items-center gap-2">
            <Target className="h-4 w-4" />
            No goals yet. Capture something this person is working toward.
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-xs text-muted-foreground">
              <tr className="border-b border-border">
                <th className="text-left py-2">Goal</th>
                <th className="text-left">Priority</th>
                <th className="text-left">Start date</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {goals.map((g) => (
                <tr
                  key={g.goal_id}
                  className="border-b border-border table-row-hover cursor-pointer"
                  onClick={() => setEditingId(g.goal_id)}
                >
                  <td className="py-2">
                    <div className="font-medium">{g.goal_name}</div>
                    {g.description && (
                      <div className="text-xs text-muted-foreground line-clamp-2">
                        {g.description}
                      </div>
                    )}
                  </td>
                  <td>
                    <PriorityBadge priority={g.priority} />
                  </td>
                  <td>{g.start_date ?? "—"}</td>
                  <td className="text-right whitespace-nowrap">
                    <button
                      className="text-muted-foreground hover:text-foreground mr-3"
                      onClick={(e) => {
                        e.stopPropagation();
                        setEditingId(g.goal_id);
                      }}
                      aria-label="Edit goal"
                    >
                      <Pencil className="h-4 w-4" />
                    </button>
                    <button
                      className="text-destructive hover:text-destructive/80"
                      onClick={(e) => {
                        e.stopPropagation();
                        if (confirm("Delete this goal?")) del.mutate(g.goal_id);
                      }}
                      aria-label="Delete goal"
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <GoalModal
        open={editingId !== null}
        mode={editingId === "new" ? "create" : "edit"}
        goal={editingGoal}
        personId={personId}
        onClose={() => setEditingId(null)}
      />
    </div>
  );
}

const PRIORITY_STYLES: Record<GoalPriority, string> = {
  urgent: "bg-red-100 text-red-700 border-red-200",
  semi_urgent: "bg-amber-100 text-amber-700 border-amber-200",
  normal: "bg-slate-100 text-slate-700 border-slate-200",
  low: "bg-emerald-50 text-emerald-700 border-emerald-200",
};

function PriorityBadge({ priority }: { priority: GoalPriority }) {
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium ${PRIORITY_STYLES[priority]}`}
    >
      {GOAL_PRIORITY_LABELS[priority]}
    </span>
  );
}

type GoalForm = {
  goal_name: string;
  description?: string;
  start_date?: string;
  priority: GoalPriority;
};

function GoalModal({
  open,
  mode,
  goal,
  personId,
  onClose,
}: {
  open: boolean;
  mode: "create" | "edit";
  goal: Goal | null;
  personId: number;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const { register, handleSubmit, reset } = useForm<GoalForm>({
    defaultValues: { priority: "normal" },
  });

  useEffect(() => {
    if (mode === "edit" && goal) {
      reset({
        goal_name: goal.goal_name,
        description: goal.description ?? "",
        start_date: goal.start_date ?? "",
        priority: goal.priority,
      });
    } else if (mode === "create") {
      reset({
        goal_name: "",
        description: "",
        start_date: "",
        priority: "normal",
      });
    }
  }, [mode, goal, reset, open]);

  const create = useMutation({
    mutationFn: (v: GoalForm) => {
      const cleaned = cleanPayload({ ...v, person_id: personId });
      return api.post<Goal>("/api/goals", cleaned);
    },
    onSuccess: (g) => {
      qc.invalidateQueries({ queryKey: ["goals", personId] });
      toast.success(`Added goal "${g.goal_name}".`);
      onClose();
    },
    onError: (err: Error) => toast.error(`Could not add goal: ${err.message}`),
  });

  const update = useMutation({
    mutationFn: (v: GoalForm) => {
      if (!goal) throw new Error("No goal to update");
      const cleaned = cleanPayload(v);
      return api.patch<Goal>(`/api/goals/${goal.goal_id}`, cleaned);
    },
    onSuccess: (g) => {
      qc.invalidateQueries({ queryKey: ["goals", personId] });
      toast.success(`Saved goal "${g.goal_name}".`);
      onClose();
    },
    onError: (err: Error) => toast.error(`Save failed: ${err.message}`),
  });

  const pending = create.isPending || update.isPending;

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={mode === "create" ? "Add goal" : "Edit goal"}
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
            {pending ? "Saving…" : mode === "create" ? "Add goal" : "Save changes"}
          </button>
        </>
      }
    >
      <form className="grid grid-cols-2 gap-4" onSubmit={(e) => e.preventDefault()}>
        <div className="col-span-2">
          <Field label="Goal" htmlFor="goal_name">
            <input
              id="goal_name"
              className="input"
              placeholder="Run a half-marathon"
              {...register("goal_name", { required: true })}
            />
          </Field>
        </div>
        <Field label="Priority" htmlFor="priority">
          <select id="priority" className="input" {...register("priority")}>
            {GOAL_PRIORITIES.map((p) => (
              <option key={p} value={p}>
                {GOAL_PRIORITY_LABELS[p]}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Start date" htmlFor="start_date">
          <input
            id="start_date"
            type="date"
            className="input"
            {...register("start_date")}
          />
        </Field>
        <div className="col-span-2">
          <Field label="Description" htmlFor="description">
            <textarea
              id="description"
              rows={3}
              className="input"
              placeholder="Why this matters, what success looks like…"
              {...register("description")}
            />
          </Field>
        </div>
      </form>
    </Modal>
  );
}

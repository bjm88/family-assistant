import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { Plus, Users } from "lucide-react";
import { api } from "@/lib/api";
import type { Person } from "@/lib/types";
import { PageHeader } from "@/components/PageHeader";
import { EmptyState } from "@/components/EmptyState";
import { Modal } from "@/components/Modal";
import { Field } from "@/components/Field";
import { ProfileAvatar } from "@/components/ProfileAvatar";
import { useToast } from "@/components/Toast";
import { PRIMARY_RELATIONSHIPS, GENDERS } from "@/lib/enums";

interface NewPersonForm {
  first_name: string;
  last_name: string;
  preferred_name?: string;
  primary_family_relationship?: string;
  gender?: string;
  date_of_birth?: string;
  email_address?: string;
  mobile_phone_number?: string;
}

export default function PeoplePage() {
  const { familyId } = useParams();
  const qc = useQueryClient();
  const toast = useToast();
  const [isOpen, setOpen] = useState(false);

  const { data: people } = useQuery<Person[]>({
    queryKey: ["people", familyId],
    queryFn: () => api.get<Person[]>(`/api/people?family_id=${familyId}`),
  });

  const createPerson = useMutation({
    mutationFn: (v: NewPersonForm) => {
      const body: Record<string, unknown> = { ...v, family_id: Number(familyId) };
      Object.keys(body).forEach((k) => {
        if (body[k] === "") body[k] = null;
      });
      return api.post<Person>("/api/people", body);
    },
    onSuccess: (p) => {
      qc.invalidateQueries({ queryKey: ["people", familyId] });
      setOpen(false);
      reset();
      toast.success(`Added ${p.first_name} ${p.last_name}.`);
    },
    onError: (err: Error) => toast.error(`Could not add person: ${err.message}`),
  });

  const { register, handleSubmit, reset, formState } = useForm<NewPersonForm>();

  return (
    <div>
      <PageHeader
        title="People"
        description="Everyone in the household, with profiles, photos, and IDs."
        actions={
          <button className="btn-primary" onClick={() => setOpen(true)}>
            <Plus className="h-4 w-4" />
            Add person
          </button>
        }
      />

      {!people ? (
        <div className="text-muted-foreground">Loading…</div>
      ) : people.length === 0 ? (
        <EmptyState
          icon={Users}
          title="No people yet"
          description="Add family members to start capturing their profile, identity documents, and contact info."
          action={
            <button className="btn-primary" onClick={() => setOpen(true)}>
              <Plus className="h-4 w-4" />
              Add a person
            </button>
          }
        />
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {people.map((p) => (
            <Link
              key={p.person_id}
              to={`/admin/families/${familyId}/people/${p.person_id}`}
              className="card hover:shadow-md transition-shadow"
            >
              <div className="card-body flex items-center gap-4">
                <ProfileAvatar person={p} size={56} />
                <div className="min-w-0">
                  <div className="font-medium truncate">
                    {p.preferred_name || p.first_name} {p.last_name}
                  </div>
                  <div className="text-xs text-muted-foreground mt-0.5">
                    {p.primary_family_relationship ?? "family member"}
                  </div>
                  <div className="text-xs text-muted-foreground truncate">
                    {p.email_address ?? p.mobile_phone_number ?? ""}
                  </div>
                </div>
              </div>
            </Link>
          ))}
        </div>
      )}

      <Modal
        open={isOpen}
        onClose={() => {
          setOpen(false);
          reset();
        }}
        title="Add a family member"
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
              disabled={createPerson.isPending}
              onClick={handleSubmit((v) => createPerson.mutate(v))}
            >
              {createPerson.isPending ? "Adding…" : "Add person"}
            </button>
          </>
        }
      >
        <form className="grid grid-cols-2 gap-4" onSubmit={(e) => e.preventDefault()}>
          <Field
            label="First name"
            htmlFor="first_name"
            error={formState.errors.first_name?.message}
          >
            <input
              id="first_name"
              className="input"
              {...register("first_name", { required: "Required" })}
            />
          </Field>
          <Field
            label="Last name"
            htmlFor="last_name"
            error={formState.errors.last_name?.message}
          >
            <input
              id="last_name"
              className="input"
              {...register("last_name", { required: "Required" })}
            />
          </Field>
          <Field label="Preferred / nickname" htmlFor="preferred_name">
            <input id="preferred_name" className="input" {...register("preferred_name")} />
          </Field>
          <Field
            label="Primary family relationship"
            htmlFor="primary_family_relationship"
            hint="High-level label; the family tree is managed on the Relationships page."
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
          <Field label="Date of birth" htmlFor="date_of_birth">
            <input
              id="date_of_birth"
              type="date"
              className="input"
              {...register("date_of_birth")}
            />
          </Field>
          <Field label="Mobile phone" htmlFor="mobile_phone_number">
            <input
              id="mobile_phone_number"
              className="input"
              {...register("mobile_phone_number")}
            />
          </Field>
          <div className="col-span-2">
            <Field label="Email" htmlFor="email_address">
              <input
                id="email_address"
                type="email"
                className="input"
                {...register("email_address")}
              />
            </Field>
          </div>
        </form>
      </Modal>
    </div>
  );
}

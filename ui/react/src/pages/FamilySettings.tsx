import { useNavigate, useParams } from "react-router-dom";
import { useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { api } from "@/lib/api";
import type { Family } from "@/lib/types";
import { PageHeader } from "@/components/PageHeader";
import { Field } from "@/components/Field";
import { useToast } from "@/components/Toast";

interface Form {
  family_name: string;
  head_of_household_notes?: string;
}

export default function FamilySettings() {
  const { familyId } = useParams();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const toast = useToast();
  const { data: family } = useQuery<Family>({
    queryKey: ["family", familyId],
    queryFn: () => api.get<Family>(`/api/families/${familyId}`),
  });
  const { register, handleSubmit, reset, formState } = useForm<Form>();

  useEffect(() => {
    if (family)
      reset({
        family_name: family.family_name,
        head_of_household_notes: family.head_of_household_notes ?? "",
      });
  }, [family, reset]);

  const save = useMutation({
    mutationFn: (v: Form) => api.patch<Family>(`/api/families/${familyId}`, v),
    onSuccess: (f) => {
      qc.invalidateQueries({ queryKey: ["family", familyId] });
      qc.invalidateQueries({ queryKey: ["families"] });
      toast.success(`Saved ${f.family_name}.`);
    },
    onError: (err: Error) => toast.error(`Save failed: ${err.message}`),
  });
  const del = useMutation({
    mutationFn: () => api.del(`/api/families/${familyId}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["families"] });
      toast.success("Family deleted.");
      navigate("/admin/families");
    },
    onError: (err: Error) => toast.error(`Delete failed: ${err.message}`),
  });

  return (
    <div>
      <PageHeader
        title="Family settings"
        description="Update the household name or delete this family."
      />

      <div className="card max-w-2xl">
        <div className="card-body">
          <form
            className="space-y-4"
            onSubmit={handleSubmit((v) => save.mutate(v))}
          >
            <Field
              label="Family name"
              htmlFor="family_name"
              error={formState.errors.family_name?.message}
            >
              <input
                id="family_name"
                className="input"
                {...register("family_name", { required: "Required" })}
              />
            </Field>
            <Field label="Notes" htmlFor="head_of_household_notes">
              <textarea
                id="head_of_household_notes"
                className="input"
                rows={4}
                {...register("head_of_household_notes")}
              />
            </Field>
            <div className="flex justify-end gap-2">
              <button type="submit" className="btn-primary" disabled={save.isPending}>
                {save.isPending ? "Saving…" : "Save changes"}
              </button>
            </div>
          </form>
        </div>
      </div>

      <div className="card max-w-2xl mt-6 border-destructive/30">
        <div className="card-header">
          <div className="card-title text-destructive">Danger zone</div>
        </div>
        <div className="card-body flex items-center justify-between gap-4">
          <div className="text-sm text-muted-foreground">
            Delete this family and every piece of data associated with it.
            This cannot be undone.
          </div>
          <button
            className="btn-destructive"
            disabled={del.isPending}
            onClick={() => {
              if (
                confirm(
                  "Delete this family and all its people, vehicles, policies, accounts, and documents?"
                )
              )
                del.mutate();
            }}
          >
            Delete family
          </button>
        </div>
      </div>
    </div>
  );
}

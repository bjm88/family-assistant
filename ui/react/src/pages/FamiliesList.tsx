import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Sparkles, Users } from "lucide-react";
import { useForm } from "react-hook-form";
import { api } from "@/lib/api";
import type { Family, FamilySummary } from "@/lib/types";
import { PageHeader } from "@/components/PageHeader";
import { EmptyState } from "@/components/EmptyState";
import { Modal } from "@/components/Modal";
import { Field } from "@/components/Field";
import { useToast } from "@/components/Toast";

interface NewFamilyForm {
  family_name: string;
  head_of_household_notes?: string;
}

export default function FamiliesList() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const toast = useToast();
  const [isOpen, setOpen] = useState(false);

  const { data, isLoading } = useQuery<FamilySummary[]>({
    queryKey: ["families"],
    queryFn: () => api.get<FamilySummary[]>("/api/families"),
  });

  const createFamily = useMutation({
    mutationFn: (form: NewFamilyForm) => api.post<Family>("/api/families", form),
    onSuccess: (family) => {
      qc.invalidateQueries({ queryKey: ["families"] });
      toast.success(`Created ${family.family_name}.`);
      navigate(`/admin/families/${family.family_id}`);
    },
    onError: (err: Error) => toast.error(`Could not create family: ${err.message}`),
  });

  const { register, handleSubmit, reset, formState } = useForm<NewFamilyForm>();

  return (
    <div className="min-h-screen">
      <div className="max-w-5xl mx-auto p-8">
        <PageHeader
          title="Family Assistant"
          description="Select a family to manage, or create a new one to get started."
          actions={
            <button className="btn-primary" onClick={() => setOpen(true)}>
              <Plus className="h-4 w-4" />
              New family
            </button>
          }
        />

        {isLoading ? (
          <div className="text-muted-foreground">Loading…</div>
        ) : !data || data.length === 0 ? (
          <EmptyState
            icon={Users}
            title="No families yet"
            description='Create your first family to add people, vehicles, insurance, finances, and documents.'
            action={
              <button className="btn-primary" onClick={() => setOpen(true)}>
                <Plus className="h-4 w-4" />
                Create your first family
              </button>
            }
          />
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {data.map((f) => (
              <div
                key={f.family_id}
                className="card hover:shadow-md transition-shadow flex flex-col"
              >
                <button
                  onClick={() => navigate(`/admin/families/${f.family_id}`)}
                  className="card-body text-left flex-1"
                >
                  <div className="text-lg font-semibold">{f.family_name}</div>
                  <div className="mt-3 text-sm text-muted-foreground grid grid-cols-2 gap-y-1">
                    <div>{f.people_count} people</div>
                    <div>{f.vehicles_count} vehicles</div>
                    <div>{f.insurance_policies_count} policies</div>
                    <div>{f.financial_accounts_count} accounts</div>
                    <div>{f.documents_count} documents</div>
                  </div>
                </button>
                <div className="border-t border-border px-5 py-3 flex items-center justify-between">
                  <button
                    onClick={() =>
                      navigate(`/admin/families/${f.family_id}`)
                    }
                    className="text-xs text-muted-foreground hover:text-foreground"
                  >
                    Open admin →
                  </button>
                  <button
                    onClick={() => navigate(`/aiassistant/${f.family_id}`)}
                    className="text-xs font-medium text-primary hover:underline inline-flex items-center gap-1"
                  >
                    <Sparkles className="h-3.5 w-3.5" />
                    Live AI
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      <Modal
        open={isOpen}
        onClose={() => {
          setOpen(false);
          reset();
        }}
        title="Create a new family"
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
              disabled={createFamily.isPending}
              onClick={handleSubmit((values) => createFamily.mutate(values))}
            >
              {createFamily.isPending ? "Creating…" : "Create family"}
            </button>
          </>
        }
      >
        <form className="space-y-4" onSubmit={(e) => e.preventDefault()}>
          <Field
            label="Family name"
            htmlFor="family_name"
            error={formState.errors.family_name?.message}
          >
            <input
              className="input"
              id="family_name"
              placeholder="The Smith Family"
              {...register("family_name", { required: "Required" })}
            />
          </Field>
          <Field label="Notes (optional)" htmlFor="head_of_household_notes">
            <textarea
              className="input"
              id="head_of_household_notes"
              rows={3}
              {...register("head_of_household_notes")}
            />
          </Field>
        </form>
      </Modal>
    </div>
  );
}

import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { Pencil, Plus, ShieldCheck, Trash2 } from "lucide-react";
import { api } from "@/lib/api";
import type { InsurancePolicy, Person, Vehicle } from "@/lib/types";
import { PageHeader } from "@/components/PageHeader";
import { EmptyState } from "@/components/EmptyState";
import { Modal } from "@/components/Modal";
import { Field } from "@/components/Field";
import { EncryptedField } from "@/components/EncryptedField";
import { useToast } from "@/components/Toast";
import { stripEmpty } from "@/lib/form";

const POLICY_TYPES = [
  "auto",
  "home",
  "renters",
  "condo",
  "health",
  "dental",
  "vision",
  "life",
  "disability",
  "umbrella",
  "pet",
  "travel",
  "other",
];
const BILLING = ["monthly", "quarterly", "semi_annual", "annual"];

// Exhaustive named field list — see VehicleForm in VehiclesPage.tsx for
// why every field has to be explicitly defaulted (so reset() truly
// blanks the form when switching between edit and create).
type PolicyForm = {
  policy_type: string;
  carrier_name: string;
  plan_name: string;
  policy_number: string;
  premium_amount_usd: string;
  premium_billing_frequency: string;
  deductible_amount_usd: string;
  coverage_limit_amount_usd: string;
  effective_date: string;
  expiration_date: string;
  agent_name: string;
  agent_phone_number: string;
  agent_email_address: string;
  notes: string;
  // react-hook-form returns a string[] for `<select multiple>` registrations.
  covered_person_ids: string[];
  covered_vehicle_ids: string[];
};

function emptyForm(): PolicyForm {
  return {
    policy_type: "auto",
    carrier_name: "",
    plan_name: "",
    policy_number: "",
    premium_amount_usd: "",
    premium_billing_frequency: "",
    deductible_amount_usd: "",
    coverage_limit_amount_usd: "",
    effective_date: "",
    expiration_date: "",
    agent_name: "",
    agent_phone_number: "",
    agent_email_address: "",
    notes: "",
    covered_person_ids: [],
    covered_vehicle_ids: [],
  };
}

function policyToForm(p: InsurancePolicy): PolicyForm {
  return {
    policy_type: p.policy_type,
    carrier_name: p.carrier_name,
    plan_name: p.plan_name ?? "",
    // Encrypted; never round-trip the cleartext into the form.
    policy_number: "",
    premium_amount_usd: p.premium_amount_usd ?? "",
    premium_billing_frequency: p.premium_billing_frequency ?? "",
    deductible_amount_usd: p.deductible_amount_usd ?? "",
    coverage_limit_amount_usd: p.coverage_limit_amount_usd ?? "",
    effective_date: p.effective_date ?? "",
    expiration_date: p.expiration_date ?? "",
    agent_name: p.agent_name ?? "",
    agent_phone_number: p.agent_phone_number ?? "",
    agent_email_address: p.agent_email_address ?? "",
    notes: p.notes ?? "",
    covered_person_ids: p.covered_person_ids.map(String),
    covered_vehicle_ids: p.covered_vehicle_ids.map(String),
  };
}

export default function InsurancePoliciesPage() {
  const { familyId } = useParams();
  const qc = useQueryClient();
  const toast = useToast();
  // null = closed, "new" = create, number = edit-that-policy-id.
  const [editingId, setEditingId] = useState<number | "new" | null>(null);

  const { data } = useQuery<InsurancePolicy[]>({
    queryKey: ["insurance", familyId],
    queryFn: () =>
      api.get<InsurancePolicy[]>(`/api/insurance-policies?family_id=${familyId}`),
  });
  const { data: people } = useQuery<Person[]>({
    queryKey: ["people", familyId],
    queryFn: () => api.get<Person[]>(`/api/people?family_id=${familyId}`),
  });
  const { data: vehicles } = useQuery<Vehicle[]>({
    queryKey: ["vehicles", familyId],
    queryFn: () => api.get<Vehicle[]>(`/api/vehicles?family_id=${familyId}`),
  });

  const del = useMutation({
    mutationFn: (id: number) => api.del(`/api/insurance-policies/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["insurance", familyId] });
      toast.success("Policy removed.");
    },
    onError: (err: Error) => toast.error(`Could not remove policy: ${err.message}`),
  });

  const editingPolicy =
    typeof editingId === "number"
      ? data?.find((p) => p.insurance_policy_id === editingId) ?? null
      : null;

  return (
    <div>
      <PageHeader
        title="Insurance policies"
        description="Auto, home, health, life, umbrella — everything the family is insured for."
        actions={
          <button className="btn-primary" onClick={() => setEditingId("new")}>
            <Plus className="h-4 w-4" /> Add policy
          </button>
        }
      />

      {!data || data.length === 0 ? (
        <EmptyState
          icon={ShieldCheck}
          title="No policies yet"
          description="Add your first policy to track premiums, renewals, and covered people."
          action={
            <button className="btn-primary" onClick={() => setEditingId("new")}>
              <Plus className="h-4 w-4" /> Add a policy
            </button>
          }
        />
      ) : (
        <div className="card">
          <div className="card-body">
            <table className="w-full text-sm">
              <thead className="text-xs text-muted-foreground">
                <tr className="border-b border-border">
                  <th className="text-left py-2">Policy</th>
                  <th className="text-left">Carrier</th>
                  <th className="text-left">Number</th>
                  <th className="text-left">Premium</th>
                  <th className="text-left">Expires</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {data.map((p) => (
                  <tr
                    key={p.insurance_policy_id}
                    className="border-b border-border table-row-hover cursor-pointer"
                    onClick={() => setEditingId(p.insurance_policy_id)}
                  >
                    <td className="py-2">
                      <div className="font-medium capitalize">
                        {p.policy_type.replace(/_/g, " ")}
                        {p.plan_name ? ` — ${p.plan_name}` : ""}
                      </div>
                      <div className="text-xs text-muted-foreground">
                        Covers {p.covered_person_ids.length} people,{" "}
                        {p.covered_vehicle_ids.length} vehicles
                      </div>
                    </td>
                    <td>{p.carrier_name}</td>
                    <td>
                      {p.policy_number_last_four ? `•••${p.policy_number_last_four}` : "—"}
                    </td>
                    <td>
                      {p.premium_amount_usd
                        ? `$${Number(p.premium_amount_usd).toFixed(2)} / ${
                            p.premium_billing_frequency ?? "—"
                          }`
                        : "—"}
                    </td>
                    <td>{p.expiration_date ?? "—"}</td>
                    <td className="text-right whitespace-nowrap">
                      <button
                        className="text-muted-foreground hover:text-foreground mr-3"
                        onClick={(e) => {
                          e.stopPropagation();
                          setEditingId(p.insurance_policy_id);
                        }}
                        aria-label="Edit policy"
                      >
                        <Pencil className="h-4 w-4" />
                      </button>
                      <button
                        className="text-destructive hover:text-destructive/80"
                        onClick={(e) => {
                          e.stopPropagation();
                          if (confirm("Delete this policy?"))
                            del.mutate(p.insurance_policy_id);
                        }}
                        aria-label="Delete policy"
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

      <PolicyModal
        open={editingId !== null}
        mode={editingId === "new" ? "create" : "edit"}
        policy={editingPolicy}
        familyId={Number(familyId)}
        people={people ?? []}
        vehicles={vehicles ?? []}
        onClose={() => setEditingId(null)}
      />
    </div>
  );
}

function PolicyModal({
  open,
  mode,
  policy,
  familyId,
  people,
  vehicles,
  onClose,
}: {
  open: boolean;
  mode: "create" | "edit";
  policy: InsurancePolicy | null;
  familyId: number;
  people: Person[];
  vehicles: Vehicle[];
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const { register, handleSubmit, reset } = useForm<PolicyForm>({
    defaultValues: emptyForm(),
  });

  useEffect(() => {
    if (!open) return;
    if (mode === "edit" && policy) reset(policyToForm(policy));
    else reset(emptyForm());
  }, [open, mode, policy, reset]);

  const invalidate = () =>
    qc.invalidateQueries({ queryKey: ["insurance", String(familyId)] });

  // Convert form inputs into a backend-ready payload. Empty strings become
  // dropped (stripEmpty); covered_*_ids arrays are coerced to numbers and
  // *always* sent so the backend can swap them in/out (an empty selection
  // means "no coverage", not "leave alone").
  const toCreatePayload = (v: PolicyForm) => {
    const cleaned = stripEmpty({
      policy_type: v.policy_type,
      carrier_name: v.carrier_name,
      plan_name: v.plan_name,
      policy_number: v.policy_number,
      premium_amount_usd: v.premium_amount_usd,
      premium_billing_frequency: v.premium_billing_frequency,
      deductible_amount_usd: v.deductible_amount_usd,
      coverage_limit_amount_usd: v.coverage_limit_amount_usd,
      effective_date: v.effective_date,
      expiration_date: v.expiration_date,
      agent_name: v.agent_name,
      agent_phone_number: v.agent_phone_number,
      agent_email_address: v.agent_email_address,
      notes: v.notes,
    });
    return {
      ...cleaned,
      family_id: familyId,
      covered_person_ids: v.covered_person_ids.map(Number),
      covered_vehicle_ids: v.covered_vehicle_ids.map(Number),
    };
  };

  const toUpdatePayload = (v: PolicyForm) => {
    const base = stripEmpty({
      policy_type: v.policy_type,
      carrier_name: v.carrier_name,
      plan_name: v.plan_name,
      premium_amount_usd: v.premium_amount_usd,
      premium_billing_frequency: v.premium_billing_frequency,
      deductible_amount_usd: v.deductible_amount_usd,
      coverage_limit_amount_usd: v.coverage_limit_amount_usd,
      effective_date: v.effective_date,
      expiration_date: v.expiration_date,
      agent_name: v.agent_name,
      agent_phone_number: v.agent_phone_number,
      agent_email_address: v.agent_email_address,
      notes: v.notes,
    });
    const payload: Record<string, unknown> = {
      ...base,
      covered_person_ids: v.covered_person_ids.map(Number),
      covered_vehicle_ids: v.covered_vehicle_ids.map(Number),
    };
    // Only include policy_number when the user typed a replacement,
    // otherwise the encrypted ciphertext is preserved server-side.
    if (v.policy_number.trim()) payload.policy_number = v.policy_number;
    return payload;
  };

  const create = useMutation({
    mutationFn: (v: PolicyForm) =>
      api.post<InsurancePolicy>("/api/insurance-policies", toCreatePayload(v)),
    onSuccess: (p) => {
      invalidate();
      toast.success(`Added ${p.policy_type} policy with ${p.carrier_name}.`);
      onClose();
    },
    onError: (err: Error) => toast.error(`Could not add policy: ${err.message}`),
  });

  const update = useMutation({
    mutationFn: (v: PolicyForm) => {
      if (!policy) throw new Error("No policy selected");
      return api.patch<InsurancePolicy>(
        `/api/insurance-policies/${policy.insurance_policy_id}`,
        toUpdatePayload(v)
      );
    },
    onSuccess: (p) => {
      invalidate();
      toast.success(`Saved ${p.carrier_name} ${p.policy_type} policy.`);
      onClose();
    },
    onError: (err: Error) => toast.error(`Save failed: ${err.message}`),
  });

  const onSubmit = (v: PolicyForm) => {
    if (mode === "create") create.mutate(v);
    else update.mutate(v);
  };

  const pending = create.isPending || update.isPending;

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={mode === "create" ? "Add insurance policy" : "Edit insurance policy"}
      wide
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
                ? "Add policy"
                : "Save changes"}
          </button>
        </>
      }
    >
      <form className="grid grid-cols-2 gap-4" onSubmit={(e) => e.preventDefault()}>
        <Field label="Policy type" htmlFor="policy_type">
          <select
            id="policy_type"
            className="input"
            {...register("policy_type", { required: true })}
          >
            {POLICY_TYPES.map((t) => (
              <option key={t} value={t}>
                {t.replace(/_/g, " ")}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Carrier" htmlFor="carrier_name">
          <input
            id="carrier_name"
            className="input"
            {...register("carrier_name", { required: true })}
          />
        </Field>
        <Field label="Plan name" htmlFor="plan_name">
          <input id="plan_name" className="input" {...register("plan_name")} />
        </Field>
        <EncryptedField
          label="Policy number"
          htmlFor="policy_number"
          lastFour={policy?.policy_number_last_four ?? null}
          mode={mode}
        >
          <input
            id="policy_number"
            className="input"
            autoComplete="off"
            placeholder={
              mode === "edit" && policy?.policy_number_last_four
                ? "Type a new number to replace, or leave blank"
                : ""
            }
            {...register("policy_number", {
              required: mode === "create",
            })}
          />
        </EncryptedField>
        <Field label="Premium (USD)" htmlFor="premium_amount_usd">
          <input
            id="premium_amount_usd"
            type="number"
            step="0.01"
            className="input"
            {...register("premium_amount_usd")}
          />
        </Field>
        <Field label="Billing frequency" htmlFor="premium_billing_frequency">
          <select
            id="premium_billing_frequency"
            className="input"
            {...register("premium_billing_frequency")}
          >
            <option value="">—</option>
            {BILLING.map((b) => (
              <option key={b} value={b}>
                {b.replace(/_/g, " ")}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Deductible (USD)" htmlFor="deductible_amount_usd">
          <input
            id="deductible_amount_usd"
            type="number"
            step="0.01"
            className="input"
            {...register("deductible_amount_usd")}
          />
        </Field>
        <Field label="Coverage limit (USD)" htmlFor="coverage_limit_amount_usd">
          <input
            id="coverage_limit_amount_usd"
            type="number"
            step="0.01"
            className="input"
            {...register("coverage_limit_amount_usd")}
          />
        </Field>
        <Field label="Effective date" htmlFor="effective_date">
          <input
            id="effective_date"
            type="date"
            className="input"
            {...register("effective_date")}
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
        <Field label="Agent name" htmlFor="agent_name">
          <input id="agent_name" className="input" {...register("agent_name")} />
        </Field>
        <Field label="Agent phone" htmlFor="agent_phone_number">
          <input
            id="agent_phone_number"
            className="input"
            {...register("agent_phone_number")}
          />
        </Field>
        <Field label="Agent email" htmlFor="agent_email_address">
          <input
            id="agent_email_address"
            type="email"
            className="input"
            {...register("agent_email_address")}
          />
        </Field>
        <div className="col-span-2 grid grid-cols-2 gap-4">
          <Field
            label="Covered people"
            htmlFor="covered_person_ids"
            hint="Hold ⌘ / Ctrl to select multiple."
          >
            <select
              id="covered_person_ids"
              className="input h-32"
              multiple
              {...register("covered_person_ids")}
            >
              {people.map((p) => (
                <option key={p.person_id} value={p.person_id}>
                  {p.first_name} {p.last_name}
                </option>
              ))}
            </select>
          </Field>
          <Field
            label="Covered vehicles"
            htmlFor="covered_vehicle_ids"
            hint="Hold ⌘ / Ctrl to select multiple."
          >
            <select
              id="covered_vehicle_ids"
              className="input h-32"
              multiple
              {...register("covered_vehicle_ids")}
            >
              {vehicles.map((v) => (
                <option key={v.vehicle_id} value={v.vehicle_id}>
                  {v.year ? `${v.year} ` : ""}
                  {v.make} {v.model}
                  {v.nickname ? ` (${v.nickname})` : ""}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <div className="col-span-2">
          <Field label="Notes" htmlFor="notes">
            <textarea id="notes" rows={2} className="input" {...register("notes")} />
          </Field>
        </div>
      </form>
    </Modal>
  );
}

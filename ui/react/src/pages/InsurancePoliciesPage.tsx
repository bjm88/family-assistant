import { useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { Plus, ShieldCheck, Trash2 } from "lucide-react";
import { api } from "@/lib/api";
import type { InsurancePolicy, Person, Vehicle } from "@/lib/types";
import { PageHeader } from "@/components/PageHeader";
import { EmptyState } from "@/components/EmptyState";
import { Modal } from "@/components/Modal";
import { Field } from "@/components/Field";

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

export default function InsurancePoliciesPage() {
  const { familyId } = useParams();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);

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

  const create = useMutation({
    mutationFn: (v: any) => {
      const body: any = { ...v, family_id: Number(familyId) };
      body.covered_person_ids = Array.isArray(v.covered_person_ids)
        ? v.covered_person_ids.map(Number)
        : [];
      body.covered_vehicle_ids = Array.isArray(v.covered_vehicle_ids)
        ? v.covered_vehicle_ids.map(Number)
        : [];
      return api.post("/api/insurance-policies", body);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["insurance", familyId] });
      setOpen(false);
      reset();
    },
  });
  const del = useMutation({
    mutationFn: (id: number) => api.del(`/api/insurance-policies/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["insurance", familyId] }),
  });

  const { register, handleSubmit, reset } = useForm<any>();

  return (
    <div>
      <PageHeader
        title="Insurance policies"
        description="Auto, home, health, life, umbrella — everything the family is insured for."
        actions={
          <button className="btn-primary" onClick={() => setOpen(true)}>
            <Plus className="h-4 w-4" /> Add policy
          </button>
        }
      />

      {!data || data.length === 0 ? (
        <EmptyState
          icon={ShieldCheck}
          title="No policies yet"
          description="Add your first policy to track premiums, renewals, and covered people."
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
                    className="border-b border-border table-row-hover"
                  >
                    <td className="py-2">
                      <div className="font-medium">
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
                    <td className="text-right">
                      <button
                        className="text-destructive hover:text-destructive/80"
                        onClick={() => {
                          if (confirm("Delete this policy?"))
                            del.mutate(p.insurance_policy_id);
                        }}
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

      <Modal
        open={open}
        onClose={() => {
          setOpen(false);
          reset();
        }}
        title="Add insurance policy"
        wide
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
              {create.isPending ? "Adding…" : "Add policy"}
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
                  {t}
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
          <Field
            label="Policy number"
            htmlFor="policy_number"
            hint="Encrypted at rest; only last 4 is displayed."
          >
            <input
              id="policy_number"
              className="input"
              {...register("policy_number", { required: true })}
            />
          </Field>
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
          <div className="col-span-2 grid grid-cols-2 gap-4">
            <Field label="Covered people" htmlFor="covered_person_ids">
              <select
                id="covered_person_ids"
                className="input h-32"
                multiple
                {...register("covered_person_ids")}
              >
                {(people ?? []).map((p) => (
                  <option key={p.person_id} value={p.person_id}>
                    {p.first_name} {p.last_name}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Covered vehicles" htmlFor="covered_vehicle_ids">
              <select
                id="covered_vehicle_ids"
                className="input h-32"
                multiple
                {...register("covered_vehicle_ids")}
              >
                {(vehicles ?? []).map((v) => (
                  <option key={v.vehicle_id} value={v.vehicle_id}>
                    {v.year ? `${v.year} ` : ""}
                    {v.make} {v.model}
                    {v.nickname ? ` (${v.nickname})` : ""}
                  </option>
                ))}
              </select>
            </Field>
          </div>
        </form>
      </Modal>
    </div>
  );
}

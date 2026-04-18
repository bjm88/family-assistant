import { useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { Landmark, Plus, Trash2 } from "lucide-react";
import { api } from "@/lib/api";
import type { FinancialAccount, Person } from "@/lib/types";
import { PageHeader } from "@/components/PageHeader";
import { EmptyState } from "@/components/EmptyState";
import { Modal } from "@/components/Modal";
import { Field } from "@/components/Field";
import { useToast } from "@/components/Toast";
import { cleanPayload } from "@/lib/form";

const ACCOUNT_TYPES = [
  "checking",
  "savings",
  "money_market",
  "certificate_of_deposit",
  "credit_card",
  "brokerage",
  "retirement_401k",
  "retirement_ira",
  "retirement_roth_ira",
  "college_529",
  "loan_auto",
  "loan_personal",
  "loan_student",
  "mortgage",
  "heloc",
  "other",
];

export default function FinancialAccountsPage() {
  const { familyId } = useParams();
  const qc = useQueryClient();
  const toast = useToast();
  const [open, setOpen] = useState(false);

  const { data } = useQuery<FinancialAccount[]>({
    queryKey: ["finances", familyId],
    queryFn: () =>
      api.get<FinancialAccount[]>(`/api/financial-accounts?family_id=${familyId}`),
  });
  const { data: people } = useQuery<Person[]>({
    queryKey: ["people", familyId],
    queryFn: () => api.get<Person[]>(`/api/people?family_id=${familyId}`),
  });

  const create = useMutation({
    mutationFn: (v: Record<string, unknown>) => {
      const cleaned = cleanPayload(v, [
        "primary_holder_person_id",
        "current_balance_usd",
        "credit_limit_usd",
      ]);
      return api.post<FinancialAccount>("/api/financial-accounts", {
        ...cleaned,
        family_id: Number(familyId),
      });
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["finances", familyId] });
      setOpen(false);
      reset();
      toast.success("Financial account added.");
    },
    onError: (err: Error) => toast.error(`Could not add account: ${err.message}`),
  });
  const del = useMutation({
    mutationFn: (id: number) => api.del(`/api/financial-accounts/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["finances", familyId] });
      toast.success("Account removed.");
    },
    onError: (err: Error) => toast.error(`Could not remove account: ${err.message}`),
  });

  const { register, handleSubmit, reset } = useForm<Record<string, unknown>>();
  const peopleById = new Map((people ?? []).map((p) => [p.person_id, p]));

  return (
    <div>
      <PageHeader
        title="Financial accounts"
        description="Bank, credit, brokerage, retirement, and loan accounts. Account numbers encrypted at rest."
        actions={
          <button className="btn-primary" onClick={() => setOpen(true)}>
            <Plus className="h-4 w-4" /> Add account
          </button>
        }
      />
      {!data || data.length === 0 ? (
        <EmptyState
          icon={Landmark}
          title="No accounts yet"
          description="Add your checking, savings, credit cards, and investment accounts."
        />
      ) : (
        <div className="card">
          <div className="card-body">
            <table className="w-full text-sm">
              <thead className="text-xs text-muted-foreground">
                <tr className="border-b border-border">
                  <th className="text-left py-2">Account</th>
                  <th className="text-left">Institution</th>
                  <th className="text-left">Number</th>
                  <th className="text-left">Holder</th>
                  <th className="text-left">Balance</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {data.map((a) => (
                  <tr
                    key={a.financial_account_id}
                    className="border-b border-border table-row-hover"
                  >
                    <td className="py-2">
                      <div className="font-medium">
                        {a.account_nickname ?? a.account_type.replace(/_/g, " ")}
                      </div>
                      <div className="text-xs text-muted-foreground">
                        {a.account_type.replace(/_/g, " ")}
                      </div>
                    </td>
                    <td>{a.institution_name}</td>
                    <td>{a.account_number_last_four ? `•••${a.account_number_last_four}` : "—"}</td>
                    <td>
                      {a.primary_holder_person_id
                        ? (() => {
                            const p = peopleById.get(a.primary_holder_person_id!);
                            return p ? `${p.first_name} ${p.last_name}` : "—";
                          })()
                        : "—"}
                    </td>
                    <td>
                      {a.current_balance_usd ? `$${Number(a.current_balance_usd).toFixed(2)}` : "—"}
                    </td>
                    <td className="text-right">
                      <button
                        className="text-destructive hover:text-destructive/80"
                        onClick={() => {
                          if (confirm("Delete this account?"))
                            del.mutate(a.financial_account_id);
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
        title="Add financial account"
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
              {create.isPending ? "Adding…" : "Add account"}
            </button>
          </>
        }
      >
        <form className="grid grid-cols-2 gap-4" onSubmit={(e) => e.preventDefault()}>
          <Field label="Account type" htmlFor="account_type">
            <select
              id="account_type"
              className="input"
              {...register("account_type", { required: true })}
            >
              {ACCOUNT_TYPES.map((t) => (
                <option key={t} value={t}>
                  {t.replace(/_/g, " ")}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Institution" htmlFor="institution_name">
            <input
              id="institution_name"
              className="input"
              {...register("institution_name", { required: true })}
            />
          </Field>
          <Field label="Nickname" htmlFor="account_nickname">
            <input
              id="account_nickname"
              className="input"
              {...register("account_nickname")}
            />
          </Field>
          <Field label="Primary holder" htmlFor="primary_holder_person_id">
            <select
              id="primary_holder_person_id"
              className="input"
              {...register("primary_holder_person_id")}
            >
              <option value="">—</option>
              {(people ?? []).map((p) => (
                <option key={p.person_id} value={p.person_id}>
                  {p.first_name} {p.last_name}
                </option>
              ))}
            </select>
          </Field>
          <Field
            label="Account number"
            htmlFor="account_number"
            hint="Encrypted at rest; only last 4 is shown."
          >
            <input
              id="account_number"
              className="input"
              {...register("account_number", { required: true })}
            />
          </Field>
          <Field
            label="Routing number"
            htmlFor="routing_number"
            hint="Encrypted. Optional for non-bank accounts."
          >
            <input
              id="routing_number"
              className="input"
              {...register("routing_number")}
            />
          </Field>
          <Field label="Current balance (USD)" htmlFor="current_balance_usd">
            <input
              id="current_balance_usd"
              type="number"
              step="0.01"
              className="input"
              {...register("current_balance_usd")}
            />
          </Field>
          <Field label="Credit limit (USD)" htmlFor="credit_limit_usd">
            <input
              id="credit_limit_usd"
              type="number"
              step="0.01"
              className="input"
              {...register("credit_limit_usd")}
            />
          </Field>
          <div className="col-span-2">
            <Field label="Online login URL" htmlFor="online_login_url">
              <input
                id="online_login_url"
                className="input"
                placeholder="https://chase.com"
                {...register("online_login_url")}
              />
            </Field>
          </div>
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

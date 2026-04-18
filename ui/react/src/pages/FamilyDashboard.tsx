import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Car, FileText, Landmark, ShieldCheck, Users } from "lucide-react";
import { api } from "@/lib/api";
import type {
  Family,
  Person,
  Vehicle,
  InsurancePolicy,
  FinancialAccount,
  DocumentRecord,
} from "@/lib/types";
import { PageHeader } from "@/components/PageHeader";

interface StatCardProps {
  to: string;
  label: string;
  value: number | string;
  icon: typeof Users;
}
function StatCard({ to, label, value, icon: Icon }: StatCardProps) {
  return (
    <Link to={to} className="card hover:shadow-md transition-shadow">
      <div className="card-body flex items-center gap-4">
        <div className="rounded-md bg-primary/10 text-primary p-3">
          <Icon className="h-5 w-5" />
        </div>
        <div>
          <div className="text-2xl font-semibold">{value}</div>
          <div className="text-sm text-muted-foreground">{label}</div>
        </div>
      </div>
    </Link>
  );
}

export default function FamilyDashboard() {
  const { familyId } = useParams();
  const { data: family } = useQuery<Family>({
    queryKey: ["family", familyId],
    queryFn: () => api.get<Family>(`/api/families/${familyId}`),
  });
  const { data: people } = useQuery<Person[]>({
    queryKey: ["people", familyId],
    queryFn: () => api.get<Person[]>(`/api/people?family_id=${familyId}`),
  });
  const { data: vehicles } = useQuery<Vehicle[]>({
    queryKey: ["vehicles", familyId],
    queryFn: () => api.get<Vehicle[]>(`/api/vehicles?family_id=${familyId}`),
  });
  const { data: policies } = useQuery<InsurancePolicy[]>({
    queryKey: ["insurance", familyId],
    queryFn: () =>
      api.get<InsurancePolicy[]>(`/api/insurance-policies?family_id=${familyId}`),
  });
  const { data: accounts } = useQuery<FinancialAccount[]>({
    queryKey: ["finances", familyId],
    queryFn: () =>
      api.get<FinancialAccount[]>(`/api/financial-accounts?family_id=${familyId}`),
  });
  const { data: documents } = useQuery<DocumentRecord[]>({
    queryKey: ["documents", familyId],
    queryFn: () => api.get<DocumentRecord[]>(`/api/documents?family_id=${familyId}`),
  });

  return (
    <div>
      <PageHeader
        title={family?.family_name ?? "Family"}
        description="A quick glance at everything we know about your household."
      />

      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
        <StatCard
          to={`/families/${familyId}/people`}
          label="People"
          value={people?.length ?? "—"}
          icon={Users}
        />
        <StatCard
          to={`/families/${familyId}/vehicles`}
          label="Vehicles"
          value={vehicles?.length ?? "—"}
          icon={Car}
        />
        <StatCard
          to={`/families/${familyId}/insurance`}
          label="Insurance policies"
          value={policies?.length ?? "—"}
          icon={ShieldCheck}
        />
        <StatCard
          to={`/families/${familyId}/finances`}
          label="Financial accounts"
          value={accounts?.length ?? "—"}
          icon={Landmark}
        />
        <StatCard
          to={`/families/${familyId}/documents`}
          label="Documents"
          value={documents?.length ?? "—"}
          icon={FileText}
        />
      </div>

      {family?.head_of_household_notes && (
        <div className="card mt-6">
          <div className="card-header">
            <div className="card-title">Notes</div>
          </div>
          <div className="card-body whitespace-pre-wrap text-sm">
            {family.head_of_household_notes}
          </div>
        </div>
      )}
    </div>
  );
}

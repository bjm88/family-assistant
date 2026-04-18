import { Link, useNavigate, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Bot, Car, FileText, Landmark, Network, ShieldCheck, Users } from "lucide-react";
import { api } from "@/lib/api";
import type {
  Assistant,
  Family,
  Person,
  PersonRelationship,
  Vehicle,
  InsurancePolicy,
  FinancialAccount,
  DocumentRecord,
} from "@/lib/types";
import { PageHeader } from "@/components/PageHeader";
import { FamilyTreeView } from "@/components/FamilyTreeView";
import { AssistantAvatar } from "@/pages/AssistantPage";

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
  const navigate = useNavigate();
  const { data: family } = useQuery<Family>({
    queryKey: ["family", familyId],
    queryFn: () => api.get<Family>(`/api/families/${familyId}`),
  });
  const { data: people } = useQuery<Person[]>({
    queryKey: ["people", familyId],
    queryFn: () => api.get<Person[]>(`/api/people?family_id=${familyId}`),
  });
  const { data: edges } = useQuery<PersonRelationship[]>({
    queryKey: ["person-relationships", familyId],
    queryFn: () =>
      api.get<PersonRelationship[]>(
        `/api/person-relationships?family_id=${familyId}`
      ),
  });
  const { data: assistants } = useQuery<Assistant[]>({
    queryKey: ["assistants", familyId],
    queryFn: () => api.get<Assistant[]>(`/api/assistants?family_id=${familyId}`),
  });
  const assistant = assistants?.[0];
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

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 mt-6">
        <div className="card lg:col-span-2">
          <div className="card-header">
            <div>
              <div className="card-title flex items-center gap-2">
                <Network className="h-4 w-4 text-primary" /> Family tree
              </div>
              <div className="text-xs text-muted-foreground mt-0.5">
                Generated from people and the relationships you've wired up.
                Click anyone to jump to the Relationships page.
              </div>
            </div>
            <Link
              to={`/families/${familyId}/relationships`}
              className="text-xs text-primary hover:underline"
            >
              Edit →
            </Link>
          </div>
          <div className="card-body">
            {people && (
              <FamilyTreeView
                people={people}
                edges={edges ?? []}
                onSelect={() => navigate(`/families/${familyId}/relationships`)}
              />
            )}
          </div>
        </div>

        <Link
          to={`/families/${familyId}/assistant`}
          className="card hover:shadow-md transition-shadow self-start"
        >
          <div className="card-header">
            <div className="card-title flex items-center gap-2">
              <Bot className="h-4 w-4 text-primary" /> Assistant
            </div>
          </div>
          <div className="card-body flex flex-col items-center gap-3 text-center">
            {assistant ? (
              <>
                <AssistantAvatar assistant={assistant} size={128} />
                <div>
                  <div className="font-semibold">{assistant.assistant_name}</div>
                  {assistant.gender && (
                    <div className="text-xs text-muted-foreground">
                      {assistant.gender}
                    </div>
                  )}
                </div>
                {assistant.avatar_generation_note && (
                  <div className="text-[11px] text-destructive">
                    avatar needs regeneration
                  </div>
                )}
              </>
            ) : (
              <>
                <div className="rounded-2xl bg-primary/10 text-primary h-32 w-32 flex items-center justify-center">
                  <Bot className="h-12 w-12" />
                </div>
                <div className="text-sm text-muted-foreground">
                  No assistant yet. Click to create one.
                </div>
              </>
            )}
          </div>
        </Link>
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

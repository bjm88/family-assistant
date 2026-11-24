import { Link, useNavigate, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, Move } from "lucide-react";
import { api } from "@/lib/api";
import type { Family, Person, PersonRelationship } from "@/lib/types";
import { FamilyTreeView } from "@/components/FamilyTreeView";
import { useAuth } from "@/lib/auth";

export default function FamilyTreePage() {
  const { familyId } = useParams();
  const navigate = useNavigate();
  const { isAdmin } = useAuth();
  // Same query keys as FamilyDashboard so drilling in reuses its cache.
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

  return (
    <div className="flex flex-col flex-1 min-h-0 gap-3">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <Link
          to={`/admin/families/${familyId}`}
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-4 w-4" /> Overview
        </Link>
        <h1 className="text-xl font-semibold">
          {family?.family_name ?? "Family"} tree
        </h1>
        {people && (
          <span className="text-xs text-muted-foreground">
            {people.length} {people.length === 1 ? "person" : "people"}
          </span>
        )}
        <div className="ml-auto flex items-center gap-4 text-xs text-muted-foreground">
          <span className="inline-flex items-center gap-1.5">
            <svg width="24" height="4" aria-hidden>
              <line x1="0" y1="2" x2="24" y2="2" stroke="#a78bfa" strokeWidth="2" strokeDasharray="4 4" />
            </svg>
            Spouse
          </span>
          <span className="inline-flex items-center gap-1.5">
            <svg width="24" height="4" aria-hidden>
              <line x1="0" y1="2" x2="24" y2="2" stroke="#94a3b8" strokeWidth="1.5" />
            </svg>
            Parent → child
          </span>
          <span className="hidden sm:inline-flex items-center gap-1">
            <Move className="h-3.5 w-3.5" /> Drag or scroll to move around
          </span>
          {isAdmin && (
            <Link
              to={`/admin/families/${familyId}/relationships`}
              className="text-primary hover:underline"
            >
              Edit relationships →
            </Link>
          )}
        </div>
      </div>

      <div className="flex-1 min-h-0 rounded-lg border border-border bg-muted/20">
        {people ? (
          <FamilyTreeView
            people={people}
            edges={edges ?? []}
            className="h-full w-full overflow-auto"
            pannable
            onSelect={
              isAdmin
                ? (personId) =>
                    navigate(`/admin/families/${familyId}/people/${personId}`)
                : undefined
            }
          />
        ) : (
          <div className="p-6 text-sm text-muted-foreground">Loading…</div>
        )}
      </div>
    </div>
  );
}

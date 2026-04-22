import { Link, useNavigate, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  Bot,
  Building2,
  Car,
  Home,
  Network,
  PawPrint,
  Star,
} from "lucide-react";
import { api } from "@/lib/api";
import type {
  Assistant,
  Family,
  Person,
  PersonRelationship,
  Pet,
  Residence,
  Vehicle,
} from "@/lib/types";
import { PageHeader } from "@/components/PageHeader";
import { FamilyTreeView } from "@/components/FamilyTreeView";
import { AssistantAvatar } from "@/components/AssistantAvatar";

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
  // Daily-driver gallery — cars and trucks. Boats, ATVs, RVs, etc.
  // still live on the full Vehicles page reachable from the side nav.
  const dailyDrivers = (vehicles ?? []).filter(
    (v) => v.vehicle_type === "car" || v.vehicle_type === "truck"
  );
  // Cache-key convention: ``PetsPage`` and ``ResidencesPage`` both
  // normalize to ``Number(familyIdParam)`` (and document why in their
  // own comments). Match that here or invalidations from those pages
  // won't refresh the dashboard cards. The other dashboard queries
  // above use raw ``familyId`` because their counterpart pages also
  // use raw — the convention is per-resource, not app-wide.
  const { data: pets } = useQuery<Pet[]>({
    queryKey: ["pets", Number(familyId)],
    queryFn: () => api.get<Pet[]>(`/api/pets?family_id=${familyId}`),
  });
  const { data: residences } = useQuery<Residence[]>({
    queryKey: ["residences", Number(familyId)],
    queryFn: () =>
      api.get<Residence[]>(`/api/residences?family_id=${familyId}`),
  });

  return (
    <div>
      <PageHeader
        title={family?.family_name ?? "Family"}
        description="A quick glance at everything we know about your household."
      />

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
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
              to={`/admin/families/${familyId}/relationships`}
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
                onSelect={(personId) =>
                  navigate(
                    `/admin/families/${familyId}/relationships?focus=${personId}`
                  )
                }
              />
            )}
          </div>
        </div>

        {/* Two CTAs on this card: clicking the body opens the
            assistant editor; the corner pill goes straight to the
            live page. We can't nest `<Link>` inside `<Link>` (DOM
            spec forbids `<a>` inside `<a>`), so the outer container
            is a `<div>` that navigates programmatically and the pill
            is a real `<Link>` with stopPropagation so the body click
            doesn't also fire. */}
        <div
          role="link"
          tabIndex={0}
          onClick={() => navigate(`/admin/families/${familyId}/assistant`)}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              navigate(`/admin/families/${familyId}/assistant`);
            }
          }}
          className="card hover:shadow-md transition-shadow self-start cursor-pointer"
        >
          <div className="card-header">
            <div className="card-title flex items-center gap-2">
              <Bot className="h-4 w-4 text-primary" /> Assistant
            </div>
            <Link
              to={`/aiassistant/${familyId}`}
              onClick={(e) => e.stopPropagation()}
              className="text-xs inline-flex items-center gap-1 rounded-full bg-primary text-primary-foreground px-2 py-0.5 font-medium hover:bg-primary/90"
            >
              Go live →
            </Link>
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
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mt-6">
        <Link
          to={`/admin/families/${familyId}/pets`}
          className="card hover:shadow-md transition-shadow self-start"
        >
          <div className="card-header">
            <div className="card-title flex items-center gap-2">
              <PawPrint className="h-4 w-4 text-primary" /> Pets
              {pets && pets.length > 0 && (
                <span className="badge ml-1">{pets.length}</span>
              )}
            </div>
            <span className="text-xs text-primary hover:underline">Manage →</span>
          </div>
          <div className="card-body">
            {!pets || pets.length === 0 ? (
              <div className="text-sm text-muted-foreground flex items-center gap-2">
                <PawPrint className="h-4 w-4" />
                No pets yet. Click to add one.
              </div>
            ) : (
              <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-3">
                {pets.map((p) => (
                  <div
                    key={p.pet_id}
                    className="flex flex-col items-center text-center gap-1"
                  >
                    <div className="h-20 w-20 rounded-full bg-muted overflow-hidden flex items-center justify-center">
                      {p.cover_photo_path ? (
                        <img
                          src={`/api/media/${p.cover_photo_path}`}
                          alt={p.pet_name}
                          className="h-full w-full object-cover"
                        />
                      ) : (
                        <PawPrint className="h-8 w-8 text-muted-foreground" />
                      )}
                    </div>
                    <div className="text-sm font-medium truncate w-full">
                      {p.pet_name}
                    </div>
                    <div className="text-xs text-muted-foreground capitalize truncate w-full">
                      {p.animal_type.replace(/_/g, " ")}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </Link>

        <Link
          to={`/admin/families/${familyId}/residences`}
          className="card hover:shadow-md transition-shadow self-start"
        >
          <div className="card-header">
            <div className="card-title flex items-center gap-2">
              <Building2 className="h-4 w-4 text-primary" /> Residences
              {residences && residences.length > 0 && (
                <span className="badge ml-1">{residences.length}</span>
              )}
            </div>
            <span className="text-xs text-primary hover:underline">Manage →</span>
          </div>
          <div className="card-body">
            {!residences || residences.length === 0 ? (
              <div className="text-sm text-muted-foreground flex items-center gap-2">
                <Home className="h-4 w-4" />
                No residences yet. Click to add one.
              </div>
            ) : (
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                {residences.map((r) => (
                  <div
                    key={r.residence_id}
                    className="border border-border rounded-lg overflow-hidden bg-white flex flex-col"
                  >
                    <div className="aspect-video bg-muted overflow-hidden">
                      {r.cover_photo_path ? (
                        <img
                          src={`/api/media/${r.cover_photo_path}`}
                          alt={r.label}
                          className="h-full w-full object-cover"
                        />
                      ) : (
                        <div className="h-full w-full flex items-center justify-center text-muted-foreground">
                          <Home className="h-8 w-8" />
                        </div>
                      )}
                    </div>
                    <div className="p-3">
                      <div className="text-sm font-medium truncate flex items-center gap-1">
                        {r.label}
                        {r.is_primary_residence && (
                          <Star
                            className="h-3 w-3 text-primary"
                            aria-label="Primary residence"
                          />
                        )}
                      </div>
                      <div className="text-xs text-muted-foreground truncate">
                        {[r.city, r.state_or_region].filter(Boolean).join(", ")}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </Link>
      </div>

      <div className="mt-6">
        <Link
          to={`/admin/families/${familyId}/vehicles`}
          className="card hover:shadow-md transition-shadow block"
        >
          <div className="card-header">
            <div className="card-title flex items-center gap-2">
              <Car className="h-4 w-4 text-primary" /> Cars &amp; trucks
              {dailyDrivers.length > 0 && (
                <span className="badge ml-1">{dailyDrivers.length}</span>
              )}
            </div>
            <span className="text-xs text-primary hover:underline">Manage →</span>
          </div>
          <div className="card-body">
            {dailyDrivers.length === 0 ? (
              <div className="text-sm text-muted-foreground flex items-center gap-2">
                <Car className="h-4 w-4" />
                No cars or trucks yet. Click to add the daily-driver fleet —
                boats, ATVs, and other vehicles live on the full Vehicles page.
              </div>
            ) : (
              <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-4">
                {dailyDrivers.map((v) => (
                  <div
                    key={v.vehicle_id}
                    className="border border-border rounded-lg overflow-hidden bg-white flex flex-col"
                  >
                    <div className="aspect-video bg-muted overflow-hidden">
                      {v.profile_image_path ? (
                        <img
                          src={`/api/media/${v.profile_image_path}`}
                          alt={`${v.make} ${v.model}`}
                          className="h-full w-full object-cover"
                        />
                      ) : (
                        <div className="h-full w-full flex items-center justify-center text-muted-foreground">
                          <Car className="h-8 w-8" />
                        </div>
                      )}
                    </div>
                    <div className="p-3">
                      <div className="text-sm font-medium truncate">
                        {v.year ? `${v.year} ` : ""}
                        {v.make} {v.model}
                      </div>
                      <div className="text-xs text-muted-foreground truncate">
                        {v.nickname ?? v.color ?? v.trim ?? ""}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
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

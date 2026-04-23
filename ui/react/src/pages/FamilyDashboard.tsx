import type { ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  Bot,
  Building2,
  Car,
  History,
  Home,
  ListTodo,
  Network,
  PawPrint,
  Sparkles,
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
import { useAuth } from "@/lib/auth";

export default function FamilyDashboard() {
  const { familyId } = useParams();
  const navigate = useNavigate();
  const { isAdmin } = useAuth();
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

  // Members get a read-only Overview: same cards, no drill-in links,
  // no "Edit →" / "Manage →" pills, no click-to-navigate. The tree is
  // still rendered (it's the most-asked-for "show me my family" view)
  // but clicking a node is a no-op for members rather than bouncing
  // them to the Relationships page they can't access.
  const description = isAdmin
    ? "A quick glance at everything we know about your household."
    : "A quick glance at your household, plus quick links to Avi, your session history, and the tasks you're on.";

  return (
    <div>
      <PageHeader
        title={family?.family_name ?? "Family"}
        description={description}
      />

      {/* Member-only hero: three big CTAs for the daily-driver surfaces
          (Live, Session history, Tasks). Admins get the same trio from
          the sidebar — they don't need a second row of buttons here. */}
      {!isAdmin && familyId && <MemberHero familyId={familyId} />}

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="card lg:col-span-2">
          <div className="card-header">
            <div>
              <div className="card-title flex items-center gap-2">
                <Network className="h-4 w-4 text-primary" /> Family tree
              </div>
              <div className="text-xs text-muted-foreground mt-0.5">
                {isAdmin
                  ? "Generated from people and the relationships you've wired up. Click anyone to jump to the Relationships page."
                  : "Generated from people and the relationships in your household."}
              </div>
            </div>
            {isAdmin && (
              <Link
                to={`/admin/families/${familyId}/relationships`}
                className="text-xs text-primary hover:underline"
              >
                Edit →
              </Link>
            )}
          </div>
          <div className="card-body">
            {people && (
              <FamilyTreeView
                people={people}
                edges={edges ?? []}
                onSelect={
                  isAdmin
                    ? (personId) =>
                        navigate(
                          `/admin/families/${familyId}/relationships?focus=${personId}`
                        )
                    : undefined
                }
              />
            )}
          </div>
        </div>

        {/* Two CTAs on this card for admins (open editor + go live).
            Members get the static info card and the live-chat pill only —
            no drill-in to the assistant editor. */}
        <DashboardCard
          interactive={isAdmin}
          onActivate={
            isAdmin
              ? () => navigate(`/admin/families/${familyId}/assistant`)
              : undefined
          }
          className="self-start"
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
                {assistant.avatar_generation_note && isAdmin && (
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
                  {isAdmin
                    ? "No assistant yet. Click to create one."
                    : "No assistant yet."}
                </div>
              </>
            )}
          </div>
        </DashboardCard>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mt-6">
        <DashboardLinkCard
          to={`/admin/families/${familyId}/pets`}
          interactive={isAdmin}
          className="self-start"
        >
          <div className="card-header">
            <div className="card-title flex items-center gap-2">
              <PawPrint className="h-4 w-4 text-primary" /> Pets
              {pets && pets.length > 0 && (
                <span className="badge ml-1">{pets.length}</span>
              )}
            </div>
            {isAdmin && (
              <span className="text-xs text-primary hover:underline">Manage →</span>
            )}
          </div>
          <div className="card-body">
            {!pets || pets.length === 0 ? (
              <div className="text-sm text-muted-foreground flex items-center gap-2">
                <PawPrint className="h-4 w-4" />
                {isAdmin ? "No pets yet. Click to add one." : "No pets on file."}
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
        </DashboardLinkCard>

        <DashboardLinkCard
          to={`/admin/families/${familyId}/residences`}
          interactive={isAdmin}
          className="self-start"
        >
          <div className="card-header">
            <div className="card-title flex items-center gap-2">
              <Building2 className="h-4 w-4 text-primary" /> Residences
              {residences && residences.length > 0 && (
                <span className="badge ml-1">{residences.length}</span>
              )}
            </div>
            {isAdmin && (
              <span className="text-xs text-primary hover:underline">Manage →</span>
            )}
          </div>
          <div className="card-body">
            {!residences || residences.length === 0 ? (
              <div className="text-sm text-muted-foreground flex items-center gap-2">
                <Home className="h-4 w-4" />
                {isAdmin
                  ? "No residences yet. Click to add one."
                  : "No residences on file."}
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
        </DashboardLinkCard>
      </div>

      <div className="mt-6">
        <DashboardLinkCard
          to={`/admin/families/${familyId}/vehicles`}
          interactive={isAdmin}
          className="block"
        >
          <div className="card-header">
            <div className="card-title flex items-center gap-2">
              <Car className="h-4 w-4 text-primary" /> Cars &amp; trucks
              {dailyDrivers.length > 0 && (
                <span className="badge ml-1">{dailyDrivers.length}</span>
              )}
            </div>
            {isAdmin && (
              <span className="text-xs text-primary hover:underline">Manage →</span>
            )}
          </div>
          <div className="card-body">
            {dailyDrivers.length === 0 ? (
              <div className="text-sm text-muted-foreground flex items-center gap-2">
                <Car className="h-4 w-4" />
                {isAdmin
                  ? "No cars or trucks yet. Click to add the daily-driver fleet — boats, ATVs, and other vehicles live on the full Vehicles page."
                  : "No cars or trucks on file."}
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
        </DashboardLinkCard>
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

// Three large tiles that give a family member one-tap access to the
// surfaces they actually use: start a live conversation with Avi, pull
// up past session transcripts, or jump to their task list. The sidebar
// has the same three links for keyboard-driven navigation, but most
// family members will reach the Overview from their phone and tap a
// tile — this row is the single biggest cue that the app is theirs to
// use, not just theirs to look at.
function MemberHero({ familyId }: { familyId: string }) {
  return (
    <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 mb-6">
      <Link
        to={`/aiassistant/${familyId}`}
        className="card hover:shadow-md transition-shadow bg-primary text-primary-foreground border-primary"
      >
        <div className="card-body flex items-start gap-3">
          <div className="rounded-lg bg-primary-foreground/10 p-2 shrink-0">
            <Sparkles className="h-5 w-5" />
          </div>
          <div className="min-w-0">
            <div className="font-semibold">Chat with Avi</div>
            <div className="text-xs opacity-90 mt-0.5">
              Live voice + text. Ask anything.
            </div>
          </div>
        </div>
      </Link>
      <Link
        to={`/aiassistant/${familyId}/sessions`}
        className="card hover:shadow-md transition-shadow"
      >
        <div className="card-body flex items-start gap-3">
          <div className="rounded-lg bg-primary/10 p-2 text-primary shrink-0">
            <History className="h-5 w-5" />
          </div>
          <div className="min-w-0">
            <div className="font-semibold">Past conversations</div>
            <div className="text-xs text-muted-foreground mt-0.5">
              Every session you've been in with Avi.
            </div>
          </div>
        </div>
      </Link>
      <Link
        to={`/admin/families/${familyId}/tasks`}
        className="card hover:shadow-md transition-shadow"
      >
        <div className="card-body flex items-start gap-3">
          <div className="rounded-lg bg-primary/10 p-2 text-primary shrink-0">
            <ListTodo className="h-5 w-5" />
          </div>
          <div className="min-w-0">
            <div className="font-semibold">My tasks</div>
            <div className="text-xs text-muted-foreground mt-0.5">
              Things you own, were assigned, or follow.
            </div>
          </div>
        </div>
      </Link>
    </div>
  );
}

// Wrapper that flips between an interactive admin card (clickable, with
// hover shadow + keyboard handler for the assistant card) and a plain
// static container for members. Keeps the JSX above unchanged structure.
function DashboardCard({
  children,
  interactive,
  onActivate,
  className,
}: {
  children: ReactNode;
  interactive: boolean;
  onActivate?: () => void;
  className?: string;
}) {
  if (!interactive) {
    return <div className={`card ${className ?? ""}`}>{children}</div>;
  }
  return (
    <div
      role="link"
      tabIndex={0}
      onClick={onActivate}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onActivate?.();
        }
      }}
      className={`card hover:shadow-md transition-shadow cursor-pointer ${className ?? ""}`}
    >
      {children}
    </div>
  );
}

// Same idea for cards that wrap a `<Link>` instead of programmatic
// navigation: render a real link for admins, a static `<div>` for
// members so there's nothing to click.
function DashboardLinkCard({
  children,
  to,
  interactive,
  className,
}: {
  children: ReactNode;
  to: string;
  interactive: boolean;
  className?: string;
}) {
  if (!interactive) {
    return <div className={`card ${className ?? ""}`}>{children}</div>;
  }
  return (
    <Link
      to={to}
      className={`card hover:shadow-md transition-shadow ${className ?? ""}`}
    >
      {children}
    </Link>
  );
}

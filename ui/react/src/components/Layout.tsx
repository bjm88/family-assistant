import { useEffect, useState } from "react";
import {
  NavLink,
  Outlet,
  useLocation,
  useParams,
} from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  Bot,
  Building2,
  Car,
  FileText,
  History,
  Home,
  Landmark,
  ListTodo,
  Menu,
  Network,
  PawPrint,
  Settings,
  ShieldCheck,
  Sparkles,
  Users,
  X,
} from "lucide-react";
import { api } from "@/lib/api";
import type { Family } from "@/lib/types";
import { cn } from "@/lib/cn";
import { useAuth } from "@/lib/auth";
import { UserPill } from "@/components/UserPill";

// Admin/CRUD nav. Tasks intentionally lives in the top assistant
// cluster (alongside Live AI + Session history) because it's the
// daily-driver surface for the agent's TODOs and Automated Monitoring,
// not a static admin record like People or Vehicles.
const NAV = [
  { to: "", label: "Overview", icon: Home, end: true, adminOnly: false },
  { to: "people", label: "People", icon: Users, adminOnly: true },
  { to: "relationships", label: "Relationships", icon: Network, adminOnly: true },
  { to: "assistant", label: "Assistant", icon: Bot, adminOnly: true },
  { to: "vehicles", label: "Vehicles", icon: Car, adminOnly: true },
  { to: "pets", label: "Pets", icon: PawPrint, adminOnly: true },
  { to: "residences", label: "Residences", icon: Building2, adminOnly: true },
  { to: "insurance", label: "Insurance", icon: ShieldCheck, adminOnly: true },
  { to: "finances", label: "Finances", icon: Landmark, adminOnly: true },
  { to: "documents", label: "Documents", icon: FileText, adminOnly: true },
  { to: "status", label: "System status", icon: Activity, adminOnly: true },
  { to: "settings", label: "Family settings", icon: Settings, adminOnly: true },
];

export default function Layout() {
  const { familyId } = useParams();
  const { isAdmin } = useAuth();
  const location = useLocation();
  const { data: family } = useQuery<Family>({
    queryKey: ["family", familyId],
    queryFn: () => api.get<Family>(`/api/families/${familyId}`),
    enabled: !!familyId,
  });

  // Off-canvas drawer state for the sidebar on mobile (<md). Auto-closes
  // on every route change so a tap on a nav link both navigates AND
  // collapses the drawer back out of the way of the page content.
  const [drawerOpen, setDrawerOpen] = useState(false);
  useEffect(() => {
    setDrawerOpen(false);
  }, [location.pathname]);

  // Members see only the four daily-driver surfaces in the sidebar.
  // Admin-only entries are filtered out so the nav doesn't dangle dead
  // links that would 403 on click. The backend is the source of truth
  // for access — this is purely a UX cleanup.
  const visibleNav = NAV.filter((item) => isAdmin || !item.adminOnly);

  const sidebar = (
    <>
      <div className="px-5 py-5 border-b border-border flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="text-xs text-muted-foreground uppercase tracking-wide">
            Family Assistant
          </div>
          <div className="mt-1 font-semibold text-lg truncate">
            {family?.family_name ?? "Loading…"}
          </div>
        </div>
        {/* Close button only renders inside the mobile drawer. The same
            <aside> markup is reused for desktop, where the X stays
            hidden via ``md:hidden``. */}
        <button
          type="button"
          onClick={() => setDrawerOpen(false)}
          className="md:hidden -mr-1 p-1 text-muted-foreground hover:text-foreground"
          aria-label="Close menu"
        >
          <X className="h-5 w-5" />
        </button>
      </div>
      <div className="p-3 border-b border-border space-y-2">
        <NavLink
          to={`/aiassistant/${familyId}`}
          className="flex items-center gap-2 rounded-md px-3 py-2 text-sm bg-primary text-primary-foreground hover:bg-primary/90 transition-colors font-medium"
        >
          <Sparkles className="h-4 w-4" />
          Live AI Assistant
        </NavLink>
        <NavLink
          to={`/aiassistant/${familyId}/sessions`}
          className="flex items-center gap-2 rounded-md px-3 py-2 text-xs text-foreground/70 hover:bg-muted hover:text-foreground transition-colors"
        >
          <History className="h-4 w-4" />
          Session history
        </NavLink>
        <NavLink
          to={`/admin/families/${familyId}/tasks`}
          className={({ isActive }) =>
            cn(
              "flex items-center gap-2 rounded-md px-3 py-2 text-xs transition-colors",
              isActive
                ? "bg-primary/10 text-primary font-medium"
                : "text-foreground/70 hover:bg-muted hover:text-foreground"
            )
          }
        >
          <ListTodo className="h-4 w-4" />
          Tasks
        </NavLink>
      </div>
      <nav className="flex-1 p-3 space-y-1 overflow-y-auto">
        {visibleNav.map(({ to, label, icon: Icon, end }) => (
          <NavLink
            key={to}
            to={
              to === ""
                ? `/admin/families/${familyId}`
                : `/admin/families/${familyId}/${to}`
            }
            end={end}
            className={({ isActive }) =>
              cn(
                "flex items-center gap-3 rounded-md px-3 py-2 text-sm transition-colors",
                isActive
                  ? "bg-primary/10 text-primary font-medium"
                  : "text-foreground/70 hover:bg-muted hover:text-foreground"
              )
            }
          >
            <Icon className="h-4 w-4" />
            {label}
          </NavLink>
        ))}
      </nav>
      <div className="p-3 border-t border-border space-y-2">
        {isAdmin && (
          <NavLink
            to="/admin/families"
            className="text-xs text-muted-foreground hover:text-foreground block"
          >
            ← All families
          </NavLink>
        )}
        <div className="text-[11px] text-muted-foreground/80 flex gap-2">
          <a
            href="/legal/privacy-policy.html"
            target="_blank"
            rel="noopener"
            className="hover:text-foreground"
          >
            Privacy
          </a>
          <span aria-hidden>·</span>
          <a
            href="/legal/terms-of-service.html"
            target="_blank"
            rel="noopener"
            className="hover:text-foreground"
          >
            Terms
          </a>
        </div>
      </div>
    </>
  );

  return (
    <div className="min-h-screen md:flex bg-background">
      {/* Mobile-only scrim. Tapping it dismisses the drawer. */}
      {drawerOpen && (
        <button
          type="button"
          aria-label="Close menu overlay"
          className="fixed inset-0 z-30 bg-black/40 md:hidden"
          onClick={() => setDrawerOpen(false)}
        />
      )}

      <aside
        className={cn(
          "w-64 bg-white border-r border-border flex-col z-40",
          // Desktop: in-flow column on the left, always visible.
          "md:flex md:static md:translate-x-0 md:inset-auto",
          // Mobile: fixed off-canvas panel that slides in from the left.
          "fixed inset-y-0 left-0 transition-transform duration-200",
          drawerOpen ? "flex translate-x-0" : "-translate-x-full md:translate-x-0"
        )}
      >
        {sidebar}
      </aside>

      <main className="flex-1 min-w-0 flex flex-col">
        {/* Page-level top bar.
            - Mobile: hamburger + truncated family name + user pill.
            - Desktop: just the user pill, anchored top-right of the
              content column. The family name still lives in the
              sidebar header on the left, so the pill sitting on the
              right reads as "next to the family name". */}
        <div className="sticky top-0 z-20 bg-white/95 backdrop-blur border-b border-border h-14 flex items-center px-3 sm:px-6 gap-2">
          <button
            type="button"
            onClick={() => setDrawerOpen(true)}
            className="md:hidden p-2 -ml-2 text-muted-foreground hover:text-foreground"
            aria-label="Open menu"
          >
            <Menu className="h-5 w-5" />
          </button>
          <div className="md:hidden font-semibold truncate">
            {family?.family_name ?? "Family"}
          </div>
          <UserPill className="ml-auto" />
        </div>

        <div className="flex-1 min-w-0">
          <div className="max-w-6xl mx-auto p-4 sm:p-6 lg:p-8">
            <Outlet />
          </div>
        </div>
      </main>
    </div>
  );
}

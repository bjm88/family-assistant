import { NavLink, Outlet, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  Bot,
  Building2,
  Car,
  FileText,
  History,
  Home,
  Landmark,
  ListTodo,
  Network,
  PawPrint,
  Settings,
  ShieldCheck,
  Sparkles,
  Users,
} from "lucide-react";
import { api } from "@/lib/api";
import type { Family } from "@/lib/types";
import { cn } from "@/lib/cn";

const NAV = [
  { to: "", label: "Overview", icon: Home, end: true },
  { to: "people", label: "People", icon: Users },
  { to: "relationships", label: "Relationships", icon: Network },
  { to: "assistant", label: "Assistant", icon: Bot },
  { to: "vehicles", label: "Vehicles", icon: Car },
  { to: "pets", label: "Pets", icon: PawPrint },
  { to: "residences", label: "Residences", icon: Building2 },
  { to: "insurance", label: "Insurance", icon: ShieldCheck },
  { to: "finances", label: "Finances", icon: Landmark },
  { to: "documents", label: "Documents", icon: FileText },
  { to: "tasks", label: "Tasks", icon: ListTodo },
  { to: "settings", label: "Family settings", icon: Settings },
];

export default function Layout() {
  const { familyId } = useParams();
  const { data: family } = useQuery<Family>({
    queryKey: ["family", familyId],
    queryFn: () => api.get<Family>(`/api/families/${familyId}`),
    enabled: !!familyId,
  });

  return (
    <div className="min-h-screen flex">
      <aside className="w-64 bg-white border-r border-border flex flex-col">
        <div className="px-5 py-5 border-b border-border">
          <div className="text-xs text-muted-foreground uppercase tracking-wide">
            Family Assistant
          </div>
          <div className="mt-1 font-semibold text-lg truncate">
            {family?.family_name ?? "Loading…"}
          </div>
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
        </div>
        <nav className="flex-1 p-3 space-y-1">
          {NAV.map(({ to, label, icon: Icon, end }) => (
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
        <div className="p-3 border-t border-border">
          <NavLink
            to="/admin/families"
            className="text-xs text-muted-foreground hover:text-foreground"
          >
            ← All families
          </NavLink>
        </div>
      </aside>
      <main className="flex-1 min-w-0">
        <div className="max-w-6xl mx-auto p-8">
          <Outlet />
        </div>
      </main>
    </div>
  );
}

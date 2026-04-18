import { NavLink, Outlet, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  Bot,
  Building2,
  Car,
  FileText,
  Home,
  Landmark,
  Network,
  PawPrint,
  Settings,
  ShieldCheck,
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
        <nav className="flex-1 p-3 space-y-1">
          {NAV.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to === "" ? `/families/${familyId}` : `/families/${familyId}/${to}`}
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
            to="/families"
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

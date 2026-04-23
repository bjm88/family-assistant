// Browser-side session glue.
//
// `AuthProvider` calls GET /api/auth/me exactly once on mount and caches
// the answer in React Query under a stable key, so every page-level
// `useAuth()` reads from the same cache without re-fetching. We rely on
// the React Query `staleTime: Infinity` semantics here because:
//
//   * The cookie is HttpOnly and we can't tell from JS when it expires.
//     The backend's sliding-refresh middleware re-issues the cookie on
//     every authenticated request anyway, so as long as the user keeps
//     using the app the session stays alive.
//   * If the cookie is rejected (401), the global handler in
//     `lib/api.ts` redirects to /login with a `next=` param. That's the
//     same outcome we'd get from re-polling /me, but cheaper.
//
// `<RequireAuth>` and `<RequireAdmin>` are thin route wrappers used in
// App.tsx — they render their children only when the role check passes
// and otherwise <Navigate> to /login or back to the user's overview.
import {
  createContext,
  useContext,
  useMemo,
  type ReactNode,
} from "react";
import { Navigate, useLocation } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "./api";

export type UserRole = "admin" | "member";

export interface CurrentUser {
  email: string;
  role: UserRole;
  family_id: number | null;
  person_id: number | null;
  family_name: string | null;
}

interface AuthContextValue {
  user: CurrentUser | null;
  isLoading: boolean;
  isAdmin: boolean;
  isMember: boolean;
  // Forces /api/auth/me to refetch — useful right after a logout/login
  // so navigation reflects the new role without a hard reload.
  refresh: () => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

const ME_QUERY_KEY = ["auth", "me"] as const;

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const query = useQuery<CurrentUser | null>({
    queryKey: ME_QUERY_KEY,
    queryFn: async () => {
      try {
        return await api.get<CurrentUser>("/api/auth/me");
      } catch (err) {
        // 401 just means "not logged in" — surface as null instead of
        // bubbling the error so callers can render the login button.
        if (err instanceof ApiError && err.status === 401) return null;
        throw err;
      }
    },
    staleTime: Infinity,
    retry: false,
  });

  const value = useMemo<AuthContextValue>(() => {
    const user = query.data ?? null;
    return {
      user,
      isLoading: query.isLoading,
      isAdmin: user?.role === "admin",
      isMember: user?.role === "member",
      refresh: async () => {
        await queryClient.invalidateQueries({ queryKey: ME_QUERY_KEY });
      },
      logout: async () => {
        await api.post("/api/auth/logout");
        queryClient.setQueryData(ME_QUERY_KEY, null);
      },
    };
  }, [query.data, query.isLoading, queryClient]);

  return (
    <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error("useAuth must be used inside <AuthProvider>");
  }
  return ctx;
}

// Tiny full-screen spinner-ish placeholder while the initial /me probe
// is in flight. Avoids a flash of "redirect to /login" when the user
// is in fact logged in.
function AuthLoading() {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        minHeight: "100vh",
        color: "#888",
        fontFamily: "system-ui, sans-serif",
      }}
    >
      Loading…
    </div>
  );
}

export function RequireAuth({ children }: { children: ReactNode }) {
  const { user, isLoading } = useAuth();
  const location = useLocation();
  if (isLoading) return <AuthLoading />;
  if (!user) {
    const next = encodeURIComponent(location.pathname + location.search);
    return <Navigate to={`/login?next=${next}`} replace />;
  }
  return <>{children}</>;
}

export function RequireAdmin({ children }: { children: ReactNode }) {
  const { user, isLoading } = useAuth();
  const location = useLocation();
  if (isLoading) return <AuthLoading />;
  if (!user) {
    const next = encodeURIComponent(location.pathname + location.search);
    return <Navigate to={`/login?next=${next}`} replace />;
  }
  if (user.role !== "admin") {
    // Members never see CRUD pages — bounce to their own overview.
    const fallback =
      user.family_id != null ? `/admin/families/${user.family_id}` : "/";
    return <Navigate to={fallback} replace />;
  }
  return <>{children}</>;
}

// Resolve the home destination for the currently-signed-in role.
// Anonymous users go to /login; admins go to the families list; members
// go straight to their own overview. Used by the "/" route in App.tsx
// and by the post-login redirect.
export function useHomePath(): string {
  const { user, isLoading } = useAuth();
  if (isLoading) return "/login";
  if (!user) return "/login";
  if (user.role === "admin") return "/admin/families";
  if (user.family_id != null) return `/admin/families/${user.family_id}`;
  return "/login";
}

import { LogOut } from "lucide-react";
import { useAuth } from "@/lib/auth";
import { useToast } from "@/components/Toast";
import { cn } from "@/lib/cn";

interface UserPillProps {
  /**
   * When ``compact`` is true the email text is hidden in favour of just
   * the avatar disc + sign-out icon. Useful for narrow headers (the
   * Live AI page) where horizontal real estate is fought over by
   * status badges and the voice toggle.
   */
  compact?: boolean;
  className?: string;
}

/**
 * Sticky top-right identity pill.
 *
 * Renders the signed-in user's role + email plus a one-tap logout
 * button. Shared between the global ``Layout`` top bar (where it sits
 * to the right of the family name) and the standalone Live AI page
 * which uses its own custom header. Keeping the component shared means
 * a single place owns the logout + redirect flow and the visual
 * treatment can't drift between the two surfaces.
 *
 * Renders nothing for anonymous viewers — the public landing page and
 * legal pages don't need the pill.
 */
export function UserPill({ compact = false, className }: UserPillProps) {
  const { user, isAdmin, logout } = useAuth();
  const toast = useToast();

  if (!user) return null;

  // ``logout`` does its own hard navigation to /login (window.location
  // .assign) so the entire SPA — every component, ref, interval, query
  // cache — is recreated for the next user. We don't add a soft
  // ``navigate('/login')`` here because that would race the hard reload
  // and occasionally show a flash of the previous user's screen.
  const handleLogout = async () => {
    try {
      await logout();
    } catch (err) {
      toast.error(`Sign out failed: ${(err as Error).message}`);
    }
  };

  return (
    <div className={cn("flex items-center gap-2", className)}>
      {!compact && (
        <div className="hidden sm:flex flex-col items-end leading-tight">
          <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
            {isAdmin ? "Admin" : "Family member"}
          </div>
          <div
            className="text-xs font-medium text-foreground max-w-[200px] truncate"
            title={user.email}
          >
            {user.email}
          </div>
        </div>
      )}
      <div
        className="h-8 w-8 rounded-full bg-primary/10 text-primary flex items-center justify-center text-sm font-semibold"
        aria-hidden
        title={user.email}
      >
        {user.email.slice(0, 1).toUpperCase()}
      </div>
      <button
        type="button"
        onClick={handleLogout}
        className="inline-flex items-center justify-center h-8 w-8 rounded-md border border-border text-muted-foreground hover:text-foreground hover:bg-muted"
        title="Sign out"
        aria-label="Sign out"
      >
        <LogOut className="h-4 w-4" />
      </button>
    </div>
  );
}

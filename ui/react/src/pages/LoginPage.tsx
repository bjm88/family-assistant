// Single-button Google sign-in landing page.
//
// We deliberately don't render anything else here — no app chrome, no
// nav, no logged-out hint about which families exist on this server.
// The page reads two query params:
//
//   * `next=`    — where to send the user after a successful login.
//                  Forwarded as a query string to /api/auth/google/start
//                  so the backend can store it in the OAuth state and
//                  redirect to it from the callback.
//   * `error=`   — set by the backend when the OAuth callback rejects
//                  (unknown email, OAuth failure, etc). We display a
//                  short human-readable explanation; the recognised
//                  values come from the auth router (e.g. "unauthorised",
//                  "oauth_failed", "unconfigured", "no_email").
//
// If the user is already logged in we skip straight to their home page
// instead of showing the button. This keeps the back button from
// stranding people on /login after they've signed in.
import { useEffect } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useAuth, useHomePath } from "@/lib/auth";

const ERROR_MESSAGES: Record<string, string> = {
  unauthorised:
    "That Google account isn't on the household roster. Ask an admin to add your email under People before signing in again.",
  oauth_failed:
    "Google sign-in failed. Please try again — if the problem persists, reach out to an admin.",
  unconfigured:
    "Google sign-in isn't configured on this server yet. An admin needs to set USER_LOGIN_GOOGLE_CLIENT_ID and SESSION_SECRET_KEY.",
  no_email:
    "Google didn't return an email address for that account. Try a different account or contact an admin.",
  state_mismatch:
    "Sign-in session expired. Please click the button again.",
};

function useQueryParam(name: string): string | null {
  const location = useLocation();
  const params = new URLSearchParams(location.search);
  return params.get(name);
}

export default function LoginPage() {
  const navigate = useNavigate();
  const { user, isLoading } = useAuth();
  const home = useHomePath();
  const next = useQueryParam("next");
  const error = useQueryParam("error");

  // Already signed in? Skip the button. We use `useEffect` instead of
  // returning <Navigate> directly so React Query's cached /me result
  // gets to settle on the first render — otherwise we'd briefly
  // navigate, then flip back when the cache invalidates.
  useEffect(() => {
    if (!isLoading && user) {
      navigate(next && next.startsWith("/") ? next : home, { replace: true });
    }
  }, [home, isLoading, navigate, next, user]);

  const startUrl = (() => {
    const u = new URL("/api/auth/google/start", window.location.origin);
    if (next && next.startsWith("/")) u.searchParams.set("next", next);
    return u.toString();
  })();

  const errorMessage = error
    ? ERROR_MESSAGES[error] ??
      `Sign-in failed (${error}). Please try again or contact an admin.`
    : null;

  return (
    <div
      style={{
        minHeight: "100vh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: "2rem",
        background:
          "linear-gradient(135deg, #f5f7fb 0%, #eef2f9 50%, #e6ecf6 100%)",
        fontFamily: "system-ui, -apple-system, 'Segoe UI', sans-serif",
      }}
    >
      <div
        style={{
          width: "100%",
          maxWidth: 420,
          background: "#fff",
          borderRadius: 18,
          padding: "2.5rem 2rem",
          boxShadow: "0 10px 40px rgba(15, 23, 42, 0.08)",
          textAlign: "center",
        }}
      >
        <h1 style={{ margin: 0, fontSize: 24, color: "#0f172a" }}>
          Family Assistant
        </h1>
        <p style={{ margin: "0.75rem 0 1.5rem", color: "#475569" }}>
          Sign in with your Google account to continue.
        </p>

        {errorMessage && (
          <div
            role="alert"
            style={{
              background: "#fee2e2",
              color: "#991b1b",
              border: "1px solid #fecaca",
              borderRadius: 10,
              padding: "0.75rem 1rem",
              marginBottom: "1.25rem",
              fontSize: 14,
              textAlign: "left",
              lineHeight: 1.4,
            }}
          >
            {errorMessage}
          </div>
        )}

        <a
          href={startUrl}
          style={{
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
            gap: 12,
            width: "100%",
            padding: "0.85rem 1.25rem",
            borderRadius: 10,
            background: "#0f172a",
            color: "#fff",
            textDecoration: "none",
            fontWeight: 600,
            fontSize: 15,
          }}
        >
          <GoogleGlyph />
          Sign in with Google
        </a>

        <p
          style={{
            margin: "1.5rem 0 0",
            color: "#64748b",
            fontSize: 13,
            lineHeight: 1.5,
          }}
        >
          Only emails on the household roster (or an admin email) can sign in.
        </p>
      </div>
    </div>
  );
}

// Inline 4-colour Google "G" so we don't pull in another icon library.
function GoogleGlyph() {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
      <path
        fill="#EA4335"
        d="M9 3.48c1.69 0 2.83.73 3.48 1.34l2.54-2.48C13.46.89 11.43 0 9 0 5.48 0 2.44 2.02.96 4.96l2.91 2.26C4.59 5.05 6.62 3.48 9 3.48z"
      />
      <path
        fill="#4285F4"
        d="M17.64 9.2c0-.74-.06-1.28-.19-1.84H9v3.34h4.96c-.1.83-.64 2.08-1.84 2.92l2.84 2.2c1.7-1.57 2.68-3.88 2.68-6.62z"
      />
      <path
        fill="#FBBC05"
        d="M3.88 10.78A5.54 5.54 0 0 1 3.58 9c0-.62.11-1.22.29-1.78L.96 4.96A9.008 9.008 0 0 0 0 9c0 1.45.35 2.82.96 4.04l2.92-2.26z"
      />
      <path
        fill="#34A853"
        d="M9 18c2.43 0 4.47-.8 5.96-2.18l-2.84-2.2c-.76.53-1.78.9-3.12.9-2.38 0-4.41-1.57-5.13-3.74L.97 13.04C2.45 15.98 5.48 18 9 18z"
      />
    </svg>
  );
}

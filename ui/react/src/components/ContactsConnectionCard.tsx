import { useEffect, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  BookUser,
  CheckCircle2,
  PlugZap,
  RefreshCw,
  XCircle,
} from "lucide-react";
import { api } from "@/lib/api";
import type {
  ContactsProviderStatus,
  ContactsStatus,
  ContactsTestResult,
} from "@/lib/types";
import { useToast } from "@/components/Toast";
import { useConfirm } from "@/components/ConfirmDialog";

// Each family member links their *own* address book, so "add her to my
// contacts" lands in the contacts of whoever asked. This card shows the
// state of that link for one person and starts the consent flow.
export function ContactsConnectionCard({
  personId,
  personFirstName,
  returnTo,
}: {
  personId: number;
  personFirstName?: string;
  // Same-site path Google's consent flow should come back to.
  returnTo: string;
}) {
  const qc = useQueryClient();
  const toast = useToast();
  const confirm = useConfirm();
  const who = personFirstName || "this person";
  const queryKey = ["contacts-status", personId];

  const { data: status, isLoading, error } = useQuery<ContactsStatus>({
    queryKey,
    queryFn: () =>
      api.get<ContactsStatus>(`/api/contacts/status?person_id=${personId}`),
  });

  // The OAuth callback returns here with ?contacts_connected=<account>
  // or ?contacts_error=<reason>. Show it once, then tidy the URL.
  useEffect(() => {
    const url = new URL(window.location.href);
    const ok = url.searchParams.get("contacts_connected");
    const err = url.searchParams.get("contacts_error");
    if (!ok && !err) return;
    if (ok) toast.success(`Contacts connected for ${ok}.`);
    if (err) toast.error(`Contacts weren't connected: ${err}`);
    url.searchParams.delete("contacts_connected");
    url.searchParams.delete("contacts_error");
    window.history.replaceState({}, "", url.pathname + url.search);
    qc.invalidateQueries({ queryKey: ["contacts-status", personId] });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [personId]);

  const test = useMutation({
    mutationFn: (provider: string) =>
      api.post<ContactsTestResult>(
        `/api/contacts/test?person_id=${personId}&provider=${provider}`
      ),
    onSuccess: (r) => {
      const n = r.contact_count;
      toast.success(
        `Avi can reach the contacts in ${r.account_identifier} (${n} ${
          n === 1 ? "contact" : "contacts"
        }).`
      );
    },
    onError: (err: Error) => toast.error(err.message),
    // A failed check can flip the connection to "needs reconnecting".
    onSettled: () => qc.invalidateQueries({ queryKey }),
  });

  const disconnect = useMutation({
    mutationFn: (provider: string) =>
      api.del(`/api/contacts/connection?person_id=${personId}&provider=${provider}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey });
      toast.success("Contacts disconnected.");
    },
    onError: (err: Error) => toast.error(`Could not disconnect: ${err.message}`),
  });

  const startUrl =
    `/api/contacts/google/oauth/start?person_id=${personId}` +
    `&return_to=${encodeURIComponent(returnTo)}`;

  return (
    <div className="card">
      <div className="card-header">
        <div className="card-title flex items-center gap-2">
          <BookUser className="h-5 w-5 text-primary" /> Contacts
        </div>
      </div>
      <div className="card-body space-y-4">
        <p className="text-sm text-muted-foreground">
          Let Avi keep {who}'s address book up to date. Forward him an email,
          share a contact card, send a photo of a business card or just paste
          someone's details and ask him to save them — he checks whether the
          person is already there, then adds or updates them.
        </p>

        {isLoading && (
          <div className="text-sm text-muted-foreground">Checking connection…</div>
        )}
        {error && (
          <div className="text-sm text-destructive">
            Couldn't load the contacts connection: {(error as Error).message}
          </div>
        )}

        {status && !status.google_oauth_configured && (
          <Notice title="Google sign-in isn't set up yet">
            Set <span className="font-mono">GOOGLE_OAUTH_CLIENT_ID</span> and{" "}
            <span className="font-mono">GOOGLE_OAUTH_CLIENT_SECRET</span> in{" "}
            <span className="font-mono">.env</span> and restart the API. In
            Google Cloud Console, enable the People API and add{" "}
            <span className="font-mono break-all">{status.google_redirect_uri}</span>{" "}
            as an authorised redirect URI.
          </Notice>
        )}

        {status?.providers.map((p) => (
          <ProviderRow
            key={p.provider}
            provider={p}
            who={who}
            status={status}
            startUrl={startUrl}
            testing={test.isPending && test.variables === p.provider}
            disconnecting={disconnect.isPending && disconnect.variables === p.provider}
            onTest={() => test.mutate(p.provider)}
            onDisconnect={async () => {
              if (
                await confirm({
                  title: `Disconnect ${p.label}?`,
                  message: `Avi will no longer be able to look up or save contacts for ${who}. Nothing already in the address book is changed or removed.`,
                  destructive: true,
                  confirmLabel: "Disconnect",
                })
              ) {
                disconnect.mutate(p.provider);
              }
            }}
          />
        ))}
      </div>
    </div>
  );
}

function ProviderRow({
  provider: p,
  who,
  status,
  startUrl,
  testing,
  disconnecting,
  onTest,
  onDisconnect,
}: {
  provider: ContactsProviderStatus;
  who: string;
  status: ContactsStatus;
  startUrl: string;
  testing: boolean;
  disconnecting: boolean;
  onTest: () => void;
  onDisconnect: () => void;
}) {
  // Only Google has a consent flow today; other providers are placeholders.
  const canConnect =
    p.available && p.provider === "google" && status.google_oauth_configured;

  return (
    <div className="border border-border rounded-md p-4 space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <div className="font-medium">{p.label}</div>
        {p.is_default && p.available && <span className="badge">default</span>}
        {!p.available && <span className="badge">coming soon</span>}
        {p.connected && !p.needs_reconnect && (
          <span className="inline-flex items-center gap-1 text-sm text-emerald-700 dark:text-emerald-400">
            <CheckCircle2 className="h-4 w-4" /> Connected
          </span>
        )}
      </div>

      {!p.available && (
        <p className="text-sm text-muted-foreground">
          Not supported yet — for now Avi can only manage Google Contacts.
        </p>
      )}

      {p.available && !p.connected && (
        <>
          <p className="text-sm">{status.permission_summary}</p>
          {canConnect && (
            <div className="flex flex-col items-start gap-2">
              <a href={startUrl} className="btn-primary">
                <PlugZap className="h-4 w-4" /> Connect {p.label}
              </a>
              <p className="text-xs text-muted-foreground">
                {status.google_consent_note} Sign in with {who}'s own Google
                account — that is the address book Avi will use for them.
              </p>
            </div>
          )}
        </>
      )}

      {p.connected && (
        <>
          <div className="text-sm">
            Signed in as <span className="font-medium">{p.account_identifier}</span>
            {p.connected_at && (
              <span className="text-muted-foreground">
                {" "}
                · since {new Date(p.connected_at).toLocaleDateString()}
              </span>
            )}
          </div>

          {p.needs_reconnect && (
            <Notice title="Needs reconnecting">
              Google is no longer accepting this connection — access was
              withdrawn or has expired. Avi can't look up or save contacts for{" "}
              {who} until it is connected again.
            </Notice>
          )}
          {p.account_matches_person === false && (
            <Notice title="Different account">
              This isn't the email address on {who}'s profile. If the wrong
              Google account was picked, reconnect and choose the right one —
              contacts Avi saves for {who} go into{" "}
              <span className="font-medium">{p.account_identifier}</span>.
            </Notice>
          )}

          <p className="text-xs text-muted-foreground">{status.permission_summary}</p>

          <div className="flex flex-wrap gap-2">
            {!p.needs_reconnect && (
              <button
                type="button"
                className="btn-secondary"
                disabled={testing}
                onClick={onTest}
              >
                <CheckCircle2 className="h-4 w-4" />
                {testing ? "Checking…" : "Test connection"}
              </button>
            )}
            {canConnect && (
              <a
                href={startUrl}
                className={p.needs_reconnect ? "btn-primary" : "btn-secondary"}
              >
                <RefreshCw className="h-4 w-4" /> Reconnect
              </a>
            )}
            <button
              type="button"
              className="btn-secondary"
              disabled={disconnecting}
              onClick={onDisconnect}
            >
              <XCircle className="h-4 w-4" />
              {disconnecting ? "Disconnecting…" : "Disconnect"}
            </button>
          </div>
        </>
      )}
    </div>
  );
}

function Notice({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="border border-amber-300 bg-amber-50 dark:border-amber-700 dark:bg-amber-950/30 text-amber-900 dark:text-amber-100 rounded-md p-3 text-sm flex gap-2">
      <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5" />
      <div>
        <div className="font-semibold mb-1">{title}</div>
        <div>{children}</div>
      </div>
    </div>
  );
}

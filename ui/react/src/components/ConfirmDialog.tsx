import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { AlertTriangle } from "lucide-react";
import { Modal } from "./Modal";

interface ConfirmDialogProps {
  open: boolean;
  onCancel: () => void;
  onConfirm: () => void;
  title: string;
  message: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  destructive?: boolean;
  busy?: boolean;
}

/**
 * Small modal-based confirmation prompt.
 *
 * Use this instead of the browser-native ``confirm(...)`` for any
 * irreversible action — it stays inside our design system, supports
 * a busy state during the actual mutation, and lets us style the
 * destructive button distinctly so a user does not blow away data
 * by miss-clicking through a generic OS dialog.
 */
export function ConfirmDialog({
  open,
  onCancel,
  onConfirm,
  title,
  message,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  destructive = false,
  busy = false,
}: ConfirmDialogProps) {
  if (!open) return null;
  return (
    <Modal
      open
      onClose={busy ? () => {} : onCancel}
      title={title}
      footer={
        <>
          <button
            className="btn-secondary"
            onClick={onCancel}
            disabled={busy}
          >
            {cancelLabel}
          </button>
          <button
            className={destructive ? "btn-destructive" : "btn-primary"}
            onClick={onConfirm}
            disabled={busy}
          >
            {busy ? "Working…" : confirmLabel}
          </button>
        </>
      }
    >
      <div className="flex items-start gap-3">
        {destructive && (
          <AlertTriangle
            className="h-5 w-5 text-destructive shrink-0 mt-0.5"
            aria-hidden
          />
        )}
        <div className="text-sm text-foreground/90 leading-relaxed">
          {message}
        </div>
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// useConfirm() — promise-returning hook backed by a single global dialog.
//
// Why this exists
// ---------------
// Every CRUD page has a few destructive actions (delete pet, remove
// medication, leave family). Wiring the JSX-style ``ConfirmDialog``
// at each call site forces three things per site: a piece of state,
// a held "what to confirm" closure, and the dialog element somewhere
// in the page's tree. That's ~15 lines of boilerplate for what
// conceptually is "ask before doing this".
//
// ``useConfirm()`` collapses all of that to a single async call:
//
//     const confirm = useConfirm();
//     ...
//     onClick={async () => {
//       const ok = await confirm({
//         title: "Remove this pet?",
//         message: <>This will erase {pet.name}'s photos and notes.</>,
//         destructive: true,
//         confirmLabel: "Remove",
//       });
//       if (ok) del.mutate(pet.pet_id);
//     }}
//
// One <ConfirmProvider> at the app root owns the dialog state; the
// hook just enqueues a request and resolves with true/false when the
// user picks. Multiple concurrent confirms are not supported (yes is
// an unusual UX); a second call while a dialog is open replaces the
// first, which is the right behavior for race-prone interactions.
// ---------------------------------------------------------------------------

interface ConfirmRequest {
  title: string;
  message: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  destructive?: boolean;
}

type ConfirmFn = (req: ConfirmRequest) => Promise<boolean>;

const ConfirmContext = createContext<ConfirmFn | null>(null);

export function useConfirm(): ConfirmFn {
  const ctx = useContext(ConfirmContext);
  if (!ctx) {
    throw new Error("useConfirm() must be used inside <ConfirmProvider>");
  }
  return ctx;
}

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [request, setRequest] = useState<ConfirmRequest | null>(null);
  // Resolver lives in a ref so calling ``confirm({...})`` again
  // before the previous one resolved cleanly cancels the older
  // promise (resolved with false) — no orphan promises.
  const resolverRef = useRef<((value: boolean) => void) | null>(null);

  const confirm = useCallback<ConfirmFn>((req) => {
    return new Promise<boolean>((resolve) => {
      if (resolverRef.current) {
        resolverRef.current(false);
      }
      resolverRef.current = resolve;
      setRequest(req);
    });
  }, []);

  const close = useCallback((result: boolean) => {
    if (resolverRef.current) {
      resolverRef.current(result);
      resolverRef.current = null;
    }
    setRequest(null);
  }, []);

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      <ConfirmDialog
        open={request !== null}
        title={request?.title ?? ""}
        message={request?.message ?? null}
        confirmLabel={request?.confirmLabel}
        cancelLabel={request?.cancelLabel}
        destructive={useMemo(() => Boolean(request?.destructive), [request])}
        onCancel={() => close(false)}
        onConfirm={() => close(true)}
      />
    </ConfirmContext.Provider>
  );
}

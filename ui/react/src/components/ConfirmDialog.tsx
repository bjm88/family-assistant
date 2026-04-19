import type { ReactNode } from "react";
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

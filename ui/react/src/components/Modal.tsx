import { X } from "lucide-react";
import type { ReactNode } from "react";

interface ModalProps {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
}

export function Modal({ open, onClose, title, children, footer, wide }: ModalProps) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4">
      <div
        className={`bg-white rounded-lg shadow-xl border border-border w-full ${
          wide ? "max-w-3xl" : "max-w-lg"
        } max-h-[90vh] flex flex-col`}
      >
        <div className="px-5 py-4 border-b border-border flex items-center justify-between">
          <h2 className="font-semibold">{title}</h2>
          <button
            type="button"
            onClick={onClose}
            className="text-muted-foreground hover:text-foreground"
          >
            <X className="h-5 w-5" />
          </button>
        </div>
        <div className="px-5 py-4 overflow-y-auto">{children}</div>
        {footer && (
          <div className="px-5 py-3 border-t border-border flex justify-end gap-2 bg-muted/40">
            {footer}
          </div>
        )}
      </div>
    </div>
  );
}

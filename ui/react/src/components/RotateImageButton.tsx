import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { useMutation } from "@tanstack/react-query";
import { RotateCcw, RotateCw } from "lucide-react";
import { api } from "@/lib/api";
import { Modal } from "@/components/Modal";
import { useToast } from "@/components/Toast";

export type RotatableImageKind =
  | "identity_document"
  | "document"
  | "person_profile"
  | "person_photo"
  | "pet_photo"
  | "residence_photo"
  | "vehicle_photo"
  | "insurance_policy_attachment"
  | "vehicle_attachment";

interface RotateImageButtonProps {
  kind: RotatableImageKind;
  // The record the image belongs to: the identity document, document,
  // person, person photo, pet photo, residence photo, vehicle, or
  // insurance-policy / vehicle attachment.
  recordId: number;
  // Identity documents only: which scan.
  side?: "front" | "back";
  // Where the browser loads the image from. It must change when the
  // image is saved rotated (stored paths do), so a cached copy of the
  // old picture is never shown.
  src: string;
  // What the picture is, for the dialog title: "Front of passport".
  label: string;
  // Refresh whatever lists show this image. The dialog waits for the
  // returned promise so the page already shows the saved picture when
  // it closes.
  onRotated: () => unknown;
  // "button": outlined button with text. "link": small text link.
  // "icon": icon only, for table rows.
  variant?: "button" | "link" | "icon";
  className?: string;
}

const VARIANT_CLASS = {
  button: "btn-secondary",
  link: "text-muted-foreground hover:text-foreground text-xs inline-flex items-center gap-1",
  icon: "text-muted-foreground hover:text-foreground",
} as const;

const TURNED_TEXT = [
  "Not rotated yet.",
  "Turned 90° clockwise.",
  "Turned upside down (180°).",
  "Turned 90° counter-clockwise.",
];

/**
 * A "Rotate" control for a stored image.
 *
 * Opens a dialog showing the picture with "Rotate left" and "Rotate
 * right" buttons, each a quarter turn. The turns are only a preview
 * until Save, which stores the picture turned by the net amount in a
 * single request.
 */
export function RotateImageButton({
  kind,
  recordId,
  side,
  src,
  label,
  onRotated,
  variant = "link",
  className,
}: RotateImageButtonProps) {
  const toast = useToast();
  const [open, setOpen] = useState(false);
  // Quarter turns clockwise since the dialog opened. Kept as a running
  // total (it can go negative or past 4) so the preview always spins
  // the short way in the direction that was clicked.
  const [turns, setTurns] = useState(0);
  const net = ((turns % 4) + 4) % 4;

  useEffect(() => {
    if (open) setTurns(0);
  }, [open, src]);

  const save = useMutation({
    mutationFn: async () => {
      await api.post("/api/images/rotate", {
        kind,
        record_id: recordId,
        side,
        degrees: net * 90,
      });
      await onRotated();
    },
    onSuccess: () => {
      setOpen(false);
      toast.success("Rotated image saved.");
    },
    onError: (err: Error) =>
      toast.error(`Could not save the rotated image: ${err.message}`),
  });

  const close = () => {
    if (!save.isPending) setOpen(false);
  };

  const dialog = (
    <Modal
      open
      onClose={close}
      title={`Rotate ${label}`}
      footer={
        <>
          <button
            type="button"
            className="btn-secondary"
            onClick={close}
            disabled={save.isPending}
          >
            Cancel
          </button>
          <button
            type="button"
            className="btn-primary"
            onClick={() => save.mutate()}
            disabled={net === 0 || save.isPending}
          >
            {save.isPending ? "Saving…" : "Save"}
          </button>
        </>
      }
    >
      <div className="flex flex-col items-center gap-4">
        {/* Square, so the picture fits whichever way it is turned. */}
        <div className="w-full max-w-[22rem] aspect-square bg-muted rounded-md border border-border overflow-hidden flex items-center justify-center">
          <img
            src={src}
            alt={label}
            className="max-h-full max-w-full object-contain transition-transform duration-200"
            style={{ transform: `rotate(${turns * 90}deg)` }}
          />
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            className="btn-secondary"
            onClick={() => setTurns((t) => t - 1)}
            disabled={save.isPending}
          >
            <RotateCcw className="h-4 w-4" /> Rotate left
          </button>
          <button
            type="button"
            className="btn-secondary"
            onClick={() => setTurns((t) => t + 1)}
            disabled={save.isPending}
          >
            <RotateCw className="h-4 w-4" /> Rotate right
          </button>
        </div>
        <div className="text-xs text-muted-foreground" aria-live="polite">
          {TURNED_TEXT[net]}
        </div>
      </div>
    </Modal>
  );

  return (
    <>
      <button
        type="button"
        className={`${VARIANT_CLASS[variant]} ${className ?? ""}`}
        title={`Rotate ${label}`}
        aria-label={`Rotate ${label}`}
        onClick={() => setOpen(true)}
      >
        <RotateCw className={variant === "link" ? "h-3.5 w-3.5" : "h-4 w-4"} />
        {variant !== "icon" && "Rotate"}
      </button>

      {/* Rendered at the top of the page, not where the button sits, so
          the dialog looks the same wherever the button is: inside a
          table cell, a small-print caption or another dialog. */}
      {open && createPortal(dialog, document.body)}
    </>
  );
}

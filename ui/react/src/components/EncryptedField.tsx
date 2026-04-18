import { Lock } from "lucide-react";
import type { ReactNode } from "react";

interface EncryptedFieldProps {
  label: string;
  htmlFor: string;
  /** Last four characters of the stored value (e.g. VIN, plate, policy
   * number). When set in edit mode, a green "On file" badge is rendered
   * and leaving the input blank preserves the existing ciphertext. */
  lastFour: string | null;
  mode: "create" | "edit";
  children: ReactNode;
}

/**
 * A form field wrapper for values that are encrypted at rest (Fernet)
 * and never round-tripped to the client. Renders a prominent "on file"
 * indicator when an existing value is stored so the user understands
 * the field isn't empty in the database — the cleartext just isn't
 * surfaced. Replaces the value when the input is non-empty on submit.
 */
export function EncryptedField({
  label,
  htmlFor,
  lastFour,
  mode,
  children,
}: EncryptedFieldProps) {
  const onFile = mode === "edit" && lastFour;
  return (
    <div>
      <label className="label" htmlFor={htmlFor}>
        {label}
      </label>
      {onFile ? (
        <div className="mb-1 inline-flex items-center gap-1.5 rounded-md border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-xs text-emerald-700">
          <Lock className="h-3 w-3" />
          On file ending in <span className="font-mono">{lastFour}</span>
        </div>
      ) : (
        <div className="mb-1 inline-flex items-center gap-1 text-xs text-muted-foreground">
          <Lock className="h-3 w-3" /> Encrypted at rest
        </div>
      )}
      {children}
    </div>
  );
}

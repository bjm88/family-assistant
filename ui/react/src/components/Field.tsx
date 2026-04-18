import type { ReactNode } from "react";

interface FieldProps {
  label: string;
  htmlFor?: string;
  error?: string;
  hint?: string;
  children: ReactNode;
}

export function Field({ label, htmlFor, error, hint, children }: FieldProps) {
  return (
    <div>
      <label className="label" htmlFor={htmlFor}>
        {label}
      </label>
      {children}
      {hint && !error && (
        <div className="text-xs text-muted-foreground mt-1">{hint}</div>
      )}
      {error && <div className="text-xs text-destructive mt-1">{error}</div>}
    </div>
  );
}

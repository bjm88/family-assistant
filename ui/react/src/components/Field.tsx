import type { ReactNode } from "react";

interface FieldProps {
  // Optional so consumers that supply their own inline label (e.g. a
  // checkbox row whose <label> wraps the input) can use Field purely
  // for layout/error/hint plumbing without rendering a duplicate
  // header label above their content.
  label?: string;
  htmlFor?: string;
  error?: string;
  hint?: string;
  children: ReactNode;
}

export function Field({ label, htmlFor, error, hint, children }: FieldProps) {
  return (
    <div>
      {label ? (
        <label className="label" htmlFor={htmlFor}>
          {label}
        </label>
      ) : null}
      {children}
      {hint && !error && (
        <div className="text-xs text-muted-foreground mt-1">{hint}</div>
      )}
      {error && <div className="text-xs text-destructive mt-1">{error}</div>}
    </div>
  );
}

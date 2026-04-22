import { Pencil, Trash2 } from "lucide-react";

interface RowActionsProps {
  onEdit: (e: React.MouseEvent) => void;
  onDelete: (e: React.MouseEvent) => void;
  /** Used in aria-labels so screen readers say e.g. "Edit vehicle". */
  entityName: string;
}

/**
 * The Pencil + Trash2 pair that sits at the end of every CRUD table
 * row in the admin UI (Vehicles, Residences, Pets, Insurance, etc.).
 *
 * Pulled out of the page-level files so the styling stays in one place
 * — the previous copies all matched but were a maintenance hazard:
 * tweaking the hover color used to be a 5-file find-and-replace.
 *
 * The handlers receive the original click event so the caller can
 * still ``e.stopPropagation()`` (every table row uses the row itself
 * as the "open editor" affordance, and we don't want a click on the
 * trash can to also open the editor).
 */
export function RowActions({ onEdit, onDelete, entityName }: RowActionsProps) {
  return (
    <>
      <button
        type="button"
        className="text-muted-foreground hover:text-foreground mr-3"
        onClick={onEdit}
        aria-label={`Edit ${entityName}`}
      >
        <Pencil className="h-4 w-4" />
      </button>
      <button
        type="button"
        className="text-destructive hover:text-destructive/80"
        onClick={onDelete}
        aria-label={`Delete ${entityName}`}
      >
        <Trash2 className="h-4 w-4" />
      </button>
    </>
  );
}

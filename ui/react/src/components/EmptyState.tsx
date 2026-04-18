import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

interface EmptyStateProps {
  icon: LucideIcon;
  title: string;
  description?: string;
  action?: ReactNode;
}

export function EmptyState({ icon: Icon, title, description, action }: EmptyStateProps) {
  return (
    <div className="card">
      <div className="card-body flex flex-col items-center justify-center text-center py-16">
        <div className="rounded-full bg-muted p-3 mb-4">
          <Icon className="h-6 w-6 text-muted-foreground" />
        </div>
        <div className="font-medium">{title}</div>
        {description && (
          <div className="text-sm text-muted-foreground mt-1 max-w-sm">{description}</div>
        )}
        {action && <div className="mt-4">{action}</div>}
      </div>
    </div>
  );
}

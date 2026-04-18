import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { CheckCircle2, XCircle, X } from "lucide-react";
import { cn } from "@/lib/cn";

type ToastKind = "success" | "error";
interface ToastItem {
  id: number;
  kind: ToastKind;
  message: string;
}

interface ToastApi {
  success: (message: string) => void;
  error: (message: string) => void;
}

const ToastCtx = createContext<ToastApi | null>(null);

export function useToast(): ToastApi {
  const ctx = useContext(ToastCtx);
  if (!ctx) throw new Error("useToast must be used inside <ToastProvider>");
  return ctx;
}

let _seq = 0;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);

  const remove = useCallback((id: number) => {
    setItems((xs) => xs.filter((x) => x.id !== id));
  }, []);

  const push = useCallback(
    (kind: ToastKind, message: string) => {
      const id = ++_seq;
      setItems((xs) => [...xs, { id, kind, message }]);
      setTimeout(() => remove(id), kind === "success" ? 2500 : 5000);
    },
    [remove]
  );

  const api: ToastApi = {
    success: (m) => push("success", m),
    error: (m) => push("error", m),
  };

  return (
    <ToastCtx.Provider value={api}>
      {children}
      <Toaster items={items} onClose={remove} />
    </ToastCtx.Provider>
  );
}

function Toaster({
  items,
  onClose,
}: {
  items: ToastItem[];
  onClose: (id: number) => void;
}) {
  return (
    <div className="fixed top-4 right-4 z-[60] flex flex-col gap-2 items-end pointer-events-none">
      {items.map((t) => (
        <ToastCard key={t.id} item={t} onClose={() => onClose(t.id)} />
      ))}
    </div>
  );
}

function ToastCard({ item, onClose }: { item: ToastItem; onClose: () => void }) {
  const [entered, setEntered] = useState(false);
  useEffect(() => {
    const id = requestAnimationFrame(() => setEntered(true));
    return () => cancelAnimationFrame(id);
  }, []);

  const Icon = item.kind === "success" ? CheckCircle2 : XCircle;
  return (
    <div
      className={cn(
        "pointer-events-auto min-w-[280px] max-w-md rounded-lg shadow-lg border px-4 py-3 flex items-start gap-3 bg-white transition-all",
        entered ? "translate-x-0 opacity-100" : "translate-x-4 opacity-0",
        item.kind === "success" ? "border-emerald-200" : "border-destructive/40"
      )}
    >
      <Icon
        className={cn(
          "h-5 w-5 mt-0.5 shrink-0",
          item.kind === "success" ? "text-emerald-600" : "text-destructive"
        )}
      />
      <div className="flex-1 text-sm">{item.message}</div>
      <button
        onClick={onClose}
        className="text-muted-foreground hover:text-foreground shrink-0"
        aria-label="dismiss"
      >
        <X className="h-4 w-4" />
      </button>
    </div>
  );
}

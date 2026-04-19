import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  AlertCircle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  HelpCircle,
  Loader2,
  RefreshCw,
  XCircle,
} from "lucide-react";
import { api } from "@/lib/api";
import type {
  SystemStatusCheck,
  SystemStatusLevel,
  SystemStatusReport,
} from "@/lib/types";
import { PageHeader } from "@/components/PageHeader";
import { cn } from "@/lib/cn";

// ---------------------------------------------------------------------------
// Status palette + helpers
// ---------------------------------------------------------------------------

interface LevelMeta {
  label: string;
  pillClass: string;
  iconClass: string;
  Icon: typeof CheckCircle2;
}

const LEVEL_META: Record<SystemStatusLevel, LevelMeta> = {
  ok: {
    label: "Healthy",
    pillClass: "bg-emerald-100 text-emerald-800 border-emerald-200",
    iconClass: "text-emerald-600",
    Icon: CheckCircle2,
  },
  degraded: {
    label: "Degraded",
    pillClass: "bg-amber-100 text-amber-800 border-amber-200",
    iconClass: "text-amber-600",
    Icon: AlertCircle,
  },
  down: {
    label: "Down",
    pillClass: "bg-red-100 text-red-800 border-red-200",
    iconClass: "text-red-600",
    Icon: XCircle,
  },
  unknown: {
    label: "Unknown",
    pillClass: "bg-slate-100 text-slate-700 border-slate-200",
    iconClass: "text-slate-500",
    Icon: HelpCircle,
  },
};

function fmtLatency(ms: number | null): string | null {
  if (ms === null || ms === undefined) return null;
  if (ms < 1) return "<1 ms";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(2)} s`;
}

function fmtTimestamp(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString([], {
      hour: "numeric",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return iso;
  }
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function StatusPage() {
  // ``staleTime: 0`` + ``refetchOnMount: 'always'`` ensures every nav to
  // this page kicks a fresh probe; an operator who just clicked "Status"
  // never wants a 30-second-old answer. The slowest single check (the
  // AI generate ping) bounds total latency to a few seconds.
  const { data, isLoading, isFetching, refetch, dataUpdatedAt, error } =
    useQuery<SystemStatusReport>({
      queryKey: ["system-status"],
      queryFn: () => api.get<SystemStatusReport>("/api/admin/status"),
      staleTime: 0,
      refetchOnMount: "always",
      refetchOnWindowFocus: true,
    });

  const overall = data?.overall ?? "unknown";
  const overallMeta = LEVEL_META[overall];

  const counts = useMemo(() => {
    const out: Record<SystemStatusLevel, number> = {
      ok: 0,
      degraded: 0,
      down: 0,
      unknown: 0,
    };
    for (const c of data?.checks ?? []) out[c.status] += 1;
    return out;
  }, [data]);

  return (
    <div>
      <PageHeader
        title="System status"
        description="Live probe of every local service Avi depends on. Visiting this page (or hitting Refresh) re-runs every check from scratch."
        actions={
          <button
            className="btn-secondary"
            onClick={() => refetch()}
            disabled={isFetching}
            title="Re-run every check"
          >
            <RefreshCw
              className={cn("h-4 w-4", isFetching && "animate-spin")}
            />
            {isFetching ? "Probing…" : "Refresh"}
          </button>
        }
      />

      {/* Overall summary card */}
      <div className="card mb-4">
        <div className="card-body flex flex-wrap items-center gap-4">
          <div className="flex items-center gap-3">
            <overallMeta.Icon
              className={cn("h-8 w-8", overallMeta.iconClass)}
            />
            <div>
              <div className="text-xs uppercase tracking-wide text-muted-foreground">
                Overall
              </div>
              <div className="text-lg font-semibold">{overallMeta.label}</div>
            </div>
          </div>
          <div className="hidden sm:block h-10 w-px bg-border" />
          <div className="flex items-center gap-4 text-sm">
            <CountChip n={counts.ok} label="healthy" level="ok" />
            <CountChip n={counts.degraded} label="degraded" level="degraded" />
            <CountChip n={counts.down} label="down" level="down" />
            {counts.unknown > 0 && (
              <CountChip
                n={counts.unknown}
                label="unknown"
                level="unknown"
              />
            )}
          </div>
          <div className="ml-auto text-xs text-muted-foreground">
            {dataUpdatedAt
              ? `Last checked ${fmtTimestamp(new Date(dataUpdatedAt).toISOString())}`
              : "Never checked"}
          </div>
        </div>
      </div>

      {/* Body */}
      {error ? (
        <div className="card">
          <div className="card-body text-sm text-destructive">
            <div className="font-medium mb-1">
              Could not fetch /api/admin/status.
            </div>
            <div className="text-muted-foreground">
              The backend may be down. {String((error as Error).message)}
            </div>
          </div>
        </div>
      ) : isLoading ? (
        <div className="card">
          <div className="card-body text-sm text-muted-foreground flex items-center gap-2">
            <Loader2 className="h-4 w-4 animate-spin" />
            Running every probe…
          </div>
        </div>
      ) : (
        <div className="space-y-2">
          {(data?.checks ?? []).map((check) => (
            <CheckRow key={check.key} check={check} />
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Subcomponents
// ---------------------------------------------------------------------------

function CountChip({
  n,
  label,
  level,
}: {
  n: number;
  label: string;
  level: SystemStatusLevel;
}) {
  const meta = LEVEL_META[level];
  return (
    <div
      className={cn(
        "inline-flex items-center gap-1 px-2 py-1 rounded-md border text-xs font-medium",
        n > 0 ? meta.pillClass : "bg-muted text-muted-foreground border-border",
      )}
    >
      <span className="tabular-nums">{n}</span>
      <span className="opacity-80">{label}</span>
    </div>
  );
}

function CheckRow({ check }: { check: SystemStatusCheck }) {
  const meta = LEVEL_META[check.status];
  const [open, setOpen] = useState(check.status !== "ok");

  const detailEntries = useMemo(
    () =>
      Object.entries(check.detail ?? {}).filter(
        ([, v]) => v !== null && v !== undefined && v !== "",
      ),
    [check.detail],
  );

  const latency = fmtLatency(check.latency_ms);

  return (
    <div
      className={cn(
        "card overflow-hidden border-l-4",
        check.status === "ok" && "border-l-emerald-400",
        check.status === "degraded" && "border-l-amber-400",
        check.status === "down" && "border-l-red-500",
        check.status === "unknown" && "border-l-slate-300",
      )}
    >
      <button
        type="button"
        className="w-full text-left card-body flex items-center gap-3 hover:bg-muted/40 transition-colors"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <meta.Icon className={cn("h-5 w-5 shrink-0", meta.iconClass)} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            <div className="font-semibold text-sm">{check.label}</div>
            <span
              className={cn(
                "text-[10px] px-1.5 py-0.5 rounded border uppercase tracking-wide",
                meta.pillClass,
              )}
            >
              {meta.label}
            </span>
            {latency && (
              <span className="text-xs text-muted-foreground tabular-nums">
                · {latency}
              </span>
            )}
          </div>
          <div className="text-sm text-muted-foreground mt-0.5 truncate">
            {check.summary}
          </div>
        </div>
        {open ? (
          <ChevronDown className="h-4 w-4 text-muted-foreground shrink-0" />
        ) : (
          <ChevronRight className="h-4 w-4 text-muted-foreground shrink-0" />
        )}
      </button>

      {open && (
        <div className="border-t border-border bg-muted/30 px-5 py-3 space-y-3">
          {check.hint && (
            <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900 leading-relaxed">
              <span className="font-semibold">Tip: </span>
              {check.hint}
            </div>
          )}

          {detailEntries.length === 0 ? (
            <div className="text-xs text-muted-foreground italic">
              No additional detail.
            </div>
          ) : (
            <dl className="grid grid-cols-1 sm:grid-cols-3 gap-x-4 gap-y-2 text-xs">
              {detailEntries.map(([k, v]) => (
                <div key={k} className="sm:col-span-3 grid grid-cols-3 gap-3">
                  <dt className="col-span-1 font-medium text-muted-foreground uppercase tracking-wide text-[10px] pt-0.5">
                    {k}
                  </dt>
                  <dd className="col-span-2 text-foreground/90 break-all font-mono">
                    {renderDetailValue(v)}
                  </dd>
                </div>
              ))}
            </dl>
          )}

          <div className="text-[10px] text-muted-foreground/80">
            Checked at {fmtTimestamp(check.checked_at)}
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * Cheap renderer for arbitrary JSON values returned by the backend's
 * ``detail`` blob — strings/numbers go inline, arrays become a tight
 * comma list, objects are pretty-printed in a code block. Anything we
 * don't recognise falls back to ``JSON.stringify``.
 */
function renderDetailValue(v: unknown): JSX.Element {
  if (Array.isArray(v)) {
    if (v.length === 0) return <span className="opacity-50">∅</span>;
    if (v.every((item) => typeof item !== "object" || item === null)) {
      return <>{v.map((x) => String(x)).join(", ")}</>;
    }
    return (
      <pre className="text-[11px] bg-white border border-border rounded-md p-2 overflow-auto max-h-48">
        {JSON.stringify(v, null, 2)}
      </pre>
    );
  }
  if (v && typeof v === "object") {
    return (
      <pre className="text-[11px] bg-white border border-border rounded-md p-2 overflow-auto max-h-48">
        {JSON.stringify(v, null, 2)}
      </pre>
    );
  }
  if (typeof v === "boolean") {
    return <>{v ? "true" : "false"}</>;
  }
  return <>{String(v)}</>;
}

// Thin fetch wrapper. Dev server proxies /api to http://localhost:8000 (see vite.config.ts).

export class ApiError extends Error {
  status: number;
  body: unknown;
  constructor(status: number, message: string, body: unknown) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

async function request<T>(
  path: string,
  init: RequestInit = {},
  parseJson = true
): Promise<T> {
  const headers = new Headers(init.headers);
  if (!(init.body instanceof FormData) && init.body != null) {
    headers.set("Content-Type", "application/json");
  }
  const res = await fetch(path, { ...init, headers });
  if (!res.ok) {
    let body: unknown = null;
    try {
      body = await res.json();
    } catch {
      /* noop */
    }
    const message = formatErrorMessage(res.status, path, body);
    throw new ApiError(res.status, message, body);
  }
  if (!parseJson || res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// FastAPI's 422 body looks like `{"detail": [{"loc": ["body", "field"],
// "msg": "Field required", "type": "..."}, ...]}`. Flatten it into a
// readable one-line string so toasts say exactly which field broke.
function formatErrorMessage(status: number, path: string, body: unknown): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail) && detail.length > 0) {
    const parts: string[] = [];
    for (const item of detail) {
      if (!item || typeof item !== "object") continue;
      const rec = item as { loc?: unknown[]; msg?: string };
      const loc = Array.isArray(rec.loc)
        ? rec.loc.filter((p) => p !== "body").join(".")
        : "";
      const msg = rec.msg ?? "Invalid value";
      parts.push(loc ? `${loc}: ${msg}` : msg);
    }
    if (parts.length > 0) return parts.join("; ");
  }
  return `Request to ${path} failed with ${status}`;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, {
      method: "POST",
      body: body == null ? undefined : JSON.stringify(body),
    }),
  patch: <T>(path: string, body: unknown) =>
    request<T>(path, { method: "PATCH", body: JSON.stringify(body) }),
  del: (path: string) => request<void>(path, { method: "DELETE" }, false),
  upload: <T>(path: string, form: FormData) =>
    request<T>(path, { method: "POST", body: form }),
};

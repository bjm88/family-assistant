// Shared helpers for preparing react-hook-form values to send to FastAPI.
//
// FastAPI + Pydantic reject empty strings for Optional[date], Optional[int],
// Optional[Decimal], etc. These helpers convert empty-string inputs to
// `undefined` (so the key is dropped) or coerce them into the right type.

/** Strip keys whose value is an empty string, null, or undefined. */
export function stripEmpty<T extends Record<string, unknown>>(obj: T): Partial<T> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(obj)) {
    if (v === "" || v === null || v === undefined) continue;
    out[k] = v;
  }
  return out as Partial<T>;
}

/**
 * Coerce specific keys to numbers. Empty strings / null / undefined become
 * `undefined` so the key is dropped entirely. Non-numeric strings are
 * coerced with `Number()` so typed inputs (`type="number"`) that come back
 * as strings still round-trip correctly.
 */
export function coerceNumbers<T extends Record<string, unknown>>(
  obj: T,
  keys: (keyof T)[]
): T {
  const out: Record<string, unknown> = { ...obj };
  for (const key of keys) {
    const v = out[key as string];
    if (v === "" || v === null || v === undefined) {
      delete out[key as string];
      continue;
    }
    const n = typeof v === "number" ? v : Number(v);
    if (Number.isNaN(n)) {
      delete out[key as string];
    } else {
      out[key as string] = n;
    }
  }
  return out as T;
}

/**
 * Standard payload cleaner: coerce numeric keys then strip the rest of the
 * empty values. Use this on the way out to the API.
 */
export function cleanPayload<T extends Record<string, unknown>>(
  obj: T,
  numericKeys: (keyof T)[] = []
): Partial<T> {
  return stripEmpty(coerceNumbers(obj, numericKeys));
}

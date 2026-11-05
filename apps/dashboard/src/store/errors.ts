/** Turn an RTK Query error (or anything thrown) into an Error with a readable message. */
export function toError(e: unknown): Error {
  if (typeof e === "object" && e && "status" in e) {
    const { status, data } = e as { status: unknown; data?: { detail?: unknown } };
    const detail = typeof data?.detail === "string" ? `: ${data.detail}` : "";
    return new Error(`HTTP ${String(status)}${detail}`);
  }
  return e instanceof Error ? e : new Error(String(e));
}
